"""Regression tests for Redmine ticket #47969.

``server._get_cache()`` used to call ``cfg.require_api_key()``
unconditionally, so an OAuth-only deployment (only ``REDMINE_OAUTH_TOKEN``
set, no ``REDMINE_API_KEY``) blew up with ``RuntimeError('Redmine API key
not configured')`` on the *first* tool call — before any HTTP request was
made — because the API key was being consumed purely as a cache-fingerprint
input.

These tests exercise the real ``_get_cache()`` in ``server.py`` (module
globals ``_config`` / ``_cache`` are reset around each test so the lazy
init actually runs) and read the persisted fingerprint back out of the
SQLite ``cache_meta`` table to prove which credential it was derived from.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from redmine_mcp import server
from redmine_mcp.config import Config

_FP_KEY = "auth_fingerprint_sha256"


@pytest.fixture(autouse=True)
def reset_server_globals():
    """Force the lazy init in _get_cache() to actually run for each test."""
    saved_config, saved_cache = server._config, server._cache
    server._config, server._cache = None, None
    try:
        yield
    finally:
        # Close whatever cache this test built so no SQLite handle leaks,
        # then restore the pre-test module state.
        if server._cache is not None:
            server._cache.close()
        server._config, server._cache = saved_config, saved_cache


def _fingerprint_of(secret: str) -> str:
    """Mirror SchemaCache._fingerprint — sha256 hex of the credential."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _persisted_fingerprint(cache: server.SchemaCache) -> str:
    """Read the fingerprint _get_cache() wrote, straight from SQLite."""
    with cache._lock:
        row = cache._conn.execute(
            "SELECT value FROM cache_meta WHERE key = ?", (_FP_KEY,)
        ).fetchone()
    assert row is not None, "auth fingerprint was never persisted"
    return row["value"]


def test_api_key_only_builds_cache(tmp_path: Path) -> None:
    """api_key configured, no oauth token -> cache builds, fp from api_key."""
    server._config = Config(api_key="api-K", cache_dir=tmp_path)
    cache = server._get_cache()
    assert cache is not None
    assert _persisted_fingerprint(cache) == _fingerprint_of("api-K")


def test_oauth_token_only_builds_cache(tmp_path: Path) -> None:
    """THE BUG (#47969): oauth token set, no api_key -> cache must build.

    Before the fix this raised RuntimeError('Redmine API key not
    configured') from require_api_key().
    """
    server._config = Config(oauth_token="bearer-T", cache_dir=tmp_path)
    cache = server._get_cache()
    assert cache is not None
    # Fingerprint must come from the oauth token — the credential the
    # client actually sends — not from a missing API key.
    assert _persisted_fingerprint(cache) == _fingerprint_of("bearer-T")


def test_no_credentials_raises_naming_both_options(tmp_path: Path) -> None:
    """Neither credential -> loud failure naming both credential types."""
    server._config = Config(cache_dir=tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        server._get_cache()
    message = str(excinfo.value)
    assert "REDMINE_OAUTH_TOKEN" in message
    assert "REDMINE_API_KEY" in message
    # No unauthenticated cache may be left behind for a later call to reuse.
    assert server._cache is None


def test_fingerprint_differs_between_two_credentials(tmp_path: Path) -> None:
    """Two different identities must not share a cache fingerprint.

    A shared fingerprint would serve one user's permission-shaped schema to
    another — a worse bug than the one being fixed. Verify both that the
    persisted digests differ and, independently, that reconcile_auth()
    actually wipes cached rows when the identity changes.
    """
    server._config = Config(api_key="api-K1", cache_dir=tmp_path)
    first = _persisted_fingerprint(server._get_cache())

    server._cache = None  # simulate a restart under a different credential
    server._config = Config(api_key="api-K2", cache_dir=tmp_path)
    second = _persisted_fingerprint(server._get_cache())

    assert first != second

    # Same db, new identity -> the cached schema is discarded, not inherited.
    restarted = server._get_cache()
    restarted.put_tracker(1, "Bug", {"x": 1})
    server._cache = None
    server._config = Config(oauth_token="bearer-T", cache_dir=tmp_path)
    assert server._get_cache().get_tracker(1) is None


def test_reused_cache_instance_is_returned(tmp_path: Path) -> None:
    """Sanity: the lazy init memoizes, so repeated calls are idempotent."""
    server._config = Config(oauth_token="bearer-T", cache_dir=tmp_path)
    assert server._get_cache() is server._get_cache()


def test_sqlite_read_is_via_the_public_connection() -> None:
    """Guard: the fingerprint really lives in cache_meta (not in RAM only).

    Opens a second, independent connection to the db file to confirm the
    fingerprint is durable — a RAM-only fingerprint would survive an
    identity change across restarts, which is exactly the leak class
    reconcile_auth exists to prevent.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dpath = Path(d)
        server._config = Config(oauth_token="bearer-T", cache_dir=dpath)
        cache = server._get_cache()
        cache.close()

        conn = sqlite3.connect(str(dpath / "schema.db"))
        try:
            row = conn.execute("SELECT value FROM cache_meta WHERE key = ?", (_FP_KEY,)).fetchone()
        finally:
            conn.close()
    assert row is not None
    assert row[0] == _fingerprint_of("bearer-T")
