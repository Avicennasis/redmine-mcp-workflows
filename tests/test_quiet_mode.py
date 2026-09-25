"""Quiet write mode (#49381).

Write tools return the full 3-5KB issue JSON, which is the #1 cause of context
exhaustion in bulk pipelines. ``quiet: true`` reduces a SUCCESSFUL result to ids
+ status; errors always keep full detail.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from redmine_mcp import server

QUIET_TOOLS = (
    "redmine_create_issue",
    "redmine_update_issue",
    "redmine_close_issue",
    "redmine_add_comment",
    "redmine_bulk_create_issues",
)


@pytest.mark.parametrize("name", QUIET_TOOLS)
def test_quiet_parameter_is_exposed(name: str) -> None:
    fn = getattr(server, name)
    sig = inspect.signature(fn)
    assert "quiet" in sig.parameters, f"{name} must accept quiet"
    assert sig.parameters["quiet"].default is False, "quiet must default off"


def test_quiet_reduce_single_issue() -> None:
    full = {
        "issue": {"id": 4242, "subject": "hi", "description": "x" * 4000},
        "source": "api",
    }
    assert server._quiet_reduce(full) == {"id": 4242, "status": "ok"}


def test_quiet_reduce_keeps_top_level_status() -> None:
    # close_issue returns a status string; quiet must not invent "ok" over it.
    assert server._quiet_reduce({"id": 7, "status": "closed"}) == {
        "id": 7,
        "status": "closed",
    }


def test_quiet_reduce_error_passes_through() -> None:
    err = {"error": "validation_failed", "errors": [{"field": "subject"}]}
    assert server._quiet_reduce(err) == err


def test_quiet_reduce_no_id_still_acks() -> None:
    assert server._quiet_reduce({"source": "api"}) == {"status": "ok"}


def test_quiet_reduce_bulk_keeps_compact_per_item() -> None:
    full = {
        "results": [
            {"subject": "a", "status": "created", "id": 1, "big": "x" * 5000},
            {
                "subject": "b",
                "status": "skipped",
                "duplicate_of": 3,
                "big": "y" * 5000,
            },
            {
                "subject": "c",
                "status": "failed",
                "error": "422",
                "big": "z" * 5000,
            },
        ],
        "summary": {"total": 3, "created": 1, "skipped": 1, "failed": 1},
    }
    out = server._quiet_reduce(full)
    assert out["summary"] == {"total": 3, "created": 1, "skipped": 1, "failed": 1}
    assert out["results"] == [
        {"subject": "a", "status": "created", "id": 1},
        {"subject": "b", "status": "skipped", "duplicate_of": 3},
        {"subject": "c", "status": "failed", "error": "422"},
    ]
    # the whole point: the bulky fields are gone
    assert all("big" not in row for row in out["results"])


def test_wrap_reduces_success_end_to_end(monkeypatch) -> None:
    """The whole handler path — not just _quiet_reduce — emits the small ack."""

    monkeypatch.setenv("REDMINE_API_KEY", "test")

    async def fake_create(client, cache, **kw):
        return {
            "issue": {"id": 999, "subject": "s", "description": "d" * 5000},
            "source": "api",
        }

    monkeypatch.setattr(server.issues, "create_issue", fake_create)
    quiet = asyncio.run(
        server.redmine_create_issue(project="claudecode", tracker="Bug", subject="s", quiet=True)
    )
    full = asyncio.run(
        server.redmine_create_issue(project="claudecode", tracker="Bug", subject="s")
    )

    assert json.loads(quiet) == {"id": 999, "status": "ok"}
    assert len(quiet) < 100
    assert len(full) > 4000  # the non-quiet path is unchanged


def test_wrap_does_not_reduce_an_error(monkeypatch) -> None:
    """A failed write must keep full detail even in quiet mode."""

    monkeypatch.setenv("REDMINE_API_KEY", "test")

    async def fake_create(client, cache, **kw):
        return {
            "error": "validation_failed",
            "errors": [{"field": "subject"}],
            "hint": "subject is required",
        }

    monkeypatch.setattr(server.issues, "create_issue", fake_create)
    out = asyncio.run(
        server.redmine_create_issue(project="claudecode", tracker="Bug", subject="s", quiet=True)
    )
    data = json.loads(out)
    assert data["error"] == "validation_failed"
    assert data["errors"] == [{"field": "subject"}]
    assert data["hint"] == "subject is required"
