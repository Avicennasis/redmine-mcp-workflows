"""Unit tests for the project activity-feed tool (#44337)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import activity


class FakeClient:
    def __init__(
        self,
        responses: dict[tuple[str, str], Any] | None = None,
        *,
        errors: dict[tuple[str, str], RedmineAPIError] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if ("GET", path) in self._errors:
            raise self._errors[("GET", path)]
        return self._responses.get(("GET", path))


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


def _full_client(
    overrides: dict[tuple[str, str], Any] | None = None,
    *,
    errors: dict[tuple[str, str], RedmineAPIError] | None = None,
) -> FakeClient:
    responses: dict[tuple[str, str], Any] = {
        ("GET", "/issues.json"): {
            "issues": [
                {
                    "id": 101,
                    "subject": "Fix the widget",
                    "updated_on": "2026-09-10T10:00:00Z",
                    "author": {"id": 5, "name": "Ada"},
                    "status": {"id": 2, "name": "In Progress"},
                    "tracker": {"id": 1, "name": "Bug"},
                    "assigned_to": {"id": 5, "name": "Ada"},
                    "journals": [{"notes": "started work"}],
                },
                {
                    "id": 102,
                    "subject": "Old issue",
                    "updated_on": "2025-01-01T00:00:00Z",
                    "author": {"id": 6, "name": "Bob"},
                    "status": {"id": 5, "name": "Closed"},
                    "tracker": {"id": 1, "name": "Bug"},
                },
            ]
        },
        ("GET", "/projects/64/news.json"): {
            "news": [
                {
                    "id": 7,
                    "title": "Release 2.0",
                    "summary": "shipped",
                    "created_on": "2026-09-11T09:00:00Z",
                    "author": {"id": 5, "name": "Ada"},
                }
            ]
        },
        ("GET", "/projects/64/wiki/index.json"): {
            "wiki_pages": [
                {
                    "title": "Runbook",
                    "version": 4,
                    "updated_on": "2026-09-12T08:00:00Z",
                }
            ]
        },
        ("GET", "/projects/64/boards.json"): {"boards": [{"id": 7, "name": "General"}]},
        ("GET", "/boards/7/messages.json"): {
            "messages": [
                {
                    "id": 55,
                    "subject": "Kickoff",
                    "content": "hello",
                    "created_on": "2026-09-09T07:00:00Z",
                    "author": {"id": 6, "name": "Bob"},
                }
            ]
        },
        ("GET", "/time_entries.json"): {
            "time_entries": [
                {
                    "id": 900,
                    "hours": 2.5,
                    "spent_on": "2026-09-13",
                    "comments": "review",
                    "user": {"id": 5, "name": "Ada"},
                    "activity": {"id": 9, "name": "Development"},
                    "issue": {"id": 101},
                }
            ]
        },
        ("GET", "/projects/64/files.json"): {
            "files": [
                {
                    "id": 3,
                    "filename": "spec.pdf",
                    "created_on": "2026-09-08T06:00:00Z",
                    "author": {"id": 5, "name": "Ada"},
                    "content_url": "http://example/spec.pdf",
                }
            ]
        },
    }
    if overrides:
        responses.update(overrides)
    return FakeClient(responses, errors=errors)


# ---------------------------------------------------------------------
# merging + sorting
# ---------------------------------------------------------------------


async def test_activity_feed_merges_all_sources_sorted_desc(cache: SchemaCache) -> None:
    client = _full_client()
    result = await activity.activity_feed(client, cache, project=64)

    assert result["project_id"] == 64
    assert result["count"] == 7
    assert set(result["sources"]) == set(activity.ACTIVITY_TYPES)
    assert all("error" not in s for s in result["sources"].values())

    timestamps = [e["timestamp"] for e in result["events"]]
    assert timestamps == sorted(timestamps, reverse=True)
    types = {e["type"] for e in result["events"]}
    assert types == {"issue", "news", "wiki", "forum", "time_entry", "file"}


async def test_activity_feed_issue_event_carries_fields(cache: SchemaCache) -> None:
    client = _full_client()
    result = await activity.activity_feed(client, cache, project=64)
    issue = next(e for e in result["events"] if e.get("id") == 101)
    assert issue["title"] == "Fix the widget"
    assert issue["author"] == "Ada"
    assert issue["url"] == "/issues/101"
    assert issue["status"] == "In Progress"
    assert "started work" in issue["description"]


# ---------------------------------------------------------------------
# filtering
# ---------------------------------------------------------------------


async def test_activity_feed_type_filter_queries_only_requested(cache: SchemaCache) -> None:
    client = _full_client()
    result = await activity.activity_feed(client, cache, project=64, activity_types=["issues"])
    assert list(result["sources"]) == ["issues"]
    assert {e["type"] for e in result["events"]} == {"issue"}
    called_paths = {path for _, path, _ in client.calls}
    assert called_paths == {"/issues.json"}


async def test_activity_feed_date_range_filters_client_side(cache: SchemaCache) -> None:
    client = _full_client()
    result = await activity.activity_feed(
        client,
        cache,
        project=64,
        from_date="2026-09-10",
        to_date="2026-09-12",
    )
    # 2026-09-09 (forum), 2026-09-08 (file) and 2025 (old issue) are excluded.
    assert [e["timestamp"][:10] for e in result["events"]] == [
        "2026-09-12",
        "2026-09-11",
        "2026-09-10",
    ]


async def test_activity_feed_issues_query_uses_updated_on_range(cache: SchemaCache) -> None:
    client = _full_client()
    await activity.activity_feed(
        client,
        cache,
        project=64,
        from_date="2026-09-01",
        to_date="2026-09-30",
        activity_types=["issues"],
    )
    _, _, params = next(c for c in client.calls if c[1] == "/issues.json")
    assert params["updated_on"] == "><2026-09-01|2026-09-30"
    assert params["status_id"] == "*"


async def test_activity_feed_limit_truncates(cache: SchemaCache) -> None:
    client = _full_client()
    result = await activity.activity_feed(client, cache, project=64, limit=2)
    assert result["count"] == 2
    assert result["truncated"] is True


# ---------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------


async def test_activity_feed_source_error_is_isolated(cache: SchemaCache) -> None:
    client = _full_client(
        errors={
            ("GET", "/projects/64/boards.json"): RedmineAPIError(status_code=403, body="denied"),
        }
    )
    result = await activity.activity_feed(client, cache, project=64)
    assert result["sources"]["forums"]["error"] == "redmine_api_403"
    # Every other source still contributed.
    assert result["count"] == 6
    assert "issue" in {e["type"] for e in result["events"]}


async def test_activity_feed_invalid_type_rejected_before_io(cache: SchemaCache) -> None:
    client = _full_client()
    result = await activity.activity_feed(client, cache, project=64, activity_types=["nope"])
    assert result["error"] == "invalid_activity_type"
    assert client.calls == []


async def test_activity_feed_project_not_found(cache: SchemaCache) -> None:
    client = FakeClient(
        {},
        errors={
            ("GET", "/projects/bogus.json"): RedmineAPIError(status_code=404, body="nope"),
        },
    )
    client._responses[("GET", "/projects.json")] = {"projects": []}
    result = await activity.activity_feed(client, cache, project="bogus")
    assert result["error"] == "project_not_found"


async def test_activity_feed_invalid_limit(cache: SchemaCache) -> None:
    client = _full_client()
    result = await activity.activity_feed(client, cache, project=64, limit=0)
    assert result["error"] == "invalid_limit"
    assert client.calls == []


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def test_in_range_open_bounds() -> None:
    assert activity._in_range("2026-09-10T00:00:00Z", None, None) is True
    assert activity._in_range(None, "2026-09-01", None) is False
    assert activity._in_range("2026-09-10T00:00:00Z", "2026-09-10", "2026-09-10") is True
    assert activity._in_range("2026-09-09T00:00:00Z", "2026-09-10", None) is False


def test_updated_on_range_param() -> None:
    assert activity._updated_on_range_param(None, None) is None
    assert activity._updated_on_range_param("2026-01-01", None) == ">=2026-01-01"
    assert activity._updated_on_range_param(None, "2026-12-31") == "<=2026-12-31"
    assert activity._updated_on_range_param("2026-01-01", "2026-12-31") == "><2026-01-01|2026-12-31"
