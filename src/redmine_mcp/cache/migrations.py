"""Schema-cache table DDL and version stamping.

The cache lives at ``${cache_dir}/schema.db``. ``apply_migrations`` is
idempotent: it brings the database up to ``CURRENT_SCHEMA_VERSION`` from
any prior version (including empty).

A note on ``workflow_transitions``: Redmine's REST API does NOT expose
``/workflows.json`` (returns 403 Forbidden even for global admins as of
Redmine 6.x). The ``outcome`` column lets us cache the **observation**
rather than the authoritative graph: each row records whether a given
``(tracker, role, from, to)`` was last seen as allowed or disallowed when
exercised. See ``docs/workflow-validation.md``.
"""

from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 3

_DDL_V1 = (
    """
    CREATE TABLE IF NOT EXISTS trackers (
        id           INTEGER PRIMARY KEY,
        name         TEXT NOT NULL,
        fetched_at   INTEGER NOT NULL,
        schema_json  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id           INTEGER PRIMARY KEY,
        identifier   TEXT NOT NULL UNIQUE,
        fetched_at   INTEGER NOT NULL,
        schema_json  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_transitions (
        tracker_id        INTEGER NOT NULL,
        role_id           INTEGER NOT NULL,
        from_status_id    INTEGER NOT NULL,
        to_status_id      INTEGER NOT NULL,
        outcome           TEXT NOT NULL CHECK (outcome IN ('allowed','disallowed')),
        observed_at       INTEGER NOT NULL,
        observation_count INTEGER NOT NULL DEFAULT 1,
        last_error_text   TEXT,
        PRIMARY KEY (tracker_id, role_id, from_status_id, to_status_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_workflow_lookup
        ON workflow_transitions (tracker_id, role_id, from_status_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS custom_fields (
        id                     INTEGER PRIMARY KEY,
        name                   TEXT NOT NULL,
        type                   TEXT,
        format                 TEXT,
        regexp                 TEXT,
        possible_values_json   TEXT,
        fetched_at             INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cache_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring the database up to ``CURRENT_SCHEMA_VERSION``."""
    conn.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY)")
    cur = conn.execute("SELECT version FROM _schema_version")
    row = cur.fetchone()
    current = row[0] if row else 0

    if current < 1:
        for stmt in _DDL_V1:
            conn.execute(stmt)
        conn.execute("DELETE FROM _schema_version")
        conn.execute("INSERT INTO _schema_version (version) VALUES (?)", (1,))
        current = 1

    if current < 2:
        # v2 added 'outcome', 'observation_count', 'last_error_text' to
        # workflow_transitions and renamed 'fetched_at' → 'observed_at'.
        # In any pre-existing v1 cache there are no rows in this table
        # yet (Phase 1 didn't populate it), so a drop+recreate is safe.
        conn.execute("DROP TABLE IF EXISTS workflow_transitions")
        for stmt in _DDL_V1:
            if "workflow_transitions" in stmt:
                conn.execute(stmt)
        conn.execute("DELETE FROM _schema_version")
        conn.execute("INSERT INTO _schema_version (version) VALUES (?)", (2,))
        current = 2

    if current < 3:
        # v3 reshapes custom_fields. The v1 table was never populated
        # (no put_ accessor existed), so drop+recreate is safe.
        conn.execute("DROP TABLE IF EXISTS custom_fields")
        conn.execute(
            """
            CREATE TABLE custom_fields (
                field_id                     INTEGER PRIMARY KEY,
                name                         TEXT NOT NULL,
                format_kind                  TEXT NOT NULL,
                is_required                  INTEGER NOT NULL,
                default_value                TEXT,
                possible_values_json         TEXT NOT NULL DEFAULT '[]',
                applicable_tracker_ids_json  TEXT NOT NULL DEFAULT '[]',
                for_all_projects             INTEGER NOT NULL DEFAULT 0,
                updated_at                   INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_custom_fields_name ON custom_fields(name)")
        conn.execute("DELETE FROM _schema_version")
        conn.execute("INSERT INTO _schema_version (version) VALUES (?)", (3,))

    conn.commit()
