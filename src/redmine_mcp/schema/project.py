"""Project schema fetcher.

Fetches ``/projects/{slug}.json?include=trackers,issue_categories,enabled_modules``
and caches the result.
"""

from __future__ import annotations

import contextlib
from typing import Any
from urllib.parse import quote

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError

_INCLUDE = "trackers,issue_categories,enabled_modules"


def _try_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    ident_segment = quote(ident_str, safe="")

    cached = cache.get_project(ident_str)
    if cached is not None:
        return {**cached, "source": "cache"}

    try:
        payload = await client.get(f"/projects/{ident_segment}.json", params={"include": _INCLUDE})
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

    # Filtered path: fetch all pages up to page_cap, then filter, then slice.
    # page_cap × 100 = 1000 projects is plenty for any plausible fleet.
    page_cap = 10
    page_size = 100
    all_projects: list[dict[str, Any]] = []
    for page in range(page_cap):
        params = {"limit": page_size, "offset": page * page_size}
        payload = await client.get("/projects.json", params=params)
        if not isinstance(payload, dict):
            break
        page_projects = payload.get("projects") or []
        all_projects.extend(page_projects)
        total = payload.get("total_count", len(all_projects))
        if len(all_projects) >= total or len(page_projects) < page_size:
            break

    q = query.lower()
    matched = [
        p
        for p in all_projects
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


async def resolve_project_id(
    client: RedmineClient,
    cache: SchemaCache,
    ident: int | str,
) -> int | None:
    """Resolve a project reference (id, slug, or display name) to a numeric id.

    Lookup order:
      1. Numeric id (int or stringy int) → return as-is.
      2. Cache by identifier slug.
      3. :func:`describe_project` (fetches by slug, caches on success).
      4. Cache by display name (case-insensitive) — handles the natural
         get→create round-trip where callers pass ``project.name`` from a
         prior issue response.
      5. Refresh the project list and try the display-name match again.

    Returns ``None`` when no path resolves; callers translate that into a
    structured ``project_not_found`` error.
    """
    if isinstance(ident, int):
        return ident
    as_int = _try_int(ident)
    if as_int is not None:
        return as_int
    ident_str = str(ident)
    cached = cache.get_project(ident_str)
    if cached is not None:
        return _try_int(cached.get("id"))
    fetched = await describe_project(client, cache, ident_str)
    if isinstance(fetched, dict) and not fetched.get("error"):
        return _try_int(fetched.get("id"))
    # Slug lookup failed — try display name (cached, then refreshed list).
    by_name = cache.get_project_by_name(ident_str)
    if by_name is not None:
        return _try_int(by_name.get("id"))
    # The unfiltered endpoint returns only its first page here. Use the local
    # filtering path so display names beyond the first 100 projects can still
    # resolve (up to list_projects' documented safety cap).
    listing = await list_projects(client, query=ident_str, limit=100)
    target = ident_str.strip().lower()
    for project in listing.get("projects", []):
        if str(project.get("name", "")).strip().lower() == target:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                cache.put_project(
                    project_id=int(project["id"]),
                    identifier=project.get("identifier", project["name"]),
                    schema=project,
                )
            return _try_int(project.get("id"))
    return None


async def project_path_segment(
    client: RedmineClient,
    cache: SchemaCache,
    ident: int | str,
) -> str:
    """Return a safe URL path segment for a project reference.

    Prefers a resolved numeric id (Redmine's ``/projects/:id`` accepts an id
    or an identifier slug). Falls back to the percent-encoded original so a
    value such as ``"1?status_id=*"`` cannot inject URL structure — Redmine
    IGNORES unknown filter params rather than rejecting them, so an
    unescaped ``?`` would silently change the query.
    """
    resolved = await resolve_project_id(client, cache, ident)
    if resolved is not None:
        return str(resolved)
    return quote(str(ident), safe="")
