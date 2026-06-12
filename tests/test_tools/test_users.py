"""Unit tests for users tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import users


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


async def test_get_user_by_id(cache):
    client = FakeClient({
        ("GET", "/users/7.json"): {"user": {"id": 7, "login": "leon"}},
    })
    result = await users.get_user(client, cache, user_id=7)
    assert result["user"]["id"] == 7
    assert result["source"] == "api"


async def test_get_user_current(cache):
    client = FakeClient({
        ("GET", "/users/current.json"): {"user": {"id": 7, "login": "claude"}},
    })
    result = await users.get_user(client, cache, user_id="current")
    assert result["user"]["login"] == "claude"


async def test_get_user_with_include(cache):
    client = FakeClient({
        ("GET", "/users/7.json"): {"user": {"id": 7, "memberships": []}},
    })
    await users.get_user(client, cache, user_id=7, include="memberships,groups")
    assert client.calls[-1][2] == {"include": "memberships,groups"}


async def test_get_user_404(cache):
    client = FakeClient(errors={
        ("GET", "/users/999.json"): RedmineAPIError(status_code=404, body=""),
    })
    result = await users.get_user(client, cache, user_id=999)
    assert result["error"] == "user_not_found"


async def test_list_users_basic(cache):
    client = FakeClient({
        ("GET", "/users.json"): {
            "users": [{"id": 7, "login": "leon"}],
            "total_count": 1,
        },
    })
    result = await users.list_users(client, cache)
    assert result["total_count"] == 1
    assert result["users"][0]["login"] == "leon"


async def test_list_users_with_filters(cache):
    client = FakeClient({
        ("GET", "/users.json"): {"users": [], "total_count": 0},
    })
    await users.list_users(client, cache, name="leon", status=1, group_id=3)
    params = client.calls[-1][2]
    assert params["name"] == "leon"
    assert params["status"] == 1
    assert params["group_id"] == 3


async def test_list_users_403(cache):
    client = FakeClient(errors={
        ("GET", "/users.json"): RedmineAPIError(status_code=403, body="Forbidden"),
    })
    result = await users.list_users(client, cache)
    assert "error" in result
