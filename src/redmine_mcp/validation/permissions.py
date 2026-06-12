"""Role-based authorization checks.

Redmine's REST API doesn't expose per-field role-permission rules, so v0.1
limits this layer to:

  * Detecting global-admin status (``user["admin"] is True``) — these
    users bypass all other role checks.
  * Comparing required-role names against the user's observed memberships
    for a given project.

Most field-level role restrictions surface only as 422s from Redmine.
The reactive workflow layer captures those; this module is the seam for
future expansion (v0.4 plugin support, where additionals/RedmineUP can
expose richer role data).
"""

from __future__ import annotations

from typing import Any

from ..errors import RoleNotAuthorized, StructuredError


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("admin") is True)


def role_names_for_project(user: dict[str, Any] | None, project_id: int) -> list[str]:
    """Return the names of every role this user has on ``project_id``."""
    if not user:
        return []
    out: list[str] = []
    for m in user.get("memberships", []) or []:
        proj = m.get("project") or {}
        if proj.get("id") != project_id:
            continue
        for role in m.get("roles", []) or []:
            name = role.get("name")
            if isinstance(name, str) and name not in out:
                out.append(name)
    return out


def require_role(
    user: dict[str, Any] | None,
    *,
    project_id: int,
    required: list[str],
) -> list[StructuredError]:
    """Return ``[]`` if the user is admin or holds one of ``required``.

    Otherwise returns a single :class:`RoleNotAuthorized` payload.
    """
    if is_admin(user):
        return []
    current = role_names_for_project(user, project_id)
    if any(r in current for r in required):
        return []
    return [RoleNotAuthorized(required=required, current=current)]
