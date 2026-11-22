"""Active Executor Daemon (Phase 4.2).

Python supervisor daemon for autonomous background task execution via Pi Headless RPC.
Enforces:
1. Hub client auth boundary (HTTP bearer token with agent identity)
2. Task-scoped git worktree sandbox confinement
3. Headless approval gating & --no-approve execution
4. Finish-to-review lifecycle ([waiting-for: review])
5. Strict v0 shadow mode (zero side-effects when shadow_mode=True)
6. Lock renewal & crash recovery on startup
"""

import hashlib
import json
import logging
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, registry, storage, task_ownership, tool_surface, trust_presets
from ..hub_client import MCP_PROTOCOL_VERSION  # noqa: F401 - re-exported
from ..hub_client import HubClient as _HubTransport
from ..tools import cycles as cycles_tool
from ..tools import delegate as delegate_tool
from ..tools import notify as notify_tool
from ..tools import tasks as tasks_tool
from ..tools import vault as vault_tool
from . import backends, evidence

logger = logging.getLogger("unified_agent.executor")

# Spool-only bookkeeping on each attempt's usage snapshot (task #1841): the
# order the attempt first ran in and whether it succeeded. Never reported.
_ATTEMPT_SEQ = "_seq"
_ATTEMPT_OK = "_ok"
_ATTEMPT_META = frozenset({_ATTEMPT_SEQ, _ATTEMPT_OK})

BASE_BRANCHES = {"main", "master", "trunk", "develop", "dev"}

# Honest-failure gate (task #1158): a backend "success" with a clean worktree
# (no new commit) is a silent no-op — fail immediately with this named reason
# rather than stamping [waiting-for: review] or burning the full retry budget.
ZERO_OUTPUT_ATTEMPT_REASON = "zero_output_attempt"


class ExecutorOwnershipLost(RuntimeError):
    """The exact attempt lost ownership; do not publish success."""

    def __init__(self, reason: str, *, commit: dict | None = None):
        super().__init__(reason)
        self.commit = commit


class ExecutorProvisioningFailed(RuntimeError):
    """An admitted attempt could not create or attest its checkout."""


class ExecutorUsagePersistenceFailed(OSError):
    """Persistence failed after totals were computed; retain them for reporting."""

    def __init__(self, reason: str, usage: dict[str, Any]):
        super().__init__(reason)
        self.usage = usage


class HubClient(_HubTransport):
    """HTTP client for executing MCP tools over the unified-agent HTTP hub.

    The transport and credential resolution live in ``hub_client.HubClient``
    (task #1724). This executor subclass adds the in-process tool fallback,
    which needs the tool modules and so cannot sit below the runtime layer.
    """

    def _direct_tool_fallback(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """In-process tool invocation fallback with agent identity ContextVar binding."""
        token = config.agent_identity_var.set(self.agent_label)
        try:
            if tool_name == "task_claim":
                return tasks_tool.task_claim(**arguments)
            elif tool_name == "task_update":
                return tasks_tool.task_update(**arguments)
            elif tool_name == "task_list":
                return tasks_tool.task_list(**arguments)
            elif tool_name == "delegate_list":
                return delegate_tool.delegate_list(**arguments)
            elif tool_name == "delegate_claim":
                return delegate_tool.delegate_claim(**arguments)
            elif tool_name == "delegate_release":
                return delegate_tool.delegate_release(**arguments)
            elif tool_name == "delegation_manage":
                return delegate_tool.delegation_manage(**arguments)
            elif tool_name == "vault_journal":
                return vault_tool.vault_journal(**arguments)
            elif tool_name == "notify":
                return notify_tool.notify(**arguments)
            elif tool_name == "system_doctor":
                # The doctor is an adapter over the whole hub, so it is named
                # through the governed tool surface, as workflows do (#1722).
                return tool_surface.registry()["system_doctor"]()
            elif tool_name == "observability_manage":
                from ..monitor import observability_manage

                return observability_manage(**arguments)
            elif tool_name == "autonomous_cycle_record_usage":
                return cycles_tool.autonomous_cycle_record_usage(**arguments)
            elif tool_name == "autonomous_cycle_manage":
                return cycles_tool.autonomous_cycle_manage(**arguments)
            else:
                raise ValueError(f"Unknown fallback tool: {tool_name}")
        finally:
            config.agent_identity_var.reset(token)


def _cancel_requested_from_hub(
    task_id: int, hub_client: HubClient, expected_run_id: str | None, *, authoritative: bool
) -> bool:
    """Check current hub state without allowing a stale-local admission fallback."""
    try:
        call = hub_client._call_over_hub if authoritative else hub_client.call_tool
        history = call(
            "observability_manage",
            {"action": "run_history", "params": {"task_id": task_id, "limit": 1}},
        )
        runs = history.get("runs") or []
        if authoritative:
            # Old hubs without current admission metadata cannot attest that an
            # executor with absent/stale local policy may continue native work.
            if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
                return True
            if runs[0].get("native_execution_allowed") is not True:
                return True
        if expected_run_id is not None:
            return bool(
                not runs
                or runs[0].get("run_id") != expected_run_id
                or runs[0].get("state") != "open"
                or runs[0].get("cancel_requested")
                or runs[0].get("native_execution_allowed") is False
            )
        return bool(
            runs
            and runs[0].get("state") == "open"
            and (
                runs[0].get("cancel_requested") or runs[0].get("native_execution_allowed") is False
            )
        )
    except Exception as exc:  # noqa: BLE001 - admission checks fail closed
        # Do not log response bodies or auth/transport exception contents.
        logger.warning(
            "could not verify current admission for task #%s from hub (%s)",
            task_id,
            type(exc).__name__,
        )
        return authoritative or expected_run_id is not None


def _cancel_requested_for_task(
    task_id: int, hub_client: HubClient | None = None, expected_run_id: str | None = None
) -> bool:
    """Read cancellation and current native admission from the authoritative store.

    Configured remote clients always poll their hub, even with a valid local
    database. Hub-local and explicitly standalone clients may read locally.
    """
    if hub_client is not None and getattr(hub_client, "authoritative_run_checks", False) is True:
        return _cancel_requested_from_hub(task_id, hub_client, expected_run_id, authoritative=True)
    try:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT cancel_requested, current_run_id, current_run_agent FROM tasks WHERE id=?",
                (int(task_id),),
            ).fetchone()
            native_denied = False
            if row is not None and row["current_run_id"]:
                from .. import run_records

                # Use immutable originating attribution for this exact run,
                # including after its delegation is terminally retired.
                current_run = run_records.get_run(row["current_run_id"])
                if current_run is not None:
                    native_denied = current_run.get("native_execution_allowed") is False
        finally:
            conn.close()
    except (storage.LiveStoreError, sqlite3.Error):
        if hub_client is None:
            return False
        return _cancel_requested_from_hub(task_id, hub_client, expected_run_id, authoritative=False)
    if expected_run_id is not None and (row is None or row["current_run_id"] != expected_run_id):
        return True
    return bool(row and (row["cancel_requested"] or native_denied))


class ExecutorDaemon:
    """Active Executor supervisor daemon."""

    def __init__(
        self,
        agent_label: str | None = None,
        shadow_mode: bool | None = None,
        hub_url: str | None = None,
        bearer_token: str | None = None,
        poll_interval: float = 5.0,
        lock_timeout_s: int = 3600,
        clock: Callable[[], float] | None = None,
        backend: Any | None = None,
    ):
        self.agent_label = (agent_label or config.executor_agent()).strip()
        if shadow_mode is None:
            self.shadow_mode = os.environ.get("UA_EXECUTOR_SHADOW_MODE", "0") == "1"
        else:
            self.shadow_mode = shadow_mode

        # The daemon runs unattended, so it defaults to requiring the hub: the
        # in-process fallback is a standalone/test convenience, and silently
        # using it in production means executing outside the hub's boundaries.
        # Opt in explicitly with UA_EXECUTOR_ALLOW_DIRECT=1.
        self.hub_client = HubClient(
            agent_label=agent_label,
            hub_url=hub_url,
            bearer_token=bearer_token,
            allow_fallback=os.environ.get("UA_EXECUTOR_ALLOW_DIRECT", "0") == "1",
        )
        self.poll_interval = poll_interval
        self.lock_timeout_s = lock_timeout_s
        self.running = False
        self.clock = clock or time.time
        self.max_attempts = config.executor_max_attempts()
        self.backoff_base_s = config.executor_backoff_base_s()
        self.backoff_factor = config.executor_backoff_factor()
        self.backoff_cap_s = config.executor_backoff_cap_s()
        # Task #1916: expired-lock reclaim runs at startup and then every
        # reclaim_interval_s from the poll loop (0 = startup only).
        self.reclaim_interval_s = config.executor_reclaim_interval_s()
        self._last_reclaim_at: float | None = None
        self.circuit_breaker_threshold = config.executor_circuit_breaker_threshold()
        self.circuit_breaker_cooldown_s = config.executor_circuit_breaker_cooldown_s()
        self.consecutive_failures = 0
        self.circuit_breaker_tripped_until = 0.0
        # Shadow mode never claims, so the same pending delegation matches
        # find_pending_work on every poll — without this, one unchanged
        # delegation produced a fresh executor_shadow event (and event-bus
        # mirror) every poll_interval forever: 17,280/day at the 5s default
        # (task #445 / PR #194 review). Tracks the last delegation actually
        # emitted for, so only a genuine change re-emits.
        self._last_shadow_delegation_id: int | None = None
        self._usage_retry_offset = 0
        self._cycle_usage_retry_offset = 0
        # Last failure-pair gate refusals from find_pending_work (task #1142).
        # Named reasons: failure_pair_gate | failure_pair_gate_unverifiable.
        self._last_failure_pair_refusals: list[dict[str, Any]] = []
        # Resolved at construction so a misconfigured backend (bad name, or an
        # ungated danger-full-access sandbox) stops the daemon at startup
        # instead of at the first claimed task.
        self.backend = (
            backend if backend is not None else backends.get_backend(agent_label=agent_label)
        )

    def _backoff_window_s(self, attempts: int) -> float:
        """Retry delay after ``attempts`` failures: min(base*factor^(n-1), cap)."""
        window = self.backoff_base_s * (self.backoff_factor ** max(0, attempts - 1))
        return min(window, self.backoff_cap_s)

    def maybe_reclaim_expired_locks(self) -> dict[str, Any] | None:
        """Run `reclaim_expired_locks` at startup and then once per interval.

        Task #1916: a lease that lapsed while the daemon kept running (a stuck
        attempt, a second instance that died) was only recovered on the next
        restart. Cheap: one paged `task_list` filtered to stale claims, and the
        recovery itself is CAS-guarded on run id, claim generation and
        updated_at, so a concurrent claimant's newer write always wins.
        """
        now = self.clock()
        if self._last_reclaim_at is not None and (
            self.reclaim_interval_s <= 0 or now - self._last_reclaim_at < self.reclaim_interval_s
        ):
            return None
        result = self.reclaim_expired_locks()
        if not result.get("skipped"):
            self._last_reclaim_at = now
        return result

    def reclaim_expired_locks(self) -> dict[str, Any]:
        """Recover this actor's expired attempts through canonical authority CAS."""
        reclaimed = []
        released = 0
        cursor = None
        try:
            while True:
                args = {"status": "doing", "stale_claims": True, "fields": "full", "limit": 100}
                if cursor is not None:
                    args["cursor"] = cursor
                page = self.hub_client.call_tool("task_list", args)
                for row in page.get("tasks", []):
                    if row.get("claimed_by") != self.agent_label:
                        continue
                    run_id = row.get("current_run_id")
                    inventory = self.hub_client.call_tool(
                        "delegation_manage",
                        {
                            "action": "list",
                            "params": {
                                "status": "in_flight",
                                "to_agent": self.agent_label,
                                "task_id": row["id"],
                                "limit": 200,
                            },
                        },
                    )
                    delegations = inventory.get("delegations", [])
                    refused = False
                    recovered_attempt = None
                    if inventory.get("truncated", len(delegations) >= 200):
                        logger.warning(
                            "recovery delegation inventory is truncated; refusing task reset"
                        )
                        continue
                    for item in delegations:
                        if item.get("task_id") != row["id"]:
                            continue
                        try:
                            if item["attempts"] >= self.max_attempts:
                                self._mark_delegation_failed(
                                    item["id"],
                                    row["id"],
                                    f"Attempt ceiling reached ({item['attempts']}/{self.max_attempts} attempts failed after lock expiry)",
                                    item["claim_generation"],
                                    expected_run_id=run_id,
                                )
                            else:
                                self._retry_delegation_to_pending(
                                    item["id"],
                                    "executor lease expired; startup recovery",
                                    item["claim_generation"],
                                    task_id=row["id"],
                                    expected_run_id=run_id,
                                    clear_backoff=True,
                                )
                                released += 1
                            recovered_attempt = item
                        except ExecutorOwnershipLost:
                            refused = True
                    if refused:
                        continue
                    generation_guard = {}
                    if recovered_attempt is not None and run_id is not None:
                        generation_guard = {
                            "expected_delegation_id": recovered_attempt["id"],
                            "expected_claim_generation": recovered_attempt["claim_generation"],
                        }
                    try:
                        self.hub_client.call_tool(
                            "task_update",
                            {
                                "id": row["id"],
                                "status": "open",
                                "release_claim": True,
                                "worktree": False,
                                "expected_run_id": run_id,
                                "expected_updated_at": row["updated_at"],
                                **generation_guard,
                            },
                        )
                    except ToolError:
                        continue
                    reclaimed.append({"id": row["id"], "title": row["title"]})
                cursor = page.get("next_cursor")
                if cursor is None:
                    break
        except (storage.LiveStoreError, sqlite3.Error, ToolError, RuntimeError, OSError) as exc:
            logger.warning(
                "skipping local expired-lock reclaim: canonical recovery authority unavailable (%s)",
                type(exc).__name__,
            )
            return {
                "reclaimed_count": len(reclaimed),
                "reclaimed_tasks": reclaimed,
                "released_delegations": released,
                "skipped": True,
                "reason": type(exc).__name__,
            }
        return {
            "reclaimed_count": len(reclaimed),
            "reclaimed_tasks": reclaimed,
            "released_delegations": released,
        }

    def renew_lock(
        self, task_id: int, expected_run_id: str | None = None, *, expected_lane: str | None = None
    ) -> bool:
        """Renew the task lock expiration for long-running execution (task #1000).

        Remote executors renew through canonical RPC using the lane from the
        attested task response. Explicit standalone clients use a local write
        first and fall back to a hub round-trip via
        `task_claim` on `storage.LiveStoreError`/`sqlite3.Error`. `task_claim`
        is renewal-safe: it deliberately succeeds for the agent that already
        holds an active lock (see poll_once's own use of it right after a
        fresh claim), so it is the existing hub-callable equivalent of this
        write — no new tool needed.

        `tasks.lock_expires_at` is genuinely shared state: other agents and
        the hub read it to decide whether a task is still actively held. A
        failure here that goes unnoticed could let the lock silently expire
        mid-execution and the task get reclaimed out from under a
        still-running executor, so — unlike `reclaim_expired_locks`'s pure
        startup housekeeping — this does not swallow a hub failure into
        "not renewed"; if the hub round-trip also fails, this raises.

        Task #1525 (PR #866 review: "cancelled executor lease keeps
        renewing"): a task whose current run has been flagged for
        cancellation is never renewed here, direct-SQL or via the hub
        fallback — this returns False instead so the caller's mid-run
        `kill_check` poll (which checks the same flag) is what actually
        stops the work, not a lease that quietly keeps itself alive.
        """
        if _cancel_requested_for_task(task_id, self.hub_client, expected_run_id):
            logger.info("task #%s: not renewing lock — cancellation requested", task_id)
            return False
        if getattr(self.hub_client, "authoritative_run_checks", False) is True:
            if expected_run_id is None:
                return False
            result = self.hub_client._call_over_hub(
                "task_claim",
                {
                    "id": task_id,
                    "agent": self.agent_label,
                    "timeout_seconds": self.lock_timeout_s,
                    "expected_run_id": expected_run_id,
                    **({"expected_lane": expected_lane} if expected_lane is not None else {}),
                },
            )
            return bool(
                isinstance(result, dict)
                and result.get("id") == task_id
                and result.get("current_run_id") == expected_run_id
                and result.get("claimed_by") == self.agent_label
                and result.get("status") == "doing"
                and not result.get("cancel_requested")
                and result.get("native_execution_allowed") is True
            )
        now_dt = datetime.now(UTC)
        new_expires = (now_dt + timedelta(seconds=self.lock_timeout_s)).isoformat(
            timespec="seconds"
        )
        try:
            conn = storage.connect()
            try:
                cur = conn.execute(
                    "UPDATE tasks SET lock_expires_at=?, updated_at=? "
                    "WHERE id=? AND claimed_by=? AND status='doing' AND cancel_requested=0 "
                    "AND (? IS NULL OR current_run_id=?)",
                    (
                        new_expires,
                        storage.next_task_revision(conn, task_id),
                        task_id,
                        self.agent_label,
                        expected_run_id,
                        expected_run_id,
                    ),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
        except (storage.LiveStoreError, sqlite3.Error) as exc:
            logger.warning(
                "local lock renewal for task #%s unusable (%s); falling back to "
                "hub round-trip via task_claim",
                task_id,
                exc,
            )
            try:
                res = self.hub_client.call_tool(
                    "task_claim",
                    {
                        "id": task_id,
                        "agent": self.agent_label,
                        "timeout_seconds": self.lock_timeout_s,
                        "expected_run_id": expected_run_id,
                        **({"expected_lane": expected_lane} if expected_lane is not None else {}),
                    },
                )
            except Exception as hub_exc:
                raise RuntimeError(
                    f"could not renew lock for task #{task_id}: local storage unusable "
                    f"({exc}) and hub round-trip failed ({hub_exc})"
                ) from hub_exc
            # safe_wait carries the id too, and is no renewal (task #2388).
            return bool(res and res.get("id") == task_id and not task_ownership.is_safe_wait(res))

    def _usage_spool_dir(self) -> Path:
        label_key = hashlib.sha256(self.agent_label.encode()).hexdigest()[:24]
        return config.data_home() / "executor-pending-usage" / label_key

    def _usage_spool_path(self, arguments: dict[str, Any]) -> Path:
        key = hashlib.sha256(
            f"{arguments['id']}:{arguments['expected_run_id']}".encode()
        ).hexdigest()
        return self._usage_spool_dir() / f"{key}.json"

    def _persist_run_usage(self, arguments: dict[str, Any]) -> Path:
        return self._persist_usage_payload(arguments, self._usage_spool_path(arguments))

    def _persist_usage_payload(self, arguments: dict[str, Any], target: Path) -> Path:
        directory = target.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as tmp:
            temp_path = Path(tmp.name)
            try:
                json.dump(arguments, tmp, sort_keys=True)
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
        try:
            os.replace(temp_path, target)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp_path.unlink(missing_ok=True)
        return target

    def _report_cycle_usage(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if self.shadow_mode:
            return None
        key = hashlib.sha256(
            f"{arguments['cycle_id']}:{arguments['usage_run_id']}:{arguments['usage_id']}".encode()
        ).hexdigest()
        target = self._usage_spool_dir() / "cycles" / f"{key}.json"
        try:
            self._persist_usage_payload(arguments, target)
            result = self.hub_client.call_tool("autonomous_cycle_record_usage", arguments)
            if result.get("cycle", {}).get("id") != arguments["cycle_id"]:
                return None
            target.unlink(missing_ok=True)
            return result
        except Exception as exc:  # noqa: BLE001 - accounting must not fail task lifecycle
            logger.warning("cycle usage retained for retry: %s", exc)
            return None

    def _accumulate_run_usage(
        self,
        task_id: int,
        run_id: str,
        attempt_id: str,
        usage: dict[str, Any],
        *,
        succeeded: bool = False,
    ) -> dict[str, Any]:
        """Durably sum distinct attempts; replaying one attempt adds nothing.

        Successful RPC cleanup removes only the pending snapshot, never these
        totals, because a retry can execute under the same task/run lease.

        ``capacity`` is not summed or last-written (task #1841): attempts are
        replayed in the order they first ran and the latest attempt decides.
        One that reports capacity replaces an earlier value; one that reports
        none (it succeeded, or failed for another reason) clears an earlier
        reached limit with ``limit_reached: false``, which the hub's merge
        honours. Replaying an earlier attempt id never revives its limit.
        """
        directory = self._usage_spool_dir() / "totals"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = hashlib.sha256(f"{task_id}:{run_id}".encode()).hexdigest()
        target = directory / f"{key}.json"
        attempts = json.loads(target.read_text()) if target.exists() else {}
        if str(attempt_id) not in attempts:
            attempts[str(attempt_id)] = {
                **usage,
                _ATTEMPT_SEQ: len(attempts),
                _ATTEMPT_OK: bool(succeeded),
            }
        total: dict[str, Any] = {}
        capacity: dict[str, Any] | None = None
        ordered = sorted(attempts.values(), key=lambda snap: snap.get(_ATTEMPT_SEQ, -1))
        for snapshot in ordered:
            for name, value in snapshot.items():
                if value is None or name in _ATTEMPT_META:
                    continue
                if name in ("input_tokens", "output_tokens", "cost_usd"):
                    total[name] = total.get(name, 0) + value
                elif name != "capacity":
                    total[name] = value
            if isinstance(snapshot.get("capacity"), dict):
                capacity = snapshot["capacity"]
            elif capacity is not None:
                capacity = {
                    "limit_reached": False,
                    "source": "executor",
                    "detail": "a later attempt succeeded"
                    if snapshot.get(_ATTEMPT_OK)
                    else "a later attempt stopped without a limit signal",
                }
        if capacity is not None:
            total["capacity"] = capacity
        # Billing is per attempt (task #1837). The total keeps a bill only when
        # every attempt reported the same one; otherwise it is charged as API,
        # so a subscription attempt can never exempt another attempt's API or
        # unknown spend. ("api" rather than absent: the hub keeps an earlier
        # report's billing when a later one omits it.)
        bills = {snapshot.get("billing") for snapshot in attempts.values()}
        if len(bills) == 1 and None not in bills:
            total["billing"] = bills.pop()
        elif bills - {None}:
            total["billing"] = "api"
        else:
            total.pop("billing", None)
        total["cost_complete"] = all(
            snapshot.get("cost_usd") is not None and snapshot.get("cost_complete") is not False
            for snapshot in attempts.values()
        )
        try:
            self._persist_usage_payload(attempts, target)
        except OSError as exc:
            raise ExecutorUsagePersistenceFailed(str(exc), total) from exc
        return total

    def _retry_pending_run_usage(self) -> None:
        """Replay at most twenty snapshots, preserving the original run fence.

        Accounting is bound to the original run even after cancellation or
        takeover. The usage-only path never renews or mutates its successor.
        """
        if self.shadow_mode:
            return
        cycle_pending = sorted((self._usage_spool_dir() / "cycles").glob("*.json"))
        cycle_start = self._cycle_usage_retry_offset % len(cycle_pending) if cycle_pending else 0
        cycle_window = (cycle_pending[cycle_start:] + cycle_pending[:cycle_start])[:20]
        self._cycle_usage_retry_offset = cycle_start + len(cycle_window)
        for path in cycle_window:
            try:
                args = json.loads(path.read_text())
                if args.get("agent") == self.agent_label:
                    self._report_cycle_usage(args)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("pending cycle usage retry failed: %s", type(exc).__name__)
        pending = sorted(self._usage_spool_dir().glob("*.json"))
        if not pending:
            self._usage_retry_offset = 0
            return
        start = self._usage_retry_offset % len(pending)
        window = (pending[start:] + pending[:start])[:20]
        self._usage_retry_offset = (start + len(window)) % len(pending)
        for path in window:
            try:
                args = json.loads(path.read_text())
                if args.get("agent") != self.agent_label or not args.get("expected_run_id"):
                    continue
                self._report_run_usage(
                    args["id"],
                    args["expected_run_id"],
                    args["run_usage"],
                    expected_lane=args.get("expected_lane"),
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("pending harness usage retry failed: %s", type(exc).__name__)

    def _report_run_usage(
        self,
        task_id: int,
        run_id: str | None,
        run_usage: dict[str, Any],
        *,
        expected_lane: str | None = None,
    ) -> dict[str, Any] | None:
        """Report cumulative harness accounting on the original fenced run.

        The usage-only path accepts late spend without renewing a lease or
        changing lifecycle. Reports persist before RPC and replay safely.
        Budget stops terminate retries; accounting failures retain the spool
        and return None rather than failing the task lifecycle.
        """
        if run_id is None or self.shadow_mode:
            return None
        arguments: dict[str, Any] = {
            "id": task_id,
            "agent": self.agent_label,
            "timeout_seconds": self.lock_timeout_s,
            "expected_run_id": run_id,
            "run_usage": run_usage,
            "usage_only": True,
        }
        if expected_lane is not None:
            arguments["expected_lane"] = expected_lane
        pending_path = self._usage_spool_path(arguments)
        try:
            self._persist_run_usage(arguments)
        except OSError as exc:
            logger.warning("task #%s: could not persist harness usage: %s", task_id, exc)
        try:
            if getattr(self.hub_client, "authoritative_run_checks", False) is True:
                res = self.hub_client._call_over_hub("task_claim", arguments)
            else:
                res = self.hub_client.call_tool("task_claim", arguments)
        except Exception as exc:  # noqa: BLE001 - accounting must not fail the run
            logger.warning("task #%s: harness usage not reported: %s", task_id, exc)
            return None
        if not isinstance(res, dict) or res.get("id") != task_id:
            return None
        try:
            pending_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("task #%s: usage reported but pending cleanup failed: %s", task_id, exc)
        for warning in res.get("budget_warnings") or []:
            logger.warning("task #%s: %s", task_id, warning)
        if res.get("budget_stop"):
            logger.warning(
                "task #%s: run ended past its budget (%s); new claims are refused "
                "until the budget period resets or the limit is raised",
                task_id,
                res["budget_stop"],
            )
        return res

    def _release_delegation(
        self, delegation_id: int, expected_claim_generation: int | None = None
    ) -> None:
        """Put a delegation back to 'pending' after we failed before executing.

        Only for the window between taking the delegation and starting the run.
        A failure DURING execution deliberately leaves the row in_flight — the
        startup reclaim releases it once the task lock expires. Releasing on
        every execution failure would re-run a failing delegation forever at
        poll cadence, which is the bug this task exists to remove, inverted.
        """
        try:
            self.hub_client.call_tool(
                "delegation_manage",
                {
                    "action": "release",
                    "params": {
                        "id": delegation_id,
                        "agent": self.agent_label,
                        "expected_claim_generation": expected_claim_generation,
                    },
                },
            )
        except Exception as e:
            logger.warning("could not release delegation #%s: %s", delegation_id, e)

    def _retire_stopped_delegation(
        self,
        delegation: dict[str, Any],
        *,
        run_id: str | None,
        reason: str,
        close_cancelled_run: bool = True,
    ) -> bool:
        """Retire only this claimed attempt when its task run stopped.

        The hub update checks the monotonic claim generation and task run in one SQL
        statement, so this worker cannot cancel a successor's delegation.
        """
        generation = delegation.get("claim_generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            logger.error(
                "cannot safely retire delegation #%s without claim generation", delegation["id"]
            )
            return False
        result = self.hub_client.call_tool(
            "delegation_manage",
            {
                "action": "cancel",
                "params": {
                    "id": delegation["id"],
                    "agent": self.agent_label,
                    "reason": reason,
                    "expected_claim_generation": generation,
                    "expected_run_id": run_id,
                    "require_no_run": run_id is None,
                },
            },
        )
        cancelled = result.get("cancelled") is True
        if run_id is not None and close_cancelled_run:
            # Another caller may already have retired the same delegation;
            # task cleanup still uses the stored exact generation below.
            # A superseded attempt may retire its own delegation, but cannot
            # close the successor task. Only an explicitly cancelled, still
            # open canonical run warrants task cleanup; transient admission
            # failures and ordinary takeover never authorize it.
            history = self.hub_client.call_tool(
                "observability_manage",
                {"action": "run_history", "params": {"task_id": delegation["task_id"], "limit": 1}},
            )
            runs = history.get("runs") or []
            if (
                runs
                and runs[0].get("run_id") == run_id
                and runs[0].get("state") == "open"
                and runs[0].get("cancel_requested") is True
            ):
                try:
                    self.hub_client.call_tool(
                        "task_update",
                        {
                            "id": delegation["task_id"],
                            "status": "cancelled",
                            "release_claim": True,
                            "worktree": False,
                            "expected_run_id": run_id,
                            "expected_delegation_id": delegation["id"],
                            "expected_claim_generation": generation,
                            "append_note": "[executor acknowledged run cancellation]",
                        },
                    )
                except ToolError as exc:
                    # A new run or attempt may win after the history read.
                    # The canonical run/generation CAS preserves that owner.
                    logger.warning(
                        "cancelled task #%s cleanup refused by canonical authority: %s",
                        delegation["task_id"],
                        exc,
                    )
        return cancelled

    @staticmethod
    def _parse_ts(ts_str: str) -> float:
        try:
            return datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            return 0.0

    def _delegation_transition(
        self,
        action: str,
        delegation_id: int,
        expected_claim_generation: int | None,
        *,
        task_id: int | None = None,
        expected_run_id: str | None = None,
        **details: Any,
    ) -> None:
        """Use canonical hub lifecycle CAS, even when a local DB is readable."""
        params = {
            "id": delegation_id,
            "agent": self.agent_label,
            "expected_claim_generation": expected_claim_generation,
            **details,
        }
        if task_id is not None:
            params["task_id"] = task_id
        if expected_run_id is not None:
            params["expected_run_id"] = expected_run_id
        try:
            result = self.hub_client.call_tool(
                "delegation_manage", {"action": action, "params": params}
            )
        except Exception as exc:
            description = {"fail": "mark", "complete": "mark", "retry": "retry"}[action]
            outcome = {"fail": " failed", "complete": " completed", "retry": ""}[action]
            raise RuntimeError(
                f"could not {description} delegation #{delegation_id}{outcome}: canonical hub unavailable ({exc})"
            ) from exc
        key = {"fail": "failed", "retry": "retried", "complete": "completed"}[action]
        if not isinstance(result, dict) or result.get(key) is not True:
            raise ExecutorOwnershipLost(f"delegation {action} refused for this claimed attempt")

    def _mark_delegation_failed(
        self,
        delegation_id: int,
        task_id: int,
        reason: str,
        expected_claim_generation: int | None = None,
        *,
        expected_run_id: str | None = None,
    ) -> None:
        self._delegation_transition(
            "fail",
            delegation_id,
            expected_claim_generation,
            task_id=task_id,
            expected_run_id=expected_run_id,
            reason=reason,
        )

    def _read_delegation_attempts(self, delegation_id: int) -> int:
        """Attempt-count read for the self-healing-guardrails ceiling check (task #1000).

        Direct local read first, falling back to a hub round-trip
        (`delegation_manage(action="get")`) on
        `storage.LiveStoreError`/`sqlite3.Error`. `attempts` gates the
        attempt-ceiling decision right after this call, so an unreadable
        count must not be silently treated as 1 (the default) — that would
        reset a delegation's real attempt history and let it retry forever
        past its actual ceiling. If the hub round-trip also fails, this
        raises rather than guessing.
        """
        if getattr(self.hub_client, "authoritative_run_checks", False) is True:
            result = self.hub_client._call_over_hub(
                "delegation_manage", {"action": "get", "params": {"id": delegation_id}}
            )
            row = result.get("delegation") if isinstance(result, dict) else None
            attempts = row.get("attempts") if isinstance(row, dict) else None
            if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
                raise RuntimeError("hub did not attest the delegation attempt count")
            return attempts
        try:
            conn = storage.connect()
            try:
                r = conn.execute(
                    "SELECT attempts FROM delegations WHERE id=?", (delegation_id,)
                ).fetchone()
                if r and r["attempts"]:
                    return r["attempts"]
                return 1
            finally:
                conn.close()
        except (storage.LiveStoreError, sqlite3.Error) as exc:
            logger.warning(
                "local attempt-count read for delegation #%s unusable (%s); "
                "falling back to hub round-trip via delegation_manage(action=get)",
                delegation_id,
                exc,
            )
            try:
                res = self.hub_client.call_tool(
                    "delegation_manage",
                    {"action": "get", "params": {"id": delegation_id}},
                )
            except Exception as hub_exc:
                raise RuntimeError(
                    f"could not read attempt count for delegation #{delegation_id}: "
                    f"local storage unusable ({exc}) and hub round-trip failed ({hub_exc})"
                ) from hub_exc
            hub_attempts = (res or {}).get("delegation", {}).get("attempts")
            return hub_attempts if hub_attempts else 1

    def _retry_delegation_to_pending(
        self,
        delegation_id: int,
        reason: str,
        expected_claim_generation: int | None = None,
        *,
        task_id: int | None = None,
        expected_run_id: str | None = None,
        clear_backoff: bool = False,
    ) -> None:
        self._delegation_transition(
            "retry",
            delegation_id,
            expected_claim_generation,
            task_id=task_id,
            expected_run_id=expected_run_id,
            reason=reason,
            **({"clear_backoff": True} if clear_backoff else {}),
        )

    def _mark_delegation_completed(
        self,
        delegation_id: int,
        result_text: str,
        expected_claim_generation: int | None = None,
        *,
        task_id: int | None = None,
        expected_run_id: str | None = None,
    ) -> None:
        self._delegation_transition(
            "complete",
            delegation_id,
            expected_claim_generation,
            task_id=task_id,
            expected_run_id=expected_run_id,
            result=result_text,
        )

    def _failure_pair_gate_for(self, from_agent: str, to_agent: str) -> dict[str, Any]:
        """from→to consecutive-failure gate (task #1142).

        Prefers the local `pair_failure_status` read; falls back to a hub
        round-trip when local storage is a tombstone. If both fail, raises
        `RuntimeError` so the eligibility filter fails closed — same
        precedent as `_cycle_gate_for_task`.
        """
        try:
            return delegate_tool.pair_failure_status(from_agent, to_agent)
        except (storage.LiveStoreError, sqlite3.Error) as exc:
            logger.warning(
                "local failure-pair gate for %s→%s unusable (%s); falling back to "
                "hub round-trip via delegation_manage(failure_pair_gate)",
                from_agent,
                to_agent,
                exc,
            )
            try:
                return self.hub_client.call_tool(
                    "delegation_manage",
                    {
                        "action": "failure_pair_gate",
                        "params": {"from_agent": from_agent, "to_agent": to_agent},
                    },
                )
            except Exception as hub_exc:
                raise RuntimeError(
                    f"failure_pair_gate unverifiable for {from_agent}->{to_agent}: "
                    f"local storage unusable ({exc}) and hub round-trip failed ({hub_exc})"
                ) from hub_exc

    def find_pending_work(self, now_ts: float | None = None) -> list[dict[str, Any]]:
        """Find pending delegation items assigned to this agent, enforcing backoff & ceilings."""
        res = self.hub_client.call_tool(
            "delegation_manage",
            {
                "action": "list",
                "params": {"status": "pending", "to_agent": self.agent_label},
            },
        )
        delegations = res.get("delegations", [])
        eligible = []
        refusals: list[dict[str, Any]] = []
        if now_ts is None:
            now_ts = self.clock()

        for d in delegations:
            if d.get("held"):
                # Interrupted work (task #2482): the hub lists the delegation
                # but refuses its claim, so it is not this daemon's to touch,
                # not even to fail it at the attempt ceiling.
                logger.debug("delegation #%s skipped: %s", d["id"], d["held"])
                continue
            attempts = d.get("attempts", 0)
            last_attempt_at = d.get("last_attempt_at")

            # Check attempt ceiling
            if attempts >= self.max_attempts:
                self._mark_delegation_failed(
                    d["id"],
                    d["task_id"],
                    f"Attempt ceiling reached ({attempts}/{self.max_attempts} attempts failed)",
                )
                continue

            # Check exponential backoff window
            if attempts > 0 and last_attempt_at:
                last_ts = self._parse_ts(last_attempt_at)
                backoff_s = self._backoff_window_s(attempts)
                if (now_ts - last_ts) < backoff_s:
                    continue

            # Failure-pair gate (task #1142): refuse claim when from→to has
            # >=3 consecutive failed outcomes without an intervening success
            # or review annotation. Never marks the pending row failed —
            # eligibility only; human review/success resets the counter.
            from_agent = d.get("from_agent") or ""
            to_agent = d.get("to_agent") or self.agent_label
            try:
                gate = self._failure_pair_gate_for(from_agent, to_agent)
            except RuntimeError as exc:
                reason = delegate_tool.FAILURE_PAIR_GATE_UNVERIFIABLE
                logger.error(
                    "delegation #%s: failure-pair gate unverifiable (%s); "
                    "refusing claim fail-closed",
                    d["id"],
                    exc,
                )
                refusals.append(
                    {
                        "delegation_id": d["id"],
                        "task_id": d.get("task_id"),
                        "from_agent": from_agent,
                        "to_agent": to_agent,
                        "reason": reason,
                        "detail": str(exc),
                    }
                )
                try:
                    evidence.record_executor_event(
                        "quarantine",
                        task_id=d.get("task_id"),
                        delegation_id=d["id"],
                        agent=self.agent_label,
                        reason=f"{reason}: {exc}",
                    )
                except Exception:
                    pass
                continue

            if gate.get("blocked"):
                reason = gate.get("reason") or delegate_tool.FAILURE_PAIR_GATE_REASON
                logger.warning(
                    "delegation #%s: failure-pair gate blocked %s→%s "
                    "(consecutive_failures=%s, last_failure_class=%s)",
                    d["id"],
                    from_agent,
                    to_agent,
                    gate.get("consecutive_failures"),
                    gate.get("last_failure_class"),
                )
                refusals.append(
                    {
                        "delegation_id": d["id"],
                        "task_id": d.get("task_id"),
                        "from_agent": from_agent,
                        "to_agent": to_agent,
                        "reason": reason,
                        "consecutive_failures": gate.get("consecutive_failures"),
                        "last_failure_class": gate.get("last_failure_class"),
                    }
                )
                try:
                    evidence.record_executor_event(
                        "quarantine",
                        task_id=d.get("task_id"),
                        delegation_id=d["id"],
                        agent=self.agent_label,
                        reason=(
                            f"{reason}: {from_agent}->{to_agent} "
                            f"consecutive_failures={gate.get('consecutive_failures')} "
                            f"last_failure_class={gate.get('last_failure_class')}"
                        ),
                    )
                except Exception:
                    pass
                continue

            eligible.append(d)

        self._last_failure_pair_refusals = refusals
        return eligible

    def task_worktree(self, claim_res: dict[str, Any]) -> str | None:
        """The worktree ``task_claim`` created and recorded for this task.

        The daemon used to create its OWN worktree here, so every delegated task
        got two: ``.worktrees/task-<id>`` on ``task/<id>-<slug>`` from
        ``_ensure_task_worktree`` at claim time, and
        ``.worktrees/task-<id>-<slug>`` on ``agy/task-<id>-<slug>`` from this
        method at execution time. Pi ran in the second; the database recorded
        the first. Nothing compared them, so both "succeeded" and the
        disagreement was invisible — until a live smoke test looked (task #178).

        That mattered beyond duplicate directories:
        ``safety.get_active_worktree_path`` and ``merge.git_merge_task`` both
        resolve from ``tasks.worktree_path``, so an agent's fs_* sandbox and the
        merge target pointed at the empty worktree while the work sat elsewhere.

        ``_ensure_task_worktree`` wins because it is the one that persists
        ``worktree_path``/``branch`` (the columns everything else reads), it
        fires for every claimer rather than only the executor, and its branch
        name is agent-neutral. Its path is ``.worktrees/task-<id>`` — no
        title-derived component at all, so a hostile title cannot influence it.

        Returns None when the claim produced no worktree (a workspace that is
        not a git repo is a supported configuration).
        """
        worktree = claim_res.get("worktree_path")
        if not worktree:
            return None
        return str(worktree)

    @staticmethod
    def _remote_worktree_local_candidate(worktree: str) -> Path | None:
        """Map a hub-side worktree path onto this host's checkout (task #1094 follow-up).

        A remote desktop executor claims tasks through the hub, and
        ``task_claim`` materializes the worktree on the HUB host — so the
        recorded ``worktree_path`` is absolute in the HUB's home (e.g.
        ``/Users/you/dev/example-project/.worktrees/task-1109``). Handing that
        path to a backend as ``cwd`` on the desktop silently spawns the child
        in a nonexistent directory.

        The desktop topology rule (AGENTS.md, task #331) puts every repo under
        ``~/dev/``, so the deterministic map is: keep everything from the
        ``dev`` segment onward and re-root it at ``~/dev``. Returns None when
        the path has no ``dev`` segment (nothing safe to assume), or when a
        ``..`` segment after it could step out of ``~/dev`` (task #2288).
        """
        parts = Path(worktree).parts
        if "dev" not in parts:
            return None
        dev_idx = parts.index("dev")
        rest = parts[dev_idx + 1 :]
        if ".." in rest:
            return None
        return ExecutorDaemon._local_dev_root() / Path(*rest)

    @staticmethod
    def _local_dev_root() -> Path:
        """The directory remote worktree paths are re-rooted under (``~/dev``)."""
        return Path.home() / "dev"

    @staticmethod
    def _local_repo_below_dev_root(candidate: Path, dev_root: Path) -> Path | None:
        """Nearest ancestor of ``candidate`` holding ``.git``, strictly below ``dev_root``.

        Task #2288: the walk must stop at the dev root. When ``~/dev/<repo>``
        is not checked out, an unbounded walk reached ``~/dev``, ``~`` and
        ``/`` and handed back any enclosing repository (a dotfile repo in
        ``~``, say), so ``git worktree add`` registered task worktrees there.
        ``GIT_CEILING_DIRECTORIES`` cannot help because this walk is Python.
        """
        real_root = dev_root.resolve()
        for path in (candidate, *candidate.parents):
            if path == dev_root or dev_root not in path.parents:
                return None
            git_entry = path / ".git"
            if git_entry.exists():
                # The lexical check above cannot see a symlink inside the dev
                # root (``~/dev/<repo> -> ~``) that aliases an enclosing
                # repository, so the checkout and its ``.git`` must also
                # resolve strictly below the resolved dev root. A symlinked
                # dev root itself stays supported: both sides are resolved.
                for real in (path.resolve(), git_entry.resolve()):
                    if real == real_root or real_root not in real.parents:
                        return None
                return path
        return None

    @staticmethod
    def _repo_name_from_remote_url(url: str) -> str:
        """Extract the repo name from a git remote URL."""
        url = url.strip()
        if url.endswith(".git"):
            url = url[:-4]
        url = url.rstrip("/")
        if ":" in url and not url.startswith(("http://", "https://", "ssh://", "file://")):
            url = url.split(":", 1)[1]
        return Path(url).name

    def _origin_matches_hub_repo(
        self,
        task_id: int,
        worktree: str,
        local_repo: Path,
        hub_repo: str | None,
    ) -> bool:
        """F1: False when the local repo's origin remote names a different repo than the hub's."""
        expected_repo = hub_repo
        if expected_repo is None:
            parts = Path(worktree).parts
            if ".worktrees" in parts:
                wt_idx = parts.index(".worktrees")
                expected_repo = parts[wt_idx - 1]
            elif "dev" in parts:
                dev_idx = parts.index("dev")
                expected_repo = parts[dev_idx + 1]
            else:
                expected_repo = local_repo.name

        origin_check = self._git(local_repo, ["config", "--get", "remote.origin.url"], check=False)
        if origin_check.returncode == 0:
            origin_url = origin_check.stdout.strip()
            remote_repo_name = self._repo_name_from_remote_url(origin_url)
            if (
                remote_repo_name
                and expected_repo
                and remote_repo_name != expected_repo
                and remote_repo_name.replace("-", "_") != expected_repo.replace("-", "_")
            ):
                logger.warning(
                    "task #%s: local repo %s origin remote (%s -> %s) mismatches hub-side repo %s; failing closed",
                    task_id,
                    local_repo,
                    origin_url,
                    remote_repo_name,
                    expected_repo,
                )
                return False
        return True

    def _existing_worktree_matches_attestation(
        self,
        path: Path,
        task_id: int,
        branch: str | None,
        hub_branch_head: str,
    ) -> bool:
        """Task #1528: an existing checkout is admitted only if it IS the attested one.

        It must be a git worktree whose toplevel is exactly ``path`` (a plain
        subdirectory of some other repo is refused), sit at ``hub_branch_head``
        exactly, and be on ``branch`` when one is recorded. Cleanliness is not
        required: the run itself dirties the tree and a retry of the same
        run/generation legitimately reuses it. Any git failure fails closed.
        """
        try:
            if not path.is_dir():
                logger.warning(
                    "task #%s: attested worktree %s does not exist; failing closed", task_id, path
                )
                return False
            top = self._git(path, ["rev-parse", "--show-toplevel"], check=False)
            if top.returncode != 0 or Path(top.stdout.strip()).resolve() != path.resolve():
                logger.warning(
                    "task #%s: %s is not a git worktree toplevel; failing closed", task_id, path
                )
                return False
            head = self._git(path, ["rev-parse", "HEAD"], check=False)
            if head.returncode != 0 or head.stdout.strip() != hub_branch_head:
                logger.warning(
                    "task #%s: existing worktree %s HEAD (%s) mismatches attested source SHA %s; "
                    "failing closed",
                    task_id,
                    path,
                    head.stdout.strip(),
                    hub_branch_head,
                )
                return False
            if branch:
                current = self._git(path, ["symbolic-ref", "--short", "HEAD"], check=False)
                if current.returncode != 0 or current.stdout.strip() != branch:
                    logger.warning(
                        "task #%s: existing worktree %s is on branch %r, not attested %r; "
                        "failing closed",
                        task_id,
                        path,
                        current.stdout.strip(),
                        branch,
                    )
                    return False
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            logger.warning(
                "task #%s: could not verify existing worktree %s (%s); failing closed",
                task_id,
                path,
                exc,
            )
            return False
        return True

    def localize_worktree(
        self,
        worktree: str,
        task_id: int,
        branch: str | None,
        *,
        hub_branch_head: str | None = None,
        hub_repo: str | None = None,
        recreate_stale: bool = True,
    ) -> str | None:
        """Return a worktree path that EXISTS on this host, creating it if needed.

        Hub-local executors get the recorded path unchanged. Remote executors
        (local memory.db tombstone topology) get the ``~/dev`` re-rooted
        equivalent; when that worktree does not exist yet it is created from
        the local repo — same branch when the branch exists locally, else a
        new local branch of the same name from HEAD, mirroring what
        ``_ensure_task_worktree`` does hub-side. On any failure return None;
        execution admission refuses Git tasks without an isolated checkout.
        """
        # Hub-local case: the recorded path is either relative (resolved
        # against the workspace root the daemon already runs in) or an
        # absolute path that exists on this host. Localization applies only
        # to absolute paths that do NOT exist here — the remote-executor
        # signature (task #1094 follow-up).
        if not os.path.isabs(worktree) or os.path.isdir(worktree):
            if hub_branch_head is None:
                return worktree
            # Attested (authoritative) run: never admit an existing path on
            # trust. Relative paths resolve against the workspace root, as the
            # gate runners do (safety.resolve_task_worktree).
            existing = Path(worktree)
            if not existing.is_absolute():
                existing = config.workspace_root() / existing
            if not self._existing_worktree_matches_attestation(
                existing, task_id, branch, hub_branch_head
            ):
                return None
            return worktree
        candidate = self._remote_worktree_local_candidate(worktree)
        if candidate is None:
            logger.warning(
                "task #%s: recorded worktree %s does not exist locally and has no "
                "dev segment to re-root; failing closed",
                task_id,
                worktree,
            )
            return None
        dev_root = self._local_dev_root()
        local_repo = self._local_repo_below_dev_root(candidate, dev_root)
        if local_repo is None:
            logger.warning(
                "task #%s: no local git repo found above %s below %s; failing closed",
                task_id,
                candidate,
                dev_root,
            )
            return None

        if candidate.exists():
            if hub_branch_head is None:
                return str(candidate)
            # Attested run: a pre-existing mirror may be a stale leftover from an
            # earlier run/generation, so re-verify origin repo, branch and HEAD.
            try:
                origin_ok = self._origin_matches_hub_repo(task_id, worktree, local_repo, hub_repo)
            except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
                logger.warning(
                    "task #%s: could not verify origin of existing mirror %s (%s); failing closed",
                    task_id,
                    candidate,
                    exc,
                )
                return None
            if not origin_ok or not self._existing_worktree_matches_attestation(
                candidate, task_id, branch, hub_branch_head
            ):
                return None
            return str(candidate)
        try:
            # F1: fail closed when re-rooted repo's origin remote mismatches hub-side repo
            if not self._origin_matches_hub_repo(task_id, worktree, local_repo, hub_repo):
                return None
            if hub_branch_head is not None:
                source = self._git(
                    local_repo, ["cat-file", "-e", f"{hub_branch_head}^{{commit}}"], check=False
                )
                if source.returncode != 0:
                    self._git(local_repo, ["fetch", "origin"])
                    source = self._git(
                        local_repo, ["cat-file", "-e", f"{hub_branch_head}^{{commit}}"], check=False
                    )
                    if source.returncode != 0:
                        logger.warning(
                            "task #%s: attested source unavailable after origin fetch", task_id
                        )
                        return None
            if branch:
                check = self._git(local_repo, ["rev-parse", "--verify", branch], check=False)
                if check.returncode == 0:
                    local_head = check.stdout.strip()
                    # F2: stale local branch must be compared to the hub branch head
                    effective_hub_head = hub_branch_head
                    if effective_hub_head is None:
                        origin_b = self._git(
                            local_repo, ["rev-parse", "--verify", f"origin/{branch}"], check=False
                        )
                        if origin_b.returncode == 0:
                            effective_hub_head = origin_b.stdout.strip()

                    if effective_hub_head and local_head != effective_hub_head:
                        logger.warning(
                            "task #%s: local branch %s (%s) mismatches hub branch head (%s)",
                            task_id,
                            branch,
                            local_head,
                            effective_hub_head,
                        )
                        if not recreate_stale:
                            return None
                        recreate = self._git(
                            local_repo, ["branch", "-f", branch, effective_hub_head], check=False
                        )
                        if recreate.returncode != 0:
                            logger.warning(
                                "task #%s: could not recreate stale local branch %s to %s (%s); failing closed",
                                task_id,
                                branch,
                                effective_hub_head,
                                (recreate.stderr or recreate.stdout).strip(),
                            )
                            return None
                    self._git(local_repo, ["worktree", "add", str(candidate), branch])
                else:
                    # The branch exists only hub-side; recreate it locally.
                    if hub_branch_head:
                        self._git(
                            local_repo,
                            ["worktree", "add", "-b", branch, str(candidate), hub_branch_head],
                        )
                    else:
                        self._git(local_repo, ["worktree", "add", "-b", branch, str(candidate)])
            else:
                self._git(local_repo, ["worktree", "add", str(candidate)])
            if hub_branch_head is not None:
                created_head = self._git(candidate, ["rev-parse", "HEAD"]).stdout.strip()
                if created_head != hub_branch_head:
                    logger.warning(
                        "task #%s: created worktree %s HEAD (%s) mismatches attested source "
                        "SHA %s; failing closed",
                        task_id,
                        candidate,
                        created_head,
                        hub_branch_head,
                    )
                    return None
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            logger.warning(
                "task #%s: could not create local worktree %s (%s); failing closed",
                task_id,
                candidate,
                exc,
            )
            return None
        logger.info(
            "task #%s: localized hub worktree %s -> %s (branch %s)",
            task_id,
            worktree,
            candidate,
            branch,
        )
        return str(candidate)

    @staticmethod
    def _git(
        worktree_abs: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess:
        res = subprocess.run(
            ["git", "-C", str(worktree_abs), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and res.returncode != 0:
            detail = (res.stderr or res.stdout).strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
        return res

    def _attempt_is_current(
        self,
        task_id: int,
        expected_run_id: str,
        delegation_id: int | None = None,
        generation: int | None = None,
    ) -> bool:
        if _cancel_requested_for_task(task_id, self.hub_client, expected_run_id):
            return False
        if delegation_id is None:
            return True
        try:
            authoritative = getattr(self.hub_client, "authoritative_run_checks", False) is True
            call = self.hub_client._call_over_hub if authoritative else self.hub_client.call_tool
            row = call("delegation_manage", {"action": "get", "params": {"id": delegation_id}}).get(
                "delegation"
            )
            return bool(
                isinstance(row, dict)
                and row.get("id") == delegation_id
                and row.get("task_id") == task_id
                and row.get("to_agent") == self.agent_label
                and row.get("status") == "in_flight"
                and row.get("claim_generation") == generation
            )
        except Exception:
            return False

    def _commit_child_changes(
        self,
        *,
        task_id: int,
        worktree: str | None,
        branch: str | None,
        expected_run_id: str | None = None,
        delegation_id: int | None = None,
        expected_claim_generation: int | None = None,
    ) -> dict[str, str | None]:
        if not worktree:
            return {"status": "refused", "reason": "no task worktree", "sha": None}

        from ..safety import resolve_task_worktree

        worktree_abs = resolve_task_worktree(worktree)
        top = Path(
            self._git(worktree_abs, ["rev-parse", "--show-toplevel"]).stdout.strip()
        ).resolve()
        if top != worktree_abs:
            raise RuntimeError(f"refusing to commit outside task worktree: {top}")

        current_branch = self._git(worktree_abs, ["branch", "--show-current"]).stdout.strip()
        if (
            not branch
            or current_branch != branch
            or branch in BASE_BRANCHES
            or not branch.startswith("task/")
        ):
            raise RuntimeError(f"refusing to commit on non-task branch {current_branch!r}")

        if not self._git(worktree_abs, ["status", "--porcelain"], check=True).stdout.strip():
            return {"status": "refused", "reason": "worktree clean", "sha": None}

        if expected_run_id is not None and not self._attempt_is_current(
            task_id, expected_run_id, delegation_id, expected_claim_generation
        ):
            raise ExecutorOwnershipLost(
                "task run or delegation generation changed before daemon commit"
            )
        # The captured canonical checkout/index/ref belongs to this exact run
        # and delegation generation. Isolation protects its successor if the
        # remote authority changes immediately after this read.
        self._git(worktree_abs, ["add", "-A"])
        body = (
            f"Task: #{task_id}\n"
            f"Child agent: {self.agent_label}\n"
            "Daemon commit reason: child sandbox blocked git metadata writes; "
            "daemon committed from the host."
        )
        self._git(
            worktree_abs,
            [
                "-c",
                "user.name=Unified Agent Executor",
                "-c",
                "user.email=unified-agent-executor@localhost",
                "commit",
                "-m",
                f"chore: finish task #{task_id} execution",
                "-m",
                body,
            ],
        )
        sha = self._git(worktree_abs, ["rev-parse", "HEAD"]).stdout.strip()
        result = {"status": "committed", "reason": None, "sha": sha}
        if expected_run_id is not None and not self._attempt_is_current(
            task_id, expected_run_id, delegation_id, expected_claim_generation
        ):
            raise ExecutorOwnershipLost(
                "task run or delegation generation changed during daemon commit", commit=result
            )
        return result

    def poll_once(self) -> dict[str, Any] | None:
        """Single iteration of the supervisor loop."""
        # The executor is the one agent with no reason to touch the hub while idle,
        # so without this it would read as 'offline' in the control center precisely
        # when it is healthy and waiting for work. Throttled inside touch_agent, so a
        # 5s poll produces one row update per heartbeat interval, not one per tick.
        try:
            executor_host = socket.gethostname()
        except OSError:
            executor_host = None
        registry.touch_agent(
            self.agent_label,
            via="executor_poll",
            kind="executor",
            pid=os.getpid(),
            host=executor_host,
        )

        self._retry_pending_run_usage()
        now_ts = self.clock()
        if now_ts < self.circuit_breaker_tripped_until:
            rem_s = round(self.circuit_breaker_tripped_until - now_ts, 1)
            logger.info(
                "Circuit breaker active for %s (%ss cooldown remaining); skipping poll",
                self.agent_label,
                rem_s,
            )
            return {
                "status": "circuit_breaker_active",
                "agent": self.agent_label,
                "cooldown_remaining_s": rem_s,
            }

        pending = self.find_pending_work(now_ts=now_ts)
        if not pending:
            return None

        for item in pending:
            task_id = item["task_id"]
            delegation_id = item["id"]
            task_spec = item["task_spec"]
            from_agent = item["from_agent"]
            title = task_spec.splitlines()[0][:120]

            if self.shadow_mode:
                msg = f"[SHADOW MODE] Would claim task #{task_id} (delegation #{delegation_id}) from {from_agent}: {title}"
                logger.info(msg)
                print(msg)
                if delegation_id != self._last_shadow_delegation_id:
                    storage.record_agent_event(
                        "executor_shadow",
                        ref_id=str(delegation_id),
                        agent=self.agent_label,
                        summary=f"would claim task #{task_id} from {from_agent}: {title[:80]}",
                    )
                    # storage.record_agent_event("executor_shadow", ...) mirrors
                    # onto "agent.executor_shadow.updated" (no _KIND_CHANNELS
                    # entry for this kind, so it falls to the generic
                    # f"agent.{kind}.updated" default) — that channel does not
                    # start with "agent.executor." and so never matches the
                    # webui's "executor" activity filter, unlike the
                    # quarantine/cooldown events evidence.record_executor_event
                    # already publishes there. Publish this one the same way so
                    # shadow-mode activity is actually visible under that filter
                    # (task #445 / PR #191 review).
                    evidence.record_executor_event(
                        "shadow",
                        task_id=task_id,
                        delegation_id=delegation_id,
                        agent=self.agent_label,
                        reason=f"would claim task #{task_id} from {from_agent}: {title[:80]}",
                    )
                    self._last_shadow_delegation_id = delegation_id
                return {
                    "status": "shadow_skipped",
                    "task_id": task_id,
                    "delegation_id": delegation_id,
                    "action": "skipped_all_side_effects",
                }

            # task_claim can create a native git worktree on the hub, so
            # refuse before asking it to claim or provision any context.
            try:
                trust_presets.require_native_allowed(self.agent_label, originator=from_agent)
            except ToolError as exc:
                reason = str(exc)
                self._mark_delegation_failed(delegation_id, task_id, reason)
                return {
                    "status": "blocked_by_trust_preset",
                    "task_id": task_id,
                    "delegation_id": delegation_id,
                    "reason": reason,
                }

            # Take the delegation itself first. This is the transition that
            # actually stops the loop: until the row leaves 'pending' it keeps
            # matching find_pending_work, so the daemon re-claimed the same
            # delegation on every 5s tick (138 times on task #149). Claiming the
            # TASK is not a gate — task_claim deliberately succeeds for the agent
            # that already holds the lock, because that is the renewal path.
            try:
                taken = self.hub_client.call_tool(
                    "delegation_manage",
                    {
                        "action": "claim",
                        "params": {"id": delegation_id, "agent": self.agent_label},
                    },
                )
            except Exception as e:
                logger.info("Delegation claim failed for #%s: %s", delegation_id, e)
                continue
            if not taken.get("claimed"):
                # Another daemon got there first, or it is no longer pending.
                logger.debug("delegation #%s already taken; skipping", delegation_id)
                continue

            generation = (taken.get("delegation") or {}).get("claim_generation")
            if not isinstance(generation, int) or isinstance(generation, bool):
                logger.error(
                    "delegation #%s claim returned no valid generation; leaving it in flight",
                    delegation_id,
                )
                continue

            # Attempt atomic claim via hub client
            try:
                claim_res = self.hub_client.call_tool(
                    "task_claim",
                    {
                        "id": task_id,
                        "agent": self.agent_label,
                        "timeout_seconds": self.lock_timeout_s,
                    },
                )
            except Exception as e:
                logger.info("Claim attempt failed for task #%s: %s", task_id, e)
                self._release_delegation(delegation_id, generation)
                continue

            if task_ownership.is_safe_wait(claim_res):
                # Someone else owns the next move: put the delegation back, as
                # the ConflictError path above does (task #2388).
                logger.info(
                    "task #%s not claimed: %s",
                    task_id,
                    task_ownership.describe_wait(claim_res["safe_wait"]),
                )
                self._release_delegation(delegation_id, generation)
                continue

            if not claim_res or "id" not in claim_res:
                self._release_delegation(delegation_id, generation)
                continue

            logger.info("Successfully claimed task #%s (%s)", task_id, title)
            return self._execute_claimed_task(taken.get("delegation") or item, claim_res)

        return None

    def _cycle_gate_for_task(self, task_id: int) -> dict[str, Any] | None:
        """The kill/budget gate's cycle answer for `task_id` (task #992).

        Prefers the direct local `storage.connect()` read (cheap, no network)
        and falls back to a hub round-trip — `autonomous_cycle_manage(action=
        "get_for_task")` — only when the local read raises. That fallback
        exists for exactly one desktop-executor scenario: local memory.db is
        an intentional 82-byte tombstone (task #987; the Mac hub, not any
        desktop, is authoritative — see docs/mac-hub-topology.md), so the
        direct read raises `storage.LiveStoreError`/`sqlite3.Error` every
        time, and PR #509 only wrapped `reclaim_expired_locks`'s call, not
        this one.

        This is a kill switch, not a convenience read (task #373 Tier 2, 3.3;
        docs/s5-bounded-autonomy-acceptance-gates.md MUST 2), so the fallback
        does not swallow a hub failure into "no cycle" the way
        `reclaim_expired_locks` swallows its own local-only failure — that
        would silently turn every desktop executor with a tombstoned local DB
        into one where a killed or budget-blocked cycle can never actually
        stop further execution. Instead, if the hub round-trip *also* fails,
        this raises `RuntimeError`: the gate could not be verified, so the
        caller must fail closed (refuse execution, terminal-fail the
        delegation) rather than either crash the daemon or proceed as if
        nothing were bound to the task. Caught by
        scripts/kill_switch_rehearsal.py, which exists to catch exactly this
        class of silent bypass.
        """
        try:
            return cycles_tool.active_cycle_for_task(task_id)
        except (storage.LiveStoreError, sqlite3.Error) as exc:
            logger.warning(
                "local cycle-gate lookup for task #%s unusable (%s); falling back to "
                "hub round-trip via autonomous_cycle_manage(get_for_task)",
                task_id,
                exc,
            )
            try:
                res = self.hub_client.call_tool(
                    "autonomous_cycle_manage",
                    {"action": "get_for_task", "params": {"task_id": task_id}},
                )
            except Exception as hub_exc:
                raise RuntimeError(
                    f"cycle gate unverifiable for task #{task_id}: local storage "
                    f"unusable ({exc}) and hub round-trip failed ({hub_exc})"
                ) from hub_exc
            return res.get("cycle")

    def _lifecycle_stop_check(self, min_interval_s: float = 15.0) -> Callable[[], bool]:
        """A throttled "is this agent paused or terminated?" check (task #1351).

        Reads the local store first and falls back to the hub, like the cycle
        gate. Unlike the pre-dispatch gate this is a mid-flight stop signal, so
        an unreadable state keeps the child running (the hub already refuses the
        agent's claims and renewals, which bounds the run by its lease); it
        never kills work on a read failure. Records ``lifecycle.stop_confirmed``
        the first time it answers True.
        """
        from .. import agent_lifecycle

        label = self.agent_label
        memo = {"at": None, "stopped": False, "recorded": False}

        def read() -> bool:
            try:
                return agent_lifecycle.is_stopped(label)
            except (storage.LiveStoreError, sqlite3.Error):
                res = self.hub_client.call_tool(
                    "coordination_manage", {"action": "agent_state", "params": {"agent": label}}
                )
                return str(res.get("state") or "active") != "active"

        def check() -> bool:
            now = self.clock()
            if memo["at"] is None or now - memo["at"] >= min_interval_s:
                memo["at"] = now
                try:
                    memo["stopped"] = read()
                except Exception as exc:  # noqa: BLE001 - see docstring
                    logger.warning("lifecycle state for %s unreadable: %s", label, exc)
            if memo["stopped"] and not memo["recorded"]:
                memo["recorded"] = True
                agent_lifecycle.record_stop_confirmed(
                    label, task_id=None, detail="executor terminated its child: agent paused"
                )
            return bool(memo["stopped"])

        return check

    def _block_native_task(
        self, delegation: dict[str, Any], *, run_id: str, reason: str
    ) -> dict[str, Any]:
        """Retire this exact attempt and close its task run without native IO."""
        task_id = delegation["task_id"]
        delegation_id = delegation["id"]
        generation = delegation["claim_generation"]
        try:
            self._mark_delegation_failed(
                delegation_id, task_id, reason, generation, expected_run_id=run_id
            )
        except ExecutorOwnershipLost:
            if not self._retire_stopped_delegation(
                delegation, run_id=run_id, reason=reason, close_cancelled_run=False
            ):
                raise
        cleanup_error = None
        try:
            self.hub_client.call_tool(
                "task_update",
                {
                    "id": task_id,
                    "status": "blocked",
                    "worktree": False,
                    "release_claim": True,
                    "expected_run_id": run_id,
                    "expected_delegation_id": delegation_id,
                    "expected_claim_generation": generation,
                    "append_note": f"[native execution refused: {reason}]",
                },
            )
        except Exception as exc:
            cleanup_error = str(exc)
        return {
            "status": "blocked_by_trust_preset",
            "cleanup_error": cleanup_error,
            "task_id": task_id,
            "delegation_id": delegation_id,
            "reason": reason,
        }

    def _execute_claimed_task(
        self, delegation: dict[str, Any], task_claim_res: dict[str, Any]
    ) -> dict[str, Any]:
        execution = {}
        try:
            return self._execute_claimed_task_current(delegation, task_claim_res, execution)
        except ExecutorProvisioningFailed as exc:
            run_id = task_claim_res["current_run_id"]
            task_id, delegation_id = delegation["task_id"], delegation["id"]
            generation = delegation["claim_generation"]
            if not self._attempt_is_current(task_id, run_id, delegation_id, generation):
                cleanup_error = None
                try:
                    self._retire_stopped_delegation(delegation, run_id=run_id, reason=str(exc))
                except Exception as cleanup_exc:
                    cleanup_error = str(cleanup_exc)
                    logger.error(
                        "stopped task #%s provisioning cleanup failed: %s", task_id, cleanup_exc
                    )
                return {
                    **({"delegation_retirement_error": cleanup_error} if cleanup_error else {}),
                    "status": "run_not_current",
                    "task_id": task_id,
                    "delegation_id": delegation_id,
                    "reason": str(exc),
                }
            cleanup_error = None
            try:
                self._mark_delegation_failed(
                    delegation_id, task_id, str(exc), generation, expected_run_id=run_id
                )
                self.hub_client.call_tool(
                    "task_update",
                    {
                        "id": task_id,
                        "status": "blocked",
                        "worktree": False,
                        "expected_run_id": run_id,
                        "expected_delegation_id": delegation_id,
                        "expected_claim_generation": generation,
                        "append_note": f"[executor provisioning failed: {exc}]",
                    },
                )
            except ExecutorOwnershipLost:
                # Cancellation or takeover can win after the admission check
                # but before the failure RPC. Retire/acknowledge that stopped
                # attempt using the same exact run and generation fences.
                try:
                    self._retire_stopped_delegation(delegation, run_id=run_id, reason=str(exc))
                except Exception as cleanup_exc:
                    cleanup_error = str(cleanup_exc)
                    logger.error(
                        "stopped task #%s provisioning cleanup failed: %s", task_id, cleanup_exc
                    )
                return {
                    **({"delegation_retirement_error": cleanup_error} if cleanup_error else {}),
                    "status": "run_not_current",
                    "task_id": task_id,
                    "delegation_id": delegation_id,
                    "reason": str(exc),
                }
            except Exception as cleanup_exc:
                cleanup_error = str(cleanup_exc)
                logger.error("task #%s provisioning cleanup failed: %s", task_id, cleanup_exc)
            return {
                "status": "provisioning_failed",
                "task_id": task_id,
                "delegation_id": delegation_id,
                "reason": str(exc),
                "cleanup_error": cleanup_error,
            }
        except ExecutorOwnershipLost as exc:
            retirement_error = None
            try:
                self._retire_stopped_delegation(
                    delegation,
                    run_id=task_claim_res.get("current_run_id"),
                    reason=str(exc),
                )
            except Exception as cleanup_exc:
                retirement_error = str(cleanup_exc)
                logger.error(
                    "stopped task #%s cleanup failed: %s", delegation["task_id"], cleanup_exc
                )
            if execution.get("registry_session_id") is not None:
                registry.end_session(
                    execution["registry_session_id"], status="failed", exit_status="run_not_current"
                )
            if execution.get("attempt_id") is not None:
                evidence.finish_attempt(
                    execution["attempt_id"],
                    status="killed",
                    waiting_for_review=False,
                    detail=str(exc)[:500],
                )
            return {
                "status": "run_not_current",
                "task_id": delegation["task_id"],
                "delegation_id": delegation["id"],
                "run_id": task_claim_res.get("current_run_id"),
                "worktree": execution.get("worktree"),
                "commit": exc.commit or execution.get("commit"),
                "reason": str(exc),
                **({"delegation_retirement_error": retirement_error} if retirement_error else {}),
            }

    def _execute_claimed_task_current(
        self, delegation: dict[str, Any], task_claim_res: dict[str, Any], execution: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a claimed task via Pi RPC and transition to finish-to-review lifecycle."""
        task_id = delegation["task_id"]
        delegation_id = delegation["id"]
        generation = delegation.get("claim_generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            logger.error("delegation #%s has no valid claim generation", delegation_id)
            return {
                "status": "missing_claim_generation",
                "task_id": task_id,
                "delegation_id": delegation_id,
            }
        run_id = task_claim_res.get("current_run_id")
        task_spec = delegation["task_spec"]
        from_agent = delegation["from_agent"]
        if not isinstance(run_id, str) or not run_id.startswith("sp-"):
            self._retire_stopped_delegation(
                delegation, run_id=None, reason="task claim returned no run token"
            )
            return {
                "status": "missing_run_token",
                "task_id": task_id,
                "delegation_id": delegation_id,
                "reason": "task claim did not return a self-pickup run token",
            }

        if task_claim_res.get("budget_stop"):
            reason = task_claim_res["budget_stop"]
            self._mark_delegation_failed(
                delegation_id, task_id, reason, generation, expected_run_id=run_id
            )
            return {
                "status": "blocked_by_budget",
                "task_id": task_id,
                "delegation_id": delegation_id,
                "reason": reason,
            }

        admission = task_claim_res.get("native_execution_allowed")
        authoritative = getattr(self.hub_client, "authoritative_run_checks", False) is True
        if authoritative and admission is not True:
            # A remote worker with absent/stale local policy needs an explicit
            # hub attestation before any cycle/worktree/session/backend IO.
            # Older or malformed hub responses supply no such authority.
            reason = (
                "hub denied native execution"
                if admission is False
                else "hub did not attest current native execution admission"
            )
            return self._block_native_task(delegation, run_id=run_id, reason=reason)
        if admission is False:
            reason = str(
                task_claim_res.get("native_execution_denial") or "hub denied native execution"
            )
            return self._block_native_task(delegation, run_id=run_id, reason=reason)
        if authoritative and _cancel_requested_for_task(task_id, self.hub_client, run_id):
            return self._block_native_task(
                delegation,
                run_id=run_id,
                reason="hub could not attest this current run's native execution admission",
            )

        # Native confinement cannot be delegated away. Re-check both live
        # bindings before worktree localization, registry writes or backend IO.
        try:
            trust_presets.require_native_allowed(self.agent_label, originator=from_agent)
        except ToolError as exc:
            return self._block_native_task(delegation, run_id=run_id, reason=str(exc))

        # 0. Bounded-autonomy gate (task #373 Tier 2, 3.3). Most delegated
        # tasks have no autonomous_cycles row at all — cycles are opt-in — so
        # this is a no-op for the common case. When a cycle IS bound to this
        # task, a 'killed' or 'blocked' cycle must not start new work: this is
        # the enforcement half of the budget system (autonomous_cycle_
        # record_usage already computes "over budget" correctly, but nothing
        # used to check it before dispatching more work — see
        # docs/s5-bounded-autonomy-acceptance-gates.md MUST 2). Direct DB read
        # first, same precedent as reclaim_expired_locks/renew_lock's internal
        # bookkeeping reads — but unlike those, this one falls back to the hub
        # (task #992) rather than silently skipping on failure: this is the
        # kill switch itself, and treating "can't read the gate" as "no cycle"
        # would make the kill switch a no-op on every desktop executor whose
        # local memory.db is a tombstone (task #987; see
        # docs/mac-hub-topology.md). See _cycle_gate_for_task's docstring
        # for the fail-closed behavior when even the hub is unreachable.
        try:
            cycle = self._cycle_gate_for_task(task_id)
        except RuntimeError as exc:
            reason = str(exc)
            logger.error(
                "task #%s: cycle gate unverifiable (%s); refusing to execute "
                "fail-closed — an unproven gate is not a passing gate",
                task_id,
                reason,
            )
            self._mark_delegation_failed(
                delegation_id, task_id, reason, generation, expected_run_id=run_id
            )
            evidence.record_executor_event(
                "quarantine",
                task_id=task_id,
                delegation_id=delegation_id,
                agent=self.agent_label,
                reason=reason,
            )
            return {
                "status": "blocked_by_cycle_gate_unverifiable",
                "task_id": task_id,
                "delegation_id": delegation_id,
                "reason": reason,
            }
        if cycle is not None and cycle["state"] in ("killed", "blocked"):
            reason = cycle.get("stop_reason") or f"cycle is {cycle['state']}"
            logger.warning(
                "task #%s has a %s autonomous cycle (%s); refusing to execute",
                task_id,
                cycle["state"],
                reason,
            )
            # Deliberately _mark_delegation_failed, not _release_delegation.
            # release() puts the row back to 'pending' for immediate retry —
            # correct for a transient claim race, wrong here: the very next
            # poll would reclaim the same delegation, find the same killed/
            # blocked cycle, and refuse again, forever, at poll_interval
            # cadence — a busy-loop with no operator ever in the path. A kill
            # or budget-block is a deliberate, standing stop, not a hiccup;
            # it needs a human decision (resume the cycle and re-delegate) to
            # move again, so the delegation goes terminal instead. Caught by
            # scripts/kill_switch_rehearsal.py, which failed on this exact
            # busy-loop before this fix.
            self._mark_delegation_failed(
                delegation_id,
                task_id,
                f"autonomous cycle {reason}",
                generation,
                expected_run_id=run_id,
            )
            evidence.record_executor_event(
                "quarantine",
                task_id=task_id,
                delegation_id=delegation_id,
                agent=self.agent_label,
                reason=f"autonomous cycle {reason}",
            )
            return {
                "status": "blocked_by_cycle",
                "task_id": task_id,
                "delegation_id": delegation_id,
                "cycle_id": cycle["id"],
                "cycle_state": cycle["state"],
                "reason": reason,
            }

        # 0.5. Executor Trust Ladder Stage 2: Refuse execution of untrusted tasks
        # unless explicit quarantine execution is permitted (UA_EXECUTOR_ALLOW_UNTRUSTED=1).
        is_untrusted = bool(task_claim_res.get("untrusted") or delegation.get("untrusted"))
        if is_untrusted and not config.executor_allow_untrusted():
            reason = (
                f"untrusted task #{task_id} quarantined: Executor Trust Ladder "
                "Stage 2 allows trusted tasks only (untrusted=0)"
            )
            logger.warning(reason)
            self._mark_delegation_failed(
                delegation_id, task_id, reason, generation, expected_run_id=run_id
            )
            evidence.record_executor_event(
                "quarantine",
                task_id=task_id,
                delegation_id=delegation_id,
                agent=self.agent_label,
                reason=reason,
            )
            fail_notes = "[failed: untrusted task quarantined (Trust Ladder Stage 2)]"
            worktree = self.task_worktree(task_claim_res)
            if worktree:
                fail_notes += f" [worktree: {worktree}]"
            try:
                self.hub_client.call_tool(
                    "task_update",
                    {
                        "id": task_id,
                        "status": "blocked",
                        "append_note": fail_notes,
                        "expected_run_id": run_id,
                        "expected_delegation_id": delegation_id,
                        "expected_claim_generation": generation,
                    },
                )
            except Exception:
                pass
            return {
                "status": "quarantined_untrusted",
                "task_id": task_id,
                "delegation_id": delegation_id,
                "failed_terminal": True,
                "worktree": worktree,
                "reason": reason,
            }

        if not self._attempt_is_current(task_id, run_id, delegation_id, generation):
            self._retire_stopped_delegation(
                delegation,
                run_id=run_id,
                reason="claimed attempt no longer current before provisioning",
            )
            return {
                "status": "claim_not_current",
                "task_id": task_id,
                "delegation_id": delegation_id,
            }

        # Provision through the canonical helper, fenced to run + generation.
        # Existing task sandbox/review/merge primitives retain the same binding.
        try:
            provisioned = self.hub_client.call_tool(
                "task_update",
                {
                    "id": task_id,
                    "worktree": True,
                    "expected_run_id": run_id,
                    "expected_delegation_id": delegation_id,
                    "expected_claim_generation": generation,
                },
            )
        except Exception as exc:
            raise ExecutorProvisioningFailed(
                f"canonical worktree provisioning failed: {exc}"
            ) from exc
        if not isinstance(provisioned, dict) or provisioned.get("current_run_id") != run_id:
            raise ExecutorProvisioningFailed("canonical worktree admission did not attest this run")
        # task_update's response omits the run's frozen operating snapshot;
        # keep the one the hub's claim response carried (task #1756).
        if "operating_profile" in task_claim_res and "operating_profile" not in provisioned:
            provisioned = {**provisioned, "operating_profile": task_claim_res["operating_profile"]}
        task_claim_res = provisioned
        worktree = self.task_worktree(task_claim_res)
        if worktree is None and not config.agent_skips_task_worktree(self.agent_label):
            workspace = config.workspace_root()
            if (
                authoritative
                or self._git(
                    workspace, ["rev-parse", "--is-inside-work-tree"], check=False
                ).returncode
                == 0
            ):
                raise ExecutorProvisioningFailed("hub did not bind an isolated canonical checkout")
        if worktree is not None:
            source_sha = task_claim_res.get("worktree_source_sha")
            if (
                not isinstance(source_sha, str)
                or len(source_sha) not in (40, 64)
                or any(char not in "0123456789abcdef" for char in source_sha)
            ):
                raise ExecutorProvisioningFailed(
                    "hub did not attest the canonical checkout source SHA"
                )
            worktree = self.localize_worktree(
                worktree,
                task_id,
                task_claim_res.get("branch"),
                hub_repo=task_claim_res.get("project"),
                hub_branch_head=source_sha,
                recreate_stale=False,
            )
            if worktree is None:
                raise ExecutorProvisioningFailed(
                    "canonical run worktree could not be localized safely"
                )
        execution["worktree"] = worktree
        wt_rel_path = worktree if worktree is not None else "."
        if worktree is None:
            logger.warning("task #%s executes WITHOUT worktree isolation (workspace root)", task_id)

        # 2. Renew lock before starting execution
        if not self.renew_lock(task_id, run_id, expected_lane=task_claim_res.get("lane")):
            reason = "task run was cancelled or superseded before executor start"
            self._retire_stopped_delegation(delegation, run_id=run_id, reason=reason)
            return {
                "status": "claim_not_current",
                "task_id": task_id,
                "delegation_id": delegation_id,
                "reason": reason,
            }

        # 3. Invoke the configured execution backend (pi | codex).
        # Scout shape (task #1027): investigation report + decision inventory,
        # never a PR branch — use the scout prompt instead of the ship prompt.
        from ..scout import (
            default_report_relpath,
            is_scout,
            report_path_from_text,
            scout_executor_prompt,
        )

        notes_for_shape = task_claim_res.get("notes") or ""
        scout_run = is_scout(notes_for_shape) or is_scout(task_spec)
        scout_report_path = None
        if scout_run:
            prompt = scout_executor_prompt(
                task_id=task_id, task_spec=task_spec, from_agent=from_agent
            )
            prior_suggested = report_path_from_text(prompt)
            scout_report_path = default_report_relpath(
                f"task-{task_id}-{run_id}-delegation-{delegation_id}-generation-{generation}"
            )
            if prior_suggested:
                prompt = prompt.replace(prior_suggested, scout_report_path)
            prompt = prompt.replace(
                "(adjust the path if you chose a better inbox filename).",
                "(Use this exact attempt-specific path and marker.)",
            )
        else:
            prompt = (
                f"You are executing delegated task #{task_id} from {from_agent}.\n\n"
                f"TASK INSTRUCTIONS:\n{task_spec}\n\n"
                f"Work strictly within the workspace/worktree context. Do not escape your sandbox."
            )
        # Operating profile planning level and standing instructions (task #1756).
        from ..operating_profiles import append_operating_brief

        prompt = append_operating_brief(
            prompt, task_id, agent=self.agent_label, claim=task_claim_res
        )

        session_dir = ".worktrees/sessions"
        session_id = f"task-{task_id}-{run_id}-delegation-{delegation_id}-g{generation}"

        # Budget-capped timeout (task #373 3.3 MUST 2): a bounded cycle's
        # subprocess must not be able to run past its own remaining time
        # budget. Passing a smaller timeout_s makes the existing
        # subprocess.run(timeout=...) mechanism in every backend the real,
        # automatic enforcement point — no backend rewrite needed. Unbounded
        # (no cycle) uses UA_EXECUTOR_TIMEOUT_S (default 300) so long
        # delegated work can opt into a larger host-local ceiling (task #1149).
        timeout_s = config.executor_timeout_s()
        if cycle is not None:
            remaining = max(1.0, cycle["max_time_s"] - cycle["used_time_s"])
            timeout_s = min(timeout_s, int(remaining))

        # Record the run as a first-class session. Until now a run left only a
        # JSONL transcript on disk that nothing indexed, so "what is the executor
        # doing right now?" had no answer. Best-effort by contract: registry helpers
        # never raise, and a None id simply means this run goes unrecorded — the task
        # must still execute. The kind is backend-specific ("pi_run"/"codex_run") so
        # the registry says which agent actually ran the work.
        model_override = config.parse_model_override(task_spec) or config.parse_model_override(
            notes_for_shape
        )
        backend_fallback = getattr(self.backend, "model", None)
        if model_override is None:
            # The run's operating profile may pick the model, never the
            # provider (task #1756): a cross-provider model is refused.
            from .. import operating_profiles

            claimed, launch_snapshot = operating_profiles.claim_snapshot(task_claim_res)
            if not claimed:
                launch_snapshot = operating_profiles.settings_for_launch(task_id, self.agent_label)
            model_override, _reason = operating_profiles.profile_model(
                launch_snapshot,
                agent_label=self.agent_label,
                backend=getattr(self.backend, "name", None),
                backend_fallback=backend_fallback,
            )
        run_model = config.resolve_run_model(
            self.agent_label,
            override=model_override,
            backend_fallback=backend_fallback,
        )
        registry_session_id = registry.start_session(
            self.agent_label,
            getattr(self.backend, "session_kind", f"{self.backend.name}_run"),
            task_id=task_id,
            delegation_id=delegation_id,
            worktree_path=wt_rel_path,
            branch=task_claim_res.get("branch"),
            external_session_id=session_id,
            transcript_path=f"{session_dir}/{session_id}.jsonl",
            model=run_model,
        )
        execution["registry_session_id"] = registry_session_id
        attempt_id = evidence.start_attempt(
            task_id=task_id,
            delegation_id=delegation_id,
            agent=self.agent_label,
            backend=getattr(self.backend, "name", "unknown"),
            task_type="scout" if scout_run else "unknown",
            model=run_model,
        )

        execution["attempt_id"] = attempt_id

        # Mid-flight kill-switch poll (task #383): a bound cycle can enter
        # 'killed' while its subprocess is already running, not just before
        # dispatch (that case is the gate at the top of this function). A
        # backend that honours kill_check re-checks this every
        # poll_interval_s and terminates the child immediately instead of
        # waiting out the budget-capped timeout_s. None when no cycle is
        # bound — unbounded runs keep exactly their prior behaviour.
        kill_check: Callable[[], bool] | None = None
        cycle_check: Callable[[], bool] | None = None
        if cycle is not None:
            cycle_id = cycle["id"]
            cycle_check = lambda: cycles_tool.cycle_state(cycle_id) == "killed"  # noqa: E731
        # Agent lifecycle (task #1351): a pause or terminate of this executor's
        # agent stops the running child too, not only new claims.
        lifecycle_stop = self._lifecycle_stop_check()

        def kill_check() -> bool:  # noqa: F811 - rebinds the Optional above
            try:
                trust_presets.require_native_allowed(self.agent_label, originator=from_agent)
            except ToolError:
                return True
            if cycle_check is not None and cycle_check():
                return True
            if lifecycle_stop():
                return True
            # Task #1525 (PR #866 review: "mid-run cancellation misses Pi
            # executions"): a `request_cancel` on this task's current run
            # must stop the child the same way a killed cycle or a paused
            # agent does — every backend that honours `kill_check` already
            # gets this for free, including PiBackend once it wires the
            # callback through (see backends.py PiBackend / pi_runner.py).
            return not self._attempt_is_current(task_id, run_id, delegation_id, generation)

        # Wall-clock measured by the daemon itself, not self-reported by
        # whatever the backend executed — this is the real automatic usage
        # collection MUST 1 requires (docs/s5-bounded-autonomy-acceptance-
        # gates.md): an agent cannot stay invisible to the budget system by
        # never reporting, because it never gets the choice.
        if not self._attempt_is_current(task_id, run_id, delegation_id, generation):
            raise ExecutorOwnershipLost("claimed attempt changed before backend start")
        run_started_at = self.clock()
        try:
            with trust_presets._native_execution_scope(self.agent_label, originator=from_agent):
                run_res = self.backend.run(
                    prompt=prompt,
                    cwd=wt_rel_path,
                    session_dir=session_dir,
                    session_id=session_id,
                    agent_label=self.agent_label,
                    timeout_s=timeout_s,
                    kill_check=kill_check,
                    model=run_model,
                )
        except (KeyboardInterrupt, SystemExit):
            # A real stop signal must propagate immediately, not get treated as
            # a business failure subject to backoff/breaker accounting. A killed
            # executor must not leave the row reading 'running' forever — the
            # reaper would eventually catch it, but only after the abandon window.
            registry.end_session(registry_session_id, status="failed", exit_status="exception")
            evidence.finish_attempt(attempt_id, status="exception", detail="backend raised")
            raise
        except Exception as e:
            # A backend that cannot even start (e.g. Pi's ToolError when its
            # subprocess fails to spawn) must not be invisible to the circuit
            # breaker, and must not leave the delegation in_flight forever with
            # no accounting until the lock-expiry reclaim eventually finds it.
            # Normalize it into the same failed-execution result the code below
            # already knows how to count toward the breaker, requeue with
            # backoff, or fail at the attempt ceiling — exactly what happens to
            # a run that completed but reported failure (task #445 / PR #194
            # review). registry/evidence bookkeeping happens exactly once,
            # below, alongside every other outcome.
            logger.warning("backend raised for task #%s: %s", task_id, e, exc_info=True)
            run_res = {"status": "error", "text": f"backend raised: {e}"}
        elapsed_s = max(0.0, self.clock() - run_started_at)

        exec_status = run_res.get("status", "error")
        usage_id = str(uuid.uuid4())

        # Capture accounting before ownership checks. The original run can
        # accept late usage after cancellation, expiry or takeover, and each
        # retry contributes to the durable cumulative total. Lifecycle writes
        # below remain subject to current task/run/delegation ownership.
        run_usage = backends.to_run_usage(
            run_res.get("usage"), model=run_model, billing=run_res.get("billing")
        )
        # Task #1841: a harness that stopped at its plan usage limit says so
        # on the run, so the finish reads capacity_ceiling, not a lease lapse.
        capacity = backends.capacity_from_run_result({**run_res, "status": exec_status})
        if capacity is not None:
            run_usage = {**(run_usage or {}), "capacity": capacity}
        usage_report = None
        # No telemetry still means an executed invocation with unknown cost.
        # Persist its marker so a later priced retry cannot erase uncertainty.
        try:
            cumulative_usage = self._accumulate_run_usage(
                task_id,
                run_id,
                usage_id,
                run_usage or {},
                succeeded=exec_status == "success",
            )
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # A broken spool must not skip lifecycle cleanup or permit a
            # retry whose earlier accounting has silently disappeared.
            logger.error("task #%s: cumulative usage persistence failed: %s", task_id, exc)
            retained = (
                exc.usage if isinstance(exc, ExecutorUsagePersistenceFailed) else (run_usage or {})
            )
            # Earlier attempts' bills cannot be proven either, so the total is
            # never exempt from dollar budgets (task #1837).
            cumulative_usage = {**retained, "cost_complete": False, "billing": "api"}
            exec_status = "accounting_persistence_failed"
        usage_report = self._report_run_usage(
            task_id, run_id, cumulative_usage, expected_lane=task_claim_res.get("lane")
        )
        if usage_report is None:
            # One immediate bounded retry covers a brief outage before
            # finish/retry lifecycle writes can close this live run.
            usage_report = self._report_run_usage(
                task_id, run_id, cumulative_usage, expected_lane=task_claim_res.get("lane")
            )

        # Record real, daemon-measured usage against the bound cycle, if any
        # (task #373 3.3). Best-effort: a failure here must never break the
        # task's own success/failure path — it only means this run's budget
        # accounting is delayed to the next successful call. Tokens and cost
        # come from the harness when it reported them (task #1917).
        if cycle is not None:
            cycle_usage: dict[str, Any] = {
                "cycle_id": cycle["id"],
                "agent": self.agent_label,
                "elapsed_s": elapsed_s,
                "retries": 0 if exec_status == "success" else 1,
                "usage_run_id": run_id,
                "usage_id": usage_id,
            }
            if run_usage is not None:
                tokens = (run_usage.get("input_tokens") or 0) + (
                    run_usage.get("output_tokens") or 0
                )
                if tokens:
                    cycle_usage["tokens"] = tokens
                # A subscription or local run spends no dollars, even when
                # the CLI printed an API-price estimate (task #1837).
                if run_usage.get("cost_usd") is not None and run_usage.get("billing") not in (
                    "subscription",
                    "local",
                ):
                    cycle_usage["cost_cents"] = run_usage["cost_usd"] * 100.0
            try:
                usage_res = self._report_cycle_usage(cycle_usage)
                if usage_res and usage_res.get("blocked"):
                    logger.warning(
                        "cycle %s went over budget after task #%s: %s",
                        cycle["id"],
                        task_id,
                        usage_res.get("reason"),
                    )
            except Exception as e:
                logger.warning("failed to record cycle usage for task #%s: %s", task_id, e)

        # A different claimant may have taken over while this backend was
        # running. Do not commit its files or mutate the shared delegation
        # after that point; the successor now owns those lifecycle writes.
        if not self._attempt_is_current(task_id, run_id, delegation_id, generation):
            retirement_error = None
            try:
                self._retire_stopped_delegation(
                    delegation,
                    run_id=run_id,
                    reason="task run cancelled or superseded during execution",
                )
            except Exception as exc:  # noqa: BLE001 - local finalization still has to run
                retirement_error = str(exc)
                logger.error(
                    "could not retire stopped delegation #%s; recovery required: %s",
                    delegation_id,
                    exc,
                )
            finally:
                try:
                    registry.end_session(
                        registry_session_id, status="failed", exit_status="run_not_current"
                    )
                finally:
                    evidence.finish_attempt(
                        attempt_id,
                        status="killed",
                        detail=(
                            "run cancelled or superseded"
                            + (
                                f"; delegation retirement pending: {retirement_error}"
                                if retirement_error is not None
                                else ""
                            )
                        )[:500],
                    )
            result = {
                "status": "run_not_current",
                "task_id": task_id,
                "delegation_id": delegation_id,
                "run_result": run_res,
            }
            if retirement_error is not None:
                result["delegation_retirement_error"] = retirement_error
            return result

        registry.end_session(
            registry_session_id,
            status="completed" if exec_status == "success" else "failed",
            exit_status=exec_status,
            summary=(run_res.get("text") or "")[:500] or None,
        )
        summary_text = run_res.get("text", "")[:500]

        notes = task_claim_res.get("notes") or ""

        # Prefer the backend error string for attempt detail so named failure
        # classes (agy_background_teardown, agy_teardown_resume_exhausted,
        # …) reach classify_failure_reason. Append resume_count when present
        # (task #1172 auto-resume bookkeeping).
        attempt_detail = (run_res.get("error") or summary_text or "")[:500]
        resume_count = run_res.get("resume_count")
        if resume_count:
            suffix = f"; resume_count={resume_count}"
            attempt_detail = (attempt_detail + suffix)[:500]

        # Task #1950 (PR #1230 review round 3): a backend that stopped its
        # child but could not confirm the process group is gone reports
        # cleanup_confirmed=False. A survivor may still be writing the
        # worktree, so an automatic retry would start a second writer beside
        # it. Hold the work for an operator instead: no retry, the delegation
        # failed, the task blocked with the backend's actionable note (which
        # names the process group to inspect). The confirmed-cleanup path
        # below is unchanged.
        if run_res.get("cleanup_confirmed") is False:
            reason = (
                f"Execution held, no automatic retry: child cleanup unconfirmed "
                f"({exec_status}): {attempt_detail}"
            )[:500]
            evidence.finish_attempt(attempt_id, status=exec_status, detail=reason)
            evidence.record_executor_event(
                "quarantine",
                task_id=task_id,
                delegation_id=delegation_id,
                agent=self.agent_label,
                reason=reason,
            )
            self._mark_delegation_failed(
                delegation_id, task_id, reason, generation, expected_run_id=run_id
            )
            hold_notes = f"[blocked: cleanup unconfirmed] {attempt_detail}"
            if worktree:
                hold_notes += f" [worktree: {worktree}]"
            try:
                self.hub_client.call_tool(
                    "task_update",
                    {
                        "id": task_id,
                        "status": "blocked",
                        "append_note": hold_notes,
                        "expected_run_id": run_id,
                        "expected_delegation_id": delegation_id,
                        "expected_claim_generation": generation,
                    },
                )
            except Exception:
                pass
            return {
                "status": "cleanup_unconfirmed",
                "exec_status": exec_status,
                "task_id": task_id,
                "delegation_id": delegation_id,
                "failed_terminal": True,
                "worktree": worktree,
                "run_result": run_res,
            }

        # A kill-switch stop (task #383) is a deliberate, standing stop, not a
        # transient failure — same reasoning as the pre-dispatch gate above.
        # Route it straight to terminal instead of the attempts/backoff policy
        # below: retrying would just re-dispatch a subprocess for a cycle that
        # is still 'killed', and the pre-dispatch gate would refuse it anyway
        # on the next poll — this skips that wasted round trip.
        budget_stop = usage_report.get("budget_stop") if usage_report else None
        accounting_failed = exec_status == "accounting_persistence_failed"
        if exec_status == "killed" or budget_stop or accounting_failed:
            reason = (
                "Execution accounting persistence failed; recovery required"
                if accounting_failed
                else f"Execution stopped by budget: {budget_stop}"
                if budget_stop
                else f"Execution stopped by kill switch: {summary_text}"
            )
            evidence.finish_attempt(attempt_id, status=exec_status, detail=reason)
            evidence.record_executor_event(
                "quarantine",
                task_id=task_id,
                delegation_id=delegation_id,
                agent=self.agent_label,
                reason=reason,
            )
            self._mark_delegation_failed(
                delegation_id, task_id, reason, generation, expected_run_id=run_id
            )
            fail_notes = (
                "[failed: accounting persistence]"
                if accounting_failed
                else "[failed: budget stop]"
                if budget_stop
                else "[failed: kill switch]"
            )
            if worktree:
                fail_notes += f" [worktree: {worktree}]"
            try:
                self.hub_client.call_tool(
                    "task_update",
                    {
                        "id": task_id,
                        "status": "doing",
                        "append_note": fail_notes,
                        "expected_run_id": run_id,
                        "expected_delegation_id": delegation_id,
                        "expected_claim_generation": generation,
                    },
                )
            except Exception:
                pass
            return {
                "status": exec_status,
                "task_id": task_id,
                "delegation_id": delegation_id,
                "failed_terminal": True,
                "worktree": worktree,
                "run_result": run_res,
            }

        # Non-success execution handling (failure policy & circuit breaker)
        if exec_status != "success":
            evidence.finish_attempt(attempt_id, status=exec_status, detail=attempt_detail)
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.circuit_breaker_threshold:
                tripped_at_count = self.consecutive_failures
                self.circuit_breaker_tripped_until = self.clock() + self.circuit_breaker_cooldown_s
                cb_msg = (
                    f"Circuit breaker tripped after {tripped_at_count} consecutive failures; "
                    f"cooldown {self.circuit_breaker_cooldown_s}s active"
                )
                logger.warning("%s (agent: %s)", cb_msg, self.agent_label)
                storage.record_agent_event(
                    "executor_circuit_breaker",
                    ref_id=str(delegation_id),
                    agent=self.agent_label,
                    summary=cb_msg,
                )
                evidence.record_executor_event(
                    "cooldown",
                    task_id=task_id,
                    delegation_id=delegation_id,
                    agent=self.agent_label,
                    reason=cb_msg,
                )
                # A trip consumes the failure budget: the next N failures after
                # cooldown get a fresh count, rather than immediately re-tripping.
                self.consecutive_failures = 0

            attempts = self._read_delegation_attempts(delegation_id)

            reason = f"Execution failed ({exec_status}): {summary_text}"
            if attempts >= self.max_attempts:
                self._mark_delegation_failed(
                    delegation_id,
                    task_id,
                    f"Attempt ceiling reached ({attempts}/{self.max_attempts}): {reason}",
                    generation,
                    expected_run_id=run_id,
                )
                fail_notes = f"[failed: attempt ceiling reached ({attempts}/{self.max_attempts})]"
                if worktree:
                    fail_notes += f" [worktree: {worktree}]"
                try:
                    self.hub_client.call_tool(
                        "task_update",
                        {
                            "id": task_id,
                            "status": "doing",
                            "append_note": fail_notes,
                            "expected_run_id": run_id,
                            "expected_delegation_id": delegation_id,
                            "expected_claim_generation": generation,
                        },
                    )
                except Exception:
                    pass
                return {
                    "status": exec_status,
                    "task_id": task_id,
                    "delegation_id": delegation_id,
                    "failed_terminal": True,
                    "worktree": worktree,
                    "run_result": run_res,
                }
            else:
                retry_reason = (
                    f"Attempt {attempts}/{self.max_attempts} failed ({exec_status}); backoff active"
                )
                self._retry_delegation_to_pending(
                    delegation_id, retry_reason, generation, task_id=task_id, expected_run_id=run_id
                )
                return {
                    "status": exec_status,
                    "task_id": task_id,
                    "delegation_id": delegation_id,
                    "backoff_active": True,
                    "worktree": worktree,
                    "run_result": run_res,
                }

        # 4. Finish-to-Review Lifecycle (Success Path)
        # Reset circuit breaker consecutive failures counter
        self.consecutive_failures = 0

        from ..scout import (
            evidence_ref_for_report,
            is_scout,
            report_path_from_text,
        )

        # Scout runs never commit; the shared success return reports that as None.
        commit_res: dict[str, str | None] | None = None
        scout_finish = is_scout(notes) or scout_run
        if scout_finish:
            report_path = report_path_from_text(summary_text) or report_path_from_text(
                run_res.get("text")
            )
            if not report_path or report_path != scout_report_path:
                failure_code = (
                    "scout_report_missing" if not report_path else "scout_report_not_current"
                )
                reason = (
                    "scout finish refused: agent output missing "
                    "[scout: report=<vault-rel-path>] marker"
                    if not report_path
                    else "scout finish refused: report path does not belong to this run and delegation generation"
                )
                evidence.finish_attempt(
                    attempt_id,
                    status=exec_status,
                    waiting_for_review=False,
                    detail=reason[:500],
                )
                evidence.record_executor_event(
                    "quarantine",
                    task_id=task_id,
                    delegation_id=delegation_id,
                    agent=self.agent_label,
                    reason=reason,
                )
                fail_notes = f"[failed: {failure_code}] {reason}"
                self._mark_delegation_failed(
                    delegation_id, task_id, reason, generation, expected_run_id=run_id
                )
                try:
                    self.hub_client.call_tool(
                        "task_update",
                        {
                            "id": task_id,
                            "status": "blocked",
                            "append_note": fail_notes,
                            "expected_run_id": run_id,
                            "expected_delegation_id": delegation_id,
                            "expected_claim_generation": generation,
                        },
                    )
                except Exception:
                    pass
                return {
                    "status": failure_code,
                    "task_id": task_id,
                    "delegation_id": delegation_id,
                    "failed_terminal": True,
                    "worktree": worktree,
                    "run_result": run_res,
                    "reason": reason,
                }
            # Scout never opens a PR branch — skip daemon commit / SHA evidence.
            finish_operation = {"kind": "scout", "report_path": report_path}
            finish_evidence = evidence_ref_for_report(report_path)
        else:
            try:
                commit_res = self._commit_child_changes(
                    task_id=task_id,
                    worktree=worktree,
                    branch=task_claim_res.get("branch"),
                    expected_run_id=run_id,
                    delegation_id=delegation_id,
                    expected_claim_generation=generation,
                )
            except ExecutorOwnershipLost:
                raise
            except Exception as e:
                evidence.finish_attempt(
                    attempt_id,
                    status=exec_status,
                    waiting_for_review=False,
                    detail=f"execution succeeded but daemon commit failed: {e}"[:500],
                )
                raise

            execution["commit"] = commit_res

            # Zero-output gate (task #1158): backend success + clean *task*
            # worktree means nothing observable landed. Fail loud and terminal
            # — do not stamp waiting-for-review or retry through the attempt
            # ceiling. Runs without a worktree (non-git workspace) keep the
            # prior finish-to-review path; there is no commit to verify.
            if worktree and not commit_res.get("sha"):
                reason = (
                    f"{ZERO_OUTPUT_ATTEMPT_REASON}: backend reported success but "
                    "worktree has no new commit (clean tree) — silent no-op will "
                    "not consume retry budget"
                )
                evidence.finish_attempt(
                    attempt_id,
                    status="error",
                    waiting_for_review=False,
                    detail=reason[:500],
                )
                evidence.record_executor_event(
                    "quarantine",
                    task_id=task_id,
                    delegation_id=delegation_id,
                    agent=self.agent_label,
                    reason=reason,
                )
                self.consecutive_failures += 1
                fail_notes = f"[failed: {ZERO_OUTPUT_ATTEMPT_REASON}]"
                if worktree:
                    fail_notes += f" [worktree: {worktree}]"
                try:
                    self.hub_client.call_tool(
                        "task_update",
                        {
                            "id": task_id,
                            "status": "doing",
                            "append_note": fail_notes,
                            "expected_run_id": run_id,
                            "expected_delegation_id": delegation_id,
                            "expected_claim_generation": generation,
                        },
                    )
                except Exception:
                    pass
                self._mark_delegation_failed(
                    delegation_id, task_id, reason, generation, expected_run_id=run_id
                )
                return {
                    "status": ZERO_OUTPUT_ATTEMPT_REASON,
                    "task_id": task_id,
                    "delegation_id": delegation_id,
                    "failed_terminal": True,
                    "worktree": worktree,
                    "commit": commit_res,
                    "run_result": run_res,
                    "reason": reason,
                }

            # DO NOT mark task as 'done'. Stamp delivery mode + [waiting-for: review].
            # Resolve delivery from the live hub row during the atomic finish.
            finish_operation = {"kind": "delivery"}
            finish_evidence = commit_res["sha"]

        try:
            self.hub_client.call_tool(
                "task_update",
                {
                    "id": task_id,
                    "status": "doing",
                    "executor_finish": finish_operation,
                    "evidence_ref": finish_evidence,
                    "expected_run_id": run_id,
                    "expected_delegation_id": delegation_id,
                    "expected_claim_generation": generation,
                },
            )
        except Exception as e:
            if not self._attempt_is_current(task_id, run_id, delegation_id, generation):
                raise ExecutorOwnershipLost(
                    "canonical closeout refused the superseded attempt",
                    commit=execution.get("commit"),
                ) from e
            # The run succeeded, but the task never actually got
            # [waiting-for: review] on it — the review scanner can't find it.
            # Finalizing this attempt as waiting_review regardless (the old
            # order) would have the trust-ladder evidence claim a state the
            # task was never put into (task #445 / PR #201 review); record
            # the real outcome instead and let the failure keep propagating.
            evidence.finish_attempt(
                attempt_id,
                status=exec_status,
                waiting_for_review=False,
                detail=f"execution succeeded but task lifecycle transition failed: {e}"[:500],
            )
            raise

        self._mark_delegation_completed(
            delegation_id,
            f"executor finish-to-review ({exec_status})",
            generation,
            task_id=task_id,
            expected_run_id=run_id,
        )

        evidence.finish_attempt(
            attempt_id,
            status=exec_status,
            waiting_for_review=True,
            detail=attempt_detail or summary_text,
        )

        # 5. Callbacks: update delegation memory note & daily journal
        if delegation.get("memory_id"):
            try:
                from ..tools.memory import memory_save

                memory_save(
                    content=(
                        f"\n\n--- Active Executor Execution Run ---\n"
                        f"Executed at: {storage.now()}\n"
                        f"Run: {run_id}; delegation generation: {generation}\n"
                        f"Worktree: {worktree}\n"
                        f"Status: {exec_status}\n"
                        f"Summary: {summary_text}\n"
                    ),
                    id=delegation["memory_id"],
                    author=self.agent_label,
                    append=True,
                )
            except Exception as e:
                logger.warning("Failed to update delegation memory note: %s", e)

        # Append vault journal entry
        try:
            # The journal is an index, not the execution record. Full paths,
            # ownership and backend output already live in the task/run evidence.
            journal_msg = (
                f"Executor task #{task_id}, delegation #{delegation_id}, "
                f"run={run_id}, generation={generation}: {exec_status}. Review requested."
            )
            self.hub_client.call_tool(
                "vault_journal",
                {
                    "entry": journal_msg,
                    "agent": self.agent_label,
                    "tags": ["executor", f"task-{task_id}"],
                },
            )
        except Exception as e:
            logger.warning("Failed to write vault journal entry: %s", e)

        # Optional iMessage notification
        if delegation.get("callback") == "notify":
            try:
                self.hub_client.call_tool(
                    "notify",
                    {
                        "message": (
                            f"[Active Executor] Task #{task_id} execution finished ({exec_status}). "
                            f"Run {run_id}, generation {generation} recorded [waiting-for: review]."
                        )
                    },
                )
            except Exception:
                pass

        return {
            "status": exec_status,
            "task_id": task_id,
            "delegation_id": delegation_id,
            "waiting_for_review": True,
            "run_id": run_id,
            "claim_generation": generation,
            "worktree": worktree,
            "commit": commit_res,
            "run_result": run_res,
        }

    def run_forever(self) -> None:
        """Run the supervisor daemon continuously."""
        self.running = True
        logger.info(
            "Starting Active Executor Daemon for agent %s (shadow_mode=%s)",
            self.agent_label,
            self.shadow_mode,
        )
        self.maybe_reclaim_expired_locks()

        while self.running:
            try:
                self.maybe_reclaim_expired_locks()
            except Exception as e:
                logger.error("Error in periodic expired-lock reclaim: %s", e, exc_info=True)
            try:
                self.poll_once()
            except Exception as e:
                logger.error("Error in daemon supervisor loop: %s", e, exc_info=True)
            time.sleep(self.poll_interval)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    from .. import execution_locality

    if execution_locality.devices_only():
        # Task #2109: the executor runs agents, so it belongs on a device.
        logger.error("%s", execution_locality.refusal_message("unified-agent-executor"))
        raise SystemExit(2)
    agent = config.executor_agent()
    if not agent:
        logger.error("UA_EXECUTOR_AGENT is required; refusing to start with no identity")
        raise SystemExit(2)
    shadow = os.environ.get("UA_EXECUTOR_SHADOW_MODE", "0") == "1"
    daemon = ExecutorDaemon(agent_label=agent, shadow_mode=shadow)
    # Which agent-label/backend pair is running is the first thing you want in
    # the log when two executor units are installed side by side.
    logger.info(
        "executor starting: agent=%s backend=%s shadow=%s",
        agent,
        daemon.backend.name,
        shadow,
    )
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        logger.info("Daemon stopped by user")


if __name__ == "__main__":
    main()
