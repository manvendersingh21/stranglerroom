"""Test-wide isolation: no test ever takes the live deployment's production lock in .state/, and no test ever runs
with per-component OS-user isolation on (docs/ISOLATION.md §3): nothing drops privileges, syncs the service area or
touches the host, whatever marker the host has."""
import os
import shutil
import tempfile
from pathlib import Path

import pytest

#: The host's real isolation (docs/ISOLATION.md): its marker and the agents' API-key copies. `clean`, `disable` and
#: `reset` act on the default service area when no marker names another, so a test that forgot to point them at a
#: temporary one would damage a live isolated deployment. The session fails if either changed while it ran.
_HOST_GUARDED = (Path("/etc/stranglerroom"), Path("/srv/stranglerroom/state/agents"))


def _host_fingerprint() -> dict:
    out: dict = {}
    for root in _HOST_GUARDED:
        try:
            entries = [root, *sorted(root.rglob("*"))] if root.exists() else []
        except OSError:
            entries = []
        for entry in entries:
            try:
                st = entry.lstat()
            except OSError:
                continue
            out[str(entry)] = (st.st_mode, st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def pytest_configure(config):
    # Module- and session-scoped fixtures run before the function-level autouse fixtures below, so the whole test
    # process gets safe values first: isolation forced "off" (no fixture-launched script or launcher ever sees a
    # host's isolation marker), the production lock and every spawned target's state dir in a per-session temp dir
    # (nothing a test starts ever writes into the real deployment's .state).
    os.environ["STRANGLER_ISOLATION"] = "off"
    session_dir = tempfile.mkdtemp(prefix="stranglerroom-tests-")
    config.add_cleanup(lambda: shutil.rmtree(session_dir, ignore_errors=True))
    os.environ["STRANGLER_PRODUCTION_LOCK"] = os.path.join(session_dir, "production.lock")
    os.environ["STRANGLER_TEST_SERVICE_STATE_DIR"] = os.path.join(session_dir, "service-state")
    config._stranglerroom_host_fingerprint = _host_fingerprint()


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_stranglerroom_host_fingerprint", None)
    if before is None:
        return
    after = _host_fingerprint()
    if after != before:
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"ERROR: the test session modified the host's isolation: {changed}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _isolated_production_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("STRANGLER_PRODUCTION_LOCK", str(tmp_path / "production.lock"))


@pytest.fixture(autouse=True)
def _isolation_off(monkeypatch):
    monkeypatch.setenv("STRANGLER_ISOLATION", "off")
