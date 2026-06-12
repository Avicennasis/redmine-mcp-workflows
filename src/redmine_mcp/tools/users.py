"""User tools (Redmine ticket #4482).

Tools (2):
  - get_user    — GET /users/{id}.json  (or /users/current.json)
  - list_users  — GET /users.json  (admin only)

Read-only surface. Admin write tools (create/update/delete) are deferred
to passthrough per the ticket spec.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def get_user(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    *,
    user_id: int | str,
    include: str | None = None,
) -> dict[str, Any]:
    """Fetch a user by id, or ``"current"`` for the API user.

    Args:
        user_id: numeric id, or the string ``"current"``.
        include: comma-separated includes (``memberships``, ``groups``).
    """
    path = f"/users/{user_id}.json"
    params: dict[str, Any] = {}
    if include:
        params["include"] = include

    try:
        resp = await client.get(path, params=params or None)
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "user_not_found",
                "hint": f"User {user_id} not found.",
                "user_id": user_id,
            }
        return e.as_structured()

    user = resp.get("user") if isinstance(resp, dict) else None
    if not user:
        return {
            "error": "user_not_found",
            "hint": f"User {user_id} not found.",
            "user_id": user_id,
        }
    return {"user": user, "source": "api"}


async def list_users(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    name: str | None = None,
    group_id: int | None = None,
    status: int | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List users (admin only).

    Args:
        name: filter by login, firstname, lastname, or mail.
        group_id: filter by group membership.
        status: filter by user status (0=anonymous, 1=active, 2=registered,
            3=locked).
        limit: page size (capped at 100).
        offset: skip the first N results.
    """
    params: dict[str, Any] = {"limit": min(limit, 100), "offset": offset}
    if name:
        params["name"] = name
    if group_id is not None:
        params["group_id"] = group_id
    if status is not None:
        params["status"] = status

    try:
        resp = await client.get("/users.json", params=params)
    except RedmineAPIError as e:
        return e.as_structured()

    if not isinstance(resp, dict):
        return {
            "users": [],
            "total_count": 0,
            "limit": limit,
            "offset": offset,
            "source": "api",
        }

    return {
        "users": resp.get("users", []),
        "total_count": resp.get("total_count", 0),
        "limit": resp.get("limit", limit),
        "offset": resp.get("offset", offset),
        "source": "api",
    }
