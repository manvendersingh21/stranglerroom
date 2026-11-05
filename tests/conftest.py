"""Test-wide isolation: no test ever takes the live deployment's production lock in .state/, and no test ever runs
with per-component OS-user isolation on (docs/ISOLATION.md §3): nothing drops privileges, syncs the service area or
touches the host, whatever marker the host has."""
import os
import shutil
import tempfile

import pytest


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


@pytest.fixture(autouse=True)
def _isolated_production_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("STRANGLER_PRODUCTION_LOCK", str(tmp_path / "production.lock"))


@pytest.fixture(autouse=True)
def _isolation_off(monkeypatch):
    monkeypatch.setenv("STRANGLER_ISOLATION", "off")
