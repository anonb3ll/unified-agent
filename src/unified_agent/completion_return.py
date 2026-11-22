"""Completion return: tell the thread that asked when its task finishes (task #1751).

A task created from a chat thread carries a ``task_origins`` row: the surface
(``slack``, ``zulip`` or ``messages``), channel, thread and requester, taken
from the gateway's authenticated request, never from model-supplied text.
When any status writer moves such a task to done, blocked or cancelled it
appends one ``completion_deliveries`` row in the same transaction, keyed by
``(task_id, revision)`` so a transition is queued exactly once. The one hook
is ``run_records.queue_completion_return``: ``task_update`` and the merge
closeout reach it through ``record_transition_in_tx``, and
``complete_direct`` (close-on-merge, the merge orchestrator, the reviewer
auto-close) calls it from its own write. Moving the task back out of one of
those statuses supersedes any row not yet delivered.

:func:`drain_pending` sends the rows. It runs after the commit (on the
existing Slack outbound worker pool), at hub start-up (a background post-init
hook) and on the queue-hygiene tick. It coalesces a task's pending rows into
one send of the latest status, never resends a delivered row, retries a
failed send a bounded number of times and then records it as abandoned. A
row older than ``DELIVERY_MAX_AGE_HOURS`` is abandoned as ``expired`` and
never sent, so a surface enabled late does not flood old threads. A surface
whose sender is not configured keeps its rows pending until then; the drain
never raises.

The message holds only the task id, the neutralised title, the new status, the
``evidence_ref`` when it is an https URL or a commit SHA, and a Control Center
link when ``UA_PUBLIC_URL`` is set. Never note text.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, storage
from .telemetry.redaction import redact_sensitive_content

log = logging.getLogger(__name__)

SURFACES = ("slack", "zulip", "messages")
# Statuses the requester hears about. done and cancelled are terminal;
# blocked is not, but it is the one mid-flight state a requester must act on.
NOTIFY_STATUSES = ("done", "blocked", "cancelled")
MAX_ATTEMPTS = 5
DRAIN_LIMIT = 50
TITLE_MAX_CHARS = 120
MAX_ORIGIN_FIELD_CHARS = 200
# A claimed row older than this belongs to a drain that died mid-send.
CLAIM_STALE_S = 600
RETRY_BASE_S = 60
RETRY_MAX_S = 3600
DEFERRED_RETRY_S = 60
# A row not delivered within this window is abandoned as expired, never sent:
# a notice a day late is noise, and a surface enabled after a backlog built up
# must not flood old threads.
DELIVERY_MAX_AGE_HOURS = 24
# A Slack slot reserved with no ledger entry (a send that died after
# reserving) defers every retry. After this many deferrals the row is
# abandoned as "slack slot stuck" instead of waiting forever.
MAX_STUCK_DEFERRALS = 10
SLACK_DEDUPE_PREFIX = "completion-return"
# A messages-surface notice is sent with message_id "<prefix>-<delivery id>", so a
# resend after a lost "delivered" mark is a recorded duplicate (task #1993).
MESSAGES_ID_PREFIX = "completion-return"
# Hosts an https evidence_ref may link to. The UA_PUBLIC_URL host is added at
# render time; any other https URL is shown as code, not as a link.
EVIDENCE_LINK_HOSTS = frozenset({"github.com"})

_PENDING_SQL = "delivered_at IS NULL AND superseded_at IS NULL AND abandoned_at IS NULL"
_PENDING_SQL_D = "d.delivered_at IS NULL AND d.superseded_at IS NULL AND d.abandoned_at IS NULL"
_TENANT_SQL = "org_id IS ?"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# No whitespace, no '<', '>', '|', '`', '@', brackets or parentheses: an
# evidence URL can neither close a Slack link nor open a markdown one.
_URL_RE = re.compile(r"^https?://[A-Za-z0-9._~:/?#!$&'*+,;=%-]{1,300}$")
_HTTPS_URL_RE = re.compile(r"^https://[A-Za-z0-9._~:/?#!$&'*+,;=%-]{1,300}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ZWSP = "​"


@dataclass(frozen=True)
class Origin:
    """Where a task was asked for. Built by a gateway from its verified request."""

    surface: str
    channel: str
    thread: str
    requester: str

    def __post_init__(self) -> None:
        if self.surface not in SURFACES:
            raise ToolError(f"origin surface must be one of {SURFACES}")
        for name in ("channel", "thread", "requester"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ToolError(f"origin {name} must be a non-empty string")
            if len(value) > MAX_ORIGIN_FIELD_CHARS or _CONTROL_RE.search(value):
                raise ToolError(
                    f"origin {name} must be at most {MAX_ORIGIN_FIELD_CHARS} printable characters"
                )


# The origin a gateway attaches to the task_add it is about to make. A
# ContextVar rather than a task_add argument, so no tool caller (and no model)
# can supply one: only in-process gateway code that set it can.
_creation_origin: ContextVar[Origin | None] = ContextVar("completion_creation_origin", default=None)


@contextmanager
def creation_origin(origin: Origin | None) -> Iterator[None]:
    """Attach ``origin`` to the ``task_add`` calls made inside this block.

    ``task_add`` records it in the same transaction as the task row.
    """
    token = _creation_origin.set(origin)
    try:
        yield
    finally:
        _creation_origin.reset(token)


def pending_creation_origin() -> Origin | None:
    return _creation_origin.get()


def _now() -> str:
    return storage.now()


def record_origin_on_connection(conn: Any, task_id: int, origin: Origin) -> bool:
    """Insert the origin inside the caller's transaction. First origin wins."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO task_origins "
        "(task_id, surface, channel, thread, requester, created_at, org_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            int(task_id),
            origin.surface,
            origin.channel,
            origin.thread,
            origin.requester,
            _now(),
            edition.tenant_org_id(),
        ),
    )
    return cur.rowcount == 1


def record_origin(task_id: int, surface: str, channel: str, thread: str, requester: str) -> dict:
    """Record where an existing task was asked for, so its completion is returned there.

    For adapters that create or link a task outside ``task_add``'s gateway
    hook, such as the Control Center conversation handoff (#1426). The
    caller must pass values from its own authenticated context (the verified
    conversation and requester), never model-supplied ids. One origin per
    task: a task that already has one keeps it (``recorded: false``). A task
    outside the caller's tenant reads as missing.
    """
    origin = Origin(surface=surface, channel=channel, thread=thread, requester=requester)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        visible = conn.execute(
            f"SELECT 1 FROM tasks WHERE id=? AND {_TENANT_SQL}",
            (int(task_id), edition.tenant_org_id()),
        ).fetchone()
        if visible is None:
            conn.rollback()
            raise ToolError(f"no task with id {task_id}")
        recorded = record_origin_on_connection(conn, int(task_id), origin)
        conn.commit()
    finally:
        conn.close()
    return {"task_id": int(task_id), "surface": origin.surface, "recorded": recorded}


def get_origin(task_id: int) -> dict | None:
    """The task's origin in the caller's tenant, or None."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT task_id, surface, channel, thread, requester, created_at FROM task_origins "
            f"WHERE task_id=? AND {_TENANT_SQL}",
            (int(task_id), edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def record_transition_on_connection(
    conn: Any, *, task_id: int, old_status: str | None, new_status: str, revision: str
) -> bool:
    """Queue or supersede deliveries for one status change, in the caller's transaction.

    Called by ``task_update`` before it commits, so a rolled-back update
    leaves no row. Returns True when a delivery row was queued.
    """
    if new_status == old_status:
        return False
    tenant = edition.tenant_org_id()
    if new_status in NOTIFY_STATUSES:
        origin = conn.execute(
            f"SELECT 1 FROM task_origins WHERE task_id=? AND {_TENANT_SQL}", (task_id, tenant)
        ).fetchone()
        if origin is None:
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO completion_deliveries "
            "(task_id, revision, status, created_at, org_id) VALUES (?, ?, ?, ?, ?)",
            (task_id, revision, new_status, _now(), tenant),
        )
        return cur.rowcount == 1
    if old_status in NOTIFY_STATUSES:
        # Reopened: whatever was waiting to go out is no longer true.
        conn.execute(
            "UPDATE completion_deliveries SET superseded_at=?, last_error=? "
            f"WHERE task_id=? AND {_PENDING_SQL} AND {_TENANT_SQL}",
            (_now(), f"task moved to {new_status}", task_id, tenant),
        )
    return False


# --- message rendering ----------------------------------------------------------


def _escape_slack(text: str) -> str:
    """Slack's three control characters. '&' first."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


#: Invisible code points outside category Cf that can still split a token:
#: Unicode default-ignorables (combining grapheme joiner, Hangul fillers,
#: variation selectors, Mongolian selectors) plus the braille blank.
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _invisible(c: str) -> bool:
    if unicodedata.category(c) == "Cf":
        return True
    point = ord(c)
    return any(low <= point <= high for low, high in _INVISIBLE_RANGES)


def neutralise_title(title: str | None, *, surface: str) -> str:
    """A title that can neither ping nor link, as an inline code span.

    Unicode format characters (bidirectional overrides, zero-width joiners)
    are dropped first, so the title cannot reorder or hide text and cannot
    split a secret past the redactor; then secrets are redacted, whitespace collapses to one line and the text
    is truncated. Backticks cannot close the span, ``@`` and ``://`` get a
    zero-width space so no mention or URL is recognised, and Slack's control
    characters are escaped so ``<!here>`` or ``<url|text>`` stays text.
    """
    # Strip format characters BEFORE redacting: a zero-width character inside a
    # token would otherwise hide it from the pattern, and stripping it later
    # would rejoin the token in the output.
    text = "".join(c for c in (title or "") if not _invisible(c))
    text = redact_sensitive_content(text, max_len=4000)
    text = " ".join(text.split())
    if len(text) > TITLE_MAX_CHARS:
        text = text[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    text = text.replace("`", "'").replace("@", "@" + _ZWSP).replace("://", ":" + _ZWSP + "//")
    if surface == "slack":
        text = _escape_slack(text)
    return f"`{text or '(untitled)'}`"


def _public_base() -> str | None:
    base = os.environ.get("UA_PUBLIC_URL", "").strip().rstrip("/")
    if not base or not _URL_RE.match(base) or "?" in base or "#" in base:
        return None
    return base


def _evidence_link_hosts() -> frozenset[str]:
    hosts = set(EVIDENCE_LINK_HOSTS)
    base = _public_base()
    if base:
        host = urlsplit(base).hostname
        if host:
            hosts.add(host.lower())
    return frozenset(hosts)


def _evidence(ref: str | None, *, surface: str) -> str | None:
    """A commit SHA or an https URL; anything else is dropped.

    An https URL on an allowlisted host (``EVIDENCE_LINK_HOSTS`` plus the
    ``UA_PUBLIC_URL`` host) is shown as a link. Any other https URL is shown
    inside a code span, so the requester sees it but it is not a link.
    """
    value = (ref or "").strip()
    if not value or redact_sensitive_content(value, max_len=4000) != value:
        return None
    if _SHA_RE.match(value):
        return f"`{value}`"
    if not _HTTPS_URL_RE.match(value):
        return None
    try:
        host = (urlsplit(value).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    shown = _escape_slack(value) if surface == "slack" else value
    if host in _evidence_link_hosts():
        return shown
    return f"`{shown}`"


def control_center_link(task_id: int, *, surface: str = "slack") -> str | None:
    """Deep link to the task in the Control Center, when a public base URL is set."""
    base = _public_base()
    if base is None:
        return None
    link = f"{base}/ui/runs/insights?task={int(task_id)}"
    return _escape_slack(link) if surface == "slack" else link


def render_message(
    *, task_id: int, title: str | None, status: str, evidence_ref: str | None, surface: str
) -> str:
    parts = [f"task-{int(task_id)} is {status}: {neutralise_title(title, surface=surface)}"]
    evidence = _evidence(evidence_ref, surface=surface)
    if evidence:
        parts.append(f"evidence: {evidence}")
    link = control_center_link(task_id, surface=surface)
    if link:
        parts.append(link)
    return " | ".join(parts)


# --- delivery -------------------------------------------------------------------


@dataclass(frozen=True)
class SendOutcome:
    """``delivered``, ``deferred`` (try later, no attempt counted), ``failed``, or
    ``skipped`` (terminal: the row can never be delivered, so it is not retried).

    ``stuck`` marks a deferral caused by a reserved Slack slot with no ledger
    entry; those are counted, and enough of them abandon the row.
    """

    result: str
    error: str | None = None
    stuck: bool = False


def _slack_config():
    from .slack_config import SlackConfig

    return SlackConfig.from_env()


def _zulip_report_agent() -> str:
    return os.environ.get("UA_ZULIP_REPORT_AGENT", "").strip()


def _messages_report_agent() -> str:
    """The service label that posts ``messages`` notices (``UA_MESSAGES_REPORT_AGENT``).

    Empty, or a label the roster does not know, means the surface has no sender.
    """
    label = os.environ.get("UA_MESSAGES_REPORT_AGENT", "").strip()
    return label if label and config.is_known_agent_label(label) else ""


def deliverable_surfaces() -> frozenset[str]:
    """Surfaces whose sender is configured now. Others keep their rows pending.

    Slack needs the gateway enabled, a bot token and ``UA_SLACK_REPLY=1`` (the
    same opt-in as inbound acks: a completion return is an in-thread reply).
    Zulip needs outbound un-retired and ``UA_ZULIP_REPORT_AGENT``. The
    ``messages`` surface (Control Center handoffs, #1426 / #1993) needs
    ``UA_MESSAGES_REPORT_AGENT`` set to a label on the roster.
    """
    surfaces: set[str] = set()
    try:
        from . import slack_outbound

        cfg = _slack_config()
        if slack_outbound.replies_enabled() and cfg.enabled and (cfg.bot_token or "").strip():
            surfaces.add("slack")
    except Exception:  # noqa: BLE001 - a broken config is "not configured"
        log.debug("Slack completion return not configured", exc_info=True)
    try:
        from .tools import zulip

        if not zulip.zulip_retired() and _zulip_report_agent():
            surfaces.add("zulip")
    except Exception:  # noqa: BLE001
        log.debug("Zulip completion return not configured", exc_info=True)
    try:
        if _messages_report_agent():
            surfaces.add("messages")
    except Exception:  # noqa: BLE001
        log.debug("Messages completion return not configured", exc_info=True)
    return frozenset(surfaces)


def _send_slack(origin: dict[str, Any], text: str, delivery_id: int, post_fn=None) -> SendOutcome:
    from . import slack_outbound

    cfg = _slack_config()
    try:
        sent = slack_outbound.send_message(
            channel_id=origin["channel"],
            text=text,
            agent_label=cfg.coordinator_agent,
            thread_ts=origin["thread"],
            event_ts=origin["thread"],
            dedupe_key=f"{SLACK_DEDUPE_PREFIX}:{delivery_id}",
            config=cfg,
            post_fn=post_fn,
            timeout_s=slack_outbound.MIRROR_TIMEOUT_S,
        )
    except slack_outbound.SlackOutboundError as exc:
        # Fixed operator messages (disabled, not allowlisted, too long): no secrets.
        return SendOutcome("failed", str(exc)[:200])
    if sent.get("sent"):
        return SendOutcome("delivered")
    if sent.get("duplicate"):
        if sent.get("event_ulid") is not None:
            # This key was delivered before (a crash between send and mark).
            return SendOutcome("delivered")
        # Mid-send elsewhere, or a send that died: at-most-once, so wait; an
        # operator frees an abandoned slot with unified-agent-slack-slots.
        return SendOutcome("deferred", "slack send in flight", stuck=True)
    if sent.get("reason") == "rate-limited":
        return SendOutcome("deferred", "slack rate-limited")
    detail = sent.get("error") or sent.get("error_type") or ""
    return SendOutcome("failed", f"slack {sent.get('reason') or 'error'} {detail}".strip()[:200])


def _send_zulip(origin: dict[str, Any], text: str) -> SendOutcome:
    """At-least-once: Zulip's send API takes no dedupe key, so a crash between
    the send and the "delivered" mark resends the notice on the next drain."""
    from .tools import zulip

    try:
        result = zulip.zulip_send_message(
            _zulip_report_agent(), origin["channel"], origin["thread"], text
        )
    except ToolError as exc:
        # The message can name a credential file path; keep the type only.
        return SendOutcome("failed", f"zulip {type(exc).__name__}")
    if isinstance(result, dict) and result.get("sent"):
        return SendOutcome("delivered")
    error = result.get("error") if isinstance(result, dict) else None
    return SendOutcome("failed", f"zulip {error or 'send failed'}"[:200])


def _send_messages(
    origin: dict[str, Any], text: str, delivery_id: int, task_id: int
) -> SendOutcome:
    """Post the notice into the origin's conversation, to the requester.

    At most once per row: the hub message id is derived from the row id, so a
    resend after a lost "delivered" mark returns the recorded message instead
    of posting a second one. The send runs as the service label, never as
    whichever caller's request scheduled the drain, and carries ``task_id`` so
    the mailbox index links the notice to its task.

    A requester hub mail cannot be addressed to is a terminal ``skipped``, not a
    failure retried until it is abandoned. That is the human operator's login on
    a closed roster: ``config.operator_label()`` is deliberately never a roster
    label, and the hub has no other operator inbox to post a notice to (the
    Control Center's attention items are derived from task state, not
    messages). On an open roster the operator label is addressable, and the
    notice is delivered into the conversation like any other.
    """
    from .messaging import send as messaging_send

    label = _messages_report_agent()
    if not label:
        return SendOutcome("deferred", "messages sender not configured")
    requester = str(origin.get("requester") or "")
    if not config.is_known_agent_label(requester):
        if requester == config.operator_label():
            return SendOutcome("skipped", "skipped: operator recipient")
        return SendOutcome("skipped", "skipped: recipient not on the roster")
    token = config.agent_identity_var.set(None)
    try:
        sent = messaging_send.send_completion_notice(
            body=text,
            delivery_id=int(delivery_id),
            task_id=int(task_id),
        )
    except messaging_send.CompletionNoticeStale:
        # The task moved after the pre-send check: no attempt is counted, and
        # the next drain supersedes the row (or a reopen already has).
        return SendOutcome("deferred", "task changed during the send")
    except messaging_send.CompletionNoticeIdTaken:
        # Another message holds the notice id (another sender's receipt, or an
        # indexed message with none): it can never become this notice.
        return SendOutcome("skipped", "skipped: notice id holds another message")
    except messaging_send.CompletionNoticeRefused:
        # The handoff authority does not hold, and a retry cannot change that.
        return SendOutcome("skipped", "skipped: not authorized by its conversation handoff")
    except ToolError as exc:
        # Fixed hub messages (unknown label, comms policy, id clash): no secrets.
        return SendOutcome("failed", f"messages {exc}"[:200])
    finally:
        config.agent_identity_var.reset(token)
    expected = {
        "act": "inform",
        "from_agent": label,
        "to_agent": requester,
        "conversation_id": origin.get("thread"),
        "task_id": int(task_id),
        "message_id": f"{MESSAGES_ID_PREFIX}-{int(delivery_id)}",
    }
    if sent.get("accepted") and all(sent.get(k) == v for k, v in expected.items()):
        return SendOutcome("delivered")
    if sent.get("duplicate"):
        # The notice id's receipt belongs to another message: it can never
        # become this notice, so a retry would only replay the mismatch.
        return SendOutcome("skipped", "skipped: notice id holds another message")
    return SendOutcome("failed", "messages send not accepted")


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _retry_delay_s(attempts: int) -> int:
    return min(RETRY_BASE_S * (2 ** max(attempts - 1, 0)), RETRY_MAX_S)


def _claim_latest(
    conn: Any, task_id: int, tenant: str | None, now: datetime, result: dict[str, list]
) -> dict[str, Any] | None:
    """Coalesce a task's pending rows and claim the newest one, in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT id, status, revision, attempts, deferrals, next_attempt_at, claimed_at "
            f"FROM completion_deliveries WHERE task_id=? AND {_PENDING_SQL} AND {_TENANT_SQL} "
            "ORDER BY revision DESC, id DESC",
            (task_id, tenant),
        ).fetchall()
        if not rows:
            conn.commit()
            return None
        latest, older = rows[0], rows[1:]
        now_iso = _iso(now)
        for row in older:
            conn.execute(
                "UPDATE completion_deliveries SET superseded_at=?, last_error=? WHERE id=?",
                (now_iso, f"coalesced into delivery {latest['id']}", row["id"]),
            )
            result["superseded"].append(row["id"])
        stale = _iso(now - timedelta(seconds=CLAIM_STALE_S))
        if (latest["next_attempt_at"] and latest["next_attempt_at"] > now_iso) or (
            latest["claimed_at"] and latest["claimed_at"] > stale
        ):
            conn.commit()
            result["waiting"].append(latest["id"])
            return None
        task = conn.execute(
            f"SELECT id, title, status, evidence_ref FROM tasks WHERE id=? AND {_TENANT_SQL}",
            (task_id, tenant),
        ).fetchone()
        origin = conn.execute(
            "SELECT surface, channel, thread, requester FROM task_origins "
            f"WHERE task_id=? AND {_TENANT_SQL}",
            (task_id, tenant),
        ).fetchone()
        if task is None or origin is None:
            conn.execute(
                "UPDATE completion_deliveries SET abandoned_at=?, last_error=? WHERE id=?",
                (now_iso, "task or origin missing", latest["id"]),
            )
            conn.commit()
            result["abandoned"].append(latest["id"])
            return None
        if task["status"] != latest["status"]:
            # Moved by a writer other than task_update (a claim, hygiene):
            # the queued status is no longer true, so it is not sent.
            conn.execute(
                "UPDATE completion_deliveries SET superseded_at=?, last_error=? WHERE id=?",
                (now_iso, f"task is now {task['status']}", latest["id"]),
            )
            conn.commit()
            result["superseded"].append(latest["id"])
            return None
        conn.execute(
            "UPDATE completion_deliveries SET claimed_at=? WHERE id=?", (now_iso, latest["id"])
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "id": latest["id"],
        "attempts": int(latest["attempts"]),
        "deferrals": int(latest["deferrals"]),
        "status": latest["status"],
        "task": dict(task),
        "origin": dict(origin),
    }


def _record_outcome(conn: Any, claim: dict[str, Any], outcome: SendOutcome, now: datetime) -> str:
    """Record one send's outcome. Every update is guarded on ``superseded_at IS
    NULL``: a reopen that superseded the row mid-send wins, and the row is
    reported as superseded rather than delivered."""
    now_iso = _iso(now)
    delivery_id = claim["id"]
    if outcome.result == "skipped":
        cur = conn.execute(
            "UPDATE completion_deliveries SET abandoned_at=?, claimed_at=NULL, last_error=? "
            "WHERE id=? AND superseded_at IS NULL",
            (now_iso, outcome.error or "skipped", delivery_id),
        )
        conn.commit()
        return "skipped" if cur.rowcount == 1 else "superseded"
    if outcome.result == "delivered":
        cur = conn.execute(
            "UPDATE completion_deliveries SET delivered_at=?, claimed_at=NULL, last_error=NULL "
            "WHERE id=? AND superseded_at IS NULL",
            (now_iso, delivery_id),
        )
        conn.commit()
        return "delivered" if cur.rowcount == 1 else "superseded"
    if outcome.result == "deferred":
        deferrals = claim["deferrals"] + (1 if outcome.stuck else 0)
        if deferrals >= MAX_STUCK_DEFERRALS:
            cur = conn.execute(
                "UPDATE completion_deliveries SET deferrals=?, claimed_at=NULL, last_error=?, "
                "abandoned_at=? WHERE id=? AND superseded_at IS NULL",
                (deferrals, "slack slot stuck", now_iso, delivery_id),
            )
            conn.commit()
            if cur.rowcount != 1:
                return "superseded"
            log.warning(
                "completion return for task %s abandoned: slack slot stuck after %d deferrals",
                claim["task"]["id"],
                deferrals,
            )
            return "abandoned"
        cur = conn.execute(
            "UPDATE completion_deliveries SET deferrals=?, claimed_at=NULL, last_error=?, "
            "next_attempt_at=? WHERE id=? AND superseded_at IS NULL",
            (
                deferrals,
                outcome.error,
                _iso(now + timedelta(seconds=DEFERRED_RETRY_S)),
                delivery_id,
            ),
        )
        conn.commit()
        return "waiting" if cur.rowcount == 1 else "superseded"
    attempts = claim["attempts"] + 1
    abandoned = now_iso if attempts >= MAX_ATTEMPTS else None
    cur = conn.execute(
        "UPDATE completion_deliveries SET attempts=?, last_error=?, claimed_at=NULL, "
        "next_attempt_at=?, abandoned_at=? WHERE id=? AND superseded_at IS NULL",
        (
            attempts,
            outcome.error or "send failed",
            _iso(now + timedelta(seconds=_retry_delay_s(attempts))),
            abandoned,
            delivery_id,
        ),
    )
    conn.commit()
    if cur.rowcount != 1:
        return "superseded"
    if abandoned:
        log.warning(
            "completion return for task %s abandoned after %d attempts: %s",
            claim["task"]["id"],
            attempts,
            outcome.error,
        )
        return "abandoned"
    return "failed"


def _expire_old(conn: Any, tenant: str | None, now: datetime, task_id: int | None) -> list[int]:
    """Abandon pending rows older than ``DELIVERY_MAX_AGE_HOURS`` as ``expired``."""
    cutoff = _iso(now - timedelta(hours=DELIVERY_MAX_AGE_HOURS))
    # A row another drain claimed recently may be mid-send: leave it to that
    # drain rather than marking a row both abandoned and delivered.
    stale = _iso(now - timedelta(seconds=CLAIM_STALE_S))
    narrow, params = "", [tenant, cutoff, stale]
    if task_id is not None:
        narrow = " AND task_id=?"
        params.append(int(task_id))
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = [
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM completion_deliveries WHERE {_PENDING_SQL} AND {_TENANT_SQL} "
                f"AND created_at < ? AND (claimed_at IS NULL OR claimed_at <= ?){narrow} "
                "ORDER BY id",
                params,
            ).fetchall()
        ]
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(
                "UPDATE completion_deliveries SET abandoned_at=?, claimed_at=NULL, "
                f"last_error='expired' WHERE id IN ({marks})",
                (_iso(now), *ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if ids:
        log.info("completion return: %d undelivered notices expired", len(ids))
    return ids


def _superseded_before_send(
    conn: Any, claim: dict[str, Any], tenant: str | None, now: datetime
) -> bool:
    """Re-check the task right before sending: a reopen since the claim wins.

    Marks the claimed row superseded and returns True when the task's status
    is no longer the one queued (or the task is gone), or when the row is no
    longer pending.
    """
    row = conn.execute(
        f"SELECT status FROM tasks WHERE id=? AND {_TENANT_SQL}", (claim["task"]["id"], tenant)
    ).fetchone()
    current = row["status"] if row is not None else None
    # The row itself too: a reopen and re-close since the claim leaves the
    # status as queued but supersedes this row in favour of a new one.
    live = conn.execute(
        f"SELECT 1 FROM completion_deliveries WHERE id=? AND {_PENDING_SQL}", (claim["id"],)
    ).fetchone()
    if current == claim["status"] and live is not None:
        return False
    conn.execute(
        "UPDATE completion_deliveries SET superseded_at=COALESCE(superseded_at, ?), "
        "claimed_at=NULL, last_error=? WHERE id=? AND delivered_at IS NULL",
        (_iso(now), f"task is now {current or 'missing'}", claim["id"]),
    )
    conn.commit()
    return True


def drain_pending(
    *,
    task_id: int | None = None,
    limit: int = DRAIN_LIMIT,
    now: datetime | None = None,
    slack_post_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send pending completion returns. Never raises.

    First, pending rows in the caller's tenant older than
    ``DELIVERY_MAX_AGE_HOURS`` are abandoned as ``expired`` (any surface), so
    they are never sent. Then only rows whose surface has a configured sender
    and that are due (not in backoff, not freshly claimed) are looked at; the
    rest stay pending untouched. ``task_id`` narrows the pass to one task (the
    post-commit drain). Returns the delivery ids per outcome.
    """
    result: dict[str, Any] = {
        "delivered": [],
        "failed": [],
        "abandoned": [],
        "expired": [],
        "superseded": [],
        "waiting": [],
        "skipped": [],
    }
    try:
        tenant = edition.tenant_org_id()
        current = now or datetime.now(UTC)
        surfaces = deliverable_surfaces()
        conn = storage.connect()
        try:
            result["expired"] = _expire_old(conn, tenant, current, task_id)
            if not surfaces:
                return result
            now_iso = _iso(current)
            stale = _iso(current - timedelta(seconds=CLAIM_STALE_S))
            placeholders = ",".join("?" * len(surfaces))
            params: list[Any] = [tenant, now_iso, stale, *sorted(surfaces)]
            narrow = ""
            if task_id is not None:
                narrow = " AND d.task_id=?"
                params.append(int(task_id))
            params.append(max(1, int(limit)))
            # Only due rows: one in backoff or freshly claimed by another
            # drain is skipped here, so a backlog of waiting rows cannot fill
            # the LIMIT and starve a new one.
            task_ids = [
                r["task_id"]
                for r in conn.execute(
                    "SELECT d.task_id FROM completion_deliveries d "
                    "JOIN task_origins o ON o.task_id = d.task_id AND o.org_id IS d.org_id "
                    f"WHERE {_PENDING_SQL_D} AND d.org_id IS ? "
                    "AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?) "
                    "AND (d.claimed_at IS NULL OR d.claimed_at <= ?) "
                    f"AND o.surface IN ({placeholders}){narrow} "
                    "GROUP BY d.task_id ORDER BY MIN(d.id) LIMIT ?",
                    params,
                ).fetchall()
            ]
            for tid in task_ids:
                claim = _claim_latest(conn, tid, tenant, current, result)
                if claim is None:
                    continue
                if _superseded_before_send(conn, claim, tenant, current):
                    result["superseded"].append(claim["id"])
                    continue
                text = render_message(
                    task_id=tid,
                    title=claim["task"]["title"],
                    status=claim["status"],
                    evidence_ref=claim["task"]["evidence_ref"],
                    surface=claim["origin"]["surface"],
                )
                try:
                    if claim["origin"]["surface"] == "slack":
                        outcome = _send_slack(claim["origin"], text, claim["id"], slack_post_fn)
                    elif claim["origin"]["surface"] == "messages":
                        outcome = _send_messages(claim["origin"], text, claim["id"], tid)
                    else:
                        outcome = _send_zulip(claim["origin"], text)
                except Exception as exc:  # noqa: BLE001 - one bad send must not stop the pass
                    outcome = SendOutcome("failed", f"{type(exc).__name__}")
                result[_record_outcome(conn, claim, outcome, current)].append(claim["id"])
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - every drain point is best-effort
        log.exception("completion return drain failed")
        result["error"] = type(exc).__name__
    return result


def schedule_drain(task_id: int | None = None) -> bool:
    """Drain on the Slack outbound worker pool, off the caller's path. Never raises."""
    try:
        if not deliverable_surfaces():
            return False
        from . import slack_outbound

        return slack_outbound.schedule_background(drain_pending, task_id=task_id)
    except Exception:  # noqa: BLE001 - the task write already committed
        log.exception("could not schedule completion return drain")
        return False


def startup_drain() -> None:
    """Post-init hook: deliver what a previous process left pending."""
    drain_pending()
