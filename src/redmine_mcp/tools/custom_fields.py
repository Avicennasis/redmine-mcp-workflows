"""Custom field listing tool (Redmine ticket #4490).

Tools (1):
  - list_custom_fields — GET /custom_fields.json

Admin-only. Returns full custom field definitions.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_custom_fields(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
) -> dict[str, Any]:
    """List all custom field definitions (admin only)."""
    try:
        resp = await client.get("/custom_fields.json")
    except RedmineAPIError as e:
        return e.as_structured()

    fields = resp.get("custom_fields", []) if isinstance(resp, dict) else []
    return {"custom_fields": fields, "count": len(fields), "source": "api"}
