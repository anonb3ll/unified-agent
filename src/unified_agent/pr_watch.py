"""Poll GitHub PR state for configured repositories and surface events into hub memory.

Buzz-emulation #1 (UA task #702): PR state otherwise lives only in GitHub and
iMessage. This is the cheapest "branches as channels" emulation — a cron-able
poller that diffs each repo's open/closed PR state against a last-seen cursor
and, for every meaningful event (opened → reviewed → merged, with review
rationale when present), writes:

- one rolling memory note per (repo, PR number) tagged ``project:<repo>`` and
  ``pr:<repo>#<n>``, authored by the configured agent label (default
  ``Desktop_Agent5``) — later events append to the same note rather than
  minting a near-duplicate (task #1335), and
- one journal-format bookkeeping line pointing at that memory note, appended
  to the day's pr-watch note ``Logs/pr-watch/<YYYY-MM-DD>.md`` (task #1920).
  The shared daily journal ``Logs/<YYYY-MM-DD>.md`` gets at most one line per
  day from this job: a pointer to that note, written by the first run of the
  day that has events. Per-event lines once made up most of the daily journal.

Idempotency: the cursor (``$UA_HOME/pr-watch-cursor.json``) stores each PR's
last-seen snapshot; a PR whose snapshot is unchanged produces no writes, so
re-runs never duplicate notes. No new infra, no new tables — the note and
journal-line formats reuse the existing memory/vault tools verbatim.

Dry-run mode (``--dry-run``) prints the events it *would* write and leaves
the cursor and the memory/vault stores untouched — the cursor only ever
advances after real writes complete, so re-running dry always shows the
same pending events.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import config, post_init
from .pr_models import _DIFF_FIELDS, PREvent, PRSnapshot  # noqa: F401 - re-exported

DEFAULT_AUTHOR = "Desktop_Agent5"
CURSOR_FILE = "pr-watch-cursor.json"
# Durable outcome of the last scheduled run (task #1783). launchd keeps only the
# last exit code in memory, and system_doctor must not shell out to launchctl,
# so the job records its own result next to its cursor.
STATUS_FILE = "pr-watch-status.json"
# Written by deploy/mac-hub/run-pr-watch.sh when it fails before Python
# starts (service.env unreadable, venv missing), so no run is ever recorded.
LAUNCH_FAILURE_FILE = "pr-watch-launch-failure.json"
# The idle cadence: the legacy journal, and the Approval Ledger poll while no row
# is in flight, run at most this often (task #1867). It was the plist
# StartInterval; the job may now be launched more often and skips a tick with
# nothing due (``plan_tick``). Also the doctor's run budget below.
SCHEDULE_INTERVAL_S = 1800
# Persisted per-component last-run stamps the tick planner reads (task #1867).
SCHEDULE_FILE = "pr-watch-schedule.json"
# Held (non-blocking) for a whole live tick, so overlapping launches (a manual
# run during a scheduled one) plan, run and stamp one at a time; a launch that
# finds it held skips.
TICK_LOCK_FILE = "pr-watch.lock"
# Approval Ledger poll interval while a row is in flight (pending or approved),
# overridable by UA_APPROVAL_LEDGER_ACTIVE_INTERVAL_S within [60 s, idle].
LEDGER_ACTIVE_INTERVAL_S = 120
_LEDGER_ACTIVE_MIN_S = 60
# A launch may arrive slightly early (launchd StartInterval is measured from
# load, not from the previous run's stamp); a component is due once this much
# of its interval is left at most.
DUE_SLACK_FRACTION = 0.1
# A run may start late or take a while; only past interval + grace is it missed.
SCHEDULE_GRACE_S = 900
_STATUS_ERROR_CHARS = 240
#: Lines kept in the run record's readable ``last_skipped``; ``skipped_total``
#: carries the full count (task #2486).
_STATUS_SKIPPED_LINES = 5
#: The skip key for repository discovery; every other key is a repository name.
DISCOVERY_KEY = "repo discovery"
#: The two passes one tick may run; each keeps its own section in the run record.
_COMPONENTS = ("journal", "connector")
GH_TIMEOUT_SECONDS = 60
#: A timed-out gh call is tried this many times in all (task #2431).
GH_ATTEMPTS = 2

# vault_journal enforces a 240-char entry cap (task #379); our lines carry the
# agent/timestamp prefix inside vault_journal too, so stay well under it.
_JOURNAL_ROOM = 180
# Per-event lines go to Logs/<JOURNAL_STREAM>/<day>.md (task #1920).
JOURNAL_STREAM = "pr-watch"

# Event kinds in lifecycle order; a PR that crosses several steps between two
# polls reports each step (synchronize, ready_for_review, review, and merge) in
# that order.
KIND_ORDER = {
    "opened": 0,
    "synchronize": 1,
    "ready_for_review": 2,
    "reviewed": 3,
    "merged": 4,
    "closed": 5,
}


def reviews_snapshot(reviews: list[dict]) -> tuple[str | None, str | None, str, str]:
    """Reduce a PR's review list to (latest submittedAt, state, body, author).

    ``gh`` returns reviews oldest-first; empty for a PR with no reviews.
    Approvals and CHANGES_REQUESTED both count as review activity — the event
    is 'someone looked at this', which is exactly what Buzz's channel would
    surface.
    """
    latest = None
    for r in reviews:
        # gh returns camelCase (submittedAt); accept snake_case too for
        # hand-built fixtures/cursors.
        submitted = r.get("submittedAt") or r.get("submitted_at")
        if not submitted:
            continue
        if latest is None or submitted > (latest.get("submittedAt") or latest.get("submitted_at")):
            latest = r
    if latest is None:
        return None, None, "", ""
    return (
        latest.get("submittedAt") or latest.get("submitted_at"),
        latest.get("state"),
        (latest.get("body") or "").strip(),
        (latest.get("author") or {}).get("login", ""),
    )


def parse_prs(payload: list[dict]) -> dict[int, PRSnapshot]:
    """Map `gh pr list --json ...` output to {number: PRSnapshot}."""
    out: dict[int, PRSnapshot] = {}
    for p in payload:
        last_at, last_state, last_body, _author = reviews_snapshot(p.get("reviews") or [])
        out[int(p["number"])] = PRSnapshot(
            number=int(p["number"]),
            title=p.get("title", ""),
            url=p.get("url", ""),
            state=p.get("state", ""),
            head_sha=p.get("headRefOid") or "",
            merge_sha=str((p.get("mergeCommit") or {}).get("oid") or ""),
            merged_at=p.get("mergedAt"),
            closed_at=p.get("closedAt"),
            last_review_at=last_at,
            last_review_state=last_state,
            last_review_body=last_body,
            body=(p.get("body") or "").strip(),
            draft=bool(p.get("isDraft", p.get("draft", False))),
            author_login=str((p.get("author") or {}).get("login") or ""),
            labels=tuple(
                sorted(
                    str(label.get("name", ""))
                    for label in (p.get("labels") or [])
                    if isinstance(label, dict) and label.get("name")
                )
            ),
            base_branch=p.get("baseRefName") or p.get("base_ref_name") or "",
            head_branch=p.get("headRefName") or p.get("head_ref_name") or "",
        )
    return out


def diff_events(
    repo: str, previous: dict[int, PRSnapshot], current: dict[int, PRSnapshot]
) -> list[PREvent]:
    """Event-diff logic (the tested core): previous cursor vs fresh fetch.

    - number in current only            -> opened
    - state open->MERGED                -> merged (rationale: PR body when present)
    - new review activity since cursor  -> reviewed (rationale: latest review body)
    - closed without merge              -> closed (kept: an abandoned PR is a
      channel event too — Buzz surfaced dead branches, not just live ones)
    - snapshot unchanged                -> no event (idempotent re-run)
    """
    events: list[PREvent] = []
    for number in sorted(set(previous) | set(current)):
        prev = previous.get(number)
        cur = current.get(number)
        if cur is None:
            continue
        if prev is None:
            events.append(PREvent(repo, "opened", cur))
            if cur.merged_at:
                # Opened AND merged between two polls: the opened note alone
                # would read as live work; surface the merge too (UA #702 r1).
                events.append(PREvent(repo, "merged", cur, rationale=cur.body))
            continue
        if prev.diff_key() == cur.diff_key():
            continue
        if cur.head_sha and cur.head_sha != prev.head_sha:
            events.append(PREvent(repo, "synchronize", cur))
        if prev.draft and not cur.draft:
            events.append(PREvent(repo, "ready_for_review", cur))
        if cur.merged_at and not prev.merged_at:
            events.append(PREvent(repo, "merged", cur, rationale=cur.body))
        if cur.last_review_at and cur.last_review_at != prev.last_review_at:
            events.append(PREvent(repo, "reviewed", cur))
        if cur.state == "CLOSED" and not cur.merged_at and not prev.closed_at:
            events.append(PREvent(repo, "closed", cur))
    events.sort(key=lambda e: (e.snapshot.number, KIND_ORDER[e.kind]))
    return events


def discover_repos(run_gh) -> list[str]:
    """Non-fork repo names for the authenticated user (full name, e.g. example/x)."""
    raw = run_gh(["gh", "repo", "list", "--limit", "100", "--json", "nameWithOwner,isFork"])
    repos = [r["nameWithOwner"] for r in json.loads(raw) if not r.get("isFork")]
    return sorted(repos)


def fetch_repo_prs(repo: str, run_gh) -> dict[int, PRSnapshot]:
    """Current PR state for one repo via `gh pr list --state all`."""
    raw = run_gh(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            "number,title,body,url,state,headRefOid,headRefName,baseRefName,isDraft,author,labels,mergedAt,closedAt,mergeCommit,reviews",
        ]
    )
    return parse_prs(json.loads(raw))


def _rationale_text(event: PREvent) -> str:
    """The 'why' when present: latest review body for reviewed, PR body for merged."""
    snap = event.snapshot
    if event.kind == "reviewed":
        if snap.last_review_body:
            return f"{snap.last_review_state or 'REVIEWED'}: {snap.last_review_body}"
        return f"{snap.last_review_state or 'REVIEWED'}"
    if event.kind in ("merged", "closed") and snap.body:
        return snap.body
    return ""


def note_for_event(event: PREvent) -> tuple[str, str]:
    """(title, content) for the memory note an event produces."""
    snap = event.snapshot
    rationale = _rationale_text(event)
    lines = [
        f"PR #{snap.number} {event.kind} in {event.repo}: {snap.title}",
        f"State: {snap.state}; url: {snap.url}",
    ]
    if event.kind == "merged" and snap.merge_sha:
        lines.append(f"Merge commit: {snap.merge_sha}")
    if rationale:
        lines.append(f"Rationale: {rationale}")
    return f"PR #{snap.number} — {event.repo}", "\n".join(lines)


def pr_note_tag(repo: str, number: int) -> str:
    """Stable tag that uniquely identifies one PR's rolling memory note."""
    return f"pr:{repo}#{int(number)}"


def _title_matches_pr(title: str, number: int) -> bool:
    """True when ``title`` is for exactly this PR number (not a longer prefix).

    ``startswith("PR #9")`` would also match ``PR #90 …``; require a non-digit
    (or end of string) after the number.
    """
    return bool(re.match(rf"^PR #{int(number)}(?:\D|$)", title or ""))


def find_pr_note_id(repo: str, number: int) -> str | None:
    """Return the existing rolling note id for (repo, PR), if any.

    Prefers the stable ``pr:<repo>#<n>`` tag. Falls back to legacy pre-#1335
    notes that only carried ``project:<repo>`` + ``pr-watch`` so an already
    journaled PR does not mint a second note on the next event.

    Legacy scan uses ``memory_list`` (newest-first, up to 200) rather than an
    FTS top-N window so a matching note is not dropped when many other hits
    outrank it.
    """
    from .tools.memory import memory_list

    tag = pr_note_tag(repo, number)
    items = memory_list(tag=tag, limit=5).get("items") or []
    if items:
        return items[0]["id"]
    # Legacy: list this project's notes and match exact PR-number title prefix.
    project_tag = f"project:{repo}"
    for row in memory_list(tag=project_tag, limit=200).get("items") or []:
        tags = row.get("tags") or []
        if "pr-watch" not in tags:
            continue
        if _title_matches_pr(row.get("title") or "", number):
            return row["id"]
    return None


def upsert_pr_note(event: PREvent, author: str) -> str:
    """Create or append to the single rolling memory note for this PR.

    Returns the note id. Five events on one PR yield one note (task #1335).
    """
    from .tools.memory import memory_get, memory_save

    title, content = note_for_event(event)
    required_tags = [
        f"project:{event.repo}",
        "pr-watch",
        pr_note_tag(event.repo, event.snapshot.number),
    ]
    existing_id = find_pr_note_id(event.repo, event.snapshot.number)
    if existing_id is None:
        res = memory_save(content=content, title=title, tags=required_tags, author=author)
        return res["id"]
    # Preserve any tags peers added; ensure required pr-watch tags remain.
    existing_tags = memory_get(existing_id).get("tags") or []
    merged_tags = sorted(set(existing_tags) | set(required_tags))
    res = memory_save(
        content=content,
        title=title,
        tags=merged_tags,
        id=existing_id,
        author=author,
        append=True,
    )
    return res["id"]


def journal_for_event(event: PREvent, note_id: str) -> str:
    """One journal line per event — a pointer to the memory note, not the record (task #379 cap).

    The line lands in the day's pr-watch note, not the shared daily journal
    (task #1920); see :func:`journal_pointer`.

    The note id and event identity come first; the title is filler and gets
    truncated so the entry always fits `_JOURNAL_ENTRY_MAX_CHARS`.
    """
    snap = event.snapshot
    head = f"PR #{snap.number} {event.kind} in {event.repo}"
    line = f"{head} ({snap.title}) — memory {note_id}"
    if len(line) > _JOURNAL_ROOM:
        room = _JOURNAL_ROOM - len(f"{head} (…) — memory {note_id}")
        line = f"{head} ({snap.title[:room]}…) — memory {note_id}"
    return line


def journal_pointer(day: str) -> tuple[str, str]:
    """(needle, entry) for the single daily-journal line that points at the day's pr-watch note."""
    link = f"[[Logs/{JOURNAL_STREAM}/{day}]]"
    return link, f"pr-watch: today's PR events are logged in {link} (one line per event)"


def load_cursor(path: Path) -> dict[str, dict[int, PRSnapshot]]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text())
    return {
        repo: {
            int(n): PRSnapshot(
                number=int(n),
                title="",
                url="",
                **{**snap, "labels": tuple(snap.get("labels") or ())},
            )
            for n, snap in prs.items()
        }
        for repo, prs in raw.items()
    }


def save_cursor(path: Path, cursor: dict[str, dict[int, PRSnapshot]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                repo: {n: snap.to_cursor() for n, snap in prs.items()}
                for repo, prs in cursor.items()
            },
            indent=1,
        )
    )


def _notify_owners(
    events: list[PREvent], fetched: dict[str, dict[int, PRSnapshot]], gh: Callable[[list[str]], str]
) -> None:
    """Message task claimants (task #2396). A failure here never costs the poll."""
    from . import pr_owner_notify

    failures: list[PREvent] = []
    for repo, current in fetched.items():
        if not any(p.state == "OPEN" for p in current.values()):
            continue
        try:
            failures.extend(pr_owner_notify.ci_failures(repo, gh, current))
        except Exception as exc:  # noqa: BLE001 - best effort beside the journal
            print(f"pr-watch: notify: CI read failed for {repo}: {exc}", file=sys.stderr)
    try:
        counts = pr_owner_notify.notify([*events, *failures])
    except Exception as exc:  # noqa: BLE001 - best effort beside the journal
        print(f"pr-watch: notify failed: {exc}", file=sys.stderr)
        return
    if counts["sent"]:
        print(f"pr-watch: told {counts['sent']} owner(s) about their PRs")


def _gh_once_retried(run_gh, cmd: list[str]) -> str:
    """Run one ``gh`` command, retrying once when it times out (task #2431).

    A slow GitHub answer is common and usually passes on the next try; a
    non-zero exit is an answer and is not retried.
    """
    for attempt in range(1, GH_ATTEMPTS + 1):
        try:
            proc = run_gh(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            if attempt == GH_ATTEMPTS:
                raise RuntimeError(
                    f"{' '.join(cmd[:3])} timed out {GH_ATTEMPTS} times "
                    f"({GH_TIMEOUT_SECONDS}s each)"
                ) from None
            continue
        if proc.returncode != 0:
            raise RuntimeError(f"{cmd[1]} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
        return proc.stdout
    raise AssertionError("unreachable")  # pragma: no cover


def run(
    dry_run: bool = False,
    repos: list[str] | None = None,
    author: str = DEFAULT_AUTHOR,
    run_gh=subprocess.run,
    skipped: list[str] | None = None,
    polled: list[str] | None = None,
) -> list[PREvent]:
    """Poll, diff, and write. Returns the events emitted (also printed).

    Writes go through the existing memory/vault tools with the configured
    author label; the cursor advances only after every event is written, so a
    crash mid-run re-processes the same events on the next tick rather than
    losing them (memory_save + journal lines are the duplicated cost, not the
    missing one).

    One slow or failing repository no longer fails the run (task #2431): a
    timed-out ``gh`` call is retried once; when repository discovery still
    fails, the repositories already in the cursor are polled instead; a repo
    whose PR list cannot be read keeps its cursor and is skipped. Each skip is
    appended to ``skipped`` (and printed) as ``"<key>: <why>"``, the key being
    the repository name or ``DISCOVERY_KEY``. Each key read successfully is
    appended to ``polled``, so the run record can tell a skip that was retried
    from one that was not (task #2486). The run still fails when there is
    nothing to fall back to or every repository failed.
    """
    skips = skipped if skipped is not None else []
    read = polled if polled is not None else []

    def _gh(cmd: list[str]) -> str:
        return _gh_once_retried(run_gh, cmd)

    cursor_path = config.data_home() / CURSOR_FILE
    cursor = load_cursor(cursor_path)
    if repos is None:
        try:
            repos = discover_repos(_gh)
        except (RuntimeError, ValueError) as exc:
            if not cursor:
                raise
            repos = sorted(cursor)
            skips.append(f"{DISCOVERY_KEY}: {exc}; polled {len(repos)} known repo(s)")
        else:
            read.append(DISCOVERY_KEY)

    events: list[PREvent] = []
    fetched: dict[str, dict[int, PRSnapshot]] = {}
    failed: list[str] = []
    for repo in repos:
        try:
            current = fetch_repo_prs(repo, _gh)
        except (RuntimeError, ValueError) as exc:
            failed.append(repo)
            skips.append(f"{repo}: {exc}")
            continue
        read.append(repo)
        fetched[repo] = current
        if repo not in cursor:
            # First sighting of this repo: seed the baseline silently instead
            # of flooding memory with an "opened" event for every historical
            # PR (most already merged). Only transitions after this run count.
            print(
                f"pr-watch: {repo}: first run — baseline {len(current)} PR(s) recorded, no events"
            )
            continue
        events.extend(diff_events(repo, cursor[repo], current))

    for line in skips:
        print(f"pr-watch: skipped {line}", file=sys.stderr)
    if repos and len(failed) == len(repos):
        raise RuntimeError(f"every repository failed: {skips[-1]}")

    if not dry_run:
        _notify_owners(events, fetched, _gh)

    if not events:
        if dry_run:
            print("pr-watch: no new PR events (dry-run: cursor left untouched)")
        else:
            print("pr-watch: no new PR events")
            save_cursor(cursor_path, {**cursor, **fetched})
        return []

    if dry_run:
        for e in events:
            title, content = note_for_event(e)
            print(
                f"[dry-run] would memory_save (upsert): {title!r} "
                f"tags=[project:{e.repo}, pr-watch, {pr_note_tag(e.repo, e.snapshot.number)}] "
                f"author={author}"
            )
            print(f"[dry-run]   {content}")
            print(
                f"[dry-run] would append to Logs/{JOURNAL_STREAM}/<today>.md: "
                f"{journal_for_event(e, '<note-id>')}"
            )
        print(f"[dry-run] cursor NOT advanced ({len(events)} event(s) still pending)")
        return events

    from .tools.vault import journal_pointer_append, journal_stream_append

    # One day for the whole run, so the event lines and the pointer agree.
    day = datetime.now().strftime("%Y-%m-%d")
    for e in events:
        note_id = upsert_pr_note(e, author)
        journal_stream_append(
            JOURNAL_STREAM,
            entry=journal_for_event(e, note_id),
            agent=author,
            day=day,
            tags=["pr-watch", e.repo],
        )
        print(f"pr-watch: #{e.snapshot.number} {e.kind} in {e.repo} -> memory {note_id}")
    # At most one daily-journal line per day (task #1920). Checking the daily
    # note itself, not whether the pr-watch note is new, keeps this idempotent
    # and retries the pointer if a previous run died before writing it.
    needle, pointer = journal_pointer(day)
    journal_pointer_append(entry=pointer, agent=author, day=day, needle=needle)

    save_cursor(cursor_path, {**cursor, **fetched})
    return events


def status_path() -> Path:
    return config.data_home() / STATUS_FILE


def launch_failure_path() -> Path:
    return config.data_home() / LAUNCH_FAILURE_FILE


def _launch_failure_after(last_run_at: object, now: datetime) -> dict | None:
    """The launcher failure newer than the last recorded run, if any."""
    try:
        failure = json.loads(launch_failure_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(failure, dict):
        return None
    failed_at = _parse_stamp(failure.get("failed_at"))
    if failed_at is None:
        return None
    ran_at = _parse_stamp(last_run_at)
    if ran_at is not None:
        # The launcher may only record whole seconds (BSD date has no %N),
        # while the run record keeps microseconds. Compare at the coarser
        # precision of the two, and let a tie count as the failure: a run
        # earlier in the same second must not hide a failed launch.
        if failed_at.microsecond == 0 or ran_at.microsecond == 0:
            failed_at = failed_at.replace(microsecond=0)
            ran_at = ran_at.replace(microsecond=0)
        if ran_at > failed_at:
            return None
    return {"age": (now - failed_at).total_seconds(), "exit": failure.get("exit")}


def load_run_status() -> dict | None:
    """The last recorded run, or None when none is recorded or it is unreadable."""
    try:
        record = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _write_status(record: dict) -> None:
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        json.dump(record, stream)
        temp = Path(stream.name)
    os.replace(temp, path)


def _pass_record(previous: object, ok: bool, stamp: str, error: str) -> dict:
    """One run's outcome over the record before it, keeping the last success."""
    before = previous if isinstance(previous, dict) else {}
    return {
        "last_run_at": stamp,
        "last_exit": 0 if ok else 1,
        "last_error": None if ok else error,
        "last_success_at": stamp if ok else before.get("last_success_at"),
        "consecutive_failures": 0 if ok else int(before.get("consecutive_failures") or 0) + 1,
    }


def _skip_key(line: str) -> str:
    """What a ``"<key>: <why>"`` skip line is about: a repository, or ``DISCOVERY_KEY``."""
    return line.split(": ", 1)[0]


def unresolved_skips(record: dict) -> dict[str, str]:
    """Skips no later journal pass has read successfully, as ``{key: line}``."""
    section = record.get("journal")
    kept = section.get("unresolved") if isinstance(section, dict) else None
    if isinstance(kept, dict):
        return {str(key): str(line) for key, line in kept.items()}
    # A record written before task #2486 holds only the last run's lines.
    lines = record.get("last_skipped")
    return {_skip_key(str(line)): str(line) for line in lines} if isinstance(lines, list) else {}


def _carry_skips(previous: dict, skipped: list[str], polled: list[str]) -> dict[str, str]:
    """The unresolved skips after one journal pass.

    A skip leaves only when its key is read again. A pass whose discovery
    answered tried every watched repository, so anything it did not skip either
    succeeded or is no longer watched. A pass over fewer repositories (explicit
    ``--repos``, or the cursor's when discovery failed) clears only what it read.
    """
    kept = {} if DISCOVERY_KEY in polled else unresolved_skips(previous)
    for key in polled:
        kept.pop(key, None)
    for line in skipped:
        kept[_skip_key(line)] = line[:_STATUS_ERROR_CHARS]
    return kept


def record_run_status(
    ok: bool,
    error: str | None = None,
    now: datetime | None = None,
    skipped: list[str] | None = None,
    *,
    polled: list[str] | None = None,
    journal: bool | None = None,
    connector: bool | None = None,
    disabled: tuple[str, ...] = (),
) -> None:
    """Record one scheduled run's outcome, keeping the last success across failures.

    ``journal`` and ``connector`` say how each pass went in this run: True or
    False, or None when the run did not reach it. A pass that did not run
    keeps its section, so a connector-only tick cannot erase what the journal
    left unresolved, nor the reverse (task #2486). ``disabled`` names a pass
    this installation no longer runs: its section and skips are dropped,
    because nothing would ever retry them.

    ``skipped`` names what a journal pass passed over (task #2431): a repo
    whose PR list timed out, or discovery that fell back to known repos.
    ``polled`` names what it read. Passing either means a journal pass ran.
    Skips stay recorded until a later pass reads them (``_carry_skips``).
    The number of repositories one account lists bounds how many there can be.
    """
    stamp = (now or datetime.now(UTC)).isoformat()
    previous = load_run_status() or {}
    first_line = (error or "").strip().splitlines()[0] if (error or "").strip() else ""
    message = first_line[:_STATUS_ERROR_CHARS] or "unknown error"
    if journal is None and (skipped is not None or polled is not None):
        journal = ok
    record = _pass_record(previous, ok, stamp, message)
    for name, outcome in zip(_COMPONENTS, (journal, connector), strict=True):
        if name in disabled:
            continue
        if outcome is not None:
            record[name] = _pass_record(previous.get(name), outcome, stamp, message)
        elif isinstance(previous.get(name), dict):
            record[name] = dict(previous[name])
    if "journal" in disabled:
        unresolved = {}
    elif journal is not None:
        unresolved = _carry_skips(previous, skipped or [], polled or [])
    else:
        unresolved = unresolved_skips(previous)
    if unresolved or "journal" in record:
        record.setdefault("journal", {})["unresolved"] = unresolved
    # Readable copy for people and for readers older than the sections.
    record["last_skipped"] = list(unresolved.values())[:_STATUS_SKIPPED_LINES]
    record["skipped_total"] = len(unresolved)
    _write_status(record)


def _parse_stamp(stamp: object) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


def _age_s(stamp: object, now: datetime) -> float | None:
    when = _parse_stamp(stamp)
    return None if when is None else (now - when).total_seconds()


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)}m"


def _record_verdict(now: datetime) -> tuple[dict | None, dict]:
    """``(verdict, record)``: a verdict when no usable run record settles it."""
    path = status_path()
    record = load_run_status()
    launch = _launch_failure_after((record or {}).get("last_run_at"), now)
    if launch is not None:
        success_age = _age_s((record or {}).get("last_success_at"), now)
        return {
            "status": "DEGRADED",
            "detail": (
                f"the pr-watch launcher exited {launch['exit']} {_minutes(launch['age'])} ago "
                f"before the job started ({launch_failure_path()}); "
                + (
                    f"last success {_minutes(success_age)} ago"
                    if success_age is not None
                    else "no successful run on record"
                )
            ),
        }, {}
    if not path.exists():
        return {"status": None, "detail": f"no pr-watch run recorded at {path}"}, {}
    if record is None or "last_exit" not in record:
        return {"status": "UNKNOWN", "detail": f"unreadable pr-watch run record at {path}"}, {}
    return None, record


def _since_success(record: dict, now: datetime) -> str:
    success_age = _age_s(record.get("last_success_at"), now)
    if success_age is None:
        return "no successful run on record"
    return f"last success {_minutes(success_age)} ago"


def _pass_verdict(record: dict, now: datetime, what: str) -> dict | None:
    """DEGRADED when this pass last failed or has not succeeded within the budget."""
    since_success = _since_success(record, now)
    if record.get("last_exit") != 0:
        run_age = _age_s(record.get("last_run_at"), now)
        when = f"{_minutes(run_age)} ago" if run_age is not None else "at an unknown time"
        failures = int(record.get("consecutive_failures") or 1)
        return {
            "status": "DEGRADED",
            "detail": (
                f"last pr-watch {what} {when} exited 1 ({failures} consecutive failure(s)): "
                f"{record.get('last_error') or 'unknown error'}; {since_success}"
            ),
        }
    success_age = _age_s(record.get("last_success_at"), now)
    budget = SCHEDULE_INTERVAL_S + SCHEDULE_GRACE_S
    if success_age is None or success_age > budget:
        return {
            "status": "DEGRADED",
            "detail": (
                f"no successful {what} in the last {_minutes(budget)} "
                f"(interval {_minutes(SCHEDULE_INTERVAL_S)} + grace); {since_success} — "
                "the job may be unloaded or not firing"
            ),
        }
    return None


def run_health(now: datetime | None = None) -> dict:
    """Judge the recorded runs: LIVE, DEGRADED, UNKNOWN, or ``None`` status when unrecorded.

    DEGRADED when the last run failed, when the last success is older than
    one schedule interval plus grace (the job stopped running, or keeps
    failing), when either pass failed the last time it ran, or while a skipped
    repository has not been read again (task #2486).
    """
    now = now or datetime.now(UTC)
    verdict, record = _record_verdict(now)
    verdict = verdict or _pass_verdict(record, now, "run")
    if verdict is not None:
        return verdict
    since_success = _since_success(record, now)
    for name in _COMPONENTS:
        section = record.get(name)
        if isinstance(section, dict) and section.get("last_exit", 0) != 0:
            # A later run without this pass succeeded; the pass itself has not.
            failed_age = _age_s(section.get("last_run_at"), now)
            when = f"{_minutes(failed_age)} ago" if failed_age is not None else "earlier"
            return {
                "status": "DEGRADED",
                "detail": (
                    f"the pr-watch {name} pass failed {when} and has not run since: "
                    f"{section.get('last_error') or 'unknown error'}; {since_success}"
                ),
            }
    unresolved = unresolved_skips(record)
    if unresolved:
        # The run polled what it could (task #2431), but what it passed over is
        # not being watched until a later pass reads it.
        return {
            "status": "DEGRADED",
            "detail": f"pr-watch ran, {len(unresolved)} skip(s) unresolved: "
            + "; ".join(list(unresolved.values())[:2])
            + f"; {since_success}",
        }
    return {"status": "LIVE", "detail": f"pr-watch healthy; {since_success}"}


def connector_health(now: datetime | None = None) -> dict:
    """Judge the Approval Ledger poll alone, in ``run_health``'s vocabulary.

    A skipped or failing journal repository does not stop pull requests
    reaching the ledger, so only the connector's own passes count here. A
    record without a connector section (written before task #2486) is judged
    on the whole run, and so is a run that failed before either pass started.
    """
    now = now or datetime.now(UTC)
    verdict, record = _record_verdict(now)
    if verdict is not None:
        return verdict
    section = record.get("connector")
    last_run = record.get("last_run_at")
    reached = any(
        isinstance(record.get(name), dict) and record[name].get("last_run_at") == last_run
        for name in _COMPONENTS
    )
    if not isinstance(section, dict) or "last_exit" not in section:
        verdict = _pass_verdict(record, now, "run")
    elif record.get("last_exit") != 0 and not reached:
        # The last run failed before either pass started, so the poll did not run.
        verdict = _pass_verdict(record, now, "run")
    else:
        verdict = _pass_verdict(section, now, "connector poll")
    if verdict is not None:
        return verdict
    return {"status": "LIVE", "detail": "pr-watch connector poll healthy"}


# --- tick cadence (task #1867) ---------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def ledger_active_interval_s() -> int:
    """Approval Ledger poll interval while a row is in flight, in seconds."""
    raw = os.environ.get("UA_APPROVAL_LEDGER_ACTIVE_INTERVAL_S", "").strip()
    try:
        value = int(raw) if raw else LEDGER_ACTIVE_INTERVAL_S
    except ValueError:
        value = LEDGER_ACTIVE_INTERVAL_S
    return max(_LEDGER_ACTIVE_MIN_S, min(value, SCHEDULE_INTERVAL_S))


def schedule_path() -> Path:
    return config.data_home() / SCHEDULE_FILE


def load_schedule() -> dict:
    """The persisted last-run stamps, or ``{}`` when none or unreadable (all due)."""
    try:
        state = json.loads(schedule_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_schedule(state: dict) -> None:
    path = schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        json.dump(state, stream)
        temp = Path(stream.name)
    os.replace(temp, path)


def tick_lock_path() -> Path:
    return config.data_home() / TICK_LOCK_FILE


@contextlib.contextmanager
def _tick_lock():
    """Yield True while this process holds the tick lock, False when another does."""
    path = tick_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def is_due(last_run_at: object, interval_s: float, now: datetime) -> bool:
    """Due when never run, stamped in the future (clock change), or nearly an interval ago."""
    age = _age_s(last_run_at, now)
    if age is None or age < 0:
        return True
    return age >= interval_s * (1 - DUE_SLACK_FRACTION)


@dataclass(frozen=True)
class TickPlan:
    journal: bool
    connector: bool
    reason: str


def plan_tick(
    state: dict,
    now: datetime,
    *,
    in_flight: Callable[[], bool],
    changed: Callable[[], bool] | None = None,
    force: bool = False,
) -> TickPlan:
    """Decide what this launch of the job does (task #1867).

    The legacy journal and the Approval Ledger poll each run once per
    ``SCHEDULE_INTERVAL_S``. Between those, the poll also runs once per
    ``ledger_active_interval_s()`` while ``in_flight()`` (a local query) says a
    ledger row is pending or approved, or, failing that, when ``changed()``
    says the open PR heads moved since the last poll (task #2234: one
    conditional GitHub request, free when GitHub answers 304). Either is
    consulted only when it is the one thing that could make the tick do work,
    and a failure in either counts as a reason to poll: a missed poll costs
    more than one extra PR list.
    """
    if force:
        return TickPlan(True, True, "forced")
    journal = is_due(state.get("journal_last_run_at"), SCHEDULE_INTERVAL_S, now)
    connector_at = state.get("connector_last_run_at")
    if is_due(connector_at, SCHEDULE_INTERVAL_S, now):
        return TickPlan(journal, True, "idle interval")
    if is_due(connector_at, ledger_active_interval_s(), now):
        try:
            flight = bool(in_flight())
        except Exception as exc:  # noqa: BLE001 - fail toward polling
            print(f"pr-watch: in-flight check failed ({exc}); polling the ledger", file=sys.stderr)
            flight = True
        if flight:
            return TickPlan(journal, True, "approval ledger row in flight")
        if changed is not None:
            try:
                moved = bool(changed())
            except Exception as exc:  # noqa: BLE001 - fail toward polling
                print(
                    f"pr-watch: open-head probe failed ({exc}); polling the ledger", file=sys.stderr
                )
                moved = True
            if moved:
                return TickPlan(journal, True, "open pull request heads changed")
    return TickPlan(journal, False, "journal interval" if journal else "nothing due")


def _ledger_in_flight() -> bool:
    from . import github_app_worker

    return github_app_worker.ledger_in_flight()


def _ledger_heads_changed() -> bool:
    from . import github_app_fastpath

    return github_app_fastpath.open_heads_changed()


def _record_outcome(ok: bool, error: str | None = None, **passes) -> None:
    # The record serves the doctor; failing to write it must never change the
    # job's own exit status or hide the original error.
    try:
        record_run_status(ok, error, **passes)
    except Exception as exc:  # noqa: BLE001
        print(f"pr-watch: could not record run status: {exc}", file=sys.stderr)


def _stamp(state: dict, key: str, started: datetime) -> None:
    """Record one component's run; a failed write only costs an early re-run."""
    state[key] = started.isoformat()
    try:
        save_schedule(state)
    except OSError as exc:
        print(f"pr-watch: could not record schedule state: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Poll GitHub PR events into memory + vault journal."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print events without writing memory/journal or advancing the cursor",
    )
    parser.add_argument(
        "--repos",
        nargs="*",
        help="repo full names (owner/name); default: all non-fork repos of the authenticated user",
    )
    parser.add_argument(
        "--author",
        default=DEFAULT_AUTHOR,
        help=f"agent label for memory author + journal attribution (default {DEFAULT_AUTHOR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run the journal and the Approval Ledger poll even if neither is due",
    )
    args = parser.parse_args()
    if args.dry_run:
        _tick(args)
        return
    with _tick_lock() as held:
        if not held:
            print("pr-watch: another pr-watch run holds the tick lock; skipping this tick")
            return
        _tick(args)


def _tick(args: argparse.Namespace) -> None:
    """One launch: plan from fresh state, run what is due, stamp it (under the lock)."""
    started = _now()
    state: dict = {}
    plan = TickPlan(True, True, "dry run")
    if not args.dry_run:
        # Decide before the poll: a launch with nothing due exits here without
        # touching the run record, after at most one conditional GitHub
        # request (task #1867, #2234).
        state = load_schedule()
        plan = plan_tick(
            state,
            started,
            in_flight=_ledger_in_flight,
            changed=_ledger_heads_changed,
            force=args.force,
        )
        if not (plan.journal or plan.connector):
            print(f"pr-watch: {plan.reason}; skipping this tick")
            return
    skipped: list[str] = []
    polled: list[str] = []
    # How each pass went, for the run record (task #2486): None until the tick
    # reaches the pass, False while it runs or once it failed, True when done.
    journal_ok: bool | None = None
    connector_ok: bool | None = None
    journal_off = False

    def _passes() -> dict:
        ran = journal_ok is not None
        return {
            "journal": journal_ok,
            "connector": connector_ok,
            "skipped": skipped if ran else None,
            "polled": polled if ran else None,
            "disabled": ("journal",) if journal_off else (),
        }

    try:
        from . import github_app

        broker_only = (
            not args.dry_run
            and github_app.enabled()
            and github_app.token_mode() == github_app.TOKEN_MODE_USER
            and github_app.user_token_source() == "connector_oauth"
        )
        if broker_only and args.repos is not None:
            raise ValueError("explicit repository polling is unavailable in broker-only mode")
        connector_failed = False
        connector_detail = ""
        if not args.dry_run:
            # Standalone CLI entrypoint: ensure the schema exists before the
            # live write path touches it. A fresh UA_HOME (no memory.db)
            # otherwise dies with "no such table: memory" — every other
            # entrypoint does this first (consolidate, events, zulip_worker,
            # tui, server). Dry-run never touches the DB, so it skips the
            # init entirely.
            from . import storage

            post_init.register_default_hooks()
            storage.init_db()
        if not args.dry_run and plan.connector:
            # The existing Mac poll schedule is the connector's consumer. The
            # worker remains inert until its explicit runtime gate is enabled.
            # Run it first so a failure polling an unrelated repo for journal
            # notes cannot prevent the Approval Ledger status from appearing.
            from . import github_app_worker

            connector_ok = False
            try:
                connector_results = github_app_worker.run_once()
                # An enabled connector with no usable repository polls nothing;
                # report that as a failed pass, as the worker CLI does.
                failed = [
                    r for r in connector_results if r.get("status") in {"error", "unconfigured"}
                ]
                connector_failed = bool(failed)
                # Carry the cause into the error so the run record names it.
                connector_detail = "; ".join(
                    sorted({str(r.get("detail") or r.get("error") or r["status"]) for r in failed})
                )
            except Exception as exc:
                connector_failed = True
                connector_detail = f"{type(exc).__name__}: {exc}"
            if not connector_failed:
                connector_ok = True
                # A failed poll is not stamped, so the next tick retries it.
                _stamp(state, "connector_last_run_at", started)
        if plan.journal:
            if broker_only:
                # The wider legacy journal discovers every repo through gh. Brokered
                # credentials are deliberately in-process and scoped to the fixed
                # connector repo, so this scheduled pass cannot call that CLI.
                print("pr-watch: legacy multi-repo journal skipped in broker-only mode")
                journal_off = True
            else:
                journal_ok = False
                run(
                    dry_run=args.dry_run,
                    repos=args.repos,
                    author=args.author,
                    skipped=skipped,
                    polled=polled,
                )
                journal_ok = True
            if not args.dry_run:
                _stamp(state, "journal_last_run_at", started)
        if connector_failed:
            raise RuntimeError(
                f"github-app connector poll failed ({connector_detail}); "
                "legacy PR journal completed"
            )
    except Exception as exc:  # cron: log loudly, exit non-zero so launchd notices
        print(f"pr-watch: FAILED: {exc}", file=sys.stderr)
        if not args.dry_run:
            _record_outcome(False, str(exc), **_passes())
        raise SystemExit(1) from exc
    if not args.dry_run:
        _record_outcome(True, **_passes())


if __name__ == "__main__":
    main()
