"""SQLite connection setup and forward-only migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from localmem import config

SCHEMA_VERSION_KEY = "schema_version"

# WAL is a persistent database property; busy_timeout and foreign_keys are
# per-connection and therefore re-applied on every connect.
_CONNECTION_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)

MigrationStep = Callable[[sqlite3.Connection], None]


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection to ``path`` with localmem's PRAGMAs and row factory.

    Transactions are managed explicitly (see :func:`transaction`), so the
    connection runs in autocommit mode.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _CONNECTION_PRAGMAS:
        conn.execute(pragma)
    return conn


def open_database(path: Path | None = None) -> sqlite3.Connection:
    """Resolve, create, migrate and return a ready-to-use connection.

    Args:
        path: explicit database file; defaults to :func:`config.resolve_db_path`.
    """
    target = path if path is not None else config.resolve_db_path()
    config.ensure_db_parent(target)
    conn = connect(target)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside a ``BEGIN IMMEDIATE`` transaction.

    The write lock is taken up front so concurrent agents serialize on
    read-then-write sequences instead of racing.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the applied schema version, or 0 for a database with no schema yet.

    Raises:
        RuntimeError: if ``meta.schema_version`` holds a non-integer value.
    """
    meta_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if meta_exists is None:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (SCHEMA_VERSION_KEY,)).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except ValueError as exc:
        raise RuntimeError(
            f"meta.{SCHEMA_VERSION_KEY} holds a non-integer value {row['value']!r}; "
            "the database looks corrupt — inspect it or point LOCALMEM_DB elsewhere"
        ) from exc


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration newer than the stored version and return the new version.

    Idempotent: running against an already-current database performs no writes.
    """
    current = schema_version(conn)
    pending = [(version, step) for version, step in _MIGRATIONS if version > current]
    if not pending:
        return current
    with transaction(conn):
        for version, step in pending:
            step(conn)
            _set_schema_version(conn, version)
    return pending[-1][0]


def _apply_initial_schema(conn: sqlite3.Connection) -> None:
    script = resources.files("localmem").joinpath("schema.sql").read_text(encoding="utf-8")
    for statement in _iter_statements(script):
        conn.execute(statement)


def _iter_statements(script: str) -> Iterator[str]:
    """Yield complete SQL statements, keeping ``CREATE TRIGGER`` bodies intact."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    remainder = buffer.strip()
    if remainder:
        raise RuntimeError(f"schema.sql ends with an incomplete statement: {remainder[:80]!r}")


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION_KEY, str(version)),
    )


# Forward-only, ordered by version. A later milestone appends one tuple here.
_MIGRATIONS: tuple[tuple[int, MigrationStep], ...] = ((1, _apply_initial_schema),)
