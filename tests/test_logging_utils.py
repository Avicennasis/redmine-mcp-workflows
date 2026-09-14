"""Unit tests for credential redaction in log output."""

from __future__ import annotations

import logging

from redmine_mcp.logging_utils import REDACTED, RedactingFilter, install_redaction, redact


def test_redact_api_key_header() -> None:
    out = redact("request headers: X-Redmine-API-Key: super-secret-key")
    assert "super-secret-key" not in out
    assert f"X-Redmine-API-Key: {REDACTED}" in out


def test_redact_bearer_token() -> None:
    out = redact("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert f"Authorization: Bearer {REDACTED}" in out


def test_redact_known_secret_anywhere() -> None:
    out = redact("boom with key deadbeefdeadbeef in the middle", secrets=["deadbeefdeadbeef"])
    assert "deadbeef" not in out
    assert REDACTED in out


def test_redact_leaves_clean_text_alone() -> None:
    assert redact("nothing to hide here") == "nothing to hide here"


def test_filter_rewrites_record_before_formatting() -> None:
    record = logging.LogRecord(
        "x",
        logging.DEBUG,
        __file__,
        1,
        "sending X-Redmine-API-Key: %s",
        ("leaky",),
        None,
    )
    RedactingFilter().filter(record)
    assert record.getMessage() == f"sending X-Redmine-API-Key: {REDACTED}"
    assert record.args == ()


def test_install_redaction_attaches_to_handlers_and_masks() -> None:
    root = logging.getLogger()
    logger = logging.getLogger("redaction-test")
    logger.setLevel(logging.DEBUG)
    captured: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, rec: logging.LogRecord) -> None:
            captured.append(rec)

    handler = Capture()
    root.addHandler(handler)
    try:
        install_redaction(secrets=("topsecret",))
        logger.debug("token=topsecret")
        assert captured
        assert captured[-1].getMessage() == f"token={REDACTED}"
    finally:
        root.removeHandler(handler)
