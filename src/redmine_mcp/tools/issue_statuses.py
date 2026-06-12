"""Issue status listing tool (Redmine ticket #4489).

Tools (1):
  - list_issue_statuses — GET /issue_statuses.json

Read-only reference data.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_issue_statuses(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
) -> dict[str, Any]:
    """List all issue statuses with their is_closed flags."""
    try:
        resp = await client.get("/issue_statuses.json")
    except RedmineAPIError as e:
        return e.as_structured()

    statuses = resp.get("issue_statuses", []) if isinstance(resp, dict) else []
    return {"issue_statuses": statuses, "count": len(statuses), "source": "api"}
