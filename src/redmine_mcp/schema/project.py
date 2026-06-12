"""Project schema fetcher.

Fetches ``/projects/{slug}.json?include=trackers,issue_categories,enabled_modules``
and caches the result.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError

_INCLUDE = "trackers,issue_categories,enabled_modules"


async def describe_project(
    client: RedmineClient,
    cache: SchemaCache,
    project_ident: int | str,
) -> dict[str, Any]:
    """Return a cached or freshly-fetched project description.

    Returns a structured ``project_not_found`` dict (rather than raising)
    when Redmine 404s on the slug — callers in the resolver path want to
    fall back to a name lookup, not bubble a generic 404.
    """
    ident_str = str(project_ident)

    cached = cache.get_project(ident_str)
    if cached is not None:
        return {**cached, "source": "cache"}

    try:
        payload = await client.get(f"/projects/{ident_str}.json", params={"include": _INCLUDE})
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "project_not_found",
                "hint": f"No project matches {ident_str!r}.",
            }
        raise
    project = payload.get("project") if isinstance(payload, dict) else None
    if not project:
        return {
            "error": "project_not_found",
            "hint": f"No project matches {ident_str!r}.",
        }

    cache.put_project(
        project_id=int(project["id"]),
        identifier=project.get("identifier", ident_str),
        schema=project,
    )
    return {**project, "source": "api"}


async def list_projects(
    client: RedmineClient,
    *,
    query: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated project listing.

    Redmine's ``/projects.json`` doesn't accept a free-text query parameter,
    so when ``query`` is provided we walk every page (capped at PAGE_CAP × 100
    to keep pathological fleets bounded), filter by substring on
    name/identifier/description, then re-slice by ``limit``/``offset``.
    ``total_count`` reflects the *filtered* total when filtering, so callers
    can paginate over matches directly.

    Without ``query``, behavior is the previous single-page passthrough.
    """
    page_size = min(limit, 100)

    if not query:
        params: dict[str, Any] = {"limit": page_size, "offset": offset}
        payload = await client.get("/projects.json", params=params)
        projects = payload.get("projects", []) if isinstance(payload, dict) else []
        total = (
            payload.get("total_count", len(projects))
            if isinstance(payload, dict)
            else len(projects)
        )
        return {
            "projects": projects,
            "total_count": total,
            "limit": limit,
            "offset": offset,
            "filtered_locally": False,
        }

    # Filtered path: fetch all pages up to PAGE_CAP, then filter, then slice.
    # PAGE_CAP × 100 = 1000 projects is plenty for any plausible fleet.
    PAGE_CAP = 10
    PAGE_SIZE = 100
    all_projects: list[dict[str, Any]] = []
    for page in range(PAGE_CAP):
        params = {"limit": PAGE_SIZE, "offset": page * PAGE_SIZE}
        payload = await client.get("/projects.json", params=params)
        if not isinstance(payload, dict):
            break
        page_projects = payload.get("projects") or []
        all_projects.extend(page_projects)
        total = payload.get("total_count", len(all_projects))
        if len(all_projects) >= total or len(page_projects) < PAGE_SIZE:
            break

    q = query.lower()
    matched = [
        p for p in all_projects
        if q in (p.get("name") or "").lower()
        or q in (p.get("identifier") or "").lower()
        or q in (p.get("description") or "").lower()
    ]
    sliced = matched[offset : offset + limit]
    return {
        "projects": sliced,
        "total_count": len(matched),
        "limit": limit,
        "offset": offset,
        "filtered_locally": True,
    }
