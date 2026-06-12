"""Tracker schema fetcher.

Builds an enriched per-tracker schema by combining:
  - ``/trackers.json`` — all trackers (the only tracker endpoint Redmine exposes)
  - ``/issue_statuses.json`` — global status registry (cached in cache_meta)
  - ``/enumerations/issue_priorities.json`` — global priority registry (cache_meta)

Note that Redmine 6.x does NOT expose ``/trackers/{id}.json`` (returns 404)
or ``/workflows.json`` (returns 403 even for global admins). The workflow
graph is therefore learned reactively — see ``workflow.py`` and
``docs/workflow-validation.md``.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient


async def refresh_global_enumerations(client: RedmineClient, cache: SchemaCache) -> None:
    """Populate ``cache_meta`` with the latest global lookups."""
    statuses = await client.get("/issue_statuses.json")
    cache.put_meta_json("issue_statuses", statuses.get("issue_statuses", []) if isinstance(statuses, dict) else [])

    priorities = await client.get("/enumerations/issue_priorities.json")
    cache.put_meta_json(
        "issue_priorities",
        priorities.get("issue_priorities", []) if isinstance(priorities, dict) else [],
    )

    roles = await client.get("/roles.json")
    cache.put_meta_json("roles", roles.get("roles", []) if isinstance(roles, dict) else [])

    activities = await client.get("/enumerations/time_entry_activities.json")
    cache.put_meta_json(
        "time_entry_activities",
        activities.get("time_entry_activities", []) if isinstance(activities, dict) else [],
    )


async def fetch_all_trackers(client: RedmineClient, cache: SchemaCache) -> list[dict[str, Any]]:
    """Fetch all trackers from Redmine and cache them.

    Returns the bare tracker list. Use :func:`describe_tracker` to get an
    enriched per-tracker schema.
    """
    payload = await client.get("/trackers.json")
    trackers = payload.get("trackers", []) if isinstance(payload, dict) else []
    for t in trackers:
        cache.put_tracker(tracker_id=int(t["id"]), name=t.get("name", ""), schema=t)
    return trackers


async def describe_tracker(
    client: RedmineClient,
    cache: SchemaCache,
    tracker_ident: int | str,
    *,
    include_observations: bool = True,
) -> dict[str, Any]:
    """Return an enriched description of one tracker.

    Triggers a tracker-list refresh + global-enumeration refresh on cache
    miss. The ``observed_workflow`` field is populated from the reactive
    workflow cache (see ``schema/workflow.py``).
    """
    # Resolve the tracker reference. Populate cache if empty.
    tracker_id = cache.resolve_tracker(tracker_ident)
    if tracker_id is None:
        await fetch_all_trackers(client, cache)
        tracker_id = cache.resolve_tracker(tracker_ident)
    if tracker_id is None:
        return {
            "error": "tracker_not_found",
            "hint": f"No tracker matches {tracker_ident!r}.",
            "available": [t["name"] for t in cache.list_trackers()],
        }

    base = cache.get_tracker(tracker_id)
    if base is None:
        await fetch_all_trackers(client, cache)
        base = cache.get_tracker(tracker_id)
    assert base is not None  # noqa: S101 — postcondition guard

    # Make sure global enumerations are warm.
    if cache.get_meta_json("issue_statuses") is None:
        await refresh_global_enumerations(client, cache)

    statuses = cache.get_meta_json("issue_statuses") or []
    priorities = cache.get_meta_json("issue_priorities") or []

    result: dict[str, Any] = {
        "id": tracker_id,
        "name": base.get("name", ""),
        "default_status": base.get("default_status"),
        "description": base.get("description"),
        "available_statuses": statuses,
        "available_priorities": priorities,
        "observation_note": (
            "Workflow knowledge is learned from observed API responses, not "
            "fetched authoritatively (Redmine does not expose /workflows via REST)."
        ),
    }

    # Surface applicable custom fields (lazy-load on cache miss).
    # /custom_fields.json is admin-only; a 403 (or anything else odd) should
    # not break tracker description, just leave the list empty.
    if not cache.list_custom_fields():
        import contextlib

        from . import custom_fields as cf_module
        with contextlib.suppress(Exception):
            await cf_module.refresh_custom_fields(client, cache)
    result["custom_fields"] = cache.list_custom_fields(tracker_id=tracker_id)

    if include_observations:
        observations = cache.list_workflow_observations(tracker_id=tracker_id)
        result["observed_workflow"] = _group_observations_by_role(observations, statuses)

    return result


def _group_observations_by_role(
    observations: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, list[str]]]]:
    """Re-shape flat observations into ``{role_id: {from: {allowed:[], disallowed:[]}}}``."""
    status_name = {s["id"]: s["name"] for s in statuses}
    grouped: dict[str, dict[str, dict[str, list[str]]]] = {}
    for o in observations:
        role_key = str(o["role_id"])
        from_name = status_name.get(o["from_status_id"], f"#{o['from_status_id']}")
        to_name = status_name.get(o["to_status_id"], f"#{o['to_status_id']}")
        bucket = grouped.setdefault(role_key, {}).setdefault(
            from_name, {"allowed_next": [], "disallowed_next": []}
        )
        target = bucket["allowed_next"] if o["outcome"] == "allowed" else bucket["disallowed_next"]
        if to_name not in target:
            target.append(to_name)
    return grouped
