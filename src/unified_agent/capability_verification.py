"""Pure capability verification projection (UA #1067).

Derives a verification classification and gap paths from catalog item fields.
Stdlib only — no Starlette, storage, or mutation tools.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

EVIDENCE_MATURITY_ORDER = ("code", "test", "ci", "deployed", "live")

VERIFICATION_CLASSIFICATIONS = (
    "not-applicable",
    "speculative",
    "superseded",
    "blocked",
    "misconfigured",
    "error",
    "degraded",
    "dormant",
    "connected-and-live",
    "tested",
    "implemented-in-source",
    "unknown",
)

_FRESHNESS_WINDOW_DAYS = 14


def evidence_maturity(item: dict) -> str:
    """Strongest evidence type present across item["evidence"], else \"none\"."""
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        return "none"
    best_idx = -1
    for row in evidence:
        if not isinstance(row, dict):
            continue
        etype = row.get("type")
        if not isinstance(etype, str) or not etype:
            continue
        try:
            idx = EVIDENCE_MATURITY_ORDER.index(etype)
        except ValueError:
            continue
        if idx > best_idx:
            best_idx = idx
    if best_idx < 0:
        return "none"
    return EVIDENCE_MATURITY_ORDER[best_idx]


def has_dated_live_evidence(item: dict) -> bool:
    """True when any evidence row has type == \"live\" and a non-empty ISO date."""
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        return False
    for row in evidence:
        if not isinstance(row, dict):
            continue
        if row.get("type") != "live":
            continue
        date_str = row.get("date")
        if isinstance(date_str, str) and date_str.strip():
            return True
    return False


def _live_evidence_date(item: dict) -> str:
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        return ""
    best = ""
    for row in evidence:
        if not isinstance(row, dict) or row.get("type") != "live":
            continue
        date_str = row.get("date")
        if isinstance(date_str, str) and date_str.strip():
            if not best or date_str > best:
                best = date_str.strip()
    return best


def _latest_evidence_date(item: dict) -> str:
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        return ""
    best = ""
    for row in evidence:
        if not isinstance(row, dict):
            continue
        date_str = row.get("date")
        if isinstance(date_str, str) and date_str.strip():
            if not best or date_str > best:
                best = date_str.strip()
    return best


def _incomplete(item: dict) -> dict[str, Any] | None:
    raw = item.get("incomplete")
    return raw if isinstance(raw, dict) else None


def _has_consumer_anchor(item: dict) -> bool:
    """Task/decision anchor that makes a gap durable despite no verified consumer."""
    incomplete = _incomplete(item)
    if incomplete is None:
        return False
    task_ref = incomplete.get("task_ref")
    decision = incomplete.get("decision")
    return (isinstance(task_ref, str) and bool(task_ref.strip())) or (
        isinstance(decision, str) and bool(decision.strip())
    )


def _parse_evidence_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def classify_item(item: dict, now: date | None = None) -> tuple[str, str]:
    """Return (classification, note). Deterministic precedence; first match wins."""
    connection = (
        item.get("connection_state") if isinstance(item.get("connection_state"), str) else ""
    )
    implementation = (
        item.get("implementation_state")
        if isinstance(item.get("implementation_state"), str)
        else ""
    )
    consumer = item.get("consumer") if isinstance(item.get("consumer"), str) else ""

    if connection == "not-applicable":
        return ("not-applicable", "boundary is not-applicable by decision")
    if implementation == "speculative":
        return ("speculative", "implementation marked speculative")
    if implementation == "superseded":
        return ("superseded", "implementation marked superseded")
    if connection == "blocked" or implementation == "blocked":
        return ("blocked", "blocked by connection or implementation state")
    if connection == "misconfigured":
        return ("misconfigured", "connection marked misconfigured")
    if connection == "error":
        return ("error", "connection marked error")
    if connection == "degraded":
        return ("degraded", "connection marked degraded")
    if connection == "dormant" or implementation == "dormant/uncertain":
        return ("dormant", "dormant or uncertain implementation")

    evaluation_date = now or datetime.now(UTC).date()
    live_date = _parse_evidence_date(_live_evidence_date(item))
    consumer_present = bool(consumer.strip()) and consumer.strip() != "no verified consumer"
    live_evidence_current = (
        live_date is not None
        and live_date <= evaluation_date
        and not is_stale(evaluation_date.isoformat(), live_date.isoformat())
    )
    if connection == "live" and consumer_present and live_evidence_current:
        return (
            "connected-and-live",
            f"live connection + consumer + recent live evidence {live_date.isoformat()}",
        )
    if connection == "live":
        return (
            "unknown",
            "live connection lacks a real consumer and valid recent nonfuture live evidence "
            "— treated unknown (never inferred from config/test)",
        )

    # Steps 11–14: evidence maturity when connection did not already resolve.
    # Never claim live unless dated live evidence is present.
    # Consumer honesty: "no verified consumer" without task_ref/decision must not
    # claim connected-and-live (already gated by dated-live rule above); fall
    # through to tested / implemented-in-source / unknown from evidence only.
    maturity = evidence_maturity(item)
    if (
        consumer == "no verified consumer"
        and not _has_consumer_anchor(item)
        and not has_dated_live_evidence(item)
        and maturity == "live"
    ):
        # Undated live maturity with no consumer — treat as unknown, never live.
        return (
            "unknown",
            "live claimed without dated live evidence — treated unknown "
            "(never inferred from config/test)",
        )

    if maturity in ("test", "ci", "deployed"):
        return ("tested", f"behavior evidence: {maturity}")
    if maturity == "live":
        return (
            "unknown",
            "live evidence without a live connection — treated unknown",
        )
    if maturity == "code":
        return ("implemented-in-source", "code evidence only")
    return ("unknown", "no verified evidence")


def is_stale(as_of: str, evidence_date: str, window_days: int = _FRESHNESS_WINDOW_DAYS) -> bool:
    """True when evidence_date is at or beyond the window relative to as_of."""
    if not as_of or not evidence_date:
        return False
    try:
        as_of_d = date.fromisoformat(as_of[:10])
        evid_d = date.fromisoformat(evidence_date[:10])
    except ValueError:
        return False
    return (as_of_d - evid_d).days >= window_days


def item_freshness(item: dict, as_of: str) -> dict[str, Any]:
    """Freshness projection; live evidence date takes precedence for staleness."""
    live_date = _live_evidence_date(item)
    latest = _latest_evidence_date(item)
    signal_date = live_date or latest
    return {
        "latest_evidence_date": latest,
        "stale": is_stale(as_of, signal_date) if signal_date else False,
        "live_evidence_date": live_date,
    }


def resolve_durable_paths(item: dict, queue_coverage: list[dict]) -> list[dict]:
    """Pure lookup of durable gap paths — never creates tasks or work."""
    incomplete = _incomplete(item)
    if incomplete is None:
        return []

    paths: list[dict[str, str]] = []
    seen_refs: set[str] = set()

    def _add(kind: str, ref: str, label: str) -> None:
        dedupe_key = ref if ref else f"named:{label}"
        if dedupe_key in seen_refs:
            return
        seen_refs.add(dedupe_key)
        paths.append({"kind": kind, "ref": ref, "label": label})

    task_ref = incomplete.get("task_ref")
    if isinstance(task_ref, str) and task_ref.strip():
        ref = task_ref.strip()
        _add("task", ref, ref)

    item_id = item.get("id") if isinstance(item.get("id"), str) else ""
    family = item.get("family") if isinstance(item.get("family"), str) else ""
    coverage = queue_coverage if isinstance(queue_coverage, list) else []
    for row in coverage:
        if not isinstance(row, dict):
            continue
        note = row.get("coverage_note")
        if not isinstance(note, str):
            continue
        matched = (item_id and item_id in note) or (family and family in note)
        if not matched:
            continue
        row_ref = row.get("task_ref")
        if not isinstance(row_ref, str) or not row_ref.strip():
            continue
        status = row.get("status") if isinstance(row.get("status"), str) else ""
        label = f"{row_ref.strip()} — {status}" if status else row_ref.strip()
        _add("task", row_ref.strip(), label)

    has_task = any(p["kind"] == "task" for p in paths)
    next_action = incomplete.get("smallest_next_action")
    if not has_task and isinstance(next_action, str) and next_action.strip():
        _add("named-gap", "", next_action.strip()[:80])

    return paths


def recommendation(item: dict) -> dict[str, Any] | None:
    """Review-only suggestion; never implies execution or auto-creation."""
    incomplete = _incomplete(item)
    if incomplete is None:
        return None
    next_action = incomplete.get("smallest_next_action")
    if not isinstance(next_action, str) or not next_action.strip():
        return None
    item_id = item.get("id") if isinstance(item.get("id"), str) else ""
    signal = f"{item_id}:{next_action.strip()[:120]}"
    return {
        "source_signal": signal,
        "actionable": False,
        "review_only": True,
        "note": (
            "review-only suggestion; nothing auto-creates work, auto-approves, or mutates tasks"
        ),
    }


def build_verification(
    item: dict,
    meta: dict,
    queue_coverage: list[dict],
    *,
    now: date | None = None,
) -> dict[str, Any]:
    """Per-item verification projection for the API + UI."""
    evaluation_date = now or datetime.now(UTC).date()
    classification, note = classify_item(item, evaluation_date)
    maturity = evidence_maturity(item)
    freshness = item_freshness(item, evaluation_date.isoformat())

    consumer = item.get("consumer") if isinstance(item.get("consumer"), str) else ""
    consumer_present = bool(consumer.strip()) and consumer.strip() != "no verified consumer"
    evidence = item.get("evidence")
    evidence_present = isinstance(evidence, list) and len(evidence) > 0

    missing: list[str] = []
    if not consumer_present:
        missing.append("consumer")
    if not evidence_present:
        missing.append("evidence")

    incomplete = _incomplete(item)
    next_action = "no next action recorded"
    owner = ""
    if incomplete is not None:
        sna = incomplete.get("smallest_next_action")
        if isinstance(sna, str) and sna.strip():
            next_action = sna.strip()
        owner_val = incomplete.get("owner")
        if isinstance(owner_val, str):
            owner = owner_val

    boundaries = item.get("boundaries") if isinstance(item.get("boundaries"), dict) else {}
    security = boundaries.get("security") if isinstance(boundaries.get("security"), str) else ""

    out: dict[str, Any] = {
        "classification": classification,
        "classification_note": note,
        "evidence_maturity": maturity,
        "freshness": freshness,
        "consumer_present": consumer_present,
        "evidence_present": evidence_present,
        "missing": missing,
        "durable_path": resolve_durable_paths(item, queue_coverage),
        "recommendation": recommendation(item),
        "next_action": next_action,
        "security_boundary": security,
    }
    if incomplete is not None:
        out["owner"] = owner
    return out
