"""Full-text search tool (Redmine ticket #4481).

Tools (1):
  - search — GET /search.json

Searches across all resource types (issues, wiki pages, news, changesets,
messages, projects, documents). Complements ``search_issues`` which only
covers issues with structured filters.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError

ALLOWED_RESOURCE_TYPES = frozenset(
    {
        "issues",
        "news",
        "documents",
        "changesets",
        "wiki_pages",
        "messages",
        "projects",
    }
)
ALLOWED_ATTACHMENT_MODES = frozenset({"0", "1", "only"})


async def search(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    *,
    query: str,
    project: int | str | None = None,
    resource_types: list[str] | None = None,
    all_words: bool = True,
    titles_only: bool = False,
    open_issues: bool = False,
    attachments: str = "0",
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """Full-text search across Redmine resources.

    Args:
        query: search string (required, non-empty).
        project: optional project id or slug to scope the search.
        resource_types: optional list of resource types to include
            (e.g. ``["issues", "wiki_pages"]``). ``None`` searches all.
        all_words: if True, match all words; if False, match any.
        titles_only: if True, only search titles.
        open_issues: if True, only return open issues (ignored for other types).
        attachments: ``"0"`` (description only), ``"1"`` (description +
            attachments), ``"only"`` (attachments only).
        limit: page size (capped at 100).
        offset: skip the first N results.
    """
    if not query or not query.strip():
        return {
            "error": "validation_failed",
            "hint": "query is required and must be non-empty.",
        }

    if attachments not in ALLOWED_ATTACHMENT_MODES:
        return {
            "error": "invalid_attachments_mode",
            "hint": f"attachments {attachments!r} is not one of {sorted(ALLOWED_ATTACHMENT_MODES)}.",
            "attachments": attachments,
            "allowed": sorted(ALLOWED_ATTACHMENT_MODES),
        }

    if resource_types:
        bad = [t for t in resource_types if t not in ALLOWED_RESOURCE_TYPES]
        if bad:
            return {
                "error": "invalid_resource_types",
                "hint": f"Unknown resource type(s): {bad}. Allowed: {sorted(ALLOWED_RESOURCE_TYPES)}.",
                "invalid": bad,
                "allowed": sorted(ALLOWED_RESOURCE_TYPES),
            }

    path = "/search.json"
    if project is not None:
        path = f"/projects/{project}/search.json"

    params: dict[str, Any] = {
        "q": query.strip(),
        "limit": min(limit, 100),
        "offset": offset,
    }
    if not all_words:
        params["all_words"] = 0
    if titles_only:
        params["titles_only"] = 1
    if open_issues:
        params["open_issues"] = 1
    if attachments != "0":
        params["attachments"] = attachments

    if resource_types:
        for rt in resource_types:
            params[rt] = 1

    try:
        resp = await client.get(path, params=params)
    except RedmineAPIError as e:
        return e.as_structured()

    if not isinstance(resp, dict):
        return {
            "results": [],
            "total_count": 0,
            "limit": limit,
            "offset": offset,
            "source": "api",
        }

    return {
        "results": resp.get("results", []),
        "total_count": resp.get("total_count", 0),
        "limit": resp.get("limit", limit),
        "offset": resp.get("offset", offset),
        "source": "api",
    }
