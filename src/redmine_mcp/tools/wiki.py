"""Wiki page CRUD tools (Redmine ticket #2378).

Tools (4):
  - get_page    — GET    /projects/{p}/wiki/{title}.json (optional historical version)
  - create_page — PUT    /projects/{p}/wiki/{title}.json after a 404-pre-flight GET
  - update_page — PUT    /projects/{p}/wiki/{title}.json (optional version for opt. lock)
  - delete_page — DELETE /projects/{p}/wiki/{title}.json

Implementation notes:
  * Redmine uses PUT for both create-and-update on wiki pages (it returns
    201 vs 200 to distinguish). We split create vs update at the tool
    boundary by adding a GET pre-flight to ``create_page`` that refuses to
    overwrite an existing page — callers should reach for ``update_page``
    in that case.
  * ``version`` on ``update_page`` enables optimistic concurrency: if the
    server's current version doesn't match, Redmine rejects the write
    (typically 409). We don't try to interpret that — just surface the
    underlying API error.
  * Titles are URL-encoded with ``urllib.parse.quote(safe="")`` so spaces,
    slashes, and unicode all survive the path. The project segment is
    passed through verbatim — Redmine accepts both numeric ids and slugs
    in the wiki URL routing layer.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


def _path(project: int | str, title: str, *, version: int | None = None) -> str:
    """Compose ``/projects/{p}/wiki/{title}.json`` (or the versioned variant)."""
    encoded_title = quote(title, safe="")
    base = f"/projects/{project}/wiki/{encoded_title}"
    if version is not None:
        return f"{base}/{version}.json"
    return f"{base}.json"


async def get_page(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    project: int | str,
    title: str,
    *,
    version: int | None = None,
) -> dict[str, Any]:
    """Fetch a wiki page, optionally a historical version.

    Returns ``{"page": {...}, "source": "api"}`` or a structured error
    payload (``wiki_page_not_found`` for 404 / null-page, the underlying
    ``redmine_api_<code>`` for other API failures).
    """
    try:
        payload = await client.get(_path(project, title, version=version))
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "wiki_page_not_found",
                "hint": f"Wiki page {title!r} not found in project {project!r}.",
                "project": project,
                "title": title,
            }
        return e.as_structured()

    page = payload.get("wiki_page") if isinstance(payload, dict) else None
    if not page:
        return {
            "error": "wiki_page_not_found",
            "hint": f"Wiki page {title!r} not found in project {project!r}.",
            "project": project,
            "title": title,
        }
    return {"page": page, "source": "api"}


def _validate_text(text: str) -> dict[str, Any] | None:
    """Reject empty / whitespace-only bodies; matches add_comment's check."""
    if not text or not text.strip():
        return {
            "error": "validation_failed",
            "errors": [
                {
                    "error": "required_field_missing",
                    "hint": "Field 'text' is required for wiki page CRUD.",
                    "field": "text",
                    "op": "wiki_write",
                }
            ],
        }
    return None


async def create_page(
    client: RedmineClient,
    cache: SchemaCache,
    project: int | str,
    title: str,
    text: str,
    *,
    parent_title: str | None = None,
    comments: str | None = None,
) -> dict[str, Any]:
    """Create a new wiki page; refuse to overwrite an existing one.

    Pre-flight: GET the page; a 404 (or null payload) means we're clear to
    create, anything else means it exists (or the GET failed for a non-404
    reason, which we propagate).
    """
    if (err := _validate_text(text)) is not None:
        return err

    pre = await get_page(client, cache, project, title)
    if "error" not in pre:
        existing = pre.get("page", {})
        return {
            "error": "wiki_page_already_exists",
            "hint": (
                f"Wiki page {title!r} already exists in project {project!r}; "
                "use update_page to modify it."
            ),
            "project": project,
            "title": title,
            "existing_version": existing.get("version"),
        }
    if pre["error"] != "wiki_page_not_found":
        # Surface 403/500/etc. — we have no business creating against an
        # endpoint we couldn't even read.
        return pre

    body: dict[str, Any] = {"text": text}
    if parent_title is not None:
        body["parent_title"] = parent_title
    if comments is not None:
        body["comments"] = comments

    try:
        resp = await client.put(_path(project, title), json={"wiki_page": body})
    except RedmineAPIError as e:
        return e.as_structured()

    page = resp.get("wiki_page") if isinstance(resp, dict) else None
    if page is None:
        # Redmine sometimes returns 204 / empty body on PUT — re-fetch.
        return await get_page(client, cache, project, title)
    return {"page": page, "source": "api"}


async def update_page(
    client: RedmineClient,
    cache: SchemaCache,
    project: int | str,
    title: str,
    text: str,
    *,
    version: int | None = None,
    parent_title: str | None = None,
    comments: str | None = None,
) -> dict[str, Any]:
    """Update an existing wiki page.

    Args:
        version: when supplied, included in the PUT body for Redmine's
            optimistic-concurrency check. A mismatch is surfaced as the
            underlying ``redmine_api_409`` (or the equivalent code your
            Redmine returns).
    """
    if (err := _validate_text(text)) is not None:
        return err

    body: dict[str, Any] = {"text": text}
    if version is not None:
        body["version"] = version
    if parent_title is not None:
        body["parent_title"] = parent_title
    if comments is not None:
        body["comments"] = comments

    try:
        await client.put(_path(project, title), json={"wiki_page": body})
    except RedmineAPIError as e:
        return e.as_structured()

    # PUT typically returns 204; re-fetch to give the caller the new
    # version + author + timestamps.
    return await get_page(client, cache, project, title)


async def delete_page(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001 — kept for signature parity
    project: int | str,
    title: str,
) -> dict[str, Any]:
    """Permanently delete a wiki page (and all its historical versions)."""
    try:
        await client.delete(_path(project, title))
    except RedmineAPIError as e:
        if e.status_code == 404:
            return {
                "error": "wiki_page_not_found",
                "hint": f"Wiki page {title!r} not found in project {project!r}.",
                "project": project,
                "title": title,
            }
        return e.as_structured()
    return {
        "project": project,
        "title": title,
        "deleted": True,
        "source": "api",
    }
