"""SQLite connection setup and forward-only migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from localmem import config

SCHEMA_VERSION_KEY = "schema_version"

#: Mode for a database file this process creates: owner only. An existing file is never
#: touched, including a custom ``$LOCALMEM_DB`` path (``docs/design_decisions.md`` §32).
DB_FILE_MODE = 0o600

#: The files SQLite puts next to the database in WAL mode. It creates them with the
#: database file's own mode, on the *first write* rather than at connect time — which is
#: why the mode is tightened before :func:`migrate` runs.
_SIDECAR_SUFFIXES = ("-wal", "-shm")

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

    ``sqlite3.connect`` succeeds against any file; a corrupt one only fails on the
    first statement, which is ``PRAGMA journal_mode=WAL``. The setup is therefore
    guarded so the half-built connection is closed rather than dropped unreferenced.
    The original exception propagates untouched — callers match on ``sqlite3.Error``.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        for pragma in _CONNECTION_PRAGMAS:
            conn.execute(pragma)
    except BaseException:
        conn.close()
        raise
    return conn


def open_database(path: Path | None = None) -> sqlite3.Connection:
    """Resolve, create, migrate and return a ready-to-use connection.

    A database file this call brings into existence is restricted to
    :data:`DB_FILE_MODE`; one that was already there is left exactly as the user set it.

    Args:
        path: explicit database file; defaults to :func:`config.resolve_db_path`.
    """
    target = path if path is not None else config.resolve_db_path()
    config.ensure_db_parent(target)
    created = not target.exists()
    conn = connect(target)
    try:
        if created:
            _restrict_new_database(target)
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _restrict_new_database(target: Path) -> None:
    """Tighten a just-created database file, before anything writes to it.

    Order is load-bearing. SQLite creates ``-wal`` and ``-shm`` on the first write and
    copies the database file's mode onto them, so tightening the file first means the
    sidecars are born restricted; tightening it afterwards leaves them world-readable.
    Measured on this machine at umask 022: no chmod → ``644 644 644``; chmod before the
    first write → ``600 600 600``; chmod after it → ``600 644 644``.

    Sidecars are swept as well, for the one case that ordering cannot cover: a stale
    ``-wal`` left behind by a crash whose database file was then deleted.
    """
    config.restrict_mode(target, DB_FILE_MODE)
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = target.with_name(target.name + suffix)
        if sidecar.exists():
            config.restrict_mode(sidecar, DB_FILE_MODE)


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


def _add_recall_tracking(conn: sqlite3.Connection) -> None:
    """v2 — record how often a memory is actually recalled.

    Two columns on ``memories``: ``recalled_count``, which every existing row starts at
    0, and ``last_recalled_at``, nullable because "never recalled" is not a timestamp.
    ``schema.sql`` stays the version-1 baseline; a v1 database reaches version 2 by
    replaying this step, exactly as the forward-only design intends.
    """
    conn.execute("ALTER TABLE memories ADD COLUMN recalled_count INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE memories ADD COLUMN last_recalled_at TEXT")


def _add_keywords(conn: sqlite3.Connection) -> None:
    """v3 — index agent-supplied keywords alongside content.

    ``memories.keywords`` holds the normalized, space-separated keyword string
    :func:`localmem.store.add_memory` writes; it is NULL for every row that predates this
    migration and for every row written without keywords, so a keyword-free database
    ranks exactly as it did in v0.2.2.

    FTS5 has no ``ALTER`` for a virtual table, so ``memories_fts`` is dropped and
    recreated with the second column. The tokenizer string is copied verbatim from
    ``schema.sql`` — ``remove_diacritics 2`` is what makes ``dung pnpm`` find ``dùng
    pnpm``, and any drift here silently unteaches that.

    All three triggers are recreated carrying the extra column. Two details are
    load-bearing:

    * the delete-side rows pass the OLD value of **every** indexed column. An
      external-content FTS5 index cannot verify a delete, so a wrong value there corrupts
      the index with no error;
    * ``mem_au`` stays narrow — ``AFTER UPDATE OF content, keywords``. The recall path's
      ``recalled_count`` bump (:func:`localmem.retriever._record_recall`) depends on that
      list to cost no index maintenance.

    The final ``'rebuild'`` repopulates the new index from ``memories``. It is the only
    part of this step whose cost scales with the database, and it is small: measured at
    under 10 ms for 5000 rows on this machine. It is paid once, synchronously, on the
    first ``open_database`` after the upgrade — including inside the MCP server, which
    opens the database on every tool call, so one tool call pays it and the rest do not.
    """
    conn.execute("ALTER TABLE memories ADD COLUMN keywords TEXT")
    conn.execute("DROP TABLE memories_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE memories_fts USING fts5("
        "content, "
        "keywords, "
        "content='memories', "
        "content_rowid='id', "
        "tokenize='unicode61 remove_diacritics 2')"
    )
    conn.execute("DROP TRIGGER IF EXISTS mem_ai")
    conn.execute("DROP TRIGGER IF EXISTS mem_ad")
    conn.execute("DROP TRIGGER IF EXISTS mem_au")
    conn.execute(
        "CREATE TRIGGER mem_ai AFTER INSERT ON memories BEGIN "
        "INSERT INTO memories_fts(rowid, content, keywords) "
        "VALUES (new.id, new.content, new.keywords); "
        "END"
    )
    conn.execute(
        "CREATE TRIGGER mem_ad AFTER DELETE ON memories BEGIN "
        "INSERT INTO memories_fts(memories_fts, rowid, content, keywords) "
        "VALUES ('delete', old.id, old.content, old.keywords); "
        "END"
    )
    conn.execute(
        "CREATE TRIGGER mem_au AFTER UPDATE OF content, keywords ON memories BEGIN "
        "INSERT INTO memories_fts(memories_fts, rowid, content, keywords) "
        "VALUES ('delete', old.id, old.content, old.keywords); "
        "INSERT INTO memories_fts(rowid, content, keywords) "
        "VALUES (new.id, new.content, new.keywords); "
        "END"
    )
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")


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


# Forward-only, ordered by version. A later milestone appends one tuple here; nothing is
# ever edited in place, because a database that already ran a step will never run it again.
_MIGRATIONS: tuple[tuple[int, MigrationStep], ...] = (
    (1, _apply_initial_schema),
    (2, _add_recall_tracking),
    (3, _add_keywords),
)

#: The version a freshly opened database ends up at. Derived, never typed twice.
CURRENT_SCHEMA_VERSION = _MIGRATIONS[-1][0]
