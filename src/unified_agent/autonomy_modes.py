"""Autonomy modes: operator control over standing involvement, per mission (task #1457).

See ``docs/autonomy-modes.md`` for the full contract. Summary: a **mission**
(a goal id -- ``goals.py``) has one of four modes:

* ``observe``       -- human sees everything live; nothing pauses beyond
                        existing gates.
* ``exception``      -- quiet; only failures, denials and budget warnings
                        surface.
* ``checkpoint``      -- pause at declared checkpoints (merge / deploy /
                        external write by default) for human ack.
* ``action-review``   -- every side-effecting action needs approval.

Modes are ordered loosest (``observe``) to strictest (``action-review``) in
``MODES``. A mission with no explicit mode defaults to the strictest
(``action-review``): the safe default is full oversight until a human
actively loosens it. An **org policy ceiling**, read read-only from the same
policy file ``policy.py``/``budgets.py`` read (``UA_POLICY_FILE``, key
``autonomy_ceiling``), bounds how *loose* a mission mode may be set -- a
mission mode must be stricter-or-equal to the ceiling, never looser. Missing
config means the least strict ceiling (``observe``): that adds no new
restriction, it does not loosen anything that was not already unrestricted.

Central invariant, enforced in :func:`_decide` (the shared core of
:func:`evaluate` and pending reevaluation): **no mode grants new privileges
or removes an explicit gate, and every gate fails CLOSED.**

1. Pure reads (``READ_ONLY_ACTION_CLASSES`` / a ``read_`` prefix) always
   proceed once the two gates below clear -- they never touch the approval
   gate or the mode's pause logic.
2. #1351 agent lifecycle (``agent_lifecycle.agent_state``) -- a paused or
   terminated actor is denied in every mode. An unreadable lifecycle state
   (the call raises) is ALSO a denial, never a silent "assume active".
3. #1348 budgets (``budgets.budget_status``) -- an exhausted ``block`` budget
   denies in every mode. A malformed budgets policy or an unreadable budget
   store (the call raises, or the result carries an ``error`` key) is ALSO a
   denial, never a silent "assume no budgets".
4. The existing global approval gate (``tools/approvals.py``), read-only --
   this module never inserts, consumes or revokes an approval row:
   * No approval decision exists yet for a gated action class -> **pause**,
     in every mode, never proceed. A missing pass is not silent permission.
   * An explicit denial -> **deny**, in every mode.
   * A live approved-unconsumed pass satisfies THIS gate -- but a global
     pass never overrides the mission's own review requirement (below).
5. Only now does the mode's own pause/proceed policy apply. When step 4
   found a live approved pass AND the mode would still pause, the result is
   **pause, reason "mission review required"** -- the approval authorized
   the action globally; it did not waive this mission's independent review.

Mode changes -- #1431 propose-on/apply-gated semantics. A human with
authority may change a mission's mode directly (:func:`set_mode`). An agent
may only **propose** one (:func:`propose_mode`): the proposal is parked as a
normal request in the EXISTING global approval queue
(``tools/approvals.check_approval_gate``, ``force=True`` so it is gated
regardless of ``UA_APPROVAL_TOOLS``) -- no second store, no separate chat
channel. A human resolves it with the existing ``approval_resolve``; a
subsequent call to :func:`apply_proposal` consumes that one-shot pass and
applies the mode change, attributing it to the human who actually resolved
the approval row (never to the proposing agent and never to whoever happens
to call ``apply_proposal``) -- this is the "truthful human vs
policy-authorized attribution" the task asks for. Validation (mission
exists, mode is well-formed, the ceiling still allows it, the approver is
human) runs BEFORE the pass is consumed, and a mutation failure after
consumption restores the pass, so a validation failure never stds an
approved proposal that was never actually applied.

Identity binding (task #1457 Devin RED finding). Every function below that
takes an ``actor`` -- :func:`set_mode`, :func:`grant_standing`,
:func:`revoke_grant`, :func:`propose_mode`, :func:`evaluate` -- resolves the
TRUE actor through :func:`_bound_actor` before doing anything else. A
verified caller (``config.verified_agent_identity()``) is always its own
actor; a caller-supplied label naming a DIFFERENT identity is refused
outright, mirroring ``approval_resolve``'s posture (a caller-supplied human
name cannot stand in for authentication). With no verified identity,
``config.enforce_auth()`` decides whether the caller-supplied label is even
honored. ``goal_manage``'s ``_actor()`` helper in ``goals.py`` does no
binding of its own any more -- it just forwards whatever it was given, and
this module is the single place that decides who the actor really is.

Attribution vocabulary (stamped on every revision and every evaluate()
decision):

* ``human-approved``   -- a named human directly set the mode, approved an
                          agent's proposal that was then applied, or holds a
                          live approval pass that a mode did not additionally
                          require review on top of.
* ``policy-authorized`` -- the mode itself, a fail-closed gate, or a
                          mission-review requirement on top of an approval
                          (not a specific new human decision for THIS action)
                          decided the outcome.
* ``standing-grant``    -- a human-granted per-(mission, action_class)
                          exemption from the mode's default pause, still
                          subject to every pre-existing gate above.
* ``proposal-pending``  -- an agent's mode-change proposal is parked in the
                          approval queue, not yet resolved.

Revocation / tightening. Revoking a standing grant (:func:`revoke_grant`) or
setting a stricter mode (:func:`set_mode`) re-evaluates every **not-yet-
started** pending action recorded for that mission (:func:`_reevaluate_pending_locked`)
by rerunning the FULL :func:`_decide` chain -- lifecycle, budget, approval
gate, and mode -- not only the mode layer, so the pending ledger cannot show
a decision that contradicts a gate that would deny/pause it right now.
"Not started" is tracked with :func:`mark_started`, which only the pending
row's OWNING actor may call: a caller that begins executing a
proceed-decision should mark it started so a later tightening does not
retroactively contest work already in flight (only *pending* work is
re-decided, per the task's acceptance criteria).

Storage. This module owns five tables (``autonomy_mission_mode``,
``autonomy_mode_revisions``, ``autonomy_pending_actions``,
``autonomy_standing_grants``, ``autonomy_action_reviews``). Their
DDL lives in ``table_schemas.autonomy_modes``, which ``storage._migrate`` runs
during database initialization; the legacy grant migration is the
``autonomy_modes.legacy_grants`` post-init hook (``post_init``), which runs
once the schema is in place. :func:`ensure_schema` runs both. Request connections never
run schema creation, migration, or an implicit commit. There is no
``SCHEMA_VERSION`` bump. Standing grants are normalized into their own table
with a ``PRIMARY KEY (mission_id, action_class)`` rather than a JSON list:
atomic INSERT/DELETE avoids losing concurrent grant updates. A partial
unique index on ``autonomy_pending_actions`` permits one not-started decision
per mission, action class, actor and immutable canonical action context.
Initialization upgrades legacy contexts and reviews, and atomically migrates
pre-normalization JSON grants while clearing their source column. Repeated
initialization cannot resurrect a revoked normalized grant.

Tenancy (task #1978). The four mission-keyed tables carry a nullable
``org_id`` stamped at insert from ``edition.tenant_org_id()``; every read and
write by mission or pending-action id is fenced with ``org_id IS <tenant>``,
and :func:`_require_mission_tenant` makes another tenant's mission read as a
missing goal. The untenanted private hub keeps ``org_id IS NULL`` throughout.

Explicit seams for undecided work (do not build ahead of the decision):

* **#1407** (agent self-merge ledger decision) is not decided. Nothing here
  assumes its outcome. The seam is ``attribution`` itself: when #1407 lands,
  its ledger decision slots in as a new attribution value (e.g.
  ``ledger-authorized``) alongside ``human-approved``/``policy-authorized``,
  without changing the :func:`_decide` control flow.
* **#1421** trust presets add tightening-only policy and filesystem
  restrictions. They do not bundle standing grants or change involvement
  modes. Native execution under a preset requires verified provider
  containment (#1355); see ``docs/trust-presets.md``.

"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import agent_lifecycle, budgets, config, edition, events, storage, table_schemas
from .tools import approvals as approvals_mod

CHANNEL = "agent.autonomy_mode"

#: The approval-queue "tool" name under which agent-proposed mode changes are
#: parked (task #1431 propose-on/apply-gated). Not a real MCP tool -- just a
#: label in the existing ``approvals`` table, exactly like any gated tool.
PROPOSE_TOOL = "autonomy_mode_change"

#: Loosest first. Strictness increases with index.
MODES: tuple[str, ...] = ("observe", "exception", "checkpoint", "action-review")
_STRICTNESS = {mode: index for index, mode in enumerate(MODES)}

#: A mission that has never had its mode set explicitly starts at the
#: strictest mode -- full oversight -- not at the org ceiling (which only
#: bounds looseness, it is not a default).
DEFAULT_MISSION_MODE = "action-review"

#: The ceiling when the policy file has no ``autonomy_ceiling`` key: the
#: LEAST strict mode. Missing config adds no new restriction.
DEFAULT_CEILING = "observe"

#: Action classes that pause under ``checkpoint`` mode unless a mission has an
#: explicit standing grant for them. A documented default, not a special
#: table -- #1421 trust presets may make this configurable later.
DEFAULT_CHECKPOINT_ACTION_CLASSES = frozenset({"merge", "deploy", "external_write"})

#: Action classes that are pure reads: they proceed in every mode and never
#: touch the approval gate or the mode's pause logic, but still respect the
#: fail-closed lifecycle/budget deny gates (task #1457 Devin ANALYSIS
#: finding: ``action-review`` should not pause a routine status check). A
#: caller opts in either by exact membership here or by a ``read_`` prefix.
READ_ONLY_ACTION_CLASSES = frozenset(
    {"read", "read_status", "status", "list", "get", "preview", "inspect"}
)

ATTRIBUTIONS = ("human-approved", "policy-authorized", "standing-grant", "proposal-pending")
DECISIONS = ("proceed", "pause_for_human", "deny")


def _is_read_only_action_class(action_class: str) -> bool:
    return action_class in READ_ONLY_ACTION_CLASSES or action_class.startswith("read_")


# --- schema (DDL in table_schemas; legacy grants via a post-init hook) --------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the autonomy tables, then migrate any legacy JSON grants.

    The DDL lives in ``table_schemas.autonomy_modes`` (core), which
    ``storage.init_db`` runs; the grant migration is the
    ``autonomy_modes.legacy_grants`` post-init hook (see ``post_init``). This
    wrapper runs both, for a caller that sets up this module's tables alone.
    """
    table_schemas.autonomy_modes(conn)
    migrate_legacy_grants(conn)


def migrate_legacy_grants(conn: sqlite3.Connection) -> None:
    """Post-init hook: move pre-normalization JSON grants into their table.

    Serialized with ``BEGIN IMMEDIATE`` so concurrent startup connections
    cannot race the backfill. A failure rolls back every grant and leaves the
    legacy source column intact for the next initialization to retry.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        _backfill_standing_grants(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _backfill_standing_grants(conn: sqlite3.Connection) -> None:
    """Idempotently migrate any pre-normalization JSON grants (task #1457).

    Earlier standing grants lived in ``autonomy_mission_mode.standing_grants``
    as JSON. This runs only during initialization, inside
    migrate_legacy_grants's write transaction. Each grant and the cleared source column commit
    together; a failed migration leaves the legacy source intact for retry.
    """
    rows = conn.execute(
        "SELECT mission_id, standing_grants, updated_by, updated_at FROM autonomy_mission_mode "
        "WHERE standing_grants IS NOT NULL AND standing_grants != '[]'"
    ).fetchall()
    if not rows:
        return
    for row in rows:
        try:
            grants = json.loads(row["standing_grants"])
        except (TypeError, ValueError):
            continue
        if not isinstance(grants, list):
            continue
        for action_class in grants:
            conn.execute(
                "INSERT OR IGNORE INTO autonomy_standing_grants "
                "(mission_id, action_class, granted_by, granted_at) VALUES (?, ?, ?, ?)",
                (row["mission_id"], str(action_class), row["updated_by"], row["updated_at"]),
            )
        conn.execute(
            "UPDATE autonomy_mission_mode SET standing_grants='[]' WHERE mission_id=?",
            (row["mission_id"],),
        )


def _connect() -> sqlite3.Connection:
    return storage.connect()


# --- validation --------------------------------------------------------------


def _validate_mode(mode: str) -> str:
    text = (mode or "").strip().lower()
    if text not in MODES:
        raise ToolError(f"mode must be one of {MODES}, got {mode!r}")
    return text


def _validate_action_class(action_class: str) -> str:
    text = (action_class or "").strip().lower()
    if not text:
        raise ToolError("action_class is required")
    if len(text) > 100:
        raise ToolError("action_class must be at most 100 characters")
    return text


def _require_actor(actor: str | None) -> str:
    text = (actor or "").strip()
    if not text:
        raise ToolError("actor is required")
    return text


def _same_identity(left: str, right: str) -> bool:
    """Mirrors ``tools.approvals._same_person`` (private to that module),
    duplicated locally for the same reason :func:`_approved_pass_live`
    duplicates TTL arithmetic: never import a leading-underscore helper
    across modules. Two labels are the same identity, including human
    aliases (the configured login label and operator name).
    """
    if not left or not right:
        return False
    if left == right:
        return True
    for canonical, aliases in config.identity_alias_allowlist().items():
        group = {canonical, *aliases}
        if left in group and right in group:
            return True
    return False


def _bound_actor(actor: str | None) -> str:
    """The TRUE actor for a direct call into this module (task #1457 Devin
    RED finding: a direct caller could supply any label it liked).

    A verified caller (``config.verified_agent_identity()``) is always its
    own actor -- mirrors ``approval_resolve``'s binding. A caller-supplied
    label naming a DIFFERENT identity is refused outright, never silently
    substituted and never silently ignored, so a verified agent cannot claim
    to be a human by passing ``actor=HUMAN``. With no verified identity,
    ``config.enforce_auth()`` decides whether the caller-supplied label is
    honored at all: unauthenticated plus enforced means no label can stand
    in for authentication.
    """
    verified = config.verified_agent_identity()
    supplied = (actor or "").strip()
    if verified:
        if supplied and not _same_identity(supplied, verified):
            raise ToolError(
                f"caller is verified as {verified!r}; a different actor "
                f"({supplied!r}) cannot be claimed through a parameter"
            )
        return verified
    if config.enforce_auth():
        raise ToolError("autonomy actions require a verified identity")
    return _require_actor(supplied)


def _require_human(actor: str, *, verb: str) -> None:
    kind = config.actor_kind_for_identity(actor)
    if kind == "human":
        return
    raise ToolError(
        f"{actor!r} is not a human identity (resolved kind: {kind!r}); agents may only "
        f"PROPOSE a mode change (autonomy_propose), never {verb} one directly -- #1431 "
        "propose-on/apply-gated semantics. A human applies it with autonomy_set/autonomy_apply."
    )


def _require_mission(mission_id: int) -> None:
    from .goals import get_goal

    if get_goal(int(mission_id)) is None:
        raise ToolError(f"unknown mission (goal): {mission_id}")
    conn = storage.connect()
    try:
        _require_mission_tenant(conn, int(mission_id))
    finally:
        conn.close()


def _require_mission_tenant(conn: sqlite3.Connection, mission_id: int) -> None:
    """Refuse a mission whose autonomy rows belong to another tenant (task #1978).

    Goals carry no tenant, so a mission belongs to the tenant whose rows it
    already has; untenanted (NULL) rows are a tenant of their own. Another
    tenant's mission reads exactly like a missing goal. Writers call this
    again inside their write transaction, so two tenants cannot both claim a
    fresh mission between the check and the insert.
    """
    org = edition.tenant_org_id()
    for table in table_schemas.AUTONOMY_MISSION_TABLES:
        if not _table_exists(conn, table):
            continue
        foreign = conn.execute(
            f"SELECT 1 FROM {table} WHERE mission_id=? AND org_id IS NOT ? LIMIT 1",
            (mission_id, org),
        ).fetchone()
        if foreign is not None:
            raise ToolError(f"unknown mission (goal): {mission_id}")


# --- org ceiling (read-only from the policy file; no new table) -------------


def org_ceiling() -> str:
    """Least-strict mode any mission may be set to, read from ``policy.yaml``.

    Reads the SAME file ``policy.py``/``budgets.py`` read
    (``config.policy_file_path()``, env ``UA_POLICY_FILE``), a top-level
    ``autonomy_ceiling: <mode>`` scalar, purely read-only -- this module never
    writes that file. A missing file or missing key means no ceiling has been
    configured, which defaults to the LEAST strict mode (``observe``): that is
    not a loosening, it is simply "no organizational restriction yet". A
    malformed value fails closed (``ToolError``), matching
    ``budgets.load_budgets``'s posture on its own file.
    """
    path = config.policy_file_path()
    if not path.exists():
        return DEFAULT_CEILING
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - fail closed, like budgets.load_budgets
        raise ToolError(f"cannot read autonomy_ceiling from {path}: {exc}") from exc
    if not data:
        return DEFAULT_CEILING
    if not isinstance(data, dict):
        raise ToolError(f"policy configuration at {path} must be a YAML mapping")
    raw = data.get("autonomy_ceiling")
    if raw is None:
        return DEFAULT_CEILING
    ceiling = str(raw).strip().lower()
    if ceiling not in MODES:
        raise ToolError(f"policy 'autonomy_ceiling' must be one of {MODES}, got {raw!r}")
    return ceiling


def _refuse_looser_than_ceiling(mode: str, ceiling: str) -> None:
    if _STRICTNESS[mode] < _STRICTNESS[ceiling]:
        raise ToolError(
            f"mode {mode!r} is looser than the org ceiling {ceiling!r}; a mission mode may "
            "only be stricter-or-equal to the ceiling, never looser (scope escape denied)"
        )


# --- mode storage --------------------------------------------------------------


def _row_mode(conn: sqlite3.Connection, mission_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM autonomy_mission_mode WHERE mission_id=? AND org_id IS ?",
        (mission_id, edition.tenant_org_id()),
    ).fetchone()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _effective_mode(mode: str) -> str:
    ceiling = org_ceiling()
    return max((mode, ceiling), key=_STRICTNESS.__getitem__)


def profile_default_mode(
    mission_id: int, actor: str, action_context: dict[str, Any] | None = None
) -> str | None:
    """The operating-profile autonomy default for a mission without an explicit mode.

    With a ``task_id`` in the action context (a merge), that task's newest
    run snapshot decides, provided the task serves this mission. Otherwise the
    actor's live runs on the mission's tasks decide, strictest first; a run
    without a profile autonomy counts as :data:`DEFAULT_MISSION_MODE`. No run,
    or any read failure, returns None (the strict default stays).
    """
    from . import operating_profiles

    context_task = (action_context or {}).get("task_id")
    modes: list[str] = []
    org = edition.tenant_org_id()
    try:
        conn = storage.connect()
        try:
            if context_task is not None:
                # Another tenant's task never sets this mission's mode (#1978).
                linked = conn.execute(
                    "SELECT 1 FROM task_goal_links l JOIN tasks t ON t.id=l.task_id "
                    "WHERE l.task_id=? AND l.goal_id=? AND t.org_id IS ?",
                    (int(context_task), int(mission_id), org),
                ).fetchone()
                task_ids = [int(context_task)] if linked else []
                finished = True
            else:
                rows = conn.execute(
                    "SELECT t.id FROM tasks t JOIN task_goal_links l ON l.task_id=t.id "
                    "WHERE l.goal_id=? AND t.claimed_by=? AND t.status='doing' AND t.org_id IS ?",
                    (int(mission_id), actor, org),
                ).fetchall()
                task_ids = [int(r["id"]) for r in rows]
                finished = False
            for task_id in task_ids:
                snap = operating_profiles.run_snapshot(conn, task_id, finished=finished)
                mode = operating_profiles.autonomy_default(snap)
                modes.append(mode if mode in MODES else DEFAULT_MISSION_MODE)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - unreadable profile keeps the strict default
        return None
    if not modes:
        return None
    return max(modes, key=_STRICTNESS.__getitem__)


def _grants_for(conn: sqlite3.Connection, mission_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT action_class FROM autonomy_standing_grants WHERE mission_id=? AND org_id IS ? "
        "ORDER BY action_class",
        (mission_id, edition.tenant_org_id()),
    ).fetchall()
    return [r["action_class"] for r in rows]


def _canonical_action_context(value: dict[str, Any] | None) -> str:
    """Bounded JSON metadata identifying an action, never an authority input."""
    if value is None:
        return "null"
    if not isinstance(value, dict):
        raise ToolError("autonomy action context must be a JSON mapping")
    nodes = 0

    def validate(item: Any, depth: int = 0) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 256 or depth > 8:
            raise ToolError("autonomy action context exceeds structural limits")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise ToolError("autonomy action context requires bounded string keys")
                validate(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ToolError("autonomy action context contains a non-JSON value")

    validate(value)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        if len(encoded.encode("utf-8")) > 16384:
            raise ToolError("autonomy action context exceeds 16384 bytes")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ToolError("autonomy action context is not valid JSON") from exc
    return encoded


def _stored_action_context(row: sqlite3.Row) -> dict[str, Any] | None:
    # Read-only previews can precede migration of an older pending table.
    raw = row["action_context"] if "action_context" in row.keys() else "null"
    try:
        value = json.loads(raw)
        if _canonical_action_context(value) != raw:
            raise ValueError("noncanonical stored context")
        return value
    except (ValueError, TypeError) as exc:
        raise ToolError("stored autonomy action context is invalid") from exc


def _action_approval_payload(
    mission_id: int, action_class: str, actor: str, context: dict[str, Any] | None
) -> dict[str, Any]:
    payload = {"mission_id": mission_id, "action_class": action_class, "actor": actor}
    if context is not None:
        payload["action_context"] = context
    return payload


def action_approval_payload(
    mission_id: int, action_class: str, actor: str, action_context: dict[str, Any] | None
) -> dict[str, Any]:
    """The exact global-approval payload :func:`evaluate` looks up for one action.

    A caller whose action class is in ``UA_APPROVAL_TOOLS`` requests this payload
    (``check_approval_gate(..., request_only=True)``) so a human has a pass to
    approve before the mission review can be acknowledged.
    """
    context = json.loads(_canonical_action_context(action_context))
    return _action_approval_payload(int(mission_id), action_class, actor, context)


def _public_revision(row: sqlite3.Row) -> dict[str, Any]:
    result = {key: row[key] for key in row.keys() if key != "action_context"}
    if "action_context" in row.keys():
        context = _stored_action_context(row)
        if context is not None:
            result["action_context"] = context
    return result


def get_mode(mission_id: int) -> dict[str, Any]:
    """Current mode, standing grants, org ceiling and revision history."""
    mission_id = int(mission_id)
    _require_mission(mission_id)
    conn = storage.connect()
    try:
        row = _row_mode(conn, mission_id) if _table_exists(conn, "autonomy_mission_mode") else None
        grants = (
            _grants_for(conn, mission_id) if _table_exists(conn, "autonomy_standing_grants") else []
        )
        if row and row["standing_grants"] not in (None, "[]"):
            try:
                legacy_grants = json.loads(row["standing_grants"])
            except (TypeError, ValueError):
                legacy_grants = []
            if isinstance(legacy_grants, list):
                grants = sorted(set(grants) | {str(grant) for grant in legacy_grants})
        revisions = (
            conn.execute(
                "SELECT * FROM autonomy_mode_revisions WHERE mission_id=? AND org_id IS ? "
                "ORDER BY id DESC LIMIT 100",
                (mission_id, edition.tenant_org_id()),
            ).fetchall()
            if _table_exists(conn, "autonomy_mode_revisions")
            else []
        )
    finally:
        conn.close()
    ceiling = org_ceiling()
    return {
        "mission_id": mission_id,
        "mode": row["mode"] if row else DEFAULT_MISSION_MODE,
        "effective_mode": _effective_mode(row["mode"] if row else DEFAULT_MISSION_MODE),
        "explicit": row is not None,
        "standing_grants": grants,
        "org_ceiling": ceiling,
        "updated_at": row["updated_at"] if row else None,
        "updated_by": row["updated_by"] if row else None,
        "revisions": [_public_revision(r) for r in revisions],
    }


def set_mode(
    mission_id: int,
    mode: str,
    actor: str,
    reason: str | None = None,
    *,
    approval_id: int | None = None,
) -> dict[str, Any]:
    """Human-only. Set a mission's mode; always attributed ``human-approved``.

    An agent calling this directly is refused (#1431): it must use
    :func:`propose_mode` instead, and a human applies it via
    :func:`apply_proposal` (which calls :func:`_set_mode_core` with the
    resolving human as ``actor``, bypassing THIS function's identity binding
    since the approval row already proved who authorized it).
    """
    actor = _bound_actor(actor)
    return _set_mode_core(mission_id, mode, actor, reason, approval_id=approval_id)


def _set_mode_core(
    mission_id: int,
    mode: str,
    actor: str,
    reason: str | None,
    *,
    approval_id: int | None = None,
) -> dict[str, Any]:
    mission_id = int(mission_id)
    mode = _validate_mode(mode)
    actor = _require_actor(actor)
    _require_human(actor, verb="apply")
    _require_mission(mission_id)
    ceiling = org_ceiling()
    _refuse_looser_than_ceiling(mode, ceiling)
    conn = _connect()
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        from_mode, revision_id, reevaluated = _write_mode_locked(
            conn, mission_id, mode, actor, reason, approval_id
        )
        conn.commit()
    finally:
        conn.close()
    return _mode_result(
        mission_id, mode, actor, reason, approval_id, ceiling, from_mode, revision_id, reevaluated
    )


def _write_mode_locked(conn, mission_id, mode, actor, reason, approval_id):
    _require_mission_tenant(conn, mission_id)
    org = edition.tenant_org_id()
    current = _row_mode(conn, mission_id)
    from_mode = current["mode"] if current else DEFAULT_MISSION_MODE
    now = storage.now()
    conn.execute(
        "INSERT INTO autonomy_mission_mode "
        "(mission_id, mode, standing_grants, updated_at, updated_by, org_id) "
        "VALUES (?, ?, '[]', ?, ?, ?) "
        "ON CONFLICT(mission_id) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by WHERE autonomy_mission_mode.org_id IS excluded.org_id",
        (mission_id, mode, now, actor, org),
    )
    cur = conn.execute(
        "INSERT INTO autonomy_mode_revisions "
        "(mission_id, from_mode, to_mode, actor, attribution, reason, approval_id, created_at, "
        "org_id) VALUES (?, ?, ?, ?, 'human-approved', ?, ?, ?, ?)",
        (mission_id, from_mode, mode, actor, reason, approval_id, now, org),
    )
    reevaluated = _reevaluate_pending_locked(conn, mission_id)
    return from_mode, int(cur.lastrowid), reevaluated


def _mode_result(
    mission_id, mode, actor, reason, approval_id, ceiling, from_mode, revision_id, reevaluated
):
    events.record_event(
        CHANNEL,
        payload={
            "mission_id": mission_id,
            "from_mode": from_mode,
            "to_mode": mode,
            "actor": actor,
            "attribution": "human-approved",
            "reason": reason,
            "revision_id": revision_id,
            "approval_id": approval_id,
        },
        originator=actor,
        notify=False,
    )
    return {
        "mission_id": mission_id,
        "mode": mode,
        "from_mode": from_mode,
        "attribution": "human-approved",
        "revision_id": revision_id,
        "org_ceiling": ceiling,
        "reevaluated_pending": reevaluated,
    }


def grant_standing(
    mission_id: int, action_class: str, actor: str, reason: str | None = None
) -> dict[str, Any]:
    """Human-only. Exempt ``action_class`` from the mode's default pause.

    A standing grant only ever affects the MODE layer of :func:`_decide` --
    it can never bypass the pre-existing lifecycle, budget or approval gates
    checked before the mode is even consulted ("no mode grants new
    privileges" holds for grants too). Stored as an atomic INSERT against
    ``autonomy_standing_grants``'s ``PRIMARY KEY (mission_id, action_class)``
    -- see the module docstring on why this replaced a JSON list column.
    """
    mission_id = int(mission_id)
    action_class = _validate_action_class(action_class)
    actor = _bound_actor(actor)
    _require_human(actor, verb="grant")
    _require_mission(mission_id)
    conn = _connect()
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        _require_mission_tenant(conn, mission_id)
        org = edition.tenant_org_id()
        row = _row_mode(conn, mission_id)
        mode = row["mode"] if row else DEFAULT_MISSION_MODE
        now = storage.now()
        try:
            conn.execute(
                "INSERT INTO autonomy_standing_grants "
                "(mission_id, action_class, granted_by, granted_at, org_id) VALUES (?, ?, ?, ?, ?)",
                (mission_id, action_class, actor, now, org),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            raise ToolError(
                f"mission {mission_id} already has a standing grant for {action_class!r}"
            ) from None
        conn.execute(
            "INSERT INTO autonomy_mode_revisions "
            "(mission_id, from_mode, to_mode, actor, attribution, reason, approval_id, created_at, "
            "org_id) VALUES (?, ?, ?, ?, 'human-approved', ?, NULL, ?, ?)",
            (
                mission_id,
                mode,
                mode,
                actor,
                reason or f"granted standing exemption: {action_class}",
                now,
                org,
            ),
        )
        reevaluated = _reevaluate_pending_locked(conn, mission_id)
        conn.commit()
        grants = _grants_for(conn, mission_id)
    finally:
        conn.close()
    events.record_event(
        CHANNEL,
        payload={"mission_id": mission_id, "granted_standing": action_class, "actor": actor},
        originator=actor,
        notify=False,
    )
    return {
        "mission_id": mission_id,
        "granted_standing": action_class,
        "standing_grants": grants,
        "reevaluated_pending": reevaluated,
    }


def revoke_grant(
    mission_id: int, action_class: str, actor: str, reason: str | None = None
) -> dict[str, Any]:
    """Human-only. Withdraw a standing grant; re-evaluates pending work for the mission."""
    mission_id = int(mission_id)
    action_class = _validate_action_class(action_class)
    actor = _bound_actor(actor)
    _require_human(actor, verb="revoke")
    _require_mission(mission_id)
    conn = _connect()
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        _require_mission_tenant(conn, mission_id)
        org = edition.tenant_org_id()
        deleted = conn.execute(
            "DELETE FROM autonomy_standing_grants WHERE mission_id=? AND action_class=? "
            "AND org_id IS ?",
            (mission_id, action_class, org),
        )
        if deleted.rowcount == 0:
            conn.rollback()
            raise ToolError(
                f"mission {mission_id} has no standing grant for {action_class!r} to revoke"
            )
        row = _row_mode(conn, mission_id)
        mode = row["mode"] if row else DEFAULT_MISSION_MODE
        now = storage.now()
        conn.execute(
            "INSERT INTO autonomy_mode_revisions "
            "(mission_id, from_mode, to_mode, actor, attribution, reason, approval_id, created_at, "
            "org_id) VALUES (?, ?, ?, ?, 'human-approved', ?, NULL, ?, ?)",
            (
                mission_id,
                mode,
                mode,
                actor,
                reason or f"revoked standing grant: {action_class}",
                now,
                org,
            ),
        )
        reevaluated = _reevaluate_pending_locked(conn, mission_id)
        conn.commit()
        grants = _grants_for(conn, mission_id)
    finally:
        conn.close()
    events.record_event(
        CHANNEL,
        payload={"mission_id": mission_id, "revoked_standing": action_class, "actor": actor},
        originator=actor,
        notify=False,
    )
    return {
        "mission_id": mission_id,
        "revoked_standing": action_class,
        "standing_grants": grants,
        "reevaluated_pending": reevaluated,
    }


# --- agent proposals (#1431 propose-on/apply-gated) --------------------------


def propose_mode(
    mission_id: int, mode: str, actor: str, reason: str | None = None
) -> dict[str, Any]:
    """Agent-only. Park a mode-change request in the EXISTING approval queue.

    No new store: this calls ``tools.approvals.check_approval_gate`` with
    ``force=True`` (gated regardless of ``UA_APPROVAL_TOOLS``) and
    ``request_only=True`` (never raises ``ApprovalPendingError`` -- the
    proposal's whole point is to return a handle, not block). A human sees it
    in the same ``approval_pending()`` queue as everything else and resolves
    it with the existing ``approval_resolve``; :func:`apply_proposal` then
    consumes the pass and applies it.
    """
    mission_id = int(mission_id)
    mode = _validate_mode(mode)
    actor = _bound_actor(actor)
    if config.actor_kind_for_identity(actor) == "human":
        raise ToolError(
            f"{actor!r} is a human identity; call autonomy_set directly instead of proposing"
        )
    _require_mission(mission_id)
    ceiling = org_ceiling()
    _refuse_looser_than_ceiling(mode, ceiling)
    payload = {
        "mission_id": mission_id,
        "to_mode": mode,
        "proposed_by": actor,
        "reason": reason,
    }
    gate = approvals_mod.check_approval_gate(
        PROPOSE_TOOL, payload, requested_by=actor, force=True, request_only=True
    )
    return {
        "mission_id": mission_id,
        "proposed_mode": mode,
        "proposed_by": actor,
        "approval_id": gate.get("approval_id"),
        "status": gate.get("status", "pending"),
        "attribution": "proposal-pending",
    }


def apply_proposal(approval_id: int) -> dict[str, Any]:
    """Apply an agent's approved mode-change proposal (#1431 apply-gated).

    Validates the mission, mode, ceiling and approver BEFORE consuming the
    one-shot pass (task #1457 Devin BUG finding: consuming first could strand
    an approved proposal that a later validation failure -- a deleted
    mission, a ceiling tightened after approval -- prevents from ever being
    applied). Consumes the pass exactly like any other gated call, then calls
    :func:`_set_mode_core` with the RESOLVING HUMAN as actor -- never the
    proposing agent, never whoever happens to call this function -- and
    restores the pass if that mutation still fails for some other reason
    (e.g. a concurrent write), rather than leaving a burned, unapplied pass.
    """
    approval_id = int(approval_id)
    row = approvals_mod.get_approval(approval_id)
    if row is None or row.get("tool") != PROPOSE_TOOL:
        raise ToolError(f"no autonomy mode-change proposal with approval id {approval_id}")
    status = row.get("status")
    if status == "denied":
        raise ToolError(
            f"proposal {approval_id} was denied: {row.get('reason') or 'no reason given'}"
        )
    if status != "approved":
        raise ToolError(
            f"proposal {approval_id} is {status!r}, not 'approved'; a human must resolve it "
            "with approval_resolve first"
        )
    resolved_by = row.get("resolved_by")
    if not resolved_by:
        raise ToolError(
            f"proposal {approval_id} has no recorded approver; cannot attribute the change"
        )
    payload = row.get("payload") or {}
    mission_id = int(payload["mission_id"])
    mode = str(payload["to_mode"])
    reason = (
        payload.get("reason") or f"applied proposal {approval_id} from {payload.get('proposed_by')}"
    )

    # Validate everything BEFORE consuming the pass.
    mode = _validate_mode(mode)
    _require_mission(mission_id)
    _require_human(resolved_by, verb="apply")
    ceiling = org_ceiling()
    _refuse_looser_than_ceiling(mode, ceiling)

    conn = _connect()
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        # Another tenant's approved proposal reads as gone (task #1793).
        locked = conn.execute(
            "SELECT * FROM approvals WHERE id=? AND org_id IS ?",
            (approval_id, edition.tenant_org_id()),
        ).fetchone()
        if locked is None or locked["tool"] != PROPOSE_TOOL or locked["status"] != "approved":
            raise ToolError(f"proposal {approval_id} is no longer approved")
        if json.loads(locked["payload"]) != payload or locked["resolved_by"] != resolved_by:
            raise ToolError(f"proposal {approval_id} changed while applying")
        if not _approved_pass_live(dict(locked)):
            raise ToolError(f"proposal {approval_id} approval pass has expired")
        _require_mission(mission_id)
        _refuse_looser_than_ceiling(mode, org_ceiling())
        consumed = conn.execute(
            "UPDATE approvals SET status='consumed', updated_at=? WHERE id=? AND status='approved'",
            (storage.now(), approval_id),
        )
        if consumed.rowcount != 1:
            raise ToolError(f"proposal {approval_id} could not be consumed")
        from_mode, revision_id, reevaluated = _write_mode_locked(
            conn, mission_id, mode, resolved_by, reason, approval_id
        )
        conn.commit()
    finally:
        conn.close()
    return _mode_result(
        mission_id,
        mode,
        resolved_by,
        reason,
        approval_id,
        ceiling,
        from_mode,
        revision_id,
        reevaluated,
    )


# --- effective-policy preview -------------------------------------------------


def preview(mission_id: int) -> dict[str, Any]:
    """Effective policy for a mission: mode, ceiling, checkpoints, grants, pending work."""
    mission_id = int(mission_id)
    state = get_mode(mission_id)
    conn = storage.connect()
    try:
        pending = (
            conn.execute(
                "SELECT * FROM autonomy_pending_actions WHERE mission_id=? AND started=0 "
                "AND org_id IS ? ORDER BY id",
                (mission_id, edition.tenant_org_id()),
            ).fetchall()
            if _table_exists(conn, "autonomy_pending_actions")
            else []
        )
        has_reviews = _table_exists(conn, "autonomy_action_reviews")
        review_has_approval_id = has_reviews and any(
            column["name"] == "approval_id"
            for column in conn.execute("PRAGMA table_info(autonomy_action_reviews)")
        )
        live_pending = []
        for row in pending:
            review = (
                conn.execute(
                    "SELECT approval_id FROM autonomy_action_reviews WHERE pending_id=?",
                    (row["id"],),
                ).fetchone()
                if review_has_approval_id
                else None
            )
            action_context = _stored_action_context(row)
            reviewed = _review_matches_gate(
                review, mission_id, row["action_class"], row["actor"], action_context
            )
            if (
                has_reviews
                and not review_has_approval_id
                and row["action_class"] not in config.approval_gated_tools()
            ):
                reviewed = (
                    conn.execute(
                        "SELECT 1 FROM autonomy_action_reviews WHERE pending_id=?", (row["id"],)
                    ).fetchone()
                    is not None
                )
            decision, attribution, reason, action_mode = _decide(
                mission_id,
                row["action_class"],
                row["actor"],
                state={**state, "reviewed": reviewed, "action_context": action_context},
            )
            # The mission-level mode is only the stored fallback: a run's
            # operating profile can set a different mode for this action
            # (task #1756), so each pending action reports its own.
            live_pending.append(
                {
                    **_public_revision(row),
                    "decision": decision,
                    "attribution": attribution,
                    "reason": reason,
                    "effective_mode": action_mode,
                    "checkpoint_action_classes": (
                        sorted(
                            DEFAULT_CHECKPOINT_ACTION_CLASSES
                            - set(state.get("standing_grants") or [])
                        )
                        if action_mode == "checkpoint"
                        else []
                    ),
                }
            )
    finally:
        conn.close()
    grants = set(state.get("standing_grants") or [])
    checkpoints = (
        sorted(DEFAULT_CHECKPOINT_ACTION_CLASSES - grants)
        if state["effective_mode"] == "checkpoint"
        else []
    )
    return {
        **state,
        # ``mode``/``effective_mode`` here are the mission's stored fallback;
        # a profile-derived mode is per action (see pending_not_started).
        "effective_mode_scope": "mission-fallback",
        "checkpoint_action_classes": checkpoints,
        "pending_not_started": live_pending,
        "invariants": [
            "no mode grants new privileges or removes an explicit gate",
            "cancellation, budget hard stops, and revoked/tightened grants are "
            "enforceable in every mode",
            "a missing approval pass pauses in every mode; it never proceeds",
            "a global approval pass never overrides this mission's own review requirement",
            "agents may only propose a mode change; a human applies it (#1431 "
            "propose-on/apply-gated)",
            "pending decisions are re-derived from the full gate chain (lifecycle, "
            "budget, approval, mode), not only the mode",
        ],
    }


# --- evaluate: existing gates first, then the mode ---------------------------


def _approved_pass_live(row: dict[str, Any]) -> bool:
    """Mirrors ``approvals._pass_expires_at`` (private to that module) for a
    read-only check here: never import a leading-underscore helper across
    modules, so the TTL arithmetic is duplicated deliberately, in one place.
    """
    ts = row.get("resolved_at") or row.get("created_at")
    if not ts:
        return False
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    expires_at = parsed + timedelta(hours=config.approval_pass_ttl_hours())
    return datetime.now(UTC) < expires_at


def _read_only_approval_lookup(tool: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Read-only view of an existing approval decision for ``(tool, payload)``.

    Deliberately never inserts a pending row, consumes a pass, or otherwise
    mutates the approval queue -- :func:`_decide` must not loosen or spend
    the existing gate, only read what it has already decided. Returns
    ``None`` when there is no live decision yet -- the caller must then pause
    (task #1457 Devin RED finding: a missing pass is not permission).
    """
    canon = approvals_mod.canonical_approval_payload(payload)
    conn = storage.connect()
    try:
        latest = conn.execute(
            "SELECT * FROM approvals WHERE tool=? AND payload=? "
            "AND status IN ('approved', 'denied') AND org_id IS ? ORDER BY id DESC LIMIT 1",
            (tool, canon, edition.tenant_org_id()),
        ).fetchone()
        if latest is None:
            return None
        if latest["status"] == "denied":
            return {
                "status": "denied",
                "approval_id": int(latest["id"]),
                "reason": latest["reason"] or "no reason given",
            }
        approved_d = {key: latest[key] for key in latest.keys()}
        if _approved_pass_live(approved_d):
            return {"status": "approved_unconsumed", "approval_id": int(approved_d["id"])}
        return None
    finally:
        conn.close()


def _mode_decision(mode: str, grants: set[str], action_class: str) -> tuple[str, str, str]:
    """The MODE layer's own pause/proceed policy, with no other gate involved."""
    if mode == "observe":
        return "proceed", "policy-authorized", "observe mode: nothing pauses beyond existing gates"
    if mode == "exception":
        return (
            "proceed",
            "policy-authorized",
            "exception mode: proceeds quietly; only failures, denials and budget warnings surface",
        )
    if mode == "checkpoint":
        if action_class in DEFAULT_CHECKPOINT_ACTION_CLASSES and action_class not in grants:
            return (
                "pause_for_human",
                "policy-authorized",
                f"checkpoint mode: {action_class!r} is a declared checkpoint",
            )
        attribution = "standing-grant" if action_class in grants else "policy-authorized"
        suffix = " (standing grant)" if action_class in grants else ""
        return "proceed", attribution, f"checkpoint mode: not a declared checkpoint{suffix}"
    # action-review
    if action_class in grants:
        return (
            "proceed",
            "standing-grant",
            "action-review mode: explicit standing grant on this action class",
        )
    return (
        "pause_for_human",
        "policy-authorized",
        "action-review mode: every side-effecting action needs approval",
    )


def _decide(
    mission_id: int,
    action_class: str,
    actor: str,
    *,
    state: dict[str, Any] | None = None,
) -> tuple[str, str, str, str]:
    """The FULL gate chain: read-only short-circuit, then #1351 lifecycle,
    then #1348 budgets (both fail-closed), then the approval gate, then the
    mode. Returns ``(decision, attribution, reason, mode)`` -- pure with
    respect to the pending ledger; callers (:func:`evaluate` and
    :func:`_reevaluate_pending_locked`) persist the result themselves. Shared
    so a mode/grant change re-derives the SAME effective decision a fresh
    :func:`evaluate` call would give right now (task #1457 Devin ANALYSIS
    finding), not only what the mode layer alone would say.
    """
    if state is None:
        state = get_mode(mission_id)
    base_mode = state["mode"]
    if not state.get("explicit", True):
        # No explicit mission mode: the run's operating profile sets the
        # default (task #1756). An explicit mode is never replaced, and the
        # org ceiling below still tightens whichever applies.
        base_mode = (
            profile_default_mode(mission_id, actor, state.get("action_context")) or base_mode
        )
    mode = _effective_mode(base_mode)
    grants = set(state.get("standing_grants") or [])

    # 1. #1351 agent lifecycle -- fail closed (a stopped OR unreadable actor
    # is denied in every mode; a read error is never a silent "assume active").
    try:
        lifecycle_state = agent_lifecycle.agent_state(actor).get("state")
    except Exception as exc:  # noqa: BLE001 - the failure itself becomes the denial reason
        return (
            "deny",
            "policy-authorized",
            f"agent lifecycle state unavailable ({exc}); denied in every mode (fail closed, #1351)",
            mode,
        )
    if lifecycle_state in ("paused", "terminated", "retired"):
        return (
            "deny",
            "policy-authorized",
            f"actor {actor!r} is {lifecycle_state} (#1351 agent lifecycle stop); enforced in every mode",
            mode,
        )

    # 2. #1348 budgets -- fail closed (a malformed policy or an unreadable
    # budget store is denied, never treated as "no budgets configured").
    try:
        budget = budgets.budget_status(actor)
    except Exception as exc:  # noqa: BLE001 - the failure itself becomes the denial reason
        return (
            "deny",
            "policy-authorized",
            f"budget status unavailable ({exc}); denied in every mode (fail closed)",
            mode,
        )
    if isinstance(budget, dict) and budget.get("error"):
        return (
            "deny",
            "policy-authorized",
            f"budget policy fails closed: {budget['error']}; denied in every mode",
            mode,
        )
    if not isinstance(budget, dict) or not isinstance(budget.get("budgets"), list):
        return "deny", "policy-authorized", "budget status is malformed; denied in every mode", mode
    warning = False
    for entry in budget["budgets"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("agents_status"), list):
            return (
                "deny",
                "policy-authorized",
                "budget status is malformed; denied in every mode",
                mode,
            )
        for agent_row in entry["agents_status"]:
            if not isinstance(agent_row, dict):
                return (
                    "deny",
                    "policy-authorized",
                    "budget status is malformed; denied in every mode",
                    mode,
                )
            if agent_row.get("agent") == actor and agent_row.get("state") == "warning":
                warning = True
            if (
                entry.get("action") == "block"
                and agent_row.get("agent") == actor
                and agent_row.get("state") == "exhausted"
            ):
                return (
                    "deny",
                    "policy-authorized",
                    f"budget {entry.get('name')!r} hard stop for {actor!r}; enforced in every mode",
                    mode,
                )

    # 3. Pure reads proceed once the fail-closed gates above are clear --
    # they never touch the approval gate or the mode's own pause logic.
    if (
        _is_read_only_action_class(action_class)
        and action_class not in config.approval_gated_tools()
    ):
        return (
            "proceed",
            "policy-authorized",
            "read-only action class proceeds in every mode (lifecycle/budget gates still apply)",
            mode,
        )

    mode_decision, mode_attribution, mode_reason = _mode_decision(mode, grants, action_class)
    if state.get("reviewed") and mode_decision == "pause_for_human":
        mode_decision = "proceed"
        mode_attribution = "human-approved"
        mode_reason = "human acknowledged this pending action"
    if warning:
        mode_reason = f"{mode_reason}; budget warning"

    # 4. The existing global approval gate, read-only.
    if action_class in config.approval_gated_tools():
        looked_up = _read_only_approval_lookup(
            action_class,
            _action_approval_payload(mission_id, action_class, actor, state.get("action_context")),
        )
        if looked_up is None:
            # No approval decision exists yet: pause, in every mode. A
            # missing pass is not permission (task #1457 Devin RED finding).
            return (
                "pause_for_human",
                "policy-authorized",
                "approval-gated action class has no approval pass yet; pause in every "
                "mode until a human decides",
                mode,
            )
        if looked_up["status"] == "denied":
            return (
                "deny",
                "policy-authorized",
                f"approval gate denied ({looked_up['reason']}); enforced in every mode",
                mode,
            )
        # A live approved-unconsumed pass satisfies THIS gate -- but it never
        # overrides the mission's own review requirement (task #1457 Devin
        # RED finding: "global approval skips mission review").
        if mode_decision == "pause_for_human":
            return (
                "pause_for_human",
                "policy-authorized",
                f"mission review required (global approval {looked_up['approval_id']} satisfies "
                f"the approval gate, but {mode!r} mode still requires review)",
                mode,
            )
        return (
            "proceed",
            "human-approved",
            f"live human-approved pass {looked_up['approval_id']}; "
            f"{mode!r} mode permits proceeding",
            mode,
        )

    # 5. No approval gate applies to this action class: the mode decides alone.
    return mode_decision, mode_attribution, mode_reason, mode


def _decision(
    mission_id: int,
    action_class: str,
    actor: str,
    decision: str,
    *,
    reason: str,
    attribution: str,
    surfaced: bool,
    conn: sqlite3.Connection,
    action_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upsert one outstanding action for its mission, class, actor and context.

    Canonical JSON is part of the partial unique key, so retries converge on
    one immutable action while distinct executions cannot share its review.
    Context-free callers retain their original one-row-per-triple behavior.
    """
    now = storage.now()
    row = conn.execute(
        "INSERT INTO autonomy_pending_actions "
        "(mission_id, action_class, actor, decision, reason, attribution, started, "
        "created_at, updated_at, action_context, org_id) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?) "
        "ON CONFLICT(mission_id, action_class, actor, action_context) WHERE started=0 DO UPDATE SET "
        "decision=excluded.decision, reason=excluded.reason, attribution=excluded.attribution, "
        "updated_at=excluded.updated_at "
        "WHERE autonomy_pending_actions.org_id IS excluded.org_id RETURNING id",
        (
            mission_id,
            action_class,
            actor,
            decision,
            reason,
            attribution,
            now,
            now,
            _canonical_action_context(action_context),
            edition.tenant_org_id(),
        ),
    ).fetchone()
    if row is None:
        # Only another tenant's open row can block the upsert, and evaluate
        # refuses that mission first; never write into or report that row.
        raise ToolError(f"unknown mission (goal): {mission_id}")
    pending_id = int(row["id"])
    return {
        "mission_id": mission_id,
        "action_class": action_class,
        "actor": actor,
        "decision": decision,
        "reason": reason,
        "attribution": attribution,
        "surfaced": surfaced,
        "pending_id": pending_id,
        **({"action_context": action_context} if action_context is not None else {}),
    }


def evaluate(
    mission_id: int, action_class: str, actor: str, *, action_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Decide whether ``actor`` may take ``action_class`` for ``mission_id``.

    Returns ``{proceed|pause_for_human|deny, reason, attribution, surfaced,
    mission_id, action_class, actor, pending_id}``. See the module docstring
    and :func:`_decide` for the gate order. Every gate read is read-only and
    fails closed; this function never loosens, inserts into, or consumes any
    of the pre-existing gates -- it can only add a pause on top of what they
    already decided, or defer to the mode when they decided nothing.
    """
    mission_id = int(mission_id)
    action_class = _validate_action_class(action_class)
    action_context = json.loads(_canonical_action_context(action_context))
    actor = _bound_actor(actor)
    _require_mission(mission_id)
    conn = _connect()
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        _require_mission_tenant(conn, mission_id)
        state = _decision_state(
            conn, mission_id, action_class, actor, action_context=action_context
        )
        decision, attribution, reason, mode = _decide(mission_id, action_class, actor, state=state)
        surfaced = not (mode == "exception" and decision == "proceed") or _budget_warning(actor)
        result = _decision(
            mission_id,
            action_class,
            actor,
            decision,
            reason=reason,
            attribution=attribution,
            surfaced=surfaced,
            conn=conn,
            action_context=action_context,
        )
        conn.commit()
        return result
    finally:
        conn.close()


def preview_action(
    mission_id: int,
    action_class: str,
    actor: str,
    *,
    action_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read one exact current action decision without migrating or recording it.

    An existing review is usable only for the same immutable context. This
    preview never marks work started and is not an execution authorization;
    actual side effects still require evaluate and mark_started at their edge.
    """
    mission_id = int(mission_id)
    action_class = _validate_action_class(action_class)
    encoded = _canonical_action_context(action_context)
    action_context = json.loads(encoded)
    actor = _bound_actor(actor)
    mission = preview(mission_id)
    matching = next(
        (
            row
            for row in mission["pending_not_started"]
            if row["action_class"] == action_class
            and row["actor"] == actor
            and _canonical_action_context(row.get("action_context")) == encoded
        ),
        None,
    )
    if matching is None:
        decision, attribution, reason, mode = _decide(
            mission_id,
            action_class,
            actor,
            state={**mission, "reviewed": False, "action_context": action_context},
        )
    else:
        decision = matching["decision"]
        attribution = matching["attribution"]
        reason = matching["reason"]
        mode = matching["effective_mode"]
    return {
        "mission_id": mission_id,
        "action_class": action_class,
        "actor": actor,
        "decision": decision,
        "reason": reason,
        "attribution": attribution,
        "surfaced": not (mode == "exception" and decision == "proceed") or _budget_warning(actor),
        "pending_id": int(matching["id"]) if matching else None,
        **({"action_context": action_context} if action_context is not None else {}),
    }


def _decision_state(
    conn: sqlite3.Connection,
    mission_id: int,
    action_class: str,
    actor: str,
    pending_id: int | None = None,
    *,
    action_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = _row_mode(conn, mission_id)
    if pending_id is None:
        pending = conn.execute(
            "SELECT id FROM autonomy_pending_actions WHERE mission_id=? AND action_class=? "
            "AND actor=? AND action_context=? AND started=0 AND org_id IS ?",
            (
                mission_id,
                action_class,
                actor,
                _canonical_action_context(action_context),
                edition.tenant_org_id(),
            ),
        ).fetchone()
        pending_id = int(pending["id"]) if pending else None
    if pending_id is not None:
        pending_row = conn.execute(
            "SELECT * FROM autonomy_pending_actions WHERE id=? AND org_id IS ?",
            (pending_id, edition.tenant_org_id()),
        ).fetchone()
        if pending_row is None:
            raise ToolError("unknown pending autonomy action")
        action_context = _stored_action_context(pending_row)
    review = (
        conn.execute(
            "SELECT approval_id FROM autonomy_action_reviews WHERE pending_id=?", (pending_id,)
        ).fetchone()
        if pending_id
        else None
    )
    reviewed = _review_matches_gate(review, mission_id, action_class, actor, action_context)
    return {
        "mode": row["mode"] if row else DEFAULT_MISSION_MODE,
        "explicit": row is not None,
        "standing_grants": _grants_for(conn, mission_id),
        "reviewed": reviewed,
        "action_context": action_context,
    }


def _review_matches_gate(
    review,
    mission_id: int,
    action_class: str,
    actor: str,
    action_context: dict[str, Any] | None = None,
) -> bool:
    if review is None:
        return False
    if action_class not in config.approval_gated_tools():
        return review["approval_id"] is None
    gate = _read_only_approval_lookup(
        action_class,
        _action_approval_payload(mission_id, action_class, actor, action_context),
    )
    return bool(
        gate
        and gate["status"] == "approved_unconsumed"
        and gate["approval_id"] == review["approval_id"]
    )


def _budget_warning(actor: str) -> bool:
    try:
        status = budgets.budget_status(actor)
        return any(
            row.get("agent") == actor and row.get("state") == "warning"
            for budget in status.get("budgets", [])
            for row in budget.get("agents_status", [])
        )
    except Exception:  # noqa: BLE001 - warning display is advisory after the gate check
        return False


def _reevaluate_pending_locked(conn: sqlite3.Connection, mission_id: int) -> list[dict[str, Any]]:
    """Re-derive the FULL gate-chain decision for every not-started pending
    action (task #1457 Devin ANALYSIS finding: rerun lifecycle, budget,
    approval and mode -- not only the mode -- so the pending ledger never
    shows a decision a fresh :func:`evaluate` call would immediately
    contradict). Called (on the same connection/transaction) right after a
    mode change or a grant revoke/grant, so a tightened mode, a withdrawn
    grant, or e.g. a lifecycle pause imposed in between is reflected on
    already-recorded pending work, not only on the next fresh call.
    """
    pending = conn.execute(
        "SELECT * FROM autonomy_pending_actions WHERE mission_id=? AND started=0 AND org_id IS ?",
        (mission_id, edition.tenant_org_id()),
    ).fetchall()
    now = storage.now()
    flipped: list[dict[str, Any]] = []
    for item in pending:
        decision, attribution, reason, _mode = _decide(
            mission_id,
            item["action_class"],
            item["actor"],
            state=_decision_state(
                conn, mission_id, item["action_class"], item["actor"], int(item["id"])
            ),
        )
        if (
            decision != item["decision"]
            or attribution != item["attribution"]
            or reason != item["reason"]
        ):
            conn.execute(
                "UPDATE autonomy_pending_actions SET decision=?, attribution=?, reason=?, updated_at=? "
                "WHERE id=? AND org_id IS ?",
                (
                    decision,
                    attribution,
                    reason,
                    now,
                    item["id"],
                    edition.tenant_org_id(),
                ),
            )
            flipped.append(
                {
                    "pending_id": int(item["id"]),
                    "action_class": item["action_class"],
                    "actor": item["actor"],
                    "from_decision": item["decision"],
                    "to_decision": decision,
                }
            )
    return flipped


def mark_started(
    pending_id: int,
    actor: str | None = None,
    *,
    action_context: dict[str, Any] | None = None,
    _admission_check: Callable[[sqlite3.Connection], None] | None = None,
    _start_execution: Callable[[sqlite3.Connection], None] | None = None,
) -> dict[str, Any]:
    """Mark a pending action as started; only the pending row's OWNING actor
    may do so (task #1457 Devin BUG finding), so a later mode tightening does
    not retroactively contest work already in flight (only pending work is
    re-decided by :func:`revoke_grant` / :func:`set_mode`).

    A trusted execution boundary can additionally recheck its stored authority
    on the owning transaction connection. This internal callback only adds
    denials after the normal gates; it is never supplied by tool/body input.
    A trusted bounded first mutation can run before the write fence releases;
    authority is rechecked before commit. Once execution is attempted, failures
    retain the started evidence instead of restoring an unspent action.
    """
    pending_id = int(pending_id)
    expected_context = _canonical_action_context(action_context)
    actor = _bound_actor(actor)
    conn = _connect()
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM autonomy_pending_actions WHERE id=? AND org_id IS ?",
            (pending_id, edition.tenant_org_id()),
        ).fetchone()
        if row is None:
            raise ToolError(f"unknown pending autonomy action: {pending_id}")
        if row["actor"] != actor:
            raise ToolError(
                f"pending action {pending_id} belongs to {row['actor']!r}, not {actor!r}; "
                "only the owning actor may mark it started"
            )
        if _canonical_action_context(_stored_action_context(row)) != expected_context:
            raise ToolError("pending autonomy action context does not match this execution")
        if row["started"]:
            raise ToolError(
                f"pending action {pending_id} already started; only a proceed decision may start once"
            )
        current, attribution, reason, _ = _decide(
            int(row["mission_id"]),
            row["action_class"],
            actor,
            state=_decision_state(
                conn, int(row["mission_id"]), row["action_class"], actor, pending_id
            ),
        )
        if (current, attribution, reason) != (row["decision"], row["attribution"], row["reason"]):
            conn.execute(
                "UPDATE autonomy_pending_actions SET decision=?, attribution=?, reason=?, "
                "updated_at=? WHERE id=? AND org_id IS ?",
                (current, attribution, reason, storage.now(), pending_id, edition.tenant_org_id()),
            )
            row = conn.execute(
                "SELECT * FROM autonomy_pending_actions WHERE id=? AND org_id IS ?",
                (pending_id, edition.tenant_org_id()),
            ).fetchone()
        if row["decision"] != "proceed":
            # Refusal must retain the current gate evidence without consuming
            # the action. Raising before commit would restore a stale proceed.
            conn.commit()
            raise ToolError(
                f"pending action {pending_id} is {row['decision']!r}; only a proceed decision may start"
            )
        if _admission_check is not None:
            try:
                _admission_check(conn)
            except Exception as exc:  # noqa: BLE001 - unreadable admission must fail closed
                reason = (
                    f"execution admission denied: {exc}"
                    if isinstance(exc, ToolError)
                    else "execution admission could not be verified"
                )
                conn.execute(
                    "UPDATE autonomy_pending_actions SET decision='deny', "
                    "attribution='policy-authorized', reason=?, updated_at=? "
                    "WHERE id=? AND org_id IS ?",
                    (reason, storage.now(), pending_id, edition.tenant_org_id()),
                )
                conn.commit()
                if isinstance(exc, ToolError):
                    raise
                raise ToolError(reason) from exc
        conn.execute(
            "UPDATE autonomy_pending_actions SET started=1, updated_at=? WHERE id=? AND org_id IS ?",
            (storage.now(), pending_id, edition.tenant_org_id()),
        )
        if _start_execution is not None:
            try:
                _start_execution(conn)
                if _admission_check is not None:
                    _admission_check(conn)
            except BaseException:
                # Git may already have mutated. Persist that the action began;
                # the enclosing invocation must keep its production pass spent.
                conn.commit()
                raise
        conn.commit()
        row = conn.execute(
            "SELECT * FROM autonomy_pending_actions WHERE id=? AND org_id IS ?",
            (pending_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    return _public_revision(row)


def review_action(pending_id: int, actor: str) -> dict[str, Any]:
    """Human acknowledgment for one exact, not-started pending action."""
    pending_id = int(pending_id)
    actor = _bound_actor(actor)
    _require_human(actor, verb="review")
    conn = _connect()
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM autonomy_pending_actions WHERE id=? AND started=0 AND org_id IS ?",
            (pending_id, edition.tenant_org_id()),
        ).fetchone()
        if row is None:
            raise ToolError(f"unknown not-started pending autonomy action: {pending_id}")
        state = _decision_state(
            conn, int(row["mission_id"]), row["action_class"], row["actor"], pending_id
        )
        current, _, _, _ = _decide(
            int(row["mission_id"]), row["action_class"], row["actor"], state=state
        )
        if current != "pause_for_human":
            raise ToolError("only a currently paused action may be acknowledged")
        approval_id = None
        if row["action_class"] in config.approval_gated_tools():
            gate = _read_only_approval_lookup(
                row["action_class"],
                _action_approval_payload(
                    row["mission_id"],
                    row["action_class"],
                    row["actor"],
                    _stored_action_context(row),
                ),
            )
            if gate is None or gate["status"] != "approved_unconsumed":
                raise ToolError("global approval must be live before mission review")
            approval_id = gate["approval_id"]
        conn.execute(
            "INSERT INTO autonomy_action_reviews (pending_id, reviewed_by, reviewed_at, approval_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(pending_id) DO UPDATE SET "
            "reviewed_by=excluded.reviewed_by, reviewed_at=excluded.reviewed_at, "
            "approval_id=excluded.approval_id",
            (pending_id, actor, storage.now(), approval_id),
        )
        decision, attribution, reason, _ = _decide(
            int(row["mission_id"]),
            row["action_class"],
            row["actor"],
            state=_decision_state(
                conn, int(row["mission_id"]), row["action_class"], row["actor"], pending_id
            ),
        )
        conn.execute(
            "UPDATE autonomy_pending_actions SET decision=?, attribution=?, reason=?, "
            "updated_at=? WHERE id=? AND org_id IS ?",
            (decision, attribution, reason, storage.now(), pending_id, edition.tenant_org_id()),
        )
        conn.commit()
        return {
            "pending_id": pending_id,
            "decision": decision,
            "attribution": attribution,
            "reviewed_by": actor,
        }
    finally:
        conn.close()
