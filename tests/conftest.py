"""Shared fixtures. LOCALMEM_DB is redirected to tmp_path so the real
~/.localmem/ database is never touched by the test suite, and HOME is redirected
so no test can reach the real ~/.claude/, ~/.codex/, ~/.gemini/ or ~/.kiro/."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from localmem import config, db
from localmem.agents import command

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CONFIG_FIXTURES_DIR = FIXTURES_DIR / "configs"

#: The absolute path every agent config in the test suite is expected to carry.
#:
#: The real answer depends on how the machine running the tests installed localmem —
#: ``~/.local/bin`` under ``uv tool install``, a venv under ``pip install -e .``, nothing
#: at all in a bare source checkout. Pinning it makes every writer assertion an exact
#: equality instead of a re-derivation of the code under test, and it means the suite
#: does not need localmem installed to run. The handful of tests that must see the real
#: resolution ask for the ``real_server_command`` fixture and skip without an install.
STUB_SERVER_COMMAND = "/opt/localmem/bin/localmem"


@pytest.fixture(autouse=True)
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "memory.db"
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(path))
    return path


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at an empty temporary directory for every test.

    The agent writers take ``home`` as a parameter and never resolve it themselves,
    so this is the second of two independent layers: even a bug that reached for
    ``Path.home()`` would land here rather than in the user's real config.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def real_server_command() -> str | None:
    """Opt out of :func:`stub_server_command`: this test needs the real installed path.

    Returns what :func:`shutil.which` finds, or ``None`` when localmem is not installed
    on this machine — the tests that need it skip on ``None`` rather than going red for
    a reason that has nothing to do with the code.
    """
    return shutil.which(command.SERVER_NAME)


@pytest.fixture(autouse=True)
def stub_server_command(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> str | None:
    """Pin the command every writer embeds, so assertions can be exact equalities.

    Skipped for any test that asks for ``real_server_command``; a test that wants to
    exercise a *failing* resolution simply patches the same attribute again itself.
    """
    if "real_server_command" in request.fixturenames:
        return None
    monkeypatch.setattr(command, "resolve_server_command", lambda: STUB_SERVER_COMMAND)
    return STUB_SERVER_COMMAND


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.open_database(db_path)
    try:
        yield connection
    finally:
        connection.close()
