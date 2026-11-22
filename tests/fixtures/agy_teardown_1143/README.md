# agy print-mode background-task silent drop — captured evidence (task #1143)

Captured 2026-09-21 against the live, locally installed `agy` binary
(`agy --version` → `1.2.7`) using the existing canary,
`scripts/agy_teardown_canary.sh` (task #1172), with no arguments (default
`--agy-bin agy`, throwaway `mktemp -d` workdir per run). The canary's prompt
asks agy to background `(sleep 8; printf 'survived\n' > canary_marker.txt) &`
and reply `LAUNCHED` without waiting.

## Result: 5/5 runs (100%) lost the backgrounded write, with NO detectable text signature

| run | `result.status` | `duration_seconds` | marker written? | teardown text seen? |
|---|---|---|---|---|
| 1 | SUCCESS | 15.06 | no | no |
| 2 | SUCCESS | 9.88  | no | no |
| 3 | SUCCESS | 21.07 | no | no |
| 4 | SUCCESS | 16.21 | no | no |
| 5 | SUCCESS | 12.79 | no | no |

`live_1.2.7_run1.stdout`/`.stderr` in this directory are the raw captured
`agy -p ... --output-format stream-json` output for run 1, byte-for-byte,
with one redaction: the absolute local scratch workdir path in the `init`
event's `cwd` field and the `pwd`/`git rev-parse` tool-call output have been
replaced with the placeholder `/tmp/agy-teardown-canary.REDACTED/run1` (it
carried a local username and a session-scoped path, neither relevant to the
finding). Nothing else is present — no tokens, no secrets, no other PII.
Runs 2-5 are not committed (same shape, different `conversation_id`/timings);
the summary table above is the full record.

## Why this is a *new*, more severe variant of the #1158 race

Task #1158 documented agy print-mode's background-task teardown by its text
signature: `root agent idle; waiting up to 5s for N background task(s)`,
`terminating N background task(s)(?: on exit)?`, `Print mode: drain grace
period expired`, or `All your background tasks have been stopped`
(`detect_agy_background_teardown`,
`src/unified_agent/executor/backends.py:51-82`). That signature is what the
executor's `#1158`/`#1172` handling (same file, `AgyBackend._invoke_once` /
`AgyBackend.run`) keys off of to convert an apparent `SUCCESS` into an
`agy_background_teardown` error and, when the worktree is dirty, drive an
auto-resume.

**On the currently installed 1.2.7, none of the four signature phrases appear
anywhere in stdout or stderr, in any of the 5 runs** — confirmed by running
the shipped detector directly against the captured trace:

```
>>> from unified_agent.executor import backends
>>> out = open("live_1.2.7_run1.stdout").read()
>>> backends.detect_agy_background_teardown(out, "")
None
>>> backends.parse_agy_stream_json(out)["error"]
None
```

The CLI simply drops the backgrounded job and reports a clean `SUCCESS`
result — at capture time `AgyBackend.run()` returned `status: "success"` for
this attempt (see "Executor status since task #1247" below for the current
behavior). That is a regression relative to what #1158's detection assumed: the
CLI apparently no longer emits *any* teardown-related log text for this
failure mode (at least not on the two-tool-call-then-reply shape the canary
exercises), which makes it strictly worse than the originally observed race —
silent data loss with no recorded signal, rather than a detectable teardown
that the executor already knows how to fail loudly on.

`tests/test_agy_teardown_repro.py` replays this fixture through the
shipped parsing/detection functions (no live `agy` binary required) and pins
this exact gap down as a regression-documentation test.

**Executor status since task #1247.** The two text-level checks above still
return `None` on this trace, but they are no longer the executor's only
check. `backends.detect_agy_unconfirmed_background_launch` reads the session's
own tool calls and flags the `DONE` `run_command` step whose `CommandLine`
backgrounds work with an unquoted `&`. `AgyBackend._invoke_once` applies it, so
`AgyBackend.run()` now returns `status: "error"` with an
`agy_background_teardown` reason for this trace
(`tests/test_agy_false_success.py`).

**Canary verdict (task #1791).** At capture time the canary reported this
trace as `ERROR ... no_marker no_teardown_signature`. It now reports
`KILLED ... teardown=0 silent=1`: agy exited 0, the last `result` event says
`SUCCESS`, a `run_command` step whose `CommandLine` is exactly the canary's
launch command is `DONE`, and the marker is missing after the wait. agy 1.2.14
shows the same shape. `TestAgyTeardownCanarySilentLoss` in
`tests/test_agy_teardown_resume.py` replays `live_1.2.7_run1.stdout` through
the canary with a fake `agy` binary.

## Scope note

This directory and its test only *document* the gap. Closing it required
editing `src/unified_agent/executor/backends.py`, which was out of scope for
task #1143 (owned by another agent per the task's file-ownership list). The
follow-up, task #1247, added the structural detector described above.
