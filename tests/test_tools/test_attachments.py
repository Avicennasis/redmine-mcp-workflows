"""Unit tests for Phase 5 attachment tools (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import attachments


class FakeClient:
    """Stand-in for RedmineClient with binary GET/POST support.

    A response value may be a single payload (every call returns it) or a
    list of payloads (queue — each call pops the next; the last entry is
    reused if the list runs out). The queue form lets tests model
    sequential GETs that need to differ — e.g. pre-PUT vs post-PUT
    verification reads in the attachment retry path (ClaudeCode#3139).
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
        self._consumed: dict[tuple[str, str], int] = {}

    def _next_response(self, key: tuple[str, str]) -> Any:
        value = self._responses.get(key)
        if isinstance(value, list):
            idx = self._consumed.get(key, 0)
            chosen = value[idx] if idx < len(value) else value[-1]
            self._consumed[key] = idx + 1
            return chosen
        return value

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if ("GET", path) in self._errors:
            raise self._errors[("GET", path)]
        return self._next_response(("GET", path))

    async def get_binary(self, path: str) -> bytes:
        self.calls.append(("GET_BIN", path, None))
        if ("GET_BIN", path) in self._errors:
            raise self._errors[("GET_BIN", path)]
        return self._responses.get(("GET_BIN", path), b"")

    async def post_binary(
        self, path: str, *, data: bytes, content_type: str = "application/octet-stream"
    ) -> Any:
        self.calls.append(("POST_BIN", path, {"size": len(data), "ct": content_type}))
        if ("POST_BIN", path) in self._errors:
            raise self._errors[("POST_BIN", path)]
        return self._responses.get(("POST_BIN", path))

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


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, tuple[Path, ...]]:
    """A safe upload root + a sample file inside it."""
    f = tmp_path / "sample.txt"
    f.write_text("hello world\n")
    return f, (tmp_path,)


# ---------------------------------------------------------------------
# _is_path_allowed
# ---------------------------------------------------------------------


def test_is_path_allowed_accepts_file_under_root(tmp_path: Path) -> None:
    f = tmp_path / "ok.txt"
    f.write_text("x")
    ok, reason = attachments._is_path_allowed(f, (tmp_path,))
    assert ok is True
    assert reason == "ok"


def test_is_path_allowed_rejects_outside_allowlist(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    inside_other = other / "x.txt"
    inside_other.write_text("x")
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    ok, reason = attachments._is_path_allowed(inside_other, (allowed_root,))
    assert ok is False
    assert reason == "outside_allowlist"


def test_is_path_allowed_rejects_missing_file(tmp_path: Path) -> None:
    ok, reason = attachments._is_path_allowed(tmp_path / "ghost.txt", (tmp_path,))
    assert ok is False
    assert reason == "not_a_file"


def test_is_path_allowed_rejects_directory(tmp_path: Path) -> None:
    ok, reason = attachments._is_path_allowed(tmp_path, (tmp_path,))
    assert ok is False
    assert reason == "not_a_file"


def test_is_path_allowed_resolves_symlink_escape(tmp_path: Path) -> None:
    """A symlink under the allowlist that points OUTSIDE must be rejected."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    link = allowed / "decoy.txt"
    link.symlink_to(outside)
    ok, reason = attachments._is_path_allowed(link, (allowed,))
    assert ok is False
    assert reason == "outside_allowlist"


# ---------------------------------------------------------------------
# upload_attachment
# ---------------------------------------------------------------------


async def test_upload_rejects_path_outside_allowlist(cache: SchemaCache, tmp_path: Path) -> None:
    """A file that doesn't fall under any allowed root is denied without I/O."""
    other = tmp_path / "other"
    other.mkdir()
    f = other / "evil.txt"
    f.write_text("nope")
    allowed_root = tmp_path / "ok"
    allowed_root.mkdir()
    client = FakeClient()
    result = await attachments.upload_attachment(
        client, cache, str(f), allowed_directories=(allowed_root,)
    )
    assert result["error"] == "attachment_path_denied"
    assert result["reason"] == "outside_allowlist"
    assert client.calls == []


async def test_upload_rejects_nonexistent_path(cache: SchemaCache, tmp_path: Path) -> None:
    client = FakeClient()
    result = await attachments.upload_attachment(
        client,
        cache,
        str(tmp_path / "ghost.txt"),
        allowed_directories=(tmp_path,),
    )
    assert result["error"] == "attachment_path_denied"
    assert result["reason"] == "not_a_file"
    assert client.calls == []


async def test_upload_happy_path_no_issue_attach(
    cache: SchemaCache, sandbox: tuple[Path, tuple[Path, ...]]
) -> None:
    f, allowed = sandbox
    client = FakeClient(
        {
            ("POST_BIN", "/uploads.json"): {"upload": {"token": "abc123", "id": 7}},
        }
    )
    result = await attachments.upload_attachment(client, cache, str(f), allowed_directories=allowed)
    assert result["upload"]["token"] == "abc123"
    assert result["upload"]["filename"] == "sample.txt"
    assert result["upload"]["size"] == len("hello world\n")
    assert result["attached_to_issue"] is None
    # Only the upload POST should fire — no PUT to attach.
    assert len(client.calls) == 1
    assert client.calls[0][0] == "POST_BIN"


async def test_upload_happy_path_attaches_to_issue(
    cache: SchemaCache, sandbox: tuple[Path, tuple[Path, ...]]
) -> None:
    f, allowed = sandbox
    client = FakeClient(
        {
            ("POST_BIN", "/uploads.json"): {"upload": {"token": "tok", "id": 9}},
            ("PUT", "/issues/42.json"): None,
            # Post-PUT verify GET (ClaudeCode#3139) — attachment is present.
            ("GET", "/issues/42.json"): {
                "issue": {"id": 42, "attachments": [{"id": 100, "filename": "sample.txt"}]},
            },
        }
    )
    result = await attachments.upload_attachment(
        client,
        cache,
        str(f),
        allowed_directories=allowed,
        issue_id=42,
        description="logs from incident",
    )
    assert result["attached_to_issue"] == 42
    # POST_BIN → PUT → GET (verify). The verify GET is what catches the
    # silent-drop case from #3139; on the happy path it fires once and finds
    # the attachment immediately.
    methods = [c[0] for c in client.calls]
    assert methods == ["POST_BIN", "PUT", "GET"]
    put_payload = client.calls[1][2]["issue"]
    assert put_payload["uploads"][0]["token"] == "tok"
    assert put_payload["uploads"][0]["filename"] == "sample.txt"
    assert put_payload["uploads"][0]["description"] == "logs from incident"


async def test_upload_propagates_post_422(
    cache: SchemaCache, sandbox: tuple[Path, tuple[Path, ...]]
) -> None:
    f, allowed = sandbox
    client = FakeClient(
        errors={
            ("POST_BIN", "/uploads.json"): RedmineAPIError(
                status_code=422, body={"errors": ["File too large"]}
            ),
        }
    )
    result = await attachments.upload_attachment(client, cache, str(f), allowed_directories=allowed)
    assert result["error"] == "redmine_api_422"
    assert result["status_code"] == 422


async def test_upload_attach_failure_returns_token_for_retry(
    cache: SchemaCache, sandbox: tuple[Path, tuple[Path, ...]]
) -> None:
    """Upload succeeds but the issue-attach PUT 422s — caller should still get the token."""
    f, allowed = sandbox
    client = FakeClient(
        responses={
            ("POST_BIN", "/uploads.json"): {"upload": {"token": "T", "id": 1}},
        },
        errors={
            ("PUT", "/issues/42.json"): RedmineAPIError(
                status_code=422, body={"errors": ["Subject can't be blank"]}
            ),
        },
    )
    result = await attachments.upload_attachment(
        client,
        cache,
        str(f),
        allowed_directories=allowed,
        issue_id=42,
    )
    assert result["error"] == "redmine_api_422"
    # Token should still be exposed so the caller can retry the attach.
    assert result["upload"]["token"] == "T"
    assert "Retry the attach" in result["hint"]


async def test_upload_handles_malformed_upload_response(
    cache: SchemaCache, sandbox: tuple[Path, tuple[Path, ...]]
) -> None:
    f, allowed = sandbox
    client = FakeClient(
        {
            ("POST_BIN", "/uploads.json"): {"unexpected": "shape"},
        }
    )
    result = await attachments.upload_attachment(client, cache, str(f), allowed_directories=allowed)
    assert result["error"] == "upload_response_malformed"


async def test_upload_recovers_from_silent_drop(
    cache: SchemaCache,
    sandbox: tuple[Path, tuple[Path, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for ClaudeCode#3139: first PUT silently drops the attachment
    (200 but attachments[] empty); after backoff a retry PUT lands it. The
    wrapper should succeed transparently."""
    # Zero out the backoffs so the test doesn't actually sleep.
    monkeypatch.setattr(attachments, "ATTACHMENT_VERIFY_BACKOFFS", (0.0, 0.0))
    f, allowed = sandbox
    client = FakeClient(
        {
            ("POST_BIN", "/uploads.json"): {"upload": {"token": "tok", "id": 9}},
            ("PUT", "/issues/42.json"): None,
            # First GET: no attachment (silent drop). Second GET: attachment present.
            ("GET", "/issues/42.json"): [
                {"issue": {"id": 42, "attachments": []}},
                {"issue": {"id": 42, "attachments": [{"id": 100, "filename": "sample.txt"}]}},
            ],
        }
    )
    result = await attachments.upload_attachment(
        client,
        cache,
        str(f),
        allowed_directories=allowed,
        issue_id=42,
    )
    assert result["attached_to_issue"] == 42
    assert "error" not in result
    # Two PUTs (initial + 1 retry), two GETs (one verify per PUT).
    methods = [c[0] for c in client.calls]
    assert methods == ["POST_BIN", "PUT", "GET", "PUT", "GET"]


async def test_upload_silent_drop_persistent_returns_structured_error(
    cache: SchemaCache,
    sandbox: tuple[Path, tuple[Path, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every retry silently drops the attachment, surface a structured
    error preserving the upload token so the caller can recover manually."""
    monkeypatch.setattr(attachments, "ATTACHMENT_VERIFY_BACKOFFS", (0.0, 0.0))
    f, allowed = sandbox
    client = FakeClient(
        {
            ("POST_BIN", "/uploads.json"): {"upload": {"token": "T", "id": 9}},
            ("PUT", "/issues/42.json"): None,
            # Every GET shows attachments empty — silent drops all the way down.
            ("GET", "/issues/42.json"): {"issue": {"id": 42, "attachments": []}},
        }
    )
    result = await attachments.upload_attachment(
        client,
        cache,
        str(f),
        allowed_directories=allowed,
        issue_id=42,
    )
    assert result["error"] == "attachment_not_attached"
    assert result["upload"]["token"] == "T"
    assert result["attempts"] == 3  # initial + 2 retries
    assert "rate-limiting" in result["hint"]
    # 3 PUTs + 3 verify GETs
    methods = [c[0] for c in client.calls]
    assert methods.count("PUT") == 3
    assert methods.count("GET") == 3


# ---------------------------------------------------------------------
# _is_save_path_allowed
# ---------------------------------------------------------------------


def test_save_path_allowed_under_allowed_root_no_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    ok, reason = attachments._is_save_path_allowed(target, (tmp_path,), overwrite=False)
    assert ok is True
    assert reason == "ok"


def test_save_path_rejects_outside_allowlist(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    ok, reason = attachments._is_save_path_allowed(
        other / "out.bin", (allowed_root,), overwrite=False
    )
    assert ok is False
    assert reason == "outside_allowlist"


def test_save_path_rejects_missing_parent(tmp_path: Path) -> None:
    ok, reason = attachments._is_save_path_allowed(
        tmp_path / "ghost" / "out.bin", (tmp_path,), overwrite=False
    )
    assert ok is False
    assert reason == "parent_missing"


def test_save_path_rejects_existing_file_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "exists.bin"
    target.write_text("x")
    ok, reason = attachments._is_save_path_allowed(target, (tmp_path,), overwrite=False)
    assert ok is False
    assert reason == "exists_no_overwrite"


def test_save_path_allows_existing_file_with_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "exists.bin"
    target.write_text("x")
    ok, reason = attachments._is_save_path_allowed(target, (tmp_path,), overwrite=True)
    assert ok is True
    assert reason == "ok"


def test_save_path_resolves_symlink_parent_escape(tmp_path: Path) -> None:
    """A parent that's a symlink pointing OUTSIDE the allowlist is rejected."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    decoy = allowed / "decoy"
    decoy.symlink_to(outside)
    target = decoy / "out.bin"
    ok, reason = attachments._is_save_path_allowed(target, (allowed,), overwrite=False)
    assert ok is False
    assert reason == "outside_allowlist"


# ---------------------------------------------------------------------
# download_attachment
# ---------------------------------------------------------------------


async def test_download_writes_bytes_and_returns_metadata(
    cache: SchemaCache, tmp_path: Path
) -> None:
    payload = b"hello downloaded\n"
    client = FakeClient(
        {
            ("GET", "/attachments/8.json"): {
                "attachment": {
                    "id": 8,
                    "filename": "demo.txt",
                    "filesize": len(payload),
                    "content_type": "text/plain",
                },
            },
            ("GET_BIN", "/attachments/download/8/demo.txt"): payload,
        }
    )
    save_to = tmp_path / "saved.txt"
    result = await attachments.download_attachment(
        client,
        cache,
        8,
        str(save_to),
        allowed_directories=(tmp_path,),
    )
    assert result["size"] == len(payload)
    assert result["filename"] == "demo.txt"
    assert result["content_type"] == "text/plain"
    assert result["attachment_id"] == 8
    # File was written with the right bytes.
    assert save_to.read_bytes() == payload
    # Both API calls fired in order.
    methods = [c[0] for c in client.calls]
    assert methods == ["GET", "GET_BIN"]


async def test_download_rejects_save_path_outside_allowlist(
    cache: SchemaCache, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    client = FakeClient()
    result = await attachments.download_attachment(
        client,
        cache,
        8,
        str(other / "x.bin"),
        allowed_directories=(allowed_root,),
    )
    assert result["error"] == "attachment_path_denied"
    assert result["reason"] == "outside_allowlist"
    assert client.calls == []


async def test_download_rejects_existing_file_without_overwrite(
    cache: SchemaCache, tmp_path: Path
) -> None:
    target = tmp_path / "exists.bin"
    target.write_text("keep me")
    client = FakeClient()
    result = await attachments.download_attachment(
        client,
        cache,
        8,
        str(target),
        allowed_directories=(tmp_path,),
    )
    assert result["error"] == "attachment_path_denied"
    assert result["reason"] == "exists_no_overwrite"
    # File was not touched.
    assert target.read_text() == "keep me"
    assert client.calls == []


async def test_download_overwrites_existing_when_flag_set(
    cache: SchemaCache, tmp_path: Path
) -> None:
    target = tmp_path / "out.txt"
    target.write_text("OLD")
    payload = b"NEW"
    client = FakeClient(
        {
            ("GET", "/attachments/8.json"): {
                "attachment": {
                    "id": 8,
                    "filename": "out.txt",
                    "filesize": len(payload),
                    "content_type": "text/plain",
                },
            },
            ("GET_BIN", "/attachments/download/8/out.txt"): payload,
        }
    )
    result = await attachments.download_attachment(
        client,
        cache,
        8,
        str(target),
        allowed_directories=(tmp_path,),
        overwrite=True,
    )
    assert result["size"] == 3
    assert target.read_bytes() == b"NEW"


async def test_download_rejects_size_mismatch_without_writing(
    cache: SchemaCache, tmp_path: Path
) -> None:
    """Short read should fail loudly rather than save a corrupt file."""
    payload = b"short"
    client = FakeClient(
        {
            ("GET", "/attachments/8.json"): {
                "attachment": {
                    "id": 8,
                    "filename": "demo.bin",
                    "filesize": 9999,
                    "content_type": "application/octet-stream",
                },
            },
            ("GET_BIN", "/attachments/download/8/demo.bin"): payload,
        }
    )
    save_to = tmp_path / "should_not_exist.bin"
    result = await attachments.download_attachment(
        client,
        cache,
        8,
        str(save_to),
        allowed_directories=(tmp_path,),
    )
    assert result["error"] == "attachment_size_mismatch"
    assert result["expected_size"] == 9999
    assert result["actual_size"] == len(payload)
    assert not save_to.exists()


async def test_download_propagates_404_metadata(cache: SchemaCache, tmp_path: Path) -> None:
    client = FakeClient(
        errors={
            ("GET", "/attachments/999.json"): RedmineAPIError(
                status_code=404, body={"errors": ["Not found"]}
            ),
        }
    )
    result = await attachments.download_attachment(
        client,
        cache,
        999,
        str(tmp_path / "x.bin"),
        allowed_directories=(tmp_path,),
    )
    assert result["error"] == "redmine_api_404"


async def test_download_handles_malformed_metadata(cache: SchemaCache, tmp_path: Path) -> None:
    client = FakeClient(
        {
            ("GET", "/attachments/8.json"): {"unexpected": "shape"},
        }
    )
    result = await attachments.download_attachment(
        client,
        cache,
        8,
        str(tmp_path / "x.bin"),
        allowed_directories=(tmp_path,),
    )
    assert result["error"] == "attachment_metadata_malformed"
