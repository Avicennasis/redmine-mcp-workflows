"""Tests for tool category mapping and feature gating."""

from __future__ import annotations

from redmine_mcp import server
from redmine_mcp.tool_categories import (
    ALL_CATEGORIES,
    TOOL_CATEGORIES,
    category_of,
    filter_by_categories,
)


def test_every_registered_tool_is_categorized() -> None:
    registered = set(server.mcp._tool_manager._tools)
    unmapped = registered - set(TOOL_CATEGORIES)
    assert not unmapped, f"tools missing a category: {sorted(unmapped)}"


def test_no_stale_category_entries() -> None:
    registered = set(server.mcp._tool_manager._tools)
    stale = set(TOOL_CATEGORIES) - registered
    assert not stale, f"category map references unknown tools: {sorted(stale)}"


def test_category_of_known_and_unknown() -> None:
    assert category_of("redmine_get_issue") == "issues"
    assert category_of("redmine_list_time_entries") == "time"
    assert category_of("nope") is None


def test_filter_by_categories_empty_keeps_all() -> None:
    names = ["redmine_get_issue", "redmine_list_time_entries"]
    assert filter_by_categories(names, ()) == set(names)


def test_filter_by_categories_keeps_enabled_only() -> None:
    names = [
        "redmine_get_issue",
        "redmine_list_time_entries",
        "redmine_list_versions",
    ]
    assert filter_by_categories(names, ["time"]) == {"redmine_list_time_entries"}


def test_expected_categories_present() -> None:
    assert {
        "issues",
        "comments",
        "attachments",
        "wiki",
        "projects",
        "time",
        "versions",
        "users",
        "groups",
        "roles",
        "memberships",
        "news",
        "forums",
        "reference",
        "search",
        "admin",
    } == ALL_CATEGORIES
