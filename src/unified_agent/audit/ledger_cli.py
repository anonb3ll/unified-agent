"""CLI entry point for audit ledger hash-chain verification (task #746).

Exit codes:
  0 = chain verified or empty log
  1 = chain broken / tampered
  2 = bad usage
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import edition, events, post_init, storage


def run_verify_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent audit verify",
        description="Verify the tamper-evident audit ledger hash chain.",
    )
    parser.add_argument("--json", action="store_true", help="emit report as JSON")
    args = parser.parse_args(argv)

    try:
        edition.require_feature("audit_hash_chain")
    except edition.FeatureNotAvailableError as exc:
        print(f"audit-ledger: {exc}", file=sys.stderr)
        return 2

    post_init.register_default_hooks()
    storage.init_db()
    result = events.verify_event_chain()

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["status"] == "valid":
            head = (result.get("head_hash") or "")[:16]
            print(
                f"audit-ledger: valid ({result['total_verified']} entries verified, head={head}...)"
            )
        elif result["status"] == "empty":
            print("audit-ledger: empty (0 hashed entries)")
        else:
            t = result.get("tampered_at", {})
            print(
                f"audit-ledger: TAMPERED at rowid={t.get('rowid')} ulid={t.get('ulid')} channel={t.get('channel')}"
            )
            print(f"         reason: {t.get('reason')}")
            if "expected_hash" in t:
                print(f"         expected: {t['expected_hash']}")
                print(f"         actual:   {t['actual_hash']}")
            if "expected_prev_hash" in t:
                print(f"         expected prev: {t['expected_prev_hash']}")
                print(f"         actual prev:   {t['actual_prev_hash']}")

    return 0 if result["verified"] else 1


def run_reanchor_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent audit re-anchor",
        description="Anchor the audit ledger chain head to an external witness.",
    )
    parser.add_argument("--json", action="store_true", help="emit report as JSON")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today UTC)")
    parser.add_argument(
        "--backend",
        choices=["vault", "tsa", "git"],
        help="anchor backend (default: UA_EVIDENCE_ANCHOR_BACKEND, else vault)",
    )
    args = parser.parse_args(argv)

    from mcp.server.mcpserver.exceptions import ToolError

    from .anchor import anchor_chain_head

    try:
        result = anchor_chain_head(day=args.date, backend=args.backend)
    except (ToolError, edition.FeatureNotAvailableError) as exc:
        print(f"audit-ledger: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result.get("anchor_backend") == "vault":
        print(
            f"audit-ledger: re-anchored {result['anchor_path']} "
            f"status={result['status']} head={(result.get('head_hash') or '')[:16]}"
        )
    else:
        ref = result.get("anchor_ref") or {}
        print(
            f"audit-ledger: anchored via {result.get('anchor_backend')} "
            f"status={result['status']} head={(result.get('head_hash') or '')[:16]} "
            f"ref={json.dumps(ref, sort_keys=True)}"
        )
    return 0 if result["verified"] else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return run_verify_cli(args)


if __name__ == "__main__":
    sys.exit(main())
