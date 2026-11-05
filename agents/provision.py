"""Provision the StranglerRoom on a running Switch 0.31.0 server.

    python -m agents.provision [--fresh-room]

1. Register (or reuse stored credentials for) the three agents: translator, fuzzer, cutover-gate.
2. Log in to the Gateway as the admin and clear each agent's addressing policy
   (``PUT /gateway/agents/{id}/addressing-policy {"policy": null}``). New agents default to
   "owner + owner's agents"; bootstrap-registered agents are owned by a non-admin bootstrap user,
   so without this the human (and any cross-owner agent) is refused with
   "You're not permitted to direct messages to me in this room."
3. Create the room ``strangler-migration`` via ``POST /agents/{translator}/ops/create_room`` with all
   three agents and the Mattermost human ``user`` — or reuse the existing, unarchived one.
   ``--fresh-room`` archives the existing room first so a demo starts on an empty channel.
4. Write ``.state/room.json`` (room id, Mattermost channel id/url) for the agents and the demo driver.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from agents.switch_client import STATE_DIR, AgentCredentials, Settings, SwitchAgent, SwitchGateway, register_agent

log = logging.getLogger("strangler.provision")

ROOM_NAME = "strangler-migration"
ROOM_DESCRIPTION = ("Shadow migration of Open Library's legacy web.py lists endpoints to FastAPI "
                    "(internetarchive/openlibrary#12487): translate, differential-fuzz, human-gated cutover.")
ROOM_INSTRUCTIONS = (
    "Pipeline: @translator ports legacy handlers to FastAPI and hands off to @fuzzer; @fuzzer shadow-tests "
    "legacy :9001 vs modern :9002 and reports divergences D-n; the human lead decides per divergence "
    "(`@translator preserve D-… and deprecate D-…`); @cutover-gate benchmarks and flips the :9000 proxy only "
    "after `@cutover-gate approve-cutover` from the human. Agents never approve on a human's behalf."
)

AGENTS: dict[str, tuple[str, str]] = {
    "translator": ("Translator", "Migration architect: extracts legacy web.py routes and writes the typed FastAPI port."),
    "fuzzer": ("Fuzzer", "Adversarial differential shadow tester: diffs legacy vs modern and clusters divergences."),
    "cutover-gate": ("Cutover Gate", "Benchmarks p50/p95/p99 and flips the strangler proxy after human sign-off."),
}
HUMAN = "user"


@dataclass(frozen=True)
class RoomInfo:
    room_id: str
    name: str
    channel_id: str | None
    channel_url: str | None
    agents: dict[str, str]  # name -> agent id
    human: str

    @staticmethod
    def path():
        return STATE_DIR / "room.json"

    def save(self) -> None:
        """Atomically publish ``room.json`` as 0644 whatever the umask: every agent reads it, and when isolated
        each agent is another OS user (docs/ISOLATION.md). It holds no secret (ids, names, the channel URL)."""
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".room.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(self), indent=2))
                fh.flush()
                os.fchmod(fh.fileno(), 0o644)
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls) -> "RoomInfo":
        if not cls.path().is_file():
            raise RuntimeError("no .state/room.json — run `python -m agents.provision` first")
        return cls(**json.loads(cls.path().read_text()))


def ensure_agents(settings: Settings) -> dict[str, AgentCredentials]:
    creds = {name: register_agent(settings, name, desc, display) for name, (display, desc) in AGENTS.items()}
    gw = SwitchGateway(settings)
    try:
        for name, c in creds.items():
            detail = gw.set_addressing_policy(c.agent_id, None)
            log.info("addressing policy of %s cleared (owner=%s)", name, detail.get("owner_name") or detail.get("owner_id"))
    finally:
        gw.close()
    return creds


def ensure_room(settings: Settings, creds: dict[str, AgentCredentials], *, fresh: bool) -> RoomInfo:
    translator = SwitchAgent(settings, creds["translator"])
    try:
        existing = [r for r in translator.list_rooms() if r.get("name") == ROOM_NAME and not r.get("archived")]
        if fresh:
            for r in existing:
                log.info("archiving previous room %s", r["room_id"])
                translator.archive_room(r["room_id"])
            existing = []
        if existing:
            room_id = existing[0]["room_id"]
            detail = translator.room_detail(room_id)
            for name in creds:
                if name not in (detail.get("agent_names") or []):
                    translator.invite_agent(room_id, name)
            if HUMAN not in (detail.get("connected_user_names") or []):
                translator.add_users(room_id, [HUMAN])
            log.info("reusing room %s (%s)", ROOM_NAME, room_id)
        else:
            created = translator.create_room(name=ROOM_NAME, description=ROOM_DESCRIPTION,
                                             agent_names=list(creds), user_names=[HUMAN],
                                             instructions=ROOM_INSTRUCTIONS)
            failed = created.get("failed_attachments") or []
            if failed:
                raise RuntimeError(f"room created but some members could not be attached: {failed}")
            room_id = created["id"]
            log.info("created room %s (%s)", ROOM_NAME, room_id)
        detail = translator.room_detail(room_id)
    finally:
        translator.close()
    gw = SwitchGateway(settings)
    try:
        gdetail = gw.room(room_id)
    finally:
        gw.close()
    info = RoomInfo(room_id=room_id, name=ROOM_NAME, channel_id=gdetail.get("external_channel_id"),
                    channel_url=gdetail.get("external_channel_url"),
                    agents={n: c.agent_id for n, c in creds.items()}, human=HUMAN)
    missing = [n for n in creds if n not in (detail.get("agent_names") or [])]
    if missing:
        raise RuntimeError(f"agents missing from room {room_id}: {missing}")
    if not info.channel_id:
        raise RuntimeError(f"room {room_id} is not bridged to Mattermost (no external_channel_id): {gdetail}")
    info.save()
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fresh-room", action="store_true", help="archive any existing room and create a new one")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.load()
    creds = ensure_agents(settings)
    info = ensure_room(settings, creds, fresh=args.fresh_room)
    print(json.dumps(asdict(info), indent=2))
    _isolation_note()
    return 0


def _isolation_note() -> None:
    """With per-component OS users (docs/ISOLATION.md), each running agent reads its own copy of room.json and of its
    API key in the service area; the launcher refreshes those copies at every (re)start, not here."""
    try:
        from agents.isolation import isolation_on

        on = isolation_on()
    except Exception:  # noqa: BLE001 - advisory only: provisioning never depends on the isolation mode
        return
    if on:
        log.warning("isolation is on: running agents keep their previous copies of room.json and their API keys until "
                    "`bash scripts/agents.sh restart` (as root) re-copies them into the service area")


if __name__ == "__main__":
    sys.exit(main())
