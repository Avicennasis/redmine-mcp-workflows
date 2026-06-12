"""FastMCP entrypoint.

Phase 1 shipped ``redmine_list_trackers``. Phase 2 added discovery
(``redmine_describe_tracker``, ``redmine_describe_project``,
``redmine_list_projects``, ``redmine_invalidate_cache``). Phase 4 added
the issue-lifecycle surface (5 tools). Phase 5 rounds out v0.1 with
comments and attachments:

  - ``redmine_add_comment``      (post a journal entry / comment)
  - ``redmine_get_journals``     (read structured journal entries)
  - ``redmine_update_journal``   (edit a journal's notes in place, Redmine 5.0+)
  - ``redmine_upload_attachment`` (path-restricted file upload)

Tools convert :class:`RedmineAPIError` into structured JSON returns rather
than letting them propagate as exceptions. Write tools short-circuit with
:class:`ReadOnlyModeError` when ``REDMINE_MCP_READ_ONLY=true``.

Note: this module deliberately does NOT use ``from __future__ import
annotations``. FastMCP's tool decorator introspects parameter annotations
via ``inspect.signature(...).parameters`` and runs ``issubclass`` on the
``annotation`` attribute; with PEP 563 enabled, every annotation becomes a
string and the ``issubclass`` check raises ``TypeError``. Annotations on
local variables still use PEP 604 union syntax (Python 3.10+ native).
"""

import atexit
import contextlib
import json
import logging
import sys

from mcp.server.fastmcp import FastMCP

from .cache.schema_db import SchemaCache
from .client import RedmineClient
from .config import Config
from .errors import ReadOnlyModeError, RedmineAPIError
from .tools import (
    attachments,
    bulk,
    comments,
    custom_fields,
    discovery,
    enumerations,
    files,
    forums,
    groups,
    issue_categories,
    issue_statuses,
    issues,
    memberships,
    news,
    passthrough,
    projects,
    queries,
    relations,
    roles,
    search,
    time_entries,
    users,
    versions,
    watchers,
    wiki,
)

log = logging.getLogger("redmine_mcp")

mcp = FastMCP(
    "redmine",
    instructions=(
        "Schema-aware MCP server for Redmine. Validates workflow transitions, "
        "custom fields, and required fields against a per-tracker cache before "
        "round-tripping the API.\n\n"
        "Text formatting: this Redmine instance uses Markdown. Notes and "
        "descriptions support **bold**, ## headings, | tables |, and - lists, "
        "but only when separated by real blank lines. When composing multi-line "
        "note text, use actual newline characters in the JSON string value — "
        "do NOT use literal backslash-n (\\\\n) escape sequences, which get "
        "stored as visible \\\\n text instead of line breaks."
    ),
)

# Module-level state. Lazy-initialized on first tool call so import is cheap
# and config errors surface inside a tool response (not as an import failure).
_config: Config | None = None
_cache: SchemaCache | None = None


def _get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
        logging.basicConfig(
            level=getattr(logging, _config.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            stream=sys.stderr,
        )
    return _config


def _get_cache() -> SchemaCache:
    global _cache
    if _cache is None:
        cfg = _get_config()
        api_key = cfg.require_api_key()
        _cache = SchemaCache(
            db_path=cfg.cache_dir / "schema.db",
            ttl_seconds=cfg.cache_ttl_seconds,
        )
        _cache.reconcile_auth(api_key)
        atexit.register(_cache.close)
    return _cache


def _dump(value: object) -> str:
    return json.dumps(value, indent=2, default=str)


def _normalize_custom_fields(
    value: list | str,
) -> tuple[list | None, dict | None]:
    """Accept ``custom_fields`` as either a Python list or a JSON string.

    Returns ``(normalized, error)``:
      * ``(None, None)`` — no change (empty string or empty list).
      * ``(list, None)`` — value to forward to the internal layer.
      * ``(None, dict)`` — structured error the caller must dump and return.

    Newer MCP transports deliver lists natively; the string branch keeps
    older transports working without forcing the caller to JSON-encode.
    """
    if isinstance(value, list):
        return (value or None, None)
    if not value:
        return (None, None)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        return (None, {
            "error": "custom_fields_invalid_json",
            "hint": f"custom_fields must be a list or a JSON-encoded list: {e}",
        })
    if not isinstance(parsed, list):
        return (None, {
            "error": "custom_fields_invalid_shape",
            "hint": "custom_fields must decode to a JSON array, not "
                    f"{type(parsed).__name__}.",
        })
    return (parsed or None, None)


async def _wrap(coro_factory, *, write: bool = False):
    """Helper to wrap a tool coroutine factory and convert API errors to JSON.

    Args:
        coro_factory: ``async (client, cache) -> dict`` callable.
        write: when True, gate the call on ``Config.read_only``.
    """
    cfg = _get_config()
    if write and cfg.read_only:
        return _dump(ReadOnlyModeError().as_dict())
    cache = _get_cache()
    try:
        async with RedmineClient(cfg) as client:
            result = await coro_factory(client, cache)
        return _dump(result)
    except RedmineAPIError as e:
        return _dump(e.as_structured())
    except Exception as e:  # pragma: no cover - last-resort guard
        log.exception("unexpected error in tool")
        return _dump({"error": "internal_error", "hint": str(e)})


@mcp.tool()
async def redmine_list_trackers() -> str:
    """List all trackers configured on the Redmine server.

    Returns a JSON document describing each tracker (id, name, default
    status, description). Cached entries are populated as a side effect for
    use by future schema-validation tools.
    """
    return await _wrap(discovery.list_trackers)


@mcp.tool()
async def redmine_describe_tracker(
    tracker: str,
    include_observations: bool = True,
) -> str:
    """Return an enriched schema for one tracker.

    Args:
        tracker: numeric id or name (e.g. ``"Bug"`` or ``"1"``).
        include_observations: if True (default), include the learned
            workflow graph (allowed/disallowed transitions per role).

    Includes available statuses, priorities, and — when
    ``include_observations`` is true — the learned workflow graph from
    prior API responses. Workflow knowledge is reactive: Redmine does not
    expose ``/workflows`` via REST, so the cache learns by observing the
    outcome of every status-change attempt.
    """
    # Tracker may arrive as a string-encoded int.
    ident: int | str = tracker
    with contextlib.suppress(TypeError, ValueError):
        ident = int(tracker)

    async def factory(client, cache):
        return await discovery.describe_tracker(
            client, cache, ident, include_observations=include_observations
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_describe_project(project: str) -> str:
    """Return a project description (trackers, modules, issue categories).

    Args:
        project: numeric id or identifier slug (e.g. ``"claudecode"`` or ``"15"``).

    Cache-backed. First call populates the cache.
    """
    ident: int | str = project
    with contextlib.suppress(TypeError, ValueError):
        ident = int(project)

    async def factory(client, cache):
        return await discovery.describe_project(client, cache, ident)
    return await _wrap(factory)


@mcp.tool()
async def redmine_list_projects(
    query: str = "",
    limit: int = 25,
    offset: int = 0,
) -> str:
    """List projects (paginated, optional substring filter).

    Args:
        query: optional case-insensitive substring filter on
            name/identifier/description (applied client-side after fetch).
        limit: page size, max 100. Defaults to 25.
        offset: skip the first N results. Defaults to 0.

    Returns a JSON document with ``projects``, ``total_count``, ``limit``,
    ``offset``, and ``filtered_locally`` (whether the query filter was applied).
    """
    q = query or None

    async def factory(client, cache):
        return await discovery.list_projects(client, query=q, limit=limit, offset=offset)
    return await _wrap(factory)


@mcp.tool()
async def redmine_create_project(
    name: str,
    identifier: str,
    description: str = "",
    homepage: str = "",
    is_public: bool = True,
    parent_id: int = 0,
    inherit_members: bool = False,
    tracker_ids: list | str = "",
    enabled_module_names: list | str = "",
) -> str:
    """Create a new project.

    Args:
        name: required project display name.
        identifier: required URL slug (lowercase, hyphens, no spaces).
        description: optional project description.
        homepage: optional homepage URL.
        is_public: whether the project is publicly visible (default True).
        parent_id: optional parent project id (``0`` for top-level).
        inherit_members: if True, inherit members from parent.
        tracker_ids: optional list of tracker ids to enable.
        enabled_module_names: optional list of modules (e.g.
            ``["issue_tracking", "wiki", "boards"]``).

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    desc = description if description else None
    hp = homepage if homepage else None
    pid = parent_id if parent_id else None
    tids = tracker_ids if isinstance(tracker_ids, list) and tracker_ids else None
    emn = enabled_module_names if isinstance(enabled_module_names, list) and enabled_module_names else None

    async def factory(client, cache):
        return await projects.create_project(
            client, cache,
            name=name, identifier=identifier,
            description=desc, homepage=hp, is_public=is_public,
            parent_id=pid, inherit_members=inherit_members,
            tracker_ids=tids, enabled_module_names=emn,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_update_project(
    project_id: str,
    name: str = "",
    description: str = "",
    homepage: str = "",
    is_public: bool = True,
    parent_id: int = 0,
    inherit_members: bool = False,
    tracker_ids: list | str = "",
    enabled_module_names: list | str = "",
) -> str:
    """Update a project. Partial — only supplied fields are sent.

    Args:
        project_id: numeric id or identifier slug.
        name: optional new display name.
        description: optional new description.
        homepage: optional new homepage URL.
        is_public: project visibility.
        parent_id: new parent project id (``0`` for unchanged).
        inherit_members: whether to inherit members from parent.
        tracker_ids: optional list of tracker ids to enable.
        enabled_module_names: optional list of modules.

    Returns the refreshed project. Honors ``REDMINE_MCP_READ_ONLY``.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)
    n = name if name else None
    desc = description if description else None
    hp = homepage if homepage else None
    ppid = parent_id if parent_id else None
    tids = tracker_ids if isinstance(tracker_ids, list) and tracker_ids else None
    emn = enabled_module_names if isinstance(enabled_module_names, list) and enabled_module_names else None

    async def factory(client, cache):
        return await projects.update_project(
            client, cache,
            project_id=pid, name=n, description=desc, homepage=hp,
            is_public=is_public, parent_id=ppid,
            inherit_members=inherit_members,
            tracker_ids=tids, enabled_module_names=emn,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_delete_project(project_id: str) -> str:
    """Permanently delete a project and all its data.

    Args:
        project_id: numeric id or identifier slug.

    **Destructive** — cannot be undone. Honors ``REDMINE_MCP_READ_ONLY``.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)

    async def factory(client, cache):
        return await projects.delete_project(client, cache, project_id=pid)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_archive_project(project_id: str) -> str:
    """Archive a project (Redmine 5.0+). Reversible via unarchive.

    Args:
        project_id: numeric id or identifier slug.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)

    async def factory(client, cache):
        return await projects.archive_project(client, cache, project_id=pid)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_unarchive_project(project_id: str) -> str:
    """Unarchive a previously archived project (Redmine 5.0+).

    Args:
        project_id: numeric id or identifier slug.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)

    async def factory(client, cache):
        return await projects.unarchive_project(client, cache, project_id=pid)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_invalidate_cache(scope: str = "all") -> str:
    """Drop cached entries for a scope. No HTTP request is made.

    Args:
        scope: one of:
            - ``"all"`` — drop all schema rows (preserves auth fingerprint).
            - ``"tracker:<id-or-name>"`` — drop one tracker + its workflow rows.
            - ``"project:<id-or-slug>"`` — drop one project.

    Use after Redmine config changes (new trackers, edited workflow,
    moved roles). The auth fingerprint is preserved so subsequent calls
    don't trigger a reconcile-wipe.
    """
    cache = _get_cache()
    try:
        return _dump(discovery.invalidate_cache(cache, scope))
    except ValueError as e:
        return _dump({"error": "invalid_scope", "hint": str(e)})


@mcp.tool()
async def redmine_get_issue(issue_id: int, include: str = "") -> str:
    """Fetch a single issue.

    Args:
        issue_id: numeric Redmine issue id.
        include: comma-separated includes (default
            ``"attachments,journals,relations,watchers"``). Pass an empty
            string to use the default.

    Read-only — no validation, no caching.
    """
    inc = include if include else None

    async def factory(client, cache):
        return await issues.get_issue(client, cache, issue_id, include=inc)
    return await _wrap(factory)


@mcp.tool()
async def redmine_create_issue(
    project: str,
    tracker: str,
    subject: str,
    description: str = "",
    priority: str = "",
    status: str = "",
    assigned_to_id: int = 0,
    difficulty: str = "",
    held: bool = False,
    held_until: str = "",
    due_date: str = "",
    start_date: str = "",
    done_ratio: int = -1,
    custom_fields: list | str = "",
) -> str:
    """Create an issue with cache-aware id resolution.

    Args:
        project: numeric id or identifier slug (e.g. ``"claudecode"``).
        tracker: numeric id or name (e.g. ``"Bug"``).
        subject: required, must be non-empty.
        description: optional issue body.
        priority: optional id or name (e.g. ``"High"``); defaults to
            tracker default when empty.
        status: optional id or name. Most fleets force initial status
            via workflow; this is rarely needed.
        assigned_to_id: optional Redmine user id; ``0`` means unassigned.
        difficulty: optional engagement-mode level for the global
            ``Difficulty`` custom field. Values: ``"Unclassified"`` /
            ``"Easy"`` / ``"Normal"`` / ``"Hard"``. When omitted, the
            field default-fills with ``"Unclassified"`` so auto-callers
            don't trip the required-field validation. Silently no-ops on
            fleets that don't have a Difficulty field configured.
        held: optional boolean. ``True`` marks the issue as held (blocks
            closing). ``False`` (default) leaves the Held field unset.
        held_until: optional ISO-8601 date (``"2026-10-01"``). Sets the
            ``Held Until`` custom field. Empty leaves it unset. Only
            meaningful when ``held`` is ``True``.
        due_date: optional ISO-8601 date (``"2026-05-17"``). Empty leaves
            it unset.
        start_date: optional ISO-8601 date. Empty leaves it unset (Redmine
            defaults to the creation date).
        done_ratio: optional 0-100 progress percent. ``-1`` (sentinel)
            means unset; ``0`` is explicit "no progress yet."
        custom_fields: optional list of ``{"id": <int>, "value": <str>}``
            entries for arbitrary custom fields. Accepts either a Python
            list (preferred — Claude can pass it natively) or a JSON-
            encoded string. Empty string means no custom-field changes
            beyond what ``difficulty`` sets.

    Returns the created issue or a structured validation error.
    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    desc = description if description else None
    pri: int | str | None = priority if priority else None
    st: int | str | None = status if status else None
    assignee = assigned_to_id if assigned_to_id else None
    diff = difficulty if difficulty else None
    h = held if held else None
    hu = held_until if held_until else None
    dd = due_date if due_date else None
    sd = start_date if start_date else None
    dr = done_ratio if done_ratio != -1 else None
    cf, cf_err = _normalize_custom_fields(custom_fields)
    if cf_err is not None:
        return _dump(cf_err)

    async def factory(client, cache):
        return await issues.create_issue(
            client, cache,
            project=project, tracker=tracker, subject=subject,
            description=desc, priority=pri, status=st,
            assigned_to_id=assignee, difficulty=diff,
            held=h, held_until=hu,
            due_date=dd, start_date=sd, done_ratio=dr,
            custom_fields=cf,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_update_issue(
    issue_id: int,
    subject: str = "",
    description: str = "",
    status: str = "",
    priority: str = "",
    assigned_to_id: int = 0,
    notes: str = "",
    difficulty: str = "",
    held: bool = False,
    held_until: str = "",
    due_date: str = "",
    start_date: str = "",
    done_ratio: int = -1,
    fixed_version_id: str = "",
    custom_fields: list | str = "",
) -> str:
    """Update an issue, with reactive workflow validation on status changes.

    Args:
        issue_id: numeric Redmine issue id.
        subject: optional new subject.
        description: optional new description.
        status: optional id or name (e.g. ``"In Progress"``). Triggers
            workflow pre-flight against the cache.
        priority: optional id or name.
        assigned_to_id: optional user id; ``0`` means unchanged.
        notes: optional comment to attach with this update (becomes a
            journal entry).
        difficulty: optional engagement-mode level for the global
            ``Difficulty`` custom field. Values: ``"Unclassified"`` /
            ``"Easy"`` / ``"Normal"`` / ``"Hard"``. NO default-fill on
            update — empty means "don't change Difficulty."
        held: optional boolean. ``True`` marks the issue as held (blocks
            closing). ``False`` (default) means unchanged. To clear a
            held flag, pass ``held=True`` is wrong — instead pass
            ``custom_fields`` with ``{"id": 2, "value": ""}`` directly,
            or set ``held=False`` won't fire (sentinel is falsy).
        held_until: optional ISO-8601 date (``"2026-10-01"``). Sets the
            ``Held Until`` custom field. Empty means unchanged.
        due_date: optional ISO-8601 date (``"2026-05-17"``). Empty leaves
            it unchanged; pass the literal string ``""`` (i.e. just don't
            send this arg) to leave it untouched. To clear an existing
            due_date you currently need ``redmine_request`` with
            ``{"issue": {"due_date": null}}`` — the MCP can't distinguish
            "unset" from "leave alone" without explicit null support.
        start_date: same shape as ``due_date``.
        done_ratio: optional 0-100 progress percent. ``-1`` (sentinel)
            means unchanged; ``0`` is explicit "reset to no progress."
        fixed_version_id: optional version id (string for id-or-empty).
            Empty leaves unchanged; explicit ``"0"`` clears the version.
        custom_fields: optional list of ``{"id": <int>, "value": <str>}``
            entries. Accepts either a Python list (preferred) or a JSON-
            encoded string. Empty means no custom-field changes.

    On a status change the cache pre-flight short-circuits any
    previously-observed disallowed transition with a
    :class:`WorkflowTransitionDisallowed` payload. After a real PUT the
    outcome is recorded so future calls benefit. Honors
    ``REDMINE_MCP_READ_ONLY``.
    """
    sub = subject if subject else None
    desc = description if description else None
    st: int | str | None = status if status else None
    pri: int | str | None = priority if priority else None
    assignee = assigned_to_id if assigned_to_id else None
    nt = notes if notes else None
    diff = difficulty if difficulty else None
    h = held if held else None
    hu = held_until if held_until else None
    dd = due_date if due_date else None
    sd = start_date if start_date else None
    dr = done_ratio if done_ratio != -1 else None
    fv: int | str | None = fixed_version_id if fixed_version_id else None
    cf, cf_err = _normalize_custom_fields(custom_fields)
    if cf_err is not None:
        return _dump(cf_err)

    async def factory(client, cache):
        return await issues.update_issue(
            client, cache, issue_id,
            subject=sub, description=desc, status=st, priority=pri,
            assigned_to_id=assignee, notes=nt, difficulty=diff,
            held=h, held_until=hu,
            due_date=dd, start_date=sd, done_ratio=dr,
            fixed_version_id=fv, custom_fields=cf,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_close_issue(issue_id: int, note: str = "") -> str:
    """Move an issue to its first ``is_closed`` status (defaults to id 5).

    Args:
        issue_id: numeric Redmine issue id.
        note: optional closing comment (becomes a journal entry).
            Use actual newline characters for multi-line notes, not
            backslash-n escape sequences. Redmine renders notes as
            Markdown (headings, bold, tables, lists all work).

    On a workflow-disallowed direct closure, the response is repackaged
    with a closure-specific hint listing the allowed next states.
    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    n = note if note else None

    async def factory(client, cache):
        return await issues.close_issue(client, cache, issue_id, note=n)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_delete_issue(
    issue_id: int,
    confirm_destructive: bool = False,
) -> str:
    """Permanently delete an issue. Cannot be undone.

    Args:
        issue_id: numeric Redmine issue id.
        confirm_destructive: must be ``True`` to proceed. Safety gate
            consistent with other destructive MCP tools.

    Returns the deleted issue's id and subject for confirmation.
    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    if not confirm_destructive:
        return _dump({
            "error": "confirmation_required",
            "hint": "Pass confirm_destructive=True to permanently delete "
                    f"issue #{issue_id}. This cannot be undone.",
        })

    async def factory(client, _cache):
        return await issues.delete_issue(client, issue_id)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_search_issues(
    query: str = "",
    project: str = "",
    status: str = "",
    query_id: int = 0,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Search/list issues with optional filters and pagination.

    Args:
        query: substring on issue subject (Redmine ``subject=~`` filter).
            Empty for unfiltered.
        project: optional numeric id or identifier slug.
        status: optional id, name, or one of Redmine's special tokens
            (``"open"``, ``"closed"``, ``"*"``). Lower-case tokens are
            passed through; named statuses (e.g. ``"Closed"``) resolve
            via the cache.
        query_id: optional Redmine *saved query* id (numeric). When set,
            invokes the saved query and merges any other filters (status,
            project, etc.) on top per Redmine's standard semantics.
            ``0`` (default) means no saved query.
        limit: page size (capped at 100).
        offset: skip the first N results.

    Returns ``{issues, total_count, limit, offset, query}``.
    """
    q = query if query else None
    proj: int | str | None = project if project else None
    st: int | str | None = status if status else None
    qid: int | None = query_id if query_id else None

    async def factory(client, cache):
        return await issues.search_issues(
            client, cache,
            query=q, project=proj, status=st, query_id=qid,
            limit=limit, offset=offset,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_add_comment(
    issue_id: int,
    note: str,
    private: bool = False,
) -> str:
    """Append a comment (journal entry) to an existing issue.

    Args:
        issue_id: numeric Redmine issue id.
        note: comment body. Empty/whitespace-only notes are rejected
            client-side. Use actual newline characters for multi-line
            notes, not backslash-n escape sequences (literal ``\\n``
            gets stored as visible text, not a line break). Redmine
            renders notes as Markdown — headings, bold, tables, and
            lists all work when separated by blank lines.
        private: if True, mark as a private journal (visible only to
            users with the ``view_private_notes`` Redmine permission).

    Honors ``REDMINE_MCP_READ_ONLY``. Direct PUT — no pre-fetch, no
    workflow check (comments don't change status).
    """
    async def factory(client, cache):
        return await comments.add_comment(
            client, cache, issue_id, note, private=private
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_get_journals(issue_id: int) -> str:
    """Return the structured journal entries for an issue.

    Args:
        issue_id: numeric Redmine issue id.

    Each journal entry has ``id``, ``user``, ``created_on``, ``notes``
    (the comment body if any), and ``details`` (a list of field-change
    records). Read-only.
    """
    async def factory(client, cache):
        return await comments.get_journals(client, cache, issue_id)
    return await _wrap(factory)


@mcp.tool()
async def redmine_update_journal(
    journal_id: int,
    notes: str,
) -> str:
    """Edit an existing journal entry's notes in place.

    Requires Redmine 5.0+. Use ``redmine_get_journals`` to find the
    ``journal_id`` to edit.

    Args:
        journal_id: numeric journal id.
        notes: replacement note text. Pass an empty string to clear the
            note (deletes the journal if it has no field-change details).
            Use actual newline characters for multi-line notes, not
            backslash-n escape sequences. Redmine renders notes as
            Markdown.

    Honors ``REDMINE_MCP_READ_ONLY``. The API user can only edit their
    own notes unless they have the ``edit_issue_notes`` permission.
    """
    async def factory(client, cache):
        return await comments.update_journal(
            client, cache, journal_id, notes
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_upload_attachment(
    file_path: str,
    issue_id: int = 0,
    description: str = "",
) -> str:
    """Upload a file (path-restricted) and optionally attach it to an issue.

    Args:
        file_path: absolute or ``~``-relative path to a regular file.
            Must resolve under one of ``REDMINE_MCP_ALLOWED_DIRECTORIES``
            (default ``/tmp``); symlinks are resolved before checking.
        issue_id: if non-zero, attach the upload to this issue. Otherwise
            return the bare upload token for later attachment.
        description: optional human-readable description for the
            attachment (only meaningful when attaching).

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    iid = issue_id if issue_id else None
    desc = description if description else None
    cfg = _get_config()
    allowed = cfg.allowed_directories

    async def factory(client, cache):
        return await attachments.upload_attachment(
            client, cache, file_path,
            allowed_directories=allowed,
            issue_id=iid,
            description=desc,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_download_attachment(
    attachment_id: int,
    save_to: str,
    overwrite: bool = False,
) -> str:
    """Download an attachment by id to a path-restricted location.

    Args:
        attachment_id: numeric Redmine attachment id (visible on issue
            payloads at ``attachments[].id``).
        save_to: target file path. Parent directory must exist and
            resolve under one of ``REDMINE_MCP_ALLOWED_DIRECTORIES``
            (default ``/tmp``); symlinks are resolved before checking.
        overwrite: if False (default), refuse to overwrite an existing
            file at ``save_to``.

    Validates downloaded byte count against Redmine's reported filesize
    before writing — short reads are surfaced as
    ``attachment_size_mismatch`` rather than silently saving a partial
    file. Read-only-safe (no Redmine writes).
    """
    async def factory(client, cache):
        cfg = _get_config()
        return await attachments.download_attachment(
            client, cache,
            attachment_id, save_to,
            allowed_directories=cfg.allowed_directories,
            overwrite=overwrite,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_create_time_entry(
    hours: str,
    issue_id: int = 0,
    project_id: int = 0,
    activity: str = "",
    spent_on: str = "",
    comments: str = "",
    user_id: int = 0,
) -> str:
    """Log a time entry against an issue or project.

    Args:
        hours: required. Decimal (e.g. ``"2.5"``) or ``"H:MM"`` (e.g.
            ``"2:30"``). Pre-validated client-side.
        issue_id: target issue. Either this or ``project_id`` is required.
        project_id: target project (when not logging against a specific
            issue). Ignored when ``issue_id`` is non-zero.
        activity: optional id or name (e.g. ``"Development"``); resolves
            against the cached activity enumeration. Empty for the
            default.
        spent_on: optional ``YYYY-MM-DD``; defaults to today server-side.
        comments: optional, max 1024 chars.
        user_id: admin-only override; otherwise current user.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    iid = issue_id if issue_id else None
    pid = project_id if project_id else None
    act = activity if activity else None
    son = spent_on if spent_on else None
    cmt = comments if comments else None
    uid = user_id if user_id else None

    async def factory(client, cache):
        return await time_entries.create_time_entry(
            client, cache,
            hours=hours, issue_id=iid, project_id=pid,
            activity=act, spent_on=son, comments=cmt, user_id=uid,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_list_time_entries(
    issue_id: int = 0,
    project_id: int = 0,
    user_id: int = 0,
    spent_on: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 25,
    offset: int = 0,
) -> str:
    """List time entries with optional filters.

    Args:
        issue_id, project_id, user_id: filter by reference id (``0`` for none).
        spent_on: exact-date filter (``YYYY-MM-DD``).
        from_date / to_date: date-range filter.
        limit: page size (capped at 100).
        offset: skip the first N results.
    """
    iid = issue_id if issue_id else None
    pid = project_id if project_id else None
    uid = user_id if user_id else None
    son = spent_on if spent_on else None
    fd = from_date if from_date else None
    td = to_date if to_date else None

    async def factory(client, cache):
        return await time_entries.list_time_entries(
            client, cache,
            issue_id=iid, project_id=pid, user_id=uid,
            spent_on=son, from_date=fd, to_date=td,
            limit=limit, offset=offset,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_update_time_entry(
    time_entry_id: int,
    hours: str = "",
    activity: str = "",
    spent_on: str = "",
    comments: str = "",
    issue_id: int = 0,
    project_id: int = 0,
) -> str:
    """Update a time entry. Partial — only supplied fields are sent.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    h = hours if hours else None
    act = activity if activity else None
    son = spent_on if spent_on else None
    cmt = comments if comments else None
    iid = issue_id if issue_id else None
    pid = project_id if project_id else None

    async def factory(client, cache):
        return await time_entries.update_time_entry(
            client, cache, time_entry_id,
            hours=h, activity=act, spent_on=son, comments=cmt,
            issue_id=iid, project_id=pid,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_delete_time_entry(time_entry_id: int) -> str:
    """Delete a time entry. Permanent — no soft-delete in Redmine.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await time_entries.delete_time_entry(client, cache, time_entry_id)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_add_watcher(issue_id: int, user_id: int) -> str:
    """Add a user as a watcher of an issue.

    Args:
        issue_id: numeric Redmine issue id.
        user_id: numeric user id to add to the watcher list.

    Idempotent on the Redmine side. Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await watchers.add_watcher(client, cache, issue_id, user_id)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_remove_watcher(issue_id: int, user_id: int) -> str:
    """Remove a user from an issue's watcher list.

    Args:
        issue_id: numeric Redmine issue id.
        user_id: numeric user id to remove.

    A 404 from Redmine means the watcher wasn't on the list (vs. issue
    not found — distinguishable by status code on the underlying error
    payload). Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await watchers.remove_watcher(client, cache, issue_id, user_id)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_list_watchers(issue_id: int) -> str:
    """Return the current watcher list for an issue.

    Each watcher entry has ``id`` and ``name``. Read-only.
    """
    async def factory(client, cache):
        return await watchers.list_watchers(client, cache, issue_id)
    return await _wrap(factory)


@mcp.tool()
async def redmine_get_wiki_page(
    project: str,
    title: str,
    version: int = 0,
) -> str:
    """Fetch a wiki page (optionally a historical version).

    Args:
        project: numeric id or identifier slug (e.g. ``"claudecode"``).
        title: wiki page title (URL-encoded automatically; spaces and
            unicode are fine).
        version: if non-zero, fetch that specific historical version.

    Returns the page (text, version, author, timestamps) or
    ``wiki_page_not_found``. Read-only.
    """
    proj: int | str = project
    with contextlib.suppress(TypeError, ValueError):
        proj = int(project)
    ver = version if version else None

    async def factory(client, cache):
        return await wiki.get_page(client, cache, proj, title, version=ver)
    return await _wrap(factory)


@mcp.tool()
async def redmine_create_wiki_page(
    project: str,
    title: str,
    text: str,
    parent_title: str = "",
    comments: str = "",
) -> str:
    """Create a new wiki page; refuses to overwrite an existing one.

    Args:
        project: numeric id or identifier slug.
        title: new wiki page title (URL-encoded automatically).
        text: markdown body. Empty/whitespace-only is rejected client-side.
        parent_title: optional parent wiki page (for hierarchy).
        comments: optional revision comment.

    Pre-flight: GET first to confirm the page doesn't exist. If it does,
    returns ``wiki_page_already_exists`` with the existing version.
    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    proj: int | str = project
    with contextlib.suppress(TypeError, ValueError):
        proj = int(project)
    parent = parent_title if parent_title else None
    cmt = comments if comments else None

    async def factory(client, cache):
        return await wiki.create_page(
            client, cache, proj, title, text,
            parent_title=parent, comments=cmt,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_update_wiki_page(
    project: str,
    title: str,
    text: str,
    version: int = 0,
    parent_title: str = "",
    comments: str = "",
) -> str:
    """Update an existing wiki page (with optional optimistic concurrency).

    Args:
        project: numeric id or identifier slug.
        title: target wiki page title.
        text: new markdown body. Empty is rejected client-side.
        version: if non-zero, included as the optimistic-lock version. A
            mismatch surfaces as ``redmine_api_409`` from Redmine.
        parent_title: optional new parent wiki page.
        comments: optional revision comment.

    Returns the updated page (re-fetched after PUT for fresh metadata).
    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    proj: int | str = project
    with contextlib.suppress(TypeError, ValueError):
        proj = int(project)
    ver = version if version else None
    parent = parent_title if parent_title else None
    cmt = comments if comments else None

    async def factory(client, cache):
        return await wiki.update_page(
            client, cache, proj, title, text,
            version=ver, parent_title=parent, comments=cmt,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_delete_wiki_page(project: str, title: str) -> str:
    """Permanently delete a wiki page (and all its historical versions).

    Args:
        project: numeric id or identifier slug.
        title: wiki page title.

    Returns ``{"deleted": true, ...}`` on success or ``wiki_page_not_found``.
    Honors ``REDMINE_MCP_READ_ONLY``. No soft-delete in Redmine.
    """
    proj: int | str = project
    with contextlib.suppress(TypeError, ValueError):
        proj = int(project)

    async def factory(client, cache):
        return await wiki.delete_page(client, cache, proj, title)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_list_relations(issue_id: int) -> str:
    """List all relations on an issue (both directions).

    Args:
        issue_id: numeric Redmine issue id.

    Each relation has ``id``, ``issue_id``, ``issue_to_id``,
    ``relation_type``, and ``delay``. Read-only.
    """
    async def factory(client, cache):
        return await relations.list_relations(client, cache, issue_id)
    return await _wrap(factory)


@mcp.tool()
async def redmine_add_relation(
    issue_id: int,
    target_issue_id: int,
    relation_type: str,
    delay: int = 0,
) -> str:
    """Create a relation between two issues.

    Args:
        issue_id: source issue.
        target_issue_id: the other end of the relation.
        relation_type: one of ``relates``, ``blocks``, ``blocked``,
            ``duplicates``, ``duplicated``, ``precedes``, ``follows``,
            ``copied_to``, ``copied_from`` — or a recognized alias
            (``related_to``, ``blocked_by``, ``duplicate_of``,
            ``duplicated_by``, ``copy_of``).
        delay: only meaningful for ``precedes`` / ``follows`` — number
            of days between the issues' due dates. ``0`` to omit.

    Honors ``REDMINE_MCP_READ_ONLY``. Cross-project relations require
    Redmine's ``cross_project_issue_relations`` setting to be enabled.
    """
    d = delay if delay else None

    async def factory(client, cache):
        return await relations.add_relation(
            client, cache,
            issue_id=issue_id,
            target_issue_id=target_issue_id,
            relation_type=relation_type,
            delay=d,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_remove_relation(relation_id: int) -> str:
    """Delete a relation by its numeric id.

    Args:
        relation_id: id from ``list_relations`` output (NOT an issue id).

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await relations.remove_relation(
            client, cache, relation_id=relation_id,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_set_parent_issue(issue_id: int, parent_issue_id: int) -> str:
    """Set or clear an issue's parent.

    Args:
        issue_id: issue whose parent is being set.
        parent_issue_id: numeric id of the new parent. Pass ``0`` to
            unparent the issue.

    Parent/child is mechanically a field on the issue (``parent_issue_id``),
    not a relation record. Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await relations.set_parent_issue(
            client, cache,
            issue_id=issue_id, parent_issue_id=parent_issue_id,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_bulk_create_issues(
    issues: list,
    on_duplicate: str = "skip",
    pacing_seconds: float = 0.05,
    stop_on_error: bool = False,
) -> str:
    """Bulk-create issues with subject idempotency (ClaudeCode#3141).

    Args:
        issues: list of issue spec dicts (≤ 100 per call). Each spec
            requires ``project``, ``tracker``, ``subject``; optional
            fields match ``redmine_create_issue`` (``description``,
            ``priority``, ``status``, ``assigned_to_id``, ``difficulty``,
            ``due_date``, ``start_date``, ``done_ratio``, ``custom_fields``).
        on_duplicate: ``"skip"`` (default) pre-checks each subject within
            its project and reports existing matches as ``skipped`` with
            ``duplicate_of``; ``"fail"`` reports duplicates as failures;
            ``"create_anyway"`` skips the pre-check entirely.
        pacing_seconds: sleep between POSTs (default 50ms — empirically
            the floor for not tripping Redmine's per-issue rate cap on
            small VMs). Set 0 to disable.
        stop_on_error: True to bail at first failure; remainder lands in
            ``skipped_for_stop_on_error``.

    Returns ``{"results": [{subject, status, id?, duplicate_of?, error?, hint?}],
    "summary": {total, created, skipped, failed}}``.
    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await bulk.bulk_create_issues(
            client, cache,
            issues=issues,
            on_duplicate=on_duplicate,
            pacing_seconds=pacing_seconds,
            stop_on_error=stop_on_error,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_bulk_update_issues(
    issue_ids: list[int],
    subject: str = "",
    description: str = "",
    status: str = "",
    priority: str = "",
    assigned_to_id: int = 0,
    notes: str = "",
    stop_on_error: bool = False,
) -> str:
    """Apply the same field updates to many issues in one call.

    Args:
        issue_ids: list of numeric issue ids (≤ 100 per call).
        subject, description, status, priority, assigned_to_id, notes:
            same semantics as ``redmine_update_issue`` — at least one
            must be supplied.
        stop_on_error: if True, halt at the first failure (remaining
            ids land in ``skipped``); otherwise best-effort across the
            whole batch.

    Returns ``{total, succeeded, failed, skipped}``. Honors
    ``REDMINE_MCP_READ_ONLY``. Sequential — Redmine has no batch endpoint.
    """
    sub = subject if subject else None
    desc = description if description else None
    st: int | str | None = status if status else None
    pri: int | str | None = priority if priority else None
    assignee = assigned_to_id if assigned_to_id else None
    nt = notes if notes else None

    async def factory(client, cache):
        return await bulk.bulk_update_issues(
            client, cache,
            issue_ids=issue_ids,
            subject=sub, description=desc, status=st, priority=pri,
            assigned_to_id=assignee, notes=nt,
            stop_on_error=stop_on_error,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_bulk_close(
    issue_ids: list[int],
    note: str = "",
    stop_on_error: bool = False,
) -> str:
    """Close many issues in one call (with optional shared note).

    Args:
        issue_ids: list of numeric issue ids (≤ 100 per call).
        note: optional shared closing comment (becomes a journal on each).
        stop_on_error: if True, halt on the first workflow-blocked or
            otherwise-failed closure.

    Returns ``{total, succeeded, failed, skipped}``. Honors
    ``REDMINE_MCP_READ_ONLY``.
    """
    n = note if note else None

    async def factory(client, cache):
        return await bulk.bulk_close(
            client, cache,
            issue_ids=issue_ids, note=n, stop_on_error=stop_on_error,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_list_versions(project: str) -> str:
    """List all versions / milestones defined on a project.

    Args:
        project: numeric id or identifier slug.

    Each version has ``id``, ``name``, ``status`` (open/locked/closed),
    ``due_date``, ``sharing``, etc. Read-only.
    """
    proj: int | str = project
    with contextlib.suppress(TypeError, ValueError):
        proj = int(project)

    async def factory(client, cache):
        return await versions.list_versions(client, cache, proj)
    return await _wrap(factory)


@mcp.tool()
async def redmine_get_version(version_id: int) -> str:
    """Fetch one version by id.

    Returns the version (name, status, due_date, sharing, ...) or
    ``version_not_found``. Read-only.
    """
    async def factory(client, cache):
        return await versions.get_version(client, cache, version_id)
    return await _wrap(factory)


@mcp.tool()
async def redmine_create_version(
    project: str,
    name: str,
    description: str = "",
    status: str = "",
    due_date: str = "",
    sharing: str = "",
    wiki_page_title: str = "",
) -> str:
    """Create a version on a project.

    Args:
        project: numeric id or identifier slug.
        name: required, must be unique within the project.
        description: optional.
        status: one of ``open`` / ``locked`` / ``closed``.
        due_date: ``YYYY-MM-DD``.
        sharing: one of ``none`` / ``descendants`` / ``hierarchy`` /
            ``tree`` / ``system``.
        wiki_page_title: optional linked wiki page.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    proj: int | str = project
    with contextlib.suppress(TypeError, ValueError):
        proj = int(project)
    desc = description if description else None
    st = status if status else None
    dd = due_date if due_date else None
    sh = sharing if sharing else None
    wpt = wiki_page_title if wiki_page_title else None

    async def factory(client, cache):
        return await versions.create_version(
            client, cache,
            project=proj, name=name,
            description=desc, status=st, due_date=dd,
            sharing=sh, wiki_page_title=wpt,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_update_version(
    version_id: int,
    name: str = "",
    description: str = "",
    status: str = "",
    due_date: str = "",
    sharing: str = "",
    wiki_page_title: str = "",
) -> str:
    """Update a version. Partial — only supplied fields are sent.

    Returns the refreshed version. Honors ``REDMINE_MCP_READ_ONLY``.
    """
    n = name if name else None
    desc = description if description else None
    st = status if status else None
    dd = due_date if due_date else None
    sh = sharing if sharing else None
    wpt = wiki_page_title if wiki_page_title else None

    async def factory(client, cache):
        return await versions.update_version(
            client, cache, version_id=version_id,
            name=n, description=desc, status=st, due_date=dd,
            sharing=sh, wiki_page_title=wpt,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_delete_version(version_id: int) -> str:
    """Delete a version by id. Permanent — no soft-delete.

    A 422 typically means the version is still referenced by issues; clear
    those first via ``redmine_assign_issue_to_version`` with ``version_id=0``.
    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await versions.delete_version(
            client, cache, version_id=version_id,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_assign_issue_to_version(
    issue_id: int, version_id: int,
) -> str:
    """Assign (or clear) an issue's target version.

    Args:
        issue_id: numeric issue id.
        version_id: numeric version id; pass ``0`` to clear.

    Thin wrapper over ``redmine_update_issue`` that sets
    ``fixed_version_id``. Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await versions.assign_issue_to_version(
            client, cache, issue_id=issue_id, version_id=version_id,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_list_news(
    project: str = "",
    limit: int = 25,
    offset: int = 0,
) -> str:
    """List news entries (global feed, or scoped to a project).

    Args:
        project: numeric id or identifier slug. Empty for the global feed
            across every project the API key can see.
        limit: page size (capped server-side at 100).
        offset: skip the first N results.

    Returns ``{news, total_count, limit, offset, source}``. Read-only.
    """
    proj: int | str | None
    if project:
        proj = project
        with contextlib.suppress(TypeError, ValueError):
            proj = int(project)
    else:
        proj = None

    async def factory(client, cache):
        return await news.list_news(
            client, cache, project=proj, limit=limit, offset=offset,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_list_messages(
    board_id: int,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """List forum messages on a board.

    Args:
        board_id: numeric Redmine board id (visible in the project's
            forums URL or via the web UI; ``redmine_request`` covers
            ``/projects/X/boards.json`` if the boards module is enabled).
        limit: page size (capped server-side at 100).
        offset: skip the first N results.

    Returns ``{messages, total_count, limit, offset, board_id, source}``.
    A 404 typically means the boards module isn't enabled on the parent
    project, or the board_id doesn't exist.  Read-only.
    """
    async def factory(client, cache):
        return await forums.list_messages(
            client, cache, board_id=board_id, limit=limit, offset=offset,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_list_boards(project_id: str) -> str:
    """List forum boards for a project.

    Args:
        project_id: numeric id or identifier slug.

    Returns ``{boards, count, project_id}``. Read-only.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)

    async def factory(client, cache):
        return await forums.list_boards(client, cache, project_id=pid)
    return await _wrap(factory)


@mcp.tool()
async def redmine_create_message(
    board_id: int,
    subject: str,
    content: str = "",
) -> str:
    """Create a new forum topic on a board.

    Args:
        board_id: numeric board id.
        subject: required topic subject.
        content: optional message body.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    c = content if content else ""

    async def factory(client, cache):
        return await forums.create_message(
            client, cache, board_id=board_id, subject=subject, content=c,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_reply_message(
    board_id: int,
    topic_id: int,
    content: str,
) -> str:
    """Reply to an existing forum topic.

    Args:
        board_id: board the topic belongs to.
        topic_id: the parent message/topic id.
        content: required reply body.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await forums.reply_message(
            client, cache, board_id=board_id, topic_id=topic_id, content=content,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_delete_message(message_id: int) -> str:
    """Delete a forum message. Permanent.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await forums.delete_message(client, cache, message_id=message_id)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_create_news(
    project: str,
    title: str,
    summary: str = "",
    description: str = "",
) -> str:
    """Create a news entry on a project.

    Args:
        project: numeric id or identifier slug.
        title: required news title.
        summary: optional short summary.
        description: optional full body.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    proj: int | str = project
    with contextlib.suppress(TypeError, ValueError):
        proj = int(project)
    s = summary if summary else None
    d = description if description else None

    async def factory(client, cache):
        return await news.create_news(
            client, cache, project=proj, title=title, summary=s, description=d,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_update_news(
    news_id: int,
    title: str = "",
    summary: str = "",
    description: str = "",
) -> str:
    """Update a news entry. Partial — only supplied fields are sent.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    t = title if title else None
    s = summary if summary else None
    d = description if description else None

    async def factory(client, cache):
        return await news.update_news(
            client, cache, news_id=news_id, title=t, summary=s, description=d,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_delete_news(news_id: int) -> str:
    """Delete a news entry. Permanent.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await news.delete_news(client, cache, news_id=news_id)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_list_memberships(
    project_id: str,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """List project members (paginated).

    Args:
        project_id: numeric id or identifier slug.
        limit: page size (capped at 100).
        offset: skip the first N results.

    Read-only.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)

    async def factory(client, cache):
        return await memberships.list_memberships(
            client, cache, project_id=pid, limit=limit, offset=offset,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_add_membership(
    project_id: str,
    user_id: int,
    role_ids: list,
) -> str:
    """Add a member to a project.

    Args:
        project_id: project id or slug.
        user_id: user or group id to add.
        role_ids: list of role ids to assign (required, non-empty).

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)

    async def factory(client, cache):
        return await memberships.add_membership(
            client, cache, project_id=pid, user_id=user_id, role_ids=role_ids,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_update_membership(
    membership_id: int,
    role_ids: list,
) -> str:
    """Update a membership's roles.

    Args:
        membership_id: numeric membership id (from ``list_memberships``).
        role_ids: new list of role ids (required, non-empty).

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await memberships.update_membership(
            client, cache, membership_id=membership_id, role_ids=role_ids,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_remove_membership(membership_id: int) -> str:
    """Remove a project membership. Inherited memberships can't be deleted.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await memberships.remove_membership(
            client, cache, membership_id=membership_id,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_list_groups() -> str:
    """List all groups (admin only). Read-only."""
    async def factory(client, cache):
        return await groups.list_groups(client, cache)
    return await _wrap(factory)


@mcp.tool()
async def redmine_get_group(
    group_id: int,
    include: str = "",
) -> str:
    """Fetch a group by id.

    Args:
        group_id: numeric group id.
        include: comma-separated includes: ``users``, ``memberships``.

    Read-only.
    """
    inc = include if include else None

    async def factory(client, cache):
        return await groups.get_group(client, cache, group_id=group_id, include=inc)
    return await _wrap(factory)


@mcp.tool()
async def redmine_create_group(
    name: str,
    user_ids: list | str = "",
) -> str:
    """Create a group (admin only).

    Args:
        name: required group name.
        user_ids: optional list of user ids as initial members.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    uids: list[int] | None = None
    if isinstance(user_ids, list) and user_ids:
        uids = user_ids
    elif isinstance(user_ids, str) and user_ids:
        import json as _json
        try:
            parsed = _json.loads(user_ids)
            uids = parsed if isinstance(parsed, list) else None
        except _json.JSONDecodeError:
            pass

    async def factory(client, cache):
        return await groups.create_group(client, cache, name=name, user_ids=uids)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_update_group(group_id: int, name: str) -> str:
    """Update a group's name.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await groups.update_group(client, cache, group_id=group_id, name=name)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_delete_group(group_id: int) -> str:
    """Delete a group (admin only). Permanent.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await groups.delete_group(client, cache, group_id=group_id)
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_add_group_user(group_id: int, user_id: int) -> str:
    """Add a user to a group.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await groups.add_group_user(
            client, cache, group_id=group_id, user_id=user_id,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_remove_group_user(group_id: int, user_id: int) -> str:
    """Remove a user from a group.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    async def factory(client, cache):
        return await groups.remove_group_user(
            client, cache, group_id=group_id, user_id=user_id,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_list_issue_categories(project_id: str) -> str:
    """List issue categories for a project.

    Args:
        project_id: numeric id or identifier slug.

    Read-only.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)

    async def factory(client, cache):
        return await issue_categories.list_issue_categories(
            client, cache, project_id=pid,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_create_issue_category(
    project_id: str,
    name: str,
    assigned_to_id: int = 0,
) -> str:
    """Create an issue category on a project.

    Args:
        project_id: numeric id or identifier slug.
        name: required category name.
        assigned_to_id: optional default assignee. ``0`` for none.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)
    aid = assigned_to_id if assigned_to_id else None

    async def factory(client, cache):
        return await issue_categories.create_issue_category(
            client, cache, project_id=pid, name=name, assigned_to_id=aid,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_update_issue_category(
    category_id: int,
    name: str = "",
    assigned_to_id: int = 0,
) -> str:
    """Update an issue category. Partial.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    n = name if name else None
    aid = assigned_to_id if assigned_to_id else None

    async def factory(client, cache):
        return await issue_categories.update_issue_category(
            client, cache, category_id=category_id, name=n, assigned_to_id=aid,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_delete_issue_category(
    category_id: int,
    reassign_to_id: int = 0,
) -> str:
    """Delete an issue category. Permanent.

    Args:
        category_id: numeric category id.
        reassign_to_id: optional category id to reassign affected issues.
            ``0`` for no reassignment.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    rid = reassign_to_id if reassign_to_id else None

    async def factory(client, cache):
        return await issue_categories.delete_issue_category(
            client, cache, category_id=category_id, reassign_to_id=rid,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_list_enumerations(enum_type: str) -> str:
    """List enumeration values for a given type.

    Args:
        enum_type: one of ``issue_priorities``, ``time_entry_activities``,
            ``document_categories``.

    Read-only reference data.
    """
    async def factory(client, cache):
        return await enumerations.list_enumerations(
            client, cache, enum_type=enum_type,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_list_roles() -> str:
    """List all roles. Read-only."""
    async def factory(client, cache):
        return await roles.list_roles(client, cache)
    return await _wrap(factory)


@mcp.tool()
async def redmine_get_role(role_id: int) -> str:
    """Fetch a role by id, including its permissions list.

    Read-only.
    """
    async def factory(client, cache):
        return await roles.get_role(client, cache, role_id=role_id)
    return await _wrap(factory)


@mcp.tool()
async def redmine_list_issue_statuses() -> str:
    """List all issue statuses with their ``is_closed`` flags.

    Read-only reference data.
    """
    async def factory(client, cache):
        return await issue_statuses.list_issue_statuses(client, cache)
    return await _wrap(factory)


@mcp.tool()
async def redmine_list_custom_fields() -> str:
    """List all custom field definitions (admin only).

    Returns id, name, field_format, customized_type, is_required,
    is_filter, possible_values, etc. Read-only.
    """
    async def factory(client, cache):
        return await custom_fields.list_custom_fields(client, cache)
    return await _wrap(factory)


@mcp.tool()
async def redmine_list_queries(project_id: str = "") -> str:
    """List available saved queries.

    Args:
        project_id: optional project id or slug to scope queries.
            Empty for all visible queries.

    Use the returned query ids with
    ``redmine_search_issues(query_id=...)`` to execute them. Read-only.
    """
    pid: int | str | None = None
    if project_id:
        pid = project_id
        with contextlib.suppress(TypeError, ValueError):
            pid = int(project_id)

    async def factory(client, cache):
        return await queries.list_queries(client, cache, project_id=pid)
    return await _wrap(factory)


@mcp.tool()
async def redmine_list_project_files(project_id: str) -> str:
    """List files on a project's Files section.

    Args:
        project_id: numeric id or identifier slug.

    Read-only.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)

    async def factory(client, cache):
        return await files.list_project_files(client, cache, project_id=pid)
    return await _wrap(factory)


@mcp.tool()
async def redmine_upload_project_file(
    project_id: str,
    file_path: str,
    filename: str = "",
    description: str = "",
    version_id: int = 0,
) -> str:
    """Upload a file to a project's Files section.

    Args:
        project_id: project id or slug.
        file_path: path to file (path-restricted like attachment upload).
        filename: optional override for the file name.
        description: optional file description.
        version_id: optional version to associate. ``0`` for none.

    Honors ``REDMINE_MCP_READ_ONLY``.
    """
    pid: int | str = project_id
    with contextlib.suppress(TypeError, ValueError):
        pid = int(project_id)
    fn = filename if filename else None
    desc = description if description else None
    vid = version_id if version_id else None
    cfg = _get_config()
    allowed = cfg.allowed_directories

    async def factory(client, cache):
        return await files.upload_project_file(
            client, cache,
            project_id=pid, file_path=file_path,
            allowed_directories=allowed,
            filename=fn, description=desc, version_id=vid,
        )
    return await _wrap(factory, write=True)


@mcp.tool()
async def redmine_get_user(
    user_id: str = "current",
    include: str = "",
) -> str:
    """Fetch a user by id, or the current API user.

    Args:
        user_id: numeric user id, or ``"current"`` (default) for the
            authenticated API user.
        include: comma-separated includes: ``memberships``, ``groups``.
            Empty for none.

    Read-only.
    """
    uid: int | str = user_id
    if user_id != "current":
        with contextlib.suppress(TypeError, ValueError):
            uid = int(user_id)
    inc = include if include else None

    async def factory(client, cache):
        return await users.get_user(client, cache, user_id=uid, include=inc)
    return await _wrap(factory)


@mcp.tool()
async def redmine_list_users(
    name: str = "",
    group_id: int = 0,
    status: int = -1,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """List users (admin only).

    Args:
        name: filter by login, firstname, lastname, or mail.
        group_id: filter by group membership. ``0`` for no filter.
        status: filter by user status. ``-1`` (default) for no filter.
            Values: ``0`` anonymous, ``1`` active, ``2`` registered,
            ``3`` locked.
        limit: page size (capped at 100).
        offset: skip the first N results.

    Read-only. Requires admin privileges.
    """
    n = name if name else None
    gid = group_id if group_id else None
    st = status if status >= 0 else None

    async def factory(client, cache):
        return await users.list_users(
            client, cache,
            name=n, group_id=gid, status=st,
            limit=limit, offset=offset,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_search(
    query: str,
    project: str = "",
    resource_types: list | str = "",
    all_words: bool = True,
    titles_only: bool = False,
    open_issues: bool = False,
    attachments: str = "0",
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Full-text search across all Redmine resource types.

    Searches issues, wiki pages, news, changesets, messages, projects,
    and documents. Complements ``redmine_search_issues`` which only
    covers issues with structured filters.

    Args:
        query: search string (required, non-empty).
        project: optional project id or slug to scope the search.
            Empty for global search.
        resource_types: optional list of resource types to include
            (e.g. ``["issues", "wiki_pages"]``). Empty searches all.
            Allowed: ``issues``, ``news``, ``documents``, ``changesets``,
            ``wiki_pages``, ``messages``, ``projects``.
        all_words: if True (default), match all words; if False, any.
        titles_only: if True, only search titles.
        open_issues: if True, only return open issues (ignored for
            other resource types).
        attachments: ``"0"`` (description only), ``"1"`` (description +
            attachments), ``"only"`` (attachments only).
        limit: page size (capped at 100).
        offset: skip the first N results.

    Returns ``{results, total_count, limit, offset}``.
    """
    proj: int | str | None = None
    if project:
        proj = project
        with contextlib.suppress(TypeError, ValueError):
            proj = int(project)

    rt: list[str] | None = None
    if isinstance(resource_types, list):
        rt = resource_types or None
    elif resource_types:
        import json as _json
        try:
            parsed = _json.loads(resource_types)
            rt = parsed if isinstance(parsed, list) else None
        except _json.JSONDecodeError:
            rt = [s.strip() for s in resource_types.split(",") if s.strip()]

    async def factory(client, cache):
        return await search.search(
            client, cache,
            query=query, project=proj, resource_types=rt,
            all_words=all_words, titles_only=titles_only,
            open_issues=open_issues, attachments=attachments,
            limit=limit, offset=offset,
        )
    return await _wrap(factory)


@mcp.tool()
async def redmine_request(
    method: str,
    path: str,
    body: str | dict = "",
    params: str | dict = "",
) -> str:
    """**ESCAPE HATCH** — generic passthrough to any Redmine REST endpoint.

    Bypasses redmine-mcp's validation, workflow checks, and schema cache.
    Every response carries ``validation_skipped: true`` so the caller
    can't accidentally forget. Gated behind
    ``REDMINE_MCP_ENABLE_PASSTHROUGH=true`` — calls return
    ``passthrough_disabled`` if the flag isn't set.

    Args:
        method: HTTP verb. One of ``GET``, ``POST``, ``PUT``, ``DELETE``,
            ``PATCH``. Case-insensitive.
        path: must start with ``/``. Joined onto ``REDMINE_URL`` by the
            client. Example: ``"/custom_fields.json"``.
        body: body for POST/PUT/PATCH. Accepted as either a JSON-encoded
            string (e.g. ``'{"issue": {"subject": "renamed"}}'``) or a
            JSON object passed directly (e.g. ``{"issue": {"subject":
            "renamed"}}``). Empty string / empty dict means no body.
            (The dual form is intentional — some MCP transports auto-parse
            JSON-shaped string args into objects before the tool sees them.)
        params: query params for GET, in the same dual string/object form.

    Honors ``REDMINE_MCP_READ_ONLY`` for non-GET methods.
    """
    cfg = _get_config()
    if not cfg.enable_passthrough:
        return _dump({
            "error": "passthrough_disabled",
            "hint": (
                "redmine_request is opt-in. Set "
                "REDMINE_MCP_ENABLE_PASSTHROUGH=true to enable. "
                "Prefer the validated tools (redmine_get_issue etc.) "
                "whenever they cover your use case."
            ),
            "validation_skipped": True,
        })

    body_json: dict | None = None
    if isinstance(body, dict):
        body_json = body or None
    elif body:
        try:
            body_json = json.loads(body)
        except json.JSONDecodeError as e:
            return _dump({
                "error": "passthrough_body_invalid_json",
                "hint": f"body must be valid JSON: {e}",
                "validation_skipped": True,
            })

    params_json: dict | None = None
    if isinstance(params, dict):
        params_json = params or None
    elif params:
        try:
            params_json = json.loads(params)
        except json.JSONDecodeError as e:
            return _dump({
                "error": "passthrough_params_invalid_json",
                "hint": f"params must be valid JSON: {e}",
                "validation_skipped": True,
            })

    is_write = (method or "").strip().upper() != "GET"

    async def factory(client, cache):
        return await passthrough.request(
            client, cache,
            method=method, path=path,
            body=body_json, params=params_json,
        )
    return await _wrap(factory, write=is_write)


def main() -> None:
    """Console-script entry point. Runs the MCP server over stdio."""
    _get_config()
    mcp.run()


if __name__ == "__main__":
    main()
