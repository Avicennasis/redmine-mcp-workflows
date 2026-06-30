# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] — 2026-06-02

### Added
- **`redmine_update_journal` .** Edit an existing journal entry's
  notes in place via `PUT /journals/:id.json` (Redmine 5.0+). The API
  user can edit their own notes; users with `edit_issue_notes` permission
  can edit any note. Passing empty notes on a details-only journal
  deletes it. Honors `REDMINE_MCP_READ_ONLY`.

## [0.6.0] — 2026-05-16

Held-field gate: non-ticket blockers for Redmine issues.

### Added
- **`IssueHeld` structured error class.** New error type returned when
  attempting to close a ticket that has a non-empty "Held" custom field.
  Includes `issue_id`, `held_reason`, and optional `held_until` in the
  structured payload. Error format:
  `Cannot close #N: held — "reason" (held until YYYY-MM-DD)`.
- **`check_held_gate()` validator** in `validation/fields.py`. Inspects
  an issue's `custom_fields` for a non-empty "Held" field. Returns
  `IssueHeld` when the gate is active, `None` otherwise. Whitespace-only
  and date-without-reason values are not considered holds.
- **Held-gate enforcement in `update_issue()`.** When a status transition
  targets a closed status and the issue has a non-empty "Held" custom
  field, `update_issue` returns `IssueHeld` before sending the PUT. This
  covers `close_issue`, `bulk_close`, and `bulk_update_issues` since all
  delegate to `update_issue`. Non-close transitions (e.g., New → In
  Progress) are unaffected. The check runs before the workflow transition
  check for efficiency (no user fetch needed).

## [0.5.0] — 2026-05-12

Dogfood-driven fixes from .
Feature work:  Path A (OAuth2 bearer token),
 (saved-query lookups), and  (bulk-create).

### Added
- **`redmine_bulk_create_issues`.** Bulk-create from
  per-spec dicts with subject idempotency. Pre-checks each subject within
  its project via exact-match (post-filter on Redmine's substring-fuzzy
  `subject` filter) and reports existing matches per `on_duplicate`:
  `"skip"` (default) → `status="skipped"` + `duplicate_of`, `"fail"` →
  `status="failed"` + `error="duplicate_subject"`, `"create_anyway"` →
  bypass the lookup. Default 50ms pacing between POSTs (configurable;
  empirically the floor for not tripping Redmine's per-issue rate cap
  on small VMs). Returns `{results, summary}` with the full per-spec
  outcome. ≤100 specs per call (matches MAX_BATCH_SIZE for the other
  bulk tools). Replaces ~150 lines of direct-HTTP scaffolding in the
  fleet's `ServerOps/scripts/redmine/create-*.py` exemplars with a
  ~30-line call.
- **`redmine_search_issues` accepts a `query_id` parameter
 .** Forwards to Redmine's `?query_id=N` so callers
  can invoke saved queries directly without dropping to the passthrough.
  Layers with `status` / `project` / `query` / `limit` / `offset` per
  Redmine's standard merge semantics. ``query_id=0`` (default) means
  no saved query — current behavior unchanged.
- **OAuth2 bearer-token support (Path A).** New
  `REDMINE_OAUTH_TOKEN` env var (and matching `REDMINE_OAUTH_TOKEN=...`
  line in the secrets file) lets the wrapper authenticate via
  Doorkeeper-issued access tokens (Redmine 6.1+) sent as
  `Authorization: Bearer <token>`. When both `REDMINE_OAUTH_TOKEN` and
  `REDMINE_API_KEY` are configured, the OAuth bearer wins — OAuth is the
  explicit-opt-in path and shouldn't be shadowed by a stale API key in
  `secrets.md`. New `Config.require_auth_headers()` returns the right
  header dict; `Config.require_api_key()` retained for back-compat.
  Path B (auth-code flow + local-callback PKCE) and Path C (device
  grant — needs Redmine image rebuild) deferred to future tickets.
- **Silent status no-op detection on `redmine_update_issue` and
  `redmine_close_issue`.** Previously, when Redmine accepted a PUT (2xx)
  but didn't apply the requested `status_id` change — most commonly because
  the `block_descendants_issues_closing` setting blocks parents with open
  subtasks, or a custom workflow rule the cache hasn't yet observed — the
  caller got a success-looking payload with the old status. Now the
  post-PUT re-fetch includes `children` and the response is a structured
  `status_change_silently_ignored` error with a hint that names the
  blocking subtasks (if the target was a closed status and children exist)
  or lists the likely causes generically. Returns `requested_status_id`,
  `requested_status`, `actual_status_id`, `actual_status`, plus the issue
  payload for inspection. Workflow observations are NOT recorded for
  silent no-ops — only confirmed-moved transitions count.

### Fixed
- **`redmine_upload_attachment` verifies the attachment actually attached
 .** Redmine's `PUT /issues/{id}.json` with `uploads:[...]`
  silently drops the attachment under per-issue rate pressure (observed:
  ~1 PUT/sec cap), returning HTTP 200 regardless. The wrapper now does a
  post-PUT `GET /issues/{id}.json?include=attachments` and checks that the
  uploaded filename appears in `attachments[]`. If not, it retries the
  PUT with the same token after 2s, then 5s, then surfaces a structured
  `attachment_not_attached` error preserving the upload token so the
  caller can recover manually. Backoffs tunable via
  `ATTACHMENT_VERIFY_BACKOFFS` (tests monkeypatch to `(0, 0)`).
- **`redmine_list_projects` substring filter now searches across all
  pages.** Previously the `query` filter only saw the first page returned
  by `/projects.json` with the given `limit`/`offset` — matches that lived
  later in the list silently dropped (caught dogfooding: `query="mcp"`
  with default `limit=25` missed `kronos-mcp` even though it exists).
  When `query` is set, list_projects now walks every page (capped at
  1000 projects) before filtering, then re-slices by `limit`/`offset`.
  `total_count` reflects the *filtered* total when filtering, so callers
  can paginate over matches directly. Unfiltered behavior unchanged.

- **`redmine_create_issue` and `redmine_update_issue` expose more fields
 .** New params on both:
  - `due_date: str = ""` (ISO-8601 date)
  - `start_date: str = ""` (ISO-8601 date)
  - `done_ratio: int = -1` (sentinel for unchanged; `0` is explicit)
  - `custom_fields: list | str = ""` (accepts native list or JSON string)
  Plus `fixed_version_id` is now first-class on the `redmine_update_issue`
  MCP wrapper (it was already supported internally). Previously these
  fields were only reachable via `redmine_request`, which itself was
  unusable . New `_normalize_custom_fields` helper handles the
  dual list/string shape with a structured error for malformed input.
- **`redmine_request` accepts dict bodies and params.**
  Some MCP transports auto-parse JSON-shaped string args into objects
  before the tool sees them, which caused pydantic to reject the input
  against the previous `body: str` schema with
  `Input should be a valid string [type=string_type, input_value={...}]`.
  Made the escape hatch genuinely an escape hatch: `body` and `params`
  now accept either a JSON-encoded string (existing) or a dict (new),
  with the wrapper normalizing to the internal dict form. Empty string
  and empty dict both mean "no body / no params" (unchanged semantics).

### Notes
- 359 tests pass (was 309) — 50 new across the regression fixes + features.

## [0.4.0] — 2026-05-11

### Added
- **Difficulty custom-field support.** `redmine_create_issue` and
  `redmine_update_issue` now accept a `difficulty` parameter for the
  global `Difficulty` custom field. Values: `Unclassified` / `Easy` /
  `Normal` / `Hard`. Engagement-mode signal (how much human oversight a
  ticket needs), distinct from `Priority`.
- **Default-fill on create** — `redmine_create_issue` without a
  `difficulty` arg (and without a `Difficulty` entry in `custom_fields`)
  default-fills `Unclassified`, so auto-callers don't trip the
  required-field validation. `redmine_update_issue` does **not**
  default-fill (would silently overwrite user-set values).
- **Conflict resolution** — when both `difficulty=...` and a matching
  `custom_fields` entry are passed, `difficulty` wins.
- **`redmine_describe_tracker`** output now includes a `custom_fields`
  array listing the issue custom fields applicable to that tracker
  (id, name, format_kind, is_required, default_value, possible_values,
  applicable_tracker_ids, for_all_projects). Lazy-loads
  `/custom_fields.json` on cache miss.
- New module `redmine_mcp.schema.custom_fields` with
  `refresh_custom_fields()` + `get_custom_field_by_name()`.
- New schema-cache table `custom_fields` (migration v3) plus
  `put_custom_field` / `get_custom_field` / `list_custom_fields` /
  `get_custom_field_by_name` accessors on `SchemaCache`.

### Notes
- `/custom_fields.json` is admin-only on Redmine. If the configured API
  key lacks admin scope, custom-field discovery silently returns empty —
  `redmine_create_issue` / `redmine_update_issue` still work, but the
  `difficulty=` convenience parameter cannot resolve the field id and
  becomes a no-op.
- Redmine 6.x's REST list response omits the `is_for_all` key, so
  `for_all_projects` is derived from absence of a `projects` array on
  the field record.

Refs: serverops #2582, claudecode #2583.

### Security
- Bump `mcp` SDK pin to `>=1.23.0,<1.24.0` (running 1.23.3) to clear three high-severity advisories on the prior `<1.9.0` pin: MCP Python SDK missing DNS rebinding protection (fixed 1.23.0), FastMCP validation-error DoS (1.9.4), and Streamable HTTP Transport unhandled-exception DoS (1.10.0). Held on `<1.24.0` (vs. the fleet canary digitalocean-dns-mcp on `<2.0.0`) because this server is in active dogfood. All 279 tests pass on 1.23.3.

### Fixed
- `redmine_create_issue(project=...)` now accepts the project's display
  name (e.g. `"Infra"`), not just the lowercase identifier slug
  (`"infra"`). The natural round-trip pattern of reading
  `redmine_get_issue(...).project.name` and feeding it back into
  `redmine_create_issue` was failing with a generic `redmine_api_404`
  because the slug lookup raised, the listing fallback never ran, and
  there was no clue that "name vs slug" was the real problem
  .
  - `_resolve_project_id` now falls through slug → cache-by-name →
    refreshed `/projects.json` listing-by-name before giving up.
    Successful name resolutions warm the cache so subsequent calls are
    cheap.
  - `schema/project.describe_project` catches the upstream `RedmineAPIError(404)`
    and returns a structured `project_not_found` dict instead of
    bubbling the exception, so the resolver can fall through cleanly.
  - `SchemaCache.get_project_by_name(name)` — case-insensitive search
    across cached projects' `schema_json.name` field. The projects table
    indexes on `identifier` only, so this is a small in-memory scan over
    a small fleet of cached entries (correctness over performance per
    the existing schema_db comment).
  - When neither slug nor name resolves, the response is now the clean
    structured `{"error": "project_not_found", "hint": "No project matches '<value>'.", ...}`
    instead of a generic 404, so LLM callers get actionable specificity.
- 2 new tests in `tests/test_tools/test_issues.py`:
  - `test_create_issue_resolves_project_by_display_name_via_cache` —
    direct regression for #2568, exercises the cache-by-name path after
    a 404 on the slug.
  - `test_create_issue_resolves_project_by_name_via_list_refresh` —
    exercises the list-refresh fallback when name isn't pre-cached, and
    asserts the cache is warmed for next time.
- `test_create_issue_returns_project_not_found_when_unresolvable` was
  rewritten to use the realistic 404-from-real-Redmine path (was
  testing a fictional `200 OK` with `{"project": null}` body that real
  Redmine never produces).
- 279 tests pass (+2 vs prior).

### Added
- v0.5 first feature: news + forum-board read tools (Redmine
  #2390) — 2 new tools.
  40 of the planned set live total.
  - `tools/news.py` — `list_news(project=None, limit, offset)`. Empty
    `project` hits `/news.json` (the global feed); a numeric id or slug
    hits `/projects/{id}/news.json` (the project feed). Same tool name
    covers both because callers always know which one they want and
    splitting them adds no value.
  - `tools/forums.py` — `list_messages(board_id, limit, offset)` →
    `/boards/{board_id}/messages.json`. A 404 typically means the
    boards module isn't enabled on the parent project; we surface the
    structured error rather than masking. No `list_boards` companion —
    `/projects/X/boards.json` is inconsistently enabled across Redmine
    versions; `redmine_request` covers it for callers who need it.
  - `server.py` — `redmine_list_news(project="", limit, offset)` and
    `redmine_list_messages(board_id, limit, offset)`. Both read-only.
- 10 new unit tests in `tests/test_tools/test_news.py` (6) and
  `tests/test_tools/test_forums.py` (4) covering global vs.
  project-scoped paths, default + propagated pagination args, 404
  surfacing, and defensive non-dict-response handling. 277 tests pass
  total; ruff clean.

- v0.3 first feature: `redmine_request` generic-passthrough escape hatch
  — 1 new
  tool, opt-in. 38 of the planned set live total.
  - `tools/passthrough.py` — sends arbitrary HTTP requests to any
    Redmine REST endpoint with NO validation, NO workflow check, and
    NO schema cache. Every response carries `validation_skipped: true`
    plus a human-readable `warning` field so callers cannot accidentally
    forget they bypassed the validation layer.
  - **Gated behind `REDMINE_MCP_ENABLE_PASSTHROUGH=true`** — calls
    return a structured `passthrough_disabled` error if the flag isn't
    set. Default-off because the tool is in by-design tension with
    redmine-mcp's "validate first" identity; users opt in when they
    need an endpoint we don't yet wrap.
  - `config.py` — new `enable_passthrough: bool` field plus parsing.
  - `server.py` — new `redmine_request(method, path, body, params)`
    tool. JSON-encoded body / params (parsed client-side). Honors
    `REDMINE_MCP_READ_ONLY` for non-GET methods.
- 12 new unit tests in `tests/test_tools/test_passthrough.py` covering
  every method (GET / POST / PUT / DELETE), method-case normalization,
  empty-path / missing-leading-slash / unknown-method validation, the
  universal `validation_skipped` flag, and error-envelope round-trip.
- 3 new tests in `tests/test_config.py` for the new env var (default
  false, truthy / falsey value parsing). 267 tests pass total; ruff clean.
- Live smoke verified end-to-end:
  read-only GET (with and without query params), POST → PUT → DELETE
  round-trip on a transient passthrough-only ticket (#2440), error
  envelopes for `OPTIONS` / missing-leading-slash / 404, plus gate-off
  default and gate-on enable both confirmed at the server-tool layer.

### Added (continued)
- v0.2 seventh feature: versions / milestones CRUD (Redmine
  #2382) — 6 new tools:
  `list_versions`, `get_version`, `create_version`, `update_version`,
  `delete_version`, `assign_issue_to_version`. 37 of the planned set
  live total.
  - `tools/versions.py` — full project-versions CRUD against
    `/projects/{p}/versions.json` (list/create) and `/versions/{id}.json`
    (get/update/delete). Status (`open`/`locked`/`closed`) and sharing
    (`none`/`descendants`/`hierarchy`/`tree`/`system`) enums are
    validated client-side so a typo fails fast with a hint instead of
    a generic 422. `due_date` is shape-checked (`YYYY-MM-DD` regex);
    Redmine still does the calendar validation.
  - `tools/issues.py` — `update_issue` extended with
    `fixed_version_id` parameter (passes through to the API; empty
    string clears the assignment). 2 new tests cover the round-trip.
  - `assign_issue_to_version` is a thin convenience wrapper over
    `update_issue` that sets `fixed_version_id` (and accepts `0` as
    the unassign sentinel).
  - `server.py` — registered all 6 tools (5 mutating ones honor
    `REDMINE_MCP_READ_ONLY`).
- 26 new unit tests (24 in `tests/test_tools/test_versions.py` covering
  every tool's happy + validation + 404 paths, 2 in test_issues.py
  for the new `fixed_version_id` round-trip). 252 tests pass total;
  ruff clean.
- Live smoke verified end-to-end:
  full create → get → list → assign-issue → unassign → update → delete
  cycle on a transient `v02-smoke-*` version in the `claudecode` project
  (assignment must precede the open→locked status flip — locked
  versions reject new issue assignments).

### Added (continued)
- v0.2 sixth feature: bulk operations (Redmine
  #2381) — 2 new tools:
  `bulk_update_issues`, `bulk_close`. 31 of the planned set live total.
  - `tools/bulk.py` — thin orchestrators over
    `issues.update_issue` / `issues.close_issue` that validate input
    once (non-empty list, ≤ MAX_BATCH_SIZE=100, at least one updatable
    field on the update tool), iterate sequentially (Redmine has no
    batch endpoint), and aggregate results into
    `{total, succeeded, failed, skipped}`.
  - `stop_on_error=True` halts the batch on first failure and lands
    the unprocessed remainder in `skipped` so callers can retry just
    those; the default (False) is best-effort.
  - `server.py` — registered both tools (mutating, honor
    `REDMINE_MCP_READ_ONLY`).
- 13 new unit tests in `tests/test_tools/test_bulk.py` covering
  validation, success aggregation, mixed-failure aggregation,
  `stop_on_error` short-circuit, and batch-size cap. 226 tests pass total.
- Live smoke verified: a single-element
  `bulk_update_issues` posted a journal entry on test issue #2411,
  and `bulk_close([])` returned `validation_failed` as expected.

### Added (continued)
- v0.2 fifth feature: issue relations (Redmine
  #2380) — 4 new tools:
  `list_relations`, `add_relation`, `remove_relation`,
  `set_parent_issue`. 29 of the planned set live total.
  - `tools/relations.py` — `list_relations` GETs
    `/issues/{id}/relations.json`; `add_relation` POSTs to the same
    path with the new relation; `remove_relation` DELETEs at the
    top-level `/relations/{id}.json` URL (NOT nested under the
    parent issue — easy trap); `set_parent_issue` lives here for
    discoverability even though it's mechanically a PUT to
    `/issues/{id}.json` with `parent_issue_id` (parent/child is a
    field on the issue, not a relation record).
  - Relation-type aliasing: callers often think in colloquial terms
    (`related_to`, `blocked_by`, `duplicate_of`, `duplicated_by`,
    `copy_of`) while Redmine's enum is the source-side form
    (`relates`, `blocked`, `duplicated`, `duplicates`, `copied_from`).
    A small alias map normalizes before posting; unknown types fail
    fast client-side with a structured error listing the canonical
    set.
  - `set_parent_issue` accepts `parent_issue_id=0` as the unparent
    sentinel (sends empty string to Redmine, which is the API's
    "remove parent" form).
  - `server.py` — registered all 4 tools (3 mutating ones honor
    `REDMINE_MCP_READ_ONLY`).
- 15 new unit tests in `tests/test_tools/test_relations.py` covering
  list / add (with type normalization + delay) / remove / set_parent
  + cross-project 422 propagation. 213 tests pass total.
- Live smoke verified: added a
  `related_to` relation between test issue #2411 and #2412, listed
  it, then removed it.

### Added (continued)
- v0.2 fourth feature: wiki page CRUD (Redmine
  #2378) — 4 new tools:
  `get_page`, `create_page`, `update_page`, `delete_page`. 25 of the
  planned set live total.
  - `tools/wiki.py` — Redmine's wiki API uses PUT for both
    create-and-update (returning 201 vs 200 to distinguish). We split
    them at the tool boundary by adding a GET pre-flight to
    `create_page` that refuses to overwrite an existing page (returns
    `wiki_page_already_exists` with the current version so the caller
    can either back off or call `update_page`).
  - `update_page` accepts an optional `version` parameter for
    Redmine's optimistic-concurrency check; a stale version surfaces
    as the underlying `redmine_api_409`. Both `create_page` and
    `update_page` re-fetch after the PUT so the caller gets fresh
    metadata (Redmine usually returns 204 on the write itself).
  - Titles are URL-encoded with `urllib.parse.quote(safe="")` so
    spaces, slashes, and unicode all survive the path. The project
    segment passes through verbatim — Redmine accepts both numeric
    ids and slugs in the wiki URL routing layer.
  - `server.py` — registered all 4 tools (3 mutating ones honor
    `REDMINE_MCP_READ_ONLY`).
- 21 new unit tests in `tests/test_tools/test_wiki.py` covering each
  tool's happy path + 404 propagation + URL encoding + version-aware
  fetch + already-exists rejection + empty-text validation. 198
  tests pass total.
- Live smoke verified: full
  create → get → re-create-rejected → update → delete → 404-confirmed
  cycle on a transient page in the `claudecode` project.

### Added (continued)
- v0.2 third feature: watchers (Redmine
  #2379) — 3 new tools:
  `add_watcher`, `remove_watcher`, `list_watchers`. 21 of the planned
  set live total.
  - `tools/watchers.py` — `add_watcher` POSTs to
    `/issues/{id}/watchers.json` (idempotent on the API side);
    `remove_watcher` DELETEs `/issues/{id}/watchers/{user_id}.json`
    (404 surfaces verbatim so callers can distinguish "not a watcher"
    from "issue not found"); `list_watchers` reuses
    `issues.get_issue(include="watchers")` and lifts the `watchers`
    array to a top-level field (mirrors the `get_journals` pattern).
  - `server.py` — registered the 3 tools (mutating ones honor
    `REDMINE_MCP_READ_ONLY`).
- 7 new unit tests in `tests/test_tools/test_watchers.py` covering
  each tool's happy + 404-propagation paths. 177 tests pass total;
  ruff clean.
- Live smoke verified end-to-end:
  add+list+remove cycle on test issue #2411.

### Added (continued)
- v0.2 second feature: time-entry CRUD (Redmine
  #2377) — 4 new tools:
  `create_time_entry`, `list_time_entries`, `update_time_entry`,
  `delete_time_entry`. 18 of the 14+ tools live total.
  - `tools/time_entries.py` — create accepts ``H:MM`` or decimal hours
    formats (parsed to a single canonical float before round-trip);
    activity names resolve through the cached
    `time_entry_activities` enumeration; ``issue_id`` and
    ``project_id`` are mutually exclusive (issue wins). Update is
    partial — only supplied fields are sent — and re-validates any
    supplied hours. Delete returns `{"deleted": true}` and is
    permanent (no soft-delete in Redmine).
  - `validation/fields.py` — new `parse_hours()` and `validate_hours()`
    helpers. Accepted forms: numeric, decimal string (`"2.5"`), or
    `"H:MM"` (`"2:30"` → 2.5h). Negative values, `H >= 60`, malformed
    strings, and booleans are rejected with structured errors.
  - `errors.py` — new `TimeEntryHoursInvalid` payload class.
  - `schema/tracker.py` — `refresh_global_enumerations` now also
    fetches `time_entry_activities` (cached under that meta key, same
    24h TTL as the other enumerations).
  - `server.py` — registered the 4 tools; the 3 mutating ones honor
    `REDMINE_MCP_READ_ONLY`.
- 25 new unit tests: 12 in `tests/test_validation.py` covering
  `parse_hours` / `validate_hours` (valid + invalid + boundary forms),
  13 in `tests/test_tools/test_time_entries.py` covering each tool's
  happy + validation-failure + propagation paths. 170 tests pass total;
  ruff clean.
- Live smoke verified end-to-end:
  created time entry #1 against test issue #2411 with `H:MM` hours +
  resolved-by-name activity, listed it, rejected an invalid-hours
  update client-side, applied a valid decimal-hours update, then
  deleted the entry.

### Added (continued)
- v0.2 first tool: `redmine_download_attachment` (Redmine
  #2376).
  - `tools/attachments.py` — `download_attachment` runs Redmine's
    two-step download (GET `/attachments/{id}.json` for metadata, then
    GET `/attachments/download/{id}/{filename}` for the bytes). Validates
    downloaded byte count against the metadata's `filesize` BEFORE
    writing — short reads are surfaced as `attachment_size_mismatch`
    rather than silently saving a partial file.
  - New `_is_save_path_allowed()` helper distinguishes target-not-yet-
    existent from `_is_path_allowed`'s upload semantics: parent must
    exist + be under the allowlist, target may not exist (or
    `overwrite=True`). Symlinks are resolved on the parent before the
    allowlist comparison so a symlinked decoy parent that points
    outside the allowlist is rejected.
  - `client.py` — `_request` extended with `binary: bool = False`; new
    `get_binary()` method returns the raw response body (used for
    attachment fetches that aren't JSON).
  - `errors.py` — `AttachmentPathDenied` extended with two new reasons
    (`parent_missing`, `exists_no_overwrite`) for the save-side checks.
  - `server.py` — registered `redmine_download_attachment` (read-only;
    no `write=True` flag because nothing in Redmine changes).
- 13 unit tests added to `tests/test_tools/test_attachments.py`
  (6 cover `_is_save_path_allowed` directly including symlink-parent
  rejection; 7 cover the tool itself including size-mismatch and
  overwrite semantics). 136 tests pass total.
- Live smoke verified end-to-end:
  downloaded attachment id 8 (the file uploaded during Phase 5 smoke
  to test issue #2411), confirmed bytes match, confirmed the
  no-overwrite and outside-allowlist rejections fire with the right
  reason codes.

### Added (continued)
- Phase 5: Comments + attachments (3 new tools — all 13 v0.1 tools now live).
  - `tools/comments.py` — `add_comment` (direct PUT with `notes` /
    `private_notes`; rejects empty/whitespace notes client-side to avoid
    no-op `updated_on` bumps) and `get_journals` (read-only, lifts
    `journals` to a top-level field).
  - `tools/attachments.py` — `upload_attachment` (path-restricted via
    `Config.allowed_directories`, two-step Redmine flow: POST
    `/uploads.json` then optional PUT `/issues/{id}.json` with the
    uploads array). Path safety check resolves symlinks before the
    allowlist comparison so a symlink under `/tmp` pointing at
    `/etc/shadow` is rejected. On a successful upload + failed attach,
    the response carries the upload token so the caller can retry the
    attach without re-reading the bytes.
  - `client.py` — `_request` extended with `content` and `headers`
    parameters; new `post_binary()` method for the
    `Content-Type: application/octet-stream` body shape Redmine's
    `/uploads.json` requires.
  - `errors.py` — added `AttachmentPathDenied` payload class
    (distinguishes `outside_allowlist` vs `not_a_file` reasons).
  - `server.py` — registered the 3 new MCP tools. The attachment tool
    pulls `Config.allowed_directories` at registration time so the tool
    function doesn't need a config singleton import.
- 21 unit tests across `tests/test_tools/test_comments.py` (9) and
  `tests/test_tools/test_attachments.py` (12 — 5 cover the path-safety
  helper directly including symlink-escape rejection). 123 tests pass
  total; ruff clean.

### Added
- Phase 4: Issue lifecycle (5 new tools — 10 of the 13 v0.1 tools live).
  - `tools/issues.py` — `get_issue`, `create_issue`, `update_issue`,
    `close_issue`, `search_issues`. Each registered as an `@mcp.tool()`
    handler in `server.py` via the existing `_wrap()` helper.
  - `_wrap(..., write=True)` short-circuits write tools with
    `ReadOnlyModeError` when `REDMINE_MCP_READ_ONLY=true`.
  - `update_issue` is the marquee tool. On a status-changing call it:
    pre-flights against the cache (`is_disallowed` short-circuits with a
    `WorkflowTransitionDisallowed` payload populated by `allowed_next`),
    sends the PUT, and records the outcome — `allowed` on success;
    `disallowed` with the captured error text on a status-related 422.
    The cache learns the workflow graph with each call.
  - `close_issue` resolves the closed status from cached
    `issue_statuses` (`is_closed=true`, falls back to id 5) and
    repackages a workflow rejection with closure-specific framing.
  - `search_issues` accepts substring query (Redmine `subject=~`) plus
    optional project/status filters; passes through `"open"`/`"closed"`/
    `"*"` special tokens case-sensitively so a named status like
    `"Closed"` still resolves through the cache.
  - Helpers in `issues.py` resolve project/tracker/priority/status
    references against the cache, populating it on miss
    (`describe_project`, `fetch_all_trackers`,
    `refresh_global_enumerations`).
- 19 unit tests in `tests/test_tools/test_issues.py` covering each
  tool's happy path plus 1–2 validation-failure paths (FakeClient
  pattern, no network). 102 tests pass total.

### Changed
- `server.py` no longer uses `from __future__ import annotations`.
  FastMCP's `add_tool` introspects parameter annotations via
  `inspect.signature(...).parameters[...].annotation` and runs
  `issubclass(...)` on them; under PEP 563 the annotations are strings,
  which fails the check. Removing the future import lets every tool
  (Phases 1–4) register correctly. Local-variable annotations still
  use PEP 604 union syntax (Python 3.10+ native).

### Added
- Phase 3: Validation layer.
  - `validation/transitions.py` — `is_disallowed`, `allowed_next`, `has_any_observation` (cache-backed reactive lookups; aggregates across role ids; includes role `0` for global-admin observations).
  - `validation/fields.py` — `validate_required` (base required fields for create), `validate_custom_fields` (shape + optional id-allowlist).
  - `validation/permissions.py` — `is_admin`, `role_names_for_project`, `require_role` (admin bypass + named-role allowlist).
  - `errors.py` — added `WorkflowTransitionDisallowed`, `RequiredFieldMissing`, `CustomFieldUnknown`, `CustomFieldShapeError`, `RoleNotAuthorized` payload classes with structured `extra` fields and human-readable `hint`s.
- 24 unit tests (`test_validation.py`) covering all three validators + the error payload shapes.
- `pyproject.toml` per-file ruff override: N818 disabled in `errors.py` (the payload classes aren't Python exceptions).

### Added (continued)
- Phase 2: Schema fetchers + 4 new discovery tools.
  - `schema/tracker.py` — `fetch_all_trackers`, `describe_tracker` (enriches with global statuses + priorities + observed workflow graph).
  - `schema/project.py` — `describe_project` (cache-backed), `list_projects` (paginated, optional client-side substring filter).
  - `schema/workflow.py` — `fetch_current_user`, `role_ids_for_project`, `record_outcome` (writes per-role observations).
  - `tools/discovery.py` — added `describe_tracker`, `describe_project`, `list_projects`, `invalidate_cache`.
  - `server.py` — registered the 4 new MCP tools (now 5 of the 13 v0.1 tools live).
  - `cache/migrations.py` v2 — added `outcome`, `observation_count`, `last_error_text` columns to `workflow_transitions`; renamed `fetched_at` → `observed_at`.
  - `cache/schema_db.py` — added `get_tracker_by_name`, `resolve_tracker`, `get_meta_json`/`put_meta_json` (TTL'd JSON blobs in cache_meta), `record_workflow_observation`, `get_workflow_observation`, `list_workflow_observations`.

### Changed
- Workflow validation pivoted from **pre-flight** to **reactive observation** because Redmine's `/workflows.json` returns 403 even for global admins. See `docs/workflow-validation.md` for the new design — the cache learns the workflow graph by recording the outcome of every status-change attempt, surfaces it via `describe_tracker(include_observations=true)`, and short-circuits known-disallowed transitions on subsequent calls.

### Added (continued)
- 24 new unit tests across `test_cache.py` (10 added: meta_json, workflow observations, resolve_tracker) and `test_schema.py` (12 new: tracker/project/workflow fetchers via a FakeClient stand-in). 59 tests passing total.

- Phase 1: Core plumbing.
  - `secrets.py` — triple-pattern loader for `~/.claude/secrets.md` (`REDMINE_API_KEY=`, `redmine_api_key:`, `TROUBLE_API_KEY=`).
  - `config.py` — `Config` dataclass with `from_env()` factory; parses all `REDMINE_*` and `REDMINE_MCP_*` env vars; `require_api_key()` helper.
  - `errors.py` — `StructuredError`, `RedmineAPIError`, `ReadOnlyModeError`.
  - `client.py` — async `RedmineClient` (httpx-backed, retry on 5xx, `paginate()` async generator).
  - `cache/migrations.py` — schema-version-stamped DDL for 5 cache tables.
  - `cache/schema_db.py` — `SchemaCache` with TTL enforcement, auth-fingerprint reconciliation, `invalidate(scope=...)`.
  - `tools/discovery.py` — `redmine_list_trackers` (smoke-test entry).
  - `server.py` — FastMCP entrypoint, registered with the user-scoped MCP config.
- 35 unit tests across `test_secrets.py`, `test_config.py`, `test_cache.py` (all passing).
- End-to-end smoke test: `claude mcp list` reports the server connected; `redmine_list_trackers` round-trips against `http://127.0.0.1:8281`.
- Phase 0: Project scaffolding, MIT license, README, pyproject.toml.
- Empty module skeleton for `src/redmine_mcp/` with package boundaries (cache, schema, validation, tools).
