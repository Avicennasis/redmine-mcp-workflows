"""Unit tests for the schema fetcher modules.

Uses a fake client (a class with the same async surface as RedmineClient)
to avoid network and exercise the cache + reshape logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.schema import project as project_schema
from redmine_mcp.schema import tracker as tracker_schema
from redmine_mcp.schema import workflow as workflow_module


class FakeClient:
    """Minimal stand-in for RedmineClient.

    Returns canned responses keyed by ``(method, path)``. Callers append
    expected hits and the test asserts on the call log afterward.
    """

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        return self._responses[("GET", path)]


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


# ---- tracker_schema.fetch_all_trackers ---------------------------------


@pytest.mark.asyncio
async def test_fetch_all_trackers_caches(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/trackers.json"): {
                "trackers": [
                    {"id": 1, "name": "Bug"},
                    {"id": 2, "name": "Feature"},
                ]
            },
        }
    )
    result = await tracker_schema.fetch_all_trackers(client, cache)
    assert len(result) == 2
    assert cache.get_tracker(1) is not None
    assert cache.get_tracker(2) is not None


# ---- tracker_schema.describe_tracker -----------------------------------


@pytest.mark.asyncio
async def test_describe_tracker_lazy_populates_and_enriches(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/trackers.json"): {
                "trackers": [{"id": 1, "name": "Bug", "default_status": {"id": 1, "name": "New"}}]
            },
            ("GET", "/issue_statuses.json"): {
                "issue_statuses": [
                    {"id": 1, "name": "New"},
                    {"id": 2, "name": "In Progress"},
                ]
            },
            ("GET", "/enumerations/issue_priorities.json"): {
                "issue_priorities": [
                    {"id": 2, "name": "Normal"},
                ]
            },
            ("GET", "/roles.json"): {"roles": [{"id": 4, "name": "Developer"}]},
            ("GET", "/enumerations/time_entry_activities.json"): {
                "time_entry_activities": [
                    {"id": 9, "name": "Development"},
                ]
            },
        }
    )

    result = await tracker_schema.describe_tracker(client, cache, "Bug")

    assert result["id"] == 1
    assert result["name"] == "Bug"
    assert result["default_status"] == {"id": 1, "name": "New"}
    assert len(result["available_statuses"]) == 2
    assert len(result["available_priorities"]) == 1
    assert "observation_note" in result
    assert "observed_workflow" in result


@pytest.mark.asyncio
async def test_describe_tracker_includes_observations(cache: SchemaCache) -> None:
    cache.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
    cache.put_meta_json(
        "issue_statuses",
        [{"id": 1, "name": "New"}, {"id": 2, "name": "In Progress"}, {"id": 5, "name": "Closed"}],
    )
    cache.put_meta_json("issue_priorities", [])
    cache.put_meta_json("roles", [{"id": 4, "name": "Developer"}])
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=5, outcome="disallowed"
    )

    client = FakeClient({})  # no calls expected — everything cached
    result = await tracker_schema.describe_tracker(client, cache, "Bug")
    workflow_view = result["observed_workflow"]
    assert "4" in workflow_view  # keyed by role_id (string)
    transitions = workflow_view["4"]["New"]
    assert "In Progress" in transitions["allowed_next"]
    assert "Closed" in transitions["disallowed_next"]


@pytest.mark.asyncio
async def test_describe_tracker_includes_custom_fields(cache: SchemaCache) -> None:
    """describe_tracker output should include applicable custom fields."""
    cache.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
    cache.put_meta_json("issue_statuses", [{"id": 1, "name": "New"}])
    cache.put_meta_json("issue_priorities", [])
    cache.put_meta_json("roles", [])
    client = FakeClient(
        {
            ("GET", "/custom_fields.json"): {
                "custom_fields": [
                    {
                        "id": 1,
                        "name": "Difficulty",
                        "customized_type": "issue",
                        "field_format": "list",
                        "is_required": True,
                        "default_value": "Unclassified",
                        "possible_values": [
                            {"value": "Unclassified", "label": "Unclassified"},
                            {"value": "Easy", "label": "Easy"},
                        ],
                        "trackers": [{"id": 1, "name": "Bug"}, {"id": 2, "name": "Feature"}],
                    },
                ]
            },
        }
    )

    result = await tracker_schema.describe_tracker(client, cache, "Bug")

    assert "custom_fields" in result
    diff = next((f for f in result["custom_fields"] if f["name"] == "Difficulty"), None)
    assert diff is not None
    assert diff["is_required"] is True
    assert diff["default_value"] == "Unclassified"
    assert set(diff["possible_values"]) == {"Unclassified", "Easy"}


@pytest.mark.asyncio
async def test_describe_tracker_filters_custom_fields_by_tracker(cache: SchemaCache) -> None:
    """A field whose trackers do not include this tracker should be excluded."""
    cache.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
    cache.put_tracker(2, "Feature", {"id": 2, "name": "Feature"})
    cache.put_meta_json("issue_statuses", [])
    cache.put_meta_json("issue_priorities", [])
    cache.put_meta_json("roles", [])
    # Pre-seed cache so describe_tracker does NOT call /custom_fields.json
    cache.put_custom_field(
        field_id=1,
        name="Difficulty",
        format_kind="list",
        is_required=True,
        default_value="Unclassified",
        possible_values=["Unclassified", "Easy"],
        applicable_tracker_ids=[1],  # Only Bug, not Feature
        for_all_projects=True,
    )
    client = FakeClient({})  # no calls expected — fully cached

    result_bug = await tracker_schema.describe_tracker(client, cache, "Bug")
    assert any(f["name"] == "Difficulty" for f in result_bug["custom_fields"])

    result_feat = await tracker_schema.describe_tracker(client, cache, "Feature")
    assert not any(f["name"] == "Difficulty" for f in result_feat["custom_fields"])


@pytest.mark.asyncio
async def test_describe_tracker_swallows_custom_fields_fetch_errors(cache: SchemaCache) -> None:
    """If /custom_fields.json raises (e.g. 403), describe_tracker still returns."""
    cache.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
    cache.put_meta_json("issue_statuses", [])
    cache.put_meta_json("issue_priorities", [])
    cache.put_meta_json("roles", [])

    class ExplodingClient(FakeClient):
        async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
            if path == "/custom_fields.json":
                raise RuntimeError("403 admin-only")
            return await super().get(path, params=params)

    client = ExplodingClient({})
    result = await tracker_schema.describe_tracker(client, cache, "Bug")
    # Soft-fail: empty list, no crash.
    assert result.get("custom_fields") == []


@pytest.mark.asyncio
async def test_describe_tracker_returns_error_for_unknown(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/trackers.json"): {"trackers": [{"id": 1, "name": "Bug"}]},
        }
    )
    result = await tracker_schema.describe_tracker(client, cache, "Nonexistent")
    assert result.get("error") == "tracker_not_found"
    assert "Bug" in result["available"]


# ---- project_schema.describe_project -----------------------------------


@pytest.mark.asyncio
async def test_describe_project_caches_on_first_call(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/projects/claudecode.json"): {
                "project": {
                    "id": 15,
                    "identifier": "claudecode",
                    "name": "ClaudeCode",
                    "trackers": [{"id": 1, "name": "Bug"}],
                }
            },
        }
    )
    first = await project_schema.describe_project(client, cache, "claudecode")
    assert first["source"] == "api"

    second = await project_schema.describe_project(client, cache, "claudecode")
    assert second["source"] == "cache"
    # Only the first call should have hit HTTP.
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_describe_project_handles_404_shaped_payload(cache: SchemaCache) -> None:
    client = FakeClient(
        {
            ("GET", "/projects/nope.json"): {"project": None},
        }
    )
    result = await project_schema.describe_project(client, cache, "nope")
    assert result.get("error") == "project_not_found"


# ---- project_schema.list_projects --------------------------------------


@pytest.mark.asyncio
async def test_list_projects_paginates_and_returns_total() -> None:
    client = FakeClient(
        {
            ("GET", "/projects.json"): {
                "projects": [
                    {"id": 1, "identifier": "alpha", "name": "Alpha"},
                    {"id": 2, "identifier": "beta", "name": "Beta"},
                ],
                "total_count": 59,
                "limit": 25,
                "offset": 0,
            },
        }
    )
    result = await project_schema.list_projects(client, limit=25, offset=0)
    assert result["total_count"] == 59
    assert len(result["projects"]) == 2
    assert result["filtered_locally"] is False


@pytest.mark.asyncio
async def test_list_projects_query_filters_client_side() -> None:
    client = FakeClient(
        {
            ("GET", "/projects.json"): {
                "projects": [
                    {"id": 1, "identifier": "alpha", "name": "Alpha"},
                    {"id": 2, "identifier": "beta", "name": "Beta"},
                    {"id": 3, "identifier": "claudecode", "name": "ClaudeCode"},
                ],
                "total_count": 3,
            },
        }
    )
    result = await project_schema.list_projects(client, query="claud")
    assert result["filtered_locally"] is True
    assert len(result["projects"]) == 1
    assert result["projects"][0]["identifier"] == "claudecode"


@pytest.mark.asyncio
async def test_list_projects_query_walks_all_pages() -> None:
    """Regression for infra#2579 Finding 3: query must catch matches on
    later pages, not just the first page of results. Previously the filter
    only saw the limit-bounded first page, so e.g. query="mcp" with default
    limit=25 missed `kronos-mcp` (id 30) even though it existed."""

    class PagingFakeClient:
        def __init__(self) -> None:
            # Page 0 (offset 0, limit 100): 100 distractor projects
            # Page 1 (offset 100, limit 100): the match lives on this page
            page0 = [
                {"id": i, "identifier": f"distract-{i}", "name": f"Distract {i}"}
                for i in range(1, 101)
            ]
            page1 = [
                {"id": 101, "identifier": "kronos-mcp", "name": "kronos-mcp"},
                {"id": 102, "identifier": "other", "name": "Other"},
            ]
            self._pages = [page0, page1]
            self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

        async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
            self.calls.append(("GET", path, params))
            offset = (params or {}).get("offset", 0)
            page_idx = offset // 100
            projects = self._pages[page_idx] if page_idx < len(self._pages) else []
            return {"projects": projects, "total_count": 102}

    client = PagingFakeClient()
    result = await project_schema.list_projects(client, query="mcp")  # type: ignore[arg-type]
    assert result["filtered_locally"] is True
    assert result["total_count"] == 1
    assert len(result["projects"]) == 1
    assert result["projects"][0]["identifier"] == "kronos-mcp"
    # Verified that we actually walked both pages.
    offsets_called = [c[2]["offset"] for c in client.calls if c[2] and "offset" in c[2]]
    assert 0 in offsets_called and 100 in offsets_called


@pytest.mark.asyncio
async def test_list_projects_query_total_reflects_filtered_count() -> None:
    """total_count when filtered = matches across all pages, not server total."""
    client = FakeClient(
        {
            ("GET", "/projects.json"): {
                "projects": [
                    {"id": 1, "identifier": "alpha-mcp", "name": "Alpha-MCP"},
                    {"id": 2, "identifier": "beta", "name": "Beta"},
                    {"id": 3, "identifier": "gamma-mcp", "name": "Gamma-MCP"},
                ],
                "total_count": 50,  # server reports many more total
            },
        }
    )
    result = await project_schema.list_projects(client, query="mcp")
    assert result["filtered_locally"] is True
    # Filtered total = 2 (alpha-mcp + gamma-mcp), not 50.
    assert result["total_count"] == 2


# ---- workflow_module.role_ids_for_project -------------------------------


def test_role_ids_for_project_extracts_per_project() -> None:
    user = {
        "id": 1,
        "memberships": [
            {"project": {"id": 10}, "roles": [{"id": 3}, {"id": 4}]},
            {"project": {"id": 11}, "roles": [{"id": 4}]},
        ],
    }
    assert workflow_module.role_ids_for_project(user, 10) == [3, 4]
    assert workflow_module.role_ids_for_project(user, 11) == [4]
    assert workflow_module.role_ids_for_project(user, 999) == []


def test_role_ids_for_project_handles_empty_memberships() -> None:
    assert workflow_module.role_ids_for_project({"memberships": []}, 1) == []
    assert workflow_module.role_ids_for_project({}, 1) == []


def test_record_outcome_uses_role_zero_when_no_roles(cache: SchemaCache) -> None:
    workflow_module.record_outcome(
        cache,
        tracker_id=1,
        role_ids=[],
        from_status_id=1,
        to_status_id=2,
        outcome="allowed",
    )
    obs = cache.get_workflow_observation(tracker_id=1, role_id=0, from_status_id=1, to_status_id=2)
    assert obs is not None
    assert obs["outcome"] == "allowed"


def test_record_outcome_records_per_role(cache: SchemaCache) -> None:
    workflow_module.record_outcome(
        cache,
        tracker_id=1,
        role_ids=[3, 4],
        from_status_id=1,
        to_status_id=2,
        outcome="disallowed",
        error_text="Status is not allowed",
    )
    for role_id in (3, 4):
        obs = cache.get_workflow_observation(
            tracker_id=1, role_id=role_id, from_status_id=1, to_status_id=2
        )
        assert obs is not None
        assert obs["outcome"] == "disallowed"
        assert obs["last_error_text"] == "Status is not allowed"
