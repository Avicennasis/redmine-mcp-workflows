"""Cross-entity project activity feed (Redmine ticket #44337).

Redmine's own ``/projects/:id/activity`` endpoint is **not usable via the
REST API**: ``activity.json`` returns 403 (there is no JSON representation),
and ``activity.atom`` requires an authenticated web session — it ignores
both ``X-Redmine-API-Key`` and ``?key=`` and 302s to ``/login``. Verified
against the target instance on 2026-09-15.

So this tool synthesizes the feed from the per-resource REST endpoints that
the API key *can* read, normalizing each into the same event shape and
merging them by timestamp:

  * ``issues``       — ``/issues.json`` (project + ``updated_on`` range)
  * ``news``         — ``/projects/:id/news.json``
  * ``wiki``         — ``/projects/:id/wiki/index.json``
  * ``forums``       — ``/projects/:id/boards.json`` → ``/boards/:id/messages.json``
  * ``time_entries`` — ``/time_entries.json`` (project + ``from``/``to``)
  * ``files``        — ``/projects/:id/files.json``

Each source is best-effort: a source that errors (e.g. forums return 403
when the boards module is disabled or the key lacks permission) is skipped
and its status recorded in the ``sources`` map, so one unavailable source
never empties the whole feed. Date filtering is applied client-side (from/to
inclusive, ``YYYY-MM-DD``) so every source honours it uniformly.
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError
from ..schema import project as project_schema

# Source keys accepted by ``activity_types``, in a stable order.
ACTIVITY_TYPES: tuple[str, ...] = (
    "issues",
    "news",
    "wiki",
    "forums",
    "time_entries",
    "files",
)

DEFAULT_MAX_PER_SOURCE = 100
MAX_LIMIT = 500


def _try_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _author_name(record: dict[str, Any], key: str = "author") -> str | None:
    author = record.get(key)
    if isinstance(author, dict):
        name = author.get("name")
        return str(name) if name else None
    return None


def _in_range(timestamp: Any, from_date: str | None, to_date: str | None) -> bool:
    """True if ``timestamp`` falls inside the inclusive ``YYYY-MM-DD`` range.

    Redmine timestamps are ISO-8601 (``2026-09-03T19:44:07Z``); comparing the
    date prefix lexicographically is exact for that format.
    """
    if from_date is None and to_date is None:
        return True
    if not isinstance(timestamp, str) or len(timestamp) < 10:
        return False
    day = timestamp[:10]
    if from_date is not None and day < from_date:
        return False
    return not (to_date is not None and day > to_date)


def _event(
    *,
    type: str,  # noqa: A002 — matches the public event schema key
    timestamp: Any,
    title: str,
    author: str | None = None,
    description: str | None = None,
    url: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": type,
        "timestamp": timestamp if isinstance(timestamp, str) else None,
        "title": title,
    }
    if author:
        event["author"] = author
    if description:
        event["description"] = description
    if url:
        event["url"] = url
    event.update(extra)
    return event


def _updated_on_range_param(from_date: str | None, to_date: str | None) -> str | None:
    """Build Redmine's ``updated_on`` filter operand for a date range."""
    if from_date and to_date:
        return f"><{from_date}|{to_date}"
    if from_date:
        return f">={from_date}"
    if to_date:
        return f"<={to_date}"
    return None


async def _collect_issues(
    client: RedmineClient,
    project_id: int,
    *,
    from_date: str | None,
    to_date: str | None,
    max_per_source: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "project_id": project_id,
        "status_id": "*",
        "sort": "updated_on:desc",
        "limit": max_per_source,
        "include": "journals",
    }
    if (operand := _updated_on_range_param(from_date, to_date)) is not None:
        params["updated_on"] = operand

    payload = await client.get("/issues.json", params=params)
    issues = payload.get("issues", []) if isinstance(payload, dict) else []

    events: list[dict[str, Any]] = []
    for issue in issues:
        timestamp = issue.get("updated_on") or issue.get("created_on")
        if not _in_range(timestamp, from_date, to_date):
            continue
        issue_id = _try_int(issue.get("id"))
        tracker = (issue.get("tracker") or {}).get("name")
        status = (issue.get("status") or {}).get("name")
        assigned = (issue.get("assigned_to") or {}).get("name")
        description_parts = [p for p in (status, tracker) if p]
        note = _latest_note(issue)
        if note:
            description_parts.append(note)
        events.append(
            _event(
                type="issue",
                timestamp=timestamp,
                title=str(issue.get("subject") or f"#{issue_id}"),
                author=_author_name(issue),
                description=" | ".join(description_parts) or None,
                url=f"/issues/{issue_id}" if issue_id is not None else None,
                id=issue_id,
                status=status,
                tracker=tracker,
                assigned_to=assigned,
            )
        )
    return events


def _latest_note(issue: dict[str, Any]) -> str | None:
    """Return the most recent journal note on an issue, if any."""
    journals = issue.get("journals")
    if not isinstance(journals, list):
        return None
    for journal in reversed(journals):
        if isinstance(journal, dict) and journal.get("notes"):
            text = str(journal["notes"]).strip().replace("\n", " ")
            return text[:200]
    return None


async def _collect_news(
    client: RedmineClient,
    project_id: int,
    *,
    from_date: str | None,
    to_date: str | None,
    max_per_source: int,
) -> list[dict[str, Any]]:
    payload = await client.get(
        f"/projects/{project_id}/news.json", params={"limit": max_per_source}
    )
    news = payload.get("news", []) if isinstance(payload, dict) else []

    events: list[dict[str, Any]] = []
    for item in news:
        timestamp = item.get("created_on")
        if not _in_range(timestamp, from_date, to_date):
            continue
        news_id = _try_int(item.get("id"))
        events.append(
            _event(
                type="news",
                timestamp=timestamp,
                title=str(item.get("title") or f"news #{news_id}"),
                author=_author_name(item),
                description=(item.get("summary") or item.get("description") or None),
                url=f"/news/{news_id}" if news_id is not None else None,
                id=news_id,
            )
        )
    return events


async def _collect_wiki(
    client: RedmineClient,
    project_id: int,
    *,
    from_date: str | None,
    to_date: str | None,
    max_per_source: int,
) -> list[dict[str, Any]]:
    payload = await client.get(f"/projects/{project_id}/wiki/index.json")
    pages = payload.get("wiki_pages", []) if isinstance(payload, dict) else []

    events: list[dict[str, Any]] = []
    for page in pages:
        timestamp = page.get("updated_on") or page.get("created_on")
        if not _in_range(timestamp, from_date, to_date):
            continue
        title = str(page.get("title") or "(untitled)")
        events.append(
            _event(
                type="wiki",
                timestamp=timestamp,
                title=title,
                author=_author_name(page),
                url=f"/projects/{project_id}/wiki/{title}",
                version=page.get("version"),
            )
        )
    if len(events) > max_per_source:
        del events[max_per_source:]
    return events


async def _collect_forums(
    client: RedmineClient,
    project_id: int,
    *,
    from_date: str | None,
    to_date: str | None,
    max_per_source: int,
) -> list[dict[str, Any]]:
    boards_payload = await client.get(f"/projects/{project_id}/boards.json")
    boards = boards_payload.get("boards", []) if isinstance(boards_payload, dict) else []

    events: list[dict[str, Any]] = []
    for board in boards:
        board_id = _try_int(board.get("id"))
        if board_id is None:
            continue
        messages_payload = await client.get(
            f"/boards/{board_id}/messages.json", params={"limit": max_per_source}
        )
        messages = (
            messages_payload.get("messages", []) if isinstance(messages_payload, dict) else []
        )
        for message in messages:
            timestamp = message.get("created_on")
            if not _in_range(timestamp, from_date, to_date):
                continue
            message_id = _try_int(message.get("id"))
            events.append(
                _event(
                    type="forum",
                    timestamp=timestamp,
                    title=str(message.get("subject") or f"message #{message_id}"),
                    author=_author_name(message),
                    description=(message.get("content") or None),
                    url=f"/boards/{board_id}/topics/{message_id}"
                    if message_id is not None
                    else None,
                    id=message_id,
                    board_id=board_id,
                    board=board.get("name"),
                )
            )
    return events


async def _collect_time_entries(
    client: RedmineClient,
    project_id: int,
    *,
    from_date: str | None,
    to_date: str | None,
    max_per_source: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"project_id": project_id, "limit": max_per_source}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date

    payload = await client.get("/time_entries.json", params=params)
    entries = payload.get("time_entries", []) if isinstance(payload, dict) else []

    events: list[dict[str, Any]] = []
    for entry in entries:
        timestamp = entry.get("spent_on") or entry.get("created_on")
        if not _in_range(timestamp, from_date, to_date):
            continue
        entry_id = _try_int(entry.get("id"))
        issue_id = _try_int((entry.get("issue") or {}).get("id"))
        title = f"{entry.get('hours')}h"
        if issue_id is not None:
            title += f" on #{issue_id}"
        events.append(
            _event(
                type="time_entry",
                timestamp=timestamp,
                title=title,
                author=_author_name(entry, "user"),
                description=(entry.get("comments") or None),
                url=f"/time_entries/{entry_id}" if entry_id is not None else None,
                id=entry_id,
                hours=entry.get("hours"),
                activity=(entry.get("activity") or {}).get("name"),
                issue_id=issue_id,
            )
        )
    return events


async def _collect_files(
    client: RedmineClient,
    project_id: int,
    *,
    from_date: str | None,
    to_date: str | None,
    max_per_source: int,
) -> list[dict[str, Any]]:
    payload = await client.get(f"/projects/{project_id}/files.json")
    files = payload.get("files", []) if isinstance(payload, dict) else []

    events: list[dict[str, Any]] = []
    for item in files:
        timestamp = item.get("created_on")
        if not _in_range(timestamp, from_date, to_date):
            continue
        file_id = _try_int(item.get("id"))
        events.append(
            _event(
                type="file",
                timestamp=timestamp,
                title=str(item.get("filename") or f"file #{file_id}"),
                author=_author_name(item),
                description=(item.get("description") or None),
                url=item.get("content_url"),
                id=file_id,
                filesize=item.get("filesize"),
                version=(item.get("version") or {}).get("name"),
            )
        )
    return events


_COLLECTORS = {
    "issues": _collect_issues,
    "news": _collect_news,
    "wiki": _collect_wiki,
    "forums": _collect_forums,
    "time_entries": _collect_time_entries,
    "files": _collect_files,
}


async def activity_feed(
    client: RedmineClient,
    cache: SchemaCache,
    *,
    project: int | str,
    from_date: str | None = None,
    to_date: str | None = None,
    activity_types: list[str] | None = None,
    limit: int = 100,
    max_per_source: int = DEFAULT_MAX_PER_SOURCE,
) -> dict[str, Any]:
    """Return a merged, timestamp-sorted activity feed for a project.

    Args:
        project: numeric id or identifier slug.
        from_date / to_date: optional ``YYYY-MM-DD`` bounds (both inclusive).
        activity_types: subset of :data:`ACTIVITY_TYPES`; default all.
        limit: cap on merged events returned (1..:data:`MAX_LIMIT`).
        max_per_source: cap on rows fetched from each source before merging.

    Returns ``{project, project_id, from, to, activity_types, count,
    truncated, events, sources}``. ``sources`` records the per-source row
    count, or ``{"error": ...}`` when a source was unavailable — one failed
    source never fails the whole feed.
    """
    if limit < 1 or limit > MAX_LIMIT:
        return {
            "error": "invalid_limit",
            "hint": f"limit must be between 1 and {MAX_LIMIT}.",
            "limit": limit,
        }
    if max_per_source < 1:
        return {
            "error": "invalid_max_per_source",
            "hint": "max_per_source must be at least 1.",
            "max_per_source": max_per_source,
        }

    requested = list(activity_types) if activity_types else list(ACTIVITY_TYPES)
    unknown = [t for t in requested if t not in _COLLECTORS]
    if unknown:
        return {
            "error": "invalid_activity_type",
            "hint": f"Unknown activity type(s): {unknown}. Valid: {list(ACTIVITY_TYPES)}.",
            "activity_types": requested,
        }

    project_id = await project_schema.resolve_project_id(client, cache, project)
    if project_id is None:
        return {
            "error": "project_not_found",
            "hint": f"No project matches {project!r}.",
            "project": project,
        }

    events: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    for type_name in requested:  # noqa: A001 — public name
        collector = _COLLECTORS[type_name]
        try:
            collected = await collector(
                client,
                project_id,
                from_date=from_date,
                to_date=to_date,
                max_per_source=max_per_source,
            )
        except RedmineAPIError as e:
            sources[type_name] = {"error": e.as_structured().get("error", "redmine_api_error")}
            continue
        sources[type_name] = {"count": len(collected)}
        events.extend(collected)

    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    truncated = len(events) > limit
    if truncated:
        del events[limit:]

    return {
        "project": project,
        "project_id": project_id,
        "from": from_date,
        "to": to_date,
        "activity_types": requested,
        "count": len(events),
        "truncated": truncated,
        "events": events,
        "sources": sources,
        "source": "api",
    }
