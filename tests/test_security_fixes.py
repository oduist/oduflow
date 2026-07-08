"""Regression tests for the security-audit fixes.

Each test pins a specific vulnerability that was closed so a future refactor
cannot silently reopen it. All are pure/unit — no Docker required.
"""

import pytest

from oduflow.env_credentials import (
    MissingCredentialsError,
    create_credentials,
    load_credentials,
)
from oduflow.naming import validate_env_name


class TestValidateEnvName:
    @pytest.mark.parametrize(
        "name",
        ["19.0", "feature/my-feature", "release/v1.2.3", "client-a", "a.b_c-1"],
    )
    def test_accepts_real_branch_names(self, name):
        assert validate_env_name(name) == name

    @pytest.mark.parametrize("name", ["", "   ", ".", "..", "a/..", "../b", "a/./b"])
    def test_rejects_traversal_and_empty(self, name):
        with pytest.raises(ValueError):
            validate_env_name(name)

    @pytest.mark.parametrize("name", ["a\\b", "a\x00b", "x" * 101])
    def test_rejects_separators_nul_and_oversize(self, name):
        with pytest.raises(ValueError):
            validate_env_name(name)

    @pytest.mark.parametrize("name", ["foo ", " foo", "\tfoo", "foo\n", " 19.0 "])
    def test_rejects_whitespace_padding(self, name):
        # A padded name must not pass: slug strips the space (container/DB) while
        # the workspace path keeps it, so "foo" and "foo " would collide.
        with pytest.raises(ValueError):
            validate_env_name(name)

    def test_returns_name_unchanged(self):
        # No silent canonicalization — the returned value is byte-for-byte input.
        assert validate_env_name("feature/My-Env.1") == "feature/My-Env.1"


class TestAutofillUiPasswords:
    def test_fills_every_empty_password_distinctly(self):
        from oduflow.server import _autofill_ui_passwords

        src = (
            "[team.1]\n"
            'ui_password = ""                  # Web UI password\n'
            "[team.2]\n"
            'ui_password = ""\n'
        )
        out, generated = _autofill_ui_passwords(src)
        assert len(generated) == 2
        assert generated[0] != generated[1]
        assert 'ui_password = ""' not in out
        for pw in generated:
            assert f'ui_password = "{pw}"' in out
        # Untouched lines survive verbatim, including the preserved comment.
        assert "[team.1]" in out and "[team.2]" in out
        assert "# Web UI password" in out

    def test_noop_when_already_set(self):
        from oduflow.server import _autofill_ui_passwords

        src = 'ui_password = "already-set"\n'
        out, generated = _autofill_ui_passwords(src)
        assert generated == []
        assert out.strip() == src.strip()


class TestHttpRequestPathGuard:
    """`http_request_to_odoo` must reject paths that could rewrite the host
    (SSRF) before it ever builds a request."""

    def _call(self, path):
        from oduflow.docker_ops.odoo_ops import http_request_to_odoo

        # The guard raises before touching settings/team/Docker, so None is fine.
        return http_request_to_odoo(None, None, "env", path)

    @pytest.mark.parametrize(
        "path", ["@evil.com/x", "//evil.com/", "http://evil.com", "evil", ""]
    )
    def test_rejects_host_rewriting_paths(self, path):
        with pytest.raises(ValueError):
            self._call(path)


class TestModuleNameValidation:
    def test_accepts_plain_modules(self):
        from oduflow.docker_ops.odoo_ops import _validate_module_names

        _validate_module_names(["sale", "purchase", "stock_account"])

    @pytest.mark.parametrize(
        "mod",
        ["base --load=evil", "a;b", "mod space", "--logfile=/x", "a,b", ""],
    )
    def test_rejects_argument_injection(self, mod):
        from oduflow.docker_ops.odoo_ops import _validate_module_names

        with pytest.raises(ValueError):
            _validate_module_names([mod])


class TestCredentialFallback:
    def test_fallback_returns_shared_when_allowed(self, tmp_path):
        creds = load_credentials(
            "env", str(tmp_path), "odoo", "secret", allow_fallback=True
        )
        assert creds == {"pg_user": "odoo", "pg_password": "secret"}

    def test_no_fallback_raises_when_missing(self, tmp_path):
        with pytest.raises(MissingCredentialsError):
            load_credentials(
                "env", str(tmp_path), "odoo", "secret", allow_fallback=False
            )

    def test_no_fallback_uses_scoped_creds_when_present(self, tmp_path):
        create_credentials("env", "1", str(tmp_path))
        creds = load_credentials(
            "env", str(tmp_path), "odoo", "secret", allow_fallback=False
        )
        assert creds["pg_user"] != "odoo"
        assert creds["pg_password"] != "secret"


class TestSetupRepoAuthSSRF:
    def test_blocks_loopback_before_storing_credentials(self, tmp_path):
        from oduflow.git_ops import setup_repo_auth
        from oduflow.url_safety import BlockedURLError

        cred_file = str(tmp_path / "creds")
        with pytest.raises(BlockedURLError):
            setup_repo_auth("https://user:pat@127.0.0.1/owner/repo.git", cred_file)
        # The PAT must never have been written to disk.
        import os

        assert not os.path.exists(cred_file)


class TestUiPasswordInjection:
    def test_injects_into_bootstrap_config(self):
        from oduflow.server import _inject_ui_password

        src = 'ui_password = ""                  # Web UI password\n'
        out = _inject_ui_password(src, "s3cret")
        assert 'ui_password = "s3cret"' in out
        assert 'ui_password = ""' not in out

    def test_only_first_empty_is_set(self):
        from oduflow.server import _inject_ui_password

        src = 'ui_password = ""\nui_password = ""\n'
        out = _inject_ui_password(src, "s3cret")
        assert out.count('ui_password = "s3cret"') == 1
        assert out.count('ui_password = ""') == 1
