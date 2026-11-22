"""Capability-matching check for agent delegation targets (task #116).

Given a task and a candidate agent, can that agent's tool allowlist actually
cover what the task needs? The audit in the 04:12 design doc's mesh review was
a static, manually-maintained table — this makes the same check queryable.
Ported from the held `feat/task-62-mesh-coordination` branch (never merged;
task #107's disposition left it "kept as reference"), with the Hermes
fallback list refreshed to match its current live `tools.include`.
"""

import os
import re
import sys
from pathlib import Path

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from .. import config, storage

HERMES_CONFIG_DEFAULT = "~/.hermes/config.yaml"

# Extracts required tools from task notes: [requires: tool1, tool2] or
# [requires-tools: tool1, tool2].
_REQUIRES_RE = re.compile(r"\[requires(?:-tools)?:\s*([^\]\n]+)\]", re.IGNORECASE)

# Fallback if ~/.hermes/config.yaml can't be read — kept in sync with its
# tools.include by hand; the live config is always the source of truth.
# Current as of task #116 (post-#114's narrowing: no git_commit_staged,
# no git_merge_task, no run_cmd, no code-authoring tools).
STATIC_HERMES_TOOLS = {
    "memory_save",
    "memory_search",
    "memory_get",
    "memory_list",
    "task_add",
    "task_update",
    "task_list",
    "vault_read",
    "vault_write",
    "vault_list",
    "vault_search",
    "vault_links",
    "vault_journal",
    "web_search",
    "web_fetch",
    "notify",
    "scan_for_reviewable_tasks",
    "review_and_notify",
    "fs_read",
    "fs_list",
    "fs_search",
    "fs_list_symbols",
    "fs_search_symbols",
    "fs_index_symbols",
    "plan_create",
    "plan_update",
    "plan_get",
    "plan_list",
    "plan_checkpoint",
    "plan_resume",
    "plan_step_retry",
    "plan_step_claim",
    "plan_handoff",
    "plan_delete",
    "git_diff_staged",
    "delegate",
    "delegate_list",
    "workflow_run",
    "workflow_list",
}


def _extract_required_tools(notes: str | None) -> set[str]:
    """Parse notes for '[requires: t1, t2]' annotations and return unique tool names."""
    if not notes:
        return set()
    reqs = set()
    for m in _REQUIRES_RE.finditer(notes):
        for item in m.group(1).split(","):
            name = item.strip()
            if name:
                reqs.add(name)
    return reqs


def get_agent_allowed_tools(agent_label: str) -> set[str] | None:
    """Resolve the set of allowed tools for an agent label.

    Returns None for unrestricted agents (Claude, Antigravity, the operator) —
    only Hermes carries an explicit tools.include allowlist today.
    """
    label = agent_label.strip()
    if config.agent_family(label) == "Hermes" or "hermes" in label.lower():
        raw = os.environ.get("UA_HERMES_CONFIG", HERMES_CONFIG_DEFAULT)
        config_path = Path(raw).expanduser()
        if config_path.exists():
            try:
                with open(config_path) as f:
                    cfg = yaml.safe_load(f)
                unified_agent_cfg = cfg.get("mcp_servers", {}).get("unified-agent", {})
                include_list = unified_agent_cfg.get("tools", {}).get("include")
                if include_list:
                    return set(include_list)
            except Exception:
                pass
        return set(STATIC_HERMES_TOOLS)
    return None


def check_agent_capability(task_id: int, agent_label: str) -> dict:
    """Verify whether a candidate agent's tool allowlist covers a task's needs.

    Parses the task's notes for '[requires: tool1, tool2]' annotations,
    resolves the candidate agent's allowlist (live from its config when
    readable, else a maintained static fallback), and reports the gap. A task
    with no '[requires: ...]' annotation is trivially capable by anyone — this
    only flags a real, stated mismatch, it does not infer requirements from
    task content.

    Returns {task_id, agent, capable, required_tools, allowed_tools, missing_tools}.
    `allowed_tools` is None for unrestricted agents (capable is always True).
    """
    agent_label = agent_label.strip()
    if not config.is_known_agent_label(agent_label):
        raise ToolError(config.unknown_agent_label_error("agent_label", agent_label))

    conn = storage.connect()
    try:
        row = conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ToolError(f"no task with id {task_id}")
        notes = row["notes"]
    finally:
        conn.close()

    required = _extract_required_tools(notes)
    allowed = get_agent_allowed_tools(agent_label)

    if allowed is None:
        capable = True
        missing = set()
    else:
        missing = required - allowed
        capable = not missing

    return {
        "task_id": task_id,
        "agent": agent_label,
        "capable": capable,
        "required_tools": sorted(required),
        "allowed_tools": sorted(allowed) if allowed is not None else None,
        "missing_tools": sorted(missing),
    }


_TOOL_CONFIG_PREREQS = {
    "gemini_ask": (
        "GEMINI_API_KEY",
        "Set GEMINI_API_KEY in the hub environment; do not install a package.",
    ),
}


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("unified-agent")
    except Exception:
        return "unknown"


def _repo_skills() -> list[dict[str, str]]:
    """Product skills shipped in this checkout, not the host's ~/.claude/skills tree."""
    root = Path(__file__).resolve().parents[3] / ".claude" / "skills"
    if not root.is_dir():
        return []
    out: list[dict[str, str]] = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        name = skill_md.parent.name
        out.append({"name": name, "path": str(skill_md.relative_to(root.parent.parent))})
        if len(out) >= 100:
            break
    return out


def _health_snapshot() -> dict[str, dict[str, str]]:
    """Config-gated health only — no systemd probes, no installs."""
    vault = config.vault_root()
    gated = config.approval_gated_tools()
    provider = config.search_provider()
    return {
        "hub_auth": {
            "status": "LIVE" if config.enforce_auth() else "DORMANT",
            "gate": "UA_ENFORCE_AUTH",
        },
        "approval_queue": {
            "status": "LIVE" if gated else "DORMANT",
            "gate": "UA_APPROVAL_TOOLS",
        },
        "vault": {
            "status": "LIVE" if vault else "DORMANT",
            "gate": "UA_VAULT_PATH",
        },
        "search_provider": {
            "status": "LIVE",
            "gate": f"UA_SEARCH_PROVIDER={provider}",
        },
    }


def _config_prerequisites(required_tools: list[str]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for tool in required_tools:
        spec = _TOOL_CONFIG_PREREQS.get(tool)
        if spec is None:
            continue
        env_name, hint = spec
        if not os.environ.get(env_name, "").strip():
            missing.append(
                {
                    "tool": tool,
                    "gate": env_name,
                    "action": "configure",
                    "detail": hint,
                }
            )
    return missing


def capability_catalog(
    task_id: int | None = None,
    agent_label: str | None = None,
) -> dict:
    """Read-only inventory of agents, CLIs, skills, MCP tools, versions, health, and boundaries.

    Emulates a provider/skill catalog without a mailbox or installer (task #510).
    Missing prerequisites for a task/agent are reported before dispatch.
    Installation and config changes stay explicit and approval-gated — this tool
    never installs, never writes, and never opens an approval pass.
    """
    from .. import mcp_matrix, registry, tool_surface
    from .workflow import workflow_list

    registered = tool_surface.registry()

    try:
        agents = registry.list_agents(include_configured=True)
    except Exception:
        agents = [
            {"label": label, "status": "unknown", "known": True}
            for label in sorted(config.agent_labels())
        ]

    try:
        workspace = str(config.workspace_root())
    except Exception:
        workspace = "denied"

    mcp_tools = sorted(registered)
    clis = sorted(config.shell_allowlist())
    skills = _repo_skills()
    workflows = workflow_list().get("workflows", [])
    health = _health_snapshot()
    gated = sorted(config.approval_gated_tools())

    prerequisites: dict | None = None
    if task_id is not None or agent_label:
        if task_id is None or not agent_label:
            raise ToolError("pass both task_id and agent_label to check dispatch prerequisites")
        match = check_agent_capability(task_id, agent_label)
        config_missing = _config_prerequisites(match["required_tools"])
        prerequisites = {
            **match,
            "missing_config": config_missing,
            "ready": match["capable"] and not config_missing,
        }

    return {
        "read_only": True,
        "install_is_explicit": True,
        "approval_gated": True,
        "versions": {
            "unified_agent": _package_version(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        },
        "agents": [
            {
                "label": a.get("label"),
                "status": a.get("status"),
                "kind": a.get("kind"),
                "host": a.get("host"),
                "known": a.get("known"),
            }
            for a in agents
        ],
        "mcp_tools": mcp_tools,
        "mcp_client_matrix": mcp_matrix.get_client_matrix(),
        "clis": clis,
        "skills": skills,
        "workflows": [
            {"name": w.get("name"), "description": w.get("description")} for w in workflows
        ],
        "health": health,
        "boundaries": {
            "workspace": workspace,
            "vault_write_dirs": config.vault_write_dirs(),
            "approval_gated_tools": gated,
            "shell_allowlist": clis,
            "audit_roots": [str(p) for p in config.audit_roots()],
        },
        "prerequisites": prerequisites,
    }
