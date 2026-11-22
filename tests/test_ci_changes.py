"""scripts/ci_changes.py decides which CI jobs a change needs (task #1498).

CI bills runner time (GitHub Actions per job-minute, Depot per second), so every
rule that skips a job is also a claim that the job could not have failed. These tests pin both halves: what is skipped,
and that anything unrecognised still runs everything.
"""

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_changes", REPO_ROOT / "scripts" / "ci_changes.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def run(paths):
    out = ci.classify(paths)
    return out["python"], out["python_scope"], out["js"]


def test_the_affected_cli_spec_is_module_level_and_static():
    """Loading the selector by path must stay readable to the import graph.

    The spec has to be a module-level binding on a static path: built inside the
    function, ``scripts/ci_changes.py`` was itself unresolved, so every test
    reaching it was unownable and ran on every Python change (task #2458).
    """
    assert Path(ci._AFFECTED_SPEC.origin) == REPO_ROOT / "scripts" / "affected_tests.py"
    module = ci._affected_cli()
    assert module is ci._affected_cli(), "the selector CLI is loaded once per process"
    assert module.__name__ == "ua_affected_tests_cli"
    assert callable(module.selected_paths)
    assert callable(module.affected_tests.parse_name_status)


def test_no_resolvable_diff_runs_everything():
    assert ci.classify([]) == {
        "python": True,
        "python_scope": "full",
        "shards": [0, 1, 2, 3],
        "js": True,
        "connectors": True,
        "native": True,
    }
    assert ci.classify(["", "  "])["python_scope"] == "full"


def test_markdown_only_runs_lint_and_doc_reading_tests_in_one_shard():
    out = ci.classify(["docs/architecture/x.md", "CHANGELOG.md", "AGENTS.md"])
    assert (out["python"], out["python_scope"], out["js"]) == (True, "docs", False)
    assert out["shards"] == [0]


@pytest.mark.parametrize("path", ["tests/fixtures/notes.md", "src/unified_agent/prompts/x.md"])
def test_markdown_under_src_or_tests_is_code(path):
    assert run([path]) == (True, "full", False)


def test_non_markdown_docs_file_is_not_documentation():
    # docs/capability-catalog/catalog.json is read by src at runtime.
    assert run(["docs/capability-catalog/catalog.json"]) == (True, "full", True)


def test_python_plus_changelog_skips_the_js_stack():
    assert run(["src/unified_agent/tools/plan.py", "tests/test_plan.py", "CHANGELOG.md"]) == (
        True,
        "full",
        False,
    )


@pytest.mark.parametrize("path", ["uv.lock", "pyproject.toml"])
def test_dependency_pins_run_both_stacks(path):
    # The protocol job's codegen check runs Python tooling pinned by uv.lock.
    assert run([path]) == (True, "full", True)


def test_packages_only_skips_python():
    assert run(["packages/web/src/App.tsx"]) == (False, "docs", True)


def test_packages_plus_markdown_runs_js_and_doc_tests():
    assert run(["packages/web/src/App.tsx", "docs/web.md"]) == (True, "docs", True)


@pytest.mark.parametrize(
    "paths,sdk,web",
    [
        (["packages/web/src/App.tsx"], False, True),
        (["packages/web/package-lock.json", "docs/web.md"], False, True),
        (["packages/web/src/App.tsx", "src/unified_agent/api.py"], True, True),
        (["packages/web/src/App.tsx", "scripts/ci_changes.py"], True, True),
        (["packages/protocol/src/schemas.ts"], True, True),
        (["packages/client/README.md"], True, True),
        (["packages/new-package/src/index.ts"], True, True),
        (["uv.lock"], True, True),
        ([".github/workflows/ci.yml"], True, True),
        ([], True, True),
        (["src/unified_agent/api.py"], False, False),
        (["docs/web.md"], False, False),
    ],
)
def test_js_components_follow_dependencies_and_unknowns_fail_closed(paths, sdk, web):
    assert ci.js_components(paths) == {"sdk": sdk, "web": web}
    # This is a separate contract: Cloud Build's closed six-key classifier
    # and its persisted reconcile tags must not silently change shape.
    assert set(ci.classify(paths)) == {
        "python",
        "python_scope",
        "shards",
        "js",
        "connectors",
        "native",
    }


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", None])
def test_js_join_rejects_an_enabled_component_that_did_not_pass(result):
    needs = {
        "gates": {
            "result": "success",
            "outputs": {"full": "true", "js": "true", "sdk": "true", "web": "true"},
        },
        "sdk": {"result": result},
        "web": {"result": "success"},
    }
    assert ci.js_verification_result(needs)[0] is False


def test_js_join_allows_only_a_deliberately_skipped_sdk_job():
    needs = {
        "gates": {
            "result": "success",
            "outputs": {"full": "true", "js": "true", "sdk": "false", "web": "true"},
        },
        "sdk": {"result": "skipped"},
        "web": {"result": "success"},
    }
    assert ci.js_verification_result(needs)[0] is True
    for value in (None, "", "FALSE", "unknown"):
        needs["gates"]["outputs"]["sdk"] = value
        assert ci.js_verification_result(needs)[0] is False
    needs["gates"]["outputs"]["sdk"] = "false"
    needs["web"]["result"] = "skipped"
    assert ci.js_verification_result(needs)[0] is False


def test_js_join_rejects_invalid_classification_or_failed_gates():
    needs = {
        "gates": {
            "result": "success",
            "outputs": {"full": "true", "js": "true", "sdk": "true", "web": "true"},
        },
        "sdk": {"result": "success"},
        "web": {"result": "success"},
    }
    assert ci.js_verification_result(needs)[0] is True
    for key in ("full", "js", "web"):
        needs["gates"]["outputs"][key] = "false"
        assert ci.js_verification_result(needs)[0] is False
        needs["gates"]["outputs"][key] = "true"
    needs["gates"]["result"] = "failure"
    assert ci.js_verification_result(needs)[0] is False


def test_workflow_or_unknown_paths_run_everything():
    assert run([".depot/workflows/ci.yml"]) == (True, "full", True)
    assert run(["deploy/gcp/cost-guards/verify_cost_guards.py", "src/x.py"]) == (True, "full", True)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/unified_agent/connectors/slack.py", True),
        ("src/unified_agent/github_app.py", True),
        ("docs/connectors.md", True),
        ("tests/connectors/test_x.py", True),
        ("src/unified_agent/tools/plan.py", False),
    ],
)
def test_connector_gate(path, expected):
    assert ci.classify([path])["connectors"] is expected


@pytest.mark.parametrize(
    "path, expected",
    [
        ("native/UnifiedAgentDesktop/core/src/lib.rs", True),
        ("native/UnifiedAgentDesktop/Cargo.lock", True),
        ("native/UnifiedAgentDesktop/README.md", True),
        ("native/UnifiedAgentMobile/Package.swift", False),
        ("src/unified_agent/storage.py", False),
        ("packages/web/src/App.tsx", False),
        ("docs/architecture/x.md", False),
    ],
)
def test_desktop_core_gate(path, expected):
    # The desktop shell's own markdown still runs its checks: the README is
    # part of the crate, not repo documentation.
    assert ci.classify([path])["native"] is expected


def test_desktop_core_job_is_required_only_when_the_desktop_changed():
    assert "CI / desktop-core" in ci.required_jobs(
        ci.classify(["native/UnifiedAgentDesktop/src-tauri/src/main.rs"])
    )
    assert "CI / desktop-core" not in PY_FULL
    assert "CI / desktop-core" in ci.required_jobs(ci.classify([]))


def test_output_format_is_github_output_lines():
    text = ci.format_outputs(ci.classify(["CHANGELOG.md"]))
    lines = dict(line.split("=", 1) for line in text.strip().splitlines())
    assert lines["python"] == "true"
    assert lines["js"] == "false"
    assert json.loads(lines["shards"]) == [0]
    skip = dict(
        line.split("=", 1) for line in ci.format_outputs(ci.skip_all()).strip().splitlines()
    )
    assert skip["python"] == skip["js"] == skip["connectors"] == skip["native"] == "false"


def test_full_candidates_match_the_old_find_command_without_duplicates():
    found = {
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "tests").rglob("*.py")
        if p.name.startswith("test_") or p.name.endswith("_test.py")
    }
    full = ci.candidates("full")
    # A file matching both patterns (test_*_test.py) must be listed once, or
    # pytest runs it twice and bills twice.
    assert len(full) == len(set(full))
    assert set(full) == found


def test_docs_candidates_are_the_tests_that_name_documentation(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_reads_docs.py").write_text('ROOT / "docs" / "x.md"\n')
    (tests / "test_reads_changelog.py").write_text("open('CHANGELOG.md')\n")
    (tests / "test_docstring_only.py").write_text('"""Has a docstring and docsets."""\n')
    (tests / "test_plain.py").write_text("assert 1\n")
    assert ci.candidates("docs", root=tmp_path) == [
        "tests/test_reads_changelog.py",
        "tests/test_reads_docs.py",
    ]


def test_docs_candidates_include_the_doc_tests_in_this_repo():
    docs = ci.candidates("docs")
    assert "tests/test_docs_tool_count.py" in docs
    assert set(docs) < set(ci.candidates("full"))


def _run(name, conclusion="success", status="completed", id_=1, app=ci.DEPOT_APP, suite=None):
    return {
        "name": name,
        "conclusion": conclusion,
        "status": status,
        "id": id_,
        "app": {"slug": app},
        "check_suite": {"id": suite},
    }


CI_SUITE = 4242


def _gha(name, suite=CI_SUITE, **kw):
    """A GitHub Actions check run: bare job name, workflow known only by suite."""
    return _run(name.removeprefix("CI / "), app=ci.GITHUB_ACTIONS_APP, suite=suite, **kw)


PY_FULL = ci.required_jobs(ci.classify(["src/x.py"]))


def _passing(names, **overrides):
    return [_run(n, id_=i) for i, n in enumerate(sorted(names), start=1)] + [
        _run(n, **kw) for n, kw in overrides.items()
    ]


def test_required_jobs_follow_the_classification():
    assert PY_FULL == {
        "CI / full-verification",
        "CI / gates",
        "CI / test (shard=0)",
        "CI / test (shard=1)",
        "CI / test (shard=2)",
        "CI / test (shard=3)",
    }
    assert ci.required_jobs(ci.classify(["CHANGELOG.md"])) == {
        "CI / full-verification",
        "CI / gates",
        "CI / test (shard=0)",
    }
    # #1787: protocol/client/web are one `js` job.
    assert "CI / js" in ci.required_jobs(ci.classify(["packages/web/src/App.tsx"]))


def test_ci_passed_accepts_every_required_job_green_and_ignores_other_apps():
    runs = _passing(PY_FULL) + [
        _run("CI / js", conclusion="skipped", id_=90),
        _run("Docker / build", conclusion="failure", app="other-app", id_=91),
    ]
    assert ci.ci_passed(runs, PY_FULL)[0]


def test_a_missing_required_job_is_not_a_pass():
    # Devin review, PR #811: 'CI / changes' alone must not prove the suite ran.
    ok, why = ci.ci_passed([_run("CI / changes")], PY_FULL)
    assert not ok and "lacks required CI checks" in why
    runs = [r for r in _passing(PY_FULL) if r["name"] != "CI / gates"]
    assert not ci.ci_passed(runs, PY_FULL)[0]


def test_a_skipped_required_job_is_not_a_pass():
    runs = [r for r in _passing(PY_FULL) if r["name"] != "CI / test (shard=2)"]
    runs.append(_run("CI / test (shard=2)", conclusion="skipped", id_=99))
    assert not ci.ci_passed(runs, PY_FULL)[0]


def test_ci_passed_rejects_failed_pending_or_cancelled_jobs():
    for kw in (
        {"conclusion": "failure"},
        {"conclusion": None, "status": "in_progress"},
        {"conclusion": "cancelled"},
    ):
        runs = _passing(PY_FULL) + [_run("CI / desktop-core", id_=99, **kw)]
        assert not ci.ci_passed(runs, PY_FULL)[0], kw


def test_a_rerun_that_passed_supersedes_the_failure():
    base = [r for r in _passing(PY_FULL) if r["name"] != "CI / test (shard=1)"]
    failed = _run("CI / test (shard=1)", conclusion="failure", id_=50)
    passed = _run("CI / test (shard=1)", conclusion="success", id_=60)
    assert ci.ci_passed(base + [failed, passed], PY_FULL)[0]
    assert not ci.ci_passed(base + [passed | {"id": 40}, failed], PY_FULL)[0]


def test_github_actions_runs_count_only_inside_the_ci_workflow_suite():
    """#1787: GitHub Actions names a check "test (shard=0)", not "CI / test (shard=0)".

    Verified against real heads: pre-Depot PR #636 reported bare job names from
    app github-actions; Depot-era PR #961 reported "CI / ..." from
    depot-code-access. Docker's "build" carries a bare name too, so a GitHub run
    counts only when its check suite belongs to the CI workflow.
    """
    runs = [_gha(n, id_=i) for i, n in enumerate(sorted(PY_FULL), start=1)]
    assert ci.ci_passed(runs, PY_FULL, frozenset({CI_SUITE}))[0]
    # Without the CI suite id nothing is attributable to CI.
    ok, why = ci.ci_passed(runs, PY_FULL)
    assert not ok and "lacks required CI checks" in why
    # A failing job from another workflow does not block the skip...
    other = _gha("build", suite=7, conclusion="failure", id_=99)
    assert ci.ci_passed(runs + [other], PY_FULL, frozenset({CI_SUITE}))[0]
    # ...but a failing CI job does.
    red = _gha("desktop-core", conclusion="failure", id_=98)
    assert not ci.ci_passed(runs + [red], PY_FULL, frozenset({CI_SUITE}))[0]


def test_either_provider_can_satisfy_a_required_job():
    names = sorted(PY_FULL)
    mixed = [_gha(n, id_=i) if i % 2 else _run(n, id_=i) for i, n in enumerate(names, start=1)]
    assert ci.ci_passed(mixed, PY_FULL, frozenset({CI_SUITE}))[0]
    assert ci.ci_check_name(_run("CI / gates"), frozenset()) == "CI / gates"
    assert ci.ci_check_name(_run("Docker / build"), frozenset()) is None
    assert ci.ci_check_name(_gha("gates"), frozenset({CI_SUITE})) == "CI / gates"
    assert ci.ci_check_name(_run("CI / gates", app="other-app"), frozenset({1})) is None


def test_ci_workflow_suites_reads_the_ci_workflow_path(monkeypatch):
    body = {
        "workflow_runs": [
            {"path": ".github/workflows/ci.yml", "check_suite_id": 11},
            {"path": ".github/workflows/docker.yml", "check_suite_id": 12},
            {"path": ".github/workflows/ci.yml", "check_suite_id": 13},
        ]
    }
    seen = []
    monkeypatch.setattr(ci, "_api", lambda path, token: seen.append(path) or body)
    assert ci.ci_workflow_suites("o/r", "h" * 40, "t") == frozenset({11, 13})
    assert seen == [f"repos/o/r/actions/runs?head_sha={'h' * 40}&per_page=100"]


def test_shards_partition_the_candidates_evenly_and_deterministically():
    files = ci.candidates("full")
    shards = [ci.shard_candidates(files, i, 4) for i in range(4)]
    flat = [f for shard in shards for f in shard]
    assert sorted(flat) == sorted(files), "every candidate runs exactly once"
    assert all(shards), "no shard is empty"
    assert shards == [ci.shard_candidates(files, i, 4) for i in range(4)]
    weights = [
        sum(1 + len(ci._TEST_DEF.findall((REPO_ROOT / f).read_text())) for f in shard)
        for shard in shards
    ]
    assert max(weights) - min(weights) <= max(weights) * 0.05, weights
    assert ci.shard_candidates(files, 0, 1) == sorted(files)


@pytest.mark.parametrize("shard,total", [(4, 4), (-1, 4), (0, 0)])
def test_shard_out_of_range_is_an_error(shard, total):
    with pytest.raises(ValueError):
        ci.shard_candidates(["tests/test_x.py"], shard, total)


def test_candidates_cli_shards_and_requires_both_flags(capsys):
    assert ci.main(["candidates", "--scope", "docs", "--shard", "0", "--total", "1"]) == 0
    assert capsys.readouterr().out.split() == ci.candidates("docs")
    with pytest.raises(SystemExit):
        ci.main(["candidates", "--scope", "full", "--shard", "0"])


def test_check_runs_are_read_across_pages(monkeypatch):
    pages = {
        1: {"total_count": 150, "check_runs": [_run(f"CI / x{i}", id_=i) for i in range(100)]},
        2: {"total_count": 150, "check_runs": [_run(f"CI / y{i}", id_=i) for i in range(50)]},
    }
    monkeypatch.setattr(ci, "_api", lambda path, token: pages[int(path.rsplit("page=", 1)[1])])
    assert len(ci.all_check_runs("o/r", "h", "t")) == 150


def test_a_short_check_run_read_raises(monkeypatch):
    monkeypatch.setattr(
        ci, "_api", lambda path, token: {"total_count": 12, "check_runs": [_run("CI / changes")]}
    )
    with pytest.raises(RuntimeError, match="read 1 of 12"):
        ci.all_check_runs("o/r", "h", "t")


@pytest.mark.parametrize("path", ["packages/protocol/README.md", "packages/client/README.md"])
def test_package_readmes_are_package_code(path):
    # Devin review, PR #811: they ship with their packages, so the JS stack must run.
    assert run([path]) == (False, "docs", True)


def test_already_tested_without_token_runs_everything(monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert ci.main(["already-tested", "--sha", "abc", "--repo", "o/r"]) == 1
    assert "running everything" in capsys.readouterr().out


def test_already_tested_api_error_runs_everything(monkeypatch, capsys):
    monkeypatch.setenv("GH_TOKEN", "t")

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(ci, "_api", boom)
    assert ci.main(["already-tested", "--sha", "abc", "--repo", "o/r"]) == 1
    assert "no — OSError" in capsys.readouterr().out


def test_already_tested_requires_an_identical_tree_and_the_prs_required_jobs(monkeypatch):
    head, sha, base = "h" * 40, "m" * 40, "b" * 40
    checks = {"total_count": 0, "check_runs": []}

    def api(path, token):
        if path.endswith("/pulls"):
            return [
                {"number": 7, "merge_commit_sha": sha, "head": {"sha": head}, "base": {"sha": base}}
            ]
        return checks

    monkeypatch.setattr(ci, "_api", api)
    git = {
        f"{sha}^{{tree}}": "T1",
        f"{head}^{{tree}}": "T2",
        "refs/ci/pr-head": head,
        f"{base}...{head}": "src/unified_agent/x.py\nCHANGELOG.md",
    }
    monkeypatch.setattr(ci, "_git", lambda *a: git.get(a[-1], ""))
    ok, why = ci.already_tested(sha, "o/r", "t")
    assert not ok and "tree differs" in why

    git[f"{head}^{{tree}}"] = "T1"
    checks["check_runs"] = [_run("CI / changes")]
    checks["total_count"] = 1
    ok, why = ci.already_tested(sha, "o/r", "t")
    assert not ok and "lacks required CI checks" in why

    checks["check_runs"] = _passing(PY_FULL)
    checks["total_count"] = len(checks["check_runs"])
    ok, why = ci.already_tested(sha, "o/r", "t")
    assert ok and "PR #7" in why


def test_already_tested_accepts_a_github_actions_pr_head(monkeypatch):
    head, sha, base = "h" * 40, "m" * 40, "b" * 40
    runs = [_gha(n, id_=i) for i, n in enumerate(sorted(PY_FULL), start=1)]

    def api(path, token):
        if path.endswith("/pulls"):
            return [
                {"number": 8, "merge_commit_sha": sha, "head": {"sha": head}, "base": {"sha": base}}
            ]
        if "/actions/runs?head_sha=" in path:
            assert head in path
            return {"workflow_runs": [{"path": ci.CI_WORKFLOW_PATH, "check_suite_id": CI_SUITE}]}
        return {"total_count": len(runs), "check_runs": runs}

    monkeypatch.setattr(ci, "_api", api)
    git = {
        f"{sha}^{{tree}}": "T1",
        f"{head}^{{tree}}": "T1",
        "refs/ci/pr-head": head,
        f"{base}...{head}": "src/unified_agent/x.py",
    }
    monkeypatch.setattr(ci, "_git", lambda *a: git.get(a[-1], ""))
    ok, why = ci.already_tested(sha, "o/r", "t")
    assert ok and "PR #8" in why


def test_already_tested_ignores_a_commit_no_pr_merged(monkeypatch):
    monkeypatch.setattr(
        ci, "_api", lambda path, token: [{"number": 7, "merge_commit_sha": "other", "head": {}}]
    )
    assert ci.already_tested("abc", "o/r", "t") == (
        False,
        "no merged pull request produced this commit",
    )


def _needs(paths, **results):
    outputs = ci.classify(paths)
    formatted = dict(line.split("=", 1) for line in ci.format_outputs(outputs).splitlines())
    jobs = {name: {"result": "success"} for name in ("gates", "test", "js", "desktop-core")}
    jobs["gates"]["outputs"] = formatted | {"full": "true"}
    for name, result in results.items():
        jobs[name]["result"] = result
    return jobs


def test_draft_cannot_certify_full_ci_even_with_green_light_checks():
    needs = _needs(["src/x.py"], test="skipped", js="skipped")
    assert not ci.verification_result(needs, "pull_request", True)[0]
    assert not ci.verification_result(_needs(["src/x.py"]), "pull_request", True)[0]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "", None])
def test_required_matrix_or_security_gate_cannot_be_skipped(result):
    for job in ("test", "gates"):
        assert not ci.verification_result(
            _needs(["src/x.py"], **{job: result}), "pull_request", False
        )[0]


@pytest.mark.parametrize(
    "paths", [["src/x.py"], ["CHANGELOG.md"], ["packages/web/src/App.tsx"], ["uv.lock"]]
)
def test_ready_revision_requires_its_full_classified_jobs(paths):
    assert ci.verification_result(_needs(paths), "pull_request", False)[0]
    ok, _ = ci.verification_result(_needs(paths, js="skipped"), "pull_request", False)
    assert ok is (not ci.classify(paths)["js"])


def test_desktop_core_is_required_only_for_desktop_changes():
    desktop = ["native/UnifiedAgentDesktop/core/src/lib.rs"]
    assert ci.verification_result(_needs(desktop), "pull_request", False)[0]
    for result in ("skipped", "failure", "cancelled"):
        ok, why = ci.verification_result(
            _needs(desktop, **{"desktop-core": result}), "pull_request", False
        )
        assert not ok and "desktop-core" in why
    # A change that never touched the desktop may skip the job.
    assert ci.verification_result(
        _needs(["src/x.py"], **{"desktop-core": "skipped"}), "pull_request", False
    )[0]


def test_unknown_results_fail_closed():
    needs = _needs([])
    needs["gates"]["outputs"] = {
        "full": "true",
        "python": "false",
        "js": "false",
        "connectors": "false",
        "native": "false",
    }
    assert ci.verification_result(needs, "workflow_dispatch", False)[0]
    needs["js"]["result"] = "failure"
    ok, why = ci.verification_result(needs, "workflow_dispatch", False)
    assert not ok and "js" in why
    needs["js"]["result"] = "skipped"
    del needs["gates"]["outputs"]["python"]
    assert not ci.verification_result(needs, "workflow_dispatch", False)[0]


def test_verify_cli_rejects_missing_or_invalid_metadata(monkeypatch, capsys):
    monkeypatch.delenv("CI_NEEDS", raising=False)
    assert ci.main(["verify"]) == 1
    monkeypatch.setenv("CI_NEEDS", "[]")
    monkeypatch.setenv("CI_EVENT", "pull_request")
    assert ci.main(["verify"]) == 1
    monkeypatch.setenv("CI_NEEDS", json.dumps(_needs(["src/x.py"])))
    monkeypatch.delenv("CI_DRAFT", raising=False)
    assert ci.main(["verify"]) == 1
    monkeypatch.setenv("CI_DRAFT", "true")
    assert ci.main(["verify"]) == 1
    monkeypatch.setenv("CI_DRAFT", "false")
    assert ci.main(["verify"]) == 0


@pytest.mark.parametrize(
    "path",
    [
        "src/unified_agent/telemetry/jwk.py",
        "src/unified_agent/audit/rules.py",
        "src/unified_agent/tools/public_relay.py",
        "tests/test_jwk_js_hash_2142.py",
        "scripts/ci_changes.py",
        "deploy/cloudbuild/ci-step.sh",
        ".github/workflows/ci.yml",
        ".depot/workflows/ci.yml",
        "tests/test_redaction_oct_blocks.py",
        "tests/param_sampling.py",
        "tests/conftest.py",
        "uv.lock",
        "pyproject.toml",
    ],
)
def test_a_change_that_can_alter_a_sampled_matrix_runs_every_case(path):
    assert ci.exhaustive(["docs/notes.md", path]) is True


def test_an_unrelated_change_runs_the_default_sample():
    assert ci.exhaustive(["src/unified_agent/tasks.py", "tests/test_tasks.py"]) is False


def test_an_unknown_change_set_runs_every_case():
    assert ci.exhaustive([]) is True
    assert ci.exhaustive(["", "  "]) is True


def test_affected_selection_is_narrower_than_the_full_suite():
    selected = ci.affected_selection(["tests/test_sleep_pass.py"])
    assert selected is not None
    assert "tests/test_sleep_pass.py" in selected
    assert len(selected) < len(ci.candidates("full"))


@pytest.mark.parametrize(
    "lines",
    [
        # The selector misses lazy hops past the first src module and does not
        # follow src modules whose dynamic imports it cannot read, so a batch
        # that changes src or scripts Python cannot be certified by a subset.
        ["src/unified_agent/sleep_pass.py"],
        ["M\tscripts/ci_changes.py"],
        ["D\tsrc/unified_agent/sleep_pass.py"],
        ["R100\tsrc/unified_agent/sleep_pass.py\ttests/test_sleep_pass.py"],
    ],
)
def test_affected_certification_runs_everything_for_src_and_scripts(lines):
    assert ci.affected_selection(lines) is None


def test_affected_certification_runs_everything_when_the_selector_has_caveats(monkeypatch):
    module = ci._affected_cli()
    real = module.selected_paths

    def with_caveat(root, changes, **kwargs):
        selected, selection = real(root, changes, **kwargs)
        selection.caveats.append("src/unified_agent/x.py import_module(name): not followed")
        return selected, selection

    monkeypatch.setattr(module, "selected_paths", with_caveat)
    assert ci.affected_selection(["tests/test_sleep_pass.py"]) is None


def test_diff_names_keeps_deletions_and_rename_sources(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "gone.py").write_text("x = 1\n")
    (repo / "old.py").write_text("y = 2\n" * 20)
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("rm", "-q", "gone.py")
    git("mv", "old.py", "new.py")
    git("commit", "-qm", "head")
    head = git("rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    real_run = subprocess.run

    def no_fetch(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ci.subprocess, "run", no_fetch)
    changes = ci._affected_cli().affected_tests.parse_name_status(ci._diff_names(base, head))
    by_path = {c.path: c for c in changes}
    assert by_path["gone.py"].deleted
    assert by_path["new.py"].renamed and by_path["new.py"].old_path == "old.py"


def test_affected_cli_prints_all_for_an_empty_diff(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert ci.main(["affected"]) == 0
    assert capsys.readouterr().out.strip() == "ALL"


def test_affected_batch_certifies_when_the_full_flag_is_off():
    needs = _needs(["src/unified_agent/tasks.py"])
    needs["gates"]["outputs"]["full"] = "false"
    needs["gates"]["outputs"]["affected"] = "true"
    assert ci.verification_result(needs, "merge_group", False)[0]
    assert ci.verification_result(needs, "pull_request", True)[0]
    needs["gates"]["outputs"]["affected"] = "false"
    assert not ci.verification_result(needs, "pull_request", False)[0]


def test_js_join_accepts_an_affected_batch_without_the_full_flag():
    needs = {
        "gates": {
            "result": "success",
            "outputs": {
                "full": "false",
                "affected": "true",
                "js": "true",
                "sdk": "true",
                "web": "true",
            },
        },
        "sdk": {"result": "success"},
        "web": {"result": "success"},
    }
    assert ci.js_verification_result(needs)[0] is True
