import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

from oduflow import production_registry, webhooks
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def team(tmp_path):
    data_dir = tmp_path / "team_1"
    data_dir.mkdir()
    return TeamSettings(team_id="1", data_dir=str(data_dir))


@pytest.fixture
def settings(team, tmp_path):
    return Settings(base_data_dir=str(tmp_path), prod_enabled=True, teams={"1": team})


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push_body(repo_url: str, branch: str) -> bytes:
    return json.dumps(
        {
            "ref": f"refs/heads/{branch}",
            "repository": {"clone_url": repo_url, "html_url": repo_url[:-4]},
        }
    ).encode()


class TestNormalizeRepoUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/Org/Repo.git",
            "https://github.com/Org/Repo",
            "https://user:token@github.com/Org/Repo.git",
            "git@github.com:Org/Repo.git",
            "HTTPS://GITHUB.COM/Org/Repo.git",
        ],
    )
    def test_equivalent_forms(self, url):
        assert webhooks.normalize_repo_url(url) == "github.com/Org/Repo"

    def test_empty(self):
        assert webhooks.normalize_repo_url("") == ""


class TestSignature:
    def test_valid(self):
        body = b"payload"
        assert webhooks.verify_signature("s3cret", body, _sign("s3cret", body))

    def test_invalid_secret(self):
        body = b"payload"
        assert not webhooks.verify_signature("other", body, _sign("s3cret", body))

    def test_missing_or_malformed_header(self):
        assert not webhooks.verify_signature("s", b"x", "")
        assert not webhooks.verify_signature("s", b"x", "sha1=abc")
        assert not webhooks.verify_signature("s", b"x", "garbage")

    def test_no_secret_rejects(self):
        assert not webhooks.verify_signature("", b"x", _sign("", b"x"))


class TestMatchProductions:
    def _prod(self, team, name, **overrides):
        record = {
            "domain": f"{name}.x.com",
            "repo_url": "https://github.com/org/erp.git",
            "branch": "production",
            "auto_update": True,
        }
        record.update(overrides)
        production_registry.create_production(team, name, record)

    def test_matches_auto_update_repo_and_branch(self, team):
        self._prod(team, "erp")
        payload = json.loads(_push_body("https://github.com/org/erp.git", "production"))
        assert webhooks.match_productions(team, payload) == ["erp"]

    def test_auto_update_disabled_not_matched(self, team):
        self._prod(team, "erp", auto_update=False)
        payload = json.loads(_push_body("https://github.com/org/erp.git", "production"))
        assert webhooks.match_productions(team, payload) == []

    def test_other_branch_not_matched(self, team):
        self._prod(team, "erp")
        payload = json.loads(_push_body("https://github.com/org/erp.git", "dev"))
        assert webhooks.match_productions(team, payload) == []

    def test_other_repo_not_matched(self, team):
        self._prod(team, "erp")
        payload = json.loads(
            _push_body("https://github.com/org/other.git", "production")
        )
        assert webhooks.match_productions(team, payload) == []

    def test_ssh_url_in_payload_matches(self, team):
        self._prod(team, "erp")
        payload = {
            "ref": "refs/heads/production",
            "repository": {"ssh_url": "git@github.com:org/erp.git"},
        }
        assert webhooks.match_productions(team, payload) == ["erp"]

    def test_tag_push_ignored(self, team):
        self._prod(team, "erp")
        payload = {"ref": "refs/tags/v1.0", "repository": {"clone_url": "x"}}
        assert webhooks.match_productions(team, payload) == []


class TestHandleGithubEvent:
    def test_disabled_production_returns_404(self, team, tmp_path):
        settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
        with patch.object(webhooks, "resolve_team") as resolve:
            status, body = webhooks.handle_github_event(
                settings,
                LockManager(),
                event="push",
                body=b"{}",
                signature_header="sha256=deadbeef",
            )
        assert status == 404
        assert body == {"ok": False, "error": "production hosting disabled"}
        resolve.assert_not_called()

    def test_bad_signature_401(self, settings, team):
        production_registry.create_production(team, "erp", {})
        status, body = webhooks.handle_github_event(
            settings,
            LockManager(),
            event="push",
            body=b"{}",
            signature_header="sha256=deadbeef",
        )
        assert status == 401

    def test_ping_pongs(self, settings, team):
        production_registry.create_production(team, "erp", {})
        secret = production_registry.get_webhook_secret(team)
        status, body = webhooks.handle_github_event(
            settings,
            LockManager(),
            event="ping",
            body=b"{}",
            signature_header=_sign(secret, b"{}"),
        )
        assert status == 200
        assert body["pong"] is True

    def test_push_queues_matching_production(self, settings, team):
        production_registry.create_production(
            team,
            "erp",
            {
                "repo_url": "https://github.com/org/erp.git",
                "branch": "production",
                "auto_update": True,
            },
        )
        secret = production_registry.get_webhook_secret(team)
        push = _push_body("https://github.com/org/erp.git", "production")
        with patch.object(webhooks, "_deploy_in_background"):
            status, body = webhooks.handle_github_event(
                settings,
                LockManager(),
                event="push",
                body=push,
                signature_header=_sign(secret, push),
            )
            # The background thread may not have run yet; queued is enough.
        assert status == 202
        assert body["queued"] == ["erp"]
        # Coalescing state cleanup for other tests.
        webhooks._pending.clear()

    def test_push_coalesces_rapid_events(self, settings, team):
        production_registry.create_production(
            team,
            "erp",
            {
                "repo_url": "https://github.com/org/erp.git",
                "branch": "production",
                "auto_update": True,
            },
        )
        secret = production_registry.get_webhook_secret(team)
        push = _push_body("https://github.com/org/erp.git", "production")
        locks = LockManager()
        with patch.object(webhooks, "_deploy_in_background"):
            _status, body1 = webhooks.handle_github_event(
                settings,
                locks,
                event="push",
                body=push,
                signature_header=_sign(secret, push),
            )
            _status, body2 = webhooks.handle_github_event(
                settings,
                locks,
                event="push",
                body=push,
                signature_header=_sign(secret, push),
            )
        webhooks._pending.clear()
        assert body1["queued"] == ["erp"]
        assert body2["queued"] == []  # coalesced

    def test_non_push_event_ignored(self, settings, team):
        production_registry.create_production(team, "erp", {})
        secret = production_registry.get_webhook_secret(team)
        status, body = webhooks.handle_github_event(
            settings,
            LockManager(),
            event="issues",
            body=b"{}",
            signature_header=_sign(secret, b"{}"),
        )
        assert status == 200
        assert body["ignored"] == "issues"
