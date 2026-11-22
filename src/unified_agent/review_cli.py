"""CLI entry point (`unified-agent-review`) — deterministic driver over ``reviewer.py``.

This is the no-LLM cron layer described in ``docs/automation.md``: it wraps
``reviewer.reconcile_tasks`` / ``scan_for_reviewable_tasks`` / ``review_task`` /
``review_and_notify`` so a shell script can run the review pipeline on a schedule
without any model in the loop. Validate + hold: the pipeline never writes to the
remote — gates-green work is held for a human merge.

Exit codes: 0 = nothing to do or every candidate came back clean
(merged/dry-run-ok/validated/unchanged/merged-upstream/stale/timeout),
1 = at least one ``red-tier`` or ``failed`` verdict, 2 = configuration/usage error.

``timeout`` (task #1254) is deliberately NOT a fail verdict: it means the
pytest run in ``merge._run_pytest`` exceeded ``UA_REVIEW_PYTEST_TIMEOUT_S``
before finishing, not that a test went red. Counting it toward exit code 1
would recreate the false-FAIL problem this task fixed — cron would still
"surface as FAIL" for a run that never actually failed. It is still broken
out in the summary line (not folded into ``validated``/``unchanged``) so a
human scanning logs can see timeouts trending up. The pipeline retries a
timeout at the same branch-head sha up to ``UA_REVIEW_TIMEOUT_MAX_RETRIES``
times (default 2) before holding silently — see
``reviewer._timeout_count_at_sha`` — so this CLI never re-runs pytest or
notifies unboundedly for a suite that genuinely never finishes.

``label-held`` (task #1260) is a separate, explicitly opt-in subcommand, not
part of ``scan``: it is the one place this CLI writes to GitHub (a PR label,
via ``held_pr.label_held_task``), so it never runs unattended — see
``held_pr.py``'s module docstring for why.

``ledger poll`` (task #1867) runs no local pipeline: it asks the hub over its
HTTP API to run one Approval Ledger poll now, so a desktop can get an approval
row for a fresh push without SSH. It needs ``UA_HTTP_HUB_URL`` and an operator
bearer; the hub admits only a lifecycle operator or the verified operator, and
the poll only requests approval rows, never resolves one.

``record-read`` (task #2294) is the explicit writer into ``review_reads``: run
on the hub after a full-diff read (``--kind steward-read``), a human's own
full read (``--kind human``, a human ``--reader`` and ``--evidence-url``), or a
read of only what changed since an earlier full read (``--kind delta-read
--from <sha> --files ... --evidence-url ...``). An Approval Ledger approval is
not a read and writes no row. It refuses a head that is not the pull
request's live head, and a delta read the release gate would not count: no
full read at its base, a base that is not an ancestor, a comparison too large
to prove, or ``--files`` that omit a red-tier path the delta changed.
``--reader`` is the reading identity as stated by whoever runs the command on
the hub; a shell on the hub is already operator-trusted, so it is recorded,
not verified. On a hub client (``NO_LOCAL_MEMORY_DB``, or with ``--hub``) the
command posts to the hub instead (task #2407). There the bearer decides the
reader, and ``--reader`` is only a fence: the hub refuses the write when the
credential is somebody else's.

A recorded read publishes ``unified-agent/review-head`` for that head, locally
and over the hub (task #2410). The Approval Ledger poll publishes open pull
requests only, so without this a read recorded after the merge left a permanent
false ERROR on the merged head. The publish is best-effort and reported as
``review_head`` in the command's output; the row stands even when it fails.
"""

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import config, held_pr, post_init, reviewer, storage
from .delegation_ledger_log import append_review_ledger
from .storage import LiveStoreError, require_live_task_store

# Verdicts that count as a clean pass for the exit-code gate.
_OK_VERDICTS = {"merged", "dry-run-ok", "validated", "unchanged", "merged-upstream", "stale"}
# Verdicts that trip exit code 1 so cron logs surface as FAIL.
_FAIL_VERDICTS = {"red-tier", "failed"}
# Inconclusive — the gates never finished, so it's neither a clean pass nor a
# failure. Excluded from both sets above (see module docstring); broken out
# in the summary line for visibility instead.
_TIMEOUT_VERDICTS = {"timeout"}
# Held for batch CI (task #1787): no exact-head CI and no targeted tests, so
# the hub ran nothing. Neither a pass nor a failure; counted in the summary.
_AWAITING_VERDICTS = {"awaiting-ci"}


def _review_one(task_id: int, dry_run: bool, notify: bool) -> dict:
    """Run the pipeline on a single task, notifying unless suppressed.

    Notification delivery is already best-effort inside ``review_and_notify`` —
    a broken bridge is reported via the ``notify_error``/``notified`` fields on the
    result and never changes the verdict or raises.
    """
    if notify:
        return reviewer.review_and_notify(task_id, dry_run=dry_run)
    return reviewer.review_task(task_id, dry_run=dry_run)


def _format_text(results: list[dict]) -> str:
    """Render one notification line per task plus a summary line."""
    if not results:
        return "no tasks waiting for review"

    lines = [r["notification"] for r in results if r.get("notification")]

    def count(verdict: str) -> int:
        return sum(1 for r in results if r["verdict"] == verdict)

    lines.append(
        f"reviewed={len(results)} merged={count('merged')} red-tier={count('red-tier')} "
        f"failed={count('failed')} validated={count('validated')} "
        f"unchanged={count('unchanged')} merged-upstream={count('merged-upstream')} "
        f"stale={count('stale')} timeout={count('timeout')} "
        f"awaiting-ci={count('awaiting-ci')}"
    )
    return "\n".join(lines)


def _exit_code(results: list[dict]) -> int:
    if any(r["verdict"] in _FAIL_VERDICTS for r in results):
        return 1
    return 0


def _label_held(task_id: int, dry_run: bool, emit_json: bool) -> int:
    """Handler for ``label-held``: apply the GitHub `held` label if warranted.

    Exit codes: 0 = not held (nothing to do) or successfully labeled;
    1 = held-despite-green but the label write failed (actionable: check
    ``gh`` auth/PATH and that the ``held`` label exists on the repo), or the
    PR's checks could not be read, so held status is unknown;
    2 = the task's PR reference could not be resolved at all.
    """
    result = held_pr.label_held_task(task_id, dry_run=dry_run)
    if emit_json:
        print(json.dumps(result, indent=2))
    else:
        print(result["message"])
    if "pr_ref" not in result:
        return 2
    if result.get("inconclusive"):
        return 1
    if result["held"] and not result.get("ok", True):
        return 1
    return 0


def _queue(args) -> int:
    """`queue health|stale|sweep|close-merged`: JSON out. Park and fold have no CLI verb."""
    from . import queue_closure

    try:
        if args.action == "health":
            result = queue_closure.queue_health(args.stale_days)
        elif args.action == "stale":
            result = queue_closure.queue_stale(args.stale_days)
        elif args.action == "sweep":
            result = queue_closure.queue_sweep(args.stale_days)
        else:
            # UA_REVIEW_DRY_RUN=1 forces a dry run here too, as for scan and task.
            forced_dry = os.environ.get("UA_REVIEW_DRY_RUN") == "1"
            result = queue_closure.close_on_merge(dry_run=forced_dry or not args.apply)
    except ToolError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


#: Hub route behind ``ledger poll`` (task #1867).
LEDGER_POLL_PATH = "/api/approvals/ledger-poll"
# A poll reads every open PR and may post statuses; give it room.
_LEDGER_POLL_TIMEOUT_S = 180.0
#: Hub route behind ``record-read`` on a hub client (task #2407).
REVIEW_READS_PATH = "/api/review/reads"
# The hub asks GitHub for the live head and, for a delta read, a compare.
_RECORD_READ_TIMEOUT_S = 90.0


def _hub_url(args: argparse.Namespace) -> str | None:
    """The hub URL from ``--hub`` or the environment; prints why when there is none."""
    hub_url = (args.hub or os.environ.get("UA_HTTP_HUB_URL", "")).strip()
    if not hub_url:
        print(
            "error: set UA_HTTP_HUB_URL (or --hub) to the hub's URL ending in /mcp", file=sys.stderr
        )
        return None
    return hub_url


def _hub_post(hub_url: str, label: str, path: str, content: bytes, timeout: float):
    """POST ``content`` to a hub route as ``label``; return ``(status, body)``."""
    from .hub_client import HubClient, _platform_url, _read_response

    url = _platform_url(hub_url, path)
    client = HubClient(agent_label=label, hub_url=hub_url, allow_fallback=False)
    with httpx.Client(timeout=timeout, follow_redirects=False) as http:
        with http.stream("POST", url, headers=client._headers(), content=content) as response:
            return response.status_code, _read_response(response)


def _ledger_poll(args: argparse.Namespace) -> int:
    """POST the hub's poll-now route and print its answer.

    Exit 0 when the poll ran and no head reported an error, 1 when the hub
    refused or a head failed, 2 on configuration or transport errors.
    """
    from . import config, device_login
    from .hub_client import PlatformCliError

    hub_url = _hub_url(args)
    if hub_url is None:
        return 2
    try:
        label = (
            args.agent
            or config.executor_agent()
            or device_login.saved_signin_agent(hub_url)
            or config.operator_label()
        )
        status, body = _hub_post(hub_url, label, LEDGER_POLL_PATH, b"{}", _LEDGER_POLL_TIMEOUT_S)
    except (PlatformCliError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        print(f"error: could not reach the hub ({type(exc).__name__})", file=sys.stderr)
        return 2
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if status < 200 or status >= 300:
        detail = payload.get("error") if isinstance(payload, dict) else None
        print(f"error: hub refused the ledger poll (HTTP {status}): {detail}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("error: hub returned a non-object JSON payload", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    failed = [
        r
        for r in payload.get("results") or []
        if isinstance(r, dict) and r.get("status") in {"error", "unconfigured"}
    ]
    return 1 if failed or payload.get("status") != "polled" else 0


def _live_pr_head(repo: str, number: int) -> str | None:
    """The pull request's current head SHA from GitHub, or ``None``."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        res = subprocess.run(
            [gh, "pr", "view", str(number), "--repo", repo, "--json", "headRefOid"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if res.returncode != 0:
            return None
        head = json.loads(res.stdout).get("headRefOid")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None
    return head if isinstance(head, str) else None


def _delta_paths(repo: str, base_sha: str, head_sha: str) -> list[str] | None:
    """Paths changed in ``base_sha..head_sha`` per GitHub's compare, the same
    answer the release gate reads; ``None`` when ``base_sha`` is not an
    ancestor. Raises ``PreflightError`` when it cannot be proven."""
    from .shipping_preflight_cli import _changed_between

    return _changed_between(repo, base_sha, head_sha, subprocess.run)


def _delta_refusal(repo: str, from_sha: str, head_sha: str, files: list[str]) -> str | None:
    """Why the release gate would not count this delta read, or ``None``."""
    from .shipping_preflight_cli import PreflightError, _red_tier

    try:
        changed = _delta_paths(repo, from_sha, head_sha)
    except PreflightError as exc:
        return str(exc)
    if changed is None:
        return f"from {from_sha[:12]} is not an ancestor of head {head_sha[:12]}"
    missing = [path for path in _red_tier(changed) if path not in set(files)]
    if missing:
        return (
            f"the file list does not name red-tier {', '.join(missing)}, changed in "
            f"{from_sha[:12]}..{head_sha[:12]}; read it and list it"
        )
    return None


def explicit_read_refusal(**fields) -> str | None:
    """``review_reads.explicit_read_refusal`` with this module's GitHub lookups."""
    from . import review_reads

    return review_reads.explicit_read_refusal(
        **fields, live_head=_live_pr_head, delta_refusal=_delta_refusal
    )


@contextlib.contextmanager
def _service_env() -> Iterator[None]:
    """The hub's ``service.env`` under this process's environment, for one call.

    The hub process has it and an interactive hub shell does not, so without
    this the documented ``record-read`` reported publishing as off (task
    #2418). A variable the shell already sets wins, as it does for the hub.
    """
    from .service_env import parse_env_file

    added = {
        key: value
        for key, value in parse_env_file(config.data_home() / "service.env").items()
        if key not in os.environ
    }
    os.environ.update(added)
    try:
        yield
    finally:
        for key in added:
            os.environ.pop(key, None)


def _publish_review_head(repo: str, number: int, head_sha: str) -> dict | None:
    """Best-effort ``unified-agent/review-head`` publish for a read recorded here.

    The Approval Ledger poll publishes open heads only, so a read recorded
    after a merge had no publisher and its head kept a false ERROR (task
    #2410). Never raises: the row is already committed and is what matters.
    """
    from . import github_app_worker

    try:
        with _service_env():
            return github_app_worker.publish_review_head(
                repo=repo, number=number, head_sha=head_sha
            )
    except Exception as exc:  # noqa: BLE001 - reported, never allowed to undo the row
        print(f"warning: review-head publish failed: {type(exc).__name__}", file=sys.stderr)
        return {"status": "error", "error": type(exc).__name__}


def _record_read(args: argparse.Namespace, *, dry_run: bool = False) -> int:
    """`record-read`: write one steward or delta read. Exit 0 recorded, 2 refused.

    ``dry_run`` (``--dry-run`` or ``UA_REVIEW_DRY_RUN=1``) applies the same
    refusals and writes nothing.
    """
    from . import review_reads

    def refuse(reason: str) -> int:
        print(f"error: {reason}", file=sys.stderr)
        return 2

    reason = explicit_read_refusal(
        repo=args.repo,
        number=args.pr,
        head_sha=args.head,
        kind=args.kind,
        reader=args.reader,
        verdict=args.verdict,
        from_sha=args.from_sha,
        files=args.files,
        evidence_url=args.evidence_url,
    )
    if reason:
        return refuse(reason)
    if dry_run:
        print(
            json.dumps(
                {
                    "status": "dry-run",
                    "repo": args.repo,
                    "number": args.pr,
                    "head_sha": args.head,
                    "kind": args.kind,
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        row_id = review_reads.record(
            repo=args.repo,
            number=args.pr,
            head_sha=args.head,
            kind=args.kind,
            reader=args.reader,
            verdict=args.verdict,
            from_sha=args.from_sha,
            files=args.files,
            evidence_url=args.evidence_url,
        )
    except ValueError as exc:
        return refuse(str(exc))
    except sqlite3.Error as exc:
        return refuse(f"could not record the read: {exc}")
    print(
        json.dumps(
            {
                "status": "recorded",
                "id": row_id,
                "repo": args.repo,
                "number": args.pr,
                "head_sha": args.head,
                "kind": args.kind,
                "review_head": _publish_review_head(args.repo, args.pr, args.head),
            },
            sort_keys=True,
        )
    )
    return 0


def _record_read_remote(args: argparse.Namespace, *, dry_run: bool = False) -> int:
    """`record-read` from a hub client. Exit 0 recorded, 2 refused or unreachable."""
    from .hub_client import PlatformCliError

    hub_url = _hub_url(args)
    if hub_url is None:
        return 2
    payload: dict[str, object] = {
        "repo": args.repo,
        "number": args.pr,
        "head_sha": args.head,
        "kind": args.kind,
        "verdict": args.verdict,
        "expected_reader": args.reader,
        "dry_run": dry_run,
    }
    optional = {"from_sha": args.from_sha, "files": args.files, "evidence_url": args.evidence_url}
    payload.update({key: value for key, value in optional.items() if value is not None})
    try:
        status, body = _hub_post(
            hub_url,
            args.agent or args.reader,
            REVIEW_READS_PATH,
            json.dumps(payload).encode("utf-8"),
            _RECORD_READ_TIMEOUT_S,
        )
    except (PlatformCliError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        print(f"error: could not reach the hub ({type(exc).__name__})", file=sys.stderr)
        return 2
    try:
        answer = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        answer = None
    if status < 200 or status >= 300:
        detail = answer.get("error") if isinstance(answer, dict) else None
        print(f"error: hub refused the read (HTTP {status}): {detail}", file=sys.stderr)
        return 2
    if not isinstance(answer, dict):
        print("error: hub returned a non-object JSON payload", file=sys.stderr)
        return 2
    print(json.dumps(answer, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent-review",
        description="Deterministic driver over the automated review pipeline (reviewer.py).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="process every task annotated [waiting-for: review]")
    scan_p.add_argument(
        "--dry-run", action="store_true", help="run full validation but never merge"
    )
    scan_p.add_argument("--json", action="store_true", help="emit results as a JSON array")
    scan_p.add_argument(
        "--no-notify", action="store_true", help="skip the iMessage push (still printed)"
    )

    task_p = sub.add_parser("task", help="run the pipeline on a single task")
    task_p.add_argument("id", type=int, help="task id")
    task_p.add_argument(
        "--dry-run", action="store_true", help="run full validation but never merge"
    )
    task_p.add_argument("--json", action="store_true", help="emit the result as a JSON array")
    task_p.add_argument(
        "--no-notify", action="store_true", help="skip the iMessage push (still printed)"
    )

    label_p = sub.add_parser(
        "label-held",
        help="apply the GitHub 'held' label if the task's PR is held-despite-green (task #1260)",
    )
    label_p.add_argument("id", type=int, help="task id")
    label_p.add_argument(
        "--dry-run", action="store_true", help="report the verdict but never write the label"
    )
    label_p.add_argument("--json", action="store_true", help="emit the result as JSON")

    queue_p = sub.add_parser(
        "queue",
        help="queue closure reads and dry runs: health, stale, sweep, close-merged (task #1848)",
    )
    queue_p.add_argument(
        "action",
        choices=("health", "stale", "sweep", "close-merged"),
        help=(
            "health: open/parked/stale/unjudged-milestone counts; stale: the stale list; "
            "sweep: PROPOSED park/fold/close list (writes nothing); close-merged: close "
            "tasks whose single matching PR merged (dry run unless --apply)"
        ),
    )
    queue_p.add_argument(
        "--stale-days", type=int, default=None, help="staleness threshold (default 30)"
    )
    queue_p.add_argument(
        "--apply", action="store_true", help="close-merged only: perform the closes"
    )

    ledger_p = sub.add_parser(
        "ledger",
        help="Approval Ledger operator actions on the hub, over its HTTP API (task #1867)",
    )
    ledger_p.add_argument(
        "action",
        choices=("poll",),
        help="poll: run one Approval Ledger poll on the hub now (requests rows, never resolves)",
    )
    ledger_p.add_argument("--hub", help="hub URL ending in /mcp (default UA_HTTP_HUB_URL)")
    ledger_p.add_argument(
        "--agent",
        help="identity whose bearer to send (default UA_EXECUTOR_AGENT, the saved device "
        "sign-in, or the operator label)",
    )

    read_p = sub.add_parser(
        "record-read",
        help="record the steward's own read of a PR head for the release gate (task #2294)",
    )
    read_p.add_argument("--repo", required=True, help="owner/name")
    read_p.add_argument("--pr", type=int, required=True, help="pull request number")
    read_p.add_argument("--head", required=True, help="full SHA of the head that was read")
    read_p.add_argument(
        "--kind",
        choices=("steward-read", "human", "delta-read"),
        default="steward-read",
        help="steward-read: the full diff at --head; human: a human's own full read at "
        "--head (not a Ledger approval); delta-read: only --from..--head",
    )
    read_p.add_argument(
        "--from",
        dest="from_sha",
        help="delta-read only: the earlier head, which must already have a full read",
    )
    read_p.add_argument(
        "--files", nargs="*", default=None, help="the changed paths the read covered"
    )
    read_p.add_argument("--reader", required=True, help="the identity label that did the read")
    read_p.add_argument("--verdict", default="pass", help="short verdict (default: pass)")
    read_p.add_argument(
        "--evidence-url",
        help="where the read is written up (required for delta and human)",
    )
    read_p.add_argument(
        "--hub",
        help="post to this hub (URL ending in /mcp); the default on a host with no local store",
    )
    read_p.add_argument(
        "--agent", help="identity whose bearer to send over the hub (default: --reader)"
    )

    args = parser.parse_args(argv)

    if args.command == "ledger":
        # A remote hub call: no local task store is read or required.
        return _ledger_poll(args)

    # UA_REVIEW_DRY_RUN=1 is equivalent to --dry-run — the cron wrapper forwards it.
    dry_run = getattr(args, "dry_run", False) or os.environ.get("UA_REVIEW_DRY_RUN") == "1"

    if args.command == "record-read" and (
        args.hub or storage.no_local_memory_db_marker().is_file()
    ):
        # A hub client holds no store; the hub verifies the caller and writes.
        return _record_read_remote(args, dry_run=dry_run)

    # Fail closed on a retired / stub / empty UA_HOME before reconcile or scan
    # can surface the historical "no such table: tasks" / "file is not a
    # database" errors as soft warnings (task #561).
    try:
        require_live_task_store()
        if args.command not in ("label-held", "queue"):
            # Migrate only an existing authoritative task store, before any
            # review can reach the mission-governed local merge path (#1544).
            post_init.register_default_hooks()
            storage.init_db()
    except (LiveStoreError, storage.SchemaVersionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.command == "label-held":
        return _label_held(args.id, dry_run=dry_run, emit_json=args.json)

    if args.command == "queue":
        return _queue(args)

    if args.command == "record-read":
        return _record_read(args, dry_run=dry_run)

    notify = not args.no_notify
    results: list[dict] = []

    if args.command == "scan":
        # Queue hygiene first (task #729): release stale claims, demote aging
        # open tasks, auto-archive old done rows. Best-effort — a hygiene
        # failure never blocks the review pass, same posture as reconcile.
        try:
            from .queue_hygiene import run_hygiene_pass

            hygiene = run_hygiene_pass(send_notifications=notify, dry_run=dry_run)
            closed_on_merge = hygiene.get("closed_on_merge") or []
            touched = (
                len(hygiene["released"])
                + len(hygiene["demoted"])
                + len(hygiene["archived"])
                + len(closed_on_merge)
            )
            if touched and not args.json:
                print(
                    f"hygiene: released={len(hygiene['released'])} "
                    f"demoted={len(hygiene['demoted'])} archived={len(hygiene['archived'])} "
                    f"closed_on_merge={len(closed_on_merge)}"
                )
            if hygiene.get("close_on_merge_error"):
                print(
                    f"warning: close-on-merge failed: {hygiene['close_on_merge_error']}",
                    file=sys.stderr,
                )
            # Task #1497: surface the governed-workflow plan sweep, including
            # its failures, so an operator can see whether cleanup ran.
            plans = hygiene.get("workflow_plans") or {}
            liveness = hygiene.get("liveness") or {}
            if liveness.get("error"):
                print(f"warning: liveness sweep failed: {liveness['error']}", file=sys.stderr)
            elif (liveness.get("reported") or liveness.get("blocked")) and not args.json:
                print(
                    f"liveness ({liveness.get('mode')}): "
                    f"reported={len(liveness.get('reported') or [])} "
                    f"blocked={len(liveness.get('blocked') or [])}"
                )
            if plans.get("error"):
                print(f"warning: workflow plan sweep failed: {plans['error']}", file=sys.stderr)
            elif (plans.get("closed") or plans.get("unverifiable")) and not args.json:
                print(
                    f"workflow plans: closed={len(plans.get('closed') or [])} "
                    f"unverifiable={len(plans.get('unverifiable') or [])}"
                )
        except Exception as e:
            print(f"warning: hygiene pass failed: {e}", file=sys.stderr)

        # Reconcile first: tasks whose branch/PR already landed on origin/main
        # are marked done here and therefore drop out of the scan below.
        # Best-effort — a reconcile problem never blocks the review pass.
        # LiveStoreError is not swallowed: the preflight above already gated
        # the store; any later store error is still a config failure.
        try:
            results.extend(
                reviewer.reconcile_tasks(send_notifications=notify, dry_run=dry_run)["results"]
            )
        except (ToolError, LiveStoreError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"warning: reconcile step failed: {e}", file=sys.stderr)

        # Plan-approval consumer: surface plans awaiting approval and notify
        # once per plan-state change. Best-effort — never blocks the review pass.
        try:
            results.extend(
                reviewer.review_plan_approvals(send_notifications=notify, dry_run=dry_run)[
                    "results"
                ]
            )
        except (ToolError, LiveStoreError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"warning: plan-approval step failed: {e}", file=sys.stderr)

        # Delegation outcome review loop (task #1142): surface terminal
        # outcomes that still lack a `[review: …]` annotation. Report-only —
        # never blocks the code-review pass.
        try:
            awaiting = reviewer.scan_outcomes_awaiting_review()
            if awaiting.get("count") and not args.json:
                print(f"outcomes awaiting review: {awaiting['count']}")
                for o in awaiting.get("outcomes", [])[:10]:
                    print(
                        f"  - delegation #{o['delegation_id']} "
                        f"{o['from_agent']}→{o['to_agent']} ({o['status']})"
                    )
        except (ToolError, LiveStoreError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"warning: outcomes-awaiting-review step failed: {e}", file=sys.stderr)

        try:
            candidates = reviewer.scan_for_reviewable_tasks()["tasks"]
        except (ToolError, LiveStoreError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        task_ids = [t["id"] for t in candidates]
    else:
        task_ids = [args.id]

    if not task_ids and not results:
        if args.json:
            print(json.dumps([]))
        else:
            print("no tasks waiting for review")
        return 0

    for task_id in task_ids:
        try:
            results.append(_review_one(task_id, dry_run=dry_run, notify=notify))
        except ToolError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(_format_text(results))

    # Per-agent delegation ledger (UA #703): one vault line per state change.
    # Best-effort — a ledger failure is a warning, never a verdict change or
    # a different exit code.
    try:
        ledger = append_review_ledger(results)
        if ledger["appended"] and not args.json:
            print(
                f"ledger: {ledger['appended']} line(s) → {ledger['page']}"
                + (" (page created)" if ledger["created"] else "")
            )
    except Exception as e:
        print(f"warning: delegation ledger append failed: {e}", file=sys.stderr)

    return _exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
