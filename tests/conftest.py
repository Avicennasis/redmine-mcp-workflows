"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test cache dir; redirect REDMINE_MCP_CACHE_DIR for any from_env() callers."""
    monkeypatch.setenv("REDMINE_MCP_CACHE_DIR", str(tmp_path))
    return tmp_path
