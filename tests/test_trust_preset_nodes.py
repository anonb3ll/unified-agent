"""Hub policy must constrain queued native work even on a policyless node."""

import json

import pytest

from unified_agent import node_queue, storage


@pytest.fixture(autouse=True)
def queue(isolated_env):
    storage.init_db()
    node_queue.register("preset-node", identity="Desktop_Agent2", facts={"backends": ["codex"]})


def bind(tmp_path, monkeypatch, agent="Desktop_Agent2", invalid=False):
    path = tmp_path / "policy.yaml"
    path.write_text(
        json.dumps({"trust_presets": {"agents": {agent: "invalid" if invalid else "inspection"}}})
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(path))


def enqueue():
    return node_queue.enqueue(
        "preset-node",
        created_by="Desktop_Agent5",
        prompt="inspect",
        cwd="/tmp/worktree",
        backend="codex",
    )


@pytest.mark.parametrize("agent", ["Desktop_Agent2", "Desktop_Agent5"])
def test_hub_refuses_bound_node_or_originator_before_enqueue(tmp_path, monkeypatch, agent):
    bind(tmp_path, monkeypatch, agent)
    with pytest.raises(node_queue.NodeError):
        enqueue()
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM node_dispatch").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("invalid", [False, True])
def test_queued_work_never_reaches_node_after_hub_restriction(tmp_path, monkeypatch, invalid):
    queued = enqueue()
    bind(tmp_path, monkeypatch, invalid=invalid)
    assert node_queue.claim_next("preset-node", identity="Desktop_Agent2") is None
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT status FROM node_dispatch WHERE id=?", (queued["item_id"],)
            ).fetchone()[0]
            == "failed"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("invalid", [False, True])
def test_hub_heartbeat_relays_stop_after_restriction(tmp_path, monkeypatch, invalid):
    queued = enqueue()
    item = node_queue.claim_next("preset-node", identity="Desktop_Agent2")
    assert item is not None
    bind(tmp_path, monkeypatch, invalid=invalid)
    report = node_queue.heartbeat(
        "preset-node", identity="Desktop_Agent2", running=queued["item_id"]
    )
    assert report["cancel"] == [queued["item_id"]]


def test_unbound_node_remains_available(tmp_path, monkeypatch):
    bind(tmp_path, monkeypatch, agent="Desktop_Agent1")
    enqueue()
    assert node_queue.claim_next("preset-node", identity="Desktop_Agent2") is not None


@pytest.mark.parametrize("bound", ["Desktop_Agent1", "Desktop_Agent5", "Desktop_agent3"])
def test_linked_run_uses_dispatch_target_and_stored_originator(tmp_path, monkeypatch, bound):
    from unified_agent.tools import delegate

    packed = delegate.delegate(
        task_spec="Inspect", from_agent="Desktop_Agent5", to_agent="Desktop_Agent1", callback="none"
    )
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id,task_id,delegation_id,agent_label,state,actor,created_at,updated_at) VALUES (?,?,?,?,'launched',?,?,?)",
            (
                "run-node-preset",
                packed["task_id"],
                packed["delegation_id"],
                "Desktop_Agent1",
                "dispatch-scheduler",
                storage.now(),
                storage.now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    queued = node_queue.enqueue(
        "preset-node",
        created_by="Desktop_agent3",
        prompt="inspect",
        cwd="/tmp/worktree",
        backend="codex",
        run_id="run-node-preset",
        task_id=packed["task_id"],
    )
    bind(tmp_path, monkeypatch, agent=bound)
    assert node_queue.claim_next("preset-node", identity="Desktop_Agent2") is None
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT status FROM node_dispatch WHERE id=?", (queued["item_id"],)
            ).fetchone()[0]
            == "failed"
        )
    finally:
        conn.close()
