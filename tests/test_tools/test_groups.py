"""Unit tests for group management tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import groups


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


async def test_list_groups(cache):
    client = FakeClient(
        {
            ("GET", "/groups.json"): {
                "groups": [{"id": 1, "name": "Admins"}],
            },
        }
    )
    result = await groups.list_groups(client, cache)
    assert result["count"] == 1
    assert result["groups"][0]["name"] == "Admins"


async def test_get_group(cache):
    client = FakeClient(
        {
            ("GET", "/groups/1.json"): {
                "group": {"id": 1, "name": "Admins"},
            },
        }
    )
    result = await groups.get_group(client, cache, group_id=1)
    assert result["group"]["name"] == "Admins"


async def test_get_group_404(cache):
    client = FakeClient(
        errors={
            ("GET", "/groups/999.json"): RedmineAPIError(status_code=404, body=""),
        }
    )
    result = await groups.get_group(client, cache, group_id=999)
    assert result["error"] == "group_not_found"


async def test_create_group(cache):
    client = FakeClient(
        {
            ("POST", "/groups.json"): {
                "group": {"id": 2, "name": "Developers"},
            },
        }
    )
    result = await groups.create_group(client, cache, name="Developers")
    assert result["group"]["id"] == 2


async def test_create_group_empty_name(cache):
    client = FakeClient()
    result = await groups.create_group(client, cache, name="")
    assert result["error"] == "validation_failed"


async def test_delete_group(cache):
    client = FakeClient()
    result = await groups.delete_group(client, cache, group_id=1)
    assert result["deleted"] is True


async def test_add_group_user(cache):
    client = FakeClient()
    result = await groups.add_group_user(client, cache, group_id=1, user_id=7)
    assert result["added"] is True
    assert result["group_id"] == 1
    assert result["user_id"] == 7


async def test_remove_group_user(cache):
    client = FakeClient()
    result = await groups.remove_group_user(client, cache, group_id=1, user_id=7)
    assert result["removed"] is True
