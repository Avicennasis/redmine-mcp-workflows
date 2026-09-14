"""Connectivity / credential health check."""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def health(
    client: RedmineClient,
    cache: SchemaCache | None = None,  # noqa: ARG001 — signature parity
) -> dict[str, Any]:
    """Validate the configured Redmine URL and credential.

    Fetches the current account. A 2xx means both the URL is reachable and
    the credential is accepted; an API error is returned structurally so the
    caller can see the status/hint.
    """
    try:
        payload = await client.get("/users/current.json")
    except RedmineAPIError as e:
        return {"status": "error", "error": e.as_structured()}

    user = payload.get("user") if isinstance(payload, dict) else None
    account: dict[str, Any] = {}
    if isinstance(user, dict):
        for key in ("id", "login", "firstname", "lastname", "mail"):
            if user.get(key) is not None:
                account[key] = user[key]
    return {"status": "ok", "account": account, "source": "api"}
