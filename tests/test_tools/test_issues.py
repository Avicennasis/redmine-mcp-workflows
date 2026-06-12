"""Unit tests for the Phase 4 issue-lifecycle tools.

Uses a stand-in :class:`FakeClient` (no network) and a real :class:`SchemaCache`
to exercise both the validation pre-flight and the post-API observation
recording paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import issues


class FakeClient:
    """Minimal stand-in for :class:`RedmineClient`.

    Returns canned responses keyed by ``(method, path)``. Pass an
    ``errors`` mapping to make particular calls raise
    :class:`RedmineAPIError` instead.

    A response value may be a single payload (static — every call returns
    it) or a list of payloads (queue — each call pops the next; the last
    element is reused if the list runs out). This lets tests script
    pre-PUT vs post-PUT GETs that need to differ (e.g. the issue's status
    changing across the update_issue flow).
    """

    def __init__(
        self,
        responses: dict[tuple[str, str], Any] | None = None,
        *,
        errors: dict[tuple[str, str], RedmineAPIError] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, str, Any]] = []
        self._consumed: dict[tuple[str, str], int] = {}

    def _next_response(self, key: tuple[str, str]) -> Any:
        value = self._responses.get(key)
        if isinstance(value, list):
            idx = self._consumed.get(key, 0)
            chosen = value[idx] if idx < len(value) else value[-1]
            self._consumed[key] = idx + 1
            return chosen
        return value

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if ("GET", path) in self._errors:
            raise self._errors[("GET", path)]
        return self._next_response(("GET", path))

    async def post(self, path: str, *, json: Any) -> Any:
        self.calls.append(("POST", path, json))
        if ("POST", path) in self._errors:
            raise self._errors[("POST", path)]
        return self._responses.get(("POST", path))

    async def put(self, path: str, *, json: Any) -> Any:
        self.calls.append(("PUT", path, json))
        if ("PUT", path) in self._errors:
            raise self._errors[("PUT", path)]
        return self._responses.get(("PUT", path))


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


def _seed_enums(cache: SchemaCache) -> None:
    """Seed cache_meta with statuses, priorities, and roles for tests."""
    cache.put_meta_json(
        "issue_statuses",
        [
            {"id": 1, "name": "New", "is_closed": False},
            {"id": 2, "name": "In Progress", "is_closed": False},
            {"id": 3, "name": "Resolved", "is_closed": False},
            {"id": 5, "name": "Closed", "is_closed": True},
            {"id": 6, "name": "Rejected", "is_closed": True},
        ],
    )
    cache.put_meta_json(
        "issue_priorities",
        [
            {"id": 1, "name": "Low"},
            {"id": 2, "name": "Normal"},
            {"id": 3, "name": "High"},
            {"id": 4, "name": "Urgent"},
        ],
    )
    cache.put_meta_json("roles", [{"id": 4, "name": "Developer"}])


def _seed_tracker_and_project(cache: SchemaCache) -> None:
    cache.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
    cache.put_project(
        15,
        "claudecode",
        {"id": 15, "identifier": "claudecode", "name": "ClaudeCode"},
    )


# ---------------------------------------------------------------------
# get_issue
# ---------------------------------------------------------------------


async def test_get_issue_returns_issue_dict(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues/42.json"): {"issue": {"id": 42, "subject": "Hello"}},
    })
    result = await issues.get_issue(client, cache, 42)
    assert result["issue"]["id"] == 42
    assert result["source"] == "api"
    assert client.calls == [
        ("GET", "/issues/42.json", {"include": issues.DEFAULT_INCLUDE}),
    ]


async def test_get_issue_returns_not_found_when_payload_empty(cache: SchemaCache) -> None:
    client = FakeClient({("GET", "/issues/99.json"): {"issue": None}})
    result = await issues.get_issue(client, cache, 99)
    assert result["error"] == "issue_not_found"
    assert result["issue_id"] == 99


# ---------------------------------------------------------------------
# create_issue
# ---------------------------------------------------------------------


async def test_create_issue_happy_path_resolves_names(cache: SchemaCache) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("POST", "/issues.json"): {"issue": {"id": 100, "subject": "A bug"}},
    })

    result = await issues.create_issue(
        client,
        cache,
        project="claudecode",
        tracker="Bug",
        subject="A bug",
        priority="High",
    )
    assert result["issue"]["id"] == 100
    # POST payload should carry resolved numeric ids.
    posted = client.calls[-1][2]["issue"]
    assert posted["project_id"] == 15
    assert posted["tracker_id"] == 1
    assert posted["priority_id"] == 3
    assert posted["subject"] == "A bug"


async def test_create_issue_returns_validation_errors_when_subject_blank(
    cache: SchemaCache,
) -> None:
    _seed_tracker_and_project(cache)
    client = FakeClient()
    result = await issues.create_issue(
        client, cache, project="claudecode", tracker="Bug", subject="   "
    )
    assert result["error"] == "validation_failed"
    assert any(e["error"] == "required_field_missing" for e in result["errors"])
    # Validation must short-circuit before any HTTP call.
    assert client.calls == []


async def test_create_issue_returns_project_not_found_when_unresolvable(
    cache: SchemaCache,
) -> None:
    _seed_enums(cache)
    cache.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
    # No project seeded; Redmine 404s the slug AND the project list has no
    # name match — exercise the realistic real-API path (404, not 200/null).
    client = FakeClient(
        responses={
            ("GET", "/projects.json"): {"projects": [], "total_count": 0},
        },
        errors={
            ("GET", "/projects/ghost.json"): RedmineAPIError(
                status_code=404, body=""
            ),
        },
    )
    result = await issues.create_issue(
        client, cache, project="ghost", tracker="Bug", subject="Subject"
    )
    assert result["error"] == "project_not_found"


async def test_create_issue_resolves_project_by_display_name_via_cache(
    cache: SchemaCache,
) -> None:
    """Regression for #2568: passing ``project.name`` (display, e.g. "ClaudeCode")
    instead of the slug ("claudecode") should still resolve via the cache."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)  # seeds project id=15, identifier=claudecode, name=ClaudeCode
    client = FakeClient(
        responses={
            ("POST", "/issues.json"): {"issue": {"id": 101, "subject": "X"}},
        },
        errors={
            # The slug-as-identifier lookup against Redmine returns 404 for "ClaudeCode"
            # (not the lowercase canonical slug). Caller still expects success
            # via the new name-fallback path.
            ("GET", "/projects/ClaudeCode.json"): RedmineAPIError(
                status_code=404, body=""
            ),
        },
    )
    result = await issues.create_issue(
        client,
        cache,
        project="ClaudeCode",  # display name, not slug
        tracker="Bug",
        subject="X",
    )
    assert result.get("issue", {}).get("id") == 101
    posted = client.calls[-1][2]["issue"]
    assert posted["project_id"] == 15  # resolved from name


async def test_create_issue_resolves_project_by_name_via_list_refresh(
    cache: SchemaCache,
) -> None:
    """When the display-name match isn't in cache yet, fall back to
    /projects.json listing and resolve from there."""
    _seed_enums(cache)
    cache.put_tracker(1, "Bug", {"id": 1, "name": "Bug"})
    # Project NOT seeded in cache. /projects/Infra.json returns 404. /projects.json
    # listing carries it, with id=4 / identifier=infra / name=Infra.
    client = FakeClient(
        responses={
            ("GET", "/projects.json"): {
                "projects": [
                    {"id": 4, "identifier": "infra", "name": "Infra"},
                ],
                "total_count": 1,
            },
            ("POST", "/issues.json"): {"issue": {"id": 202, "subject": "Y"}},
        },
        errors={
            ("GET", "/projects/Infra.json"): RedmineAPIError(
                status_code=404, body=""
            ),
        },
    )
    result = await issues.create_issue(
        client,
        cache,
        project="Infra",
        tracker="Bug",
        subject="Y",
    )
    assert result.get("issue", {}).get("id") == 202
    posted = client.calls[-1][2]["issue"]
    assert posted["project_id"] == 4
    # Cache was warmed by the resolution so a subsequent call is cheap.
    assert cache.get_project_by_name("Infra") is not None


async def test_create_issue_propagates_redmine_api_error(cache: SchemaCache) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient(
        errors={("POST", "/issues.json"): RedmineAPIError(
            status_code=422, body={"errors": ["Subject can't be blank"]}
        )},
    )
    result = await issues.create_issue(
        client, cache, project="claudecode", tracker="Bug", subject="Subject"
    )
    assert result["error"] == "redmine_api_422"
    assert result["status_code"] == 422


# ---- difficulty convenience parameter -----------------------------------


def _seed_difficulty_field(cache: SchemaCache, *, field_id: int = 1) -> None:
    """Pre-populate the cache with the Difficulty custom field record."""
    cache.put_custom_field(
        field_id=field_id, name="Difficulty", format_kind="list",
        is_required=True, default_value="Unclassified",
        possible_values=["Unclassified", "Easy", "Normal", "Hard"],
        applicable_tracker_ids=[],  # global → matches all trackers
        for_all_projects=True,
    )


async def test_create_issue_difficulty_easy_translates_to_custom_fields(
    cache: SchemaCache,
) -> None:
    """Passing difficulty='Easy' should add a custom_fields entry with id=1."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    _seed_difficulty_field(cache, field_id=1)
    client = FakeClient({
        ("POST", "/issues.json"): {"issue": {"id": 200, "subject": "Easy bug"}},
    })

    result = await issues.create_issue(
        client, cache,
        project="claudecode", tracker="Bug", subject="Easy bug",
        difficulty="Easy",
    )
    assert result["issue"]["id"] == 200
    posted = client.calls[-1][2]["issue"]
    cfs = posted["custom_fields"]
    assert cfs == [{"id": 1, "value": "Easy"}]


async def test_create_issue_default_fills_unclassified(cache: SchemaCache) -> None:
    """No difficulty arg, no Difficulty entry in custom_fields → default-fill Unclassified."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    _seed_difficulty_field(cache, field_id=1)
    client = FakeClient({
        ("POST", "/issues.json"): {"issue": {"id": 201, "subject": "X"}},
    })

    result = await issues.create_issue(
        client, cache, project="claudecode", tracker="Bug", subject="X",
    )
    assert result["issue"]["id"] == 201
    posted = client.calls[-1][2]["issue"]
    cfs = posted["custom_fields"]
    assert cfs == [{"id": 1, "value": "Unclassified"}]


async def test_create_issue_explicit_custom_fields_skip_default_fill(
    cache: SchemaCache,
) -> None:
    """If the caller passes Difficulty via custom_fields, default-fill should NOT fire."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    _seed_difficulty_field(cache, field_id=1)
    client = FakeClient({
        ("POST", "/issues.json"): {"issue": {"id": 202, "subject": "Y"}},
    })

    result = await issues.create_issue(
        client, cache, project="claudecode", tracker="Bug", subject="Y",
        custom_fields=[{"id": 1, "name": "Difficulty", "value": "Hard"}],
    )
    assert result["issue"]["id"] == 202
    posted = client.calls[-1][2]["issue"]
    cfs = posted["custom_fields"]
    # Single Difficulty entry preserved as-is, not overwritten.
    diff_entries = [c for c in cfs if c.get("name") == "Difficulty" or c.get("id") == 1]
    assert len(diff_entries) == 1
    assert diff_entries[0]["value"] == "Hard"


async def test_create_issue_difficulty_overrides_custom_fields_entry(
    cache: SchemaCache,
) -> None:
    """If both difficulty=... and a Difficulty entry in custom_fields are passed,
    the difficulty parameter wins."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    _seed_difficulty_field(cache, field_id=1)
    client = FakeClient({
        ("POST", "/issues.json"): {"issue": {"id": 203, "subject": "Z"}},
    })

    result = await issues.create_issue(
        client, cache, project="claudecode", tracker="Bug", subject="Z",
        custom_fields=[{"id": 1, "value": "Easy"}],
        difficulty="Hard",
    )
    assert result["issue"]["id"] == 203
    posted = client.calls[-1][2]["issue"]
    cfs = posted["custom_fields"]
    assert cfs == [{"id": 1, "value": "Hard"}]


async def test_create_issue_difficulty_lazy_loads_from_api(cache: SchemaCache) -> None:
    """If the Difficulty field isn't cached, get_custom_field_by_name should
    refresh from /custom_fields.json."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    # NOTE: no _seed_difficulty_field() — the cache is empty for custom fields.
    client = FakeClient({
        ("GET", "/custom_fields.json"): {"custom_fields": [
            {
                "id": 1, "name": "Difficulty", "customized_type": "issue",
                "field_format": "list", "is_required": True,
                "default_value": "Unclassified",
                "possible_values": [{"value": "Unclassified", "label": "Unclassified"}],
                "trackers": [],
            },
        ]},
        ("POST", "/issues.json"): {"issue": {"id": 204, "subject": "Lazy"}},
    })

    result = await issues.create_issue(
        client, cache, project="claudecode", tracker="Bug", subject="Lazy",
        difficulty="Easy",
    )
    assert result["issue"]["id"] == 204
    posted = client.calls[-1][2]["issue"]
    assert posted["custom_fields"] == [{"id": 1, "value": "Easy"}]


async def test_create_issue_no_default_fill_when_field_unknown(cache: SchemaCache) -> None:
    """If no Difficulty field exists in Redmine (e.g. legacy fleet),
    create still works — no custom_fields added, no error."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/custom_fields.json"): {"custom_fields": []},  # no Difficulty
        ("POST", "/issues.json"): {"issue": {"id": 205, "subject": "No-field"}},
    })

    result = await issues.create_issue(
        client, cache, project="claudecode", tracker="Bug", subject="No-field",
    )
    assert result["issue"]["id"] == 205
    posted = client.calls[-1][2]["issue"]
    assert "custom_fields" not in posted


# ---------------------------------------------------------------------
# update_issue
# ---------------------------------------------------------------------


def _issue_payload(
    issue_id: int = 42,
    *,
    status_id: int = 1,
    status_name: str = "New",
) -> dict[str, Any]:
    return {
        "issue": {
            "id": issue_id,
            "subject": "Demo",
            "tracker": {"id": 1, "name": "Bug"},
            "project": {"id": 15, "identifier": "claudecode"},
            "status": {"id": status_id, "name": status_name},
        }
    }


def _issue_payload_held(
    issue_id: int = 42,
    *,
    status_id: int = 1,
    status_name: str = "New",
    held: str = "",
    held_until: str | None = None,
) -> dict[str, Any]:
    custom_fields = [{"id": 1, "name": "Difficulty", "value": "Normal"}]
    if held:
        custom_fields.append({"id": 2, "name": "Held", "value": held})
    if held_until is not None:
        custom_fields.append({"id": 3, "name": "Held Until", "value": held_until})
    return {
        "issue": {
            "id": issue_id,
            "subject": "Demo",
            "tracker": {"id": 1, "name": "Bug"},
            "project": {"id": 15, "identifier": "claudecode"},
            "status": {"id": status_id, "name": status_name},
            "custom_fields": custom_fields,
        }
    }


async def test_update_issue_no_status_change_does_not_record_observation(
    cache: SchemaCache,
) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    fetched = _issue_payload()
    client = FakeClient({
        ("GET", "/issues/42.json"): fetched,
        ("PUT", "/issues/42.json"): None,
    })
    result = await issues.update_issue(
        client, cache, 42, subject="Renamed", notes="bumping subject"
    )
    assert "issue" in result
    # No observation should be recorded since status didn't change.
    assert cache.list_workflow_observations(tracker_id=1) == []
    # PUT payload contains only what we specified.
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload == {"subject": "Renamed", "notes": "bumping subject"}


async def test_update_issue_passes_fixed_version_id(cache: SchemaCache) -> None:
    """fixed_version_id assigns the issue to a target version; empty string clears."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(client, cache, 42, fixed_version_id=7)
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload == {"fixed_version_id": 7}


async def test_update_issue_passes_due_date(cache: SchemaCache) -> None:
    """Regression for ClaudeCode#2734: due_date must reach the API."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(client, cache, 42, due_date="2026-05-17")
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload == {"due_date": "2026-05-17"}


async def test_update_issue_passes_start_date(cache: SchemaCache) -> None:
    """Regression for ClaudeCode#2734: start_date must reach the API."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(client, cache, 42, start_date="2026-04-09")
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload == {"start_date": "2026-04-09"}


async def test_update_issue_passes_done_ratio_including_zero(cache: SchemaCache) -> None:
    """done_ratio=0 must be sent explicitly (it's not the same as 'unchanged')."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(client, cache, 42, done_ratio=0)
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload == {"done_ratio": 0}


async def test_update_issue_passes_done_ratio_50(cache: SchemaCache) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(client, cache, 42, done_ratio=50)
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload == {"done_ratio": 50}


async def test_update_issue_passes_custom_fields(cache: SchemaCache) -> None:
    """Custom-fields list (other than Difficulty) reaches the API as-is."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(
        client, cache, 42,
        custom_fields=[{"id": 7, "value": "spike-A"}, {"id": 8, "value": "external"}],
    )
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload == {
        "custom_fields": [
            {"id": 7, "value": "spike-A"},
            {"id": 8, "value": "external"},
        ],
    }


async def test_create_issue_passes_due_and_start_and_done_ratio(cache: SchemaCache) -> None:
    """The same three new fields are first-class on create_issue too."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    _seed_difficulty_field(cache)
    client = FakeClient({
        ("POST", "/issues.json"): {"issue": {"id": 99}},
    })
    await issues.create_issue(
        client, cache,
        project="claudecode", tracker="Bug", subject="New",
        due_date="2026-05-17",
        start_date="2026-05-12",
        done_ratio=25,
    )
    post_payload = next(c for c in client.calls if c[0] == "POST")[2]["issue"]
    assert post_payload["due_date"] == "2026-05-17"
    assert post_payload["start_date"] == "2026-05-12"
    assert post_payload["done_ratio"] == 25


async def test_update_issue_passes_empty_fixed_version_id_to_clear(
    cache: SchemaCache,
) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(client, cache, 42, fixed_version_id="")
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload == {"fixed_version_id": ""}


async def test_update_issue_difficulty_translates_to_custom_fields(
    cache: SchemaCache,
) -> None:
    """update_issue accepts a 'difficulty' parameter that translates to custom_fields."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    _seed_difficulty_field(cache, field_id=1)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(client, cache, 42, difficulty="Hard")
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload == {"custom_fields": [{"id": 1, "value": "Hard"}]}


async def test_update_issue_difficulty_no_default_fill(cache: SchemaCache) -> None:
    """update_issue with NO difficulty arg and NO Difficulty in custom_fields
    must NOT default-fill — that would silently overwrite user-set values."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    _seed_difficulty_field(cache, field_id=1)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(client, cache, 42, subject="Renamed")
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    # No custom_fields added.
    assert "custom_fields" not in put_payload
    assert put_payload == {"subject": "Renamed"}


async def test_update_issue_difficulty_overrides_existing_custom_field_entry(
    cache: SchemaCache,
) -> None:
    """If both difficulty=... and a Difficulty entry in custom_fields are passed,
    difficulty wins."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    _seed_difficulty_field(cache, field_id=1)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(
        client, cache, 42,
        custom_fields=[{"id": 1, "value": "Easy"}],
        difficulty="Hard",
    )
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload["custom_fields"] == [{"id": 1, "value": "Hard"}]


async def test_update_issue_preserves_explicit_difficulty_via_custom_fields(
    cache: SchemaCache,
) -> None:
    """Caller passes Difficulty via custom_fields only — should be preserved."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    _seed_difficulty_field(cache, field_id=1)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(),
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(
        client, cache, 42,
        custom_fields=[{"id": 1, "value": "Normal"}],
    )
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload["custom_fields"] == [{"id": 1, "value": "Normal"}]


async def test_update_issue_records_allowed_outcome_on_status_change(
    cache: SchemaCache,
) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    # Pre-PUT GET shows the issue in New; post-PUT GET shows it in In Progress
    # — i.e. Redmine genuinely applied the status change.
    client = FakeClient({
        ("GET", "/issues/42.json"): [
            _issue_payload(status_id=1, status_name="New"),
            _issue_payload(status_id=2, status_name="In Progress"),
        ],
        ("GET", "/users/current.json"): {
            "user": {
                "id": 1,
                "admin": True,
                "memberships": [
                    {"project": {"id": 15}, "roles": [{"id": 4, "name": "Developer"}]}
                ],
            }
        },
        ("PUT", "/issues/42.json"): None,
    })
    result = await issues.update_issue(client, cache, 42, status="In Progress")
    assert "issue" in result
    obs = cache.get_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2
    )
    assert obs is not None
    assert obs["outcome"] == "allowed"


async def test_update_issue_short_circuits_on_cached_disallowed(
    cache: SchemaCache,
) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=5,
        outcome="disallowed", error_text="Status is not allowed",
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed",
    )
    fetched = _issue_payload(status_id=1, status_name="New")
    client = FakeClient({
        ("GET", "/issues/42.json"): fetched,
        ("GET", "/users/current.json"): {
            "user": {
                "id": 1,
                "admin": False,
                "memberships": [
                    {"project": {"id": 15}, "roles": [{"id": 4, "name": "Developer"}]}
                ],
            }
        },
    })
    result = await issues.update_issue(client, cache, 42, status="Closed")
    assert result["error"] == "workflow_transition_disallowed"
    assert result["from_status"] == "New"
    assert result["to_status"] == "Closed"
    # Pre-flight must skip the PUT entirely.
    assert not any(c[0] == "PUT" for c in client.calls)
    # allowed_next_states should reflect the prior allowed observation.
    assert "In Progress" in result["allowed_next_states"]


async def test_update_issue_records_disallowed_when_api_returns_422_status(
    cache: SchemaCache,
) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    fetched = _issue_payload(status_id=1, status_name="New")
    client = FakeClient(
        responses={
            ("GET", "/issues/42.json"): fetched,
            ("GET", "/users/current.json"): {
                "user": {
                    "id": 1,
                    "admin": False,
                    "memberships": [
                        {"project": {"id": 15}, "roles": [{"id": 4, "name": "Developer"}]}
                    ],
                }
            },
        },
        errors={("PUT", "/issues/42.json"): RedmineAPIError(
            status_code=422, body={"errors": ["Status is not allowed"]}
        )},
    )
    result = await issues.update_issue(client, cache, 42, status="Closed")
    assert result["error"] == "redmine_api_422"
    obs = cache.get_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=5
    )
    assert obs is not None
    assert obs["outcome"] == "disallowed"
    assert "Status is not allowed" in (obs["last_error_text"] or "")


async def test_update_issue_returns_nothing_to_update_when_no_fields_supplied(
    cache: SchemaCache,
) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({("GET", "/issues/42.json"): _issue_payload()})
    result = await issues.update_issue(client, cache, 42)
    assert result["error"] == "nothing_to_update"
    assert not any(c[0] == "PUT" for c in client.calls)


async def test_update_issue_propagates_get_404(cache: SchemaCache) -> None:
    client = FakeClient(errors={("GET", "/issues/999.json"): RedmineAPIError(
        status_code=404, body={"errors": ["Not found"]}
    )})
    result = await issues.update_issue(client, cache, 999, subject="x")
    assert result["error"] == "redmine_api_404"


async def test_update_issue_detects_silent_status_no_op(cache: SchemaCache) -> None:
    """Regression for infra#2579 Findings 1+2: when Redmine returns 2xx but
    the status_id didn't actually move (e.g. block_descendants_issues_closing
    with open subtasks, or a custom workflow rule the cache hasn't observed
    yet), the caller used to get a success-looking payload. Now they get a
    structured `status_change_silently_ignored` error with a hint."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        # Both fetches return status_id=1 — i.e. the PUT had no effect.
        ("GET", "/issues/42.json"): _issue_payload(status_id=1, status_name="New"),
        ("GET", "/users/current.json"): {
            "user": {
                "id": 1,
                "admin": True,
                "memberships": [
                    {"project": {"id": 15}, "roles": [{"id": 4, "name": "Developer"}]}
                ],
            }
        },
        ("PUT", "/issues/42.json"): None,
    })
    result = await issues.update_issue(client, cache, 42, status="In Progress")
    assert result["error"] == "status_change_silently_ignored"
    assert result["requested_status"] == "In Progress"
    assert result["actual_status"] == "New"
    assert result["requested_status_id"] == 2
    assert result["actual_status_id"] == 1
    assert "did not apply" in result["hint"]
    # Should NOT record the transition as observed-allowed — the status
    # didn't move; we only know this single attempt was a no-op.
    obs = cache.get_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2
    )
    assert obs is None


async def test_update_issue_silent_no_op_names_subtasks_when_closing(
    cache: SchemaCache,
) -> None:
    """When the target is a closed status AND the issue has children, the
    hint should name the children as the likely cause (Redmine's
    block_descendants_issues_closing setting)."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    issue_with_children: dict[str, Any] = _issue_payload(status_id=1, status_name="New")
    issue_with_children["issue"]["children"] = [
        {"id": 100, "tracker": {"id": 1, "name": "Bug"}, "subject": "Child A"},
        {"id": 101, "tracker": {"id": 1, "name": "Bug"}, "subject": "Child B"},
    ]
    client = FakeClient({
        ("GET", "/issues/42.json"): issue_with_children,
        ("GET", "/users/current.json"): {
            "user": {
                "id": 1,
                "admin": True,
                "memberships": [
                    {"project": {"id": 15}, "roles": [{"id": 4, "name": "Developer"}]}
                ],
            }
        },
        ("PUT", "/issues/42.json"): None,
    })
    result = await issues.close_issue(client, cache, 42, note="trying to close")
    assert result["error"] == "status_change_silently_ignored"
    assert "#100" in result["hint"]
    assert "#101" in result["hint"]
    assert "subtasks" in result["hint"]
    assert "block_descendants_issues_closing" in result["hint"]


async def test_update_issue_silent_no_op_re_fetches_with_children_include(
    cache: SchemaCache,
) -> None:
    """When status_changing, the post-PUT re-fetch must include children
    so subtask detection works."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(status_id=1, status_name="New"),
        ("GET", "/users/current.json"): {
            "user": {
                "id": 1,
                "admin": True,
                "memberships": [
                    {"project": {"id": 15}, "roles": [{"id": 4, "name": "Developer"}]}
                ],
            }
        },
        ("PUT", "/issues/42.json"): None,
    })
    await issues.update_issue(client, cache, 42, status="In Progress")
    # Two GET /issues/42.json calls: pre-PUT (no children) and post-PUT (with children).
    issue_gets = [c for c in client.calls if c[0] == "GET" and c[1] == "/issues/42.json"]
    assert len(issue_gets) == 2
    # Pre-PUT include is DEFAULT_INCLUDE (no children).
    assert "children" not in issue_gets[0][2]["include"]
    # Post-PUT include adds children for subtask detection.
    assert "children" in issue_gets[1][2]["include"]


# ---------------------------------------------------------------------
# close_issue
# ---------------------------------------------------------------------


async def test_close_issue_uses_first_is_closed_status(cache: SchemaCache) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): [
            _issue_payload(status_id=3, status_name="Resolved"),
            # Post-PUT: status genuinely moved to Closed.
            _issue_payload(status_id=5, status_name="Closed"),
        ],
        ("GET", "/users/current.json"): {
            "user": {"id": 1, "admin": True, "memberships": []}
        },
        ("PUT", "/issues/42.json"): None,
    })
    result = await issues.close_issue(client, cache, 42, note="done")
    assert "issue" in result
    put_payload = next(c for c in client.calls if c[0] == "PUT")[2]["issue"]
    assert put_payload["status_id"] == 5  # Closed
    assert put_payload["notes"] == "done"


async def test_close_issue_repackages_disallowed_with_closure_hint(
    cache: SchemaCache,
) -> None:
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    cache.record_workflow_observation(
        tracker_id=1, role_id=0, from_status_id=1, to_status_id=5,
        outcome="disallowed", error_text="Status is not allowed",
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=0, from_status_id=1, to_status_id=3, outcome="allowed",
    )
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload(status_id=1, status_name="New"),
        ("GET", "/users/current.json"): {
            "user": {"id": 1, "admin": True, "memberships": []}
        },
    })
    result = await issues.close_issue(client, cache, 42)
    assert result["error"] == "workflow_transition_disallowed"
    assert "direct closure" in result["hint"]
    assert "Resolved" in result["hint"]


# ---------------------------------------------------------------------
# held-gate integration
# ---------------------------------------------------------------------


async def test_update_issue_rejects_close_when_held(cache: SchemaCache) -> None:
    """update_issue with a closed-status target rejects when Held is non-empty."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload_held(
            status_id=1, status_name="New",
            held="Wait for June monthly report",
            held_until="2026-06-01",
        ),
    })
    result = await issues.update_issue(client, cache, 42, status=5)  # 5 = Closed
    assert result["error"] == "issue_held"
    assert result["issue_id"] == 42
    assert result["held_reason"] == "Wait for June monthly report"
    assert result["held_until"] == "2026-06-01"
    assert "#42" in result["hint"]
    # Verify no PUT was sent.
    assert not any(c[0] == "PUT" for c in client.calls)


async def test_update_issue_rejects_close_when_held_no_date(cache: SchemaCache) -> None:
    """Held without date still blocks close."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload_held(
            status_id=1, status_name="New",
            held="Need on-site USB access",
        ),
    })
    result = await issues.update_issue(client, cache, 42, status=5)
    assert result["error"] == "issue_held"
    assert "held_until" not in result
    assert not any(c[0] == "PUT" for c in client.calls)


async def test_update_issue_allows_close_when_not_held(cache: SchemaCache) -> None:
    """Close succeeds when Held field is empty."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): [
            _issue_payload_held(status_id=1, status_name="New", held=""),
            _issue_payload_held(status_id=5, status_name="Closed", held=""),
        ],
        ("GET", "/users/current.json"): {
            "user": {"id": 1, "admin": True, "memberships": []}
        },
        ("PUT", "/issues/42.json"): None,
    })
    result = await issues.update_issue(client, cache, 42, status=5)
    assert "issue" in result
    assert any(c[0] == "PUT" for c in client.calls)


async def test_update_issue_allows_non_close_transition_when_held(
    cache: SchemaCache,
) -> None:
    """Non-close status changes are allowed even when held."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): [
            _issue_payload_held(
                status_id=1, status_name="New",
                held="Wait for something",
            ),
            _issue_payload_held(
                status_id=2, status_name="In Progress",
                held="Wait for something",
            ),
        ],
        ("GET", "/users/current.json"): {
            "user": {"id": 1, "admin": True, "memberships": []}
        },
        ("PUT", "/issues/42.json"): None,
    })
    result = await issues.update_issue(client, cache, 42, status=2)  # 2 = In Progress
    assert "issue" in result
    assert any(c[0] == "PUT" for c in client.calls)


async def test_close_issue_rejects_when_held(cache: SchemaCache) -> None:
    """close_issue (which delegates to update_issue) also rejects held tickets."""
    _seed_enums(cache)
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues/42.json"): _issue_payload_held(
            status_id=3, status_name="Resolved",
            held="Waiting on vendor response",
        ),
    })
    result = await issues.close_issue(client, cache, 42, note="closing")
    assert result["error"] == "issue_held"
    assert result["held_reason"] == "Waiting on vendor response"
    assert not any(c[0] == "PUT" for c in client.calls)


# ---------------------------------------------------------------------
# search_issues
# ---------------------------------------------------------------------


async def test_search_issues_passes_substring_subject_filter(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues.json"): {
            "issues": [{"id": 1, "subject": "drift:foo"}],
            "total_count": 1,
            "offset": 0,
            "limit": 25,
        },
    })
    result = await issues.search_issues(client, cache, query="drift")
    assert result["total_count"] == 1
    sent = client.calls[-1][2]
    assert sent["subject"] == "~drift"
    assert sent["limit"] == 25


async def test_search_issues_resolves_project_slug_to_id(cache: SchemaCache) -> None:
    _seed_tracker_and_project(cache)
    client = FakeClient({
        ("GET", "/issues.json"): {"issues": [], "total_count": 0},
    })
    result = await issues.search_issues(client, cache, project="claudecode")
    assert result["total_count"] == 0
    sent = client.calls[-1][2]
    assert sent["project_id"] == 15


async def test_search_issues_passes_open_status_token_through(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues.json"): {"issues": [], "total_count": 0},
    })
    await issues.search_issues(client, cache, query="x", status="open")
    sent = client.calls[-1][2]
    assert sent["status_id"] == "open"


async def test_search_issues_resolves_named_status(cache: SchemaCache) -> None:
    _seed_enums(cache)
    client = FakeClient({
        ("GET", "/issues.json"): {"issues": [], "total_count": 0},
    })
    await issues.search_issues(client, cache, query="x", status="Closed")
    sent = client.calls[-1][2]
    assert sent["status_id"] == 5


async def test_search_issues_forwards_query_id(cache: SchemaCache) -> None:
    """ClaudeCode#2744: query_id invokes a Redmine saved query."""
    client = FakeClient({("GET", "/issues.json"): {"issues": [], "total_count": 0}})
    await issues.search_issues(client, cache, query_id=12)
    params = next(c for c in client.calls if c[0] == "GET")[2]
    assert params["query_id"] == 12


async def test_search_issues_query_id_combines_with_status(cache: SchemaCache) -> None:
    """Saved-query + status layered as separate URL params (Redmine merges)."""
    _seed_enums(cache)
    client = FakeClient({("GET", "/issues.json"): {"issues": [], "total_count": 0}})
    await issues.search_issues(client, cache, query_id=12, status="closed")
    params = next(c for c in client.calls if c[0] == "GET")[2]
    assert params["query_id"] == 12
    assert params["status_id"] == "closed"


async def test_search_issues_omits_query_id_when_not_set(cache: SchemaCache) -> None:
    """Default behavior unchanged — no query_id key on the wire when omitted."""
    client = FakeClient({("GET", "/issues.json"): {"issues": [], "total_count": 0}})
    await issues.search_issues(client, cache, query="bug")
    params = next(c for c in client.calls if c[0] == "GET")[2]
    assert "query_id" not in params


async def test_search_issues_caps_limit_at_100(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues.json"): {"issues": [], "total_count": 0},
    })
    await issues.search_issues(client, cache, limit=500)
    sent = client.calls[-1][2]
    assert sent["limit"] == 100
