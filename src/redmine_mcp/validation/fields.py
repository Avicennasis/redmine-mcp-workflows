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
    CustomFieldValueInvalid,
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
            errors.append(CustomFieldShapeError(entry=entry, reason="entry must be a dict"))
            continue
        if "id" not in entry:
            errors.append(CustomFieldShapeError(entry=entry, reason="missing 'id' key"))
            continue
        if "value" not in entry:
            errors.append(CustomFieldShapeError(entry=entry, reason="missing 'value' key"))
            continue
        if known_field_ids is not None:
            try:
                fid = int(entry["id"])
            except (TypeError, ValueError):
                errors.append(CustomFieldShapeError(entry=entry, reason="'id' must be an integer"))
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


def _custom_field_id(
    entry: dict[str, Any],
    name_to_id: dict[str, int],
) -> int | None:
    """Resolve an entry's field id from its ``id`` or ``name`` key."""
    try:
        return int(entry["id"])
    except (KeyError, TypeError, ValueError):
        pass
    name = entry.get("name")
    if isinstance(name, str):
        return name_to_id.get(name)
    return None


def correct_custom_field_values(
    custom_fields: list[dict[str, Any]] | None,
    *,
    enum_values_by_id: dict[int, list[str]] | None = None,
    name_to_id: dict[str, int] | None = None,
    field_names_by_id: dict[int, str] | None = None,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]], list[StructuredError]]:
    """Case-correct enum custom-field values to Redmine's canonical casing.

    LLMs routinely emit the right value in the wrong case (``"high"`` for
    ``"High"``). Redmine rejects that outright, so we fuzzy-match against
    the field's cached ``possible_values`` and rewrite to the canonical
    casing before the request goes out.

    Returns ``(corrected, corrections, errors)``:

      * ``corrected`` — the list to send to Redmine. Only entries whose
        value was rewritten differ from the input; every other entry is
        returned untouched.
      * ``corrections`` — one record per rewrite:
        ``{"field_id", "field_name", "from", "to", "message"}``.
      * ``errors`` — one :class:`CustomFieldValueInvalid` per value that
        matched no possible value (or matched ambiguously).

    Only scalar non-empty strings are checked, and only for fields whose
    ``possible_values`` are known to the cache. Text/date/numeric fields
    (empty ``possible_values``) and empty strings (clearing a field) pass
    through untouched — an empty cache must never turn a write into a
    rejection.
    """
    if not custom_fields:
        return custom_fields, [], []
    enum_values_by_id = enum_values_by_id or {}
    name_to_id = name_to_id or {}
    field_names_by_id = field_names_by_id or {}

    corrected: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    errors: list[StructuredError] = []

    for entry in custom_fields:
        if not isinstance(entry, dict):
            corrected.append(entry)
            continue

        field_id = _custom_field_id(entry, name_to_id)
        possible = enum_values_by_id.get(field_id) if field_id is not None else None
        value = entry.get("value")

        if not possible or not isinstance(value, str) or not value.strip():
            corrected.append(entry)
            continue
        if value in possible:
            corrected.append(entry)
            continue

        matches = [v for v in possible if v.casefold() == value.casefold()]
        field_name = (
            entry.get("name")
            or (field_names_by_id.get(field_id) if field_id is not None else None)
            or None
        )
        if len(matches) == 1:
            canonical = matches[0]
            corrected.append({**entry, "value": canonical})
            corrections.append(
                {
                    "field_id": field_id,
                    "field_name": field_name,
                    "from": value,
                    "to": canonical,
                    "message": f"corrected {value!r} -> {canonical!r}",
                }
            )
        else:
            corrected.append(entry)
            errors.append(
                CustomFieldValueInvalid(
                    field_id=field_id if field_id is not None else 0,
                    field_name=field_name,
                    value=value,
                    possible_values=possible,
                    reason="ambiguous" if len(matches) > 1 else "no_match",
                )
            )

    return corrected, corrections, errors


HELD_FIELD_NAME = "Held"
HELD_UNTIL_FIELD_NAME = "Held Until"


def _matches_field(cf: dict[str, Any], field_id: int | None, name: str) -> bool:
    """Match a custom-field entry by id when configured, else by exact name."""
    if field_id is not None:
        return cf.get("id") == field_id
    return cf.get("name") == name


def check_held_gate(
    issue: dict[str, Any],
    *,
    held_field_id: int | None = None,
    held_until_field_id: int | None = None,
) -> IssueHeld | None:
    """Return ``IssueHeld`` if the issue has a non-empty Held custom field.

    The caller decides when to invoke this — typically only when the
    target status is a closed status.

    ``held_field_id`` / ``held_until_field_id`` may pin the fields by id
    (``REDMINE_MCP_HELD_FIELD_ID`` / ``..._HELD_UNTIL_FIELD_ID``). When unset
    the fields are matched by their exact English names, which a renamed or
    localized Redmine will not satisfy.
    """
    custom_fields = issue.get("custom_fields") or []
    held_value: str = ""
    held_until_value: str | None = None

    for cf in custom_fields:
        if _matches_field(cf, held_field_id, HELD_FIELD_NAME):
            held_value = (cf.get("value") or "").strip()
        elif _matches_field(cf, held_until_field_id, HELD_UNTIL_FIELD_NAME):
            held_until_value = (cf.get("value") or "").strip() or None

    if not held_value:
        return None

    return IssueHeld(
        issue_id=issue.get("id", 0),
        held_reason=held_value,
        held_until=held_until_value,
    )
