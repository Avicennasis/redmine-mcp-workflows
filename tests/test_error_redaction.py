"""Error-boundary redaction tests (#44445).

The requirement is narrow on purpose: credential *values* must not reach the
LLM context window, but URLs, hosts, paths and error bodies stay intact so
diagnostics (e.g. which proxy blocked a path) survive.
"""

from __future__ import annotations

from redmine_mcp.errors import RedmineAPIError, StructuredError
from redmine_mcp.logging_utils import REDACTED


def test_api_error_redacts_header_in_body() -> None:
    err = RedmineAPIError(status_code=401, body="sent X-Redmine-API-Key: leaky-key")
    payload = err.as_structured()
    assert "leaky-key" not in str(payload)
    assert f"X-Redmine-API-Key: {REDACTED}" in payload["body"]
    # The non-secret hint is preserved.
    assert payload["hint"] == "Authentication failed. Check REDMINE_API_KEY."


def test_api_error_redacts_known_secret_in_dict_body() -> None:
    err = RedmineAPIError(
        status_code=422,
        body={"errors": ["token super-secret-value is invalid"]},
        secrets=["super-secret-value"],
    )
    payload = err.as_structured()
    assert "super-secret-value" not in str(payload)
    assert REDACTED in payload["body"]["errors"][0]


def test_api_error_keeps_urls_and_hosts() -> None:
    body = '<a href="https://auth.gateway.simmons.systems/?rd=/issues.json">Unauthorized</a>'
    payload = RedmineAPIError(status_code=401, body=body).as_structured()
    # The proxy URL is diagnostic value and must survive.
    assert "auth.gateway.simmons.systems" in payload["body"]
    assert "/issues.json" in payload["body"]


def test_api_error_str_does_not_leak_known_secret() -> None:
    err = RedmineAPIError(status_code=500, body="oops", secrets=["deadbeefdeadbeef"])
    assert "deadbeefdeadbeef" not in str(err)


def test_api_error_redacts_explicit_hint() -> None:
    err = RedmineAPIError(status_code=0, body="", hint="try key=abc123 next time")
    payload = err.as_structured()
    assert "abc123" not in payload["hint"]
    assert f"key={REDACTED}" in payload["hint"]


def test_structured_error_as_dict_redacts_extra() -> None:
    err = StructuredError(
        error="some_error",
        hint="Authorization: Bearer abc.def",
        extra={"url": "http://x.example/y?token=zzz"},
    )
    payload = err.as_dict()
    assert "abc.def" not in str(payload)
    assert "zzz" not in str(payload)
    assert "http://x.example/y" in payload["url"]
