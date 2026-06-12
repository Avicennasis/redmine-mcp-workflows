"""Unit tests for v0.2 #2380 issue-relation tools (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import relations


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

    async def post(self, path: str, *, json: Any) -> Any:
        self.calls.append(("POST", path, json))
        if ("POST", path) in self._errors:
            raise self._errors[("POST", path)]
        return self._responses.get(("POST", path))

    async def put(self, path: str, *, json: Any) -> Any:
        self.calls.append(("PUT", path, json))
        if ("PUT", path) in self._errors:
            raise self._errors[("PUT", path)]
        return self._responses.get(("PUT", path))

    async def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path, None))
        if ("DELETE", path) in self._errors:
            raise self._errors[("DELETE", path)]
        return self._responses.get(("DELETE", path))


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


# ---------------------------------------------------------------------
# list_relations
# ---------------------------------------------------------------------


async def test_list_relations_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues/42/relations.json"): {
            "relations": [
                {
                    "id": 1,
                    "issue_id": 42,
                    "issue_to_id": 99,
                    "relation_type": "blocks",
                    "delay": None,
                },
                {
                    "id": 2,
                    "issue_id": 42,
                    "issue_to_id": 50,
                    "relation_type": "relates",
                    "delay": None,
                },
            ],
        },
    })
    result = await relations.list_relations(client, cache, 42)
    assert result["issue_id"] == 42
    assert len(result["relations"]) == 2
    assert result["relations"][0]["relation_type"] == "blocks"
    assert result["source"] == "api"


async def test_list_relations_empty(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues/42/relations.json"): {"relations": []},
    })
    result = await relations.list_relations(client, cache, 42)
    assert result["relations"] == []


async def test_list_relations_404_surfaces(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("GET", "/issues/999/relations.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await relations.list_relations(client, cache, 999)
    assert result["error"] == "redmine_api_404"


# ---------------------------------------------------------------------
# add_relation
# ---------------------------------------------------------------------


async def test_add_relation_happy_path_blocks(cache: SchemaCache) -> None:
    client = FakeClient({
        ("POST", "/issues/42/relations.json"): {
            "relation": {
                "id": 7, "issue_id": 42, "issue_to_id": 99,
                "relation_type": "blocks", "delay": None,
            },
        },
    })
    result = await relations.add_relation(
        client, cache, issue_id=42, target_issue_id=99, relation_type="blocks",
    )
    assert result["relation"]["id"] == 7
    assert result["relation"]["relation_type"] == "blocks"
    payload = client.calls[0][2]
    assert payload == {
        "relation": {"issue_to_id": 99, "relation_type": "blocks"},
    }


async def test_add_relation_normalizes_related_to_to_relates(
    cache: SchemaCache,
) -> None:
    """``related_to`` / ``related to`` / ``relates`` all normalize to relates."""
    client = FakeClient({
        ("POST", "/issues/42/relations.json"): {
            "relation": {"id": 1, "relation_type": "relates"},
        },
    })
    await relations.add_relation(
        client, cache, issue_id=42, target_issue_id=99, relation_type="related_to",
    )
    sent = client.calls[0][2]
    assert sent["relation"]["relation_type"] == "relates"


async def test_add_relation_normalizes_blocked_by(cache: SchemaCache) -> None:
    client = FakeClient({
        ("POST", "/issues/42/relations.json"): {
            "relation": {"id": 1, "relation_type": "blocked"},
        },
    })
    await relations.add_relation(
        client, cache, issue_id=42, target_issue_id=99, relation_type="blocked_by",
    )
    assert client.calls[0][2]["relation"]["relation_type"] == "blocked"


async def test_add_relation_rejects_unknown_type(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await relations.add_relation(
        client, cache, issue_id=42, target_issue_id=99, relation_type="bananas",
    )
    assert result["error"] == "relation_type_unknown"
    assert "allowed_types" in result
    assert "blocks" in result["allowed_types"]
    assert client.calls == []


async def test_add_relation_passes_delay_for_precedes(cache: SchemaCache) -> None:
    """``precedes``/``follows`` accept an optional ``delay`` (days)."""
    client = FakeClient({
        ("POST", "/issues/42/relations.json"): {
            "relation": {"id": 1, "relation_type": "precedes", "delay": 5},
        },
    })
    await relations.add_relation(
        client, cache, issue_id=42, target_issue_id=99,
        relation_type="precedes", delay=5,
    )
    payload = client.calls[0][2]["relation"]
    assert payload["delay"] == 5


async def test_add_relation_omits_delay_when_none(cache: SchemaCache) -> None:
    client = FakeClient({
        ("POST", "/issues/42/relations.json"): {"relation": {"id": 1}},
    })
    await relations.add_relation(
        client, cache, issue_id=42, target_issue_id=99, relation_type="blocks",
    )
    payload = client.calls[0][2]["relation"]
    assert "delay" not in payload


async def test_add_relation_propagates_422(cache: SchemaCache) -> None:
    """Cross-project relations may 422 if cross_project_issue_relations is off."""
    client = FakeClient(errors={
        ("POST", "/issues/42/relations.json"): RedmineAPIError(
            status_code=422,
            body={"errors": ["Issue is invalid: cross-project relations not allowed"]},
        ),
    })
    result = await relations.add_relation(
        client, cache, issue_id=42, target_issue_id=99, relation_type="blocks",
    )
    assert result["error"] == "redmine_api_422"


# ---------------------------------------------------------------------
# remove_relation
# ---------------------------------------------------------------------


async def test_remove_relation_happy_path(cache: SchemaCache) -> None:
    """DELETE goes to /relations/{relation_id}.json (top-level, not nested)."""
    client = FakeClient({("DELETE", "/relations/7.json"): None})
    result = await relations.remove_relation(client, cache, relation_id=7)
    assert result == {"relation_id": 7, "removed": True, "source": "api"}


async def test_remove_relation_404_surfaces(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("DELETE", "/relations/999.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await relations.remove_relation(client, cache, relation_id=999)
    assert result["error"] == "redmine_api_404"


# ---------------------------------------------------------------------
# set_parent_issue
# ---------------------------------------------------------------------


async def test_set_parent_issue_happy_path(cache: SchemaCache) -> None:
    """Parent is a PUT to /issues/{id}.json with parent_issue_id."""
    client = FakeClient({
        ("PUT", "/issues/42.json"): None,
    })
    result = await relations.set_parent_issue(
        client, cache, issue_id=42, parent_issue_id=99,
    )
    assert result == {
        "issue_id": 42,
        "parent_issue_id": 99,
        "updated": True,
        "source": "api",
    }
    payload = client.calls[0][2]
    assert payload == {"issue": {"parent_issue_id": 99}}


async def test_set_parent_issue_unparent_with_zero(cache: SchemaCache) -> None:
    """Passing parent_issue_id=0 (or None) clears the parent."""
    client = FakeClient({("PUT", "/issues/42.json"): None})
    result = await relations.set_parent_issue(
        client, cache, issue_id=42, parent_issue_id=0,
    )
    assert result["parent_issue_id"] == 0
    payload = client.calls[0][2]
    # Redmine accepts an empty string to clear parent.
    assert payload["issue"]["parent_issue_id"] == ""


async def test_set_parent_issue_404_surfaces(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("PUT", "/issues/999.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await relations.set_parent_issue(
        client, cache, issue_id=999, parent_issue_id=42,
    )
    assert result["error"] == "redmine_api_404"
