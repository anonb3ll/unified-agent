"""Tests for Policy Engine DSL and Causal Episode Guardrails (task #156)."""

import sqlite3
import time
from pathlib import Path

import pytest

from unified_agent import config, events, policy, safety, storage


@pytest.fixture(autouse=True)
def _setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    return db_file


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    pfile = tmp_path / "policy.yaml"
    monkeypatch.setattr(config, "policy_file_path", lambda: pfile)
    return pfile


def _create_episode(episode_id: str, originator: str = "test_agent") -> str:
    """Helper to record an episode start event in event_log."""
    return events.record_event(
        channel="agent.episode.started",
        payload={"action": "start_episode"},
        originator=originator,
        episode_id=episode_id,
    )


# --- 1. DSL Parser Tests ------------------------------------------------------


def test_policy_parser_loads_valid_yaml(policy_file):
    yaml_content = """
version: 1
enabled: true
default_action: allow

rules:
  - name: fatigue_intervention_cap
    description: Cap fatigue interventions per episode
    tool: fatigue_intervention
    type: fatigue_band_cap
    require_episode: true
    bands:
      green: 5
      yellow: 3
      red: 1

  - name: focus_anchor_stabilization
    description: 45s stabilization cooldown on set_focus_anchor()
    tool: set_focus_anchor
    type: cooldown
    cooldown_seconds: 45.0

  - name: realignment_limit
    description: Single-response realignment limit per episode
    tool: realignment
    type: max_calls_per_episode
    max_calls: 1
    require_episode: true
"""
    policy_file.write_text(yaml_content, encoding="utf-8")

    cfg = policy.load_policy(policy_file)
    assert cfg.version == 1
    assert cfg.enabled is True
    assert cfg.default_action == "allow"
    assert len(cfg.rules) == 3

    r1 = cfg.rules[0]
    assert r1.name == "fatigue_intervention_cap"
    assert r1.type == "fatigue_band_cap"
    assert r1.bands == {"green": 5, "yellow": 3, "red": 1}
    assert r1.require_episode is True

    r2 = cfg.rules[1]
    assert r2.name == "focus_anchor_stabilization"
    assert r2.type == "cooldown"
    assert r2.cooldown_seconds == 45.0

    r3 = cfg.rules[2]
    assert r3.name == "realignment_limit"
    assert r3.type == "max_calls_per_episode"
    assert r3.max_calls == 1
    assert r3.require_episode is True


def test_policy_parser_missing_file_uses_default_fallback(tmp_path, monkeypatch):
    nonexistent = tmp_path / "nonexistent_policy.yaml"
    monkeypatch.setattr(config, "policy_file_path", lambda: nonexistent)

    cfg = policy.load_policy(nonexistent)
    assert cfg.enabled is True
    assert len(cfg.rules) >= 3
    rule_names = [r.name for r in cfg.rules]
    assert "fatigue_intervention_cap" in rule_names
    assert "focus_anchor_stabilization" in rule_names
    assert "realignment_limit" in rule_names


def test_policy_parser_invalid_yaml_fails_closed(policy_file):
    policy_file.write_text("version: [invalid yaml syntax ::", encoding="utf-8")

    with pytest.raises(policy.PolicyViolation) as exc_info:
        policy.load_policy(policy_file)
    assert "YAML" in str(exc_info.value) or "policy" in str(exc_info.value).lower()


# --- 2. Stabilization Cooldown Rules -----------------------------------------


def test_stabilization_cooldown_grants_first_call_and_blocks_rapid_second(policy_file):
    yaml_content = """
rules:
  - name: focus_anchor_stabilization
    tool: set_focus_anchor
    type: cooldown
    cooldown_seconds: 45.0
"""
    policy_file.write_text(yaml_content, encoding="utf-8")

    # 1st call: No prior calls in event_log -> permitted
    policy.evaluate_policy("set_focus_anchor", payload={})

    # Record the call in event_log
    events.record_event(
        channel="agent.tool.set_focus_anchor",
        payload={"tool": "set_focus_anchor"},
        originator="test_agent",
    )

    # 2nd call immediately -> blocked by cooldown
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("set_focus_anchor", payload={})
    assert "cooldown active" in str(exc.value).lower() or "stabilization" in str(exc.value).lower()


def test_stabilization_cooldown_passes_after_duration(policy_file, monkeypatch):
    yaml_content = """
rules:
  - name: focus_anchor_stabilization
    tool: set_focus_anchor
    type: cooldown
    cooldown_seconds: 45.0
"""
    policy_file.write_text(yaml_content, encoding="utf-8")

    # Record a call 50 seconds in the past in event_log
    past_time_ms = int((time.time() - 50) * 1000)
    past_ulid = events.new_ulid(ts_ms=past_time_ms)
    from datetime import UTC, datetime, timedelta

    past_iso = (datetime.now(UTC) - timedelta(seconds=50)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, originator, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                past_ulid,
                "agent.tool.set_focus_anchor",
                '{"tool": "set_focus_anchor"}',
                "test_agent",
                past_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Call now (50s > 45s cooldown) -> permitted
    policy.evaluate_policy("set_focus_anchor", payload={})


# --- 3. Single-Response Realignment Limits ------------------------------------


def test_realignment_limit_per_episode(policy_file):
    yaml_content = """
rules:
  - name: realignment_limit
    tool: realignment
    type: max_calls_per_episode
    max_calls: 1
    require_episode: true
"""
    policy_file.write_text(yaml_content, encoding="utf-8")
    ep_id = "ep_realignment_001"
    _create_episode(ep_id)

    # 1st call for episode -> permitted
    policy.evaluate_policy("realignment", payload={}, episode_id=ep_id)

    # Record 1st call
    events.record_event(
        channel="agent.tool.realignment",
        payload={"tool": "realignment"},
        originator="test_agent",
        episode_id=ep_id,
    )

    # 2nd call in same episode -> blocked
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("realignment", payload={}, episode_id=ep_id)
    assert "limit" in str(exc.value).lower() or "max_calls" in str(exc.value).lower()

    # Call under a different valid episode -> permitted
    ep_id2 = "ep_realignment_002"
    _create_episode(ep_id2)
    policy.evaluate_policy("realignment", payload={}, episode_id=ep_id2)


# --- 4. Fatigue Intervention Caps --------------------------------------------


def test_fatigue_band_caps(policy_file):
    yaml_content = """
rules:
  - name: fatigue_intervention_cap
    tool: fatigue_intervention
    type: fatigue_band_cap
    require_episode: true
    bands:
      green: 5
      yellow: 3
      red: 1
"""
    policy_file.write_text(yaml_content, encoding="utf-8")
    ep_red = "ep_fatigue_red"
    _create_episode(ep_red)

    # Under red band (cap=1): 1st call allowed
    policy.evaluate_policy(
        "fatigue_intervention", payload={"fatigue_band": "red"}, episode_id=ep_red
    )
    events.record_event(
        channel="agent.tool.fatigue_intervention",
        payload={"tool": "fatigue_intervention", "fatigue_band": "red"},
        originator="test_agent",
        episode_id=ep_red,
    )

    # 2nd call under red band -> blocked
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy(
            "fatigue_intervention", payload={"fatigue_band": "red"}, episode_id=ep_red
        )
    assert "red" in str(exc.value).lower() or "limit" in str(exc.value).lower()


# --- 5. Fail-Closed Verification Tests ---------------------------------------


def test_fail_closed_missing_episode_id(policy_file):
    yaml_content = """
rules:
  - name: realignment_limit
    tool: realignment
    type: max_calls_per_episode
    max_calls: 1
    require_episode: true
"""
    policy_file.write_text(yaml_content, encoding="utf-8")

    # episode_id is None / missing -> Fail Closed
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("realignment", payload={}, episode_id=None)
    assert "episode_id" in str(exc.value).lower()


def test_fail_closed_episode_not_in_event_log(policy_file):
    yaml_content = """
rules:
  - name: realignment_limit
    tool: realignment
    type: max_calls_per_episode
    max_calls: 1
    require_episode: true
"""
    policy_file.write_text(yaml_content, encoding="utf-8")

    # episode_id is given but NOT recorded in event_log -> Fail Closed
    fake_ep_id = "ep_ghost_999"
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("realignment", payload={}, episode_id=fake_ep_id)
    assert "not found" in str(exc.value).lower() or "event_log" in str(exc.value).lower()


def test_fail_closed_db_error(policy_file, monkeypatch):
    yaml_content = """
rules:
  - name: realignment_limit
    tool: realignment
    type: max_calls_per_episode
    max_calls: 1
    require_episode: true
"""
    policy_file.write_text(yaml_content, encoding="utf-8")
    ep_id = "ep_db_err"
    _create_episode(ep_id)

    # Force connect() to raise a database error
    def bad_connect():
        raise sqlite3.OperationalError("disk I/O error mock")

    monkeypatch.setattr(storage, "connect", bad_connect)

    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("realignment", payload={}, episode_id=ep_id)
    assert "database error" in str(exc.value).lower() or "disk i/o" in str(exc.value).lower()


# --- 6. Integration Chokepoint Tests ------------------------------------------


def test_safety_check_policy_gate(policy_file):
    yaml_content = """
rules:
  - name: realignment_limit
    tool: realignment
    type: max_calls_per_episode
    max_calls: 1
    require_episode: true
"""
    policy_file.write_text(yaml_content, encoding="utf-8")

    ep_id = "ep_chokepoint"
    _create_episode(ep_id)

    # Calls safety.check_policy_gate
    safety.check_policy_gate("realignment", payload={}, episode_id=ep_id)
    events.record_event(
        channel="agent.tool.realignment",
        payload={"tool": "realignment"},
        originator="test_agent",
        episode_id=ep_id,
    )

    with pytest.raises(policy.PolicyViolation):
        safety.check_policy_gate("realignment", payload={}, episode_id=ep_id)


# --- 7. Phase 3 — tool / role / threshold rules (task #1031) -----------------


def test_phase3_tool_deny(policy_file):
    policy_file.write_text(
        """
rules:
  - name: deny_task_delete
    type: tool
    tool: task_delete
    action: deny
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="denied by policy rule"):
        policy.evaluate_policy("task_delete", payload={})


def test_phase3_tool_require_approval_parks(policy_file, monkeypatch):
    from unified_agent.tools.approvals import ApprovalPendingError, approval_pending

    monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
    policy_file.write_text(
        """
rules:
  - name: approve_run_cmd
    type: tool
    tool: run_cmd
    action: require_approval
""",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalPendingError):
        policy.evaluate_policy("run_cmd", payload={"command": ["ls"]})
    pending = approval_pending(tool="run_cmd")["pending"]
    assert len(pending) == 1
    assert pending[0]["payload"]["command"] == ["ls"]


def test_phase3_tool_require_approval_fails_closed_on_gate_error(policy_file, monkeypatch):
    policy_file.write_text(
        """
rules:
  - name: approve_run_cmd
    type: tool
    tool: run_cmd
    action: require_approval
""",
        encoding="utf-8",
    )

    def boom(*_a, **_k):
        raise RuntimeError("approval store unavailable")

    monkeypatch.setattr("unified_agent.tools.approvals.check_approval_gate", boom)
    with pytest.raises(policy.PolicyViolation, match="failed closed"):
        policy.evaluate_policy("run_cmd", payload={"command": ["ls"]})


def test_phase3_role_allow_list_denies_outsiders(policy_file, monkeypatch):
    policy_file.write_text(
        """
rules:
  - name: steward_only
    type: role
    tool: approval_resolve
    roles: [steward, chief-of-staff]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent4")
    monkeypatch.setattr("unified_agent.profiles.current", lambda: "developer")
    with pytest.raises(policy.PolicyViolation, match="role policy"):
        policy.evaluate_policy("approval_resolve", payload={}, originator="Desktop_Agent4")


def test_phase3_role_allow_list_permits_matching_profile(policy_file, monkeypatch):
    policy_file.write_text(
        """
rules:
  - name: steward_only
    type: role
    tool: approval_resolve
    roles: [steward, chief-of-staff]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent5")
    monkeypatch.setattr("unified_agent.profiles.current", lambda: "steward")
    policy.evaluate_policy("approval_resolve", payload={}, originator="Desktop_Agent5")


def test_phase3_threshold_breaches_and_missing_field_fail_closed(policy_file):
    policy_file.write_text(
        """
rules:
  - name: budget_cap
    type: threshold
    tool: "*"
    field: budget_usd
    max: 5.0
    action: deny
""",
        encoding="utf-8",
    )
    policy.evaluate_policy("gemini_ask", payload={"budget_usd": 4.5})
    with pytest.raises(policy.PolicyViolation, match="denied by policy rule"):
        policy.evaluate_policy("gemini_ask", payload={"budget_usd": 5.01})
    with pytest.raises(policy.PolicyViolation, match="fail-closed: missing value"):
        policy.evaluate_policy("gemini_ask", payload={})


def test_phase3_threshold_nested_field_and_min_bound(policy_file):
    policy_file.write_text(
        """
rules:
  - name: confidence_floor
    type: threshold
    tool: memory_promote
    field: meta.confidence
    min: 0.7
    action: deny
""",
        encoding="utf-8",
    )
    policy.evaluate_policy("memory_promote", payload={"meta": {"confidence": 0.9}})
    with pytest.raises(policy.PolicyViolation):
        policy.evaluate_policy("memory_promote", payload={"meta": {"confidence": 0.2}})


def test_phase3_tool_rule_scoped_by_roles(policy_file, monkeypatch):
    policy_file.write_text(
        """
rules:
  - name: research_cannot_delegate
    type: tool
    tool: delegate
    roles: [research]
    action: deny
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("unified_agent.profiles.current", lambda: "developer")
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent4")
    # Developer is outside the role scope → rule does not apply → allowed.
    policy.evaluate_policy("delegate", payload={}, originator="Desktop_Agent4")

    monkeypatch.setattr("unified_agent.profiles.current", lambda: "research")
    with pytest.raises(policy.PolicyViolation, match="denied by policy rule"):
        policy.evaluate_policy("delegate", payload={}, originator="Desktop_Agent8")


def test_phase3_malformed_rules_fail_closed(policy_file):
    policy_file.write_text(
        """
rules:
  - name: bad_threshold
    type: threshold
    tool: "*"
    action: deny
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="requires a 'field'"):
        policy.load_policy(policy_file)

    policy_file.write_text(
        """
rules:
  - name: bad_role
    type: role
    tool: approval_resolve
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="non-empty 'roles'"):
        policy.load_policy(policy_file)

    policy_file.write_text(
        """
rules:
  - name: bad_type
    type: invent_me
    tool: "*"
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="Unknown policy rule type"):
        policy.load_policy(policy_file)


def test_phase3_example_yaml_loads(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "policy.phase3.yaml"
    assert example.is_file()
    cfg = policy.load_policy(example)
    assert cfg.version == 2
    assert cfg.enabled is True
    kinds = {r.type for r in cfg.rules}
    assert {"tool", "role", "threshold"}.issubset(kinds)


# --- 8. Non-finite threshold values and bounds (task #1219) ------------------
#
# _parse_optional_float previously accepted NaN/inf (float() happily parses
# "NaN", "Infinity", etc.), and the threshold-rule numeric coercion in
# evaluate_policy did the same for candidate payload values. Because a NaN
# comparison (`numeric > max_value`, `numeric < min_value`) always returns
# False, a non-finite bound or candidate silently satisfied every threshold
# rule instead of tripping one — a fail-open. Both sites must now reject
# non-finite values by raising PolicyViolation (fail-closed), matching how
# a non-numeric value is already handled one line above each check.


@pytest.mark.parametrize(
    "raw",
    [float("nan"), "NaN", float("inf"), float("-inf"), "Infinity", "-Infinity", "inf"],
)
def test_parse_optional_float_rejects_non_finite(raw):
    with pytest.raises(policy.PolicyViolation, match="finite number"):
        policy._parse_optional_float(raw, key="max")


@pytest.mark.parametrize("raw", [0, 1, -1, 3.5, "3.5", "-2", None])
def test_parse_optional_float_accepts_finite_and_none(raw):
    result = policy._parse_optional_float(raw, key="max")
    if raw is None:
        assert result is None
    else:
        assert result == float(raw)


def test_parse_optional_float_still_rejects_non_numeric():
    with pytest.raises(policy.PolicyViolation, match="must be a number"):
        policy._parse_optional_float("not-a-number", key="max")


def test_phase3_threshold_config_rejects_non_finite_max_bound(policy_file):
    policy_file.write_text(
        """
rules:
  - name: budget_cap
    type: threshold
    tool: "*"
    field: budget_usd
    max: .nan
    action: deny
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="finite number"):
        policy.load_policy(policy_file)


def test_phase3_threshold_config_rejects_non_finite_min_bound(policy_file):
    policy_file.write_text(
        """
rules:
  - name: confidence_floor
    type: threshold
    tool: memory_promote
    field: meta.confidence
    min: "Infinity"
    action: deny
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="finite number"):
        policy.load_policy(policy_file)


def test_cooldown_rule_rejects_non_finite_cooldown_seconds(policy_file):
    policy_file.write_text(
        """
rules:
  - name: bad_cooldown
    type: cooldown
    tool: some_tool
    cooldown_seconds: .nan
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="finite number"):
        policy.load_policy(policy_file)


@pytest.mark.parametrize(
    "candidate",
    [float("nan"), "NaN", float("inf"), "Infinity", float("-inf"), "-Infinity"],
)
def test_phase3_threshold_evaluate_rejects_non_finite_candidate(policy_file, candidate):
    policy_file.write_text(
        """
rules:
  - name: budget_cap
    type: threshold
    tool: "*"
    field: budget_usd
    max: 5.0
    action: deny
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="finite number"):
        policy.evaluate_policy("gemini_ask", payload={"budget_usd": candidate})


def test_phase3_threshold_evaluate_finite_regression_still_works(policy_file):
    policy_file.write_text(
        """
rules:
  - name: budget_cap
    type: threshold
    tool: "*"
    field: budget_usd
    max: 5.0
    action: deny
""",
        encoding="utf-8",
    )
    # Finite happy path: under the cap, allowed.
    policy.evaluate_policy("gemini_ask", payload={"budget_usd": 4.99})
    # Finite sad path: over the cap, denied.
    with pytest.raises(policy.PolicyViolation, match="denied by policy rule"):
        policy.evaluate_policy("gemini_ask", payload={"budget_usd": 5.01})


# --- 9. Huge integers (bigger than float range) also fail closed (task #1219,
# Devin review finding on PR #639) -------------------------------------------
#
# float(10**400) raises OverflowError, not TypeError/ValueError, since Python
# converts an int to float only while it fits the float range. Both
# _parse_optional_float and the threshold coercion in evaluate_policy caught
# only TypeError/ValueError, so a huge integer bound or candidate value
# escaped as an unhandled OverflowError instead of PolicyViolation.
#
# Note: the *string* form of a huge integer (e.g. "10" + "0" * 400) does not
# raise OverflowError — float() parses an out-of-range decimal string as
# `inf` instead — so it is already caught by the pre-existing non-finite
# check, not the new OverflowError catch. It's exercised here anyway per the
# review finding's "test the string form if reachable" ask.

_HUGE_INT = 10**400


@pytest.mark.parametrize("raw", [_HUGE_INT, -_HUGE_INT])
def test_parse_optional_float_rejects_huge_integer(raw):
    with pytest.raises(policy.PolicyViolation, match="must be a number"):
        policy._parse_optional_float(raw, key="max")


def test_parse_optional_float_rejects_huge_integer_string_as_non_finite():
    # str(10**400) parses via float() to inf, not an OverflowError, so this
    # is caught by the isfinite() check rather than the new except clause.
    with pytest.raises(policy.PolicyViolation, match="finite number"):
        policy._parse_optional_float(str(_HUGE_INT), key="max")


def test_phase3_threshold_config_rejects_huge_integer_max_bound(policy_file):
    policy_file.write_text(
        f"""
rules:
  - name: budget_cap
    type: threshold
    tool: "*"
    field: budget_usd
    max: {_HUGE_INT}
    action: deny
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="must be a number"):
        policy.load_policy(policy_file)


@pytest.mark.parametrize("candidate", [_HUGE_INT, -_HUGE_INT])
def test_phase3_threshold_evaluate_rejects_huge_integer_candidate(policy_file, candidate):
    policy_file.write_text(
        """
rules:
  - name: budget_cap
    type: threshold
    tool: "*"
    field: budget_usd
    max: 5.0
    action: deny
""",
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyViolation, match="must be numeric"):
        policy.evaluate_policy("gemini_ask", payload={"budget_usd": candidate})


def test_phase3_threshold_evaluate_rejects_huge_integer_string_candidate(policy_file):
    policy_file.write_text(
        """
rules:
  - name: budget_cap
    type: threshold
    tool: "*"
    field: budget_usd
    max: 5.0
    action: deny
""",
        encoding="utf-8",
    )
    # String form parses to inf, caught by the finiteness check instead.
    with pytest.raises(policy.PolicyViolation, match="finite number"):
        policy.evaluate_policy("gemini_ask", payload={"budget_usd": str(_HUGE_INT)})
