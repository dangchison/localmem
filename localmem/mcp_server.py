"""Stdio MCP server exposing ``memory_recall`` and ``memory_add``.

This module is a **serialization layer**, nothing more. Retrieval lives in
:mod:`localmem.retriever`, writes and tier-1/2 dedup live in :mod:`localmem.store`; the
job here is to resolve inputs, call one of those two, and shape the answer into the JSON
that the original spec §4 froze.

Three rules govern everything below:

1. **The contract is frozen.** The key sets emitted by :func:`memory_recall` and
   :func:`memory_add` are exactly §4's, no more and no fewer. Anything the internal
   dataclasses carry beyond that — ``lexical_score``, ``core_memory_tokens``,
   ``seen_count`` on a recall hit — is deliberately not on the wire.
2. **A tool never raises.** §4 says recall on an empty database is "never an error", and
   an exception escaping into an RPC stream is worse than a degraded payload. Every tool
   body is wrapped; see :func:`memory_recall` for the authorization note.
3. **stdout is the protocol channel.** Nothing here prints. Diagnostics go to stderr.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from mcp.server import MCPServer

from localmem import __version__, config, core_memory, db, retriever, store

SERVER_NAME = "localmem"

Transport = Literal["stdio", "sse", "streamable-http"]
DEFAULT_TRANSPORT: Transport = "stdio"

# The original spec §4, verbatim. These strings are part of the frozen API: every agent session pays
# for them in context, and clients cache them. Split across lines only to fit the line
# limit — the concatenated value is byte-identical to §4.
RECALL_DESCRIPTION = (
    "Search the user's persistent cross-session memory. Call before answering "
    "questions about project history, past decisions, or user preferences."
)
ADD_DESCRIPTION = (
    "Save a durable fact, decision, or lesson to the user's persistent memory. "
    "Call when you learn something worth remembering across sessions. Always pass "
    "keywords: synonyms, Vietnamese+English terms, error codes, symptoms — search is "
    "lexical."
)

# §4's `workspace` param: this value means "every workspace" on recall. It is not a
# workspace name, which is why memory_add rejects it.
ALL_WORKSPACES = "all"

# The kinds an agent may write. `imported` belongs to the importer and `core` to the human
# (see :data:`CORE_KIND_REJECTION`); neither is on the tool surface. This is input
# validation, not a change to §4's payload shape — see docs/design_decisions.md §23.
ADD_KINDS = ("note", "trace")

# Core memory is a *push* tier: every recall loads it, in every session. An agent acting on
# injected instructions must therefore not be able to write one, and with the global tier
# (§24) a single poisoned core row would reach every workspace. The CLI still writes them.
CORE_KIND_REJECTION = (
    "kind 'core' is human-curated — core memory is loaded into every recall, so it is "
    "written by a person, not by an agent. Use `localmem add --kind core` from the CLI."
)

# Every failure payload carries this prefix so a caller can tell a localmem problem from
# a legitimately empty result.
ERROR_PREFIX = "localmem error: "

# RFC 3339 UTC, the shape §4's example uses. The database stores SQLite's
# `datetime('now')` format instead, so the two are bridged here and nowhere else.
WIRE_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Scores are fused from normalized views; four places is well past anything that can
# reorder two results, and it keeps the payload free of float noise like 0.8700000000001.
SCORE_DIGITS = 4

_LOGGER = logging.getLogger(__name__)


def build_server() -> MCPServer:
    """Return a server with the two frozen tools registered.

    Descriptions are passed explicitly rather than taken from the functions' docstrings:
    the docstrings serve maintainers, :data:`RECALL_DESCRIPTION` and
    :data:`ADD_DESCRIPTION` serve agents, and only the latter are frozen.
    """
    server = MCPServer(name=SERVER_NAME, version=__version__)
    server.add_tool(memory_recall, name="memory_recall", description=RECALL_DESCRIPTION)
    server.add_tool(memory_add, name="memory_add", description=ADD_DESCRIPTION)
    return server


def serve(transport: Transport = DEFAULT_TRANSPORT) -> None:
    """Run the MCP server until the client disconnects.

    ``transport`` stays a parameter because the original spec §1 requires streamable HTTP to be
    "a flag later, not a rewrite". v1 ships stdio only (§12), so nothing exposes any other
    value; the seam costs one argument and saves the rewrite.
    """
    _configure_logging()
    build_server().run(transport=transport)


def memory_recall(
    query: str,
    workspace: str | None = None,
    k: int = store.DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return the memories matching ``query`` as §4's recall payload.

    Args:
        query: free text; FTS5 metacharacters are neutralized downstream.
        workspace: a workspace name, ``"all"`` for every workspace, or ``None`` to detect
            one from the current directory.
        k: number of results, 1 to 20.

    Returns:
        ``{"results": [...], "core_memory": str, "message": str | None}``. On failure the
        same shape with an empty result set and ``message`` prefixed
        :data:`ERROR_PREFIX`.

    ``except Exception`` is authorized *here and at :func:`memory_add` only*, because
    these two functions are the RPC boundary: §4 promises recall is "never an error", and
    a traceback escaping into the protocol stream corrupts the session for every later
    call. ``BaseException`` still propagates, so cancellation and interrupts are
    unaffected.
    """
    try:
        return _recall(query, workspace, k)
    except Exception as exc:
        _LOGGER.exception("memory_recall failed for query %r", query)
        return {"results": [], "core_memory": "", "message": f"{ERROR_PREFIX}{exc}"}


def memory_add(
    content: str,
    workspace: str | None = None,
    kind: str = store.DEFAULT_KIND,
    source: str | None = None,
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Store ``content`` and return §4's add payload.

    Args:
        content: the text to remember; must not be blank.
        workspace: a workspace name, or ``None`` to detect one from the current
            directory. ``"all"`` is rejected — it is a recall filter, not a place.
        kind: one of :data:`ADD_KINDS`. ``"core"`` is rejected with
            :data:`CORE_KIND_REJECTION`, which names the CLI command that writes one.
        source: the calling agent's name, if it knows it.
        keywords: alternative wordings this memory should be findable by. This is the
            one part of localmem a model contributes to, and it is charged at write
            time only — roughly 20-40 output tokens, once, for a memory that is then
            recalled for free forever. Recall itself stays LLM-free.

    Returns:
        ``{"status": "added" | "duplicate_merged", "id": int, "seen_count": int}``. On
        failure ``{"status": "error", "id": 0, "seen_count": 0, "message": ...}``.

    ``session_id`` is accepted by :func:`localmem.store.add_memory` but is not part of
    §4's tool surface, so it is always ``None`` on this path.

    See :func:`memory_recall` for why ``except Exception`` is authorized here.
    """
    try:
        return _add(content, workspace, kind, source, keywords)
    except Exception as exc:
        _LOGGER.exception("memory_add failed for kind %r", kind)
        return {"status": "error", "id": 0, "seen_count": 0, "message": f"{ERROR_PREFIX}{exc}"}


def _recall(query: str, workspace: str | None, k: int) -> dict[str, Any]:
    """Run one recall; raises on bad input or a database failure."""
    limit = _validate_k(k)
    scope = _recall_scope(workspace)
    with _connection() as conn:
        outcome = retriever.retrieve(conn, query, scope, limit)
    return {
        "results": [_result_payload(memory) for memory in outcome.results],
        "core_memory": outcome.core_memory,
        "message": outcome.message,
    }


def _add(
    content: str,
    workspace: str | None,
    kind: str,
    source: str | None,
    keywords: list[str] | None,
) -> dict[str, Any]:
    """Run one write; raises on bad input or a database failure."""
    if not content.strip():
        # Checked before the workspace is resolved so an empty write never pays for the
        # `git rev-parse` that detection runs.
        raise ValueError("content is empty; pass the text you want to remember")
    if kind == core_memory.CORE_KIND:
        raise ValueError(CORE_KIND_REJECTION)
    if kind not in ADD_KINDS:
        raise ValueError(f"kind must be one of {', '.join(ADD_KINDS)}; got {kind!r}")
    target = _add_workspace(workspace)
    with _connection() as conn:
        result = store.add_memory(conn, content, target, kind, source, None, keywords)
    return {"status": result.status, "id": result.id, "seen_count": result.seen_count}


def _validate_k(k: int) -> int:
    """Return ``k`` coerced to an int within §4's range of 1 to 20.

    Raises:
        ValueError: if ``k`` is out of range or is not a whole number.
    """
    limit = int(k)
    if not store.MIN_LIMIT <= limit <= store.MAX_LIMIT:
        raise ValueError(f"k must be between {store.MIN_LIMIT} and {store.MAX_LIMIT}, got {k}")
    return limit


def _recall_scope(workspace: str | None) -> str | None:
    """Return the workspace filter for a recall; ``None`` spans every workspace.

    Detection runs per call rather than once at startup: one server process outlives any
    single directory an agent asks about.
    """
    if workspace is None:
        return config.detect_workspace()
    scope = config.validate_workspace(workspace)
    return None if scope == ALL_WORKSPACES else scope


def _add_workspace(workspace: str | None) -> str:
    """Return the workspace a new memory is stored in.

    Raises:
        ValueError: if the caller asked for ``"all"``. Storing a memory in a workspace
            literally named ``all`` would make it unreachable by every normal recall,
            since ``"all"`` is how §4 spells "search everything".
    """
    if workspace is None:
        return config.detect_workspace()
    target = config.validate_workspace(workspace)
    if target == ALL_WORKSPACES:
        raise ValueError(
            f"workspace {ALL_WORKSPACES!r} means every workspace on memory_recall and "
            "cannot be written to; name the workspace this memory belongs to"
        )
    return target


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    """Open the database for the duration of one tool call.

    One connection per call, not one per process. A ``sqlite3.Connection`` is not safe
    across interleaved transaction sequences, and a connection held open for the life of
    the server suppresses WAL auto-checkpointing, so the ``-wal`` sidecar grows without
    bound. Opening a local file costs well under a millisecond.
    """
    conn = db.open_database()
    try:
        yield conn
    finally:
        conn.close()


def _result_payload(memory: retriever.RetrievedMemory) -> dict[str, Any]:
    """Return one recall hit as §4's result object — exactly these eight keys."""
    return {
        "id": memory.id,
        "content": memory.content,
        "workspace": memory.workspace,
        "kind": memory.kind,
        "source": memory.source,
        "created_at": _to_wire_timestamp(memory.created_at),
        "score": round(memory.score, SCORE_DIGITS),
        "neighbors": [
            {"id": neighbor.id, "content": neighbor.content} for neighbor in memory.neighbors
        ],
    }


def _to_wire_timestamp(created_at: str) -> str:
    """Return a stored ``datetime('now')`` value as an RFC 3339 UTC instant.

    The conversion lives here because the wire format is this layer's concern:
    ``retriever`` and ``store`` hand back the column as SQLite wrote it.

    A value the database could not have written — hand-edited or corrupt — is passed
    through unchanged rather than failing the recall, matching how
    :func:`localmem.retriever.recency_boost` treats the same column.
    """
    try:
        parsed = datetime.strptime(created_at, retriever.SQLITE_TIMESTAMP_FORMAT)
    except ValueError:
        return created_at
    return parsed.replace(tzinfo=timezone.utc).strftime(WIRE_TIMESTAMP_FORMAT)


def _configure_logging() -> None:
    """Route this module's diagnostics to stderr and keep them off stdout.

    stdout carries JSON-RPC framing; one stray byte on it desynchronizes the client.
    Propagation is disabled because the MCP SDK calls :func:`logging.basicConfig` when a
    server is constructed, which would otherwise echo every record a second time.
    """
    if _LOGGER.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _LOGGER.addHandler(handler)
    _LOGGER.propagate = False
