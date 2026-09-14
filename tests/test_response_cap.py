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
    cap = 200
    monkeypatch.setattr(server, "_config", Config(max_response_bytes=cap))
    out = server._dump({"a": "x" * 500})
    data = json.loads(out)
    assert data["_truncated"] is True
    assert data["_limit_bytes"] == cap
    assert data["_original_bytes"] > cap
    assert data["preview"]
    assert len(out.encode("utf-8")) <= cap


def test_dump_under_cap_untouched(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(server, "_config", Config(max_response_bytes=10_000))
    payload = {"a": "small"}
    assert json.loads(server._dump(payload)) == payload


def test_dump_unicode_output_is_valid_and_byte_bounded(monkeypatch) -> None:  # noqa: ANN001
    cap = 120
    monkeypatch.setattr(server, "_config", Config(max_response_bytes=cap))
    out = server._dump({"a": "💥" * 500})
    assert json.loads(out)["_truncated"] is True
    assert len(out.encode("utf-8")) <= cap


def test_dump_tiny_cap_still_returns_valid_bounded_json(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(server, "_config", Config(max_response_bytes=1))
    out = server._dump({"a": "large"})
    assert json.loads(out) == 0
    assert len(out.encode("utf-8")) == 1
