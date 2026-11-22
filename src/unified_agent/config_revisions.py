"""Immutable revisions of authority config (task #1352).

Profiles, policy, approval rules, and the org registry stay in the stores
that already own them (the policy file, the checked-in org charter). Each
write appends one hash-chained ``event_log`` row on ``agent.config.revision``
— actor, diff, timestamp, and the document — and never updates an older row.
Rollback is a new revision. It does not rewrite history, and it does not run
until the existing approval gate has a one-shot pass for that exact restore.
A profile overlay cannot raise an identity above the privilege of its code
default. The env approval allowlist (``UA_APPROVAL_TOOLS``) is recorded as
observed and is never written back.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import config, events, org_registry, policy, storage, team_policy
from .tools.approvals import (
    check_approval_gate,
    mark_governed_execution,
    restore_consumed_approval,
)

CHANNEL = "agent.config.revision"
ROLLBACK_TOOL = "config_revision_rollback"
# Commits that add, remove, or edit require_approval rules. Other commits
# stay a chief-of-staff write. This name is a gate, not a second MCP tool.
COMMIT_TOOL = "config_revision_commit"
KINDS = ("policy", "profile", "approval_rule", "org")
MAX_RAW_BYTES = 256_000
MAX_DIFF_CHARS = 80_000
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SECRET_KEYS = frozenset(
    {
        "token",
        "password",
        "secret",
        "authorization",
        "api_key",
        "apikey",
        "bearer",
        "private_key",
        "client_secret",
    }
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:^|[\s#\"'])(" + "|".join(sorted(_SECRET_KEYS)) + r")\s*[:=]\s*\S+"
)
# Higher rank is usually a wider capability set, but rank is not a subset
# check: operator is below developer and still grants approval_resolve.
# An overlay may repeat the code default or move to a strictly lower rank
# whose named tools the code default already allows. Same-rank swaps
# (research vs operator) are refused: they are a different grant.
_RANK = {
    "public": 0,
    "cloud-assistant": 0.5,
    "cloud-vault-steward": 0.75,
    "research": 1,
    "operator": 2,
    "steward": 3,
    "developer": 4,
    "cto": 4.5,
    "chief-of-staff": 5,
}
# Re-entrant so a commit can hold the lock across the live read and the write.
# ``_mutate_file`` takes the same lock; a plain Lock would deadlock.
_lock = threading.RLock()


class ConfigRevisionError(ToolError):
    """A config revision was refused before it changed the live store."""


class ConfigFanoutError(ConfigRevisionError, RuntimeError):
    """Raised when durable config was committed, but post-commit event fanout failed."""

    def __init__(
        self,
        message: str,
        *,
        result: dict[str, Any],
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.result = dict(result)
        self.committed = True
        self.revision = result.get("revision")
        self.kind = result.get("kind")
        self.document_sha256 = result.get("document_sha256")
        self.action = result.get("action")
        self.notification_error = result.get("notification_error")
        self.notification_sent = False
        self.cause = cause


def applied_profile_overlay(identity: str, code_default: str) -> str | None:
    """Profile name from the policy-file overlay, or None to keep the code default.

    An overlay that would escalate, or that is not a known profile, is ignored
    so a hand-edited file cannot widen an identity. Commit refuses those
    instead of ignoring them.
    """
    try:
        overlay = _overlay_map()
    except ConfigRevisionError:
        return None
    chosen = overlay.get(identity)
    if chosen is None:
        return None
    if not _overlay_is_allowed(chosen, code_default):
        return None
    return chosen


def list_revisions(kind: str, limit: int = 50) -> dict[str, Any]:
    """Newest-first revision summaries plus whether live config has drifted."""
    kind = _kind(kind)
    limit = max(1, min(int(limit), 100))
    with _lock:
        live = _live_document(kind)
        raw = _raw_for(kind) if kind in ("policy", "org") else ""
        conn = storage.connect()
        try:
            rows = _load_kind(conn, kind)
        finally:
            conn.close()
    live_sha = _sha(live)
    latest = rows[-1] if rows else None
    drift = latest is None or not _same_snapshot(kind, latest, live, raw)
    if not drift:
        # A clean live file is not the historical diff of the latest revision.
        latest_diff = ""
    elif latest is not None and kind in ("policy", "org") and latest["document_sha256"] == live_sha:
        latest_diff = _text_diff(latest.get("raw") or "", raw)
    else:
        latest_diff = _make_diff(None if latest is None else latest["document"], live)
    public = [_public_row(row) for row in rows]
    public.reverse()
    return {
        "kind": kind,
        "live_sha256": live_sha,
        "drift": drift,
        "latest_diff": latest_diff,
        "revisions": public[:limit],
        "count": len(rows),
    }


def diff_revisions(kind: str, from_revision: int, to_revision: int | None = None) -> dict[str, Any]:
    """Unified diff of canonical JSON. ``to_revision`` omitted means the live document."""
    kind = _kind(kind)
    with _lock:
        conn = storage.connect()
        try:
            rows = _load_kind(conn, kind)
        finally:
            conn.close()
    source = _find(rows, kind, int(from_revision))
    if to_revision is None:
        target_doc = _live_document(kind)
        target_label = "live"
        target_sha = _sha(target_doc)
    else:
        target = _find(rows, kind, int(to_revision))
        target_doc = target["document"]
        target_label = str(target["revision"])
        target_sha = target["document_sha256"]
    if kind in ("policy", "org"):
        source_raw = source.get("raw") or ""
        if to_revision is None:
            target_raw = _raw_for(kind)
        else:
            target_raw = target.get("raw") or ""
        shown = _text_diff(source_raw, target_raw)
    else:
        shown = _make_diff(source["document"], target_doc)
    return {
        "kind": kind,
        "from_revision": source["revision"],
        "to_revision": target_label,
        "from_sha256": source["document_sha256"],
        "to_sha256": target_sha,
        "diff": shown,
    }


def show_revision(kind: str, revision: int) -> dict[str, Any]:
    """One immutable revision, including its document and the diff recorded with it."""
    kind = _kind(kind)
    with _lock:
        conn = storage.connect()
        try:
            rows = _load_kind(conn, kind)
        finally:
            conn.close()
    row = _find(rows, kind, int(revision))
    return _verified_snapshot(kind, row)


def revision_snapshots(kind: str) -> list[dict[str, Any]]:
    """Complete newest-first immutable history for internal catalog projections.

    The bounded public summary list keeps its existing default and maximum.
    Load once and verify every document through the same reader as show_revision.
    """
    kind = _kind(kind)
    with _lock:
        conn = storage.connect()
        try:
            rows = _load_kind(conn, kind)
        finally:
            conn.close()
    return [_verified_snapshot(kind, row) for row in reversed(rows)]


def _verified_snapshot(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    if _sha(row["document"]) != row["document_sha256"]:
        raise ConfigRevisionError(
            f"{kind} revision {row['revision']} document does not match its stored hash"
        )
    shown = _public_row(row)
    shown["document"] = row["document"]
    shown["diff"] = row.get("diff") or ""
    return shown


def capture(kind: str, actor: str | None = None) -> dict[str, Any]:
    """Record the live document when it has no revision yet or has drifted.

    Capture does not change the live store. A commit or rollback that already
    matches the latest revision is left as it is.
    """
    kind = _kind(kind)
    who = _actor(actor)
    return _append_live(kind, action="capture", actor=who)


def commit(
    kind: str,
    document: dict[str, Any],
    actor: str | None = None,
    *,
    expected_base_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate ``document``, write it through the existing store, and record it."""
    kind = _kind(kind)
    if expected_base_sha256 is not None and (
        kind != "policy"
        or not isinstance(expected_base_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_base_sha256)
    ):
        raise ConfigRevisionError("expected_base_sha256 requires a policy commit and raw SHA-256")
    if not isinstance(document, dict):
        raise ConfigRevisionError("document must be an object")
    _refuse_secrets(document)
    who = _actor(actor)
    # One lock covers the live read, the patch, and the write. Profile and
    # approval commits share the policy file; building the text before the
    # lock let a concurrent policy commit get overwritten.
    with _lock:
        if kind == "policy":
            if expected_base_sha256 is not None and _policy_raw_sha() != expected_base_sha256:
                raise ConfigRevisionError("live policy changed since the approved base")
            return _commit_policy(document, who, expected_base_sha256=expected_base_sha256)
        if kind == "profile":
            return _commit_profile(document, who)
        if kind == "approval_rule":
            return _commit_approval(document, who)
        return _commit_org(document, who)


def rollback(kind: str, revision: int, actor: str | None = None) -> dict[str, Any]:
    """Restore a prior revision through the approval gate, as a new revision."""
    kind = _kind(kind)
    who = _actor(actor)
    target_n = int(revision)
    with _lock:
        conn = storage.connect()
        try:
            rows = _load_kind(conn, kind)
        finally:
            conn.close()
        target = _find(rows, kind, target_n)
        if _sha(target["document"]) != target["document_sha256"]:
            raise ConfigRevisionError(
                f"{kind} revision {target_n} document does not match its stored hash"
            )
        planned = _plan_restore(kind, target)
        if planned["unchanged"]:
            return _unchanged_revision(kind, target, planned.get("note"))
        # The pass matches this target on this live file. A later edit makes
        # a new payload, so the old pass cannot land on a file nobody re-reviewed.
        base_sha = _restore_base(kind)
        payload = {
            "kind": kind,
            "revision": target_n,
            "document_sha256": target["document_sha256"],
            "base_sha256": base_sha,
        }
        # Validate before the pass is consumed so a bad restore does not spend it.
        _validate_planned(kind, planned)
        gate = check_approval_gate(ROLLBACK_TOOL, payload, requested_by=who, force=True)
        approval_id = int(gate["approval_id"]) if gate.get("approval_id") is not None else None
        try:
            if _restore_base(kind) != base_sha:
                raise ConfigRevisionError(
                    "live config changed after approval was requested; request rollback again"
                )
            # Rebuild against the live file under the lock. The plan above was
            # only the pre-approval check.
            planned = _plan_restore(kind, target)
            if planned["unchanged"]:
                if approval_id is not None:
                    restore_consumed_approval(approval_id)
                return _unchanged_revision(kind, target, planned.get("note"))
            _validate_planned(kind, planned)
            recorded = _apply_planned(
                kind,
                planned,
                actor=who,
                action="rollback",
                restores=target_n,
                approval_id=approval_id,
            )
        except Exception as exc:
            if not isinstance(exc, ConfigFanoutError) and approval_id is not None:
                restore_consumed_approval(approval_id)
            raise
    recorded["approval_id"] = approval_id
    recorded["restores_revision"] = target_n
    if planned.get("note"):
        recorded["note"] = planned["note"]
    return recorded


def _unchanged_revision(kind: str, latest: dict[str, Any], note: str | None) -> dict[str, Any]:
    return {
        "kind": kind,
        "unchanged": True,
        "revision": latest["revision"],
        "ulid": latest["ulid"],
        "document_sha256": latest["document_sha256"],
        "note": note,
    }


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise ConfigRevisionError(f"unknown config kind {kind!r}; choose from {', '.join(KINDS)}")
    return kind


def _actor(explicit: str | None) -> str:
    verified = config.verified_agent_identity()
    if verified:
        return verified
    if explicit and explicit.strip():
        return explicit.strip()
    raise ConfigRevisionError("config revision requires an authenticated actor")


def _sha(document: Any) -> str:
    blob = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _raw_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _policy_raw_sha() -> str:
    _mapping, raw = _read_policy_file()
    return _raw_sha(raw)


def _restore_base(kind: str) -> str:
    """Hash of the file a rollback would rewrite, before the restore."""
    if kind == "org":
        _document, raw = _live_org()
        return _raw_sha(raw)
    return _policy_raw_sha()


def _same_snapshot(kind: str, row: dict[str, Any], live: dict[str, Any], raw: str) -> bool:
    """True when a revision already records this live document.

    Policy and org revisions also store the file text, so a comment-only
    edit is drift even though the parsed mapping hash is unchanged.
    """
    if row["document_sha256"] != _sha(live):
        return False
    if kind in ("policy", "org"):
        return row.get("raw_sha256") == _raw_sha(raw)
    return True


def _text_diff(before: str, after: str) -> str:
    text = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="previous",
            tofile="live",
        )
    )
    if len(text) > MAX_DIFF_CHARS:
        return text[:MAX_DIFF_CHARS] + "\n… diff truncated\n"
    return text


def _pretty(document: Any) -> str:
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _make_diff(before: Any, after: Any) -> str:
    prior = "" if before is None else _pretty(before)
    text = "".join(
        difflib.unified_diff(
            prior.splitlines(keepends=True),
            _pretty(after).splitlines(keepends=True),
            fromfile="previous",
            tofile="next",
        )
    )
    if len(text) > MAX_DIFF_CHARS:
        return text[:MAX_DIFF_CHARS] + "\n… diff truncated\n"
    return text


_PROSE_SECRET_VALUES = frozenset(
    {
        "allow",
        "deny",
        "false",
        "missing",
        "none",
        "null",
        "operator",
        "optional",
        "required",
        "true",
    }
)


def _refuse_secret_text(raw: str) -> None:
    """Reject private keys and credential assignments in the file text.

    A prose value such as ``password: required`` is not a credential. A
    value such as ``api_key: live-value`` is, whether it sits in a comment,
    a block scalar, or a description. Mapping keys are still refused by
    ``_refuse_secrets``.
    """
    if "BEGIN PRIVATE KEY" in raw or "BEGIN OPENSSH PRIVATE KEY" in raw:
        raise ConfigRevisionError("config documents cannot carry private keys")
    for match in _SECRET_ASSIGNMENT.finditer(raw):
        token = match.group(0).split(":", 1)[-1].split("=", 1)[-1]
        token = token.strip().strip("\"'`").rstrip(".,;").casefold()
        if token not in _PROSE_SECRET_VALUES:
            raise ConfigRevisionError("config documents cannot carry secret values")


def _refuse_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).replace("-", "_").casefold() in _SECRET_KEYS:
                raise ConfigRevisionError(
                    f"config documents cannot carry secret values; remove {key!r}"
                )
            _refuse_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _refuse_secrets(item)
    elif isinstance(value, str) and "BEGIN PRIVATE KEY" in value:
        raise ConfigRevisionError("config documents cannot carry private keys")


def _policy_path() -> Path:
    path = config.policy_file_path()
    _guard_config_path(path)
    return path


def _read_policy_file() -> tuple[dict[str, Any], str]:
    path = _policy_path()
    if not path.exists():
        return {}, ""
    raw = path.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > MAX_RAW_BYTES:
        raise ConfigRevisionError("policy file exceeds the revision size limit")
    if not raw.strip():
        # An empty file is not the missing-file case. load_policy fails closed
        # on it; recording in-code defaults would show a healthy history.
        raise ConfigRevisionError("policy file is empty; delete it to use the in-code defaults")
    data = yaml.safe_load(raw)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigRevisionError("policy file must be a YAML mapping")
    _refuse_secrets(data)
    return data, raw


def _default_policy_document() -> dict[str, Any]:
    return {
        "version": 1,
        "enabled": True,
        "default_action": "allow",
        "rules": [_rule_to_dict(rule) for rule in policy.DEFAULT_POLICY_RULES],
    }


def _rule_to_dict(rule: policy.PolicyRule) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": rule.name,
        "description": rule.description,
        "tool": rule.tool,
        "type": rule.type,
        "action": rule.action,
    }
    if rule.max_calls is not None:
        out["max_calls"] = rule.max_calls
    if rule.cooldown_seconds is not None:
        out["cooldown_seconds"] = rule.cooldown_seconds
    if rule.bands is not None:
        out["bands"] = rule.bands
    if rule.require_episode:
        out["require_episode"] = True
    if rule.roles:
        out["roles"] = list(rule.roles)
    if rule.field_path is not None:
        out["field_path"] = rule.field_path
    if rule.max_value is not None:
        out["max_value"] = rule.max_value
    if rule.min_value is not None:
        out["min_value"] = rule.min_value
    if rule.window_seconds is not None:
        out["window_seconds"] = rule.window_seconds
    if rule.bind:
        out["bind"] = dict(rule.bind)
    return out


def _live_policy_document() -> dict[str, Any]:
    mapping, raw = _read_policy_file()
    if not raw:
        return _default_policy_document()
    return mapping


def _overlay_map() -> dict[str, str]:
    mapping, _raw = _read_policy_file()
    raw = mapping.get("profile_defaults") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out


def _overlay_is_allowed(chosen: str, code_default: str) -> bool:
    if chosen not in _RANK or code_default not in _RANK:
        return False
    if _RANK[chosen] > _RANK[code_default]:
        return False
    if _RANK[chosen] == _RANK[code_default] and chosen != code_default:
        return False
    if _overlay_widens_tools(chosen, code_default):
        return False
    return True


def _overlay_widens_tools(chosen: str, code_default: str) -> bool:
    """True when ``chosen`` grants a tool the code default's privilege set refuses.

    For developer, CTO and steward, that set is the high-impact deny list
    (``approval_resolve`` and the rest of ``DEVELOPER_DENIED_TOOLS``; the CTO's
    is ``CTO_DENIED_TOOLS``).
    Pruned and legacy names are not a privilege grant. For an allow-list
    profile, ``chosen`` must be a subset of that allow-list. Chief of staff
    already holds every tool.
    """
    if chosen == code_default:
        return False
    from .profiles import (
        CHIEF_OF_STAFF,
        CLOUD_ASSISTANT,
        CLOUD_ASSISTANT_ALLOWED_TOOLS,
        CLOUD_VAULT_STEWARD,
        CLOUD_VAULT_STEWARD_ALLOWED_TOOLS,
        CTO,
        CTO_DENIED_TOOLS,
        DEVELOPER,
        DEVELOPER_DENIED_TOOLS,
        OPERATOR,
        OPERATOR_ALLOWED_TOOLS,
        PUBLIC,
        PUBLIC_ALLOWED_TOOLS,
        RESEARCH,
        RESEARCH_ALLOWED_TOOLS,
        STEWARD,
        STEWARD_DENIED_TOOLS,
        tool_is_allowed_for,
    )

    if code_default == CHIEF_OF_STAFF:
        return False
    allow_lists = {
        CLOUD_ASSISTANT: CLOUD_ASSISTANT_ALLOWED_TOOLS,
        CLOUD_VAULT_STEWARD: CLOUD_VAULT_STEWARD_ALLOWED_TOOLS,
        OPERATOR: OPERATOR_ALLOWED_TOOLS,
        PUBLIC: PUBLIC_ALLOWED_TOOLS,
        RESEARCH: RESEARCH_ALLOWED_TOOLS,
    }
    if code_default in allow_lists:
        chosen_tools = allow_lists.get(chosen)
        if chosen_tools is None:
            return True
        return not chosen_tools <= allow_lists[code_default]
    if code_default == STEWARD:
        denied = STEWARD_DENIED_TOOLS
    elif code_default == CTO:
        denied = CTO_DENIED_TOOLS
    elif code_default == DEVELOPER:
        denied = DEVELOPER_DENIED_TOOLS
    else:
        return True
    return any(tool_is_allowed_for(chosen, name) for name in denied)


def rollback_recovery_allowed(identity: str | None, action: str = "rollback") -> bool:
    """A code-default chief of staff may still read and roll back after a downgrade.

    Commit and capture stay on the effective profile. Rollback is still
    approval-gated, so the downgraded identity cannot restore itself alone.
    """
    if action not in {"rollback", "list", "show", "diff"} or not identity:
        return False
    from .profiles import CHIEF_OF_STAFF, current_session_grant, profiles_by_identity

    if current_session_grant() is not None:
        # A session credential (task #1744) never inherits this recovery path.
        return False

    return profiles_by_identity().get(identity) == CHIEF_OF_STAFF


def _require_remaining_cos(overlay: dict[str, str]) -> None:
    """Refuse an overlay that removes every code-default chief of staff."""
    from .profiles import CHIEF_OF_STAFF, profiles_by_identity

    identities = [
        label for label, profile in profiles_by_identity().items() if profile == CHIEF_OF_STAFF
    ]
    remaining = [
        label for label in identities if overlay.get(label, CHIEF_OF_STAFF) == CHIEF_OF_STAFF
    ]
    if identities and not remaining:
        raise ConfigRevisionError(
            "profile overlay must leave at least one chief-of-staff identity; "
            "that identity is the recovery path for config revisions"
        )


def _validate_overlay(overlay: dict[str, Any]) -> dict[str, str]:
    from .profiles import DEVELOPER, profiles_by_identity

    if not isinstance(overlay, dict):
        raise ConfigRevisionError("profile overlay must be an object")
    clean: dict[str, str] = {}
    for key, value in overlay.items():
        if not isinstance(key, str) or _LABEL_RE.fullmatch(key) is None:
            raise ConfigRevisionError(f"invalid profile identity {key!r}")
        if not isinstance(value, str) or value not in _RANK:
            raise ConfigRevisionError(f"unknown profile {value!r} for {key}")
        code_default = profiles_by_identity().get(key, DEVELOPER)
        if not _overlay_is_allowed(value, code_default):
            raise ConfigRevisionError(
                f"profile overlay cannot change {key} from {code_default} to {value}: "
                "an overlay may only repeat the code default or reduce its privilege"
            )
        clean[key] = value
    clean = dict(sorted(clean.items()))
    _require_remaining_cos(clean)
    return clean


def _live_profile_document() -> dict[str, Any]:
    from .profiles import profiles_by_identity

    return {
        "code_defaults": dict(sorted(profiles_by_identity().items())),
        "overlay": dict(sorted(_overlay_map().items())),
    }


def _approval_rules_of(mapping: dict[str, Any]) -> list[Any]:
    rules = mapping.get("rules") or []
    if not isinstance(rules, list):
        return []
    return [
        rule
        for rule in rules
        if isinstance(rule, dict) and rule.get("action") == "require_approval"
    ]


def _live_approval_document() -> dict[str, Any]:
    mapping, raw = _read_policy_file()
    rules = _approval_rules_of(mapping if raw else {})
    return {
        "rules": rules,
        "env_gated_tools": sorted(config.approval_gated_tools()),
    }


def _live_org() -> tuple[dict[str, Any], str]:
    path = org_registry.registry_path()
    _guard_config_path(path)
    raw = path.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > MAX_RAW_BYTES:
        raise ConfigRevisionError("org registry exceeds the revision size limit")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ConfigRevisionError("org registry must be a YAML mapping")
    _refuse_secrets(data)
    return data, raw


def _live_document(kind: str) -> dict[str, Any]:
    if kind == "policy":
        return _live_policy_document()
    if kind == "profile":
        return _live_profile_document()
    if kind == "approval_rule":
        return _live_approval_document()
    document, _raw = _live_org()
    return document


def _dump(mapping: dict[str, Any]) -> str:
    _refuse_secrets(mapping)
    text = yaml.safe_dump(mapping, sort_keys=False, allow_unicode=True)
    if len(text.encode("utf-8")) > MAX_RAW_BYTES:
        raise ConfigRevisionError("config document exceeds the revision size limit")
    return text


def _validate_policy_text(text: str, *, conn: Any = None) -> None:
    from . import operating_policy
    from .budget_policy import BudgetConfigError, load_budgets

    path = _policy_path()
    parent = path.parent
    created_parent = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    # A name that does not exist yet. A fixed sibling would delete a draft
    # the operator already had at policy.yaml.revision-probe.
    probe = _unused_sibling(path, "revision-probe")
    try:
        _write_exclusive(probe, text)
        try:
            policy.load_policy(probe)
            try:
                load_budgets(probe)
            except BudgetConfigError as exc:
                raise ConfigRevisionError(str(exc)) from exc
            try:
                probe_cfg = operating_policy.load_config(probe, strict=True)
                ref_conn = conn if conn is not None else storage.connect()
                try:
                    operating_policy.check_references(probe_cfg, ref_conn)
                finally:
                    if conn is None:
                        ref_conn.close()
            except operating_policy.OperatingConfigError as exc:
                raise ConfigRevisionError(str(exc)) from exc
        finally:
            probe.unlink(missing_ok=True)
    except Exception:
        probe.unlink(missing_ok=True)
        if created_parent:
            try:
                parent.rmdir()
            except OSError:
                pass
        raise
    _validate_team_block(text)


def _validate_team_block(text: str) -> None:
    """Team specs (task #1736) live in the policy file's ``teams`` block.

    Runs on every path that records policy text: commit, rollback, capture
    and an unchanged commit of a hand-edited file.
    """
    try:
        team_policy.validate_policy_text(text)
    except team_policy.TeamSpecError as exc:
        raise ConfigRevisionError(str(exc)) from exc


def _validate_org_text(text: str) -> None:
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ConfigRevisionError("org registry must declare schema_version: 1")
    rows = data.get("offices")
    if not isinstance(rows, list):
        raise ConfigRevisionError("org registry offices must be a list")
    seen: set[str] = set()
    for raw_office in rows:
        if not isinstance(raw_office, dict):
            raise ConfigRevisionError("org registry office rows must be objects")
        office_id = raw_office.get("id")
        members = raw_office.get("members")
        if (
            not isinstance(office_id, str)
            or re.fullmatch(org_registry.OFFICE_ID_PATTERN, office_id) is None
            or office_id in seen
        ):
            raise ConfigRevisionError(f"invalid or duplicate org office id: {office_id!r}")
        if not isinstance(members, list) or any(not isinstance(label, str) for label in members):
            raise ConfigRevisionError(f"office {office_id!r} members must be a list of identities")
        if raw_office.get("lead") not in members:
            raise ConfigRevisionError(f"office {office_id!r} lead must be one of its members")
        seen.add(office_id)
    try:
        org_registry.load_service_identities(org_registry.resolve_placeholders(data))
    except ValueError as exc:
        raise ConfigRevisionError(str(exc)) from exc
    _refuse_secrets(data)


def _base_policy_mapping() -> dict[str, Any]:
    mapping, raw = _read_policy_file()
    if not raw:
        return _default_policy_document()
    return dict(mapping)


def _approval_change_payload(kind: str, document: dict[str, Any]) -> dict[str, Any] | None:
    """Payload for a commit that changes require_approval rules, else None."""
    if kind == "policy":
        new_rules = _approval_rules_of(document)
        live_rules = _approval_rules_of(_base_policy_mapping())
        if new_rules == live_rules:
            return None
        return {
            "kind": "policy",
            "document_sha256": _sha(document),
            "rules_sha256": _sha(new_rules),
            "base_sha256": _policy_raw_sha(),
        }
    if kind == "approval_rule":
        rules = list(document.get("rules") or [])
        if rules == _live_approval_document()["rules"]:
            return None
        return {
            "kind": "approval_rule",
            "rules_sha256": _sha(rules),
            "base_sha256": _policy_raw_sha(),
        }
    return None


def _authorize_approval_change(kind: str, document: dict[str, Any], actor: str) -> int | None:
    """Park a require_approval edit until a different identity approves it.

    The pass is bound to the live policy file. A commit that does not touch
    those rules returns None and writes immediately.
    """
    payload = _approval_change_payload(kind, document)
    if payload is None:
        return None
    gate = check_approval_gate(COMMIT_TOOL, payload, requested_by=actor, force=True)
    approval_id = int(gate["approval_id"]) if gate.get("approval_id") is not None else None
    if _policy_raw_sha() != payload["base_sha256"]:
        if approval_id is not None:
            restore_consumed_approval(approval_id)
        raise ConfigRevisionError("live policy changed after approval was requested; commit again")
    return approval_id


def _commit_policy(
    document: dict[str, Any], actor: str, *, expected_base_sha256: str | None = None
) -> dict[str, Any]:
    # Validate before the unchanged short-circuit. A hand-edited file can
    # already match ``document`` and would otherwise be recorded without the
    # overlay gate.
    if "profile_defaults" in document:
        _validate_overlay(document.get("profile_defaults") or {})
    if _sha(document) == _sha(_live_policy_document()):
        return _append_live(
            "policy", action="commit", actor=actor, expected_base_sha256=expected_base_sha256
        )
    text = _dump(document)
    _validate_policy_text(text)
    approval_id = _authorize_approval_change("policy", document, actor)
    try:
        return _apply_text(
            "policy",
            text,
            actor=actor,
            action="commit",
            restores=None,
            approval_id=approval_id,
            expected_base_sha256=expected_base_sha256,
        )
    except Exception as exc:
        if not isinstance(exc, ConfigFanoutError) and approval_id is not None:
            restore_consumed_approval(approval_id)
        raise


def _commit_profile(document: dict[str, Any], actor: str) -> dict[str, Any]:
    overlay = _validate_overlay(document.get("overlay") or {})
    if overlay == _live_profile_document()["overlay"]:
        return _append_live("profile", action="commit", actor=actor)
    mapping = _base_policy_mapping()
    if overlay:
        mapping["profile_defaults"] = overlay
    else:
        mapping.pop("profile_defaults", None)
    text = _dump(mapping)
    _validate_policy_text(text)
    return _apply_text(
        "profile", text, actor=actor, action="commit", restores=None, approval_id=None
    )


def _commit_approval(document: dict[str, Any], actor: str) -> dict[str, Any]:
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise ConfigRevisionError("approval rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("action") != "require_approval":
            raise ConfigRevisionError(
                "every approval rule must be an object with action require_approval"
            )
        _refuse_secrets(rule)
    observed = sorted(config.approval_gated_tools())
    submitted_env = document.get("env_gated_tools")
    if submitted_env is not None and sorted(submitted_env) != observed:
        raise ConfigRevisionError(
            "UA_APPROVAL_TOOLS is observed, not written; omit env_gated_tools "
            "or pass the current set"
        )
    if rules == _live_approval_document()["rules"]:
        return _append_live("approval_rule", action="commit", actor=actor)
    mapping = _base_policy_mapping()
    kept = [
        rule
        for rule in (mapping.get("rules") or [])
        if not (isinstance(rule, dict) and rule.get("action") == "require_approval")
    ]
    mapping["rules"] = kept + list(rules)
    text = _dump(mapping)
    _validate_policy_text(text)
    approval_id = _authorize_approval_change("approval_rule", document, actor)
    try:
        return _apply_text(
            "approval_rule",
            text,
            actor=actor,
            action="commit",
            restores=None,
            approval_id=approval_id,
        )
    except Exception as exc:
        if not isinstance(exc, ConfigFanoutError) and approval_id is not None:
            restore_consumed_approval(approval_id)
        raise


def _commit_org(document: dict[str, Any], actor: str) -> dict[str, Any]:
    live, _raw = _live_org()
    text = _dump(document)
    # Validate before the unchanged fast path: a live chart that no longer
    # passes (an older release or a hand edit) must not be re-recorded as a
    # committed revision just because the caller resubmitted it verbatim.
    _validate_org_text(text)
    if _sha(document) == _sha(live):
        return _append_live("org", action="commit", actor=actor)
    return _apply_text("org", text, actor=actor, action="commit", restores=None, approval_id=None)


def _plan_restore(kind: str, target: dict[str, Any]) -> dict[str, Any]:
    document = target["document"]
    if kind == "policy":
        # Compare stored file text, not the parsed mapping. A comment-only
        # edit keeps the same document hash and must still be restorable.
        raw = target.get("raw") or ""
        path = _policy_path()
        current_raw = path.read_text(encoding="utf-8") if path.exists() else ""
        if current_raw == raw:
            return {"unchanged": True}
        return {"unchanged": False, "text": raw, "delete": raw == ""}
    if kind == "profile":
        overlay = _validate_overlay(document.get("overlay") or {})
        if overlay == _live_profile_document()["overlay"]:
            return {"unchanged": True}
        mapping = _base_policy_mapping()
        if overlay:
            mapping["profile_defaults"] = overlay
        else:
            mapping.pop("profile_defaults", None)
        return {"unchanged": False, "text": _dump(mapping)}
    if kind == "approval_rule":
        rules = document.get("rules") or []
        note = None
        target_env = document.get("env_gated_tools") or []
        observed = sorted(config.approval_gated_tools())
        if list(target_env) != observed:
            note = (
                "env approval allowlist was not restored; "
                "UA_APPROVAL_TOOLS stays at the current process value"
            )
        if rules == _live_approval_document()["rules"]:
            return {"unchanged": True, "note": note}
        mapping = _base_policy_mapping()
        kept = [
            rule
            for rule in (mapping.get("rules") or [])
            if not (isinstance(rule, dict) and rule.get("action") == "require_approval")
        ]
        mapping["rules"] = kept + list(rules)
        return {"unchanged": False, "text": _dump(mapping), "note": note}
    raw = target.get("raw") or ""
    if not raw:
        raise ConfigRevisionError("org revision has no file text to restore")
    _current, current_raw = _live_org()
    if current_raw == raw:
        return {"unchanged": True}
    return {"unchanged": False, "text": raw}


def _validate_planned(kind: str, planned: dict[str, Any]) -> None:
    if planned.get("delete"):
        return
    text = planned.get("text")
    if text is None:
        return
    if kind == "org":
        _validate_org_text(text)
    else:
        _validate_policy_text(text)


def _append_live(
    kind: str,
    *,
    action: str,
    actor: str,
    expected_base_sha256: str | None = None,
) -> dict[str, Any]:
    """Record the current document when it is not already the latest revision."""
    with _lock:
        # Snapshot the mapping and the file text together. Reading either
        # before the lock let a commit land in between and record a mix.
        if expected_base_sha256 is None:
            live = _live_document(kind)
            raw = _raw_for(kind)
            _refuse_secret_text(raw)
            if kind == "policy":
                _validate_team_block(raw)
        conn = storage.connect()
        published: list[dict[str, Any]] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            if expected_base_sha256 is not None:
                # Fenced policy commits must also guard the unchanged/capture
                # path. Read one snapshot after supported writers serialize,
                # then recheck after validation before recording or returning.
                live, raw = _read_policy_file()
                if not raw:
                    live = _default_policy_document()
                _refuse_secret_text(raw)
                _validate_team_block(raw)
                if (
                    _raw_sha(raw) != expected_base_sha256
                    or _policy_raw_sha() != expected_base_sha256
                ):
                    raise ConfigRevisionError("live policy changed since the approved base")
            rows = _load_kind(conn, kind)
            if rows and _same_snapshot(kind, rows[-1], live, raw):
                latest = rows[-1]
                conn.rollback()
                return {
                    "kind": kind,
                    "unchanged": True,
                    "revision": latest["revision"],
                    "ulid": latest["ulid"],
                    "document_sha256": latest["document_sha256"],
                    "action": latest["action"],
                }
            recorded = _record(
                conn,
                published,
                kind=kind,
                action=action,
                actor=actor,
                document=live,
                raw=raw,
                restores=None,
                approval_id=None,
            )
            conn.commit()
            mark_governed_execution()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    try:
        sent, err = _publish(published)
        recorded["unchanged"] = False
        recorded["notification_sent"] = sent
        recorded["notification_error"] = err
        return _result(recorded)
    except Exception as exc:
        recorded["unchanged"] = False
        recorded["notification_sent"] = False
        recorded["notification_error"] = str(exc)[:200]
        res = _result(recorded)
        raise ConfigFanoutError(
            f"Config change committed durably as {kind} revision {res['revision']} "
            f"({res['document_sha256'][:12]}), but event fanout failed: {exc}",
            result=res,
            cause=exc,
        ) from exc


def _apply_text(
    kind: str,
    text: str,
    *,
    actor: str,
    action: str,
    restores: int | None,
    approval_id: int | None,
    expected_base_sha256: str | None = None,
) -> dict[str, Any]:
    path = org_registry.registry_path() if kind == "org" else _policy_path()
    return _mutate_file(
        path,
        text,
        delete=False,
        primary=kind,
        actor=actor,
        action=action,
        restores=restores,
        approval_id=approval_id,
        expected_base_sha256=expected_base_sha256,
    )


def _apply_planned(
    kind: str,
    planned: dict[str, Any],
    *,
    actor: str,
    action: str,
    restores: int | None,
    approval_id: int | None,
) -> dict[str, Any]:
    path = org_registry.registry_path() if kind == "org" else _policy_path()
    try:
        if planned.get("delete"):
            res = _mutate_file(
                path,
                "",
                delete=True,
                primary=kind,
                actor=actor,
                action=action,
                restores=restores,
                approval_id=approval_id,
            )
        else:
            res = _mutate_file(
                path,
                planned["text"],
                delete=False,
                primary=kind,
                actor=actor,
                action=action,
                restores=restores,
                approval_id=approval_id,
            )
    except ConfigFanoutError as exc:
        if planned.get("note"):
            exc.result["note"] = planned["note"]
        raise
    if planned.get("note"):
        res["note"] = planned["note"]
    return res


def _mutate_file(
    path: Path,
    text: str,
    *,
    delete: bool,
    primary: str,
    actor: str,
    action: str,
    restores: int | None,
    approval_id: int | None,
    expected_base_sha256: str | None = None,
) -> dict[str, Any]:
    with _lock:
        conn = storage.connect()
        published: list[dict[str, Any]] = []
        previous = None
        changed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            previous = path.read_bytes() if path.exists() else None
            if expected_base_sha256 is not None and _policy_raw_sha() != expected_base_sha256:
                raise ConfigRevisionError("live policy changed since the approved base")
            if primary != "org":
                # The early preview can race a sprint/run settings write.
                # Recheck under the same DB writer lock those writers hold.
                _validate_policy_text("{}" if delete else text, conn=conn)
            if expected_base_sha256 is not None:
                # Validate against the approved snapshot after the final
                # policy validation, under the DB writer lock shared by
                # supported policy writers. A refusal must not restore over
                # the other writer's file (changed is still false).
                # Match the established text snapshot representation (including
                # universal newline normalization), retaining original bytes
                # separately for rollback.
                if _policy_raw_sha() != expected_base_sha256:
                    raise ConfigRevisionError("live policy changed since the approved base")
            changed = True
            if delete:
                path.unlink(missing_ok=True)
            else:
                _replace_file(path, text)
            if primary == "org" and not delete:
                org_registry.load_registry()
            live = _live_document(primary)
            recorded = _record(
                conn,
                published,
                kind=primary,
                action=action,
                actor=actor,
                document=live,
                raw=_raw_for(primary),
                restores=restores,
                approval_id=approval_id,
            )
            _capture_siblings(conn, published, primary=primary, actor=actor)
            conn.commit()
            # File and revision are durable: fan-out cannot refund authority.
            mark_governed_execution()
        except Exception:
            conn.rollback()
            if changed:
                _restore_bytes(path, previous)
            raise
        finally:
            conn.close()
    try:
        sent, err = _publish(published)
        recorded["unchanged"] = False
        recorded["notification_sent"] = sent
        recorded["notification_error"] = err
        return _result(recorded)
    except Exception as exc:
        recorded["unchanged"] = False
        recorded["notification_sent"] = False
        recorded["notification_error"] = str(exc)[:200]
        res = _result(recorded)
        raise ConfigFanoutError(
            f"Config change committed durably as {primary} revision {res['revision']} "
            f"({res['document_sha256'][:12]}), but event fanout failed: {exc}",
            result=res,
            cause=exc,
        ) from exc


def _capture_siblings(
    conn: Any, published: list[dict[str, Any]], *, primary: str, actor: str
) -> None:
    if primary == "org":
        return
    for kind in ("policy", "profile", "approval_rule"):
        if kind == primary:
            continue
        live = _live_document(kind)
        rows = _load_kind(conn, kind)
        if rows and rows[-1]["document_sha256"] == _sha(live):
            continue
        # Only record a sibling when this write actually moved it, or when
        # the sibling has never been captured. A first policy commit should
        # not invent empty profile history unless the overlay is non-empty
        # or a profile revision already exists.
        if not rows and kind != "policy" and _sha(live) == _sha(_empty_sibling(kind)):
            continue
        _record(
            conn,
            published,
            kind=kind,
            action="capture",
            actor=actor,
            document=live,
            raw=_raw_for(kind),
            restores=None,
            approval_id=None,
        )


def _empty_sibling(kind: str) -> dict[str, Any]:
    if kind == "profile":
        from .profiles import profiles_by_identity

        return {
            "code_defaults": dict(sorted(profiles_by_identity().items())),
            "overlay": {},
        }
    if kind == "approval_rule":
        return {"rules": [], "env_gated_tools": sorted(config.approval_gated_tools())}
    return {}


def _raw_for(kind: str) -> str:
    if kind == "org":
        _doc, raw = _live_org()
        return raw
    if kind == "policy":
        _mapping, raw = _read_policy_file()
        return raw
    # Profile and approval revisions restore a slice. Their raw is the
    # canonical slice, not the whole policy file (that lives on the policy row).
    return _pretty(_live_document(kind))


def _guard_config_path(path: Path) -> None:
    """Refuse a symlink or a path whose resolution leaves its own directory.

    Policy and org files are the configured stores, not workspace paths.
    ``resolve_in_workspace`` would reject a legitimate ``~/.config`` policy
    file. The check here is the redirect: a symlink (or a link that resolves
    outside the configured parent) must not receive the write.
    """
    if path.is_symlink():
        raise ConfigRevisionError("refusing to write config through a symlink")
    if not path.exists():
        return
    resolved = path.resolve()
    root = path.parent.resolve()
    if resolved != root and root not in resolved.parents:
        raise ConfigRevisionError("config path resolves outside its directory")


def _unused_sibling(path: Path, label: str) -> Path:
    """A sibling path that does not exist, so a write cannot delete a draft."""
    parent = path.parent
    for _ in range(8):
        candidate = parent / f".{path.name}.{label}.{os.getpid()}.{os.urandom(4).hex()}"
        if candidate.exists() or candidate.is_symlink():
            continue
        return candidate
    raise ConfigRevisionError("could not allocate a temporary config file")


def _write_exclusive(path: Path, text: str) -> None:
    """Create ``path`` and write ``text``. An existing path is left untouched."""
    if path.is_symlink() or path.exists():
        raise ConfigRevisionError("refusing to replace an existing config file")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    payload = text.encode("utf-8")
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ConfigRevisionError("config write stopped before the file was complete")
            view = view[written:]
    except Exception:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)


def _replace_file(path: Path, text: str) -> None:
    _refuse_secret_text(text)
    _guard_config_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unused_sibling(path, "tmp")
    try:
        _write_exclusive(tmp, text)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _restore_bytes(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(previous)


def _record(
    conn: Any,
    published: list[dict[str, Any]],
    *,
    kind: str,
    action: str,
    actor: str,
    document: dict[str, Any],
    raw: str,
    restores: int | None,
    approval_id: int | None,
) -> dict[str, Any]:
    prior = _load_kind(conn, kind)
    parent = prior[-1]["revision"] if prior else None
    previous = prior[-1]["document"] if prior else None
    revision = (parent or 0) + 1
    payload = {
        "schema": 1,
        "kind": kind,
        "revision": revision,
        "action": action,
        "actor": actor,
        "ts": storage.now(),
        "parent_revision": parent,
        "restores_revision": restores,
        "document_sha256": _sha(document),
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "diff": (
            _text_diff("" if not prior else (prior[-1].get("raw") or ""), raw)
            if kind in ("policy", "org")
            else _make_diff(previous, document)
        ),
        "approval_id": approval_id,
        "document": document,
        "raw": raw,
    }
    _ulid, _inserted, event = events.record_event_once_on_connection(
        conn,
        CHANNEL,
        payload=payload,
        originator=actor,
    )
    if event is not None:
        published.append(event)
        payload["ulid"] = event["ulid"]
        payload["entry_hash"] = event.get("entry_hash")
        payload["prev_hash"] = event.get("prev_hash")
    return payload


def _publish(events_to_send: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if not events_to_send:
        return True, None
    if not config.event_bus_enabled():
        return False, None
    errors: list[str] = []
    delivered = 0
    for event in events_to_send:
        try:
            ok = events.publish(event)
            if ok:
                delivered += 1
            else:
                errors.append(f"event bus delivery failed for {event.get('ulid') or 'event'}")
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        return False, "; ".join(errors)[:200]
    return True, None


def _load_kind(conn: Any, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ulid, payload, originator, created_at, prev_hash, entry_hash "
        "FROM event_log WHERE channel=? ORDER BY ROWID ASC",
        (CHANNEL,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("kind") != kind:
            continue
        payload["ulid"] = row["ulid"]
        payload["entry_hash"] = row["entry_hash"]
        payload["prev_hash"] = row["prev_hash"]
        payload.setdefault("ts", row["created_at"])
        out.append(payload)
    return out


def _find(rows: list[dict[str, Any]], kind: str, revision: int) -> dict[str, Any]:
    for row in rows:
        if row["revision"] == revision:
            return row
    raise ConfigRevisionError(f"no {kind} revision {revision}")


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": row["revision"],
        "action": row["action"],
        "actor": row["actor"],
        "ts": row.get("ts"),
        "ulid": row.get("ulid"),
        "entry_hash": row.get("entry_hash"),
        "prev_hash": row.get("prev_hash"),
        "document_sha256": row["document_sha256"],
        "parent_revision": row.get("parent_revision"),
        "restores_revision": row.get("restores_revision"),
        "approval_id": row.get("approval_id"),
    }


def _result(payload: dict[str, Any]) -> dict[str, Any]:
    res = {
        "kind": payload["kind"],
        "action": payload["action"],
        "revision": payload["revision"],
        "actor": payload["actor"],
        "ts": payload["ts"],
        "ulid": payload.get("ulid"),
        "entry_hash": payload.get("entry_hash"),
        "document_sha256": payload["document_sha256"],
        "parent_revision": payload.get("parent_revision"),
        "restores_revision": payload.get("restores_revision"),
        "approval_id": payload.get("approval_id"),
        "unchanged": False,
        "diff": payload.get("diff") or "",
        "notification_sent": payload.get("notification_sent", True),
        "notification_error": payload.get("notification_error"),
    }
    if "note" in payload:
        res["note"] = payload["note"]
    return res
