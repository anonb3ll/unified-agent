"""Git-pinned skill sources: add, discover, refresh, import (task #1750).

Every test runs against a local bare-repo fixture over ``file://``; nothing
touches the network.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import skill_sources as src
from unified_agent import skills_registry as reg
from unified_agent import storage

GOOD_DESC = (
    "Reviews a Python module for missing type hints and adds them. "
    "Use when asked to type-annotate, add hints, or fix mypy errors in Python code."
)
OTHER_DESC = (
    "Drafts release notes from merged pull requests grouped by change type. "
    "Use when the user asks for a changelog or release summary."
)
GOOD_BODY = """\
# Steps

1. Run `uv run mypy <module>` and collect errors.
2. Annotate public functions first; use `from __future__ import annotations`.
3. Re-run mypy until clean.
"""


def skill_md(name: str, description: str = GOOD_DESC, body: str = GOOD_BODY) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def git_env(isolated_env, tmp_path, monkeypatch):
    """Hermetic git: no user/system config, a fixed identity, file:// allowed."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(src.FILE_URL_ENV, "1")
    return gitconfig


class Repo:
    """A working repo plus the bare remote the registry fetches from."""

    def __init__(self, root: Path):
        self.work = root / "work"
        self.bare = root / "remote.git"
        self.work.mkdir(parents=True)
        run_git(self.work, "init", "-q", "-b", "main")
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        run_git(self.work, "remote", "add", "origin", str(self.bare))

    @property
    def url(self) -> str:
        return f"file://{self.bare}"

    def write(self, rel: str, text: str | bytes, mode: int | None = None) -> None:
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
        if mode is not None:
            path.chmod(mode)

    def commit(self, message: str = "update") -> str:
        run_git(self.work, "add", "-A")
        run_git(self.work, "commit", "-q", "-m", message)
        run_git(self.work, "push", "-q", "origin", "main")
        return run_git(self.work, "rev-parse", "HEAD")


@pytest.fixture
def repo(git_env, tmp_path):
    r = Repo(tmp_path / "skills-repo")
    r.write("README.md", "# shared skills\n")
    r.write("skills/type-hints/SKILL.md", skill_md("type-hints"))
    r.write("skills/release-notes/SKILL.md", skill_md("release-notes", OTHER_DESC))
    r.commit("initial")
    return r


# --- pinning ----------------------------------------------------------------


def test_add_pins_branch_to_exact_sha(repo):
    head = run_git(repo.work, "rev-parse", "HEAD")
    result = src.add(repo.url, "main", author="Desktop_Agent1")
    source = result["source"]
    assert source["pinned_sha"] == head
    assert len(source["pinned_sha"]) == 40
    assert source["ref"] == "main"
    assert source["added_by"] == "Desktop_Agent1"
    assert source["last_refreshed"]
    # Adding the same url+ref again is idempotent and does not move the pin.
    repo.write("skills/type-hints/notes.md", "extra\n")
    repo.commit()
    again = src.add(repo.url, "main", author="Desktop_Agent1")
    assert again["idempotent"] is True
    assert again["source"]["pinned_sha"] == head


def test_add_accepts_an_exact_sha_ref(repo):
    first = run_git(repo.work, "rev-parse", "HEAD")
    repo.write("skills/type-hints/notes.md", "extra\n")
    repo.commit()
    result = src.add(repo.url, first, author="a")
    assert result["source"]["pinned_sha"] == first


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/repo.git",
        "ext::sh -c touch% /tmp/pwned",
        # Built at runtime so the fake userinfo never reads as a real basic-auth URL.
        "https://" + ":".join(("user", "fake")) + "@github.com/o/r.git",
        "https://github.com/o/r.git?x=1",
        "-uhttps://github.com/o/r.git",
        "ssh://root@github.com/o/r.git",
        "ssh://git@-oProxyCommand=touch/r.git",
        "git@-oProxyCommand=x:o/r.git",
        "/abs/local/path",
        "relative/path",
        "",
    ],
)
def test_add_rejects_disallowed_urls(git_env, url):
    with pytest.raises(ToolError):
        src.add(url, "main", author="a")


def test_file_urls_are_gated_off_by_default(repo, monkeypatch):
    monkeypatch.delenv(src.FILE_URL_ENV)
    with pytest.raises(ToolError, match="file://"):
        src.add(repo.url, "main", author="a")


def test_allowed_url_shapes_validate():
    assert src.validate_url("https://github.com/o/r.git")
    assert src.validate_url("ssh://git@github.com/o/r.git")
    assert src.validate_url("git@github.com:o/r.git")


@pytest.mark.parametrize(
    "ref",
    ["--upload-pack=touch x", "a..b", "main.lock", "refs/heads/", "x@{1}", "-b", "a b", ""],
)
def test_add_rejects_unsafe_refs(repo, ref):
    with pytest.raises(ToolError):
        src.add(repo.url, ref, author="a")


def test_schema_refuses_non_sha_pins(isolated_env):
    conn = storage.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO skill_sources (url, ref, pinned_sha, added_at) VALUES (?,?,?,?)",
                ("https://x/y.git", "main", "abc123", "now"),
            )
    finally:
        conn.close()


# --- discover ---------------------------------------------------------------


def test_discover_lists_packages_at_the_pinned_sha(repo):
    source = src.add(repo.url, "main", author="a")["source"]
    pinned = source["pinned_sha"]
    # A later commit is invisible until a refresh and explicit import.
    repo.write("skills/later/SKILL.md", skill_md("later"))
    repo.commit()

    found = src.discover(source["id"])
    assert found["sha"] == pinned
    paths = {p["path"]: p for p in found["packages"]}
    assert set(paths) == {"skills/release-notes", "skills/type-hints"}
    hints = paths["skills/type-hints"]
    assert hints["name"] == "type-hints"
    assert hints["importable"] is True
    assert hints["problems"] == []
    assert reg.list_skills()["skills"] == []


def test_discover_rejects_a_sha_that_is_neither_pinned_nor_refreshed(repo):
    source = src.add(repo.url, "main", author="a")["source"]
    with pytest.raises(ToolError, match="40-character"):
        src.discover(source["id"], sha="abc123")
    with pytest.raises(ToolError, match="pinned"):
        src.discover(source["id"], sha="0" * 40)


# --- import -----------------------------------------------------------------


def test_import_records_git_provenance_and_content_hash(repo):
    source = src.add(repo.url, "main", author="a")["source"]
    sha = source["pinned_sha"]
    result = src.import_package(
        source["id"], "skills/type-hints", sha=source["pinned_sha"], author="Desktop_Agent1"
    )
    assert result["ok"] is True
    skill = result["skill"]
    assert skill["name"] == "type-hints"
    assert skill["version"] == 1
    assert skill["source"] == f"git:{repo.url}@{sha}:skills/type-hints"
    assert len(skill["content_hash"]) == 64
    assert result["review"]["status"] in {"pass", "warn"}
    assert result["provenance"] == {
        "source_id": source["id"],
        "url": repo.url,
        "sha": sha,
        "path": "skills/type-hints",
    }
    stored = reg.get("type-hints")
    assert stored["content"] == skill_md("type-hints")

    again = src.import_package(
        source["id"], "skills/type-hints", sha=source["pinned_sha"], author="Desktop_Agent1"
    )
    assert again["idempotent"] is True
    assert len(reg.list_skills("type-hints")["skills"]) == 1


def test_import_runs_skill_review_and_blocks_high_findings(git_env, tmp_path):
    r = Repo(tmp_path / "bad")
    r.write("broken/SKILL.md", "# no frontmatter\n\nbody\n")
    r.commit()
    source = src.add(r.url, "main", author="a")["source"]
    result = src.import_package(
        source["id"], "broken", name="broken", sha=source["pinned_sha"], author="a"
    )
    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["review"]["status"] == "fail"
    assert reg.list_skills()["skills"] == []


def test_import_name_must_match_frontmatter(repo):
    source = src.add(repo.url, "main", author="a")["source"]
    with pytest.raises(ToolError, match="frontmatter"):
        src.import_package(
            source["id"],
            "skills/type-hints",
            name="other-name",
            sha=source["pinned_sha"],
            author="a",
        )


def test_import_carries_supporting_files(repo):
    repo.write(
        "skills/type-hints/SKILL.md",
        skill_md("type-hints", body=GOOD_BODY + "\nSee `references/guide.md`.\n"),
    )
    repo.write("skills/type-hints/references/guide.md", "# Guide\n\nDetails.\n")
    repo.commit()
    source = src.add(repo.url, "main", author="a")["source"]
    result = src.import_package(
        source["id"], "skills/type-hints", sha=source["pinned_sha"], author="a"
    )
    assert result["ok"] is True
    files = {f["path"] for f in result["skill"]["package_manifest"]["files"]}
    assert files == {"SKILL.md", "references/guide.md"}


# --- refresh ----------------------------------------------------------------


def test_refresh_resolves_new_sha_and_never_auto_imports(repo):
    source = src.add(repo.url, "main", author="a")["source"]
    old = source["pinned_sha"]
    src.import_package(source["id"], "skills/type-hints", sha=source["pinned_sha"], author="a")

    repo.write("skills/type-hints/SKILL.md", skill_md("type-hints", body=GOOD_BODY + "\nMore.\n"))
    repo.write("skills/later/SKILL.md", skill_md("later", OTHER_DESC.replace("release", "rel")))
    repo.commit("change")
    new = run_git(repo.work, "rev-parse", "HEAD")

    refreshed = src.refresh(source["id"], author="a")
    assert refreshed["changed"] is True
    assert refreshed["pinned_sha"] == old
    assert refreshed["resolved_sha"] == new
    assert refreshed["imported"] is False
    diff = refreshed["diff"]
    assert diff["packages_changed"] == ["skills/type-hints"]
    assert diff["packages_added"] == ["skills/later"]
    assert diff["packages_removed"] == []
    assert {"status": "M", "path": "skills/type-hints/SKILL.md"} in diff["files"]

    # Nothing moved: the pin, the registry versions, and discover's default.
    row = src.list_sources()["sources"][0]
    assert row["pinned_sha"] == old
    assert row["refreshed_sha"] == new
    assert len(reg.list_skills("type-hints")["skills"]) == 1
    assert src.discover(source["id"])["sha"] == old
    # The refreshed SHA can be previewed before it is adopted.
    preview = src.discover(source["id"], sha=new)
    assert "skills/later" in {p["path"] for p in preview["packages"]}

    adopted = src.import_package(source["id"], "skills/type-hints", sha=new, author="a")
    assert adopted["skill"]["version"] == 2
    assert adopted["skill"]["source"].endswith(f"@{new}:skills/type-hints")
    assert src.list_sources()["sources"][0]["pinned_sha"] == new


def test_refresh_unchanged_reports_no_diff(repo):
    source = src.add(repo.url, "main", author="a")["source"]
    refreshed = src.refresh(source["id"], author="a")
    assert refreshed["changed"] is False
    assert refreshed["diff"] is None


# --- size limits and unsafe paths ---------------------------------------------


def test_oversized_skill_md_and_support_file_are_refused(git_env, tmp_path):
    r = Repo(tmp_path / "big")
    r.write("huge/SKILL.md", skill_md("huge", body=GOOD_BODY + "x" * (reg.MAX_SKILL_FILE_BYTES)))
    r.write("bulky/SKILL.md", skill_md("bulky"))
    r.write("bulky/assets/blob.txt", "y" * (reg.MAX_PACKAGE_FILE_BYTES + 1))
    r.commit()
    source = src.add(r.url, "main", author="a")["source"]
    packages = {p["path"]: p for p in src.discover(source["id"])["packages"]}
    for path in ("huge", "bulky"):
        assert packages[path]["importable"] is False
        assert any("limit" in problem for problem in packages[path]["problems"])
        with pytest.raises(ToolError, match="limit"):
            src.import_package(source["id"], path, sha=source["pinned_sha"], author="a")
    assert reg.list_skills()["skills"] == []


def test_too_many_files_is_refused(git_env, tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "MAX_PACKAGE_FILES", 3)
    r = Repo(tmp_path / "many")
    r.write("many/SKILL.md", skill_md("many"))
    for i in range(4):
        r.write(f"many/assets/f{i}.txt", "x\n")
    r.commit()
    source = src.add(r.url, "main", author="a")["source"]
    with pytest.raises(ToolError, match="limit"):
        src.import_package(source["id"], "many", sha=source["pinned_sha"], author="a")


def test_escaping_symlink_member_is_refused(git_env, tmp_path):
    r = Repo(tmp_path / "links")
    r.write("linked/SKILL.md", skill_md("linked"))
    (r.work / "linked" / "passwd").symlink_to("../../../../etc/passwd")
    r.commit()
    source = src.add(r.url, "main", author="a")["source"]
    package = src.discover(source["id"])["packages"][0]
    assert package["importable"] is False
    assert any("symlink" in problem for problem in package["problems"])
    with pytest.raises(ToolError, match="symlink"):
        src.import_package(source["id"], "linked", sha=source["pinned_sha"], author="a")


def test_hidden_and_secret_members_are_refused(git_env, tmp_path):
    r = Repo(tmp_path / "hidden")
    r.write("dotted/SKILL.md", skill_md("dotted"))
    r.write("dotted/.env", "X=1\n")
    r.write("secretive/SKILL.md", skill_md("secretive", OTHER_DESC))
    r.write("secretive/assets/credentials.json", "{}\n")
    r.commit()
    source = src.add(r.url, "main", author="a")["source"]
    for path in ("dotted", "secretive"):
        with pytest.raises(ToolError):
            src.import_package(source["id"], path, sha=source["pinned_sha"], author="a")


@pytest.mark.parametrize("path", ["../skills/type-hints", "/etc", "skills/../skills/type-hints"])
def test_unsafe_import_paths_are_refused(repo, path):
    source = src.add(repo.url, "main", author="a")["source"]
    with pytest.raises(ToolError):
        src.import_package(source["id"], path, sha=source["pinned_sha"], author="a")


def test_import_path_must_be_a_discovered_package(repo):
    source = src.add(repo.url, "main", author="a")["source"]
    with pytest.raises(ToolError, match="no SKILL.md package"):
        src.import_package(source["id"], "skills", sha=source["pinned_sha"], author="a")


def test_nested_skill_md_marks_the_outer_package_unimportable(git_env, tmp_path):
    r = Repo(tmp_path / "nested")
    r.write("outer/SKILL.md", skill_md("outer"))
    r.write("outer/inner/SKILL.md", skill_md("inner", OTHER_DESC))
    r.commit()
    source = src.add(r.url, "main", author="a")["source"]
    packages = {p["path"]: p for p in src.discover(source["id"])["packages"]}
    assert packages["outer"]["importable"] is False
    assert any("nested" in problem for problem in packages["outer"]["problems"])
    assert packages["outer/inner"]["importable"] is True
    with pytest.raises(ToolError, match="nested"):
        src.import_package(source["id"], "outer", sha=source["pinned_sha"], author="a")
    assert (
        src.import_package(source["id"], "outer/inner", sha=source["pinned_sha"], author="a")["ok"]
        is True
    )


# --- no execution, read-only originals -----------------------------------------


MARKER_SCRIPT = "#!/bin/sh\ntouch {marker}\n"


def _hook_dir(tmp_path: Path, marker: Path) -> Path:
    hooks = tmp_path / "evil-hooks"
    hooks.mkdir()
    for hook in (
        "reference-transaction",
        "post-checkout",
        "post-merge",
        "post-rewrite",
        "pre-push",
        "fsmonitor-watchman",
    ):
        path = hooks / hook
        path.write_text(MARKER_SCRIPT.format(marker=marker))
        path.chmod(0o755)
    return hooks


def test_operations_never_execute_hooks_or_repo_scripts(git_env, tmp_path):
    marker = tmp_path / "EXECUTED"
    script = MARKER_SCRIPT.format(marker=marker)
    r = Repo(tmp_path / "hooky")
    r.write("tool/SKILL.md", skill_md("tool"))
    r.write("tool/scripts/install.sh", script, mode=0o755)
    r.write("tool/hooks/post-checkout", script, mode=0o755)
    r.write(".githooks/post-checkout", script, mode=0o755)
    r.write(".gitattributes", "* filter=evil\n")
    r.commit()

    # A user-level config that would run code on fetch or checkout if honored.
    hooks = _hook_dir(tmp_path, marker)
    git_env.write_text(
        f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = {hooks / 'fsmonitor-watchman'}\n"
        f"[filter \"evil\"]\n\tsmudge = sh -c 'touch {marker}; cat'\n"
        f"\tclean = sh -c 'touch {marker}; cat'\n"
    )

    # Control: the same config does run code under a plain git fetch.
    control = tmp_path / "control.git"
    subprocess.run(["git", "init", "-q", "--bare", str(control)], check=True)
    subprocess.run(
        ["git", "-C", str(control), "fetch", "-q", r.url, "main:refs/heads/main"], check=True
    )
    if not marker.exists():
        pytest.skip("this git does not run reference-transaction hooks on fetch")
    marker.unlink()

    source = src.add(r.url, "main", author="a")["source"]
    src.discover(source["id"])
    src.import_package(source["id"], "tool", sha=source["pinned_sha"], author="a")
    assert not marker.exists(), "add, discover or import ran a hook or filter"
    r.write("tool/notes.md", "more\n")
    r.commit()
    marker.unlink(missing_ok=True)  # the fixture's own commits run the hooks
    refreshed = src.refresh(source["id"], author="a")
    result = src.import_package(source["id"], "tool", sha=refreshed["resolved_sha"], author="a")
    assert result["ok"] is True
    assert not marker.exists(), "a hook, filter or repo script ran"

    # Imported scripts keep their bytes but nothing was executed or checked out.
    cache = src.cache_dir(r.url)
    assert (cache / "HEAD").is_file()
    assert not (cache / "hooks").exists()
    assert not any(p.name == "install.sh" for p in cache.rglob("*"))


def test_originals_are_left_untouched(repo):
    before_head = run_git(repo.bare, "rev-parse", "main")
    before_refs = run_git(repo.bare, "for-each-ref")
    before_files = sorted(str(p) for p in repo.work.rglob("*") if ".git" not in p.parts)
    source = src.add(repo.url, "main", author="a")["source"]
    src.discover(source["id"])
    src.import_package(source["id"], "skills/type-hints", sha=source["pinned_sha"], author="a")
    src.refresh(source["id"], author="a")
    assert run_git(repo.bare, "rev-parse", "main") == before_head
    assert run_git(repo.bare, "for-each-ref") == before_refs
    assert run_git(repo.work, "status", "--porcelain") == ""
    assert sorted(str(p) for p in repo.work.rglob("*") if ".git" not in p.parts) == before_files


def test_cache_is_private_and_bare(repo):
    src.add(repo.url, "main", author="a")
    cache = src.cache_dir(repo.url)
    assert run_git(cache, "rev-parse", "--is-bare-repository") == "true"
    mode = stat.S_IMODE(os.stat(cache.parent).st_mode)
    assert mode & 0o077 == 0


# --- tool surface -------------------------------------------------------------


def test_skill_registry_actions_expose_sources(repo):
    added = reg.skill_registry("source_add", {"url": repo.url, "ref": "main", "author": "a"})
    sid = added["source"]["id"]
    assert reg.skill_registry("source_list", {})["sources"][0]["id"] == sid
    found = reg.skill_registry("source_discover", {"source_id": sid})
    assert len(found["packages"]) == 2
    imported = reg.skill_registry(
        "source_import",
        {
            "source_id": sid,
            "path": "skills/type-hints",
            "sha": added["source"]["pinned_sha"],
            "author": "a",
        },
    )
    assert imported["ok"] is True
    refreshed = reg.skill_registry("source_refresh", {"source_id": sid, "author": "a"})
    assert refreshed["changed"] is False


def test_unknown_source_id_is_an_actionable_error(isolated_env):
    with pytest.raises(ToolError, match="source_list"):
        src.discover(999)


# --- review round 1 (PR #1226) ------------------------------------------------


def test_import_requires_an_explicit_sha(repo):
    source = src.add(repo.url, "main", author="a")["source"]
    for missing in (None, ""):
        with pytest.raises(ToolError, match="sha"):
            src.import_package(source["id"], "skills/type-hints", sha=missing, author="a")
    with pytest.raises(ToolError, match="sha"):
        reg.skill_registry(
            "source_import",
            {"source_id": source["id"], "path": "skills/type-hints", "author": "a"},
        )
    assert reg.list_skills()["skills"] == []


def test_import_refuses_a_pin_that_moved_since_discovery(repo):
    """Two callers: B adopts a newer commit between A's discovery and import."""
    source = src.add(repo.url, "main", author="a")["source"]
    discovered = src.discover(source["id"])["sha"]
    repo.write("skills/type-hints/SKILL.md", skill_md("type-hints", body=GOOD_BODY + "\nB.\n"))
    repo.write(
        "skills/release-notes/SKILL.md",
        skill_md("release-notes", OTHER_DESC, body=GOOD_BODY + "\nB.\n"),
    )
    repo.commit("both change")
    newer = src.refresh(source["id"], author="b")["resolved_sha"]
    src.import_package(source["id"], "skills/type-hints", sha=newer, author="b")
    assert src.list_sources()["sources"][0]["pinned_sha"] == newer

    with pytest.raises(ToolError, match="pinned"):
        src.import_package(source["id"], "skills/release-notes", sha=discovered, author="a")
    assert reg.list_skills("release-notes")["skills"] == []


@pytest.mark.parametrize(
    "first, second",
    [
        ("references/Guide.md", "references/guide.md"),
        ("References/a.md", "references/b.md"),
        ("references/caf\u00e9.md", "references/cafe\u0301.md"),
        ("references/\uff27uide.md", "references/guide.md"),
    ],
)
def test_case_and_unicode_path_collisions_are_refused(git_env, tmp_path, first, second):
    r = Repo(tmp_path / "collide")
    r.write("one/SKILL.md", skill_md("one"))
    r.write(f"one/{first}", "first\n")
    r.write(f"one/{second}", "second\n")
    r.commit()
    source = src.add(r.url, "main", author="a")["source"]
    package = src.discover(source["id"])["packages"][0]
    assert package["importable"] is False
    assert any("collision" in problem for problem in package["problems"])
    with pytest.raises(ToolError, match="collision"):
        src.import_package(source["id"], "one", sha=source["pinned_sha"], author="a")
    assert reg.list_skills()["skills"] == []


def test_package_discovery_work_is_linear():
    """50,000 sibling packages must not trigger a root-by-root rescan."""
    import time

    entries = [("100644", "blob", "a" * 40, 1, f"skills/p{i}/SKILL.md") for i in range(50_000)]
    start = time.perf_counter()
    packages = src._packages(entries)
    elapsed = time.perf_counter() - start
    assert len(packages) == 50_000
    # The quadratic scan took ~2.4 s at 4,000 entries (~6 min here).
    assert elapsed < 20, elapsed


def test_nested_packages_are_found_without_rescans():
    entries = [
        ("100644", "blob", "a" * 40, 1, "outer/SKILL.md"),
        ("100644", "blob", "a" * 40, 1, "outer/notes.md"),
        ("100644", "blob", "a" * 40, 1, "outer/x/inner/SKILL.md"),
        ("100644", "blob", "a" * 40, 1, "outer/x/inner/ref.md"),
        ("100644", "blob", "a" * 40, 1, "SKILL.md"),
    ]
    packages = src._packages(entries)
    assert any("nested" in p for p in packages["outer"]["problems"])
    assert any("nested" in p for p in packages[""]["problems"])
    assert packages["outer/x/inner"]["problems"] == []
    assert {f[0] for f in packages["outer/x/inner"]["files"]} == {"SKILL.md", "ref.md"}


# --- review round 2 (PR #1226) ------------------------------------------------


def _staged_capture(name: str, version: int, stage: Path) -> None:
    """Materialize a stored version and run the installer's staged capture."""
    for rel, (data, mode) in reg.version_files(name, version).items():
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
    reg._capture_package(stage, "staged package verification")


def test_package_over_the_installer_entry_limit_is_refused(git_env, tmp_path):
    """75 files + 518 directories = 593 entries > MAX_PACKAGE_ENTRIES (512)."""
    r = Repo(tmp_path / "dirs")
    r.write("many-dirs/SKILL.md", skill_md("many-dirs"))
    for i in range(74):
        r.write(f"many-dirs/d{i}/a/b/c/d/e/f/note.md", "note\n")
    r.commit()
    source = src.add(r.url, "main", author="a")["source"]
    package = src.discover(source["id"])["packages"][0]
    assert package["importable"] is False
    assert any("entry" in problem for problem in package["problems"])
    with pytest.raises(ToolError, match="entry"):
        src.import_package(source["id"], "many-dirs", sha=source["pinned_sha"], author="a")
    assert reg.list_skills()["skills"] == []


def test_accepted_package_near_the_entry_limit_passes_staged_capture(git_env, tmp_path):
    """60 files of depth 7 + SKILL.md = 481 entries: importable and installable."""
    r = Repo(tmp_path / "dirs-ok")
    r.write("some-dirs/SKILL.md", skill_md("some-dirs"))
    for i in range(60):
        r.write(f"some-dirs/d{i}/a/b/c/d/e/f/note.md", "note\n")
    r.commit()
    source = src.add(r.url, "main", author="a")["source"]
    assert src.discover(source["id"])["packages"][0]["importable"] is True
    result = src.import_package(source["id"], "some-dirs", sha=source["pinned_sha"], author="a")
    assert result["ok"] is True
    _staged_capture("some-dirs", result["skill"]["version"], tmp_path / "stage" / "some-dirs")
