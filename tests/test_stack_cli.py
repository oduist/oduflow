import sys
from unittest.mock import patch

from oduflow.settings import Settings, TeamSettings
from oduflow.stack_ops import StackPlan


def test_stack_validate_cli(tmp_path, capsys):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text(
        """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata: {name: demo}
spec:
  environment:
    name: demo
    branch: main
    repoUrl: https://github.com/acme/demo.git
    odooImage: odoo:18.0
""",
        encoding="utf-8",
    )
    from oduflow import server

    with patch.object(sys, "argv", ["oduflow", "stack", "validate", str(manifest)]):
        server._run_cli()

    assert "Stack 'demo' is valid" in capsys.readouterr().out


def test_startup_stack_is_applied_before_server(tmp_path):
    from oduflow import server

    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_password="password",
        teams={"1": team},
    )
    manifest = object()
    events = []

    with (
        patch.object(
            sys,
            "argv",
            ["oduflow", "--stack", "/stack/oduflow.yaml", "--transport", "http"],
        ),
        patch.object(server, "find_toml"),
        patch.object(server, "_get_settings", return_value=settings),
        patch.object(server.migrations, "run_pending"),
        patch.object(server, "_ensure_initialized"),
        patch.object(server.quotas, "apply_all"),
        patch("oduflow.stack_loader.load_stack", return_value=manifest),
        patch(
            "oduflow.stack_ops.apply_stack",
            side_effect=lambda *a, **k: events.append("stack") or StackPlan("demo", ()),
        ),
        patch.object(
            server, "_start_http", side_effect=lambda: events.append("server")
        ),
    ):
        server._run_cli()

    assert events == ["stack", "server"]
