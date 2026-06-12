"""Versions / milestones CRUD tools (Redmine ticket #2382).

Tools (6):
  - list_versions          — GET    /projects/{p}/versions.json
  - get_version            — GET    /versions/{id}.json
  - create_version         — POST   /projects/{p}/versions.json
  - update_version         — PUT    /versions/{id}.json
  - delete_version         — DELETE /versions/{id}.json
  - assign_issue_to_version — convenience wrapper over update_issue
                              (sets ``fixed_version_id`` on an issue)

Status enumeration:
  Redmine constrains version status to ``open``, ``locked``, ``closed``.
  We validate client-side so a typo fails fast with a helpful hint, not
  with a generic 422 from the server.

Sharing enumeration:
  ``none``, ``descendants``, ``hierarchy``, ``tree``, ``system``. Same
  rationale as status — validate client-side.

Date format:
  ``due_date`` must be ``YYYY-MM-DD``; checked with a tight regex (no
  ``datetime.strptime`` because we don't need the full parse, just the
  shape — and Redmine itself does the calendar validation).
"""

from __future__ import annotations

import re
from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError
from . import issues as issues_module

ALLOWED_STATUSES: tuple[str, ...] = ("open", "locked", "closed")
ALLOWED_SHARINGS: tuple[str, ...] = (
    "none", "descendants", "hierarchy", "tree", "system",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validation_error(hint: str, *, field: str = "name") -> dict[str, Any]:
    return {
        "error": "validation_failed",
        "errors": [
            {
                "error": "required_field_missing",
                "hint": hint,
                "field": field,
                "op": "version_write",
            }
        ],
    }


def _check_status(status: str | None) -> dict[str, Any] | None:
    if status is None:
        return None
    if status not in ALLOWED_STATUSES:
        return {
            "error": "version_status_unknown",
            "hint": f"Status {status!r} is not one of {list(ALLOWED_STATUSES)}.",
            "status": status,
            "allowed_statuses": list(ALLOWED_STATUSES),
        }
    return None


def _check_sharing(sharing: str | None) -> dict[str, Any] | None:
    if sharing is None:
        return None
    if sharing not in ALLOWED_SHARINGS:
        return {
            "error": "version_sharing_unknown",
            "hint": f"Sharing {sharing!r} is not one of {list(ALLOWED_SHARINGS)}.",
            "sharing": sharing,
            "allowed_sharings": list(ALLOWED_SHARINGS),
        }
    return None


def _check_date(due_date: str | None) -> dict[str, Any] | None:
    if due_date is None:
        return None
    if not _DATE_RE.match(due_date):
        return {
            "error": "version_date_invalid",
            "hint": (
                f"due_date {due_date!r} is not in YYYY-MM-DD format. "
                "Redmine performs the calendar check; we just enforce the shape."
            ),
            "due_date": due_date,
        }
    return None


async def list_versions(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    project: int | str,
) -> dict[str, Any]:
    """List all versions defined on a project."""
    try:
        payload = await client.get(f"/projects/{project}/versions.json")
    except RedmineAPIError as e:
        return e.as_structured()
    items = payload.get("versions", []) if isinstance(payload, dict) else []
    return {"project": project, "versions": items, "source": "api"}


async def get_version(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    version_id: int,
) -> dict[str, Any]:
    """Fetch one version by id."""
    try:
        payload = await client.get(f"/versions/{version_id}.json")
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "version_not_found",
                "hint": f"Version {version_id} not found.",
                "version_id": version_id,
            }
        return e.as_structured()
    version = payload.get("version") if isinstance(payload, dict) else None
    if not version:
        return {
            "error": "version_not_found",
            "hint": f"Version {version_id} not found.",
            "version_id": version_id,
        }
    return {"version": version, "source": "api"}


async def create_version(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project: int | str,
    name: str,
    description: str | None = None,
    status: str | None = None,
    due_date: str | None = None,
    sharing: str | None = None,
    wiki_page_title: str | None = None,
) -> dict[str, Any]:
    """Create a new version on a project."""
    if not name or not name.strip():
        return _validation_error("Field 'name' is required for create_version.")
    if (err := _check_status(status)) is not None:
        return err
    if (err := _check_sharing(sharing)) is not None:
        return err
    if (err := _check_date(due_date)) is not None:
        return err

    body: dict[str, Any] = {"name": name}
    if description is not None:
        body["description"] = description
    if status is not None:
        body["status"] = status
    if due_date is not None:
        body["due_date"] = due_date
    if sharing is not None:
        body["sharing"] = sharing
    if wiki_page_title is not None:
        body["wiki_page_title"] = wiki_page_title

    try:
        resp = await client.post(
            f"/projects/{project}/versions.json", json={"version": body},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    version = resp.get("version") if isinstance(resp, dict) else None
    return {"version": version, "source": "api"}


async def update_version(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    version_id: int,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    due_date: str | None = None,
    sharing: str | None = None,
    wiki_page_title: str | None = None,
) -> dict[str, Any]:
    """Update a version. Partial — only supplied fields are sent."""
    if (err := _check_status(status)) is not None:
        return err
    if (err := _check_sharing(sharing)) is not None:
        return err
    if (err := _check_date(due_date)) is not None:
        return err

    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if status is not None:
        body["status"] = status
    if due_date is not None:
        body["due_date"] = due_date
    if sharing is not None:
        body["sharing"] = sharing
    if wiki_page_title is not None:
        body["wiki_page_title"] = wiki_page_title

    if not body:
        return {
            "error": "nothing_to_update",
            "hint": "Provide at least one updatable field.",
            "version_id": version_id,
        }

    try:
        await client.put(f"/versions/{version_id}.json", json={"version": body})
    except RedmineAPIError as e:
        return e.as_structured()

    return await get_version(client, cache, version_id)


async def delete_version(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    version_id: int,
) -> dict[str, Any]:
    """Delete a version by id. Permanent — no soft-delete in Redmine.

    A 422 typically means the version is still referenced by issues; the
    caller should re-target those issues' ``fixed_version_id`` first.
    """
    try:
        await client.delete(f"/versions/{version_id}.json")
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "version_not_found",
                "hint": f"Version {version_id} not found.",
                "version_id": version_id,
            }
        return e.as_structured()
    return {"version_id": version_id, "deleted": True, "source": "api"}


async def assign_issue_to_version(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    issue_id: int,
    version_id: int,
) -> dict[str, Any]:
    """Set (or clear) an issue's target version.

    Pass ``version_id=0`` to unassign — Redmine recognizes the empty
    string sentinel for clearing ``fixed_version_id``.
    """
    payload: int | str = version_id if version_id else ""
    return await issues_module.update_issue(
        client, cache, issue_id, fixed_version_id=payload,
    )
