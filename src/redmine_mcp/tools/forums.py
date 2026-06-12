"""Forum/board message tools (Redmine tickets #2390, #4493).

Tools (5):
  - list_boards    — GET    /projects/{id}/boards.json
  - list_messages  — GET    /boards/{board_id}/messages.json
  - create_message — POST   /boards/{board_id}/topics.json
  - reply_message  — POST   /boards/{board_id}/topics/{id}.json (via message update)
  - delete_message — DELETE /messages/{id}.json
"""

from __future__ import annotations

from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_messages(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    *,
    board_id: int,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List forum messages on a board.

    Args:
        board_id: numeric Redmine board id.
        limit: page size (capped server-side at 100).
        offset: skip the first N results.
    """
    path = f"/boards/{board_id}/messages.json"
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    try:
        resp = await client.get(path, params=params)
    except RedmineAPIError as e:
        return e.as_structured()

    if not isinstance(resp, dict):
        return {
            "messages": [],
            "total_count": 0,
            "limit": limit,
            "offset": offset,
            "board_id": board_id,
            "source": "api",
        }

    return {
        "messages": resp.get("messages", []),
        "total_count": resp.get("total_count", 0),
        "limit": resp.get("limit", limit),
        "offset": resp.get("offset", offset),
        "board_id": board_id,
        "source": "api",
    }


async def list_boards(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
) -> dict[str, Any]:
    """List forum boards for a project."""
    try:
        resp = await client.get(f"/projects/{project_id}/boards.json")
    except RedmineAPIError as e:
        return e.as_structured()

    boards = resp.get("boards", []) if isinstance(resp, dict) else []
    return {
        "boards": boards,
        "count": len(boards),
        "project_id": project_id,
        "source": "api",
    }


async def create_message(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    board_id: int,
    subject: str,
    content: str = "",
) -> dict[str, Any]:
    """Create a new forum topic on a board.

    Args:
        board_id: numeric board id.
        subject: required topic subject.
        content: optional message body.
    """
    if not subject or not subject.strip():
        return {
            "error": "validation_failed",
            "hint": "Field 'subject' is required for create_message.",
        }

    body: dict[str, Any] = {"subject": subject}
    if content:
        body["content"] = content

    try:
        resp = await client.post(
            f"/boards/{board_id}/topics.json",
            json={"message": body},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    message = resp.get("message") if isinstance(resp, dict) else None
    return {"message": message, "board_id": board_id, "source": "api"}


async def reply_message(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    board_id: int,
    topic_id: int,
    content: str,
) -> dict[str, Any]:
    """Reply to a forum topic.

    Args:
        board_id: board the topic belongs to.
        topic_id: the parent message/topic id to reply to.
        content: required reply body.
    """
    if not content or not content.strip():
        return {
            "error": "validation_failed",
            "hint": "Field 'content' is required for reply_message.",
        }

    try:
        resp = await client.post(
            f"/boards/{board_id}/topics/{topic_id}.json",
            json={"message": {"content": content}},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    message = resp.get("message") if isinstance(resp, dict) else None
    return {"message": message, "topic_id": topic_id, "source": "api"}


async def delete_message(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    message_id: int,
) -> dict[str, Any]:
    """Delete a forum message. Permanent."""
    try:
        await client.delete(f"/messages/{message_id}.json")
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "message_not_found",
                "hint": f"Message {message_id} not found.",
                "message_id": message_id,
            }
        return e.as_structured()
    return {"message_id": message_id, "deleted": True, "source": "api"}
