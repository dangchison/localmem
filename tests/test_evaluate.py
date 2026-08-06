"""The eval harness, and the baseline that turns it into a regression gate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmem import evaluate, retriever
from localmem.cli import main

BASELINE_PATH = Path(__file__).parent / "fixtures" / "eval" / "baseline.json"

#: Set this to rewrite :data:`BASELINE_PATH` from the current code. The diff it produces
#: belongs in the CHANGELOG and the ADR of whatever change moved the numbers — that is
#: the whole point of pinning them.
UPDATE_BASELINE_ENV_VAR = "LOCALMEM_UPDATE_BASELINE"


def _tiny_fixture(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- fixture loading ---------------------------------------------------------


def test_bundled_fixture_loads() -> None:
    fixture = evaluate.load_fixture()
    assert fixture.version == evaluate.SUPPORTED_FIXTURE_VERSION
    assert fixture.name == "bilingual-v1"
    assert len(fixture.corpus) == 59
    assert len(fixture.positives) == 45
    assert len(fixture.negatives) == 20


def test_bundled_fixture_is_bilingual() -> None:
    """Both languages appear on both sides, which is the point of this corpus."""
    contents = [doc.content for doc in evaluate.load_fixture().corpus]
    assert any(any(ord(character) > 127 for character in text) for text in contents)
    assert any(all(ord(character) < 128 for character in text) for text in contents)


def test_unknown_version_is_refused(tmp_path: Path) -> None:
    path = _tiny_fixture(tmp_path, {"version": 99, "corpus": [], "queries": []})
    with pytest.raises(ValueError, match="fixture version"):
        evaluate.load_fixture(path)


def test_query_naming_an_unknown_document_is_refused(tmp_path: Path) -> None:
    path = _tiny_fixture(
        tmp_path,
        {
            "version": 1,
            "corpus": [{"id": "a", "content": "alpha"}],
            "queries": [{"id": "q", "text": "alpha", "relevant": ["ghost"]}],
        },
    )
    with pytest.raises(ValueError, match="unknown documents"):
        evaluate.load_fixture(path)


def test_repeated_corpus_id_is_refused(tmp_path: Path) -> None:
    path = _tiny_fixture(
        tmp_path,
        {
            "version": 1,
            "corpus": [{"id": "a", "content": "alpha"}, {"id": "a", "content": "beta"}],
            "queries": [],
        },
    )
    with pytest.raises(ValueError, match="repeats a corpus id"):
        evaluate.load_fixture(path)


def test_malformed_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        evaluate.load_fixture(path)


# --- metric arithmetic -------------------------------------------------------


def test_metrics_on_a_hand_checked_fixture(tmp_path: Path) -> None:
    """Three documents whose ranks can be read off by eye.

    ``alpha`` and ``beta`` are each their own query's only match; ``gamma`` is asked for
    with a word it does not carry, so that query misses entirely. Expected: two of three
    positives answered at rank 1, one never — recall@1 = recall@5 = 2/3, MRR = 2/3.
    """
    path = _tiny_fixture(
        tmp_path,
        {
            "version": 1,
            "name": "tiny",
            "corpus": [
                {"id": "alpha", "content": "kubernetes ingress annotation"},
                {"id": "beta", "content": "postgres vacuum threshold"},
                {"id": "gamma", "content": "tailwind purge globs"},
            ],
            "queries": [
                {"id": "q-alpha", "text": "kubernetes ingress", "relevant": ["alpha"]},
                {"id": "q-beta", "text": "postgres vacuum", "relevant": ["beta"]},
                {"id": "q-gamma", "text": "zzzunmatchable", "relevant": ["gamma"]},
            ],
        },
    )
    report = evaluate.run_eval(evaluate.load_fixture(path), tmp_path / "eval.db")

    assert report.positive_queries == 3
    assert report.negative_queries == 0
    assert report.recall[1] == pytest.approx(2 / 3, abs=1e-4)
    assert report.recall[5] == pytest.approx(2 / 3, abs=1e-4)
    assert report.mrr == pytest.approx(2 / 3, abs=1e-4)

    by_id = {outcome.query_id: outcome for outcome in report.outcomes}
    assert by_id["q-alpha"].first_gold_rank == 1
    assert by_id["q-gamma"].first_gold_rank is None


def test_off_corpus_silence_counts_only_negatives(tmp_path: Path) -> None:
    path = _tiny_fixture(
        tmp_path,
        {
            "version": 1,
            "corpus": [{"id": "alpha", "content": "kubernetes ingress annotation"}],
            "queries": [
                {"id": "q-hit", "text": "kubernetes ingress", "relevant": ["alpha"]},
                {"id": "q-quiet", "text": "zzzunmatchable", "relevant": []},
            ],
        },
    )
    report = evaluate.run_eval(evaluate.load_fixture(path), tmp_path / "eval.db")

    assert report.negative_queries == 1
    assert report.off_corpus_silent == 1
    assert report.recall[1] == pytest.approx(1.0)


# --- view attribution --------------------------------------------------------


def test_answer_sources_partition_the_queries(tmp_path: Path) -> None:
    report = evaluate.run_eval(evaluate.load_fixture(), tmp_path / "eval.db")
    assert set(report.answered_by) == set(evaluate.ANSWER_SOURCES)
    assert sum(report.answered_by.values()) == len(report.outcomes)


def test_a_conjunctive_match_is_attributed_to_the_lexical_view(tmp_path: Path) -> None:
    """Every query token present in one document — no fallback, no entity."""
    path = _tiny_fixture(
        tmp_path,
        {
            "version": 1,
            "corpus": [{"id": "alpha", "content": "kubernetes ingress annotation"}],
            "queries": [{"id": "q", "text": "kubernetes ingress", "relevant": ["alpha"]}],
        },
    )
    report = evaluate.run_eval(evaluate.load_fixture(path), tmp_path / "eval.db")
    assert report.outcomes[0].answered_by == evaluate.ANSWERED_BY_LEXICAL


def test_an_entity_bridge_is_attributed_to_the_relational_view(tmp_path: Path) -> None:
    """Shared identifier, no shared prose — the case view B exists for."""
    path = _tiny_fixture(
        tmp_path,
        {
            "version": 1,
            "corpus": [
                {
                    "id": "alpha",
                    "content": "build_match_expression trả rỗng khi câu hỏi toàn ký tự lạ",
                }
            ],
            "queries": [
                {"id": "q", "text": "build_match_expression hỏng chỗ nào", "relevant": ["alpha"]}
            ],
        },
    )
    report = evaluate.run_eval(evaluate.load_fixture(path), tmp_path / "eval.db")
    assert report.outcomes[0].answered_by == evaluate.ANSWERED_BY_RELATIONAL


def test_a_query_with_no_answer_is_attributed_to_nothing(tmp_path: Path) -> None:
    path = _tiny_fixture(
        tmp_path,
        {
            "version": 1,
            "corpus": [{"id": "alpha", "content": "kubernetes ingress annotation"}],
            "queries": [{"id": "q", "text": "zzzunmatchable", "relevant": []}],
        },
    )
    report = evaluate.run_eval(evaluate.load_fixture(path), tmp_path / "eval.db")
    assert report.outcomes[0].answered_by == evaluate.ANSWERED_BY_NONE


# --- determinism -------------------------------------------------------------


def test_two_runs_agree_exactly(tmp_path: Path) -> None:
    """The property the baseline assertion rests on."""
    fixture = evaluate.load_fixture()
    first = evaluate.run_eval(fixture, tmp_path / "one.db").summary()
    second = evaluate.run_eval(fixture, tmp_path / "two.db").summary()
    assert first == second


def test_query_order_does_not_change_the_report(tmp_path: Path) -> None:
    """With tracking on, an earlier recall would bump a later query's seen_count boost."""
    fixture = evaluate.load_fixture()
    forward = evaluate.run_eval(fixture, tmp_path / "forward.db")
    reversed_fixture = evaluate.Fixture(
        version=fixture.version,
        name=fixture.name,
        description=fixture.description,
        corpus=fixture.corpus,
        queries=tuple(reversed(fixture.queries)),
    )
    backward = evaluate.run_eval(reversed_fixture, tmp_path / "backward.db")

    assert backward.recall == forward.recall
    assert backward.mrr == forward.mrr
    forward_ranks = {out.query_id: out.first_gold_rank for out in forward.outcomes}
    backward_ranks = {out.query_id: out.first_gold_rank for out in backward.outcomes}
    assert backward_ranks == forward_ranks


def test_tracking_env_var_is_restored(tmp_path: Path) -> None:
    os.environ.pop(retriever.NO_TRACKING_ENV_VAR, None)
    evaluate.run_eval(evaluate.load_fixture(), tmp_path / "eval.db")
    assert retriever.NO_TRACKING_ENV_VAR not in os.environ


def test_tracking_env_var_keeps_a_preexisting_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(retriever.NO_TRACKING_ENV_VAR, "user-set")
    evaluate.run_eval(evaluate.load_fixture(), tmp_path / "eval.db")
    assert os.environ[retriever.NO_TRACKING_ENV_VAR] == "user-set"


def test_recalls_are_not_recorded_by_an_eval_run(tmp_path: Path) -> None:
    """A measurement must not leave a trail that changes the next measurement."""
    import sqlite3

    db_file = tmp_path / "eval.db"
    evaluate.run_eval(evaluate.load_fixture(), db_file)
    conn = sqlite3.connect(db_file)
    try:
        total = conn.execute("SELECT SUM(recalled_count) FROM memories").fetchone()[0]
    finally:
        conn.close()
    assert total == 0


# --- age_days ----------------------------------------------------------------


def test_age_days_backdates_the_row(tmp_path: Path) -> None:
    path = _tiny_fixture(
        tmp_path,
        {
            "version": 1,
            "corpus": [{"id": "old", "content": "kubernetes ingress annotation", "age_days": 400}],
            "queries": [{"id": "q", "text": "kubernetes ingress", "relevant": ["old"]}],
        },
    )
    import sqlite3

    db_file = tmp_path / "eval.db"
    evaluate.run_eval(evaluate.load_fixture(path), db_file)
    conn = sqlite3.connect(db_file)
    try:
        created = conn.execute("SELECT created_at FROM memories").fetchone()[0]
    finally:
        conn.close()
    assert created.startswith("2024-11-27")


def test_age_days_orders_two_otherwise_identical_matches(tmp_path: Path) -> None:
    """The fresher of two equally-matching rows must rank first."""
    path = _tiny_fixture(
        tmp_path,
        {
            "version": 1,
            "corpus": [
                {"id": "stale", "content": "kubernetes ingress annotation stale", "age_days": 300},
                {"id": "fresh", "content": "kubernetes ingress annotation fresh", "age_days": 1},
            ],
            "queries": [
                {"id": "q", "text": "kubernetes ingress annotation", "relevant": ["fresh"]}
            ],
        },
    )
    report = evaluate.run_eval(evaluate.load_fixture(path), tmp_path / "eval.db")
    assert report.outcomes[0].returned[0] == "fresh"


# --- validation of run_eval's own arguments ----------------------------------


def test_empty_k_values_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one cut-off"):
        evaluate.run_eval(evaluate.load_fixture(), tmp_path / "eval.db", k_values=())


def test_k_above_the_store_limit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must stay between"):
        evaluate.run_eval(evaluate.load_fixture(), tmp_path / "eval.db", k_values=(1, 99))


# --- the regression gate -----------------------------------------------------


def test_bundled_fixture_matches_the_recorded_baseline(tmp_path: Path) -> None:
    """Pin the measurement, in both directions.

    A metric that moved *up* fails this too, and that is deliberate: every number in this
    project is a recorded measurement (``docs/design_decisions.md``), so an unexplained
    improvement is as much a gap in the record as a regression. Rewrite the file with
    ``LOCALMEM_UPDATE_BASELINE=1 pytest tests/test_evaluate.py`` and put the diff in the
    CHANGELOG entry of whatever caused it.

    Per-query ranks are pinned alongside the aggregates because on a 14-query fixture one
    query is worth seven points of recall — two queries moving in opposite directions
    would leave every aggregate untouched.
    """
    summary = evaluate.run_eval(evaluate.load_fixture(), tmp_path / "eval.db").summary()
    if os.environ.get(UPDATE_BASELINE_ENV_VAR):
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        pytest.skip(f"baseline rewritten at {BASELINE_PATH}")

    recorded = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert summary == recorded


# --- CLI ---------------------------------------------------------------------


def test_cli_eval_prints_the_table() -> None:
    result = CliRunner().invoke(main, ["eval"])
    assert result.exit_code == 0
    assert "recall@1" in result.output
    assert "off-corpus silent" in result.output


def test_cli_eval_json_is_one_object() -> None:
    result = CliRunner().invoke(main, ["eval", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["fixture"] == "bilingual-v1"
    assert set(payload["recall"]) == {"@1", "@3", "@5"}
    assert payload["positive_queries"] == 45


def test_cli_eval_rejects_a_broken_fixture(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(main, ["eval", "--fixture", str(path)])
    assert result.exit_code != 0
    assert "not valid JSON" in result.output


def test_cli_eval_does_not_touch_the_user_database(db_path: Path) -> None:
    """The harness builds its own throwaway database; the real one stays absent."""
    result = CliRunner().invoke(main, ["eval", "--json"])
    assert result.exit_code == 0
    assert not db_path.exists()
