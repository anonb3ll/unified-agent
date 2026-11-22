"""Stored next-move ownership and safe_wait (task #2388).

Ground truth:
- the columns arrive additively and the backfill belongs to that one migration;
- safe_wait is decided from the row and the wait info, so a stale next_owner
  can never wedge a task;
- only the owner, the lane agent when nobody holds the task, a lifecycle
  operator or the human operator may change ownership.
"""

from __future__ import annotations

import contextlib

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage, task_ownership
from unified_agent.tools.tasks import task_add, task_claim, task_list, task_update

AGENT = "Desktop_Agent2"
OTHER = "server_agent2"


def _operator() -> str:
    return config.operator_label()


@contextlib.contextmanager
def _verified(agent: str):
    reset = config.agent_identity_var.set(agent)
    try:
        yield
    finally:
        config.agent_identity_var.reset(reset)


def _row(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def _stalled_task(title: str = "Stalled") -> dict:
    return task_add(title, lane=AGENT)


OWNERSHIP_COLUMNS = ("next_owner", "waiting_on", "terminal_predicate", "ownership_generation")
OLD_STAMP = "2026-10-01T00:00:00+00:00"


def _ownership_columns() -> dict[str, tuple[str, int, str | None]]:
    conn = storage.connect()
    try:
        return {
            r["name"]: (r["type"], r["notnull"], r["dflt_value"])
            for r in conn.execute("PRAGMA table_info(tasks)")
            if r["name"] in OWNERSHIP_COLUMNS
        }
    finally:
        conn.close()


def _drop_ownership_columns() -> None:
    """Return a real store to its shape from before these columns existed."""
    conn = storage.connect()
    try:
        for column in OWNERSHIP_COLUMNS:
            conn.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
        conn.commit()
    finally:
        conn.close()


def _seed_task(status: str, claimed_by: str | None) -> int:
    conn = storage.connect()
    try:
        task_id = conn.execute(
            "INSERT INTO tasks (title, status, claimed_by, created_at, updated_at) "
            "VALUES ('seed', ?, ?, ?, ?)",
            (status, claimed_by, OLD_STAMP, OLD_STAMP),
        ).lastrowid
        conn.commit()
        return int(task_id)
    finally:
        conn.close()


def _set_task(task_id: int, **columns) -> None:
    assignments = ", ".join(f"{name}=?" for name in columns)
    conn = storage.connect()
    try:
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id=?", (*columns.values(), task_id))
        conn.commit()
    finally:
        conn.close()


def test_migration_adds_ownership_columns_and_is_idempotent():
    _drop_ownership_columns()
    assert _ownership_columns() == {}
    expected = {
        "next_owner": ("TEXT", 0, None),
        "waiting_on": ("TEXT", 0, None),
        "terminal_predicate": ("TEXT", 0, None),
        "ownership_generation": ("INTEGER", 1, "0"),
    }
    storage.init_db()
    assert _ownership_columns() == expected
    storage.init_db()
    assert _ownership_columns() == expected
    assert storage.get_schema_version() == storage.SCHEMA_VERSION


def test_fresh_store_has_the_same_ownership_columns_as_a_migrated_one():
    fresh = _ownership_columns()
    _drop_ownership_columns()
    storage.init_db()
    assert _ownership_columns() == fresh != {}


def test_backfill_sets_next_owner_only_for_claimed_doing_rows():
    ids = {
        "doing_claimed": _seed_task("doing", AGENT),
        "doing_unclaimed": _seed_task("doing", None),
        "doing_blank": _seed_task("doing", "   "),
        "open_claimed": _seed_task("open", AGENT),
        "review_claimed": _seed_task("waiting_review", AGENT),
        "blocked_claimed": _seed_task("blocked", AGENT),
        "done_claimed": _seed_task("done", AGENT),
    }
    _drop_ownership_columns()
    storage.init_db()
    owners = {name: _row(task_id)["next_owner"] for name, task_id in ids.items()}
    assert owners == {
        "doing_claimed": AGENT,
        "doing_unclaimed": None,
        "doing_blank": None,
        "open_claimed": None,
        "review_claimed": None,
        "blocked_claimed": None,
        "done_claimed": None,
    }
    # The backfill is not a task edit: revision fences and digests key on this.
    assert {_row(task_id)["updated_at"] for task_id in ids.values()} == {OLD_STAMP}
    assert {_row(task_id)["ownership_generation"] for task_id in ids.values()} == {0}


def test_backfill_rerun_never_overwrites_a_stored_owner():
    backfilled = _seed_task("doing", AGENT)
    preset = _seed_task("doing", AGENT)
    _drop_ownership_columns()
    storage.init_db()
    # The claim moved on after the first run; a stored value stays as it was.
    _set_task(backfilled, claimed_by=OTHER)
    _set_task(preset, next_owner="someone_else")
    storage.init_db()
    assert _row(backfilled)["next_owner"] == AGENT
    assert _row(preset)["next_owner"] == "someone_else"


def test_ownership_read_without_stored_columns_is_the_derivation():
    assert task_ownership.read({"claimed_by": f" {AGENT} "}) == task_ownership.Ownership(
        claimant=AGENT,
        next_owner=None,
        waiting_on=None,
        terminal_predicate=None,
        generation=0,
    )
    assert task_ownership.read({"claimed_by": None}).claimant == ""


def test_ownership_read_returns_stored_values_from_a_real_row():
    task_id = _seed_task("doing", AGENT)
    _set_task(
        task_id,
        next_owner=OTHER,
        waiting_on="ci",
        terminal_predicate="merged",
        ownership_generation=4,
    )
    assert task_ownership.read(_row(task_id)) == task_ownership.Ownership(
        claimant=AGENT,
        next_owner=OTHER,
        waiting_on="ci",
        terminal_predicate="merged",
        generation=4,
    )


def test_ownership_read_treats_blank_stored_text_as_absent():
    record = task_ownership.read(
        {"claimed_by": "  ", "next_owner": " ", "waiting_on": "", "ownership_generation": None}
    )
    assert record == task_ownership.Ownership(
        claimant="", next_owner=None, waiting_on=None, terminal_predicate=None, generation=0
    )


def test_ownership_columns_appear_in_task_responses():
    added = task_add("Response shape", lane=AGENT)
    claimed = task_claim(added["id"], agent=AGENT)
    updated = task_update(added["id"], waiting_on="ci", actor=AGENT)
    listed = [
        *task_list(status="doing", fields="full")["tasks"],
        *task_list(status="doing", fields="compact")["tasks"],
    ]
    for response in (claimed, updated, *listed):
        assert set(OWNERSHIP_COLUMNS) <= set(response)
    assert updated["next_owner"] == AGENT and updated["waiting_on"] == "ci"
    assert all(row["ownership_generation"] == 1 for row in listed)


def test_a_cleared_owner_stays_cleared_on_later_migrations():
    """The backfill belongs to the migration that adds the columns, and no other."""
    task_id = _seed_task("doing", AGENT)
    _drop_ownership_columns()
    storage.init_db()
    assert _row(task_id)["next_owner"] == AGENT
    _set_task(task_id, next_owner=None)
    storage.init_db()
    assert _row(task_id)["next_owner"] is None


# ── safe_wait decision table (#2388) ─────────────────────────────────


AT = "2026-10-09T12:00:00+00:00"
LATER = "2026-10-09T13:00:00+00:00"
EARLIER = "2026-10-09T11:00:00+00:00"
ME = "Desktop_Agent1"


def _wait(owner, until, *, waiting_on=None, generation=2, claimed_by=None):
    return {
        "owner": owner,
        "waiting_on": waiting_on,
        "until": until,
        "generation": generation,
        "claimed_by": claimed_by,
    }


SAFE_WAIT_CASES = [
    pytest.param(
        {"claimed_by": AGENT, "lock_expires_at": LATER, "ownership_generation": 2},
        {},
        _wait(AGENT, LATER, claimed_by=AGENT),
        id="other-agent-lease",
    ),
    pytest.param(
        {
            "claimed_by": AGENT,
            "lock_expires_at": LATER,
            "next_owner": OTHER,
            "waiting_on": "ci",
            "ownership_generation": 2,
        },
        {},
        _wait(OTHER, LATER, waiting_on="ci", claimed_by=AGENT),
        id="lease-reports-the-stored-owner",
    ),
    pytest.param(
        {"claimed_by": AGENT, "lock_expires_at": None, "ownership_generation": 2},
        {},
        _wait(AGENT, None, claimed_by=AGENT),
        id="lease-without-expiry",
    ),
    pytest.param(
        {"claimed_by": ME, "lock_expires_at": LATER, "next_owner": AGENT},
        {"wait_until": LATER},
        None,
        id="my-own-lease-is-a-renewal",
    ),
    pytest.param(
        {"claimed_by": AGENT, "lock_expires_at": AT},
        {},
        None,
        id="lease-is-expired-at-its-expiry-instant",
    ),
    pytest.param(
        {"claimed_by": AGENT, "lock_expires_at": EARLIER},
        {},
        None,
        id="lapsed-lease-no-owner-no-wait",
    ),
    pytest.param(
        {"next_owner": AGENT},
        {},
        None,
        id="stored-owner-without-a-wait-is-not-live",
    ),
    pytest.param(
        {"next_owner": AGENT, "ownership_generation": 2},
        {"wait_until": LATER},
        _wait(AGENT, LATER),
        id="stored-owner-with-a-declared-wait",
    ),
    pytest.param(
        {"next_owner": AGENT, "ownership_generation": 2},
        {"monitor_at": LATER},
        _wait(AGENT, LATER),
        id="stored-owner-with-a-pending-monitor",
    ),
    pytest.param(
        {"next_owner": AGENT, "ownership_generation": 2},
        {"wait_until": EARLIER, "monitor_at": LATER},
        _wait(AGENT, LATER),
        id="expired-wait-falls-through-to-the-monitor",
    ),
    pytest.param(
        {"next_owner": AGENT, "ownership_generation": 2},
        {"wait_until": LATER, "monitor_at": "2026-10-09T12:30:00+00:00"},
        _wait(AGENT, LATER),
        id="declared-wait-wins-over-the-monitor",
    ),
    pytest.param(
        {"next_owner": AGENT},
        {"wait_until": AT},
        None,
        id="wait-ending-now-is-over",
    ),
    pytest.param(
        {"next_owner": AGENT},
        {"wait_until": EARLIER, "monitor_at": EARLIER},
        None,
        id="both-waits-past",
    ),
    pytest.param(
        {"next_owner": ME},
        {"wait_until": LATER},
        None,
        id="caller-is-the-stored-owner",
    ),
    pytest.param(
        {
            "claimed_by": AGENT,
            "lock_expires_at": EARLIER,
            "next_owner": AGENT,
            "ownership_generation": 2,
        },
        {"wait_until": LATER},
        _wait(AGENT, LATER, claimed_by=AGENT),
        id="lapsed-lease-still-owned-through-its-wait",
    ),
]


@pytest.mark.parametrize(("task", "wait_info", "expected"), SAFE_WAIT_CASES)
def test_safe_wait_decision(task, wait_info, expected):
    assert task_ownership.safe_wait(task, ME, now=AT, **wait_info) == expected


def test_is_safe_wait_recognises_only_the_non_claim_result():
    assert task_ownership.is_safe_wait({"id": 1, "claimed": False, "safe_wait": {"owner": "a"}})
    assert not task_ownership.is_safe_wait({"id": 1, "status": "doing"})
    assert not task_ownership.is_safe_wait(None)


# ── ownership write path (#2388) ─────────────────────────────────────


def _ownership(task_id: int) -> tuple:
    row = _row(task_id)
    return row["next_owner"], row["waiting_on"], row["ownership_generation"]


def _owned_task() -> int:
    task = _stalled_task("Owned")
    task_claim(task["id"], agent=AGENT, timeout_seconds=3600, expected_lane=AGENT)
    return task["id"]


def test_the_owner_may_hand_off_and_the_generation_counts_it():
    task_id = _owned_task()
    res = task_update(task_id, next_owner=OTHER, waiting_on="review", actor=AGENT)
    assert (res["next_owner"], res["waiting_on"], res["ownership_generation"]) == (
        OTHER,
        "review",
        2,
    )
    # The claimant keeps its say while it holds the lease, and so does the new owner.
    task_update(task_id, waiting_on="ci", actor=OTHER)
    assert _ownership(task_id) == (OTHER, "ci", 2)


def test_a_handoff_by_a_non_owner_is_refused_and_writes_nothing():
    task_id = _owned_task()
    before = tuple(_row(task_id))
    with pytest.raises(ToolError, match="may not change this task's ownership"):
        task_update(task_id, next_owner=OTHER, actor="Desktop_Agent4")
    assert tuple(_row(task_id)) == before


@pytest.mark.parametrize("field", ["waiting_on", "terminal_predicate"])
def test_every_ownership_field_is_guarded(field):
    task_id = _owned_task()
    with pytest.raises(ToolError, match="may not change this task's ownership"):
        task_update(task_id, actor="Desktop_Agent4", **{field: "x"})


def test_lane_agent_may_set_ownership_when_nobody_holds_the_task():
    task = _stalled_task("Unheld")
    task_update(task["id"], next_owner=OTHER, actor=AGENT)
    assert _ownership(task["id"]) == (OTHER, None, 1)


def test_shared_pool_task_has_no_ownership_authority_but_operators():
    task = task_add("Pool")
    with pytest.raises(ToolError, match="may not change this task's ownership"):
        task_update(task["id"], next_owner=OTHER, actor=AGENT)
    task_update(task["id"], next_owner=OTHER, actor=_operator())
    assert _ownership(task["id"])[0] == OTHER


def test_a_self_pickup_agent_is_not_an_ownership_authority():
    """The liveness rule admits the run's agent; the ownership rule does not."""
    task = task_add("Unclaimed doing")
    _set_task(task["id"], status="doing", current_run_agent=AGENT)
    with pytest.raises(ToolError, match="may not change this task's ownership"):
        task_update(task["id"], next_owner=OTHER, actor=AGENT)


def test_lifecycle_operator_may_change_ownership(monkeypatch):
    task_id = _owned_task()
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Agent1")
    task_update(task_id, next_owner=OTHER, actor="Desktop_Agent1")
    assert _ownership(task_id)[0] == OTHER


def test_an_unknown_next_owner_is_refused():
    task_id = _owned_task()
    with pytest.raises(ToolError, match="next_owner"):
        task_update(task_id, next_owner="not a label!", actor=AGENT)


@pytest.mark.parametrize("field", ["waiting_on", "terminal_predicate"])
def test_ownership_text_is_capped(field):
    task_id = _owned_task()
    with pytest.raises(ToolError, match=f"{field} must be at most 500"):
        task_update(task_id, actor=AGENT, **{field: "x" * 501})
    task_update(task_id, actor=AGENT, **{field: "x" * 500})


def test_empty_string_clears_and_only_an_owner_change_bumps_the_generation():
    task_id = _owned_task()
    task_update(task_id, waiting_on="ci", terminal_predicate="merged", actor=AGENT)
    assert _ownership(task_id) == (AGENT, "ci", 1)
    task_update(task_id, next_owner=AGENT, actor=AGENT)
    assert _ownership(task_id)[2] == 1

    task_update(task_id, next_owner="", waiting_on="", terminal_predicate="", actor=AGENT)
    assert _ownership(task_id) == (None, None, 2)
    assert _row(task_id)["terminal_predicate"] is None


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_closing_a_task_clears_the_owner(status):
    task_id = _owned_task()
    task_update(task_id, status=status, actor=AGENT, evidence_ref="sha")
    assert _ownership(task_id)[0] is None
    assert _ownership(task_id)[2] == 2


def test_releasing_a_claim_clears_the_owner():
    task_id = _owned_task()
    with _verified(AGENT):
        task_update(task_id, release_claim=True, status="open")
    assert _ownership(task_id)[0] is None
    assert _ownership(task_id)[2] == 2


def test_an_ordinary_update_leaves_ownership_alone():
    task_id = _owned_task()
    task_update(task_id, priority="high", status="blocked", actor=AGENT)
    assert _ownership(task_id) == (AGENT, None, 1)


def test_a_new_owner_cannot_be_named_on_the_call_that_ends_ownership():
    task_id = _owned_task()
    with pytest.raises(ToolError, match="next_owner cannot be set"):
        task_update(task_id, status="done", next_owner=OTHER, actor=AGENT, evidence_ref="sha")
    assert _ownership(task_id) == (AGENT, None, 1)


@pytest.mark.parametrize(
    ("status", "owner", "lease", "hold", "run", "expected"),
    [
        pytest.param("doing", "A", "live", "live", "open", task_ownership.SLEEPING, id="sleeping"),
        pytest.param("open", "A", "none", "live", "none", task_ownership.SLEEPING, id="sleep-open"),
        pytest.param("doing", "A", "live", "none", "ended", task_ownership.ZOMBIE, id="zombie"),
        pytest.param("doing", "A", "lapsed", "none", "open", task_ownership.ZOMBIE, id="run-only"),
        pytest.param("doing", "A", "lapsed", "none", "none", task_ownership.DEAD, id="dead"),
        pytest.param("doing", None, "none", "none", "none", task_ownership.DEAD, id="dead-unowned"),
        pytest.param("open", "A", "none", "expired", "none", task_ownership.SHELVED, id="shelved"),
        pytest.param("open", "A", "none", "none", "none", task_ownership.VOID, id="void"),
        pytest.param("doing", "A", "live", "none", "open", None, id="healthy-claim"),
        pytest.param("open", None, "none", "none", "none", None, id="unowned-backlog"),
    ],
)
def test_stuck_work_table(status, owner, lease, hold, run, expected):
    facts = task_ownership.StuckFacts(status, owner, lease, hold, run)
    assert task_ownership.classify(facts) == expected
