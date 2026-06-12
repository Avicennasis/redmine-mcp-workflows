"""Role tools (Redmine ticket #4488).

Tools (2):
  - list_roles — GET /roles.json
  - get_role   — GET /roles/{id}.json

Read-only, admin-oriented.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_roles(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
) -> dict[str, Any]:
    """List all roles."""
    try:
        resp = await client.get("/roles.json")
    except RedmineAPIError as e:
        return e.as_structured()

    roles = resp.get("roles", []) if isinstance(resp, dict) else []
    return {"roles": roles, "count": len(roles), "source": "api"}


async def get_role(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    role_id: int,
) -> dict[str, Any]:
    """Fetch a role by id, including its permissions list."""
    try:
        resp = await client.get(f"/roles/{role_id}.json")
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "role_not_found",
                "hint": f"Role {role_id} not found.",
                "role_id": role_id,
            }
        return e.as_structured()

    role = resp.get("role") if isinstance(resp, dict) else None
    if not role:
        return {
            "error": "role_not_found",
            "hint": f"Role {role_id} not found.",
            "role_id": role_id,
        }
    return {"role": role, "source": "api"}
