"""Attachment tools.

Tools (2):
  - upload_attachment   — path-restricted file upload, optionally attach to an issue
  - download_attachment — fetch attachment bytes by id and write to a path
                          inside ``Config.allowed_directories``

Upload — two-step Redmine flow:
  1. ``POST /uploads.json`` with raw bytes + ``Content-Type: application/octet-stream``
     returns ``{"upload": {"token": "<token>", "id": <id>}}``.
  2. (optional) ``PUT /issues/{id}.json`` with
     ``{"issue": {"uploads": [{"token", "filename", "content_type", "description"}]}}``.

Download — two-step:
  1. ``GET /attachments/{id}.json`` returns metadata (filename, filesize,
     content_type).
  2. ``GET /attachments/download/{id}/{filename}`` returns the raw bytes.
  Bytes are written to ``save_to`` (parent dir must be under
  ``allowed_directories``); existing files require ``overwrite=True``.

Path safety:
  Both upload and download are restricted to files under
  ``Config.allowed_directories`` (default: ``/tmp``, expandable via
  ``REDMINE_MCP_ALLOWED_DIRECTORIES``). The check resolves symlinks before
  comparing, so a symlink under ``/tmp`` pointing at ``/etc/shadow`` is
  rejected on read AND write.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import AttachmentPathDenied, RedmineAPIError

# Post-PUT verify retry backoff seconds — empirically 5s is enough to clear
# Redmine's per-issue rate-limiting silent-drop window (ClaudeCode#3139).
# Tunable for tests (which monkeypatch to (0, 0)).
ATTACHMENT_VERIFY_BACKOFFS: tuple[float, ...] = (2.0, 5.0)


def _is_path_allowed(path: Path, allowed: tuple[Path, ...]) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for an EXISTING file path (upload).

    ``reason`` is ``"ok"`` when allowed, ``"not_a_file"`` if the path
    doesn't resolve to a regular file, or ``"outside_allowlist"`` if it
    resolves outside every allowed root.
    """
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False, "not_a_file"
    if not resolved.is_file():
        return False, "not_a_file"
    for base in allowed:
        try:
            base_resolved = base.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        try:
            resolved.relative_to(base_resolved)
            return True, "ok"
        except ValueError:
            continue
    return False, "outside_allowlist"


def _is_save_path_allowed(
    path: Path,
    allowed: tuple[Path, ...],
    *,
    overwrite: bool,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a SAVE-TO target path (download).

    The target may not exist yet. Resolves the parent directory (must
    exist + be under ``allowed``) and rejects existing files unless
    ``overwrite`` is set.

    ``reason`` is ``"ok"``, ``"parent_missing"`` (parent doesn't exist or
    isn't a directory), ``"outside_allowlist"`` (parent resolves outside
    every allowed root), or ``"exists_no_overwrite"``.
    """
    expanded = path.expanduser()
    if not expanded.name:
        return False, "parent_missing"
    parent = expanded.parent
    try:
        parent_resolved = parent.resolve()
    except (OSError, RuntimeError):
        return False, "parent_missing"
    if not parent_resolved.is_dir():
        return False, "parent_missing"
    in_allowed = False
    for base in allowed:
        try:
            base_resolved = base.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        try:
            parent_resolved.relative_to(base_resolved)
            in_allowed = True
            break
        except ValueError:
            continue
    if not in_allowed:
        return False, "outside_allowlist"
    target = parent_resolved / expanded.name
    if target.exists() and not overwrite:
        return False, "exists_no_overwrite"
    return True, "ok"


async def upload_attachment(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    file_path: str,
    *,
    allowed_directories: tuple[Path, ...],
    issue_id: int | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Upload a file (path-restricted) and optionally attach it to an issue.

    Args:
        file_path: absolute or ``~``-relative path to a regular file under
            one of ``allowed_directories``.
        allowed_directories: tuple of permitted root directories (resolved
            from ``Config.allowed_directories``). Symlinks are resolved
            before comparison.
        issue_id: if provided, also attach the upload to this issue via a
            second PUT call. If omitted/None, return the bare upload token
            so the caller can attach it later (mirrors jztan/onozaty).
        description: optional human-readable description for the
            attachment row (only meaningful when ``issue_id`` is set).

    Returns ``{"upload": {token, id, filename, size}, "attached_to_issue":
    issue_id, "source": "api"}`` on success, or a structured error.
    """
    path = Path(file_path)
    ok, reason = _is_path_allowed(path, allowed_directories)
    if not ok:
        return AttachmentPathDenied(
            path=file_path,
            allowed_directories=[str(p) for p in allowed_directories],
            reason=reason,
        ).as_dict()

    resolved = path.expanduser().resolve()
    try:
        data = resolved.read_bytes()
    except OSError as e:
        return {
            "error": "attachment_read_failed",
            "hint": f"Could not read {file_path!r}: {e}",
            "path": file_path,
        }
    size = len(data)
    filename = resolved.name
    content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"

    try:
        upload_resp = await client.post_binary(
            "/uploads.json",
            data=data,
            content_type="application/octet-stream",
        )
    except RedmineAPIError as e:
        return e.as_structured()

    upload = (upload_resp or {}).get("upload") if isinstance(upload_resp, dict) else None
    if not isinstance(upload, dict) or "token" not in upload:
        return {
            "error": "upload_response_malformed",
            "hint": "Redmine /uploads.json did not return a token.",
            "body": upload_resp,
        }

    result: dict[str, Any] = {
        "upload": {
            "token": upload.get("token"),
            "id": upload.get("id"),
            "filename": filename,
            "size": size,
            "content_type": content_type,
        },
        "attached_to_issue": issue_id,
        "source": "api",
    }

    if issue_id is None:
        return result

    attachment_entry: dict[str, Any] = {
        "token": upload["token"],
        "filename": filename,
        "content_type": content_type,
    }
    if description:
        attachment_entry["description"] = description

    # ClaudeCode#3139: Redmine's PUT /issues/{id}.json with uploads silently
    # drops the attachment under per-issue rate pressure (observed: ~1 PUT/sec
    # cap). HTTP 200 is returned regardless. We do the PUT, then GET-verify
    # the attachment landed; if not, sleep + retry PUT with the same token
    # (tokens are stable across re-attach attempts within their TTL).
    put_attempts = 1 + len(ATTACHMENT_VERIFY_BACKOFFS)
    last_attempt_attached = False
    last_get_payload: Any = None
    for attempt in range(put_attempts):
        if attempt > 0:
            await asyncio.sleep(ATTACHMENT_VERIFY_BACKOFFS[attempt - 1])
        try:
            await client.put(
                f"/issues/{issue_id}.json",
                json={"issue": {"uploads": [attachment_entry]}},
            )
        except RedmineAPIError as e:
            # Surface the API failure but keep the token so the caller can
            # retry the attach without re-uploading the bytes.
            err = e.as_structured()
            err["upload"] = result["upload"]
            err["hint"] = (
                (err.get("hint") or "")
                + " Upload succeeded; attaching to the issue failed. "
                "Retry the attach with the returned token."
            ).strip()
            return err

        # Verify the attachment actually landed by GETing the issue and
        # checking attachments[] for our filename. (We also have the upload
        # id but Redmine's attachments[] returns a separate id once attached,
        # so filename is the durable join key.)
        try:
            verify_payload = await client.get(
                f"/issues/{issue_id}.json",
                params={"include": "attachments"},
            )
        except RedmineAPIError:
            # Verification GET itself failed; assume PUT worked rather than
            # eat the token. Caller can manually verify if needed.
            return result
        last_get_payload = verify_payload
        issue_dict = (verify_payload or {}).get("issue") if isinstance(verify_payload, dict) else None
        attachments = (issue_dict or {}).get("attachments") or []
        if any(a.get("filename") == filename for a in attachments):
            last_attempt_attached = True
            break

    if not last_attempt_attached:
        # PUT returned 200, but the attachment never showed up in the issue's
        # attachments[] across all retries. Surface a structured error with
        # the token so the caller can manually re-attach or escalate.
        return {
            "error": "attachment_not_attached",
            "hint": (
                f"PUT /issues/{issue_id}.json returned 200 but the file "
                f"{filename!r} did not appear in attachments[] across "
                f"{put_attempts} attempts (Redmine silently dropped the "
                f"attachment, most commonly per-issue rate-limiting). "
                "Upload token preserved; manual re-attach via "
                "redmine_request PUT with the token may succeed."
            ),
            "upload": result["upload"],
            "attempts": put_attempts,
            "verified_attachments": [
                {"id": a.get("id"), "filename": a.get("filename")}
                for a in (
                    (last_get_payload or {}).get("issue", {}).get("attachments", [])
                    if isinstance(last_get_payload, dict)
                    else []
                )
            ],
        }

    return result


async def download_attachment(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    attachment_id: int,
    save_to: str,
    *,
    allowed_directories: tuple[Path, ...],
    overwrite: bool = False,
) -> dict[str, Any]:
    """Download an attachment by id and write it to a path-restricted location.

    Args:
        attachment_id: numeric Redmine attachment id (visible on issue
            payloads under ``attachments[].id``).
        save_to: target file path; the parent directory must exist and
            resolve under one of ``allowed_directories``. Symlinks are
            resolved before checking.
        allowed_directories: tuple of permitted root directories
            (resolved from ``Config.allowed_directories``).
        overwrite: if False (default), refuse to overwrite an existing
            file at ``save_to``.

    Returns ``{"path": str, "size": int, "expected_size": int,
    "filename": str, "content_type": str, "attachment_id": int,
    "source": "api"}`` on success, or a structured error.
    """
    target = Path(save_to)
    ok, reason = _is_save_path_allowed(
        target, allowed_directories, overwrite=overwrite
    )
    if not ok:
        return AttachmentPathDenied(
            path=save_to,
            allowed_directories=[str(p) for p in allowed_directories],
            reason=reason,
        ).as_dict()

    try:
        meta_resp = await client.get(f"/attachments/{attachment_id}.json")
    except RedmineAPIError as e:
        return e.as_structured()
    meta = (meta_resp or {}).get("attachment") if isinstance(meta_resp, dict) else None
    if not isinstance(meta, dict) or "filename" not in meta:
        return {
            "error": "attachment_metadata_malformed",
            "hint": f"Redmine /attachments/{attachment_id}.json did not return a filename.",
            "body": meta_resp,
        }

    filename = meta["filename"]
    expected_size = meta.get("filesize")
    content_type = meta.get("content_type") or "application/octet-stream"

    try:
        data = await client.get_binary(
            f"/attachments/download/{attachment_id}/{filename}"
        )
    except RedmineAPIError as e:
        return e.as_structured()

    actual_size = len(data)
    if isinstance(expected_size, int) and actual_size != expected_size:
        return {
            "error": "attachment_size_mismatch",
            "hint": (
                f"Downloaded {actual_size} bytes but Redmine metadata reported "
                f"{expected_size}. Skipping write to avoid corrupting {save_to!r}."
            ),
            "attachment_id": attachment_id,
            "expected_size": expected_size,
            "actual_size": actual_size,
        }

    resolved_target = target.expanduser().parent.resolve() / target.name
    try:
        resolved_target.write_bytes(data)
    except OSError as e:
        return {
            "error": "attachment_write_failed",
            "hint": f"Could not write {save_to!r}: {e}",
            "path": save_to,
        }

    return {
        "path": str(resolved_target),
        "size": actual_size,
        "expected_size": expected_size,
        "filename": filename,
        "content_type": content_type,
        "attachment_id": attachment_id,
        "source": "api",
    }
