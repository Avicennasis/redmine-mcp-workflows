"""SQLite-backed schema cache.

The :class:`SchemaCache` is the single entry point. It hides connection
management, migration application, TTL enforcement, and auth-key-change
detection.

Threading note: SQLite connections are opened with
``check_same_thread=False`` and protected by a :class:`threading.Lock`.
The cache is small (a handful of rows × a handful of trackers) so
contention is negligible; correctness over performance.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .migrations import apply_migrations

_AUTH_FINGERPRINT_KEY = "auth_fingerprint_sha256"


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class SchemaCache:
    """Persistent schema cache backed by SQLite."""

    def __init__(self, db_path: Path, *, ttl_seconds: int) -> None:
        self._db_path = db_path
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        apply_migrations(self._conn)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reconcile_auth(self, api_key: str) -> None:
        """If the api-key fingerprint changed, wipe everything cached.

        Caching schema for one user and serving it to another would leak
        permission data. Cheaper to flush than to risk that.
        """
        fp = _fingerprint(api_key)
        with self._lock:
            cur = self._conn.execute(
                "SELECT value FROM cache_meta WHERE key = ?",
                (_AUTH_FINGERPRINT_KEY,),
            )
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO cache_meta (key, value) VALUES (?, ?)",
                    (_AUTH_FINGERPRINT_KEY, fp),
                )
                self._conn.commit()
                return
            if row["value"] != fp:
                self._conn.execute("DELETE FROM trackers")
                self._conn.execute("DELETE FROM projects")
                self._conn.execute("DELETE FROM workflow_transitions")
                self._conn.execute("DELETE FROM custom_fields")
                # Wipe non-fingerprint cache_meta too (statuses, priorities, user info)
                self._conn.execute(
                    "DELETE FROM cache_meta WHERE key != ?",
                    (_AUTH_FINGERPRINT_KEY,),
                )
                self._conn.execute(
                    "UPDATE cache_meta SET value = ? WHERE key = ?",
                    (fp, _AUTH_FINGERPRINT_KEY),
                )
                self._conn.commit()

    # ------------------------------------------------------------------
    # trackers
    # ------------------------------------------------------------------

    def get_tracker(self, tracker_id: int) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT fetched_at, schema_json FROM trackers WHERE id = ?",
                (tracker_id,),
            )
            row = cur.fetchone()
        if row is None or self._is_stale(row["fetched_at"]):
            return None
        return json.loads(row["schema_json"])

    def get_tracker_by_name(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT fetched_at, schema_json FROM trackers WHERE name = ?",
                (name,),
            )
            row = cur.fetchone()
        if row is None or self._is_stale(row["fetched_at"]):
            return None
        return json.loads(row["schema_json"])

    def put_tracker(self, tracker_id: int, name: str, schema: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO trackers (id, name, fetched_at, schema_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    fetched_at = excluded.fetched_at,
                    schema_json = excluded.schema_json
                """,
                (tracker_id, name, int(time.time()), json.dumps(schema)),
            )
            self._conn.commit()

    def list_trackers(self) -> list[dict[str, Any]]:
        """Return all currently-cached trackers (stale entries excluded)."""
        cutoff = int(time.time()) - self._ttl
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, name, schema_json FROM trackers WHERE fetched_at >= ? ORDER BY id",
                (cutoff,),
            )
            return [
                {"id": r["id"], "name": r["name"], **json.loads(r["schema_json"])}
                for r in cur.fetchall()
            ]

    def resolve_tracker(self, ident: int | str) -> int | None:
        """Resolve a tracker reference (id or name) to a numeric id."""
        if isinstance(ident, int):
            return ident
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM trackers WHERE id = ? OR name = ?",
                (_try_int(ident), ident),
            )
            row = cur.fetchone()
        return row["id"] if row else None

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------

    def get_project(self, identifier: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT fetched_at, schema_json FROM projects WHERE identifier = ?",
                (identifier,),
            )
            row = cur.fetchone()
        if row is None or self._is_stale(row["fetched_at"]):
            return None
        return json.loads(row["schema_json"])

    def get_project_by_name(self, name: str) -> dict[str, Any] | None:
        """Look up a cached project by its display name (case-insensitive).

        ``name`` lives inside ``schema_json`` rather than as a column —
        the projects table indexes on ``identifier`` (slug) only. Callers
        of ``redmine_create_issue(project=...)`` often round-trip the
        display name out of ``redmine_get_issue``'s response, which is
        the natural failure surface this method exists to catch.
        """
        target = name.strip().lower()
        if not target:
            return None
        with self._lock:
            cur = self._conn.execute(
                "SELECT fetched_at, schema_json FROM projects",
            )
            rows = cur.fetchall()
        for row in rows:
            if self._is_stale(row["fetched_at"]):
                continue
            schema = json.loads(row["schema_json"])
            if str(schema.get("name", "")).strip().lower() == target:
                return schema
        return None

    def put_project(self, project_id: int, identifier: str, schema: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO projects (id, identifier, fetched_at, schema_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    identifier = excluded.identifier,
                    fetched_at = excluded.fetched_at,
                    schema_json = excluded.schema_json
                """,
                (project_id, identifier, int(time.time()), json.dumps(schema)),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # global enumerations (statuses, priorities, current user) — JSON in cache_meta
    # ------------------------------------------------------------------

    def get_meta_json(self, key: str) -> Any | None:
        """Read a JSON blob from cache_meta, honoring TTL via a sibling timestamp key."""
        ts_key = f"{key}__ts"
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, value FROM cache_meta WHERE key IN (?, ?)",
                (key, ts_key),
            )
            rows = {r["key"]: r["value"] for r in cur.fetchall()}
        if key not in rows or ts_key not in rows:
            return None
        try:
            ts = int(rows[ts_key])
        except (TypeError, ValueError):
            return None
        if self._is_stale(ts):
            return None
        try:
            return json.loads(rows[key])
        except json.JSONDecodeError:
            return None

    def put_meta_json(self, key: str, value: Any) -> None:
        with self._lock:
            now = int(time.time())
            self._conn.executemany(
                """
                INSERT INTO cache_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                [(key, json.dumps(value)), (f"{key}__ts", str(now))],
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # workflow observations — cache learns the graph from API outcomes
    # ------------------------------------------------------------------

    def record_workflow_observation(
        self,
        *,
        tracker_id: int,
        role_id: int,
        from_status_id: int,
        to_status_id: int,
        outcome: str,
        error_text: str | None = None,
    ) -> None:
        """Record (or refresh) a single observed workflow transition.

        ``outcome`` must be ``"allowed"`` or ``"disallowed"``.
        Repeat observations bump ``observation_count``; an outcome flip
        replaces the row (resetting count to 1).
        """
        if outcome not in {"allowed", "disallowed"}:
            raise ValueError(f"outcome must be 'allowed' or 'disallowed', got {outcome!r}")
        with self._lock:
            now = int(time.time())
            cur = self._conn.execute(
                """
                SELECT outcome, observation_count FROM workflow_transitions
                WHERE tracker_id = ? AND role_id = ?
                  AND from_status_id = ? AND to_status_id = ?
                """,
                (tracker_id, role_id, from_status_id, to_status_id),
            )
            existing = cur.fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO workflow_transitions
                        (tracker_id, role_id, from_status_id, to_status_id,
                         outcome, observed_at, observation_count, last_error_text)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        tracker_id, role_id, from_status_id, to_status_id,
                        outcome, now, error_text,
                    ),
                )
            elif existing["outcome"] == outcome:
                self._conn.execute(
                    """
                    UPDATE workflow_transitions SET
                        observed_at = ?,
                        observation_count = observation_count + 1,
                        last_error_text = COALESCE(?, last_error_text)
                    WHERE tracker_id = ? AND role_id = ?
                      AND from_status_id = ? AND to_status_id = ?
                    """,
                    (
                        now, error_text,
                        tracker_id, role_id, from_status_id, to_status_id,
                    ),
                )
            else:
                # Flip the outcome — reset the count.
                self._conn.execute(
                    """
                    UPDATE workflow_transitions SET
                        outcome = ?,
                        observed_at = ?,
                        observation_count = 1,
                        last_error_text = ?
                    WHERE tracker_id = ? AND role_id = ?
                      AND from_status_id = ? AND to_status_id = ?
                    """,
                    (
                        outcome, now, error_text,
                        tracker_id, role_id, from_status_id, to_status_id,
                    ),
                )
            self._conn.commit()

    def get_workflow_observation(
        self,
        *,
        tracker_id: int,
        role_id: int,
        from_status_id: int,
        to_status_id: int,
    ) -> dict[str, Any] | None:
        """Return ``{outcome, observation_count, observed_at, last_error_text}`` or ``None``."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT outcome, observation_count, observed_at, last_error_text
                FROM workflow_transitions
                WHERE tracker_id = ? AND role_id = ?
                  AND from_status_id = ? AND to_status_id = ?
                """,
                (tracker_id, role_id, from_status_id, to_status_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def list_workflow_observations(
        self,
        *,
        tracker_id: int,
        role_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List all observations for a tracker (optionally filtered by role)."""
        params: tuple[Any, ...] = (tracker_id,)
        sql = (
            "SELECT tracker_id, role_id, from_status_id, to_status_id, "
            "outcome, observation_count, observed_at, last_error_text "
            "FROM workflow_transitions WHERE tracker_id = ?"
        )
        if role_id is not None:
            sql += " AND role_id = ?"
            params = (tracker_id, role_id)
        sql += " ORDER BY from_status_id, to_status_id"
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # custom fields
    # ------------------------------------------------------------------

    def put_custom_field(
        self,
        *,
        field_id: int,
        name: str,
        format_kind: str,
        is_required: bool,
        default_value: str | None,
        possible_values: list[str],
        applicable_tracker_ids: list[int],
        for_all_projects: bool,
    ) -> None:
        """Upsert a single custom-field record."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO custom_fields (
                    field_id, name, format_kind, is_required, default_value,
                    possible_values_json, applicable_tracker_ids_json,
                    for_all_projects, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(field_id) DO UPDATE SET
                    name=excluded.name,
                    format_kind=excluded.format_kind,
                    is_required=excluded.is_required,
                    default_value=excluded.default_value,
                    possible_values_json=excluded.possible_values_json,
                    applicable_tracker_ids_json=excluded.applicable_tracker_ids_json,
                    for_all_projects=excluded.for_all_projects,
                    updated_at=excluded.updated_at
                """,
                (
                    field_id, name, format_kind, 1 if is_required else 0,
                    default_value, json.dumps(possible_values),
                    json.dumps(applicable_tracker_ids),
                    1 if for_all_projects else 0, int(time.time()),
                ),
            )
            self._conn.commit()

    def get_custom_field(self, field_id: int) -> dict[str, Any] | None:
        """Return one custom field by numeric id, or None."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT field_id, name, format_kind, is_required, default_value, "
                "possible_values_json, applicable_tracker_ids_json, for_all_projects "
                "FROM custom_fields WHERE field_id = ?",
                (field_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_custom_field(row)

    def get_custom_field_by_name(self, name: str) -> dict[str, Any] | None:
        """Look up a custom field by exact name (case-sensitive)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT field_id, name, format_kind, is_required, default_value, "
                "possible_values_json, applicable_tracker_ids_json, for_all_projects "
                "FROM custom_fields WHERE name = ?",
                (name,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_custom_field(row)

    def list_custom_fields(self, *, tracker_id: int | None = None) -> list[dict[str, Any]]:
        """List cached custom fields, optionally filtered by tracker applicability.

        An empty ``applicable_tracker_ids`` list means "applies to every
        tracker" (Redmine's convention when the field is global).
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT field_id, name, format_kind, is_required, default_value, "
                "possible_values_json, applicable_tracker_ids_json, for_all_projects "
                "FROM custom_fields ORDER BY name"
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            field = _row_to_custom_field(row)
            if tracker_id is not None:
                applicable = field["applicable_tracker_ids"]
                if applicable and tracker_id not in applicable:
                    continue
            out.append(field)
        return out

    # ------------------------------------------------------------------
    # invalidation
    # ------------------------------------------------------------------

    def invalidate(self, scope: str = "all") -> dict[str, int]:
        """Invalidate cached entries.

        Scope syntax:
          * ``"all"``: drop every cached row (preserves auth fingerprint)
          * ``"tracker:<id-or-name>"``: drop one tracker (and its workflow rows)
          * ``"project:<id-or-slug>"``: drop one project

        Returns a dict of ``{table: rows_deleted}``.
        """
        deleted: dict[str, int] = {}
        with self._lock:
            if scope == "all":
                for table in ("trackers", "projects", "workflow_transitions", "custom_fields"):
                    cur = self._conn.execute(f"DELETE FROM {table}")
                    deleted[table] = cur.rowcount
                # Wipe meta JSON blobs but keep the auth fingerprint.
                cur = self._conn.execute(
                    "DELETE FROM cache_meta WHERE key != ?",
                    (_AUTH_FINGERPRINT_KEY,),
                )
                deleted["cache_meta"] = cur.rowcount
            elif scope.startswith("tracker:"):
                ident = scope.split(":", 1)[1]
                tracker_id = self._resolve_tracker_id_locked(ident)
                if tracker_id is not None:
                    cur = self._conn.execute("DELETE FROM trackers WHERE id = ?", (tracker_id,))
                    deleted["trackers"] = cur.rowcount
                    cur = self._conn.execute(
                        "DELETE FROM workflow_transitions WHERE tracker_id = ?",
                        (tracker_id,),
                    )
                    deleted["workflow_transitions"] = cur.rowcount
                else:
                    deleted["trackers"] = 0
            elif scope.startswith("project:"):
                ident = scope.split(":", 1)[1]
                cur = self._conn.execute(
                    "DELETE FROM projects WHERE identifier = ? OR id = ?",
                    (ident, _try_int(ident)),
                )
                deleted["projects"] = cur.rowcount
            else:
                raise ValueError(
                    f"unknown invalidate scope {scope!r}; "
                    "expected 'all' | 'tracker:<id-or-name>' | 'project:<id-or-slug>'"
                )
            self._conn.commit()
        return deleted

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _is_stale(self, fetched_at: int) -> bool:
        return (int(time.time()) - fetched_at) > self._ttl

    def _resolve_tracker_id_locked(self, ident: str) -> int | None:
        """Resolve tracker id from either int or name. Caller must hold _lock."""
        as_int = _try_int(ident)
        cur = self._conn.execute(
            "SELECT id FROM trackers WHERE id = ? OR name = ?",
            (as_int, ident),
        )
        row = cur.fetchone()
        return row["id"] if row else None


def _try_int(s: str | int | None) -> int | None:
    try:
        return int(s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _row_to_custom_field(row: sqlite3.Row) -> dict[str, Any]:
    """Shape a custom_fields row into the dict the rest of the code expects."""
    return {
        "id": row["field_id"],
        "name": row["name"],
        "format_kind": row["format_kind"],
        "is_required": bool(row["is_required"]),
        "default_value": row["default_value"],
        "possible_values": json.loads(row["possible_values_json"]),
        "applicable_tracker_ids": json.loads(row["applicable_tracker_ids_json"]),
        "for_all_projects": bool(row["for_all_projects"]),
    }
