"""Unit tests for membership tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import memberships


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


async def test_list_memberships(cache):
    client = FakeClient(
        {
            ("GET", "/projects/claudecode/memberships.json"): {
                "memberships": [{"id": 1, "user": {"id": 7}}],
                "total_count": 1,
            },
        }
    )
    result = await memberships.list_memberships(
        client,
        cache,
        project_id="claudecode",
    )
    assert result["total_count"] == 1
    assert result["project_id"] == "claudecode"


async def test_add_membership(cache):
    client = FakeClient(
        {
            ("POST", "/projects/claudecode/memberships.json"): {
                "membership": {"id": 5, "user": {"id": 7}, "roles": [{"id": 3}]},
            },
        }
    )
    result = await memberships.add_membership(
        client,
        cache,
        project_id="claudecode",
        user_id=7,
        role_ids=[3],
    )
    assert result["membership"]["id"] == 5


async def test_add_membership_empty_roles(cache):
    client = FakeClient()
    result = await memberships.add_membership(
        client,
        cache,
        project_id="claudecode",
        user_id=7,
        role_ids=[],
    )
    assert result["error"] == "validation_failed"


async def test_remove_membership(cache):
    client = FakeClient()
    result = await memberships.remove_membership(client, cache, membership_id=5)
    assert result["removed"] is True


async def test_remove_membership_404(cache):
    client = FakeClient(
        errors={
            ("DELETE", "/memberships/999.json"): RedmineAPIError(
                status_code=404,
                body="",
            ),
        }
    )
    result = await memberships.remove_membership(client, cache, membership_id=999)
    assert result["error"] == "membership_not_found"
