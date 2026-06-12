"""Unit tests for project lifecycle tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import projects


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


async def test_create_project(cache):
    client = FakeClient({
        ("POST", "/projects.json"): {
            "project": {"id": 99, "name": "Test", "identifier": "test"},
        },
    })
    result = await projects.create_project(
        client, cache, name="Test", identifier="test",
    )
    assert result["project"]["id"] == 99
    assert result["source"] == "api"


async def test_create_project_empty_name(cache):
    client = FakeClient()
    result = await projects.create_project(client, cache, name="", identifier="test")
    assert result["error"] == "validation_failed"


async def test_create_project_empty_identifier(cache):
    client = FakeClient()
    result = await projects.create_project(client, cache, name="Test", identifier="")
    assert result["error"] == "validation_failed"


async def test_update_project(cache):
    client = FakeClient({
        ("GET", "/projects/99.json"): {
            "project": {"id": 99, "name": "Updated"},
        },
    })
    result = await projects.update_project(
        client, cache, project_id=99, name="Updated",
    )
    assert result["project"]["name"] == "Updated"


async def test_update_project_nothing(cache):
    client = FakeClient()
    result = await projects.update_project(client, cache, project_id=99)
    assert result["error"] == "nothing_to_update"


async def test_delete_project(cache):
    client = FakeClient()
    result = await projects.delete_project(client, cache, project_id=99)
    assert result["deleted"] is True
    assert result["project_id"] == 99


async def test_delete_project_404(cache):
    client = FakeClient(errors={
        ("DELETE", "/projects/999.json"): RedmineAPIError(status_code=404, body=""),
    })
    result = await projects.delete_project(client, cache, project_id=999)
    assert result["error"] == "project_not_found"


async def test_archive_project(cache):
    client = FakeClient()
    result = await projects.archive_project(client, cache, project_id=99)
    assert result["archived"] is True


async def test_unarchive_project(cache):
    client = FakeClient()
    result = await projects.unarchive_project(client, cache, project_id=99)
    assert result["unarchived"] is True
