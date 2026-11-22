#!/usr/bin/env python3
"""Decide which CI jobs a change needs (task #1498). Stdlib only: runs before `uv sync`.

CI minutes are billed per job (GitHub Actions rounds each job up to the minute;
Depot CI bills by the second), and the four pytest shards are most of every
run, so the savings come from not running tests that cannot change the answer.
Each rule here narrows only where the skipped job provably cannot read the
changed files. Anything unrecognised runs everything.

The same rules serve both CI providers: GitHub Actions (`.github/workflows/`,
automatic since task #1787) and the dispatch-only Depot copy
(`.depot/workflows/`).

Subcommands:

  classify      changed paths on stdin -> GITHUB_OUTPUT lines on stdout
  skip          GITHUB_OUTPUT lines that turn every test job off
  js-components changed paths on stdin -> SDK/frontend job switches; --skip disables both
  verify-js     require the enabled SDK/frontend jobs to pass before certifying `js`
  verify        require enabled jobs to pass; draft light checks never certify CI
  candidates    test files for a scope (full | docs), one per line; with
                --shard/--total, only that shard's share of them
  exhaustive    changed paths on stdin -> "true" when the run must execute
                every case of the sampled parametrize matrices
                (tests/param_sampling.py), else "false"
  already-tested
                exit 0 when a push to main is byte-identical to a PR head whose
                CI already passed, so re-running the suite could not differ.
                Any doubt (API error, no PR, tree mismatch, a failed or pending
                job) exits 1 and the caller runs everything.
  affected      changed paths on stdin -> affected test files, one per line.
                The single line ALL means the full suite (empty diff, or a
                change the import graph cannot narrow). --base and --head
                read ``git diff --name-only`` instead of stdin. Reuses
                scripts/affected_tests.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The selector CLI, loaded by path twice over: this module uses it under
#: ``affected_selection``, and ``scripts/affected_tests.py`` imports this module
#: back, so it must not be imported before this module finishes initializing.
#: The spec is built here, at module level on a static path, so the import graph
#: in ``unified_agent.affected_tests`` can follow it (task #2458); only the
#: ``exec_module`` stays lazy, inside :func:`_affected_cli`.
_AFFECTED_SPEC = importlib.util.spec_from_file_location(
    "ua_affected_tests_cli", REPO_ROOT / "scripts" / "affected_tests.py"
)
# Built here but not run: ``module_from_spec`` only creates the module object, so
# the cycle stays broken. Both bindings are module-level on purpose — the graph in
# ``unified_agent.affected_tests`` traces ``exec_module`` through single
# module-level bindings, which is why this is not done inside the function.
_AFFECTED_MODULE = importlib.util.module_from_spec(_AFFECTED_SPEC)

# Python-only sources: skipping the protocol/client/web stack is safe because no
# JS package reads them. pyproject.toml / uv.lock are deliberately NOT here: the
# protocol job's codegen check runs Python tooling pinned by uv.lock.
PYTHON_ONLY = re.compile(r"^(src/|tests/|scripts/|Makefile$)")
PACKAGES = re.compile(r"^packages/")
CONNECTORS = re.compile(
    r"^src/unified_agent/(connectors(\.py|/.*)|horizon2\.py|tools/horizon2\.py|tools/doctor\.py"
    r"|slack_.*\.py|github_.*\.py|.*_(connector|gateway)\.py)$|^tests/connectors/|^docs/connectors\.md$"
)
# The Tauri desktop shell (task #1648). Its README is part of the crate, so any
# path under it, markdown included, runs the desktop-core job.
NATIVE_DESKTOP = re.compile(r"^native/UnifiedAgentDesktop/")
# A test that can read documentation names it: a docs/ path, a .md suffix, or one
# of the well-known root files. Markdown under src/ or tests/ is code-adjacent
# (fixtures, packaged prompts) and is never treated as documentation.
DOC_READER = re.compile(r"docs[/\"']|\.md\b|README|CHANGELOG|AGENTS|CLAUDE|GEMINI|SKILL")
FULL_SHARDS = [0, 1, 2, 3]
# Paths whose change can alter the outcome of a sampled matrix case
# (tasks #2230, #2261): the redaction, JWK and audit scanners those matrices exercise,
# the matrix files themselves (the same names as SAMPLED_FILES in
# tests/param_sampling.py, which samples nothing else), the sampler, the CI
# decision and shard steps, and the dependency set. Any of them, or an unknown
# change set, runs every case; otherwise the default (representative) sample
# runs.
EXHAUSTIVE = re.compile(
    r"^src/unified_agent/(telemetry/|audit/|tools/public_relay\.py$)"
    r"|^tests/(test_[^/]*(redact|jwk|secret|audit)[^/]*\.py"
    r"|growth_budget\.py|param_sampling\.py|conftest\.py)$"
    r"|^scripts/ci_changes\.py$|^deploy/cloudbuild/|^\.(github|depot)/workflows/ci\.yml$"
    r"|^(pyproject\.toml|uv\.lock)$"
)
# Check runs come from either provider. Depot CI's GitHub App names each check
# "CI / <job>"; GitHub Actions names it "<job>" and records the workflow on the
# check suite, so only its runs in a suite of CI_WORKFLOW_PATH count (task #1787).
DEPOT_APP = "depot-code-access"
GITHUB_ACTIONS_APP = "github-actions"
CI_APPS = frozenset({DEPOT_APP, GITHUB_ACTIONS_APP})
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CI_PREFIX = "CI / "
PASSING = {"success", "skipped", "neutral"}
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.M)


def is_markdown_doc(path: str) -> bool:
    # packages/*/README.md ship with their packages, so they stay package code
    # and keep the JS stack running (Devin review, PR #811).
    return path.endswith(".md") and not path.startswith(("src/", "tests/", "packages/"))


def classify(paths: list[str]) -> dict[str, object]:
    """Map changed paths to job switches. An empty list runs everything."""
    paths = [p for p in (p.strip() for p in paths) if p]
    if not paths:
        return _outputs(python=True, scope="full", js=True, connectors=True, native=True)
    code = [p for p in paths if not is_markdown_doc(p)]
    connectors = any(CONNECTORS.search(p) for p in paths)
    native = any(NATIVE_DESKTOP.match(p) for p in paths)
    if not code:
        # Markdown only: lint + audit + the tests that read docs. No JS test reads
        # repo markdown (packages/*/tests only mock doc-path strings).
        return _outputs(python=True, scope="docs", js=False, connectors=connectors, native=native)
    if all(PACKAGES.match(p) for p in code):
        docs_too = len(code) != len(paths)
        return _outputs(
            python=docs_too, scope="docs", js=True, connectors=connectors, native=native
        )
    if all(PYTHON_ONLY.match(p) for p in code):
        return _outputs(python=True, scope="full", js=False, connectors=connectors, native=native)
    return _outputs(python=True, scope="full", js=True, connectors=connectors, native=native)


def exhaustive(paths: list[str]) -> bool:
    """Whether a change set must run every sampled matrix case. Empty means unknown."""
    paths = [p for p in (p.strip() for p in paths) if p]
    return not paths or any(EXHAUSTIVE.search(p) for p in paths)


def js_components(paths: list[str]) -> dict[str, bool]:
    """Split the JS leg without changing classify's persisted six-key contract.

    A frontend-only change needs SDK builds, but cannot change SDK test results.
    SDK changes need both their own tests and the consuming frontend checks.
    Unknown package/shared tooling changes run both components.
    """
    if not classify(paths)["js"]:
        return {"sdk": False, "web": False}
    code = [p for p in (p.strip() for p in paths) if p and not is_markdown_doc(p)]
    web_only = bool(code) and all(p.startswith("packages/web/") for p in code)
    return {"sdk": not web_only, "web": True}


def js_verification_result(needs: dict) -> tuple[bool, str]:
    """The stable `js` check joins components; missing/skipped required work fails."""
    gate = needs.get("gates", {})
    outputs = gate.get("outputs", {})
    batch = outputs.get("affected") == "true"
    if gate.get("result") != "success" or (outputs.get("full") != "true" and not batch):
        return False, "full JS verification requires successful gates"
    if outputs.get("js") != "true" or outputs.get("web") != "true":
        return False, "invalid or missing frontend dependency classification"
    if outputs.get("sdk") not in {"true", "false"}:
        return False, "invalid or missing SDK classification"
    required = {"gates", "web"}
    if outputs["sdk"] == "true":
        required.add("sdk")
    missing = sorted(job for job in required if needs.get(job, {}).get("result") != "success")
    if missing:
        return False, "required JS components did not succeed: " + ", ".join(missing)
    if any(value.get("result") not in {"success", "skipped"} for value in needs.values()):
        return False, "a JS component failed or was cancelled"
    return True, "all enabled JS components completed successfully for this revision"


def _outputs(
    *, python: bool, scope: str, js: bool, connectors: bool, native: bool
) -> dict[str, object]:
    return {
        "python": python,
        "python_scope": scope,
        "shards": FULL_SHARDS if scope == "full" else [0],
        "js": js,
        "connectors": connectors,
        "native": native,
    }


def skip_all() -> dict[str, object]:
    return {
        "python": False,
        "python_scope": "full",
        "shards": [0],
        "js": False,
        "connectors": False,
        "native": False,
    }


def format_outputs(outputs: dict[str, object]) -> str:
    lines = []
    for key, value in outputs.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, list):
            value = json.dumps(value)
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def candidates(scope: str, root: Path = REPO_ROOT) -> list[str]:
    tests = sorted(
        {
            str(p.relative_to(root))
            for pattern in ("test_*.py", "*_test.py")
            for p in (root / "tests").rglob(pattern)
        }
    )
    if scope == "full":
        return tests
    if scope == "docs":
        return [t for t in tests if DOC_READER.search((root / t).read_text(errors="replace"))]
    raise ValueError(f"unknown scope {scope!r}")


def shard_candidates(files: list[str], shard: int, total: int, root: Path = REPO_ROOT) -> list[str]:
    """This shard's share of ``files``, balanced by test-function count.

    GitHub-hosted runners have no timing-aware splitter (Depot's
    tests-run-action did that), so each file is weighted by its number of
    ``def test_`` lines plus one for collection, and assigned greedily to the
    lightest shard (largest first). Deterministic for a given tree: every shard
    computes the same partition, so each file runs exactly once.
    """
    if total < 1 or not 0 <= shard < total:
        raise ValueError(f"shard {shard} out of range for total {total}")

    def weight(path: str) -> int:
        try:
            return 1 + len(_TEST_DEF.findall((root / path).read_text(errors="replace")))
        except OSError:
            return 1

    loads = [0] * total
    owned: list[list[str]] = [[] for _ in range(total)]
    for path in sorted(files, key=lambda p: (-weight(p), p)):
        target = min(range(total), key=lambda i: (loads[i], i))
        loads[target] += weight(path)
        owned[target].append(path)
    return sorted(owned[shard])


# --- already-tested ---------------------------------------------------------


def _api(path: str, token: str) -> object:
    req = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def ci_check_name(run: dict, ci_suites: frozenset[int] = frozenset()) -> str | None:
    """The "CI / <job>" name of a CI check run from either provider, else None.

    Depot CI reports "CI / test (shard=0)" itself. GitHub Actions reports the
    bare job name "test (shard=0)"; it is a CI job only when its check suite
    belongs to the CI workflow (``ci_suites``), since Docker or Install Gate
    jobs carry bare names too.
    """
    app = (run.get("app") or {}).get("slug")
    name = run.get("name") or ""
    if app == DEPOT_APP:
        return name if name.startswith(CI_PREFIX) else None
    if app == GITHUB_ACTIONS_APP and name:
        suite = (run.get("check_suite") or {}).get("id")
        return CI_PREFIX + name if suite in ci_suites else None
    return None


def latest_ci_runs(
    check_runs: list[dict], ci_suites: frozenset[int] = frozenset()
) -> dict[str, dict]:
    """Newest run per CI job name, so a re-run that passed supersedes its failure."""
    latest: dict[str, dict] = {}
    for run in check_runs:
        name = ci_check_name(run, ci_suites)
        if name is None:
            continue
        if name not in latest or run.get("id", 0) > latest[name].get("id", 0):
            latest[name] = run
    return latest


def required_jobs(outputs: dict[str, object]) -> set[str]:
    """CI job checks a head must have passed, given what its classification enabled.

    Task #1787 folded the classifier, the held-PR rule and connector
    conformance into ``gates``, and protocol/client/web into
    ``js``. A head verified under the older job layout lacks these names, so
    it reads as "not already tested" and runs everything (fail-safe).
    """
    jobs = {"CI / gates", "CI / full-verification"}
    if outputs["python"]:
        jobs |= {f"CI / test (shard={i})" for i in outputs["shards"]}
    if outputs["js"]:
        jobs.add("CI / js")
    if outputs["native"]:
        jobs.add("CI / desktop-core")
    return jobs


def ci_passed(
    check_runs: list[dict], required: set[str], ci_suites: frozenset[int] = frozenset()
) -> tuple[bool, str]:
    """Every required job ran and passed, and no CI job on the head failed.

    A job that is absent is not a pass: the head must carry each check its own
    classification enabled (Devin review, PR #811). Either provider's runs
    count (task #1787).
    """
    runs = latest_ci_runs(check_runs, ci_suites)
    missing = sorted(required - set(runs))
    if missing:
        return False, f"PR head lacks required CI checks: {', '.join(missing)}"
    not_passed = sorted(
        f"{name}={run.get('conclusion') or run.get('status')}"
        for name, run in runs.items()
        if run.get("status") != "completed"
        or run.get("conclusion") not in PASSING
        or (name in required and run.get("conclusion") != "success")
    )
    if not_passed:
        return False, f"PR head CI did not pass: {', '.join(not_passed)}"
    return True, f"all {len(required)} required CI jobs passed on the PR head"


def all_check_runs(repo: str, sha: str, token: str) -> list[dict]:
    """Every check run on a commit, across pages; a short or inconsistent read raises."""
    runs: list[dict] = []
    page = 1
    while True:
        body = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}", token)
        batch = body.get("check_runs", [])
        runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if len(runs) != body.get("total_count", len(runs)):
        raise RuntimeError(f"read {len(runs)} of {body.get('total_count')} check runs")
    return runs


def ci_workflow_suites(repo: str, sha: str, token: str) -> frozenset[int]:
    """Check-suite ids of GitHub Actions CI workflow runs on a commit."""
    body = _api(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100", token)
    return frozenset(
        run["check_suite_id"]
        for run in body.get("workflow_runs", [])
        if run.get("path") == CI_WORKFLOW_PATH and run.get("check_suite_id") is not None
    )


def already_tested(sha: str, repo: str, token: str) -> tuple[bool, str]:
    pulls = _api(f"repos/{repo}/commits/{sha}/pulls", token)
    pr = next((p for p in pulls if p.get("merge_commit_sha") == sha), None)
    if pr is None:
        return False, "no merged pull request produced this commit"
    number, head, base = pr["number"], pr["head"]["sha"], pr["base"]["sha"]
    _git("fetch", "--no-tags", "--quiet", "origin", f"+refs/pull/{number}/head:refs/ci/pr-head")
    if _git("rev-parse", "refs/ci/pr-head") != head:
        return False, f"PR #{number} head moved after the API read"
    if _git("rev-parse", f"{sha}^{{tree}}") != _git("rev-parse", f"{head}^{{tree}}"):
        return False, f"main's tree differs from PR #{number} head {head[:12]} (merged behind main)"
    # Re-derive what the PR's own run had to cover, from the same rules.
    required = required_jobs(classify(_git("diff", "--name-only", f"{base}...{head}").splitlines()))
    runs = all_check_runs(repo, head, token)
    suites = frozenset()
    if any((r.get("app") or {}).get("slug") == GITHUB_ACTIONS_APP for r in runs):
        suites = ci_workflow_suites(repo, head, token)
    ok, why = ci_passed(runs, required, suites)
    return ok, f"PR #{number} head {head[:12]}: {why}"


def verification_result(needs: dict, event: str, draft: bool) -> tuple[bool, str]:
    """Enabled jobs must execute successfully; draft light checks never certify CI.

    An affected merge-queue batch (``affected=true``) certifies the jobs it
    actually ran, including when the batch pull request is a draft.
    """
    gates = needs.get("gates", {})
    if gates.get("result") != "success":
        return False, "gates (change classification and cheap checks) did not succeed"
    outputs = gates.get("outputs", {})
    affected = outputs.get("affected") == "true"
    if draft and event == "pull_request" and not affected:
        return False, "draft received light checks only; mark ready for full verification"
    if outputs.get("full") != "true" and not affected:
        return False, "full verification was not requested"
    flags = {key: outputs.get(key) for key in ("python", "js", "connectors", "native")}
    if any(value not in {"true", "false"} for value in flags.values()):
        return False, "invalid or missing classification outputs"
    # gates carries the classifier, the held-PR rule and connector conformance
    # as steps (task #1787), so its success covers them.
    required = {"gates"}
    if flags["python"] == "true":
        required.add("test")
    if flags["js"] == "true":
        required.add("js")
    if flags["native"] == "true":
        required.add("desktop-core")
    missing = sorted(job for job in required if needs.get(job, {}).get("result") != "success")
    if missing:
        return False, "required jobs did not succeed: " + ", ".join(missing)
    unexpected = sorted(
        job for job, value in needs.items() if value.get("result") not in {"success", "skipped"}
    )
    if unexpected:
        return False, "jobs failed or were cancelled: " + ", ".join(unexpected)
    return True, "all enabled jobs completed successfully for this revision"


def _affected_cli():
    # scripts/affected_tests.py imports this module. Run it only when selecting,
    # after this module has finished initializing, so the two imports don't cycle.
    name = "ua_affected_tests_cli"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    sys.modules[name] = _AFFECTED_MODULE
    assert _AFFECTED_SPEC is not None and _AFFECTED_SPEC.loader is not None
    _AFFECTED_SPEC.loader.exec_module(_AFFECTED_MODULE)
    return _AFFECTED_MODULE


# Python a batch may not certify with a subset. The selector misses a lazy
# import hop past the first src module and does not follow src modules whose
# dynamic imports it cannot read (affected_tests module docstring); scripts are
# loaded by src modules the same ways. Those batches run the full shards.
UNCERTIFIABLE_PYTHON = ("src/", "scripts/")


def affected_selection(paths: list[str], root: Path = REPO_ROOT) -> list[str] | None:
    """Test files that certify a merge-queue batch, or None for the full suite.

    ``paths`` are ``git diff --name-status`` lines (bare paths read as
    modified). Narrowing is allowed only where the selection is complete: no
    Python under :data:`UNCERTIFIABLE_PYTHON` changed, renamed or was deleted,
    and the selector reports no caveat.
    """
    cleaned = [path.strip() for path in paths if path.strip()]
    if not cleaned:
        return None
    module = _affected_cli()
    changes = module.affected_tests.parse_name_status(cleaned)
    for change in changes:
        for path in (change.path, change.old_path):
            if path and path.endswith(".py") and path.startswith(UNCERTIFIABLE_PYTHON):
                return None
    selected, selection = module.selected_paths(root, changes)
    if selected is None or selection.caveats:
        return None
    return selected


def _diff_names(base: str, head: str) -> list[str]:
    """``git diff --name-status`` lines from ``base`` to ``head``."""
    subprocess.run(
        ["git", "fetch", "--no-tags", "--quiet", "--depth=1", "origin", base],
        check=True,
    )
    out = subprocess.run(
        # --name-status keeps deletions and a rename's old path; the selector
        # finds whoever still imports a module that is gone.
        ["git", "diff", "--name-status", "-M", base, head],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("classify")
    sub.add_parser("exhaustive", help="changed paths on stdin -> true|false")
    sub.add_parser("verify", help="require successful enabled job results from CI_NEEDS")
    sub.add_parser("skip", help="GITHUB_OUTPUT lines that skip every test job")
    components = sub.add_parser("js-components", help="changed paths -> SDK/frontend switches")
    components.add_argument("--skip", action="store_true")
    sub.add_parser("verify-js", help="require successful SDK/frontend results from CI_NEEDS")
    cand = sub.add_parser("candidates")
    cand.add_argument("--scope", choices=("full", "docs"), required=True)
    cand.add_argument("--shard", type=int, help="0-based shard index (needs --total)")
    cand.add_argument("--total", type=int, help="number of shards (needs --shard)")
    tested = sub.add_parser("already-tested")
    tested.add_argument("--sha", required=True)
    tested.add_argument("--repo", required=True)
    aff = sub.add_parser("affected", help="changed paths -> affected test files, or ALL")
    aff.add_argument("--base", help="git diff base (with --head)")
    aff.add_argument("--head", help="git diff head (with --base)")
    args = parser.parse_args(argv)

    if args.cmd == "classify":
        sys.stdout.write(format_outputs(classify(sys.stdin.read().splitlines())))
        return 0
    if args.cmd == "exhaustive":
        print("true" if exhaustive(sys.stdin.read().splitlines()) else "false")
        return 0
    if args.cmd == "skip":
        sys.stdout.write(format_outputs(skip_all()))
        return 0
    if args.cmd == "js-components":
        outputs = (
            {"sdk": False, "web": False}
            if args.skip
            else js_components(sys.stdin.read().splitlines())
        )
        sys.stdout.write(format_outputs(outputs))
        return 0
    if args.cmd == "candidates":
        files = candidates(args.scope)
        if (args.shard is None) != (args.total is None):
            parser.error("--shard and --total go together")
        if args.total is not None:
            files = shard_candidates(files, args.shard, args.total)
        print("\n".join(files))
        return 0
    if args.cmd == "affected":
        if bool(args.base) != bool(args.head):
            parser.error("--base and --head go together")
        try:
            paths = (
                _diff_names(args.base, args.head) if args.base else sys.stdin.read().splitlines()
            )
            selected = affected_selection(paths)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"affected: {exc}", file=sys.stderr)
            selected = None
        print("ALL" if selected is None else "\n".join(selected))
        return 0
    if args.cmd in {"verify", "verify-js"}:
        try:
            needs = json.loads(os.environ["CI_NEEDS"])
            if args.cmd == "verify-js":
                ok, why = js_verification_result(needs)
            else:
                event = os.environ["CI_EVENT"]
                raw_draft = os.environ.get("CI_DRAFT", "")
                if event == "pull_request" and raw_draft not in {"true", "false"}:
                    raise ValueError("missing draft state")
                ok, why = verification_result(needs, event, raw_draft == "true")
        except (KeyError, ValueError, TypeError, AttributeError):
            ok, why = False, "invalid or missing CI result metadata"
        print(why)
        return 0 if ok else 1
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("already-tested: no GH_TOKEN; running everything")
        return 1
    try:
        ok, why = already_tested(args.sha, args.repo, token)
    except Exception as exc:  # noqa: BLE001 - any doubt means run the suite
        ok, why = False, f"{type(exc).__name__}: {exc}"
    print(f"already-tested: {'yes' if ok else 'no'} — {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
