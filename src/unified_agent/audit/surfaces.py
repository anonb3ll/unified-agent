"""Scheduled static scan of our own agent config surfaces.

Reuses ``run_audit`` (no parallel rule engine) over allowlisted roots:
hub/workspace trees, host MCP registrations, hooks, skills/rules, and
secret/env handling. Read-only — never remediates, never mutates services.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .. import config
from . import baseline as audit_baseline
from .engine import run_audit
from .grade import gated, grade_from_summary, worst_grade
from .rules import SEVERITIES

DEFAULT_SURFACE_RELS = (
    "dev/agent",
    "dev/unified_agent",
    ".claude",
    ".gemini",
    ".hermes",
    ".unified_agent",
    ".cursor",
    ".codex",
)


class SurfaceAuditError(ValueError):
    """Root is outside the configured allowlist, or usage is invalid."""


def default_surface_roots(home: Path | None = None) -> list[Path]:
    home = Path(home or Path.home())
    return [home / rel for rel in DEFAULT_SURFACE_RELS]


def configured_allowlist(home: Path | None = None) -> list[Path]:
    """UA_AUDIT_ROOTS when set (replace-not-append); else default surface dirs."""
    extra = [p.expanduser().resolve() for p in config.audit_roots()]
    if extra:
        return extra
    return [p.resolve() for p in default_surface_roots(home) if p.exists()]


def _in_allowlist(resolved: Path, allowlist: list[Path]) -> bool:
    for root in allowlist:
        try:
            if resolved == root or resolved.is_relative_to(root):
                return True
        except ValueError:
            continue
    return False


def resolve_surface_root(path: str, allowlist: list[Path]) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not _in_allowlist(resolved, allowlist):
        listed = ", ".join(str(r) for r in allowlist) or "(empty allowlist)"
        raise SurfaceAuditError(
            f"path {path!r} is outside the auditable roots ({listed}). "
            "Pass --allowlist or set UA_AUDIT_ROOTS to include a parent."
        )
    if not resolved.is_dir():
        raise SurfaceAuditError(f"not a directory: {path}")
    return resolved


def _aggregate_summary(reports: list[dict]) -> dict:
    summary = {sev: 0 for sev in SEVERITIES}
    summary["excluded"] = 0
    for report in reports:
        src = report.get("summary") or {}
        for sev in SEVERITIES:
            summary[sev] += int(src.get(sev) or 0)
        summary["excluded"] += int(src.get("excluded") or 0)
    return summary


def run_surface_scan(
    roots: list[str] | None = None,
    *,
    fail_on: str = "high",
    excludes: list[str] | None = None,
    file_tasks: bool = False,
    dry_run: bool = False,
    allowlist: list[Path] | str | None = None,
    home: Path | None = None,
    baseline: audit_baseline.Baseline | Path | None = None,
) -> dict:
    """Scan allowlisted agent-surface roots. Never remediates.

    ``baseline`` applies the operator-reviewed baseline to every root (see
    ``audit.baseline``); the report then states how many findings it accepted.
    """
    if isinstance(baseline, (str, os.PathLike)):
        baseline = audit_baseline.load(Path(baseline))
    if isinstance(allowlist, (str, os.PathLike)):
        allowlist = [Path(allowlist)]
    allow = [Path(p).expanduser().resolve() for p in (allowlist or configured_allowlist(home))]
    if not allow:
        raise SurfaceAuditError(
            "no auditable roots: set UA_AUDIT_ROOTS or pass --allowlist / existing default dirs"
        )

    requested = list(roots) if roots else [str(p) for p in allow if p.is_dir()]
    resolved_roots = [resolve_surface_root(p, allow) for p in requested]

    reports: list[dict] = []
    all_findings: list[dict] = []
    files_scanned = 0
    truncated = False
    for root in resolved_roots:
        extra = {"baseline": baseline} if baseline is not None else {}
        report = run_audit(
            root, remediate=False, excludes=excludes, include_repo_rules=True, **extra
        )
        report["grade"] = grade_from_summary(report["summary"], truncated=report.get("truncated"))
        reports.append(report)
        files_scanned += int(report.get("files_scanned") or 0)
        truncated = truncated or bool(report.get("truncated"))
        for fd in report.get("findings") or ():
            item = dict(fd)
            item["root"] = report["root"]
            all_findings.append(item)

    summary = _aggregate_summary(reports)
    overall_grade = worst_grade([r["grade"] for r in reports] or ["A"])
    if truncated and overall_grade != "F":
        overall_grade = worst_grade([overall_grade, "E"])
    status = "fail" if summary["high"] else ("warn" if summary["medium"] else "pass")

    combined = {
        "grade": overall_grade,
        "status": status,
        "fail_on": fail_on,
        "files_scanned": files_scanned,
        "summary": summary,
        "findings": all_findings,
        "truncated": truncated,
        "roots": reports,
        "remediate": False,
    }
    if baseline is not None:
        combined["baseline"] = {
            "path": str(baseline.path),
            "entries": len(baseline.entries),
            "accepted": sum(int((r.get("baseline") or {}).get("accepted") or 0) for r in reports),
            "warning": baseline.warning,
        }

    if file_tasks:
        from .triage import file_findings_as_tasks

        filed = {"created": 0, "skipped": [], "tasks": [], "groups": 0, "dry_run": dry_run}
        for report in reports:
            part = file_findings_as_tasks(
                report, min_severity=fail_on if fail_on != "none" else "high", dry_run=dry_run
            )
            filed["created"] += part["created"]
            filed["skipped"].extend(part["skipped"])
            filed["tasks"].extend(part["tasks"])
            filed["groups"] += part["groups"]
        combined["tasks"] = filed
    return combined


def _format_text(report: dict) -> str:
    s = report["summary"]
    out = [
        f"surface-audit: grade {report['grade']}  status={report['status']}  "
        f"roots={len(report['roots'])}  files={report['files_scanned']}"
    ]
    for root_report in report["roots"]:
        loc = root_report["root"]
        rs = root_report["summary"]
        out.append(
            f"[{root_report['grade']}] {root_report['status'].upper():4} {loc}  "
            f"(high={rs['high']} medium={rs['medium']} low={rs['low']} info={rs['info']})"
        )
        for fd in root_report.get("findings") or ():
            place = f"{fd['path']}:{fd['line']}" if fd.get("line") else (fd.get("path") or "(repo)")
            out.append(f"         [{fd['severity'].upper():6}] {fd['rule']:24} {place}")
            out.append(f"                  {fd['message']}")
    accepted = ""
    if report.get("baseline"):
        accepted = f" accepted={report['baseline']['accepted']}"
    out.append(
        f"overall: grade {report['grade']}  "
        f"(high={s['high']} medium={s['medium']} low={s['low']} info={s['info']}{accepted})"
    )
    if (report.get("baseline") or {}).get("warning"):
        out.append(f"warning: {report['baseline']['warning']}")
    if report.get("tasks"):
        t = report["tasks"]
        out.append(
            f"tasks: created={t.get('created', 0)} skipped={len(t.get('skipped') or [])} "
            f"dry_run={t.get('dry_run', False)}"
        )
    return "\n".join(out)


def run_surfaces_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent-audit surfaces",
        description=(
            "Scan allowlisted agent config surfaces (hub, MCP, hooks, skills, env) "
            "with the existing audit engine. Read-only. Grade A–F; exit 1 on high."
        ),
    )
    parser.add_argument(
        "roots",
        nargs="*",
        help="directories to scan (default: configured allowlist that exists on disk)",
    )
    parser.add_argument("--json", action="store_true", help="emit the combined report as JSON")
    parser.add_argument(
        "--fail-on",
        choices=[*SEVERITIES, "none"],
        default="high",
        help="exit 1 if any finding is at least this severe (default: high)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="glob pattern of paths to exclude (repeatable)",
    )
    parser.add_argument(
        "--allowlist",
        action="append",
        default=[],
        help="auditable parent root (repeatable). Default: UA_AUDIT_ROOTS or built-in surfaces",
    )
    parser.add_argument(
        "--file-tasks",
        action="store_true",
        help="file high findings via existing [audit-triage:] task dedup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --file-tasks, report what would be filed without writing",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="ignore the operator baseline ($UA_HOME/audit-baseline.json)",
    )
    args = parser.parse_args(argv)

    allow = [Path(p) for p in args.allowlist] or None
    try:
        report = run_surface_scan(
            args.roots or None,
            fail_on=args.fail_on,
            excludes=args.exclude,
            file_tasks=args.file_tasks,
            dry_run=args.dry_run,
            allowlist=allow,
            baseline=None if args.no_baseline else audit_baseline.load(),
        )
    except SurfaceAuditError as exc:
        print(f"surface-audit: {exc}", file=sys.stderr)
        return 2

    if (report.get("baseline") or {}).get("warning"):
        print(report["baseline"]["warning"], file=sys.stderr)
    print(json.dumps(report, indent=2) if args.json else _format_text(report))
    return 1 if gated(report["findings"], args.fail_on) else 0
