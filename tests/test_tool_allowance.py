"""Per-tool call allowance view (task #2144).

The view must answer "how many calls are left" from the same counter the
policy gate enforces against — the in-process sliding-window buckets in
``gateway._LIMITS`` — so the load-bearing test drives the limiter to its cap
and asserts remaining=0/stopped exactly where enforcement starts refusing.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, gateway, policy, profiles, server, storage
from unified_agent.monitor import observability_manage
from unified_agent.tool_allowance import tool_allowance
from unified_agent.webui import routes as webui_routes

_REAL_PROFILES_BY_IDENTITY = profiles.profiles_by_identity


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_path))
    gateway.reset_limits()
    gateway.reset_coalesced()
    yield policy_path


@contextlib.contextmanager
def _verified(agent: str):
    token = config.agent_identity_var.set(agent)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _scope(agent: str, **kwargs) -> gateway.CallScope:
    return gateway.CallScope(agent=agent, profile=kwargs.pop("profile", "developer"), **kwargs)


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


_ANY = object()


def _row(view: dict, tool: str, rule: str | None = None, agent: object = _ANY) -> dict:
    for row in view["tools"]:
        if (
            row["tool"] == tool
            and (rule is None or row["rule"] == rule)
            and (agent is _ANY or row["agent"] == agent)
        ):
            return row
    raise AssertionError(f"no row for {tool!r} rule {rule!r} in {view['tools']}")


_CAP = (
    "rules:\n  - name: read_cap\n    type: rate_limit\n    tool: fs_read\n"
    "    max_calls: 2\n    window_seconds: 60\n"
)

_CAP_ONE = (
    "rules:\n  - name: read_cap\n    type: rate_limit\n    tool: fs_read\n"
    "    max_calls: 1\n    window_seconds: 60\n"
)

_GLOB_CAP = (
    "rules:\n  - name: reads\n    type: rate_limit\n    tool: fs_*\n"
    "    max_calls: 1\n    window_seconds: 60\n"
)


def test_allowance_reports_stopped_exactly_where_enforcement_refuses(_isolated):
    _write(_isolated, _CAP)
    scope = _scope("agent_beta")
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=scope)
        view = tool_allowance(now=1000)
        row = _row(view, "fs_read", "read_cap")
        assert (row["used"], row["limit"], row["remaining"]) == (1, 2, 1)
        assert row["stopped"] is False and row["current"] is True

        # The call that fills the bucket is still admitted; the view agrees.
        policy.evaluate_policy("fs_read", now=1010, call_scope=scope)
        view = tool_allowance(now=1010)
        row = _row(view, "fs_read", "read_cap")
        assert (row["used"], row["remaining"], row["stopped"]) == (2, 0, True)
        assert row["resets_in_seconds"] == pytest.approx(50.0)

        # The next call is refused at exactly the point the view says stopped.
        with pytest.raises(policy.PolicyViolation) as exc:
            policy.evaluate_policy("fs_read", now=1011, call_scope=scope)
        assert exc.value.reason_code == "rate_limited"
        assert _row(tool_allowance(now=1011), "fs_read")["stopped"] is True

        # Once the oldest charge leaves the window the view opens again.
        view = tool_allowance(now=1060)
        row = _row(view, "fs_read")
        assert (row["used"], row["remaining"], row["stopped"]) == (1, 1, False)
        policy.evaluate_policy("fs_read", now=1060, call_scope=scope)


def test_allowance_is_per_caller(_isolated):
    _write(_isolated, _CAP)
    policy.evaluate_policy("fs_read", now=1000, call_scope=_scope("agent_beta"))
    with _verified("agent_alpha"):
        view = tool_allowance(now=1000)
        row = _row(view, "fs_read", "read_cap")
        # No bucket for this caller: the configured limit shows at full.
        assert (row["used"], row["remaining"], row["stopped"]) == (0, 2, False)


def test_unlimited_tools_appear_only_with_usage(_isolated):
    events.record_event(
        channel=gateway.CHANNEL_GATEWAY_DECISION,
        payload={
            "decision": "allow",
            "reason_code": "allowed",
            "rule": "",
            "tool_name": "task_list",
            "caller": "agent_beta",
            "profile": "developer",
        },
        originator="agent_beta",
        notify=False,
    )
    events.record_event(
        channel=gateway.CHANNEL_GATEWAY_DECISION_ROLLUP,
        payload={
            "decision": "allow",
            "reason_code": "allowed",
            "rule": "",
            "tool_name": "memory_search",
            "caller": "agent_beta",
            "profile": "developer",
            "count": 7,
        },
        originator="agent_beta",
        notify=False,
    )
    with _verified("agent_beta"):
        view = tool_allowance()
    by_tool = {r["tool"]: r for r in view["tools"]}
    assert set(by_tool) == {"task_list", "memory_search"}
    assert by_tool["task_list"]["limit"] is None and by_tool["task_list"]["used_today"] == 1
    assert by_tool["memory_search"]["limit"] is None and by_tool["memory_search"]["used_today"] == 7
    # A deny consumes no allowance and does not create a usage row.
    events.record_event(
        channel=gateway.CHANNEL_GATEWAY_DECISION,
        payload={
            "decision": "deny",
            "reason_code": "policy_deny",
            "rule": "x",
            "tool_name": "fs_write",
            "caller": "agent_beta",
            "profile": "developer",
        },
        originator="agent_beta",
        notify=False,
    )
    with _verified("agent_beta"):
        assert "fs_write" not in {r["tool"] for r in tool_allowance()["tools"]}


def test_rollup_is_counted_on_its_bucket_day_not_its_write_day(_isolated):
    """A rollup row is stamped when it flushes; its calls belong to the day
    the bucket started. A bucket that began before UTC midnight must not
    count toward today no matter when the row was written (#2144 r4)."""
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for bucket, count in (
        (midnight - timedelta(hours=1), 5),  # the whole bucket ran yesterday
        (midnight, 3),  # today's first hourly bucket
    ):
        events.record_event(
            channel=gateway.CHANNEL_GATEWAY_DECISION_ROLLUP,
            payload={
                "decision": "allow",
                "reason_code": "allowed",
                "rule": "",
                "tool_name": "memory_search",
                "caller": "agent_beta",
                "profile": "developer",
                "count": count,
                "bucket": bucket.isoformat(timespec="seconds"),
                "bucket_s": 3600,
            },
            originator="agent_beta",
            notify=False,
        )
    with _verified("agent_beta"):
        view = tool_allowance()
    assert _row(view, "memory_search")["used_today"] == 3


def test_pending_coalesced_reads_count_toward_today(_isolated):
    gateway._COALESCER.add(
        ("agent_beta", "task_list", "developer", "allow", None), None, time.time(), 3600
    )
    with _verified("agent_beta"):
        view = tool_allowance()
    assert _row(view, "task_list")["used_today"] == 1


def test_pending_reads_in_a_prior_day_bucket_do_not_count(_isolated):
    """Pending coalescer counts share one bucket; one that began before UTC
    midnight holds none of today's calls (#2144 r4)."""
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    gateway._COALESCER.add(
        ("agent_beta", "task_list", "developer", "allow", None),
        None,
        midnight.timestamp() - 1,
        3600,
    )
    with _verified("agent_beta"):
        assert "task_list" not in {r["tool"] for r in tool_allowance()["tools"]}
    # A call in today's bucket still counts — and draining the stale bucket
    # to the ledger keeps it out too, via the rollup's own bucket stamp.
    gateway._COALESCER.add(
        ("agent_beta", "task_list", "developer", "allow", None), None, time.time(), 3600
    )
    with _verified("agent_beta"):
        assert _row(tool_allowance(), "task_list")["used_today"] == 1


def test_session_subjects_report_used_today_unknown(_isolated, monkeypatch):
    """An unverified MCP session is charged under ``session:<id>``, but the
    ledger records its decisions under the empty caller shared by every
    unverified caller — the per-subject count cannot be known, so the row
    reports ``used_today: null`` rather than a false 0 (#2144 r4)."""
    _write(_isolated, _CAP)
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    session = config.mcp_session_id_var.set("sess-anon")
    try:
        policy.evaluate_policy("fs_read", now=1000, call_scope=_scope("session:sess-anon"))
        view = tool_allowance(now=1001)
        row = _row(view, "fs_read", "read_cap", agent="session:sess-anon")
        assert row["used"] == 1 and row["current"] is True
        assert row["used_today"] is None and row["used_today_reason"]

        # A glob rule's pattern row for the same subject is null too.
        _write(_isolated, _GLOB_CAP)
        view = tool_allowance(now=2000)
    finally:
        config.mcp_session_id_var.reset(session)
        with profiles._lock:
            profiles._active.clear()
    pattern = [r for r in view["tools"] if r.get("pattern")]
    assert len(pattern) == 1 and pattern[0]["agent"] == "session:sess-anon"
    assert pattern[0]["used_today"] is None and pattern[0]["used_today_reason"]


def test_cross_identity_read_needs_the_operator(_isolated, monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_LABEL", "human_operator")
    with _verified("agent_beta"):
        with pytest.raises(ToolError, match="per-caller"):
            tool_allowance("agent_alpha")
        with pytest.raises(ToolError, match="per-caller"):
            tool_allowance("*")
    with _verified("human_operator"):
        assert tool_allowance("agent_alpha")["agent"] == "agent_alpha"
        assert tool_allowance("*")["agent"] is None
        # The operator's own calls are rare: the fleet view is the default.
        assert tool_allowance()["agent"] is None


def test_an_unverified_caller_cannot_read_another_identity(_isolated, monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_LABEL", "human_operator")
    # No verified identity and no MCP session: the caller key is empty, which
    # must never pass the operator check, whatever the operator label is.
    with pytest.raises(ToolError, match="per-caller"):
        tool_allowance("agent_alpha")
    with pytest.raises(ToolError, match="per-caller"):
        tool_allowance("*")
    assert tool_allowance()["agent"] == ""


def test_today_count_is_fenced_to_this_tenant(_isolated, monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _decision():
        events.record_event(
            channel=gateway.CHANNEL_GATEWAY_DECISION,
            payload={
                "decision": "allow",
                "reason_code": "allowed",
                "rule": "",
                "tool_name": "task_list",
                "caller": "agent_beta",
                "profile": "developer",
            },
            originator="agent_beta",
            notify=False,
        )

    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_other")
    _decision()
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_this")
    _decision()
    _decision()
    with _verified("agent_beta"):
        assert _row(tool_allowance(), "task_list")["used_today"] == 2


def test_observability_manage_dispatches_tool_allowance(_isolated, monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("agent_alpha")
    session = config.mcp_session_id_var.set("allowance-test")
    try:
        view = observability_manage("tool_allowance")
        assert view["agent"] == "agent_alpha"
        assert view["tools"] == []
        with pytest.raises(ToolError, match="per-caller"):
            observability_manage("tool_allowance", {"agent": "other"})
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.clear()


def test_observability_manage_tool_allowance_profile_gate(_isolated, monkeypatch):
    """Allow-list profiles keep their action set: no allowance side door."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    # A neutral label whose least-privilege default is the allow-list profile.
    monkeypatch.setattr(
        profiles,
        "profiles_by_identity",
        lambda: {**_REAL_PROFILES_BY_IDENTITY(), "agent_delta": profiles.PUBLIC},
    )
    identity = config.agent_identity_var.set("agent_delta")
    session = config.mcp_session_id_var.set("allowance-public")
    try:
        with pytest.raises(ToolError, match="not exposed"):
            observability_manage("tool_allowance")
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.clear()


def _client():
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui_routes.web_routes()))
    return TestClient(app, base_url="http://127.0.0.1")


def test_http_route_is_tenant_fenced(_isolated, monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_OPERATOR_LABEL", "human_operator")
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        "agent_alpha:tok-dev,human_operator:tok-op",
    )
    client = _client()
    dev = {"Authorization": "Bearer tok-dev"}
    op = {"Authorization": "Bearer tok-op"}

    res = client.get("/api/tool-allowance", headers=dev)
    assert res.status_code == 200, res.text
    assert res.json()["agent"] == "agent_alpha"

    res = client.get("/api/tool-allowance?agent=Other", headers=dev)
    assert res.status_code == 403

    res = client.get("/api/tool-allowance?agent=agent_alpha", headers=op)
    assert res.status_code == 200 and res.json()["agent"] == "agent_alpha"
    res = client.get("/api/tool-allowance?agent=*", headers=op)
    assert res.status_code == 200 and res.json()["agent"] is None

    assert client.get("/api/tool-allowance").status_code == 401


def test_glob_rule_keeps_a_pattern_row_for_uncharged_tools(_isolated):
    """Enforcement buckets are per concrete tool (gateway.reserve_rate_limits):
    a full `fs_read` bucket under an `fs_*` rule must not hide that `fs_write`
    still has its own allowance (#2144 P2-1)."""
    _write(_isolated, _GLOB_CAP)
    scope = _scope("agent_beta")
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=scope)
        view = tool_allowance(now=1001)
        charged = _row(view, "fs_read", "reads")
        assert (charged["used"], charged["stopped"]) == (1, True)
        pattern = [r for r in view["tools"] if r.get("pattern")]
        assert len(pattern) == 1
        assert (pattern[0]["tool"], pattern[0]["rule"]) == ("fs_*", "reads")
        assert (pattern[0]["remaining"], pattern[0]["stopped"]) == (1, False)

        # Enforcement agrees: a different matching tool is still admitted —
        # and once charged, it shows beside the pattern row.
        policy.evaluate_policy("fs_write", now=1001, call_scope=scope)
        view = tool_allowance(now=1001)
        assert _row(view, "fs_write", "reads")["stopped"] is True
        assert len([r for r in view["tools"] if r.get("pattern")]) == 1


def test_literal_rule_bucket_still_suppresses_the_pattern_row(_isolated):
    """A literal-tool rule's bucket IS the whole allowance: no duplicate row."""
    _write(_isolated, _CAP)
    scope = _scope("agent_beta")
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=scope)
        view = tool_allowance(now=1001)
        rows = [r for r in view["tools"] if r["rule"] == "read_cap"]
        assert len(rows) == 1 and not rows[0].get("pattern")


def test_fleet_glob_rule_keeps_a_pattern_row(_isolated, monkeypatch):
    """Fleet view has the same collapse: one agent's fs_read bucket must not
    suppress the rule row other agents' matching tools are charged under."""
    monkeypatch.setenv("UA_OPERATOR_LABEL", "human_operator")
    _write(_isolated, _GLOB_CAP)
    policy.evaluate_policy("fs_read", now=1000, call_scope=_scope("agent_beta"))
    with _verified("human_operator"):
        view = tool_allowance(now=1001)
    charged = _row(view, "fs_read", "reads")
    assert charged["agent"] == "agent_beta" and charged["stopped"] is True
    pattern = [r for r in view["tools"] if r.get("pattern")]
    assert len(pattern) == 1
    assert (pattern[0]["agent"], pattern[0]["tool"], pattern[0]["rule"]) == (
        None,
        "fs_*",
        "reads",
    )


def test_superseded_generation_never_reports_stopped(_isolated):
    """An edited rule retires its bucket generation; the next gate call drops
    it via reconcile_rate_rules, so the view must not keep showing the old
    generation stopped (#2144 P2-2)."""
    _write(_isolated, _CAP_ONE)
    scope = _scope("agent_beta")
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=scope)
        # Widen the cap: the old generation's full bucket is stale.
        _write(
            _isolated,
            "rules:\n  - name: read_cap\n    type: rate_limit\n    tool: fs_read\n"
            "    max_calls: 5\n    window_seconds: 60\n",
        )
        view = tool_allowance(now=1001)
        assert all(r["stopped"] is False for r in view["tools"])
        # The old generation's bucket stays visible with its own cap; the
        # current rule stands beside it at full remaining.
        stale = _row(view, "fs_read", "read_cap")
        assert stale["current"] is False and (stale["used"], stale["limit"]) == (1, 1)
        pattern = [r for r in view["tools"] if r.get("pattern")]
        assert len(pattern) == 1
        assert (pattern[0]["limit"], pattern[0]["remaining"]) == (5, 5)
        # The gate agrees: the next call under the new generation is admitted.
        policy.evaluate_policy("fs_read", now=1001, call_scope=scope)


def test_explicit_namespace_bucket_never_reports_stopped(_isolated):
    """Buckets charged under an explicit policy_config keep their own
    `explicit:` generation, which is never the current one — so they are
    visible but cannot render stopped (#2144 P2-2)."""
    _write(_isolated, _CAP_ONE)
    explicit = policy.load_policy()
    scope = _scope("agent_beta")
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=scope, policy_config=explicit)
        view = tool_allowance(now=1001)
        row = _row(view, "fs_read", "read_cap")
        assert row["current"] is False and row["stopped"] is False
        assert (row["used"], row["limit"], row["remaining"]) == (1, 1, 0)
        # The configured rule still stands beside it at full remaining.
        pattern = [r for r in view["tools"] if r.get("pattern")]
        assert len(pattern) == 1 and pattern[0]["remaining"] == 1
        # And the current generation really is uncharged.
        policy.evaluate_policy("fs_read", now=1001, call_scope=scope)


def _claim_task(claimed_by: str, title: str) -> int:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (title, status, claimed_by, project, created_at, updated_at) "
            "VALUES (?, 'doing', ?, 'unified-agent', 't', 't')",
            (title, claimed_by),
        )
        conn.commit()
        return int(conn.execute("SELECT id FROM tasks WHERE title=?", (title,)).fetchone()["id"])
    finally:
        conn.close()


def test_stopped_row_rechecks_the_current_bind(_isolated):
    """A bucket charged under a bound claim stops nothing once the caller's
    live claims moved on: the view applies the gate's bind filter to the
    current scope (#2144 P3/P2-3)."""
    held_id = _claim_task("agent_beta", "held-now")
    bound_id = _claim_task("agent_alpha", "bound-elsewhere")
    _write(
        _isolated,
        "rules:\n  - name: task_reads\n    type: rate_limit\n    tool: fs_read\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        f'    bind:\n      task: "{bound_id}"\n',
    )
    # Charged while the bound task was claimed; that claim's calls are refused.
    bound_scope = _scope(
        "agent_beta",
        claims=frozenset({gateway.ClaimBind(project="unified-agent", task=str(bound_id))}),
    )
    policy.evaluate_policy("fs_read", now=100, call_scope=bound_scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("fs_read", now=101, call_scope=bound_scope)
    # The same identity now holds a different task: the gate admits.
    moved_scope = _scope(
        "agent_beta",
        claims=frozenset({gateway.ClaimBind(project="unified-agent", task=str(held_id))}),
    )
    policy.evaluate_policy("fs_read", now=102, call_scope=moved_scope)
    with _verified("agent_beta"):
        view = tool_allowance(now=103)
    row = _row(view, "fs_read", "task_reads")
    assert row["used"] == 1 and row["stopped"] is False
    assert row["applies"] is False and row["applies_reason"]


def test_stopped_row_rechecks_the_current_roles(_isolated, monkeypatch):
    """A role-scoped rule charged as steward stops nothing once the caller's
    profile is developer: the view applies the gate's role filter (#2144 P2-3)."""
    _write(
        _isolated,
        "rules:\n  - name: steward_reads\n    type: rate_limit\n    tool: fs_read\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        "    roles: [steward]\n",
    )
    monkeypatch.setattr(profiles, "current", lambda: "steward")
    scope = _scope("agent_beta")
    policy.evaluate_policy("fs_read", now=100, call_scope=scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("fs_read", now=101, call_scope=scope)
    monkeypatch.setattr(profiles, "current", lambda: "developer")
    # As a developer the rule does not apply: the gate admits.
    policy.evaluate_policy("fs_read", now=102, call_scope=scope)
    with _verified("agent_beta"):
        view = tool_allowance(now=103)
    row = _row(view, "fs_read", "steward_reads")
    assert row["used"] == 1 and row["stopped"] is False
    assert row["applies"] is False and row["applies_reason"]


def test_operator_view_of_a_foreign_subject_scopes_that_subject(_isolated, monkeypatch):
    """For `agent=<other>` the bind/roles check resolves THAT subject's claims,
    not the operator's (#2144 P2-3)."""
    monkeypatch.setenv("UA_OPERATOR_LABEL", "human_operator")
    _claim_task("agent_beta", "cursor-held")
    bound_id = _claim_task("agent_alpha", "cursor-unbound")
    _write(
        _isolated,
        "rules:\n  - name: task_reads\n    type: rate_limit\n    tool: fs_read\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        f'    bind:\n      task: "{bound_id}"\n',
    )
    bound_scope = _scope(
        "agent_beta",
        claims=frozenset({gateway.ClaimBind(project="unified-agent", task=str(bound_id))}),
    )
    policy.evaluate_policy("fs_read", now=100, call_scope=bound_scope)
    with _verified("human_operator"):
        row = _row(tool_allowance("agent_beta", now=101), "fs_read", "task_reads")
    assert row["stopped"] is False and row["applies"] is False
    # Hand the bound claim back to the subject: the same row stops again.
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET claimed_by='agent_beta' WHERE id=?", (bound_id,))
        conn.commit()
    finally:
        conn.close()
    with _verified("human_operator"):
        row = _row(tool_allowance("agent_beta", now=102), "fs_read", "task_reads")
    assert row["stopped"] is True and row["applies"] is True


def test_fleet_rows_carry_the_rules_bind_and_roles(_isolated, monkeypatch):
    """Fleet rows attach the rule's bind/roles and re-check the bind against
    the bucket owner's live claims; a role scope the view cannot confirm (the
    owner's session profile is not visible) reports ``applies: null`` and is
    never stopped (#2144 P2-B)."""
    monkeypatch.setenv("UA_OPERATOR_LABEL", "human_operator")
    _write(
        _isolated,
        "rules:\n  - name: dev_reads\n    type: rate_limit\n    tool: fs_read\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        "    roles: [developer]\n"
        "    bind:\n      project: unified-agent\n",
    )
    scope = _scope(
        "agent_beta",
        claims=frozenset({gateway.ClaimBind(project="unified-agent", task="7")}),
    )
    policy.evaluate_policy("fs_read", now=100, call_scope=scope)
    with _verified("human_operator"):
        view = tool_allowance(now=101)
    row = _row(view, "fs_read", "dev_reads", agent="agent_beta")
    assert row["stopped"] is False
    assert row["bind"] == {"project": "unified-agent"}
    assert row["roles"] == ["developer"]
    assert row["applies"] is None
    assert "session profile not visible" in row["applies_reason"]


def test_ledger_failure_surfaces_as_ledger_error(_isolated, monkeypatch):
    """A failed decision-ledger read must not silently report zeros (#2144 P3-2)."""

    class _BrokenConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.Error("disk image malformed")

        def close(self):
            pass

    monkeypatch.setattr(storage, "connect", lambda: _BrokenConn())
    with _verified("agent_beta"):
        view = tool_allowance()
    assert "disk image malformed" in view["ledger_error"]


def test_allowance_ledger_query_uses_the_channel_created_index(_isolated):
    """The ledger scan must not walk event_log on a private hub where every
    org_id is NULL (#2144 P3-1)."""
    from unified_agent import tool_allowance as ta

    conn = storage.connect()
    try:
        for sql, params in (
            (ta._LEDGER_SQL_FLEET, ("a", "b", "2026-01-01", None)),
            (ta._LEDGER_SQL_SUBJECT, ("a", "b", "2026-01-01", None, "agent_beta")),
        ):
            plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            detail = " ".join(str(row["detail"]) for row in plan)
            assert "idx_event_log_channel_created" in detail, detail
            assert "SCAN event_log" not in detail, detail
    finally:
        conn.close()


def test_capacity_reports_the_live_bucket_table(_isolated):
    """A NEW bucket can be refused at MAX_BUCKETS even while every visible row
    has remaining; the view exposes the table fill (#2144 P3-3)."""
    _write(_isolated, _CAP)
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=_scope("agent_beta"))
        view = tool_allowance(now=1001)
    assert view["capacity"] == {
        "buckets": gateway.live_bucket_count(now=1001),
        "max_buckets": gateway.MAX_BUCKETS,
    }
    assert view["capacity"]["buckets"] >= 1


def test_reading_the_view_never_changes_what_the_gate_decides(_isolated):
    """A read must not run the generation sweep: viewing under a disabled or
    edited policy must leave the live buckets and ``_active_rules`` untouched,
    so the gate still refuses afterward (#2144 P2-A)."""
    _write(_isolated, _CAP_ONE)
    scope = _scope("agent_beta")
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=scope)
        with pytest.raises(policy.PolicyViolation, match="rate limited"):
            policy.evaluate_policy("fs_read", now=1001, call_scope=scope)

        # Policy disabled, then read: the counter and the sweep marker must
        # be exactly where enforcement left them.
        _write(_isolated, "version: 1\nenabled: false\ndefault_action: allow\nrules: []\n")
        buckets_before = gateway.bucket_count()
        active_before = gateway._LIMITS._active_rules
        view = tool_allowance(now=1002)
        assert gateway.bucket_count() == buckets_before
        assert gateway._LIMITS._active_rules == active_before
        # The disabled policy's bucket stays visible, just not chargeable.
        assert _row(view, "fs_read", "read_cap")["stopped"] is False

        # The identical rule back on: the charged window still refuses. Under
        # the bug the disabled-policy view had already dropped the bucket and
        # this call was admitted.
        _write(_isolated, _CAP_ONE)
        with pytest.raises(policy.PolicyViolation, match="rate limited"):
            policy.evaluate_policy("fs_read", now=1002, call_scope=scope)

        # widen → view → revert leaves the old generation's bucket in place too.
        _write(
            _isolated,
            "rules:\n  - name: read_cap\n    type: rate_limit\n    tool: fs_read\n"
            "    max_calls: 5\n    window_seconds: 60\n",
        )
        tool_allowance(now=1003)
        _write(_isolated, _CAP_ONE)
        with pytest.raises(policy.PolicyViolation, match="rate limited"):
            policy.evaluate_policy("fs_read", now=1003, call_scope=scope)


def test_foreign_role_scope_is_confirmed_only_by_identity(_isolated, monkeypatch):
    """The gate reads the subject's LIVE session profile, which a view of
    another identity cannot see: a role-scoped rule is confirmed only when the
    identity itself is in ``roles``, otherwise the row reports the raw counter
    with ``applies: null`` — never a guessed false and never stopped
    (#2144 P2-B)."""
    monkeypatch.setenv("UA_OPERATOR_LABEL", "human_operator")
    _write(
        _isolated,
        "rules:\n  - name: dev_reads\n    type: rate_limit\n    tool: fs_read\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        "    roles: [developer]\n",
    )
    scope = _scope("agent_beta")
    # Charged while the live profile was developer; the session then moved to
    # a profile outside the role, so the gate now admits.
    monkeypatch.setattr(profiles, "current", lambda: "developer")
    policy.evaluate_policy("fs_read", now=100, call_scope=scope)
    monkeypatch.setattr(profiles, "current", lambda: "research")
    policy.evaluate_policy("fs_read", now=101, call_scope=scope)
    with _verified("human_operator"):
        row = _row(tool_allowance("agent_beta", now=102), "fs_read", "dev_reads")
    assert row["used"] == 1 and row["stopped"] is False
    assert row["applies"] is None
    assert "session profile not visible" in row["applies_reason"]

    # Opposite direction: the live profile IS in roles while the identity is
    # not — the gate refuses, but the view still cannot confirm it.
    _write(
        _isolated,
        "rules:\n  - name: research_writes\n    type: rate_limit\n    tool: fs_write\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        "    roles: [research]\n",
    )
    policy.evaluate_policy("fs_write", now=101, call_scope=scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("fs_write", now=102, call_scope=scope)
    with _verified("human_operator"):
        row = _row(tool_allowance("agent_beta", now=103), "fs_write", "research_writes")
    assert row["stopped"] is False and row["applies"] is None
    assert "session profile not visible" in row["applies_reason"]

    # The identity itself in ``roles`` IS confirmable without the profile.
    _write(
        _isolated,
        "rules:\n  - name: beta_lists\n    type: rate_limit\n    tool: task_list\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        "    roles: [agent_beta]\n",
    )
    policy.evaluate_policy("task_list", now=104, call_scope=scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("task_list", now=105, call_scope=scope)
    with _verified("human_operator"):
        row = _row(tool_allowance("agent_beta", now=106), "task_list", "beta_lists")
    assert row["stopped"] is True and row["applies"] is True


def test_fleet_rows_recheck_scope_per_bucket_agent(_isolated, monkeypatch):
    """Fleet rows re-run the scope check per bucket owner: a bind the owner no
    longer holds reports ``applies: false``, a role scope the view cannot
    confirm reports ``applies: null``, and neither is stopped (#2144 P2-B)."""
    monkeypatch.setenv("UA_OPERATOR_LABEL", "human_operator")
    bound = _claim_task("agent_beta", "beta-bound-reads")
    _claim_task("agent_beta", "beta-other-task")
    _write(
        _isolated,
        "rules:\n"
        "  - name: task_reads\n    type: rate_limit\n    tool: fs_read\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        f'    bind:\n      task: "{bound}"\n'
        "  - name: research_writes\n    type: rate_limit\n    tool: fs_write\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        "    roles: [research]\n"
        "  - name: beta_lists\n    type: rate_limit\n    tool: task_list\n"
        "    max_calls: 1\n    window_seconds: 60\n"
        "    roles: [agent_beta]\n",
    )
    bound_scope = _scope(
        "agent_beta",
        claims=frozenset({gateway.ClaimBind(project="unified-agent", task=str(bound))}),
    )
    policy.evaluate_policy("fs_read", now=100, call_scope=bound_scope)
    monkeypatch.setattr(profiles, "current", lambda: "research")
    policy.evaluate_policy("fs_write", now=100, call_scope=_scope("agent_beta"))
    policy.evaluate_policy("task_list", now=100, call_scope=_scope("agent_beta"))
    # The bound claim moved to another agent: the bucket stops nothing now.
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET claimed_by='agent_alpha' WHERE id=?", (bound,))
        conn.commit()
    finally:
        conn.close()

    with _verified("human_operator"):
        view = tool_allowance(now=101)
    row = _row(view, "fs_read", "task_reads", agent="agent_beta")
    assert row["stopped"] is False
    assert row["applies"] is False and "bind" in row["applies_reason"]
    row = _row(view, "fs_write", "research_writes", agent="agent_beta")
    assert row["stopped"] is False and row["applies"] is None
    assert "session profile not visible" in row["applies_reason"]
    row = _row(view, "task_list", "beta_lists", agent="agent_beta")
    assert row["stopped"] is True and row["applies"] is True


def test_fleet_view_resolves_claims_once_per_distinct_agent(_isolated, monkeypatch):
    """The fleet scope re-check resolves one agent's claims once, no matter how
    many buckets it owns (#2144 P2-B, bounded)."""
    monkeypatch.setenv("UA_OPERATOR_LABEL", "human_operator")
    _write(
        _isolated,
        "rules:\n  - name: project_reads\n    type: rate_limit\n    tool: fs_*\n"
        "    max_calls: 2\n    window_seconds: 60\n"
        "    bind:\n      project: unified-agent\n",
    )
    claims = frozenset({gateway.ClaimBind(project="unified-agent", task="9")})
    policy.evaluate_policy("fs_read", now=100, call_scope=_scope("agent_beta", claims=claims))
    policy.evaluate_policy("fs_write", now=100, call_scope=_scope("agent_beta", claims=claims))
    policy.evaluate_policy("fs_read", now=100, call_scope=_scope("agent_alpha", claims=claims))

    seen: list[str] = []
    real = gateway.resolve_claims
    monkeypatch.setattr(gateway, "resolve_claims", lambda agent: seen.append(agent) or real(agent))
    with _verified("human_operator"):
        tool_allowance(now=101)
    assert sorted(seen) == ["agent_alpha", "agent_beta"]


def test_capacity_counts_live_buckets_only(_isolated):
    """``admit`` prunes fully-expired windows before the MAX_BUCKETS check, so
    the view must too: 'table full' only when a new bucket would really be
    refused (#2144 P3)."""
    _write(_isolated, _CAP)
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=_scope("agent_beta"))
        assert tool_allowance(now=1001)["capacity"]["buckets"] == 1
        # Every stamp has left the window: a new bucket cannot be refused.
        assert tool_allowance(now=2000)["capacity"]["buckets"] == 0


def test_capacity_skips_buckets_the_next_sweep_drops(_isolated):
    """A bucket of an edited or disabled rule is dropped by the next gate
    call's sweep before ``admit`` checks MAX_BUCKETS, so it must not count
    toward 'table full' (#2144 review r3)."""
    _write(_isolated, _CAP_ONE)
    scope = _scope("agent_beta")
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=scope)
        assert tool_allowance(now=1001)["capacity"]["buckets"] == 1
        # Edit the rule: a new generation, so the old bucket is swept next call.
        _write(_isolated, _CAP_ONE.replace("max_calls: 1", "max_calls: 5"))
        assert tool_allowance(now=1001)["capacity"]["buckets"] == 0
        # Disabled file policy: its rule buckets are swept, so they are not
        # counted. (explicit: buckets would still count: they survive the sweep
        # and keep enforcing on the policy_config path; this fixture has none.)
        _write(_isolated, "version: 1\nenabled: false\ndefault_action: allow\nrules: []\n")
        assert tool_allowance(now=1001)["capacity"]["buckets"] == 0
        # Still read-only: the bucket is untouched until the gate sweeps.
        assert gateway.bucket_count() == 1


def test_glob_pattern_row_counts_only_tools_without_a_row(_isolated):
    """The pattern row's ``used_today`` must not re-count calls already shown
    on a charged tool's own row (#2144 P3)."""
    _write(_isolated, _GLOB_CAP)
    for tool, n in (("fs_read", 2), ("fs_write", 3)):
        for _ in range(n):
            events.record_event(
                channel=gateway.CHANNEL_GATEWAY_DECISION,
                payload={
                    "decision": "allow",
                    "reason_code": "allowed",
                    "rule": "reads",
                    "tool_name": tool,
                    "caller": "agent_beta",
                    "profile": "developer",
                },
                originator="agent_beta",
                notify=False,
            )
    with _verified("agent_beta"):
        policy.evaluate_policy("fs_read", now=1000, call_scope=_scope("agent_beta"))
        view = tool_allowance(now=1001)
    assert _row(view, "fs_read", "reads")["used_today"] == 2
    pattern = [r for r in view["tools"] if r.get("pattern")]
    assert len(pattern) == 1 and pattern[0]["used_today"] == 3
