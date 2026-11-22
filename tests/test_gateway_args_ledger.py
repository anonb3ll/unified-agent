"""The gateway decision event records what a call did, not only that it ran.

Task #1907 (audit S1). Every registered call's hashed ``agent.gateway.decision``
event carries ``args_sha256``, a canonical-JSON digest of the arguments. The
mutating tools with the highest forensic value also carry ``args_summary``, a
redacted and size-capped readable digest that never holds file or note content.
A mutating call whose event cannot be written fails with a ToolError; a
read-only call stays best-effort.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import agent_access, config, events, gateway, profiles, server, storage
from unified_agent.tools import doctor, graph, messages, tasks

# Built at runtime so no credential-shaped literal sits in the source.
_SECRET = "sk-" + "Q" * 24
_SESSION = "uas_" + "A" * 43


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
    monkeypatch.setattr(agent_access, "enforce", lambda *_a, **_k: None)
    monkeypatch.setattr(profiles, "tool_is_allowed", lambda _name: True)
    gateway.reset_limits()


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


def _decisions() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY rowid",
            (gateway.CHANNEL_GATEWAY_DECISION,),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["payload"]) for row in rows]


def _canonical_sha(arguments: dict) -> str:
    text = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def failing_transactional_decision(monkeypatch):
    original = events.record_event_once_on_connection
    failing = True

    def append(conn, channel, *args, **kwargs):
        key = kwargs.get("dedupe_key") or ""
        if failing and channel == gateway.CHANNEL_GATEWAY_DECISION and ":gateway:" in key:
            conn.execute("SELECT * FROM deliberately_missing_table")
        return original(conn, channel, *args, **kwargs)

    def recover():
        nonlocal failing
        failing = False

    monkeypatch.setattr(events, "record_event_once_on_connection", append)
    monkeypatch.setattr(gateway, "_last_decision_failure", None)
    monkeypatch.setattr(gateway, "_decision_write_seen", False)
    return recover


def test_task_add_rolls_back_on_a_failed_decision_write(failing_transactional_decision):
    add = _wrapped("task_add", tasks.task_add)
    with _verified("builder-1"):
        with pytest.raises(gateway.LedgerWriteError, match="SQLITE_ERROR") as error:
            add(title="atomic task", idempotency_key="task-retry")
        assert "deliberately_missing_table" not in str(error.value)
        with contextlib.closing(storage.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
            assert storage.get_idempotency_record(conn, "task-retry", "task_add") is None
        assert doctor._decision_ledger_check()["status"] == "DEGRADED"
        assert "SQLITE_ERROR" in doctor._decision_ledger_check()["detail"]

        failing_transactional_decision()
        result = add(title="atomic task", idempotency_key="task-retry")
        with contextlib.closing(storage.connect()) as conn:
            assert (
                conn.execute("SELECT title FROM tasks WHERE id=?", (result["id"],)).fetchone()[0]
                == "atomic task"
            )
    assert [e["decision"] for e in _decisions() if e["tool_name"] == "task_add"] == [
        "error",
        "allow",
    ]


def test_task_add_preserves_rollback_error_when_error_audit_also_fails(
    failing_transactional_decision, monkeypatch
):
    def fail_record_event(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(events, "record_event", fail_record_event)
    add = _wrapped("task_add", tasks.task_add)
    with _verified("builder-1"):
        with pytest.raises(gateway.LedgerWriteError, match="SQLITE_ERROR") as error:
            add(title="atomic task double failure")
        assert error.value.rolled_back is True
        assert "Effects may have occurred" not in str(error.value)
        with contextlib.closing(storage.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_inbox_rolls_back_read_receipts_on_failed_decision_write(
    failing_transactional_decision, monkeypatch
):
    monkeypatch.setenv("UA_AGENT_LABELS", "builder-1:builder-2")
    with _verified("builder-1"):
        messages.message_send(act="inform", to_agent="builder-2", body="test message")
    inbox = _wrapped("coordination_manage", graph.coordination_manage)
    with _verified("builder-2"):
        with pytest.raises(gateway.LedgerWriteError, match="SQLITE_ERROR"):
            inbox(action="message_inbox")
        assert messages.message_list(unread_only=True)["unread"] == 1
        failing_transactional_decision()
        assert inbox(action="message_inbox")["marked_read"] == 1
        assert messages.message_list(unread_only=True)["unread"] == 0
    assert [e["decision"] for e in _decisions() if e["tool_name"] == "coordination_manage"] == [
        "error",
        "allow",
    ]


def test_run_cmd_event_records_argv_cwd_and_a_redacted_digest():
    def run_cmd(command: str, cwd: str = ".", timeout_s: int = 30):
        return {"exit_code": 0}

    with _verified("Desktop_Agent1"):
        _wrapped("run_cmd", run_cmd)(command=f"echo {_SECRET} {_SESSION}", cwd="sub")

    (event,) = _decisions()
    assert event["decision"] == "allow"
    assert event["args_sha256"] == gateway.arguments_sha256(
        {"command": f"echo {_SECRET} {_SESSION}", "cwd": "sub"}
    )
    summary = event["args_summary"]
    assert summary["argv"][0] == "echo"
    assert len(summary["argv"]) == 3
    assert summary["cwd"] == "sub"
    text = json.dumps(event)
    assert _SECRET not in text
    assert _SESSION not in text
    assert "[REDACTED_SECRET]" in text
    assert events.verify_event_chain()["status"] == "valid"


def test_fs_write_event_records_path_and_content_hash_never_content():
    content = f"line one\nprivate body {_SECRET}\n"

    def fs_write(path, content, append=False, expected_content_hash=None):
        return {"ok": True}

    with _verified("Desktop_Agent1"):
        _wrapped("fs_write", fs_write)(path="notes/a.md", content=content)

    (event,) = _decisions()
    summary = event["args_summary"]
    assert summary["path"] == "notes/a.md"
    assert summary["content_sha256"] == gateway._content_digest(content)[0]
    assert summary["content_bytes"] == len(content.encode("utf-8"))
    assert "private body" not in json.dumps(event)


def test_vault_write_and_memory_save_summaries_hash_their_content():
    def vault_write(path, content, mode="create", append=False, force=False, template=None):
        return {"ok": True}

    def memory_save(content, title=None, tags=None, id=None, author=None):
        return {"id": "n1"}

    with _verified("Desktop_Agent1"):
        _wrapped("vault_write", vault_write)(path="Logs/x.md", content="vault body", mode="append")
        _wrapped("memory_save", memory_save)(content="memo body", title="T", id="n1")

    vault_event, memory_event = _decisions()
    assert vault_event["args_summary"] == {
        "path": "Logs/x.md",
        "mode": "append",
        "content_sha256": hashlib.sha256(b"vault body").hexdigest(),
        "content_bytes": len(b"vault body"),
    }
    assert memory_event["args_summary"]["id"] == "n1"
    assert memory_event["args_summary"]["title"] == "T"
    assert (
        memory_event["args_summary"]["content_sha256"] == hashlib.sha256(b"memo body").hexdigest()
    )
    for event in (vault_event, memory_event):
        assert "body" not in json.dumps(event)


def test_summary_is_written_only_for_mutating_calls():
    # Read-only polling dominates the channel, so a curated tool recorded as a
    # read-only call keeps the row small: digest only, no summary.
    arguments = {"path": "a.md", "content": "x"}
    with _verified("Desktop_Agent1"):
        with gateway.invocation("fs_write") as call:
            gateway.record_call_result("fs_write", call, None, arguments=arguments)
        with gateway.invocation("fs_write") as call:
            gateway.record_call_result("fs_write", call, None, arguments=arguments, mutating=True)

    read_only, mutating = _decisions()
    assert read_only["args_sha256"] == _canonical_sha(arguments)
    assert "args_summary" not in read_only
    assert mutating["args_summary"]["path"] == "a.md"


def test_read_only_tool_gets_a_digest_but_no_summary(monkeypatch):
    def task_list(status=None, limit=20):
        return []

    # Per-call shape; with coalescing on (the default) the digest lands in the
    # hourly rollup instead (task #1922, tests/test_gateway_coalesce.py).
    monkeypatch.setenv("UA_GATEWAY_COALESCE_READS", "0")
    with _verified("Desktop_Agent1"):
        _wrapped("task_list", task_list)(status="todo")

    (event,) = _decisions()
    assert event["args_sha256"] == _canonical_sha({"status": "todo"})
    assert "args_summary" not in event


def test_read_only_tool_digest_reaches_the_rollup():
    def task_list(status=None, limit=20):
        return []

    with _verified("Desktop_Agent1"):
        _wrapped("task_list", task_list)(status="todo")
    assert _decisions() == []
    gateway.flush_coalesced()
    events_ = events.query_events(channels=[gateway.CHANNEL_GATEWAY_DECISION_ROLLUP])
    (rollup,) = [row["payload"] for row in events_]
    assert rollup["args_sha256"] == [_canonical_sha({"status": "todo"})]
    assert "args_summary" not in rollup


def test_summary_is_size_capped():
    def run_cmd(command: str, cwd: str = ".", timeout_s: int = 30):
        return {"exit_code": 0}

    huge = "echo " + " ".join(f"word{i}" * 50 for i in range(500))
    with _verified("Desktop_Agent1"):
        _wrapped("run_cmd", run_cmd)(command=huge)

    (event,) = _decisions()
    assert len(json.dumps(event["args_summary"])) <= gateway.ARGS_SUMMARY_MAX_BYTES
    assert event["args_summary"].get("truncated") is True


def test_mutating_call_fails_closed_when_the_ledger_write_fails(monkeypatch):
    ran = []

    def fs_write(path, content, append=False, expected_content_hash=None):
        ran.append(path)
        return {"ok": True}

    def broken(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(events, "record_event", broken)
    with _verified("Desktop_Agent1"):
        with pytest.raises(ToolError, match="evidence ledger"):
            _wrapped("fs_write", fs_write)(path="a.txt", content="x")
    assert ran == ["a.txt"]


def test_mutating_error_path_also_fails_closed(monkeypatch):
    def run_cmd(command: str, cwd: str = ".", timeout_s: int = 30):
        raise ValueError("tool broke")

    def broken(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(events, "record_event", broken)
    with _verified("Desktop_Agent1"):
        with pytest.raises(ToolError, match="evidence ledger"):
            _wrapped("run_cmd", run_cmd)(command="ls")


def test_read_only_call_stays_best_effort_when_the_ledger_write_fails(monkeypatch):
    def task_list(status=None, limit=20):
        return ["row"]

    def broken(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(events, "record_event", broken)
    with _verified("Desktop_Agent1"):
        assert _wrapped("task_list", task_list)() == ["row"]


def test_weak_secrets_do_not_change_digests():
    assert gateway.arguments_sha256({"token": "one"}) == gateway.arguments_sha256({"token": "two"})
    assert gateway.arguments_sha256({"command": "echo Bearer one"}) == gateway.arguments_sha256(
        {"command": "echo Bearer two"}
    )
    assert gateway._content_digest("password=one")[0] == gateway._content_digest("password=two")[0]


def test_split_bearer_summary_is_redacted_and_truncation_is_explicit():
    summary = gateway.arguments_summary("run_cmd", {"command": "echo Bearer weakvalue"})
    assert "weakvalue" not in json.dumps(summary)
    summary = gateway.arguments_summary("run_cmd", {"command": "echo " + "x " * 33})
    assert summary["truncated"] is True


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("delegation_manage", {"action": "list"}),
        ("security_audit", {"remediate": False}),
        ("export_data", {"action": "tasks", "params": {}}),
        ("vault_ingest_status", {"mark": False}),
        ("vault_wiki_custodian", {"dry_run": True}),
    ],
)
def test_mixed_read_modes_survive_ledger_outage(name, arguments, monkeypatch):
    import inspect

    def fn(**kwargs):
        return {"ok": True}

    fn.__signature__ = inspect.Signature(
        [
            inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, default=value)
            for key, value in arguments.items()
        ]
    )
    monkeypatch.setattr(
        events, "record_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked"))
    )
    with _verified("Desktop_Agent2"):
        assert _wrapped(name, fn)(**arguments) == {"ok": True}


@pytest.mark.parametrize("quote", ["'", '"'])
def test_quoted_weak_bearer_credentials_are_redacted(quote):
    command = f"echo Bearer {quote}weak secret{quote}"
    summary = gateway.arguments_summary("run_cmd", {"command": command})
    assert "weak secret" not in json.dumps(summary)
    assert gateway.arguments_sha256({"command": command}) == gateway.arguments_sha256(
        {"command": f"echo Bearer {quote}other secret{quote}"}
    )


@pytest.mark.parametrize(
    "context",
    ["'Bearer'", '"Bearer"', "'Bea'rer", r"Bear\er", "'Authorization: Bearer'"],
)
@pytest.mark.parametrize("value", ["marker_one", "'marker one'"])
def test_normalized_bearer_context_is_redacted_before_persistence(context, value):
    # Pure ledger fixtures: no subprocess or actual command tool executes.
    arguments = {"command": f"inert {context} {value}"}
    with gateway.invocation("run_cmd") as call:
        gateway.record_call_result("run_cmd", call, None, arguments=arguments, mutating=True)
    (event,) = _decisions()
    assert "marker" not in json.dumps(event)
    assert event["args_summary"]["argv"][-1] == "[REDACTED_SECRET]"
    assert event["args_sha256"] == gateway.arguments_sha256(
        {"command": f"inert {context} marker_two"}
    )


def test_normalized_assignment_context_is_redacted():
    first = {"command": "inert 'pass'word=marker_one"}
    second = {"command": "inert 'pass'word=marker_two"}
    assert "marker" not in json.dumps(gateway.arguments_summary("run_cmd", first))
    assert gateway.arguments_sha256(first) == gateway.arguments_sha256(second)


@pytest.mark.parametrize(
    "assignment",
    [
        "'password' '=' {value}",
        "'token' ':' {value}",
        "'pass'word = {value}",
        "'pass'word= {value}",
        "'api_key' '={value}'",
        r"pass\word ':{value}'",
        "'secret : ' {value}",
        "' password ' '=' {value}",
    ],
)
def test_normalized_cross_argv_assignments_are_redacted_before_persistence(assignment):
    first = {"command": "inert " + assignment.format(value="marker_one")}
    second = {"command": "inert " + assignment.format(value="marker_two")}
    with gateway.invocation("run_cmd") as call:
        gateway.record_call_result("run_cmd", call, None, arguments=first, mutating=True)
    (event,) = _decisions()
    assert "marker" not in json.dumps(event)
    assert event["args_summary"]["argv"][-1] == "[REDACTED_SECRET]"
    assert event["args_sha256"] == gateway.arguments_sha256(second)


@pytest.mark.parametrize("prefix", ["", "--", "cfg.", "auth/"])
@pytest.mark.parametrize("delimiter", ["=", ":"])
@pytest.mark.parametrize(
    "layout",
    ["'{key}' '{delimiter}' {value}", "'{key}{delimiter}' {value}", "'{key}' '{delimiter}{value}'"],
)
def test_normalized_assignment_boundaries_match_the_raw_redactor(prefix, delimiter, layout):
    # A bounded matrix checks the raw redactor's full key vocabulary across
    # non-word prefixes and every supported delimiter split, without execution.
    for key in ("password", "passwd", "token", "secret", "api_key", "api-key", "ApiKey"):
        command = "inert " + layout.format(
            key=prefix + key, delimiter=delimiter, value="marker_one"
        )
        other = "inert " + layout.format(key=prefix + key, delimiter=delimiter, value="marker_two")
        summary = gateway.arguments_summary("run_cmd", {"command": command})
        assert "marker" not in json.dumps(summary), command
        assert gateway.arguments_sha256({"command": command}) == gateway.arguments_sha256(
            {"command": other}
        ), command


@pytest.mark.parametrize("key", ["mypassword", "cfg._password", "password_hint"])
def test_normalized_assignment_preserves_noncredential_word_boundaries(key):
    first = {"command": f"inert '{key}' '=' marker_one"}
    second = {"command": f"inert '{key}' '=' marker_two"}
    assert "marker_one" in json.dumps(gateway.arguments_summary("run_cmd", first))
    assert gateway.arguments_sha256(first) != gateway.arguments_sha256(second)


def test_unparseable_command_has_no_credential_fallback():
    first = {"command": "inert 'Bearer' marker_one '"}
    second = {"command": "inert 'Bearer' marker_two '"}
    assert "marker" not in json.dumps(gateway.arguments_summary("run_cmd", first))
    assert gateway.arguments_sha256(first) == gateway.arguments_sha256(second)


def test_declared_wait_keeps_until_readable_and_hashes_only_the_reason():
    until = "2026-10-20T00:00:00+00:00"
    reason = "waiting on the vendor's reply"
    summary = gateway.arguments_summary(
        "task_update",
        {"id": 7, "monitor_at": until, "declared_wait": {"reason": reason, "until": until}},
    )
    assert summary == {
        "id": 7,
        "monitor_at": until,
        "declared_wait.until": until,
        "declared_wait.reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
        "declared_wait.reason_bytes": len(reason.encode()),
    }
    assert "vendor" not in json.dumps(summary)
    # A clear is visible as an empty until; a malformed value records nothing.
    cleared = gateway.arguments_summary("task_update", {"id": 7, "declared_wait": {}})
    assert cleared == {"id": 7, "declared_wait.until": None}
    assert gateway.arguments_summary("task_update", {"id": 7, "declared_wait": ["x"]}) == {"id": 7}


def _decision_rows() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload, episode_id FROM event_log WHERE channel=? ORDER BY rowid",
            (gateway.CHANNEL_GATEWAY_DECISION,),
        ).fetchall()
    finally:
        conn.close()
    return [{"payload": json.loads(r["payload"]), "episode_id": r["episode_id"]} for r in rows]


def test_a_denial_during_a_run_is_stamped_with_its_episode_and_a_redacted_digest(tmp_path):
    """Task #2309: the deny row's own episode_id column binds it to the run."""
    (tmp_path / "policy.yaml").write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
        "  - name: no_writes\n    type: tool\n    tool: fs_write\n    action: deny\n",
        encoding="utf-8",
    )

    def fs_write(path: str, content: str):
        raise AssertionError("a denied call must not run")

    token = config.episode_id_var.set("acp:nd-0123456789abcdef")
    try:
        with _verified("developer-agent"), pytest.raises(ToolError):
            _wrapped("fs_write", fs_write)(path="a.txt", content=_SECRET)
    finally:
        config.episode_id_var.reset(token)

    (row,) = _decision_rows()
    assert row["episode_id"] == "acp:nd-0123456789abcdef"
    payload = row["payload"]
    assert (payload["decision"], payload["reason_code"], payload["rule"]) == (
        "deny",
        "policy_deny",
        "no_writes",
    )
    assert payload["args_sha256"] == gateway.arguments_sha256({"path": "a.txt", "content": _SECRET})
    assert _SECRET not in json.dumps(payload)
    assert "args_summary" not in payload


def test_ownership_keeps_the_owner_readable_and_hashes_the_free_text():
    waiting_on = "the secret plan"
    summary = gateway.arguments_summary(
        "task_update",
        {
            "id": 7,
            "next_owner": "Desktop_Agent2",
            "waiting_on": waiting_on,
            "terminal_predicate": "x",
        },
    )
    assert summary["next_owner"] == "Desktop_Agent2"
    assert "waiting_on" not in summary
    assert summary["waiting_on_sha256"] == hashlib.sha256(waiting_on.encode()).hexdigest()
    assert "terminal_predicate" not in summary and "terminal_predicate_sha256" in summary
