"""Prompt-injection boundary tags for user-controlled Redmine content.

Redmine data is attacker-influenceable: anyone who can file an issue, comment,
edit a wiki page, or attach a file can write text that an LLM later reads as
part of a tool response. Wrapping that text in explicit delimiters marks it as
*data* rather than instructions — a low-cost, structural mitigation.

The tags are XML-style on purpose: models are well-trained to treat such tags
as boundaries and not as free-text instructions. Wrapping is idempotent and
only applies to non-empty strings, so re-fetching already-wrapped content (or
content that happens to contain the marker) does not double-wrap.
"""

from __future__ import annotations

from typing import Any

USER_CONTENT_BEGIN = "<redmine_user_content>"
USER_CONTENT_END = "</redmine_user_content>"

# Response fields that carry Redmine user-authored content. Deliberately a
# small allowlist: wrapping system/derived fields (ids, statuses, counts, urls)
# would add noise and could confuse structural parsing.
USER_CONTENT_FIELDS = frozenset(
    {
        "description",
        "notes",
        "comments",
        "summary",
        "text",
        "content",
    }
)


def wrap_user_content(value: str) -> str:
    """Wrap a user-authored string in boundary tags (idempotent)."""
    if not value:
        return value
    stripped = value.strip()
    if stripped.startswith(USER_CONTENT_BEGIN) and stripped.endswith(USER_CONTENT_END):
        return value
    return f"{USER_CONTENT_BEGIN}\n{value}\n{USER_CONTENT_END}"


def wrap_user_content_fields(value: Any) -> Any:
    """Recursively wrap user-content fields in a tool response structure.

    Walks dicts and lists; every string value under a key in
    :data:`USER_CONTENT_FIELDS` is wrapped. Non-string values (``None``,
    numbers, nested structures) are left untouched.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and key in USER_CONTENT_FIELDS:
                out[key] = wrap_user_content(item)
            else:
                out[key] = wrap_user_content_fields(item)
        return out
    if isinstance(value, list):
        return [wrap_user_content_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(wrap_user_content_fields(item) for item in value)
    return value
