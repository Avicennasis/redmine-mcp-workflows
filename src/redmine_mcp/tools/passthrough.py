"""Generic passthrough tool (Redmine ticket #2384).

The escape hatch. Sends arbitrary HTTP requests to the Redmine API
without any validation, cache, or workflow pre-flight. Every response
carries ``validation_skipped: true`` so callers cannot accidentally
forget they bypassed the validation layer.

By-design tension:
  redmine-mcp's identity is "validate against a per-tracker cache before
  round-tripping the API". A generic ``redmine_request`` tool undermines
  that — it lets advanced users hit any endpoint regardless of whether
  the validation layer covers it. We accept the tension because the
  alternative is users falling back to raw curl, which gives us no
  observability at all. The tool is gated at the ``server.py`` layer
  behind ``REDMINE_MCP_ENABLE_PASSTHROUGH=true`` so it's opt-in, and
  the warning flag makes the bypass impossible to miss in tool output.

Why method+path instead of one URL string:
  Forces the caller to think in REST terms (verb + resource) rather
  than in URL-template terms — closer to what a Redmine developer
  expects. Also keeps the API key + base URL handling on our side.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError

ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "DELETE", "PATCH")

_WARNING = (
    "redmine_request bypasses redmine-mcp's validation, workflow checks, "
    "and schema cache. Caller is responsible for payload correctness."
)


def _envelope(
    *,
    method: str,
    path: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Wrap any response (success or error) with the universal flags."""
    return {
        "validation_skipped": True,
        "warning": _WARNING,
        "method": method,
        "path": path,
        **extra,
    }


async def request(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send an arbitrary request to the Redmine API.

    Args:
        method: HTTP verb. Case-insensitive; normalized to upper.
        path: must start with ``/``. Joined onto the configured
            ``REDMINE_URL`` by the underlying client.
        body: JSON body for POST / PUT / PATCH. Ignored for GET / DELETE.
        params: query-string params for GET.

    Returns a dict that ALWAYS has ``validation_skipped=True`` and a
    ``warning`` field, plus ``method`` and ``path`` echoes. On success,
    ``body`` carries the parsed JSON response (or None for 204). On
    failure, ``error`` carries the structured-error key.
    """
    method_norm = (method or "").strip().upper()
    if method_norm not in ALLOWED_METHODS:
        return _envelope(
            method=method_norm or method,
            path=path,
            extra={
                "error": "passthrough_method_unknown",
                "hint": (
                    f"Method {method!r} is not in the allowed set "
                    f"{list(ALLOWED_METHODS)}."
                ),
                "allowed_methods": list(ALLOWED_METHODS),
            },
        )

    if not path or not path.startswith("/"):
        return _envelope(
            method=method_norm,
            path=path,
            extra={
                "error": "passthrough_path_invalid",
                "hint": (
                    "path must be a non-empty string starting with '/'. "
                    "We refuse to auto-prepend so the caller stays explicit."
                ),
            },
        )

    try:
        if method_norm == "GET":
            resp = await client.get(path, params=params)
        elif method_norm == "DELETE":
            resp = await client.delete(path)
        elif method_norm == "POST":
            resp = await client.post(path, json=body if body is not None else {})
        elif method_norm == "PUT":
            resp = await client.put(path, json=body if body is not None else {})
        else:  # PATCH — _request handles arbitrary methods
            resp = await client._request("PATCH", path, json=body)
    except RedmineAPIError as e:
        return _envelope(
            method=method_norm,
            path=path,
            extra=e.as_structured(),
        )

    return _envelope(
        method=method_norm,
        path=path,
        extra={"body": resp},
    )
