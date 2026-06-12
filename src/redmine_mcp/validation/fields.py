"""Required-field and custom-field validators.

The validators run BEFORE the API call. A returned list of
:class:`StructuredError` payloads is non-empty when validation fails.
The caller (Phase 4 issue tools) returns the list as a structured MCP
response without sending the API request.

Custom-field type/format validation is best-effort in v0.1: Redmine's
``/custom_fields.json`` is admin-only on most installs, so we cache only
what we see and validate primarily for *shape* and *id existence*. Tighter
type validation lands in v0.2.
"""

from __future__ import annotations

from typing import Any

from ..errors import (
    CustomFieldShapeError,
    CustomFieldUnknown,
    IssueHeld,
    RequiredFieldMissing,
    StructuredError,
    TimeEntryHoursInvalid,
)

# Redmine's hard requirements for issue creation (regardless of tracker).
_BASE_REQUIRED_FIELDS_FOR_CREATE = ("project", "tracker", "subject")


def validate_required(payload: dict[str, Any], op: str = "create") -> list[StructuredError]:
    """Return errors for any base-required field that is absent or empty."""
    errors: list[StructuredError] = []
    if op != "create":
        # Update operations don't have base required fields — Redmine accepts partials.
        return errors
    for name in _BASE_REQUIRED_FIELDS_FOR_CREATE:
        val = payload.get(name)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(RequiredFieldMissing(field_name=name, op=op))
    return errors


def parse_hours(value: Any) -> tuple[float | None, str | None]:
    """Parse a hours value to a non-negative float.

    Returns ``(hours, error_reason)``. Exactly one side is non-None.

    Accepted forms:
      * numeric (``int`` / ``float``) — used as-is
      * decimal string — ``"2.5"``, ``"0.25"``
      * H:MM string — ``"2:30"`` (= 2.5 hours)
    """
    if value is None:
        return None, "value is required"
    if isinstance(value, bool):
        return None, "boolean is not a valid hours value"
    if isinstance(value, int | float):
        f = float(value)
        if f < 0:
            return None, f"must be non-negative, got {f}"
        return f, None
    s = str(value).strip()
    if not s:
        return None, "value is empty"
    if ":" in s:
        parts = s.split(":")
        if len(parts) != 2:
            return None, "H:MM must have exactly one ':'"
        try:
            h = int(parts[0])
            m = int(parts[1])
        except ValueError:
            return None, "H and MM must be integers"
        if h < 0:
            return None, f"H must be non-negative, got {h}"
        if m < 0 or m >= 60:
            return None, f"MM must be 0-59, got {m}"
        return h + m / 60.0, None
    try:
        f = float(s)
    except ValueError:
        return None, "must be numeric or 'H:MM'"
    if f < 0:
        return None, f"must be non-negative, got {f}"
    return f, None


def validate_hours(value: Any) -> tuple[float | None, list[StructuredError]]:
    """Validate-and-parse helper. Returns ``(parsed_hours_or_None, errors)``."""
    parsed, reason = parse_hours(value)
    if reason is not None:
        return None, [TimeEntryHoursInvalid(value=value, reason=reason)]
    return parsed, []


def validate_custom_fields(
    payload: dict[str, Any],
    *,
    known_field_ids: list[int] | None = None,
    tracker_name: str | None = None,
) -> list[StructuredError]:
    """Validate the shape and id-presence of any ``custom_fields`` entry.

    Each entry must be a dict with ``id`` and ``value`` keys. If
    ``known_field_ids`` is provided (Phase 5+ when we have the lookup),
    each id must be in that set; otherwise this check is skipped.
    """
    errors: list[StructuredError] = []
    raw = payload.get("custom_fields")
    if raw is None:
        return errors
    if not isinstance(raw, list):
        errors.append(CustomFieldShapeError(entry=raw, reason="must be a list"))
        return errors

    for entry in raw:
        if not isinstance(entry, dict):
            errors.append(
                CustomFieldShapeError(entry=entry, reason="entry must be a dict")
            )
            continue
        if "id" not in entry:
            errors.append(
                CustomFieldShapeError(entry=entry, reason="missing 'id' key")
            )
            continue
        if "value" not in entry:
            errors.append(
                CustomFieldShapeError(entry=entry, reason="missing 'value' key")
            )
            continue
        if known_field_ids is not None:
            try:
                fid = int(entry["id"])
            except (TypeError, ValueError):
                errors.append(
                    CustomFieldShapeError(entry=entry, reason="'id' must be an integer")
                )
                continue
            if fid not in known_field_ids:
                errors.append(
                    CustomFieldUnknown(
                        field_id=fid,
                        tracker=tracker_name,
                        known_ids=known_field_ids,
                    )
                )
    return errors


HELD_FIELD_NAME = "Held"
HELD_UNTIL_FIELD_NAME = "Held Until"


def check_held_gate(issue: dict[str, Any]) -> IssueHeld | None:
    """Return ``IssueHeld`` if the issue has a non-empty Held custom field.

    The caller decides when to invoke this — typically only when the
    target status is a closed status.
    """
    custom_fields = issue.get("custom_fields") or []
    held_value: str = ""
    held_until_value: str | None = None

    for cf in custom_fields:
        name = cf.get("name", "")
        if name == HELD_FIELD_NAME:
            held_value = (cf.get("value") or "").strip()
        elif name == HELD_UNTIL_FIELD_NAME:
            held_until_value = (cf.get("value") or "").strip() or None

    if not held_value:
        return None

    return IssueHeld(
        issue_id=issue.get("id", 0),
        held_reason=held_value,
        held_until=held_until_value,
    )
