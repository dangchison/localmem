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

from localmem import audit, core_memory, db, dedup, retriever, store
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
    # 6 — a diagnosis a later memory corrected.
    wrong = store.add_memory(conn, "the leak is in the image resizer", WORKSPACE)
    store.add_memory(conn, "the connection pool was exhausted", WORKSPACE, supersedes=[wrong.id])
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


def test_the_promotion_note_names_the_command_that_now_exists() -> None:
    """v0.4.0 makes good on the v0.2 note; the command it names has to be real.

    Asserted against the live command registry rather than the string, so a rename
    breaks here instead of leaving the report pointing at nothing.
    """
    assert "localmem promote ID" in audit.PROMOTION_NOTE
    assert "promote" in main.commands
    assert "wait for the promote tooling" not in audit.PROMOTION_NOTE


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


# ------------------------------------------------------------ section 6: superseded


def test_the_superseded_section_names_what_replaced_each_row(
    dirty: sqlite3.Connection, db_path: Path
) -> None:
    """v0.4.0 C4: a count is the minimum; the replacement is what makes it actionable."""
    report = audit.run(dirty, db_path)

    assert report.superseded_total == 1
    (row,) = report.superseded
    assert row.content == "the leak is in the image resizer"
    assert row.replacement_content == "the connection pool was exhausted"
    assert (row.workspace, row.replacement_workspace) == (WORKSPACE, WORKSPACE)


def test_the_superseded_section_is_scoped_to_the_retracted_rows_workspace(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    wrong = store.add_memory(conn, "the leak is in the image resizer", WORKSPACE)
    store.add_memory(
        conn, "the pool was exhausted", core_memory.GLOBAL_WORKSPACE, supersedes=[wrong.id]
    )

    assert audit.run(conn, db_path, WORKSPACE).superseded_total == 1
    assert audit.run(conn, db_path, core_memory.GLOBAL_WORKSPACE).superseded_total == 0


def test_the_superseded_section_reports_a_clean_database(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    store.add_memory(conn, LEFT, WORKSPACE)
    report = audit.run(conn, db_path)
    assert (report.superseded_total, report.superseded) == (0, ())


def test_the_superseded_section_renders_with_its_note(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "the leak is in the image resizer", "-w", WORKSPACE])
    runner.invoke(main, ["add", "the pool was exhausted", "-w", WORKSPACE, "--supersedes", "1"])

    output = runner.invoke(main, ["audit"]).output

    assert "6. superseded memories" in output
    assert "corrected by a later memory: 1" in output
    assert "superseded by id=2" in output
    assert audit.SUPERSEDED_NOTE in output


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
    assert body["superseded"] == {"total": 0, "note": audit.SUPERSEDED_NOTE, "rows": []}


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


# --------------------------------------------------------- section 7: lesson health

#: The milestone-D fixture pair: a real trace and a later restatement of it. Their
#: overlap is 0.314 — over the capture gate, under tier 2's.
TRACE = (
    "Tests were flaky under xdist because two fixtures shared a temp directory. Gave each "
    "worker its own tmp_path and the intermittent failures stopped."
)
TRACE_RESTATED = (
    "The flaky tests came back. Same cause as before — fixtures sharing one temp dir "
    "across xdist workers. Each worker needs its own tmp_path."
)


@pytest.fixture
def learning(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A database with something in every part of section 7."""
    store.add_memory(conn, "413 on upload is the proxy, not the app", WORKSPACE, "lesson")
    store.add_memory(conn, "never trust a green suite you did not watch run", "global", "lesson")
    forgotten = store.add_memory(
        conn, "the ALTER TABLE needs a default or it locks the table", WORKSPACE, "lesson"
    )
    _age(conn, forgotten.id, 90)
    retracted = store.add_memory(conn, "the leak is in the resizer", WORKSPACE, "lesson")
    store.add_memory(
        conn, "it was the connection pool all along", WORKSPACE, "lesson", supersedes=[retracted.id]
    )
    for _ in range(4):
        store.add_memory(conn, "remember to bump the lockfile after a dependency change", WORKSPACE)
    old_trace = store.add_memory(conn, TRACE, WORKSPACE, "trace")
    _age(conn, old_trace.id, 90)
    store.add_memory(conn, TRACE_RESTATED, WORKSPACE, "trace")
    return conn


def test_active_lessons_are_counted_per_workspace(
    learning: sqlite3.Connection, db_path: Path
) -> None:
    lessons = audit.run(learning, db_path).lessons
    # Five lessons stored, one of them retracted.
    assert lessons.active_total == 4
    assert dict(lessons.active_per_workspace) == {WORKSPACE: 3, "global": 1}
    assert lessons.superseded_lessons == 1


def test_the_superseded_count_does_not_duplicate_section_six(
    learning: sqlite3.Connection, db_path: Path
) -> None:
    """Section 6 lists retracted rows; section 7 only tallies the lessons among them."""
    report = audit.run(learning, db_path)
    assert report.superseded_total == 1
    assert report.lessons.superseded_lessons == 1
    output = CliRunner().invoke(main, ["audit"]).output
    assert output.count("the leak is in the resizer") == 1


def test_stale_lessons_are_old_and_never_recalled(
    learning: sqlite3.Connection, db_path: Path
) -> None:
    lessons = audit.run(learning, db_path).lessons
    assert lessons.stale_total == 1
    assert lessons.stale[0].content.startswith("the ALTER TABLE")
    assert lessons.stale_age_days == audit.DEAD_MEMORY_AGE_DAYS


def test_a_retracted_lesson_is_not_reported_as_stale(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """A lesson nobody recalls because it was corrected is not a neglected lesson."""
    wrong = store.add_memory(conn, "the leak is in the image resizer", WORKSPACE, "lesson")
    store.add_memory(conn, "it was the pool", WORKSPACE, "lesson", supersedes=[wrong.id])
    _age(conn, wrong.id, 90)

    assert audit.run(conn, db_path).lessons.stale_total == 0


def test_unread_rows_are_stored_repeatedly_and_never_recalled(
    learning: sqlite3.Connection, db_path: Path
) -> None:
    lessons = audit.run(learning, db_path).lessons
    assert lessons.unread_total == 1
    assert lessons.unread[0].seen_count == 4
    assert lessons.unread[0].recalled_count == 0
    assert lessons.unread[0].content.startswith("remember to bump the lockfile")


def test_prunable_traces_are_counted_and_nothing_is_deleted(
    learning: sqlite3.Connection, db_path: Path
) -> None:
    before = learning.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]

    lessons = audit.run(learning, db_path).lessons

    assert lessons.prunable.eligible == 1
    assert lessons.prunable.days == audit.DEAD_MEMORY_AGE_DAYS
    assert learning.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == before


def test_the_prunable_count_agrees_with_gc(learning: sqlite3.Connection, db_path: Path) -> None:
    """audit and `gc --prune-traces --dry-run` must never report different numbers."""
    reported = audit.run(learning, db_path).lessons.prunable
    direct = store.count_prunable_traces(learning, audit.DEAD_MEMORY_AGE_DAYS)
    assert (reported.eligible, reported.protected) == (direct.eligible, direct.protected)


def test_the_similarity_distribution_uses_the_gate_s_own_code_path(
    learning: sqlite3.Connection, db_path: Path
) -> None:
    """The report must measure what the gate decides on, or it cannot inform it."""
    similarity = audit.run(learning, db_path).lessons.similarity

    assert similarity.total == 2
    assert similarity.scanned == 2
    assert similarity.threshold == dedup.CAPTURE_JACCARD_THRESHOLD
    # The restatement pair scores 0.314, so both traces see each other over the gate.
    assert similarity.at_or_above_threshold == 2
    assert similarity.with_neighbour == 2
    assert sum(bucket.count for bucket in similarity.buckets) == similarity.scanned


def test_the_histogram_puts_the_gate_on_a_bucket_boundary() -> None:
    """The whole point of the buckets: everything at or above the cut is one group."""
    assert dedup.CAPTURE_JACCARD_THRESHOLD in audit.SIMILARITY_BUCKET_EDGES


def test_similarity_is_empty_without_traces(conn: sqlite3.Connection, db_path: Path) -> None:
    store.add_memory(conn, "just a note", WORKSPACE)
    similarity = audit.run(conn, db_path).lessons.similarity
    assert (similarity.total, similarity.scanned, similarity.median) == (0, 0, None)


def test_lesson_health_says_so_when_tracking_is_off(
    learning: sqlite3.Connection, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reporting a recall-derived zero as fact would justify deleting a live store."""
    monkeypatch.setenv(retriever.NO_TRACKING_ENV_VAR, "1")

    assert audit.run(learning, db_path).lessons.tracking_disabled is True

    output = CliRunner().invoke(main, ["audit"]).output
    assert audit.TRACKING_DISABLED_NOTE in output


def test_lesson_health_is_quiet_about_tracking_when_it_is_on(
    learning: sqlite3.Connection, db_path: Path
) -> None:
    assert audit.run(learning, db_path).lessons.tracking_disabled is False
    assert audit.TRACKING_DISABLED_NOTE not in CliRunner().invoke(main, ["audit"]).output


def test_the_lesson_section_renders(learning: sqlite3.Connection, db_path: Path) -> None:
    output = CliRunner().invoke(main, ["audit"]).output
    assert "7. lesson health" in output
    assert "active lessons: 4" in output
    assert audit.PRUNE_NOTE in output
    assert audit.SIMILARITY_NOTE in output
    assert "<- gate" in output


def test_lesson_health_is_workspace_scoped(learning: sqlite3.Connection, db_path: Path) -> None:
    lessons = audit.run(learning, db_path, WORKSPACE).lessons
    assert dict(lessons.active_per_workspace) == {WORKSPACE: 3}
    assert lessons.active_total == 3


def test_every_number_in_section_seven_is_scoped_including_the_trace_counts(
    learning: sqlite3.Connection, db_path: Path
) -> None:
    """Both trace figures must answer for the same workspace, or they contradict each other.

    Every trace in the fixture lives in ``WORKSPACE``; ``global`` holds one lesson and no
    traces at all. ``prunable`` shipped unscoped once, so ``global`` claimed a prunable
    trace on the same screen where ``similarity`` correctly reported having none to
    measure. A fixture with traces in a single workspace cannot catch that, which is why
    this asserts against the empty side too.
    """
    here = audit.run(learning, db_path, WORKSPACE).lessons
    elsewhere = audit.run(learning, db_path, "global").lessons

    assert (here.prunable.eligible, here.similarity.total) == (1, 2)
    assert (elsewhere.prunable.eligible, elsewhere.similarity.total) == (0, 0)


def test_lesson_health_reaches_the_json_payload(learning: sqlite3.Connection) -> None:
    body = json.loads(CliRunner().invoke(main, ["audit", "--json"]).output)["lesson_health"]

    assert body["active"]["total"] == 4
    assert body["active"]["superseded"] == 1
    assert body["stale"]["total"] == 1
    assert body["unread"]["total"] == 1
    assert body["prunable_traces"]["eligible"] == 1
    assert body["prunable_traces"]["note"] == audit.PRUNE_NOTE
    assert body["trace_similarity"]["threshold"] == dedup.CAPTURE_JACCARD_THRESHOLD
    assert body["trace_similarity"]["note"] == audit.SIMILARITY_NOTE
    assert body["tracking_disabled"] is False
    assert body["tracking_note"] is None


def test_the_json_payload_carries_the_tracking_caveat(
    learning: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(retriever.NO_TRACKING_ENV_VAR, "1")
    body = json.loads(CliRunner().invoke(main, ["audit", "--json"]).output)["lesson_health"]
    assert body["tracking_disabled"] is True
    assert body["tracking_note"] == audit.TRACKING_DISABLED_NOTE


def test_audit_still_writes_nothing_with_the_new_section(
    learning: sqlite3.Connection, db_path: Path
) -> None:
    """Section 7 runs FTS5 queries per trace; none of them may write."""
    learning.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = db_path.read_bytes()

    audit.run(learning, db_path)

    assert db_path.read_bytes() == before


def test_prunable_traces_are_scoped_to_the_requested_workspace(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """**The regression detector for the section contradicting itself.**

    A fixture whose traces all live in one workspace can never catch this: every number
    agrees by accident. The failing shape is a workspace that holds no traces at all,
    where an unscoped count reported "1 eligible" directly above "no traces stored yet".
    """
    store.add_memory(conn, "a global lesson that applies everywhere", "global", "lesson")
    trace = store.add_memory(conn, TRACE, "repo-a", "trace")
    _age(conn, trace.id, 90)

    empty = audit.run(conn, db_path, "global").lessons
    holding = audit.run(conn, db_path, "repo-a").lessons

    assert empty.prunable.eligible == 0, "counted a trace from another workspace"
    assert empty.prunable.workspace == "global"
    assert holding.prunable.eligible == 1
    # The two numbers in this section must tell the same story about the same workspace.
    assert empty.similarity.total == 0
    assert holding.similarity.total == 1


def test_the_unscoped_report_still_counts_every_workspace(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    """`audit` with no `-w` is the whole database, which is what `gc` actually prunes."""
    for workspace in ("repo-a", "repo-b"):
        trace = store.add_memory(conn, f"{TRACE} in {workspace}", workspace, "trace")
        _age(conn, trace.id, 90)

    lessons = audit.run(conn, db_path).lessons

    assert lessons.prunable.eligible == 2
    assert lessons.prunable.workspace is None


def test_the_prunable_line_names_its_scope(conn: sqlite3.Connection, db_path: Path) -> None:
    """The label is the other half of the fix: a scoped count must say it is scoped."""
    trace = store.add_memory(conn, TRACE, "repo-a", "trace")
    _age(conn, trace.id, 90)
    runner = CliRunner()

    assert "across every workspace" in runner.invoke(main, ["audit"]).output
    scoped = runner.invoke(main, ["audit", "-w", "repo-a"]).output
    assert "in workspace 'repo-a'" in scoped
    # And the caveat that gc itself is not scoped.
    assert "has no `-w` flag and acts on EVERY workspace" in scoped


def test_gc_reports_the_whole_database(conn: sqlite3.Connection) -> None:
    """`gc --prune-traces` has no `-w`; its own preview must count every workspace."""
    for workspace in ("repo-a", "repo-b"):
        trace = store.add_memory(conn, f"{TRACE} in {workspace}", workspace, "trace")
        _age(conn, trace.id, 90)

    result = CliRunner().invoke(main, ["gc", "--prune-traces", "30", "--dry-run"])

    assert "would delete 2 never-recalled traces" in result.output
    assert store.count_prunable_traces(conn, 30).workspace is None
