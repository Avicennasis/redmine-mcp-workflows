"""Unit tests for the validation layer (transitions, fields, permissions)."""

from __future__ import annotations

from pathlib import Path

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import (
    CustomFieldShapeError,
    CustomFieldUnknown,
    CustomFieldValueInvalid,
    IssueHeld,
    RequiredFieldMissing,
    RoleNotAuthorized,
    TimeEntryHoursInvalid,
    WorkflowTransitionDisallowed,
)
from redmine_mcp.validation import fields as field_validators
from redmine_mcp.validation import permissions, transitions

# ---------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


def test_is_disallowed_returns_none_when_unobserved(cache: SchemaCache) -> None:
    assert (
        transitions.is_disallowed(
            cache, tracker_id=1, role_ids=[4], from_status_id=1, to_status_id=2
        )
        is None
    )


def test_is_disallowed_returns_hit_when_role_matches(cache: SchemaCache) -> None:
    cache.record_workflow_observation(
        tracker_id=1,
        role_id=4,
        from_status_id=1,
        to_status_id=5,
        outcome="disallowed",
        error_text="Status is not allowed",
    )
    hit = transitions.is_disallowed(
        cache, tracker_id=1, role_ids=[4], from_status_id=1, to_status_id=5
    )
    assert hit is not None
    assert hit.role_id == 4
    assert hit.last_error_text == "Status is not allowed"


def test_is_disallowed_includes_role_zero_global_admin(cache: SchemaCache) -> None:
    cache.record_workflow_observation(
        tracker_id=1,
        role_id=0,
        from_status_id=1,
        to_status_id=5,
        outcome="disallowed",
    )
    hit = transitions.is_disallowed(
        cache, tracker_id=1, role_ids=[], from_status_id=1, to_status_id=5
    )
    assert hit is not None
    assert hit.role_id == 0


def test_is_disallowed_skips_allowed_observations(cache: SchemaCache) -> None:
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    assert (
        transitions.is_disallowed(
            cache, tracker_id=1, role_ids=[4], from_status_id=1, to_status_id=2
        )
        is None
    )


def test_is_disallowed_picks_most_recent_when_multiple_roles(cache: SchemaCache) -> None:
    import time

    cache.record_workflow_observation(
        tracker_id=1, role_id=3, from_status_id=1, to_status_id=5, outcome="disallowed"
    )
    time.sleep(1.05)
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=5, outcome="disallowed"
    )
    hit = transitions.is_disallowed(
        cache, tracker_id=1, role_ids=[3, 4], from_status_id=1, to_status_id=5
    )
    assert hit is not None
    assert hit.role_id == 4  # later observation wins


def test_is_disallowed_unknown_role_keeps_union_unknown(cache: SchemaCache) -> None:
    """One disallowed role cannot prove an unobserved role also disallows it."""
    cache.record_workflow_observation(
        tracker_id=1, role_id=3, from_status_id=1, to_status_id=5, outcome="disallowed"
    )
    assert (
        transitions.is_disallowed(
            cache, tracker_id=1, role_ids=[3, 4], from_status_id=1, to_status_id=5
        )
        is None
    )


def test_is_disallowed_does_not_mix_global_observation_into_project_roles(
    cache: SchemaCache,
) -> None:
    cache.record_workflow_observation(
        tracker_id=1, role_id=0, from_status_id=1, to_status_id=5, outcome="disallowed"
    )
    assert (
        transitions.is_disallowed(
            cache, tracker_id=1, role_ids=[4], from_status_id=1, to_status_id=5
        )
        is None
    )


def test_is_disallowed_allowed_role_wins_over_disallowed_role(cache: SchemaCache) -> None:
    """Redmine is role-union: one role allowing the transition is enough.

    A ``disallowed`` observation for role 3 must not block a user who also
    holds role 4, which has been observed allowing the same transition.
    """
    cache.record_workflow_observation(
        tracker_id=1, role_id=3, from_status_id=1, to_status_id=5, outcome="disallowed"
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=5, outcome="allowed"
    )
    assert (
        transitions.is_disallowed(
            cache, tracker_id=1, role_ids=[3, 4], from_status_id=1, to_status_id=5
        )
        is None
    )


def test_is_disallowed_allowed_wins_even_when_disallowed_is_newer(cache: SchemaCache) -> None:
    """A later disallowed observation on one role must not outweigh an allowed one.

    Ordering is irrelevant under role-union: role 4's ``allowed`` proves the
    user may make the transition even though role 3 failed more recently.
    """
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=5, outcome="allowed"
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=3, from_status_id=1, to_status_id=5, outcome="disallowed"
    )
    assert (
        transitions.is_disallowed(
            cache, tracker_id=1, role_ids=[3, 4], from_status_id=1, to_status_id=5
        )
        is None
    )


def test_allowed_next_returns_observed_to_states(cache: SchemaCache) -> None:
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=3, outcome="allowed"
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=5, outcome="disallowed"
    )
    out = transitions.allowed_next(cache, tracker_id=1, role_ids=[4], from_status_id=1)
    assert sorted(out) == [2, 3]


def test_has_any_observation(cache: SchemaCache) -> None:
    assert (
        transitions.has_any_observation(
            cache, tracker_id=1, role_ids=[4], from_status_id=1, to_status_id=2
        )
        is False
    )
    cache.record_workflow_observation(
        tracker_id=1, role_id=4, from_status_id=1, to_status_id=2, outcome="allowed"
    )
    assert (
        transitions.has_any_observation(
            cache, tracker_id=1, role_ids=[4], from_status_id=1, to_status_id=2
        )
        is True
    )


# ---------------------------------------------------------------------
# fields
# ---------------------------------------------------------------------


def test_validate_required_create_complete_payload() -> None:
    errs = field_validators.validate_required(
        {"project": "claudecode", "tracker": 1, "subject": "x"}, op="create"
    )
    assert errs == []


def test_validate_required_create_missing_subject() -> None:
    errs = field_validators.validate_required({"project": "claudecode", "tracker": 1}, op="create")
    assert len(errs) == 1
    assert isinstance(errs[0], RequiredFieldMissing)
    assert errs[0].as_dict()["field"] == "subject"


def test_validate_required_create_empty_string_treated_as_missing() -> None:
    errs = field_validators.validate_required(
        {"project": "claudecode", "tracker": 1, "subject": "   "}, op="create"
    )
    assert len(errs) == 1
    assert errs[0].as_dict()["field"] == "subject"


def test_validate_required_update_skips_check() -> None:
    errs = field_validators.validate_required({}, op="update")
    assert errs == []


def test_validate_custom_fields_no_entries_no_errors() -> None:
    assert field_validators.validate_custom_fields({}) == []
    assert field_validators.validate_custom_fields({"custom_fields": []}) == []


def test_validate_custom_fields_must_be_list() -> None:
    errs = field_validators.validate_custom_fields({"custom_fields": {"id": 1, "value": "x"}})
    assert len(errs) == 1
    assert isinstance(errs[0], CustomFieldShapeError)


def test_validate_custom_fields_entry_must_be_dict() -> None:
    errs = field_validators.validate_custom_fields({"custom_fields": ["not a dict"]})
    assert any(isinstance(e, CustomFieldShapeError) for e in errs)


def test_validate_custom_fields_missing_keys() -> None:
    errs = field_validators.validate_custom_fields({"custom_fields": [{"id": 1}, {"value": "x"}]})
    assert len(errs) == 2
    reasons = [e.as_dict()["reason"] for e in errs]
    assert any("'value'" in r for r in reasons)
    assert any("'id'" in r for r in reasons)


def test_validate_custom_fields_unknown_id_when_known_set_provided() -> None:
    errs = field_validators.validate_custom_fields(
        {"custom_fields": [{"id": 99, "value": "x"}]},
        known_field_ids=[1, 2, 3],
        tracker_name="Bug",
    )
    assert len(errs) == 1
    assert isinstance(errs[0], CustomFieldUnknown)
    payload = errs[0].as_dict()
    assert payload["field_id"] == 99
    assert payload["known_ids"] == [1, 2, 3]


def test_validate_custom_fields_skips_id_check_when_known_set_absent() -> None:
    # Without a known_field_ids list we trust the API; only shape errors fire.
    errs = field_validators.validate_custom_fields({"custom_fields": [{"id": 99, "value": "x"}]})
    assert errs == []


# ---------------------------------------------------------------------
# custom-field enum case correction
# ---------------------------------------------------------------------

_ENUMS = {1: ["Unclassified", "Easy", "Normal", "Hard"]}


def test_correct_custom_field_values_exact_match_unchanged() -> None:
    entries = [{"id": 1, "value": "Easy"}]
    corrected, corrections, errors = field_validators.correct_custom_field_values(
        entries, enum_values_by_id=_ENUMS
    )
    assert corrected == entries
    assert corrections == []
    assert errors == []


def test_correct_custom_field_values_case_insensitive_rewrite() -> None:
    corrected, corrections, errors = field_validators.correct_custom_field_values(
        [{"id": 1, "value": "hARd"}],
        enum_values_by_id=_ENUMS,
        field_names_by_id={1: "Difficulty"},
    )
    assert corrected == [{"id": 1, "value": "Hard"}]
    assert errors == []
    assert corrections == [
        {
            "field_id": 1,
            "field_name": "Difficulty",
            "from": "hARd",
            "to": "Hard",
            "message": "corrected 'hARd' -> 'Hard'",
        }
    ]


def test_correct_custom_field_values_name_keyed_entry() -> None:
    corrected, corrections, errors = field_validators.correct_custom_field_values(
        [{"name": "Difficulty", "value": "normal"}],
        enum_values_by_id=_ENUMS,
        name_to_id={"Difficulty": 1},
        field_names_by_id={1: "Difficulty"},
    )
    assert corrected == [{"name": "Difficulty", "value": "Normal"}]
    assert len(corrections) == 1
    assert errors == []


def test_correct_custom_field_values_rejects_no_match() -> None:
    corrected, corrections, errors = field_validators.correct_custom_field_values(
        [{"id": 1, "value": "Impossible"}], enum_values_by_id=_ENUMS
    )
    assert corrected == [{"id": 1, "value": "Impossible"}]
    assert corrections == []
    assert len(errors) == 1
    assert isinstance(errors[0], CustomFieldValueInvalid)
    assert errors[0].as_dict()["reason"] == "no_match"


def test_correct_custom_field_values_rejects_ambiguous() -> None:
    _corrected, _corrections, errors = field_validators.correct_custom_field_values(
        [{"id": 2, "value": "prod"}],
        enum_values_by_id={2: ["Prod", "PROD"]},
    )
    assert len(errors) == 1
    assert errors[0].as_dict()["reason"] == "ambiguous"


def test_correct_custom_field_values_skips_unknown_field() -> None:
    """No cached possible_values → pass through, never reject."""
    entries = [{"id": 99, "value": "whatever"}]
    corrected, corrections, errors = field_validators.correct_custom_field_values(
        entries, enum_values_by_id=_ENUMS
    )
    assert corrected == entries
    assert corrections == []
    assert errors == []


def test_correct_custom_field_values_skips_empty_and_non_string() -> None:
    entries = [{"id": 1, "value": ""}, {"id": 1, "value": ["Easy", "Hard"]}, {"id": 1, "value": 5}]
    corrected, corrections, errors = field_validators.correct_custom_field_values(
        entries, enum_values_by_id=_ENUMS
    )
    assert corrected == entries
    assert corrections == []
    assert errors == []


def test_correct_custom_field_values_none_returns_none() -> None:
    corrected, corrections, errors = field_validators.correct_custom_field_values(None)
    assert corrected is None
    assert corrections == []
    assert errors == []


# ---------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------


def test_is_admin_recognizes_admin_user() -> None:
    assert permissions.is_admin({"admin": True}) is True
    assert permissions.is_admin({"admin": False}) is False
    assert permissions.is_admin({}) is False
    assert permissions.is_admin(None) is False


def test_role_names_for_project() -> None:
    user = {
        "memberships": [
            {"project": {"id": 10}, "roles": [{"name": "Manager"}, {"name": "Developer"}]},
            {"project": {"id": 11}, "roles": [{"name": "Reporter"}]},
        ],
    }
    assert permissions.role_names_for_project(user, 10) == ["Manager", "Developer"]
    assert permissions.role_names_for_project(user, 11) == ["Reporter"]
    assert permissions.role_names_for_project(user, 999) == []


def test_require_role_admin_bypasses() -> None:
    errs = permissions.require_role(
        {"admin": True, "memberships": []},
        project_id=1,
        required=["Manager"],
    )
    assert errs == []


def test_require_role_when_user_has_required() -> None:
    user = {
        "admin": False,
        "memberships": [{"project": {"id": 1}, "roles": [{"name": "Manager"}]}],
    }
    assert permissions.require_role(user, project_id=1, required=["Manager"]) == []


def test_require_role_when_user_missing_required() -> None:
    user = {
        "admin": False,
        "memberships": [{"project": {"id": 1}, "roles": [{"name": "Reporter"}]}],
    }
    errs = permissions.require_role(user, project_id=1, required=["Manager"])
    assert len(errs) == 1
    assert isinstance(errs[0], RoleNotAuthorized)
    payload = errs[0].as_dict()
    assert payload["required_roles"] == ["Manager"]
    assert payload["current_roles"] == ["Reporter"]


# ---------------------------------------------------------------------
# WorkflowTransitionDisallowed payload shape
# ---------------------------------------------------------------------


def test_workflow_transition_disallowed_payload() -> None:
    err = WorkflowTransitionDisallowed(
        tracker="Bug",
        from_status="In Progress",
        to_status="Closed",
        user_role="Developer",
        allowed_next_states=["Resolved"],
        last_error_text="Status is not allowed",
        observed_at=12345,
    )
    payload = err.as_dict()
    assert payload["error"] == "workflow_transition_disallowed"
    assert payload["tracker"] == "Bug"
    assert payload["from_status"] == "In Progress"
    assert payload["to_status"] == "Closed"
    assert payload["user_role"] == "Developer"
    assert payload["allowed_next_states"] == ["Resolved"]
    assert payload["observation_basis"] == "learned"
    assert payload["last_error_text"] == "Status is not allowed"
    assert payload["observed_at"] == 12345
    assert "Try one of: Resolved." in payload["hint"]


def test_workflow_transition_disallowed_no_allowed_states_hint() -> None:
    err = WorkflowTransitionDisallowed(
        tracker="Bug",
        from_status="New",
        to_status="Closed",
    )
    payload = err.as_dict()
    assert "No allowed next states have been observed" in payload["hint"]
    assert payload["allowed_next_states"] == []


# ---------------------------------------------------------------------
# parse_hours / validate_hours (v0.2 — time-entry support)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (2.5, 2.5),
        (2, 2.0),
        ("2.5", 2.5),
        ("0.25", 0.25),
        ("2:30", 2.5),
        ("0:15", 0.25),
        ("10:00", 10.0),
        ("0", 0.0),
    ],
)
def test_parse_hours_accepts_valid_forms(value: object, expected: float) -> None:
    parsed, reason = field_validators.parse_hours(value)
    assert reason is None
    assert parsed == pytest.approx(expected)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "abc", "2:abc", "2:30:00", "-1", -0.5, "2:60", "2:-5", True],
)
def test_parse_hours_rejects_invalid_forms(value: object) -> None:
    parsed, reason = field_validators.parse_hours(value)
    assert parsed is None
    assert reason


def test_parse_hours_none_is_required() -> None:
    parsed, reason = field_validators.parse_hours(None)
    assert parsed is None
    assert reason == "value is required"


def test_validate_hours_returns_structured_error_on_bad_input() -> None:
    parsed, errors = field_validators.validate_hours("nope")
    assert parsed is None
    assert len(errors) == 1
    assert isinstance(errors[0], TimeEntryHoursInvalid)
    assert errors[0].as_dict()["error"] == "time_entry_hours_invalid"


def test_validate_hours_no_errors_on_good_input() -> None:
    parsed, errors = field_validators.validate_hours("1:15")
    assert parsed == pytest.approx(1.25)
    assert errors == []


# ---------------------------------------------------------------------
# IssueHeld payload shape
# ---------------------------------------------------------------------


def test_issue_held_payload_with_date() -> None:
    err = IssueHeld(
        issue_id=1234,
        held_reason="Wait for June monthly report to verify",
        held_until="2026-06-01",
    )
    payload = err.as_dict()
    assert payload["error"] == "issue_held"
    assert payload["issue_id"] == 1234
    assert payload["held_reason"] == "Wait for June monthly report to verify"
    assert payload["held_until"] == "2026-06-01"
    assert "#1234" in payload["hint"]
    assert "Wait for June monthly report" in payload["hint"]
    assert "2026-06-01" in payload["hint"]


def test_issue_held_payload_without_date() -> None:
    err = IssueHeld(
        issue_id=99,
        held_reason="Need on-site USB access",
    )
    payload = err.as_dict()
    assert payload["error"] == "issue_held"
    assert payload["issue_id"] == 99
    assert payload["held_reason"] == "Need on-site USB access"
    assert "held_until" not in payload
    assert "#99" in payload["hint"]
    assert "Need on-site USB access" in payload["hint"]


# ---------------------------------------------------------------------
# check_held_gate
# ---------------------------------------------------------------------


def test_check_held_gate_returns_none_when_no_custom_fields() -> None:
    issue = {"id": 10, "subject": "No CF"}
    result = field_validators.check_held_gate(issue)
    assert result is None


def test_check_held_gate_returns_none_when_held_field_absent() -> None:
    issue = {
        "id": 10,
        "subject": "Has CF but not Held",
        "custom_fields": [{"id": 1, "name": "Difficulty", "value": "Normal"}],
    }
    result = field_validators.check_held_gate(issue)
    assert result is None


def test_check_held_gate_returns_none_when_held_field_empty() -> None:
    issue = {
        "id": 10,
        "subject": "Held is empty",
        "custom_fields": [
            {"id": 1, "name": "Difficulty", "value": "Normal"},
            {"id": 2, "name": "Held", "value": ""},
        ],
    }
    result = field_validators.check_held_gate(issue)
    assert result is None


def test_check_held_gate_returns_error_when_held_nonempty() -> None:
    issue = {
        "id": 42,
        "subject": "Gated ticket",
        "custom_fields": [
            {"id": 2, "name": "Held", "value": "Wait for June monthly report"},
        ],
    }
    result = field_validators.check_held_gate(issue)
    assert result is not None
    assert isinstance(result, IssueHeld)
    payload = result.as_dict()
    assert payload["issue_id"] == 42
    assert payload["held_reason"] == "Wait for June monthly report"
    assert "held_until" not in payload


def test_check_held_gate_includes_held_until_when_present() -> None:
    issue = {
        "id": 42,
        "subject": "Date-gated ticket",
        "custom_fields": [
            {"id": 2, "name": "Held", "value": "Wait for June monthly report"},
            {"id": 3, "name": "Held Until", "value": "2026-06-01"},
        ],
    }
    result = field_validators.check_held_gate(issue)
    assert result is not None
    payload = result.as_dict()
    assert payload["held_until"] == "2026-06-01"


def test_check_held_gate_ignores_whitespace_only_held_value() -> None:
    issue = {
        "id": 10,
        "subject": "Whitespace held",
        "custom_fields": [
            {"id": 2, "name": "Held", "value": "   "},
        ],
    }
    result = field_validators.check_held_gate(issue)
    assert result is None


def test_check_held_gate_held_until_alone_not_a_hold() -> None:
    issue = {
        "id": 10,
        "subject": "Date but no reason",
        "custom_fields": [
            {"id": 2, "name": "Held", "value": ""},
            {"id": 3, "name": "Held Until", "value": "2026-06-01"},
        ],
    }
    result = field_validators.check_held_gate(issue)
    assert result is None


def test_check_held_gate_matches_by_id_when_configured() -> None:
    """A pinned field id works even when the field is renamed/localized."""
    issue = {
        "id": 11,
        "custom_fields": [{"id": 2, "name": "Wartend", "value": "upstream patch"}],
    }
    result = field_validators.check_held_gate(issue, held_field_id=2)
    assert isinstance(result, IssueHeld)
    assert result.as_dict()["held_reason"] == "upstream patch"


def test_check_held_gate_ignores_name_when_id_configured() -> None:
    """With an id configured, a same-named field at a different id is not it."""
    issue = {
        "id": 12,
        "custom_fields": [{"id": 99, "name": "Held", "value": "wrong field"}],
    }
    assert field_validators.check_held_gate(issue, held_field_id=2) is None


def test_check_held_gate_pinned_held_until_field() -> None:
    issue = {
        "id": 13,
        "custom_fields": [
            {"id": 2, "name": "Held", "value": "reason"},
            {"id": 3, "name": "Held Until", "value": "2026-06-01"},
        ],
    }
    result = field_validators.check_held_gate(issue, held_field_id=2, held_until_field_id=3)
    assert isinstance(result, IssueHeld)
    assert result.as_dict()["held_until"] == "2026-06-01"
