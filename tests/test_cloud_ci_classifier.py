"""Run the real cloud runner against controlled Git trees, never cloud APIs (#2117)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER = (ROOT / "scripts/ci_changes.py").read_text()


# When each fixture build was created: generation order, which decides which
# run is newer, whatever order their statuses were posted in (review r2). The
# older build's time sorts after this run's as text ("." < "Z"), so only a real
# time comparison orders them correctly.
CREATE_TIMES = {
    "older-build": "2026-10-26T10:00:00Z",
    "fixture-build": "2026-10-26T10:00:00.5Z",
    "newer-build": "2026-10-26T10:00:00.500000001Z",
}


# A fixture watcher run takes about 15 s, and twice that on a contended CI
# shard; a tighter bound turns load into a false failure.
WATCHER_RUN_TIMEOUT_S = 120


def create_time_reply() -> str:
    """Stub lines answering `builds describe <id> --format=value(createTime)`."""
    return (
        " if '--format=value(createTime)' in sys.argv:\n"
        f"  times={{**{CREATE_TIMES!r}, **json.loads(os.environ.get('FIXTURE_CREATE_TIMES','{{}}'))}}\n"
        "  if sys.argv[3] not in times: sys.exit(1)\n"
        "  print(times[sys.argv[3]]); sys.exit(0)\n"
    )


def classifier(js: bool, native: bool) -> str:
    """Change real classification rules, leaving its CLI and path handling intact."""
    source = CLASSIFIER
    if js:
        source = source.replace('r"^(src/|tests/|scripts/|Makefile$)"', 'r"^never/"')
    if native:
        source = source.replace('r"^native/UnifiedAgentDesktop/"', 'r"^src/"')
    return source


@pytest.fixture
def cloud_fixture(tmp_path):
    repo = tmp_path / "operator"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q", "--initial-branch=main", "--template=")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@example.invalid")
    for name in (
        "scripts/cloud-ci.sh",
        "scripts/ci_changes.py",
        "deploy/cloudbuild/ci-step.sh",
        "deploy/cloudbuild/ci.yaml",
    ):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    (repo / "src").mkdir()
    (repo / "src/change.py").write_text("# base\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("remote", "add", "origin", str(repo))
    base = git("rev-parse", "HEAD")
    # A genuinely different base and source; the runner fetches origin/main.
    git("checkout", "-qb", "tested")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    git_stub = binaries / "git"
    git_stub.write_text(
        f"#!{sys.executable}\n"
        "import os,pathlib,subprocess,sys\n"
        "if sys.argv[1:2] == ['diff'] and os.environ.get('FIXTURE_DIFF_FAIL'): sys.exit(23)\n"
        "if 'checkout' in sys.argv and os.environ.get('FIXTURE_CHECKOUT_CLASSIFIER'):\n"
        f" result=subprocess.run([{shutil.which('git')!r}, *sys.argv[1:]])\n"
        " if result.returncode: sys.exit(result.returncode)\n"
        " path=pathlib.Path(sys.argv[sys.argv.index('-C')+1])/'scripts/ci_changes.py'\n"
        " path.unlink(missing_ok=True)\n"
        " path.write_text(os.environ['FIXTURE_CHECKOUT_CLASSIFIER'])\n"
        " sys.exit(0)\n"
        f"os.execv({shutil.which('git')!r}, ['git', *sys.argv[1:]])\n"
    )
    git_stub.chmod(0o755)
    events = tmp_path / "events.jsonl"
    successful_posts = tmp_path / "successful-posts.jsonl"
    observed = tmp_path / "observed.json"
    gh = binaries / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        f"with open({str(events)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        # The status listing replays FIXTURE_OLDER_STATUSES, this run's own
        # successful posts, and FIXTURE_NEWER_STATUSES (`context<TAB>
        # description` lines) a run that started meanwhile wrote, newest first
        # with rising ids (#2211). The newer ones show from listing number
        # FIXTURE_NEWER_FROM_LISTING (default 3: after the pre-submit baseline
        # and the pre-pending check), after this run's posts unless
        # FIXTURE_NEWER_POSITION=before-own puts them first (review round 4).
        "if sys.argv[1:3] == ['api', '--paginate']:\n"
        f" posted=pathlib.Path({str(successful_posts)!r})\n"
        # Older statuses predate this run; newer ones appear from the second
        # listing on, after the runner's pre-submit baseline (#2211 round 3).
        f" listed=pathlib.Path({str(tmp_path / 'listed')!r})\n"
        " n=int(listed.read_text() or 0)+1 if listed.exists() else 1\n"
        " listed.write_text(str(n))\n"
        " if str(n) == os.environ.get('FIXTURE_LIST_FAIL_AT'): sys.exit(23)\n"
        " later=n >= int(os.environ.get('FIXTURE_NEWER_FROM_LISTING','3'))\n"
        " before=os.environ.get('FIXTURE_NEWER_POSITION') == 'before-own'\n"
        " older=os.environ.get('FIXTURE_OLDER_STATUSES','')\n"
        " rows=[tuple(l.split('\\t',1)) for l in older.splitlines() if l]\n"
        " newer=os.environ.get('FIXTURE_NEWER_STATUSES','') if later else ''\n"
        " newer=[tuple(l.split('\\t',1)) for l in newer.splitlines() if l]\n"
        " own=[]\n"
        " for line in posted.read_text().splitlines() if posted.exists() else []:\n"
        "  kv=dict(a.split('=',1) for a in json.loads(line) if '=' in a)\n"
        "  own.append((kv['context'], kv.get('description','')))\n"
        " rows += (newer + own) if before else (own + newer)\n"
        " for i,(c,d) in reversed(list(enumerate(rows,1))): print(f'{i}\\t{c}\\t{d}')\n"
        " sys.exit(0)\n"
        "assert sys.argv[1:4] == ['api', '-X', 'POST']\n"
        "if os.environ.get('FIXTURE_POST_FAIL') in sys.argv[1:]:\n"
        f" marker=pathlib.Path({str(tmp_path / 'post-failed')!r})\n"
        " first=not marker.exists(); marker.touch()\n"
        " if first or os.environ.get('FIXTURE_POST_ALWAYS_FAIL'): sys.exit(23)\n"
        f"with open({str(successful_posts)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if os.environ.get('FIXTURE_SIGNAL_POST') and 'state=success' in sys.argv:\n"
        f" marker=pathlib.Path({str(tmp_path / 'signalled')!r})\n"
        " if not marker.exists():\n"
        "  import signal\n"
        "  marker.touch()\n"
        f"  watcher=int(pathlib.Path({str(tmp_path / 'observed.watcher')!r}).read_text())\n"
        "  os.kill(watcher,getattr(signal,'SIG'+os.environ.get('SIGNAL_KIND','TERM')))\n"
    )
    gh.chmod(0o755)
    # Exercise the real YAML skip branches. Docker is the only replaced executor.
    docker = binaries / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$FIXTURE_DOCKER_CALLS"\n'
        'exit "${FIXTURE_DOCKER_EXIT:-0}"\n'
    )
    docker.chmod(0o755)
    wrappers = {
        s["id"]: s["args"][-1].replace("$$", "$")
        for s in yaml.safe_load((ROOT / "deploy/cloudbuild/ci.yaml").read_text())["steps"]
        if s["id"] in {"js", "native"}
    }
    setup = (ROOT / "deploy/cloudbuild/ci-step.sh").read_text()
    start = setup.index("    git diff --name-only refs/ci/base HEAD")
    setup_classify = setup[start : setup.index("    ensure_uv", start)]
    gcloud = binaries / "gcloud"
    gcloud.write_text(
        f"#!{sys.executable}\n"
        "import hashlib,json,os,pathlib,signal,subprocess,sys\n"
        f"events=pathlib.Path({str(events)!r}); observed=pathlib.Path({str(observed)!r})\n"
        # Polls run under the runner's bounded() wrapper; signal the watcher itself.
        # The runner re-execs itself, so the watcher and every $(...) subshell
        # it forks share one command line; a bash that forks a subshell for a
        # command substitution would otherwise hand back a pid that exits as
        # soon as this poll returns. Walk up to the topmost such process.
        "def watcher():\n"
        # -ww: without a tty, ps cuts `command` to $COLUMNS (80 on the CI
        # runner), which hid the bounded() wrapper's marker below.
        " ps=lambda f,p: subprocess.check_output(['ps','-ww','-o',f+'=','-p',str(p)],text=True)\n"
        " pid=os.getppid()\n"
        " if 'subprocess.run(sys.argv[1:]' in ps('command',pid): pid=int(ps('ppid',pid))\n"
        " cmd=ps('command',pid)\n"
        " while True:\n"
        "  parent=int(ps('ppid',pid))\n"
        "  if parent<=1 or ps('command',parent)!=cmd: return pid\n"
        "  pid=parent\n"
        "with events.open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if sys.argv[1:3] == ['builds','describe']:\n"
        + create_time_reply()
        + "if sys.argv[1:3] == ['builds','submit']:\n"
        " source=pathlib.Path(sys.argv[3]); repo=source/'repo'; scratch=source/'results'\n"
        " scratch.mkdir()\n"
        f" command={setup_classify!r}.replace('/workspace/.ci', str(scratch))\n"
        " subprocess.run(['bash','-eo','pipefail','-c',command],cwd=repo,check=True)\n"
        " enabled=[]; failed=[]\n"
        f" for job,wrapper in {wrappers!r}.items():\n"
        "  calls=scratch/(job+'.docker'); env={**os.environ,'FIXTURE_DOCKER_CALLS':str(calls)}\n"
        "  env['FIXTURE_DOCKER_EXIT']='23' if os.environ.get('FAIL_STEP')==job else '0'\n"
        "  result=subprocess.run(['bash','-c',wrapper.replace('/workspace/.ci',str(scratch))],\n"
        "                        cwd=repo,env=env,stdout=subprocess.DEVNULL)\n"
        "  if calls.exists(): enabled.append(job)\n"
        "  if result.returncode: failed.append(job)\n"
        " data={'enabled':enabled,'failed':failed,\n"
        "       'sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),\n"
        "       'base':subprocess.check_output(['git','rev-parse','refs/ci/base'],cwd=repo,text=True).strip(),\n"
        "       'classifier':hashlib.sha256((repo/'scripts/ci_changes.py').read_bytes()).hexdigest()}\n"
        " config=next(a.split('=',1)[1] for a in sys.argv if a.startswith('--config='))\n"
        " import yaml\n"
        " data['tags']=yaml.safe_load(pathlib.Path(config).read_text()).get('tags', [])\n"
        " observed.write_text(json.dumps(data)); print('fixture-build',flush=True)\n"
        " if os.environ.get('FIXTURE_SIGNAL_SUBMIT'):\n"
        "  os.kill(os.getppid(),getattr(signal,'SIG'+os.environ.get('SIGNAL_KIND','TERM')))\n"
        "elif '--format=json' in sys.argv:\n"
        " if os.environ.get('FIXTURE_SIGNAL_JSON'):\n"
        f"  marker=pathlib.Path({str(tmp_path / 'signalled')!r})\n"
        "  if not marker.exists():\n"
        "   marker.touch()\n"
        "   os.kill(int(observed.with_suffix('.watcher').read_text()),\n"
        "           getattr(signal,'SIG'+os.environ.get('SIGNAL_KIND','TERM')))\n"
        " if os.environ.get('FIXTURE_JSON_FAIL'): sys.exit(23)\n"
        " if os.environ.get('FIXTURE_JSON_INVALID'): print('{broken'); sys.exit(0)\n"
        " data=json.loads(observed.read_text())\n"
        " jobs=['gates','shard-0','shard-1','shard-2','shard-3','js','native','full-verification']\n"
        " steps=[{'id':s,'status':'FAILURE' if s in data['failed'] else 'SUCCESS'}\n"
        "        for s in jobs if s!=os.environ.get('MISSING_STEP')]\n"
        " print(json.dumps({'status':os.environ.get('BUILD_STATUS','SUCCESS'),'steps':steps,\n"
        "   'tags':data['tags'],'substitutions':{'_SOURCE_SHA':data['sha']}}))\n"
        "else:\n"
        " observed.with_suffix('.watcher').write_text(str(watcher()))\n"
        " marker=observed.with_suffix('.polled')\n"
        " first=not marker.exists(); marker.touch()\n"
        " if first and os.environ.get('FIXTURE_EDIT_RUNNER'):\n"
        "  pathlib.Path('scripts/cloud-ci.sh').write_text('#!/bin/bash\\nexit 87\\n'+'broken )\\n'*4000)\n"
        "  print('WORKING'); sys.exit(0)\n"
        " npath=observed.with_suffix('.polln')\n"
        " n=int(npath.read_text() or '0')+1 if npath.exists() else 1\n"
        " npath.write_text(str(n))\n"
        " if os.environ.get('FIXTURE_POLL_FAILS') and n<=int(os.environ['FIXTURE_POLL_FAILS']): sys.exit(23)\n"
        " if os.environ.get('FIXTURE_POLL_FAIL'): sys.exit(23)\n"
        " if os.environ.get('FIXTURE_TERM'):\n"
        "  os.kill(watcher(),signal.SIGTERM)\n"
        " print(os.environ.get('BUILD_STATUS','SUCCESS'))\n"
    )
    gcloud.chmod(0o755)
    sleep = binaries / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)

    def run(
        source=CLASSIFIER,
        operator=CLASSIFIER,
        *,
        missing=False,
        symlink=False,
        parent_symlink=None,
        changed_path="src/change.py",
        **env,
    ):
        path = repo / "scripts/ci_changes.py"
        if missing:
            path.unlink()
        elif symlink:
            path.unlink()
            path.symlink_to("outside.py")
            (repo / "scripts/outside.py").write_text(source)
        else:
            path.write_text(source)
        if parent_symlink:
            target = (
                tmp_path / "external-classifier"
                if parent_symlink == "external"
                else repo / "submitted-scripts"
            )
            (repo / "scripts").rename(target)
            (repo / "scripts").symlink_to(
                target if parent_symlink == "external" else target.name,
                target_is_directory=True,
            )
        change = repo / changed_path
        change.parent.mkdir(parents=True, exist_ok=True)
        change.write_text("# submitted change\n")
        git("add", ".")
        git("commit", "-qm", "submitted head")
        sha = git("rev-parse", "HEAD")
        if parent_symlink:
            (repo / "scripts").unlink()
            (repo / "scripts").mkdir()
            shutil.copy2(ROOT / "scripts/cloud-ci.sh", repo / "scripts/cloud-ci.sh")
        if symlink:
            path.unlink()
        path.write_text(operator)
        # The operator is a newer commit, while the argument remains the older
        # source SHA. Its committed classifier may deliberately disagree.
        git("checkout", "-qb", "operator")
        git("add", "scripts" if parent_symlink else "scripts/ci_changes.py")
        git("commit", "--allow-empty", "-qm", "operator classifier")
        assert git("rev-parse", "HEAD") != sha
        result = subprocess.run(
            ["bash", "scripts/cloud-ci.sh", sha],
            cwd=repo,
            env={
                **os.environ,
                "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                "UA_CI_GCP_PROJECT": "fixture-project",
                "UA_CI_BACKEND": "cloudbuild",
                "UA_CI_REPO": "fixture/platform",
                "UA_CI_NO_STATUS": "0",
                **env,
            },
            capture_output=True,
            text=True,
            timeout=WATCHER_RUN_TIMEOUT_S,
        )
        calls = (
            [json.loads(line) for line in events.read_text().splitlines()]
            if events.exists()
            else []
        )
        posts = []
        for line in successful_posts.read_text().splitlines() if successful_posts.exists() else []:
            call = json.loads(line)
            assert call[3] == f"repos/fixture/platform/statuses/{sha}"
            posts.append(dict(arg.split("=", 1) for arg in call if "=" in arg))
        data = json.loads(observed.read_text()) if observed.exists() else None
        if data:
            assert data["sha"] == sha
            assert data["base"] == base
        return result, posts, calls, data

    def reconcile(sha, **env):
        """`--reconcile fixture-build <sha>` after a run: (result, the posts it added)."""
        before = successful_posts.read_text().count("\n") if successful_posts.exists() else 0
        result = subprocess.run(
            ["bash", "scripts/cloud-ci.sh", "--reconcile", "fixture-build", sha],
            cwd=repo,
            env={
                **os.environ,
                "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                "UA_CI_GCP_PROJECT": "fixture-project",
                "UA_CI_BACKEND": "cloudbuild",
                "UA_CI_REPO": "fixture/platform",
                "UA_CI_NO_STATUS": "0",
                **env,
            },
            capture_output=True,
            text=True,
            timeout=WATCHER_RUN_TIMEOUT_S,
        )
        lines = successful_posts.read_text().splitlines() if successful_posts.exists() else []
        added = [dict(a.split("=", 1) for a in json.loads(line) if "=" in a) for line in lines]
        return result, added[before:]

    run.reconcile = reconcile
    return run


@pytest.mark.parametrize(
    "source_js,source_native", [(True, True), (False, False), (True, False), (False, True)]
)
@pytest.mark.parametrize("disagree", [True, False])
def test_contexts_follow_submitted_classifier_and_real_skip_branches(
    cloud_fixture, source_js, source_native, disagree
):
    source = classifier(source_js, source_native)
    operator = classifier(not source_js, not source_native) if disagree else source
    result, posts, _, data = cloud_fixture(source, operator)
    assert result.returncode == 0, result.stdout + result.stderr
    assert data["classifier"] == hashlib.sha256(source.encode()).hexdigest()
    assert set(data["enabled"]) == {
        job for job, enabled in (("js", source_js), ("native", source_native)) if enabled
    }
    contexts = {"CI / gates", "CI / full-verification"}
    contexts.update(f"CI / test (shard={i})" for i in range(4))
    for job, context in (("js", "CI / js"), ("native", "CI / desktop-core")):
        if job in data["enabled"]:
            contexts.add(context)
    assert {p["context"] for p in posts} == contexts
    assert {p["context"] for p in posts if p["state"] == "success"} == contexts


@pytest.mark.parametrize(
    "path,enabled",
    [
        ("docs/ci.md", set()),
        ("packages/web/src/app.ts", {"js"}),
        ("native/UnifiedAgentDesktop/core/src/lib.rs", {"js", "native"}),
    ],
)
def test_real_classifier_path_scopes_keep_all_cloud_python_shards(cloud_fixture, path, enabled):
    result, posts, _, data = cloud_fixture(changed_path=path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(data["enabled"]) == enabled
    assert {f"CI / test (shard={i})" for i in range(4)} <= {
        p["context"] for p in posts if p["state"] == "success"
    }


@pytest.mark.parametrize(
    "source,options",
    [
        (CLASSIFIER, {"missing": True}),
        (CLASSIFIER, {"symlink": True}),
        ("raise SystemExit(7)\n", {}),
        ("print('js=true\\nnative=false')\n", {}),
        (CLASSIFIER.replace('value = "true" if value else "false"', 'value = "yes"'), {}),
        (
            CLASSIFIER.replace(
                "sys.stdout.write(format_outputs(classify(sys.stdin.read().splitlines())))",
                'sys.stdout.write(format_outputs(classify(sys.stdin.read().splitlines())) + "js=false\\n")',
            ),
            {},
        ),
        (CLASSIFIER.replace('f"{key}={value}"', 'f"prefix_{key}={value}"'), {}),
        (CLASSIFIER.replace("json.dumps(value)", '"[false, 1, 2, 3]"'), {}),
        *(
            (
                CLASSIFIER.replace(
                    "sys.stdout.write(format_outputs(classify(sys.stdin.read().splitlines())))",
                    "sys.stdout.write(format_outputs(classify(sys.stdin.read().splitlines()))"
                    f".replace(chr(10), {separator}))",
                ),
                {},
            )
            for separator in ("chr(13) + chr(10)", "chr(8232)")
        ),
    ],
    ids=[
        "missing",
        "symlink",
        "error",
        "old-contract",
        "invalid-flags",
        "duplicate",
        "substring",
        "invalid-shards",
        "crlf",
        "unicode-lines",
    ],
)
def test_invalid_submitted_classifier_refuses_before_submission(cloud_fixture, source, options):
    result, posts, calls, data = cloud_fixture(source, **options)
    assert result.returncode != 0, result.stdout + result.stderr
    assert posts == []
    assert not any(call[:2] == ["builds", "submit"] for call in calls)
    assert data is None


def test_failed_snapshot_diff_refuses_before_submission(cloud_fixture):
    result, posts, calls, data = cloud_fixture(FIXTURE_DIFF_FAIL="1")
    assert result.returncode != 0, result.stdout + result.stderr
    assert posts == []
    assert not any(call[:2] == ["builds", "submit"] for call in calls)
    assert data is None


@pytest.mark.parametrize(
    "env",
    [
        {"MISSING_STEP": "js"},
        {"MISSING_STEP": "shard-2"},
        {"FAIL_STEP": "native"},
        {"MISSING_STEP": "full-verification"},
        {"MISSING_STEP": "gates"},
        *(
            {"BUILD_STATUS": status}
            for status in ("FAILURE", "CANCELLED", "INTERNAL_ERROR", "TIMEOUT", "EXPIRED")
        ),
    ],
)
def test_required_missing_or_failed_job_never_certifies_aggregate(cloud_fixture, env):
    result, posts, _, _ = cloud_fixture(classifier(True, True), **env)
    assert result.returncode != 0, result.stdout + result.stderr
    aggregate = [p for p in posts if p["context"] == "CI / full-verification"]
    assert [p["state"] for p in aggregate] == ["pending", "failure"]
    if "MISSING_STEP" in env:
        context = {
            "js": "CI / js",
            "shard-2": "CI / test (shard=2)",
            "gates": "CI / gates",
            "full-verification": "CI / full-verification",
        }[env["MISSING_STEP"]]
        assert [p["state"] for p in posts if p["context"] == context] == ["pending", "failure"]


@pytest.mark.parametrize("target", ["internal", "external"])
def test_submitted_classifier_parent_symlink_refuses_without_execution(
    cloud_fixture, tmp_path, target
):
    marker = tmp_path / "classifier-executed"
    source = (
        "import sys\nsys.stdin.read()\n"
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "print('python=true\\npython_scope=full\\nshards=[0, 1, 2, 3]"
        "\\njs=true\\nconnectors=false\\nnative=true')\n"
    )
    result, posts, calls, data = cloud_fixture(source, parent_symlink=target)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not marker.exists(), "must reject the ancestor before executing its classifier"
    assert calls == [], "must refuse before any gcloud or gh API stub call"
    assert posts == []
    assert data is None


@pytest.mark.parametrize(
    "options",
    [{}, {"missing": True}, {"symlink": True}],
    ids=["replaced-blob", "untracked-leaf", "symlink-git-mode"],
)
def test_checkout_classifier_must_match_regular_head_blob(cloud_fixture, tmp_path, options):
    marker = tmp_path / "checkout-classifier-executed"
    replacement = (
        "import sys\nsys.stdin.read()\n"
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "print('python=true\\npython_scope=full\\nshards=[0, 1, 2, 3]"
        "\\njs=true\\nconnectors=false\\nnative=true')\n"
    )
    result, posts, calls, data = cloud_fixture(**options, FIXTURE_CHECKOUT_CLASSIFIER=replacement)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not marker.exists(), "unsubmitted checkout bytes must never execute"
    assert calls == []
    assert posts == []
    assert data is None


def test_runner_edit_during_poll_does_not_change_the_running_program(cloud_fixture):
    result, posts, calls, _ = cloud_fixture(FIXTURE_EDIT_RUNNER="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    assert {p["state"] for p in posts if p["context"] == "CI / full-verification"} == {
        "pending",
        "success",
    }


@pytest.mark.parametrize("failure", ["FIXTURE_POLL_FAIL", "FIXTURE_TERM"])
@pytest.mark.parametrize("remote", ["SUCCESS", "FAILURE", "WORKING"])
def test_watcher_failure_reconciles_existing_build_without_resubmitting(
    cloud_fixture, failure, remote
):
    result, posts, calls, _ = cloud_fixture(
        classifier(True, True),
        UA_CI_QUEUE_DEADLINE_S="0",
        UA_CI_RUN_DEADLINE_S="0",
        **{failure: "1", "BUILD_STATUS": remote},
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    latest = {post["context"]: post for post in posts}
    assert latest and all(post["state"] != "pending" for post in latest.values())
    expected = {"SUCCESS": "success", "FAILURE": "failure", "WORKING": "error"}[remote]
    assert latest["CI / full-verification"]["state"] == expected
    assert all("fixture-build" in post["target_url"] for post in latest.values())
    # An unfinished build leaves `error`; the watcher names the re-attach command (#2205).
    hint = "scripts/cloud-ci.sh --reconcile fixture-build "
    assert (hint in result.stderr) == (remote == "WORKING"), result.stderr


@pytest.mark.parametrize("failure", ["FIXTURE_JSON_FAIL", "FIXTURE_JSON_INVALID"])
def test_unreadable_final_build_report_sets_error_instead_of_leaving_pending(
    cloud_fixture, failure
):
    result, posts, calls, _ = cloud_fixture(**{failure: "1"})
    assert result.returncode != 0, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    latest = {post["context"]: post["state"] for post in posts}
    assert latest and set(latest.values()) == {"error"}


@pytest.mark.parametrize("state", ["pending", "success"])
def test_transient_status_write_failure_retries_same_build_without_stranding_contexts(
    cloud_fixture, state
):
    result, posts, calls, _ = cloud_fixture(FIXTURE_POST_FAIL=f"state={state}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    assert set({post["context"]: post["state"] for post in posts}.values()) == {"success"}


def test_persistent_status_failure_attempts_other_contexts_and_returns_failure(cloud_fixture):
    result, posts, calls, _ = cloud_fixture(
        FIXTURE_POST_FAIL="context=CI / gates", FIXTURE_POST_ALWAYS_FAIL="1"
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    latest = {post["context"]: post["state"] for post in posts}
    assert len(latest) == 5 and set(latest.values()) == {"success", "failure"}
    assert latest["CI / full-verification"] == "failure"
    assert "Could not publish CI / gates" in result.stderr


@pytest.mark.parametrize("stage", ["FIXTURE_SIGNAL_JSON", "FIXTURE_SIGNAL_POST"])
@pytest.mark.parametrize("signal_name,exit_code", [("TERM", 143), ("HUP", 129), ("INT", 130)])
def test_signal_during_final_reconciliation_still_settles_all_contexts(
    cloud_fixture, stage, signal_name, exit_code
):
    result, posts, calls, _ = cloud_fixture(
        classifier(True, True), **{stage: "1", "SIGNAL_KIND": signal_name}
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    latest = {post["context"]: post["state"] for post in posts}
    assert len(latest) == 8 and set(latest.values()) == {"success"}


@pytest.mark.parametrize("signal_name,exit_code", [("TERM", 143), ("HUP", 129), ("INT", 130)])
def test_signal_after_known_submit_id_recovers_link_and_preserves_exit(
    cloud_fixture, signal_name, exit_code
):
    result, posts, calls, _ = cloud_fixture(
        classifier(True, True), FIXTURE_SIGNAL_SUBMIT="1", SIGNAL_KIND=signal_name
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    latest = {post["context"]: post for post in posts}
    assert len(latest) == 8 and {post["state"] for post in latest.values()} == {"success"}
    assert all("fixture-build" in post["target_url"] for post in latest.values())


NEWER_RUN = "\n".join(
    f"{c}\tcloud-build newer-build running"
    for c in ["CI / gates", *(f"CI / test (shard={i})" for i in range(4)), "CI / full-verification"]
)


def test_a_fresh_run_never_overwrites_a_newer_run_on_the_same_head(cloud_fixture):
    """Two runs on one SHA (a zombie watcher and a fresh one): the older must not
    write last, or a stale SUCCESS replaces the newer run's result (#2211)."""
    result, posts, calls, _ = cloud_fixture(FIXTURE_NEWER_STATUSES=NEWER_RUN)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    # Only this run's own pending writes went out; nothing settled after the wait.
    assert {p["state"] for p in posts} == {"pending"}
    last_poll = max(i for i, c in enumerate(calls) if "--format=value(status)" in c)
    assert any(c[:2] == ["api", "--paginate"] for c in calls[last_poll:]), "re-read after waiting"


def test_an_interrupted_fresh_run_leaves_a_newer_runs_statuses(cloud_fixture):
    result, posts, calls, _ = cloud_fixture(
        FIXTURE_NEWER_STATUSES=NEWER_RUN,
        FIXTURE_POLL_FAIL="1",
        UA_CI_QUEUE_DEADLINE_S="0",
        UA_CI_RUN_DEADLINE_S="0",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Leaving the statuses" in result.stderr
    assert {p["state"] for p in posts} == {"pending"}
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1


ALL_CONTEXTS = {
    "CI / gates",
    *(f"CI / test (shard={i})" for i in range(4)),
    "CI / js",
    "CI / desktop-core",
    "CI / full-verification",
}
BASE_CONTEXTS = ALL_CONTEXTS - {"CI / js", "CI / desktop-core"}


def interrupted(build="fixture-build", contexts=ALL_CONTEXTS) -> list[tuple[str, str]]:
    """What an interrupted watcher leaves: pending, then `error`, per context."""
    ordered = sorted(contexts)
    return [(c, f"cloud-build {build} running") for c in ordered] + [
        (c, f"cloud-build {build}: watcher interrupted; rerun cloud-ci.sh --reconcile")
        for c in ordered
    ]


def listing(statuses: list[tuple[str, str]]) -> str:
    """GitHub's listing of `statuses` (oldest first): newest first, with rising ids."""
    rows = [f"{i}\t{c}\t{d}" for i, (c, d) in enumerate(statuses, start=1)]
    return "\n".join(reversed(rows))


@pytest.fixture
def reattach_fixture(tmp_path):
    """Run `cloud-ci.sh --reconcile` against recorded gcloud/gh stubs (#2205).

    The gcloud stub fails any `builds submit`, so no test can bill a build.
    """
    repo = tmp_path / "operator"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/cloud-ci.sh", repo / "scripts/cloud-ci.sh")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q", "--initial-branch=main", "--template=")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@example.invalid")
    git("add", ".")
    git("commit", "-qm", "head")
    git("remote", "add", "origin", str(repo))
    sha = git("rev-parse", "HEAD")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    events = tmp_path / "events.jsonl"
    polls = tmp_path / "polls"
    lists = tmp_path / "lists"
    log = f"with open({str(events)!r}, 'a') as f: f.write(json.dumps(sys.argv)+'\\n')\n"
    gcloud = binaries / "gcloud"
    gcloud.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        + log
        + "if sys.argv[1:3] == ['builds','submit']: sys.exit(99)\n"
        "assert sys.argv[1:3] == ['builds','describe'], sys.argv\n"
        "if True:\n" + create_time_reply() + "assert sys.argv[3] == 'fixture-build', sys.argv\n"
        f"polls=pathlib.Path({str(polls)!r})\n"
        "done=len(polls.read_text()) if polls.exists() else 0\n"
        "if '--format=value(status)' in sys.argv:\n"
        " if os.environ.get('FIXTURE_POLL_FAIL'): sys.exit(23)\n"
        " with polls.open('a') as f: f.write('.')\n"
        " done+=1\n"
        "final=os.environ.get('FIXTURE_FINAL_STATUS','SUCCESS')\n"
        "status=final if done > int(os.environ.get('FIXTURE_RUNNING_POLLS','0')) else 'WORKING'\n"
        "if '--format=json' in sys.argv:\n"
        " jobs=['gates','shard-0','shard-1','shard-2','shard-3','js','native','full-verification']\n"
        " print(json.dumps({'status':status,\n"
        "   'substitutions':{'_SOURCE_SHA':os.environ['FIXTURE_BUILD_SHA']},\n"
        "   'tags':json.loads(os.environ.get('FIXTURE_TAGS',\n"
        '          \'["ua-ci", "ua-ci-js-1", "ua-ci-native-1"]\')),\n'
        "   'steps':[{'id':s,'status':'SUCCESS'} for s in jobs\n"
        "            if s != os.environ.get('FIXTURE_MISSING_STEP')]}))\n"
        "else: print(status)\n"
    )
    gh = binaries / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n" + log + "if sys.argv[1:3] == ['api','--paginate']:\n"
        f" assert sys.argv[3] == 'repos/fixture/platform/commits/{sha}/statuses?per_page=100'\n"
        f" lists=pathlib.Path({str(lists)!r})\n"
        " later=lists.exists() and os.environ.get('FIXTURE_LATER_STATUSES')\n"
        " lists.touch()\n"
        " print(later or os.environ.get('FIXTURE_STATUSES',''))\n"
        " sys.exit(0)\n"
        "assert sys.argv[1:4] == ['api','-X','POST'], sys.argv\n"
    )
    sleep = binaries / "sleep"
    sleep.write_text(f"#!{sys.executable}\nimport json,sys\n" + log)
    for stub in (gcloud, gh, sleep):
        stub.chmod(0o755)

    def run(*, build_sha=sha, statuses=None, later=None, build_id="fixture-build", **env):
        """`statuses` is the commit's history (oldest first); `later` replaces it
        for every listing after the first (a run that started meanwhile)."""
        statuses = interrupted() if statuses is None else statuses
        if later is not None:
            env["FIXTURE_LATER_STATUSES"] = listing(later)
        result = subprocess.run(
            ["bash", "scripts/cloud-ci.sh", "--reconcile", build_id, sha],
            cwd=repo,
            env={
                **os.environ,
                "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                "UA_CI_GCP_PROJECT": "fixture-project",
                "UA_CI_BACKEND": "cloudbuild",
                "UA_CI_REPO": "fixture/platform",
                "UA_CI_NO_STATUS": "0",
                "FIXTURE_BUILD_SHA": build_sha,
                "FIXTURE_STATUSES": listing(statuses),
                **env,
            },
            capture_output=True,
            text=True,
            timeout=WATCHER_RUN_TIMEOUT_S,
        )
        calls = (
            [json.loads(line) for line in events.read_text().splitlines()]
            if events.exists()
            else []
        )
        posts = []
        for call in calls:
            if call[1:4] == ["api", "-X", "POST"]:
                assert call[4] == f"repos/fixture/platform/statuses/{sha}"
                posts.append(dict(arg.split("=", 1) for arg in call if "=" in arg))
        assert not any(Path(call[0]).name == "gcloud" and "submit" in call for call in calls)
        return result, posts, calls

    run.sha = sha
    return run


@pytest.mark.parametrize("build_sha", ["0" * 40, ""], ids=["other-sha", "no-source-substitution"])
def test_reconcile_refuses_a_build_of_another_head(reattach_fixture, build_sha):
    result, posts, calls = reattach_fixture(build_sha=build_sha)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Refusing: build fixture-build tested" in result.stderr
    assert posts == []
    # One describe to read _SOURCE_SHA; no status listing, polling or posting.
    assert [Path(call[0]).name for call in calls] == ["gcloud"]


def test_reconcile_rejects_a_malformed_build_id(reattach_fixture):
    result, posts, calls = reattach_fixture(build_id="../x")
    assert result.returncode == 2
    assert calls == [] and posts == []


def test_reconcile_of_a_build_no_status_names_never_overwrites_another_build(reattach_fixture):
    other = [(c, "cloud-build other-build: all required jobs passed") for c in ALL_CONTEXTS]
    result, posts, _ = reattach_fixture(statuses=other)
    assert result.returncode != 0
    assert "a later run superseded it" in result.stderr
    assert posts == []


@pytest.mark.parametrize(
    "statuses",
    [[], interrupted(contexts=BASE_CONTEXTS)],
    ids=["no-status-at-all", "optional-writes-never-landed"],
)
def test_reconcile_settles_every_context_the_build_ran(reattach_fixture, statuses):
    """Devin review: the contexts come from the classification recorded on the
    build at submit time, not from which statuses happen to name it. A watcher
    whose js/desktop-core (or every) write failed still gets them settled."""
    result, posts, _ = reattach_fixture(statuses=statuses)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p["context"] for p in posts} == ALL_CONTEXTS
    assert len(posts) == len(ALL_CONTEXTS)
    assert {p["state"] for p in posts} == {"success"}
    assert all("fixture-build" in p["target_url"] for p in posts)
    assert "--reconcile" not in result.stderr


def test_reconcile_of_a_successful_build_posts_success_on_every_context(reattach_fixture):
    result, posts, _ = reattach_fixture(statuses=interrupted())
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p["context"] for p in posts} == ALL_CONTEXTS
    assert len(posts) == len(ALL_CONTEXTS)
    assert {p["state"] for p in posts} == {"success"}
    assert all("fixture-build" in p["target_url"] for p in posts)
    assert "--reconcile" not in result.stderr


def test_reconcile_never_overwrites_another_builds_optional_status(reattach_fixture):
    """Another build's `CI / js` is newest and this build never named that context.
    A --reconcile has no baseline to tell an older run from a newer one, so it
    refuses rather than overwrite it."""
    statuses = interrupted(contexts=BASE_CONTEXTS) + [
        ("CI / js", "cloud-build other-build: js passed")
    ]
    result, posts, _ = reattach_fixture(statuses=statuses)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert posts == []


@pytest.mark.parametrize("skipped", ["js", "native"])
@pytest.mark.parametrize("named", [False, True])
def test_reconcile_never_posts_a_leg_the_build_classified_out(reattach_fixture, skipped, named):
    """Review r2 (P1): a skipped leg's step runs and passes, so step presence and
    SUCCESS prove nothing. The submit-time classification tag says it was skipped:
    nothing is posted for that context, as a fresh run posts nothing for it."""
    context = "CI / js" if skipped == "js" else "CI / desktop-core"
    tags = ["ua-ci", f"ua-ci-{skipped}-0", f"ua-ci-{'native' if skipped == 'js' else 'js'}-1"]
    statuses = interrupted() if named else []
    result, posts, _ = reattach_fixture(statuses=statuses, FIXTURE_TAGS=json.dumps(tags))
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p["context"] for p in posts} == ALL_CONTEXTS - {context}


def test_reconcile_of_a_recorded_leg_whose_step_is_missing_fails_it(reattach_fixture):
    result, posts, _ = reattach_fixture(statuses=[], FIXTURE_MISSING_STEP="js")
    assert result.returncode != 0
    latest = {p["context"]: p["state"] for p in posts}
    assert latest["CI / js"] == "failure" and latest["CI / full-verification"] == "failure"


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (interrupted(), ALL_CONTEXTS),
        (interrupted(contexts=BASE_CONTEXTS), BASE_CONTEXTS),
        ([], BASE_CONTEXTS),
    ],
    ids=["all-named", "base-named", "none-named"],
)
def test_reconcile_of_a_build_without_a_recorded_classification(
    reattach_fixture, statuses, expected
):
    """A build submitted before the classification tags: an optional context is
    settled only when a status shows this run posted it, never assumed to have run."""
    result, posts, _ = reattach_fixture(statuses=statuses, FIXTURE_TAGS='["ua-ci"]')
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p["context"] for p in posts} == expected


@pytest.mark.parametrize(
    "tags",
    [
        ["ua-ci-js-1", "ua-ci-js-0", "ua-ci-native-1"],
        ["ua-ci-js-2", "ua-ci-native-1"],
        ["ua-ci-js-1"],
    ],
    ids=["contradictory", "invalid", "half"],
)
def test_reconcile_refuses_a_malformed_classification_record(reattach_fixture, tags):
    result, posts, calls = reattach_fixture(FIXTURE_TAGS=json.dumps(tags))
    assert result.returncode != 0
    assert "classification" in result.stderr
    assert posts == []


def test_reconcile_overwrites_an_older_builds_late_status(reattach_fixture):
    """Review r2 (P2): an older build's watcher finished after this build's
    statuses. Build creation, not status order, says which run is newer."""
    late = [(c, "cloud-build older-build: all required jobs passed") for c in sorted(ALL_CONTEXTS)]
    result, posts, _ = reattach_fixture(statuses=interrupted() + late)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p["context"]: p["state"] for p in posts} == dict.fromkeys(ALL_CONTEXTS, "success")


def test_reconcile_settles_after_an_older_builds_statuses(reattach_fixture):
    older = [(c, "cloud-build older-build: shard failed") for c in ALL_CONTEXTS]
    result, posts, _ = reattach_fixture(statuses=older + interrupted())
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p["context"]: p["state"] for p in posts} == dict.fromkeys(ALL_CONTEXTS, "success")


@pytest.mark.parametrize(
    "newer",
    [
        [(c, "cloud-build newer-build: failed") for c in sorted(BASE_CONTEXTS)],
        [("CI / test (shard=2)", "cloud-build newer-build: shard-2 FAILURE")],
        [("CI / gates", "manual override")],
    ],
    ids=["newer-build-failed", "one-context-superseded", "foreign-status"],
)
def test_reconcile_never_overwrites_a_newer_runs_status(reattach_fixture, newer):
    """Statuses are last-write-wins: an older build must not replace a newer result."""
    result, posts, calls = reattach_fixture(statuses=interrupted() + newer)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert posts == []
    assert not any("--format=value(status)" in call for call in calls), "refuses before waiting"


def test_reconcile_rechecks_for_a_newer_run_after_waiting(reattach_fixture):
    newer = [(c, "cloud-build newer-build running") for c in sorted(ALL_CONTEXTS)]
    result, posts, calls = reattach_fixture(later=interrupted() + newer, FIXTURE_RUNNING_POLLS="2")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert sum("--format=value(status)" in call for call in calls) == 3
    assert posts == []


@pytest.mark.parametrize(
    "env,context",
    [
        ({"FIXTURE_FINAL_STATUS": "FAILURE"}, None),
        ({"FIXTURE_FINAL_STATUS": "CANCELLED"}, None),
        ({"FIXTURE_MISSING_STEP": "shard-1"}, "CI / test (shard=1)"),
    ],
    ids=["failure", "cancelled", "missing-step"],
)
def test_reconcile_of_a_failed_build_never_certifies_the_aggregate(reattach_fixture, env, context):
    result, posts, _ = reattach_fixture(**env)
    assert result.returncode != 0, result.stdout + result.stderr
    latest = {p["context"]: p["state"] for p in posts}
    assert set(latest) == ALL_CONTEXTS
    assert latest["CI / full-verification"] == "failure"
    if context:
        assert latest[context] == "failure"
        assert {latest[c] for c in ALL_CONTEXTS - {context, "CI / full-verification"}} == {
            "success"
        }
    # A final failure is a real result, not an interruption: no re-attach hint.
    assert "--reconcile" not in result.stderr


def test_reconcile_waits_for_an_unfinished_build_before_posting(reattach_fixture):
    result, posts, calls = reattach_fixture(FIXTURE_RUNNING_POLLS="2")
    assert result.returncode == 0, result.stdout + result.stderr
    names = [Path(call[0]).name for call in calls]
    assert names.count("sleep") == 2
    assert sum("--format=value(status)" in call for call in calls) == 3
    # Nothing is posted while the build runs: every write follows the last poll.
    last_poll = max(i for i, call in enumerate(calls) if "--format=value(status)" in call)
    first_post = min(i for i, call in enumerate(calls) if call[1:4] == ["api", "-X", "POST"])
    assert first_post > last_poll
    assert {p["context"] for p in posts} == ALL_CONTEXTS
    assert {p["state"] for p in posts} == {"success"}


def test_a_failed_poll_is_retried_and_the_build_result_is_posted(cloud_fixture):
    """One failed describe used to abort and stamp every context as interrupted."""
    result, posts, calls, _ = cloud_fixture(FIXTURE_POLL_FAILS="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "watcher interrupted" not in result.stderr
    latest = {post["context"]: post["state"] for post in posts}
    assert latest["CI / full-verification"] == "success"
    assert sum("--format=value(status)" in call for call in calls) >= 2


def test_reconcile_interrupted_while_waiting_names_the_command_again(reattach_fixture):
    result, posts, _ = reattach_fixture(
        FIXTURE_RUNNING_POLLS="5",
        FIXTURE_POLL_FAIL="1",
        UA_CI_QUEUE_DEADLINE_S="0",
        UA_CI_RUN_DEADLINE_S="0",
    )
    assert result.returncode != 0
    assert {p["context"]: p["state"] for p in posts} == dict.fromkeys(ALL_CONTEXTS, "error")
    assert f"scripts/cloud-ci.sh --reconcile fixture-build {reattach_fixture.sha}" in result.stderr


def test_reconcile_interrupted_after_a_newer_run_started_leaves_statuses(reattach_fixture):
    newer = [(c, "cloud-build newer-build running") for c in sorted(ALL_CONTEXTS)]
    result, posts, _ = reattach_fixture(
        later=interrupted() + newer,
        FIXTURE_RUNNING_POLLS="5",
        FIXTURE_POLL_FAIL="1",
        UA_CI_QUEUE_DEADLINE_S="0",
        UA_CI_RUN_DEADLINE_S="0",
    )
    assert result.returncode != 0
    assert "Leaving the statuses" in result.stderr
    assert posts == []


def test_reconciling_the_same_build_twice_is_idempotent_and_allowed(reattach_fixture):
    """A second --reconcile of the build that settled the head finds its own final
    statuses newest; it is not a newer run, so it may post again, the same (#2211)."""
    first, posts, calls = reattach_fixture()
    assert first.returncode == 0, first.stdout + first.stderr
    assert {p["context"]: p["state"] for p in posts} == dict.fromkeys(ALL_CONTEXTS, "success")
    settled = interrupted() + [(p["context"], p["description"]) for p in posts]

    second, all_posts, _ = reattach_fixture(statuses=settled)
    again = all_posts[len(posts) :]
    assert second.returncode == 0, second.stdout + second.stderr
    assert "superseded" not in second.stderr
    assert again == posts


def test_a_fresh_run_whose_pending_writes_failed_never_overwrites_a_newer_run(cloud_fixture):
    """Review at 4b4130da (P2): with no pending write of its own (seen=0), exit
    recovery still must not post over a newer build's statuses."""
    result, posts, calls, _ = cloud_fixture(
        FIXTURE_POST_FAIL="state=pending",
        FIXTURE_POST_ALWAYS_FAIL="1",
        FIXTURE_NEWER_STATUSES=NEWER_RUN.replace(" running", ": FAILURE"),
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    assert posts == [], "no status may be written over the newer build"
    assert "a later run superseded it" in result.stderr


def test_a_fresh_run_without_pending_writes_still_settles_over_older_statuses(cloud_fixture):
    """Statuses that predate this run's submission do not block its results."""
    older = "\n".join(
        f"{c}\tcloud-build older-build: FAILURE"
        for c in ["CI / gates", *(f"CI / test (shard={i})" for i in range(4))]
    )
    result, posts, _, _ = cloud_fixture(
        FIXTURE_POST_FAIL="state=pending",
        FIXTURE_POST_ALWAYS_FAIL="1",
        FIXTURE_OLDER_STATUSES=older,
    )
    latest = {p["context"]: p["state"] for p in posts}
    assert latest and set(latest.values()) == {"success"}, result.stdout + result.stderr


def test_a_newer_build_that_posted_before_this_runs_pending_writes_blocks_them(cloud_fixture):
    """Review at e66d4cf6 (P2): B posts its failure after A's baseline but before A's
    pending writes. A's pending must not paper over it, nor its success follow."""
    result, posts, calls, _ = cloud_fixture(
        FIXTURE_NEWER_STATUSES=NEWER_RUN.replace(" running", ": FAILURE"),
        FIXTURE_NEWER_FROM_LISTING="2",
        FIXTURE_NEWER_POSITION="before-own",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    assert posts == [], [(p["context"], p["state"]) for p in posts]
    assert "a later run superseded it" in result.stderr


def test_a_newer_build_hidden_under_this_runs_own_pending_still_blocks_success(cloud_fixture):
    """B's failure sits between A's baseline and A's own newest pending status:
    the newest row is A's, but B's history above the baseline still blocks."""
    result, posts, _, _ = cloud_fixture(
        FIXTURE_NEWER_STATUSES=NEWER_RUN.replace(" running", ": FAILURE"),
        FIXTURE_NEWER_FROM_LISTING="3",
        FIXTURE_NEWER_POSITION="before-own",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert {p["state"] for p in posts} == {"pending"}
    assert "a later run superseded it" in result.stderr


def test_a_failed_status_read_before_pending_posts_nothing_and_names_recovery(cloud_fixture):
    """Codex review at f557f2ad (P2): the read before the first pending write
    fails while B's failure sits above A's baseline. A must not post pending over
    it; it posts nothing and prints the command that re-attaches to its build."""
    result, posts, calls, _ = cloud_fixture(
        FIXTURE_NEWER_STATUSES=NEWER_RUN.replace(" running", ": FAILURE"),
        FIXTURE_NEWER_FROM_LISTING="2",
        FIXTURE_NEWER_POSITION="before-own",
        FIXTURE_LIST_FAIL_AT="2",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert sum(call[:2] == ["builds", "submit"] for call in calls) == 1
    assert posts == [], [(p["context"], p["state"]) for p in posts]
    assert "scripts/cloud-ci.sh --reconcile fixture-build" in result.stderr
    # It stops there: no polling, no further listing or recovery pass.
    assert not any("--format=value(status)" in c for c in calls)


@pytest.mark.parametrize("js,native", [(False, False), (True, False), (False, True), (True, True)])
def test_reconcile_after_a_fresh_run_settles_exactly_its_contexts(cloud_fixture, js, native):
    """Review r2 (P1): re-attaching to a build settles the contexts the fresh
    run posted, from the classification it recorded on the build, so a leg its
    classifier skipped is never reported as passed."""
    result, fresh, _, data = cloud_fixture(classifier(js, native))
    assert result.returncode == 0, result.stdout + result.stderr
    assert {"ua-ci-js-" + str(int(js)), "ua-ci-native-" + str(int(native))} <= set(data["tags"])
    second, again = cloud_fixture.reconcile(data["sha"])
    assert second.returncode == 0, second.stdout + second.stderr
    assert {p["context"] for p in again} == {p["context"] for p in fresh}
    assert ("CI / js" in {p["context"] for p in again}) is js
    assert ("CI / desktop-core" in {p["context"] for p in again}) is native


def test_a_newer_failed_build_corrects_an_older_builds_late_success(cloud_fixture):
    """Review r2 (P2): an older build's pending predates this run's baseline and
    its success lands while this build runs. This build is newer by creation, so
    its failure is posted, not refused."""
    contexts = [
        "CI / gates",
        *(f"CI / test (shard={i})" for i in range(4)),
        "CI / full-verification",
    ]
    older_pending = "\n".join(f"{c}\tcloud-build older-build running" for c in contexts)
    older_final = "\n".join(
        f"{c}\tcloud-build older-build: all required jobs passed" for c in contexts
    )
    result, posts, _, _ = cloud_fixture(
        FIXTURE_OLDER_STATUSES=older_pending,
        FIXTURE_NEWER_STATUSES=older_final,
        BUILD_STATUS="FAILURE",
    )
    assert "superseded" not in result.stderr, result.stderr
    latest = {p["context"]: p["state"] for p in posts}
    assert latest["CI / full-verification"] == "failure", result.stdout + result.stderr


@pytest.mark.parametrize(
    "description",
    ["manual override", "cloud-build unknown-build: all required jobs passed"],
    ids=["not-a-build", "build-not-found"],
)
def test_a_status_from_a_run_of_unknown_age_still_refuses(cloud_fixture, description):
    """A status above the baseline whose build cannot be dated counts as newer."""
    newer = "\n".join(f"{c}\t{description}" for c in ["CI / gates", "CI / full-verification"])
    result, posts, _, _ = cloud_fixture(FIXTURE_NEWER_STATUSES=newer)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert {p["state"] for p in posts} == {"pending"}


# --- Review r3 (at cf6e0f7d) ----------------------------------------------------


@pytest.mark.parametrize("listing", ["2", "3"], ids=["before-pending", "before-settling"])
def test_a_fresh_run_refuses_over_a_status_with_no_description(cloud_fixture, listing):
    """Review r3 (P2): GitHub allows an empty or null description. Such a status
    names no build, so its run cannot be dated: it counts as newer and refuses."""
    result, posts, _, _ = cloud_fixture(
        FIXTURE_NEWER_STATUSES="CI / gates\t", FIXTURE_NEWER_FROM_LISTING=listing
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert not any(p["state"] == "success" for p in posts)
    if listing == "2":
        assert posts == []


def test_reconcile_refuses_over_a_status_with_no_description(reattach_fixture):
    result, posts, _ = reattach_fixture(statuses=interrupted() + [("CI / gates", "")])
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert posts == []


@pytest.mark.parametrize("hidden", [False, True], ids=["latest", "buried-under-own-pending"])
@pytest.mark.parametrize(
    "build,times",
    [
        ("newer-build", {}),
        ("newer-build", {"newer-build": ""}),
        ("newer-build", {"fixture-build": ""}),
        ("unavailable-build", {}),
        ("", {}),
    ],
    ids=["known-newer", "foreign-time-empty", "own-time-empty", "foreign-lookup-fails", "no-build"],
)
def test_reconcile_refuses_any_status_it_cannot_prove_older(reattach_fixture, hidden, build, times):
    """Review r3 (P2): a --reconcile has no baseline, so every other run's status
    in a context's history, not only the newest, must be from a build provably
    created before this one. A lookup that fails or returns no time refuses."""
    foreign = [("CI / gates", f"cloud-build {build}: FAILURE" if build else "manual failure")]
    own = interrupted()
    result, posts, _ = reattach_fixture(
        statuses=foreign + own if hidden else own + foreign,
        FIXTURE_CREATE_TIMES=json.dumps(times),
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert posts == []


@pytest.mark.parametrize("context", ["ci / gates", "CI / Gates", " CI / gates ", "CI / JS"])
def test_a_fresh_run_matches_status_contexts_without_case(cloud_fixture, context):
    """Review r3 (P2): GitHub compares contexts case-insensitively, so a newer
    run's `ci / gates` is the same context as this run's `CI / gates`."""
    source = classifier(True, False) if context == "CI / JS" else CLASSIFIER
    result, posts, _, _ = cloud_fixture(
        source, FIXTURE_NEWER_STATUSES=f"{context}\tcloud-build newer-build: FAILURE"
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert not any(p["state"] == "success" for p in posts)


@pytest.mark.parametrize("context", ["ci / gates", "CI / Gates", " CI / gates "])
def test_reconcile_matches_status_contexts_without_case(reattach_fixture, context):
    result, posts, _ = reattach_fixture(
        statuses=interrupted() + [(context, "cloud-build newer-build: FAILURE")]
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert posts == []


def test_reconcile_of_a_legacy_build_reads_its_optional_contexts_without_case(reattach_fixture):
    """A build without classification tags: its own `ci / js` status still counts."""
    statuses = interrupted(contexts=BASE_CONTEXTS) + [
        ("ci / js", "cloud-build fixture-build running")
    ]
    result, posts, _ = reattach_fixture(statuses=statuses, FIXTURE_TAGS='["ua-ci"]')
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p["context"] for p in posts} == BASE_CONTEXTS | {"CI / js"}


# --- Review r4 (at 17d60bf7) ----------------------------------------------------


def test_the_runner_exports_the_c_locale_before_its_first_child(tmp_path):
    """Locale-free guard for the r4 fix: the Turkish-locale tests below skip on
    a host without locale data (the CI image installs none), so this one runs
    everywhere. A stub git, the runner's first child, reports LC_ALL and stops
    before anything external happens."""
    stub = tmp_path / "git"
    stub.write_text('#!/bin/sh\nprintf "LC_ALL=%s\\n" "$LC_ALL" >&2\nexit 77\n')
    stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/cloud-ci.sh"), "head"],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "LC_ALL": "C.UTF-8",
            "UA_CI_REPO": "fixture/repo",
            "UA_CI_GCP_PROJECT": "fixture",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 77 and "LC_ALL=C\n" in result.stderr, result.stderr


@pytest.fixture(scope="session")
def turkish_locale(tmp_path_factory):
    """`LOCPATH`/`LC_ALL` for tr_TR.UTF-8, compiled into a scratch dir.

    In that locale `CI` lowercases to `cı` (dotless i) and `[A-Za-z]` follows
    its collation. Skipped where the host cannot compile the locale.
    """
    root = tmp_path_factory.mktemp("locales")
    localedef = shutil.which("localedef")
    if localedef is None:
        pytest.skip("localedef is not available")
    built = subprocess.run(
        [localedef, "--no-archive", "-i", "tr_TR", "-f", "UTF-8", str(root / "tr_TR.UTF-8")],
        capture_output=True,
        text=True,
    )
    if not (root / "tr_TR.UTF-8").is_dir():
        pytest.skip(f"tr_TR.UTF-8 could not be compiled: {built.stderr.strip()[:200]}")
    probe = subprocess.run(
        ["bash", "-c", 'x=CI; printf %s "${x,,}"'],
        env={**os.environ, "LOCPATH": str(root), "LC_ALL": "tr_TR.UTF-8"},
        capture_output=True,
        text=True,
    )
    if probe.stdout == "ci":
        pytest.skip("bash does not apply tr_TR case rules here")
    return {"LOCPATH": str(root), "LC_ALL": "tr_TR.UTF-8"}


@pytest.mark.parametrize("context", ["CI / gates", "ci / gates"])
def test_a_fresh_run_refuses_a_newer_build_under_a_turkish_locale(
    cloud_fixture, turkish_locale, context
):
    """Review r4 (P2): context matching must not follow the caller's locale."""
    result, posts, _, _ = cloud_fixture(
        FIXTURE_NEWER_STATUSES=f"{context}\tcloud-build newer-build: FAILURE", **turkish_locale
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert not any(p["state"] == "success" for p in posts)


@pytest.mark.parametrize("context", ["CI / gates", "ci / gates"])
def test_reconcile_refuses_a_newer_build_under_a_turkish_locale(
    reattach_fixture, turkish_locale, context
):
    result, posts, _ = reattach_fixture(
        statuses=interrupted() + [(context, "cloud-build newer-build: FAILURE")], **turkish_locale
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "a later run superseded it" in result.stderr
    assert posts == []


def test_reconcile_settles_normally_under_a_turkish_locale(reattach_fixture, turkish_locale):
    """The build-id check and the rest of the run behave as in any other locale."""
    result, posts, _ = reattach_fixture(**turkish_locale)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p["context"]: p["state"] for p in posts} == dict.fromkeys(ALL_CONTEXTS, "success")
