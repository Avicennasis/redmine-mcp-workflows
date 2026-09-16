"""Bulk-operation tools (Redmine #2381 + the original ticket).

Tools (3):
  - bulk_create_issues — create many issues from per-spec dicts, with
                         subject-based idempotency
  - bulk_update_issues — apply uniform field updates across many issues
  - bulk_close         — close many issues in one call (with optional note)

All three are thin orchestrators over ``issues.create_issue`` /
``issues.update_issue`` / ``issues.close_issue``. They:

  * validate the input shape once (non-empty list, at least one updatable
    field on the update tool, batch size ≤ MAX_BATCH_SIZE);
  * iterate the per-issue calls sequentially (Redmine's REST API doesn't
    support a single batch endpoint, and concurrent writes against
    overlapping rows can deadlock SQLite-backed Redmines);
  * aggregate the results into ``{succeeded, failed, skipped, total}``
    so a caller sees the whole picture from one return.

``stop_on_error=True`` halts the batch at the first failure; the
unprocessed remainder lands in ``skipped`` so callers can retry just
those. The default (False) is best-effort — every issue gets attempted,
errors get collected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError
from . import issues as issues_module

# Default sleep between per-issue POSTs in bulk_create_issues. Empirically
# 50ms is the floor for not tripping Redmine's per-issue update-rate cap
# on the small dev VM (see the original ticket follow-up notes). Configurable
# per-call.
DEFAULT_BULK_CREATE_PACING_S: float = 0.05

# Cap batch size to keep latency bounded and avoid accidentally PUTting
# the entire instance. Redmine has no batch endpoint — these are sequential
# PUTs — so the cap is a safety guardrail, not an API limit.
MAX_BATCH_SIZE = 1000

# Upper bound on parallel in-flight requests. Redmine rate-limits per issue,
# so this stays deliberately modest; the default is 1 (sequential).
MAX_CONCURRENCY = 50


def _is_server_error(result: Any) -> bool:
    """True if a per-item result is a 5xx API error."""
    if not isinstance(result, dict):
        return False
    code = result.get("status_code")
    return isinstance(code, int) and 500 <= code < 600


def _check_concurrency(
    concurrency: int,
    pacing_seconds: float,
) -> dict[str, Any] | None:
    """Validate ``concurrency`` and its mutual exclusion with ``pacing_seconds``.

    ``concurrency == 1`` (the default) is the legacy sequential path and keeps
    ``pacing_seconds``. ``concurrency > 1`` runs bounded-parallel and cannot
    also pace: the whole point of pacing is to bound the *per-issue* request
    rate, which fan-out deliberately multiplies. The default pacing value is
    treated as "not set", so ``concurrency=5`` alone is valid and simply
    ignores pacing.
    """
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        return {
            "error": "invalid_concurrency",
            "hint": f"concurrency must be between 1 and {MAX_CONCURRENCY}.",
            "concurrency": concurrency,
        }
    if concurrency > 1 and pacing_seconds not in (0.0, DEFAULT_BULK_CREATE_PACING_S):
        return {
            "error": "concurrency_pacing_conflict",
            "hint": (
                "concurrency > 1 is mutually exclusive with pacing_seconds: "
                "pick one throttle. Leave pacing_seconds at its default (or 0) "
                "to use concurrency, or set concurrency=1 to pace sequentially."
            ),
            "concurrency": concurrency,
            "pacing_seconds": pacing_seconds,
        }
    return None


async def _run_concurrent(
    count: int,
    worker: Callable[[int], Awaitable[dict[str, Any]]],
    *,
    concurrency: int,
    should_abort: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any] | None]:
    """Run ``worker(i)`` for ``i`` in ``range(count)`` with bounded parallelism.

    Returns a list the same length as the input, in input order; entries are
    ``None`` for items that were never started because ``should_abort`` fired
    on an earlier item. The shared cursor is advanced under a lock so each
    index is claimed exactly once; ``should_abort`` is checked after each
    result and stops workers from claiming further work.
    """
    results: list[dict[str, Any] | None] = [None] * count
    if count == 0:
        return results

    abort = asyncio.Event()
    cursor = 0
    cursor_lock = asyncio.Lock()

    async def runner() -> None:
        nonlocal cursor
        while not abort.is_set():
            async with cursor_lock:
                if abort.is_set() or cursor >= count:
                    return
                index = cursor
                cursor += 1
            result = await worker(index)
            results[index] = result
            if should_abort(result):
                abort.set()

    runners = [asyncio.create_task(runner()) for _ in range(min(concurrency, count))]
    await asyncio.gather(*runners)
    return results


def _validation_error(hint: str, *, field: str = "issue_ids") -> dict[str, Any]:
    return {
        "error": "validation_failed",
        "errors": [
            {
                "error": "required_field_missing",
                "hint": hint,
                "field": field,
                "op": "bulk",
            }
        ],
    }


def _check_batch_size(issue_ids: list[int]) -> dict[str, Any] | None:
    if not issue_ids:
        return _validation_error("issue_ids must be non-empty.")
    if len(issue_ids) > MAX_BATCH_SIZE:
        return {
            "error": "batch_too_large",
            "hint": (
                f"Batch of {len(issue_ids)} exceeds MAX_BATCH_SIZE "
                f"({MAX_BATCH_SIZE}). Split into smaller batches."
            ),
            "size": len(issue_ids),
            "max_batch_size": MAX_BATCH_SIZE,
        }
    return None


async def bulk_update_issues(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    issue_ids: list[int],
    subject: str | None = None,
    description: str | None = None,
    status: int | str | None = None,
    priority: int | str | None = None,
    assigned_to_id: int | None = None,
    notes: str | None = None,
    custom_fields: list[dict[str, Any]] | None = None,
    difficulty: str | None = None,
    held: str | bool | None = None,
    held_until: str | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
    done_ratio: int | None = None,
    stop_on_error: bool = False,
    concurrency: int = 1,
    difficulty_field_id: int | None = None,
    held_field_id: int | None = None,
    held_until_field_id: int | None = None,
) -> dict[str, Any]:
    """Apply the same field updates to every issue in ``issue_ids``.

    Returns ``{total, succeeded, failed, skipped}``. Each ``failed`` entry
    is ``{"issue_id": N, "error": "...", ...}`` — the underlying tool's
    error payload, with ``issue_id`` injected. ``skipped`` holds ids that
    were never attempted: the tail after ``stop_on_error``, or (when
    ``concurrency > 1``) whatever had not started when a 5xx aborted the run.

    ``concurrency`` (default 1) controls parallel in-flight PUTs. The default
    keeps the legacy sequential behaviour; ``> 1`` runs a bounded fan-out via
    ``asyncio.Semaphore`` and is mutually exclusive with pacing.
    """
    if (err := _check_batch_size(issue_ids)) is not None:
        return err
    # No pacing on this tool; pass 0 so only the range check applies.
    if (err := _check_concurrency(concurrency, 0.0)) is not None:
        return err

    # Validate the hold reason ONCE, up front. update_issue would reject each
    # issue individually, but a bad `held` is the same mistake N times, not N
    # mistakes — and on a bulk call it would otherwise overwrite every existing
    # reason in the batch at once.
    if (held_err := issues_module._validate_held_reason(held)) is not None:
        return held_err.as_dict()

    update_kwargs: dict[str, Any] = {}
    if subject is not None:
        update_kwargs["subject"] = subject
    if description is not None:
        update_kwargs["description"] = description
    if status is not None:
        update_kwargs["status"] = status
    if priority is not None:
        update_kwargs["priority"] = priority
    if assigned_to_id is not None:
        update_kwargs["assigned_to_id"] = assigned_to_id
    if notes is not None:
        update_kwargs["notes"] = notes
    if custom_fields is not None:
        update_kwargs["custom_fields"] = custom_fields
    if difficulty is not None:
        update_kwargs["difficulty"] = difficulty
    if held is not None:
        update_kwargs["held"] = held
    if held_until is not None:
        update_kwargs["held_until"] = held_until
    if due_date is not None:
        update_kwargs["due_date"] = due_date
    if start_date is not None:
        update_kwargs["start_date"] = start_date
    if done_ratio is not None:
        update_kwargs["done_ratio"] = done_ratio

    if not update_kwargs:
        return _validation_error(
            "At least one updatable field must be supplied.",
            field="fields",
        )

    succeeded: list[int] = []
    failed: list[dict[str, Any]] = []
    skipped: list[int] = []

    async def update_one(index: int) -> dict[str, Any]:
        return await issues_module.update_issue(
            client,
            cache,
            issue_ids[index],
            **update_kwargs,
            difficulty_field_id=difficulty_field_id,
            held_field_id=held_field_id,
            held_until_field_id=held_until_field_id,
        )

    if concurrency > 1:
        results = await _run_concurrent(
            len(issue_ids),
            update_one,
            concurrency=concurrency,
            should_abort=lambda r: _is_server_error(r) or (stop_on_error and "error" in r),
        )
        for issue_id, result in zip(issue_ids, results, strict=False):
            if result is None:
                skipped.append(issue_id)
            elif "error" in result:
                failed.append({"issue_id": issue_id, **result})
            else:
                succeeded.append(issue_id)
        return {
            "total": len(issue_ids),
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "concurrency": concurrency,
        }

    for idx, issue_id in enumerate(issue_ids):
        result = await update_one(idx)
        if isinstance(result, dict) and "error" in result:
            failed.append({"issue_id": issue_id, **result})
            if stop_on_error:
                skipped = list(issue_ids[idx + 1 :])
                break
        else:
            succeeded.append(issue_id)

    return {
        "total": len(issue_ids),
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }


async def _find_existing_by_subject(
    client: RedmineClient,
    project: int | str,
    subject: str,
) -> int | None:
    """Look up an existing issue by exact subject within ``project``.

    Mirrors the pattern from `ServerOps/scripts/redmine/create-*.py`:
    Redmine's `subject=X` filter is substring-fuzzy, so we GET with the
    full subject as the filter (≤5 results) then exact-string-match
    client-side. Returns the matching issue id or ``None`` if no
    exact-subject match in the project.

    Raises :class:`RedmineAPIError` for anything other than 404/422 —
    swallowing a transient failure would report "no duplicate" and let the
    caller create one, defeating ``on_duplicate="skip"``.
    """
    params: dict[str, Any] = {
        "project_id": project,
        "subject": subject,
        "status_id": "*",
        "limit": 5,
    }
    try:
        payload = await client.get("/issues.json", params=params)
    except RedmineAPIError as e:
        if e.status_code in (404, 422):
            return None
        raise
    issues_found = (payload or {}).get("issues") if isinstance(payload, dict) else None
    for i in issues_found or []:
        if i.get("subject") == subject:
            return _try_int(i.get("id"))
    return None


def _try_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def bulk_create_issues(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    issues: list[dict[str, Any]],
    on_duplicate: str = "skip",
    pacing_seconds: float = DEFAULT_BULK_CREATE_PACING_S,
    stop_on_error: bool = False,
    concurrency: int = 1,
    difficulty_field_id: int | None = None,
) -> dict[str, Any]:
    """Bulk-create issues from per-spec dicts with subject idempotency.

    Each ``issues[i]`` is a spec dict accepting the same fields as
    ``issues.create_issue`` (``project``, ``tracker``, ``subject``,
    ``description``, ``priority``, ``status``, ``assigned_to_id``,
    ``difficulty``, ``due_date``, ``start_date``, ``done_ratio``,
    ``custom_fields``). ``project``, ``tracker``, ``subject`` are
    required per spec.

    Args:
        issues: list of issue spec dicts. Empty list → no-op success.
        on_duplicate: ``"skip"`` (default) — pre-check by exact subject
            within the project; existing match reported as
            ``status="skipped"`` with ``duplicate_of=<id>``.
            ``"fail"`` — duplicate reported as
            ``status="failed"`` with ``error="duplicate_subject"``.
            ``"create_anyway"`` — skip the pre-check entirely (caller
            has already deduped or wants intentional duplicates).
        pacing_seconds: sleep between POSTs to avoid tripping Redmine's
            per-issue rate cap. Default 50ms. Only used when
            ``concurrency == 1``.
        stop_on_error: True to bail at first failure; remainder lands in
            ``skipped_for_stop_on_error`` (sequential) or ``skipped``
            (concurrent).
        concurrency: parallel in-flight creates (default 1 = sequential,
            which keeps the pacing behaviour). Values > 1 run a bounded
            fan-out and cannot also pace — leave ``pacing_seconds`` at its
            default (or 0) or the call is rejected.

    Returns ``{"results": [...], "summary": {...}}``. Each result has:
        - ``subject``: the requested subject (always present)
        - ``status``: ``"created"`` / ``"skipped"`` / ``"failed"``
        - ``id``: int (when created or duplicate-of)
        - ``duplicate_of``: int (when skipped due to existing subject)
        - ``error`` / ``hint``: when failed

    With ``concurrency > 1``, a 5xx from any item aborts the batch: results
    already produced are kept and the rest are reported as ``skipped`` with
    ``reason="aborted"``.
    """
    if on_duplicate not in {"skip", "fail", "create_anyway"}:
        return _validation_error(
            f"on_duplicate must be 'skip', 'fail', or 'create_anyway'; got {on_duplicate!r}.",
            field="on_duplicate",
        )
    if not isinstance(issues, list):
        return _validation_error("issues must be a list.", field="issues")
    if len(issues) > MAX_BATCH_SIZE:
        return {
            "error": "batch_too_large",
            "hint": (
                f"Batch of {len(issues)} exceeds MAX_BATCH_SIZE "
                f"({MAX_BATCH_SIZE}). Split into smaller batches."
            ),
            "size": len(issues),
            "max_batch_size": MAX_BATCH_SIZE,
        }
    if pacing_seconds < 0:
        return _validation_error("pacing_seconds must be non-negative.", field="pacing_seconds")
    if (err := _check_concurrency(concurrency, pacing_seconds)) is not None:
        return err

    # Pre-validate every spec has the required keys before any I/O — fail
    # fast rather than POST half the batch then bail.
    for idx, spec in enumerate(issues):
        if not isinstance(spec, dict):
            return _validation_error(
                f"issues[{idx}] must be a dict; got {type(spec).__name__}.",
                field=f"issues[{idx}]",
            )
        for required in ("project", "tracker", "subject"):
            if not spec.get(required):
                return _validation_error(
                    f"issues[{idx}] missing required field {required!r}.",
                    field=f"issues[{idx}].{required}",
                )

    async def process(spec: dict[str, Any]) -> dict[str, Any]:
        """Create (or skip) one spec, returning its per-item result dict."""
        subject = spec["subject"]
        project = spec["project"]

        # Idempotency pre-check.
        if on_duplicate in {"skip", "fail"}:
            try:
                existing_id = await _find_existing_by_subject(client, project, subject)
            except RedmineAPIError as e:
                structured = e.as_structured()
                return {
                    "subject": subject,
                    "status": "failed",
                    "error": structured.get("error", "redmine_api_error"),
                    "hint": (
                        "Duplicate pre-check failed; refusing to create "
                        "rather than risk a duplicate."
                    ),
                    "status_code": structured.get("status_code"),
                }
            if existing_id is not None:
                if on_duplicate == "skip":
                    return {
                        "subject": subject,
                        "status": "skipped",
                        "duplicate_of": existing_id,
                    }
                # on_duplicate == "fail"
                return {
                    "subject": subject,
                    "status": "failed",
                    "error": "duplicate_subject",
                    "hint": (
                        f"Issue with subject {subject!r} already exists in "
                        f"project {project!r} as #{existing_id}."
                    ),
                    "duplicate_of": existing_id,
                }

        # Build create_issue kwargs from the spec (only forward keys present).
        create_kwargs: dict[str, Any] = {
            "project": project,
            "tracker": spec["tracker"],
            "subject": subject,
        }
        for k in (
            "description",
            "priority",
            "status",
            "assigned_to_id",
            "difficulty",
            "due_date",
            "start_date",
            "done_ratio",
            "custom_fields",
        ):
            if k in spec and spec[k] is not None:
                create_kwargs[k] = spec[k]

        result = await issues_module.create_issue(
            client, cache, **create_kwargs, difficulty_field_id=difficulty_field_id
        )
        if isinstance(result, dict) and "error" in result:
            return {
                "subject": subject,
                "status": "failed",
                "error": result.get("error"),
                "hint": result.get("hint"),
                "status_code": result.get("status_code"),
            }

        new_issue = (result or {}).get("issue") or {}
        return {
            "subject": subject,
            "status": "created",
            "id": _try_int(new_issue.get("id")),
        }

    summary = {"created": 0, "skipped": 0, "failed": 0}

    if concurrency > 1:
        parallel = await _run_concurrent(
            len(issues),
            lambda i: process(issues[i]),
            concurrency=concurrency,
            should_abort=lambda r: (
                _is_server_error(r) or (stop_on_error and r["status"] == "failed")
            ),
        )
        results: list[dict[str, Any]] = []
        for spec, result in zip(issues, parallel, strict=False):
            if result is None:
                results.append(
                    {"subject": spec["subject"], "status": "skipped", "reason": "aborted"}
                )
                summary["skipped"] += 1
            else:
                results.append(result)
                summary[result["status"]] += 1
        out: dict[str, Any] = {
            "results": results,
            "summary": {"total": len(issues), **summary},
            "concurrency": concurrency,
        }
        return out

    results = []
    skipped_for_stop_on_error: list[dict[str, Any]] = []

    for idx, spec in enumerate(issues):
        if idx > 0 and pacing_seconds > 0:
            await asyncio.sleep(pacing_seconds)

        result = await process(spec)
        results.append(result)
        summary[result["status"]] += 1
        if result["status"] == "failed" and stop_on_error:
            skipped_for_stop_on_error = [{"subject": s["subject"]} for s in issues[idx + 1 :]]
            break

    summary_with_total = {"total": len(issues), **summary}
    out = {"results": results, "summary": summary_with_total}
    if skipped_for_stop_on_error:
        out["skipped_for_stop_on_error"] = skipped_for_stop_on_error
    return out


async def bulk_close(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    issue_ids: list[int],
    note: str | None = None,
    stop_on_error: bool = False,
    held_field_id: int | None = None,
    held_until_field_id: int | None = None,
) -> dict[str, Any]:
    """Close every issue in ``issue_ids``, optionally with a shared note."""
    if (err := _check_batch_size(issue_ids)) is not None:
        return err

    succeeded: list[int] = []
    failed: list[dict[str, Any]] = []
    skipped: list[int] = []

    for idx, issue_id in enumerate(issue_ids):
        result = await issues_module.close_issue(
            client,
            cache,
            issue_id,
            note=note,
            held_field_id=held_field_id,
            held_until_field_id=held_until_field_id,
        )
        if isinstance(result, dict) and "error" in result:
            failed.append({"issue_id": issue_id, **result})
            if stop_on_error:
                skipped = list(issue_ids[idx + 1 :])
                break
        else:
            succeeded.append(issue_id)

    return {
        "total": len(issue_ids),
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }
