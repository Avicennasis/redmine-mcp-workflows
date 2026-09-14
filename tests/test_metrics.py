"""Unit tests for the in-memory metrics registry."""

from __future__ import annotations

import json
from types import SimpleNamespace

from redmine_mcp import server
from redmine_mcp.metrics import Metrics


def test_record_counts_calls_and_errors() -> None:
    m = Metrics()
    m.record("redmine_get_issue", 0.01)
    m.record("redmine_get_issue", 0.03)
    m.record("redmine_get_issue", 0.02, error=True)
    snap = m.snapshot()
    tool = snap["tools"]["redmine_get_issue"]
    assert tool["calls"] == 3
    assert tool["errors"] == 1
    assert tool["max_ms"] == 30.0
    assert snap["total_calls"] == 3
    assert snap["total_errors"] == 1


def test_snapshot_empty() -> None:
    snap = Metrics().snapshot()
    assert snap == {"total_calls": 0, "total_errors": 0, "tools": {}}


def test_reset_clears() -> None:
    m = Metrics()
    m.record("x", 0.1, error=True)
    m.reset()
    assert m.snapshot() == {"total_calls": 0, "total_errors": 0, "tools": {}}


def test_snapshot_reset_returns_then_atomically_clears() -> None:
    m = Metrics()
    m.record("x", 0.1, error=True)
    before = m.snapshot(reset=True)
    assert before["total_calls"] == 1
    assert before["total_errors"] == 1
    assert m.snapshot() == {"total_calls": 0, "total_errors": 0, "tools": {}}


async def test_wrap_counts_structured_error_as_error(monkeypatch) -> None:
    metrics = Metrics()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

    monkeypatch.setattr(server, "METRICS", metrics)
    monkeypatch.setattr(server, "_get_config", lambda: SimpleNamespace(read_only=False))
    monkeypatch.setattr(server, "_get_cache", object)
    monkeypatch.setattr(server, "RedmineClient", lambda _cfg: FakeClient())

    async def factory(_client, _cache):
        return {"error": "validation_failed"}

    await server._wrap(factory)
    assert metrics.snapshot()["total_errors"] == 1


async def test_wrap_does_not_mask_successful_write_when_invalidation_fails(
    monkeypatch, caplog
) -> None:  # noqa: ANN001
    metrics = Metrics()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

    class BrokenCache:
        def invalidate_projects(self) -> None:
            raise RuntimeError("disk unavailable")

    monkeypatch.setattr(server, "METRICS", metrics)
    monkeypatch.setattr(server, "_get_config", lambda: SimpleNamespace(read_only=False))
    monkeypatch.setattr(server, "_get_cache", BrokenCache)
    monkeypatch.setattr(server, "RedmineClient", lambda _cfg: FakeClient())

    async def factory(_client, _cache):
        return {"created": True}

    result = await server._wrap(factory, write=True)
    assert json.loads(result) == {"created": True}
    assert "cache invalidation failed after successful write" in caplog.text
    assert metrics.snapshot()["total_errors"] == 0
