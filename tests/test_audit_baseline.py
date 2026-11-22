"""Operator audit baseline (`unified-agent-audit baseline`, task #2323).

An operator accepts a previewed secret finding in an agent's own home (a
fixture JWK in an agent log, say) by value hash. Runs given the baseline
grade that exact (rule, value, file) as INFO; a new value, the same value
under another rule or in another file, repo source, and every credential-
store or permission finding keep full severity. The plain audit command,
which every verification gate runs, never applies it unless asked. The file
holds hashes only, and a damaged or swapped one is ignored with a warning.
"""

from __future__ import annotations

import builtins
import json
import os
import re
import stat
from pathlib import Path

import pytest
import test_audit_surfaces as ts
import test_audit_transcripts as tx

from unified_agent import config
from unified_agent.audit import baseline as bl
from unified_agent.audit.cli import main as audit_main
from unified_agent.audit.engine import run_audit
from unified_agent.audit.grade import gated
from unified_agent.audit.surfaces import run_surface_scan

reach_within_tmp = tx.reach_within_tmp  # the shared fixture, by name


def _jwk_text(value: str) -> str:
    """A symmetric JWK holding `value`; names are variables so this file is not one."""
    kty, member = "kty", "k"
    return json.dumps({kty: "oct", member: value})


def _live_jwk_line(n: int) -> str:
    return _jwk_text(ts._opaque(n))


def _codex(tmp_path, *texts):
    """A `.codex` tree with one unverified rollout per text (each HIGH)."""
    root = tmp_path / ".codex"
    for i, text in enumerate(texts):
        rel = ts.SESSION_REL.replace("2026/10/25", f"2026/10/{25 + i}")
        tx._write(root, rel, ts._unverified_rollout(text))
    return root


def _accept(root, reason="reviewed fixture JWKs", *extra) -> int:
    return audit_main(["baseline", "accept", str(root), "--reason", reason, "--yes", *extra])


def _entries() -> list[dict]:
    loaded = bl.load()
    assert loaded.warning is None
    return loaded.entries


def _with_baseline(root):
    return run_audit(root, baseline=bl.default_path())


def test_accept_then_rerun_grades_the_reviewed_value_info(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("USER", "op-alice")
    root = _codex(tmp_path, _live_jwk_line(1))
    assert gated(run_audit(root)["findings"], "high")

    assert _accept(root) == 0
    out = capsys.readouterr().out
    assert "accepted 1 finding" in out

    report = _with_baseline(root)
    found = tx._secrets(report)
    assert [fd["severity"] for fd in found] == ["info"]
    assert found[0]["message"].startswith("accepted (reviewed 20")
    assert "by op-alice)" in found[0]["message"]
    assert report["baseline"]["accepted"] == 1
    assert report["baseline"]["warning"] is None
    assert not gated(report["findings"], "high")
    # Without the baseline the same run is unchanged.
    assert gated(run_audit(root)["findings"], "high")


def test_a_new_value_or_the_same_value_elsewhere_stays_high(tmp_path):
    root = _codex(tmp_path, _live_jwk_line(1))
    assert _accept(root) == 0
    session = next(root.rglob("rollout-*.jsonl"))
    # A second record with a new value in the accepted file, and the accepted
    # value copied into a file that was never reviewed.
    session.write_text(session.read_text() + ts._unverified_rollout(_live_jwk_line(2)))
    _codex(tmp_path / "other", _live_jwk_line(1))
    copy = next((tmp_path / "other" / ".codex").rglob("rollout-*.jsonl"))
    (root / "sessions" / "copy.jsonl").write_text(copy.read_text())

    report = _with_baseline(root)
    by_sev = {}
    for fd in tx._secrets(report):
        by_sev.setdefault(fd["severity"], []).append(fd["path"])

    assert by_sev.get("info") == [os.path.relpath(session, root)]
    assert sorted(by_sev["high"]) == sorted([os.path.relpath(session, root), "sessions/copy.jsonl"])
    assert gated(report["findings"], "high")


def test_the_same_value_under_another_rule_keeps_its_severity(tmp_path):
    value = ts._opaque(3)
    root = _codex(tmp_path, _jwk_text(value))
    assert _accept(root) == 0
    session = next(root.rglob("rollout-*.jsonl"))
    session.write_text(ts._unverified_rollout(f"api_key = '{value}'"))

    found = tx._secrets(_with_baseline(root))

    assert [fd["severity"] for fd in found] == ["high"]
    assert "assigned secret literal" in found[0]["message"]


@pytest.mark.parametrize("damage", ["garbage", "tampered", "group_readable", "symlink"])
def test_a_damaged_baseline_is_ignored_with_a_warning(tmp_path, damage, capsys):
    root = _codex(tmp_path, _live_jwk_line(1))
    assert _accept(root) == 0
    path = bl.default_path()
    if damage == "garbage":
        path.write_text("{not json")
    elif damage == "tampered":
        lines = path.read_text().splitlines()
        entry = json.loads(lines[1])
        entry["path"] = "sessions/elsewhere.jsonl"
        lines[1] = json.dumps(entry)
        path.write_text("\n".join(lines) + "\n")
    elif damage == "group_readable":
        path.chmod(0o640)
    else:
        real = path.with_name("real.json")
        path.rename(real)
        path.symlink_to(real)
    capsys.readouterr()

    report = _with_baseline(root)

    assert {fd["severity"] for fd in tx._secrets(report)} == {"high"}
    assert report["baseline"]["accepted"] == 0
    assert "ignored" in report["baseline"]["warning"]
    # The CLI says so on stderr and still gates.
    assert audit_main([str(root), "--baseline"]) == 1
    assert "audit baseline" in capsys.readouterr().err
    # A damaged file is never appended to or replaced.
    before = path.read_bytes() if damage != "symlink" else None
    assert _accept(root) == 1
    if before is not None:
        assert path.read_bytes() == before


def test_baseline_file_is_owner_only_and_append_only(tmp_path):
    root = _codex(tmp_path, _live_jwk_line(1), _live_jwk_line(2))
    assert _accept(root, "first review") == 0
    path = bl.default_path()
    first = _entries()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(first) == 2
    # Re-accepting the same findings adds nothing; a new one is appended.
    assert _accept(root, "again") == 0
    assert _entries() == first
    tx._write(
        root, "sessions/2026/10/27/rollout-x.jsonl", ts._unverified_rollout(_live_jwk_line(7))
    )
    assert _accept(root, "second review") == 0
    after = _entries()
    assert after[: len(first)] == first
    assert len(after) == len(first) + 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_list_shows_truncated_hashes_and_never_a_value(tmp_path, capsys):
    value = ts._opaque(1)
    root = _codex(tmp_path, _jwk_text(value))
    assert _accept(root, "fixture review") == 0
    raw = bl.default_path().read_text()
    assert value not in raw
    full = _entries()[0]["sha256"]
    capsys.readouterr()

    assert audit_main(["baseline", "list"]) == 0
    text = capsys.readouterr().out
    assert audit_main(["baseline", "list", "--json"]) == 0
    listed = capsys.readouterr().out

    for out in (text, listed):
        assert value not in out
        assert full not in out
        assert full[:12] in out
    assert "fixture review" in text
    assert "1 entr" in text
    assert json.loads(listed)["entries"][0]["sha256"] == full[:12]


def test_credential_store_and_permission_rules_are_never_accepted(tmp_path, reach_within_tmp):
    root = _codex(tmp_path, _live_jwk_line(1))
    tx._write(root, "auth.json", json.dumps({"tokens": {"id_token": tx._vendor_key()}}), 0o644)
    assert _accept(root) == 0

    report = _with_baseline(root)
    rules = {(fd["rule"], fd["severity"]) for fd in report["findings"] if fd["path"] == "auth.json"}

    assert ("SECRETS-STORE-PERMS", "medium") in rules
    entries = _entries()
    assert {entry["path"] for entry in entries} == {
        os.path.relpath(next(root.rglob("rollout-*")), root)
    }
    assert all(entry["rule"].startswith("SECRETS-KEY:") for entry in entries)


def test_accept_needs_a_reason_and_records_the_verified_identity(tmp_path, monkeypatch, capsys):
    root = _codex(tmp_path, _live_jwk_line(1))
    with pytest.raises(SystemExit) as exc:
        audit_main(["baseline", "accept", str(root)])
    assert exc.value.code == 2
    assert _accept(root, "   ") == 2
    assert not bl.default_path().exists()

    monkeypatch.setenv("USER", "op-shell")
    token = config.agent_identity_var.set("Desktop_Reviewer")
    try:
        assert _accept(root) == 0
    finally:
        config.agent_identity_var.reset(token)
    assert {e["who"] for e in _entries()} == {"Desktop_Reviewer"}


def test_surfaces_report_states_the_accepted_count(tmp_path, capsys):
    root = _codex(tmp_path, _live_jwk_line(1))
    assert _accept(root) == 0

    report = run_surface_scan(roots=[str(root)], allowlist=[tmp_path], baseline=bl.default_path())
    assert report["baseline"]["accepted"] == 1
    assert report["summary"]["high"] == 0
    # The steward cron's command over an agent-home root: surfaces applies
    # the baseline by default; the plain command does not.
    capsys.readouterr()
    assert audit_main(["surfaces", str(root), "--allowlist", str(tmp_path)]) == 0
    assert "accepted=1" in capsys.readouterr().out
    assert audit_main(["surfaces", str(root), "--allowlist", str(tmp_path), "--no-baseline"]) == 1
    assert audit_main([str(root)]) == 1


def test_a_rotated_value_refiles_past_an_accepted_risk_task(tmp_path):
    """Task #2419: `[accepted-risk]` stops refiling by class, while the gate
    reads the value-keyed baseline. A value that rotated in a baselined file
    fails the gate, so it must get an owner, and one owner only."""
    root = _codex(tmp_path, _live_jwk_line(1))

    def scan():
        return run_surface_scan(
            roots=[str(root)],
            allowlist=[tmp_path],
            baseline=bl.default_path(),
            file_tasks=True,
        )

    [filed] = scan()["tasks"]["tasks"]
    tx.task_update(
        filed["id"],
        status="cancelled",
        append_note=f"{tx.ACCEPTED_RISK_TAG} reviewed fixture",
        actor="test-agent",
    )
    assert _accept(root) == 0
    assert scan()["tasks"]["created"] == 0

    # A file nobody baselined stays covered by the accepted class.
    other = tmp_path / "unreviewed"
    tx._write(other, "x.jsonl", ts._unverified_rollout(_live_jwk_line(9)))
    (root / "sessions" / "never-accepted.jsonl").write_text(
        (other / "x.jsonl").read_text(),
    )
    quiet = scan()
    assert quiet["summary"]["high"] == 1
    assert quiet["tasks"]["created"] == 0

    (root / "sessions" / "never-accepted.jsonl").unlink()
    session = next(root.rglob("rollout-*.jsonl"))
    session.write_text(ts._unverified_rollout(_live_jwk_line(2)))

    rotated = scan()
    assert rotated["summary"]["high"] == 1
    assert rotated["tasks"]["created"] == 1
    [task] = tx.task_list(status="open", notes_contain="[audit-triage:")["tasks"]
    assert "operator baseline holds an older value" in task["notes"]

    assert scan()["tasks"]["created"] == 0  # the new open task is the owner

    assert _accept(root) == 0  # re-accepting the new value quiets the file
    assert scan()["summary"]["high"] == 0


def test_plain_audit_ignores_the_baseline_and_repo_source_never_qualifies(tmp_path, capsys):
    """Review P1: CI, local-gate, the hooks and the bug worker run the plain
    command, so it applies the baseline only on --baseline, and a file inside
    a git work tree is never accepted, even when asked."""
    home = _codex(tmp_path, _live_jwk_line(1))
    assert _accept(home) == 0
    assert audit_main([str(home)]) == 1
    assert audit_main([str(home), "--baseline"]) == 0

    proj = tmp_path / "proj"
    tx._write(proj, "src/app.sh", f"export KEY={tx._vendor_key()}\n")
    tx._git_init_commit(proj, "src/app.sh")
    collected: list = []
    run_audit(proj, collect_values=collected)
    assert [item["in_git"] for item in collected] == [True]
    # Forced past the CLI's filter: the engine still refuses repo source.
    bl.append(collected, root=proj, reason="forced")
    capsys.readouterr()
    assert _accept(proj) == 0
    assert "skipping 1 finding(s) inside a git work tree" in capsys.readouterr().out

    assert audit_main([str(proj)]) == 1
    assert audit_main([str(proj), "--baseline"]) == 1
    report = _with_baseline(proj)
    assert [fd["severity"] for fd in tx._secrets(report)] == ["high"]
    assert report["baseline"]["accepted"] == 0


_REPO = Path(__file__).resolve().parents[1]
_AUDIT_CALL = re.compile(r"unified-agent-audit|run_audit\.py|unified_agent\.audit\.cli")
_GATES = {
    ".github/workflows/ci.yml",
    "scripts/local-gate.sh",
    "scripts/install-hooks.sh",
    "scripts/hook_pre_push.py",
    "src/unified_agent/bug_worker/gates.py",
}


def test_no_verification_gate_passes_baseline():
    """Every scripted audit run in CI, the gates, the hooks and the bug
    worker uses the plain command without --baseline."""
    callers = set()
    for top in ("scripts", ".github/workflows", ".agents", "src/unified_agent/bug_worker"):
        for path in (_REPO / top).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".sh", ".yml", ".yaml", ".json"}:
                continue
            text = path.read_text(errors="replace")
            if _AUDIT_CALL.search(text):
                callers.add(path.relative_to(_REPO).as_posix())
                assert "--baseline" not in text, path
    assert _GATES <= callers


def test_a_file_swapped_in_after_open_is_judged_on_its_descriptor(tmp_path, monkeypatch):
    """Review P2: the mode and owner checks run on the open descriptor, so a
    forged world-writable baseline swapped in at open time is refused."""
    root = _codex(tmp_path, _live_jwk_line(1))
    assert _accept(root) == 0
    path = bl.default_path()
    forged = path.read_bytes()
    real_open = os.open
    swaps = []

    def swapping_open(target, flags, *args, **kwargs):
        if Path(target) == path and len(swaps) < 2:
            swaps.append(flags)
            tmp = path.with_name("forged.tmp")
            tmp.write_bytes(forged)
            tmp.chmod(0o666)
            os.replace(tmp, path)
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    loaded = bl.load()
    assert loaded.entries == [] and "mode 0666" in loaded.warning
    report = run_audit(root, baseline=loaded)
    assert {fd["severity"] for fd in tx._secrets(report)} == {"high"}
    # The append path opens, locks and checks the same way.
    tx._write(
        root, "sessions/2026/10/28/rollout-y.jsonl", ts._unverified_rollout(_live_jwk_line(8))
    )
    assert _accept(root) == 1
    assert len(swaps) == 2
    assert path.read_bytes() == forged


class _Tty:
    def isatty(self):
        return True


def test_accept_previews_and_writes_only_the_previewed_snapshot(tmp_path, monkeypatch, capsys):
    """Review P2: accept shows what it would take (paths, detectors, never a
    value) and needs --yes or an interactive yes; it writes exactly the
    findings it showed, and --path narrows them."""
    value = ts._opaque(1)
    root = _codex(tmp_path, _jwk_text(value), _live_jwk_line(2))
    first, second = sorted(os.path.relpath(p, root) for p in root.rglob("rollout-*.jsonl"))
    capsys.readouterr()
    assert audit_main(["baseline", "accept", str(root), "--reason", "r"]) == 1
    out = capsys.readouterr()
    assert f"{first}:3  (JWK)" in out.out and second in out.out
    assert "re-run with --yes" in out.err
    assert value not in out.out + out.err
    assert not bl.default_path().exists()

    # Interactive: a value written while the prompt is open is not taken.
    session = root / first

    def answer(_prompt):
        session.write_text(session.read_text() + ts._unverified_rollout(_live_jwk_line(9)))
        return "y"

    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr(builtins, "input", answer)
    assert audit_main(["baseline", "accept", str(root), "--reason", "r", "--path", first]) == 0

    graded = {(fd["path"], fd["line"]): fd["severity"] for fd in tx._secrets(_with_baseline(root))}
    assert graded[(first, 3)] == "info"
    assert graded[(first, 6)] == "high"  # added after the preview
    assert graded[(second, 3)] == "high"  # outside --path
    assert {e["path"] for e in _entries()} == {first}
