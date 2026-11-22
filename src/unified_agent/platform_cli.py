"""Read-only CLI adapter for the canonical platform HTTP object surface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote, urlencode

import httpx

from . import config
from .executor.daemon import HubClient
from .hub_client import (  # noqa: F401 - platform transport helpers; re-exported here
    _MCP_PATH,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PlatformCliError,
    _platform_url,
    _post_json,
    _read_response,
)


def _safe_segment(value: str, label: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise PlatformCliError(f"{label} must be non-empty and contain no control characters")
    return quote(value, safe="")


def _get_json(client: HubClient, path: str, query: str = "") -> dict:
    url = _platform_url(client.hub_url, path, query)
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as http:
            with http.stream("GET", url, headers=client._headers()) as response:
                status = response.status_code
                if status < 200 or status >= 300:
                    raise PlatformCliError(f"canonical hub rejected platform read (HTTP {status})")
                body = _read_response(response)
    except PlatformCliError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise PlatformCliError(f"could not reach canonical hub ({type(exc).__name__})") from exc
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformCliError("canonical hub returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PlatformCliError("canonical hub returned a non-object JSON payload")
    return payload


def run_platform_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unified-agent platform")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("manifest", help="read the canonical platform object manifest")
    get = sub.add_parser("get", help="read one canonical platform object")
    get.add_argument("kind")
    get.add_argument("object_id", nargs="?")
    graph = sub.add_parser(
        "provenance-graph",
        help="read the provenance graph (human->device->agent->action->proof)",
    )
    graph.add_argument(
        "--query",
        choices=["by_human", "by_release"],
        help="named query: by_human (everything a human authorized) or "
        "by_release (every action behind a set of task ids)",
    )
    graph.add_argument("--task-id", type=int, help="scope to one task")
    graph.add_argument(
        "--task-ids", help="comma-separated task ids (query=by_release's release proxy)"
    )
    graph.add_argument("--project-id", type=int, help="scope to a [project-spec] subtree")
    graph.add_argument("--since", help="ISO timestamp lower bound")
    graph.add_argument("--until", help="ISO timestamp upper bound")
    graph.add_argument("--human", help="human identity (required for query=by_human)")
    graph.add_argument("--agent", help="filter by executing agent label")
    graph.add_argument("--pr", help="filter by PR URL or commit SHA")
    graph.add_argument("--limit", type=int, help="max underlying evidence records")
    args = parser.parse_args(argv)
    try:
        hub_url = os.environ.get("UA_HTTP_HUB_URL", "").strip()
        if not hub_url:
            raise PlatformCliError(
                "UA_HTTP_HUB_URL is required for platform reads; set an explicit canonical URL ending in /mcp"
            )
        _platform_url(hub_url, "/api/platform/manifest")
        client = HubClient(
            agent_label=config.executor_agent(), hub_url=hub_url, allow_fallback=False
        )
        if args.command == "manifest":
            payload = _get_json(client, "/api/platform/manifest")
        elif args.command == "provenance-graph":
            query_params: dict[str, str] = {}
            if args.query:
                query_params["query"] = args.query
            if args.task_id is not None:
                query_params["task_id"] = str(args.task_id)
            if args.task_ids:
                query_params["task_ids"] = args.task_ids
            if args.project_id is not None:
                query_params["project_id"] = str(args.project_id)
            if args.since:
                query_params["since"] = args.since
            if args.until:
                query_params["until"] = args.until
            if args.human:
                query_params["human"] = args.human
            if args.agent:
                query_params["agent"] = args.agent
            if args.pr:
                query_params["pr"] = args.pr
            if args.limit is not None:
                query_params["limit"] = str(args.limit)
            payload = _get_json(client, "/api/platform/provenance-graph", urlencode(query_params))
        else:
            kind = _safe_segment(args.kind, "object kind")
            suffix = f"/api/platform/objects/{kind}"
            if args.object_id is not None:
                suffix += f"/{_safe_segment(args.object_id, 'object id')}"
            payload = _get_json(client, suffix)
    except (PlatformCliError, RuntimeError) as exc:
        print(f"platform read failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
