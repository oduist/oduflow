"""Persistent store for minted OAuth access/refresh tokens.

The module had no test file. It holds bearer secrets and decides whether a
request is authenticated, so the rules worth pinning are the asymmetries:
expiry drops only the access record (its refresh partner must survive to
rotate), revocation and rotation drop both sides, and the cache re-reads from
disk when a sibling process rewrites the file.
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from oduflow import oauth_token_store as store_mod
from oduflow.oauth_token_store import OAuthTokenStore


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "oauth_tokens.json")


@pytest.fixture
def store(path):
    return OAuthTokenStore(path, access_ttl=3600)


def _read(path) -> dict:
    with open(path) as f:
        return json.load(f)


class TestMintPair:
    def test_both_tokens_resolve(self, store):
        access, refresh, expires_at = store.mint_pair("1", ["openid"])

        assert store.get_access(access)["client_id"] == "1"
        assert store.get_refresh(refresh)["client_id"] == "1"
        assert expires_at > time.time()

    def test_scopes_are_stored_on_both_sides(self, store):
        access, refresh, _ = store.mint_pair("1", ["oduflow_env:main"])

        assert store.get_access(access)["scopes"] == ["oduflow_env:main"]
        assert store.get_refresh(refresh)["scopes"] == ["oduflow_env:main"]

    def test_expiry_is_now_plus_the_configured_ttl(self, path):
        store = OAuthTokenStore(path, access_ttl=120)
        before = int(time.time())

        _, _, expires_at = store.mint_pair("1", [])

        assert before + 120 <= expires_at <= int(time.time()) + 120

    def test_the_pair_records_point_at_each_other(self, store):
        access, refresh, _ = store.mint_pair("1", [])

        assert store.get_access(access)["refresh"] == refresh
        assert store.get_refresh(refresh)["access"] == access

    def test_tokens_are_unique_per_mint(self, store):
        first = store.mint_pair("1", [])
        second = store.mint_pair("1", [])

        assert first[0] != second[0]
        assert first[1] != second[1]

    def test_the_file_is_written_owner_only(self, store, path):
        store.mint_pair("1", [])

        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_an_unknown_token_resolves_to_nothing(self, store):
        assert store.get_access("nope") is None
        assert store.get_refresh("nope") is None

    def test_the_returned_record_is_a_copy(self, store):
        access, _, _ = store.mint_pair("1", [])

        store.get_access(access)["client_id"] = "tampered"

        assert store.get_access(access)["client_id"] == "1"


class TestExpiry:
    def test_an_expired_access_token_stops_resolving(self, path):
        store = OAuthTokenStore(path, access_ttl=-1)
        access, _, _ = store.mint_pair("1", [])

        assert store.get_access(access) is None

    def test_expiry_preserves_the_refresh_partner(self, path):
        # The whole point of refresh: an expired access token must still be
        # exchangeable for a new pair.
        store = OAuthTokenStore(path, access_ttl=-1)
        access, refresh, _ = store.mint_pair("1", [])

        assert store.get_access(access) is None
        assert store.get_refresh(refresh) is not None

    def test_a_token_expiring_exactly_now_is_still_valid(
        self, store, path, monkeypatch
    ):
        # The check is `expires_at < now`, so the token survives its own
        # expiry second.
        access, _, expires_at = store.mint_pair("1", [])
        monkeypatch.setattr(store_mod.time, "time", lambda: float(expires_at))

        assert store.get_access(access) is not None

    def test_a_token_expiring_a_second_ago_is_gone(self, store, monkeypatch):
        access, _, expires_at = store.mint_pair("1", [])
        monkeypatch.setattr(store_mod.time, "time", lambda: expires_at + 1.0)

        assert store.get_access(access) is None

    def test_a_record_without_an_expiry_never_expires(self, store, path):
        # Hand-written or legacy records carry no expires_at.
        with open(path, "w") as f:
            json.dump({"access": {"tok": {"client_id": "1"}}, "refresh": {}}, f)
        store._reload()

        assert store.get_access("tok")["client_id"] == "1"

    def test_pruning_removes_the_expired_record_from_disk(self, path):
        store = OAuthTokenStore(path, access_ttl=-1)
        access, refresh, _ = store.mint_pair("1", [])

        store.get_access(access)  # triggers the prune

        data = _read(path)
        assert access not in data["access"]
        assert refresh in data["refresh"]


class TestRotate:
    def test_rotation_returns_a_fresh_pair_with_the_same_identity(self, store):
        _, refresh, _ = store.mint_pair("7", ["oduflow_env:main"])

        result = store.rotate(refresh)

        assert result is not None
        new_access, new_refresh, expires_at, client_id, scopes = result
        assert client_id == "7"
        assert scopes == ["oduflow_env:main"]
        assert expires_at > time.time()
        assert store.get_access(new_access)["client_id"] == "7"
        assert store.get_refresh(new_refresh) is not None

    def test_rotation_invalidates_the_whole_old_pair(self, store):
        old_access, old_refresh, _ = store.mint_pair("1", [])

        store.rotate(old_refresh)

        assert store.get_refresh(old_refresh) is None
        assert store.get_access(old_access) is None

    def test_replaying_a_rotated_refresh_token_fails(self, store):
        _, refresh, _ = store.mint_pair("1", [])
        store.rotate(refresh)

        assert store.rotate(refresh) is None

    def test_an_unknown_refresh_token_rotates_to_nothing(self, store):
        assert store.rotate("never-issued") is None

    def test_a_failed_rotation_leaves_existing_tokens_alone(self, store):
        access, refresh, _ = store.mint_pair("1", [])

        store.rotate("never-issued")

        assert store.get_access(access) is not None
        assert store.get_refresh(refresh) is not None

    def test_the_new_access_token_carries_a_real_expiry(self, path):
        store = OAuthTokenStore(path, access_ttl=60)
        _, refresh, _ = store.mint_pair("1", [])

        new_access, _, expires_at, _, _ = store.rotate(refresh)

        assert store.get_access(new_access)["expires_at"] == expires_at
        assert expires_at >= int(time.time())


class TestRevoke:
    def test_revoking_an_access_token_kills_its_refresh_partner(self, store):
        access, refresh, _ = store.mint_pair("1", [])

        store.revoke(access)

        assert store.get_access(access) is None
        assert store.get_refresh(refresh) is None

    def test_revoking_a_refresh_token_kills_its_access_partner(self, store):
        access, refresh, _ = store.mint_pair("1", [])

        store.revoke(refresh)

        assert store.get_access(access) is None
        assert store.get_refresh(refresh) is None

    def test_revoking_an_unknown_token_is_a_no_op(self, store):
        access, _, _ = store.mint_pair("1", [])

        store.revoke("never-issued")

        assert store.get_access(access) is not None

    def test_other_pairs_survive_a_revocation(self, store):
        keep_access, keep_refresh, _ = store.mint_pair("1", [])
        drop_access, _, _ = store.mint_pair("2", [])

        store.revoke(drop_access)

        assert store.get_access(keep_access) is not None
        assert store.get_refresh(keep_refresh) is not None


class TestPersistenceAndCache:
    def test_tokens_survive_a_restart(self, path):
        access, refresh, _ = OAuthTokenStore(path, 3600).mint_pair("1", [])

        restarted = OAuthTokenStore(path, 3600)

        assert restarted.get_access(access)["client_id"] == "1"
        assert restarted.get_refresh(refresh) is not None

    def test_a_token_minted_by_a_sibling_process_is_picked_up(self, path):
        reader = OAuthTokenStore(path, 3600)
        writer = OAuthTokenStore(path, 3600)

        access, _, _ = writer.mint_pair("1", [])

        # The reader's cache predates the mint; a miss must re-read the file.
        assert reader.get_access(access)["client_id"] == "1"

    def test_a_refresh_token_minted_elsewhere_is_picked_up(self, path):
        reader = OAuthTokenStore(path, 3600)
        writer = OAuthTokenStore(path, 3600)

        _, refresh, _ = writer.mint_pair("1", [])

        assert reader.get_refresh(refresh) is not None

    def test_an_unchanged_file_is_not_re_read(self, path, monkeypatch):
        store = OAuthTokenStore(path, 3600)
        store.mint_pair("1", [])
        reloads = []
        monkeypatch.setattr(OAuthTokenStore, "_reload", lambda self: reloads.append(1))

        store.get_access("unknown")
        store.get_access("unknown")

        # A miss triggers _maybe_reload, but the mtime is unchanged, so the
        # expensive path must stay untaken.
        assert reloads == []

    def test_a_missing_file_starts_empty(self, path):
        assert OAuthTokenStore(path, 3600).get_access("anything") is None

    def test_a_corrupt_file_starts_empty_instead_of_crashing(self, path):
        with open(path, "w") as f:
            f.write("{not json")

        assert OAuthTokenStore(path, 3600).get_access("anything") is None

    def test_a_store_can_recover_from_corruption(self, path):
        with open(path, "w") as f:
            f.write("{not json")
        store = OAuthTokenStore(path, 3600)

        access, _, _ = store.mint_pair("1", [])

        assert store.get_access(access) is not None


class TestLoadFile:
    def test_wrong_shaped_sections_are_rejected(self, tmp_path):
        path = str(tmp_path / "s.json")
        with open(path, "w") as f:
            json.dump({"access": [], "refresh": {}}, f)

        assert store_mod._load_file(path) == {"access": {}, "refresh": {}}

    def test_a_bad_refresh_section_alone_is_enough_to_reject(self, tmp_path):
        # Both sections must be dicts; accepting a half-valid file would let a
        # later write persist a broken structure.
        path = str(tmp_path / "s.json")
        with open(path, "w") as f:
            json.dump({"access": {"t": {}}, "refresh": "nope"}, f)

        assert store_mod._load_file(path) == {"access": {}, "refresh": {}}

    def test_a_non_dict_document_is_rejected(self, tmp_path):
        path = str(tmp_path / "s.json")
        with open(path, "w") as f:
            json.dump(["not", "a", "store"], f)

        assert store_mod._load_file(path) == {"access": {}, "refresh": {}}

    def test_missing_sections_default_to_empty(self, tmp_path):
        path = str(tmp_path / "s.json")
        with open(path, "w") as f:
            json.dump({}, f)

        assert store_mod._load_file(path) == {"access": {}, "refresh": {}}


class TestPruneExpired:
    def test_reports_whether_anything_was_removed(self):
        data = {"access": {"a": {"expires_at": 1}}, "refresh": {}}

        assert store_mod._prune_expired(data, now=100) is True
        assert store_mod._prune_expired(data, now=100) is False

    def test_a_record_expiring_exactly_now_is_kept(self):
        data = {"access": {"a": {"expires_at": 100}}, "refresh": {}}

        assert store_mod._prune_expired(data, now=100) is False
        assert "a" in data["access"]

    def test_records_without_an_expiry_are_kept(self):
        data = {"access": {"a": {}}, "refresh": {}}

        assert store_mod._prune_expired(data, now=10**9) is False
        assert "a" in data["access"]

    def test_refresh_records_are_never_pruned(self):
        data = {
            "access": {"a": {"expires_at": 1}},
            "refresh": {"r": {"expires_at": 1}},
        }

        store_mod._prune_expired(data, now=100)

        assert data["refresh"] == {"r": {"expires_at": 1}}
