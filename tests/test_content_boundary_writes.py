"""Boundary tags must never be STORED in Redmine.

Read responses wrap user text in ``<redmine_user_content>`` tags (#40869). A
caller that reads a description, edits it and writes it back used to send the
tags along, and nothing removed them — so Redmine stored them, humans saw them,
and the next read wrapped the already-tagged text again.

Every write leaves through ``RedmineClient._request(json=...)``, which now
strips the tags. These tests drive the real server tools and the real client
against an in-memory Redmine (``httpx.MockTransport``) and assert on what the
fake server actually stored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from redmine_mcp import server
from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.client import RedmineClient
from redmine_mcp.config import Config
from redmine_mcp.content_boundary import (
    USER_CONTENT_BEGIN,
    USER_CONTENT_END,
    strip_user_content_tags,
    strip_user_content_tags_deep,
    wrap_user_content,
)

URL = "http://redmine.test"
TAGS = (USER_CONTENT_BEGIN, USER_CONTENT_END)

# ---------------------------------------------------------------------
# strip_user_content_tags
# ---------------------------------------------------------------------

ROUND_TRIP_VALUES = [
    "plain",
    "Line one\n\n## Heading\n- item\n| a | b |",
    "\nleading newline",
    "trailing newline\n",
    "\n\nboth\n\n",
    "  padded  ",
    "unicode — ✓ ünïcode",
]


@pytest.mark.parametrize("value", ROUND_TRIP_VALUES)
def test_strip_undoes_wrap_exactly(value: str) -> None:
    wrapped = wrap_user_content(value)
    assert wrapped != value
    assert strip_user_content_tags(wrapped) == value


def test_strip_whole_value_tolerates_surrounding_whitespace() -> None:
    wrapped = "\n  " + wrap_user_content("body") + "\n"
    assert strip_user_content_tags(wrapped) == "body"


def test_strip_removes_a_quoted_excerpt_inside_a_value() -> None:
    quoted = f"Previously:\n{wrap_user_content('old text')}\nNow fixed."
    assert strip_user_content_tags(quoted) == "Previously:\nold text\nNow fixed."


def test_strip_removes_bare_exact_tag_strings() -> None:
    assert strip_user_content_tags(f"a {USER_CONTENT_BEGIN}b{USER_CONTENT_END} c") == "a b c"
    assert strip_user_content_tags(f"dangling {USER_CONTENT_END}") == "dangling "


@pytest.mark.parametrize(
    "value",
    [
        "no tags at all",
        "<Redmine_User_Content>case differs</Redmine_User_Content>",
        '<redmine_user_content id="x">attribute</redmine_user_content >',
        "&lt;redmine_user_content&gt;escaped&lt;/redmine_user_content&gt;",
        "<redmine_user_contents>near miss",
        "",
    ],
)
def test_strip_leaves_lookalikes_alone(value: str) -> None:
    assert strip_user_content_tags(value) == value


def test_strip_deep_walks_bodies_and_spares_keys_and_non_strings() -> None:
    body = {
        "issue": {
            "subject": wrap_user_content("subj"),
            "custom_fields": [{"id": 2, "value": wrap_user_content("held reason")}],
            "due_date": None,
            "done_ratio": 40,
            "is_private": False,
        },
        USER_CONTENT_BEGIN: "keys are not values",
    }
    out = strip_user_content_tags_deep(body)
    assert out["issue"]["subject"] == "subj"
    assert out["issue"]["custom_fields"] == [{"id": 2, "value": "held reason"}]
    assert out["issue"]["due_date"] is None
    assert out["issue"]["done_ratio"] == 40
    assert out["issue"]["is_private"] is False
    assert USER_CONTENT_BEGIN in out


# ---------------------------------------------------------------------
# in-memory Redmine
# ---------------------------------------------------------------------


class FakeRedmine:
    """Stores exactly what each write sends, and serves it back on read."""

    def __init__(self) -> None:
        self.description = "Line one\n\n## Heading\n- item"
        self.journals: list[dict[str, Any]] = [{"id": 5, "notes": "first comment", "details": []}]
        self.wiki_text = "# Page\n\nBody text."
        self.wiki_comments: str | None = None
        self.bodies: list[Any] = []

    def _issue(self) -> dict[str, Any]:
        return {
            "issue": {
                "id": 1,
                "subject": "Subject",
                "tracker": {"id": 1, "name": "Bug"},
                "project": {"id": 15, "name": "Proj"},
                "status": {"id": 1, "name": "New"},
                "description": self.description,
                "journals": self.journals,
                "custom_fields": [],
            }
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        if body is not None:
            self.bodies.append(body)
        if path == "/issues/1.json" and method == "GET":
            return httpx.Response(200, json=self._issue())
        if path == "/issues/1.json" and method == "PUT":
            issue = body["issue"]
            if "description" in issue:
                self.description = issue["description"]
            if "notes" in issue:
                self.journals.append({"id": 6, "notes": issue["notes"], "details": []})
            return httpx.Response(204)
        if path == "/journals/5.json" and method == "PUT":
            self.journals[0]["notes"] = body["journal"]["notes"]
            return httpx.Response(204)
        if path == "/projects/p/wiki/Page.json" and method == "GET":
            page = {"title": "Page", "text": self.wiki_text, "version": 1}
            return httpx.Response(200, json={"wiki_page": page})
        if path == "/projects/p/wiki/Page.json" and method == "PUT":
            self.wiki_text = body["wiki_page"]["text"]
            self.wiki_comments = body["wiki_page"].get("comments")
            return httpx.Response(204)
        if path == "/echo.json":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"errors": [f"no route {method} {path}"]})

    def all_sent_strings(self) -> list[str]:
        out: list[str] = []

        def walk(v: Any) -> None:
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, dict):
                for item in v.values():
                    walk(item)
            elif isinstance(v, list):
                for item in v:
                    walk(item)

        walk(self.bodies)
        return out


def _real_client(fake: FakeRedmine, cfg: Config) -> RedmineClient:
    client = RedmineClient(cfg)
    client._client = httpx.AsyncClient(  # type: ignore[assignment]
        base_url=cfg.redmine_url, transport=httpx.MockTransport(fake.handler)
    )
    return client


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRedmine:
    fake = FakeRedmine()
    cfg = Config(redmine_url=URL, api_key="dummy-test-key", read_only=False)
    cache = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    monkeypatch.setattr(server, "_get_config", lambda: cfg)
    monkeypatch.setattr(server, "_get_cache", lambda: cache)
    monkeypatch.setattr(server, "RedmineClient", lambda c: _real_client(fake, c))
    return fake


def _assert_nothing_tagged_was_sent(fake: FakeRedmine) -> None:
    assert fake.bodies, "the write never reached the fake server"
    for s in fake.all_sent_strings():
        assert not any(t in s for t in TAGS), s


# ---------------------------------------------------------------------
# read -> write back -> nothing tagged is stored
# ---------------------------------------------------------------------


async def test_issue_description_round_trip_stores_no_tags(fake: FakeRedmine) -> None:
    original = fake.description
    read = json.loads(await server.redmine_get_issue(issue_id=1))
    wrapped = read["issue"]["description"]
    assert wrapped.startswith(USER_CONTENT_BEGIN), "precondition: reads are wrapped"

    out = json.loads(await server.redmine_update_issue(issue_id=1, description=wrapped))
    assert "error" not in out, out
    assert fake.description == original
    _assert_nothing_tagged_was_sent(fake)


async def test_note_quoting_an_earlier_comment_stores_no_tags(fake: FakeRedmine) -> None:
    read = json.loads(await server.redmine_get_issue(issue_id=1))
    quoted = read["issue"]["journals"][0]["notes"]
    note = f"Following up on:\n{quoted}\nDone now."

    out = json.loads(await server.redmine_add_comment(issue_id=1, note=note))
    assert "error" not in out, out
    assert fake.journals[-1]["notes"] == "Following up on:\nfirst comment\nDone now."
    _assert_nothing_tagged_was_sent(fake)


async def test_journal_edit_round_trip_stores_no_tags(fake: FakeRedmine) -> None:
    read = json.loads(await server.redmine_get_journals(issue_id=1))
    wrapped = read["journals"][0]["notes"]
    assert wrapped.startswith(USER_CONTENT_BEGIN)

    out = json.loads(await server.redmine_update_journal(journal_id=5, notes=wrapped + " (edited)"))
    assert "error" not in out, out
    assert fake.journals[0]["notes"] == "first comment (edited)"
    _assert_nothing_tagged_was_sent(fake)


async def test_wiki_text_round_trip_stores_no_tags(fake: FakeRedmine) -> None:
    original = fake.wiki_text
    read = json.loads(await server.redmine_get_wiki_page(project="p", title="Page"))
    wrapped = read["page"]["text"]
    assert wrapped.startswith(USER_CONTENT_BEGIN)

    out = json.loads(
        await server.redmine_update_wiki_page(
            project="p", title="Page", text=wrapped, comments=wrap_user_content("why")
        )
    )
    assert "error" not in out, out
    assert fake.wiki_text == original
    assert fake.wiki_comments == "why"
    _assert_nothing_tagged_was_sent(fake)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
async def test_every_json_write_method_is_stripped(fake: FakeRedmine, method: str) -> None:
    """Subjects, news, forum posts, custom fields, passthrough bodies: one path."""
    cfg = Config(redmine_url=URL, api_key="dummy-test-key")
    body = {
        "news": {"title": wrap_user_content("t"), "summary": wrap_user_content("s")},
        "message": {"subject": wrap_user_content("re"), "content": wrap_user_content("c")},
        "custom_fields": [{"id": 2, "value": wrap_user_content("held reason")}],
    }
    async with _real_client(fake, cfg) as client:
        await client._request(method, "/echo.json", json=body)
    _assert_nothing_tagged_was_sent(fake)
    assert fake.bodies[-1]["news"] == {"title": "t", "summary": "s"}
    assert fake.bodies[-1]["custom_fields"] == [{"id": 2, "value": "held reason"}]
