"""Session-scoped evidence (task #1989).

Streamed ACP events carry item_id but no task_id, so a task's evidence report
showed the dispatch's enqueue and receipt and none of its turns. The hub now
records one ``turn_summary`` per turn that carries task_id, and the evidence
report filters to one dispatch item (``nd-…`` or ``node:<node>:<item>``).
"""

from __future__ import annotations

import json
import secrets

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, gateway, node_queue, server, storage, webui
from unified_agent.audit import evidence_report
from unified_agent.tools import tasks

NODE = "evidence-node"
IDENTITY = "Desktop_Agent1"


def _claimed(tmp_path, task_id: int | None) -> str:
    node_queue.register(NODE, identity=IDENTITY, facts={"backends": ["codex"]})
    node_queue.enqueue(
        NODE,
        created_by=IDENTITY,
        prompt="fixture",
        cwd=str(tmp_path),
        backend="codex",
        task_id=task_id,
    )
    item = node_queue.claim_next(NODE, identity=IDENTITY)
    assert item is not None
    return item["item_id"]


def _turn(turn_id: str) -> list[dict]:
    return [
        {"kind": "turn_start", "turn_id": turn_id, "data": {"turn_id": turn_id}},
        {"kind": "tool_call", "turn_id": turn_id, "data": {"toolCallId": "t1"}},
        {
            "kind": "tool_call_update",
            "turn_id": turn_id,
            "data": {"toolCallId": "t1", "status": "completed"},
        },
        {"kind": "tool_call", "turn_id": turn_id, "data": {"toolCallId": "t2"}},
        {
            "kind": "tool_call_update",
            "turn_id": turn_id,
            "data": {"toolCallId": "t2", "status": "failed"},
        },
        {"kind": "agent_message_chunk", "turn_id": turn_id, "text": "done"},
        {
            "kind": "turn_end",
            "turn_id": turn_id,
            "data": {"turn_id": turn_id, "stop_reason": "end_turn"},
        },
    ]


def _node_payloads(kind: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ROWID",
            (node_queue.CHANNEL_NODE,),
        ).fetchall()
    finally:
        conn.close()
    return [p for p in (json.loads(r["payload"]) for r in rows) if p.get("kind") == kind]


def test_turn_end_records_one_task_summary_with_tool_counts(tmp_path):
    task_id = tasks.task_add("session evidence fixture")["id"]
    item_id = _claimed(tmp_path, task_id)
    node_queue.record_node_event(NODE, item_id, _turn("turn-1"), identity=IDENTITY)
    # A node that re-sends the batch (spool replay) must not double the summary.
    node_queue.record_node_event(NODE, item_id, _turn("turn-1")[-1:], identity=IDENTITY)

    [summary] = _node_payloads("turn_summary")
    assert summary["task_id"] == task_id
    assert summary["item_id"] == item_id
    assert summary["evidence_ref"] == f"node:{NODE}:{item_id}"
    assert summary["turn_id"] == "turn-1"
    assert summary["stop_reason"] == "end_turn"
    assert summary["tool_calls"] == 2
    assert summary["tool_call_updates"] == 2
    assert summary["tool_calls_failed"] == 1
    assert summary["events"] >= 7
    # The streamed events themselves stay off the task chain.
    assert all("task_id" not in p for p in _node_payloads("acp_event"))


def test_each_turn_gets_its_own_summary(tmp_path):
    task_id = tasks.task_add("two turns")["id"]
    item_id = _claimed(tmp_path, task_id)
    node_queue.record_node_event(NODE, item_id, _turn("turn-1"), identity=IDENTITY)
    node_queue.record_node_event(NODE, item_id, _turn("turn-2")[-2:], identity=IDENTITY)
    summaries = _node_payloads("turn_summary")
    assert [s["turn_id"] for s in summaries] == ["turn-1", "turn-2"]
    assert summaries[1]["tool_calls"] == 0


def test_a_dispatch_without_a_task_records_no_summary(tmp_path):
    item_id = _claimed(tmp_path, None)
    node_queue.record_node_event(NODE, item_id, _turn("turn-1"), identity=IDENTITY)
    assert _node_payloads("turn_summary") == []


def test_the_task_report_shows_the_turn_summary(tmp_path):
    task_id = tasks.task_add("report fixture")["id"]
    item_id = _claimed(tmp_path, task_id)
    before = evidence_report.build_evidence_report(task_id=task_id)
    node_queue.record_node_event(NODE, item_id, _turn("turn-1"), identity=IDENTITY)
    after = evidence_report.build_evidence_report(task_id=task_id)
    grown = (
        after["records"][0]["proof"]["events_count"] - before["records"][0]["proof"]["events_count"]
    )
    assert grown == 1, "one summary per turn, not every streamed event"


@pytest.mark.parametrize("form", ["id", "evidence_ref"])
def test_report_filters_to_one_dispatch_item(tmp_path, form):
    task_id = tasks.task_add("item filter fixture")["id"]
    first = _claimed(tmp_path, task_id)
    node_queue.record_node_event(NODE, first, _turn("turn-1"), identity=IDENTITY)
    second = _claimed(tmp_path, task_id)
    node_queue.record_node_event(NODE, second, _turn("turn-1"), identity=IDENTITY)
    tasks.task_add("unrelated task")
    # A later dispatch may have replaced the task's completion pointer.
    tasks.task_update(task_id, evidence_ref=f"node:{NODE}:{second}")

    item = first if form == "id" else f"node:{NODE}:{first}"
    report = evidence_report.build_evidence_report(item=item)
    assert [r["task_id"] for r in report["records"]] == [task_id]
    assert report["filter"]["item"] == item
    assert report["records"][0]["proof"]["evidence_ref"] == f"node:{NODE}:{first}"
    whole = evidence_report.build_evidence_report(task_id=task_id)
    scoped = report["records"][0]["proof"]["events_count"]
    assert 0 < scoped < whole["records"][0]["proof"]["events_count"]


def test_report_item_filter_without_a_task_or_match_is_empty(tmp_path):
    tasks.task_add("not this one")
    loose = _claimed(tmp_path, None)
    for item in (loose, "nd-0000000000000000", f"node:other-node:{loose}"):
        report = evidence_report.build_evidence_report(item=item)
        assert report["records"] == [], item


def test_report_refuses_a_malformed_item():
    with pytest.raises(ToolError, match="invalid node dispatch item"):
        evidence_report.build_evidence_report(item="node:x")


def test_unfiltered_report_filter_block_is_unchanged():
    tasks.task_add("shape fixture")
    report = evidence_report.build_evidence_report()
    assert "item" not in report["filter"]


def test_api_evidence_takes_an_item_filter(tmp_path, monkeypatch):
    task_id = tasks.task_add("route fixture")["id"]
    item_id = _claimed(tmp_path, task_id)
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:test-token-1989")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        ok = client.get(f"/api/evidence?item=node:{NODE}:{item_id}")
        bad = client.get("/api/evidence?item=not-an-item")
    assert ok.status_code == 200, ok.text
    assert [r["task_id"] for r in ok.json()["records"]] == [task_id]
    assert bad.status_code == 400
    assert "item" in bad.json()["error"]


def test_item_filter_removes_receipts_from_other_dispatches(tmp_path, monkeypatch):
    from unified_agent import delivery_receipts

    task_id = tasks.task_add("receipt filter")["id"]
    item_id = _claimed(tmp_path, task_id)
    ref = f"node:{NODE}:{item_id}"
    monkeypatch.setattr(
        delivery_receipts,
        "receipts_for_task",
        lambda *a, **kw: [
            {"run_id": "selected", "evidence_ref": ref},
            {"run_id": "other", "evidence_ref": "node:other:nd-0000000000000000"},
        ],
    )
    report = evidence_report.build_evidence_report(item=item_id)
    assert [r["run_id"] for r in report["records"][0]["delivery_receipts"]] == ["selected"]


def test_item_filter_leaves_the_project_chain_on_unfiltered_proof(tmp_path):
    """The item override narrows display records, never the project chain."""
    root = tasks.task_add("item project", notes="[project-spec]")["id"]
    pr_url = "https://github.com/example/repo/pull/4242"
    merged = tasks.task_add("merged child", parent_id=root, evidence_ref=pr_url)["id"]
    tasks.task_update(merged, status="done", evidence_ref=pr_url, actor=IDENTITY)
    dispatched = tasks.task_add("dispatched child", parent_id=root)["id"]
    item_id = _claimed(tmp_path, dispatched)
    node_queue.record_node_event(NODE, item_id, _turn("turn-1"), identity=IDENTITY)
    tasks.task_claim(dispatched, agent=IDENTITY)

    report = evidence_report.build_evidence_report(project_id=root, item=item_id)

    assert [r["task_id"] for r in report["records"]] == [dispatched]
    steps = {step["task_id"]: step["proof"] for step in report["project_chain"]["steps"]}
    assert steps[merged]["evidence_ref"] == pr_url
    assert steps[merged]["pr_url"] == pr_url
    assert [e["task_id"] for e in report["project_chain"]["evidence"]] == [merged]
    unfiltered = evidence_report.build_evidence_report(project_id=root)
    assert report["project_chain"]["steps"] == unfiltered["project_chain"]["steps"]


def test_item_filter_attributes_the_selected_dispatch_not_the_task(tmp_path):
    """A second dispatch's claimant must not be reported as the item's agent."""
    task_id = tasks.task_add("two dispatchers")["id"]
    first = _claimed(tmp_path, task_id)
    other_node, other_identity = "evidence-node-b", "Desktop_Agent2"
    node_queue.register(other_node, identity=other_identity, facts={"backends": ["codex"]})
    node_queue.enqueue(
        other_node,
        created_by=other_identity,
        prompt="fixture",
        cwd=str(tmp_path),
        backend="codex",
        task_id=task_id,
    )
    second = node_queue.claim_next(other_node, identity=other_identity)
    assert second is not None
    tasks.task_claim(task_id, agent=other_identity)

    record = evidence_report.build_evidence_report(item=first)["records"][0]
    assert record["agent"] == IDENTITY
    assert record["device"] == f"unverified-device:{NODE}"
    scope = record["item_scope"]
    assert scope["item"] == f"node:{NODE}:{first}"
    assert scope["agent_source"] == "node_dispatch"
    assert "human_provenance" in scope["task_wide"]

    other = evidence_report.build_evidence_report(item=second["item_id"])["records"][0]
    assert other["agent"] == other_identity
    # The unfiltered task record keeps its task-wide attribution and shape.
    whole = evidence_report.build_evidence_report(task_id=task_id)["records"][0]
    assert whole["agent"] == other_identity
    assert "item_scope" not in whole


def test_a_failed_summary_write_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    task_id = tasks.task_add("summary failure")["id"]
    item_id = _claimed(tmp_path, task_id)
    real = node_queue.events.record_event

    def failing(channel, payload, **kw):
        if payload.get("kind") == "turn_summary":
            raise RuntimeError("ledger unavailable")
        return real(channel, payload, **kw)

    monkeypatch.setattr(node_queue.events, "record_event", failing)
    with caplog.at_level("WARNING", logger="unified_agent.node_queue"):
        result = node_queue.record_node_event(NODE, item_id, _turn("turn-1"), identity=IDENTITY)
    assert result["recorded"] == 7
    assert _node_payloads("turn_summary") == []
    assert any("turn summary not recorded" in r.getMessage() for r in caplog.records)


def test_item_proof_never_reads_a_commit_sha_from_the_dispatch_id(tmp_path):
    """``nd-<16 hex>`` is a dispatch id, not commit evidence."""
    task_id = tasks.task_add("no commit")["id"]
    item_id = _claimed(tmp_path, task_id)
    node_queue.record_node_event(NODE, item_id, _turn("turn-1"), identity=IDENTITY)

    proof = evidence_report.build_evidence_report(item=item_id)["records"][0]["proof"]
    assert proof["evidence_ref"] == f"node:{NODE}:{item_id}"
    assert proof["commit_sha"] is None
    assert proof["pr_url"] is None


def test_item_proof_ignores_another_dispatchs_task_wide_references(tmp_path):
    """Dispatch B's PR in the task notes is not dispatch A's evidence."""
    task_id = tasks.task_add("two dispatch refs")["id"]
    first = _claimed(tmp_path, task_id)
    _claimed(tmp_path, task_id)
    pr_url = "https://github.com/example/repo/pull/5151"
    tasks.task_update(
        task_id, append_note=f"[review: merged] {pr_url} commit: abcdef1234567", actor=IDENTITY
    )

    proof = evidence_report.build_evidence_report(item=first)["records"][0]["proof"]
    assert proof["pr_url"] is None
    assert proof["commit_sha"] is None
    assert proof["merge_claim"]["status"] == "none"
    # The unfiltered task record still reports the task-wide references.
    whole = evidence_report.build_evidence_report(task_id=task_id)["records"][0]["proof"]
    assert whole["pr_url"] == pr_url


def test_item_and_agent_filters_match_the_dispatchs_agent(tmp_path):
    task_id = tasks.task_add("agent filter")["id"]
    item_id = _claimed(tmp_path, task_id)
    tasks.task_claim(task_id, agent="Desktop_Agent2")

    hit = evidence_report.build_evidence_report(item=item_id, agent=IDENTITY)
    assert [r["task_id"] for r in hit["records"]] == [task_id]
    assert hit["records"][0]["agent"] == IDENTITY
    miss = evidence_report.build_evidence_report(item=item_id, agent="Desktop_Agent2")
    assert miss["records"] == []


def _aborted_turn(turn_id: str, reason: str) -> list[dict]:
    return [
        {"kind": "turn_start", "turn_id": turn_id, "data": {"turn_id": turn_id}},
        {"kind": "tool_call", "turn_id": turn_id, "data": {"toolCallId": "t1"}},
        {
            "kind": "tool_call_update",
            "turn_id": turn_id,
            "data": {"toolCallId": "t1", "status": "failed"},
        },
        {
            "kind": "turn_aborted",
            "turn_id": turn_id,
            "data": {"turn_id": turn_id, "reason": reason},
        },
    ]


@pytest.mark.parametrize("reason", ["cancel", "steer", "timeout", "error"])
def test_an_aborted_turn_records_a_summary_with_its_reason(tmp_path, reason):
    task_id = tasks.task_add("aborted turn")["id"]
    item_id = _claimed(tmp_path, task_id)
    node_queue.record_node_event(NODE, item_id, _aborted_turn("turn-1", reason), identity=IDENTITY)
    node_queue.record_node_event(
        NODE, item_id, _aborted_turn("turn-1", reason)[-1:], identity=IDENTITY
    )

    [summary] = _node_payloads("turn_summary")
    assert summary["task_id"] == task_id
    assert summary["outcome"] == "aborted"
    assert summary["abort_reason"] == reason
    assert summary["tool_calls"] == 1
    assert summary["tool_calls_failed"] == 1


def test_a_finished_turn_summary_says_it_completed(tmp_path):
    task_id = tasks.task_add("completed turn")["id"]
    item_id = _claimed(tmp_path, task_id)
    node_queue.record_node_event(NODE, item_id, _turn("turn-1"), identity=IDENTITY)
    [summary] = _node_payloads("turn_summary")
    assert summary["outcome"] == "completed"
    assert "abort_reason" not in summary


def test_failed_tool_calls_count_distinct_calls_including_the_initial_status(tmp_path):
    """A call that starts failed counts; repeated failed updates count once."""
    task_id = tasks.task_add("failed call counting")["id"]
    item_id = _claimed(tmp_path, task_id)
    turn = "turn-1"

    def update(call_id: str, status: str) -> dict:
        return {
            "kind": "tool_call_update",
            "turn_id": turn,
            "data": {"toolCallId": call_id, "status": status},
        }

    node_queue.record_node_event(
        NODE,
        item_id,
        [
            {"kind": "turn_start", "turn_id": turn, "data": {"turn_id": turn}},
            # Rejected at once: the initial tool_call already says failed.
            {"kind": "tool_call", "turn_id": turn, "data": {"toolCallId": "a", "status": "failed"}},
            {"kind": "tool_call", "turn_id": turn, "data": {"toolCallId": "b"}},
            # One call, two failed updates: still one failed call.
            update("b", "failed"),
            update("b", "failed"),
            # Failed, then retried to completion: its latest status is not failed.
            {"kind": "tool_call", "turn_id": turn, "data": {"toolCallId": "c"}},
            update("c", "failed"),
            update("c", "completed"),
            {"kind": "turn_end", "turn_id": turn, "data": {"turn_id": turn, "stop_reason": "x"}},
        ],
        identity=IDENTITY,
    )
    [summary] = _node_payloads("turn_summary")
    assert summary["tool_calls"] == 3
    assert summary["tool_call_updates"] == 4
    assert summary["tool_calls_failed"] == 2


def _set_task_times(task_id: int, ts: str) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET created_at=?, updated_at=?, completed_at=NULL WHERE id=?",
            (ts, ts, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_item_time_window_tests_the_dispatch_not_the_task_row(tmp_path):
    """A recent dispatch on a task last touched long ago is in a recent window."""
    task_id = tasks.task_add("old task, new dispatch")["id"]
    item_id = _claimed(tmp_path, task_id)
    _set_task_times(task_id, "2020-01-01T00:00:00+00:00")

    recent = evidence_report.build_evidence_report(item=item_id, since="2021-01-01")
    assert [r["task_id"] for r in recent["records"]] == [task_id]
    old = evidence_report.build_evidence_report(item=item_id, until="2020-12-31")
    assert old["records"] == [], "the dispatch was not active in that window"
    # Without an item, the window still tests the task row.
    assert (
        evidence_report.build_evidence_report(task_id=task_id, since="2021-01-01")["records"] == []
    )


def test_item_pr_filter_tests_the_items_own_proof(tmp_path):
    """Another dispatch's PR in the task notes does not select this item."""
    task_id = tasks.task_add("pr filter")["id"]
    item_id = _claimed(tmp_path, task_id)
    pr_url = "https://github.com/example/repo/pull/6161"
    tasks.task_update(task_id, append_note=f"merged {pr_url}", actor=IDENTITY)

    assert evidence_report.build_evidence_report(item=item_id, pr=pr_url)["records"] == []
    own = evidence_report.build_evidence_report(item=item_id, pr=item_id)
    assert [r["task_id"] for r in own["records"]] == [task_id]
    # Without an item, the PR filter still reads the task's notes.
    whole = evidence_report.build_evidence_report(pr=pr_url)
    assert [r["task_id"] for r in whole["records"]] == [task_id]


def test_turn_summary_counts_only_its_own_item_and_turn(tmp_path):
    """The count is exact under noise that shares one of its keys (task #2017)."""
    task_id = tasks.task_add("count isolation")["id"]
    item_id = _claimed(tmp_path, task_id)
    other_item = _claimed(tmp_path, task_id)
    # Same turn id on another item, another turn on this item, and the same
    # item/turn keys on a different channel and a non-ACP node event.
    node_queue.record_node_event(NODE, other_item, _turn("turn-1")[:-1], identity=IDENTITY)
    node_queue.record_node_event(NODE, item_id, _turn("turn-0")[1:-1], identity=IDENTITY)
    events.record_event(
        "other.channel", {"kind": "acp_event", "item_id": item_id, "turn_id": "turn-1"}
    )
    events.record_event(
        node_queue.CHANNEL_NODE, {"kind": "progress", "item_id": item_id, "turn_id": "turn-1"}
    )
    node_queue.record_node_event(NODE, item_id, _turn("turn-1"), identity=IDENTITY)

    [summary] = [s for s in _node_payloads("turn_summary") if s["item_id"] == item_id]
    assert summary["turn_id"] == "turn-1"
    assert summary["events"] == 7
    assert summary["tool_calls"] == 2
    assert summary["tool_call_updates"] == 2
    assert summary["tool_calls_failed"] == 1


def test_turn_summary_query_uses_the_item_turn_index(tmp_path):
    """The per-turn count must not scan every agent.node event (task #2017)."""
    _claimed(tmp_path, None)  # initializes the store
    conn = storage.connect()
    try:
        supported = storage.node_turn_index_supported(conn)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + node_queue._TURN_EVENTS_SQL,
            (node_queue.CHANNEL_NODE, "nd-0000000000000000", "turn-1"),
        ).fetchall()
    finally:
        conn.close()
    details = " ".join(r["detail"] for r in plan)
    if supported:
        assert "USING INDEX idx_event_log_node_item_turn" in details, details
    else:
        assert "idx_event_log_node_item_turn" not in details, details
        assert "USING INDEX idx_event_log_channel" in details, details


def test_item_turn_index_tolerates_non_finite_node_payloads(tmp_path):
    """NaN/Infinity payloads neither block the index build nor drop events (task #2017).

    ``json.dumps`` writes ``NaN``/``Infinity``; SQLite >= 3.42 parses them as
    JSON5, so the expression index builds over such rows and accepts new ones.
    """
    task_id = tasks.task_add("non-finite payloads")["id"]
    item_id = _claimed(tmp_path, task_id)
    noisy = [dict(e, data={**e.get("data", {}), "confidence": float("nan")}) for e in _turn("t")]
    node_queue.record_node_event(NODE, item_id, noisy[:-1], identity=IDENTITY)
    conn = storage.connect()
    try:
        raw = conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE channel=? AND payload LIKE '%NaN%'",
            (node_queue.CHANNEL_NODE,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert raw == 6  # the stored rows really carry Python's NaN spelling
    # Rebuild the index over the stored NaN rows, as a first startup on this code would.
    conn = storage.connect()
    try:
        supported = storage.node_turn_index_supported(conn)
        conn.execute("DROP INDEX IF EXISTS idx_event_log_node_item_turn")
        conn.commit()
    finally:
        conn.close()
    storage.init_db()
    # An append that matches the index after it exists is still recorded.
    node_queue.record_node_event(
        NODE,
        item_id,
        [dict(noisy[-1], data={**noisy[-1]["data"], "x": float("inf")})],
        identity=IDENTITY,
    )

    if not supported:
        # Older engines retain the pre-index best-effort summary limitation, but
        # every underlying ACP event, including NaN/Infinity, stays durable.
        assert len([s for s in _node_payloads("acp_event") if s["item_id"] == item_id]) == 7
        return
    [summary] = [s for s in _node_payloads("turn_summary") if s["item_id"] == item_id]
    assert summary["events"] == 7
    assert summary["tool_calls"] == 2
    assert summary["tool_calls_failed"] == 1


# --- gateway denials bound to a run (task #2309) --------------------------------


def _deny(tool: str, *, episode: str | None, arguments: dict | None = None) -> None:
    """One gateway deny, written as a registered call inside ``episode``."""
    identity = config.agent_identity_var.set(IDENTITY)
    bound = config.episode_id_var.set(episode)
    try:
        with gateway.invocation(tool) as call:
            call.registered_arguments = arguments
            gateway.record_denial(
                tool_name=tool,
                reason_code="policy_deny",
                rule="no_writes",
                caller=IDENTITY,
                profile="developer",
                episode_id=None,
            )
    finally:
        config.episode_id_var.reset(bound)
        config.agent_identity_var.reset(identity)


def test_report_lists_a_runs_denials_for_the_run_task_and_pr(tmp_path):
    pr_url = "https://github.com/example/repo/pull/2309"
    task_id = tasks.task_add("denial binding fixture")["id"]
    tasks.task_update(task_id, evidence_ref=pr_url)
    item_id = _claimed(tmp_path, task_id)
    arguments = {"path": "a.txt", "content": "do-not-leak"}
    _deny("fs_write", episode=f"acp:{item_id}", arguments=arguments)
    _deny("fs_write", episode="acp:nd-ffffffffffffffff")  # another run's deny

    for scope in ({"item": item_id}, {"task_id": task_id}, {"pr": pr_url}):
        (record,) = evidence_report.build_evidence_report(**scope)["records"]
        denials = record["denials"]
        assert denials["status"] == "recorded", scope
        assert denials["runs"] == [
            {"run": f"node:{NODE}:{item_id}", "episode_id": f"acp:{item_id}", "bound": True}
        ]
        (item,) = denials["items"]
        assert (item["tool"], item["rule"], item["reason_code"]) == (
            "fs_write",
            "no_writes",
            "policy_deny",
        )
        assert item["episode_id"] == f"acp:{item_id}"
        assert item["args_sha256"] == gateway.arguments_sha256(arguments)
        assert "do-not-leak" not in json.dumps(denials)

    # A second dispatch of the task that ran without an episode makes the
    # task's list incomplete; the bound item's own list stays complete.
    _claimed(tmp_path, task_id)
    task_denials = evidence_report.build_evidence_report(task_id=task_id)["records"][0]["denials"]
    assert task_denials["status"] == "partial"
    assert [run["bound"] for run in task_denials["runs"]] == [True, False]
    item_denials = evidence_report.build_evidence_report(item=item_id)["records"][0]["denials"]
    assert item_denials["status"] == "recorded"


def test_queued_and_unclaimed_terminal_items_are_not_runs(tmp_path):
    task_id = tasks.task_add("queued sibling")["id"]
    item_id = _claimed(tmp_path, task_id)
    _deny("fs_write", episode=f"acp:{item_id}")
    queued = node_queue.enqueue(
        NODE,
        created_by=IDENTITY,
        prompt="still waiting",
        cwd=str(tmp_path),
        backend="codex",
        task_id=task_id,
    )
    abandoned = node_queue.enqueue(
        NODE,
        created_by=IDENTITY,
        prompt="cancelled before claim",
        cwd=str(tmp_path),
        backend="codex",
        task_id=task_id,
    )
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE node_dispatch SET status='cancelled' WHERE id=?",
            (abandoned["item_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    denials = evidence_report.build_evidence_report(task_id=task_id)["records"][0]["denials"]
    assert denials["status"] == "recorded"
    assert denials["runs"] == [
        {"run": f"node:{NODE}:{item_id}", "episode_id": f"acp:{item_id}", "bound": True}
    ]
    queued_report = evidence_report.build_evidence_report(item=queued["item_id"])["records"][0]
    assert queued_report["denials"]["status"] == "not-recorded"
    assert queued_report["denials"]["runs"] == []


def test_unbound_count_excludes_other_tenants(tmp_path):
    task_id = tasks.task_add("unattributed window")["id"]
    item_id = _claimed(tmp_path, task_id)
    _deny("fs_write", episode=None)
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, originator, episode_id, "
            "created_at, org_id) VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (
                "01OTHERORGUNBOUNDDENIAL000000",
                gateway.CHANNEL_GATEWAY_DECISION,
                json.dumps({"decision": "deny", "tool_name": "fs_write"}),
                IDENTITY,
                storage.now(),
                "other-org",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    denials = evidence_report.build_evidence_report(task_id=task_id)["records"][0]["denials"]
    assert denials["unbound_in_window"] == 1
    assert denials["runs"][0]["run"] == f"node:{NODE}:{item_id}"


def test_unbound_history_is_reported_as_not_recorded_never_as_clean(tmp_path):
    task_id = tasks.task_add("pre-binding run")["id"]
    item_id = _claimed(tmp_path, task_id)
    _deny("fs_write", episode=None)  # a deny that predates the episode stamp

    for scope in ({"item": item_id}, {"task_id": task_id}):
        denials = evidence_report.build_evidence_report(**scope)["records"][0]["denials"]
        assert denials["status"] == "not-recorded", scope
        assert denials["items"] == []
        assert denials["unbound_in_window"] == 1
        assert denials["runs"] == [
            {"run": f"node:{NODE}:{item_id}", "episode_id": f"acp:{item_id}", "bound": False}
        ]

    # A task worked without any dispatch run has nothing to bind to, and an
    # executor run on a bearer token has no episode to bind with.
    bare = tasks.task_add("interactive work")["id"]
    denials = evidence_report.build_evidence_report(task_id=bare)["records"][0]["denials"]
    assert denials["status"] == "not-recorded"
    assert denials["runs"] == []
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "created_at, updated_at) VALUES ('run-1', ?, ?, 'finished', ?, ?, ?)",
            (bare, IDENTITY, IDENTITY, storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    denials = evidence_report.build_evidence_report(task_id=bare)["records"][0]["denials"]
    assert denials["status"] == "not-recorded"
    assert denials["runs"] == [{"run": "run-1", "episode_id": None, "bound": False}]


@pytest.mark.parametrize("binding", ["credential_minted", "bound_allow"])
def test_one_bound_event_does_not_make_a_run_with_an_unbound_denial_complete(tmp_path, binding):
    """Review of #2309: a bound run with an unbound deny in its window is not
    complete coverage, so an empty list must never read as zero denials."""
    task_id = tasks.task_add("mixed-context run")["id"]
    item_id = _claimed(tmp_path, task_id)
    _deny("fs_write", episode=None)  # bearer deny first
    if binding == "credential_minted":
        events.record_event(
            channel="hub.session_credential.minted",
            payload={"item_id": item_id, "task_id": task_id},
            notify=False,
        )
    else:
        events.record_event(
            channel=gateway.CHANNEL_GATEWAY_DECISION,
            payload={"decision": "allow", "tool_name": "task_list", "caller": IDENTITY},
            originator=IDENTITY,
            episode_id=f"acp:{item_id}",
            notify=False,
        )

    for scope in ({"item": item_id}, {"task_id": task_id}):
        denials = evidence_report.build_evidence_report(**scope)["records"][0]["denials"]
        assert denials["unbound_in_window"] == 1
        if binding == "credential_minted":
            assert [run["bound"] for run in denials["runs"]] == [False], scope
            assert denials["status"] == "not-recorded", scope
        else:
            assert [run["bound"] for run in denials["runs"]] == [True], scope
            assert denials["status"] == "partial", scope


def test_task_wide_unbound_count_covers_every_participating_agent(tmp_path):
    """Review of #2309: the task belongs to one agent while another executes a
    run of it; the executor's bearer deny must still count against the task."""
    owner = "Desktop_Agent2"
    task_id = tasks.task_add("two-agent task")["id"]
    item_id = _claimed(tmp_path, task_id)  # run executed by IDENTITY
    tasks.task_claim(task_id, agent=owner)
    events.record_event(
        channel=gateway.CHANNEL_GATEWAY_DECISION,
        payload={"decision": "allow", "tool_name": "task_list", "caller": IDENTITY},
        originator=IDENTITY,
        episode_id=f"acp:{item_id}",
        notify=False,
    )
    _deny("fs_write", episode=None)  # the executor's bearer deny

    record = evidence_report.build_evidence_report(task_id=task_id)["records"][0]
    assert record["agent"] == owner
    denials = record["denials"]
    assert denials["unbound_in_window"] == 1
    assert denials["status"] == "partial"
    assert set(denials["window"]["callers"]) >= {owner, IDENTITY}


# --- interactive (bearer) runs bound to an episode (task #2313) ------------------


def _live_claim(agent: str = IDENTITY) -> tuple[int, str]:
    task_id = tasks.task_add("interactive run", lane="dispatch-ready")["id"]
    claimed = tasks.task_claim(task_id, agent=agent, expected_lane="dispatch-ready")
    assert claimed["current_run_id"].startswith("sp-")
    return task_id, claimed["current_run_id"]


def _set_task(task_id: int, **cols) -> None:
    conn = storage.connect()
    try:
        sets = ", ".join(f"{col}=?" for col in cols)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*cols.values(), task_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("case", ["one", "zero", "two", "expired", "foreign_run"])
def test_a_bearer_episode_needs_exactly_one_live_claim_with_its_own_open_run(monkeypatch, case):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")
    if case == "zero":
        assert gateway.bearer_episode_id(IDENTITY) is None
        return
    task_id, run_id = _live_claim()
    expected = gateway.run_episode_id(run_id)
    if case == "two":
        _live_claim()
        expected = None
    elif case == "expired":
        _set_task(task_id, lock_expires_at="2000-01-01T00:00:00+00:00")
        expected = None
    elif case == "foreign_run":
        _set_task(task_id, current_run_agent="Desktop_Agent2")
        expected = None
    assert gateway.bearer_episode_id(IDENTITY) == expected
    assert expected is None or expected == f"run:{run_id}"


def test_the_bearer_episode_lookup_is_indexed_and_never_raises(monkeypatch):
    conn = storage.connect()
    try:
        plan = " ".join(
            str(row["detail"])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT current_run_id, current_run_agent FROM tasks "
                "WHERE status='doing' AND claimed_by=? "
                "AND (lock_expires_at IS NULL OR lock_expires_at > ?) LIMIT 2",
                (IDENTITY, storage.now()),
            ).fetchall()
        )
    finally:
        conn.close()
    assert "USING INDEX" in plan, plan

    def broken():
        raise storage.LiveStoreError("store down")

    monkeypatch.setattr(storage, "connect", broken)
    assert gateway.bearer_episode_id(IDENTITY) is None


def test_a_denial_during_a_bearer_run_is_bound_and_listed_by_the_report(monkeypatch):
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    token = f"tok-{secrets.token_urlsafe(12)}"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{IDENTITY}:{token}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    task_id, run_id = _live_claim()

    async def mcp(request):
        # What a refused tool call writes: a deny that names no episode itself.
        with gateway.invocation("fs_write") as call:
            call.registered_arguments = {"path": "a.txt"}
            gateway.record_denial(
                tool_name="fs_write",
                reason_code="policy_deny",
                rule="no_writes",
                caller=config.verified_agent_identity() or "",
                profile="developer",
                episode_id=None,
            )
        return JSONResponse({"episode": config.current_episode_id()})

    app = server.HubAuthASGIMiddleware(Starlette(routes=[Route("/mcp", mcp, methods=["POST"])]))
    with TestClient(app) as client:
        res = client.post("/mcp", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    episode = gateway.run_episode_id(run_id)
    assert res.json()["episode"] == episode

    denials = evidence_report.build_evidence_report(task_id=task_id)["records"][0]["denials"]
    assert denials["runs"] == [{"run": run_id, "episode_id": episode, "bound": True}]
    assert denials["unbound_in_window"] == 0
    assert denials["status"] == "recorded"
    (item,) = denials["items"]
    assert (item["tool"], item["episode_id"]) == ("fs_write", episode)


def test_a_self_pickup_run_with_no_bound_event_is_reported_unbound(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")
    task_id, run_id = _live_claim()
    _live_claim()  # a second claim: this agent's calls carry no episode
    _deny("fs_write", episode=None)
    denials = evidence_report.build_evidence_report(task_id=task_id)["records"][0]["denials"]
    assert denials["runs"] == [
        {"run": run_id, "episode_id": gateway.run_episode_id(run_id), "bound": False}
    ]
    assert denials["status"] == "not-recorded"
    assert denials["unbound_in_window"] == 1


def test_a_former_claimants_unbound_denial_stays_in_the_window(monkeypatch):
    """Review of #2313: A holds two claims (so its calls are unbound), is denied,
    then releases with its notes replaced; B claims. A's claim event keeps A a
    participant, so the denial is still counted and the task is not `recorded`."""
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")
    first, second = "Desktop_Agent2", IDENTITY
    task_id, run_a = _live_claim(first)
    _live_claim(first)  # a second claim: A's calls carry no episode
    identity = config.agent_identity_var.set(first)
    try:
        with gateway.invocation("fs_write"):
            gateway.record_denial(
                tool_name="fs_write",
                reason_code="policy_deny",
                rule="no_writes",
                caller=first,
                profile="developer",
                episode_id=None,
            )
    finally:
        config.agent_identity_var.reset(identity)
    # A releases; the claimant column and notes no longer name A.
    _set_task(
        task_id,
        status="open",
        claimed_by=None,
        claimed_at=None,
        lock_expires_at=None,
        current_run_id=None,
        current_run_agent=None,
        notes="rewritten",
    )
    run_b = tasks.task_claim(task_id, agent=second, expected_lane="dispatch-ready")[
        "current_run_id"
    ]
    for run_id in (run_a, run_b):  # both runs bound by some event
        events.record_event(
            channel="test.bound",
            payload={},
            episode_id=gateway.run_episode_id(run_id),
            notify=False,
        )
    denials = evidence_report.build_evidence_report(task_id=task_id)["records"][0]["denials"]
    assert first in denials["window"]["callers"]
    assert denials["unbound_in_window"] == 1
    assert denials["status"] == "partial"
