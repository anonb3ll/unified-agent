#!/usr/bin/env python3
"""Render the control center as a self-contained HTML page.

The web UI at `/ui` is served by the HTTP hub. A cloud session runs the server over
stdio (see docs/cloud-sessions.md), so there is no hub and no `/ui` — but the data
behind it is not HTTP-shaped at all: `webui.api.overview_payload()` calls the same
registered tools in-process, on any transport. This renders that payload to a static
page you can publish as a web artifact.

A snapshot, deliberately. An artifact runs on claude.ai and cannot reach a socket
inside the session's container, so there is nothing to poll. For an ephemeral session
that is the right shape: the state at a moment, readable from a phone and shareable,
which the loopback-bound `/ui` never was.

    uv run python scripts/render_dashboard.py --out /tmp/dashboard.html
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unified_agent.webui import api  # noqa: E402

# The palette is app.css's, so a snapshot and the live control center read as one
# product. Both themes define every status colour — see that file's header for why a
# foreground token and the ink that sits on a filled swatch of it must not be mixed up.
#
# The theme *plumbing* differs from app.css on purpose. A page served by the hub only
# ever sees `prefers-color-scheme`, but an artifact has three states: an explicit
# viewer choice stamps `data-theme` on the root element, and the default "system"
# setting stamps nothing. A bare light/dark media query gets the crossed case wrong —
# viewer forces light while the OS is dark, and the page paints dark text on the
# host's light ground. So light is the base, dark arrives by media query guarded
# against an explicit light choice, and an explicit dark choice restates it.
CSS = """
:root {
  --bg:#f5f5f7; --panel:#fff; --surface:#f7f7f9; --border:#e2e2e6;
  --text:#18181b; --muted:#5f5f6a; --accent:#3b5bdb;
  --online:#15803d; --idle:#a16207; --stale:#c2410c; --danger:#b91c1c; --unknown:#6b6b76;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:#09090b; --panel:#111114; --surface:#17171b; --border:#26262b;
    --text:#e7e7ea; --muted:#9a9aa4; --accent:#7c9cf5;
    --online:#4ade80; --idle:#fbbf24; --stale:#fb923c; --danger:#f87171; --unknown:#71717a;
  }
}
:root[data-theme="dark"] {
  --bg:#09090b; --panel:#111114; --surface:#17171b; --border:#26262b;
  --text:#e7e7ea; --muted:#9a9aa4; --accent:#7c9cf5;
  --online:#4ade80; --idle:#fbbf24; --stale:#fb923c; --danger:#f87171; --unknown:#71717a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
     font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:1.25rem 1.5rem;border-bottom:1px solid var(--border);background:var(--panel);
       display:flex;flex-wrap:wrap;gap:0.75rem;align-items:baseline}
h1{font-size:1.1rem;font-weight:600;margin:0}
h2{font-size:0.82rem;font-weight:600;margin:0 0 0.75rem;text-transform:uppercase;
   letter-spacing:.06em;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
main{display:grid;gap:1rem;padding:1rem 1.5rem;
     grid-template-columns:repeat(auto-fit,minmax(min(100%,32rem),1fr));align-items:start}
.pane{background:var(--panel);border:1px solid var(--border);border-radius:10px;
      padding:1rem;min-width:0}
.pane.full{grid-column:1/-1}
.count{color:var(--muted);font-weight:400;font-size:.78rem;text-transform:none;letter-spacing:0}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th{text-align:left;font-weight:600;color:var(--muted);font-size:.74rem;text-transform:uppercase;
   letter-spacing:.05em;padding:.4rem .6rem .4rem 0;border-bottom:1px solid var(--border)}
td{padding:.4rem .6rem .4rem 0;
   border-bottom:1px solid color-mix(in srgb,var(--border) 55%,transparent);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
.mono{font-family:var(--mono);font-size:.8rem}
.nowrap{white-space:nowrap}
.scroll{overflow-x:auto}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:.45rem}
.dot.online{background:var(--online)}.dot.idle{background:var(--idle)}
.dot.stale{background:var(--stale)}.dot.offline{background:var(--unknown)}
.dot.unknown{background:var(--unknown)}
.badge{font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;font-weight:600;
       padding:.1rem .4rem;border-radius:4px;border:1px solid var(--border);color:var(--muted);
       white-space:nowrap}
.badge.warn{color:var(--idle);border-color:color-mix(in srgb,var(--idle) 45%,transparent);
            background:color-mix(in srgb,var(--idle) 10%,transparent)}
.badge.danger{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 45%,transparent);
              background:color-mix(in srgb,var(--danger) 10%,transparent)}
.badge.info{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 45%,transparent);
            background:color-mix(in srgb,var(--accent) 10%,transparent)}
.chips{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:.75rem}
.chip{border:1px solid var(--border);border-radius:999px;padding:.18rem .65rem;
      font-size:.78rem;color:var(--muted)}
.chip b{color:var(--text);font-weight:600}
.chip.warn{border-color:color-mix(in srgb,var(--idle) 50%,transparent);color:var(--idle);
           background:color-mix(in srgb,var(--idle) 10%,transparent)}
.chip.warn b{color:var(--idle)}
.empty{color:var(--muted);font-size:.83rem;padding:.5rem 0}
.err{color:var(--danger);font-size:.83rem;padding:.5rem 0}
footer{padding:.9rem 1.5rem;border-top:1px solid var(--border);background:var(--panel);
       color:var(--muted);font-size:.8rem;display:flex;flex-wrap:wrap;gap:1.25rem}
.pill{font-size:.75rem;padding:.15rem .6rem;border-radius:999px;border:1px solid var(--border);
      color:var(--muted)}
"""


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def duration(seconds: object) -> str:
    if seconds is None:
        return "—"
    try:
        s = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d"


def ago(iso: object) -> str:
    if not iso:
        return "—"
    try:
        then = dt.datetime.fromisoformat(str(iso))
    except ValueError:
        return "—"
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.UTC)
    return duration((dt.datetime.now(dt.UTC) - then).total_seconds())


def pane(title: str, count: str, body: str, *, full: bool = False) -> str:
    suffix = f' <span class="count">{esc(count)}</span>' if count else ""
    cls = "pane full" if full else "pane"
    return f'<section class="{cls}"><h2>{esc(title)}{suffix}</h2>{body}</section>'


def table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    """Rows carry pre-escaped HTML; callers escape their own cell values."""
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def failed(payload: dict) -> str | None:
    """An error and an empty result must never render the same — see webui/api.py."""
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        return str(payload["error"])
    errors = payload.get("errors")
    if errors:
        return ", ".join(str(e) for e in errors)
    return None


def chips(items: list[tuple[str, object]], *, warn_keys: frozenset[str] = frozenset()) -> str:
    out = []
    for label, value in items:
        warn = label in warn_keys and value
        out.append(
            f'<span class="chip{" warn" if warn else ""}"><b>{esc(value)}</b> {esc(label)}</span>'
        )
    return f'<div class="chips">{"".join(out)}</div>'


def render_attention(payload: dict) -> str:
    err = failed(payload)
    items = payload.get("items") or []
    if err and not items:
        return pane(
            "Attention", "", f'<p class="err">attention query failed: {esc(err)}</p>', full=True
        )
    sev_cls = {"critical": "badge danger", "high": "badge warn", "medium": "badge info"}
    rows = [
        [
            f'<span class="{sev_cls.get(i.get("severity"), "badge")}">{esc(i.get("severity", "—"))}</span>',
            f'<span class="mono nowrap">{esc(i.get("kind", "—"))}</span>',
            esc(
                f"{i.get('source_subject', '')}: {i['title']}"
                if i.get("title")
                else i.get("source_subject")
            ),
            esc(i.get("reason", "—")),
            f'<span class="nowrap">{esc(duration(i.get("age_s")))}</span>',
        ]
        for i in items
    ]
    total = payload.get("total", len(items))
    count = f"{total} item{'' if total == 1 else 's'}" if total else ""
    return pane(
        "Attention",
        count,
        table(["Severity", "Kind", "Subject", "Reason", "Age"], rows, "nothing waiting on you"),
        full=True,
    )


def render_agents(payload: dict) -> str:
    agents = payload.get("agents") or []
    c = payload.get("counts") or {}
    rows = [
        [
            f'<span class="dot {esc(a.get("status", "unknown"))}"></span>{esc(a.get("label"))}',
            f'<span class="nowrap">{esc(a.get("status"))}</span>',
            f'<span class="nowrap">{esc(ago(a.get("last_seen")) + " ago" if a.get("last_seen") else "never")}</span>',
            f'<span class="mono">{esc(a.get("host") or "—")}</span>',
            esc(a.get("open_sessions", 0)),
            esc(a.get("active_tasks", 0)),
        ]
        for a in agents
    ]
    count = (
        f"{c.get('online', 0)} online · {c.get('idle', 0)} idle · "
        f"{c.get('stale', 0)} stale · {len(agents)} total"
    )
    return pane(
        "Agent activity",
        count,
        table(
            ["Agent", "Status", "Last seen", "Host", "Sessions", "Tasks"],
            rows,
            "no agents observed yet",
        ),
    )


def render_queue(payload: dict) -> str:
    err = failed(payload)
    if err:
        return pane(
            "Queue & health", "", f'<p class="err">queue health query failed: {esc(err)}</p>'
        )
    from unified_agent import config

    payload = dict(payload)
    payload.setdefault("operator_only", payload.get(config.LEGACY_OPERATOR_QUEUE_KEY, []))
    c = payload.get("counts") or {}
    groups = [
        [dict(t, _s=label) for t in payload.get(key) or []]
        for key, label in (
            ("doing", "doing"),
            ("waiting_review", "review wait"),
            ("desktop_open", "open"),
            ("dispatch_ready", "dispatch ready"),
            ("interactive_only", "interactive only"),
            ("operator_only", "Operator only"),
        )
    ]
    active = [task for group in groups for task in group]
    # Give every nonempty lane a visible row before filling the remaining
    # table capacity. Otherwise 25 earlier tasks hide an entire lane.
    visible = [group[0] for group in groups if group]
    visible.extend(task for group in groups for task in group[1:])
    rows = [
        [
            f'<span class="mono">#{esc(t.get("id"))}</span>',
            f'<span class="badge {"warn" if t["_s"] == "doing" else "info"}">{esc(t["_s"])}</span>',
            f'<span class="nowrap">{esc(t.get("priority") or "med")}</span>',
            esc(t.get("title")),
            esc(t.get("project") or "unified-agent"),
        ]
        for t in visible[:25]
    ]
    summary = chips(
        [
            ("doing", c.get("doing", 0)),
            ("open", c.get("desktop_open", 0)),
            ("dispatch ready", c.get("dispatch_ready", 0)),
            ("interactive only", c.get("interactive_only", 0)),
            ("Operator only", c.get("operator_only", c.get(config.LEGACY_OPERATOR_QUEUE_KEY, 0))),
            ("review wait", c.get("waiting_review", 0)),
            ("stale claims", c.get("stale_claims", 0)),
            ("blocked/archived", c.get("blocked_archived", 0)),
        ],
        warn_keys=frozenset({"stale claims"}),
    )
    return pane(
        "Queue & health",
        f"{len(active)} active",
        summary
        + table(["ID", "Status", "Priority", "Title", "Project"], rows, "no active desktop tasks"),
    )


def render_sessions(payload: dict) -> str:
    sessions = payload.get("sessions") or []
    rows = [
        [
            f'<span class="dot {"online" if s.get("status") in ("starting", "running", "waiting") else "offline"}"></span>{esc(s.get("agent"))}',
            f'<span class="mono">{esc(s.get("kind"))}</span>',
            f'<span class="nowrap">{esc(s.get("status"))}</span>',
            f'<span class="nowrap">{esc(duration(s.get("duration_s")))}</span>',
            esc(f"#{s['task_id']}" if s.get("task_id") else "—"),
        ]
        for s in sessions
    ]
    return pane(
        "Sessions",
        f"{payload.get('open', 0)} open · {len(sessions)} recent",
        table(["Agent", "Kind", "Status", "Duration", "Task"], rows, "no sessions recorded"),
    )


def render_anomalies(payload: dict) -> str:
    err = failed(payload)
    if err:
        return pane("Anomalies", "", f'<p class="err">{esc(err)}</p>')
    items = payload.get("anomalies") or []
    if not items:
        return pane("Anomalies", "", '<p class="empty">no anomalies</p>')
    rows = [
        [esc(a.get("kind") or a.get("rule") or "?"), esc(a.get("detail") or a.get("summary") or "")]
        for a in items
    ]
    return pane("Anomalies", str(len(items)), table(["Kind", "Detail"], rows, "no anomalies"))


def build(overview: dict, attention: dict, generated: dt.datetime) -> str:
    health = (overview.get("health") or {}).get("summary") or {}
    metrics = overview.get("metrics") or {}
    db = metrics.get("db_size_bytes")
    db_text = f"db {db / 1024:.0f}KB" if db else ""
    degraded = [
        k
        for k in (
            "agents",
            "sessions",
            "tasks",
            "queue_health",
            "approvals",
            "anomalies",
            "review_approval",
            "executor_delegation",
        )
        if isinstance(overview.get(k), dict) and failed(overview[k])
    ]
    if failed(attention):
        degraded.insert(0, "attention")
    banner = (
        f'<span class="pill" style="color:var(--danger);border-color:var(--danger)">'
        f"degraded — {esc(', '.join(degraded))}</span>"
        if degraded
        else '<span class="pill">snapshot</span>'
    )
    panes = "".join(
        [
            render_attention(attention),
            render_queue(overview.get("queue_health") or {}),
            render_agents(overview.get("agents") or {}),
            render_sessions(overview.get("sessions") or {}),
            render_anomalies(overview.get("anomalies") or {}),
        ]
    )
    return f"""<title>unified-agent control center</title>
<style>{CSS}</style>
<header>
  <h1>unified-agent <span style="color:var(--muted);font-weight:400">control center</span></h1>
  {banner}
  <span class="pill">{esc(generated.strftime("%Y-%m-%d %H:%M UTC"))}</span>
</header>
<main>{panes}</main>
<footer>
  <span>doctor {esc(health.get("live", 0))} live · {esc(health.get("dormant", 0))} dormant
        · <span style="color:var(--idle)">{esc(health.get("degraded", 0))} degraded</span>
        · {esc(health.get("misconfigured", 0))} misconfigured</span>
  <span>{esc(db_text)}</span>
  <span>snapshot — not a live view; re-render to refresh</span>
</footer>"""


async def collect() -> tuple[dict, dict]:
    overview = await api.overview_payload()
    try:
        attention = await api.attention_payload()
    except Exception as exc:  # noqa: BLE001 - reported in-band, same as the web UI
        attention = {"error": str(exc)[:200], "items": []}
    return overview, attention


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="dashboard.html", help="path to write (default: %(default)s)"
    )
    args = parser.parse_args()

    overview, attention = asyncio.run(collect())
    page = build(overview, attention, dt.datetime.now(dt.UTC))
    out = Path(args.out)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(page):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
