"""Issue category tools (Redmine ticket #4486).

Tools (4):
  - list_issue_categories  — GET    /projects/{id}/issue_categories.json
  - create_issue_category  — POST   /projects/{id}/issue_categories.json
  - update_issue_category  — PUT    /issue_categories/{id}.json
  - delete_issue_category  — DELETE /issue_categories/{id}.json
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_issue_categories(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
) -> dict[str, Any]:
    """List issue categories for a project."""
    try:
        resp = await client.get(f"/projects/{project_id}/issue_categories.json")
    except RedmineAPIError as e:
        return e.as_structured()

    cats = resp.get("issue_categories", []) if isinstance(resp, dict) else []
    return {
        "issue_categories": cats,
        "count": len(cats),
        "project_id": project_id,
        "source": "api",
    }


async def create_issue_category(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
    name: str,
    assigned_to_id: int | None = None,
) -> dict[str, Any]:
    """Create an issue category on a project."""
    if not name or not name.strip():
        return {
            "error": "validation_failed",
            "hint": "Field 'name' is required for create_issue_category.",
        }

    body: dict[str, Any] = {"name": name}
    if assigned_to_id is not None:
        body["assigned_to_id"] = assigned_to_id

    try:
        resp = await client.post(
            f"/projects/{project_id}/issue_categories.json",
            json={"issue_category": body},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    cat = resp.get("issue_category") if isinstance(resp, dict) else None
    return {"issue_category": cat, "source": "api"}


async def update_issue_category(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    category_id: int,
    name: str | None = None,
    assigned_to_id: int | None = None,
) -> dict[str, Any]:
    """Update an issue category."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if assigned_to_id is not None:
        body["assigned_to_id"] = assigned_to_id

    if not body:
        return {
            "error": "nothing_to_update",
            "hint": "Provide at least one updatable field (name, assigned_to_id).",
            "category_id": category_id,
        }

    try:
        await client.put(
            f"/issue_categories/{category_id}.json",
            json={"issue_category": body},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    return {"category_id": category_id, "updated": True, "source": "api"}


async def delete_issue_category(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    category_id: int,
    reassign_to_id: int | None = None,
) -> dict[str, Any]:
    """Delete an issue category.

    Args:
        category_id: numeric category id.
        reassign_to_id: optional category id to reassign affected issues to.
    """
    path = f"/issue_categories/{category_id}.json"
    if reassign_to_id is not None:
        path = f"/issue_categories/{category_id}.json?reassign_to_id={int(reassign_to_id)}"

    try:
        await client.delete(path)
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "category_not_found",
                "hint": f"Issue category {category_id} not found.",
                "category_id": category_id,
            }
        return e.as_structured()
    return {"category_id": category_id, "deleted": True, "source": "api"}
