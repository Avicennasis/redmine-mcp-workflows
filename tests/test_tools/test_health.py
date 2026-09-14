"""Unit tests for the health-check tool."""

from __future__ import annotations

import asyncio
from typing import Any

from redmine_mcp import server
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import health


class FakeClient:
    def __init__(self, *, response: Any = None, error: RedmineAPIError | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[tuple[str, str]] = []

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path))
        if self._error is not None:
            raise self._error
        return self._response


async def test_health_ok_reports_account() -> None:
    client = FakeClient(
        response={
            "user": {
                "id": 7,
                "login": "leon",
                "firstname": "L",
                "lastname": "S",
                "mail": "x@example.com",
            }
        }
    )
    result = await health.health(client)
    assert result["status"] == "ok"
    assert result["account"]["login"] == "leon"
    assert client.calls == [("GET", "/users/current.json")]


async def test_health_error_is_structured() -> None:
    client = FakeClient(error=RedmineAPIError(status_code=401, body="unauthorized"))
    result = await health.health(client)
    assert result["status"] == "error"
    assert result["error"]["status_code"] == 401


async def test_health_rejects_non_dict_payload() -> None:
    client = FakeClient(response=None)
    result = await health.health(client)
    assert result["status"] == "error"
    assert result["error"]["error"] == "health_response_malformed"


async def test_server_lifespan_does_not_wait_for_probe(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_probe(_cfg) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(server, "_startup_healthcheck", slow_probe)
    async with server._server_lifespan(server.mcp):
        await asyncio.wait_for(started.wait(), timeout=0.5)
        assert not release.is_set()
