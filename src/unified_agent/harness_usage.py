"""Measure an interactive harness session's own usage for its run (task #1837).

Executor runs report usage from the harness's stdout (task #1917). An agent
working interactively, which is most of the fleet's runs, had no way to: its
``task_update`` could carry ``run_usage``, but nothing measured it, so every
such run finished unknown-cost. The harness already records each model call
in its local session store, so the run's usage is read from there:

* Claude Code: ``<store>/<project>/<session>.jsonl`` plus that session's
  ``<session>/subagents/*.jsonl``. Each assistant message's ``usage`` counts
  once per message id: a message spans several lines as it streams, the last
  line holds its final counts, and the first line's timestamp decides which
  run the message belongs to.
* Codex: ``<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*-<session>.jsonl``. Its
  ``token_count`` events carry a cumulative ``total_token_usage``; the window's
  usage is the last total inside it minus the last total before it.

Only lines timestamped in ``[since, until)`` count, so a session that worked on
several tasks charges each run for its own window. The session id binds the
measurement to one harness session: a concurrent session in the same project
is never summed in. No cost is derived from tokens: the stores carry none.

``billing`` is never inferred. Neither store records which login or provider
paid for a call, and asking the CLI how it is logged in now says nothing about
calls already made (a login or a resumed session's provider can change). The
caller may state it with ``--billing``; otherwise the run reports its tokens
and stays unknown-cost, keeping its reserve.

With ``--report --task N --run R --agent L`` it also records the result on
that run as late accounting, ``task_claim(N, L, expected_run_id=R,
run_usage=..., usage_only=True)``, through the hub at ``UA_HTTP_HUB_URL`` with
the bearer in ``UA_AGENT_BEARER_TOKEN``. The hub accepts it only from the run's
own authenticated actor; numeric totals only go up, so a repeat is harmless.
``scripts/hook_usage_report.py`` runs this from Claude Code when a run ends.

``ua-usage`` prints the result as the ``run_usage`` object ``task_update`` and
``task_claim`` accept. It reads local files only and calls no hub or provider.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import native_sessions
from .executor import backends

PROVIDERS = {"claude": "anthropic", "codex": "openai"}


def parse_when(value: str) -> datetime:
    """An ISO 8601 instant; a naive one is UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _stamp(record: dict[str, Any]) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return parse_when(raw)
    except ValueError:
        return None


def _records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _in_window(when: datetime | None, since: datetime, until: datetime | None) -> bool:
    return when is not None and when >= since and (until is None or when < until)


def claude_files(session_id: str, store: Path | None = None) -> list[Path]:
    """A Claude Code session's transcript and its subagent transcripts."""
    main = native_sessions.session_file("claude", session_id, claude_dir=store)
    if main is None:
        return []
    subagents = main.parent / session_id / "subagents"
    try:
        extra = sorted(p for p in subagents.glob("*.jsonl") if p.is_file())
    except OSError:
        extra = []
    return [main, *extra]


def measure_claude(
    paths: Iterable[Path],
    since: datetime | None = None,
    until: datetime | None = None,
    *,
    windows: list[tuple[datetime, datetime | None]] | None = None,
) -> dict[str, Any] | None:
    """Harness usage of the assistant messages started in the window, or None.

    A message is attributed by its first line, so a call in flight at a claim
    or a finish is charged once, to the run it started in, at its final count.
    ``windows`` (disjoint) counts messages started in any of them; models are
    tallied across all of them before the run is named (task #2037).
    """
    spans = windows if windows else [(since, until)]
    messages: dict[str, tuple[datetime | None, str | None, dict[str, Any]]] = {}
    for path in paths:
        for record in _records(path):
            if record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            key = str(message.get("id") or record.get("requestId") or record.get("uuid") or "")
            if not key:
                continue
            key = f"{path.name}:{key}"
            model = message.get("model") if isinstance(message.get("model"), str) else None
            started = messages[key][0] if key in messages else _stamp(record)
            messages[key] = (started, model, message["usage"])
    per_model: dict[str | None, dict[str, Any] | None] = {}
    for started, model, raw in messages.values():
        if model == "<synthetic>" or not any(_in_window(started, a, b) for a, b in spans):
            continue
        per_model[model] = backends.add_harness_usage(
            per_model.get(model), backends._claude_usage(raw, model)
        )
    return _combine(per_model)


def measure_codex(
    path: Path, since: datetime, until: datetime | None = None
) -> dict[str, Any] | None:
    """Harness usage of a Codex session's token totals in the window, or None."""
    usage, _ = _codex_window(path, since, until)
    return usage


def _codex_window(
    path: Path, since: datetime, until: datetime | None = None
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """A Codex window's usage, plus its output tokens per active model.

    Totals are cumulative, so the window's usage is the last in-window total
    minus the last one before it. Each in-window total's output delta is
    charged to the model of the latest ``turn_context`` before it, so a run
    can be named for the model that produced most of its output (task #2037).
    """
    before: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    model: str | None = None  # the run's model, as before: in-window or initial
    active: str | None = None  # the model in effect at this point of the file
    previous: dict[str, Any] | None = None
    per_model: dict[str, int] = {}
    for record in _records(path):
        when = _stamp(record)
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context":
            if isinstance(payload.get("model"), str):
                active = payload["model"]
                if model is None or _in_window(when, since, until):
                    model = payload["model"]
            continue
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        total = info.get("total_token_usage") if isinstance(info, dict) else None
        # A total without both counters measures nothing; it must not replace
        # a valid baseline or latest total.
        if (
            not isinstance(total, dict)
            or when is None
            or _int(total, "input_tokens") is None
            or _int(total, "output_tokens") is None
        ):
            continue
        if when < since:
            before = total
        elif _in_window(when, since, until):
            last = total
            if active is not None:
                delta = (_int(total, "output_tokens") or 0) - (_int(previous, "output_tokens") or 0)
                per_model[active] = per_model.get(active, 0) + max(0, delta)
        previous = total
    if last is None:
        return None, {}
    usage = backends._codex_turn_usage(
        {
            name: max(0, (_int(last, name) or 0) - (_int(before, name) or 0))
            for name in ("input_tokens", "output_tokens", "cached_input_tokens")
        }
    )
    if usage is not None:
        usage["model"] = max(per_model, key=lambda m: per_model[m]) if per_model else model
    return usage, per_model


def _int(values: dict[str, Any] | None, name: str) -> int | None:
    value = (values or {}).get(name)
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return value if ok else None


def _combine(per_model: dict[str | None, dict[str, Any] | None]) -> dict[str, Any] | None:
    """Sum every model's usage, named for the model with the most output.

    ``run_usage`` names one model. A session's main model and its subagents'
    smaller models are one run, so the tokens are summed and the run is
    attributed to the model that produced most of the output.
    """
    usages = [(m, u) for m, u in per_model.items() if u is not None]
    if not usages:
        return None
    total: dict[str, Any] | None = None
    for _, usage in usages:
        total = backends.add_harness_usage(total, usage)
    if total is not None:
        total["model"] = max(usages, key=lambda mu: mu[1].get("output_tokens") or 0)[0]
    return total


def _check_disjoint(spans: list[tuple[datetime, datetime | None]]) -> None:
    """Refuse empty, reversed or overlapping windows, which would count a call twice."""
    for i, (start, end) in enumerate(spans):
        if end is not None and end <= start:
            raise ValueError(f"window {start.isoformat()},{end.isoformat()} is empty or reversed")
        if i + 1 < len(spans):
            following = spans[i + 1][0]
            if end is None or end > following:
                raise ValueError(f"windows overlap at {following.isoformat()}")


def measure(
    harness: str,
    session_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    *,
    windows: list[tuple[datetime, datetime | None]] | None = None,
    billing: str | None = None,
    claude_store: Path | None = None,
    codex_dir: Path | None = None,
) -> dict[str, Any] | None:
    """The ``run_usage`` of one harness session's window, or None if unmeasured.

    ``windows`` measures several disjoint ``[since, until)`` windows of the same
    session and sums them, so a run that was cut short and later resumed reports
    its cumulative total in one snapshot (task #2037). Raises ``ValueError`` for
    empty, reversed or overlapping windows (only the last may be open-ended) and
    ``LookupError`` when the session's store file is not found.
    """
    spans = sorted(windows, key=lambda w: w[0]) if windows else [(since, until)]
    if any(start is None for start, _ in spans):
        raise LookupError("a window needs a start")
    _check_disjoint(spans)
    if harness == "claude":
        paths = claude_files(session_id, claude_store)
        if not paths:
            raise LookupError(f"no Claude Code session {session_id!r} in the local store")
        # One pass over every window, so each model's output is tallied across
        # all of them before the run is named.
        usage = measure_claude(paths, windows=spans)
        if usage is not None and not usage.get("provider"):
            usage["provider"] = PROVIDERS[harness]
        return backends.to_run_usage(usage, billing=billing)

    if harness != "codex":
        raise LookupError(f"unsupported harness {harness!r}; use claude or codex")
    path = native_sessions.session_file("codex", session_id, codex_dir=codex_dir)
    if path is None:
        raise LookupError(f"no Codex session {session_id!r} in the local store")
    usage: dict[str, Any] | None = None
    output_by_model: dict[str, int] = {}
    for start, end in spans:
        part, per_model = _codex_window(path, start, end)
        usage = backends.add_harness_usage(usage, part)
        for name, out in per_model.items():
            output_by_model[name] = output_by_model.get(name, 0) + out
    if usage is not None and output_by_model:
        # As within one window, the run is named for the model with the most
        # output across all of them, not the last window's.
        usage["model"] = max(output_by_model, key=lambda m: output_by_model[m])
    if usage is not None and not usage.get("provider"):
        usage["provider"] = PROVIDERS[harness]
    return backends.to_run_usage(usage, billing=billing)


def _detect(harness: str | None, session: str | None) -> tuple[str, str]:
    if harness is None and os.environ.get("CLAUDECODE") == "1":
        harness = "claude"
    if session is None and harness == "claude":
        session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not harness or not session:
        raise LookupError(
            "could not tell which harness session this is; pass --harness and --session"
        )
    return harness, session


def report(task_id: int, run_id: str, agent: str, usage: dict[str, Any]) -> dict[str, Any]:
    """Record ``usage`` on ``run_id`` as late accounting by its own actor."""
    from .hub_client import HubClient

    client = HubClient(agent_label=agent, allow_fallback=False)
    return client.call_tool(
        "task_claim",
        {
            "id": task_id,
            "agent": agent,
            "expected_run_id": run_id,
            "run_usage": usage,
            "usage_only": True,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ua-usage",
        description=(
            "Print the run_usage of this harness session since a run's claim, for "
            "task_update(run_usage=...) or task_claim(usage_only=True). Reads the "
            "local session store only; with --report it also writes the usage to "
            "the hub (task_claim usage_only)."
        ),
    )
    parser.add_argument("--since", help="Window start, ISO 8601 (the run's claimed_at)")
    parser.add_argument("--until", help="Window end, ISO 8601 (default: now)")
    parser.add_argument(
        "--window",
        action="append",
        metavar="START,END",
        help="A disjoint window to sum, ISO 8601 START,END; repeatable (replaces --since/--until)",
    )
    parser.add_argument("--harness", choices=sorted(PROVIDERS), help="Default: detected")
    parser.add_argument(
        "--session", help="Harness session id (default: CLAUDE_CODE_SESSION_ID in Claude Code)"
    )
    parser.add_argument(
        "--billing",
        choices=("api", "subscription", "local"),
        help=(
            "State who paid for the window; not inferred, since the session store "
            "records no login or provider per call (default: unset, unknown cost)"
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Also record the usage on --run (late accounting via the hub)",
    )
    parser.add_argument("--task", type=int, help="Task id of the run (with --report)")
    parser.add_argument("--run", help="Run id, sp-... (with --report)")
    parser.add_argument("--agent", help="The run's agent label (with --report)")
    args = parser.parse_args(argv)
    if args.report and not (args.task and args.run and args.agent):
        print("ua-usage: --report needs --task, --run and --agent", file=sys.stderr)
        return 2
    if bool(args.window) == bool(args.since) or (args.window and args.until):
        print(
            "ua-usage: give --since (optionally --until) or one or more --window", file=sys.stderr
        )
        return 2
    try:
        if args.window:
            windows = []
            for item in args.window:
                start, sep, end = item.partition(",")
                if not sep:
                    raise ValueError(item)
                windows.append((parse_when(start), parse_when(end)))
            since = until = None
        else:
            windows = None
            since = parse_when(args.since)
            until = parse_when(args.until) if args.until else None
    except ValueError:
        print("ua-usage: --since/--until/--window must be ISO 8601 instants", file=sys.stderr)
        return 2
    try:
        harness, session = _detect(args.harness, args.session)
        usage = measure(harness, session, since, until, windows=windows, billing=args.billing)
    except ValueError as exc:
        print(f"ua-usage: {exc}", file=sys.stderr)
        return 2
    except LookupError as exc:
        print(f"ua-usage: {exc}", file=sys.stderr)
        return 1
    if usage is None:
        print("ua-usage: no model calls in that window", file=sys.stderr)
        return 1
    print(json.dumps(usage, sort_keys=True))
    if args.report:
        try:
            report(args.task, args.run, args.agent, usage)
        except Exception as exc:  # noqa: BLE001 - the CLI reports, never crashes
            # Exception text can carry the rejected Authorization value (an
            # invalid header echoes it), so only the type and an HTTP status.
            status = re.search(r"\bHTTP (\d{3})\b", str(exc))
            detail = f" (HTTP {status.group(1)})" if status else ""
            print(f"ua-usage: report failed: {type(exc).__name__}{detail}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
