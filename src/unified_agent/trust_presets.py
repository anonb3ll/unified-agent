"""Reviewed, tightening-only boundaries for mediated tool calls.

These presets never grant profile visibility or filesystem roots. Native
runtimes cannot yet enforce the same boundary and are refused when bound.
Selection comes only from policy.yaml and authenticated caller context.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, storage

if TYPE_CHECKING:
    from .policy import PolicyConfig

PRESETS = ("inspection", "configured-workspace", "active-worktree")

# Each admitted read has been reviewed for mediated path resolution or a
# database-only implementation. A readOnlyHint is not confinement evidence:
# Git reads, native probes, searches that load models and unknown tools fail
# closed. Existing profile, approval and policy checks continue to apply.
_READ_TOOLS = frozenset(
    {
        "fs_read",
        "fs_list",
        "fs_search",
        "read_image",
        "vault_read",
        "vault_list",
        "vault_search",
        "vault_links",
        "vault_query",
        "task_list",
        "run_history",
        "memory_get",
        "memory_list",
        "profile_status",
        "approval_pending",
        "event_log_verify",
    }
)
_MIXED_READ_ACTIONS = {
    "goal_manage": frozenset(
        {
            "list",
            "tree",
            "get",
            "ancestry",
            "operating_profiles",
            "operating_resolve",
            "operating_sprint_list",
            "operating_sprint_get",
        }
    ),
    "task_comment": frozenset({"list", "get"}),
    "observability_manage": frozenset({"run_history"}),
    "org_proposal": frozenset({"list", "get"}),
}


@dataclass
class TrustPresetConfig:
    default: str | None = None
    agents: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return self.default is not None or bool(self.agents)


def _name(value: object) -> str:
    if not isinstance(value, str) or value not in PRESETS:
        raise ToolError(f"trust preset must be one of {PRESETS}, got {value!r}")
    return value


def parse_config(raw: object) -> TrustPresetConfig:
    """Parse the optional reviewed block; unknown fields and names fail closed."""
    if not isinstance(raw, dict) or set(raw) - {"default", "agents"}:
        raise ToolError("trust_presets must be a mapping with only default and agents")
    default = _name(raw["default"]) if "default" in raw else None
    agents = raw.get("agents", {})
    if not isinstance(agents, dict):
        raise ToolError("trust_presets.agents must be a mapping")
    bindings = {}
    for agent, preset in agents.items():
        if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", agent):
            raise ToolError("trust preset agent bindings require exact identity labels")
        if agent != config.operator_label() and not config.is_known_agent_label(agent):
            raise ToolError("trust preset binding identity is not in the configured roster")
        canonical = config.canonical_agent_label(agent)
        if canonical is not None and canonical != agent:
            raise ToolError("trust preset bindings require exact canonical identity case")
        bindings[agent] = _name(preset)
    return TrustPresetConfig(default=default, agents=bindings)


def _policy() -> PolicyConfig:
    from .policy import load_policy

    return load_policy()


def _agent(agent: str | None, *, bindings: dict[str, str] | None = None) -> str:
    if not isinstance(agent, str) or not agent or agent != agent.strip():
        raise ToolError("trust preset requires a verified agent identity")
    roster = config.agent_labels()
    known = (
        agent in roster
        if roster
        else config.canonical_agent_label(agent) == agent or agent in (bindings or {})
    )
    if not known and agent != config.operator_label():
        raise ToolError("trust preset requires a known verified identity")
    return agent


def _select(cfg: TrustPresetConfig, agent: str | None) -> str | None:
    if not cfg.configured:
        return None
    return cfg.agents.get(_agent(agent, bindings=cfg.agents), cfg.default)


def selection(agent: str | None = None) -> str | None:
    """Explicit agents are trusted internal recording targets, never payload fields."""
    return _select(
        _policy().trust_presets, agent if agent is not None else config.verified_agent_identity()
    )


def is_read_only_call(tool: str, payload: dict[str, Any] | None = None) -> bool:
    """Internal classification used by HTTP policy entry points; ignores body hints."""
    values = payload or {}
    if tool in _MIXED_READ_ACTIONS:
        action = values.get("action")
        return isinstance(action, str) and action in _MIXED_READ_ACTIONS[tool]
    return tool in _READ_TOOLS


def check_call(
    tool: str, *, read_only: bool, payload: dict | None = None, _config: PolicyConfig | None = None
) -> None:
    """Deny-only admission; authenticated caller cannot select a body-supplied preset."""
    cfg = _config if _config is not None else _policy()
    preset = _select(cfg.trust_presets, config.verified_agent_identity())
    if preset is None:
        return
    values = payload or {}
    if read_only and is_read_only_call(tool, values):
        return
    if tool == "dispatch_report_self":
        # The implementation independently binds run id, agent and terminal
        # state; allowing admission grants no coordinator mutation privileges.
        return
    if tool == "fs_write" and preset != "inspection":
        return
    raise ToolError(f"trust preset {preset!r} denies tool {tool!r}")


def _active_root(agent: str) -> Path:
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT worktree_path, lock_expires_at, current_run_id FROM tasks "
                "WHERE claimed_by=? AND status='doing'",
                (agent,),
            ).fetchall()
        finally:
            conn.close()
        active = []
        for row in rows:
            expiry = row["lock_expires_at"]
            if not expiry:
                raise ToolError("active-worktree preset cannot verify a claimed task lease")
            deadline = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                raise ValueError("claim expiry must include a timezone")
            if deadline > datetime.now(UTC):
                active.append(row)
        if len(active) != 1:
            raise ToolError("active-worktree preset requires exactly one live claim")
        row = active[0]
        if not row["current_run_id"] or not row["worktree_path"]:
            raise ToolError("active-worktree preset requires a bound run and worktree")
        candidate = Path(row["worktree_path"]).expanduser()
        root = (
            candidate if candidate.is_absolute() else config.workspace_root().resolve() / candidate
        ).resolve(strict=True)
        if not root.is_dir():
            raise ToolError("active-worktree preset worktree is not a directory")
        return root
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("active-worktree preset could not verify its live claim") from exc


def check_path(resolved: Path, *, for_write: bool = False) -> None:
    """Intersect a path already admitted by an existing resolver with the preset."""
    cfg = _policy()
    agent = config.verified_agent_identity()
    preset = _select(cfg.trust_presets, agent)
    if preset is None:
        return
    try:
        path = resolved.resolve()
        if for_write:
            protected = config.policy_file_path().resolve()
            if path == protected or (
                path.exists() and protected.exists() and path.samefile(protected)
            ):
                raise ToolError("trust preset cannot write its active policy file")
            if preset == "inspection":
                raise ToolError("inspection trust preset denies filesystem writes")
        root = (
            _active_root(agent or "")
            if preset == "active-worktree"
            else config.workspace_root().resolve()
        )
        if not path.is_relative_to(root):
            raise ToolError(f"path escapes the {preset!r} trust preset boundary")
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("trust preset could not verify filesystem containment") from exc


def _fingerprint(cfg: PolicyConfig) -> str:
    return hashlib.sha256(
        json.dumps(asdict(cfg), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def snapshot(agent: str | None = None) -> dict | None:
    """Evidence records the reviewed binding, not an unsupported sandbox claim."""
    cfg = _policy()
    identity = agent if agent is not None else config.verified_agent_identity()
    preset = _select(cfg.trust_presets, identity)
    if preset is None:
        return None
    return {
        "preset": preset,
        "policy_fingerprint": _fingerprint(cfg),
        "enforcement": {"mediated_tools": True, "native_runtime": False},
        "agent": identity,
    }


@dataclass(frozen=True)
class _NativeExecutionAttribution:
    target: str
    originator: str | None


_native_execution_attribution: ContextVar[_NativeExecutionAttribution | None] = ContextVar(
    "trust_preset_native_execution_attribution", default=None
)


@contextmanager
def _native_execution_scope(agent: str, *, originator: str | None) -> Iterator[None]:
    """Carry trusted daemon attribution across the unchanged Backend protocol.

    Internal orchestration only: a present originator is the stored delegation
    actor, never a request-supplied grant. An absent delegator requires a verified
    caller executing as itself. This does not impersonate that caller or waive
    any preset; concrete backends recheck current bindings. The context applies
    only to this exact target and lasts only for backend.run.
    """
    cfg = _policy().trust_presets
    if cfg.configured:
        agent = _agent(agent, bindings=cfg.agents)
        if originator is not None:
            originator = _agent(originator, bindings=cfg.agents)
        elif _agent(config.verified_agent_identity(), bindings=cfg.agents) != agent:
            raise ToolError("trust preset requires verified self-target native attribution")
    require_native_allowed(agent, originator=originator)
    # Preserve stored attribution even before presets are configured: a policy
    # may first appear between daemon admission and the concrete backend check.
    token = _native_execution_attribution.set(_NativeExecutionAttribution(agent, originator))
    try:
        yield
    finally:
        _native_execution_attribution.reset(token)


def require_native_allowed(agent: str | None = None, *, originator: str | None = None) -> None:
    """Refuse launches requiring containment no generic native provider can enforce."""
    cfg = _policy().trust_presets
    if not cfg.configured:
        return
    caller = config.verified_agent_identity()
    scope = _native_execution_attribution.get()
    if originator is None and scope is not None and agent == scope.target:
        originator = scope.originator
    if caller is None and originator is None:
        raise ToolError("trust preset requires a verified native launch originator")
    # The executor caller and originating delegator are separate authorities.
    # Neither may launder a restricted launch through an unbound target.
    identities = {identity for identity in (caller, originator, agent) if identity is not None}
    for identity in identities:
        if _select(cfg, identity) is not None:
            raise ToolError(
                "trust preset requires native containment; no supported native provider is available"
            )


def describe_configuration() -> dict:
    """Non-secret doctor status; malformed reviewed policy raises ToolError."""
    cfg = _policy()
    return {
        "configured": cfg.trust_presets.configured,
        "presets": list(PRESETS),
        "default": cfg.trust_presets.default,
        "agents": dict(cfg.trust_presets.agents),
        "mediated_enforced": cfg.trust_presets.configured,
        "native_supported": False,
        "policy_fingerprint": _fingerprint(cfg),
    }
