"""Per-host fleet snapshots for the Hosts page, /agents/hosts (task #798).

``system_doctor`` already reports per-unit LIVE/DORMANT/MISCONFIGURED status
on the process's own host. This module stores that snapshot and aggregates it
with MCP connection liveness from the agent registry so the hub can show every
observed fleet host with an as-of timestamp and a staleness label.

Accuracy contract: a row never presents guessed doctor/executor/worktree state
as live. Missing remote reports stay ``reported: false`` / ``doctor_freshness:
"missing"``. A host row is a doctor snapshot or a registered ua-node. An agent
label, attribution string, or Desktop_/Laptop_/server_ identity bucket is never
a host row. Agents attach only to a host row that already exists.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from . import config, edition, registry, storage

MAX_SNAPSHOT_BYTES = 256_000

# Documented identity-map prefixes. These group MCP connections; they are not a
# live probe of that machine's systemd/launchd units.
_ROLE_PREFIXES = (
    ("Laptop_", "laptop"),
    ("Desktop_", "desktop"),
    ("server_", "hub"),
    ("gcp_", "gcp"),
)

# Where an agent's host came from, strongest first (task #1388).
HOST_SOURCES = ("node", "binding", "heartbeat", "node_last", "prefix")


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("unified-agent")
    except Exception:
        return "unknown"


def _alias_values() -> set[str]:
    return {alias for aliases in config.identity_alias_allowlist().values() for alias in aliases}


def _observed_agent_labels() -> set[str]:
    """Labels stored on ``agents``. Read-only; never rewrites history."""
    try:
        conn = storage.connect()
        try:
            rows = conn.execute("SELECT label FROM agents").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return {row["label"] for row in rows if row["label"]}


# Sentence words are attribution even when the names are unknown
# (``Alice working with Bob``, ``Alice by Bob``). ``by the <word>`` is a
# place name for any word, including ones this list has never seen
# (``Workstation by the Bay``). ``from``/``in``/``on`` also appear in
# hostnames (``Mac hub in Office``), so those count only beside an
# identity.
_SENTENCE_WORDS = frozenset({"authorized", "by", "chat", "via", "with", "working"})
_IDENTITY_CONNECTORS = frozenset({"from", "in", "on"})
# ``by the`` is a place unless the phrase is ``by the <role>``
# (``Report by the operator``). A role word anywhere else belongs to the
# machine name and stays a place (``Agent PC by the Bay``).
_ROLE_WORDS = frozenset({"agent", "human", "operator", "steward", "user"})


def _identity_words(words: list[str]) -> bool:
    pool = set(config.fleet_agent_labels())
    pool |= config.agent_labels()
    pool |= config.expected_unregistered_labels()
    pool |= set(config.human_identity_labels())
    pool |= _alias_values()
    known = {token.casefold() for token in pool if token}
    if any(word in known for word in words):
        return True
    return any(word.startswith(("desktop_", "laptop_", "server_")) for word in words)


def _host_name_is_attribution_prose(host: str) -> bool:
    """True when ``host`` is attribution syntax rather than a machine name.

    Parentheses (``Claude Code (Omnigent spike)``) are attribution. So are
    sentence words (``working``, ``with``, ``via``, ``by``, ``authorized``,
    ``chat``), including when neither name is on the roster. ``by the``
    followed by any word is a place name, not a closed place list
    (``Workstation by the Bay``); it is attribution only as the phrase
    ``by the <role>`` (``Report by the operator``), so a machine name that
    merely contains a role word (``Agent PC by the Bay``) stays a host.
    ``from``, ``in``, and ``on`` are
    attribution only when the same phrase also names a human, agent, alias,
    or fleet prefix. A measured hostname may contain spaces, an article, a
    place, or a roster label. A bare agent label is not prose.
    """
    if "(" in host or ")" in host:
        return True
    words = [raw.strip(".,;:").casefold() for raw in host.split()]
    words = [word for word in words if word]
    for index, word in enumerate(words):
        if word not in _SENTENCE_WORDS:
            continue
        # ``by the <word>`` is a place, including unlisted places, unless it is
        # ``by the <role>`` (``Report by the operator``). Only a role word
        # immediately after the article is attribution: a role word elsewhere
        # belongs to the machine name (``Agent PC by the Bay``), which must
        # still be recorded as a host.
        if word == "by" and index + 1 < len(words) and words[index + 1] == "the":
            if index + 2 < len(words) and words[index + 2] in _ROLE_WORDS:
                return True
            continue
        return True
    if any(word in _IDENTITY_CONNECTORS for word in words):
        return _identity_words(words)
    return False


def host_key_is_attribution(
    host: str | None,
    *,
    observed: set[str] | None = None,
    node_names: set[str] | None = None,
) -> bool:
    """True when ``host`` must not be written as a heartbeat-only host.

    Doctor snapshots are stored on their own path and may reuse an agent
    label or a hostname that contains spaces. A registered ua-node name is
    not an attribution.
    """
    name = (host or "").strip()
    if not name:
        return True
    if node_names and name in node_names:
        return False
    if any(ch.isspace() for ch in name) or "(" in name or ")" in name:
        return True
    if config.canonical_agent_label(name) is not None:
        return True
    if name in config.agent_labels() or name in config.expected_unregistered_labels():
        return True
    if name in config.human_identity_labels() or name in _alias_values():
        return True
    for prefix, _role in _ROLE_PREFIXES:
        if name.startswith(prefix):
            return True
    labels = observed if observed is not None else _observed_agent_labels()
    return name in labels


def host_id_for_agent(label: str | None, recorded_host: str | None = None) -> str | None:
    """Host key for one agent: measured hostname wins, else identity-map role."""
    if recorded_host and recorded_host.strip():
        return recorded_host.strip()
    if not label:
        return None
    for prefix, role in _ROLE_PREFIXES:
        if label.startswith(prefix):
            return role
    return None


def doctor_freshness(as_of: str | None, *, now: datetime | None = None) -> str:
    """live | stale | missing — never inferred from MCP last-seen."""
    status = registry.derive_status(as_of, now=now)
    if status == "unknown":
        return "missing"
    if status in ("online", "idle"):
        return "live"
    return "stale"


def _load_json(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _dump_json(value: Any) -> str | None:
    if value is None:
        return None
    encoded = json.dumps(value, default=str)
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        return None
    return encoded


def _newer(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return a if a >= b else b


def _current_executor_claim() -> dict[str, Any] | None:
    agent = config.executor_agent()
    if not agent:
        return None
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id, title, claimed_by, claimed_at FROM tasks "
            "WHERE status='doing' AND claimed_by=? "
            "ORDER BY claimed_at DESC LIMIT 1",
            (agent,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "task_id": row["id"],
        "title": row["title"],
        "claimed_by": row["claimed_by"],
        "claimed_at": row["claimed_at"],
    }


def _worktree_census(found: list[dict[str, Any]] | None, as_of: str) -> dict[str, Any]:
    if found is None:
        return {"reported": False, "as_of": None}
    return {
        "reported": True,
        "as_of": as_of,
        "total": len(found),
        "stale": sum(1 for item in found if item.get("stale")),
        "disposable": sum(1 for item in found if item.get("disposable")),
    }


def _executor_from_checks(checks: list[dict[str, Any]], as_of: str) -> dict[str, Any]:
    match = next(
        (item for item in checks if item.get("capability") == "active_executor_service"),
        None,
    )
    return {
        "reported": True,
        "as_of": as_of,
        "unit_status": match["status"] if match else "UNKNOWN",
        "detail": match.get("detail") if match else "no executor check in this report",
        "agent": config.executor_agent() or None,
        "claim": _current_executor_claim(),
    }


def touch_host_seen(
    host: str | None,
    *,
    via: str,
    version: str | None = None,
) -> bool:
    """Record that `host` was observed. Best-effort; never raises."""
    if not host or not host.strip():
        return False
    host = host.strip()
    if host_key_is_attribution(host):
        return False
    ts = storage.now()
    try:
        conn = storage.connect()
        try:
            conn.execute(
                """
                INSERT INTO host_reports
                  (host, last_seen, last_seen_via, version, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                  last_seen = excluded.last_seen,
                  last_seen_via = excluded.last_seen_via,
                  version = COALESCE(excluded.version, host_reports.version),
                  updated_at = excluded.updated_at
                """,
                (host, ts, via, version, ts),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def record_doctor_snapshot(
    host: str,
    report: dict[str, Any],
    *,
    via: str = "system_doctor",
    version: str | None = None,
    worktrees: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
    worktrees_as_of: str | None = None,
) -> str | None:
    """Persist a doctor/survey snapshot for `host`. Best-effort; never raises.

    ``worktrees_as_of`` stamps the census when the survey is older than the
    doctor report (a shared survey, task #1897). Returns the as-of timestamp
    written, or None if the write failed.
    """
    if not host or not host.strip():
        return None
    host = host.strip()
    # A measured hostname may contain spaces or collide with an agent label.
    # current_source_host() is not normalized. An attribution sentence
    # (parentheses) is not a machine, even when a doctor payload arrived with it.
    if _host_name_is_attribution_prose(host):
        return None
    checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    ts = as_of or storage.now()
    doctor = {"checks": checks, "summary": summary}
    if _dump_json(doctor) is None:
        return None
    executor = _executor_from_checks(checks, ts)
    census = _worktree_census(worktrees, worktrees_as_of or ts)
    try:
        conn = storage.connect()
        try:
            conn.execute(
                """
                INSERT INTO host_reports
                  (host, last_seen, last_seen_via, version, doctor_as_of,
                   doctor_json, executor_json, worktrees_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                  last_seen = excluded.last_seen,
                  last_seen_via = excluded.last_seen_via,
                  version = COALESCE(excluded.version, host_reports.version),
                  doctor_as_of = excluded.doctor_as_of,
                  doctor_json = excluded.doctor_json,
                  executor_json = excluded.executor_json,
                  worktrees_json = excluded.worktrees_json,
                  updated_at = excluded.updated_at
                """,
                (
                    host,
                    ts,
                    via,
                    version or _package_version(),
                    ts,
                    _dump_json(doctor),
                    _dump_json(executor),
                    _dump_json(census),
                    ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return ts
    except Exception:
        return None


def record_local_doctor_snapshot(report: dict[str, Any]) -> str | None:
    """Persist this process's doctor report under ``current_source_host()``."""
    found = None
    survey_as_of = None
    try:
        from .tools.worktree_policy import cached_survey_worktrees

        # The doctor's own stale_worktrees row ran this survey moments ago, so
        # this reads the shared result instead of running git again (#1897).
        found, age_s = cached_survey_worktrees()
        if age_s > 0:
            survey_as_of = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat(
                timespec="seconds"
            )
    except Exception:
        found = None
    return record_doctor_snapshot(
        config.current_source_host(),
        report,
        via="system_doctor",
        version=_package_version(),
        worktrees=found,
        worktrees_as_of=survey_as_of,
    )


def _unreported_executor() -> dict[str, Any]:
    return {"reported": False, "as_of": None, "unit_status": None, "claim": None}


def _unreported_worktrees() -> dict[str, Any]:
    return {"reported": False, "as_of": None}


def _row_from_report(row: sqlite3.Row, now: datetime) -> dict[str, Any]:
    doctor = _load_json(row["doctor_json"])
    executor = _load_json(row["executor_json"]) or _unreported_executor()
    worktrees = _load_json(row["worktrees_json"]) or _unreported_worktrees()
    doctor_as_of = row["doctor_as_of"]
    last_seen = row["last_seen"]
    as_of = _newer(last_seen, doctor_as_of)
    doctor_payload = None
    if isinstance(doctor, dict) and doctor_as_of:
        doctor_payload = {
            "as_of": doctor_as_of,
            "summary": doctor.get("summary") or {},
            "checks": doctor.get("checks") or [],
        }
    return {
        "host": row["host"],
        "host_source": "doctor" if doctor_as_of else "heartbeat",
        "as_of": as_of,
        "staleness": registry.derive_status(as_of, now=now),
        "last_seen": last_seen,
        "last_seen_via": row["last_seen_via"],
        "last_seen_status": registry.derive_status(last_seen, now=now),
        "version": row["version"],
        "doctor": doctor_payload,
        "doctor_freshness": doctor_freshness(doctor_as_of, now=now),
        "executor": executor,
        "worktrees": worktrees,
        "agents": [],
    }


def _blank_host(host: str, *, host_source: str, now: datetime) -> dict[str, Any]:
    return {
        "host": host,
        "host_source": host_source,
        "as_of": None,
        "staleness": "unknown",
        "last_seen": None,
        "last_seen_via": None,
        "last_seen_status": "unknown",
        "version": None,
        "doctor": None,
        "doctor_freshness": "missing",
        "executor": _unreported_executor(),
        "worktrees": _unreported_worktrees(),
        "agents": [],
    }


def _credentials() -> dict[str, dict[str, Any]]:
    """Credential metadata per agent label; never a token value (task #1388).

    ``issued_token_ids`` are the active rows of the ``agent_tokens`` lifecycle
    table. Issuance is not proof that a credential is currently configured;
    ``fingerprints`` are the first 12 hex chars of the SHA-256 of each static
    ``UA_AGENT_TOKENS`` entry — enough to tell two credentials apart, useless
    to authenticate with.
    """
    import hashlib

    out: dict[str, dict[str, Any]] = {}

    def entry(label: str) -> dict[str, Any]:
        return out.setdefault(label, {"issued_token_ids": [], "fingerprints": []})

    # Static configuration has no per-token org binding. In a tenant context,
    # attributing its global entries to the selected org would leak metadata.
    if edition.tenant_org_id() is None:
        try:
            for token, label in config.agent_tokens().items():
                entry(label)["fingerprints"].append(hashlib.sha256(token.encode()).hexdigest()[:12])
        except Exception:  # noqa: BLE001 - metadata is advisory
            pass
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT id, agent_label FROM agent_tokens "
                "WHERE status='active' AND org_id IS ? ORDER BY id",
                (edition.tenant_org_id(),),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            entry(row["agent_label"])["issued_token_ids"].append(int(row["id"]))
    except sqlite3.Error:
        pass
    for meta in out.values():
        meta["fingerprints"].sort()
        meta["source"] = "lifecycle" if meta["issued_token_ids"] else "static"
    return out


SURFACE_KINDS = {"hermes_profiles": "hermes", "kanban_assignees": "kanban"}


def map_surfaces(
    host: str, surfaces: dict[str, Any] | None, *, mapping: dict[str, str], labels: set[str]
) -> list[dict[str, Any]]:
    """Map a node's reported surfaces to agent labels without guessing (task #1391, #1538).

    A surface maps through an explicit ``UA_SURFACE_MAP`` or built-in fleet
    default entry (``<host>/<kind>/<name>``), or when its name is exactly a known
    agent label. Anything else is ``unmapped``: similar names (``codex-dev``,
    ``desktop_agent2``) are not taken as ``Desktop_Agent2``.
    """
    out: list[dict[str, Any]] = []
    for field, kind in SURFACE_KINDS.items():
        names = (surfaces or {}).get(field) or []
        if not isinstance(names, list):
            continue
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            key = f"{host}/{kind}/{name}"
            if key in mapping:
                agent, how = mapping[key], "explicit"
            elif name in labels:
                agent, how = name, "exact"
            else:
                agent, how = None, "unmapped"
            out.append({"kind": kind, "name": name, "agent": agent, "mapping": how})
    return out


def _no_credential() -> dict[str, Any]:
    return {"issued_token_ids": [], "fingerprints": [], "source": "none"}


def _apply_agent(
    host_row: dict[str, Any],
    agent: dict[str, Any],
    now: datetime,
    *,
    host_source: str | None = None,
    credential: dict[str, Any] | None = None,
) -> None:
    """Attach an agent to a host row. A node row's liveness comes only from its
    own heartbeats, so agent activity never advances it."""
    host_row["agents"].append(
        {
            "label": agent.get("label"),
            "kind": agent.get("kind"),
            "status": agent.get("status"),
            "last_seen": agent.get("last_seen"),
            "last_seen_via": agent.get("last_seen_via"),
            "version": agent.get("version"),
            "active_tasks": agent.get("active_tasks") or 0,
            "host_source": host_source,
            "credential": credential or _no_credential(),
        }
    )
    if host_row.get("host_source") == "node":
        return
    last_seen = _newer(host_row.get("last_seen"), agent.get("last_seen"))
    if last_seen != host_row.get("last_seen"):
        host_row["last_seen"] = last_seen
        host_row["last_seen_via"] = agent.get("last_seen_via")
    if not host_row.get("version") and agent.get("version"):
        host_row["version"] = agent.get("version")
    host_row["as_of"] = _newer(host_row.get("as_of"), last_seen)
    host_row["last_seen_status"] = registry.derive_status(host_row.get("last_seen"), now=now)
    host_row["staleness"] = registry.derive_status(host_row.get("as_of"), now=now)


def _registered_nodes() -> dict[str, dict[str, Any]]:
    try:
        from . import node_registry

        return node_registry.list_nodes()
    except Exception:
        return {}


def _retired_node_names() -> set[str]:
    """Names of operator-retired nodes (task #2181); empty when unreadable."""
    try:
        from . import node_registry

        return set(node_registry.read_retired_nodes())
    except Exception:
        return set()


def _node_is_live(last_heartbeat: str | None, now: datetime) -> bool:
    """Use the node dispatch liveness rule, not agent registry status.

    The shared rule also bounds a future-dated heartbeat (task #1997)."""
    from .node_registry import heartbeat_is_live

    return heartbeat_is_live(last_heartbeat, now)


_STATUS_RANK = {"online": 0, "idle": 1, "stale": 2, "offline": 3, "unknown": 4}


def _grade_node_report_row(row: dict[str, Any], node_grade: str, now: datetime) -> None:
    """Re-grade a node host built from its ``host_reports`` row (task #1997).

    Registration and every node heartbeat write ``host_reports.last_seen`` with
    ``last_seen_via='ua_node'``: a mirror of ``nodes.last_heartbeat``, the value
    routing reads. That signal takes the shared node rule, graded from the
    ``nodes`` row so Devices and routing read the same timestamp. Hub-stamped
    reports (a doctor snapshot, a heartbeat_tool touch) are independent of the
    node and keep ``registry.derive_status``. Staleness is the freshest grade
    among them, so a future-dated node beat can no longer lift a host online.
    ``node_grade`` is ``node_registry.node_status`` of the ``nodes`` row.
    """
    node_sourced = row.get("last_seen_via") == "ua_node"
    if node_sourced:
        row["last_seen_status"] = node_grade
    independent = [] if node_sourced else [row.get("last_seen")]
    doctor = row.get("doctor") or {}
    independent.append(doctor.get("as_of"))
    grades = [node_grade] + [
        registry.derive_status(stamp, now=now) for stamp in independent if stamp
    ]
    row["staleness"] = min(grades, key=lambda g: _STATUS_RANK.get(g, len(_STATUS_RANK)))


def fleet_payload(*, now: datetime | None = None) -> dict[str, Any]:
    """Doctor snapshots and registered ua-nodes, with roster agents attached.

    Heartbeat-only rows and agent-label rows stay in ``host_reports``. They are
    omitted from this view and are not deleted. So is the row of a retired
    node's host (task #2196), unless that host is also an active node.
    """
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    local_host = config.current_source_host()
    hosts: dict[str, dict[str, Any]] = {}

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT host, last_seen, last_seen_via, version, doctor_as_of, "
            "doctor_json, executor_json, worktrees_json FROM host_reports "
            "ORDER BY host"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()

    nodes = _registered_nodes()
    node_names = set(nodes)
    # Task #2196: a retired node's host key leaves Devices too, including the
    # doctor snapshot stored under it. A name that is also an active node
    # (retired and brought back between these two reads) stays.
    retired_hosts = _retired_node_names() - node_names
    surface_mapping = config.surface_map()
    known_labels = (
        {label for label in _observed_agent_labels() if config.is_fleet_roster_label(label)}
        | config.agent_labels()
        | {info.get("agent") for info in nodes.values() if info.get("agent")}
    )

    for row in rows:
        host = row["host"]
        # Doctor snapshots and ua-nodes are the host rows. A hostname heartbeat
        # is stored and surfaces here once a doctor payload or node registration
        # lands on that same key. Agent labels recorded as heartbeat hosts, and
        # attribution sentences (parentheses), stay in the table and stay out
        # of this list. A spaced hostname such as "Mac hub" is a host.
        if _host_name_is_attribution_prose(host) and host not in node_names:
            continue
        if not row["doctor_as_of"] and host not in node_names:
            continue
        if host in retired_hosts:
            continue
        hosts[host] = _row_from_report(row, reference)

    # ua-node (task #1327): a registered node measured its own facts, so its row
    # is labeled "node" and the agent it is bound to groups under it instead of
    # under a Desktop_/Laptop_ identity-prefix guess.
    live_node_for_agent: dict[str, str] = {}
    last_node_for_agent: dict[str, str] = {}
    live_role_hosts: dict[str, set[str]] = {}
    from .node_registry import node_live_beat, node_status

    for name, info in sorted(nodes.items(), key=lambda kv: kv[1].get("last_heartbeat") or ""):
        agent_name = info.get("agent") or ""
        # Liveness reads the beat after the last retire/un-retire (#2196);
        # last_seen keeps the stored beat as history.
        live_beat = node_live_beat(info)
        # The agent list's status rule too, so the two never disagree (#2192).
        node_grade = node_status(info, reference)
        if agent_name:
            last_node_for_agent[agent_name] = name
            if _node_is_live(live_beat, reference):
                live_node_for_agent[agent_name] = name
                role = host_id_for_agent(agent_name)
                if role:
                    live_role_hosts.setdefault(role, set()).add(name)
        if name not in hosts:
            blank = _blank_host(name, host_source="node", now=reference)
            beat = info.get("last_heartbeat")
            if beat:
                blank["last_seen"] = beat
                blank["last_seen_via"] = "ua_node"
                blank["as_of"] = beat
                # The node liveness rule, not derive_status (task #1997).
                blank["last_seen_status"] = node_grade
                blank["staleness"] = blank["last_seen_status"]
            hosts[name] = blank
        else:
            # A host_reports row exists (real registration writes one): its
            # node-derived status takes the same rule as the blank row above.
            _grade_node_report_row(hosts[name], node_grade, reference)
        hosts[name]["host_source"] = "node"
        hosts[name]["node"] = info
        hosts[name]["surfaces"] = map_surfaces(
            name, info.get("surfaces"), mapping=surface_mapping, labels=known_labels
        )

    try:
        # Same source as agent_list: node-bound agents that have no registry row
        # are synthesized, and their liveness comes from the node heartbeat. A
        # registered node must not show an empty agent list on Devices.
        from .tools.registry import select_fleet_agents

        agents = select_fleet_agents(include_configured=True)
    except Exception:
        agents = []

    node_agents = set(last_node_for_agent)
    bindings = config.agent_hosts()
    credentials = _credentials()
    # A prefix is usable only if exactly one live same-prefix node exists.
    # Choosing the first node would silently move agents between devices.
    role_host = {
        role: next(iter(names)) for role, names in live_role_hosts.items() if len(names) == 1
    }
    resolved: list[dict[str, Any]] = []
    for agent in agents:
        label = agent.get("label") or ""
        if not (config.is_fleet_roster_label(label) or label in node_agents):
            continue
        unseen = not agent.get("last_seen") and label not in node_agents
        # Where an agent is (task #1388), strongest source first: a live ua-node
        # bound to the label; an explicit UA_AGENT_HOSTS binding; the host the
        # last authenticated request carried (registry.touch_agent); the last
        # node the label was bound to, now offline; and only then the label
        # prefix, which resolves solely to a host a same-prefix node already
        # occupies. The prefix never creates a host.
        candidates = (
            ("node", live_node_for_agent.get(label)),
            ("binding", bindings.get(label)),
            # select_fleet_agents synthesizes a host from a node heartbeat for
            # node-only agents. That is a last known node, not an independently
            # observed agent heartbeat.
            (
                "heartbeat",
                agent.get("host") if agent.get("last_seen_via") != "ua_node" else None,
            ),
            ("node_last", last_node_for_agent.get(label)),
            (
                "prefix",
                role_host.get(host_id_for_agent(label) or "") if not unseen else None,
            ),
        )
        host_id, source = None, None
        for src, value in candidates:
            if value and value.strip():
                host_id, source = value.strip(), src
                break
        if host_id == "hub" and local_host in hosts:
            host_id = local_host
        credential = credentials.get(label) or _no_credential()
        placed = bool(host_id) and host_id in hosts
        resolved.append(
            {
                "label": label,
                "host": host_id if placed else None,
                "host_source": source if placed else None,
                "unplaced_host": None if placed else host_id,
                "status": agent.get("status"),
                "credential": credential,
            }
        )
        if placed:
            _apply_agent(
                hosts[host_id], agent, reference, host_source=source, credential=credential
            )

    ordered = sorted(
        hosts.values(),
        key=lambda item: (
            0 if item["host"] == local_host else 1,
            item["host"],
        ),
    )
    return {
        "as_of": storage.now(),
        "local_host": local_host,
        "hosts": ordered,
        # One answer to "which device does each agent run on, with what
        # credential id" (task #1388). host is None when nothing places it on a
        # known host row; unplaced_host keeps what was claimed, unverified.
        "agents": sorted(resolved, key=lambda a: a["label"]),
    }
