"""In-memory stand-in for a Switch room, faithful to the Switch 0.31.0 semantics StranglerRoom relies on.

* ``FakeRoom`` holds participants, messages and room documents.
* ``FakeSwitchAgent`` exposes the subset of :class:`agents.switch_client.SwitchAgent` the agents use.
* Addressing follows Switch: a message addresses every participant it ``@mentions`` (token-bounded,
  case-insensitive), never its own sender; ``send_targeted`` prefixes ``@target`` like the server.
* Events have the real wire shape (``payload.sender_kind`` is ``None`` for humans AND agents, the human
  has ``sender_role: "owner"``), documents are creator-only for update/delete (403 ``SwitchError``).
* ``FakeRoom.pump(agents)`` delivers queued events to ``RoomAgent.dispatch`` until the room is quiet,
  which lets a test run the whole multi-agent loop in-process.
"""
from __future__ import annotations

import itertools
import queue
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agents.commands import mentions
from agents.switch_client import Event, SwitchError


@dataclass
class FakeMessage:
    id: str
    sender: str
    body: str
    at: float
    addressed: list[str] = field(default_factory=list)


class FakeRoom:
    def __init__(self, *, room_id: str = "room-1", agents: tuple[str, ...] = ("translator", "fuzzer", "cutover-gate"),
                 human: str = "user"):
        self.room_id = room_id
        self.human = human
        self.agent_ids = {name: f"agent-{name}" for name in agents}
        self.messages: list[FakeMessage] = []
        self.documents: dict[str, dict] = {}
        self.typing: list[tuple[str, bool]] = []
        self._queues: dict[str, queue.Queue[Event]] = {name: queue.Queue() for name in agents}
        self._seq = itertools.count(1)

    # ------------------------------------------------------------------ room API
    def participants(self) -> list[dict]:
        rows = [{"id": aid, "name": n, "type": "agent", "status": "live", "alias": None} for n, aid in self.agent_ids.items()]
        rows.append({"id": "ext-user-1", "name": self.human, "type": "user", "status": None, "alias": None})
        return rows

    def agent(self, name: str) -> "FakeSwitchAgent":
        return FakeSwitchAgent(self, name)

    def human_says(self, body: str) -> FakeMessage:
        return self._post(self.human, body)

    def agent_says_raw(self, agent: str, body: str) -> FakeMessage:
        """Post as an agent without going through its client (e.g. a compromised agent)."""
        return self._post(agent, body)

    def _post(self, sender: str, body: str) -> FakeMessage:
        msg = FakeMessage(id=f"sw_{uuid.uuid4().hex[:12]}", sender=sender, body=body, at=time.time())
        names = set(self.agent_ids) | {self.human}
        for target in dict.fromkeys(mentions(body)):
            if target in names and target != sender.lower():
                msg.addressed.append(target)
                if target in self._queues:
                    self._queues[target].put(self._event(msg))
        self.messages.append(msg)
        return msg

    def _event(self, msg: FakeMessage) -> Event:
        seq = next(self._seq)
        is_human = msg.sender == self.human
        return Event(name="message", sequence=seq, data={
            "type": "message", "room_id": self.room_id, "bridge_id": "bridge-1", "channel_type": "channel_public",
            "payload": {"addressed": True, "sender": f"@switch-{msg.sender}:localhost", "sender_name": msg.sender,
                        "sender_kind": None, "on_behalf_of": None, "message_id": msg.id, "body": msg.body,
                        "timestamp": int(msg.at * 1000), "thread_id": None, "attachments": [],
                        "opening_message": None, "sender_role": "owner" if is_human else None},
            "sequence": seq, "missed": {"count": 0, "reason": None}})

    def redeliver(self, msg: FakeMessage, to: str) -> None:
        """Queue ``msg`` for agent ``to`` again (same message id, new sequence) — a stream replay/redelivery."""
        assert to in msg.addressed, f"{msg.id} never addressed {to}"
        self._queues[to].put(self._event(msg))

    def pending(self, name: str) -> int:
        return self._queues[name].qsize()

    def pump(self, agents: dict[str, Any], *, max_events: int = 200) -> int:
        """Deliver queued events to each agent's ``dispatch`` until no agent has pending events."""
        delivered = 0
        while True:
            busy = [n for n in agents if not self._queues[n].empty()]
            if not busy:
                return delivered
            if delivered >= max_events:
                raise RuntimeError(f"room did not go quiet after {max_events} events (loop?)")
            for name in busy:
                agents[name].dispatch(self._queues[name].get())
                delivered += 1

    # ------------------------------------------------------------------ inspection helpers
    def bodies(self, sender: str | None = None) -> list[str]:
        return [m.body for m in self.messages if sender is None or m.sender == sender]

    def doc_by_name(self, name: str) -> dict | None:
        return next((d for d in self.documents.values() if d["name"] == name), None)


class FakeSwitchAgent:
    """Duck-typed :class:`agents.switch_client.SwitchAgent` bound to a :class:`FakeRoom`."""

    def __init__(self, room: FakeRoom, name: str):
        self.room = room
        self.name = name
        self.agent_id = room.agent_ids[name]
        self.connection_id = f"conn-{name}"
        self.connected_rooms: list[str] = []
        self.closed = False

    @property
    def is_live(self) -> bool:
        return bool(self.connected_rooms) and not self.closed

    def connect(self, rooms: list[str], *, delivery_filter: str = "addressed", timeout: float = 30) -> None:
        self.connected_rooms = list(rooms)

    def next_event(self, timeout: float | None = None) -> Event | None:
        try:
            return self.room._queues[self.name].get(timeout=timeout or 0.01)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.closed = True

    # messaging
    def post(self, body: str, *, thread_id: str | None = None) -> str:
        return self.room._post(self.name, body).id

    def send_targeted(self, body: str, targets: list[str], *, thread_id: str | None = None) -> dict:
        names = {p["name"] for p in self.room.participants()}
        unknown = [t for t in targets if t not in names]
        if unknown:
            raise SwitchError("POST", "send_targeted_message", 400, f"unknown targets {unknown}")
        msg = self.room._post(self.name, " ".join(f"@{t}" for t in targets) + " " + body)
        return {"event_id": msg.id, "target_statuses": {t: "live" for t in targets if t in self.room.agent_ids}}

    def participants(self) -> list[dict]:
        return self.room.participants()

    def set_typing(self, room_id: str, is_typing: bool) -> None:
        self.room.typing.append((self.name, is_typing))

    # documents (creator-only writes, unique names)
    def documents(self) -> list[dict]:
        return [{k: d[k] for k in ("id", "name", "description", "instructions", "scope", "created_by_agent_id")}
                for d in self.room.documents.values()]

    def create_document(self, *, name: str, description: str, instructions: str, content: str) -> str:
        if self.room.doc_by_name(name):
            raise SwitchError("POST", "create_room_document", 400, f"A room-scoped document named {name!r} already exists")
        doc_id = f"doc-{uuid.uuid4().hex[:8]}"
        self.room.documents[doc_id] = {"id": doc_id, "name": name, "description": description,
                                       "instructions": instructions, "content": content, "scope": "room",
                                       "created_by_agent_id": self.agent_id, "versions": 1}
        self.room._post(self.name, f"📄 created room document “{name}”.")
        return doc_id

    def update_document(self, document_id: str, *, content: str, description: str | None = None) -> None:
        doc = self.room.documents.get(document_id)
        if doc is None:
            raise SwitchError("POST", "update_room_document", 400, f"Room-scoped document {document_id} not found")
        if doc["created_by_agent_id"] != self.agent_id:
            raise SwitchError("POST", "update_room_document", 403,
                              f"Agent {self.agent_id} did not create document {document_id}")
        doc["content"] = content
        doc["versions"] += 1
        if description is not None:
            doc["description"] = description
        self.room._post(self.name, f"📄 updated room document “{doc['name']}”.")

    def load_documents(self, ids: list[str]) -> list[dict]:
        out = []
        for i in ids:
            doc = self.room.documents.get(i)
            if doc is None:
                raise SwitchError("POST", "load_internal_documents", 400, f"document {i} not attached to this room")
            out.append({"id": doc["id"], "name": doc["name"], "description": doc["description"],
                        "content": doc["content"], "room_seq": 0, "source_event_id": None})
        return out

    def upsert_document(self, *, name: str, description: str, instructions: str, content: str) -> str:
        existing = self.room.doc_by_name(name)
        if existing:
            if existing["created_by_agent_id"] != self.agent_id:
                raise RuntimeError(f"room document {name!r} exists but was created by {existing['created_by_agent_id']}")
            self.update_document(existing["id"], content=content, description=description)
            return existing["id"]
        return self.create_document(name=name, description=description, instructions=instructions, content=content)

    def read_context(self, room_id: str, limit: int = 50, before: str | None = None) -> dict:
        """Switch semantics: the newest ``limit`` messages strictly older than ``before`` (ISO-8601), newest last;
        ``truncated`` says whether even older ones exist; ``oldest_timestamp`` is this page's oldest (epoch ms)."""
        msgs = self.room.messages
        if before:
            cutoff = datetime.fromisoformat(before.replace("Z", "+00:00")).timestamp() * 1000
            msgs = [m for m in msgs if int(m.at * 1000) < cutoff]
        limit = max(1, min(int(limit), 500))  # Switch clamps limit to 1..500
        page = msgs[-limit:]
        return {"threads": [{"root": {"id": m.id, "kind": "message", "sender": m.sender, "sender_name": m.sender,
                                      "body": m.body, "timestamp": int(m.at * 1000), "attachments": []},
                             "replies": []} for m in page],
                "truncated": len(msgs) > len(page),
                # Switch: the MINIMUM sent_at over the page (not the lowest-seq row's), agent_core read_context
                "oldest_timestamp": min(int(m.at * 1000) for m in page) if page else None}
