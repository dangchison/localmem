"""Retrieval quality, measured through the production read path.

Every threshold in localmem — the keyword column weight, the capture Jaccard gate, the
supersede penalty — was fitted against a hand-built fixture that lived in a scratchpad
and did not survive the milestone that produced it (``docs/design_decisions.md`` §44,
§45, and the Gate 0 report in ``.corp/localmem-v1/gate0-v030-result.md``). This module
is the instrument those measurements needed: a versioned corpus, a runner that ingests
it through :func:`localmem.store.add_memory` and queries it through
:func:`localmem.retriever.retrieve`, and a report a test can pin.

Nothing here re-implements retrieval. Measuring through a path other than the one the
numbers inform would be worse than not measuring at all — the same rule
:func:`localmem.audit._trace_similarity` follows for the capture gate.

Two properties make the report pinnable to the byte:

* **the clock is fixed.** Documents are backdated relative to :data:`EVAL_EPOCH` and
  every retrieval is measured against it, so the recency term is a constant of the
  fixture rather than of the day the suite runs;
* **tracking is off.** A recall bumps ``recalled_count``, which feeds
  :func:`localmem.retriever.seen_count_boost`; left on, the score of query *n* would
  depend on queries 1..*n-1*. :func:`disabled_tracking` removes that coupling.

The metrics are deliberately rank-based — which document came back where — and never
raw bm25 scores, whose magnitudes sit around 1e-06 and carry no portable meaning
(``docs/design_decisions.md`` §8).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

from localmem import db, retriever, store

#: The bundled fixture, shipped inside the package so an installed wheel can run
#: ``localmem eval`` with no arguments. Read the same way :mod:`localmem.db` reads
#: ``schema.sql``, which needs no ``__init__.py`` in the data directory.
FIXTURE_DIRECTORY = "evaldata"
DEFAULT_FIXTURE_NAME = "bilingual_v1.json"

#: The only fixture layout this module understands. A fixture that grows a field keeps
#: this version; one that changes the meaning of an existing field bumps it, so an old
#: baseline can never be silently compared against a new corpus.
SUPPORTED_FIXTURE_VERSION = 1

#: Where the fixture is loaded. Never the user's own workspaces: the runner builds a
#: throwaway database and this name only has to be stable, not unique.
EVAL_WORKSPACE = "eval"

#: The instant recency is measured against, and the point ``age_days`` counts back from.
#: A fixed value is what lets the baseline be an equality assertion rather than a range.
EVAL_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: The cut-offs reported by default. ``retrieve`` is called once per query at
#: ``max(...)`` and the smaller cut-offs are read off that one ranking.
DEFAULT_K_VALUES: tuple[int, ...] = (1, 3, 5)

_RECALL_DIGITS = 4
# Stages the two columns a fixture controls but the write path derives from the clock and
# from history. Deliberately not `UPDATE OF content, keywords`, so the `mem_au` trigger
# does not fire and the FTS index is left alone (``docs/design_decisions.md`` §53).
_STAGE_ROW_SQL = "UPDATE memories SET created_at = ?, seen_count = ? WHERE id = ?"


@dataclass(frozen=True)
class FixtureDoc:
    """One corpus document, as an agent would have written it.

    Attributes:
        id: the fixture-local identifier queries refer to; never a database row id.
        content: the memory text, stored verbatim.
        kind: ``note``, ``trace`` or ``lesson`` — the kinds an agent may write.
        keywords: the agent's alternative wordings, indexed as the second FTS5 column.
        age_days: how far before :data:`EVAL_EPOCH` the row is dated.
        seen_count: how many times this fact was stored. Staged with a direct ``UPDATE``,
            exactly as ``age_days`` is, rather than by writing the same content twice —
            a second write would merge onto the same row and leave two fixture ids
            pointing at one memory, which the id mapping cannot represent. Above 1 it is
            the only thing that makes :func:`localmem.retriever.seen_count_boost`
            non-zero, since ``ln(1)`` is 0 and the weight would multiply nothing.
        supersedes: fixture ids this document corrects. They must appear **earlier** in
            the corpus, because a supersede link is resolved against rows that already
            exist — the same constraint :func:`localmem.store.add_memory` enforces
            against real ids.
    """

    id: str
    content: str
    kind: str
    keywords: tuple[str, ...]
    age_days: int
    seen_count: int
    supersedes: tuple[str, ...]


@dataclass(frozen=True)
class FixtureQuery:
    """One graded query.

    An empty ``relevant`` marks an **off-corpus** query: one whose answer is genuinely
    not stored. Those are not failures to recall — they are the noise side of the
    measurement, and the reason Gate 0 rejected a semantic view that scored recall well
    (``.corp/localmem-v1/gate0-v030-result.md`` §3).
    """

    id: str
    text: str
    relevant: tuple[str, ...]


@dataclass(frozen=True)
class Fixture:
    """A versioned corpus and its graded queries."""

    version: int
    name: str
    description: str
    corpus: tuple[FixtureDoc, ...]
    queries: tuple[FixtureQuery, ...]

    @property
    def positives(self) -> tuple[FixtureQuery, ...]:
        """The queries with at least one relevant document."""
        return tuple(query for query in self.queries if query.relevant)

    @property
    def negatives(self) -> tuple[FixtureQuery, ...]:
        """The off-corpus queries, which should ideally return nothing."""
        return tuple(query for query in self.queries if not query.relevant)


#: What answered a query. ``both`` is the only value under which the fusion weights
#: change any ranking at all — with one view empty, its weight multiplies a column of
#: zeros and the other view's weight is a constant factor that no rank-based metric can
#: see. A report where ``both`` is 0 is therefore *not* evidence about
#: :data:`localmem.retriever.LEXICAL_WEIGHT` or its siblings, and says so.
ANSWERED_BY_NONE = "none"
ANSWERED_BY_LEXICAL = "lexical"
ANSWERED_BY_RELATIONAL = "relational"
ANSWERED_BY_BOTH = "both"
ANSWERED_BY_FALLBACK = "fallback"

ANSWER_SOURCES = (
    ANSWERED_BY_BOTH,
    ANSWERED_BY_LEXICAL,
    ANSWERED_BY_RELATIONAL,
    ANSWERED_BY_FALLBACK,
    ANSWERED_BY_NONE,
)


@dataclass(frozen=True)
class QueryOutcome:
    """What one query actually returned.

    Attributes:
        returned: the fixture ids that came back, best first.
        first_gold_rank: 1-based rank of the first relevant document, or ``None`` if no
            relevant document appeared at all. Always ``None`` for an off-corpus query.
        from_fallback: the disjunctive last-resort pass answered this query, which is a
            weaker claim than an ordinary hit.
        answered_by: which view produced the ranking — one of :data:`ANSWER_SOURCES`.
    """

    query_id: str
    text: str
    gold: tuple[str, ...]
    returned: tuple[str, ...]
    first_gold_rank: int | None
    from_fallback: bool
    answered_by: str

    @property
    def is_negative(self) -> bool:
        """Whether this query has no relevant document in the corpus."""
        return not self.gold

    @property
    def silent(self) -> bool:
        """Whether the query returned nothing — the desired answer for a negative."""
        return not self.returned


@dataclass(frozen=True)
class EvalReport:
    """Aggregate quality of one fixture run.

    Attributes:
        recall: cut-off → share of positive queries with a relevant document in the top
            ``k``.
        mrr: mean reciprocal rank over positive queries; a query that never returns its
            document contributes 0.
        off_corpus_silent: how many off-corpus queries returned nothing at all. This is
            the metric that stops a recall improvement from being bought with noise.
        answered_by: how many queries each view answered, keyed by
            :data:`ANSWER_SOURCES`. It states the coverage of the run, so a reader can
            see which code paths these numbers are evidence about — and which they are
            silent on.
    """

    fixture_name: str
    fixture_version: int
    positive_queries: int
    negative_queries: int
    recall: Mapping[int, float]
    mrr: float
    off_corpus_silent: int
    answered_by: Mapping[str, int]
    outcomes: tuple[QueryOutcome, ...]

    def summary(self) -> dict[str, object]:
        """Return the JSON-serializable form a baseline is pinned against.

        Three levels of detail, each catching what the one above it hides:

        * the aggregates, which are the answer to "is retrieval better";
        * the per-query gold rank, because two queries moving in opposite directions
          leave every aggregate untouched;
        * the **full returned ranking**, because a change can reorder everything below
          the gold row — which is most of what the system returns — without moving a
          single gold rank. Measured: with only ranks pinned, four of eight perturbed
          constants slipped through, including the supersede penalty and the
          ``seen_count`` boost.

        Every field is an id or a position, never a bm25 score, so the whole document
        stays portable across SQLite builds (§53).
        """
        return {
            "fixture": self.fixture_name,
            "fixture_version": self.fixture_version,
            "positive_queries": self.positive_queries,
            "negative_queries": self.negative_queries,
            "recall": {f"@{k}": value for k, value in sorted(self.recall.items())},
            "mrr": self.mrr,
            "off_corpus_silent": self.off_corpus_silent,
            "answered_by": {source: self.answered_by[source] for source in ANSWER_SOURCES},
            "queries": {
                outcome.query_id: {
                    "first_gold_rank": outcome.first_gold_rank,
                    "silent": outcome.silent,
                    "answered_by": outcome.answered_by,
                    "returned": list(outcome.returned),
                }
                for outcome in self.outcomes
            },
        }


def default_fixture_path() -> Path:
    """Return the path of the bundled fixture."""
    bundled = resources.files("localmem").joinpath(FIXTURE_DIRECTORY).joinpath(DEFAULT_FIXTURE_NAME)
    return Path(str(bundled))


def load_fixture(path: Path | None = None) -> Fixture:
    """Read and validate a fixture file.

    Args:
        path: an explicit fixture; defaults to the bundled one.

    Raises:
        ValueError: if the file is not valid JSON, declares an unsupported version, is
            missing a required field, or refers to a document id that does not exist.
    """
    target = path if path is not None else default_fixture_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{target} must hold a JSON object, got {type(raw).__name__}")

    version = raw.get("version")
    if version != SUPPORTED_FIXTURE_VERSION:
        raise ValueError(
            f"{target} declares fixture version {version!r}; this localmem understands "
            f"version {SUPPORTED_FIXTURE_VERSION}. A baseline recorded against one "
            "version says nothing about another."
        )

    corpus = tuple(_read_doc(entry, target) for entry in _read_list(raw, "corpus", target))
    known = {doc.id for doc in corpus}
    if len(known) != len(corpus):
        raise ValueError(f"{target} repeats a corpus id")
    _check_supersede_order(corpus, target)

    queries = tuple(
        _read_query(entry, known, target) for entry in _read_list(raw, "queries", target)
    )
    if len({query.id for query in queries}) != len(queries):
        raise ValueError(f"{target} repeats a query id")

    return Fixture(
        version=version,
        name=str(raw.get("name", target.stem)),
        description=str(raw.get("description", "")),
        corpus=corpus,
        queries=queries,
    )


@contextmanager
def disabled_tracking() -> Iterator[None]:
    """Turn recall tracking off for the duration of the block, then restore it.

    Without this the ``recalled_count`` bump of query *n* changes the
    :func:`localmem.retriever.seen_count_boost` seen by query *n+1*, and the report
    depends on the order the fixture happens to list its queries.
    """
    previous = os.environ.get(retriever.NO_TRACKING_ENV_VAR)
    os.environ[retriever.NO_TRACKING_ENV_VAR] = "1"
    try:
        yield
    finally:
        if previous is None:
            del os.environ[retriever.NO_TRACKING_ENV_VAR]
        else:
            os.environ[retriever.NO_TRACKING_ENV_VAR] = previous


def run_eval(
    fixture: Fixture,
    db_path: Path,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
) -> EvalReport:
    """Load ``fixture`` into a database at ``db_path`` and measure every query.

    ``db_path`` should name a file that does not exist yet — the caller owns its
    lifetime, which is what lets a test keep the database for inspection and the CLI
    throw it away.

    Args:
        fixture: the corpus and queries to run.
        db_path: where to build the throwaway database.
        k_values: the cut-offs to report; retrieval is run once at the largest.

    Raises:
        ValueError: if ``k_values`` is empty or asks for more results than
            :data:`localmem.store.MAX_LIMIT`.
    """
    cutoffs = tuple(sorted(set(k_values)))
    if not cutoffs:
        raise ValueError("k_values must name at least one cut-off")
    depth = cutoffs[-1]
    if not store.MIN_LIMIT <= depth <= store.MAX_LIMIT:
        raise ValueError(
            f"k_values must stay between {store.MIN_LIMIT} and {store.MAX_LIMIT}, got {depth}"
        )

    conn = db.open_database(db_path)
    try:
        by_row_id = _ingest(conn, fixture)
        with disabled_tracking():
            outcomes = tuple(_run_query(conn, query, by_row_id, depth) for query in fixture.queries)
    finally:
        conn.close()

    positives = tuple(outcome for outcome in outcomes if not outcome.is_negative)
    negatives = tuple(outcome for outcome in outcomes if outcome.is_negative)
    return EvalReport(
        fixture_name=fixture.name,
        fixture_version=fixture.version,
        positive_queries=len(positives),
        negative_queries=len(negatives),
        recall={k: _recall_at(positives, k) for k in cutoffs},
        mrr=_mean_reciprocal_rank(positives),
        off_corpus_silent=sum(1 for outcome in negatives if outcome.silent),
        answered_by={
            source: sum(1 for outcome in outcomes if outcome.answered_by == source)
            for source in ANSWER_SOURCES
        },
        outcomes=outcomes,
    )


def _ingest(conn: sqlite3.Connection, fixture: Fixture) -> dict[int, str]:
    """Write every document through the production write path; return row id → doc id."""
    by_row_id: dict[int, str] = {}
    row_ids: dict[str, int] = {}
    for doc in fixture.corpus:
        result = store.add_memory(
            conn,
            doc.content,
            EVAL_WORKSPACE,
            kind=doc.kind,
            source="eval",
            keywords=doc.keywords,
            supersedes=[row_ids[target] for target in doc.supersedes] or None,
        )
        by_row_id[result.id] = doc.id
        row_ids[doc.id] = result.id
        created = EVAL_EPOCH - timedelta(days=doc.age_days)
        with db.transaction(conn):
            conn.execute(
                _STAGE_ROW_SQL,
                (
                    created.strftime(retriever.SQLITE_TIMESTAMP_FORMAT),
                    doc.seen_count,
                    result.id,
                ),
            )
    return by_row_id


def _run_query(
    conn: sqlite3.Connection,
    query: FixtureQuery,
    by_row_id: Mapping[int, str],
    depth: int,
) -> QueryOutcome:
    """Retrieve one query and translate row ids back into fixture ids."""
    outcome = retriever.retrieve(conn, query.text, EVAL_WORKSPACE, k=depth, now=EVAL_EPOCH)
    returned = tuple(by_row_id[memory.id] for memory in outcome.results)
    gold = set(query.relevant)
    rank = next(
        (position for position, doc_id in enumerate(returned, start=1) if doc_id in gold),
        None,
    )
    return QueryOutcome(
        query_id=query.id,
        text=query.text,
        gold=query.relevant,
        returned=returned,
        first_gold_rank=rank,
        from_fallback=any(memory.from_fallback for memory in outcome.results),
        answered_by=_answer_source(outcome.results),
    )


def _answer_source(results: Sequence[retriever.RetrievedMemory]) -> str:
    """Name the view that produced ``results``.

    Read off the per-result provenance the retriever already carries: ``lexical_score``
    and ``relational_score`` are ``None`` exactly when that view did not see the row.
    """
    if not results:
        return ANSWERED_BY_NONE
    if any(memory.from_fallback for memory in results):
        return ANSWERED_BY_FALLBACK
    lexical = any(memory.lexical_score is not None for memory in results)
    relational = any(memory.relational_score is not None for memory in results)
    if lexical and relational:
        return ANSWERED_BY_BOTH
    if lexical:
        return ANSWERED_BY_LEXICAL
    return ANSWERED_BY_RELATIONAL


def _recall_at(positives: Sequence[QueryOutcome], k: int) -> float:
    """Return the share of positive queries answered within the top ``k``."""
    if not positives:
        return 0.0
    hits = sum(
        1
        for outcome in positives
        if outcome.first_gold_rank is not None and outcome.first_gold_rank <= k
    )
    return round(hits / len(positives), _RECALL_DIGITS)


def _mean_reciprocal_rank(positives: Sequence[QueryOutcome]) -> float:
    """Return the mean of ``1/rank``, counting a total miss as 0."""
    if not positives:
        return 0.0
    ranks = [
        outcome.first_gold_rank for outcome in positives if outcome.first_gold_rank is not None
    ]
    return round(sum(1.0 / rank for rank in ranks) / len(positives), _RECALL_DIGITS)


def _check_supersede_order(corpus: Sequence[FixtureDoc], target: Path) -> None:
    """Refuse a corpus whose supersede links point forward or nowhere."""
    seen: set[str] = set()
    for doc in corpus:
        for superseded in doc.supersedes:
            if superseded not in seen:
                raise ValueError(
                    f"{target}: {doc.id!r} supersedes {superseded!r}, which is not a "
                    "document defined before it"
                )
        seen.add(doc.id)


def _read_list(raw: Mapping[str, object], key: str, target: Path) -> list[object]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{target} is missing a {key!r} list")
    return value


def _read_doc(entry: object, target: Path) -> FixtureDoc:
    if not isinstance(entry, dict):
        raise ValueError(f"{target} holds a corpus entry that is not an object")
    try:
        doc_id = str(entry["id"])
        content = str(entry["content"])
    except KeyError as exc:
        raise ValueError(f"{target} holds a corpus entry missing {exc}") from exc
    keywords = entry.get("keywords", [])
    if not isinstance(keywords, list):
        raise ValueError(f"{target}: keywords of {doc_id!r} must be a list")
    supersedes = entry.get("supersedes", [])
    if not isinstance(supersedes, list):
        raise ValueError(f"{target}: supersedes of {doc_id!r} must be a list")
    return FixtureDoc(
        id=doc_id,
        content=content,
        kind=str(entry.get("kind", store.DEFAULT_KIND)),
        keywords=tuple(str(keyword) for keyword in keywords),
        age_days=int(entry.get("age_days", 0)),
        seen_count=int(entry.get("seen_count", 1)),
        supersedes=tuple(str(doc_id) for doc_id in supersedes),
    )


def _read_query(entry: object, known: set[str], target: Path) -> FixtureQuery:
    if not isinstance(entry, dict):
        raise ValueError(f"{target} holds a query entry that is not an object")
    try:
        query_id = str(entry["id"])
        text = str(entry["text"])
    except KeyError as exc:
        raise ValueError(f"{target} holds a query entry missing {exc}") from exc
    relevant = entry.get("relevant", [])
    if not isinstance(relevant, list):
        raise ValueError(f"{target}: relevant of {query_id!r} must be a list")
    unknown = sorted(str(doc_id) for doc_id in relevant if str(doc_id) not in known)
    if unknown:
        raise ValueError(f"{target}: query {query_id!r} names unknown documents {unknown}")
    return FixtureQuery(
        id=query_id,
        text=text,
        relevant=tuple(str(doc_id) for doc_id in relevant),
    )
