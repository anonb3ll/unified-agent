"""Single-flight for the Control Center's cached ``system_doctor`` reading.

The doctor result is cached for 30 s. Without a lock, every request that
arrived after the cache expired ran its own full doctor (several systemctl
probes each), and a dashboard load that fans out to several panes did that
once per pane. One caller computes; the others wait and reuse its result.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from unified_agent.webui import api as webui_api


async def _until(predicate, timeout: float = 5.0) -> None:
    """Yield to the loop until ``predicate()`` holds, instead of a fixed sleep."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.002)


def _until_sync(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.002)


@pytest.fixture
def slow_doctor(monkeypatch):
    calls: list[str] = []

    async def _fake_safe(name, /, default=None, **kwargs):
        calls.append(name)
        await asyncio.sleep(0.05)
        return {"checks": [], "summary": {"run": len(calls)}}

    monkeypatch.setattr(webui_api, "_safe", _fake_safe)
    monkeypatch.setattr(webui_api, "_doctor_cache", None)
    monkeypatch.setattr(webui_api, "_doctor_inflight", None)
    monkeypatch.setattr(webui_api, "_doctor_pending_forced", None)
    return calls


async def test_concurrent_requests_after_expiry_run_the_doctor_once(slow_doctor):
    results = await asyncio.gather(*(webui_api.health_payload() for _ in range(5)))

    assert slow_doctor == ["system_doctor"]
    assert {r["summary"]["run"] for r in results} == {1}


async def test_concurrent_forced_refreshes_coalesce_into_one_queued_call(slow_doctor):
    """The first forced request calls the doctor. The others arrived after that
    call began, so they never share it (task #1897): they share one call
    queued behind it, however many there are."""
    results = await asyncio.gather(*(webui_api.health_payload(force=True) for _ in range(3)))

    assert slow_doctor == ["system_doctor", "system_doctor"]
    assert [r["summary"]["run"] for r in results] == [1, 2, 2]


async def test_a_forced_refresh_after_a_completed_run_recomputes(slow_doctor):
    await webui_api.health_payload()
    cached = await webui_api.health_payload()
    forced = await webui_api.health_payload(force=True)

    assert slow_doctor == ["system_doctor", "system_doctor"]
    assert cached["summary"]["run"] == 1
    assert forced["summary"]["run"] == 2
    assert forced["cached_age_s"] == 0.0


def test_the_lock_works_across_event_loops(slow_doctor):
    async def _burst():
        await asyncio.gather(*(webui_api.health_payload(force=True) for _ in range(2)))

    asyncio.run(_burst())
    asyncio.run(_burst())

    # Per burst: one call, plus one queued call for the forced request that
    # arrived after it began (task #1897). Nothing leaks between loops.
    assert slow_doctor == ["system_doctor"] * 4
    assert webui_api._doctor_inflight is None
    assert webui_api._doctor_pending_forced is None


def test_concurrent_loops_in_different_threads_run_the_doctor_once(slow_doctor):
    """PR #1002 review: one process-wide lock decides who refreshes, so two
    event loops racing after expiry share one reading."""
    import threading

    barrier = threading.Barrier(2)
    results: list[dict] = []

    def _run():
        barrier.wait()
        results.append(asyncio.run(webui_api.health_payload()))

    threads = [threading.Thread(target=_run, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert slow_doctor == ["system_doctor"]
    assert len(results) == 2


async def test_a_burst_larger_than_the_thread_pool_does_not_deadlock(monkeypatch):
    """PR #1002 review: waiters must not hold worker threads, or a burst past
    the AnyIO pool (default 40) starves the refresh they wait on."""
    import anyio

    calls: list[str] = []

    async def _threaded_safe(name, /, default=None, **kwargs):
        calls.append(name)
        await anyio.to_thread.run_sync(time.sleep, 0.05)
        return {"checks": [], "summary": {"run": len(calls)}}

    monkeypatch.setattr(webui_api, "_safe", _threaded_safe)
    monkeypatch.setattr(webui_api, "_doctor_cache", None)
    monkeypatch.setattr(webui_api, "_doctor_inflight", None)
    monkeypatch.setattr(webui_api, "_doctor_pending_forced", None)
    results = await asyncio.wait_for(
        asyncio.gather(*(webui_api.health_payload(force=True) for _ in range(60))), timeout=20
    )
    # One call and one queued call for the 59 that arrived after it began.
    assert calls == ["system_doctor", "system_doctor"]
    assert len(results) == 60


async def test_a_failed_refresh_releases_waiters_and_the_next_call_retries(monkeypatch):
    calls: list[str] = []

    async def _failing_once(name, /, default=None, **kwargs):
        calls.append(name)
        await asyncio.sleep(0.02)
        if len(calls) == 1:
            raise RuntimeError("doctor crashed")
        return {"checks": [], "summary": {"run": len(calls)}}

    monkeypatch.setattr(webui_api, "_safe", _failing_once)
    monkeypatch.setattr(webui_api, "_doctor_cache", None)
    outcomes = await asyncio.gather(
        *(webui_api.health_payload() for _ in range(3)), return_exceptions=True
    )
    assert all(isinstance(o, RuntimeError) for o in outcomes)
    assert (await webui_api.health_payload())["summary"]["run"] == 2


async def test_a_cancelled_waiter_does_not_cancel_the_shared_refresh(slow_doctor):
    """PR #1002 review: closing one request must not fail the others."""
    owner = asyncio.ensure_future(webui_api.health_payload())
    await asyncio.sleep(0)
    quitter = asyncio.ensure_future(webui_api.health_payload())
    stayer = asyncio.ensure_future(webui_api.health_payload())
    await asyncio.sleep(0)  # both join the owner's call, still running
    quitter.cancel()
    first, last = await asyncio.gather(owner, stayer)
    assert slow_doctor == ["system_doctor"]
    assert first["summary"]["run"] == last["summary"]["run"] == 1
    assert quitter.cancelled()


# --- stale-while-revalidate on the hub (task #1897) ------------------------------


@pytest.fixture
def stale_then_fresh(monkeypatch):
    """A poll gets the tool's stale report; a forced call gets a fresh run."""
    calls: list[dict] = []

    async def _fake_safe(name, /, default=None, **kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.05)
        if kwargs.get("refresh"):
            return {"checks": [], "summary": {"run": "fresh"}, "cache_age_s": 0.0}
        return {
            "checks": [],
            "summary": {"run": "stale"},
            "cache_age_s": 300.0,
            "refreshing": True,
        }

    monkeypatch.setattr(webui_api, "_safe", _fake_safe)
    monkeypatch.setattr(webui_api, "_doctor_cache", None)
    monkeypatch.setattr(webui_api, "_doctor_inflight", None)
    monkeypatch.setattr(webui_api, "_doctor_pending_forced", None)
    return calls


async def test_a_stale_tool_report_shows_its_true_age(stale_then_fresh):
    reading = await webui_api.health_payload()

    assert reading["cached_age_s"] >= 300.0
    assert reading["refreshing"] is True
    # Older than the pane TTL, so the next poll asks the hub again (cheap: the
    # tool serves its shared report) instead of pinning the stale one.
    await webui_api.health_payload()
    assert len(stale_then_fresh) == 2


async def test_a_forced_refresh_does_not_join_a_poll_in_flight(stale_then_fresh):
    poll = asyncio.ensure_future(webui_api.health_payload())
    await asyncio.sleep(0)
    forced = await webui_api.health_payload(force=True)
    polled = await poll

    assert forced["summary"]["run"] == "fresh"
    assert forced["cached_age_s"] == 0.0
    assert polled["summary"]["run"] == "stale"
    assert [c.get("refresh", False) for c in stale_then_fresh] == [False, True]
    # The poll finished after the forced call: it must not replace the forced
    # reading in the pane cache.
    again = await webui_api.health_payload()
    assert again["summary"]["run"] == "fresh"


async def test_a_forced_refresh_ignores_a_stale_reading_cached_meanwhile(stale_then_fresh):
    await webui_api.health_payload()
    forced = await webui_api.health_payload(force=True)

    assert forced["summary"]["run"] == "fresh"
    assert stale_then_fresh[-1] == {"refresh": True}


async def test_a_later_forced_request_sees_a_worktree_change_the_earlier_one_missed(
    monkeypatch,
):
    """Review of #1044: a forced request that arrives while an earlier forced
    request's run is going must not get that run's rows, which were probed
    before it asked. End to end through the API and the real doctor cache."""
    import threading

    import anyio

    from unified_agent import config, host_registry
    from unified_agent.tools import doctor, worktree_policy

    present = ["task-1", "task-2"]
    paused = threading.Event()
    release = threading.Event()
    runs: list[int] = []

    def _survey() -> list[dict]:
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
        runs.append(1)
        row = doctor._stale_worktrees_check()
        if len(runs) == 1:
            paused.set()
            release.wait(timeout=5)
        host_registry.record_local_doctor_snapshot({"checks": [row], "summary": {}})
        return {"checks": [row], "summary": {"run": len(runs)}}

    async def _real_safe(name, /, default=None, **kwargs):
        return await anyio.to_thread.run_sync(lambda: doctor.system_doctor(**kwargs))

    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "30")
    monkeypatch.setenv("UA_WORKTREE_SURVEY_CACHE_S", "300")
    monkeypatch.setattr(worktree_policy, "survey_worktrees", _survey)
    monkeypatch.setattr(doctor, "_run_system_doctor", _run)
    monkeypatch.setattr(webui_api, "_safe", _real_safe)
    monkeypatch.setattr(webui_api, "_doctor_cache", None)
    monkeypatch.setattr(webui_api, "_doctor_inflight", None)
    monkeypatch.setattr(webui_api, "_doctor_pending_forced", None, raising=False)
    worktree_policy.clear_survey_cache()
    doctor.clear_report_cache()
    try:
        first = asyncio.ensure_future(webui_api.health_payload(force=True))
        for _ in range(2500):
            if paused.is_set():
                break
            await asyncio.sleep(0.002)
        assert paused.is_set()

        present.remove("task-1")
        second = asyncio.ensure_future(webui_api.health_payload(force=True))
        # The second request queued its own forced call behind the first.
        await _until(lambda: webui_api._doctor_pending_forced is not None)
        release.set()
        r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    finally:
        release.set()

    def detail(report: dict) -> str:
        (row,) = [c for c in report["checks"] if c["capability"] == "stale_worktrees"]
        return row["detail"]

    assert "task-1" in detail(r1)
    assert "task-1" not in detail(r2)
    assert "task-2" in detail(r2)
    (local,) = [
        h
        for h in host_registry.fleet_payload()["hosts"]
        if h.get("host") == config.current_source_host()
    ]
    assert local["worktrees"]["total"] == 1
    doctor.clear_report_cache()
    worktree_policy.clear_survey_cache()


# --- review of #1045: bounded, cancellation-safe, race-free forced refresh -------


@pytest.fixture
def doctor_state(monkeypatch):
    """Fresh web-layer doctor state; ``calls`` records each call's refresh flag."""
    import threading

    calls: list[bool] = []
    gate = threading.Event()

    async def _safe(name, /, default=None, **kwargs):
        forced = bool(kwargs.get("refresh"))
        calls.append(forced)
        if len(calls) == 1:
            # The first call (always a poll here) stalls until the test says.
            await asyncio.to_thread(gate.wait, 5)
            return {"checks": [], "summary": {"run": "before-refresh"}}
        return {"checks": [], "summary": {"run": "fresh" if forced else f"poll-{len(calls)}"}}

    monkeypatch.setattr(webui_api, "_safe", _safe)
    monkeypatch.setattr(webui_api, "_doctor_cache", None)
    monkeypatch.setattr(webui_api, "_doctor_inflight", None)
    monkeypatch.setattr(webui_api, "_doctor_pending_forced", None)
    yield calls, gate
    gate.set()


def test_the_web_wait_is_within_the_doctors_own_bound():
    from unified_agent.tools import doctor

    assert webui_api._DOCTOR_WAIT_S <= doctor.RUN_WAIT_TIMEOUT_S


async def test_refresh_behind_a_stalled_call_starts_after_a_bounded_wait(monkeypatch, doctor_state):
    calls, gate = doctor_state
    # The queued call stops waiting after half of this bound.
    monkeypatch.setattr(webui_api, "_DOCTOR_WAIT_S", 0.1, raising=False)
    poll = asyncio.ensure_future(webui_api.health_payload())
    await _until(lambda: calls)

    forced = await asyncio.wait_for(webui_api.health_payload(force=True), timeout=3)

    assert forced["summary"]["run"] == "fresh"
    assert calls == [False, True]
    gate.set()
    await asyncio.wait_for(asyncio.shield(poll), timeout=3)
    await _until(lambda: webui_api._doctor_inflight is None)
    # The stalled call's late answer does not replace the forced reading.
    assert (await webui_api.health_payload())["summary"]["run"] == "fresh"


async def test_a_waiter_on_a_stalled_call_gets_the_last_reading_and_an_error(
    monkeypatch, doctor_state
):
    calls, gate = doctor_state
    monkeypatch.setattr(webui_api, "_DOCTOR_WAIT_S", 0.05, raising=False)
    monkeypatch.setattr(webui_api, "_DOCTOR_CACHE_S", 0.0)
    monkeypatch.setattr(
        webui_api, "_doctor_cache", (time.monotonic() - 90, {"checks": [], "summary": {}}, 0)
    )

    reading = await asyncio.wait_for(webui_api.health_payload(), timeout=3)

    assert "did not answer within" in reading["error"]
    assert reading["cached_age_s"] >= 90


async def test_a_cancelled_refresh_does_not_fail_the_refreshes_sharing_its_call(doctor_state):
    calls, gate = doctor_state
    poll = asyncio.ensure_future(webui_api.health_payload())
    await _until(lambda: calls)
    owner = asyncio.ensure_future(webui_api.health_payload(force=True))
    await _until(lambda: webui_api._doctor_pending_forced is not None)
    queued = webui_api._doctor_pending_forced
    survivor = asyncio.ensure_future(webui_api.health_payload(force=True))
    await asyncio.sleep(0)  # the survivor joins the queued call

    owner.cancel()
    await _until(owner.done)
    assert webui_api._doctor_pending_forced is queued
    gate.set()
    reading = await asyncio.wait_for(survivor, timeout=3)

    assert owner.cancelled()
    assert reading["summary"]["run"] == "fresh"
    assert calls == [False, True]
    await poll


def test_a_refresh_never_takes_a_call_that_finished_while_it_took_the_lock(
    monkeypatch, doctor_state
):
    """The earlier call probes before the click and finishes in the instant the
    forced request takes the state lock: its reading must not answer it."""
    import threading

    calls, gate = doctor_state
    earlier_done = threading.Event()

    def _earlier():
        asyncio.run(webui_api.health_payload())
        earlier_done.set()

    threading.Thread(target=_earlier, daemon=True).start()
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.002)
    real = webui_api._doctor_state_lock

    class _RacingLock:
        armed = True

        def __enter__(self):
            if self.armed:
                self.armed = False
                gate.set()  # the earlier call finishes right here
                assert earlier_done.wait(5)
            return real.__enter__()

        def __exit__(self, *exc):
            return real.__exit__(*exc)

    monkeypatch.setattr(webui_api, "_doctor_state_lock", _RacingLock())
    forced = asyncio.run(webui_api.health_payload(force=True))

    assert forced["summary"]["run"] == "fresh"
    assert calls == [False, True]


def test_promotion_leaves_no_gap_for_a_request_from_another_loop(monkeypatch, doctor_state):
    """When the call in flight finishes in one thread, the queued forced call
    takes its slot in the same step, so a request from another loop arriving
    then joins it instead of starting an extra call."""
    import threading

    calls, gate = doctor_state
    monkeypatch.setattr(webui_api, "_DOCTOR_CACHE_S", 0.0)
    first_done = threading.Event()

    def _first():
        asyncio.run(webui_api.health_payload())
        first_done.set()

    threading.Thread(target=_first, daemon=True).start()
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.002)

    real_lock = webui_api._doctor_state_lock
    decided: set[int] = set()

    class _DecisionLock:
        """The state lock, noting each thread that has picked the call it waits on."""

        def __enter__(self):
            return real_lock.__enter__()

        def __exit__(self, *exc):
            decided.add(threading.get_ident())
            return real_lock.__exit__(*exc)

    monkeypatch.setattr(webui_api, "_doctor_state_lock", _DecisionLock())

    async def _scenario() -> tuple[dict, list[dict]]:
        forced = asyncio.ensure_future(webui_api.health_payload(force=True))
        # Queued behind the first call.
        await _until(lambda: webui_api._doctor_pending_forced is not None)
        # Block this loop: the queued call cannot resume on its own while the
        # first call finishes and a request from a third thread arrives.
        gate.set()
        assert first_done.wait(5)
        late: list[dict] = []
        third = threading.Thread(
            target=lambda: late.append(asyncio.run(webui_api.health_payload())), daemon=True
        )
        # Thread idents are reused: forget the finished first thread's mark.
        decided.clear()
        third.start()
        # The third request has picked the call it waits on.
        _until_sync(lambda: third.ident in decided)
        reading = await asyncio.wait_for(forced, timeout=3)
        await asyncio.to_thread(third.join, 5)
        return reading, late

    forced, late = asyncio.run(_scenario())

    assert calls == [False, True]
    assert forced["summary"]["run"] == "fresh"
    assert late[0]["summary"]["run"] == "fresh"
    assert webui_api._doctor_inflight is None
    assert webui_api._doctor_pending_forced is None


def test_a_queued_refresh_whose_loop_shuts_down_releases_its_slot(monkeypatch, doctor_state):
    """Task #1926: the loop that queued a forced call shuts down while the call
    waits for promotion. The call must leave the queue and resolve, or the call
    ahead later promotes it into the in-flight slot where nothing runs it."""
    import threading

    calls, gate = doctor_state
    monkeypatch.setattr(webui_api, "_DOCTOR_CACHE_S", 0.0)
    monkeypatch.setattr(webui_api, "_DOCTOR_WAIT_S", 2.0, raising=False)
    first_done = threading.Event()

    def _first():
        asyncio.run(webui_api.health_payload())
        first_done.set()

    threading.Thread(target=_first, daemon=True).start()
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.002)

    async def _queue_then_exit():
        asyncio.ensure_future(webui_api.health_payload(force=True))
        # Queued behind the stalled first call.
        await _until(lambda: webui_api._doctor_pending_forced is not None)
        return webui_api._doctor_pending_forced

    # asyncio.run cancels the still-waiting tasks as it shuts its loop down.
    dead = asyncio.run(_queue_then_exit())

    assert dead is not None
    assert webui_api._doctor_pending_forced is None
    assert dead.future.done()
    gate.set()
    assert first_done.wait(5)
    assert webui_api._doctor_inflight is None

    started = time.monotonic()
    forced = asyncio.run(webui_api.health_payload(force=True))

    assert forced["summary"]["run"] == "fresh"
    assert "error" not in forced
    assert time.monotonic() - started < 1.0
    assert calls == [False, True]


def test_no_write_only_forced_flag_remains():
    assert not hasattr(webui_api, "_doctor_inflight_forced")
