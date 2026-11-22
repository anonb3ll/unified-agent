"""Escaped plan-review HTML and the fixed ``plan-review v1`` intent schema.

Hermes desktop already frames ``::preview{file=…}`` and routes
``data-hermes-send`` / ``window.hermes.send`` as a hidden user turn (capped at
500 characters). This module is the hub side of that contract: render a plan
as trusted chrome plus escaped untrusted text, and parse only the three
fixed intents. It does not talk to Electron and does not store a second
approval state.
"""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from ..safety import resolve_in_workspace

INTENT_MAX_CHARS = 500
PREVIEW_DIR = ".unified-agent/plan-preview"
PLAN_ID_RE = re.compile(r"^[0-9a-f]{12}$")
INTENT_RE = re.compile(
    r"^plan-review v1 (approve|changes|annotate) ([0-9a-f]{12})"
    r"(?: (\d+)(?: (.*))?)?\Z"
)
PLAN_REF_RE = re.compile(r"\bplan[:\s#]+`?([0-9a-f]{6,32})`?", re.IGNORECASE)
ANNOTATION_RE = re.compile(r"\[plan-annotation:([0-9a-f]{12})\] @ [^\n]*?: (.*)")
WAITING_FOR_PLAN_APPROVAL = "[waiting-for: plan-approval]"
APPROVED_MARKER = "[plan: approved @"
CHANGES_MARKER = "[plan: changes-requested @"

# Fixed chrome. No plan text is interpolated into this script — it reads
# validated attributes at click time and hands a schema-shaped prompt to
# window.hermes.send (injected by the desktop iframe).
_ANNOTATE_SCRIPT = """<script>
(function(){
  var root=document.querySelector("[data-plan-id]");
  if(!root)return;
  var id=root.getAttribute("data-plan-id")||"";
  if(!/^[0-9a-f]{12}$/.test(id))return;
  document.addEventListener("click",function(e){
    var btn=e.target&&e.target.closest&&e.target.closest("[data-plan-annotate]");
    if(!btn)return;
    e.preventDefault();
    var idx=btn.getAttribute("data-plan-annotate")||"";
    if(!/^\\d+$/.test(idx))return;
    var raw=window.prompt("Comment on section "+idx);
    if(!raw||!String(raw).trim())return;
    var comment=String(raw).replace(/[\\r\\n\\u0000-\\u001f]+/g," ").trim().slice(0,400);
    if(window.hermes&&typeof window.hermes.send==="function"){
      window.hermes.send("plan-review v1 annotate "+id+" "+idx+" "+comment);
    }
  },true);
})();
</script>
"""

_PREVIEW_STYLE = """<style>
article{font:14px/1.45 system-ui,sans-serif;color:var(--foreground,inherit);
  background:var(--card,transparent);border:1px solid var(--border,rgba(127,127,127,.35));
  border-radius:8px;padding:12px 14px;max-width:40rem}
h1{font-size:1.05rem;margin:0 0 8px}
.goal{margin:0 0 12px;color:var(--muted-foreground,inherit)}
ol{margin:0;padding-left:1.4rem}
li{margin:0 0 10px}
.meta{font-size:12px;color:var(--muted-foreground,inherit)}
.comment{margin:6px 0 0;padding:6px 8px;border-left:3px solid var(--accent,#888)}
.actions{display:flex;gap:8px;margin-top:12px}
button{font:inherit;cursor:pointer;padding:6px 10px;border-radius:6px;
  border:1px solid var(--border,rgba(127,127,127,.45));background:transparent;
  color:inherit}
button.primary{background:var(--accent,transparent)}
.status{font-size:12px;margin-top:8px}
</style>
"""


def require_plan_id(plan_id: str) -> str:
    pid = (plan_id or "").strip().lower()
    if not PLAN_ID_RE.fullmatch(pid):
        raise ToolError("plan_id must be a 12-character hex id")
    return pid


def preview_relpath(plan_id: str) -> str:
    return f"{PREVIEW_DIR}/{require_plan_id(plan_id)}.html"


def preview_directive(plan_id: str) -> str:
    return f'::preview{{file="{preview_relpath(plan_id)}"}}'


def plan_state_fingerprint(plan_data: dict) -> str:
    """Same digest the plan-approval notifier uses (updated_at + step status/owner)."""
    plan = plan_data.get("plan") or {}
    parts = [str(plan.get("updated_at") or ""), str(plan.get("status") or "")]
    for step in plan_data.get("steps") or []:
        parts.append(f"{step.get('step_idx')}:{step.get('status')}:{step.get('owner')}")
    return hashlib.blake2s("|".join(parts).encode(), digest_size=6).hexdigest()


def annotation_id(plan_id: str, step_idx: int, comment: str) -> str:
    payload = f"{plan_id}|{step_idx}|{comment}".encode()
    return hashlib.blake2s(payload, digest_size=6).hexdigest()


def parse_review_intent(intent: str) -> dict:
    """Parse a Hermes ``hermes.send`` payload. Refuse anything off-schema."""
    if not isinstance(intent, str):
        raise ToolError("intent must be a string")
    raw = intent.strip()
    if not raw:
        raise ToolError("intent must not be empty")
    if len(raw) > INTENT_MAX_CHARS:
        raise ToolError(f"intent exceeds {INTENT_MAX_CHARS} characters")
    match = INTENT_RE.match(raw)
    if match is None:
        raise ToolError("intent must match 'plan-review v1 <approve|changes|annotate> <plan_id> …'")
    action, plan_id, step_raw, comment = match.groups()
    if action in {"approve", "changes"}:
        if step_raw is not None:
            raise ToolError("intent must match 'plan-review v1 <approve|changes> <plan_id>'")
        return {"action": action, "plan_id": plan_id}
    if step_raw is None or comment is None or not comment.strip():
        raise ToolError("annotate intent needs a step index and a non-empty comment")
    cleaned = re.sub(r"[\x00-\x1f]+", " ", comment).strip()
    if not cleaned:
        raise ToolError("annotate intent needs a step index and a non-empty comment")
    return {
        "action": "annotate",
        "plan_id": plan_id,
        "step_idx": int(step_raw),
        "comment": cleaned,
    }


def plan_id_from_notes(notes: str) -> str | None:
    match = PLAN_REF_RE.search(notes or "")
    return match.group(1) if match else None


def _esc(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def _annotations(note: str | None) -> list[str]:
    if not note:
        return []
    return [match.group(2) for match in ANNOTATION_RE.finditer(note)]


def render_plan_preview_html(plan_data: dict, *, approved: bool = False) -> str:
    """Trusted chrome + escaped plan body. No plan text is emitted as markup."""
    plan = plan_data.get("plan") or {}
    plan_id = require_plan_id(str(plan.get("id") or ""))
    fingerprint = plan_state_fingerprint(plan_data)
    steps = list(plan_data.get("steps") or [])
    items = []
    for step in steps:
        idx = int(step.get("step_idx", 0))
        comments = "".join(
            f'<p class="comment">{_esc(comment)}</p>' for comment in _annotations(step.get("note"))
        )
        items.append(
            "<li>"
            f"<div><strong>Section {idx + 1}</strong> "
            f'<span class="meta">{_esc(step.get("status"))}'
            f"{' · ' + _esc(step.get('owner')) if step.get('owner') else ''}</span></div>"
            f"<div>{_esc(step.get('description'))}</div>"
            f'<button type="button" data-plan-annotate="{idx}">Comment</button>'
            f"{comments}"
            "</li>"
        )
    if approved:
        actions = '<p class="status">Approved via the plan-confirmation gate.</p>'
    else:
        actions = (
            '<div class="actions">'
            f'<button type="button" class="primary" data-hermes-send="plan-review v1 approve {plan_id}">Approve</button>'
            f'<button type="button" data-hermes-send="plan-review v1 changes {plan_id}">Request changes</button>'
            "</div>"
        )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<title>Plan review {_esc(plan_id)}</title>{_PREVIEW_STYLE}</head><body>"
        f'<article data-plan-id="{plan_id}" data-fingerprint="{fingerprint}">'
        "<h1>Plan review</h1>"
        f'<p class="goal">{_esc(plan.get("goal"))}</p>'
        f"<ol>{''.join(items)}</ol>"
        f"{actions}</article>"
        f"{_ANNOTATE_SCRIPT}</body></html>"
    )


def write_preview_html(plan_id: str, html_doc: str) -> Path:
    rel = preview_relpath(plan_id)
    path = resolve_in_workspace(rel, protect_configured_hooks=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_doc, encoding="utf-8")
    return path
