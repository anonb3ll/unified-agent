"""Cross-process acceptance evidence, including controls that must fail (#1456)."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from unified_agent import config

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/teamwork_rehearsal.py"


def load_rehearsal():
    spec = importlib.util.spec_from_file_location("teamwork_rehearsal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def launch(root, fault=None):
    argv = [sys.executable, str(SCRIPT), "--out", str(root)]
    if fault:
        argv.extend(["--fault", fault])
    # The script bounds its own coordinator and writes a failing report when
    # that bound is hit; this outer limit only catches a wedged launcher.
    timeout = load_rehearsal().COORDINATOR_TIMEOUT_S + 60
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def test_environment_is_allowlisted_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HUB_URL", "https://invalid.example")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "ambient-fixture-sentinel")
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(tmp_path / "host-charter"))
    monkeypatch.setenv("UA_EXECUTOR_TOKEN", "ambient-executor-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-provider-sentinel")
    env = load_rehearsal().isolated_environment(tmp_path)
    assert not set(env) & {
        "UA_HUB_URL",
        "UA_AGENT_BEARER_TOKEN",
        "UA_EXECUTOR_TOKEN",
        "OPENAI_API_KEY",
    }
    assert Path(env["HOME"]) == tmp_path / "fixture-home"
    assert Path(env["XDG_CACHE_HOME"]) == tmp_path / "cache"
    assert Path(env["XDG_DATA_HOME"]) == tmp_path / "data"
    assert Path(env["UA_HOME"]).is_relative_to(tmp_path)
    assert Path(env["UA_POLICY_FILE"]).is_relative_to(tmp_path)
    assert Path(env["UA_ORG_REGISTRY_PATH"]) == tmp_path / "org.yaml"
    assert env["UA_ORG_REGISTRY_PATH"] != str(tmp_path / "host-charter")
    assert env["UA_EVENT_BUS"] == env["UA_STATE_SYNC"] == "0"

    # Exercise the production child boundary, not just the fixture's parent.
    from unified_agent.executor.child_env import build_executor_child_env

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("UA_TEAMWORK_FAULT", "corrupt-artifact")
    child = build_executor_child_env("codex", "Desktop_Agent2")
    assert child["UA_TEAMWORK_ROOT"] == str(tmp_path)
    assert child["UA_TEAMWORK_FAULT"] == "corrupt-artifact"
    assert child["UA_ORG_REGISTRY_PATH"] == str(tmp_path / "org.yaml")
    assert "UA_EXECUTOR_TOKEN" not in child
    assert "UA_ENFORCE_AUTH" not in child
    assert "OPENAI_API_KEY" not in child
    assert child.get("UA_AGENT_BEARER_TOKEN") != "ambient-fixture-sentinel"


def test_internal_worker_refuses_nonisolated_store(tmp_path, monkeypatch):
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "fixture-only").write_text("teamwork-rehearsal-v1\n")
    monkeypatch.setenv("UA_TEAMWORK_ROOT", str(root))
    monkeypatch.setenv("UA_HOME", str(tmp_path / "other-store"))
    monkeypatch.setenv("UA_WORKSPACE", str(root / "workspace"))
    with pytest.raises(RuntimeError, match="isolation path mismatch"):
        load_rehearsal().fenced_root()


def test_internal_worker_refuses_ambient_organization(tmp_path, monkeypatch):
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "fixture-only").write_text("teamwork-rehearsal-v1\n")
    for key, value in load_rehearsal().isolated_environment(root).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(tmp_path / "host-charter"))
    with pytest.raises(RuntimeError, match="isolation path mismatch"):
        load_rehearsal().fenced_root()


def test_rehearsal_preserves_ambient_store_and_proves_recovery(tmp_path):
    ambient = config.db_path()
    before = ambient.read_bytes()
    root = tmp_path / "evidence"
    result = launch(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ambient.read_bytes() == before
    report = json.loads((root / "report.json").read_text())
    assert report["status"] == "pass"
    assert report["live_models_verified"] is report["live_a2a_verified"] is False
    assert {row["adapter"] for row in report["attempts"]} == {"codex", "cursor"}
    assert {row["identity"] for row in report["attempts"]} == {
        "Desktop_Agent2",
        "Desktop_Agent4",
    }
    assert {row["status"] for row in report["attempts"]} == {"success", "killed", "timeout"}
    assert report["claim_generations"] == [1, 2]
    assert len(report["run_history"]) == len(set(report["run_ids"])) == 2
    assert all(row["outcome"] == "handed_off" for row in report["run_history"])
    assert report["event_chain"]["verified"]
    assert len(report["checks"]) == 15
    competing = json.loads((root / "runs/competing-claim-refused.json").read_text())
    assert competing["claim"]["claimed"] is False
    assert competing["claim"]["safe_wait"]["claimed_by"] == "Desktop_Agent2"
    assert all(row["status"] in {"completed", "abandoned"} for row in report["sessions"])
    log = json.loads((root / "events.json").read_text())
    messages = [row for row in log if row["channel"].startswith("agent.message.")]
    assert len(messages) == 2
    handoff = next(row for row in messages if row["payload"]["act"] == "handoff")
    ack = next(row for row in messages if row["payload"]["act"] == "done")
    assert handoff["payload"]["conversation_id"] == ack["payload"]["conversation_id"]
    assert ack["payload"]["reply_to"] == report["handoff_message_id"]
    with sqlite3.connect(root / "store/memory.db") as conn:
        assert conn.execute(
            "SELECT status,attempts,claim_generation FROM delegations"
        ).fetchall() == [("completed", 2, 2)]
        assert (
            conn.execute("SELECT COUNT(*) FROM tasks WHERE current_run_id IS NOT NULL").fetchone()[
                0
            ]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM agent_sessions WHERE status='abandoned'").fetchone()[
                0
            ]
            == 2
        )
    again = launch(root)
    assert again.returncode != 0  # An evidence directory is never reused/overwritten.
    assert json.loads((root / "report.json").read_text()) == report


@pytest.mark.parametrize(
    "fault, reason",
    [("corrupt-artifact", "receiver refused corrupt artifact"), ("deny-claim", "denied by policy")],
)
def test_negative_controls_fail_before_ack(tmp_path, fault, reason):
    root = tmp_path / "evidence"
    result = launch(root, fault)
    assert result.returncode != 0
    report = json.loads((root / "report.json").read_text())
    # On an unexpected failure, show the store state the report captured (#1644).
    assert report["status"] == "fail" and reason in report["error"], json.dumps(
        report.get("diagnostics"), indent=1
    )
    with sqlite3.connect(root / "store/memory.db") as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE channel='agent.message.done'"
            ).fetchone()[0]
            == 0
        )
        if fault == "deny-claim":
            assert (
                conn.execute("SELECT COUNT(*) FROM tasks WHERE claimed_by IS NOT NULL").fetchone()[
                    0
                ]
                == 0
            )


def seeded_root(tmp_path, monkeypatch):
    """A rehearsal-shaped root whose store/ holds a real claimed task."""
    from unified_agent import storage
    from unified_agent.tools.tasks import task_add, task_claim

    root = tmp_path / "rehearsal"
    (root / "runs").mkdir(parents=True)
    (root / "store").mkdir()
    monkeypatch.setenv("UA_HOME", str(root / "store"))
    storage.init_db()  # As the coordinator does before its first phase.
    task = task_add("Diagnostics fixture")
    claim = task_claim(task["id"], agent="Desktop_Agent2", timeout_seconds=3600)
    return root, task, claim


def assert_names_claimed_run(diagnostics, task, claim):
    assert "store_error" not in diagnostics, diagnostics.get("store_error")
    row = next(row for row in diagnostics["tasks"] if row["id"] == task["id"])
    assert row["claimed_by"] == "Desktop_Agent2"
    assert row["current_run_id"] == claim["current_run_id"]
    assert row["lock_expires_at"] == claim["lock_expires_at"]
    assert any(
        event["kind"] == "selfpickup.claimed" and event["run_id"] == claim["current_run_id"]
        for event in diagnostics["run_events"]
    )
    assert "memory.db" in diagnostics["store_files"]


def test_failure_report_names_task_and_run_state(tmp_path, monkeypatch):
    """A failing rehearsal must say which run each task holds (#1644)."""
    root, task, claim = seeded_root(tmp_path, monkeypatch)
    report = load_rehearsal().failure_report(root, "boom", [{"phase": "p"}])
    assert report["status"] == "fail" and report["error"] == "boom"
    assert report["diagnostics"]["phases"] == [{"phase": "p"}]
    assert_names_claimed_run(report["diagnostics"], task, claim)


def test_failure_report_without_store_says_so(tmp_path):
    report = load_rehearsal().failure_report(tmp_path, "boom")
    assert report["diagnostics"]["phases"] == []
    assert "store_error" in report["diagnostics"]


def test_coordinator_timeout_report_names_stalled_phase(tmp_path, monkeypatch):
    """A killed coordinator leaves only disk; the report must rebuild phases from it."""
    rehearsal = load_rehearsal()
    root, task, claim = seeded_root(tmp_path, monkeypatch)
    rehearsal.record_attempt(
        root,
        {"event": "start", "phase": "bootstrap", "adapter": "codex", "identity": "Desktop_Agent2"},
    )
    rehearsal.record_attempt(
        root,
        {
            "event": "end",
            "phase": "bootstrap",
            "adapter": "codex",
            "status": "success",
            "elapsed_s": 4.2,
        },
    )
    (root / "runs/bootstrap.json").write_text("{}")
    rehearsal.record_attempt(
        root,
        {"event": "start", "phase": "claim", "adapter": "cursor", "identity": "Desktop_Agent4"},
    )
    with open(root / rehearsal.ATTEMPTS_LOG, "a") as handle:
        handle.write('{"event": "end", "pha')  # Killed mid-write.
    report = rehearsal.coordinator_timeout_report(root, "x" * 5000 + "stalled")
    diagnostics = report["diagnostics"]
    assert report["status"] == "fail"
    assert str(rehearsal.COORDINATOR_TIMEOUT_S) in report["error"]
    done, stalled = diagnostics["phases"]
    assert done["phase"] == "bootstrap" and done["status"] == "success"
    assert done["elapsed_s"] == 4.2 and done["started_at"] <= done["ended_at"]
    assert stalled["phase"] == "claim" and "ended_at" not in stalled
    assert diagnostics["phase_in_progress"] == stalled
    assert diagnostics["phase_in_progress"]["identity"] == "Desktop_Agent4"
    assert diagnostics["completed_phase_markers"] == ["bootstrap"]
    assert diagnostics["coordinator_stderr_tail"].endswith("stalled")
    assert len(diagnostics["coordinator_stderr_tail"]) == 4000
    assert_names_claimed_run(diagnostics, task, claim)


def test_coordinator_timeout_report_without_attempts_log(tmp_path):
    report = load_rehearsal().coordinator_timeout_report(tmp_path, "")
    assert report["diagnostics"]["phases"] == []
    assert report["diagnostics"]["phase_in_progress"] is None


def test_network_denial_is_explicit():
    with pytest.raises(OSError, match="refuses outbound network"):
        load_rehearsal().block_network()
