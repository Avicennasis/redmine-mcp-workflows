"""Tests for the endpoint catalog."""

from __future__ import annotations

from redmine_mcp.tools import endpoints


def test_catalog_returns_all() -> None:
    result = endpoints.list_endpoints()
    assert result["total_count"] == len(endpoints.ENDPOINTS)
    assert all({"method", "path", "purpose"} <= set(e) for e in result["endpoints"])


def test_catalog_filters_by_path_substring() -> None:
    result = endpoints.list_endpoints("time_entries")
    assert result["total_count"] >= 1
    assert all("time_entries" in e["path"] for e in result["endpoints"])


def test_catalog_filter_matches_method() -> None:
    result = endpoints.list_endpoints("delete")
    assert result["total_count"] >= 1
    assert all(e["method"] == "DELETE" for e in result["endpoints"])


def test_catalog_no_match() -> None:
    result = endpoints.list_endpoints("zzz-no-such-thing")
    assert result["total_count"] == 0
    assert result["endpoints"] == []


def test_catalog_matches_typed_tool_routes_and_verbs() -> None:
    by_path = {entry["path"]: entry for entry in endpoints.ENDPOINTS}
    assert by_path["/search.json"]["method"] == "GET"
    assert by_path["/projects/{id}/archive.json"]["method"] == "PUT"
    assert by_path["/projects/{id}/unarchive.json"]["method"] == "PUT"
    assert by_path["/projects/{project}/wiki/{title}.json"]["method"] == "GET"
