"""Common runtime for the three StranglerRoom room agents.

A :class:`RoomAgent` subclass implements :meth:`RoomAgent.handle`. The runtime:

* loads settings, the provisioned room (``.state/room.json``) and this agent's stored credentials;
* opens ONE Switch connection (``scope=single&filter=addressed&rooms=<room>``) with 2 s heartbeats;
* dispatches every *addressed* room message sequentially to ``handle`` — long jobs are fine, the
  stream and heartbeat threads keep the connection alive meanwhile;
* classifies the sender from the room's participant list (``type: "user"`` → human, ``"agent"`` →
  agent). ``sender_kind`` is NOT used: Switch 0.31.0 leaves it ``null`` for humans and agents alike;
* fails loud: a handler exception is logged with its traceback and reported in the room, and the
  agent keeps serving.

Mention hygiene: an @-mention *addresses* that participant. Agents only write ``@<agent>`` when they
deliberately hand off (``handoff``); instructions to the human name agents without ``@``.
"""
from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Iterator

from agents.commands import defuse_mentions
from agents.provision import RoomInfo
from agents.switch_client import AgentCredentials, Event, Settings, SwitchAgent

LEGACY_URL = os.environ.get("LEGACY_URL", "http://127.0.0.1:9001").rstrip("/")
MODERN_URL = os.environ.get("MODERN_URL", "http://127.0.0.1:9002").rstrip("/")
PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:9000").rstrip("/")
# The proxy admin token is resolved by targets/common/router_token.py (one rule shared with the proxy).


@dataclass(frozen=True)
class Sender:
    name: str
    #: "human" — the room's human lead (``RoomInfo.human``, a participant of type ``user``); "guest" — any other
    #: human participant; "agent"; "unknown" — not a participant, or a name that is ambiguous after case folding.
    #: Only "human" passes the human-only governance gates.
    kind: str

    @property
    def is_human(self) -> bool:
        return self.kind == "human"

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"


class RoomAgent:
    """Base class: subclasses set ``name`` and implement ``handle``."""

    name: str = ""

    def __init__(self, *, settings: Settings | None = None, room: RoomInfo | None = None,
                 switch: SwitchAgent | None = None):
        if not self.name:
            raise TypeError("RoomAgent subclasses must set `name`")
        self.log = logging.getLogger(f"strangler.{self.name}")
        self.settings = settings or Settings.load()
        self.room = room or RoomInfo.load()
        if switch is None:
            creds = AgentCredentials.load(self.name)
            if creds is None:
                raise RuntimeError(f"no credentials for {self.name} — run `python -m agents.provision` first")
            switch = SwitchAgent(self.settings, creds)
        self.switch = switch
        self.pace = float(os.environ.get("STRANGLER_AGENT_PACE", "0"))
        self._participants: dict[str, str] = {}
        self._stop = threading.Event()

    # ------------------------------------------------------------------ subclass API
    def handle(self, event: Event, sender: Sender) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_connected(self) -> None:
        """Hook run once the stream is live (e.g. rehydrate state from the room documents)."""

    # ------------------------------------------------------------------ helpers
    def classify(self, name: str) -> Sender:
        """Classify a sender by its room participant record (Switch's ``type``; ``sender_kind`` is unusable).

        Fail closed: only the configured human lead is ``human``; other people are ``guest``; a name that two
        participants share after case folding is ``unknown`` (it cannot be attributed). The participant list is
        re-read on every call (one cheap op per addressed message), so a participant who joined after an earlier
        lookup can never inherit a cached classification. A failing lookup raises (``dispatch`` reports it)."""
        key = (name or "").lower()
        fresh: dict[str, str] = {}
        for p in self.switch.participants():
            k = str(p.get("name") or "").lower()
            fresh[k] = "ambiguous" if k in fresh else str(p.get("type"))
        self._participants = fresh
        ptype = self._participants.get(key, "")
        if ptype == "user":
            kind = "human" if key == (self.room.human or "").lower() else "guest"
        else:
            kind = {"agent": "agent"}.get(ptype, "unknown")
        return Sender(name=name, kind=kind)

    def _pace(self) -> None:
        if self.pace > 0:
            time.sleep(self.pace)

    def say(self, body: str) -> str:
        """Post ``body`` to the room (no @-mentions of agents unless you mean to address them)."""
        self._pace()
        return self.switch.post(body)

    def handoff(self, target: str, body: str) -> dict:
        """Address ``target`` (an agent or the human) with ``body``; Switch prefixes ``@target``.
        Warns in the room if an agent target is not live, because then nothing will happen."""
        self._pace()
        result = self.switch.send_targeted(body, [target])
        status = (result.get("target_statuses") or {}).get(target)
        if status not in (None, "live"):
            self.switch.post(f"⚠️ handoff to {target} sent, but Switch reports it as `{status}` — "
                             f"start it with `scripts/agents.sh start`.")
        return result

    def ask_human(self, body: str) -> dict:
        return self.handoff(self.room.human, body)

    @contextlib.contextmanager
    def working(self) -> Iterator[None]:
        """Show a typing indicator (refreshed every 3 s) while a long job runs."""
        stop = threading.Event()

        def pump() -> None:
            while not stop.is_set():
                try:
                    self.switch.set_typing(self.room.room_id, True)
                except Exception as exc:  # noqa: BLE001 - cosmetic; never break the job for it
                    self.log.debug("typing indicator failed: %s", exc)
                stop.wait(3)

        t = threading.Thread(target=pump, name=f"{self.name}-typing", daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=5)
            with contextlib.suppress(Exception):
                self.switch.set_typing(self.room.room_id, False)

    # ------------------------------------------------------------------ main loop
    def stop(self) -> None:
        self._stop.set()

    def dispatch(self, event: Event) -> None:
        """Route one event to ``handle`` with sender classification and loud failure reporting."""
        if not (event.is_message and event.addressed) or event.room_id != self.room.room_id:
            return
        if (event.sender_name or "").lower() == self.name:
            return
        try:
            # inside the guard: a failed participant lookup is reported like any handler failure (the command is
            # not executed, the agent keeps serving) instead of killing the event loop
            sender = self.classify(event.sender_name)
            self.log.info("<- %s (%s): %s", sender.name, sender.kind, event.body[:200])
            self.handle(event, sender)
        except Exception as exc:  # noqa: BLE001 - the room must hear about every failure
            self.log.error("handler failed on %r:\n%s", event.body[:200], traceback.format_exc())
            try:
                # Quoted text is defused: echoing "@x" back would address x (mention hygiene).
                self.switch.post(f"⚠️ {self.name} failed while handling “{defuse_mentions(event.body[:120])}”: "
                                 f"`{type(exc).__name__}: {defuse_mentions(str(exc)[:300])}` — "
                                 f"see `.state/run/{self.name}.log`.")
            except Exception:  # noqa: BLE001
                self.log.error("could not report the failure to the room:\n%s", traceback.format_exc())

    def run_forever(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop())
        self.switch.connect([self.room.room_id])
        self.on_connected()
        self.log.info("%s is live in %s (%s)", self.name, self.room.name, self.room.room_id)
        try:
            while not self._stop.is_set():
                event = self.switch.next_event(timeout=1.0)
                if event is not None:
                    self.dispatch(event)
        finally:
            self.switch.close()
            self.log.info("%s stopped", self.name)


def main_for(agent_cls: type[RoomAgent]) -> None:
    """Entry point used by ``python -m agents.<agent>``."""
    logging.basicConfig(level=os.environ.get("STRANGLER_LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    agent_cls().run_forever()
