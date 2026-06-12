"""Unit tests for Phase 5 comment tools (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import comments


class FakeClient:
    """Stand-in for RedmineClient — see test_issues.py for the canonical version."""

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


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


# ---------------------------------------------------------------------
# add_comment
# ---------------------------------------------------------------------


async def test_add_comment_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({("PUT", "/issues/42.json"): None})
    result = await comments.add_comment(client, cache, 42, "looks good to me")
    assert result == {
        "issue_id": 42,
        "note": "looks good to me",
        "private": False,
        "source": "api",
    }
    put_payload = client.calls[-1][2]
    assert put_payload == {"issue": {"notes": "looks good to me"}}


async def test_add_comment_private_flag_sets_private_notes(cache: SchemaCache) -> None:
    client = FakeClient({("PUT", "/issues/42.json"): None})
    result = await comments.add_comment(
        client, cache, 42, "internal context", private=True
    )
    assert result["private"] is True
    put_payload = client.calls[-1][2]
    assert put_payload["issue"]["private_notes"] is True


async def test_add_comment_rejects_empty_note(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await comments.add_comment(client, cache, 42, "   ")
    assert result["error"] == "validation_failed"
    assert result["errors"][0]["error"] == "required_field_missing"
    # No HTTP call should fire on validation rejection.
    assert client.calls == []


async def test_add_comment_propagates_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("PUT", "/issues/999.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]}
        ),
    })
    result = await comments.add_comment(client, cache, 999, "hi")
    assert result["error"] == "redmine_api_404"
    assert result["status_code"] == 404


async def test_add_comment_propagates_403(cache: SchemaCache) -> None:
    """Server-side role rejection (the case our client-side check skips)."""
    client = FakeClient(errors={
        ("PUT", "/issues/42.json"): RedmineAPIError(
            status_code=403, body={"errors": ["You are not authorized"]}
        ),
    })
    result = await comments.add_comment(client, cache, 42, "denied")
    assert result["error"] == "redmine_api_403"


# ---------------------------------------------------------------------
# get_journals
# ---------------------------------------------------------------------


async def test_get_journals_returns_journal_list(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues/42.json"): {
            "issue": {
                "id": 42,
                "subject": "demo",
                "journals": [
                    {"id": 1, "user": {"id": 5, "name": "Léon"},
                     "notes": "first comment", "created_on": "2026-05-04T10:00:00Z",
                     "details": []},
                    {"id": 2, "user": {"id": 1, "name": "Avic"},
                     "notes": "", "created_on": "2026-05-04T10:05:00Z",
                     "details": [{"property": "attr", "name": "subject",
                                  "old_value": "old", "new_value": "demo"}]},
                ],
            }
        },
    })
    result = await comments.get_journals(client, cache, 42)
    assert result["issue_id"] == 42
    assert result["source"] == "api"
    assert len(result["journals"]) == 2
    assert result["journals"][0]["notes"] == "first comment"
    # Verify the include parameter was passed.
    sent_params = client.calls[-1][2]
    assert sent_params == {"include": "journals"}


async def test_get_journals_empty_when_issue_has_none(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues/42.json"): {"issue": {"id": 42, "subject": "no comments"}},
    })
    result = await comments.get_journals(client, cache, 42)
    assert result["journals"] == []
    assert result["issue_id"] == 42


async def test_get_journals_propagates_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("GET", "/issues/999.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]}
        ),
    })
    # get_issue catches the RedmineAPIError and returns it as a structured dict
    # only when called from inside _wrap; called directly it raises. comments.get_journals
    # invokes get_issue directly, so the exception surfaces here.
    with pytest.raises(RedmineAPIError):
        await comments.get_journals(client, cache, 999)


async def test_get_journals_returns_not_found_when_issue_payload_empty(
    cache: SchemaCache,
) -> None:
    client = FakeClient({("GET", "/issues/99.json"): {"issue": None}})
    result = await comments.get_journals(client, cache, 99)
    assert result["error"] == "issue_not_found"
    assert result["issue_id"] == 99


# ---------------------------------------------------------------------
# update_journal
# ---------------------------------------------------------------------


async def test_update_journal_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({("PUT", "/journals/10355.json"): None})
    result = await comments.update_journal(client, cache, 10355, "fixed note")
    assert result == {
        "journal_id": 10355,
        "notes": "fixed note",
        "source": "api",
    }
    put_payload = client.calls[-1][2]
    assert put_payload == {"journal": {"notes": "fixed note"}}


async def test_update_journal_empty_notes_clears(cache: SchemaCache) -> None:
    client = FakeClient({("PUT", "/journals/100.json"): None})
    result = await comments.update_journal(client, cache, 100, "")
    assert result["journal_id"] == 100
    assert result["notes"] == ""
    put_payload = client.calls[-1][2]
    assert put_payload == {"journal": {"notes": ""}}


async def test_update_journal_propagates_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("PUT", "/journals/999.json"): RedmineAPIError(
            status_code=404, body={"errors": ["Not found"]}
        ),
    })
    result = await comments.update_journal(client, cache, 999, "hi")
    assert result["error"] == "redmine_api_404"
    assert result["status_code"] == 404


async def test_update_journal_propagates_403(cache: SchemaCache) -> None:
    client = FakeClient(errors={
        ("PUT", "/journals/42.json"): RedmineAPIError(
            status_code=403, body={"errors": ["You are not authorized"]}
        ),
    })
    result = await comments.update_journal(client, cache, 42, "denied")
    assert result["error"] == "redmine_api_403"
