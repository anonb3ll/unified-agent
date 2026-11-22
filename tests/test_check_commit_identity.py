"""Tests for scripts/check_commit_identity.py (task #2079).

Mac merge commits fell back to git's auto-detected identity (an OS username
at a ``.lan`` host): unattributed, and it leaks a host name into history. The
guard fails when a commit in a range carries such an identity. Each test builds
a throwaway repository with crafted GIT_AUTHOR_* / GIT_COMMITTER_* values.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SCRIPT = SCRIPTS / "check_commit_identity.py"
sys.path.insert(0, str(SCRIPTS))
import check_commit_identity as guard  # noqa: E402

FLEET = "fleet@example.com"
GOOD = ("Desktop_Agent1", FLEET)


def _clean_env(home: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GIT_", "UA_COMMIT_", "UA_OPERATOR_")) and k != "UA_AGENT_LABELS"
    }
    env.update(
        HOME=str(home),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    return env


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    env = _clean_env(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    _commit(path, "base", author=GOOD, committer=GOOD)
    subprocess.run(["git", "tag", "base"], cwd=path, check=True, env=env)
    return path


def _commit(path: Path, msg: str, *, author: tuple[str, str], committer: tuple[str, str]) -> str:
    env = _clean_env(path.parent)
    env.update(
        GIT_AUTHOR_NAME=author[0],
        GIT_AUTHOR_EMAIL=author[1],
        GIT_COMMITTER_NAME=committer[0],
        GIT_COMMITTER_EMAIL=committer[1],
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", msg], cwd=path, check=True, env=env
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, env=env, capture_output=True, text=True
    )
    return out.stdout.strip()


def _run(path: Path, *args: str, env_extra: dict[str, str] | None = None):
    env = _clean_env(path.parent)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(path), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _check(path: Path, *extra: str, env_extra: dict[str, str] | None = None):
    return _run(path, "--range", "base..HEAD", "--allow-email", FLEET, *extra, env_extra=env_extra)


# --- end-to-end over a crafted repository -----------------------------------


def test_good_identity_passes(repo: Path):
    _commit(repo, "feat: good", author=GOOD, committer=GOOD)
    result = _check(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 commit" in result.stdout


def test_lan_author_fails_and_names_the_commit_and_fix(repo: Path):
    sha = _commit(repo, "feat: bad", author=("someuser", "someuser@somehost.lan"), committer=GOOD)
    result = _check(repo)
    assert result.returncode == 1
    assert sha[:12] in result.stderr
    assert "author" in result.stderr
    assert ".lan" in result.stderr
    assert "--amend" in result.stderr
    assert "GIT_COMMITTER_EMAIL" in result.stderr


def test_good_author_but_local_committer_fails(repo: Path):
    sha = _commit(repo, "merge", author=GOOD, committer=("someuser", "someuser@somehost.local"))
    result = _check(repo)
    assert result.returncode == 1
    assert sha[:12] in result.stderr
    assert "committer" in result.stderr


WEB_FLOW = ("GitHub", "noreply@github.com")


def test_github_web_merge_committer_passes(repo: Path):
    # A GitHub web or squash merge: the author is the merging account (its
    # profile name, configured explicitly as the operator's name, at the fleet
    # address), the committer is GitHub's web-flow identity.
    _commit(repo, "Merge PR", author=("Operator Person", FLEET), committer=WEB_FLOW)
    result = _check(repo, env_extra={"UA_OPERATOR_NAME": "Operator Person"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_web_flow_committer_does_not_exempt_the_author_name(repo: Path):
    # Review finding 1 (#1208): a claimed web-flow committer is self-declared
    # metadata, not proof of GitHub provenance, so the author name is still
    # checked.
    _commit(repo, "Merge PR", author=("someuser", FLEET), committer=WEB_FLOW)
    result = _check(repo)
    assert result.returncode == 1, result.stdout
    assert "author" in result.stderr


@pytest.mark.parametrize(
    "email",
    [
        "someuser@somehost.localdomain",
        "someuser@somehost.home",
        "someuser@(none)",
        "someuser@somehost",
        "no-at-sign",
        # Devin review (#1208): shapes the first denylist missed.
        "someuser@somehost.home.arpa",
        "someuser@somehost.lan.",
        "someuser@somehost.internal",
        "someuser@10.1.2.3",
        "someuser@[10.1.2.3]",
        "someuser@[IPv6:fd00::1]",
    ],
)
def test_auto_detected_host_domains_fail(repo: Path, email: str):
    _commit(repo, "bad", author=GOOD, committer=("Desktop_Agent1", email))
    result = _check(repo)
    assert result.returncode == 1, result.stdout
    assert "host" in result.stderr
    assert "somehost" not in result.stderr
    assert "someuser" not in result.stderr


def test_unlisted_email_fails_without_echoing_it(repo: Path):
    # Review finding 2 (#1208): diagnostics must not republish the address.
    sha = _commit(
        repo, "bad", author=("Desktop_Agent1", "private.person@example.org"), committer=GOOD
    )
    result = _check(repo)
    assert result.returncode == 1
    assert sha[:12] in result.stderr
    assert "not an allowed fleet address" in result.stderr
    assert "private.person" not in result.stderr
    assert "example.org" not in result.stderr


def test_bare_os_username_with_fleet_email_fails_without_echoing_it(repo: Path):
    _commit(repo, "bad", author=GOOD, committer=("someuser", FLEET))
    result = _check(repo)
    assert result.returncode == 1
    assert "label" in result.stderr
    assert "someuser" not in result.stderr
    # The allowed fleet address is not echoed either.
    assert FLEET not in result.stderr


def test_lan_identity_is_redacted_to_its_domain_class(repo: Path):
    sha = _commit(repo, "bad", author=GOOD, committer=("someuser", "someuser@someusers-box.lan"))
    result = _check(repo)
    assert result.returncode == 1
    assert sha[:12] in result.stderr
    assert "committer" in result.stderr
    assert ".lan" in result.stderr
    assert "someuser" not in result.stderr
    assert "someusers-box" not in result.stderr


def test_no_raw_output_mode_hint_points_to_local_git_log(repo: Path):
    # Round 3 (#1208): there is no raw-identity output mode; the fix text says
    # how to see raw identities locally with git itself.
    _commit(repo, "bad", author=GOOD, committer=("someuser", "someuser@somehost.lan"))
    result = _check(repo)
    assert result.returncode == 1
    assert "git log --format=" in result.stderr
    assert "someuser" not in result.stderr
    assert _check(repo, "--reveal").returncode == 2


def test_label_match_is_case_insensitive(repo: Path):
    _commit(repo, "ok", author=("desktop_agent2", FLEET), committer=("server_Agent2", FLEET))
    assert _check(repo).returncode == 0


def test_service_noreply_identities_pass(repo: Path):
    _commit(repo, "ok", author=("Claude", "noreply@anthropic.com"), committer=GOOD)
    assert _check(repo).returncode == 0


def test_users_noreply_address_passes_only_when_explicitly_allowed(repo: Path):
    # Devin review (#1208): an address passes only by exact allowlist match,
    # with no suffix-based allowance.
    noreply = "123+x@users.noreply.github.com"
    _commit(repo, "ok", author=("Desktop_Agent1", noreply), committer=GOOD)
    assert _check(repo).returncode == 1
    assert _check(repo, "--allow-email", noreply).returncode == 0


@pytest.mark.parametrize(
    "identity",
    [
        ("someuser", "noreply@anthropic.com"),
        ("someuser", "noreply@github.com"),
        ("Desktop_Agent1", "noreply@anthropic.com"),
        ("someuser", "123+someuser@users.noreply.github.com"),
    ],
)
def test_service_address_does_not_exempt_the_name(repo: Path, identity: tuple[str, str]):
    # Review finding 1 (#1208): a service address is accepted only as an exact
    # (name, email) pair, or for a users.noreply address with a fleet label.
    _commit(repo, "bad", author=identity, committer=identity)
    result = _check(repo)
    assert result.returncode == 1, result.stdout
    assert "author" in result.stderr and "committer" in result.stderr


def test_extra_names_and_emails_from_env(repo: Path):
    _commit(repo, "ok", author=("Cursor Agent", "bot@cursor.example"), committer=GOOD)
    assert _check(repo).returncode == 1
    env = {
        "UA_COMMIT_ALLOWED_NAMES": "Cursor Agent",
        "UA_COMMIT_ALLOWED_EMAILS": f"{FLEET}, bot@cursor.example",
    }
    result = _run(repo, "--range", "base..HEAD", env_extra=env)
    assert result.returncode == 0, result.stderr


LEGACY_CURSOR = "Cursor Agent <cursoragent@cursor.com>"


def test_legacy_cursor_identity_needs_an_explicit_pair(repo: Path):
    # Review finding 3 (#1208): the merged legacy Cursor commit is allowed only
    # through an explicit identity pair, documented in docs/COLLABORATION.md.
    _commit(
        repo,
        "docs",
        author=("Cursor Agent", "cursoragent@cursor.com"),
        committer=("Cursor Agent", "cursoragent@cursor.com"),
    )
    assert _check(repo).returncode == 1
    result = _check(repo, "--allow-identity", LEGACY_CURSOR)
    assert result.returncode == 0, result.stderr
    env = {"UA_COMMIT_ALLOWED_IDENTITIES": f"Other Bot <bot@example.net>; {LEGACY_CURSOR}"}
    assert _check(repo, env_extra=env).returncode == 0


def test_identity_pair_does_not_allow_its_parts_separately(repo: Path):
    _commit(repo, "bad", author=("Cursor Agent", FLEET), committer=GOOD)
    _commit(repo, "bad", author=("Desktop_Agent1", "cursoragent@cursor.com"), committer=GOOD)
    result = _check(repo, "--allow-identity", LEGACY_CURSOR)
    assert result.returncode == 1
    assert "2 of 2 commits" in result.stderr


def test_malformed_allow_identity_is_a_usage_error(repo: Path):
    assert _check(repo, "--allow-identity", "no brackets").returncode == 2


def test_operator_name_env_is_an_allowed_name(repo: Path):
    _commit(repo, "ok", author=("Operator Person", FLEET), committer=GOOD)
    assert _check(repo).returncode == 1
    result = _check(repo, env_extra={"UA_OPERATOR_NAME": "Operator Person"})
    assert result.returncode == 0, result.stderr


def test_git_config_email_is_never_an_implicit_default(repo: Path):
    # Devin review (#1208): `git config user.email <x>@<host>.home.arpa` used to
    # become the allowed address. There is no implicit default any more.
    for configured in (FLEET, "someuser@somehost.home.arpa"):
        env = _clean_env(repo.parent)
        subprocess.run(["git", "config", "user.email", configured], cwd=repo, check=True, env=env)
        _commit(repo, "x", author=("Desktop_Agent1", configured), committer=GOOD)
        result = _run(repo, "--range", "base..HEAD")
        assert result.returncode == 2, result.stdout
        assert "--allow-email" in result.stderr


@pytest.mark.parametrize(
    "allowed",
    [
        "someuser@somehost.home.arpa",
        "someuser@somehost.lan",
        "someuser@somehost.lan.",
        "someuser@10.1.2.3",
        "some user@example.com",
        "<fleet@example.com>",
        "no-at-sign",
    ],
)
def test_host_like_or_malformed_allowed_address_is_a_usage_error(repo: Path, allowed: str):
    result = _run(repo, "--range", "base..HEAD", "--allow-email", allowed)
    assert result.returncode == 2
    assert "--allow-email entry 1" in result.stderr
    env_result = _run(
        repo, "--range", "base..HEAD", env_extra={"UA_COMMIT_ALLOWED_EMAILS": allowed}
    )
    assert env_result.returncode == 2
    assert "UA_COMMIT_ALLOWED_EMAILS entry 1" in env_result.stderr


def test_unlisted_novel_domain_fails_without_any_denylist_entry(repo: Path):
    # Correctness does not depend on the host denylist: an address that is not
    # exactly allowed fails even when its domain shape is unknown to it.
    _commit(repo, "bad", author=("Desktop_Agent1", "someuser@somehost.corp"), committer=GOOD)
    result = _check(repo)
    assert result.returncode == 1
    assert "not an allowed fleet address" in result.stderr


def test_no_allowed_email_configured_is_a_usage_error(repo: Path):
    _commit(repo, "ok", author=GOOD, committer=GOOD)
    result = _run(repo, "--range", "base..HEAD")
    assert result.returncode == 2
    assert "UA_COMMIT_ALLOWED_EMAILS" in result.stderr


def test_host_like_git_config_email_is_not_a_default_either(repo: Path):
    env = _clean_env(repo.parent)
    subprocess.run(
        ["git", "config", "user.email", "someuser@somehost.lan"], cwd=repo, check=True, env=env
    )
    result = _run(repo, "--range", "base..HEAD")
    assert result.returncode == 2


def test_range_excludes_commits_before_base(repo: Path):
    # The bad commit sits below the range base, so it is not checked.
    _commit(repo, "bad", author=("someuser", "someuser@somehost.lan"), committer=GOOD)
    subprocess.run(["git", "tag", "-f", "base"], cwd=repo, check=True, env=_clean_env(repo.parent))
    _commit(repo, "good", author=GOOD, committer=GOOD)
    assert _check(repo).returncode == 0


def test_max_count_checks_only_the_newest_commits(repo: Path):
    _commit(repo, "bad", author=("someuser", "someuser@somehost.lan"), committer=GOOD)
    _commit(repo, "good", author=GOOD, committer=GOOD)
    assert _check(repo).returncode == 1
    result = _check(repo, "--max-count", "1")
    assert result.returncode == 0, result.stderr
    assert "1 commit" in result.stdout


def test_empty_range_passes(repo: Path):
    result = _check(repo)
    assert result.returncode == 0
    assert "0 commits" in result.stdout


def test_bad_range_is_a_usage_error(repo: Path):
    result = _run(repo, "--range", "no-such-ref..HEAD", "--allow-email", FLEET)
    assert result.returncode == 2


# --- units -----------------------------------------------------------------


def test_fleet_labels_come_from_the_config_catalog():
    labels = guard.load_fleet_labels()
    assert {"Desktop_Agent1", "server_agent2", "cloud_agent1", "Desktop_Agent7"} <= labels


@pytest.mark.parametrize(
    "email,host_like",
    [
        ("a@b.lan", True),
        ("a@B.LOCAL", True),
        ("a@b", True),
        ("a@(none)", True),
        ("", True),
        ("a@b.home.arpa", True),
        ("a@b.lan.", True),
        ("a@b.example.com.", True),
        ("a@b.internal", True),
        ("a@192.168.1.1", True),
        ("a@::1", True),
        ("a@[192.168.1.1]", True),
        ("a@[ipv6:fd00::1]", True),
        ("a@example.com", False),
        ("1+a@users.noreply.github.com", False),
    ],
)
def test_host_like_email(email: str, host_like: bool):
    assert (guard.host_like_reason(email) is not None) is host_like


# --- round-2 review (#1208): no raw identity on any error path ----------------

PRIVATE_NAME = "Private Person"
PRIVATE_EMAIL = "private.person@example.org"


def _assert_undisclosed(result, *markers: str) -> None:
    out = result.stdout + result.stderr
    for marker in (PRIVATE_NAME, PRIVATE_EMAIL, "private.person", "example.org", *markers):
        assert marker not in out, f"{marker!r} disclosed in: {out!r}"


def test_malformed_cli_identity_is_not_echoed(repo: Path):
    result = _check(repo, "--allow-identity", f"{PRIVATE_NAME} <{PRIVATE_EMAIL}")
    assert result.returncode == 2
    assert "--allow-identity" in result.stderr
    _assert_undisclosed(result)


def test_malformed_env_identity_is_not_echoed(repo: Path):
    env = {"UA_COMMIT_ALLOWED_IDENTITIES": f"{PRIVATE_NAME} {PRIVATE_EMAIL}>"}
    result = _check(repo, env_extra=env)
    assert result.returncode == 2
    assert "UA_COMMIT_ALLOWED_IDENTITIES" in result.stderr
    _assert_undisclosed(result)


def test_unquoted_identity_argument_is_not_echoed_by_argparse(repo: Path):
    # A misquoted shell line splits the pair into stray arguments, which
    # argparse would normally print back as "unrecognized arguments: ...".
    result = _check(repo, "--allow-identity", "Private", "Person", f"<{PRIVATE_EMAIL}>")
    assert result.returncode == 2
    _assert_undisclosed(result)


def test_invalid_option_value_is_not_echoed(repo: Path):
    result = _check(repo, "--max-count", PRIVATE_EMAIL)
    assert result.returncode == 2
    _assert_undisclosed(result)


def test_missing_repo_path_and_git_stderr_are_not_echoed(tmp_path: Path):
    missing = tmp_path / "secret-marker-dir"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(missing), "--allow-email", FLEET],
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "git log failed" in result.stderr
    _assert_undisclosed(result, "secret-marker", str(tmp_path))


def test_git_failure_reports_exit_code_not_stderr(repo: Path):
    result = _run(repo, "--range", "no-such-ref..HEAD", "--allow-email", FLEET)
    assert result.returncode == 2
    assert "exit 128" in result.stderr
    # git's own message ("unknown revision ...") is not passed through.
    assert "unknown revision" not in result.stderr


def test_range_with_unsafe_characters_is_not_echoed(repo: Path):
    result = _run(repo, "--range", f"{PRIVATE_NAME} <{PRIVATE_EMAIL}>", "--allow-email", FLEET)
    assert result.returncode == 2
    _assert_undisclosed(result)


def test_unreadable_catalog_error_does_not_name_the_path(tmp_path: Path):
    with pytest.raises(guard.UsageError) as exc:
        guard.load_fleet_labels(tmp_path / "secret-marker" / "config.py")
    assert "secret-marker" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def _fingerprints(text: str) -> list[str]:
    import re

    return re.findall(r"fp ([0-9a-f]{10})", text)


def test_fingerprint_is_keyed_per_run_not_a_bare_hash(repo: Path):
    import hashlib

    bad = ("someuser", "someuser@somehost.lan")
    _commit(repo, "bad", author=GOOD, committer=bad)
    _commit(repo, "bad", author=GOOD, committer=bad)
    first = _check(repo)
    second = _check(repo)
    fps1, fps2 = _fingerprints(first.stderr), _fingerprints(second.stderr)
    assert len(fps1) == 2 and fps1[0] == fps1[1]  # correlatable within a run
    assert fps1[0] != fps2[0]  # not across runs
    # A dictionary of candidate addresses cannot be matched against the output.
    assert hashlib.sha256(bad[1].encode()).hexdigest()[:10] not in first.stderr


# --- round-3 review (#1208): output is built only from allowlisted parts ------

MARKERS = (
    "private.person",
    "example.org",
    "secret-host",
    "Brien",
    "Prïvate",
    "exämple",
    "backslash-marker",
    "newline-marker",
)
HOSTILE_VALUES = (
    "private.person@example.org",
    "secret-host.lan",
    "O'Brien <private.person@example.org>",
    'O\'Brien "Q" <private.person@example.org',
    "mixed \"quotes\" 'private.person'",
    "C:\\backslash-marker\\private.person",
    "line1\nnewline-marker private.person@example.org",
    "Prïvate Persön <prïvate@exämple.org>",
    "--private.person@example.org",
    "secret-host",
)
OPTIONS = ("--range", "--repo", "--allow-email", "--allow-name", "--allow-identity", "--max-count")
ENV_VARS = (
    "UA_COMMIT_ALLOWED_IDENTITIES",
    "UA_COMMIT_ALLOWED_EMAILS",
    "UA_COMMIT_ALLOWED_NAMES",
    "UA_AGENT_LABELS",
    "UA_OPERATOR_NAME",
    "UA_OPERATOR_ALIASES",
)


def _disclosures(result) -> list[str]:
    out = result.stdout + result.stderr
    return [m for m in MARKERS if m.lower() in out.lower()]


def test_no_hostile_input_is_ever_disclosed(repo: Path):
    # Property-style matrix: every hostile value in every option, as stray
    # arguments, and in every environment variable, on success, violation,
    # usage-error and git-error paths alike. No marker may appear in stdout or
    # stderr, whatever the exit code.
    _commit(repo, "ok", author=GOOD, committer=GOOD)
    subprocess.run(["git", "tag", "clean"], cwd=repo, check=True, env=_clean_env(repo.parent))
    _commit(
        repo,
        "hostile",
        author=("O'Brien Prïvate", "private.person@example.org"),
        committer=("secret-host", "newline-marker@secret-host.lan"),
    )
    cases: list[tuple[list[str], dict[str, str]]] = []
    for value in HOSTILE_VALUES:
        for option in OPTIONS:
            cases.append(([option, value], {}))
            cases.append(([f"{option}={value}"], {}))
        cases.append(([value], {}))
        cases.append((["--allow-identity", "Desktop_Agent1", value], {}))
        for var in ENV_VARS:
            cases.append(([], {var: value}))
    leaks = []
    codes = set()
    for argv, env in cases:
        # A valid base range first (clean: exit 0; with the hostile commit:
        # exit 1); a hostile --range later in argv overrides it.
        for base in ("base..clean", "base..HEAD"):
            result = _run(repo, "--range", base, "--allow-email", FLEET, *argv, env_extra=env)
            codes.add(result.returncode)
            found = _disclosures(result)
            if found:
                leaks.append((base, argv, sorted(env), result.returncode, found))
    assert not leaks, leaks
    assert {0, 1, 2} <= codes  # success, violation and error paths were exercised


@pytest.mark.parametrize("tag", ["private.person@example.org", "secret-host.lan"])
def test_identity_shaped_range_is_not_echoed_on_success_or_git_error(repo: Path, tag: str):
    _commit(repo, "ok", author=GOOD, committer=GOOD)
    subprocess.run(["git", "tag", tag], cwd=repo, check=True, env=_clean_env(repo.parent))
    ok = _run(repo, "--range", tag, "--allow-email", FLEET)
    assert ok.returncode == 0, ok.stderr
    assert "the requested range" in ok.stdout
    missing = _run(repo, "--range", f"{tag}-missing", "--allow-email", FLEET)
    assert missing.returncode == 2
    for result in (ok, missing):
        assert not _disclosures(result)


def test_apostrophe_and_mixed_quotes_in_option_values_are_not_echoed(repo: Path):
    for value in ("O'Brien <private.person@example.org>", "a\"b'c private.person"):
        result = _check(repo, "--max-count", value)
        assert result.returncode == 2
        assert "invalid arguments (values not shown)" in result.stderr
        assert not _disclosures(result)


def test_help_is_fixed_text(repo: Path):
    result = _run(repo, "--help")
    assert result.returncode == 0
    assert "--range" in result.stdout and "--allow-identity" in result.stdout


OUTPUT_FUNCTIONS = ("emit", "_write_fallback")
SINK_CALLS = frozenset(
    {"print", "breakpoint", "input", "os.write", "os.system", "pprint", "pprint.pprint"}
)
SINK_ATTRS = frozenset(
    {
        "write",
        "writelines",
        "print_help",
        "print_usage",
        "_print_message",
        "print_exc",
        "print_exception",
        "print_stack",
        "warn",
        "warn_explicit",
        "showwarning",
    }
)
SINK_MODULES = frozenset({"logging", "warnings", "traceback", "pprint", "pdb", "code"})


def _audit_output_sinks(source: str) -> list[tuple[str, str, int]]:
    """Every output sink in ``source`` outside emit()/_write_fallback().

    Walks module level, class bodies, and sync and async functions alike.
    """
    import ast as _ast

    offenders: list[tuple[str, str, int]] = []

    def visit(node, scope: str) -> None:
        for child in _ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, _ast.FunctionDef | _ast.AsyncFunctionDef | _ast.ClassDef):
                child_scope = child.name
            if scope not in OUTPUT_FUNCTIONS:
                check(child, scope)
            visit(child, child_scope)

    def check(node, scope: str) -> None:
        if isinstance(node, _ast.Import | _ast.ImportFrom):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            if any(n.split(".")[0] in SINK_MODULES for n in names):
                offenders.append((scope, "import", node.lineno))
        if isinstance(node, _ast.Raise) and node.exc is not None:
            exc = node.exc
            if isinstance(exc, _ast.Call) and _ast.unparse(exc.func) == "SystemExit" and exc.args:
                offenders.append((scope, "SystemExit(arg)", node.lineno))
        if not isinstance(node, _ast.Call):
            return
        target = _ast.unparse(node.func)
        attr = node.func.attr if isinstance(node.func, _ast.Attribute) else None
        if target in SINK_CALLS or attr in SINK_ATTRS:
            offenders.append((scope, target, node.lineno))
        if target in ("sys.exit", "exit", "quit", "SystemExit") and node.args:
            arg = node.args[0]
            ok = (isinstance(arg, _ast.Constant) and type(arg.value) is int) or (
                isinstance(arg, _ast.Call) and _ast.unparse(arg.func) == "main"
            )
            if not ok:
                offenders.append((scope, f"{target}(non-int)", node.lineno))

    visit(_ast.parse(source), "<module>")
    return offenders


def test_every_output_sink_goes_through_the_emitter():
    assert _audit_output_sinks(SCRIPT.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "planted",
    [
        "print('x')",
        "import sys\nsys.stdout.write('x')",
        "import sys\nsys.stdout.buffer.write(b'x')",
        "import os\nos.write(2, b'x')",
        "import sys\nsys.exit('x')",
        "raise SystemExit('x')",
        "import warnings",
        "import logging",
        "from traceback import print_exc",
        "breakpoint()",
        "class C:\n    print('x')",
        "async def f():\n    print('x')",
        "def g():\n    def h():\n        print('x')",
    ],
)
def test_output_sink_audit_catches_planted_sinks(planted: str):
    assert _audit_output_sinks(planted), planted


@pytest.mark.parametrize(
    "fields",
    [
        {"sha": "private.person@example.org"},
        {"sha": "g" * 40},
        {"role": "private.person"},
        {"reason": "private.person"},
        {"fp_kind": "email", "fp": "private.person"},
        {"count": "3"},
        {"count": True},
    ],
)
def test_emitter_rejects_values_outside_the_allowlist(fields: dict):
    base = {
        "sha": "a" * 40,
        "role": "author",
        "reason": "email_not_allowed",
        "fp_kind": "email",
        "fp": "0123456789",
    }
    if "count" in fields:
        with pytest.raises(ValueError):
            guard.emit("ok", noun="commits", **fields)
        return
    with pytest.raises(ValueError):
        guard.emit("violation", **{**base, **fields})


def test_emitter_rejects_unknown_templates_and_fields():
    with pytest.raises(ValueError):
        guard.emit("no-such-template")
    with pytest.raises(ValueError):
        guard.emit("ok", count=1, noun="commit", extra="private.person")


# --- round-4 review (#1208): the reporting path itself fails closed ----------


def test_fallback_writer_prints_only_the_fixed_line():
    import ast as _ast

    tree = _ast.parse(SCRIPT.read_text(encoding="utf-8"))
    (func,) = [
        n
        for n in _ast.walk(tree)
        if isinstance(n, _ast.FunctionDef) and n.name == "_write_fallback"
    ]
    writes = [
        n
        for n in _ast.walk(func)
        if isinstance(n, _ast.Call) and _ast.unparse(n.func).endswith(".write")
    ]
    assert writes
    for call in writes:
        assert [_ast.unparse(a) for a in call.args] == ["FALLBACK_LINE"]


def test_git_killed_by_a_signal_exits_2_without_traceback_or_launch_path(tmp_path: Path):
    # A git child killed by SIGTERM returns -15; that used to fail emit()'s
    # validation inside the error handler and dump a traceback naming the
    # launch path.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    git = fakebin / "git"
    git.write_text("#!/bin/sh\nkill -TERM $$\n")
    git.chmod(0o755)
    launcher_dir = tmp_path / "secret-launch-marker"
    launcher_dir.mkdir()
    launcher = launcher_dir / "secret-launch-marker.py"
    launcher.symlink_to(SCRIPT)
    env = _clean_env(tmp_path)
    env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        [sys.executable, str(launcher), "--repo", str(tmp_path), "--allow-email", FLEET],
        env=env,
        capture_output=True,
        text=True,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 2, out
    assert "signal 15" in result.stderr
    assert "Traceback" not in out
    assert "secret-launch-marker" not in out


def test_emit_failure_inside_the_error_handler_fails_closed(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise guard.UsageError("git_failed", code=-15)  # rejected by the code validator

    monkeypatch.setattr(guard, "list_commits", boom)
    assert guard.main(["--allow-email", FLEET]) == 2
    captured = capsys.readouterr()
    assert captured.err == guard.FALLBACK_LINE
    assert captured.out == ""


def test_emit_always_raising_still_fails_closed(monkeypatch, capsys):
    def broken_emit(*_a, **_k):
        raise ValueError("private.person@example.org")

    monkeypatch.setattr(guard, "emit", broken_emit)
    assert guard.main(["--help"]) == 2
    captured = capsys.readouterr()
    assert captured.err == guard.FALLBACK_LINE
    assert "private.person" not in captured.out + captured.err


def test_installed_hooks_print_only_the_fixed_line(monkeypatch, capsys):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(sys, "unraisablehook", sys.unraisablehook)
    guard.install_fail_closed_hooks()
    try:
        raise RuntimeError("private.person@example.org")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    sys.unraisablehook(object())
    captured = capsys.readouterr()
    assert captured.err == guard.FALLBACK_LINE * 2
    assert captured.out == ""


def test_isolated_interpreter_ignores_python_startup_settings(repo: Path):
    # Python startup (outside the script) echoes an unknown PYTHONWARNINGS
    # category; -I ignores PYTHON* variables, which is why the documented
    # wiring runs the check with `python3 -I -S`.
    env = _clean_env(repo.parent)
    env["PYTHONWARNINGS"] = "ignore::secret_warning_marker"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), "--repo", str(repo), "--help"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "secret_warning_marker" not in result.stdout + result.stderr


# --- Devin review (#1208): exact matching, timeout, range shape ---------------


def _crafted_commit(repo: Path, ident: str) -> str:
    """Write a commit object whose author and committer lines are ``ident``."""
    env = _clean_env(repo.parent)

    def git(*args: str, stdin: str | None = None) -> str:
        out = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            env=env,
            capture_output=True,
            text=True,
            input=stdin,
        )
        return out.stdout.strip()

    tree = git("rev-parse", "HEAD^{tree}")
    parent = git("rev-parse", "HEAD")
    body = (
        f"tree {tree}\nparent {parent}\n"
        f"author {ident} 1700000000 +0000\ncommitter {ident} 1700000000 +0000\n\ncrafted\n"
    )
    sha = git("hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=body)
    git("tag", "-f", "crafted", sha)
    return sha


@pytest.mark.parametrize(
    "ident",
    [
        f"Desktop_Agent1 < {FLEET} >",
        "Desktop_Agent1 < someuser@somehost.lan >",
    ],
)
def test_whitespace_inside_the_address_does_not_evade_matching(repo: Path, ident: str):
    sha = _crafted_commit(repo, ident)
    result = _run(repo, "--range", "base..crafted", "--allow-email", FLEET)
    assert result.returncode == 1, result.stdout + result.stderr
    assert sha[:12] in result.stderr
    assert "someuser" not in result.stderr and "somehost" not in result.stderr


def test_git_timeout_is_a_fixed_usage_error(tmp_path: Path, monkeypatch, capsys):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    git = fakebin / "git"
    git.write_text("#!/bin/sh\nexec sleep 30\n")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(guard, "GIT_TIMEOUT_S", 0.5)
    assert guard.main(["--repo", str(tmp_path), "--allow-email", FLEET]) == 2
    captured = capsys.readouterr()
    assert "timed out" in captured.err
    assert str(tmp_path) not in captured.out + captured.err


def test_default_git_timeout_matches_run_cmd():
    assert guard.GIT_TIMEOUT_S == 120


@pytest.mark.parametrize("bad_range", ["--", "-n1", "--all", "-"])
def test_option_shaped_range_is_a_usage_error(repo: Path, bad_range: str):
    result = _check(repo, f"--range={bad_range}")
    assert result.returncode == 2, result.stdout
    assert "invalid arguments" in result.stderr
