"""Round-trip check: the hand-written SQL migrations in ``migrations/`` must
produce the same schema as the SQLAlchemy model (``Base.metadata`` via
``init_db``).

The migrations target Postgres and use a couple of Postgres-only spellings that
SQLite cannot run verbatim:

* ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` — SQLite has no ``IF NOT EXISTS``
  on ``ADD COLUMN``. We strip the clause and emulate its idempotency by skipping
  the statement when the column already exists (migrations 003/004 re-add columns
  that the current 001 already creates, exactly the case the clause guards).

Everything else in the migrations (table DDL, plain indexes, the partial UNIQUE
index ``uq_active_root``, ``BOOLEAN ... DEFAULT FALSE``) is portable and runs on
SQLite as written, so the comparison below is exhaustive for column sets and
covers uniqueness *enforcement*.

One reflection subtlety drives the introspection choice. The migrations express
some uniqueness via inline column ``UNIQUE`` (e.g. ``jira_issue_key VARCHAR(64)
UNIQUE``). SQLite implements those as implicit ``sqlite_autoindex_*`` indexes,
but SQLAlchemy's reflection *hides* them — so reflecting the migrated DB would
silently drop ``mattermost_post_id`` / ``jira_issue_key`` / ``incident_post_id``
uniqueness and the comparison would be apples-to-oranges. We therefore read the
migrated schema with raw ``PRAGMA`` (which sees the autoindexes) and the model
schema with SQLAlchemy reflection (whose metadata exposes the same constraints),
then compare the resulting column-tuple sets. Nothing is silently truncated.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Inspector, inspect

from mm_jira_bot.repository import create_database_engine, init_db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# Columns/column-groups whose uniqueness must be enforced in *both* schemas,
# regardless of whether the form is a UNIQUE constraint, a UNIQUE index, or an
# inline column UNIQUE.
REQUIRED_UNIQUE_COLSETS = {
    frozenset({"mattermost_post_id"}),
    frozenset({"jira_issue_key"}),
    frozenset({"incident_post_id"}),
    frozenset({"alert_signature", "mattermost_channel_id"}),  # uq_active_root
}

# Named indexes the migrations and the model must agree on for the
# episode-tracking machinery (incl. the partial-unique uq_active_root).
REQUIRED_NAMED_INDEXES = {
    "uq_active_root",
    "ix_alert_tickets_alert_signature",
    "ix_alert_tickets_signature_channel",
    "ix_alert_tickets_root_post_id",
}

_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+COLUMN\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<column>\w+)",
    re.IGNORECASE,
)
_IF_NOT_EXISTS_RE = re.compile(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS", re.IGNORECASE)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _iter_statements(sql: str) -> Iterator[str]:
    """Yield non-empty SQL statements, stripping ``--`` line comments first.

    The migrations contain no semicolons inside string literals, so a naive
    split on ``;`` is safe here.
    """
    cleaned = _LINE_COMMENT_RE.sub("", sql)
    for raw in cleaned.split(";"):
        statement = raw.strip()
        if statement:
            yield statement


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _sqlite_index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Explicitly named indexes (excludes implicit ``sqlite_autoindex_*``)."""
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {row[1] for row in rows if not row[1].startswith("sqlite_autoindex_")}


def _sqlite_unique_colsets(conn: sqlite3.Connection, table: str) -> set[frozenset[str]]:
    """Every column-group with a uniqueness guarantee, including the implicit
    ``sqlite_autoindex_*`` indexes backing inline column UNIQUE constraints."""
    groups: set[frozenset[str]] = set()
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        name, unique = row[1], row[2]
        if not unique:
            continue
        cols = [info[2] for info in conn.execute(f"PRAGMA index_info({name})").fetchall()]
        groups.add(frozenset(cols))
    return groups


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply every ``migrations/*.sql`` in filename order to ``conn``.

    Postgres-only ``ADD COLUMN IF NOT EXISTS`` is rewritten to plain
    ``ADD COLUMN`` and skipped when the column already exists, emulating the
    idempotency the clause provides on Postgres.
    """
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert files, f"no migration files found in {MIGRATIONS_DIR}"
    for path in files:
        for statement in _iter_statements(path.read_text()):
            match = _ADD_COLUMN_RE.match(statement)
            if match:
                table = match.group("table")
                column = match.group("column")
                if column in _sqlite_columns(conn, table):
                    continue
                statement = _IF_NOT_EXISTS_RE.sub("ADD COLUMN", statement)
            conn.execute(statement)
    conn.commit()


def _model_columns(inspector: Inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def _model_unique_colsets(inspector: Inspector, table: str) -> set[frozenset[str]]:
    """Uniqueness for the model schema, collapsing unique indexes and unique
    constraints into the set of column groups they cover."""
    groups: set[frozenset[str]] = set()
    for index in inspector.get_indexes(table):
        if index.get("unique"):
            groups.add(frozenset(c for c in index["column_names"] if c is not None))
    for constraint in inspector.get_unique_constraints(table):
        groups.add(frozenset(constraint["column_names"]))
    return groups


def _model_index_names(inspector: Inspector, table: str) -> set[str]:
    return {name for index in inspector.get_indexes(table) if (name := index["name"])}


def test_migrations_match_model_schema(tmp_path) -> None:
    # 1. Schema built from the hand-written migrations (raw sqlite3 + PRAGMA, so
    #    the implicit unique autoindexes remain visible).
    migrated = sqlite3.connect(":memory:")
    try:
        _apply_migrations(migrated)
        mig_tables = _sqlite_tables(migrated)
        mig_columns = {t: _sqlite_columns(migrated, t) for t in mig_tables}
        mig_unique = _sqlite_unique_colsets(migrated, "alert_tickets")
        mig_index_names = _sqlite_index_names(migrated, "alert_tickets")
    finally:
        migrated.close()

    # 2. Schema built from the SQLAlchemy model's own DDL (init_db).
    db_path = tmp_path / "model.sqlite"
    engine = create_database_engine(f"sqlite:///{db_path}")
    try:
        init_db(engine)
        inspector = inspect(engine)
        model_tables = set(inspector.get_table_names())
        model_columns = {t: _model_columns(inspector, t) for t in model_tables}
        model_unique = _model_unique_colsets(inspector, "alert_tickets")
        model_index_names = _model_index_names(inspector, "alert_tickets")
    finally:
        engine.dispose()

    # Same set of tables.
    assert (
        mig_tables
        == model_tables
        == {
            "alert_tickets",
            "alert_feedback",
            "app_settings",
        }
    ), f"table sets differ: migrations={mig_tables} model={model_tables}"

    # Identical column sets per table — the strongest, fully portable check.
    for table in sorted(model_tables):
        assert mig_columns[table] == model_columns[table], (
            f"column sets differ for {table!r}: "
            f"migrations-only={mig_columns[table] - model_columns[table]} "
            f"model-only={model_columns[table] - mig_columns[table]}"
        )

    # Uniqueness enforcement matches (form-agnostic) for alert_tickets, and every
    # business-key / episode column-group is unique in both schemas.
    assert mig_unique == model_unique, (
        f"unique column groups differ for alert_tickets: "
        f"migrations={mig_unique} model={model_unique}"
    )
    assert mig_unique >= REQUIRED_UNIQUE_COLSETS, (
        f"migrations miss required unique groups: {REQUIRED_UNIQUE_COLSETS - mig_unique}"
    )
    assert model_unique >= REQUIRED_UNIQUE_COLSETS, (
        f"model misses required unique groups: {REQUIRED_UNIQUE_COLSETS - model_unique}"
    )

    # The episode-tracking indexes (incl. the partial unique uq_active_root) are
    # present by name in both schemas.
    assert mig_index_names >= REQUIRED_NAMED_INDEXES, (
        f"migrations miss named indexes: {REQUIRED_NAMED_INDEXES - mig_index_names}"
    )
    assert model_index_names >= REQUIRED_NAMED_INDEXES, (
        f"model misses named indexes: {REQUIRED_NAMED_INDEXES - model_index_names}"
    )
