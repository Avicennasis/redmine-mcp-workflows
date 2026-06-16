"""Environment-variable configuration parsing.

All config is via env vars (no config file in v0.1). See README for the
canonical list. ``Config.from_env()`` is the canonical entry point; tests
can construct ``Config`` directly to avoid env-var pollution.
"""

from __future__ import annotations

import os
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
            extra_headers=_parse_headers(e.get("REDMINE_HEADERS")),
            allowed_directories=_parse_directories(e.get("REDMINE_MCP_ALLOWED_DIRECTORIES")),
            log_level=e.get("REDMINE_MCP_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
        )

    def require_api_key(self) -> str:
        """Return the API key, raising a clear error if missing.

        Retained for back-compat with callers that explicitly want the API
        key path. New code should prefer :meth:`require_auth_headers`,
        which handles both API key and OAuth2 bearer token.
        """
        if not self.api_key:
            raise RuntimeError(
                "Redmine API key not configured. Set REDMINE_API_KEY env var "
                "(e.g. in ~/.bash_secrets)."
            )
        return self.api_key

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
            "(preferred) or REDMINE_API_KEY env var (e.g. in ~/.bash_secrets)."
        )
