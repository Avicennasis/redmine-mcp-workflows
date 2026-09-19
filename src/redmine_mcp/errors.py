"""Structured error types for MCP tool responses.

Every validation failure surfaces as a JSON-serializable dict via
``StructuredError.as_dict()``. Specific subclasses set their ``error``
kind and extend with extra fields. Tools render these via
``json.dumps(err.as_dict())``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StructuredError:
    """Base for any error rendered to an MCP caller."""

    error: str = "unknown_error"
    hint: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"error": self.error}
        if self.hint:
            d["hint"] = self.hint
        d.update(self.extra)
        return d


# ---------------------------------------------------------------------
# API-layer error
# ---------------------------------------------------------------------


class RedmineAPIError(Exception):
    """Raised when the Redmine REST API returns a non-2xx response.

    Carries enough context to render a structured MCP error.
    """

    def __init__(self, status_code: int, body: Any, *, hint: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.hint = hint
        super().__init__(f"Redmine API error {status_code}: {body!r}")

    def as_structured(self) -> dict[str, Any]:
        err = StructuredError(
            error=f"redmine_api_{self.status_code}",
            hint=self.hint or _hint_for_status(self.status_code, self.body),
            extra={"status_code": self.status_code, "body": self.body},
        )
        return err.as_dict()


# Hosts/paths that mean "an identity provider answered this request". Only
# consulted for an HTML 401, so a broad token like "/auth" is safe here.
_AUTH_PROXY_MARKERS = (
    "auth.gateway",
    "authelia",
    "/auth",
    "sso.",
    "oauth",
    "keycloak",
    "login.microsoftonline",
)


def _looks_like_proxy_denial(body: Any) -> bool:
    """True when a 401 body is an HTML redirect to an identity provider.

    Redmine's own 401 is JSON (``{"error": ...}``) or a short text reason. An
    HTML ``<a href>`` pointing at an auth host means a reverse proxy answered
    and the API key was never examined — the case where "check your API key"
    sends you after a credential that is working fine.
    """
    if not isinstance(body, str):
        return False
    lowered = body.lower()
    if "<a href=" not in lowered or "unauthorized" not in lowered:
        return False
    return any(marker in lowered for marker in _AUTH_PROXY_MARKERS)


def _hint_for_status(code: int, body: Any = None) -> str:
    if code == 401:
        if _looks_like_proxy_denial(body):
            return (
                "A reverse proxy (e.g. Authelia) in front of Redmine refused this "
                "path — the API key itself is probably fine. Redmine's global-"
                "enumeration endpoints (/issue_statuses.json, /roles.json, "
                "/users/current.json) are commonly blocked for API clients even when "
                "/issues/* works, which breaks any operation that needs them — notably "
                "close_issue, which resolves the closed status from /issue_statuses. "
                "Check the proxy's access rules for Redmine's API paths, or point "
                "REDMINE_URL at a loopback that bypasses the proxy."
            )
        return "Authentication failed. Check REDMINE_API_KEY."
    if code == 403:
        return "Access denied. The current user lacks permission for this operation."
    if code == 404:
        return "Resource not found. Verify the issue/project/tracker ID."
    if code == 422:
        return "Validation failed server-side. Inspect 'body' for the field-level reason."
    if 500 <= code < 600:
        return "Redmine server error. Retry; if persistent, check Redmine logs."
    return ""


# ---------------------------------------------------------------------
# Validation-layer errors (Phase 3+)
# ---------------------------------------------------------------------


@dataclass
class ReadOnlyModeError(StructuredError):
    """Returned when a write tool is invoked under REDMINE_MCP_READ_ONLY=true."""

    error: str = "read_only_mode"
    hint: str = (
        "This server is running in read-only mode "
        "(REDMINE_MCP_READ_ONLY=true). Unset the env var to enable writes."
    )


@dataclass
class WorkflowTransitionDisallowed(StructuredError):
    """Cache has previously observed this transition as disallowed."""

    error: str = "workflow_transition_disallowed"

    def __init__(
        self,
        *,
        tracker: str,
        from_status: str,
        to_status: str,
        user_role: str | None = None,
        allowed_next_states: list[str] | None = None,
        observation_basis: str = "learned",
        last_error_text: str | None = None,
        observed_at: int | None = None,
    ) -> None:
        super().__init__(error="workflow_transition_disallowed")
        if allowed_next_states:
            self.hint = f"Try one of: {', '.join(allowed_next_states)}."
        else:
            self.hint = (
                "No allowed next states have been observed yet. "
                "Confirm the transition is valid in your Redmine workflow."
            )
        self.extra = {
            "tracker": tracker,
            "from_status": from_status,
            "to_status": to_status,
            "user_role": user_role,
            "allowed_next_states": allowed_next_states or [],
            "observation_basis": observation_basis,
        }
        if last_error_text:
            self.extra["last_error_text"] = last_error_text
        if observed_at is not None:
            self.extra["observed_at"] = observed_at


@dataclass
class RequiredFieldMissing(StructuredError):
    """A field marked required by the tracker (or by basic create rules) is empty."""

    error: str = "required_field_missing"

    def __init__(self, *, field_name: str, op: str = "create") -> None:
        super().__init__(error="required_field_missing")
        self.hint = f"Field {field_name!r} is required for {op}."
        self.extra = {"field": field_name, "op": op}


@dataclass
class CustomFieldUnknown(StructuredError):
    """A custom_fields entry references an id not in the tracker schema."""

    error: str = "custom_field_unknown"

    def __init__(
        self,
        *,
        field_id: int | str,
        tracker: str | None = None,
        known_ids: list[int] | None = None,
    ) -> None:
        super().__init__(error="custom_field_unknown")
        self.hint = (
            f"Custom field id {field_id!r} is not registered"
            f"{' on tracker ' + tracker if tracker else ''}. "
            "Use redmine_describe_tracker to list known fields."
        )
        self.extra = {"field_id": field_id, "tracker": tracker}
        if known_ids:
            self.extra["known_ids"] = known_ids


@dataclass
class CustomFieldShapeError(StructuredError):
    """A custom_fields entry has the wrong shape."""

    error: str = "custom_field_shape_error"

    def __init__(self, *, entry: Any, reason: str) -> None:
        super().__init__(error="custom_field_shape_error")
        self.hint = (
            f"Each custom_fields entry must be a dict with 'id' and 'value'. Reason: {reason}"
        )
        self.extra = {"entry": entry, "reason": reason}


@dataclass
class CustomFieldValueInvalid(StructuredError):
    """A custom-field value is not one of the field's cached enum values.

    Raised only when the field's ``possible_values`` are known (a ``list``
    custom field that has been cached). Values are matched case-insensitively
    and auto-corrected to the canonical casing before this fires, so a hit
    here means the value matches no possible value even ignoring case (or
    matches more than one ambiguously).
    """

    error: str = "custom_field_value_invalid"

    def __init__(
        self,
        *,
        field_id: int,
        field_name: str | None = None,
        value: Any,
        possible_values: list[str],
        reason: str = "no_match",
    ) -> None:
        super().__init__(error="custom_field_value_invalid")
        label = f" {field_name!r}" if field_name else f" id {field_id}"
        if reason == "ambiguous":
            self.hint = (
                f"Value {value!r} matches multiple values of custom field"
                f"{label} ignoring case; pass the exact casing."
            )
        else:
            self.hint = (
                f"Value {value!r} is not valid for custom field{label}. "
                "Case-insensitive matching found no candidate. "
                "Use redmine_list_custom_fields to see the accepted values."
            )
        self.extra = {
            "field_id": field_id,
            "value": value,
            "possible_values": possible_values,
            "reason": reason,
        }


@dataclass
class RoleNotAuthorized(StructuredError):
    """The current user lacks an expected role for this operation."""

    error: str = "role_not_authorized"

    def __init__(self, *, required: list[str], current: list[str]) -> None:
        super().__init__(error="role_not_authorized")
        self.hint = f"Operation requires one of {required}; current roles: {current}."
        self.extra = {"required_roles": required, "current_roles": current}


@dataclass
class TimeEntryHoursInvalid(StructuredError):
    """A time-entry ``hours`` value didn't parse to a non-negative number."""

    error: str = "time_entry_hours_invalid"

    def __init__(self, *, value: Any, reason: str) -> None:
        super().__init__(error="time_entry_hours_invalid")
        self.hint = (
            f"hours value {value!r} is invalid: {reason}. Accepted formats: "
            "decimal (e.g. 2.5), 'H:MM' (e.g. '2:30'), or non-negative numeric."
        )
        self.extra = {"value": value, "reason": reason}


@dataclass
class AttachmentPathDenied(StructuredError):
    """An attachment upload or download was rejected based on the path."""

    error: str = "attachment_path_denied"

    def __init__(
        self,
        *,
        path: str,
        allowed_directories: list[str],
        reason: str = "outside_allowlist",
    ) -> None:
        super().__init__(error="attachment_path_denied")
        if reason == "not_a_file":
            self.hint = (
                f"{path!r} is not a regular file (missing, a directory, or a broken symlink)."
            )
        elif reason == "parent_missing":
            self.hint = (
                f"Parent directory of {path!r} does not exist (or isn't a "
                "directory). Create it before downloading."
            )
        elif reason == "exists_no_overwrite":
            self.hint = f"{path!r} already exists. Pass overwrite=True to replace it."
        else:
            self.hint = (
                f"{path!r} resolves outside the configured allowed directories. "
                "Set REDMINE_MCP_ALLOWED_DIRECTORIES (comma-separated) to expand."
            )
        self.extra = {
            "path": path,
            "allowed_directories": allowed_directories,
            "reason": reason,
        }


@dataclass
class IssueHeld(StructuredError):
    """A close attempt was rejected because the issue has a non-empty Held field."""

    error: str = "issue_held"

    def __init__(
        self,
        *,
        issue_id: int,
        held_reason: str,
        held_until: str | None = None,
    ) -> None:
        super().__init__(error="issue_held")
        date_suffix = f" (held until {held_until})" if held_until else ""
        self.hint = f'Cannot close #{issue_id}: held — "{held_reason}"{date_suffix}'
        self.extra = {
            "issue_id": issue_id,
            "held_reason": held_reason,
        }
        if held_until is not None:
            self.extra["held_until"] = held_until


@dataclass
class HeldReasonRequired(StructuredError):
    """``held=True`` was passed without a reason string.

    The Held custom field is free text that humans read to decide whether a
    hold is still valid.  A bare truthy flag used to write the literal ``"1"``,
    which is indistinguishable from "held for reasons nobody recorded" and
    silently destroyed any prose already in the field.  Requiring the reason
    at the call site is what keeps the field answerable.
    """

    error: str = "held_reason_required"

    def __init__(self) -> None:
        super().__init__(error="held_reason_required")
        self.hint = (
            'Pass held="<why this ticket is held>" instead of held=True. '
            "The Held field is prose a human reads later; a bare flag records nothing "
            "and overwrites any existing reason. Use held=False to clear a hold."
        )


__all__ = [
    "StructuredError",
    "RedmineAPIError",
    "ReadOnlyModeError",
    "WorkflowTransitionDisallowed",
    "RequiredFieldMissing",
    "CustomFieldUnknown",
    "CustomFieldShapeError",
    "CustomFieldValueInvalid",
    "RoleNotAuthorized",
    "AttachmentPathDenied",
    "TimeEntryHoursInvalid",
    "HeldReasonRequired",
    "IssueHeld",
]
