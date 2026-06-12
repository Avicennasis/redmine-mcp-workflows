"""News tools (Redmine tickets #2390, #4492).

Tools (4):
  - list_news   — GET    /news.json or /projects/{id}/news.json
  - create_news — POST   /projects/{id}/news.json
  - update_news — PUT    /news/{id}.json
  - delete_news — DELETE /news/{id}.json

Niche surface — Redmine's news/announcement feed. ``list_news`` is the
original read-only tool; CRUD tools added per #4492.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_news(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    *,
    project: int | str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List news entries, optionally scoped to a project.

    Args:
        project: numeric id or identifier slug. ``None`` for the global
            feed across all projects the API key can see.
        limit: Redmine page size (capped server-side at 100).
        offset: skip the first N results.
    """
    path = "/news.json" if project is None else f"/projects/{project}/news.json"
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    try:
        resp = await client.get(path, params=params)
    except RedmineAPIError as e:
        return e.as_structured()

    if not isinstance(resp, dict):
        return {"news": [], "total_count": 0, "limit": limit, "offset": offset, "source": "api"}

    return {
        "news": resp.get("news", []),
        "total_count": resp.get("total_count", 0),
        "limit": resp.get("limit", limit),
        "offset": resp.get("offset", offset),
        "source": "api",
    }


async def create_news(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project: int | str,
    title: str,
    summary: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a news entry on a project.

    Args:
        project: numeric id or identifier slug.
        title: required news title.
        summary: optional short summary.
        description: optional full body.
    """
    if not title or not title.strip():
        return {
            "error": "validation_failed",
            "hint": "Field 'title' is required for create_news.",
        }

    body: dict[str, Any] = {"title": title}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description

    try:
        resp = await client.post(
            f"/projects/{project}/news.json", json={"news": body},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    news_item = resp.get("news") if isinstance(resp, dict) else None

    if news_item is None:
        try:
            refetch = await client.get(
                f"/projects/{project}/news.json", params={"limit": 1},
            )
            entries = refetch.get("news", []) if isinstance(refetch, dict) else []
            if entries:
                news_item = entries[0]
        except RedmineAPIError:
            pass

    return {"news": news_item, "source": "api"}


async def update_news(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    news_id: int,
    title: str | None = None,
    summary: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Update a news entry. Partial — only supplied fields are sent."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description

    if not body:
        return {
            "error": "nothing_to_update",
            "hint": "Provide at least one updatable field.",
            "news_id": news_id,
        }

    try:
        await client.put(f"/news/{news_id}.json", json={"news": body})
    except RedmineAPIError as e:
        return e.as_structured()

    return {"news_id": news_id, "updated": True, "source": "api"}


async def delete_news(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    news_id: int,
) -> dict[str, Any]:
    """Delete a news entry. Permanent."""
    try:
        await client.delete(f"/news/{news_id}.json")
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "news_not_found",
                "hint": f"News entry {news_id} not found.",
                "news_id": news_id,
            }
        return e.as_structured()
    return {"news_id": news_id, "deleted": True, "source": "api"}
