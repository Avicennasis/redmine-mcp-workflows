"""Reactive workflow observation layer.

Redmine's REST API does not expose ``/workflows.json`` (returns 403 even
for global admins). Instead of pre-fetching the workflow graph, we record
the outcome of every status-change attempt and learn the graph by
observation. See ``docs/workflow-validation.md``.

Phase 4 (issue update) is the primary caller. This module exists in
Phase 2 so the cache helpers and the describe-tracker tool have a stable
import surface to talk to.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient


async def fetch_current_user(client: RedmineClient, cache: SchemaCache) -> dict[str, Any]:
    """Fetch the currently-authenticated user (with memberships) and cache."""
    cached = cache.get_meta_json("current_user")
    if cached is not None:
        return cached
    payload = await client.get("/users/current.json", params={"include": "memberships"})
    user = payload.get("user", {}) if isinstance(payload, dict) else {}
    cache.put_meta_json("current_user", user)
    return user


def role_ids_for_project(user: dict[str, Any], project_id: int) -> list[int]:
    """Extract the current user's role ids for a given project.

    Falls back to an empty list if the user has no membership for that
    project — typical for global admins, who effectively bypass workflow
    role checks but Redmine still applies tracker-level workflow rules.
    """
    out: list[int] = []
    for m in user.get("memberships", []) or []:
        proj = m.get("project") or {}
        if proj.get("id") == project_id:
            for role in m.get("roles", []) or []:
                rid = role.get("id")
                if isinstance(rid, int):
                    out.append(rid)
    return out


def record_outcome(
    cache: SchemaCache,
    *,
    tracker_id: int,
    role_ids: list[int],
    from_status_id: int,
    to_status_id: int,
    outcome: str,
    error_text: str | None = None,
) -> None:
    """Record one observation per role.

    Empty ``role_ids`` (e.g., for global admins with no project membership)
    records under role_id=0 as a generic-admin observation.
    """
    if not role_ids:
        role_ids = [0]
    for rid in role_ids:
        cache.record_workflow_observation(
            tracker_id=tracker_id,
            role_id=rid,
            from_status_id=from_status_id,
            to_status_id=to_status_id,
            outcome=outcome,
            error_text=error_text,
        )
