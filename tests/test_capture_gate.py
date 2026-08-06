"""Tests for the v0.5.0 capture gate: ``add --if-novel`` and ``gc --prune-traces``.

Two properties carry this file, and both are about a gate that could silently do nothing.

The redundancy gate is **dead code unless candidate generation is disjunctive.** That is
not a hypothetical: the conjunctive expression tier 2 uses returned zero candidates for
every restatement in the milestone-D fixture, so the Jaccard comparison never ran and the
gate could not fire at any threshold. :func:`test_the_gate_is_not_dead_code` is the
regression detector for exactly that, and it fails if anyone "harmonises" this path with
tier 2's.

The prune is the second: a single trace referenced by another row's ``superseded_by``
aborts the whole bulk ``DELETE`` with a foreign-key error, which would leave the command
reporting success having pruned nothing. Measured, not assumed —
:func:`test_a_referenced_trace_does_not_abort_the_prune` holds the measurement.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from localmem import dedup, store
from localmem.cli import main

WORKSPACE = "proj"

#: One of the milestone-D fixture's real traces, and a later restatement of it in
#: different words. Their Jaccard overlap is 0.314 — comfortably over the 0.25 gate and
#: nowhere near tier 2's 0.7, which is the whole reason this threshold is its own number.
ORIGINAL = (
    "Tests were flaky under xdist because two fixtures shared a temp directory. Gave each "
    "worker its own tmp_path and the intermittent failures stopped."
)
RESTATEMENT = (
    "The flaky tests came back. Same cause as before — fixtures sharing one temp dir "
    "across xdist workers. Each worker needs its own tmp_path."
)
NOVEL = (
    "The deploy failed because the health check probed / while the app only serves "
    "/healthz, so Kubernetes killed every pod before it finished booting."
)


def _add(conn: sqlite3.Connection, content: str, **kwargs: object) -> store.AddResult:
    return store.add_memory(conn, content, WORKSPACE, **kwargs)  # type: ignore[arg-type]


def _age(conn: sqlite3.Connection, memory_id: int, days: int) -> None:
    conn.execute(
        "UPDATE memories SET created_at = datetime('now', ?) WHERE id = ?",
        (f"-{days} days", memory_id),
    )
    conn.commit()


def _kinds(conn: sqlite3.Connection) -> list[str]:
    return [row["kind"] for row in conn.execute("SELECT kind FROM memories ORDER BY id")]


# ------------------------------------------------------------------ the redundancy gate


def test_the_gate_is_not_dead_code(conn: sqlite3.Connection) -> None:
    """**The designated regression detector for a gate that cannot fire.**

    A threshold is worthless if the candidate query never proposes the row it would have
    matched. Measured on the milestone-D fixture: the *conjunctive* expression tier 2
    builds returns 0 candidates for this pair, because a restatement shares only two or
    three of its five top terms with the original — which is precisely what makes it a
    restatement. The disjunctive expression returns it and scores 0.314.

    So this asserts the mechanism, not just the outcome: a real neighbour is found, and
    its score sits above the capture threshold and below tier 2's.
    """
    _add(conn, ORIGINAL, kind="trace")

    neighbour = dedup.nearest_neighbour(
        conn, RESTATEMENT, WORKSPACE, store.build_or_match_expression
    )

    assert neighbour is not None, "no candidate proposed — the gate would never fire"
    assert neighbour.score >= dedup.CAPTURE_JACCARD_THRESHOLD
    assert neighbour.score < dedup.TIER2_JACCARD_THRESHOLD, (
        "this pair must NOT reach tier 2's threshold — if it did, the capture gate would "
        "not need a threshold of its own and this test would be proving nothing"
    )


def test_the_conjunctive_expression_would_have_shipped_a_dead_gate(
    conn: sqlite3.Connection,
) -> None:
    """The negative half of the measurement above, pinned so it cannot be forgotten."""
    _add(conn, ORIGINAL, kind="trace")
    expression = store.build_match_expression(" ".join(dedup.top_terms(RESTATEMENT)))

    rows = conn.execute(
        dedup._CANDIDATE_SQL, (expression, WORKSPACE, 0, dedup.TIER2_MAX_CANDIDATES)
    ).fetchall()

    assert rows == [], (
        "the conjunctive query now finds this pair; if that is a deliberate improvement, "
        "re-measure the capture threshold against it rather than deleting this test"
    )


def test_a_restatement_is_not_stored(conn: sqlite3.Connection) -> None:
    first = _add(conn, ORIGINAL, kind="trace")

    result = _add(conn, RESTATEMENT, kind="trace", if_novel=True)

    assert result.status == store.STATUS_SKIPPED_REDUNDANT
    assert result.id == first.id, "the skip must name the memory that made it redundant"
    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1


def test_a_novel_trace_is_stored(conn: sqlite3.Connection) -> None:
    _add(conn, ORIGINAL, kind="trace")

    result = _add(conn, NOVEL, kind="trace", if_novel=True)

    assert result.status == store.STATUS_ADDED
    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 2


def test_if_novel_still_merges_an_exact_duplicate(conn: sqlite3.Connection) -> None:
    """The gate only ever prevents a NEW row — it must not cost the seen_count signal.

    ``audit`` reports rows that are stored repeatedly and never read back. If ``--if-novel``
    short-circuited ahead of the tier-1 hash lookup, re-capturing the same session would
    stop bumping ``seen_count`` and that signal would quietly go to zero.
    """
    first = _add(conn, ORIGINAL, kind="trace")

    result = _add(conn, ORIGINAL, kind="trace", if_novel=True)

    assert result.status == store.STATUS_DUPLICATE_MERGED
    assert result.id == first.id
    assert result.seen_count == 2


def test_if_novel_writes_nothing_at_all_when_it_skips(conn: sqlite3.Connection) -> None:
    """A skip is a decline, never a resolution: no delete, no edit, no queue row."""
    first = _add(conn, ORIGINAL, kind="trace")
    before = conn.execute(
        "SELECT content, seen_count, updated_at FROM memories WHERE id = ?", (first.id,)
    ).fetchone()
    queue_before = conn.execute("SELECT COUNT(*) AS n FROM dedup_queue").fetchone()["n"]

    _add(conn, RESTATEMENT, kind="trace", if_novel=True)

    after = conn.execute(
        "SELECT content, seen_count, updated_at FROM memories WHERE id = ?", (first.id,)
    ).fetchone()
    assert tuple(after) == tuple(before)
    assert conn.execute("SELECT COUNT(*) AS n FROM dedup_queue").fetchone()["n"] == queue_before


def test_the_first_memory_is_always_novel(conn: sqlite3.Connection) -> None:
    """An empty workspace has nothing to be redundant against."""
    assert _add(conn, ORIGINAL, kind="trace", if_novel=True).status == store.STATUS_ADDED


def test_the_gate_does_not_reach_across_workspaces(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, ORIGINAL, "other", "trace")

    result = store.add_memory(conn, RESTATEMENT, WORKSPACE, "trace", if_novel=True)

    assert result.status == store.STATUS_ADDED


def test_without_the_flag_nothing_changes(conn: sqlite3.Connection) -> None:
    """The default path is byte-for-byte the v0.4.0 behaviour."""
    _add(conn, ORIGINAL, kind="trace")

    assert _add(conn, RESTATEMENT, kind="trace").status == store.STATUS_ADDED


def test_cli_add_if_novel_reports_the_skip() -> None:
    runner = CliRunner()
    assert runner.invoke(main, ["add", ORIGINAL, "-w", WORKSPACE, "--kind", "trace"]).exit_code == 0

    result = runner.invoke(
        main, ["add", RESTATEMENT, "-w", WORKSPACE, "--kind", "trace", "--if-novel"]
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == store.STATUS_SKIPPED_REDUNDANT


def test_cli_refuses_if_novel_with_supersedes(conn: sqlite3.Connection) -> None:
    """Silently dropping a declared correction would be the worse failure."""
    runner = CliRunner()
    added = runner.invoke(main, ["add", ORIGINAL, "-w", WORKSPACE])
    memory_id = json.loads(added.output)["id"]

    result = runner.invoke(
        main, ["add", NOVEL, "-w", WORKSPACE, "--if-novel", "--supersedes", str(memory_id)]
    )

    assert result.exit_code != 0
    assert "--if-novel cannot be combined with --supersedes" in result.output
    # Refused before anything was written: no correction, and no memory either.
    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1
    assert (
        conn.execute("SELECT superseded_by FROM memories WHERE id = ?", (memory_id,)).fetchone()[
            "superseded_by"
        ]
        is None
    )


# ----------------------------------------------------------------------- the trace prune


def test_plain_gc_deletes_no_memory(conn: sqlite3.Connection) -> None:
    """The never-auto-delete principle, asserted on the command that could break it."""
    added = _add(conn, ORIGINAL, kind="trace")
    _age(conn, added.id, 400)

    result = CliRunner().invoke(main, ["gc"])

    assert result.exit_code == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1


def test_prune_traces_removes_only_old_unrecalled_traces(conn: sqlite3.Connection) -> None:
    old_trace = _add(conn, ORIGINAL, kind="trace")
    fresh_trace = _add(conn, NOVEL, kind="trace")
    old_note = _add(conn, "a deliberate note nobody has recalled either", kind="note")
    recalled = _add(conn, "a trace that has proved its worth by being read back", kind="trace")
    for memory_id in (old_trace.id, old_note.id, recalled.id):
        _age(conn, memory_id, 90)
    conn.execute("UPDATE memories SET recalled_count = 3 WHERE id = ?", (recalled.id,))
    conn.commit()

    pruned = store.prune_traces(conn, 30)

    assert pruned == 1
    surviving = {row["id"] for row in conn.execute("SELECT id FROM memories")}
    assert surviving == {fresh_trace.id, old_note.id, recalled.id}


def test_a_referenced_trace_does_not_abort_the_prune(conn: sqlite3.Connection) -> None:
    """**Measured, not assumed.** The FK has no ``ON DELETE``, so deleting a referenced
    row raises ``FOREIGN KEY constraint failed`` — and because the prune is one bulk
    ``DELETE``, an unhandled collision would take every other doomed trace down with it
    and prune *nothing* while reporting success.
    """
    stale = _add(conn, "the wrong diagnosis that a later trace corrected", kind="note")
    correcting_trace = _add(conn, ORIGINAL, kind="trace", supersedes=[stale.id])
    ordinary_trace = _add(conn, NOVEL, kind="trace")
    for memory_id in (stale.id, correcting_trace.id, ordinary_trace.id):
        _age(conn, memory_id, 90)

    pruned = store.prune_traces(conn, 30)

    assert pruned == 1, "the referenced trace aborted the whole statement"
    surviving = {row["id"] for row in conn.execute("SELECT id FROM memories")}
    assert correcting_trace.id in surviving, "a memory's replacement was deleted"
    assert ordinary_trace.id not in surviving
    # The retraction still points somewhere real.
    assert (
        conn.execute("SELECT superseded_by FROM memories WHERE id = ?", (stale.id,)).fetchone()[
            "superseded_by"
        ]
        == correcting_trace.id
    )


def test_the_raw_delete_really_would_have_failed(conn: sqlite3.Connection) -> None:
    """The measurement behind the exclusion, pinned so the reason cannot rot."""
    stale = _add(conn, "the wrong diagnosis that a later trace corrected", kind="note")
    _add(conn, ORIGINAL, kind="trace", supersedes=[stale.id])

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("DELETE FROM memories WHERE kind = 'trace'")


def test_the_preview_lists_exactly_what_the_delete_removes(conn: sqlite3.Connection) -> None:
    """The four prune statements must agree; this is what catches them drifting."""
    stale = _add(conn, "the wrong diagnosis that a later trace corrected", kind="note")
    _add(conn, ORIGINAL, kind="trace", supersedes=[stale.id])
    doomed = _add(conn, NOVEL, kind="trace")
    for memory_id in (stale.id, doomed.id):
        _age(conn, memory_id, 90)
    conn.execute("UPDATE memories SET created_at = datetime('now', '-90 days')")
    conn.commit()

    report = store.count_prunable_traces(conn, 30)
    previewed = {trace.id for trace in report.samples}
    before = {row["id"] for row in conn.execute("SELECT id FROM memories")}
    store.prune_traces(conn, 30)
    after = {row["id"] for row in conn.execute("SELECT id FROM memories")}

    assert previewed == before - after
    assert report.eligible == len(previewed)
    assert report.protected == 1


def test_a_pruned_trace_takes_its_queue_rows_with_it(conn: sqlite3.Connection) -> None:
    """Cascade, confirmed rather than assumed."""
    first = _add(conn, "use pnpm not npm for this repository always", kind="trace")
    second = _add(conn, "use pnpm, not npm, for this repository always!", kind="trace")
    conn.execute(
        "INSERT INTO dedup_queue (memory_id, candidate_id, score) VALUES (?, ?, ?)",
        (second.id, first.id, 0.9),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM dedup_queue").fetchone()["n"] >= 1
    for memory_id in (first.id, second.id):
        _age(conn, memory_id, 90)

    store.prune_traces(conn, 30)

    assert conn.execute("SELECT COUNT(*) AS n FROM dedup_queue").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM memory_entities").fetchone()["n"] == 0


def test_gc_dry_run_prunes_nothing_and_says_what_would_go(conn: sqlite3.Connection) -> None:
    added = _add(conn, ORIGINAL, kind="trace")
    _age(conn, added.id, 90)

    result = CliRunner().invoke(main, ["gc", "--prune-traces", "30", "--dry-run"])

    assert result.exit_code == 0
    assert "would delete 1 never-recalled traces older than 30 days" in result.output
    assert f"id={added.id}" in result.output
    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1


def test_gc_prune_traces_applies(conn: sqlite3.Connection) -> None:
    added = _add(conn, ORIGINAL, kind="trace")
    kept = _add(conn, NOVEL, kind="note")
    for memory_id in (added.id, kept.id):
        _age(conn, memory_id, 90)

    result = CliRunner().invoke(main, ["gc", "--prune-traces", "30"])

    assert result.exit_code == 0
    assert "pruned 1 never-recalled traces older than 30 days" in result.output
    assert _kinds(conn) == ["note"]
