"""Advertised tool surface and argument handling at the server layer (card 23 / J).

* ``redmine_request`` is not advertised while the passthrough is disabled —
  every call would answer ``passthrough_disabled``.
* ``redmine_update_issue`` can clear ``due_date`` / ``start_date`` without the
  passthrough (``clear_due_date`` / ``clear_start_date``).
* ``redmine_search_issues`` takes an opt-in ``brief`` / ``fields`` projection.
* ``redmine_close_issue`` passes ``clear_held`` through.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from redmine_mcp import server
from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.config import Config
from redmine_mcp.tools import issues

# ---------------------------------------------------------------------
# redmine_request visibility
# ---------------------------------------------------------------------


@pytest.fixture
def isolated_tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Let apply_tool_filter remove tools from a COPY of the registry."""
    tools = dict(server.mcp._tool_manager._tools)
    monkeypatch.setattr(server.mcp._tool_manager, "_tools", tools)
    return tools


def test_passthrough_hidden_when_disabled(isolated_tools: dict[str, Any]) -> None:
    before = set(isolated_tools)
    assert "redmine_request" in before
    cfg = Config(redmine_url="http://r.test", api_key="k", enable_passthrough=False)
    allowed = server.apply_tool_filter(cfg)
    assert "redmine_request" not in allowed
    assert "redmine_request" not in isolated_tools
    assert set(isolated_tools) == before - {"redmine_request"}


def test_passthrough_listed_when_enabled(isolated_tools: dict[str, Any]) -> None:
    cfg = Config(redmine_url="http://r.test", api_key="k", enable_passthrough=True)
    allowed = server.apply_tool_filter(cfg)
    assert "redmine_request" in allowed
    assert "redmine_request" in isolated_tools


def test_no_tool_description_points_at_a_hidden_passthrough_for_due_date() -> None:
    doc = inspect.getdoc(server.redmine_update_issue) or ""
    assert "clear_due_date" in doc
    assert "redmine_request" not in doc


# ---------------------------------------------------------------------
# server wrappers pass the new arguments through
# ---------------------------------------------------------------------


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    cfg = Config(redmine_url="http://r.test", api_key="k", read_only=False)
    cache = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    monkeypatch.setattr(server, "_get_config", lambda: cfg)
    monkeypatch.setattr(server, "_get_cache", lambda: cache)

    class _NoClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def __aenter__(self) -> _NoClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

    monkeypatch.setattr(server, "RedmineClient", _NoClient)
    seen: dict[str, Any] = {}

    def recorder(name: str):
        async def fake(_client: Any, _cache: Any, *args: Any, **kwargs: Any) -> dict:
            seen[name] = {"args": args, "kwargs": kwargs}
            return {"issues": [], "total_count": 0, "source": "api"}

        return fake

    monkeypatch.setattr(issues, "update_issue", recorder("update_issue"))
    monkeypatch.setattr(issues, "close_issue", recorder("close_issue"))
    monkeypatch.setattr(issues, "search_issues", recorder("search_issues"))
    return seen


async def test_update_issue_clear_due_date_passes_through(captured: dict[str, Any]) -> None:
    await server.redmine_update_issue(issue_id=7, clear_due_date=True)
    kw = captured["update_issue"]["kwargs"]
    assert kw["clear_due_date"] is True
    assert kw["due_date"] is None
    assert kw["clear_start_date"] is False


@pytest.mark.parametrize("name", ["due_date", "start_date"])
async def test_update_issue_rejects_set_and_clear_together(
    captured: dict[str, Any], name: str
) -> None:
    out = json.loads(
        await server.redmine_update_issue(issue_id=7, **{name: "2026-10-06", f"clear_{name}": True})
    )
    assert out["error"] == f"{name}_arguments_conflict"
    assert "update_issue" not in captured


async def test_close_issue_passes_clear_held(captured: dict[str, Any]) -> None:
    await server.redmine_close_issue(issue_id=7, clear_held=True)
    assert captured["close_issue"]["kwargs"]["clear_held"] is True


async def test_search_default_requests_whole_issues(captured: dict[str, Any]) -> None:
    await server.redmine_search_issues(query="x")
    assert captured["search_issues"]["kwargs"]["fields"] is None


async def test_search_brief_requests_the_brief_projection(captured: dict[str, Any]) -> None:
    await server.redmine_search_issues(brief=True)
    assert captured["search_issues"]["kwargs"]["fields"] == list(issues.BRIEF_ISSUE_FIELDS)


@pytest.mark.parametrize("fields", ["subject, status", ["subject", "status"]])
async def test_search_fields_accepts_string_or_list_and_overrides_brief(
    captured: dict[str, Any], fields: Any
) -> None:
    await server.redmine_search_issues(brief=True, fields=fields)
    assert captured["search_issues"]["kwargs"]["fields"] == ["subject", "status"]
