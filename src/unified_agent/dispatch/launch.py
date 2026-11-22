"""Stubbable process-launch boundary for the approved-dispatch coordinator.

Task #548 introduced the claim/run persistence edge. Task #549 replaces the
noop default with a routed adapter: named-agent resolution, workspace/identity
policy, and an observable launch request. Task #551 adds
``FakeWorkerProcessSpawner`` for e2e smoke (bounded subprocess, no real CLIs).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, execution_locality, storage, trust_presets
from . import child_env, claude_sandbox, harness_sandbox, routing

_RUN_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_dispatch_run_id(raw: str | None) -> str:
    """Reject missing, placeholder, or path-unsafe run IDs before writing artifacts."""
    run_id = (raw or "").strip()
    if not run_id or run_id == "unknown":
        raise ValueError("invalid_run_id")
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        raise ValueError("invalid_run_id")
    if not _RUN_ID_SAFE_RE.fullmatch(run_id):
        raise ValueError("invalid_run_id")
    return run_id


def _validate_launch_cwd(raw: str | None) -> str:
    """Require the dispatch cwd to exist — do not recreate a removed worktree."""
    cwd = (raw or ".").strip() or "."
    try:
        path = Path(cwd).expanduser()
        if not path.is_dir():
            raise ValueError(f"cwd_missing:{cwd}")
        return str(path)
    except OSError as exc:
        raise ValueError(f"cwd_invalid:{cwd}:{exc}") from exc


def _argv0_unavailable(argv: list[str], *, cwd: str = ".") -> bool:
    """True when ``argv[0]`` cannot be executed (preflight before detached spawn).

    Relative paths are resolved against the launch ``cwd``, not the spawner's
    process cwd — routing through a detached supervisor means the exec'd process
    is python, which always exists, so this check must mirror the child's cwd.
    """
    exe = argv[0] if argv else ""
    if not exe:
        return True
    if os.path.dirname(exe):
        candidate = Path(exe)
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        return not candidate.is_file()
    return shutil.which(exe) is None


def _native_guard(agent_label: str, delegation_id: int | None) -> dict[str, Any] | None:
    """Re-read originating authority at the actual native execution edge."""
    originator = None
    if delegation_id is not None:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT from_agent FROM delegations WHERE id=?", (delegation_id,)
            ).fetchone()
            if row is not None:
                originator = row["from_agent"]
        finally:
            conn.close()
    try:
        trust_presets.require_native_allowed(agent_label, originator=originator)
    except ToolError as exc:
        return {"ok": False, "error": str(exc), "spawned": False, "pid": None}
    return None


class LaunchBoundary(Protocol):
    def launch(
        self,
        *,
        run_id: str,
        task_id: int,
        agent_label: str,
        delegation_id: int | None,
    ) -> dict[str, Any]: ...


class ProcessSpawner(Protocol):
    def spawn(self, request: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class RecordingLaunchBoundary:
    """Test double that records calls and never spawns a process."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: str | None = None

    def launch(
        self,
        *,
        run_id: str,
        task_id: int,
        agent_label: str,
        delegation_id: int | None,
    ) -> dict[str, Any]:
        call = {
            "run_id": run_id,
            "task_id": task_id,
            "agent_label": agent_label,
            "delegation_id": delegation_id,
        }
        self.calls.append(call)
        if self.fail_with:
            return {"ok": False, "error": self.fail_with}
        return {"ok": True, "stub": True, **call}


@dataclass
class NoopLaunchBoundary:
    """Legacy stub retained for tests that bypass routing on purpose."""

    def launch(
        self,
        *,
        run_id: str,
        task_id: int,
        agent_label: str,
        delegation_id: int | None,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "stub": True,
            "run_id": run_id,
            "task_id": task_id,
            "agent_label": agent_label,
            "delegation_id": delegation_id,
        }


@dataclass
class StubProcessSpawner:
    """Records resolved launch requests; never execs claude/codex/agy."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: str | None = None

    def spawn(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        if self.fail_with:
            return {"ok": False, "error": self.fail_with}
        return {
            "ok": True,
            "stub": True,
            "spawned": False,
            "run_id": request.get("run_id"),
            "agent_label": request.get("agent_label"),
            "cli_backend": request.get("cli_backend"),
            "pid": None,
        }


@dataclass
class CliProcessSpawner:
    """Start the agent's own headless CLI as a detached process.

    Used by Control Center dispatch. Scheduler ticks keep ``StubProcessSpawner``.
    Does not wait for the child to finish. Writes stdout/stderr to a log file.
    Does not put tokens into argv.
    """

    def spawn(self, request: dict[str, Any]) -> dict[str, Any]:
        # Task #2191: the per-run settings file and sidecar written below are
        # launch inputs, not evidence. A launch that does not spawn (refused by
        # the host sandbox probe, missing binary, failed supervisor) removes
        # what this call wrote; a spawned run's supervisor removes them when
        # the child exits. ``written`` holds the identity of each file this
        # call wrote, so a same-named file another launch owns is never removed.
        written: dict[Path, claude_sandbox.ArtifactIdentity] = {}
        result: dict[str, Any] | None = None
        try:
            result = self._spawn(request, written)
            return result
        finally:
            if written and not (result or {}).get("spawned"):
                claude_sandbox.discard_run_artifacts(written)

    def _spawn(
        self, request: dict[str, Any], written: dict[Path, claude_sandbox.ArtifactIdentity]
    ) -> dict[str, Any]:
        if execution_locality.devices_only():
            # Task #2109: a devices-only hub never starts an agent itself.
            return {
                "ok": False,
                "error": "server_execution_refused",
                "code": "hosted_execution_refused",
                "detail": execution_locality.refusal_message("local agent spawn"),
                "spawned": False,
                "pid": None,
            }
        refused = _native_guard(str(request.get("agent_label") or ""), request.get("delegation_id"))
        if refused is not None:
            return refused
        binary = str(request.get("binary") or "")
        cli_backend = str(request.get("cli_backend") or "")
        try:
            run_id = _validate_dispatch_run_id(request.get("run_id"))
            cwd = _validate_launch_cwd(request.get("cwd"))
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "spawned": False,
                "pid": None,
            }
        prompt = str(request.get("task_spec") or "")
        agent_label = str(request.get("agent_label") or "")
        task_id = request.get("task_id")
        settings_path: str | None = None
        sidecar_path: str | None = None
        # Dispatch-local unattended flag for CLIs that need it (Agy). Never set
        # global UA_*_SKIP_PERMISSIONS=1 — same posture as Claude's sandbox path.
        skip_permissions: bool | None = None

        # A write helper that raises removes its own temp and landed file
        # (identity-checked, task #2196), so only a returned path needs
        # recording in ``written``.
        if cli_backend in {"claude", "agy"} and task_id is not None and agent_label:
            try:
                sidecar_file = claude_sandbox.write_dispatch_sidecar(
                    run_id=run_id,
                    task_id=task_id,
                    agent_label=agent_label,
                )
                written.update(claude_sandbox.snapshot_artifacts([sidecar_file]))
                sidecar_path = str(sidecar_file)
                prompt = claude_sandbox.append_dispatch_report_instructions(
                    prompt,
                    run_id=run_id,
                    agent_label=agent_label,
                    sidecar_path=sidecar_path,
                )
            except OSError as exc:
                return {
                    "ok": False,
                    "error": f"dispatch_sidecar_setup_failed:{exc}",
                    "spawned": False,
                    "pid": None,
                }

        if cli_backend == "claude":
            try:
                settings_file = claude_sandbox.write_claude_dispatch_settings(run_id, cwd=cwd)
                written.update(claude_sandbox.snapshot_artifacts([settings_file]))
                settings_path = str(settings_file)
            except OSError as exc:
                return {
                    "ok": False,
                    "error": f"claude_sandbox_setup_failed:{exc}",
                    "spawned": False,
                    "pid": None,
                }
        elif cli_backend == "agy":
            # Headless Agy defaults to permission_mode=request-review and
            # auto-denies tools (soak #628 / run-628-39556904). Opt in only for
            # this CliProcessSpawner path — not via UA_AGY_SKIP_PERMISSIONS.
            skip_permissions = True

        if task_id is not None:
            from ..goals import append_why_chain

            prompt = append_why_chain(prompt, int(task_id))
        if agent_label:
            from ..skills_registry import append_assigned_skills

            # request["profile"] is the route's actual profile (set by
            # CoordinatorLaunchBoundary from AgentRoute.profile below), not
            # necessarily the agent's default profile — a Hermes-routed run
            # can carry a different profile than what
            # profiles.default_for_identity(agent_label) would guess.
            prompt = append_assigned_skills(prompt, agent_label, profile=request.get("profile"))
        if task_id is not None:
            from ..operating_profiles import append_operating_brief

            # Last, so the prompt ends with the Operating profile block
            # (docs/operating-profiles.md).
            prompt = append_operating_brief(prompt, int(task_id), agent=agent_label)

        # Explicit allowlist — never inherit service.env secrets wholesale.
        env = child_env.build_child_env(
            os.environ,
            extras=request.get("env") or {},
            agent_label=agent_label,
        )

        # Copilot/Cursor/Agy blanket-permission flags need a verified sandbox
        # posture (task #1948); headless_argv refuses them without one.
        sandbox_posture = None
        if cli_backend in harness_sandbox.NATIVE_SANDBOX:
            sandbox_posture = harness_sandbox.resolve(cli_backend, cwd=cwd, env=env, binary=binary)
        try:
            argv = routing.headless_argv(
                cli_backend,
                binary,
                prompt=prompt,
                cwd=cwd,
                skip_permissions=skip_permissions,
                dispatch_settings_path=settings_path,
                model=request.get("model"),
                sandbox_posture=sandbox_posture,
            )
        except ValueError as exc:
            refused: dict[str, Any] = {
                "ok": False,
                "error": str(exc),
                "spawned": False,
                "pid": None,
            }
            if sandbox_posture is not None:
                refused["harness_sandbox"] = sandbox_posture.as_dict()
            return refused
        if _argv0_unavailable(argv, cwd=cwd):
            return {
                "ok": False,
                "error": f"binary_not_found:{binary}",
                "spawned": False,
                "pid": None,
            }

        try:
            log_dir = claude_sandbox.dispatch_runs_dir()
            log_path = log_dir / f"{run_id}.log"
        except OSError as exc:
            # Log-directory setup runs after binary preflight; its failures are
            # spawn setup errors, not a missing CLI binary.
            return {
                "ok": False,
                "error": f"spawn_failed:{exc}",
                "spawned": False,
                "pid": None,
            }

        try:
            from .supervise import spawn_via_detached_supervisor

            pid = spawn_via_detached_supervisor(
                argv,
                cwd=cwd,
                env=env,
                log_path=log_path,
                run_id=run_id,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "error": f"binary_not_found:{binary}",
                "spawned": False,
                "pid": None,
            }
        except RuntimeError as exc:
            return {
                "ok": False,
                "error": f"spawn_failed:{exc}",
                "spawned": False,
                "pid": None,
            }
        except OSError as exc:
            return {
                "ok": False,
                "error": f"spawn_failed:{exc}",
                "spawned": False,
                "pid": None,
            }
        out = {
            "ok": True,
            "stub": False,
            "spawned": True,
            "pid": pid,
            "log_path": str(log_path),
            "run_id": run_id,
            "agent_label": request.get("agent_label"),
            "cli_backend": cli_backend,
        }
        if settings_path:
            out["claude_settings_path"] = settings_path
        if sidecar_path:
            out["dispatch_sidecar_path"] = sidecar_path
        return out


def _persist_spawn_pid(run_id: str, pid: int) -> bool:
    """Record the child pid before the coordinator marks the run launched.

    A crash between Popen and state='launched' otherwise leaves a live child
    behind a reclaimable claimed lease, and the next claim starts a second
    process. Reclaim treats a live recorded pid as a handoff.

    Task #602: the token is ``pid:<n>:<start>`` where the process start time is
    readable, so a recycled pid is not mistaken for our agent. It degrades to
    the legacy ``pid:<n>`` when it is not; rows in that form are no worse off
    than before, they just fall back to liveness alone.

    Task #1221 finding 2: the write is additionally guarded on
    ``termination_outcome IS NULL``. ``coordinator.cancel_run`` sets
    ``termination_outcome='requested'`` before it reads evidence_ref to
    terminate — the same guard on both sides makes this a compare-and-swap:
    whichever of "record this pid" or "record cancellation intent" commits
    first wins, and the loser is responsible for actually stopping the real
    process. Returns False when cancel_run won the race — the caller (this
    module's ``RoutedLaunchBoundary.launch``) must then kill the pid itself,
    since cancel_run's termination attempt ran against stale/absent evidence
    and has no way to see this spawn.
    """
    from .supervise import pid_token

    conn = storage.connect()
    try:
        cur = conn.execute(
            "UPDATE dispatch_runs SET evidence_ref=?, updated_at=? WHERE run_id=? "
            "AND state IN ('claimed','launched','running') AND termination_outcome IS NULL",
            (pid_token(pid), storage.now(), run_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _persist_spawn_pid_after_cancel(run_id: str, pid: int) -> None:
    """Keep a late child auditable after cancellation won the CAS race.

    ``_persist_spawn_pid`` intentionally refuses a row whose cancellation
    intent is already durable. The child still needs an evidence token after
    the launch boundary kills it, otherwise the cancellation record says
    nothing about the process that crossed the boundary.
    """
    from .supervise import pid_token

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET evidence_ref=?, updated_at=? WHERE run_id=? "
            "AND evidence_ref IS NULL AND termination_outcome IS NOT NULL "
            "AND state IN ('claimed','launched','running','cancelled')",
            (pid_token(pid), storage.now(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _load_task_row(task_id: int) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}
    finally:
        conn.close()


def _repo_scope(task: dict[str, Any] | None, workspace: Path) -> dict[str, str | None]:
    worktree = (task or {}).get("worktree_path") or None
    branch = (task or {}).get("branch") or None
    project = (task or {}).get("project") or None
    if worktree:
        try:
            wt = Path(worktree).expanduser().resolve()
            ws = workspace.expanduser().resolve()
            # Worktree may live under ~/dev/<repo>/.worktrees/ — still record it.
            return {
                "workspace": str(ws),
                "worktree_path": str(wt),
                "branch": branch,
                "project": project,
                "repo_scope": str(wt),
            }
        except OSError:
            return {
                "workspace": str(workspace),
                "worktree_path": str(worktree),
                "branch": branch,
                "project": project,
                "repo_scope": str(worktree),
            }
    return {
        "workspace": str(workspace),
        "worktree_path": None,
        "branch": branch,
        "project": project,
        "repo_scope": str(workspace),
    }


def live_run_count(exclude_run_id: str | None = None) -> int:
    """How many dispatch runs still reserve a process/task slot.

    ``exclude_run_id`` leaves out the run that is *asking*. ``claim_and_dispatch``
    inserts a run as ``claimed`` before it invokes the launch boundary, so a
    boundary that counts every live slot also counts the run it is launching:
    with ``UA_DISPATCH_MAX_LIVE_RUNS=1`` that refuses every launch, and with any
    other ceiling it silently caps one run lower than configured. Excluding the
    asking run preserves task #1221's intent — another agent's unlaunched claim
    still reserves its slot.
    """
    # The predicate is an unparenthesized OR chain, so the exclusion must be
    # wrapped: appending `AND run_id != ?` without the parentheses binds to only
    # its last clause (AND binds tighter than OR) and silently excludes nothing.
    sql = (
        "SELECT COUNT(*) AS n FROM dispatch_runs "
        f"WHERE ({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE})"
    )
    params: tuple[str, ...] = ()
    if exclude_run_id:
        sql += " AND run_id != ?"
        params = (exclude_run_id,)
    conn = storage.connect()
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


def default_spawner() -> ProcessSpawner:
    """The spawner a scheduler tick gets, decided by config at call time.

    ``CliProcessSpawner`` only when ``UA_DISPATCH_REAL_SPAWN=1``; otherwise the
    stub, which is the shipped default. Resolved per instantiation rather than
    at import so flipping the flag and restarting the hub is sufficient, and so
    tests can monkeypatch the env without reloading the module.
    """
    if config.dispatch_real_spawn():
        return CliProcessSpawner()
    return StubProcessSpawner()


@dataclass
class DevicesOnlyLaunchBoundary:
    """Fallback for a devices-only hub (task #2109): never launch locally.

    Reached when no ua-node is registered for the agent label. The run goes to
    ``retryable`` with this reason, which is the "waiting for a device" state,
    rather than starting the agent on the hub.
    """

    def launch(
        self,
        *,
        run_id: str,
        task_id: int,
        agent_label: str,
        delegation_id: int | None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error": f"no_device_for_agent:{agent_label}",
            "detail": (
                f"no ua-node device is registered for {agent_label!r}, and this hub "
                f"runs with {execution_locality.ENV}=devices, so it will not run the "
                "agent itself. Start `unified-agent node` on a device bound to that "
                "agent, then dispatch again."
            ),
            "spawned": False,
            "run_id": run_id,
        }


def devices_launch_boundary() -> LaunchBoundary:
    """Node routing with no local fallback: the only boundary a devices-only hub uses."""
    return NodeRoutedLaunchBoundary(fallback=DevicesOnlyLaunchBoundary())


def ui_launch_boundary() -> LaunchBoundary:
    """Control Center dispatch: the operator's click starts the agent's CLI.

    On a devices-only hub the same click routes to the agent's ua-node device.
    """
    if execution_locality.devices_only():
        return devices_launch_boundary()
    return RoutedLaunchBoundary(spawner=CliProcessSpawner())


def scheduler_launch_boundary() -> LaunchBoundary:
    """Launch boundary for scheduler ticks — stub unless real spawn is opted in.

    ``UA_DISPATCH_NODE_ROUTING=1`` wraps whatever the spawn flag picked in a
    ``NodeRoutedLaunchBoundary``: labels bound to a registered ua-node launch
    onto that device, everything else flows through unchanged.

    A devices-only hub (task #2109) never launches locally. With real spawn
    opted in, it routes to a node with no local fallback. Without the opt-in it
    keeps the stub, so an unattended scheduler tick starts no work anywhere
    until ``UA_DISPATCH_REAL_SPAWN=1`` is set.
    """
    if execution_locality.devices_only():
        if config.dispatch_real_spawn():
            return devices_launch_boundary()
        return RoutedLaunchBoundary(spawner=StubProcessSpawner())
    if config.dispatch_real_spawn():
        inner: LaunchBoundary = RoutedLaunchBoundary(spawner=CliProcessSpawner())
    else:
        inner = RoutedLaunchBoundary(spawner=StubProcessSpawner())
    if config.dispatch_node_routing():
        return NodeRoutedLaunchBoundary(fallback=inner)
    return inner


def _resolve_route_or_error(
    agent_label: str,
) -> tuple[Any, Any, dict[str, Any] | None]:
    """Resolve the dispatch route and check the label identity (fail closed).

    Returns ``(route, resolved, None)`` on success or ``(None, resolved,
    error_dict)`` — shared by RoutedLaunchBoundary and NodeRoutedLaunchBoundary.
    """
    resolved = routing.resolve_named_agent(agent_label)
    if not resolved.ok or resolved.route is None:
        return (
            None,
            resolved,
            {
                "ok": False,
                "error": resolved.reason,
                "resolve": resolved.as_dict(),
            },
        )
    route = resolved.route
    # Execution identity must match the delegated label exactly — aliases
    # may resolve TO this label, but the claim carries the canonical form.
    if route.agent_label != agent_label:
        return (
            None,
            resolved,
            {
                "ok": False,
                "error": "identity_mismatch",
                "resolve": resolved.as_dict(),
                "expected": agent_label,
            },
        )
    return route, resolved, None


def _run_model_for(route: Any, task_spec: str, task_id: int | None = None) -> str | None:
    """Resolve the dispatch run model.

    Notes override > operating-profile model (task #1756; same provider only)
    > label config > backend default.
    """
    model_override = config.parse_model_override(task_spec)
    backend_fallback = None
    if route.cli_backend == "codex":
        from ..executor.backends import DEFAULT_CODEX_MODEL

        backend_fallback = os.environ.get("UA_CODEX_MODEL") or DEFAULT_CODEX_MODEL
    elif route.cli_backend == "agy":
        backend_fallback = os.environ.get("UA_AGY_MODEL") or None
    if model_override is None and task_id is not None:
        from .. import operating_profiles

        model_override, _reason = operating_profiles.profile_model(
            operating_profiles.settings_for_launch(task_id, route.agent_label),
            agent_label=route.agent_label,
            backend=route.cli_backend,
            backend_fallback=backend_fallback,
        )
    return config.resolve_run_model(
        route.agent_label,
        override=model_override,
        backend_fallback=backend_fallback,
    )


@dataclass
class RoutedLaunchBoundary:
    """Resolve named-agent route + workspace, then spawn via ``ProcessSpawner``.

    The default spawner is chosen by ``default_spawner()`` — the stub unless
    ``UA_DISPATCH_REAL_SPAWN=1`` (task #599). Control Center dispatch keeps
    injecting ``CliProcessSpawner`` explicitly, so an operator click starts a
    CLI regardless of the flag: a human at the keyboard is a different
    authorization from an unattended tick, and the flag only governs the
    latter.

    Real spawn additionally refuses once ``dispatch_max_live_runs()`` runs are
    already live, so no scheduler bug or approval burst can fork an unbounded
    number of agent processes. The stub is exempt from the ceiling — it starts
    nothing, and capping it would silently change existing tick behaviour.

    Fail-closed on unknown/ambiguous/malformed labels, non-dispatchable roster
    identities, denied workspace bindings, and optional workspace mismatches.
    Argv is the persona's native headless CLI (claude -p, codex exec, agy -p,
    cursor agent -p, hermes --oneshot) — never a shared Pi/API alias.
    Never puts secrets into the launch request or argv.
    """

    spawner: ProcessSpawner = field(default_factory=default_spawner)
    expected_workspace: Path | str | None = None
    last_request: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def launch(
        self,
        *,
        run_id: str,
        task_id: int,
        agent_label: str,
        delegation_id: int | None,
    ) -> dict[str, Any]:
        if not isinstance(self.spawner, StubProcessSpawner):
            refused = _native_guard(agent_label, delegation_id)
            if refused is not None:
                return refused
        route, resolved, error = _resolve_route_or_error(agent_label)
        if error is not None:
            return error

        ws = routing.resolve_workspace_for_route(route, expected_workspace=self.expected_workspace)
        if not ws.ok or ws.workspace is None:
            return {
                "ok": False,
                "error": ws.reason,
                "resolve": resolved.as_dict(),
                "workspace": ws.as_dict(),
            }

        task = _load_task_row(task_id)
        if task is None:
            return {
                "ok": False,
                "error": "unknown_task",
                "resolve": resolved.as_dict(),
                "workspace": ws.as_dict(),
            }
        scope = _repo_scope(task, ws.workspace)
        task_spec = routing.bound_task_spec((task or {}).get("notes"))
        run_model = _run_model_for(route, task_spec, task_id)
        try:
            argv = routing.build_launch_argv(route, run_id=run_id, task_id=task_id, model=run_model)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc)[:500],
                "resolve": resolved.as_dict(),
                "workspace": ws.as_dict(),
            }

        request = {
            "run_id": run_id,
            "task_id": task_id,
            "delegation_id": delegation_id,
            "agent_label": route.agent_label,
            "execution_identity": route.agent_label,
            "profile": route.profile,
            "cli_backend": route.cli_backend,
            "binary": route.binary,
            "argv": argv,
            "cwd": scope["repo_scope"] or str(ws.workspace),
            "workspace": scope["workspace"],
            "worktree_path": scope["worktree_path"],
            "branch": scope["branch"],
            "project": scope["project"],
            "repo_scope": scope["repo_scope"],
            "task_spec": task_spec,
            "model": run_model,
            # Explicitly absent: tokens, API keys, bearer headers, provider bodies.
            "env": {
                "UA_DISPATCH_RUN_ID": run_id,
                "UA_DISPATCH_TASK_ID": str(task_id),
                "UA_DISPATCH_AGENT": route.agent_label,
                "UA_DISPATCH_PROFILE": route.profile,
                "UA_AGENT_LABEL": route.agent_label,
            },
        }
        self.last_request = request
        # Concurrency ceiling, checked against live DB state rather than an
        # in-process counter: ticks, Control Center clicks, and the CLI are
        # separate processes, so anything held in memory would undercount.
        # Only real spawns are capped — the stub starts nothing, and capping it
        # would silently change existing tick behaviour.
        if not isinstance(self.spawner, StubProcessSpawner):
            # Exclude this run: it is already `claimed` in the table, and
            # counting it here would refuse the launch it is asking for.
            live = live_run_count(run_id)
            ceiling = config.dispatch_max_live_runs()
            if live >= ceiling:
                return {
                    "ok": False,
                    "error": f"max_live_runs_reached:{live}/{ceiling}",
                    "spawned": False,
                    "pid": None,
                    "run_id": run_id,
                    "agent_label": route.agent_label,
                }
        spawned = self.spawner.spawn(request)
        pid = spawned.get("pid")
        if spawned.get("ok", True) and pid is not None:
            recorded = _persist_spawn_pid(run_id, int(pid))
            if not recorded:
                # `_persist_spawn_pid`'s guarded write lost its compare-and-swap
                # against `termination_outcome`. Confirm a cancellation is
                # actually why before treating this as finding 2's race — some
                # other, unrelated state change losing the same guard should
                # never trigger self-termination of a legitimate spawn.
                from .coordinator import get_run as _reread_run

                current = _reread_run(run_id)
                if current is not None and current.get("termination_outcome") is not None:
                    # Task #1221 finding 2: a cancellation was requested for
                    # this run before the spawn could be recorded as evidence.
                    # cancel_run's termination attempt ran against
                    # stale/absent evidence and cannot see this pid —
                    # terminate it directly. No identity check is needed: this
                    # is the pid the spawner just handed back, not one read
                    # from a stored token.
                    from .coordinator import finalize_cancellation
                    from .supervise import classify_termination_outcome, pid_token, terminate

                    _persist_spawn_pid_after_cancel(run_id, int(pid))
                    outcome = terminate(int(pid))
                    cessation = classify_termination_outcome(outcome)
                    run_row = dict(current)
                    run_row["evidence_ref"] = run_row.get("evidence_ref") or pid_token(int(pid))
                    finalize = finalize_cancellation(
                        run_row,
                        actor="dispatch-launch",
                        reason="cancelled_during_launch",
                        termination=outcome,
                        cessation=cessation,
                        allow_downgrade_confirmed_cessation=True,
                    )
                    return {
                        "ok": False,
                        "error": "cancelled_during_launch",
                        "spawned": True,
                        "pid": pid,
                        "termination": outcome,
                        "cessation": cessation,
                        "finalize": finalize,
                        "request": request,
                        "spawn": spawned,
                    }
        if not spawned.get("ok", True):
            return {
                "ok": False,
                "error": str(spawned.get("error") or "launch_failed"),
                "request": request,
                "spawn": spawned,
            }
        return {
            "ok": True,
            "stub": bool(spawned.get("stub", False)),
            "spawned": bool(spawned.get("spawned", False)),
            "pid": spawned.get("pid"),
            "run_id": run_id,
            "task_id": task_id,
            "agent_label": route.agent_label,
            "delegation_id": delegation_id,
            "route": route.as_dict(),
            "workspace": ws.as_dict(),
            "request": request,
            "spawn": spawned,
        }


def _persist_node_evidence(run_id: str, evidence_ref: str) -> bool:
    """Record the node queue item as a run's evidence before it goes live.

    Same compare-and-swap contract as ``_persist_spawn_pid``: the write is
    guarded on ``termination_outcome IS NULL``, so a cancellation committed
    between the enqueue and this write wins, and the caller owns cleanup of
    the orphaned queue item.
    """
    conn = storage.connect()
    try:
        cur = conn.execute(
            "UPDATE dispatch_runs SET evidence_ref=?, updated_at=? WHERE run_id=? "
            "AND termination_outcome IS NULL",
            (evidence_ref, storage.now(), run_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


@dataclass
class NodeRoutedLaunchBoundary:
    """Launch a governed dispatch run on a registered ua-node (task #1327, I1).

    Wraps the ordinary scheduler boundary. For an agent label with no
    registered node, every call defers to ``fallback`` byte-for-byte. For a
    label WITH a node the boundary fails closed — offline nodes and missing
    backends produce a launch error (claim_and_dispatch turns it into the
    ordinary retryable/launch_failed path) rather than a silent local spawn.

    A routed launch enqueues one ``node_dispatch`` item linked to the run; the
    node's claim/heartbeat/receipt then drive the run lifecycle (lease
    renewal, running state, outcome) through ``node_queue``.
    """

    fallback: LaunchBoundary

    def launch(
        self,
        *,
        run_id: str,
        task_id: int,
        agent_label: str,
        delegation_id: int | None,
    ) -> dict[str, Any]:
        from .. import node_queue

        route, resolved, error = _resolve_route_or_error(agent_label)
        if error is not None:
            return error

        sel = node_queue.select_node_for_agent(agent_label, route.cli_backend)
        if not sel["routed"]:
            return self.fallback.launch(
                run_id=run_id,
                task_id=task_id,
                agent_label=agent_label,
                delegation_id=delegation_id,
            )
        refused = _native_guard(agent_label, delegation_id)
        if refused is not None:
            return {**refused, "node_routed": True}
        # Same workspace policy as the local boundary (task #1327 review):
        # node routing must not bypass a denied UA_AGENT_WORKSPACES binding.
        # An explicit solitary Desktop DENY still resolves via the process
        # default (binding_source=process_default_after_deny); to route a
        # device-scoped label (e.g. Laptop_*) to its node, give it an explicit
        # UA_AGENT_WORKSPACES binding.
        ws = routing.resolve_workspace_for_route(route)
        if not ws.ok:
            return {
                "ok": False,
                "error": ws.reason,
                "resolve": resolved.as_dict(),
                "workspace": ws.as_dict(),
                "node_routed": True,
            }
        if not sel["ok"]:
            return {
                "ok": False,
                "error": sel["error"],
                "resolve": resolved.as_dict(),
                "node_routed": True,
            }

        task = _load_task_row(task_id)
        if task is None:
            return {
                "ok": False,
                "error": "unknown_task",
                "resolve": resolved.as_dict(),
                "node_routed": True,
            }
        task_spec = routing.bound_task_spec(task.get("notes"))
        prompt = task_spec if task_spec.strip() else f"Task #{task_id}: {task['title']}"
        from ..goals import append_why_chain
        from ..skills_registry import append_assigned_skills

        prompt = append_why_chain(prompt, int(task_id))
        # route.profile is this route's actual profile, not
        # profiles.default_for_identity(agent_label)'s guess (item #4, #1353
        # review): a routed launch can run under a different profile than the
        # agent's default.
        prompt = append_assigned_skills(prompt, agent_label, profile=route.profile)
        from ..operating_profiles import append_operating_brief

        # Last, so the prompt ends with the Operating profile block.
        prompt = append_operating_brief(prompt, int(task_id), agent=agent_label)
        cwd = node_queue.node_cwd_for_task(sel["facts"], task.get("worktree_path"))
        if cwd is None:
            return {
                "ok": False,
                "error": "node_no_worktree_root",
                "resolve": resolved.as_dict(),
                "node": sel["node"],
                "node_routed": True,
            }

        # Same live-run ceiling as a real spawn: a node launch starts an agent
        # process too, just on a device rather than this host. Exclude the
        # asking run: claim_and_dispatch already wrote it as `claimed`.
        live = live_run_count(run_id)
        ceiling = config.dispatch_max_live_runs()
        if live >= ceiling:
            return {
                "ok": False,
                "error": f"max_live_runs_reached:{live}/{ceiling}",
                "spawned": False,
                "pid": None,
                "run_id": run_id,
                "agent_label": route.agent_label,
                "node_routed": True,
            }

        run_model = _run_model_for(route, task_spec, task_id)
        run_max_s = config.dispatch_run_max_s()
        if 0 < run_max_s != float("inf"):
            timeout_s = min(node_queue.MAX_TIMEOUT_S, max(1, int(run_max_s)))
        else:
            timeout_s = node_queue.DEFAULT_TIMEOUT_S

        try:
            # Ungated enqueue: the scheduler only claims explicitly approved
            # ready work, so the approval for this run already happened.
            enqueued = node_queue.enqueue(
                sel["node"],
                created_by="dispatch-scheduler",
                prompt=prompt,
                cwd=cwd,
                backend=route.cli_backend,
                model=run_model,
                timeout_s=timeout_s,
                run_id=run_id,
                task_id=task_id,
            )
        except node_queue.NodeError as exc:
            return {
                "ok": False,
                "error": f"node_enqueue_failed:{exc}"[:500],
                "resolve": resolved.as_dict(),
                "node": sel["node"],
                "node_routed": True,
            }

        item_id = enqueued["item_id"]
        evidence_ref = f"node:{sel['node']}:{item_id}"
        if not _persist_node_evidence(run_id, evidence_ref):
            # Cancellation won the compare-and-swap: retire the orphaned item.
            # Do NOT report "cancelled_during_launch" — claim_and_dispatch
            # treats that error as already-finalized, and nothing finalized
            # this run. Reporting ok lets its launched-transition hit the
            # cancellation lock and surface "cancellation_in_progress".
            node_queue.cancel(item_id, actor="dispatch-launch")
        return {
            "ok": True,
            "stub": False,
            "spawned": True,
            "pid": None,
            "run_id": run_id,
            "task_id": task_id,
            "agent_label": route.agent_label,
            "delegation_id": delegation_id,
            "node": sel["node"],
            "item_id": item_id,
            "route": route.as_dict(),
            "request": {
                "node": sel["node"],
                "backend": route.cli_backend,
                "cwd": cwd,
                "model": run_model,
                "timeout_s": timeout_s,
            },
            "node_routed": True,
        }
