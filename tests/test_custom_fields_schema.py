"""Unit tests for the custom_fields schema fetcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.schema.custom_fields import (
    get_custom_field_by_name,
    refresh_custom_fields,
)

# The canned response mirrors the live Redmine 6.x /custom_fields.json shape:
#  - possible_values is a list of {value, label} dicts (not bare strings)
#  - trackers is a list of {id, name} dicts
#  - is_for_all is NOT in the response; presence/absence of "projects"
#    is the only signal for project scope.
_DIFFICULTY_FIELD: dict[str, Any] = {
    "id": 1,
    "name": "Difficulty",
    "description": "Engagement mode.",
    "customized_type": "issue",
    "field_format": "list",
    "is_required": True,
    "is_filter": True,
    "default_value": "Unclassified",
    "possible_values": [
        {"value": "Unclassified", "label": "Unclassified"},
        {"value": "Easy", "label": "Easy"},
        {"value": "Normal", "label": "Normal"},
        {"value": "Hard", "label": "Hard"},
    ],
    "trackers": [
        {"id": 1, "name": "Bug"},
        {"id": 2, "name": "Feature"},
        {"id": 3, "name": "Support"},
        {"id": 4, "name": "Prompt"},
        {"id": 5, "name": "NewApp"},
    ],
    "roles": [],
}

_SCOPED_FIELD: dict[str, Any] = {
    "id": 7,
    "name": "Customer",
    "customized_type": "issue",
    "field_format": "string",
    "is_required": False,
    "default_value": None,
    "possible_values": [],
    "trackers": [{"id": 1, "name": "Bug"}],
    # Has a projects array → NOT for_all_projects.
    "projects": [{"id": 42, "name": "Inbox", "identifier": "inbox"}],
}

_NON_ISSUE_FIELD: dict[str, Any] = {
    "id": 99,
    "name": "ProjectColor",
    "customized_type": "project",  # Should be filtered out.
    "field_format": "string",
}


class FakeClient:
    """Minimal stand-in for RedmineClient."""

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path))
        return self._responses[("GET", path)]


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


@pytest.fixture
def client() -> FakeClient:
    return FakeClient(
        {
            ("GET", "/custom_fields.json"): {
                "custom_fields": [_DIFFICULTY_FIELD, _SCOPED_FIELD, _NON_ISSUE_FIELD],
            },
        }
    )


# ---- refresh_custom_fields ----------------------------------------------


@pytest.mark.asyncio
async def test_refresh_custom_fields_populates_cache(client, cache) -> None:
    await refresh_custom_fields(client, cache)
    diff = cache.get_custom_field_by_name("Difficulty")
    assert diff is not None
    assert diff["id"] == 1
    assert diff["is_required"] is True
    assert diff["default_value"] == "Unclassified"
    assert diff["format_kind"] == "list"
    assert set(diff["possible_values"]) == {"Unclassified", "Easy", "Normal", "Hard"}
    assert diff["applicable_tracker_ids"] == [1, 2, 3, 4, 5]
    assert diff["for_all_projects"] is True  # no "projects" key in payload


@pytest.mark.asyncio
async def test_refresh_marks_scoped_field_not_for_all(client, cache) -> None:
    """A field with a 'projects' array is NOT for_all_projects."""
    await refresh_custom_fields(client, cache)
    customer = cache.get_custom_field_by_name("Customer")
    assert customer is not None
    assert customer["for_all_projects"] is False


@pytest.mark.asyncio
async def test_refresh_skips_non_issue_fields(client, cache) -> None:
    """Fields with customized_type != 'issue' are filtered out."""
    await refresh_custom_fields(client, cache)
    assert cache.get_custom_field_by_name("ProjectColor") is None


@pytest.mark.asyncio
async def test_refresh_is_idempotent(client, cache) -> None:
    await refresh_custom_fields(client, cache)
    await refresh_custom_fields(client, cache)
    # Still exactly one row per field.
    fields = cache.list_custom_fields()
    names = [f["name"] for f in fields]
    assert names.count("Difficulty") == 1
    assert names.count("Customer") == 1


@pytest.mark.asyncio
async def test_refresh_handles_empty_payload(cache) -> None:
    client = FakeClient({("GET", "/custom_fields.json"): {"custom_fields": []}})
    await refresh_custom_fields(client, cache)
    assert cache.list_custom_fields() == []


@pytest.mark.asyncio
async def test_refresh_handles_non_dict_payload(cache) -> None:
    """403 / weird responses come through as non-dict — should not crash."""
    client = FakeClient({("GET", "/custom_fields.json"): None})
    await refresh_custom_fields(client, cache)
    assert cache.list_custom_fields() == []


# ---- get_custom_field_by_name (with lazy-load) --------------------------


@pytest.mark.asyncio
async def test_get_by_name_lazy_loads_on_empty_cache(client, cache) -> None:
    """Calling get_custom_field_by_name on an empty cache should refresh first."""
    assert cache.list_custom_fields() == []
    diff = await get_custom_field_by_name(client, cache, "Difficulty")
    assert diff is not None
    assert diff["name"] == "Difficulty"
    # Should have made exactly one network call.
    assert client.calls == [("GET", "/custom_fields.json")]


@pytest.mark.asyncio
async def test_get_by_name_does_not_refetch_when_present(client, cache) -> None:
    await refresh_custom_fields(client, cache)
    client.calls.clear()
    diff = await get_custom_field_by_name(client, cache, "Difficulty")
    assert diff is not None
    # No additional fetch.
    assert client.calls == []


@pytest.mark.asyncio
async def test_get_by_name_returns_none_when_field_absent(cache) -> None:
    client = FakeClient({("GET", "/custom_fields.json"): {"custom_fields": []}})
    result = await get_custom_field_by_name(client, cache, "Difficulty")
    assert result is None
