"""Log redaction for credentials.

A :class:`RedactingFilter` attached to root log handlers rewrites any record
whose rendered message contains a Redmine credential — either a known secret
value (the configured API key / OAuth token) or a recognizable auth header
(``X-Redmine-API-Key: …`` / ``Authorization: Bearer …``). It applies at every
log level, including DEBUG, so accidental credential logging is scrubbed
before it reaches a handler's formatter.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

REDACTED = "***REDACTED***"

# Header-shaped patterns: keep the header name, replace the secret.
_HEADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(X-Redmine-API-Key\s*:\s*)(\S+)", re.IGNORECASE),
    re.compile(r"(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)(\S+)", re.IGNORECASE),
)

# Credential-bearing query/body parameters: keep the parameter name, replace
# the value. Deliberately narrow — only key-shaped names whose value is the
# credential — so ordinary URLs, hosts and paths survive intact.
_QUERY_PARAM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"((?:[?&]|\b)(?:key|api[_-]?key|access[_-]?token|refresh[_-]?token"
        r"|client[_-]?secret|auth[_-]?token|token|password|passwd|secret)=)"
        r"([^&\s\"'<>]+)",
        re.IGNORECASE,
    ),
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with auth headers, credential params and known secrets masked.

    Only credential *values* are replaced; URLs, hostnames and paths are left
    alone so error diagnostics (e.g. which proxy or endpoint failed) survive.
    """
    for pattern in (*_HEADER_PATTERNS, *_QUERY_PARAM_PATTERNS):
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively redact every string inside a JSON-shaped value.

    Leaves non-string scalars untouched, so an error ``body`` keeps its
    structure (status codes, field names, URLs) while credential strings are
    scrubbed.
    """
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {key: redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets) for item in value)
    return value


class RedactingFilter(logging.Filter):
    """Rewrite log records to mask credentials before formatting."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message, self._secrets)
        if redacted != message:
            # Collapse to a pre-formatted message so the original args (which
            # may themselves hold the secret) are not re-rendered.
            record.msg = redacted
            record.args = ()
        if record.exc_info is not None:
            # Formatters render exc_info *after* filters run, so redacting only
            # record.getMessage() still leaks credentials embedded in an
            # exception message/traceback. Seed the formatter's cached text
            # with a scrubbed version so every handler emits the safe copy.
            exc_text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(exc_text, self._secrets)
        return True


def install_redaction(secrets: Iterable[str] = ()) -> RedactingFilter:
    """Attach a :class:`RedactingFilter` to every root handler.

    Returns the installed filter so callers can keep a reference. The server
    configures its root handler before calling this function.
    """
    filt = RedactingFilter(secrets)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(filt)
    return filt
