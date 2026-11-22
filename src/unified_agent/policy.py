"""Policy Engine DSL and Causal Episode Guardrails (task #156 + Phase 3 / #1031).

Parses ~/.config/agent_hub/policy.yaml (or env UA_POLICY_FILE) and enforces
policy evaluation rules against event_log in memory.db.

Phase 1 (task #156) shipped episode-centric rules:
  cooldown / max_calls_per_episode / fatigue_band_cap

Phase 3 (task #1031) adds declarative YAML rule kinds on the same file:
  tool       — allow / deny / require_approval for matched tools
  role       — only named profiles or agent identities may invoke the tool
  threshold  — deny or require_approval when a payload field crosses a bound

Task #1357 adds call-time gateway rules on the same file. They do not change
who can see a tool (that stays the profile list):
  rate_limit — per-tool, per-caller cap inside a bounded window
  bind       — company / agent / project / task scope; deny beats allow

A deny writes ``agent.gateway.decision`` with a reason code. With no gateway
rules configured, evaluation matches the previous allow/deny behavior.

FAIL CLOSED: If an episode record is missing, invalid, or database operations
fail during a policy check, policy evaluation fails closed and raises
PolicyViolation. ``require_approval`` reuses the existing approval queue with
``force=True``; ``ApprovalPendingError`` propagates unchanged so callers can
park for human review, while unexpected approval-path errors become
PolicyViolation.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import config, gateway, storage, trust_presets

logger = logging.getLogger("unified_agent.policy")

_VALID_ACTIONS = frozenset({"allow", "deny", "require_approval"})
_LEGACY_RULE_TYPES = frozenset({"cooldown", "max_calls_per_episode", "fatigue_band_cap"})
_PHASE3_RULE_TYPES = frozenset({"tool", "role", "threshold", "rate_limit"})


class PolicyViolation(ToolError):
    """Raised when a tool invocation violates policy rules or fails closed."""

    def __init__(self, message: str, *, reason_code: str | None = None):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass
class PolicyRule:
    name: str
    description: str = ""
    tool: str = "*"
    type: str = "max_calls_per_episode"
    max_calls: int | None = None
    cooldown_seconds: float | None = None
    bands: dict[str, int] | None = None
    require_episode: bool = False
    # Phase 3 (#1031)
    action: str = "deny"
    roles: list[str] = field(default_factory=list)
    field_path: str | None = None
    max_value: float | None = None
    min_value: float | None = None
    window_seconds: float | None = None
    bind: dict[str, str] = field(default_factory=dict)


@dataclass
class PolicyConfig:
    version: int = 1
    enabled: bool = True
    default_action: str = "allow"
    rules: list[PolicyRule] = field(default_factory=list)
    trust_presets: trust_presets.TrustPresetConfig = field(
        default_factory=trust_presets.TrustPresetConfig
    )


DEFAULT_POLICY_RULES = [
    PolicyRule(
        name="fatigue_intervention_cap",
        description="Cap fatigue interventions per episode",
        tool="fatigue_intervention",
        type="fatigue_band_cap",
        require_episode=True,
        bands={"green": 5, "yellow": 3, "red": 1},
    ),
    PolicyRule(
        name="focus_anchor_stabilization",
        description="45s stabilization cooldown on set_focus_anchor()",
        tool="set_focus_anchor",
        type="cooldown",
        cooldown_seconds=45.0,
    ),
    PolicyRule(
        name="realignment_limit",
        description="Single-response realignment limit per episode",
        tool="realignment",
        type="max_calls_per_episode",
        max_calls=1,
        require_episode=True,
    ),
]


def _parse_bind(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PolicyViolation(f"policy rule 'bind' must be a mapping, got {type(raw).__name__}")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key not in gateway.BIND_KEYS:
            raise PolicyViolation(
                f"policy rule bind key {key!r} is not one of {sorted(gateway.BIND_KEYS)}"
            )
        if isinstance(value, bool) or value is None:
            raise PolicyViolation(f"policy rule bind '{key}' must be a string or integer")
        text = str(value).strip()
        if not text:
            raise PolicyViolation(f"policy rule bind '{key}' must be non-empty")
        out[str(key)] = text
    return out


def _parse_rate_max_calls(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise PolicyViolation(
            f"policy rule 'max_calls' for rate_limit must be an integer, got {raw!r}"
        )
    if raw < 1 or raw > gateway.MAX_CALLS:
        raise PolicyViolation(
            f"policy rule 'max_calls' for rate_limit must be from 1 to {gateway.MAX_CALLS}, "
            f"got {raw}"
        )
    return raw


def _parse_roles(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    raise PolicyViolation(f"policy rule 'roles' must be a string or list, got {type(raw).__name__}")


def _parse_optional_float(raw: Any, *, key: str) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PolicyViolation(f"policy rule '{key}' must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise PolicyViolation(
            f"policy rule '{key}' must be a finite number, got {raw!r} (non-finite bounds "
            "are a misconfiguration, not a permissive rule)"
        )
    return value


def load_policy(file_path: Path | None = None) -> PolicyConfig:
    """Load and parse policy.yaml DSL configuration.

    Fails closed (raises PolicyViolation) if the configuration file is present
    but unparseable or malformed.
    """
    path = file_path if file_path is not None else config.policy_file_path()
    if not path.exists():
        return PolicyConfig(
            version=1, enabled=True, default_action="allow", rules=list(DEFAULT_POLICY_RULES)
        )

    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise PolicyViolation(f"Policy configuration at {path} must be a YAML mapping")
    except Exception as e:
        if isinstance(e, PolicyViolation):
            raise
        raise PolicyViolation(f"Failed to load or parse policy YAML at {path}: {e}") from e

    try:
        preset_config = (
            trust_presets.parse_config(data["trust_presets"])
            if "trust_presets" in data
            else trust_presets.TrustPresetConfig()
        )
    except ToolError as exc:
        raise PolicyViolation(f"Invalid trust_presets configuration: {exc}") from exc
    enabled = bool(data.get("enabled", True))
    default_action = str(data.get("default_action", "allow"))
    if default_action not in ("allow", "deny"):
        raise PolicyViolation(
            f"Policy default_action must be 'allow' or 'deny', got {default_action!r}"
        )
    version = int(data.get("version", 1))

    raw_rules = data.get("rules", [])
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise PolicyViolation(f"Policy 'rules' must be a list at {path}")

    rules: list[PolicyRule] = []
    rate_limit_names: set[str] = set()
    for item in raw_rules:
        if not isinstance(item, dict):
            raise PolicyViolation("Each policy rule must be a YAML mapping")
        rule_type = str(item.get("type", "max_calls_per_episode"))
        if rule_type not in _LEGACY_RULE_TYPES | _PHASE3_RULE_TYPES:
            raise PolicyViolation(
                f"Unknown policy rule type {rule_type!r}; "
                f"supported: {sorted(_LEGACY_RULE_TYPES | _PHASE3_RULE_TYPES)}"
            )
        # Role rules are allow-lists: omitted action means allow matched roles.
        # Tool/threshold default to deny on match / breach.
        if "action" in item and item["action"] is not None:
            action = str(item["action"])
        elif rule_type == "role":
            action = "allow"
        else:
            action = "deny"
        if action not in _VALID_ACTIONS:
            raise PolicyViolation(
                f"Policy rule action must be one of {sorted(_VALID_ACTIONS)}, got {action!r}"
            )
        # Accept both `field` (YAML) and legacy aliases; store as field_path.
        field_path = item.get("field")
        if field_path is not None:
            field_path = str(field_path).strip() or None
        max_calls = item.get("max_calls")
        if rule_type == "rate_limit":
            max_calls = _parse_rate_max_calls(max_calls)
        rule = PolicyRule(
            name=str(item.get("name", "unnamed_rule")),
            description=str(item.get("description", "")),
            tool=str(item.get("tool", "*")),
            type=rule_type,
            max_calls=max_calls,
            cooldown_seconds=_parse_optional_float(
                item.get("cooldown_seconds"), key="cooldown_seconds"
            ),
            bands=item.get("bands"),
            require_episode=bool(item.get("require_episode", False)),
            action=action,
            roles=_parse_roles(item.get("roles")),
            field_path=field_path,
            max_value=_parse_optional_float(item.get("max"), key="max"),
            min_value=_parse_optional_float(item.get("min"), key="min"),
            window_seconds=_parse_optional_float(item.get("window_seconds"), key="window_seconds"),
            bind=_parse_bind(item.get("bind")),
        )
        if rule.type == "threshold" and not rule.field_path:
            raise PolicyViolation(
                f"Policy rule '{rule.name}' of type 'threshold' requires a 'field' path"
            )
        if rule.type == "threshold" and rule.max_value is None and rule.min_value is None:
            raise PolicyViolation(
                f"Policy rule '{rule.name}' of type 'threshold' requires 'max' and/or 'min'"
            )
        if rule.type == "role" and not rule.roles:
            raise PolicyViolation(
                f"Policy rule '{rule.name}' of type 'role' requires a non-empty 'roles' list"
            )
        if rule.type == "rate_limit":
            if rule.window_seconds is None:
                raise PolicyViolation(
                    f"Policy rule '{rule.name}' of type 'rate_limit' requires 'window_seconds'"
                )
            if rule.window_seconds < 1 or rule.window_seconds > gateway.MAX_WINDOW_SECONDS:
                raise PolicyViolation(
                    f"Policy rule '{rule.name}' window_seconds must be from 1 to "
                    f"{gateway.MAX_WINDOW_SECONDS:g}s, got {rule.window_seconds!r}"
                )
            if rule.action != "deny":
                raise PolicyViolation(
                    f"Policy rule '{rule.name}' of type 'rate_limit' denies when the "
                    "window is exhausted"
                )
            if rule.name in rate_limit_names:
                raise PolicyViolation(
                    f"Policy rule name '{rule.name}' is used by more than one rate_limit "
                    "rule; rate-limit budgets are keyed by name"
                )
            rate_limit_names.add(rule.name)
        rules.append(rule)

    return PolicyConfig(
        version=version,
        enabled=enabled,
        default_action=default_action,
        rules=rules,
        trust_presets=preset_config,
    )


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid_to_ts(ulid_str: str | None) -> float | None:
    if not ulid_str or len(ulid_str) < 10:
        return None
    try:
        val = 0
        for char in ulid_str[:10]:
            idx = _CROCKFORD.find(char.upper())
            if idx < 0:
                return None
            val = (val << 5) | idx
        return val / 1000.0
    except Exception:
        return None


def _parse_ts(ts_str: str | None, ulid_str: str | None = None) -> float:
    """Parse storage.now() ISO timestamp to UTC Unix timestamp (float seconds), falling back to ULID."""
    if ts_str:
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except Exception:
            pass
    ulid_ts = _ulid_to_ts(ulid_str)
    if ulid_ts is not None:
        return ulid_ts
    return time.time()


def _payload_field(payload: dict[str, Any], field_path: str) -> Any:
    """Resolve a dotted field path against a payload mapping.

    Missing intermediate keys yield ``None`` (threshold rules treat that as
    fail-closed when a bound is set — unknown value must not silently pass).
    """
    current: Any = payload
    for part in field_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _caller_principals(_originator: str | None, scope: gateway.CallScope | None = None) -> set[str]:
    """Identity + active profile labels for role matching.

    The verified agent (or the already-namespaced ``scope.agent``) is the only
    identity principal. An adapter-supplied ``originator`` is never an agent
    label and cannot satisfy a role allow-list.
    """
    principals: set[str] = set()
    identity = (config.verified_agent_identity() or "").strip()
    if identity:
        principals.add(identity)
    elif scope is not None and scope.agent:
        principals.add(scope.agent)
    try:
        from . import profiles

        principals.add(profiles.current())
    except Exception:
        # Profile lookup must not open a bypass; evaluation continues with
        # whatever identity we already have.
        pass
    return {p for p in principals if p}


def _roles_apply(
    rule: PolicyRule,
    originator: str | None,
    scope: gateway.CallScope | None = None,
    *,
    principals: set[str] | None = None,
) -> bool:
    """Return True when the rule is in scope for this caller.

    An empty ``roles`` list means the rule applies to every caller. A non-empty
    list means at least one principal (profile or agent identity) must match.
    ``principals`` overrides the request-context set — for a view that scopes
    a subject other than the caller (task #2144), where the request's own
    identity and profile must not leak into the check.
    """
    if not rule.roles:
        return True
    if principals is None:
        principals = _caller_principals(originator, scope)
    return any(role in principals for role in rule.roles)


def _refuse(
    message: str,
    *,
    tool: str,
    reason_code: str,
    rule: str,
    scope: gateway.CallScope,
    episode_id: str | None,
    cause: BaseException | None = None,
) -> None:
    """Record a deny, then raise. Ledger failure still denies the call."""
    gateway.record_denial(
        tool_name=tool,
        reason_code=reason_code,
        rule=rule,
        caller=scope.agent,
        profile=scope.profile,
        episode_id=episode_id,
    )
    exc = PolicyViolation(message, reason_code=reason_code)
    if cause is not None:
        raise exc from cause
    raise exc


def _enforce_action(
    rule: PolicyRule,
    tool: str,
    payload: dict[str, Any],
    scope: gateway.CallScope,
    episode_id: str | None,
    *,
    reason_code: str,
    deferred: list[PolicyRule] | None = None,
) -> None:
    """Apply a Phase 3 declarative action for an already-matched rule.

    With ``deferred``, a require_approval action is queued instead of run, so
    the caller can check rate limits before an approval pass is consumed.
    """
    if rule.action == "allow":
        return
    if rule.action == "require_approval" and deferred is not None:
        deferred.append(rule)
        return
    detail = f": {rule.description}" if rule.description else ""
    if rule.action == "deny":
        _refuse(
            f"Tool '{tool}' denied by policy rule '{rule.name}'{detail}",
            tool=tool,
            reason_code=reason_code,
            rule=rule.name,
            scope=scope,
            episode_id=episode_id,
        )
    # require_approval — force the existing approval queue, fail closed on errors
    if rule.action == "require_approval":
        try:
            from .tools.approvals import ApprovalPendingError, check_approval_gate

            check_approval_gate(
                tool,
                payload,
                requested_by=config.verified_agent_identity(),
                force=True,
            )
        except ApprovalPendingError:
            raise
        except PolicyViolation:
            raise
        except Exception as exc:
            _refuse(
                f"Approval gate failed closed for policy rule '{rule.name}' on tool '{tool}': {exc}",
                tool=tool,
                reason_code="policy_unavailable",
                rule=rule.name,
                scope=scope,
                episode_id=episode_id,
                cause=exc,
            )
        return
    _refuse(
        f"Unknown policy action {rule.action!r} on rule '{rule.name}'",
        tool=tool,
        reason_code="policy_unavailable",
        rule=rule.name,
        scope=scope,
        episode_id=episode_id,
    )


def _is_restrictive(rule: PolicyRule) -> bool:
    """False only for rules whose match can let a call through.

    A ``tool`` or ``threshold`` allow is a no-op on its own, but it does keep
    a default-deny policy from firing, so it must not match on missing
    claims. A ``role`` allow is the same: matching an unclaimed caller would
    treat a project-bound allow-list as a grant. Every other rule can only
    refuse.
    """
    return not (rule.type in ("tool", "threshold", "role") and rule.action == "allow")


def _episode_call_count(conn: Any, ep_id: str, tool: str) -> int:
    """Calls of ``tool`` in episode ``ep_id`` for the per-episode caps.

    The larger of the tool's own events (``tool.<name>``, ``agent.tool.<name>``,
    ``$.tool`` / ``$.kind``) and the gateway's canonical count of calls that ran,
    rolled-up and still-coalesced reads included (review of #2313): a bearer
    run writes no per-call row for a read, so the old count saw none of them.
    The larger, not the sum, so a call recorded both ways counts once.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM event_log "
        "WHERE episode_id = ? AND (channel = ? OR channel = ? "
        "   OR json_extract(payload, '$.tool') = ? "
        "   OR json_extract(payload, '$.kind') = ?)",
        (ep_id, f"tool.{tool}", f"agent.tool.{tool}", tool, tool),
    ).fetchone()
    legacy = int(row["n"]) if row else 0
    return max(legacy, gateway.episode_call_count(conn, ep_id, tool))


def evaluate_policy(
    tool: str,
    payload: dict[str, Any] | None = None,
    episode_id: str | None = None,
    originator: str | None = None,
    policy_config: PolicyConfig | None = None,
    *,
    now: float | None = None,
    call_scope: gateway.CallScope | None = None,
    _approval_payload: dict[str, Any] | None = None,
) -> None:
    """Evaluate policy rules for a tool invocation.

    Profile visibility is a separate decision (``profiles.allowed_tools``).
    This function is call permission: allow, deny, require_approval, or a
    per-caller rate limit. Deny beats allow. An allow does not cancel a later
    deny or an exhausted rate limit.

    Raises PolicyViolation if the invocation is blocked or fails closed.
    Raises ApprovalPendingError when a require_approval rule parks the call.
    ``now`` overrides the rate-limit clock (tests). ``call_scope`` overrides
    claim resolution (tests). The private ``_approval_payload`` preserves an
    adapter's historical pass key without changing rule or trust evaluation.
    """
    ep_id = episode_id if episode_id is not None else config.current_episode_id()
    cfg = policy_config if policy_config is not None else load_policy()
    trust_presets.check_call(
        tool, read_only=trust_presets.is_read_only_call(tool, payload), payload=payload, _config=cfg
    )
    if policy_config is None:
        gateway.reconcile_rate_rules(cfg.rules if cfg.enabled else [])
        policy_namespace = None
    else:
        policy_namespace = hashlib.sha256(
            json.dumps(asdict(cfg), sort_keys=True).encode()
        ).hexdigest()[:16]

    if not cfg.enabled:
        return

    payload_dict = payload or {}
    tool_matched = [r for r in cfg.rules if fnmatch.fnmatch(tool, r.tool)]
    if not tool_matched and cfg.default_action != "deny":
        return
    if call_scope is not None:
        scope = call_scope
    else:
        need_claims = any(gateway.bind_needs_claims(rule.bind) for rule in tool_matched)
        scope = gateway.resolve_scope(originator, need_claims=need_claims)
    # Tool match first. Type `role` is an allow-list for the tool and must still
    # fire when the caller is outside `roles` (deny), so it is NOT filtered by
    # `_roles_apply`. Other kinds treat `roles` as an optional scope filter.
    # Bindings are an additional filter on every kind, including role.
    matching_rules = [
        r
        for r in tool_matched
        if gateway.binding_matches(r.bind, scope, unclaimed_matches=_is_restrictive(r))
        and (r.type == "role" or _roles_apply(r, originator, scope))
    ]

    permission_rules = [rule for rule in matching_rules if rule.type != "rate_limit"]
    if not permission_rules:
        if cfg.default_action == "deny":
            _refuse(
                f"Tool '{tool}' denied by default policy action",
                tool=tool,
                reason_code="default_deny",
                rule="",
                scope=scope,
                episode_id=ep_id,
            )
        if not matching_rules:
            return

    now_ts = time.time()
    deferred: list[PolicyRule] = []

    for rule in matching_rules:
        # Rate limits are reserved after every deny, before any approval pass.
        if rule.type == "rate_limit":
            continue
        # --- Phase 3 declarative kinds ---------------------------------------
        if rule.type == "tool":
            _enforce_action(
                rule, tool, payload_dict, scope, ep_id, reason_code="policy_deny", deferred=deferred
            )
            continue

        if rule.type == "role":
            # Allow-list: caller must match roles. Fail closed if they do not.
            if not _roles_apply(rule, originator, scope):
                _refuse(
                    f"Tool '{tool}' denied by role policy '{rule.name}': "
                    f"requires one of {rule.roles}",
                    tool=tool,
                    reason_code="role_denied",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                )
            # Matched role: honour action (allow is a no-op).
            if rule.action != "allow":
                _enforce_action(
                    rule,
                    tool,
                    payload_dict,
                    scope,
                    ep_id,
                    reason_code="policy_deny",
                    deferred=deferred,
                )
            continue

        if rule.type == "threshold":
            raw_value = _payload_field(payload_dict, rule.field_path or "")
            if raw_value is None:
                _refuse(
                    f"Policy rule '{rule.name}' requires payload field "
                    f"'{rule.field_path}' (fail-closed: missing value)",
                    tool=tool,
                    reason_code="threshold_invalid",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                )
            try:
                numeric = float(raw_value)
            except (TypeError, ValueError, OverflowError) as exc:
                _refuse(
                    f"Policy rule '{rule.name}' payload field '{rule.field_path}' "
                    f"must be numeric, got {raw_value!r}",
                    tool=tool,
                    reason_code="threshold_invalid",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                    cause=exc,
                )
            if not math.isfinite(numeric):
                # A non-finite candidate (NaN, inf, -inf, or their string forms)
                # would otherwise fail both bound comparisons silently and
                # slip through as if it satisfied the rule (fail-open). Treat
                # it the same as a non-numeric value: fail closed at the
                # shared boundary, before any per-rule action is consulted.
                _refuse(
                    f"Policy rule '{rule.name}' payload field '{rule.field_path}' "
                    f"must be a finite number, got {raw_value!r}",
                    tool=tool,
                    reason_code="threshold_invalid",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                )
            breached = False
            if rule.max_value is not None and numeric > rule.max_value:
                breached = True
            if rule.min_value is not None and numeric < rule.min_value:
                breached = True
            if breached:
                _enforce_action(
                    rule,
                    tool,
                    payload_dict,
                    scope,
                    ep_id,
                    reason_code="threshold_breached",
                    deferred=deferred,
                )
            continue

        # --- Phase 1 episode guardrails --------------------------------------
        # Check fail-closed episode existence if required
        is_episode_rule = rule.require_episode or rule.type in (
            "max_calls_per_episode",
            "fatigue_band_cap",
        )
        if is_episode_rule:
            if not ep_id:
                _refuse(
                    f"Policy rule '{rule.name}' requires a valid episode_id, "
                    "but none was provided.",
                    tool=tool,
                    reason_code="episode_limited",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                )
            # Verify the episode exists. A self-pickup run episode (``run:<id>``,
            # task #2313) exists when the hub opened that run: its own record,
            # not whatever this call's earlier calls or denials wrote.
            try:
                conn = storage.connect()
                try:
                    if gateway.is_run_episode(ep_id):
                        count = 1 if gateway.run_episode_exists(conn, ep_id) else 0
                    else:
                        row = conn.execute(
                            "SELECT COUNT(*) AS n FROM event_log WHERE episode_id = ?",
                            (ep_id,),
                        ).fetchone()
                        count = row["n"] if row else 0
                finally:
                    conn.close()
            except sqlite3.Error as e:
                _refuse(
                    f"Database error while evaluating episode policy rule '{rule.name}': {e}",
                    tool=tool,
                    reason_code="policy_unavailable",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                    cause=e,
                )

            if count == 0:
                _refuse(
                    f"Episode '{ep_id}' not found in event_log "
                    f"(fail-closed policy requirement for rule '{rule.name}').",
                    tool=tool,
                    reason_code="episode_limited",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                )

        # 1. Cooldown rule
        if rule.type == "cooldown":
            cooldown_sec = rule.cooldown_seconds or 0.0
            if cooldown_sec > 0:
                try:
                    conn = storage.connect()
                    try:
                        row = conn.execute(
                            "SELECT ulid, created_at FROM event_log "
                            "WHERE channel = ? OR channel = ? "
                            "   OR json_extract(payload, '$.tool') = ? "
                            "   OR json_extract(payload, '$.kind') = ? "
                            "ORDER BY ulid DESC LIMIT 1",
                            (f"tool.{tool}", f"agent.tool.{tool}", tool, tool),
                        ).fetchone()
                    finally:
                        conn.close()
                except sqlite3.Error as e:
                    _refuse(
                        f"Database error while checking cooldown for rule '{rule.name}': {e}",
                        tool=tool,
                        reason_code="policy_unavailable",
                        rule=rule.name,
                        scope=scope,
                        episode_id=ep_id,
                        cause=e,
                    )

                if row:
                    last_ts = _parse_ts(row["created_at"], row["ulid"])
                    elapsed = now_ts - last_ts
                    if elapsed < cooldown_sec:
                        remaining = cooldown_sec - elapsed
                        _refuse(
                            f"Stabilization cooldown active for tool '{tool}': "
                            f"{remaining:.1f}s remaining (rule '{rule.name}').",
                            tool=tool,
                            reason_code="episode_limited",
                            rule=rule.name,
                            scope=scope,
                            episode_id=ep_id,
                        )

        # 2. Max calls per episode rule
        elif rule.type == "max_calls_per_episode":
            max_calls = rule.max_calls if rule.max_calls is not None else 1
            try:
                conn = storage.connect()
                try:
                    call_count = _episode_call_count(conn, ep_id, tool)
                finally:
                    conn.close()
            except sqlite3.Error as e:
                _refuse(
                    f"Database error while checking max calls for rule '{rule.name}': {e}",
                    tool=tool,
                    reason_code="policy_unavailable",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                    cause=e,
                )

            if call_count >= max_calls:
                _refuse(
                    f"Episode '{ep_id}' reached maximum limit of {max_calls} call(s) "
                    f"for tool '{tool}' (rule '{rule.name}').",
                    tool=tool,
                    reason_code="episode_limited",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                )

        # 3. Fatigue band cap rule
        elif rule.type == "fatigue_band_cap":
            bands = rule.bands or {"green": 5, "yellow": 3, "red": 1}
            fatigue_band = payload_dict.get("fatigue_band") or payload_dict.get("band")

            if not fatigue_band:
                # Query event_log for latest fatigue_band in episode
                try:
                    conn = storage.connect()
                    try:
                        row = conn.execute(
                            "SELECT payload FROM event_log "
                            "WHERE episode_id = ? AND payload LIKE '%fatigue_band%' "
                            "ORDER BY ulid DESC LIMIT 1",
                            (ep_id,),
                        ).fetchone()
                        if row and row["payload"]:
                            pdata = json.loads(row["payload"])
                            fatigue_band = pdata.get("fatigue_band") or pdata.get("band")
                    finally:
                        conn.close()
                except Exception:
                    pass

            band_name = str(fatigue_band).lower() if fatigue_band else "yellow"
            cap = bands.get(band_name, bands.get("red", 1))

            try:
                conn = storage.connect()
                try:
                    call_count = _episode_call_count(conn, ep_id, tool)
                finally:
                    conn.close()
            except sqlite3.Error as e:
                _refuse(
                    f"Database error while checking fatigue band cap for rule '{rule.name}': {e}",
                    tool=tool,
                    reason_code="policy_unavailable",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                    cause=e,
                )

            if call_count >= cap:
                _refuse(
                    f"Episode '{ep_id}' reached fatigue band '{band_name}' limit of "
                    f"{cap} call(s) for tool '{tool}' (rule '{rule.name}').",
                    tool=tool,
                    reason_code="episode_limited",
                    rule=rule.name,
                    scope=scope,
                    episode_id=ep_id,
                )

    reservation = gateway.reserve_rate_limits(
        [rule for rule in matching_rules if rule.type == "rate_limit"],
        tool=tool,
        scope=scope,
        now=now,
        episode_id=ep_id,
        policy_namespace=policy_namespace,
    )
    try:
        for rule in deferred:
            _enforce_action(
                rule,
                tool,
                payload_dict if _approval_payload is None else _approval_payload,
                scope,
                ep_id,
                reason_code="policy_deny",
            )
    except BaseException:
        reservation.refund()
        raise
