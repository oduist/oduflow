import pytest

from oduflow.naming import (
    get_db_name,
    get_filestore_paths,
    get_repo_path,
    get_resource_name,
    get_service_container_name,
    get_template_db_name,
    get_workspace_path,
    slugify_branch,
    validate_service_name,
    validate_template_name,
)


class TestSlugifyBranch:
    def test_simple(self):
        assert slugify_branch("main") == "main"

    def test_slash(self):
        assert slugify_branch("feature/payments") == "feature-payments"

    def test_complex(self):
        assert slugify_branch("hotfix/CRM-123/fix") == "hotfix-crm-123-fix"

    def test_special_chars(self):
        assert slugify_branch("feat/hello@world!") == "feat-helloworld"

    def test_truncation(self):
        long_name = "a" * 100
        assert len(slugify_branch(long_name)) == 63

    def test_uppercase(self):
        assert slugify_branch("Feature/BRANCH") == "feature-branch"

    def test_underscores_preserved(self):
        assert slugify_branch("fix_bug") == "fix_bug"

    def test_dots_removed(self):
        assert slugify_branch("release/1.2.3") == "release-123"

    def test_empty_string(self):
        assert slugify_branch("") == ""


class TestGetDbName:
    def test_main(self):
        assert get_db_name("main") == "oduflow_1_main"

    def test_feature(self):
        assert get_db_name("feature/payments") == "oduflow_1_feature-payments"

    def test_complex(self):
        assert get_db_name("hotfix/CRM-123/fix") == "oduflow_1_hotfix-crm-123-fix"

    def test_with_team_id(self):
        assert get_db_name("main", team_id="2") == "oduflow_2_main"

    def test_feature_with_team_id(self):
        assert (
            get_db_name("feature/payments", team_id="3") == "oduflow_3_feature-payments"
        )


class TestGetResourceName:
    def test_odoo(self):
        assert (
            get_resource_name("main", "odoo", "oduflow-", "1") == "oduflow-1-main-odoo"
        )

    def test_slash_branch(self):
        assert (
            get_resource_name("feature/payments", "odoo", "oduflow-", "1")
            == "oduflow-1-feature-payments-odoo"
        )

    def test_custom_prefix(self):
        assert get_resource_name("main", "odoo", "test-", "2") == "test-2-main-odoo"


class TestServiceNaming:
    @pytest.mark.parametrize(
        "good",
        ["redis", "Odoo-MCP-server", "service_1", "service.v2", "1-service"],
    )
    def test_accepts_docker_safe_names(self, good):
        assert validate_service_name(good) == good
        assert (
            get_service_container_name(good, "oduflow-", "1")
            == f"oduflow-1-svc-{good}"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "Odoo MCP server",
            "-service",
            "_service",
            ".service",
            "service/name",
            "service:name",
            "service@host",
            "service\n",
        ],
    )
    def test_rejects_names_docker_would_reject(self, bad):
        with pytest.raises(ValueError, match="Invalid service name"):
            validate_service_name(bad)
        with pytest.raises(ValueError, match="Invalid service name"):
            get_service_container_name(bad, "oduflow-", "1")

    def test_error_explains_how_to_replace_spaces(self):
        with pytest.raises(ValueError) as exc_info:
            get_service_container_name("Odoo MCP server", "oduflow-", "1")

        assert str(exc_info.value) == (
            "Invalid service name 'Odoo MCP server': must start with a letter "
            "or digit and contain only letters, digits, dots, hyphens, and "
            "underscores. Spaces are not allowed; use hyphens instead (for "
            "example, 'odoo-mcp-server')."
        )


class TestGetWorkspacePath:
    def test_simple(self):
        assert get_workspace_path("main", "/tmp/ws") == "/tmp/ws/main"

    def test_slash(self):
        assert (
            get_workspace_path("feature/payments", "/tmp/ws")
            == "/tmp/ws/feature-payments"
        )


class TestGetRepoPath:
    def test_simple(self):
        assert get_repo_path("main", "/tmp/ws") == "/tmp/ws/main/repo"

    def test_slash(self):
        assert (
            get_repo_path("feature/payments", "/tmp/ws")
            == "/tmp/ws/feature-payments/repo"
        )


class TestGetFilestorePaths:
    def test_keys(self):
        paths = get_filestore_paths("main", "/tmp/ws")
        assert set(paths.keys()) == {"upper", "work", "merged"}

    def test_paths(self):
        paths = get_filestore_paths("main", "/tmp/ws")
        assert paths["upper"] == "/tmp/ws/main/filestore_upper"
        assert paths["work"] == "/tmp/ws/main/filestore_work"
        assert paths["merged"] == "/tmp/ws/main/filestore"

    def test_slash_branch(self):
        paths = get_filestore_paths("feature/payments", "/tmp/ws")
        assert paths["merged"] == "/tmp/ws/feature-payments/filestore"


class TestGetTemplateDbName:
    def test_named(self):
        assert get_template_db_name("prod") == "oduflow_template_1_prod"

    def test_custom_name(self):
        assert (
            get_template_db_name("myproject-v17") == "oduflow_template_1_myproject-v17"
        )

    def test_slash(self):
        assert get_template_db_name("client/prod") == "oduflow_template_1_client-prod"

    def test_with_team_id(self):
        assert get_template_db_name("prod", team_id="2") == "oduflow_template_2_prod"

    def test_named_with_team_id(self):
        assert (
            get_template_db_name("myproject-v17", team_id="3")
            == "oduflow_template_3_myproject-v17"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "../etc",
            "client/../prod",
            "/abs",
            "client/",  # trailing slash -> empty segment
            "..",
            ".",
            ".hidden",
            "a b",  # whitespace
            "a;b",
            "a'b",
            'a"b',
            "a`b",
            "x'; DROP DATABASE oduflow_1_main; --",
            "",
            "a" * 64,  # too long
        ],
    )
    def test_rejects_dangerous_names(self, bad):
        # Path-traversal / SQL-identifier break-out must be refused (issue #38).
        with pytest.raises(ValueError, match="Invalid template name"):
            get_template_db_name(bad)
        with pytest.raises(ValueError, match="Invalid template name"):
            validate_template_name(bad)

    @pytest.mark.parametrize(
        "good", ["prod", "client/prod", "myproject-v17", "v17.0", "a", "A_b-c.d"]
    )
    def test_accepts_valid_names(self, good):
        assert validate_template_name(good) == good


class TestProdNaming:
    @pytest.mark.parametrize("good", ["erp", "erp-main", "a", "x1", "client2-erp"])
    def test_accepts_valid_prod_names(self, good):
        from oduflow.naming import prod_env_name, validate_prod_name

        assert validate_prod_name(good) == good
        assert prod_env_name(good) == f"prod-{good}"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "ERP",  # uppercase
            "-erp",  # leading dash
            "erp/main",  # slash
            "erp_main",  # underscore
            "a b",
            "a" * 32,  # too long
            "erp;drop",
            "../etc",
        ],
    )
    def test_rejects_bad_prod_names(self, bad):
        from oduflow.naming import validate_prod_name

        with pytest.raises(ValueError, match="Invalid production name"):
            validate_prod_name(bad)

    def test_prod_env_name_feeds_existing_helpers(self):
        # The derived internal name flows through the shared naming chain
        # unchanged: dashes survive slugification, so DB/container names stay
        # aligned with the prod-{name} namespace.
        from oduflow.naming import prod_env_name

        env = prod_env_name("erp")
        assert get_db_name(env, "2") == "oduflow_2_prod-erp"
        assert get_resource_name(env, "odoo", "oduflow-", "2") == (
            "oduflow-2-prod-erp-odoo"
        )
        assert get_workspace_path(env, "/data/ws") == "/data/ws/prod-erp"
