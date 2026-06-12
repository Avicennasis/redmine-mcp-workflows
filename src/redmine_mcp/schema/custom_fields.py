"""Custom-field schema fetcher.

Wraps Redmine's admin-only ``/custom_fields.json`` endpoint and caches
the result. Used by :mod:`redmine_mcp.schema.tracker` to surface
applicable custom fields in ``describe_tracker`` output, and by
:mod:`redmine_mcp.tools.issues` for the ``Difficulty`` default-fill on
create.

Note: ``/custom_fields.json`` requires admin permission. If the endpoint
returns 403 (or anything else odd), callers should handle the error
gracefully — custom-field discovery is enriching information, not
load-bearing for issue create/update. The functions here propagate
exceptions; callers in ``describe_tracker`` wrap the call in try/except.

Shape notes (Redmine 6.x):
  * ``possible_values`` is a list of ``{value, label}`` dicts for
    list-type fields, or empty for text/string fields. We normalize to a
    flat list of strings.
  * ``trackers`` is a list of ``{id, name}`` dicts. Empty/missing means
    "applies to all trackers."
  * There is **no** ``is_for_all`` key in the list response. Project
    scope is signaled by the presence or absence of a ``projects``
    array: present → field is scoped to those projects; absent → field
    applies to all projects.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient


async def refresh_custom_fields(client: RedmineClient, cache: SchemaCache) -> None:
    """Fetch /custom_fields.json and upsert each issue-customized field.

    Idempotent — repeated calls upsert. Skips fields whose
    ``customized_type`` is not ``"issue"`` (e.g., project- or
    time-entry-scoped fields).
    """
    payload = await client.get("/custom_fields.json")
    fields = payload.get("custom_fields", []) if isinstance(payload, dict) else []
    for f in fields:
        if not isinstance(f, dict):
            continue
        if f.get("customized_type") != "issue":
            continue

        raw_values = f.get("possible_values") or []
        possible_values: list[str] = []
        for v in raw_values:
            if isinstance(v, dict) and "value" in v:
                possible_values.append(str(v["value"]))
            elif isinstance(v, str):
                possible_values.append(v)

        trackers = f.get("trackers") or []
        applicable_tracker_ids = [
            int(t["id"]) for t in trackers if isinstance(t, dict) and "id" in t
        ]

        # Redmine 6.x omits 'is_for_all' from /custom_fields.json. Presence of
        # a non-empty 'projects' array means project-scoped; absence (or empty)
        # means for-all-projects.
        projects = f.get("projects")
        for_all_projects = not projects

        cache.put_custom_field(
            field_id=int(f["id"]),
            name=str(f.get("name", "")),
            format_kind=str(f.get("field_format", "")),
            is_required=bool(f.get("is_required", False)),
            default_value=f.get("default_value"),
            possible_values=possible_values,
            applicable_tracker_ids=applicable_tracker_ids,
            for_all_projects=for_all_projects,
        )


async def get_custom_field_by_name(
    client: RedmineClient,
    cache: SchemaCache,
    name: str,
) -> dict[str, Any] | None:
    """Look up a cached custom field by name; refresh on cache miss.

    Returns ``None`` if the field still isn't present after a refresh.
    """
    cached = cache.get_custom_field_by_name(name)
    if cached is not None:
        return cached
    await refresh_custom_fields(client, cache)
    return cache.get_custom_field_by_name(name)
