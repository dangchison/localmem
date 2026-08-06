"""Tests for ``localmem export`` / ``localmem restore``.

The two properties that matter: a round trip through a document reproduces the *recall*,
not just the rows, and both directions are idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmem import config, db, retriever, store, transfer
from localmem.cli import main

WORKSPACE = "proj"


def _open(path: Path) -> sqlite3.Connection:
    return db.open_database(path)


def _contents(conn: sqlite3.Connection) -> list[str]:
    return [
        row["content"]
        for row in conn.execute("SELECT content FROM memories ORDER BY id").fetchall()
    ]


def _recalled(conn: sqlite3.Connection, query: str, workspace: str) -> list[str]:
    return [hit.content for hit in retriever.retrieve(conn, query, workspace).results]


@pytest.fixture
def source(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A database with several kinds, workspaces, seen counts and a recall recorded."""
    store.add_memory(conn, "config_loader retries three times on timeout", WORKSPACE, "note", "cli")
    store.add_memory(conn, "config_loader caches nothing", WORKSPACE, "trace", None, "s1")
    store.add_memory(conn, "prefer small commits", WORKSPACE, "core")
    store.add_memory(conn, "reset the upload buffer before retrying", "global")
    for _ in range(3):
        store.add_memory(conn, "always run the migrations first", WORKSPACE)
    retriever.retrieve(conn, "timeout", WORKSPACE)
    return conn


# ------------------------------------------------------------------------------ export


def test_the_document_carries_every_column_of_every_row(
    source: sqlite3.Connection,
) -> None:
    document = transfer.export_document(source)
    records = document[transfer.MEMORIES_KEY]

    assert document["format"] == transfer.EXPORT_FORMAT
    assert document["format_version"] == transfer.EXPORT_FORMAT_VERSION
    assert len(records) == 5
    columns = {row["name"] for row in source.execute("PRAGMA table_info(memories)").fetchall()}
    assert set(records[0]) == columns
    assert {"content_hash", "seen_count", "recalled_count", "created_at", "session_id"} <= columns


def test_keywords_survive_a_round_trip_with_their_values_intact(
    tmp_path: Path,
) -> None:
    """Export is ``SELECT *``; restore is an explicit column list. Only the second can
    forget a column, and forgetting it loses data with no error whatsoever.

    ``test_the_document_carries_every_column_of_every_row`` cannot catch that: it derives
    the expected columns from ``PRAGMA table_info`` at runtime, so it passes for any new
    column whether or not restore actually carries it. This asserts the *values*.
    """
    origin = _open(tmp_path / "origin.db")
    try:
        store.add_memory(
            origin,
            "client_max_body_size mặc định 1m trong nginx",
            WORKSPACE,
            keywords=["413", "upload", "tải lên"],
        )
        store.add_memory(origin, "a memory with no keywords at all", WORKSPACE)
        document = transfer.export_document(origin)
    finally:
        origin.close()

    assert [record["keywords"] for record in document[transfer.MEMORIES_KEY]] == [
        "413 upload tải lên",
        None,
    ]

    target = _open(tmp_path / "target.db")
    try:
        transfer.restore(target, document[transfer.MEMORIES_KEY])
        stored = target.execute("SELECT keywords FROM memories ORDER BY id").fetchall()
        assert [row["keywords"] for row in stored] == ["413 upload tải lên", None]
        # Restored keywords are indexed, not merely stored: the recall works on the
        # other machine too, which is the only reason to carry them at all.
        assert [hit.content for hit in store.search_memories(target, "413", WORKSPACE)] == [
            "client_max_body_size mặc định 1m trong nginx"
        ]
    finally:
        target.close()


def test_a_hand_written_document_may_spell_keywords_as_a_list(tmp_path: Path) -> None:
    target = _open(tmp_path / "target.db")
    try:
        transfer.restore(
            target,
            [{"content": "nginx body size limit", "workspace": WORKSPACE, "keywords": ["413"]}],
        )
        assert [hit.content for hit in store.search_memories(target, "413", WORKSPACE)] == [
            "nginx body size limit"
        ]
    finally:
        target.close()


def test_the_document_holds_no_derived_or_transient_table(
    source: sqlite3.Connection,
) -> None:
    document = transfer.export_document(source)
    assert set(document) == {
        "format",
        "format_version",
        "localmem_version",
        "exported_at",
        "workspace",
        transfer.MEMORIES_KEY,
    }


def test_export_can_be_restricted_to_one_workspace(source: sqlite3.Connection) -> None:
    document = transfer.export_document(source, "global")
    assert [record["workspace"] for record in document[transfer.MEMORIES_KEY]] == ["global"]
    assert document["workspace"] == "global"


def test_export_rejects_a_blank_workspace(source: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        transfer.export_document(source, "   ")


def test_export_of_an_empty_database_is_a_valid_document(
    conn: sqlite3.Connection,
) -> None:
    document = transfer.export_document(conn)
    assert document[transfer.MEMORIES_KEY] == []
    assert json.loads(transfer.render(document)) == document


# ----------------------------------------------------------------------------- restore


def test_round_trip_into_an_empty_database_reproduces_the_recall(
    source: sqlite3.Connection, tmp_path: Path
) -> None:
    before = _recalled(source, "config_loader timeout", WORKSPACE)
    document = transfer.export_document(source)

    target = _open(tmp_path / "target.db")
    try:
        outcome = transfer.restore(target, document[transfer.MEMORIES_KEY])
        assert (outcome.total, outcome.added, outcome.merged) == (5, 5, 0)
        assert outcome.links_created > 0
        assert _recalled(target, "config_loader timeout", WORKSPACE) == before
        assert _contents(target) == _contents(source)
    finally:
        target.close()


def test_round_trip_preserves_the_columns_that_carry_history(
    source: sqlite3.Connection, tmp_path: Path
) -> None:
    document = transfer.export_document(source)
    target = _open(tmp_path / "target.db")
    try:
        transfer.restore(target, document[transfer.MEMORIES_KEY])
        query = (
            "SELECT content, workspace, kind, source, session_id, seen_count, created_at "
            "FROM memories ORDER BY content"
        )
        assert [tuple(row) for row in target.execute(query).fetchall()] == [
            tuple(row) for row in source.execute(query).fetchall()
        ]
    finally:
        target.close()


def test_the_global_tier_survives_the_trip(source: sqlite3.Connection, tmp_path: Path) -> None:
    document = transfer.export_document(source)
    target = _open(tmp_path / "target.db")
    try:
        transfer.restore(target, document[transfer.MEMORIES_KEY])
        # Restored into another machine, the shared row is still reachable from a repo.
        assert _recalled(target, "upload buffer", "some-other-repo") == [
            "reset the upload buffer before retrying"
        ]
    finally:
        target.close()


def test_restoring_twice_changes_nothing(source: sqlite3.Connection, tmp_path: Path) -> None:
    records = transfer.export_document(source)[transfer.MEMORIES_KEY]
    target = _open(tmp_path / "target.db")
    try:
        transfer.restore(target, records)
        snapshot = target.execute("SELECT * FROM memories ORDER BY id").fetchall()

        second = transfer.restore(target, records)

        assert (second.added, second.merged) == (0, 5)
        assert second.links_created == 0
        after = target.execute("SELECT * FROM memories ORDER BY id").fetchall()
        assert [tuple(row) for row in after] == [tuple(row) for row in snapshot]
    finally:
        target.close()


def test_restoring_into_a_database_that_already_has_data_merges(
    source: sqlite3.Connection, tmp_path: Path
) -> None:
    records = transfer.export_document(source)[transfer.MEMORIES_KEY]
    target = _open(tmp_path / "target.db")
    try:
        existing = store.add_memory(target, "config_loader caches nothing", WORKSPACE, "note")
        store.add_memory(target, "a memory only this machine has", WORKSPACE)

        outcome = transfer.restore(target, records)

        assert (outcome.added, outcome.merged) == (4, 1)
        assert target.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 6
        # The target's own row keeps its kind and its creation time; only seen_count moves.
        row = target.execute(
            "SELECT kind, seen_count FROM memories WHERE id = ?", (existing.id,)
        ).fetchone()
        assert row["kind"] == "note"
        assert row["seen_count"] == 1
    finally:
        target.close()


def test_a_conflicting_row_takes_the_larger_seen_count(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    for _ in range(4):
        store.add_memory(conn, "always run the migrations first", WORKSPACE)
    records = transfer.export_document(conn)[transfer.MEMORIES_KEY]

    target = _open(tmp_path / "target.db")
    try:
        store.add_memory(target, "always run the migrations first", WORKSPACE)
        transfer.restore(target, records)
        assert target.execute("SELECT seen_count FROM memories").fetchone()[0] == 4

        # And it never goes down: restoring a weaker document leaves the larger count.
        transfer.restore(
            target,
            [
                {
                    "content": "always run the migrations first",
                    "workspace": WORKSPACE,
                    "seen_count": 1,
                }
            ],
        )
        assert target.execute("SELECT seen_count FROM memories").fetchone()[0] == 4
    finally:
        target.close()


def test_restore_recomputes_the_content_hash(conn: sqlite3.Connection) -> None:
    """A hand-edited document cannot smuggle in a row tier-1 dedup will never match."""
    transfer.restore(
        conn,
        [{"content": "use pnpm not npm", "workspace": WORKSPACE, "content_hash": "nonsense"}],
    )
    merged = store.add_memory(conn, "- Use   PNPM not npm", WORKSPACE)
    assert merged.status == store.STATUS_DUPLICATE_MERGED
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_restore_fills_in_the_defaults_a_plain_add_would_have_used(
    conn: sqlite3.Connection,
) -> None:
    transfer.restore(conn, [{"content": "a minimal record", "workspace": WORKSPACE}])
    row = conn.execute("SELECT * FROM memories").fetchone()
    assert row["kind"] == store.DEFAULT_KIND
    assert (row["source"], row["session_id"], row["last_recalled_at"]) == (None, None, None)
    assert (row["seen_count"], row["recalled_count"]) == (1, 0)
    assert row["created_at"] == row["updated_at"]


def test_restoring_nothing_is_not_an_error(conn: sqlite3.Connection) -> None:
    outcome = transfer.restore(conn, [])
    assert (outcome.total, outcome.added, outcome.merged, outcome.links_created) == (0, 0, 0, 0)


def test_a_restored_row_is_searchable(source: sqlite3.Connection, tmp_path: Path) -> None:
    """The FTS index is maintained by the schema's own trigger, so it needs no rebuild."""
    records = transfer.export_document(source)[transfer.MEMORIES_KEY]
    target = _open(tmp_path / "target.db")
    try:
        transfer.restore(target, records)
        assert store.search_memories(target, "config_loader", WORKSPACE)
    finally:
        target.close()


# ------------------------------------------------------------------- document validation


def test_a_non_localmem_json_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.json"
    path.write_text('{"memories": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a localmem export document"):
        transfer.read_document(path)


def test_invalid_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        transfer.read_document(path)


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"workspace": "ws"}, "no content"),
        ({"content": "   ", "workspace": "ws"}, "no content"),
        ({"content": "a fact"}, "no workspace"),
        ({"content": "a fact", "workspace": "  "}, "no workspace"),
        ("not an object", "where a memory object was expected"),
    ],
)
def test_a_malformed_record_is_refused(tmp_path: Path, record: object, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"format": transfer.EXPORT_FORMAT, "memories": [record]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=message):
        transfer.read_document(path)


# ------------------------------------------------------------------------------- CLI


def test_cli_export_writes_json_to_stdout(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "use pnpm not npm", "-w", WORKSPACE])
    result = runner.invoke(main, ["export"])

    assert result.exit_code == 0
    document = json.loads(result.output)
    assert [record["content"] for record in document["memories"]] == ["use pnpm not npm"]


def test_cli_export_to_a_file_reports_what_it_wrote(db_path: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "use pnpm not npm", "-w", WORKSPACE])
    target = tmp_path / "backup.json"

    result = runner.invoke(main, ["export", "-o", str(target)])

    assert result.exit_code == 0
    assert f"exported 1 memories to {target}" in result.output
    assert json.loads(target.read_text(encoding="utf-8"))["memories"][0]["content"] == (
        "use pnpm not npm"
    )


def test_cli_export_restore_round_trip_between_two_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented backup workflow, end to end, through the command line only."""
    runner = CliRunner()
    first = tmp_path / "machine-a.db"
    second = tmp_path / "machine-b.db"
    backup = tmp_path / "backup.json"

    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(first))
    runner.invoke(main, ["add", "config_loader retries on timeout", "-w", WORKSPACE])
    runner.invoke(main, ["add", "prefer pnpm everywhere", "-w", "global", "--kind", "core"])
    assert runner.invoke(main, ["export", "-o", str(backup)]).exit_code == 0

    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(second))
    restored = runner.invoke(main, ["restore", str(backup)])
    assert restored.exit_code == 0
    assert "restored 2 memories — 2 new, 0 merged into existing" in restored.output

    found = runner.invoke(main, ["search", "config_loader", "-w", WORKSPACE])
    assert "config_loader retries on timeout" in found.output
    assert "prefer pnpm everywhere" in found.output  # the core tier came across too

    again = runner.invoke(main, ["restore", str(backup)])
    assert "0 new, 2 merged into existing" in again.output


def test_cli_restore_refuses_a_file_that_is_not_an_export(db_path: Path, tmp_path: Path) -> None:
    path = tmp_path / "notes.json"
    path.write_text("[]", encoding="utf-8")
    result = CliRunner().invoke(main, ["restore", str(path)])
    assert result.exit_code != 0
    assert "not a localmem export document" in result.output


def test_a_document_without_a_memories_array_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"format": transfer.EXPORT_FORMAT}), encoding="utf-8")
    with pytest.raises(ValueError, match="no 'memories' array"):
        transfer.read_document(path)


def test_supersede_links_are_exported_but_deliberately_not_restored(tmp_path: Path) -> None:
    """v0.4.0 C4, stated in the docs and pinned here: the link is local to a database.

    ``superseded_by`` holds a row id, and ids are reassigned on restore — carrying one
    across would point the retraction at whichever memory happens to hold that id in the
    target. Both memories travel; the link does not, so the corrected memory arrives
    ranking like any other row and the link is re-declared with ``--supersedes``.
    """
    origin = _open(tmp_path / "origin.db")
    try:
        wrong = store.add_memory(origin, "the leak is in the resizer", WORKSPACE)
        store.add_memory(origin, "the pool was exhausted", WORKSPACE, supersedes=[wrong.id])
        document = transfer.export_document(origin)
    finally:
        origin.close()

    # It is in the document — export is `SELECT *`, and provenance is worth carrying.
    assert [record["superseded_by"] for record in document[transfer.MEMORIES_KEY]] == [2, None]

    target = _open(tmp_path / "target.db")
    try:
        # A row already present, so the ids in the document mean something else here.
        store.add_memory(target, "an unrelated memory that takes id 1", WORKSPACE)
        transfer.restore(target, document[transfer.MEMORIES_KEY])
        links = target.execute("SELECT superseded_by FROM memories ORDER BY id").fetchall()
    finally:
        target.close()

    assert [row["superseded_by"] for row in links] == [None, None, None]


def test_cli_export_reports_a_write_failure_instead_of_a_traceback(
    db_path: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "use pnpm not npm", "-w", WORKSPACE])
    # `export` writes the file it is given; it does not create directories for it.
    result = runner.invoke(main, ["export", "-o", str(tmp_path / "nope" / "backup.json")])
    assert result.exit_code != 0
    assert "cannot write" in result.output


def test_cli_restore_reports_an_unreadable_file_instead_of_a_traceback(
    db_path: Path, tmp_path: Path
) -> None:
    unreadable = tmp_path / "not-utf8.json"
    unreadable.write_bytes(b"\xff\xfe not utf-8 at all")
    result = CliRunner().invoke(main, ["restore", str(unreadable)])
    assert result.exit_code != 0
    assert "cannot read" in result.output


def test_cli_restore_rejects_a_missing_file(db_path: Path) -> None:
    assert CliRunner().invoke(main, ["restore", "does-not-exist.json"]).exit_code != 0
