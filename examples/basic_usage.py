"""Drive localmem's Python API end to end against a throwaway database.

Run it from a checkout::

    python examples/basic_usage.py

Everything happens inside a :class:`tempfile.TemporaryDirectory`. ``$LOCALMEM_DB`` is
pointed at a file in there before any localmem module opens anything, so the real
``~/.localmem/memory.db`` is never created, read or written — the last step checks that.

These are the same calls ``localmem serve`` makes for ``memory_add`` and ``memory_recall``;
the MCP SDK is deliberately not imported, so this script needs nothing beyond the package
itself.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from localmem import config, core_memory, db, dedup, retriever, store

WORKSPACE = "demo-project"
OTHER_WORKSPACE = "other-project"


def add(conn: sqlite3.Connection, content: str, kind: str = "note") -> None:
    """Store ``content`` in :data:`WORKSPACE` and report what the write did.

    Args:
        conn: an open localmem database connection.
        content: the text to remember.
        kind: ``note``, ``trace``, ``imported`` or ``core``.
    """
    result = store.add_memory(conn, content, WORKSPACE, kind)
    print(f"  {result.status:16} id={result.id} seen_count={result.seen_count}  {content!r}")


def show(outcome: retriever.RetrievalResult) -> None:
    """Print a retrieval result the way ``localmem search`` does.

    Args:
        outcome: what :func:`localmem.retriever.retrieve` returned.
    """
    if outcome.core_memory:
        print(f"  core memory (~{outcome.core_memory_tokens} estimated tokens):")
        for line in outcome.core_memory.splitlines():
            print(f"    {line}")
    if not outcome.results:
        print(f"  (no results — {outcome.message})")
        return
    for rank, hit in enumerate(outcome.results, start=1):
        print(f"  {rank}. [score {hit.score:.3g}] id={hit.id} kind={hit.kind}  {hit.content}")
        for neighbor in hit.neighbors:
            print(f"       related id={neighbor.id}: {neighbor.content}")


def step_write(conn: sqlite3.Connection) -> None:
    """Show tier-1 deduplication: the same fact twice is one row, seen twice.

    Args:
        conn: an open localmem database connection.
    """
    print("\n1. Writing memories")
    add(conn, "use pnpm, not npm")
    add(conn, "the staging deploy needs a manual approval step")
    add(conn, "ConfigLoader reads settings.toml at import time, not on first use")
    # Normalization folds case, whitespace runs and markdown bullet prefixes before
    # hashing, so this is the *same* memory as the first one.
    add(conn, "- Use   PNPM, not npm")


def step_core(conn: sqlite3.Connection) -> None:
    """Add a ``core`` memory — the tier attached to every recall in this workspace.

    Args:
        conn: an open localmem database connection.
    """
    print("\n2. Core memory (always loaded, capped at ~400 estimated tokens)")
    add(conn, "never force-push to main", kind="core")
    built = core_memory.build_core_memory(conn, WORKSPACE)
    print(f"  {built.tokens} estimated tokens, {built.dropped} rows dropped to fit the cap")


def step_recall(conn: sqlite3.Connection) -> None:
    """Run the full dual-view retrieval pipeline on a few queries.

    Args:
        conn: an open localmem database connection.
    """
    print("\n3. Recall — lexical view + entity view, fused")
    for query in ("pnpm", "ConfigLoader", "settings.toml"):
        print(f"\n  query {query!r}:")
        show(retriever.retrieve(conn, query, WORKSPACE, k=3))

    # Retrieval is lexical plus an entity graph, never semantic: a query sharing no words
    # and no entities with any memory finds nothing. This is limitation 1 in the README.
    print("\n  query 'how do we install dependencies?' (no shared words or entities):")
    show(retriever.retrieve(conn, "how do we install dependencies?", WORKSPACE, k=3))


def step_workspaces(conn: sqlite3.Connection) -> None:
    """Show that workspaces are the isolation boundary for reads and for dedup.

    Args:
        conn: an open localmem database connection.
    """
    print("\n4. Workspace isolation")
    other = store.add_memory(conn, "use pnpm, not npm", OTHER_WORKSPACE)
    print(f"  same text in {OTHER_WORKSPACE!r}: {other.status} id={other.id} (a separate row)")

    scoped = retriever.retrieve(conn, "pnpm", WORKSPACE, k=5)
    everywhere = retriever.retrieve(conn, "pnpm", None, k=5)
    print(f"  hits in {WORKSPACE!r}: {len(scoped.results)}")
    print(f"  hits across every workspace: {len(everywhere.results)}")


def step_dedupe(conn: sqlite3.Connection) -> None:
    """Show tier-2: a near-duplicate is queued for review and never merged for you.

    Args:
        conn: an open localmem database connection.
    """
    print("\n5. Near-duplicate review queue (Jaccard >= 0.7, nothing auto-merged)")
    # Punctuation is meaningful text, so this hashes differently from "use pnpm, not npm"
    # and tier 1 lets it through. Tier 2 catches it and queues the pair.
    add(conn, "use pnpm not npm!")
    pairs = dedup.pending_pairs(conn, WORKSPACE)
    if not pairs:
        print("  no pending pairs")
        return
    for pair in pairs:
        print(f"  pair {pair.queue_id} [jaccard {pair.score:.3g}]")
        print(f"    newer id={pair.newer.id}: {pair.newer.content!r}")
        print(f"    older id={pair.older.id}: {pair.older.content!r}")
    print("  resolve with: localmem dedupe --merge ID   (or --keep-both ID)")


def step_stats(conn: sqlite3.Connection, path: Path) -> None:
    """Print the same aggregate counts ``localmem stats`` reports.

    Args:
        conn: an open localmem database connection.
        path: the database file, used for the on-disk size.
    """
    print("\n6. Stats")
    summary = store.collect_stats(conn, path)
    print(f"  memories: {summary.total}")
    print(f"  entities: {summary.total_entities} ({summary.total_entity_links} links)")
    print(f"  queue depth: {summary.queue_depth}")
    print(f"  core memory: ~{summary.core_memory_tokens} estimated tokens")
    for workspace, count in summary.per_workspace:
        print(f"    {workspace}: {count}")


def main() -> None:
    """Run every step against a temporary database and prove the real one is untouched.

    Raises:
        RuntimeError: if localmem resolved a database path outside the temporary
            directory, or if ``~/.localmem/`` appeared during the run.
    """
    real_db_dir = Path.home() / ".localmem"
    real_existed = real_db_dir.exists()

    with tempfile.TemporaryDirectory(prefix="localmem-example-") as workdir:
        path = Path(workdir) / "memory.db"
        os.environ[config.DB_PATH_ENV_VAR] = str(path)

        resolved = config.resolve_db_path()
        if resolved != path:
            raise RuntimeError(f"expected localmem to use {path}, it resolved {resolved}")
        print(f"database: {resolved}  (temporary — the real ~/.localmem/ is not touched)")

        conn = db.open_database(resolved)
        try:
            step_write(conn)
            step_core(conn)
            step_recall(conn)
            step_workspaces(conn)
            step_dedupe(conn)
            step_stats(conn, resolved)
        finally:
            conn.close()

    if real_db_dir.exists() and not real_existed:
        raise RuntimeError(f"{real_db_dir} was created by this example; that is a bug")
    state = "exists as before" if real_existed else "does not exist"
    print(f"\ndone — {real_db_dir} still {state}")


if __name__ == "__main__":
    main()
