import pytest

from jplay.common import system


@pytest.fixture(autouse=True)
def own_state(tmp_path, monkeypatch):
    """Every test gets its own /var/lib/jplay and /etc/jplay/roles."""
    monkeypatch.setenv("JPLAY_STATE", str(tmp_path / "jplay-state"))
    monkeypatch.setattr(system, "ROLES_FILE", tmp_path / "roles")
    system.state_root.cache_clear()
    system.updates.cache_clear()
    system.sysupdates.cache_clear()
    yield
    system.state_root.cache_clear()
    system.updates.cache_clear()
