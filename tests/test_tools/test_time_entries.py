"""Unit tests for v0.2 #2377 time-entry tools (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import time_entries


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

    async def post(self, path: str, *, json: Any) -> Any:
        self.calls.append(("POST", path, json))
        if ("POST", path) in self._errors:
            raise self._errors[("POST", path)]
        return self._responses.get(("POST", path))

    async def put(self, path: str, *, json: Any) -> Any:
        self.calls.append(("PUT", path, json))
        if ("PUT", path) in self._errors:
            raise self._errors[("PUT", path)]
        return self._responses.get(("PUT", path))

    async def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path, None))
        if ("DELETE", path) in self._errors:
            raise self._errors[("DELETE", path)]
        return self._responses.get(("DELETE", path))


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


def _seed_activities(cache: SchemaCache) -> None:
    cache.put_meta_json(
        "time_entry_activities",
        [
            {"id": 8, "name": "Design"},
            {"id": 9, "name": "Development"},
            {"id": 10, "name": "QA"},
        ],
    )


# ---------------------------------------------------------------------
# create_time_entry
# ---------------------------------------------------------------------


async def test_create_happy_path_resolves_activity_name(cache: SchemaCache) -> None:
    _seed_activities(cache)
    client = FakeClient(
        {
            ("POST", "/time_entries.json"): {
                "time_entry": {
                    "id": 100,
                    "hours": 2.5,
                    "activity": {"id": 9, "name": "Development"},
                },
            },
        }
    )
    result = await time_entries.create_time_entry(
        client,
        cache,
        hours="2:30",
        issue_id=42,
        activity="Development",
        comments="lunchtime poking",
    )
    assert result["time_entry"]["id"] == 100
    posted = client.calls[-1][2]["time_entry"]
    assert posted["hours"] == 2.5
    assert posted["issue_id"] == 42
    assert posted["activity_id"] == 9
    assert posted["comments"] == "lunchtime poking"
    assert "project_id" not in posted


async def test_create_requires_issue_or_project(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await time_entries.create_time_entry(client, cache, hours=1.0)
    assert result["error"] == "validation_failed"
    assert client.calls == []


async def test_create_rejects_invalid_hours_before_post(cache: SchemaCache) -> None:
    _seed_activities(cache)
    client = FakeClient()
    result = await time_entries.create_time_entry(
        client,
        cache,
        hours="not-a-number",
        project_id=15,
    )
    assert result["error"] == "validation_failed"
    assert result["errors"][0]["error"] == "time_entry_hours_invalid"
    assert client.calls == []


async def test_create_rejects_unknown_activity_name(cache: SchemaCache) -> None:
    _seed_activities(cache)
    client = FakeClient()
    result = await time_entries.create_time_entry(
        client,
        cache,
        hours=1.0,
        project_id=15,
        activity="Underwater Basket Weaving",
    )
    assert result["error"] == "activity_not_found"
    # Activity check must short-circuit before the POST.
    assert not any(c[0] == "POST" for c in client.calls)


async def test_create_propagates_redmine_422(cache: SchemaCache) -> None:
    _seed_activities(cache)
    client = FakeClient(
        errors={
            ("POST", "/time_entries.json"): RedmineAPIError(
                status_code=422, body={"errors": ["Activity is required"]}
            ),
        }
    )
    result = await time_entries.create_time_entry(
        client,
        cache,
        hours=1.0,
        issue_id=42,
    )
    assert result["error"] == "redmine_api_422"


# ---------------------------------------------------------------------
# list_time_entries
# ---------------------------------------------------------------------


async def test_list_passes_filters_and_caps_limit(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/time_entries.json"): {
                "time_entries": [{"id": 1, "hours": 1.5}],
                "total_count": 1,
            },
        }
    )
    result = await time_entries.list_time_entries(
        client,
        cache,
        issue_id=42,
        user_id=1,
        from_date="2026-05-01",
        to_date="2026-05-31",
        limit=500,
        offset=10,
    )
    assert result["total_count"] == 1
    sent = client.calls[-1][2]
    assert sent["issue_id"] == 42
    assert sent["user_id"] == 1
    assert sent["from"] == "2026-05-01"
    assert sent["to"] == "2026-05-31"
    assert sent["limit"] == 100  # capped
    assert sent["offset"] == 10


async def test_list_with_no_filters(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/time_entries.json"): {"time_entries": [], "total_count": 0},
        }
    )
    result = await time_entries.list_time_entries(client, cache)
    assert result["total_count"] == 0
    sent = client.calls[-1][2]
    # Only limit + offset, no filter keys.
    assert set(sent.keys()) == {"limit", "offset"}


# ---------------------------------------------------------------------
# update_time_entry
# ---------------------------------------------------------------------


async def test_update_happy_path_partial(cache: SchemaCache) -> None:
    _seed_activities(cache)
    client = FakeClient(
        {
            ("PUT", "/time_entries/100.json"): None,
            ("GET", "/time_entries/100.json"): {
                "time_entry": {"id": 100, "hours": 3.0, "comments": "updated"},
            },
        }
    )
    result = await time_entries.update_time_entry(
        client,
        cache,
        100,
        hours=3.0,
        comments="updated",
    )
    assert result["time_entry"]["hours"] == 3.0
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["time_entry"]
    assert put_payload == {"hours": 3.0, "comments": "updated"}


async def test_update_rejects_invalid_hours(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await time_entries.update_time_entry(client, cache, 100, hours="bad")
    assert result["error"] == "validation_failed"
    assert result["errors"][0]["error"] == "time_entry_hours_invalid"
    assert not any(c[0] == "PUT" for c in client.calls)


async def test_update_returns_nothing_to_update_when_no_fields(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await time_entries.update_time_entry(client, cache, 100)
    assert result["error"] == "nothing_to_update"
    assert client.calls == []


async def test_update_propagates_404(cache: SchemaCache) -> None:
    client = FakeClient(
        errors={
            ("PUT", "/time_entries/999.json"): RedmineAPIError(
                status_code=404, body={"errors": ["Not found"]}
            ),
        }
    )
    result = await time_entries.update_time_entry(client, cache, 999, hours=1.0)
    assert result["error"] == "redmine_api_404"


# ---------------------------------------------------------------------
# delete_time_entry
# ---------------------------------------------------------------------


async def test_delete_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({("DELETE", "/time_entries/100.json"): None})
    result = await time_entries.delete_time_entry(client, cache, 100)
    assert result["deleted"] is True
    assert result["time_entry_id"] == 100
    assert client.calls == [("DELETE", "/time_entries/100.json", None)]


async def test_delete_propagates_404(cache: SchemaCache) -> None:
    client = FakeClient(
        errors={
            ("DELETE", "/time_entries/999.json"): RedmineAPIError(
                status_code=404, body={"errors": ["Not found"]}
            ),
        }
    )
    result = await time_entries.delete_time_entry(client, cache, 999)
    assert result["error"] == "redmine_api_404"
