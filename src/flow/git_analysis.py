import ast
import logging
import os
import re
import subprocess

logger = logging.getLogger("flow")

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


def _check_field_changes(py_rel_path: str, repo_path: str) -> bool:
    """Return True if any ``fields.`` definition was added, removed or modified."""
    abs_path = os.path.join(repo_path, py_rel_path)

    try:
        new_source = open(abs_path).read() if os.path.isfile(abs_path) else ""
    except Exception:
        new_source = ""

    try:
        old_source = subprocess.run(
            ["git", "-C", repo_path, "show", f"HEAD~1:{py_rel_path}"],
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
        return True
    return False


def _is_security_path(file_path: str) -> bool:
    return "/security/" in f"/{file_path}/" or file_path.startswith("security/")


def classify_changes(changed_files: list[str], repo_path: str) -> dict:
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
    if not changed_files:
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
            manifest_action = _check_manifest_changes(f, module, repo_path)
            if manifest_action == "install":
                modules_to_install.add(module)
            elif manifest_action == "upgrade":
                modules_to_upgrade.add(module)
            continue

        if ext == ".py":
            py_changed = True
            if module and module not in modules_to_install:
                if _check_field_changes(f, repo_path):
                    modules_to_upgrade.add(module)
            continue

        if ext == ".xml":
            if _is_security_path(f) and module:
                xml_security.append(f)
                if module not in modules_to_install:
                    modules_to_upgrade.add(module)
            else:
                xml_hot.append(f)
            continue

        if ext == ".js":
            js_changed.append(f)
            continue

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
        return {
            "action": "install" if modules_to_install else "upgrade",
            "modules_to_install": sorted(modules_to_install),
            "modules_to_upgrade": sorted(modules_to_upgrade),
            "details": details,
        }

    if py_changed:
        return {
            "action": "restart",
            "modules_to_upgrade": [],
            "modules_to_install": [],
            "details": details,
        }

    return {
        "action": "refresh",
        "modules_to_upgrade": [],
        "modules_to_install": [],
        "details": details,
    }


def _check_manifest_changes(
    manifest_rel_path: str, module: str, repo_path: str
) -> str | None:
    """
    Check if __manifest__.py changes require a module install or upgrade.
    Uses git to compare old vs new manifest content.
    Returns ``"install"`` for a new module, ``"upgrade"`` for significant
    changes to an existing module, or ``None`` if no action is needed.
    """
    manifest_abs = os.path.join(repo_path, manifest_rel_path)
    if not os.path.isfile(manifest_abs):
        return None

    try:
        new_manifest = _parse_manifest(manifest_abs)
    except Exception:
        logger.warning("Cannot parse new manifest for %s", module)
        return "upgrade"

    try:
        old_content = subprocess.run(
            ["git", "-C", repo_path, "show", f"HEAD~1:{manifest_rel_path}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        old_manifest = ast.literal_eval(old_content)
    except Exception:
        logger.info("No previous manifest for %s, new module detected", module)
        return "install"

    if old_manifest.get("version") != new_manifest.get("version"):
        logger.info("Module %s: version changed", module)
        return "upgrade"

    for key in MANIFEST_KEYS_WITH_FILES:
        old_val = old_manifest.get(key, [])
        new_val = new_manifest.get(key, [])
        if old_val != new_val:
            logger.info("Module %s: '%s' list changed", module, key)
            return "upgrade"

    return None
