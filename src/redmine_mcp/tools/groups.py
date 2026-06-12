"""Group management tools (Redmine ticket #4485).

Tools (7):
  - list_groups      — GET    /groups.json
  - get_group        — GET    /groups/{id}.json
  - create_group     — POST   /groups.json
  - update_group     — PUT    /groups/{id}.json
  - delete_group     — DELETE /groups/{id}.json
  - add_group_user   — POST   /groups/{id}/users.json
  - remove_group_user — DELETE /groups/{id}/users/{user_id}.json

Admin-only surface.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_groups(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
) -> dict[str, Any]:
    """List all groups (admin only)."""
    try:
        resp = await client.get("/groups.json")
    except RedmineAPIError as e:
        return e.as_structured()

    groups = resp.get("groups", []) if isinstance(resp, dict) else []
    return {"groups": groups, "count": len(groups), "source": "api"}


async def get_group(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    group_id: int,
    include: str | None = None,
) -> dict[str, Any]:
    """Fetch a group by id.

    Args:
        group_id: numeric group id.
        include: comma-separated includes (``users``, ``memberships``).
    """
    params: dict[str, Any] = {}
    if include:
        params["include"] = include

    try:
        resp = await client.get(f"/groups/{group_id}.json", params=params or None)
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "group_not_found",
                "hint": f"Group {group_id} not found.",
                "group_id": group_id,
            }
        return e.as_structured()

    group = resp.get("group") if isinstance(resp, dict) else None
    if not group:
        return {
            "error": "group_not_found",
            "hint": f"Group {group_id} not found.",
            "group_id": group_id,
        }
    return {"group": group, "source": "api"}


async def create_group(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    name: str,
    user_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Create a group (admin only)."""
    if not name or not name.strip():
        return {
            "error": "validation_failed",
            "hint": "Field 'name' is required for create_group.",
        }

    body: dict[str, Any] = {"name": name}
    if user_ids:
        body["user_ids"] = user_ids

    try:
        resp = await client.post("/groups.json", json={"group": body})
    except RedmineAPIError as e:
        return e.as_structured()

    group = resp.get("group") if isinstance(resp, dict) else None
    return {"group": group, "source": "api"}


async def update_group(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    group_id: int,
    name: str | None = None,
) -> dict[str, Any]:
    """Update a group's name."""
    if name is None:
        return {
            "error": "nothing_to_update",
            "hint": "Provide at least one updatable field (name).",
            "group_id": group_id,
        }

    try:
        await client.put(
            f"/groups/{group_id}.json",
            json={"group": {"name": name}},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    return await get_group(client, cache, group_id=group_id)


async def delete_group(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    group_id: int,
) -> dict[str, Any]:
    """Delete a group (admin only). Permanent."""
    try:
        await client.delete(f"/groups/{group_id}.json")
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "group_not_found",
                "hint": f"Group {group_id} not found.",
                "group_id": group_id,
            }
        return e.as_structured()
    return {"group_id": group_id, "deleted": True, "source": "api"}


async def add_group_user(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    group_id: int,
    user_id: int,
) -> dict[str, Any]:
    """Add a user to a group."""
    try:
        await client.post(
            f"/groups/{group_id}/users.json",
            json={"user_id": user_id},
        )
    except RedmineAPIError as e:
        return e.as_structured()
    return {
        "group_id": group_id,
        "user_id": user_id,
        "added": True,
        "source": "api",
    }


async def remove_group_user(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    group_id: int,
    user_id: int,
) -> dict[str, Any]:
    """Remove a user from a group."""
    try:
        await client.delete(f"/groups/{group_id}/users/{user_id}.json")
    except RedmineAPIError as e:
        return e.as_structured()
    return {
        "group_id": group_id,
        "user_id": user_id,
        "removed": True,
        "source": "api",
    }
