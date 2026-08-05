"""The always-load tier: ``kind='core'`` rows, concatenated and token-capped.

Core memory is attached to every recall, so it is the one part of the pipeline that
must never fail a query. Database errors are contained here and reported as an empty
core memory rather than propagated.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from localmem.tokens import estimate_tokens

CORE_KIND = "core"

# PLAN.md §5 step 7: hard cap, expressed in estimated tokens.
CORE_MEMORY_TOKEN_CAP = 400

CORE_MEMORY_JOINER = "\n"

_SELECT_CORE_SQL = "SELECT content FROM memories WHERE kind = ?"
_CORE_ORDER_BY = " ORDER BY created_at ASC, id ASC"
_SELECT_CORE_WORKSPACES_SQL = "SELECT DISTINCT workspace FROM memories WHERE kind = ?"


@dataclass(frozen=True)
class CoreMemory:
    """The assembled core-memory string plus what it cost to fit it under the cap.

    Attributes:
        text: the rows joined by newlines, already truncated to fit the cap.
        tokens: the estimated token count of ``text``.
        dropped: how many whole rows were discarded, oldest first, to fit the cap.
    """

    text: str
    tokens: int
    dropped: int


def build_core_memory(
    conn: sqlite3.Connection,
    workspace: str | None,
    cap: int = CORE_MEMORY_TOKEN_CAP,
) -> CoreMemory:
    """Return the capped core memory of ``workspace`` (``None`` spans every workspace).

    Rows are ordered oldest first and joined with newlines. When the result exceeds
    ``cap`` estimated tokens, **whole rows are dropped from the front** — a row is never
    split mid-text, so what survives is always a set of complete memories. A single row
    that is larger than the cap on its own is therefore dropped entirely.

    Never raises: an unreadable database yields an empty core memory.
    """
    sql = _SELECT_CORE_SQL
    params: list[object] = [CORE_KIND]
    if workspace is not None:
        sql += " AND workspace = ?"
        params.append(workspace)
    contents = [row["content"] for row in _query(conn, sql + _CORE_ORDER_BY, params)]

    dropped = 0
    while contents and estimate_tokens(CORE_MEMORY_JOINER.join(contents)) > cap:
        contents.pop(0)
        dropped += 1
    text = CORE_MEMORY_JOINER.join(contents)
    return CoreMemory(text=text, tokens=estimate_tokens(text), dropped=dropped)


def core_memory_totals(
    conn: sqlite3.Connection, cap: int = CORE_MEMORY_TOKEN_CAP
) -> tuple[int, int]:
    """Return ``(estimated tokens, dropped rows)`` summed over every workspace.

    Each workspace is capped independently, because that is how a recall sees it — a
    single global concatenation would report a number no query ever pays.
    """
    tokens = 0
    dropped = 0
    for row in _query(conn, _SELECT_CORE_WORKSPACES_SQL, [CORE_KIND]):
        built = build_core_memory(conn, row["workspace"], cap)
        tokens += built.tokens
        dropped += built.dropped
    return tokens, dropped


def _query(conn: sqlite3.Connection, sql: str, params: list[object]) -> list[sqlite3.Row]:
    """Run ``sql``, treating a database failure as "there is no core memory"."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
