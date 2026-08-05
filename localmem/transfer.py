"""Backup and multi-machine transfer: raw ``memories`` rows out, raw rows back in.

Raw over derived, the same principle the store itself is built on. The export carries
the ``memories`` table and nothing else:

* ``entities`` / ``memory_entities`` are **derived** — :func:`restore` rebuilds them with
  the ordinary backfill, so a stale graph can never travel between machines;
* ``dedup_queue`` is **transient** — an unreviewed near-duplicate is a local judgement
  call about a local pair of ids, and ids do not survive the trip.

Copying ``memory.db`` by hand is the alternative, and it is only safe with every agent
stopped: WAL keeps recent commits in a ``-wal`` sidecar, so a half-copied pair of files
is a corrupt database. Export/restore has no such window.

Both directions are idempotent. Restoring the same document twice changes nothing the
second time, and restoring into a database that already holds some of the rows merges
rather than duplicates.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from localmem import __version__, db, dedup, indexer, store
from localmem.config import validate_workspace

#: Identifies the document so a stray JSON file is refused with a useful message.
EXPORT_FORMAT = "localmem-export"

#: Bumped only when the *shape* changes; the row payload is just the table's columns.
EXPORT_FORMAT_VERSION = 1

#: The document key holding the row array.
MEMORIES_KEY = "memories"

# `SELECT *` on purpose: "every column of `memories`" is the contract, so a column added
# by a later migration travels without this module being edited. Restore reads the
# columns it knows by name and ignores the rest, which is what makes a newer export
# usable by an older localmem.
_EXPORT_SQL = "SELECT * FROM memories"
_EXPORT_WHERE = " WHERE workspace = ?"
_EXPORT_ORDER_BY = " ORDER BY id ASC"

_NOW_SQL = "SELECT datetime('now') AS now"
_COUNT_SQL = "SELECT COUNT(*) AS n FROM memories"

# `id` is deliberately absent: row ids are local to a database, and forcing them in would
# collide with whatever the target already stores. `superseded_by` is absent for the same
# reason — it holds an id, it is reserved schema with no logic behind it in v0.2, and
# carrying a remapped-away reference would be worse than carrying none.
_RESTORE_SQL = """
INSERT INTO memories
    (content, content_hash, workspace, kind, source, session_id,
     seen_count, created_at, updated_at, recalled_count, last_recalled_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(workspace, content_hash) DO UPDATE
   SET seen_count = max(memories.seen_count, excluded.seen_count)
"""


@dataclass(frozen=True)
class RestoreOutcome:
    """What one restore did.

    Attributes:
        total: records read from the document.
        added: records stored as new rows.
        merged: records that matched an existing ``(workspace, content_hash)`` and only
            lifted its ``seen_count``.
        links_created: entity links the follow-up backfill rebuilt.
    """

    total: int
    added: int
    merged: int
    links_created: int


def export_document(conn: sqlite3.Connection, workspace: str | None = None) -> dict[str, Any]:
    """Return every ``memories`` row of ``workspace`` as one JSON-ready document.

    Args:
        conn: an open connection.
        workspace: restrict to one workspace; ``None`` exports all of them.

    Raises:
        ValueError: if ``workspace`` is empty or whitespace only.
    """
    sql = _EXPORT_SQL
    params: list[object] = []
    if workspace is not None:
        sql += _EXPORT_WHERE
        params.append(validate_workspace(workspace))
    rows = conn.execute(sql + _EXPORT_ORDER_BY, params).fetchall()
    return {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "localmem_version": __version__,
        "exported_at": conn.execute(_NOW_SQL).fetchone()["now"],
        "workspace": workspace,
        MEMORIES_KEY: [dict(row) for row in rows],
    }


def render(document: dict[str, Any]) -> str:
    """Return ``document`` as the JSON text ``export`` writes."""
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)


def read_document(path: Path) -> list[dict[str, Any]]:
    """Return the memory records held in the export file at ``path``.

    Raises:
        OSError: if the file cannot be read.
        UnicodeDecodeError: if it is not valid UTF-8.
        ValueError: if it is not a localmem export document.
    """
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("format") != EXPORT_FORMAT:
        raise ValueError(
            f"{path} is not a localmem export document — expected a JSON object with "
            f'"format": "{EXPORT_FORMAT}", as `localmem export` writes'
        )
    records = parsed.get(MEMORIES_KEY)
    if not isinstance(records, list):
        raise ValueError(f"{path} has no {MEMORIES_KEY!r} array to restore")
    return [_validated(record, path) for record in records]


def restore(conn: sqlite3.Connection, records: Sequence[dict[str, Any]]) -> RestoreOutcome:
    """Merge ``records`` into the database and rebuild the entity graph.

    An existing row wins every column except ``seen_count``, which becomes the larger of
    the two: the target's ``created_at``, ``kind`` and ``source`` are its own history and
    a restore is not entitled to rewrite them. That is also what makes a second restore
    of the same document a no-op.

    ``content_hash`` is recomputed rather than trusted, so a hand-edited document cannot
    smuggle in a row that dedup will never match again.

    Raises:
        sqlite3.Error: if the write fails; the whole restore rolls back.
        ValueError: if a record carries a blank workspace.
    """
    if not records:
        return RestoreOutcome(total=0, added=0, merged=0, links_created=0)

    with db.transaction(conn):
        now = conn.execute(_NOW_SQL).fetchone()["now"]
        before = _row_count(conn)
        for record in records:
            conn.execute(_RESTORE_SQL, _values(record, now))
        added = _row_count(conn) - before

    _processed, links_created = indexer.backfill_all(conn)
    return RestoreOutcome(
        total=len(records),
        added=added,
        merged=len(records) - added,
        links_created=links_created,
    )


def _validated(record: object, path: Path) -> dict[str, Any]:
    """Return one record, checked for the two fields nothing can be reconstructed from."""
    if not isinstance(record, dict):
        raise ValueError(
            f"{path} holds a {type(record).__name__} where a memory object was expected"
        )
    content = record.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{path} holds a memory with no content")
    workspace = record.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError(f"{path} holds a memory with no workspace: {content[:60]!r}")
    return record


def _values(record: dict[str, Any], now: str) -> tuple[object, ...]:
    """Return one record as the bound values :data:`_RESTORE_SQL` expects.

    Every optional column falls back to what a fresh ``localmem add`` would have stored,
    so a hand-written document with only ``content`` and ``workspace`` still restores.
    """
    content = str(record["content"])
    created_at = _text(record.get("created_at")) or now
    return (
        content,
        dedup.content_hash(content),
        validate_workspace(str(record["workspace"])),
        _text(record.get("kind")) or store.DEFAULT_KIND,
        _text(record.get("source")),
        _text(record.get("session_id")),
        _count(record.get("seen_count"), default=1),
        created_at,
        _text(record.get("updated_at")) or created_at,
        _count(record.get("recalled_count"), default=0),
        _text(record.get("last_recalled_at")),
    )


def _text(value: object) -> str | None:
    """Return ``value`` as text, or ``None`` for anything the column may hold as NULL."""
    return None if value is None else str(value)


def _count(value: object, default: int) -> int:
    """Return ``value`` as a non-negative count, falling back to ``default``.

    ``bool`` is excluded on purpose: it is an ``int`` in Python, and a ``true`` in a
    hand-written document means "yes", not "one".
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(int(value), 0)


def _row_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(_COUNT_SQL).fetchone()["n"])
