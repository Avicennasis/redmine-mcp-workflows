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

REDACTED = "***REDACTED***"

# Header-shaped patterns: keep the header name, replace the secret.
_HEADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(X-Redmine-API-Key\s*:\s*)(\S+)", re.IGNORECASE),
    re.compile(r"(Authorization\s*:\s*Bearer\s+)(\S+)", re.IGNORECASE),
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with auth headers and known secret values masked."""
    for pattern in _HEADER_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


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
        return True


def install_redaction(secrets: Iterable[str] = ()) -> RedactingFilter:
    """Attach a :class:`RedactingFilter` to every root handler.

    Idempotent per call site: returns the installed filter so callers can
    keep a reference. Safe to call when no handlers are configured yet —
    it also installs on the root logger directly.
    """
    filt = RedactingFilter(secrets)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(filt)
    return filt
