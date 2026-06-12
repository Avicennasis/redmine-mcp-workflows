# Roadmap

The v0.1 surface is intentionally small (13 tools). Everything else is tracked
as a Redmine ticket in the `claudecode` project so it doesn't get lost.

## v0.2 — feature breadth

| Ticket | Priority | What |
|---|---|---|
| [#2376](https://trouble.simmons.systems/issues/2376) | Normal | `redmine_download_attachment` tool |
| [#2377](https://trouble.simmons.systems/issues/2377) | Normal | Time entries (create/list/update/delete) |
| [#2378](https://trouble.simmons.systems/issues/2378) | Normal | Wiki page CRUD |
| [#2379](https://trouble.simmons.systems/issues/2379) | Normal | Watchers (add/remove/list) |
| [#2380](https://trouble.simmons.systems/issues/2380) | Normal | Relations (parent/child, blocks, related-to) |
| [#2381](https://trouble.simmons.systems/issues/2381) | Normal | Bulk operations (`bulk_update_issues`, `bulk_close`) |
| [#2382](https://trouble.simmons.systems/issues/2382) | Low | Versions / milestones CRUD + assign-issues-to-version |

## v0.3 — auth & escape hatches

| Ticket | Priority | What |
|---|---|---|
| [#2383](https://trouble.simmons.systems/issues/2383) | Normal | OAuth2 support (Doorkeeper, Redmine 6.1+) |
| [#2384](https://trouble.simmons.systems/issues/2384) | Low | `redmine_request` generic-passthrough tool (with skip-validation warnings) |

## v0.4 — plugin support

| Ticket | Priority | What |
|---|---|---|
| [#2385](https://trouble.simmons.systems/issues/2385) | Low | RedmineUP Checklists |
| [#2386](https://trouble.simmons.systems/issues/2386) | Low | RedmineUP Agile (sprint/board ops) |
| [#2387](https://trouble.simmons.systems/issues/2387) | Low | additionals (extended custom fields) |
| [#2388](https://trouble.simmons.systems/issues/2388) | Low | redmine_oauth (auth pass-through) |

## v0.5 — niche surfaces

| Ticket | Priority | What |
|---|---|---|
| [#2389](https://trouble.simmons.systems/issues/2389) | Low | User/group admin tools (admin-API-only) |
| [#2390](https://trouble.simmons.systems/issues/2390) | Low | News / forums tools |

## v1.0 — public release

| Ticket | Priority | What |
|---|---|---|
| [#2391](https://trouble.simmons.systems/issues/2391) | **High** | Extract to standalone repo + PyPI release |
| [#2392](https://trouble.simmons.systems/issues/2392) | Normal | GitHub Actions release workflow + version-tag → PyPI publish |
| [#2393](https://trouble.simmons.systems/issues/2393) | Normal | Project/issue templates, CONTRIBUTING.md, CODE_OF_CONDUCT.md |

## Out of scope (no ticket — not building)

- HTTP/SSE transport (stdio-only)
- Multi-tenant / multi-Redmine in a single server instance
- i18n / non-English error messages
- Rate-limiting / circuit-breaking against upstream Redmine
