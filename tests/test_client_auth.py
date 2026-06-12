"""Auth-header tests for RedmineClient (ClaudeCode#2383).

Locks in that the wrapper actually uses OAuth bearer when configured,
falls back to X-Redmine-API-Key when only the API key is set, and raises
loudly when neither credential is present.
"""

from __future__ import annotations

import pytest

from redmine_mcp.client import RedmineClient
from redmine_mcp.config import Config


def test_client_uses_oauth_bearer_when_configured() -> None:
    cfg = Config(oauth_token="bearer-T")
    client = RedmineClient(cfg)
    headers = client._client.headers
    assert headers["authorization"] == "Bearer bearer-T"
    # API key header must not leak through.
    assert "x-redmine-api-key" not in headers


def test_client_falls_back_to_api_key() -> None:
    cfg = Config(api_key="api-K")
    client = RedmineClient(cfg)
    headers = client._client.headers
    assert headers["x-redmine-api-key"] == "api-K"
    # No bearer when no oauth token.
    assert headers.get("authorization") is None


def test_client_prefers_oauth_when_both_set() -> None:
    cfg = Config(api_key="api-K", oauth_token="bearer-T")
    client = RedmineClient(cfg)
    headers = client._client.headers
    assert headers["authorization"] == "Bearer bearer-T"
    assert "x-redmine-api-key" not in headers


def test_client_extra_headers_override_auth_when_caller_insists() -> None:
    """extra_headers wins last — caller can still inject a custom Authorization
    if they want (e.g. for proxy auth in front of Redmine)."""
    cfg = Config(
        oauth_token="bearer-T",
        extra_headers={"Authorization": "Custom override-value"},
    )
    client = RedmineClient(cfg)
    assert client._client.headers["authorization"] == "Custom override-value"


def test_client_raises_when_no_credentials() -> None:
    cfg = Config()
    with pytest.raises(RuntimeError, match="No Redmine credentials configured"):
        RedmineClient(cfg)
