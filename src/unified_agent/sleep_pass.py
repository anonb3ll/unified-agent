"""Nightly sleep pass — memory cognitive maintenance (Roadmap Phase 1.1).

Letta-style "sleep-time compute" for the shared memory.db, built to the
approved design ("00 - Inbox/Memory-Consolidation-Sleep-Pass-Design-Approved.md",
eng-review CLEARED 2026-07-21). Single module, direct functions — no class
hierarchy (eng-review D2).

What it does, once a night: reads memory.db (mode=ro — D6, never the writable
`storage.connect()`), reuses `consolidate.py`'s pairwise similarity and union-find clustering at a
broader threshold (0.60, D7) than the weekly near-duplicate job (0.90), and
asks an LLM to classify each cluster as a duplicate (safe to merge), a genuine
contradiction (needs a human), or merely related. It also digests today's
journal and lists low-salience/zero-access notes as pruning candidates.

Like `consolidate.py`, this is STRICTLY READ-ONLY over the database (D6, D9 /
"validate+hold"): everything it finds is written to a single markdown proposal
in the vault inbox. Nothing is ever applied automatically. A human reviews the
file, deletes the blocks they disagree with, and runs
`unified-agent-consolidate apply-batch <path>` to execute what's left — the
markdown *is* the review UI.

Does not call `init_db()` / migrate / start embedding backfills — the hub (or
operator) must already have an initialized `memory.db`. Workload preflight runs
before evolve or any LLM call; oversized/invalid config defers with exit 2
(`deferred-preflight`). Hitting the per-run cluster review cap finishes with
status `partial` and also exits 2. Skips entirely (exit 0) when the host is on
battery power (D8). Hard-fails (exit 1) if every LLM provider in the fallback
chain is unreachable when there was real work to review (D3) — the nightly
timer's failure state is meant to page on this, not silently ship an empty
proposal. A model failure after some clusters were reviewed keeps that work:
the failed clusters wait for the next run and the pass finishes `partial`
(task #1502). Exit 2 is also an intentional pager signal for sustained incomplete
runs (deferred workload or partial cluster coverage).
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import (
    config,
    consolidate,
    edition,
    evolve,
    llm_client,
    org_partition,
    scheduled_status,
    settings,
)
from .service_env import parse_env_file
from .tools.vault import parse_journal_entries  # noqa: F401 - re-exported for callers

DEFAULT_CONTRADICTION_THRESHOLD = 0.60
DEFAULT_MAX_REVIEW_CLUSTERS = 300
DEFAULT_PRUNE_SALIENCE_MAX = 0.3
DEFAULT_PRUNE_MIN_AGE_DAYS = 90.0

# Operators may lower these ceilings; raising the worst-case workload requires
# a reviewed code change. No zero/unlimited escape hatch.
#
# max_candidates/max_pairs raised 2000/250_000 -> 6000/18_000_000 (task #997,
# 2026-09-14): the active memory population passed 2000 organically (~2,760
# candidates, growing ~35-40/night) and every nightly run had deferred at
# preflight since 2026-09-10 as a result — not a scheduler fault, the ceiling
# was simply set before the corpus reached today's real size. Safe to raise
# now because `cluster_duplicates` no longer decodes and compares embeddings
# in a Python double loop: `_pairwise_cosine_matrix` does the O(n^2) cosine
# step as one BLAS matrix multiply, so 6000 candidates (~18M pairs, a 6000x6000
# float32 matrix, ~144MB) costs a fraction of a second rather than minutes.
# 6000 gives roughly 3 months of runway at the current growth rate; the actual
# long-term fix (bucketed/ANN comparison instead of full pairwise) is task
# #967's HNSW evaluation at 250K/500K notes, not this ceiling.
#
# max_clusters raised 100 -> 300 (task #1068, 2026-09-17): same failure shape as
# #997, one ceiling up. The active corpus carries ~123 clusters at threshold
# 0.60, so a 100-cluster budget deferred the remainder on every run and the pass
# reported `partial` (exit 2) — the documented pager for sustained incomplete
# runs — night after night, with preflight reasons empty (no resource limit
# reached). Two changes together clear it: this ceiling gives the backlog
# ~2.4x runway, and `_rotating_review_window` makes a future backlog above the
# ceiling fair rather than permanently starving its tail. 300 bounds the
# worst-case nightly review at ~3x today's measured work, against a free local
# model, on a 03:00 timer.
_PREFLIGHT_LIMITS = {
    "max_candidates": 6000,
    "max_recent_changes": 500,
    "max_pairs": 18_000_000,
    "max_payload_bytes": 32 * 1024 * 1024,
    "recent_window_hours": 24,
    "max_clusters": DEFAULT_MAX_REVIEW_CLUSTERS,
    "max_journal_bytes": 64 * 1024,
}


def _contradiction_threshold(override: float | None = None) -> float:
    if override is not None:
        return override
    raw = os.environ.get("UA_SLEEP_PASS_THRESHOLD")
    if raw is None:
        return DEFAULT_CONTRADICTION_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        return float("nan")  # rejected by preflight, never used for clustering


def _prune_salience_max() -> float:
    raw = os.environ.get("UA_SLEEP_PASS_PRUNE_SALIENCE_MAX")
    try:
        return float(raw) if raw else DEFAULT_PRUNE_SALIENCE_MAX
    except ValueError:
        return DEFAULT_PRUNE_SALIENCE_MAX


def _prune_min_age_days() -> float:
    raw = os.environ.get("UA_SLEEP_PASS_PRUNE_MIN_AGE_DAYS")
    try:
        return float(raw) if raw else DEFAULT_PRUNE_MIN_AGE_DAYS
    except ValueError:
        return DEFAULT_PRUNE_MIN_AGE_DAYS


def _connect_ro() -> sqlite3.Connection:
    """Read-only connection via the `mode=ro` URI (design doc D6).

    Distinct from `storage.connect()` (used everywhere else in the codebase):
    this connection cannot issue a mutating statement even by accident, and
    avoids any possibility of the nightly pass contending with a live writer.
    Requires an existing initialized database. A missing database fails closed;
    maintenance must not create/migrate it or start embedding backfills.
    """
    conn = org_partition.open_store(f"file:{config.db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _preflight(conn: sqlite3.Connection) -> dict:
    """Aggregate metadata only, in the same read transaction as candidate loading.

    Recent creation/update counts are a bulk-import proxy, not an event count.
    Match consolidate's candidate predicate (including its expired-note behavior):
    notes past ``expires_at`` still count toward admission and pair-load
    estimates — consolidate does not filter ``expires_at``, and sleep-pass must
    not either, or preflight would under-count the real clustering workload.
    """
    limits = {}
    reasons = []
    for key, ceiling in _PREFLIGHT_LIMITS.items():
        raw = os.environ.get(f"UA_SLEEP_PASS_{key.upper()}", str(ceiling))
        try:
            value = int(raw)
            if not 0 < value <= ceiling:
                raise ValueError
        except ValueError:
            reasons.append(f"invalid_config:{key}")
            value = ceiling
        limits[key] = value
    now = datetime.now(UTC)
    cutoff = (now - timedelta(hours=limits["recent_window_hours"])).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS active_candidate_count, "
        "COALESCE(SUM(julianday(m.created_at) >= julianday(?)), 0) AS recent_created_count, "
        "COALESCE(SUM(julianday(m.updated_at) >= julianday(?)), 0) AS recent_updated_count, "
        "COALESCE(SUM(julianday(m.created_at) IS NULL OR julianday(m.updated_at) IS NULL), 0) "
        "AS invalid_timestamp_count, "
        "COUNT(me.embedding) AS embedded_candidate_count, "
        "COALESCE(SUM(LENGTH(CAST(m.content AS BLOB)) "
        "+ COALESCE(LENGTH(CAST(m.title AS BLOB)), 0) "
        "+ COALESCE(LENGTH(CAST(m.tags AS BLOB)), 0) "
        "+ COALESCE(LENGTH(me.embedding), 0)), 0) AS candidate_payload_bytes "
        "FROM memory m LEFT JOIN memory_embeddings me ON me.note_id = m.id "
        "WHERE m.id NOT LIKE 'masterindex%' AND m.id NOT LIKE 'tagindex_%' "
        "AND m.superseded_by IS NULL "
        "AND instr(m.tags, '\"meta:deleted\"') = 0 "
        # Tenant fence (task #1762): admission measures the same candidate set
        # consolidate._load_candidates reads, so another tenant's volume never
        # defers this tenant's pass.
        "AND m.org_id IS ?",
        (cutoff, cutoff, edition.tenant_org_id()),
    ).fetchone()
    snapshot = dict(row)
    embedded = snapshot["embedded_candidate_count"]
    snapshot["pairwise_comparison_count"] = embedded * (embedded - 1) // 2
    for measured, limit, reason in (
        (snapshot["active_candidate_count"], "max_candidates", "candidate_limit"),
        (
            max(snapshot["recent_created_count"], snapshot["recent_updated_count"]),
            "max_recent_changes",
            "recent_change_limit",
        ),
        (snapshot["pairwise_comparison_count"], "max_pairs", "pair_limit"),
        (snapshot["candidate_payload_bytes"], "max_payload_bytes", "payload_limit"),
    ):
        if measured > limits[limit]:
            reasons.append(reason)
    if snapshot["invalid_timestamp_count"]:
        reasons.append("invalid_timestamps")
    journal_bytes = 0
    vault = config.vault_root()
    if vault is not None:
        try:
            journal_bytes = (vault / "Logs" / f"{now:%Y-%m-%d}.md").stat().st_size
        except FileNotFoundError:
            pass
        except OSError:
            journal_bytes = None
            reasons.append("journal_unreadable")
    if journal_bytes is not None and journal_bytes > limits["max_journal_bytes"]:
        reasons.append("journal_limit")
    return {
        "observed_at": now.isoformat(),
        "recent_cutoff": cutoff,
        **snapshot,
        "journal_bytes": journal_bytes,
        "limits": limits,
        "reasons": reasons,
    }


def _review_cluster(cluster_notes: list[dict]) -> dict:
    """Review one cluster using consolidate's bounded adjudication seam."""
    return consolidate._review_cluster(cluster_notes)


# Task #1502: one cluster's failed review must not discard the night's work.
# A single reply that missed the verdict schema, or one transient provider
# error, used to abort the whole pass after dozens of good reviews: exit 1, no
# report, and the timer read as broken. Each cluster now gets one retry; a
# cluster that still fails is left for the next run and the pass finishes
# `partial` (exit 2). Several failed clusters in a row mean the route itself is
# down, so the pass stops asking. If nothing was reviewed at all, the chain is
# unavailable and the hard failure of design D3 (exit 1) stands.
_REVIEW_ATTEMPTS_PER_CLUSTER = 2
_MAX_CONSECUTIVE_FAILED_CLUSTERS = 3


def _review_clusters(review_clusters: list[list[dict]], progress: dict) -> tuple[list[dict], int]:
    """Review each cluster; return the reviews and how many clusters went unreviewed.

    Raises the last ``llm_client.LLMConnectionError`` when no cluster could be
    reviewed. Otherwise the failed and not-attempted clusters are counted, and
    the route of the last failure is kept in ``progress["llm_route"]``.
    """
    reviews: list[dict] = []
    failed = consecutive = 0
    last_error: llm_client.LLMConnectionError | None = None
    for index, cluster in enumerate(review_clusters):
        review = None
        for _attempt in range(_REVIEW_ATTEMPTS_PER_CLUSTER):
            try:
                review = _review_cluster(cluster)
                break
            except llm_client.LLMConnectionError as exc:
                last_error = exc
        if review is None:
            failed += 1
            consecutive += 1
            if consecutive >= _MAX_CONSECUTIVE_FAILED_CLUSTERS:
                failed += len(review_clusters) - index - 1
                break
            continue
        consecutive = 0
        survivor = consolidate._pick_survivor(cluster) if review["verdict"] == "DUPLICATE" else None
        reviews.append({"cluster": cluster, "survivor": survivor, **review})
        progress["cluster_count"] = len(reviews)
    if failed:
        assert last_error is not None
        if not reviews:
            raise last_error
        progress["llm_route"] = _bounded_llm_route(last_error.route)
    return reviews, failed


def _find_pruning_candidates(
    conn: sqlite3.Connection, salience_max: float, min_age_days: float
) -> list[dict]:
    """Low-salience, never-accessed, stale notes — proposed for human archival review.

    Deliberately advisory-only (no apply-batch action exists for a single-note
    prune, unlike the survivor/superseded shape a duplicate cluster produces) —
    the design's PruningSuggester is explicit that this replaces a physical
    salience-decay write with a proposal, keeping the pass fully read-only.
    """
    rows = conn.execute(
        "SELECT id, title, salience, access_count, updated_at FROM memory "
        "WHERE superseded_by IS NULL AND id NOT LIKE 'masterindex%' "
        "AND id NOT LIKE 'tagindex_%' AND salience <= ? AND access_count = 0 "
        "AND instr(tags, '\"meta:deleted\"') = 0 AND org_id IS ?",
        # Tenant fence (task #1762): prune proposals are the caller's tenant only.
        (salience_max, edition.tenant_org_id()),
    ).fetchall()
    now_dt = datetime.now(UTC)
    candidates = []
    for r in rows:
        if not r["updated_at"]:
            continue
        try:
            updated_dt = datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        age_days = max(0.0, (now_dt - updated_dt).total_seconds() / 86400.0)
        if age_days >= min_age_days:
            candidates.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "salience": r["salience"],
                    "age_days": round(age_days, 1),
                }
            )
    candidates.sort(key=lambda c: c["age_days"], reverse=True)
    return candidates


def _generate_journal_digest(date: str | None = None, max_bytes: int = 64 * 1024) -> str | None:
    """Bounded summary of the daily journal and PR stream; None if no entries.

    Raises `llm_client.LLMConnectionError` (uncaught, by design) if entries
    exist but every provider fails — same hard-fail contract as cluster review.
    """
    vault_root = config.vault_root()
    if vault_root is None or not vault_root.is_dir():
        return None
    day = date or datetime.now(UTC).strftime("%Y-%m-%d")
    from .safety import resolve_in_vault
    from .tools.vault import _preset_readable, journal_stream_path

    entries = []
    remaining = max_bytes
    for rel in (f"Logs/{day}.md", journal_stream_path("pr-watch", day)):
        journal_path = resolve_in_vault(rel)
        if not _preset_readable(journal_path) or not journal_path.is_file():
            continue
        with journal_path.open("rb") as stream:
            data = stream.read(remaining + 1)
        if len(data) > remaining:
            raise ToolError("journal grew beyond the preflight limit; digest not evaluated")
        remaining -= len(data)
        entries.extend(parse_journal_entries(data.decode("utf-8")))
    if not entries:
        return None
    bullet_list = "\n".join(f"- [{e['time']}] {e['agent']}: {e['text']}" for e in entries)
    prompt = (
        "Summarize today's multi-agent journal in 3-5 sentences. Highlight decisions "
        f"made and any anomalies or incidents; skip routine bookkeeping:\n\n{bullet_list}"
    )
    # subscription_pool opt-in (task #1252): the nightly digest is a named
    # zero-spend background call site — still gated on UA_SUBSCRIPTION_POOL_ENABLED.
    response = llm_client.complete(prompt, subscription_pool=True, memory_agent=True)
    return response["text"]


_MAX_RENDERED_CLUSTER_IDS = 12


def _compact_cluster_ids(cluster: list[dict]) -> str:
    ids = [f"`{note['id']}`" for note in cluster]
    if len(ids) > _MAX_RENDERED_CLUSTER_IDS:
        omitted = len(ids) - _MAX_RENDERED_CLUSTER_IDS
        return ", ".join(ids[:_MAX_RENDERED_CLUSTER_IDS]) + f", … (+{omitted} more)"
    return ", ".join(ids)


def _inline_code(text: str) -> str:
    """Compatibility alias for the shared literal report-field renderer."""
    return consolidate.inline_code(text)


def render_sleep_pass_markdown(
    cluster_reviews: list[dict],
    digest: str | None,
    prune_candidates: list[dict],
    threshold: float,
    detected_cluster_count: int | None = None,
    deferred_cluster_count: int = 0,
    preflight: dict | None = None,
    llm_failed_cluster_count: int = 0,
    digest_failed: bool = False,
) -> str:
    """Human-readable, human-approved-then-applied nightly report.

    Duplicate-cluster blocks carry the exact `unified-agent-consolidate apply`
    command `apply_batch()` looks for (same shape as `consolidate.py`'s own
    weekly proposal) — deleting a block is how a reviewer rejects it.
    """
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    lines = [
        "---",
        "title: Nightly Sleep Pass",
        f"generated: {ts}",
        "tags: [memory, sleep-pass, proposal]",
        "---",
        "",
        "# Nightly Sleep Pass",
        "",
        f"*Generated: `{ts}` · cluster threshold: `{threshold}`.*",
        "",
        "This is a **proposal only** — nothing has been changed. Apply blocks below "
        "are explicit, reversible `unified-agent-consolidate apply` commands; delete "
        "a block you disagree with, then run "
        "`unified-agent-consolidate apply-batch <this-file>` to execute the rest at once.",
        "",
    ]

    if preflight is not None:
        lines += ["## Preflight", "", "```json", json.dumps(preflight, indent=2), "```", ""]
        if preflight["reasons"]:
            lines += [
                "**Status: deferred-preflight. This run is incomplete.**",
                "",
                "Workload/capacity gate: " + ", ".join(preflight["reasons"]) + ".",
                "Clusters, journal digest, pruning, and evolve were not evaluated. "
                "No clean-state conclusion can be drawn. Review the limits and recent imports "
                "before retrying; an unchanged oversized population will defer again.",
                "",
            ]
            return "\n".join(lines)

    duplicates = [r for r in cluster_reviews if r["verdict"] == "DUPLICATE"]
    contradictions = [r for r in cluster_reviews if r["verdict"] == "CONTRADICTION"]
    unrelated = [r for r in cluster_reviews if r["verdict"] == "UNRELATED"]
    needs_review = [r for r in cluster_reviews if r["verdict"] == consolidate.VERDICT_NEEDS_REVIEW]
    unparsed = [r for r in cluster_reviews if r["verdict"] == consolidate.VERDICT_UNPARSED]
    detected_count = (
        len(cluster_reviews) if detected_cluster_count is None else detected_cluster_count
    )

    lines += ["## Cluster Review", ""]
    if not cluster_reviews:
        lines += ["No topic clusters found at this threshold.", ""]
    else:
        disputed = sum(1 for r in contradictions if consolidate.disputed_label_marker(r))
        disputed_note = f" (**{disputed}** disputed by the judge's reason)" if disputed else ""
        flagged = f", **{len(needs_review)}** needs review" if needs_review else ""
        if unparsed:
            flagged += f", **{len(unparsed)}** unparsed"
        lines += [
            f"Reviewed **{len(cluster_reviews)}** of **{detected_count}** cluster(s): "
            f"**{len(duplicates)}** duplicate, **{len(contradictions)}** contradiction"
            f"{disputed_note}, **{len(unrelated)}** unrelated{flagged}.",
            "",
        ]
        if deferred_cluster_count:
            lines += [
                f"Deferred **{deferred_cluster_count}** cluster(s) to keep this nightly pass bounded. "
                "They remain queued for later runs.",
                "",
            ]
        if llm_failed_cluster_count:
            lines += [
                f"**{llm_failed_cluster_count}** cluster(s) were not reviewed: the model did "
                "not return a usable verdict, or the pass stopped asking after repeated "
                "failures. This run is incomplete; they are reviewed again when the rotating "
                "review window next reaches them.",
                "",
            ]
        if duplicates:
            lines += ["### Duplicates (safe to merge)", ""]
            for r in duplicates:
                survivor = r["survivor"]
                losers = [c["id"] for c in r["cluster"] if c["id"] != survivor["id"]]
                lines += [
                    f"- Survivor `{survivor['id']}` — {_inline_code(r['reason'])}",
                    "",
                    "```bash",
                    f"unified-agent-consolidate apply {survivor['id']} {' '.join(losers)}",
                    "```",
                    "",
                ]
        if contradictions:
            lines += ["### Contradictions (needs human resolution — no auto-apply)", ""]
            for r in contradictions:
                lines.append(
                    f"- {_compact_cluster_ids(r['cluster'])} — {_inline_code(r['reason'])}"
                )
                marker = consolidate.disputed_label_marker(r)
                if marker:
                    lines.append(f"  - {_inline_code(marker)}")
            lines.append("")
        if needs_review:
            # Task #2022: the judge said CONTRADICTION but its structured
            # answer reported the pair self-consistent with no conflicting
            # attributes. The basis is printed so a human sees why it moved.
            lines += [
                "### Needs review (judge's structured answer disputes its label — no auto-apply)",
                "",
            ]
            for r in needs_review:
                if r.get("downgrade_reason"):
                    basis = _inline_code(r["downgrade_reason"])
                else:
                    basis = f'cue: "{_inline_code(r.get("consistency_flag"))}"'
                lines.append(
                    f"- {_compact_cluster_ids(r['cluster'])} — {_inline_code(r['reason'])} "
                    f"(judge said {_inline_code(r.get('judge_verdict', 'CONTRADICTION'))}; "
                    f"{basis})"
                )
            lines.append("")
        if unparsed:
            # Task #2006: the model's reply could not be parsed. That blocks a
            # merge, but it says the adjudicator failed, not that the notes are
            # unrelated, so it must not vanish from the report.
            lines += [
                "### Unparsed (model reply could not be parsed — no verdict, no auto-apply)",
                "",
                "The adjudicator returned no usable verdict for these clusters. This is a "
                "signal the model or its context budget is broken, not a finding about the "
                "notes.",
                "",
            ]
            for r in unparsed:
                # The raw reply is untrusted model text: one line, so it cannot
                # read as an apply command to apply_batch(), and an inline code
                # span, so an image, link or HTML in it stays inert text.
                reply = consolidate.one_line(r.get("reason"))
                provider = consolidate.one_line(r.get("provider")) or "unknown"
                lines.append(
                    f"- {_compact_cluster_ids(r['cluster'])} — reply from "
                    f"{_inline_code(provider)}: "
                    f"{_inline_code(reply) if reply else '(empty reply)'}"
                )
            lines.append("")
        if unrelated:
            lines += ["### Unrelated (no action)", ""]
            for r in unrelated:
                lines.append(
                    f"- {_compact_cluster_ids(r['cluster'])} — {_inline_code(r['reason'])}"
                )
            lines.append("")

    lines += ["## Daily Digest", ""]
    if digest:
        literal_digest = consolidate.neutralize_apply_lines(digest)
        longest = max((len(run) for run in re.findall(r"`+", literal_digest)), default=0)
        fence = "`" * max(3, longest + 1)
        lines += [f"{fence}text", literal_digest, fence, ""]
    elif digest_failed:
        lines += [
            "The model did not return a digest of today's journal; this run is incomplete.",
            "",
        ]
    else:
        lines += ["No journal entries found for today.", ""]

    lines += ["## Pruning Suggestions (low-salience, zero-access — review only)", ""]
    if prune_candidates:
        for c in prune_candidates:
            lines.append(
                f"- `{c['id']}` — {consolidate.inline_code(c['title'], fallback='(untitled)')} · salience `{c['salience']}` · "
                f"untouched `{c['age_days']}` days"
            )
        lines.append("")
    else:
        lines += ["No pruning candidates at current thresholds.", ""]

    return "\n".join(lines)


def _write_artifact(markdown: str) -> str | None:
    """Persist the report, preferring the vault inbox, falling back to a report file.

    Same chokepoint and fallback shape as `consolidate.py`'s `_write_artifact`
    (calls `resolve_in_vault` directly rather than the `vault_write` MCP tool,
    since this is a standalone script, not a hub-served tool call). The vault
    write holds the per-note lock (#2086). Same locked-note rule too (#2091): a
    ``locked: true`` note at the target name is never overwritten; the report
    goes to the next free sibling (``...-HHMMSS-2.md``), and the returned path,
    reported as ``artifact_path``, names it.
    """
    name = f"Sleep-Pass-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.md"
    try:
        from .note_lock import unlocked_note_slot
        from .safety import resolve_in_vault

        target = resolve_in_vault(f"00 - Inbox/{name}", for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with unlocked_note_slot(target) as slot:
            slot.write_text(markdown, encoding="utf-8")
        return str(slot)
    except (ToolError, OSError):
        pass
    try:
        reports = config.data_home() / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        target = reports / name
        target.write_text(markdown, encoding="utf-8")
        return str(target)
    except OSError:
        return None


def _rotating_review_window(clusters: list, max_clusters: int, *, day: str) -> list:
    """Tonight's slice of `clusters` — a window that advances one budget per day.

    `clusters` is sorted deterministically (by note id), so truncating it with
    `clusters[:max_clusters]` reviews the same head every night and starves the
    tail permanently: with 123 clusters detected and a 100-cluster budget the
    same 23 were deferred on 2026-09-15, -16 and -17 while no resource limit was
    reached, and every run reported `partial` (exit 2) — the pager signal for
    sustained incomplete runs, firing nightly without the condition ever
    clearing. The cap exists to bound the nightly LLM spend, not to exclude a
    subset, so the window moves by one full budget per day and every cluster is
    reviewed within `ceil(N / max_clusters)` nights.

    Deterministic within a day (a re-run reviews the same window), stateless
    (no cursor to persist), and the same cost as before — the same number of
    clusters is reviewed per night. An unparseable `day` falls back to the
    previous head window rather than failing the run.
    """
    total = len(clusters)
    if max_clusters <= 0 or total <= max_clusters:
        return list(clusters)
    try:
        day_index = date.fromisoformat(day).toordinal()
    except (TypeError, ValueError):
        day_index = 0
    offset = (day_index * max_clusters) % total
    window = list(clusters[offset : offset + max_clusters])
    if len(window) < max_clusters:
        window += clusters[: max_clusters - len(window)]
    return window


def _run_sleep_pass(threshold: float | None, write: bool, progress: dict) -> dict:
    """Execute maintenance, retaining measured progress if a later phase fails."""
    if llm_client.is_on_battery():
        return {"status": "skipped-battery"}

    thr = _contradiction_threshold(threshold)
    progress["phase"] = "preflight"
    # Vault notes are filed only after the read transaction closes: a note
    # write can wait on another writer's note lock (#2086), and the snapshot
    # must not stay open across that wait.
    # A proposal computed before a later read fails is still filed, once the
    # snapshot is closed, and the read's error is what propagates.
    deferred: dict | None = None
    evolve_result: dict | None = None
    conn = _connect_ro()
    try:
        conn.execute("BEGIN")
        preflight = _preflight(conn)
        progress["preflight"] = preflight
        if not 0 <= thr <= 1:
            preflight["reasons"].append("invalid_config:threshold")
        if preflight["reasons"]:
            deferred = {
                "status": "deferred-preflight",
                "preflight": preflight,
                "deferred_candidate_count": preflight["active_candidate_count"],
                "cluster_count": 0,
                "detected_cluster_count": None,
                "deferred_cluster_count": None,
                "duplicate_count": 0,
                "contradiction_count": 0,
                "prune_candidate_count": None,
                "digest": None,
                "artifact_path": None,
                "markdown": render_sleep_pass_markdown([], None, [], thr, preflight=preflight),
                "evolve": {"skipped": "deferred-preflight", "proposal_count": 0},
            }
        else:
            progress["phase"] = "evolve"
            evolve_result = evolve.run_evolve_pass(write=False, conn=conn)
            progress["evolve"] = evolve_result
            candidates = consolidate._load_candidates(conn)
            progress["phase"] = "pruning"
            prune_candidates = _find_pruning_candidates(
                conn, _prune_salience_max(), _prune_min_age_days()
            )
    except BaseException as exc:
        conn.close()
        if write and evolve_result is not None and isinstance(exc, Exception):
            # Keep the failing phase in progress; a filing error must not
            # replace the read error, so it is attached as a note instead.
            try:
                progress["evolve"] = evolve.file_proposal(evolve_result)
            except Exception as file_exc:
                exc.add_note(f"evolve proposal not filed: {type(file_exc).__name__}")
        raise
    conn.close()

    if deferred is not None:
        if write:
            deferred["artifact_path"] = _write_artifact(deferred["markdown"])
        return deferred

    assert evolve_result is not None
    if write:
        progress["phase"] = "evolve"
        evolve_result = evolve.file_proposal(evolve_result)
        progress["evolve"] = evolve_result

    # All source data is now bounded and from the admitted snapshot. Release the
    # read transaction before model waits so live writers can checkpoint the WAL.
    progress["phase"] = "clustering"
    clusters = consolidate.cluster_duplicates(candidates, thr)
    clusters.sort(key=lambda cluster: tuple(sorted(note["id"] for note in cluster)))
    detected_cluster_count = len(clusters)
    review_clusters = _rotating_review_window(
        clusters,
        preflight["limits"]["max_clusters"],
        day=preflight["observed_at"][:10],
    )
    progress.update(phase="review", detected_cluster_count=detected_cluster_count, cluster_count=0)
    cluster_reviews, llm_failed_cluster_count = _review_clusters(review_clusters, progress)
    deferred_cluster_count = detected_cluster_count - len(review_clusters)

    progress["phase"] = "digest"
    digest_failed = False
    try:
        digest = _generate_journal_digest(
            date=preflight["observed_at"][:10], max_bytes=preflight["limits"]["max_journal_bytes"]
        )
    except llm_client.LLMConnectionError as exc:
        # Task #1502: a model answered the cluster reviews, so a failed digest
        # must not discard them or read as "no model answered". Keep the
        # report and finish partial; with nothing reviewed the hard fail stands.
        if not cluster_reviews:
            raise
        digest, digest_failed = None, True
        progress.setdefault("llm_route", _bounded_llm_route(exc.route))

    progress["phase"] = "report"
    markdown = render_sleep_pass_markdown(
        cluster_reviews,
        digest,
        prune_candidates,
        thr,
        detected_cluster_count=detected_cluster_count,
        deferred_cluster_count=deferred_cluster_count,
        preflight=preflight,
        llm_failed_cluster_count=llm_failed_cluster_count,
        digest_failed=digest_failed,
    )
    artifact_path = _write_artifact(markdown) if write else None

    return {
        "status": (
            "partial"
            if deferred_cluster_count or llm_failed_cluster_count or digest_failed
            else "ok"
        ),
        # Task #1502: a pass that lost clusters or its digest to the model
        # still names why.
        "error_code": (
            "llm_review_incomplete"
            if llm_failed_cluster_count
            else "llm_digest_failed"
            if digest_failed
            else None
        ),
        "llm_route": progress.get("llm_route"),
        "preflight": preflight,
        "cluster_count": len(cluster_reviews),
        "detected_cluster_count": detected_cluster_count,
        "deferred_cluster_count": deferred_cluster_count,
        "llm_failed_cluster_count": llm_failed_cluster_count,
        "duplicate_count": sum(1 for r in cluster_reviews if r["verdict"] == "DUPLICATE"),
        "contradiction_count": sum(1 for r in cluster_reviews if r["verdict"] == "CONTRADICTION"),
        "unrelated_count": sum(1 for r in cluster_reviews if r["verdict"] == "UNRELATED"),
        "contradictions_disputed": sum(
            1 for r in cluster_reviews if consolidate.disputed_label_marker(r)
        ),
        "needs_review_count": sum(
            1 for r in cluster_reviews if r["verdict"] == consolidate.VERDICT_NEEDS_REVIEW
        ),
        "unparsed_count": sum(
            1 for r in cluster_reviews if r["verdict"] == consolidate.VERDICT_UNPARSED
        ),
        "prune_candidate_count": len(prune_candidates),
        # Task #2088: every cluster not judged DUPLICATE, as note ids and the
        # verdict class only, for the console summary. Not telemetry.
        "refused": consolidate.refused_clusters(
            [r for r in cluster_reviews if r["verdict"] != "DUPLICATE"],
            ids_of=lambda r: [note["id"] for note in r["cluster"]],
        ),
        "digest": digest,
        "artifact_path": artifact_path,
        "markdown": markdown,
        "evolve": evolve_result,
    }


_LLM_ROUTE_KEYS = ("target", "ok", "elapsed_s", "error", "status", "reason")
_LLM_ROUTE_MAX_LEGS = 8
_LLM_ROUTE_ERROR_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _bounded_llm_route(route: object) -> list[dict] | None:
    """Project a chain route into an allowlisted, content-free diagnostic.

    ``llm_client.LLMConnectionError`` carries the ordered per-leg
    ``{target, ok, elapsed_s}`` records precisely so the all-providers-
    unavailable case still says what was tried and how long each leg took
    (task #445). This record is allowlisted, so the route is rebuilt key by
    key instead of passed through: a leg name, a boolean, and a duration are
    provider/health facts, never prompt or note text.

    A failed leg also keeps ``error``, ``status`` and ``reason`` (task #1639):
    four bare ``ok: false`` legs left a nightly failure undiagnosable for
    days. Each is a fixed fact, never provider text, which can echo the
    private notes in the prompt: ``error`` only if it is an identifier (an
    exception class or category), ``status`` only if it is an HTTP status
    integer, and ``reason`` only if it is one of ``llm_client.LEG_REASONS``.
    Anything else is dropped here, so this record never depends on the caller.
    Returns None when there is no route, so a success record stays visibly
    different from a failure that recorded no attempts.
    """
    if not isinstance(route, list) or not route:
        return None
    projected: list[dict] = []
    for attempt in route[:_LLM_ROUTE_MAX_LEGS]:
        if not isinstance(attempt, dict):
            continue
        target = attempt.get("target")
        if not isinstance(target, str) or not target:
            continue
        elapsed = attempt.get("elapsed_s")
        record: dict = {
            "target": target,
            "ok": bool(attempt.get("ok")),
            "elapsed_s": (round(float(elapsed), 3) if isinstance(elapsed, (int, float)) else None),
        }
        error = attempt.get("error")
        if isinstance(error, str) and _LLM_ROUTE_ERROR_RX.fullmatch(error):
            record["error"] = error
        status = attempt.get("status")
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            record["status"] = status
        reason = attempt.get("reason")
        if isinstance(reason, str) and reason in llm_client.LEG_REASONS:
            record["reason"] = reason
        model = attempt.get("model")
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", model):
            record["model"] = model
        projected.append(record)
    return projected or None


# Local-model settings the scheduled pass takes from the hub's service.env
# (task #1639). The hub and doctor read service.env; the timer sources only
# sleep-pass.env. On the Mac that left the hub on UA_OLLAMA_MODEL while the
# nightly pass silently requested llama3:8b, a model nobody chose for an 8 GB
# host, and doctor's probe described a model the pass never used. Only these
# non-secret keys are inherited, and only when the pass's own env leaves them
# unset, so sleep-pass.env still wins. The claude_cli leg's binary, model and
# timeout and the pinned UA_OPENROUTER_MODEL (task #2025) are inherited the
# same way: the scheduled job's PATH is minimal, so a UA_CLAUDE_CLI_BIN the
# hub was given must reach the pass too, and both chains use one model. The
# OpenRouter key itself is a secret and is never inherited. Memory routing
# uses the same non-secret mechanism; provider opt-ins and secrets are not inherited.
_HUB_LOCAL_MODEL_KEYS = (
    llm_client.OPENROUTER_MODEL_ENV,
    llm_client.CLAUDE_CLI_BIN_ENV,
    llm_client.CLAUDE_CLI_MODEL_ENV,
    llm_client.CLAUDE_CLI_TIMEOUT_ENV,
    llm_client.OLLAMA_BASE_URL_ENV,
    llm_client.OLLAMA_MODEL_ENV,
    llm_client.OLLAMA_TIMEOUT_ENV,
    llm_client.MEMORY_PROVIDER_ENV,
    llm_client.MEMORY_MODEL_ENV,
    # Task #2109: a devices-only hub's scheduled pass must not run claude -p.
    "UA_EXECUTION_LOCALITY",
)


SLEEP_PASS_ENV_FILE = "sleep-pass.env"


def _resolved_hub_model_env(primary: dict[str, str], hub: dict[str, str]) -> dict[str, str]:
    """Inherit non-secret settings without pairing a new provider with an old model.

    Selecting a provider at the higher layer resets its memory model to the
    provider-specific default unless that same layer supplies a memory model.
    """
    resolved = {}
    provider_selected = bool(primary.get(llm_client.MEMORY_PROVIDER_ENV, "").strip())
    for key in _HUB_LOCAL_MODEL_KEYS:
        value = primary.get(key, "").strip()
        if not value and not (provider_selected and key == llm_client.MEMORY_MODEL_ENV):
            value = hub.get(key, "").strip()
        if value:
            resolved[key] = value
    return resolved


def scheduled_ollama_env() -> dict[str, str]:
    """The local-leg settings the *scheduled* pass runs with, for doctor (task #1639).

    Mirrors the runtime order: ``<UA_HOME>/sleep-pass.env`` (what
    deploy/run-sleep-pass-scheduled.sh sources) wins, then the
    ``settings.toml`` values the pass seeds at import (task #2175), and a key
    both leave unset comes from ``service.env`` (what
    ``_inherit_hub_local_model_env`` copies), read from the ``UA_HOME`` that
    ``sleep-pass.env`` sets, if it sets one (task #2196). Only the non-secret
    keys in ``_HUB_LOCAL_MODEL_KEYS`` (Ollama, claude_cli, and memory-routing
    settings) are returned; nothing else is read out. A key no layer sets is
    absent, so llm_client's default applies, as it does at run time.
    """
    own, home = _scheduled_own_env()
    return _resolved_hub_model_env(own, parse_env_file(home / "service.env"))


def _scheduled_own_env() -> tuple[dict[str, str], Path]:
    """The scheduled pass's environment before it inherits, and its UA_HOME.

    The wrapper finds ``sleep-pass.env`` under ``config.data_home()`` and
    sources it into the process. Importing ``config`` then seeds every key it
    left unset from ``settings.toml`` (``settings.apply_to_environ``), and
    ``_inherit_hub_local_model_env`` reads ``service.env`` from
    ``config.data_home()``, both in that sourced environment. So a ``UA_HOME``
    set in ``sleep-pass.env`` relocates both files (task #2196), and the
    returned home is where ``service.env`` is read. The settings loader is
    reused, so its secret and posture filters apply unchanged (task #2175).

    The settings file is ``UA_SETTINGS_FILE`` from ``sleep-pass.env`` (empty
    disables it), else ``<UA_HOME>/settings.toml``. This process's own
    ``UA_SETTINGS_FILE`` is not consulted (task #2193): neither the systemd
    unit nor the LaunchAgent sets one, so the nightly job never has it. The
    doctor cannot see the scheduler's environment, so its ``config.data_home()``
    stands in for the wrapper's when locating ``sleep-pass.env``.
    """
    env = parse_env_file(config.data_home() / SLEEP_PASS_ENV_FILE)
    location = {"UA_HOME": str(config.data_home()), **env}
    home = Path(location["UA_HOME"]).expanduser()
    target = settings.settings_path(location)
    if target is not None:
        settings.apply_to_environ(env, path=target, quiet=True)
    return env, home


def scheduled_default_path(home: str) -> str:
    """The PATH the nightly job starts with when sleep-pass.env sets none.

    macOS launchd gives a LaunchAgent /usr/bin:/bin:/usr/sbin:/sbin; the
    systemd unit (deploy/systemd/ua-sleep-pass.service) sets its own.
    """
    if sys.platform == "darwin":
        return "/usr/bin:/bin:/usr/sbin:/sbin"
    return f"{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def scheduled_env() -> dict[str, str]:
    """The environment the *scheduled* pass's LLM chain sees, for doctor (task #1639).

    The scheduled run sources ``sleep-pass.env`` for its provider settings,
    and seeds the keys it leaves unset from ``settings.toml`` at import, which
    carries non-secret ``UA_*`` values only (task #2175: a memory pin in its
    ``[env]`` table is what the pass runs with). Only the
    ``_HUB_LOCAL_MODEL_KEYS`` may come from ``service.env``. So every key of
    those two layers is taken as is, the local-leg and memory keys are
    resolved as ``scheduled_ollama_env`` does, and PATH and HOME are the
    scheduler's (``scheduled_default_path``) unless the file sets them. Values
    may be secrets: the result is for an in-process ``leg_status`` probe and
    must never be printed or logged.
    """
    own, ua_home = _scheduled_own_env()
    env = {key: value for key, value in own.items() if key not in _HUB_LOCAL_MODEL_KEYS}
    env.update(_resolved_hub_model_env(own, parse_env_file(ua_home / "service.env")))
    # The scheduler's PATH, not the hub's (task #2025): the claude_cli leg is
    # found on PATH, and the hub's launcher adds Homebrew and ~/.local/bin
    # while the nightly job's does not. sleep-pass.env may set its own PATH.
    home = os.environ.get("HOME", "")
    env.setdefault("HOME", home)
    env.setdefault("PATH", scheduled_default_path(home))
    return env


def _inherit_hub_local_model_env() -> list[str]:
    """Inherit unset non-secret model/routing settings; return the keys set."""
    hub_env = parse_env_file(config.data_home() / "service.env")
    inherited = []
    resolved = _resolved_hub_model_env(dict(os.environ), hub_env)
    for key, value in resolved.items():
        if not os.environ.get(key, "").strip():
            os.environ[key] = value
            inherited.append(key)
    return inherited


def _run_telemetry(result: dict, write: bool, started_at: str, started: float) -> dict:
    """Allowlisted automation record: no note IDs, bodies, paths, or provider errors."""
    return {
        "schema_version": 1,
        "event": "sleep_pass.completed",
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "status": result["status"],
        "phase": result.get("phase", "completed"),
        "error_code": result.get("error_code"),
        "llm_route": result.get("llm_route"),
        "preflight": result.get("preflight"),
        "cluster_count": result.get("cluster_count"),
        "detected_cluster_count": result.get("detected_cluster_count"),
        "deferred_cluster_count": result.get("deferred_cluster_count"),
        "deferred_candidate_count": result.get("deferred_candidate_count", 0),
        "llm_failed_cluster_count": result.get("llm_failed_cluster_count"),
        "duplicate_count": result.get("duplicate_count"),
        "contradiction_count": result.get("contradiction_count"),
        "contradictions_disputed": result.get("contradictions_disputed"),
        "needs_review_count": result.get("needs_review_count"),
        "unparsed_count": result.get("unparsed_count"),
        "prune_candidate_count": result.get("prune_candidate_count"),
        "digest_generated": result.get("digest") is not None,
        "evolve_proposal_count": (result.get("evolve") or {}).get("proposal_count"),
        "report_requested": write,
        "report_written": result.get("artifact_path") is not None,
    }


def run_sleep_pass(threshold: float | None = None, write: bool = True) -> dict:
    """Run read-only maintenance; return proposal plus content-free telemetry.

    Battery skips and preflight deferrals do no downstream work. Admitted runs
    preserve evolve-before-LLM ordering. Provider failures still propagate.
    """
    started_at, started = datetime.now(UTC).isoformat(), time.monotonic()
    progress: dict[str, object] = {"status": "error", "phase": "battery_check"}
    try:
        result = _run_sleep_pass(threshold, write, progress)
    except Exception as exc:
        is_llm_unavailable = isinstance(exc, llm_client.LLMConnectionError)
        progress["error_code"] = "llm_unavailable" if is_llm_unavailable else "maintenance_failed"
        if is_llm_unavailable:
            # Name the legs that were attempted, and how long each took. The
            # route is the whole point of LLMConnectionError carrying it (task
            # #445): without this projection the one failure mode that most
            # needs the diagnostic — every provider returning None — records
            # only "llm_unavailable" and cannot be attributed to a leg.
            progress["llm_route"] = _bounded_llm_route(exc.route)
        # Preserve the original exception contract; expose only allowlisted progress.
        exc.sleep_pass_telemetry = _run_telemetry(progress, write, started_at, started)
        raise
    if write and result["status"] != "skipped-battery" and not result["artifact_path"]:
        result["status"] = "error"
        result["error_code"] = "report_write_failed"
    result["telemetry"] = _run_telemetry(result, write, started_at, started)
    return result


def main() -> None:
    scheduled = "--scheduled" in sys.argv[1:]
    run_id = None
    if scheduled:
        candidate = os.environ.get("UA_SLEEP_PASS_RUN_ID")
        current = scheduled_status.current_run_status("sleep-pass")
        if current.get("run_id") == candidate and current.get("state") == "started":
            run_id = candidate
    parser = argparse.ArgumentParser(
        description=(
            "Nightly sleep pass: LLM contradiction/duplicate review of topic clusters, "
            "a journal digest, and pruning suggestions. Read-only, proposal-only."
        )
    )
    parser.add_argument(
        "-t",
        "--threshold",
        type=float,
        default=None,
        help=f"Cluster threshold (default: UA_SLEEP_PASS_THRESHOLD or {DEFAULT_CONTRADICTION_THRESHOLD}).",
    )
    parser.add_argument(
        "-n",
        "--no-write",
        action="store_true",
        help="Do not write the artifact; just print the summary.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print only content-free run telemetry."
    )
    parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    _inherit_hub_local_model_env()

    try:
        res = run_sleep_pass(threshold=args.threshold, write=not args.no_write)
    except (
        llm_client.LLMConnectionError,
        sqlite3.Error,
        ToolError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        # Provider/tool messages may contain private input or credentials.
        res = {
            "status": "error",
            "error_code": (
                "llm_unavailable"
                if isinstance(exc, llm_client.LLMConnectionError)
                else "maintenance_failed"
            ),
        }
        res["telemetry"] = exc.sleep_pass_telemetry

    if not args.json:
        if res["status"] == "skipped-battery":
            print("Skipping sleep pass: host is on battery power.")
        elif res["status"] == "deferred-preflight":
            print(
                "Deferred sleep pass: " + ", ".join(res["preflight"]["reasons"]) + ". "
                "Clusters, digest, pruning, and evolve were not evaluated."
            )
        elif res["status"] == "error":
            print(f"Sleep pass failed: {res['error_code']}.", file=sys.stderr)
        else:
            print(
                f"Reviewed {res['cluster_count']} of {res['detected_cluster_count']} cluster(s): "
                f"{res['duplicate_count']} duplicate, {res['contradiction_count']} contradiction, "
                f"{res.get('unrelated_count', 0)} unrelated, "
                f"{res.get('needs_review_count', 0)} needs review, "
                f"{res.get('unparsed_count', 0)} unparsed. "
                f"{res['deferred_cluster_count']} deferred, "
                f"{res.get('llm_failed_cluster_count', 0)} not reviewed (model failure). "
                f"{res['prune_candidate_count']} pruning candidate(s). "
                f"{(res.get('evolve') or {}).get('proposal_count', 0)} skill-cluster proposal(s)."
            )
            # Task #2088: name what adjudication refused, ids and class only.
            for line in consolidate.refused_cluster_summary(res.get("refused", [])):
                print(line)
        if res.get("artifact_path"):
            print(f"Report written to: {res['artifact_path']}")
    # The default timer command also gets one parseable completion record.
    print(json.dumps(res["telemetry"], sort_keys=True))
    if res["status"] == "error":
        if args.scheduled:
            # Task #1502: record "no model answered" as its own state, so doctor
            # can tell that run apart from a job that failed to run at all.
            state = "llm_unavailable" if res["error_code"] == "llm_unavailable" else "error"
            scheduled_status.finish_scheduled_run("sleep-pass", run_id, state, 1)
        sys.exit(1)
    if res["status"] in {"deferred-preflight", "partial"}:
        if args.scheduled:
            scheduled_status.finish_scheduled_run("sleep-pass", run_id, res["status"], 2)
        sys.exit(2)
    if args.scheduled:
        scheduled_status.finish_scheduled_run("sleep-pass", run_id, res["status"], 0)


if __name__ == "__main__":
    main()
