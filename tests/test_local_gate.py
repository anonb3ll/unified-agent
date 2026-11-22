"""Regression tests for the shell local verification gate (tasks #1214, #2027)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
GATE = REPO_ROOT / "scripts" / "local-gate.sh"


def _fake_command(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def _run_gate(
    tmp_path: Path,
    *args: str,
    bash_env: str | None = None,
    expect_rc: int | None = 0,
    fragment: str = "- Test change.\n",
    extra_env: dict[str, str] | None = None,
    committed: str | None = None,
) -> tuple[list[list[str]], str]:
    """Run a copy of local-gate.sh with fake ``uv``/``make`` on PATH.

    ``bash_env`` is written to a file passed as ``BASH_ENV``, which a
    non-interactive bash sources before the script runs. Tests use it to
    simulate an older bash (``enable -n mapfile``) or to make a leg abort.
    Returns the recorded ``uv`` argv lists and stdout+stderr.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(GATE, scripts / "local-gate.sh")
    shutil.copy2(REPO_ROOT / "scripts" / "changelog_assemble.py", scripts)
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "2140.fixed.md").write_text(fragment)
    shutil.copy2(
        REPO_ROOT / "scripts" / "check_staged_conflict_markers.py",
        scripts / "check_staged_conflict_markers.py",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_command(
        fake_bin / "uv",
        'printf "%s\\0" "$@" >> "$UA_TEST_ARG_LOG"; printf "\\0" >> "$UA_TEST_ARG_LOG"; '
        'if [[ "$1" == run && "$2" == python && "$3" == -c ]]; then shift 3; python3 -c "$@"; '
        'elif [[ "$1" == run && "$2" == python && "$3" == scripts/changelog_assemble.py ]]; '
        'then shift 2; python3 "$@"; '
        'elif [[ "$1" == run && "$2" == python && "$3" == scripts/affected_tests.py ]]; '
        'then printf "%b" "${UA_TEST_AFFECTED_OUT:-}"; echo "selector explain" >&2; '
        'exit "${UA_TEST_AFFECTED_RC:-0}"; fi',
    )
    _fake_command(fake_bin / "make", 'printf "%s\\n" "$*" >> "$UA_TEST_MAKE_LOG"')

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    if committed is not None:
        # A real commit plus the receipt writer; ``committed`` is "clean" or "dirty".
        shutil.copy2(REPO_ROOT / "scripts" / "gate_route.py", scripts)
        package = tmp_path / "src" / "unified_agent"
        package.mkdir(parents=True)
        (package / "__init__.py").touch()
        shutil.copy2(REPO_ROOT / "src" / "unified_agent" / "check_receipts.py", package)
        (tmp_path / ".gitignore").write_text("bin/\n*.log\n__pycache__/\n")
        git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
        subprocess.run([*git, "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run([*git, "commit", "-qm", "base"], cwd=tmp_path, check=True)
        if committed == "dirty":
            (tmp_path / "untracked.txt").write_text("x\n")
    log_path = tmp_path / "uv-args.log"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["UA_TEST_ARG_LOG"] = str(log_path)
    env["UA_TEST_MAKE_LOG"] = str(tmp_path / "make.log")
    env.pop("BASH_ENV", None)
    env.update(extra_env or {})
    if bash_env is not None:
        bash_env_file = tmp_path / "bash-env.sh"
        bash_env_file.write_text(bash_env + "\n")
        env["BASH_ENV"] = str(bash_env_file)

    result = subprocess.run(
        ["bash", str(scripts / "local-gate.sh"), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    if expect_rc is not None:
        assert result.returncode == expect_rc, output
    else:
        assert result.returncode != 0, output
    calls = [
        [item.decode() for item in record.split(b"\0")]
        for record in (log_path.read_bytes() if log_path.exists() else b"").split(b"\0\0")
        if record
    ]
    return calls, output


def test_targeted_args_preserve_quotes_and_globs(tmp_path):
    calls, _ = _run_gate(
        tmp_path,
        "--targeted",
        'tests/test_api.py -k "failed login" tests/test_*.py',
    )

    assert ["sync", "--all-extras", "--locked"] in calls
    assert [
        "run",
        "python",
        "-c",
    ] == next(call[:3] for call in calls if call[:3] == ["run", "python", "-c"])
    assert [
        "run",
        "pytest",
        "-q",
        "tests/test_api.py",
        "-k",
        "failed login",
        "tests/test_*.py",
    ] in calls


@pytest.mark.parametrize(("state", "recorded"), [("clean", True), ("dirty", False)])
def test_gate_records_a_receipt_per_leg_only_on_a_clean_tree(tmp_path, state, recorded):
    _, output = _run_gate(
        tmp_path,
        "--fast",
        committed=state,
        extra_env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    store = tmp_path / ".git" / "ua-check-receipts.jsonl"
    assert store.exists() is recorded, output
    if not recorded:
        return
    rows = [json.loads(line) for line in store.read_text().splitlines()]
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert {row["tree"] for row in rows} == {tree}
    by_check = {row["check"]: row for row in rows}
    assert by_check["ruff check ."]["conclusion"] == "success"
    assert (by_check["pytest"]["conclusion"], by_check["pytest"]["scope"]) == (
        "skipped",
        "partial",
    )


def test_local_gate_installs_all_ci_extras(tmp_path):
    calls, _ = _run_gate(tmp_path, "--fast")

    assert calls[0] == ["sync", "--all-extras", "--locked"]


@pytest.mark.parametrize("args", [(), ("--fast",), ("--targeted", "tests/test_api.py")])
@pytest.mark.parametrize("valid", [True, False])
def test_fragment_validation_controls_gate_result_in_every_mode(tmp_path, args, valid):
    fragment = "- Valid entry.\n" if valid else "Missing the required bullet.\n"
    calls, output = _run_gate(tmp_path, *args, fragment=fragment, expect_rc=0 if valid else 1)

    assert ["run", "python", "scripts/changelog_assemble.py", "--check"] in calls
    assert f"--- leg: changelog fragments -> {'PASS' if valid else 'FAIL'}" in output
    assert f"GATE RESULT: {'PASS' if valid else 'FAIL'}" in output
    assert (tmp_path / "changelog.d" / "2140.fixed.md").read_text() == fragment
    assert "smoke" in (tmp_path / "make.log").read_text()
    if args:
        assert ["run", "pytest", "-q"] not in calls
    if args and args[0] == "--targeted":
        assert ["run", "pytest", "-q", "tests/test_api.py"] in calls


def test_targeted_many_paths_runs_and_preserves_full_leg_name(tmp_path):
    paths = [f"tests/test_cross_module_regression_{index:02d}.py" for index in range(40)]
    targeted = " ".join(paths)
    calls, output = _run_gate(tmp_path, "--targeted", targeted)

    assert ["run", "pytest", "-q", *paths] in calls
    leg_name = f"pytest (targeted: {targeted})"
    assert f"--- leg: {leg_name} ---" in output
    assert f"{leg_name} (" in output  # The summary also retains the full leg name.
    assert "GATE RESULT: PASS" in output


def _pytest_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[:2] == ["run", "pytest"]]


def test_full_mode_runs_full_pytest_leg(tmp_path):
    calls, output = _run_gate(tmp_path)

    assert ["run", "pytest", "-q"] in calls
    assert "pytest (full suite) (" in output
    assert "GATE RESULT: PASS" in output


def test_targeted_runs_pytest_leg_on_bash_without_mapfile(tmp_path):
    """macOS /bin/bash is 3.2, which has no ``mapfile`` builtin (task #2027).

    Before the fix, the missing builtin left the argv array unset, the
    ``set -u`` error aborted the whole pytest ``case`` and the gate still
    printed PASS without ever running pytest.
    """
    marker = tmp_path / "bash-env-sourced"
    calls, output = _run_gate(
        tmp_path,
        "--targeted",
        'tests/test_codex_llm_client.py -k "a b"',
        bash_env=f"enable -n mapfile; : > {marker}",
    )

    assert marker.exists()  # the mapfile-less shell really ran the gate
    assert ["run", "pytest", "-q", "tests/test_codex_llm_client.py", "-k", "a b"] in calls
    assert 'pytest (targeted: tests/test_codex_llm_client.py -k "a b") (' in output
    assert "GATE RESULT: PASS" in output


def test_gate_fails_when_requested_pytest_leg_does_not_run(tmp_path):
    """A pytest leg that silently never runs must not read as PASS (task #2027).

    A readonly ``TARGETED_ARR`` makes the targeted branch abort the same way
    the missing ``mapfile`` did: bash reports the error and skips the rest of
    the compound command, then carries on with the other legs.
    """
    calls, output = _run_gate(
        tmp_path,
        "--targeted",
        "tests/test_codex_llm_client.py",
        bash_env="readonly TARGETED_ARR",
        expect_rc=None,
    )

    assert _pytest_calls(calls) == [
        ["run", "pytest", "tests/test_attribution_coverage.py", "-q"],
        [
            "run",
            "pytest",
            "tests/test_audit_ledger_chain.py::test_cli_verify_rejects_tampered_fixture",
            "-q",
        ],
    ]
    assert "pytest (targeted: tests/test_codex_llm_client.py)" in output
    assert "did not run" in output
    assert "GATE RESULT: FAIL" in output
    assert "GATE RESULT: PASS" not in output


def test_targeted_bad_quoting_fails_with_parser_error(tmp_path):
    calls, output = _run_gate(
        tmp_path,
        "--targeted",
        'tests/test_api.py -k "unterminated',
        expect_rc=2,
    )

    assert "--targeted must contain valid shell-style quoting" in output
    assert "No closing quotation" in output  # the parser's own reason is not discarded
    assert _pytest_calls(calls) == []


def test_affected_runs_pytest_on_the_selected_files(tmp_path):
    """Task #2231: --affected runs what scripts/affected_tests.py selects."""
    calls, output = _run_gate(
        tmp_path,
        "--affected",
        extra_env={"UA_TEST_AFFECTED_OUT": "tests/test_a.py\\ntests/sub/test_b.py\\n"},
    )

    assert ["run", "python", "scripts/affected_tests.py", "--explain"] in calls
    assert ["run", "pytest", "-q", "tests/test_a.py", "tests/sub/test_b.py"] in calls
    assert "selector explain" in output
    assert "pytest (affected: 2 files) (" in output
    assert "GATE RESULT: PASS" in output


def test_affected_skips_pytest_when_the_diff_needs_the_full_suite(tmp_path):
    calls, output = _run_gate(tmp_path, "--affected", extra_env={"UA_TEST_AFFECTED_OUT": "ALL\\n"})

    assert not [call for call in _pytest_calls(calls) if call[2:3] == ["-q"]]
    assert "pytest (affected) -> SKIP (this diff needs the full suite" in output
    assert "GATE RESULT: PASS" in output


def test_affected_fails_when_test_selection_fails(tmp_path):
    calls, output = _run_gate(
        tmp_path, "--affected", extra_env={"UA_TEST_AFFECTED_RC": "2"}, expect_rc=None
    )

    assert not [call for call in _pytest_calls(calls) if call[2:3] == ["-q"]]
    assert "test selection failed" in output
    assert "GATE RESULT: FAIL" in output
