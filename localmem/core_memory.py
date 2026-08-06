"""The always-load tier: ``kind='core'`` rows, concatenated and token-capped.

Core memory is attached to every recall, so it is the one part of the pipeline that
must never fail a query. Database errors are contained here and reported as an empty
core memory rather than propagated.

A named workspace also loads the shared :data:`GLOBAL_WORKSPACE` tier, its own rows
first; see ``docs/design_decisions.md`` §24.

A superseded core row is excluded outright — see :data:`_SELECT_CORE_SQL`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from localmem.config import FALLBACK_WORKSPACE
from localmem.tokens import estimate_tokens

CORE_KIND = "core"

#: The shared tier every *named* workspace also reads. Taken from
#: :data:`localmem.config.FALLBACK_WORKSPACE` rather than spelled again: a directory with
#: no repository and no name already lands here, so the two must be the same string.
#: Defined in this module because it is the deepest one that needs it — the retriever, the
#: audit report and the CLI all import it from here rather than keeping a copy.
GLOBAL_WORKSPACE = FALLBACK_WORKSPACE

# the original spec §5 step 7: hard cap, expressed in estimated tokens.
CORE_MEMORY_TOKEN_CAP = 400

CORE_MEMORY_JOINER = "\n"

# `superseded_by IS NULL` on both statements: core memory is the one *push* tier, loaded
# into every recall in the workspace, so a retracted convention must stop being pushed the
# moment its correction is written. Everywhere else a superseded row stays retrievable and
# is merely ranked down; here there is no ranking to rank it down in.
_SELECT_CORE_SQL = "SELECT content FROM memories WHERE kind = ? AND superseded_by IS NULL"
_CORE_ORDER_BY = " ORDER BY created_at ASC, id ASC"
_SELECT_CORE_WORKSPACES_SQL = (
    "SELECT DISTINCT workspace FROM memories WHERE kind = ? AND superseded_by IS NULL"
)


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

    A *named* workspace is followed by the shared :data:`GLOBAL_WORKSPACE` tier: the
    workspace's own rows are fitted first and the shared ones fill whatever room is left,
    so a repo can never lose a core row to one it does not own. ``None`` and
    :data:`GLOBAL_WORKSPACE` itself behave exactly as they always did.

    Never raises: an unreadable database yields an empty core memory.
    """
    own = _core_contents(conn, workspace)
    dropped = _fit([], own, cap)
    if workspace is None or workspace == GLOBAL_WORKSPACE:
        return _assemble(own, dropped)

    shared = _core_contents(conn, GLOBAL_WORKSPACE)
    dropped += _fit(own, shared, cap)
    return _assemble(own + shared, dropped)


def core_memory_totals(
    conn: sqlite3.Connection, cap: int = CORE_MEMORY_TOKEN_CAP
) -> tuple[int, int]:
    """Return ``(estimated tokens, dropped rows)`` summed over every workspace.

    Each workspace is capped independently, because that is how a recall sees it — a
    single global concatenation would report a number no query ever pays.
    """
    tokens = 0
    dropped = 0
    for workspace in core_workspaces(conn):
        built = build_core_memory(conn, workspace, cap)
        tokens += built.tokens
        dropped += built.dropped
    return tokens, dropped


def core_workspaces(conn: sqlite3.Connection) -> list[str]:
    """Return every workspace holding at least one core row, sorted by name.

    Never raises, for the same reason :func:`build_core_memory` does not.
    """
    return sorted(
        row["workspace"] for row in _query(conn, _SELECT_CORE_WORKSPACES_SQL, [CORE_KIND])
    )


def _core_contents(conn: sqlite3.Connection, workspace: str | None) -> list[str]:
    """Return one workspace's core rows, oldest first (``None`` spans every workspace)."""
    sql = _SELECT_CORE_SQL
    params: list[object] = [CORE_KIND]
    if workspace is not None:
        sql += " AND workspace = ?"
        params.append(workspace)
    return [row["content"] for row in _query(conn, sql + _CORE_ORDER_BY, params)]


def _fit(kept: list[str], contents: list[str], cap: int) -> int:
    """Drop whole rows from the front of ``contents`` until ``kept + contents`` fits.

    ``kept`` is charged against the cap but never dropped, which is what makes the shared
    tier yield to the workspace's own rows rather than the other way round.

    Returns:
        How many rows were dropped. ``contents`` is mutated in place.
    """
    dropped = 0
    while contents and estimate_tokens(CORE_MEMORY_JOINER.join(kept + contents)) > cap:
        contents.pop(0)
        dropped += 1
    return dropped


def _assemble(contents: list[str], dropped: int) -> CoreMemory:
    text = CORE_MEMORY_JOINER.join(contents)
    return CoreMemory(text=text, tokens=estimate_tokens(text), dropped=dropped)


def _query(conn: sqlite3.Connection, sql: str, params: list[object]) -> list[sqlite3.Row]:
    """Run ``sql``, treating a database failure as "there is no core memory"."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
