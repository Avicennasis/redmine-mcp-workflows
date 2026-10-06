"""docs/tool-catalog.md and the README headline must match the registered tools.

The catalog said 81 tools while 92 were registered: eleven tools were missing,
three were listed twice, and two section counts were wrong. The README is the
documentation of record for the tool surface (CONTRIBUTING.md), so drift is a
bug, and these tests make it a failing one.
"""

from __future__ import annotations

import re
from pathlib import Path

from redmine_mcp import server

ROOT = Path(__file__).resolve().parent.parent
CATALOG = (ROOT / "docs" / "tool-catalog.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
REGISTERED = set(server.mcp._tool_manager._tools)


def _catalog_rows(text: str) -> list[str]:
    return re.findall(r"^\| `(redmine_\w+)`", text, re.M)


def test_catalog_lists_every_registered_tool_exactly_once() -> None:
    rows = _catalog_rows(CATALOG)
    duplicates = sorted({r for r in rows if rows.count(r) > 1})
    assert not duplicates, f"listed more than once: {duplicates}"
    assert set(rows) == REGISTERED, {
        "missing": sorted(REGISTERED - set(rows)),
        "stale": sorted(set(rows) - REGISTERED),
    }


def test_catalog_section_counts_match_their_rows() -> None:
    for section in re.split(r"^## ", CATALOG, flags=re.M)[1:]:
        title = section.splitlines()[0]
        match = re.search(r"\((\d+)\)", title)
        assert match, f"section without a count: {title!r}"
        assert int(match.group(1)) == len(_catalog_rows("\n" + section)), title


def test_catalog_and_readme_headline_counts_are_current() -> None:
    n = len(REGISTERED)
    assert CATALOG.splitlines()[2].startswith(f"{n} tools"), CATALOG.splitlines()[2]
    assert f"**{n} tools" in README
