#!/usr/bin/env bash
# localmem auto-capture — Claude Code Stop hook.
# Reads the hook payload on stdin and stores the final assistant message as a trace.
# Exits 0 no matter what: a memory-capture hook must never fail a session.
set -uo pipefail

command -v localmem >/dev/null 2>&1 || exit 0
command -v jq >/dev/null 2>&1 || exit 0

payload=$(cat)

# Claude Code has moved this field between versions; try the current shape, then the
# transcript fallback. `// empty` keeps jq quiet when neither is present.
summary=$(printf '%s' "$payload" | jq -r '
  (.last_assistant_message // .response // .message.content // empty)
  | if type == "array" then map(select(.type == "text") | .text) | join("\n") else . end
' 2>/dev/null)

# Nothing worth storing, or a one-liner like "done" — stop here.
[ -z "${summary//[[:space:]]/}" ] && exit 0
[ "${#summary}" -lt 40 ] && exit 0

# Workspace: let localmem detect it from the repository the session ran in.
cd "$(printf '%s' "$payload" | jq -r '.cwd // "."')" 2>/dev/null || exit 0

localmem add "$summary" --kind trace --source claude-code-hook >/dev/null 2>&1 || exit 0
exit 0
