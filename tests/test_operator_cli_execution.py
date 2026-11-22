"""Controlled execution through the CLI (task #1938).

Most cases drive the real in-process hub through the same authenticated /mcp
seam the installed client uses, with a relay that can lose a request before
the hub sees it or drop the response after the hub committed it. Waits and
follows run on a fake clock, so no test sleeps.
"""

import json
import os
import secrets
import stat
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from unified_agent import config, storage
from unified_agent.cli import client, execution, receipts
from unified_agent.cli.main import run

SPEC = "Rebase the fix branch onto main.\nDone when the branch merges cleanly.\n"


@pytest.fixture(autouse=True)
def no_embedding_model(monkeypatch):
    """A delegation's memory note degrades to FTS; never fetch a model here."""
    from unified_agent import embeddings

    def unavailable(text):
        raise ValueError("embedding model is not used by these tests")

    monkeypatch.setattr(embeddings, "generate_embedding", unavailable)


@pytest.fixture
def clock(monkeypatch):
    """A fake clock: every sleep advances it and runs any queued hook once."""
    state = {"now": 1000.0, "hooks": []}

    def sleep(seconds):
        state["now"] += max(seconds, 0.01)
        if state["hooks"]:
            state["hooks"].pop(0)()

    monkeypatch.setattr(execution, "_monotonic", lambda: state["now"])
    monkeypatch.setattr(execution, "_sleep", sleep)
    return state


@pytest.fixture
def live(monkeypatch, tmp_path):
    """The real hub behind the authenticated /mcp seam, with a fault-injecting relay."""
    from starlette.testclient import TestClient

    from unified_agent import server

    for name in ("UA_EXECUTOR_TOKEN", "UA_EXECUTOR_AGENT", "UA_CLI_IDENTITY", "UA_CLI_SESSION"):
        monkeypatch.delenv(name, raising=False)
    tokens = {
        "Desktop_Agent2": secrets.token_urlsafe(24),
        "server_agent1": secrets.token_urlsafe(24),
    }
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in tokens.items()))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", tokens["Desktop_Agent2"])
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:8765/mcp")
    monkeypatch.setenv("UA_CLI_PROJECT", "unified-agent")
    monkeypatch.setenv("UA_CLI_STATE_DIR", str(tmp_path / "cli-state"))
    storage.init_db()
    faults = {"tokens": tokens, "drop_after": [], "fail_before": [], "seen": []}
    real_client = httpx.Client
    with TestClient(server.mcp.streamable_http_app(), base_url="http://127.0.0.1:8765") as asgi:

        def relay(request):
            name = None
            if request.method == "POST":
                body = json.loads(request.content)
                if body.get("method") == "tools/call":
                    params = body["params"]
                    name = params["name"]
                    action = params["arguments"].get("action")
                    faults["seen"].append((name, action))
                    marker = (name, action)
                    if faults["fail_before"] and faults["fail_before"][0] in {marker, name}:
                        faults["fail_before"].pop(0)
                        raise httpx.ConnectError("lost before the hub", request=request)
            answer = asgi.request(
                request.method, str(request.url), headers=request.headers, content=request.content
            )
            if (
                name
                and faults["drop_after"]
                and faults["drop_after"][0]
                in {
                    (name, action),
                    name,
                }
            ):
                faults["drop_after"].pop(0)
                # The hub committed; only the response is lost.
                raise httpx.ReadTimeout("response dropped", request=request)
            return httpx.Response(
                answer.status_code, headers=answer.headers, content=answer.content
            )

        monkeypatch.setattr(
            client.httpx,
            "Client",
            lambda **kwargs: real_client(transport=httpx.MockTransport(relay), **kwargs),
        )
        yield faults


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text(SPEC, encoding="utf-8")
    return str(path)


def ua(capsys, *argv, code=0):
    assert run([*argv, "--format", "json"]) == code, capsys.readouterr()
    return json.loads(capsys.readouterr().out)


def delegations():
    conn = storage.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM delegations ORDER BY id")]
    finally:
        conn.close()


def start_args(spec_file, *extra):
    return (
        "delegate",
        "start",
        "--to",
        "server_agent1",
        "--spec-file",
        spec_file,
        "--callback",
        "none",
        *extra,
    )


def as_identity(label):
    return config.agent_identity_var.set(label)


# --- dropped committed responses, retry replay and reconciliation -------------


def test_dropped_committed_response_replays_without_a_duplicate(live, capsys, spec_file):
    live["drop_after"].append(("delegation_manage", "create"))
    lost = ua(capsys, *start_args(spec_file, "--idempotency-key", "drop-1"), code=11)
    assert lost["outcome"] == "unknown"
    reference = lost["error"]["reference"]
    assert reference["idempotency_key"] == "drop-1" and reference["state"] == "unknown"
    # The hub committed exactly once even though the client saw no response.
    [row] = delegations()

    # An identical retry without naming the key reuses the unsettled key.
    replay = ua(capsys, *start_args(spec_file))
    assert replay["data"]["reference"]["idempotency_key"] == "drop-1"
    assert replay["data"]["result"]["delegation_id"] == row["id"]
    assert len(delegations()) == 1

    # Reconciliation resolves it from the receipt with no effect.
    resolved = ua(capsys, "reconcile", "drop-1")["data"]
    assert resolved["reference"]["state"] == "committed"
    assert resolved["result"]["delegation_id"] == row["id"]
    assert len(delegations()) == 1


def test_lost_request_is_not_committed_until_an_explicit_keyed_retry(live, capsys, spec_file):
    live["fail_before"].append(("delegation_manage", "create"))
    lost = ua(capsys, *start_args(spec_file, "--idempotency-key", "lost-1"), code=11)
    assert lost["outcome"] == "unknown"
    assert delegations() == []

    pending = ua(capsys, "reconcile", "lost-1", code=12)
    assert pending["outcome"] == "not_committed"
    assert delegations() == []

    sent = ua(capsys, "reconcile", "lost-1", "--retry")["data"]
    assert sent["reference"]["state"] == "committed"
    [row] = delegations()
    assert sent["result"]["delegation_id"] == row["id"]
    # Retrying a committed key replays; it never inserts again.
    again = ua(capsys, "reconcile", "lost-1", "--retry")["data"]
    assert again["result"] == sent["result"]
    assert len(delegations()) == 1


def test_committed_delegation_with_an_unwritable_receipt_is_reported(
    live, capsys, spec_file, monkeypatch
):
    real_write = receipts._atomic_write

    def fail_after_prepared(folder, target, encoded):
        # The pre-send receipt and index land; every later receipt write fails.
        if b'"state": "prepared"' not in encoded and b'"state"' in encoded:
            raise OSError("disk full")
        real_write(folder, target, encoded)

    monkeypatch.setattr(receipts, "_atomic_write", fail_after_prepared)
    assert run([*start_args(spec_file, "--idempotency-key", "k1"), "--format", "json"]) == 0
    out = capsys.readouterr().out
    assert "nothing was sent" not in out
    data = json.loads(out)["data"]
    [row] = delegations()
    assert data["result"]["delegation_id"] == row["id"]
    assert data["result"]["task_id"] == row["task_id"]
    assert data["reference"]["idempotency_key"] == "k1"
    assert data["reference"]["receipt_updated"] is False
    [warning] = data["warnings"]
    assert warning["code"] == "receipt_not_updated" and "k1" in warning["message"]
    assert receipts.load("k1")["state"] == "prepared"

    # An identical rerun with the key replays the commit; no second delegation.
    monkeypatch.setattr(receipts, "_atomic_write", real_write)
    replay = ua(capsys, *start_args(spec_file, "--idempotency-key", "k1"))["data"]
    assert replay["result"]["delegation_id"] == row["id"]
    assert "warnings" not in replay and len(delegations()) == 1
    assert receipts.load("k1")["state"] == "committed"


def test_unknown_outcome_with_an_unwritable_receipt_keeps_its_key(
    live, capsys, spec_file, monkeypatch
):
    real_write = receipts._atomic_write

    def fail_after_prepared(folder, target, encoded):
        if b'"state": "prepared"' not in encoded and b'"state"' in encoded:
            raise OSError("disk full")
        real_write(folder, target, encoded)

    monkeypatch.setattr(receipts, "_atomic_write", fail_after_prepared)
    live["drop_after"].append(("delegation_manage", "create"))
    lost = ua(capsys, *start_args(spec_file, "--idempotency-key", "k2"), code=11)
    assert lost["outcome"] == "unknown"
    assert "nothing was sent" not in lost["error"]["message"]
    assert "k2" in lost["error"]["message"]
    assert lost["error"]["reference"]["idempotency_key"] == "k2"
    assert lost["error"]["reference"]["receipt_updated"] is False

    # Reconcile reports the hub's answer, not a pre-send failure.
    resolved = ua(capsys, "reconcile", "k2")["data"]
    [row] = delegations()
    assert resolved["result"]["delegation_id"] == row["id"]
    assert resolved["warnings"][0]["code"] == "receipt_not_updated"


def test_receipt_is_saved_before_send_privately_and_holds_no_credential(
    live, capsys, spec_file, monkeypatch
):
    seen = {}
    real_tool = client.ReadClient.tool

    def observe(self, name, arguments, *, write=False):
        if arguments.get("action") == "create":
            path = receipts.path_for("private-1")
            seen["state"] = json.loads(path.read_text())["state"]
        return real_tool(self, name, arguments, write=write)

    monkeypatch.setattr(client.ReadClient, "tool", observe)
    ua(capsys, *start_args(spec_file, "--idempotency-key", "private-1"))
    assert seen["state"] == "prepared"
    path = receipts.path_for("private-1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    text = path.read_text()
    assert all(token not in text for token in live["tokens"].values())
    saved = json.loads(text)
    assert saved["state"] == "committed" and saved["identity"] == "Desktop_Agent2"
    assert saved["arguments"]["from_agent"] == "Desktop_Agent2"


def test_same_key_for_a_different_request_is_refused(live, capsys, spec_file, tmp_path):
    ua(capsys, *start_args(spec_file, "--idempotency-key", "bound-1"))
    other = tmp_path / "other.md"
    other.write_text("A different request.\n", encoding="utf-8")
    # The local receipt refuses before sending ...
    refused = ua(capsys, *start_args(str(other), "--idempotency-key", "bound-1"), code=2)
    assert refused["error"]["code"] == "validation"
    # ... and the hub refuses even without that receipt.
    receipts.path_for("bound-1").unlink()
    conflict = ua(capsys, *start_args(str(other), "--idempotency-key", "bound-1"), code=8)
    assert conflict["outcome"] == "conflict"
    assert len(delegations()) == 1


def test_another_identity_cannot_read_or_replay_your_key(live, capsys, spec_file, monkeypatch):
    mine = ua(capsys, *start_args(spec_file, "--idempotency-key", "scoped-1"))["data"]
    assert ua(capsys, "delegate", "lookup", "scoped-1")["data"]["state"] == "committed"
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", live["tokens"]["server_agent1"])
    assert ua(capsys, "delegate", "lookup", "scoped-1")["data"] == {
        "idempotency_key": "scoped-1",
        "state": "not_found",
    }
    # The receipt is bound to the identity it was sent as.
    mismatch = ua(capsys, "reconcile", "scoped-1", code=2)
    assert mismatch["error"]["code"] == "context_mismatch"
    assert mine["result"]["delegation_id"] == delegations()[0]["id"]


# --- concurrent claims --------------------------------------------------------


def _create(key, spec=SPEC):
    from unified_agent.tools.delegate import delegation_manage

    token = as_identity("Desktop_Agent2")
    try:
        return delegation_manage(
            "create",
            {
                "task_spec": spec,
                "from_agent": "Desktop_Agent2",
                "to_agent": "server_agent1",
                "callback": "none",
                "idempotency_key": key,
            },
        )
    finally:
        config.agent_identity_var.reset(token)


def test_a_concurrent_same_key_request_is_refused_while_the_first_holds_it(monkeypatch):
    from unified_agent.tool_errors import ConflictError
    from unified_agent.tools import tasks

    real_add = tasks.task_add
    observed = {}

    def task_add(*args, **kwargs):
        if "inner" not in observed:
            observed["inner"] = "running"
            with pytest.raises(ConflictError, match="still in progress"):
                _create("race-1")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(tasks, "task_add", task_add)
    first = _create("race-1")
    assert observed["inner"] == "running"
    assert [row["id"] for row in delegations()] == [first["delegation_id"]]
    assert _create("race-1") == first


def test_racing_threads_with_one_key_create_one_delegation():
    from unified_agent.tool_errors import ConflictError

    barrier = threading.Barrier(4)
    results, conflicts = [], []

    def worker():
        barrier.wait()
        try:
            results.append(_create("threads-1"))
        except ConflictError:
            conflicts.append(True)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    [row] = delegations()
    assert results and all(item["delegation_id"] == row["id"] for item in results)
    assert len(results) + len(conflicts) == 4


def test_stale_holder_takeover_reuses_its_child_task_and_note(monkeypatch):
    """A holder that died after creating the child task and note leaves an
    in-progress receipt. Once it is older than STALE_AFTER_S the retry takes
    it over and reuses that task and note, inserting exactly one delegation."""
    from datetime import UTC, datetime, timedelta

    from unified_agent.tools import delegate_receipts, memory

    real_save = memory.memory_save
    notes = []

    def recording_save(*args, **kwargs):
        result = real_save(*args, **kwargs)
        notes.append(result["id"])
        return result

    monkeypatch.setattr(memory, "memory_save", recording_save)

    class HubDied(BaseException):
        pass

    def die(*args, **kwargs):
        raise HubDied

    # A hard kill: the delegation insert never commits and nothing releases the key.
    real_commit = delegate_receipts.commit_on_connection
    real_release = delegate_receipts.release
    monkeypatch.setattr(delegate_receipts, "commit_on_connection", die)
    monkeypatch.setattr(delegate_receipts, "release", lambda *args: None)
    with pytest.raises(HubDied):
        _create("takeover-1")
    monkeypatch.setattr(delegate_receipts, "commit_on_connection", real_commit)
    monkeypatch.setattr(delegate_receipts, "release", real_release)

    assert delegations() == []
    conn = storage.connect()
    try:
        [orphan_task] = [row[0] for row in conn.execute("SELECT id FROM tasks")]
    finally:
        conn.close()
    assert len(notes) == 1

    from unified_agent.tool_errors import ConflictError

    with pytest.raises(ConflictError, match="still in progress"):
        _create("takeover-1")

    key = delegate_receipts.storage_key("Desktop_Agent2", "takeover-1")
    conn = storage.connect()
    try:
        record = delegate_receipts._read(conn, key)
        assert record["state"] == "in_progress"
        aged = datetime.now(UTC) - timedelta(seconds=delegate_receipts.STALE_AFTER_S + 1)
        delegate_receipts._write(conn, key, {**record, "since": aged.isoformat()})
        conn.commit()
    finally:
        conn.close()

    created = _create("takeover-1")
    [row] = delegations()
    assert created["delegation_id"] == row["id"]
    assert created["task_id"] == orphan_task == row["task_id"]
    assert created["memory_id"] == notes[0] == row["memory_id"]
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        conn.close()
    assert _create("takeover-1") == created


def test_aged_in_progress_receipt_is_taken_over(monkeypatch):
    from unified_agent.tools import delegate_receipts

    key = delegate_receipts.storage_key("Desktop_Agent2", "aged-1")
    request = {
        "task_spec": SPEC.strip(),
        "from_agent": "Desktop_Agent2",
        "to_agent": "server_agent1",
        "deadline": None,
        "callback": "none",
        "priority": "med",
        "parent_task_id": None,
        "project": None,
    }
    conn = storage.connect()
    try:
        delegate_receipts._write(
            conn,
            key,
            {
                "state": "in_progress",
                "request": delegate_receipts.request_digest(request),
                "token": "dead-holder",
                "since": "2000-01-01T00:00:00+00:00",
            },
        )
        conn.commit()
    finally:
        conn.close()
    created = _create("aged-1")
    assert [row["id"] for row in delegations()] == [created["delegation_id"]]


def test_concurrent_gateway_claims_have_exactly_one_winner(live, capsys, tmp_path, monkeypatch):
    """Two credentials race task_claim through the gateway: one wins, one is told to wait."""
    from unified_agent.cli.result import CliError

    task = ua(capsys, "task", "add", "--title", "race target")["data"]["id"]
    monkeypatch.delenv("UA_AGENT_BEARER_TOKEN")
    barrier = threading.Barrier(2)
    outcomes = {}

    def claim(label):
        token_file = tmp_path / f"{label}.bearer"
        token_file.write_text(live["tokens"][label], encoding="utf-8")
        hub = client.ReadClient(None, label, 30, str(token_file))
        try:
            hub.whoami()
            barrier.wait()
            outcomes[label] = hub.tool("task_claim", {"id": task, "agent": label}, write=True)
        except CliError as error:
            outcomes[label] = error.code
        finally:
            hub.close()

    threads = [threading.Thread(target=claim, args=(label,)) for label in live["tokens"]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    winners = [label for label, value in outcomes.items() if value.get("claimed_by") == label]
    losers = [value for value in outcomes.values() if value.get("claimed") is False]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0]["safe_wait"]["claimed_by"] == winners[0]


# --- approvals and revocation --------------------------------------------------


def _resolve(approval_id, decision):
    from unified_agent.tools.approvals import approval_resolve

    operator = config.operator_label()
    token = as_identity(operator)
    try:
        approval_resolve(approval_id, decision, resolved_by=operator)
    finally:
        config.agent_identity_var.reset(token)


def test_approval_pending_outcome_then_one_shot_consumption(live, capsys, spec_file, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate")
    parked = ua(capsys, *start_args(spec_file, "--idempotency-key", "gate-1"), code=9)
    assert parked["outcome"] == "approval_required"
    approval = parked["error"]["approval_id"]
    assert parked["error"]["reference"]["state"] == "approval_required"
    looked = ua(capsys, "delegate", "lookup", "gate-1")["data"]
    assert looked == {
        "idempotency_key": "gate-1",
        "state": "approval_required",
        "approval_id": approval,
    }
    # Reconciling before a decision keeps the parked outcome; nothing is created.
    assert ua(capsys, "reconcile", "gate-1", code=9)["error"]["approval_id"] == approval
    assert delegations() == []

    _resolve(approval, "approve")
    done = ua(capsys, "reconcile", "gate-1", "--retry")["data"]
    [row] = delegations()
    assert done["result"]["delegation_id"] == row["id"]
    # A replay of the committed key needs no second approval.
    again = ua(capsys, *start_args(spec_file, "--idempotency-key", "gate-1"))["data"]
    assert again["result"] == done["result"]
    assert ua(capsys, "approval", "list")["data"]["pending"] == []


def test_revoked_approval_is_not_consumed_by_the_retry(live, capsys, spec_file, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate")
    parked = ua(capsys, *start_args(spec_file, "--idempotency-key", "revoke-1"), code=9)
    approval = parked["error"]["approval_id"]
    _resolve(approval, "approve")
    _resolve(approval, "revoke")
    reparked = ua(capsys, "reconcile", "revoke-1", "--retry", code=9)
    assert reparked["error"]["approval_id"] != approval
    assert delegations() == []


def test_credential_revoked_mid_wait_keeps_the_reference_and_never_cancels(
    live, capsys, spec_file, monkeypatch, clock
):
    tokens = live["tokens"]

    def revoke():
        monkeypatch.setenv("UA_AGENT_TOKENS", f"server_agent1:{tokens['server_agent1']}")

    clock["hooks"].append(revoke)
    refused = ua(capsys, *start_args(spec_file, "--idempotency-key", "revoked-1", "--wait"), code=3)
    assert refused["error"]["code"] == "authentication"
    reference = refused["error"]["reference"]
    [row] = delegations()
    assert reference["delegation_id"] == row["id"] and reference["state"] == "committed"
    assert row["status"] == "pending"
    assert not any(
        name in {"dispatch_cancel"} or action == "cancel" for name, action in live["seen"]
    )


# --- bounded waits and intervention -------------------------------------------


def test_wait_timeout_is_in_progress_with_a_durable_reference(live, capsys, spec_file, clock):
    timed = ua(
        capsys,
        *start_args(spec_file, "--idempotency-key", "wait-1", "--wait", "--wait-timeout", "5"),
        code=10,
    )
    assert timed["outcome"] == "in_progress"
    reference = timed["error"]["reference"]
    [row] = delegations()
    assert reference["delegation_id"] == row["id"] and reference["status"] == "pending"
    assert row["status"] == "pending"  # ending the wait cancelled nothing

    from unified_agent.tools.tasks import task_update

    clock["hooks"].append(lambda: task_update(row["task_id"], status="cancelled"))
    waited = ua(capsys, "delegate", "wait", str(row["id"]), "--wait-timeout", "30")["data"]
    assert waited["terminal"] is True and waited["status"] == "cancelled"


def test_wait_survives_a_lost_poll_by_reconnecting(live, capsys, spec_file, clock, monkeypatch):
    created = ua(capsys, *start_args(spec_file, "--idempotency-key", "poll-1"))["data"]
    delegation_id = created["result"]["delegation_id"]
    task_id = created["result"]["task_id"]
    from unified_agent.tools.tasks import task_update

    real_read = execution._read_delegation
    calls = {"n": 0}

    def flaky(hub, delegation):
        calls["n"] += 1
        if calls["n"] == 1:
            raise execution.CliError("transport", "lost", 6)
        if calls["n"] == 2:
            task_update(task_id, status="cancelled")
        return real_read(hub, delegation)

    monkeypatch.setattr(execution, "_read_delegation", flaky)
    waited = ua(capsys, "delegate", "wait", str(delegation_id))["data"]
    assert waited["status"] == "cancelled" and calls["n"] == 2


def test_wait_never_reads_past_its_deadline(live, capsys, spec_file, clock):
    created = ua(capsys, *start_args(spec_file, "--idempotency-key", "deadline-1"))["data"]
    delegation_id = created["result"]["delegation_id"]
    from unified_agent.tools.tasks import task_update

    # The delegation turns terminal only once the 5 s budget is spent.
    clock["hooks"].extend([lambda: None] * 4)
    clock["hooks"].append(lambda: task_update(created["result"]["task_id"], status="cancelled"))
    argv = ("delegate", "wait", str(delegation_id), "--wait-timeout", "5", "--poll-interval", "1")
    timed = ua(capsys, *argv, code=10)
    assert timed["outcome"] == "in_progress" and timed["error"]["code"] == "in_progress"
    assert timed["error"]["reference"]["status"] == "pending"


def test_interrupting_a_wait_or_follow_cancels_nothing(live, capsys, spec_file, clock, tmp_path):
    created = ua(capsys, *start_args(spec_file, "--idempotency-key", "ctrl-c-1"))["data"]

    def interrupt():
        raise KeyboardInterrupt

    clock["hooks"].append(interrupt)
    stopped = ua(capsys, "delegate", "wait", str(created["result"]["delegation_id"]), code=10)
    assert stopped["error"]["code"] == "interrupted"
    assert stopped["error"]["reference"]["status"] == "pending"
    assert delegations()[0]["status"] == "pending"

    expected = _task_events(1)
    cursor = tmp_path / "cursor.json"
    clock["hooks"].append(interrupt)
    argv = [
        "events",
        "follow",
        "--from-start",
        "--channel",
        "agent.task.*",
        "--format",
        "json",
        "--cursor-file",
        str(cursor),
    ]
    assert run(argv) == 130
    lines = _jsonl(capsys)
    assert lines[-1]["reason"] == "interrupted" and lines[-1]["cursor"] == expected[-1]
    assert json.loads(cursor.read_text())["after_seq"] == expected[-1]


def test_delegate_cancel_respects_the_claim_generation_fence(live, capsys, spec_file):
    created = ua(capsys, *start_args(spec_file, "--idempotency-key", "fence-1"))["data"]
    delegation = str(created["result"]["delegation_id"])
    stale = ua(capsys, "delegate", "cancel", delegation, "--expected-claim-generation", "7", code=8)
    assert stale["outcome"] == "conflict"
    assert delegations()[0]["status"] == "pending"
    done = ua(capsys, "delegate", "cancel", delegation, "--expected-claim-generation", "0")
    assert done["data"]["cancelled"] is True
    assert delegations()[0]["status"] == "cancelled"
    # A second cancel changes nothing and is reported, not invented.
    assert ua(capsys, "delegate", "cancel", delegation, code=8)["outcome"] == "conflict"


def test_run_cancel_of_another_agents_run_is_refused_by_the_hub(live, capsys):
    refused = ua(capsys, "run", "cancel", "run-unknown", code=1)
    assert refused["error"]["reference"]["run_id"] == "run-unknown"
    assert ("dispatch_cancel", None) in live["seen"]


# --- resumable event stream ---------------------------------------------------


def _task_events(count):
    from unified_agent.tools.tasks import task_add

    token = as_identity("Desktop_Agent2")
    try:
        for index in range(count):
            task_add(title=f"event source {index}")
    finally:
        config.agent_identity_var.reset(token)
    conn = storage.connect()
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT ROWID FROM event_log WHERE channel LIKE 'agent.task.%' ORDER BY ROWID"
            )
        ]
    finally:
        conn.close()


def _jsonl(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def test_stream_reconnects_without_gaps_or_duplicates(live, capsys, clock):
    expected = _task_events(7)
    assert len(expected) == 7
    # One page is served then lost after the hub answered; one never arrives.
    live["drop_after"].append("event_log_query")
    live["fail_before"].extend(["event_log_query"])
    code = run(
        [
            "events",
            "follow",
            "--from-start",
            "--channel",
            "agent.task.*",
            "--limit",
            "3",
            "--max-events",
            "7",
            "--format",
            "json",
        ]
    )
    assert code == 0
    lines = _jsonl(capsys)
    assert [line["seq"] for line in lines if line["type"] == "event"] == expected
    assert lines[-1] == {
        "schema_version": "ua.event/1",
        "type": "end",
        "reason": "max_events",
        "cursor": expected[-1],
    }


def test_cursor_file_resumes_across_invocations(live, capsys, clock, tmp_path):
    expected = _task_events(5)
    cursor = tmp_path / "cursor.json"
    base = [
        "events",
        "follow",
        "--channel",
        "agent.task.*",
        "--format",
        "json",
        "--cursor-file",
        str(cursor),
    ]
    assert run([*base, "--from-start", "--max-events", "2"]) == 0
    first = [line["seq"] for line in _jsonl(capsys) if line["type"] == "event"]
    assert json.loads(cursor.read_text())["after_seq"] == first[-1]
    assert run([*base, "--once"]) == 0
    second = [line["seq"] for line in _jsonl(capsys) if line["type"] == "event"]
    assert first + second == expected


def test_stream_stops_on_revocation_and_reports_its_cursor(live, capsys, clock, monkeypatch):
    expected = _task_events(2)
    tokens = live["tokens"]
    clock["hooks"].append(
        lambda: monkeypatch.setenv("UA_AGENT_TOKENS", f"server_agent1:{tokens['server_agent1']}")
    )
    code = run(
        ["events", "follow", "--from-start", "--channel", "agent.task.*", "--format", "json"]
    )
    assert code == 3
    lines = _jsonl(capsys)
    assert [line["seq"] for line in lines if line.get("type") == "event"] == expected
    assert lines[-1]["error"]["reference"] == {"after_seq": expected[-1]}


def test_follow_ends_on_duration_while_reads_keep_timing_out(
    live, capsys, clock, monkeypatch, tmp_path
):
    real_tool = client.ReadClient.tool
    calls = {"n": 0}

    def slow_timeout(self, name, arguments, *, write=False):
        if name != "event_log_query":
            return real_tool(self, name, arguments, write=write)
        calls["n"] += 1
        clock["now"] += 10.0
        raise execution.CliError("timeout", "Hub read timed out.", 7)

    monkeypatch.setattr(client.ReadClient, "tool", slow_timeout)
    cursor = tmp_path / "cursor.json"
    argv = ["events", "follow", "--from-start", "--duration", "1", "--max-reconnects", "5"]
    argv += ["--cursor-file", str(cursor), "--format", "json"]
    assert run(argv) == 0
    lines = _jsonl(capsys)
    assert lines == [
        {"schema_version": "ua.event/1", "type": "end", "reason": "duration", "cursor": 0}
    ]
    assert calls["n"] <= 2
    assert json.loads(cursor.read_text())["after_seq"] == 0


def test_follow_requires_an_explicit_start(live, capsys):
    assert ua(capsys, "events", "follow", code=2)["error"]["code"] == "validation"


# --- input boundaries ---------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["delegate", "start", "--to", "server_agent1", "--spec", "do it now"],
        ["delegate", "start", "--to", "server_agent1", "--prompt", "do it now"],
        ["delegate", "start", "--to", "server_agent1"],
        ["delegate", "start", "--to", "server_agent1", "--spec-file", "x", "--from-agent", "y"],
        [
            "delegate",
            "start",
            "--to",
            "x",
            "--spec-file",
            "x",
            "--new-key",
            "--idempotency-key",
            "k",
        ],
        ["delegate", "wait", "1", "--wait-timeout", "0"],
        ["events", "follow", "--from-start", "--limit", "9999"],
    ],
)
def test_prompts_identity_and_unbounded_waits_are_refused_before_transport(
    live, capsys, argv, monkeypatch
):
    monkeypatch.setattr(client.ReadClient, "_post", lambda *a, **k: pytest.fail("sent"))
    assert run([*argv, "--format", "json"]) == 2


def test_invalid_key_and_empty_spec_are_refused(live, capsys, spec_file, tmp_path):
    bad = ua(capsys, *start_args(spec_file, "--idempotency-key", "bad key"), code=2)
    assert bad["error"]["code"] == "validation"
    empty = tmp_path / "empty.md"
    empty.write_text("  \n", encoding="utf-8")
    assert ua(capsys, *start_args(str(empty)), code=2)["error"]["code"] == "validation"
    assert delegations() == []


@pytest.mark.parametrize(
    "args",
    [
        ["delegate", "--help"],
        ["events", "follow", "--help"],
        ["reconcile", "--help"],
        ["workflow", "run", "--help"],
        ["delegate", "start"],
    ],
)
def test_execution_help_and_parse_failures_do_not_load_hub(tmp_path, args):
    source = Path(__file__).resolve().parents[1] / "src"
    script = """
import sys
from unified_agent.cli.main import run
code = run(sys.argv[1:])
assert not any(name in sys.modules for name in (
    'unified_agent.server', 'unified_agent.storage', 'unified_agent.config',
    'unified_agent.object_model', 'unified_agent.executor.daemon'))
raise SystemExit(code)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(source),
            "UA_HOME": str(tmp_path / "missing"),
            "UA_CLI_STATE_DIR": str(tmp_path / "state"),
        },
        check=False,
    )
    assert completed.returncode == (0 if "--help" in args else 2), completed.stderr
    assert not (tmp_path / "missing").exists() and not (tmp_path / "state").exists()


# --- workflows over a fixture hub ----------------------------------------------


@pytest.fixture
def workflow_hub(monkeypatch, tmp_path):
    from unified_agent import device_login

    for name in ("UA_EXECUTOR_TOKEN", "UA_AGENT_TOKENS", "UA_EXECUTOR_AGENT", "UA_CLI_IDENTITY"):
        monkeypatch.delenv(name, raising=False)
    bearer = secrets.token_urlsafe(16)
    monkeypatch.setenv("UA_HTTP_HUB_URL", "https://hub.example.test/mcp")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", bearer)
    monkeypatch.setenv("UA_CLI_STATE_DIR", str(tmp_path / "cli-state"))
    monkeypatch.setattr(device_login, "stored_access_token", lambda **kwargs: None)
    state = {"result": {"workflow": "w", "status": "done", "steps": [], "outputs": {}}}
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "inputs": {"type": ["object", "null"]},
            "instance_id": {"type": ["string", "null"]},
        },
        "required": ["name"],
    }
    tools = [{"name": "workflow_run", "inputSchema": schema, "annotations": {}}]

    def handle(request):
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "s"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            result = {"tools": tools}
        elif body["params"]["name"] == "profile_status":
            result = {"structuredContent": {"identity": "Desktop_Agent2", "profile": "developer"}}
        else:
            state["sent"] = body["params"]
            if state.get("timeout"):
                raise httpx.ReadTimeout("lost", request=request)
            result = {"content": [{"type": "text", "text": json.dumps(state["result"])}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    real_client = httpx.Client
    monkeypatch.setattr(
        client.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return state


@pytest.mark.parametrize(
    "status,code,outcome",
    [
        ("done", 0, "completed"),
        ("failed", 1, "failed"),
        ("denied", 4, "failed"),
        ("indeterminate", 11, "unknown"),
        ("pending-approval", 9, "approval_required"),
    ],
)
def test_workflow_outcomes_are_distinct(workflow_hub, capsys, status, code, outcome):
    workflow_hub["result"] = {
        "workflow": "w",
        "status": status,
        "steps": [
            {
                "name": "s1",
                "tool": "task_list",
                "status": status,
                "attempts": 1,
                "error": "private hub prose",
            }
        ],
    }
    out = ua(capsys, "workflow", "run", "w", "--inputs", '{"task_id": 5}', code=code)
    assert out["outcome"] == outcome
    assert workflow_hub["sent"]["arguments"] == {"name": "w", "inputs": {"task_id": 5}}
    if status != "done":
        # A refusal reports step names and states, never the hub's step prose.
        assert "private hub prose" not in json.dumps(out)


def test_workflow_timeout_is_unknown_and_offers_no_blind_retry(workflow_hub, capsys):
    workflow_hub["timeout"] = True
    out = ua(capsys, "workflow", "run", "w", code=11)
    assert out["outcome"] == "unknown"
    assert out["error"]["reference"] == {"workflow": "w"}


# --- review round 1: receipt index, tenant transition, cursor delivery --------


def _unsettled(key, digest, state="unknown"):
    receipt = receipts.prepare(
        hub="https://hub.example.test/mcp",
        identity="Desktop_Agent2",
        command="delegate.start",
        tool="delegation_manage",
        arguments={"n": key},
        key=key,
    )
    receipt.update(state=state, request_digest=digest)
    receipts.save(receipt)
    return receipt


def test_retry_lookup_is_exact_however_many_receipts_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_CLI_STATE_DIR", str(tmp_path / "state"))
    # A former scan bound must not decide whether a retry reuses its key.
    monkeypatch.setattr(receipts, "_MAX_SCAN", 0, raising=False)
    for index in range(30):
        _unsettled(f"decoy-{index}", f"digest-{index}", state="committed")
    target = _unsettled("target-1", "the-digest")
    assert receipts.find_retry("the-digest")["idempotency_key"] == target["idempotency_key"]
    assert receipts.find_retry("absent-digest") is None


def test_unreadable_retry_index_fails_closed(tmp_path, monkeypatch):
    from unified_agent.cli.result import CliError

    monkeypatch.setenv("UA_CLI_STATE_DIR", str(tmp_path / "state"))
    _unsettled("target-2", "digest-2")
    receipts.path_for("target-2").write_text("{not json", encoding="utf-8")
    with pytest.raises(CliError) as refused:
        receipts.find_retry("digest-2")
    assert "--idempotency-key" in refused.value.message and "--new-key" in refused.value.message
    for index_file in receipts.index_directory().iterdir():
        index_file.write_text("garbage", encoding="utf-8")
    with pytest.raises(CliError):
        receipts.find_retry("digest-2")


def test_unreadable_index_refuses_start_before_sending(live, capsys, spec_file, monkeypatch):
    live["drop_after"].append(("delegation_manage", "create"))
    lost = ua(capsys, *start_args(spec_file), code=11)
    key = lost["error"]["reference"]["idempotency_key"]
    receipts.path_for(key).write_text("{not json", encoding="utf-8")
    before = list(live["seen"])
    refused = ua(capsys, *start_args(spec_file), code=2)
    assert refused["error"]["code"] == "receipt_unreadable"
    assert ("delegation_manage", "create") not in live["seen"][len(before) :]
    assert len(delegations()) == 1


def test_old_committed_receipts_are_pruned_and_unsettled_kept(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("UA_CLI_STATE_DIR", str(tmp_path / "state"))
    old = _unsettled("old-committed", "d-old", state="committed")
    _unsettled("new-committed", "d-new", state="committed")
    parked = _unsettled("old-parked", "d-parked", state="approval_required")
    aged = time.time() - (receipts.RETENTION_DAYS + 1) * 86400
    for receipt in (old, parked):
        os.utime(receipts.path_for(receipt["idempotency_key"]), (aged, aged))
    removed = receipts.prune()
    assert removed == 1
    assert not receipts.path_for("old-committed").exists()
    assert receipts.path_for("new-committed").exists()
    assert receipts.find_retry("d-parked")["idempotency_key"] == "old-parked"
    assert receipts.find_retry("d-old") is None


def test_tenant_transition_refuses_a_pre_tenant_receipt_instead_of_duplicating(
    monkeypatch,
):
    first = _create("tenant-1")
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="new idempotency key"):
        _create("tenant-1")
    with pytest.raises(ToolError, match="new idempotency key"):
        from unified_agent.tools.delegate import delegation_manage

        token = as_identity("Desktop_Agent2")
        try:
            delegation_manage(
                "lookup", {"idempotency_key": "tenant-1", "from_agent": "Desktop_Agent2"}
            )
        finally:
            config.agent_identity_var.reset(token)
    assert [row["id"] for row in delegations()] == [first["delegation_id"]]


def test_follow_is_at_least_once_across_a_crash_and_seq_dedupes(
    live, capsys, clock, tmp_path, monkeypatch
):
    expected = _task_events(4)
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"after_seq": 0}), encoding="utf-8")
    argv = [
        "events",
        "follow",
        "--channel",
        "agent.task.*",
        "--format",
        "json",
        "--cursor-file",
        str(cursor),
        "--once",
    ]
    real_write = execution._write_cursor

    def crash(path, seq):
        raise SystemExit(137)  # the process dies after printing, before saving

    monkeypatch.setattr(execution, "_write_cursor", crash)
    run(argv)
    first = [line["seq"] for line in _jsonl(capsys) if line.get("type") == "event"]
    assert first == expected
    monkeypatch.setattr(execution, "_write_cursor", real_write)
    assert run(argv) == 0
    second = [line["seq"] for line in _jsonl(capsys) if line.get("type") == "event"]
    # Re-delivered, never skipped: a consumer that dedupes by seq sees each once.
    assert second == expected
    assert sorted(set(first + second)) == expected


# --- review round 2: one key per fingerprint under concurrency ----------------


def test_overlapping_identical_starts_share_one_key_and_one_delegation(
    live, spec_file, tmp_path, monkeypatch
):
    """Two overlapping `delegate start` runs of the same request, paused between
    the retry lookup and the receipt save, must agree on one key."""
    from unified_agent.cli.main import parser
    from unified_agent.cli.result import CliError

    monkeypatch.delenv("UA_AGENT_BEARER_TOKEN")
    token_file = tmp_path / "codex.bearer"
    token_file.write_text(live["tokens"]["Desktop_Agent2"], encoding="utf-8")
    second_looked = threading.Event()
    lookups = {"n": 0}
    lock = threading.Lock()
    real_find = receipts.find_retry

    def paused_find(digest):
        found = real_find(digest)
        with lock:
            lookups["n"] += 1
            order = lookups["n"]
        if order == 1:
            second_looked.wait(2)  # the window between lookup and save
        else:
            second_looked.set()
        return found

    real_tool = client.ReadClient.tool

    def held_send(self, name, arguments, *, write=False):
        if arguments.get("action") == "create":
            # Keep the first request unsettled until the other run has looked.
            second_looked.wait(5)
        return real_tool(self, name, arguments, write=write)

    monkeypatch.setattr(receipts, "find_retry", paused_find)
    monkeypatch.setattr(client.ReadClient, "tool", held_send)
    keys = []

    def start():
        args = parser().parse_args([*start_args(spec_file)])
        args.project = "unified-agent"
        hub = client.ReadClient(None, "Desktop_Agent2", 30, str(token_file))
        try:
            identity = hub.whoami()
            tool = next(item for item in hub.tools() if item["name"] == "delegation_manage")
            try:
                keys.append(execution.delegate_start(args, hub, identity, tool)["reference"])
            except CliError as error:
                keys.append(error.reference)
        finally:
            hub.close()

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(keys) == 2
    assert keys[0]["idempotency_key"] == keys[1]["idempotency_key"]
    assert len(delegations()) == 1


def test_save_never_repoints_an_index_that_names_an_unsettled_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_CLI_STATE_DIR", str(tmp_path / "state"))
    first = _unsettled("first-1", "shared-digest", state="prepared")
    _unsettled("second-1", "shared-digest", state="prepared")
    assert receipts.find_retry("shared-digest")["idempotency_key"] == first["idempotency_key"]
    first["state"] = "committed"
    receipts.save(first)
    second = receipts.load("second-1")
    receipts.save(second)
    assert receipts.find_retry("shared-digest")["idempotency_key"] == "second-1"


def test_prune_keeps_an_index_a_concurrent_save_repointed(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("UA_CLI_STATE_DIR", str(tmp_path / "state"))
    old = _unsettled("old-done", "race-digest", state="committed")
    aged = time.time() - (receipts.RETENTION_DAYS + 1) * 86400
    os.utime(receipts.path_for(old["idempotency_key"]), (aged, aged))
    index = receipts.index_directory() / "race-digest.json"
    real_unlink = Path.unlink
    saver = {}

    def save_new():
        receipt = receipts.prepare(
            hub="https://hub.example.test/mcp",
            identity="Desktop_Agent2",
            command="delegate.start",
            tool="delegation_manage",
            arguments={"n": "new"},
            key="new-inflight",
        )
        receipt["request_digest"] = "race-digest"
        receipts.save(receipt)

    def interleaved_unlink(self, *args, **kwargs):
        if self == index and "thread" not in saver:
            # A concurrent identical start publishes its new in-flight key here.
            saver["thread"] = threading.Thread(target=save_new)
            saver["thread"].start()
            saver["thread"].join(1)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interleaved_unlink)
    assert receipts.prune() == 1
    saver["thread"].join(30)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert receipts.find_retry("race-digest")["idempotency_key"] == "new-inflight"
