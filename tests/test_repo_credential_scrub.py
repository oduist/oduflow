"""Inline git credentials must not land in the Docker ``oduflow.repo`` label.

``create_environment`` moves any ``https://user:PAT@host/...`` credentials into
the team credential store and persists only a sanitized URL, because labels are
world-readable via ``docker inspect`` and copied verbatim into saved template
metadata.
"""

from __future__ import annotations

import os

from oduflow.git_ops import extract_and_store_inline_credentials


def test_inline_creds_moved_to_store_and_url_sanitized(tmp_path):
    cred = str(tmp_path / "sub" / ".git-credentials")
    url, user = extract_and_store_inline_credentials(
        "https://alice:ghp_SECRET123@github.com/o/r.git", cred
    )
    assert url == "https://github.com/o/r.git"  # no credentials in the label URL
    assert user == "alice"  # non-secret account name, safe for the label
    stored = open(cred).read()
    assert "ghp_SECRET123" in stored  # secret lives in the credential store
    assert oct(os.stat(cred).st_mode & 0o777) == "0o600"
    assert oct(os.stat(os.path.dirname(cred)).st_mode & 0o777) == "0o700"


def test_clean_url_is_untouched(tmp_path):
    cred = str(tmp_path / ".git-credentials")
    url, user = extract_and_store_inline_credentials(
        "https://github.com/o/r.git", cred
    )
    assert url == "https://github.com/o/r.git"
    assert user == ""
    assert not os.path.exists(cred)  # nothing stored for a credential-free URL


def test_token_as_username_kept_out_of_label(tmp_path):
    # https://<TOKEN>@host (token in the username slot, no password) must not
    # leak the token as the label's git_user; rely on host-based matching.
    cred = str(tmp_path / ".git-credentials")
    url, user = extract_and_store_inline_credentials(
        "https://ghp_TOKENONLY@github.com/o/r.git", cred
    )
    assert url == "https://github.com/o/r.git"
    assert user == ""
    assert "ghp_TOKENONLY" in open(cred).read()


def test_empty_url_noop(tmp_path):
    cred = str(tmp_path / ".git-credentials")
    url, user = extract_and_store_inline_credentials("", cred)
    assert url == ""
    assert user == ""
    assert not os.path.exists(cred)
