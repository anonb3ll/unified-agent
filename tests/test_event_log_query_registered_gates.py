"""The registered ``event_log_query`` against its HTTP siblings (task #1811).

``/api/ledger/rows`` and ``/api/events/stream`` admit a read through the
operator's recovery exemption, the ``UA_APPROVAL_TOOLS`` gate and an event log
that an allowed read does not feed (PR #981). These tests pin the same three
behaviours on the registered tool the MCP surface and ``/api/events`` call:

* the operator's one recovery read passes the agent-access switch, agents and
  other tools do not;
* any tool listed in ``UA_APPROVAL_TOOLS`` parks in the wrapper, except the
  tools that already gate themselves, which are gated exactly once;
* an allowed ``event_log_query`` call writes no event-log row, so paging with
  ``after_seq`` reaches an empty page, while a refusal is still recorded.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import agent_access, config, events, gateway, server, storage
from unified_agent.tools import approvals
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
)

AGENT = "Desktop_Worker"
OPERATOR = config.DEFAULT_OPERATOR_LABEL
_query_events = events.query_events  # unpatched, for reading decisions back


@pytest.fixture(autouse=True)
def _gate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))
    for name in (
        "UA_AGENT_ACCESS_ENABLED",
        "UA_AGENT_ACCESS_READ_ONLY",
        "UA_AGENT_ACCESS_PRODUCTION_BLOCKED",
        "UA_MANAGED_SECRETS",
        "UA_APPROVAL_TOOLS",
        "UA_OPERATOR_LABEL",
    ):
        monkeypatch.delenv(name, raising=False)
    gateway.reset_limits()
    yield
    gateway.reset_limits()


@contextmanager
def _as(identity: str | None):
    token = config.agent_identity_var.set(identity)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _query(**kwargs):
    # The MCP adapter passes every parameter, defaults included.
    arguments = {"channels": None, "since": None, "limit": 100, "after_seq": None}
    arguments.update(kwargs)
    return server.REGISTERED_TOOLS["event_log_query"](**arguments)


def _approve(approval_id: int) -> None:
    with _as(OPERATOR):
        approval_resolve(int(approval_id), "approve")


def _decisions(tool: str = "event_log_query") -> list[dict]:
    return [
        event["payload"]
        for event in _query_events(channels=[gateway.CHANNEL_GATEWAY_DECISION])
        if event["payload"].get("tool_name") == tool
    ]


def _task_titles() -> list[str]:
    conn = storage.connect()
    try:
        return [row["title"] for row in conn.execute("SELECT title FROM tasks ORDER BY id")]
    finally:
        conn.close()


# --- item 2: the operator's recovery read ------------------------------------------------


def test_operator_recovery_read_passes_the_off_switch(monkeypatch):
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    events.record_event("agent.task.updated", payload={"n": 1})
    with _as(OPERATOR):
        with pytest.raises(agent_access.AgentAccessDenied, match="disabled"):
            _query(after_seq=0)
        with agent_access.operator_recovery_read("event_log_query", OPERATOR):
            page = _query(after_seq=0, channels=["agent.task.*"])
    assert [row["payload"] for row in page["events"]] == [{"n": 1}]


def test_recovery_grant_covers_no_agent_and_no_other_tool(monkeypatch):
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    with _as(AGENT), agent_access.operator_recovery_read("event_log_query", AGENT):
        with pytest.raises(agent_access.AgentAccessDenied, match="disabled"):
            _query(after_seq=0)
    with _as(OPERATOR), agent_access.operator_recovery_read("event_log_query", OPERATOR):
        with pytest.raises(agent_access.AgentAccessDenied, match="disabled"):
            server.REGISTERED_TOOLS["task_list"]()
        # A write is never a recovery read, even for the granted tool name.
        assert not agent_access.recovery_read_granted("event_log_query", read_only=False)


def test_one_recovery_channel_set_is_shared():
    from unified_agent.webui import routes

    assert not hasattr(routes, "_OPERATOR_RECOVERY_VIA")
    assert agent_access.OPERATOR_RECOVERY_VIA == {"bearer", "oauth", "cookie", "workos"}
    assert agent_access.is_operator_recovery(OPERATOR, "bearer")
    assert not agent_access.is_operator_recovery(OPERATOR, None)
    assert not agent_access.is_operator_recovery(AGENT, "bearer")


# --- item 3: UA_APPROVAL_TOOLS in the registered wrapper --------------------------------


def test_listed_event_log_query_parks_until_approved(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "event_log_query")
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})
    with _as(AGENT):
        with pytest.raises(ApprovalPendingError) as parked:
            _query(after_seq=0, limit=5)
        [pending] = approval_pending()["pending"]
        # The HTTP governed reads' payload: supplied arguments, None omitted.
        assert (pending["tool"], pending["payload"]) == (
            "event_log_query",
            {"after_seq": 0, "limit": 5},
        )
        assert pending["id"] == parked.value.approval_id
        _approve(pending["id"])
        page = _query(after_seq=0, limit=5)
        assert page["count"] >= 1
        # One-shot: the next identical call parks again.
        with pytest.raises(ApprovalPendingError):
            _query(after_seq=0, limit=5)
    assert [d["decision"] for d in _decisions()] == ["pending", "pending"]


def test_http_shaped_pass_admits_the_equal_registered_read(monkeypatch):
    """A pass for the ledger route's arguments admits the equal MCP read."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "event_log_query")
    payload = approvals.supplied_arguments_payload(
        {"channels": None, "since": None, "after_seq": 0, "limit": 10}
    )
    with _as(AGENT):
        with pytest.raises(ApprovalPendingError) as pending:
            approvals.check_approval_gate("event_log_query", payload, requested_by=AGENT)
        _approve(pending.value.approval_id)
        assert _query(after_seq=0, limit=10)["count"] >= 0
    assert approval_pending()["pending"] == []


def test_unlisted_tool_is_not_gated(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_add")
    with _as(AGENT):
        _query(after_seq=0)
    assert approval_pending()["pending"] == []


def test_listed_write_tool_parks_in_the_wrapper(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_add")
    with _as(AGENT):
        with pytest.raises(ApprovalPendingError):
            server.REGISTERED_TOOLS["task_add"](title="gated-fixture")
        [pending] = approval_pending()["pending"]
        assert pending["tool"] == "task_add"
        assert pending["payload"] == {"title": "gated-fixture"}
        _approve(pending["id"])
        server.REGISTERED_TOOLS["task_add"](title="gated-fixture")
    assert _task_titles() == ["gated-fixture"]
    assert approval_pending()["pending"] == []


def test_policy_pass_and_listed_tool_share_one_approval(tmp_path, monkeypatch):
    """A require_approval rule that consumed a pass is not asked for a second."""
    (tmp_path / "policy.yaml").write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
        "  - name: approve_task_add\n    type: tool\n    tool: task_add\n"
        "    action: require_approval\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_add")
    with _as(AGENT):
        with pytest.raises(ApprovalPendingError):
            server.REGISTERED_TOOLS["task_add"](title="one-pass")
        [pending] = approval_pending()["pending"]
        _approve(pending["id"])
        server.REGISTERED_TOOLS["task_add"](title="one-pass")
    assert _task_titles() == ["one-pass"]
    assert approval_pending()["pending"] == []


def test_self_gated_tool_is_gated_once(monkeypatch):
    """run_cmd gates itself; the wrapper must not queue a second request."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    with _as(AGENT):
        with pytest.raises(ApprovalPendingError):
            server.REGISTERED_TOOLS["run_cmd"](command="echo gated-once")
        [pending] = approval_pending()["pending"]
        assert pending["tool"] == "run_cmd"
        _approve(pending["id"])
        result = server.REGISTERED_TOOLS["run_cmd"](command="echo gated-once")
    assert "gated-once" in result["stdout"]
    assert approval_pending()["pending"] == []


def test_listed_routine_fire_parks_and_refunds_its_rate_slot(tmp_path, monkeypatch):
    """routine_fire's own wrapper branch honours UA_APPROVAL_TOOLS."""
    from unified_agent.routines import routine_add

    (tmp_path / "policy.yaml").write_text(
        "rules:\n  - name: fire_cap\n    type: rate_limit\n    tool: routine_fire\n"
        "    max_calls: 1\n    window_seconds: 60\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "routine_fire")
    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    routine_id = routine_add("Gated fire", trigger="api")["id"]
    fire = server.REGISTERED_TOOLS["routine_fire"]
    with _as("Laptop_Agent5"):
        # A parked fire runs nothing, so it keeps no slot of the 1-call cap.
        for _ in range(3):
            with pytest.raises(ApprovalPendingError):
                fire(routine_id, idempotency_key="event-1")
        assert _task_titles() == []
        [pending] = approval_pending()["pending"]
        assert (pending["tool"], pending["payload"]) == (
            "routine_fire",
            {"routine_id": routine_id, "idempotency_key": "event-1"},
        )
        _approve(pending["id"])
        receipt = fire(routine_id, idempotency_key="event-1")
        assert receipt["status"] == "created"
        # A receipt replay needs no second pass.
        assert fire(routine_id, idempotency_key="event-1") == receipt
    assert len(_task_titles()) == 1
    assert approval_pending()["pending"] == []


def test_listed_package_register_costs_one_approval(monkeypatch):
    """The forced inner register gate is the only one a listing asks for.

    A wrapper pass would be spent once the call started, before the inner
    ``horizon2_server_register`` gate parked, so the retry would ask for the
    listed tool again: three approvals for one registration.
    """
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "horizon2_package_register")
    register = server.REGISTERED_TOOLS["horizon2_package_register"]
    arguments = {
        "package": "obsidian",
        "provenance": "sha256:abc",
        "review_ref": "review:42",
        "server_id": "obsidian-vault",
        "scopes": None,
    }
    with _as(AGENT):
        with pytest.raises(ApprovalPendingError):
            register(**arguments)
        [pending] = approval_pending()["pending"]
        assert pending["tool"] == "horizon2_server_register"
        _approve(pending["id"])
        result = register(**arguments)
    assert result["server"]["server_id"] == "obsidian-vault"
    assert result["approval_id"] == pending["id"]
    assert approval_pending()["pending"] == []


@pytest.fixture
def _forced_team_boot(monkeypatch):
    """``team_spec.boot`` reduced to its forced one-shot ``team_up`` gate."""
    from unified_agent import team_spec

    booted: list[int] = []

    def boot(spec, *, actor, expected_plan_sha256=None):
        gate = approvals.check_approval_gate(
            team_spec.TEAM_UP_TOOL, {"team": spec["team"]}, requested_by=actor, force=True
        )
        booted.append(int(gate["approval_id"]))
        return {"booted": True, "approval_id": gate["approval_id"]}

    monkeypatch.setattr(team_spec, "resolve", lambda team=None, spec=None: {"team": team})
    monkeypatch.setattr(team_spec, "boot", boot)
    return booted


def test_listed_coordination_team_up_boot_costs_one_approval(_forced_team_boot, monkeypatch):
    """A team boot asks only for its forced ``team_up`` gate (task #1896).

    A wrapper pass would be spent once the call started, before the inner gate
    parked, so the retry would ask for ``coordination_manage`` again: three
    approvals for one boot.
    """
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "coordination_manage")
    manage = server.REGISTERED_TOOLS["coordination_manage"]
    params = {"team": "platform-engineering", "plan": False}
    with _as(AGENT):
        with pytest.raises(ApprovalPendingError):
            manage("team_up", params)
        [pending] = approval_pending()["pending"]
        assert pending["tool"] == "team_up"
        assert _forced_team_boot == []
        _approve(pending["id"])
        result = manage("team_up", params)
    assert result == {"booted": True, "approval_id": pending["id"]}
    assert _forced_team_boot == [pending["id"]]
    assert approval_pending()["pending"] == []


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("team_up", {"team": "platform-engineering"}),
        ("team_up", {"team": "platform-engineering", "plan": True}),
        # Only a literal False skips the wrapper gate; the boot still forces its own.
        ("team_up", {"team": "platform-engineering", "plan": 0}),
        ("team_list", {}),
    ],
)
def test_listed_coordination_manage_gates_its_other_calls(
    _forced_team_boot, monkeypatch, action, params
):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "coordination_manage")
    with _as(AGENT):
        with pytest.raises(ApprovalPendingError):
            server.REGISTERED_TOOLS["coordination_manage"](action, params)
        [pending] = approval_pending()["pending"]
    assert pending["tool"] == "coordination_manage"
    assert pending["payload"] == {"action": action, "params": params}
    assert _forced_team_boot == []


_RELAY_REQUESTER = "cloud_agent13"  # chief of staff: relay_manage is in its profile


def _relay_params(text: str = "Fixed in the latest release, thanks!") -> dict:
    return {"channel": "dry_run", "target": "issue-42", "text": text}


def _relay_sent() -> list[dict]:
    from unified_agent.tools import public_relay

    return events.query_events(channels=[public_relay.SENT_CHANNEL], limit=10)


def test_listed_relay_send_costs_one_approval(monkeypatch):
    """A relay send asks only for its forced ``relay.public.send`` gate (task #1896)."""
    from unified_agent.tools import public_relay

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "relay_manage")
    relay = server.REGISTERED_TOOLS["relay_manage"]
    with _as(_RELAY_REQUESTER):
        with pytest.raises(ApprovalPendingError):
            relay("send", _relay_params())
        [pending] = approval_pending()["pending"]
        assert pending["tool"] == public_relay.RELAY_APPROVAL_TOOL
        assert _relay_sent() == []
        _approve(pending["id"])
        sent = relay("send", _relay_params())
    assert sent["status"] == "sent"
    assert sent["approval_id"] == pending["id"]
    assert len(_relay_sent()) == 1
    assert approval_pending()["pending"] == []


def test_listed_relay_draft_stays_gated_even_with_autosend(monkeypatch):
    """Draft can auto-send without its own gate, so a listing still parks it."""
    from unified_agent.tools import public_relay

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "relay_manage")
    monkeypatch.setattr(public_relay, "resolve_relay_autosend", lambda channel, context: True)
    with _as(_RELAY_REQUESTER):
        with pytest.raises(ApprovalPendingError):
            server.REGISTERED_TOOLS["relay_manage"]("draft", _relay_params())
        [pending] = approval_pending()["pending"]
    assert pending["tool"] == "relay_manage"
    assert _relay_sent() == []


_GATE_CALLS = {"check_approval_gate", "check_prod_action_gate"}
# This unregistered human recovery path admits a raw task writer. It does
# not add a gate inside the registered task_update tool body. Keep the
# exception tied to the exact caller, so a real native gate is still found.
_ADAPTER_GATE_CALLERS = {("claim_release.py", "task_claim_release", "task_update")}


def _gated_tool_names(root: Path | None = None) -> set[str]:
    """Tool gates in package source, excluding explicit external admission callers."""
    root = root or Path(server.__file__).resolve().parent
    trees = [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in root.rglob("*.py")]
    constants: dict[str, set[str]] = {}
    for _, tree in trees:
        for node in tree.body:
            if isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            constants.setdefault(target.id, set()).add(value.value)
    names: set[str] = set()
    for path, tree in trees:
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called not in _GATE_CALLS:
                continue
            first = node.args[0]
            gated: set[str] = set()
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                gated.add(first.value)
            elif isinstance(first, ast.Name | ast.Attribute):
                key = first.id if isinstance(first, ast.Name) else first.attr
                gated |= constants.get(key, set())
            owner = parents.get(node)
            while owner is not None and not isinstance(
                owner, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                owner = parents.get(owner)
            caller = owner.name if owner is not None else None
            names |= {
                tool
                for tool in gated
                if (path.relative_to(root).as_posix(), caller, tool) not in _ADAPTER_GATE_CALLERS
            }
    return names


def test_adapter_admission_exception_does_not_hide_a_native_task_update_gate(tmp_path):
    source = tmp_path / "claim_release.py"
    adapter = 'def task_claim_release():\n    check_approval_gate("task_update", {})\n'
    source.write_text(adapter)
    assert _gated_tool_names(tmp_path) == set()
    # A gate in the native writer must still trigger the registration invariant,
    # even when it shares the adapter's module. Other adapter gates also count.
    source.write_text(
        adapter
        + 'def task_update():\n    check_approval_gate("task_update", {})\n'
        + 'def task_claim_release_other():\n    check_approval_gate("run_cmd", {})\n'
    )
    assert _gated_tool_names(tmp_path) == {"task_update", "run_cmd"}


def test_every_self_gating_registered_tool_skips_the_wrapper_gate():
    self_gating = _gated_tool_names() & set(server.REGISTERED_TOOLS)
    assert {"run_cmd", "fs_write", "delegate", "task_delete"} <= self_gating
    missing = self_gating - server._SELF_GATED_APPROVAL_TOOLS
    assert not missing, f"self-gating tools would be gated twice: {sorted(missing)}"
    assert server._SELF_GATED_APPROVAL_TOOLS <= set(server.REGISTERED_TOOLS)


# --- item 4: allowed reads do not feed the log they page --------------------------------


def test_paging_to_the_tail_ends_on_an_empty_page(tmp_path):
    for n in range(5):
        events.record_event("agent.task.updated", payload={"n": n})
    after_seq, pages, seen = 0, 0, []
    with _as(AGENT):
        while True:
            pages += 1
            assert pages <= 10, "event_log_query paging never reached an empty page"
            page = _query(after_seq=after_seq, limit=2)
            if not page["events"]:
                break
            seen.extend(row["payload"].get("n") for row in page["events"])
            after_seq = page["seq_cursor"]
        # Polling the tail again stays empty: an allowed read writes no row.
        assert _query(after_seq=after_seq)["events"] == []
    assert seen == [0, 1, 2, 3, 4]
    assert pages == 4
    assert _decisions() == []

    # A refusal is still recorded.
    (tmp_path / "policy.yaml").write_text(
        "rules:\n  - name: deny_event_reads\n    type: tool\n"
        "    tool: event_log_query\n    action: deny\n",
        encoding="utf-8",
    )
    with _as(AGENT), pytest.raises(ToolError, match="deny_event_reads"):
        _query(after_seq=after_seq)
    assert [(d["decision"], d["rule"]) for d in _decisions()] == [("deny", "deny_event_reads")]


def test_failed_read_is_still_recorded(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("fixture read failure")

    monkeypatch.setattr(events, "query_events", broken)
    with _as(AGENT), pytest.raises(RuntimeError, match="fixture read failure"):
        _query(after_seq=0)
    assert [(d["decision"], d["reason_code"]) for d in _decisions()] == [("error", "tool_error")]


def test_other_tools_still_record_allowed_calls():
    with _as(AGENT):
        server.REGISTERED_TOOLS["task_list"]()
    # A read-only allow is counted into the hourly rollup (task #1922).
    gateway.flush_coalesced()
    rollups = [
        event["payload"]
        for event in _query_events(channels=[gateway.CHANNEL_GATEWAY_DECISION_ROLLUP])
        if event["payload"].get("tool_name") == "task_list"
    ]
    assert [(r["decision"], r["count"]) for r in rollups] == [("allow", 1)]
