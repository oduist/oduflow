"""End-to-end tests for the push-based Odoo.sh template import endpoints."""

import io
import json
import os
import tarfile
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.docker_ops import system_ops
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    # No ui_password -> UI mounts without auth, so token endpoints are reachable.
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team1"))
    settings = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app), settings, team


def _mint(client, template_name="zipfit"):
    r = client.post(
        "/api/templates/import-token", json={"template_name": template_name}
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    return data["token"], data


def _tar_bytes(members):
    """Build an in-memory tar. `members` maps arcname -> file content (bytes)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_import_script_served(tmp_path):
    client, _s, _t = _client(tmp_path)
    r = client.get("/import-odoo.sh")
    assert r.status_code == 200
    assert "PGDATABASE" in r.text
    assert "backup.daily" in r.text
    assert "finalize" in r.text


def test_token_command_contains_server_and_token(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, data = _mint(client)
    assert token in data["command"]
    assert "/import-odoo.sh" in data["command"]
    assert data["template_name"] == "zipfit"


def test_full_push_flow(tmp_path):
    client, _settings, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}

    # status: nothing done yet
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["manifest"] is False
    assert st["progress"]["filestore_chunks"] == []

    # manifest
    manifest = {
        "name": "zipfit-odoo-main-7950600",
        "odoo_branch": "18.0",
        "revision": "deadbeef",
        "repository": "git@github.com:zipfit/odoo.git",
        "installed_modules": {"standard": "base,web", "custom": "zipfit_x"},
        "backup_datetime_utc": "2026-07-02 02:09:47",
    }
    r = client.post(
        "/api/templates/import/manifest",
        headers=hdr,
        content=json.dumps(manifest).encode(),
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    staging = team.get_import_staging_dir("zipfit")
    meta = json.loads(open(os.path.join(staging, "metadata.json")).read())
    assert meta["odoo_image"] == "odoo:18.0"
    assert meta["source"] == "odoo.sh"
    assert meta["source_db"] == "zipfit-odoo-main-7950600"
    # Nothing touches the live template until finalize.
    assert not os.path.exists(team.get_template_dir("zipfit"))

    # dump (gzip bytes, contents irrelevant here)
    r = client.post(
        "/api/templates/import/dump", headers=hdr, content=b"\x1f\x8bFAKEGZIP"
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert os.path.isfile(os.path.join(staging, "dump.sql.gz"))

    # filestore chunk "ab"
    tar = _tar_bytes({"ab/abcdef0123": b"BLOBDATA"})
    r = client.post(
        "/api/templates/import/filestore?chunk=ab", headers=hdr, content=tar
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    extracted = os.path.join(staging, "filestore", "ab", "abcdef0123")
    assert open(extracted, "rb").read() == b"BLOBDATA"

    # status reflects progress
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["manifest"] is True
    assert st["progress"]["dump"] is True
    assert st["progress"]["filestore_chunks"] == ["ab"]

    # re-uploading the same chunk does not duplicate it (resume idempotency)
    client.post("/api/templates/import/filestore?chunk=ab", headers=hdr, content=tar)
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["filestore_chunks"] == ["ab"]

    # finalize (mock the heavy DB restore)
    with patch.object(
        system_ops,
        "finalize_imported_template",
        return_value={
            "status": "imported",
            "template_name": "zipfit",
            "template_db": "oduflow_template_1_zipfit",
            "restore_seconds": 1.2,
            "affected_envs": [],
            "remount_failures": [],
        },
    ) as fin:
        r = client.post("/api/templates/import/finalize", headers=hdr)
    assert r.status_code == 200 and r.json()["ok"] is True
    fin.assert_called_once()
    assert fin.call_args.kwargs["staging_dir"] == staging
    assert r.json()["result"]["template_db"] == "oduflow_template_1_zipfit"

    # token invalidated after finalize
    st = client.get("/api/templates/import/status", headers=hdr)
    assert st.json()["ok"] is False


def test_resume_survives_a_fresh_token(tmp_path):
    """Progress is derived from disk, so a brand-new token (minted after the
    previous one expired mid-upload) sees what already landed."""
    client, _s, _t = _client(tmp_path)
    token_a, _ = _mint(client, "zipfit")
    hdr_a = {"Authorization": f"Bearer {token_a}"}
    manifest = {"name": "db", "odoo_branch": "18.0", "installed_modules": {}}
    client.post(
        "/api/templates/import/manifest",
        headers=hdr_a,
        content=json.dumps(manifest).encode(),
    )
    client.post("/api/templates/import/dump", headers=hdr_a, content=b"gz")
    client.post(
        "/api/templates/import/filestore?chunk=00",
        headers=hdr_a,
        content=_tar_bytes({"00/x": b"y"}),
    )

    # A different token for the same template still reports the staged progress.
    token_b, _ = _mint(client, "zipfit")
    st = client.get(
        "/api/templates/import/status",
        headers={"Authorization": f"Bearer {token_b}"},
    ).json()
    assert st["progress"]["manifest"] is True
    assert st["progress"]["dump"] is True
    assert st["progress"]["filestore_chunks"] == ["00"]


def test_finalize_requires_manifest_and_dump(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, _ = _mint(client)
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/templates/import/finalize", headers=hdr)
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_invalid_chunk_rejected(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, _ = _mint(client)
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/api/templates/import/filestore?chunk=../evil", headers=hdr, content=b"x"
    )
    assert r.status_code == 400


def test_bad_token_rejected(tmp_path):
    client, _s, _t = _client(tmp_path)
    r = client.get(
        "/api/templates/import/status",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.json()["ok"] is False


def test_filestore_tar_zip_slip_is_skipped(tmp_path):
    dest = tmp_path / "fs"
    dest.mkdir()
    tar_path = tmp_path / "evil.tar"
    with tarfile.open(tar_path, "w") as tf:
        data = b"pwn"
        info = tarfile.TarInfo(name="../escape")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        good = b"ok"
        info2 = tarfile.TarInfo(name="ab/file")
        info2.size = len(good)
        tf.addfile(info2, io.BytesIO(good))
    written = system_ops.extract_filestore_tar(str(tar_path), str(dest))
    assert written == 1
    assert (dest / "ab" / "file").read_bytes() == b"ok"
    assert not (tmp_path / "escape").exists()


def test_import_paths_do_not_bypass_auth_for_sibling_routes(tmp_path):
    """Regression: a public PREFIX /api/templates/import/ used to expose
    /api/templates/{name}/delete with name="import" without credentials. The
    ingest endpoints must be public as exact paths only."""
    team = TeamSettings(
        team_id="1", data_dir=str(tmp_path / "team1"), ui_password="secret"
    )
    settings = Settings(
        routing_mode="port", base_data_dir=str(tmp_path), teams={"1": team}
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    # Sibling route captured by {name}="import" must require auth.
    r = client.post("/api/templates/import/delete")
    assert r.status_code == 401

    # The token-authed ingest endpoint stays reachable without Basic auth
    # (404 = unknown token, i.e. it got past the auth middleware).
    r = client.get(
        "/api/templates/import/status",
        headers={"Authorization": "Bearer " + "a" * 24},
    )
    assert r.status_code == 404

    # Minting a token still requires the UI login.
    r = client.post("/api/templates/import-token", json={"template_name": "x"})
    assert r.status_code == 401


def test_existing_template_does_not_fake_resume(tmp_path):
    """Regression: progress must come from the staging dir, not the live
    template — otherwise re-importing into an existing template name skips
    every upload and finalize silently restores the OLD data."""
    client, _s, team = _client(tmp_path)
    tpl_dir = team.get_template_dir("zipfit")
    os.makedirs(os.path.join(tpl_dir, "filestore", "ab"))
    with open(team.get_template_metadata_path("zipfit"), "w") as f:
        f.write("{}")
    with open(os.path.join(tpl_dir, "dump.sql.gz"), "wb") as f:
        f.write(b"old")

    token, _ = _mint(client, "zipfit")
    st = client.get(
        "/api/templates/import/status",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert st["progress"]["manifest"] is False
    assert st["progress"]["dump"] is False
    assert st["progress"]["filestore_chunks"] == []


def test_corrupt_chunk_is_not_marked_complete(tmp_path):
    """Regression: a truncated/corrupt chunk tar must fail the request AND
    leave no chunk directory behind, so resume re-uploads it."""
    client, _s, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}

    r = client.post(
        "/api/templates/import/filestore?chunk=ab",
        headers=hdr,
        content=b"this is not a tar archive",
    )
    assert r.status_code == 500
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["filestore_chunks"] == []
    fs_dir = os.path.join(team.get_import_staging_dir("zipfit"), "filestore")
    assert not os.path.isdir(os.path.join(fs_dir, "ab"))

    # A good re-upload of the same chunk then completes it.
    r = client.post(
        "/api/templates/import/filestore?chunk=ab",
        headers=hdr,
        content=_tar_bytes({"ab/f": b"data"}),
    )
    assert r.status_code == 200
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["filestore_chunks"] == ["ab"]


def test_extract_filestore_chunk_missing_dir_fails_cleanly(tmp_path):
    """A tar that does not contain the expected <chunk>/ dir is rejected and
    leaves nothing behind."""
    import pytest

    from oduflow.errors import ExternalCommandError

    tar_path = tmp_path / "wrong.tar"
    with tarfile.open(tar_path, "w") as tf:
        info = tarfile.TarInfo(name="cd/file")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    fs = tmp_path / "fs"
    with pytest.raises(ExternalCommandError):
        system_ops.extract_filestore_chunk(str(tar_path), str(fs), "ab")
    assert not (fs / "ab").exists()
    assert not (fs / ".incoming_ab").exists()


def test_finalize_swaps_staging_into_template(tmp_path):
    """finalize_imported_template promotes the staged upload into the live
    template inside the remount guard and removes the staging dir."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})

    staging = team.get_import_staging_dir("zipfit")
    os.makedirs(os.path.join(staging, "filestore", "ab"))
    with open(os.path.join(staging, "filestore", "ab", "f"), "wb") as f:
        f.write(b"blob")
    with open(os.path.join(staging, "metadata.json"), "w") as f:
        json.dump({"odoo_version": "18.0"}, f)
    with open(os.path.join(staging, "dump.sql.gz"), "wb") as f:
        f.write(b"gz")

    # Pre-existing template content that must be replaced, not merged.
    tpl_dir = team.get_template_dir("zipfit")
    os.makedirs(os.path.join(tpl_dir, "filestore", "ff"))
    with open(os.path.join(tpl_dir, "dump.pgdump"), "wb") as f:
        f.write(b"old")

    remount = MagicMock(affected=[], failures=[])

    @contextmanager
    def fake_remount(*a, **kw):
        yield remount

    from oduflow.docker_ops import env_ops

    with (
        patch.object(system_ops, "get_client"),
        patch.object(env_ops, "remount_template_overlays", fake_remount),
        patch.object(system_ops, "get_odoo_uid_gid", return_value="101:101"),
        patch.object(system_ops, "chown_recursive"),
        patch.object(system_ops, "_update_template_sizes"),
        patch.object(
            system_ops,
            "reload_template",
            return_value={"template_db": "tdb", "restore_seconds": 1.0},
        ),
    ):
        result = system_ops.finalize_imported_template(
            settings, team, "zipfit", staging_dir=staging
        )

    assert result["template_db"] == "tdb"
    fs = team.get_template_filestore_path("zipfit")
    assert open(os.path.join(fs, "ab", "f"), "rb").read() == b"blob"
    assert not os.path.exists(os.path.join(fs, "ff"))  # old filestore replaced
    assert not os.path.exists(os.path.join(tpl_dir, "dump.pgdump"))  # stale gone
    assert os.path.isfile(os.path.join(tpl_dir, "dump.sql.gz"))
    assert (
        json.load(open(team.get_template_metadata_path("zipfit")))["odoo_version"]
        == "18.0"
    )
    assert not os.path.exists(staging)  # staging cleaned up
