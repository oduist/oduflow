import hashlib
import json
from unittest.mock import patch

import pytest

from oduflow.docker_ops import system_ops
from oduflow.errors import ConflictError, NotFoundError
from oduflow.settings import Settings, TeamSettings


def _template(tmp_path, name="default", content='{"odoo_image":"odoo:18.0"}'):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path))
    template_dir = tmp_path / "templates" / name
    template_dir.mkdir(parents=True)
    metadata_path = template_dir / "metadata.json"
    if content is not None:
        metadata_path.write_text(content, encoding="utf-8")
    return team, metadata_path


def test_get_template_metadata_returns_raw_content_and_revision(tmp_path):
    raw = '{"custom": true, "odoo_image": "odoo:18.0"}\n'
    team, _ = _template(tmp_path, content=raw)

    result = system_ops.get_template_metadata(team, "default")

    assert result["content"] == raw
    assert result["revision"] == hashlib.sha256(raw.encode()).hexdigest()


def test_get_template_metadata_allows_creating_missing_file(tmp_path):
    team, metadata_path = _template(tmp_path, content=None)

    opened = system_ops.get_template_metadata(team, "default")
    updated = system_ops.update_template_metadata(
        team,
        "default",
        '{"custom": "value"}',
        opened["revision"],
    )

    assert opened["content"] == "{}\n"
    assert json.loads(updated["content"]) == {"custom": "value"}
    assert json.loads(metadata_path.read_text()) == {"custom": "value"}


def test_update_template_metadata_preserves_custom_fields_and_formats_json(tmp_path):
    team, metadata_path = _template(tmp_path)
    opened = system_ops.get_template_metadata(team, "default")

    result = system_ops.update_template_metadata(
        team,
        "default",
        '{"odoo_image":"odoo:19.0","custom":{"enabled":true}}',
        opened["revision"],
    )

    assert result["content"].endswith("\n")
    assert json.loads(metadata_path.read_text()) == {
        "odoo_image": "odoo:19.0",
        "custom": {"enabled": True},
    }
    assert result["revision"] == hashlib.sha256(metadata_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("content", ["{broken", "[]", "null"])
def test_update_template_metadata_rejects_invalid_content_without_modifying_file(
    tmp_path, content
):
    team, metadata_path = _template(tmp_path)
    original = metadata_path.read_bytes()
    revision = system_ops.get_template_metadata(team, "default")["revision"]

    with pytest.raises((TypeError, ValueError)):
        system_ops.update_template_metadata(team, "default", content, revision)

    assert metadata_path.read_bytes() == original


def test_update_template_metadata_rejects_stale_revision(tmp_path):
    team, metadata_path = _template(tmp_path)
    opened = system_ops.get_template_metadata(team, "default")
    metadata_path.write_text('{"changed": true}', encoding="utf-8")

    with pytest.raises(ConflictError, match="changed after it was opened"):
        system_ops.update_template_metadata(
            team,
            "default",
            '{"replacement": true}',
            opened["revision"],
        )

    assert json.loads(metadata_path.read_text()) == {"changed": True}


def test_template_metadata_validates_name_and_template_existence(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path))

    with pytest.raises(ValueError):
        system_ops.get_template_metadata(team, "../escape")
    with pytest.raises(NotFoundError, match="not found"):
        system_ops.get_template_metadata(team, "missing")


def test_list_templates_keeps_invalid_metadata_available_for_repair(tmp_path):
    team, metadata_path = _template(tmp_path, content="{broken")
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})

    with (
        patch("oduflow.docker_ops.system_ops.get_client"),
        patch("oduflow.docker_ops.system_ops._db_exists", return_value=False),
    ):
        result = system_ops.list_templates(settings, team)

    assert result[0]["metadata_valid"] is False
    assert metadata_path.read_text() == "{broken"
