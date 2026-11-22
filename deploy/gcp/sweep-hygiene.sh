#!/usr/bin/env bash
# Sweep GCP resource hygiene and budget burn for task #536.
# Non-secret output only. Exits 0 if all invariants hold, 1 if warnings found.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/gcp/lib.sh
source "${SCRIPT_DIR}/lib.sh"

ua_gcp_require_gcloud

PROJECT="$(ua_gcp_project)"
ua_gcp_require_project "$PROJECT"

REGION="${UA_GCP_REGION:-us-central1}"
ZONE="${UA_GCP_ZONE:-us-central1-a}"
BUCKET="$(ua_gcp_bucket_name)"

echo "=== GCP Resource & Budget Hygiene Sweep ==="
echo "Project: $PROJECT"
echo "Region / Zone: $REGION / $ZONE"
echo "Bucket: $BUCKET"
echo ""

WARNINGS=0

# 1. Check Compute instances and machine types (e2-micro invariant)
echo "--- 1. Compute Instances ---"
if ! INSTANCES_JSON="$(gcloud compute instances list --project="$PROJECT" --format=json 2>&1)"; then
  echo "  [WARN] gcloud compute instances list failed: ${INSTANCES_JSON}"
  WARNINGS=$((WARNINGS + 1))
else
  python3 -c '
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception as e:
    print(f"  [WARN] Failed to parse instances JSON: {e}")
    sys.exit(2)
if not data:
    print("  [OK] No compute instances running.")
    sys.exit(0)
print(f"  Found {len(data)} instance(s):")
warnings = 0
for inst in data:
    name = inst.get("name", "unknown")
    status = inst.get("status", "unknown")
    mtype = inst.get("machineType", "").split("/")[-1]
    ext_addrs = []
    for nic in inst.get("networkInterfaces", []):
        for ac in nic.get("accessConfigs", []):
            nat = ac.get("natIP")
            if nat:
                ext_addrs.append(nat)
        for ac in nic.get("ipv6AccessConfigs", []):
            v6 = ac.get("externalIpv6")
            if v6:
                ext_addrs.append(v6)
    ext_addr = ext_addrs[0] if ext_addrs else "none"
    print(f"    - {name} (status: {status}, type: {mtype}, external_addr: {ext_addr})")
    if mtype != "e2-micro":
        print(f"    [WARN] Instance {name} is machine type {mtype} (expected e2-micro)")
        warnings += 1
    if ext_addrs:
        joined = ", ".join(ext_addrs)
        print(
            f"    [WARN] Instance {name} has external address(es) {joined} "
            "(expected Cloud NAT without external IP)"
        )
        warnings += 1
if warnings > 0:
    sys.exit(2)
' "$INSTANCES_JSON" || {
    # Count any non-zero exit (exit 2 = intentional WARN; exit 1 = parse/runtime
    # failure from mixed stdout/stderr JSON). Silent ignore of exit 1 hid failures.
    rc=$?
    if [[ $rc -ne 0 ]]; then
      WARNINGS=$((WARNINGS + 1))
    fi
  }
fi
echo ""

# 2. Check Static/Unattached External IPs
echo "--- 2. Unattached IP / Address Inventory ---"
if ! ADDRS_JSON="$(gcloud compute addresses list --project="$PROJECT" --format=json 2>&1)"; then
  echo "  [WARN] gcloud compute addresses list failed: ${ADDRS_JSON}"
  WARNINGS=$((WARNINGS + 1))
else
  python3 -c '
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception as e:
    print(f"  [WARN] Failed to parse addresses JSON: {e}")
    sys.exit(2)
unattached = [a.get("name") for a in data if a.get("status") != "IN_USE"]
if unattached:
    joined = ", ".join(str(x) for x in unattached)
    print(f"  [WARN] Found {len(unattached)} unattached external IP address(es): {joined}")
    sys.exit(2)
else:
    print("  [OK] No idle unattached external IP addresses.")
' "$ADDRS_JSON" || {
    rc=$?
    if [[ $rc -ne 0 ]]; then
      WARNINGS=$((WARNINGS + 1))
    fi
  }
fi
echo ""

# 3. Check Budget Alerts
echo "--- 3. Budget Alerts ---"
if ! PROJECT_JSON="$(gcloud projects describe "$PROJECT" --format=json 2>&1)"; then
  echo "  [WARN] gcloud projects describe failed: ${PROJECT_JSON}"
  WARNINGS=$((WARNINGS + 1))
elif ! BILLING_JSON="$(gcloud billing projects describe "$PROJECT" --format=json 2>&1)"; then
  echo "  [WARN] gcloud billing projects describe failed: ${BILLING_JSON}"
  WARNINGS=$((WARNINGS + 1))
else
  BILLING_ACCT="$(python3 -c '
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
acct = data.get("billingAccountName", "")
print(acct.split("/", 1)[1] if acct.startswith("billingAccounts/") else "")
' "$BILLING_JSON" 2>/dev/null || true)"
  if [[ -z "$BILLING_ACCT" ]]; then
    echo "  [WARN] Project $PROJECT has no linked billing account."
    WARNINGS=$((WARNINGS + 1))
  elif ! BUDGETS_JSON="$(gcloud billing budgets list --billing-account="$BILLING_ACCT" --format=json 2>&1)"; then
    echo "  [WARN] gcloud billing budgets list failed: ${BUDGETS_JSON}"
    WARNINGS=$((WARNINGS + 1))
  else
    python3 -c '
import json, sys
try:
    project_data = json.loads(sys.argv[1])
    data = json.loads(sys.argv[2])
except Exception as e:
    print(f"  [WARN] Failed to parse budgets/project JSON: {e}")
    sys.exit(2)
proj_number = project_data.get("projectNumber")
scoped = [
    b for b in data
    if proj_number and f"projects/{proj_number}" in b.get("budgetFilter", {}).get("projects", [])
]
if not scoped:
    print("  [WARN] No billing budget scoped to this project.")
    sys.exit(2)
print(f"  [OK] Found {len(scoped)} active budget alert(s) scoped to this project:")
for b in scoped:
    display = b.get("displayName", "unnamed")
    amt = b.get("amount", {}).get("specifiedAmount", {})
    currency = amt.get("currencyCode", "USD")
    units = amt.get("units", "0")
    print(f"    - {display}: {units} {currency}")
' "$PROJECT_JSON" "$BUDGETS_JSON" || {
      rc=$?
    if [[ $rc -ne 0 ]]; then
      WARNINGS=$((WARNINGS + 1))
    fi
  }
  fi
fi
echo ""

# 4. Check GCS Backup Bucket and Recent Objects
echo "--- 4. GCS Backup Object Recency ---"
MAX_BACKUP_AGE_HOURS="${UA_BACKUP_MAX_AGE_HOURS:-36}"
# Pass threshold via argv — never interpolate env into Python source (injection risk).
if ! python3 -c 'import math, sys; v=float(sys.argv[1]); exit(0 if math.isfinite(v) and v > 0 else 1)' "$MAX_BACKUP_AGE_HOURS" 2>/dev/null; then
  echo "  [WARN] Invalid UA_BACKUP_MAX_AGE_HOURS=${MAX_BACKUP_AGE_HOURS} (must be a finite number > 0)"
  WARNINGS=$((WARNINGS + 1))
elif ! GCS_OBJECTS_JSON="$(gcloud storage ls --json "gs://${BUCKET}/memory-db/**" 2>&1)"; then
  echo "  [WARN] gcloud storage ls failed: ${GCS_OBJECTS_JSON}"
  WARNINGS=$((WARNINGS + 1))
else
  python3 -c '
import json, sys, math
from datetime import datetime, timezone

raw = sys.argv[1]
try:
    max_age_hours = float(sys.argv[2])
except ValueError:
    print(f"  [WARN] Invalid UA_BACKUP_MAX_AGE_HOURS={sys.argv[2]!r} (must be a finite number > 0)")
    sys.exit(2)
if not math.isfinite(max_age_hours) or max_age_hours <= 0:
    print(f"  [WARN] Invalid UA_BACKUP_MAX_AGE_HOURS={sys.argv[2]!r} (must be a finite number > 0)")
    sys.exit(2)
bucket = sys.argv[3]

try:
    data = json.loads(raw)
except Exception as e:
    print(f"  [WARN] Failed to parse storage JSON: {e}")
    sys.exit(2)

if not data:
    print(f"  [WARN] No backup objects found under gs://{bucket}/memory-db/")
    sys.exit(2)

newest_dt = None
newest_name = None
now = datetime.now(timezone.utc)

for item in data:
    meta = item.get("metadata", {})
    name = meta.get("name") or item.get("url", "")
    tc = meta.get("timeCreated")
    if tc:
        try:
            dt = datetime.fromisoformat(tc.replace("Z", "+00:00"))
            if newest_dt is None or dt > newest_dt:
                newest_dt = dt
                newest_name = name
        except Exception:
            pass

if newest_dt is None:
    print(f"  [WARN] Could not determine creation timestamps for backup objects in gs://{bucket}/memory-db/")
    sys.exit(2)

age_hours = (now - newest_dt).total_seconds() / 3600.0
print(f"  Found {len(data)} backup object(s). Newest: {newest_name} ({age_hours:.1f}h old, created {newest_dt.isoformat()})")

if age_hours > max_age_hours:
    print(f"  [WARN] Most recent backup is {age_hours:.1f}h old (threshold: {max_age_hours}h)")
    sys.exit(2)
else:
    print(f"  [OK] Recent backup is within {max_age_hours}h threshold.")
' "$GCS_OBJECTS_JSON" "$MAX_BACKUP_AGE_HOURS" "$BUCKET" || {
    rc=$?
    if [[ $rc -ne 0 ]]; then
      WARNINGS=$((WARNINGS + 1))
    fi
  }
fi
echo ""

# 5. Check Prohibited Vertex / AI Services
echo "--- 5. Service Usage / Drift Check ---"
if ! SERVICES_JSON="$(gcloud services list --enabled --project="$PROJECT" --format=json 2>&1)"; then
  echo "  [WARN] gcloud services list failed: ${SERVICES_JSON}"
  WARNINGS=$((WARNINGS + 1))
else
  python3 -c '
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception as e:
    print(f"  [WARN] Failed to parse services JSON: {e}")
    sys.exit(2)
forbidden = {"aiplatform.googleapis.com", "cloudaicompanion.googleapis.com", "generativelanguage.googleapis.com"}
enabled = {s.get("config", {}).get("name") for s in data}
matched = forbidden & enabled
if matched:
    joined = ", ".join(sorted(matched))
    print(f"  [WARN] Prohibited AI service(s) enabled on Cloud credit project: {joined}")
    sys.exit(2)
else:
    print("  [OK] No forbidden Vertex/AI APIs enabled.")
' "$SERVICES_JSON" || {
    rc=$?
    if [[ $rc -ne 0 ]]; then
      WARNINGS=$((WARNINGS + 1))
    fi
  }
fi
echo ""

# Summary
echo "=== Sweep Summary ==="
if [[ "$WARNINGS" -eq 0 ]]; then
  echo "Status: CLEAN (0 warnings)"
  exit 0
else
  echo "Status: WARNINGS FOUND ($WARNINGS warning(s))"
  exit 1
fi
