"""scripts/check_batch_held_prs.py: the held-PR rule on a merge batch (task #1787).

No network: the GitHub API seam (`_api`) is replaced with fixtures.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "check_batch_held_prs", REPO_ROOT / "scripts" / "check_batch_held_prs.py"
)
batch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = batch
spec.loader.exec_module(batch)

BATCH = "b" * 40


def _fake_api(prs, compare, files, commits):
    def api(path, token):
        if path.startswith("repos/o/r/pulls?state=open"):
            return prs if "page=1" in path else []
        if path.startswith("repos/o/r/compare/"):
            head = path.split("/compare/")[1].split("...")[0]
            return {"status": compare[head]}
        if "/files" in path:
            number = int(path.split("/pulls/")[1].split("/")[0])
            rows = [
                {"filename": f[0], "status": f[1]} if isinstance(f, tuple) else {"filename": f}
                for f in files[number]
            ]
            return rows if "page=1" in path else []
        if "/commits" in path:
            number = int(path.split("/pulls/")[1].split("/")[0])
            return [{"commit": {"message": m}} for m in commits[number]] if "page=1" in path else []
        raise AssertionError(path)

    return api


def _pr(number, head, labels=("held",), body=""):
    return {
        "number": number,
        "head": {"sha": head},
        "labels": [{"name": n} for n in labels],
        "body": body,
    }


def test_only_held_prs_contained_in_the_batch_are_checked(monkeypatch, capsys):
    prs = [
        _pr(1, "1" * 40),  # held, in batch, adds a test
        _pr(2, "2" * 40),  # held, NOT in batch (diverged)
        _pr(3, "3" * 40, labels=()),  # in batch, not held
    ]
    monkeypatch.setattr(
        batch,
        "_api",
        _fake_api(
            prs,
            {"1" * 40: "ahead", "2" * 40: "diverged", "3" * 40: "identical"},
            {1: ["tests/test_fix.py", "src/x.py"]},
            {1: ["fix: x"]},
        ),
    )
    monkeypatch.setenv("GH_TOKEN", "t")
    assert batch.main(["--repo", "o/r", "--sha", BATCH]) == 0
    out = capsys.readouterr().out
    assert "PASS PR #1" in out and "#2" not in out and "#3" not in out


def test_a_held_member_without_a_regression_test_fails_the_batch(monkeypatch, capsys):
    prs = [_pr(4, "4" * 40)]
    monkeypatch.setattr(
        batch,
        "_api",
        _fake_api(prs, {"4" * 40: "identical"}, {4: ["src/x.py"]}, {4: ["fix: x"]}),
    )
    monkeypatch.setenv("GH_TOKEN", "t")
    assert batch.main(["--repo", "o/r", "--sha", BATCH]) == 1
    assert "FAIL PR #4" in capsys.readouterr().out


def test_api_errors_are_errors_not_passes(monkeypatch):
    def boom(path, token):
        raise OSError("network down")

    monkeypatch.setattr(batch, "_api", boom)
    monkeypatch.setenv("GH_TOKEN", "t")
    assert batch.main(["--repo", "o/r", "--sha", BATCH]) == 2


def test_no_token_is_an_error(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(batch, "_token", lambda: "")
    assert batch.main(["--repo", "o/r", "--sha", BATCH]) == 2


def test_a_deleted_test_is_not_coverage(monkeypatch, capsys):
    """Devin on PR #969: the files API lists removed files; the event gate skips them."""
    monkeypatch.setattr(
        batch,
        "_api",
        _fake_api(
            [_pr(7, "7" * 40)],
            {"7" * 40: "ahead"},
            {7: [("tests/test_bug.py", "removed"), ("src/x.py", "modified")]},
            {7: ["fix: the bug"]},
        ),
    )
    monkeypatch.setenv("GH_TOKEN", "t")
    assert batch.main(["--repo", "o/r", "--sha", BATCH]) == 1
    assert "FAIL PR #7" in capsys.readouterr().out
