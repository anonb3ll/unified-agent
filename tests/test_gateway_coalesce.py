"""Read-only allow decisions coalesce into hourly rollups (task #1922).

On the hub, ``agent.gateway.decision`` held 557k rows in 7 days, more than 90%
of them read-only ALLOW decisions from executor polls and the web UI. A
read-only allow now adds to an in-memory count per (caller, tool, profile,
decision, bucket) and is written as one hashed
``agent.gateway.decision.rollup`` event when the bucket rolls over, the key
table fills, or the hub shuts down. Denies, errors, parked approvals and every
mutating call stay one event per call. ``args_sha256`` is computed over the
redacted arguments, so a digest cannot confirm a guessed secret.
"""

from __future__ import annotations

import contextlib
import hashlib
import json

import pytest

from unified_agent import agent_access, config, events, gateway, profiles, server, storage

_SECRET = "sk-" + "Q" * 24
_OTHER_SECRET = "sk-" + "Z" * 24

HOUR = 3600.0
T0 = 1_800_000_000.0 - (1_800_000_000.0 % HOUR)  # an hour boundary


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules: []\n", encoding="utf-8"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_path))
    monkeypatch.delenv("UA_GATEWAY_COALESCE_READS", raising=False)
    monkeypatch.delenv("UA_GATEWAY_COALESCE_BUCKET_S", raising=False)
    monkeypatch.setattr(agent_access, "enforce", lambda *_a, **_k: None)
    monkeypatch.setattr(profiles, "tool_is_allowed", lambda _name: True)
    gateway.reset_limits()
    gateway.reset_coalesced()
    clock = {"now": T0 + 10}
    monkeypatch.setattr(gateway, "_coalesce_clock", lambda: clock["now"])
    yield {"clock": clock, "policy": policy_path}
    gateway.reset_coalesced()


@contextlib.contextmanager
def _verified(agent: str):
    token = config.agent_identity_var.set(agent)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _wrapped(name: str, fn):
    fn.__name__ = name
    return server._guarded_and_instrumented(fn)


def _rows(channel: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY rowid", (channel,)
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["payload"]) for row in rows]


def _per_call() -> list[dict]:
    return _rows(gateway.CHANNEL_GATEWAY_DECISION)


def _rollups() -> list[dict]:
    return _rows(gateway.CHANNEL_GATEWAY_DECISION_ROLLUP)


def _task_list():
    def task_list(status=None, limit=20):
        return []

    return _wrapped("task_list", task_list)


def test_read_only_allows_roll_up_with_counts_across_a_bucket_boundary(_isolated):
    clock = _isolated["clock"]
    task_list = _task_list()
    with _verified("Desktop_Person"):
        task_list(status="todo")
        clock["now"] = T0 + 20
        task_list(status="todo")
        clock["now"] = T0 + 30
        task_list(status="doing")
        assert _per_call() == []
        assert _rollups() == []

        clock["now"] = T0 + HOUR + 5  # the next bucket: the first one is written
        task_list()
        (first,) = _rollups()
        clock["now"] = T0 + HOUR + 9
        task_list()

    assert first["decision"] == "allow"
    assert first["reason_code"] == "allowed"
    assert first["tool_name"] == "task_list"
    assert first["caller"] == "Desktop_Person"
    assert first["count"] == 3
    assert first["bucket_s"] == 3600
    assert first["bucket"] == "2027-01-15T08:00:00+00:00"
    assert first["first_at"] == "2027-01-15T08:00:10+00:00"
    assert first["last_at"] == "2027-01-15T08:00:30+00:00"
    assert first["distinct_args"] == 2
    assert len(first["args_sha256"]) == 2
    assert "tool" not in first  # episode counters match $.tool; a rollup is not a call

    gateway.flush_coalesced()
    first_again, second = _rollups()
    assert first_again == first
    assert second["count"] == 2
    assert second["distinct_args"] == 1
    assert _per_call() == []
    assert events.verify_event_chain()["status"] == "valid"


def test_keys_split_by_caller_and_tool():
    def approval_pending():
        return []

    pending = _wrapped("approval_pending", approval_pending)
    task_list = _task_list()
    with _verified("Desktop_Person"):
        task_list()
        task_list()
        pending()
    with _verified("server_agent2"):
        task_list()
    gateway.flush_coalesced()
    counts = {(r["caller"], r["tool_name"]): r["count"] for r in _rollups()}
    assert counts == {
        ("Desktop_Person", "task_list"): 2,
        ("Desktop_Person", "approval_pending"): 1,
        ("server_agent2", "task_list"): 1,
    }


def test_mutating_calls_stay_one_event_per_call():
    def fs_write(path, content, append=False, expected_content_hash=None):
        return {"ok": True}

    write = _wrapped("fs_write", fs_write)
    with _verified("Desktop_Agent1"):
        write(path="a.md", content="x")
        write(path="a.md", content="y")
    assert [e["decision"] for e in _per_call()] == ["allow", "allow"]
    assert all("args_summary" in e for e in _per_call())
    gateway.flush_coalesced()
    assert _rollups() == []


def test_denies_and_errors_on_read_only_tools_stay_per_call(_isolated):
    _isolated["policy"].write_text(
        "rules:\n  - name: list_cap\n    type: rate_limit\n    tool: task_list\n"
        "    max_calls: 1\n    window_seconds: 60\n",
        encoding="utf-8",
    )

    def fs_read(path):
        raise ValueError("no such file")

    task_list = _task_list()
    read = _wrapped("fs_read", fs_read)
    with _verified("Desktop_Person"):
        task_list()
        with pytest.raises(Exception, match="rate limited"):
            task_list()
        with pytest.raises(ValueError):
            read(path="missing.md")
    decisions = [(e["tool_name"], e["decision"]) for e in _per_call()]
    assert decisions == [("task_list", "deny"), ("fs_read", "error")]
    gateway.flush_coalesced()
    (rollup,) = _rollups()
    assert (rollup["tool_name"], rollup["count"]) == ("task_list", 1)


def test_delegation_poll_actions_roll_up_but_its_writes_do_not():
    def delegation_manage(action, params=None):
        return {"delegations": []}

    manage = _wrapped("delegation_manage", delegation_manage)
    with _verified("server_agent3"):
        manage(action="list", params={"status": "pending", "to_agent": "server_agent3"})
        manage(action="get", params={"id": 1})
        manage(action="release", params={"id": 1})
    assert [e["tool_name"] for e in _per_call()] == ["delegation_manage"]
    gateway.flush_coalesced()
    (rollup,) = _rollups()
    assert rollup["count"] == 2


def test_episode_attributed_calls_stay_per_call():
    task_list = _task_list()
    token = config.episode_id_var.set("acp:item-1")
    try:
        with _verified("Desktop_Agent1"):
            task_list()
    finally:
        config.episode_id_var.reset(token)
    assert len(_per_call()) == 1


def test_a_bearer_runs_reads_coalesce_into_a_rollup_bound_to_the_run():
    """Task #2313: a run episode keeps read allows coalesced; the rollup carries it."""
    task_list = _task_list()
    token = config.episode_id_var.set(gateway.run_episode_id("sp-RUN1"))
    try:
        with _verified("Desktop_Agent1"):
            task_list()
            task_list()
    finally:
        config.episode_id_var.reset(token)
    with _verified("Desktop_Agent1"):
        task_list()  # outside the run: its own rollup, unbound
    assert _per_call() == []
    # A drain inside another run's request must not stamp that run on these.
    other = config.episode_id_var.set(gateway.run_episode_id("sp-OTHER"))
    try:
        gateway.flush_coalesced()
    finally:
        config.episode_id_var.reset(other)
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT episode_id, payload FROM event_log WHERE channel=? ORDER BY rowid",
            (gateway.CHANNEL_GATEWAY_DECISION_ROLLUP,),
        ).fetchall()
    finally:
        conn.close()
    assert sorted((r["episode_id"] or "", json.loads(r["payload"])["count"]) for r in rows) == [
        ("", 1),
        ("run:sp-RUN1", 2),
    ]


@pytest.mark.parametrize("flush_between", [False, True])
def test_an_episode_cap_counts_a_bearer_runs_coalesced_reads(_isolated, monkeypatch, flush_between):
    """Review of #2313: cap=1 lets the run's first read through and denies the
    second, whether the first is still coalesced in memory or already rolled up."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import run_records
    from unified_agent.tools import tasks

    assert (gateway.SELF_PICKUP_CHANNEL, gateway.SELF_PICKUP_CLAIMED) == (
        run_records.CHANNEL,
        run_records.KIND_CLAIMED,
    )
    _isolated["policy"].write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
        "  - name: one_list_per_run\n    tool: task_list\n"
        "    type: max_calls_per_episode\n    max_calls: 1\n    require_episode: true\n",
        encoding="utf-8",
    )
    task_id = tasks.task_add("capped run", lane="dispatch-ready")["id"]
    run_id = tasks.task_claim(task_id, agent="Desktop_Agent1", expected_lane="dispatch-ready")[
        "current_run_id"
    ]
    task_list = _task_list()
    token = config.episode_id_var.set(gateway.run_episode_id(run_id))
    try:
        with _verified("Desktop_Agent1"):
            assert task_list() == []  # a fresh run's first call is not "episode not found"
            if flush_between:
                gateway.flush_coalesced()
            with pytest.raises(ToolError, match="maximum limit of 1"):
                task_list()
    finally:
        config.episode_id_var.reset(token)
    # An unknown run episode is still refused as not existing.
    token = config.episode_id_var.set(gateway.run_episode_id("sp-NOSUCHRUN"))
    try:
        with _verified("Desktop_Agent1"), pytest.raises(ToolError, match="not found"):
            task_list()
    finally:
        config.episode_id_var.reset(token)


def test_coalescing_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("UA_GATEWAY_COALESCE_READS", "0")
    task_list = _task_list()
    with _verified("Desktop_Person"):
        task_list()
        task_list()
    assert len(_per_call()) == 2
    assert _rollups() == []


def test_bucket_width_is_configurable(_isolated, monkeypatch):
    monkeypatch.setenv("UA_GATEWAY_COALESCE_BUCKET_S", "60")
    clock = _isolated["clock"]
    task_list = _task_list()
    with _verified("Desktop_Person"):
        task_list()
        clock["now"] = T0 + 75
        task_list()
    (rollup,) = _rollups()
    assert rollup["bucket_s"] == 60
    assert rollup["count"] == 1


def test_bucket_width_config_rejects_nonsense(monkeypatch):
    for raw in ("abc", "-5", "0", "10", "999999"):
        monkeypatch.setenv("UA_GATEWAY_COALESCE_BUCKET_S", raw)
        assert config.gateway_coalesce_bucket_s() == 3600
    monkeypatch.setenv("UA_GATEWAY_COALESCE_BUCKET_S", "900")
    assert config.gateway_coalesce_bucket_s() == 900


def test_size_threshold_flushes_early(monkeypatch):
    monkeypatch.setattr(gateway, "COALESCE_MAX_KEYS", 2)
    with _verified("Desktop_Person"):
        for name in ("task_list", "approval_pending"):

            def tool():
                return []

            _wrapped(name, tool)()
    assert sorted(r["tool_name"] for r in _rollups()) == ["approval_pending", "task_list"]


def test_distinct_argument_digests_are_capped(monkeypatch):
    monkeypatch.setattr(gateway, "ROLLUP_ARGS_SAMPLE_MAX", 3)
    monkeypatch.setattr(gateway, "ROLLUP_DISTINCT_MAX", 5)
    task_list = _task_list()
    with _verified("Desktop_Person"):
        for limit in range(8):
            task_list(limit=limit)
    gateway.flush_coalesced()
    (rollup,) = _rollups()
    assert rollup["count"] == 8
    assert rollup["distinct_args"] == 5
    assert rollup["distinct_args_capped"] is True
    assert len(rollup["args_sha256"]) == 3


def test_hub_lifespan_flushes_pending_counts_on_shutdown(monkeypatch):
    from unified_agent import mcp_live_state

    async def idle(_server):
        return None

    monkeypatch.setattr(mcp_live_state, "poll_loop", idle)
    task_list = _task_list()

    async def run():
        async with server._hub_lifespan(server.mcp):
            with _verified("Desktop_Person"):
                task_list()
                task_list()
            assert _rollups() == []

    import anyio

    anyio.run(run)
    (rollup,) = _rollups()
    assert rollup["count"] == 2


def test_flush_is_registered_at_exit():
    # A stdio server or CLI process has no lifespan teardown; atexit covers it.
    assert gateway._EXIT_FLUSH_REGISTERED is True


def test_rollup_write_failure_is_logged_not_raised(monkeypatch):
    task_list = _task_list()
    with _verified("Desktop_Person"):
        task_list()

    def broken(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(events, "record_event", broken)
    gateway.flush_coalesced()  # best-effort, like a read-only per-call write


def test_call_counts_sum_rollups_and_per_call_rows():
    def fs_write(path, content, append=False, expected_content_hash=None):
        return {"ok": True}

    task_list = _task_list()
    with _verified("Desktop_Person"):
        for _ in range(4):
            task_list()
        _wrapped("fs_write", fs_write)(path="a.md", content="x")
    gateway.flush_coalesced()
    rows = events.query_events(channels=[f"{gateway.CHANNEL_GATEWAY_DECISION}*"], limit=100)
    assert len(rows) == 2
    counts = gateway.decision_call_counts(rows)
    assert counts[("Desktop_Person", "task_list", "allow")] == 4
    assert counts[("Desktop_Person", "fs_write", "allow")] == 1
    assert sum(counts.values()) == 5


# --- args_sha256 is computed over redacted arguments (#1907 review) ---------------------


def _canonical_sha(arguments: dict) -> str:
    text = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_args_digest_cannot_confirm_a_guessed_secret():
    raw = {"command": f"curl -H 'x-api-key: {_SECRET}' https://example.test"}
    other = {"command": f"curl -H 'x-api-key: {_OTHER_SECRET}' https://example.test"}
    digest = gateway.arguments_sha256(raw)
    assert digest != _canonical_sha(raw)
    assert digest == gateway.arguments_sha256(other)


def test_args_digest_redacts_values_under_sensitive_keys():
    assert gateway.arguments_sha256({"token": "hunter2"}) == gateway.arguments_sha256(
        {"token": "letmein"}
    )
    assert gateway.arguments_sha256({"api_key": "a"}) == _canonical_sha(
        {"api_key": "[REDACTED_SECRET]"}
    )


def test_args_digest_still_distinguishes_ordinary_arguments():
    long_a = "x" * 5000 + "a"
    long_b = "x" * 5000 + "b"
    assert gateway.arguments_sha256({"path": long_a}) != gateway.arguments_sha256({"path": long_b})
    assert gateway.arguments_sha256({"status": "todo"}) == _canonical_sha({"status": "todo"})


def test_mutating_event_digest_is_over_redacted_arguments():
    def run_cmd(command: str, cwd: str = ".", timeout_s: int = 30):
        return {"exit_code": 0}

    with _verified("Desktop_Agent1"):
        _wrapped("run_cmd", run_cmd)(command=f"echo {_SECRET}")
    (event,) = _per_call()
    assert event["args_sha256"] == _canonical_sha({"command": "echo [REDACTED_SECRET]"})
    assert event["args_sha256"] != _canonical_sha({"command": f"echo {_SECRET}"})


def test_full_long_arguments_contribute_to_digest():
    prefix = "x" * 1_000_001
    assert gateway.arguments_sha256({"content": prefix + "a"}) != gateway.arguments_sha256(
        {"content": prefix + "b"}
    )


def test_off_spelling_disables_coalescing(monkeypatch):
    monkeypatch.setenv("UA_GATEWAY_COALESCE_READS", "off")
    assert config.gateway_coalesce_reads() is False


def test_idle_bucket_deadline_flushes_and_stale_timer_cannot_drain_successor(monkeypatch):
    timers = []

    class Timer:
        def __init__(self, delay, callback, args=()):
            self.delay, self.callback, self.args = delay, callback, args
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(gateway.threading, "Timer", Timer)
    key = ("Desktop_Agent2", "task_list", "developer", "allow", None)
    coalescer = gateway._ReadCoalescer()
    coalescer.add(key, "digest", T0 + 10, 3600)
    first = timers[-1]
    assert first.delay == 3590
    first.callback(*first.args)
    assert coalescer.pending() == 0
    assert _rollups()[0]["count"] == 1
    coalescer.add(key, "digest", T0 + HOUR + 10, 3600)
    first.callback(*first.args)
    assert coalescer.pending() == 1
    timers[-1].callback(*timers[-1].args)
    assert coalescer.pending() == 0
    assert len(_rollups()) == 2
