# Tool Catalog

81 tools organized by domain.

## Discovery & Introspection (5)

| Tool | Description |
|------|-------------|
| `redmine_describe_tracker` | Return an enriched schema for one tracker — required fields, allowed status transitions per role, custom field schemas. Cache-backed. |
| `redmine_describe_project` | Return a project description — enabled trackers, modules, issue categories, members, default tracker. |
| `redmine_list_projects` | List projects (paginated, optional substring filter). |
| `redmine_list_trackers` | List all trackers configured on the Redmine server. |
| `redmine_search` | Full-text search across all Redmine resource types. |

## Cache Control (1)

| Tool | Description |
|------|-------------|
| `redmine_invalidate_cache` | Drop cached entries for a scope (`"all"`, `"tracker:<id>"`, or `"project:<slug>"`). No HTTP request is made. |

## Issues (6)

Every write validates against the schema cache — status transitions, custom fields, required fields.

| Tool | Description |
|------|-------------|
| `redmine_create_issue` | Create an issue with cache-aware id resolution. Supports `difficulty` and `held`/`held_until` convenience params. |
| `redmine_get_issue` | Fetch a single issue (with optional includes: attachments, journals, relations, watchers). |
| `redmine_update_issue` | Update an issue, with reactive workflow validation on status changes. Supports `difficulty`, `held`/`held_until`. |
| `redmine_close_issue` | Move an issue to its first `is_closed` status. Checks the held gate before closing. |
| `redmine_delete_issue` | Permanently delete an issue. Cannot be undone. |
| `redmine_search_issues` | Search/list issues with optional filters, pagination, and saved query support. |

## Bulk Operations (3)

| Tool | Description |
|------|-------------|
| `redmine_bulk_create_issues` | Bulk-create issues with subject idempotency. Pre-checks each subject within its project to avoid duplicates. |
| `redmine_bulk_update_issues` | Apply the same field updates to many issues in one call. ≤100 per batch. |
| `redmine_bulk_close` | Close many issues at once with an optional shared note. Returns `{total, succeeded, failed, skipped}`. |

## Comments & Journals (3)

| Tool | Description |
|------|-------------|
| `redmine_add_comment` | Append a comment (journal entry) to an existing issue. |
| `redmine_get_journals` | Return the structured journal entries for an issue. |
| `redmine_update_journal` | Edit an existing journal entry's notes in place (Redmine 5.0+). |

## Attachments (2)

| Tool | Description |
|------|-------------|
| `redmine_upload_attachment` | Upload a file (path-restricted to `REDMINE_MCP_ALLOWED_DIRECTORIES`) and optionally attach it to an issue. |
| `redmine_download_attachment` | Download an attachment by id to a path-restricted location. Validates byte count against metadata before writing. |

## Relations (4)

| Tool | Description |
|------|-------------|
| `redmine_list_relations` | List all relations on an issue (both directions). |
| `redmine_add_relation` | Create a relation between two issues. Accepts colloquial aliases (`related_to`, `blocked_by`, `duplicate_of`). |
| `redmine_remove_relation` | Delete a relation by its numeric id. |
| `redmine_set_parent_issue` | Set or clear an issue's parent. Pass `0` to unparent. |

## Watchers (3)

| Tool | Description |
|------|-------------|
| `redmine_add_watcher` | Add a user as a watcher of an issue. Idempotent. |
| `redmine_remove_watcher` | Remove a user from an issue's watcher list. |
| `redmine_list_watchers` | Return the current watcher list for an issue. |

## Time Entries (4)

| Tool | Description |
|------|-------------|
| `redmine_create_time_entry` | Log a time entry against an issue or project. Accepts `H:MM` or decimal hours; activity names resolve through the cached enumeration. |
| `redmine_list_time_entries` | List time entries with optional filters (issue, project, user, date range). |
| `redmine_update_time_entry` | Update a time entry. Partial — only supplied fields are sent. |
| `redmine_delete_time_entry` | Delete a time entry. Permanent. |

## Versions / Milestones (6)

| Tool | Description |
|------|-------------|
| `redmine_list_versions` | List all versions defined on a project. |
| `redmine_get_version` | Fetch one version by id. |
| `redmine_create_version` | Create a version on a project. Status and sharing enums validated client-side. |
| `redmine_update_version` | Update a version. Partial — only supplied fields are sent. |
| `redmine_delete_version` | Delete a version by id. Returns 422 if issues still reference it. |
| `redmine_assign_issue_to_version` | Assign (or clear) an issue's target version. Pass `version_id=0` to clear. |

## Wiki (4)

| Tool | Description |
|------|-------------|
| `redmine_get_wiki_page` | Fetch a wiki page (optionally a historical version). |
| `redmine_create_wiki_page` | Create a new wiki page. Pre-flight GET refuses to overwrite an existing page. |
| `redmine_update_wiki_page` | Update an existing wiki page. Optional `version` parameter for optimistic concurrency. |
| `redmine_delete_wiki_page` | Permanently delete a wiki page and all its historical versions. |

## Projects (7)

| Tool | Description |
|------|-------------|
| `redmine_list_projects` | List projects (paginated, optional substring filter). |
| `redmine_create_project` | Create a new project. |
| `redmine_update_project` | Update a project. Partial — only supplied fields are sent. |
| `redmine_delete_project` | Permanently delete a project and all its data. |
| `redmine_archive_project` | Archive a project (Redmine 5.0+). Reversible via unarchive. |
| `redmine_unarchive_project` | Unarchive a previously archived project. |
| `redmine_describe_project` | Return a project description (trackers, modules, members). |

## Groups (7)

| Tool | Description |
|------|-------------|
| `redmine_list_groups` | List all groups (admin only). |
| `redmine_get_group` | Fetch a group by id. |
| `redmine_create_group` | Create a group (admin only). |
| `redmine_update_group` | Update a group's name. |
| `redmine_delete_group` | Delete a group (admin only). Permanent. |
| `redmine_add_group_user` | Add a user to a group. |
| `redmine_remove_group_user` | Remove a user from a group. |

## Memberships (4)

| Tool | Description |
|------|-------------|
| `redmine_list_memberships` | List project members (paginated). |
| `redmine_add_membership` | Add a member to a project. |
| `redmine_update_membership` | Update a membership's roles. |
| `redmine_remove_membership` | Remove a project membership. Inherited memberships can't be deleted. |

## Users & Roles (4)

| Tool | Description |
|------|-------------|
| `redmine_get_user` | Fetch a user by id, or the current API user. |
| `redmine_list_users` | List users (admin only). |
| `redmine_list_roles` | List all roles. |
| `redmine_get_role` | Fetch a role by id, including its permissions list. |

## Issue Categories (3)

| Tool | Description |
|------|-------------|
| `redmine_list_issue_categories` | List issue categories for a project. |
| `redmine_create_issue_category` | Create an issue category on a project. |
| `redmine_delete_issue_category` | Delete an issue category. Permanent. |

## News (4)

| Tool | Description |
|------|-------------|
| `redmine_list_news` | List news entries (global or project-scoped). |
| `redmine_create_news` | Create a news entry on a project. |
| `redmine_update_news` | Update a news entry. Partial — only supplied fields are sent. |
| `redmine_delete_news` | Delete a news entry. Permanent. |

## Forums (4)

| Tool | Description |
|------|-------------|
| `redmine_list_boards` | List forum boards for a project. |
| `redmine_list_messages` | List forum messages on a board. |
| `redmine_create_message` | Create a new forum topic on a board. |
| `redmine_reply_message` | Reply to an existing forum topic. |
| `redmine_delete_message` | Delete a forum message. Permanent. |

## Files (2)

| Tool | Description |
|------|-------------|
| `redmine_list_project_files` | List files on a project's Files section. |
| `redmine_upload_project_file` | Upload a file to a project's Files section. |

## Enumerations & Reference Data (5)

| Tool | Description |
|------|-------------|
| `redmine_list_enumerations` | List enumeration values for a given type (priorities, time entry activities, etc.). |
| `redmine_list_issue_statuses` | List all issue statuses with their `is_closed` flags. |
| `redmine_list_custom_fields` | List all custom field definitions (admin only). |
| `redmine_list_queries` | List available saved queries. |
| `redmine_list_trackers` | List all trackers. |

## Passthrough (1)

| Tool | Description |
|------|-------------|
| `redmine_request` | **Escape hatch** — generic passthrough to any Redmine REST endpoint. Gated behind `REDMINE_MCP_ENABLE_PASSTHROUGH=true`. No validation, no workflow check, no schema cache. Every response carries `validation_skipped: true`. |
