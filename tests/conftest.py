"""Test-wide isolation: no test ever takes the live deployment's production lock in .state/."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_production_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("STRANGLER_PRODUCTION_LOCK", str(tmp_path / "production.lock"))
