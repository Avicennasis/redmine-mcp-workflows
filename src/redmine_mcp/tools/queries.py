"""Saved queries listing tool (Redmine ticket #4491).

Tools (1):
  - list_queries — GET /queries.json

Read-only. Lists available saved/custom queries. Use the returned
query ids with ``redmine_search_issues(query_id=...)`` to execute them.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_queries(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str | None = None,
) -> dict[str, Any]:
    """List available saved queries.

    Args:
        project_id: optional project id or slug to scope queries.
            ``None`` lists all visible queries.
    """
    path = "/queries.json"
    if project_id is not None:
        path = f"/projects/{project_id}/queries.json"

    try:
        resp = await client.get(path)
    except RedmineAPIError as e:
        return e.as_structured()

    queries = resp.get("queries", []) if isinstance(resp, dict) else []
    return {
        "queries": queries,
        "count": len(queries),
        "source": "api",
    }
