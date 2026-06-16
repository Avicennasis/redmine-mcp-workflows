"""Issue-relation tools (Redmine ticket #2380).

Tools (4):
  - list_relations    — GET    /issues/{id}/relations.json
  - add_relation      — POST   /issues/{id}/relations.json
  - remove_relation   — DELETE /relations/{relation_id}.json   (top-level URL!)
  - set_parent_issue  — PUT    /issues/{id}.json (parent_issue_id field)

Relation type normalization:
  Redmine's enum is the *source-side* form: ``relates``, ``blocks``,
  ``duplicates``, ``precedes``, ``copied_to`` (plus the inverse-side forms
  ``blocked``, ``duplicated``, ``follows``, ``copied_from``). Callers
  often think in colloquial terms (``related_to``, ``blocked_by``,
  ``duplicate_of``); we normalize a small alias map before posting and
  reject anything not in the canonical set with a structured error that
  lists the allowed types.

Why ``set_parent_issue`` lives here (not in ``issues.py``):
  Parent/child is conceptually a relation but mechanically a field on
  the issue itself (``parent_issue_id``). Grouping it with the relations
  tools keeps the mental model consistent for callers — every
  cross-issue link lives under the same module.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError

# Canonical Redmine relation types (source-side, as accepted by the API).
ALLOWED_TYPES: tuple[str, ...] = (
    "relates",
    "duplicates",
    "duplicated",
    "blocks",
    "blocked",
    "precedes",
    "follows",
    "copied_to",
    "copied_from",
)

# Common aliases callers use → canonical type. Inverse-direction aliases
# (e.g. ``blocked_by`` → ``blocked``) are intentional: the caller asks
# "what is the relationship of MY issue to the other one", which matches
# the inverse-side form.
_ALIASES: dict[str, str] = {
    "related_to": "relates",
    "related to": "relates",
    "blocked_by": "blocked",
    "blocked by": "blocked",
    "duplicate_of": "duplicated",
    "duplicate of": "duplicated",
    "duplicated_by": "duplicates",
    "duplicated by": "duplicates",
    "copy_of": "copied_from",
    "copy of": "copied_from",
}


def _normalize_relation_type(raw: str) -> str | None:
    """Return the canonical Redmine relation type or ``None`` if unknown."""
    key = raw.strip().lower()
    if key in ALLOWED_TYPES:
        return key
    return _ALIASES.get(key)


async def list_relations(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    issue_id: int,
) -> dict[str, Any]:
    """Return all relations on an issue (both directions)."""
    try:
        payload = await client.get(f"/issues/{issue_id}/relations.json")
    except RedmineAPIError as e:
        return e.as_structured()
    rels = payload.get("relations", []) if isinstance(payload, dict) else []
    return {"issue_id": issue_id, "relations": rels, "source": "api"}


async def add_relation(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    issue_id: int,
    target_issue_id: int,
    relation_type: str,
    delay: int | None = None,
) -> dict[str, Any]:
    """Create a relation between two issues.

    Args:
        issue_id: source issue (the one we're adding the relation TO).
        target_issue_id: the other end of the relation.
        relation_type: one of :data:`ALLOWED_TYPES` or a recognized alias
            (``related_to``, ``blocked_by``, ``duplicate_of``, etc.).
        delay: only meaningful for ``precedes`` / ``follows`` — number of
            days between the two issues' due dates.
    """
    canonical = _normalize_relation_type(relation_type)
    if canonical is None:
        return {
            "error": "relation_type_unknown",
            "hint": (
                f"Relation type {relation_type!r} is not recognized. "
                "Use one of the canonical types or a known alias "
                "(e.g. 'related_to', 'blocked_by', 'duplicate_of')."
            ),
            "relation_type": relation_type,
            "allowed_types": list(ALLOWED_TYPES),
        }

    relation: dict[str, Any] = {
        "issue_to_id": target_issue_id,
        "relation_type": canonical,
    }
    if delay is not None:
        relation["delay"] = delay

    try:
        resp = await client.post(
            f"/issues/{issue_id}/relations.json",
            json={"relation": relation},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    rel = resp.get("relation") if isinstance(resp, dict) else None
    return {"relation": rel, "source": "api"}


async def remove_relation(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    relation_id: int,
) -> dict[str, Any]:
    """Delete a relation by its numeric id (visible on ``list_relations``).

    Note: Redmine's DELETE for relations lives at the top-level path
    ``/relations/{id}.json``, NOT under the parent issue.
    """
    try:
        await client.delete(f"/relations/{relation_id}.json")
    except RedmineAPIError as e:
        return e.as_structured()
    return {"relation_id": relation_id, "removed": True, "source": "api"}


async def set_parent_issue(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    issue_id: int,
    parent_issue_id: int,
) -> dict[str, Any]:
    """Set (or clear) an issue's parent. Pass ``parent_issue_id=0`` to clear.

    Parent/child is mechanically a field on the issue, not a relation
    record — Redmine surfaces ``parent_issue_id`` on the issue payload
    instead of in ``/relations``. We send empty-string when clearing
    because that's the form Redmine recognizes for "remove parent".
    """
    # Redmine treats empty string as "unparent"; treat 0 the same way for
    # caller convenience (matches the int-default convention used elsewhere
    # in the server module's tool signatures).
    parent_payload: int | str = parent_issue_id if parent_issue_id else ""
    try:
        await client.put(
            f"/issues/{issue_id}.json",
            json={"issue": {"parent_issue_id": parent_payload}},
        )
    except RedmineAPIError as e:
        return e.as_structured()
    return {
        "issue_id": issue_id,
        "parent_issue_id": parent_issue_id,
        "updated": True,
        "source": "api",
    }
