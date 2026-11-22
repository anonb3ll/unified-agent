#!/usr/bin/env python3
"""Deterministic validation + rendering for the Unified-Agent capability catalog.

Authoritative catalog: docs/capability-catalog/catalog.json
Schema (documentation + external tooling): docs/capability-catalog/catalog.schema.json

This script is the source of truth for CI. It is intentionally dependency-free
(no jsonschema import) so it runs in any environment, including a locked-down
CI image. It enforces:

* the ten required families with the five pilot families provably covered;
* allowed `implementation_state`, `connection_state`, evidence type, entry-point
  kind, origin kind, and wedge enums;
* every required field present and non-empty, including consumer, evidence,
  boundary, and freshness fields (acceptance criterion 2);
* connection-state honesty: a `live` claim requires a dated live evidence row and
  may never be inferred from code/config alone (acceptance criterion 3);
* historical-emulation guard: `consumer == "no verified consumer"` can never be
  classified `shipped-and-used` (acceptance criterion 4);
* queue coverage: every task reference named by the task spec must appear in
  `queue_coverage` so no duplicate work is filed (acceptance criterion 6);
* at least five traceable paths that reference real item ids (criterion 7);
* freshness fields: as_of, source baseline, origin main SHA, verification limits,
  and update instructions (criterion 8).

Usage:
    python scripts/validate_capability_catalog.py            # validate + print report
    python scripts/validate_capability_catalog.py --render   # validate + render catalog.md

Exit code 0 = valid; 1 = validation failure; 2 = usage/render error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "docs" / "capability-catalog" / "catalog.json"
SCHEMA_PATH = REPO_ROOT / "docs" / "capability-catalog" / "catalog.schema.json"
RENDER_PATH = REPO_ROOT / "docs" / "capability-catalog" / "catalog.md"

FAMILY_IDS = {
    "governance-evidence",
    "dispatch-delegation",
    "workroom-multi-harness",
    "langgraph-planning",
    "provider-quota",
    "first-mate",
    "telemetry-utilization",
    "integrations-surfaces",
    "memory-vault-gbrain",
    "browser-boundary",
}
PILOT_FAMILIES = {
    "governance-evidence",
    "dispatch-delegation",
    "langgraph-planning",
    "provider-quota",
    "first-mate",
}
MIN_ITEMS_PILOT = 4
MIN_ITEMS_OTHER = 2

IMPLEMENTATION_STATES = {
    "shipped-and-used",
    "shipped-but-unconsumed",
    "partially-wired",
    "dormant/uncertain",
    "superseded",
    "speculative",
    "blocked",
}
CONNECTION_STATES = {
    "live",
    "degraded",
    "error",
    "unknown",
    "dormant",
    "misconfigured",
    "blocked",
    "not-applicable",
}
EVIDENCE_TYPES = {"code", "test", "ci", "deployed", "live"}
ENTRY_KINDS = {"mcp-tool", "cli", "web-ui", "library", "event-hook", "config", "not-applicable"}
ORIGIN_KINDS = {"task", "pr", "adr", "source", "decision"}
WEDGES = {"evidence-wedge", "expansion-slice"}

# Acceptance criterion 6: existing queue coverage that this catalog must not duplicate.
REQUIRED_QUEUE_COVERAGE = [
    "935",
    "944",
    "1024",
    "1032",
    "1043",
    "1044",
    "1045",
    "1046",
    "1047",
    "1048",
    "1050",
    "1051",
    "1052",
    "1053",
    "1054",
    "1055",
    "1056",
    "1063",
]

_NO_VERIFIED_CONSUMER = "no verified consumer"
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ValidationError(Exception):
    pass


def _check(cond: bool, message: str) -> None:
    if not cond:
        raise ValidationError(message)


def _nonempty(value: object, label: str) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"{label} must be a non-empty string")
    if isinstance(value, (list, dict)) and len(value) == 0:
        raise ValidationError(f"{label} must be non-empty")


def validate(catalog: dict) -> dict:
    """Return a report dict; raise ValidationError on any failure."""
    report: dict = {}

    # --- freshness --------------------------------------------------------
    fresh = catalog.get("freshness", {})
    _nonempty(fresh.get("as_of"), "freshness.as_of")
    if not _ISO_DATE.match(fresh["as_of"]):
        raise ValidationError("freshness.as_of must be an ISO date YYYY-MM-DD")
    if fresh["as_of"] > date.today().isoformat():
        raise ValidationError("freshness.as_of must not be in the future")
    _nonempty(fresh.get("source_baseline_sha"), "freshness.source_baseline_sha")
    _nonempty(fresh.get("origin_main_sha"), "freshness.origin_main_sha")
    _nonempty(fresh.get("live_hub_checked_at"), "freshness.live_hub_checked_at")
    if (
        not isinstance(fresh.get("verification_limits"), list)
        or len(fresh["verification_limits"]) == 0
    ):
        raise ValidationError("freshness.verification_limits must be a non-empty list")
    if (
        not isinstance(fresh.get("update_instructions"), list)
        or len(fresh["update_instructions"]) == 0
    ):
        raise ValidationError("freshness.update_instructions must be a non-empty list")
    report["as_of"] = fresh["as_of"]
    report["source_baseline_sha"] = fresh["source_baseline_sha"]

    # --- families ---------------------------------------------------------
    family_ids = {f["id"] for f in catalog.get("families", [])}
    if family_ids != FAMILY_IDS:
        missing = sorted(FAMILY_IDS - family_ids)
        extra = sorted(family_ids - FAMILY_IDS)
        raise ValidationError(
            f"families must be exactly the ten required ids; missing={missing}, extra={extra}"
        )
    family_labels = {f["id"]: f["label"] for f in catalog["families"]}

    # --- items ------------------------------------------------------------
    items = catalog.get("items", [])
    _check(len(items) >= 40, f"catalog must have at least 40 items (found {len(items)})")
    ids = [it.get("id") for it in items]
    _check(len(ids) == len(set(ids)), "item ids must be unique")
    _check(
        all(bool(i) and bool(re.match(r"^[a-z0-9-]+$", i)) for i in ids),
        "item ids must match ^[a-z0-9-]+$",
    )

    per_family: dict[str, list] = defaultdict(list)
    counts: Counter = Counter()
    live_claim_without_live_evidence: list[str] = []
    for it in items:
        iid = it.get("id", "?")
        fam = it.get("family", "")
        _check(fam in FAMILY_IDS, f"{iid}: family '{fam}' is not a required family")
        per_family[fam].append(iid)
        _check(it.get("capability", "").strip(), f"{iid}: capability required")
        _check(it.get("customer_purpose", "").strip(), f"{iid}: customer_purpose required")
        _check(it.get("wedge") in WEDGES, f"{iid}: wedge must be one of {sorted(WEDGES)}")
        counts[it["wedge"]] += 1

        state = it.get("implementation_state")
        _check(state in IMPLEMENTATION_STATES, f"{iid}: implementation_state '{state}' not allowed")
        counts[("state", state)] += 1

        conn = it.get("connection_state")
        _check(conn in CONNECTION_STATES, f"{iid}: connection_state '{conn}' not allowed")
        counts[("conn", conn)] += 1

        consumer = it.get("consumer", "")
        _check(isinstance(consumer, str) and consumer.strip(), f"{iid}: consumer required")
        consumer_literal = consumer.strip()
        if consumer_literal == _NO_VERIFIED_CONSUMER:
            _check(
                state != "shipped-and-used",
                f"{iid}: consumer is '{_NO_VERIFIED_CONSUMER}' but implementation_state is "
                "'shipped-and-used' — a historical emulation cannot be claimed live without "
                "a current source path, callable entry point, real consumer, and current evidence",
            )

        origin = it.get("origin", {})
        _check(origin.get("kind") in ORIGIN_KINDS, f"{iid}: origin.kind not allowed")
        _check(
            isinstance(origin.get("refs"), list) and origin["refs"], f"{iid}: origin.refs required"
        )

        ep = it.get("entry_point", {})
        _check(ep.get("kind") in ENTRY_KINDS, f"{iid}: entry_point.kind not allowed")
        _nonempty(ep.get("ref"), f"{iid}: entry_point.ref")

        ev = it.get("evidence")
        _check(isinstance(ev, list) and len(ev) >= 1, f"{iid}: at least one evidence row required")
        ev_types: set[str] = set()
        for row in ev:
            _check(row.get("type") in EVIDENCE_TYPES, f"{iid}: evidence type not allowed")
            ev_types.add(row["type"])
            _nonempty(row.get("ref"), f"{iid}: evidence.ref")
            _nonempty(row.get("detail"), f"{iid}: evidence.detail")
            if row["type"] == "live":
                _check(
                    bool(row.get("date")) and bool(_ISO_DATE.match(row["date"])),
                    f"{iid}: live evidence requires a YYYY-MM-DD date",
                )
        if conn == "live":
            if "live" not in ev_types:
                live_claim_without_live_evidence.append(iid)
            # live requires at least one dated live row OR a dated exploration note; treat
            # 'live' evidence as mandatory below (hard rule).

        b = it.get("boundaries", {})
        for field in ("security", "identity", "approval", "privacy", "release", "tenant_workspace"):
            _nonempty(b.get(field), f"{iid}: boundaries.{field}")

        incomplete = it.get("incomplete")
        if state != "shipped-and-used":
            _check(
                isinstance(incomplete, dict),
                f"{iid}: incomplete required for state '{state}' (owner, smallest_next_action, non_action)",
            )
            _nonempty(incomplete.get("owner"), f"{iid}: incomplete.owner")
            _nonempty(
                incomplete.get("smallest_next_action"), f"{iid}: incomplete.smallest_next_action"
            )
            _nonempty(incomplete.get("non_action"), f"{iid}: incomplete.non_action")
        else:
            counts[("complete", True)] += 1

    _check(
        not live_claim_without_live_evidence,
        "connection_state 'live' requires at least one evidence row of type 'live': "
        + ", ".join(live_claim_without_live_evidence),
    )

    # family coverage
    for fam in PILOT_FAMILIES:
        _check(
            len(per_family.get(fam, [])) >= MIN_ITEMS_PILOT,
            f"pilot family '{fam}' needs >= {MIN_ITEMS_PILOT} items (found {len(per_family.get(fam, []))})",
        )
    for fam in FAMILY_IDS - PILOT_FAMILIES:
        _check(
            len(per_family.get(fam, [])) >= MIN_ITEMS_OTHER,
            f"family '{fam}' needs >= {MIN_ITEMS_OTHER} items (found {len(per_family.get(fam, []))})",
        )

    # --- queue coverage (acceptance criterion 6) --------------------------
    covered = {
        re.sub(r"^#", "", str(q.get("task_ref", ""))) for q in catalog.get("queue_coverage", [])
    }
    missing_q = [ref for ref in REQUIRED_QUEUE_COVERAGE if ref not in covered]
    _check(
        not missing_q,
        "queue_coverage must mention every task ref named by the spec; missing: "
        + ", ".join("#" + m for m in missing_q),
    )

    # --- traceable paths (acceptance criterion 7) -------------------------
    paths = catalog.get("traceable_paths", [])
    _check(len(paths) >= 5, f"traceable_paths must have >= 5 entries (found {len(paths)})")
    id_set = set(ids)
    for p in paths:
        _check(
            p.get("capability_id") in id_set,
            f"traceable_paths references unknown capability_id {p.get('capability_id')!r}",
        )
        _nonempty(p.get("consumer"), f"traceable_paths consumer for {p.get('capability_id')}")
        _nonempty(
            p.get("behavior_evidence"),
            f"traceable_paths behavior_evidence for {p.get('capability_id')}",
        )
        _nonempty(
            p.get("owner_or_closure"),
            f"traceable_paths owner_or_closure for {p.get('capability_id')}",
        )

    report["items"] = len(items)
    report["per_family"] = {f: sorted(v) for f, v in sorted(per_family.items())}
    report["counts"] = counts
    report["family_labels"] = family_labels
    return report


def load_catalog(path: Path = CATALOG_PATH) -> dict:
    if not path.exists():
        raise ValidationError(f"catalog not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def render(catalog: dict, report: dict) -> str:
    """Render catalog.md (human view) from the authoritative catalog.json."""
    fam_order = [f["id"] for f in catalog["families"]]
    fresh = catalog["freshness"]
    items_by_family = {
        fid: [it for it in catalog["items"] if it["family"] == fid] for fid in fam_order
    }

    lines: list[str] = []
    lines.append("# Unified-Agent capability catalog and ledger")
    lines.append("")
    lines.append(
        "> Rendered view. The authoritative structured artifact is "
        "[`catalog.json`](./catalog.json); edit that file and re-run "
        "`python scripts/validate_capability_catalog.py --render`. This view is generated "
        "and must not be hand-edited."
    )
    lines.append("")
    lines.append(f"- Freshness date (as_of): **{fresh['as_of']}**")
    lines.append(f"- Source baseline SHA: `{fresh['source_baseline_sha']}`")
    lines.append(f"- origin/main SHA: `{fresh['origin_main_sha']}`")
    lines.append(f"- Live hub checked at: `{fresh['live_hub_checked_at']}`")
    lines.append(f"- Items: **{report['items']}** across {len(fam_order)} families")
    lines.append("")
    lines.append("## Classification counts")
    lines.append("")
    lines.append("| Classification | Count |")
    lines.append("|---|---|")
    for state in sorted({k[1] for k in report["counts"] if k[0] == "state"}):
        lines.append(f"| implementation_state: {state} | {report['counts'][('state', state)]} |")
    for conn in sorted({k[1] for k in report["counts"] if k[0] == "conn"}):
        lines.append(f"| connection_state: {conn} | {report['counts'][('conn', conn)]} |")
    lines.append(f"| wedge: evidence-wedge | {report['counts'].get('evidence-wedge', 0)} |")
    lines.append(f"| wedge: expansion-slice | {report['counts'].get('expansion-slice', 0)} |")
    lines.append("")

    lines.append("## Verification limits")
    lines.append("")
    for limit in fresh["verification_limits"]:
        lines.append(f"- {limit}")
    lines.append("")

    lines.append("## Update instructions")
    lines.append("")
    for i, step in enumerate(fresh["update_instructions"], 1):
        lines.append(f"{i}. {step}")
    lines.append("")

    for fid in fam_order:
        fam = next(f for f in catalog["families"] if f["id"] == fid)
        lines.append(f"## {fam['label']} (`{fid}`)")
        lines.append("")
        if fam["pilot"]:
            lines.append("_Pilot family (first-slice deep coverage)._")
            lines.append("")
        lines.append(fam["definition"])
        lines.append("")
        lines.append("| ID | Capability | State | Connection | Entry point | Consumer | Wedge |")
        lines.append("|---|---|---|---|---|---|---|")
        for it in items_by_family[fid]:
            ep = it["entry_point"]
            lines.append(
                f"| `{it['id']}` | {it['capability']} | {it['implementation_state']} | "
                f"{it['connection_state']} | `{ep['ref']}` | {it['consumer']} | {it['wedge']} |"
            )
        lines.append("")
        lines.append("### Detail")
        lines.append("")
        for it in items_by_family[fid]:
            lines.append(f"#### `{it['id']}` — {it['capability']}")
            lines.append("")
            lines.append(f"- **Customer purpose:** {it['customer_purpose']}")
            lines.append(
                f"- **Origin:** {it['origin']['kind']} — {', '.join(it['origin']['refs'])}"
            )
            lines.append(f"- **Implementation state:** {it['implementation_state']}")
            lines.append(f"- **Consumer:** {it['consumer']}")
            lines.append(
                f"- **Entry point:** {it['entry_point']['ref']} (`{it['entry_point']['kind']}`) — {it['entry_point']['note']}"
            )
            lines.append(f"- **Connection state:** {it['connection_state']}")
            lines.append("- **Evidence:**")
            for row in it["evidence"]:
                d = f" ({row['date']})" if row.get("date") else ""
                lines.append(f"  - `{row['type']}`{d} — {row['ref']} — {row['detail']}")
            lines.append("- **Boundaries:**")
            for bf in (
                "security",
                "identity",
                "approval",
                "privacy",
                "release",
                "tenant_workspace",
            ):
                lines.append(f"  - {bf}: {it['boundaries'][bf]}")
            if it.get("incomplete"):
                inc = it["incomplete"]
                lines.append(
                    "- **Incomplete (owner / decision / next action / non-action):** "
                    f"{inc.get('owner')} / {inc.get('decision') or '—'} / "
                    f"{inc['smallest_next_action']} / {inc['non_action']}"
                )
            lines.append(f"- **Wedge:** {it['wedge']}")
            lines.append("")

    lines.append("## Queue coverage (no duplicate work filed)")
    lines.append("")
    lines.append("| Task ref | Title | Status | Coverage note | Decision |")
    lines.append("|---|---|---|---|---|")
    for q in catalog["queue_coverage"]:
        lines.append(
            f"| {q['task_ref']} | {q['title']} | {q['status']} | {q['coverage_note']} | {q['decision']} |"
        )
    lines.append("")

    lines.append(
        "## Traceable paths (capability -> consumer -> behavior evidence -> owner/closure)"
    )
    lines.append("")
    for p in catalog["traceable_paths"]:
        lines.append(f"### `{p['capability_id']}`")
        lines.append("")
        lines.append(f"- **Consumer:** {p['consumer']}")
        lines.append(f"- **Behavior evidence:** {p['behavior_evidence']}")
        lines.append(f"- **Owner / closure decision:** {p['owner_or_closure']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render",
        action="store_true",
        help="render docs/capability-catalog/catalog.md after validating",
    )
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH, help="path to catalog.json")
    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog)
        report = validate(catalog)
    except (ValidationError, json.JSONDecodeError, OSError) as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1

    print(
        f"catalog OK: {report['items']} items, as_of={report['as_of']}, "
        f"baseline={report['source_baseline_sha']}"
    )
    print("families:")
    for fid in sorted(report["per_family"]):
        print(f"  {fid}: {len(report['per_family'][fid])} items")
    print("classification counts:")
    for state in sorted({k[1] for k in report["counts"] if k[0] == "state"}):
        print(f"  implementation_state {state}: {report['counts'][('state', state)]}")
    for conn in sorted({k[1] for k in report["counts"] if k[0] == "conn"}):
        print(f"  connection_state {conn}: {report['counts'][('conn', conn)]}")
    print(f"  wedge evidence-wedge: {report['counts'].get('evidence-wedge', 0)}")
    print(f"  wedge expansion-slice: {report['counts'].get('expansion-slice', 0)}")

    if args.render:
        try:
            RENDER_PATH.write_text(render(catalog, report), encoding="utf-8")
        except OSError as exc:
            print(f"RENDER FAILED: {exc}", file=sys.stderr)
            return 2
        print(f"rendered {RENDER_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
