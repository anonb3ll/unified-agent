# Bug worker dogfood: task #487 (2026-08-21)

Live verification that the #488 SYSTEM bug-to-patch worker correctly handles
the original dogfood target (#487) **after** that bug was merged — the worker
must not re-claim closed tickets.

## Background

- **#487** — Hermes MCP circuit breaker tripped on `CallToolResult.isError`
  (merged PR #217, `e7e3833`, status `done`).
- **#488** — Bug worker pipeline (merged PR #230). Synthetic E2E in
  `tests/test_bug_worker.py`; live dogfood on #487 deferred until this run.

## Live evidence (agent-cursor, 2026-08-21)

Commands run from `~/dev/unified_agent` @ `main`/`7ba4385` (hub not restarted;
CLI uses installed package):

```bash
uv run unified-agent-bug-worker status
# enabled=False  (default kill switch — expected)

uv run unified-agent-bug-worker discover
# no eligible SYSTEM bug tickets

uv run python -c "
from unified_agent.tools.bug_worker import bug_worker_check_eligibility
print(bug_worker_check_eligibility(task_id=487))
"
# {'task_id': 487, 'eligible': False, 'reason': "status 'done' is not claimable"}
```

### Interpretation

| Check | Result | Pass criteria |
| ----- | ------ | ------------- |
| Kill switch default | `UA_BUG_WORKER_ENABLED=0` | Worker off unless explicitly enabled |
| Discover frontier | Empty queue | No open SYSTEM bugs with markers |
| #487 eligibility | Rejected (`done`) | Merged bugs never re-enter worker |

#487 retains correct portfolio markers (`stream: SYSTEM`, `bug: true`,
`worker-eligible: true`) but is correctly **ineligible** because status is
`done`. This is the intended dogfood outcome: the worker gates on lifecycle
state, not marker presence alone.

## Automated regression

`tests/test_bug_worker_dogfood.py::test_task_487_markers_reject_done_status`
encodes the #487 notes shape and asserts `is_system_bug_eligible` rejects a
`done` task while accepting an `open` twin.

## Boundaries observed

- No auto-merge, no hub restart, no `UA_BUG_WORKER_ENABLED=1` production enable.
- No re-opening #487 for worker exercise.
