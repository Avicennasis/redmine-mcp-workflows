"""Status-transition validator backed by reactive observations.

Two queries:

  - :func:`is_disallowed`: did we observe this exact transition fail before?
  - :func:`allowed_next`: which transitions has this role observed succeeding?

Both walk every supplied role id (Redmine permissions are role-union),
plus role ``0`` which captures "global admin / no project membership"
observations.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..cache.schema_db import SchemaCache


@dataclass
class DisallowedHit:
    """A cached observation of a disallowed transition."""

    role_id: int
    observed_at: int
    observation_count: int
    last_error_text: str | None


def _role_set(role_ids: Iterable[int]) -> list[int]:
    """Always include role 0 (global-admin / no-membership) in lookups."""
    seen: list[int] = []
    for r in (*role_ids, 0):
        if r not in seen:
            seen.append(r)
    return seen


def is_disallowed(
    cache: SchemaCache,
    *,
    tracker_id: int,
    role_ids: Iterable[int],
    from_status_id: int,
    to_status_id: int,
) -> DisallowedHit | None:
    """Return a hit if any role observed this transition as disallowed.

    The most-recent observation across roles wins. Returns ``None`` if no
    role has any observation (success or failure) for this transition —
    the caller should let the API decide.
    """
    best: DisallowedHit | None = None
    for rid in _role_set(role_ids):
        obs = cache.get_workflow_observation(
            tracker_id=tracker_id,
            role_id=rid,
            from_status_id=from_status_id,
            to_status_id=to_status_id,
        )
        if obs is None or obs["outcome"] != "disallowed":
            continue
        if best is None or obs["observed_at"] > best.observed_at:
            best = DisallowedHit(
                role_id=rid,
                observed_at=obs["observed_at"],
                observation_count=obs["observation_count"],
                last_error_text=obs.get("last_error_text"),
            )
    return best


def allowed_next(
    cache: SchemaCache,
    *,
    tracker_id: int,
    role_ids: Iterable[int],
    from_status_id: int,
) -> list[int]:
    """Return all to-status-ids that some matching role has observed as allowed."""
    role_pool = set(_role_set(role_ids))
    out: list[int] = []
    for o in cache.list_workflow_observations(tracker_id=tracker_id):
        if o["from_status_id"] != from_status_id:
            continue
        if o["role_id"] not in role_pool:
            continue
        if o["outcome"] != "allowed":
            continue
        if o["to_status_id"] not in out:
            out.append(o["to_status_id"])
    return out


def has_any_observation(
    cache: SchemaCache,
    *,
    tracker_id: int,
    role_ids: Iterable[int],
    from_status_id: int,
    to_status_id: int,
) -> bool:
    """True iff *any* matching role has any observation (allowed or disallowed) for this transition.

    Useful as a "should we trust the cache?" check — if no observation
    exists, callers should let the API decide rather than guess.
    """
    role_pool = _role_set(role_ids)
    for rid in role_pool:
        if cache.get_workflow_observation(
            tracker_id=tracker_id,
            role_id=rid,
            from_status_id=from_status_id,
            to_status_id=to_status_id,
        ) is not None:
            return True
    return False
