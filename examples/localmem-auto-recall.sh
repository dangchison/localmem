#!/usr/bin/env bash
# localmem auto-recall — Claude Code UserPromptSubmit hook.
# Reads the hook payload on stdin and prints matching memories on stdout, which Claude
# Code injects into the context of that prompt.
# Exits 0 no matter what, and prints nothing when it has nothing: a recall hook runs on
# every prompt, so it must never block one and never add noise to one.
set -uo pipefail

# Seconds before the search is abandoned. `timeout` is coreutils; macOS ships it as
# `gtimeout` with `brew install coreutils`, and without either the search simply runs
# unguarded — localmem is a local SQLite read, not a network call.
readonly LOCALMEM_HOOK_TIMEOUT=5
readonly LOCALMEM_HOOK_RESULTS=3
# Prompts longer than this are searched by their first characters only. A pasted log or
# stack trace has no retrieval value past this point, and the cap bounds every cost after
# it in one move — see the quadratic note below.
readonly LOCALMEM_MAX_PROMPT_CHARS=4000

command -v localmem >/dev/null 2>&1 || exit 0
command -v jq >/dev/null 2>&1 || exit 0

payload=$(cat) || exit 0

prompt=$(printf '%s' "$payload" | jq -r '.prompt // empty' 2>/dev/null) || exit 0

# Cap FIRST, before anything looks at the prompt character by character.
if [ "${#prompt}" -gt "$LOCALMEM_MAX_PROMPT_CHARS" ]; then
    prompt=${prompt:0:$LOCALMEM_MAX_PROMPT_CHARS}
fi

# Empty, whitespace-only, or a payload that never had a `prompt` field: nothing to search.
#
# This MUST stay a `case`. The obvious `[ -z "${prompt//[[:space:]]/}" ]` is quadratic in
# bash 3.2.57 — the version /usr/bin/env bash resolves to on every stock macOS — whenever
# the text is whitespace-heavy, which a pasted log is. Measured on 3.2.57: 50 KB of
# log-shaped text spent **523 seconds** inside that one expansion, before the timeout
# guard below is ever reached. The `case` below is a single linear pattern match: 0.03 s
# on 1 MB, and 0.08 s on 1 MB of pure whitespace, which is its worst case.
case "$prompt" in
    *[![:space:]]*) ;;
    *) exit 0 ;;
esac

# Workspace: let localmem detect it from the repository the session is running in.
cd "$(printf '%s' "$payload" | jq -r '.cwd // "."')" 2>/dev/null || exit 0

if command -v timeout >/dev/null 2>&1; then
    timeout "$LOCALMEM_HOOK_TIMEOUT" \
        localmem search "$prompt" --context -k "$LOCALMEM_HOOK_RESULTS" 2>/dev/null || exit 0
elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$LOCALMEM_HOOK_TIMEOUT" \
        localmem search "$prompt" --context -k "$LOCALMEM_HOOK_RESULTS" 2>/dev/null || exit 0
else
    localmem search "$prompt" --context -k "$LOCALMEM_HOOK_RESULTS" 2>/dev/null || exit 0
fi

exit 0
