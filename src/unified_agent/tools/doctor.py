"""Dormant-capability report (Tier 1 easy win, System Leverage Plan 2026-07-20).

Config-gated capabilities (hub auth, approval gates, per-agent workspaces,
optional extras, the automation timers) have repeatedly shipped as code and
then sat inert for days because activation is a config/env step, not a code
change — task #108 found 5 of 7 features in one batch dormant in production
this way. ``system_doctor`` is the one-call check that turns that class of
discovery into a boot-time/post-merge routine instead of an audit-day surprise.

Read-only, with one narrow exception: every check is a config/env read or a
service probe (Linux ``systemctl --user show``, Darwin ``launchctl print``;
shared with ``monitor._service_probe``) — except a zero-runs Darwin timer
check, which may write a single first-seen timestamp for its own LaunchAgent
label via ``storage.record_launchd_first_seen`` (see that table's schema
comment in storage.py). That write is set-once bookkeeping this module owns
for its own timing math, never domain state (tasks/memory/vault), and never
touches anything this report itself doesn't already probe.
"""

import contextvars
import copy
import importlib
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import (
    approval_push,
    backup,
    config,
    post_init,
    scheduled_status,
    service_probes,
    storage,
)

# The service probes moved to ``service_probes`` (task #1722). Every moved name
# is re-exported here; ``_ProbeForwardingModule`` (end of this module) forwards
# a patch of one of them to ``service_probes``, and this module calls the
# patchable ones through ``service_probes.<name>``, so a patch made through
# either module reaches every caller.
from ..service_probes import (  # noqa: F401 - re-exported, see DOCTOR_REEXPORTS
    _MAC_ENVIRONMENT_FILES,
    _SERVICE_UNITS,
    HUB_PROBE_TIMEOUT_S,
    SERVICE_CHECK_TIMEOUT_S,
    _active_executor_check,
    _bounded_ping,
    _check,
    _executor_agent_label,
    _executor_token,
    _hub_auth_probe,
    _literal_shell_exports,
    _load_env_file,
    _mac_environment_files,
    _mac_home,
    _mac_launchagent_environment_layers,
    _mac_launchagent_plist,
    _read_unit_property,
    _running_inside_http_hub,
    _service_env,
    _service_probe,
    _systemctl_memo,
    _unit_display,
    _unit_environment_files,
    _unit_property,
    _unit_property_memo,
    connector_secret_environment,
    launchd_label_for_unit,
)
from . import web

logger = logging.getLogger(__name__)

# Timer/service units this repo installs (deploy/systemd/*, deploy/*.service) —
# kept in sync with deploy/systemd/README.md's unit list by hand.
_TIMER_UNITS = {
    "anomaly_cron": "ua-anomaly-check.timer",
    # ua-consolidate-proposal retired task #884 — sleep_pass covers near-dups.
    "sleep_pass_timer": "ua-sleep-pass.timer",
    "planning_loop_timer": "ua-planning-loop.timer",
    "review_pipeline_cron": "ua-review-pipeline.timer",
    "steward_audit_cron": "ua-steward-audit.timer",
    "cluster_briefing_timer": "cluster-briefing.timer",
    # Task #603; retired by #1435 (see _RETIRED_UNITS). Kept listed so a
    # re-enabled tick is still visible: it was the only timer here whose firing
    # could start an agent process.
    "dispatch_tick_timer": "ua-dispatch-tick.timer",
}
# Units whose lane has been retired. Proven retirement is DORMANT; a loaded
# unit is MISCONFIGURED, and an unavailable/inconclusive probe is UNKNOWN.
# Dispatch retirement also requires the Darwin disabled override, via routing.
_RETIRED_UNITS = {
    # Dispatch lane A retired by task #1435: work is self-pickup (recorded by
    # run_records.py) plus the executor daemons (lane B). The plist stays on
    # disk; the hub has it `launchctl disable`d and booted out.
    "dispatch_tick_timer": (
        "#1435",
        "dispatch lane A retired — self-pickup (run_records) and executor daemons "
        "replaced it; re-enable with launchctl enable + bootstrap only after review",
    ),
    # The daily briefing moved from the Mac's Hermes to the chief of staff
    # (then cloud_agent17) on 2026-10-06, task #1888. The plist stays on disk; the hub
    # has it booted out and `launchctl disable`d, which also stops a later
    # setup run from loading it again.
    "cluster_briefing_timer": (
        "#1888",
        "the daily briefing is produced by the cloud chief of staff, not this "
        "Mac job; re-enable with launchctl enable + bootstrap only after review",
    ),
}
# Retirements that apply only to the Mac LaunchAgent. Elsewhere the same
# capability keeps the ordinary timer check: a Linux host may still run its
# own optional cluster-briefing.timer (docs/installation.md).
_DARWIN_ONLY_RETIRED_UNITS = frozenset({"cluster_briefing_timer"})


def _retirement_applies(capability: str) -> bool:
    if capability not in _RETIRED_UNITS:
        return False
    return capability not in _DARWIN_ONLY_RETIRED_UNITS or sys.platform == "darwin"


# Timers whose own script uses a nonzero exit code as a deliberate signal
# rather than a crash (task #1484). Without this, a periodic Darwin timer
# that ran and exited nonzero on purpose — e.g. steward-audit exiting 1
# because `unified-agent-audit surfaces --fail-on high` found HIGH findings —
# read identically to a genuinely broken timer: MISCONFIGURED "installed but
# inactive". Each entry maps *that unit's own* designed exit code(s) to the
# human explanation shown in the check detail; any other nonzero code for
# that unit still falls through to MISCONFIGURED, so a real failure keeps
# paging as one. A designed code only counts when that run's own status record
# backs it (_TIMER_DESIGNED_EXIT_EVIDENCE): steward-audit also exits 2 when
# its audit binary is missing or not executable, and that run records no
# completion, so it stays MISCONFIGURED.
_TIMER_DESIGNED_EXIT_CODES: dict[str, dict[str, str]] = {
    "steward_audit_cron": {
        "1": "HIGH-severity findings at the --fail-on gate (see ~/.unified_agent/audit.log "
        "and the filed [audit-triage:] task(s)) — not a broken timer",
        # Task #1856: the audit ran and found findings, then writing the inbox
        # note or sending the alert failed (an iMessage cooldown is the usual
        # cause). The audit outcome and the delivery outcome are separate facts.
        "2": "the audit ran and reported HIGH-severity findings, but delivering the "
        "report or alert failed (see the ERROR lines in ~/.unified_agent/audit.log) — "
        "not a broken timer",
    },
    "sleep_pass_timer": {
        # Task #1502: exit 1 with an llm_unavailable record means the job ran and
        # no model answered. That still pages (design D3), but as the model
        # route, not as a timer that is installed and never runs.
        "1": "ran, but no LLM route answered, so nothing was reviewed — see the "
        "llm_provider_chain row; not a broken timer",
        # sleep_pass.py: exit 2 = deferred-preflight/partial (by design),
        # but only a run-specific completion record can establish that.
        "2": "preflight deferred or only a partial pass ran — not a broken timer",
    },
}


def _steward_audit_current_status() -> dict[str, str]:
    """Completion record of the current scheduled steward-audit run, or {}.

    An exit code alone cannot distinguish a designed outcome from a wrapper
    failure. The wrapper writes a bounded status file under $HOME/.unified_agent
    even when UA_HOME is overridden. It is reset before scheduled setup and
    records how the run completed, so log volume and manual audit lines cannot
    substitute for current timer evidence.
    """
    state_dir = Path.home() / ".unified_agent"
    status_file = state_dir / "steward-audit-status"
    if (state_dir / "steward-audit-status.lock").exists():
        return {}
    try:
        pointer = dict(
            line.split("=", 1)
            for line in status_file.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except OSError:
        return {}
    run_id = pointer.get("run_id", "")
    if pointer.get("kind") != "scheduled" or not run_id.startswith("steward-audit-run."):
        return {}
    if not run_id.removeprefix("steward-audit-run.").isalnum():
        return {}
    try:
        fields = dict(
            line.split("=", 1)
            for line in (state_dir / run_id / "status").read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except OSError:
        return {}
    if fields.get("kind") != "scheduled" or fields.get("run_id") != run_id:
        return {}
    return fields


def _steward_audit_completed(state: str, exit_code: str) -> bool:
    fields = _steward_audit_current_status()
    return fields.get("state") == state and fields.get("exit_code") == exit_code


def _steward_audit_gate_tripped() -> bool:
    """True when the latest scheduled steward-audit run tripped the findings gate."""
    return _steward_audit_completed("surfaces_fail", "1")


def _steward_audit_delivery_failed() -> bool:
    """True when the latest scheduled run found findings, then failed to deliver them.

    The wrapper writes ``delivery_fail`` whenever delivery fails, whatever the
    scan returned, so the scan's own exit code must be 1 (findings at the
    gate). A scan that errored (exit 2) or a record without the scan code
    stays unevidenced, and the doctor keeps reporting MISCONFIGURED.
    """
    fields = _steward_audit_current_status()
    return (
        fields.get("state") == "delivery_fail"
        and fields.get("exit_code") == "2"
        and fields.get("audit_exit_code") == "1"
    )


# Evidence each designed exit code must be backed by before it downgrades
# MISCONFIGURED to DEGRADED; codes without an entry need none.
_TIMER_DESIGNED_EXIT_EVIDENCE = {
    "steward_audit_cron": {
        "1": _steward_audit_gate_tripped,
        "2": _steward_audit_delivery_failed,
    },
    "sleep_pass_timer": {
        "1": lambda: _sleep_pass_exit_one_evidenced(),
        "2": lambda: _sleep_pass_exit_two_evidenced(),
    },
}


def _sleep_pass_exit_one_evidenced() -> bool:
    """Require this scheduled run to have recorded that no model answered (#1502)."""
    fields = scheduled_status.current_run_status("sleep-pass")
    return (
        fields.get("kind") == "scheduled"
        and fields.get("state") == "llm_unavailable"
        and fields.get("exit_code") == "1"
    )


def _sleep_pass_exit_two_evidenced() -> bool:
    """Require this scheduled invocation to have completed with a designed status."""
    fields = scheduled_status.current_run_status("sleep-pass")
    return fields.get("kind") == "scheduled" and fields.get("state") in {
        "deferred-preflight",
        "partial",
    }


_EXTRAS = {
    "pdf_extract": ("pypdf", "uv sync --extra pdf"),
    # Required dependency (the tree-sitter extra only existed for the abandoned
    # tree_sitter_languages fallback). The check still reports a missing install.
    "tree_sitter_index": (
        "tree_sitter_language_pack",
        "tree-sitter-language-pack is a required dependency (uv sync)",
    ),
}


# Task #1197 DoD: the GCS replica of memory.db must stay under 60 s of lag.
# `litestream status` is a local read. Replica position is a bounded GCS list
# (binary search for the newest L0 object). A full multi-level LTX listing walks
# every object and exceeds any timeout that would keep the Devices pane usable.
# The sample is cached longer than a doctor poll so a request never re-lists.
REPLICATION_LAG_BUDGET_S = 60.0
REPLICATION_PROBE_TIMEOUT_S = 45.0
REPLICA_LIST_TIMEOUT_S = 3.0
REPLICA_SAMPLE_BUDGET_S = 12.0
REPLICATION_RESULT_CACHE_S = 600.0
_REPLICA_LEVELS = range(10)
# (monotonic, uri, local_txid, rows). Empty samples are not cached.
_replica_cache: tuple[float, str, int, list] | None = None


def reclaim_stale_task_locks() -> dict:
    """Scan the tasks table for expired locks on 'doing' tasks and release them.

    A lapsed lease is released only when its owner probes terminal or absent
    (:mod:`lease_reaper`); an owner that probes unknown is kept and listed.

    Returns {reclaimed_count: N, reclaimed_tasks: [{id, title, claimed_by, expired_at}],
    renewed_count: N, kept_unknown: [{id, claimed_by, warning}]}.
    """
    ts = storage.now()
    reclaimed: list[dict] = []
    kept_unknown: list[dict] = []
    conn = storage.connect()
    try:
        # A lapsed lease held by an agent that is still working is renewed, not
        # stolen (task #1337): doctor runs constantly (briefings, install gate),
        # so releasing here reopened live work and drove claim thrash.
        from ..queue_hygiene import _parse_ts, renew_live_claims

        renewed = renew_live_claims(conn, _parse_ts(ts))
        if renewed:
            conn.commit()
        # One writer on an expired claim (task #2395): the same probe-gated
        # release queue hygiene uses, so doctor cannot undo a kept owner.
        from .. import lease_reaper

        reaped = lease_reaper.reap_stale_claim_leases(conn, now=_parse_ts(ts))
        reclaimed = [
            {
                "id": r.task_id,
                "title": r.title,
                "claimed_by": r.owner or None,
                "expired_at": r.expired_at,
                "to_status": r.to_status,
                "interrupted_run": r.interrupted_run,
            }
            for r in reaped
            if r.action == lease_reaper.RELEASED
        ]
        kept_unknown = [
            {"id": r.task_id, "claimed_by": r.owner, "warning": r.warning}
            for r in reaped
            if r.action == lease_reaper.KEPT and r.warning
        ]
        if not reclaimed and conn.in_transaction:
            conn.rollback()
        if reclaimed:
            conn.commit()
            from .. import run_records
            from ..sync import schedule_sync_to_vault

            # Task #1436: each reclaimed stale lock is an abandoned run.
            for r in reclaimed:
                run_records.safe(
                    run_records.record_abandoned,
                    r["id"],
                    agent=r["claimed_by"],
                    closed_by="system-doctor",
                    reason="lease expired; stale lock reclaimed by system_doctor",
                    reason_code=run_records.REASON_LEASE_EXPIRED,
                    to_status=r["to_status"],
                )
                # Activity, bus and heartbeat bookkeeping use their own write
                # connections. Release this transaction's writer lock first;
                # otherwise they time out and silently lose reclamation events.
                storage.record_agent_event(
                    "task",
                    ref_id=str(r["id"]),
                    agent=r["claimed_by"],
                    summary=f"stale lock reclaimed: {r['title']}",
                    # Doctor performed the release; the former claimant must
                    # not look alive and renew its other expired claims.
                    heartbeat=False,
                )
            lease_reaper.publish_after_commit(reaped)
            schedule_sync_to_vault()
    finally:
        conn.close()

    return {
        "reclaimed_count": len(reclaimed),
        "reclaimed_tasks": reclaimed,
        "renewed_count": len(renewed),
        "kept_unknown": kept_unknown,
    }


# web_ui reason for an install with no UI build and no UA_WEB_DIST_DIR override.
UI_NOT_BUILT = "ui_not_built"


# How far back a tenant beacon counts as a recent writer (task #1996).
TENANT_BEACON_WINDOW_S = 24 * 3600


class _HubWorkdirUnknown(Exception):
    """A relative hub path whose base (the unit's working directory) is unknown."""


def _hub_working_directory(unit: str) -> str | None:
    """The hub's working directory, or None when it cannot be read.

    systemd reports it as a unit property. launchd has none: on Darwin the
    LaunchAgent plist's ``WorkingDirectory`` decides, and a plist without one
    starts the job in ``/`` (#1153).
    """
    workdir = _unit_property(unit, "WorkingDirectory")
    if workdir is not None or sys.platform != "darwin":
        return workdir
    plist_path = service_probes._mac_launchagent_plist(unit)
    if plist_path is None:
        return None
    try:
        with plist_path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return None
    if not isinstance(data, dict):
        return None
    # Assumes the launcher script does not cd first (run-hub.sh does not).
    workdir = data.get("WorkingDirectory", "/")
    return workdir if isinstance(workdir, str) and workdir else None


def _hub_tenant_org(environ: dict[str, str] | None = None) -> str | None:
    """The hub's one organization, from ``edition.configured_org_id``.

    Accept the reconstructed hub environment after settings are applied, or
    read unit posture values when called independently. The key order lives
    only in ``edition``; a blank pilot variable must not hide the hosted org.
    """
    from .. import edition

    if environ is None:
        environ = {key: _hub_posture_setting(key)[0] or "" for key in edition._ORG_ENV_KEYS}
    return edition.configured_org_id(environ)


def _tenant_consistency_check() -> dict:
    """DEGRADED when a process sharing the hub's data dir stamps another tenant.

    ``edition.tenant_org_id()`` reads each process's own environment, so an
    executor or CLI started without the hub's ``UA_EDITION`` and organization
    (``WORKOS_ORGANIZATION_ID``, or the hosted hub's
    ``UA_HUB_WORKOS_ORGANIZATION_ID``) writes rows under a different
    ``org_id`` (NULL, usually) that the hub can neither see nor end (task
    #1996). Every process that opens the store leaves a per-process beacon
    naming the tenant it stamps (``edition.record_tenant_beacon``); this
    compares the beacons seen in the last day with the hub unit's tenant.
    """
    from .. import edition

    gate = (
        "UA_EDITION / WORKOS_ORGANIZATION_ID / UA_HUB_WORKOS_ORGANIZATION_ID "
        "(every process sharing UA_HOME)"
    )
    try:
        hub_unit = _SERVICE_UNITS["hub_service"]
        # An unreadable or empty unit leaves _hub_posture_setting on the
        # caller's own environment, whose relative paths are the caller's.
        from_unit = bool(_service_env(hub_unit))

        def hub_path(raw: str) -> Path:
            # A relative path in the unit is relative to the hub's working
            # directory, not to the shell running the doctor (systemd user
            # units default to the home directory).
            path = Path(raw).expanduser()
            if path.is_absolute() or not from_unit:
                return path.absolute()
            workdir = _hub_working_directory(hub_unit)
            if workdir is None or (workdir and not Path(workdir).expanduser().is_absolute()):
                raise _HubWorkdirUnknown(raw)
            return (Path(workdir).expanduser() if workdir else Path.home()) / path

        home_raw, home_determined = _hub_posture_setting("UA_HOME")
        # A readable unit omitting UA_HOME chose the hub default, independent
        # of an operator shell pointing at another store.
        if home_raw:
            data_dir = hub_path(home_raw)
        elif home_determined:
            data_dir = Path.home() / ".unified_agent"
        else:
            data_dir = config.data_home()
        # Reconstruct the same effective environment as the hub. Settings can
        # supply the hosted org even when the unit explicitly sets the edition.
        # Do not borrow an operator shell's organization when the unit omits it.
        from .. import settings as settings_mod

        hub_env = {"UA_HOME": str(data_dir)}
        for key in ("UA_EDITION", *edition._ORG_ENV_KEYS):
            raw = _hub_posture_setting(key)[0]
            if raw is not None:
                hub_env[key] = raw
        settings_file, settings_determined = _hub_posture_setting("UA_SETTINGS_FILE")
        if settings_file is not None and settings_determined:
            hub_env["UA_SETTINGS_FILE"] = (
                str(hub_path(settings_file)) if settings_file.strip() else settings_file
            )
        settings_mod.apply_to_environ(hub_env, quiet=True)
        hub_tenant = edition.tenant_for(hub_env.get("UA_EDITION"), _hub_tenant_org(hub_env))
        beacons = edition.read_tenant_beacons(data_dir, max_age_s=TENANT_BEACON_WINDOW_S)
    except _HubWorkdirUnknown as exc:
        return _check(
            "tenant_consistency",
            "UNKNOWN",
            f"the hub unit sets a relative path ({exc.args[0]!r}) and its working "
            "directory cannot be read",
            gate,
        )
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check(
            "tenant_consistency", "UNKNOWN", f"tenant beacon probe failed: {exc}"[:200], gate
        )

    def name(tenant: str | None) -> str:
        return tenant or "no tenant"

    mismatched = [b for b in beacons if b["org_id"] != hub_tenant]
    if mismatched:
        listed = "; ".join(
            f"{b['process']} (pid {b['pid']}) stamps {name(b['org_id'])}" for b in mismatched[:5]
        )
        more = f"; and {len(mismatched) - 5} more" if len(mismatched) > 5 else ""
        return _check(
            "tenant_consistency",
            "DEGRADED",
            f"{len(mismatched)} of {len(beacons)} process(es) using {data_dir} in the last 24h "
            f"stamp a tenant other than the hub's ({name(hub_tenant)}): {listed}{more}. "
            "The hub cannot see or end their rows; give every process that shares this "
            "data directory the hub's UA_EDITION and WORKOS_ORGANIZATION_ID "
            "(or UA_HUB_WORKOS_ORGANIZATION_ID)",
            gate,
        )
    if hub_tenant is None:
        return _check(
            "tenant_consistency",
            "DORMANT",
            f"tenant boundary off on the hub; {len(beacons)} process(es) using {data_dir} "
            "in the last 24h stamp no tenant either",
            gate,
        )
    if not beacons:
        return _check(
            "tenant_consistency",
            "UNKNOWN",
            f"hub tenant is {hub_tenant}, but no process using {data_dir} recorded a "
            "tenant beacon in the last 24h",
            gate,
        )
    return _check(
        "tenant_consistency",
        "LIVE",
        f"{len(beacons)} process(es) using {data_dir} in the last 24h all stamp the "
        f"hub's tenant ({hub_tenant})",
        gate,
    )


# An HTTP hub holds a descriptor per client socket and per open store
# connection; launchd's default soft limit of 256 was exhausted in production
# (PR #1001). The LaunchAgent template sets 8192, but an installed plist is
# only re-read on bootout/bootstrap, so report the limit the process got.
_HUB_MIN_OPEN_FILES = 4096


def _open_files_limit_check() -> dict:
    gate = "launchd SoftResourceLimits.NumberOfFiles / RLIMIT_NOFILE"
    try:
        if config.transport() != "http":
            return _check("hub_open_files_limit", "DORMANT", "not an HTTP hub process", gate)
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check("hub_open_files_limit", "UNKNOWN", f"cannot read limit: {exc}", gate)
    if soft != resource.RLIM_INFINITY and soft < _HUB_MIN_OPEN_FILES:
        return _check(
            "hub_open_files_limit",
            "DEGRADED",
            f"soft open-files limit {soft} < {_HUB_MIN_OPEN_FILES}; add SoftResourceLimits "
            "NumberOfFiles to the installed hub plist, then launchctl bootout + bootstrap",
            gate,
        )
    return _check("hub_open_files_limit", "LIVE", f"soft open-files limit {soft}", gate)


def _operator_identity_check() -> dict:
    """Which login is the human operator, and whether configuration was refused.

    A refused label or alias otherwise shows only as a one-time stderr line,
    while the operator silently loses approval authority.
    """
    gate = "UA_OPERATOR_LABEL / UA_OPERATOR_NAME / UA_OPERATOR_ALIASES"
    try:
        label = config.operator_label()
        if label == config.UNASSIGNED_OPERATOR_LABEL:
            return _check(
                "operator_identity",
                "MISCONFIGURED",
                f"no human operator: the default label {config.DEFAULT_OPERATOR_LABEL!r} "
                "names a worker; set operator.label to a separate human login",
                gate,
            )
        problems = []
        raw = (os.environ.get("UA_OPERATOR_LABEL") or "").strip()
        if raw and raw != label:
            problems.append(
                f"UA_OPERATOR_LABEL {raw!r} {config.operator_label_problem(raw)}; using {label!r}"
            )
        claimed = {(os.environ.get("UA_OPERATOR_NAME") or "").strip()}
        claimed |= {
            part.strip() for part in (os.environ.get("UA_OPERATOR_ALIASES") or "").split(",")
        }
        claimed -= {"", label}
        aliases = config.operator_aliases()
        refused = sorted(name for name in claimed if name not in aliases)
        if refused:
            problems.append(f"refused names that belong to a worker: {refused!r}")
        tokens = config.agent_tokens()
        if tokens and label not in set(tokens.values()):
            problems.append(f"no bearer resolves to the operator login {label!r}")
        summary = f"operator login {label!r}, name {config.operator_name()!r}"
        if problems:
            return _check(
                "operator_identity", "MISCONFIGURED", f"{summary}; " + "; ".join(problems), gate
            )
        extra = sorted(aliases - {config.operator_name()})
        if extra:
            summary += f", aliases {extra!r}"
        return _check("operator_identity", "LIVE", summary, gate)
    except Exception as exc:
        return _check("operator_identity", "UNKNOWN", f"cannot evaluate: {exc}", gate)


def _stale_task_locks_check() -> dict:
    res = reclaim_stale_task_locks()
    count = res["reclaimed_count"]
    if count > 0:
        return _check(
            "stale_task_locks",
            "MISCONFIGURED",
            f"reclaimed {count} stale task lock(s): "
            + ", ".join(f"#{t['id']}" for t in res["reclaimed_tasks"]),
            "task_claim lock_expires_at",
        )
    kept = res["kept_unknown"]
    if kept:
        return _check(
            "stale_task_locks",
            "DEGRADED",
            f"{len(kept)} lapsed lease(s) kept, owner neither live nor recorded as finished: "
            + ", ".join(f"#{t['id']}" for t in kept),
            "task_claim lock_expires_at",
        )
    return _check(
        "stale_task_locks",
        "LIVE",
        "no stale task locks found",
        "task_claim lock_expires_at",
    )


def _queue_backpressure_check() -> dict:
    """Queue flow as an operator signal (task #1430). Read-only; never raises."""
    gate = "task_claim leases, UA_AGENT_WIP_MAX, UA_AGENT_WIP_OVERRIDES, UA_LANE_WIP"
    try:
        from ..queue_hygiene import queue_pressure

        report = queue_pressure()
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check("queue_backpressure", "UNKNOWN", f"queue read failed: {exc}"[:200], gate)
    depth = report["depth"]
    claims = report["claims"]
    summary = (
        f"open {depth['open']}, doing {depth['doing']}, waiting_review "
        f"{depth['waiting_review']}, blocked {depth['blocked']}; oldest claim "
        f"{claims['oldest_claim_hours'] if claims['oldest_claim_hours'] is not None else '-'}h"
    )
    if report["signals"]:
        return _check(
            "queue_backpressure", "DEGRADED", f"{summary}; " + "; ".join(report["signals"]), gate
        )
    notes = report.get("notes") or []
    detail = f"queue flowing: {summary}" + (f"; note: {'; '.join(notes)}" if notes else "")
    return _check("queue_backpressure", "LIVE", detail[:500], gate)


def _command_items_unsettled_check() -> dict:
    """Supervised node commands that did not settle (task #2413).

    DEGRADED when a claimed command is past its lease by more than the grace
    (the node or its run vanished), or an owner's completion message is still
    unsent past the grace; LIVE otherwise. Read-only; never raises.
    """
    gate = "node_command, node_command_notice; queue hygiene redelivers messages"
    try:
        from .. import node_command

        found = node_command.unsettled()
        grace = node_command.UNSETTLED_GRACE_S
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check("command_items_unsettled", "UNKNOWN", f"read failed: {exc}"[:200], gate)
    overdue, unsent = found["overdue"], found["unsent"]
    if overdue or unsent:
        parts = []
        if overdue:
            parts.append(f"{len(overdue)} claimed past lease: {', '.join(overdue[:10])}")
        if unsent:
            parts.append(f"{len(unsent)} owner message(s) unsent: {', '.join(unsent[:10])}")
        detail = f"over {grace // 60} min: " + "; ".join(parts)
        return _check("command_items_unsettled", "DEGRADED", detail[:500], gate)
    return _check(
        "command_items_unsettled",
        "LIVE",
        f"no supervised command unsettled past {grace // 60} min",
        gate,
    )


def _dispatch_unsettled_spawns_check() -> dict:
    """Dispatch runs whose spawn has no recorded outcome, holding their task (#2512).

    DEGRADED once any is past its lease longer than the grace
    (``coordinator.UNSETTLED_SPAWN_GRACE_S``), naming each run and the settle
    route; LIVE otherwise. Read-only; never raises.
    """
    gate = "dispatch_launch_keys spawn marks, POST /api/dispatch-runs/{run_id}/settle-spawn"
    try:
        from ..dispatch import coordinator

        held = coordinator.unsettled_spawns()
        grace = coordinator.UNSETTLED_SPAWN_GRACE_S
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check("dispatch_unsettled_spawns", "UNKNOWN", f"read failed: {exc}"[:200], gate)
    stuck = [h for h in held if h["past_lease_s"] is not None and h["past_lease_s"] >= grace]
    if stuck:
        runs = ", ".join(f"{h['run_id']} (task #{h['task_id']})" for h in stuck[:10])
        more = f" and {len(stuck) - 10} more" if len(stuck) > 10 else ""
        detail = (
            f"{len(stuck)} spawn(s) unsettled over {grace // 60} min past their lease, each "
            f"holding its task: {runs}{more}. If the launcher crashed, settle each with "
            "POST /api/dispatch-runs/{run_id}/settle-spawn (operator)"
        )
        return _check("dispatch_unsettled_spawns", "DEGRADED", detail[:500], gate)
    detail = f"no spawn unsettled past the {grace // 60} min grace" + (
        f"; {len(held)} within it" if held else ""
    )
    return _check("dispatch_unsettled_spawns", "LIVE", detail, gate)


def _wip_policy_check() -> dict:
    """The WIP numbers the running hub will actually enforce (task #2759).

    Production ran a per-agent cap of 20 while the docs said 2, and the fleet
    soft cap equalled the per-agent cap, so the fleet warning fired at the same
    claim as the per-agent refusal. Report the effective values so the next
    reader does not have to source service.env to find them, and say so when
    the fleet ceiling does not sit above the per-agent cap.
    """
    gate = (
        "UA_AGENT_WIP_MAX, UA_AGENT_WIP_OVERRIDES, UA_FLEET_WIP_MAX, "
        "UA_FLEET_WIP_HARD_MAX, UA_LANE_WIP"
    )
    try:
        from .. import config

        per_agent = config.agent_wip_max()
        overrides = config.agent_wip_overrides()
        soft = config.fleet_wip_max()
        hard = config.fleet_wip_hard_max()
        lanes = config.lane_wip_limits()
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check("wip_policy", "UNKNOWN", f"config read failed: {exc}"[:200], gate)
    detail = (
        f"per agent {per_agent}; fleet soft {soft}, hard {hard}; "
        f"overrides {json.dumps(overrides, sort_keys=True)}; "
        f"lane caps {json.dumps(lanes, sort_keys=True)}"
    )
    if soft <= per_agent or hard <= per_agent:
        return _check(
            "wip_policy",
            "DEGRADED",
            detail + " — the fleet ceiling does not sit above the per-agent cap, "
            "so the fleet gate fires at the same claim",
            gate,
        )
    return _check("wip_policy", "LIVE", detail, gate)


def _queue_closure_check() -> dict:
    """Open/parked/stale counts and unjudged review-ready milestones (task #1848).

    Informational: staleness and an unjudged milestone are things for the operator
    to act on, not a fault in the queue machinery, so this row is LIVE unless the
    read itself fails. Read-only; never raises.
    """
    gate = "UA_QUEUE_STALE_DAYS, queue_closure"
    try:
        from ..queue_closure import queue_health

        report = queue_health()
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check("queue_closure", "UNKNOWN", f"queue read failed: {exc}"[:200], gate)
    detail = (
        f"open {report['open']}, parked {report['parked']}, stale {report['stale']} "
        f"(>= {report['stale_days']}d, {report['stale_actionable']} proposable)"
    )
    unjudged = report["milestones"]["unjudged_ids"]
    if unjudged:
        detail += "; review-ready milestone(s) unjudged: " + ", ".join(f"#{i}" for i in unjudged)
    states = ", ".join(f"{s} {n}" for s, n in report["ownership_states"].items() if n)
    if states:
        detail += f"; ownership: {states}"
    expired = report["deployment_waits"]["expired"]
    if expired:
        detail += "; deployment wait(s) expired: " + ", ".join(f"#{w['task_id']}" for w in expired)
    return _check("queue_closure", "LIVE", detail[:500], gate)


def _session_owed_check() -> dict:
    """What ended sessions left behind over the last week (task #2389). Report only."""
    gate = "session_owed (report only)"
    try:
        from ..session_owed import summary

        report = summary()
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check("session_owed", "UNKNOWN", f"owed read failed: {exc}"[:200], gate)
    if not report["sessions"]:
        return _check("session_owed", "DORMANT", "no ended session reported in 7 days", gate)
    detail = (
        f"7d: {report['sessions']} session(s) reported, {report['with_owed']} left work owed "
        f"({report['items']} item(s)); {report['shared_label']} of those had another open "
        f"session on the label, the likely false positives; {report['turn_sets']} distinct "
        "owed set(s) seen at turn end"
    )
    return _check("session_owed", "LIVE", detail, gate)


def _hub_auth_check() -> dict:
    raw, determined = _hub_setting("UA_AGENT_TOKENS")
    if not determined:
        return _check(
            "hub_auth",
            "MISCONFIGURED",
            "cannot determine the hub's auth configuration (unit not installed or "
            "unreadable) — this is 'cannot tell', not 'not configured'",
            "UA_AGENT_TOKENS",
        )
    try:
        tokens = _hub_service_tokens(raw)
    except Exception as exc:
        return _check(
            "hub_auth",
            "MISCONFIGURED",
            f"invalid UA_AGENT_TOKENS: {exc}",
            "UA_AGENT_TOKENS",
        )
    workos = _workos_leg_status()
    gate = "UA_AGENT_TOKENS / UA_HUB_WORKOS_ORGANIZATION_ID / UA_ENFORCE_AUTH"
    if not tokens and workos is None:
        return _check(
            "hub_auth",
            "DORMANT",
            "no bearer tokens configured — every HTTP caller is anonymous",
            "UA_AGENT_TOKENS",
        )
    enforce_raw, _ = _hub_posture_setting("UA_ENFORCE_AUTH")
    transport_raw, _ = _hub_posture_setting("UA_TRANSPORT")
    legacy_raw, _ = _hub_posture_setting("UA_LEGACY_OPEN_POSTURE")
    enforce = config.enforce_auth_from_settings(enforce_raw, transport_raw, legacy_raw)
    mode = (
        "enforced (unauthenticated calls rejected)"
        if enforce
        else ("warn-mode (unauthenticated calls logged, not rejected)")
    )
    if workos is None:
        return _check(
            "hub_auth",
            "LIVE",
            f"{len(tokens)} token(s) configured, {mode}",
            "UA_AGENT_TOKENS / UA_ENFORCE_AUTH",
        )
    # Task #2114: a hub whose humans sign in with WorkOS is configured even
    # with no bearer tokens. Nothing here proves the WorkOS credentials work;
    # only a real sign-in does, so the row never says they are valid.
    if workos["problem"]:
        leg = f"WorkOS hub leg set but unusable: {workos['problem']}"
        status = "DEGRADED" if tokens else "MISCONFIGURED"
    else:
        leg = f"WorkOS hub leg {workos['summary']}"
        status = "LIVE"
    head = f"{len(tokens)} token(s) configured" if tokens else "no bearer tokens"
    return _check("hub_auth", status, f"{head}; {leg}; {mode}", gate)


def _workos_leg_status() -> dict | None:
    """The hub's WorkOS leg as configured, or None when it is not turned on.

    ``{"problem": str | None, "summary": str}``. Settings are read like the
    rest of the doctor, from the hub's own unit first. The checks are local
    (a usable identity map, every provider setting present, an HTTPS issuer and
    callback, a Fernet cookie key). They cannot tell whether WorkOS accepts the
    API key or knows the client id, so a passing leg is reported as
    "configured (not verified live)", never as valid (task #2114).
    """
    edition_raw, _ = _hub_setting("UA_EDITION")
    org, _ = _hub_setting("UA_HUB_WORKOS_ORGANIZATION_ID")
    if (edition_raw or "enterprise").strip().lower() != "enterprise" or not (org or "").strip():
        return None
    raw_map, _ = _hub_setting("UA_HUB_WORKOS_IDENTITY_MAP")
    try:
        identity_map = json.loads(raw_map or "")
    except ValueError:
        identity_map = None
    if not isinstance(identity_map, dict) or not identity_map:
        return {"problem": "UA_HUB_WORKOS_IDENTITY_MAP is missing or not a JSON object"}
    names = (
        "WORKOS_API_KEY",
        "WORKOS_CLIENT_ID",
        "WORKOS_ISSUER",
        "WORKOS_REDIRECT_URI",
        "WORKOS_COOKIE_PASSWORD",
    )
    settings = {name: (_hub_setting(name)[0] or "").strip() for name in names}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        return {"problem": "missing " + ", ".join(missing)}
    try:
        from ..pilot_auth import WorkOSSDKProvider
        from ..workos_hub_auth import WorkOSHubAuth

        provider = WorkOSSDKProvider(
            object(),  # never called: this is a shape check, not a live one
            redirect_uri=settings["WORKOS_REDIRECT_URI"],
            cookie_password=settings["WORKOS_COOKIE_PASSWORD"],
            organization_id=org.strip(),
            issuer=settings["WORKOS_ISSUER"],
        )
        WorkOSHubAuth(provider, organization_id=org.strip(), identity_map=identity_map)
    except Exception as exc:  # noqa: BLE001 - reported in-band; no setting values echoed
        return {"problem": f"provider settings rejected ({type(exc).__name__})"}
    known = sum(1 for label in identity_map.values() if config.is_known_agent_label(label))
    if not known:
        return {"problem": "no UA_HUB_WORKOS_IDENTITY_MAP label is a known hub identity"}
    return {
        "problem": None,
        "summary": (
            f"configured for organization {org.strip()} with {known} mapped identit"
            f"{'y' if known == 1 else 'ies'} (configured, not verified live)"
        ),
    }


def _agent_token_collisions_check() -> dict:
    """Name the labels whose bearer token the hub refuses as a collision (task #1657).

    ``config.agent_tokens`` merges ``UA_AGENT_TOKENS`` with the managed
    ``agent_tokens`` store and fails closed when one token maps to two labels:
    that token authenticates no one. A client resolves its bearer from its own
    environment, so a cross-source collision looks configured on the client
    and is refused by the hub. Labels and sources only, never a token value.
    """
    gate = "UA_AGENT_TOKENS / managed agent_tokens store"
    raw, determined = _hub_setting("UA_AGENT_TOKENS")
    if not determined:
        return _check(
            "agent_token_collisions",
            "UNKNOWN",
            "cannot determine the hub's auth configuration (unit not installed or unreadable)",
            gate,
        )
    try:
        groups = _hub_service_tokens(raw, probe="agent_token_collisions")
    except Exception as exc:  # noqa: BLE001 - a doctor probe never raises
        return _check(
            "agent_token_collisions", "UNKNOWN", f"cannot read the hub's tokens: {exc}"[:200], gate
        )
    if not groups:
        return _check(
            "agent_token_collisions", "LIVE", "no two agent labels share a bearer token", gate
        )
    parts = []
    for group in groups:
        where = (
            "across UA_AGENT_TOKENS and the managed store"
            if group.get("cross_source")
            else f"within {', '.join(group.get('sources') or [])}"
        )
        parts.append(f"{', '.join(group['labels'])} share one token ({where})")
    return _check(
        "agent_token_collisions",
        "MISCONFIGURED",
        "; ".join(parts) + " — the hub refuses that token for every one of these labels; "
        "remove or rotate the duplicate registration",
        gate,
    )


@service_probes._systemctl_memo
def _deployment_mode_check() -> dict:
    """Doctor check for canonical deployment mode and transport/auth consistency (task #1422)."""
    mode_raw, mode_src = _hub_posture_setting("UA_DEPLOYMENT_MODE")
    enforce_raw, _ = _hub_posture_setting("UA_ENFORCE_AUTH")
    transport_raw, _ = _hub_posture_setting("UA_TRANSPORT")
    legacy_raw, _ = _hub_posture_setting("UA_LEGACY_OPEN_POSTURE")
    public_raw, _ = _hub_posture_setting("UA_AUTHENTICATED_PUBLIC")
    public_url_raw, _ = _hub_posture_setting("UA_PUBLIC_URL")
    public_hosts_raw, _ = _hub_posture_setting("UA_HTTP_PUBLIC_HOSTS")

    try:
        resolved_mode, gate = config.deployment_mode_from_settings(
            mode_raw,
            enforce_raw,
            transport_raw,
            legacy_raw,
            public_raw,
            public_url_raw,
            public_hosts_raw=public_hosts_raw,
        )
    except config.DeploymentModeError as exc:
        return _check(
            "deployment_mode",
            "MISCONFIGURED",
            str(exc),
            "UA_DEPLOYMENT_MODE",
        )

    enforce = config.enforce_auth_from_settings(enforce_raw, transport_raw, legacy_raw)
    is_public = (public_raw or "").strip().lower() in config._ENV_ON

    try:
        tokens_dict = _hub_service_tokens()
    except Exception as exc:
        return _check(
            "deployment_mode",
            "MISCONFIGURED",
            f"invalid UA_AGENT_TOKENS: {exc}",
            "UA_AGENT_TOKENS",
        )

    from .. import edition

    edition_raw, _ = _hub_posture_setting("UA_EDITION")
    hub_edition = (edition_raw or edition.EDITION_ENTERPRISE).strip().lower()
    if hub_edition not in edition.VALID_EDITIONS:
        hub_edition = edition.EDITION_ENTERPRISE
    workos_org_raw, _ = _hub_posture_setting("UA_HUB_WORKOS_ORGANIZATION_ID")
    workos_map_raw, _ = _hub_posture_setting("UA_HUB_WORKOS_IDENTITY_MAP")
    try:
        workos_map = json.loads(workos_map_raw or "") if workos_map_raw else None
    except ValueError:
        workos_map = None
    workos_api_key = (_hub_posture_setting("WORKOS_API_KEY")[0] or "").strip()
    workos_client_id = (_hub_posture_setting("WORKOS_CLIENT_ID")[0] or "").strip()
    workos_issuer = (_hub_posture_setting("WORKOS_ISSUER")[0] or "").strip()
    workos_redirect = (_hub_posture_setting("WORKOS_REDIRECT_URI")[0] or "").strip()
    workos_cookie = (_hub_posture_setting("WORKOS_COOKIE_PASSWORD")[0] or "").strip()

    try:
        from urllib.parse import urlparse

        from cryptography.fernet import Fernet

        workos_provider_ok = bool(
            workos_api_key
            and workos_client_id
            and len(workos_cookie) >= 32
            and urlparse(workos_redirect).scheme == "https"
            and urlparse(workos_issuer).scheme == "https"
        )
        if workos_provider_ok:
            Fernet(workos_cookie.encode())
    except Exception:
        workos_provider_ok = False

    workos_map_ok = (
        isinstance(workos_map, dict)
        and bool(workos_map)
        and not any(
            not isinstance(k, str) or not isinstance(v, str) or not k or not v or len(v) > 100
            for k, v in workos_map.items()
        )
    )
    workos_enabled = bool(
        hub_edition == edition.EDITION_ENTERPRISE
        and (workos_org_raw or "").strip()
        and workos_map_ok
        and workos_provider_ok
    )

    oauth_flag, _ = _hub_posture_setting("UA_MCP_OAUTH")
    oauth_issuer, _ = _hub_posture_setting("UA_MCP_OAUTH_ISSUER")
    oauth_passphrase, _ = _hub_posture_setting("UA_MCP_OAUTH_PASSPHRASE")
    oauth_logins, _ = _hub_posture_setting("UA_MCP_OAUTH_APPROVER_LOGINS")
    oauth_agent, _ = _hub_posture_setting("UA_MCP_OAUTH_AGENT")
    has_oauth_approver = bool(
        (oauth_passphrase or "").strip() or (oauth_logins or "").strip() or workos_enabled
    )
    target_agent = (oauth_agent or config.mcp_oauth_agent()).strip()
    try:
        issuer_ok = bool(
            (oauth_issuer or "").strip()
            and config._mcp_oauth_issuer_ok((oauth_issuer or "").strip().rstrip("/"))
        )
    except Exception:
        issuer_ok = False
    oauth_enabled = bool(
        (oauth_flag or "").strip() == "1"
        and issuer_ok
        and has_oauth_approver
        and config.is_known_agent_label(target_agent)
    )

    tokens_configured = bool(tokens_dict or oauth_enabled or workos_enabled)
    host_raw, _ = _hub_posture_setting("UA_HTTP_HOST")

    try:
        config.validate_deployment_mode(
            resolved_mode,
            enforce=enforce,
            tokens_configured=tokens_configured,
            public=is_public,
            public_url=public_url_raw or "",
            public_hosts=public_hosts_raw or "",
            transport=transport_raw or config.transport(),
            http_host=host_raw or "127.0.0.1",
        )
    except config.DeploymentModeError as exc:
        return _check(
            "deployment_mode",
            "MISCONFIGURED",
            str(exc),
            "UA_DEPLOYMENT_MODE / UA_ENFORCE_AUTH",
        )

    detail = f"canonical mode '{resolved_mode}' ({gate})"
    return _check(
        "deployment_mode",
        "LIVE",
        detail,
        "UA_DEPLOYMENT_MODE",
    )


def _agent_token_lifecycle_check() -> dict:
    """Warn on active ``agent_tokens`` rows older than ``UA_TOKEN_MAX_AGE_DAYS``.

    This checks the metadata table (task #1198: hash + issued_at, never a
    plaintext value), not ``UA_AGENT_TOKENS`` itself — a hub can have live
    bearer tokens configured (``hub_auth`` above) with no lifecycle metadata
    recorded yet, e.g. tokens issued before this table existed. That state is
    DORMANT ("nothing to warn about yet"), not an error: metadata only exists
    for tokens issued through ``agent_token_issue``/``agent_token_rotate``.
    """
    from ..tools.token_lifecycle import stale_active_tokens

    gate = "UA_TOKEN_MAX_AGE_DAYS"
    try:
        max_age = config.token_max_age_days()
        stale = stale_active_tokens(max_age)
        conn = storage.connect()
        try:
            from .. import edition

            total_active = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_tokens WHERE status = 'active' AND org_id IS ?",
                (edition.tenant_org_id(),),
            ).fetchone()["n"]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check(
            "agent_token_lifecycle", "MISCONFIGURED", f"lifecycle probe failed: {exc}"[:200], gate
        )

    if total_active == 0:
        return _check(
            "agent_token_lifecycle",
            "DORMANT",
            "no agent_tokens metadata recorded yet (tokens issued outside "
            "agent_token_issue/rotate have no lifecycle row)",
            gate,
        )
    if stale:
        oldest = max(t["age_days"] for t in stale if t["age_days"] is not None)
        labels = sorted({t["agent_label"] for t in stale})
        return _check(
            "agent_token_lifecycle",
            "MISCONFIGURED",
            f"{len(stale)} active token(s) older than {max_age}d (oldest {oldest}d): "
            f"{', '.join(labels)} — rotation is due",
            gate,
        )
    return _check(
        "agent_token_lifecycle",
        "LIVE",
        f"{total_active} active token(s) tracked, all within {max_age}d",
        gate,
    )


def _retired_agent_credentials_check() -> dict:
    """Retired agents that still hold a working credential (task #2305). Must be zero.

    A retire revokes the label's ``agent_tokens`` rows, session credentials and
    device sign-ins (OAuth refresh and access tokens, task #2315) in one
    transaction, so a non-zero count means a credential was issued after the
    retirement or the store was edited by hand. A label still listed in the
    hub's ``UA_AGENT_TOKENS`` is reported separately: that bearer keeps
    authenticating whatever the database says, and only removing it from the
    env (then restarting the hub) stops it. Labels and counts only, never a value.
    """
    from .. import agent_lifecycle, edition, oauth_store

    capability = "retired_agent_credentials"
    gate = "agent_retire / UA_AGENT_TOKENS"
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT json_extract(payload, '$.agent') AS agent FROM event_log "
                "WHERE channel=? AND json_extract(payload, '$.kind')=?",
                (agent_lifecycle.CHANNEL, agent_lifecycle.KIND_CHANGED),
            ).fetchall()
            retired = sorted(
                r["agent"]
                for r in rows
                if r["agent"] and agent_lifecycle.retired_state(r["agent"], conn=conn)
            )
            problems: list[str] = []
            for label in retired:
                tokens = conn.execute(
                    "SELECT COUNT(*) AS n FROM agent_tokens WHERE agent_label=? "
                    "AND status='active' AND org_id IS ?",
                    (label, edition.tenant_org_id()),
                ).fetchone()["n"]
                try:
                    sessions = conn.execute(
                        "SELECT COUNT(*) AS n FROM session_credentials WHERE revoked_at IS NULL "
                        "AND (agent_label=? OR device_agent=?)",
                        (label, label),
                    ).fetchone()["n"]
                except Exception:  # noqa: BLE001 - a store without the table holds none
                    sessions = 0
                try:
                    signins = oauth_store.live_signins_for(conn, label)
                except Exception:  # noqa: BLE001 - a store without the tables holds none
                    signins = 0
                if tokens:
                    problems.append(f"{label}: {tokens} active agent_tokens row(s)")
                if sessions:
                    problems.append(f"{label}: {sessions} live session credential(s)")
                if signins:
                    problems.append(f"{label}: {signins} live device sign-in token(s)")
        finally:
            conn.close()
        raw, determined = _hub_setting("UA_AGENT_TOKENS")
        env_source = (raw or "") if determined else None
        env_labels = {
            label
            for label, _token, source in config.agent_token_entries(env_source, False)
            if source == "UA_AGENT_TOKENS"
        }
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check(capability, "MISCONFIGURED", f"probe failed: {exc}"[:200], gate)
    in_env = [label for label in retired if label in env_labels]
    if in_env:
        problems.append(
            f"{', '.join(in_env)}: still a bearer in UA_AGENT_TOKENS — remove from "
            "UA_AGENT_TOKENS and restart the hub; the database revoke does not stop an env "
            "bearer token"
        )
    if problems:
        return _check(
            capability,
            "MISCONFIGURED",
            f"{len(retired)} retired agent(s) with live credentials: " + "; ".join(problems),
            gate,
        )
    detail = (
        f"{len(retired)} retired agent(s), 0 live credentials" if retired else "no retired agents"
    )
    return _check(capability, "LIVE", detail[:500], gate)


def _mcp_oauth_check() -> dict:
    """Remote MCP OAuth 2.1 authorization server. Opt-in; partial config is MISCONFIGURED."""
    flag, determined = _hub_setting("UA_MCP_OAUTH")
    if not determined:
        return _check(
            "mcp_oauth",
            "MISCONFIGURED",
            "cannot determine the hub's OAuth connector configuration",
            "UA_MCP_OAUTH",
        )
    if (flag or "0") != "1":
        return _check(
            "mcp_oauth",
            "DORMANT",
            "MCP OAuth 2.1 AS off — set UA_MCP_OAUTH=1 plus issuer and an approver",
            "UA_MCP_OAUTH",
        )
    issuer, _ = _hub_setting("UA_MCP_OAUTH_ISSUER")
    passphrase, _ = _hub_setting("UA_MCP_OAUTH_PASSPHRASE")
    fallback, _ = _hub_setting("UA_MCP_OAUTH_PASSPHRASE_FALLBACK")
    logins, _ = _hub_setting("UA_MCP_OAUTH_APPROVER_LOGINS")
    clients, _ = _hub_setting("UA_MCP_OAUTH_CLIENTS")
    agent, _ = _hub_setting("UA_MCP_OAUTH_AGENT")
    agent = (agent or config.mcp_oauth_agent()).strip()
    # Approvers (task #1322): verified humans first, the passphrase only as a
    # flagged fallback.
    approvers = []
    # Doctor can run in an operator shell while the hub's credentials live
    # only in its service unit; `_workos_leg_status` reads that same unit. It
    # checks the settings' shape only, so the approver is named as configured,
    # not as working (task #2114).
    workos = _workos_leg_status()
    if workos is not None and not workos["problem"]:
        approvers.append("workos (configured, not verified live)")
    if (logins or "").strip():
        approvers.append("tailnet+passkey")
    passphrase_ok = bool(passphrase) and (fallback or "0").strip() == "1"
    if passphrase_ok:
        approvers.append("passphrase (fallback, not human-verified)")
    missing = []
    if not (issuer or "").strip():
        missing.append("UA_MCP_OAUTH_ISSUER")
    if not approvers and not passphrase:
        missing.append("UA_MCP_OAUTH_APPROVER_LOGINS (or WorkOS leg, or passphrase fallback)")
    if not config.is_known_agent_label(agent):
        missing.append("UA_MCP_OAUTH_AGENT")
    if missing:
        return _check(
            "mcp_oauth",
            "MISCONFIGURED",
            "UA_MCP_OAUTH=1 but missing " + ", ".join(missing),
            "UA_MCP_OAUTH / UA_MCP_OAUTH_ISSUER / UA_MCP_OAUTH_APPROVER_LOGINS",
        )
    if not approvers:
        return _check(
            "mcp_oauth",
            "DEGRADED",
            "OAuth 2.1 AS up, existing tokens work, but no approver can sign anything new in: "
            "the passphrase needs UA_MCP_OAUTH_PASSPHRASE_FALLBACK=1, or configure "
            "UA_MCP_OAUTH_APPROVER_LOGINS / the WorkOS hub leg",
            "UA_MCP_OAUTH_APPROVER_LOGINS / UA_MCP_OAUTH_PASSPHRASE_FALLBACK",
        )
    cc = config.mcp_oauth_client_credentials()
    cc_note = f"; client_credentials clients={len(cc)}" if cc else "; client_credentials=off"
    return _check(
        "mcp_oauth",
        "LIVE",
        f"OAuth 2.1 AS active for {agent} at {(issuer or '').rstrip('/')}{cc_note}"
        f"; {len(config.mcp_oauth_clients(clients or ''))} pinned client credential(s)"
        f"; approvers: {', '.join(approvers)}",
        "UA_MCP_OAUTH / UA_MCP_OAUTH_ISSUER",
    )


def _oauth_store_check() -> dict:
    """Durable OAuth store (task #1321, J2): persisted clients / sessions / devices."""
    flag, determined = _hub_setting("UA_MCP_OAUTH")
    if determined and (flag or "0") != "1":
        return _check(
            "oauth_store",
            "DORMANT",
            "OAuth AS off — no durable sign-in sessions to hold",
            "UA_MCP_OAUTH",
        )
    try:
        from .. import oauth_store

        counts = oauth_store.counts()
    except Exception as exc:  # noqa: BLE001 - reported in-band
        return _check(
            "oauth_store",
            "UNKNOWN",
            f"cannot read the hub OAuth store from this process: {str(exc)[:160]}",
            "hub DB (oauth_clients / oauth_refresh_tokens)",
        )
    summary = (
        f"{counts['clients']} registered client(s), {counts['refresh_tokens']} live "
        f"session(s) across {counts['devices']} bound device(s)"
    )
    failures = oauth_store.write_failure_status()
    if failures["count"]:
        # Sign-ins issued since the failure exist only in this process's memory
        # and are lost on restart; the row must not read LIVE over that.
        return _check(
            "oauth_store",
            "DEGRADED",
            f"{summary}; {failures['count']} durable write failure(s) since start "
            f"(last: {failures['last_kind']}) — those sign-ins will not survive a restart",
            "hub DB (oauth_clients / oauth_refresh_tokens)",
        )
    return _check(
        "oauth_store",
        "LIVE",
        f"{summary}; survives hub restart",
        "hub DB (oauth_clients / oauth_refresh_tokens)",
    )


def _approval_gates_check() -> dict:
    raw, determined = _hub_posture_setting("UA_APPROVAL_TOOLS")
    if not determined:
        return _check(
            "approval_gates",
            "MISCONFIGURED",
            "cannot determine the hub's approval configuration (unit not installed or unreadable)",
            "UA_APPROVAL_TOOLS",
        )
    baseline_raw, _ = _hub_posture_setting("UA_APPROVAL_BASELINE")
    legacy_raw, _ = _hub_posture_setting("UA_LEGACY_OPEN_POSTURE")
    gated = config.effective_approval_tools(raw, baseline_raw, legacy_raw)
    if not gated:
        return _check(
            "approval_gates",
            "DORMANT",
            "no tools gated — the approval queue never populates",
            "UA_APPROVAL_TOOLS",
        )
    return _check(
        "approval_gates", "LIVE", f"gating: {', '.join(sorted(gated))}", "UA_APPROVAL_TOOLS"
    )


def _hosted_process() -> bool:
    """True when this process is the hosted hub, matching the container entrypoint.

    Hosted has no systemd unit. ``UA_HOSTED=1`` or a non-empty
    ``RAILWAY_ENVIRONMENT_ID`` means the process environment is the push
    configuration. A workstation doctor still reads the hub unit.
    """
    if os.environ.get("UA_HOSTED", "").strip() == "1":
        return True
    return bool(os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip())


def _approval_push_check() -> dict:
    """Phone push for waiting approvals (task #1988). Shows the host, never the URL."""
    gate = "UA_APPROVAL_PUSH_URL / UA_APPROVAL_PUSH_TOKEN"
    if _hosted_process():
        # Platform variables are the hub config. An unreadable unit is normal
        # here and must not report UNKNOWN.
        raw, determined = os.environ.get("UA_APPROVAL_PUSH_URL"), True
        token, token_known = os.environ.get("UA_APPROVAL_PUSH_TOKEN"), True
    else:
        raw, determined = _hub_posture_setting("UA_APPROVAL_PUSH_URL")
        token, token_known = _hub_posture_setting("UA_APPROVAL_PUSH_TOKEN")
    if not determined:
        return _check(
            "approval_push", "UNKNOWN", "cannot read the hub unit's push configuration", gate
        )
    if not (raw or "").strip():
        if _hosted_process():
            detail = (
                "no push URL — set UA_APPROVAL_PUSH_URL to an https topic on the "
                "hosted service; set UA_APPROVAL_PUSH_TOKEN when that topic requires a bearer"
            )
        else:
            detail = "no push URL — approvals alert nobody"
        return _check("approval_push", "DORMANT", detail, gate)
    host = approval_push.push_host(raw)
    if host is None:
        return _check(
            "approval_push", "MISCONFIGURED", "UA_APPROVAL_PUSH_URL is not an https URL", gate
        )
    if not token_known:
        auth = "(token presence unknown)"
    else:
        auth = "with a token" if (token or "").strip() else "without a token"
    return _check("approval_push", "LIVE", f"configured: pushes to {host} {auth}", gate)


def _approval_slack_cards_check() -> dict:
    """Slack approval cards (task #2303). Names the channel ID, never the token."""
    from .. import approval_slack

    gate = f"{approval_slack.CHANNEL_ENV} / SLACK_BOT_TOKEN / UA_SLACK_CHANNEL_IDS"
    names = (
        approval_slack.CHANNEL_ENV,
        "UA_SLACK_ENABLED",
        "SLACK_BOT_TOKEN",
        "UA_SLACK_CHANNEL_IDS",
        "UA_SLACK_CHANNEL_MAP",
    )
    values: dict[str, str | None] = {}
    for name in names:
        if _hosted_process():
            value, known = os.environ.get(name), True
        else:
            value, known = _hub_posture_setting(name)
        if not known:
            return _check(
                "approval_slack_cards",
                "UNKNOWN",
                "cannot read the hub unit's Slack card configuration",
                gate,
            )
        values[name] = value
    channel = (values[approval_slack.CHANNEL_ENV] or "").strip()
    if not channel:
        return _check(
            "approval_slack_cards",
            "DORMANT",
            f"no card channel — set {approval_slack.CHANNEL_ENV} to an allowlisted Slack "
            "channel ID to post approval cards",
            gate,
        )
    problem = approval_slack.channel_problem_from_settings(
        channel,
        enabled_raw=values["UA_SLACK_ENABLED"],
        bot_token=values["SLACK_BOT_TOKEN"],
        channel_ids_csv=values["UA_SLACK_CHANNEL_IDS"],
        channel_map=values["UA_SLACK_CHANNEL_MAP"],
    )
    if problem is not None:
        return _check("approval_slack_cards", "MISCONFIGURED", problem, gate)
    try:
        dead = approval_slack.dead_letters_since()
        stuck = approval_slack.stuck_intents()
        last = approval_slack.last_delivery(channel)
    except Exception as exc:  # noqa: BLE001 - a doctor row must not raise
        return _check(
            "approval_slack_cards",
            "UNKNOWN",
            f"cannot read approval card delivery records ({type(exc).__name__})",
            gate,
        )
    window = approval_slack.DEAD_LETTER_WINDOW_H
    if dead or stuck:
        parts = []
        if dead:
            parts.append(f"{dead} delivery(ies) dead-lettered in the last {window}h")
        if stuck:
            parts.append(f"{stuck} delivery(ies) not drained")
        return _check(
            "approval_slack_cards",
            "DEGRADED",
            "; ".join(parts) + " — see approval.delivery events",
            gate,
        )
    if last is None:
        return _check(
            "approval_slack_cards",
            "UNKNOWN",
            f"configured for channel {channel}, but no card has been delivered there yet — "
            "request one approval to confirm the path",
            gate,
        )
    return _check(
        "approval_slack_cards",
        "LIVE",
        f"cards post to channel {channel}; last delivery {last}",
        gate,
    )


def _search_provider_check() -> dict:
    try:
        provider = web._resolve_provider()
    except Exception as e:
        return _check(
            "search_provider",
            "MISCONFIGURED",
            f"provider resolution failed: {e}",
            "UA_SEARCH_PROVIDER",
        )
    if provider == "ddg":
        detail = "resolves to keyless 'ddg' — set BRAVE_API_KEY or TAVILY_API_KEY to upgrade"
    else:
        detail = f"resolves to keyed provider {provider!r}"
    return _check(
        "search_provider", "LIVE", detail, "UA_SEARCH_PROVIDER / BRAVE_API_KEY / TAVILY_API_KEY"
    )


def _per_agent_workspaces_check() -> dict:
    workspaces = config.agent_workspaces()
    if not workspaces:
        return _check(
            "per_agent_workspaces",
            "DORMANT",
            "no bindings — hub callers share the process-wide UA_WORKSPACE",
            "UA_AGENT_WORKSPACES",
        )
    return _check(
        "per_agent_workspaces",
        "LIVE",
        f"bound: {', '.join(sorted(workspaces))}",
        "UA_AGENT_WORKSPACES",
    )


def _workspace_pin_check() -> dict:
    """Whether service.env pins the hub sandbox root (#1806, #1825).

    After ``unified-agent init`` the uncommented ``UA_WORKSPACE`` line in
    ``$UA_HOME/service.env`` is authoritative and ``settings.toml``
    ``paths.workspace`` is informational. Only that one key is read from the
    file; nothing else in it is reported.
    """
    from .. import service_env, settings

    gate = "UA_WORKSPACE in $UA_HOME/service.env (settings.toml paths.workspace is informational)"
    env_path = config.data_home() / "service.env"
    try:
        if not env_path.is_file():
            return _check(
                "workspace_pin",
                "DORMANT",
                "no service.env (dev checkout or before `unified-agent init`): UA_WORKSPACE "
                "comes from the environment, then settings.toml paths.workspace",
                gate,
            )
        raw = (service_env.parse_env_file(env_path).get("UA_WORKSPACE") or "").strip()
        if not raw:
            return _check(
                "workspace_pin",
                "MISCONFIGURED",
                "service.env has no uncommented UA_WORKSPACE pin, so settings.toml "
                "paths.workspace can move the sandbox root on restart; run "
                "`unified-agent init` to pin the effective value",
                gate,
            )
        try:
            # The same rule init applies, so a pin init refuses never reads LIVE.
            pinned = service_env.resolve_workspace_pin(raw)
        except ValueError:
            return _check(
                "workspace_pin",
                "MISCONFIGURED",
                "service.env UA_WORKSPACE references a variable that is not set here "
                "(or a command substitution), so `unified-agent init` refuses it; pin "
                "a literal path with `unified-agent init --workspace PATH`",
                gate,
            )
        try:
            data = settings.load(settings.settings_path())
        except settings.SettingsError:
            data = {}
        configured = str((data.get("paths") or {}).get("workspace") or "").strip()
        # settings.toml values are never shell-expanded at runtime; only ~ is.
        if configured and Path(configured).expanduser() != pinned:
            return _check(
                "workspace_pin",
                "DEGRADED",
                f"service.env pins UA_WORKSPACE={pinned}; settings.toml paths.workspace "
                f"names {configured}, which is ignored but misleading: align it or run "
                "`unified-agent init --workspace PATH` to move the workspace",
                gate,
            )
        return _check("workspace_pin", "LIVE", f"service.env pins UA_WORKSPACE={pinned}", gate)
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check("workspace_pin", "UNKNOWN", f"cannot evaluate: {type(exc).__name__}", gate)


def _langgraph_engine_check() -> dict:
    try:
        import importlib.metadata

        import langgraph  # noqa: F401
        import langgraph.checkpoint.sqlite  # noqa: F401

        from ..langgraph_pilot import default_checkpoint_path

        version = importlib.metadata.version("langgraph")
        ckpt_path = default_checkpoint_path()
        return _check(
            "langgraph_engine",
            "LIVE",
            f"langgraph v{version} available, checkpoints at {ckpt_path}",
            "langgraph / checkpoints.sqlite3",
        )
    except ImportError as e:
        return _check(
            "langgraph_engine",
            "MISCONFIGURED",
            f"langgraph import failed: {e} — run `uv sync`",
            "uv sync",
        )
    except Exception as e:
        return _check(
            "langgraph_engine",
            "MISCONFIGURED",
            f"langgraph check failed: {e}",
            "langgraph",
        )


def _mcp2_runtime_telemetry_check() -> dict:
    from .. import mcp_runtime_telemetry

    status_enum, evidence = mcp_runtime_telemetry.evaluate_health()
    snapshot = mcp_runtime_telemetry.get_telemetry_snapshot()
    proto = snapshot.get("negotiated_protocol_version")
    gate = "MCP 2.x protocol negotiation"

    if status_enum == mcp_runtime_telemetry.McpHealthStatus.HEALTHY:
        if proto is None:
            return _check(
                "mcp2_runtime_telemetry",
                "DORMANT",
                "no active MCP 2.x client session negotiated yet",
                gate,
            )
        client = (snapshot.get("client") or {}).get("name") or "client"
        return _check(
            "mcp2_runtime_telemetry",
            "LIVE",
            f"MCP 2.x healthy; protocol {proto} negotiated with {client}",
            gate,
        )
    if status_enum == mcp_runtime_telemetry.McpHealthStatus.DEGRADED:
        detail = "; ".join(evidence) if evidence else "MCP 2.x runtime degraded"
        return _check(
            "mcp2_runtime_telemetry",
            "MISCONFIGURED",
            f"degraded: {detail}",
            gate,
        )
    if status_enum == mcp_runtime_telemetry.McpHealthStatus.INCOMPATIBLE:
        detail = "; ".join(evidence) if evidence else "MCP 2.x version skew or capability mismatch"
        return _check(
            "mcp2_runtime_telemetry",
            "MISCONFIGURED",
            f"incompatible: {detail}",
            gate,
        )
    return _check(
        "mcp2_runtime_telemetry",
        "UNKNOWN",
        "MCP 2.x telemetry in unknown state",
        gate,
    )


def _hub_posture_setting(name: str) -> tuple[str | None, bool]:
    """A posture flag from the hub unit, without borrowing the caller's value.

    ``_hub_setting`` falls back to this process whenever the unit omits a key.
    That is right for settings the unit and the caller share on purpose. It is
    wrong for the new defaults: an operator shell with ``UA_LEGACY_OPEN_POSTURE=1``
    or ``UA_APPROVAL_BASELINE=0`` would make a hub that left those unset look
    open. A readable unit that does not set the key is unset. The caller
    environment is used only when the unit cannot be read at all.
    """
    env = service_probes._service_env(_SERVICE_UNITS["hub_service"])
    if env is None:
        if name in os.environ:
            return os.environ[name], True
        return None, False
    if name in env:
        return env[name], True
    # A unit that sets other keys and omits this one chose the default.
    # An empty environment (no Environment= and no EnvironmentFile entries)
    # still falls back to the caller: that is the hermetic "unit sets nothing"
    # probe, and a hub with no settings at all has nothing else to report.
    if env:
        return None, True
    if name in os.environ:
        return os.environ[name], True
    return None, True


# Cold MCP imports can exceed five seconds on a loaded host. Three sequential
# cross-home credential probes still spend at most 45 seconds waiting on children.
_HUB_TOKEN_PROBE_TIMEOUT_S = 15.0


def _hub_service_tokens(
    raw: str | None = None, *, probe: str = "agent_tokens"
) -> dict[str, str] | list[dict]:
    """Resolve the hub's effective agent tokens without mutating caller environment.

    When the hub unit specifies its own UA_HOME differing from the ambient process,
    probes managed tokens in an isolated child process to prevent contaminating
    concurrent storage.connect calls. ``probe`` names the ``config`` reader:
    ``agent_tokens`` (the default) or ``agent_token_collisions`` (task #1657).
    """
    hub_env = service_probes._service_env(_SERVICE_UNITS["hub_service"])
    tokens_raw, _ = _hub_posture_setting("UA_AGENT_TOKENS")
    effective_raw = (
        tokens_raw
        if tokens_raw is not None
        else (raw or ("" if (hub_env is not None and hub_env) else None))
    )

    if hub_env and "UA_HOME" in hub_env:
        try:
            hub_home = Path(hub_env["UA_HOME"]).expanduser().resolve()
            current_home = config.data_home().resolve()
        except Exception:
            hub_home = Path(hub_env["UA_HOME"]).expanduser()
            current_home = config.data_home()

        if hub_home != current_home:
            env = dict(os.environ)
            src_dir = str(Path(__file__).resolve().parent.parent.parent)
            current_pp = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{src_dir}:{current_pp}" if current_pp else src_dir
            env["UA_HOME"] = str(hub_home)
            # The effective raw configuration travels only over the child's
            # stdin pipe, never through argv or a duplicate environment value.
            env.pop("UA_AGENT_TOKENS", None)
            for k in (
                "UA_MANAGED_SECRET_KEYS",
                "UA_MANAGED_SECRETS",
                "UA_EDITION",
                "WORKOS_ORGANIZATION_ID",
                "UA_HUB_WORKOS_ORGANIZATION_ID",
            ):
                if k in hub_env:
                    env[k] = hub_env[k]
                else:
                    env.pop(k, None)
            cmd = [
                sys.executable,
                "-c",
                "import json, sys; from unified_agent import config; "
                "raw = json.load(sys.stdin); "
                "print(json.dumps(getattr(config, sys.argv[1])(raw, include_managed=True)))",
                probe,
            ]
            try:
                res = subprocess.run(
                    cmd,
                    env=env,
                    input=json.dumps(effective_raw),
                    capture_output=True,
                    text=True,
                    timeout=_HUB_TOKEN_PROBE_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                # Child output and other exception messages may echo
                # credentials, including a failed pipe write. Only fixed
                # diagnostic categories belong in the API-visible doctor detail.
                raise RuntimeError("hub token probe timed out (TimeoutExpired)") from None
            except Exception as exc:
                raise RuntimeError(f"hub token probe failed ({type(exc).__name__})") from None
            if res.returncode != 0:
                raise RuntimeError(f"hub token probe exited with status {res.returncode}")
            try:
                return json.loads(res.stdout.strip())
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    f"hub token probe returned invalid JSON ({type(exc).__name__})"
                ) from None

    return getattr(config, probe)(effective_raw, include_managed=True)


def _hub_setting(name: str) -> tuple[str | None, bool]:
    """(value, determined) for a hub setting, preferring the hub unit's own env.

    `determined` is False when neither the unit nor this process can tell us —
    the caller must then report "cannot determine", never "not configured".
    """
    env = service_probes._service_env(_SERVICE_UNITS["hub_service"])
    if env is not None and name in env:
        return env[name], True
    if name in os.environ:
        return os.environ[name], True
    if env is not None:
        return None, True  # the unit is readable and simply does not set it
    return None, False


def _unit_check(capability: str, unit: str) -> dict:
    """``service_probes._unit_check`` with this report's designed timer exit codes."""
    return service_probes._unit_check(
        capability, unit, _TIMER_DESIGNED_EXIT_CODES, _TIMER_DESIGNED_EXIT_EVIDENCE
    )


def _retired_unit_check(capability: str, unit: str) -> dict:
    """Report a retired unit honestly: DORMANT when gone, MISCONFIGURED if still loaded."""
    task, note = _RETIRED_UNITS[capability]
    display = service_probes._unit_display(unit)
    if capability == "dispatch_tick_timer":
        # Share Studio's Darwin disabled-override policy without a second probe.
        from ..dispatch.routing import scheduler_runtime_state

        state = scheduler_runtime_state()["status"]
        loaded = state.startswith("misconfigured_")
        absent = state == "retired"
        seen = state.removeprefix("misconfigured_") if loaded else state
    else:
        props = service_probes._service_probe(unit, SERVICE_CHECK_TIMEOUT_S)
        load_state = props.get("LoadState") if props is not None else None
        loaded = load_state == "loaded"
        absent = load_state == "not-found"
        seen = props.get("ActiveState") if loaded else load_state
        label = launchd_label_for_unit(unit) if sys.platform == "darwin" else None
        if absent and label:
            # An absent LaunchAgent with an installed plist loads again at the
            # next login unless launchd holds a disabled override for it, so
            # absence alone does not prove retirement on macOS.
            from ..monitor import _launchd_disabled_override

            if _launchd_disabled_override(label, SERVICE_CHECK_TIMEOUT_S) is not True:
                absent = False
                seen = "absent without a launchd disabled override"
    if loaded:
        return _check(
            capability,
            "MISCONFIGURED",
            f"{display}: retired by {task} but still loaded "
            f"({seen}) — boot it out and disable it; {note}",
            unit,
        )
    if absent:
        return _check(
            capability,
            "DORMANT",
            f"{display}: retired by {task} (no loaded job found); {note}",
            unit,
        )
    return _check(
        capability,
        "UNKNOWN",
        f"{display}: retired by {task}; probe unavailable or retirement unconfirmed "
        f"({seen}); {note}",
        unit,
    )


def _repo_deploy_dir() -> Path | None:
    """deploy/ dir of the source checkout this module runs from, or None.

    Walks up from this file looking for deploy/unified-agent.service — present
    for the editable install the deploy scripts set up; a non-editable install
    has no templates to compare against.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "deploy"
        if (candidate / "unified-agent.service").is_file():
            return candidate
    return None


def _installed_unit_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user"


def _parse_unit(text: str) -> dict[str, list[tuple[str, str]]]:
    """Unit file → {section: [(key, value), ...]}; comments and blanks dropped."""
    sections: dict[str, list[tuple[str, str]]] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if sep:
            sections.setdefault(section, []).append((key.strip(), value.strip()))
    return sections


def _unit_drift(template_text: str, installed_text: str) -> list[str]:
    """Directives present in the repo template but absent from the installed unit.

    Environment= entries compare by variable name only: installed units
    legitimately carry local values (a real UA_HTTP_ALLOWED_HOSTS, a moved
    vault path) that must never read as drift. Every other directive must
    appear verbatim. Directives only in the installed unit are local
    tailoring, not drift.
    """
    tmpl, inst = _parse_unit(template_text), _parse_unit(installed_text)
    missing: list[str] = []
    for section, entries in tmpl.items():
        inst_entries = inst.get(section, [])
        inst_exact = set(inst_entries)
        inst_env_names = {v.split("=", 1)[0] for k, v in inst_entries if k == "Environment"}
        for key, value in entries:
            if key == "Environment":
                name = value.split("=", 1)[0]
                if name not in inst_env_names:
                    missing.append(f"[{section}] Environment={name}=…")
            elif (key, value) not in inst_exact:
                missing.append(f"[{section}] {key}={value}")
    return missing


def _unit_drift_check() -> dict:
    """Compare every installed repo-templated unit against its template.

    Motivated by the 2026-07-21 incident: the installed unified-agent.service
    had silently drifted from deploy/unified-agent.service — two Exec hooks
    shipped in the repo were never live because install is a manual `cp`.
    """
    gate = "deploy/ unit templates vs ~/.config/systemd/user/"
    deploy_dir = _repo_deploy_dir()
    if deploy_dir is None:
        return _check(
            "unit_template_drift",
            "DORMANT",
            "repo deploy/ templates not reachable from this install — drift not checkable",
            gate,
        )
    templates = [deploy_dir / "unified-agent.service"]
    templates += sorted((deploy_dir / "systemd").glob("*.service"))
    templates += sorted((deploy_dir / "systemd").glob("*.timer"))
    installed_dir = _installed_unit_dir()
    compared = 0
    drifted: list[str] = []
    for template_path in templates:
        installed_path = installed_dir / template_path.name
        if not installed_path.is_file():
            # Not installed on this host — the per-unit checks already say so.
            continue
        try:
            missing = _unit_drift(template_path.read_text(), installed_path.read_text())
        except OSError as e:
            drifted.append(f"{template_path.name}: unreadable ({e})")
            continue
        compared += 1
        if missing:
            drifted.append(f"{template_path.name}: missing {'; '.join(missing)}")
    if drifted:
        return _check("unit_template_drift", "MISCONFIGURED", " | ".join(drifted), gate)
    if not compared:
        return _check(
            "unit_template_drift",
            "DORMANT",
            "no repo-templated units installed on this host",
            gate,
        )
    return _check(
        "unit_template_drift",
        "LIVE",
        f"{compared} installed unit(s) match their repo templates",
        gate,
    )


def _extra_check(capability: str, module: str, install_hint: str) -> dict:
    try:
        importlib.import_module(module)
    except ImportError:
        return _check(
            capability, "DORMANT", f"{module} not installed — run `{install_hint}`", install_hint
        )
    return _check(capability, "LIVE", f"{module} import OK", install_hint)


def _workflow_definitions_check() -> dict:
    from .workflow import _load_definition, _workflows_dir

    wdir = _workflows_dir()
    if not wdir.is_dir():
        return {
            "capability": "workflow_definitions",
            "status": "DORMANT",
            "detail": f"{wdir} directory missing",
            "gate": "workflows/*.yaml",
        }
    yamls = list(wdir.glob("*.yaml"))
    if not yamls:
        return {
            "capability": "workflow_definitions",
            "status": "DORMANT",
            "detail": "no workflows defined",
            "gate": "workflows/*.yaml",
        }
    failed = []
    for path in yamls:
        try:
            _load_definition(path.stem)
        except Exception as e:
            failed.append(f"{path.stem}: {e}")
    if failed:
        return {
            "capability": "workflow_definitions",
            "status": "MISCONFIGURED",
            "detail": f"{len(failed)} workflow(s) invalid: " + "; ".join(failed),
            "gate": "workflows/*.yaml",
        }
    return {
        "capability": "workflow_definitions",
        "status": "LIVE",
        "detail": "all workflows loaded",
        "gate": "workflows/*.yaml",
    }


def _tui_dashboard_check() -> dict:
    """Is the Phase 4.3 TUI actually launchable, or just declared?

    `unified-agent-tui` is an entry point, so a missing/broken textual install
    only surfaces when someone runs it. Import the module the entry point names
    and report the gate instead.
    """
    gate = "unified-agent-tui entry point"
    try:
        import textual

        from .. import tui
    except ImportError as e:
        return _check(
            "tui_dashboard",
            "DORMANT",
            f"TUI dependencies not importable: {e}",
            gate,
        )
    if not callable(getattr(tui, "main", None)):
        return _check(
            "tui_dashboard",
            "MISCONFIGURED",
            "unified_agent.tui has no callable main(); entry point would fail",
            gate,
        )
    return _check(
        "tui_dashboard",
        "LIVE",
        f"textual {textual.__version__}, unified_agent.tui:main importable",
        gate,
    )


def _web_ui_check() -> dict:
    """Is the web control center actually servable, or only declared?

    The failure this exists to catch is deploy/packaging: `packages/web/dist`
    is a build artifact shipped separately from the Python package, so a hub
    that never received it serves a hard 503 on /ui while dev works perfectly.
    Same class of "shipped broken, caught only by
    live verification" as #104/#110, so it gets a runtime canary rather than trust.
    """
    gate = "GET /ui on the HTTP hub"
    try:
        from .. import webui
        from ..webui import spa as web_spa
    except ImportError as e:
        return _check("web_ui", "MISCONFIGURED", f"webui package not importable: {e}", gate)

    try:
        routes = webui.web_routes()
    except Exception as e:  # noqa: BLE001 - a broken route table is the finding
        return _check("web_ui", "MISCONFIGURED", f"web_routes() raised: {e}", gate)

    if config.transport() != "http":
        return _check(
            "web_ui",
            "DORMANT",
            f"{len(routes)} routes ready; hub is on stdio transport so nothing is served",
            gate,
        )

    if not web_spa.modern_dist_ready():
        check = _check(
            "web_ui",
            "MISCONFIGURED",
            "packages/web/dist/index.html missing — run `npm --prefix packages/web run build`",
            gate,
        )
        if not os.environ.get("UA_WEB_DIST_DIR"):
            # No override: the package or checkout simply has no UI build.
            # `doctor --allow-unbuilt-ui` (the installer) tolerates only this.
            check["reason"] = UI_NOT_BUILT
        return check

    return _check(
        "web_ui",
        "LIVE",
        f"{len(routes)} routes mounted, SPA dist present",
        gate,
    )


def _node_turn_index_check() -> dict:
    """Report optional turn-count acceleration separately from event durability."""
    gate = "SQLite >= 3.42 with JSON5 support / storage.init_db()"
    try:
        conn = storage.connect()
        try:
            version = storage.sqlite_runtime_version(conn)
            supported = storage.node_turn_index_supported(conn)
            present = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='idx_event_log_node_item_turn'"
                ).fetchone()
                is not None
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - a doctor probe never raises
        return _check(
            "node_turn_index", "UNKNOWN", f"SQLite probe failed: {type(exc).__name__}", gate
        )
    if not supported:
        return _check(
            "node_turn_index",
            "DEGRADED" if present else "DORMANT",
            f"SQLite {version} lacks JSON5 support; "
            + (
                "restart the hub to remove the incompatible optional index"
                if present
                else "optional turn index disabled; event payloads remain unchanged"
            ),
            gate,
        )
    return _check(
        "node_turn_index",
        "LIVE" if present else "DEGRADED",
        f"SQLite {version}; "
        + (
            "turn-count expression index present"
            if present
            else "turn-count expression index missing; restart the hub to rebuild it"
        ),
        gate,
    )


def _event_bus_check() -> dict:
    """Is the event bus socket actually accepting connections?

    #157 shipped the bus and nothing ever ran it: `publish()` has been silently
    returning False for every event since. The durable event_log hid it — data kept
    accruing, so nothing looked broken. This check is the difference between "the bus
    exists" and "the bus works".
    """
    import socket as _socket

    gate = "UA_EVENT_BUS / UA_EVENT_BUS_EMBED"
    if not config.event_bus_enabled():
        return _check("event_bus", "DORMANT", "disabled by UA_EVENT_BUS=0", gate)

    path = config.event_socket_path()
    if not path.exists():
        detail = (
            "socket absent; hub not running or not embedding the bus"
            if config.event_bus_embed()
            else "socket absent and UA_EVENT_BUS_EMBED=0 (run unified-agent-eventbus)"
        )
        return _check("event_bus", "DORMANT", detail, gate)

    if not path.is_socket():
        return _check("event_bus", "MISCONFIGURED", f"{path} exists but is not a socket", gate)

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(str(path))
    except OSError as e:
        return _check(
            "event_bus",
            "MISCONFIGURED",
            f"stale socket at {path}: connect failed ({e})",
            gate,
        )
    finally:
        sock.close()
    return _check("event_bus", "LIVE", f"accepting connections at {path}", gate)


def _lifecycle_capture_check() -> dict:
    gate = "UA_LIFECYCLE_CAPTURE"
    if not config.lifecycle_capture_enabled():
        return _check(
            "lifecycle_capture",
            "DORMANT",
            "optional capture off (default) — session_packet still derives from existing stores",
            gate,
        )
    excluded_kinds = ",".join(sorted(config.lifecycle_exclude_kinds())) or "(none)"
    excluded_tools = ",".join(sorted(config.lifecycle_exclude_tools())) or "(none)"
    return _check(
        "lifecycle_capture",
        "LIVE",
        f"recording sanitized observations; exclude kinds={excluded_kinds} tools={excluded_tools}",
        gate,
    )


def _agent_wake_check() -> dict:
    """Idle-session wakes (tasks #1873, #2050): requests, grants, and the breaker.

    DORMANT until an open session that requested wakes (``wake: true``, launched
    with ``UA_WAKE=1``) also holds the operator's grant; the detail names the
    sessions still waiting on one. LIVE once a granted session has been woken;
    until then DEGRADED, since only TUIOS panes can be woken (supported Herdr
    cannot atomically attest empty input). Also DEGRADED while any granted session is paused
    after unanswered attempted wakes (including uncertain submissions): it is ignoring the doorbell, and stays paused until
    it reads mail. A genuinely new hub session starts with fresh state.
    """
    gate = (
        "UA_WAKE=1 per session, an operator grant per session (Control Center), "
        "UA_NODE_WAKE=1 on the ua-node"
    )
    try:
        from ..messaging import wake

        conn = storage.connect()
        try:
            counts = wake.doctor_summary(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return _check("agent_wake", "UNKNOWN", f"wake state unreadable: {exc}"[:300], gate)
    if not counts["opted_in"]:
        return _check(
            "agent_wake", "DORMANT", "no open session requested wakes (launch with UA_WAKE=1)", gate
        )
    awaiting = ""
    if counts["awaiting"]:
        more = counts["awaiting"] - len(counts["awaiting_ids"])
        awaiting = (
            f"; {counts['awaiting']} awaiting an operator grant: "
            + ", ".join(counts["awaiting_ids"])
            + (f" (+{more} more)" if more else "")
        )
    detail = (
        f"{counts['granted']} of {counts['opted_in']} requesting session(s) granted, "
        f"{counts['woken']} woken at least once{awaiting}"
    )
    if not counts["granted"]:
        return _check("agent_wake", "DORMANT", detail, gate)
    detail += (
        "; TUIOS panes wake through its draft-safe queue; Herdr panes stay hook-only: "
        "supported Herdr 0.9.3/0.9.4 has no atomic empty-input guard "
        "(input_state_unavailable)"
    )
    if counts["paused"]:
        return _check(
            "agent_wake",
            "DEGRADED",
            f"{detail}; {counts['paused']} paused after {wake.BREAKER_AFTER} wake attempts with no "
            "inbox read (breaker clears after an inbox read; a new hub session starts fresh)",
            gate,
        )
    return _check("agent_wake", "LIVE" if counts["woken"] else "DEGRADED", detail, gate)


def _agent_label_drift_check() -> dict:
    """Agents active recently that nobody added to UA_AGENT_LABELS.

    Not a security control — `record_agent_event` deliberately never validates labels,
    and 874 rows of history depend on that. This makes the drift *visible* instead of
    letting the registry quietly fill with labels no one declared.
    """
    gate = "UA_AGENT_LABELS vs observed activity"
    try:
        from .. import registry
    except ImportError as e:
        return _check("agent_label_drift", "MISCONFIGURED", f"registry unavailable: {e}", gate)

    if not config.agent_labels():
        return _check(
            "agent_label_drift",
            "LIVE",
            "UA_AGENT_LABELS unset — open roster; drift check skipped",
            gate,
        )

    try:
        unregistered = registry.unregistered_labels()
    except Exception as e:  # noqa: BLE001
        return _check("agent_label_drift", "MISCONFIGURED", f"query failed: {e}", gate)

    # Exempt the identities that are unregistered on purpose. Without this the
    # check was pinned DORMANT permanently by two of them — `unified-agent-cron`
    # (emitted by monitor.py on every scheduled run) and the configured operator label (a
    # deliberate decision, memory 92437723a6e8) — and a signal that is always on
    # carries no information: a genuinely unexpected third label would arrive as
    # a change to a string the reader has already learned to skip.
    expected = config.expected_unregistered_labels()
    exempt = sorted(label for label in unregistered if label in expected)
    unexpected = [label for label in unregistered if label not in expected]

    # Name the exemptions in both outcomes, so an exempted identity is visible
    # rather than silently swallowed by config.
    suffix = f" ({len(exempt)} expected and exempt: {', '.join(exempt)})" if exempt else ""

    if not unexpected:
        return _check(
            "agent_label_drift",
            "LIVE",
            f"every recently active label is configured or expected{suffix}",
            gate,
        )
    return _check(
        "agent_label_drift",
        "DORMANT",
        f"{len(unexpected)} unregistered label(s) active in the last 7d: "
        f"{', '.join(unexpected[:6])}{suffix}",
        gate,
    )


def _org_registry_check() -> dict:
    """Fail when chart identities disagree with the configured roster or offices."""
    gate = "UA_AGENT_LABELS and src/unified_agent/org.yaml"
    try:
        from .. import org_registry, registry

        offices = org_registry.load_registry()
        service_labels = set(org_registry.load_service_identities())
        configured = config.agent_labels()
        expected_unregistered = config.expected_unregistered_labels()
        # An unset roster is intentionally open. In that mode, chart members
        # and declared service identities accepted by the existing label
        # validator are known; observed labels with no office are still
        # reported below.
        member_roster = configured or {
            label
            for label in [
                *(member for office in offices for member in office["members"]),
                *service_labels,
            ]
            if config.is_known_agent_label(label)
        }
        # The human operator (login label and bare names) belongs in the chart
        # without being an agent label.
        humans = {config.operator_label(), *config.operator_aliases()}
        member_roster = member_roster | expected_unregistered | humans
        active = set(configured)
        active.update(
            agent["label"]
            for agent in registry.list_agents(include_configured=False)
            if agent["status"] in {"online", "idle", "stale"}
            and agent["label"] not in expected_unregistered
        )
        findings = org_registry.validate_registry(
            member_roster, offices, active_labels=active, service_labels=service_labels
        )
    except Exception as exc:  # noqa: BLE001 - doctor remains a complete report
        return _check("org_registry", "MISCONFIGURED", f"registry check failed: {exc}", gate)

    if findings:
        detail = "; ".join(
            f"{finding['identity']}: {finding['issue']}"
            + (f" ({finding['office']})" if finding.get("office") else "")
            for finding in findings
        )
        return _check("org_registry", "MISCONFIGURED", detail, gate)
    services_note = (
        f"; {len(service_labels)} declared service identit"
        f"{'y' if len(service_labels) == 1 else 'ies'} without an office"
        if service_labels
        else ""
    )
    return _check(
        "org_registry",
        "LIVE",
        f"{len(offices)} offices match the configured and active agent roster{services_note}",
        gate,
    )


def _stale_worktrees_check() -> dict:
    """Surface worktrees that outlived their work, wherever they actually live.

    `cleanup_stale_worktrees` only ever walks the workspace root, but Rule 2 of
    COLLABORATION.md puts worktrees in the development checkout. The result was
    that leftover worktrees in `~/dev/<repo>` were reported by nothing — found
    on 2026-08-18 only by looking by hand, six days after the PRs merged.

    Read-only, and deliberately so: this names them, and a human or the
    workspace-scoped cleanup tool removes them.
    """
    gate = "UA_AUDIT_ROOTS read surface + .worktrees/"
    try:
        from .worktree_policy import cached_survey_worktrees

        # Shared for UA_WORKTREE_SURVEY_CACHE_S: the survey runs git four or five
        # times per worktree and was the bulk of a doctor run's CPU (#1897).
        # A refresh=True run re-surveys once and replaces the shared survey, so
        # a worktree removed just before Refresh is gone from the report; the
        # host snapshot that follows reuses that new survey (Devin, #1044).
        found, age_s = cached_survey_worktrees(force=_forced_run.get())
    except Exception as e:  # noqa: BLE001
        return _check("stale_worktrees", "MISCONFIGURED", f"survey failed: {e}", gate)

    # An absolute time, not an age: the report itself may be served for minutes,
    # and an age would freeze at whatever it was when the run finished.
    aged = ""
    if age_s > 0:
        taken = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat(timespec="seconds")
        aged = f" (worktree survey taken {taken}, shared for UA_WORKTREE_SURVEY_CACHE_S)"
    if not found:
        return _check("stale_worktrees", "LIVE", f"no worktrees present{aged}", gate)

    stale = [w for w in found if w["stale"]]
    # Deploy checkouts and idle worktrees a running process uses are never
    # stale (task #2174); name them as kept so they are not mistaken for leftovers.
    kept = sum(1 for w in found if w.get("stale_reason") in ("deploy_checkout", "in_use"))
    kept_note = f"; {kept} kept (deploy checkout or in use)" if kept else ""
    # A done task's worktree a parked shell still uses is stale but never
    # removed (task #2178): report it, and keep it out of the removable count.
    stale_in_use = sum(1 for w in stale if w.get("in_use"))
    if stale_in_use:
        kept_note += f"; {stale_in_use} in use (kept until released)"
    # Without a process listing, cleanup keeps every worktree (task #2178).
    # That is safe but makes cleanup a no-op, so it is not reported as LIVE.
    unknown = [w for w in found if "in_use" in w and w["in_use"] is None]
    if unknown:
        names = ", ".join(Path(w["path"]).name for w in stale[:6])
        listed = f" ({len(stale)} stale: {names})" if stale else ""
        # The survey names the cause, including which process refused a read.
        note = unknown[0].get("liveness_note") or (
            "running processes could not be listed on this host, so cleanup removes nothing"
        )
        return _check(
            "stale_worktrees",
            "DEGRADED",
            f"{len(found)} worktree(s){listed}; {note}{aged}",
            gate,
        )
    if not stale:
        return _check(
            "stale_worktrees",
            "LIVE",
            f"{len(found)} worktree(s), none stale{kept_note}{aged}",
            gate,
        )

    outside = sum(1 for w in stale if not w["in_workspace"])
    where = f", {outside} outside the workspace (remove by hand)" if outside else ""
    disposable = sum(1 for w in stale if w["disposable"] and not w.get("in_use"))
    names = ", ".join(Path(w["path"]).name for w in stale[:6])
    return _check(
        "stale_worktrees",
        "DORMANT",
        f"{len(stale)} of {len(found)} worktree(s) are stale ({disposable} hold nothing "
        f"and can be removed){where}: {names}{kept_note}{aged}",
        gate,
    )


def _decision_ledger_check() -> dict:
    from .. import gateway

    status, detail = gateway.decision_write_health()
    return _check("gateway_decision_ledger", status, detail, gateway.CHANNEL_GATEWAY_DECISION)


def _database_backup_check() -> dict:
    """Make missing, stale, or corrupt complete backups visible without writing."""
    gate = "UA_BACKUP_DIR + scheduled backup runner"
    try:
        status = backup.backup_status()
    except OSError as exc:
        return _check(
            "database_backup", "MISCONFIGURED", f"cannot inspect backup directory: {exc}", gate
        )
    if not status["exists"]:
        return _check(
            "database_backup",
            "DORMANT",
            f"no complete database backup found in {status['directory']}",
            gate,
        )
    verification = status["verification"]
    if not verification["ok"]:
        return _check(
            "database_backup",
            "MISCONFIGURED",
            f"latest backup {status['path']} failed integrity check: {verification['detail']}",
            gate,
        )
    age_hours = status["age_seconds"] / 3600
    if age_hours > 26:
        return _check(
            "database_backup",
            "MISCONFIGURED",
            f"latest complete backup is {age_hours:.1f}h old: {status['path']}",
            gate,
        )
    return _check(
        "database_backup",
        "LIVE",
        f"latest complete backup is {age_hours:.1f}h old: {status['path']}",
        gate,
    )


_VAULT_SYNC_UNIT = "ua-vault-sync.timer"
# doctor's contract has no DEGRADED status; the mcp2_runtime_telemetry check
# already established the convention this follows — an internally-DEGRADED
# condition surfaces as MISCONFIGURED with a "degraded: ..." detail prefix.
_VAULT_SYNC_STALENESS_MULTIPLE = 3


def _vault_sync_check() -> dict:
    """Freshness + outstanding-conflict report for task #1288's hub -> desktop pull.

    LIVE requires both a recent successful sync (age < 3x the configured
    interval) and zero outstanding conflicts. Either one failing degrades to
    MISCONFIGURED with the counts in the detail, so an operator sees "how far
    behind" and "how many notes need a manual look" in the same line.

    A host that has never installed ua-vault-sync.timer and has no state file
    is DORMANT, not MISCONFIGURED — this must read quietly on the Mac hub
    itself, which does not run this timer (direction is hub -> desktop only).
    """
    from .. import settings, vault_sync

    gate = f"{_VAULT_SYNC_UNIT} + {vault_sync.__name__}"
    unit_props = service_probes._service_probe(_VAULT_SYNC_UNIT, SERVICE_CHECK_TIMEOUT_S)
    installed = bool(unit_props) and unit_props.get("LoadState") == "loaded"
    service_env = service_probes._service_env("ua-vault-sync.service")
    if installed and service_env is None:
        return _check(
            "vault_sync", "UNKNOWN", "cannot determine the sync service environment", gate
        )
    target_env = dict(service_env or {})
    if service_env is not None:
        settings.apply_to_environ(target_env, quiet=True)
    try:
        state = vault_sync.read_state(config.vault_sync_state_path(service_env))
    except vault_sync.VaultSyncStateError as exc:
        return _check("vault_sync", "MISCONFIGURED", str(exc), gate)

    if not installed and state is None:
        return _check(
            "vault_sync",
            "DORMANT",
            "not configured on this host (no ua-vault-sync.timer, no state file) — "
            "expected on hosts that do not pull the vault, e.g. the Mac hub itself",
            gate,
        )

    if not config.vault_sync_remote_host(target_env) or not config.vault_sync_remote_root(
        target_env
    ):
        return _check(
            "vault_sync",
            "MISCONFIGURED",
            "set UA_VAULT_SYNC_REMOTE_HOST and UA_VAULT_SYNC_REMOTE_ROOT in the sync service environment",
            gate,
        )
    try:
        config.vault_sync_local_root(target_env)
    except ValueError:
        return _check(
            "vault_sync",
            "MISCONFIGURED",
            "set UA_VAULT_PATH or UA_VAULT_SYNC_LOCAL_ROOT in the sync service environment",
            gate,
        )

    if state is None:
        return _check(
            "vault_sync",
            "UNKNOWN",
            f"{_VAULT_SYNC_UNIT} installed but has not completed a run yet",
            gate,
        )

    last_success = state.get("last_success_utc")
    if not last_success:
        return _check(
            "vault_sync",
            "MISCONFIGURED",
            "state file present but no successful sync has ever completed "
            f"(last_run_utc={state.get('last_run_utc')!r}, error={state.get('error')!r})",
            gate,
        )

    try:
        age_s = (datetime.now(UTC) - datetime.fromisoformat(last_success)).total_seconds()
    except (TypeError, ValueError):
        return _check(
            "vault_sync", "MISCONFIGURED", f"unparseable last_success_utc: {last_success!r}", gate
        )

    # target_env carries the sync service's settings.toml values as well as its
    # unit environment, so an interval set only in settings still counts.
    interval_s = (
        config.vault_sync_interval_minutes(target_env if service_env is not None else None) * 60
    )
    budget_s = _VAULT_SYNC_STALENESS_MULTIPLE * interval_s
    try:
        conflict_count = int(state.get("conflict_count", 0) or 0)
        filed_upstream_count = int(state.get("filed_upstream_count", 0) or 0)
        pulled_count = int(state.get("pulled_count", 0) or 0)
        if conflict_count < 0 or filed_upstream_count < 0 or pulled_count < 0:
            raise ValueError("negative count")
    except (TypeError, ValueError):
        return _check("vault_sync", "MISCONFIGURED", "invalid sync state counts", gate)

    fresh = 0 <= age_s <= budget_s
    # Legacy basename-based classifications are not trustworthy move evidence.
    clean = conflict_count == 0 and filed_upstream_count == 0
    last_run_healthy = (
        state.get("last_run_ok", True)
        and state.get("last_run_reachable", True)
        and not state.get("dry_run", False)
    )
    legacy_detail = (
        f"{filed_upstream_count} legacy filed-upstream (requires fresh sync), "
        if filed_upstream_count
        else ""
    )
    detail = (
        f"last sync {age_s:.0f}s ago (budget {budget_s:.0f}s at {interval_s // 60:.0f}min "
        f"interval x{_VAULT_SYNC_STALENESS_MULTIPLE}); {conflict_count} unresolved conflict(s), "
        f"{legacy_detail}"
        f"{pulled_count} file(s) pulled last run; timer installed={installed}, "
        f"last run healthy={bool(last_run_healthy)}"
    )
    if fresh and clean and installed and last_run_healthy:
        return _check("vault_sync", "LIVE", detail, gate)
    return _check("vault_sync", "MISCONFIGURED", f"degraded: {detail}", gate)


def vault_sync_status() -> dict:
    """This host's vault_sync row; a ua-node reports it on heartbeats (#1813)."""
    return _vault_sync_check()


# A report older than this is not current: the node probes every
# node_agent.VAULT_SYNC_PROBE_S (300s), so three missed probes means stale.
_FLEET_VAULT_SYNC_REPORT_MAX_AGE_S = 900


def _parse_utc(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _node_live_beat(info: dict) -> object:
    """The heartbeat that counts for a node's liveness (task #2196).

    ``node_registry.node_live_beat``: None while the stored beat predates the
    node's last retire/un-retire; ``last_heartbeat`` stays last-seen history.
    """
    from .. import node_registry

    return node_registry.node_live_beat(info)


def _fleet_vault_sync_check() -> dict:
    """Vault sync health as each live ua-node reports it (task #1813).

    ``vault_sync`` above reads only the host doctor runs on, and the hub does
    not pull its own vault, so a desktop timer failing every run surfaced
    nowhere the fleet looks. Each node sends its local ``vault_sync`` row on
    heartbeats; this folds the rows of nodes inside the liveness window into
    one line. An offline node is skipped. A live node with no report, or a
    report older than three probe intervals, is UNKNOWN rather than ignored,
    so a node that never reports (or stopped reporting) cannot read as green.
    """
    from .. import node_registry

    gate = "ua-node heartbeat vault_sync fact"
    try:
        nodes = node_registry.read_nodes()
    except Exception as exc:  # noqa: BLE001 - a doctor probe never raises
        return _check("fleet_vault_sync", "UNKNOWN", f"cannot read node registry: {exc}", gate)
    now = datetime.now(UTC)
    reports: dict[str, dict] = {}
    for name, info in nodes.items():
        if not _heartbeat_is_live(_parse_utc(_node_live_beat(info)), now):
            continue
        state = info.get("vault_sync")
        if not state:
            reports[name] = {"status": "UNKNOWN", "detail": "no vault sync report yet"}
            continue
        as_of = _parse_utc(state.get("as_of"))
        if as_of is None or (now - as_of).total_seconds() > _FLEET_VAULT_SYNC_REPORT_MAX_AGE_S:
            reports[name] = {
                "status": "UNKNOWN",
                "detail": f"last report is stale (as_of={state.get('as_of')!r})",
            }
            continue
        reports[name] = state
    pulling = {name: st for name, st in reports.items() if st["status"] != "DORMANT"}
    if not pulling:
        return _check(
            "fleet_vault_sync",
            "DORMANT",
            f"no live node runs vault sync ({len(reports)} live node(s) reporting)",
            gate,
        )
    bad = {name: st for name, st in pulling.items() if st["status"] != "LIVE"}
    if not bad:
        return _check("fleet_vault_sync", "LIVE", f"healthy on {', '.join(sorted(pulling))}", gate)
    detail = "; ".join(f"{name}: {st['status']} {st['detail']}" for name, st in sorted(bad.items()))
    if any(st["status"] == "MISCONFIGURED" for st in bad.values()):
        return _check("fleet_vault_sync", "MISCONFIGURED", f"degraded: {detail}", gate)
    return _check("fleet_vault_sync", "UNKNOWN", detail, gate)


def _fleet_nodes_check() -> dict:
    """Each host the operator declared, live or not (tasks #1393, #1835).

    The other fleet rows skip offline nodes, so a host whose ua-node stopped
    simply disappeared from them. This row starts from the declared fleet
    instead: every ``UA_FLEET_NODES`` name must have a heartbeat inside the
    liveness window, and one that does not is DEGRADED with its last heartbeat
    (or "never registered"). A ``UA_FLEET_OPTIONAL_NODES`` name is reported
    either way and never changes the status: absent reads as "unreported,
    optional by operator decision", not as healthy and not as a fault. Live
    nodes nobody declared are listed so a new host is not invisible. With no
    required node declared the row is DORMANT and names what is live. A
    declared name no node could register under is MISCONFIGURED.

    A heartbeat stamped more than the clock-skew allowance in the future is not
    live: a rolled-back clock must not keep a stopped node green.

    A row an operator retired (``node_retire``, task #2181) is not a host: it
    is listed under ``retired`` (a key present only when there is one) and
    counted once in the detail, and a declared name that is retired reads as
    retired rather than as never registered (still DEGRADED when required, so
    the declaration gets fixed).
    """
    from .. import node_registry

    gate = "UA_FLEET_NODES / UA_FLEET_OPTIONAL_NODES vs ua-node heartbeats"
    required = config.fleet_nodes()
    optional = config.fleet_optional_nodes()
    invalid = []
    for name in (*required, *optional):
        try:
            node_registry.validate_node_name(name)
        except node_registry.NodeError:
            invalid.append(name)
    try:
        nodes = node_registry.read_nodes()
    except Exception as exc:  # noqa: BLE001 - a doctor probe never raises
        return _check("fleet_nodes", "UNKNOWN", f"cannot read node registry: {exc}", gate)
    # Task #2181: retired rows (node_retire) are not fleet hosts; read_nodes
    # leaves them out. They are counted and named here once, and a declared
    # name that is retired says so instead of reading as never registered.
    try:
        retired = sorted(node_registry.read_retired_nodes())
    except Exception:  # noqa: BLE001 - a doctor probe never raises
        retired = []
    retired = [name for name in retired if name not in nodes]
    now = datetime.now(UTC)

    def last_beat(name: str) -> datetime | None:
        return _parse_utc((nodes.get(name) or {}).get("last_heartbeat"))

    def is_live(name: str) -> bool:
        # The kept beat is history; liveness uses the beat after the last
        # retire/un-retire (task #2196), as routing does.
        return _heartbeat_is_live(_parse_utc(_node_live_beat(nodes.get(name) or {})), now)

    live = sorted(name for name in nodes if is_live(name))
    undeclared = [name for name in live if name not in required and name not in optional]
    hosts = []
    for name in (*required, *optional):
        beat = last_beat(name)
        if name in retired:
            state = "retired"
        elif name not in nodes:
            state = "unregistered"
        elif is_live(name):
            state = "live"
        else:
            state = "stale"
        hosts.append(
            {
                "node": name,
                "required": name in required,
                "state": state,
                "last_heartbeat": beat.isoformat() if beat else None,
            }
        )
    # No node can register under an invalid name. A required one is a
    # misconfiguration; an optional one is named but, like any optional node,
    # never sets the status. Liveness is still reported either way.
    bad_required = [name for name in invalid if name in required]
    bad_optional = [name for name in invalid if name in optional]
    notes = []
    if bad_required:
        notes.append(f"not a valid node name: {', '.join(bad_required)}")
    if bad_optional:
        notes.append(f"not a valid optional node name: {', '.join(bad_optional)}")
    if notes:
        notes[-1] += " (1-128 of A-Z a-z 0-9 . _ -, starting alphanumeric)"

    def last_seen(name: str) -> str:
        beat = last_beat(name)
        if beat is None:
            return "no readable heartbeat"
        if beat > now:
            return f"heartbeat stamped {_human_age(beat - now)} in the future"
        return f"last heartbeat {_human_age(now - beat)} ago"

    def describe(host: dict) -> str:
        name = host["node"]
        if host["state"] == "retired":
            return f"{name} retired by an operator" + (
                " (still declared in UA_FLEET_NODES)" if host["required"] else " (optional)"
            )
        if host["state"] == "live":
            return f"{name} live" + ("" if host["required"] else " (optional)")
        if not host["required"]:
            seen = f" ({last_seen(name)})" if host["state"] == "stale" else ""
            return f"{name} unreported{seen}, optional by operator decision"
        if host["state"] == "unregistered":
            return f"{name} never registered"
        return f"{name} {last_seen(name)}"

    declared = set(required) | set(optional)
    retired_note = []
    undeclared_retired = [name for name in retired if name not in declared]
    if undeclared_retired:
        retired_note.append(
            f"{len(undeclared_retired)} retired node row(s) excluded: "
            + ", ".join(undeclared_retired)
        )

    if not required:
        detail = "no required node declared (set UA_FLEET_NODES)"
        if undeclared:
            detail += f"; live now: {', '.join(undeclared)}"
        optional_parts = [describe(host) for host in hosts]
        row = _check(
            "fleet_nodes",
            "DORMANT",
            "; ".join([*notes, detail, *optional_parts, *retired_note]),
            gate,
        )
        row["hosts"] = hosts
        if retired:
            row["retired"] = retired
        return row

    parts = [describe(host) for host in hosts]
    if undeclared:
        parts.append(f"also live, undeclared: {', '.join(undeclared)}")
    missing = [host for host in hosts if host["required"] and host["state"] != "live"]
    status = "MISCONFIGURED" if bad_required else "DEGRADED" if missing else "LIVE"
    row = _check("fleet_nodes", status, "; ".join([*notes, *parts, *retired_note]), gate)
    row["hosts"] = hosts
    if retired:
        row["retired"] = retired
    return row


def _heartbeat_is_live(beat: datetime | None, now: datetime) -> bool:
    """Inside the node liveness window, and no further in the future than the
    clock-skew allowance (``node_registry.NODE_CLOCK_SKEW_S``): a rolled-back
    hub clock or a hand-edited row must not keep a stopped node live on any
    fleet row (tasks #1835, #1987). Delegates to the one rule scheduler
    routing and the host views share (task #1997)."""
    from .. import node_registry

    return node_registry.heartbeat_is_live(beat, now)


def _human_age(age: timedelta) -> str:
    seconds = max(0, int(age.total_seconds()))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _hub_code_sha() -> str | None:
    """The commit the hub runs: its start record while that process lives, else HEAD here."""
    from .. import hub_runtime

    record = hub_runtime.read_hub_start()
    if record and record.get("sha") and hub_runtime.pid_alive(record.get("pid")):
        return str(record["sha"])
    return hub_runtime.checkout_head(hub_runtime.package_checkout())


def _fleet_node_versions_check() -> dict:
    """Whether each live ua-node runs the hub's commit (task #1946).

    The node version string is the package version, which does not change per
    commit, so a node twelve days behind the hub looked current. Each node now
    reports the HEAD of the checkout it was imported from (``code_sha``). This
    compares the live nodes (same liveness window as ``fleet_vault_sync``)
    with the hub's own commit, resolved once per run from files, with no
    network and no ``git`` subprocess. A difference is DEGRADED, not
    MISCONFIGURED: a node may lag a deploy briefly. A live node that reports
    no SHA (older code, or not a git checkout) is UNKNOWN.
    """
    from .. import node_registry

    gate = "ua-node code_sha fact vs hub checkout HEAD"
    try:
        nodes = node_registry.read_nodes()
    except Exception as exc:  # noqa: BLE001 - a doctor probe never raises
        return _check("fleet_node_versions", "UNKNOWN", f"cannot read node registry: {exc}", gate)
    now = datetime.now(UTC)
    live = {}
    for name, info in nodes.items():
        if _heartbeat_is_live(_parse_utc(_node_live_beat(info)), now):
            live[name] = info.get("code_sha")
    if not live:
        return _check("fleet_node_versions", "DORMANT", "no live ua-node", gate)
    try:
        hub_sha = _hub_code_sha()
    except Exception:  # noqa: BLE001 - a doctor probe never raises
        hub_sha = None
    if not hub_sha:
        return _check(
            "fleet_node_versions",
            "UNKNOWN",
            f"cannot read the hub's own checkout SHA to compare {len(live)} live node(s)",
            gate,
        )
    unknown = sorted(name for name, sha in live.items() if not sha)
    behind = {name: sha for name, sha in live.items() if sha and sha != hub_sha}
    parts = []
    if behind:
        parts.append(
            "differs from hub "
            + hub_sha[:12]
            + ": "
            + ", ".join(f"{name} at {sha[:12]}" for name, sha in sorted(behind.items()))
        )
    if unknown:
        parts.append(
            "no code_sha reported (older ua-node or not a git checkout): " + ", ".join(unknown)
        )
    if behind:
        return _check("fleet_node_versions", "DEGRADED", "; ".join(parts), gate)
    if unknown:
        return _check("fleet_node_versions", "UNKNOWN", "; ".join(parts), gate)
    return _check(
        "fleet_node_versions",
        "LIVE",
        f"{', '.join(sorted(live))} at hub commit {hub_sha[:12]}",
        gate,
    )


def _fleet_node_acceptance_check() -> dict:
    """Which registered ua-nodes passed device acceptance (task #2112).

    A node is accepted when its latest acceptance run (``node_acceptance``)
    passed every diagnostic and an independent lifecycle operator attested
    the exact run for the bound agent/device. ``LIVE`` when every
    registered node is accepted. A failed run on a counted node is ``DEGRADED`` either way. With
    ``UA_NODE_REQUIRE_ACCEPTANCE=1`` a node that is not accepted takes no real
    work, so any such node is ``DEGRADED``; with the gate off, nodes never run
    or still pending leave the row ``DORMANT``. No registered node is
    ``DORMANT``; an unreadable store is ``UNKNOWN``. A node whose heartbeat
    is past the registry's offline cutoff is named but not counted, so a
    retired or renamed device's leftover row cannot hold the row down.
    """
    from .. import node_acceptance, node_registry

    required = config.node_require_acceptance()
    gate = "node_acceptance runs" + (
        " (UA_NODE_REQUIRE_ACCEPTANCE=1)" if required else " (UA_NODE_REQUIRE_ACCEPTANCE off)"
    )
    try:
        nodes = node_registry.read_nodes()
        states = node_acceptance.read_states()
    except Exception as exc:  # noqa: BLE001 - a doctor probe never raises
        return _check("fleet_node_acceptance", "UNKNOWN", f"cannot read acceptance: {exc}", gate)
    if not nodes:
        return _check("fleet_node_acceptance", "DORMANT", "no registered ua-node", gate)
    accepted, failed, waiting, offline = [], [], [], []
    now = datetime.now(UTC)
    for name, info in sorted(nodes.items()):
        state = states.get(name)
        beat = info.get("last_heartbeat")
        parsed = node_registry.parse_heartbeat(beat)
        if (
            parsed is not None
            and parsed <= now
            and node_registry.heartbeat_status(beat, now) == "offline"
        ):
            # Silent past the registry's offline cutoff (a day by default): a
            # retired or renamed device whose row was never removed. Named
            # with its last run, not counted (task #2172). This window is
            # wider than the versions and vault-sync rows, which skip any
            # node that is not live; a node down for minutes still counts.
            # A beat in the future also reads offline (clock skew) but is
            # not old, so it stays counted.
            last = state["state"] if state else "never run"
            offline.append(f"{name} offline since {beat} (last run: {last})")
        elif state is None:
            waiting.append(f"{name} never run")
        elif state["agent"] != info.get("agent"):
            waiting.append(f"{name} accepted for another agent")
        elif state["state"] == "accepted":
            accepted.append(name)
        elif state["state"] == "failed":
            failed.append(f"{name} failed ({state.get('reason') or 'no reason'})")
        else:
            waiting.append(f"{name} pending")
    parts = []
    if accepted:
        parts.append("accepted: " + ", ".join(accepted))
    if failed:
        parts.append("failed: " + "; ".join(failed))
    if waiting:
        parts.append("not accepted: " + ", ".join(waiting))
    if offline:
        parts.append("not counted: " + ", ".join(offline))
    detail = "; ".join(parts)
    if not accepted and not failed and not waiting:
        return _check("fleet_node_acceptance", "DORMANT", detail, gate)
    if not failed and not waiting:
        return _check("fleet_node_acceptance", "LIVE", detail, gate)
    if failed or required:
        return _check("fleet_node_acceptance", "DEGRADED", detail, gate)
    return _check("fleet_node_acceptance", "DORMANT", detail, gate)


def _litestream_binary() -> str | None:
    found = shutil.which("litestream")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "litestream"
    return str(candidate) if candidate.is_file() else None


def _litestream_json(argv: list[str], env: dict[str, str]) -> tuple[list | None, str | None]:
    """Run a litestream `-json` command; (rows, None) or (None, error detail)."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=REPLICATION_PROBE_TIMEOUT_S,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{argv[1] if len(argv) > 1 else 'litestream'} failed to run: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return None, detail[-1] if detail else f"exit {proc.returncode}"
    try:
        parsed = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None, "unparseable -json output"
    return (parsed if isinstance(parsed, list) else []), None


def _txid_int(value: object) -> int | None:
    if not isinstance(value, str) or value in ("", "-"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _parse_gs_uri(uri: str) -> tuple[str, str] | None:
    if not uri.startswith("gs://"):
        return None
    rest = uri[5:]
    bucket, _, path = rest.partition("/")
    path = path.strip("/")
    if not bucket or not path:
        return None
    return bucket, path


def _parse_ltx_object(name: str) -> tuple[int, int] | None:
    base = name.rsplit("/", 1)[-1]
    if not base.endswith(".ltx"):
        return None
    min_s, sep, max_s = base[: -len(".ltx")].partition("-")
    if not sep:
        return None
    try:
        return int(min_s, 16), int(max_s, 16)
    except ValueError:
        return None


def _ltx_timestamp(item: dict) -> str:
    meta = item.get("metadata") or {}
    stamped = meta.get("litestream-timestamp")
    if isinstance(stamped, str) and stamped:
        return stamped
    created = item.get("timeCreated")
    return created if isinstance(created, str) else ""


class _BoundedAuthRequest:
    """google-auth transport whose default timeout is the list budget, not 120s."""

    def __init__(self) -> None:
        from google.auth.transport.requests import Request

        self._request = Request()

    def __call__(
        self, url, method="GET", body=None, headers=None, timeout=REPLICA_LIST_TIMEOUT_S, **kwargs
    ):
        bounded = (
            REPLICA_LIST_TIMEOUT_S if timeout is None else min(timeout, REPLICA_LIST_TIMEOUT_S)
        )
        return self._request(
            url, method=method, body=body, headers=headers, timeout=bounded, **kwargs
        )


def _gcs_access_token(env: dict[str, str]) -> str:
    import google.auth

    scopes = ["https://www.googleapis.com/auth/devstorage.read_only"]
    cred_path = env.get("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path:
        creds, _ = google.auth.load_credentials_from_file(cred_path, scopes=scopes)
    else:
        creds, _ = google.auth.default(scopes=scopes)
    creds.refresh(_BoundedAuthRequest())
    token = getattr(creds, "token", None)
    if not token:
        raise RuntimeError("GCS credentials produced no access token")
    return token


def _gcs_first_object(bucket: str, prefix: str, start_offset: str, token: str) -> dict | None:
    query = urllib.parse.urlencode(
        {
            "prefix": prefix,
            "startOffset": start_offset,
            "maxResults": "1",
            "fields": "items(name,timeCreated,metadata)",
        }
    )
    url = f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o?{query}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=REPLICA_LIST_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"GCS list HTTP {exc.code}: {detail}") from exc
    items = payload.get("items") or []
    return items[0] if items else None


def _newest_l0_row(
    object_prefix: str, local_txid: int, fetch_one, deadline: float | None = None
) -> tuple[dict | None, str | None]:
    """Binary-search the newest L0 object whose min txid is <= local_txid.

    ``fetch_one(start_offset)`` returns the first object at or after that name,
    or None. A timeout after a caught-up object is already in hand must not
    discard that object: the full enumeration used to time out and report a
    healthy replica as MISCONFIGURED.
    """
    lo, hi = 0, local_txid
    best: dict | None = None
    if deadline is None:
        deadline = time.monotonic() + REPLICA_SAMPLE_BUDGET_S
    while lo <= hi:
        if time.monotonic() > deadline:
            if best and _txid_int(best.get("max_txid")) is not None:
                if _txid_int(best["max_txid"]) >= local_txid:
                    return best, None
            return None, "replica list exceeded its budget before the search finished"
        mid = (lo + hi) // 2
        try:
            item = fetch_one(f"{object_prefix}{mid:016x}")
        except Exception as exc:
            if best and (_txid_int(best.get("max_txid")) or -1) >= local_txid:
                return best, None
            return best, f"replica list failed: {exc}"
        parsed = _parse_ltx_object(item["name"]) if item else None
        if parsed is None or parsed[0] > local_txid:
            hi = mid - 1
            continue
        min_txid, max_txid = parsed
        best = {
            "max_txid": f"{max_txid:016x}",
            "timestamp": _ltx_timestamp(item),
            "min_txid": f"{min_txid:016x}",
        }
        lo = mid + 1
    return best, None


def _replica_sample(
    uri: str, local_txid: int, env: dict[str, str]
) -> tuple[list | None, str | None]:
    """Newest LTX object at the replica, from L0 upward, without listing every file."""
    parsed = _parse_gs_uri(uri)
    if parsed is None:
        return None, f"replica URI is not a gs:// URL: {uri}"
    bucket, path = parsed
    try:
        token = _gcs_access_token(env)
    except Exception as exc:
        return None, f"GCS credentials: {exc}"

    best: dict | None = None
    deadline = time.monotonic() + REPLICA_SAMPLE_BUDGET_S
    finished = True
    for level in _REPLICA_LEVELS:
        if time.monotonic() > deadline:
            finished = False
            break
        prefix = f"{path}/ltx/{level}/"

        def fetch_one(start_offset: str, prefix: str = prefix) -> dict | None:
            return _gcs_first_object(bucket, prefix, start_offset, token)

        try:
            head = fetch_one(prefix)
        except Exception as exc:
            if best is not None and (_txid_int(best.get("max_txid")) or -1) >= local_txid:
                return [best], None
            if best is not None:
                return [best], f"replica list failed: {exc}"
            return None, f"replica list failed: {exc}"
        if head is None:
            continue
        row, err = _newest_l0_row(prefix, local_txid, fetch_one, deadline)
        if row is not None and (
            best is None
            or (_txid_int(row.get("max_txid")) or -1) > (_txid_int(best.get("max_txid")) or -1)
        ):
            best = row
        if err:
            if best is not None and (_txid_int(best.get("max_txid")) or -1) >= local_txid:
                return [best], None
            if best is not None:
                return [best], err
            return None, err
        if best is not None and (_txid_int(best.get("max_txid")) or -1) >= local_txid:
            break
    if best is not None:
        return [best], None
    if not finished:
        return None, "replica list exceeded its budget before every level was checked"
    return [], None


def _cached_replica_sample(
    uri: str, local_txid: int, env: dict[str, str]
) -> tuple[list | None, str | None]:
    global _replica_cache
    now = time.monotonic()
    if (
        _replica_cache
        and _replica_cache[1] == uri
        and _replica_cache[2] == local_txid
        and (now - _replica_cache[0]) < REPLICATION_RESULT_CACHE_S
    ):
        return list(_replica_cache[3]), None
    rows, err = _replica_sample(uri, local_txid, env)
    if err is None and rows:
        _replica_cache = (now, uri, local_txid, rows)
    return rows, err


def _replication_check() -> dict:
    """Continuous WAL replication of memory.db to the GCS replica (task #1197).

    Replica-side state is authoritative: local_txid (litestream status) vs the
    newest L0 object's max txid at the replica. A txid gap that keeps
    aging past REPLICATION_LAG_BUDGET_S means the daemon is down or GCS is
    unreachable; a gap with a fresh LTX is just an in-flight sync interval.
    """
    capability = "wal_replication"
    gate = "litestream + UA_REPLICA_GCS_URI (deploy/litestream/, com.unified-agent.litestream)"
    replica_uri, _ = _hub_setting("UA_REPLICA_GCS_URI")
    gac, _ = _hub_setting("GOOGLE_APPLICATION_CREDENTIALS")
    binary = _litestream_binary()
    if not replica_uri:
        detail = (
            "litestream installed but UA_REPLICA_GCS_URI unset — replication not configured"
            if binary
            else "no litestream binary and no UA_REPLICA_GCS_URI — WAL replication not configured"
        )
        return _check(capability, "DORMANT", detail, gate)
    if not binary:
        return _check(
            capability,
            "MISCONFIGURED",
            "UA_REPLICA_GCS_URI is set but litestream is not installed",
            gate,
        )
    deploy_dir = _repo_deploy_dir()
    cfg = deploy_dir / "litestream" / "litestream.yml" if deploy_dir else None
    if not cfg or not cfg.is_file():
        return _check(
            capability,
            "UNKNOWN",
            "deploy/litestream/litestream.yml not found — cannot probe local txid",
            gate,
        )
    env = {
        **os.environ,
        "UA_HOME": str(config.data_home()),
        "UA_REPLICA_GCS_URI": replica_uri,
    }
    if not env.get("GOOGLE_APPLICATION_CREDENTIALS") and gac:
        env["GOOGLE_APPLICATION_CREDENTIALS"] = gac

    status_rows, err = _litestream_json([binary, "status", "-json", "-config", str(cfg)], env)
    if err:
        return _check(capability, "MISCONFIGURED", f"litestream status: {err}", gate)
    local_hint = (status_rows or [{}])[0].get("local_txid", "-")
    upper = _txid_int(local_hint)
    if upper is None:
        return _check(
            capability,
            "MISCONFIGURED",
            "local db has no LTX data yet",
            gate,
        )
    ltx_rows, err = _cached_replica_sample(replica_uri, upper, env)
    if err and not ltx_rows:
        detail = f"litestream ltx on replica: {err}"
        # #1406 follow-on: a USER_MANAGED SA key deleted from GCP still leaves
        # GOOGLE_APPLICATION_CREDENTIALS pointing at a local JSON that mints
        # JWTs Google rejects. Point the operator at the remediation.
        err_l = err.lower()
        if "invalid_grant" in err_l or "invalid jwt" in err_l or "jwt signature" in err_l:
            detail += (
                " — GOOGLE_APPLICATION_CREDENTIALS SA key is rejected by GCS; "
                "mint a new USER_MANAGED key for the backup uploader and replace "
                "the local JSON (do not blank UA_REPLICA_GCS_URI / UA_BACKUP_GCS_URI)"
            )
        return _check(capability, "MISCONFIGURED", detail, gate)
    if not ltx_rows:
        return _check(
            capability,
            "MISCONFIGURED",
            f"no LTX files at {replica_uri} — replication has never synced",
            gate,
        )

    local_txid_s = (status_rows or [{}])[0].get("local_txid", "-")
    replica_max = max(int(r.get("max_txid", "0"), 16) for r in ltx_rows)
    ages = []
    for row in ltx_rows:
        try:
            ts = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
            ages.append((datetime.now(UTC) - ts).total_seconds())
        except (ValueError, TypeError):
            continue
    age_s = min(ages) if ages else float("inf")

    daemon = ""
    if sys.platform == "darwin":
        props = service_probes._service_probe(
            "com.unified-agent.litestream", SERVICE_CHECK_TIMEOUT_S
        )
        if props is not None and props.get("LoadState") == "loaded":
            daemon = f"; launchagent {props.get('ActiveState')}"

    if local_txid_s == "-":
        return _check(
            capability,
            "MISCONFIGURED",
            f"local db has no LTX data yet (replica at txid {replica_max:#x}){daemon}",
            gate,
        )
    gap = int(local_txid_s, 16) - replica_max
    if gap <= 0:
        return _check(
            capability,
            "LIVE",
            f"replica caught up at txid {local_txid_s}; newest LTX {age_s:.0f}s ago{daemon}",
            gate,
        )
    if age_s <= REPLICATION_LAG_BUDGET_S:
        return _check(
            capability,
            "LIVE",
            f"replica {gap} txid(s) behind local {local_txid_s}; last sync {age_s:.0f}s ago "
            f"is inside the sync interval{daemon}",
            gate,
        )
    return _check(
        capability,
        "MISCONFIGURED",
        f"replica {gap} txid(s) behind local {local_txid_s} and newest LTX is "
        f"{age_s:.0f}s old (> {REPLICATION_LAG_BUDGET_S:.0f}s lag budget){daemon}",
        gate,
    )


def _semantic_search_check() -> dict:
    """Report whether semantic memory search is actually working on this host.

    It is the one capability that can fail completely while every tool call
    still returns 200: `memory_save` succeeds without an embedding row and
    `memory_search` silently falls back to FTS, so nothing distinguishes a
    machine that lost semantic search from one that never had it (assessment
    §3.2). Two independent signals are combined:

    - `embeddings.embedding_state()` — failures observed in *this* process.
      Definitive when non-zero, but empty on a freshly restarted hub.
    - `storage.embedding_coverage()` — notes with no embedding row. Survives
      restarts and needs no model, which is why it carries the check.
    """
    gate = "fastembed model cache (~/.cache/huggingface)"
    from .. import embeddings

    state = embeddings.embedding_state()
    try:
        coverage = storage.embedding_coverage()
    except Exception as exc:
        return _check("semantic_search", "MISCONFIGURED", f"cannot read coverage: {exc}", gate)

    current_model = config.embed_model()
    try:
        stored_models = storage.stored_embedding_model_ids()
    except Exception as exc:
        return _check(
            "semantic_search",
            "MISCONFIGURED",
            f"cannot read stored embedding model id: {exc}",
            "UA_EMBED_MODEL",
        )
    mismatched = [model for model in stored_models if model != current_model]
    if mismatched:
        return _check(
            "semantic_search",
            "MISCONFIGURED",
            "stored embedding model id "
            + ", ".join(mismatched)
            + f" != current UA_EMBED_MODEL {current_model}; existing vectors were not rewritten",
            "UA_EMBED_MODEL",
        )

    if state["degraded"]:
        return _check(
            "semantic_search",
            "MISCONFIGURED",
            f"embedding is failing — {state['failures']} failure(s) this process with no "
            f"success since, last: {state['last_error']}. {coverage['missing']} of "
            f"{coverage['total']} notes unembedded; queries and new notes are FTS-only",
            gate,
        )
    if coverage["total"] == 0:
        return _check("semantic_search", "DORMANT", "no memory notes to embed yet", gate)
    if coverage["missing"]:
        detail = (
            f"{coverage['missing']} of {coverage['total']} notes have no embedding — "
            "those notes are reachable by keyword only"
        )
        if state["last_error"]:
            detail += f"; last embedding error: {state['last_error']}"
        return _check("semantic_search", "MISCONFIGURED", detail, gate)
    legacy_excluded = coverage.get("legacy_excluded", 0)
    if legacy_excluded:
        # A nondefault UA_EMBED_MODEL drops NULL-model rows out of cosine
        # search. They still have an embedding row, so `missing` stays 0 and
        # this used to read LIVE. Count them as a coverage gap. The default
        # model still treats those rows as compatible, and embedding_coverage
        # reports legacy_excluded=0 in that case.
        return _check(
            "semantic_search",
            "MISCONFIGURED",
            f"{legacy_excluded} of {coverage['total']} notes have unlabeled embeddings "
            f"excluded by UA_EMBED_MODEL {current_model}; those notes are reachable "
            "by keyword only",
            "UA_EMBED_MODEL",
        )
    if coverage["mirror_available"] and coverage["mirror_missing"]:
        # A complete memory_embeddings table does not prove search works. With
        # sqlite-vec loaded, _semantic_similarity_rows reads memory_vec and
        # returns [] outright when it is empty — it only falls back to the UDF
        # scan on OperationalError, which a present-but-empty table does not
        # raise. upsert_memory_vec swallows its failures, so this gap is
        # reachable with every other signal reading healthy.
        embedded = coverage["embedded"]
        blind = coverage["mirror_missing"] >= embedded
        return _check(
            "semantic_search",
            "MISCONFIGURED",
            f"{coverage['mirror_missing']} of {embedded} embeddings missing from the "
            "memory_vec search index — "
            + (
                "the index is empty, so semantic search returns no results at all"
                if blind
                else "those notes cannot be reached by semantic search"
            ),
            gate,
        )
    if coverage["mirror_available"] and not coverage["mirror_usable"]:
        detail = "memory_vec search index failed its KNN probe"
        if coverage["mirror_error"]:
            detail += f": {coverage['mirror_error']}"
        return _check("semantic_search", "MISCONFIGURED", detail, gate)

    if not state["successes"]:
        # Everything on record is embedded, but nothing has exercised the model
        # since this process started, so its readiness is unverified. Claiming
        # LIVE here was a real false all-clear: a hub restarted after the model
        # went away has a fully-embedded corpus and a clean slate, and every
        # query silently falls back to FTS. UNKNOWN rather than a probe because
        # a probe would make a read-only status call download a model on a cold
        # host; the first real save or search resolves this within seconds of
        # the hub being used.
        return _check(
            "semantic_search",
            "UNKNOWN",
            f"all {coverage['total']} notes embedded, but nothing has embedded "
            "successfully in this process — model readiness unverified since restart",
            gate,
        )

    detail = f"all {coverage['total']} notes embedded"
    if state["dimensions"]:
        detail += f" ({state['dimensions']}-dim vectors)"
    if state["failures"]:
        # Reached only when something succeeded *after* the last failure, so the
        # model demonstrably works now. Still worth surfacing: a host recovering
        # from repeated failures is not the same as one that never failed.
        detail += f"; recovered after {state['failures']} earlier failure(s)"
    return _check("semantic_search", "LIVE", detail, gate)


def _symbol_embedding_check() -> dict:
    """Report symbol-index model ids for this workspace only.

    ``semantic_search`` judges memory embeddings. Symbol vectors live in the
    same database for every workspace, so a stale index elsewhere must not
    fail that check. This one joins ``workspace_symbols.workspace``.
    """
    gate = "UA_EMBED_MODEL"
    current_model = config.embed_model()
    try:
        if isinstance(config.workspace_root_var.get(), config.WorkspaceDenied):
            # Hub-owned census (task #1779): a DENY binding forbids the caller's
            # file tools, not this read of model ids and counts. Same rule as
            # the worktree survey: read the process workspace.
            workspace = str(config.process_workspace_root().resolve())
        else:
            from ..safety import resolve_in_workspace

            workspace = str(resolve_in_workspace("."))
    except Exception as exc:
        return _check(
            "symbol_embeddings",
            "MISCONFIGURED",
            f"cannot resolve workspace for symbol embedding model id: {exc}",
            gate,
        )
    try:
        coverage = storage.symbol_embedding_coverage(workspace)
    except Exception as exc:
        return _check(
            "symbol_embeddings",
            "MISCONFIGURED",
            f"cannot read symbol embedding model id: {exc}",
            gate,
        )
    total = coverage["total"]
    legacy = coverage["legacy"]
    blank = coverage["blank"]
    stored_models = coverage["models"]
    # No rows at all. An all-NULL index is coverage, not an empty index:
    # NULL is the default model and search still returns those symbols.
    # A blank model is not NULL, so an all-blank index is excluded coverage.
    if total == 0:
        return _check(
            "symbol_embeddings",
            "DORMANT",
            "no symbol embeddings in this workspace",
            gate,
        )
    legacy_compatible = current_model == config.DEFAULT_EMBED_MODEL
    mismatched = [model for model in stored_models if model != current_model]
    if mismatched:
        detail = (
            "stored symbol embedding model id "
            + ", ".join(mismatched)
            + f" != current UA_EMBED_MODEL {current_model}; existing vectors were not rewritten"
        )
        if blank:
            detail += f"; {blank} blank model rows are excluded"
        if legacy and not legacy_compatible:
            detail += f"; {legacy} unlabeled legacy rows are excluded"
        return _check("symbol_embeddings", "MISCONFIGURED", detail, gate)
    if blank:
        excluded_legacy = legacy if not legacy_compatible else 0
        detail = (
            f"{blank} of {total} symbol embeddings have a blank model and are excluded "
            f"by UA_EMBED_MODEL {current_model}"
        )
        if excluded_legacy:
            detail += f"; {excluded_legacy} unlabeled legacy rows are also excluded"
        return _check(
            "symbol_embeddings",
            "MISCONFIGURED",
            detail,
            gate,
        )
    if legacy and not legacy_compatible:
        return _check(
            "symbol_embeddings",
            "MISCONFIGURED",
            f"{legacy} of {total} symbol embeddings are unlabeled and excluded "
            f"by UA_EMBED_MODEL {current_model}",
            gate,
        )
    if legacy and legacy_compatible:
        labeled = total - legacy
        if labeled:
            detail = (
                f"symbol embeddings in this workspace use {current_model}; "
                f"{legacy} unlabeled legacy rows are compatible"
            )
        else:
            detail = (
                f"{legacy} unlabeled legacy symbol embeddings in this workspace "
                f"are compatible with the default model {current_model}"
            )
        return _check("symbol_embeddings", "LIVE", detail, gate)
    return _check(
        "symbol_embeddings",
        "LIVE",
        f"symbol embeddings in this workspace use {current_model}",
        gate,
    )


def _zulip_relay_check() -> dict:
    """Verify owner-only zuliprc files exist for every mapped desktop agent bot."""
    gate = "UA_HOME/zuliprc-desktop-* + UA_ZULIP_SITE"
    try:
        from . import zulip

        status = zulip.zulip_credential_status()
    except Exception as exc:  # noqa: BLE001 — doctor must never abort
        return _check("zulip_relay", "MISCONFIGURED", f"credential probe failed: {exc}", gate)

    missing = status.get("missing") or []
    site = status.get("site", "")
    configured = [
        filename
        for filename in zulip._BOT_CONFIG_FILES.values()
        if (config.data_home() / filename).is_file()
    ]
    if not configured:
        # Nothing configured at all — the common case on any host that does not
        # run the Zulip relay (every foreign install, and this repo before the
        # fleet's zuliprc files are dropped in). That is DORMANT, not broken.
        return _check(
            "zulip_relay",
            "DORMANT",
            "no Zulip bot credentials on this host — relay not in use "
            f"(drop zuliprc-desktop-* into {config.data_home()} to enable)",
            gate,
        )
    if not os.environ.get("UA_ZULIP_SITE", "").strip():
        # Bot credentials on disk but no site: with Zulip retired (the default)
        # the relay is simply unused; un-retired, it cannot send (#1772).
        if zulip.zulip_retired():
            return _check(
                "zulip_relay",
                "DORMANT",
                f"Zulip retired and UA_ZULIP_SITE unset — {len(configured)} bot "
                "credential file(s) present but unused",
                gate,
            )
        return _check(
            "zulip_relay",
            "MISCONFIGURED",
            "Zulip is not retired but UA_ZULIP_SITE is unset — configure the site "
            "(chat.zulip_site) or retire Zulip",
            gate,
        )
    if missing:
        return _check(
            "zulip_relay",
            "MISCONFIGURED",
            f"partial Zulip config — missing or invalid credentials for: {', '.join(missing)}",
            gate,
        )
    return _check(
        "zulip_relay",
        "LIVE",
        f"{len(status.get('agents') or {})} desktop bot credential(s) loadable for {site}",
        gate,
    )


def _mcp_gateway_check() -> dict:
    """`mcp_gateway` (task #1336): flag, config and secret metadata; no network."""
    try:
        from . import mcp_gateway

        return mcp_gateway.doctor_check()
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check(
            "mcp_gateway",
            "UNKNOWN",
            f"gateway probe failed: {type(exc).__name__}",
            "UA_MCP_GATEWAY",
        )


def _zulip_chat_check() -> dict:
    """Report whether Slack-like Zulip live-chat replies are enabled on the worker."""
    gate = "UA_ZULIP_CHAT (unified-agent-zulip-worker.service)"
    try:
        from .. import zulip_chat
    except Exception as exc:  # noqa: BLE001 — doctor must never abort
        return _check("zulip_chat", "MISCONFIGURED", f"import failed: {exc}", gate)

    unit = _SERVICE_UNITS["zulip_worker_service"]
    env = service_probes._service_env(unit)
    if env is not None and "UA_ZULIP_CHAT" in env:
        enabled = env["UA_ZULIP_CHAT"].strip().lower() in {"1", "true", "yes", "on"}
        channels_raw = env.get("UA_ZULIP_CHAT_CHANNELS", "").strip()
    else:
        enabled = zulip_chat.chat_enabled()
        channels_raw = os.environ.get("UA_ZULIP_CHAT_CHANNELS", "").strip()
    # Same fail-closed rule as tools.zulip.zulip_retired(): only an explicit
    # 0/false/no/off re-opens Zulip. A retired hub makes chat inert (task #1714).
    # The worker reads only its unit environment: when that exists without
    # UA_ZULIP_RETIRED, the worker is retired whatever this process says.
    retired_raw = (
        env.get("UA_ZULIP_RETIRED") if env is not None else os.environ.get("UA_ZULIP_RETIRED")
    )
    retired = (retired_raw or "").strip().lower() not in {"0", "false", "no", "off"}

    if channels_raw:
        values = {part.strip().lower() for part in channels_raw.split(":") if part.strip()}
        from . import zulip as zulip_tools

        channels = frozenset(values & set(zulip_tools.ALLOWED_CHANNELS)) or zulip_chat.CHAT_CHANNELS
    else:
        channels = zulip_chat.CHAT_CHANNELS

    if not enabled:
        return _check(
            "zulip_chat",
            "DORMANT",
            "live chat replies off — set UA_ZULIP_CHAT=1 in the zulip worker "
            "EnvironmentFile (~/.unified_agent/service.env)",
            gate,
        )
    if retired:
        return _check(
            "zulip_chat",
            "DORMANT",
            "UA_ZULIP_CHAT is on but Zulip is retired (UA_ZULIP_RETIRED): no reply is "
            "generated and no model is called",
            gate,
        )
    return _check(
        "zulip_chat",
        "LIVE",
        f"live chat replies enabled on channels: {', '.join(sorted(channels))}",
        gate,
    )


def _ts_worker_check() -> dict:
    """Report the task-space (Discord/Telegram) chat worker's config posture.

    Tokens are env-only on the hub host (task #727); without one the worker
    has nothing to poll, which is DORMANT (not broken) — the exact posture of
    zulip_relay before zuliprc files land. A token without its allowed-user
    allowlist would let any sender be classified (never acted on), so a
    missing allowlist with a live token is MISCONFIGURED.
    """
    discord_gate = "UA_TS_DISCORD_TOKEN + UA_TS_DISCORD_ALLOWED_USER_IDS (+ UA_TS_DISCORD_CHANNELS)"
    telegram_gate = "UA_TS_TELEGRAM_TOKEN + UA_TS_TELEGRAM_ALLOWED_USER_IDS"
    try:
        from ..ts_worker import transport as ts_transport
    except Exception as exc:  # noqa: BLE001 — doctor must never abort
        return _check("ts_worker", "MISCONFIGURED", f"import failed: {exc}", discord_gate)

    missing: list[str] = []
    configured: list[str] = []
    if ts_transport.discord_token() is not None:
        if not ts_transport.discord_allowed_user_ids():
            missing.append("discord allowed-user allowlist")
        elif not (os.environ.get("UA_TS_DISCORD_CHANNELS") or "").strip():
            missing.append("UA_TS_DISCORD_CHANNELS (no channels to poll)")
        else:
            configured.append("discord")
    if ts_transport.telegram_token() is not None:
        if not ts_transport.telegram_allowed_user_ids():
            missing.append("telegram allowed-user allowlist")
        else:
            configured.append("telegram")
    if not configured and not missing:
        return _check(
            "ts_worker",
            "DORMANT",
            "no Discord/Telegram tokens on this host — surfaces disabled "
            "(set UA_TS_DISCORD_TOKEN / UA_TS_TELEGRAM_TOKEN to enable; "
            "tokens land via #727, never in chat)",
            f"{discord_gate} / {telegram_gate}",
        )
    if missing:
        return _check(
            "ts_worker",
            "MISCONFIGURED",
            "partial task-space chat config — missing or invalid: " + ", ".join(missing),
            f"{discord_gate} / {telegram_gate}",
        )
    return _check(
        "ts_worker",
        "LIVE",
        f"task-space chat surface(s) configured: {', '.join(sorted(configured))}",
        f"{discord_gate} / {telegram_gate}",
    )


def _sleep_pass_installed() -> bool | None:
    """Whether this host schedules the nightly sleep pass (timer or LaunchAgent).

    The same probe the ``sleep_pass_timer`` check uses. A host that never runs
    the pass has no nightly chain to judge (task #1731). ``None`` means the
    probe could not tell (service manager unavailable, timed out, or
    inconclusive); that is not the same as a definitively absent unit.
    """
    props = service_probes._service_probe(_TIMER_UNITS["sleep_pass_timer"], SERVICE_CHECK_TIMEOUT_S)
    if props is None:
        return None
    return props.get("LoadState") == "loaded"


def _leg_part(leg: dict, name: str | None = None) -> str:
    """One leg as ``name:state``, with its failure detail or healthy caveat."""
    if leg["reachable"] is True:
        state = "up"
    elif leg["reachable"] is False:
        state = "down"
    else:
        state = "unconfigured"
    part = f"{name or leg['leg']}:{state}"
    if (leg["reachable"] is False or leg.get("memory_selected")) and leg.get("detail"):
        part += f" ({leg['detail']})"
    if leg["reachable"] is True and leg.get("caveat"):
        # The subscription check proves login, not remaining quota; a memory
        # route also reports the model it pinned above.
        part += f" ({leg['caveat']})"
    return part


def _llm_provider_chain_check() -> dict:
    """Per-leg health of the llm_client fallback chain, without spending.

    Before this check the only way to learn a leg's state was a `complete()`
    call that spends the moment a leg answers (task #1178). Every leg probe is
    config presence plus an unbilled check — `claude --help` and
    `claude auth status` for the claude_cli leg (task #2025), a metadata GET
    for the HTTP legs — never a generation call. The hub chain is probed with the hub unit's
    Environment(File) merged over this process's — the same source
    `_hub_setting` prefers — because the legs run inside the hub.

    The nightly sleep pass is a separate chain (task #1639): its provider
    settings come from sleep-pass.env, then the settings.toml values it seeds
    at startup (task #2175), plus the local-leg keys it inherits from
    service.env. A memory pin in either of the first two layers is the one leg
    the pass runs, so only that leg is listed. It is probed with that
    environment and judged on its own, so neither chain's working leg can mask
    the other having none: a usable hub with an unusable nightly pass (or the
    reverse) is DEGRADED. The chain is claude_cli then openrouter (task #2025): the claude_cli row is up only
    when `claude --help` lists the flags the leg passes and on a claude.ai
    subscription login (`claude auth status`, no completion), and
    the openrouter row only when its key answers and its pinned model is
    still listed. The nightly rows use the scheduler's PATH, not the hub's.

    A memory pin in the hub's environment (UA_MEMORY_PROVIDER, task #2021)
    routes the hub's memory calls (consolidate adjudication) to that one leg,
    never the chain, so it is judged as a third route (`<leg>[memory]` rows):
    a pinned leg that is down or unconfigured is an outage for memory calls
    even while another hub leg is up, and invalid memory settings, which fail
    every memory call before inference, are MISCONFIGURED.
    """
    gate = (
        "llm_client legs: claude CLI (UA_CLAUDE_CLI_BIN or PATH, logged in) / "
        "OPENROUTER_API_KEY (+UA_OPENROUTER_MODEL) / "
        "UA_SUBSCRIPTION_POOL_ENABLED+URL+API_KEY; nightly pass: sleep-pass.env + settings.toml"
    )
    try:
        from .. import llm_client, sleep_pass

        env = dict(os.environ)
        env.update(service_probes._service_env(_SERVICE_UNITS["hub_service"]) or {})
        legs = llm_client.leg_status(env=env)
        try:
            memory_provider = llm_client.memory_model_settings(env)[0]
        except ValueError as exc:
            # The message is a fixed validation string; it names no value.
            return _check(
                "llm_provider_chain",
                "MISCONFIGURED",
                f"hub memory route misconfigured: {exc}",
                gate,
            )
        memory_legs = llm_client.leg_status(env=env, memory_agent=True) if memory_provider else None
        # Only a definitively absent sleep pass skips the nightly chain; an
        # indeterminate probe judges it conservatively.
        nightly_legs = (
            None
            if _sleep_pass_installed() is False
            else llm_client.leg_status(env=sleep_pass.scheduled_env(), memory_agent=True)
        )
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check("llm_provider_chain", "MISCONFIGURED", f"probe failed: {exc}", gate)

    parts = [_leg_part(leg) for leg in legs]
    memory_parts = [_leg_part(leg, f"{leg['leg']}[memory]") for leg in memory_legs or []]
    nightly_parts = [
        _leg_part(leg, f"{leg['leg']}[sleep-pass]")
        for leg in nightly_legs or []
        if leg["configured"] or leg.get("memory_selected")
    ]
    detail = "; ".join(parts + memory_parts + nightly_parts)

    hub_usable = any(leg["reachable"] for leg in legs)
    memory_usable = memory_legs is None or any(leg["reachable"] for leg in memory_legs)
    # No sleep pass scheduled here: judge the hub chain alone (#1731).
    nightly_usable = (
        hub_usable if nightly_legs is None else any(leg["reachable"] for leg in nightly_legs)
    )
    if hub_usable and nightly_usable and memory_usable:
        return _check("llm_provider_chain", "LIVE", f"fallback chain usable — {detail}", gate)
    if hub_usable:
        broken = []
        if not nightly_usable:
            broken.append("the nightly sleep pass has no usable leg")
        if not memory_usable:
            broken.append(
                f"the hub memory route ({llm_client.MEMORY_PROVIDER_ENV}={memory_provider}) "
                "has no usable leg"
            )
        return _check(
            "llm_provider_chain",
            "DEGRADED",
            f"hub chain usable, but {' and '.join(broken)} — {detail}",
            gate,
        )
    if nightly_usable:
        return _check(
            "llm_provider_chain",
            "DEGRADED",
            f"nightly sleep pass usable, but every hub leg is down — {detail}",
            gate,
        )
    if any(leg["explicit"] for leg in legs + (memory_legs or []) + (nightly_legs or [])):
        return _check(
            "llm_provider_chain",
            "MISCONFIGURED",
            f"every configured leg is unreachable — {detail}",
            gate,
        )
    return _check(
        "llm_provider_chain",
        "DORMANT",
        f"no leg opted in and the default CLI leg is unavailable — {detail}",
        gate,
    )


def _dispatch_scheduler_check() -> dict:
    """Describe host retirement and effective gates using Studio's runtime contract."""
    from ..dispatch.routing import scheduler_runtime_state

    runtime = scheduler_runtime_state()
    state = runtime["status"]
    status = (
        "DORMANT"
        if state == "retired"
        else "MISCONFIGURED"
        if state.startswith("misconfigured_")
        else "UNKNOWN"
    )
    detail = (
        f"dispatch lane A retired by #1435; runtime={state}; "
        f"enabled={runtime['enabled']}; real_spawn={runtime['real_spawn']}; "
        f"mode={runtime['mode']}"
    )
    return _check("dispatch_scheduler", status, detail, "UA_DISPATCH_SCHEDULER_ENABLED")


def _connectors_check() -> list[dict]:
    """One row per registered ConnectorManifest (task #1204).

    Reports whether a connector's declared entry points resolve to real code,
    whether its named secrets are present (names only — never values), and
    whether it holds an active Horizon 2 registration. This is the same
    ``connectors``/``governed_servers`` state the Connectors pane reads via
    ``horizon2_package_list`` — doctor and pane never diverge because both
    read the registry, not a separate snapshot.
    """
    from .. import connectors as connector_manifests
    from .. import edition

    secret_env, hub_env_readable = service_probes.connector_secret_environment()

    checks: list[dict] = []
    # Initialize the store once per process, not once per doctor run: each
    # init_db() reruns the schema and post-init hooks in a write transaction,
    # and once the previous backfill pass finished it started every embedding
    # backfill scan again, so a polled doctor kept them running (task #1850).
    db_ready = False
    for name, manifest in sorted(connector_manifests.all_manifests().items()):
        gate = connector_manifests.describe_required_secrets(manifest)
        capability = f"connector:{name}"
        runtime_health: dict[str, str] | None = None
        try:
            connector_manifests.validate_entry_points_resolve(manifest)
        except Exception as exc:  # noqa: BLE001 - one connector must not abort doctor
            checks.append(
                _check(capability, "MISCONFIGURED", f"manifest entry points invalid: {exc}", gate)
            )
            continue

        secret_state = connector_manifests.secret_status(manifest, env=secret_env)
        if error := secret_state.get("error"):
            checks.append(_check(capability, "UNKNOWN", error, gate))
            continue
        try:
            if not db_ready:
                post_init.register_default_hooks()
                storage.init_db_once()
                db_ready = True
            conn = storage.connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM governed_servers WHERE connector=? AND revoked_at IS NULL "
                    "AND org_id IS ? LIMIT 1",
                    (name, edition.tenant_org_id()),
                ).fetchone()
                registered_active = row is not None
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - doctor never raises
            checks.append(
                _check(
                    capability,
                    "UNKNOWN",
                    f"cannot determine active registration: {exc}",
                    gate,
                )
            )
            continue

        if not manifest.horizon2_registration_supported:
            detail = (
                "manifest has no Horizon 2 transport; active registration row is ignored"
                if registered_active
                else "manifest has no Horizon 2 transport; registration cannot make it live"
            )
            checks.append(_check(capability, "DORMANT", detail, gate))
            continue

        if name == "int-gcp-agent20" and registered_active:
            # An approval-gated operation is evidence of review, not evidence
            # that GCP inventory is readable or the configured cloud worker is running.
            # Probe only after explicit registration opts this connector in;
            # a fresh install with no GCP setup remains correctly DORMANT.
            from .gcloud import gcp_Agent20_health

            runtime_health = gcp_Agent20_health()
            if runtime_health["status"] in {"DEGRADED", "UNKNOWN"}:
                status = runtime_health["status"]
                checks.append(
                    {
                        **_check(
                            capability,
                            status,
                            f"GCP runtime health {status}: {runtime_health['detail']}",
                            gate,
                        ),
                        "health": status,
                    }
                )
                continue

        secrets_unknown = (
            not hub_env_readable
            and bool(manifest.required_secrets or manifest.required_secret_alternatives)
            and bool(secret_state["missing"])
        )

        if not secret_state["satisfied"]:
            status = "UNKNOWN" if registered_active and secrets_unknown else "DORMANT"
            if registered_active and secrets_unknown:
                detail = (
                    "cannot determine hub service secret configuration; "
                    "doctor process is missing: "
                    f"{', '.join(secret_state['missing'])}"
                )
            else:
                detail = f"missing required secret(s): {', '.join(secret_state['missing'])}"
            if registered_active and not secrets_unknown:
                status = "MISCONFIGURED"
            check = _check(capability, status, detail, gate)
            if runtime_health is not None:
                check["health"] = runtime_health["status"]
            checks.append(check)
        elif not registered_active:
            check = _check(
                capability,
                "DORMANT",
                "secrets present but no active Horizon 2 registration",
                gate,
            )
            if runtime_health is not None:
                check["health"] = runtime_health["status"]
            checks.append(check)
        else:
            check = _check(
                capability,
                "LIVE",
                f"registered, secrets present; capabilities: "
                f"{', '.join(sorted(manifest.capabilities))}",
                gate,
            )
            if runtime_health is not None:
                check["health"] = runtime_health["status"]
            checks.append(check)
    return checks


def _tool_classification_drift_check() -> dict:
    """Verify tool allowlist classification has zero unclassified or stale tools (task #1200)."""
    from .. import tool_classification

    gate = "tool_classification.py classification sets"
    try:
        drift = tool_classification.check_classification_drift()
        if not drift.passed:
            detail = "; ".join(drift.errors)
            return _check(
                "tool_classification",
                "MISCONFIGURED",
                f"tool classification drift detected: {detail}",
                gate,
            )

        report = tool_classification.classify_all()
        detail = (
            f"0 drift across {report.served_count} served tools: "
            f"{report.reachable_count} REACHABLE, {report.withheld_count} WITHHELD, {report.omitted_count} OMITTED"
        )
        return _check("tool_classification", "LIVE", detail, gate)
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check(
            "tool_classification",
            "MISCONFIGURED",
            f"tool classification check failed: {exc}",
            gate,
        )


def _github_approval_backlog_check() -> dict:
    """DEGRADED when an approved GitHub pass sits unconsumed past one poll.

    A zero count only means something while the poller runs: during #1783
    pr-watch exited 1 on every run, no ledger rows were created, and this row
    still said LIVE. It now also requires a healthy pr-watch run record.
    """
    from .. import github_app, github_app_worker, pr_watch

    gate = "com.unified-agent.pr-watch StartInterval"
    try:
        if not github_app.enabled():
            return _check(
                "github_approval_backlog",
                "DORMANT",
                "GitHub connector is off; unconsumed passes are not polled",
                gate,
            )
        # Task #1804: the same rows approval_pending reads, split by age, so a
        # LIVE row can say how many fresh passes its approved_unconsumed holds.
        passes = github_app_worker.unconsumed_github_passes()
        # The connector poll's own record (task #2486): a journal repository
        # that was skipped does not stop pull requests reaching the ledger,
        # and pr_watch_job reports it.
        poller = pr_watch.connector_health()
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check("github_approval_backlog", "UNKNOWN", f"backlog probe failed: {exc}", gate)
    count = passes["stale"] + passes["expired"]
    if count:
        return _check(
            "github_approval_backlog",
            "DEGRADED",
            f"{count} approved GitHub pass(es) unconsumed longer than one poll interval"
            + (f" ({passes['expired']} past their TTL)" if passes["expired"] else ""),
            gate,
        )
    fresh = (
        f"{passes['fresh']} fresh unconsumed GitHub pass(es) (none stale)"
        if passes["fresh"]
        else "no stale unconsumed GitHub passes"
    )
    if passes.get("unreachable"):
        # Task #2616: a pass for a PR that merged or moved on cannot be spent;
        # the orphan sweep retires it at its TTL, so it is not a backlog.
        fresh += (
            f"; {passes['unreachable']} pass(es) for closed or superseded PR heads "
            "await retirement at their TTL"
        )
    if poller["status"] is None:
        return _check(
            "github_approval_backlog",
            "UNKNOWN",
            f"{fresh}, but the poller is unverified: {poller['detail']}",
            gate,
        )
    if poller["status"] != "LIVE":
        return _check(
            "github_approval_backlog",
            "DEGRADED" if poller["status"] == "DEGRADED" else "UNKNOWN",
            f"{fresh} on record, but the pr-watch poller is not running cleanly, "
            f"so new PRs are not reaching the ledger: {poller['detail']}",
            gate,
        )
    return _check("github_approval_backlog", "LIVE", fresh, gate)


def _pr_watch_job_check() -> dict:
    """The pr-watch job's own last run, from the record it writes (task #1783).

    Read from ``$UA_HOME/pr-watch-status.json`` rather than ``launchctl``: the
    job records each scheduled run's exit and its last success, so a job that
    fails every run, or stopped firing, reads DEGRADED.
    """
    from .. import github_app, pr_watch, required_settings

    gate = f"com.unified-agent.pr-watch (StartInterval) -> $UA_HOME/{pr_watch.STATUS_FILE}"
    try:
        health = pr_watch.run_health()
        if health["status"] is None:
            if required_settings.PR_WATCH_JOB in _installed_scheduled_jobs():
                # The journal runs even with the connector off; unverified, not dormant.
                return _check(
                    "pr_watch_job",
                    "UNKNOWN",
                    f"{health['detail']}; the pr-watch LaunchAgent is installed but has not "
                    "recorded a run since this upgrade",
                    gate,
                )
            if github_app.enabled():
                return _check(
                    "pr_watch_job",
                    "UNKNOWN",
                    f"{health['detail']}; the Approval Ledger connector is on, so pr-watch "
                    "should be running (not installed here, or not run since this upgrade)",
                    gate,
                )
            return _check("pr_watch_job", "DORMANT", health["detail"], gate)
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check("pr_watch_job", "UNKNOWN", f"pr-watch status probe failed: {exc}", gate)
    return _check("pr_watch_job", health["status"], health["detail"], gate)


def _installed_scheduled_jobs() -> frozenset[str]:
    """Scheduled jobs present on this host, from files only (no launchctl)."""
    from .. import pr_watch, required_settings

    jobs: set[str] = set()
    plist = service_probes._mac_launchagent_plist("com.unified-agent.pr-watch")
    if (plist is not None and plist.is_file()) or pr_watch.status_path().exists():
        jobs.add(required_settings.PR_WATCH_JOB)
    return frozenset(jobs)


_PR_WATCH_UNIT = "com.unified-agent.pr-watch"


def _pr_watch_job_env() -> dict[str, str] | None:
    """The environment the scheduled pr-watch job runs with, or None when unreadable.

    The job's LaunchAgent ``EnvironmentVariables``, then the service.env its
    launcher sources and the launcher's literal exports, over the settings.toml
    values its own process applies. The hub's plist never reaches it.
    """
    from .. import settings

    plist = service_probes._mac_launchagent_plist(_PR_WATCH_UNIT)
    if plist is None or not plist.is_file():
        return None
    try:
        layers = service_probes._mac_launchagent_environment_layers(_PR_WATCH_UNIT)
    except Exception:  # noqa: BLE001 - a plist whose root is not a dict
        return None
    if layers is None or not layers[2]:
        return None
    env = dict(layers[0])
    for path in service_probes._mac_environment_files(_PR_WATCH_UNIT):
        if path.is_file():
            env.update(service_probes._load_env_file(path))
    env.update(layers[1])
    target = settings.settings_path(
        {"UA_HOME": str(service_probes._mac_home() / ".unified_agent"), **env}
    )
    try:
        values = settings.to_env(settings.load(target))[0] if target is not None else {}
    except Exception:  # noqa: BLE001 - a malformed file applies nothing, as at startup
        values = {}
    return {**values, **env}


def _required_settings_check() -> dict:
    """MISCONFIGURED when an enabled feature lacks a setting it needs (task #1783).

    Hub features read the hub service's environment layered over this process
    (which has settings.toml applied), matching ``_hub_setting``'s precedence.
    Features that run inside the pr-watch job are judged against that job's
    own environment when its LaunchAgent is readable.
    """
    from .. import required_settings

    gate = ", ".join(sorted({req.env for req in required_settings.REQUIREMENTS}))
    try:
        hub_env = service_probes._service_env(_SERVICE_UNITS["hub_service"]) or {}
        pr_watch_env = _pr_watch_job_env()
        result = required_settings.evaluate(
            {**os.environ, **hub_env},
            _installed_scheduled_jobs(),
            consumer_envs=(
                {required_settings.PR_WATCH_JOB: pr_watch_env} if pr_watch_env is not None else {}
            ),
        )
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check("required_settings", "UNKNOWN", f"settings probe failed: {exc}", gate)
    if result["unmet"]:
        return _check("required_settings", "MISCONFIGURED", "; ".join(result["unmet"]), gate)
    if not result["enabled"]:
        return _check(
            "required_settings",
            "DORMANT",
            "no enabled feature on this host depends on a removable setting",
            gate,
        )
    return _check(
        "required_settings",
        "LIVE",
        f"required settings present for: {', '.join(result['enabled'])}",
        gate,
    )


def _hub_checkout_drift_check() -> dict:
    """DEGRADED when the hub's checkout HEAD moved after the hub started (task #1771)."""
    from .. import hub_runtime

    gate = f"$UA_HOME/{hub_runtime.START_FILE} vs checkout HEAD"
    try:
        record = hub_runtime.read_hub_start()
        if record is None:
            return _check(
                "hub_checkout_drift",
                "DORMANT",
                "no hub start recorded on this host (no HTTP hub here, or it has not "
                "restarted since this check shipped)",
                gate,
            )
        started_sha = record.get("sha")
        checkout = str(record.get("checkout") or "")
        if not hub_runtime.pid_alive(record.get("pid")):
            return _check(
                "hub_checkout_drift",
                "UNKNOWN",
                f"recorded hub process {record.get('pid')} is not running; "
                "cannot compare its code with the checkout",
                gate,
            )
        if not started_sha or not checkout:
            return _check(
                "hub_checkout_drift",
                "UNKNOWN",
                "the hub did not start from a readable git checkout",
                gate,
            )
        head = hub_runtime.checkout_head(Path(checkout))
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check("hub_checkout_drift", "UNKNOWN", f"checkout probe failed: {exc}", gate)
    if head is None:
        return _check(
            "hub_checkout_drift",
            "UNKNOWN",
            f"cannot read HEAD of {checkout}; running hub started at {started_sha[:12]}",
            gate,
        )
    if head != started_sha:
        return _check(
            "hub_checkout_drift",
            "DEGRADED",
            f"checkout moved without a deploy/restart: the running hub started at "
            f"{started_sha[:12]} ({record.get('started_at')}) but {checkout} HEAD is now "
            f"{head[:12]}; lazily imported modules would load the newer code. Restart the "
            "hub through the deploy path, or return the checkout to the running SHA",
            gate,
        )
    return _check(
        "hub_checkout_drift",
        "LIVE",
        f"running hub code matches checkout HEAD {head[:12]}",
        gate,
    )


def _split_task_worktree(path: Path) -> tuple[Path, Path] | None:
    """(main checkout, worktree root) when ``path`` lies in a per-task worktree.

    Per-task worktrees live at ``<repo>/.worktrees/<name>`` (task claims) or
    ``<repo>/.claude/worktrees/<name>`` (Claude Code agents).
    """
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".worktrees":
            return Path(*parts[:i]), Path(*parts[: i + 2])
        if i > 0 and parts[i] == "worktrees" and parts[i - 1] == ".claude":
            return Path(*parts[: i - 1]), Path(*parts[: i + 2])
    return None


def _finder_mapping(finder: Path) -> list[str]:
    """Package paths in a setuptools editable finder's ``MAPPING`` literal."""
    import ast

    try:
        tree = ast.parse(finder.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return []
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "MAPPING" and node.value is not None:
            try:
                mapping = ast.literal_eval(node.value)
            except ValueError:
                return []
            if isinstance(mapping, dict):
                return [v for v in mapping.values() if isinstance(v, str)]
    return []


def _editable_pointers(venv: Path) -> list[tuple[Path, str, Path]]:
    """(file, entry, resolved target) for each unified_agent editable pointer in ``venv``.

    Two layouts: a path-line ``*unified_agent*.pth`` (hatchling, uv), whose
    relative entries resolve from its site-packages; and a setuptools import
    hook, whose target is the ``MAPPING`` in ``__editable__*unified_agent*finder.py``.
    Targets are resolved, so a symlinked parent cannot hide where they lead.
    """
    found = []
    sites = [*venv.glob("lib/python*/site-packages"), *venv.glob("Lib/site-packages")]
    for site in sorted(sites):
        for pth in sorted(site.glob("*unified_agent*.pth")):
            try:
                lines = pth.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                entry = line.strip()
                if entry and not entry.startswith(("#", "import ", "import\t")):
                    found.append((pth, entry, (site / entry).resolve()))
        for finder in sorted(site.glob("__editable__*unified_agent*finder.py")):
            for entry in _finder_mapping(finder):
                found.append((finder, entry, (site / entry).resolve()))
    return found


def _package_checkout_check(checkout: Path | None = None, prefix: Path | None = None) -> dict:
    """DEGRADED when the shared venv runs a per-task worktree's code (task #1944).

    The desktop's shared ``.venv`` editable pointer was once left aimed at an
    earlier session's ``.worktrees/task-*`` tree, so every service importing
    through it ran that stale tree whatever main said. Two readings, from
    files only: this process importing ``unified_agent`` from a task worktree
    while its interpreter lives outside that worktree, and any editable
    pointer in the main checkout's ``.venv`` that does not resolve inside the
    main checkout's ``src`` (a task worktree, an old clone, a renamed tree).
    A worktree running its own venv is fine. A non-editable install has no
    pointer and is not judged here.
    """
    from .. import hub_runtime

    gate = "shared .venv editable install (*unified_agent*.pth / finder) vs the main checkout"
    try:
        checkout = (checkout or hub_runtime.package_checkout()).resolve()
        prefix = (prefix or Path(sys.prefix)).resolve()
        split = _split_task_worktree(checkout)
        main = split[0] if split else checkout
        problems = []
        if split is not None and not prefix.is_relative_to(split[1]):
            problems.append(
                f"this process (interpreter {prefix}) imports unified_agent from "
                f"per-task worktree {checkout}"
            )
        venv = main / ".venv"
        main_src = (main / "src").resolve()
        for source, entry, target in _editable_pointers(venv):
            if not target.is_relative_to(main_src):
                shown = entry if str(target) == entry else f"{entry} (resolves to {target})"
                problems.append(f"{venv}/…/{source.name} points at {shown}")
    except Exception as exc:  # noqa: BLE001 - a doctor probe never raises
        return _check("package_checkout", "UNKNOWN", f"package path probe failed: {exc}", gate)
    if problems:
        return _check(
            "package_checkout",
            "DEGRADED",
            "; ".join(problems) + " — services using that venv run code from outside the "
            f"main checkout. Repoint the editable install at {main / 'src'} (`uv sync` in {main}) "
            "and restart the services that import it",
            gate,
        )
    where = " (a per-task worktree with its own venv)" if split else ""
    return _check(
        "package_checkout", "LIVE", f"unified_agent imported from {checkout}{where}", gate
    )


def _execution_locality_check() -> dict:
    """Where agent work may execute: on the hub host or only on devices (task #2109)."""
    from .. import execution_locality as el

    gate = el.ENV
    try:
        env = {}
        for name in (el.ENV, "RAILWAY_ENVIRONMENT_ID", "UA_HOSTED", "UA_HOME"):
            value, _ = _hub_posture_setting(name)
            if value is not None:
                env[name] = value
        if el.ENV not in env:
            # The hub seeds unset UA_* values from its settings.toml [env] at
            # import (config -> settings), so a value there decides its
            # locality just as one in the unit would (Codex review, #1234).
            from .. import settings as settings_mod

            settings_file, determined = _hub_posture_setting("UA_SETTINGS_FILE")
            if settings_file is not None and determined:
                env["UA_SETTINGS_FILE"] = settings_file
            settings_mod.apply_to_environ(env, quiet=True)
        try:
            el.validate(env)
        except ValueError as exc:
            return _check(
                "execution_locality",
                "MISCONFIGURED",
                f"{exc}; the hub refuses to start, and other processes fail closed to devices",
                gate,
            )
        if el.devices_only(env):
            present = sorted(b for b in el.HARNESS_BINARIES if shutil.which(b))
            detail = (
                f"devices only ({el.source(env)}): dispatch routes to ua-node devices with no "
                "local fallback; agent spawns, pi_run_task, the claude_cli leg, agy, the "
                "executor, `unified-agent node` and code-running run_cmd are refused, and the "
                "hub installs an audit-hook backstop at start"
            )
            if present:
                detail += f"; harness binaries on PATH (refused): {', '.join(present)}"
            return _check("execution_locality", "LIVE", detail, gate)
        return _check(
            "execution_locality",
            "DORMANT",
            f"local ({el.source(env)}): agent work may execute on this host",
            gate,
        )
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check("execution_locality", "UNKNOWN", f"probe failed: {exc}", gate)


def _hosted_local_dependencies_check() -> dict:
    """Workspace, vault, and notification-bridge presence (task #1442).

    Reports the capability consequence of a hostless tree. Does not enable
    a deployment mode.
    """
    try:
        from ..hosted_capabilities import doctor_check

        return doctor_check()
    except Exception as exc:  # noqa: BLE001 - doctor must never abort
        return _check(
            "hosted_local_dependencies",
            "UNKNOWN",
            f"hosted dependency probe failed: {exc}",
            "UA_WORKSPACE, UA_VAULT_PATH, UA_BRIDGE_DIR",
        )


def _trust_presets_check() -> dict:
    gate = "policy.yaml trust_presets"
    try:
        from .. import trust_presets

        state = trust_presets.describe_configuration()
    except Exception as exc:  # noqa: BLE001 - doctor must report invalid policy
        return _check("trust_presets", "MISCONFIGURED", f"invalid preset policy: {exc}", gate)
    if not state["configured"]:
        return _check("trust_presets", "DORMANT", "no trust presets configured", gate)
    names = ", ".join(state["presets"])
    return _check(
        "trust_presets",
        "LIVE",
        f"tightening-only mediated presets active ({names}); bound native launches denied "
        "until verified provider containment is available (#1355)",
        gate,
    )


# One shared report (task #1850). After a hub restart the Control Center and
# other pollers ran the doctor back to back, each run probing systemctl, the
# executor, a KNN query and every LLM leg. Holds (monotonic finish, ISO
# finish, report, run seconds) of the last good run.
#
# Stale-while-revalidate (task #1897): a 60s Control Center poll against a 20s
# TTL re-ran every probe on every poll, and a run averaged 14s of hub CPU. Now
# a report younger than UA_DOCTOR_CACHE_S is served as is; one younger than
# UA_DOCTOR_MAX_STALE_S is served at once while exactly one background run
# replaces it; only a missing report, one past the max-stale bound, or
# refresh=True makes the caller wait for a run.
_report_cache: tuple[float, str, dict, float] | None = None
# Guards every global below; held only for reads and assignments, never across
# a run.
_report_lock = threading.Lock()
# Bumped by clear_report_cache, so a run that started before a clear (a test,
# usually) cannot store its result afterwards.
_report_generation = 0
# (ISO time, message) of the last failed background refresh. Cleared by the
# next good run; reported beside the last good report until then.
_refresh_error: tuple[str, str] | None = None
# True while the current thread executes a refresh=True run. Probes that keep
# their own shared result (the worktree survey) read it to take a new reading
# instead of reusing one (Devin, #1044). Set only around that one run, in the
# thread executing it, so it never reaches a concurrent caller's run.
_forced_run: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "system_doctor_forced_run", default=False
)


class _DoctorRun:
    """One run in progress. Callers that arrive meanwhile and need a run wait on
    ``done`` and share its result, whatever the TTL: single-flight is separate
    from reuse after completion, which ``UA_DOCTOR_CACHE_S=0`` turns off
    (Devin, #1016)."""

    def __init__(self, *, background: bool, generation: int, forced: bool = False) -> None:
        self.done = threading.Event()
        self.entry: tuple[float, str, dict, float] | None = None
        self.error: BaseException | None = None
        self.background = background
        self.generation = generation
        # refresh=True: every probe re-reads, shared probe results included.
        self.forced = forced


_report_inflight: _DoctorRun | None = None
# Longest a caller waits on someone else's run. A run takes seconds (36 s was
# the worst seen on the hub); without a bound, one run that never finished
# (a thread that failed to start, a hung probe) would block every later caller
# and pin a worker thread each (review, #1044).
RUN_WAIT_TIMEOUT_S = 300.0
# The forced run queued behind the run in flight. A run already going began
# before a refresh=True caller asked, so its rows (the worktree row above all)
# may predate whatever the operator just changed; forced callers that arrive
# meanwhile coalesce onto this one run, which starts when that run finishes.
_report_pending_forced: _DoctorRun | None = None


def _max_stale_s(ttl: float) -> float:
    """The stale-serving bound in force, or 0 when stale serving is off."""
    if ttl <= 0:
        return 0.0
    bound = config.doctor_max_stale_s()
    return bound if bound > ttl else 0.0


def _cached_report(
    entry: tuple[float, str, dict, float],
    *,
    cached: bool,
    refreshing: bool = False,
    refresh_error: tuple[str, str] | None = None,
) -> dict:
    out = copy.deepcopy(entry[2])
    ttl = config.doctor_cache_ttl_s()
    age = round(time.monotonic() - entry[0], 1) if cached else 0.0
    out["cached"] = cached
    out["cached_at"] = entry[1]
    out["cache_age_s"] = age
    out["cache_ttl_s"] = ttl
    out["cache_max_stale_s"] = _max_stale_s(ttl)
    # Past the fresh TTL: still within the max-stale bound, or it would not be
    # served, but no longer what a run would say now.
    out["stale"] = bool(cached and ttl > 0 and age >= ttl)
    out["refreshing"] = refreshing
    out["run_duration_s"] = round(entry[3], 2)
    if refresh_error is not None:
        out["refresh_error"] = {"at": refresh_error[0], "error": refresh_error[1]}
    return out


def clear_report_cache() -> None:
    """Forget the shared report so the next call runs every probe (tests)."""
    global _report_cache, _report_inflight, _report_generation, _refresh_error
    global _report_pending_forced
    with _report_lock:
        _report_cache = None
        _report_inflight = None
        _report_pending_forced = None
        _refresh_error = None
        _report_generation += 1


def wait_for_report_refresh(timeout: float | None = None) -> bool:
    """Block until the run in progress, if any, finishes (tests, diagnostics).

    Returns False only when ``timeout`` expired with the run still going.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        with _report_lock:
            run = _report_pending_forced or _report_inflight
        if run is None:
            return True
        left = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not run.done.wait(left):
            return False


def _execute_run(run: _DoctorRun) -> None:
    """Run every probe for ``run``, publish its result, and wake its waiters.

    A failed run never replaces the last good report. A failed background run
    is recorded in ``_refresh_error`` so the reports served meanwhile say why
    they are not getting younger.
    """
    global _report_cache, _report_inflight, _refresh_error
    started = time.monotonic()
    token = _forced_run.set(run.forced)
    # One systemctl reading per unit property for the whole run (task #1905).
    memo_token = service_probes._unit_property_memo.set({})
    try:
        if run.forced:
            # A forced run re-probes the LLM legs, so an operator who just fixed
            # one sees it at once. Cleared here, as this run starts, not when the
            # caller asked: a run it waited out could have refilled the cache
            # meanwhile. Inside the try, so a raise still frees the slot (#1923).
            from .. import llm_client

            llm_client.clear_probe_cache()
        report = _run_system_doctor()
        run.entry = (
            time.monotonic(),
            datetime.now(UTC).isoformat(),
            report,
            time.monotonic() - started,
        )
    except BaseException as exc:
        run.error = exc
    finally:
        service_probes._unit_property_memo.reset(memo_token)
        _forced_run.reset(token)
        with _report_lock:
            current = _report_cache
            # A forced run may overlap a background run that started earlier and
            # finishes later; the older reading must not replace the newer one.
            # An entry's start is its finish minus its duration.
            newer = current is None or started >= current[0] - current[3]
            # A forced run can displace a hung predecessor after the wait
            # bound. That predecessor still wakes its own waiters, but may
            # neither publish a reading nor alter the newer refresh error.
            if run.generation == _report_generation and _report_inflight is run:
                if run.entry is not None:
                    if config.doctor_cache_ttl_s() > 0 and newer:
                        _report_cache = run.entry
                    _refresh_error = None
                elif run.background and run.error is not None:
                    message = f"{type(run.error).__name__}: {run.error}"
                    _refresh_error = (datetime.now(UTC).isoformat(), message[:300])
            if _report_inflight is run:
                # Hand the slot straight to a queued forced run, so a caller
                # arriving in between joins it instead of starting another.
                _report_inflight = _report_pending_forced
        run.done.set()


def _background_refresh(run: _DoctorRun) -> None:
    _execute_run(run)
    if run.error is not None:
        logger.warning(
            "system_doctor background refresh failed; serving the last good report: %s",
            run.error,
        )


def system_doctor(refresh: bool = False) -> dict:
    """Report LIVE/DORMANT/DEGRADED/MISCONFIGURED/UNKNOWN capability health.

    The report is shared (task #1850, stale-while-revalidate since #1897): a
    report younger than ``UA_DOCTOR_CACHE_S`` (default 120) is served as is;
    one younger than ``UA_DOCTOR_MAX_STALE_S`` (default 600) is served at once
    with ``refreshing: true`` while exactly one background run replaces it.
    Only a missing report, one past the max-stale bound, or ``refresh=True``
    waits for a run (and a failed run raises rather than serving an older
    report), so the stale path never serves past the bound; the
    ``stale_worktrees`` row may reflect a survey up to
    ``UA_WORKTREE_SURVEY_CACHE_S`` older and names its time. A reused
    report says so: ``cached`` is true, ``cached_at`` is when its run finished,
    ``cache_age_s`` how long ago, and ``stale`` whether that is past the fresh
    TTL. When the last background run failed, ``refresh_error`` carries when
    and why, beside the last good report. Pass ``refresh=True`` to run every
    probe now, the shared worktree survey included. A forced caller never
    shares a run that began before it asked: forced callers that arrive while a
    run is going share the one forced run queued to start after it.

    Checks: hub auth (UA_AGENT_TOKENS/UA_ENFORCE_AUTH), agent-token lifecycle
    age against UA_TOKEN_MAX_AGE_DAYS (task #1198 — metadata-only, never a
    plaintext value), the global approval queue (UA_APPROVAL_TOOLS), which
    web-search provider is actually
    resolving (UA_SEARCH_PROVIDER/BRAVE_API_KEY/TAVILY_API_KEY), per-agent
    hub workspace bindings (UA_AGENT_WORKSPACES), whether service.env pins the
    hub sandbox root (``workspace_pin``, #1806), the automation timers this
    repo installs (anomaly check, weekly consolidate proposal, nightly sleep
    pass, review pipeline, steward audit, cluster briefing — deploy/systemd/*),
    the hub service itself, the Active Executor daemon's ability to actually
    authenticate to the hub (not merely to be running),
    installed-unit-vs-repo-template drift (directives shipped in deploy/
    templates but missing from the installed ~/.config/systemd/user units —
    Environment values are compared by name only, so local settings never
    flag), worktrees that hold no work and can be removed (surveyed read-only
    across the workspace and the UA_AUDIT_ROOTS read surface, since worktrees
    live in the development checkout rather than the workspace),
    every registered ConnectorManifest (task #1204: entry points resolve,
    named secrets present, active Horizon 2 registration — one row per
    connector, same registry the Connectors pane reads; GCP also reports a
    separate read-only runtime health status),
    the optional pdf extra and required tree-sitter-language-pack, the llm_client fallback chain's
    per-leg health (config presence plus unbilled metadata GETs — never a
    generation call, so the report itself spends nothing), and     the latest
    integrity-checked complete SQLite backup. ``hosted_local_dependencies``
    (task #1442) names workspace, vault, and notification-bridge families
    that are missing on a hostless tree; it does not enable a deployment
    mode. The backup check is read-only
    and does not schedule or create a snapshot. ``vault_sync`` (task #1288)
    reports the hub -> desktop vault pull's freshness against a 3x-interval
    staleness budget and its outstanding conflict count from the last run's
    state file, LIVE only when both are clean; DORMANT on a host that has
    never installed ua-vault-sync.timer (the Mac hub itself, by design).

    Each finding carries the env var or systemd unit that gates it, so a
    DORMANT result is directly actionable. Never raises: an individual probe
    failure surfaces as MISCONFIGURED/DORMANT/DEGRADED/UNKNOWN on that one check rather
    than aborting the whole report.

    Returns {checks: [{capability, status, detail, gate}],
    summary: {live, dormant, degraded, misconfigured, unknown},
    host, as_of, hosts: [...]} — ``hosts`` is the hub's aggregated fleet
    view. A host row is a doctor snapshot or a registered ua-node. Agent labels
    and attribution strings are not host rows. Remote doctor / executor /
    worktree fields stay unreported unless that host submitted a snapshot; they
    are never inferred from last-seen.
    """
    global _report_inflight, _report_pending_forced, _refresh_error
    ttl = config.doctor_cache_ttl_s()
    max_stale = _max_stale_s(ttl)
    start_background: _DoctorRun | None = None
    prior: _DoctorRun | None = None
    with _report_lock:
        entry = _report_cache
        age = time.monotonic() - entry[0] if entry is not None else None
        run = _report_inflight
        if not refresh and entry is not None and ttl > 0 and age < max(ttl, max_stale):
            if age >= ttl and run is None:
                # Stale but within bound: one background run replaces it.
                run = _report_inflight = _DoctorRun(background=True, generation=_report_generation)
                start_background = run
            serve = (entry, run is not None, _refresh_error)
        else:
            serve = None
            if run is None:
                owner = True
                run = _report_inflight = _DoctorRun(
                    background=False, generation=_report_generation, forced=refresh
                )
            elif refresh:
                # The run in flight began before this force, so its rows may be
                # older than what the caller just changed. Queue (or join) the
                # one forced run that starts after it (review, #1044).
                owner = _report_pending_forced is None
                if owner:
                    prior = run
                    _report_pending_forced = _DoctorRun(
                        background=False, generation=_report_generation, forced=True
                    )
                run = _report_pending_forced
            else:
                owner = False
    if serve is not None:
        served, refreshing, error = serve
        if start_background is not None:
            try:
                # The run sees the caller's context, as a foreground run does
                # (workspace root, identity); a bare Thread would start empty.
                context = contextvars.copy_context()
                threading.Thread(
                    target=context.run,
                    args=(_background_refresh, start_background),
                    name="system-doctor-refresh",
                    daemon=True,
                ).start()
            except BaseException as exc:  # noqa: BLE001 - e.g. "can't start new thread"
                # Never leave a run in the slot that nobody executes: every later
                # caller would report refreshing forever, then wait on it.
                logger.warning("system_doctor background refresh did not start: %s", exc)
                with _report_lock:
                    start_background.error = exc
                    if _report_inflight is start_background:
                        _report_inflight = _report_pending_forced
                    if start_background.generation == _report_generation:
                        message = f"background refresh did not start: {type(exc).__name__}: {exc}"
                        _refresh_error = (datetime.now(UTC).isoformat(), message[:300])
                    error = _refresh_error
                start_background.done.set()
                refreshing = False
        return _cached_report(served, cached=True, refreshing=refreshing, refresh_error=error)
    if not owner:
        # Share the run that will answer this caller rather than start another,
        # even with a zero TTL: the run in flight for an unforced caller, or the
        # queued forced run for a forced one.
        if not run.done.wait(RUN_WAIT_TIMEOUT_S):
            raise TimeoutError(
                f"system_doctor: the shared run did not finish within {RUN_WAIT_TIMEOUT_S:.0f}s"
            )
        if run.error is not None:
            raise run.error
        return _cached_report(run.entry, cached=True)
    if prior is not None:
        if not prior.done.wait(RUN_WAIT_TIMEOUT_S):
            logger.warning(
                "system_doctor: the run in flight did not finish within %.0fs; "
                "starting the forced run anyway",
                RUN_WAIT_TIMEOUT_S,
            )
        with _report_lock:
            if _report_pending_forced is run:
                _report_pending_forced = None
            if run.generation == _report_generation and (
                _report_inflight is None or _report_inflight is prior
            ):
                # Timeout recovery must take the hung predecessor's slot.
                # Compare under the lock so a newer run is never displaced.
                _report_inflight = run
    _execute_run(run)
    if run.error is not None:
        raise run.error
    return _cached_report(run.entry, cached=False)


def _run_system_doctor() -> dict:
    """Run every check once; ``system_doctor`` decides when a run is needed."""
    checks = [
        _hub_auth_check(),
        _agent_token_collisions_check(),
        _deployment_mode_check(),
        _agent_token_lifecycle_check(),
        _retired_agent_credentials_check(),
        _operator_identity_check(),
        _tenant_consistency_check(),
        _mcp_oauth_check(),
        _oauth_store_check(),
        _approval_gates_check(),
        _approval_push_check(),
        _approval_slack_cards_check(),
        _trust_presets_check(),
        _search_provider_check(),
        _per_agent_workspaces_check(),
        _workspace_pin_check(),
        _stale_task_locks_check(),
        _queue_backpressure_check(),
        _wip_policy_check(),
        _queue_closure_check(),
        _dispatch_unsettled_spawns_check(),
        _command_items_unsettled_check(),
        _session_owed_check(),
        _workflow_definitions_check(),
        _tui_dashboard_check(),
        _web_ui_check(),
        _event_bus_check(),
        _node_turn_index_check(),
        _lifecycle_capture_check(),
        _agent_wake_check(),
        _agent_label_drift_check(),
        _org_registry_check(),
        _stale_worktrees_check(),
        _database_backup_check(),
        _decision_ledger_check(),
        _vault_sync_check(),
        _fleet_vault_sync_check(),
        _fleet_nodes_check(),
        _fleet_node_versions_check(),
        _fleet_node_acceptance_check(),
        _replication_check(),
        _semantic_search_check(),
        _symbol_embedding_check(),
        _zulip_relay_check(),
        _zulip_chat_check(),
        _mcp_gateway_check(),
        *_connectors_check(),
        _github_approval_backlog_check(),
        _pr_watch_job_check(),
        _required_settings_check(),
        _hub_checkout_drift_check(),
        _package_checkout_check(),
        _ts_worker_check(),
        _llm_provider_chain_check(),
        _dispatch_scheduler_check(),
        _langgraph_engine_check(),
        _mcp2_runtime_telemetry_check(),
        _tool_classification_drift_check(),
        _hosted_local_dependencies_check(),
        _execution_locality_check(),
        _open_files_limit_check(),
    ]
    for capability, unit in _TIMER_UNITS.items():
        if _retirement_applies(capability):
            checks.append(_retired_unit_check(capability, unit))
        else:
            checks.append(_unit_check(capability, unit))
    for capability, unit in _SERVICE_UNITS.items():
        # The executor gets a capability check, not a liveness check: unit-active
        # alone gave a six-day false all-clear while every poll was 401'd (#174).
        if capability == "active_executor_service":
            checks.append(service_probes._active_executor_check())
        else:
            checks.append(_unit_check(capability, unit))
    checks.append(_unit_drift_check())
    for capability, (module, install_hint) in _EXTRAS.items():
        checks.append(_extra_check(capability, module, install_hint))

    summary = {
        "live": sum(1 for c in checks if c["status"] == "LIVE"),
        "dormant": sum(1 for c in checks if c["status"] == "DORMANT"),
        "degraded": sum(1 for c in checks if c["status"] == "DEGRADED"),
        "misconfigured": sum(1 for c in checks if c["status"] == "MISCONFIGURED"),
        "unknown": sum(1 for c in checks if c["status"] == "UNKNOWN"),
    }
    report: dict = {"checks": checks, "summary": summary}
    try:
        from .. import host_registry

        as_of = host_registry.record_local_doctor_snapshot(report)
        fleet = host_registry.fleet_payload()
        report["host"] = fleet.get("local_host") or config.current_source_host()
        report["as_of"] = as_of or storage.now()
        report["hosts"] = fleet.get("hosts") or []
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        report["host"] = config.current_source_host()
        report["as_of"] = storage.now()
        report["hosts"] = []
        report["hosts_error"] = f"host registry unavailable: {exc}"[:200]
    return report


class _ProbeForwardingModule(types.ModuleType):
    """This module, forwarding a patch of a moved probe name to ``service_probes``.

    The probes moved to ``service_probes`` (task #1722), where the executor, the
    delivery receipts and the Horizon 2 tools reach them without importing this
    adapter. Tests and callers patched them here for years
    (``monkeypatch.setattr(doctor, "_service_env", ...)``). Setting one of the
    ``DOCTOR_REEXPORTS`` names here sets it in ``service_probes`` too, so the
    moved code sees the patch, and restoring it here restores both. The other
    direction is ``service_probes.register_reexport_namespace`` below: a patch
    made on ``service_probes`` rebinds this module's copy as well.
    """

    def __setattr__(self, name: str, value) -> None:
        if name in service_probes.DOCTOR_REEXPORTS:
            setattr(service_probes, name, value)
        super().__setattr__(name, value)


service_probes.register_reexport_namespace(globals())
sys.modules[__name__].__class__ = _ProbeForwardingModule
