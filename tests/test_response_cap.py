"""Tests for the configurable tool-response size cap."""

from __future__ import annotations

import json

from redmine_mcp import server
from redmine_mcp.config import Config


def test_dump_unlimited_by_default(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(server, "_config", Config())
    payload = {"a": "x" * 500}
    assert json.loads(server._dump(payload)) == payload


def test_dump_truncates_over_cap(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(server, "_config", Config(max_response_bytes=50))
    out = server._dump({"a": "x" * 500})
    data = json.loads(out)
    assert data["_truncated"] is True
    assert data["_limit_bytes"] == 50
    assert data["_original_bytes"] > 50
    assert len(data["preview"]) == 50


def test_dump_under_cap_untouched(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(server, "_config", Config(max_response_bytes=10_000))
    payload = {"a": "small"}
    assert json.loads(server._dump(payload)) == payload
