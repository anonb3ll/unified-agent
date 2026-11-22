"""Operating-profile configuration: parsing, inheritance and tables (task #1732).

The core-layer half of operating profiles, like ``budget_policy`` under
``budgets``: storage and config revisions import it without reaching into the
services layer. Resolution and enforcement live in ``operating_profiles``.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import config

BLOCK = "operating_profiles"
COMMS_MODES = ("direct", "funnel", "custom")
PLANNING_LEVELS = ("P0", "P1", "P2", "P3", "P4")
SPRINT_STATUSES = ("planned", "active", "closed")
# Mirrors of delivery.DELIVERY_MODES and autonomy_modes.MODES, which sit in
# the services layer. tests/test_operating_profiles.py guards against drift.
DELIVERY_MODES = frozenset({"no-mistakes", "direct-pr", "local-only"})
AUTONOMY_MODES = ("observe", "exception", "checkpoint", "action-review")
FIELDS = (
    "comms",
    "delivery",
    "autonomy",
    "planning",
    "model",
    "budget",
    "instructions",
    "relay",
)
# Dimensions with a live enforcement point today (task #1756 wired all but
# ``relay``). The rest are frozen on the run and reported as ``recorded``.
# TODO(#1734): flip ``relay`` once tools/public_relay.resolve_relay_autosend
# reads operating_profiles.relay_autosend().
ENFORCED_FIELDS = frozenset(
    {"comms", "delivery", "autonomy", "planning", "model", "budget", "instructions"}
)
OPERATOR = "operator"
LIAISON = "liaison"
APPROVAL_TOOL = "operating_settings"
DENIED_CHANNEL = "agent.comms.denied"
DEGRADED_CHANNEL = "agent.operating.degraded"
MAX_EXTENDS_DEPTH = 8
MAX_INSTRUCTIONS = 4000
MAX_RELAY_CHANNELS = 16
_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_LABEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")


class OperatingConfigError(ToolError):
    """The operating_profiles block or a stored override is malformed."""


class CommsPolicyDenied(ToolError):
    """A message or notification is outside the run's comms policy."""


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    extends: str | None
    settings: dict[str, Any]


@dataclass
class OperatingConfig:
    default: str | None = None
    profiles: dict[str, Profile] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    # Lenient loads (the live path) record what they had to drop here instead
    # of failing hub-wide; strict loads (policy revisions) raise.
    warnings: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.profiles)


# ---------------------------------------------------------------------------
# Parsing (fails closed on unknown keys and values)
# ---------------------------------------------------------------------------


def _err(where: str, message: str) -> OperatingConfigError:
    return OperatingConfigError(f"{where}: {message}")


def _mapping(raw: object, where: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _err(where, "must be a mapping")
    unknown = set(raw) - allowed
    if unknown:
        raise _err(where, f"unknown keys {sorted(map(str, unknown))}; allowed {sorted(allowed)}")
    return raw


def _name(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not _NAME_RE.fullmatch(raw):
        raise _err(where, f"profile names are lowercase slugs, got {raw!r}")
    return raw


def _token(raw: object, where: str) -> str:
    """A comms party: a label, ``*``, ``operator``, ``liaison`` or ``office:<id>``."""
    if not isinstance(raw, str) or not raw.strip():
        raise _err(where, "comms parties must be non-empty strings")
    text = raw.strip()
    if text in ("*", OPERATOR, LIAISON):
        return text
    if text.startswith("office:"):
        office = text.split(":", 1)[1]
        if not _NAME_RE.fullmatch(office):
            raise _err(where, f"invalid office id in {text!r}")
        return text
    if not _LABEL_RE.fullmatch(text):
        raise _err(where, f"invalid identity label {text!r}")
    return text


def _liaison(raw: object, where: str) -> str:
    text = _token(raw, where)
    if text in ("*", OPERATOR, LIAISON):
        raise _err(where, "liaison must be an identity label or office:<id>")
    return text


def _parse_comms(raw: object, where: str) -> dict[str, Any]:
    body = _mapping(raw, where, {"mode", "liaison", "allow"})
    out: dict[str, Any] = {}
    if "mode" in body:
        if body["mode"] not in COMMS_MODES:
            raise _err(where, f"mode must be one of {COMMS_MODES}")
        out["mode"] = body["mode"]
    if "liaison" in body:
        out["liaison"] = _liaison(body["liaison"], f"{where}.liaison")
    if "allow" in body:
        rules = body["allow"]
        if not isinstance(rules, list):
            raise _err(where, "allow must be a list of {from, to} rules")
        parsed = []
        for i, rule in enumerate(rules):
            item = _mapping(rule, f"{where}.allow[{i}]", {"from", "to"})
            if set(item) != {"from", "to"}:
                raise _err(f"{where}.allow[{i}]", "needs both from and to")
            parsed.append(
                {
                    "from": _token(item["from"], f"{where}.allow[{i}].from"),
                    "to": _token(item["to"], f"{where}.allow[{i}].to"),
                }
            )
        out["allow"] = parsed
    return out


def _parse_delivery(raw: object, where: str) -> dict[str, Any]:
    body = _mapping(raw, where, {"mode", "yolo"})
    out: dict[str, Any] = {}
    if "mode" in body:
        if body["mode"] not in DELIVERY_MODES:
            raise _err(where, f"mode must be one of {sorted(DELIVERY_MODES)}")
        out["mode"] = body["mode"]
    if "yolo" in body:
        if not isinstance(body["yolo"], bool):
            raise _err(where, "yolo must be true or false")
        out["yolo"] = body["yolo"]
    return out


def _positive(raw: object, where: str, *, integer: bool) -> int | float:
    kinds = (int,) if integer else (int, float)
    # NaN and infinity pass ``raw <= 0``; neither is a spending limit.
    if (
        isinstance(raw, bool)
        or not isinstance(raw, kinds)
        or (isinstance(raw, float) and not math.isfinite(raw))
        or raw <= 0
    ):
        raise _err(where, "must be a positive " + ("integer" if integer else "number"))
    return raw


def _parse_budget(raw: object, where: str) -> dict[str, Any]:
    body = _mapping(raw, where, {"max_cost_usd", "max_time_s", "max_tokens"})
    out: dict[str, Any] = {}
    if "max_cost_usd" in body:
        out["max_cost_usd"] = float(_positive(body["max_cost_usd"], where, integer=False))
    for key in ("max_time_s", "max_tokens"):
        if key in body:
            out[key] = int(_positive(body[key], f"{where}.{key}", integer=True))
    return out


def _parse_relay(raw: object, where: str) -> dict[str, Any]:
    """``relay: {autosend: [channel, ...]}``: channels a public relay may send to unprompted."""
    body = _mapping(raw, where, {"autosend"})
    out: dict[str, Any] = {}
    if "autosend" in body:
        channels = body["autosend"]
        if not isinstance(channels, list) or len(channels) > MAX_RELAY_CHANNELS:
            raise _err(
                f"{where}.autosend", f"must be a list of up to {MAX_RELAY_CHANNELS} channels"
            )
        seen: list[str] = []
        for i, channel in enumerate(channels):
            if not isinstance(channel, str) or not _NAME_RE.fullmatch(channel):
                raise _err(
                    f"{where}.autosend[{i}]", f"channels are lowercase slugs, got {channel!r}"
                )
            if channel not in seen:
                seen.append(channel)
        out["autosend"] = seen
    return out


def parse_settings(raw: object, where: str = "settings") -> dict[str, Any]:
    """Validate a partial settings mapping (a profile body or an override)."""
    if raw is None:
        return {}
    body = _mapping(raw, where, set(FIELDS))
    out: dict[str, Any] = {}
    if "comms" in body:
        out["comms"] = _parse_comms(body["comms"], f"{where}.comms")
    if "delivery" in body:
        out["delivery"] = _parse_delivery(body["delivery"], f"{where}.delivery")
    if "autonomy" in body:
        if body["autonomy"] not in AUTONOMY_MODES:
            raise _err(f"{where}.autonomy", f"must be one of {AUTONOMY_MODES}")
        out["autonomy"] = body["autonomy"]
    if "planning" in body:
        if body["planning"] not in PLANNING_LEVELS:
            raise _err(f"{where}.planning", f"must be one of {PLANNING_LEVELS}")
        out["planning"] = body["planning"]
    if "model" in body:
        if not isinstance(body["model"], str) or not _MODEL_RE.fullmatch(body["model"]):
            raise _err(f"{where}.model", "must be a model id without spaces")
        out["model"] = body["model"]
    if "budget" in body:
        out["budget"] = _parse_budget(body["budget"], f"{where}.budget")
    if "instructions" in body:
        text = body["instructions"]
        if not isinstance(text, str) or len(text) > MAX_INSTRUCTIONS:
            raise _err(f"{where}.instructions", f"must be text up to {MAX_INSTRUCTIONS} chars")
        out["instructions"] = text
    if "relay" in body:
        out["relay"] = _parse_relay(body["relay"], f"{where}.relay")
    return out


def _check_comms_complete(comms: dict[str, Any], where: str) -> None:
    mode = comms.get("mode", "direct")
    if mode == "funnel" and not comms.get("liaison"):
        raise _err(where, "funnel comms needs a liaison (a label or office:<id>)")
    if mode == "custom" and not comms.get("allow"):
        raise _err(where, "custom comms needs at least one allow rule")


def parse_config(
    raw: object, *, offices: list[dict[str, Any]] | None = None, strict: bool = True
) -> OperatingConfig:
    """Parse the block. ``strict=False`` (the live path) drops a unit binding
    whose office is gone from org.yaml, with a warning, instead of failing
    every message and claim on the hub; a policy revision parses strictly."""
    body = _mapping(raw, BLOCK, {"default", "profiles", "units"})
    raw_profiles = body.get("profiles") or {}
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise _err(f"{BLOCK}.profiles", "must name at least one profile")
    profiles: dict[str, Profile] = {}
    for key, value in raw_profiles.items():
        name = _name(key, f"{BLOCK}.profiles")
        where = f"{BLOCK}.profiles.{name}"
        item = _mapping(value or {}, where, {"description", "extends", *FIELDS})
        extends = item.get("extends")
        if extends is not None:
            extends = _name(extends, f"{where}.extends")
        description = item.get("description") or ""
        if not isinstance(description, str):
            raise _err(f"{where}.description", "must be text")
        settings = parse_settings(
            {k: v for k, v in item.items() if k in FIELDS},
            where,
        )
        profiles[name] = Profile(name, description, extends, settings)
    for profile in profiles.values():
        if profile.extends is not None and profile.extends not in profiles:
            raise _err(f"{BLOCK}.profiles.{profile.name}", f"extends unknown {profile.extends!r}")
    cfg = OperatingConfig(profiles=profiles)
    for name in profiles:
        flat = flatten(cfg, name)
        _check_comms_complete(flat.get("comms", {}), f"{BLOCK}.profiles.{name}.comms")
    default = body.get("default")
    if default is not None:
        default = _name(default, f"{BLOCK}.default")
        if default not in profiles:
            raise _err(f"{BLOCK}.default", f"unknown profile {default!r}")
    cfg.default = default
    raw_units = body.get("units") or {}
    if not isinstance(raw_units, dict):
        raise _err(f"{BLOCK}.units", "must map office ids to profile names")
    known = None
    if raw_units:
        from . import org_registry

        try:
            known = {
                o["id"] for o in (offices if offices is not None else org_registry.load_registry())
            }
        except Exception as exc:  # noqa: BLE001 - an unreadable registry
            if strict:
                raise _err(f"{BLOCK}.units", f"cannot read the org registry: {exc}") from exc
            cfg.warnings.append(f"org registry unreadable; office bindings ignored: {exc}")
            raw_units = {}
    for office, profile in raw_units.items():
        if known is not None and office not in known:
            if strict:
                raise _err(f"{BLOCK}.units", f"unknown office {office!r}")
            cfg.warnings.append(f"office {office!r} is not in org.yaml; its binding is ignored")
            continue
        name = _name(profile, f"{BLOCK}.units.{office}")
        if name not in profiles:
            raise _err(f"{BLOCK}.units.{office}", f"unknown profile {name!r}")
        cfg.units[str(office)] = name
    return cfg


def load_config(path: Path | None = None, *, strict: bool = False) -> OperatingConfig:
    """The operating_profiles block of the policy file; empty when absent.

    The live path is lenient about org.yaml drift (see :func:`parse_config`);
    a malformed block still fails closed. Policy revisions pass ``strict=True``.
    """
    p = path if path is not None else config.policy_file_path()
    if not p.exists():
        return OperatingConfig()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any read/parse failure fails closed
        raise OperatingConfigError(f"cannot read {BLOCK} from {p}: {exc}") from exc
    if not isinstance(data, dict) or BLOCK not in data:
        return OperatingConfig()
    return parse_config(data[BLOCK], strict=strict)


# ---------------------------------------------------------------------------
# Merging with provenance
# ---------------------------------------------------------------------------

_DICT_FIELDS = ("comms", "delivery", "budget", "relay")


def _merge(
    target: dict[str, Any], sources: dict[str, str], layer: dict[str, Any], label: str
) -> None:
    for key, value in layer.items():
        if key in _DICT_FIELDS:
            merged = dict(target.get(key) or {})
            for sub, sub_value in value.items():
                merged[sub] = sub_value
                sources[f"{key}.{sub}"] = label
            target[key] = merged
        else:
            target[key] = value
            sources[key] = label


def flatten(cfg: OperatingConfig, name: str) -> dict[str, Any]:
    """A profile's settings with its ``extends`` chain applied."""
    chain: list[Profile] = []
    current: str | None = name
    while current is not None:
        if len(chain) >= MAX_EXTENDS_DEPTH or any(p.name == current for p in chain):
            raise _err(f"{BLOCK}.profiles.{name}", "extends chain loops or is too deep")
        profile = cfg.profiles.get(current)
        if profile is None:
            raise _err(f"{BLOCK}.profiles", f"unknown profile {current!r}")
        chain.append(profile)
        current = profile.extends
    out: dict[str, Any] = {}
    for profile in reversed(chain):
        _merge(out, {}, profile.settings, profile.name)
    return out


# ---------------------------------------------------------------------------
# Database state: sprints and per-run settings
# ---------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sprints (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          goal_id INTEGER,
          org_unit TEXT,
          profile TEXT,
          overrides TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'planned'
            CHECK (status IN ('planned', 'active', 'closed')),
          starts_at TEXT,
          ends_at TEXT,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sprint_tasks (
          task_id INTEGER PRIMARY KEY,
          sprint_id INTEGER NOT NULL REFERENCES sprints(id),
          linked_by TEXT NOT NULL,
          linked_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sprint_tasks_sprint ON sprint_tasks(sprint_id);
        CREATE TABLE IF NOT EXISTS run_settings (
          task_id INTEGER PRIMARY KEY,
          profile TEXT,
          overrides TEXT NOT NULL DEFAULT '{}',
          set_by TEXT NOT NULL,
          set_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_operating_snapshots (
          run_id TEXT PRIMARY KEY,
          task_id INTEGER NOT NULL,
          agent TEXT NOT NULL,
          snapshot TEXT NOT NULL,
          recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_operating_snapshots_task
          ON run_operating_snapshots(task_id, recorded_at);
        -- A task-tagged message reads the sender's own newest run (task #1775).
        CREATE INDEX IF NOT EXISTS idx_run_operating_snapshots_task_agent
          ON run_operating_snapshots(task_id, agent, recorded_at);
        """
    )
    # ``claim_ulid`` orders a task's runs by claim, immune to wall-clock steps:
    # it is minted on the write lock, after everything already committed, so it
    # sorts in commit order from every process. ``recorded_at`` is only a label.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(run_operating_snapshots)")}
    if "claim_ulid" not in cols:
        conn.execute("ALTER TABLE run_operating_snapshots ADD COLUMN claim_ulid TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_operating_snapshots_claim "
        "ON run_operating_snapshots(task_id, claim_ulid)"
    )
    # The sender-scoped newest-run lookup (task #1775) in claim order.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_operating_snapshots_agent_claim "
        "ON run_operating_snapshots(task_id, agent, claim_ulid)"
    )
    # sprints.org_id (task #2014), the goals.org_id pattern (#1999): nullable,
    # stamped at insert from edition.tenant_org_id(). Rows that predate it take
    # their bound goal's tenant once, when the column is added; a sprint with
    # no goal stays NULL (untenanted). No SCHEMA_VERSION bump: additive.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sprints)")}
    if "org_id" not in cols:
        conn.execute("ALTER TABLE sprints ADD COLUMN org_id TEXT")
        goal_cols = {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}
        if "org_id" in goal_cols:
            conn.execute(
                "UPDATE sprints SET org_id=(SELECT g.org_id FROM goals g "
                "WHERE g.id=sprints.goal_id) WHERE goal_id IS NOT NULL"
            )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sprints_org_id ON sprints(org_id)")


def migrate(conn: sqlite3.Connection) -> None:
    """Called from storage._migrate only: tables plus the one-time backfill."""
    ensure_schema(conn)
    _backfill_snapshots(conn)
    _backfill_claim_order(conn)


def _backfill_snapshots(conn: sqlite3.Connection) -> None:
    backfill_key = "operating_snapshots_backfilled"
    if conn.execute("SELECT 1 FROM meta WHERE key=?", (backfill_key,)).fetchone():
        return
    # Runs claimed before the keyed table existed carry their snapshot only in
    # the claim event; copy those once so lookups never scan the event log.
    has_events = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_log'"
    ).fetchone()
    if has_events is not None:
        conn.execute(
            "INSERT OR IGNORE INTO run_operating_snapshots "
            "(run_id, task_id, agent, snapshot, recorded_at, claim_ulid) "
            "SELECT json_extract(payload, '$.run_id'), json_extract(payload, '$.task_id'), "
            "json_extract(payload, '$.agent'), COALESCE(json_extract(payload, '$.operating_profile'), '{}'), "
            "created_at, ulid FROM event_log WHERE channel = 'agent.run.selfpickup' "
            "AND json_extract(payload, '$.kind') = 'selfpickup.claimed' "
            "AND NOT EXISTS (SELECT 1 FROM run_operating_snapshots r "
            "WHERE r.run_id = json_extract(payload, '$.run_id')) ORDER BY ulid"
        )
    conn.execute("INSERT INTO meta(key, value) VALUES (?, '1')", (backfill_key,))


def _backfill_claim_order(conn: sqlite3.Connection) -> None:
    """One-time: give snapshots stored before ``claim_ulid`` their claim event's ULID."""
    key = "operating_snapshots_claim_ulid_backfilled"
    if conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
        return
    has_events = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_log'"
    ).fetchone()
    if has_events is not None:
        conn.execute(
            "UPDATE run_operating_snapshots SET claim_ulid = ("
            "SELECT MIN(e.ulid) FROM event_log e WHERE e.channel = 'agent.run.selfpickup' "
            "AND json_extract(e.payload, '$.kind') = 'selfpickup.claimed' "
            "AND json_extract(e.payload, '$.run_id') = run_operating_snapshots.run_id) "
            "WHERE claim_ulid IS NULL"
        )
    conn.execute("INSERT INTO meta(key, value) VALUES (?, '1')", (key,))


def record_snapshot(
    conn: sqlite3.Connection, run_id: str, task_id: int, agent: str, snapshot: dict[str, Any]
) -> None:
    """Key a run's frozen snapshot by run id, inside the claim transaction (no DDL)."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_operating_snapshots'"
    ).fetchone()
    if present is None:
        return
    values = [
        run_id,
        int(task_id),
        agent,
        json.dumps(snapshot, sort_keys=True),
        datetime.now(UTC).isoformat(),
    ]
    columns = "run_id, task_id, agent, snapshot, recorded_at"
    if snapshot_claim_order_ready(conn):
        from . import events

        # Minted under the claim's write lock, so it sorts after every claim
        # already committed even when the wall clock stepped backward.
        columns += ", claim_ulid"
        values.append(events.mint_ulid_on_connection(conn))
    marks = ",".join("?" * len(values))
    conn.execute(
        f"INSERT OR REPLACE INTO run_operating_snapshots ({columns}) VALUES ({marks})",  # noqa: S608
        values,
    )


def snapshot_claim_order_ready(conn: sqlite3.Connection) -> bool:
    """Whether the snapshot table carries ``claim_ulid`` (a pre-migration DB does not yet)."""
    return any(
        r["name"] == "claim_ulid"
        for r in conn.execute("PRAGMA table_info(run_operating_snapshots)")
    )


def referenced_profiles(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Profile names that stored sprints and run settings depend on, with who uses them."""
    present = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('sprints', 'run_settings')"
    ).fetchone()[0]
    if present != 2:
        return {}
    out: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT id, profile FROM sprints WHERE profile IS NOT NULL "
        "AND (status != 'closed' OR EXISTS (SELECT 1 FROM sprint_tasks st "
        "JOIN tasks t ON t.id = st.task_id WHERE st.sprint_id = sprints.id))"
    ):
        out.setdefault(row["profile"], []).append(f"sprint {row['id']}")
    for row in conn.execute("SELECT task_id, profile FROM run_settings WHERE profile IS NOT NULL"):
        out.setdefault(row["profile"], []).append(f"run settings of task {row['task_id']}")
    return out


def check_references(cfg: OperatingConfig, conn: sqlite3.Connection) -> None:
    """Refuse a policy that would orphan a profile stored sprints or run settings use."""
    missing = {
        name: users for name, users in referenced_profiles(conn).items() if name not in cfg.profiles
    }
    if missing:
        detail = "; ".join(
            f"{name!r} used by {', '.join(users[:5])}" for name, users in missing.items()
        )
        raise OperatingConfigError(
            f"{BLOCK}: the revision removes profiles still in use ({detail}); "
            "clear or repoint those settings first"
        )


def forget_tasks(conn: sqlite3.Connection, task_ids: list[int]) -> None:
    """Drop sprint links and run settings of deleted tasks, inside the caller's transaction."""
    if not task_ids:
        return
    present = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('sprint_tasks', 'run_settings')"
    ).fetchone()[0]
    if present != 2:
        return
    marks = ",".join("?" * len(task_ids))
    ids = [int(t) for t in task_ids]
    conn.execute(f"DELETE FROM sprint_tasks WHERE task_id IN ({marks})", ids)  # noqa: S608
    conn.execute(f"DELETE FROM run_settings WHERE task_id IN ({marks})", ids)  # noqa: S608
