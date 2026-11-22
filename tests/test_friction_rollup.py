"""Friction rollup and verdict lifecycle (task #2399).

Reports rank by severity, volume and spread; session-end nudges ingest once.
A key closes only by a quiet recheck after a fix claim, or by a human accept;
a report after the claim (or after a plain close) reopens it, an accepted key
stays accepted. Reads and writes are fenced to the caller's tenant.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, friction_rollup, storage
from unified_agent.tool_errors import PermissionDeniedError

OPERATOR = "Desktop_Operator"
DEV = "Desktop_DevA"
OTHER = "Desktop_DevB"


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _schema(monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_LABEL", OPERATOR)
    monkeypatch.delenv("UA_OPERATOR_ALIASES", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    conn = storage.connect()
    try:
        friction_rollup.ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _report(key: str, sev: str = "med", who: str = DEV, **kw) -> dict:
    with _as(who):
        return friction_rollup.report(key, sev, kw.pop("summary", f"{key} hurt"), **kw)


def _events() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ROWID",
            (friction_rollup.CHANNEL,),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(r["payload"]) for r in rows]


def _later(days: float) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _session(sid: str, agent: str, score: int, reasons: list[str]) -> None:
    stamp = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_sessions (id, agent, kind, status, started_at, "
            "last_heartbeat_at, org_id) VALUES (?, ?, 'cli', 'completed', ?, ?, ?)",
            (sid, agent, stamp, stamp, friction_rollup.edition.tenant_org_id()),
        )
        conn.execute(
            "INSERT INTO session_friction_nudges (session_id, score, reasons, nudged_at) "
            "VALUES (?, ?, ?, ?)",
            (sid, score, json.dumps(reasons), stamp),
        )
        conn.commit()
    finally:
        conn.close()


def test_rollup_ranks_by_severity_volume_and_spread():
    _report("tool:run_cmd:timeout", "high")
    for _ in range(4):
        _report("test:flaky", "low")
    _report("path:denied", "med", who=DEV)
    _report("path:denied", "med", who=OTHER)
    with _as(OPERATOR):
        friction_rollup.report("path:denied", "med", "denied again", channel="operator")
    items = friction_rollup.rollup()["items"]
    assert [i["key"] for i in items] == ["path:denied", "tool:run_cmd:timeout", "test:flaky"]
    top = items[0]
    assert (top["count"], top["channels"], top["reporters"]) == (3, 2, 3)
    assert top["score"] == pytest.approx(3 * 2 * 1.5 * 1.5)
    assert top["latest_summaries"][0] == "denied again"
    assert top["verdict"] == {"state": "open"}
    assert items[2]["score"] == pytest.approx(2.322, abs=1e-3)


def test_rollup_window_limit_and_validation():
    _report("a:one")
    _report("b:two")
    out = friction_rollup.friction_rollup(window_days=7, limit=1)
    assert out["total"] == 2 and out["truncated"] and len(out["items"]) == 1
    with pytest.raises(ToolError, match="window_days"):
        friction_rollup.friction_rollup(window_days=0)
    with pytest.raises(ToolError, match="invalid friction key"):
        _report("Bad Key!")
    with pytest.raises(ToolError, match="severity"):
        _report("a:one", "urgent")
    with pytest.raises(ToolError, match="at most 300"):
        _report("a:one", summary="x" * 301)


def test_reporter_spoof_refused_but_human_may_file_for_others():
    with _as(DEV), pytest.raises(PermissionDeniedError, match="may not report"):
        friction_rollup.report("a:one", "low", "s", reporter=OTHER)
    with _as(OPERATOR):
        out = friction_rollup.report("a:one", "low", "s", reporter=OTHER)
    assert out["reporter"] == OTHER
    with pytest.raises(ToolError, match="reporter is required"):
        friction_rollup.report("a:one", "low", "s")
    with _as(DEV), pytest.raises(PermissionDeniedError, match="operator channel"):
        friction_rollup.friction_report("a:one", "low", "s", channel="operator")
    with _as(DEV), pytest.raises(ToolError, match="session_end comes from ingest"):
        friction_rollup.friction_report("a:one", "low", "s", channel="session_end")


def test_summary_is_redacted():
    secret = "sk-ant-api03-" + "A" * 90
    out = _report("a:one", summary=f"leaked {secret} in a log")
    conn = storage.connect()
    try:
        stored = conn.execute(
            "SELECT summary FROM friction_reports WHERE id=?", (out["id"],)
        ).fetchone()["summary"]
    finally:
        conn.close()
    assert secret not in stored


def test_source_ref_makes_report_idempotent():
    first = _report("a:one", source_ref="ci:run:1")
    again = _report("a:one", source_ref="ci:run:1")
    assert first["duplicate"] is False and again["duplicate"] is True
    assert friction_rollup.rollup()["items"][0]["count"] == 1


def test_ingest_session_nudges_is_idempotent_and_maps_reasons(monkeypatch):
    monkeypatch.setenv("UA_FRICTION_THRESHOLD", "20")
    _session(
        "sess-1",
        DEV,
        60,
        ["you interrupted the agent twice", "you denied 1 tool call", "something new"],
    )
    _session("sess-2", OTHER, 20, ["the agent retried failing tools 3 times"])
    first = friction_rollup.ingest_session_nudges()
    assert first == {"sessions": 2, "inserted": 4, "already_ingested": 0, "backlog": 0}
    # A session already turned into reports is not read again.
    again = friction_rollup.ingest_session_nudges()
    assert again == {"sessions": 0, "inserted": 0, "already_ingested": 0, "backlog": 0}
    items = {i["key"]: i for i in friction_rollup.rollup()["items"]}
    assert set(items) == {
        "session:interrupts",
        "session:denied_tools",
        "session:other",
        "session:tool_retries",
    }
    assert items["session:interrupts"]["max_severity"] == "high"
    assert items["session:tool_retries"]["max_severity"] == "med"
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT reporter, channel, source_ref FROM friction_reports "
            "WHERE key='session:tool_retries'"
        ).fetchone()
    finally:
        conn.close()
    assert (row["reporter"], row["channel"], row["source_ref"]) == (
        OTHER,
        "session_end",
        "sess-2:session:tool_retries",
    )


def _burst(count: int, reason: str = "you corrected the agent once") -> None:
    """``count`` sessions that each ended with one friction signal, a second apart."""
    start = datetime.now(UTC) - timedelta(hours=1)
    tenant = friction_rollup.edition.tenant_org_id()
    conn = storage.connect()
    try:
        for n in range(count):
            sid, stamp = f"burst-{n:04d}", (start + timedelta(seconds=n)).isoformat()
            conn.execute(
                "INSERT INTO agent_sessions (id, agent, kind, status, started_at, "
                "last_heartbeat_at, org_id) VALUES (?, ?, 'cli', 'completed', ?, ?, ?)",
                (sid, DEV, stamp, stamp, tenant),
            )
            conn.execute(
                "INSERT INTO session_friction_nudges (session_id, score, reasons, nudged_at) "
                "VALUES (?, 40, ?, ?)",
                (sid, json.dumps([reason]), stamp),
            )
        conn.commit()
    finally:
        conn.close()


def _ingested() -> set[str]:
    conn = storage.connect()
    try:
        rows = conn.execute("SELECT source_ref FROM friction_reports WHERE channel='session_end'")
        return {r["source_ref"].split(":", 1)[0] for r in rows}
    finally:
        conn.close()


@pytest.mark.parametrize("count", [201, 450])
def test_a_burst_larger_than_the_cap_drains_oldest_first(count):
    """The cap limits reports written, not rows read: no session waits behind newer ones."""
    _burst(count)
    seen: list[dict] = []
    while not seen or seen[-1]["inserted"]:
        seen.append(friction_rollup.ingest_session_nudges(cap=200))
        if len(seen) == 1:
            # The oldest session is in the first pass, the newest ones wait.
            assert "burst-0000" in _ingested() and f"burst-{count - 1:04d}" not in _ingested()
    assert [s["inserted"] for s in seen] == [*[200] * (count // 200), count % 200, 0]
    assert [s["backlog"] for s in seen][:2] == [count - 200, max(0, count - 400)]
    assert len(_ingested()) == count and seen[-1]["backlog"] == 0


def test_a_fix_claim_is_not_closed_while_a_recurrence_is_still_unread():
    """Quiet means every signal was read: an unread session may be the recurrence."""
    _report("session:corrections")
    with _as(DEV):
        friction_rollup.claim_fixed("session:corrections", "pr#12", recheck_days=1)
    _burst(3)
    assert friction_rollup.ingest_session_nudges(cap=1)["backlog"] == 2
    # The one session read so far reopened its key at once. A second key, due
    # and with no report since its claim, must wait for the two unread sessions.
    states = {i["key"]: i["verdict"]["state"] for i in friction_rollup.rollup()["items"]}
    assert states == {"session:corrections": "open"}
    _report("b:two")
    with _as(DEV):
        friction_rollup.claim_fixed("b:two", "pr#13", recheck_days=1)
    waiting = friction_rollup.recheck(None, now=_later(2))
    assert waiting == {"closed": [], "reopened": [], "pending": ["b:two"], "unread_sessions": 2}
    while friction_rollup.ingest_session_nudges(cap=1)["inserted"]:
        pass
    assert friction_rollup.recheck(None, now=_later(2)) == {
        "closed": ["b:two"],
        "reopened": [],
        "pending": [],
    }


_FOUR_SIGNALS = [
    "you interrupted the agent twice",
    "you denied 1 tool call",
    "you corrected the agent once",
    "the agent retried failing tools 3 times",
]


@pytest.mark.parametrize("one_session", [True, False], ids=["one-session", "many-sessions"])
@pytest.mark.parametrize("reports", [3, 4], ids=["at-cap", "cap-plus-one"])
def test_the_cap_bounds_reports_not_sessions(one_session, reports):
    """``cap`` limits reports written; a session cut by it finishes on the next pass."""
    signals = _FOUR_SIGNALS[:reports]
    if one_session:
        _session("multi", DEV, 40, signals)
    else:
        for n, signal in enumerate(signals):
            _session(f"single-{n}", DEV, 40, [signal])
    first = friction_rollup.ingest_session_nudges(cap=3)
    assert (first["inserted"], first["backlog"]) == (3, reports - 3)
    conn = storage.connect()
    try:
        assert friction_rollup._unread_sessions(conn) == reports - 3
    finally:
        conn.close()
    second = friction_rollup.ingest_session_nudges(cap=3)
    assert (second["inserted"], second["backlog"]) == (reports - 3, 0)
    assert friction_rollup.ingest_session_nudges(cap=3)["inserted"] == 0
    assert len(friction_rollup.rollup()["items"]) == reports


def test_ingest_skips_nudge_without_a_session():
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO session_friction_nudges (session_id, score, reasons, nudged_at) "
            "VALUES ('ghost', 40, '[\"you corrected the agent once\"]', ?)",
            (storage.now(),),
        )
        conn.commit()
    finally:
        conn.close()
    assert friction_rollup.ingest_session_nudges()["inserted"] == 0


def test_fix_claim_needs_evidence_and_reports():
    with _as(DEV), pytest.raises(ToolError, match="no friction reports"):
        friction_rollup.claim_fixed("a:one", "pr#1")
    _report("a:one")
    with _as(DEV), pytest.raises(ToolError, match="evidence_ref is required"):
        friction_rollup.friction_claim_fixed("a:one", "  ")
    with _as(DEV):
        out = friction_rollup.friction_claim_fixed("a:one", "pr#12", "bumped timeout")
    assert out["verdict"]["state"] == "fix_claimed"
    assert out["verdict"]["evidence_ref"] == "pr#12"
    with _as(OTHER), pytest.raises(ToolError, match="not open"):
        friction_rollup.claim_fixed("a:one", "pr#13")


def test_recheck_closes_only_after_window_without_new_reports():
    _report("a:one")
    with _as(DEV):
        friction_rollup.claim_fixed("a:one", "pr#12", recheck_days=3)
    assert friction_rollup.recheck("a:one") == {"closed": [], "reopened": [], "pending": ["a:one"]}
    assert friction_rollup.recheck(None, now=_later(2))["pending"] == ["a:one"]
    out = friction_rollup.recheck(None, now=_later(3.1))
    assert out == {"closed": ["a:one"], "reopened": [], "pending": []}
    assert friction_rollup.rollup()["items"] == []
    closed = friction_rollup.rollup(include_closed=True)["items"][0]
    assert closed["verdict"]["state"] == "closed"
    assert [(e["from"], e["to"]) for e in _events()] == [
        ("open", "fix_claimed"),
        ("fix_claimed", "closed"),
    ]
    with pytest.raises(ToolError, match="not fix_claimed"):
        friction_rollup.recheck("a:one")


def test_report_after_fix_claim_reopens_at_once():
    _report("a:one")
    with _as(DEV):
        friction_rollup.claim_fixed("a:one", "pr#12")
    out = _report("a:one", who=OTHER)
    assert out["verdict"]["state"] == "open"
    assert out["verdict"]["note"] == "reopened by 1 report(s) after the fix claim"
    assert out["verdict"]["evidence_ref"] == "pr#12"
    assert friction_rollup.recheck(None, now=_later(10)) == {
        "closed": [],
        "reopened": [],
        "pending": [],
    }


def test_recheck_reopens_on_a_report_that_bypassed_report():
    _report("a:one")
    with _as(DEV):
        friction_rollup.claim_fixed("a:one", "pr#12")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO friction_reports (key, severity, channel, reporter, summary, "
            "created_at) VALUES ('a:one', 'low', 'report', ?, 's', ?)",
            (OTHER, _later(0.01).isoformat(timespec="microseconds")),
        )
        conn.commit()
    finally:
        conn.close()
    out = friction_rollup.recheck(None, now=_later(5))
    assert out["reopened"] == ["a:one"] and out["closed"] == []


def test_report_on_closed_key_reopens_it():
    _report("a:one")
    with _as(DEV):
        friction_rollup.claim_fixed("a:one", "pr#12", recheck_days=1)
    friction_rollup.recheck(None, now=_later(2))
    out = _report("a:one")
    assert out["verdict"]["state"] == "open"
    assert "after it closed" in out["verdict"]["note"]


def test_accept_is_human_only_and_accepted_key_stays_closed():
    _report("a:one")
    with _as(DEV), pytest.raises(PermissionDeniedError, match="only the human operator"):
        friction_rollup.friction_accept("a:one", "known cost")
    with pytest.raises(PermissionDeniedError):
        friction_rollup.accept("a:one", "unverified caller")
    with _as(OPERATOR):
        with pytest.raises(ToolError, match="note"):
            friction_rollup.accept("a:one", " ")
        out = friction_rollup.friction_accept("a:one", "upstream limit, living with it")
    assert out["verdict"]["state"] == "accepted"
    again = _report("a:one", who=OTHER)
    assert again["verdict"]["state"] == "accepted"
    assert friction_rollup.rollup()["items"] == []
    accepted = friction_rollup.rollup(include_closed=True)["items"][0]
    assert accepted["count"] == 2
    with _as(DEV), pytest.raises(ToolError, match="not open"):
        friction_rollup.claim_fixed("a:one", "pr#1")


def test_recheck_sweep_handles_many_keys():
    for key in ("a:one", "b:two", "c:three"):
        _report(key)
        with _as(DEV):
            friction_rollup.claim_fixed(key, "pr#1", recheck_days=1 if key != "c:three" else 5)
    assert _report("b:two")["verdict"]["state"] == "open"
    out = friction_rollup.friction_recheck()
    assert out["pending"] == ["a:one", "c:three"] and out["closed"] == []
    out = friction_rollup.recheck(None, now=_later(2))
    assert out == {"closed": ["a:one"], "reopened": [], "pending": ["c:three"]}


def test_friction_is_fenced_to_the_callers_tenant(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_one")
    _report("a:one")
    _session("sess-1", DEV, 40, ["you corrected the agent once"])
    with _as(DEV):
        friction_rollup.claim_fixed("a:one", "pr#1")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_two")
    assert friction_rollup.rollup(include_closed=True)["items"] == []
    assert friction_rollup.ingest_session_nudges()["sessions"] == 0
    assert friction_rollup.recheck(None, now=_later(10))["closed"] == []
    with _as(DEV), pytest.raises(ToolError, match="no friction reports"):
        friction_rollup.claim_fixed("a:one", "pr#2")
    with pytest.raises(ToolError, match="not fix_claimed"):
        friction_rollup.recheck("a:one")
    # Another tenant's unread session does not hold this tenant's claim open.
    _report("b:two")
    with _as(DEV):
        friction_rollup.claim_fixed("b:two", "pr#3", recheck_days=1)
    assert friction_rollup.recheck("b:two", now=_later(2))["closed"] == ["b:two"]
    _report("a:one")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_one")
    items = friction_rollup.rollup()["items"]
    assert [(i["key"], i["count"], i["verdict"]["state"]) for i in items] == [
        ("a:one", 1, "fix_claimed")
    ]
    assert friction_rollup.ingest_session_nudges()["inserted"] == 1
