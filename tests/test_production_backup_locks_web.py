"""REST snapshot/restore must serialize with prune on the backup-store key,
mirroring the MCP tools (production lock first, then the team's backup store)."""

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager, prod_backups_lock_key
from oduflow.server import prod_lock_key
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path) -> tuple[TestClient, LockManager]:
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        prod_enabled=True,
        teams={"1": team},
    )
    app = Starlette()
    locks = LockManager()
    mount_web_ui(app, lambda: settings, locks)
    return TestClient(app), locks


def _assert_prod_lock_released(locks: LockManager) -> None:
    locks.acquire_env(prod_lock_key("1", "erp"))
    locks.release_env(prod_lock_key("1", "erp"))


def test_rest_snapshot_bounces_off_a_running_prune(tmp_path):
    client, locks = _client(tmp_path)
    key = prod_backups_lock_key("1")
    locks.acquire_env(key, operation="prune_production_backups")
    try:
        response = client.post("/api/productions/erp/snapshot")
    finally:
        locks.release_env(key)

    assert response.status_code == 409
    assert "prune_production_backups" in response.json()["error"]
    _assert_prod_lock_released(locks)


def test_rest_restore_bounces_off_a_running_prune(tmp_path):
    client, locks = _client(tmp_path)
    key = prod_backups_lock_key("1")
    locks.acquire_env(key, operation="prune_production_backups")
    try:
        response = client.post(
            "/api/productions/erp/restore",
            json={"confirm": "erp", "snapshot_id": "snap-1"},
        )
    finally:
        locks.release_env(key)

    assert response.status_code == 409
    assert "prune_production_backups" in response.json()["error"]
    _assert_prod_lock_released(locks)
