"""Unit tests for credential redaction in log output."""

from __future__ import annotations

import logging
import sys

from redmine_mcp.logging_utils import (
    REDACTED,
    RedactingFilter,
    install_redaction,
    redact,
    redact_value,
)


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


def test_filter_redacts_exception_traceback() -> None:
    try:
        raise RuntimeError("request failed with token=topsecret")
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        "x",
        logging.ERROR,
        __file__,
        1,
        "request failed",
        (),
        exc_info,
    )
    RedactingFilter(("topsecret",)).filter(record)
    rendered = logging.Formatter("%(message)s").format(record)
    assert "topsecret" not in rendered
    assert f"token={REDACTED}" in rendered


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


def test_redact_query_param_credentials() -> None:
    out = redact("GET http://redmine.example/issues.json?key=abc123&limit=5")
    assert "abc123" not in out
    assert f"key={REDACTED}" in out
    # The URL and non-secret params survive.
    assert "http://redmine.example/issues.json" in out
    assert "limit=5" in out


def test_redact_common_credential_params() -> None:
    for text in ("token=xyz", "password=hunter2", "access_token=abc", "client_secret=shh"):
        out = redact(text)
        assert REDACTED in out
        assert out != text


def test_redact_basic_auth_header() -> None:
    out = redact("Authorization: Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in out
    assert f"Authorization: Basic {REDACTED}" in out


def test_redact_preserves_urls_hosts_and_paths() -> None:
    text = "authelia at https://auth.gateway.simmons.systems blocked /issue_statuses.json"
    assert redact(text) == text


def test_redact_value_recurses_into_structures() -> None:
    payload = {
        "status_code": 401,
        "body": {"errors": ["X-Redmine-API-Key: leaky-key"]},
        "url": "http://redmine.example/issues.json?key=leaky-key",
        "codes": [500, "token=leaky"],
    }
    out = redact_value(payload)
    assert "leaky-key" not in str(out)
    assert "leaky" not in str(out)
    # Non-secret structure is preserved.
    assert out["status_code"] == 401
    assert out["url"].startswith("http://redmine.example/issues.json")
    assert out["codes"][0] == 500


def test_redact_value_known_secret_recurses() -> None:
    out = redact_value({"a": ["bare-deadbeef"]}, secrets=["bare-deadbeef"])
    assert out == {"a": [REDACTED]}
