"""SSRF guards for outbound requests built from user-supplied URLs.

Used by the git remote validation and the import-from-Odoo path so the server
cannot be tricked into connecting to loopback, the cloud metadata endpoint
(169.254.169.254), or — for clearly-external operations — internal RFC1918
hosts.

Note: this resolves and checks the host at validation time. A later connection
re-resolves DNS, so this does not defeat a determined DNS-rebinding attacker;
it raises the bar against the common cases (literal internal IPs, metadata
endpoints, localhost) without pinning.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from oduflow.errors import FlowError


class BlockedURLError(FlowError):
    """The URL resolves to a network location that is not allowed."""


def _is_blocked(ip: ipaddress._BaseAddress, *, allow_private: bool) -> bool:
    # Never-legitimate remote targets, blocked even for internal git hosts.
    if (
        ip.is_loopback
        or ip.is_link_local  # includes 169.254.169.254 cloud metadata
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    ):
        return True
    # RFC1918 / ULA private ranges: blocked unless the caller opts in (internal
    # git servers are a legitimate use case; importing a DB from a URL is not).
    if not allow_private and ip.is_private:
        return True
    return False


def assert_allowed_host(hostname: str | None, *, allow_private: bool = False) -> None:
    """Raise BlockedURLError if hostname resolves to a disallowed address."""
    if not hostname:
        raise BlockedURLError("URL has no host component.")

    try:
        candidates = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            raise BlockedURLError(f"Cannot resolve host '{hostname}': {exc}") from exc
        candidates = [ipaddress.ip_address(info[4][0]) for info in infos]

    for ip in candidates:
        if _is_blocked(ip, allow_private=allow_private):
            raise BlockedURLError(
                f"Host '{hostname}' resolves to a blocked address ({ip}); "
                "refusing the request to prevent SSRF."
            )


def assert_allowed_url(
    url: str, *, require_https: bool = False, allow_private: bool = False
) -> None:
    """Validate the scheme and resolve-check the host of an outbound URL."""
    parsed = urlparse(url)
    if require_https and parsed.scheme != "https":
        raise BlockedURLError(
            f"URL must use https:// (got {parsed.scheme or 'unknown'}://)."
        )
    assert_allowed_host(parsed.hostname, allow_private=allow_private)
