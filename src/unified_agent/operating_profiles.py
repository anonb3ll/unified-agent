"""Operating profiles: how a team, a sprint and a single run operate (task #1732).

Configuration parsing and the tables live in ``operating_policy`` (core
layer); this module resolves the layers, freezes them onto runs, enforces the
comms policy and serves the operator's writes. Task #1756 adds the readers
that wire the frozen run snapshot to delivery, autonomy, model, budget,
planning and instructions (see :func:`run_snapshot`). See
docs/operating-profiles.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage
from .operating_policy import (  # noqa: F401 - re-exported for callers and tests
    APPROVAL_TOOL,
    BLOCK,
    COMMS_MODES,
    DEGRADED_CHANNEL,
    DENIED_CHANNEL,
    ENFORCED_FIELDS,
    FIELDS,
    LIAISON,
    MAX_EXTENDS_DEPTH,
    MAX_INSTRUCTIONS,
    MAX_RELAY_CHANNELS,
    OPERATOR,
    PLANNING_LEVELS,
    SPRINT_STATUSES,
    CommsPolicyDenied,
    OperatingConfig,
    OperatingConfigError,
    Profile,
    _check_comms_complete,
    _err,
    _mapping,
    _merge,
    _name,
    ensure_schema,
    flatten,
    load_config,
    parse_config,
    parse_settings,
    snapshot_claim_order_ready,
)

logger = logging.getLogger("unified_agent.operating_profiles")


def _connect() -> sqlite3.Connection:
    conn = storage.connect()
    ensure_schema(conn)
    return conn


def _loads(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise OperatingConfigError("stored operating override is not valid JSON") from exc
    return value if isinstance(value, dict) else {}


def _sprint_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["overrides"] = _loads(out.get("overrides"))
    return out


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _has_tables(conn: sqlite3.Connection) -> bool:
    found = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('sprints', 'sprint_tasks', 'run_settings')"
    ).fetchone()[0]
    return found == 3


def _task_row(
    conn: sqlite3.Connection, task_id: int, *, fenced: bool = False
) -> sqlite3.Row | None:
    """The task row. ``fenced`` (caller-addressed reads, task #2043) makes another
    tenant's task unknown; untenanted internal callers (claim, cron, executor) read
    it unfenced and fence what hangs off it on the row's own ``org_id``."""
    sql = "SELECT id, claimed_by, project, org_id FROM tasks WHERE id=?"
    args: tuple[Any, ...] = (int(task_id),)
    if fenced:
        sql += " AND org_id IS ?"
        args = (*args, edition.tenant_org_id())
    return conn.execute(sql, args).fetchone()


def _unit_for(cfg: OperatingConfig, agent: str | None, sprint: dict[str, Any] | None) -> str | None:
    if sprint and sprint.get("org_unit"):
        return str(sprint["org_unit"])
    if not agent or not cfg.units:
        return None
    from . import org_registry

    for office in org_registry.offices_for_identity(agent):
        if office in cfg.units:
            return office
    return None


def _profile_layer(
    cfg: OperatingConfig, name: str | None, where: str, warnings: list[str] | None = None
) -> dict[str, Any]:
    """A stored layer's profile. Lenient callers pass ``warnings`` and get ``{}``
    for a profile that has since been removed from policy, instead of a claim
    or message failing on it."""
    if name is None:
        return {}
    if name not in cfg.profiles:
        if warnings is None:
            raise _err(where, f"names unknown profile {name!r}")
        warnings.append(f"{where} names profile {name!r}, which is no longer in policy; ignored")
        return {}
    return flatten(cfg, name)


def _override_layer(raw: object, where: str, warnings: list[str] | None) -> dict[str, Any]:
    try:
        if isinstance(raw, str):
            raw = _loads(raw)
        return parse_settings(raw, where)
    except OperatingConfigError as exc:
        if warnings is None:
            raise
        warnings.append(f"{where} overrides no longer parse ({exc}); ignored")
        return {}


class OperatingAuthorityDenied(ToolError):
    """A task or sprint authority is missing; callers must not fall back to defaults."""


def resolve(
    *,
    task_id: int | None = None,
    agent: str | None = None,
    sprint_id: int | None = None,
    cfg: OperatingConfig | None = None,
    conn: sqlite3.Connection | None = None,
    strict: bool = False,
    fenced: bool = False,
) -> dict[str, Any]:
    """Resolve the effective operating settings for a task, sprint or agent.

    ``fenced`` is for caller-addressed entry points: another tenant's task is
    unknown. Either way a task only takes a sprint of its own tenant, and a
    sprint addressed with no task must be the caller's (task #2043).

    ``strict`` (operator writes) raises on anything that would not resolve.
    The default lenient mode (claims, messages, reads) degrades instead: a
    removed profile or unparsable stored override is skipped, an incomplete
    comms policy stays in force and fails closed in :func:`comms_decision`,
    and each problem is listed in ``warnings``.
    """
    warnings: list[str] | None = None if strict else []
    cfg = cfg if cfg is not None else load_config()
    own = conn is None
    conn = conn if conn is not None else _connect()
    try:
        sprint = None
        run = None
        # A task's sprint shares the task row's tenant; a bare sprint_id is the caller's.
        sprint_org = edition.tenant_org_id()
        if task_id is not None:
            row = _task_row(conn, task_id, fenced=fenced)
            if row is None:
                raise OperatingAuthorityDenied(f"unknown task: {task_id}")
            agent = agent or row["claimed_by"]
            sprint_org = row["org_id"]
            if not _has_tables(conn):
                link = run_row = None
            else:
                link = conn.execute(
                    "SELECT sprint_id FROM sprint_tasks WHERE task_id=?", (int(task_id),)
                ).fetchone()
                run_row = conn.execute(
                    "SELECT profile, overrides, set_by, set_at FROM run_settings WHERE task_id=?",
                    (int(task_id),),
                ).fetchone()
            if link is not None and sprint_id is None:
                sprint_id = int(link["sprint_id"])
            if run_row is not None:
                run = dict(run_row)
        if sprint_id is not None:
            # A foreign sprint refuses like a missing one; it never reads as "no sprint".
            sprint_row = conn.execute(
                "SELECT * FROM sprints WHERE id=? AND org_id IS ?", (int(sprint_id), sprint_org)
            ).fetchone()
            sprint = dict(sprint_row) if sprint_row is not None else None
            if sprint is None:
                raise OperatingAuthorityDenied(f"unknown sprint: {sprint_id}")
    finally:
        if own:
            conn.close()

    settings: dict[str, Any] = {}
    sources: dict[str, str] = {}
    layers: list[dict[str, Any]] = []

    def apply(label: str, layer: dict[str, Any]) -> None:
        if layer:
            _merge(settings, sources, layer, label)
            layers.append({"layer": label, "settings": layer})

    if cfg.default:
        apply(f"org:{cfg.default}", flatten(cfg, cfg.default))
    unit = _unit_for(cfg, agent, sprint)
    if unit and unit in cfg.units:
        apply(f"unit:{unit}:{cfg.units[unit]}", flatten(cfg, cfg.units[unit]))
    if sprint is not None:
        where = f"sprint {sprint['id']}"
        apply(
            f"sprint:{sprint['id']}:{sprint['profile']}",
            _profile_layer(cfg, sprint["profile"], where, warnings),
        )
        apply(
            f"sprint:{sprint['id']}:overrides",
            _override_layer(sprint["overrides"], where, warnings),
        )
    if run is not None:
        where = f"run settings for task {task_id}"
        apply(
            f"run:task-{task_id}:{run['profile']}",
            _profile_layer(cfg, run["profile"], where, warnings),
        )
        apply(f"run:task-{task_id}:overrides", _override_layer(run["overrides"], where, warnings))

    comms = settings.get("comms") or {}
    if comms:
        try:
            _check_comms_complete(comms, "resolved comms")
        except OperatingConfigError as exc:
            if warnings is None:
                raise
            warnings.append(f"{exc}; the policy fails closed until it is completed")
    fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8")).hexdigest()[
        :16
    ]
    return {
        "configured": cfg.configured,
        "task_id": task_id,
        "agent": agent,
        "sprint_id": sprint["id"] if sprint else None,
        "unit": unit,
        "settings": settings,
        "sources": sources,
        "layers": layers,
        "enforcement": {
            name: ("enforced" if name in ENFORCED_FIELDS else "recorded") for name in settings
        },
        "fingerprint": fingerprint,
        "warnings": [*cfg.warnings, *(warnings or [])],
    }


def snapshot_for_claim(conn: sqlite3.Connection, task_id: int, agent: str) -> dict[str, Any] | None:
    """The frozen per-run record stamped into a run's claim event."""
    cfg = load_config()
    # Never create tables here: executescript would commit the caller's claim
    # transaction. storage._migrate creates them; a pre-migration database
    # simply has no sprint or run layer yet.
    resolved = resolve(task_id=task_id, agent=agent, cfg=cfg, conn=conn)
    if not cfg.configured and not resolved["settings"]:
        # No named profiles and no sprint or run settings: nothing to freeze.
        # Overrides alone are settings, so they are still frozen and reach
        # every consumer even with no operating_profiles block.
        return None
    return {
        "settings": resolved["settings"],
        "sources": resolved["sources"],
        "unit": resolved["unit"],
        "sprint_id": resolved["sprint_id"],
        "fingerprint": resolved["fingerprint"],
        "warnings": resolved["warnings"],
    }


# ---------------------------------------------------------------------------
# Run snapshot readers (task #1756): the consumers of the frozen posture
# ---------------------------------------------------------------------------

#: Layers an operator sets for one sprint or one run. They beat file-based
#: project config. Org and unit layers are standing defaults and sit below it.
HIGH_LAYER_PREFIXES = ("sprint:", "run:")
LOW_LAYER_PREFIXES = ("org:", "unit:")

PLANNING_MEANINGS = {
    "P0": "No planning step: act directly on the task.",
    "P1": "Light: state the approach in a sentence or two before acting.",
    "P2": "Standard: write a short plan (steps, files, tests) before changing code.",
    "P3": "Thorough: write a plan with risks and alternatives and check it with the "
    "requester before large changes.",
    "P4": "Full design review: write a design and get it reviewed before implementing.",
}

#: The model provider each single-provider CLI backend runs. Multi-provider
#: harnesses (cursor, copilot, pi, hermes) are judged by their baseline model.
BACKEND_PROVIDERS = {
    "claude": "anthropic",
    "codex": "openai",
    "agy": "google",
    "agy-interactive": "google",
}
_PROVIDER_PATTERNS = (
    ("anthropic", re.compile(r"(claude|sonnet|opus|haiku|fable)(\b|[-_.0-9])")),
    ("openai", re.compile(r"(gpt|chatgpt|codex)(\b|[-_.0-9])|o[0-9]+(\b|[-_.])")),
    ("google", re.compile(r"(gemini|gemma)(\b|[-_.0-9])")),
    ("xai", re.compile(r"grok(\b|[-_.0-9])")),
)
_PROVIDER_PREFIXES = {"anthropic", "openai", "google", "xai"}


def layer_tier(label: str | None) -> str | None:
    """``high`` for a sprint or run layer, ``low`` for an org or unit layer."""
    text = str(label or "")
    if text.startswith(HIGH_LAYER_PREFIXES):
        return "high"
    if text.startswith(LOW_LAYER_PREFIXES):
        return "low"
    return None


def run_snapshot(
    conn: sqlite3.Connection, task_id: int, *, finished: bool = False
) -> dict[str, Any] | None:
    """The run's frozen operating snapshot from the keyed store, else None.

    Reads ``run_operating_snapshots`` (task #1764), never the event log: the
    open run is the one ``tasks.current_run_id`` names, looked up by run id.
    ``finished=True`` reads the task's newest run whether or not it has
    finished: review and merge act on work a run already handed off, and that
    work is governed by the posture its run started with. A run claimed with
    no profile configured is keyed with an empty snapshot, which reads as None.
    """
    if finished:
        row = _latest_snapshot_row(conn, int(task_id))
    else:
        task = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (int(task_id),)
        ).fetchone()
        run_id = task["current_run_id"] if task is not None else None
        row = (
            _snapshot_row(
                conn,
                "SELECT run_id, agent, snapshot FROM run_operating_snapshots WHERE run_id=?",
                (run_id,),
            )
            if run_id
            else None
        )
    snap = (row or {}).get("snapshot")
    return snap if isinstance(snap, dict) and snap else None


def task_run_snapshot(task_id: int | None, *, finished: bool = False) -> dict[str, Any] | None:
    """:func:`run_snapshot` on its own connection; a read failure means no snapshot."""
    if task_id is None:
        return None
    try:
        conn = storage.connect()
        try:
            return run_snapshot(conn, int(task_id), finished=finished)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - consumers fall back to their legacy default
        logger.warning("operating snapshot unreadable for task %s: %s", task_id, exc)
        return None


def claim_snapshot(claim: dict[str, Any] | None) -> tuple[bool, dict[str, Any] | None]:
    """The frozen snapshot a hub ``task_claim`` response carried: (present, snapshot).

    ``present`` is False for a hub that predates the field; a present None
    means the hub froze no profile for the run. A remote executor must use
    this instead of reading its own store (task #1756).
    """
    if not isinstance(claim, dict) or "operating_profile" not in claim:
        return False, None
    snap = claim["operating_profile"]
    return True, snap if isinstance(snap, dict) else None


def settings_for_launch(task_id: int | None, agent: str | None) -> dict[str, Any] | None:
    """The posture a launched run works under.

    The open run's snapshot when the task is already claimed. A dispatch
    launch usually precedes the agent's claim, so otherwise the settings are
    resolved now for ``agent``: the same resolver the claim freezes moments
    later. Unconfigured or unreadable config means no settings.
    """
    snap = task_run_snapshot(task_id)
    if snap is not None or task_id is None:
        return snap
    try:
        cfg = load_config()
        resolved = resolve(task_id=int(task_id), agent=agent, cfg=cfg)
        if not cfg.configured and not resolved["settings"]:
            return None
    except OperatingAuthorityDenied:
        raise
    except Exception as exc:  # noqa: BLE001 - a bad block must not stop a launch
        logger.warning("operating settings unresolvable for task %s: %s", task_id, exc)
        return None
    return {
        "settings": resolved["settings"],
        "sources": resolved["sources"],
        "unit": resolved["unit"],
        "sprint_id": resolved["sprint_id"],
        "fingerprint": resolved["fingerprint"],
        "resolved_at": "launch",
    }


def _settings(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    settings = (snapshot or {}).get("settings")
    return settings if isinstance(settings, dict) else {}


def _sources(snapshot: dict[str, Any] | None) -> dict[str, str]:
    sources = (snapshot or {}).get("sources")
    return sources if isinstance(sources, dict) else {}


def delivery_layers(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Split the snapshot's delivery keys into ``high`` and ``low`` tiers.

    Each tier maps ``mode``/``yolo`` to its value plus ``layer`` (the label of
    the highest layer that set a key in that tier).
    """
    delivery = _settings(snapshot).get("delivery")
    out: dict[str, dict[str, Any]] = {"high": {}, "low": {}}
    if not isinstance(delivery, dict):
        return out
    sources = _sources(snapshot)
    for key in ("mode", "yolo"):
        if key not in delivery:
            continue
        label = sources.get(f"delivery.{key}")
        tier = layer_tier(label)
        if tier is None:
            continue
        out[tier][key] = delivery[key]
        out[tier]["layer"] = label
    return out


def autonomy_default(snapshot: dict[str, Any] | None) -> str | None:
    value = _settings(snapshot).get("autonomy")
    return value if isinstance(value, str) else None


def model_provider(model: str | None) -> str | None:
    """Best-effort provider family of a model id; None when it cannot be told."""
    text = str(model or "").strip().lower()
    if not text:
        return None
    if "/" in text:
        prefix, _, rest = text.partition("/")
        if prefix in _PROVIDER_PREFIXES:
            return prefix
        text = rest
    if text.startswith("cursor-"):
        text = text[len("cursor-") :]
    for provider, pattern in _PROVIDER_PATTERNS:
        if pattern.match(text):
            return provider
    return None


def profile_model(
    snapshot: dict[str, Any] | None,
    *,
    agent_label: str | None,
    backend: str | None,
    backend_fallback: str | None = None,
) -> tuple[str | None, str]:
    """``(model, reason)``: the snapshot's model when it keeps the run's provider.

    The profile may change the model, never the provider (and so never the
    backend). The provider is read from the model the run would otherwise use
    (``config.resolve_run_model`` without an override), or from the backend
    when that is unknown. A model whose provider differs, or cannot be told,
    is refused and the run keeps its normal model.
    """
    model = _settings(snapshot).get("model")
    if not isinstance(model, str) or not model:
        return None, "unset"
    baseline = config.resolve_run_model(
        agent_label, override=None, backend_fallback=backend_fallback
    )
    have = model_provider(baseline) or BACKEND_PROVIDERS.get(str(backend or ""))
    want = model_provider(model)
    if want is None or have is None or want != have:
        reason = (
            f"profile model {model!r} ({want or 'unknown provider'}) refused: the "
            f"{backend or 'unknown'} run stays on {have or 'an unknown provider'}"
        )
        logger.warning("%s (agent %s)", reason, agent_label)
        return None, reason
    return model, "applied"


def budget_caps(snapshot: dict[str, Any] | None) -> dict[str, float | int]:
    """Snapshot budget in autonomous-cycle units (``time_s``, ``tokens``, ``cost_cents``)."""
    budget = _settings(snapshot).get("budget")
    if not isinstance(budget, dict):
        return {}
    out: dict[str, float | int] = {}
    if "max_time_s" in budget:
        out["time_s"] = budget["max_time_s"]
    if "max_tokens" in budget:
        out["tokens"] = budget["max_tokens"]
    if "max_cost_usd" in budget:
        cents = round(float(budget["max_cost_usd"]) * 100, 4)
        # A non-finite cap is no cap: leave the cycle default in force.
        if math.isfinite(cents):
            out["cost_cents"] = cents
    return out


def relay_autosend(snapshot: dict[str, Any] | None, channel: str) -> bool:
    """Whether the run's profile lets a public relay auto-send on ``channel``.

    Default False. TODO(#1734): tools/public_relay.resolve_relay_autosend
    (PR #953) should call this; until it lands nothing reads it.
    """
    relay = _settings(snapshot).get("relay")
    if not isinstance(relay, dict):
        return False
    return channel in (relay.get("autosend") or ())


def brief_section(snapshot: dict[str, Any] | None) -> str | None:
    """The "Operating profile" block for a run brief: planning level and instructions."""
    settings = _settings(snapshot)
    planning = settings.get("planning")
    instructions = settings.get("instructions")
    if not planning and not (isinstance(instructions, str) and instructions.strip()):
        return None
    sources = _sources(snapshot)
    lines = ["## Operating profile"]
    if planning in PLANNING_MEANINGS:
        lines.append(f"Planning {planning}: {PLANNING_MEANINGS[planning]}")
    if isinstance(instructions, str) and instructions.strip():
        origin = sources.get("instructions")
        lines.append(f"Instructions (set by {origin}):" if origin else "Instructions:")
        lines.append(instructions.strip())
    return "\n".join(lines)


def append_operating_brief(
    prompt: str,
    task_id: int | None,
    *,
    agent: str | None = None,
    claim: dict[str, Any] | None = None,
) -> str:
    """Decorate a dispatch prompt with the run's planning level and instructions.

    ``claim`` is the hub's task-claim response: when it carries the run's
    frozen snapshot that snapshot is used as-is, with no local store read.
    """
    present, snapshot = claim_snapshot(claim)
    block = brief_section(snapshot if present else settings_for_launch(task_id, agent))
    if not block:
        return prompt
    return f"{prompt.rstrip()}\n\n{block}"


# ---------------------------------------------------------------------------
# Comms enforcement (task #1735)
# ---------------------------------------------------------------------------


def _is_operator(label: str | None) -> bool:
    return config.actor_kind_for_identity(label) == "human"


def _office_members(
    office_id: str, warnings: list[str] | None = None
) -> tuple[str | None, set[str]]:
    from . import org_registry

    try:
        office = org_registry.find_office(office_id)
    except Exception as exc:  # noqa: BLE001 - unreadable registry cannot grant office access
        if warnings is not None:
            warnings.append(f"office {office_id!r} unavailable; no liaison or members: {exc}")
        return None, set()
    if office is None:
        if warnings is not None:
            warnings.append(f"office {office_id!r} is missing; no liaison or members")
        return None, set()
    return office["lead"], set(office["members"])


def liaison_label(comms: dict[str, Any], warnings: list[str] | None = None) -> str | None:
    raw = comms.get("liaison")
    if not raw:
        return None
    if str(raw).startswith("office:"):
        lead, _ = _office_members(str(raw).split(":", 1)[1], warnings)
        return lead
    return str(raw)


def _matches(
    token: str, label: str, liaison: str | None, warnings: list[str] | None = None
) -> bool:
    if token == "*":
        return True
    if token == OPERATOR:
        return _is_operator(label)
    if token == LIAISON:
        return liaison is not None and label == liaison
    if token.startswith("office:"):
        _, members = _office_members(token.split(":", 1)[1], warnings)
        return label in members
    return label == token


def comms_decision(
    comms: dict[str, Any], sender: str, recipient: str, *, warnings: list[str] | None = None
) -> tuple[bool, str]:
    """``(allowed, reason)`` for one message under one resolved comms policy."""
    mode = comms.get("mode", "direct")
    if _is_operator(sender) or mode == "direct":
        return True, "allowed"
    liaison = liaison_label(comms, warnings)
    if mode == "funnel":
        if not _is_operator(recipient) or sender == liaison:
            return True, "allowed"
        if liaison is None:
            return False, "funnel comms has no resolvable liaison; nobody may address the operator"
        return False, (
            f"funnel comms routes everything for the operator through {liaison}; "
            f"send it to {liaison} instead"
        )
    for rule in comms.get("allow") or ():
        if _matches(rule["from"], sender, liaison, warnings) and _matches(
            rule["to"], recipient, liaison, warnings
        ):
            return True, "allowed"
    return False, f"custom comms has no allow rule from {sender} to {recipient}"


def _latest_snapshot_row(
    conn: sqlite3.Connection, task_id: int, *, agent: str | None = None
) -> dict | None:
    """The task's newest run's snapshot, by claim order rather than the wall clock.

    ``claim_ulid`` is minted on the claim's write lock, so it survives a clock
    step backward between claims; ``rowid`` breaks ties and covers rows a
    database that predates the column has not backfilled yet. ``agent``
    narrows it to that agent's own newest run (task #1775).
    """
    order = "claim_ulid DESC, rowid DESC" if snapshot_claim_order_ready(conn) else "rowid DESC"
    where, args = "task_id=?", (int(task_id),)
    if agent is not None:
        where, args = "task_id=? AND agent=?", (int(task_id), agent)
    return _snapshot_row(
        conn,
        f"SELECT run_id, agent, snapshot FROM run_operating_snapshots WHERE {where} "  # noqa: S608
        f"ORDER BY {order} LIMIT 1",
        args,
    )


def _snapshot_row(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> dict | None:
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_operating_snapshots'"
    ).fetchone()
    if present is None:
        return None
    row = conn.execute(sql, args).fetchone()
    if row is None:
        return None
    return {"run_id": row["run_id"], "agent": row["agent"], "snapshot": _loads(row["snapshot"])}


def _context(task_id: int, run_id: str | None, body: dict[str, Any]) -> dict[str, Any]:
    return {"task_id": task_id, "run_id": run_id, **body}


def _live_contexts(
    conn: sqlite3.Connection, sender: str, cfg: OperatingConfig
) -> list[dict[str, Any]]:
    """The runs the sender holds now, read by run id (no event-log scan)."""
    out = []
    for row in conn.execute(
        "SELECT id, current_run_id, current_run_agent FROM tasks "
        "WHERE claimed_by=? AND status='doing'",
        (sender,),
    ).fetchall():
        tid = int(row["id"])
        run_id = row["current_run_id"] if row["current_run_agent"] == sender else None
        snap = (
            _snapshot_row(
                conn,
                "SELECT run_id, agent, snapshot FROM run_operating_snapshots WHERE run_id=?",
                (run_id,),
            )
            if run_id
            else None
        )
        if snap is not None:
            out.append(_context(tid, run_id, snap["snapshot"]))
        elif cfg.configured:
            out.append(
                _context(tid, run_id, resolve(task_id=tid, agent=sender, cfg=cfg, conn=conn))
            )
    return out


def _tied_context(
    conn: sqlite3.Connection, sender: str, task_id: int, cfg: OperatingConfig
) -> dict[str, Any] | None:
    """The policy the sender's own run of ``task_id`` ran under, open or finished."""
    # The sender's own newest run of the task (task #1775): another agent's
    # later claim never retires the policy this sender ran the task under.
    snap = _latest_snapshot_row(conn, int(task_id), agent=sender)
    if snap is not None:
        return _context(int(task_id), snap["run_id"], snap["snapshot"])
    # Unfenced: only the sender's own claim counts, and a tied policy only narrows.
    row = _task_row(conn, task_id)
    if row is None or row["claimed_by"] != sender or not cfg.configured:
        return None
    return _context(int(task_id), None, resolve(task_id=task_id, agent=sender, cfg=cfg, conn=conn))


def _contexts(
    conn: sqlite3.Connection, sender: str, task_id: int | None, cfg: OperatingConfig
) -> list[dict[str, Any]]:
    """Every policy a message must satisfy (task #1764: a task id only narrows).

    Every run the sender holds now judges every message; with no live run,
    the sender's org and office profiles do. A ``task_id`` of the sender's
    own run (open or finished) adds that run's frozen policy on top, so tying
    a message to an old, more permissive task can never widen what the
    sender may do. A task the sender never ran adds nothing. Frozen
    snapshots apply even after the operating_profiles block is removed.
    """
    out = _live_contexts(conn, sender, cfg)
    if not out and cfg.configured:
        out.append(_context(None, None, resolve(agent=sender, cfg=cfg, conn=conn)))
    if task_id is not None:
        tied = _tied_context(conn, sender, int(task_id), cfg)
        already = (
            tied is not None
            and tied.get("run_id")
            and any(c.get("run_id") == tied["run_id"] for c in out)
        )
        if tied is not None and not already:
            out.append(tied)
    return out


def _report_degraded(sender: str, contexts: list[dict[str, Any]]) -> None:
    for ctx in contexts:
        warnings = ctx.get("warnings") or []
        if not warnings:
            continue
        warning_id = hashlib.sha256(json.dumps(warnings, sort_keys=True).encode()).hexdigest()[:16]
        events.record_event(
            channel=DEGRADED_CHANNEL,
            payload={
                "agent": sender,
                "task_id": ctx.get("task_id"),
                "run_id": ctx.get("run_id"),
                "warnings": warnings[:10],
            },
            originator=sender,
            notify=False,
            dedupe_key=(
                f"operating-degraded:{sender}:{ctx.get('task_id')}:{warning_id}:"
                f"{ctx.get('fingerprint')}:{storage.now()[:10]}"
            ),
        )


def check_comms(
    sender: str | None,
    recipient: str,
    *,
    task_id: int | None = None,
    route: str = "message",
) -> None:
    """Refuse an agent's message or notification that its run's comms policy forbids.

    See :func:`_contexts` for which policies judge a message; all must allow
    it. Nothing configured and no frozen run, nothing refused. Denials and
    degraded (stale) configuration are written to the evidence ledger.
    """
    if not sender or _is_operator(sender):
        return
    cfg = load_config()
    conn = _connect()
    try:
        contexts = _contexts(conn, sender, task_id, cfg)
    finally:
        conn.close()
    decisions = []
    for ctx in contexts:
        comms = (ctx.get("settings") or {}).get("comms") or {}
        allowed, reason = comms_decision(
            comms, sender, recipient, warnings=ctx.setdefault("warnings", [])
        )
        decisions.append((ctx, comms, allowed, reason))
    # Complete every warning set before reporting it once, even when an
    # earlier run denies. The first denial still determines the error.
    _report_degraded(sender, contexts)
    for ctx, comms, allowed, reason in decisions:
        if allowed:
            continue
        source = (ctx.get("sources") or {}).get("comms.mode", "default")
        events.record_event(
            channel=DENIED_CHANNEL,
            payload={
                "route": route,
                "from_agent": sender,
                "to_agent": recipient,
                "task_id": ctx.get("task_id"),
                "run_id": ctx.get("run_id"),
                "mode": comms.get("mode"),
                "set_by_layer": source,
                "reason": reason,
            },
            originator=sender,
            notify=False,
        )
        where = f"task {ctx['task_id']}" if ctx.get("task_id") else "your org/office profile"
        raise CommsPolicyDenied(f"comms policy for {where} (set by {source}) refuses: {reason}")


# Hub-internal deliveries with no agent behind them (task #1775): the digest
# flush (each entry was judged when queued), anomaly alerts, the review
# pipeline and queue hygiene. Every other route needs a named sender.
INTERNAL_ROUTES = frozenset({"digest_flush", "anomaly_alert", "review_pipeline", "queue_hygiene"})


def check_operator_contact(sender: str | None, *, task_id: int | None = None, route: str) -> None:
    """Gate an agent-initiated notification addressed to the operator.

    A caller with no verified identity (a stdio session without a token) is
    refused while operating profiles are configured, unless the route is a
    named hub-internal delivery; otherwise it would bypass every run's comms
    policy simply by not presenting a token.
    """
    if not sender:
        if route in INTERNAL_ROUTES:
            return
        if load_config().configured:
            events.record_event(
                channel=DENIED_CHANNEL,
                payload={"route": route, "from_agent": None, "reason": "unverified caller"},
                notify=False,
            )
            raise CommsPolicyDenied(
                "operating profiles are configured: an unverified caller cannot notify "
                "the operator; connect with an agent token"
            )
        return
    operator = config.operator_label()
    check_comms(sender, operator, task_id=task_id, route=route)


# ---------------------------------------------------------------------------
# Operator writes
# ---------------------------------------------------------------------------


def _bound_actor(actor: str | None) -> str:
    verified = config.verified_agent_identity()
    supplied = (actor or "").strip()
    if verified:
        if (
            supplied
            and config.canonical_agent_label(supplied) != config.canonical_agent_label(verified)
            and not (_is_operator(supplied) and _is_operator(verified))
        ):
            raise ToolError(f"caller is verified as {verified!r}; cannot act as {supplied!r}")
        return verified
    if config.enforce_auth():
        raise ToolError("operating settings require a verified identity")
    if not supplied:
        raise ToolError("actor is required")
    return supplied


def _require_targets(
    *, task_id: int | None = None, sprint_id: int | None = None, goal_id: int | None = None
) -> None:
    """Read-only tenant preflight. Writers repeat these checks under their write lock."""
    conn = storage.connect()
    try:
        if sprint_id is not None:
            sprint_get(sprint_id, conn=conn)
        if task_id is not None and _task_row(conn, task_id, fenced=True) is None:
            raise ToolError(f"unknown task: {task_id}")
        if goal_id is not None:
            if (
                conn.execute(
                    "SELECT 1 FROM goals WHERE id=? AND org_id IS ?",
                    (int(goal_id), edition.tenant_org_id()),
                ).fetchone()
                is None
            ):
                raise ToolError(f"unknown goal: {goal_id}")
    finally:
        conn.close()


def _authorize(
    actor: str | None,
    action: str,
    payload: dict[str, Any],
    *,
    task_id: int | None = None,
    sprint_id: int | None = None,
    goal_id: int | None = None,
) -> str:
    """Humans write directly; an agent's write waits for a human's approval.

    ``task_id`` is checked (tenant-fenced) before the gate, so a write against an
    unknown or foreign task is refused now rather than queued and refused on
    replay (task #2046). The write re-checks inside its own transaction."""
    who = _bound_actor(actor)
    if task_id is not None or sprint_id is not None or goal_id is not None:
        _require_targets(task_id=task_id, sprint_id=sprint_id, goal_id=goal_id)
    if _is_operator(who):
        return who
    from .tools.approvals import check_approval_gate

    check_approval_gate(APPROVAL_TOOL, {"action": action, **payload}, requested_by=who, force=True)
    return who


def _check_profile_name(name: object, cfg: OperatingConfig) -> str | None:
    if name in (None, ""):
        return None
    checked = _name(name, "profile")
    if checked not in cfg.profiles:
        raise ToolError(f"unknown operating profile {checked!r}; known {sorted(cfg.profiles)}")
    return checked


def _date(value: object, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ToolError(f"{field_name} must be an ISO-8601 date or datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(
            f"{field_name} must be an ISO-8601 date or datetime, got {value!r}"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _check_window(starts_at: object, ends_at: object) -> None:
    """Sprint dates are ISO-8601 (date or datetime; naive means UTC), start <= end."""
    start, end = _date(starts_at, "starts_at"), _date(ends_at, "ends_at")
    if start and end and start > end:
        raise ToolError("starts_at must not be after ends_at")


def _verify_resolves(
    conn: sqlite3.Connection,
    cfg: OperatingConfig,
    *,
    sprint_id: int | None = None,
    task_ids: tuple[int, ...] = (),
) -> None:
    """Refuse a write whose merged result would not resolve (e.g. funnel, no liaison).

    Called inside the write's transaction; the caller rolls back on error, so
    an incomplete override is never stored to block a later claim.
    """
    if sprint_id is not None:
        resolve(sprint_id=sprint_id, cfg=cfg, conn=conn, strict=True)
        # A pre-fence link of another tenant's task does not take this sprint (#2043).
        rows = conn.execute(
            "SELECT st.task_id FROM sprint_tasks st JOIN tasks t ON t.id = st.task_id "
            "JOIN sprints s ON s.id = st.sprint_id WHERE st.sprint_id=? AND t.org_id IS s.org_id",
            (int(sprint_id),),
        ).fetchall()
        task_ids = (*task_ids, *(int(r["task_id"]) for r in rows))
    for tid in task_ids:
        # Unfenced: these ids come from the join above or a caller's fenced check.
        if _task_row(conn, tid) is not None:
            resolve(task_id=tid, cfg=cfg, conn=conn, strict=True)


def sprint_add(
    name: str,
    *,
    actor: str | None = None,
    goal_id: int | None = None,
    org_unit: str | None = None,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
) -> dict[str, Any]:
    title = (name or "").strip()
    if not title or len(title) > 200:
        raise ToolError("sprint name must be 1-200 characters")
    cfg = load_config()
    profile = _check_profile_name(profile, cfg)
    clean = parse_settings(overrides or {}, "sprint overrides")
    _check_window(starts_at, ends_at)
    if org_unit is not None:
        from . import org_registry

        if org_registry.find_office(str(org_unit)) is None:
            raise ToolError(f"unknown office {org_unit!r}")
        org_unit = org_registry.find_office(str(org_unit))["id"]
    payload = {
        "name": title,
        "goal_id": goal_id,
        "org_unit": org_unit,
        "profile": profile,
        "overrides": clean,
        "starts_at": starts_at,
        "ends_at": ends_at,
    }
    who = _authorize(actor, "sprint_add", payload, goal_id=goal_id)
    now = storage.now()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cfg = load_config(strict=True)
        if (
            goal_id is not None
            and conn.execute(
                # Tenant-fenced like goals.py (#1999): a foreign goal is unknown.
                "SELECT 1 FROM goals WHERE id=? AND org_id IS ?",
                (int(goal_id), edition.tenant_org_id()),
            ).fetchone()
            is None
        ):
            raise ToolError(f"unknown goal: {goal_id}")
        cur = conn.execute(
            "INSERT INTO sprints (name, goal_id, org_unit, profile, overrides, starts_at, "
            "ends_at, created_by, created_at, updated_at, org_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                title,
                goal_id,
                org_unit,
                profile,
                json.dumps(clean, sort_keys=True),
                starts_at,
                ends_at,
                who,
                now,
                now,
                edition.tenant_org_id(),
            ),
        )
        try:
            _verify_resolves(conn, cfg, sprint_id=int(cur.lastrowid))
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return sprint_get(int(cur.lastrowid), conn=conn)
    finally:
        conn.close()


def sprint_get(sprint_id: int, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    # Tenant-fenced like goals (#2014): a foreign sprint is unknown, which
    # also refuses sprint_update and sprint_link_task, and a pre-fence link of
    # another tenant's task is not listed.
    tenant = edition.tenant_org_id()
    own = conn is None
    conn = conn if conn is not None else _connect()
    try:
        sprint = _sprint_row(
            conn.execute(
                "SELECT * FROM sprints WHERE id=? AND org_id IS ?", (int(sprint_id), tenant)
            ).fetchone()
        )
        if sprint is None:
            raise ToolError(f"unknown sprint: {sprint_id}")
        sprint["task_ids"] = [
            int(r["task_id"])
            for r in conn.execute(
                "SELECT s.task_id FROM sprint_tasks s JOIN tasks t ON t.id = s.task_id "
                "WHERE s.sprint_id=? AND t.org_id IS ? ORDER BY s.task_id",
                (int(sprint_id), tenant),
            )
        ]
        return sprint
    finally:
        if own:
            conn.close()


def sprint_list(status: str | None = None) -> dict[str, Any]:
    if status is not None and status not in SPRINT_STATUSES:
        raise ToolError(f"status must be one of {SPRINT_STATUSES}")
    conn = _connect()
    try:
        sql = "SELECT * FROM sprints WHERE org_id IS ?"
        args: tuple[Any, ...] = (edition.tenant_org_id(),)
        if status:
            sql += " AND status=?"
            args = (*args, status)
        rows = conn.execute(sql + " ORDER BY id DESC", args).fetchall()
        return {"sprints": [_sprint_row(r) for r in rows]}
    finally:
        conn.close()


def sprint_update(sprint_id: int, *, actor: str | None = None, **changes: Any) -> dict[str, Any]:
    allowed = {"name", "status", "profile", "overrides", "org_unit", "starts_at", "ends_at"}
    unknown = set(changes) - allowed
    if unknown or not changes:
        raise ToolError(f"sprint_update takes some of {sorted(allowed)}")
    cfg = load_config()
    values: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "name":
            title = (value or "").strip() if isinstance(value, str) else ""
            if not title or len(title) > 200:
                raise ToolError("sprint name must be 1-200 characters")
            values[key] = title
        elif key == "status":
            if value not in SPRINT_STATUSES:
                raise ToolError(f"status must be one of {SPRINT_STATUSES}")
            values[key] = value
        elif key == "profile":
            values[key] = _check_profile_name(value, cfg)
        elif key == "overrides":
            values[key] = json.dumps(
                parse_settings(value or {}, "sprint overrides"), sort_keys=True
            )
        elif key == "org_unit" and value is not None:
            from . import org_registry

            office = org_registry.find_office(str(value))
            if office is None:
                raise ToolError(f"unknown office {value!r}")
            values[key] = office["id"]
        else:
            values[key] = value
    _authorize(actor, "sprint_update", {"sprint_id": int(sprint_id), **values}, sprint_id=sprint_id)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cfg = load_config(strict=True)
        current = sprint_get(sprint_id, conn=conn)
        _check_window(
            values.get("starts_at", current.get("starts_at")),
            values.get("ends_at", current.get("ends_at")),
        )
        sets = ", ".join(f"{k}=?" for k in values)
        conn.execute(
            f"UPDATE sprints SET {sets}, updated_at=? WHERE id=?",  # noqa: S608 - keys allowlisted
            (*values.values(), storage.now(), int(sprint_id)),
        )
        try:
            _verify_resolves(conn, cfg, sprint_id=int(sprint_id))
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return sprint_get(sprint_id, conn=conn)
    finally:
        conn.close()


def sprint_link_task(
    sprint_id: int, task_id: int, *, actor: str | None = None, unlink: bool = False
) -> dict[str, Any]:
    who = _authorize(
        actor,
        "sprint_unlink_task" if unlink else "sprint_link_task",
        {"sprint_id": int(sprint_id), "task_id": int(task_id)},
        sprint_id=sprint_id,
        task_id=task_id,
    )
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cfg = load_config(strict=True)
        sprint_get(sprint_id, conn=conn)
        # Fenced like the sprint (#2014): another tenant's task is unknown here.
        if (
            conn.execute(
                "SELECT 1 FROM tasks WHERE id=? AND org_id IS ?",
                (int(task_id), edition.tenant_org_id()),
            ).fetchone()
            is None
        ):
            raise ToolError(f"unknown task: {task_id}")
        if unlink:
            conn.execute(
                "DELETE FROM sprint_tasks WHERE task_id=? AND sprint_id=?",
                (int(task_id), int(sprint_id)),
            )
        else:
            conn.execute(
                "INSERT INTO sprint_tasks (task_id, sprint_id, linked_by, linked_at) "
                "VALUES (?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "sprint_id=excluded.sprint_id, linked_by=excluded.linked_by, "
                "linked_at=excluded.linked_at",
                (int(task_id), int(sprint_id), who, storage.now()),
            )
        try:
            _verify_resolves(conn, cfg, task_ids=(int(task_id),))
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return sprint_get(sprint_id, conn=conn)
    finally:
        conn.close()


def run_settings_set(
    task_id: int,
    *,
    actor: str | None = None,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Set how the next run of ``task_id`` operates. A live run keeps its snapshot."""
    cfg = load_config()
    profile = _check_profile_name(profile, cfg)
    clean = parse_settings(overrides or {}, "run overrides")
    if profile is None and not clean:
        raise ToolError("give a profile, overrides, or both (run_clear removes settings)")
    who = _authorize(
        actor,
        "run_set",
        {"task_id": int(task_id), "profile": profile, "overrides": clean},
        task_id=task_id,
    )
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cfg = load_config(strict=True)
        if _task_row(conn, task_id, fenced=True) is None:
            raise ToolError(f"unknown task: {task_id}")
        conn.execute(
            "INSERT INTO run_settings (task_id, profile, overrides, set_by, set_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET profile=excluded.profile, "
            "overrides=excluded.overrides, set_by=excluded.set_by, set_at=excluded.set_at",
            (int(task_id), profile, json.dumps(clean, sort_keys=True), who, storage.now()),
        )
        try:
            _verify_resolves(conn, cfg, task_ids=(int(task_id),))
        except Exception:
            conn.rollback()
            raise
        conn.commit()
    finally:
        conn.close()
    return resolve(task_id=task_id, cfg=cfg, fenced=True)


def run_settings_clear(task_id: int, *, actor: str | None = None) -> dict[str, Any]:
    _authorize(actor, "run_clear", {"task_id": int(task_id)}, task_id=task_id)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _task_row(conn, task_id, fenced=True) is None:
            raise ToolError(f"unknown task: {task_id}")
        conn.execute("DELETE FROM run_settings WHERE task_id=?", (int(task_id),))
        # Resolve the response before committing: a clear it cannot report (a
        # pre-fence link to another tenant's sprint) rolls back, as run_set does.
        try:
            result = resolve(task_id=task_id, conn=conn, fenced=True)
        except Exception:
            conn.rollback()
            raise
        conn.commit()
    finally:
        conn.close()
    return result


def profiles_view() -> dict[str, Any]:
    cfg = load_config()
    return {
        "configured": cfg.configured,
        "default": cfg.default,
        "units": dict(cfg.units),
        "profiles": {
            name: {
                "description": p.description,
                "extends": p.extends,
                "settings": p.settings,
                "effective": flatten(cfg, name),
            }
            for name, p in sorted(cfg.profiles.items())
        },
        "fields": list(FIELDS),
        "enforced_fields": sorted(ENFORCED_FIELDS),
    }


# ---------------------------------------------------------------------------
# MCP surface: goal_manage actions prefixed ``operating_``
# ---------------------------------------------------------------------------

READ_ACTIONS = frozenset(
    {"operating_profiles", "operating_resolve", "operating_sprint_list", "operating_sprint_get"}
)


def _int(body: dict[str, Any], key: str, *, required: bool = True) -> int | None:
    value = body.get(key)
    if value is None:
        if required:
            raise ToolError(f"{key} is required")
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{key} must be an integer") from exc


def preflight(action: str, body: dict[str, Any] | None) -> None:
    """Refuse invisible operating targets before either public adapter parks approval."""
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    action = action.strip().lower()
    if action not in {
        "operating_run_set",
        "operating_run_clear",
        "operating_sprint_update",
        "operating_sprint_link",
        "operating_sprint_unlink",
        "operating_sprint_add",
    }:
        return
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ToolError("params must be an object")
    task_id = (
        _int(body, "task_id")
        if action
        in {
            "operating_run_set",
            "operating_run_clear",
            "operating_sprint_link",
            "operating_sprint_unlink",
        }
        else None
    )
    sprint_id = (
        _int(body, "sprint_id")
        if action
        in {
            "operating_sprint_update",
            "operating_sprint_link",
            "operating_sprint_unlink",
        }
        else None
    )
    goal_id = _int(body, "goal_id", required=False) if action == "operating_sprint_add" else None
    _require_targets(task_id=task_id, sprint_id=sprint_id, goal_id=goal_id)


def manage(action: str, body: dict[str, Any]) -> dict[str, Any]:
    actor = body.get("actor") or body.get("author")
    if action == "operating_profiles":
        return profiles_view()
    if action == "operating_resolve":
        return resolve(
            task_id=_int(body, "task_id", required=False),
            sprint_id=_int(body, "sprint_id", required=False),
            agent=body.get("agent"),
            fenced=True,
        )
    if action == "operating_sprint_add":
        return sprint_add(
            str(body.get("name") or ""),
            actor=actor,
            goal_id=_int(body, "goal_id", required=False),
            org_unit=body.get("org_unit"),
            profile=body.get("profile"),
            overrides=body.get("overrides"),
            starts_at=body.get("starts_at"),
            ends_at=body.get("ends_at"),
        )
    if action == "operating_sprint_list":
        return sprint_list(body.get("status"))
    if action == "operating_sprint_get":
        return sprint_get(_int(body, "sprint_id"))
    if action == "operating_sprint_update":
        changes = {
            k: body[k]
            for k in ("name", "status", "profile", "overrides", "org_unit", "starts_at", "ends_at")
            if k in body
        }
        return sprint_update(_int(body, "sprint_id"), actor=actor, **changes)
    if action in ("operating_sprint_link", "operating_sprint_unlink"):
        return sprint_link_task(
            _int(body, "sprint_id"),
            _int(body, "task_id"),
            actor=actor,
            unlink=action.endswith("unlink"),
        )
    if action == "operating_run_set":
        return run_settings_set(
            _int(body, "task_id"),
            actor=actor,
            profile=body.get("profile"),
            overrides=body.get("overrides"),
        )
    if action == "operating_run_clear":
        return run_settings_clear(_int(body, "task_id"), actor=actor)
    raise ToolError(
        "operating action must be one of operating_profiles, operating_resolve, "
        "operating_sprint_add, operating_sprint_list, operating_sprint_get, "
        "operating_sprint_update, operating_sprint_link, operating_sprint_unlink, "
        "operating_run_set, operating_run_clear"
    )
