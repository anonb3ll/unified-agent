"""A FIFO, socket or device in a scanned tree must not hang the audit (#2212).

`_iter_files` used to list every name `os.walk` returned, and `_read_text`
then called `read_bytes()`, which blocks forever in `open()` on a FIFO with no
writer. A device reports `st_size == 0`, so the size cap never applied to it.
Each call that could block runs under `_call_with_deadline`, which fails the
test instead of hanging the run.
"""

import builtins
import os
import socket
import stat
import threading
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, storage
from unified_agent.audit import engine, rules
from unified_agent.audit.anchor import VaultAnchorBackend
from unified_agent.safety import LEDGER_ANCHOR_DIR

DEADLINE_S = 15

needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")


def _release(fifo: Path) -> None:
    """Unblock a thread stuck in open() on `fifo`: a blocked reader gets a
    writer and then EOF, a blocked writer gets a reader and then EPIPE."""
    for flags in (os.O_WRONLY | os.O_NONBLOCK, os.O_RDONLY | os.O_NONBLOCK):
        try:
            fd = os.open(fifo, flags)
        except OSError:
            continue  # O_WRONLY fails when no reader is waiting
        os.close(fd)


def _call_with_deadline(fn, fifos: list[Path]):
    """Run `fn()` in a daemon thread. If it is still running after
    DEADLINE_S, unblock it through `fifos` and fail the test."""
    result: dict = {}

    def target():
        try:
            result["value"] = fn()
        except BaseException as exc:  # re-raised in the test thread
            result["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(DEADLINE_S)
    if t.is_alive():
        for _ in range(20):
            for fifo in fifos:
                _release(fifo)
            t.join(0.25)
            if not t.is_alive():
                break
        pytest.fail(f"still running after {DEADLINE_S}s: blocked on a non-regular file")
    if "error" in result:
        raise result["error"]
    return result["value"]


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "ok.py").write_text("print('hi')\n")
    return root


@needs_fifo
def test_run_audit_returns_with_a_fifo_in_the_tree(tmp_path):
    root = _project(tmp_path)
    fifos = [root / "x", root / "sub" / "pipe.py"]
    for fifo in fifos:
        os.mkfifo(fifo)

    report = _call_with_deadline(lambda: engine.run_audit(root), fifos)

    assert report["files_scanned"] == 1
    assert not any(fd["path"] in {"x", "sub/pipe.py"} for fd in report["findings"])


@needs_fifo
def test_run_audit_returns_with_a_fifo_named_git(tmp_path):
    # A `.git` FIFO is a file name, not a directory, so SKIP_DIRS never applied.
    root = _project(tmp_path)
    fifos = [root / ".git", root / "sub" / ".git"]
    for fifo in fifos:
        os.mkfifo(fifo)

    report = _call_with_deadline(lambda: engine.run_audit(root), fifos)

    assert report["files_scanned"] == 1


@needs_fifo
def test_git_fifo_beside_transcripts_fails_fast_and_closed(tmp_path):
    """PR #1304 review: a `.git` FIFO counts as a work tree, so each
    transcript with a SECRETS-KEY finding runs a git probe. Git refuses a
    non-regular `.git` at once ("not a regular file", exit 128) without
    opening it, so the probe returns unknown promptly and the finding keeps
    its full severity, as for any unreadable `.git`."""
    import json
    import time

    root = tmp_path / ".codex"
    sessions = root / "sessions" / "2026" / "01" / "01"
    sessions.mkdir(parents=True)
    fifo = sessions / ".git"
    os.mkfifo(fifo)
    kty, member = "kty", "k"
    jwk = {kty: "oct", member: "FAKE-FIXTURE-NOT-A-KEY-" + "0" * 24}
    for i in range(5):
        (sessions / f"rollout-{i}.jsonl").write_text(json.dumps({"content": jwk}) + "\n")

    start = time.monotonic()
    report = _call_with_deadline(lambda: engine.run_audit(root), [fifo])
    elapsed = time.monotonic() - start

    secrets = [f for f in report["findings"] if f["rule"] == "SECRETS-KEY"]
    assert len({f["path"] for f in secrets}) == 5
    assert all(f["severity"] == "high" for f in secrets)
    # Five git probes; a blocked one would take its 5 s timeout each.
    assert elapsed < 5


@needs_fifo
def test_run_audit_on_a_fifo_root_returns(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    report = _call_with_deadline(lambda: engine.run_audit(fifo), [fifo])

    assert report["files_scanned"] == 0


@needs_fifo
def test_iter_files_lists_non_regular_names_for_the_repo_rules(tmp_path, monkeypatch):
    # The repo rules match on names (a `.env` link to /dev/null is still an
    # exposed .env), so the walk keeps every name; only reading is refused.
    root = _project(tmp_path)
    (root / "target.txt").write_text("x\n")
    os.mkfifo(root / "fifo")
    (root / "link_to_file").symlink_to(root / "target.txt")
    (root / "link_to_fifo").symlink_to(root / "fifo")
    (root / "link_to_dev_null").symlink_to("/dev/null")
    (root / "dangling").symlink_to(root / "missing")
    expected = [
        "dangling",
        "fifo",
        "link_to_dev_null",
        "link_to_fifo",
        "link_to_file",
        "ok.py",
        "target.txt",
    ]
    if hasattr(socket, "AF_UNIX"):
        # Bind by relative name: AF_UNIX paths are capped near 108 bytes.
        monkeypatch.chdir(root / "sub")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind("sock")
        sock.close()
        assert stat.S_ISSOCK(os.lstat(root / "sub" / "sock").st_mode)
        expected.append("sub/sock")

    files, truncated = engine._iter_files(root)

    assert sorted(f.relative_to(root).as_posix() for f in files) == sorted(expected)
    assert truncated is False


@needs_fifo
def test_non_regular_files_do_not_use_up_the_file_cap(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    for i in range(3):
        os.mkfifo(root / f"a{i}")
    (root / "b.py").write_text("x = 1\n")
    (root / "c.py").write_text("x = 2\n")
    monkeypatch.setattr(engine, "MAX_FILES", 1)

    files, truncated = engine._iter_files(root)

    assert [f.name for f in files] == ["a0", "a1", "a2", "b.py"]
    assert truncated is True


def _env_findings(report: dict) -> list[dict]:
    return [fd for fd in report["findings"] if fd["path"] == ".env"]


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs /dev/null")
def test_env_link_to_a_device_is_still_reported_as_exposed(tmp_path):
    # Review of d9d15ae2: filtering the walk hid `.env -> /dev/null` from
    # check_env_exposure, losing the high finding main reports for it.
    regular = _project(tmp_path / "a")
    (regular / ".env").write_text("")
    linked = _project(tmp_path / "b")
    (linked / ".env").symlink_to("/dev/null")

    expected = _env_findings(engine.run_audit(regular))
    got = _env_findings(engine.run_audit(linked))

    assert [fd["rule"] for fd in expected] == ["SECRETS-ENV-EXPOSED"]
    assert expected[0]["severity"] == "high"
    assert got == expected


@needs_fifo
def test_env_fifo_is_reported_as_exposed_without_blocking(tmp_path):
    root = _project(tmp_path)
    fifo = root / ".env"
    os.mkfifo(fifo)

    report = _call_with_deadline(lambda: engine.run_audit(root), [fifo])

    assert [(fd["rule"], fd["severity"]) for fd in _env_findings(report)] == [
        ("SECRETS-ENV-EXPOSED", "high")
    ]
    assert report["files_scanned"] == 1


def _swap_for_fifo_before_open(monkeypatch, target: Path, skip: int = 0) -> dict:
    """Replace `target` with a FIFO after any type check and just before it
    is opened, whether the code goes through os.open, open(), Path.read_* or
    Path.write_*. The first `skip` opens of `target` pass untouched, so a
    later reader or writer can be hit. Only `target` is swapped; every other
    path passes through. The returned state says whether the swap happened."""
    state = {"swapped": False, "opens": 0}

    def swap(path) -> None:
        if state["swapped"] or Path(path) != target:
            return
        state["opens"] += 1
        if state["opens"] <= skip:
            return
        state["swapped"] = True
        target.unlink()
        os.mkfifo(target)

    real_os_open = os.open
    real_open = builtins.open
    real_path_methods = {
        name: getattr(Path, name)
        for name in ("read_text", "read_bytes", "write_text", "write_bytes")
    }

    def os_open(path, *args, **kwargs):
        swap(path)
        return real_os_open(path, *args, **kwargs)

    def open_(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            swap(file)
        return real_open(file, *args, **kwargs)

    def hooked(real):
        def method(self, *args, **kwargs):
            swap(self)
            return real(self, *args, **kwargs)

        return method

    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(builtins, "open", open_)
    for name, real in real_path_methods.items():
        monkeypatch.setattr(Path, name, hooked(real))
    return state


@needs_fifo
def test_read_text_does_not_block_when_a_file_becomes_a_fifo(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    target.write_text("print('hi')\n")
    _swap_for_fifo_before_open(monkeypatch, target)

    assert _call_with_deadline(lambda: engine._read_text(target), [target]) is None


@needs_fifo
def test_ignore_file_that_becomes_a_fifo_does_not_block(tmp_path, monkeypatch):
    root = _project(tmp_path)
    ignore = root / ".ua-audit-ignore"
    ignore.write_text("ok.py\n")
    _swap_for_fifo_before_open(monkeypatch, ignore)

    report = _call_with_deadline(lambda: engine.run_audit(root), [ignore])

    assert report["files_scanned"] == 1


def test_ignore_file_is_still_applied(tmp_path):
    root = _project(tmp_path)
    (root / "skip.py").write_text("x = 1\n")
    (root / ".ua-audit-ignore").write_bytes(b"# comment\r\nskip.py\r\n\n")

    report = engine.run_audit(root)

    # ok.py and the ignore file itself are scanned; skip.py is excluded.
    assert report["files_scanned"] == 2
    assert report["summary"]["excluded"] == 1


@needs_fifo
def test_read_text_does_not_block_on_a_fifo(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    assert _call_with_deadline(lambda: engine._read_text(fifo), [fifo]) is None


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs /dev/null")
def test_read_text_refuses_a_character_device():
    # st_size is 0 on a device, so the size cap alone never refused one.
    assert engine._read_text(Path("/dev/null")) is None


def test_read_text_still_reads_regular_files_and_links_to_them(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("print('hi')\n")
    link = tmp_path / "link.py"
    link.symlink_to(target)

    assert engine._read_text(target) == "print('hi')\n"
    assert engine._read_text(link) == "print('hi')\n"


def test_read_text_refuses_oversized_and_binary_files(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.config, "SEARCH_FILE_MAX_BYTES", 8)
    big = tmp_path / "big.txt"
    big.write_text("x" * 9)
    exact = tmp_path / "exact.txt"
    exact.write_text("x" * 8)
    binary = tmp_path / "bin"
    binary.write_bytes(b"ab\x00cd")

    assert engine._read_text(big) is None
    assert engine._read_text(exact) == "x" * 8
    assert engine._read_text(binary) is None


@needs_fifo
def test_vault_anchor_scan_skips_a_fifo_note(tmp_path):
    # conftest points UA_VAULT_PATH at tmp_path / "vault".
    anchor_dir = tmp_path / "vault" / LEDGER_ANCHOR_DIR
    anchor_dir.mkdir(parents=True)
    head = "a" * 64
    (anchor_dir / "2026-10-24.md").write_text(f"- head_hash: {head}\n")
    fifo = anchor_dir / "2026-10-25.md"
    os.mkfifo(fifo)
    backend = VaultAnchorBackend()

    refs = _call_with_deadline(backend.candidate_refs, [fifo])
    found = _call_with_deadline(lambda: backend.find_ref_for_head(head), [fifo])

    assert [h for h, _ in refs] == [head]
    assert found == {"backend": "vault", "path": f"{LEDGER_ANCHOR_DIR}/2026-10-24.md"}


def _anchor_dir(tmp_path: Path) -> Path:
    # conftest points UA_VAULT_PATH at tmp_path / "vault".
    anchor_dir = tmp_path / "vault" / LEDGER_ANCHOR_DIR
    anchor_dir.mkdir(parents=True)
    return anchor_dir


@needs_fifo
def test_vault_candidate_refs_skip_a_note_that_becomes_a_fifo(tmp_path, monkeypatch):
    anchor_dir = _anchor_dir(tmp_path)
    head, other = "a" * 64, "b" * 64
    (anchor_dir / "2026-10-24.md").write_text(f"- head_hash: {head}\n")
    note = anchor_dir / "2026-10-25.md"
    note.write_text(f"- head_hash: {other}\n")
    _swap_for_fifo_before_open(monkeypatch, note)

    refs = _call_with_deadline(VaultAnchorBackend().candidate_refs, [note])

    assert [h for h, _ in refs] == [head]


@needs_fifo
def test_vault_find_ref_skips_a_note_that_becomes_a_fifo(tmp_path, monkeypatch):
    anchor_dir = _anchor_dir(tmp_path)
    head = "a" * 64
    (anchor_dir / "2026-10-24.md").write_text(f"- head_hash: {head}\n")
    # Scanned newest first, so this note is opened before the one that matches.
    note = anchor_dir / "2026-10-25.md"
    note.write_text(f"- head_hash: {'b' * 64}\n")
    _swap_for_fifo_before_open(monkeypatch, note)

    found = _call_with_deadline(lambda: VaultAnchorBackend().find_ref_for_head(head), [note])

    assert found == {"backend": "vault", "path": f"{LEDGER_ANCHOR_DIR}/2026-10-24.md"}


def test_vault_anchor_scan_keeps_strict_utf8(tmp_path):
    anchor_dir = _anchor_dir(tmp_path)
    (anchor_dir / "2026-10-25.md").write_bytes(b"- head_hash: " + b"a" * 64 + b"\n\xff\n")

    with pytest.raises(UnicodeDecodeError):
        VaultAnchorBackend().candidate_refs()


def test_vault_anchor_scan_keeps_text_mode_line_endings(tmp_path):
    # Path.read_text turned "\r" and "\r\n" into "\n"; the shared reader does too.
    anchor_dir = _anchor_dir(tmp_path)
    cr, crlf = "c" * 64, "d" * 64
    (anchor_dir / "2026-10-25.md").write_bytes(
        f"title\r- head_hash: {cr}\r- head_hash: {crlf}\r\n".encode()
    )

    refs = VaultAnchorBackend().candidate_refs()

    assert sorted(h for h, _ in refs) == [cr, crlf]


def test_ignore_file_keeps_text_mode_line_endings(tmp_path):
    root = _project(tmp_path)
    (root / "a.py").write_text("x = 1\n")
    (root / "b.py").write_text("x = 2\n")
    (root / ".ua-audit-ignore").write_bytes(b"a.py\rb.py\x0cnot-a-split\n")

    report = engine.run_audit(root)

    # "\r" ends a line, as in text-mode open(), so a.py is excluded. A form
    # feed does not (str.splitlines would split there), so b.py is scanned.
    assert report["summary"]["excluded"] == 1
    assert report["files_scanned"] == 3  # ok.py, b.py, .ua-audit-ignore


# Review at 3d01515c: the repo rules read `.claude/settings.json` and each
# `.gitignore` again after the content pass, with is_file() then read_text().
# Each test excludes the file from the content pass, so the repo rule is the
# first reader to open it and the swap lands on that open.


@needs_fifo
def test_agent_settings_that_becomes_a_fifo_does_not_block(tmp_path, monkeypatch):
    root = _project(tmp_path)
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"defaultMode": "bypassPermissions"}\n')
    state = _swap_for_fifo_before_open(monkeypatch, settings)

    report = _call_with_deadline(
        lambda: engine.run_audit(root, excludes=[".claude/settings.json"]), [settings]
    )

    assert state["swapped"]
    assert report["files_scanned"] == 1


@needs_fifo
def test_gitignore_that_becomes_a_fifo_does_not_block(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / ".env").write_text("")
    gitignore = root / ".gitignore"
    gitignore.write_text(".env\n")
    state = _swap_for_fifo_before_open(monkeypatch, gitignore)

    report = _call_with_deadline(
        lambda: engine.run_audit(root, excludes=[".gitignore"]), [gitignore]
    )

    assert state["swapped"]
    # The swapped .gitignore no longer covers .env, so the exposure is reported.
    assert [fd["rule"] for fd in _env_findings(report)] == ["SECRETS-ENV-EXPOSED"]


def test_load_json_keeps_its_parsing(tmp_path):
    good = tmp_path / "good.json"
    good.write_bytes(b'{"a": 1,\r\n "b": "\xff"}\r\n')
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    array = tmp_path / "array.json"
    array.write_text("[1]")

    assert rules._load_json(good) == {"a": 1, "b": "\ufffd"}
    assert rules._load_json(bad) is None
    assert rules._load_json(array) is None
    assert rules._load_json(tmp_path / "missing.json") is None


@needs_fifo
def test_load_json_refuses_a_fifo(tmp_path):
    fifo = tmp_path / "settings.json"
    os.mkfifo(fifo)

    assert _call_with_deadline(lambda: rules._load_json(fifo), [fifo]) is None


def test_gitignore_covers_env_keeps_its_line_handling(tmp_path):
    crlf = tmp_path / "crlf"
    crlf.write_bytes(b"# x\r\n  .env*  \r\nnode_modules\r\n")
    cr = tmp_path / "cr"
    cr.write_bytes(b"dist\r.env\r")
    other = tmp_path / "other"
    other.write_text("node_modules\n")

    assert rules._gitignore_covers_env(crlf) is True
    assert rules._gitignore_covers_env(cr) is True
    assert rules._gitignore_covers_env(other) is False
    assert rules._gitignore_covers_env(tmp_path / "missing") is False


@needs_fifo
def test_gitignore_covers_env_refuses_a_fifo(tmp_path):
    fifo = tmp_path / ".gitignore"
    os.mkfifo(fifo)

    assert _call_with_deadline(lambda: rules._gitignore_covers_env(fifo), [fifo]) is False


# Review at 79a1b3af: the remediation pass, the vault anchor verifier and the
# re-anchor writer read or write scanned paths again after the protected pass.


def _finding(report: dict, rule: str) -> dict:
    (fd,) = [fd for fd in report["findings"] if fd["rule"] == rule]
    return fd


def _assert_fifo(path: Path) -> None:
    assert stat.S_ISFIFO(os.lstat(path).st_mode)


@needs_fifo
def test_remediate_with_a_fifo_gitignore_does_not_block(tmp_path):
    root = _project(tmp_path)
    (root / ".env").write_text("")
    (root / "CLAUDE.md").write_text("# rules\n")
    gitignore = root / ".gitignore"
    os.mkfifo(gitignore)

    report = _call_with_deadline(lambda: engine.run_audit(root, remediate=True), [gitignore])

    fd = _finding(report, "SECRETS-ENV-EXPOSED")
    assert fd["fixed"] is False
    assert fd["message"].startswith("[AUTO-FIX-FAILED]")
    _assert_fifo(gitignore)


@needs_fifo
def test_remediate_does_not_write_into_a_fifo_gitignore(tmp_path):
    root = _project(tmp_path)
    (root / ".git").mkdir()  # HYGIENE-NO-GITIGNORE needs a .git to fire
    (root / "CLAUDE.md").write_text("# rules\n")
    gitignore = root / ".gitignore"
    os.mkfifo(gitignore)

    report = _call_with_deadline(lambda: engine.run_audit(root, remediate=True), [gitignore])

    fd = _finding(report, "HYGIENE-NO-GITIGNORE")
    assert fd["fixed"] is False
    assert fd["message"].startswith("[AUTO-FIX-FAILED]")
    _assert_fifo(gitignore)


@needs_fifo
def test_remediate_does_not_write_into_a_fifo_claude_md(tmp_path):
    root = _project(tmp_path)
    claude_md = root / "CLAUDE.md"
    os.mkfifo(claude_md)

    report = _call_with_deadline(lambda: engine.run_audit(root, remediate=True), [claude_md])

    fd = _finding(report, "AGENT-NO-INSTRUCTIONS")
    assert fd["fixed"] is False
    assert fd["message"].startswith("[AUTO-FIX-FAILED]")
    _assert_fifo(claude_md)


@needs_fifo
@pytest.mark.parametrize(
    "skip,step",
    [(1, "remediation read"), (2, "remediation write")],
)
def test_remediate_hidden_unicode_survives_a_fifo_swap(tmp_path, monkeypatch, skip, step):
    # Open 1 is the content pass that finds the character; the swap lands on
    # the remediation's read (open 2) or its write (open 3).
    root = _project(tmp_path)
    agents = root / "AGENTS.md"
    agents.write_text("be\u200b nice\n")
    state = _swap_for_fifo_before_open(monkeypatch, agents, skip=skip)

    report = _call_with_deadline(lambda: engine.run_audit(root, remediate=True), [agents])

    assert state["swapped"], step
    fd = _finding(report, "AGENT-HIDDEN-UNICODE")
    assert fd["fixed"] is False
    assert fd["message"].startswith("[AUTO-FIX-FAILED]")
    _assert_fifo(agents)


def test_remediate_still_fixes_regular_files(tmp_path):
    root = _project(tmp_path)
    (root / ".env").write_text("")
    (root / ".gitignore").write_bytes(b"node_modules\r\n")
    (root / "AGENTS.md").write_text("be\u200b nice\n")

    report = engine.run_audit(root, remediate=True)

    assert _finding(report, "SECRETS-ENV-EXPOSED")["fixed"] is True
    assert _finding(report, "AGENT-HIDDEN-UNICODE")["fixed"] is True
    # Text-mode read then write, as before: CRLF comes back as LF.
    assert (root / ".gitignore").read_bytes() == (
        b"node_modules\n\n# Added by unified-agent auto-remediation\n.env*\n"
    )
    assert (root / "AGENTS.md").read_text() == "be nice\n"

    bare = _project(tmp_path / "bare")
    (bare / ".git").mkdir()
    report = engine.run_audit(bare, remediate=True)
    assert _finding(report, "HYGIENE-NO-GITIGNORE")["fixed"] is True
    assert _finding(report, "AGENT-NO-INSTRUCTIONS")["fixed"] is True
    assert (bare / ".gitignore").is_file()
    assert (bare / "CLAUDE.md").is_file()


@needs_fifo
def test_vault_verify_after_discovery_survives_a_fifo_swap(tmp_path, monkeypatch):
    anchor_dir = _anchor_dir(tmp_path)
    head = "a" * 64
    note = anchor_dir / "2026-10-25.md"
    note.write_text(f"- head_hash: {head}\n")
    # Open 1 is discovery (candidate_refs); the swap lands on verify's read.
    state = _swap_for_fifo_before_open(monkeypatch, note, skip=1)
    backend = VaultAnchorBackend()

    def discover_then_verify():
        ((found, ref),) = backend.candidate_refs()
        return found, backend.verify(ref, head)

    found, verification = _call_with_deadline(discover_then_verify, [note])

    assert state["swapped"]
    assert found == head
    assert verification["verified"] is False


@needs_fifo
def test_vault_verify_refuses_a_fifo_note(tmp_path):
    anchor_dir = _anchor_dir(tmp_path)
    note = anchor_dir / "2026-10-25.md"
    os.mkfifo(note)
    ref = {"backend": "vault", "path": f"{LEDGER_ANCHOR_DIR}/2026-10-25.md"}

    result = _call_with_deadline(lambda: VaultAnchorBackend().verify(ref, "a" * 64), [note])

    assert result["verified"] is False


def test_vault_verify_still_reads_regular_notes(tmp_path):
    anchor_dir = _anchor_dir(tmp_path)
    head = "a" * 64
    (anchor_dir / "2026-10-25.md").write_bytes(f"x\r- head_hash: {head}\r\n".encode())
    ref = {"backend": "vault", "path": f"{LEDGER_ANCHOR_DIR}/2026-10-25.md"}

    assert VaultAnchorBackend().verify(ref, head)["verified"] is True
    missing = {"backend": "vault", "path": f"{LEDGER_ANCHOR_DIR}/2026-10-01.md"}
    assert VaultAnchorBackend().verify(missing, head)["reason"] == "anchor note missing"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    (tmp_path / "vault" / "Logs").mkdir(parents=True, exist_ok=True)
    return tmp_path / "vault" / LEDGER_ANCHOR_DIR


@needs_fifo
def test_reanchor_refuses_a_fifo_note(ledger):
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 1})
    ledger.mkdir(parents=True)
    note = ledger / "2026-10-25.md"
    os.mkfifo(note)

    with pytest.raises(ToolError, match="not a regular file"):
        _call_with_deadline(lambda: reanchor_chain_head(day="2026-10-25"), [note])
    _assert_fifo(note)


@needs_fifo
@pytest.mark.parametrize("skip,step", [(0, "read"), (1, "append")])
def test_reanchor_survives_a_fifo_swap(ledger, monkeypatch, skip, step):
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 1})
    assert reanchor_chain_head(day="2026-10-25")["anchor_action"] == "created"
    events.record_event("agent.task.updated", payload={"task_id": 2})
    note = ledger / "2026-10-25.md"
    # The rerun reads the note (open 1), then appends to it (open 2).
    state = _swap_for_fifo_before_open(monkeypatch, note, skip=skip)

    with pytest.raises(ToolError, match="not a regular file"):
        _call_with_deadline(lambda: reanchor_chain_head(day="2026-10-25"), [note])
    assert state["swapped"], step
    _assert_fifo(note)


def test_reanchor_still_creates_and_appends(ledger):
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 1})
    assert reanchor_chain_head(day="2026-10-25")["anchor_action"] == "created"
    assert reanchor_chain_head(day="2026-10-25")["anchor_action"] == "unchanged"
    events.record_event("agent.task.updated", payload={"task_id": 2})
    assert reanchor_chain_head(day="2026-10-25")["anchor_action"] == "appended"
    assert (ledger / "2026-10-25.md").read_text().count("- head_hash:") >= 2


# Review at b8a26c06: a dated note without witness shape reached
# note_is_locked(), which reopened it with a blocking open().

LOCKED_NOTE = "---\nlocked: true\n---\nan operator note\n"


@needs_fifo
def test_reanchor_lock_check_does_not_reopen_the_note(ledger, monkeypatch):
    from unified_agent.audit.reanchor import reanchor_chain_head
    from unified_agent.safety import VaultLockedError

    events.record_event("agent.task.updated", payload={"task_id": 1})
    ledger.mkdir(parents=True)
    note = ledger / "2026-10-25.md"
    note.write_text(LOCKED_NOTE)
    # Open 1 is the re-anchor's safe read; a second open would be the lock check.
    state = _swap_for_fifo_before_open(monkeypatch, note, skip=1)

    with pytest.raises(VaultLockedError):
        _call_with_deadline(lambda: reanchor_chain_head(day="2026-10-25"), [note])
    assert state["opens"] == 1
    assert not state["swapped"]
    monkeypatch.undo()  # drop the open hooks before the test reads the note itself
    assert note.read_text() == LOCKED_NOTE


def test_reanchor_still_refuses_an_unlocked_non_witness_note(ledger):
    from unified_agent.audit.reanchor import reanchor_chain_head
    from unified_agent.safety import VaultLockedError

    events.record_event("agent.task.updated", payload={"task_id": 1})
    ledger.mkdir(parents=True)
    note = ledger / "2026-10-25.md"
    note.write_text("an operator note\n")

    with pytest.raises(ToolError, match="not a witness") as info:
        reanchor_chain_head(day="2026-10-25")
    assert not isinstance(info.value, VaultLockedError)


@needs_fifo
def test_note_is_locked_refuses_a_fifo(tmp_path):
    from unified_agent.note_lock import note_is_locked

    fifo = tmp_path / "vault" / "note.md"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)

    with pytest.raises(ToolError, match="not a regular file"):
        _call_with_deadline(lambda: note_is_locked(fifo), [fifo])


@needs_fifo
def test_note_is_locked_survives_a_fifo_swap(tmp_path, monkeypatch):
    from unified_agent.note_lock import note_is_locked

    note = tmp_path / "vault" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text(LOCKED_NOTE)
    state = _swap_for_fifo_before_open(monkeypatch, note)

    with pytest.raises(ToolError, match="not a regular file"):
        _call_with_deadline(lambda: note_is_locked(note), [note])
    assert state["swapped"]


@pytest.mark.parametrize(
    "content,locked",
    [
        (LOCKED_NOTE, True),
        ("---\nlocked: false\n---\nx\n", False),
        ("no frontmatter\n", False),
        # Frontmatter that closes past the first 8 KiB read is widened, not missed.
        ("---\n" + "k: v\n" * 3000 + "locked: true\n---\nx\n", True),
        # Never closed within the 1 MiB ceiling: fails closed.
        ("---\n" + "k: v\n" * 220_000, True),
    ],
    ids=["locked", "unlocked", "no-frontmatter", "widened", "unterminated"],
)
def test_note_lock_verdict_from_bytes_matches_the_file_reader(tmp_path, content, locked):
    from unified_agent.note_lock import note_bytes_are_locked, note_is_locked

    note = tmp_path / "vault" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text(content)

    assert note_is_locked(note) is locked
    assert note_bytes_are_locked(content.encode()) is locked


def test_note_is_locked_on_a_missing_note_is_false(tmp_path):
    from unified_agent.note_lock import note_is_locked

    assert note_is_locked(tmp_path / "vault" / "missing.md") is False
