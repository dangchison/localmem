"""Commands: ``add``, ``search``, ``stats``, ``backfill``, ``dedupe``, ``gc``, ``serve``.

Every command runs headless — no prompts, no TTY requirement. ``dedupe`` prompts only
when stdin is a terminal; with no terminal and no flags it prints the pending queue and
exits cleanly instead of waiting for input that will never arrive.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from localmem import __version__, config, core_memory, db, dedup, indexer, retriever, store

_KIND_CHOICES = ("note", "trace", "core")
_SIZE_UNITS = ("B", "KB", "MB", "GB")
_NEIGHBOR_PREVIEW_CHARS = 100

# Memory text and search queries routinely start with a markdown bullet ("- use pnpm"),
# which click would otherwise reject as an unknown option.
_TEXT_ARGUMENT_SETTINGS = {"ignore_unknown_options": True}


@click.group()
@click.version_option(__version__, prog_name="localmem")
def main() -> None:
    """Local-first, zero-token memory for AI coding agents."""


@main.command(context_settings=_TEXT_ARGUMENT_SETTINGS)
@click.argument("content")
@click.option("-w", "--workspace", default=None, help="Workspace name (default: auto-detected).")
@click.option(
    "--kind",
    type=click.Choice(_KIND_CHOICES),
    default="note",
    show_default=True,
    help="Memory kind.",
)
@click.option("--source", default=None, help="Origin of the memory, e.g. the calling agent.")
@click.option("--session-id", default=None, help="Session identifier for provenance.")
def add(
    content: str,
    workspace: str | None,
    kind: str,
    source: str | None,
    session_id: str | None,
) -> None:
    """Store CONTENT, merging it into an existing row if identical."""
    with _session() as (conn, _path):
        target_workspace = _resolve_workspace(workspace)
        result = store.add_memory(conn, content, target_workspace, kind, source, session_id)
    click.echo(
        json.dumps(
            {"status": result.status, "id": result.id, "seen_count": result.seen_count},
            ensure_ascii=False,
        )
    )


@main.command(context_settings=_TEXT_ARGUMENT_SETTINGS)
@click.argument("query")
@click.option("-w", "--workspace", default=None, help="Workspace name (default: auto-detected).")
@click.option(
    "-k",
    "limit",
    type=click.IntRange(store.MIN_LIMIT, store.MAX_LIMIT),
    default=store.DEFAULT_LIMIT,
    show_default=True,
    help="Number of results.",
)
@click.option("--all", "search_all", is_flag=True, help="Search every workspace.")
def search(query: str, workspace: str | None, limit: int, search_all: bool) -> None:
    """Recall memories matching QUERY, best match first."""
    with _session() as (conn, _path):
        target_workspace = None if search_all else _resolve_workspace(workspace)
        outcome = retriever.retrieve(conn, query, target_workspace, limit)
    _echo_core_memory(outcome)
    if not outcome.results:
        scope = "any workspace" if search_all else f"workspace {target_workspace!r}"
        click.echo(f"no memories matching {query!r} in {scope}")
        return
    for rank, hit in enumerate(outcome.results, start=1):
        click.echo(
            # Fused scores are small by construction (weights sum to 1 before the
            # boosts), so a general format keeps neighbouring ranks distinguishable.
            f"{rank}. [score {hit.score:.3g}] "
            f"id={hit.id} workspace={hit.workspace} kind={hit.kind} "
            f"seen={hit.seen_count} created={hit.created_at}"
        )
        if hit.source:
            click.echo(f"   source: {hit.source}")
        for line in hit.content.splitlines():
            click.echo(f"   {line}")
        for neighbor in hit.neighbors:
            click.echo(f"   related id={neighbor.id}: {_preview(neighbor.content)}")


@main.command()
def stats() -> None:
    """Show row counts per workspace and kind, plus the database path and size."""
    with _session() as (conn, path):
        summary = store.collect_stats(conn, path)
    click.echo(f"database: {summary.db_path}")
    click.echo(f"size:     {_format_size(summary.db_size_bytes)}")
    click.echo(f"memories: {summary.total}")
    click.echo(f"entities: {summary.total_entities}")
    click.echo(f"entity links: {summary.total_entity_links}")
    click.echo(f"queue depth: {summary.queue_depth} pending near-duplicate pairs")
    click.echo(f"core memory: ~{summary.core_memory_tokens} estimated tokens")
    if summary.core_memory_dropped:
        click.echo(
            f"  warning: {summary.core_memory_dropped} core rows are hidden by the "
            f"{core_memory.CORE_MEMORY_TOKEN_CAP}-token cap"
        )
    _echo_counts("by workspace", summary.per_workspace)
    _echo_counts("by kind", summary.per_kind)


@main.command()
@click.option(
    "-w",
    "--workspace",
    default=None,
    help="Restrict to one workspace (default: every workspace).",
)
def backfill(workspace: str | None) -> None:
    """Extract entities for memories stored before they were indexed.

    Safe to re-run: memories that already have entity links are skipped.
    """
    with _session() as (conn, _path):
        processed, links_created = indexer.backfill_all(conn, workspace)
    click.echo(f"processed {processed} memories, created {links_created} links")


@main.command()
@click.option("--review", is_flag=True, help="Review pending pairs (prompts only on a TTY).")
@click.option(
    "-w",
    "--workspace",
    default=None,
    help="Restrict to one workspace (default: every workspace).",
)
@click.option("--list", "list_only", is_flag=True, help="Print the pending pairs and exit.")
@click.option(
    "--merge",
    "merge_id",
    type=int,
    default=None,
    metavar="ID",
    help="Resolve pair ID by keeping the newer memory and folding seen_count into it.",
)
@click.option(
    "--keep-both",
    "keep_both_id",
    type=int,
    default=None,
    metavar="ID",
    help="Resolve pair ID by keeping both memories unchanged.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def dedupe(
    review: bool,
    workspace: str | None,
    list_only: bool,
    merge_id: int | None,
    keep_both_id: int | None,
    as_json: bool,
) -> None:
    """Review the tier-2 near-duplicate queue.

    Near-duplicates are never merged automatically: a pair leaves the queue only when
    you resolve it here.
    """
    scope = None if workspace is None else config.validate_workspace(workspace)
    with _session() as (conn, _path):
        if merge_id is not None:
            _echo_resolution(dedup.resolve_merge(conn, merge_id), as_json)
            return
        if keep_both_id is not None:
            _echo_resolution(dedup.resolve_keep_both(conn, keep_both_id), as_json)
            return
        pairs = dedup.pending_pairs(conn, scope)
        if review and not list_only and _is_interactive():
            _review_pairs(conn, pairs)
            return
        _echo_pairs(pairs, as_json)


@main.command()
@click.option("--dry-run", is_flag=True, help="Report what would be pruned and write nothing.")
@click.option(
    "--days",
    type=click.IntRange(min=0),
    default=dedup.GC_DEFAULT_DAYS,
    show_default=True,
    help="Prune resolved queue rows older than this many days.",
)
def gc(dry_run: bool, days: int) -> None:
    """Prune resolved queue rows, reclaim disk space and print the result."""
    with _session() as (conn, path):
        prunable = dedup.count_prunable(conn, days)
        size_before = store.database_size_bytes(path)
        if dry_run:
            click.echo(f"would prune {prunable} resolved queue rows older than {days} days")
            click.echo(f"size:    {_format_size(size_before)} (unchanged, nothing written)")
            return
        pruned = dedup.prune_resolved(conn, days)
        # VACUUM cannot run inside a transaction, so it happens after prune_resolved
        # has committed rather than within it.
        dedup.vacuum(conn)
        # VACUUM rewrites the whole database through the WAL, and database_size_bytes
        # counts the -wal sidecar. Without this checkpoint gc reports that the file grew.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size_after = store.database_size_bytes(path)
        remaining = dedup.pending_pairs(conn)
    click.echo(f"pruned {pruned} resolved queue rows older than {days} days")
    click.echo(f"size:    {_format_size(size_before)} -> {_format_size(size_after)}")
    click.echo(f"queue depth: {len(remaining)} pending near-duplicate pairs")


@main.command()
def serve() -> None:
    """Run the MCP server on stdio; this is what agent configs invoke.

    Emits nothing on stdout — that channel carries JSON-RPC framing.
    """
    # Imported here, not at module scope: the MCP SDK pulls in starlette, uvicorn and
    # httpx, and no other command needs any of them.
    from localmem import mcp_server

    mcp_server.serve()


@contextmanager
def _session() -> Iterator[tuple[sqlite3.Connection, Path]]:
    """Open the database and translate failures into clean CLI errors."""
    try:
        path = config.resolve_db_path()
        conn = db.open_database(path)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        raise click.ClickException(f"cannot open the localmem database: {exc}") from exc
    try:
        yield conn, path
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    except (sqlite3.Error, RuntimeError) as exc:
        raise click.ClickException(f"database error: {exc}") from exc
    finally:
        conn.close()


def _resolve_workspace(explicit: str | None) -> str:
    if explicit is None:
        return config.detect_workspace()
    return config.validate_workspace(explicit)


def _is_interactive() -> bool:
    """Return whether stdin is a terminal, i.e. whether prompting can succeed."""
    return sys.stdin.isatty()


def _preview(text: str) -> str:
    """Collapse ``text`` onto one line, shortened for a nested listing."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _NEIGHBOR_PREVIEW_CHARS:
        return collapsed
    return collapsed[: _NEIGHBOR_PREVIEW_CHARS - 1] + "…"


def _echo_core_memory(outcome: retriever.RetrievalResult) -> None:
    if not outcome.core_memory:
        return
    click.echo(f"core memory (~{outcome.core_memory_tokens} estimated tokens):")
    for line in outcome.core_memory.splitlines():
        click.echo(f"   {line}")
    if outcome.core_memory_dropped:
        click.echo(f"   ({outcome.core_memory_dropped} older core rows dropped to fit the cap)")
    click.echo("")


def _pair_payload(pair: dedup.DuplicatePair) -> dict[str, object]:
    return {
        "queue_id": pair.queue_id,
        "score": pair.score,
        "workspace": pair.workspace,
        "queued_at": pair.queued_at,
        "newer": {"id": pair.newer.id, "content": pair.newer.content},
        "older": {"id": pair.older.id, "content": pair.older.content},
    }


def _echo_pairs(pairs: list[dedup.DuplicatePair], as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps([_pair_payload(pair) for pair in pairs], ensure_ascii=False))
        return
    if not pairs:
        click.echo("no pending near-duplicate pairs")
        return
    for pair in pairs:
        _echo_pair(pair)


def _echo_pair(pair: dedup.DuplicatePair) -> None:
    click.echo(
        f"pair {pair.queue_id} [jaccard {pair.score:.3g}] "
        f"workspace={pair.workspace} queued={pair.queued_at}"
    )
    click.echo(
        f"   newer id={pair.newer.id} seen={pair.newer.seen_count}: {_preview(pair.newer.content)}"
    )
    click.echo(
        f"   older id={pair.older.id} seen={pair.older.seen_count}: {_preview(pair.older.content)}"
    )


def _echo_resolution(resolution: dedup.Resolution, as_json: bool) -> None:
    if as_json:
        click.echo(
            json.dumps(
                {
                    "queue_id": resolution.queue_id,
                    "status": resolution.status,
                    "kept_id": resolution.kept_id,
                    "removed_id": resolution.removed_id,
                    "seen_count": resolution.seen_count,
                },
                ensure_ascii=False,
            )
        )
        return
    removed = (
        "nothing removed" if resolution.removed_id is None else f"removed {resolution.removed_id}"
    )
    click.echo(
        f"pair {resolution.queue_id}: {resolution.status} — "
        f"kept {resolution.kept_id} (seen={resolution.seen_count}), {removed}"
    )


def _review_pairs(conn: sqlite3.Connection, pairs: list[dedup.DuplicatePair]) -> None:
    """Walk the pending pairs on a terminal, one prompt each.

    The queue is re-read before every prompt: merging a pair removes a memory, and that
    takes any other pair built on the same memory with it.
    """
    if not pairs:
        click.echo("no pending near-duplicate pairs")
        return
    for queue_id in [pair.queue_id for pair in pairs]:
        still_open = {open_pair.queue_id: open_pair for open_pair in dedup.pending_pairs(conn)}
        pair = still_open.get(queue_id)
        if pair is None:
            click.echo(f"pair {queue_id}: resolved by an earlier merge, skipping")
            continue
        _echo_pair(pair)
        choice = click.prompt("[m]erge / [k]eep both / [s]kip", default="s", show_default=True)
        answer = choice.strip().lower()[:1]
        if answer == "m":
            _echo_resolution(dedup.resolve_merge(conn, pair.queue_id), False)
        elif answer == "k":
            _echo_resolution(dedup.resolve_keep_both(conn, pair.queue_id), False)
        else:
            click.echo(f"pair {pair.queue_id}: skipped, still pending")


def _echo_counts(title: str, counts: tuple[tuple[str, int], ...]) -> None:
    click.echo(f"\n{title}:")
    if not counts:
        click.echo("  (none)")
        return
    width = max(len(name) for name, _ in counts)
    for name, count in counts:
        click.echo(f"  {name.ljust(width)}  {count}")


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in _SIZE_UNITS[:-1]:
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_SIZE_UNITS[-1]}"


if __name__ == "__main__":  # pragma: no cover
    main()
