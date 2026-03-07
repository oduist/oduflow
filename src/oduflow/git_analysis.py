from __future__ import annotations

import ast
import logging
import os
import re
import subprocess

from oduflow import settings

logger = logging.getLogger("oduflow")


def _trace(msg: str, *args: object) -> None:
    if settings.TRACE:
        logger.info("[TRACE] " + msg, *args)


MANIFEST_KEYS_WITH_FILES = ("data", "demo", "assets", "qweb")

_FIELD_RE = re.compile(r"^\s*\w+\s*=\s*fields\..*", re.MULTILINE)


def _parse_manifest(path: str) -> dict:
    with open(path, "r") as f:
        return ast.literal_eval(f.read())


def _get_module_name(file_path: str, repo_path: str = "") -> str | None:
    """Extract Odoo module name from a relative file path.

    Walks up from the file's directory to find the nearest ancestor
    that contains a ``__manifest__.py``, which marks an Odoo module.
    Falls back to the first directory component when *repo_path* is
    not provided or no manifest is found.
    """
    parts = file_path.split("/")
    if len(parts) < 2:
        return None

    if repo_path:
        if os.path.basename(file_path) == "__manifest__.py":
            dir_parts = parts[:-1]
        else:
            dir_parts = parts[:-1]

        for i in range(len(dir_parts), 0, -1):
            candidate = "/".join(dir_parts[:i])
            manifest = os.path.join(repo_path, candidate, "__manifest__.py")
            if os.path.isfile(manifest):
                return dir_parts[i - 1]

    return parts[0]


def _extract_field_lines(source: str) -> set[str]:
    """Return the set of normalised field-definition lines from Python source."""
    lines = set()
    for match in _FIELD_RE.finditer(source):
        line = match.group(0)
        lines.add(re.sub(r"\s+", " ", line).strip())
    return lines


def _check_field_changes(
    py_rel_path: str, repo_path: str, base_ref: str = "HEAD~1"
) -> bool:
    """Return True if any ``fields.`` definition was added, removed or modified."""
    abs_path = os.path.join(repo_path, py_rel_path)

    try:
        new_source = open(abs_path).read() if os.path.isfile(abs_path) else ""
    except Exception:
        new_source = ""

    try:
        old_source = subprocess.run(
            ["git", "-C", repo_path, "show", f"{base_ref}:{py_rel_path}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except Exception:
        old_source = ""

    old_fields = _extract_field_lines(old_source)
    new_fields = _extract_field_lines(new_source)

    if old_fields != new_fields:
        diff = (new_fields - old_fields) | (old_fields - new_fields)
        logger.info("Field definitions changed in %s: %s", py_rel_path, diff)
        _trace("_check_field_changes(%s) -> CHANGED: %s", py_rel_path, diff)
        return True
    _trace("_check_field_changes(%s) -> no change", py_rel_path)
    return False


def _is_security_path(file_path: str) -> bool:
    return "/security/" in f"/{file_path}/" or file_path.startswith("security/")


def _is_data_path(file_path: str) -> bool:
    return "/data/" in f"/{file_path}/" or file_path.startswith("data/")


def classify_changes(
    changed_files: list[str], repo_path: str, base_ref: str = "HEAD~1"
) -> dict:
    """
    Classify changed files and determine required Odoo actions.

    Returns:
        {
            "action": "none" | "refresh" | "restart" | "upgrade" | "install",
            "modules_to_upgrade": [...],
            "modules_to_install": [...],
            "details": {
                "py_changed": bool,
                "xml_hot": [...],        # xml not in security/
                "xml_security": [...],   # xml in security/
                "manifest_upgrade": [...], # modules needing upgrade due to manifest
                "js_changed": [...],
            }
        }
    """
    _trace("classify_changes: %d files to classify", len(changed_files))

    if not changed_files:
        _trace("classify_changes: no changed files -> action=none")
        return {
            "action": "none",
            "modules_to_upgrade": [],
            "modules_to_install": [],
            "details": {},
        }

    py_changed = False
    xml_hot = []
    xml_security = []
    js_changed = []
    modules_to_upgrade: set[str] = set()
    modules_to_install: set[str] = set()

    for f in changed_files:
        ext = os.path.splitext(f)[1].lower()
        module = _get_module_name(f, repo_path)

        if os.path.basename(f) == "__manifest__.py" and module:
            manifest_action = _check_manifest_changes(f, module, repo_path, base_ref)
            _trace(
                "  file=%s ext=manifest module=%s -> manifest_action=%s",
                f,
                module,
                manifest_action,
            )
            if manifest_action == "install":
                modules_to_install.add(module)
            elif manifest_action == "upgrade":
                modules_to_upgrade.add(module)
            continue

        if ext == ".py":
            py_changed = True
            field_changed = False
            if module and module not in modules_to_install:
                field_changed = _check_field_changes(f, repo_path, base_ref)
                if field_changed:
                    modules_to_upgrade.add(module)
            _trace(
                "  file=%s ext=.py module=%s field_changed=%s", f, module, field_changed
            )
            continue

        if ext == ".xml":
            if (_is_security_path(f) or _is_data_path(f)) and module:
                xml_security.append(f)
                if module not in modules_to_install:
                    modules_to_upgrade.add(module)
                _trace(
                    "  file=%s ext=.xml module=%s -> security/data XML, UPGRADE",
                    f,
                    module,
                )
            else:
                xml_hot.append(f)
                _trace("  file=%s ext=.xml module=%s -> hot-reload XML", f, module)
            continue

        if ext == ".js":
            js_changed.append(f)
            _trace("  file=%s ext=.js module=%s -> hot-reload JS", f, module)
            continue

        _trace("  file=%s ext=%s module=%s -> ignored", f, ext, module)

    modules_to_upgrade -= modules_to_install

    details = {
        "py_changed": py_changed,
        "xml_hot": xml_hot,
        "xml_security": xml_security,
        "manifest_upgrade": sorted(modules_to_upgrade),
        "manifest_install": sorted(modules_to_install),
        "js_changed": js_changed,
    }

    if modules_to_install or modules_to_upgrade:
        action = "install" if modules_to_install else "upgrade"
        _trace(
            "classify_changes RESULT: action=%s install=%s upgrade=%s",
            action,
            sorted(modules_to_install),
            sorted(modules_to_upgrade),
        )
        return {
            "action": action,
            "modules_to_install": sorted(modules_to_install),
            "modules_to_upgrade": sorted(modules_to_upgrade),
            "details": details,
        }

    if py_changed:
        _trace(
            "classify_changes RESULT: action=restart (Python files changed, no field/manifest changes)"
        )
        return {
            "action": "restart",
            "modules_to_upgrade": [],
            "modules_to_install": [],
            "details": details,
        }

    _trace("classify_changes RESULT: action=refresh (only XML/JS hot-reload)")
    return {
        "action": "refresh",
        "modules_to_upgrade": [],
        "modules_to_install": [],
        "details": details,
    }


def _check_manifest_changes(
    manifest_rel_path: str, module: str, repo_path: str, base_ref: str = "HEAD~1"
) -> str | None:
    """
    Check if __manifest__.py changes require a module install or upgrade.
    Uses git to compare *base_ref* vs current manifest content.
    Returns ``"install"`` for a new module, ``"upgrade"`` for significant
    changes to an existing module, or ``None`` if no action is needed.
    """
    manifest_abs = os.path.join(repo_path, manifest_rel_path)
    if not os.path.isfile(manifest_abs):
        _trace("_check_manifest(%s) -> file missing, skip", module)
        return None

    try:
        new_manifest = _parse_manifest(manifest_abs)
    except Exception:
        logger.warning("Cannot parse new manifest for %s", module)
        _trace("_check_manifest(%s) -> parse error, assume upgrade", module)
        return "upgrade"

    try:
        old_content = subprocess.run(
            ["git", "-C", repo_path, "show", f"{base_ref}:{manifest_rel_path}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        old_manifest = ast.literal_eval(old_content)
    except Exception:
        logger.info("No previous manifest for %s, new module detected", module)
        _trace("_check_manifest(%s) -> new module, INSTALL", module)
        return "install"

    if old_manifest.get("version") != new_manifest.get("version"):
        logger.info("Module %s: version changed", module)
        _trace(
            "_check_manifest(%s) -> version changed %s -> %s, UPGRADE",
            module,
            old_manifest.get("version"),
            new_manifest.get("version"),
        )
        return "upgrade"

    for key in MANIFEST_KEYS_WITH_FILES:
        old_val = old_manifest.get(key, [])
        new_val = new_manifest.get(key, [])
        if old_val != new_val:
            logger.info("Module %s: '%s' list changed", module, key)
            _trace("_check_manifest(%s) -> '%s' changed, UPGRADE", module, key)
            return "upgrade"

    _trace("_check_manifest(%s) -> no significant changes", module)
    return None
