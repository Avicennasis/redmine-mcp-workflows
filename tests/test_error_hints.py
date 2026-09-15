"""Hints for RedmineAPIError — especially 401 disambiguation.

A 401 from Redmine and a 401 from a reverse proxy in front of Redmine look
identical by status code but mean opposite things: the first says the API key
is wrong, the second says the key was never examined. The hint has to tell
them apart, because "check your API key" costs real time when the key works.
"""

from __future__ import annotations

from redmine_mcp.errors import RedmineAPIError, _hint_for_status, _looks_like_proxy_denial

# The exact shape Authelia returns: an HTML anchor to the auth gateway.
AUTHELIA_401 = (
    '<a href="https://auth.gateway.simmons.systems/?rd=https%3A%2F%2F'
    'trouble.simmons.systems%2Fissue_statuses.json&amp;rm=GET">401 Unauthorized</a>'
)


def test_proxy_denial_body_is_recognised():
    assert _looks_like_proxy_denial(AUTHELIA_401)


def test_redmine_json_401_is_not_proxy_denial():
    assert not _looks_like_proxy_denial('{"error": "Invalid or missing API key"}')


def test_plain_text_401_is_not_proxy_denial():
    assert not _looks_like_proxy_denial("401 Unauthorized")


def test_unrelated_html_401_is_not_proxy_denial():
    """HTML alone isn't enough — it must point at an identity provider."""
    assert not _looks_like_proxy_denial("<html><body>401 Unauthorized</body></html>")


def test_non_string_body_is_not_proxy_denial():
    assert not _looks_like_proxy_denial({"detail": "Unauthorized"})
    assert not _looks_like_proxy_denial(None)


def test_proxy_denial_hint_points_at_the_proxy_not_the_key():
    hint = _hint_for_status(401, AUTHELIA_401)

    assert "proxy" in hint.lower()
    assert "API key itself is probably fine" in hint
    # It names the concrete trap: close_issue needs the status enumeration.
    assert "close_issue" in hint
    assert "/issue_statuses.json" in hint


def test_redmine_401_hint_still_points_at_the_key():
    hint = _hint_for_status(401, '{"error": "Invalid or missing API key"}')
    assert hint == "Authentication failed. Check REDMINE_API_KEY."


def test_as_structured_uses_the_proxy_hint():
    err = RedmineAPIError(401, AUTHELIA_401).as_structured()

    assert err["error"] == "redmine_api_401"
    assert err["status_code"] == 401
    assert "proxy" in err["hint"].lower()


def test_as_structured_keeps_an_explicit_hint():
    err = RedmineAPIError(401, AUTHELIA_401, hint="caller-supplied").as_structured()
    assert err["hint"] == "caller-supplied"


def test_other_status_hints_are_unchanged():
    """Regression guard: the 401 work must not disturb the other statuses."""
    assert _hint_for_status(403).startswith("Access denied")
    assert _hint_for_status(404).startswith("Resource not found")
    assert _hint_for_status(422).startswith("Validation failed")
    assert _hint_for_status(500).startswith("Redmine server error")
    assert _hint_for_status(418) == ""
    # A proxy 403 keeps the generic hint — only 401 has the two-cause split.
    assert _hint_for_status(403, AUTHELIA_401).startswith("Access denied")
