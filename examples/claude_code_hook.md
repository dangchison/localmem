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

# The summary is passed to `localmem add` as an exec argument, and an argument list has a
# hard kernel limit (ARG_MAX, 1048576 bytes on macOS). Past it exec fails with E2BIG, the
# `|| exit 0` below swallows it, and the hook exits 0 having stored NOTHING — silently, on
# exactly the input this script is most likely to see. Measured on this machine: a 900 KB
# summary stored fine, 1.1 MB and 1.5 MB stored nothing at all. 100 KB is far beyond any
# useful trace and an order of magnitude below the limit, whether ${#summary} is counting
# characters or bytes. A truncated trace that says so beats a session quietly lost.
readonly LOCALMEM_MAX_SUMMARY_CHARS=100000
readonly LOCALMEM_TRUNCATION_MARKER="…[truncated by capture hook]"

# The noise gate. A Stop hook fires on EVERY session, so without a floor here the store
# fills with "Done." and "The tests pass." — permanent rows that teach nothing. Length is
# the only signal available without a model, and it was measured before it was chosen
# (.corp/localmem-v1/gate-d-capture.md): over a fixture of 10 trivial summaries and 8 that
# recorded a real lesson, the noise tops out at 61 characters and the real traces start at
# 120. A floor of 80 drops 10/10 noise and loses 0/8 real traces, with 19 characters of
# margin below and 40 above. The 40 this replaced let 9 of those 10 through.
#
# The fixture is synthetic — the real database had one row in it when this was measured —
# so treat 80 as provisional. `localmem audit` reports what is actually being stored.
readonly LOCALMEM_MIN_SUMMARY_CHARS=80

command -v localmem >/dev/null 2>&1 || exit 0
command -v jq >/dev/null 2>&1 || exit 0

payload=$(cat)

# Claude Code has moved this field between versions; try the current shape, then the
# transcript fallback. `// empty` keeps jq quiet when neither is present.
summary=$(printf '%s' "$payload" | jq -r '
  (.last_assistant_message // .response // .message.content // empty)
  | if type == "array" then map(select(.type == "text") | .text) | join("\n") else . end
' 2>/dev/null)

# Cap FIRST, before anything else looks at the summary — see the ARG_MAX note above. The
# marker is appended later, after the blank and length tests have judged the real content:
# a summary that is nothing but whitespace must stay nothing, not become a marker.
truncated=no
if [ "${#summary}" -gt "$LOCALMEM_MAX_SUMMARY_CHARS" ]; then
    summary=${summary:0:$LOCALMEM_MAX_SUMMARY_CHARS}
    truncated=yes
fi

# Nothing worth storing, or a one-liner like "done" — stop here.
#
# The blank test MUST stay a `case`. The obvious `[ -z "${summary//[[:space:]]/}" ]` is
# quadratic in bash 3.2.57 — what /usr/bin/env bash is on a stock macOS — whenever the text
# is whitespace-heavy, and a session summary quoting a log or a stack trace is exactly that.
# Measured on 3.2.57: 50 KB of log-shaped text did not finish that expansion in 30 seconds.
case "$summary" in
    *[![:space:]]*) ;;
    *) exit 0 ;;
esac
[ "${#summary}" -lt "$LOCALMEM_MIN_SUMMARY_CHARS" ] && exit 0

# Say so in the record itself: a trace that was cut should admit it rather than read as a
# complete summary that happens to stop mid-sentence.
if [ "$truncated" = yes ]; then
    summary="${summary}${LOCALMEM_TRUNCATION_MARKER}"
fi

# Workspace: let localmem detect it from the repository the session ran in.
cd "$(printf '%s' "$payload" | jq -r '.cwd // "."')" 2>/dev/null || exit 0

# The redundancy gate, and the reason it lives inside `localmem` rather than here.
#
# Deciding "have I already recorded this session, in other words?" needs the summary
# tokenized and compared against what is stored. localmem/dedup.py already does exactly
# that — normalize, tokenize, Jaccard — and re-implementing any of it in shell would be a
# second copy free to drift from the one the rest of the system decides on. So the hook
# asks the question instead of answering it: --if-novel makes the write conditional and
# skips it when an existing memory in this workspace overlaps by >= 0.25 (measured; see
# dedup.CAPTURE_JACCARD_THRESHOLD for why it is not tier 2's 0.7).
#
# Nothing is deleted or edited by this: the redundant summary is simply not written. The
# hook stays silent either way — stdout is discarded, and a skip is a success, not an
# error, so `|| exit 0` is still only there for the E2BIG case above.
localmem add "$summary" --kind trace --source claude-code-hook --if-novel >/dev/null 2>&1 || exit 0
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
localmem audit                    # section 5: never-recalled rows; section 7: lesson health
```

## Before you turn it on

- **It stores what the session said**, subject to the two gates below. If a session discussed
  a secret, the summary can contain it. Everything stays on your disk, but it is still written
  down.
- **Two gates decide whether a session is worth a row**, and both were measured before they
  were chosen (`.corp/localmem-v1/gate-d-capture.md`, `docs/design_decisions.md` §44–45):

  | gate | value | measured |
  |---|---|---|
  | minimum length | **80 characters** | drops 10/10 trivial summaries, loses 0/8 real traces; the 40 it replaced let 9/10 noise through |
  | redundancy (`--if-novel`) | **Jaccard ≥ 0.25** | skips 3/3 restatements, wrongly skips 0/8 novel traces; at the near-duplicate queue's 0.70 it would never fire at all |

  The redundancy gate **declines to write**; it never deletes or edits a stored memory. Both
  numbers come from a *synthetic* fixture — the database had one row in it when they were
  measured — so treat them as defaults and re-derive them from your own traces with
  `localmem audit` section 7, which reports the real distribution with the threshold marked.
- **A summary longer than 100,000 characters is cut**, and the stored trace ends with
  `…[truncated by capture hook]` so the record admits it. That cap is not tidiness: the
  summary is passed to `localmem add` as an exec argument, and past `ARG_MAX` (1 MiB on
  macOS) exec fails with `E2BIG`, which the script's `|| exit 0` would swallow — storing
  **nothing**, with no error anywhere. Measured before the cap existed: a 900 KB summary
  stored fine, 1.1 MB and 1.5 MB stored nothing at all.
- **Traces still accumulate, just far more slowly.** Run `localmem audit` occasionally:
  section 5 lists old rows that have never once been recalled, section 7 counts the traces a
  cleanup would remove, and `localmem dedupe --review` drains the near-duplicate queue an
  automatic capture fills faster than typing does. To act on that count:

  ```bash
  localmem gc --prune-traces 30 --dry-run   # what would go, by id
  localmem gc --prune-traces 30             # remove it
  ```

  This is opt-in and off by default — plain `localmem gc` deletes no memory at all. It also
  never removes a trace that another memory names as its replacement, however old.
- **The payload shape is Claude Code's, not localmem's.** If a future version renames the
  field, the script above stores nothing and exits 0 rather than breaking your session. Run
  it by hand against a saved payload to check:
  `printf '%s' "$(cat payload.json)" | ~/.claude/hooks/localmem-capture.sh`
- **`jq` is required by the script, not by localmem.** localmem itself has three runtime
  dependencies and `jq` is not one of them.
