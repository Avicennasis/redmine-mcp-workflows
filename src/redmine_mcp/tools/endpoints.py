"""Read-only catalog of Redmine REST endpoints.

The passthrough tool (``redmine_request``) is an escape hatch, but it is only
usable if the caller knows the path shape. This catalog lists the endpoints
the server itself exercises plus the common ones we deliberately do not wrap
(so an agent does not have to guess). It is documentation-as-data: no API
calls are made.
"""

from __future__ import annotations

from typing import Any

# (method, path, purpose). Paths use ``{id}`` placeholders.
ENDPOINTS: list[dict[str, str]] = [
    {
        "method": "GET",
        "path": "/issues.json",
        "purpose": "List/search issues (filters: project_id, status_id, assigned_to_id, parent_id, cf_<id>)",
    },
    {
        "method": "GET",
        "path": "/issues/{id}.json",
        "purpose": "Fetch one issue (include=attachments,journals,relations,children,watchers)",
    },
    {"method": "POST", "path": "/issues.json", "purpose": "Create an issue"},
    {
        "method": "PUT",
        "path": "/issues/{id}.json",
        "purpose": "Update an issue (status, assignee, fields, notes)",
    },
    {"method": "DELETE", "path": "/issues/{id}.json", "purpose": "Delete an issue"},
    {
        "method": "GET",
        "path": "/issues/{id}/relations.json",
        "purpose": "List an issue's relations",
    },
    {"method": "POST", "path": "/issues/{id}/relations.json", "purpose": "Create a relation"},
    {
        "method": "DELETE",
        "path": "/relations/{id}.json",
        "purpose": "Delete a relation (top-level path)",
    },
    {"method": "GET", "path": "/projects.json", "purpose": "List projects"},
    {
        "method": "GET",
        "path": "/projects/{id}.json",
        "purpose": "Project detail (include=trackers,issue_categories,enabled_modules)",
    },
    {"method": "POST", "path": "/projects.json", "purpose": "Create a project"},
    {
        "method": "POST",
        "path": "/projects/{id}/archive.json",
        "purpose": "Archive a project (Redmine 5.0+)",
    },
    {"method": "POST", "path": "/projects/{id}/unarchive.json", "purpose": "Unarchive a project"},
    {
        "method": "GET",
        "path": "/projects/{id}/issue_categories.json",
        "purpose": "List issue categories",
    },
    {
        "method": "GET",
        "path": "/projects/{id}/versions.json",
        "purpose": "List versions/milestones",
    },
    {"method": "POST", "path": "/projects/{id}/versions.json", "purpose": "Create a version"},
    {
        "method": "GET",
        "path": "/time_entries.json",
        "purpose": "List time entries (from/to, project_id, user_id, issue_id)",
    },
    {"method": "POST", "path": "/time_entries.json", "purpose": "Create a time entry"},
    {"method": "PUT", "path": "/time_entries/{id}.json", "purpose": "Update a time entry"},
    {
        "method": "GET",
        "path": "/enumerations/time_entry_activities.json",
        "purpose": "Valid time-entry activity values",
    },
    {"method": "GET", "path": "/users/current.json", "purpose": "Current account (health check)"},
    {"method": "GET", "path": "/users.json", "purpose": "List users (admin)"},
    {"method": "GET", "path": "/groups.json", "purpose": "List groups (admin)"},
    {"method": "GET", "path": "/roles.json", "purpose": "List roles"},
    {"method": "GET", "path": "/trackers.json", "purpose": "List trackers (admin)"},
    {"method": "GET", "path": "/issue_statuses.json", "purpose": "List issue statuses (admin)"},
    {
        "method": "GET",
        "path": "/custom_fields.json",
        "purpose": "List custom field definitions (admin)",
    },
    {"method": "GET", "path": "/queries.json", "purpose": "List saved queries"},
    {"method": "GET", "path": "/attachments/{id}.json", "purpose": "Attachment metadata"},
    {
        "method": "GET",
        "path": "/attachments/download/{id}/{filename}",
        "purpose": "Attachment bytes",
    },
    {
        "method": "POST",
        "path": "/uploads.json",
        "purpose": "Upload a file, returns a token for issue/attachment use",
    },
    {"method": "GET", "path": "/news.json", "purpose": "Global news feed"},
    {"method": "GET", "path": "/projects/{id}/news.json", "purpose": "Project news feed"},
    {
        "method": "GET",
        "path": "/projects/{id}/boards.json",
        "purpose": "Forum boards (boards module)",
    },
    {"method": "GET", "path": "/wiki/{project}/{title}.json", "purpose": "Fetch a wiki page"},
    {"method": "POST", "path": "/search.json", "purpose": "Full-text search (q, scope)"},
]


def list_endpoints(query: str | None = None) -> dict[str, Any]:
    """Return the endpoint catalog, optionally filtered by substring.

    Matches case-insensitively against method, path, and purpose.
    """
    if query:
        needle = query.strip().lower()
        items = [
            e
            for e in ENDPOINTS
            if needle in e["method"].lower()
            or needle in e["path"].lower()
            or needle in e["purpose"].lower()
        ]
    else:
        items = list(ENDPOINTS)
    return {"endpoints": items, "total_count": len(items), "source": "catalog"}
