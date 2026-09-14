"""Environment-variable configuration parsing.

All config is via env vars (no config file in v0.1). See README for the
canonical list. ``Config.from_env()`` is the canonical entry point; tests
can construct ``Config`` directly to avoid env-var pollution.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_cache_dir

from . import secrets

DEFAULT_REDMINE_URL = "http://127.0.0.1:8281"
DEFAULT_CACHE_TTL_SECONDS = 86400  # 24h
DEFAULT_ALLOWED_DIRECTORIES = ("/tmp",)
DEFAULT_LOG_LEVEL = "INFO"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_optional_int(raw: str | None) -> int | None:
    """Parse an optional integer env var; ``None`` for unset/blank/invalid."""
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_headers(raw: str | None) -> dict[str, str]:
    """Parse a comma-separated ``Header: Value`` string into a dict."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        name, _, val = entry.partition(":")
        out[name.strip()] = val.strip()
    return out


def _parse_directories(raw: str | None) -> tuple[Path, ...]:
    if not raw:
        return tuple(Path(p) for p in DEFAULT_ALLOWED_DIRECTORIES)
    return tuple(Path(p.strip()).expanduser() for p in raw.split(",") if p.strip())


def _parse_names(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated list of tool names into a set."""
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _parse_list(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated list, preserving order, dropping blanks."""
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration.

    Use ``Config.from_env()`` in production; pass kwargs directly in tests.
    """

    redmine_url: str = DEFAULT_REDMINE_URL
    api_key: str | None = None
    oauth_token: str | None = None
    read_only: bool = False
    enable_passthrough: bool = False
    cache_dir: Path = field(default_factory=lambda: Path(user_cache_dir("redmine-mcp")))
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    # Optional explicit custom-field ids for the convenience params. When set,
    # the field is used directly instead of being discovered by (English)
    # name, which breaks silently on a renamed or localized Redmine.
    held_field_id: int | None = None
    held_until_field_id: int | None = None
    difficulty_field_id: int | None = None
    # TLS: verify server certificates (default true). ``ca_bundle`` points at
    # a custom CA file/dir for self-signed or private-CA Redmine deployments;
    # when set it takes precedence over ``ssl_verify``.
    ssl_verify: bool = True
    ca_bundle: str | None = None
    # Fallback issue for time entries logged without an explicit target —
    # routes meetings/admin time to a management issue.
    default_time_issue: int | None = None
    # Tool names to hide from the advertised surface at startup.
    disabled_tools: frozenset[str] = frozenset()
    # Regex patterns: allowlist keeps only matching tools; denylist then
    # removes matching tools (deny wins).
    tool_allowlist: tuple[str, ...] = ()
    tool_denylist: tuple[str, ...] = ()
    extra_headers: dict[str, str] = field(default_factory=dict)
    allowed_directories: tuple[Path, ...] = field(
        default_factory=lambda: tuple(Path(p) for p in DEFAULT_ALLOWED_DIRECTORIES)
    )
    log_level: str = DEFAULT_LOG_LEVEL

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        """Build a Config from a mapping (defaults to ``os.environ``)."""
        e = dict(env if env is not None else os.environ)

        api_key = secrets.load_api_key(e.get("REDMINE_MCP_SECRETS_FILE"), env=e)
        oauth_token = secrets.load_oauth_token(e.get("REDMINE_MCP_SECRETS_FILE"), env=e)

        cache_dir_str = e.get("REDMINE_MCP_CACHE_DIR")
        cache_dir = (
            Path(cache_dir_str).expanduser()
            if cache_dir_str
            else Path(user_cache_dir("redmine-mcp"))
        )

        try:
            cache_ttl = int(e.get("REDMINE_MCP_CACHE_TTL", DEFAULT_CACHE_TTL_SECONDS))
        except ValueError:
            cache_ttl = DEFAULT_CACHE_TTL_SECONDS

        return cls(
            redmine_url=e.get("REDMINE_URL", DEFAULT_REDMINE_URL).rstrip("/"),
            api_key=api_key,
            oauth_token=oauth_token,
            read_only=_truthy(e.get("REDMINE_MCP_READ_ONLY")),
            enable_passthrough=_truthy(e.get("REDMINE_MCP_ENABLE_PASSTHROUGH")),
            cache_dir=cache_dir,
            cache_ttl_seconds=cache_ttl,
            held_field_id=_parse_optional_int(e.get("REDMINE_MCP_HELD_FIELD_ID")),
            held_until_field_id=_parse_optional_int(e.get("REDMINE_MCP_HELD_UNTIL_FIELD_ID")),
            difficulty_field_id=_parse_optional_int(e.get("REDMINE_MCP_DIFFICULTY_FIELD_ID")),
            ssl_verify=True
            if e.get("REDMINE_MCP_SSL_VERIFY") is None
            else _truthy(e.get("REDMINE_MCP_SSL_VERIFY")),
            ca_bundle=(e.get("REDMINE_MCP_CA_BUNDLE") or "").strip() or None,
            default_time_issue=_parse_optional_int(e.get("REDMINE_MCP_DEFAULT_TIME_ISSUE")),
            disabled_tools=_parse_names(e.get("REDMINE_MCP_DISABLED_TOOLS")),
            tool_allowlist=_parse_list(e.get("REDMINE_MCP_TOOL_ALLOWLIST")),
            tool_denylist=_parse_list(e.get("REDMINE_MCP_TOOL_DENYLIST")),
            extra_headers=_parse_headers(e.get("REDMINE_HEADERS")),
            allowed_directories=_parse_directories(e.get("REDMINE_MCP_ALLOWED_DIRECTORIES")),
            log_level=e.get("REDMINE_MCP_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
        )

    def verify_tls(self) -> bool | ssl.SSLContext:
        """Return the value httpx should use for its ``verify`` argument.

        A configured ``ca_bundle`` (custom CA file or directory) wins over
        ``ssl_verify``; otherwise certificate verification is governed by
        ``ssl_verify``. Build an explicit SSL context for custom CAs because
        HTTPX's legacy ``verify=<path string>`` form is deprecated.
        """
        if self.ca_bundle:
            if Path(self.ca_bundle).is_dir():
                return ssl.create_default_context(capath=self.ca_bundle)
            return ssl.create_default_context(cafile=self.ca_bundle)
        return self.ssl_verify

    def require_api_key(self) -> str:
        """Return the API key, raising a clear error if missing.

        Retained for back-compat with callers that explicitly want the API
        key path. New code should prefer :meth:`require_auth_headers`,
        which handles both API key and OAuth2 bearer token.
        """
        if not self.api_key:
            raise RuntimeError(
                "Redmine API key not configured. Set REDMINE_API_KEY env var "
                "(see README for configuration)."
            )
        return self.api_key

    def require_credential(self) -> str:
        """Return whichever raw credential is present, for fingerprinting.

        Resolution order matches :meth:`require_auth_headers` exactly:
          1. ``oauth_token`` (bearer wins when both are configured, because
             that is the credential the HTTP client actually sends)
          2. ``api_key``
          3. neither        -> ``RuntimeError``

        The returned string is a *fingerprint input*, never a header value;
        callers hash it (see ``SchemaCache.reconcile_auth``) so that cached
        schema is never shared across two identities.
        """
        if self.oauth_token:
            return self.oauth_token
        if self.api_key:
            return self.api_key
        raise RuntimeError(
            "No Redmine credentials configured. Set REDMINE_OAUTH_TOKEN "
            "(preferred) or REDMINE_API_KEY env var (see README for configuration)."
        )

    def require_auth_headers(self) -> dict[str, str]:
        """Return the auth headers, preferring OAuth bearer when set.

        Resolution order:
          1. ``oauth_token`` → ``Authorization: Bearer <token>``
          2. ``api_key``    → ``X-Redmine-API-Key: <key>``
          3. neither        → ``RuntimeError``

        Doorkeeper-issued bearer tokens (Redmine 6.1+) wins when both are
        configured — OAuth is the explicit-opt-in path.
        """
        if self.oauth_token:
            return {"Authorization": f"Bearer {self.oauth_token}"}
        if self.api_key:
            return {"X-Redmine-API-Key": self.api_key}
        raise RuntimeError(
            "No Redmine credentials configured. Set REDMINE_OAUTH_TOKEN "
            "(preferred) or REDMINE_API_KEY env var (see README for configuration)."
        )
