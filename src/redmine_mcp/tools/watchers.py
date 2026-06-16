"""Watcher tools.

Tools (3 — Redmine ticket #2379, "smart-pings" use case):
  - add_watcher    — POST   /issues/{id}/watchers.json
  - remove_watcher — DELETE /issues/{id}/watchers/{user_id}.json
  - list_watchers  — GET    /issues/{id}.json?include=watchers

Redmine has no standalone GET endpoint for an issue's watchers — the list
is exposed via the ``include=watchers`` parameter on the issue payload.
``list_watchers`` lifts that array to a top-level field, mirroring how
``get_journals`` handles journals.

Add/remove are idempotent on the API side: re-adding an existing watcher
returns 200, re-removing a nonexistent one returns 404. We surface the
404 verbatim rather than silently masking it — that lets the caller
distinguish ``the watcher was already gone`` from ``the issue itself
doesn't exist``.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError
from . import issues as issues_module


async def add_watcher(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    issue_id: int,
    user_id: int,
) -> dict[str, Any]:
    """Add ``user_id`` as a watcher of ``issue_id``."""
    try:
        await client.post(
            f"/issues/{issue_id}/watchers.json",
            json={"user_id": user_id},
        )
    except RedmineAPIError as e:
        return e.as_structured()
    return {
        "issue_id": issue_id,
        "user_id": user_id,
        "added": True,
        "source": "api",
    }


async def remove_watcher(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    issue_id: int,
    user_id: int,
) -> dict[str, Any]:
    """Remove ``user_id`` from the watcher list of ``issue_id``."""
    try:
        await client.delete(f"/issues/{issue_id}/watchers/{user_id}.json")
    except RedmineAPIError as e:
        return e.as_structured()
    return {
        "issue_id": issue_id,
        "user_id": user_id,
        "removed": True,
        "source": "api",
    }


async def list_watchers(
    client: RedmineClient,
    cache: SchemaCache,
    issue_id: int,
) -> dict[str, Any]:
    """Return the current watcher list for ``issue_id``.

    Wraps ``issues.get_issue(issue_id, include="watchers")`` and lifts
    the ``watchers`` array (each entry has ``id`` and ``name``) to a
    top-level field so callers don't have to dig through the issue
    payload.
    """
    result = await issues_module.get_issue(client, cache, issue_id, include="watchers")
    if "error" in result:
        return result
    issue = result.get("issue") or {}
    watchers = issue.get("watchers") or []
    return {
        "issue_id": issue_id,
        "watchers": watchers,
        "source": "api",
    }
