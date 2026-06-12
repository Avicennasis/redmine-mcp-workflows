"""Comments and journal tools.

Tools (3):
  - add_comment      — append a journal entry (note + optional private flag) to an issue
  - get_journals     — read-only fetch of an issue's structured journal entries
  - update_journal   — edit an existing journal's notes in place (Redmine 5.0+)

Implementation notes:
  * Redmine has no dedicated "post comment" endpoint. A comment IS a journal
    entry created by ``PUT /issues/{id}.json`` with a ``notes`` field (and
    optional ``private_notes`` flag). We send the PUT directly rather than
    going through ``issues.update_issue`` to skip the pre-fetch overhead —
    a comment-only call has no status pre-flight to run.
  * ``get_journals`` reuses ``issues.get_issue`` with ``include="journals"``
    so we pick up its consistent error handling and shape.
  * ``update_journal`` uses ``PUT /journals/{id}.json`` (Redmine 5.0+) to
    edit the ``notes`` field of an existing journal entry in place. Only
    the API user's own journals can be edited (unless the user has the
    ``edit_issue_notes`` permission). Setting notes to empty on a journal
    with no ``details`` deletes the journal entirely.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError
from . import issues as issues_module


async def add_comment(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity with other tools
    issue_id: int,
    note: str,
    *,
    private: bool = False,
) -> dict[str, Any]:
    """Post a comment (journal entry) on an existing issue.

    Args:
        issue_id: numeric Redmine issue id.
        note: the comment body. Empty/whitespace-only notes are rejected
            client-side to avoid no-op PUTs that still touch ``updated_on``.
        private: if True, mark the journal as private (visible only to
            users with the appropriate Redmine permission).

    Returns ``{"issue_id": id, "note": note, "private": bool, "source": "api"}``
    on success, or a structured error payload on failure.
    """
    if not note or not note.strip():
        return {
            "error": "validation_failed",
            "errors": [
                {
                    "error": "required_field_missing",
                    "hint": "Field 'note' is required for add_comment.",
                    "field": "note",
                    "op": "add_comment",
                }
            ],
        }

    payload: dict[str, Any] = {"notes": note}
    if private:
        payload["private_notes"] = True

    try:
        await client.put(f"/issues/{issue_id}.json", json={"issue": payload})
    except RedmineAPIError as e:
        return e.as_structured()

    return {
        "issue_id": issue_id,
        "note": note,
        "private": private,
        "source": "api",
    }


async def update_journal(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity with other tools
    journal_id: int,
    notes: str,
) -> dict[str, Any]:
    """Edit an existing journal entry's notes in place.

    Requires Redmine 5.0+ (``PUT /journals/:id.json``). The API user can
    only edit their own notes unless they have the ``edit_issue_notes``
    permission.

    Args:
        journal_id: numeric journal id (from ``get_journals``).
        notes: replacement note text. Pass an empty string to clear the
            note; if the journal has no ``details`` (field changes), this
            deletes the journal entirely.

    Returns ``{"journal_id": id, "notes": notes, "source": "api"}`` on
    success, or a structured error payload on failure.
    """
    try:
        await client.put(
            f"/journals/{journal_id}.json",
            json={"journal": {"notes": notes}},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    return {
        "journal_id": journal_id,
        "notes": notes,
        "source": "api",
    }


async def get_journals(
    client: RedmineClient,
    cache: SchemaCache,
    issue_id: int,
) -> dict[str, Any]:
    """Return the journal entries (comments + field changes) for an issue.

    Wraps ``issues.get_issue(issue_id, include="journals")`` and lifts the
    journals to a top-level field so callers don't have to dig through the
    issue payload. ``details`` on each journal records field changes;
    ``notes`` carries the comment body.
    """
    result = await issues_module.get_issue(
        client, cache, issue_id, include="journals"
    )
    if "error" in result:
        return result
    issue = result.get("issue") or {}
    journals = issue.get("journals") or []
    return {
        "issue_id": issue_id,
        "journals": journals,
        "source": "api",
    }
