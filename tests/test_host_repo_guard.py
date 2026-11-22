"""Scope the session host-repo guard to this checkout (task #482 review flake)."""

import json
import subprocess

import conftest
import pytest
from host_repo_guard import external_branch_names, scope_host_repo_state, settle_mismatch


def test_scope_ignores_concurrent_task_claim_worktree(tmp_path):
    """A cockpit claim in the same gitdir is not this suite escaping tmp_path.

    The #482 review run collected 2261 passes, then session teardown failed
    because `/home/you/dev/agent/.worktrees/task-483` appeared mid-suite.
    That path is outside the product checkout under test.
    """
    repo = tmp_path / "task-482"
    repo.mkdir()
    before_wt = f"{repo}  4b513c0 [task/482-p0-add-sanitized-lifecycle-cap]\n"
    before_br = "* task/482-p0-add-sanitized-lifecycle-cap\n  main\n"
    after_wt = (
        before_wt
        + "/home/you/dev/agent/.worktrees/task-483"
        + "                   e7e3833 [task/483-p0-introduce-typed-inter-agent]\n"
    )
    after_br = before_br + "+ task/483-p0-introduce-typed-inter-agent\n"
    assert scope_host_repo_state(repo, before_wt, before_br) == scope_host_repo_state(
        repo, after_wt, after_br
    )


def test_scope_still_flags_a_worktree_created_inside_the_checkout(tmp_path):
    repo = tmp_path / "task-482"
    leaked = repo / ".claude" / "worktrees" / "agy-task-1"
    leaked.mkdir(parents=True)
    before_wt = f"{repo}  4b513c0 [task/482-p0-add-sanitized-lifecycle-cap]\n"
    after_wt = before_wt + f"{leaked}  4b513c0 [agy/task-1-slug]\n"
    before = scope_host_repo_state(repo, before_wt, "* task/482-p0-add-sanitized-lifecycle-cap\n")
    after = scope_host_repo_state(
        repo,
        after_wt,
        "* task/482-p0-add-sanitized-lifecycle-cap\n  agy/task-1-slug\n",
    )
    assert before != after
    assert str(leaked) in after[0]


def test_scope_still_flags_a_local_branch_with_no_external_worktree(tmp_path):
    repo = tmp_path / "task-482"
    repo.mkdir()
    wt = f"{repo}  4b513c0 [task/482-p0-add-sanitized-lifecycle-cap]\n"
    before = scope_host_repo_state(repo, wt, "* task/482-p0-add-sanitized-lifecycle-cap\n")
    after = scope_host_repo_state(
        repo,
        wt,
        "* task/482-p0-add-sanitized-lifecycle-cap\n  agy/task-1-escaped\n",
    )
    assert before != after
    assert "agy/task-1-escaped" in after[1]


def test_settle_mismatch_absorbs_transient_concurrent_branch_churn():
    """A concurrent `git fetch origin pull/523/head:pr-523-review` settles.

    `scope_host_repo_state` cannot scope this out: the fetch creates a bare
    branch ref with no worktree, so there is nothing spatial to exclude it
    by. It is a race, not a location, so the fix is temporal: re-poll and
    only fail if the mismatch is still there. Here the second poll finds the
    branch gone again (or never landed) and matches `before`, so the guard
    must not fail.
    """
    before = ("", "* main\n")
    mid_fetch = ("", "* main\n  pr-523-review\n")
    settled_again = ("", "* main\n")
    polls = iter([settled_again])

    result = settle_mismatch(before, mid_fetch, lambda: next(polls))

    assert result == before


def test_settle_mismatch_still_fails_a_persistent_branch_escape():
    """A test that really creates a branch and leaves it must still fail.

    Every re-read keeps seeing the escaped branch, so `settle_mismatch` must
    not paper over it — it returns the still-mismatched state once retries
    are exhausted, for the caller to fail on.
    """
    before = ("", "* main\n")
    first_after = ("", "* main\n  agy/task-1-escaped\n")
    polls = iter([first_after, first_after, first_after])

    result = settle_mismatch(before, first_after, lambda: next(polls), max_retries=3)

    assert result == first_after
    assert result != before


def test_settle_mismatch_stops_polling_git_became_unavailable():
    """A `None` poll result (git disappeared mid-session) is returned as-is.

    `_host_repo_state()` returns `None` when git itself is unavailable; that
    reports unavailable state rather than retrying it as a mutation mismatch.
    The active session guard fails closed when its snapshot becomes unavailable.
    """
    before = ("", "* main\n")
    first_after = ("", "* main\n  agy/task-1-escaped\n")

    result = settle_mismatch(before, first_after, lambda: None, max_retries=3)

    assert result is None


@pytest.mark.parametrize("path_name", ["checkout", "checkout with  two spaces"])
def test_scope_ignores_only_worktree_column_padding(tmp_path, path_name):
    repo = tmp_path / path_name
    repo.mkdir()
    before = f"{repo}  1234567 [main] locked\n"
    after = f"{repo}                1234567     [main] locked\n"
    assert scope_host_repo_state(repo, before, "* main\n") == scope_host_repo_state(
        repo, after, "* main\n"
    )


@pytest.mark.parametrize(
    "change",
    [
        "HEAD",
        "branch",
        "detached",
        "locked",
        "prunable",
        "nested_path",
        "path_spacing",
    ],
)
def test_scope_preserves_semantic_worktree_changes_with_spaces(tmp_path, change):
    repo = tmp_path / "checkout with  two spaces"
    repo.mkdir()
    path = repo
    head, state, flags = "1234567", "[main]", ""
    before = f"{path}   {head} {state}\n"
    if change == "HEAD":
        head = "abcdef0"
    elif change == "branch":
        state = "[other]"
    elif change == "detached":
        state = "(detached HEAD)"
    elif change in {"locked", "prunable"}:
        flags = " " + change
    elif change == "nested_path":
        path = repo / "nested worktree"
    else:
        path = tmp_path / "checkout with one space"
    after = f"{path}   {head} {state}{flags}\n"
    assert scope_host_repo_state(repo, before, "* main\n") != scope_host_repo_state(
        repo, after, "* main\n"
    )


def _git(repo, *args):
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        text=False,
        timeout=20,
    ).stdout.decode("utf-8", errors="surrogateescape")


def _init_repo(repo):
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )


@pytest.mark.parametrize("path_name", ["checkout", "checkout with  two spaces"])
def test_external_real_worktree_registration_only_changes_display_padding(tmp_path, path_name):
    repo = tmp_path / path_name
    _init_repo(repo)

    def git(*args):
        return _git(repo, *args)

    before_wt = git("worktree", "list")
    before_br = git("branch", "--list")
    external = tmp_path / "external checkout with a substantially longer path"
    git("worktree", "add", "-b", "external", str(external))
    after_wt = git("worktree", "list")
    after_br = git("branch", "--list")
    # Establish the actual cause: Git repads even the unchanged checkout row.
    assert before_wt.splitlines()[0] != after_wt.splitlines()[0]
    assert scope_host_repo_state(repo, before_wt, before_br) == scope_host_repo_state(
        repo, after_wt, after_br
    )
    leaked = repo / ".worktrees" / "nested checkout with spaces"
    git("worktree", "add", "-b", "leaked", str(leaked))
    assert scope_host_repo_state(repo, before_wt, before_br) != scope_host_repo_state(
        repo, git("worktree", "list"), git("branch", "--list")
    )


@pytest.mark.parametrize(
    "path_name", ["checkout trailing  ", "checkout\twith tabs", "checkout\r\nwith lines  "]
)
def test_host_snapshot_preserves_exact_git_paths(tmp_path, monkeypatch, path_name):
    repo = tmp_path / path_name
    _init_repo(repo)
    monkeypatch.setattr(conftest, "__file__", str(repo / "tests" / "conftest.py"))
    before = conftest._host_repo_state()
    assert json.loads(before[0])["worktree"] == str(repo)
    external = tmp_path / "external checkout with a substantially longer path"
    _git(repo, "worktree", "add", "-b", "external", str(external))
    assert conftest._host_repo_state() == before
    leaked = repo / ".worktrees" / "nested\r\ncheckout trailing  "
    _git(repo, "worktree", "add", "-b", "leaked", str(leaked))
    after = conftest._host_repo_state()
    assert after != before
    assert str(leaked) in {json.loads(row)["worktree"] for row in after[0].splitlines()}


@pytest.mark.parametrize(
    "fields",
    [
        ["HEAD " + "2" * 40, "branch refs/heads/main"],
        ["HEAD " + "1" * 40, "branch refs/heads/other"],
        ["HEAD " + "1" * 40, "detached"],
        ["HEAD " + "1" * 40, "branch refs/heads/main", "locked reason with spaces"],
        ["HEAD " + "1" * 40, "branch refs/heads/main", "prunable stale path"],
        ["bare"],
    ],
    ids=["HEAD", "branch", "detached", "locked", "prunable", "bare"],
)
def test_machine_snapshot_preserves_git_metadata(tmp_path, fields):
    repo = tmp_path / "checkout with spaces"
    repo.mkdir()
    baseline = f"worktree {repo}\0HEAD {'1' * 40}\0branch refs/heads/main\0\0"
    changed = f"worktree {repo}\0" + "\0".join(fields) + "\0\0"
    before = scope_host_repo_state(repo, baseline, "* main\n")
    after = scope_host_repo_state(repo, changed, "* main\n")
    assert after != before
    parsed = json.loads(after[0])
    for field in fields:
        key, _, value = field.partition(" ")
        assert parsed[key] == value


@pytest.mark.parametrize("flag", ["locked", "prunable"])
def test_machine_snapshot_preserves_flag_reason_changes(tmp_path, flag):
    repo = tmp_path / "checkout"
    repo.mkdir()
    base = f"worktree {repo}\0HEAD {'1' * 40}\0branch refs/heads/main\0{flag} "
    before = scope_host_repo_state(repo, base + "reason A\0\0", "* main\n")
    after = scope_host_repo_state(repo, base + "reason B\0\0", "* main\n")
    assert before != after


@pytest.mark.parametrize(
    "raw",
    [
        "unexpected display row",
        "worktree /external\0HEAD " + "1" * 40 + "\0branch refs/heads/main\0",
        "worktree /external\0HEAD " + "1" * 40 + "\0branch refs/heads/main\0unknown x\0\0",
        "worktree /external\0HEAD "
        + "1" * 40
        + "\0branch refs/heads/main\0branch refs/heads/other\0\0",
        "worktree /external\0HEAD " + "1" * 40 + "\0branch refs/heads/main\0detached\0\0",
        "worktree /external\0HEAD " + "1" * 40 + "\0branch refs/heads/\0\0",
        "worktree relative-path\0HEAD " + "1" * 40 + "\0branch refs/heads/main\0\0",
    ],
    ids=[
        "display",
        "unterminated",
        "unknown-field",
        "duplicate",
        "conflicting-state",
        "empty-branch",
        "relative-path",
    ],
)
def test_malformed_worktree_records_remain_opaque(tmp_path, raw):
    state = scope_host_repo_state(tmp_path, raw, "* main\n")
    assert json.loads(state[0])["opaque"] == raw.removesuffix("\0\0")
    assert state[1] == "* main"


@pytest.mark.parametrize(
    "usage_error", [b"error: unknown switch `z'\n", b"error: opcion desconocida z\n"]
)
def test_old_git_rejected_z_falls_back_without_disabling_guard(tmp_path, monkeypatch, usage_error):
    repo = tmp_path / 'checkout\tquote"é trailing  '
    _init_repo(repo)
    monkeypatch.setattr(conftest, "__file__", str(repo / "tests" / "conftest.py"))
    real_run = subprocess.run
    rejected = []

    def old_git(args, **kwargs):
        if args == ["git", "worktree", "list", "--porcelain", "-z"]:
            rejected.append(args)
            return subprocess.CompletedProcess(args, 129, b"", usage_error)
        return real_run(args, **kwargs)

    monkeypatch.setattr(conftest.subprocess, "run", old_git)
    before = conftest._host_repo_state()
    assert before is not None
    assert json.loads(before[0])["worktree"] == str(repo)
    external = tmp_path / "external\r\ncheckout trailing  "
    _git(repo, "worktree", "add", "-b", "external", str(external))
    assert conftest._host_repo_state() == before
    leaked = repo / ".worktrees" / "nested\r\ncheckout trailing  "
    _git(repo, "worktree", "add", "-b", "leaked", str(leaked))
    after = conftest._host_repo_state()
    assert after != before
    records = [json.loads(row) for row in after[0].splitlines()]
    assert any(row.get("worktree") == str(leaked) for row in records)
    assert all("opaque" not in row for row in records)
    assert len(rejected) == 3


def test_plain_porcelain_git_octal_quotes_decode_bytes_exactly(tmp_path):
    repo = tmp_path / 'checkout\tline\r\nquote"\\é trailing  '
    repo.mkdir()
    escaped = str(repo).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
    escaped = escaped.replace("é", "\\303\\251")
    raw = f'worktree "{escaped}"\nHEAD {"1" * 40}\nbranch refs/heads/main\nlocked "reason\\nnext\\t\\377"\n\n'
    parsed = json.loads(scope_host_repo_state(repo, raw, "* main\n")[0])
    assert parsed["worktree"] == str(repo)
    assert parsed["locked"] == "reason\nnext\t\udcff"


def test_plain_porcelain_preserves_unquoted_legacy_path_whitespace(tmp_path):
    repo = tmp_path / "checkout\twith carriage\r trailing  "
    repo.mkdir()
    raw = f"worktree {repo}\nHEAD {'1' * 40}\nbranch refs/heads/main\n\n"
    assert json.loads(scope_host_repo_state(repo, raw, "* main\n")[0])["worktree"] == str(repo)


@pytest.mark.parametrize(
    "bad_quote", ['"/external\\q"', '"/external\\777"', '"/external\\12"', '"/external']
)
def test_invalid_git_quotes_are_retained_opaque(tmp_path, bad_quote):
    raw = f"worktree {bad_quote}\nHEAD {'1' * 40}\nbranch refs/heads/external\n\n"
    scoped = scope_host_repo_state(tmp_path, raw, "+ external\n")
    assert json.loads(scoped[0])["opaque"] == raw.removesuffix("\n\n")
    assert scoped[1] == "+ external"


def test_old_git_unquoted_newline_path_remains_opaque(tmp_path):
    raw = f"worktree /external/line\nbreak  \nHEAD {'1' * 40}\nbranch refs/heads/external\n\n"
    scoped = scope_host_repo_state(tmp_path, raw, "+ external\n")
    assert json.loads(scoped[0])["opaque"] == raw.removesuffix("\n\n")
    assert scoped[1] == "+ external"


def test_established_host_guard_fails_if_snapshot_becomes_unavailable(monkeypatch):
    values = iter([("worktree", "branch"), None])
    monkeypatch.setattr(conftest, "_host_repo_state", lambda: next(values))
    guard = conftest.host_repo_unchanged.__wrapped__()
    next(guard)
    with pytest.raises(pytest.fail.Exception, match="unavailable"):
        next(guard)


@pytest.mark.parametrize("failure", ["worktrees", "branches", "timeout"])
def test_snapshot_errors_fail_closed_in_existing_repository(tmp_path, monkeypatch, failure):
    repo = tmp_path / "checkout"
    _init_repo(repo)
    monkeypatch.setattr(conftest, "__file__", str(repo / "tests" / "conftest.py"))
    real_run = subprocess.run

    def unavailable(args, **kwargs):
        if args[:3] == ["git", "worktree", "list"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 30)
            if failure == "worktrees":
                return subprocess.CompletedProcess(args, 1, b"", b"unavailable")
        if args == ["git", "branch", "--list"] and failure == "branches":
            return subprocess.CompletedProcess(args, 1, b"", b"unavailable")
        return real_run(args, **kwargs)

    monkeypatch.setattr(conftest.subprocess, "run", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        conftest._host_repo_state()


@pytest.mark.parametrize("field", ["locked", "prunable"])
def test_plain_porcelain_preserves_literal_quotes_in_metadata(tmp_path, field):
    repo = tmp_path / "checkout"
    repo.mkdir()
    base = f"worktree {repo}\nHEAD {'1' * 40}\nbranch refs/heads/main\n{field} "
    assert scope_host_repo_state(repo, base + "reason\n\n", "* main\n") != scope_host_repo_state(
        repo, base + '"reason"\n\n', "* main\n"
    )


@pytest.mark.parametrize("change", ["HEAD", "branch", "detached", "locked", "prunable"])
def test_old_git_admin_snapshot_preserves_nested_newline_metadata(tmp_path, monkeypatch, change):
    repo = tmp_path / "checkout"
    _init_repo(repo)
    nested = repo / ".worktrees" / "nested\r\ncheckout trailing  "
    _git(repo, "worktree", "add", "-b", "nested", str(nested))
    monkeypatch.setattr(conftest, "__file__", str(repo / "tests" / "conftest.py"))
    real_run = subprocess.run

    def old_git(args, **kwargs):
        if args == ["git", "worktree", "list", "--porcelain", "-z"]:
            return subprocess.CompletedProcess(args, 129, b"", b"error: unknown switch `z'\n")
        return real_run(args, **kwargs)

    monkeypatch.setattr(conftest.subprocess, "run", old_git)
    before = conftest._host_repo_state()
    assert any(json.loads(row)["worktree"] == str(nested) for row in before[0].splitlines())
    if change == "HEAD":
        _git(
            nested,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "next",
        )
    elif change == "branch":
        _git(nested, "checkout", "-b", "next-branch")
    elif change == "detached":
        _git(nested, "checkout", "--detach")
    elif change == "locked":
        _git(repo, "worktree", "lock", "--reason", '"reason"', str(nested))
    else:
        (nested / ".git").unlink()
    assert conftest._host_repo_state() != before


@pytest.mark.parametrize("main_kind", ["ordinary", "bare", "configured"])
def test_old_git_metadata_from_linked_checkout_scopes_external_main(
    tmp_path, monkeypatch, main_kind
):
    repo = tmp_path / "main\r\ncheckout trailing  "
    _init_repo(repo)
    if main_kind == "bare":
        common = tmp_path / "bare\nrepo.git"
        _git(tmp_path, "clone", "--bare", str(repo), str(common))
        repo = common
    elif main_kind == "configured":
        _git(repo, "config", "core.worktree", str(repo))
    linked = tmp_path / "linked\r\ncheckout trailing  "
    _git(repo, "worktree", "add", "-b", "linked", str(linked))
    monkeypatch.setattr(conftest, "__file__", str(linked / "tests" / "conftest.py"))
    real_run = subprocess.run

    def old_git(args, **kwargs):
        if args == ["git", "worktree", "list", "--porcelain", "-z"]:
            return subprocess.CompletedProcess(args, 129, b"", b"error: unknown switch `z'\n")
        return real_run(args, **kwargs)

    monkeypatch.setattr(conftest.subprocess, "run", old_git)
    before = conftest._host_repo_state()
    records = [json.loads(row) for row in before[0].splitlines()]
    assert len(records) == 1
    assert records[0]["worktree"] == str(linked)
    external = tmp_path / "external\r\ncheckout trailing  "
    _git(repo, "worktree", "add", "-b", "external", str(external))
    assert conftest._host_repo_state() == before
    _git(repo, "worktree", "lock", "--reason", "reason", str(linked))
    unquoted = conftest._host_repo_state()
    _git(repo, "worktree", "unlock", str(linked))
    _git(repo, "worktree", "lock", "--reason", '"reason"', str(linked))
    assert conftest._host_repo_state() != unquoted


def test_old_git_metadata_rejects_relative_admin_gitdir(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    _init_repo(repo)
    external = tmp_path / "external"
    _git(repo, "worktree", "add", "-b", "external", str(external))
    admin = repo / ".git" / "worktrees" / "external"
    (admin / "gitdir").write_bytes(b"relative/.git\n")
    monkeypatch.setattr(conftest, "__file__", str(repo / "tests" / "conftest.py"))
    real_run = subprocess.run

    def old_git(args, **kwargs):
        if args == ["git", "worktree", "list", "--porcelain", "-z"]:
            return subprocess.CompletedProcess(args, 129, b"", b"error: unknown switch `z'\n")
        return real_run(args, **kwargs)

    monkeypatch.setattr(conftest.subprocess, "run", old_git)
    with pytest.raises(RuntimeError, match="absolute"):
        conftest._host_repo_state()


@pytest.mark.parametrize("failure", ["unsupported-z", "git-absent"])
def test_host_snapshot_startup_without_repository_remains_unavailable(
    tmp_path, monkeypatch, failure
):
    repo = tmp_path / "not-a-repository"
    repo.mkdir()
    monkeypatch.setattr(conftest, "__file__", str(repo / "tests" / "conftest.py"))
    real_run = subprocess.run

    def unavailable(args, **kwargs):
        if failure == "git-absent":
            raise FileNotFoundError("git")
        if args == ["git", "worktree", "list", "--porcelain", "-z"]:
            return subprocess.CompletedProcess(args, 129, b"", b"error: opcion desconocida z\n")
        return real_run(args, **kwargs)

    monkeypatch.setattr(conftest.subprocess, "run", unavailable)
    assert conftest._host_repo_state() is None


def _run_guard_around(monkeypatch, repo, during):
    """Drive the session guard for a checkout at `repo` around `during()`."""
    monkeypatch.setattr(conftest, "__file__", str(repo / "tests" / "conftest.py"))
    monkeypatch.setattr(conftest, "_HOST_REPO_GUARD_RETRY_DELAY_SECONDS", 0)
    guard = conftest.host_repo_unchanged.__wrapped__()
    next(guard)
    during()
    with pytest.raises(StopIteration):
        next(guard)


def _pre_existing_lanes(tmp_path, repo):
    _git(repo, "worktree", "add", "-b", "worktree-agent-old", str(tmp_path / "agent-old"))
    _git(repo, "worktree", "add", "-b", "worktree-agent-done", str(tmp_path / "agent-done"))


def _concurrent_agent_churn(tmp_path, repo):
    """What other agents sharing this gitdir do while a run is in flight (#1998).

    Lanes add isolated worktrees outside this checkout, then switch them to a
    task branch (`git switch -c claude/<id>-...`), which leaves the worktree's
    first branch as a bare ref no worktree holds. A finished lane removes its
    worktree and keeps the branch it pushed.
    """
    lane = tmp_path / "agent-lane"
    _git(repo, "worktree", "add", "-b", "worktree-agent-lane", str(lane))
    _git(lane, "switch", "-c", "claude/1986-first-task")
    _git(lane, "switch", "-c", "claude/1987-next-task")
    _git(tmp_path / "agent-old", "switch", "-c", "claude/1985-old-task")
    _git(repo, "worktree", "remove", str(tmp_path / "agent-done"))


def test_guard_ignores_concurrent_external_worktree_and_branch_churn(tmp_path, monkeypatch):
    """Another agent's worktree add and branch switches are not an escape (#1998).

    Before the fix, every branch the churn left behind without an external
    worktree (`worktree-agent-lane`, `claude/1986-first-task`,
    `worktree-agent-old`, `worktree-agent-done`) failed session teardown even
    though no test touched the repository.
    """
    repo = tmp_path / "checkout"
    _init_repo(repo)
    _pre_existing_lanes(tmp_path, repo)

    _run_guard_around(monkeypatch, repo, lambda: _concurrent_agent_churn(tmp_path, repo))


def test_guard_still_fails_a_test_created_branch_amid_concurrent_churn(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    _init_repo(repo)
    _pre_existing_lanes(tmp_path, repo)

    def churn_and_escape():
        _concurrent_agent_churn(tmp_path, repo)
        # A test that escaped its tmp_path and branched in the host checkout.
        _git(repo, "branch", "agy/task-1-escaped")

    with pytest.raises(pytest.fail.Exception) as failure:
        _run_guard_around(monkeypatch, repo, churn_and_escape)
    message = str(failure.value)
    assert "agy/task-1-escaped" in message
    for concurrent in ("worktree-agent-lane", "claude/1986", "worktree-agent-old"):
        assert concurrent not in message


def test_guard_still_fails_a_test_switching_the_checkout_branch(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    _init_repo(repo)
    _pre_existing_lanes(tmp_path, repo)

    def churn_and_escape():
        _concurrent_agent_churn(tmp_path, repo)
        _git(repo, "switch", "-c", "agy/task-2-escaped")

    with pytest.raises(pytest.fail.Exception, match="agy/task-2-escaped"):
        _run_guard_around(monkeypatch, repo, churn_and_escape)


def test_reflog_attribution_ignores_checkouts_before_the_session(tmp_path):
    """Only switches an external worktree made since the guard started count."""
    repo = tmp_path / "checkout"
    _init_repo(repo)
    lane = tmp_path / "lane"
    _git(repo, "worktree", "add", "-b", "lane-start", str(lane))
    _git(lane, "switch", "-c", "lane-next")
    worktrees = _git(repo, "worktree", "list", "--porcelain", "-z")

    assert "lane-start" in external_branch_names(repo, worktrees, since=0)
    assert "lane-start" not in external_branch_names(repo, worktrees, since=2**40)
    assert "lane-next" in external_branch_names(repo, worktrees, since=2**40)
