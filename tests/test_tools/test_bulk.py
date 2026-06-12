"""Unit tests for v0.2 #2381 bulk-operation tools (no network).

Bulk tools are thin orchestrators over ``update_issue`` / ``close_issue``.
The unit tests here stub those underlying functions out via monkeypatch so
the bulk-level concerns (input validation, result aggregation,
stop-on-error semantics) are exercised in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.tools import bulk


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


class _Sentinel:
    """Stand-in for the client. Bulk tools don't call it directly when the
    underlying issues.update_issue / issues.close_issue are stubbed."""


# ---------------------------------------------------------------------
# bulk_update_issues
# ---------------------------------------------------------------------


async def test_bulk_update_rejects_empty_issue_list(cache: SchemaCache) -> None:
    result = await bulk.bulk_update_issues(
        _Sentinel(), cache, issue_ids=[], notes="anything",
    )
    assert result["error"] == "validation_failed"


async def test_bulk_update_rejects_no_fields_supplied(cache: SchemaCache) -> None:
    """At least one updatable field must be provided."""
    result = await bulk.bulk_update_issues(
        _Sentinel(), cache, issue_ids=[1, 2, 3],
    )
    assert result["error"] == "validation_failed"


async def test_bulk_update_all_succeed(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All ids return success → succeeded list contains every id."""
    calls: list[dict[str, Any]] = []

    async def fake_update(client, cache, issue_id, **kwargs):
        calls.append({"issue_id": issue_id, **kwargs})
        return {"issue": {"id": issue_id, "status": {"name": "In Progress"}}}

    monkeypatch.setattr(bulk.issues_module, "update_issue", fake_update)

    result = await bulk.bulk_update_issues(
        _Sentinel(), cache,
        issue_ids=[10, 11, 12],
        notes="batch comment",
    )
    assert result["total"] == 3
    assert result["succeeded"] == [10, 11, 12]
    assert result["failed"] == []
    # Same kwargs sent to each.
    assert all(c["notes"] == "batch comment" for c in calls)


async def test_bulk_update_collects_failures_per_issue(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One id errors → recorded in failed[], others still processed."""
    async def fake_update(client, cache, issue_id, **kwargs):
        if issue_id == 11:
            return {"error": "redmine_api_404", "hint": "gone"}
        return {"issue": {"id": issue_id}}

    monkeypatch.setattr(bulk.issues_module, "update_issue", fake_update)

    result = await bulk.bulk_update_issues(
        _Sentinel(), cache,
        issue_ids=[10, 11, 12],
        notes="batch",
    )
    assert result["succeeded"] == [10, 12]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["issue_id"] == 11
    assert result["failed"][0]["error"] == "redmine_api_404"


async def test_bulk_update_stop_on_error_short_circuits(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With stop_on_error=True, processing halts at the first failure."""
    seen: list[int] = []

    async def fake_update(client, cache, issue_id, **kwargs):
        seen.append(issue_id)
        if issue_id == 11:
            return {"error": "redmine_api_422", "hint": "rejected"}
        return {"issue": {"id": issue_id}}

    monkeypatch.setattr(bulk.issues_module, "update_issue", fake_update)

    result = await bulk.bulk_update_issues(
        _Sentinel(), cache,
        issue_ids=[10, 11, 12],
        notes="batch",
        stop_on_error=True,
    )
    # Only 10 and 11 processed; 12 never touched.
    assert seen == [10, 11]
    assert result["succeeded"] == [10]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["issue_id"] == 11
    # 12 should appear in skipped, not failed.
    assert result["skipped"] == [12]


async def test_bulk_update_propagates_all_supported_fields(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every supported field flows through to update_issue."""
    captured: dict[str, Any] = {}

    async def fake_update(client, cache, issue_id, **kwargs):
        captured.update(kwargs)
        return {"issue": {"id": issue_id}}

    monkeypatch.setattr(bulk.issues_module, "update_issue", fake_update)

    await bulk.bulk_update_issues(
        _Sentinel(), cache,
        issue_ids=[1],
        subject="new subject",
        description="new desc",
        status="In Progress",
        priority="High",
        assigned_to_id=7,
        notes="batched",
    )
    assert captured["subject"] == "new subject"
    assert captured["description"] == "new desc"
    assert captured["status"] == "In Progress"
    assert captured["priority"] == "High"
    assert captured["assigned_to_id"] == 7
    assert captured["notes"] == "batched"


async def test_bulk_update_caps_batch_size(cache: SchemaCache) -> None:
    """Reject batches over MAX_BATCH_SIZE to avoid runaway calls."""
    too_many = list(range(1, bulk.MAX_BATCH_SIZE + 2))
    result = await bulk.bulk_update_issues(
        _Sentinel(), cache, issue_ids=too_many, notes="x",
    )
    assert result["error"] == "batch_too_large"
    assert result["max_batch_size"] == bulk.MAX_BATCH_SIZE


# ---------------------------------------------------------------------
# bulk_close
# ---------------------------------------------------------------------


async def test_bulk_close_rejects_empty_list(cache: SchemaCache) -> None:
    result = await bulk.bulk_close(_Sentinel(), cache, issue_ids=[])
    assert result["error"] == "validation_failed"


async def test_bulk_close_all_succeed(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_close(client, cache, issue_id, *, note=None):
        return {"issue": {"id": issue_id, "status": {"name": "Closed"}}}

    monkeypatch.setattr(bulk.issues_module, "close_issue", fake_close)

    result = await bulk.bulk_close(
        _Sentinel(), cache, issue_ids=[10, 11, 12], note="batched closure",
    )
    assert result["succeeded"] == [10, 11, 12]
    assert result["failed"] == []


async def test_bulk_close_records_workflow_disallowed_per_issue(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow-blocked closures are surfaced like any other failure."""
    async def fake_close(client, cache, issue_id, *, note=None):
        if issue_id == 11:
            return {
                "error": "workflow_transition_disallowed",
                "from_status": "New",
                "allowed_next_states": ["In Progress"],
            }
        return {"issue": {"id": issue_id}}

    monkeypatch.setattr(bulk.issues_module, "close_issue", fake_close)

    result = await bulk.bulk_close(
        _Sentinel(), cache, issue_ids=[10, 11, 12],
    )
    assert result["succeeded"] == [10, 12]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["error"] == "workflow_transition_disallowed"


async def test_bulk_close_propagates_note_to_each(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes_seen: list[str | None] = []

    async def fake_close(client, cache, issue_id, *, note=None):
        notes_seen.append(note)
        return {"issue": {"id": issue_id}}

    monkeypatch.setattr(bulk.issues_module, "close_issue", fake_close)
    await bulk.bulk_close(
        _Sentinel(), cache, issue_ids=[10, 11], note="cleanup pass",
    )
    assert notes_seen == ["cleanup pass", "cleanup pass"]


async def test_bulk_close_omits_note_when_empty(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes_seen: list[str | None] = []

    async def fake_close(client, cache, issue_id, *, note=None):
        notes_seen.append(note)
        return {"issue": {"id": issue_id}}

    monkeypatch.setattr(bulk.issues_module, "close_issue", fake_close)
    await bulk.bulk_close(_Sentinel(), cache, issue_ids=[10])
    assert notes_seen == [None]


async def test_bulk_close_caps_batch_size(cache: SchemaCache) -> None:
    too_many = list(range(1, bulk.MAX_BATCH_SIZE + 2))
    result = await bulk.bulk_close(_Sentinel(), cache, issue_ids=too_many)
    assert result["error"] == "batch_too_large"


# ---------------------------------------------------------------------
# bulk_create_issues (ClaudeCode#3141)
# ---------------------------------------------------------------------


class _CreateFakeClient:
    """Stand-in for the client when bulk_create_issues calls _find_existing.

    Stubs the underlying `client.get('/issues.json', ...)` so the
    idempotency code path runs end-to-end. The internal create_issue is
    monkeypatched separately.
    """

    def __init__(self, existing: dict[tuple[Any, str], list[dict]] | None = None) -> None:
        # key: (project, subject) -> list of issue dicts to return
        self._existing = existing or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if path == "/issues.json" and params:
            key = (params.get("project_id"), params.get("subject"))
            return {"issues": self._existing.get(key, [])}
        return {"issues": []}


async def test_bulk_create_rejects_bad_on_duplicate(cache: SchemaCache) -> None:
    result = await bulk.bulk_create_issues(
        _CreateFakeClient(), cache,
        issues=[{"project": "p", "tracker": "t", "subject": "s"}],
        on_duplicate="invalid",
    )
    assert result["error"] == "validation_failed"


async def test_bulk_create_rejects_missing_required_field(cache: SchemaCache) -> None:
    result = await bulk.bulk_create_issues(
        _CreateFakeClient(), cache,
        issues=[{"project": "p", "tracker": "t"}],  # no subject
    )
    assert result["error"] == "validation_failed"


async def test_bulk_create_caps_batch_size(cache: SchemaCache) -> None:
    too_many = [
        {"project": "p", "tracker": "t", "subject": f"s{i}"}
        for i in range(bulk.MAX_BATCH_SIZE + 1)
    ]
    result = await bulk.bulk_create_issues(_CreateFakeClient(), cache, issues=too_many)
    assert result["error"] == "batch_too_large"


async def test_bulk_create_empty_list_returns_empty_results(cache: SchemaCache) -> None:
    """Zero-issue input is a valid no-op."""
    result = await bulk.bulk_create_issues(_CreateFakeClient(), cache, issues=[])
    assert result["results"] == []
    assert result["summary"] == {"total": 0, "created": 0, "skipped": 0, "failed": 0}


async def test_bulk_create_happy_path(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    next_id = iter([101, 102, 103])

    async def fake_create(client, cache, **kwargs):
        return {"issue": {"id": next(next_id), "subject": kwargs["subject"]}}

    monkeypatch.setattr(bulk.issues_module, "create_issue", fake_create)
    specs = [
        {"project": "claudecode", "tracker": "Bug", "subject": f"item {i}"}
        for i in range(3)
    ]
    result = await bulk.bulk_create_issues(
        _CreateFakeClient(), cache, issues=specs, pacing_seconds=0,
    )
    assert result["summary"] == {"total": 3, "created": 3, "skipped": 0, "failed": 0}
    statuses = [r["status"] for r in result["results"]]
    assert statuses == ["created", "created", "created"]
    assert [r["id"] for r in result["results"]] == [101, 102, 103]


async def test_bulk_create_skip_dedups_existing_subject(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls: list[str] = []

    async def fake_create(client, cache, **kwargs):
        create_calls.append(kwargs["subject"])
        return {"issue": {"id": 999, "subject": kwargs["subject"]}}

    monkeypatch.setattr(bulk.issues_module, "create_issue", fake_create)
    client = _CreateFakeClient(existing={
        ("simsyssites", "abc.com — add GA4"): [
            {"id": 42, "subject": "abc.com — add GA4"},
        ],
    })
    result = await bulk.bulk_create_issues(
        client, cache,
        issues=[
            {"project": "simsyssites", "tracker": "Feature",
             "subject": "abc.com — add GA4"},                # duplicate → skip
            {"project": "simsyssites", "tracker": "Feature",
             "subject": "xyz.com — add GA4"},                # fresh → create
        ],
        pacing_seconds=0,
    )
    assert result["summary"] == {"total": 2, "created": 1, "skipped": 1, "failed": 0}
    assert result["results"][0]["status"] == "skipped"
    assert result["results"][0]["duplicate_of"] == 42
    assert result["results"][1]["status"] == "created"
    assert create_calls == ["xyz.com — add GA4"]


async def test_bulk_create_dedup_requires_exact_subject_match(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redmine's `subject=X` filter is substring-fuzzy; we exact-match
    client-side. Substring hits on the GET must NOT count as duplicates."""
    async def fake_create(client, cache, **kwargs):
        return {"issue": {"id": 999, "subject": kwargs["subject"]}}

    monkeypatch.setattr(bulk.issues_module, "create_issue", fake_create)
    client = _CreateFakeClient(existing={
        ("p", "abc"): [{"id": 41, "subject": "abc widget"}],
    })
    result = await bulk.bulk_create_issues(
        client, cache,
        issues=[{"project": "p", "tracker": "Bug", "subject": "abc"}],
        pacing_seconds=0,
    )
    assert result["results"][0]["status"] == "created"
    assert result["summary"]["created"] == 1


async def test_bulk_create_fail_mode_reports_duplicate_as_failure(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create(client, cache, **kwargs):
        return {"issue": {"id": 999, "subject": kwargs["subject"]}}

    monkeypatch.setattr(bulk.issues_module, "create_issue", fake_create)
    client = _CreateFakeClient(existing={
        ("p", "dup"): [{"id": 7, "subject": "dup"}],
    })
    result = await bulk.bulk_create_issues(
        client, cache,
        issues=[{"project": "p", "tracker": "Bug", "subject": "dup"}],
        on_duplicate="fail",
        pacing_seconds=0,
    )
    assert result["summary"]["failed"] == 1
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["error"] == "duplicate_subject"
    assert result["results"][0]["duplicate_of"] == 7


async def test_bulk_create_create_anyway_skips_pre_check(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_duplicate='create_anyway' must not issue the lookup GET."""
    async def fake_create(client, cache, **kwargs):
        return {"issue": {"id": 999, "subject": kwargs["subject"]}}

    monkeypatch.setattr(bulk.issues_module, "create_issue", fake_create)
    client = _CreateFakeClient(existing={
        ("p", "dup"): [{"id": 7, "subject": "dup"}],
    })
    result = await bulk.bulk_create_issues(
        client, cache,
        issues=[{"project": "p", "tracker": "Bug", "subject": "dup"}],
        on_duplicate="create_anyway",
        pacing_seconds=0,
    )
    assert result["results"][0]["status"] == "created"
    assert all(c[0] != "GET" for c in client.calls)


async def test_bulk_create_mid_loop_failure_collected_without_halt(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = iter(range(10))

    async def fake_create(client, cache, **kwargs):
        i = next(counter)
        if i == 1:
            return {"error": "redmine_api_422", "hint": "invalid"}
        return {"issue": {"id": 200 + i, "subject": kwargs["subject"]}}

    monkeypatch.setattr(bulk.issues_module, "create_issue", fake_create)
    specs = [{"project": "p", "tracker": "Bug", "subject": f"s{i}"} for i in range(3)]
    result = await bulk.bulk_create_issues(
        _CreateFakeClient(), cache, issues=specs, pacing_seconds=0,
    )
    statuses = [r["status"] for r in result["results"]]
    assert statuses == ["created", "failed", "created"]
    assert result["summary"] == {"total": 3, "created": 2, "skipped": 0, "failed": 1}


async def test_bulk_create_stop_on_error_lists_remainder(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = iter(range(10))

    async def fake_create(client, cache, **kwargs):
        i = next(counter)
        if i == 1:
            return {"error": "redmine_api_500", "hint": "boom"}
        return {"issue": {"id": 200 + i, "subject": kwargs["subject"]}}

    monkeypatch.setattr(bulk.issues_module, "create_issue", fake_create)
    specs = [{"project": "p", "tracker": "Bug", "subject": f"s{i}"} for i in range(4)]
    result = await bulk.bulk_create_issues(
        _CreateFakeClient(), cache, issues=specs, pacing_seconds=0, stop_on_error=True,
    )
    assert result["summary"]["created"] == 1
    assert result["summary"]["failed"] == 1
    assert "skipped_for_stop_on_error" in result
    skipped_subjects = [s["subject"] for s in result["skipped_for_stop_on_error"]]
    assert skipped_subjects == ["s2", "s3"]


async def test_bulk_create_forwards_optional_fields(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the keys present in the spec get forwarded to create_issue."""
    forwarded_kwargs: dict[str, Any] = {}

    async def fake_create(client, cache, **kwargs):
        forwarded_kwargs.update(kwargs)
        return {"issue": {"id": 1, "subject": kwargs["subject"]}}

    monkeypatch.setattr(bulk.issues_module, "create_issue", fake_create)
    await bulk.bulk_create_issues(
        _CreateFakeClient(), cache,
        issues=[{
            "project": "p", "tracker": "Bug", "subject": "s",
            "due_date": "2026-05-17", "difficulty": "Easy",
            "custom_fields": [{"id": 9, "value": "x"}],
        }],
        pacing_seconds=0,
    )
    assert forwarded_kwargs["due_date"] == "2026-05-17"
    assert forwarded_kwargs["difficulty"] == "Easy"
    assert forwarded_kwargs["custom_fields"] == [{"id": 9, "value": "x"}]
    assert "description" not in forwarded_kwargs
    assert "assigned_to_id" not in forwarded_kwargs
