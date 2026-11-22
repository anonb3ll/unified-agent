"""Spot VM CI backend (task #2252): the executor must run ci.yaml the way Cloud
Build does, and cloud-ci.sh must reach either backend only through its
helpers. No GCP or Docker calls."""

from __future__ import annotations

import importlib.util
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
CONFIG = yaml.safe_load((REPO_ROOT / "deploy" / "cloudbuild" / "ci.yaml").read_text())
RUNNER = REPO_ROOT / "scripts" / "cloud-ci.sh"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


executor = _load("spot_ci_executor", REPO_ROOT / "deploy" / "spot-ci" / "executor.py")
spot_ci = _load("spot_ci_client", REPO_ROOT / "scripts" / "spot_ci.py")


class FakeStep:
    """Finishes with `code` after `polls` polls; `None` never finishes."""

    def __init__(self, code: int | None, polls: int = 1):
        self.code, self.polls, self.killed = code, polls, False

    def poll(self):
        if self.killed:
            return -9
        self.polls -= 1
        return self.code if self.polls <= 0 else None

    def kill(self):
        self.killed = True


def _run(config: dict, codes: dict[str, int | None], clock_step: float = 1.0, *, deadline=None):
    started: list[str] = []

    def launch(sid, argv, log):
        started.append(sid)
        return FakeStep(codes.get(sid, 0))

    build = executor.Build(
        config, {"_SOURCE_SHA": "a" * 40}, launch, lambda sid, line: None, deadline=deadline
    )
    ticks = itertools.count(0, clock_step)
    result = build.run(poll_interval=0, clock=lambda: next(ticks))
    return result, dict((s["id"], s["status"]) for s in build.snapshot()[1]), started


def test_substitution_follows_cloud_build():
    subs = {"_PY_IMAGE": "python:3", "_SOURCE_SHA": "abc"}
    assert executor.substitute("${_PY_IMAGE} $_SOURCE_SHA $$result $$(cat x)", subs) == (
        "python:3 abc $result $(cat x)"
    )
    with pytest.raises(ValueError, match="unknown substitution"):
        executor.substitute("echo $result", subs)


def test_every_ci_yaml_step_renders_with_mounts():
    """An unescaped `$` in ci.yaml would fail every spot run before it starts."""
    build = executor.Build(CONFIG, {"_SOURCE_SHA": "a" * 40}, None, None)
    for i, sid in enumerate(build.deps):
        argv = executor.docker_command(build.by_id[sid], build.subs, f"ua-ci-{i}")
        assert "/var/run/docker.sock:/var/run/docker.sock" in argv
        assert "/workspace:/workspace" in argv
        assert not any("${_" in arg for arg in argv), sid


def test_wait_for_defaults_to_every_earlier_step():
    steps = [{"id": "a"}, {"id": "b", "waitFor": ["-"]}, {"id": "c"}, {"id": "d", "waitFor": ["a"]}]
    assert executor.dependencies(steps) == {"a": [], "b": [], "c": ["a", "b"], "d": ["a"]}
    with pytest.raises(ValueError, match="unknown or later"):
        executor.dependencies([{"id": "a", "waitFor": ["b"]}, {"id": "b"}])


def test_green_run_starts_setup_first_and_succeeds():
    result, steps, started = _run(CONFIG, {})
    assert result == "SUCCESS"
    assert set(steps.values()) == {"SUCCESS"}
    assert started[0] == "setup" and started[-1] == "full-verification"


def test_allowed_failure_is_reported_and_aggregate_still_runs():
    result, steps, started = _run(CONFIG, {"shard-2": 1, "full-verification": 1})
    assert steps["shard-2"] == "FAILURE"
    assert "full-verification" in started
    assert result == "FAILURE"  # the aggregate gate is not allowFailure


def test_hard_failure_cancels_pending_steps():
    result, steps, started = _run(CONFIG, {"setup": 1})
    assert result == "FAILURE"
    assert started == ["setup"]
    assert steps["setup"] == "FAILURE"
    assert {steps[s] for s in steps if s != "setup"} == {"CANCELLED"}


def test_build_timeout_stops_running_steps():
    config = {"steps": [{"id": "slow", "name": "x"}], "timeout": "10s"}
    result, steps, _ = _run(config, {"slow": None}, clock_step=4.0)
    assert result == "TIMEOUT"
    assert steps == {"slow": "TIMEOUT"}


def test_spot_deadline_caps_a_build_with_a_longer_ci_yaml_timeout():
    config = {"steps": [{"id": "slow", "name": "x"}], "timeout": "10800s"}
    result, steps, started = _run(config, {"slow": None}, deadline=0)
    assert (result, steps, started) == ("TIMEOUT", {"slow": "CANCELLED"}, [])
    result, steps, started = _run(config, {"slow": None}, deadline=6)
    assert (result, steps, started) == ("TIMEOUT", {"slow": "TIMEOUT"}, ["slow"])


def test_client_reads_injected_tags_with_substitution():
    text = "steps: []\ntags:\n- ua-ci-js-1\n- ua-ci-native-0\n- ua-ci\n- sha-${_SOURCE_SHA}\noptions: {}\n"
    assert spot_ci.config_tags(text, {"_SOURCE_SHA": "f00"}) == [
        "ua-ci-js-1",
        "ua-ci-native-0",
        "ua-ci",
        "sha-f00",
    ]


def test_runner_defaults_to_spot_and_describes_only_through_helper():
    text = RUNNER.read_text()
    assert 'backend="${UA_CI_BACKEND:-spot}"' in text
    # One `gcloud builds describe`, inside describe_cmd; every caller uses dcmd.
    assert len(re.findall(r"gcloud builds describe", text.split("describe_cmd() {", 1)[1])) == 1
    assert text.count('bounded "${dcmd[@]}"') == 4


def test_submit_queues_nothing_when_the_metadata_refresh_fails(tmp_path, monkeypatch):
    """Once the queue marker exists the run executes, so a later failure must
    not lose its id (Devin review): the refresh comes before any upload."""
    (tmp_path / "ci.yaml").write_text("steps: []\n")
    calls: list[tuple[str, ...]] = []

    def fake_gcloud(*args, stdin=None):
        calls.append(args)
        if args[:3] == ("compute", "instances", "add-metadata"):
            raise RuntimeError("metadata refresh refused")
        return "TERMINATED\n"

    monkeypatch.setenv("UA_CI_GCP_PROJECT", "fixture-project")
    monkeypatch.setattr(spot_ci, "gcloud", fake_gcloud)
    monkeypatch.setattr(spot_ci, "commit_file", lambda sha, name: b"")
    with pytest.raises(RuntimeError, match="metadata"):
        spot_ci.submit(tmp_path, "a" * 40)
    assert not any(c[0] == "storage" for c in calls)


def test_submit_uploads_runner_scripts_from_the_tested_commit(tmp_path, monkeypatch):
    """A stale checkout must not downgrade the VM: both files come from the SHA."""
    (tmp_path / "ci.yaml").write_text("steps: []\n")
    sent: dict[str, bytes] = {}

    def fake_gcloud(*args, stdin=None):
        for arg in args:
            if arg.startswith("--metadata-from-file=startup-script="):
                sent["startup.sh"] = Path(arg.split("=", 2)[2]).read_bytes()
            if arg.endswith("executor.py"):
                sent["executor.py"] = Path(arg).read_bytes()
        return "RUNNING\n"

    monkeypatch.setenv("UA_CI_GCP_PROJECT", "fixture-project")
    monkeypatch.setattr(spot_ci, "gcloud", fake_gcloud)
    monkeypatch.setattr(spot_ci, "commit_file", lambda sha, name: f"{name}@{sha}".encode())
    spot_ci.submit(tmp_path, "b" * 40)
    assert sent == {
        "startup.sh": b"startup.sh@" + b"b" * 40,
        "executor.py": b"executor.py@" + b"b" * 40,
    }


def test_commit_file_reads_the_commit_and_refuses_an_unknown_one():
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    committed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{head}:deploy/spot-ci/startup.sh"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    assert spot_ci.commit_file(head, "startup.sh") == committed
    with pytest.raises(RuntimeError, match="not readable"):
        spot_ci.commit_file("0" * 40, "startup.sh")


def test_commit_file_falls_back_for_a_commit_older_than_the_runner():
    """A head that predates deploy/spot-ci gets this checkout's runner, not a refusal."""
    first = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    local = (REPO_ROOT / "deploy" / "spot-ci" / "executor.py").read_bytes()
    assert spot_ci.commit_file(first, "executor.py") == local


def _fail_run(tmp_path: Path, files: dict[str, str], *, cat_fails: bool = False) -> dict:
    """Run startup.sh's fail_run against a directory standing in for the bucket."""
    text = (REPO_ROOT / "deploy" / "spot-ci" / "startup.sh").read_text()
    func = re.search(r"^fail_run\(\) \{.*?^\}\n", text, re.S | re.M).group(0)
    bucket = tmp_path / "bucket"
    run = bucket / "runs" / "spot-run"
    run.mkdir(parents=True)
    for name, body in files.items():
        (run / name).write_text(body)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gcloud"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'args=(); for a in "$@"; do [ "$a" = -q ] || args+=("$a"); done\n'
        'case "${args[1]}" in\n'
        '  cat) [ -z "$FAKE_CAT_FAILS" ] && cat "$FAKE_BUCKET/${args[2]#gs://}" ;;\n'
        '  ls) d="$FAKE_BUCKET/${args[2]#gs://}"; [ -d "$d" ] || exit 1\n'
        '      for f in "$d"*; do [ -e "$f" ] && echo "${args[2]}${f##*/}"; done; exit 0 ;;\n'
        '  cp) cp "${args[2]}" "$FAKE_BUCKET/${args[3]#gs://}" ;;\n'
        "esac\n"
    )
    fake.chmod(0o755)
    script = f'log() {{ :; }}\n{func}\nfail_run "gs://runs/spot-run" "test reason"'
    subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "FAKE_BUCKET": str(bucket / "..") + "/bucket",
            "FAKE_CAT_FAILS": "1" if cat_fails else "",
        },
        check=False,
    )
    return json.loads((run / "state.json").read_text())


def test_fail_run_settles_even_when_state_json_is_unreadable(tmp_path):
    meta = {"createTime": "2026-10-29T00:00:00Z", "config": "ci.yaml", "tags": ["t"]}
    state = _fail_run(tmp_path, {"state.json": "{not json", "meta.json": json.dumps(meta)})
    assert state["status"] == "INTERNAL_ERROR"
    assert state["id"] == "spot-run" and state["steps"] == []
    assert state["createTime"] == meta["createTime"] and state["tags"] == ["t"]
    assert "config" not in state and state["finishTime"]


def test_fail_run_settles_with_neither_state_nor_meta(tmp_path):
    state = _fail_run(tmp_path, {})
    assert state["status"] == "INTERNAL_ERROR" and state["id"] == "spot-run"
    assert state["createTime"] and state["finishTime"]


def test_fail_run_keeps_an_existing_final_result(tmp_path):
    state = _fail_run(tmp_path, {"state.json": json.dumps({"id": "x", "status": "SUCCESS"})})
    assert state["status"] == "SUCCESS"


def test_fail_run_keeps_a_final_result_it_cannot_read(tmp_path):
    """A transient read failure must not overwrite a final result (#2262 review)."""
    final = {"id": "spot-run", "status": "SUCCESS"}
    state = _fail_run(tmp_path, {"state.json": json.dumps(final)}, cat_fails=True)
    assert state == final


def test_provision_checks_cloud_nat_before_creating_anything():
    text = (REPO_ROOT / "deploy" / "spot-ci" / "provision.sh").read_text()
    check = text.index("compute routers nats list")
    assert check < text.index("buckets create") < text.index("instances create")
    assert "Nothing was created" in text


def _run_fits(tmp_path: Path, uptime: str) -> bool:
    """Run startup.sh's run_fits with a fake /proc/uptime."""
    text = (REPO_ROOT / "deploy" / "spot-ci" / "startup.sh").read_text()
    settings = re.search(r"^MAX_RUN_S=.*?^UPTIME_FILE=.*?\n", text, re.S | re.M).group(0)
    func = re.search(r"^run_fits\(\) \{.*?^\}\n", text, re.S | re.M).group(0)
    up = tmp_path / "uptime"
    up.write_text(uptime)
    done = subprocess.run(
        ["bash", "-c", f"{settings}{func}run_fits"],
        env={"PATH": "/usr/bin:/bin", "UA_CI_UPTIME_FILE": str(up)},
        check=False,
    )
    return done.returncode == 0


def test_a_run_starts_only_when_it_fits_before_the_duration_cap(tmp_path):
    """#2613: GCE's 3 h cap counts from VM start, so a late run would be cut off."""
    assert _run_fits(tmp_path, "60.12 30.00\n")
    assert _run_fits(tmp_path, "899.00 1.00\n")
    # After ~70 min an exhaustive run (~135 min, #2599) would hit the cap.
    assert not _run_fits(tmp_path, "900.00 1.00\n")
    assert not _run_fits(tmp_path, "4200.50 1.00\n")
    assert not _run_fits(tmp_path, "garbage\n")


def test_executor_is_bounded_even_when_its_revision_ignores_the_soft_deadline():
    text = (REPO_ROOT / "deploy" / "spot-ci" / "startup.sh").read_text()
    settings = re.search(r"^MAX_RUN_S=.*?^UPTIME_FILE=.*?\n", text, re.S | re.M).group(0)
    run = re.search(r"^run_executor\(\) \{.*?^\}\n", text, re.S | re.M).group(0)
    result = subprocess.run(
        [
            "bash",
            "-c",
            settings
            + run
            + 'timeout() { printf "%s\\n" "$UA_SPOT_BUILD_TIMEOUT_S" "$@"; }; run_executor test-bucket test-run',
        ],
        env={"PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [
        "9480",
        "--kill-after=15s",
        "9600s",
        "python3",
        "-I",
        "/opt/ua-ci-executor.py",
        "test-bucket",
        "test-run",
    ]


def test_the_drain_loop_checks_the_budget_before_spending_an_attempt():
    text = (REPO_ROOT / "deploy" / "spot-ci" / "startup.sh").read_text()
    loop = text[text.index("while :; do") :]
    assert loop.index("run_fits") < loop.index('attempts="$(gcloud storage cat')
    provision = (REPO_ROOT / "deploy" / "spot-ci" / "provision.sh").read_text()
    assert "--max-run-duration=3h" in provision  # keep MAX_RUN_S in step
    assert loop.index("executor staging used") < loop.index("echo $((attempts + 1))")
