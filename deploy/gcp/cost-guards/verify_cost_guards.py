#!/usr/bin/env python3
"""Verify the Sep-2026 GCP cost guards still hold (project ua-worker, task #1447).

Intent versus live: reads the applied payloads that sit next to this file
(``egress_policy.json``, ``lifecycle.json``), reads the live alert policy and
backups bucket back from GCP, and diffs them. Exits 1 on any mismatch and 2 when
live state cannot be read at all. Read-only: it never changes a GCP resource.

History: written during the #1410 litestream egress incident and kept in a temp
directory. The Linux desktop rebuild of the desktop (2026-09-27) wiped that directory,
which is the loss #1447 was filed to prevent, so it now lives here.

Run from any host with an authenticated ``gcloud`` (the Mac hub has one):

    python3 deploy/gcp/cost-guards/verify_cost_guards.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = "ua-worker"
BUCKET = "ua-worker-backups-000000000000"
POLICY_ID = "12402854993839794509"
MIN_SNAPSHOTS = 25
# Snapshots past this age are deleted by the memory-db/ lifecycle rule (Operator,
# 2026-11-18), so fewer than 30 live objects is normal and a recent one is required.
SNAPSHOT_RETENTION_DAYS = 30
MAX_SNAPSHOT_AGE = timedelta(hours=48)


def load_intent(directory: Path = HERE) -> dict[str, dict]:
    """The applied payloads, resolved next to this script rather than a scratch path."""
    return {
        name: json.loads((directory / name).read_text())
        for name in ("egress_policy.json", "lifecycle.json")
    }


def evaluate(
    intent: dict[str, dict],
    policy: dict,
    bucket: dict,
    litestream_objects: list[str],
    snapshot_objects: list[str],
    now: datetime | None = None,
) -> list[tuple[bool, str, str]]:
    """Compare live state with intent. Returns (ok, label, detail) per check."""
    newest = _newest_snapshot(snapshot_objects)
    age = (now or datetime.now(UTC)) - newest if newest else None
    want = intent["egress_policy.json"]
    want_rules = intent["lifecycle.json"]["rule"]
    live_rules = (bucket.get("lifecycle_config") or {}).get("rule") or []

    def thresholds(p: dict) -> list[int]:
        return sorted(
            int(c["conditionThreshold"]["thresholdValue"]) for c in p.get("conditions", [])
        )

    return [
        (policy.get("enabled") is True, "alert policy enabled", ""),
        (
            policy.get("displayName") == want["displayName"],
            "alert policy displayName matches",
            "",
        ),
        (
            policy.get("notificationChannels") == want["notificationChannels"],
            "alert policy targets the email channel",
            "",
        ),
        (
            thresholds(policy) == thresholds(want),
            "both thresholds match intent",
            str(thresholds(policy)),
        ),
        (
            _canon(live_rules) == _canon(want_rules),
            "lifecycle rules match intent",
            "",
        ),
        (
            bool(live_rules) and all(_spares_recent_backups(r) for r in live_rules),
            "lifecycle reaches only litestream/ and memory-db/ past retention (recent backups protected)",
            "",
        ),
        (
            bucket.get("public_access_prevention") == "enforced",
            "public access prevention enforced",
            "",
        ),
        (litestream_objects == [], "litestream prefix empty (all versions)", ""),
        (
            len(snapshot_objects) >= MIN_SNAPSHOTS,
            "daily memory.db snapshots present",
            f"{len(snapshot_objects)} objects",
        ),
        (
            age is not None and age <= MAX_SNAPSHOT_AGE,
            "newest snapshot under 48 h old",
            f"newest {newest:%Y-%m-%dT%H:%MZ}" if newest else "no dated snapshot",
        ),
    ]


def _canon(rules: list[dict]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rules)


def _spares_recent_backups(rule: dict) -> bool:
    """A rule may clear litestream/, or memory-db/ copies past retention, nothing else."""
    cond = rule.get("condition", {})
    prefix = cond.get("matchesPrefix")
    if prefix == ["litestream/"]:
        return True
    if prefix != ["memory-db/"]:
        return False
    if cond.get("isLive") is False:
        return True  # noncurrent copies left behind by a delete
    return cond.get("age", 0) >= SNAPSHOT_RETENTION_DAYS and set(cond) <= {
        "age",
        "isLive",
        "matchesPrefix",
    }


def _newest_snapshot(objects: list[str]) -> datetime | None:
    """Newest timestamp in names like memory-20261117T081506Z.db; others are ignored."""
    stamps = []
    for url in objects:
        name = url.rsplit("/", 1)[-1].split("#", 1)[0]
        try:
            stamps.append(datetime.strptime(name, "memory-%Y%m%dT%H%M%SZ.db").replace(tzinfo=UTC))
        except ValueError:
            continue
    return max(stamps, default=None)


def _gcloud(*args: str, tolerate: str | None = None) -> str:
    """Run gcloud. ``tolerate`` marks a non-zero exit as expected when its text appears."""
    r = subprocess.run(["gcloud", *args], capture_output=True, text=True)
    if r.returncode and (tolerate is None or tolerate not in r.stdout + r.stderr):
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r.stdout


def _ls(url: str, *, all_versions: bool = False) -> list[str]:
    flags = ["--all-versions"] if all_versions else []
    out = _gcloud("storage", "ls", *flags, url, tolerate="matched no objects")
    return [line for line in out.splitlines() if line.strip()]


def _alert_policy(token: str) -> dict:
    req = urllib.request.Request(
        f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/alertPolicies/{POLICY_ID}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    intent = load_intent()
    try:
        token = _gcloud("auth", "print-access-token").strip()
        policy = _alert_policy(token)
        bucket = json.loads(
            _gcloud("storage", "buckets", "describe", f"gs://{BUCKET}", "--format=json")
        )
        # Noncurrent versions count: a deleted-but-versioned replica still bills.
        litestream = _ls(f"gs://{BUCKET}/litestream/", all_versions=True)
        snapshots = _ls(f"gs://{BUCKET}/memory-db/")
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"UNKNOWN: could not read live GCP state: {type(exc).__name__}: {exc}")
        return 2

    results = evaluate(intent, policy, bucket, litestream, snapshots)
    for ok, label, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    failed = [label for ok, label, _ in results if not ok]
    print()
    if failed:
        print(f"COST GUARDS DRIFTED ({len(failed)}): {'; '.join(failed)}")
        return 1
    print(f"COST GUARDS HOLD: {len(results)}/{len(results)} checks match applied intent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
