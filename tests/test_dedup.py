"""Tests for M3 tier-2 near-duplicate detection, the review queue and ``gc``.

Tier-1 normalization and hashing are covered in ``test_store.py``; this file starts at
the token-overlap primitives tier 2 decides with.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmem import cli, db, dedup, store
from localmem.cli import main

WORKSPACE = "proj"
LEFT = "use pnpm not npm"
RIGHT = "use pnpm, not npm!"

_SIZE_LINE_RE = re.compile(r"(\d+(?:\.\d+)?) (B|KB|MB|GB)")
_SIZE_MULTIPLIERS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


@contextmanager
def _open(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.open_database(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _queue_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM dedup_queue ORDER BY id").fetchall()


def _age_queue_row(conn: sqlite3.Connection, queue_id: int, created_at: str) -> None:
    conn.execute("UPDATE dedup_queue SET created_at = ? WHERE id = ?", (created_at, queue_id))


# --- similarity primitives --------------------------------------------------


def test_tokenize_uses_the_normalized_form() -> None:
    assert dedup.tokenize("- Use   PNPM, not npm!") == {"use", "pnpm", "not", "npm"}


def test_jaccard_is_one_for_a_punctuation_only_difference() -> None:
    """The gap tier 1 leaves open: same words, different punctuation."""
    assert dedup.jaccard(LEFT, RIGHT) == 1.0


def test_jaccard_is_zero_for_unrelated_text() -> None:
    assert dedup.jaccard(LEFT, "the weather in Hanoi is warm today") == 0.0


def test_jaccard_of_two_empty_texts_is_zero() -> None:
    assert dedup.jaccard("", "   ") == 0.0


def test_jaccard_is_a_ratio_of_the_union() -> None:
    assert dedup.jaccard("a b c d", "a b c e") == pytest.approx(3 / 5)


def test_top_terms_drops_stopwords_and_ranks_by_frequency() -> None:
    terms = dedup.top_terms("the cache is the cache and the loader is not the cache")
    assert terms[0] == "cache"
    assert "the" not in terms
    assert len(terms) <= dedup.TIER2_TOP_TERMS


def test_top_terms_breaks_ties_on_first_occurrence() -> None:
    assert dedup.top_terms("zebra apple mango") == ["zebra", "apple", "mango"]


def test_top_terms_of_pure_stopwords_is_empty() -> None:
    assert dedup.top_terms("the and or of to") == []


# --- tier 2 on write --------------------------------------------------------


def test_near_duplicate_is_queued_but_both_rows_survive(conn: sqlite3.Connection) -> None:
    """AC16."""
    first = store.add_memory(conn, LEFT, WORKSPACE)
    second = store.add_memory(conn, RIGHT, WORKSPACE)

    assert first.id != second.id
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    rows = _queue_rows(conn)
    assert len(rows) == 1
    assert (rows[0]["memory_id"], rows[0]["candidate_id"]) == (second.id, first.id)
    assert rows[0]["status"] == dedup.QUEUE_STATUS_PENDING
    assert rows[0]["score"] == 1.0


def test_tier2_never_changes_the_add_result(conn: sqlite3.Connection) -> None:
    """AC17."""
    store.add_memory(conn, LEFT, WORKSPACE)
    second = store.add_memory(conn, RIGHT, WORKSPACE)
    assert second == (store.STATUS_ADDED, second.id, 1)


def test_cli_add_still_reports_added_for_a_near_duplicate(db_path: Path) -> None:
    """AC17 through the CLI."""
    runner = CliRunner()
    runner.invoke(main, ["add", LEFT, "-w", WORKSPACE])
    result = runner.invoke(main, ["add", RIGHT, "-w", WORKSPACE])
    assert json.loads(result.output) == {"status": "added", "id": 2, "seen_count": 1}
    assert db_path.exists()


def test_unrelated_content_is_not_queued(conn: sqlite3.Connection) -> None:
    """AC18."""
    store.add_memory(conn, LEFT, WORKSPACE)
    store.add_memory(conn, "the weather in Hanoi is warm today", WORKSPACE)
    store.add_memory(conn, "remember to rotate the deploy key", WORKSPACE)
    assert _queue_rows(conn) == []


def test_partial_overlap_below_the_threshold_is_not_queued(conn: sqlite3.Connection) -> None:
    """AC18: sharing most terms is not enough on its own."""
    store.add_memory(conn, "alpha bravo charlie delta", WORKSPACE)
    store.add_memory(conn, "alpha bravo charlie echo foxtrot", WORKSPACE)
    assert dedup.jaccard("alpha bravo charlie delta", "alpha bravo charlie echo foxtrot") < (
        dedup.TIER2_JACCARD_THRESHOLD
    )
    assert _queue_rows(conn) == []


def test_tier2_stays_inside_the_workspace(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, LEFT, "a")
    store.add_memory(conn, RIGHT, "b")
    assert _queue_rows(conn) == []


def test_tier2_is_skipped_when_the_content_is_all_stopwords(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "the and or of to", WORKSPACE)
    store.add_memory(conn, "of to the and or", WORKSPACE)
    assert _queue_rows(conn) == []


def test_tier2_is_skipped_when_nothing_survives_sanitization(conn: sqlite3.Connection) -> None:
    added = store.add_memory(conn, "keep me", WORKSPACE)
    queued = dedup.enqueue_near_duplicates(conn, added.id, "keep me", WORKSPACE, lambda _: "")
    assert queued == []


def test_tier2_contains_a_database_failure(conn: sqlite3.Connection) -> None:
    """DD-11: a tier-2 failure must never cost the user the memory that was written."""
    store.add_memory(conn, LEFT, WORKSPACE)
    second = store.add_memory(conn, RIGHT, WORKSPACE)
    conn.execute("DROP TABLE dedup_queue")

    queued = dedup.enqueue_near_duplicates(
        conn, second.id, RIGHT, WORKSPACE, store.build_match_expression
    )

    assert queued == []
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


# --- queue inspection -------------------------------------------------------


def test_pending_pairs_orders_older_and_newer(conn: sqlite3.Connection) -> None:
    first = store.add_memory(conn, LEFT, WORKSPACE)
    second = store.add_memory(conn, RIGHT, WORKSPACE)
    conn.execute("UPDATE memories SET created_at = '2026-01-01 00:00:00' WHERE id = ?", (first.id,))
    conn.execute(
        "UPDATE memories SET created_at = '2026-02-01 00:00:00' WHERE id = ?", (second.id,)
    )

    pairs = dedup.pending_pairs(conn)
    assert len(pairs) == 1
    assert (pairs[0].older.id, pairs[0].newer.id) == (first.id, second.id)
    assert pairs[0].workspace == WORKSPACE


def test_pending_pairs_can_be_filtered_by_workspace(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, LEFT, "a")
    store.add_memory(conn, RIGHT, "a")
    store.add_memory(conn, LEFT, "b")
    store.add_memory(conn, RIGHT, "b")
    assert len(dedup.pending_pairs(conn)) == 2
    assert [pair.workspace for pair in dedup.pending_pairs(conn, "a")] == ["a"]


# --- resolution -------------------------------------------------------------


def test_merge_keeps_the_newer_row_and_folds_seen_count(conn: sqlite3.Connection) -> None:
    """AC20."""
    older = store.add_memory(conn, LEFT, WORKSPACE)
    store.add_memory(conn, LEFT, WORKSPACE)  # tier 1 bumps the older row to seen_count 2
    newer = store.add_memory(conn, RIGHT, WORKSPACE)
    pair = dedup.pending_pairs(conn)[0]

    resolution = dedup.resolve_merge(conn, pair.queue_id)

    assert (resolution.kept_id, resolution.removed_id) == (newer.id, older.id)
    assert resolution.status == dedup.QUEUE_STATUS_MERGED
    assert resolution.seen_count == 3
    assert (
        conn.execute("SELECT seen_count FROM memories WHERE id = ?", (newer.id,)).fetchone()[0] == 3
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?", (older.id,)).fetchone()[0] == 0
    )
    # Deleting the older memory cascades its entity links — and this queue row — away.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM memory_entities WHERE memory_id = ?", (older.id,)
        ).fetchone()[0]
        == 0
    )
    assert dedup.pending_pairs(conn) == []


def test_merging_the_same_pair_twice_is_a_clean_error(conn: sqlite3.Connection) -> None:
    """AC20."""
    store.add_memory(conn, LEFT, WORKSPACE)
    store.add_memory(conn, RIGHT, WORKSPACE)
    pair = dedup.pending_pairs(conn)[0]
    dedup.resolve_merge(conn, pair.queue_id)

    with pytest.raises(ValueError, match="no pending near-duplicate pair"):
        dedup.resolve_merge(conn, pair.queue_id)
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_keep_both_marks_the_pair_and_removes_nothing(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, LEFT, WORKSPACE)
    store.add_memory(conn, RIGHT, WORKSPACE)
    pair = dedup.pending_pairs(conn)[0]

    resolution = dedup.resolve_keep_both(conn, pair.queue_id)

    assert resolution.status == dedup.QUEUE_STATUS_KEPT_BOTH
    assert resolution.removed_id is None
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    assert _queue_rows(conn)[0]["status"] == dedup.QUEUE_STATUS_KEPT_BOTH
    assert dedup.pending_pairs(conn) == []


def test_resolving_an_unknown_pair_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no pending near-duplicate pair with id 42"):
        dedup.resolve_keep_both(conn, 42)


# --- gc ---------------------------------------------------------------------


def test_prune_only_removes_resolved_rows_past_the_window(conn: sqlite3.Connection) -> None:
    """AC21."""
    store.add_memory(conn, LEFT, "a")
    store.add_memory(conn, RIGHT, "a")
    store.add_memory(conn, LEFT, "b")
    store.add_memory(conn, RIGHT, "b")
    resolved, pending = dedup.pending_pairs(conn)
    dedup.resolve_keep_both(conn, resolved.queue_id)
    _age_queue_row(conn, resolved.queue_id, "2020-01-01 00:00:00")

    assert dedup.count_prunable(conn) == 1
    assert dedup.prune_resolved(conn) == 1
    remaining = _queue_rows(conn)
    assert [row["id"] for row in remaining] == [pending.queue_id]
    assert dedup.count_prunable(conn) == 0


def test_prune_leaves_recent_resolved_rows_alone(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, LEFT, WORKSPACE)
    store.add_memory(conn, RIGHT, WORKSPACE)
    pair = dedup.pending_pairs(conn)[0]
    dedup.resolve_keep_both(conn, pair.queue_id)
    assert dedup.prune_resolved(conn) == 0
    assert len(_queue_rows(conn)) == 1


def test_vacuum_runs_outside_a_transaction(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, LEFT, WORKSPACE)
    dedup.vacuum(conn)
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


# --- CLI --------------------------------------------------------------------


def _seed_pair(runner: CliRunner) -> None:
    runner.invoke(main, ["add", LEFT, "-w", WORKSPACE])
    runner.invoke(main, ["add", RIGHT, "-w", WORKSPACE])


def test_cli_dedupe_lists_pending_pairs_without_a_tty(db_path: Path) -> None:
    """AC19: no TTY, no flags — print and exit, never wait for input."""
    runner = CliRunner()
    _seed_pair(runner)
    result = runner.invoke(main, ["dedupe", "--review", "--list"])
    assert result.exit_code == 0
    assert "pair 1" in result.output
    assert LEFT in result.output and RIGHT in result.output
    assert db_path.exists()


def test_cli_dedupe_without_flags_prints_the_queue(db_path: Path) -> None:
    """AC19."""
    runner = CliRunner()
    _seed_pair(runner)
    result = runner.invoke(main, ["dedupe", "--review"])
    assert result.exit_code == 0
    assert "pair 1" in result.output


def test_cli_dedupe_reports_an_empty_queue(db_path: Path) -> None:
    result = CliRunner().invoke(main, ["dedupe", "--list"])
    assert result.exit_code == 0
    assert "no pending near-duplicate pairs" in result.output


def test_cli_dedupe_emits_json(db_path: Path) -> None:
    runner = CliRunner()
    _seed_pair(runner)
    result = runner.invoke(main, ["dedupe", "--list", "--json"])
    payload = json.loads(result.output)
    assert [entry["queue_id"] for entry in payload] == [1]
    assert payload[0]["older"]["content"] == LEFT


def test_cli_dedupe_filters_by_workspace(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", LEFT, "-w", "a"])
    runner.invoke(main, ["add", RIGHT, "-w", "a"])
    result = runner.invoke(main, ["dedupe", "--list", "--json", "-w", "b"])
    assert json.loads(result.output) == []


def test_cli_dedupe_merge_and_repeat(db_path: Path) -> None:
    """AC20 through the CLI."""
    runner = CliRunner()
    _seed_pair(runner)
    merged = runner.invoke(main, ["dedupe", "--merge", "1", "--json"])
    assert merged.exit_code == 0
    assert json.loads(merged.output) == {
        "queue_id": 1,
        "status": "merged",
        "kept_id": 2,
        "removed_id": 1,
        "seen_count": 2,
    }

    again = runner.invoke(main, ["dedupe", "--merge", "1"])
    assert again.exit_code != 0
    assert "no pending near-duplicate pair" in again.output
    assert "Traceback" not in again.output


def test_cli_dedupe_keep_both(db_path: Path) -> None:
    runner = CliRunner()
    _seed_pair(runner)
    result = runner.invoke(main, ["dedupe", "--keep-both", "1"])
    assert result.exit_code == 0
    assert "kept_both" in result.output
    assert "nothing removed" in result.output


def test_cli_dedupe_prompts_on_a_terminal(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _seed_pair(runner)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    result = runner.invoke(main, ["dedupe", "--review"], input="m\n")
    assert result.exit_code == 0
    assert "merged" in result.output


def test_cli_dedupe_skips_on_a_terminal(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _seed_pair(runner)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    result = runner.invoke(main, ["dedupe", "--review"], input="s\n")
    assert result.exit_code == 0
    assert "still pending" in result.output

    keep = runner.invoke(main, ["dedupe", "--review"], input="k\n")
    assert "kept_both" in keep.output


def test_cli_dedupe_review_skips_pairs_a_merge_removed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merging deletes a memory, so any other pair built on it is gone by its turn."""
    runner = CliRunner()
    for text in (LEFT, RIGHT, "- Use PNPM; not npm"):
        runner.invoke(main, ["add", text, "-w", WORKSPACE])
    assert len(_read_queue(db_path)) == 3

    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    result = runner.invoke(main, ["dedupe", "--review"], input="m\nk\n")

    assert result.exit_code == 0
    assert "resolved by an earlier merge, skipping" in result.output
    assert "Traceback" not in result.output


def test_cli_dedupe_on_a_terminal_with_an_empty_queue(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    result = CliRunner().invoke(main, ["dedupe", "--review"])
    assert result.exit_code == 0
    assert "no pending near-duplicate pairs" in result.output


def test_cli_gc_dry_run_writes_nothing(db_path: Path) -> None:
    """AC21."""
    runner = CliRunner()
    _seed_pair(runner)
    runner.invoke(main, ["dedupe", "--keep-both", "1"])
    with _open(db_path) as conn:
        _age_queue_row(conn, 1, "2020-01-01 00:00:00")

    before_rows = _read_queue(db_path)
    before_mtime = db_path.stat().st_mtime_ns

    result = runner.invoke(main, ["gc", "--dry-run"])

    assert result.exit_code == 0
    assert db_path.stat().st_mtime_ns == before_mtime
    assert "would prune 1" in result.output
    assert "nothing written" in result.output
    assert _read_queue(db_path) == before_rows


def test_cli_gc_prunes_resolved_and_keeps_pending(db_path: Path) -> None:
    """AC21."""
    runner = CliRunner()
    runner.invoke(main, ["add", LEFT, "-w", "a"])
    runner.invoke(main, ["add", RIGHT, "-w", "a"])
    runner.invoke(main, ["add", LEFT, "-w", "b"])
    runner.invoke(main, ["add", RIGHT, "-w", "b"])
    runner.invoke(main, ["dedupe", "--keep-both", "1"])
    with _open(db_path) as conn:
        _age_queue_row(conn, 1, "2020-01-01 00:00:00")

    result = runner.invoke(main, ["gc", "--days", "30"])

    assert result.exit_code == 0
    assert "pruned 1 resolved queue rows" in result.output
    assert "queue depth: 1 pending" in result.output
    assert [row[0] for row in _read_queue(db_path)] == [2]


def test_cli_gc_never_reports_that_the_database_grew(db_path: Path) -> None:
    """The size line after a prune must not exceed the size before it.

    ``VACUUM`` rewrites the database through the WAL, and ``database_size_bytes`` counts
    the ``-wal`` sidecar, so without the checkpoint ``gc`` reported growth on a small
    file — a command that says it reclaimed space and then prints a bigger number.
    """
    runner = CliRunner()
    _seed_pair(runner)
    for index in range(30):
        runner.invoke(main, ["add", f"filler memory number {index} with some padding", "-w", "pad"])
    runner.invoke(main, ["dedupe", "--keep-both", "1"])
    with _open(db_path) as conn:
        _age_queue_row(conn, 1, "2020-01-01 00:00:00")

    result = runner.invoke(main, ["gc"])

    assert result.exit_code == 0
    assert "pruned 1 resolved queue rows" in result.output
    size_line = next(line for line in result.output.splitlines() if line.startswith("size:"))
    before, after = (_to_bytes(match) for match in _SIZE_LINE_RE.findall(size_line))
    assert after <= before, size_line


def _to_bytes(match: tuple[str, str]) -> float:
    amount, unit = match
    return float(amount) * _SIZE_MULTIPLIERS[unit]


def test_cli_gc_on_an_empty_database(db_path: Path) -> None:
    result = CliRunner().invoke(main, ["gc"])
    assert result.exit_code == 0
    assert "pruned 0 resolved queue rows" in result.output


# --- stats ------------------------------------------------------------------


def test_cli_stats_shows_queue_depth_and_core_tokens_on_an_empty_database(db_path: Path) -> None:
    """AC22."""
    result = CliRunner().invoke(main, ["stats"])
    assert result.exit_code == 0
    assert "queue depth: 0 pending near-duplicate pairs" in result.output
    assert "core memory: ~0 estimated tokens" in result.output


def test_stats_counts_the_pending_queue_and_core_memory(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    store.add_memory(conn, LEFT, WORKSPACE)
    store.add_memory(conn, RIGHT, WORKSPACE)
    store.add_memory(conn, "prefer small commits", WORKSPACE, "core")
    summary = store.collect_stats(conn, db_path)
    assert summary.queue_depth == 1
    assert summary.core_memory_tokens > 0
    assert summary.core_memory_dropped == 0


def test_cli_stats_warns_about_a_truncated_core_memory(db_path: Path) -> None:
    runner = CliRunner()
    for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"):
        runner.invoke(main, ["add", f"{word} " * 100, "-w", WORKSPACE, "--kind", "core"])
    result = runner.invoke(main, ["stats"])
    assert result.exit_code == 0
    assert "core rows are hidden by the 400-token cap" in result.output


def _read_queue(db_path: Path) -> list[tuple[int, str]]:
    with _open(db_path) as conn:
        return [(int(row["id"]), row["status"]) for row in _queue_rows(conn)]
