"""Unit tests for redmine_copy_issue (#40871)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.tools import copy_issue as copy_issue_module


class FakeClient:
    def __init__(
        self,
        responses: dict[tuple[str, str], Any] | None = None,
        *,
        errors: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, str, Any]] = []
        self._consumed: dict[tuple[str, str], int] = {}

    def _next(self, key: tuple[str, str]) -> Any:
        value = self._responses.get(key)
        if isinstance(value, list):
            idx = self._consumed.get(key, 0)
            chosen = value[idx] if idx < len(value) else value[-1]
            self._consumed[key] = idx + 1
            return chosen
        return value

    async def get(self, path: str, *, params: dict | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if ("GET", path) in self._errors:
            raise self._errors[("GET", path)]
        return self._next(("GET", path))

    async def post(self, path: str, *, json: Any = None) -> Any:
        self.calls.append(("POST", path, json))
        if ("POST", path) in self._errors:
            raise self._errors[("POST", path)]
        return self._next(("POST", path))


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


def _source(**overrides: Any) -> dict[str, Any]:
    issue = {
        "id": 42,
        "subject": "Original",
        "project": {"id": 15, "name": "ClaudeCode"},
        "tracker": {"id": 1, "name": "Bug"},
        "priority": {"id": 2, "name": "Normal"},
        "assigned_to": {"id": 7, "name": "Ada"},
        "description": "body",
        "due_date": "2026-01-01",
        "start_date": "2025-12-01",
        "done_ratio": 30,
        "custom_fields": [{"id": 1, "name": "Difficulty", "value": "Easy"}],
        "children": [{"id": 43, "subject": "Child"}],
        "watchers": [{"id": 7, "name": "Ada"}],
        "relations": [{"id": 1, "issue_id": 42, "issue_to_id": 99, "relation_type": "relates"}],
    }
    issue.update(overrides)
    return {"issue": issue}


def _posted(client: FakeClient, path: str = "/issues.json") -> dict[str, Any]:
    posts = [c for c in client.calls if c[0] == "POST" and c[1] == path]
    assert posts, f"no POST {path}"
    return posts[0][2]["issue"] if path == "/issues.json" else posts[0][2]


async def test_copy_inherits_fields_and_prepends_subject(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/issues/42.json"): _source(),
            ("POST", "/issues.json"): {"issue": {"id": 999, "subject": "Copy of Original"}},
        }
    )
    result = await copy_issue_module.copy_issue(client, cache, 42)
    assert result["issue"]["id"] == 999
    assert result["copied"] == {"watchers": 0, "relations": 0, "subtasks": 0}
    body = _posted(client)
    assert body["project_id"] == 15
    assert body["tracker_id"] == 1
    assert body["subject"] == "Copy of Original"
    assert body["description"] == "body"
    assert body["priority_id"] == 2
    assert body["assigned_to_id"] == 7
    assert body["due_date"] == "2026-01-01"
    assert body["start_date"] == "2025-12-01"
    assert body["done_ratio"] == 30
    assert body["custom_fields"] == [{"id": 1, "value": "Easy"}]
    assert "parent_issue_id" not in body


async def test_copy_overrides(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/issues/42.json"): _source(),
            ("POST", "/issues.json"): {"issue": {"id": 999}},
        }
    )
    await copy_issue_module.copy_issue(
        client,
        cache,
        42,
        project=15,
        tracker=2,
        subject="Renamed",
        priority=3,
        description="new body",
        custom_fields=[{"id": 1, "value": "Hard"}],
    )
    body = _posted(client)
    assert body["tracker_id"] == 2
    assert body["subject"] == "Renamed"
    assert body["description"] == "new body"
    assert body["priority_id"] == 3
    assert body["custom_fields"] == [{"id": 1, "value": "Hard"}]


async def test_copy_watchers_and_relations(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/issues/42.json"): _source(),
            ("POST", "/issues.json"): {"issue": {"id": 999}},
            ("POST", "/issues/999/watchers.json"): {},
            ("POST", "/issues/999/relations.json"): {},
        }
    )
    result = await copy_issue_module.copy_issue(
        client, cache, 42, copy_watchers=True, copy_relations=True
    )
    assert result["copied"]["watchers"] == 1
    assert result["copied"]["relations"] == 1
    watcher_posts = [c for c in client.calls if c[1] == "/issues/999/watchers.json"]
    assert watcher_posts[0][2] == {"user_id": 7}
    relation_posts = [c for c in client.calls if c[1] == "/issues/999/relations.json"]
    # The relation's OTHER end is 99, not the source id 42.
    assert relation_posts[0][2] == {"relation": {"issue_to_id": 99, "relation_type": "relates"}}


async def test_copy_subtasks_recurses_with_parent(cache: SchemaCache) -> None:
    child = _source(id=43, subject="Child", children=[])
    client = FakeClient(
        {
            ("GET", "/issues/42.json"): _source(),
            ("GET", "/issues/43.json"): child,
            ("POST", "/issues.json"): [{"issue": {"id": 999}}, {"issue": {"id": 1000}}],
        }
    )
    result = await copy_issue_module.copy_issue(client, cache, 42, copy_subtasks=True)
    assert result["copied"]["subtasks"] == 1
    posts = [c[2]["issue"] for c in client.calls if c[0] == "POST" and c[1] == "/issues.json"]
    assert len(posts) == 2
    assert posts[1]["parent_issue_id"] == 999
    assert posts[1]["subject"] == "Child"


async def test_copy_unknown_source_returns_error(cache: SchemaCache) -> None:
    client = FakeClient({("GET", "/issues/42.json"): {"error": "issue_not_found"}})
    # get_issue returns the payload as-is when it carries an error key
    result = await copy_issue_module.copy_issue(client, cache, 42)
    assert "error" in result
