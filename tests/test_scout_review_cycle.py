"""Historical scout verdicts cannot suppress a new report review cycle."""

import hashlib

import pytest

from unified_agent import reviewer, storage
from unified_agent.scout import build_scout_report, validate_scout_report
from unified_agent.tools import tasks


@pytest.mark.parametrize("path", ["00 - Inbox/Reusable report.md", '00 - Inbox/A "report".md'])
@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("prior", ["ready", "incomplete"])
def test_reused_report_is_reviewed_once_per_current_request(
    isolated_env, monkeypatch, valid, prior, path
):
    storage.init_db()
    report = (
        build_scout_report(
            topic="New content",
            bottom_line="New result",
            findings=["changed"],
            decisions=[{"decision": "Ship?", "options": ["yes", "no"]}],
        )
        if valid
        else "Changed report with no decision inventory"
    )
    old = (
        f"[review: scout-ready @ old] report={path} Historical ready"
        if prior == "ready"
        else f"[review: scout-incomplete @ old] report {path}: "
        + "; ".join(validate_scout_report(report))
    )
    notes = f"[task-shape: scout]\n[no-code]\n[scout: report={path}]\n[waiting-for: review]\n{old}\n[waiting-for: review]"
    task = tasks.task_add("Review changed scout report", notes=notes)
    reads, appended, notified = [], [], []

    def read(p, **kwargs):
        reads.append(p)
        return {"content": report, "truncated": False}

    def append(task_id, note):
        appended.append(note)
        tasks.task_update(task_id, append_note=note, worktree=False)

    monkeypatch.setattr("unified_agent.tools.vault.vault_read", read)
    monkeypatch.setattr(reviewer, "_append_note", append)
    monkeypatch.setattr(
        "unified_agent.tools.notify.notify",
        lambda message: notified.append(message) or {"delivered": True},
    )
    first = reviewer.review_and_notify(task["id"], dry_run=False)
    assert first["verdict"] == ("scout-ready" if valid else "scout-incomplete")
    assert first["notification"]
    assert reads == [path]
    assert len(appended) == 1
    with storage.connect() as conn:
        current_notes = conn.execute(
            "SELECT notes FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()[0]
    assert old in current_notes
    repeated = reviewer.review_and_notify(task["id"], dry_run=False)
    assert repeated["verdict"] == "unchanged"
    assert repeated["notification"] is None
    assert len(appended) == 1
    assert len(notified) == 1


def test_quoted_scout_ready_is_not_a_current_verdict(isolated_env, monkeypatch):
    storage.init_db()
    path = "00 - Inbox/Reusable report.md"
    body = f"report={path} decision inventory ready for captain — no PR/merge"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    notes = f'[task-shape: scout]\n[no-code]\n[scout: report={path}]\n[waiting-for: review]\n"[review: scout-ready @ old body-sha256={digest}] {body}"'
    task = tasks.task_add("Review quoted verdict", notes=notes)
    monkeypatch.setattr(
        "unified_agent.tools.vault.vault_read",
        lambda *args, **kwargs: {"content": "Invalid changed artifact", "truncated": False},
    )
    monkeypatch.setattr(reviewer, "_append_note", lambda *args: None)
    result = reviewer._review_scout_task(
        task["id"], "Changed scout report", notes, evidence_ref=f"vault:{path}", dry_run=False
    )
    assert result["verdict"] == "scout-incomplete"


@pytest.mark.parametrize(
    "read_error",
    [
        'Filesystem says "unavailable"',
        "file busy\nretry later",
        'file busy\r\nretry "later"',
        "file busy\n[review: scout-incomplete @ earlier] quoted error context",
    ],
)
def test_scout_read_error_notifies_once(isolated_env, monkeypatch, read_error):
    storage.init_db()
    path = '00 - Inbox/A "report".md'
    notes = f"[task-shape: scout]\n[no-code]\n[scout: report={path}]\n[waiting-for: review]"
    task = tasks.task_add("Quoted scout read failure", notes=notes)
    notified = []

    def unavailable(*args, **kwargs):
        raise RuntimeError(read_error)

    monkeypatch.setattr("unified_agent.tools.vault.vault_read", unavailable)
    monkeypatch.setattr(
        "unified_agent.tools.notify.notify",
        lambda message: notified.append(message) or {"delivered": True},
    )
    first = reviewer.review_and_notify(task["id"])
    assert first["verdict"] == "scout-incomplete"
    repeated = reviewer.review_and_notify(task["id"])
    assert repeated["verdict"] == "unchanged"
    assert repeated["notification"] is None
    assert len(notified) == 1


@pytest.mark.parametrize(
    "next_error",
    ["file gone\nrequest replacement", "file busy\nretry", "file busy"],
)
def test_scout_changed_multiline_error_notifies_once_for_each_reason(
    isolated_env, monkeypatch, next_error
):
    storage.init_db()
    path = "00 - Inbox/Unavailable report.md"
    notes = f"[task-shape: scout]\n[no-code]\n[scout: report={path}]\n[waiting-for: review]"
    task = tasks.task_add("Changing scout read failure", notes=notes)
    error = "file busy\nretry later"
    notified = []

    def unavailable(*args, **kwargs):
        raise RuntimeError(error)

    monkeypatch.setattr("unified_agent.tools.vault.vault_read", unavailable)
    monkeypatch.setattr(
        "unified_agent.tools.notify.notify",
        lambda message: notified.append(message) or {"delivered": True},
    )
    assert reviewer.review_and_notify(task["id"])["verdict"] == "scout-incomplete"
    error = next_error
    # A later unrelated note must not make the prior verdict match this reason.
    tasks.task_update(
        task["id"],
        append_note=f"Human debugging: could not read scout report {path!r}: {error}",
        worktree=False,
    )
    assert reviewer.review_and_notify(task["id"])["verdict"] == "scout-incomplete"
    assert reviewer.review_and_notify(task["id"])["verdict"] == "unchanged"
    assert len(notified) == 2


@pytest.mark.parametrize("prior", ["ready", "incomplete"])
def test_legacy_unkeyed_scout_verdict_is_validated_once(isolated_env, monkeypatch, prior):
    storage.init_db()
    path = "00 - Inbox/Legacy report.md"
    legacy = f"[review: scout-{prior} @ old] report={path} Historical prose"
    notes = (
        f"[task-shape: scout]\n[no-code]\n[scout: report={path}]\n[waiting-for: review]\n{legacy}"
    )
    task = tasks.task_add("Validate legacy scout verdict", notes=notes)
    report = build_scout_report(
        topic="Legacy report",
        bottom_line="Current content",
        findings=["checked"],
        decisions=[{"decision": "Ship?", "options": ["yes", "no"]}],
    )
    reads, notified = [], []

    def read(p, **kwargs):
        reads.append(p)
        return {"content": report, "truncated": False}

    monkeypatch.setattr("unified_agent.tools.vault.vault_read", read)
    monkeypatch.setattr(
        "unified_agent.tools.notify.notify",
        lambda message: notified.append(message) or {"delivered": True},
    )
    assert reviewer.review_and_notify(task["id"])["verdict"] == "scout-ready"
    assert reviewer.review_and_notify(task["id"])["verdict"] == "unchanged"
    assert reads == [path]
    assert len(notified) == 1
    with storage.connect() as conn:
        current = conn.execute("SELECT notes FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
    assert legacy in current
    assert "body-sha256=" in current
