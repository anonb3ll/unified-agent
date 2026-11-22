"""Tests for project -> source-repository binding (task #1425, Parity 20)."""

import subprocess

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import project_bindings, storage


def _credential_url(scheme, username, password, host_path):
    credentials = username + (":" + password if password else "")
    return scheme + ":" + "/" * 2 + credentials + chr(64) + host_path


# --------------------------------------------------------------------------
# normalize_repo
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "owner/name",
        "github.com/owner/name",
        "https://github.com/owner/name",
        "https://github.com/owner/name.git",
        "https://github.com/owner/name/",
        "git@github.com:owner/name.git",
        "git@github.com:owner/name",
        "ssh://git@github.com/owner/name.git",
    ],
)
def test_normalize_repo_accepts_known_forms(raw):
    assert project_bindings.normalize_repo(raw) == "owner/name"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        _credential_url("https", "user", "pass", "github.com/owner/name"),
        _credential_url("https", "token", "", "github.com/owner/name"),
        _credential_url("ssh", "user", "pass", "github.com/owner/name.git"),
        _credential_url("ssh", "someone", "", "github.com/owner/name.git"),
        "https://gitlab.com/owner/name",
        "gitlab.com/owner/name",
        "owner/name/extra",
        "just-a-name",
        "owner/",
        "/name",
        "has space/name",
        "owner//name",
        "github.com/owner//name",
    ],
)
def test_normalize_repo_rejects_bad_input(raw):
    with pytest.raises(ToolError):
        project_bindings.normalize_repo(raw)


def test_normalize_repo_is_case_insensitive():
    assert project_bindings.normalize_repo("Owner/Name") == "owner/name"
    assert project_bindings.normalize_repo("https://github.com/OWNER/NAME.git") == "owner/name"


# --------------------------------------------------------------------------
# bind / unbind / get / list / effective_binding
# --------------------------------------------------------------------------


def test_bind_persists_and_get_reads_it_back():
    result = project_bindings.bind(
        "browser", "someowner/browser", default_branch="main", bound_by="test-agent"
    )
    binding = result["binding"]
    assert binding["repo"] == "someowner/browser"
    assert binding["default_branch"] == "main"
    assert binding["bound_by"] == "test-agent"
    assert binding["bound_at"]

    fetched = project_bindings.get("browser")
    assert fetched["binding"]["repo"] == "someowner/browser"
    assert fetched["binding"]["source"] == "db"


def test_bind_rejects_unknown_project():
    with pytest.raises(ToolError):
        project_bindings.bind("not-a-real-project", "owner/name", bound_by="test-agent")


def test_bind_requires_a_bound_by_identity():
    with pytest.raises(ToolError):
        project_bindings.bind("browser", "owner/name")


def test_bind_is_upsert():
    project_bindings.bind("browser", "owner/first", bound_by="a")
    project_bindings.bind("browser", "owner/second", bound_by="b")
    binding = project_bindings.get("browser")["binding"]
    assert binding["repo"] == "owner/second"
    assert binding["bound_by"] == "b"


def test_unbind_removes_the_db_row():
    project_bindings.bind("browser", "owner/name", bound_by="test-agent")
    result = project_bindings.unbind("browser", unbound_by="test-agent")
    assert result["unbound"] is True
    assert result["previous"]["repo"] == "owner/name"
    assert project_bindings.get("browser")["binding"] is None


def test_unbind_of_never_bound_project_is_a_no_op():
    result = project_bindings.unbind("browser", unbound_by="test-agent")
    assert result["unbound"] is False
    assert result["previous"] is None


def test_db_binding_wins_over_config_seed():
    # unified-agent has a config seed (anonb3ll/unified-agent); a DB row must
    # override it.
    seeded = project_bindings.effective_binding("unified-agent")
    assert seeded is not None
    assert seeded["source"] == "config_default"
    assert seeded["repo"] == "anonb3ll/unified-agent"

    project_bindings.bind("unified-agent", "someone-else/fork", bound_by="test-agent")
    overridden = project_bindings.effective_binding("unified-agent")
    assert overridden["source"] == "db"
    assert overridden["repo"] == "someone-else/fork"


def test_effective_binding_is_none_for_an_unseeded_unbound_project():
    assert project_bindings.effective_binding("sample-store") is None


def test_list_bindings_covers_every_known_project():
    result = project_bindings.list_bindings()
    projects = {row["project"] for row in result["projects"]}
    assert projects == {"unified-agent", "browser", "sample-store", "client-portal"}


def test_get_rejects_unknown_project():
    with pytest.raises(ToolError):
        project_bindings.get("not-a-real-project")


# --------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------


def test_resolve_unknown_project_reports_unknown_state():
    resolved = project_bindings.resolve("not-a-real-project")
    assert resolved["local_state"] == "unknown"
    assert resolved["local_path"] is None
    assert resolved["remote_matches"] is None


def test_resolve_reports_not_checked_out_here(tmp_path):
    resolved = project_bindings.resolve("browser", dev_root=tmp_path)
    assert resolved["local_state"] == "not_checked_out_here"
    assert resolved["local_path"] is None
    assert resolved["remote_matches"] is None


def test_resolve_reports_checked_out_with_no_binding(tmp_path):
    (tmp_path / "browser").mkdir()
    resolved = project_bindings.resolve("browser", dev_root=tmp_path)
    assert resolved["local_state"] == "checked_out"
    assert resolved["repo"] is None
    assert resolved["remote_matches"] is None


def _init_git_repo_with_origin(path, origin_url: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin_url], cwd=path, check=True)


def test_resolve_matches_when_origin_matches_binding(tmp_path):
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")
    _init_git_repo_with_origin(tmp_path / "browser", "https://github.com/someowner/browser.git")
    resolved = project_bindings.resolve("browser", dev_root=tmp_path)
    assert resolved["local_state"] == "checked_out"
    assert resolved["remote_matches"] is True


def test_resolve_mismatches_when_origin_differs_from_binding(tmp_path):
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")
    _init_git_repo_with_origin(tmp_path / "browser", "https://github.com/other/repo.git")
    resolved = project_bindings.resolve("browser", dev_root=tmp_path)
    assert resolved["remote_matches"] is False


def test_resolve_remote_matches_none_when_origin_unreadable(tmp_path):
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")
    (tmp_path / "browser").mkdir()  # not even a git repo
    resolved = project_bindings.resolve("browser", dev_root=tmp_path)
    assert resolved["remote_matches"] is None


# --------------------------------------------------------------------------
# check_worktree_binding: warn by default, refuse under the strict flag
# --------------------------------------------------------------------------


def test_check_worktree_binding_warns_by_default_for_unbound_project(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_PROJECT_REPO_STRICT", raising=False)
    project_repo = tmp_path / "browser"
    project_repo.mkdir()
    message = project_bindings.check_worktree_binding("browser", project_repo)
    assert message is not None
    assert "no bound source repository" in message


def test_check_worktree_binding_refuses_under_strict_flag_for_unbound_project(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("UA_PROJECT_REPO_STRICT", "1")
    project_repo = tmp_path / "browser"
    project_repo.mkdir()
    with pytest.raises(ToolError):
        project_bindings.check_worktree_binding("browser", project_repo)


def test_check_worktree_binding_passes_when_bound_and_origin_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_PROJECT_REPO_STRICT", "1")
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")
    project_repo = tmp_path / "browser"
    _init_git_repo_with_origin(project_repo, "git@github.com:someowner/browser.git")
    assert project_bindings.check_worktree_binding("browser", project_repo) is None


def test_check_worktree_binding_refuses_under_strict_flag_on_origin_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_PROJECT_REPO_STRICT", "1")
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")
    project_repo = tmp_path / "browser"
    _init_git_repo_with_origin(project_repo, "https://github.com/other/repo.git")
    with pytest.raises(ToolError):
        project_bindings.check_worktree_binding("browser", project_repo)


def test_ensure_schema_is_idempotent():
    conn = storage.connect()
    try:
        project_bindings.ensure_schema(conn)
        project_bindings.ensure_schema(conn)
    finally:
        conn.close()


def test_bind_prefers_verified_identity_over_claimed_bound_by(monkeypatch):
    monkeypatch.setattr(project_bindings.config, "verified_agent_identity", lambda: "Verified")
    result = project_bindings.bind("browser", "someowner/browser", bound_by="Claimed")
    assert result["binding"]["bound_by"] == "Verified"


def test_unbind_requires_an_actor_without_verified_identity(monkeypatch):
    monkeypatch.setattr(project_bindings.config, "verified_agent_identity", lambda: None)
    with pytest.raises(ToolError):
        project_bindings.unbind("browser")


def test_bind_rolls_back_the_row_when_the_audit_event_fails(monkeypatch):
    """The write and its audit event share one transaction: if the event
    append raises, the binding row must not be left committed unaudited."""

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated event-log failure")

    monkeypatch.setattr(project_bindings.events, "record_event_once_on_connection", _boom)
    with pytest.raises(RuntimeError):
        project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")
    assert project_bindings.get("browser")["binding"] is None


def test_unbind_rolls_back_when_the_audit_event_fails(monkeypatch):
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated event-log failure")

    monkeypatch.setattr(project_bindings.events, "record_event_once_on_connection", _boom)
    with pytest.raises(RuntimeError):
        project_bindings.unbind("browser", unbound_by="test-agent")
    # The row must still be present -- the DELETE was rolled back too.
    assert project_bindings.get("browser")["binding"]["repo"] == "someowner/browser"


# --------------------------------------------------------------------------
# _read_git_origin: direct, non-following config read (no `git` subprocess)
# --------------------------------------------------------------------------


def test_read_git_origin_ignores_instead_of_rewrite(tmp_path):
    repo = tmp_path / "browser"
    _init_git_repo_with_origin(repo, "https://github.com/someowner/browser.git")
    config_path = repo / ".git" / "config"
    config_path.write_text(
        config_path.read_text()
        + '\n[url "https://github.com/other/rewritten.git"]\n'
        + "\tinsteadOf = https://github.com/someowner/browser.git\n"
    )
    assert project_bindings._read_git_origin(repo) == "https://github.com/someowner/browser.git"


def test_read_git_origin_accepts_legacy_dotted_section(tmp_path):
    repo = tmp_path / "browser"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text(
        "[remote.origin]\n\turl = https://github.com/someowner/browser.git\n"
    )
    assert project_bindings._read_git_origin(repo) == "https://github.com/someowner/browser.git"


def test_duplicate_origin_urls_fail_closed_under_strict_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_PROJECT_REPO_STRICT", "1")
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")
    repo = tmp_path / "browser"
    _init_git_repo_with_origin(repo, "https://github.com/someowner/browser.git")
    config_path = repo / ".git" / "config"
    config_path.write_text(config_path.read_text() + "\turl = https://github.com/other/repo.git\n")
    assert project_bindings._read_git_origin(repo) is None
    with pytest.raises(ToolError, match="could not read the origin remote"):
        project_bindings.check_worktree_binding("browser", repo)


def test_repeated_identical_origin_urls_are_accepted(tmp_path):
    repo = tmp_path / "browser"
    _init_git_repo_with_origin(repo, "https://github.com/someowner/browser.git")
    config_path = repo / ".git" / "config"
    config_path.write_text(
        config_path.read_text()
        + '\n[remote "origin"]\n\turl = https://github.com/someowner/browser.git\n'
    )
    assert project_bindings._read_git_origin(repo) == "https://github.com/someowner/browser.git"


def test_read_git_origin_does_not_follow_include_path(tmp_path):
    repo = tmp_path / "browser"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    included = tmp_path / "included.gitconfig"
    included.write_text('[remote "origin"]\n\turl = https://github.com/someowner/browser.git\n')
    (repo / ".git" / "config").write_text(f"[include]\n\tpath = {included}\n")
    assert project_bindings._read_git_origin(repo) is None


def test_read_git_origin_refuses_worktree_gitdir_pointer(tmp_path):
    repo = tmp_path / "browser"
    repo.mkdir(parents=True)
    real_repo = tmp_path / "elsewhere"
    _init_git_repo_with_origin(real_repo, "https://github.com/someowner/browser.git")
    (repo / ".git").write_text(f"gitdir: {real_repo / '.git'}\n")
    assert project_bindings._read_git_origin(repo) is None


def test_read_git_origin_refuses_symlinked_git_directory(tmp_path):
    repo = tmp_path / "browser"
    repo.mkdir()
    real_repo = tmp_path / "elsewhere"
    _init_git_repo_with_origin(real_repo, "https://github.com/someowner/browser.git")
    (repo / ".git").symlink_to(real_repo / ".git", target_is_directory=True)
    assert project_bindings._read_git_origin(repo) is None


def test_read_git_origin_refuses_symlinked_config(tmp_path):
    repo = tmp_path / "browser"
    repo.mkdir()
    (repo / ".git").mkdir()
    outside = tmp_path / "outside-config"
    outside.write_text('[remote "origin"]\n\turl = https://github.com/someowner/browser.git\n')
    (repo / ".git" / "config").symlink_to(outside)
    assert project_bindings._read_git_origin(repo) is None


def test_read_git_origin_refuses_oversized_config(tmp_path):
    repo = tmp_path / "browser"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/someowner/browser.git\n'
        + "#" * project_bindings._MAX_GIT_CONFIG_BYTES
    )
    assert project_bindings._read_git_origin(repo) is None


def test_invalid_origin_credentials_are_not_exposed_in_warning_or_event(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.delenv("UA_PROJECT_REPO_STRICT", raising=False)
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")
    repo = tmp_path / "browser"
    secret = ":".join(("user", "pass"))
    origin = _credential_url("https", "user", "pass", "github.com/other/repo")
    _init_git_repo_with_origin(repo, origin)
    recorded = []
    monkeypatch.setattr(
        project_bindings.events,
        "record_event",
        lambda event_type, **kwargs: recorded.append((event_type, kwargs["payload"])),
    )

    message = project_bindings.check_worktree_binding("browser", repo)

    assert message is not None
    assert secret not in message
    assert secret not in caplog.text
    assert secret not in repr(recorded)
    assert recorded[0][0] == "task.worktree.repo_binding_warning"
