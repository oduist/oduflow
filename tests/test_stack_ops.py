from pathlib import Path
from unittest.mock import patch

import pytest

from oduflow.errors import ConflictError
from oduflow.settings import Settings, TeamSettings
from oduflow.stack_loader import StackValidationError, load_stack, resolve_env_values
from oduflow.stack_models import ServiceRoute, ValueFrom
from oduflow.stack_ops import PlanAction, StackPlan, apply_stack, build_plan

MANIFEST = """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata:
  name: acme
spec:
  environment:
    name: acme-dev
    branch: main
    repoUrl: https://github.com/acme/addons.git
    odooImage: odoo:18.0
    template: default
    env:
      LOG_LEVEL: info
    modules:
      install: [acme_base]
  extraRepositories:
    enterprise:
      repoUrl: https://github.com/odoo/enterprise.git
      branch: "18.0"
  volumes:
    fs-data:
      description: FreeSWITCH data
  files:
    - source: files/fs.conf
      volume: fs-data
      path: config/fs.conf
  services:
    fs:
      image: example/fs:1
      port: 8080
      env:
        ODOO_URL:
          environmentField: url
      volumes:
        - source: fs-data
          target: /data
"""


@pytest.fixture(autouse=True)
def allow_example_repo_urls():
    with patch("oduflow.stack_ops.git_ops.validate_repo_url"):
        yield


@pytest.fixture
def stack_fixture(tmp_path):
    data_dir = tmp_path / "team"
    team = TeamSettings(
        team_id="1",
        data_dir=str(data_dir),
        port_registry_path=str(data_dir / "ports.json"),
        port_range_start=50000,
        port_range_end=50100,
        hostname="localhost",
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user="odoo",
        db_password="odoo",
        teams={"1": team},
    )
    path = tmp_path / "oduflow.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    files = tmp_path / "files"
    files.mkdir()
    (files / "fs.conf").write_text("profile=internal\n", encoding="utf-8")
    return settings, team, load_stack(path), str(path)


def test_host_environment_value_is_required():
    value = ValueFrom(fromEnv="MISSING_SECRET")

    with pytest.raises(StackValidationError, match="MISSING_SECRET"):
        resolve_env_values({"TOKEN": value}, environ={})


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
def test_plan_new_stack_has_dependency_ordered_actions(
    _services, _envs, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture

    plan = build_plan(settings, team, manifest, path)

    assert [(item.operation, item.resource) for item in plan.actions] == [
        ("create", "extraRepositories.enterprise"),
        ("create", "volumes.fs-data"),
        ("create", "environment"),
        ("write", "files.fs-data:config/fs.conf"),
        ("create", "services.fs"),
        ("install", "modules"),
    ]


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments")
def test_plan_refuses_to_adopt_existing_environment(
    envs, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    envs.return_value = [{"env_name": "acme-dev", "stack": "", "stack_resource": ""}]

    with (
        patch("oduflow.stack_ops.service_ops.list_services", return_value=[]),
        patch(
            "oduflow.stack_ops.env_ops.get_environment_info",
            return_value={"env_vars": {}},
        ),
        patch("oduflow.stack_ops._installed_modules", return_value=set()),
    ):
        plan = build_plan(settings, team, manifest, path)

    conflict = next(item for item in plan.actions if item.resource == "environment")
    assert conflict.operation == "conflict"
    assert "not owned" in conflict.detail


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
@patch("oduflow.stack_ops.env_ops.get_environment_info")
@patch("oduflow.stack_ops.env_ops.list_environments")
def test_plan_is_empty_when_owned_environment_matches(
    envs, env_info, _services, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    manifest.spec.extra_repositories = {}
    manifest.spec.volumes = {}
    manifest.spec.files = []
    manifest.spec.services = {}
    manifest.spec.environment.modules.install = []
    envs.return_value = [
        {
            "env_name": "acme-dev",
            "stack": "acme",
            "stack_resource": "environment",
            "repo_url": "https://github.com/acme/addons.git",
            "git_branch": "main",
            "template_name": "default",
            "extra_addons": {},
            "odoo_image": "odoo:18.0",
        }
    ]
    env_info.return_value = {"env_vars": {"LOG_LEVEL": "info"}}

    plan = build_plan(settings, team, manifest, path)

    assert plan.actions == ()


def test_apply_preflights_conflicts_before_mutation(stack_fixture):
    settings, team, manifest, path = stack_fixture
    conflict = StackPlan("acme", (PlanAction("conflict", "environment", "owned"),))

    with (
        patch("oduflow.stack_ops.build_plan", return_value=conflict),
        patch("oduflow.stack_ops.volume_ops.create_volume") as create_volume,
    ):
        with pytest.raises(ConflictError, match="apply refused"):
            apply_stack(settings, team, manifest, path)

    create_volume.assert_not_called()


def test_apply_creates_resources_and_persists_non_secret_state(stack_fixture):
    settings, team, manifest, path = stack_fixture
    actions = StackPlan(
        "acme",
        (
            PlanAction("create", "extraRepositories.enterprise"),
            PlanAction("create", "volumes.fs-data"),
            PlanAction("create", "environment"),
            PlanAction("write", "files.fs-data:config/fs.conf"),
            PlanAction("create", "services.fs"),
            PlanAction("install", "modules", "acme_base"),
        ),
    )
    events = []

    with (
        patch("oduflow.stack_ops.build_plan", return_value=actions),
        patch(
            "oduflow.extra_addons.clone_extra_repo",
            side_effect=lambda *a, **k: events.append("repo"),
        ),
        patch(
            "oduflow.stack_ops.volume_ops.create_volume",
            side_effect=lambda *a, **k: events.append("volume"),
        ),
        patch(
            "oduflow.stack_ops.env_ops.create_environment",
            side_effect=lambda *a, **k: events.append("environment"),
        ),
        patch(
            "oduflow.stack_ops.volume_file_ops.write_file_in_volume",
            side_effect=lambda *a, **k: events.append("file"),
        ),
        patch(
            "oduflow.stack_ops.env_ops.get_env_base_url",
            return_value=("http://odoo", "localhost"),
        ),
        patch(
            "oduflow.stack_ops.service_ops.create_service",
            side_effect=lambda *a, **k: events.append("service"),
        ) as create_service,
        patch(
            "oduflow.stack_ops.odoo_ops.install_odoo_modules",
            side_effect=lambda *a, **k: events.append("modules") or {"exit_code": 0},
        ),
        patch(
            "oduflow.stack_ops.env_ops.restart_environment",
            side_effect=lambda *a, **k: events.append("restart"),
        ),
    ):
        result = apply_stack(settings, team, manifest, path)

    assert result == actions
    assert events == [
        "repo",
        "volume",
        "environment",
        "file",
        "service",
        "modules",
        "restart",
    ]
    assert create_service.call_args.kwargs["env_vars"] == {"ODOO_URL": "http://odoo"}
    state = Path(team.data_dir) / "stacks/acme.json"
    assert state.is_file()
    assert "http://odoo" not in state.read_text(encoding="utf-8")


def test_apply_no_changes_is_idempotent(stack_fixture):
    settings, team, manifest, path = stack_fixture
    empty = StackPlan("acme", ())

    with (
        patch("oduflow.stack_ops.build_plan", return_value=empty),
        patch("oduflow.stack_ops.env_ops.create_environment") as create_environment,
    ):
        result = apply_stack(settings, team, manifest, path)

    assert result == empty
    assert (Path(team.data_dir) / "stacks/acme.json").is_file()
    create_environment.assert_not_called()


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
def test_plan_reports_route_mode_conflict_before_apply(
    _services, _envs, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    manifest.spec.services["fs"].port = None
    manifest.spec.services["fs"].routes = [ServiceRoute(path="/events", port=8080)]

    plan = build_plan(settings, team, manifest, path)

    assert (
        PlanAction(
            "conflict",
            "services.fs",
            "HTTP routes require routing.mode = 'traefik'",
        )
        in plan.actions
    )
