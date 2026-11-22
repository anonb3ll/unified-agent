"""One registered synthetic workflow; fixture identities are not real consent."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, server, storage
from unified_agent.tools import approvals
from unified_agent.tools import synthetic_workflow as workflow
from unified_agent.tools.tasks import task_add, task_claim


@pytest.fixture(autouse=True)
def _host_chief_of_staff(seat_chief_of_staff):
    seat_chief_of_staff("Laptop_Agent5")


@contextmanager
def identity(label):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def scenario(monkeypatch):
    # macOS /tmp is a symlink; the production guard stays strict.
    monkeypatch.setenv("UA_HOME", str(config.data_home().resolve()))
    monkeypatch.setenv("UA_EVENT_BUS", "0")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("UA_SYNTHETIC_WORKFLOW", "1")
    monkeypatch.setenv("UA_APPROVAL_BASELINE", "0")
    home = config.data_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "DISPOSABLE_SYNTHETIC_WORKFLOW").write_text("disposable-synthetic-workflow\n")
    with identity("Laptop_Agent5"):
        task = task_add("disposable acceptance")
        claim = task_claim(task["id"], agent="Laptop_Agent5")
    return {
        "task_id": task["id"],
        "run_id": claim["current_run_id"],
        "client_key": "bounded-example",
        "label": "fixture",
    }


def call(args):
    with identity("Laptop_Agent5"):
        return server.REGISTERED_TOOLS["synthetic_workflow"](**args)


def approve(args):
    with pytest.raises(approvals.ApprovalPendingError) as pending:
        call(args)
    with identity("Desktop_Person"):
        approvals.approval_resolve(pending.value.approval_id, "approve")
    return pending.value.approval_id


def counts():
    path = config.data_home() / "synthetic-provider.sqlite"
    if not path.exists():
        return (0, 0)
    with sqlite3.connect(path) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0],
        )


def test_registered_combined_approval_execution_replay_and_cost(scenario):
    approval_id = approve(scenario)
    result = call(scenario)
    assert result["status"] == "completed"
    assert result["approval_id"] == approval_id
    assert result["run_id"] == scenario["run_id"]
    assert result["cost"] == {"availability": "unknown", "cost_usd": None}
    replay = call(scenario)
    assert replay["replayed"] and replay["result"] == result["result"]
    assert replay["evidence_ids"] == result["evidence_ids"]
    assert counts() == (1, 1)
    assert events.verify_event_chain()["status"] == "valid"
    with storage.connect() as conn:
        task = conn.execute("SELECT notes FROM tasks WHERE id=?", (scenario["task_id"],)).fetchone()
        assert task[0].count("Synthetic workflow") == 1
        stages = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT payload FROM event_log WHERE channel=?", (workflow.CHANNEL,)
            )
        ]
        assert [s["stage"] for s in stages] == [
            "effect_intent",
            "effect_dispatch",
            "effect_completed",
        ]
        assert len({s["approval"]["_consumption_nonce"] for s in stages}) == 1
        assert all(s["task_receipt_key"] == result["task_receipt_key"] for s in stages)
        assert all(s["principal"]["actor"] == "Laptop_Agent5" for s in stages)


@pytest.mark.asyncio
async def test_actual_mcp_combined_workflow(scenario):
    approve(scenario)
    with identity("Laptop_Agent5"):
        async with Client(server.mcp) as client:
            response = await client.session.call_tool("synthetic_workflow", scenario)
    assert not response.is_error
    result = json.loads(response.content[0].text)
    assert result["status"] == "completed" and counts() == (1, 1)


@pytest.mark.parametrize("fault", ["actor", "payload", "run", "deny", "disabled"])
def test_current_authority_cannot_be_reused(scenario, monkeypatch, tmp_path, fault):
    approve(scenario)
    args = dict(scenario)
    if fault == "payload":
        args["label"] = "changed"
    elif fault == "run":
        args["run_id"] = "sp-forged"
    elif fault == "deny":
        policy = tmp_path / "deny.yaml"
        policy.write_text(
            "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
            "  - name: refuse\n    type: tool\n    tool: synthetic_workflow\n    action: deny\n"
        )
        monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    elif fault == "disabled":
        monkeypatch.delenv("UA_SYNTHETIC_WORKFLOW")
    with identity("Desktop_Agent2" if fault == "actor" else "Laptop_Agent5"):
        with pytest.raises((ToolError, RuntimeError)):
            server.REGISTERED_TOOLS["synthetic_workflow"](**args)
    assert counts() == (0, 0)


def test_denied_request_never_executes(scenario):
    with pytest.raises(approvals.ApprovalPendingError) as pending:
        call(scenario)
    with identity("Desktop_Person"):
        approvals.approval_resolve(pending.value.approval_id, "deny", reason="fixture denied")
    with pytest.raises(ToolError, match="DENIED"):
        call(scenario)
    assert counts() == (0, 0)


def test_completed_authority_cannot_be_restored(scenario):
    approval_id = approve(scenario)
    call(scenario)
    approvals.restore_consumed_approval(approval_id)
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def fresh_http(args, mode="normal"):
    """Actual authenticated MCP over HTTP in a fresh process, only fixture paths."""
    home = config.data_home()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "UA_HOME": str(home),
        "UA_WORKSPACE": str(home / "workspace"),
        "UA_VAULT_PATH": str(home / "vault"),
        "UA_DEV_ROOT": str(home / "dev"),
        "UA_SETTINGS_FILE": "",
        "UA_STATE_SYNC": "0",
        "UA_SYNTHETIC_WORKFLOW": "1",
        "UA_POLICY_FILE": str(home / "missing-policy"),
        "UA_APPROVAL_BASELINE": "0",
        "UA_EVENT_SOCKET": str(home / "missing.sock"),
        "UA_EVENT_BUS": "0",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    code = r"""
import asyncio, json, os, sys
from unified_agent import config
config.load_gemini_env = lambda: None
from unified_agent import profiles, server
# Host-side chief-of-staff stand-in, as the seat_chief_of_staff fixture does in-process.
profiles.DEFAULT_PROFILES_BY_IDENTITY['Laptop_Agent5'] = profiles.CHIEF_OF_STAFF
from unified_agent.invocation_service import InvocationService
from unified_agent.tools import synthetic_workflow as workflow
import httpx
args, mode = json.loads(sys.argv[1]), sys.argv[2]
# In-memory disposable test authentication only, no credential issued or saved.
os.environ['UA_AGENT_TOKENS']='Laptop_Agent5:disposable-synthetic-test-token'
os.environ['UA_ENFORCE_AUTH']='1'
os.environ['UA_HTTP_ALLOWED_HOSTS']='127.0.0.1:*'
original = workflow._Provider.send
if mode in {'provider-crash','marker-crash'}:
    def send(self, *a):
        if mode == 'provider-crash':
            original(self, *a)
        os._exit(0)
    workflow._Provider.send = send
if mode in {'intent-crash', 'terminal-crash'}:
    complete = InvocationService.complete_mutation
    def commit(conn, prepare=None):
        complete(conn, prepare)
        state = 'pending' if mode == 'intent-crash' else 'completed'
        if conn.execute("SELECT 1 FROM invocation_receipts WHERE state=?", (state,)).fetchone():
            os._exit(0)
    InvocationService.complete_mutation = staticmethod(commit)
async def run():
    app = server.mcp.streamable_http_app()
    async with server.mcp.session_manager.run(), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8765',
        headers={'Authorization':'Bearer disposable-synthetic-test-token',
                 'Accept':'application/json, text/event-stream'}) as client:
        response = await client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'initialize',
            'params':{'protocolVersion':'2024-11-05','capabilities':{},
                      'clientInfo':{'name':'synthetic-acceptance','version':'1'}}})
        assert response.status_code == 200
        client.headers['mcp-session-id']=response.headers['mcp-session-id']
        await client.post('/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'})
        response = await client.post('/mcp',json={'jsonrpc':'2.0','id':2,'method':'tools/call',
            'params':{'name':'synthetic_workflow','arguments':args}})
        assert response.status_code == 200
        if response.headers.get('content-type','').startswith('text/event-stream'):
            body=json.loads(next(x[6:] for x in response.text.splitlines() if x.startswith('data: ')))
        else:
            body=response.json()
        print(json.dumps(body['result']))
asyncio.run(run())
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, json.dumps(args), mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr[-1800:]
    if mode.endswith("crash"):
        assert proc.stdout == "", proc.stdout
        return None
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("crash", ["provider-crash", "terminal-crash"])
def test_one_combined_http_crash_recovery_replay(scenario, crash):
    approval_id = approve(scenario)
    fresh_http(scenario, crash)
    assert counts() == (1, 1)
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    recovered = fresh_http({**scenario, "recovery_fence": 1})
    assert recovered["status"] == "completed"
    replay = fresh_http(scenario)
    assert replay["result"] == recovered["result"] and replay["replayed"]
    assert replay["evidence_ids"] == recovered["evidence_ids"]
    assert counts() == (1, 1)
    assert recovered["cost"]["cost_usd"] is None
    assert events.verify_event_chain()["status"] == "valid"
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE channel='agent.approval.consumed'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("mode", ["intent-crash", "marker-crash"])
def test_interrupted_pre_send_never_resends_on_reconciliation(scenario, mode):
    approve(scenario)
    fresh_http(scenario, mode)
    result = fresh_http({**scenario, "recovery_fence": 1})
    assert result["status"] == ("pending" if mode == "intent-crash" else "indeterminate")
    assert counts() == (0, 0)
    # A dispatch marker is uncertainty, not permission to send a replacement.
    if mode == "marker-crash":
        assert call(scenario)["status"] == "indeterminate"
        assert counts() == (0, 0)


@pytest.mark.parametrize("fault", ["revoke", "expire", "nonce", "payload", "deny", "run", "tenant"])
def test_original_authority_required_after_provider_commit(scenario, monkeypatch, tmp_path, fault):
    approval_id = approve(scenario)
    fresh_http(scenario, "provider-crash")
    args = {**scenario, "recovery_fence": 1}
    if fault == "revoke":
        with identity("Desktop_Person"):
            approvals.approval_resolve(approval_id, "revoke")
    elif fault in {"expire", "nonce", "run", "tenant"}:
        with storage.connect() as conn:
            if fault == "expire":
                conn.execute(
                    "UPDATE approvals SET resolved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                    (approval_id,),
                )
            elif fault == "nonce":
                conn.execute("UPDATE approvals SET status='revoked' WHERE id=?", (approval_id,))
            elif fault == "run":
                conn.execute(
                    "UPDATE tasks SET current_run_id='sp-foreign' WHERE id=?",
                    (scenario["task_id"],),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET org_id='foreign-org' WHERE id=?", (scenario["task_id"],)
                )
    elif fault == "payload":
        args["label"] = "different"
    else:
        path = tmp_path / "deny.yaml"
        path.write_text(
            "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
            "  - name: refuse\n    type: tool\n    tool: synthetic_workflow\n    action: deny\n"
        )
        monkeypatch.setenv("UA_POLICY_FILE", str(path))
    if fault == "run":
        # A later run does not erase a landed fact for the original bound run.
        # Lookup-only recovery cannot send or mutate the newer task run.
        result = call(args)
        assert result["status"] == "completed" and result["run_id"] == scenario["run_id"]
    else:
        with pytest.raises((ToolError, RuntimeError)):
            call(args)
    assert counts() == (1, 1)


def test_required_terminal_evidence_failure_recovers_without_send(scenario, monkeypatch):
    approve(scenario)
    record = events.record_event_once_on_connection

    def fail(conn, channel, *args, **kwargs):
        if channel == workflow.CHANNEL and kwargs["payload"]["stage"] == "effect_completed":
            raise RuntimeError("synthetic mandatory evidence unavailable")
        return record(conn, channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event_once_on_connection", fail)
    result = call(scenario)
    assert result["status"] == "indeterminate" and counts() == (1, 1)
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE channel=?", (workflow.CHANNEL + ".completed",)
            ).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(events, "record_event_once_on_connection", record)
    result = call({**scenario, "recovery_fence": result["fence"]})
    assert result["status"] == "completed" and counts() == (1, 1)


@pytest.mark.parametrize("action", ["block", "warn"])
def test_combined_workflow_uses_existing_budget_stop(scenario, monkeypatch, tmp_path, action):
    approve(scenario)
    policy = tmp_path / "budget.yaml"
    policy.write_text(
        "budgets:\n  - name: fixture\n    agents: [Laptop_Agent5]\n"
        "    limit_usd: 1\n    period: month\n    run_reserve_usd: 0\n"
        f"    action: {action}\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    with identity("Laptop_Agent5"):
        task_claim(
            scenario["task_id"], agent="Laptop_Agent5", run_usage={"billing": "api", "cost_usd": 2}
        )
    if action == "block":
        with pytest.raises(ToolError, match="budget hard stop"):
            call(scenario)
        assert counts() == (0, 0)
    else:
        assert call(scenario)["status"] == "completed" and counts() == (1, 1)


def test_completed_replay_survives_later_task_finish_and_expiry(scenario, monkeypatch):
    approve(scenario)
    first = call(scenario)
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET title='later',status='done',current_run_id=NULL,claimed_by=NULL WHERE id=?",
            (scenario["task_id"],),
        )
    monkeypatch.setattr(
        approvals,
        "_pass_expires_at",
        lambda row: workflow.datetime(2000, 1, 1, tzinfo=workflow.UTC),
    )
    replay = call(scenario)
    assert replay["result"] == first["result"] and counts() == (1, 1)
    assert replay["evidence_ids"] == first["evidence_ids"]


def test_optional_cost_projection_failure_keeps_committed_unknown(scenario, monkeypatch):
    approve(scenario)

    def unavailable(*a, **k):
        raise RuntimeError("fixture projection unavailable")

    monkeypatch.setattr(workflow.budgets, "cost_rollup", unavailable)
    result = call(scenario)
    assert result["status"] == "completed" and result["cost"]["cost_usd"] is None
    assert result["task_cost_rollup"] == {"availability": "unknown", "groups": None}
    assert counts() == (1, 1)


def test_missing_task_receipt_prevents_dispatch(scenario, monkeypatch):
    approve(scenario)
    monkeypatch.setattr(workflow.approvals, "bound_task_receipt", lambda *a, **k: None)
    with pytest.raises(ToolError, match="task receipt"):
        call(scenario)
    assert counts() == (0, 0)


def test_lost_intent_requires_fresh_approval_and_keeps_task_note_once(scenario, monkeypatch):
    first_approval = approve(scenario)
    record = events.record_event_once_on_connection

    def fail(conn, channel, *a, **k):
        if channel == workflow.CHANNEL and k["payload"]["stage"] == "effect_intent":
            raise RuntimeError("fixture intent failure")
        return record(conn, channel, *a, **k)

    monkeypatch.setattr(events, "record_event_once_on_connection", fail)
    with pytest.raises(RuntimeError, match="intent failure"):
        call(scenario)
    assert counts() == (0, 0)
    assert approvals.get_approval(first_approval)["status"] == "consumed"
    monkeypatch.setattr(events, "record_event_once_on_connection", record)
    second_approval = approve(scenario)
    assert second_approval != first_approval
    assert call(scenario)["status"] == "completed"
    with storage.connect() as conn:
        assert (
            conn.execute("SELECT notes FROM tasks WHERE id=?", (scenario["task_id"],))
            .fetchone()[0]
            .count("Synthetic workflow")
            == 1
        )
    assert counts() == (1, 1)


def test_registered_no_effect_can_upgrade_without_resend_and_fences_stale_recovery(
    scenario, monkeypatch
):
    approve(scenario)
    fresh_http(scenario, "provider-crash")
    lookup = workflow._Provider.lookup
    monkeypatch.setattr(workflow._Provider, "lookup", lambda *a: workflow.Observation("no_effect"))
    first = call({**scenario, "recovery_fence": 1})
    assert first["status"] == "no_effect" and first["fence"] == 2
    with pytest.raises(RuntimeError, match="fence"):
        call({**scenario, "recovery_fence": 1})
    monkeypatch.setattr(workflow._Provider, "lookup", lookup)
    later = call({**scenario, "recovery_fence": 2})
    assert later["status"] == "completed" and counts() == (1, 1)


def test_completed_recovery_rejects_a_different_positive_fence(scenario):
    approve(scenario)
    done = call(scenario)
    assert done["status"] == "completed"
    with pytest.raises(RuntimeError, match="fence"):
        call({**scenario, "recovery_fence": done["fence"] + 1})
    assert call({**scenario, "recovery_fence": done["fence"]})["status"] == "completed"
    assert call(scenario)["replayed"]
    assert counts() == (1, 1)


def test_completed_concurrent_replays_do_not_duplicate_effects(scenario):
    from concurrent.futures import ThreadPoolExecutor

    approve(scenario)
    first = call(scenario)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(scenario), range(4)))
    assert all(r["operation_id"] == first["operation_id"] for r in results)
    assert all(r["evidence_ids"] == first["evidence_ids"] for r in results)
    assert counts() == (1, 1)


def test_completed_replay_obeys_current_deny(scenario, monkeypatch, tmp_path):
    approve(scenario)
    call(scenario)
    path = tmp_path / "deny.yaml"
    path.write_text(
        "rules:\n  - name: refuse\n    type: tool\n    tool: synthetic_workflow\n    action: deny\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    with pytest.raises(ToolError, match="denied"):
        call(scenario)
    assert counts() == (1, 1)


def test_existing_evidence_interfaces_join_the_workflow(scenario):
    from unified_agent.tools.events import event_log_query
    from unified_agent.tools.evidence import evidence_report

    approve(scenario)
    result = call(scenario)
    with identity("Laptop_Agent5"):
        ledger = event_log_query(channels=[workflow.CHANNEL])
        report = evidence_report(task_id=scenario["task_id"])
    assert {r["ulid"] for r in ledger["events"]} == set(result["evidence_ids"])
    record = report["records"][0]
    assert set(result["evidence_ids"]) <= {e["ulid"] for e in record["proof"]["event_hashes"]}
    assert record["action"]["approvals"][0]["id"] == result["approval_id"]


def test_policy_approval_replay_is_not_a_second_consumption(scenario, monkeypatch, tmp_path):
    path = tmp_path / "require.yaml"
    path.write_text(
        "rules:\n  - name: require\n    type: tool\n    tool: synthetic_workflow\n    action: require_approval\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    approval_id = approve(scenario)
    first = call(scenario)
    replay = call(scenario)
    assert first["operation_id"] == replay["operation_id"]
    assert first["approval_id"] == replay["approval_id"] == approval_id
    assert counts() == (1, 1)


def test_superseded_consumption_cannot_recover(scenario):
    approval_id = approve(scenario)
    fresh_http(scenario, "provider-crash")
    approvals.restore_consumed_approval(approval_id)
    with identity("Laptop_Agent5"), approvals.approval_invocation("synthetic_workflow", scenario):
        approvals.check_approval_gate("synthetic_workflow", scenario, force=True)
        approvals.mark_governed_execution()
    with pytest.raises(ToolError, match="superseded"):
        call({**scenario, "recovery_fence": 1})
    assert counts() == (1, 1)


def test_synthetic_provider_counts_duplicate_send_attempts(scenario):
    # Supplementary fixture check: count sends independently of unique effects.
    # This is not the registered-workflow acceptance proof above.
    provider = workflow._Provider(config.data_home())
    first = provider.send("fixture-operation", "fixture-digest", {})
    assert provider.send("fixture-operation", "fixture-digest", {}) == first
    assert counts() == (1, 2)


@pytest.mark.parametrize("owner", [None, "foreign-org"])
def test_tenant_bound_workflow_refuses_null_and_foreign_task(scenario, monkeypatch, owner):
    # Reuse the existing single-org tenant boundary on a fresh disposable home.
    # No fabricated WorkOS session or body-supplied tenant is used.
    from unified_agent import edition

    with storage.connect() as conn:
        conn.execute("UPDATE tasks SET org_id=? WHERE id=?", (owner, scenario["task_id"]))
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "expected-org")
    with pytest.raises((ToolError, RuntimeError)):
        call(scenario)
    assert counts() == (0, 0)


def test_anonymous_identity_is_not_replaced_by_environment(scenario, monkeypatch):
    monkeypatch.setenv("UA_AGENT_LABEL", "Laptop_Agent5")
    monkeypatch.setenv("AGENT_LABEL", "Laptop_Agent5")
    with identity(None), pytest.raises(ToolError):
        server.REGISTERED_TOOLS["synthetic_workflow"](**scenario)
    assert counts() == (0, 0)


@pytest.mark.parametrize("link", ["symlink", "hardlink", "ancestor", "provider"])
def test_review_disposable_guard_refuses_linked_state_before_wrapper_writes(
    scenario, monkeypatch, tmp_path, link
):
    import hashlib

    source = config.db_path()
    approve(scenario)
    # Checkpoint the fixture before inspecting bytes; this is never a live store.
    with storage.connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    original_home = config.data_home()
    alias = tmp_path / "marked-alias"
    if link == "ancestor":
        parent = tmp_path / "linked-parent"
        parent.symlink_to(original_home.parent, target_is_directory=True)
        alias = parent / original_home.name
    else:
        alias.mkdir()
        (alias / "DISPOSABLE_SYNTHETIC_WORKFLOW").write_text("disposable-synthetic-workflow\n")
        if link == "symlink":
            (alias / "memory.db").symlink_to(source)
        elif link == "hardlink":
            os.link(source, alias / "memory.db")
        else:
            (alias / "synthetic-provider.sqlite").symlink_to(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv("UA_HOME", str(alias))
    with pytest.raises((ToolError, storage.LiveStoreError)):
        call(scenario)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_review_operator_cancel_blocks_new_delivery_but_preserves_landed_facts(scenario):
    from unified_agent.tools.tasks import task_update

    approve(scenario)
    with identity("Desktop_Person"):
        task_update(scenario["task_id"], request_cancel=True)
    with pytest.raises(ToolError, match="cancellation"):
        call(scenario)
    assert counts() == (0, 0)


def test_review_policy_change_after_intent_blocks_dispatch_without_deadlock(
    scenario, monkeypatch, tmp_path
):
    import time

    approve(scenario)
    original = workflow.InvocationService.complete_mutation
    path = tmp_path / "revoked-policy.yaml"
    path.write_text(
        "rules:\n  - name: stop\n    type: tool\n    tool: synthetic_workflow\n    action: deny\n"
    )

    def commit(conn, prepare=None):
        original(conn, prepare)
        if conn.execute("SELECT 1 FROM invocation_receipts WHERE state='pending'").fetchone():
            monkeypatch.setenv("UA_POLICY_FILE", str(path))

    monkeypatch.setattr(workflow.InvocationService, "complete_mutation", staticmethod(commit))
    started = time.monotonic()
    with pytest.raises(ToolError, match="policy changed"):
        call(scenario)
    assert time.monotonic() - started < 3
    assert counts() == (0, 0)


def test_review_legacy_alias_is_not_selected_writer_proof(scenario):
    import hashlib

    from unified_agent.tools.tasks import task_update

    approve(scenario)
    with identity("Laptop_Agent5"):
        principal = workflow._principal()
    key = (
        principal.organization,
        principal.subject,
        principal.actor,
        workflow.TOOL,
        1,
        scenario["client_key"],
    )
    task_key = "synthetic-task:" + hashlib.sha256(json.dumps(key).encode()).hexdigest()
    with identity("Laptop_Agent5"):
        task_update(
            scenario["task_id"],
            append_note="unrelated legacy update",
            idempotency_key=task_key,
            expected_run_id=scenario["run_id"],
        )
    with pytest.raises(ToolError, match="selected task receipt"):
        call(scenario)
    assert counts() == (0, 0)


def test_review_owner_gateway_proof_avoids_postcommit_append(scenario, monkeypatch):
    from unified_agent import gateway

    approve(scenario)
    record = events.record_event

    def fail(channel, *a, **k):
        if (
            channel == gateway.CHANNEL_GATEWAY_DECISION
            and k.get("payload", {}).get("tool_name") == workflow.TOOL
        ):
            raise RuntimeError("fixture post-body gateway unavailable")
        return record(channel, *a, **k)

    monkeypatch.setattr(events, "record_event", fail)
    result = call(scenario)
    assert result["status"] == "completed"
    assert call(scenario)["result"] == result["result"]
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE channel=? AND json_extract(payload,'$.tool_name')=? AND json_extract(payload,'$.decision')='allow'",
                (gateway.CHANNEL_GATEWAY_DECISION, workflow.TOOL),
            ).fetchone()[0]
            == 1
        )
    assert counts() == (1, 1)


def test_review_natural_expiry_and_cancellation_do_not_erase_landed_fact(scenario, monkeypatch):
    from unified_agent.tools.tasks import task_update

    approve(scenario)
    fresh_http(scenario, "provider-crash")
    with identity("Desktop_Person"):
        task_update(scenario["task_id"], request_cancel=True)
    monkeypatch.setattr(
        approvals,
        "_pass_expires_at",
        lambda row: workflow.datetime(2000, 1, 1, tzinfo=workflow.UTC),
    )
    result = call({**scenario, "recovery_fence": 1})
    assert result["status"] == "completed" and counts() == (1, 1)


def test_review_gateway_owner_failure_rolls_back_intent_before_send(scenario, monkeypatch):
    from unified_agent import gateway

    approve(scenario)
    record = events.record_event_once_on_connection

    def unavailable(conn, channel, *a, **k):
        if (
            channel == gateway.CHANNEL_GATEWAY_DECISION
            and k.get("payload", a[0] if a else {}).get("tool_name") == workflow.TOOL
        ):
            raise RuntimeError("fixture required gateway unavailable")
        return record(conn, channel, *a, **k)

    monkeypatch.setattr(events, "record_event_once_on_connection", unavailable)
    with pytest.raises((ToolError, RuntimeError)):
        call(scenario)
    assert counts() == (0, 0)
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invocation_receipts").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE channel=?", (workflow.CHANNEL,)
            ).fetchone()[0]
            == 0
        )


def test_review_pruning_retains_original_gateway_and_replay_proof(scenario):
    from test_events_prune import _frozen_now

    approve(scenario)
    with _frozen_now(90):
        first = call(scenario)
    events.record_event("agent.synthetic-fixture.current", payload={})
    events.prune_events(older_than_days=30)
    replay = call(scenario)
    assert replay["result"] == first["result"] and replay["evidence_ids"] == first["evidence_ids"]
    assert counts() == (1, 1)
    assert events.verify_event_chain()["status"] == "valid"


@pytest.mark.parametrize(
    "setting,value",
    [
        ("UA_STATE_SYNC", "1"),
        ("UA_EVENT_BUS", "1"),
        ("HF_HUB_OFFLINE", "0"),
        ("TRANSFORMERS_OFFLINE", "0"),
        ("UA_APPROVAL_PUSH_URL", "https://synthetic.invalid/topic"),
        ("UA_APPROVAL_WEBHOOK_URL", "https://synthetic.invalid/hook"),
    ],
)
def test_review_inherited_egress_configuration_is_refused(scenario, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises((ToolError, storage.LiveStoreError)):
        call(scenario)
    assert counts() == (0, 0)


def test_review_synthetic_initialization_skips_inherited_workers_and_secret_loader(
    scenario, monkeypatch
):
    def forbidden(*a, **k):
        raise AssertionError("inherited initializer must not run")

    monkeypatch.setattr(config, "load_gemini_env", forbidden)
    monkeypatch.setattr(config, "process_workspace_root", forbidden)
    local = []
    monkeypatch.setattr(
        storage,
        "post_init_hooks",
        lambda: [
            storage.PostInitHook("fixture-local", lambda conn: local.append(True), 1, False),
            storage.PostInitHook("fixture-outbound", forbidden, 2, True),
        ],
    )
    config.ensure_dirs()
    storage.run_post_init_hooks()
    assert local == [True]


@contextmanager
def authenticated_profile_session(monkeypatch):
    from unified_agent import profiles

    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    session = config.mcp_session_id_var.set("synthetic-profile-race-session")
    try:
        yield
    finally:
        with identity("Laptop_Agent5"):
            profiles.reset()
        config.mcp_session_id_var.reset(session)


def activate_profile(name):
    from unified_agent.tools.profiles import profile_activate

    with identity("Laptop_Agent5"):
        return profile_activate(name)


def restore_profile():
    # Developer cannot activate a higher profile through the registered tool.
    # The in-process reset returns this identity to its code default.
    from unified_agent.tools.profiles import profile_reset

    with identity("Laptop_Agent5"):
        return profile_reset()


def test_review_profile_downgrade_after_intent_blocks_send_and_restoration_is_explicit(
    scenario, monkeypatch
):
    from unified_agent import profiles

    with authenticated_profile_session(monkeypatch):
        approve(scenario)
        commit = workflow.InvocationService.complete_mutation
        changed = []

        def downgrade(conn, prepare=None):
            commit(conn, prepare)
            if (
                not changed
                and conn.execute(
                    "SELECT 1 FROM invocation_receipts WHERE state='pending'"
                ).fetchone()
            ):
                activate_profile("developer")
                with identity("Laptop_Agent5"):
                    assert not profiles.tool_is_allowed(workflow.TOOL)
                changed.append(True)

        monkeypatch.setattr(
            workflow.InvocationService, "complete_mutation", staticmethod(downgrade)
        )
        with pytest.raises(ToolError, match="no longer visible"):
            call(scenario)
        assert changed and counts() == (0, 0)
        with pytest.raises(ToolError, match="not exposed"):
            call({**scenario, "recovery_fence": 1})
        with pytest.raises(ToolError, match="disposable synthetic mode"):
            with identity("Laptop_Agent5"):
                server.REGISTERED_TOOLS["profile_activate"](profile="chief-of-staff")
        restore_profile()
        # Restoration grants visibility, not an automatic replacement send.
        assert call({**scenario, "recovery_fence": 1})["status"] == "pending"
        assert counts() == (0, 0)
        assert call(scenario)["status"] == "completed"
        assert call(scenario)["replayed"] and counts() == (1, 1)


def test_review_profile_downgrade_after_provider_commit_retains_uncertainty(scenario, monkeypatch):
    with authenticated_profile_session(monkeypatch):
        approve(scenario)
        send = workflow._Provider.send

        def commit_then_downgrade(self, *args):
            result = send(self, *args)
            activate_profile("developer")
            return result

        monkeypatch.setattr(workflow._Provider, "send", commit_then_downgrade)
        result = call(scenario)
        assert result["status"] == "indeterminate" and result["result"] is None
        assert counts() == (1, 1)
        with pytest.raises(ToolError, match="not exposed"):
            call({**scenario, "recovery_fence": result["fence"]})
        restore_profile()
        recovered = call({**scenario, "recovery_fence": result["fence"]})
        assert recovered["status"] == "completed" and counts() == (1, 1)


def test_review_profile_downgrade_during_lookup_cannot_complete_until_restored(
    scenario, monkeypatch
):
    with authenticated_profile_session(monkeypatch):
        approve(scenario)
        fresh_http(scenario, "provider-crash")
        lookup = workflow._Provider.lookup
        changed = []

        def lookup_then_downgrade(self, *args):
            result = lookup(self, *args)
            if not changed:
                activate_profile("developer")
                changed.append(True)
            return result

        monkeypatch.setattr(workflow._Provider, "lookup", lookup_then_downgrade)
        with pytest.raises(ToolError, match="no longer visible"):
            call({**scenario, "recovery_fence": 1})
        assert counts() == (1, 1)
        restore_profile()
        pending = call(scenario)
        assert pending["status"] == "indeterminate"
        assert call({**scenario, "recovery_fence": pending["fence"]})["status"] == "completed"
        assert counts() == (1, 1)


def test_review_role_policy_context_change_with_visible_tool_blocks_dispatch(
    scenario, monkeypatch, tmp_path
):
    from unified_agent import profiles

    with authenticated_profile_session(monkeypatch):
        path = tmp_path / "role-policy.yaml"
        path.write_text(
            "rules:\n  - name: chief_only\n    type: role\n    tool: synthetic_workflow\n    roles: [chief-of-staff]\n"
        )
        monkeypatch.setenv("UA_POLICY_FILE", str(path))
        approve(scenario)
        commit = workflow.InvocationService.complete_mutation
        changed = []

        def change_role(conn, prepare=None):
            commit(conn, prepare)
            if (
                not changed
                and conn.execute(
                    "SELECT 1 FROM invocation_receipts WHERE state='pending'"
                ).fetchone()
            ):
                activate_profile("steward")
                with identity("Laptop_Agent5"):
                    assert profiles.tool_is_allowed(workflow.TOOL)
                changed.append(True)

        monkeypatch.setattr(
            workflow.InvocationService, "complete_mutation", staticmethod(change_role)
        )
        with pytest.raises(ToolError, match="policy context changed"):
            call(scenario)
        assert changed and counts() == (0, 0)
        with pytest.raises(ToolError, match="role policy"):
            call(scenario)
        restore_profile()
        assert call({**scenario, "recovery_fence": 1})["status"] == "pending"
        assert counts() == (0, 0)
        assert call(scenario)["status"] == "completed" and counts() == (1, 1)


def test_review_claim_project_change_after_intent_blocks_dispatch(scenario, monkeypatch, tmp_path):
    path = tmp_path / "project-policy.yaml"
    path.write_text(
        "rules:\n  - name: blocked_project\n    type: tool\n    tool: synthetic_workflow\n    action: deny\n    bind:\n      project: browser\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    approve(scenario)
    commit = workflow.InvocationService.complete_mutation
    changed = []

    def move_project(conn, prepare=None):
        commit(conn, prepare)
        if (
            not changed
            and conn.execute("SELECT 1 FROM invocation_receipts WHERE state='pending'").fetchone()
        ):
            changed.append(True)
            with identity("Laptop_Agent5"):
                server.REGISTERED_TOOLS["task_update"](
                    id=scenario["task_id"], project="browser", worktree=False
                )

    monkeypatch.setattr(workflow.InvocationService, "complete_mutation", staticmethod(move_project))
    with pytest.raises(ToolError, match="policy context changed"):
        call(scenario)
    assert changed and counts() == (0, 0)
    with pytest.raises(ToolError, match="denied"):
        call(scenario)
    assert counts() == (0, 0)


def test_review_claim_scope_port_reads_owner_snapshot_without_commit(scenario):
    from unified_agent import gateway

    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE tasks SET project='browser' WHERE id=?", (scenario["task_id"],))
        scope = gateway.resolve_claims_on_connection(conn, "Laptop_Agent5")
        assert scope.projects == {"browser"}
        assert conn.in_transaction
        with storage.connect() as observer:
            assert (
                observer.execute(
                    "SELECT project FROM tasks WHERE id=?", (scenario["task_id"],)
                ).fetchone()[0]
                != "browser"
            )
        conn.rollback()


def test_disposable_approval_does_not_queue_slack(scenario, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_SLACK_CHANNEL", "C0SYNTHETIC")
    monkeypatch.setenv("UA_SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-synthetic")
    monkeypatch.setenv("UA_SLACK_CHANNEL_IDS", "C0SYNTHETIC")
    with pytest.raises(approvals.ApprovalPendingError):
        call(scenario)
    with storage.connect() as conn:
        queued = conn.execute("SELECT COUNT(*) FROM approval_slack_outbox").fetchone()[0]
    assert queued == 0


def test_prechange_provider_database_gains_org_id(scenario):
    path = config.data_home() / "synthetic-provider.sqlite"
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE effects (operation_id TEXT PRIMARY KEY, "
            "digest TEXT NOT NULL, result TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE sends (operation_id TEXT NOT NULL)")
        conn.execute("INSERT INTO effects VALUES ('op-1', 'abc', '{\"ok\": true}')")
        conn.execute("INSERT INTO sends VALUES ('op-1')")
    workflow._Provider(config.data_home())
    with sqlite3.connect(path) as conn:
        effects = {row[1] for row in conn.execute("PRAGMA table_info(effects)")}
        sends = {row[1] for row in conn.execute("PRAGMA table_info(sends)")}
        effect_indexes = {row[1] for row in conn.execute("PRAGMA index_list(effects)")}
        send_indexes = {row[1] for row in conn.execute("PRAGMA index_list(sends)")}
        kept = conn.execute("SELECT result FROM effects WHERE operation_id='op-1'").fetchone()
    assert "org_id" in effects and "org_id" in sends
    assert "idx_effects_org_id" in effect_indexes
    assert "idx_sends_org_id" in send_indexes
    assert kept[0] == '{"ok": true}'
    workflow._Provider(config.data_home())


def test_disposable_mode_refuses_tools_other_than_synthetic_workflow(scenario):
    with identity("Laptop_Agent5"):
        with pytest.raises(ToolError, match="disposable synthetic mode"):
            server.REGISTERED_TOOLS["web_fetch"](url="https://example.invalid")
        with pytest.raises(ToolError, match="disposable synthetic mode"):
            server.REGISTERED_TOOLS["task_update"](id=scenario["task_id"], request_cancel=True)


@pytest.mark.parametrize("client_key", ["", "has space", "a/b", "é", "x" * 201, 7])
def test_client_key_is_bounded_before_any_store_write(scenario, client_key):
    with pytest.raises(ToolError, match="client key"):
        call({**scenario, "client_key": client_key})


def test_adopting_a_receipt_needs_an_active_invocation():
    with pytest.raises(RuntimeError, match="no active approval invocation"):
        approvals.adopt_invocation_receipt("synthetic_workflow", {}, None, {})


def test_adopting_a_receipt_refuses_one_the_gate_would_not_validate():
    with approvals.approval_invocation("synthetic_workflow", {}):
        with pytest.raises(ToolError, match="consumed approval receipt"):
            approvals.adopt_invocation_receipt("synthetic_workflow", {}, None, {})
