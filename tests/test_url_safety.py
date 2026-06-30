"""Tests for the SSRF guard (issue #45)."""

import pytest

from oduflow.url_safety import (
    BlockedURLError,
    assert_allowed_host,
    assert_allowed_url,
)


def test_public_ip_allowed():
    assert_allowed_host("8.8.8.8")
    assert_allowed_host("8.8.8.8", allow_private=True)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "169.254.169.254", "0.0.0.0", "::1", "224.0.0.1"],
)
def test_never_legitimate_hosts_blocked_even_with_allow_private(host):
    # Loopback, cloud-metadata link-local, unspecified, multicast: blocked even
    # for internal git hosts.
    with pytest.raises(BlockedURLError):
        assert_allowed_host(host, allow_private=True)


@pytest.mark.parametrize("host", ["10.0.0.1", "192.168.1.10", "172.16.5.5"])
def test_private_blocked_by_default(host):
    with pytest.raises(BlockedURLError):
        assert_allowed_host(host)  # allow_private=False


@pytest.mark.parametrize("host", ["10.0.0.1", "192.168.1.10", "172.16.5.5"])
def test_private_allowed_when_opted_in(host):
    # Internal git servers (validate_repo_url passes allow_private=True).
    assert_allowed_host(host, allow_private=True)


def test_require_https():
    with pytest.raises(BlockedURLError, match="https"):
        assert_allowed_url("http://8.8.8.8/x", require_https=True)
    assert_allowed_url("https://8.8.8.8/x", require_https=True)


def test_missing_host():
    with pytest.raises(BlockedURLError):
        assert_allowed_host("")


def test_metadata_url_blocked():
    with pytest.raises(BlockedURLError):
        assert_allowed_url(
            "http://169.254.169.254/latest/meta-data/", allow_private=True
        )
