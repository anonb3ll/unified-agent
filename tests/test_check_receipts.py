"""Check receipts route a merge to reuse, targeted or full (task #2412).

Ground truth:
- reuse needs the newest terminal full run on this exact tree and environment
  to have passed, recently;
- a changed tree or environment never reuses;
- a cancelled, skipped or partial run is evidence of nothing;
- a newer failure vetoes an older pass;
- unknown is full.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unified_agent import check_receipts as cr

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gate_route.py"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
TREE = "a" * 40
ENV = "e" * 16
SELECTED = ["tests/test_docs.py"]


def _receipt(**overrides: str) -> cr.Receipt:
    fields = {
        "check": "ci:full-verification",
        "conclusion": cr.SUCCESS,
        "tree": TREE,
        "env": ENV,
        "scope": cr.SCOPE_FULL,
        "ref": "run-1",
        "finished_at": (NOW - timedelta(hours=1)).isoformat(),
    }
    return cr.Receipt(**{**fields, **overrides})


def _at(hours: int) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


@pytest.mark.parametrize(
    ("receipts", "selection", "expected"),
    [
        pytest.param([_receipt()], None, cr.REUSE, id="green-full-same-tree-env"),
        pytest.param([_receipt(tree="b" * 40)], None, cr.FULL, id="changed-tree"),
        pytest.param([_receipt(env="f" * 16)], None, cr.FULL, id="changed-env"),
        pytest.param([_receipt(conclusion="cancelled")], None, cr.FULL, id="cancelled"),
        pytest.param([_receipt(conclusion="skipped")], None, cr.FULL, id="skipped"),
        pytest.param([_receipt(scope=cr.SCOPE_PARTIAL)], None, cr.FULL, id="partial-success"),
        pytest.param([_receipt(finished_at="yesterday")], None, cr.FULL, id="undated"),
        pytest.param([_receipt(finished_at=_at(24 * 8))], None, cr.FULL, id="too-old-full-needed"),
        pytest.param([], SELECTED, cr.TARGETED, id="no-proof-certified-subset"),
        pytest.param(
            [_receipt(finished_at=_at(24 * 8))], SELECTED, cr.TARGETED, id="too-old-subset"
        ),
        pytest.param([_receipt(conclusion="cancelled")], SELECTED, cr.TARGETED, id="cancel-subset"),
        pytest.param([], [], cr.FULL, id="empty-selection-is-unknown"),
        pytest.param([], None, cr.FULL, id="no-proof-no-subset"),
        pytest.param(
            [_receipt(conclusion=cr.FAILURE, finished_at=_at(1)), _receipt(finished_at=_at(2))],
            SELECTED,
            cr.FULL,
            id="newer-failure-vetoes-pass-and-subset",
        ),
        pytest.param(
            [_receipt(conclusion=cr.FAILURE, finished_at=_at(2)), _receipt(finished_at=_at(1))],
            None,
            cr.REUSE,
            id="newer-pass-after-failure",
        ),
        pytest.param(
            [_receipt(conclusion=cr.FAILURE, finished_at=_at(1)), _receipt(finished_at=_at(1))],
            None,
            cr.FULL,
            id="tie-goes-to-failure",
        ),
    ],
)
def test_route_table(receipts, selection, expected):
    route = cr.classify(TREE, ENV, receipts, selection=selection, now=NOW)
    assert route.route == expected, route.reason


@pytest.mark.parametrize(("tree", "env"), [("", ENV), (TREE, "")])
def test_unknown_tree_or_env_is_full_even_with_a_matching_receipt(tree, env):
    route = cr.classify(tree, env, [_receipt(tree=tree, env=env)], selection=SELECTED, now=NOW)
    assert route.route == cr.FULL


def test_reuse_cites_the_run_and_cancelled_runs_are_counted_not_used():
    receipts = [_receipt(ref="run-pass"), _receipt(conclusion="cancelled", ref="run-cancel")]
    route = cr.classify(TREE, ENV, receipts, selection=None, now=NOW)
    assert (route.route, route.proof, route.ignored) == (cr.REUSE, "run-pass", 1)


def _jobs(**overrides: str) -> list[dict]:
    names = [cr.VERIFICATION_JOB, *cr.SHARD_JOBS]
    return [{"name": n, "conclusion": overrides.get(n, cr.SUCCESS)} for n in names]


def _run(**overrides: str) -> dict:
    return {
        "event": "workflow_dispatch",
        "conclusion": cr.SUCCESS,
        "url": "https://ci/1",
        "updatedAt": NOW.isoformat(),
        **overrides,
    }


@pytest.mark.parametrize(
    ("run", "jobs", "scope"),
    [
        pytest.param(_run(), _jobs(), cr.SCOPE_FULL, id="dispatch-all-green"),
        pytest.param(_run(event="schedule"), _jobs(), cr.SCOPE_FULL, id="nightly-all-green"),
        pytest.param(_run(event="pull_request"), _jobs(), cr.SCOPE_PARTIAL, id="pr-gates"),
        pytest.param(_run(event="merge_group"), _jobs(), cr.SCOPE_PARTIAL, id="queue-subset"),
        pytest.param(
            _run(), _jobs(**{"test (shard=2)": "skipped"}), cr.SCOPE_PARTIAL, id="skipped-shard"
        ),
        pytest.param(
            _run(), _jobs(**{cr.VERIFICATION_JOB: "skipped"}), cr.SCOPE_PARTIAL, id="no-verify-job"
        ),
        pytest.param(
            _run(conclusion=cr.FAILURE), _jobs(), cr.SCOPE_FULL, id="failed-full-can-veto"
        ),
        pytest.param(
            _run(event="pull_request", conclusion=cr.FAILURE),
            _jobs(),
            cr.SCOPE_PARTIAL,
            id="failed-pr-gates-cannot-veto",
        ),
    ],
)
def test_only_a_full_event_with_every_shard_green_is_full_scope(run, jobs, scope):
    assert cr.receipt_from_run(run, jobs, TREE, ENV).scope == scope


def test_shard_jobs_match_the_ci_classifier():
    spec = importlib.util.spec_from_file_location(
        "ci_changes_t", REPO_ROOT / "scripts/ci_changes.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert cr.SHARD_JOBS == tuple(f"test (shard={n})" for n in module.FULL_SHARDS)


def test_env_fingerprint_changes_with_a_fact_and_is_empty_when_a_fact_is_missing():
    assert cr.ci_env(["ubuntu-latest"]) == cr.ci_env(["ubuntu-latest", "ubuntu-latest"])
    assert cr.ci_env(["ubuntu-latest"]) != cr.ci_env(["ua-ci"])
    assert cr.ci_env([]) == ""
    assert cr.env_fingerprint({"backend": "local", "python": ""}) == ""


def test_store_keeps_good_lines_and_skips_damaged_ones(tmp_path):
    path = tmp_path / "git" / cr.STORE_NAME
    assert cr.read_receipts(path) == []
    cr.append_receipt(path, _receipt())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"check": "half"}\nnot json\n')
    cr.append_receipt(path, _receipt(ref="run-2"))
    assert [r.ref for r in cr.read_receipts(path)] == ["run-1", "run-2"]


# The real CLI over a real git repository and a fake gh.
def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "one")
    return root


def _fake_gh(tmp_path: Path, runs: list[dict], jobs: list[dict], runner_rc: int = 1) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (tmp_path / "runs.json").write_text(json.dumps(runs))
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    script = fake_bin / "gh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f"  run) cat {tmp_path}/runs.json ;;\n"
        f"  api) cat {tmp_path}/jobs.json ;;\n"
        f'  variable) echo "variable not found" >&2; exit {runner_rc} ;;\n'
        "esac\n"
    )
    script.chmod(0o755)
    return fake_bin


def _cli(repo: Path, fake_bin: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=repo, env=env, capture_output=True, text=True
    )


def _labelled_jobs(label: str) -> list[dict]:
    return [{**job, "labels": [label]} for job in _jobs()]


def _route_json(repo: Path, fake_bin: Path) -> dict:
    done = _cli(
        repo, fake_bin, "route", "--repo", "o/r", "--head", "HEAD", "--base", "HEAD", "--json"
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_cli_reuses_a_green_dispatch_on_the_same_tree_from_a_different_commit(repo, tmp_path):
    tested = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "same tree, new commit")
    now = datetime.now(UTC).isoformat()
    runs = [
        {
            "databaseId": 7,
            "headSha": tested,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": cr.SUCCESS,
            "updatedAt": now,
            "url": "https://ci/7",
        }
    ]
    out = _route_json(repo, _fake_gh(tmp_path, runs, _labelled_jobs("ubuntu-latest")))
    assert (out["route"], out["proof"]) == (cr.REUSE, "https://ci/7")


def test_cli_does_not_reuse_across_a_changed_runner_or_a_cancelled_run(repo, tmp_path):
    head = _git(repo, "rev-parse", "HEAD")
    now = datetime.now(UTC).isoformat()
    run = {
        "databaseId": 7,
        "headSha": head,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": cr.SUCCESS,
        "updatedAt": now,
        "url": "https://ci/7",
    }
    other = _route_json(repo, _fake_gh(tmp_path, [run], _labelled_jobs("self-hosted-gpu")))
    assert other["route"] == cr.FULL
    (tmp_path / "runs.json").write_text(json.dumps([{**run, "conclusion": "cancelled"}]))
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": _labelled_jobs("ubuntu-latest")}))
    cancelled = _route_json(repo, tmp_path / "bin")
    assert (cancelled["route"], cancelled["ignored"]) == (cr.FULL, 1)


def test_cli_reports_full_when_ci_cannot_be_read(repo, tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (fake_bin / "gh").chmod(0o755)
    out = _route_json(repo, fake_bin)
    assert out["route"] == cr.FULL
    assert "could not be read" in out["reason"]


def test_cli_exit_2_for_an_unreadable_ref(repo, tmp_path):
    done = _cli(repo, _fake_gh(tmp_path, [], []), "route", "--repo", "o/r", "--head", "nope")
    assert done.returncode == 2


def test_record_refuses_a_dirty_tree_or_a_moved_head_then_local_route_reuses(repo, tmp_path):
    fake_bin = _fake_gh(tmp_path, [], [])
    head = _git(repo, "rev-parse", "HEAD")
    record = ("record", "--check", "pytest", "--conclusion", "success", "--scope", "full")
    route = (
        "route",
        "--repo",
        "o/r",
        "--head",
        "HEAD",
        "--base",
        "HEAD",
        "--env",
        "local",
        "--json",
    )
    store = repo / ".git" / cr.STORE_NAME

    (repo / "a.txt").write_text("dirty\n")
    assert _cli(repo, fake_bin, *record, "--head", head).returncode == 0
    assert not store.exists()
    _git(repo, "checkout", "-q", "--", "a.txt")

    assert _cli(repo, fake_bin, *record, "--head", "f" * 40).returncode == 0
    assert not store.exists()

    assert _cli(repo, fake_bin, *record, "--head", head).returncode == 0
    assert json.loads(_cli(repo, fake_bin, *route).stdout)["route"] == cr.REUSE
