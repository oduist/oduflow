import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from oduflow.stack_loader import (
    StackValidationError,
    load_stack,
    read_stack_file,
)
from oduflow.stack_models import Service, ServiceRoute, StackManifest

VALID = """\
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
        ESL_PASSWORD:
          fromEnv: FS_ESL_PASSWORD
      volumes:
        - source: fs-data
          target: /data
"""


def _write(tmp_path: Path, content: str = VALID) -> Path:
    path = tmp_path / "oduflow.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_valid_stack(tmp_path):
    manifest = load_stack(_write(tmp_path))

    assert manifest.metadata.name == "acme"
    assert manifest.spec.environment.odoo_image == "odoo:18.0"
    assert manifest.spec.services["fs"].env["ODOO_URL"].environment_field == "url"


def test_duplicate_yaml_key_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        VALID.replace("  name: acme\n", "  name: acme\n  name: duplicate\n", 1),
    )

    with pytest.raises(StackValidationError, match="duplicate YAML key 'name'"):
        load_stack(path)


def test_unknown_fields_are_rejected(tmp_path):
    path = _write(tmp_path, VALID.replace("spec:\n", "spec:\n  unsupported: true\n"))

    with pytest.raises(StackValidationError, match="unsupported"):
        load_stack(path)


def test_undeclared_volume_reference_is_rejected(tmp_path):
    path = _write(tmp_path, VALID.replace("volume: fs-data", "volume: missing"))

    with pytest.raises(StackValidationError, match="undeclared volume 'missing'"):
        load_stack(path)


def test_environment_cannot_reference_its_own_token(tmp_path):
    path = _write(
        tmp_path,
        VALID.replace(
            "LOG_LEVEL: info",
            "LOG_LEVEL:\n        environmentField: token",
        ),
    )

    with pytest.raises(StackValidationError, match="cannot reference environmentField"):
        load_stack(path)


def test_stack_file_must_stay_inside_manifest_directory(tmp_path):
    path = _write(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(StackValidationError, match="escapes the stack directory"):
        read_stack_file(path, "../outside.txt")


def test_service_route_paths_are_canonicalized_before_duplicate_validation():
    route = ServiceRoute(path="/api/", port=8080)

    assert route.path == "/api"
    with pytest.raises(ValidationError, match="duplicate paths"):
        Service(
            image="example/service:1",
            routes=[
                ServiceRoute(path="/api", port=8080),
                ServiceRoute(path="/api/", port=8081),
            ],
        )


def test_shipped_json_schema_matches_models():
    schema_path = (
        Path(__file__).parents[1] / "src/oduflow/schemas/oduflow-stack-v1alpha1.json"
    )
    shipped = json.loads(schema_path.read_text(encoding="utf-8"))

    assert shipped == StackManifest.model_json_schema(by_alias=True)
