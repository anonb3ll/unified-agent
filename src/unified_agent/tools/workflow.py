"""Chained workflows from repo-reviewed YAML definitions (task #62 · B).

Workflow definitions live IN THE REPO (`src/unified_agent/workflows/*.yaml`) —
the operator's 2026-07-17 decision: they are executable, so they get the same
treatment as code (PR review, CI, red-tier gating). There is no vault or
env-configurable definition path; changing a workflow means landing a diff.

Execution model:
- steps run in listed order with EXPLICIT data passing: `{{ inputs.<name> }}`
  and `{{ steps.<step>.<field> }}` placeholders are substituted into step args;
  an unresolvable placeholder fails the run (nothing is passed implicitly);
- fail-fast with bounded per-step retries (`retries:` per step, capped);
- steps can only call tools in _TOOL_REGISTRY — a curated subset of the
  existing MCP tools. Workflows can NEVER shell out directly: shell access
  goes through `run_cmd`, so the allowlist, sandbox, and approval gate all
  still apply. `workflow_run` itself and the approval tools are deliberately
  not callable from a workflow (no recursion, no self-approval).
- a step parked by the approval queue (ApprovalPendingError) stops the run
  immediately without burning retries; re-run the workflow after approval.
- a step tool is invoked through `server.REGISTERED_TOOLS` (task #1218), the
  same guarded+instrumented callable the MCP surface calls — never a raw
  import of the tool function — so `agent_access.enforce`,
  `operational_profiles.tool_is_allowed` and `safety.check_policy_gate` run
  for every step. An admitted HTTP workflow carries only the selected
  repo-defined step's route authority; MCP workflows retain the caller's profile
  ceiling. A step denied by
  policy or access control stops the run immediately (status 'denied'),
  without burning retries against a deterministic refusal.
- `author` is treated like any other claimed-identity field elsewhere in the
  codebase (`config.verify_write_identity`): the authenticated caller
  (`agent_identity_var`) is authoritative, an omitted `author` inherits it,
  and a mismatched `author` is rejected/overridden rather than honored — a
  caller cannot attribute a workflow's steps to a different agent than the
  one it authenticated as.
"""

import re
from pathlib import Path

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from .. import gateway, profiles, storage, workflow_roles
from ..agent_access import AgentAccessDenied
from ..config import agent_identity_var, verify_write_identity
from ..policy import PolicyViolation
from . import approvals
from .approvals import ApprovalPendingError, check_approval_gate

# Tools that call check_approval_gate themselves; the workflow engine must not
# gate them a second time (the payloads would differ and double-queue).
_SELF_GATED = {"run_cmd", "fs_write", "delegate"}

_MAX_STEP_RETRIES = 3
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


def _workflows_dir() -> Path:
    """Package-relative workflow definition directory (repo-reviewed; no override)."""
    return Path(__file__).resolve().parent.parent / "workflows"


def _tool_registry() -> dict:
    """Tool name -> callable: the curated allowlist of tools a workflow step may name.

    Excluded on purpose: workflow_run (recursion), approval_* (a workflow must
    not resolve its own gate), destructive deletes and repo-mutating git tools
    (memory_delete, task_delete, plan_delete, git_commit_staged, git_merge_task,
    git_worktree_*) — run those deliberately, not from a definition file.
    Resolved lazily to avoid import cycles.

    This dict's callables are used only for *name validation*
    (`_load_definition`) and for the engine's own internal bookkeeping calls
    (the vault_journal step-start/step-complete markers). Actual step
    invocation goes through `_governed_invoke`/`server.REGISTERED_TOOLS`
    instead (task #1218) so the access/profile/policy wrappers around each
    tool actually run — see module docstring.
    """
    from .. import monitor
    from . import audit as audit_tools
    from . import (
        dash,
        delegate,
        export,
        files,
        lint,
        memory,
        plan,
        shell,
        tasks,
        vault,
        web,
    )
    from .notify import notify

    fns = [
        web.web_search,
        web.web_fetch,
        dash.dash_list,
        dash.dash_search,
        dash.dash_view,
        files.fs_read,
        files.fs_write,
        files.fs_list,
        files.fs_search,
        shell.run_cmd,
        shell.run_cmd_check,
        audit_tools.security_audit,
        audit_tools.audit_triage_tasks,
        lint.python_lint_and_format,
        memory.memory_save,
        memory.memory_search,
        memory.memory_list,
        memory.memory_get,
        tasks.task_add,
        tasks.task_update,
        tasks.task_list,
        plan.plan_create,
        plan.plan_update,
        plan.plan_get,
        plan.plan_list,
        plan.plan_checkpoint,
        plan.plan_resume,
        plan.plan_step_retry,
        plan.plan_step_claim,
        plan.plan_handoff,
        delegate.delegate,
        delegate.delegate_list,
        vault.vault_read,
        vault.vault_write,
        vault.vault_list,
        vault.vault_search,
        vault.vault_journal,
        notify,
        # Defined above the services layer (the reviewer and the whole-hub
        # doctor); named here for validation and called through tool_surface.
        _surface_tool("review_and_notify"),
        _surface_tool("system_doctor"),
        monitor.agent_activity,
        monitor.metrics_snapshot,
        monitor.check_anomalies,
        export.export_tasks,
        export.export_memory,
    ]
    return {fn.__name__: fn for fn in fns}


def _surface_tool(name: str):
    """A curated entry for a tool that lives above this layer, called through tool_surface."""

    def call(**kwargs):
        from .. import tool_surface

        return tool_surface.registry()[name](**kwargs)

    call.__name__ = name
    return call


def _governed_invoke(tool: str, args: dict):
    """Invoke a workflow step's tool through the shared governed wrapper (task #1218).

    `server.REGISTERED_TOOLS[tool]` is the exact access/profile/policy-gated,
    instrumented callable the MCP surface calls (server.py's
    `_guarded_and_instrumented`, applied to every tool at registration) — the
    thing a raw import of the bare tool function (the old `_tool_registry()`
    invocation path) skipped entirely. Routing every step invocation through
    it means a workflow step gets `agent_access.enforce`,
    `operational_profiles.tool_is_allowed`, and `safety.check_policy_gate`
    exactly as a direct MCP call would — it is no longer a quieter path
    around them.

    Looked up through `tool_surface`, which `server` fills at start-up, so this
    module never imports the MCP adapter.
    """
    from .. import tool_surface

    fn = tool_surface.registry().get(tool)
    if fn is None:
        # Curated in _tool_registry() but not (yet) registered on this build —
        # fail closed rather than silently falling back to an ungoverned call.
        raise ToolError(
            f"tool {tool!r} is curated for workflows but not registered on this "
            "server build; refusing an ungoverned fallback call"
        )
    return fn(**args)


def _load_definition(name: str) -> dict:
    if not _NAME_RE.match(name or ""):
        raise ToolError(
            f"workflow name {name!r} is invalid (letters/digits/hyphen/underscore only)"
        )
    from .. import team_templates

    path = _workflows_dir() / f"{name}.yaml"
    installed = name.startswith(team_templates.PREFIX) and name in team_templates.workflow_names()
    if not path.is_file() and not installed:
        available = sorted(p.stem for p in _workflows_dir().glob("*.yaml"))
        raise ToolError(
            f"no workflow definition {name!r} in {_workflows_dir()}; "
            f"available: {', '.join(available) or '(none)'}"
        )
    try:
        spec = (
            team_templates.workflow_definition(name)
            if installed
            else yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except yaml.YAMLError as e:
        raise ToolError(f"workflow {name!r} is not valid YAML: {e}") from None
    if not isinstance(spec, dict):
        raise ToolError(f"workflow {name!r} must be a YAML mapping")
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ToolError(f"workflow {name!r} must define a non-empty `steps` list")
    registry = _tool_registry()
    seen = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ToolError(f"workflow {name!r} step {i} must be a mapping")
        sname = step.get("name")
        if not sname or not _NAME_RE.match(str(sname)):
            raise ToolError(f"workflow {name!r} step {i} needs a valid `name`")
        if sname in seen:
            raise ToolError(f"workflow {name!r} has duplicate step name {sname!r}")
        seen.add(sname)
        tool = step.get("tool")
        if tool not in registry:
            raise ToolError(
                f"workflow {name!r} step {sname!r} calls unknown/uncallable tool {tool!r}; "
                "workflow steps may only call the curated MCP tool registry"
            )
        if step.get("args") is not None and not isinstance(step["args"], dict):
            raise ToolError(f"workflow {name!r} step {sname!r} `args` must be a mapping")
    workflow_roles.validate(spec)
    if spec.get("roles") is not None and spec.get("name") != name:
        raise ToolError("role workflow name must match its repo definition filename")
    return spec


def _resolve_path(scope: dict, dotted: str) -> object:
    cur: object = scope
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise ToolError(
                f"cannot resolve {{{{ {dotted} }}}}: {part!r} not found — data passing "
                "is explicit; only declared inputs and prior step results are available"
            )
    return cur


def _substitute(value, scope: dict):
    """Recursively substitute {{ ... }} placeholders in step args.

    A string that is exactly one placeholder resolves to the referenced object
    (type-preserving); placeholders embedded in a longer string interpolate as
    text. Lists/dicts are walked recursively.
    """
    if isinstance(value, str):
        whole = re.fullmatch(r"\s*\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}\s*", value)
        if whole:
            return _resolve_path(scope, whole.group(1))
        return _PLACEHOLDER_RE.sub(lambda m: str(_resolve_path(scope, m.group(1))), value)
    if isinstance(value, list):
        return [_substitute(v, scope) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, scope) for k, v in value.items()}
    return value


def workflow_list() -> dict:
    """List repo-defined workflows (task #62 · B).

    Definitions are version-controlled YAML in `src/unified_agent/workflows/`
    — editing one is a code change (PR + CI), not a config tweak.
    Returns {workflows: [{name, description, steps, inputs}]}.
    """
    out = []
    wdir = _workflows_dir()
    if wdir.is_dir():
        for path in sorted(wdir.glob("*.yaml")):
            entry = {"name": path.stem, "description": None, "steps": None, "inputs": []}
            try:
                spec = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(spec, dict):
                    entry["description"] = spec.get("description")
                    steps = spec.get("steps")
                    entry["steps"] = (
                        [s.get("name") for s in steps] if isinstance(steps, list) else None
                    )
                    declared = spec.get("inputs") or []
                    entry["inputs"] = (
                        [str(name) for name in declared] if isinstance(declared, list) else []
                    )
            except Exception:
                entry["description"] = "(unparseable definition)"
            out.append(entry)
    from .. import team_templates

    for name in team_templates.workflow_names():
        entry = {"name": name, "description": None, "steps": None, "inputs": []}
        try:
            spec = _load_definition(name)
            entry.update(
                description=spec["description"],
                steps=[s["name"] for s in spec["steps"]],
                inputs=spec["inputs"],
            )
        except ToolError as exc:
            entry.update(description="(unavailable starter definition)", error=str(exc))
        out.append(entry)
    return {"workflows": out}


def workflow_run(
    name: str,
    inputs: dict | None = None,
    author: str | None = None,
    instance_id: str | None = None,
    role_bindings: dict | None = None,
    closure: dict | None = None,
) -> dict:
    """Run a repo-defined YAML workflow with explicit data passing (task #62 · B).

    Loads `src/unified_agent/workflows/<name>.yaml`, validates it, and executes
    its steps in order. Each step calls one tool from the curated registry
    through the same guarded, instrumented path the MCP server itself calls
    (`server.REGISTERED_TOOLS`) — so every safety layer (run_cmd allowlist,
    sandbox, vault write dirs, approval queue, and the access/profile/policy
    wrappers) applies unchanged; a workflow can never shell out around them
    or run a step more quietly than a direct tool call would. Args support
    `{{ inputs.<name> }}` / `{{ steps.<step>.<field> }}` substitution. Failed
    steps retry up to their `retries:` (max 3) then the run fails fast; a step
    held by the approval queue stops the run immediately (status
    'pending-approval') — re-run after approval_resolve; post-effect ledger failures
    stop as 'indeterminate' without retrying; a step denied by
    policy or access control stops the run immediately (status 'denied').
    `author` is your agent label, recorded in the activity index — it must
    match the authenticated caller (an omitted `author` inherits it; a
    mismatched one is rejected, per `config.verify_write_identity`).

    With roles, creates an ordinary task frontier and returns instance_id.
    role_bindings selects eligible seats without changing caller authority.
    Resume with instance_id; closure requires exit, reason, target where needed,
    task_id and expected_updated_at. Each bound seat claims its own task before
    advancing. The successor is projected atomically with task completion.
    See docs/role-workflows.md for closure and restart semantics.

    Returns {workflow, status, steps: [...], outputs} for linear definitions,
    or {workflow, instance_id, status, frontier, outputs, trail} for role workflows.
    """
    # The authenticated identity is authoritative (task #1218): an omitted
    # `author` inherits it, and a claimed `author` that mismatches it is
    # rejected/overridden rather than silently attributing this run's steps
    # to whoever the caller claims to be — the same rule every other
    # attributed write in this codebase follows (memory_save, task_update,
    # delegate, vault_write, plan_*).
    author = verify_write_identity(author)

    if instance_id is not None:
        return workflow_roles.run(
            name,
            None,
            inputs,
            author,
            instance_id,
            role_bindings,
            closure,
            _governed_invoke,
            _execute_role_step,
            _prepare_role_step,
        )
    spec = _load_definition(name)
    inputs = dict(inputs or {})
    declared = spec.get("inputs") or []
    if not isinstance(declared, list):
        raise ToolError(f"workflow {name!r} `inputs` must be a list of input names")
    missing = [k for k in declared if k not in inputs]
    if missing:
        raise ToolError(f"workflow {name!r} missing required inputs: {', '.join(missing)}")
    unknown = [k for k in inputs if k not in declared]
    if unknown:
        raise ToolError(
            f"workflow {name!r} got undeclared inputs: {', '.join(unknown)} — "
            "declare them in the definition's `inputs` list (data passing is explicit)"
        )

    from .. import team_templates

    if name.startswith(team_templates.PREFIX) and name in team_templates.workflow_names():
        spec = team_templates.bind_inputs(spec, inputs)

    if "task_id" in inputs:
        from ..tools.capability import check_agent_capability

        task_id = inputs["task_id"]
        cap_result = check_agent_capability(int(task_id), author)
        if not cap_result.get("capable"):
            raise ToolError(
                f"Missing tools to execute workflow for task {task_id}: {cap_result.get('missing_tools')}"
            )

    if spec.get("roles") is not None:
        return workflow_roles.run(
            name,
            spec,
            inputs,
            author,
            None,
            role_bindings,
            closure,
            _governed_invoke,
            _execute_role_step,
            _prepare_role_step,
        )
    if role_bindings is not None or closure is not None:
        raise ToolError("role_bindings and closure require a role workflow")
    return _run_linear(name, spec, inputs, author)


def _prepare_role_step(inputs, author):
    if "task_id" in inputs:
        from ..tools.capability import check_agent_capability

        try:
            result = check_agent_capability(int(inputs["task_id"]), author)
        except ToolError:
            result = {"capable": False}
        if not result.get("capable"):
            return {
                "status": "denied",
                "outputs": {},
                "steps": [],
                "error": "Missing tools to execute the workflow task",
            }
    return None


def _execute_role_step(spec, step, inputs, outputs, author):
    return _run_linear(spec["name"], {"steps": [step]}, inputs, author, outputs)


def _run_linear(name, spec, inputs, author, prior_outputs=None):
    registry = _tool_registry()
    scope: dict = {
        "inputs": inputs,
        "steps": {
            name: value if isinstance(value, dict) else {"value": value}
            for name, value in (prior_outputs or {}).items()
        },
    }
    step_reports: list[dict] = []
    status = "done"
    try:
        storage.record_agent_event(
            "workflow",
            ref_id=name,
            agent=author,
            summary=f"run started ({len(spec['steps'])} steps)",
        )
    except Exception:
        pass

    for step in spec["steps"]:
        sname, tool = step["name"], step["tool"]
        retries = min(int(step.get("retries") or 0), _MAX_STEP_RETRIES)
        report = {"name": sname, "tool": tool, "status": "failed", "attempts": 0, "error": None}
        step_reports.append(report)
        result = None
        for attempt in range(retries + 1):
            report["attempts"] = attempt + 1
            token = agent_identity_var.set(author)
            invocation = None
            try:
                args = _substitute(step.get("args") or {}, scope)

                try:
                    if author:
                        registry["vault_journal"](
                            entry=f"Workflow step {sname} ({tool}) starting",
                            agent=author,
                            tags=["workflow", "step"],
                        )
                except Exception:
                    pass

                # Bind only this step's action: its registered child may join
                # the engine receipt before execution. Later steps and retries
                # must obtain their own one-shot pass.
                with approvals.approval_invocation(tool, args, requested_by=author) as invocation:
                    if tool not in _SELF_GATED:
                        check_approval_gate(tool, args, requested_by=author)
                    # An admitted HTTP workflow carries only this validated
                    # static step's capability, never the entire curated registry.
                    with profiles.http_workflow_step_authority(tool):
                        result = _governed_invoke(tool, args)

                try:
                    if author:
                        registry["vault_journal"](
                            entry=f"Workflow step {sname} ({tool}) completed",
                            agent=author,
                            tags=["workflow", "step"],
                        )
                except Exception:
                    pass

                report["status"] = "done"
                report["error"] = None
                break
            except ApprovalPendingError as e:
                # Waiting on a human is not a failure to retry against.
                report["status"] = "pending-approval"
                report["error"] = str(e)
                status = "pending-approval"
                break
            except (PolicyViolation, AgentAccessDenied) as e:
                # A policy/access denial is deterministic — retrying the
                # identical call cannot change the outcome, so stop like the
                # approval-pending case rather than burning retries.
                report["status"] = "denied"
                report["error"] = f"{type(e).__name__}: {e}"
                status = "denied"
                break
            except gateway.LedgerWriteError as e:
                if e.rolled_back:
                    report["error"] = f"{type(e).__name__}: {e}"
                else:
                    # The body already ran; a retry could repeat a completed effect.
                    report["status"] = "indeterminate"
                    report["error"] = f"{type(e).__name__}: {e}"
                    status = "indeterminate"
                    break
            except ToolError as e:
                if approvals.failed_before_execution(invocation):
                    report["status"] = "denied"
                    report["error"] = f"{type(e).__name__}: {e}"
                    status = "denied"
                    break
                report["error"] = f"{type(e).__name__}: {e}"
            except Exception as e:  # noqa: BLE001 - each tool raises its own types
                report["error"] = f"{type(e).__name__}: {e}"
            finally:
                agent_identity_var.reset(token)
        if report["status"] == "done":
            report["result"] = result
            scope["steps"][sname] = result if isinstance(result, dict) else {"value": result}
        else:
            if status == "done":
                status = "failed"
            break  # fail fast

    outputs = {r["name"]: r.get("result") for r in step_reports if r["status"] == "done"}
    try:
        storage.record_agent_event(
            "workflow",
            ref_id=name,
            agent=author,
            summary=f"run {status} ({sum(1 for r in step_reports if r['status'] == 'done')}"
            f"/{len(spec['steps'])} steps done)",
        )
    except Exception:
        pass
    if status == "pending-approval":
        gateway.release_unprogressed_invocation()
    return {"workflow": name, "status": status, "steps": step_reports, "outputs": outputs}
