"""Server-wrapper tests for redmine_request.

The internal `passthrough.request()` already accepts dict bodies; the bug
was at the FastMCP wrapper in `server.py`, which used to declare
`body: str` and reject dict-shaped inputs at pydantic validation time.
These tests exercise the wrapper (not the internal function) to lock in
the dual string/dict shape.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from redmine_mcp import server
from redmine_mcp.config import Config


@pytest.fixture(autouse=True)
def force_passthrough_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure _get_config() returns a Config with enable_passthrough=True
    regardless of how the env was set when the test session started."""
    cfg = Config(
        redmine_url="http://127.0.0.1:8281",
        api_key="dummy-test-key",
        enable_passthrough=True,
        read_only=False,
    )
    monkeypatch.setattr(server, "_get_config", lambda: cfg)


class _FakeClient:
    """No-op async context manager. The body/params capture happens at
    the passthrough.request level, not at the client level."""

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_redmine_request_accepts_string_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original supported shape — JSON-encoded string body."""
    monkeypatch.setattr(server, "RedmineClient", _FakeClient)
    captured: dict[str, Any] = {}

    async def fake_request(client: Any, cache: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"validation_skipped": True, "body": {"ok": True}}

    monkeypatch.setattr("redmine_mcp.tools.passthrough.request", fake_request)

    raw = await server.redmine_request(
        method="PUT",
        path="/issues/42.json",
        body='{"issue": {"due_date": "2026-05-17"}}',
    )
    result = json.loads(raw)
    assert "error" not in result
    assert captured["body"] == {"issue": {"due_date": "2026-05-17"}}


@pytest.mark.asyncio
async def test_redmine_request_accepts_dict_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for when the MCP transport delivers `body`
    already parsed as a dict, the wrapper must accept it (rather than
    failing pydantic string validation) and forward it as-is."""
    monkeypatch.setattr(server, "RedmineClient", _FakeClient)
    captured: dict[str, Any] = {}

    async def fake_request(client: Any, cache: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"validation_skipped": True, "body": {"ok": True}}

    monkeypatch.setattr("redmine_mcp.tools.passthrough.request", fake_request)

    raw = await server.redmine_request(
        method="PUT",
        path="/issues/42.json",
        body={"issue": {"due_date": "2026-05-17"}},
    )
    result = json.loads(raw)
    assert "error" not in result
    # Dict passes through untouched — no re-serialize, no string detour.
    assert captured["body"] == {"issue": {"due_date": "2026-05-17"}}


@pytest.mark.asyncio
async def test_redmine_request_accepts_dict_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same fix applied to `params` — accept dict for symmetry with body."""
    monkeypatch.setattr(server, "RedmineClient", _FakeClient)
    captured: dict[str, Any] = {}

    async def fake_request(client: Any, cache: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"validation_skipped": True, "body": {}}

    monkeypatch.setattr("redmine_mcp.tools.passthrough.request", fake_request)

    raw = await server.redmine_request(
        method="GET",
        path="/issues.json",
        params={"limit": 5, "sort": "id:desc"},
    )
    result = json.loads(raw)
    assert "error" not in result
    assert captured["params"] == {"limit": 5, "sort": "id:desc"}


@pytest.mark.asyncio
async def test_redmine_request_empty_dict_body_treated_as_no_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`body={}` should be treated the same as `body=""` — no body sent."""
    monkeypatch.setattr(server, "RedmineClient", _FakeClient)
    captured: dict[str, Any] = {}

    async def fake_request(client: Any, cache: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"validation_skipped": True, "body": None}

    monkeypatch.setattr("redmine_mcp.tools.passthrough.request", fake_request)

    raw = await server.redmine_request(
        method="DELETE",
        path="/issues/42.json",
        body={},
    )
    result = json.loads(raw)
    assert "error" not in result
    assert captured["body"] is None


@pytest.mark.asyncio
async def test_redmine_request_invalid_string_body_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed JSON string still returns the existing structured error
    (the dict branch shouldn't have changed this path)."""
    monkeypatch.setattr(server, "RedmineClient", _FakeClient)
    raw = await server.redmine_request(
        method="POST",
        path="/issues.json",
        body="{not json",
    )
    result = json.loads(raw)
    assert result["error"] == "passthrough_body_invalid_json"
    assert result["validation_skipped"] is True
