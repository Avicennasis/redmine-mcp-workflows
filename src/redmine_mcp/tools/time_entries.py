"""Time-entry CRUD tools.

Tools (4 — Redmine ticket #2377):
  - create_time_entry  (validates hours format, resolves activity name → id)
  - list_time_entries  (paginated; filters by issue/project/user/date range)
  - update_time_entry  (partial; re-validates any supplied hours)
  - delete_time_entry  (DELETE /time_entries/{id}.json)

Hour-format validation is client-side (see ``validation/fields.parse_hours``);
activity names resolve against the cached ``time_entry_activities``
enumeration. Both are pre-flight checks, so a malformed call short-circuits
without round-tripping.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError, StructuredError
from ..schema import tracker as tracker_schema
from ..validation import fields as field_validators


def _validation_response(errors: list[StructuredError]) -> dict[str, Any]:
    return {"error": "validation_failed", "errors": [e.as_dict() for e in errors]}


def _try_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _resolve_activity_id(
    client: RedmineClient, cache: SchemaCache, ident: int | str
) -> int | None:
    """Resolve an activity reference (id, numeric string, or name) to an id."""
    if isinstance(ident, int):
        return ident
    as_int = _try_int(ident)
    if as_int is not None:
        return as_int
    items = cache.get_meta_json("time_entry_activities")
    if items is None:
        await tracker_schema.refresh_global_enumerations(client, cache)
        items = cache.get_meta_json("time_entry_activities") or []
    target = str(ident).strip().lower()
    for item in items:
        if str(item.get("name", "")).lower() == target:
            return _try_int(item.get("id"))
    return None


# ---------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------


async def create_time_entry(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    hours: Any,
    issue_id: int | None = None,
    project_id: int | None = None,
    activity: int | str | None = None,
    spent_on: str | None = None,
    comments: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Create a time entry.

    Args:
        hours: required. Decimal (``2.5``), string decimal (``"2.5"``),
            or ``"H:MM"`` (e.g. ``"2:30"``). Pre-validated client-side.
        issue_id / project_id: exactly one is required (Redmine accepts
            either). If both are passed, ``issue_id`` wins.
        activity: optional id or name (e.g. ``"Development"``). If
            omitted, Redmine uses the project / system default.
        spent_on: optional ``YYYY-MM-DD``; defaults to today server-side.
        comments: optional, max 1024 chars (server enforces).
        user_id: admin-only; otherwise the API ignores it and uses
            current user.
    """
    if issue_id is None and project_id is None:
        return {
            "error": "validation_failed",
            "errors": [
                {
                    "error": "required_field_missing",
                    "hint": "One of issue_id or project_id is required for create_time_entry.",
                    "field": "issue_id_or_project_id",
                    "op": "create_time_entry",
                }
            ],
        }

    parsed_hours, hour_errs = field_validators.validate_hours(hours)
    if hour_errs:
        return _validation_response(hour_errs)

    payload: dict[str, Any] = {"hours": parsed_hours}
    if issue_id is not None:
        payload["issue_id"] = issue_id
    elif project_id is not None:
        payload["project_id"] = project_id

    if activity is not None:
        activity_id = await _resolve_activity_id(client, cache, activity)
        if activity_id is None:
            return {
                "error": "activity_not_found",
                "hint": f"No time-entry activity matches {activity!r}.",
                "activity": activity,
            }
        payload["activity_id"] = activity_id
    if spent_on is not None:
        payload["spent_on"] = spent_on
    if comments is not None:
        payload["comments"] = comments
    if user_id is not None:
        payload["user_id"] = user_id

    try:
        resp = await client.post("/time_entries.json", json={"time_entry": payload})
    except RedmineAPIError as e:
        return e.as_structured()

    entry = resp.get("time_entry") if isinstance(resp, dict) else None
    return {"time_entry": entry, "source": "api"}


async def list_time_entries(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    *,
    issue_id: int | None = None,
    project_id: int | None = None,
    user_id: int | None = None,
    spent_on: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List time entries with optional filters.

    Args:
        issue_id, project_id, user_id: filter by reference id.
        spent_on: exact-date filter (``YYYY-MM-DD``).
        from_date / to_date: date-range filter (Redmine ``from`` / ``to``).
        limit: page size (capped at 100).
        offset: skip the first N results.
    """
    params: dict[str, Any] = {"limit": min(limit, 100), "offset": offset}
    if issue_id is not None:
        params["issue_id"] = issue_id
    if project_id is not None:
        params["project_id"] = project_id
    if user_id is not None:
        params["user_id"] = user_id
    if spent_on is not None:
        params["spent_on"] = spent_on
    if from_date is not None:
        params["from"] = from_date
    if to_date is not None:
        params["to"] = to_date

    payload = await client.get("/time_entries.json", params=params)
    entries = payload.get("time_entries", []) if isinstance(payload, dict) else []
    total = payload.get("total_count", len(entries)) if isinstance(payload, dict) else len(entries)
    return {
        "time_entries": entries,
        "total_count": total,
        "limit": limit,
        "offset": offset,
    }


async def update_time_entry(
    client: RedmineClient,
    cache: SchemaCache,
    time_entry_id: int,
    *,
    hours: Any = None,
    activity: int | str | None = None,
    spent_on: str | None = None,
    comments: str | None = None,
    issue_id: int | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    """Update a time entry. Partial; only supplied fields are sent.

    Hours, when supplied, are pre-validated through the same parser as
    ``create_time_entry``. Activity, when supplied, resolves through the
    cached enumeration.
    """
    payload: dict[str, Any] = {}

    if hours is not None:
        parsed_hours, hour_errs = field_validators.validate_hours(hours)
        if hour_errs:
            return _validation_response(hour_errs)
        payload["hours"] = parsed_hours

    if activity is not None:
        activity_id = await _resolve_activity_id(client, cache, activity)
        if activity_id is None:
            return {
                "error": "activity_not_found",
                "hint": f"No time-entry activity matches {activity!r}.",
                "activity": activity,
            }
        payload["activity_id"] = activity_id

    if spent_on is not None:
        payload["spent_on"] = spent_on
    if comments is not None:
        payload["comments"] = comments
    if issue_id is not None:
        payload["issue_id"] = issue_id
    if project_id is not None:
        payload["project_id"] = project_id

    if not payload:
        return {
            "error": "nothing_to_update",
            "hint": "Provide at least one updatable field.",
            "time_entry_id": time_entry_id,
        }

    try:
        await client.put(
            f"/time_entries/{time_entry_id}.json",
            json={"time_entry": payload},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    # Re-fetch for the canonical post-update shape — Redmine returns 204 on PUT.
    try:
        verify = await client.get(f"/time_entries/{time_entry_id}.json")
    except RedmineAPIError as e:
        return e.as_structured()
    entry = verify.get("time_entry") if isinstance(verify, dict) else None
    return {"time_entry": entry, "source": "api"}


async def delete_time_entry(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    time_entry_id: int,
) -> dict[str, Any]:
    """Delete a time entry."""
    try:
        await client.delete(f"/time_entries/{time_entry_id}.json")
    except RedmineAPIError as e:
        return e.as_structured()
    return {"deleted": True, "time_entry_id": time_entry_id, "source": "api"}
