import hashlib
import hmac
import json
from unittest.mock import Mock, patch

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
        manager = Mock()
        manager.submit.return_value = {
            "operation_id": "op-1",
            "state": "queued",
        }
        with patch.object(webhooks, "get_operation_manager", return_value=manager):
            status, body = webhooks.handle_github_event(
                settings,
                LockManager(),
                event="push",
                body=push,
                signature_header=_sign(secret, push),
            )
        assert status == 202
        assert body["queued"] == ["erp"]
        manager.submit.assert_called_once()

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
        manager = Mock()
        manager.submit.side_effect = [
            {"operation_id": "op-1", "state": "queued"},
            {"operation_id": "op-1", "state": "queued", "coalesced": True},
        ]
        with patch.object(webhooks, "get_operation_manager", return_value=manager):
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


class TestDispatchPush:
    def test_nothing_matching_queues_nothing(self, settings, team):
        manager = Mock()
        with patch.object(webhooks, "get_operation_manager", return_value=manager):
            queued = webhooks.dispatch_push(
                settings, team, LockManager(), {"ref": "refs/tags/v1"}
            )

        assert queued == []
        manager.submit.assert_not_called()


class TestSignatureScheme:
    def test_a_correct_digest_under_the_wrong_scheme_is_rejected(self):
        # The scheme is not decoration: accepting any prefix would let a
        # caller present a valid sha256 digest labelled as something else.
        body = b'{"ref":"refs/heads/main"}'
        digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

        assert webhooks.verify_signature("s3cret", body, f"md5={digest}") is False
        assert webhooks.verify_signature("s3cret", body, f"sha1={digest}") is False
        assert webhooks.verify_signature("s3cret", body, digest) is False

    def test_the_scheme_is_matched_case_insensitively(self):
        body = b"x"
        digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

        assert webhooks.verify_signature("s3cret", body, f"SHA256={digest}") is True

    def test_an_empty_digest_is_rejected(self):
        assert webhooks.verify_signature("s3cret", b"x", "sha256=") is False

    def test_an_uppercase_digest_still_matches(self):
        body = b"x"
        digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

        assert (
            webhooks.verify_signature("s3cret", body, f"sha256={digest.upper()}")
            is True
        )

    def test_a_different_body_does_not_match(self):
        digest = hmac.new(b"s3cret", b"one", hashlib.sha256).hexdigest()

        assert webhooks.verify_signature("s3cret", b"two", f"sha256={digest}") is False


class TestMalformedPayload:
    @pytest.fixture
    def secret(self, team):
        # The team's webhook secret is generated with its first production.
        production_registry.create_production(
            team, "erp", {"repo_url": "https://github.com/org/erp.git"}
        )
        return production_registry.get_webhook_secret(team)

    def test_unparsable_json_is_a_400_not_a_401(self, settings, team, secret):
        # 401 would tell the caller their signature was wrong when it was
        # actually fine — the request is authenticated, the body is not JSON.
        body = b"{not json"

        status, payload = webhooks.handle_github_event(
            settings,
            LockManager(),
            event="push",
            body=body,
            signature_header=_sign(secret, body),
        )

        assert status == 400
        assert payload["error"] == "malformed payload"

    def test_undecodable_bytes_are_also_a_400(self, settings, team, secret):
        body = b"\xff\xfe not utf-8"

        status, _payload = webhooks.handle_github_event(
            settings,
            LockManager(),
            event="push",
            body=body,
            signature_header=_sign(secret, body),
        )

        assert status == 400
