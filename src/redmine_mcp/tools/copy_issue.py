"""Issue-copy tool (Redmine ticket #40871).

Redmine has **no REST copy endpoint** — ``GET/POST /issues/:id/copy`` 404s
(verified against the target instance). The web UI's copy form works by
re-posting the source issue's fields with a ``copy_from`` parameter that the
JSON API does not honour. So this tool reconstructs the copy from a read of
the source issue and a normal ``POST /issues.json``, then optionally replays
the parts the API does not copy for us: watchers, relations and subtasks.

Standard fields can be overridden per call (``project``, ``tracker``,
``subject``, ``priority``, ``description``, ``assigned_to_id``, dates,
``done_ratio``, ``custom_fields``); anything omitted is inherited from the
source.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError, StructuredError
from ..schema import project as project_schema
from ..validation import fields as field_validators
from . import issues as issues_module

# Depth guard for recursive sub-task copying — a self-referential subtask
# chain must not loop forever.
_MAX_SUBTASK_DEPTH = 5


def _try_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _source_custom_fields(issue: dict[str, Any]) -> list[dict[str, Any]]:
    """Map the source issue's custom-field values to writable entries."""
    out: list[dict[str, Any]] = []
    for cf in issue.get("custom_fields") or []:
        if not isinstance(cf, dict):
            continue
        fid = _try_int(cf.get("id"))
        if fid is None:
            continue
        out.append({"id": fid, "value": cf.get("value")})
    return out


async def _copy_one(
    client: RedmineClient,
    cache: SchemaCache,
    source: dict[str, Any],
    *,
    project_id: int,
    tracker_id: int,
    subject: str,
    overrides: dict[str, Any],
    parent_issue_id: int | None,
    copy_subtasks: bool,
    copy_watchers: bool,
    copy_relations: bool,
    depth: int,
) -> dict[str, Any]:
    api_payload: dict[str, Any] = {
        "project_id": project_id,
        "tracker_id": tracker_id,
        "subject": subject,
    }
    if parent_issue_id is not None:
        api_payload["parent_issue_id"] = parent_issue_id

    # Field-by-field: override wins, else inherit from source.
    source_priority = (source.get("priority") or {}).get("id")
    source_assignee = (source.get("assigned_to") or {}).get("id")
    inherited = {
        "description": source.get("description"),
        "priority_id": overrides.get("priority_id", source_priority),
        "assigned_to_id": overrides.get("assigned_to_id", source_assignee),
        "due_date": source.get("due_date"),
        "start_date": source.get("start_date"),
        "done_ratio": source.get("done_ratio"),
        "custom_fields": overrides.get("custom_fields", _source_custom_fields(source)),
    }
    for key, value in {**inherited, **overrides}.items():
        if value is not None:
            api_payload[key] = value

    resp = await client.post("/issues.json", json={"issue": api_payload})
    new_issue = resp.get("issue") if isinstance(resp, dict) else None
    new_id = _try_int((new_issue or {}).get("id"))

    copied = {"watchers": 0, "relations": 0, "subtasks": 0}

    if new_id is not None and copy_watchers:
        for watcher in source.get("watchers") or []:
            wid = _try_int((watcher or {}).get("id"))
            if wid is None:
                continue
            try:
                await client.post(f"/issues/{new_id}/watchers.json", json={"user_id": wid})
                copied["watchers"] += 1
            except RedmineAPIError:
                # A watcher who cannot see the new project is skippable.
                continue

    if new_id is not None and copy_relations:
        copied["relations"] = await _copy_relations(
            client, source, source_id=_try_int(source.get("id")), new_id=new_id
        )

    if new_id is not None and copy_subtasks and depth < _MAX_SUBTASK_DEPTH:
        for child in source.get("children") or []:
            child_id = _try_int((child or {}).get("id"))
            if child_id is None:
                continue
            child_source = await issues_module.get_issue(
                client, cache, child_id, include="watchers,relations,children"
            )
            if "error" in child_source:
                continue
            copied["subtasks"] += 1
            await _copy_one(
                client,
                cache,
                child_source["issue"],
                project_id=project_id,
                tracker_id=_try_int((child_source["issue"].get("tracker") or {}).get("id"))
                or tracker_id,
                subject=str(child_source["issue"].get("subject") or ""),
                overrides={},
                parent_issue_id=new_id,
                copy_subtasks=copy_subtasks,
                copy_watchers=copy_watchers,
                copy_relations=copy_relations,
                depth=depth + 1,
            )

    return {"issue": new_issue, "copied": copied, "source": "api"}


async def _copy_relations(
    client: RedmineClient,
    source: dict[str, Any],
    *,
    source_id: int | None,
    new_id: int,
) -> int:
    """Recreate the source's issue-relations on the copy. Returns the count."""
    count = 0
    for relation in source.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        a = _try_int(relation.get("issue_id"))
        b = _try_int(relation.get("issue_to_id"))
        if a is None or b is None:
            continue
        # The other end is whichever id is not the source issue.
        other = b if a == source_id else a
        if other == new_id:
            continue
        body: dict[str, Any] = {
            "issue_to_id": other,
            "relation_type": relation.get("relation_type", "relates"),
        }
        delay = relation.get("delay")
        if delay is not None:
            body["delay"] = delay
        try:
            await client.post(f"/issues/{new_id}/relations.json", json={"relation": body})
            count += 1
        except RedmineAPIError:
            # Relations to issues the caller cannot see are skippable.
            continue
    return count


async def copy_issue(
    client: RedmineClient,
    cache: SchemaCache,
    issue_id: int,
    *,
    project: int | str | None = None,
    tracker: int | str | None = None,
    subject: str | None = None,
    priority: int | str | None = None,
    description: str | None = None,
    assigned_to_id: int | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
    done_ratio: int | None = None,
    custom_fields: list[dict[str, Any]] | None = None,
    copy_subtasks: bool = False,
    copy_watchers: bool = False,
    copy_relations: bool = False,
) -> dict[str, Any]:
    """Duplicate an issue, optionally into another project/tracker.

    Returns ``{"issue", "copied": {watchers, relations, subtasks}, "source"}``
    or a structured error. The copy is created in the source's project and
    tracker unless overridden.
    """
    fetched = await issues_module.get_issue(
        client, cache, issue_id, include="journals,relations,watchers,attachments,children"
    )
    if "error" in fetched:
        return fetched
    source = fetched["issue"]

    project_id = (
        await project_schema.resolve_project_id(client, cache, project)
        if project is not None
        else _try_int((source.get("project") or {}).get("id"))
    )
    if project_id is None:
        return {
            "error": "project_not_found",
            "hint": f"No project matches {project!r}.",
            "project": project,
        }

    tracker_id = (
        await issues_module._resolve_tracker_id(client, cache, tracker)
        if tracker is not None
        else _try_int((source.get("tracker") or {}).get("id"))
    )
    if tracker_id is None:
        return {
            "error": "tracker_not_found",
            "hint": f"No tracker matches {tracker!r}.",
            "tracker": tracker,
        }

    final_subject = (
        subject if subject is not None else f"Copy of {source.get('subject', '')}".strip()
    )

    errs: list[StructuredError] = []
    validation_view: dict[str, Any] = {
        "project": project_id,
        "tracker": tracker_id,
        "subject": final_subject,
    }
    if custom_fields is not None:
        validation_view["custom_fields"] = custom_fields
    errs.extend(field_validators.validate_required(validation_view, op="create"))
    errs.extend(field_validators.validate_custom_fields(validation_view, known_field_ids=None))
    if errs:
        return {"error": "validation_failed", "errors": [e.as_dict() for e in errs]}

    overrides: dict[str, Any] = {}
    if description is not None:
        overrides["description"] = description
    if due_date is not None:
        overrides["due_date"] = due_date
    if start_date is not None:
        overrides["start_date"] = start_date
    if done_ratio is not None:
        overrides["done_ratio"] = done_ratio
    if assigned_to_id is not None:
        overrides["assigned_to_id"] = assigned_to_id
    if custom_fields is not None:
        overrides["custom_fields"] = custom_fields
    if priority is not None:
        priority_id = await issues_module._resolve_enum_id(
            client, cache, kind="issue_priorities", ident=priority
        )
        if priority_id is None:
            return {
                "error": "priority_not_found",
                "hint": f"No priority matches {priority!r}.",
                "priority": priority,
            }
        overrides["priority_id"] = priority_id

    return await _copy_one(
        client,
        cache,
        source,
        project_id=project_id,
        tracker_id=tracker_id,
        subject=final_subject,
        overrides=overrides,
        parent_issue_id=None,
        copy_subtasks=copy_subtasks,
        copy_watchers=copy_watchers,
        copy_relations=copy_relations,
        depth=0,
    )
