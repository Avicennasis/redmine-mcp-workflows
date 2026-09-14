"""Async httpx-based Redmine REST client.

Wraps Redmine's JSON REST API with auth headers, retry on transient errors,
and a pagination helper. Returns parsed JSON for 2xx; raises
:class:`RedmineAPIError` for non-2xx.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .config import Config
from .errors import RedmineAPIError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PAGE_SIZE = 100  # Redmine's max per page

# Methods that can be safely re-sent after an ambiguous failure. Retrying a
# POST/PUT/DELETE on a 5xx or a transport error can duplicate a write that
# actually committed before the response was lost, so those are single-shot.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 429 is retryable for *any* method: the server rejected the request without
# processing it, so re-sending cannot duplicate a write.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 2  # total = 3 attempts
RETRY_BACKOFF_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 60.0


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    """Delay before the next attempt, honoring ``Retry-After`` when present."""
    if resp is not None:
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                delay = float(raw)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(raw)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    delay = (retry_at - datetime.now(UTC)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = float("nan")
            if math.isfinite(delay):
                return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)
    return RETRY_BACKOFF_SECONDS * (2**attempt)


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """Return the URL origin, normalizing default ports."""
    port = url.port
    if port is None:
        port = {"http": 80, "https": 443}.get(url.scheme)
    return (url.scheme.lower(), url.host.lower(), port)


class RedmineClient:
    """Async client for the Redmine REST API.

    Use as an async context manager so the underlying httpx client is closed
    cleanly:

        async with RedmineClient(config) as client:
            trackers = await client.get("/trackers.json")
    """

    def __init__(self, config: Config, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._config = config
        headers = httpx.Headers(
            {
                "Accept": "application/json",
                "User-Agent": "redmine-mcp-workflows/1.0",
            }
        )
        # OAuth bearer if available, otherwise X-Redmine-API-Key; raises if
        # neither is configured.
        headers.update(config.require_auth_headers())
        # Preserve the existing escape hatch for custom/proxy Authorization
        # headers, but keep the dedicated switch-user setting authoritative.
        headers.update(config.extra_headers)
        if config.switch_user:
            # Redmine admin impersonation: act as another login.
            headers["X-Redmine-Switch-User"] = config.switch_user
        self._client = httpx.AsyncClient(
            base_url=config.redmine_url,
            headers=headers,
            timeout=timeout,
            verify=config.verify_tls(),
        )

    async def __aenter__(self) -> RedmineClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        binary: bool = False,
        idempotent: bool | None = None,
    ) -> Any:
        # Retry only when re-sending cannot duplicate a committed write.
        # Callers may override for a known-safe non-idempotent method.
        if idempotent is None:
            idempotent = method.upper() in IDEMPOTENT_METHODS

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            resp: httpx.Response | None = None
            try:
                resp = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    content=content,
                    headers=headers,
                )
            except httpx.TransportError as e:
                last_exc = e
                if idempotent and attempt < MAX_RETRIES:
                    await asyncio.sleep(_retry_delay(None, attempt))
                    continue
                raise RedmineAPIError(
                    status_code=0,
                    body=str(e),
                    hint="Network error reaching Redmine.",
                ) from e

            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES and idempotent:
                log.debug("retrying %s %s (status %s)", method, path, resp.status_code)
                await asyncio.sleep(_retry_delay(resp, attempt))
                continue

            if not (200 <= resp.status_code < 300):
                try:
                    body: Any = resp.json()
                except ValueError:
                    body = resp.text
                raise RedmineAPIError(status_code=resp.status_code, body=body)

            if binary:
                return resp.content

            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as e:
                raise RedmineAPIError(
                    status_code=resp.status_code,
                    body=resp.text,
                    hint="Redmine returned a non-JSON success body.",
                ) from e

        # Unreachable; appeases type checker.
        raise RedmineAPIError(status_code=0, body=str(last_exc) if last_exc else "unknown")

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, *, json: Any) -> Any:
        return await self._request("POST", path, json=json)

    async def put(self, path: str, *, json: Any) -> Any:
        return await self._request("PUT", path, json=json)

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    async def post_binary(
        self,
        path: str,
        *,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> Any:
        """POST raw bytes (not JSON) — used for Redmine's /uploads.json endpoint.

        The endpoint expects the raw file body with
        ``Content-Type: application/octet-stream`` and returns JSON. Auth and
        retry semantics inherit from :meth:`_request`.
        """
        return await self._request(
            "POST",
            path,
            content=data,
            headers={"Content-Type": content_type},
        )

    async def get_binary(self, path: str) -> bytes:
        """GET a URL and return the raw response body — for attachment downloads.

        Path may be relative (joined to ``Config.redmine_url``) or an absolute
        URL **on the same host**. Auth headers and retry behavior inherit from
        :meth:`_request`.

        The same-host check exists because the caller may be handing us a URL
        that came from an API response: an absolute URL pointing elsewhere
        would otherwise receive this client's auth headers.
        """
        base = httpx.URL(self._config.redmine_url)
        resolved = base.join(path)
        if _origin(resolved) != _origin(base):
            raise RedmineAPIError(
                status_code=0,
                body=f"refused cross-origin binary fetch: {path!r}",
                hint=(
                    "get_binary only fetches from the configured Redmine origin "
                    f"({_origin(base)!r}); refusing to send credentials to "
                    f"{_origin(resolved)!r}."
                ),
            )
        return await self._request("GET", path, binary=True)

    async def paginate(
        self,
        path: str,
        *,
        items_key: str,
        params: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every item across paginated responses.

        Redmine list endpoints return a JSON object with the items under a
        named key (e.g., ``"issues"``, ``"projects"``) plus
        ``total_count``, ``offset``, and ``limit``.
        """
        params = dict(params or {})
        offset = 0
        while True:
            params["offset"] = offset
            params["limit"] = page_size
            page = await self.get(path, params=params)
            items = page.get(items_key, []) if isinstance(page, dict) else []
            for item in items:
                yield item

            total = page.get("total_count", 0) if isinstance(page, dict) else 0
            offset += len(items)
            if not items or offset >= total:
                return
