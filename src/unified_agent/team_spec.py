"""Team specs: declare who is on a team and how each seat starts, then boot it (task #1736).

A team spec names an existing org office and lists its seats for a run. Each
seat is an org.yaml roster label (no second identity store) with a harness
from ``acp_matrix.ACP_HARNESSES``, an optional model, the host it runs on, a
workspace (repo plus worktree policy), startup guidance and a skill set.
Edges between seats (``delegates_to``, ``reviews``, ``can_observe``,
``collaborates_with``, ``escalates_to``) are recorded; enforcing them belongs
to the comms policy (#1735). ``operating_profile`` names an operating profile
(#1732); it is validated when ``unified_agent.operating_profiles`` is
importable and configured, and recorded unvalidated otherwise.

Specs live under a top-level ``teams:`` block in the reviewed policy file, so
``config_revisions`` versions them as ``policy`` revisions: every commit is a
hash-chained revision, rollback is approval-gated, and the commit path is the
same chief-of-staff write as the rest of the policy file.

``team_up(team, plan=True)`` is a read-only dry run. It returns, per seat, the
host, harness, model, workspace, guidance and the readiness checks, with every
refusal and its reason. The plan carries no timestamps, so two plans of the
same spec against the same hub state are byte-identical and diffable.

``team_up(team, plan=False)`` boots a committed spec only. It recomputes the
plan, refuses when any seat is refused, then asks the approval gate for one
pass bound to the exact plan hash. Seats launch in edge order through the
ua-node queue behind an injectable :class:`SeatLauncher`, and each seat gets
one run record on ``agent.team.run`` linked to a team-run id.

Boundaries: no tmux, no writes to user harness config (skills are delivered by
``harness_config`` bundles, and the plan only checks that they are published),
and no auto-trust of workspaces (a seat's workspace must sit inside a root the
node itself reported).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Protocol

from mcp.server.mcpserver.exceptions import ToolError

from . import config, events, org_registry, storage
from .node_registry import CWD_REALPATH_FEATURE
from .team_policy import (  # noqa: F401 - the policy layer is re-exported here
    BLOCK,
    EDGE_TYPES,
    HARNESSES,
    MAX_POLICY_BYTES,
    TeamSpecError,
    _sha,
    launch_order,
    load_specs,
    parse_block,
    parse_spec,
    roster_findings,
    spec_sha256,
    validate_policy_text,
)

logger = logging.getLogger("unified_agent.team_spec")

TEAM_UP_TOOL = "team_up"
RUN_CHANNEL = "agent.team.run"

# ---------------------------------------------------------------------------
# Readiness checks
# ---------------------------------------------------------------------------


def _operating_profile(name: str | None) -> dict[str, Any]:
    if name is None:
        return {"name": None, "status": "none"}
    try:
        from . import operating_profiles  # type: ignore[attr-defined]
    except ImportError:
        return {
            "name": name,
            "status": "unvalidated",
            "note": "operating profiles are not installed on this hub (#1735); "
            "the name is recorded unvalidated",
        }
    try:
        cfg = operating_profiles.load_config()
    except Exception as exc:  # noqa: BLE001 - a broken profile block refuses the plan
        return {"name": name, "status": "error", "note": str(exc)[:300]}
    if not getattr(cfg, "configured", False):
        return {
            "name": name,
            "status": "unvalidated",
            "note": "no operating profiles are configured; the name is recorded unvalidated",
        }
    if name not in getattr(cfg, "profiles", {}):
        return {"name": name, "status": "unknown", "note": f"no operating profile {name!r}"}
    return {"name": name, "status": "validated"}


def _autonomy_context(spec: dict[str, Any]) -> dict[str, Any]:
    """A mission review covers this exact spec, not any later edit of it."""
    return {"team": spec["team"], "spec_sha256": spec_sha256(spec)}


def _autonomy(spec: dict[str, Any], actor: str | None) -> dict[str, Any]:
    from . import autonomy_modes

    mission = spec["mission_id"]
    if mission is None:
        try:
            ceiling = autonomy_modes.org_ceiling()
        except ToolError as exc:
            return {"mission_id": None, "decision": "deny", "reason": str(exc)[:300]}
        return {
            "mission_id": None,
            "decision": "unbound",
            "reason": f"no mission bound; org autonomy ceiling is {ceiling}. "
            "Boot still needs a one-shot approval.",
        }
    if not actor:
        return {
            "mission_id": mission,
            "decision": "deny",
            "reason": "autonomy mode needs an authenticated actor",
        }
    try:
        decided = autonomy_modes.preview_action(
            mission, TEAM_UP_TOOL, actor, action_context=_autonomy_context(spec)
        )
    except ToolError as exc:
        return {"mission_id": mission, "decision": "deny", "reason": str(exc)[:300]}
    return {
        "mission_id": mission,
        "decision": decided["decision"],
        "reason": decided["reason"],
        "attribution": decided.get("attribution"),
    }


def _bundle_skills() -> tuple[str | None, set[str]]:
    from . import harness_config

    try:
        active = harness_config.active_bundle()
    except Exception:  # noqa: BLE001 - an unreadable bundle reads as none published
        active = None
    if not active:
        return None, set()
    skills = active["bundle"].get("skills") or []
    return active["version"], {s.get("name") for s in skills if isinstance(s, dict)}


def _live(node: dict[str, Any], now: datetime) -> bool:
    """The shared node liveness rule, future-dated beats included (task #1997).

    ``node`` is a ``list_nodes`` entry; a beat from before its last
    retire/un-retire does not count (task #2196).
    """
    from .node_registry import heartbeat_is_live, node_live_beat

    return heartbeat_is_live(node_live_beat(node), now)


def _inside(path: str, root: str) -> bool:
    child, parent = PurePosixPath(path), PurePosixPath(root)
    return child == parent or parent in child.parents


def _pick_node(
    seat: dict[str, Any], nodes: dict[str, dict[str, Any]], now: datetime
) -> tuple[str | None, dict[str, Any] | None, dict[str, str] | None]:
    """The node a seat runs on, or the refusal that says why there is none."""
    label, host, backend = seat["label"], seat["host"], f"acp:{seat['harness']}"
    owned = {name: row for name, row in nodes.items() if row["agent"] == label}
    if host is not None:
        named = {
            name: row
            for name, row in nodes.items()
            if name == host or (row.get("device_node") or "") == host
        }
        if not named:
            return None, None, {"code": "unknown_host", "detail": f"no node registered on {host!r}"}
        owned = {name: row for name, row in named.items() if row["agent"] == label}
        if not owned:
            return (
                None,
                None,
                {
                    "code": "host_not_bound_to_label",
                    "detail": f"no node on {host!r} is registered to {label}; a node runs "
                    "only as the identity that registered it",
                },
            )
    if not owned:
        return None, None, {"code": "no_node_for_label", "detail": f"no node registered to {label}"}
    live = {name: row for name, row in owned.items() if _live(row, now)}
    if not live:
        names = ", ".join(sorted(owned))
        return None, None, {"code": "host_offline", "detail": f"node(s) {names} are offline"}
    advertising = sorted(name for name, row in live.items() if backend in (row["backends"] or []))
    if not advertising:
        names = ", ".join(sorted(live))
        return (
            None,
            None,
            {
                "code": "harness_unavailable_on_host",
                "detail": f"{backend} is not advertised by {names}",
            },
        )
    if seat["workspace"].get("repo") is not None:
        # A bound checkout needs a node that enforces its realpath (#1770).
        # Prefer such a node among the candidates (all on the named host when
        # one is given), so an older node sorting first cannot shadow a capable
        # one. When none qualifies, the plan still refuses it explicitly as
        # ``cwd_realpath_unsupported``.
        advertising.sort(key=lambda name: not _enforces_realpath(live[name]))
    name = advertising[0]
    return name, live[name], None


def _enforces_realpath(node: dict[str, Any]) -> bool:
    features = node.get("node_features")
    return isinstance(features, list) and CWD_REALPATH_FEATURE in features


def _workspace_cwd(
    seat: dict[str, Any], node: dict[str, Any]
) -> tuple[str | None, str | None, str | None, str | None]:
    """``(cwd, refusal_code, detail, cwd_realpath)`` for a seat's workspace on its node.

    The hub never sees the node's filesystem, so it trusts only what the node
    measured: its worktree roots and the git checkouts directly under them,
    each with its resolved path. A checkout the node did not report is
    ``workspace_unverified``; one whose real path leaves its root (a symlink)
    is refused as ``workspace``. Nothing is trusted by name alone.

    ``cwd_realpath`` is the checkout's real path as the node measured it (task
    #1770). The approval and the queued item bind it, and the node refuses to
    launch if the path has since been retargeted, even inside its roots. A bare
    worktree root (no ``workspace.repo``) carries none.
    """
    roots = list(node.get("worktree_roots") or [])
    if not roots:
        return None, "workspace_unverified", "the node reported no worktree roots", None
    repo = seat["workspace"]["repo"]
    if repo is None:
        return roots[0], None, f"{roots[0]} is a node-reported worktree root", None
    reported = list(node.get("repos") or [])
    if repo.startswith("/"):
        if not any(_inside(repo, root) for root in roots):
            return (
                None,
                "workspace",
                f"{repo} is outside the node's reported worktree roots",
                None,
            )
        wanted = [repo]
    else:
        wanted = [str(PurePosixPath(root) / repo) for root in roots]
    entry = next(
        (row for path in wanted for row in reported if row.get("path") == path),
        None,
    )
    if entry is None:
        return (
            None,
            "workspace_unverified",
            f"the node has not reported a git checkout at {' or '.join(wanted)} "
            "(nodes report checkouts directly under their worktree roots; "
            "upgrade ua-node or fix workspace.repo)",
            None,
        )
    if not _inside(entry["realpath"], entry["root_realpath"]):
        return (
            None,
            "workspace",
            f"{entry['path']} resolves to {entry['realpath']}, outside its root "
            f"{entry['root_realpath']} (symlink; no auto-trust)",
            None,
        )
    return (
        entry["path"],
        None,
        f"{entry['path']} is a node-reported checkout inside its root",
        entry["realpath"],
    )


def _count_wip(conn: Any, label: str, now_ts: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE claimed_by=? AND status='doing' "
        "AND (lock_expires_at IS NULL OR lock_expires_at > ?)",
        (label, now_ts),
    ).fetchone()
    return int(row["n"])


def _guidance(spec: dict[str, Any], seat: dict[str, Any]) -> str:
    parts = [text for text in (spec["guidance"], seat["guidance"]) if text]
    return "\n\n".join(parts)


def _edges_for(spec: dict[str, Any], label: str) -> dict[str, list[dict[str, str]]]:
    return {
        "outgoing": [
            {"type": e["type"], "to": e["to"]} for e in spec["edges"] if e["from"] == label
        ],
        "incoming": [
            {"type": e["type"], "from": e["from"]} for e in spec["edges"] if e["to"] == label
        ],
    }


def startup_prompt(spec: dict[str, Any], seat_plan: dict[str, Any]) -> str:
    """The first prompt a seat receives. It is a message, not a file write."""
    lines = [
        f"You are seat {seat_plan['label']} on team {spec['team']} "
        f"(harness {seat_plan['harness']}).",
        "Confirm your identity with profile_status, then read your seat, team and edges "
        'with coordination_manage(action="team_seat").',
    ]
    edges = seat_plan["edges"]
    for edge in edges["outgoing"]:
        lines.append(f"- you {edge['type']} {edge['to']}")
    for edge in edges["incoming"]:
        lines.append(f"- {edge['from']} {edge['type']} you")
    if seat_plan["workspace"]["worktree"] == "task":
        lines.append(
            "Claim a task before writing code and work in its .worktrees/task-<id> worktree."
        )
    else:
        lines.append("Work in the shared checkout; claim a task before writing code.")
    if seat_plan["skills"]:
        lines.append("Skills for this seat: " + ", ".join(seat_plan["skills"]) + ".")
    if spec["operating_profile"]:
        lines.append(f"Operating profile: {spec['operating_profile']}.")
    if seat_plan["guidance"]:
        lines.extend(["", seat_plan["guidance"]])
    return "\n".join(lines)


def live_checks(
    conn: Any,
    label: str,
    *,
    actor: str | None,
    now: datetime,
    out: list[str] | None = None,
) -> list[tuple[str, bool, str, str | None]]:
    """Identity, lifecycle, WIP, budget and trust checks for one seat, read now.

    The plan shows them; boot runs them again right before each enqueue, so a
    seat that became refused after the preview is skipped, not launched.
    Budget warnings are appended to ``out``.
    """
    from . import agent_lifecycle, budgets, run_records

    rows: list[tuple[str, bool, str, str | None]] = []
    if label in config.revoked_agents():
        rows.append(("identity", False, f"{label} is revoked", "revoked"))
    elif agent_lifecycle.retired_state(label, conn=conn) is not None:
        # A plan read, not a call by the retired identity: shown, not recorded.
        rows.append(("identity", False, f"{label} is retired", "retired"))
    block = agent_lifecycle.claim_block(conn, label)
    rows.append(("lifecycle", block is None, block or "active", "lifecycle" if block else None))
    cap = config.agent_wip_max_for(label)
    wip = _count_wip(conn, label, now.isoformat(timespec="seconds"))
    rows.append(("wip", wip < cap, f"{label} holds {wip} doing task(s) (cap {cap})", "wip_cap"))
    decision = budgets.evaluate_claim_or_config_error(conn, label)
    if decision.blocked:
        rows.append(("budget", False, decision.blocked, "budget"))
    else:
        rows.append(("budget", True, "within budget", None))
        if out is not None:
            out.extend(decision.warnings)
    native = run_records.native_execution_status(label, originator=actor)
    if native.get("native_execution_allowed"):
        rows.append(("trust_preset", True, "native launch allowed", None))
    else:
        rows.append(
            (
                "trust_preset",
                False,
                native.get("native_execution_denial") or "native launch refused",
                "trust_preset",
            )
        )
    return rows


def _seat_plan(
    spec: dict[str, Any],
    seat: dict[str, Any],
    *,
    conn: Any,
    nodes: dict[str, dict[str, Any]],
    bundle: tuple[str | None, set[str]],
    actor: str | None,
    now: datetime,
) -> dict[str, Any]:
    label = seat["label"]
    refusals: list[dict[str, str]] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, code: str | None = None) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok and code:
            refusals.append({"seat": label, "code": code, "detail": detail})

    node_name, node, refused = _pick_node(seat, nodes, now)
    if refused is not None:
        check("host", False, refused["detail"], refused["code"])
        cwd = cwd_realpath = None
    else:
        check("host", True, f"node {node_name} is live and advertises acp:{seat['harness']}")
        cwd, code, why, cwd_realpath = _workspace_cwd(seat, node or {})
        check("workspace", cwd is not None, why or "", code)
        if cwd_realpath is not None:
            check(
                "cwd_realpath",
                _enforces_realpath(node or {}),
                "node must advertise checkout realpath enforcement; upgrade ua-node",
                "cwd_realpath_unsupported",
            )

    for name, ok, detail, code in live_checks(conn, label, actor=actor, now=now, out=warnings):
        check(name, ok, detail, code)

    if seat["harness"] == "hermes":
        check(
            "hermes_profile",
            False,
            "acp:hermes needs a hermes_profile, which team specs do not carry yet; "
            "launch Hermes from the Sessions pane",
            "hermes_profile_unsupported",
        )

    version, published = bundle
    skills = list(dict.fromkeys([*spec["skills"], *seat["skills"]]))
    missing = [name for name in skills if name not in published]
    if missing:
        where = (
            f"harness_config bundle {version}" if version else "any active harness_config bundle"
        )
        check(
            "skills",
            False,
            f"skill(s) {', '.join(missing)} not in {where}; publish them through harness_config",
            "skill_not_published",
        )
    elif skills:
        state = (node or {}).get("harness_config") or {}
        current = state.get("bundle_version") == version
        applied = set(state.get("skills") or []) if current else set()
        delivering = list(state.get("skill_surfaces") or []) if current else []
        absent = [name for name in skills if name not in applied]
        if node is None:
            check("skills", False, "no node to deliver skills to")
        elif absent:
            check(
                "skills",
                False,
                f"skill(s) {', '.join(absent)} not applied on node {node_name} for bundle "
                f"{version}; run harness_config apply on that node",
                "skill_not_applied",
            )
        elif seat["harness"] not in delivering:
            # The node's skill state is shared by every seat on it; only the
            # surface named for a seat's harness delivers skills to that seat.
            check(
                "skills",
                False,
                f"node {node_name} delivers skills only through its "
                f"{', '.join(delivering) or 'no'} surface(s), not to harness "
                f"{seat['harness']}; rules surfaces carry no skills",
                "skill_not_applied",
            )
        else:
            check("skills", True, f"applied on node {node_name} (bundle {version})")

    plan = {
        "label": label,
        "harness": seat["harness"],
        "backend": f"acp:{seat['harness']}",
        "model": seat["model"],
        "host": seat["host"],
        "node": node_name,
        "workspace": {**seat["workspace"], "cwd": cwd, "cwd_realpath": cwd_realpath},
        "guidance": _guidance(spec, seat),
        "skills": skills,
        "edges": _edges_for(spec, label),
        "checks": checks,
        "refusals": refusals,
        "warnings": warnings,
    }
    plan["prompt_sha256"] = hashlib.sha256(startup_prompt(spec, plan).encode("utf-8")).hexdigest()
    return plan


def plan_spec(
    spec: dict[str, Any], *, actor: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Dry-run plan for one parsed spec. Reads only; never launches or records."""
    from .node_registry import list_nodes

    now = now or datetime.now(UTC)
    offices = org_registry.load_registry()
    office = next((row for row in offices if row["id"] == spec["team"]), None)
    team_refusals = [row for row in roster_findings(spec, offices) if row["seat"] == ""]
    seat_roster = [row for row in roster_findings(spec, offices) if row["seat"] != ""]
    if spec.get("starter_template"):
        from . import team_templates

        try:
            team_templates.validate_installed(spec)
        except ToolError as exc:
            team_refusals.append({"seat": "", "code": "starter_template", "detail": str(exc)})
    profile = _operating_profile(spec["operating_profile"])
    if profile["status"] in {"unknown", "error"}:
        team_refusals.append(
            {"seat": "", "code": "operating_profile", "detail": profile.get("note") or ""}
        )
    autonomy = _autonomy(spec, actor)
    if autonomy["decision"] == "deny":
        team_refusals.append({"seat": "", "code": "autonomy_deny", "detail": autonomy["reason"]})
    elif autonomy["decision"] == "pause_for_human":
        # Not a refusal: boot opens the mission review (an autonomy pending
        # action a human acknowledges), then boots once it is reviewed.
        autonomy["needs"] = "mission review at boot"
    nodes = list_nodes()
    bundle = _bundle_skills()
    by_label = {seat["label"]: seat for seat in spec["seats"]}
    conn = storage.connect()
    try:
        seats = [
            _seat_plan(
                spec,
                by_label[label],
                conn=conn,
                nodes=nodes,
                bundle=bundle,
                actor=actor,
                now=now,
            )
            for label in spec["launch_order"]
        ]
    finally:
        conn.close()
    for seat in seats:
        seat["refusals"] = [
            *[row for row in seat_roster if row["seat"] == seat["label"]],
            *seat["refusals"],
        ]
    refusals = [*team_refusals, *(row for seat in seats for row in seat["refusals"])]
    plan = {
        "team": spec["team"],
        "office": None
        if office is None
        else {"id": office["id"], "name": office["name"], "lead": office["lead"]},
        "description": spec["description"],
        "spec_sha256": spec_sha256(spec),
        "operating_profile": profile,
        "autonomy": autonomy,
        "harness_bundle": bundle[0],
        "launch_order": list(spec["launch_order"]),
        "edges": spec["edges"],
        "seats": seats,
        "refusals": refusals,
        "warnings": [w for seat in seats for w in seat["warnings"]],
        "ready": not refusals,
    }
    plan["plan_sha256"] = _sha(plan)
    plan["plan_text"] = render_plan_text(plan)
    return plan


def render_plan_text(plan: dict[str, Any]) -> str:
    """A stable, line-oriented rendering an operator can diff."""
    lines = [
        f"team {plan['team']}  spec {plan['spec_sha256'][:12]}  "
        f"{'READY' if plan['ready'] else 'REFUSED'}",
        f"operating_profile {plan['operating_profile'].get('name') or '-'} "
        f"({plan['operating_profile']['status']})",
        f"autonomy {plan['autonomy']['decision']}: {plan['autonomy']['reason']}",
        f"launch order: {' -> '.join(plan['launch_order'])}",
    ]
    for seat in plan["seats"]:
        cwd = seat["workspace"]["cwd"]
        realpath = seat["workspace"].get("cwd_realpath")
        # The approval binds the checkout's real path (#1770), so the operator
        # must see it: two bindings with one path string differ only here.
        shown = f"{cwd} -> {realpath}" if realpath and realpath != cwd else (cwd or "-")
        lines.append(
            f"seat {seat['label']}  {seat['backend']}  model={seat['model'] or 'default'}  "
            f"node={seat['node'] or '-'}  cwd={shown}  "
            f"worktree={seat['workspace']['worktree']}"
        )
        guidance = hashlib.sha256(seat["guidance"].encode("utf-8")).hexdigest()
        lines.append(
            f"  binds prompt={seat['prompt_sha256'][:16]}  guidance={guidance[:16]}  "
            f"skills={','.join(seat['skills']) or '-'}"
        )
        if realpath:
            lines.append(f"  binds cwd_realpath={realpath}")
        elif cwd:
            lines.append("  binds cwd_realpath=- (bare worktree root, not bound)")
        else:
            lines.append("  binds cwd_realpath=- (no workspace resolved)")
        for text in seat["guidance"].splitlines():
            lines.append(f"  | {text}")
        for row in seat["checks"]:
            lines.append(f"  [{'ok' if row['ok'] else 'NO'}] {row['name']}: {row['detail']}")
    for edge in plan["edges"]:
        lines.append(f"edge {edge['from']} {edge['type']} {edge['to']}")
    for row in plan["refusals"]:
        lines.append(f"refused {row['seat'] or 'team'} {row['code']}: {row['detail']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


class SeatLauncher(Protocol):
    """Starts one seat; returns at least ``{"item_id": ..., "node": ...}``."""

    def launch(
        self, seat: dict[str, Any], *, prompt: str, team_run_id: str, actor: str
    ) -> dict[str, Any]: ...


class NodeSeatLauncher:
    """Queue the seat on its ua-node as an ACP session (the #1329 launch path)."""

    def launch(
        self, seat: dict[str, Any], *, prompt: str, team_run_id: str, actor: str
    ) -> dict[str, Any]:
        from . import node_queue

        if seat["harness"] == "hermes":
            raise ToolError("acp:hermes seats need a hermes_profile; team specs do not carry one")
        # Ungated enqueue: the team-level approval already bound this exact
        # node, backend, cwd and prompt hash. Trust presets still run inside.
        return node_queue.enqueue(
            seat["node"],
            created_by=actor,
            prompt=prompt,
            cwd=seat["workspace"]["cwd"],
            cwd_realpath=seat["workspace"].get("cwd_realpath"),
            backend=seat["backend"],
            model=seat["model"],
        )


_launcher_factory: Callable[[], SeatLauncher] = NodeSeatLauncher


def set_launcher_factory(factory: Callable[[], SeatLauncher] | None) -> None:
    """Swap the launcher (tests use a double). ``None`` restores the node launcher."""
    global _launcher_factory
    _launcher_factory = factory or NodeSeatLauncher


def _record_run(kind: str, payload: dict[str, Any], actor: str, key: str) -> str | None:
    ulid, _inserted = events.record_event_once(
        RUN_CHANNEL, payload={"kind": kind, **payload}, originator=actor, dedupe_key=key
    )
    return ulid


def _approval_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "team": plan["team"],
        "spec_sha256": plan["spec_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "seats": [
            {
                "label": seat["label"],
                "node": seat["node"],
                "backend": seat["backend"],
                "model": seat["model"],
                "cwd": seat["workspace"]["cwd"],
                "cwd_realpath": seat["workspace"].get("cwd_realpath"),
                "prompt_sha256": seat["prompt_sha256"],
            }
            for seat in plan["seats"]
        ],
    }


def _safe_record(
    kind: str, payload: dict[str, Any], actor: str, key: str
) -> tuple[str | None, str | None]:
    """Record a run event; a ledger failure is reported, never left to strand a boot."""
    try:
        return _record_run(kind, payload, actor, key), None
    except Exception as exc:  # noqa: BLE001 - the caller reports it per seat
        logger.warning("team run record %s failed: %s", key, exc)
        return None, str(exc)[:300]


def _restore(approval_id: int | None) -> str | None:
    """Give back an unspent approval pass. Returns an error note if that fails."""
    if approval_id is None:
        return None
    from .tools.approvals import restore_consumed_approval

    try:
        restore_consumed_approval(approval_id)
        return None
    except Exception as exc:  # noqa: BLE001 - reported to the operator
        logger.warning("could not restore team_up approval %s: %s", approval_id, exc)
        return str(exc)[:300]


def boot(
    spec: dict[str, Any],
    *,
    actor: str,
    expected_plan_sha256: str | None = None,
    launcher: SeatLauncher | None = None,
) -> dict[str, Any]:
    """Launch every seat of a committed spec in edge order, behind one approval.

    Order of gates: plan (refused plans stop here, before any approval), the
    mission's autonomy mode (a pause opens a mission review and returns; when
    ``team_up`` is globally gated it also opens the mission-bound approval
    request), the one-shot ``team_up`` approval, then per seat a fresh
    readiness check right before its enqueue. A seat refused at launch, whose
    recheck failed, or whose delegate did not launch, is skipped and reported;
    later independent seats still start. The mission review is spent at the
    first launch attempt, so a boot that launches nothing can be retried.
    """
    from . import autonomy_modes
    from .tools.approvals import check_approval_gate

    if actor in config.revoked_agents():
        raise ToolError(f"{actor!r} is revoked and cannot boot a team")
    from . import agent_lifecycle

    agent_lifecycle.refuse_if_retired(actor, site="team_spec.team_up")
    plan = plan_spec(spec, actor=actor)
    if expected_plan_sha256 and expected_plan_sha256 != plan["plan_sha256"]:
        return {
            "booted": False,
            "stale_plan": True,
            "reason": "the plan changed since it was previewed; review the new plan",
            "plan": plan,
        }
    if not plan["ready"]:
        return {"booted": False, "reason": "the plan has refusals", "plan": plan}

    pending_id = None
    if spec["mission_id"] is not None:
        decided = autonomy_modes.evaluate(
            spec["mission_id"], TEAM_UP_TOOL, actor, action_context=_autonomy_context(spec)
        )
        if decided["decision"] == "pause_for_human":
            review: dict[str, Any] = {
                "mission_id": spec["mission_id"],
                "pending_id": decided.get("pending_id"),
                "reason": decided["reason"],
            }
            steps = f"a human reviews pending autonomy action {decided.get('pending_id')}"
            if TEAM_UP_TOOL in config.approval_gated_tools():
                # The mission gate reads a global pass bound to the mission
                # action, and the review cannot be acknowledged without it.
                # Open that request now (it spends nothing) so the pause can
                # be resolved instead of repeating forever.
                gate = check_approval_gate(
                    TEAM_UP_TOOL,
                    autonomy_modes.action_approval_payload(
                        spec["mission_id"], TEAM_UP_TOOL, actor, _autonomy_context(spec)
                    ),
                    requested_by=actor,
                    force=True,
                    request_only=True,
                )
                review["global_approval_id"] = gate.get("approval_id")
                review["global_approval_status"] = gate.get("status")
                if gate.get("status") != "approved":
                    steps = (
                        f"a human approves {TEAM_UP_TOOL} request {gate.get('approval_id')}, "
                        f"then {steps}"
                    )
            return {
                "booted": False,
                "autonomy_review": review,
                "reason": f"mission review required: {steps}, then boot again",
                "plan": plan,
            }
        if decided["decision"] != "proceed":
            return {
                "booted": False,
                "reason": f"autonomy {decided['decision']}: {decided['reason']}",
                "plan": plan,
            }
        pending_id = decided.get("pending_id")

    gate = check_approval_gate(
        TEAM_UP_TOOL, _approval_payload(plan), requested_by=actor, force=True
    )
    approval_id = int(gate["approval_id"]) if gate.get("approval_id") is not None else None
    team_run_id = f"tr-{secrets.token_hex(8)}"
    started_ref, started_error = _safe_record(
        "team_run_started",
        {
            "team_run_id": team_run_id,
            "team": spec["team"],
            "spec_sha256": plan["spec_sha256"],
            "plan_sha256": plan["plan_sha256"],
            "launch_order": plan["launch_order"],
            "operating_profile": plan["operating_profile"],
            "approval_id": approval_id,
            "actor": actor,
        },
        actor,
        f"team-run:{team_run_id}",
    )
    if started_error is not None:
        # Nothing launched: give the pass back so the operator can retry.
        restore_error = _restore(approval_id)
        failed: dict[str, Any] = {
            "booted": False,
            "team_run_id": team_run_id,
            "reason": f"could not record the team run: {started_error}",
            "approval_restored": restore_error is None,
            "approval_id": approval_id,
            "seats": [],
        }
        if restore_error is not None:
            failed["approval_restore_error"] = restore_error
        return failed
    seat_launcher = launcher or _launcher_factory()
    outcomes: list[dict[str, Any]] = []
    launched: set[str] = set()
    waits_on = {
        seat["label"]: {
            e["to"]
            for e in spec["edges"]
            if e["type"] == "delegates_to" and e["from"] == seat["label"]
        }
        for seat in plan["seats"]
    }
    mission_started = False

    def attempt(seat: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        """One seat's recheck and launch; every failure is that seat's outcome."""
        nonlocal mission_started
        label = seat["label"]
        missing = sorted(waits_on[label] - launched)
        if missing:
            # Edge order: a seat never starts before the seats it delegates to.
            return {
                **base,
                "outcome": "not_started",
                "reason": f"delegate(s) {', '.join(missing)} did not launch",
            }
        try:
            conn = storage.connect()
            try:
                rechecked = live_checks(conn, label, actor=actor, now=datetime.now(UTC))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - a transient read must not strand the boot
            return {
                **base,
                "outcome": "failed",
                "reason": f"readiness recheck failed: {exc}"[:300],
            }
        refused = [row for row in rechecked if not row[1] and row[3]]
        if refused:
            return {
                **base,
                "outcome": "refused",
                "reason": "; ".join(f"{row[3]}: {row[2]}" for row in refused),
            }
        try:
            if pending_id is not None and not mission_started:
                # The mission review is spent at the first real launch attempt,
                # not before: a boot that never reaches an enqueue leaves it
                # reusable. Once spent it stays spent, since an enqueue may
                # have happened.
                autonomy_modes.mark_started(
                    pending_id, actor, action_context=_autonomy_context(spec)
                )
                mission_started = True
            result = seat_launcher.launch(
                seat,
                prompt=startup_prompt(spec, seat),
                team_run_id=team_run_id,
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 - each seat reports its own outcome
            return {**base, "outcome": "failed", "reason": str(exc)[:300]}
        launched.add(label)
        return {
            **base,
            "outcome": "launched",
            "item_id": result.get("item_id"),
            "status": result.get("status"),
        }

    try:
        for seat in plan["seats"]:
            label = seat["label"]
            base = {
                "team_run_id": team_run_id,
                "team": spec["team"],
                "label": label,
                "node": seat["node"],
                "backend": seat["backend"],
                "model": seat["model"],
                "cwd": seat["workspace"]["cwd"],
            }
            outcome = attempt(seat, base)
            ref, error = _safe_record(
                f"seat_{outcome['outcome']}", outcome, actor, f"team-run:{team_run_id}:{label}"
            )
            outcome["run_ref"] = ref
            if error is not None:
                outcome["record_error"] = error
            outcomes.append(outcome)
    except BaseException:
        # Anything unforeseen: if no seat was enqueued the pass was not spent.
        if not launched:
            _restore(approval_id)
        raise
    result: dict[str, Any] = {
        "booted": bool(outcomes) and all(o["outcome"] == "launched" for o in outcomes),
        "team_run_id": team_run_id,
        "team_run_ref": started_ref,
        "approval_id": approval_id,
        "plan_sha256": plan["plan_sha256"],
        "seats": outcomes,
    }
    if not launched:
        # Nothing started, so the pass was not spent on a launch.
        restore_error = _restore(approval_id)
        result["approval_restored"] = restore_error is None and approval_id is not None
        if restore_error is not None:
            result["approval_restore_error"] = restore_error
    return result


# ---------------------------------------------------------------------------
# Public entry points (coordination_manage actions and HTTP)
# ---------------------------------------------------------------------------


def _actor(explicit: str | None = None) -> str | None:
    verified = config.verified_agent_identity()
    if verified:
        return verified
    return explicit.strip() if explicit and explicit.strip() else None


def resolve(team: str | None = None, spec: dict | None = None) -> dict[str, Any]:
    if spec is not None:
        if team is not None and not isinstance(team, str):
            raise TeamSpecError("team must be a string")
        if not isinstance(spec, dict):
            raise TeamSpecError("spec must be an object")
        team_id = team or spec.get("team")
        body = {key: value for key, value in spec.items() if key != "team"}
        return parse_spec(team_id, body)
    if not team:
        raise TeamSpecError("pass team (a committed spec id) or spec (an inline draft)")
    specs = load_specs()
    if team not in specs:
        raise TeamSpecError(f"no committed team spec {team!r}; known: {sorted(specs) or 'none'}")
    return specs[team]


def team_up(
    team: str | None = None,
    spec: dict | None = None,
    plan: bool = True,
    expected_plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Preview (``plan=True``, default) or boot (``plan=False``) a team.

    A plan may use an inline draft ``spec``; a boot only takes a committed
    ``team`` id, so what launches is always a reviewed, revisioned spec.
    Boot needs a one-shot approval of the exact plan (tool ``team_up``).
    """
    actor = _actor()
    if plan:
        return plan_spec(resolve(team, spec), actor=actor)
    if spec is not None:
        raise TeamSpecError(
            "boot runs a committed spec only; commit it with team_spec_commit, then pass team"
        )
    if not actor:
        raise ToolError("booting a team requires an authenticated identity")
    return boot(resolve(team), actor=actor, expected_plan_sha256=expected_plan_sha256)


def team_list() -> dict[str, Any]:
    """Committed specs with their seats, harnesses and edge counts."""
    specs = load_specs()
    return {
        "teams": [
            {
                "team": spec["team"],
                "description": spec["description"],
                "operating_profile": spec["operating_profile"],
                "spec_sha256": spec_sha256(spec),
                "seats": [
                    {"label": s["label"], "harness": s["harness"], "model": s["model"]}
                    for s in spec["seats"]
                ],
                "edges": len(spec["edges"]),
                "launch_order": spec["launch_order"],
            }
            for spec in sorted(specs.values(), key=lambda row: row["team"])
        ],
        "source": "policy file teams block (revisioned as config kind 'policy')",
    }


def seats_for(label: str) -> list[dict[str, Any]]:
    """Every committed seat held by ``label``, with its team and edges."""
    out: list[dict[str, Any]] = []
    for spec in sorted(load_specs().values(), key=lambda row: row["team"]):
        for seat in spec["seats"]:
            if seat["label"] != label:
                continue
            out.append(
                {
                    "team": spec["team"],
                    "harness": seat["harness"],
                    "model": seat["model"],
                    "host": seat["host"],
                    "workspace": seat["workspace"],
                    "skills": list(dict.fromkeys([*spec["skills"], *seat["skills"]])),
                    "operating_profile": spec["operating_profile"],
                    "teammates": [s["label"] for s in spec["seats"] if s["label"] != label],
                    "edges": _edges_for(spec, label),
                    "latest_run": _latest_run(label, spec["team"]),
                }
            )
    return out


def _latest_run(label: str, team: str) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT ulid, payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.label')=? AND json_extract(payload, '$.team')=? "
            "ORDER BY ROWID DESC LIMIT 1",
            (RUN_CHANNEL, label, team),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    return {
        "team_run_id": payload.get("team_run_id"),
        "outcome": payload.get("outcome"),
        "item_id": payload.get("item_id"),
        "run_ref": row["ulid"],
    }


def team_seat(label: str | None = None) -> dict[str, Any]:
    """The caller's own seat(s), team(s) and incoming/outgoing edges."""
    verified = config.verified_agent_identity()
    if verified and label and label != verified:
        raise ToolError("team_seat reads only your own seat")
    who = verified or (label.strip() if label else None)
    if not who:
        raise ToolError("team_seat needs an authenticated identity (or label without auth)")
    return {"agent": who, "seats": seats_for(who)}


def team_spec_commit(team: str, spec: dict) -> dict[str, Any]:
    """Write one spec into the policy file's ``teams`` block as a policy revision.

    Same authority as ``config_revision_manage``: a chief-of-staff write that
    records a hash-chained revision. Remove a team by committing the policy
    document without it.
    """
    from . import config_revisions, profiles

    if not profiles.tool_is_allowed("config_revision_manage"):
        raise ToolError("committing a team spec requires config_revision_manage authority")
    parsed = resolve(team, spec)
    body = {key: value for key, value in spec.items() if key != "team"}
    # Hold the revision lock across the read and the commit so a concurrent
    # policy commit is not overwritten by this read-modify-write.
    with config_revisions._lock:
        mapping = config_revisions._base_policy_mapping()
        teams = dict(mapping.get(BLOCK) or {})
        teams[parsed["team"]] = body
        mapping[BLOCK] = teams
        try:
            result = config_revisions.commit("policy", mapping)
        except config_revisions.ConfigFanoutError as exc:
            result = exc.result
    return {**result, "team": parsed["team"], "spec_sha256": spec_sha256(parsed)}
