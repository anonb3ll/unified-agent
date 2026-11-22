"""scripts/bisect_batch.py finds the pull request that broke a batch."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "bisect_batch", REPO_ROOT / "scripts" / "bisect_batch.py"
)
bisect_batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bisect_batch)


@pytest.mark.parametrize("bad", ["11", "15", "19"])
def test_bisect_finds_the_bad_pr(bad):
    prs = ["11", "13", "15", "17", "19"]

    def runner(prefix):
        return bad not in prefix

    assert bisect_batch.bisect(prs, runner) == bad


def test_bisect_reports_a_batch_that_passed():
    with pytest.raises(bisect_batch.BatchPassed):
        bisect_batch.bisect(["11", "13"], lambda prefix: True)


def test_cli_finds_the_bad_pr_in_a_fake_batch(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import os, sys\n"
        "prs = os.environ['BISECT_BATCH_PRS'].split(',')\n"
        "sys.exit(0 if '17' not in prs else 1)\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "bisect_batch.py"),
            "11",
            "13",
            "15",
            "17",
            "19",
            "--",
            sys.executable,
            str(runner),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "17"
