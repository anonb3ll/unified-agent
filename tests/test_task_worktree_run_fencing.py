"""A successor task run or delegation attempt owns an independent Git index."""

import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.tools import delegate as delegation_tools
from unified_agent.tools import tasks


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(isolated_env, workspace_is_product_repo):
    git(isolated_env, "init", "-b", "main")
    git(
        isolated_env,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    return isolated_env


def expired(task_id):
    conn = storage.connect()
    conn.execute(
        "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (task_id,)
    )
    conn.commit()
    conn.close()


def path(repo, value):
    candidate = Path(value)
    return candidate if candidate.is_absolute() else repo / candidate


def test_first_claim_legacy_path_and_live_renewal_remain_stable(repo):
    task = tasks.task_add("Stable checkout")
    first = tasks.task_claim(task["id"], "Desktop_agent3")
    assert first["worktree_path"] == str(repo.resolve() / f".worktrees/task-{task['id']}")
    renewed = tasks.task_claim(
        task["id"], "Desktop_agent3", expected_run_id=first["current_run_id"]
    )
    assert renewed["current_run_id"] == first["current_run_id"]
    assert renewed["worktree_path"] == first["worktree_path"]


@pytest.mark.parametrize("same_agent", [False, True])
def test_takeover_preserves_dirty_old_tree_and_uses_independent_branch_index(repo, same_agent):
    task = tasks.task_add("Take over safely")
    first = tasks.task_claim(task["id"], "Desktop_agent3")
    old = path(repo, first["worktree_path"])
    (old / "old-only.txt").write_text("old agent output")
    git(old, "add", "old-only.txt")
    old_index = git(old, "rev-parse", "--git-path", "index")
    expired(task["id"])
    second = tasks.task_claim(task["id"], "Desktop_agent3" if same_agent else "Desktop_Agent2")
    new = path(repo, second["worktree_path"])
    assert second["current_run_id"] != first["current_run_id"]
    assert second["branch"] != first["branch"]
    assert new != old
    assert git(new, "rev-parse", "--git-path", "index") != old_index
    assert not (new / "old-only.txt").exists()
    assert (old / "old-only.txt").read_text() == "old agent output"
    assert git(old, "status", "--porcelain") == "A  old-only.txt"


def test_takeover_moves_old_review_and_evidence_to_history(repo):
    task = tasks.task_add("New evidence required")
    first = tasks.task_claim(task["id"], "Desktop_agent3")
    tasks.task_update(
        task["id"],
        status="waiting_review",
        append_note="[waiting-for: review] old PR\n[review: approved] prior result",
        evidence_ref="https://example.invalid/old-pr",
        worktree=False,
        actor="Desktop_agent3",
        expected_run_id=first["current_run_id"],
    )
    second = tasks.task_claim(task["id"], "Desktop_agent3")
    assert second["evidence_ref"] is None
    assert "[waiting-for: review]" not in second["notes"]
    assert "[review: approved]" not in second["notes"]
    assert "old PR" in second["notes"]
    assert "https://example.invalid/old-pr" in second["notes"]
    assert path(repo, first["worktree_path"]).exists()
    assert second["worktree_path"] != first["worktree_path"]


def test_claim_refuses_successor_response_when_run_changes_during_creation(repo, monkeypatch):
    task = tasks.task_add("Provision race")
    original = tasks._git_worktree_add_from_known_project_repo
    created = []

    def racing_add(*args, **kwargs):
        result = original(*args, **kwargs)
        created.append(args[0])
        conn = storage.connect()
        conn.execute(
            "UPDATE tasks SET current_run_id='sp-successor', current_run_agent='Desktop_Agent2', claimed_by='Desktop_Agent2', worktree_path='successor-only', branch='task/successor' WHERE id=?",
            (task["id"],),
        )
        conn.commit()
        conn.close()
        return result

    monkeypatch.setattr(tasks, "_git_worktree_add_from_known_project_repo", racing_add)
    with pytest.raises(ToolError, match="run_not_current"):
        tasks.task_claim(task["id"], "Desktop_agent3")
    conn = storage.connect()
    row = conn.execute(
        "SELECT worktree_path,branch FROM tasks WHERE id=?", (task["id"],)
    ).fetchone()
    assert tuple(row) == ("successor-only", "task/successor")
    conn.close()
    assert path(repo, created[0]).exists()


def provision(task_id, run, delegation_id, generation):
    return tasks.task_update(
        task_id,
        worktree=True,
        actor="Desktop_agent3",
        expected_run_id=run,
        expected_delegation_id=delegation_id,
        expected_claim_generation=generation,
    )


def test_same_run_new_delegation_generation_gets_a_clean_index(repo):
    item = delegation_tools.delegate(
        "Isolate retries", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    first_delegation = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")[
        "delegation"
    ]
    claimed = tasks.task_claim(item["task_id"], "Desktop_agent3")
    first = provision(
        item["task_id"],
        claimed["current_run_id"],
        item["delegation_id"],
        first_delegation["claim_generation"],
    )
    old = path(repo, first["worktree_path"])
    (old / "attempt-one.txt").write_text("stale output")
    git(old, "add", "attempt-one.txt")
    assert delegation_tools.delegate_release(
        item["delegation_id"], "Desktop_agent3", first_delegation["claim_generation"]
    )["released"]
    second_delegation = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")[
        "delegation"
    ]
    second = provision(
        item["task_id"],
        claimed["current_run_id"],
        item["delegation_id"],
        second_delegation["claim_generation"],
    )
    assert second["current_run_id"] == first["current_run_id"]
    assert second["branch"] != first["branch"]
    assert second["worktree_path"] != first["worktree_path"]
    assert not (path(repo, second["worktree_path"]) / "attempt-one.txt").exists()
    assert (old / "attempt-one.txt").exists()
    with pytest.raises(ToolError, match="delegation_not_current"):
        provision(
            item["task_id"],
            claimed["current_run_id"],
            item["delegation_id"],
            first_delegation["claim_generation"],
        )


def test_generation_change_during_creation_cannot_rebind_successor(repo, monkeypatch):
    item = delegation_tools.delegate(
        "Generation race", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    delegation = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")[
        "delegation"
    ]
    claimed = tasks.task_claim(item["task_id"], "Desktop_agent3")
    original = tasks._git_worktree_add_from_known_project_repo

    def racing_add(*args, **kwargs):
        result = original(*args, **kwargs)
        conn = storage.connect()
        conn.execute(
            "UPDATE delegations SET claim_generation=claim_generation+1 WHERE id=?",
            (item["delegation_id"],),
        )
        conn.execute(
            "UPDATE tasks SET worktree_path='successor-only', branch='task/successor' WHERE id=?",
            (item["task_id"],),
        )
        conn.commit()
        conn.close()
        return result

    monkeypatch.setattr(tasks, "_git_worktree_add_from_known_project_repo", racing_add)
    with pytest.raises(ToolError, match="delegation_not_current"):
        provision(
            item["task_id"],
            claimed["current_run_id"],
            item["delegation_id"],
            delegation["claim_generation"],
        )
    conn = storage.connect()
    assert (
        conn.execute("SELECT worktree_path FROM tasks WHERE id=?", (item["task_id"],)).fetchone()[0]
        == "successor-only"
    )
    conn.close()


def test_restrictive_native_admission_cannot_provision_takeover(repo, monkeypatch):
    task = tasks.task_add("Preserve containment")
    first = tasks.task_claim(task["id"], "Desktop_agent3")
    expired(task["id"])
    monkeypatch.setattr(
        tasks.run_records,
        "native_execution_status",
        lambda *args: {
            "native_execution_allowed": False,
            "native_execution_denial": "Containment unavailable",
        },
    )
    with pytest.raises(ToolError, match="Containment unavailable"):
        tasks.task_claim(task["id"], "Desktop_agent3")
    conn = storage.connect()
    row = conn.execute(
        "SELECT current_run_id,worktree_path FROM tasks WHERE id=?", (task["id"],)
    ).fetchone()
    assert tuple(row) == (first["current_run_id"], first["worktree_path"])
    conn.close()
    assert path(repo, first["worktree_path"]).exists()


def test_task_update_new_run_cannot_leave_an_old_checkout_for_renewal(repo):
    task = tasks.task_add("Reopen through task update")
    first = tasks.task_claim(task["id"], "Desktop_agent3")
    old = path(repo, first["worktree_path"])
    (old / "prior-run.txt").write_text("Keep this dirty output")
    tasks.task_update(
        task["id"],
        status="blocked",
        worktree=False,
        actor="Desktop_agent3",
        expected_run_id=first["current_run_id"],
    )
    reopened = tasks.task_update(task["id"], status="doing", worktree=False, actor="Desktop_agent3")
    assert reopened["current_run_id"] != first["current_run_id"]
    assert reopened["worktree_path"] is None
    claimed = tasks.task_claim(
        task["id"], "Desktop_agent3", expected_run_id=reopened["current_run_id"]
    )
    assert claimed["current_run_id"] == reopened["current_run_id"]
    assert claimed["worktree_path"] != first["worktree_path"]
    assert (old / "prior-run.txt").exists()
    assert not (path(repo, claimed["worktree_path"]) / "prior-run.txt").exists()


@pytest.mark.parametrize("mutation", ["notes", "blocked", "review"])
def test_stale_generation_cannot_publish_task_lifecycle_or_evidence(repo, mutation):
    item = delegation_tools.delegate(
        "Task write generation fence", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    delegation = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")[
        "delegation"
    ]
    claimed = tasks.task_claim(item["task_id"], "Desktop_agent3")
    conn = storage.connect()
    conn.execute(
        "UPDATE delegations SET claim_generation=claim_generation+1 WHERE id=?",
        (item["delegation_id"],),
    )
    conn.commit()
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (item["task_id"],)).fetchone())
    conn.close()
    kwargs = {"append_note": "stale success", "evidence_ref": "stale-head"}
    if mutation == "blocked":
        kwargs["status"] = "blocked"
    elif mutation == "review":
        kwargs.update(status="waiting_review", append_note="[waiting-for: review] stale success")
    with pytest.raises(ToolError, match="delegation_not_current"):
        tasks.task_update(
            item["task_id"],
            actor="Desktop_agent3",
            worktree=False,
            expected_run_id=claimed["current_run_id"],
            expected_delegation_id=item["delegation_id"],
            expected_claim_generation=delegation["claim_generation"],
            **kwargs,
        )
    conn = storage.connect()
    assert (
        dict(conn.execute("SELECT * FROM tasks WHERE id=?", (item["task_id"],)).fetchone())
        == before
    )
    conn.close()


def test_current_generation_can_publish_its_review_handoff(repo):
    item = delegation_tools.delegate(
        "Current task finish", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    delegation = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")[
        "delegation"
    ]
    claimed = tasks.task_claim(item["task_id"], "Desktop_agent3")
    finished = tasks.task_update(
        item["task_id"],
        status="waiting_review",
        append_note="[waiting-for: review] current work",
        evidence_ref="current-head",
        actor="Desktop_agent3",
        worktree=False,
        expected_run_id=claimed["current_run_id"],
        expected_delegation_id=item["delegation_id"],
        expected_claim_generation=delegation["claim_generation"],
    )
    assert finished["evidence_ref"] == "current-head"
    assert finished["current_run_id"] is None


def test_provisioning_rejects_inconsistent_current_run_actor(repo):
    task = tasks.task_add("Run actor must match")
    claimed = tasks.task_claim(task["id"], "Desktop_agent3")
    conn = storage.connect()
    conn.execute("UPDATE tasks SET current_run_agent='Desktop_Agent2' WHERE id=?", (task["id"],))
    conn.commit()
    conn.close()
    with pytest.raises(ToolError, match="run_not_current"):
        tasks.task_update(
            task["id"],
            worktree=True,
            actor="Desktop_agent3",
            expected_run_id=claimed["current_run_id"],
        )


@pytest.mark.parametrize(
    "action,status", [("complete", "waiting_review"), ("fail", "blocked"), ("retry", "open")]
)
def test_matching_terminal_delegation_can_publish_its_task_transition(repo, action, status):
    item = delegation_tools.delegate(
        "Publish matching terminal receipt", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    delegation = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")[
        "delegation"
    ]
    claimed = tasks.task_claim(item["task_id"], "Desktop_agent3")
    assertions = dict(
        expected_run_id=claimed["current_run_id"],
        expected_claim_generation=delegation["claim_generation"],
    )
    assert delegation_tools.delegation_manage(
        action,
        dict(
            id=item["delegation_id"], task_id=item["task_id"], agent="Desktop_agent3", **assertions
        ),
    )
    result = tasks.task_update(
        item["task_id"],
        status=status,
        worktree=False,
        actor="Desktop_agent3",
        append_note="truthful result",
        expected_delegation_id=item["delegation_id"],
        **assertions,
    )
    assert result["status"] == status
    assert result["current_run_id"] is None


def test_terminal_delegation_cannot_provision_a_native_worktree(repo):
    item = delegation_tools.delegate(
        "No source for terminal attempts", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    delegation = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")[
        "delegation"
    ]
    claimed = tasks.task_claim(item["task_id"], "Desktop_agent3")
    assertions = dict(
        expected_run_id=claimed["current_run_id"],
        expected_claim_generation=delegation["claim_generation"],
    )
    assert delegation_tools.delegate_fail(item["delegation_id"], "Desktop_agent3", **assertions)[
        "failed"
    ]
    with pytest.raises(ToolError, match="delegation_not_current"):
        tasks.task_update(
            item["task_id"],
            worktree=True,
            actor="Desktop_agent3",
            expected_delegation_id=item["delegation_id"],
            **assertions,
        )


def test_equal_generation_from_another_stored_delegation_cannot_share_index(repo):
    first = delegation_tools.delegate(
        "First stored delegation", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    second = delegation_tools.delegate(
        "Second stored delegation", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    conn = storage.connect()
    conn.execute(
        "UPDATE delegations SET task_id=? WHERE id=?", (first["task_id"], second["delegation_id"])
    )
    conn.commit()
    conn.close()
    d1 = delegation_tools.delegate_claim(first["delegation_id"], "Desktop_agent3")["delegation"]
    d2 = delegation_tools.delegate_claim(second["delegation_id"], "Desktop_agent3")["delegation"]
    assert d1["claim_generation"] == d2["claim_generation"]
    claimed = tasks.task_claim(first["task_id"], "Desktop_agent3")
    one = provision(
        first["task_id"], claimed["current_run_id"], first["delegation_id"], d1["claim_generation"]
    )
    old = path(repo, one["worktree_path"])
    (old / "first-delegation.txt").write_text("belongs only to delegation one")
    git(old, "add", "first-delegation.txt")
    two = provision(
        first["task_id"], claimed["current_run_id"], second["delegation_id"], d2["claim_generation"]
    )
    assert two["worktree_path"] != one["worktree_path"]
    assert not (path(repo, two["worktree_path"]) / "first-delegation.txt").exists()
    assert (old / "first-delegation.txt").exists()


def test_task_update_new_run_archives_predecessor_delivery_metadata(repo):
    task = tasks.task_add("Reopen without inherited delivery")
    first = tasks.task_claim(task["id"], "Desktop_agent3")
    tasks.task_update(
        task["id"],
        status="waiting_review",
        append_note="[waiting-for: review] old delivery",
        evidence_ref="prior-head",
        worktree=False,
        actor="Desktop_agent3",
        expected_run_id=first["current_run_id"],
    )
    reopened = tasks.task_update(task["id"], status="doing", worktree=False, actor="Desktop_agent3")
    assert reopened["evidence_ref"] is None
    assert "[waiting-for: review]" not in reopened["notes"]
    assert "prior-head" in reopened["notes"]
    assert first["current_run_id"] in reopened["notes"]


def test_new_delegation_generation_archives_prior_attempt_delivery_metadata(repo):
    item = delegation_tools.delegate(
        "New attempt needs new evidence", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    first = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")["delegation"]
    claimed = tasks.task_claim(item["task_id"], "Desktop_agent3")
    provision(
        item["task_id"], claimed["current_run_id"], item["delegation_id"], first["claim_generation"]
    )
    tasks.task_update(
        item["task_id"],
        append_note="[waiting-for: review] prior attempt",
        evidence_ref="old-attempt-head",
        worktree=False,
        actor="Desktop_agent3",
        expected_run_id=claimed["current_run_id"],
    )
    delegation_tools.delegate_release(
        item["delegation_id"], "Desktop_agent3", first["claim_generation"]
    )
    second = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")["delegation"]
    fresh = provision(
        item["task_id"],
        claimed["current_run_id"],
        item["delegation_id"],
        second["claim_generation"],
    )
    assert fresh["evidence_ref"] is None
    assert "[waiting-for: review]" not in fresh["notes"]
    assert "old-attempt-head" in fresh["notes"]


def test_canonical_provision_response_records_exact_attached_source_sha(repo):
    item = delegation_tools.delegate(
        "Canonical source provenance", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    delegation = delegation_tools.delegate_claim(item["delegation_id"], "Desktop_agent3")[
        "delegation"
    ]
    claimed = tasks.task_claim(item["task_id"], "Desktop_agent3")
    assert claimed["worktree_source_sha"] == git(
        path(repo, claimed["worktree_path"]), "rev-parse", "HEAD"
    )
    provisioned = provision(
        item["task_id"],
        claimed["current_run_id"],
        item["delegation_id"],
        delegation["claim_generation"],
    )
    assert provisioned["worktree_source_sha"] == git(
        path(repo, provisioned["worktree_path"]), "rev-parse", "HEAD"
    )


def test_source_provenance_refuses_detached_or_foreign_checkout(repo, tmp_path):
    task = tasks.task_add("Source identity")
    claimed = tasks.task_claim(task["id"], "Desktop_agent3")
    git(path(repo, claimed["worktree_path"]), "checkout", "--detach")
    assert tasks._task_worktree_source_sha(claimed) is None
    foreign = tmp_path / "foreign-source"
    foreign.mkdir()
    git(foreign, "init", "-b", "main")
    git(
        foreign,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "foreign",
    )
    assert (
        tasks._task_worktree_source_sha(
            {**claimed, "worktree_path": str(foreign), "branch": "main"}
        )
        is None
    )


def test_done_cleanup_cannot_clear_successor_worktree_binding(repo, monkeypatch):
    task = tasks.task_add("Reopen while predecessor checkout is released")
    first = tasks.task_claim(task["id"], "Desktop_agent3")
    original_release = tasks._release_task_worktree
    successor = {}

    def reopen_after_release(*args, **kwargs):
        result = original_release(*args, **kwargs)
        assert result == "removed"
        tasks.task_update(task["id"], status="doing", actor="Desktop_agent3", worktree=False)
        successor.update(tasks.task_claim(task["id"], "Desktop_agent3"))
        return result

    monkeypatch.setattr(tasks, "_release_task_worktree", reopen_after_release)
    tasks.task_update(
        task["id"],
        status="done",
        actor="Desktop_agent3",
        expected_run_id=first["current_run_id"],
        worktree=False,
    )
    conn = storage.connect()
    current = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone())
    conn.close()
    assert current["status"] == "doing"
    assert current["current_run_id"] == successor["current_run_id"]
    assert current["current_run_id"] != first["current_run_id"]
    assert current["worktree_path"] == successor["worktree_path"]
    assert current["branch"] == successor["branch"]
    assert path(repo, current["worktree_path"]).is_dir()


@pytest.mark.parametrize("skip", [False, True])
def test_unclaimed_actor_backed_update_starts_with_captured_run_checkout(repo, monkeypatch, skip):
    from unified_agent import config

    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: skip)
    task = tasks.task_add("Actor-backed start")
    started = tasks.task_update(task["id"], status="doing", actor="Desktop_agent3", worktree=True)
    assert started["claimed_by"] is None
    # An unverified `actor` is attribution, not identity (task #1886): the run
    # opens with no agent so the asserted label is not charged per-agent WIP.
    # The checkout is still bound to this run.
    assert started["current_run_agent"] is None
    assert started["current_run_id"].startswith("sp-")
    if skip:
        assert started["worktree_path"] is None
    else:
        assert path(repo, started["worktree_path"]).is_dir()
        assert (
            git(path(repo, started["worktree_path"]), "branch", "--show-current")
            == started["branch"]
        )


def test_actor_backed_provisioning_refuses_claimant_takeover_during_creation(repo, monkeypatch):
    task = tasks.task_add("Actor authority barrier")
    original = tasks._git_worktree_add_from_known_project_repo
    created = []

    def takeover_after_add(*args, **kwargs):
        result = original(*args, **kwargs)
        created.append(args[0])
        with storage.connect() as conn:
            conn.execute(
                "UPDATE tasks SET current_run_id='sp-successor', current_run_agent='Desktop_Agent2', claimed_by='Desktop_Agent2', worktree_path='successor-preserved', branch='task/successor' WHERE id=?",
                (task["id"],),
            )
            conn.commit()
        return result

    monkeypatch.setattr(tasks, "_git_worktree_add_from_known_project_repo", takeover_after_add)
    with pytest.raises(ToolError, match="run_not_current"):
        tasks.task_update(task["id"], status="doing", actor="Desktop_agent3", worktree=True)
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT claimed_by,current_run_id,worktree_path,branch FROM tasks WHERE id=?",
            (task["id"],),
        ).fetchone()
        assert tuple(row) == (
            "Desktop_Agent2",
            "sp-successor",
            "successor-preserved",
            "task/successor",
        )
    assert path(repo, created[0]).exists()
