"""Unit tests for v0.5 #2390 forum/board tools (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import forums


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
# list_messages
# ---------------------------------------------------------------------


async def test_list_messages_happy_path(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/boards/3/messages.json"): {
                "messages": [
                    {"id": 11, "subject": "Welcome", "content": "..."},
                    {"id": 12, "subject": "Roadmap", "content": "..."},
                ],
                "total_count": 2,
                "limit": 25,
                "offset": 0,
            },
        }
    )
    result = await forums.list_messages(client, cache, board_id=3)
    assert result["board_id"] == 3
    assert result["total_count"] == 2
    assert len(result["messages"]) == 2
    assert result["messages"][0]["subject"] == "Welcome"
    assert result["source"] == "api"
    # Verify path + pagination
    method, path, params = client.calls[-1]
    assert (method, path) == ("GET", "/boards/3/messages.json")
    assert params == {"limit": 25, "offset": 0}


async def test_list_messages_propagates_pagination(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/boards/3/messages.json"): {
                "messages": [],
                "total_count": 0,
                "limit": 5,
                "offset": 50,
            },
        }
    )
    await forums.list_messages(client, cache, board_id=3, limit=5, offset=50)
    assert client.calls[-1][2] == {"limit": 5, "offset": 50}


# ---------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------


async def test_list_messages_404_when_board_missing_or_module_disabled(
    cache: SchemaCache,
) -> None:
    """Common failure: boards module not enabled on the parent project."""
    client = FakeClient(
        errors={
            ("GET", "/boards/999/messages.json"): RedmineAPIError(
                status_code=404,
                body={"errors": ["Not found"]},
            ),
        }
    )
    result = await forums.list_messages(client, cache, board_id=999)
    assert result["error"] == "redmine_api_404"


async def test_list_messages_handles_non_dict_response(cache: SchemaCache) -> None:
    """Defensive: if Redmine returns a non-dict, return empty envelope."""
    client = FakeClient({("GET", "/boards/3/messages.json"): None})
    result = await forums.list_messages(client, cache, board_id=3)
    assert result["messages"] == []
    assert result["board_id"] == 3
    assert result["source"] == "api"
