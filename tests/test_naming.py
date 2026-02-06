from flow.naming import slugify_branch, get_db_name, get_resource_name, get_workspace_path, get_repo_path, get_filestore_paths


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
        assert get_db_name("main") == "flow_main"

    def test_feature(self):
        assert get_db_name("feature/payments") == "flow_feature-payments"

    def test_complex(self):
        assert get_db_name("hotfix/CRM-123/fix") == "flow_hotfix-crm-123-fix"


class TestGetResourceName:
    def test_odoo(self):
        assert get_resource_name("main", "odoo") == "flow-main-odoo"

    def test_slash_branch(self):
        assert get_resource_name("feature/payments", "odoo") == "flow-feature-payments-odoo"

    def test_custom_prefix(self):
        assert get_resource_name("main", "odoo", prefix="test-") == "test-main-odoo"


class TestGetWorkspacePath:
    def test_simple(self):
        assert get_workspace_path("main", "/tmp/ws") == "/tmp/ws/main"

    def test_slash(self):
        assert get_workspace_path("feature/payments", "/tmp/ws") == "/tmp/ws/feature-payments"


class TestGetRepoPath:
    def test_simple(self):
        assert get_repo_path("main", "/tmp/ws") == "/tmp/ws/main/repo"

    def test_slash(self):
        assert get_repo_path("feature/payments", "/tmp/ws") == "/tmp/ws/feature-payments/repo"


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
