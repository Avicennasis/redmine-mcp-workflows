"""Unit tests for v0.2 #2379 watcher tools (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import watchers


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


# ---------------------------------------------------------------------
# add_watcher
# ---------------------------------------------------------------------


async def test_add_watcher_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({("POST", "/issues/42/watchers.json"): None})
    result = await watchers.add_watcher(client, cache, 42, 7)
    assert result == {
        "issue_id": 42, "user_id": 7, "added": True, "source": "api",
    }
    assert client.calls == [("POST", "/issues/42/watchers.json", {"user_id": 7})]


async def test_add_watcher_propagates_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("POST", "/issues/999/watchers.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await watchers.add_watcher(client, cache, 999, 7)
    assert result["error"] == "redmine_api_404"


# ---------------------------------------------------------------------
# remove_watcher
# ---------------------------------------------------------------------


async def test_remove_watcher_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({("DELETE", "/issues/42/watchers/7.json"): None})
    result = await watchers.remove_watcher(client, cache, 42, 7)
    assert result == {
        "issue_id": 42, "user_id": 7, "removed": True, "source": "api",
    }


async def test_remove_watcher_404_surfaces_to_caller(cache: SchemaCache) -> None:
    """Removing a non-watcher returns 404; we surface it (don't silently mask)."""
    client = FakeClient(errors={
        ("DELETE", "/issues/42/watchers/99.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await watchers.remove_watcher(client, cache, 42, 99)
    assert result["error"] == "redmine_api_404"


# ---------------------------------------------------------------------
# list_watchers
# ---------------------------------------------------------------------


async def test_list_watchers_returns_watcher_list(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues/42.json"): {
            "issue": {
                "id": 42,
                "subject": "demo",
                "watchers": [
                    {"id": 7, "name": "Léon"},
                    {"id": 8, "name": "Avic"},
                ],
            },
        },
    })
    result = await watchers.list_watchers(client, cache, 42)
    assert result["issue_id"] == 42
    assert len(result["watchers"]) == 2
    assert result["watchers"][0]["name"] == "Léon"
    # Verify include parameter
    sent_params = client.calls[-1][2]
    assert sent_params == {"include": "watchers"}


async def test_list_watchers_empty_when_issue_has_none(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues/42.json"): {"issue": {"id": 42, "subject": "alone"}},
    })
    result = await watchers.list_watchers(client, cache, 42)
    assert result["watchers"] == []


async def test_list_watchers_returns_not_found_when_issue_payload_empty(
    cache: SchemaCache,
) -> None:
    client = FakeClient({("GET", "/issues/99.json"): {"issue": None}})
    result = await watchers.list_watchers(client, cache, 99)
    assert result["error"] == "issue_not_found"
