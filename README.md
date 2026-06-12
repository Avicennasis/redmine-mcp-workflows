# redmine-mcp-workflows

A schema-aware Model Context Protocol (MCP) server for Redmine.

Most MCP servers for Redmine are thin wrappers around the REST API: an LLM call goes through, Redmine returns `422 Unprocessable Entity`, and the LLM has to guess what went wrong. `redmine-mcp-workflows` is different — it caches each tracker's workflow graph, custom field schemas, and role-based field permissions, then **validates writes before they leave the client**. When something can't succeed, you get a structured error with a clear next step instead of a stack trace.

## What you get

**v0.1 (13 tools) + v0.2 quartet shipped + v0.3 escape hatch + v0.5 news/forums pair — 40 tools total live, 277 tests, ruff clean.**

### Discovery & introspection (4)
- `redmine_describe_tracker(tracker)` — required fields, allowed status transitions per role, custom field schemas. Cache-backed.
- `redmine_describe_project(project)` — enabled trackers, custom fields, members, default tracker.
- `redmine_list_projects(query=None, limit=25, offset=0)` — paginated.
- `redmine_list_trackers()` — all trackers + cache.

### Cache control (1)
- `redmine_invalidate_cache(scope)` — `"all"`, `"tracker:<id>"`, or `"project:<slug>"`.

### Issue lifecycle (5) — every write validates against the cache
- `redmine_create_issue(project, tracker, subject, ..., difficulty="")`
- `redmine_get_issue(id, include=...)`
- `redmine_update_issue(id, ..., difficulty="", **fields)` — validates status transitions, custom fields, role permissions.
- `redmine_close_issue(id, note=None)` — convenience over `update_issue` with closure-specific error messaging.
- `redmine_search_issues(query, project=None, status=None, ...)`

#### Difficulty (engagement-mode signal, v0.4)

If your fleet has a global `Difficulty` custom field configured (values
`Unclassified` / `Easy` / `Normal` / `Hard`), the `difficulty` parameter
on `redmine_create_issue` and `redmine_update_issue` is the convenience
path: pass `difficulty="Easy"` instead of constructing
`custom_fields=[{"id": <N>, "value": "Easy"}]` manually. The field id is
resolved via `/custom_fields.json` and cached.

- **Create** default-fills `Unclassified` when `difficulty` is omitted
  and no `Difficulty` entry is supplied in `custom_fields` — so
  auto-callers don't trip the required-field validation.
- **Update** does NOT default-fill (would silently overwrite user-set
  values).
- If both `difficulty=...` and a matching `custom_fields` entry are
  passed, `difficulty` wins.
- On fleets without a Difficulty field, the parameter silently no-ops.

### Comments & attachments (4)
- `redmine_add_comment(id, note, private=False)`
- `redmine_get_journals(id)`
- `redmine_upload_attachment(file_path, issue_id=None, description=None)` — path-restricted to `REDMINE_MCP_ALLOWED_DIRECTORIES`.
- `redmine_download_attachment(attachment_id, save_to, overwrite=False)` — validates downloaded byte count against metadata `filesize` before writing (no partial-file corruption).

### Time tracking (4) — v0.2
- `redmine_create_time_entry(hours, issue_id=0, project_id=0, activity="", ...)` — accepts `H:MM` or decimal hours; activity names resolve through the cached enumeration.
- `redmine_list_time_entries(issue_id=0, project_id=0, user_id=0, spent_on="", from_date="", to_date="", ...)`
- `redmine_update_time_entry(id, hours="", activity="", ...)` — partial; re-validates supplied hours.
- `redmine_delete_time_entry(id)` — permanent.

### Watchers (3) — v0.2
- `redmine_add_watcher(issue_id, user_id)` — idempotent on the API side.
- `redmine_remove_watcher(issue_id, user_id)` — 404 surfaces verbatim so callers can distinguish "not a watcher" from "issue not found".
- `redmine_list_watchers(issue_id)`

### Wiki page CRUD (4) — v0.2
- `redmine_get_wiki_page(project, title, version=0)` — optional historical version.
- `redmine_create_wiki_page(project, title, text, parent_title="", comments="")` — pre-flight GET refuses to overwrite.
- `redmine_update_wiki_page(project, title, text, version=0, ...)` — optional `version` for optimistic concurrency (mismatch → `redmine_api_409`).
- `redmine_delete_wiki_page(project, title)` — permanent.

### Issue relations (4) — v0.2
- `redmine_list_relations(issue_id)`
- `redmine_add_relation(issue_id, target_issue_id, relation_type, delay=0)` — accepts colloquial `related_to`/`blocked_by`/`duplicate_of` aliases.
- `redmine_remove_relation(relation_id)` — DELETEs `/relations/{id}.json` (top-level URL).
- `redmine_set_parent_issue(issue_id, parent_issue_id)` — pass `0` to unparent.

### Bulk operations (2) — v0.2
- `redmine_bulk_update_issues(issue_ids, ...)` — apply same field updates across many issues. ≤100 per batch.
- `redmine_bulk_close(issue_ids, note="")` — close many at once. Returns `{total, succeeded, failed, skipped}`. `stop_on_error=True` halts on first failure.

### Versions / milestones (6) — v0.2
- `redmine_list_versions(project)`
- `redmine_get_version(version_id)`
- `redmine_create_version(project, name, status="open"|"locked"|"closed", sharing="none"|"descendants"|...)` — enums validated client-side.
- `redmine_update_version(version_id, ...)` — partial updates.
- `redmine_delete_version(version_id)` — 422 if issues still reference it.
- `redmine_assign_issue_to_version(issue_id, version_id)` — pass `version_id=0` to clear.

### Generic passthrough (1) — v0.3, opt-in
- `redmine_request(method, path, body="", params="")` — escape hatch to any Redmine REST endpoint. Gated behind `REDMINE_MCP_ENABLE_PASSTHROUGH=true`. Every response carries `validation_skipped: true` plus a `warning` field. NO validation, NO workflow check, NO schema cache. Use when no validated tool covers your use case; prefer the typed tools whenever possible.

### News & forums (2) — v0.5
- `redmine_list_news(project="", limit=25, offset=0)` — paginated news feed. Empty `project` for the global feed; pass an id or slug for the per-project feed at `/projects/{id}/news.json`.
- `redmine_list_messages(board_id, limit=25, offset=0)` — forum messages on a specific board. A 404 typically means the boards module isn't enabled on the parent project.

## Install

```bash
pip install redmine-mcp-workflows
```

Or for development:

```bash
git clone https://github.com/avicennasis/redmine-mcp-workflows
cd redmine-mcp-workflows
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
```

## Register with Claude Code

```bash
claude mcp add-json -s user redmine '{
  "command": "/path/to/.venv/bin/redmine-mcp",
  "env": {
    "REDMINE_URL": "https://redmine.example.com",
    "REDMINE_API_KEY": "your-api-key"
  }
}'
```

Or for Claude Desktop, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "redmine": {
      "command": "/path/to/.venv/bin/redmine-mcp",
      "env": {
        "REDMINE_URL": "https://redmine.example.com",
        "REDMINE_API_KEY": "your-api-key"
      }
    }
  }
}
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `REDMINE_URL` | `http://127.0.0.1:8281` | Redmine base URL. The default loopback is convenient for setups behind a reverse proxy that filters `X-Redmine-API-Key`. |
| `REDMINE_API_KEY` | _(none)_ | API key (set in `~/.bash_secrets`). |
| `REDMINE_OAUTH_TOKEN` | _(none)_ | OAuth2 bearer token (Doorkeeper, Redmine 6.1+). When set, sent as `Authorization: Bearer <token>` and **takes precedence over the API key**. |
| `REDMINE_MCP_READ_ONLY` | `false` | Set `true` to disable write tools. |
| `REDMINE_MCP_CACHE_DIR` | platform user-cache dir | Override for the SQLite cache location. |
| `REDMINE_MCP_CACHE_TTL` | `86400` (24h) | Per-entry TTL in seconds. |
| `REDMINE_HEADERS` | _(empty)_ | Extra HTTP headers, comma-separated (e.g., `Authorization: Bearer ...`). |
| `REDMINE_MCP_ALLOWED_DIRECTORIES` | `/tmp` | Comma-separated paths that `redmine_upload_attachment` may read from. |
| `REDMINE_MCP_LOG_LEVEL` | `INFO` | stdlib logging level. |

## Text formatting in notes

Redmine renders issue descriptions and journal notes using a text formatter — **Markdown** on most modern instances (including `redmine.simmons.systems`). This means `**bold**`, `## headings`, `| col | col |` tables, and `- list items` all render correctly in note text.

**Important for MCP callers:** when composing multi-line notes, the `note` parameter must contain actual newline characters in the JSON string value. Do **not** use literal two-character `\n` escape sequences — they get double-escaped and stored as visible `\n` text instead of line breaks. If your notes look like a single paragraph with visible `\n` markers, this is the cause.

Correct (actual newlines in the JSON string):
```json
{"note": "## Summary\n\nDone:\n- Fixed the auth bug\n- Updated tests"}
```

Wrong (literal backslash-n sequences — renders as one line with visible \n):
```
note = "## Summary\\nDone:\\n- Fixed the auth bug\\n- Updated tests"
```

## How workflow validation works

When Claude calls `redmine_update_issue(id=42, status="Closed")`:

1. Server fetches issue 42's current state.
2. Looks up the workflow row `(tracker_id, current_role, current_status, "Closed")` in the schema cache.
3. If the transition isn't allowed, returns:

   ```json
   {
     "error": "workflow_transition_disallowed",
     "tracker": "Bug",
     "from_status": "In Progress",
     "to_status": "Closed",
     "user_role": "Developer",
     "hint": "Workflow requires status to pass through 'Resolved' first.",
     "allowed_next_states": ["Resolved", "Reopened"]
   }
   ```

4. Otherwise sends the PUT.

Same pattern applies to custom fields (rejects unknown fields and regex/format violations) and required fields (rejects creates that miss required-and-empty fields).

## Differences from other Redmine MCP servers

There are several others — most notably [jztan/redmine-mcp-server](https://github.com/jztan/redmine-mcp-server) (55 tools, comprehensive, no validation), [@onozaty/redmine-mcp-server](https://www.npmjs.com/package/@onozaty/redmine-mcp-server) (TypeScript, Zod schemas), [runekaagaard/mcp-redmine](https://github.com/runekaagaard/mcp-redmine) (generic OpenAPI passthrough). All are good projects.

`redmine-mcp-workflows` differs by **validating before sending**, with a smaller, opinionated tool surface that prefers helpful errors over breadth. See [docs/differences.md](docs/differences.md) for a full comparison.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, testing conventions, and PR flow.
