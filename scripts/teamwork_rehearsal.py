#!/usr/bin/env python3
"""Isolated cross-adapter teamwork rehearsal (#1456), with retained evidence.

The executables are deterministic fixtures, not live Codex/Cursor models. The
production adapters, registered tool gates, SQLite transactions and event chain
are real. No ambient fleet environment or credentials enter the child process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTS = {"codex": "Desktop_Agent2", "cursor": "Desktop_Agent4"}

# Budgets sized for a CPU-starved 2-vCPU CI runner (#1644), where one fixture
# child spends most of its time importing the package. On 2 pinned cores a
# whole rehearsal took 63-167s, so the old 30s phase / 180s coordinator limits
# were too tight. A paused child sleeps well past its phase timeout, so the
# adapter timeout (not the child) always ends a paused phase.
PHASE_TIMEOUT_S = 60
PAUSE_TIMEOUT_S = 30
FIXTURE_PAUSE_S = 120
COORDINATOR_TIMEOUT_S = 360
# One JSON line per phase start and end, appended as the coordinator goes, so
# a launcher that has to kill a wedged coordinator can still name the phase
# that stalled and time the phases that finished (#1644).
ATTEMPTS_LOG = Path("runs") / "attempts.jsonl"


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def isolated_environment(root: Path) -> dict[str, str]:
    """Construct an allowlisted environment; never copy credential/config vars."""
    return {
        "PATH": os.defpath,
        "HOME": str(root / "fixture-home"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"),
        "PYTHONPATH": str(REPO / "src"),
        "PYTHONUNBUFFERED": "1",
        "UA_TEAMWORK_ROOT": str(root),
        # Native backend children use the production allowlist. Only this
        # synthetic installation opts its non-secret fixture settings in;
        # hub authority and credentials retain the unconditional deny rules.
        "UA_EXECUTOR_ENV_PASSTHROUGH": ",".join(
            (
                "PYTHONPATH",
                "PYTHONUNBUFFERED",
                "UA_TEAMWORK_ROOT",
                "UA_TEAMWORK_FAULT",
                "UA_DEV_ROOT",
                "UA_POLICY_FILE",
                "UA_ORG_REGISTRY_PATH",
                "UA_AGENT_LABELS",
                "UA_EVENT_BUS",
                "UA_EVENT_BUS_EMBED",
                "UA_STATE_SYNC",
                "UA_APPROVAL_BASELINE",
                "UA_DELEGATION_SPEC_ENFORCE",
                "UA_TRANSPORT",
                "HF_HOME",
                "HF_HUB_OFFLINE",
            )
        ),
        "UA_HOME": str(root / "store"),
        "UA_WORKSPACE": str(root / "workspace"),
        "UA_DEV_ROOT": str(root / "empty-dev"),
        "UA_VAULT_PATH": str(root / "vault"),
        "UA_POLICY_FILE": str(root / "policy.yaml"),
        "UA_ORG_REGISTRY_PATH": str(root / "org.yaml"),
        "UA_AGENT_LABELS": ":".join(AGENTS.values()),
        "UA_EVENT_BUS": "0",
        "UA_EVENT_BUS_EMBED": "0",
        "UA_STATE_SYNC": "0",
        "UA_APPROVAL_BASELINE": "0",
        "UA_DELEGATION_SPEC_ENFORCE": "0",
        "UA_ENFORCE_AUTH": "0",
        "UA_TRANSPORT": "stdio",
        "XDG_CONFIG_HOME": str(root / "config"),
        "HF_HOME": str(root / "model-cache"),
        "HF_HUB_OFFLINE": "1",
    }


def fenced_root() -> Path:
    root = Path(os.environ["UA_TEAMWORK_ROOT"]).resolve()
    require(
        (root / "fixture-only").read_text() == "teamwork-rehearsal-v1\n", "fixture marker missing"
    )
    for variable, child in (
        ("UA_HOME", "store"),
        ("UA_WORKSPACE", "workspace"),
        ("HOME", "fixture-home"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("UA_ORG_REGISTRY_PATH", "org.yaml"),
    ):
        require(Path(os.environ[variable]).resolve() == root / child, "isolation path mismatch")
    return root


def block_network(*_args, **_kwargs):
    raise OSError("teamwork rehearsal refuses outbound network")


def tool(name, **kwargs):
    from unified_agent.server import REGISTERED_TOOLS

    return REGISTERED_TOOLS[name](**kwargs)


def resolve(value, responses):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            found = responses
            for key in value["$ref"].split("."):
                found = found[int(key)] if isinstance(found, list) else found[key]
            return found
        return {key: resolve(item, responses) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, responses) for item in value]
    return value


def fixture_worker(adapter: str, argv: list[str]) -> None:
    """Run actual registered calls behind native CLI argv/JSONL contracts."""
    root = fenced_root()
    socket.socket.connect = block_network
    socket.socket.connect_ex = block_network
    socket.getaddrinfo = block_network
    prompt = argv[-1]  # Both production headless builders put the prompt last.
    request = json.loads(prompt)
    label = os.environ.get("UA_AGENT_LABEL")
    require(label == AGENTS[adapter], "backend did not bind its distinct identity")
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import config

    config.agent_identity_var.set(label)
    session = tool(
        "session_start",
        kind=f"{adapter}_run",
        agent=label,
        task_id=request.get("task_id"),
        external_session_id=request["phase"],
        recall_prior_packet=bool(request.get("task_id")),
    )
    responses = {"session": session}
    for call in request["calls"]:
        try:
            result = tool(call["tool"], **resolve(call["params"], responses))
        except ToolError as exc:
            if not call.get("expect_error"):
                raise
            expected_error = call["expect_error"]
            require(
                isinstance(expected_error, str) and expected_error in str(exc),
                "unexpected reason for tool refusal",
            )
            result = {"refused": True, "error": str(exc)}
        else:
            require(not call.get("expect_error"), "expected tool denial did not occur")
        if "verify_sha256" in call:
            actual = hashlib.sha256(result["content"].encode()).hexdigest()
            handoff = json.loads(resolve(call["handoff"], responses))
            require(
                actual == call["verify_sha256"] == handoff["sha256"],
                "receiver refused corrupt artifact",
            )
            require(
                handoff["correlation_id"] == request["correlation_id"],
                "receiver refused mismatched correlation",
            )
        responses[call["name"]] = result
    # This is diagnostic evidence of the crash boundary, never task authority.
    # Written atomically: the coordinator's kill_check fires on the marker's
    # existence and then parses it, so it must never see a partial file.
    marker = root / "runs" / f"{request['phase']}.json"
    staged = marker.with_name(marker.name + ".tmp")
    staged.write_text(json.dumps(responses, indent=2))
    os.replace(staged, marker)
    if request.get("pause"):
        time.sleep(FIXTURE_PAUSE_S)  # Adapter kill/timeout must stop this bounded child.
    tool("session_end", session_id=session["session_id"], summary=request["phase"])
    text = json.dumps(responses)
    if adapter == "codex":
        print(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        )
        print(json.dumps({"type": "turn.completed"}))
    else:
        print(json.dumps({"type": "result", "result": text}))


def call(name, tool_name, **params):
    return {"name": name, "tool": tool_name, "params": params}


def ref(path):
    return {"$ref": path}


def record_attempt(root: Path, entry: dict) -> None:
    """Append one attempt event and push it to disk before the phase goes on."""
    line = json.dumps({**entry, "at": datetime.now(UTC).isoformat()}) + "\n"
    with open(root / ATTEMPTS_LOG, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def read_attempts(root: Path) -> tuple[list[dict], list[dict]]:
    """Fold the attempts log into (phases, in_progress).

    A phase with a start and no end was running when the coordinator stopped.
    A torn final line (the coordinator killed mid-write) is skipped.
    """
    path = root / ATTEMPTS_LOG
    phases: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            phase = phases.setdefault(
                entry["phase"],
                {"phase": entry["phase"], "adapter": entry.get("adapter")},
            )
            if entry.get("event") == "start":
                phase["identity"] = entry.get("identity")
                phase["started_at"] = entry.get("at")
            elif entry.get("event") == "end":
                phase["status"] = entry.get("status")
                phase["elapsed_s"] = entry.get("elapsed_s")
                phase["ended_at"] = entry.get("at")
    rows = list(phases.values())
    return rows, [row for row in rows if "started_at" in row and "ended_at" not in row]


def store_diagnostics(db: Path) -> dict:
    """Task, run and delegation state for a failure report (#1644).

    Read with plain sqlite3 (read-only URI), never the product's storage
    layer, so collecting evidence cannot itself change the store. The file
    listing shows whether -wal/-shm survived, which separates "a write never
    happened" from "a committed write was lost with its WAL".
    """
    store = db.parent
    out: dict = {
        "sqlite_version": sqlite3.sqlite_version,
        "store_files": {
            path.name: path.stat().st_size for path in sorted(store.glob(db.name + "*"))
        }
        if store.is_dir()
        else {},
    }
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        out["store_error"] = f"{type(exc).__name__}: {exc}"
        return out
    conn.row_factory = sqlite3.Row
    try:
        out["tasks"] = [
            dict(row)
            for row in conn.execute(
                "SELECT id, status, claimed_by, claimed_at, lock_expires_at, current_run_id, "
                "current_run_agent, cancel_requested, updated_at FROM tasks ORDER BY id"
            )
        ]
        runs = []
        for row in conn.execute(
            "SELECT rowid, ulid, created_at, payload FROM event_log "
            "WHERE channel='agent.run.selfpickup' ORDER BY rowid"
        ):
            payload = json.loads(row["payload"])
            runs.append(
                {
                    "rowid": row["rowid"],
                    "ulid": row["ulid"],
                    "created_at": row["created_at"],
                    **{
                        key: payload.get(key)
                        for key in (
                            "kind",
                            "task_id",
                            "run_id",
                            "agent",
                            "observed_from",
                            "claimed_at",
                            "lock_expires_at",
                            "outcome",
                            "finished_by",
                            "reason",
                        )
                        if key in payload
                    },
                }
            )
        out["run_events"] = runs
        out["delegations"] = [
            dict(row)
            for row in conn.execute(
                "SELECT id, task_id, status, attempts, claim_generation, to_agent, updated_at "
                "FROM delegations ORDER BY id"
            )
        ]
        out["sessions"] = [
            dict(row)
            for row in conn.execute(
                "SELECT id, agent, task_id, status, external_session_id, started_at, ended_at "
                "FROM agent_sessions ORDER BY started_at"
            )
        ]
    except (sqlite3.Error, ValueError) as exc:
        out["store_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return out


def rehearse(attempts: list | None = None) -> dict:
    """Run every phase. ``attempts`` collects per-phase outcomes and timings."""
    root = fenced_root()
    socket.socket.connect = block_network
    socket.socket.connect_ex = block_network
    socket.getaddrinfo = block_network
    from unified_agent import config, events, post_init, run_records, storage
    from unified_agent.executor.backends import CodexBackend, CursorBackend

    post_init.init_db()  # storage.init_db() plus the hub's post-init hooks
    storage.join_init_background_threads()
    backends = {
        "codex": CodexBackend(codex_bin=str(root / "codex-fixture"), sandbox="workspace-write"),
        "cursor": CursorBackend(cursor_bin=str(root / "cursor-fixture"), skip_permissions=False),
    }
    attempts = [] if attempts is None else attempts

    def execute(
        adapter, phase, calls, task_id=None, pause=False, interrupt=False, correlation_id=None
    ):
        marker = root / "runs" / f"{phase}.json"
        started = time.monotonic()
        attempt = {"phase": phase, "adapter": adapter, "identity": AGENTS[adapter]}
        attempts.append(attempt)
        record_attempt(root, {"event": "start", **attempt})
        try:
            result = backends[adapter].run(
                prompt=json.dumps(
                    {
                        "phase": phase,
                        "calls": calls,
                        "task_id": task_id,
                        "pause": pause,
                        "correlation_id": correlation_id,
                    }
                ),
                cwd=".",
                session_dir=str(root / "runs"),
                session_id=phase,
                agent_label=AGENTS[adapter],
                timeout_s=PAUSE_TIMEOUT_S if pause and not interrupt else PHASE_TIMEOUT_S,
                kill_check=(lambda: marker.exists()) if interrupt else None,
            )
            attempt["status"] = result["status"]
        except Exception as exc:
            attempt["status"] = f"raised: {type(exc).__name__}"
            raise
        finally:
            attempt["elapsed_s"] = round(time.monotonic() - started, 2)
            record_attempt(
                root,
                {
                    "event": "end",
                    "phase": phase,
                    "adapter": adapter,
                    "status": attempt.get("status"),
                    "elapsed_s": attempt["elapsed_s"],
                },
            )
        expected = "killed" if interrupt else "timeout" if pause else "success"
        require(result["status"] == expected, f"{phase}: expected {expected}, got {result}")
        if pause:
            require(
                marker.exists(), f"{phase}: child stopped before the designated durable boundary"
            )
        return json.loads(marker.read_text()) if pause else json.loads(result["text"])

    def planning(name, action, **params):
        return call(name, "plan_manage", action=action, params=params)

    bootstrap = execute(
        "codex",
        "bootstrap",
        [
            call(
                "goal",
                "goal_manage",
                action="add",
                params={"title": "Synthetic two-harness teamwork", "author": AGENTS["codex"]},
            ),
            call("task", "task_add", title="Synthetic verifier", project="unified-agent"),
            call(
                "delegated",
                "delegation_manage",
                action="create",
                params={
                    "task_spec": "Synthetic producer: deliver one result for peer acknowledgment",
                    "from_agent": AGENTS["codex"],
                    "to_agent": AGENTS["cursor"],
                    "parent_task_id": ref("task.id"),
                    "callback": "none",
                },
            ),
            planning(
                "plan",
                "create",
                goal="Produce and acknowledge one result",
                steps=["produce", "acknowledge"],
                author=AGENTS["codex"],
            ),
            planning(
                "bound",
                "joint_bind",
                plan_id=ref("plan.plan_id"),
                goal_id=ref("goal.goal.id"),
                org_unit_id="platform-engineering",
                author=AGENTS["codex"],
            ),
            call(
                "proposal_comment",
                "task_comment",
                action="append",
                params={
                    "task_id": ref("task.id"),
                    "body": "Propose recoverable teamwork steps",
                    "author": AGENTS["codex"],
                    "refs": {"plan_id": ref("plan.plan_id")},
                },
            ),
            planning(
                "proposed",
                "joint_propose",
                comment_id=ref("proposal_comment.comment.id"),
                plan_id=ref("plan.plan_id"),
                expected_revision=0,
                steps=[
                    {"description": "produce one result", "owner": AGENTS["cursor"]},
                    {
                        "description": "verify and acknowledge",
                        "owner": AGENTS["codex"],
                        "depends_on": [0],
                    },
                ],
                author=AGENTS["codex"],
            ),
        ],
    )
    task_a = bootstrap["task"]["id"]
    task_b = bootstrap["delegated"]["task_id"]
    delegation = bootstrap["delegated"]["delegation_id"]
    plan = bootstrap["plan"]["plan_id"]
    accepted = execute(
        "cursor",
        "peer-review",
        [
            call(
                "review_comment",
                "task_comment",
                action="append",
                params={
                    "task_id": task_a,
                    "body": "Peer accepts the recovery plan",
                    "author": AGENTS["cursor"],
                    "refs": {"plan_id": plan},
                },
            ),
            planning(
                "accepted",
                "joint_review",
                comment_id=ref("review_comment.comment.id"),
                plan_id=plan,
                expected_revision=1,
                decision="accept",
                author=AGENTS["cursor"],
            ),
        ],
    )
    require(accepted["accepted"]["revision"] == 2, "peer review did not apply the shared revision")
    stale = planning(
        "conflict",
        "joint_propose",
        comment_id=bootstrap["proposal_comment"]["comment"]["id"],
        plan_id=plan,
        expected_revision=0,
        steps=["stale"],
        author=AGENTS["codex"],
    )
    stale["expect_error"] = "joint revision conflict"
    execute("codex", "stale-plan-refused", [stale])

    # Separate processes race for their different tasks using the atomic tool.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                execute,
                adapter,
                f"claim-{adapter}",
                [call("claim", "task_claim", id=task_id, agent=AGENTS[adapter])],
                task_id,
            )
            for adapter, task_id in (("codex", task_a), ("cursor", task_b))
        ]
        claimed_a, claimed_b = [future.result() for future in futures]
    run_a = claimed_a["claim"]["current_run_id"]
    run_b = claimed_b["claim"]["current_run_id"]
    require(run_a and run_b and run_a != run_b, "claims lack distinct durable run tokens")
    wrong = call("claim", "task_claim", id=task_a, agent=AGENTS["cursor"])
    competing = execute("cursor", "competing-claim-refused", [wrong])
    require(
        competing["claim"].get("claimed") is False
        and competing["claim"]["safe_wait"]["claimed_by"] == AGENTS["codex"],
        "competing claim did not return safe_wait",
    )
    initial = execute(
        "cursor",
        "claim-delegation",
        [
            call(
                "claim",
                "delegation_manage",
                action="claim",
                params={"id": delegation, "agent": AGENTS["cursor"]},
            )
        ],
        task_b,
    )
    generation = initial["claim"]["delegation"]["claim_generation"]
    require(initial["claim"]["claimed"], "initial delegation claim failed")
    interrupted = execute(
        "cursor",
        "interrupted",
        [planning("recover", "joint_recover", plan_id=plan, author=AGENTS["cursor"])],
        task_b,
        pause=True,
        interrupt=True,
    )
    recovery = execute(
        "cursor",
        "recover-interrupt",
        [
            call(
                "end",
                "session_end",
                session_id=interrupted["session"]["session_id"],
                status="abandoned",
                summary="fixture interrupted before output",
            ),
            call(
                "retry",
                "delegation_manage",
                action="retry",
                params={
                    "id": delegation,
                    "agent": AGENTS["cursor"],
                    "expected_claim_generation": generation,
                    "expected_run_id": run_b,
                    "task_id": task_b,
                    "clear_backoff": True,
                    "reason": "fixture interrupt",
                },
            ),
            call(
                "claim",
                "delegation_manage",
                action="claim",
                params={"id": delegation, "agent": AGENTS["cursor"]},
            ),
            call("renew", "task_claim", id=task_b, agent=AGENTS["cursor"], expected_run_id=run_b),
        ],
        task_b,
    )
    require(
        recovery["session"]["recalled_from_session_id"] == interrupted["session"]["session_id"],
        "cold recovery recalled the wrong interrupted session",
    )
    new_generation = recovery["claim"]["delegation"]["claim_generation"]
    require(
        recovery["retry"]["retried"] and new_generation > generation,
        "retry failed to fence the prior worker",
    )
    require(recovery["renew"]["current_run_id"] == run_b, "live renewal created a duplicate run")
    fences = execute(
        "cursor",
        "stale-and-duplicate-refused",
        [
            call(
                "stale",
                "delegation_manage",
                action="complete",
                params={
                    "id": delegation,
                    "agent": AGENTS["cursor"],
                    "expected_claim_generation": generation,
                    "expected_run_id": run_b,
                    "task_id": task_b,
                },
            ),
            call(
                "duplicate",
                "delegation_manage",
                action="claim",
                params={"id": delegation, "agent": AGENTS["cursor"]},
            ),
        ],
        task_b,
    )
    require(
        not fences["stale"]["completed"] and not fences["duplicate"]["claimed"],
        "duplicate/stale worker was admitted",
    )

    correlation = events.new_ulid()
    message_id = events.new_ulid()
    payload = {
        "schema": "teamwork-output-v1",
        "task_id": task_b,
        "run_id": run_b,
        "claim_generation": new_generation,
        "correlation_id": correlation,
        "result": {"value": 42},
    }
    body = json.dumps(payload, sort_keys=True)
    sha = hashlib.sha256(body.encode()).hexdigest()
    message = {
        "act": "handoff",
        "to_agent": AGENTS["codex"],
        "body": json.dumps({**payload, "artifact": "result.json", "sha256": sha}),
        "conversation_id": correlation,
        "message_id": message_id,
        "task_id": task_b,
        "delegation_id": delegation,
    }
    finish = {
        "id": task_b,
        "status": "waiting_review",
        "worktree": False,
        "actor": AGENTS["cursor"],
        "expected_run_id": run_b,
        "expected_delegation_id": delegation,
        "expected_claim_generation": new_generation,
        "append_note": f"[synthetic-output: {correlation}]",
        "idempotency_key": f"finish-{correlation}",
    }
    completion = {
        "id": delegation,
        "agent": AGENTS["cursor"],
        "expected_claim_generation": new_generation,
        "expected_run_id": run_b,
        "task_id": task_b,
        "result": "delivered synthetic output",
    }
    timed_out = execute(
        "cursor",
        "lost-response",
        [
            call(
                "artifact",
                "fs_write",
                path="result.json",
                content=body + "corrupt"
                if os.environ.get("UA_TEAMWORK_FAULT") == "corrupt-artifact"
                else body,
            ),
            planning("claim_step", "claim", plan_id=plan, step_idx=0, agent=AGENTS["cursor"]),
            planning(
                "handoff",
                "handoff",
                plan_id=plan,
                step_idx=0,
                outputs={
                    "artifact": "result.json",
                    "sha256": sha,
                    "message_id": message_id,
                    "correlation_id": correlation,
                },
                agent=AGENTS["cursor"],
            ),
            call("message", "message_send", **message),
            call("finish", "task_update", **finish),
            call("complete", "delegation_manage", action="complete", params=completion),
        ],
        task_b,
        pause=True,
    )
    require(timed_out["complete"]["completed"], "durable completion did not precede lost response")
    replay = execute(
        "cursor",
        "replay-lost-response",
        [
            call(
                "end",
                "session_end",
                session_id=timed_out["session"]["session_id"],
                status="abandoned",
                summary="response lost after durable commit",
            ),
            call("message", "message_send", **message),
            call("finish", "task_update", **finish),
            call("complete", "delegation_manage", action="complete", params=completion),
        ],
        task_b,
    )
    require(
        replay["message"]["duplicate"] and not replay["complete"]["completed"],
        "retry duplicated a durable effect",
    )
    require(
        replay["session"]["recalled_from_session_id"] == timed_out["session"]["session_id"],
        "restart did not recall its prior durable session",
    )
    peer = execute(
        "codex",
        "receive-and-ack",
        [
            planning("recover", "joint_recover", plan_id=plan, author=AGENTS["codex"]),
            call("delivery", "message_list", conversation_id=correlation, to_agent=AGENTS["codex"]),
            {
                **call("artifact", "fs_read", path="result.json"),
                "verify_sha256": sha,
                "handoff": ref("delivery.messages.0.body"),
            },
            call(
                "ack",
                "message_send",
                act="done",
                to_agent=AGENTS["cursor"],
                body=json.dumps(
                    {
                        "accepted_message_id": message_id,
                        "sha256": sha,
                        "correlation_id": correlation,
                    }
                ),
                conversation_id=correlation,
                reply_to=message_id,
                task_id=task_b,
                delegation_id=delegation,
            ),
            planning("claim_step", "claim", plan_id=plan, step_idx=1, agent=AGENTS["codex"]),
            planning(
                "handoff",
                "handoff",
                plan_id=plan,
                step_idx=1,
                outputs={"acknowledged_message_id": message_id, "sha256": sha},
                agent=AGENTS["codex"],
            ),
            call(
                "finish",
                "task_update",
                id=task_a,
                status="waiting_review",
                worktree=False,
                actor=AGENTS["codex"],
                expected_run_id=run_a,
            ),
        ],
        task_a,
        correlation_id=correlation,
    )
    require(peer["handoff"]["plan"]["status"] == "done", "shared plan failed to complete")
    require(
        peer["recover"]["steps"][0]["outputs"]["message_id"] == message_id,
        "cold recovery lost structured handoff",
    )
    delivered = peer["delivery"]["messages"]
    require(
        len(delivered) == 1 and delivered[0]["message_id"] == message_id,
        "delivery was missing or duplicated",
    )
    require(
        hashlib.sha256((root / "workspace/result.json").read_bytes()).hexdigest() == sha,
        "artifact differs from its handoff digest",
    )
    config.agent_identity_var.set(AGENTS["cursor"])
    acknowledged = tool("message_list", conversation_id=correlation, to_agent=AGENTS["cursor"])[
        "messages"
    ]
    require(
        len(acknowledged) == 1 and acknowledged[0]["reply_to"] == message_id,
        "acknowledgment lost its correlation",
    )
    history = run_records.run_history(limit=100)["runs"]
    require(
        len(history) == 2 and all(row["outcome"] == "handed_off" for row in history),
        "run history duplicated or lacks closeout",
    )
    chain = events.verify_event_chain()
    require(
        chain["verified"] and chain["status"] == "valid", "event evidence chain failed verification"
    )
    log = events.query_events(limit=1000)
    (root / "events.json").write_text(json.dumps(log, indent=2))
    with storage.connect() as conn:
        require(
            conn.execute("SELECT COUNT(*) FROM tasks WHERE current_run_id IS NOT NULL").fetchone()[
                0
            ]
            == 0,
            "run left open",
        )
        require(
            conn.execute("SELECT notes FROM tasks WHERE id=?", (task_b,))
            .fetchone()[0]
            .count(f"[synthetic-output: {correlation}]")
            == 1,
            "finish duplicated output",
        )
        require(
            conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE channel='agent.run.selfpickup' AND json_extract(payload,'$.kind')='selfpickup.finished'"
            ).fetchone()[0]
            == 2,
            "finish receipts duplicated",
        )
        sessions = [
            dict(row)
            for row in conn.execute(
                "SELECT id,agent,kind,task_id,status,external_session_id FROM agent_sessions ORDER BY started_at"
            )
        ]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        "schema": "teamwork-rehearsal-v1",
        "status": "pass",
        "scope": "isolated deterministic CLI fixtures through production adapters and registered tools",
        "live_models_verified": False,
        "live_a2a_verified": False,
        "root": str(root),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checks": [
            "distinct adapter identities",
            "shared peer-reviewed plan",
            "stale plan refused",
            "parallel distinct task claims",
            "competing claim refused",
            "actual child interruption",
            "generation-fenced retry",
            "stale/duplicate delegation refused",
            "durable handoff before timeout",
            "idempotent response-loss replay",
            "cold-session recall",
            "artifact verified before ACK",
            "correlated delivery and ACK",
            "one finish per run",
            "valid durable event chain",
        ],
        "plan_id": plan,
        "goal_id": bootstrap["goal"]["goal"]["id"],
        "task_ids": [task_a, task_b],
        "run_ids": [run_a, run_b],
        "delegation_id": delegation,
        "claim_generations": [generation, new_generation],
        "correlation_id": correlation,
        "handoff_message_id": message_id,
        "ack_message_id": acknowledged[0]["message_id"],
        "artifact_sha256": sha,
        "run_history": history,
        "event_chain": chain,
        "attempts": attempts,
        "sessions": sessions,
        "evidence": ["store/memory.db", "events.json", "workspace/result.json", "runs/"],
    }


def failure_report(root: Path, error: str, attempts: list | None = None) -> dict:
    """A failing report that names its cause: phase timings plus store state."""
    return {
        "schema": "teamwork-rehearsal-v1",
        "status": "fail",
        "root": str(root),
        "live_models_verified": False,
        "live_a2a_verified": False,
        "error": error,
        "diagnostics": {
            "phases": attempts or [],
            **store_diagnostics(root / "store" / "memory.db"),
        },
    }


def coordinator_timeout_report(root: Path, stderr: str) -> dict:
    """The report for a coordinator the launcher had to kill (#1644).

    The coordinator never wrote report.json, so rebuild what it knew from disk:
    the persisted attempts log (finished phases with timings, and the phase
    still running), the phase markers, the store state and its stderr tail.
    """
    phases, in_progress = read_attempts(root)
    report = failure_report(root, f"coordinator exceeded {COORDINATOR_TIMEOUT_S}s", phases)
    report["diagnostics"]["phase_in_progress"] = in_progress[-1] if in_progress else None
    report["diagnostics"]["completed_phase_markers"] = sorted(
        path.stem for path in (root / "runs").glob("*.json")
    )
    report["diagnostics"]["coordinator_stderr_tail"] = stderr[-4000:]
    return report


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--fixture-harness":
        try:
            fixture_worker(sys.argv[2], sys.argv[3:])
        except Exception as exc:
            event = (
                {"type": "turn.failed", "error": {"message": f"{type(exc).__name__}: {exc}"}}
                if sys.argv[2] == "codex"
                else {"type": "error", "message": str(exc)}
            )
            print(json.dumps(event))
            return 1
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--isolated-coordinator":
        root = fenced_root()
        attempts: list = []
        try:
            report = rehearse(attempts)
        except Exception as exc:
            report = failure_report(root, str(exc), attempts)
        (root / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
        return 0 if report["status"] == "pass" else 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fault",
        choices=["corrupt-artifact", "deny-claim"],
        help="negative control: must produce a failing report",
    )
    parser.add_argument("--out", type=Path, help="new directory for retained synthetic evidence")
    args = parser.parse_args()
    if args.out:
        root = args.out.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        root = Path(tempfile.mkdtemp(prefix="ua-teamwork-"))
    for directory in (
        "workspace",
        "runs",
        "empty-dev",
        "model-cache",
        "fixture-home",
        "cache",
        "data",
    ):
        (root / directory).mkdir()
    (root / "fixture-only").write_text("teamwork-rehearsal-v1\n")
    (root / "policy.yaml").write_text(
        "rules:\n  - name: fixture-deny-claim\n    type: tool\n    tool: task_claim\n    action: deny\n"
        if args.fault == "deny-claim"
        else "rules: []\n"
    )
    # The rehearsal supplies its own installation chart; a neutral product
    # starter must not enroll the harnesses or infer their shared office.
    (root / "org.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "offices": [
                    {
                        "id": "platform-engineering",
                        "name": "Synthetic platform team",
                        "lead": AGENTS["codex"],
                        "members": list(AGENTS.values()),
                        "owned_epics": [],
                        "default_lanes": ["dispatch-ready"],
                        "decision_rights": ["Complete synthetic teamwork rehearsal"],
                    }
                ],
            }
        )
    )
    for adapter in AGENTS:
        script = root / f"{adapter}-fixture"
        script.write_text(
            f"#!{sys.executable}\nimport os\nos.execv({sys.executable!r}, [{sys.executable!r}, {str(Path(__file__).resolve())!r}, '--fixture-harness', {adapter!r}, * __import__('sys').argv[1:]])\n"
        )
        script.chmod(0o700)
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--isolated-coordinator"],
            env={**isolated_environment(root), "UA_TEAMWORK_FAULT": args.fault or ""},
            capture_output=True,
            text=True,
            timeout=COORDINATOR_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else exc.stderr or ""
        )
        report = coordinator_timeout_report(root, stderr)
        (root / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
        return 1
    if result.returncode:
        print(result.stderr[-4000:], file=sys.stderr)
    print(result.stdout.strip() or json.dumps({"status": "fail", "root": str(root)}))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
