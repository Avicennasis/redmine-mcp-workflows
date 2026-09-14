"""Unit tests for env-var config parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from redmine_mcp.config import (
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_REDMINE_URL,
    Config,
)


def test_defaults_when_env_empty(tmp_path: Path) -> None:
    cfg = Config.from_env(env={})
    assert cfg.redmine_url == DEFAULT_REDMINE_URL
    assert cfg.api_key is None
    assert cfg.read_only is False
    assert cfg.cache_ttl_seconds == DEFAULT_CACHE_TTL_SECONDS
    assert cfg.extra_headers == {}
    assert cfg.log_level == "INFO"


def test_strips_trailing_slash_from_url() -> None:
    cfg = Config.from_env(env={"REDMINE_URL": "https://example.com/redmine/"})
    assert cfg.redmine_url == "https://example.com/redmine"


def test_read_only_truthy_values() -> None:
    for value in ("1", "true", "TRUE", "yes", "on"):
        cfg = Config.from_env(env={"REDMINE_MCP_READ_ONLY": value})
        assert cfg.read_only is True, value


def test_read_only_falsey_values() -> None:
    for value in ("", "0", "false", "no", "off", "anything-else"):
        cfg = Config.from_env(env={"REDMINE_MCP_READ_ONLY": value})
        assert cfg.read_only is False, value


def test_cache_ttl_int_parse() -> None:
    cfg = Config.from_env(env={"REDMINE_MCP_CACHE_TTL": "3600"})
    assert cfg.cache_ttl_seconds == 3600


def test_cache_ttl_falls_back_on_garbage() -> None:
    cfg = Config.from_env(env={"REDMINE_MCP_CACHE_TTL": "not-an-int"})
    assert cfg.cache_ttl_seconds == DEFAULT_CACHE_TTL_SECONDS


def test_custom_field_ids_default_to_none() -> None:
    cfg = Config.from_env(env={})
    assert cfg.held_field_id is None
    assert cfg.held_until_field_id is None
    assert cfg.difficulty_field_id is None


def test_custom_field_ids_parsed() -> None:
    cfg = Config.from_env(
        env={
            "REDMINE_MCP_HELD_FIELD_ID": "2",
            "REDMINE_MCP_HELD_UNTIL_FIELD_ID": "3",
            "REDMINE_MCP_DIFFICULTY_FIELD_ID": "7",
        }
    )
    assert cfg.held_field_id == 2
    assert cfg.held_until_field_id == 3
    assert cfg.difficulty_field_id == 7


def test_custom_field_ids_ignore_blank_and_garbage() -> None:
    cfg = Config.from_env(
        env={
            "REDMINE_MCP_HELD_FIELD_ID": "  ",
            "REDMINE_MCP_HELD_UNTIL_FIELD_ID": "nope",
            "REDMINE_MCP_DIFFICULTY_FIELD_ID": "",
        }
    )
    assert cfg.held_field_id is None
    assert cfg.held_until_field_id is None
    assert cfg.difficulty_field_id is None


def test_extra_headers_parsed() -> None:
    cfg = Config.from_env(env={"REDMINE_HEADERS": "Authorization: Bearer abc, X-Trace-Id: xyz"})
    assert cfg.extra_headers == {"Authorization": "Bearer abc", "X-Trace-Id": "xyz"}


def test_extra_headers_ignores_malformed() -> None:
    cfg = Config.from_env(env={"REDMINE_HEADERS": "no-colon, , X-OK: yes"})
    assert cfg.extra_headers == {"X-OK": "yes"}


def test_allowed_directories_list() -> None:
    cfg = Config.from_env(env={"REDMINE_MCP_ALLOWED_DIRECTORIES": "/tmp,/srv/uploads,/var/data"})
    assert cfg.allowed_directories == (
        Path("/tmp"),
        Path("/srv/uploads"),
        Path("/var/data"),
    )


def test_log_level_uppercased() -> None:
    cfg = Config.from_env(env={"REDMINE_MCP_LOG_LEVEL": "debug"})
    assert cfg.log_level == "DEBUG"


def test_require_api_key_raises_when_missing() -> None:
    cfg = Config(api_key=None)
    with pytest.raises(RuntimeError, match="API key not configured"):
        cfg.require_api_key()


def test_require_api_key_returns_value() -> None:
    cfg = Config(api_key="present")
    assert cfg.require_api_key() == "present"


def test_api_key_loaded_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDMINE_API_KEY", "direct-env-key")
    cfg = Config.from_env(env={"REDMINE_API_KEY": "direct-env-key"})
    assert cfg.api_key == "direct-env-key"


def test_enable_passthrough_default_false(tmp_path: Path) -> None:
    cfg = Config.from_env(env={})
    assert cfg.enable_passthrough is False


def test_enable_passthrough_truthy_values() -> None:
    for value in ("1", "true", "TRUE", "yes", "on"):
        cfg = Config.from_env(env={"REDMINE_MCP_ENABLE_PASSTHROUGH": value})
        assert cfg.enable_passthrough is True, value


def test_enable_passthrough_falsey_values() -> None:
    for value in ("", "0", "false", "no", "off"):
        cfg = Config.from_env(env={"REDMINE_MCP_ENABLE_PASSTHROUGH": value})
        assert cfg.enable_passthrough is False, value


# ---- OAuth2 bearer token ----


def test_oauth_token_loaded_from_env_var(tmp_path: Path) -> None:
    """REDMINE_OAUTH_TOKEN env var lands on Config.oauth_token."""
    cfg = Config.from_env(
        env={
            "REDMINE_OAUTH_TOKEN": "doorkeeper-issued-token",
        }
    )
    assert cfg.oauth_token == "doorkeeper-issued-token"
    assert cfg.api_key is None


def test_require_auth_headers_prefers_oauth_bearer() -> None:
    """When both creds are set, OAuth bearer wins."""
    cfg = Config(api_key="api-K", oauth_token="oauth-T")
    headers = cfg.require_auth_headers()
    assert headers == {"Authorization": "Bearer oauth-T"}
    assert "X-Redmine-API-Key" not in headers


def test_require_auth_headers_falls_back_to_api_key() -> None:
    """API key works as the sole credential when no OAuth token is set."""
    cfg = Config(api_key="api-K")
    headers = cfg.require_auth_headers()
    assert headers == {"X-Redmine-API-Key": "api-K"}


def test_require_auth_headers_oauth_only_works() -> None:
    """OAuth bearer alone is sufficient — no api_key needed."""
    cfg = Config(oauth_token="oauth-T")
    headers = cfg.require_auth_headers()
    assert headers == {"Authorization": "Bearer oauth-T"}


def test_require_auth_headers_raises_when_both_missing() -> None:
    cfg = Config()
    with pytest.raises(RuntimeError, match="No Redmine credentials configured"):
        cfg.require_auth_headers()


# ---- TLS / SSL options ----


def test_tls_verify_defaults_on() -> None:
    cfg = Config.from_env(env={})
    assert cfg.ssl_verify is True
    assert cfg.ca_bundle is None
    assert cfg.verify_tls() is True


def test_tls_verify_can_be_disabled() -> None:
    for value in ("0", "false", "no", "off"):
        cfg = Config.from_env(env={"REDMINE_MCP_SSL_VERIFY": value})
        assert cfg.ssl_verify is False, value
        assert cfg.verify_tls() is False


def test_tls_ca_bundle_overrides_verify() -> None:
    cfg = Config.from_env(
        env={"REDMINE_MCP_SSL_VERIFY": "false", "REDMINE_MCP_CA_BUNDLE": "/etc/ssl/redmine-ca.pem"}
    )
    assert cfg.ca_bundle == "/etc/ssl/redmine-ca.pem"
    assert cfg.verify_tls() == "/etc/ssl/redmine-ca.pem"


def test_tls_blank_ca_bundle_is_none() -> None:
    cfg = Config.from_env(env={"REDMINE_MCP_CA_BUNDLE": "   "})
    assert cfg.ca_bundle is None
