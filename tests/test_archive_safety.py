"""Regression tests for archive extraction path-traversal guard (issue #43)."""

import os

from oduflow.docker_ops.system_ops import _is_within_directory


def test_within_directory_accepts_inside_paths(tmp_path):
    root = str(tmp_path)
    assert _is_within_directory(root, root)
    assert _is_within_directory(root, os.path.join(root, "a"))
    assert _is_within_directory(root, os.path.join(root, "a/b/c.dat"))


def test_within_directory_rejects_traversal(tmp_path):
    root = str(tmp_path / "filestore")
    os.makedirs(root)
    # Classic zip-slip members.
    assert not _is_within_directory(root, os.path.join(root, "../../etc/passwd"))
    assert not _is_within_directory(root, os.path.join(root, "../sibling"))
    assert not _is_within_directory(root, "/etc/passwd")
    # A sibling dir that merely shares a name prefix must not count as inside.
    assert not _is_within_directory(root, str(tmp_path / "filestore-evil"))
