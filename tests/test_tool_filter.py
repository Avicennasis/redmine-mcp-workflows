"""Unit tests for tool-registration filtering."""

from __future__ import annotations

from redmine_mcp.tool_filter import filter_tool_names

NAMES = [
    "redmine_get_issue",
    "redmine_search_issues",
    "redmine_create_issue",
    "redmine_delete_issue",
    "redmine_list_projects",
]


def test_no_filters_keeps_everything() -> None:
    assert filter_tool_names(NAMES) == set(NAMES)


def test_disabled_removes_exact_names() -> None:
    out = filter_tool_names(NAMES, disabled={"redmine_delete_issue"})
    assert "redmine_delete_issue" not in out
    assert len(out) == len(NAMES) - 1


def test_allowlist_keeps_only_matches() -> None:
    out = filter_tool_names(NAMES, allowlist=[r"^redmine_(get|list)_"])
    assert out == {"redmine_get_issue", "redmine_list_projects"}


def test_denylist_removes_matches() -> None:
    out = filter_tool_names(NAMES, denylist=[r"_issue$"])
    assert out == {"redmine_search_issues", "redmine_list_projects"}


def test_disabled_beats_allowlist() -> None:
    out = filter_tool_names(
        NAMES,
        allowlist=[r"^redmine_(get|list)_"],
        disabled={"redmine_get_issue"},
    )
    assert out == {"redmine_list_projects"}


def test_blank_entries_ignored() -> None:
    out = filter_tool_names(NAMES, disabled={"", "redmine_delete_issue"})
    assert "redmine_delete_issue" not in out
    assert len(out) == len(NAMES) - 1


def test_denylist_wins_over_allowlist() -> None:
    out = filter_tool_names(
        NAMES,
        allowlist=[r"^redmine_(get|delete)_issue$"],
        denylist=[r"delete"],
    )
    assert out == {"redmine_get_issue"}


def test_categories_gate_before_other_filters() -> None:
    out = filter_tool_names(NAMES, categories=["issues"])
    # Only the issue-named tools in NAMES survive; list_projects is not "issues".
    assert out == {
        "redmine_get_issue",
        "redmine_search_issues",
        "redmine_create_issue",
        "redmine_delete_issue",
    }
