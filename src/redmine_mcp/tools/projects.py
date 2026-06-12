"""Project lifecycle tools (Redmine ticket #4483).

Tools (5):
  - create_project    — POST   /projects.json
  - update_project    — PUT    /projects/{id}.json
  - delete_project    — DELETE /projects/{id}.json
  - archive_project   — PUT    /projects/{id}/archive.json   (Redmine 5.0+)
  - unarchive_project — PUT    /projects/{id}/unarchive.json (Redmine 5.0+)

Existing read tools (list_projects, describe_project) live in
``schema/project.py`` and ``tools/discovery.py``.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def create_project(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    name: str,
    identifier: str,
    description: str | None = None,
    homepage: str | None = None,
    is_public: bool | None = None,
    parent_id: int | None = None,
    inherit_members: bool | None = None,
    tracker_ids: list[int] | None = None,
    enabled_module_names: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new project."""
    if not name or not name.strip():
        return {
            "error": "validation_failed",
            "hint": "Field 'name' is required for create_project.",
        }
    if not identifier or not identifier.strip():
        return {
            "error": "validation_failed",
            "hint": "Field 'identifier' is required for create_project.",
        }

    body: dict[str, Any] = {"name": name, "identifier": identifier}
    if description is not None:
        body["description"] = description
    if homepage is not None:
        body["homepage"] = homepage
    if is_public is not None:
        body["is_public"] = is_public
    if parent_id is not None:
        body["parent_id"] = parent_id
    if inherit_members is not None:
        body["inherit_members"] = inherit_members
    if tracker_ids is not None:
        body["tracker_ids"] = tracker_ids
    if enabled_module_names is not None:
        body["enabled_module_names"] = enabled_module_names

    try:
        resp = await client.post("/projects.json", json={"project": body})
    except RedmineAPIError as e:
        return e.as_structured()

    project = resp.get("project") if isinstance(resp, dict) else None
    return {"project": project, "source": "api"}


async def update_project(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
    name: str | None = None,
    description: str | None = None,
    homepage: str | None = None,
    is_public: bool | None = None,
    parent_id: int | None = None,
    inherit_members: bool | None = None,
    tracker_ids: list[int] | None = None,
    enabled_module_names: list[str] | None = None,
) -> dict[str, Any]:
    """Update a project. Partial — only supplied fields are sent."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if homepage is not None:
        body["homepage"] = homepage
    if is_public is not None:
        body["is_public"] = is_public
    if parent_id is not None:
        body["parent_id"] = parent_id
    if inherit_members is not None:
        body["inherit_members"] = inherit_members
    if tracker_ids is not None:
        body["tracker_ids"] = tracker_ids
    if enabled_module_names is not None:
        body["enabled_module_names"] = enabled_module_names

    if not body:
        return {
            "error": "nothing_to_update",
            "hint": "Provide at least one updatable field.",
            "project_id": project_id,
        }

    try:
        await client.put(f"/projects/{project_id}.json", json={"project": body})
    except RedmineAPIError as e:
        return e.as_structured()

    try:
        resp = await client.get(f"/projects/{project_id}.json")
    except RedmineAPIError as e:
        return {"updated": True, "project_id": project_id, "source": "api"}

    project = resp.get("project") if isinstance(resp, dict) else None
    return {"project": project, "source": "api"}


async def delete_project(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
) -> dict[str, Any]:
    """Delete a project. Permanent — no soft-delete in Redmine."""
    try:
        await client.delete(f"/projects/{project_id}.json")
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "project_not_found",
                "hint": f"Project {project_id} not found.",
                "project_id": project_id,
            }
        return e.as_structured()
    return {"project_id": project_id, "deleted": True, "source": "api"}


async def archive_project(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
) -> dict[str, Any]:
    """Archive a project (Redmine 5.0+)."""
    try:
        await client.put(f"/projects/{project_id}/archive.json", json={})
    except RedmineAPIError as e:
        return e.as_structured()
    return {"project_id": project_id, "archived": True, "source": "api"}


async def unarchive_project(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
) -> dict[str, Any]:
    """Unarchive a project (Redmine 5.0+)."""
    try:
        await client.put(f"/projects/{project_id}/unarchive.json", json={})
    except RedmineAPIError as e:
        return e.as_structured()
    return {"project_id": project_id, "unarchived": True, "source": "api"}
