"""Tests for the M3 retrieval pipeline: tokens, core memory and the dual-view retriever.

Every clock-dependent assertion injects ``now`` instead of patching the standard library,
so nothing here depends on the machine's date.
"""

from __future__ import annotations

import json
import math
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmem import cli, core_memory, retriever, store, tokens
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
    """AC6, re-read for v0.2: two *named* workspaces are still fully isolated.

    The global tier is the one deliberately shared workspace; nothing else leaks, which
    is what this fixture pins now that a fallback exists at all.
    """
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


# --- the shared global tier -------------------------------------------------

GLOBAL = core_memory.GLOBAL_WORKSPACE


def test_a_named_workspace_also_recalls_the_global_tier(conn: sqlite3.Connection) -> None:
    """The whole point of v0.2: a lesson stored once is reachable from every repo."""
    shared = store.add_memory(conn, "reset the upload buffer before retrying", GLOBAL)
    local = store.add_memory(conn, "this project routes upload traffic through nginx", WORKSPACE)

    outcome = retriever.retrieve(conn, "upload", WORKSPACE, now=NOW)

    assert {hit.id for hit in outcome.results} == {shared.id, local.id}
    assert {hit.workspace for hit in outcome.results} == {GLOBAL, WORKSPACE}


def test_the_global_tier_reaches_every_named_workspace_but_repos_stay_isolated(
    conn: sqlite3.Connection,
) -> None:
    shared = store.add_memory(conn, "check the upload buffer size first", GLOBAL)
    in_a = store.add_memory(conn, "upload retries are disabled in repo a", "a")

    from_b = retriever.retrieve(conn, "upload", "b", now=NOW)

    assert [hit.id for hit in from_b.results] == [shared.id]
    assert in_a.id not in {hit.id for hit in from_b.results}


def test_searching_the_global_workspace_does_not_widen_to_everything(
    conn: sqlite3.Connection,
) -> None:
    shared = store.add_memory(conn, "check the upload buffer size first", GLOBAL)
    store.add_memory(conn, "upload retries are disabled in repo a", "a")

    outcome = retriever.retrieve(conn, "upload", GLOBAL, now=NOW)

    assert [hit.id for hit in outcome.results] == [shared.id]


def test_searching_every_workspace_is_unchanged(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "check the upload buffer size first", GLOBAL)
    store.add_memory(conn, "upload retries are disabled in repo a", "a")
    store.add_memory(conn, "upload works fine in repo b", "b")

    assert len(retriever.retrieve(conn, "upload", None, now=NOW).results) == 3


def test_a_tie_puts_the_workspace_row_above_the_global_row(conn: sqlite3.Connection) -> None:
    """Equal fused score, and the current workspace wins. No penalty is applied.

    The two texts are anagrams, so bm25 — and therefore both normalized view scores —
    match exactly; both rows are backdated to the same instant and neither has been seen
    twice, so nothing but the tiebreak can separate them. The workspace row is inserted
    *first* on purpose: its lower id would otherwise sort it second, so the assertion
    cannot pass by accident.
    """
    own = store.add_memory(conn, "deploy pipeline broke badly", WORKSPACE)
    shared = store.add_memory(conn, "badly broke deploy pipeline", GLOBAL)
    _backdate(conn, own.id, AT_NOW)
    _backdate(conn, shared.id, AT_NOW)

    outcome = retriever.retrieve(conn, "deploy pipeline", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [own.id, shared.id]
    assert outcome.results[0].score == pytest.approx(outcome.results[1].score, abs=1e-12)


def test_a_better_global_row_still_outranks_a_weaker_local_one(
    conn: sqlite3.Connection,
) -> None:
    """The tiebreak is a tiebreak: it must not become a thumb on the scale."""
    store.add_memory(conn, "pnpm is mentioned once here among many other words", WORKSPACE)
    shared = store.add_memory(conn, "pnpm pnpm pnpm", GLOBAL)

    outcome = retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)

    assert outcome.results[0].id == shared.id
    assert outcome.results[0].score > outcome.results[1].score


def test_the_global_tier_reaches_the_pure_recency_view_too(conn: sqlite3.Connection) -> None:
    """Consistency: "what happened recently" must not answer from a different tier set."""
    shared = store.add_memory(conn, "a shared lesson", GLOBAL)
    local = store.add_memory(conn, "a local note", WORKSPACE)
    store.add_memory(conn, "someone else's note", "other")
    _backdate(conn, shared.id, THIRTY_DAYS_BEFORE_NOW)
    _backdate(conn, local.id, AT_NOW)

    outcome = retriever.retrieve(conn, "today", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [local.id, shared.id]


def test_entity_only_recall_reaches_the_global_tier(conn: sqlite3.Connection) -> None:
    """The relational view carries the fallback as well, not just bm25."""
    shared = store.add_memory(conn, "ConfigLoader retries three times on timeout", GLOBAL)
    query = "ConfigLoader zzqqxx"
    assert store.search_memories(conn, query, WORKSPACE) == []

    outcome = retriever.retrieve(conn, query, WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [shared.id]
    assert outcome.results[0].relational_score == 1.0


def test_evidence_closure_keeps_each_hit_inside_its_own_tier(conn: sqlite3.Connection) -> None:
    """A global hit gathers global neighbors; a repo hit gathers repo neighbors."""
    store.add_memory(conn, "config_loader reads the timeout", GLOBAL)
    global_sibling = store.add_memory(conn, "config_loader caches nothing", GLOBAL)
    store.add_memory(conn, "config_loader is wired up in main", WORKSPACE)

    outcome = retriever.retrieve(conn, "timeout", WORKSPACE, now=NOW)

    assert [hit.workspace for hit in outcome.results] == [GLOBAL]
    assert {neighbor.id for neighbor in outcome.results[0].neighbors} == {global_sibling.id}


# --- core memory across the two tiers ---------------------------------------


def test_core_memory_puts_the_workspace_tier_before_the_global_one(
    conn: sqlite3.Connection,
) -> None:
    shared = store.add_memory(conn, "prefer pnpm everywhere", GLOBAL, "core")
    own = store.add_memory(conn, "this project pins yarn", WORKSPACE, "core")
    # The shared row is older, so a naive single-list build would put it first.
    _backdate(conn, shared.id, "2026-01-01 00:00:00")
    _backdate(conn, own.id, "2026-02-01 00:00:00")

    built = core_memory.build_core_memory(conn, WORKSPACE)

    assert built.text == "this project pins yarn\nprefer pnpm everywhere"
    assert built.dropped == 0


def test_core_memory_of_the_global_workspace_is_not_doubled(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "prefer pnpm everywhere", GLOBAL, "core")
    assert core_memory.build_core_memory(conn, GLOBAL).text == "prefer pnpm everywhere"


def test_core_memory_drops_the_global_tier_before_the_workspace_tier(
    conn: sqlite3.Connection,
) -> None:
    """AC15 re-read: the cap costs the shared tier first, never the repo's own rows."""
    for index, word in enumerate(("alpha", "bravo", "charlie"), start=1):
        shared = store.add_memory(conn, f"{word} " * 100, GLOBAL, "core")
        _backdate(conn, shared.id, f"2026-01-{index:02d} 00:00:00")
    own = store.add_memory(conn, "delta " * 100, WORKSPACE, "core")
    _backdate(conn, own.id, "2026-03-01 00:00:00")

    built = core_memory.build_core_memory(conn, WORKSPACE)

    assert built.tokens <= core_memory.CORE_MEMORY_TOKEN_CAP
    lines = built.text.splitlines()
    assert lines[0].split()[0] == "delta"
    assert built.dropped == 4 - len(lines)
    assert all(len(line.split()) == 100 for line in lines)


def test_core_memory_reaches_a_recall_from_another_workspace(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "prefer pnpm everywhere", GLOBAL, "core")
    store.add_memory(conn, "the migration ran on staging", WORKSPACE)

    outcome = retriever.retrieve(conn, "migration", WORKSPACE, now=NOW)

    assert outcome.core_memory == "prefer pnpm everywhere"


def test_core_memory_none_scope_is_unchanged(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "only for a", "a", "core")
    store.add_memory(conn, "shared", GLOBAL, "core")
    assert set(core_memory.build_core_memory(conn, None).text.splitlines()) == {
        "only for a",
        "shared",
    }


def test_core_workspaces_lists_only_workspaces_holding_core_rows(
    conn: sqlite3.Connection,
) -> None:
    store.add_memory(conn, "a plain note", WORKSPACE)
    store.add_memory(conn, "a core row", "b", "core")
    store.add_memory(conn, "another core row", "a", "core")
    assert core_memory.core_workspaces(conn) == ["a", "b"]


# --- recall usage tracking (schema version 2) -------------------------------


def _tracking(conn: sqlite3.Connection, memory_id: int) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT recalled_count, last_recalled_at FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    return int(row["recalled_count"]), row["last_recalled_at"]


def test_a_returned_memory_gets_its_recall_counted(conn: sqlite3.Connection) -> None:
    added = store.add_memory(conn, "use pnpm not npm", WORKSPACE)
    assert _tracking(conn, added.id) == (0, None)

    retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    count, stamp = _tracking(conn, added.id)
    assert count == 1
    assert stamp is not None

    retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    assert _tracking(conn, added.id)[0] == 2


def test_a_memory_that_was_not_returned_is_not_counted(conn: sqlite3.Connection) -> None:
    matched = store.add_memory(conn, "use pnpm not npm", WORKSPACE)
    missed = store.add_memory(conn, "unrelated note about coffee", WORKSPACE)
    retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    assert _tracking(conn, matched.id)[0] == 1
    assert _tracking(conn, missed.id)[0] == 0


def test_a_neighbor_is_evidence_not_a_recall(conn: sqlite3.Connection) -> None:
    target = store.add_memory(conn, "config_loader reads the timeout", WORKSPACE)
    sibling = store.add_memory(conn, "config_loader caches nothing", WORKSPACE)

    outcome = retriever.retrieve(conn, "timeout", WORKSPACE, now=NOW)

    assert {neighbor.id for neighbor in outcome.results[0].neighbors} == {sibling.id}
    assert _tracking(conn, target.id)[0] == 1
    assert _tracking(conn, sibling.id)[0] == 0


def test_tracking_never_costs_the_user_their_recall(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database error in the counter is swallowed; the results still come back."""
    added = store.add_memory(conn, "use pnpm not npm", WORKSPACE)
    monkeypatch.setattr(
        retriever,
        "_RECORD_RECALL_SQL",
        "UPDATE no_such_table SET recalled_count = 1 WHERE id IN ({placeholders})",
    )

    outcome = retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [added.id]
    assert _tracking(conn, added.id) == (0, None)


def test_an_empty_result_set_records_nothing(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "use pnpm not npm", WORKSPACE)
    retriever.retrieve(conn, "nothing matches this", WORKSPACE, now=NOW)
    assert conn.execute("SELECT SUM(recalled_count) AS n FROM memories").fetchone()["n"] == 0


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


# --- supersede: ranking, evidence and the no-op guarantee -------------------

WRONG = "the leak is in src/resizer.py — it holds every buffer it allocates"
RIGHT = "not a leak: the connection pool was exhausted, max=5 in config/db.yml"

#: The ranking milestone B produced for :func:`_supersede_corpus`, captured by running
#: this exact corpus against the previous commit's ``localmem`` package in a detached
#: worktree — not by copying what the current code prints. Ids, scores to nine places and
#: the neighbour lists must all still match, because C's only intended ranking change is
#: the supersede penalty and a corpus with no superseded row must not feel it.
MILESTONE_B_RANKING: dict[str, list[tuple[int, float, list[int]]]] = {
    "pnpm": [(1, 0.625, []), (5, 0.048857998, [])],
    "413 upload nginx": [(2, 0.6125, [])],
    "src/main.py MyClass": [(3, 1.045586124, []), (6, 0.043527528, [])],
    "vpn deploy": [(4, 0.634547822, [])],
    "connection": [(6, 0.643527528, [3])],
}

_SUPERSEDE_CORPUS = (
    ("use pnpm not npm in this repo", "proj", ["package manager"], "2026-07-06 12:00:00"),
    (
        "the 413 on upload comes from nginx client_max_body_size",
        "proj",
        ["upload"],
        "2026-06-06 12:00:00",
    ),
    ("src/main.py boots MyClass before the config is read", "proj", [], "2026-08-01 12:00:00"),
    (
        "deploy needs the VPN, and the VPN needs the yubikey",
        "global",
        ["vpn"],
        "2026-07-20 12:00:00",
    ),
    ("Dùng pnpm thay vì npm cho dự án này", "proj", ["trình quản lý gói"], "2026-08-04 12:00:00"),
    (
        "the retry loop in src/main.py never releases a connection",
        "proj",
        [],
        "2026-07-30 12:00:00",
    ),
)


def _build_supersede_corpus(conn: sqlite3.Connection) -> None:
    for content, workspace, keywords, created_at in _SUPERSEDE_CORPUS:
        added = store.add_memory(conn, content, workspace, keywords=keywords)
        _backdate(conn, added.id, created_at)


def test_a_superseded_row_is_ranked_down_by_exactly_the_penalty(
    conn: sqlite3.Connection,
) -> None:
    """v0.4.0 C2. The correction shares no query token, so only the penalty moves."""
    wrong = store.add_memory(conn, WRONG, WORKSPACE)
    before = retriever.retrieve(conn, "resizer", WORKSPACE, now=NOW).results[0].score

    store.add_memory(conn, RIGHT, WORKSPACE, supersedes=[wrong.id])

    after = retriever.retrieve(conn, "resizer", WORKSPACE, now=NOW).results[0]
    assert after.id == wrong.id
    assert after.score == pytest.approx(before * retriever.SUPERSEDED_SCORE_PENALTY)


def test_the_penalty_demotes_but_never_removes(conn: sqlite3.Connection) -> None:
    """The user's requirement, literally: the wrong diagnosis is still findable."""
    wrong = store.add_memory(conn, "the 413 is the app body-parser limit", WORKSPACE)
    right = store.add_memory(
        conn, "the 413 is nginx client_max_body_size", WORKSPACE, supersedes=[wrong.id]
    )

    outcome = retriever.retrieve(conn, "413", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [right.id, wrong.id]
    assert outcome.results[1].score < outcome.results[0].score


# The two configurations measured on the multiply-only build, where the retraction won
# both. `_capped_below_replacement` is what turns them around; parametrizing them keeps
# the numbers in the constant's docstring tied to something that runs.
@pytest.mark.parametrize(
    ("wrong_created_at", "right_created_at"),
    [
        pytest.param(SIXTY_DAYS_BEFORE_NOW, SIXTY_DAYS_BEFORE_NOW, id="both-old"),
        pytest.param(SIXTY_DAYS_BEFORE_NOW, AT_NOW, id="retraction-old-correction-new"),
    ],
)
def test_the_correction_outranks_the_retraction_whenever_both_are_found(
    conn: sqlite3.Connection, wrong_created_at: str, right_created_at: str
) -> None:
    """v0.4.0 C2, the guarantee: the retraction is the better *lexical* match here.

    It is shorter, so bm25 puts it on top and `_min_max` floors the correction at 0.0 —
    the case where multiplying by 0.1 left the retraction winning at 0.0612 to 0.0489.
    """
    wrong = store.add_memory(conn, "the 413 is the body-parser limit", WORKSPACE)
    right = store.add_memory(
        conn,
        "the 413 is nginx client_max_body_size, never the body-parser limit",
        WORKSPACE,
        supersedes=[wrong.id],
    )
    _backdate(conn, wrong.id, wrong_created_at)
    _backdate(conn, right.id, right_created_at)

    outcome = retriever.retrieve(conn, "413 body-parser limit", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [right.id, wrong.id]


def test_the_cap_holds_when_the_correction_itself_scores_zero(
    conn: sqlite3.Connection,
) -> None:
    """The corner the cap cannot separate on score alone, pinned deliberately.

    The correction is the weakest candidate, so `_min_max` gives it 0.0, and both rows
    are dated far enough back that ``2**(-age/30)`` underflows to exactly 0.0 for both —
    so the correction scores 0.0, the cap lands on 0.0, and the two scores are equal.
    What separates them is `_fuse`'s sort key, and both rows are given the **same**
    ``created_at`` so the only thing left to break the tie is the id. A correction always
    has the larger one, because it is written afterwards. If the sort key is ever
    reordered, this test is the thing that notices.
    """
    wrong = store.add_memory(conn, "the 413 is the body-parser limit", WORKSPACE)
    right = store.add_memory(
        conn,
        "the 413 is nginx client_max_body_size, never the body-parser limit",
        WORKSPACE,
        supersedes=[wrong.id],
    )
    for memory_id in (wrong.id, right.id):
        _backdate(conn, memory_id, "1800-01-01 00:00:00")

    outcome = retriever.retrieve(conn, "413 body-parser limit", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [right.id, wrong.id]
    assert outcome.results[0].score == outcome.results[1].score == 0.0


def test_the_cap_is_not_applied_when_the_correction_was_not_found(
    conn: sqlite3.Connection,
) -> None:
    """No cap without a replacement to cap against — the plain multiply still applies."""
    wrong = store.add_memory(conn, WRONG, WORKSPACE)
    before = retriever.retrieve(conn, "resizer", WORKSPACE, now=NOW).results[0].score
    right = store.add_memory(conn, RIGHT, WORKSPACE, supersedes=[wrong.id])

    outcome = retriever.retrieve(conn, "resizer", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [wrong.id]
    assert outcome.results[0].score == pytest.approx(before * retriever.SUPERSEDED_SCORE_PENALTY)
    assert [neighbor.id for neighbor in outcome.results[0].neighbors] == [right.id]


def test_a_chain_ranks_oldest_diagnosis_last(conn: sqlite3.Connection) -> None:
    """Each cap is taken against an already demoted score, so the chain stays ordered."""
    first = store.add_memory(conn, "the 413 is the body-parser limit", WORKSPACE)
    second = store.add_memory(
        conn, "the 413 is the proxy body limit, not body-parser", WORKSPACE, supersedes=[first.id]
    )
    third = store.add_memory(
        conn,
        "the 413 is nginx client_max_body_size, not the proxy or the body-parser",
        WORKSPACE,
        supersedes=[second.id],
    )

    outcome = retriever.retrieve(conn, "413 body-parser", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [third.id, second.id, first.id]


def test_a_superseded_hit_carries_its_replacement_as_its_first_neighbour(
    conn: sqlite3.Connection,
) -> None:
    """v0.4.0 C3: the correction rides along in the frozen ``neighbors`` field."""
    wrong = store.add_memory(conn, WRONG, WORKSPACE)
    # An entity sibling that would otherwise take the first slot; the replacement outranks it.
    sibling = store.add_memory(conn, "src/resizer.py has been in the tree since 2019", WORKSPACE)
    right = store.add_memory(conn, RIGHT, WORKSPACE, supersedes=[wrong.id])

    outcome = retriever.retrieve(conn, "buffer", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [wrong.id]
    neighbors = outcome.results[0].neighbors
    assert neighbors[0] == retriever.Neighbor(id=right.id, content=RIGHT)
    assert [neighbor.id for neighbor in neighbors] == [right.id, sibling.id]


def test_the_replacement_is_not_repeated_when_it_already_ranked(
    conn: sqlite3.Connection,
) -> None:
    """`_neighbors` dedupes against the result list, and that covers this path too."""
    wrong = store.add_memory(conn, "the 413 is the app body-parser limit", WORKSPACE)
    right = store.add_memory(
        conn, "the 413 is nginx client_max_body_size", WORKSPACE, supersedes=[wrong.id]
    )

    outcome = retriever.retrieve(conn, "413", WORKSPACE, now=NOW)

    superseded = next(hit for hit in outcome.results if hit.id == wrong.id)
    assert right.id not in {neighbor.id for neighbor in superseded.neighbors}


def test_the_replacement_neighbour_crosses_from_the_global_tier(
    conn: sqlite3.Connection,
) -> None:
    """A global lesson may retract a repo memory, so the evidence must cross too."""
    wrong = store.add_memory(conn, WRONG, WORKSPACE)
    right = store.add_memory(conn, RIGHT, core_memory.GLOBAL_WORKSPACE, supersedes=[wrong.id])

    outcome = retriever.retrieve(conn, "resizer", WORKSPACE, now=NOW)

    assert outcome.results[0].id == wrong.id
    assert [neighbor.id for neighbor in outcome.results[0].neighbors] == [right.id]


def test_core_memory_skips_a_superseded_row(conn: sqlite3.Connection) -> None:
    """v0.4.0 C4: a retracted convention stops being pushed into every recall."""
    retracted = store.add_memory(conn, "always deploy from the release branch", WORKSPACE, "core")
    store.add_memory(conn, "run migrations before the test suite", WORKSPACE, "core")
    assert "release branch" in core_memory.build_core_memory(conn, WORKSPACE).text

    store.add_memory(
        conn,
        "deploy from main; the release branch is gone",
        WORKSPACE,
        "core",
        supersedes=[retracted.id],
    )

    built = core_memory.build_core_memory(conn, WORKSPACE)
    assert "release branch is gone" in built.text
    assert "always deploy from the release branch" not in built.text
    # Still stored, still retrievable — excluded from the push tier, not deleted.
    assert retracted.id in [
        hit.id for hit in store.search_memories(conn, "release branch", WORKSPACE)
    ]


def test_a_workspace_whose_only_core_row_is_superseded_has_no_core_memory(
    conn: sqlite3.Connection,
) -> None:
    """The workspace listing is filtered too, so `stats` and `audit` agree with recall."""
    retracted = store.add_memory(conn, "the only core row here", "solo", "core")
    store.add_memory(conn, "and its correction, which is a note", "solo", supersedes=[retracted.id])

    assert core_memory.core_workspaces(conn) == []
    assert core_memory.build_core_memory(conn, "solo").text == ""


def test_ranking_is_identical_to_milestone_b_when_nothing_is_superseded(
    conn: sqlite3.Connection,
) -> None:
    """The regression that matters: C's penalty must be invisible until it is used."""
    _build_supersede_corpus(conn)

    for query, expected in MILESTONE_B_RANKING.items():
        outcome = retriever.retrieve(conn, query, "proj", k=5, now=NOW)
        actual = [
            (hit.id, round(hit.score, 9), [neighbor.id for neighbor in hit.neighbors])
            for hit in outcome.results
        ]
        assert actual == expected, query


# --- the disjunctive fallback -----------------------------------------------


def test_the_fallback_finds_a_row_the_conjunctive_query_missed(
    conn: sqlite3.Connection,
) -> None:
    """The measured failure: one unmatched token used to zero out the whole query."""
    target = store.add_memory(conn, "client_max_body_size mặc định 1m trong nginx", WORKSPACE)
    store.add_memory(conn, "totally different subject about the weather", WORKSPACE)
    # "khi" appears in no memory, so the conjunctive MATCH returns nothing at all.
    assert store.search_memories(conn, "nginx khi", WORKSPACE) == []

    outcome = retriever.retrieve(conn, "nginx khi", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [target.id]
    assert outcome.results[0].from_fallback is True


def test_the_fallback_does_not_fire_when_the_lexical_view_answered(
    conn: sqlite3.Connection,
) -> None:
    store.add_memory(conn, "config_loader reads the timeout", WORKSPACE)
    store.add_memory(conn, "an unrelated note about timeout handling elsewhere", WORKSPACE)

    outcome = retriever.retrieve(conn, "config_loader timeout", WORKSPACE, now=NOW)

    # The conjunctive query matched, so the second row — which shares only "timeout" —
    # must not be dragged in.
    assert len(outcome.results) == 1
    assert outcome.results[0].from_fallback is False


def test_the_fallback_does_not_fire_when_only_the_relational_view_answered(
    conn: sqlite3.Connection,
) -> None:
    """The gate is "both views empty". An entity-only hit still counts as answered."""
    store.add_memory(conn, "ConfigLoader retries three times on timeout", WORKSPACE)
    store.add_memory(conn, "a note mentioning retries and nothing else relevant", WORKSPACE)
    query = "ConfigLoader zzqqxx"
    assert store.search_memories(conn, query, WORKSPACE) == []
    assert retriever.query_entities(query) == ["configloader"]

    outcome = retriever.retrieve(conn, query, WORKSPACE, now=NOW)

    assert [hit.from_fallback for hit in outcome.results] == [False]
    assert outcome.results[0].lexical_score is None


def test_the_fallback_cannot_invent_a_hit_from_nothing(conn: sqlite3.Connection) -> None:
    """It relaxes AND to OR; it does not lower the bar to "no shared token at all"."""
    store.add_memory(conn, "config_loader reads the timeout", WORKSPACE)

    outcome = retriever.retrieve(conn, "cấu hình tailwind", WORKSPACE, now=NOW)

    assert outcome.results == ()
    assert outcome.message == retriever.EMPTY_MESSAGE


def test_a_keyword_reaches_the_retriever_without_the_fallback(
    conn: sqlite3.Connection,
) -> None:
    """Keywords are the main lever; the fallback is only the safety net behind them."""
    target = store.add_memory(
        conn,
        "client_max_body_size mặc định 1m trong nginx",
        WORKSPACE,
        keywords=["413", "upload", "tải lên"],
    )

    outcome = retriever.retrieve(conn, "413 upload", WORKSPACE, now=NOW)

    assert [hit.id for hit in outcome.results] == [target.id]
    assert outcome.results[0].from_fallback is False


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


# --- search --context -------------------------------------------------------


def test_context_prints_absolutely_nothing_without_hits(db_path: Path) -> None:
    """The whole point of the mode: a prompt hook runs this on every single prompt."""
    result = CliRunner().invoke(main, ["search", "nothing here", "--context", "-w", WORKSPACE])
    assert result.exit_code == 0
    assert result.output == ""


def test_context_prints_nothing_against_an_empty_database(db_path: Path) -> None:
    result = CliRunner().invoke(main, ["search", "anything", "--context", "--all"])
    assert result.exit_code == 0
    assert result.output == ""


def test_context_prints_a_header_and_one_line_per_hit(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "config_loader reads the timeout", "-w", WORKSPACE])
    runner.invoke(main, ["add", "config_loader caches nothing", "-w", WORKSPACE])

    result = runner.invoke(main, ["search", "config_loader", "--context", "-w", WORKSPACE])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "Relevant memories (localmem):"
    assert len(lines) == 3
    assert all(line.startswith(f"- ({WORKSPACE}) ") for line in lines[1:])


def test_context_collapses_a_multiline_memory_onto_one_line(db_path: Path) -> None:
    runner = CliRunner()
    multiline = "config_loader:\n  reads the timeout\n  caches nothing"
    runner.invoke(main, ["add", multiline, "-w", WORKSPACE])

    result = runner.invoke(main, ["search", "config_loader", "--context", "-w", WORKSPACE])

    assert result.output.splitlines()[1:] == [
        f"- ({WORKSPACE}) config_loader: reads the timeout caches nothing"
    ]


def test_context_truncates_a_long_memory_and_names_the_id(db_path: Path) -> None:
    """A whole-file skill must not paste itself into every prompt."""
    runner = CliRunner()
    long_memory = "config_loader " + "detail " * 200
    added = json.loads(runner.invoke(main, ["add", long_memory, "-w", WORKSPACE]).output)

    result = runner.invoke(main, ["search", "config_loader", "--context", "-w", WORKSPACE])

    line = result.output.splitlines()[1]
    body = line[len(f"- ({WORKSPACE}) ") :]
    head, marker, tail = body.partition("…")
    assert len(head) == cli.CONTEXT_SNIPPET_CHARS
    assert marker == "…"
    assert tail == f" (memory_recall id {added['id']} for full text)"


def test_context_never_splits_a_vietnamese_letter_in_half(db_path: Path) -> None:
    """NFD `ế` is three codepoints; cutting between them turns it into `e`.

    The memory is built so the 400th codepoint lands *inside* a cluster, which is the
    only case that can go wrong and the one a fixed slice gets wrong silently.
    """
    runner = CliRunner()
    letter = unicodedata.normalize("NFD", "ế")
    assert len(letter) == 3, "the fixture only tests anything if NFD really decomposes"

    # Place the cluster so that BOTH the cut index and the codepoint before it are combining
    # marks: the base lands at 398, its two marks at 399 and 400. Anything less exercises the
    # fast path and passes against a plain slice.
    prefix = "config_loader "
    limit = cli.CONTEXT_SNIPPET_CHARS
    content = f"{prefix}{'x' * (limit - 2 - len(prefix))}{letter * 40}"
    assert unicodedata.combining(content[limit]) != 0
    assert unicodedata.combining(content[limit - 1]) != 0
    runner.invoke(main, ["add", content, "-w", WORKSPACE])

    result = runner.invoke(main, ["search", "config_loader", "--context", "-w", WORKSPACE])

    head = result.output.splitlines()[1].split("…")[0]
    assert unicodedata.combining(head[-1]) == 0, "the snippet ends on a combining mark"
    # The whole cluster is given up, base included — never the base without its marks.
    assert head[-1] == "x"
    # And the cut still lands as late as it can: at most one cluster is surrendered.
    assert len(head) >= limit - len(letter)


def test_cut_point_is_the_limit_when_nothing_is_split() -> None:
    assert cli._cut_point("a" * 500, cli.CONTEXT_SNIPPET_CHARS) == cli.CONTEXT_SNIPPET_CHARS


def test_context_leaves_a_short_memory_whole(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "config_loader reads the timeout", "-w", WORKSPACE])
    result = runner.invoke(main, ["search", "config_loader", "--context", "-w", WORKSPACE])
    assert result.output.splitlines()[1] == f"- ({WORKSPACE}) config_loader reads the timeout"
    assert "…" not in result.output


def test_context_never_injects_core_memory(db_path: Path) -> None:
    """DD-30: core is charged once per recall, never once per prompt."""
    runner = CliRunner()
    runner.invoke(main, ["add", "prefer pnpm everywhere", "-w", WORKSPACE, "--kind", "core"])
    runner.invoke(main, ["add", "config_loader reads the timeout", "-w", WORKSPACE])

    result = runner.invoke(main, ["search", "config_loader", "--context", "-w", WORKSPACE])

    assert "core memory" not in result.output
    assert "prefer pnpm everywhere" not in result.output
    # The same query without the flag still gets it.
    plain = runner.invoke(main, ["search", "config_loader", "-w", WORKSPACE])
    assert "prefer pnpm everywhere" in plain.output


def test_context_shows_the_workspace_that_answered(db_path: Path) -> None:
    """The shared tier is legible in the injected block, exactly as in plain output."""
    runner = CliRunner()
    runner.invoke(main, ["add", "config_loader reads the timeout", "-w", "global"])
    result = runner.invoke(main, ["search", "config_loader", "--context", "-w", WORKSPACE])
    assert result.output.splitlines()[1].startswith("- (global) ")


def test_context_honours_k(db_path: Path) -> None:
    runner = CliRunner()
    for index in range(5):
        runner.invoke(main, ["add", f"config_loader detail number {index}", "-w", WORKSPACE])
    result = runner.invoke(
        main, ["search", "config_loader", "--context", "-k", "2", "-w", WORKSPACE]
    )
    assert len(result.output.splitlines()) == 3


def test_context_prints_no_neighbors_or_scores(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "config_loader reads the timeout", "-w", WORKSPACE])
    runner.invoke(main, ["add", "config_loader caches nothing", "-w", WORKSPACE])
    result = runner.invoke(main, ["search", "timeout", "--context", "-w", WORKSPACE])
    assert "related id=" not in result.output
    assert "score" not in result.output


def test_context_drops_fallback_hits_but_plain_search_keeps_them(db_path: Path) -> None:
    """The hook runs on every prompt, so the weak pass is exactly where noise is dear."""
    runner = CliRunner()
    runner.invoke(main, ["add", "client_max_body_size 1m trong nginx", "-w", WORKSPACE])
    query = "nginx khi"

    plain = runner.invoke(main, ["search", query, "-w", WORKSPACE])
    context = runner.invoke(main, ["search", query, "--context", "-w", WORKSPACE])

    assert plain.exit_code == 0
    assert "client_max_body_size" in plain.output
    assert "weak: no exact match" in plain.output
    assert context.exit_code == 0
    assert context.output == ""


def test_context_fallback_opts_the_weak_hits_back_in(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "client_max_body_size 1m trong nginx", "-w", WORKSPACE])

    result = runner.invoke(main, ["search", "nginx khi", "--context-fallback", "-w", WORKSPACE])

    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "Relevant memories (localmem):"
    assert "client_max_body_size" in result.output


def test_context_still_prints_ordinary_hits_unchanged(db_path: Path) -> None:
    """The flag must not disturb the common path the hook actually runs."""
    runner = CliRunner()
    runner.invoke(main, ["add", "config_loader reads the timeout", "-w", WORKSPACE])

    result = runner.invoke(main, ["search", "config_loader", "--context", "-w", WORKSPACE])

    assert result.output.splitlines() == [
        "Relevant memories (localmem):",
        f"- ({WORKSPACE}) config_loader reads the timeout",
    ]


def test_cli_add_accepts_repeatable_keywords_and_recall_finds_them(db_path: Path) -> None:
    runner = CliRunner()
    added = runner.invoke(
        main,
        [
            "add",
            "client_max_body_size mặc định 1m trong nginx",
            "-w",
            WORKSPACE,
            "-K",
            "413",
            "-K",
            "tải lên",
        ],
    )
    assert json.loads(added.output)["status"] == "added"

    found = runner.invoke(main, ["search", "413", "-w", WORKSPACE])

    assert found.exit_code == 0
    assert "client_max_body_size" in found.output
    # A keyword hit is an ordinary hit, not a weak one.
    assert "weak: no exact match" not in found.output


# --- LOCALMEM_NO_TRACKING ---------------------------------------------------


def recalled_counts(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT recalled_count FROM memories ORDER BY id").fetchall()
    return [int(row["recalled_count"]) for row in rows]


def test_tracking_is_on_by_default(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "use pnpm not npm", WORKSPACE)
    retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    assert recalled_counts(conn) == [1]


def test_no_tracking_env_makes_recall_read_only(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.2.1 item 3: the opt-out, proven in both directions on one database."""
    store.add_memory(conn, "use pnpm not npm", WORKSPACE)

    monkeypatch.setenv(retriever.NO_TRACKING_ENV_VAR, "1")
    outcome = retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    assert len(outcome.results) == 1
    assert recalled_counts(conn) == [0]

    monkeypatch.delenv(retriever.NO_TRACKING_ENV_VAR)
    retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    assert recalled_counts(conn) == [1]


def test_no_tracking_accepts_any_non_empty_value(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Set at all means off — including ``0``, which nobody sets to mean "on"."""
    store.add_memory(conn, "use pnpm not npm", WORKSPACE)
    for value in ("1", "0", "no", "true"):
        monkeypatch.setenv(retriever.NO_TRACKING_ENV_VAR, value)
        retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    assert recalled_counts(conn) == [0]


def test_an_empty_no_tracking_value_leaves_tracking_on(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_memory(conn, "use pnpm not npm", WORKSPACE)
    monkeypatch.setenv(retriever.NO_TRACKING_ENV_VAR, "")
    retriever.retrieve(conn, "pnpm", WORKSPACE, now=NOW)
    assert recalled_counts(conn) == [1]


def test_cli_search_respects_no_tracking(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "use pnpm not npm", "-w", WORKSPACE])
    monkeypatch.setenv(retriever.NO_TRACKING_ENV_VAR, "1")
    runner.invoke(main, ["search", "pnpm", "--context", "-w", WORKSPACE])
    result = runner.invoke(main, ["stats"])
    assert "recalls: 0 recorded across all memories" in result.output
