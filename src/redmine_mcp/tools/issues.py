"""Issue-lifecycle tools.

Tools (5):
  - get_issue (read-only, no validation)
  - create_issue (validates required + custom-fields, resolves project/tracker/priority/status)
  - update_issue (the marquee — pre-flight workflow check via cache, post-API record_outcome)
  - close_issue (convenience wrapper; resolves the closed status from cache_meta)
  - search_issues (paginated, free-text on subject, project/status filters)

Validation flow for ``update_issue`` (see ``docs/workflow-validation.md``):

  1. Fetch current issue (gives tracker_id, project_id, current_status_id).
  2. If status is changing: resolve user's role_ids for the project, look up
     ``(tracker, role, from, to)`` in the reactive workflow cache. A
     ``disallowed`` hit → return :class:`WorkflowTransitionDisallowed`
     without sending the PUT.
  3. Run base+custom-field validators.
  4. Send the PUT.
  5. Record an observation: ``allowed`` on 2xx, ``disallowed`` on 422 with
     a status-related error.
  6. Re-fetch the issue and return it.
"""

from __future__ import annotations

import contextlib
from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import (
    RedmineAPIError,
    StructuredError,
    WorkflowTransitionDisallowed,
)
from ..schema import project as project_schema
from ..schema import tracker as tracker_schema
from ..schema import workflow as workflow_module
from ..validation import fields as field_validators
from ..validation import transitions

DEFAULT_INCLUDE = "attachments,journals,relations,watchers"

DIFFICULTY_FIELD_NAME = "Difficulty"
DIFFICULTY_DEFAULT_VALUE = "Unclassified"

HELD_FIELD_NAME = "Held"
HELD_UNTIL_FIELD_NAME = "Held Until"


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _validation_response(errors: list[StructuredError]) -> dict[str, Any]:
    """Wrap a non-empty list of validators' errors into a single response."""
    return {"error": "validation_failed", "errors": [e.as_dict() for e in errors]}


def _try_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _resolve_project_id(
    client: RedmineClient, cache: SchemaCache, ident: int | str
) -> int | None:
    """Resolve a project reference (id, numeric string, slug, or display name).

    Lookup order:
      1. Numeric id (int or stringy int) → return as-is.
      2. Cache by identifier slug.
      3. ``describe_project`` (fetches by slug, caches on success).
      4. Cache by display name (case-insensitive) — handles the natural
         get→create round-trip pattern where callers pass ``project.name``
         from a prior ``redmine_get_issue`` response.
      5. Refresh the project list and try the display-name match again.

    Returns ``None`` when no path resolves; callers translate that into
    a structured ``project_not_found`` error.
    """
    if isinstance(ident, int):
        return ident
    as_int = _try_int(ident)
    if as_int is not None:
        return as_int
    ident_str = str(ident)
    cached = cache.get_project(ident_str)
    if cached is not None:
        return _try_int(cached.get("id"))
    fetched = await project_schema.describe_project(client, cache, ident_str)
    if isinstance(fetched, dict) and not fetched.get("error"):
        return _try_int(fetched.get("id"))
    # Slug lookup failed — try display name (cached, then refreshed list).
    by_name = cache.get_project_by_name(ident_str)
    if by_name is not None:
        return _try_int(by_name.get("id"))
    listing = await project_schema.list_projects(client, limit=100)
    target = ident_str.strip().lower()
    for project in listing.get("projects", []):
        if str(project.get("name", "")).strip().lower() == target:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                cache.put_project(
                    project_id=int(project["id"]),
                    identifier=project.get("identifier", project["name"]),
                    schema=project,
                )
            return _try_int(project.get("id"))
    return None


async def _resolve_tracker_id(
    client: RedmineClient, cache: SchemaCache, ident: int | str
) -> int | None:
    """Resolve a tracker reference (id or name) to a numeric id."""
    resolved = cache.resolve_tracker(ident)
    if resolved is not None:
        return resolved
    await tracker_schema.fetch_all_trackers(client, cache)
    return cache.resolve_tracker(ident)


async def _resolve_enum_id(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    kind: str,
    ident: int | str,
) -> int | None:
    """Resolve a status or priority reference. ``kind`` is the cache_meta key."""
    if isinstance(ident, int):
        return ident
    as_int = _try_int(ident)
    if as_int is not None:
        return as_int
    items = cache.get_meta_json(kind)
    if items is None:
        await tracker_schema.refresh_global_enumerations(client, cache)
        items = cache.get_meta_json(kind) or []
    target = str(ident).strip().lower()
    for item in items:
        if str(item.get("name", "")).lower() == target:
            return _try_int(item.get("id"))
    return None


def _422_error_text(body: Any) -> str:
    """Best-effort extraction of a human-readable error string from a 422 body."""
    if isinstance(body, dict):
        errs = body.get("errors")
        if isinstance(errs, list) and errs:
            return "; ".join(str(e) for e in errs)
    return str(body)[:200]


def _looks_like_workflow_disallowed(body: Any) -> bool:
    """True if a 422 body indicates a status-transition rejection."""
    if not isinstance(body, dict):
        return False
    errs = body.get("errors")
    if not isinstance(errs, list):
        return False
    for e in errs:
        s = str(e).lower()
        if "status" in s and ("not allowed" in s or "is invalid" in s):
            return True
    return False


def _name_for_status(statuses: list[dict[str, Any]], status_id: int) -> str:
    for s in statuses:
        if s.get("id") == status_id:
            return s.get("name", f"#{status_id}")
    return f"#{status_id}"


def _name_for_role(roles: list[dict[str, Any]], role_id: int) -> str:
    if role_id == 0:
        return "global-admin"
    for r in roles:
        if r.get("id") == role_id:
            return r.get("name", f"role#{role_id}")
    return f"role#{role_id}"


def _has_difficulty_entry(custom_fields: list[dict[str, Any]] | None, field_id: int) -> bool:
    """True if ``custom_fields`` already carries a Difficulty entry.

    Accepts entries identified either by name (``"Difficulty"``) or by
    the resolved numeric id, since callers may use either shape.
    """
    if not custom_fields:
        return False
    for entry in custom_fields:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") == DIFFICULTY_FIELD_NAME:
            return True
        if entry.get("id") == field_id:
            return True
    return False


def _merge_custom_field(
    custom_fields: list[dict[str, Any]] | None,
    field_id: int,
    value: str,
    *,
    field_name: str = DIFFICULTY_FIELD_NAME,
) -> list[dict[str, Any]]:
    """Add or replace a single custom-field entry in the list.

    Drops any prior entry that matches by id OR by name (avoids leaving
    a stale name-keyed duplicate when the caller passed one and we're
    overriding by id).
    """
    out: list[dict[str, Any]] = []
    for entry in custom_fields or []:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        if entry.get("id") == field_id or entry.get("name") == field_name:
            continue
        out.append(entry)
    out.append({"id": field_id, "value": value})
    return out


async def _apply_difficulty(
    client: RedmineClient,
    cache: SchemaCache,
    custom_fields: list[dict[str, Any]] | None,
    difficulty: str | None,
    *,
    default_fill: bool,
) -> list[dict[str, Any]] | None:
    """Translate the ``difficulty`` convenience param into a custom_fields entry.

    Returns the (possibly modified) ``custom_fields`` list:
      * If ``difficulty`` is supplied → overrides any existing Difficulty entry.
      * Else, if ``default_fill`` and no Difficulty entry is present → fills with
        :data:`DIFFICULTY_DEFAULT_VALUE`.
      * Else → returns ``custom_fields`` unchanged.

    Silently returns ``custom_fields`` unchanged if the Difficulty field
    isn't discoverable in Redmine (e.g. legacy fleet, or admin-only
    ``/custom_fields.json`` returned 403).
    """
    if difficulty is None and not default_fill:
        return custom_fields

    # Late import: avoid circular module load at startup.
    from ..schema import custom_fields as cf_module

    try:
        field = await cf_module.get_custom_field_by_name(client, cache, DIFFICULTY_FIELD_NAME)
    except Exception:  # noqa: BLE001 — enrichment, not load-bearing
        field = None
    if field is None:
        return custom_fields

    field_id = int(field["id"])
    if difficulty is not None:
        return _merge_custom_field(custom_fields, field_id, difficulty)
    # default-fill path
    if _has_difficulty_entry(custom_fields, field_id):
        return custom_fields
    return _merge_custom_field(custom_fields, field_id, DIFFICULTY_DEFAULT_VALUE)


async def _apply_held(
    client: RedmineClient,
    cache: SchemaCache,
    custom_fields: list[dict[str, Any]] | None,
    held: bool | None,
    held_until: str | None,
) -> list[dict[str, Any]] | None:
    """Translate ``held`` / ``held_until`` convenience params into custom_fields entries.

    Returns the (possibly modified) ``custom_fields`` list.
    Silently returns ``custom_fields`` unchanged if the Held fields
    aren't discoverable in Redmine.
    """
    if held is None and held_until is None:
        return custom_fields

    from ..schema import custom_fields as cf_module

    if held is not None:
        try:
            field = await cf_module.get_custom_field_by_name(client, cache, HELD_FIELD_NAME)
        except Exception:
            field = None
        if field is not None:
            custom_fields = _merge_custom_field(
                custom_fields,
                int(field["id"]),
                "1" if held else "",
                field_name=HELD_FIELD_NAME,
            )

    if held_until is not None:
        try:
            field = await cf_module.get_custom_field_by_name(client, cache, HELD_UNTIL_FIELD_NAME)
        except Exception:
            field = None
        if field is not None:
            custom_fields = _merge_custom_field(
                custom_fields,
                int(field["id"]),
                held_until,
                field_name=HELD_UNTIL_FIELD_NAME,
            )

    return custom_fields


# ---------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------


async def get_issue(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity with the other tools
    issue_id: int,
    *,
    include: str | None = None,
) -> dict[str, Any]:
    """Fetch a single issue. No validation, no caching — straight passthrough."""
    params = {"include": include if include is not None else DEFAULT_INCLUDE}
    payload = await client.get(f"/issues/{issue_id}.json", params=params)
    issue = payload.get("issue") if isinstance(payload, dict) else None
    if not issue:
        return {
            "error": "issue_not_found",
            "hint": f"Issue {issue_id} not found.",
            "issue_id": issue_id,
        }
    return {"issue": issue, "source": "api"}


async def create_issue(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    project: int | str,
    tracker: int | str,
    subject: str,
    description: str | None = None,
    priority: int | str | None = None,
    status: int | str | None = None,
    assigned_to_id: int | None = None,
    custom_fields: list[dict[str, Any]] | None = None,
    difficulty: str | None = None,
    held: bool | None = None,
    held_until: str | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
    done_ratio: int | None = None,
) -> dict[str, Any]:
    """Create an issue with pre-flight validation and id resolution.

    The ``difficulty`` parameter is a convenience for the global
    ``Difficulty`` custom field (engagement-mode signal:
    ``Unclassified`` / ``Easy`` / ``Normal`` / ``Hard``). When supplied,
    it overrides any matching entry in ``custom_fields``. When omitted,
    and no ``Difficulty`` entry is present in ``custom_fields``, the field
    is default-filled with ``"Unclassified"`` so auto-callers don't trip
    the required-field validation. Silently no-ops if the Difficulty
    field is not discoverable in Redmine.

    The ``held`` and ``held_until`` parameters are convenience shortcuts
    for the ``Held`` (boolean/checkbox) and ``Held Until`` (date) custom
    fields. ``held=True`` marks the issue as held; ``held_until`` sets
    the date (ISO-8601). Silently no-ops if the fields aren't configured.
    """
    validation_view: dict[str, Any] = {
        "project": project,
        "tracker": tracker,
        "subject": subject,
    }
    if custom_fields is not None:
        validation_view["custom_fields"] = custom_fields
    errs: list[StructuredError] = []
    errs.extend(field_validators.validate_required(validation_view, op="create"))
    errs.extend(field_validators.validate_custom_fields(validation_view, known_field_ids=None))
    if errs:
        return _validation_response(errs)

    custom_fields = await _apply_difficulty(
        client, cache, custom_fields, difficulty, default_fill=True
    )
    custom_fields = await _apply_held(client, cache, custom_fields, held, held_until)

    project_id = await _resolve_project_id(client, cache, project)
    if project_id is None:
        return {
            "error": "project_not_found",
            "hint": f"No project matches {project!r}.",
            "project": project,
        }
    tracker_id = await _resolve_tracker_id(client, cache, tracker)
    if tracker_id is None:
        return {
            "error": "tracker_not_found",
            "hint": f"No tracker matches {tracker!r}.",
            "tracker": tracker,
        }

    api_payload: dict[str, Any] = {
        "project_id": project_id,
        "tracker_id": tracker_id,
        "subject": subject,
    }
    if description is not None:
        api_payload["description"] = description
    if priority is not None:
        priority_id = await _resolve_enum_id(client, cache, kind="issue_priorities", ident=priority)
        if priority_id is None:
            return {
                "error": "priority_not_found",
                "hint": f"No priority matches {priority!r}.",
                "priority": priority,
            }
        api_payload["priority_id"] = priority_id
    if status is not None:
        status_id = await _resolve_enum_id(client, cache, kind="issue_statuses", ident=status)
        if status_id is None:
            return {
                "error": "status_not_found",
                "hint": f"No status matches {status!r}.",
                "status": status,
            }
        api_payload["status_id"] = status_id
    if assigned_to_id is not None:
        api_payload["assigned_to_id"] = assigned_to_id
    if custom_fields is not None:
        api_payload["custom_fields"] = custom_fields
    if due_date is not None:
        api_payload["due_date"] = due_date
    if start_date is not None:
        api_payload["start_date"] = start_date
    if done_ratio is not None:
        api_payload["done_ratio"] = done_ratio

    try:
        resp = await client.post("/issues.json", json={"issue": api_payload})
    except RedmineAPIError as e:
        return e.as_structured()

    issue = resp.get("issue") if isinstance(resp, dict) else None
    return {"issue": issue, "source": "api"}


async def update_issue(
    client: RedmineClient,
    cache: SchemaCache,
    issue_id: int,
    *,
    subject: str | None = None,
    description: str | None = None,
    status: int | str | None = None,
    priority: int | str | None = None,
    assigned_to_id: int | None = None,
    notes: str | None = None,
    fixed_version_id: int | str | None = None,
    custom_fields: list[dict[str, Any]] | None = None,
    difficulty: str | None = None,
    held: bool | None = None,
    held_until: str | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
    done_ratio: int | None = None,
) -> dict[str, Any]:
    """Update an issue with reactive workflow validation.

    On a status-changing call:
      * Pre-flight against the cache; a learned ``disallowed`` short-circuits.
      * Post-API: record the outcome (``allowed`` on success; ``disallowed``
        with the error text on a status-related 422).

    The ``difficulty`` parameter is a convenience for the global
    ``Difficulty`` custom field. Unlike ``create_issue``, ``update_issue``
    does **not** default-fill — passing nothing means "don't change
    Difficulty"; default-filling would silently overwrite user-set
    values on every unrelated update.

    The ``held`` and ``held_until`` parameters are convenience shortcuts
    for the ``Held`` and ``Held Until`` custom fields. ``held=True`` marks
    the issue as held; ``held=False`` clears it. ``held_until`` sets the
    date (ISO-8601). Omitting either means "don't change."
    """
    try:
        current = await get_issue(client, cache, issue_id, include="attachments,journals")
    except RedmineAPIError as e:
        return e.as_structured()
    if "error" in current:
        return current
    issue = current["issue"]
    tracker_id = _try_int(issue.get("tracker", {}).get("id"))
    project_id = _try_int(issue.get("project", {}).get("id"))
    current_status_id = _try_int(issue.get("status", {}).get("id"))
    if tracker_id is None or project_id is None or current_status_id is None:
        return {
            "error": "issue_state_unavailable",
            "hint": "Current issue is missing tracker/project/status — cannot validate.",
            "issue_id": issue_id,
        }

    validation_view: dict[str, Any] = {}
    if custom_fields is not None:
        validation_view["custom_fields"] = custom_fields
    errs: list[StructuredError] = []
    errs.extend(field_validators.validate_required(validation_view, op="update"))
    errs.extend(field_validators.validate_custom_fields(validation_view, known_field_ids=None))
    if errs:
        return _validation_response(errs)

    # Translate the difficulty convenience param. NO default-fill on update —
    # the contract is "change this field"; default-filling would silently
    # overwrite user-set values on every unrelated update.
    custom_fields = await _apply_difficulty(
        client, cache, custom_fields, difficulty, default_fill=False
    )
    custom_fields = await _apply_held(client, cache, custom_fields, held, held_until)

    target_status_id: int | None = None
    if status is not None:
        target_status_id = await _resolve_enum_id(
            client, cache, kind="issue_statuses", ident=status
        )
        if target_status_id is None:
            return {
                "error": "status_not_found",
                "hint": f"No status matches {status!r}.",
                "status": status,
            }

    status_changing = target_status_id is not None and target_status_id != current_status_id

    if status_changing and target_status_id is not None:
        statuses = cache.get_meta_json("issue_statuses") or []
        target_is_closed = any(
            s.get("id") == target_status_id and s.get("is_closed") for s in statuses
        )
        if target_is_closed:
            held_err = field_validators.check_held_gate(issue)
            if held_err is not None:
                return held_err.as_dict()

    if status_changing:
        user = await workflow_module.fetch_current_user(client, cache)
        role_ids = workflow_module.role_ids_for_project(user, project_id)
        hit = transitions.is_disallowed(
            cache,
            tracker_id=tracker_id,
            role_ids=role_ids,
            from_status_id=current_status_id,
            to_status_id=target_status_id,
        )
        if hit is not None:
            statuses = cache.get_meta_json("issue_statuses") or []
            roles = cache.get_meta_json("roles") or []
            allowed_ids = transitions.allowed_next(
                cache,
                tracker_id=tracker_id,
                role_ids=role_ids,
                from_status_id=current_status_id,
            )
            err = WorkflowTransitionDisallowed(
                tracker=issue.get("tracker", {}).get("name", str(tracker_id)),
                from_status=_name_for_status(statuses, current_status_id),
                to_status=_name_for_status(statuses, target_status_id),
                user_role=_name_for_role(roles, hit.role_id),
                allowed_next_states=[_name_for_status(statuses, s) for s in allowed_ids],
                observation_basis="learned",
                last_error_text=hit.last_error_text,
                observed_at=hit.observed_at,
            )
            return err.as_dict()

    target_priority_id: int | None = None
    if priority is not None:
        target_priority_id = await _resolve_enum_id(
            client, cache, kind="issue_priorities", ident=priority
        )
        if target_priority_id is None:
            return {
                "error": "priority_not_found",
                "hint": f"No priority matches {priority!r}.",
                "priority": priority,
            }

    api_payload: dict[str, Any] = {}
    if subject is not None:
        api_payload["subject"] = subject
    if description is not None:
        api_payload["description"] = description
    if target_status_id is not None:
        api_payload["status_id"] = target_status_id
    if target_priority_id is not None:
        api_payload["priority_id"] = target_priority_id
    if assigned_to_id is not None:
        api_payload["assigned_to_id"] = assigned_to_id
    if notes is not None:
        api_payload["notes"] = notes
    if fixed_version_id is not None:
        api_payload["fixed_version_id"] = fixed_version_id
    if custom_fields is not None:
        api_payload["custom_fields"] = custom_fields
    if due_date is not None:
        api_payload["due_date"] = due_date
    if start_date is not None:
        api_payload["start_date"] = start_date
    if done_ratio is not None:
        api_payload["done_ratio"] = done_ratio

    if not api_payload:
        return {
            "error": "nothing_to_update",
            "hint": "Provide at least one updatable field.",
            "issue_id": issue_id,
        }

    try:
        await client.put(f"/issues/{issue_id}.json", json={"issue": api_payload})
    except RedmineAPIError as e:
        if status_changing:
            user = await workflow_module.fetch_current_user(client, cache)
            role_ids = workflow_module.role_ids_for_project(user, project_id)
            if e.status_code == 422 and _looks_like_workflow_disallowed(e.body):
                workflow_module.record_outcome(
                    cache,
                    tracker_id=tracker_id,
                    role_ids=role_ids,
                    from_status_id=current_status_id,
                    to_status_id=target_status_id,  # type: ignore[arg-type]
                    outcome="disallowed",
                    error_text=_422_error_text(e.body),
                )
        return e.as_structured()

    try:
        # Pull children too when a status change was requested so we can
        # detect silent no-ops caused by "block_descendants_issues_closing"
        # and name the blocking subtasks in the hint.
        re_fetch_include = (
            "attachments,journals,children" if status_changing else "attachments,journals"
        )
        final = await get_issue(client, cache, issue_id, include=re_fetch_include)
    except RedmineAPIError as e:
        return e.as_structured()

    if status_changing and isinstance(final, dict) and "issue" in final:
        actual_status_id = _try_int(final["issue"].get("status", {}).get("id"))
        if actual_status_id is not None and actual_status_id != target_status_id:
            # Silent no-op: PUT returned 2xx, note (if any) was journaled, but
            # the status_id didn't move. Common cause is Redmine's
            # "block_descendants_issues_closing" with open subtasks; other
            # causes include custom workflow rules not yet in the cache.
            statuses = cache.get_meta_json("issue_statuses") or []
            actual_name = _name_for_status(statuses, actual_status_id)
            requested_name = _name_for_status(statuses, target_status_id)  # type: ignore[arg-type]
            target_is_closed = any(
                s.get("id") == target_status_id and s.get("is_closed") for s in statuses
            )
            children = final["issue"].get("children") or []
            hint = (
                f"Redmine accepted the PUT but did not apply the status change "
                f"(requested {requested_name!r}, still {actual_name!r})."
            )
            if children and target_is_closed:
                refs = ", ".join(f"#{c['id']}" for c in children if "id" in c)
                hint += (
                    f" Issue has subtasks: {refs}. If Redmine's "
                    f"'block_descendants_issues_closing' setting is enabled, "
                    f"the parent cannot close until every subtask closes."
                )
            else:
                hint += (
                    " Likely causes: a workflow rule not yet observed by the "
                    "cache, insufficient permissions for this transition, or "
                    "an admin-level setting (e.g. block_descendants_issues_closing) "
                    "blocking the change."
                )
            # Don't record this as "allowed" — the workflow may or may not allow it;
            # we only know that *this* attempt didn't move the status.
            return {
                "error": "status_change_silently_ignored",
                "hint": hint,
                "requested_status_id": target_status_id,
                "requested_status": requested_name,
                "actual_status_id": actual_status_id,
                "actual_status": actual_name,
                "issue": final["issue"],
            }

        # Status genuinely moved → record allowed outcome.
        user = await workflow_module.fetch_current_user(client, cache)
        role_ids = workflow_module.role_ids_for_project(user, project_id)
        workflow_module.record_outcome(
            cache,
            tracker_id=tracker_id,
            role_ids=role_ids,
            from_status_id=current_status_id,
            to_status_id=target_status_id,  # type: ignore[arg-type]
            outcome="allowed",
        )

    return final


async def close_issue(
    client: RedmineClient,
    cache: SchemaCache,
    issue_id: int,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Set status to the first status flagged ``is_closed`` (defaults to id 5)."""
    statuses = cache.get_meta_json("issue_statuses")
    if statuses is None:
        await tracker_schema.refresh_global_enumerations(client, cache)
        statuses = cache.get_meta_json("issue_statuses") or []
    closed_id: int | None = None
    for s in statuses:
        if s.get("is_closed"):
            closed_id = _try_int(s.get("id"))
            if closed_id is not None:
                break
    if closed_id is None:
        closed_id = 5  # Standard Redmine "Closed" id.

    result = await update_issue(client, cache, issue_id, status=closed_id, notes=note)
    if isinstance(result, dict) and result.get("error") == "workflow_transition_disallowed":
        result = dict(result)
        from_st = result.get("from_status")
        allowed = result.get("allowed_next_states") or []
        if allowed:
            result["hint"] = (
                f"Workflow does not allow direct closure from {from_st!r}; "
                f"try transitioning to one of {allowed} first."
            )
        else:
            result["hint"] = (
                f"Workflow does not allow direct closure from {from_st!r}; "
                "no allowed next states observed yet."
            )
    return result


async def delete_issue(
    client: RedmineClient,
    issue_id: int,
) -> dict[str, Any]:
    """Permanently delete an issue. Cannot be undone."""
    try:
        issue_resp = await client.get(f"/issues/{issue_id}.json")
        subject = issue_resp.get("issue", {}).get("subject", "(unknown)")
    except RedmineAPIError:
        subject = "(unknown)"

    try:
        await client.delete(f"/issues/{issue_id}.json")
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "issue_not_found",
                "hint": f"Issue #{issue_id} not found.",
                "issue_id": issue_id,
            }
        return e.as_structured()
    return {
        "issue_id": issue_id,
        "subject": subject,
        "deleted": True,
        "source": "api",
    }


async def search_issues(
    client: RedmineClient,
    cache: SchemaCache,
    query: str | None = None,
    *,
    project: int | str | None = None,
    status: int | str | None = None,
    query_id: int | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """Search/list issues with optional substring + project/status filters.

    ``query_id`` invokes a Redmine *saved query* by its numeric id; Redmine
    merges saved-query filters with any explicit URL params (project,
    status, etc.) layered on the request.
    """
    params: dict[str, Any] = {"limit": min(limit, 100), "offset": offset}
    if query_id is not None:
        params["query_id"] = query_id
    if query:
        # ~ prefix asks Redmine for substring match on the field.
        params["subject"] = f"~{query}"
    if project is not None:
        project_id = await _resolve_project_id(client, cache, project)
        if project_id is None:
            return {
                "error": "project_not_found",
                "hint": f"No project matches {project!r}.",
                "project": project,
            }
        params["project_id"] = project_id
    if status is not None:
        # Redmine accepts "open" / "closed" / "*" as special tokens — match
        # case-sensitively so a named status like "Closed" still goes through
        # the cache resolver below.
        if isinstance(status, str) and status in {"open", "closed", "*"}:
            params["status_id"] = status
        else:
            status_id = await _resolve_enum_id(client, cache, kind="issue_statuses", ident=status)
            if status_id is None:
                return {
                    "error": "status_not_found",
                    "hint": f"No status matches {status!r}.",
                    "status": status,
                }
            params["status_id"] = status_id

    payload = await client.get("/issues.json", params=params)
    issues = payload.get("issues", []) if isinstance(payload, dict) else []
    total = payload.get("total_count", len(issues)) if isinstance(payload, dict) else len(issues)
    return {
        "issues": issues,
        "total_count": total,
        "limit": limit,
        "offset": offset,
        "query": query,
    }
