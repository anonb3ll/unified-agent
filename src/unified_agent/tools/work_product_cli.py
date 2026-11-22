"""CLI for the work-product submit -> approval -> release wedge (task #1278).

``release`` is already taken by ``unified_agent.release`` (versioned,
immutable *software* release artifacts — a different concept entirely), so
this surface is namespaced ``work-product`` instead:

    unified-agent work-product submit <file> --pack <id> [--title T] [--tag k=v] [--model M]
    unified-agent work-product submit <file> --pack <id> --hub URL [--token-env NAME]
    unified-agent work-product release <id> --hub URL [--token-env NAME]
    unified-agent work-product verify <id>
    unified-agent work-product list [--status S] [--pack P]
    unified-agent work-product evidence <id>
    unified-agent work-product diff <id>
    unified-agent work-product packs
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

from .. import post_init, storage
from .approvals import ApprovalPendingError
from .work_product import (
    list_template_packs,
    list_work_products,
    work_product_diff,
    work_product_evidence,
    work_product_submit,
    work_product_verify,
)


def _parse_tags(pairs: list[str]) -> dict[str, str]:
    tags: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            print(f"work-product: invalid --tag {pair!r}, expected key=value", file=sys.stderr)
            raise SystemExit(2)
        key, _, value = pair.partition("=")
        tags[key.strip()] = value.strip()
    return tags


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _remote_http(action: str, hub: str, token: str, payload: dict) -> int:
    """Installed-wheel fallback when the optional TypeScript SDK build is absent."""
    path = (
        "/api/work-products"
        if action == "submit"
        else f"/api/work-products/{payload['work_product_id']}/release"
    )
    body = payload if action == "submit" else {}
    try:
        response = httpx.post(
            hub.rstrip("/") + path,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            # Bound connection setup, but do not abandon an in-flight release:
            # the hub may commit after the client stops waiting.
            timeout=httpx.Timeout(None, connect=10.0),
        )
    except httpx.HTTPError as exc:
        print(
            f"work-product: hub outcome unconfirmed ({exc}); check the hub before retrying",
            file=sys.stderr,
        )
        return 2
    if response.status_code >= 400:
        print(
            f"work-product: hub returned {response.status_code}: {response.text[:300]}",
            file=sys.stderr,
        )
        return 2
    try:
        result = response.json()
    except ValueError:
        print("work-product: hub returned invalid JSON; outcome unconfirmed", file=sys.stderr)
        return 2
    if not isinstance(result, dict):
        print("work-product: hub returned an invalid result; outcome unconfirmed", file=sys.stderr)
        return 2
    _emit(result)
    return 0


def _sdk_adapter_available(script: Path) -> bool:
    return script.is_file() and (script.parent.parent / "dist/index.js").is_file()


def _remote_sdk(action: str, hub: str, token_env: str, payload: dict) -> int:
    """Use the first-party SDK when built, with installed-wheel HTTP fallback."""
    token = os.environ.get(token_env)
    if not token:
        print(f"work-product: {token_env} is unset or empty", file=sys.stderr)
        return 2
    script = Path(__file__).resolve().parents[3] / "packages/client/bin/work-product.mjs"
    if not _sdk_adapter_available(script):
        return _remote_http(action, hub, token, payload)
    try:
        completed = subprocess.run(
            ["node", str(script), action, hub, token_env],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        print(
            f"work-product: SDK adapter outcome unconfirmed ({exc}); check the hub before retrying",
            file=sys.stderr,
        )
        return 2
    if completed.returncode:
        print(
            "work-product: SDK adapter did not confirm the outcome: "
            f"{completed.stderr.strip()[:300]}; check the hub before retrying",
            file=sys.stderr,
        )
        return 2
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        print(
            "work-product: SDK adapter returned invalid JSON; outcome unconfirmed", file=sys.stderr
        )
        return 2
    _emit(result)
    return 0


def _run_submit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="unified-agent work-product submit")
    parser.add_argument("file", help="Path to the document/file to submit")
    parser.add_argument("--pack", required=True, help="Template pack id (e.g. legal)")
    parser.add_argument("--title", help="Work product title (default: the file name)")
    parser.add_argument(
        "--tag", action="append", default=[], metavar="KEY=VALUE", help="Repeatable"
    )
    parser.add_argument("--model", help="Producing model identifier")
    parser.add_argument("--hub", help="Use the typed SDK against this hub URL")
    parser.add_argument("--token-env", default="UA_AGENT_BEARER_TOKEN")
    args = parser.parse_args(argv)

    title = args.title or Path(args.file).name
    tags = _parse_tags(args.tag)
    if args.hub:
        return _remote_sdk(
            "submit",
            args.hub,
            args.token_env,
            {
                "source_path": args.file,
                "title": title,
                "template_pack": args.pack,
                "tags": tags,
                "model": args.model,
            },
        )
    post_init.register_default_hooks()
    storage.init_db()
    try:
        result = work_product_submit(args.file, title, args.pack, tags=tags, model=args.model)
    except ApprovalPendingError as pending:
        print(
            f"work-product: submitted, pending human approval {pending.approval_id}. "
            "A human resolves it with approval_resolve(); retry this exact submit "
            "afterwards to release."
        )
        return 0
    except Exception as exc:
        print(f"work-product: submit failed: {exc}", file=sys.stderr)
        return 2
    _emit(result)
    return 0


def _run_release(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="unified-agent work-product release")
    parser.add_argument("id", type=int)
    parser.add_argument("--hub", required=True, help="Hub URL")
    parser.add_argument("--token-env", default="UA_AGENT_BEARER_TOKEN")
    args = parser.parse_args(argv)
    return _remote_sdk("release", args.hub, args.token_env, {"work_product_id": args.id})


def _run_verify(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="unified-agent work-product verify")
    parser.add_argument("id", type=int)
    args = parser.parse_args(argv)
    post_init.register_default_hooks()
    storage.init_db()
    try:
        result = work_product_verify(args.id)
    except Exception as exc:
        print(f"work-product: verify failed: {exc}", file=sys.stderr)
        return 2
    _emit(result)
    return 1 if result.get("tampered") else 0


def _run_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="unified-agent work-product list")
    parser.add_argument("--status")
    parser.add_argument("--pack")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    post_init.register_default_hooks()
    storage.init_db()
    result = list_work_products(status=args.status, template_pack=args.pack, limit=args.limit)
    _emit(result)
    return 0


def _run_evidence(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="unified-agent work-product evidence")
    parser.add_argument("id", type=int)
    args = parser.parse_args(argv)
    post_init.register_default_hooks()
    storage.init_db()
    try:
        result = work_product_evidence(args.id)
    except Exception as exc:
        print(f"work-product: evidence failed: {exc}", file=sys.stderr)
        return 2
    _emit(result)
    return 0 if result["ledger"]["chain_valid"] and result["consistent"] else 1


def _run_diff(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="unified-agent work-product diff")
    parser.add_argument("id", type=int)
    args = parser.parse_args(argv)
    post_init.register_default_hooks()
    storage.init_db()
    try:
        result = work_product_diff(args.id)
    except Exception as exc:
        print(f"work-product: diff failed: {exc}", file=sys.stderr)
        return 2
    if result.get("diffable"):
        print(result["diff"], end="")
    else:
        print(f"work-product: no diff: {result.get('reason')}")
    return 0


def _run_packs(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="unified-agent work-product packs")
    parser.parse_args(argv)
    _emit({"template_packs": list_template_packs()})
    return 0


def run_work_product_cli(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list in (["-h"], ["--help"]):
        print(
            "usage: unified-agent work-product {submit,release,verify,list,evidence,diff,packs} ..."
        )
        return 0
    if args_list and args_list[0] == "submit":
        return _run_submit(args_list[1:])
    if args_list and args_list[0] == "release":
        return _run_release(args_list[1:])
    if args_list and args_list[0] == "verify":
        return _run_verify(args_list[1:])
    if args_list and args_list[0] == "list":
        return _run_list(args_list[1:])
    if args_list and args_list[0] == "evidence":
        return _run_evidence(args_list[1:])
    if args_list and args_list[0] == "diff":
        return _run_diff(args_list[1:])
    if args_list and args_list[0] == "packs":
        return _run_packs(args_list[1:])
    print(
        "usage: unified-agent work-product {submit,release,verify,list,evidence,diff,packs} ...",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(run_work_product_cli())
