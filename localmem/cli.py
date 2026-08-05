"""Command line interface: ``add``, ``search`` and ``stats``.

Every command runs headless — no prompts, no TTY requirement.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from localmem import __version__, config, db, store

_KIND_CHOICES = ("note", "trace", "core")
_SIZE_UNITS = ("B", "KB", "MB", "GB")

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
        hits = store.search_memories(conn, query, target_workspace, limit)
    if not hits:
        scope = "any workspace" if search_all else f"workspace {target_workspace!r}"
        click.echo(f"no memories matching {query!r} in {scope}")
        return
    for rank, hit in enumerate(hits, start=1):
        click.echo(
            # BM25 magnitudes are tiny on small corpora (near-zero IDF), so a general
            # format keeps ranked scores distinguishable instead of all printing 0.0000.
            f"{rank}. [score {hit.score:.3g}] "
            f"id={hit.id} workspace={hit.workspace} kind={hit.kind} "
            f"seen={hit.seen_count} created={hit.created_at}"
        )
        if hit.source:
            click.echo(f"   source: {hit.source}")
        for line in hit.content.splitlines():
            click.echo(f"   {line}")


@main.command()
def stats() -> None:
    """Show row counts per workspace and kind, plus the database path and size."""
    with _session() as (conn, path):
        summary = store.collect_stats(conn, path)
    click.echo(f"database: {summary.db_path}")
    click.echo(f"size:     {_format_size(summary.db_size_bytes)}")
    click.echo(f"memories: {summary.total}")
    _echo_counts("by workspace", summary.per_workspace)
    _echo_counts("by kind", summary.per_kind)


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
