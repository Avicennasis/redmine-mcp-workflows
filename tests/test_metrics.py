"""Unit tests for the in-memory metrics registry."""

from __future__ import annotations

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
