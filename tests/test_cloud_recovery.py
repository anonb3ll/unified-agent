"""Approval-gated GCP worker self-healing (task #1043).

The safety argument is that recovery cannot act on an unclear signal, cannot
run without the approval queue, and cannot loop. Each of those gets a test.
"""

from unittest.mock import patch

import pytest

from unified_agent import storage
from unified_agent.tools.cloud_recovery import (
    COOLDOWN_MINUTES,
    cloud_worker_recover,
    evaluate_recovery,
)


@pytest.fixture(autouse=True)
def _explicit_cloud_configuration(monkeypatch):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_DB_PATH", str(tmp_path / "recovery.db"))
    storage.init_db()
    yield


@pytest.fixture
def _gated(monkeypatch):
    """The live hub gates gcloud_ops; tests that exercise recovery say so."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "gcloud_ops")
    yield


def _inventory(instances, sections=None, status="ok", errors=None):
    return {
        "status": status,
        "project_id": "example-project",
        "instances": [{"zone": "europe-west1-b", **row} for row in instances],
        "sections": sections if sections is not None else {"instances": "ok"},
        "errors": errors or [],
    }


# --------------------------------------------------------------------------- #
# evaluate_recovery: the decision, isolated
# --------------------------------------------------------------------------- #


def test_unreadable_instances_never_eligible():
    """The live false-healthy: an unreadable section must not read as stopped."""
    inv = _inventory([], sections={"instances": "unavailable"})
    decision = evaluate_recovery(inv, "example-worker")

    assert decision["eligible"] is False
    assert decision["state"] == "unknown"


def test_running_instance_not_eligible():
    inv = _inventory([{"name": "example-worker", "status": "RUNNING"}])
    decision = evaluate_recovery(inv, "example-worker")

    assert decision["eligible"] is False
    assert decision["state"] == "running"


def test_terminated_instance_is_eligible():
    inv = _inventory([{"name": "example-worker", "status": "TERMINATED"}])
    decision = evaluate_recovery(inv, "example-worker")

    assert decision["eligible"] is True
    assert decision["state"] == "stopped"


def test_absent_instance_not_eligible():
    """A missing VM is not a restart case -- recovery must not create infrastructure."""
    inv = _inventory([])
    decision = evaluate_recovery(inv, "example-worker")

    assert decision["eligible"] is False
    assert decision["state"] == "absent"


def test_transitional_instance_not_eligible():
    """Acting mid-transition would race the transition."""
    inv = _inventory([{"name": "example-worker", "status": "PROVISIONING"}])
    decision = evaluate_recovery(inv, "example-worker")

    assert decision["eligible"] is False
    assert decision["state"] == "transitional"


# --------------------------------------------------------------------------- #
# cloud_worker_recover: the governed path
# --------------------------------------------------------------------------- #


def _patch(inventory, ops=None):
    ops = ops or (lambda **kw: {"status": "ok"})
    return (
        patch("unified_agent.tools.gcloud.gcloud_inventory", return_value=inventory),
        patch("unified_agent.tools.gcloud.gcloud_ops", side_effect=lambda **kw: ops(**kw)),
    )


def test_stale_health_fails_closed(_gated):
    inv_p, ops_p = _patch(_inventory([], sections={"instances": "unavailable"}))
    with inv_p, ops_p as ops:
        res = cloud_worker_recover(dry_run=False)

    assert res["outcome"] == "unknown"
    assert ops.call_count == 0, "must not act on an unreadable health signal"


@pytest.mark.parametrize(
    "kwargs", [{"target": "other-worker"}, {"project": "other-project"}, {"zone": "other-zone"}]
)
def test_recovery_scope_is_fixed_to_the_configured_worker(_gated, kwargs):
    with pytest.raises(Exception, match="allowed recovery target|server-configured"):
        cloud_worker_recover(**kwargs)


def test_healthy_worker_is_left_alone(_gated):
    inv_p, ops_p = _patch(_inventory([{"name": "example-worker", "status": "RUNNING"}]))
    with inv_p, ops_p as ops:
        res = cloud_worker_recover(dry_run=False)

    assert res["outcome"] == "healthy"
    assert ops.call_count == 0


def test_dry_run_is_the_default_and_changes_nothing(_gated):
    inv_p, ops_p = _patch(_inventory([{"name": "example-worker", "status": "TERMINATED"}]))
    with inv_p, ops_p as ops:
        res = cloud_worker_recover()

    assert res["outcome"] == "would_recover"
    assert ops.call_count == 0, "the default call must not mutate"


def test_refuses_to_run_when_approval_gate_is_inactive(monkeypatch):
    """If the hub's allowlist stops covering gcloud_ops, recovery stops too.

    Otherwise a config change would silently convert an approval-gated action
    into an unattended one.
    """
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate")
    inv_p, ops_p = _patch(_inventory([{"name": "example-worker", "status": "TERMINATED"}]))
    with inv_p, ops_p as ops:
        res = cloud_worker_recover(dry_run=False)

    assert res["outcome"] == "ungoverned"
    assert ops.call_count == 0, "must not self-heal without the approval queue"


def test_pending_approval_is_reported_not_treated_as_failure(_gated):
    class ApprovalPendingError(Exception):
        pass

    def pending(**kw):
        raise ApprovalPendingError("approval 42 pending")

    inv_p, ops_p = _patch(_inventory([{"name": "example-worker", "status": "TERMINATED"}]), pending)
    with inv_p, ops_p:
        res = cloud_worker_recover(dry_run=False)

    assert res["outcome"] == "awaiting_approval"


def test_denial_is_terminal_and_not_retried_into_approval(_gated):
    class ApprovalDeniedError(Exception):
        pass

    calls = []

    def denied(**kw):
        calls.append(kw)
        raise ApprovalDeniedError("denied by Desktop_Person")

    inv = _inventory([{"name": "example-worker", "status": "TERMINATED"}])
    inv_p, ops_p = _patch(inv, denied)
    with inv_p, ops_p:
        first = cloud_worker_recover(dry_run=False)
        second = cloud_worker_recover(dry_run=False)

    assert first["outcome"] == "denied"
    # The retry must be stopped by the cooldown, not converted into a second
    # approval request -- that would be rotating around the denial.
    assert second["outcome"] == "cooldown"
    assert len(calls) == 1


def test_repeat_call_is_blocked_by_cooldown(_gated):
    calls = []

    def ok(**kw):
        calls.append(kw)
        return {"status": "ok"}

    inv = _inventory([{"name": "example-worker", "status": "TERMINATED"}])
    inv_p, ops_p = _patch(inv, ok)
    with inv_p, ops_p:
        first = cloud_worker_recover(dry_run=False)
        second = cloud_worker_recover(dry_run=False)

    assert first["outcome"] == "recovered"
    assert second["outcome"] == "cooldown"
    assert len(calls) == 1, "duplicate execution must not reach the mutating tool"
    assert str(COOLDOWN_MINUTES) in second["detail"]


def test_start_failure_is_reported_as_failed(_gated):
    def boom(**kw):
        raise RuntimeError("compute API timeout")

    inv_p, ops_p = _patch(_inventory([{"name": "example-worker", "status": "TERMINATED"}]), boom)
    with inv_p, ops_p:
        res = cloud_worker_recover(dry_run=False)

    assert res["outcome"] == "failed"
    assert "timeout" in res["detail"]


def test_recovery_only_ever_requests_instance_start(_gated):
    """No destructive action can be reached from this path."""
    seen = []

    def record(**kw):
        seen.append(kw["action"])
        return {"status": "ok"}

    inv = _inventory([{"name": "example-worker", "status": "TERMINATED"}])
    inv_p, ops_p = _patch(inv, record)
    with inv_p, ops_p:
        cloud_worker_recover(dry_run=False)

    assert seen == ["instance_start"]
    assert "instance_stop" not in seen


def test_attempt_is_recorded_on_the_audit_log(_gated):
    from unified_agent import events

    inv = _inventory([{"name": "example-worker", "status": "TERMINATED"}])
    inv_p, ops_p = _patch(inv)
    with inv_p, ops_p:
        cloud_worker_recover(dry_run=False)

    rows = events.query_events(channels=["agent.cloud.recovery"], limit=10)
    outcomes = [(r.get("payload") or {}).get("outcome") for r in rows]
    assert "attempted" in outcomes
    assert "recovered" in outcomes
    assert all(r.get("originator") == "cloud_worker_recover" for r in rows)


# --------------------------------------------------------------------------- #
# Cooldown determinism at scale, expiry, and the tool's own risk annotations
# --------------------------------------------------------------------------- #


def test_recent_attempt_survives_a_channel_busier_than_the_scan_limit(_gated):
    """Regression: the cooldown lookup must not decay as the audit channel grows.

    query_events defaults to ascending ULID, so a bare `limit` returns the
    OLDEST rows. Read that way, once this channel held more than the scan limit
    the real attempt would fall outside the read window entirely and
    _recent_attempt would report "no recent attempt" forever.

    This exercises _recent_attempt directly. Going through cloud_worker_recover
    would not prove anything: the dedupe bucket independently blocks a repeat
    inside the same window, and would mask the bug.
    """
    from datetime import UTC, datetime

    from unified_agent import events
    from unified_agent.tools.cloud_recovery import _ATTEMPT_SCAN_LIMIT, _recent_attempt

    # Channel history from before the window: more rows than the lookup scans.
    for i in range(_ATTEMPT_SCAN_LIMIT + 10):
        events.record_event(
            "agent.cloud.recovery",
            payload={"target": "some-other-instance", "outcome": "attempted", "n": i},
            originator="test-noise",
        )
    # The attempt that matters is the most recent row. Read oldest-first, the
    # scan window is entirely consumed by the history above and never reaches it.
    events.record_event(
        "agent.cloud.recovery",
        payload={"target": "example-worker", "action": "instance_start", "outcome": "denied"},
        originator="cloud_worker_recover",
    )

    found = _recent_attempt("example-worker", datetime.now(UTC))

    assert found is not None, (
        "the recent attempt must still be found when the channel holds more "
        "events than the scan limit"
    )
    assert found["outcome"] == "denied"


def test_cooldown_expires_and_allows_a_later_attempt(_gated):
    """The window is a cooldown, not a permanent lock."""
    from datetime import UTC, datetime, timedelta

    from unified_agent.tools import cloud_recovery

    calls = []

    def ok(**kw):
        calls.append(kw)
        return {"status": "ok"}

    inv = _inventory([{"name": "example-worker", "status": "TERMINATED"}])
    inv_p, ops_p = _patch(inv, ok)
    with inv_p, ops_p:
        first = cloud_worker_recover(dry_run=False)

        # Advance the module's clock past the cooldown window. One seam moves
        # both the window check and the dedupe bucket, which is the point of
        # having a single clock.
        later = datetime.now(UTC) + timedelta(minutes=COOLDOWN_MINUTES + 1)
        with patch.object(cloud_recovery, "_now", lambda: later):
            second = cloud_worker_recover(dry_run=False)

    assert first["outcome"] == "recovered"
    assert second["outcome"] == "recovered", "a new attempt must be allowed after the window"
    assert len(calls) == 2


def test_recovery_tool_is_annotated_destructive_and_non_idempotent():
    """An MCP client must not be told this is safe to retry or read-only."""
    from unified_agent.server import (
        _DESTRUCTIVE_TOOLS,
        _READ_ONLY_TOOLS,
        REGISTERED_TOOLS,
        _tool_annotations,
    )

    assert "cloud_worker_recover" in REGISTERED_TOOLS
    assert "cloud_worker_recover" not in _READ_ONLY_TOOLS
    assert "cloud_worker_recover" in _DESTRUCTIVE_TOOLS

    anno = _tool_annotations(REGISTERED_TOOLS["cloud_worker_recover"])
    assert anno.read_only_hint is False
    assert anno.destructive_hint is True
    assert anno.idempotent_hint is False


def test_ungoverned_refusal_precedes_any_audit_write(monkeypatch):
    """A refused run must not leave an attempt record that would cool down a real one."""
    from unified_agent import events

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "")
    inv_p, ops_p = _patch(_inventory([{"name": "example-worker", "status": "TERMINATED"}]))
    with inv_p, ops_p:
        res = cloud_worker_recover(dry_run=False)

    assert res["outcome"] == "ungoverned"
    rows = events.query_events(channels=["agent.cloud.recovery"], limit=10)
    assert rows == [], "refusing to act must not consume the cooldown window"


def test_dry_run_writes_no_audit_attempt(_gated):
    """A proposal is not an attempt; it must not consume the cooldown window."""
    from unified_agent import events

    inv_p, ops_p = _patch(_inventory([{"name": "example-worker", "status": "TERMINATED"}]))
    with inv_p, ops_p:
        res = cloud_worker_recover()

    assert res["outcome"] == "would_recover"
    assert events.query_events(channels=["agent.cloud.recovery"], limit=10) == []


def test_recovery_binds_server_target_before_reading_inventory(_gated, monkeypatch):
    from unittest.mock import Mock

    from unified_agent.tools import gcloud

    monkeypatch.setenv("UA_GCP_PROJECT", "selected-project")
    monkeypatch.setenv("UA_GCP_WORKER", "selected-worker")
    seen = []

    def inventory(**kwargs):
        seen.append(kwargs)
        monkeypatch.setenv("UA_GCP_PROJECT", "changed-project")
        monkeypatch.setenv("UA_GCP_WORKER", "changed-worker")
        return _inventory([{"name": "selected-worker", "status": "TERMINATED"}])

    operation = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(gcloud, "gcloud_inventory", inventory)
    monkeypatch.setattr(gcloud, "gcloud_ops", operation)
    result = cloud_worker_recover(target="selected-worker", dry_run=False)
    assert result["outcome"] == "recovered"
    assert result["target"] == "selected-worker"
    assert seen == [{"project": "selected-project", "sections": ["instances"]}]
    operation.assert_called_once_with(
        action="instance_start",
        target="selected-worker",
        project="selected-project",
        zone="europe-west1-b",
    )


def test_recovery_uses_matching_worker_zone_not_other_instance(_gated):
    inv = _inventory(
        [
            {"name": "other-worker", "status": "RUNNING", "zone": "us-central1-a"},
            {"name": "example-worker", "status": "TERMINATED", "zone": "europe-west1-b"},
        ]
    )
    inv_p, ops_p = _patch(inv)
    with inv_p, ops_p as ops:
        result = cloud_worker_recover(dry_run=False)
    assert result["outcome"] == "recovered"
    ops.assert_called_once_with(
        action="instance_start",
        target="example-worker",
        project="example-project",
        zone="europe-west1-b",
    )


@pytest.mark.parametrize("zone", [None, "", "--zone=us-central1-a", "region/*", "not-a-zone"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_unverified_worker_zone_never_consumes_cooldown(_gated, monkeypatch, zone, dry_run):
    from unified_agent.tools import cloud_recovery

    inv = _inventory([{"name": "example-worker", "status": "TERMINATED", "zone": zone}])
    if zone is None:
        inv["instances"][0].pop("zone")
    with (
        patch.object(cloud_recovery, "_recent_attempt") as recent,
        patch.object(cloud_recovery.events, "record_event_once") as audit,
    ):
        inv_p, ops_p = _patch(inv)
        with inv_p, ops_p as ops:
            result = cloud_worker_recover(dry_run=dry_run)
    assert result["outcome"] == "unknown"
    assert "zone" in result["detail"]
    recent.assert_not_called()
    audit.assert_not_called()
    ops.assert_not_called()


@pytest.mark.parametrize("first_state", ["RUNNING", "TERMINATED"])
def test_duplicate_worker_names_never_select_arbitrary_zone(_gated, first_state):
    from unified_agent.tools import cloud_recovery

    inv = _inventory(
        [
            {"name": "example-worker", "status": first_state, "zone": "europe-west1-b"},
            {"name": "example-worker", "status": "TERMINATED", "zone": "us-central1-a"},
        ]
    )
    with (
        patch.object(cloud_recovery, "_recent_attempt") as recent,
        patch.object(cloud_recovery.events, "record_event_once") as audit,
    ):
        inv_p, ops_p = _patch(inv)
        with inv_p, ops_p as ops:
            result = cloud_worker_recover(dry_run=False)
    assert result["outcome"] == "unknown"
    assert "ambiguous" in result["detail"]
    recent.assert_not_called()
    audit.assert_not_called()
    ops.assert_not_called()
