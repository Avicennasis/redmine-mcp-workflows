"""Unit tests for read-only reference data tools:
enumerations, roles, issue_statuses, custom_fields, queries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import (
    custom_fields,
    enumerations,
    issue_statuses,
    queries,
    roles,
)


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


# --- enumerations ---


async def test_list_enumerations_priorities(cache):
    client = FakeClient(
        {
            ("GET", "/enumerations/issue_priorities.json"): {
                "issue_priorities": [
                    {"id": 1, "name": "Low", "is_default": False},
                    {"id": 2, "name": "Normal", "is_default": True},
                ],
            },
        }
    )
    result = await enumerations.list_enumerations(
        client,
        cache,
        enum_type="issue_priorities",
    )
    assert result["count"] == 2
    assert result["values"][1]["name"] == "Normal"
    assert result["type"] == "issue_priorities"


async def test_list_enumerations_invalid_type(cache):
    client = FakeClient()
    result = await enumerations.list_enumerations(
        client,
        cache,
        enum_type="bogus",
    )
    assert result["error"] == "invalid_enumeration_type"


# --- roles ---


async def test_list_roles(cache):
    client = FakeClient(
        {
            ("GET", "/roles.json"): {
                "roles": [{"id": 3, "name": "Manager"}, {"id": 4, "name": "Developer"}],
            },
        }
    )
    result = await roles.list_roles(client, cache)
    assert result["count"] == 2


async def test_get_role(cache):
    client = FakeClient(
        {
            ("GET", "/roles/3.json"): {
                "role": {"id": 3, "name": "Manager", "permissions": ["add_issues"]},
            },
        }
    )
    result = await roles.get_role(client, cache, role_id=3)
    assert result["role"]["name"] == "Manager"
    assert "add_issues" in result["role"]["permissions"]


async def test_get_role_404(cache):
    client = FakeClient(
        errors={
            ("GET", "/roles/999.json"): RedmineAPIError(status_code=404, body=""),
        }
    )
    result = await roles.get_role(client, cache, role_id=999)
    assert result["error"] == "role_not_found"


# --- issue_statuses ---


async def test_list_issue_statuses(cache):
    client = FakeClient(
        {
            ("GET", "/issue_statuses.json"): {
                "issue_statuses": [
                    {"id": 1, "name": "New", "is_closed": False},
                    {"id": 5, "name": "Closed", "is_closed": True},
                ],
            },
        }
    )
    result = await issue_statuses.list_issue_statuses(client, cache)
    assert result["count"] == 2
    assert result["issue_statuses"][1]["is_closed"] is True


# --- custom_fields ---


async def test_list_custom_fields(cache):
    client = FakeClient(
        {
            ("GET", "/custom_fields.json"): {
                "custom_fields": [
                    {"id": 1, "name": "Difficulty", "field_format": "list"},
                ],
            },
        }
    )
    result = await custom_fields.list_custom_fields(client, cache)
    assert result["count"] == 1
    assert result["custom_fields"][0]["name"] == "Difficulty"


async def test_list_custom_fields_403(cache):
    client = FakeClient(
        errors={
            ("GET", "/custom_fields.json"): RedmineAPIError(
                status_code=403,
                body="Forbidden",
            ),
        }
    )
    result = await custom_fields.list_custom_fields(client, cache)
    assert "error" in result


# --- queries ---


async def test_list_queries_global(cache):
    client = FakeClient(
        {
            ("GET", "/queries.json"): {
                "queries": [{"id": 1, "name": "My open bugs", "is_public": True}],
            },
        }
    )
    result = await queries.list_queries(client, cache)
    assert result["count"] == 1
    assert result["queries"][0]["name"] == "My open bugs"


async def test_list_queries_project_scoped(cache):
    client = FakeClient(
        {
            ("GET", "/projects/claudecode/queries.json"): {
                "queries": [],
            },
        }
    )
    result = await queries.list_queries(client, cache, project_id="claudecode")
    assert result["count"] == 0
    assert client.calls[-1][1] == "/projects/claudecode/queries.json"
