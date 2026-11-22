"""J7 budget reservations survive retries, restarts, and outcome audit failures."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from unified_agent import llm_spend, storage


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_LLM_BROKER_DAILY_USD", "0.20")
    monkeypatch.setenv("UA_LLM_BROKER_RESERVE_USD", "0.10")
    storage.init_db()
    return storage


def test_reservations_are_durable_and_isolated_by_agent(ledger):
    first = llm_spend.reserve(org_id=None, agent="A", node="n1", model="m")
    second = llm_spend.reserve(org_id=None, agent="A", node="n2", model="m")
    assert first[0] != second[0]
    with pytest.raises(llm_spend.BudgetError, match="exhausted"):
        llm_spend.reserve(org_id=None, agent="A", node="n1", model="m")
    llm_spend.reserve(org_id=None, agent="B", node="n1", model="m")
    llm_spend.reserve(org_id="org2", agent="A", node="n1", model="m")


def test_concurrent_reservations_cannot_exceed_daily_cap(ledger):
    def call(_index):
        try:
            return llm_spend.reserve(org_id=None, agent="A", node="n", model="m")
        except llm_spend.BudgetError:
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(call, range(5)))
    assert sum(result is not None for result in results) == 2


def test_reported_cost_reconciles_and_unknown_cost_keeps_reserve(ledger):
    first = llm_spend.reserve(org_id=None, agent="A", node="n1", model="m")
    llm_spend.finish(
        first[0],
        first[1],
        agent="A",
        node="n1",
        model="m",
        outcome="completed",
        usage={"cost_usd": 0.02},
    )
    second = llm_spend.reserve(org_id=None, agent="A", node="n1", model="m")
    llm_spend.finish(
        second[0],
        second[1],
        agent="A",
        node="n1",
        model="m",
        outcome="transport_error",
        usage={"cost_usd": 0.001},
    )
    with pytest.raises(llm_spend.BudgetError, match="exhausted"):
        llm_spend.reserve(org_id=None, agent="A", node="n1", model="m")
    conn = ledger.connect()
    try:
        rows = conn.execute(
            "SELECT actual_usd, outcome FROM llm_broker_spend WHERE agent = 'A' ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    assert [(row["actual_usd"], row["outcome"]) for row in rows] == [
        (0.02, "completed"),
        (None, "transport_error"),
    ]


def test_failed_outcome_audit_keeps_full_reservation(ledger):
    attempt = llm_spend.reserve(org_id=None, agent="A", node="n1", model="m")
    conn = ledger.connect()
    try:
        conn.execute(
            "CREATE TRIGGER refuse_result BEFORE INSERT ON event_log "
            "WHEN NEW.channel = 'hub.llm.broker_result' BEGIN SELECT RAISE(FAIL, 'audit unavailable'); END"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(Exception, match="audit unavailable"):
        llm_spend.finish(
            attempt[0],
            attempt[1],
            agent="A",
            node="n1",
            model="m",
            outcome="completed",
            usage={"cost_usd": 0.01},
        )
    conn = ledger.connect()
    try:
        row = conn.execute(
            "SELECT actual_usd, outcome FROM llm_broker_spend WHERE request_id = ?", (attempt[0],)
        ).fetchone()
    finally:
        conn.close()
    assert row["actual_usd"] is None and row["outcome"] is None
