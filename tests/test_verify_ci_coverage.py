"""Read-only CI evidence: no live network, changes to refs, or approval writes."""

import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "verify_ci_coverage", Path(__file__).resolve().parents[1] / "scripts/verify_ci_coverage.py"
)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)

HEAD, BASE, BATCH, MERGED, TREE = (letter * 40 for letter in "abcde")
ROOT = "repos/o/r"
RUN = "https://example.test/builds/1"


class GitHub:
    def __init__(self, *, tested=BATCH, merged=None):
        self.tested, self.merged = tested, merged
        self.calls = []
        self.reads = 0
        self.pr = {
            "number": 1,
            "head": {"sha": HEAD},
            "base": {"ref": "main", "repo": {"full_name": "o/r"}},
            "state": "closed" if merged else "open",
            "merged": bool(merged),
        }
        self.branch = merged or BASE
        self.rows = [self.status(i, name) for i, name in enumerate(ci.REQUIRED, 1)]
        self.changed = None
        self.diverged = None
        self.tree = TREE

    def status(self, ident, name, state="success", **extra):
        return {
            "id": ident,
            "context": name,
            "state": state,
            "url": f"https://api.github.com/{ROOT}/statuses/{self.tested}",
            "target_url": RUN,
            **extra,
        }

    def __call__(self, path):
        self.calls.append(path)
        if path == f"{ROOT}/pulls/1":
            return copy.deepcopy(self.pr)
        if path.startswith(f"{ROOT}/git/ref/heads/"):
            return {"object": {"sha": self.branch}}
        if "/compare/" in path:
            older = path.split("/compare/")[1].split("...")[0]
            return {
                "status": "diverged" if older == self.diverged else "ahead",
                "base_commit": {"sha": older},
                "merge_base_commit": {"sha": older},
            }
        if "/statuses?" in path:
            page = int(path.split("&page=")[1])
            if page == 1:
                self.reads += 1
                if self.reads == 2 and self.changed:
                    self.changed(self)
            return copy.deepcopy(self.rows[(page - 1) * 100 : page * 100])
        if path.startswith(f"{ROOT}/commits/"):
            revision = path.rsplit("/", 1)[1]
            tree = self.tree if revision == self.merged else TREE
            return {"sha": revision, "commit": {"tree": {"sha": tree}}}
        raise AssertionError(path)


def verify(api, **kwargs):
    return ci.verify(api, "o/r", 1, HEAD, BASE, api.tested, api.merged, **kwargs)


def test_exact_head_and_batch_report_only_observed_coverage():
    for tested in (HEAD, BATCH):
        api = GitHub(tested=tested)
        report = verify(api)
        assert report["mode"] == ("exact-head" if tested == HEAD else "batch")
        assert report["head_sha"] == HEAD
        assert report["not_observed"] == list(ci.OPTIONAL)
        assert len(report["successful_status_ids"]) == len(ci.REQUIRED)
        assert report["verified_tree"] is None
        assert report["limits"]


@pytest.mark.parametrize("older", [HEAD, BASE])
def test_batch_must_contain_both_head_and_integration_base(older):
    api = GitHub()
    api.diverged = older
    with pytest.raises(ci.Refused, match="does not contain"):
        verify(api)


@pytest.mark.parametrize("change", ["head", "branch", "base_repo", "state"])
def test_stale_pins_and_closed_unmerged_prs_are_refused(change):
    api = GitHub()
    if change == "head":
        api.pr["head"]["sha"] = MERGED
    elif change == "branch":
        api.branch = MERGED
    elif change == "base_repo":
        api.pr["base"]["repo"]["full_name"] = "another/repo"
    else:
        api.pr["state"] = "closed"
    with pytest.raises(ci.Refused):
        verify(api)


@pytest.mark.parametrize("state", ["failure", "error", "pending"])
def test_a_newer_non_success_cannot_be_hidden_by_old_green(state):
    api = GitHub()
    api.rows.insert(0, api.status(90, ci.REQUIRED[0], state))
    with pytest.raises(ci.Refused, match="did not succeed"):
        verify(api)


def test_missing_required_status_and_check_run_only_ci_are_not_passes():
    api = GitHub()
    api.rows.pop()
    with pytest.raises(ci.Refused, match="required status absent"):
        verify(api)
    api.rows = []
    with pytest.raises(ci.Refused, match="required status absent"):
        verify(api)


def test_newer_status_ids_win_even_when_api_order_is_reversed():
    api = GitHub()
    api.rows.append(api.status(90, ci.REQUIRED[0], "failure"))
    api.rows.append(api.status(100, ci.REQUIRED[0]))
    assert verify(api)["successful_status_ids"][ci.REQUIRED[0]] == 100


def test_all_required_jobs_must_reference_the_same_ci_run():
    api = GitHub()
    api.rows[0]["target_url"] = "https://example.test/builds/older"
    with pytest.raises(ci.Refused, match="different runs"):
        verify(api)


def test_optional_coverage_is_explicit_and_its_failures_are_not_ignored():
    api = GitHub()
    with pytest.raises(ci.Refused, match="required status absent"):
        verify(api, require=("CI / js",))
    api.rows.append(api.status(90, "CI / js"))
    assert verify(api, require=("CI / js",))["not_observed"] == ["CI / desktop-core"]
    api.rows[-1]["state"] = "failure"
    with pytest.raises(ci.Refused, match="CI / js=failure"):
        verify(api)


def test_final_merged_tree_is_checked_separately_from_ancestry():
    api = GitHub(merged=MERGED)
    assert verify(api)["verified_tree"] == TREE
    api.tree = HEAD
    with pytest.raises(ci.Refused, match="merged tree differs"):
        verify(api)


def test_read_all_status_pages_including_a_late_failure():
    api = GitHub()
    api.rows.extend(api.status(i, f"unrelated-{i}") for i in range(10, 105))
    api.rows.append(api.status(200, ci.REQUIRED[0], "failure"))
    with pytest.raises(ci.Refused, match="did not succeed"):
        verify(api)
    assert any(path.endswith("page=2") for path in api.calls)


@pytest.mark.parametrize(
    "patch",
    [
        {"id": None},
        {"id": True},
        {"state": "skipped"},
        {"state": {}},
        {"context": None},
        {"url": "https://api.github.com/repos/o/r/statuses/other"},
        {"target_url": []},
        {"target_url": "http://example.test/run"},
        {"target_url": "https://user@example.test/run"},
    ],
)
def test_malformed_incomplete_or_unsafe_status_evidence_fails_closed(patch):
    api = GitHub()
    for row in api.rows:
        row.update(patch)
    with pytest.raises(ci.Unavailable):
        verify(api)


@pytest.mark.parametrize(
    "target_url",
    [
        "https://example.test:not-a-port/run",
        "https://example.test:65536/run",
    ],
)
def test_non_numeric_or_out_of_range_ci_run_port_is_unavailable(target_url):
    api = GitHub()
    for row in api.rows:
        row["target_url"] = target_url
    with pytest.raises(ci.Unavailable, match="not a valid HTTPS"):
        verify(api)


def test_numeric_ci_run_port_stays_valid_evidence():
    api = GitHub()
    for row in api.rows:
        row["target_url"] = "https://example.test:443/builds/1"
    assert verify(api)["coverage"] == "observed"


def test_duplicate_pages_and_excessive_history_are_unavailable(monkeypatch):
    api = GitHub()
    api.rows.append(copy.deepcopy(api.rows[0]))
    with pytest.raises(ci.Unavailable, match="duplicate"):
        verify(api)
    api = GitHub()
    api.rows = [api.status(i, f"unrelated-{i}") for i in range(1, 101)]
    monkeypatch.setattr(ci, "MAX_PAGES", 1)
    with pytest.raises(ci.Unavailable, match="page budget"):
        verify(api)


@pytest.mark.parametrize("change", ["head", "branch", "status"])
def test_changes_during_the_observation_are_refused(change):
    api = GitHub()
    if change == "status":
        api.changed = lambda fake: fake.rows.append(fake.status(90, ci.REQUIRED[0]))
        get = api
    else:

        def get(path):
            if path == f"{ROOT}/pulls/1" and api.reads:
                if change == "head":
                    api.pr["head"]["sha"] = MERGED
                else:
                    api.branch = MERGED
            return api(path)

    with pytest.raises(ci.Refused, match="changed|moved"):
        ci.verify(get, "o/r", 1, HEAD, BASE, BATCH)


@pytest.mark.parametrize("head", ["abc123", "a" * 39, "a" * 40 + "; echo unsafe"])
def test_full_sha_pins_are_required_before_any_api_call(head):
    api = GitHub()
    with pytest.raises(ci.Unavailable, match="full commit"):
        ci.verify(api, "o/r", 1, head, BASE, BATCH)
    assert not api.calls


def test_gh_transport_is_get_only_and_does_not_repeat_sensitive_diagnostics(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, "private-output", "private-diagnostic")

    monkeypatch.setattr(ci.subprocess, "run", run)
    with pytest.raises(ci.Unavailable) as error:
        ci.gh_get(f"{ROOT}/pulls/1")
    assert "private-" not in str(error.value)
    assert calls[0][0][:7] == [
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        f"{ROOT}/pulls/1",
    ]
    assert calls[0][1]["timeout"] == 30


def test_cli_reports_coverage_and_distinguishes_refusal_from_unavailability(monkeypatch, capsys):
    args = ["--repo", "o/r", "--pr", "1", "--head", HEAD, "--base", BASE, "--tested", BATCH]
    api = GitHub()
    monkeypatch.setattr(ci, "gh_get", api)
    assert ci.main(args) == 0
    assert json.loads(capsys.readouterr().out)["coverage"] == "observed"
    api.rows = []
    assert ci.main(args) == 1
    assert json.loads(capsys.readouterr().out)["coverage"] == "refused"
    monkeypatch.setattr(ci, "gh_get", lambda path: None)
    assert ci.main(args) == 2
    assert json.loads(capsys.readouterr().out)["coverage"] == "refused"
