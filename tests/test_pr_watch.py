"""Tests for pr_watch event-diff logic (UA task #702, Buzz-emulation #1).

The diff is the tested core: new PR, new review, merged, closed-without-merge,
and unchanged = no-op. Fetch/write paths are exercised via a stub run_gh and
the hermetic isolated_env fixture; no network, no real memory.db.
"""

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from export_guard import needs_exported_paths

from unified_agent import pr_watch
from unified_agent.tools.memory import memory_search

_REPO_ROOT = Path(__file__).resolve().parents[1]
# deploy/mac-hub/ is not in the public export (task #2615), so the launcher
# body skips there and still runs in the private repo.
needs_mac_hub_assets = needs_exported_paths(
    _REPO_ROOT / "deploy" / "mac-hub" / "run-pr-watch.sh", repo=_REPO_ROOT
)


def make_pr(number: int, **overrides) -> dict:
    """A `gh pr list` payload row with a clean no-review default."""
    base = {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "url": f"https://github.com/anonb3ll/x/pull/{number}",
        "state": "OPEN",
        "headRefOid": f"sha{number}",
        "mergedAt": None,
        "closedAt": None,
        "reviews": [],
    }
    base.update(overrides)
    return base


def reviewed(
    number: int, state: str, body: str, submitted_at: str, author: str = "reviewer"
) -> list[dict]:
    """A review row in the REAL `gh` wire shape (camelCase submittedAt)."""
    return [
        {"author": {"login": author}, "state": state, "body": body, "submittedAt": submitted_at}
    ]


class FakeGh:
    """Stub for subprocess.run on `gh ...` calls, keyed by repo name."""

    def __init__(self, repos: dict[str, list[dict]]):
        self.repos = repos
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[1] == "repo":
            payload = [{"nameWithOwner": name, "isFork": False} for name in self.repos]
        else:
            payload = self.repos[cmd[cmd.index("-R") + 1]]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")


@pytest.fixture
def watcher_env(tmp_path, monkeypatch):
    """Hermetic cursor dir + real memory/vault stores via isolated_env."""
    from unified_agent import config

    home = tmp_path / "ua_home"
    home.mkdir(exist_ok=True)
    (tmp_path / "vault").mkdir(exist_ok=True)  # isolated_env sets UA_VAULT_PATH here
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setattr(config, "data_home", lambda: home)
    return home


class SlowGh(FakeGh):
    """``FakeGh`` whose calls matching ``slow`` time out ``times`` times (task #2431)."""

    def __init__(self, repos, slow: str, times: int):
        super().__init__(repos)
        self.slow, self.left = slow, times

    def __call__(self, cmd, **kwargs):
        target = "repo" if cmd[1] == "repo" else cmd[cmd.index("-R") + 1]
        if target == self.slow and self.left > 0:
            self.calls.append(cmd)
            self.left -= 1
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return super().__call__(cmd, **kwargs)


# ---------------------------------------------------------------- diff logic


def test_new_pr_is_opened_event():
    cur = pr_watch.parse_prs([make_pr(1)])
    events = pr_watch.diff_events("anonb3ll/x", {}, cur)
    assert [(e.kind, e.snapshot.number) for e in events] == [("opened", 1)]


def test_new_review_is_reviewed_event_with_rationale():
    prev = pr_watch.parse_prs([make_pr(1)])
    cur = pr_watch.parse_prs(
        [make_pr(1, reviews=reviewed(1, "COMMENTED", "rename this", "2026-08-31T10:00:00Z"))]
    )
    events = pr_watch.diff_events("anonb3ll/x", prev, cur)
    assert [(e.kind, e.snapshot.number) for e in events] == [("reviewed", 1)]
    assert events[0].snapshot.last_review_body == "rename this"


def test_merged_is_merged_event_with_pr_body_rationale():
    prev = pr_watch.parse_prs([make_pr(1, body="Adds the thing")])
    cur = pr_watch.parse_prs(
        [make_pr(1, body="Adds the thing", state="MERGED", mergedAt="2026-08-31T12:00:00Z")]
    )
    events = pr_watch.diff_events("anonb3ll/x", prev, cur)
    assert [(e.kind, e.snapshot.number) for e in events] == [("merged", 1)]
    title, content = pr_watch.note_for_event(events[0])
    assert "Adds the thing" in content
    assert "anonb3ll/x" in title


def test_closed_without_merge_is_closed_event():
    prev = pr_watch.parse_prs([make_pr(1)])
    cur = pr_watch.parse_prs([make_pr(1, state="CLOSED", closedAt="2026-08-31T12:00:00Z")])
    events = pr_watch.diff_events("anonb3ll/x", prev, cur)
    assert [(e.kind, e.snapshot.number) for e in events] == [("closed", 1)]


def test_unchanged_snapshot_is_noop():
    cur = pr_watch.parse_prs([make_pr(1)])
    assert pr_watch.diff_events("anonb3ll/x", cur, cur) == []
    # Same state rebuilt from a separate fetch round-trip also produces nothing.
    again = pr_watch.parse_prs([make_pr(1)])
    assert pr_watch.diff_events("anonb3ll/x", cur, again) == []


def test_cursor_fields_carry_full_lifecycle_in_order():
    """A PR opened, reviewed, and merged between two runs reports every step."""
    prev = pr_watch.parse_prs([make_pr(1, body="feature work")])
    cur = pr_watch.parse_prs(
        [
            make_pr(
                1,
                body="feature work",
                state="MERGED",
                mergedAt="2026-08-31T12:00:00Z",
                reviews=reviewed(1, "APPROVED", "ship it", "2026-08-31T11:00:00Z"),
            )
        ]
    )
    events = pr_watch.diff_events("anonb3ll/x", prev, cur)
    assert [e.kind for e in events] == ["reviewed", "merged"]


def test_title_edit_alone_is_not_an_event():
    prev = pr_watch.parse_prs([make_pr(1, title="wip")])
    cur = pr_watch.parse_prs([make_pr(1, title="feat: the thing")])
    assert pr_watch.diff_events("anonb3ll/x", prev, cur) == []


def test_vanished_pr_produces_no_event():
    cur = pr_watch.parse_prs([make_pr(1)])
    assert pr_watch.diff_events("anonb3ll/x", cur, {}) == []


# ------------------------------------------------------------- review reduce


def test_reviews_snapshot_takes_latest_submitted():
    reviews = reviewed(1, "COMMENTED", "first", "2026-08-31T10:00:00Z") + reviewed(
        1, "APPROVED", "ok now", "2026-08-31T11:00:00Z"
    )
    at, state, body, author = pr_watch.reviews_snapshot(reviews)
    assert (at, state, body, author) == ("2026-08-31T11:00:00Z", "APPROVED", "ok now", "reviewer")


def test_reviews_snapshot_empty_for_no_reviews():
    assert pr_watch.reviews_snapshot([]) == (None, None, "", "")


def test_reviews_snapshot_accepts_live_camelcase_and_snake_fallback():
    """Regression (UA #702 review r1): live `gh` sends submittedAt; snake_case
    fixtures must not silently shadow it — both shapes reduce to a review."""
    live = [
        {
            "author": {"login": "r"},
            "state": "APPROVED",
            "body": "ok",
            "submittedAt": "2026-08-31T11:00:00Z",
        }
    ]
    assert pr_watch.reviews_snapshot(live) == ("2026-08-31T11:00:00Z", "APPROVED", "ok", "r")
    legacy = [
        {
            "author": {"login": "r"},
            "state": "APPROVED",
            "body": "ok",
            "submitted_at": "2026-08-31T11:00:00Z",
        }
    ]
    assert pr_watch.reviews_snapshot(legacy) == ("2026-08-31T11:00:00Z", "APPROVED", "ok", "r")
    # Review with no timestamp is skipped, not fatal.
    assert pr_watch.reviews_snapshot([{"state": "COMMENTED"}]) == (None, None, "", "")


# ------------------------------------------------------------ end-to-end run


def seed_cursor(home, repos: dict[str, list[dict]]) -> None:
    """Pre-seed the cursor as if a previous run had already seen these PRs."""
    cursor = {repo: pr_watch.parse_prs(prs) for repo, prs in repos.items()}
    pr_watch.save_cursor(home / pr_watch.CURSOR_FILE, cursor)


def test_first_run_seeds_baseline_without_events(watcher_env, capsys):
    prs = [
        make_pr(1),
        make_pr(2, state="MERGED", mergedAt="2026-01-01T00:00:00Z"),
    ]
    gh = FakeGh({"anonb3ll/x": prs})
    events = pr_watch.run(dry_run=False, repos=["anonb3ll/x"], author="Desktop_Agent5", run_gh=gh)
    assert events == []
    assert "first run" in capsys.readouterr().out
    # Baseline recorded; the next identical run is a no-op.
    cursor = pr_watch.load_cursor(watcher_env / pr_watch.CURSOR_FILE)
    assert set(cursor["anonb3ll/x"]) == {1, 2}
    assert (
        pr_watch.run(dry_run=False, repos=["anonb3ll/x"], author="Desktop_Agent5", run_gh=gh) == []
    )


def test_run_dry_run_writes_nothing_and_keeps_cursor(watcher_env, capsys):
    # Cursor already knows PR 9 (unchanged); PR 1 is newly opened.
    prs = {"anonb3ll/x": [make_pr(1), make_pr(9)]}
    seed_cursor(watcher_env, {"anonb3ll/x": [make_pr(9)]})
    gh = FakeGh(prs)
    events = pr_watch.run(dry_run=True, repos=["anonb3ll/x"], run_gh=gh)
    assert [e.kind for e in events] == ["opened"]
    out = capsys.readouterr().out
    assert "[dry-run] would memory_save" in out
    # Cursor NOT advanced: a re-run must see the same events.
    seeded = pr_watch.load_cursor(watcher_env / pr_watch.CURSOR_FILE)
    assert set(seeded) == {"anonb3ll/x"} and set(seeded["anonb3ll/x"]) == {9}
    assert memory_search("PR #1", tags=["project:anonb3ll/x"])["results"] == []  # store untouched
    events2 = pr_watch.run(dry_run=True, repos=["anonb3ll/x"], run_gh=gh)
    assert [e.kind for e in events2] == ["opened"]


def test_run_live_writes_memory_and_journal_then_idempotent(watcher_env, capsys):
    prs = {"anonb3ll/x": [make_pr(1), make_pr(9)]}
    seed_cursor(watcher_env, {"anonb3ll/x": [make_pr(9)]})
    gh = FakeGh(prs)
    events = pr_watch.run(dry_run=False, repos=["anonb3ll/x"], author="Desktop_Agent5", run_gh=gh)
    assert [e.kind for e in events] == ["opened"]

    from unified_agent.tools.vault import vault_journal  # noqa: F401  (import path smoke)

    res = memory_search("PR #1 opened", tags=["project:anonb3ll/x"])
    assert res["results"], "memory note with project: tag must be searchable"
    note = res["results"][0]
    assert "Desktop_Agent5" in json.dumps(note) or note["id"]

    # Idempotent re-run: identical state, no new events, no new notes.
    events2 = pr_watch.run(dry_run=False, repos=["anonb3ll/x"], author="Desktop_Agent5", run_gh=gh)
    assert events2 == []
    res2 = memory_search("PR #1 opened", tags=["project:anonb3ll/x"])
    assert len(res2["results"]) == len(res["results"])

    cursor = pr_watch.load_cursor(watcher_env / pr_watch.CURSOR_FILE)
    assert cursor["anonb3ll/x"][1].state == "OPEN"


def test_run_routes_event_lines_to_pr_watch_note_and_one_daily_pointer(watcher_env, tmp_path):
    """Task #1920: per-event lines go to Logs/pr-watch/<day>.md; the daily journal
    gets one pointer line per day, however many runs and events follow."""
    day = datetime.now().strftime("%Y-%m-%d")
    vault = tmp_path / "vault"
    prs = {"example/x": [make_pr(1), make_pr(2), make_pr(9)]}
    seed_cursor(watcher_env, {"example/x": [make_pr(9)]})
    gh = FakeGh(prs)
    events = pr_watch.run(dry_run=False, repos=["example/x"], author="Desktop_Agent5", run_gh=gh)
    assert sorted(e.snapshot.number for e in events) == [1, 2]

    stream = (vault / "Logs" / "pr-watch" / f"{day}.md").read_text()
    assert stream.startswith(f"---\ndate: {day}\ntype: journal\nstream: pr-watch\n---\n")
    event_lines = [ln for ln in stream.splitlines() if ln.startswith("- **")]
    assert len(event_lines) == 2
    assert all("[Desktop_Agent5] PR #" in ln and "#pr-watch" in ln for ln in event_lines)
    assert any("PR #1 opened in example/x" in ln for ln in event_lines)

    daily = (vault / "Logs" / f"{day}.md").read_text()
    pointer_lines = [ln for ln in daily.splitlines() if "pr-watch" in ln]
    assert len(pointer_lines) == 1
    assert f"[[Logs/pr-watch/{day}]]" in pointer_lines[0]
    assert "PR #1" not in daily and "PR #2" not in daily

    # A later run with a new event appends to the pr-watch note only.
    gh.repos["example/x"] = [*prs["example/x"], make_pr(3)]
    pr_watch.run(dry_run=False, repos=["example/x"], author="Desktop_Agent5", run_gh=gh)
    stream = (vault / "Logs" / "pr-watch" / f"{day}.md").read_text()
    assert sum(ln.startswith("- **") for ln in stream.splitlines()) == 3
    daily = (vault / "Logs" / f"{day}.md").read_text()
    assert sum("pr-watch" in ln for ln in daily.splitlines()) == 1


def test_run_restores_missing_daily_pointer(watcher_env, tmp_path):
    """The pointer check reads the daily note, so a run that died after the
    stream write but before the pointer gets its pointer on the next run."""
    day = datetime.now().strftime("%Y-%m-%d")
    vault = tmp_path / "vault"
    stream = vault / "Logs" / "pr-watch" / f"{day}.md"
    stream.parent.mkdir(parents=True)
    stream.write_text("---\ndate: x\n---\n- earlier line\n")
    seed_cursor(watcher_env, {"example/x": [make_pr(9)]})
    gh = FakeGh({"example/x": [make_pr(1), make_pr(9)]})
    pr_watch.run(dry_run=False, repos=["example/x"], author="Desktop_Agent5", run_gh=gh)
    daily = (vault / "Logs" / f"{day}.md").read_text()
    assert f"[[Logs/pr-watch/{day}]]" in daily


def test_journal_pointer_fits_journal_cap():
    from unified_agent.tools.vault import _JOURNAL_ENTRY_MAX_CHARS

    needle, entry = pr_watch.journal_pointer("2026-10-07")
    assert needle == "[[Logs/pr-watch/2026-10-07]]" and needle in entry
    assert len(entry) <= _JOURNAL_ENTRY_MAX_CHARS


def test_run_picks_up_review_between_runs(watcher_env):
    prs = {"anonb3ll/x": [make_pr(1)]}
    seed_cursor(watcher_env, prs)
    gh = FakeGh(prs)
    pr_watch.run(dry_run=False, repos=["anonb3ll/x"], author="Desktop_Agent5", run_gh=gh)
    gh.repos["anonb3ll/x"] = [
        make_pr(1, reviews=reviewed(1, "CHANGES_REQUESTED", "needs a test", "2026-08-31T10:00:00Z"))
    ]
    events = pr_watch.run(dry_run=False, repos=["anonb3ll/x"], author="Desktop_Agent5", run_gh=gh)
    assert [(e.kind, e.snapshot.last_review_state) for e in events] == [
        ("reviewed", "CHANGES_REQUESTED")
    ]


def test_five_events_on_one_pr_yield_one_rolling_note(watcher_env):
    """Task #1335: five events on one PR → one rolling memory note."""
    from unified_agent.tools.memory import memory_get, memory_list

    snap = pr_watch.PRSnapshot(
        number=7,
        title="feat",
        url="https://github.com/anonb3ll/x/pull/7",
        state="OPEN",
        head_sha="a",
    )
    note_ids = []
    for kind in ("opened", "synchronize", "ready_for_review", "reviewed", "merged"):
        note_ids.append(
            pr_watch.upsert_pr_note(pr_watch.PREvent("anonb3ll/x", kind, snap), "Desktop_Agent5")
        )
    assert len(set(note_ids)) == 1
    items = memory_list(tag="pr:anonb3ll/x#7", limit=10)["items"]
    assert len(items) == 1
    body = memory_get(items[0]["id"])["content"]
    for kind in ("opened", "synchronize", "ready_for_review", "reviewed", "merged"):
        assert f"PR #7 {kind}" in body


def test_legacy_pr_note_without_stable_tag_is_reused(watcher_env):
    """Pre-#1335 notes lacked pr:<repo>#<n>; next event must append, not fork."""
    from unified_agent.tools.memory import memory_get, memory_list, memory_save

    legacy = memory_save(
        content="PR #9 opened in anonb3ll/x: old",
        title="PR #9 opened — anonb3ll/x",
        tags=["project:anonb3ll/x", "pr-watch"],
        author="Desktop_Agent5",
    )
    snap = pr_watch.PRSnapshot(
        number=9,
        title="old",
        url="https://github.com/anonb3ll/x/pull/9",
        state="OPEN",
        head_sha="b",
    )
    note_id = pr_watch.upsert_pr_note(
        pr_watch.PREvent("anonb3ll/x", "reviewed", snap), "Desktop_Agent5"
    )
    assert note_id == legacy["id"]
    assert len(memory_list(tag="pr:anonb3ll/x#9", limit=10)["items"]) == 1
    assert "reviewed" in memory_get(note_id)["content"]


def test_legacy_pr_note_number_prefix_not_confused(watcher_env):
    """PR #90 must not be treated as the rolling note for PR #9."""
    from unified_agent.tools.memory import memory_get, memory_list, memory_save

    legacy90 = memory_save(
        content="PR #90 opened in anonb3ll/x: big",
        title="PR #90 opened — anonb3ll/x",
        tags=["project:anonb3ll/x", "pr-watch"],
        author="Desktop_Agent5",
    )
    snap9 = pr_watch.PRSnapshot(
        number=9,
        title="small",
        url="https://github.com/anonb3ll/x/pull/9",
        state="OPEN",
        head_sha="d",
    )
    note_id = pr_watch.upsert_pr_note(
        pr_watch.PREvent("anonb3ll/x", "opened", snap9), "Desktop_Agent5"
    )
    assert note_id != legacy90["id"]
    assert len(memory_list(tag="pr:anonb3ll/x#9", limit=10)["items"]) == 1
    assert "pr:anonb3ll/x#9" not in memory_get(legacy90["id"])["tags"]


def test_title_matches_pr_rejects_longer_number():
    assert pr_watch._title_matches_pr("PR #9 opened — anonb3ll/x", 9)
    assert pr_watch._title_matches_pr("PR #9 — anonb3ll/x", 9)
    assert not pr_watch._title_matches_pr("PR #90 opened — anonb3ll/x", 9)
    assert pr_watch._title_matches_pr("PR #90 — anonb3ll/x", 90)


def test_upsert_preserves_peer_added_tags(watcher_env):
    from unified_agent.tools.memory import memory_get, memory_save

    snap = pr_watch.PRSnapshot(
        number=3,
        title="t",
        url="https://github.com/anonb3ll/x/pull/3",
        state="OPEN",
        head_sha="c",
    )
    note_id = pr_watch.upsert_pr_note(
        pr_watch.PREvent("anonb3ll/x", "opened", snap), "Desktop_Agent5"
    )
    memory_save(
        content="peer annotation",
        id=note_id,
        author="Desktop_Agent1",
        append=True,
        tags=["project:anonb3ll/x", "pr-watch", "pr:anonb3ll/x#3", "decision"],
    )
    pr_watch.upsert_pr_note(pr_watch.PREvent("anonb3ll/x", "merged", snap), "Desktop_Agent5")
    tags = memory_get(note_id)["tags"]
    assert "decision" in tags
    assert "pr:anonb3ll/x#3" in tags


def test_journal_line_within_cap(watcher_env):
    prs = {"anonb3ll/x": [make_pr(2, title="x" * 200)]}
    seed_cursor(watcher_env, prs)
    gh = FakeGh(prs)
    pr_watch.run(
        dry_run=False, repos=["anonb3ll/x"], author="Desktop_Agent5", run_gh=gh
    )  # must not raise ToolError


# ----------------------------------------------------- fresh UA_HOME regression


def test_main_broker_mode_skips_legacy_gh_journal(watcher_env, monkeypatch, capsys):
    import sys

    from unified_agent import github_app_worker

    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_SOURCE", "connector_oauth")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setattr(github_app_worker, "run_once", lambda: [])
    monkeypatch.setattr(pr_watch, "run", lambda **_kwargs: pytest.fail("legacy gh journal ran"))
    monkeypatch.setattr(sys, "argv", ["pr-watch"])
    # A skip left from before the journal was switched off (task #2486).
    pr_watch.record_run_status(ok=True, skipped=["example/x: gh pr list timed out 2 times"])
    pr_watch.main()
    assert "journal skipped in broker-only mode" in capsys.readouterr().out
    # Nothing will ever retry it, so it must not degrade the job for good.
    record = pr_watch.load_run_status()
    assert "journal" not in record and record["skipped_total"] == 0
    assert pr_watch.run_health()["status"] == "LIVE"


def test_main_disabled_broker_keeps_legacy_journal(watcher_env, monkeypatch):
    import sys

    from unified_agent import github_app_worker

    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_SOURCE", "connector_oauth")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    monkeypatch.setattr(github_app_worker, "run_once", lambda: [])
    called = []
    monkeypatch.setattr(pr_watch, "run", lambda **kwargs: called.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["pr-watch"])
    pr_watch.main()
    assert len(called) == 1


def test_main_broker_rejects_explicit_legacy_repos(watcher_env, monkeypatch, capsys):
    import sys

    from unified_agent import github_app_worker

    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_SOURCE", "connector_oauth")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setattr(
        github_app_worker, "run_once", lambda: pytest.fail("connector poll ran before rejection")
    )
    monkeypatch.setattr(pr_watch, "run", lambda **_kwargs: pytest.fail("legacy gh journal ran"))
    monkeypatch.setattr(sys, "argv", ["pr-watch", "--repos", "anonb3ll/unified-agent"])
    with pytest.raises(SystemExit) as exc:
        pr_watch.main()
    assert exc.value.code == 1
    assert "explicit repository polling is unavailable" in capsys.readouterr().err


def test_run_live_fresh_ua_home_initializes_schema(watcher_env, monkeypatch, capsys):
    """Regression (UA #702 r2): a bare CLI live run against a fresh UA_HOME
    (no memory.db) crashed with "no such table: memory" because main() never
    called storage.init_db(). The autouse isolated_env fixture inits the DB
    for every test, so this test deletes it first to reproduce the bare
    production condition, then exercises the real CLI main() end to end."""
    import sys

    db = watcher_env / "memory.db"
    if db.exists():
        db.unlink()  # conftest's init_db created it; production-fresh UA_HOME has none

    seed_cursor(watcher_env, {"anonb3ll/x": [make_pr(9)]})
    gh = FakeGh({"anonb3ll/x": [make_pr(1), make_pr(9)]})

    # main() has no run_gh injection point, so wrap run() to supply the stub;
    # main's own storage.init_db() call (the fix under test) still executes.
    real_run = pr_watch.run

    def run_with_stub_gh(*args, **kwargs):
        kwargs["run_gh"] = gh
        return real_run(*args, **kwargs)

    monkeypatch.setattr(pr_watch, "run", run_with_stub_gh)

    # Drive the real main() with argv patched — the crash was in main(), not run().
    argv = ["pr-watch", "--repos", "anonb3ll/x", "--author", "Desktop_Agent5"]
    old_argv = sys.argv
    sys.argv = argv
    try:
        pr_watch.main()
    finally:
        sys.argv = old_argv
    out = capsys.readouterr().out
    assert "-> memory" in out, "live run must land the note"

    res = memory_search("PR #1 opened", tags=["project:anonb3ll/x"])
    assert res["results"], "note must be searchable after fresh-UA_HOME live run"

    # Schema now initialized in place: a second run is a clean no-op.
    events2 = pr_watch.run(dry_run=False, repos=["anonb3ll/x"], author="Desktop_Agent5", run_gh=gh)
    assert events2 == []


def test_dry_run_does_not_create_db(watcher_env, capsys):
    """Dry-run never touches the DB: no memory.db appears on a fresh UA_HOME."""
    db = watcher_env / "memory.db"
    if db.exists():
        db.unlink()
    seed_cursor(watcher_env, {"anonb3ll/x": [make_pr(9)]})
    gh = FakeGh({"anonb3ll/x": [make_pr(1), make_pr(9)]})
    events = pr_watch.run(dry_run=True, repos=["anonb3ll/x"], run_gh=gh)
    assert [e.kind for e in events] == ["opened"]
    assert not db.exists(), "dry-run must not create or touch memory.db"


# --------------------------------------------------- opened+merged between polls


def test_pr_opened_and_merged_between_polls_emits_both(watcher_env):
    """UA #702 r1 follow-up: diff_events() short-circuited on prev is None, so
    a PR opened AND merged between two polls emitted only "opened" — the merge
    (and its rationale) never surfaced."""
    cur = pr_watch.parse_prs(
        [make_pr(1, body="feature work", state="MERGED", mergedAt="2026-08-31T12:00:00Z")]
    )
    events = pr_watch.diff_events("anonb3ll/x", {}, cur)
    assert [(e.kind, e.snapshot.number) for e in events] == [("opened", 1), ("merged", 1)]
    merged_note_title, merged_note_content = pr_watch.note_for_event(events[1])
    assert "feature work" in merged_note_content
    assert "merged" in merged_note_content
    assert merged_note_title == "PR #1 — anonb3ll/x"


def test_main_fails_when_enabled_connector_is_unconfigured(watcher_env, monkeypatch, capsys):
    # An enabled Approval Ledger connector with no repository polls nothing;
    # the scheduled pass must exit non-zero rather than report success.
    import sys

    from unified_agent import github_app_worker

    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setattr(
        github_app_worker,
        "run_once",
        lambda: [{"status": "unconfigured", "detail": "set UA_GITHUB_CONNECTOR_REPO"}],
    )
    called = []
    monkeypatch.setattr(pr_watch, "run", lambda **kwargs: called.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["pr-watch"])
    with pytest.raises(SystemExit) as exc:
        pr_watch.main()
    assert exc.value.code == 1
    assert len(called) == 1  # the legacy journal still ran first
    assert "connector poll failed" in capsys.readouterr().err


# ------------------------------------------------ durable run record (#1783)


def test_main_records_a_successful_run(watcher_env, monkeypatch):
    import sys

    from unified_agent import github_app_worker

    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    monkeypatch.setattr(github_app_worker, "run_once", lambda: [])
    monkeypatch.setattr(pr_watch, "run", lambda **_kwargs: [])
    monkeypatch.setattr(sys, "argv", ["pr-watch"])
    pr_watch.main()
    record = pr_watch.load_run_status()
    assert record["last_exit"] == 0
    assert record["last_error"] is None
    assert record["last_success_at"] == record["last_run_at"]
    assert record["consecutive_failures"] == 0
    assert pr_watch.status_path().parent == watcher_env


def test_main_records_a_failed_run_and_keeps_last_success(watcher_env, monkeypatch):
    """The #1783 incident: every scheduled run exited 1 and nothing durable said so."""
    import sys

    from unified_agent import github_app_worker

    pr_watch.record_run_status(ok=True)
    first_success = pr_watch.load_run_status()["last_success_at"]
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setattr(
        github_app_worker,
        "run_once",
        lambda: [{"status": "unconfigured", "detail": "set UA_GITHUB_CONNECTOR_REPO"}],
    )
    monkeypatch.setattr(pr_watch, "run", lambda **_kwargs: [])
    monkeypatch.setattr(sys, "argv", ["pr-watch"])
    for _ in range(2):
        with pytest.raises(SystemExit):
            pr_watch.main()
    record = pr_watch.load_run_status()
    assert record["last_exit"] == 1
    assert "connector poll failed" in record["last_error"]
    assert record["last_success_at"] == first_success
    assert record["consecutive_failures"] == 2


def test_dry_run_records_nothing(watcher_env, monkeypatch):
    import sys

    monkeypatch.setattr(pr_watch, "run", lambda **_kwargs: [])
    monkeypatch.setattr(sys, "argv", ["pr-watch", "--dry-run"])
    pr_watch.main()
    assert pr_watch.load_run_status() is None


def test_status_write_failure_never_changes_the_exit(watcher_env, monkeypatch):
    import sys

    from unified_agent import github_app_worker

    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    monkeypatch.setattr(github_app_worker, "run_once", lambda: [])
    monkeypatch.setattr(pr_watch, "run", lambda **_kwargs: [])
    monkeypatch.setattr(pr_watch, "_write_status", lambda record: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(sys, "argv", ["pr-watch"])
    pr_watch.main()  # must not raise


@needs_mac_hub_assets
def test_launcher_records_a_failure_before_the_entrypoint(tmp_path, monkeypatch):
    """run-pr-watch.sh exits before Python runs (no venv): the doctor must still see it."""
    script = Path(__file__).resolve().parents[1] / "deploy/mac-hub/run-pr-watch.sh"
    home = tmp_path / "home"
    home.mkdir()
    ua_home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(ua_home))
    env = {"HOME": str(home), "UA_HOME": str(ua_home), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(["bash", str(script)], env=env, check=False, capture_output=True)
    assert proc.returncode != 0
    record = json.loads(pr_watch.launch_failure_path().read_text())
    assert record["exit"] == proc.returncode
    assert pr_watch.run_health()["status"] == "DEGRADED"
    if shutil.which("perl"):
        # Microsecond precision lets the doctor order it against the run record.
        assert "." in record["failed_at"]


def _record_launch_failure(failed_at: str) -> None:
    pr_watch.launch_failure_path().write_text(json.dumps({"failed_at": failed_at, "exit": 127}))


def test_same_second_launch_failure_after_a_success_is_not_live(watcher_env):
    """The launcher may record whole seconds; a success earlier in that second must not mask it."""
    success = datetime(2026, 10, 2, 9, 28, 29, 100000, tzinfo=UTC)
    pr_watch.record_run_status(ok=True, now=success)
    # Failed at 09:28:29.700, stored with second precision by BSD date.
    _record_launch_failure("2026-10-02T09:28:29+00:00")
    health = pr_watch.run_health(now=success + timedelta(minutes=1))
    assert health["status"] == "DEGRADED"
    assert "launcher exited 127" in health["detail"]


def test_launch_failure_in_an_earlier_second_stays_behind_a_success(watcher_env):
    success = datetime(2026, 10, 2, 9, 28, 29, 100000, tzinfo=UTC)
    pr_watch.record_run_status(ok=True, now=success)
    _record_launch_failure("2026-10-02T09:28:28+00:00")
    assert pr_watch.run_health(now=success + timedelta(minutes=1))["status"] == "LIVE"


def test_microsecond_launch_failure_is_ordered_within_the_second(watcher_env):
    success = datetime(2026, 10, 2, 9, 28, 29, 500000, tzinfo=UTC)
    pr_watch.record_run_status(ok=True, now=success)
    later = success + timedelta(minutes=1)
    _record_launch_failure("2026-10-02T09:28:29.200000+00:00")
    assert pr_watch.run_health(now=later)["status"] == "LIVE"
    _record_launch_failure("2026-10-02T09:28:29.700000+00:00")
    assert pr_watch.run_health(now=later)["status"] == "DEGRADED"


# ---------------------------------------------------- slow GitHub (task #2431)


def test_timed_out_gh_call_is_retried_once(watcher_env):
    seed_cursor(watcher_env, {"example/x": [make_pr(1)]})
    gh = SlowGh({"example/x": [make_pr(1)]}, slow="repo", times=1)
    skipped: list[str] = []
    assert pr_watch.run(dry_run=False, author="Desktop_Agent5", run_gh=gh, skipped=skipped) == []
    assert [c[1] for c in gh.calls][:3] == ["repo", "repo", "pr"]
    assert skipped == []


def test_discovery_timeout_falls_back_to_known_repos(watcher_env, capsys):
    seed_cursor(watcher_env, {"example/x": [make_pr(1)]})
    gh = SlowGh({"example/x": [make_pr(1), make_pr(2)]}, slow="repo", times=2)
    skipped: list[str] = []
    events = pr_watch.run(dry_run=False, author="Desktop_Agent5", run_gh=gh, skipped=skipped)
    assert [e.snapshot.number for e in events] == [2]
    assert (
        len(skipped) == 1 and "repo discovery" in skipped[0] and "timed out 2 times" in skipped[0]
    )
    assert "pr-watch: skipped repo discovery" in capsys.readouterr().err
    # No cursor to fall back to: the run still fails, loudly.
    (watcher_env / pr_watch.CURSOR_FILE).unlink()
    with pytest.raises(RuntimeError, match="timed out"):
        pr_watch.run(dry_run=False, run_gh=SlowGh({}, slow="repo", times=2))


def test_one_slow_repo_is_skipped_and_keeps_its_cursor(watcher_env):
    seed_cursor(watcher_env, {"example/x": [make_pr(1)], "example/y": [make_pr(5)]})
    gh = SlowGh({"example/x": [make_pr(1)], "example/y": [make_pr(5), make_pr(6)]}, "example/x", 2)
    skipped: list[str] = []
    events = pr_watch.run(
        dry_run=False, repos=["example/x", "example/y"], run_gh=gh, skipped=skipped
    )
    assert [(e.repo, e.snapshot.number) for e in events] == [("example/y", 6)]
    assert skipped and skipped[0].startswith("example/x:")
    cursor = pr_watch.load_cursor(watcher_env / pr_watch.CURSOR_FILE)
    assert set(cursor["example/x"]) == {1} and set(cursor["example/y"]) == {5, 6}
    with pytest.raises(RuntimeError, match="every repository failed"):
        pr_watch.run(dry_run=False, repos=["example/x"], run_gh=SlowGh({}, "example/x", 2))


def test_a_skip_is_recorded_and_is_not_healthy(watcher_env):
    pr_watch.record_run_status(ok=True, skipped=["example/x: gh pr list timed out 2 times"])
    record = pr_watch.load_run_status()
    assert record["last_exit"] == 0 and record["last_skipped"] == [
        "example/x: gh pr list timed out 2 times"
    ]
    health = pr_watch.run_health()
    # The job ran, but one repository is not being watched (task #2486).
    assert health["status"] == "DEGRADED" and "1 skip(s) unresolved" in health["detail"]


# ------------------------------------------------ unresolved skips (task #2486)


def _timed_out(*repos: str) -> list[str]:
    return [f"{repo}: gh pr list timed out 2 times" for repo in repos]


def test_seven_skips_report_a_total_of_seven(watcher_env):
    """The stored list was cut to five, so seven skipped repositories read as five."""
    pr_watch.record_run_status(ok=True, skipped=_timed_out(*(f"example/r{n}" for n in range(7))))
    record = pr_watch.load_run_status()
    assert record["skipped_total"] == 7
    assert len(record["last_skipped"]) == 5  # the readable list stays capped
    assert "7 skip(s) unresolved" in pr_watch.run_health()["detail"]


def test_connector_only_tick_keeps_journal_skips(watcher_env):
    """A tick with no journal pass wrote an empty list and read as healthy."""
    pr_watch.record_run_status(ok=True, skipped=_timed_out("example/x"), connector=True)
    pr_watch.record_run_status(ok=True, connector=True)
    record = pr_watch.load_run_status()
    assert record["skipped_total"] == 1 and record["last_skipped"] == _timed_out("example/x")
    assert pr_watch.run_health()["status"] == "DEGRADED"
    # The Approval Ledger poll is judged on its own passes.
    assert pr_watch.connector_health()["status"] == "LIVE"


def test_a_skip_clears_only_when_that_repository_is_read_again(watcher_env):
    pr_watch.record_run_status(ok=True, skipped=_timed_out("example/x", "example/y"))
    # A pass over another repository retried neither.
    pr_watch.record_run_status(ok=True, skipped=[], polled=["example/z"])
    assert pr_watch.load_run_status()["skipped_total"] == 2
    pr_watch.record_run_status(ok=True, skipped=_timed_out("example/y"), polled=["example/x"])
    record = pr_watch.load_run_status()
    assert record["skipped_total"] == 1 and record["last_skipped"] == _timed_out("example/y")
    pr_watch.record_run_status(ok=True, skipped=[], polled=["example/y"])
    assert pr_watch.load_run_status()["skipped_total"] == 0
    assert pr_watch.run_health()["status"] == "LIVE"


def test_a_failed_connector_poll_outlives_a_journal_only_tick(watcher_env):
    pr_watch.record_run_status(ok=False, error="connector poll failed", connector=False)
    pr_watch.record_run_status(ok=True, skipped=[], polled=[pr_watch.DISCOVERY_KEY])
    assert pr_watch.load_run_status()["last_exit"] == 0
    for health in (pr_watch.run_health(), pr_watch.connector_health()):
        assert health["status"] == "DEGRADED" and "connector poll failed" in health["detail"]
    pr_watch.record_run_status(ok=True, connector=True)
    assert pr_watch.run_health()["status"] == "LIVE"


def test_status_file_from_before_the_split_still_reads(watcher_env):
    pr_watch.status_path().write_text(
        json.dumps(
            {
                "last_run_at": datetime.now(UTC).isoformat(),
                "last_exit": 0,
                "last_error": None,
                "last_success_at": datetime.now(UTC).isoformat(),
                "consecutive_failures": 0,
                "last_skipped": _timed_out("example/x"),
            }
        )
    )
    assert pr_watch.run_health()["status"] == "DEGRADED"
    assert pr_watch.connector_health()["status"] == "LIVE"  # no section: the whole run counts
    pr_watch.record_run_status(ok=True, connector=True)
    assert pr_watch.load_run_status()["skipped_total"] == 1  # carried into the new record
    pr_watch.record_run_status(ok=True, skipped=[], polled=["example/x"])
    assert pr_watch.run_health()["status"] == "LIVE"


def test_a_run_that_fails_before_either_pass_is_not_a_healthy_connector(watcher_env):
    pr_watch.record_run_status(ok=True, connector=True)
    pr_watch.record_run_status(ok=False, error="no such table: memory")
    health = pr_watch.connector_health()
    assert health["status"] == "DEGRADED" and "no such table" in health["detail"]
    # A journal failure on a tick without the poll says nothing about the poll.
    pr_watch.record_run_status(ok=True, connector=True)
    pr_watch.record_run_status(ok=False, error="every repository failed", journal=False)
    assert pr_watch.connector_health()["status"] == "LIVE"
    assert pr_watch.run_health()["status"] == "DEGRADED"


def test_scheduled_ticks_keep_a_skip_until_the_journal_reads_the_repo(watcher_env, monkeypatch):
    """End to end: journal tick skips a repo, a connector-only tick, then a clean journal tick."""
    import sys

    from unified_agent import github_app_fastpath, github_app_worker

    seed_cursor(watcher_env, {"example/x": [make_pr(1)], "example/y": [make_pr(5)]})
    repos = {"example/x": [make_pr(1)], "example/y": [make_pr(5)]}
    gh = SlowGh(repos, "example/x", 2)
    real_run = pr_watch.run
    monkeypatch.setattr(pr_watch, "run", lambda **kwargs: real_run(run_gh=gh, **kwargs))
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    monkeypatch.setattr(github_app_worker, "run_once", lambda: [])
    monkeypatch.setattr(github_app_worker, "ledger_in_flight", lambda: True)
    monkeypatch.setattr(github_app_fastpath, "open_heads_changed", lambda: False)
    monkeypatch.setattr(sys, "argv", ["pr-watch"])
    start = datetime.now(UTC) - timedelta(minutes=40)
    clock = {"now": start}
    monkeypatch.setattr(pr_watch, "_now", lambda: clock["now"])

    pr_watch.main()  # journal and connector; example/x times out twice
    assert pr_watch.load_run_status()["skipped_total"] == 1
    clock["now"] = start + timedelta(minutes=5)
    pr_watch.main()  # a ledger row is in flight: connector only
    record = pr_watch.load_run_status()
    assert record["last_exit"] == 0 and record["skipped_total"] == 1
    assert record["journal"]["last_run_at"] != record["connector"]["last_run_at"]
    assert pr_watch.run_health()["status"] == "DEGRADED"
    clock["now"] = start + timedelta(minutes=35)
    pr_watch.main()  # the journal is due again and example/x answers
    assert pr_watch.load_run_status()["skipped_total"] == 0
    assert pr_watch.run_health()["status"] == "LIVE"
