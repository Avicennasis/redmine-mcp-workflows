"""Enumeration tools (Redmine ticket #4487).

Tools (1):
  - list_enumerations — GET /enumerations/{type}.json

Covers issue_priorities, time_entry_activities, and document_categories.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError

ALLOWED_TYPES = frozenset({
    "issue_priorities",
    "time_entry_activities",
    "document_categories",
})


async def list_enumerations(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    enum_type: str,
) -> dict[str, Any]:
    """List enumeration values for a given type.

    Args:
        enum_type: one of ``issue_priorities``, ``time_entry_activities``,
            ``document_categories``.
    """
    if enum_type not in ALLOWED_TYPES:
        return {
            "error": "invalid_enumeration_type",
            "hint": f"Type {enum_type!r} is not one of {sorted(ALLOWED_TYPES)}.",
            "type": enum_type,
            "allowed_types": sorted(ALLOWED_TYPES),
        }

    try:
        resp = await client.get(f"/enumerations/{enum_type}.json")
    except RedmineAPIError as e:
        return e.as_structured()

    items = resp.get(enum_type, []) if isinstance(resp, dict) else []
    return {
        "type": enum_type,
        "values": items,
        "count": len(items),
        "source": "api",
    }
