"""Tests for the MCP business-workflow prompts."""

from __future__ import annotations

from redmine_mcp import server

EXPECTED = {"redmine_bug_triage", "redmine_feature_spec"}


def test_prompts_registered() -> None:
    registered = set(server.mcp._prompt_manager._prompts)
    assert registered >= EXPECTED


def test_bug_triage_prompt_guides_search_before_create() -> None:
    text = server.redmine_bug_triage("500 on /login")
    assert "redmine_search_issues" in text
    assert "redmine_create_issue" in text
    assert "500 on /login" in text


def test_feature_spec_prompt_asks_for_acceptance_criteria() -> None:
    text = server.redmine_feature_spec("add SSO")
    assert "Acceptance criteria" in text
    assert "add SSO" in text
