"""Server-wrapper regression tests for the project tools' partial-update contract.

Redmine ticket #50212: the FastMCP wrappers in ``server.py`` used to declare
``is_public: bool = True`` and ``inherit_members: bool = False`` — non-sentinel
defaults forwarded unconditionally into the ``None``-guarded tools layer, so a
call that supplied *only* a description also flipped a private project public
(and unset member inheritance). These tests run the wrapper with the REAL
tools layer against a recording FakeClient and assert on the request body,
because asserting the returned project still looks private would also pass for
a correct-by-accident call on an already-public project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from redmine_mcp import server
from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.config import Config


class _FakeClient:
    """Records (method, path, payload) and serves canned GET responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str, *, params: dict | None = None) -> dict:
        self.calls.append(("GET", path, params))
        return {"project": {"id": 99, "name": "refreshed"}}

    async def post(self, path: str, *, json: dict | None = None) -> dict:
        self.calls.append(("POST", path, json))
        return {"project": {"id": 99, "name": "created"}}

    async def put(self, path: str, *, json: dict | None = None) -> dict:
        self.calls.append(("PUT", path, json))
        return {}

    async def delete(self, path: str) -> dict:
        self.calls.append(("DELETE", path, None))
        return {}


class _FakeRedmineClient:
    """Stands in for RedmineClient in _wrap's async-with."""

    last: _FakeClient | None = None

    def __init__(self, _cfg: Config) -> None:
        self._client = _FakeClient()

    async def __aenter__(self) -> _FakeClient:
        _FakeRedmineClient.last = self._client
        return self._client

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def patch_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = Config(
        redmine_url="http://127.0.0.1:8281",
        api_key="dummy-test-key",
        read_only=False,
    )
    monkeypatch.setattr(server, "_get_config", lambda: cfg)
    cache = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    monkeypatch.setattr(server, "_get_cache", lambda: cache)
    monkeypatch.setattr(server, "RedmineClient", _FakeRedmineClient)
    _FakeRedmineClient.last = None


def _put_payload() -> dict:
    """The project dict from the single PUT the update wrapper issued."""
    puts = [c for c in _FakeRedmineClient.last.calls if c[0] == "PUT"]
    assert len(puts) == 1, f"expected exactly one PUT, got {len(puts)}"
    return puts[0][2]["project"]


def _post_payload() -> dict:
    posts = [c for c in _FakeRedmineClient.last.calls if c[0] == "POST"]
    assert len(posts) == 1, f"expected exactly one POST, got {len(posts)}"
    return posts[0][2]["project"]


async def test_update_omitted_visibility_not_sent() -> None:
    """The #50212 regression: a description-only update must not carry
    is_public/inherit_members in the request body."""
    result = json.loads(
        await server.redmine_update_project(project_id="99", description="new text")
    )
    assert "error" not in result, result
    body = _put_payload()
    assert "is_public" not in body, f"is_public leaked into partial update: {body}"
    assert "inherit_members" not in body, f"inherit_members leaked: {body}"
    assert body["description"] == "new text"


async def test_update_explicit_values_still_sent() -> None:
    """Control for the pair: explicitly supplied values must round-trip."""
    await server.redmine_update_project(project_id=99, is_public=False, inherit_members=True)
    body = _put_payload()
    assert body["is_public"] is False
    assert body["inherit_members"] is True


async def test_update_name_only_not_sent() -> None:
    """Second shape of the same bug: name-only update on a project that had
    member inheritance enabled must not silently unset it."""
    await server.redmine_update_project(project_id=99, name="Renamed")
    body = _put_payload()
    assert "is_public" not in body
    assert "inherit_members" not in body
    assert body["name"] == "Renamed"


async def test_create_omitted_visibility_not_sent() -> None:
    """Create had the same non-sentinel defaults. Harmless at creation
    (the forced values matched Redmine's own defaults) but fixed for the
    contract — omitted means the key is absent from the POST body."""
    result = json.loads(
        await server.redmine_create_project(name="Test", identifier="test")
    )
    assert "error" not in result, result
    body = _post_payload()
    assert "is_public" not in body
    assert "inherit_members" not in body


async def test_create_explicit_visibility_sent() -> None:
    await server.redmine_create_project(name="Test", identifier="test", is_public=False)
    body = _post_payload()
    assert body["is_public"] is False
