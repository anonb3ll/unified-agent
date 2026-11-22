"""Why a head moved after its recorded review read (task #2398, report mode)."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from unified_agent import events, review_provenance, review_reads, shipping_preflight_cli, storage

REPO = "example/project"
HEAD = "a" * 40
READ = "c" * 40
READER = "reviewer-one"


def record_read(head_sha, **over):
    kwargs: dict[str, Any] = dict(
        repo=REPO,
        number=7,
        head_sha=head_sha,
        kind=review_reads.STEWARD_READ,
        reader=READER,
        verdict="pass",
    )
    kwargs.update(over)
    return review_reads.record(**kwargs)


def fake_gh(pairs, calls=None):
    """Fake ``gh api repos/{repo}/compare/{base}...{head}``; a missing pair fails."""

    def invoke(args, **kwargs):
        assert args[:2] == ["gh", "api"], args
        assert kwargs["timeout"] <= 30
        base, head = args[2].split("/compare/", 1)[1].split("...")
        if calls is not None:
            calls.append((base, head))
        entry = pairs.get((base, head))
        if entry is None:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(entry))

    return invoke


def never(_args, **_kwargs):  # pragma: no cover - asserts by being called
    pytest.fail("provenance called GitHub when it did not need to")


def patch_file(name, body, *, start=10, status="modified", previous=None):
    entry = {
        "filename": name,
        "status": status,
        "patch": f"@@ -{start},3 +{start},4 @@ def f\n{body}",
    }
    if previous:
        entry["previous_filename"] = previous
    return entry


A_PATCH = " context\n-old\n+new\n+added"
B_PATCH = " other\n+line"


def rebase(old_files, new_files):
    return {
        (READ, HEAD): {"status": "diverged", "files": [{"filename": "unrelated.py"}]},
        ("main", READ): {"status": "ahead", "files": old_files},
        ("main", HEAD): {"status": "ahead", "files": new_files},
    }


def prov(pairs, *, calls=None):
    return review_provenance.provenance(
        repo=REPO, number=7, head_sha=HEAD, run_gh=fake_gh(pairs, calls)
    )


def test_no_full_read_is_no_read_and_calls_nothing():
    out = review_provenance.provenance(repo=REPO, number=7, head_sha=HEAD, run_gh=never)
    assert out["head_change_cause"] == "no-read" and out["reviewed_sha"] is None
    assert out["next_gate_owner"] == {"role": "reviewer", "label": None}
    assert out["bridged"] is False


def test_a_delta_read_alone_is_not_a_full_read():
    record_read(READ)
    record_read(HEAD, kind=review_reads.DELTA_READ, from_sha=READ, files=["src/a.py"])
    out = review_provenance.provenance(repo=REPO, number=7, head_sha=READ, run_gh=never)
    # The newest *full* read is at READ, so nothing moved from it.
    assert (out["reviewed_sha"], out["head_change_cause"]) == (READ, "none")


def test_the_read_head_is_none_and_the_steward_owns_the_next_gate():
    record_read(HEAD)
    out = review_provenance.provenance(repo=REPO, number=7, head_sha=HEAD, run_gh=never)
    assert out["head_change_cause"] == "none" and out["current_head"] == HEAD
    assert out["next_gate_owner"] == {"role": "deploy-steward", "label": None}
    assert out["delta_files"] == [] and out["bridged"] is False


def test_commits_after_the_read_are_new_commits_with_renames_counted():
    record_read(READ)
    pairs = {
        (READ, HEAD): {
            "status": "ahead",
            "files": [
                {"filename": "src/a.py"},
                {"filename": "docs/new.md", "previous_filename": "deploy/old.md"},
            ],
        }
    }
    calls: list = []
    out = prov(pairs, calls=calls)
    assert out["head_change_cause"] == "new-commits"
    assert out["delta_files"] == ["deploy/old.md", "docs/new.md", "src/a.py"]
    assert out["next_gate_owner"] == {"role": "reviewer", "label": READER}
    assert calls == [(READ, HEAD)]


def test_a_rebase_that_only_shifted_hunks_is_rebase_identical():
    record_read(READ)
    pairs = rebase(
        [patch_file("src/a.py", A_PATCH, start=10), patch_file("src/b.py", B_PATCH)],
        [patch_file("src/a.py", A_PATCH, start=42), patch_file("src/b.py", B_PATCH, start=3)],
    )
    calls: list = []
    out = prov(pairs, calls=calls)
    assert out["head_change_cause"] == "rebase-identical" and out["bridged"] is True
    assert out["reviewed_sha"] == READ and out["delta_files"] == []
    assert out["next_gate_owner"] == {"role": "deploy-steward", "label": None}
    assert len(calls) == 3


def test_a_rebase_that_changed_the_patch_names_the_files():
    record_read(READ)
    pairs = rebase(
        [patch_file("src/a.py", A_PATCH), patch_file("src/b.py", B_PATCH)],
        [
            patch_file("src/a.py", A_PATCH + "\n+one more"),
            patch_file("src/b.py", B_PATCH),
            patch_file("deploy/c.sh", "+x"),
        ],
    )
    out = prov(pairs)
    assert out["head_change_cause"] == "rebase-changed" and out["bridged"] is False
    assert out["delta_files"] == ["deploy/c.sh", "src/a.py"]
    assert out["next_gate_owner"] == {"role": "reviewer", "label": READER}


def test_a_changed_context_line_is_a_change():
    record_read(READ)
    pairs = rebase(
        [patch_file("src/a.py", A_PATCH)],
        [patch_file("src/a.py", A_PATCH.replace(" context", " contexts"))],
    )
    assert prov(pairs)["head_change_cause"] == "rebase-changed"


def test_a_rename_on_one_side_names_both_paths():
    record_read(READ)
    pairs = rebase(
        [patch_file("src/a.py", A_PATCH)],
        [patch_file("src/z.py", A_PATCH, status="renamed", previous="src/a.py")],
    )
    out = prov(pairs)
    assert out["head_change_cause"] == "rebase-changed"
    assert out["delta_files"] == ["src/a.py", "src/z.py"]


def test_a_file_without_a_patch_is_unknown():
    record_read(READ)
    binary = {"filename": "img.png", "status": "modified"}
    out = prov(rebase([binary], [binary]))
    assert out["head_change_cause"] == "unknown" and out["transient"] is False
    assert "img.png" in out["reason"]
    assert out["next_gate_owner"] == {"role": "reviewer", "label": READER}


def test_a_comparison_at_the_file_cap_is_unknown():
    record_read(READ)
    files = [patch_file(f"src/f{i}.py", "+x") for i in range(300)]
    out = prov(rebase(files, files))
    assert out["head_change_cause"] == "unknown" and "truncated" in out["reason"]


def test_an_oversized_comparison_is_unknown():
    record_read(READ)

    def huge(_args, **_kwargs):
        return SimpleNamespace(
            returncode=0, stdout=" " * (review_provenance._COMPARE_JSON_LIMIT + 1)
        )

    out = review_provenance.provenance(repo=REPO, number=7, head_sha=HEAD, run_gh=huge)
    assert out["head_change_cause"] == "unknown" and out["transient"] is False


@pytest.mark.parametrize("missing", [(READ, HEAD), ("main", HEAD)])
def test_github_unavailable_is_unknown_and_transient(missing):
    record_read(READ)
    pairs = rebase([patch_file("src/a.py", A_PATCH)], [patch_file("src/a.py", A_PATCH)])
    del pairs[missing]
    out = prov(pairs)
    assert out["head_change_cause"] == "unknown" and out["transient"] is True
    assert out["bridged"] is False


def test_a_timeout_is_unknown():
    record_read(READ)

    def slow(args, **_kwargs):
        raise review_provenance.subprocess.TimeoutExpired(args, 30)

    out = review_provenance.provenance(repo=REPO, number=7, head_sha=HEAD, run_gh=slow)
    assert out["head_change_cause"] == "unknown"


def test_normalize_patch_drops_only_hunk_line_numbers():
    patch = "@@ -1,2 +3,4 @@ def f\n context\n-a\n+b\n\\ No newline at end of file"
    assert review_provenance.normalize_patch(patch) == (
        "@@ def f\n context\n-a\n+b\n\\ No newline at end of file"
    )


def _events():
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT payload FROM event_log WHERE channel=?", (review_provenance.CHANNEL,)
        ).fetchall()
    finally:
        conn.close()


def test_record_writes_once_per_head_pair():
    record_read(READ)
    pairs = rebase([patch_file("src/a.py", A_PATCH)], [patch_file("src/a.py", A_PATCH)])
    out = prov(pairs)
    assert review_provenance.record(out, REPO, 7) is True
    assert review_provenance.record(out, REPO, 7) is False
    rows = _events()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["head_change_cause"] == "rebase-identical" and payload["number"] == 7


def test_record_skips_a_transient_unknown_and_swallows_errors(monkeypatch):
    record_read(READ)
    assert review_provenance.record(prov({}), REPO, 7) is False
    assert _events() == []

    def broken(*_args, **_kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(events, "record_event_once", broken)
    out = review_provenance.provenance(repo=REPO, number=7, head_sha=READ, run_gh=never)
    assert review_provenance.record(out, REPO, 7) is False


# --- review_head_check stays report-only --------------------------------------


def check(run_gh):
    return shipping_preflight_cli.review_head_check(
        repo=REPO, number=7, head_sha=HEAD, run_gh=run_gh
    )


def test_the_gate_raises_a_distinguishable_preflight_error_on_a_rebase():
    record_read(READ)
    with pytest.raises(shipping_preflight_cli.RebasedOffReads) as caught:
        shipping_preflight_cli.review_head_gate(
            repo=REPO, number=7, head_sha=HEAD, run_gh=fake_gh(rebase([], []))
        )
    assert isinstance(caught.value, shipping_preflight_cli.PreflightError)


def test_the_check_still_fails_a_bridgeable_rebase_but_says_why():
    record_read(READ)
    pairs = rebase([patch_file("src/a.py", A_PATCH)], [patch_file("src/a.py", A_PATCH, start=90)])
    out = check(fake_gh(pairs))
    assert (out["status"], out["conclusion"], out["read_head"]) == ("head-moved", "failure", None)
    assert out["provenance"]["head_change_cause"] == "rebase-identical"
    assert out["summary"].startswith("Recorded reads exist")
    assert out["summary"].endswith(f"the patch read at {READ[:12]} is unchanged.")
    assert len(out["summary"]) <= shipping_preflight_cli._CHECK_SUMMARY_MAX
    assert len(_events()) == 1

    # A repeat check reuses the recorded answer: only the gate's own comparison.
    calls: list = []
    again = check(fake_gh(pairs, calls))
    assert calls == [(READ, HEAD)]
    assert again["summary"] == out["summary"] and again["provenance"] == out["provenance"]


def test_the_check_is_unchanged_when_provenance_fails(monkeypatch):
    record_read(READ)
    pairs = rebase([], [])
    with pytest.raises(shipping_preflight_cli.RebasedOffReads) as caught:
        shipping_preflight_cli.review_head_gate(
            repo=REPO, number=7, head_sha=HEAD, run_gh=fake_gh(pairs)
        )

    def broken(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(review_provenance, "observe", broken)
    out = check(fake_gh(pairs))
    assert out == {
        "status": "head-moved",
        "conclusion": "failure",
        "summary": str(caught.value)[: shipping_preflight_cli._CHECK_SUMMARY_MAX],
        "read_head": None,
    }


def test_other_refusals_carry_no_provenance():
    record_read(READ)
    red = {(READ, HEAD): {"status": "ahead", "files": [{"filename": "deploy/x.sh"}]}}
    out = check(fake_gh(red))
    assert out["conclusion"] == "failure" and "provenance" not in out
