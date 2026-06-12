# Differences from jztan/redmine-mcp-server

[jztan/redmine-mcp-server](https://github.com/jztan/redmine-mcp-server) is the
most comprehensive Redmine MCP server in the wild — 55 tools, prompt-injection
protection, read-only mode, RedmineUP Checklists support. It's a great
reference for breadth.

`redmine-mcp-workflows` makes a different trade. We optimize for **the LLM getting
useful errors**, not for tool coverage. The headline difference: every write
tool consults a per-tracker schema cache before round-tripping the API, so an
invalid status transition or unknown custom field is caught and explained
client-side.

| | jztan | this server |
|---|---|---|
| Tool count | 55 | 13 (v0.1), targeting ~30 by v1.0 |
| Workflow validation | ✗ | ✓ — validates status transitions per tracker per role |
| Custom field validation | partial | ✓ — rejects unknown / regex-violating values pre-flight |
| Required field validation | ✗ | ✓ — rejects creates that omit required-and-empty fields |
| Schema cache (SQLite) | ✗ | ✓ — lazy populate, 24h TTL, manual invalidation |
| Read-only mode | ✓ | ✓ — `REDMINE_MCP_READ_ONLY=true` |
| Prompt injection protection | ✓ | planned for v0.2 |
| Generic API passthrough | partial | deferred to v0.3 (would bypass validation by design) |
| Time entries / wiki / watchers | ✓ | deferred to v0.2 |
| RedmineUP Checklists | ✓ | deferred to v0.4 |
| OAuth2 | ✓ (Redmine 6.1+) | deferred to v0.3 |

Pick jztan if you need breadth right now and don't mind generic 422 errors when
your transition is disallowed. Pick this server if you'd rather have Claude
self-correct from structured errors instead of guessing.

(Phase 6 will expand this doc with concrete examples once the validation layer
is in place.)
