"""Unit tests for v0.2 #2382 versions/milestones CRUD tools (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import versions


class FakeClient:
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

    async def post(self, path: str, *, json: Any) -> Any:
        self.calls.append(("POST", path, json))
        if ("POST", path) in self._errors:
            raise self._errors[("POST", path)]
        return self._responses.get(("POST", path))

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
# list_versions
# ---------------------------------------------------------------------


async def test_list_versions_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/projects/claudecode/versions.json"): {
            "versions": [
                {"id": 1, "name": "v1.0", "status": "closed"},
                {"id": 2, "name": "v1.1", "status": "open"},
            ],
        },
    })
    result = await versions.list_versions(client, cache, "claudecode")
    assert result["project"] == "claudecode"
    assert len(result["versions"]) == 2
    assert result["versions"][1]["name"] == "v1.1"
    assert result["source"] == "api"


async def test_list_versions_empty(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/projects/15/versions.json"): {"versions": []},
    })
    result = await versions.list_versions(client, cache, 15)
    assert result["versions"] == []


async def test_list_versions_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("GET", "/projects/nope/versions.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await versions.list_versions(client, cache, "nope")
    assert result["error"] == "redmine_api_404"


# ---------------------------------------------------------------------
# get_version
# ---------------------------------------------------------------------


async def test_get_version_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/versions/7.json"): {
            "version": {
                "id": 7, "name": "v2.0", "status": "open",
                "due_date": "2026-06-30", "sharing": "none",
            },
        },
    })
    result = await versions.get_version(client, cache, 7)
    assert result["version"]["name"] == "v2.0"
    assert result["version"]["due_date"] == "2026-06-30"
    assert result["source"] == "api"


async def test_get_version_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("GET", "/versions/999.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await versions.get_version(client, cache, 999)
    assert result["error"] == "version_not_found"
    assert result["version_id"] == 999


async def test_get_version_returns_not_found_when_payload_empty(
    cache: SchemaCache,
) -> None:
    client = FakeClient({("GET", "/versions/99.json"): {"version": None}})
    result = await versions.get_version(client, cache, 99)
    assert result["error"] == "version_not_found"


# ---------------------------------------------------------------------
# create_version
# ---------------------------------------------------------------------


async def test_create_version_minimal(cache: SchemaCache) -> None:
    client = FakeClient({
        ("POST", "/projects/claudecode/versions.json"): {
            "version": {"id": 5, "name": "v1.2", "status": "open"},
        },
    })
    result = await versions.create_version(
        client, cache, project="claudecode", name="v1.2",
    )
    assert result["version"]["id"] == 5
    payload = client.calls[0][2]
    assert payload == {"version": {"name": "v1.2"}}


async def test_create_version_with_all_optionals(cache: SchemaCache) -> None:
    client = FakeClient({
        ("POST", "/projects/claudecode/versions.json"): {
            "version": {"id": 6, "name": "v2.0"},
        },
    })
    await versions.create_version(
        client, cache,
        project="claudecode", name="v2.0",
        description="Major release",
        status="open",
        due_date="2026-12-31",
        sharing="descendants",
        wiki_page_title="ReleaseNotes-v2",
    )
    sent = client.calls[0][2]["version"]
    assert sent["name"] == "v2.0"
    assert sent["description"] == "Major release"
    assert sent["status"] == "open"
    assert sent["due_date"] == "2026-12-31"
    assert sent["sharing"] == "descendants"
    assert sent["wiki_page_title"] == "ReleaseNotes-v2"


async def test_create_version_rejects_empty_name(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await versions.create_version(
        client, cache, project="claudecode", name="",
    )
    assert result["error"] == "validation_failed"
    assert client.calls == []


async def test_create_version_rejects_unknown_status(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await versions.create_version(
        client, cache, project="claudecode", name="v1", status="bananas",
    )
    assert result["error"] == "version_status_unknown"
    assert "open" in result["allowed_statuses"]
    assert client.calls == []


async def test_create_version_rejects_unknown_sharing(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await versions.create_version(
        client, cache, project="claudecode", name="v1", sharing="bananas",
    )
    assert result["error"] == "version_sharing_unknown"
    assert "descendants" in result["allowed_sharings"]


async def test_create_version_rejects_bad_date_format(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await versions.create_version(
        client, cache, project="claudecode", name="v1", due_date="next Tuesday",
    )
    assert result["error"] == "version_date_invalid"
    assert client.calls == []


async def test_create_version_propagates_422(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("POST", "/projects/claudecode/versions.json"): RedmineAPIError(
            status_code=422, body={"errors": ["Name has already been taken"]},
        ),
    })
    result = await versions.create_version(
        client, cache, project="claudecode", name="v1.0",
    )
    assert result["error"] == "redmine_api_422"


# ---------------------------------------------------------------------
# update_version
# ---------------------------------------------------------------------


async def test_update_version_partial(cache: SchemaCache) -> None:
    """Only provided fields are sent — status alone, no name clobber."""
    client = FakeClient({
        ("PUT", "/versions/7.json"): None,
        ("GET", "/versions/7.json"): {
            "version": {"id": 7, "name": "v2.0", "status": "closed"},
        },
    })
    result = await versions.update_version(
        client, cache, version_id=7, status="closed",
    )
    assert result["version"]["status"] == "closed"
    sent = client.calls[0][2]["version"]
    assert sent == {"status": "closed"}


async def test_update_version_with_all_fields(cache: SchemaCache) -> None:
    client = FakeClient({
        ("PUT", "/versions/7.json"): None,
        ("GET", "/versions/7.json"): {"version": {"id": 7, "name": "v2.0"}},
    })
    await versions.update_version(
        client, cache, version_id=7,
        name="v2.0-final",
        description="Locked",
        status="locked",
        due_date="2026-12-25",
        sharing="hierarchy",
    )
    sent = client.calls[0][2]["version"]
    assert sent["name"] == "v2.0-final"
    assert sent["description"] == "Locked"
    assert sent["status"] == "locked"
    assert sent["due_date"] == "2026-12-25"
    assert sent["sharing"] == "hierarchy"


async def test_update_version_rejects_no_fields(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await versions.update_version(client, cache, version_id=7)
    assert result["error"] == "nothing_to_update"
    assert client.calls == []


async def test_update_version_rejects_unknown_status(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await versions.update_version(
        client, cache, version_id=7, status="bananas",
    )
    assert result["error"] == "version_status_unknown"


async def test_update_version_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("PUT", "/versions/999.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await versions.update_version(
        client, cache, version_id=999, name="x",
    )
    assert result["error"] == "redmine_api_404"


# ---------------------------------------------------------------------
# delete_version
# ---------------------------------------------------------------------


async def test_delete_version_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({("DELETE", "/versions/7.json"): None})
    result = await versions.delete_version(client, cache, version_id=7)
    assert result == {"version_id": 7, "deleted": True, "source": "api"}


async def test_delete_version_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("DELETE", "/versions/999.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]},
        ),
    })
    result = await versions.delete_version(client, cache, version_id=999)
    assert result["error"] == "version_not_found"


async def test_delete_version_propagates_422_for_in_use(cache: SchemaCache) -> None:
    """Redmine refuses delete when issues still reference the version."""
    client = FakeClient(errors={
        ("DELETE", "/versions/7.json"): RedmineAPIError(
            status_code=422,
            body={"errors": ["Version is in use and can't be deleted"]},
        ),
    })
    result = await versions.delete_version(client, cache, version_id=7)
    assert result["error"] == "redmine_api_422"


# ---------------------------------------------------------------------
# assign_issue_to_version
# ---------------------------------------------------------------------


async def test_assign_issue_to_version_happy_path(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thin wrapper over update_issue with fixed_version_id."""
    captured: dict[str, Any] = {}

    async def fake_update(client, cache, issue_id, **kwargs):
        captured["issue_id"] = issue_id
        captured.update(kwargs)
        return {"issue": {"id": issue_id, "fixed_version": {"id": 7}}}

    monkeypatch.setattr(versions.issues_module, "update_issue", fake_update)
    result = await versions.assign_issue_to_version(
        FakeClient(), cache, issue_id=42, version_id=7,
    )
    assert result["issue"]["fixed_version"]["id"] == 7
    assert captured["issue_id"] == 42
    assert captured["fixed_version_id"] == 7


async def test_assign_issue_to_version_unassign_with_zero(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing version_id=0 clears the version (sends empty string)."""
    captured: dict[str, Any] = {}

    async def fake_update(client, cache, issue_id, **kwargs):
        captured.update(kwargs)
        return {"issue": {"id": issue_id, "fixed_version": None}}

    monkeypatch.setattr(versions.issues_module, "update_issue", fake_update)
    await versions.assign_issue_to_version(
        FakeClient(), cache, issue_id=42, version_id=0,
    )
    # Empty string is the Redmine "unassign" sentinel.
    assert captured["fixed_version_id"] == ""


async def test_assign_issue_to_version_propagates_error(
    cache: SchemaCache, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_update(client, cache, issue_id, **kwargs):
        return {"error": "redmine_api_404", "hint": "gone"}

    monkeypatch.setattr(versions.issues_module, "update_issue", fake_update)
    result = await versions.assign_issue_to_version(
        FakeClient(), cache, issue_id=999, version_id=7,
    )
    assert result["error"] == "redmine_api_404"
