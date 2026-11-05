"""The production lock (agents/locks.py): translator deploys and cutover-gate flips are mutually exclusive."""
import os
import subprocess
import sys
import threading
import time

import pytest

from agents.cutover_agent import CutoverAgent
from agents.locks import production_lock, production_lock_path
from agents.provision import RoomInfo
from agents.switch_client import Settings
from tests.fakes import FakeRoom
from tests.test_translator_agent import make_agent as make_translator

SETTINGS = Settings(*(["unused"] * 8))


def _hold(path, ready: threading.Event, release: threading.Event) -> threading.Thread:
    def run():
        with production_lock(timeout=5, path=path):
            ready.set()
            release.wait(10)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(5)
    return t


def test_lock_is_exclusive_within_and_across_processes(tmp_path):
    path = tmp_path / "production.lock"
    ready, release = threading.Event(), threading.Event()
    holder = _hold(path, ready, release)
    with pytest.raises(TimeoutError, match="another production change"):
        with production_lock(timeout=0.3, path=path):
            pass
    release.set()
    holder.join(5)
    with production_lock(timeout=1, path=path):
        assert os.stat(path).st_mode & 0o777 == 0o600
    child = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(1.5)\n")],
        stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held"
    with pytest.raises(TimeoutError):
        with production_lock(timeout=0.3, path=path):
            pass
    child.wait(10)
    with production_lock(timeout=1, path=path):  # released when the holder process exits
        pass


def test_lock_path_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("STRANGLER_PRODUCTION_LOCK", str(tmp_path / "x.lock"))
    assert production_lock_path() == tmp_path / "x.lock"
    monkeypatch.delenv("STRANGLER_PRODUCTION_LOCK")
    monkeypatch.setenv("STRANGLER_STATE_DIR", str(tmp_path / "state"))
    assert production_lock_path() == tmp_path / "state" / "production.lock"


def test_translator_does_not_deploy_while_a_flip_holds_the_lock(tmp_path):
    room = FakeRoom()
    agent = make_translator(room, tmp_path / "lists.py", production_lock_timeout=0.3)
    calls = []
    agent._decide = lambda *a: calls.append(a)  # the build-changing step must not run
    ready, release = threading.Event(), threading.Event()
    holder = _hold(production_lock_path(), ready, release)
    room.human_says("@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5")
    room.pump({"translator": agent})
    assert not calls and "a cutover or rollback is in progress" in room.bodies("translator")[-1]
    release.set()
    holder.join(5)
    room.human_says("@translator preserve D-1 D-2 D-4 and deprecate D-3 D-5")
    room.pump({"translator": agent})
    assert len(calls) == 1


def test_cutover_gate_does_not_flip_while_a_deploy_holds_the_lock(tmp_path):
    room = FakeRoom()
    info = RoomInfo(room_id=room.room_id, name="strangler-migration", channel_id="c", channel_url=None,
                    agents=room.agent_ids, human=room.human)
    gate = CutoverAgent(settings=SETTINGS, room=info, switch=room.agent("cutover-gate"), router_token="t",
                        production_lock_timeout=0.3)
    calls = []
    gate._approve = lambda *a: calls.append(a)
    gate._rollback = lambda *a: calls.append(a)
    ready, release = threading.Event(), threading.Event()
    holder = _hold(production_lock_path(), ready, release)
    for verb in ("approve-cutover", "rollback"):
        room.human_says(f"@cutover-gate {verb}")
        room.pump({"cutover-gate": gate})
        assert not calls and "the translator is deploying a build" in room.bodies("cutover-gate")[-1], verb
    release.set()
    holder.join(5)
    room.human_says("@cutover-gate approve-cutover")
    room.pump({"cutover-gate": gate})
    assert len(calls) == 1
