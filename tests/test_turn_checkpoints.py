"""Per-turn workspace checkpoints (task #1743).

Snapshots go to hidden refs through a temporary index: the user's HEAD, branch,
index and working tree must be byte-for-byte unchanged, the change summary is
capped and redacted, and old sessions' refs are pruned.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from unified_agent import acp_backend, turn_checkpoints
from unified_agent.turn_checkpoints import TurnCheckpoints


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.delenv("UA_TURN_CHECKPOINTS", raising=False)
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "fixture-user")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "kept.txt").write_text("one\n")
    (root / "gone.txt").write_text("bye\n")
    (root / ".gitignore").write_text("ignored.log\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def state(repo: Path) -> dict[str, str]:
    """Everything a checkpoint must leave alone."""
    index = repo / ".git" / "index"
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "symbolic-ref", "HEAD"),
        "branches": git(repo, "for-each-ref", "refs/heads"),
        "status": git(repo, "status", "--porcelain", "--untracked-files=all"),
        "staged": git(repo, "diff", "--cached"),
        "index": hashlib.sha256(index.read_bytes()).hexdigest(),
        "files": json.dumps(
            {
                str(p.relative_to(repo)): p.read_bytes().hex()
                for p in sorted(repo.rglob("*"))
                if p.is_file() and ".git" not in p.relative_to(repo).parts
            }
        ),
    }


def test_capture_leaves_head_branch_index_and_tree_unchanged(repo):
    (repo / "kept.txt").write_text("one\ntwo\n")
    (repo / "staged.txt").write_text("staged\n")
    git(repo, "add", "staged.txt")
    (repo / "new.txt").write_text("untracked\n")
    before = state(repo)
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    assert state(repo) == before
    assert git(repo, "rev-parse", store.ref("turn-1", "start")).strip() == start
    # The snapshot holds the working tree, including untracked files.
    assert git(repo, "show", f"{start}:new.txt") == "untracked\n"
    assert git(repo, "show", f"{start}:kept.txt") == "one\ntwo\n"


def test_changes_list_files_and_bounded_diff(repo):
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    (repo / "kept.txt").write_text("one\nchanged\n")
    (repo / "gone.txt").unlink()
    (repo / "added.txt").write_text("hello\n")
    (repo / "ignored.log").write_text("never captured\n")
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert {(f["status"], f["path"]) for f in summary["files"]} == {
        ("M", "kept.txt"),
        ("D", "gone.txt"),
        ("A", "added.txt"),
    }
    assert summary["files_total"] == 3 and not summary["files_truncated"]
    assert "+changed" in summary["diff"] and "ignored.log" not in summary["diff"]
    assert not summary["diff_truncated"]


def test_diff_and_file_list_are_capped_and_redacted(repo, monkeypatch):
    monkeypatch.setattr(turn_checkpoints, "MAX_FILES", 3)
    monkeypatch.setattr(turn_checkpoints, "MAX_DIFF_BYTES", 2_000)
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    secret = "sk-ant-api03-" + "A" * 40
    (repo / "config.py").write_text(f"KEY = '{secret}'\n")
    for i in range(10):
        (repo / f"bulk{i}.txt").write_text("x" * 1_000 + "\n")
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert len(summary["files"]) == 3 and summary["files_total"] == 11
    assert summary["files_truncated"] and summary["diff_truncated"]
    assert len(summary["diff"].encode()) <= 2_100
    assert secret not in summary["diff"]


def test_prune_keeps_the_newest_sessions(repo, monkeypatch):
    for day, session in enumerate(("old", "middle", "new"), start=1):
        # committerdate has one-second resolution; date the sessions apart.
        # GIT_* is stripped from git's environment, so set it on the identity.
        monkeypatch.setattr(
            turn_checkpoints,
            "_IDENTITY",
            {**turn_checkpoints._IDENTITY, "GIT_COMMITTER_DATE": f"2026-01-0{day}T00:00:00+0000"},
        )
        store = TurnCheckpoints.open(repo, session)
        store.capture("turn-1", "start")
        store.capture("turn-1", "end")
    removed = TurnCheckpoints.open(repo, "new").prune(keep=2)
    refs = git(repo, "for-each-ref", "--format=%(refname)", turn_checkpoints.REF_ROOT)
    assert removed == 2
    assert "/old/" not in refs and "/middle/" in refs and "/new/" in refs


def test_no_store_outside_git_or_when_disabled(tmp_path, repo, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert TurnCheckpoints.open(plain, "item-1") is None
    monkeypatch.setenv("UA_TURN_CHECKPOINTS", "0")
    assert TurnCheckpoints.open(repo, "item-1") is None


def test_unborn_repository_snapshots_without_a_parent(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_TURN_CHECKPOINTS", raising=False)
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q")
    (root / "a.txt").write_text("a\n")
    store = TurnCheckpoints.open(root, "item-1")
    start = store.capture("turn-1", "start")
    assert git(root, "show", f"{start}:a.txt") == "a\n"
    assert git(root, "status", "--porcelain") == "?? a.txt\n"


def test_session_ids_become_safe_ref_names(repo):
    store = TurnCheckpoints.open(repo, "../evil name/..")
    commit = store.capture("turn 1/../x", "start")
    ref = store.ref("turn 1/../x", "start")
    assert ".." not in ref and " " not in ref
    assert git(repo, "rev-parse", ref).strip() == commit


# --- end to end through a fake ACP agent -------------------------------------------

FAKE_AGENT = r"""
import json, os, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1,
              "agentCapabilities": {}, "agentInfo": {"name": "fake", "version": "0"}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "sess-1", "modes": {
              "currentModeId": "default",
              "availableModes": [{"id": "default", "name": "Default"}]}}})
    elif method == "session/set_mode":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        with open(os.environ["FAKE_EDIT"], "a") as fh:
            fh.write("edited by the agent\n")
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}})
"""


def test_acp_turn_emits_checkpoint_after_turn_end(repo, tmp_path, monkeypatch):
    script = tmp_path / "fake_agent.py"
    script.write_text("#!" + sys.executable + "\n" + FAKE_AGENT)
    script.chmod(0o755)
    monkeypatch.setenv("UA_GEMINI_BIN", str(script))
    monkeypatch.setenv("FAKE_EDIT", str(repo / "kept.txt"))
    before_head = git(repo, "rev-parse", "HEAD")
    events: list[dict] = []
    result = acp_backend.AcpBackend("gemini").run(
        prompt="edit please",
        cwd=str(repo),
        session_dir=str(tmp_path / "sess"),
        session_id="item-42",
        agent_label="Desktop_Agent1",
        timeout_s=30,
        on_event=events.append,
    )
    assert result["status"] == "success", (result.get("error"), [e["kind"] for e in events])
    kinds = [e["kind"] for e in events]
    # The snapshot follows turn_end, so it never stretches the recorded turn.
    assert kinds.index("turn_end") < kinds.index("turn_checkpoint") < kinds.index("session_end")
    checkpoint = next(e for e in events if e["kind"] == "turn_checkpoint")
    data = checkpoint["data"]
    assert checkpoint["turn_id"] == data["turn_id"] == "turn-1"
    assert data["files"] == [{"status": "M", "path": "kept.txt"}]
    assert "+edited by the agent" in data["diff"]
    assert data["ref"] == "refs/ua/checkpoints/item-42/turn-1/end"
    assert git(repo, "rev-parse", "HEAD") == before_head
    assert git(repo, "status", "--porcelain") == " M kept.txt\n"


# --- review hardening (PR #1089) -------------------------------------------------


def test_repository_configured_commands_never_run(repo, tmp_path):
    """Hooks, fsmonitor, filters and textconv set by the repo must not execute."""
    marks = tmp_path / "marks"
    marks.mkdir()

    def mark(name):
        return f"touch {marks / name}"

    hooks = repo / ".git" / "hooks"
    (hooks / "reference-transaction").write_text(f"#!/bin/sh\n{mark('hook')}\n")
    (hooks / "reference-transaction").chmod(0o755)
    git(repo, "config", "core.fsmonitor", mark("fsmonitor"))
    git(repo, "config", "filter.evil.clean", f"{mark('clean')}; cat")
    git(repo, "config", "filter.evil.process", mark("process"))
    git(repo, "config", "filter.evil.required", "true")
    git(repo, "config", "diff.evil.textconv", f"{mark('textconv')}; cat")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.txt filter=evil diff=evil\n")
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    (repo / "kept.txt").write_text("changed\n")
    end = store.capture("turn-1", "end")
    store.changes(start, end)
    assert sorted(p.name for p in marks.iterdir()) == []


def test_config_defined_hooks_never_run(repo, tmp_path):
    """``hook.<name>.command`` hooks ignore core.hooksPath and must be disabled too."""
    marks = tmp_path / "marks"
    marks.mkdir()
    for name, event in (
        ("h1", "reference-transaction"),
        ("h2", "post-index-change"),
        ("a=b", "reference-transaction"),
    ):
        git(repo, "config", f"hook.{name}.command", f"touch {marks / name.replace('=', '_')}")
        git(repo, "config", f"hook.{name}.event", event)
    # Probe: an older git has no config-defined hooks, so there is nothing to test.
    git(repo, "update-ref", "refs/probe", "HEAD")
    git(repo, "update-ref", "-d", "refs/probe")
    if not any(marks.iterdir()):
        pytest.skip("this git does not run config-defined hooks")
    for marker in marks.iterdir():
        marker.unlink()
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    (repo / "kept.txt").write_text("changed\n")
    end = store.capture("turn-1", "end")
    store.changes(start, end)
    store.prune(keep=0)
    assert sorted(p.name for p in marks.iterdir()) == []


def test_inherited_git_environment_cannot_redirect_the_snapshot(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    store = TurnCheckpoints.open(repo, "item-1")
    assert store.root == repo
    store.capture("turn-1", "start")
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert git(other, "for-each-ref", turn_checkpoints.REF_ROOT) == ""
    assert git(repo, "for-each-ref", turn_checkpoints.REF_ROOT) != ""


def pem(edge: str) -> str:
    # Built at runtime so no key-shaped literal sits in the source.
    return "-" * 5 + f"{edge} " + "PRIVATE" + " KEY" + "-" * 5


def test_secret_files_and_private_keys_are_withheld(repo):
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    (repo / ".env").write_text("DATABASE_PASSWORD=hunter2-not-in-the-diff\n")
    (repo / "deploy.pem").write_text("cert body\n")
    key_body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC-key-body-line"
    (repo / "notes.txt").write_text(f"{pem('BEGIN')}\n{key_body}\n{pem('END')}\n")
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    flags = {f["path"]: f.get("withheld", False) for f in summary["files"]}
    assert flags[".env"] is True and flags["deploy.pem"] is True and flags["notes.txt"] is False
    assert summary["withheld"] == 2
    assert "hunter2" not in summary["diff"] and "cert body" not in summary["diff"]
    assert key_body not in summary["diff"]
    assert "[content withheld: private key]" in summary["diff"]


def test_a_pgp_private_key_block_is_withheld(repo):
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    key_body = "lQOYBGVzZXJrZXktYm9keS1saW5lLXBncA-key-body-line"
    begin = "-" * 5 + "BEGIN " + "PGP PRIVATE" + " KEY BLOCK" + "-" * 5
    end_marker = "-" * 5 + "END " + "PGP PRIVATE" + " KEY BLOCK" + "-" * 5
    (repo / "notes.txt").write_text(f"{begin}\n\n{key_body}\n{end_marker}\n")
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert key_body not in summary["diff"]
    assert "[content withheld: private key]" in summary["diff"]


def embedded_key(lines: list[str]) -> str:
    """A config file with a private key in the middle, built at runtime."""
    block = "".join(f"  {line}\n" for line in [pem("BEGIN"), *lines, pem("END")])
    return f"name: web\ntls_key: |\n{block}port: 443\n"


def key_body(count: int, tag: str) -> list[str]:
    return [f"MIIEvQIBADANBgkqhkiG9w0BAQEF{tag}{i:02d}xyzBODY" for i in range(count)]


def test_a_hunk_inside_a_key_without_its_begin_line_is_withheld(repo):
    # Task #1990: editing one middle line of an embedded key gives a hunk of
    # body lines only; the BEGIN marker sits outside the diff context.
    body = key_body(20, "old")
    (repo / "config.yaml").write_text(embedded_key(body))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "key")
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    edited = [*body[:10], "MIIEvQIBADANBgkqhkiG9w0BAQEFnewlineXYZ", *body[11:]]
    (repo / "config.yaml").write_text(embedded_key(edited))
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert all(line not in summary["diff"] for line in body)
    assert "newlineXYZ" not in summary["diff"]
    assert "[content withheld: private key]" in summary["diff"]
    assert "config.yaml" in summary["diff"]
    assert {f["path"]: f.get("withheld", False) for f in summary["files"]} == {"config.yaml": False}


def test_a_file_holding_a_key_withholds_a_far_away_edit(repo):
    # The edit is far from the key, so the hunk carries no key line at all;
    # the blobs still hold one, so the whole section is withheld.
    body = key_body(3, "old")
    filler = "".join(f"filler line {i}\n" for i in range(30))
    (repo / "config.yaml").write_text(embedded_key(body) + filler)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "key")
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    changed = (embedded_key(body) + filler).replace("filler line 29", "changed tail")
    (repo / "config.yaml").write_text(changed)
    (repo / "kept.txt").write_text("one\ntwo\n")
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert "changed tail" not in summary["diff"]
    assert "[content withheld: private key]" in summary["diff"]
    assert "+two" in summary["diff"]  # an ordinary file keeps its diff


def test_a_credential_in_a_file_name_is_redacted(repo):
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    (repo / f"dump-{token}.txt").write_text("x\n")
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert summary["files"] and all(token not in f["path"] for f in summary["files"])
    assert token not in summary["diff"]


def test_a_secret_under_a_non_utf8_directory_is_withheld(repo):
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    try:
        folder = Path(os.fsdecode(bytes(repo) + b"/caf\xe9"))
        folder.mkdir()
    except (OSError, UnicodeError):
        pytest.skip("this filesystem refuses non-UTF-8 names")
    # Not credential-shaped, so only withholding (not redaction) can hide it.
    (folder / ".env").write_text("plain words that must stay home\n")
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert [f.get("withheld") for f in summary["files"]] == [True]
    assert summary["files"][0]["path"] == "caf�/.env"
    assert "stay home" not in summary["diff"]
    json.dumps(summary).encode("utf-8")  # no lone surrogates reach the hub


def test_a_secret_section_that_slips_past_the_pathspec_is_still_withheld():
    diff = (
        "diff --git a/ok.txt b/ok.txt\n+fine\n"
        'diff --git "a/caf\\351/.env" "b/caf\\351/.env"\n+TOKEN=one\n'
        "diff --git a/dir b/x/.env b/dir b/x/.env\n+TOKEN=two\n"
    )
    out = turn_checkpoints._withhold_sections(diff)
    assert "+fine" in out and "TOKEN" not in out
    assert out.count("[content withheld: secret file]") == 2


def test_credentials_are_redacted_before_the_cut(repo, monkeypatch):
    """A cut through a key must not leave a short, unmatchable fragment behind."""
    monkeypatch.setattr(turn_checkpoints, "MAX_DIFF_BYTES", 300)
    store = TurnCheckpoints.open(repo, "item-1")
    secret = "sk-ant-api03-" + "Q" * 60
    # Slide the key across the cut so some offset leaves only a few characters.
    for pad in range(120, 200, 3):
        start = store.capture(f"turn-{pad}", "start")
        (repo / "kept.txt").write_text("x" * pad + secret + "\n")
        end = store.capture(f"turn-{pad}", "end")
        diff = store.changes(start, end)["diff"]
        assert "sk-ant-api03-Q" not in diff, pad
        assert "sk-ant" not in diff.replace("[REDACTED", ""), pad
        git(repo, "checkout", "-q", "--", "kept.txt")


@pytest.mark.parametrize("shape", ["non_ascii", "many_long_paths"])
def test_summary_always_fits_the_event_cap(repo, shape):
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    if shape == "non_ascii":
        (repo / "kept.txt").write_text("Привет мир, это длинный текст. " * 1200)
    else:
        deep = repo / ("d" * 60) / ("e" * 60)
        deep.mkdir(parents=True)
        for i in range(150):
            (deep / f"{'f' * 20}{i:03d}.txt").write_text(f"{i}\n")
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert len(json.dumps(summary).encode()) <= turn_checkpoints.MAX_SUMMARY_BYTES
    assert summary["files"] and (summary["diff_truncated"] or summary["files_truncated"])


def test_a_non_utf8_file_keeps_its_summary(repo):
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    (repo / "latin1.txt").write_bytes("café crème\n".encode("latin-1"))
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert [f["path"] for f in summary["files"]] == ["latin1.txt"]


def test_large_untracked_files_are_not_captured(repo, monkeypatch):
    monkeypatch.setattr(turn_checkpoints, "MAX_UNTRACKED_BYTES", 1_000)
    store = TurnCheckpoints.open(repo, "item-1")
    (repo / "big.bin").write_bytes(b"\0" * 5_000)
    (repo / "small.txt").write_text("small\n")
    commit = store.capture("turn-1", "start")
    names = git(repo, "ls-tree", "--name-only", commit).split()
    assert "small.txt" in names and "big.bin" not in names


def test_a_timeout_turns_checkpoints_off_for_the_session(repo, monkeypatch):
    store = TurnCheckpoints.open(repo, "item-1")
    real_run = subprocess.run

    def slow(cmd, *args, **kwargs):
        if "write-tree" in cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(turn_checkpoints.subprocess, "run", slow)
    with pytest.raises(turn_checkpoints.CheckpointError, match="off for this session"):
        store.capture("turn-1", "start")
    monkeypatch.setattr(turn_checkpoints.subprocess, "run", real_run)
    with pytest.raises(turn_checkpoints.CheckpointError, match="off for this session"):
        store.capture("turn-2", "start")


def test_prune_never_removes_the_calling_session(repo, monkeypatch):
    for day, session in enumerate(("mine", "a", "b"), start=1):
        # GIT_* is stripped from git's environment; date the commit explicitly.
        monkeypatch.setattr(
            turn_checkpoints,
            "_IDENTITY",
            {**turn_checkpoints._IDENTITY, "GIT_COMMITTER_DATE": f"2026-01-0{day}T00:00:00+0000"},
        )
        TurnCheckpoints.open(repo, session).capture("turn-1", "start")
    removed = TurnCheckpoints.open(repo, "mine").prune(keep=1)
    refs = git(repo, "for-each-ref", "--format=%(refname)", turn_checkpoints.REF_ROOT)
    assert removed == 1
    assert "/mine/" in refs and "/b/" in refs and "/a/" not in refs


def test_sanitized_ids_do_not_collide():
    parts = {turn_checkpoints._ref_part(value) for value in ("a/b", "a.b", "a-b", "a b")}
    assert len(parts) == 4


# --- task #1992 follow-ups ------------------------------------------------------------


def test_a_huge_diff_is_stopped_at_the_ceiling_not_spooled(repo, monkeypatch):
    """Git's diff output is cut off at MAX_DIFF_READ_BYTES: neither node memory
    nor a temporary file (tmpfs is RAM) ever holds the whole diff (#1992, #2010)."""
    monkeypatch.setattr(turn_checkpoints, "MAX_DIFF_READ_BYTES", 4_096)
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    # Well past a pipe buffer, so git is still writing when the cap is hit.
    (repo / "huge.txt").write_text(("z" * 100 + "\n") * 20_000)
    end = store.capture("turn-1", "end")

    def no_spool(*_a, **_k):
        raise AssertionError("the content diff must not be spooled to a file")

    monkeypatch.setattr(turn_checkpoints.tempfile, "TemporaryFile", no_spool)
    real_popen = subprocess.Popen
    procs: list[subprocess.Popen] = []

    def spy(cmd, *args, **kwargs):
        proc = real_popen(cmd, *args, **kwargs)
        if "diff" in cmd and "--name-status" not in cmd:
            procs.append(proc)
        return proc

    monkeypatch.setattr(turn_checkpoints.subprocess, "Popen", spy)
    summary = store.changes(start, end)
    assert [f["path"] for f in summary["files"]] == ["huge.txt"]
    assert summary["diff"] == ""
    assert summary["diff_truncated"] and summary["diff_too_large"]
    assert len(procs) == 1
    assert procs[0].returncode is not None and procs[0].returncode < 0, "git was not stopped"


def test_a_diff_under_the_ceiling_is_kept(repo, monkeypatch):
    monkeypatch.setattr(turn_checkpoints, "MAX_DIFF_READ_BYTES", 1_000_000)
    store = TurnCheckpoints.open(repo, "item-1")
    start = store.capture("turn-1", "start")
    (repo / "kept.txt").write_text("one\nstreamed\n")
    end = store.capture("turn-1", "end")
    summary = store.changes(start, end)
    assert "+streamed" in summary["diff"]
    assert not summary["diff_truncated"] and not summary["diff_too_large"]


def _same_second(monkeypatch) -> None:
    monkeypatch.setattr(
        turn_checkpoints,
        "_IDENTITY",
        {**turn_checkpoints._IDENTITY, "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"},
    )


def test_prune_orders_sessions_created_in_the_same_second(repo, monkeypatch):
    """Committer dates tie within a second; the recorded sequence must not (task #1992)."""
    _same_second(monkeypatch)
    # Names sort in creation order, so a refname tie-break keeps the oldest.
    for session in ("a-oldest", "b-older", "c-newer", "d-newest"):
        store = TurnCheckpoints.open(repo, session)
        store.capture("turn-1", "start")
        store.capture("turn-1", "end")
    removed = TurnCheckpoints.open(repo, "d-newest").prune(keep=2)
    refs = git(repo, "for-each-ref", "--format=%(refname)", turn_checkpoints.REF_ROOT)
    assert removed == 4
    assert "/d-newest/" in refs and "/c-newer/" in refs
    assert "/b-older/" not in refs and "/a-oldest/" not in refs


def test_prune_ranks_a_session_by_its_latest_checkpoint(repo, monkeypatch):
    _same_second(monkeypatch)
    b = TurnCheckpoints.open(repo, "b")
    b.capture("turn-1", "start")
    TurnCheckpoints.open(repo, "a").capture("turn-1", "start")
    b.capture("turn-2", "start")  # b is active again, so a is now the oldest
    removed = TurnCheckpoints.open(repo, "c").prune(keep=1)
    refs = git(repo, "for-each-ref", "--format=%(refname)", turn_checkpoints.REF_ROOT)
    assert removed == 1
    assert "/b/" in refs and "/a/" not in refs


def test_prune_still_orders_legacy_refs_without_a_sequence(repo, monkeypatch):
    for day, session in enumerate(("old", "new"), start=1):
        date = f"2026-01-0{day}T00:00:00+0000"
        store = TurnCheckpoints.open(repo, session)
        commit = store.capture("turn-1", "start")
        # Rewrite as a pre-#1992 checkpoint: same tree, no seq= in the message.
        tree = git(repo, "rev-parse", f"{commit}^{{tree}}").strip()
        legacy = subprocess.run(
            ["git", "-C", str(repo), "commit-tree", tree, "-m", f"ua checkpoint {session}"],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **turn_checkpoints._IDENTITY, "GIT_COMMITTER_DATE": date},
        ).stdout.strip()
        git(repo, "update-ref", store.ref("turn-1", "start"), legacy)
    removed = TurnCheckpoints.open(repo, "other").prune(keep=1)
    refs = git(repo, "for-each-ref", "--format=%(refname)", turn_checkpoints.REF_ROOT)
    assert removed == 1
    assert "/new/" in refs and "/old/" not in refs


# --- an aborted turn whose agent is not confirmed stopped (PR #1149) ------------


def test_aborted_turn_skips_the_checkpoint_when_the_agent_is_not_confirmed_stopped(
    repo, tmp_path, monkeypatch
):
    """A snapshot with a possibly live writer is skipped, and the reason recorded."""
    from unified_agent import acp_client

    agent = FAKE_AGENT.replace(
        """        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})""",
        """        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "boom"}})""",
    )
    assert agent != FAKE_AGENT
    script = tmp_path / "fake_agent.py"
    script.write_text("#!" + sys.executable + "\n" + agent)
    script.chmod(0o755)
    monkeypatch.setenv("UA_GEMINI_BIN", str(script))
    monkeypatch.setenv("FAKE_EDIT", str(repo / "kept.txt"))
    real = acp_client.AcpClient.terminate

    def unconfirmed(self):
        real(self)  # really stop it; only the reported outcome is unconfirmed
        return False

    monkeypatch.setattr(acp_client.AcpClient, "terminate", unconfirmed)
    events: list[dict] = []
    result = acp_backend.AcpBackend("gemini").run(
        prompt="edit please",
        cwd=str(repo),
        session_dir=str(tmp_path / "sess"),
        session_id="item-43",
        agent_label="Desktop_Agent1",
        timeout_s=30,
        on_event=events.append,
    )
    assert result["status"] == "error", [e["kind"] for e in events]
    kinds = [e["kind"] for e in events]
    assert "turn_aborted" in kinds
    checkpoint = next(e for e in events if e["kind"] == "turn_checkpoint")
    assert checkpoint["data"]["skipped"] == "agent_not_confirmed_stopped"
    assert "ref" not in checkpoint["data"]
    assert git(repo, "for-each-ref", "refs/ua/checkpoints/item-43/turn-1/end") == ""
