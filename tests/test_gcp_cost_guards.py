"""The GCP cost-guard verifier compares live state with its committed intent (task #1447).

Offline: live GCP responses are built from the committed payloads, then bent one
way at a time to prove each drift is caught.
"""

import copy
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

GUARD_DIR = Path(__file__).resolve().parents[1] / "deploy" / "gcp" / "cost-guards"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location(
        "verify_cost_guards", GUARD_DIR / "verify_cost_guards.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def live(guard):
    intent = guard.load_intent()
    policy = {**copy.deepcopy(intent["egress_policy.json"]), "enabled": True}
    bucket = {
        "lifecycle_config": {"rule": copy.deepcopy(intent["lifecycle.json"]["rule"])},
        "public_access_prevention": "enforced",
    }
    now = datetime.now(UTC)
    snapshots = [
        f"gs://b/memory-db/memory-{now - timedelta(days=i):%Y%m%dT%H%M%SZ}.db" for i in range(30)
    ]
    return intent, policy, bucket, [], snapshots


RECENT = (
    "lifecycle reaches only litestream/ and memory-db/ past retention (recent backups protected)"
)


def failed(guard, *args):
    return [label for ok, label, _ in guard.evaluate(*args) if not ok]


def test_intent_resolves_next_to_the_script_not_a_scratch_path(guard):
    assert guard.HERE == GUARD_DIR
    intent = guard.load_intent()
    assert set(intent) == {"egress_policy.json", "lifecycle.json"}
    thresholds = sorted(
        c["conditionThreshold"]["thresholdValue"]
        for c in intent["egress_policy.json"]["conditions"]
    )
    assert thresholds == [5 * 2**30, 10 * 2**30]
    prefixes = [r["condition"]["matchesPrefix"] for r in intent["lifecycle.json"]["rule"]]
    assert prefixes == [["litestream/"]] * 2 + [["memory-db/"]] * 2


def test_matching_live_state_passes_every_check(guard, live):
    assert failed(guard, *live) == []


def test_disabled_policy_is_drift(guard, live):
    intent, policy, *rest = live
    policy["enabled"] = False
    assert failed(guard, intent, policy, *rest) == ["alert policy enabled"]


def test_raised_threshold_is_drift(guard, live):
    intent, policy, *rest = live
    policy["conditions"][0]["conditionThreshold"]["thresholdValue"] = str(50 * 2**30)
    assert failed(guard, intent, policy, *rest) == ["both thresholds match intent"]


def test_lifecycle_rule_reaching_backups_is_drift(guard, live):
    intent, policy, bucket, *rest = live
    bucket["lifecycle_config"]["rule"][0]["condition"]["matchesPrefix"] = [""]
    assert failed(guard, intent, policy, bucket, *rest) == [
        "lifecycle rules match intent",
        RECENT,
    ]


def test_backup_retention_shortened_below_30_days_is_drift(guard, live):
    intent, policy, bucket, *rest = live
    bucket["lifecycle_config"]["rule"][2]["condition"]["age"] = 7
    assert failed(guard, intent, policy, bucket, *rest) == [
        "lifecycle rules match intent",
        RECENT,
    ]


def test_stale_newest_snapshot_is_drift(guard, live):
    intent, policy, bucket, litestream, snapshots = live
    old = [s.replace(f"{datetime.now(UTC):%Y}", "2025") for s in snapshots]
    assert failed(guard, intent, policy, bucket, litestream, old) == [
        "newest snapshot under 48 h old"
    ]


def test_missing_lifecycle_config_is_drift_not_a_crash(guard, live):
    intent, policy, bucket, *rest = live
    del bucket["lifecycle_config"]
    assert failed(guard, intent, policy, bucket, *rest) == [
        "lifecycle rules match intent",
        RECENT,
    ]


def test_litestream_objects_or_thin_snapshots_are_drift(guard, live):
    intent, policy, bucket, _, snapshots = live
    assert failed(guard, intent, policy, bucket, ["gs://b/litestream/x#1"], snapshots) == [
        "litestream prefix empty (all versions)"
    ]
    assert failed(guard, intent, policy, bucket, [], snapshots[:5]) == [
        "daily memory.db snapshots present"
    ]


def test_unreadable_live_state_exits_2(guard, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise OSError("gcloud not found")

    monkeypatch.setattr(guard, "_gcloud", boom)
    assert guard.main() == 2
    assert "UNKNOWN" in capsys.readouterr().out
