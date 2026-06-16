"""Unit tests for issue category tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import issue_categories


class FakeClient:
    def __init__(self, responses=None, *, errors=None):
        self._responses = responses or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path, *, params=None):
        self.calls.append(("GET", path, params))
        if ("GET", path) in self._errors:
            raise self._errors[("GET", path)]
        return self._responses.get(("GET", path))

    async def post(self, path, *, json=None):
        self.calls.append(("POST", path, json))
        if ("POST", path) in self._errors:
            raise self._errors[("POST", path)]
        return self._responses.get(("POST", path))

    async def put(self, path, *, json=None):
        self.calls.append(("PUT", path, json))
        if ("PUT", path) in self._errors:
            raise self._errors[("PUT", path)]
        return self._responses.get(("PUT", path))

    async def delete(self, path):
        self.calls.append(("DELETE", path, None))
        if ("DELETE", path) in self._errors:
            raise self._errors[("DELETE", path)]
        return self._responses.get(("DELETE", path))


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


async def test_list_issue_categories(cache):
    client = FakeClient(
        {
            ("GET", "/projects/claudecode/issue_categories.json"): {
                "issue_categories": [{"id": 1, "name": "Backend"}],
            },
        }
    )
    result = await issue_categories.list_issue_categories(
        client,
        cache,
        project_id="claudecode",
    )
    assert result["count"] == 1
    assert result["issue_categories"][0]["name"] == "Backend"


async def test_create_issue_category(cache):
    client = FakeClient(
        {
            ("POST", "/projects/claudecode/issue_categories.json"): {
                "issue_category": {"id": 2, "name": "Frontend"},
            },
        }
    )
    result = await issue_categories.create_issue_category(
        client,
        cache,
        project_id="claudecode",
        name="Frontend",
    )
    assert result["issue_category"]["id"] == 2


async def test_create_issue_category_empty_name(cache):
    client = FakeClient()
    result = await issue_categories.create_issue_category(
        client,
        cache,
        project_id="claudecode",
        name="",
    )
    assert result["error"] == "validation_failed"


async def test_update_issue_category(cache):
    client = FakeClient()
    result = await issue_categories.update_issue_category(
        client,
        cache,
        category_id=1,
        name="New Name",
    )
    assert result["updated"] is True


async def test_update_issue_category_nothing(cache):
    client = FakeClient()
    result = await issue_categories.update_issue_category(
        client,
        cache,
        category_id=1,
    )
    assert result["error"] == "nothing_to_update"


async def test_delete_issue_category(cache):
    client = FakeClient()
    result = await issue_categories.delete_issue_category(
        client,
        cache,
        category_id=1,
    )
    assert result["deleted"] is True


async def test_delete_issue_category_with_reassign(cache):
    client = FakeClient()
    result = await issue_categories.delete_issue_category(
        client,
        cache,
        category_id=1,
        reassign_to_id=2,
    )
    assert result["deleted"] is True
    path = client.calls[-1][1]
    assert "reassign_to_id=2" in path


async def test_delete_issue_category_404(cache):
    client = FakeClient(
        errors={
            ("DELETE", "/issue_categories/999.json"): RedmineAPIError(
                status_code=404,
                body="",
            ),
        }
    )
    result = await issue_categories.delete_issue_category(
        client,
        cache,
        category_id=999,
    )
    assert result["error"] == "category_not_found"
