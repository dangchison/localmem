"""Tests for ``localmem audit`` — the deterministic, zero-token hygiene report.

The load-bearing property is negative: audit **writes nothing**. That is asserted
against the real file, not against a mock, in
:func:`test_audit_leaves_the_database_byte_identical`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmem import audit, core_memory, db, retriever, store
from localmem.cli import main

WORKSPACE = "proj"
LEFT = "use pnpm not npm"
RIGHT = "use pnpm, not npm!"


def _backdate(conn: sqlite3.Connection, memory_id: int, created_at: str) -> None:
    conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (created_at, memory_id))


def _age(conn: sqlite3.Connection, memory_id: int, days: int) -> None:
    conn.execute(
        "UPDATE memories SET created_at = datetime('now', ?) WHERE id = ?",
        (f"-{days} days", memory_id),
    )


@pytest.fixture
def dirty(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A database with something wrong in every section of the report."""
    # 1 — a pending near-duplicate pair.
    store.add_memory(conn, LEFT, WORKSPACE)
    store.add_memory(conn, RIGHT, WORKSPACE)
    # 2 — a row seen well past the promotion threshold.
    for _ in range(7):
        store.add_memory(conn, "always run the migrations before the test suite", WORKSPACE)
    # 4 — core memory over the 400-token cap.
    for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"):
        store.add_memory(conn, f"{word} " * 100, WORKSPACE, "core")
    # 5 — an old memory nothing has ever recalled.
    stale = store.add_memory(conn, "a note nobody ever asks about", WORKSPACE)
    _age(conn, stale.id, 90)
    return conn


# --------------------------------------------------------------------------- read-only


def test_audit_leaves_the_database_byte_identical(dirty: sqlite3.Connection, db_path: Path) -> None:
    """The whole promise of the command: it reports, it never repairs."""
    dirty.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before_bytes = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    report = audit.run(dirty, db_path)

    assert report.total_memories > 0
    assert db_path.read_bytes() == before_bytes
    assert db_path.stat().st_mtime_ns == before_mtime


def test_the_audit_command_leaves_the_database_byte_identical(db_path: Path) -> None:
    """The same guarantee through the CLI, which opens its own connection."""
    runner = CliRunner()
    runner.invoke(main, ["add", LEFT, "-w", WORKSPACE])
    runner.invoke(main, ["gc"])  # checkpoints and vacuums, so the file is settled
    before_bytes = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    result = runner.invoke(main, ["audit"])

    assert result.exit_code == 0
    assert db_path.read_bytes() == before_bytes
    assert db_path.stat().st_mtime_ns == before_mtime


# ------------------------------------------------------------------ section 1: queue


def test_the_queue_section_counts_pending_pairs(dirty: sqlite3.Connection, db_path: Path) -> None:
    report = audit.run(dirty, db_path)
    assert report.queue.pending == 1
    assert report.queue.per_workspace == ((WORKSPACE, 1),)
    assert report.queue.oldest_queued_at is not None
    assert report.queue.oldest_age_days is not None
    assert len(report.queue.samples) == 1


def test_the_queue_section_is_workspace_scoped(dirty: sqlite3.Connection, db_path: Path) -> None:
    assert audit.run(dirty, db_path, "somewhere-else").queue.pending == 0


def test_the_queue_section_names_the_commands_that_drain_it(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", LEFT, "-w", WORKSPACE])
    runner.invoke(main, ["add", RIGHT, "-w", WORKSPACE])
    output = runner.invoke(main, ["audit"]).output
    assert "pending pairs: 1" in output
    for command in audit.QUEUE_COMMANDS:
        assert command in output


# ------------------------------------------------------------- section 2: promotion


def test_promotion_candidates_need_the_seen_count_threshold(
    dirty: sqlite3.Connection, db_path: Path
) -> None:
    candidates = audit.run(dirty, db_path).promotion_candidates
    assert [candidate.content for candidate in candidates] == [
        "always run the migrations before the test suite"
    ]
    assert candidates[0].seen_count == 7
    assert candidates[0].kind != core_memory.CORE_KIND


def test_promotion_candidates_never_include_core_rows(
    dirty: sqlite3.Connection, db_path: Path
) -> None:
    dirty.execute("UPDATE memories SET seen_count = 9 WHERE kind = 'core'")
    candidates = audit.run(dirty, db_path).promotion_candidates
    assert all(candidate.kind != core_memory.CORE_KIND for candidate in candidates)


def test_promotion_candidates_rank_recalls_above_repetition(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """Schema v2 makes this ordering possible: what is used beats what is written twice."""
    for _ in range(9):
        store.add_memory(conn, "written over and over again", WORKSPACE)
    for _ in range(5):
        store.add_memory(conn, "asked for again and again", WORKSPACE)
    retriever.retrieve(conn, "asked", WORKSPACE)

    candidates = audit.run(conn, db_path).promotion_candidates

    assert [candidate.content for candidate in candidates] == [
        "asked for again and again",
        "written over and over again",
    ]
    assert (candidates[0].recalled_count, candidates[0].seen_count) == (1, 5)


def test_the_promotion_note_refuses_to_imply_a_command_that_does_not_exist(
    dirty: sqlite3.Connection, db_path: Path
) -> None:
    output = CliRunner().invoke(main, ["audit"]).output
    assert audit.PROMOTION_NOTE in output
    assert "does not promote" in audit.PROMOTION_NOTE


# ---------------------------------------------------------- section 3: distribution


def test_the_distribution_section_breaks_rows_down_per_workspace(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    store.add_memory(conn, "one", "a")
    store.add_memory(conn, "two", "a", "core")
    store.add_memory(conn, "three", "b")

    summaries = {summary.workspace: summary for summary in audit.run(conn, db_path).workspaces}

    assert set(summaries) == {"a", "b"}
    assert summaries["a"].total == 2
    assert dict(summaries["a"].per_kind) == {"note": 1, "core": 1}
    assert summaries["a"].oldest_created_at <= summaries["a"].newest_created_at


def test_the_distribution_section_reuses_collect_stats(
    dirty: sqlite3.Connection, db_path: Path
) -> None:
    report = audit.run(dirty, db_path)
    summary = store.collect_stats(dirty, db_path)
    assert report.total_memories == summary.total
    assert report.total_entities == summary.total_entities
    assert report.total_entity_links == summary.total_entity_links
    assert report.db_size_bytes == summary.db_size_bytes


# ------------------------------------------------------------ section 4: core health


def test_core_health_reports_rows_hidden_by_the_cap(
    dirty: sqlite3.Connection, db_path: Path
) -> None:
    health = {entry.workspace: entry for entry in audit.run(dirty, db_path).core_health}
    assert set(health) == {WORKSPACE}
    assert health[WORKSPACE].cap == core_memory.CORE_MEMORY_TOKEN_CAP
    assert health[WORKSPACE].tokens <= core_memory.CORE_MEMORY_TOKEN_CAP
    assert health[WORKSPACE].dropped == 4


def test_core_health_of_a_named_workspace_includes_the_shared_tier(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    store.add_memory(conn, "prefer pnpm everywhere", core_memory.GLOBAL_WORKSPACE, "core")
    entry = audit.run(conn, db_path, WORKSPACE).core_health[0]
    assert entry.workspace == WORKSPACE
    assert entry.tokens > 0


# ------------------------------------------------------------- section 5: dead rows


def test_dead_memories_are_old_and_never_recalled(dirty: sqlite3.Connection, db_path: Path) -> None:
    report = audit.run(dirty, db_path)
    assert report.dead_total == 1
    assert [dead.content for dead in report.dead] == ["a note nobody ever asks about"]
    assert report.dead[0].age_days is not None
    assert report.dead[0].age_days > audit.DEAD_MEMORY_AGE_DAYS


def test_a_recalled_memory_is_not_dead(dirty: sqlite3.Connection, db_path: Path) -> None:
    retriever.retrieve(dirty, "nobody asks", WORKSPACE)
    assert audit.run(dirty, db_path).dead_total == 0


def test_a_young_memory_is_not_dead(conn: sqlite3.Connection, db_path: Path) -> None:
    store.add_memory(conn, "stored just now and never recalled", WORKSPACE)
    assert audit.run(conn, db_path).dead_total == 0


def test_a_malformed_timestamp_reports_an_unknown_age_rather_than_guessing(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    added = store.add_memory(conn, "a note with a hand-edited timestamp", WORKSPACE)
    _backdate(conn, added.id, "0001-01-01 not a timestamp")
    dead = audit.run(conn, db_path).dead
    assert [entry.age_days for entry in dead] == [None]
    assert "age unknown" in CliRunner().invoke(main, ["audit"]).output


# ------------------------------------------------------------------- empty and JSON


def test_an_empty_database_produces_an_empty_report(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    report = audit.run(conn, db_path)
    assert report.total_memories == 0
    assert report.queue.pending == 0
    assert report.promotion_candidates == ()
    assert report.workspaces == ()
    assert report.core_health == ()
    assert (report.dead, report.dead_total) == ((), 0)


def test_the_audit_command_on_an_empty_database_exits_zero(db_path: Path) -> None:
    result = CliRunner().invoke(main, ["audit"])
    assert result.exit_code == 0
    assert "no pending pairs" in result.output
    assert "no core memories yet" in result.output


def test_the_audit_command_always_exits_zero_even_when_everything_is_wrong(
    db_path: Path,
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", LEFT, "-w", WORKSPACE])
    runner.invoke(main, ["add", RIGHT, "-w", WORKSPACE])
    result = runner.invoke(main, ["audit"])
    assert result.exit_code == 0
    for heading in ("1. near-duplicate queue", "2. core memory", "3. distribution"):
        assert heading in result.output


def test_the_json_mode_parses_and_carries_the_same_numbers(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", LEFT, "-w", WORKSPACE])
    runner.invoke(main, ["add", RIGHT, "-w", WORKSPACE])

    body = json.loads(runner.invoke(main, ["audit", "--json"]).output)

    assert body["workspace"] is None
    assert body["queue"]["pending"] == 1
    assert body["queue"]["commands"] == list(audit.QUEUE_COMMANDS)
    assert body["database"]["memories"] == 2
    assert body["dead_memories"]["age_days"] == audit.DEAD_MEMORY_AGE_DAYS
    assert body["promotion_candidates"]["note"] == audit.PROMOTION_NOTE


def test_the_json_mode_reports_the_requested_workspace(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", LEFT, "-w", WORKSPACE])
    body = json.loads(runner.invoke(main, ["audit", "-w", WORKSPACE, "--json"]).output)
    assert body["workspace"] == WORKSPACE
    assert [entry["workspace"] for entry in body["workspaces"]] == [WORKSPACE]


def test_audit_rejects_a_blank_workspace(conn: sqlite3.Connection, db_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        audit.run(conn, db_path, "   ")


def test_the_audit_command_reports_a_blank_workspace_cleanly(db_path: Path) -> None:
    result = CliRunner().invoke(main, ["audit", "-w", "   "])
    assert result.exit_code != 0
    assert "non-empty" in result.output


def test_the_schema_version_the_report_depends_on(conn: sqlite3.Connection) -> None:
    """Sections 2 and 5 read ``recalled_count``, which only exists from version 2."""
    assert db.schema_version(conn) >= 2
