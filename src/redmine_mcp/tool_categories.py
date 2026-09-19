"""Tool categories for feature gating.

``REDMINE_MCP_FEATURES`` lets an operator advertise only selected domains of
the tool surface, complementing the exact-name and regex filters. Every
registered tool belongs to exactly one category here; ``tests`` assert the
map stays complete as tools are added.
"""

from __future__ import annotations

from collections.abc import Iterable

TOOL_CATEGORIES: dict[str, str] = {
    # issues
    "redmine_get_issue": "issues",
    "redmine_create_issue": "issues",
    "redmine_update_issue": "issues",
    "redmine_close_issue": "issues",
    "redmine_delete_issue": "issues",
    "redmine_search_issues": "issues",
    "redmine_my_issues": "issues",
    "redmine_list_child_issues": "issues",
    "redmine_bulk_close": "issues",
    "redmine_bulk_create_issues": "issues",
    "redmine_bulk_update_issues": "issues",
    "redmine_set_parent_issue": "issues",
    "redmine_assign_issue_to_version": "issues",
    "redmine_add_relation": "issues",
    "redmine_remove_relation": "issues",
    "redmine_list_relations": "issues",
    "redmine_add_watcher": "issues",
    "redmine_remove_watcher": "issues",
    "redmine_list_watchers": "issues",
    # comments / journals
    "redmine_add_comment": "comments",
    "redmine_get_journals": "comments",
    "redmine_update_journal": "comments",
    # attachments
    "redmine_download_attachment": "attachments",
    "redmine_upload_attachment": "attachments",
    "redmine_view_attachment": "attachments",
    # wiki
    "redmine_create_wiki_page": "wiki",
    "redmine_get_wiki_page": "wiki",
    "redmine_update_wiki_page": "wiki",
    "redmine_delete_wiki_page": "wiki",
    # projects
    "redmine_create_project": "projects",
    "redmine_update_project": "projects",
    "redmine_delete_project": "projects",
    "redmine_list_projects": "projects",
    "redmine_describe_project": "projects",
    "redmine_archive_project": "projects",
    "redmine_unarchive_project": "projects",
    "redmine_create_issue_category": "projects",
    "redmine_list_issue_categories": "projects",
    "redmine_update_issue_category": "projects",
    "redmine_delete_issue_category": "projects",
    "redmine_list_project_files": "projects",
    "redmine_upload_project_file": "projects",
    "redmine_activity_feed": "projects",
    # time tracking
    "redmine_create_time_entry": "time",
    "redmine_list_time_entries": "time",
    "redmine_today_time_entries": "time",
    "redmine_time_report": "time",
    "redmine_bulk_create_time_entries": "time",
    "redmine_update_time_entry": "time",
    "redmine_delete_time_entry": "time",
    # versions
    "redmine_create_version": "versions",
    "redmine_list_versions": "versions",
    "redmine_get_version": "versions",
    "redmine_update_version": "versions",
    "redmine_delete_version": "versions",
    # users
    "redmine_list_users": "users",
    "redmine_get_user": "users",
    # groups
    "redmine_list_groups": "groups",
    "redmine_get_group": "groups",
    "redmine_create_group": "groups",
    "redmine_update_group": "groups",
    "redmine_delete_group": "groups",
    "redmine_add_group_user": "groups",
    "redmine_remove_group_user": "groups",
    # roles
    "redmine_list_roles": "roles",
    "redmine_get_role": "roles",
    # memberships
    "redmine_list_memberships": "memberships",
    "redmine_add_membership": "memberships",
    "redmine_update_membership": "memberships",
    "redmine_remove_membership": "memberships",
    # news
    "redmine_list_news": "news",
    "redmine_create_news": "news",
    "redmine_update_news": "news",
    "redmine_delete_news": "news",
    # forums
    "redmine_list_boards": "forums",
    "redmine_list_messages": "forums",
    "redmine_create_message": "forums",
    "redmine_reply_message": "forums",
    "redmine_delete_message": "forums",
    # reference data
    "redmine_list_enumerations": "reference",
    "redmine_list_issue_statuses": "reference",
    "redmine_list_trackers": "reference",
    "redmine_list_queries": "reference",
    "redmine_describe_tracker": "reference",
    "redmine_list_custom_fields": "reference",
    "redmine_list_endpoints": "reference",
    # search
    "redmine_search": "search",
    # admin / escape hatch
    "redmine_invalidate_cache": "admin",
    "redmine_request": "admin",
    "redmine_health": "admin",
    "redmine_metrics": "admin",
}

ALL_CATEGORIES = frozenset(TOOL_CATEGORIES.values())


def category_of(tool_name: str) -> str | None:
    """Return a tool's category, or ``None`` if it is unmapped."""
    return TOOL_CATEGORIES.get(tool_name)


def filter_by_categories(names: Iterable[str], categories: Iterable[str]) -> set[str]:
    """Keep only tools whose category is enabled.

    An empty ``categories`` means "no gating" — every name passes. Unmapped
    tools are dropped when gating is active (they belong to no enabled
    category), which keeps the advertised surface honest.
    """
    enabled = {c for c in categories if c}
    if not enabled:
        return set(names)
    unknown = enabled - ALL_CATEGORIES
    if unknown:
        raise ValueError(
            "unknown REDMINE_MCP_FEATURES categories: "
            f"{', '.join(sorted(unknown))}; valid categories: "
            f"{', '.join(sorted(ALL_CATEGORIES))}"
        )
    return {n for n in names if TOOL_CATEGORIES.get(n) in enabled}
