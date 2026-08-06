"""The ``localmem`` command line.

Commands: ``init``, ``add``, ``promote``, ``forget``, ``search``, ``import``, ``agents``,
``benchmark``, ``eval``, ``stats``, ``audit``, ``backfill``, ``dedupe``, ``gc``,
``export``, ``restore``, ``serve``.

Every command runs headless — no prompts, no TTY requirement, with one deliberate
exception. ``dedupe`` and ``init`` prompt only when stdin is a terminal; with no terminal
and no flags ``init`` prints what it *would* do and writes no agent config, and
``import --select`` fails with a clear message rather than hanging or silently importing
everything. ``forget`` is the exception: it is the only command that destroys a memory,
so with no terminal and no ``--yes`` it **fails** rather than falling back to deleting.

This module is the only place that resolves the user's home directory. ``Path.home()``
is read here and passed down as a parameter, so nothing under :mod:`localmem.agents` can
reach a real config file on its own.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import click

from localmem import (
    __version__,
    agents,
    audit,
    benchmark,
    config,
    core_memory,
    db,
    dedup,
    evaluate,
    importer,
    indexer,
    retriever,
    store,
    transfer,
)

#: Every kind a person may write from the command line. ``imported`` is missing on
#: purpose — it is the importer's stamp and nothing else may claim it. ``core`` is here
#: and deliberately absent from :data:`localmem.mcp_server.ADD_KINDS`: the CLI is the
#: human channel. Shared by ``add`` and ``promote`` so the two can never drift. The two
#: lists are not derived from one another on purpose — that one is a security boundary,
#: this one is a UI — so a new kind has to be added here *and* there, deliberately.
_KIND_CHOICES = ("note", "trace", "core", "lesson")

#: What ``promote`` defaults to. The command exists because lessons are the kind worth
#: reclassifying after the fact — you rarely know a note was a lesson while writing it.
_PROMOTE_DEFAULT_KIND = "lesson"
_SIZE_UNITS = ("B", "KB", "MB", "GB")
_NEIGHBOR_PREVIEW_CHARS = 100

#: ``search --context`` truncates each memory here. A hook pays this per prompt, so a
#: whole-file skill or checklist must not inject itself in full every time; the agent is
#: told the id and can recall the row properly when it actually needs the rest.
CONTEXT_SNIPPET_CHARS = 400
_CONTEXT_HEADER = "Relevant memories (localmem):"

#: Width of the longest bar in `audit`'s trace-similarity histogram. The shape is what
#: carries the message, so the bars are scaled to the fullest bucket rather than to an
#: absolute count.
_HISTOGRAM_BAR_CHARS = 40

#: Appended to a result the disjunctive fallback found. Nothing else matched the query,
#: so the row shares *some* word with it rather than all of them — worth showing, worth
#: labelling. ``--context`` drops these entirely; see :func:`_echo_context`.
_FALLBACK_MARKER = " [weak: no exact match, any-word fallback]"

# The query `init` step 5 runs to prove recall works end to end.
_SELF_CHECK_QUERY = "localmem"

_NEXT_STEPS = ('localmem search "your question"', "localmem stats", "localmem benchmark")

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
@click.option(
    "-K",
    "--keyword",
    "keywords",
    multiple=True,
    help=(
        "Another word this memory should be findable by — a synonym, the other "
        "language's term, an error code. Repeatable. Merging an identical memory "
        "unions its keywords into the stored row."
    ),
)
@click.option(
    "--supersedes",
    "supersedes",
    multiple=True,
    type=int,
    metavar="ID",
    help=(
        "The id of a memory this one corrects. Repeatable. The old memory is kept and "
        "stays searchable — it is ranked down, and a recall that returns it attaches "
        "this one as its first neighbour. An unknown id is an error and stores nothing."
    ),
)
@click.option(
    "--if-novel",
    "if_novel",
    is_flag=True,
    help=(
        "Store only if no existing memory in this workspace already says the same thing. "
        "Reports `skipped_redundant` and writes nothing when one does. Built for the "
        "auto-capture hook; nothing is ever deleted or edited by this flag."
    ),
)
def add(
    content: str,
    workspace: str | None,
    kind: str,
    source: str | None,
    session_id: str | None,
    keywords: tuple[str, ...],
    supersedes: tuple[int, ...],
    if_novel: bool,
) -> None:
    """Store CONTENT, merging it into an existing row if identical."""
    if if_novel and supersedes:
        # Refused rather than resolved either way round. Applying the correction after
        # declining the write would retract a memory in favour of one that was never
        # stored; skipping it silently would drop a retraction the caller believes it
        # made. Both are worse than saying so.
        raise click.UsageError(
            "--if-novel cannot be combined with --supersedes: a skipped write has nothing "
            "to supersede with, and dropping the correction silently would be worse. "
            "Store the correction unconditionally, or drop --supersedes."
        )
    with _session() as (conn, _path):
        target_workspace = _resolve_workspace(workspace)
        result = store.add_memory(
            conn,
            content,
            target_workspace,
            kind,
            source,
            session_id,
            keywords,
            supersedes,
            if_novel,
        )
    click.echo(
        json.dumps(
            {"status": result.status, "id": result.id, "seen_count": result.seen_count},
            ensure_ascii=False,
        )
    )


@main.command()
@click.argument("memory_id", metavar="ID", type=int)
@click.option(
    "--kind",
    type=click.Choice(_KIND_CHOICES),
    default=_PROMOTE_DEFAULT_KIND,
    show_default=True,
    help="The kind to give the memory.",
)
def promote(memory_id: int, kind: str) -> None:
    """Reclassify the memory ID — by default into a lesson.

    Addressed by id, because re-adding the same text with a different `--kind` does not
    reclassify it: `add` merges on the content hash and keeps the stored kind.
    Run `localmem search` first; every hit prints its id.

    Nothing but the kind changes — content, keywords, seen_count and the entity links
    all stay as they are. Safe to run twice: a memory that already has that kind is
    reported as `unchanged` and nothing is written.
    """
    with _session() as (conn, _path):
        result = store.promote_memory(conn, memory_id, kind)
        warning = _core_cap_warning(conn, result)
    click.echo(
        json.dumps(
            {
                "status": result.status,
                "id": result.id,
                "workspace": result.workspace,
                "kind": result.kind,
                "previous_kind": result.previous_kind,
            },
            ensure_ascii=False,
        )
    )
    if warning is not None:
        click.echo(warning, err=True)


@main.command()
@click.argument("memory_id", metavar="ID", type=int)
@click.option("--dry-run", is_flag=True, help="Show what would be deleted and write nothing.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Also delete a memory that other memories name as their replacement, clearing "
        "superseded_by on them — which restores those retracted memories to full rank."
    ),
)
def forget(memory_id: int, dry_run: bool, yes: bool, force: bool) -> None:
    """Delete the memory ID permanently. There is no undo.

    Addressed by id, one at a time, exactly as `promote` is — run `localmem search`
    first, every hit prints its id. There is no `--workspace` or `--kind` bulk form: a
    mistyped filter would be unrecoverable, and this is the one command that loses data.

    The row is printed in full before anything happens, and deleting it needs either a
    yes at the prompt or `--yes`. With no terminal to ask on and no `--yes` the command
    fails and writes nothing; it never falls back to deleting. `--dry-run` shows the same
    report and always writes nothing.

    What goes with it: the full-text index entry, the entity links, any pending
    near-duplicate pair naming it, and every extracted entity that no other memory still
    uses — an entity pulled out of a memory holding a secret is that secret, so it does
    not stay behind.

    Deliberately **not** available over MCP. Recalled memory text is documented as data
    and not instructions, and a stored string saying "always delete memory id=1" replayed
    into an agent that can delete is a hole that a read/write split does not cover.
    """
    with _session() as (conn, _path):
        target = store.describe_forget(conn, memory_id)
        _echo_forget_target(target, force=force)
        if dry_run:
            click.echo("nothing was written")
            return
        if not yes and not _confirm_forget(target):
            click.echo(f"memory {target.id} was kept; nothing was written")
            return
        result = store.forget_memory(conn, memory_id, force=force)
    _echo_forget_result(result)


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
@click.option(
    "--context",
    "as_context",
    is_flag=True,
    help="Compact output for a prompt hook; prints nothing when nothing matches.",
)
@click.option(
    "--context-fallback",
    "context_fallback",
    is_flag=True,
    help="Include weak OR-fallback hits in --context output (implies --context).",
)
def search(
    query: str,
    workspace: str | None,
    limit: int,
    search_all: bool,
    as_context: bool,
    context_fallback: bool,
) -> None:
    """Recall memories matching QUERY, best match first."""
    with _session() as (conn, _path):
        target_workspace = None if search_all else _resolve_workspace(workspace)
        outcome = retriever.retrieve(conn, query, target_workspace, limit)
    if as_context or context_fallback:
        _echo_context(outcome, context_fallback)
        return
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
            # Only ever appended, and only on the weak pass, so the ordinary line is
            # byte-for-byte what v0.2.2 printed.
            f"{_FALLBACK_MARKER if hit.from_fallback else ''}"
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
    click.echo(f"recalls: {summary.total_recalled} recorded across all memories")
    click.echo(f"queue depth: {summary.queue_depth} pending near-duplicate pairs")
    click.echo(f"core memory: ~{summary.core_memory_tokens} estimated tokens")
    if summary.core_memory_dropped:
        click.echo(
            f"  warning: {summary.core_memory_dropped} core rows are hidden by the "
            f"{core_memory.CORE_MEMORY_TOKEN_CAP}-token cap"
        )
    _echo_counts("by workspace", summary.per_workspace)
    _echo_counts("by kind", summary.per_kind)


@main.command("audit")
@click.option(
    "-w",
    "--workspace",
    default=None,
    help="Restrict to one workspace (default: every workspace).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def audit_command(workspace: str | None, as_json: bool) -> None:
    """Report memory hygiene: review queue, promotion candidates, distribution, dead rows.

    Read-only and deterministic — no model, no network, and not one write. It is a
    report, not a gate: it always exits 0 and never repairs what it finds.
    """
    with _session() as (conn, path):
        report = audit.run(conn, path, workspace)
    if as_json:
        click.echo(json.dumps(_audit_payload(report), ensure_ascii=False))
        return
    _echo_audit(report)


@main.command("export")
@click.option(
    "-w",
    "--workspace",
    default=None,
    help="Restrict to one workspace (default: every workspace).",
)
@click.option(
    "-o",
    "--output",
    default=None,
    metavar="FILE",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write to FILE instead of stdout.",
)
def export_command(workspace: str | None, output: Path | None) -> None:
    """Write the raw memory rows as JSON, for backup or another machine.

    Only the ``memories`` table travels: the entity graph is rebuilt by ``restore`` and
    the near-duplicate queue is local, transient state.
    """
    with _session() as (conn, _path):
        document = transfer.export_document(conn, workspace)
    text = transfer.render(document)
    if output is None:
        click.echo(text)
        return
    try:
        output.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"cannot write {output}: {exc}") from exc
    click.echo(f"exported {len(document[transfer.MEMORIES_KEY])} memories to {output}")


@main.command("restore")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def restore_command(path: Path) -> None:
    """Merge the export document at PATH back into the database.

    Safe to run twice and safe to run into a database that already has data: a row that
    is already there keeps its own ``created_at``, ``kind`` and ``source``, and only its
    ``seen_count`` rises to the larger of the two.
    """
    try:
        records = transfer.read_document(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with _session() as (conn, _path):
        outcome = transfer.restore(conn, records)
    click.echo(
        f"restored {outcome.total} memories — {outcome.added} new, "
        f"{outcome.merged} merged into existing"
    )
    click.echo(f"entity backfill created {outcome.links_created} links")


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
@click.option(
    "--prune-traces",
    "prune_traces_days",
    type=click.IntRange(min=0),
    default=None,
    metavar="N",
    help=(
        "ALSO delete auto-captured traces older than N days that have never been "
        "recalled. Off unless given: plain `gc` deletes no memory. Preview with "
        "--dry-run first."
    ),
)
def gc(dry_run: bool, days: int, prune_traces_days: int | None) -> None:
    """Prune resolved queue rows, reclaim disk space and print the result.

    Deletes **no memory** unless you pass `--prune-traces N`. Without it this command
    only ever touches the near-duplicate queue, exactly as it always has.
    """
    with _session() as (conn, path):
        prunable = dedup.count_prunable(conn, days)
        traces = (
            None
            if prune_traces_days is None
            else store.count_prunable_traces(conn, prune_traces_days)
        )
        size_before = store.database_size_bytes(path)
        if dry_run:
            click.echo(f"would prune {prunable} resolved queue rows older than {days} days")
            if traces is not None:
                _echo_trace_prune_preview(traces)
            click.echo(f"size:    {_format_size(size_before)} (unchanged, nothing written)")
            return
        trace_prune = (
            store.PruneOutcome(0, 0)
            if prune_traces_days is None
            else store.prune_traces(conn, prune_traces_days)
        )
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
    if prune_traces_days is not None:
        click.echo(
            f"pruned {trace_prune.traces} never-recalled traces older than {prune_traces_days} days"
        )
        click.echo(f"removed {trace_prune.entities} entities no other memory still uses")
        if traces is not None and traces.protected:
            click.echo(
                f"kept {traces.protected} otherwise-prunable traces that another memory "
                "names as its replacement"
            )
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


@main.command()
@click.option(
    "--yes",
    is_flag=True,
    help="Answer yes to every agent registration question (step 2 only).",
)
@click.option(
    "--import-all",
    "import_all",
    is_flag=True,
    help="Import every instruction file found. Asked separately from --yes, never bundled.",
)
@click.option(
    "-w",
    "--workspace",
    default=None,
    help="Workspace for imported records (default: auto-detected).",
)
def init(yes: bool, import_all: bool, workspace: str | None) -> None:
    """Set up the database, agent configs, and optionally import instruction files.

    Safe to re-run: every step is idempotent, and a step that fails leaves the steps
    before it intact. Nothing outside the database is written without a yes — with no
    terminal and no flags this prints what it would do and writes no agent config.
    """
    home = Path.home()
    cwd = Path.cwd()
    interactive = _is_interactive()
    with _session() as (conn, path):
        _init_database(path)
        _init_agents(home, cwd, yes=yes, interactive=interactive)
        target_workspace = _resolve_workspace(workspace)
        _init_import(
            conn, home, cwd, target_workspace, import_all=import_all, interactive=interactive
        )
        _init_snippet(home, cwd)
        _init_self_check(conn, target_workspace)


@main.command("import")
@click.argument(
    "paths",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("-w", "--workspace", default=None, help="Workspace name (default: auto-detected).")
@click.option("--dry-run", is_flag=True, help="Report what would be created and write nothing.")
@click.option("--select", "select", is_flag=True, help="Confirm each file (needs a terminal).")
@click.option(
    "--whole-file",
    "whole_file",
    is_flag=True,
    help="Store each file as one record instead of splitting it — for skills and checklists.",
)
def import_command(
    paths: tuple[Path, ...],
    workspace: str | None,
    dry_run: bool,
    select: bool,
    whole_file: bool,
) -> None:
    """Import markdown instruction files (CLAUDE.md, AGENTS.md, steering) into memory.

    Re-importing an unchanged file adds no rows: every record merges into the one it
    created last time and bumps its ``seen_count``.

    ``--whole-file`` keeps a document intact — a security checklist recalled one bullet
    at a time is not a checklist. Pair it with ``-w global`` to reach it from every repo.
    """
    if select and not _is_interactive():
        raise click.UsageError(
            "--select needs a terminal to ask on. Re-run without it to import every "
            "PATH given, or pass only the paths you want."
        )
    chosen = _select_paths(paths) if select else list(paths)
    target_workspace = _resolve_workspace(workspace)
    if dry_run:
        _echo_import_preview(chosen, target_workspace, whole_file=whole_file)
        return
    if not chosen:
        click.echo("no files selected, nothing imported")
        return
    with _session() as (conn, _path):
        for path in chosen:
            _import_one(conn, path, target_workspace, whole_file=whole_file)


@main.command("agents")
@click.option(
    "--install",
    "install_slug",
    default=None,
    metavar="NAME",
    help="Register localmem for one agent by slug; naming it here is the consent.",
)
@click.option(
    "--repair",
    is_flag=True,
    help=(
        "Update an existing localmem entry whose command is no longer the path this "
        "install resolves to. Without it such an entry is reported and left alone."
    ),
)
def agents_command(install_slug: str | None, repair: bool) -> None:
    """Detect supported agents and print where their config lives.

    With ``--install NAME`` it registers localmem for that one agent, under the same
    merge, backup and refuse-on-malformed rules ``init`` uses.

    The command written into every config is the **absolute path** of the installed
    ``localmem``: an agent launched from the desktop inherits no shell PATH, so a bare
    name leaves it unable to start the server, with nothing printed anywhere. A localmem
    binary that has no lasting path — one running under ``uvx`` — is refused rather than
    written, because a config pointing into a cache fails silently once that cache is
    pruned. `--repair` is what moves an existing entry to a new path.
    """
    home = Path.home()
    cwd = Path.cwd()
    if install_slug is None:
        _echo_agent_table(home, cwd)
        return
    writer = agents.writer_for(install_slug)
    if writer is None:
        raise click.UsageError(
            f"unknown agent {install_slug!r}; known agents: {', '.join(agents.slugs())}"
        )
    try:
        result = writer.apply(home, cwd, repair=repair)
    except agents.UnresolvableCommandError as exc:
        # Nothing has been written at this point: server_entry() raises before any writer
        # opens a file. Exit non-zero so a script installing localmem fails here rather
        # than believing an agent was registered (AC1.4).
        raise click.ClickException(str(exc)) from exc
    _echo_apply_result(writer, result, home, cwd)


@main.command("benchmark")
@click.argument("paths", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-w", "--workspace", default=None, help="Workspace name (default: auto-detected).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def benchmark_command(paths: tuple[Path, ...], workspace: str | None, as_json: bool) -> None:
    """Estimate what instruction files cost per session against localmem's fixed cost.

    The "after" figure is one fixed overhead per session — pointer snippet, the two MCP
    tool descriptions and core memory — not a charge per file.
    """
    home = Path.home()
    cwd = Path.cwd()
    targets = _unique_paths([*agents.find_instruction_files(home, cwd), *paths])
    with _session() as (conn, _path):
        target_workspace = _resolve_workspace(workspace)
        core = core_memory.build_core_memory(conn, target_workspace)
    report = benchmark.measure(targets, core.text)
    if as_json:
        click.echo(json.dumps(_benchmark_payload(report, target_workspace), ensure_ascii=False))
        return
    _echo_benchmark(report, target_workspace)


@main.command("eval")
@click.option(
    "--fixture",
    "fixture_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Fixture to measure (default: the bundled bilingual corpus).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def eval_command(fixture_path: Path | None, as_json: bool) -> None:
    """Measure retrieval quality against a graded fixture.

    Builds a throwaway database, loads the fixture through the ordinary write path and
    runs every query through the ordinary recall path, then reports recall@k, MRR and how
    many off-corpus queries stayed silent. The user's own database is never opened.

    This is a report, so it always exits 0. The regression gate lives in the test suite,
    which pins this output against ``tests/fixtures/eval/baseline.json``.
    """
    try:
        fixture = evaluate.load_fixture(fixture_path)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix="localmem-eval-") as scratch:
        try:
            report = evaluate.run_eval(fixture, Path(scratch) / "eval.db")
        except (ValueError, RuntimeError, sqlite3.Error) as exc:
            raise click.ClickException(f"eval failed: {exc}") from exc
    if as_json:
        click.echo(json.dumps(report.summary(), ensure_ascii=False))
        return
    _echo_eval(report)


def _echo_eval(report: evaluate.EvalReport) -> None:
    """Print the metrics table, then every query that did not land where it should."""
    click.echo(f"fixture: {report.fixture_name} (v{report.fixture_version})")
    click.echo(
        f"{report.positive_queries} queries with a stored answer, "
        f"{report.negative_queries} off-corpus"
    )
    click.echo("")
    for cutoff, value in sorted(report.recall.items()):
        click.echo(f"  recall@{cutoff}          {value:.4f}")
    click.echo(f"  MRR                 {report.mrr:.4f}")
    click.echo(
        f"  off-corpus silent   {report.off_corpus_silent}/{report.negative_queries} "
        "(higher is better: a query with no stored answer should return nothing)"
    )

    click.echo("\nanswered by:")
    for source in evaluate.ANSWER_SOURCES:
        click.echo(f"  {source:<12} {report.answered_by[source]}")
    if not report.answered_by[evaluate.ANSWERED_BY_BOTH]:
        click.echo(
            "  note: no query engaged both views, so this run is NOT evidence about the\n"
            "        fusion weights — with one view empty they only rescale the ranking."
        )

    misses = [
        outcome
        for outcome in report.outcomes
        if not outcome.is_negative and outcome.first_gold_rank is None
    ]
    ranked_low = [
        (outcome, outcome.first_gold_rank)
        for outcome in report.outcomes
        if not outcome.is_negative
        and outcome.first_gold_rank is not None
        and outcome.first_gold_rank > 1
    ]
    leaks = [outcome for outcome in report.outcomes if outcome.is_negative and not outcome.silent]

    if misses:
        click.echo("\nmissed entirely:")
        for outcome in misses:
            top = outcome.returned[0] if outcome.returned else "(nothing)"
            click.echo(f"  {outcome.query_id}: {outcome.text!r}")
            click.echo(f"    wanted {', '.join(outcome.gold)} — got {top} first")
    if ranked_low:
        click.echo("\nfound but not first:")
        for outcome, rank in ranked_low:
            click.echo(
                f"  {outcome.query_id}: rank {rank} "
                f"(above it: {', '.join(outcome.returned[: rank - 1])})"
            )
    if leaks:
        click.echo("\noff-corpus queries that returned something anyway:")
        for outcome in leaks:
            marker = " [fallback]" if outcome.from_fallback else ""
            click.echo(f"  {outcome.query_id}: {outcome.text!r} → {outcome.returned[0]}{marker}")


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


def _core_cap_warning(conn: sqlite3.Connection, result: store.PromoteResult) -> str | None:
    """Return the warning a promotion into ``core`` earns, or ``None``.

    Core memory is the one tier that is *pushed* into every recall, and it is capped at
    :data:`localmem.core_memory.CORE_MEMORY_TOKEN_CAP` estimated tokens; past that, whole
    rows are dropped oldest-first and silently stop being loaded. Promoting a long memory
    into that tier can therefore evict another one, which is worth saying out loud at the
    moment it happens.

    The sizing is :func:`localmem.core_memory.build_core_memory`'s, not a second
    implementation of the cap: it is asked what this workspace's core memory looks like
    *after* the update, and reports whatever that call says is being dropped.

    Written to stderr by the caller, so stdout stays a single JSON object.
    """
    if result.kind != core_memory.CORE_KIND:
        return None
    built = core_memory.build_core_memory(conn, result.workspace)
    if not built.dropped:
        return None
    return (
        f"warning: {built.dropped} core rows in workspace {result.workspace!r} are hidden "
        f"by the {core_memory.CORE_MEMORY_TOKEN_CAP}-token cap and will not be loaded on "
        f"recall; what a recall does load is ~{built.tokens} estimated tokens. "
        "`localmem audit` reports the core tier per workspace"
    )


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


def _echo_context(outcome: retriever.RetrievalResult, include_fallback: bool = False) -> None:
    """Print the compact block a prompt hook injects, or nothing at all.

    Three rules, all about a hook that runs on *every* prompt:

    * no hits prints **nothing** — not the friendly empty-database message, which would
      otherwise be pasted into the context of every prompt the user ever writes;
    * core memory is left out. It is loaded on demand by an ordinary recall; injecting
      it per prompt would turn a capped 400-token block into a fixed per-prompt cost,
      which is the charge localmem exists to avoid (``docs/design_decisions.md`` §30);
    * OR-fallback hits are **dropped** unless ``include_fallback`` says otherwise. The
      fallback cannot stay silent — on ten queries whose answer was not stored at all it
      returned plausible-looking rows ten times out of ten — and this is the one caller
      that pays for that on every single prompt. An ordinary ``search`` and
      ``memory_recall`` still return them, for an agent to judge for itself.
    """
    shown = [hit for hit in outcome.results if include_fallback or not hit.from_fallback]
    if not shown:
        return
    click.echo(_CONTEXT_HEADER)
    for hit in shown:
        click.echo(f"- ({hit.workspace}) {_context_snippet(hit)}")


def _context_snippet(hit: retriever.RetrievedMemory) -> str:
    """Collapse a memory onto one line, truncated with the way to read the rest."""
    collapsed = " ".join(hit.content.split())
    if len(collapsed) <= CONTEXT_SNIPPET_CHARS:
        return collapsed
    head = collapsed[: _cut_point(collapsed, CONTEXT_SNIPPET_CHARS)]
    return f"{head}… (memory_recall id {hit.id} for full text)"


def _cut_point(text: str, limit: int) -> int:
    """Return the largest cut at or below ``limit`` that keeps clusters whole.

    Vietnamese in NFD is `e` + U+0302 + U+0301, three codepoints for one letter. Slicing
    between them is valid UTF-8 and visibly wrong — `ế` becomes `e` — so the cut backs off
    while the first *dropped* codepoint is a combining mark. ``unicodedata`` is enough for
    this: the cases that need a real grapheme library (emoji ZWJ sequences, regional
    indicators) render as one glyph either side of the cut and cost nothing when split.
    """
    cut = limit
    while cut > 0 and unicodedata.combining(text[cut]) != 0:
        cut -= 1
    return cut


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


def _echo_forget_target(target: store.ForgetTarget, *, force: bool) -> None:
    """Print the row `forget` is about to remove, and everything that goes with it.

    Printed before the prompt, before ``--dry-run`` returns, and before ``--yes`` acts,
    so the three paths always show the user the same thing.
    """
    click.echo(
        f"memory {target.id} — workspace={target.workspace} kind={target.kind} "
        f"created={target.created_at} seen={target.seen_count} "
        f"recalled={target.recalled_count}"
    )
    if target.source:
        click.echo(f"   source: {target.source}")
    for line in target.content.splitlines():
        click.echo(f"   {line}")
    click.echo(
        f"also removes: {target.entity_links} entity links, "
        f"{target.queued_pairs} queued near-duplicate pairs, and its full-text index entry"
    )
    if not target.supersedes:
        return
    _echo_forget_dependents(target, force=force)


def _echo_forget_dependents(target: store.ForgetTarget, *, force: bool) -> None:
    """Print the memories this one corrects, and what deleting it does to them.

    Without ``--force`` this is a preview of the refusal the store will raise. With it,
    it is the warning — said plainly, because un-retracting a correction is a change to
    what future recalls answer, not a bookkeeping detail.
    """
    total = len(target.supersedes)
    noun = "memory" if total == 1 else "memories"
    click.echo(f"memory {target.id} supersedes {total} {noun}:")
    for row in target.supersedes[: store.FORGET_DEPENDENT_LIMIT]:
        click.echo(f"   id={row.id} ({row.workspace}) {_preview(row.content)}")
    hidden = total - min(total, store.FORGET_DEPENDENT_LIMIT)
    if hidden:
        click.echo(f"   … and {hidden} more")
    if force:
        click.echo(
            f"warning: --force clears superseded_by on {'that' if total == 1 else 'those'} "
            f"{noun}, which restores {'it' if total == 1 else 'them'} to full rank in "
            "every future recall — the correction goes away and what it corrected comes "
            "back."
        )
        return
    click.echo("this will be refused without --force")


def _confirm_forget(target: store.ForgetTarget) -> bool:
    """Ask before deleting, or fail when there is no terminal to ask on.

    The no-terminal case is an error and not a default, either way round. Deleting would
    destroy a memory nobody agreed to lose; skipping silently would let a script believe
    it had removed a secret that is still there.
    """
    if not _is_interactive():
        raise click.UsageError(
            f"deleting memory {target.id} needs a yes, and there is no terminal to ask "
            "on. Re-run with --yes if you are sure, or with --dry-run to see what would "
            "go. Nothing was written."
        )
    return click.confirm(f"Permanently delete memory {target.id}?", default=False)


def _echo_forget_result(result: store.ForgetResult) -> None:
    """Report exactly what the delete removed, including the entity sweep."""
    click.echo(f"deleted memory {result.id}")
    click.echo(f"removed {result.entities_removed} entities no other memory still uses")
    if not result.restored:
        return
    ids = ", ".join(str(memory_id) for memory_id in result.restored)
    click.echo(
        f"cleared superseded_by on {len(result.restored)} memories (id {ids}); they are "
        "no longer retracted and are back at full rank"
    )


def _echo_trace_prune_preview(traces: store.TracePruneReport) -> None:
    """Print exactly which traces a real run would delete, and what it would spare."""
    click.echo(
        f"would delete {traces.eligible} never-recalled traces older than "
        f"{traces.days} days (showing {len(traces.samples)})"
    )
    for trace in traces.samples:
        click.echo(f"   id={trace.id} workspace={trace.workspace} created={trace.created_at}")
        click.echo(f"     {_preview(trace.content)}")
    if traces.protected:
        click.echo(
            f"   keeping {traces.protected} otherwise-prunable traces that another "
            "memory names as its replacement"
        )


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


def _init_database(path: Path) -> None:
    """Step 1 — the database is already created and migrated by :func:`_session`."""
    click.echo("Step 1 — database")
    click.echo(f"  ready at {path}")


def _init_agents(home: Path, cwd: Path, *, yes: bool, interactive: bool) -> None:
    """Step 2 — ask about each detected agent individually and write only on a yes.

    An unresolvable command stops this step and only this step. ``init``'s contract is
    that every step is independent and a failing one leaves the others intact, and steps
    3 to 5 — importing instruction files, printing the snippet, proving recall works —
    need no agent config at all. The refusal is printed in full, so it is loud without
    taking the rest of the setup down with it; `localmem agents --install NAME` is the
    command that exits non-zero on the same condition.
    """
    click.echo("\nStep 2 — agent configuration")
    detected = agents.detect_agents(home, cwd)
    if not detected:
        click.echo("  no supported agent detected; nothing to register")
        return
    try:
        for writer in detected:
            _register_agent(writer, home, cwd, yes=yes, interactive=interactive)
    except agents.UnresolvableCommandError as exc:
        click.echo(f"  no agent was registered: {exc}")


def _register_agent(
    writer: agents.AgentWriter,
    home: Path,
    cwd: Path,
    *,
    yes: bool,
    interactive: bool,
) -> None:
    if yes or (interactive and _confirm_agent(writer)):
        _echo_apply_result(writer, writer.apply(home, cwd), home, cwd)
        return
    if interactive:
        click.echo(f"  {writer.name}: skipped")
        return
    # No terminal to ask on and no flag: report the intent, write nothing.
    _echo_apply_result(writer, writer.apply(home, cwd, dry_run=True), home, cwd)
    click.echo("    (no terminal to ask on; nothing written — re-run with --yes to apply)")


def _confirm_agent(writer: agents.AgentWriter) -> bool:
    return click.confirm(f"  Register localmem as an MCP server for {writer.name}?", default=False)


def _echo_apply_result(
    writer: agents.AgentWriter,
    result: agents.ApplyResult,
    home: Path,
    cwd: Path,
) -> None:
    """Report one writer's outcome, including what to do by hand when it refused."""
    click.echo(f"  {result.agent}: {result.detail}")
    if result.printed_command is not None:
        click.echo(f"    run: {result.printed_command}")
    if result.action != "refused":
        return
    click.echo("    add this by hand instead:")
    for line in writer.render_config(home, cwd).splitlines():
        click.echo(f"      {line}")


def _init_import(
    conn: sqlite3.Connection,
    home: Path,
    cwd: Path,
    workspace: str,
    *,
    import_all: bool,
    interactive: bool,
) -> None:
    """Step 3 — a question of its own, never bundled with step 2."""
    click.echo("\nStep 3 — import existing instruction files")
    found = agents.find_instruction_files(home, cwd)
    if not found:
        click.echo("  no CLAUDE.md, AGENTS.md or Kiro steering file found; nothing to import")
        return
    for path in found:
        click.echo(f"  found {path}")
    chosen = _choose_import_files(found, import_all=import_all, interactive=interactive)
    if not chosen:
        # The original spec §8 step 3, verbatim and unindented so the line is exactly the message.
        click.echo(importer.SKIP_MESSAGE)
        return
    for path in chosen:
        _import_one(conn, path, workspace)


def _choose_import_files(
    found: Sequence[Path], *, import_all: bool, interactive: bool
) -> list[Path]:
    if import_all:
        return list(found)
    if not interactive:
        return []
    choice = click.prompt(
        "  [a] import all / [s] select files / [n] skip", default="n", show_default=True
    )
    answer = choice.strip().lower()[:1]
    if answer == "a":
        return list(found)
    if answer == "s":
        return _select_paths(found)
    return []


def _select_paths(paths: Sequence[Path]) -> list[Path]:
    return [path for path in paths if click.confirm(f"    import {path}?", default=False)]


def _import_one(
    conn: sqlite3.Connection, path: Path, workspace: str, *, whole_file: bool = False
) -> None:
    """Import one file, reporting a read failure without aborting the rest."""
    try:
        outcome = importer.import_file(conn, path, workspace, whole_file=whole_file)
    except (OSError, UnicodeDecodeError) as exc:
        click.echo(f"  cannot read {path}: {exc}")
        return
    click.echo(
        f"  {outcome.path}: {outcome.records} records — "
        f"{outcome.added} new, {outcome.merged} merged into existing"
    )
    click.echo(f"  {importer.TRIM_SUGGESTION_TEMPLATE.format(path=outcome.path)}")


def _echo_import_preview(
    paths: Sequence[Path], workspace: str, *, whole_file: bool = False
) -> None:
    """Print what an import would create. Opens no database and writes nothing."""
    records: list[str] = []
    for path in paths:
        try:
            records.extend(importer.read_records(path, whole_file=whole_file))
        except (OSError, UnicodeDecodeError) as exc:
            raise click.ClickException(f"cannot read {path}: {exc}") from exc
    click.echo(f"would create {len(records)} records in workspace {workspace!r}")
    for content in records[: importer.DRY_RUN_PREVIEW]:
        click.echo("  ---")
        for line in content.splitlines():
            click.echo(f"  {line}")
    click.echo("nothing was written")


def _init_snippet(home: Path, cwd: Path) -> None:
    """Step 4 — print the pointer snippet; never edit an instruction file."""
    click.echo("\nStep 4 — pointer snippet")
    detected = agents.detect_agents(home, cwd)
    if detected:
        click.echo(f"  for: {', '.join(writer.name for writer in detected)}")
    click.echo("  Paste this into the instruction file your agent already loads.")
    click.echo("  localmem never edits those files for you.\n")
    for line in agents.POINTER_SNIPPET.splitlines():
        click.echo(line)


def _init_self_check(conn: sqlite3.Connection, workspace: str) -> None:
    """Step 5 — run one real recall, then print where to go next."""
    click.echo("\nStep 5 — self-check")
    outcome = retriever.retrieve(conn, _SELF_CHECK_QUERY, workspace, 1)
    if outcome.results:
        hit = outcome.results[0]
        click.echo(f"  recall works — id={hit.id}: {_preview(hit.content)}")
    else:
        click.echo(f"  recall works — {outcome.message or 'nothing matched yet'}")
    click.echo("  next steps:")
    for command in _NEXT_STEPS:
        click.echo(f"    {command}")


def _echo_agent_table(home: Path, cwd: Path) -> None:
    slug_width = max(len(writer.slug) for writer in agents.WRITERS)
    name_width = max(len(writer.name) for writer in agents.WRITERS)
    for writer in agents.WRITERS:
        state = "detected " if writer.is_detected(home, cwd) else "not found"
        target = writer.target_path(home, cwd)
        location = str(target) if target is not None else "(no project config — prints a command)"
        click.echo(
            f"{writer.slug.ljust(slug_width)}  {writer.name.ljust(name_width)}  {state}  {location}"
        )
    click.echo("\nregister one with: localmem agents --install NAME")


def _unique_paths(paths: Sequence[Path]) -> list[Path]:
    """Return ``paths`` with duplicates removed, first occurrence winning."""
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        key = path if not path.exists() else path.resolve()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _echo_benchmark(report: benchmark.Benchmark, workspace: str) -> None:
    click.echo(f"workspace: {workspace}")
    if not report.files:
        click.echo("no instruction files found; pass paths explicitly to measure them")
    else:
        width = max(len(str(cost.path)) for cost in report.files)
        for cost in report.files:
            measured = (
                f"unreadable: {cost.error}" if cost.error else f"~{cost.tokens} estimated tokens"
            )
            click.echo(f"  {str(cost.path).ljust(width)}  {measured}")
    click.echo(f"\nbefore (pushed every session): ~{report.before_tokens} estimated tokens")
    click.echo(f"after  (pulled on demand):     ~{report.after_tokens} estimated tokens")
    click.echo(f"    pointer snippet:   ~{report.pointer_tokens}")
    click.echo(f"    tool descriptions: ~{report.tool_description_tokens}")
    click.echo(f"    core memory:       ~{report.core_memory_tokens}")
    click.echo(f"saved: ~{report.saved_tokens} estimated tokens ({report.saved_pct}%)\n")
    # The original spec §10 step 4, verbatim and unindented.
    click.echo(benchmark.CAVEAT)


def _benchmark_payload(report: benchmark.Benchmark, workspace: str) -> dict[str, object]:
    return {
        "workspace": workspace,
        "files": [
            {"path": str(cost.path), "before_tokens": cost.tokens, "error": cost.error}
            for cost in report.files
        ],
        "before_tokens": report.before_tokens,
        "after_tokens": report.after_tokens,
        "saved_tokens": report.saved_tokens,
        "saved_pct": report.saved_pct,
        "after_breakdown": {
            "pointer_snippet_tokens": report.pointer_tokens,
            "tool_description_tokens": report.tool_description_tokens,
            "core_memory_tokens": report.core_memory_tokens,
        },
        "caveat": benchmark.CAVEAT,
    }


def _echo_audit(report: audit.Audit) -> None:
    """Render the hygiene report as seven numbered sections."""
    scope = report.workspace if report.workspace is not None else "every workspace"
    click.echo(f"localmem audit — {scope}")
    click.echo(f"database: {report.db_path} ({_format_size(report.db_size_bytes)})")
    _echo_audit_queue(report.queue)
    _echo_audit_promotions(report.promotion_candidates)
    _echo_audit_distribution(report)
    _echo_audit_core(report.core_health)
    _echo_audit_dead(report)
    _echo_audit_superseded(report)
    _echo_audit_lessons(report.lessons)


def _echo_audit_queue(queue: audit.QueueReport) -> None:
    click.echo("\n1. near-duplicate queue")
    if not queue.pending:
        click.echo("   no pending pairs — nothing waiting for review")
        return
    click.echo(f"   pending pairs: {queue.pending}, oldest {_age(queue.oldest_age_days)}")
    for name, count in queue.per_workspace:
        click.echo(f"     {name}  {count}")
    for pair in queue.samples:
        # Reused verbatim from `dedupe --list`, so the two commands describe a pair
        # identically; that is also why these lines are not indented like the rest.
        _echo_pair(pair)
    click.echo("   resolve with:")
    for command in audit.QUEUE_COMMANDS:
        click.echo(f"     {command}")


def _echo_audit_promotions(candidates: tuple[audit.PromotionCandidate, ...]) -> None:
    click.echo("\n2. core memory promotion candidates")
    if not candidates:
        click.echo(f"   nothing has been seen {audit.PROMOTION_SEEN_COUNT_THRESHOLD} times yet")
        return
    for candidate in candidates:
        click.echo(
            f"   id={candidate.id} workspace={candidate.workspace} kind={candidate.kind} "
            f"seen={candidate.seen_count} recalled={candidate.recalled_count}"
        )
        click.echo(f"     {_preview(candidate.content)}")
    click.echo(f"   {audit.PROMOTION_NOTE}")


def _echo_audit_distribution(report: audit.Audit) -> None:
    click.echo("\n3. distribution")
    click.echo(
        f"   memories: {report.total_memories}   entities: {report.total_entities}   "
        f"entity links: {report.total_entity_links}"
    )
    if not report.workspaces:
        click.echo("   (no memories yet)")
        return
    width = max(len(summary.workspace) for summary in report.workspaces)
    for summary in report.workspaces:
        kinds = ", ".join(f"{kind} {count}" for kind, count in summary.per_kind)
        click.echo(f"   {summary.workspace.ljust(width)}  {summary.total}  ({kinds})")
        click.echo(
            f"   {' '.ljust(width)}  oldest {summary.oldest_created_at}, "
            f"newest {summary.newest_created_at}"
        )


def _echo_audit_core(core_health: tuple[audit.CoreHealth, ...]) -> None:
    click.echo("\n4. core memory health")
    if not core_health:
        click.echo("   no core memories yet")
        return
    width = max(len(health.workspace) for health in core_health)
    for health in core_health:
        hidden = f"  (hidden by the cap: {health.dropped})" if health.dropped else ""
        click.echo(
            f"   {health.workspace.ljust(width)}  ~{health.tokens}/{health.cap} tokens{hidden}"
        )


def _echo_audit_dead(report: audit.Audit) -> None:
    click.echo("\n5. dead memories")
    if not report.dead_total:
        click.echo(
            f"   none — every memory older than {audit.DEAD_MEMORY_AGE_DAYS} days has "
            "been recalled at least once"
        )
        return
    click.echo(
        f"   never recalled and older than {audit.DEAD_MEMORY_AGE_DAYS} days: "
        f"{report.dead_total} (showing {len(report.dead)})"
    )
    for dead in report.dead:
        click.echo(
            f"   id={dead.id} workspace={dead.workspace} kind={dead.kind} "
            f"created={dead.created_at} ({_age(dead.age_days)})"
        )
        click.echo(f"     {_preview(dead.content)}")
    click.echo(f"   {audit.DEAD_MEMORY_NOTE}")


def _echo_audit_superseded(report: audit.Audit) -> None:
    click.echo("\n6. superseded memories")
    if not report.superseded_total:
        click.echo("   none — nothing in here has been corrected by a later memory")
        return
    click.echo(
        f"   corrected by a later memory: {report.superseded_total} "
        f"(showing {len(report.superseded)})"
    )
    for row in report.superseded:
        click.echo(
            f"   id={row.id} workspace={row.workspace} kind={row.kind} created={row.created_at}"
        )
        click.echo(f"     {_preview(row.content)}")
        click.echo(f"     superseded by id={row.replacement_id} ({row.replacement_workspace})")
        click.echo(f"     {_preview(row.replacement_content)}")
    click.echo(f"   {audit.SUPERSEDED_NOTE}")


def _echo_audit_lessons(lessons: audit.LessonHealth) -> None:
    """Render section 7 — is the store learning, and what would the gate do today?"""
    click.echo("\n7. lesson health")
    # First, before any number that leans on recall counts can be misread as a fact.
    if lessons.tracking_disabled:
        click.echo(f"   {audit.TRACKING_DISABLED_NOTE}")
    click.echo(
        f"   active lessons: {lessons.active_total} "
        f"(superseded: {lessons.superseded_lessons} — listed in section 6)"
    )
    for name, count in lessons.active_per_workspace:
        click.echo(f"     {name}  {count}")

    if not lessons.stale_total:
        click.echo(f"   none unused for {lessons.stale_age_days} days")
    else:
        click.echo(
            f"   never recalled in {lessons.stale_age_days} days: {lessons.stale_total} "
            f"(showing {len(lessons.stale)})"
        )
        for lesson in lessons.stale:
            click.echo(
                f"     id={lesson.id} workspace={lesson.workspace} "
                f"created={lesson.created_at} ({_age(lesson.age_days)})"
            )
            click.echo(f"       {_preview(lesson.content)}")

    if not lessons.unread_total:
        click.echo("   nothing is being stored repeatedly and never read back")
    else:
        click.echo(
            f"   stored repeatedly, never read back "
            f"(seen >= {audit.UNREAD_SEEN_COUNT_THRESHOLD}, "
            f"recalled < {audit.UNREAD_RECALLED_COUNT_THRESHOLD}): "
            f"{lessons.unread_total} (showing {len(lessons.unread)})"
        )
        for row in lessons.unread:
            click.echo(
                f"     id={row.id} workspace={row.workspace} kind={row.kind} "
                f"seen={row.seen_count} recalled={row.recalled_count}"
            )
            click.echo(f"       {_preview(row.content)}")

    _echo_audit_prunable(lessons.prunable)
    _echo_audit_similarity(lessons.similarity)


def _echo_audit_prunable(prunable: store.TracePruneReport) -> None:
    # The scope is named in the line itself. Without it this count sits directly above the
    # similarity figures, which are workspace-scoped, and the two read as contradicting
    # each other whenever the traces live somewhere else.
    scope = (
        "across every workspace"
        if prunable.workspace is None
        else f"in workspace {prunable.workspace!r}"
    )
    click.echo(
        f"   traces eligible for `gc --prune-traces {prunable.days}` {scope}: "
        f"{prunable.eligible}"
        + (f", kept as a replacement: {prunable.protected}" if prunable.protected else "")
    )
    click.echo(f"   {audit.PRUNE_NOTE}")


def _echo_audit_similarity(similarity: audit.TraceSimilarity) -> None:
    """Render the Jaccard distribution the capture threshold can be re-derived from."""
    if not similarity.scanned:
        click.echo("   trace similarity: no traces stored yet, nothing to measure")
        return
    sampled = "" if similarity.scanned == similarity.total else f" of {similarity.total}"
    click.echo(
        f"   trace similarity over {similarity.scanned}{sampled} traces "
        f"(median {similarity.median:g}, {similarity.with_neighbour} with any neighbour):"
    )
    widest = max(bucket.count for bucket in similarity.buckets) or 1
    for bucket in similarity.buckets:
        mark = " <- gate" if bucket.lower == similarity.threshold else ""
        bar = "#" * round(_HISTOGRAM_BAR_CHARS * bucket.count / widest)
        click.echo(
            f"     {bucket.lower:.2f}-{bucket.upper:.2f}  {str(bucket.count).rjust(4)}  {bar}{mark}"
        )
    click.echo(
        f"   at or above {similarity.threshold}: {similarity.at_or_above_threshold} "
        "— these are what the capture gate would skip today"
    )
    click.echo(f"   {audit.SIMILARITY_NOTE}")


def _age(age_days: float | None) -> str:
    """Render an age in days, or say so when the timestamp could not be read."""
    return "age unknown" if age_days is None else f"{age_days:g} days old"


def _audit_payload(report: audit.Audit) -> dict[str, object]:
    """Return the whole report as JSON-ready data — same numbers, no rendering."""
    return {
        "workspace": report.workspace,
        "database": {
            "path": str(report.db_path),
            "size_bytes": report.db_size_bytes,
            "memories": report.total_memories,
            "entities": report.total_entities,
            "entity_links": report.total_entity_links,
        },
        "queue": {
            "pending": report.queue.pending,
            "per_workspace": [
                {"workspace": name, "pending": count} for name, count in report.queue.per_workspace
            ],
            "oldest_queued_at": report.queue.oldest_queued_at,
            "oldest_age_days": report.queue.oldest_age_days,
            "samples": [_pair_payload(pair) for pair in report.queue.samples],
            "commands": list(audit.QUEUE_COMMANDS),
        },
        "promotion_candidates": {
            "seen_count_threshold": audit.PROMOTION_SEEN_COUNT_THRESHOLD,
            "note": audit.PROMOTION_NOTE,
            "rows": [
                {
                    "id": candidate.id,
                    "workspace": candidate.workspace,
                    "kind": candidate.kind,
                    "seen_count": candidate.seen_count,
                    "recalled_count": candidate.recalled_count,
                    "content": candidate.content,
                }
                for candidate in report.promotion_candidates
            ],
        },
        "workspaces": [
            {
                "workspace": summary.workspace,
                "total": summary.total,
                "per_kind": [{"kind": kind, "count": count} for kind, count in summary.per_kind],
                "oldest_created_at": summary.oldest_created_at,
                "newest_created_at": summary.newest_created_at,
            }
            for summary in report.workspaces
        ],
        "core_memory": [
            {
                "workspace": health.workspace,
                "tokens": health.tokens,
                "cap": health.cap,
                "dropped": health.dropped,
            }
            for health in report.core_health
        ],
        "dead_memories": {
            "age_days": audit.DEAD_MEMORY_AGE_DAYS,
            "total": report.dead_total,
            "note": audit.DEAD_MEMORY_NOTE,
            "rows": [
                {
                    "id": dead.id,
                    "workspace": dead.workspace,
                    "kind": dead.kind,
                    "created_at": dead.created_at,
                    "age_days": dead.age_days,
                    "content": dead.content,
                }
                for dead in report.dead
            ],
        },
        "lesson_health": _lesson_health_payload(report.lessons),
        "superseded": {
            "total": report.superseded_total,
            "note": audit.SUPERSEDED_NOTE,
            "rows": [
                {
                    "id": row.id,
                    "workspace": row.workspace,
                    "kind": row.kind,
                    "created_at": row.created_at,
                    "content": row.content,
                    "replacement_id": row.replacement_id,
                    "replacement_workspace": row.replacement_workspace,
                    "replacement_content": row.replacement_content,
                }
                for row in report.superseded
            ],
        },
    }


def _lesson_health_payload(lessons: audit.LessonHealth) -> dict[str, object]:
    """Return section 7 as JSON-ready data — same numbers, same notes, no rendering."""
    return {
        # First key in the object, mirroring the text mode: a consumer that reads the
        # counts without reading this flag is drawing the wrong conclusion from them.
        "tracking_disabled": lessons.tracking_disabled,
        "tracking_note": audit.TRACKING_DISABLED_NOTE if lessons.tracking_disabled else None,
        "active": {
            "total": lessons.active_total,
            "per_workspace": [
                {"workspace": name, "count": count} for name, count in lessons.active_per_workspace
            ],
            "superseded": lessons.superseded_lessons,
        },
        "stale": {
            "age_days": lessons.stale_age_days,
            "total": lessons.stale_total,
            "rows": [
                {
                    "id": lesson.id,
                    "workspace": lesson.workspace,
                    "created_at": lesson.created_at,
                    "age_days": lesson.age_days,
                    "content": lesson.content,
                }
                for lesson in lessons.stale
            ],
        },
        "unread": {
            "seen_count_threshold": audit.UNREAD_SEEN_COUNT_THRESHOLD,
            "recalled_count_threshold": audit.UNREAD_RECALLED_COUNT_THRESHOLD,
            "total": lessons.unread_total,
            "rows": [
                {
                    "id": row.id,
                    "workspace": row.workspace,
                    "kind": row.kind,
                    "seen_count": row.seen_count,
                    "recalled_count": row.recalled_count,
                    "content": row.content,
                }
                for row in lessons.unread
            ],
        },
        "prunable_traces": {
            "days": lessons.prunable.days,
            # Explicit, because `gc --prune-traces` is NOT scoped: a consumer comparing
            # this count against what the command deletes needs to know which it is.
            "workspace": lessons.prunable.workspace,
            "eligible": lessons.prunable.eligible,
            "protected": lessons.prunable.protected,
            "note": audit.PRUNE_NOTE,
        },
        "trace_similarity": {
            "total": lessons.similarity.total,
            "scanned": lessons.similarity.scanned,
            "with_neighbour": lessons.similarity.with_neighbour,
            "threshold": lessons.similarity.threshold,
            "at_or_above_threshold": lessons.similarity.at_or_above_threshold,
            "median": lessons.similarity.median,
            "buckets": [
                {"lower": bucket.lower, "upper": bucket.upper, "count": bucket.count}
                for bucket in lessons.similarity.buckets
            ],
            "note": audit.SIMILARITY_NOTE,
        },
    }


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
