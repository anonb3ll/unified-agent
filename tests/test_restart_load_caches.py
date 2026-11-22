"""Restart-load guards (task #1850).

After a hub restart the Control Center polled ``system_doctor`` about every
seven seconds. Each run probed every service unit, the executor, a KNN query and
every LLM leg (ollama on localhost about once a second), and the doctor's own
``init_db()`` restarted every embedding backfill scan whenever the last one had
finished. These tests pin the guards:

- one shared doctor report per ``UA_DOCTOR_CACHE_S`` window, labelled as cached;
- past that window, stale-while-revalidate up to ``UA_DOCTOR_MAX_STALE_S`` with
  one background run (task #1897);
- LLM leg probes reused for ``UA_LLM_PROBE_CACHE_S``, failures included;
- the doctor initializes the store once per process, not once per run;
- the memory_vec mirror backfill finds the missing rows with one set-based
  query on one connection, not a vec0 lookup and a connection per row.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest

from unified_agent import config, embeddings, llm_client, memory_semantic, storage
from unified_agent.tools import doctor, indexer


class _ManualClock:
    """A module's ``time``, with ``monotonic`` advanced by hand instead of by
    sleeping. Every other attribute is the real ``time`` module."""

    def __init__(self) -> None:
        self.now = time.monotonic()

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch):
    """The doctor's clock; ``clock.advance(s)`` ages the shared report by ``s``."""
    manual = _ManualClock()
    monkeypatch.setattr(doctor, "time", manual)
    return manual


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.002)


@pytest.fixture
def run_waiters(monkeypatch):
    """Count callers parked on a shared doctor run, so a test releases its gate
    once they all wait rather than after a fixed sleep."""
    waiting: list[int] = []

    class _CountedEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.append(1)
            return super().wait(timeout)

    real_run = doctor._DoctorRun

    class _CountedRun(real_run):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.done = _CountedEvent()

    monkeypatch.setattr(doctor, "_DoctorRun", _CountedRun)
    return waiting


def _in_thread(fn) -> concurrent.futures.Future:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn)
    pool.shutdown(wait=False)
    return future


# --- system_doctor shared report -------------------------------------------------


@pytest.fixture
def counted_doctor(monkeypatch):
    """Replace the probe run with a counter; every run returns a new report."""
    runs: list[int] = []

    def _fake_run() -> dict:
        runs.append(1)
        return {"checks": [{"capability": "x", "status": "LIVE"}], "summary": {"run": len(runs)}}

    monkeypatch.setattr(doctor, "_run_system_doctor", _fake_run)
    doctor.clear_report_cache()
    yield runs
    doctor.wait_for_report_refresh(timeout=5)
    doctor.clear_report_cache()


def test_two_calls_within_the_ttl_run_the_probes_once(monkeypatch, counted_doctor):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")

    first = doctor.system_doctor()
    second = doctor.system_doctor()

    assert len(counted_doctor) == 1
    assert first["cached"] is False
    assert first["cache_age_s"] == 0.0
    assert second["cached"] is True
    assert second["cached_at"] == first["cached_at"]
    assert second["cache_age_s"] >= 0.0
    assert second["cache_ttl_s"] == 30.0
    assert second["summary"] == first["summary"]


def test_a_call_after_the_ttl_runs_the_probes_again(monkeypatch, counted_doctor, clock):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "0")

    doctor.system_doctor()
    clock.advance(0.1)
    again = doctor.system_doctor()

    assert len(counted_doctor) == 2
    assert again["cached"] is False
    assert again["summary"]["run"] == 2


def test_refresh_bypasses_the_shared_report(monkeypatch, counted_doctor):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")

    doctor.system_doctor()
    fresh = doctor.system_doctor(refresh=True)
    after = doctor.system_doctor()

    assert len(counted_doctor) == 2
    assert fresh["cached"] is False
    assert fresh["summary"]["run"] == 2
    assert after["cached"] is True
    assert after["summary"]["run"] == 2


def test_a_zero_ttl_disables_the_cache(monkeypatch, counted_doctor):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0")

    doctor.system_doctor()
    second = doctor.system_doctor()

    assert len(counted_doctor) == 2
    assert second["cached"] is False


def test_concurrent_callers_share_one_run(monkeypatch, run_waiters):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")
    runs: list[int] = []
    gate = threading.Event()

    def _slow_run() -> dict:
        runs.append(1)
        gate.wait(timeout=5)
        return {"checks": [], "summary": {"run": len(runs)}}

    monkeypatch.setattr(doctor, "_run_system_doctor", _slow_run)
    doctor.clear_report_cache()
    results: list[dict] = []
    threads = [
        threading.Thread(target=lambda: results.append(doctor.system_doctor()), daemon=True)
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    # One caller runs the probes; the other four wait on its run.
    _wait_until(lambda: len(runs) == 1 and len(run_waiters) == 4)
    gate.set()
    for t in threads:
        t.join(timeout=10)

    assert len(runs) == 1
    assert len(results) == 5
    assert {r["summary"]["run"] for r in results} == {1}
    assert sum(1 for r in results if r["cached"] is False) == 1
    doctor.clear_report_cache()


def _concurrent_calls(n: int, gate: threading.Event, waiting: list[int]) -> list[dict]:
    results: list[dict] = []
    errors: list[BaseException] = []

    def _call() -> None:
        try:
            results.append(doctor.system_doctor())
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=_call, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    # One caller runs the probes; the rest wait on its run.
    _wait_until(lambda: len(waiting) == n - 1)
    gate.set()
    for t in threads:
        t.join(timeout=10)
    return results + errors  # type: ignore[operator]


def test_concurrent_callers_share_one_run_even_with_a_zero_ttl(monkeypatch, run_waiters):
    """Devin, PR #1016: a zero TTL turns off reuse after a run completes, not
    single-flight. Callers that arrive while a run is in progress share it."""
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0")
    runs: list[int] = []
    gate = threading.Event()

    def _slow_run() -> dict:
        runs.append(1)
        gate.wait(timeout=5)
        return {"checks": [], "summary": {"run": len(runs)}}

    monkeypatch.setattr(doctor, "_run_system_doctor", _slow_run)
    doctor.clear_report_cache()

    results = _concurrent_calls(5, gate, run_waiters)

    assert len(runs) == 1
    assert {r["summary"]["run"] for r in results} == {1}
    assert sum(1 for r in results if r["cached"] is False) == 1
    # After the run completes, a zero TTL reuses nothing.
    after = doctor.system_doctor()
    assert len(runs) == 2
    assert after["cached"] is False
    doctor.clear_report_cache()


def test_a_failed_run_reaches_its_waiters_and_the_next_call_retries(monkeypatch, run_waiters):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")
    runs: list[int] = []
    gate = threading.Event()

    def _failing_once() -> dict:
        runs.append(1)
        if len(runs) == 1:
            gate.wait(timeout=5)
            raise RuntimeError("probe crashed")
        return {"checks": [], "summary": {"run": len(runs)}}

    monkeypatch.setattr(doctor, "_run_system_doctor", _failing_once)
    doctor.clear_report_cache()

    outcomes = _concurrent_calls(3, gate, run_waiters)

    assert len(runs) == 1
    assert all(isinstance(o, RuntimeError) for o in outcomes)
    retried = doctor.system_doctor()
    assert len(runs) == 2
    assert retried["cached"] is False
    doctor.clear_report_cache()


def test_a_caller_cannot_alter_the_shared_report(monkeypatch, counted_doctor):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")

    first = doctor.system_doctor()
    first["checks"].append({"capability": "injected"})
    first["summary"]["run"] = 99

    second = doctor.system_doctor()
    assert [c["capability"] for c in second["checks"]] == ["x"]
    assert second["summary"]["run"] == 1


def test_the_doctor_initializes_the_store_once_per_process(monkeypatch):
    """The connector check used storage.init_db() on every run, which reran
    the schema and restarted every finished backfill scan."""
    calls: list[str] = []
    real_once = storage.init_db_once
    monkeypatch.setattr(storage, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(
        storage, "init_db_once", lambda: (calls.append("init_db_once"), real_once())[1]
    )

    doctor._connectors_check()

    assert "init_db" not in calls


def test_doctor_cache_ttl_setting(monkeypatch):
    monkeypatch.delenv("UA_DOCTOR_CACHE_S", raising=False)
    assert config.doctor_cache_ttl_s() == 120.0
    for raw, expected in (("45", 45.0), ("0", 0.0), ("-1", 120.0), ("nan", 120.0), ("x", 120.0)):
        monkeypatch.setenv("UA_DOCTOR_CACHE_S", raw)
        assert config.doctor_cache_ttl_s() == expected


def test_doctor_max_stale_setting(monkeypatch):
    monkeypatch.delenv("UA_DOCTOR_MAX_STALE_S", raising=False)
    assert config.doctor_max_stale_s() == 600.0
    for raw, expected in (("900", 900.0), ("0", 0.0), ("-1", 600.0), ("inf", 600.0)):
        monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", raw)
        assert config.doctor_max_stale_s() == expected


def test_default_windows_keep_a_minute_poll_from_blocking():
    """A 60s pane poll lands inside the fresh or stale window every time, and
    full runs are at least two minutes apart (task #1897)."""
    fresh, bound = config.DOCTOR_CACHE_DEFAULT_S, config.DOCTOR_MAX_STALE_DEFAULT_S
    assert fresh >= 120
    assert bound > fresh + 60


# --- stale-while-revalidate (task #1897) -----------------------------------------


@pytest.fixture
def gated_doctor(monkeypatch):
    """Run 1 returns at once; later runs block on ``gate`` (or raise ``fail``)."""
    state = {"runs": 0, "fail": None}
    gate = threading.Event()

    def _run() -> dict:
        state["runs"] += 1
        run = state["runs"]
        if run > 1:
            gate.wait(timeout=5)
            if state["fail"] is not None:
                raise state["fail"]
        return {"checks": [], "summary": {"run": run}}

    monkeypatch.setattr(doctor, "_run_system_doctor", _run)
    doctor.clear_report_cache()
    yield state, gate
    gate.set()
    doctor.wait_for_report_refresh(timeout=5)
    doctor.clear_report_cache()


def test_polls_inside_the_fresh_window_reuse_one_report(monkeypatch, counted_doctor):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "600")

    first = doctor.system_doctor()
    polls = [doctor.system_doctor() for _ in range(5)]

    assert len(counted_doctor) == 1
    assert first["cached"] is False and first["stale"] is False
    for poll in polls:
        assert poll["cached"] is True
        assert poll["stale"] is False
        assert poll["refreshing"] is False
        assert poll["cached_at"] == first["cached_at"]
        assert poll["cache_max_stale_s"] == 600.0
        assert "refresh_error" not in poll


def test_a_stale_report_is_served_at_once_while_one_background_run_refreshes(
    monkeypatch, gated_doctor, clock
):
    state, gate = gated_doctor
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "30")
    doctor.system_doctor()
    clock.advance(0.1)

    results: list[dict] = []
    threads = [
        threading.Thread(target=lambda: results.append(doctor.system_doctor()), daemon=True)
        for _ in range(8)
    ]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)
    elapsed = time.monotonic() - started

    # Nobody waited for the gated run, and exactly one run was started.
    assert len(results) == 8
    assert elapsed < 2
    assert state["runs"] == 2
    for r in results:
        assert r["summary"]["run"] == 1
        assert r["cached"] is True
        assert r["stale"] is True
        assert r["refreshing"] is True
        assert r["cache_age_s"] >= 0.05

    gate.set()
    assert doctor.wait_for_report_refresh(timeout=5)
    after = doctor.system_doctor()
    assert state["runs"] == 2
    assert after["summary"]["run"] == 2
    assert after["refreshing"] is False
    assert after["stale"] is False


def test_refresh_blocks_and_returns_a_fresh_report_over_a_stale_one(
    monkeypatch, gated_doctor, clock
):
    state, gate = gated_doctor
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "30")
    doctor.system_doctor()
    clock.advance(0.1)

    call = _in_thread(lambda: doctor.system_doctor(refresh=True))
    # The caller runs the probes itself and is still inside the gated run.
    _wait_until(lambda: state["runs"] == 2)
    assert not call.done()
    gate.set()
    fresh = call.result(timeout=5)

    assert state["runs"] == 2
    assert fresh["summary"]["run"] == 2
    assert fresh["cached"] is False
    assert fresh["cache_age_s"] == 0.0
    assert fresh["stale"] is False


def test_a_report_past_max_stale_blocks_on_a_run(monkeypatch, gated_doctor, clock):
    state, gate = gated_doctor
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "0.15")
    doctor.system_doctor()
    clock.advance(0.25)

    call = _in_thread(doctor.system_doctor)
    _wait_until(lambda: state["runs"] == 2)
    assert not call.done()
    gate.set()
    report = call.result(timeout=5)

    assert state["runs"] == 2
    assert report["summary"]["run"] == 2
    assert report["cached"] is False


def test_a_max_stale_at_or_below_the_ttl_turns_stale_serving_off(
    monkeypatch, counted_doctor, clock
):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "0.05")

    first = doctor.system_doctor()
    clock.advance(0.1)
    again = doctor.system_doctor()

    assert first["cache_max_stale_s"] == 0.0
    assert len(counted_doctor) == 2
    assert again["cached"] is False


def test_a_failed_background_refresh_keeps_the_last_good_report(monkeypatch, gated_doctor, clock):
    state, gate = gated_doctor
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "30")
    good = doctor.system_doctor()
    clock.advance(0.1)
    state["fail"] = RuntimeError("probe exploded")
    gate.set()

    served = doctor.system_doctor()
    assert served["summary"]["run"] == 1
    assert doctor.wait_for_report_refresh(timeout=5)

    after = doctor.system_doctor()
    assert after["summary"]["run"] == 1
    assert after["cached_at"] == good["cached_at"]
    assert after["stale"] is True
    assert after["refresh_error"]["error"] == "RuntimeError: probe exploded"
    assert after["refresh_error"]["at"]

    # The next good run clears the error.
    assert doctor.wait_for_report_refresh(timeout=5)
    state["fail"] = None
    doctor.system_doctor()
    assert doctor.wait_for_report_refresh(timeout=5)
    healed = doctor.system_doctor()
    assert healed["summary"]["run"] > 1
    assert "refresh_error" not in healed


def test_a_run_started_before_a_clear_does_not_repopulate_the_cache(
    monkeypatch, gated_doctor, clock
):
    state, gate = gated_doctor
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "30")
    doctor.system_doctor()
    clock.advance(0.1)
    doctor.system_doctor()  # starts the gated background run
    run = doctor._report_inflight
    doctor.clear_report_cache()
    gate.set()
    assert run is not None and run.done.wait(5)

    assert doctor._report_cache is None


# --- shared worktree survey (task #1897) -----------------------------------------


@pytest.fixture
def counted_survey(monkeypatch):
    from unified_agent.tools import worktree_policy

    calls: list[int] = []

    def _survey() -> list[dict]:
        calls.append(1)
        return [{"path": f"/w/{len(calls)}", "stale": False, "disposable": True}]

    monkeypatch.setattr(worktree_policy, "survey_worktrees", _survey)
    worktree_policy.clear_survey_cache()
    yield worktree_policy, calls
    worktree_policy.clear_survey_cache()


def test_the_worktree_survey_is_shared_within_its_ttl(monkeypatch, counted_survey):
    worktree_policy, calls = counted_survey
    monkeypatch.setenv("UA_WORKTREE_SURVEY_CACHE_S", "300")

    first, first_age = worktree_policy.cached_survey_worktrees()
    first[0]["path"] = "mutated"
    second, second_age = worktree_policy.cached_survey_worktrees()

    assert len(calls) == 1
    assert first_age == 0.0
    assert second_age >= 0.0
    assert second[0]["path"] == "/w/1"


def test_a_changed_root_list_resurveys(monkeypatch, counted_survey, tmp_path):
    worktree_policy, calls = counted_survey
    monkeypatch.setenv("UA_WORKTREE_SURVEY_CACHE_S", "300")

    worktree_policy.cached_survey_worktrees()
    monkeypatch.setenv("UA_AUDIT_ROOTS", str(tmp_path))
    worktree_policy.cached_survey_worktrees()

    assert len(calls) == 2


def test_a_zero_survey_ttl_always_resurveys(monkeypatch, counted_survey):
    worktree_policy, calls = counted_survey
    monkeypatch.setenv("UA_WORKTREE_SURVEY_CACHE_S", "0")

    worktree_policy.cached_survey_worktrees()
    worktree_policy.cached_survey_worktrees()

    assert len(calls) == 2


def test_one_doctor_run_surveys_worktrees_once(monkeypatch, counted_survey):
    """The stale_worktrees row and the host snapshot each ran a full git
    survey per doctor run (task #1897)."""
    from unified_agent import host_registry

    worktree_policy, calls = counted_survey
    monkeypatch.setenv("UA_WORKTREE_SURVEY_CACHE_S", "300")

    row = doctor._stale_worktrees_check()
    host_registry.record_local_doctor_snapshot({"checks": [row], "summary": {}})

    assert len(calls) == 1
    assert row["status"] == "LIVE"


# --- LLM leg probe cache ---------------------------------------------------------


@pytest.fixture
def probe_counter(monkeypatch):
    calls: list[str] = []
    llm_client.clear_probe_cache()
    yield calls
    llm_client.clear_probe_cache()


def test_a_down_endpoint_is_probed_once_per_ttl(monkeypatch, probe_counter):
    monkeypatch.setenv("UA_LLM_PROBE_CACHE_S", "60")

    def refused(url, headers=None, timeout=None):
        probe_counter.append(url)
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", refused)

    # Ollama left the chain (task #2025); its probe helper still uses the cache.
    first = llm_client._ollama_leg_status({}, timeout_s=2.0)
    second = llm_client._ollama_leg_status({}, timeout_s=2.0)

    assert probe_counter == [f"{llm_client.DEFAULT_OLLAMA_BASE_URL}/models"]
    assert first["reachable"] is False and second["reachable"] is False
    assert "cached" not in first["detail"]
    assert "ConnectError" in second["detail"] and "cached" in second["detail"]


def test_a_probe_is_repeated_after_its_ttl(monkeypatch, probe_counter):
    llm_clock = _ManualClock()
    monkeypatch.setattr(llm_client, "time", llm_clock)
    monkeypatch.setenv("UA_LLM_PROBE_CACHE_S", "0.05")

    def refused(url, headers=None, timeout=None):
        probe_counter.append(url)
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", refused)
    llm_client._probe_endpoint("http://127.0.0.1:9/models", {}, 1.0)
    llm_clock.advance(0.1)
    llm_client._probe_endpoint("http://127.0.0.1:9/models", {}, 1.0)

    assert len(probe_counter) == 2


def test_probes_with_different_credentials_are_cached_apart(monkeypatch, probe_counter):
    monkeypatch.setenv("UA_LLM_PROBE_CACHE_S", "60")

    def answer(url, headers=None, timeout=None):
        probe_counter.append((headers or {}).get("Authorization", ""))
        status = 200 if headers and headers.get("Authorization") == "Bearer good" else 401
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", answer)
    url = "https://example.invalid/auth/key"
    bad = llm_client._probe_endpoint(url, {"Authorization": "Bearer bad"}, 1.0)
    good = llm_client._probe_endpoint(url, {"Authorization": "Bearer good"}, 1.0)
    bad_again = llm_client._probe_endpoint(url, {"Authorization": "Bearer bad"}, 1.0)

    assert bad[0] is False and good[0] is True and bad_again[0] is False
    assert len(probe_counter) == 2
    # The cache key holds a digest, never the credential itself.
    assert not any("Bearer" in str(key) for key in llm_client._probe_cache)


def test_a_short_timeout_failure_is_not_served_to_a_more_patient_call(monkeypatch, probe_counter):
    """Devin, PR #1016: a 1 s timeout failing says nothing about a 5 s probe."""
    monkeypatch.setenv("UA_LLM_PROBE_CACHE_S", "60")
    timeouts: list[float] = []

    def timing_out(url, headers=None, timeout=None):
        timeouts.append(timeout)
        if timeout < 3:
            raise httpx.ReadTimeout("slow", request=httpx.Request("GET", url))
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", timing_out)
    url = "http://127.0.0.1:9/models"

    assert llm_client._probe_endpoint(url, {}, 1.0)[0] is False
    # No more patient than the failure: served from the cache.
    assert llm_client._probe_endpoint(url, {}, 0.5)[0] is False
    assert llm_client._probe_endpoint(url, {}, 1.0)[0] is False
    assert timeouts == [1.0]
    # More patient: probed again, and it succeeds.
    assert llm_client._probe_endpoint(url, {}, 5.0)[0] is True
    assert timeouts == [1.0, 5.0]
    # A success is reused whatever the caller's timeout.
    ok, why = llm_client._probe_endpoint(url, {}, 0.5)
    assert ok is True and "cached" in why
    assert timeouts == [1.0, 5.0]


def test_a_zero_probe_ttl_disables_the_cache(monkeypatch, probe_counter):
    monkeypatch.setenv("UA_LLM_PROBE_CACHE_S", "0")

    def refused(url, headers=None, timeout=None):
        probe_counter.append(url)
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", refused)
    llm_client._probe_endpoint("http://127.0.0.1:9/models", {}, 1.0)
    llm_client._probe_endpoint("http://127.0.0.1:9/models", {}, 1.0)

    assert len(probe_counter) == 2


# --- set-based memory_vec mirror backfill ---------------------------------------


def _vec_available() -> bool:
    conn = storage.connect()
    try:
        conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _seed_embedded_notes(n: int, *, mirrored: set[int], wide: set[int] = frozenset()) -> None:
    """``n`` notes with a source embedding; only indexes in ``mirrored`` are in memory_vec."""
    conn = storage.connect()
    try:
        for i in range(n):
            note_id = f"mirror-{i:04d}"
            conn.execute(
                "INSERT INTO memory (id, title, content, created_at, updated_at) "
                "VALUES (?, ?, ?, '2026-01-01', '2026-01-01')",
                (note_id, f"Note {i}", f"body {i}"),
            )
            dim = storage.MEMORY_VEC_DIM * 2 if i in wide else storage.MEMORY_VEC_DIM
            emb = embeddings.embedding_to_bytes([0.01 * (i % 7 + 1)] * dim)
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings (note_id, embedding, model) "
                "VALUES (?, ?, ?)",
                (note_id, emb, config.embed_model()),
            )
        conn.commit()
        conn.execute(
            "DELETE FROM memory_vec WHERE rowid IN "
            "(SELECT rowid FROM memory WHERE id LIKE 'mirror-%')"
        )
        for i in sorted(mirrored):
            note_id = f"mirror-{i:04d}"
            emb = conn.execute(
                "SELECT embedding FROM memory_embeddings WHERE note_id=?", (note_id,)
            ).fetchone()[0]
            storage.upsert_memory_vec(conn, note_id, emb)
        conn.commit()
    finally:
        conn.close()


def _mirrored_ids() -> set[str]:
    conn = storage.connect()
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT m.id FROM memory m WHERE m.id LIKE 'mirror-%' "
                "AND m.rowid IN (SELECT rowid FROM memory_vec)"
            )
        }
    finally:
        conn.close()


def test_mirror_backfill_finds_exactly_the_missing_rows_on_one_connection(monkeypatch):
    if not _vec_available():
        pytest.skip("sqlite-vec not loadable on this host")
    n = 130
    already = set(range(0, n, 3))
    wide = {1}  # a different model's width: served by the per-row scan, never mirrored
    _seed_embedded_notes(n, mirrored=already, wide=wide)
    monkeypatch.setattr(memory_semantic, "BACKFILL_COMMIT_EVERY", 20)

    connects: list[int] = []
    real_connect = storage.connect

    def counting_connect():
        connects.append(1)
        return real_connect()

    monkeypatch.setattr(storage, "connect", counting_connect)
    embedded_calls: list[str] = []
    monkeypatch.setattr(
        embeddings,
        "generate_embedding",
        lambda text: embedded_calls.append(text) or [0.1] * storage.MEMORY_VEC_DIM,
    )

    memory_semantic.backfill_memory_embeddings()

    # O(1) connections for N rows: the pass opens exactly one.
    assert len(connects) == 1
    # Every note already had a source embedding, so the model was never asked.
    assert embedded_calls == []
    monkeypatch.setattr(storage, "connect", real_connect)
    expected = {f"mirror-{i:04d}" for i in range(n) if i not in wide}
    assert _mirrored_ids() == expected


def test_mirror_backfill_returns_the_count_it_added():
    if not _vec_available():
        pytest.skip("sqlite-vec not loadable on this host")
    _seed_embedded_notes(10, mirrored={0, 1, 2})
    conn = storage.connect()
    try:
        assert memory_semantic.mirror_missing_vectors(conn) == 7
        assert memory_semantic.mirror_missing_vectors(conn) == 0
    finally:
        conn.close()


# --- backfills stop when the model cannot be loaded ------------------------------


def _unloadable(calls: list[str]):
    def _raise(text: str):
        calls.append(text)
        raise embeddings._tag_phase(OSError("model download blocked"), embeddings.PHASE_ACQUIRE)

    return _raise


def test_memory_backfill_stops_at_the_first_model_load_failure(monkeypatch):
    conn = storage.connect()
    try:
        conn.executemany(
            "INSERT INTO memory (id, title, content, created_at, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01', '2026-01-01')",
            [(f"unembedded-{i}", f"T{i}", f"b{i}") for i in range(30)],
        )
        conn.execute("DELETE FROM memory_embeddings")
        conn.commit()
    finally:
        conn.close()
    calls: list[str] = []
    monkeypatch.setattr(embeddings, "generate_embedding", _unloadable(calls))

    memory_semantic.backfill_memory_embeddings()

    assert len(calls) == 1


def test_symbol_backfill_stops_at_the_first_model_load_failure(monkeypatch):
    conn = storage.connect()
    try:
        conn.executemany(
            "INSERT INTO workspace_symbols (workspace, file_path, symbol_name, symbol_type, "
            "signature, docstring, line_no) VALUES ('ws', 'mod.py', ?, 'function', '()', '', ?)",
            [(f"fn_{i}", i + 1) for i in range(30)],
        )
        conn.commit()
    finally:
        conn.close()
    calls: list[str] = []
    monkeypatch.setattr(embeddings, "generate_embedding", _unloadable(calls))

    indexer._backfill_symbol_embeddings()

    assert len(calls) == 1


def test_an_inference_failure_on_one_row_does_not_stop_the_pass(monkeypatch):
    conn = storage.connect()
    try:
        conn.executemany(
            "INSERT INTO memory (id, title, content, created_at, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01', '2026-01-01')",
            [(f"rowfail-{i}", f"T{i}", f"b{i}") for i in range(5)],
        )
        conn.execute("DELETE FROM memory_embeddings")
        conn.commit()
    finally:
        conn.close()

    def flaky(text: str):
        if text.startswith("T2\n"):
            raise embeddings._tag_phase(ValueError("bad text"), embeddings.PHASE_INFER)
        return [0.1] * storage.MEMORY_VEC_DIM

    monkeypatch.setattr(embeddings, "generate_embedding", flaky)

    memory_semantic.backfill_memory_embeddings()

    conn = storage.connect()
    try:
        ids = {r[0] for r in conn.execute("SELECT note_id FROM memory_embeddings")}
    finally:
        conn.close()
    assert {f"rowfail-{i}" for i in (0, 1, 3, 4)} <= ids
    assert "rowfail-2" not in ids


def test_a_forced_doctor_run_clears_the_llm_probe_cache(monkeypatch):
    from unified_agent import llm_client
    from unified_agent.tools import doctor

    cleared = []
    monkeypatch.setattr(llm_client, "clear_probe_cache", lambda: cleared.append(True))
    monkeypatch.setattr(doctor, "_run_system_doctor", lambda: {"checks": [], "summary": {}})
    doctor.system_doctor(refresh=True)
    assert cleared == [True]
    doctor.system_doctor()
    assert cleared == [True]


def test_a_raising_probe_cache_clear_does_not_wedge_the_run_slot(monkeypatch, counted_doctor):
    """The clear runs inside the run's try, so its raise still frees the slot,
    wakes the waiters and reaches the caller (task #1923)."""

    def _boom() -> None:
        raise RuntimeError("probe cache clear failed")

    monkeypatch.setattr(llm_client, "clear_probe_cache", _boom)
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")

    with pytest.raises(RuntimeError, match="probe cache clear failed"):
        doctor.system_doctor(refresh=True)

    assert doctor._report_inflight is None
    assert doctor.wait_for_report_refresh(timeout=1)
    monkeypatch.setattr(llm_client, "clear_probe_cache", lambda: None)
    assert doctor.system_doctor()["cached"] is False


# --- refresh=True re-reads shared probe results (Devin, PR #1044) ----------------


@pytest.fixture
def removable_worktrees(monkeypatch):
    """A doctor run that reads the worktree survey the way the real one does:
    the stale_worktrees row, then the host snapshot."""
    from unified_agent import host_registry
    from unified_agent.tools import worktree_policy

    present = ["task-1", "task-2"]
    surveys: list[int] = []

    def _survey() -> list[dict]:
        surveys.append(1)
        return [
            {
                "path": f"/dev/repo/.worktrees/{name}",
                "stale": True,
                "disposable": True,
                "in_workspace": True,
            }
            for name in present
        ]

    def _run() -> dict:
        row = doctor._stale_worktrees_check()
        host_registry.record_local_doctor_snapshot({"checks": [row], "summary": {}})
        return {"checks": [row], "summary": {}}

    monkeypatch.setattr(worktree_policy, "survey_worktrees", _survey)
    monkeypatch.setattr(doctor, "_run_system_doctor", _run)
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")
    monkeypatch.setenv("UA_WORKTREE_SURVEY_CACHE_S", "300")
    worktree_policy.clear_survey_cache()
    doctor.clear_report_cache()
    yield present, surveys
    doctor.wait_for_report_refresh(timeout=5)
    doctor.clear_report_cache()
    worktree_policy.clear_survey_cache()


def _worktree_detail(report: dict) -> str:
    (row,) = [c for c in report["checks"] if c["capability"] == "stale_worktrees"]
    return row["detail"]


def test_refresh_resurveys_a_removed_worktree_inside_the_survey_ttl(removable_worktrees):
    present, surveys = removable_worktrees

    first = doctor.system_doctor()
    assert "task-1" in _worktree_detail(first)
    assert len(surveys) == 1

    present.remove("task-1")
    # A non-forced reading keeps the shared survey inside its TTL.
    assert "task-1" in doctor._stale_worktrees_check()["detail"]
    assert len(surveys) == 1

    forced = doctor.system_doctor(refresh=True)
    assert "task-1" not in _worktree_detail(forced)
    assert "task-2" in _worktree_detail(forced)
    # Exactly one new survey: the host snapshot reused the forced one.
    assert len(surveys) == 2
    # The persisted host census was written from that same new survey.
    from unified_agent import config, host_registry

    (local,) = [
        h
        for h in host_registry.fleet_payload()["hosts"]
        if h.get("host") == config.current_source_host()
    ]
    assert local["worktrees"]["reported"] is True
    assert local["worktrees"]["total"] == 1
    # And the forced survey replaced the shared one for later readers.
    assert "task-1" not in doctor._stale_worktrees_check()["detail"]
    assert len(surveys) == 2


def test_a_forced_flag_does_not_leak_to_a_concurrent_unforced_reading(removable_worktrees):
    present, surveys = removable_worktrees
    doctor.system_doctor()
    present.remove("task-1")

    assert doctor._forced_run.get() is False
    seen: list[str] = []
    reader = threading.Thread(
        target=lambda: seen.append(doctor._stale_worktrees_check()["detail"]), daemon=True
    )
    doctor.system_doctor(refresh=True)
    assert doctor._forced_run.get() is False
    reader.start()
    reader.join(timeout=5)
    # The unforced reader reused the (now refreshed) shared survey, no extra run.
    assert len(surveys) == 2
    assert "task-1" not in seen[0]


def test_a_forced_refresh_does_not_join_a_background_run(monkeypatch, gated_doctor, clock):
    state, gate = gated_doctor
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "30")
    doctor.system_doctor()
    clock.advance(0.1)
    doctor.system_doctor()  # starts the gated background run (run 2)
    background = doctor._report_inflight
    assert background is not None and background.background

    call = _in_thread(lambda: doctor.system_doctor(refresh=True))
    # The forced caller queued its own run behind the background one.
    _wait_until(lambda: doctor._report_pending_forced is not None)
    gate.set()
    forced = call.result(timeout=5)

    assert state["runs"] == 3
    assert forced["summary"]["run"] == 3
    assert forced["cached"] is False
    assert background.done.wait(5)
    # Whichever finished last, the newer (forced) reading stays published.
    assert doctor.system_doctor()["summary"]["run"] == 3


def test_a_forced_caller_never_gets_a_run_that_began_before_it_asked(monkeypatch, run_waiters):
    """Review of #1044: a forced caller that arrives while another run is
    going (forced or not) must not be answered by that run, whose rows were
    computed before the force. Forced callers arriving meanwhile coalesce onto
    one run that starts after it."""
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")
    starts: list[float] = []
    gate = threading.Event()

    def _run() -> dict:
        starts.append(time.monotonic())
        n = len(starts)
        if n == 2:
            gate.wait(timeout=5)
        return {"checks": [], "summary": {"run": n, "started": starts[-1]}}

    monkeypatch.setattr(doctor, "_run_system_doctor", _run)
    doctor.clear_report_cache()
    try:
        doctor.system_doctor()
        first: list[dict] = []
        a = threading.Thread(
            target=lambda: first.append(doctor.system_doctor(refresh=True)), daemon=True
        )
        a.start()
        deadline = time.monotonic() + 5
        while len(starts) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        forced_at = time.monotonic()
        later: list[dict] = []
        others = [
            threading.Thread(
                target=lambda: later.append(doctor.system_doctor(refresh=True)), daemon=True
            )
            for _ in range(2)
        ]
        for t in others:
            t.start()
        # Both forced callers park: one on run 2 before starting the queued
        # run, the other on the queued run itself.
        _wait_until(lambda: len(run_waiters) == 2)
        gate.set()
        for t in [a, *others]:
            t.join(timeout=5)

        assert first[0]["summary"]["run"] == 2
        assert len(later) == 2
        assert {r["summary"]["run"] for r in later} == {3}
        assert all(r["summary"]["started"] >= forced_at for r in later)
        # Two forced callers, one queued run: no run per caller.
        assert len(starts) == 3
    finally:
        gate.set()
        doctor.wait_for_report_refresh(timeout=5)
        doctor.clear_report_cache()


# --- independent review of #1044 --------------------------------------------------


def test_a_background_thread_that_fails_to_start_does_not_wedge_the_doctor(
    monkeypatch, gated_doctor, clock
):
    state, gate = gated_doctor
    gate.set()
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "0.4")
    doctor.system_doctor()
    clock.advance(0.1)

    def _no_threads(self):
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", _no_threads)
        served = doctor.system_doctor()

    # Served at once, not marked refreshing, and the failure is reported.
    assert served["summary"]["run"] == 1
    assert served["refreshing"] is False
    assert "did not start" in served["refresh_error"]["error"]
    assert doctor._report_inflight is None
    # Nothing is left in the slot to wait on: a forced call and a call past
    # max-stale both run instead of hanging.
    forced = doctor.system_doctor(refresh=True)
    assert forced["summary"]["run"] == 2
    clock.advance(0.5)
    past_bound = doctor.system_doctor()
    assert past_bound["cached"] is False
    assert past_bound["summary"]["run"] == 3


def test_waiting_on_a_run_that_never_finishes_is_bounded(monkeypatch, counted_doctor):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")
    monkeypatch.setattr(doctor, "RUN_WAIT_TIMEOUT_S", 0.2)
    # A run sits in the slot and never finishes (a hung probe, a lost thread).
    with doctor._report_lock:
        doctor._report_inflight = doctor._DoctorRun(
            background=False, generation=doctor._report_generation
        )

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        doctor.system_doctor()
    assert time.monotonic() - started < 2
    # A forced caller waits it out for the same bound, then runs itself.
    forced = doctor.system_doctor(refresh=True)
    assert forced["cached"] is False
    assert len(counted_doctor) == 1
    doctor.clear_report_cache()


@pytest.mark.parametrize("poll_age", [30, 200], ids=["stale", "expired"])
def test_polls_recover_after_a_force_displaces_a_hung_run(monkeypatch, counted_doctor, poll_age):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "10")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "100")
    hung = doctor._DoctorRun(background=True, generation=doctor._report_generation)
    # Controlled timeout: no probe thread or real 300-second wait.
    monkeypatch.setattr(hung.done, "wait", lambda timeout: False)
    with doctor._report_lock:
        doctor._report_inflight = hung

    assert doctor.system_doctor(refresh=True)["summary"]["run"] == 1
    assert doctor._report_inflight is None
    assert doctor.wait_for_report_refresh(timeout=0)
    with doctor._report_lock:
        stamp, iso, report, duration = doctor._report_cache
        doctor._report_cache = (stamp - poll_age, iso, report, duration)

    polled = doctor.system_doctor()
    if poll_age == 30:
        assert polled["stale"] and polled["refreshing"]
        assert doctor.wait_for_report_refresh(timeout=5)
        polled = doctor.system_doctor()
    assert polled["summary"]["run"] == 2
    assert not polled["refreshing"]
    assert len(counted_doctor) == 2


@pytest.mark.parametrize("late_failure", [False, True], ids=["success", "failure"])
def test_displaced_background_run_cannot_publish_late_state(monkeypatch, late_failure):
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "10")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=5)
            if late_failure:
                raise RuntimeError("displaced failure")
            return {"checks": [], "summary": {"run": "displaced"}}
        return {"checks": [], "summary": {"run": "replacement"}}

    monkeypatch.setattr(doctor, "_run_system_doctor", probe)
    doctor.clear_report_cache()
    prior = doctor._DoctorRun(background=True, generation=doctor._report_generation)
    with doctor._report_lock:
        doctor._report_inflight = prior
    thread = threading.Thread(target=doctor._background_refresh, args=(prior,), daemon=True)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        real_wait = prior.done.wait
        monkeypatch.setattr(prior.done, "wait", lambda timeout: False)
        replacement = doctor.system_doctor(refresh=True)
        assert replacement["summary"]["run"] == "replacement"
        # A subsequent background failure belongs to the replacement's era.
        # Neither a late predecessor success nor failure may erase/replace it.
        newer_error = ("newer reading", "newer background failure")
        with doctor._report_lock:
            doctor._refresh_error = newer_error
        release.set()
        assert real_wait(timeout=5)
        assert doctor._report_cache[2]["summary"]["run"] == "replacement"
        assert doctor._refresh_error == newer_error
        assert doctor._report_inflight is None
    finally:
        release.set()
        thread.join(timeout=5)
        doctor.clear_report_cache()


def test_unforced_waiter_shares_the_replacement_of_a_hung_run(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0")
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def probe():
        calls.append(1)
        entered.set()
        assert release.wait(timeout=5)
        return {"checks": [], "summary": {"run": len(calls)}}

    monkeypatch.setattr(doctor, "_run_system_doctor", probe)
    doctor.clear_report_cache()
    prior = doctor._DoctorRun(background=True, generation=doctor._report_generation)
    monkeypatch.setattr(prior.done, "wait", lambda timeout: False)
    with doctor._report_lock:
        doctor._report_inflight = prior
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            forced = pool.submit(doctor.system_doctor, refresh=True)
            assert entered.wait(timeout=5)
            replacement = doctor._report_pending_forced or doctor._report_inflight
            real_wait = replacement.done.wait

            def wait(timeout):
                joined.set()
                return real_wait(timeout)

            monkeypatch.setattr(replacement.done, "wait", wait)
            waiter = pool.submit(doctor.system_doctor)
            try:
                assert joined.wait(timeout=1)
            finally:
                release.set()
            assert forced.result(timeout=5)["summary"]["run"] == 1
            assert waiter.result(timeout=5)["summary"]["run"] == 1
        assert len(calls) == 1
        assert doctor._report_inflight is None
    finally:
        release.set()
        doctor.clear_report_cache()


def test_a_forced_run_clears_the_llm_probe_cache_as_it_starts(monkeypatch, gated_doctor, clock):
    """The clear must follow any run the forced caller waited out, which could
    have refilled the probe cache from before the click."""
    from unified_agent import llm_client

    state, gate = gated_doctor
    events: list[str] = []
    real_run = doctor._run_system_doctor

    def _run() -> dict:
        events.append("start")
        out = real_run()
        events.append(f"end {out['summary']['run']}")
        return out

    monkeypatch.setattr(doctor, "_run_system_doctor", _run)
    monkeypatch.setattr(llm_client, "clear_probe_cache", lambda: events.append("clear"))
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "30")
    doctor.system_doctor()
    clock.advance(0.1)
    doctor.system_doctor()  # gated background run 2
    call = _in_thread(lambda: doctor.system_doctor(refresh=True))
    _wait_until(lambda: doctor._report_pending_forced is not None)
    gate.set()
    forced = call.result(timeout=5)

    assert forced["summary"]["run"] == 3
    assert events.index("clear") > events.index("end 2")
    assert events.index("clear") < len(events) - 2  # before run 3 started


def test_a_background_run_sees_the_callers_context(monkeypatch, gated_doctor, clock):
    from unified_agent import config

    state, gate = gated_doctor
    gate.set()
    seen: list[object] = []
    real_run = doctor._run_system_doctor

    def _run() -> dict:
        seen.append(config.workspace_root_var.get())
        return real_run()

    monkeypatch.setattr(doctor, "_run_system_doctor", _run)
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0.05")
    monkeypatch.setenv("UA_DOCTOR_MAX_STALE_S", "30")
    marker = config.workspace_root_var.set(Path("/srv/ws"))
    try:
        doctor.system_doctor()  # foreground
        clock.advance(0.1)
        doctor.system_doctor()  # background
        assert doctor.wait_for_report_refresh(timeout=5)
    finally:
        config.workspace_root_var.reset(marker)

    assert seen == [Path("/srv/ws"), Path("/srv/ws")]
