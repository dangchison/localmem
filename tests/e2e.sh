#!/usr/bin/env bash
#
# localmem end-to-end acceptance script (PLAN.md §11, "Final acceptance").
#
#   fresh venv -> pip install -e . -> localmem init (sandboxed HOME, decline agents and
#   import) -> add x3 (one duplicate) -> search shows ranked results and the merge ->
#   import a fixture twice with the row count stable -> drive `localmem serve` with a
#   scripted MCP client over stdio -> benchmark --json parsed for saved_pct.
#
# Both HOME and LOCALMEM_DB are redirected into a throwaway directory before any localmem
# process starts, so this script cannot reach the real ~/.localmem/ or any real agent
# config. That is asserted at the end by comparing checksums taken before the run.
#
# Exits 0 on success, non-zero with a named failure otherwise.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FIXTURE="${REPO_ROOT}/tests/fixtures/CLAUDE.md"

# Captured before HOME is redirected; every "did we touch the user's files" check uses it.
REAL_HOME="${HOME}"

STEP=""
SANDBOX=""

# ------------------------------------------------------------------ output helpers

step() {
    STEP="$1"
    printf '\n\033[1m== %s\033[0m\n' "$1"
}

info() { printf '   %s\n' "$1"; }

fail() {
    printf '\n\033[31mFAILED\033[0m during: %s\n       %s\n' "${STEP:-startup}" "$1" >&2
    if [ -n "${SANDBOX}" ] && [ -d "${SANDBOX}" ]; then
        printf '       sandbox kept for inspection: %s\n' "${SANDBOX}" >&2
        KEEP_SANDBOX=1
    fi
    exit 1
}

KEEP_SANDBOX=0
cleanup() {
    if [ "${KEEP_SANDBOX}" -eq 0 ] && [ -n "${SANDBOX}" ] && [ -d "${SANDBOX}" ]; then
        rm -rf "${SANDBOX}"
    fi
}
trap cleanup EXIT

# Fingerprint a path: its sha256 if it is a file, a marker otherwise. Used to prove the
# user's real files are byte-for-byte what they were.
digest() {
    local target="$1"
    if [ ! -e "${target}" ]; then
        printf 'absent'
    elif [ -d "${target}" ]; then
        printf 'dir:'
        (cd "${target}" && find . -type f -exec "${SHA_TOOL[@]}" {} + 2>/dev/null | sort) || true
    else
        "${SHA_TOOL[@]}" "${target}" | awk '{print $1}'
    fi
}

if command -v shasum >/dev/null 2>&1; then
    SHA_TOOL=(shasum -a 256)
elif command -v sha256sum >/dev/null 2>&1; then
    SHA_TOOL=(sha256sum)
else
    fail "neither shasum nor sha256sum is available; cannot verify your files are untouched"
fi

# ------------------------------------------------------------------ 0. preconditions

step "0. Preconditions"

[ -f "${REPO_ROOT}/pyproject.toml" ] || fail "no pyproject.toml at ${REPO_ROOT}"
[ -f "${FIXTURE}" ] || fail "missing import fixture ${FIXTURE}"

# Pick an interpreter that satisfies requires-python >= 3.10. $PYTHON wins; then the repo's
# own .venv, which is the interpreter the project is developed against; then the PATH.
choose_python() {
    local candidate
    for candidate in "${PYTHON:-}" "${REPO_ROOT}/.venv/bin/python" \
        python3.13 python3.12 python3.11 python3.10 python3; do
        [ -n "${candidate}" ] || continue
        command -v "${candidate}" >/dev/null 2>&1 || continue
        if "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
            >/dev/null 2>&1; then
            command -v "${candidate}"
            return 0
        fi
    done
    return 1
}

PYTHON_BIN="$(choose_python)" || fail \
    "no Python >= 3.10 found. Set PYTHON=/path/to/python3.11 (or newer) and re-run."
info "interpreter: ${PYTHON_BIN} ($("${PYTHON_BIN}" -V 2>&1))"

# Snapshot everything the run must not touch, using the *real* home.
BEFORE_LOCALMEM="$(digest "${REAL_HOME}/.localmem")"
BEFORE_CLAUDE_JSON="$(digest "${REAL_HOME}/.claude.json")"
BEFORE_CODEX="$(digest "${REAL_HOME}/.codex/config.toml")"
BEFORE_GEMINI="$(digest "${REAL_HOME}/.gemini/config/mcp_config.json")"
BEFORE_KIRO="$(digest "${REAL_HOME}/.kiro/settings/mcp.json")"
BEFORE_REPO_MCP="$(digest "${REPO_ROOT}/.mcp.json")"
info "snapshotted the real ~/.localmem/ and the four real agent configs"

# ------------------------------------------------------------------ 1. sandbox + install

step "1. Sandbox and install"

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/localmem-e2e.XXXXXX")"
SANDBOX_HOME="${SANDBOX}/home"
WORK="${SANDBOX}/work"
mkdir -p "${SANDBOX_HOME}" "${WORK}" "${SANDBOX}/db"

# Both redirections happen here, before any localmem process exists.
export HOME="${SANDBOX_HOME}"
export USERPROFILE="${SANDBOX_HOME}"
export LOCALMEM_DB="${SANDBOX}/db/memory.db"
info "HOME        -> ${HOME}"
info "LOCALMEM_DB -> ${LOCALMEM_DB}"

# Fake every agent so step 2 of `init` has something real to decline.
mkdir -p "${SANDBOX_HOME}/.claude" "${SANDBOX_HOME}/.codex" \
    "${SANDBOX_HOME}/.gemini" "${SANDBOX_HOME}/.kiro"
# ...and an instruction file so step 3 has something real to decline importing.
cat >"${WORK}/CLAUDE.md" <<'MD'
# Sandbox project

- this file exists so `init` has something to offer importing
MD

VENV="${SANDBOX}/venv"
"${PYTHON_BIN}" -m venv "${VENV}" || fail "could not create a virtualenv at ${VENV}"
LM="${VENV}/bin/localmem"

info "installing ${REPO_ROOT} into ${VENV} ..."
"${VENV}/bin/python" -m pip install --quiet --disable-pip-version-check -e "${REPO_ROOT}" \
    >"${SANDBOX}/pip.log" 2>&1 ||
    fail "pip install -e . failed; see ${SANDBOX}/pip.log"
[ -x "${LM}" ] || fail "pip install succeeded but ${LM} is missing"
info "installed $("${LM}" --version)"

cd "${WORK}"

# ------------------------------------------------------------------ 2. init, declining

step "2. localmem init — declining agents and import"

# No TTY and no flags: init reports what it would do and writes no agent config.
"${LM}" init </dev/null >"${SANDBOX}/init.out" 2>&1 ||
    fail "localmem init exited non-zero; output in ${SANDBOX}/init.out"

grep -q "nothing written" "${SANDBOX}/init.out" ||
    fail "init did not report that it wrote no agent config"
grep -q "You can import anytime later" "${SANDBOX}/init.out" ||
    fail "init did not print the skip-import message"
grep -q "Step 5 — self-check" "${SANDBOX}/init.out" ||
    fail "init did not reach step 5"

for declined in \
    "${SANDBOX_HOME}/.codex/config.toml" \
    "${SANDBOX_HOME}/.gemini/config/mcp_config.json" \
    "${SANDBOX_HOME}/.kiro/settings/mcp.json" \
    "${WORK}/.mcp.json"; do
    [ ! -e "${declined}" ] || fail "init wrote ${declined} without a yes"
done
info "four agents detected, none registered, nothing written"

[ -f "${LOCALMEM_DB}" ] || fail "init did not create the database at ${LOCALMEM_DB}"

row_count() {
    "${LM}" stats | awk -F': *' '/^memories:/ {print $2; exit}'
}

count="$(row_count)"
[ "${count}" = "0" ] || fail "expected an empty database after declining import, got ${count} rows"
info "database created, 0 rows — declining import left a working cold start"

# ------------------------------------------------------------------ 3. add x3

step "3. localmem add x3 (one duplicate)"

first="$("${LM}" add "use pnpm, not npm")"
second="$("${LM}" add "the pnpm lockfile must be regenerated after a dependency bump")"
# Same memory as the first: normalization folds the bullet prefix, the case and the spaces.
third="$("${LM}" add "- Use   PNPM, not npm")"

info "1: ${first}"
info "2: ${second}"
info "3: ${third}"

case "${first}" in *'"status": "added"'*) ;; *) fail "first add was not 'added': ${first}" ;; esac
case "${second}" in *'"status": "added"'*) ;; *) fail "second add was not 'added': ${second}" ;; esac
case "${third}" in
    *'"status": "duplicate_merged"'*) ;;
    *) fail "third add should have merged into the first: ${third}" ;;
esac
case "${third}" in *'"seen_count": 2'*) ;; *) fail "merge did not bump seen_count: ${third}" ;; esac

count="$(row_count)"
[ "${count}" = "2" ] || fail "three adds with one duplicate should leave 2 rows, got ${count}"

# ------------------------------------------------------------------ 4. search

step "4. localmem search — ranked results, duplicate merged"

"${LM}" search "pnpm" >"${SANDBOX}/search.out" 2>&1 || fail "localmem search exited non-zero"
cat "${SANDBOX}/search.out"

grep -q '^1\. \[score' "${SANDBOX}/search.out" || fail "search printed no ranked first result"
grep -q '^2\. \[score' "${SANDBOX}/search.out" || fail "search printed no ranked second result"
grep -q 'seen=2' "${SANDBOX}/search.out" ||
    fail "search did not show the merged memory with seen=2"

# ------------------------------------------------------------------ 5. import twice

step "5. localmem import — twice, row count stable"

"${LM}" import --dry-run "${FIXTURE}" >"${SANDBOX}/import-dry.out" 2>&1 ||
    fail "import --dry-run exited non-zero"
grep -q "nothing was written" "${SANDBOX}/import-dry.out" ||
    fail "import --dry-run did not say it wrote nothing"
[ "$(row_count)" = "2" ] || fail "import --dry-run changed the row count"

"${LM}" import "${FIXTURE}" >"${SANDBOX}/import-1.out" 2>&1 || fail "first import exited non-zero"
cat "${SANDBOX}/import-1.out"
after_first="$(row_count)"
[ "${after_first}" -gt 2 ] || fail "the first import added no rows"

"${LM}" import "${FIXTURE}" >"${SANDBOX}/import-2.out" 2>&1 || fail "second import exited non-zero"
cat "${SANDBOX}/import-2.out"
after_second="$(row_count)"
[ "${after_first}" = "${after_second}" ] ||
    fail "re-importing changed the row count: ${after_first} -> ${after_second}"
grep -q "0 new" "${SANDBOX}/import-2.out" ||
    fail "the second import reported new rows; it should have merged every record"
info "row count stable at ${after_second} across two imports"

# ------------------------------------------------------------------ 6. MCP round trip

step "6. localmem serve — scripted MCP client over stdio"

cat >"${SANDBOX}/mcp_client.py" <<'PY'
"""Drive a real `localmem serve` subprocess over stdio and check the frozen contract.

Uses anyio.run() plus mcp.client.stdio.stdio_client, both of which the `mcp` dependency
already provides -- this adds nothing to the dependency list.
"""

import json
import os
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER = sys.argv[1]
ERRLOG = sys.argv[2]

WORKSPACE = "e2e-mcp"
EMPTY_WORKSPACE = "e2e-mcp-empty"
CONTENT = "the e2e run wrote this through memory_add"
RESULT_KEYS = {
    "id", "content", "workspace", "kind", "source", "created_at", "score", "neighbors",
}


class Failure(Exception):
    """A contract check that did not hold."""


def check(condition, message):
    if not condition:
        raise Failure(message)


def body(result):
    check(result.structured_content is not None, "tool result carried no structured content")
    text = "".join(block.text for block in result.content if block.type == "text")
    check(
        json.loads(text) == result.structured_content,
        "the text and structured encodings of the tool result disagree",
    )
    return result.structured_content


async def run(session):
    initialized = await session.initialize()
    check(
        initialized.server_info.name == "localmem",
        f"server identified as {initialized.server_info.name!r}",
    )

    listed = await session.list_tools()
    names = {tool.name for tool in listed.tools}
    check(names == {"memory_recall", "memory_add"}, f"unexpected tool set {sorted(names)}")

    empty = body(await session.call_tool(
        "memory_recall", {"query": "nothing here", "workspace": EMPTY_WORKSPACE}
    ))
    check(set(empty) == {"results", "core_memory", "message"}, f"recall keys {sorted(empty)}")
    check(empty["results"] == [], "recall on an empty workspace returned results")
    check(empty["message"], "recall on an empty workspace returned no friendly message")

    added = body(await session.call_tool(
        "memory_add", {"content": CONTENT, "workspace": WORKSPACE, "source": "e2e"}
    ))
    check(set(added) == {"status", "id", "seen_count"}, f"add keys {sorted(added)}")
    check(added["status"] == "added", f"add returned {added['status']!r}")
    check(added["id"] > 0, "add returned no row id")

    recalled = body(await session.call_tool(
        "memory_recall", {"query": "memory_add", "workspace": WORKSPACE, "k": 5}
    ))
    hits = recalled["results"]
    check(bool(hits), "the memory written over MCP was not recalled over MCP")
    check(set(hits[0]) == RESULT_KEYS, f"result keys {sorted(hits[0])}")
    check(hits[0]["content"] == CONTENT, "recall returned different content than was added")
    check(hits[0]["workspace"] == WORKSPACE, "recall crossed the workspace boundary")

    again = body(await session.call_tool(
        "memory_add", {"content": CONTENT, "workspace": WORKSPACE}
    ))
    check(again["status"] == "duplicate_merged", f"re-add returned {again['status']!r}")
    check(again["seen_count"] == 2, f"re-add left seen_count at {again['seen_count']}")

    return {"tools": sorted(names), "id": added["id"], "recalled": len(hits)}


async def main():
    parameters = StdioServerParameters(
        command=SERVER, args=["serve"], env=dict(os.environ)
    )
    with open(ERRLOG, "w", encoding="utf-8") as stream:
        async with stdio_client(parameters, errlog=stream) as (read, write):
            async with ClientSession(read, write) as session:
                summary = await run(session)
    print(json.dumps(summary))


try:
    anyio.run(main)
except Failure as exc:
    print(f"MCP contract violated: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc
PY

"${VENV}/bin/python" "${SANDBOX}/mcp_client.py" "${LM}" "${SANDBOX}/serve.err" \
    >"${SANDBOX}/mcp.out" 2>"${SANDBOX}/mcp.err" || {
    cat "${SANDBOX}/mcp.err" >&2
    [ -s "${SANDBOX}/serve.err" ] && {
        echo "--- localmem serve stderr ---" >&2
        cat "${SANDBOX}/serve.err" >&2
    }
    fail "the scripted MCP client failed"
}
info "round trip ok: $(cat "${SANDBOX}/mcp.out")"

# The server must leave no WAL sidecar behind: one connection per tool call, closed each time.
for sidecar in "${LOCALMEM_DB}-wal" "${LOCALMEM_DB}-shm"; do
    [ ! -e "${sidecar}" ] || fail "the MCP server left ${sidecar} behind"
done
info "no -wal/-shm sidecar left behind"

# ------------------------------------------------------------------ 7. benchmark --json

step "7. localmem benchmark --json"

"${LM}" benchmark --json >"${SANDBOX}/benchmark.json" 2>&1 ||
    fail "localmem benchmark --json exited non-zero"
cat "${SANDBOX}/benchmark.json"

"${VENV}/bin/python" - "${SANDBOX}/benchmark.json" <<'PY' || fail "benchmark --json is not usable"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)

required = {
    "workspace", "files", "before_tokens", "after_tokens",
    "saved_tokens", "saved_pct", "after_breakdown", "caveat",
}
missing = required - set(report)
if missing:
    raise SystemExit(f"benchmark JSON is missing {sorted(missing)}")
if not isinstance(report["saved_pct"], (int, float)):
    raise SystemExit(f"saved_pct is {type(report['saved_pct']).__name__}, not a number")
if "±15%" not in report["caveat"]:
    raise SystemExit("the accuracy caveat is missing from the JSON payload")
print(f"saved_pct = {report['saved_pct']}")
PY

# ------------------------------------------------------------------ 8. nothing touched

step "8. The real HOME is untouched"

[ ! -e "${REAL_HOME}/.localmem" ] || [ "${BEFORE_LOCALMEM}" != "absent" ] ||
    fail "the run created ${REAL_HOME}/.localmem — the sandbox leaked"

check_unchanged() {
    local label="$1" target="$2" before="$3"
    local after
    after="$(digest "${target}")"
    [ "${after}" = "${before}" ] || fail "${label} changed during the run: ${target}"
    [ ! -e "${target}.bak" ] || fail "${label} gained a .bak file: ${target}.bak"
}

check_unchanged "the real localmem database" "${REAL_HOME}/.localmem" "${BEFORE_LOCALMEM}"
check_unchanged "the real Claude Code state" "${REAL_HOME}/.claude.json" "${BEFORE_CLAUDE_JSON}"
check_unchanged "the real Codex config" "${REAL_HOME}/.codex/config.toml" "${BEFORE_CODEX}"
check_unchanged "the real Antigravity config" \
    "${REAL_HOME}/.gemini/config/mcp_config.json" "${BEFORE_GEMINI}"
check_unchanged "the real Kiro config" "${REAL_HOME}/.kiro/settings/mcp.json" "${BEFORE_KIRO}"
check_unchanged "the repository .mcp.json" "${REPO_ROOT}/.mcp.json" "${BEFORE_REPO_MCP}"

if [ "${BEFORE_LOCALMEM}" = "absent" ]; then
    info "${REAL_HOME}/.localmem still does not exist"
else
    info "${REAL_HOME}/.localmem is byte-for-byte what it was"
fi
info "all four real agent configs are byte-for-byte what they were"

printf '\n\033[32mPASS\033[0m — localmem end-to-end acceptance complete.\n'
