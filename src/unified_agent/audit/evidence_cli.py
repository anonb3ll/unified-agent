"""CLI entry point for user-attributable agent evidence reports (task #781).

Traces the 5-link accountability chain:
    human -> device -> agent -> action -> proof

Task #844 adds `unified-agent evidence report --project <id>` to render the
existing project_chain (spec, nested steps, merged PRs, last merge, audit
verify) as one Markdown or JSON artifact.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, events, post_init, storage
from . import evidence_signing
from .evidence_report import (
    build_evidence_report,
    format_evidence_report,
    read_historical_report,
)


def _emit_report(report: dict, fmt: str, out: str | None) -> int:
    sidecar = None
    try:
        signing_material = (
            evidence_signing._key_material() if evidence_signing.configured() else None
        )
        export_report = (
            evidence_signing.with_report_metadata(report, key_material=signing_material)
            if signing_material is not None
            else report
        )
        output = format_evidence_report(export_report, fmt=fmt)
        if evidence_signing.configured():
            chain = report.get("chain_integrity") or {}
            sidecar = evidence_signing.sign_export(
                evidence_signing.encode_for_signing(output),
                chain_head=str(chain.get("head_hash") or ""),
                generated_at=str(report.get("generated_at") or ""),
                key_material=signing_material,
            )
    except (RuntimeError, ValueError) as exc:
        print(f"evidence-report: signing error: {exc}", file=sys.stderr)
        return 2
    if out:
        out_path = Path(out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(evidence_signing.encode_for_signing(output))
        print(f"evidence-report: saved {fmt} report to {out_path}")
        if sidecar:
            sig_path = evidence_signing.sidecar_path(out_path)
            evidence_signing.dump_sidecar(sig_path, sidecar)
            print(f"evidence-report: saved signature sidecar to {sig_path}")
    else:
        if isinstance(output, bytes):
            sys.stdout.buffer.write(output)
        else:
            print(output)

    chain = report.get("chain_integrity", {})
    return 0 if chain.get("verified", True) else 1


def _check_anchor(artifact: bytes) -> dict[str, Any]:
    """Check the `anchor` header of a JSON evidence export, if present (task #1265, F2).

    A non-JSON export (csv/markdown/text/pdf) only carries a one-line anchor
    summary, not the structured reference needed to re-verify — those report
    "not checked" rather than guessing at a lossy format. Absent anchor is
    reported as ``not_anchored``, not a failure; the signature check above
    remains the primary exit-code driver.
    """
    try:
        report = json.loads(artifact.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"code": 0, "message": "not checked (export is not JSON)"}
    if not isinstance(report, dict):
        return {"code": 0, "message": "not checked (export is not a JSON object)"}

    anchor_block = report.get("anchor")
    if not isinstance(anchor_block, dict) or anchor_block.get("status") != "anchored":
        return {"code": 0, "message": "not anchored"}

    ref = anchor_block.get("ref")
    chain_block = report.get("chain_integrity")
    backend = anchor_block.get("backend")
    later = anchor_block.get("entries_after_anchor")
    if (
        not isinstance(ref, dict)
        or not isinstance(chain_block, dict)
        or not isinstance(backend, str)
        or (
            later is not None
            and (not isinstance(later, int) or isinstance(later, bool) or later < 0)
        )
    ):
        return {"code": 2, "message": "malformed anchor or chain header"}
    # The witness covers the head it was taken over, which precedes the
    # report's own chain head (entries_after_anchor later entries hang off it
    # by the hash chain). Older exports without anchored_head fall back to the
    # report head.
    report_head = chain_block.get("head_hash")
    head_hash = anchor_block.get("anchored_head") or report_head
    if not isinstance(head_hash, str) or not isinstance(report_head, str):
        return {"code": 1, "message": "anchor or report head missing"}
    from . import anchor as chain_anchor

    try:
        result = chain_anchor.verify_anchor(ref, head_hash, backend=backend)
    except ToolError as exc:
        return {"code": 2, "message": f"configuration error: {exc}"}
    except Exception:  # noqa: BLE001 - malformed exports must not crash the CLI
        return {"code": 2, "message": "anchor verification could not complete"}

    if not result.get("verified"):
        return {
            "code": 1,
            "message": f"anchor verification failed: {result.get('reason', 'unknown')}",
        }
    if head_hash == report_head:
        return {"code": 0, "message": f"anchor verified ({result.get('reason', 'ok')})"}

    # An older witness does not, by itself, prove the report's newer head.
    # Verify the complete local ledger and the exact segment between the two
    # hashes when it is available. An offline export without that ledger can
    # verify the historical witness but must not claim current-head coverage.
    if not config.db_path().is_file():
        return {"code": 1, "message": "historical anchor verified; report-head link unavailable"}
    try:
        conn = storage.connect()
        try:
            chain = events.verify_event_chain(conn)
            positions = conn.execute(
                "SELECT ROWID, entry_hash FROM event_log WHERE entry_hash IN (?, ?)",
                (head_hash, report_head),
            ).fetchall()
            by_hash = {row["entry_hash"]: row["ROWID"] for row in positions}
            start, end = by_hash.get(head_hash), by_hash.get(report_head)
            count = (
                conn.execute(
                    "SELECT COUNT(*) FROM event_log WHERE ROWID > ? AND ROWID <= ? "
                    "AND entry_hash IS NOT NULL",
                    (start, end),
                ).fetchone()[0]
                if start is not None and end is not None and start < end
                else None
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - fail closed on unreadable local ledger
        return {
            "code": 1,
            "message": f"historical anchor verified; report-head link unreadable: {exc}",
        }
    if (
        not chain.get("verified")
        or count is None
        or count != anchor_block.get("entries_after_anchor")
    ):
        return {"code": 1, "message": "historical anchor verified; report-head link failed"}
    return {"code": 0, "message": f"anchor and local ledger link verified ({count} later entries)"}


def _check_attestation(artifact: bytes, sidecar: dict[str, Any] | None) -> dict[str, Any]:
    """Check the `attestation` header of a JSON evidence export, if present (task #1267, F4).

    The byte-level HMAC check above already fails closed on ANY tamper to
    this block once it ships inside a signed export — this adds two checks
    that a valid-but-wrong signature can't catch on its own:

    - Structural honesty (#1217 rule, extended to the report header): a
      `human` attestation claim must actually carry IdP evidence
      (`verified_via == "idp"` plus the WorkOS session claims). A block
      claiming human attestation without that evidence is rejected even if
      it happens to arrive unsigned or with a sidecar that was never checked.
    - Key id consistency: `attestation.key_id`, baked into the report body
      at build time, must match the key id actually used to sign the
      artifact (the sidecar's `key_id`) when both are present — they are
      supposed to come from the same `_key_material()` call.

    A non-JSON export (csv/markdown/text/pdf) only carries a one-line
    summary, not the structured block, so it reports "not checked" rather
    than guessing. A pre-1.5 export with no `attestation` block at all is
    reported as such, not a failure.
    """
    try:
        report = json.loads(artifact.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"code": 0, "message": "not checked (export is not JSON)"}
    if not isinstance(report, dict):
        return {"code": 0, "message": "not checked (export is not a JSON object)"}

    schema_version = report.get("schema_version")
    strict_schema = False
    if schema_version is not None:
        if not isinstance(schema_version, str) or not re.fullmatch(
            r"\d+(?:\.\d+)+", schema_version
        ):
            return {"code": 2, "message": "invalid evidence report schema_version"}
        strict_schema = tuple(int(part) for part in schema_version.split(".")) >= (1, 5)

    block = report.get("attestation")
    if not isinstance(block, dict):
        if strict_schema:
            return {"code": 2, "message": "schema 1.5+ export is missing its attestation block"}
        return {"code": 0, "message": "no attestation block (pre-1.5 export)"}

    attester_kind = block.get("attester_kind")
    human_attested = block.get("human_attested")
    if strict_schema:
        if attester_kind not in {"human", "agent-bearer-only", "unknown"}:
            return {"code": 2, "message": "invalid attestation attester_kind"}
        if human_attested is not (attester_kind == "human"):
            return {"code": 2, "message": "inconsistent attestation human_attested flag"}
        if attester_kind == "unknown" and (
            block.get("attester") is not None or block.get("verified_via") is not None
        ):
            return {"code": 2, "message": "inconsistent unknown attestation identity"}
        if attester_kind == "agent-bearer-only" and (
            not isinstance(block.get("attester"), str)
            or not block["attester"].strip()
            or block.get("verified_via") == "idp"
        ):
            return {"code": 2, "message": "inconsistent agent-bearer-only attestation identity"}
        if not isinstance(block.get("attested_at"), str) or not block["attested_at"].strip():
            return {"code": 2, "message": "schema 1.5+ attestation is missing attested_at"}
        if not isinstance(block.get("scope"), dict) or block.get("scope") != report.get("filter"):
            return {
                "code": 2,
                "message": "schema 1.5+ attestation scope does not match report filter",
            }
        if block.get("attested_at") != report.get("generated_at"):
            return {
                "code": 2,
                "message": "attestation timestamp does not match report generated_at",
            }
        if block.get("key_id") is not None and (
            not isinstance(block.get("key_id"), str) or not block["key_id"].strip()
        ):
            return {"code": 2, "message": "invalid attestation key_id"}

    if attester_kind == "human":
        verified_via = block.get("verified_via")
        subject = block.get("workos_subject")
        organization_id = block.get("workos_organization_id")
        if (
            verified_via != "idp"
            or not isinstance(subject, str)
            or not subject.strip()
            or not isinstance(organization_id, str)
            or not organization_id.strip()
            or not isinstance(block.get("workos_session_id"), str)
            or not block["workos_session_id"].strip()
            or human_attested is not True
            or not isinstance(block.get("attester"), str)
            or not block["attester"].strip()
        ):
            return {
                "code": 2,
                "message": "inconsistent: human attestation claim without IdP evidence",
            }

    block_key_id = block.get("key_id")
    sidecar_key_id = sidecar.get("key_id") if isinstance(sidecar, dict) else None
    if (strict_schema and isinstance(sidecar, dict) and block_key_id != sidecar_key_id) or (
        block_key_id and sidecar_key_id and block_key_id != sidecar_key_id
    ):
        return {
            "code": 2,
            "message": (
                f"attestation key_id ({block_key_id!r}) does not match "
                f"signing key ({sidecar_key_id!r})"
            ),
        }

    attester = block.get("attester") or "unattested"
    return {
        "code": 0,
        "message": (
            f"attester={attester} kind={attester_kind or 'unknown'} key_id={block_key_id or 'none'}"
        ),
    }


def run_verify_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent evidence verify",
        description="Verify an evidence export against its detached .sig.json sidecar, "
        "and its chain-head anchor when the export carries one.",
    )
    parser.add_argument("file", help="Evidence export bytes to verify")
    parser.add_argument("--sig", help="Signature sidecar (default: <file>.sig.json)")
    parser.add_argument(
        "--pubkey",
        help="Reserved for the Ed25519 mode; HMAC verification uses protected key configuration",
    )
    args = parser.parse_args(argv)
    receipt = _load_receipt(Path(args.file).expanduser())
    if receipt is not None:
        return _verify_receipt(receipt)
    if args.pubkey:
        print("evidence-verify: --pubkey is not valid for the HMAC fallback", file=sys.stderr)
        return 2
    artifact_path = Path(args.file).expanduser()
    sig_path = (
        Path(args.sig).expanduser() if args.sig else evidence_signing.sidecar_path(artifact_path)
    )
    try:
        artifact = artifact_path.read_bytes()
    except OSError as exc:
        print(f"evidence-verify: unable to read export: {exc}", file=sys.stderr)
        return 2
    sidecar: dict[str, Any] | None = None
    try:
        sidecar = evidence_signing.load_sidecar(sig_path)
    except FileNotFoundError:
        print("evidence-verify: unsigned export (signature sidecar absent)")
        exit_code = 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"evidence-verify: unable to read sidecar: {exc}", file=sys.stderr)
        exit_code = 2
    else:
        valid, reason = evidence_signing.verify_export(artifact, sidecar)
        print(f"evidence-verify: {reason}")
        exit_code = evidence_signing.verify_exit_code(valid, reason)

    anchor_result = _check_anchor(artifact)
    print(f"evidence-verify: anchor: {anchor_result['message']}")

    attestation_result = _check_attestation(artifact, sidecar)
    print(f"evidence-verify: attestation: {attestation_result['message']}")

    return max(exit_code, anchor_result["code"], attestation_result["code"])


def _load_receipt(path: Path) -> dict[str, Any] | None:
    """The parsed file when it claims to be a merge receipt (any version), else
    None. A wrong version is routed here so it fails as a receipt, cleanly."""
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    schema = doc.get("schema") if isinstance(doc, dict) else None
    return doc if isinstance(schema, str) and schema.startswith("merge_receipt/") else None


def _verify_receipt(receipt: dict[str, Any]) -> int:
    """Offline merge-receipt check (task #2307): no hub, no database, no network
    beyond what a third-party anchor witness itself needs."""
    from .merge_receipt import verify_receipt

    result = verify_receipt(receipt)
    for check in result["checks"]:
        mark = {True: "ok", False: "FAIL", None: "not checked"}[check["ok"]]
        print(f"receipt-verify: {check['name']}: {mark}: {check['detail']}")
    if result["malformed"]:
        print("receipt-verify: malformed receipt; nothing else was checked", file=sys.stderr)
        return 2
    print(
        "receipt-verify: consistency checks passed"
        if result["ok"]
        else "receipt-verify: consistency checks FAILED"
    )
    for item in result["not_verified"]:
        print(f"receipt-verify: NOT verified: {item}")
    return 0 if result["ok"] else 1


def _gh_api(path: str) -> Any:
    import subprocess

    result = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path.split('?')[0]} failed")
    return json.loads(result.stdout)


def _default_repo() -> str | None:
    import subprocess

    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def run_receipt_cli(
    argv: list[str] | None = None,
    *,
    api: Any = None,
    review_gate: Any = None,
) -> int:
    """``unified-agent evidence receipt --pr N`` — render ``receipt.json`` (task #2307).

    Reads the hub store read-only. Refuses (exit 1) rather than emit a receipt
    whose PR is unmerged, whose task binding is ambiguous, or that would break
    an honesty rule; exit 2 is a usage or environment error.
    """
    from . import merge_receipt

    parser = argparse.ArgumentParser(
        prog="unified-agent evidence receipt",
        description=(
            "Render the merge receipt (receipt.json, schema merge_receipt/1) for one "
            "merged PR from hub records and GitHub, never from prose. Verify it offline "
            "with `unified-agent evidence verify receipt.json`."
        ),
    )
    parser.add_argument("--pr", type=int, required=True, help="Merged pull request number")
    parser.add_argument("--repo", help="owner/name (default: gh repo view)")
    parser.add_argument(
        "--task",
        type=int,
        help="Assert the bound task; refused unless it is the one task whose "
        "evidence_ref carries the PR URL",
    )
    parser.add_argument("--out", "-o", help="Write receipt.json here (default: stdout)")
    parser.add_argument(
        "--markdown",
        "--md",
        action="store_true",
        help="Print the one-screen PR comment instead of JSON (the JSON still goes to --out)",
    )
    args = parser.parse_args(argv)
    repo = args.repo or _default_repo()
    if not repo:
        print("evidence-receipt: pass --repo owner/name", file=sys.stderr)
        return 2
    if review_gate is None:
        from ..shipping_preflight_cli import review_head_gate as review_gate
    try:
        conn = storage.connect_read_only()
    except Exception as exc:  # noqa: BLE001 - LiveStoreError and sqlite errors alike
        print(f"evidence-receipt: hub store unavailable: {exc}", file=sys.stderr)
        return 2
    try:
        try:
            github = merge_receipt.fetch_github(repo, args.pr, api or _gh_api)
        except Exception as exc:  # noqa: BLE001 - gh missing, offline, bad JSON
            print(f"evidence-receipt: GitHub unavailable: {exc}", file=sys.stderr)
            return 2
        receipt = merge_receipt.build_receipt(
            repo=repo,
            number=args.pr,
            conn=conn,
            github=github,
            review_gate=review_gate,
            task_id=args.task,
        )
    except merge_receipt.ReceiptError as exc:
        print(f"evidence-receipt: {exc}", file=sys.stderr)
        return 1
    except edition.FeatureNotAvailableError as exc:
        print(f"evidence-receipt: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    body = json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
        print(f"evidence-receipt: saved {out_path}", file=sys.stderr)
    if args.markdown:
        print(merge_receipt.render_markdown(receipt), end="")
    elif not args.out:
        print(body, end="")
    return 0


def _build_or_fail(**kwargs) -> tuple[dict | None, int]:
    try:
        return build_evidence_report(**kwargs), 0
    except edition.FeatureNotAvailableError as exc:
        print(f"evidence-report: {exc}", file=sys.stderr)
        return None, 2
    except Exception as exc:
        print(f"evidence-report: error building report: {exc}", file=sys.stderr)
        return None, 2


def run_project_report_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent evidence report",
        description=(
            "Render a project_chain evidence artifact for a 30-second "
            "buyer/risk-officer demo: project spec, nested steps, merged PR "
            "evidence, last merge, and inline audit-verify status."
        ),
    )
    parser.add_argument(
        "--project",
        "-p",
        type=int,
        required=True,
        help="Project root task ID (must carry a [project-spec] marker)",
    )
    parser.add_argument("--json", action="store_true", help="Emit report in raw JSON format")
    parser.add_argument(
        "--markdown",
        "--md",
        action="store_true",
        help="Emit report as Markdown (default for this subcommand)",
    )
    parser.add_argument("--out", "-o", help="Write report to file path")
    args = parser.parse_args(argv)

    post_init.register_default_hooks()
    storage.init_db()
    fmt = "json" if args.json else "markdown"
    report, code = _build_or_fail(project_id=args.project)
    if report is None:
        return code
    return _emit_report(report, fmt, args.out)


def run_render_cli(argv: list[str] | None = None) -> int:
    """``unified-agent evidence render FILE`` — re-render a saved JSON export.

    Reads any schema version through ``read_historical_report`` (task #1673):
    a pre-1.7 export's ``human_authority: verified`` renders as
    credential-attributed. Reads only the file: no hub database, no signing.
    Verify the original bytes with ``unified-agent evidence verify``.
    """
    parser = argparse.ArgumentParser(
        prog="unified-agent evidence render",
        description=(
            "Re-render a saved JSON evidence export (any schema version) as Markdown, "
            "text, CSV or normalized JSON. Historical human_authority values are read "
            "with today's meaning."
        ),
    )
    parser.add_argument("file", help="A JSON evidence export")
    parser.add_argument(
        "--format",
        "-f",
        choices=("markdown", "text", "csv", "json"),
        default="markdown",
        help="Output format (default markdown)",
    )
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"evidence-render: cannot read JSON export: {exc}", file=sys.stderr)
        return 2
    if not isinstance(report, dict):
        print("evidence-render: export is not a JSON object", file=sys.stderr)
        return 2
    try:
        output = format_evidence_report(read_historical_report(report), fmt=args.format)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"evidence-render: malformed export: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


def run_export_cli(argv: list[str] | None = None) -> int:
    """``unified-agent evidence export`` — run one scheduled signed evidence
    export (task #1266). Off by default: refuses cleanly unless
    UA_EVIDENCE_EXPORT_ENABLED=1 and a destination are configured.
    """
    parser = argparse.ArgumentParser(
        prog="unified-agent evidence export",
        description=(
            "Run one scheduled signed evidence export to the configured storage "
            "destination (local path / GCS / S3), the same entry point the "
            "connector's timer calls. Off by default; configure "
            "UA_EVIDENCE_EXPORT_ENABLED=1 and UA_EVIDENCE_EXPORT_DESTINATION first."
        ),
    )
    parser.parse_args(argv)

    from . import evidence_export_sink

    try:
        result = evidence_export_sink.run_once()
    except (evidence_export_sink.DestinationError, evidence_signing.SigningUnavailable) as exc:
        print(f"evidence-export: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in ("exported", "skipped", "disabled") else 1


def run_evidence_cli(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "verify":
        return run_verify_cli(args_list[1:])
    if args_list and args_list[0] == "report":
        return run_project_report_cli(args_list[1:])
    if args_list and args_list[0] == "export":
        return run_export_cli(args_list[1:])
    if args_list and args_list[0] == "render":
        return run_render_cli(args_list[1:])
    if args_list and args_list[0] == "receipt":
        return run_receipt_cli(args_list[1:])

    parser = argparse.ArgumentParser(
        prog="unified-agent evidence",
        description="Generate an attribution-grade evidence report (human->device->agent->action->proof).",
        epilog=(
            "Also: unified-agent evidence report --project <id>; unified-agent evidence export; "
            "unified-agent evidence render <export.json>; "
            "unified-agent evidence receipt --pr <n> [--repo owner/name] [--out receipt.json]"
        ),
    )
    parser.add_argument("--task", "-t", type=int, help="Task ID to inspect")
    parser.add_argument("--pr", help="Filter by PR URL or commit SHA")
    parser.add_argument("--since", help="Filter records since ISO timestamp (e.g. 2026-09-01)")
    parser.add_argument("--until", help="Filter records until ISO timestamp")
    parser.add_argument("--agent", "-a", help="Filter by executing agent label")
    parser.add_argument("--project", "-p", help="Filter by project name")
    parser.add_argument(
        "--limit", "-n", type=int, default=50, help="Max records to return (default 50, max 500)"
    )
    parser.add_argument("--json", action="store_true", help="Emit report in raw JSON format")
    parser.add_argument(
        "--markdown", "--md", action="store_true", help="Emit report as GFM Markdown table"
    )
    parser.add_argument("--out", "-o", help="Write report to file path")

    args = parser.parse_args(args_list)

    post_init.register_default_hooks()
    storage.init_db()

    fmt = "json" if args.json else ("markdown" if args.markdown else "text")
    report, code = _build_or_fail(
        task_id=args.task,
        since=args.since,
        until=args.until,
        agent=args.agent,
        project=args.project,
        pr=args.pr,
        limit=args.limit,
    )
    if report is None:
        return code
    return _emit_report(report, fmt, args.out)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return run_evidence_cli(args)


if __name__ == "__main__":
    sys.exit(main())
