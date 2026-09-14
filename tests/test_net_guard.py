"""Tests for the host allow-list guard."""

from __future__ import annotations

from redmine_mcp.net_guard import host_allowed, host_of, is_private_literal


def test_host_of_extracts_lowercased_hostname() -> None:
    assert host_of("https://Trouble.Example:8443/x") == "trouble.example"
    assert host_of("not a url") is None


def test_empty_allowlist_permits_everything() -> None:
    assert host_allowed("https://anything.example", ()) is True


def test_star_permits_everything() -> None:
    assert host_allowed("https://anything.example", ("*",)) is True


def test_allowlist_accepts_listed_host() -> None:
    assert host_allowed("https://trouble.example/x", ("trouble.example",)) is True


def test_allowlist_rejects_unlisted_host() -> None:
    assert host_allowed("https://evil.example/x", ("trouble.example",)) is False


def test_allowlist_is_case_and_whitespace_insensitive() -> None:
    assert host_allowed("https://Trouble.Example", (" Trouble.Example ",)) is True


def test_private_literal_detection() -> None:
    assert is_private_literal("127.0.0.1") is True
    assert is_private_literal("10.0.0.5") is True
    assert is_private_literal("192.168.1.1") is True
    assert is_private_literal("8.8.8.8") is False
    assert is_private_literal("redmine.example") is False
    assert is_private_literal(None) is False
