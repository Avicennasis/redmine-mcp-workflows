"""Unit tests for v0.2 #2378 wiki page CRUD tools (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import wiki


class FakeClient:
    """In-memory stand-in for RedmineClient used by tools.

    Records every call as ``(method, path, payload_or_params)`` so tests can
    assert on the URL the tool sent (especially title encoding) plus the
    payload shape.
    """

    def __init__(
        self,
        responses: dict[tuple[str, str], Any] | None = None,
        *,
        errors: dict[tuple[str, str], RedmineAPIError] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if ("GET", path) in self._errors:
            raise self._errors[("GET", path)]
        return self._responses.get(("GET", path))

    async def put(self, path: str, *, json: Any) -> Any:
        self.calls.append(("PUT", path, json))
        if ("PUT", path) in self._errors:
            raise self._errors[("PUT", path)]
        return self._responses.get(("PUT", path))

    async def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path, None))
        if ("DELETE", path) in self._errors:
            raise self._errors[("DELETE", path)]
        return self._responses.get(("DELETE", path))


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


# ---------------------------------------------------------------------
# get_page
# ---------------------------------------------------------------------


async def test_get_page_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/projects/claudecode/wiki/Home.json"): {
            "wiki_page": {
                "title": "Home",
                "text": "# Welcome\n\nBody here.",
                "version": 3,
                "author": {"id": 7, "name": "Léon"},
            },
        },
    })
    result = await wiki.get_page(client, cache, "claudecode", "Home")
    assert result["page"]["title"] == "Home"
    assert result["page"]["version"] == 3
    assert result["source"] == "api"
    # No version filter sent.
    assert client.calls == [("GET", "/projects/claudecode/wiki/Home.json", None)]


async def test_get_page_with_historical_version(cache: SchemaCache) -> None:
    """version= parameter should round-trip to ``?version=N`` on the URL path."""
    client = FakeClient({
        ("GET", "/projects/claudecode/wiki/Home/2.json"): {
            "wiki_page": {"title": "Home", "version": 2, "text": "Old body."},
        },
    })
    result = await wiki.get_page(client, cache, "claudecode", "Home", version=2)
    assert result["page"]["version"] == 2
    assert client.calls[-1][1] == "/projects/claudecode/wiki/Home/2.json"


async def test_get_page_url_encodes_title_with_space(cache: SchemaCache) -> None:
    """Titles with spaces must be percent-encoded to survive the URL path."""
    client = FakeClient({
        ("GET", "/projects/claudecode/wiki/Release%20Notes.json"): {
            "wiki_page": {"title": "Release Notes", "text": "...", "version": 1},
        },
    })
    result = await wiki.get_page(client, cache, "claudecode", "Release Notes")
    assert result["page"]["title"] == "Release Notes"
    assert client.calls[-1][1] == "/projects/claudecode/wiki/Release%20Notes.json"


async def test_get_page_returns_not_found_on_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("GET", "/projects/claudecode/wiki/Ghost.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await wiki.get_page(client, cache, "claudecode", "Ghost")
    assert result["error"] == "wiki_page_not_found"
    assert result["project"] == "claudecode"
    assert result["title"] == "Ghost"


async def test_get_page_returns_not_found_when_payload_empty(cache: SchemaCache) -> None:
    """Some Redmine builds return 200 with ``{"wiki_page": null}`` instead of 404."""
    client = FakeClient({
        ("GET", "/projects/claudecode/wiki/Phantom.json"): {"wiki_page": None},
    })
    result = await wiki.get_page(client, cache, "claudecode", "Phantom")
    assert result["error"] == "wiki_page_not_found"


async def test_get_page_propagates_other_api_errors(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("GET", "/projects/claudecode/wiki/Home.json"): RedmineAPIError(
            status_code=403, body={"errors": ["Forbidden"]},
        ),
    })
    result = await wiki.get_page(client, cache, "claudecode", "Home")
    assert result["error"] == "redmine_api_403"


# ---------------------------------------------------------------------
# create_page
# ---------------------------------------------------------------------


async def test_create_page_happy_path(cache: SchemaCache) -> None:
    """Pre-flight GET returns 404 → PUT creates → returns the new page."""
    client = FakeClient(
        responses={
            ("PUT", "/projects/claudecode/wiki/NewPage.json"): {
                "wiki_page": {"title": "NewPage", "text": "fresh body", "version": 1},
            },
        },
        errors={
            ("GET", "/projects/claudecode/wiki/NewPage.json"): RedmineAPIError(
                status_code=404, body={"errors": ["Not found"]},
            ),
        },
    )
    result = await wiki.create_page(
        client, cache, "claudecode", "NewPage", "fresh body"
    )
    assert result["page"]["title"] == "NewPage"
    assert result["page"]["version"] == 1
    assert result["source"] == "api"
    # GET-first then PUT.
    assert [c[0] for c in client.calls] == ["GET", "PUT"]
    put_payload = client.calls[1][2]
    assert put_payload == {"wiki_page": {"text": "fresh body"}}


async def test_create_page_rejects_when_page_already_exists(cache: SchemaCache) -> None:
    """If the GET returns a real page, refuse to overwrite via create_page."""
    client = FakeClient({
        ("GET", "/projects/claudecode/wiki/Home.json"): {
            "wiki_page": {"title": "Home", "text": "existing", "version": 5},
        },
    })
    result = await wiki.create_page(
        client, cache, "claudecode", "Home", "would clobber"
    )
    assert result["error"] == "wiki_page_already_exists"
    assert result["title"] == "Home"
    assert result["existing_version"] == 5
    # No PUT sent — only the pre-flight GET.
    assert [c[0] for c in client.calls] == ["GET"]


async def test_create_page_rejects_empty_text(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await wiki.create_page(client, cache, "claudecode", "Empty", "")
    assert result["error"] == "validation_failed"
    # No HTTP call made.
    assert client.calls == []


async def test_create_page_rejects_whitespace_only_text(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await wiki.create_page(client, cache, "claudecode", "Empty", "   \n\t")
    assert result["error"] == "validation_failed"
    assert client.calls == []


async def test_create_page_passes_optional_parent_and_comments(
    cache: SchemaCache,
) -> None:
    client = FakeClient(
        responses={
            ("PUT", "/projects/claudecode/wiki/Child.json"): {
                "wiki_page": {"title": "Child", "version": 1, "text": "body"},
            },
        },
        errors={
            ("GET", "/projects/claudecode/wiki/Child.json"): RedmineAPIError(
                status_code=404, body={"errors": ["Not found"]},
            ),
        },
    )
    result = await wiki.create_page(
        client, cache, "claudecode", "Child", "body",
        parent_title="Parent", comments="Initial revision",
    )
    assert result["page"]["title"] == "Child"
    put_payload = client.calls[-1][2]
    assert put_payload["wiki_page"]["text"] == "body"
    assert put_payload["wiki_page"]["parent_title"] == "Parent"
    assert put_payload["wiki_page"]["comments"] == "Initial revision"


async def test_create_page_url_encodes_title(cache: SchemaCache) -> None:
    client = FakeClient(
        responses={
            ("PUT", "/projects/claudecode/wiki/Release%20Notes.json"): {
                "wiki_page": {"title": "Release Notes", "version": 1, "text": "x"},
            },
        },
        errors={
            ("GET", "/projects/claudecode/wiki/Release%20Notes.json"):
                RedmineAPIError(status_code=404, body={"errors": ["Not found"]}),
        },
    )
    await wiki.create_page(client, cache, "claudecode", "Release Notes", "x")
    paths = [c[1] for c in client.calls]
    assert all("Release%20Notes" in p for p in paths)


# ---------------------------------------------------------------------
# update_page
# ---------------------------------------------------------------------


async def test_update_page_happy_path_no_version(cache: SchemaCache) -> None:
    """update_page sends a PUT with the new text — no GET pre-flight needed."""
    client = FakeClient({
        ("PUT", "/projects/claudecode/wiki/Home.json"): None,
        ("GET", "/projects/claudecode/wiki/Home.json"): {
            "wiki_page": {"title": "Home", "text": "updated body", "version": 6},
        },
    })
    result = await wiki.update_page(
        client, cache, "claudecode", "Home", "updated body"
    )
    assert result["page"]["text"] == "updated body"
    assert result["page"]["version"] == 6
    methods = [c[0] for c in client.calls]
    assert methods == ["PUT", "GET"]
    put_payload = client.calls[0][2]
    assert put_payload == {"wiki_page": {"text": "updated body"}}


async def test_update_page_with_version_for_optimistic_lock(
    cache: SchemaCache,
) -> None:
    """When ``version`` is supplied, it travels in the wiki_page body."""
    client = FakeClient({
        ("PUT", "/projects/claudecode/wiki/Home.json"): None,
        ("GET", "/projects/claudecode/wiki/Home.json"): {
            "wiki_page": {"title": "Home", "text": "v6 body", "version": 6},
        },
    })
    result = await wiki.update_page(
        client, cache, "claudecode", "Home", "v6 body", version=5,
    )
    assert result["page"]["version"] == 6
    put_payload = client.calls[0][2]
    assert put_payload["wiki_page"]["version"] == 5
    assert put_payload["wiki_page"]["text"] == "v6 body"


async def test_update_page_surfaces_version_conflict(cache: SchemaCache) -> None:
    """Redmine returns 409 (or 422 with version error) on concurrent update."""
    client = FakeClient(errors={
        ("PUT", "/projects/claudecode/wiki/Home.json"): RedmineAPIError(
            status_code=409, body={"errors": ["Version is not the most recent"]},
        ),
    })
    result = await wiki.update_page(
        client, cache, "claudecode", "Home", "stale body", version=3,
    )
    assert result["error"] == "redmine_api_409"


async def test_update_page_rejects_empty_text(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await wiki.update_page(client, cache, "claudecode", "Home", "")
    assert result["error"] == "validation_failed"
    assert client.calls == []


async def test_update_page_returns_not_found_on_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("PUT", "/projects/claudecode/wiki/Ghost.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await wiki.update_page(client, cache, "claudecode", "Ghost", "body")
    assert result["error"] == "redmine_api_404"


async def test_update_page_passes_optional_parent_and_comments(
    cache: SchemaCache,
) -> None:
    client = FakeClient({
        ("PUT", "/projects/claudecode/wiki/Child.json"): None,
        ("GET", "/projects/claudecode/wiki/Child.json"): {
            "wiki_page": {"title": "Child", "version": 2, "text": "body"},
        },
    })
    await wiki.update_page(
        client, cache, "claudecode", "Child", "body",
        parent_title="NewParent", comments="reparented",
    )
    put_payload = client.calls[0][2]
    assert put_payload["wiki_page"]["parent_title"] == "NewParent"
    assert put_payload["wiki_page"]["comments"] == "reparented"


# ---------------------------------------------------------------------
# delete_page
# ---------------------------------------------------------------------


async def test_delete_page_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({("DELETE", "/projects/claudecode/wiki/Doomed.json"): None})
    result = await wiki.delete_page(client, cache, "claudecode", "Doomed")
    assert result == {
        "project": "claudecode",
        "title": "Doomed",
        "deleted": True,
        "source": "api",
    }


async def test_delete_page_returns_not_found_on_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("DELETE", "/projects/claudecode/wiki/Ghost.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await wiki.delete_page(client, cache, "claudecode", "Ghost")
    assert result["error"] == "wiki_page_not_found"
    assert result["title"] == "Ghost"


async def test_delete_page_url_encodes_title(cache: SchemaCache) -> None:
    client = FakeClient({
        ("DELETE", "/projects/claudecode/wiki/Old%20Notes.json"): None,
    })
    await wiki.delete_page(client, cache, "claudecode", "Old Notes")
    assert client.calls[-1][1] == "/projects/claudecode/wiki/Old%20Notes.json"
