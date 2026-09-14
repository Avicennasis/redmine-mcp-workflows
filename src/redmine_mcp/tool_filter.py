"""Tool-registration filtering.

Operators can trim the advertised tool surface to cut context overhead and
hide operations a deployment does not want exposed. Filtering is applied once
at startup (see ``server.apply_tool_filter``) against the registered tool
names; a filtered-out tool is removed from ``list_tools()`` and is no longer
callable.

Precedence (most specific wins):
  1. ``disabled`` — exact tool names, always removed.
  2. ``allowlist`` — if set, only names matching at least one pattern stay.
  3. ``denylist`` — names matching any pattern are removed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .tool_categories import filter_by_categories


def filter_tool_names(
    names: Iterable[str],
    *,
    categories: Iterable[str] = (),
    disabled: Iterable[str] = (),
    allowlist: Iterable[str] = (),
    denylist: Iterable[str] = (),
) -> set[str]:
    """Return the subset of ``names`` that survives the configured filters."""
    allowed = filter_by_categories(set(names), categories)

    allow_patterns = [re.compile(p) for p in allowlist if p]
    if allow_patterns:
        allowed = {n for n in allowed if any(p.search(n) for p in allow_patterns)}

    deny_patterns = [re.compile(p) for p in denylist if p]
    if deny_patterns:
        allowed = {n for n in allowed if not any(p.search(n) for p in deny_patterns)}

    disabled_set = {d for d in disabled if d}
    if disabled_set:
        allowed -= disabled_set

    return allowed
