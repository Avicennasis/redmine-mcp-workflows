"""Retry / idempotency tests for RedmineClient.

Locks in the two behaviours that protect against duplicate writes:

  * only idempotent methods are retried on a 5xx or a transport error, and
  * ``429`` is retried only for idempotent methods, honoring ``Retry-After``.

Also covers the same-origin guard on :meth:`RedmineClient.get_binary`.
"""

from __future__ import annotations

import ssl
from typing import Any

import httpx
import pytest

from redmine_mcp import client as client_module
from redmine_mcp.client import RedmineClient, _retry_delay
from redmine_mcp.config import Config
from redmine_mcp.errors import RedmineAPIError

URL = "https://trouble.example"


def _resp(
    status: int,
    *,
    json: Any | None = None,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        json=json,
        headers=headers or {},
        content=content,
        request=httpx.Request("GET", URL),
    )


class FakeTransport:
    """Pops scripted responses (or raises scripted exceptions) in order."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, path))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "RETRY_BACKOFF_SECONDS", 0.0)


def _client(script: list[Any]) -> tuple[RedmineClient, FakeTransport]:
    client = RedmineClient(Config(api_key="k", redmine_url=URL))
    transport = FakeTransport(script)
    client._client = transport  # type: ignore[assignment]
    return client, transport


# ---------------------------------------------------------------------
# retry policy
# ---------------------------------------------------------------------


async def test_get_retries_5xx_then_succeeds() -> None:
    client, transport = _client([_resp(503), _resp(200, json={"ok": True})])
    assert await client.get("/things.json") == {"ok": True}
    assert len(transport.calls) == 2


async def test_get_gives_up_after_max_retries() -> None:
    client, transport = _client([_resp(503), _resp(503), _resp(503)])
    with pytest.raises(RedmineAPIError) as exc:
        await client.get("/things.json")
    assert exc.value.status_code == 503
    assert len(transport.calls) == 3  # 1 + MAX_RETRIES


async def test_get_retries_transport_error() -> None:
    client, transport = _client([httpx.ConnectError("boom"), _resp(200, json={"ok": True})])
    assert await client.get("/things.json") == {"ok": True}
    assert len(transport.calls) == 2


async def test_post_is_not_retried_on_5xx() -> None:
    client, transport = _client([_resp(503, json={"errors": ["nope"]})])
    with pytest.raises(RedmineAPIError) as exc:
        await client.post("/issues.json", json={"issue": {"subject": "x"}})
    assert exc.value.status_code == 503
    assert len(transport.calls) == 1  # a 5xx may have committed — do not resend


async def test_post_is_not_retried_on_transport_error() -> None:
    client, transport = _client([httpx.ReadTimeout("ambiguous")])
    with pytest.raises(RedmineAPIError) as exc:
        await client.post("/issues.json", json={"issue": {"subject": "x"}})
    assert exc.value.status_code == 0
    assert len(transport.calls) == 1


async def test_post_does_not_retry_429() -> None:
    client, transport = _client([_resp(429, headers={"Retry-After": "0"})])
    with pytest.raises(RedmineAPIError) as exc:
        await client.post("/issues.json", json={"issue": {"subject": "x"}})
    assert exc.value.status_code == 429
    assert len(transport.calls) == 1


async def test_get_retries_429_then_succeeds() -> None:
    client, transport = _client(
        [
            _resp(429, headers={"Retry-After": "0"}),
            _resp(200, json={"ok": True}),
        ]
    )
    assert await client.get("/things.json") == {"ok": True}
    assert len(transport.calls) == 2


# ---------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------


def test_retry_delay_uses_retry_after_seconds() -> None:
    assert _retry_delay(_resp(429, headers={"Retry-After": "5"}), 0) == 5.0


def test_retry_delay_caps_absurd_retry_after() -> None:
    assert _retry_delay(_resp(429, headers={"Retry-After": "99999"}), 0) == 60.0


def test_retry_delay_honors_http_date() -> None:
    resp = _resp(503, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"})
    assert _retry_delay(resp, 1) == 60.0


def test_retry_delay_clamps_negative_seconds() -> None:
    assert _retry_delay(_resp(429, headers={"Retry-After": "-5"}), 0) == 0.0


def test_retry_delay_rejects_non_finite_seconds() -> None:
    assert (
        _retry_delay(_resp(429, headers={"Retry-After": "nan"}), 1)
        == client_module.RETRY_BACKOFF_SECONDS * 2
    )


def test_retry_delay_backoff_without_header() -> None:
    assert _retry_delay(None, 0) == client_module.RETRY_BACKOFF_SECONDS


# ---------------------------------------------------------------------
# get_binary same-host guard
# ---------------------------------------------------------------------


async def test_get_binary_allows_same_host_absolute_url() -> None:
    client, transport = _client([_resp(200, content=b"binary")])
    assert await client.get_binary(f"{URL}/attachments/download/1/x.bin") == b"binary"
    assert len(transport.calls) == 1


async def test_get_binary_refuses_cross_host_url() -> None:
    client, transport = _client([_resp(200, content=b"should-not-fetch")])
    with pytest.raises(RedmineAPIError) as exc:
        await client.get_binary("https://evil.example/steal")
    assert "cross-origin" in str(exc.value.body)
    assert transport.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://trouble.example/steal",
        "https://trouble.example:8443/steal",
    ],
)
async def test_get_binary_refuses_same_host_different_origin(url: str) -> None:
    client, transport = _client([_resp(200, content=b"should-not-fetch")])
    with pytest.raises(RedmineAPIError):
        await client.get_binary(url)
    assert transport.calls == []


# ---------------------------------------------------------------------
# TLS verify wiring
# ---------------------------------------------------------------------


def _captured_httpx_kwargs(monkeypatch: pytest.MonkeyPatch, config: Config) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_async_client(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(client_module.httpx, "AsyncClient", fake_async_client)
    RedmineClient(config)
    return captured


def test_client_passes_verify_true_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = _captured_httpx_kwargs(monkeypatch, Config(api_key="k", redmine_url=URL))
    assert kwargs["verify"] is True


def test_client_passes_verify_false_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(api_key="k", redmine_url=URL, ssl_verify=False)
    assert _captured_httpx_kwargs(monkeypatch, cfg)["verify"] is False


def test_client_passes_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    context = ssl.create_default_context()
    monkeypatch.setattr(Config, "verify_tls", lambda self: context)
    cfg = Config(api_key="k", redmine_url=URL, ca_bundle="/etc/ssl/ca.pem")
    assert _captured_httpx_kwargs(monkeypatch, cfg)["verify"] is context


def test_client_sets_switch_user_header(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(api_key="k", redmine_url=URL, switch_user="alice")
    headers = _captured_httpx_kwargs(monkeypatch, cfg)["headers"]
    assert headers["X-Redmine-Switch-User"] == "alice"


def test_client_omits_switch_user_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(api_key="k", redmine_url=URL)
    headers = _captured_httpx_kwargs(monkeypatch, cfg)["headers"]
    assert "X-Redmine-Switch-User" not in headers


def test_extra_headers_cannot_override_switch_user(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(
        api_key="k",
        redmine_url=URL,
        switch_user="alice",
        extra_headers={"x-redmine-switch-user": "mallory"},
    )
    headers = httpx.Headers(_captured_httpx_kwargs(monkeypatch, cfg)["headers"])
    assert headers["X-Redmine-Switch-User"] == "alice"
