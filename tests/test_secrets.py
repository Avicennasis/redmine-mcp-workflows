"""Unit tests for the env-var secrets loader."""

from __future__ import annotations

import pytest

from redmine_mcp import secrets


def test_loads_redmine_api_key_from_env() -> None:
    assert secrets.load_api_key(env={"REDMINE_API_KEY": "from-env"}) == "from-env"


def test_api_key_returns_none_when_unset() -> None:
    assert secrets.load_api_key(env={}) is None


def test_api_key_whitespace_stripped() -> None:
    assert secrets.load_api_key(env={"REDMINE_API_KEY": "  spaced  "}) == "spaced"


def test_load_oauth_token_from_env() -> None:
    assert secrets.load_oauth_token(env={"REDMINE_OAUTH_TOKEN": "bearer-xyz"}) == "bearer-xyz"


def test_oauth_token_returns_none_when_unset() -> None:
    assert secrets.load_oauth_token(env={}) is None


def test_oauth_token_whitespace_stripped() -> None:
    assert secrets.load_oauth_token(env={"REDMINE_OAUTH_TOKEN": "  tok  "}) == "tok"


def test_both_can_coexist() -> None:
    env = {"REDMINE_API_KEY": "api-key", "REDMINE_OAUTH_TOKEN": "oauth-tok"}
    assert secrets.load_api_key(env=env) == "api-key"
    assert secrets.load_oauth_token(env=env) == "oauth-tok"


def test_secrets_file_param_ignored() -> None:
    """The secrets_file param is kept for back-compat but has no effect."""
    assert secrets.load_api_key("/nonexistent", env={"REDMINE_API_KEY": "k"}) == "k"
    assert secrets.load_api_key("/nonexistent", env={}) is None
