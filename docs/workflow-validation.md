# Workflow validation

The killer feature — but with a twist that took us by surprise.

## The problem

Redmine's `/issues/{id}.json` PUT endpoint enforces workflow restrictions
server-side: a user with role X can only move issue from status A to status B
if the workflow row `(tracker_id, X, A, B)` exists. When the transition is
disallowed, you get an HTTP 422 with a body like:

```json
{ "errors": ["Status is not allowed"] }
```

That's not enough information for an LLM to retry productively. Did the
status not exist? Was it a permission issue? What status *would* be allowed?

## What we wish we could do

Pre-flight every transition. Cache the workflow graph on first describe,
look up `(tracker_id, role_id, from, to)` locally, and short-circuit any
disallowed transition with a structured error before it leaves the client.

## What Redmine actually allows

`/workflows.json` returns **HTTP 403 Forbidden** even for global admins — the
workflow editor is web-UI-only. There's no REST endpoint for "what
transitions does role R allow on tracker T?" Confirmed against Redmine 6.x
on 2026-05-04.

We weighed several alternatives — admin web-UI scraping, a Redmine Ruby
plugin, an env-var-driven manual override file — and settled on a different
shape entirely.

## What we do instead: reactive workflow validation

The cache learns the workflow graph by observation. Every `update_issue`
that mutates `status_id` records the outcome:

* **Success → record `(tracker_id, role_id, from, to)` as known-allowed.**
* **422 with `errors=["Status is not allowed"]` → record as known-disallowed,
  along with the error text and timestamp.**

After a few sessions of normal use, the cache holds a confidence-weighted
view of every transition that's been attempted. We expose that view via
`redmine_describe_tracker(tracker, include_observations=true)`, which
returns:

```json
{
  "tracker": "Bug",
  "id": 1,
  "default_status": {"id": 1, "name": "New"},
  "available_statuses": [...],
  "observed_workflow": {
    "Developer": {
      "New": {"allowed_next": ["In Progress"], "disallowed_next": ["Closed"]},
      "In Progress": {"allowed_next": ["Resolved"], "disallowed_next": []}
    }
  },
  "observation_note": "Workflow knowledge is learned from observed API responses, not fetched authoritatively (Redmine does not expose /workflows via REST)."
}
```

When `redmine_update_issue` runs the validation hook:

1. Look up the proposed `(tracker, role, from, to)` in observed_workflow.
2. If marked disallowed → return a structured error *before* sending the PUT.
3. If marked allowed → send the PUT. (If it 422s anyway, that's a rule
   change since last observation; record the new state and surface the error.)
4. If unknown → send the PUT, observe the outcome, record it, and surface
   the right structured response either way.

Net effect: the *first* time anyone hits a disallowed transition for a
given role, they get the raw 422 wrapped in a structured error. *Every
subsequent* attempt of that same transition gets the pre-flight rejection
with `allowed_next_states` populated from the cache.

## What we can pre-flight authoritatively

Some validation works fine without the workflow graph. The cache pulls
these on first describe:

* **Status exists at all** — via `/issue_statuses.json`. Reject typo'd
  status names client-side.
* **Priority exists** — via `/enumerations/issue_priorities.json`.
* **Tracker is enabled on project** — via
  `/projects/{slug}.json?include=trackers`.
* **Required-but-empty fields** — basic check on subject, project, tracker.
  Custom-field requiredness varies per tracker and isn't queryable, so we
  defer to the reactive layer there too.

## Cache schema

See [`cache/migrations.py`](../src/redmine_mcp/cache/migrations.py). The
relevant table is `workflow_transitions`, which stores observations rather
than authoritative facts:

```
workflow_transitions(
  tracker_id, role_id, from_status_id, to_status_id,
  outcome,             -- 'allowed' | 'disallowed'
  observed_at,         -- last seen
  observation_count    -- how many times we've seen this outcome
)
```

The structured error response on a disallowed transition:

```json
{
  "error": "workflow_transition_disallowed",
  "tracker": "Bug",
  "from_status": "New",
  "to_status": "Closed",
  "user_role": "Developer",
  "hint": "Last observed 2026-05-04. Try one of: In Progress.",
  "allowed_next_states": ["In Progress"],
  "observation_basis": "learned"
}
```

## Cache invalidation

Three triggers:
1. Per-entry TTL (default 24h for schema, longer for workflow observations
   since they change rarely).
2. Explicit `redmine_invalidate_cache(scope=...)` tool call.
3. Auto-detect API key change (SHA256 of key stored in `cache_meta`).

A workflow rule that *was* allowed and silently stops working will surface
on the next attempt: the cached observation says "allowed", we send the
PUT, Redmine returns 422, we update the cache, the LLM gets the new
restriction in its next call.
