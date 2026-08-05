"""M5 importer: the markdown splitter, the `import` command, and idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmem import cli, importer, store
from tests.conftest import FIXTURES_DIR

WORKSPACE = "proj"


def row_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
    return int(row["n"])


def seen_counts(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT seen_count FROM memories ORDER BY id").fetchall()
    return [int(row["seen_count"]) for row in rows]


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory the CLI runs in, so no test writes into the repository."""
    path = tmp_path / "work"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


# --------------------------------------------------------------------------- splitter


def test_paragraphs_become_one_record_each() -> None:
    records = importer.parse_records("first paragraph\nwrapped line\n\nsecond paragraph\n")
    assert records == ["first paragraph\nwrapped line", "second paragraph"]


def test_heading_is_prepended_to_its_records() -> None:
    """AC24 / §9's own example."""
    records = importer.parse_records("# Build commands\n\nuse pnpm, not npm\n")
    assert records == ["[Build commands] use pnpm, not npm"]


def test_the_nearest_heading_wins() -> None:
    text = "# Outer\n\nouter note\n\n## Inner\n\ninner note\n"
    assert importer.parse_records(text) == ["[Outer] outer note", "[Inner] inner note"]


def test_a_top_level_bullet_keeps_its_nested_children() -> None:
    text = (
        "- use pnpm, not npm\n"
        "  - `pnpm install` first\n"
        "  - then `pnpm -r build`\n"
        "- run migrations\n"
    )
    assert importer.parse_records(text) == [
        "- use pnpm, not npm\n  - `pnpm install` first\n  - then `pnpm -r build`",
        "- run migrations",
    ]


def test_numbered_bullets_split_at_the_top_level() -> None:
    assert importer.parse_records("1. first step\n2. second step\n") == [
        "1. first step",
        "2. second step",
    ]


def test_a_fenced_block_stays_whole_and_is_not_markup() -> None:
    """AC23: `#` and `-` inside a fence are code, not heading and bullet syntax."""
    text = "# Recipe\n\n```bash\n# not a heading\n- not a bullet\npnpm -r test\n```\n"
    records = importer.parse_records(text)
    assert records == ["[Recipe] ```bash\n# not a heading\n- not a bullet\npnpm -r test\n```"]


def test_a_tilde_fence_is_closed_only_by_tildes() -> None:
    text = "~~~\n```\nstill inside\n~~~\nafter\n"
    assert importer.parse_records(text) == ["~~~\n```\nstill inside\n~~~", "after"]


def test_a_longer_closing_fence_is_accepted() -> None:
    assert importer.parse_records("```\ncode\n`````\n") == ["```\ncode\n`````"]


def test_a_fence_with_an_info_string_does_not_close_itself() -> None:
    assert importer.parse_records("```python\nprint(1)\n```\n") == ["```python\nprint(1)\n```"]


def test_an_unterminated_fence_is_still_emitted() -> None:
    assert importer.parse_records("```\ncode without an end\n") == ["```\ncode without an end"]


def test_markup_only_records_are_dropped() -> None:
    assert importer.parse_records("---\n\n***\n\nreal content\n") == ["real content"]


def test_headings_alone_produce_no_records() -> None:
    assert importer.parse_records("# One\n\n## Two\n\n### Three\n") == []


def test_closing_hashes_are_stripped_from_a_heading() -> None:
    assert importer.parse_records("## Build ##\n\nuse pnpm\n") == ["[Build] use pnpm"]


def test_an_empty_heading_clears_the_context() -> None:
    assert importer.parse_records("# Build\n\nfirst\n\n# \n\nsecond\n") == [
        "[Build] first",
        "second",
    ]


def test_an_empty_document_yields_nothing() -> None:
    assert importer.parse_records("") == []
    assert importer.parse_records("\n\n\n") == []


def test_the_splitter_is_deterministic() -> None:
    """DD-14: byte-stable output is what makes a re-import a no-op."""
    text = (FIXTURES_DIR / "CLAUDE.md").read_text(encoding="utf-8")
    assert importer.parse_records(text) == importer.parse_records(text)


def test_the_claude_fixture_splits_as_specified() -> None:
    records = importer.parse_records((FIXTURES_DIR / "CLAUDE.md").read_text(encoding="utf-8"))
    assert records == [
        "[Project conventions] This project uses a pnpm workspace and a single Postgres "
        "instance.\nEvery service reads its config from the environment.",
        "[Build commands] - use pnpm, not npm\n  - `pnpm install` at the repo root\n"
        "  - `pnpm -r build` builds every package",
        "[Build commands] - run migrations before the test suite",
        "[Build commands] - never commit a lockfile conflict",
        "[Shell recipe] ```bash\n# this comment is not a heading\n- this dash is not a bullet\n"
        "pnpm -r test --filter ./services/api\n```",
        "[Ghi chú tiếng Việt] Dùng pnpm thay vì npm khi cài phụ thuộc.",
    ]


def test_source_for_uses_the_file_name() -> None:
    assert importer.source_for(Path("/a/b/CLAUDE.md")) == "import:CLAUDE.md"


# --------------------------------------------------------------------------- import_file


def test_import_file_stores_every_record(conn: sqlite3.Connection) -> None:
    outcome = importer.import_file(conn, FIXTURES_DIR / "AGENTS.md", WORKSPACE)

    assert outcome.records == outcome.added == row_count(conn)
    assert outcome.merged == 0
    rows = conn.execute("SELECT kind, source, workspace FROM memories").fetchall()
    assert {row["kind"] for row in rows} == {"imported"}
    assert {row["source"] for row in rows} == {"import:AGENTS.md"}
    assert {row["workspace"] for row in rows} == {WORKSPACE}


def test_double_import_is_idempotent(conn: sqlite3.Connection) -> None:
    """AC9 / §9 DoD: row count identical, seen_count incremented."""
    first = importer.import_file(conn, FIXTURES_DIR / "CLAUDE.md", WORKSPACE)
    rows_after_first = row_count(conn)

    second = importer.import_file(conn, FIXTURES_DIR / "CLAUDE.md", WORKSPACE)

    assert rows_after_first == row_count(conn)
    assert second.added == 0
    assert second.merged == first.records
    assert seen_counts(conn) == [2] * rows_after_first


def test_a_missing_file_raises_oserror(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(OSError):
        importer.import_file(conn, tmp_path / "gone.md", WORKSPACE)


def test_records_are_searchable_after_import(conn: sqlite3.Connection) -> None:
    importer.import_file(conn, FIXTURES_DIR / "CLAUDE.md", WORKSPACE)
    hits = store.search_memories(conn, "pnpm", WORKSPACE, 5)
    assert hits
    assert any("Build commands" in hit.content for hit in hits)


def test_vietnamese_records_survive_the_round_trip(conn: sqlite3.Connection) -> None:
    importer.import_file(conn, FIXTURES_DIR / "CLAUDE.md", WORKSPACE)
    hits = store.search_memories(conn, "phu thuoc", WORKSPACE, 5)
    assert any("phụ thuộc" in hit.content for hit in hits)


# --------------------------------------------------------------------------- CLI


def test_import_command_round_trip(work: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.main, ["import", str(FIXTURES_DIR / "AGENTS.md"), "-w", WORKSPACE])

    assert result.exit_code == 0
    assert "records" in result.output
    assert "Consider trimming the imported sections" in result.output
    found = runner.invoke(cli.main, ["search", "pull requests", "-w", WORKSPACE])
    assert "Prefer small pull requests" in found.output


def test_import_command_is_idempotent(work: Path, db_path: Path) -> None:
    """AC9 through the CLI."""
    runner = CliRunner()
    arguments = ["import", str(FIXTURES_DIR / "CLAUDE.md"), "-w", WORKSPACE]
    runner.invoke(cli.main, arguments)
    connection = sqlite3.connect(db_path)
    try:
        before = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        connection.close()

    second = runner.invoke(cli.main, arguments)

    connection = sqlite3.connect(db_path)
    try:
        after = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        seen = connection.execute("SELECT MIN(seen_count) FROM memories").fetchone()[0]
    finally:
        connection.close()
    assert second.exit_code == 0
    assert after == before
    assert seen == 2


def test_dry_run_writes_nothing(work: Path, db_path: Path, tmp_path: Path) -> None:
    """AC8 / §9 DoD: no database rows and no file mtime changes."""
    source = tmp_path / "CLAUDE.md"
    source.write_text(
        "# Build\n\nfirst fact\n\nsecond fact\n\nthird fact\n\nfourth fact\n\n"
        "fifth fact\n\nsixth fact\n",
        encoding="utf-8",
    )
    mtime_before = source.stat().st_mtime_ns

    result = CliRunner().invoke(cli.main, ["import", str(source), "--dry-run", "-w", WORKSPACE])

    assert result.exit_code == 0
    assert "would create 6 records" in result.output
    assert result.output.count("---") == importer.DRY_RUN_PREVIEW
    assert "[Build] fifth fact" in result.output
    assert "[Build] sixth fact" not in result.output
    assert "nothing was written" in result.output
    assert not db_path.exists()
    assert source.stat().st_mtime_ns == mtime_before


def test_dry_run_reports_an_unreadable_file(work: Path, tmp_path: Path) -> None:
    broken = tmp_path / "CLAUDE.md"
    broken.write_bytes(b"\xff\xfe not utf-8")

    result = CliRunner().invoke(cli.main, ["import", str(broken), "--dry-run"])

    assert result.exit_code != 0
    assert "cannot read" in result.output


def test_import_reports_an_unreadable_file_without_aborting(work: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.md"
    broken.write_bytes(b"\xff\xfe not utf-8")

    result = CliRunner().invoke(
        cli.main, ["import", str(broken), str(FIXTURES_DIR / "AGENTS.md"), "-w", WORKSPACE]
    )

    assert result.exit_code == 0
    assert "cannot read" in result.output
    found = CliRunner().invoke(cli.main, ["search", "pull requests", "-w", WORKSPACE])
    assert "Prefer small pull requests" in found.output


def test_select_without_a_terminal_errors_clearly(work: Path, db_path: Path) -> None:
    """AC25 / DD-15: a clear failure, not a hang and not a silent import-everything."""
    result = CliRunner().invoke(cli.main, ["import", str(FIXTURES_DIR / "AGENTS.md"), "--select"])

    assert result.exit_code != 0
    assert "--select needs a terminal" in result.output
    assert not db_path.exists()


def test_select_asks_per_file(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    result = CliRunner().invoke(
        cli.main,
        [
            "import",
            str(FIXTURES_DIR / "AGENTS.md"),
            str(FIXTURES_DIR / "steering.md"),
            "--select",
            "-w",
            WORKSPACE,
        ],
        input="y\nn\n",
    )

    assert result.exit_code == 0
    found = CliRunner().invoke(cli.main, ["search", "coverage floor", "-w", WORKSPACE])
    assert "coverage floor" in found.output
    steering = CliRunner().invoke(cli.main, ["search", "steering files", "-w", WORKSPACE])
    assert "no memories matching" in steering.output


def test_select_can_choose_nothing(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    result = CliRunner().invoke(
        cli.main,
        ["import", str(FIXTURES_DIR / "AGENTS.md"), "--select", "-w", WORKSPACE],
        input="n\n",
    )

    assert result.exit_code == 0
    assert "no files selected, nothing imported" in result.output


def test_import_requires_at_least_one_path(work: Path) -> None:
    result = CliRunner().invoke(cli.main, ["import"])
    assert result.exit_code != 0


def test_import_rejects_a_missing_path(work: Path) -> None:
    result = CliRunner().invoke(cli.main, ["import", "does-not-exist.md"])
    assert result.exit_code != 0


# --------------------------------------------------------------------- whole-file mode

SKILL = FIXTURES_DIR / "security-review.md"


def test_whole_file_produces_exactly_one_record() -> None:
    records = importer.read_records(SKILL, whole_file=True)
    assert len(records) == 1
    # The document arrives intact — heading, every section, every bullet.
    assert records[0].startswith("# Security review checklist")
    assert "New runtime dependency?" in records[0]


def test_whole_file_does_not_change_the_default_split() -> None:
    assert len(importer.read_records(SKILL)) > 1
    assert importer.read_records(SKILL) == importer.parse_records(SKILL.read_text(encoding="utf-8"))


def test_whole_file_of_a_content_free_document_yields_no_record() -> None:
    assert importer.whole_file_records("\n---\n***\n") == []
    assert importer.whole_file_records("   \n\n") == []


def test_whole_file_import_is_idempotent(conn: sqlite3.Connection) -> None:
    first = importer.import_file(conn, SKILL, WORKSPACE, whole_file=True)
    assert (first.records, first.added) == (1, 1)

    second = importer.import_file(conn, SKILL, WORKSPACE, whole_file=True)

    assert row_count(conn) == 1
    assert (second.added, second.merged) == (0, 1)
    assert seen_counts(conn) == [2]


def test_the_two_modes_share_a_workspace_without_colliding(conn: sqlite3.Connection) -> None:
    """A split import and a whole-file import of the same file are different records."""
    split = importer.import_file(conn, SKILL, WORKSPACE)
    whole = importer.import_file(conn, SKILL, WORKSPACE, whole_file=True)

    assert whole.added == 1
    assert row_count(conn) == split.records + 1
    assert seen_counts(conn) == [1] * row_count(conn)


def test_a_whole_file_record_is_recalled_intact(conn: sqlite3.Connection) -> None:
    importer.import_file(conn, SKILL, "global", whole_file=True)
    hits = store.search_memories(conn, "security review", "global", 5)
    assert len(hits) == 1
    assert hits[0].kind == importer.IMPORT_KIND
    assert hits[0].source == "import:security-review.md"
    assert "parameterized" in hits[0].content
    assert "New runtime dependency?" in hits[0].content


def test_cli_whole_file_import_stores_one_record(work: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.main, ["import", str(SKILL), "-w", "global", "--whole-file"])
    assert result.exit_code == 0
    assert "1 records — 1 new" in result.output

    # And it is reachable from a completely different workspace, because it is global.
    found = runner.invoke(cli.main, ["search", "security review", "-w", "some-other-repo"])
    assert found.exit_code == 0
    assert "New runtime dependency?" in found.output


def test_cli_whole_file_dry_run_writes_nothing(work: Path, db_path: Path) -> None:
    result = CliRunner().invoke(
        cli.main, ["import", str(SKILL), "-w", "global", "--whole-file", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "would create 1 records" in result.output
    assert "nothing was written" in result.output
    assert not db_path.exists()


def test_skip_message_is_the_plan_text_verbatim() -> None:
    """AC21: character for character, as one module constant (DD-10)."""
    assert importer.SKIP_MESSAGE == (
        "You can import anytime later with `localmem import` — skipping now loses nothing."
    )
