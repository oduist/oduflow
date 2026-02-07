import ast
import logging
import os

logger = logging.getLogger("flow")

MANIFEST_KEYS_WITH_FILES = ("data", "demo", "assets", "qweb")


def _parse_manifest(path: str) -> dict:
    with open(path, "r") as f:
        return ast.literal_eval(f.read())


def _get_module_name(file_path: str) -> str | None:
    """Extract Odoo module name from a relative file path (first directory component)."""
    parts = file_path.split("/")
    if len(parts) < 2:
        return None
    return parts[0]


def _is_security_path(file_path: str) -> bool:
    return "/security/" in f"/{file_path}/" or file_path.startswith("security/")


def classify_changes(changed_files: list[str], repo_path: str) -> dict:
    """
    Classify changed files and determine required Odoo actions.

    Returns:
        {
            "action": "none" | "refresh" | "restart" | "upgrade",
            "modules_to_upgrade": [...],
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
        return {"action": "none", "modules_to_upgrade": [], "details": {}}

    py_changed = False
    xml_hot = []
    xml_security = []
    js_changed = []
    modules_to_upgrade: set[str] = set()

    for f in changed_files:
        ext = os.path.splitext(f)[1].lower()
        module = _get_module_name(f)

        if os.path.basename(f) == "__manifest__.py" and module:
            upgrade_needed = _check_manifest_changes(f, module, repo_path)
            if upgrade_needed:
                modules_to_upgrade.add(module)
            continue

        if ext == ".py":
            py_changed = True
            continue

        if ext == ".xml":
            if _is_security_path(f) and module:
                xml_security.append(f)
                modules_to_upgrade.add(module)
            else:
                xml_hot.append(f)
            continue

        if ext == ".js":
            js_changed.append(f)
            continue

    details = {
        "py_changed": py_changed,
        "xml_hot": xml_hot,
        "xml_security": xml_security,
        "manifest_upgrade": sorted(modules_to_upgrade),
        "js_changed": js_changed,
    }

    if modules_to_upgrade:
        return {
            "action": "upgrade",
            "modules_to_upgrade": sorted(modules_to_upgrade),
            "details": details,
        }

    if py_changed:
        return {
            "action": "restart",
            "modules_to_upgrade": [],
            "details": details,
        }

    return {
        "action": "refresh",
        "modules_to_upgrade": [],
        "details": details,
    }


def _check_manifest_changes(
    manifest_rel_path: str, module: str, repo_path: str
) -> bool:
    """
    Check if __manifest__.py changes require a module upgrade.
    Uses git to compare old vs new manifest content.
    Returns True if upgrade is needed.
    """
    import subprocess

    manifest_abs = os.path.join(repo_path, manifest_rel_path)
    if not os.path.isfile(manifest_abs):
        return False

    try:
        new_manifest = _parse_manifest(manifest_abs)
    except Exception:
        logger.warning("Cannot parse new manifest for %s", module)
        return True

    try:
        old_content = subprocess.run(
            ["git", "-C", repo_path, "show", f"HEAD~1:{manifest_rel_path}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        old_manifest = ast.literal_eval(old_content)
    except Exception:
        logger.warning("Cannot parse old manifest for %s, assuming upgrade needed", module)
        return True

    if old_manifest.get("version") != new_manifest.get("version"):
        logger.info("Module %s: version changed", module)
        return True

    for key in MANIFEST_KEYS_WITH_FILES:
        old_val = old_manifest.get(key, [])
        new_val = new_manifest.get(key, [])
        if old_val != new_val:
            logger.info("Module %s: '%s' list changed", module, key)
            return True

    return False
