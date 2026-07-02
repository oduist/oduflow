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
    meta_path = team.get_template_metadata_path("zipfit")
    meta = json.loads(open(meta_path).read())
    assert meta["odoo_image"] == "odoo:18.0"
    assert meta["source"] == "odoo.sh"
    assert meta["source_db"] == "zipfit-odoo-main-7950600"

    # dump (gzip bytes, contents irrelevant here)
    r = client.post(
        "/api/templates/import/dump", headers=hdr, content=b"\x1f\x8bFAKEGZIP"
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    dump_path = os.path.join(team.get_template_dir("zipfit"), "dump.sql.gz")
    assert os.path.isfile(dump_path)

    # filestore chunk "ab"
    tar = _tar_bytes({"ab/abcdef0123": b"BLOBDATA"})
    r = client.post(
        "/api/templates/import/filestore?chunk=ab", headers=hdr, content=tar
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    extracted = os.path.join(
        team.get_template_filestore_path("zipfit"), "ab", "abcdef0123"
    )
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
