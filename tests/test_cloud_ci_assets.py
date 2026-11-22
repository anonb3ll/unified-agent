"""Static checks for the Cloud Build CI path (task #2115). No GCP calls."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
CONFIG = REPO_ROOT / "deploy" / "cloudbuild" / "ci.yaml"
STEP_SCRIPT = REPO_ROOT / "deploy" / "cloudbuild" / "ci-step.sh"
RUNNER = REPO_ROOT / "scripts" / "cloud-ci.sh"


def _steps() -> dict[str, dict]:
    data = yaml.safe_load(CONFIG.read_text())
    return {step["id"]: step for step in data["steps"]}


def test_scripts_are_executable():
    assert os.access(STEP_SCRIPT, os.X_OK)
    assert os.access(RUNNER, os.X_OK)


def test_every_reported_step_exists_and_waits_for_setup():
    steps = _steps()
    for sid in ("gates", "shard-0", "shard-1", "shard-2", "shard-3", "js", "native"):
        assert sid in steps, sid
        assert steps[sid].get("waitFor") == ["setup"], sid


def test_runner_maps_every_review_gate_context():
    """The contexts the review gate reads (config.DEFAULT_REVIEW_CI_REQUIRED_CHECKS,
    ci_changes.required_jobs) must each be posted from a real step."""
    text = RUNNER.read_text()
    assert 'map "CI / gates" gates' in text
    assert 'map "CI / test (shard=$i)" "shard-$i"' in text
    assert 'map "CI / js" js' in text
    assert '"CI / full-verification"' in text


def test_python_steps_run_privileged_for_bubblewrap():
    for sid in ("setup", "gates", "shard-0", "shard-1", "shard-2", "shard-3"):
        args = " ".join(_steps()[sid]["args"])
        assert "--privileged" in args, sid
        assert "--init" in args, sid


def test_checks_drop_root_before_running():
    text = STEP_SCRIPT.read_text()
    root_block = text.index('if [ "$(id -u)" = "0" ]')
    drop = text.index("exec runuser", root_block)
    first_check = text.index("uv run --frozen ruff check")
    assert drop < first_check


def test_no_github_credential_enters_the_build():
    text = CONFIG.read_text() + STEP_SCRIPT.read_text()
    assert not re.search(r"GH_TOKEN|GITHUB_TOKEN|secretEnv|availableSecrets", text)


def test_aggregate_rejects_failed_and_missing_results(tmp_path):
    """allowFailure must collect diagnostics without certifying a red build."""
    aggregate = _steps()["full-verification"]
    assert not aggregate.get("allowFailure", False)
    jobs = {"gates", "shard-0", "shard-1", "shard-2", "shard-3", "js", "native"}
    assert set(aggregate["waitFor"]) == jobs
    script = aggregate["args"][-1].replace("$$", "$").replace("/workspace/.ci", str(tmp_path))
    for job in jobs:
        (tmp_path / f"{job}.exit").write_text("0\n")
    assert subprocess.run(["bash", "-c", script]).returncode == 0
    (tmp_path / "shard-2.exit").write_text("123\n")
    assert subprocess.run(["bash", "-c", script]).returncode != 0
    (tmp_path / "shard-2.exit").unlink()
    assert subprocess.run(["bash", "-c", script]).returncode != 0


def test_js_and_native_checks_cover_enabled_workflow_gates():
    script = STEP_SCRIPT.read_text()
    # One AND-list: a failed web unit run or build must not be masked by the
    # exit status of a later Playwright line in the same subshell.
    assert re.search(r"npm run build \\\n\s*&& npx playwright install chromium", script)
    for command in (
        "npm run test:a11y",
        "npx playwright install chromium",
        "cargo fmt",
        "cargo clippy",
        "cargo test",
    ):
        assert command in script
    runner = RUNNER.read_text()
    assert 'ci_changes.py" classify' in runner
    assert 'map "CI / desktop-core" native' in runner


def test_check_wrappers_preserve_container_exit_status(tmp_path):
    """Run the real generated wrapper with a failed fake Docker command."""
    step = _steps()["shard-1"]
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 23\n")
    docker.chmod(0o755)
    script = step["args"][-1].replace("$$", "$").replace("/workspace/.ci", str(tmp_path))
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(["bash", "-c", script], env=env)
    assert result.returncode == 23
    assert (tmp_path / "shard-1.exit").read_text().strip() == "23"


def test_parallel_checks_have_private_git_and_readonly_python():
    for sid in ("gates", "shard-0", "shard-1", "shard-2", "shard-3", "js", "native"):
        script = _steps()[sid]["args"][-1]
        assert f"/workspace/.ci/checkouts/{sid}:/workspace/repo" in script
        assert "/workspace/repo/.venv:/workspace/repo/.venv:ro" in script


def test_build_classification_cannot_be_switched_off_by_substitutions():
    data = yaml.safe_load(CONFIG.read_text())
    assert "_JS" not in data["substitutions"]
    assert "_NATIVE" not in data["substitutions"]
    setup = STEP_SCRIPT.read_text()
    assert "git diff --name-only refs/ci/base HEAD" in setup
    assert "exact source SHA required" in setup
    for job in ("js", "native"):
        assert f"grep -qx '{job}=false' /workspace/.ci/classification" in _steps()[job]["args"][-1]


@pytest.mark.parametrize(
    "changed,tested_packages",
    [
        ("packages/web/src/App.tsx", {"web"}),
        ("packages/client/src/client.ts", {"protocol", "client", "web"}),
        ("uv.lock", {"protocol", "client", "web"}),
        ("", {"protocol", "client", "web"}),
    ],
)
def test_cloud_js_executes_dependency_selected_checks(tmp_path, changed, tested_packages):
    """Run the actual JS shell with local command stubs, never Docker or GCP."""
    for package in ("protocol", "client", "web"):
        (tmp_path / "packages" / package).mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts/ci_changes.py", tmp_path / "scripts/ci_changes.py")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "commands"
    for command in ("npm", "npx", "uv"):
        stub = binaries / command
        stub.write_text(
            '#!/bin/sh\nprintf "%s|%s|%s\\n" "${0##*/}" "$PWD" "$*" >> "$COMMAND_LOG"\n'
        )
        stub.chmod(0o755)
    git = binaries / "git"
    git.write_text('#!/bin/sh\n[ "${FAIL_DIFF:-0}" = 0 ] || exit 1\nprintf "%s\\n" "$CHANGED"\n')
    git.chmod(0o755)
    body = STEP_SCRIPT.read_text().split("\njs)\n", 1)[1].split("\n    ;;", 1)[0]
    env = os.environ | {
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
        "CHANGED": changed,
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", "ensure_uv() { :; }\n" + body],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    commands = [line.split("|", 2) for line in log.read_text().splitlines()]
    assert {
        Path(cwd).name for command, cwd, args in commands if command == "npm" and args == "test"
    } == tested_packages
    assert {
        Path(cwd).name
        for command, cwd, args in commands
        if command == "npm" and args == "run build"
    } == {"protocol", "client", "web"}
    log.unlink()
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", "ensure_uv() { :; }\n" + body],
        cwd=tmp_path,
        env=env | {"FAIL_DIFF": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not log.exists(), "a failed diff must stop before executing selected checks"


@pytest.mark.parametrize("explicit_repo", [True, False])
def test_submit_preserves_exact_git_snapshot_without_local_state(tmp_path, explicit_repo):
    """Exercise real Git packaging; the stub replaces only the billed service."""
    repo = tmp_path / "source"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q", "--initial-branch=main", "--template=")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@example.invalid")
    (repo / "scripts").mkdir()
    (repo / "deploy/cloudbuild").mkdir(parents=True)
    shutil.copy2(RUNNER, repo / "scripts/cloud-ci.sh")
    shutil.copy2(REPO_ROOT / "scripts/ci_changes.py", repo / "scripts/ci_changes.py")
    shutil.copy2(CONFIG, repo / "deploy/cloudbuild/ci.yaml")
    shutil.copy2(STEP_SCRIPT, repo / "deploy/cloudbuild/ci-step.sh")
    git("add", ".")
    git("commit", "-qm", "source")
    git("remote", "add", "origin", str(repo))
    sha = git("rev-parse", "HEAD")
    (repo / "private-local-only").write_text("not part of the commit")
    (repo / ".git/hooks").mkdir()
    (repo / ".git/hooks/local-hook").write_text("local configuration")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'test "$*" = "repo view --json nameWithOwner -q .nameWithOwner" || exit 91\n'
        "echo acme/platform\n"
    )
    if explicit_repo:
        # An explicit target must not discover a different repository.
        gh.write_text("#!/bin/sh\nexit 91\n")
    gh.chmod(0o755)
    gcloud = binaries / "gcloud"
    gcloud.write_text(
        "#!/usr/bin/python3\n"
        "import pathlib,subprocess,sys,json\n"
        "if sys.argv[1:3] == ['builds','submit']:\n"
        " assert '--project=fixture-ci-project' in sys.argv\n"
        " p=pathlib.Path(sys.argv[3]); r=p/'repo'\n"
        f" assert subprocess.check_output(['git','-C',str(r),'rev-parse','HEAD'],text=True).strip()=={sha!r}\n"
        " assert (r/'.git/shallow').is_file()\n"
        f" assert subprocess.check_output(['git','-C',str(r),'rev-parse','refs/ci/base'],text=True).strip()=={sha!r}\n"
        " assert not (r/'private-local-only').exists()\n"
        " assert not (r/'.git/hooks/local-hook').exists()\n"
        " assert (p/'.gcloudignore').is_file()\n"
        " print('fixture-build')\n"
        "elif '--format=json' in sys.argv:\n"
        " print(json.dumps({'status':'SUCCESS','steps':[{'id':s,'status':'SUCCESS'} for s in "
        "['gates','shard-0','shard-1','shard-2','shard-3','js','native','full-verification']]}))\n"
        "else: print('SUCCESS')\n"
    )
    gcloud.chmod(0o755)
    runner_env = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "UA_CI_NO_STATUS": "1",
        "UA_CI_GCP_PROJECT": "fixture-ci-project",
        "UA_CI_BACKEND": "cloudbuild",
    }
    runner_env.pop("UA_CI_REPO", None)
    if explicit_repo:
        runner_env["UA_CI_REPO"] = "acme/platform"
    result = subprocess.run(
        ["bash", "scripts/cloud-ci.sh", sha],
        cwd=repo,
        capture_output=True,
        text=True,
        env=runner_env,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CI / full-verification     SUCCESS" in result.stdout


def test_missing_project_refuses_before_submitting_a_billed_build(tmp_path):
    runner_env = {k: v for k, v in os.environ.items() if k != "UA_CI_GCP_PROJECT"}
    result = subprocess.run(
        ["bash", str(RUNNER), "HEAD"],
        cwd=tmp_path,
        env=runner_env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "set UA_CI_GCP_PROJECT" in result.stderr
    assert "build " not in result.stdout


def test_test_budget_runs_on_pull_request_runs_only():
    """Task #2233: only a PR run uploads the budget's base ref and commit messages."""
    step = STEP_SCRIPT.read_text()
    gates = step[step.index("gates)") : step.index("shard)")]
    assert "if [ -f /workspace/commit-messages ] && [ -f scripts/test_budget.py ]; then" in gates
    assert "python3 -I scripts/test_budget.py count --base refs/ci/budget-base --head HEAD" in gates
    assert "--messages /workspace/commit-messages --messages-format nul" in gates
    assert "test budget: skipped" in gates
    runner = RUNNER.read_text()
    assert "--json baseRefName,baseRefOid" in runner
    assert 'git -C "$root" log -z --format=%B "$budget_base..$sha" > "$src/commit-messages"' in (
        runner
    )


def test_stacked_pr_run_counts_from_its_own_base_branch(tmp_path):
    """Review finding: a stacked child was measured from main and passed on its
    parent's trailer. The upload now carries the merge base with the PR's base
    branch and only the child's commit messages."""
    repo = tmp_path / "source"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q", "--initial-branch=main", "--template=")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@example.invalid")
    (repo / "scripts").mkdir()
    (repo / "deploy/cloudbuild").mkdir(parents=True)
    shutil.copy2(RUNNER, repo / "scripts/cloud-ci.sh")
    shutil.copy2(REPO_ROOT / "scripts/ci_changes.py", repo / "scripts/ci_changes.py")
    shutil.copy2(CONFIG, repo / "deploy/cloudbuild/ci.yaml")
    shutil.copy2(STEP_SCRIPT, repo / "deploy/cloudbuild/ci-step.sh")
    git("add", ".")
    git("commit", "-qm", "source")
    main = git("rev-parse", "HEAD")
    git("checkout", "-qb", "parent")
    git("commit", "-q", "--allow-empty", "-m", "test: parent\n\nTest-Budget: parent reason")
    parent = git("rev-parse", "HEAD")
    git("checkout", "-qb", "child")
    git("commit", "-q", "--allow-empty", "-m", "test: child")
    child = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    git("remote", "add", "origin", str(repo))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  "pr view 7 -R acme/platform --json headRefOid -q .headRefOid") echo {child} ;;\n'
        f'  "pr view 7 -R acme/platform --json baseRefName,baseRefOid "*) echo parent {parent} ;;\n'
        "  *) exit 91 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    gcloud = binaries / "gcloud"
    gcloud.write_text(
        "#!/usr/bin/python3\n"
        "import pathlib,subprocess,sys,json\n"
        "if sys.argv[1:3] == ['builds','submit']:\n"
        " p=pathlib.Path(sys.argv[3]); r=p/'repo'\n"
        " rev=lambda ref: subprocess.check_output(['git','-C',str(r),'rev-parse',ref],text=True).strip()\n"
        f" assert rev('HEAD')=={child!r} and rev('refs/ci/budget-base')=={parent!r}\n"
        f" assert rev('refs/ci/base')=={main!r}\n"
        " assert (p/'commit-messages').read_bytes()==b'test: child\\n\\x00', (p/'commit-messages').read_bytes()\n"
        " print('fixture-build')\n"
        "elif '--format=json' in sys.argv:\n"
        " print(json.dumps({'status':'SUCCESS','steps':[{'id':s,'status':'SUCCESS'} for s in "
        "['gates','shard-0','shard-1','shard-2','shard-3','js','native','full-verification']]}))\n"
        "else: print('SUCCESS')\n"
    )
    gcloud.chmod(0o755)
    runner_env = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "UA_CI_NO_STATUS": "1",
        "UA_CI_GCP_PROJECT": "fixture-ci-project",
        "UA_CI_BACKEND": "cloudbuild",
        "UA_CI_REPO": "acme/platform",
    }
    result = subprocess.run(
        ["bash", "scripts/cloud-ci.sh", "7"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=runner_env,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CI / full-verification     SUCCESS" in result.stdout


def _yaml_seconds(value: str) -> int:
    assert re.fullmatch(r"\d+s", value), value
    return int(value[:-1])


def _runner_default(name: str) -> int:
    match = re.search(rf"^{name}=(\d+)$", RUNNER.read_text(), re.MULTILINE)
    assert match, f"{name} default not found in {RUNNER.name}"
    return int(match.group(1))


def test_watcher_deadlines_outlast_the_build_limits():
    """A deadline shorter than the build it waits for makes the watcher give up on
    a build that would pass (#2599: a 90-minute limit under a ~2-hour exhaustive
    shard; #2600: a queue bound no longer than one whole build ahead of it)."""
    data = yaml.safe_load(CONFIG.read_text())
    timeout = _yaml_seconds(data["timeout"])
    queue_ttl = _yaml_seconds(data["queueTtl"])
    run_allow = _runner_default("run_allow")
    queue_allow = _runner_default("queue_allow")
    slack = 600
    # The run deadline starts when the build leaves the queue.
    assert run_allow >= timeout + slack
    # Builds run one at a time, so a queued build can wait out one whole build.
    assert queue_ttl >= timeout + slack
    # The watcher checks its deadline right after a describe; leave it a poll
    # margin past the queue expiry so a build that just started is not missed.
    assert queue_allow >= queue_ttl + slack
