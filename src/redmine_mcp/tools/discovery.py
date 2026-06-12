"""Discovery & introspection tools.

Phase 1: ``redmine_list_trackers`` (smoke-test entry — populates the cache).
Phase 2: ``redmine_describe_tracker``, ``redmine_describe_project``,
         ``redmine_list_projects``, ``redmine_invalidate_cache``.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..schema import project as project_schema
from ..schema import tracker as tracker_schema


async def list_trackers(client: RedmineClient, cache: SchemaCache) -> dict[str, Any]:
    """Fetch trackers from Redmine and cache them.

    Always hits the API. Returns a dict shaped like::

        {
            "trackers": [...],
            "count": <int>,
            "source": "api",
        }
    """
    trackers = await tracker_schema.fetch_all_trackers(client, cache)
    return {"trackers": trackers, "count": len(trackers), "source": "api"}


async def describe_tracker(
    client: RedmineClient,
    cache: SchemaCache,
    tracker: int | str,
    *,
    include_observations: bool = True,
) -> dict[str, Any]:
    """Return an enriched description for one tracker.

    Pulls in available statuses + priorities + the learned workflow graph.
    Cache is populated lazily on first call.
    """
    return await tracker_schema.describe_tracker(
        client, cache, tracker, include_observations=include_observations
    )


async def describe_project(
    client: RedmineClient,
    cache: SchemaCache,
    project: int | str,
) -> dict[str, Any]:
    """Return a cached or freshly-fetched project description."""
    return await project_schema.describe_project(client, cache, project)


async def list_projects(
    client: RedmineClient,
    *,
    query: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated project list with optional client-side substring filter."""
    return await project_schema.list_projects(client, query=query, limit=limit, offset=offset)


def invalidate_cache(cache: SchemaCache, scope: str) -> dict[str, Any]:
    """Drop cached entries for the requested scope.

    Scope syntax:
      * ``"all"`` — drop all schema/observation rows (auth fingerprint preserved)
      * ``"tracker:<id-or-name>"`` — drop one tracker + its workflow observations
      * ``"project:<id-or-slug>"`` — drop one project
    """
    deleted = cache.invalidate(scope)
    return {
        "scope": scope,
        "deleted": deleted,
        "total_rows_deleted": sum(deleted.values()),
    }
