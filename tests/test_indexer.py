from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmem import cli, db, indexer, store
from localmem.indexer import EntityClass

WORKSPACE = "m2"


def named(content: str) -> dict[str, EntityClass]:
    return {entity.norm_name: entity.entity_class for entity in indexer.extract_entities(content)}


def weights(content: str) -> dict[str, float]:
    return {entity.norm_name: entity.weight for entity in indexer.extract_entities(content)}


def entity_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"])


def link_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM memory_entities").fetchone()["n"])


def graph_size(conn: sqlite3.Connection) -> tuple[int, int]:
    return entity_count(conn), link_count(conn)


def links_of(conn: sqlite3.Connection, memory_id: int) -> dict[str, float]:
    rows = conn.execute(
        "SELECT e.norm_name AS norm_name, me.weight AS weight "
        "  FROM memory_entities AS me JOIN entities AS e ON e.id = me.entity_id "
        " WHERE me.memory_id = ?",
        (memory_id,),
    ).fetchall()
    return {row["norm_name"]: float(row["weight"]) for row in rows}


# --------------------------------------------------------------------------- extraction


def test_url_wins_over_file_path() -> None:
    """AC6: the whole URL is one entity and its path does not leak into FILE_PATH."""
    entities = indexer.extract_entities("see https://example.com/path/to/file.py here")
    assert len(entities) == 1
    assert entities[0].entity_class is EntityClass.URL
    assert entities[0].norm_name == "https://example.com/path/to/file.py"


def test_quoted_string_does_not_mask_its_interior() -> None:
    """AC7: the quoted span and the identifier inside it are both extracted."""
    found = {
        (entity.entity_class, entity.name)
        for entity in indexer.extract_entities('use "ConfigLoader" now')
    }
    assert (EntityClass.QUOTED_STRING, '"ConfigLoader"') in found
    assert (EntityClass.CAMEL_CASE, "ConfigLoader") in found


def test_quoted_sentence_keeps_both_forms() -> None:
    entities = named('he said "use ConfigLoader" today')
    assert entities["use configloader"] is EntityClass.QUOTED_STRING
    assert entities["configloader"] is EntityClass.CAMEL_CASE


def test_whitespace_only_quote_is_dropped() -> None:
    assert indexer.extract_entities('he said "   " and left') == []


def test_prose_abbreviation_is_not_a_file_path() -> None:
    """AC8: the frozen `{2,6}` extension length keeps `e.g` out."""
    assert indexer.extract_entities("see e.g. the docs") == []


def test_real_file_name_is_still_a_file_path() -> None:
    assert "main.py" in named("open main.py please")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("edit src/main.py now", "src/main.py"),
        ("localmem/agents/kiro.py here", "localmem/agents/kiro.py"),
        ("see ./scripts/run.sh ok", "./scripts/run.sh"),
        ("open /etc/hosts", "/etc/hosts"),
        ("a ../up/one.txt b", "../up/one.txt"),
        ("read main.py", "main.py"),
        ("trailing src/main.py, and more", "src/main.py"),
    ],
)
def test_file_path_keeps_every_segment(content: str, expected: str) -> None:
    """The leading directory is part of the match, and trailing punctuation is not."""
    assert named(content) == {expected: EntityClass.FILE_PATH}


def test_mention_beats_the_identifier_classes() -> None:
    assert named("ping @build_bot now") == {"@build_bot": EntityClass.MENTION}


def test_snake_case_and_camel_case() -> None:
    entities = named("resolve_db_path calls MyHelper")
    assert entities["resolve_db_path"] is EntityClass.SNAKE_CASE
    assert entities["myhelper"] is EntityClass.CAMEL_CASE


def test_acronym_noise_is_accepted() -> None:
    # DD-12: Vietnamese abbreviations become ACRONYM entities; documented, not suppressed.
    assert named("UBND đã duyệt") == {"ubnd": EntityClass.ACRONYM}


def test_shouty_english_also_produces_acronyms() -> None:
    assert set(named("THIS IS URGENT")) == {"this", "is", "urgent"}


def test_plain_prose_yields_nothing() -> None:
    assert indexer.extract_entities("hello world") == []


def test_empty_content_yields_nothing() -> None:
    assert indexer.extract_entities("") == []


def test_norm_name_is_lowercased_and_name_keeps_the_raw_form() -> None:
    (entity,) = indexer.extract_entities("MyClass MyClass")
    assert entity.name == "MyClass"
    assert entity.norm_name == "myclass"


def test_repeated_entity_collapses_to_one_with_weight_one() -> None:
    """AC9 (extraction half): 200 repeats stay a single entity."""
    entities = indexer.extract_entities("MyClass " * 200)
    assert len(entities) == 1
    assert entities[0].count == 200
    assert entities[0].weight == 1.0


def test_weight_counts_the_entity_not_its_class() -> None:
    """AC10: the regression test for the rejected per-class count formula."""
    assert weights("MyClass calls OtherThing once, MyClass again") == {
        "myclass": 1.0,
        "otherthing": 0.5,
    }


def test_extraction_is_capped_at_max_extract_chars() -> None:
    tail = "TailClass"
    content = "x" * indexer.MAX_EXTRACT_CHARS + " " + tail
    assert "tailclass" not in named(content)


def test_adversarial_path_string_extracts_quickly() -> None:
    """AC16: the char cap bounds worst-case backtracking."""
    content = "./a/b." * 8500
    assert len(content) > 50_000
    started = time.perf_counter()
    indexer.extract_entities(content)
    assert time.perf_counter() - started < 2.0


def test_cardinality_cap_is_deterministic() -> None:
    """AC11: 60 identifiers, 50 kept, same set every run."""
    content = " ".join(f"ident_a{index:02d}" for index in range(60))
    first = [entity.norm_name for entity in indexer.extract_entities(content)]
    second = [entity.norm_name for entity in indexer.extract_entities(content)]
    assert len(first) == indexer.MAX_ENTITIES_PER_MEMORY
    assert first == second
    assert set(first) == {f"ident_a{index:02d}" for index in range(50)}


def test_cap_keeps_the_highest_counts_first() -> None:
    content = "top_one " * 3 + " ".join(f"ident_a{index:02d}" for index in range(60))
    entities = indexer.extract_entities(content)
    assert entities[0].norm_name == "top_one"
    assert entities[0].weight == 1.0
    assert all(entity.weight == pytest.approx(1 / 3) for entity in entities[1:])


# ------------------------------------------------------------------------------ linking


def test_add_memory_writes_links_in_the_same_transaction(conn: sqlite3.Connection) -> None:
    result = store.add_memory(conn, "load src/main.py with MyClass", WORKSPACE)
    assert set(links_of(conn, result.id)) == {"src/main.py", "myclass"}


def test_add_memory_with_no_entities_writes_no_links(conn: sqlite3.Connection) -> None:
    result = store.add_memory(conn, "hello world", WORKSPACE)
    assert links_of(conn, result.id) == {}
    assert entity_count(conn) == 0


def test_duplicate_merge_does_not_reindex(conn: sqlite3.Connection) -> None:
    """AC5 / DD-8: the merge path leaves the entity graph untouched."""
    store.add_memory(conn, "load src/main.py with MyClass", WORKSPACE)
    before = graph_size(conn)
    merged = store.add_memory(conn, "load src/main.py with MyClass", WORKSPACE)
    assert merged.status == store.STATUS_DUPLICATE_MERGED
    assert graph_size(conn) == before


def test_entities_are_shared_across_memories(conn: sqlite3.Connection) -> None:
    first = store.add_memory(conn, "MyClass lives here", WORKSPACE)
    second = store.add_memory(conn, "MyClass lives there too", WORKSPACE)
    assert entity_count(conn) == 1
    assert "myclass" in links_of(conn, first.id)
    assert "myclass" in links_of(conn, second.id)


def test_entity_display_name_keeps_the_first_spelling(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "MYCLASS is shouty", WORKSPACE)
    store.add_memory(conn, "myclass is quiet", WORKSPACE)
    row = conn.execute("SELECT name FROM entities WHERE norm_name = 'myclass'").fetchone()
    assert row["name"] == "MYCLASS"


def test_index_memory_refreshes_weights_on_reindex(conn: sqlite3.Connection) -> None:
    result = store.add_memory(conn, "MyClass and OtherThing", WORKSPACE)
    with db.transaction(conn):
        written = indexer.index_memory(conn, result.id, "MyClass MyClass OtherThing")
    assert written == 2
    assert links_of(conn, result.id) == {"myclass": 1.0, "otherthing": 0.5}


def test_index_memory_never_opens_a_transaction(conn: sqlite3.Connection) -> None:
    """DD-1: the caller owns the transaction, so an outer rollback discards the links."""
    result = store.add_memory(conn, "hello world", WORKSPACE)
    with pytest.raises(RuntimeError, match="rollback me"), db.transaction(conn):
        indexer.index_memory(conn, result.id, "MyClass here")
        raise RuntimeError("rollback me")
    assert entity_count(conn) == 0
    assert link_count(conn) == 0


def test_repeated_entity_yields_exactly_one_link(conn: sqlite3.Connection) -> None:
    """AC9: 200 repeats become one link weighted 1.0."""
    result = store.add_memory(conn, "MyClass " * 200, WORKSPACE)
    assert links_of(conn, result.id) == {"myclass": 1.0}


def test_weights_reach_the_database(conn: sqlite3.Connection) -> None:
    """AC10 end to end."""
    result = store.add_memory(conn, "MyClass calls OtherThing once, MyClass again", WORKSPACE)
    assert links_of(conn, result.id) == {"myclass": 1.0, "otherthing": 0.5}


def test_cardinality_cap_reaches_the_database(conn: sqlite3.Connection) -> None:
    """AC11 end to end."""
    content = " ".join(f"ident_a{index:02d}" for index in range(60))
    result = store.add_memory(conn, content, WORKSPACE)
    assert len(links_of(conn, result.id)) == indexer.MAX_ENTITIES_PER_MEMORY


def test_deleting_a_memory_cascades_its_links(conn: sqlite3.Connection) -> None:
    result = store.add_memory(conn, "MyClass here", WORKSPACE)
    with db.transaction(conn):
        conn.execute("DELETE FROM memories WHERE id = ?", (result.id,))
    assert link_count(conn) == 0


# ----------------------------------------------------------------------------- backfill


def unindex_everything(conn: sqlite3.Connection) -> None:
    """Recreate the pre-M2 state: memories exist, the entity graph does not."""
    with db.transaction(conn):
        conn.execute("DELETE FROM memory_entities")
        conn.execute("DELETE FROM entities")


def test_backfill_indexes_pre_existing_rows(conn: sqlite3.Connection) -> None:
    """AC12: first run creates links, second run creates none."""
    store.add_memory(conn, "load src/main.py with MyClass", WORKSPACE)
    store.add_memory(conn, "call resolve_db_path first", WORKSPACE)
    unindex_everything(conn)

    processed, created = indexer.backfill_all(conn)
    assert processed == 2
    assert created > 0
    after_first = link_count(conn)

    _, created_again = indexer.backfill_all(conn)
    assert created_again == 0
    assert link_count(conn) == after_first


def test_backfill_reports_zero_links_for_entity_free_rows(conn: sqlite3.Connection) -> None:
    """AC13 / DD-9: such a row is reprocessed forever but never adds a link."""
    store.add_memory(conn, "hello world", WORKSPACE)
    assert indexer.backfill_all(conn) == (1, 0)
    assert indexer.backfill_all(conn) == (1, 0)


def test_backfill_on_an_empty_database(conn: sqlite3.Connection) -> None:
    assert indexer.backfill_all(conn) == (0, 0)


def test_backfill_can_be_scoped_to_one_workspace(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "MyClass here", "alpha")
    store.add_memory(conn, "OtherThing there", "beta")
    unindex_everything(conn)

    processed, created = indexer.backfill_all(conn, "alpha")
    assert (processed, created) == (1, 1)
    assert set(count_norm_names(conn)) == {"myclass"}


def count_norm_names(conn: sqlite3.Connection) -> list[str]:
    return [row["norm_name"] for row in conn.execute("SELECT norm_name FROM entities")]


def test_backfill_rejects_a_blank_workspace(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="non-empty name"):
        indexer.backfill_all(conn, "   ")


def test_backfill_crosses_batch_boundaries(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indexer, "BACKFILL_BATCH_SIZE", 2)
    for index in range(5):
        store.add_memory(conn, f"MyClass number {index}", WORKSPACE)
    unindex_everything(conn)
    processed, created = indexer.backfill_all(conn)
    assert processed == 5
    assert created == 5


# -------------------------------------------------------------------------------- stats


def test_stats_counts_the_entity_graph(conn: sqlite3.Connection, db_path: Path) -> None:
    store.add_memory(conn, "load src/main.py with MyClass", WORKSPACE)
    summary = store.collect_stats(conn, db_path)
    assert summary.total_entities == 2
    assert summary.total_entity_links == 2


def test_stats_on_an_empty_database(conn: sqlite3.Connection, db_path: Path) -> None:
    summary = store.collect_stats(conn, db_path)
    assert (summary.total_entities, summary.total_entity_links) == (0, 0)


# ---------------------------------------------------------------------------------- cli


def test_cli_command_set() -> None:
    """AC15, widened in M3 by dedupe and gc, in M4 by serve, in M5 by four more.

    v0.4.0 adds ``promote``, the fifteenth.
    """
    result = CliRunner().invoke(cli.main, ["--help"])
    assert result.exit_code == 0
    assert set(cli.main.commands) == {
        "add",
        "agents",
        "audit",
        "backfill",
        "benchmark",
        "dedupe",
        "export",
        "gc",
        "import",
        "init",
        "promote",
        "restore",
        "search",
        "serve",
        "stats",
    }
    assert "backfill" in result.output


def test_cli_stats_reports_entities(db_path: Path) -> None:
    """AC4."""
    runner = CliRunner()
    added = runner.invoke(cli.main, ["add", "load src/main.py with MyClass", "-w", WORKSPACE])
    assert added.exit_code == 0
    result = runner.invoke(cli.main, ["stats"])
    assert result.exit_code == 0
    assert "entities: 2" in result.output
    assert "entity links: 2" in result.output


def test_cli_stats_on_an_empty_database(db_path: Path) -> None:
    """AC14."""
    result = CliRunner().invoke(cli.main, ["stats"])
    assert result.exit_code == 0
    assert "entities: 0" in result.output
    assert "entity links: 0" in result.output


def test_cli_backfill_output(db_path: Path) -> None:
    """AC12 through the CLI."""
    runner = CliRunner()
    runner.invoke(cli.main, ["add", "load src/main.py with MyClass", "-w", WORKSPACE])
    connection = db.open_database(db_path)
    try:
        unindex_everything(connection)
    finally:
        connection.close()

    first = runner.invoke(cli.main, ["backfill"])
    assert first.exit_code == 0
    assert first.output.strip() == "processed 1 memories, created 2 links"

    second = runner.invoke(cli.main, ["backfill"])
    assert second.exit_code == 0
    assert second.output.strip() == "processed 0 memories, created 0 links"


def test_cli_backfill_rejects_a_blank_workspace(db_path: Path) -> None:
    result = CliRunner().invoke(cli.main, ["backfill", "-w", "  "])
    assert result.exit_code != 0
    assert "non-empty name" in result.output
