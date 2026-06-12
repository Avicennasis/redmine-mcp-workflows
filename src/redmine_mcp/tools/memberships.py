"""Project membership tools (Redmine ticket #4484).

Tools (4):
  - list_memberships  — GET    /projects/{id}/memberships.json
  - add_membership    — POST   /projects/{id}/memberships.json
  - update_membership — PUT    /memberships/{id}.json
  - remove_membership — DELETE /memberships/{id}.json
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_memberships(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List project members (paginated)."""
    params: dict[str, Any] = {"limit": min(limit, 100), "offset": offset}

    try:
        resp = await client.get(
            f"/projects/{project_id}/memberships.json", params=params,
        )
    except RedmineAPIError as e:
        return e.as_structured()

    if not isinstance(resp, dict):
        return {
            "memberships": [],
            "total_count": 0,
            "limit": limit,
            "offset": offset,
            "project_id": project_id,
            "source": "api",
        }

    return {
        "memberships": resp.get("memberships", []),
        "total_count": resp.get("total_count", 0),
        "limit": resp.get("limit", limit),
        "offset": resp.get("offset", offset),
        "project_id": project_id,
        "source": "api",
    }


async def add_membership(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
    user_id: int,
    role_ids: list[int],
) -> dict[str, Any]:
    """Add a member to a project.

    Args:
        project_id: project id or slug.
        user_id: user or group id.
        role_ids: list of role ids to assign.
    """
    if not role_ids:
        return {
            "error": "validation_failed",
            "hint": "role_ids is required and must be non-empty.",
        }

    body = {"user_id": user_id, "role_ids": role_ids}

    try:
        resp = await client.post(
            f"/projects/{project_id}/memberships.json",
            json={"membership": body},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    membership = resp.get("membership") if isinstance(resp, dict) else None
    return {"membership": membership, "source": "api"}


async def update_membership(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    membership_id: int,
    role_ids: list[int],
) -> dict[str, Any]:
    """Update a membership's roles. Only role_ids is editable."""
    if not role_ids:
        return {
            "error": "validation_failed",
            "hint": "role_ids is required and must be non-empty.",
        }

    try:
        await client.put(
            f"/memberships/{membership_id}.json",
            json={"membership": {"role_ids": role_ids}},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    try:
        resp = await client.get(f"/memberships/{membership_id}.json")
        membership = resp.get("membership") if isinstance(resp, dict) else None
        return {"membership": membership, "source": "api"}
    except RedmineAPIError:
        return {"membership_id": membership_id, "updated": True, "source": "api"}


async def remove_membership(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    membership_id: int,
) -> dict[str, Any]:
    """Remove a membership. Inherited memberships can't be deleted."""
    try:
        await client.delete(f"/memberships/{membership_id}.json")
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "membership_not_found",
                "hint": f"Membership {membership_id} not found.",
                "membership_id": membership_id,
            }
        return e.as_structured()
    return {"membership_id": membership_id, "removed": True, "source": "api"}
