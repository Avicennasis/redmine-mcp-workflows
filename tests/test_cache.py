"""Unit tests for the SQLite schema cache."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from redmine_mcp.cache.schema_db import SchemaCache


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


def test_apply_migrations_creates_tables(tmp_path: Path) -> None:
    c = SchemaCache(db_path=tmp_path / "fresh.db", ttl_seconds=60)
    try:
        # Round-trip via the public API to confirm tables exist & writable
        c.put_tracker(1, "Bug", {"id": 1, "name": "Bug", "default_status": {}})
        assert c.get_tracker(1) is not None
    finally:
        c.close()


def test_tracker_round_trip(cache: SchemaCache) -> None:
    cache.put_tracker(7, "Feature", {"hello": "world"})
    got = cache.get_tracker(7)
    assert got == {"hello": "world"}


def test_tracker_overwrite(cache: SchemaCache) -> None:
    cache.put_tracker(7, "Feature", {"v": 1})
    cache.put_tracker(7, "Feature", {"v": 2})
    assert cache.get_tracker(7) == {"v": 2}


def test_tracker_ttl_expiry(tmp_path: Path) -> None:
    c = SchemaCache(db_path=tmp_path / "ttl.db", ttl_seconds=0)
    try:
        c.put_tracker(1, "Bug", {"a": 1})
        time.sleep(1.05)  # nudge past the 0-second TTL
        assert c.get_tracker(1) is None
    finally:
        c.close()


def test_list_trackers_excludes_stale(tmp_path: Path) -> None:
    c = SchemaCache(db_path=tmp_path / "list.db", ttl_seconds=0)
    try:
        c.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
        time.sleep(1.05)
        assert c.list_trackers() == []
    finally:
        c.close()


def test_list_trackers_returns_fresh(cache: SchemaCache) -> None:
    cache.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
    cache.put_tracker(2, "Feature", {"id": 2, "name": "Feature"})
    listed = cache.list_trackers()
    assert {t["id"] for t in listed} == {1, 2}


def test_project_round_trip_by_identifier(cache: SchemaCache) -> None:
    cache.put_project(99, "claudecode", {"name": "ClaudeCode"})
    assert cache.get_project("claudecode") == {"name": "ClaudeCode"}


def test_invalidate_all_clears_everything(cache: SchemaCache) -> None:
    cache.put_tracker(1, "Bug", {"x": 1})
    cache.put_project(99, "claudecode", {"y": 2})
    deleted = cache.invalidate("all")
    assert deleted["trackers"] >= 1
    assert deleted["projects"] >= 1
    assert cache.get_tracker(1) is None
    assert cache.get_project("claudecode") is None


def test_invalidate_tracker_by_id(cache: SchemaCache) -> None:
    cache.put_tracker(1, "Bug", {"x": 1})
    cache.put_tracker(2, "Feature", {"x": 2})
    cache.invalidate("tracker:1")
    assert cache.get_tracker(1) is None
    assert cache.get_tracker(2) is not None


def test_invalidate_tracker_by_name(cache: SchemaCache) -> None:
    cache.put_tracker(1, "Bug", {"x": 1})
    cache.invalidate("tracker:Bug")
    assert cache.get_tracker(1) is None


def test_invalidate_project_by_identifier(cache: SchemaCache) -> None:
    cache.put_project(99, "claudecode", {"y": 2})
    cache.invalidate("project:claudecode")
    assert cache.get_project("claudecode") is None


def test_invalidate_unknown_scope_raises(cache: SchemaCache) -> None:
    with pytest.raises(ValueError, match="unknown invalidate scope"):
        cache.invalidate("everything-please")


def test_reconcile_auth_first_call_seeds_fingerprint(cache: SchemaCache) -> None:
    cache.put_tracker(1, "Bug", {"x": 1})
    cache.reconcile_auth("api-key-v1")
    # Same key on subsequent call: data preserved
    cache.reconcile_auth("api-key-v1")
    assert cache.get_tracker(1) is not None


def test_reconcile_auth_wipes_on_key_change(cache: SchemaCache) -> None:
    cache.reconcile_auth("api-key-v1")
    cache.put_tracker(1, "Bug", {"x": 1})
    cache.put_project(99, "claudecode", {"y": 2})
    cache.reconcile_auth("api-key-v2")
    assert cache.get_tracker(1) is None
    assert cache.get_project("claudecode") is None


# ----- meta JSON blobs (statuses, priorities, current_user) ----------------


def test_meta_json_round_trip(cache: SchemaCache) -> None:
    cache.put_meta_json("issue_statuses", [{"id": 1, "name": "New"}])
    assert cache.get_meta_json("issue_statuses") == [{"id": 1, "name": "New"}]


def test_meta_json_returns_none_when_unset(cache: SchemaCache) -> None:
    assert cache.get_meta_json("never-written") is None


def test_meta_json_ttl_expiry(tmp_path: Path) -> None:
    c = SchemaCache(db_path=tmp_path / "ttl.db", ttl_seconds=0)
    try:
        c.put_meta_json("statuses", [1, 2, 3])
        time.sleep(1.05)
        assert c.get_meta_json("statuses") is None
    finally:
        c.close()


# ----- workflow observations ----------------------------------------------


def test_workflow_observation_first_record(cache: SchemaCache) -> None:
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    obs = cache.get_workflow_observation(tracker_id=1, role_id=4, from_status_id=1, to_status_id=2)
    assert obs is not None
    assert obs["outcome"] == "allowed"
    assert obs["observation_count"] == 1


def test_workflow_observation_repeats_increment_count(cache: SchemaCache) -> None:
    for _ in range(3):
        cache.record_workflow_observation(
            tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
        )
    obs = cache.get_workflow_observation(tracker_id=1, role_id=4, from_status_id=1, to_status_id=2)
    assert obs["observation_count"] == 3


def test_workflow_observation_outcome_flip_resets_count(cache: SchemaCache) -> None:
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    cache.record_workflow_observation(
        tracker_id=1,
        role_id=4,
        from_status_id=1,
        to_status_id=2,
        outcome="disallowed",
        error_text="rule changed",
    )
    obs = cache.get_workflow_observation(tracker_id=1, role_id=4, from_status_id=1, to_status_id=2)
    assert obs["outcome"] == "disallowed"
    assert obs["observation_count"] == 1
    assert obs["last_error_text"] == "rule changed"


def test_workflow_observation_invalid_outcome_raises(cache: SchemaCache) -> None:
    with pytest.raises(ValueError, match="must be 'allowed' or 'disallowed'"):
        cache.record_workflow_observation(
            tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="maybe"
        )


def test_list_workflow_observations_filtered_by_role(cache: SchemaCache) -> None:
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=3, from_status_id=1, to_status_id=2, outcome="disallowed"
    )
    cache.record_workflow_observation(
        tracker_id=2, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    role4 = cache.list_workflow_observations(tracker_id=1, role_id=4)
    assert len(role4) == 1
    assert role4[0]["role_id"] == 4

    all_t1 = cache.list_workflow_observations(tracker_id=1)
    assert len(all_t1) == 2


def test_invalidate_tracker_drops_workflow_rows(cache: SchemaCache) -> None:
    cache.put_tracker(1, "Bug", {"x": 1})
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    cache.invalidate("tracker:1")
    assert cache.list_workflow_observations(tracker_id=1) == []


# ----- resolve_tracker --------------------------------------------------


def test_resolve_tracker_by_int(cache: SchemaCache) -> None:
    cache.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
    assert cache.resolve_tracker(1) == 1


def test_resolve_tracker_by_name(cache: SchemaCache) -> None:
    cache.put_tracker(7, "Feature", {"id": 7, "name": "Feature"})
    assert cache.resolve_tracker("Feature") == 7


def test_resolve_tracker_returns_none_when_missing(cache: SchemaCache) -> None:
    assert cache.resolve_tracker("Nonexistent") is None


# ----- custom fields ------------------------------------------------------


def test_put_and_get_custom_field(cache: SchemaCache) -> None:
    cache.put_custom_field(
        field_id=42,
        name="Difficulty",
        format_kind="list",
        is_required=True,
        default_value="Unclassified",
        possible_values=["Unclassified", "Easy", "Normal", "Hard"],
        applicable_tracker_ids=[1, 2, 3, 4, 5],
        for_all_projects=True,
    )
    field = cache.get_custom_field(42)
    assert field is not None
    assert field["name"] == "Difficulty"
    assert field["is_required"] is True
    assert field["default_value"] == "Unclassified"
    assert field["possible_values"] == ["Unclassified", "Easy", "Normal", "Hard"]
    assert 2 in field["applicable_tracker_ids"]
    assert field["for_all_projects"] is True


def test_get_custom_field_returns_none_when_missing(cache: SchemaCache) -> None:
    assert cache.get_custom_field(999) is None


def test_get_custom_field_by_name(cache: SchemaCache) -> None:
    cache.put_custom_field(
        field_id=1,
        name="Difficulty",
        format_kind="list",
        is_required=True,
        default_value="Unclassified",
        possible_values=["Unclassified", "Easy"],
        applicable_tracker_ids=[],
        for_all_projects=True,
    )
    found = cache.get_custom_field_by_name("Difficulty")
    assert found is not None
    assert found["id"] == 1
    assert cache.get_custom_field_by_name("Nonexistent") is None


def test_custom_field_upsert(cache: SchemaCache) -> None:
    cache.put_custom_field(
        field_id=7,
        name="Component",
        format_kind="string",
        is_required=False,
        default_value=None,
        possible_values=[],
        applicable_tracker_ids=[1],
        for_all_projects=False,
    )
    cache.put_custom_field(
        field_id=7,
        name="Component",
        format_kind="string",
        is_required=True,
        default_value="core",
        possible_values=["core", "ui"],
        applicable_tracker_ids=[1, 2],
        for_all_projects=True,
    )
    field = cache.get_custom_field(7)
    assert field["is_required"] is True
    assert field["default_value"] == "core"
    assert field["possible_values"] == ["core", "ui"]
    assert field["applicable_tracker_ids"] == [1, 2]
    assert field["for_all_projects"] is True


def test_list_custom_fields_filters_by_tracker(cache: SchemaCache) -> None:
    cache.put_custom_field(
        field_id=42,
        name="Difficulty",
        format_kind="list",
        is_required=True,
        default_value="Unclassified",
        possible_values=["Unclassified", "Easy", "Normal", "Hard"],
        applicable_tracker_ids=[1, 2],
        for_all_projects=True,
    )
    cache.put_custom_field(
        field_id=99,
        name="Customer",
        format_kind="string",
        is_required=False,
        default_value=None,
        possible_values=[],
        applicable_tracker_ids=[3],
        for_all_projects=False,
    )

    fields_for_t1 = cache.list_custom_fields(tracker_id=1)
    assert {f["name"] for f in fields_for_t1} == {"Difficulty"}

    fields_for_t3 = cache.list_custom_fields(tracker_id=3)
    assert {f["name"] for f in fields_for_t3} == {"Customer"}

    fields_all = cache.list_custom_fields()
    assert {f["name"] for f in fields_all} == {"Difficulty", "Customer"}


def test_list_custom_fields_empty_applicable_means_all_trackers(cache: SchemaCache) -> None:
    """An empty applicable_tracker_ids list means 'applies to every tracker'."""
    cache.put_custom_field(
        field_id=1,
        name="Difficulty",
        format_kind="list",
        is_required=True,
        default_value="Unclassified",
        possible_values=["Unclassified"],
        applicable_tracker_ids=[],
        for_all_projects=True,
    )
    for tracker_id in (1, 2, 99):
        assert any(
            f["name"] == "Difficulty" for f in cache.list_custom_fields(tracker_id=tracker_id)
        )


def test_reconcile_auth_wipes_custom_fields_on_key_change(cache: SchemaCache) -> None:
    cache.reconcile_auth("api-key-v1")
    cache.put_custom_field(
        field_id=1,
        name="Difficulty",
        format_kind="list",
        is_required=True,
        default_value="Unclassified",
        possible_values=["Unclassified"],
        applicable_tracker_ids=[],
        for_all_projects=True,
    )
    cache.reconcile_auth("api-key-v2")
    assert cache.get_custom_field(1) is None


def test_invalidate_all_clears_custom_fields(cache: SchemaCache) -> None:
    cache.put_custom_field(
        field_id=1,
        name="Difficulty",
        format_kind="list",
        is_required=True,
        default_value="Unclassified",
        possible_values=["Unclassified"],
        applicable_tracker_ids=[],
        for_all_projects=True,
    )
    cache.invalidate("all")
    assert cache.get_custom_field(1) is None
