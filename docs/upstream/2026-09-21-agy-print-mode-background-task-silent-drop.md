# DRAFT — unsent. Not filed with Google/Antigravity. Awaiting the operator's go-ahead.

> This file is a prepared submission for Google's Antigravity feedback
> channel. It has **not** been transmitted anywhere. Task #1143's boundary is
> explicit: write the draft, do not submit it. If you are an agent reading
> this file, do not file it — filing is the operator's call, not an agent's.

---

**Title:** `agy -p --output-format stream-json`: backgrounded shell command is
silently killed and dropped with a clean `SUCCESS` result — no log signal at
all in current builds

**Component:** Antigravity CLI (`agy`), print mode (`-p` / non-interactive)

**Version:** `agy 1.2.7` (`agy --version`)

**Severity (reporter's assessment):** High — silent data/work loss with no
error signal, in the CLI's primary non-interactive/automation mode.

## Summary

When a print-mode (`-p`) turn asks the agent to background a shell command
(e.g. `some-long-command &`) and end the turn without waiting for it, the
backgrounded process is reliably killed when the CLI's own process exits —
even when there is ample wall-clock time left for it to finish on its own.
The CLI reports the turn as `"status": "SUCCESS"` and prints no indication
that anything was terminated. Nothing in stdout or stderr — not a warning, not
an idle-wait notice, not an exit-time termination notice — distinguishes this
from a turn that genuinely had no background work at all.

This was previously somewhat observable: earlier builds apparently emitted
operator-facing text such as `root agent idle; waiting up to 5s for N
background task(s)` and `terminating N background task(s) on exit` when this
happened (our own tooling has a detector keyed on that text, filed against
our internal task #1158). On the currently installed 1.2.7, **none of that
text appears** in 5/5 reproduction runs — the loss is now completely silent.

## Reproduction

Minimal prompt (no MCP tools, no repo-specific setup beyond a scratch git
repo so the agent has read/write filesystem access):

```
In this git repo, run EXACTLY this shell command in the background (do not
wait for it in the foreground):

  (sleep 8; printf 'survived\n' > canary_marker.txt) &

Then reply with the single word LAUNCHED and end your turn. Do not wait for
the background job.
```

Invocation:

```
agy -p "<prompt above>" --output-format stream-json --dangerously-skip-permissions
```

(`--dangerously-skip-permissions` only removes the interactive
approve-each-tool-call prompt for `run_command`; the same result is expected
without it, modulo an extra approval step, and was the behavior originally
observed under `request-review` mode as well.)

**Expected:** either (a) `canary_marker.txt` exists 8 seconds after launch
(background job allowed to run to completion, independent of the parent
CLI process's lifetime), or (b) if the CLI intentionally tears down
backgrounded work at turn end, the `result` event or stderr says so
unambiguously (e.g. a `warnings` field, a non-`SUCCESS` status, or at minimum
a stable, documented log line) so a calling harness can detect it
programmatically instead of pattern-matching prose that changes across
releases without notice.

**Observed:** `canary_marker.txt` is never created. `result.status` is
`"SUCCESS"`. No teardown-related text appears anywhere in stdout or stderr.
The turn's own `run_command` tool-call step for the backgrounding command
itself reports `"state": "DONE"` (i.e. the CLI believes it successfully
executed `(sleep 8; ...) &`) — the failure is downstream of that, in whatever
kills the process group at CLI shutdown.

### Reproduction rate

5/5 runs (100%) over a single session, 2026-09-21, wall-clock CLI run times
ranging 9.9–21.1s (i.e. comfortably longer than the 8s the backgrounded job
needed, ruling out "the whole process just didn't live long enough").
Reproduction script and raw captured trace (one full run + the 5-run summary
table): `scripts/agy_teardown_canary.sh` and
`tests/fixtures/agy_teardown_1143/` in our repo
(`github.com/anonb3ll/unified-agent`, paths relative to repo root — happy to
attach the raw JSONL directly to the tracker issue instead/also).

## Impact

We drive `agy` non-interactively as one of several CLI-backed automation
backends (Claude Code, Codex, Cursor, Copilot, Hermes, `agy`) in an internal
task-execution daemon. Print mode is our only supported non-interactive
integration path for `agy` today. Because this failure mode produces a clean
`SUCCESS` with no distinguishing signal, any turn that (correctly, per your
own UX affordance of "you can background long-running work") uses `&` to
avoid blocking the turn on a slow command is at risk of that command's effect
silently not happening, while the harness driving `agy` has no way to tell
the difference between "genuinely nothing to background" and "backgrounded
something that got killed." We currently mitigate by not relying on print
mode when a task is expected to background work (routing those to a
PTY-driven interactive `agy` session instead, at the cost of losing the
simpler non-interactive contract) and by keeping `agy` off long-running/large
lanes in our scheduler as a blast-radius reduction — a policy workaround, not
a fix.

## Ask

1. Either (preferred) make backgrounded commands survive the parent CLI
   process's own exit in `-p` mode, so `&` behaves the way an interactive
   shell would — or, if backgrounding-then-exiting genuinely cannot be
   supported in one-shot print mode, document that explicitly (e.g. in
   `agy -p --help`) so callers don't reach for a pattern the CLI cannot honor.
2. If teardown is intentional, surface it in the structured `stream-json`
   output itself (a field on the `result` event, e.g. `"terminated_background_tasks": N`),
   not only in free-text log lines whose wording is not covered by any
   apparent stability contract and has already changed between the version
   this was first observed on and 1.2.7.
3. Confirm whether `--dangerously-skip-permissions` is expected to affect
   this at all (we did not test the `request-review` default in this pass,
   only skip-permissions, for speed — happy to add that datapoint if useful).

Reporter is glad to share the full raw stdout/stderr for all 5 runs, the
harness code invoking `agy`, and the internal task history against this issue
if that's useful for triage.
