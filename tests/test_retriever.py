"""Tests for the M3 retrieval pipeline: tokens, core memory and the dual-view retriever.

Every clock-dependent assertion injects ``now`` instead of patching the standard library,
so nothing here depends on the machine's date.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmem import core_memory, retriever, store, tokens
from localmem.cli import main

WORKSPACE = "proj"
VIETNAMESE = "Dùng pnpm thay vì npm cho dự án này"
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
THIRTY_DAYS_BEFORE_NOW = "2026-07-06 12:00:00"
SIXTY_DAYS_BEFORE_NOW = "2026-06-06 12:00:00"
AT_NOW = "2026-08-05 12:00:00"


def _backdate(conn: sqlite3.Connection, memory_id: int, created_at: str) -> None:
    """Rewrite one row's created_at; the FTS index is keyed on content, so it is intact."""
    conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (created_at, memory_id))


# --- tokens -----------------------------------------------------------------


def test_estimate_tokens_empty_string_is_zero() -> None:
    assert tokens.estimate_tokens("") == 0


def test_estimate_tokens_uses_four_chars_for_latin_text() -> None:
    assert tokens.estimate_tokens("a" * 40) == 10


def test_estimate_tokens_uses_dense_divisor_for_vietnamese() -> None:
    # Well past the 0.15 non-ASCII cutoff, so the denser 2.5-chars-per-token rule applies.
    text = "đường dẫn tệp không đúng"
    assert tokens.estimate_tokens(text) == math.ceil(len(text) / tokens.CHARS_PER_TOKEN_DENSE)


def test_estimate_tokens_ignores_a_few_accents() -> None:
    # 2 non-ASCII characters out of 44 is 0.045 — below the cutoff, so English pricing.
    text = "the naïve café rule stays on the latin path"
    assert tokens.estimate_tokens(text) == 11


# --- core memory ------------------------------------------------------------


def test_core_memory_is_empty_without_core_rows(conn: sqlite3.Connection) -> None:
    built = core_memory.build_core_memory(conn, WORKSPACE)
    assert (built.text, built.tokens, built.dropped) == ("", 0, 0)


def test_core_memory_joins_rows_oldest_first(conn: sqlite3.Connection) -> None:
    first = store.add_memory(conn, "prefer pnpm", WORKSPACE, "core")
    second = store.add_memory(conn, "deploy on fridays never", WORKSPACE, "core")
    _backdate(conn, first.id, "2026-01-01 00:00:00")
    _backdate(conn, second.id, "2026-02-01 00:00:00")
    built = core_memory.build_core_memory(conn, WORKSPACE)
    assert built.text == "prefer pnpm\ndeploy on fridays never"
    assert built.dropped == 0


def test_core_memory_is_workspace_scoped(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "only for a", "a", "core")
    store.add_memory(conn, "only for b", "b", "core")
    assert core_memory.build_core_memory(conn, "a").text == "only for a"
    assert set(core_memory.build_core_memory(conn, None).text.splitlines()) == {
        "only for a",
        "only for b",
    }


def test_core_memory_drops_whole_oldest_rows_over_the_cap(conn: sqlite3.Connection) -> None:
    """AC15."""
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    for index, word in enumerate(words, start=1):
        added = store.add_memory(conn, f"{word} " * 100, WORKSPACE, "core")
        _backdate(conn, added.id, f"2026-01-{index:02d} 00:00:00")
    built = core_memory.build_core_memory(conn, WORKSPACE)

    assert built.tokens <= core_memory.CORE_MEMORY_TOKEN_CAP
    assert built.dropped == 4
    # Whole rows only: what survives is the two newest, each still complete.
    lines = built.text.splitlines()
    assert len(lines) == 2
    assert lines[0].split()[0] == "echo"
    assert all(len(line.split()) == 100 for line in lines)


def test_core_memory_drops_a_single_row_larger_than_the_cap(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "x " * 5000, WORKSPACE, "core")
    built = core_memory.build_core_memory(conn, WORKSPACE)
    assert (built.text, built.dropped) == ("", 1)


def test_core_memory_survives_an_unusable_database() -> None:
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    try:
        assert core_memory.build_core_memory(bare, WORKSPACE).text == ""
        assert core_memory.core_memory_totals(bare) == (0, 0)
    finally:
        bare.close()


# --- retrieval basics -------------------------------------------------------


def test_retrieve_on_empty_database(conn: sqlite3.Connection) -> None:
    """AC5."""
    outcome = retriever.retrieve(conn, "anything", WORKSPACE, now=NOW)
    assert outcome.results == ()
    assert outcome.core_memory == ""
    assert outcome.message == retriever.EMPTY_MESSAGE


def test_retrieve_returns_provenance_and_core_memory(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "use pnpm not npm", WORKSPACE, "note", "codex")
    store.add_memory(conn, "prefer small commits", WORKSPACE, "core")
    outcome = retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    hit = outcome.results[0]
    assert (hit.content, hit.kind, hit.source, hit.workspace) == (
        "use pnpm not npm",
        "note",
        "codex",
        WORKSPACE,
    )
    assert outcome.core_memory == "prefer small commits"
    assert outcome.core_memory_tokens == tokens.estimate_tokens("prefer small commits")
    assert outcome.message is None


def test_retrieve_is_workspace_scoped(conn: sqlite3.Connection) -> None:
    """AC6."""
    store.add_memory(conn, "use pnpm not npm", "a")
    store.add_memory(conn, "use pnpm not npm", "b")
    scoped = retriever.retrieve(conn, "pnpm", "a", now=NOW)
    assert [hit.workspace for hit in scoped.results] == ["a"]
    assert len(retriever.retrieve(conn, "pnpm", None, now=NOW).results) == 2


def test_retrieve_matches_vietnamese_without_diacritics(conn: sqlite3.Connection) -> None:
    """AC7."""
    store.add_memory(conn, VIETNAMESE, WORKSPACE)
    outcome = retriever.retrieve(conn, "dung pnpm", WORKSPACE, now=NOW)
    assert [hit.content for hit in outcome.results] == [VIETNAMESE]


def test_retrieve_finds_a_row_the_lexical_view_missed(conn: sqlite3.Connection) -> None:
    """AC8: the §11 entity-only case.

    The query carries one token no memory contains, so the conjunctive FTS5 MATCH
    returns nothing at all. The row is recalled purely because the query and the memory
    share the ``configloader`` entity.
    """
    store.add_memory(conn, "ConfigLoader retries three times on timeout", WORKSPACE)
    store.add_memory(conn, "totally different subject about the weather", WORKSPACE)
    query = "ConfigLoader zzqqxx"

    assert retriever.query_entities(query) == ["configloader"]
    lexical_only = store.search_memories(conn, query, WORKSPACE)
    assert lexical_only == []

    outcome = retriever.retrieve(conn, query, WORKSPACE, now=NOW)
    assert len(outcome.results) == 1
    hit = outcome.results[0]
    assert hit.content.startswith("ConfigLoader")
    assert hit.lexical_score is None
    assert hit.relational_score == 1.0


def test_retrieve_with_no_searchable_tokens_returns_nothing(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "npm is broken", WORKSPACE)
    assert retriever.retrieve(conn, "*** ( ) ***", WORKSPACE, now=NOW).results == ()


@pytest.mark.parametrize("k", [1, 20])
def test_retrieve_accepts_k_boundaries(conn: sqlite3.Connection, k: int) -> None:
    """AC9."""
    for index in range(3):
        store.add_memory(conn, f"pnpm note number {index}", WORKSPACE)
    assert len(retriever.retrieve(conn, "pnpm", WORKSPACE, k, now=NOW).results) <= k


@pytest.mark.parametrize("k", [0, 21, -1])
def test_retrieve_rejects_k_out_of_range(conn: sqlite3.Connection, k: int) -> None:
    """AC9."""
    with pytest.raises(ValueError, match="k must be between"):
        retriever.retrieve(conn, "pnpm", WORKSPACE, k, now=NOW)


def test_retrieve_rejects_a_blank_workspace(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        retriever.retrieve(conn, "pnpm", "   ", now=NOW)


def test_query_entities_collapses_duplicate_normal_forms() -> None:
    # QUOTED_STRING does not mask its interior, so 'ConfigLoader' is reported twice under
    # two classes and must fold to a single lookup key.
    assert retriever.query_entities('use "ConfigLoader" now') == ["configloader"]


# --- fuse -------------------------------------------------------------------


def test_entity_hit_flips_the_fusion_weights(conn: sqlite3.Connection) -> None:
    """AC10: the flip reverses the ranking this fixture would otherwise produce."""
    relational_favourite = store.add_memory(
        conn,
        "config_loader config_loader API API and lots of extra padding words to lengthen it",
        WORKSPACE,
    )
    lexical_favourite = store.add_memory(conn, "config_loader API API", WORKSPACE)

    outcome = retriever.retrieve(conn, "API config_loader", WORKSPACE, now=NOW)
    ranked = {hit.id: hit for hit in outcome.results}
    assert [hit.id for hit in outcome.results] == [relational_favourite.id, lexical_favourite.id]

    # Both views scored both rows, so the ordering is decided by the weights alone.
    counterfactual = {
        hit.id: retriever.LEXICAL_WEIGHT * (hit.lexical_score or 0.0)
        + retriever.RELATIONAL_WEIGHT * (hit.relational_score or 0.0)
        for hit in outcome.results
    }
    assert counterfactual[relational_favourite.id] < counterfactual[lexical_favourite.id]
    assert ranked[relational_favourite.id].relational_score == 1.0
    assert ranked[lexical_favourite.id].lexical_score == 1.0


def test_a_lone_candidate_normalizes_to_one(conn: sqlite3.Connection) -> None:
    """AC11."""
    store.add_memory(conn, "only one row here", WORKSPACE)
    hit = retriever.retrieve(conn, "only", WORKSPACE, now=NOW).results[0]
    assert hit.lexical_score == 1.0
    assert hit.relational_score is None


def test_recency_separates_two_otherwise_equal_rows(conn: sqlite3.Connection) -> None:
    """AC12."""
    older = store.add_memory(conn, "alpha beta gamma", WORKSPACE)
    newer = store.add_memory(conn, "gamma beta alpha", WORKSPACE)
    _backdate(conn, older.id, "2026-06-06 12:00:00")  # 60 days before NOW
    _backdate(conn, newer.id, "2026-08-05 12:00:00")

    outcome = retriever.retrieve(conn, "alpha beta", WORKSPACE, now=NOW)
    assert [hit.id for hit in outcome.results] == [newer.id, older.id]
    # The two rows are anagrams, so bm25 — and therefore both normalized scores — match;
    # the whole gap is the decay term.
    assert outcome.results[0].lexical_score == outcome.results[1].lexical_score == 1.0
    expected = retriever.RECENCY_WEIGHT * 2.0 ** (-60 / retriever.RECENCY_HALF_LIFE_DAYS)
    assert retriever.recency_boost("2026-06-06 12:00:00", NOW) == pytest.approx(expected, abs=1e-9)
    assert outcome.results[0].score - outcome.results[1].score == pytest.approx(
        retriever.RECENCY_WEIGHT - expected, abs=1e-9
    )


def test_malformed_created_at_does_not_break_retrieval(conn: sqlite3.Connection) -> None:
    """AC23."""
    added = store.add_memory(conn, "use pnpm not npm", WORKSPACE)
    _backdate(conn, added.id, "yesterday-ish")
    outcome = retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    assert [hit.id for hit in outcome.results] == [added.id]
    assert retriever.recency_boost("yesterday-ish", NOW) == 0.0


def test_seen_count_boost_is_a_natural_log() -> None:
    """AC13."""
    assert retriever.seen_count_boost(1) == 0.0
    assert retriever.seen_count_boost(4) == pytest.approx(0.02 * math.log(4))
    # A corrupt count must not raise.
    assert retriever.seen_count_boost(0) == 0.0


def test_seen_count_boost_lifts_a_repeated_memory(conn: sqlite3.Connection) -> None:
    """AC13, wired into the pipeline."""
    quiet = store.add_memory(conn, "alpha beta gamma", WORKSPACE)
    repeated = store.add_memory(conn, "gamma beta alpha", WORKSPACE)
    for _ in range(3):
        store.add_memory(conn, "gamma beta alpha", WORKSPACE)
    stamp = "2026-08-05 12:00:00"
    _backdate(conn, quiet.id, stamp)
    _backdate(conn, repeated.id, stamp)

    outcome = retriever.retrieve(conn, "alpha beta", WORKSPACE, now=NOW)
    assert [hit.id for hit in outcome.results] == [repeated.id, quiet.id]
    assert outcome.results[0].score - outcome.results[1].score == pytest.approx(
        retriever.seen_count_boost(4), abs=1e-9
    )


# --- recency cue ------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "what did we decide recently",
        "the latest decision",
        "newest note",
        "what happened last week",
        "what happened last month",
        "notes from yesterday",
        "notes from today",
        "recent changes",
        "ghi chú hôm qua",
        "ghi chú hôm nay",
        "quyết định tuần trước",
        "quyết định tháng trước",
        "gần đây có gì",
        "bản mới nhất",
    ],
)
def test_recency_cues_are_detected(query: str) -> None:
    assert retriever.has_recency_cue(query) is True


@pytest.mark.parametrize(
    "query",
    ["use pnpm not npm", "todayish plans", "recentish", "what did we decide"],
)
def test_non_cue_queries_are_not_detected(query: str) -> None:
    # Matching is whole-word, so `todayish` is not `today`.
    assert retriever.has_recency_cue(query) is False


def test_vietnamese_cues_match_with_and_without_diacritics() -> None:
    """AC-cue-c."""
    assert retriever.has_recency_cue("quyết định tuần trước") is True
    assert retriever.has_recency_cue("quyet dinh tuan truoc") is True
    assert retriever.has_recency_cue("ghi chú hôm qua") is True
    assert retriever.has_recency_cue("ghi chu hom qua") is True


def test_cue_detection_is_case_insensitive() -> None:
    assert retriever.has_recency_cue("The LATEST Decision") is True


def test_a_cue_containing_d_stroke_still_needs_its_d_stroke() -> None:
    """`đ` has no canonical decomposition, exactly as with FTS5's remove_diacritics 2."""
    assert retriever.has_recency_cue("gần đây") is True
    assert retriever.has_recency_cue("gan đay") is True
    assert retriever.has_recency_cue("gan day") is False


def test_recency_boost_weight_defaults_to_the_uncued_value() -> None:
    """AC-cue-a."""
    decay = 2.0 ** (-30 / retriever.RECENCY_HALF_LIFE_DAYS)
    assert retriever.recency_boost(THIRTY_DAYS_BEFORE_NOW, NOW) == pytest.approx(
        retriever.RECENCY_WEIGHT * decay, abs=1e-9
    )
    assert retriever.recency_boost(
        THIRTY_DAYS_BEFORE_NOW, NOW, retriever.RECENCY_CUE_WEIGHT
    ) == pytest.approx(retriever.RECENCY_CUE_WEIGHT * decay, abs=1e-9)


def test_a_cue_multiplies_the_recency_term_in_the_pipeline(conn: sqlite3.Connection) -> None:
    """AC-cue-a and AC-cue-b: the same row, the same decay, two recency weights.

    The stored text deliberately does **not** contain "recently": a fixture that
    contained the cue word would pass even if cue tokens leaked into the MATCH
    expression, which is exactly the bug this fixture has to be able to see.
    """
    added = store.add_memory(conn, "the migration ran on staging", WORKSPACE)
    _backdate(conn, added.id, THIRTY_DAYS_BEFORE_NOW)
    assert "recent" not in "the migration ran on staging"
    decay = 2.0 ** (-30 / retriever.RECENCY_HALF_LIFE_DAYS)

    uncued = retriever.retrieve(conn, "migration", WORKSPACE, now=NOW).results[0]
    cued = retriever.retrieve(conn, "migration recently", WORKSPACE, now=NOW).results[0]

    # The lone candidate normalizes to 1.0 in both runs, so the whole difference is recency.
    assert uncued.lexical_score == cued.lexical_score == 1.0
    base = retriever.LEXICAL_WEIGHT
    assert uncued.score - base == pytest.approx(retriever.RECENCY_WEIGHT * decay, abs=1e-9)
    assert cued.score - base == pytest.approx(retriever.RECENCY_CUE_WEIGHT * decay, abs=1e-9)


def test_a_cue_flips_the_ranking_towards_the_fresher_row(conn: sqlite3.Connection) -> None:
    """AC-cue-d.

    Both rows are anagrams, so their lexical scores are equal and cancel. The older row
    carries a ``seen_count`` boost of ``0.02·ln(20) ≈ 0.0599`` — more than the uncued
    recency spread of ``0.05·0.75 = 0.0375``, less than the cued ``0.25·0.75 = 0.1875``.
    """
    older = store.add_memory(conn, "deploy pipeline broke badly", WORKSPACE)
    newer = store.add_memory(conn, "badly broke deploy pipeline", WORKSPACE)
    _backdate(conn, older.id, SIXTY_DAYS_BEFORE_NOW)
    _backdate(conn, newer.id, AT_NOW)
    conn.execute("UPDATE memories SET seen_count = 20 WHERE id = ?", (older.id,))
    # Neither row contains "today", so the cue can only reach them by being stripped out
    # of the lexical query first.
    assert "today" not in "deploy pipeline broke badly"

    uncued = retriever.retrieve(conn, "deploy pipeline", WORKSPACE, now=NOW)
    cued = retriever.retrieve(conn, "deploy pipeline today", WORKSPACE, now=NOW)

    assert retriever.has_recency_cue("deploy pipeline") is False
    assert retriever.has_recency_cue("deploy pipeline today") is True
    assert [hit.id for hit in uncued.results] == [older.id, newer.id]
    assert [hit.id for hit in cued.results] == [newer.id, older.id]


# --- cue stripping and pure recency mode ------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("recent pnpm", "pnpm"),
        ("notes from last week", "notes from"),
        ("what did we decide recently about the API", "what did we decide about the API"),
        ("today", ""),
        ("tuần trước", ""),
        ("tuan truoc", ""),
        ("hôm qua", ""),
    ],
)
def test_strip_recency_cues_removes_only_the_matched_spans(query: str, expected: str) -> None:
    residual, found = retriever.strip_recency_cues(query)
    assert (residual, found) == (expected, True)


@pytest.mark.parametrize("query", ["pnpm", "!!!", "todayish", "use pnpm not npm"])
def test_strip_recency_cues_leaves_a_cue_free_query_untouched(query: str) -> None:
    # Byte-for-byte identical, so the overwhelmingly common path cannot regress.
    assert retriever.strip_recency_cues(query) == (query, False)


def test_a_cued_query_returns_the_same_rows_as_the_bare_query(conn: sqlite3.Connection) -> None:
    """Regression test for cue words leaking into the conjunctive FTS5 MATCH.

    None of the stored rows contains "recent", so before cue stripping
    ``search "recent pnpm"`` returned nothing at all while ``search "pnpm"`` returned two
    rows. The fixture cannot pass accidentally: the assertion below pins that absence.
    """
    contents = [
        "we switched the deploy pipeline to pnpm",
        "the pnpm lockfile was regenerated",
        "unrelated note about coffee",
    ]
    assert not any("recent" in content for content in contents)
    for content in contents:
        store.add_memory(conn, content, WORKSPACE)

    bare = retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    cued = retriever.retrieve(conn, "recent pnpm", WORKSPACE, now=NOW)

    assert len(bare.results) == 2
    assert {hit.id for hit in cued.results} == {hit.id for hit in bare.results}
    # Same rows, heavier recency term.
    assert min(hit.score for hit in cued.results) > min(hit.score for hit in bare.results)


def test_pure_recency_mode_returns_newest_first(conn: sqlite3.Connection) -> None:
    oldest = store.add_memory(conn, "alpha note", WORKSPACE)
    middle = store.add_memory(conn, "bravo note", WORKSPACE)
    newest = store.add_memory(conn, "charlie note", WORKSPACE)
    _backdate(conn, oldest.id, SIXTY_DAYS_BEFORE_NOW)
    _backdate(conn, middle.id, THIRTY_DAYS_BEFORE_NOW)
    _backdate(conn, newest.id, AT_NOW)

    outcome = retriever.retrieve(conn, "today", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [newest.id, middle.id, oldest.id]
    assert outcome.message is None
    # No lexical or relational question was asked, so neither view scored anything.
    assert all(
        hit.lexical_score is None and hit.relational_score is None for hit in outcome.results
    )
    assert outcome.results[0].score == pytest.approx(retriever.RECENCY_CUE_WEIGHT, abs=1e-9)
    assert outcome.results[1].score == pytest.approx(retriever.RECENCY_CUE_WEIGHT * 0.5, abs=1e-9)


def test_pure_recency_mode_honours_k_and_the_workspace(conn: sqlite3.Connection) -> None:
    for index in range(4):
        store.add_memory(conn, f"note {index}", "a")
    store.add_memory(conn, "somebody else's note", "b")

    outcome = retriever.retrieve(conn, "tuần trước", "a", k=2, now=NOW)

    assert len(outcome.results) == 2
    assert {hit.workspace for hit in outcome.results} == {"a"}


def test_pure_recency_mode_attaches_neighbors_and_core_memory(conn: sqlite3.Connection) -> None:
    """Closure and core memory behave exactly as on the normal path."""
    store.add_memory(conn, "prefer small commits", WORKSPACE, "core")
    store.add_memory(conn, "config_loader reads the timeout", WORKSPACE)
    store.add_memory(conn, "config_loader caches nothing", WORKSPACE)

    outcome = retriever.retrieve(conn, "today", WORKSPACE, k=1, now=NOW)

    assert len(outcome.results) == 1
    assert outcome.core_memory == "prefer small commits"
    assert len(outcome.results[0].neighbors) >= 1


def test_pure_recency_mode_on_an_empty_workspace_is_not_an_error(
    conn: sqlite3.Connection,
) -> None:
    store.add_memory(conn, "a note somewhere else", "other")
    outcome = retriever.retrieve(conn, "today", "empty-workspace", now=NOW)
    assert outcome.results == ()
    assert outcome.message == retriever.EMPTY_MESSAGE
    assert outcome.core_memory == ""


def test_punctuation_only_query_does_not_enter_recency_mode(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "use pnpm not npm", WORKSPACE)
    outcome = retriever.retrieve(conn, "!!!", WORKSPACE, now=NOW)
    assert outcome.results == ()
    assert outcome.message == retriever.EMPTY_MESSAGE


def test_cli_search_with_a_cue_still_finds_rows(db_path: Path) -> None:
    """The user-visible half of the regression: this printed no matches before."""
    runner = CliRunner()
    runner.invoke(main, ["add", "we switched the deploy pipeline to pnpm", "-w", WORKSPACE])
    runner.invoke(main, ["add", "the pnpm lockfile was regenerated", "-w", WORKSPACE])

    bare = runner.invoke(main, ["search", "pnpm", "-w", WORKSPACE])
    cued = runner.invoke(main, ["search", "recent pnpm", "-w", WORKSPACE])

    assert cued.exit_code == 0
    assert "no memories matching" not in cued.output
    assert cued.output.count("workspace=") == bare.output.count("workspace=") == 2
    assert db_path.exists()


# --- evidence closure -------------------------------------------------------


def test_neighbors_come_from_entity_siblings(conn: sqlite3.Connection) -> None:
    """AC14."""
    target = store.add_memory(conn, "config_loader reads the timeout", WORKSPACE)
    sibling_one = store.add_memory(conn, "config_loader caches nothing", WORKSPACE)
    sibling_two = store.add_memory(conn, "config_loader is not thread safe", WORKSPACE)
    store.add_memory(conn, "unrelated note about the weather", WORKSPACE)

    outcome = retriever.retrieve(conn, "timeout", WORKSPACE, now=NOW)
    assert [hit.id for hit in outcome.results] == [target.id]
    neighbors = {neighbor.id for neighbor in outcome.results[0].neighbors}
    assert neighbors == {sibling_one.id, sibling_two.id}


def test_neighbors_are_capped_and_never_repeat_a_result(conn: sqlite3.Connection) -> None:
    """AC14."""
    for index in range(5):
        store.add_memory(conn, f"config_loader detail number {index}", WORKSPACE)
    outcome = retriever.retrieve(conn, "config_loader detail", WORKSPACE, k=5, now=NOW)

    selected = {hit.id for hit in outcome.results}
    assert len(selected) == 5
    for hit in outcome.results:
        assert len(hit.neighbors) <= retriever.MAX_NEIGHBORS
        assert not {neighbor.id for neighbor in hit.neighbors} & selected


def test_neighbors_prefer_session_adjacency(conn: sqlite3.Connection) -> None:
    """AC14: a populated session_id takes the adjacency path before entity siblings."""
    before = store.add_memory(conn, "opened the incident channel", WORKSPACE, "trace", None, "s1")
    target = store.add_memory(
        conn, "the culprit was a stale timeout", WORKSPACE, "trace", None, "s1"
    )
    after = store.add_memory(conn, "closed the incident channel", WORKSPACE, "trace", None, "s1")
    store.add_memory(conn, "unrelated row in another session", WORKSPACE, "trace", None, "s2")

    outcome = retriever.retrieve(conn, "culprit", WORKSPACE, now=NOW)
    assert [hit.id for hit in outcome.results] == [target.id]
    assert {neighbor.id for neighbor in outcome.results[0].neighbors} == {before.id, after.id}


def test_session_neighbors_stay_inside_the_workspace(conn: sqlite3.Connection) -> None:
    target = store.add_memory(conn, "the culprit was a stale timeout", "a", "trace", None, "s1")
    store.add_memory(conn, "same session, other workspace", "b", "trace", None, "s1")
    outcome = retriever.retrieve(conn, "culprit", "a", now=NOW)
    assert [hit.id for hit in outcome.results] == [target.id]
    assert outcome.results[0].neighbors == ()


# --- CLI --------------------------------------------------------------------


def test_cli_search_prints_core_memory_and_neighbors(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "prefer pnpm everywhere", "-w", WORKSPACE, "--kind", "core"])
    runner.invoke(main, ["add", "config_loader reads the timeout", "-w", WORKSPACE])
    runner.invoke(main, ["add", "config_loader caches nothing", "-w", WORKSPACE])
    result = runner.invoke(main, ["search", "timeout", "-w", WORKSPACE])

    assert result.exit_code == 0
    assert "core memory" in result.output
    assert "prefer pnpm everywhere" in result.output
    assert "related id=" in result.output
    assert "config_loader caches nothing" in result.output
    assert db_path.exists()


def test_cli_search_prints_source_and_shortens_a_long_neighbor(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(
        main,
        ["add", "config_loader reads the timeout", "-w", WORKSPACE, "--source", "claude-code"],
    )
    runner.invoke(main, ["add", "config_loader " + "detail " * 60, "-w", WORKSPACE])
    result = runner.invoke(main, ["search", "timeout", "-w", WORKSPACE])

    assert result.exit_code == 0
    assert "source: claude-code" in result.output
    assert "…" in result.output


def test_cli_search_warns_when_core_memory_is_truncated(db_path: Path) -> None:
    runner = CliRunner()
    for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"):
        runner.invoke(main, ["add", f"{word} " * 100, "-w", WORKSPACE, "--kind", "core"])
    runner.invoke(main, ["add", "config_loader reads the timeout", "-w", WORKSPACE])
    result = runner.invoke(main, ["search", "timeout", "-w", WORKSPACE])

    assert result.exit_code == 0
    assert "older core rows dropped to fit the cap" in result.output


def test_cli_search_still_reports_no_matches(db_path: Path) -> None:
    result = CliRunner().invoke(main, ["search", "nothing here", "-w", WORKSPACE])
    assert result.exit_code == 0
    assert "no memories matching" in result.output
