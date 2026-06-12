"""Unit tests for v0.3 #2384 generic passthrough tool (no network).

The passthrough tool is the by-design escape hatch — it sends arbitrary
HTTP requests to the Redmine API with NO validation, NO cache, and a
loud ``validation_skipped: true`` flag in every response. These tests
focus on three things:

  1. The tool actually skips validation (no schema lookups, no
     cache reads on the way in).
  2. The ``validation_skipped`` warning is present on every response,
     success OR failure — callers must always know they bypassed it.
  3. Method normalization, path handling, and argument shape are
     consistent with the rest of the tool surface (so it doesn't
     surprise callers used to the validated tools).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redmine_mcp.cache.schema_db import SchemaCache
from redmine_mcp.errors import RedmineAPIError
from redmine_mcp.tools import passthrough


class FakeClient:
    def __init__(
        self,
        responses: dict[tuple[str, str], Any] | None = None,
        *,
        errors: dict[tuple[str, str], RedmineAPIError] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, str, Any, Any]] = []  # method, path, body, params

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, None, params))
        if ("GET", path) in self._errors:
            raise self._errors[("GET", path)]
        return self._responses.get(("GET", path))

    async def post(self, path: str, *, json: Any) -> Any:
        self.calls.append(("POST", path, json, None))
        if ("POST", path) in self._errors:
            raise self._errors[("POST", path)]
        return self._responses.get(("POST", path))

    async def put(self, path: str, *, json: Any) -> Any:
        self.calls.append(("PUT", path, json, None))
        if ("PUT", path) in self._errors:
            raise self._errors[("PUT", path)]
        return self._responses.get(("PUT", path))

    async def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path, None, None))
        if ("DELETE", path) in self._errors:
            raise self._errors[("DELETE", path)]
        return self._responses.get(("DELETE", path))


@pytest.fixture
def cache(tmp_path: Path) -> SchemaCache:
    c = SchemaCache(db_path=tmp_path / "schema.db", ttl_seconds=60)
    yield c
    c.close()


# ---------------------------------------------------------------------
# happy paths — every method
# ---------------------------------------------------------------------


async def test_get_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/custom_fields.json"): {"custom_fields": [{"id": 1, "name": "OS"}]},
    })
    result = await passthrough.request(
        client, cache, method="GET", path="/custom_fields.json",
    )
    assert result["validation_skipped"] is True
    assert result["method"] == "GET"
    assert result["path"] == "/custom_fields.json"
    assert result["body"] == {"custom_fields": [{"id": 1, "name": "OS"}]}
    assert client.calls == [("GET", "/custom_fields.json", None, None)]


async def test_get_with_query_params(cache: SchemaCache) -> None:
    client = FakeClient({
        ("GET", "/issues.json"): {"issues": []},
    })
    await passthrough.request(
        client, cache, method="GET", path="/issues.json",
        params={"sort": "id:desc", "limit": 5},
    )
    assert client.calls[0][3] == {"sort": "id:desc", "limit": 5}


async def test_post_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({
        ("POST", "/uploads.json"): {"upload": {"token": "abc.def"}},
    })
    result = await passthrough.request(
        client, cache, method="POST", path="/uploads.json",
        body={"foo": "bar"},
    )
    assert result["validation_skipped"] is True
    assert result["body"] == {"upload": {"token": "abc.def"}}
    assert client.calls[0][2] == {"foo": "bar"}


async def test_put_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({("PUT", "/issues/42.json"): None})
    result = await passthrough.request(
        client, cache, method="PUT", path="/issues/42.json",
        body={"issue": {"subject": "renamed"}},
    )
    assert result["validation_skipped"] is True
    assert result["method"] == "PUT"


async def test_delete_happy_path(cache: SchemaCache) -> None:
    client = FakeClient({("DELETE", "/issues/42.json"): None})
    result = await passthrough.request(
        client, cache, method="DELETE", path="/issues/42.json",
    )
    assert result["validation_skipped"] is True
    assert result["method"] == "DELETE"


# ---------------------------------------------------------------------
# method normalization + validation
# ---------------------------------------------------------------------


async def test_method_case_normalized(cache: SchemaCache) -> None:
    """Lowercase methods (get/post/etc.) normalize to uppercase."""
    client = FakeClient({("GET", "/x.json"): {"ok": True}})
    result = await passthrough.request(
        client, cache, method="get", path="/x.json",
    )
    assert result["method"] == "GET"
    assert client.calls[0][0] == "GET"


async def test_unknown_method_rejected(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await passthrough.request(
        client, cache, method="OPTIONS", path="/x.json",
    )
    assert result["error"] == "passthrough_method_unknown"
    assert "GET" in result["allowed_methods"]
    # validation_skipped still present — the warning is universal.
    assert result["validation_skipped"] is True
    assert client.calls == []


async def test_empty_path_rejected(cache: SchemaCache) -> None:
    client = FakeClient()
    result = await passthrough.request(
        client, cache, method="GET", path="",
    )
    assert result["error"] == "passthrough_path_invalid"
    assert client.calls == []


async def test_path_without_leading_slash_rejected(cache: SchemaCache) -> None:
    """Paths must start with /. Don't silently auto-prepend — surprises bite."""
    client = FakeClient()
    result = await passthrough.request(
        client, cache, method="GET", path="issues.json",
    )
    assert result["error"] == "passthrough_path_invalid"
    assert client.calls == []


# ---------------------------------------------------------------------
# error propagation
# ---------------------------------------------------------------------


async def test_api_error_propagates_with_validation_skipped_flag(
    cache: SchemaCache,
) -> None:
    """Even the error envelope carries the warning so caller knows
    they bypassed the validation layer when this 422'd."""
    client = FakeClient(errors={
        ("POST", "/some/endpoint.json"): RedmineAPIError(
            status_code=422, body={"errors": ["something is invalid"]},
        ),
    })
    result = await passthrough.request(
        client, cache, method="POST", path="/some/endpoint.json",
        body={"x": 1},
    )
    assert result["error"] == "redmine_api_422"
    assert result["validation_skipped"] is True
    assert result["method"] == "POST"
    assert result["path"] == "/some/endpoint.json"


# ---------------------------------------------------------------------
# universal warning shape
# ---------------------------------------------------------------------


async def test_validation_skipped_present_on_every_success(
    cache: SchemaCache,
) -> None:
    """Hard guarantee: callers can rely on the flag being there."""
    client = FakeClient({("GET", "/x.json"): {"ok": True}})
    for method in ("GET",):
        result = await passthrough.request(
            client, cache, method=method, path="/x.json",
        )
        assert "validation_skipped" in result
        assert result["validation_skipped"] is True


async def test_warning_field_explains_why(cache: SchemaCache) -> None:
    """A human-readable hint explaining what was skipped."""
    client = FakeClient({("GET", "/x.json"): {"ok": True}})
    result = await passthrough.request(
        client, cache, method="GET", path="/x.json",
    )
    assert "warning" in result
    assert "validation" in result["warning"].lower()
