"""Unit tests for search tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import search


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


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


async def test_search_basic(cache):
    client = FakeClient(
        {
            ("GET", "/search.json"): {
                "results": [{"id": 1, "title": "test", "type": "issue"}],
                "total_count": 1,
            },
        }
    )
    result = await search.search(client, cache, query="test")
    assert result["total_count"] == 1
    assert len(result["results"]) == 1
    assert result["source"] == "api"
    assert client.calls[-1][2]["q"] == "test"


async def test_search_project_scoped(cache):
    client = FakeClient(
        {
            ("GET", "/projects/myproj/search.json"): {
                "results": [],
                "total_count": 0,
            },
        }
    )
    result = await search.search(client, cache, query="test", project="myproj")
    assert result["total_count"] == 0
    assert client.calls[-1][1] == "/projects/myproj/search.json"


async def test_search_empty_query_rejected(cache):
    client = FakeClient()
    result = await search.search(client, cache, query="")
    assert result["error"] == "validation_failed"
    assert len(client.calls) == 0


async def test_search_invalid_resource_type(cache):
    client = FakeClient()
    result = await search.search(client, cache, query="test", resource_types=["bogus"])
    assert result["error"] == "invalid_resource_types"
    assert "bogus" in result["invalid"]


async def test_search_resource_type_filter(cache):
    client = FakeClient(
        {
            ("GET", "/search.json"): {"results": [], "total_count": 0},
        }
    )
    await search.search(client, cache, query="test", resource_types=["issues", "wiki_pages"])
    params = client.calls[-1][2]
    assert params["issues"] == 1
    assert params["wiki_pages"] == 1


async def test_search_titles_only(cache):
    client = FakeClient(
        {
            ("GET", "/search.json"): {"results": [], "total_count": 0},
        }
    )
    await search.search(client, cache, query="test", titles_only=True)
    assert client.calls[-1][2]["titles_only"] == 1


async def test_search_invalid_attachments_mode(cache):
    client = FakeClient()
    result = await search.search(client, cache, query="test", attachments="bad")
    assert result["error"] == "invalid_attachments_mode"


async def test_search_api_error(cache):
    client = FakeClient(
        errors={
            ("GET", "/search.json"): RedmineAPIError(status_code=500, body="error"),
        }
    )
    result = await search.search(client, cache, query="test")
    assert "error" in result


async def test_search_non_dict_response(cache):
    client = FakeClient({("GET", "/search.json"): None})
    result = await search.search(client, cache, query="test")
    assert result["results"] == []
    assert result["total_count"] == 0
