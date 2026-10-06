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


def strip_user_content_tags(value: str) -> str:
    """Remove boundary tags from a string that is about to be WRITTEN to Redmine.

    The tags are a read-side marker. A caller that reads a description, edits
    it and writes it back would otherwise store ``<redmine_user_content>`` in
    Redmine — visible to humans, and wrapped again on the next read.

    Two cases, and only these:

    * the tags wrap the whole value (surrounding whitespace allowed) — the
      wrapper and the single newline :func:`wrap_user_content` puts inside each
      tag are removed, so ``strip(wrap(v)) == v`` exactly;
    * the exact tag strings appear elsewhere (a quoted excerpt of a read) —
      each occurrence is removed, with the newline the wrapper put beside it.

    Anything that merely resembles a tag (other case, attributes, escaped
    ``&lt;...&gt;``) is left alone.
    """
    if USER_CONTENT_BEGIN not in value and USER_CONTENT_END not in value:
        return value
    stripped = value.strip()
    if stripped.startswith(USER_CONTENT_BEGIN) and stripped.endswith(USER_CONTENT_END):
        inner = stripped[len(USER_CONTENT_BEGIN) : len(stripped) - len(USER_CONTENT_END)]
        if inner.startswith("\n"):
            inner = inner[1:]
        if inner.endswith("\n"):
            inner = inner[:-1]
        value = inner
    value = value.replace(USER_CONTENT_BEGIN + "\n", "").replace("\n" + USER_CONTENT_END, "")
    return value.replace(USER_CONTENT_BEGIN, "").replace(USER_CONTENT_END, "")


def strip_user_content_tags_deep(value: Any) -> Any:
    """Apply :func:`strip_user_content_tags` to every string in a JSON body.

    Used on every outgoing JSON request body (see ``RedmineClient._request``),
    which is the one path all write tools share — descriptions, notes, wiki
    text, journal edits, subjects, news, forum messages, custom-field values
    and passthrough bodies alike. Keys and non-string values are untouched.
    """
    if isinstance(value, str):
        return strip_user_content_tags(value)
    if isinstance(value, dict):
        return {key: strip_user_content_tags_deep(item) for key, item in value.items()}
    if isinstance(value, list):
        return [strip_user_content_tags_deep(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_user_content_tags_deep(item) for item in value)
    return value
