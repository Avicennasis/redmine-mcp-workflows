"""Host allow-list guard for the configured Redmine URL.

An operator can pin the set of hostnames the client may talk to via
``REDMINE_MCP_ALLOWED_HOSTS``. With no allow-list configured the guard is
inert (current behaviour). With one configured, a request to a host outside
the list is refused before any credential is sent.

This is a host-name check, not DNS pinning: it does not by itself stop a
name that resolves to a private IP (DNS rebinding). Its purpose is to stop
the client being pointed at an arbitrary/unexpected host; genuine rebinding
protection needs a pinned resolver, which this server does not have.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from urllib.parse import urlparse


def host_of(url: str) -> str | None:
    """Return the lowercased hostname of ``url`` (no port), or ``None``."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.hostname
    return host.lower() if host else None


def is_private_literal(host: str | None) -> bool:
    """True when ``host`` is a literal loopback/private/link-local IP."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a name, not an IP literal
    return ip.is_private or ip.is_loopback or ip.is_link_local


def host_allowed(url: str, allowed_hosts: Iterable[str]) -> bool:
    """True when ``url``'s host is in ``allowed_hosts``.

    An empty allow-list permits everything. ``"*"`` permits everything too.
    """
    allowed = {h.strip().lower() for h in allowed_hosts if h and h.strip()}
    if not allowed or "*" in allowed:
        return True
    host = host_of(url)
    if host is None:
        return False
    return host in allowed
