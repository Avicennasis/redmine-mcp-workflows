"""Unit tests for v0.5 #2390 news tool (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import news


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


# ---------------------------------------------------------------------
# list_news — global feed
# ---------------------------------------------------------------------


async def test_list_news_global_feed_default_pagination(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/news.json"): {
            "news": [
                {"id": 1, "title": "Welcome", "summary": "..."},
                {"id": 2, "title": "Update", "summary": "..."},
            ],
            "total_count": 2,
            "limit": 25,
            "offset": 0,
        },
    })
    result = await news.list_news(client, cache)
    assert result["total_count"] == 2
    assert len(result["news"]) == 2
    assert result["news"][0]["title"] == "Welcome"
    assert result["source"] == "api"
    # Verify path + default pagination
    method, path, params = client.calls[-1]
    assert (method, path) == ("GET", "/news.json")
    assert params == {"limit": 25, "offset": 0}


async def test_list_news_propagates_pagination_args(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/news.json"): {"news": [], "total_count": 0, "limit": 5, "offset": 10},
    })
    result = await news.list_news(client, cache, limit=5, offset=10)
    assert result["limit"] == 5
    assert result["offset"] == 10
    assert client.calls[-1][2] == {"limit": 5, "offset": 10}


# ---------------------------------------------------------------------
# list_news — project-scoped
# ---------------------------------------------------------------------


async def test_list_news_project_by_slug_uses_scoped_path(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/projects/claudecode/news.json"): {
            "news": [{"id": 7, "title": "redmine-mcp v0.4 shipped"}],
            "total_count": 1,
        },
    })
    result = await news.list_news(client, cache, project="claudecode")
    assert result["total_count"] == 1
    assert result["news"][0]["title"] == "redmine-mcp v0.4 shipped"


async def test_list_news_project_by_id_uses_scoped_path(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/projects/15/news.json"): {"news": [], "total_count": 0},
    })
    await news.list_news(client, cache, project=15)
    assert client.calls[-1][1] == "/projects/15/news.json"


# ---------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------


async def test_list_news_404_propagates_structured_error(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("GET", "/projects/nonexistent/news.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await news.list_news(client, cache, project="nonexistent")
    assert result["error"] == "redmine_api_404"


async def test_list_news_handles_non_dict_response(cache: SchemaCache) -> None:
    """Defensive: if Redmine returns a non-dict (shouldn't happen), don't crash."""
    client = FakeClient({("GET", "/news.json"): None})
    result = await news.list_news(client, cache)
    assert result == {
        "news": [], "total_count": 0, "limit": 25, "offset": 0, "source": "api",
    }
