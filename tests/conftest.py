"""Test-wide isolation: no test ever takes the live deployment's production lock in .state/, and no test ever runs
with per-component OS-user isolation on (docs/ISOLATION.md §3): nothing drops privileges, syncs the service area or
touches the host, whatever marker the host has."""
import os

import pytest


def pytest_configure(config):
    # Module- and session-scoped fixtures run before the function-level autouse fixture below: force "off" for the
    # whole test process too, so no fixture-launched script or launcher ever sees a host's isolation marker.
    os.environ["STRANGLER_ISOLATION"] = "off"


@pytest.fixture(autouse=True)
def _isolated_production_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("STRANGLER_PRODUCTION_LOCK", str(tmp_path / "production.lock"))


@pytest.fixture(autouse=True)
def _isolation_off(monkeypatch):
    monkeypatch.setenv("STRANGLER_ISOLATION", "off")
