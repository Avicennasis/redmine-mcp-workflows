"""Async httpx-based Redmine REST client.

Wraps Redmine's JSON REST API with auth headers, retry on transient errors,
and a pagination helper. Returns parsed JSON for 2xx; raises
:class:`RedmineAPIError` for non-2xx.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import Config
from .errors import RedmineAPIError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PAGE_SIZE = 100  # Redmine's max per page
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
MAX_RETRIES = 2  # total = 3 attempts
RETRY_BACKOFF_SECONDS = 0.5


class RedmineClient:
    """Async client for the Redmine REST API.

    Use as an async context manager so the underlying httpx client is closed
    cleanly:

        async with RedmineClient(config) as client:
            trackers = await client.get("/trackers.json")
    """

    def __init__(self, config: Config, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._config = config
        headers = {
            "Accept": "application/json",
            "User-Agent": "redmine-mcp-workflows/1.0",
        }
        # OAuth bearer if available, otherwise X-Redmine-API-Key; raises if
        # neither is configured.
        headers.update(config.require_auth_headers())
        headers.update(config.extra_headers)
        self._client = httpx.AsyncClient(
            base_url=config.redmine_url,
            headers=headers,
            timeout=timeout,
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
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
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
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
                    continue
                raise RedmineAPIError(
                    status_code=0,
                    body=str(e),
                    hint="Network error reaching Redmine.",
                ) from e

            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                log.debug("retrying %s %s (status %s)", method, path, resp.status_code)
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
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
        URL on the same host. Auth headers and retry behavior inherit from
        :meth:`_request`.
        """
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
