"""Executor closeouts must transform the current task row, not a claim snapshot."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_executor_scout_completion import _workspace_repo

from unified_agent import config, delivery, storage
from unified_agent.delivery import DeliveryPolicy, annotate_notes
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.scout import apply_scout_finish, report_path_from_text
from unified_agent.tools import delegate as delegations
from unified_agent.tools import tasks


@pytest.fixture(autouse=True)
def executor_env(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(isolated_env / "empty-sessions"))
    monkeypatch.setenv("UA_PROJECT_DELIVERY_FILE", str(isolated_env / "delivery.yaml"))
    storage.init_db()
    _workspace_repo(isolated_env)


def _notes(task_id):
    with storage.connect() as conn:
        return conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()[0]


@pytest.mark.parametrize(
    "outcome", ["success", "scout", "killed", "ceiling", "scout-missing", "zero"]
)
def test_backend_barrier_preserves_concurrent_notes(isolated_env, monkeypatch, outcome):
    entered, edited = Event(), Event()
    created = delegations.delegate(
        "[task-shape: scout] Investigate" if outcome.startswith("scout") else "Implement output",
        "Desktop_Agent1",
        "Desktop_Agent2",
        callback="none",
    )
    task_id = created["task_id"]

    class Backend:
        name = "synthetic"

        def run(self, **kwargs):
            entered.set()
            assert edited.wait(10), "concurrent note writer did not finish"
            text = "finished"
            if outcome == "scout":
                text += f"\n[scout: report={report_path_from_text(kwargs['prompt'])}]"
            if outcome == "success":
                (isolated_env / kwargs["cwd"] / "child.txt").write_text("output")
            return {
                "status": {"killed": "killed", "ceiling": "error"}.get(outcome, "success"),
                "text": text,
            }

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False, backend=Backend())
    daemon.max_attempts = 1

    def concurrent_edit():
        assert entered.wait(10), "backend did not reach barrier"
        token = config.agent_identity_var.set("Desktop_Agent2")
        try:
            tasks.task_update(
                task_id, append_note="Reviewer: preserve this explanation.", worktree=False
            )
            if outcome == "scout":
                tasks.task_update(
                    task_id,
                    append_note="Old report [scout: report=obsolete.md] kept explanation",
                    worktree=False,
                )
        finally:
            config.agent_identity_var.reset(token)
            edited.set()

    with ThreadPoolExecutor(max_workers=1) as writers:
        writer = writers.submit(concurrent_edit)
        result = daemon.poll_once()
        writer.result(timeout=10)
    notes = _notes(task_id)
    assert "Reviewer: preserve this explanation." in notes
    if outcome in ("success", "scout"):
        assert result["waiting_for_review"]
        assert "[waiting-for: review]" in notes
        if outcome == "scout":
            assert "obsolete.md" not in notes
            assert "Old report" in notes and "kept explanation" in notes
    else:
        assert result["failed_terminal"]
        assert "[failed:" in notes


@pytest.mark.parametrize("override", [False, True])
def test_finish_resolves_live_project_and_delivery_notes(isolated_env, monkeypatch, override):
    (isolated_env / "delivery.yaml").write_text(
        "projects:\n  browser:\n    mode: local-only\n    yolo: false\n"
    )
    created = delegations.delegate(
        "Implement output", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    task_id = created["task_id"]

    class Backend:
        name = "synthetic"

        def run(self, **kwargs):
            token = config.agent_identity_var.set("Desktop_Agent2")
            try:
                tasks.task_update(task_id, project="browser", worktree=False)
                if override:
                    tasks.task_update(
                        task_id,
                        append_note="Current choice [delivery: mode=direct-pr yolo=off] keep prose",
                        worktree=False,
                    )
            finally:
                config.agent_identity_var.reset(token)
            (isolated_env / kwargs["cwd"] / "child.txt").write_text("output")
            return {"status": "success", "text": "finished"}

    result = ExecutorDaemon(
        agent_label="Desktop_Agent2", shadow_mode=False, backend=Backend()
    ).poll_once()
    assert result["waiting_for_review"]
    notes = _notes(task_id)
    expected = "direct-pr" if override else "local-only"
    assert f"[delivery-run: mode={expected} yolo=off]" in notes
    if override:
        assert "[delivery: mode=direct-pr yolo=off]" in notes
        assert "Current choice" in notes and "keep prose" in notes


def test_marker_replacements_preserve_adjacent_prose():
    delivered = annotate_notes(
        "Before [delivery-run: mode=direct-pr] after\nUntouched.", DeliveryPolicy(mode="local-only")
    )
    assert "Before" in delivered and "after" in delivered and "Untouched." in delivered
    assert "mode=direct-pr" not in delivered
    scouted = apply_scout_finish("Before [scout: report=old.md] after\nUntouched.", "new.md")
    assert "Before" in scouted and "after" in scouted and "Untouched." in scouted
    assert "report=old.md" not in scouted


@pytest.mark.parametrize("replacement", ["run", "generation"])
def test_atomic_finish_rejects_stale_successor(isolated_env, replacement):
    created = delegations.delegate(
        "Implement output", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegations.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
        with storage.connect() as conn:
            conn.execute(
                "UPDATE tasks SET notes='Successor instructions' WHERE id=?", (claim["id"],)
            )
            if replacement == "run":
                conn.execute(
                    "UPDATE tasks SET current_run_id='sp-successor' WHERE id=?", (claim["id"],)
                )
            else:
                conn.execute(
                    "UPDATE delegations SET claim_generation=claim_generation+1 WHERE id=?",
                    (delegation["id"],),
                )
            conn.commit()
        with pytest.raises(ToolError):
            tasks.task_update(
                claim["id"],
                status="doing",
                executor_finish={"kind": "delivery"},
                expected_run_id=claim["current_run_id"],
                expected_delegation_id=delegation["id"],
                expected_claim_generation=delegation["claim_generation"],
                evidence_ref="abc123",
            )
        assert _notes(claim["id"]) == "Successor instructions"
    finally:
        config.agent_identity_var.reset(token)


def _claimed_operation():
    created = delegations.delegate(
        "Implement output", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegations.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)
    return {
        "id": claim["id"],
        "status": "doing",
        "executor_finish": {"kind": "delivery"},
        "expected_run_id": claim["current_run_id"],
        "expected_delegation_id": delegation["id"],
        "expected_claim_generation": delegation["claim_generation"],
        "evidence_ref": "abc123",
    }


def _as_executor(**arguments):
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        return tasks.task_update(**arguments)
    finally:
        config.agent_identity_var.reset(token)


def test_finish_and_append_serialize_in_one_transaction(isolated_env, monkeypatch):
    operation = _claimed_operation()
    resolving, editing, release = Event(), Event(), Event()
    real_resolve = delivery.resolve_delivery

    def held_resolve(*args, **kwargs):
        resolving.set()
        assert release.wait(10)
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(delivery, "resolve_delivery", held_resolve)

    def append():
        assert resolving.wait(10)
        editing.set()
        return _as_executor(
            id=operation["id"], append_note="Note behind the finish transaction", worktree=False
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        finish = workers.submit(_as_executor, **operation)
        writer = workers.submit(append)
        assert editing.wait(10)
        release.set()
        finish.result(timeout=10)
        writer.result(timeout=10)
    notes = _notes(operation["id"])
    assert "[waiting-for: review]" in notes
    assert "Note behind the finish transaction" in notes


def test_finish_failure_rolls_back_evidence_and_notes(isolated_env, monkeypatch):
    operation = _claimed_operation()
    before = _notes(operation["id"])

    def fail(*args, **kwargs):
        raise RuntimeError("resolution failed")

    monkeypatch.setattr(delivery, "resolve_delivery", fail)
    with pytest.raises(RuntimeError, match="resolution failed"):
        _as_executor(**operation)
    assert _notes(operation["id"]) == before
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT evidence_ref FROM tasks WHERE id=?", (operation["id"],)
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_run_id": None},
        {"expected_delegation_id": None},
        {"expected_claim_generation": None},
        {"status": "done"},
        {"notes": "overwrite"},
        {"append_note": "extra"},
        {"project": "browser"},
        {"release_claim": True},
        {"worktree": True},
        {"executor_finish": {"kind": "scout", "report_path": "report.md"}},
        {"executor_finish": {"kind": "delivery", "notes": "overwrite"}},
        {"executor_finish": {"kind": "scout", "report_path": "report.md\n[no-code]"}},
    ],
)
def test_finish_rejects_incomplete_or_ambiguous_operations(isolated_env, changes):
    operation = _claimed_operation()
    before = _notes(operation["id"])
    with pytest.raises(ToolError):
        _as_executor(**(operation | changes))
    assert _notes(operation["id"]) == before


def test_untrusted_quarantine_appends_to_live_notes(isolated_env, monkeypatch):
    created = delegations.delegate(
        "Quarantine untrusted input", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    task_id = created["task_id"]
    with storage.connect() as conn:
        conn.execute("UPDATE tasks SET untrusted=1 WHERE id=?", (task_id,))
        conn.commit()

    class Backend:
        name = "synthetic"

        def run(self, **kwargs):
            pytest.fail("untrusted task ran a backend")

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False, backend=Backend())
    real_failed = daemon._mark_delegation_failed

    def before_quarantine(*args, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as workers:
            workers.submit(
                _as_executor,
                id=task_id,
                append_note="Security reviewer clarification",
                worktree=False,
            ).result(timeout=10)
        return real_failed(*args, **kwargs)

    monkeypatch.setattr(daemon, "_mark_delegation_failed", before_quarantine)
    result = daemon.poll_once()
    assert result["status"] == "quarantined_untrusted"
    notes = _notes(task_id)
    assert "Security reviewer clarification" in notes
    assert "[failed: untrusted task quarantined" in notes


def test_delivery_finish_keeps_non_git_no_evidence_compatibility(isolated_env):
    operation = _claimed_operation()
    result = _as_executor(**(operation | {"evidence_ref": None}))
    assert "[waiting-for: review]" in result["notes"]
    assert result["evidence_ref"] is None


@pytest.mark.parametrize("state", ["pending", "completed", "failed", "cancelled"])
def test_finish_refuses_a_retired_or_unclaimed_delegation(isolated_env, state):
    operation = _claimed_operation()
    before = _notes(operation["id"])
    with storage.connect() as conn:
        conn.execute(
            "UPDATE delegations SET status=? WHERE id=?",
            (state, operation["expected_delegation_id"]),
        )
        conn.commit()
    with pytest.raises(ToolError, match="delegation_not_current"):
        _as_executor(**operation)
    assert _notes(operation["id"]) == before


def test_task_update_existing_positional_priority_is_unchanged(isolated_env):
    task = tasks.task_add("Keep positional API compatibility")
    result = tasks.task_update(task["id"], None, None, None, None, "low")
    assert result["priority"] == "low"


@pytest.mark.parametrize("kind", ["delivery", "scout"])
@pytest.mark.parametrize(
    "history",
    [
        "[waiting-for: review]\n[review: rejected] Reviewer: earlier output failed.",
        "[waiting-for: review]\n[review: approved] Reviewer: earlier work approved.",
        '[waiting-for: review]\n[review: rejected @ 2026-09-30T00:00:00Z] Failed.\nReviewer quoted `[waiting-for: review]` and "[waiting-for: review]" for context.',
    ],
)
def test_new_finish_rearms_review_after_concurrent_resolution(isolated_env, kind, history):
    from unified_agent import reviewer

    created = delegations.delegate(
        "[task-shape: scout] Investigate" if kind == "scout" else "Implement output",
        "Desktop_Agent1",
        "Desktop_Agent2",
        callback="none",
    )
    task_id = created["task_id"]

    class Backend:
        name = "synthetic"

        def run(self, **kwargs):
            # Deterministic post-claim barrier: a review outcome reaches the
            # canonical row while this attempt is held in its backend.
            _as_executor(id=task_id, append_note=history, worktree=False)
            assert not reviewer._has_live_review_marker(_notes(task_id))
            if kind == "scout":
                text = f"[scout: report={report_path_from_text(kwargs['prompt'])}]"
            else:
                (isolated_env / kwargs["cwd"] / "child.txt").write_text("new output")
                text = "Finished new output"
            return {"status": "success", "text": text}

    result = ExecutorDaemon(
        agent_label="Desktop_Agent2", shadow_mode=False, backend=Backend()
    ).poll_once()
    assert result["waiting_for_review"]
    notes = _notes(task_id)
    assert history in notes  # retain the complete historical review prose
    assert reviewer._has_live_review_marker(notes)
    assert task_id in {row["id"] for row in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert notes.rindex("[waiting-for: review]") > notes.rindex("[review:")


def test_same_finish_retry_key_does_not_reissue_review_request(isolated_env):
    operation = _claimed_operation() | {"idempotency_key": "test-same-finish-request"}
    _as_executor(**operation)
    _as_executor(id=operation["id"], append_note="[review: rejected] Human outcome", worktree=False)
    before = _notes(operation["id"])
    _as_executor(**operation)
    assert _notes(operation["id"]) == before
    from unified_agent import reviewer

    assert not reviewer._has_live_review_marker(before)


def test_generic_finish_helpers_keep_default_review_idempotency():
    from unified_agent.delivery import apply_finish_markers

    policy = DeliveryPolicy()
    delivered = apply_finish_markers("Human prose", policy)
    assert apply_finish_markers(delivered, policy) == delivered
    scouted = apply_scout_finish("Human prose", "report.md")
    assert apply_scout_finish(scouted, "report.md").count("[waiting-for: review]") == 1


@pytest.mark.parametrize("kind", ["delivery", "scout"])
@pytest.mark.parametrize(
    "old_outcome", ["[review: approved]", "[review: no-merge]", "[merged commit: old123]"]
)
def test_new_handoff_requires_its_own_completion_outcome(isolated_env, kind, old_outcome):
    operation = _claimed_operation()
    _as_executor(
        id=operation["id"],
        append_note=f"[waiting-for: review] Old work\n{old_outcome} Previous approval",
        worktree=False,
    )
    if kind == "scout":
        operation |= {
            "executor_finish": {"kind": "scout", "report_path": "new-report.md"},
            "evidence_ref": "vault:new-report.md",
        }
    _as_executor(**operation)
    with pytest.raises(ToolError, match="waiting for review"):
        _as_executor(id=operation["id"], status="done", worktree=False)
    _as_executor(
        id=operation["id"],
        append_note='Reviewer quoted `[review: approved]` and "[review: no-merge]" for context.',
        worktree=False,
    )
    with pytest.raises(ToolError, match="waiting for review"):
        _as_executor(id=operation["id"], status="done", worktree=False)
    _as_executor(
        id=operation["id"], append_note="[review: approved] Current result approved", worktree=False
    )
    assert _as_executor(id=operation["id"], status="done", worktree=False)["status"] == "done"


def test_completion_rejects_latest_negative_outcome():
    assert (
        tasks._completion_review_error(
            "[waiting-for: review]\n[review: approved]\n[review: rejected]"
        )
        is not None
    )


def test_current_yolo_merge_receipt_allows_completion(isolated_env):
    notes = "[waiting-for: review]\n[merged commit: new123 @ 2026-09-30T13:00:00Z]\n[review: auto-merged @ 2026-09-30T13:00:01Z] current landed merge"
    assert tasks._completion_review_error(notes) is None
    task = tasks.task_add("Reassert landed YOLO completion", notes=notes)
    assert _as_executor(id=task["id"], status="done", worktree=False)["status"] == "done"
    assert _as_executor(id=task["id"], status="done", worktree=False)["status"] == "done"


@pytest.mark.parametrize(
    "receipt",
    [
        "[merged commit: old123]\n[waiting-for: review]",
        '[waiting-for: review]\n"[merged commit: quoted123]"',
    ],
)
def test_yolo_completion_requires_current_unquoted_receipt(isolated_env, receipt):
    notes = receipt + "\n[review: auto-merged @ now]"
    assert tasks._completion_review_error(notes) is not None
    task = tasks.task_add("Refuse stale or quoted YOLO receipt", notes=notes)
    with pytest.raises(ToolError, match="waiting for review"):
        _as_executor(id=task["id"], status="done", worktree=False)


@pytest.mark.parametrize("qualifier", [" when done", " once shipped", " ONCE SHIPPED"])
def test_future_review_template_preserves_current_completion(isolated_env, qualifier):
    from unified_agent import reviewer

    notes = "[waiting-for: review]\n[review: approved]\n[waiting-for: review]" + qualifier
    assert not reviewer._has_live_review_marker(notes)
    assert tasks._completion_review_error(notes) is None
    task = tasks.task_add("Complete approved work with future template", notes=notes)
    assert _as_executor(id=task["id"], status="done", worktree=False)["status"] == "done"
    # A subsequent real request still requires its own outcome.
    assert tasks._completion_review_error(notes + "\n[waiting-for: review]") is not None
