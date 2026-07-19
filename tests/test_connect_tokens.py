"""One-time token store for the cross-subdomain Connect As landing."""

from oduflow import connect_tokens


def test_issue_then_consume_returns_sid_once():
    token = connect_tokens.issue("180.dev.example.com", "sid-abc")
    assert connect_tokens.consume(token, "180.dev.example.com") == "sid-abc"
    # One-time: a second consume of the same token fails.
    assert connect_tokens.consume(token, "180.dev.example.com") is None


def test_unknown_token_returns_none():
    assert connect_tokens.consume("does-not-exist", "180.dev.example.com") is None


def test_host_mismatch_rejected():
    token = connect_tokens.issue("180.dev.example.com", "sid")
    assert connect_tokens.consume(token, "181.dev.example.com") is None


def test_host_match_is_case_insensitive():
    token = connect_tokens.issue("180.DEV.Example.com", "sid")
    assert connect_tokens.consume(token, "180.dev.example.COM") == "sid"


def test_valid_within_ttl():
    token = connect_tokens.issue("h", "sid", now=1000.0)
    assert connect_tokens.consume(token, "h", now=1000.0 + 119) == "sid"


def test_expired_token_rejected():
    token = connect_tokens.issue("h", "sid", now=1000.0)
    assert connect_tokens.consume(token, "h", now=1000.0 + 121) is None
