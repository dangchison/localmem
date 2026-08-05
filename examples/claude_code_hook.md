# Auto-capture with a Claude Code Stop hook

An opt-in example. It answers the one failure mode a pull-based memory has: **the agent
forgets to call `memory_add`.** A hook does not forget.

Nothing in localmem installs this. localmem never edits your agent settings — that rule holds
for hooks exactly as it holds for MCP registration. Copy the script, read it, and add the
block to your own `settings.json` yourself.

## What it does

Claude Code runs a **Stop hook** when a session finishes and hands it a JSON object on stdin.
The script below pulls the last assistant message out of that object and pipes it into:

```
localmem add "<summary>" --kind trace --source claude-code-hook -w <workspace>
```

`--kind trace` is deliberate. A trace is a raw record of what happened, not a curated fact —
which is exactly what an end-of-session summary is, and exactly the shape *Zero-Mem* argues
for. Tier-1 dedup means a repeated summary bumps `seen_count` instead of piling up rows.

`--kind core` is **not** available here, and not by accident: core memory is loaded into every
recall, so it stays human-curated. See `docs/design_decisions.md` §23.

## The script

It lives in this repository as [`localmem-capture.sh`](localmem-capture.sh); a test asserts
the copy below is byte-for-byte the same file, so the two cannot drift. Save it as
`~/.claude/hooks/localmem-capture.sh` and `chmod +x` it.

```bash
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
```

## Wiring it up

Add this to `~/.claude/settings.json` (or the project's `.claude/settings.json`). If the file
already has a `hooks` key, merge into it rather than replacing it:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/localmem-capture.sh"
          }
        ]
      }
    ]
  }
}
```

Check it worked:

```bash
localmem search "" --all          # nothing matches an empty query — use a real one
localmem stats                    # `by kind` should start showing `trace`
localmem audit                    # section 5 tells you which traces are never recalled
```

## Before you turn it on

- **It stores what the session said, unfiltered.** If a session discussed a secret, the
  summary can contain it. Everything stays on your disk, but it is still written down.
- **Traces accumulate.** Run `localmem audit` occasionally; section 5 lists old rows that
  have never once been recalled, and `localmem dedupe --review` drains the near-duplicate
  queue an automatic capture fills faster than typing does.
- **The payload shape is Claude Code's, not localmem's.** If a future version renames the
  field, the script above stores nothing and exits 0 rather than breaking your session. Run
  it by hand against a saved payload to check:
  `printf '%s' "$(cat payload.json)" | ~/.claude/hooks/localmem-capture.sh`
- **`jq` is required by the script, not by localmem.** localmem itself has three runtime
  dependencies and `jq` is not one of them.
