"""Public-reply relay with public-safety gating (task #1734, FirstMate item b).

An agent drafts a reply for a public or external channel (a GitHub issue or PR
comment today; Slack, Discord, Telegram or a Zulip stream later). Before
anything leaves the hub the relay:

1. runs deterministic public-safety gates over the draft and reaches a verdict
   of ``pass``, ``redact`` or ``block``;
2. records that verdict on the evidence ledger (``relay.public.verdict``) with
   each finding's category and a masked excerpt, never the raw text;
3. queues the gated text for human approval through the global approval gate
   (always, whatever ``UA_APPROVAL_TOOLS`` says), unless the run's operating
   profile allows auto-send for that channel (``resolve_relay_autosend``);
4. on a later identical ``send`` after a human approved it, consumes the
   one-shot pass, hands the gated text to the channel adapter and records
   ``relay.public.sent``.

Gates reuse the hub's existing scanners rather than restating them: the
audit engine's secret and hidden-Unicode patterns, the telemetry redaction
patterns, the PEM and credential-assignment patterns shared with the gcloud
metadata scrubber, and ``safety``'s public-address classification. The new
gates (internal hostnames, home paths, private ids, PII) live here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import json
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, events, safety, storage, tailnet
from ..audit import rules as audit_rules
from ..telemetry import redaction
from . import approvals
from . import zulip as _zulip

logger = logging.getLogger(__name__)

RELAY_APPROVAL_TOOL = "relay.public.send"
VERDICT_CHANNEL = "relay.public.verdict"
SENT_CHANNEL = "relay.public.sent"
DRY_RUN_CHANNEL = "relay.public.dry_run"
SEND_FAILED_CHANNEL = "relay.public.send_failed"
# Durable delivery intent, appended before the adapter is called. Its dedupe
# key is the approval id, so one approval can never start a second delivery.
SENDING_CHANNEL = "relay.public.sending"
# The adapter raised after the request may have reached the channel (timeout,
# connection drop, 5xx). Does not resolve the attempt: nothing is re-posted
# until the channel is checked or an operator records the outcome.
SEND_UNCERTAIN_CHANNEL = "relay.public.send_uncertain"


# How long a delivery attempt counts as in flight after its intent is recorded.
# Longer than every adapter's own network timeout, so a send that is still
# running cannot be cleared by an operator while it can still reach the channel.
SEND_LEASE_SECONDS = 300
_CLOSING_CHANNELS = (SENT_CHANNEL, SEND_FAILED_CHANNEL)
# Attempts whose adapter call is running in this process right now.
_ACTIVE_SENDS: set[str] = set()
_ACTIVE_SENDS_LOCK = threading.Lock()


def _now_ts() -> float:
    return time.time()


class RelayDefiniteFailure(Exception):
    """An adapter failure where nothing can have reached the channel."""


class RelayOutcomeUncertain(Exception):
    """An adapter failure where the message may or may not have been delivered."""


class _PriorAttempt(Exception):
    def __init__(self, attempt: str) -> None:
        super().__init__(attempt)
        self.attempt = attempt


PASS = "pass"
REDACT = "redact"
BLOCK = "block"
_VERDICT_RANK = {PASS: 0, REDACT: 1, BLOCK: 2}

MAX_TARGET_CHARS = 200
DRY_RUN_RECORD_CHARS = 4000


# --- findings -------------------------------------------------------------------


def _mask(value: str, keep: int = 4) -> str:
    """A bounded excerpt that names the shape of a match without repeating it."""
    shown = value[:keep] if len(value) > keep * 2 else value[: max(1, len(value) // 3)]
    return f"{shown}…({len(value)} chars)"


@dataclass(frozen=True)
class Finding:
    """One gate hit. ``excerpt`` is masked; the matched text is never stored."""

    category: str
    action: str  # REDACT or BLOCK
    start: int
    end: int
    excerpt: str
    replacement: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {"category": self.category, "action": self.action, "excerpt": self.excerpt}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class GateContext:
    """What the gates need to know about the destination."""

    public: bool = True
    max_chars: int = 10_000
    internal_hostnames: frozenset[str] = frozenset()
    blocked_terms: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GateResult:
    verdict: str
    findings: tuple[Finding, ...]
    text: str | None  # gated text; None when blocked
    original_sha256: str
    gated_sha256: str | None = None
    categories: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
            "categories": dict(self.categories),
            "original_sha256": self.original_sha256,
            "gated_sha256": self.gated_sha256,
        }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- gates ----------------------------------------------------------------------
#
# Each gate is ``(text, ctx) -> list[Finding]``. Secrets, private keys, hidden
# Unicode, over-length drafts and operator-blocked terms block; everything else
# redacts in place.


# Whole-token placeholder forms. A credential is exempt only when its value is
# entirely one of these; a marker word such as "dummy" or "example" embedded in
# an otherwise credential-shaped value still blocks (security review, #953).
_PLACEHOLDER_FORMS = (
    re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}"),  # ${API_KEY}: balanced, upper-case
    re.compile(r"\$[A-Z_][A-Z0-9_]*"),  # $API_KEY
    re.compile(r"<[a-z][a-z0-9_-]*>"),  # <token>, <your-api-key>
    re.compile(r"REDACTED|\[REDACTED(?:_[A-Z]+)*\]"),  # case-sensitive, whole token
)
_VALUE_SPLIT = re.compile(r"[\s=:]+")


def _secret_value(m: re.Match[str]) -> str:
    """The credential part of a match: the named or last group, else the last token."""
    value = m.groupdict().get("val")
    if not value and m.re.groups:
        value = next((g for g in reversed(m.groups()) if g), None)
    if not value:
        value = _VALUE_SPLIT.split(m.group(0).strip())[-1]
    return value.strip().strip("'\"")


def _placeholder(m: re.Match[str]) -> bool:
    value = _secret_value(m)
    return bool(value) and any(form.fullmatch(value) for form in _PLACEHOLDER_FORMS)


_VARIATION_SELECTORS = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))


def _is_hidden(ch: str) -> bool:
    """Invisible format characters (Unicode Cf: zero-width, bidi, soft hyphen,
    tags, U+180E) and variation selectors."""
    cp = ord(ch)
    if any(lo <= cp <= hi for lo, hi in _VARIATION_SELECTORS):
        return True
    return unicodedata.category(ch) == "Cf"


def _strip_hidden(text: str) -> tuple[str, list[int]]:
    """Text without hidden characters, plus each kept char's original index."""
    kept = [i for i, ch in enumerate(text) if not _is_hidden(ch)]
    return "".join(text[i] for i in kept), kept


def gate_secrets(text: str, ctx: GateContext) -> list[Finding]:
    """Secret shapes, matched on the text with hidden characters removed.

    An invisible character inside a token (``gh\u200bp_…``) must not defeat the
    patterns, so matching runs on the stripped text and spans map back.
    """
    stripped, kept = _strip_hidden(text)
    found = _secret_findings(stripped)
    if len(kept) == len(text):
        return found
    remapped = []
    for f in found:
        start = kept[f.start] if f.start < len(kept) else len(text)
        end = kept[f.end - 1] + 1 if 0 < f.end <= len(kept) else start
        remapped.append(dataclasses.replace(f, start=start, end=end))
    return remapped


def _secret_findings(text: str) -> list[Finding]:
    found: list[Finding] = []
    for name, rx in redaction.CREDENTIAL_SHAPE_PATTERNS:
        for m in rx.finditer(text):
            if name != "private key" and _placeholder(m):
                continue
            category = "private_key" if "private key" in name else "secret"
            excerpt = "-----BEGIN …PRIVATE KEY" if category == "private_key" else _mask(m.group(0))
            found.append(Finding(category, BLOCK, m.start(), m.end(), excerpt, detail=name))
    for name, rx in audit_rules.SECRET_PATTERNS:
        for m in rx.finditer(text):
            if _placeholder(m):
                continue
            found.append(
                Finding("secret", BLOCK, m.start(), m.end(), _mask(m.group(0)), detail=name)
            )
    for rx in redaction.secret_patterns():
        for m in rx.finditer(text):
            if _placeholder(m):
                continue
            found.append(Finding("secret", BLOCK, m.start(), m.end(), _mask(m.group(0))))
    for m in redaction.ENV_CREDENTIAL_ASSIGNMENT_RE.finditer(text):
        if _placeholder(m):
            continue
        found.append(
            Finding(
                "secret",
                BLOCK,
                m.start(),
                m.end(),
                _mask(m.group(0)),
                detail="credential assignment",
            )
        )
    for rx in (redaction.PEM_PRIVATE_KEY_BLOCK_RE, audit_rules.PRIVATE_KEY_HEADER_RX):
        for m in rx.finditer(text):
            found.append(
                Finding("private_key", BLOCK, m.start(), m.end(), "-----BEGIN …PRIVATE KEY")
            )
    found.extend(_jwk_findings(text))
    return found


def _jwk_findings(text: str) -> list[Finding]:
    found = []
    for member in redaction.jwk.private_members(text):
        if any(form.fullmatch(member.value) for form in _PLACEHOLDER_FORMS):
            continue
        found.append(
            Finding(
                "private_key",
                BLOCK,
                member.start,
                member.end,
                f'JWK "{member.name}": …({len(member.value)} chars)',
                detail="JWK symmetric key" if member.name == "k" else "JWK private member",
            )
        )
    return found


def gate_hidden_unicode(text: str, ctx: GateContext) -> list[Finding]:
    """Any invisible format character or variation selector blocks the draft.

    Broader than the audit engine's ``HIDDEN_CHAR_RX``: a public reply has no
    reason to carry U+2066-2069, a soft hyphen, ZWNJ/ZWJ, U+180E or a
    variation selector, and each can hide text from the human approver.
    """
    return [
        Finding(
            "hidden_unicode",
            BLOCK,
            i,
            i + 1,
            f"U+{ord(ch):04X}",
            detail="invisible, format or variation-selector character",
        )
        for i, ch in enumerate(text)
        if _is_hidden(ch) or audit_rules.HIDDEN_CHAR_RX.match(ch)
    ]


_IPV4_CANDIDATE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\w)")
_IPV6_CANDIDATE = re.compile(r"(?<![\w:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f.]{0,15}){2,8}(?![\w:])")


def gate_private_addresses(text: str, ctx: GateContext) -> list[Finding]:
    found: list[Finding] = []
    for rx in (_IPV4_CANDIDATE, _IPV6_CANDIDATE):
        for m in rx.finditer(text):
            candidate = m.group(0)
            if not any(ch.isalnum() for ch in candidate):
                continue
            try:
                addr = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if not safety._non_public(addr):
                continue
            embedded = safety._embedded_ipv4(addr)
            judged = embedded if embedded is not None else addr
            category = (
                "tailnet_address"
                if isinstance(judged, ipaddress.IPv4Address) and judged in tailnet._CGNAT
                else "private_address"
            )
            found.append(
                Finding(category, REDACT, m.start(), m.end(), _mask(candidate), "[redacted-ip]")
            )
    return found


_INTERNAL_SUFFIX_HOST = re.compile(
    r"(?<![\w.@-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:ts\.net|local|internal|lan|localdomain|home\.arpa)(?![\w-])",
    re.IGNORECASE,
)


def gate_internal_hostnames(text: str, ctx: GateContext) -> list[Finding]:
    found = [
        Finding(
            "internal_hostname", REDACT, m.start(), m.end(), _mask(m.group(0)), "[internal-host]"
        )
        for m in _INTERNAL_SUFFIX_HOST.finditer(text)
    ]
    for name in sorted(ctx.internal_hostnames, key=len, reverse=True):
        rx = re.compile(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", re.IGNORECASE)
        for m in rx.finditer(text):
            found.append(
                Finding(
                    "internal_hostname",
                    REDACT,
                    m.start(),
                    m.end(),
                    _mask(m.group(0)),
                    "[internal-host]",
                )
            )
    return found


# /home/<user> and /Users/<user> become "~": the rest of the path is kept so a
# reply can still point at a file without naming the account it lives under.
_HOME_PATH = re.compile(r"(?<![\w.])(?:/home|/Users)/(?!Shared\b)[A-Za-z0-9._-]+(?=/|\b)")
_WINDOWS_HOME_PATH = re.compile(r"(?<![\w])[A-Za-z]:\\Users\\[A-Za-z0-9._ -]+?(?=\\|\b)")


def gate_home_paths(text: str, ctx: GateContext) -> list[Finding]:
    found: list[Finding] = []
    for rx in (_HOME_PATH, _WINDOWS_HOME_PATH):
        for m in rx.finditer(text):
            found.append(
                Finding("home_path", REDACT, m.start(), m.end(), _mask(m.group(0), keep=6), "~")
            )
    return found


_TASK_REF = re.compile(r"(?<![\w&#/])#\d{3,6}(?!\w)")
_RUN_ID = re.compile(r"(?<![\w-])sp-[0-9A-HJKMNP-TV-Z]{26}(?![\w-])")
_BARE_ULID = re.compile(r"(?<![\w-])[0-7][0-9A-HJKMNP-TV-Z]{25}(?![\w-])")
_MEMORY_ID = re.compile(
    r"(?<![0-9A-Za-z])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{12}(?![0-9A-Za-z])"
)


def gate_internal_ids(text: str, ctx: GateContext) -> list[Finding]:
    """Hub task refs, run ids, event ULIDs and memory ids — public channels only."""
    if not ctx.public:
        return []
    found: list[Finding] = []
    for category, rx in (
        ("task_ref", _TASK_REF),
        ("run_id", _RUN_ID),
        ("event_id", _BARE_ULID),
        ("memory_id", _MEMORY_ID),
    ):
        for m in rx.finditer(text):
            found.append(
                Finding(
                    "internal_id",
                    REDACT,
                    m.start(),
                    m.end(),
                    _mask(m.group(0), keep=3),
                    "[internal-ref]",
                    detail=category,
                )
            )
    return found


_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)
_SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")
_PHONE = re.compile(
    r"(?<![\w.+-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])"
    r"|(?<![\w+])\+\d{10,15}(?!\w)"
)


def gate_pii(text: str, ctx: GateContext) -> list[Finding]:
    found: list[Finding] = []
    for m in _EMAIL.finditer(text):
        local, _, domain = m.group(0).rpartition("@")
        domain = domain.lower()
        if (
            "noreply" in local.lower()
            or local.lower() == "git"
            or any(domain == d or domain.endswith("." + d) for d in _SAFE_EMAIL_DOMAINS)
        ):
            continue
        found.append(
            Finding("email", REDACT, m.start(), m.end(), _mask(m.group(0), 2), "[redacted-email]")
        )
    for m in _PHONE.finditer(text):
        found.append(
            Finding("phone", REDACT, m.start(), m.end(), _mask(m.group(0), 2), "[redacted-phone]")
        )
    return found


def gate_policy(text: str, ctx: GateContext) -> list[Finding]:
    """Channel length limit and operator-listed blocked terms."""
    found: list[Finding] = []
    if len(text) > ctx.max_chars:
        found.append(
            Finding(
                "length",
                BLOCK,
                0,
                0,
                f"{len(text)} chars",
                detail=f"exceeds the channel limit of {ctx.max_chars} characters",
            )
        )
    for term in sorted(ctx.blocked_terms):
        rx = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        for m in rx.finditer(text):
            found.append(
                Finding(
                    "blocked_term",
                    BLOCK,
                    m.start(),
                    m.end(),
                    # Never echo any part of a listed term: a short term would
                    # survive masking whole. Category and length only.
                    f"blocked term ({len(m.group(0))} chars)",
                    detail="term listed in UA_RELAY_BLOCKED_TERMS",
                )
            )
    return found


GATES: tuple[Callable[[str, GateContext], list[Finding]], ...] = (
    gate_secrets,
    gate_hidden_unicode,
    gate_private_addresses,
    gate_internal_hostnames,
    gate_home_paths,
    gate_internal_ids,
    gate_pii,
    gate_policy,
)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Drop a hit wholly inside an earlier hit of equal or stronger action."""
    ordered = sorted(
        findings, key=lambda f: (f.start, -(f.end - f.start), -_VERDICT_RANK[f.action])
    )
    # One sweep (PR #1147 review: comparing each hit with every kept hit was
    # quadratic in a reply with thousands of findings). Hits come in start
    # order, so every kept hit starts at or before ``f``; ``f`` is covered when
    # a kept non-empty hit of equal or stronger action reaches its end.
    reach = dict.fromkeys(_VERDICT_RANK.values(), -1)  # furthest end per rank
    kept: list[Finding] = []
    for f in ordered:
        rank = _VERDICT_RANK[f.action]
        if any(end >= f.end for r, end in reach.items() if r >= rank):
            continue
        kept.append(f)
        if f.end > f.start:
            reach[rank] = max(reach[rank], f.end)
    return kept


def _strongest(findings: list[Finding]) -> str:
    verdict = PASS
    for f in findings:
        if _VERDICT_RANK[f.action] > _VERDICT_RANK[verdict]:
            verdict = f.action
    return verdict


def _shield_blocked_terms(findings: list[Finding]) -> list[Finding]:
    """No excerpt may show text that overlaps an operator-blocked term.

    Another gate's hit can cover a blocked term (an email containing it, a
    secret around it); its masked excerpt could still echo part of the term,
    so any overlapping finding is reduced to category and length.
    """
    spans = [(f.start, f.end) for f in findings if f.category == "blocked_term"]
    if not spans:
        return findings
    out = []
    for f in findings:
        if f.category != "blocked_term" and any(
            f.start < end and start < f.end for start, end in spans
        ):
            f = dataclasses.replace(f, excerpt=f"{f.category} ({f.end - f.start} chars)")
        out.append(f)
    return out


def _apply_redactions(text: str, findings: list[Finding]) -> str:
    out: list[str] = []
    cursor = 0
    for f in sorted(findings, key=lambda f: (f.start, -(f.end - f.start))):
        if f.action != REDACT or f.end <= f.start or f.start < cursor:
            continue
        out.append(text[cursor : f.start])
        out.append(f.replacement)
        cursor = f.end
    out.append(text[cursor:])
    return "".join(out)


def run_gates(text: str, ctx: GateContext | None = None) -> GateResult:
    """Run every gate and reach a verdict. Deterministic for a given text and context."""
    ctx = ctx or GateContext()
    findings: list[Finding] = []
    for gate in GATES:
        findings.extend(gate(text, ctx))
    findings = _shield_blocked_terms(findings)
    findings = _dedupe(findings)
    verdict = _strongest(findings)
    gated = None if verdict == BLOCK else _apply_redactions(text, findings)
    if gated is not None and len(gated) > ctx.max_chars:
        # Redaction placeholders can lengthen the text past the channel limit.
        findings.append(
            Finding(
                "length",
                BLOCK,
                0,
                0,
                f"{len(gated)} chars after redaction",
                detail=f"exceeds the channel limit of {ctx.max_chars} characters",
            )
        )
        verdict, gated = BLOCK, None
    categories: dict[str, int] = {}
    for f in findings:
        categories[f.category] = categories.get(f.category, 0) + 1
    return GateResult(
        verdict=verdict,
        findings=tuple(findings),
        text=gated,
        original_sha256=_sha(text),
        gated_sha256=_sha(gated) if gated is not None else None,
        categories=dict(sorted(categories.items())),
    )


# --- channel adapters -----------------------------------------------------------


class RelayAdapter(Protocol):
    """One outbound channel. Slack, Discord, Telegram and Zulip plug in here."""

    name: str
    max_chars: int
    public_by_default: bool
    # params.context keys this adapter reads. Each one changes what is sent,
    # so all of them are bound into the approval payload; others are refused.
    context_keys: frozenset[str]
    # The target is visible to the audience (a GitHub thread, a Zulip topic),
    # so a target finding that would only be redacted in evidence blocks.
    target_is_published: bool

    def unavailable_reason(self, context: Mapping[str, Any]) -> str | None:
        """Why this adapter cannot send right now, or None when it can."""

    def find_delivery(self, target: str, marker: str) -> dict[str, Any] | None:
        """A delivered message carrying ``marker``; None when not found or not
        supported. Used only to reconcile, never to decide to post."""

    def validate_target(self, target: str) -> str:
        """Return the normalized target or raise ToolError."""

    def send(self, target: str, text: str, context: Mapping[str, Any]) -> dict[str, Any]:
        """Deliver ``text``; return a small receipt (no credentials, no raw target).

        Raise ``RelayDefiniteFailure`` when nothing can have reached the
        channel, ``RelayOutcomeUncertain`` when it might have.
        """


class DryRunAdapter:
    """Records what would have been sent, and sends nothing."""

    name = "dry_run"
    max_chars = 65_536
    public_by_default = True
    context_keys: frozenset[str] = frozenset()
    target_is_published = False

    def unavailable_reason(self, context: Mapping[str, Any]) -> str | None:
        return None

    def find_delivery(self, target: str, marker: str) -> dict[str, Any] | None:
        return None

    def validate_target(self, target: str) -> str:
        return target

    def send(self, target: str, text: str, context: Mapping[str, Any]) -> dict[str, Any]:
        event_id = events.record_event(
            DRY_RUN_CHANNEL,
            payload={
                "target": context.get("target_record", "[unrecorded]"),
                "text": text[:DRY_RUN_RECORD_CHARS],
                "truncated": len(text) > DRY_RUN_RECORD_CHARS,
                "sha256": _sha(text),
            },
            originator=context.get("requested_by"),
        )
        return {"delivered": False, "dry_run": True, "dry_run_event_id": event_id}


_GITHUB_TARGET = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d{1,9})$")


class GitHubCommentAdapter:
    """An issue or pull-request comment through the existing GitHub transport.

    Target form ``owner/repo#123``. The transport comes from
    ``github_app.build_transport()`` (the operator's configured token mode); no
    new credential is introduced. Tests inject ``transport_factory``.
    """

    name = "github"
    # The attempt marker appended to the body must fit as well.
    max_chars = 65_536 - 80
    public_by_default = True
    context_keys: frozenset[str] = frozenset()
    target_is_published = True

    def __init__(self, transport_factory: Callable[[], Any] | None = None) -> None:
        self._transport_factory = transport_factory

    def _transport(self) -> Any:
        if self._transport_factory is not None:
            return self._transport_factory()
        from .. import github_app

        return github_app.build_transport()

    def unavailable_reason(self, context: Mapping[str, Any]) -> str | None:
        # Building the transport reads only config (a user-mode token is fetched
        # lazily on first request), so this catches "app mode with no
        # installation-token provider" before a draft is queued or a pass spent.
        try:
            self._transport()
        except (PermissionError, ValueError) as exc:
            return f"GitHub transport unavailable: {exc}"
        return None

    def validate_target(self, target: str) -> str:
        if not _GITHUB_TARGET.fullmatch(target):
            raise ToolError("github target must look like owner/repo#123 (issue or PR number)")
        return target

    @staticmethod
    def _split(target: str) -> tuple[str, int]:
        m = _GITHUB_TARGET.fullmatch(target)
        if m is None:  # validated earlier; kept for direct callers
            raise RelayDefiniteFailure("github target must look like owner/repo#123")
        return m.group(1), int(m.group(2))

    def send(self, target: str, text: str, context: Mapping[str, Any]) -> dict[str, Any]:
        repo, number = self._split(target)
        try:
            transport = self._transport()
        except (PermissionError, ValueError) as exc:
            raise RelayDefiniteFailure(f"GitHub transport unavailable: {exc}") from exc
        marker = str(context.get("attempt_marker") or "")
        body = f"{text}\n\n{marker}" if marker else text
        comment_id = transport.create_evidence_comment(repo=repo, number=number, body=body)
        return {
            "delivered": True,
            "target": context.get("target_record", "[unrecorded]"),
            "comment_id": str(comment_id),
        }

    def find_delivery(self, target: str, marker: str) -> dict[str, Any] | None:
        repo, number = self._split(target)
        transport = self._transport()
        finder = getattr(transport, "find_comment_with_marker", None)
        if finder is None:
            return None
        found = finder(repo=repo, number=number, marker=marker)
        return None if found is None else {"comment_id": str(found.get("id"))}


class ZulipStreamAdapter:
    """A Zulip stream message through the existing zuliprc credentials.

    Refuses while ``tools.zulip.zulip_retired()`` holds (the default). Target
    form ``stream/topic``; ``context["agent_label"]`` names whose bot sends.
    """

    name = "zulip"
    # The sender refuses anything longer (tools/zulip.py MAX_CONTENT_CHARS), so
    # the length gate uses the same limit rather than spending an approval.
    max_chars = _zulip.MAX_CONTENT_CHARS
    public_by_default = True
    context_keys: frozenset[str] = frozenset({"agent_label"})
    target_is_published = True  # the topic is visible to every stream reader

    def unavailable_reason(self, context: Mapping[str, Any]) -> str | None:
        if _zulip.zulip_retired():
            return (
                "Zulip outbound sending is retired (UA_ZULIP_RETIRED); the relay will not queue it"
            )
        if not str(context.get("agent_label") or "").strip():
            return "zulip relay needs context.agent_label (whose bot sends)"
        # The sender refuses an authenticated caller that names another bot
        # (zulip._require_agent_credentials) before building any request. Check
        # the same rule here, so it is refused before an attempt is recorded
        # and, in send(), classified as a definite not-sent failure.
        label = str(context["agent_label"]).strip()
        verified = config.verified_agent_identity()
        if verified and verified != label:
            return f"Auth identity mismatch: claimed {label!r} but verified as {verified!r}"
        return None

    def validate_target(self, target: str) -> str:
        stream, sep, topic = target.partition("/")
        if not sep or not stream.strip() or not topic.strip():
            raise ToolError("zulip target must look like stream/topic")
        if len(topic.strip()) > _zulip.MAX_TOPIC_CHARS:
            raise ToolError(f"zulip topic exceeds {_zulip.MAX_TOPIC_CHARS} characters")
        return target

    def find_delivery(self, target: str, marker: str) -> dict[str, Any] | None:
        return None  # a Zulip message cannot carry a hidden marker

    def send(self, target: str, text: str, context: Mapping[str, Any]) -> dict[str, Any]:
        reason = self.unavailable_reason(context)
        if reason:
            # Refused locally (retired, no label, identity mismatch): no request
            # was built, so nothing can have reached Zulip.
            raise RelayDefiniteFailure(reason)
        stream, _, topic = target.partition("/")
        try:
            result = _zulip.zulip_send_message(
                agent_label=str(context["agent_label"]), channel=stream, topic=topic, content=text
            )
        except _zulip.ZulipRelayError as exc:
            # Raised by the sender's own checks, before any request.
            raise RelayDefiniteFailure(str(exc)) from exc
        if result.get("sent") is not True:
            # zulip_send_message folds transport errors and API refusals into
            # {"sent": False}; the request may still have been accepted.
            raise RelayOutcomeUncertain(str(result.get("error") or "Zulip did not confirm"))
        return {"delivered": True, "message_id": result.get("message_id")}


_ADAPTERS: dict[str, RelayAdapter] = {}


def register_adapter(adapter: RelayAdapter) -> None:
    _ADAPTERS[adapter.name] = adapter


def get_adapter(channel: str) -> RelayAdapter:
    adapter = _ADAPTERS.get(channel)
    if adapter is None:
        raise ToolError(f"unknown relay channel {channel!r}; known: {', '.join(sorted(_ADAPTERS))}")
    return adapter


for _adapter in (DryRunAdapter(), GitHubCommentAdapter(), ZulipStreamAdapter()):
    register_adapter(_adapter)


# --- auto-send hook -------------------------------------------------------------


def resolve_relay_autosend(channel: str, context: Mapping[str, Any] | None) -> bool:
    """Whether the run's operating profile lets this channel skip human approval.

    Always False until operating profiles land (task #1732), which wires this
    to the run's profile. A blocked draft never sends, whatever this returns.
    """
    return False


# --- relay operations -----------------------------------------------------------


@dataclass(frozen=True)
class _Draft:
    adapter: RelayAdapter
    channel: str
    target: str  # the real destination; bound into the approval, never recorded
    target_record: str  # the gated form evidence events carry
    public: bool
    context: dict[str, Any]  # adapter extras; every one is approval-bound
    requested_by: str | None
    result: GateResult
    target_result: GateResult

    @property
    def target_verdict(self) -> str:
        """A published target cannot be redacted in place, so redact blocks."""
        verdict = self.target_result.verdict
        if verdict == REDACT and self.adapter.target_is_published:
            return BLOCK
        return verdict

    @property
    def verdict(self) -> str:
        """The stronger of the text and target verdicts."""
        if _VERDICT_RANK[self.target_verdict] > _VERDICT_RANK[self.result.verdict]:
            return self.target_verdict
        return self.result.verdict

    def send_context(self) -> dict[str, Any]:
        return {
            **self.context,
            "requested_by": self.requested_by,
            "target_record": self.target_record,
        }


def _validated_context(adapter: RelayAdapter, raw: Any) -> dict[str, Any]:
    context = raw or {}
    if not isinstance(context, Mapping):
        raise ToolError("params.context must be an object")
    unknown = sorted(set(context) - set(adapter.context_keys))
    if unknown:
        raise ToolError(
            f"params.context keys {unknown} are not used by the {adapter.name} channel; "
            f"allowed: {sorted(adapter.context_keys)}"
        )
    out: dict[str, Any] = {}
    for key in sorted(context):
        value = context[key]
        if not isinstance(value, str | int | bool) or isinstance(value, str) and len(value) > 200:
            raise ToolError(f"params.context.{key} must be a short string, number or boolean")
        out[key] = value
    return out


def _prepare(params: Mapping[str, Any]) -> _Draft:
    channel = str(params.get("channel") or "").strip()
    target = str(params.get("target") or "").strip()
    text = params.get("text")
    if not channel:
        raise ToolError("params.channel is required (e.g. 'dry_run' or 'github')")
    if not target or len(target) > MAX_TARGET_CHARS:
        raise ToolError(f"params.target is required and at most {MAX_TARGET_CHARS} characters")
    if not isinstance(text, str) or not text.strip():
        raise ToolError("params.text must be a non-empty string")
    adapter = get_adapter(channel)
    context = _validated_context(adapter, params.get("context"))
    target = adapter.validate_target(target)
    # A public channel stays public: params.public can only widen a private
    # adapter, never switch off hub-id redaction on a public one.
    public = True if adapter.public_by_default else bool(params.get("public", False))
    requested_by = config.verified_agent_identity() or (
        str(params["requested_by"]).strip() if params.get("requested_by") else None
    )
    ctx = GateContext(
        public=public,
        max_chars=adapter.max_chars,
        internal_hostnames=config.relay_internal_hostnames(),
        blocked_terms=config.relay_blocked_terms(),
    )
    # The target is an address, not prose: hub-id redaction does not apply
    # (a GitHub "owner/repo#953" is the destination itself), but every other
    # gate does, and a secret or hidden character in it blocks the draft.
    target_result = run_gates(
        target, dataclasses.replace(ctx, public=False, max_chars=MAX_TARGET_CHARS)
    )
    return _Draft(
        adapter=adapter,
        channel=channel,
        target=target,
        target_record=target_result.text if target_result.text is not None else "[blocked]",
        public=public,
        context=context,
        requested_by=requested_by,
        result=run_gates(text, ctx),
        target_result=target_result,
    )


def _approval_payload(draft: _Draft) -> dict[str, Any]:
    """The exact payload a human approves. Deterministic for an identical draft.

    Everything that changes what is sent, or who sends it, is in here: the
    channel, the destination, the gated text and every adapter context field
    (the Zulip bot identity, for one), plus the sender identity and tenant.
    Changing any of them needs a new decision.
    """
    return {
        "channel": draft.channel,
        "target": draft.target,
        "public": draft.public,
        # Who sends and for which tenant: two senders never share one grant.
        "sender": draft.requested_by,
        "org_id": edition.tenant_org_id(),
        "context": draft.context,
        "verdict": draft.verdict,
        "redactions": draft.result.categories,
        "target_redactions": draft.target_result.categories,
        "text": draft.result.text,
    }


def _record_verdict(draft: _Draft) -> str:
    return events.record_event(
        VERDICT_CHANNEL,
        payload={
            "channel": draft.channel,
            "target": draft.target_record,
            "public": draft.public,
            **draft.result.to_dict(),
            "verdict": draft.verdict,
            "text_verdict": draft.result.verdict,
            "target_verdict": draft.target_verdict,
            "target_findings": [f.to_dict() for f in draft.target_result.findings],
        },
        originator=draft.requested_by,
    )


def _attempt_fingerprint(draft: _Draft) -> str:
    return _sha(f"{draft.channel}\0{draft.target}\0{draft.result.gated_sha256}")


def _unresolved_attempt_on(conn: Any, fingerprint: str) -> str | None:
    """A prior delivery of the same text to the same target with no known outcome.

    Matched by content key (channel, target, gated-text hash), never by approval
    id, so a fresh approval cannot re-post. An attempt resolves only with
    ``relay.public.sent`` or ``relay.public.send_failed``. One with neither may
    have reached the channel, and one with an evidence-gap record did reach it,
    so in both cases the relay will not post the same text there again. Only the
    current tenant's attempts count (``org_id IS`` the tenant fence).
    """
    row = conn.execute(
        "SELECT s.ulid FROM event_log s WHERE s.channel = ? "
        "AND json_extract(s.payload, '$.fingerprint') = ? AND s.org_id IS ? "
        "AND NOT EXISTS (SELECT 1 FROM event_log t WHERE t.causal_parent_id = s.ulid "
        "AND t.channel IN (?, ?)) ORDER BY s.rowid DESC LIMIT 1",
        (
            SENDING_CHANNEL,
            fingerprint,
            edition.tenant_org_id(),
            SENT_CHANNEL,
            SEND_FAILED_CHANNEL,
        ),
    ).fetchone()
    return row[0] if row else None


def _sending_dedupe_key(approval_id: int) -> str:
    """One delivery intent per approval, qualified by the current tenant.

    Approval ids are unique within one hub database, but the key must not rely
    on that: a tenant whose ids overlap another's would otherwise receive the
    other tenant's attempt back from ``record_event_once`` and never post.
    """
    return f"{SENDING_CHANNEL}:{edition.tenant_org_id() or '-'}:{approval_id}"


def _attempt_marker(attempt: str) -> str:
    """An opaque marker for the posted body: a digest, never the hub's event id."""
    return f"<!-- relay-attempt:{_sha('relay-attempt:' + attempt)[:20]} -->"


def _attempt_in_flight(conn: Any, attempt: str, sending: Mapping[str, Any]) -> bool:
    """Whether the adapter call for ``attempt`` can still be running.

    True while it is running in this process, or while its recorded lease has
    not expired and nothing was recorded after it (any child event means the
    adapter returned). An operator never closes an in-flight attempt: the
    request could still land after the channel was checked.
    """
    with _ACTIVE_SENDS_LOCK:
        if attempt in _ACTIVE_SENDS:
            return True
    lease = sending.get("lease_expires_at")
    if not isinstance(lease, int | float) or lease <= _now_ts():
        return False
    child = conn.execute(
        "SELECT 1 FROM event_log WHERE causal_parent_id = ? LIMIT 1", (attempt,)
    ).fetchone()
    return child is None


def _prior_in_flight(attempt: str) -> bool:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE ulid = ? AND channel = ? AND org_id IS ?",
            (attempt, SENDING_CHANNEL, edition.tenant_org_id()),
        ).fetchone()
        return row is not None and _attempt_in_flight(conn, attempt, json.loads(row[0] or "{}"))
    finally:
        conn.close()


def _close_attempt(
    attempt: str,
    channel: str,
    payload: Mapping[str, Any] | Callable[[Mapping[str, Any]], Mapping[str, Any]],
    originator: str | None,
    *,
    operator: bool = False,
) -> str:
    """Append the one outcome that closes ``attempt``; the first writer wins.

    The tenant-scoped lookup of the attempt, the "no outcome yet" check (and,
    for an operator, the "not in flight" check) and the append share one
    ``BEGIN IMMEDIATE`` transaction, and the append also carries a dedupe key
    per attempt, so a delivery, a reconcile and any number of operators cannot
    record contradictory outcomes. ``payload`` may be a callable that receives
    the attempt's ``relay.public.sending`` payload.
    """
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload FROM event_log WHERE ulid = ? AND channel = ? AND org_id IS ?",
            (attempt, SENDING_CHANNEL, edition.tenant_org_id()),
        ).fetchone()
        if row is None:
            raise ToolError(f"no relay delivery attempt {attempt}")
        sending = json.loads(row[0] or "{}")
        closed = conn.execute(
            "SELECT channel FROM event_log WHERE causal_parent_id = ? AND channel IN (?, ?)",
            (attempt, *_CLOSING_CHANNELS),
        ).fetchone()
        if closed is not None:
            raise ToolError(f"relay attempt {attempt} already has an outcome ({closed[0]})")
        if operator and _attempt_in_flight(conn, attempt, sending):
            raise ToolError(
                f"relay attempt {attempt} is still being delivered; its outcome cannot be "
                "recorded until the send finishes or its lease expires "
                f"({SEND_LEASE_SECONDS}s after it started)"
            )
        body = payload(sending) if callable(payload) else payload
        ulid, won, publish_payload = events.record_event_once_on_connection(
            conn,
            channel,
            payload=dict(body),
            originator=originator,
            causal_parent_id=attempt,
            dedupe_key=f"relay.public.outcome:{attempt}",
        )
        if not won:
            raise ToolError(f"relay attempt {attempt} already has an outcome")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    if publish_payload is not None:
        events.publish(publish_payload)
    return ulid


def _classify_failure(exc: BaseException) -> str:
    """``failed`` only when nothing can have reached the channel, else ``uncertain``."""
    if isinstance(exc, RelayOutcomeUncertain):
        return "uncertain"
    if isinstance(exc, RelayDefiniteFailure | PermissionError):
        return "failed"
    status = None
    if isinstance(exc, HTTPError):
        status = exc.code
    else:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
        return "failed"
    # Timeouts, connection errors, 5xx, 408/429, unparseable replies and anything
    # unknown: the request may have landed.
    return "uncertain"


def _reconcile(draft: _Draft, attempt: str) -> dict[str, Any] | None:
    """Look for the earlier attempt's marker on the channel; record it if found.

    Never posts. Returns the receipt when the earlier delivery is found.
    """
    try:
        found = draft.adapter.find_delivery(draft.target, _attempt_marker(attempt))
    except Exception as exc:  # noqa: BLE001 - lookup is best-effort; the refusal stands
        logger.warning("relay reconcile lookup for %s failed (%s)", attempt, type(exc).__name__)
        return None
    if found is None:
        return None
    receipt = {"delivered": True, "reconciled": True, **found}
    try:
        _close_attempt(
            attempt,
            SENT_CHANNEL,
            {
                "channel": draft.channel,
                "target": draft.target_record,
                "approval_id": None,
                "reconciled": True,
                "gated_sha256": draft.result.gated_sha256,
                "receipt": receipt,
            },
            draft.requested_by,
        )
    except Exception as exc:  # noqa: BLE001 - found live; the refusal stands either way
        logger.warning("relay reconcile of %s not recorded (%s)", attempt, type(exc).__name__)
    return receipt


def _record_delivery_gap(draft: _Draft, attempt: str, approval_id: int | None, receipt, exc):
    """The channel accepted the message but ``relay.public.sent`` did not land.

    Mirrors ``approvals._record_evidence_gap``: log the exception type (never
    its message), then append ``audit.evidence.gap`` naming the attempt. The
    attempt stays unresolved either way, so the same text is never re-posted to
    the same target under a fresh approval.
    """
    logger.warning(
        "evidence gap: relay delivery %s (approval %s) sent but not recorded (%s)",
        attempt,
        approval_id,
        type(exc).__name__,
    )
    try:
        events.record_event(
            approvals.CHANNEL_EVIDENCE_GAP,
            payload={
                "kind": "relay_delivery",
                "ref_id": attempt,
                "approval_id": approval_id,
                "stage": "sent_record",
                "error_type": type(exc).__name__,
                "channel": draft.channel,
                "target": draft.target_record,
                "receipt": receipt,
            },
            originator=draft.requested_by,
            causal_parent_id=attempt,
        )
    except Exception:  # noqa: BLE001 - the warning above is the fallback record
        logger.warning("evidence gap for relay delivery %s could not be recorded", attempt)


def _deliver(draft: _Draft, approval_id: int | None, *, autosend: bool) -> dict[str, Any]:
    assert draft.result.text is not None
    fingerprint = _attempt_fingerprint(draft)

    def no_unresolved_prior(conn: Any) -> None:
        # Runs inside the BEGIN IMMEDIATE that appends the intent, so two
        # concurrent sends of the same content cannot both pass it.
        prior = _unresolved_attempt_on(conn, fingerprint)
        if prior is not None:
            raise _PriorAttempt(prior)

    # Durable intent first, keyed on the approval id (one approval, one
    # delivery) and guarded by content key (one live attempt per text+target).
    try:
        attempt, won = events.record_event_once(
            SENDING_CHANNEL,
            payload={
                "channel": draft.channel,
                "target": draft.target_record,
                "approval_id": approval_id,
                "autosend": autosend,
                "gated_sha256": draft.result.gated_sha256,
                "fingerprint": fingerprint,
                "lease_expires_at": _now_ts() + SEND_LEASE_SECONDS,
            },
            originator=draft.requested_by,
            dedupe_key=None if approval_id is None else _sending_dedupe_key(approval_id),
            precondition=no_unresolved_prior,
        )
    except _PriorAttempt as prior:
        if _prior_in_flight(prior.attempt):
            raise ToolError(
                f"an earlier delivery of this exact text to this target ({prior.attempt}) is "
                "still being sent; it is not posted again"
            ) from None
        receipt = _reconcile(draft, prior.attempt)
        if receipt is not None:
            raise ToolError(
                f"this exact text was already delivered to this target (attempt "
                f"{prior.attempt}, found on the channel and now recorded); it is not posted again"
            ) from None
        raise ToolError(
            f"an earlier delivery of this exact text to this target ({prior.attempt}) has no "
            "recorded sent outcome and may already be live; it is not posted again. An "
            "operator can record its outcome with relay_manage('resolve_attempt', ...)"
        ) from None
    if not won:
        raise ToolError(
            f"approval {approval_id} already started a delivery ({attempt}); "
            "it is never sent twice. Draft again for a fresh human decision."
        )
    context = {**draft.send_context(), "attempt_marker": _attempt_marker(attempt)}
    with _ACTIVE_SENDS_LOCK:
        _ACTIVE_SENDS.add(attempt)
    try:
        return _send_and_record(draft, attempt, approval_id, autosend, context)
    finally:
        with _ACTIVE_SENDS_LOCK:
            _ACTIVE_SENDS.discard(attempt)


def _send_and_record(
    draft: _Draft,
    attempt: str,
    approval_id: int | None,
    autosend: bool,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    assert draft.result.text is not None
    try:
        receipt = draft.adapter.send(draft.target, draft.result.text, context)
    except Exception as exc:
        # Redact before truncating: cutting first could leave half a credential.
        reason = redaction.redact_sensitive_content(str(exc) or type(exc).__name__, 300)
        outcome = _classify_failure(exc)
        failure = {
            "channel": draft.channel,
            "target": draft.target_record,
            "approval_id": approval_id,
            "gated_sha256": draft.result.gated_sha256,
            "error_type": type(exc).__name__,
            "error": reason,
        }
        if outcome == "failed":
            try:
                _close_attempt(attempt, SEND_FAILED_CHANNEL, failure, draft.requested_by)
            except ToolError as closed:
                logger.warning("relay failure of %s not recorded (%s)", attempt, closed)
            raise ToolError(
                f"relay send to {draft.channel} failed before reaching it: {reason}. "
                "The approval was consumed; draft again for a fresh human decision."
            ) from None
        events.record_event(
            SEND_UNCERTAIN_CHANNEL,
            payload=failure,
            originator=draft.requested_by,
            causal_parent_id=attempt,
        )
        raise ToolError(
            f"relay send to {draft.channel} has an unknown outcome ({type(exc).__name__}): "
            f"{reason}. The message may be live, so it is not retried; attempt {attempt} "
            "stays open until the channel shows it or an operator records the outcome with "
            "relay_manage('resolve_attempt', ...)."
        ) from None
    try:
        sent_event = _close_attempt(
            attempt,
            SENT_CHANNEL,
            {
                "channel": draft.channel,
                "target": draft.target_record,
                "approval_id": approval_id,
                "autosend": autosend,
                "verdict": draft.verdict,
                "categories": draft.result.categories,
                "gated_sha256": draft.result.gated_sha256,
                "receipt": receipt,
            },
            draft.requested_by,
        )
    except Exception as exc:  # noqa: BLE001 - delivered; report it, never re-send
        _record_delivery_gap(draft, attempt, approval_id, receipt, exc)
        return {
            "attempt_id": attempt,
            "sent_event_id": None,
            "recording_gap": True,
            "receipt": receipt,
        }
    return {"attempt_id": attempt, "sent_event_id": sent_event, "receipt": receipt}


def relay_check(params: Mapping[str, Any]) -> dict[str, Any]:
    """Run the gates only. Records nothing and queues nothing."""
    draft = _prepare(params)
    return {
        "channel": draft.channel,
        "target": draft.target_record,
        "public": draft.public,
        **draft.result.to_dict(),
        "verdict": draft.verdict,
        "target_findings": [f.to_dict() for f in draft.target_result.findings],
        "text": draft.result.text if draft.verdict != BLOCK else None,
    }


def relay_draft(params: Mapping[str, Any]) -> dict[str, Any]:
    """Gate a draft, record the verdict, and queue it for approval (or auto-send).

    The verdict is recorded before anything else is decided, so a blocked
    draft leaves evidence even when its channel is unavailable.
    """
    draft = _prepare(params)
    verdict_event = _record_verdict(draft)
    out: dict[str, Any] = {
        "channel": draft.channel,
        "target": draft.target_record,
        "public": draft.public,
        "verdict": draft.verdict,
        "findings": [f.to_dict() for f in draft.result.findings],
        "target_findings": [f.to_dict() for f in draft.target_result.findings],
        "verdict_event_id": verdict_event,
    }
    if draft.verdict == BLOCK:
        out["status"] = "blocked"
        out["hint"] = "fix the blocking findings and draft again; nothing was queued"
        return out
    reason = draft.adapter.unavailable_reason(draft.context)
    if reason:
        raise ToolError(f"{reason} (verdict recorded as {verdict_event}; nothing was queued)")
    out["text"] = draft.result.text
    if resolve_relay_autosend(draft.channel, draft.send_context()):
        out.update(_deliver(draft, None, autosend=True))
        out["status"] = "sent"
        return out
    gate = approvals.check_approval_gate(
        RELAY_APPROVAL_TOOL,
        _approval_payload(draft),
        requested_by=draft.requested_by,
        force=True,
        request_only=True,
    )
    out["approval_id"] = gate.get("approval_id")
    out["status"] = gate.get("status", "pending")
    out["hint"] = (
        "a human resolves it with approval_resolve(approval_id, 'approve'|'deny'); then "
        "call relay_manage('send', <the same params>) to consume the pass and send"
    )
    return out


def relay_send(params: Mapping[str, Any]) -> dict[str, Any]:
    """Re-gate an identical draft, consume its approved pass, and send it."""
    draft = _prepare(params)
    if draft.verdict == BLOCK:
        _record_verdict(draft)
        raise ToolError("the draft is blocked by the public-safety gates; nothing was sent")
    reason = draft.adapter.unavailable_reason(draft.context)
    if reason:
        raise ToolError(reason)
    gate = approvals.check_approval_gate(
        RELAY_APPROVAL_TOOL,
        _approval_payload(draft),
        requested_by=draft.requested_by,
        force=True,
    )
    out = {
        "channel": draft.channel,
        "target": draft.target_record,
        "verdict": draft.verdict,
        "approval_id": gate.get("approval_id"),
        "status": "sent",
    }
    out.update(_deliver(draft, gate.get("approval_id"), autosend=False))
    return out


def relay_status(params: Mapping[str, Any]) -> dict[str, Any]:
    raw = params.get("approval_id")
    try:
        approval_id = int(raw)
    except (TypeError, ValueError):
        raise ToolError("params.approval_id must be an integer") from None
    row = approvals.get_approval(approval_id)
    if row is None or row.get("tool") != RELAY_APPROVAL_TOOL:
        raise ToolError(f"no relay approval request with id {approval_id}")
    payload = row.get("payload") or {}
    return {
        "approval_id": approval_id,
        "status": row.get("status"),
        "channel": payload.get("channel"),
        "target": payload.get("target"),
        "verdict": payload.get("verdict"),
        "redactions": payload.get("redactions"),
        "requested_by": row.get("requested_by"),
        "resolved_by": row.get("resolved_by"),
        "reason": row.get("reason"),
    }


_RESOLUTIONS = {"delivered": SENT_CHANNEL, "not_delivered": SEND_FAILED_CHANNEL}


def relay_resolve_attempt(params: Mapping[str, Any]) -> dict[str, Any]:
    """Record the outcome of a delivery attempt left open (operator only).

    For an attempt stuck between ``relay.public.sending`` and a recorded
    outcome (a crash before the adapter call, an uncertain send, a recording
    gap). A human checks the channel and records ``delivered`` or
    ``not_delivered``. This never posts anything: after ``not_delivered`` the
    agent drafts again and a human approves again.

    The resolver is always the *verified* caller, and that identity must be a
    human: a ``resolved_by`` the caller merely claims is never trusted, in
    enforced or open mode (an agent could otherwise name a human and clear its
    own uncertain attempt). The attempt is looked up inside the current tenant
    only, and an attempt whose send is still in flight is refused until it
    finishes or its lease expires.
    """
    attempt = str(params.get("attempt_id") or "").strip()
    outcome = str(params.get("outcome") or "").strip()
    if not attempt:
        raise ToolError("params.attempt_id is required (the relay.public.sending event id)")
    if outcome not in _RESOLUTIONS:
        raise ToolError(f"params.outcome must be one of {sorted(_RESOLUTIONS)}")
    resolver = config.verified_agent_identity()
    if not resolver:
        raise ToolError("resolving a relay attempt requires a verified human identity")
    if config.actor_kind_for_identity(resolver) != "human":
        raise ToolError(
            "resolving a relay attempt is an operator action: it needs a verified human "
            "identity (the agent that drafted cannot record its own delivery outcome)"
        )
    claimed = str(params.get("resolved_by") or "").strip()
    if claimed and not approvals._same_person(claimed, resolver):
        raise ToolError(
            f"params.resolved_by {claimed!r} does not match the verified identity "
            f"{resolver!r}; the relay records the verified caller only"
        )
    reason = redaction.redact_sensitive_content(str(params.get("reason") or ""), 300)

    def outcome_payload(sending: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "channel": sending.get("channel"),
            "target": sending.get("target"),
            "approval_id": sending.get("approval_id"),
            "gated_sha256": sending.get("gated_sha256"),
            "manual_resolution": True,
            "resolved_by": resolver,
            "reason": reason,
            "receipt": {"delivered": outcome == "delivered", "manual": True},
        }

    event = _close_attempt(attempt, _RESOLUTIONS[outcome], outcome_payload, resolver, operator=True)
    return {"attempt_id": attempt, "outcome": outcome, "event_id": event, "reposted": False}


_ACTIONS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "check": relay_check,
    "draft": relay_draft,
    "send": relay_send,
    "status": relay_status,
    "resolve_attempt": relay_resolve_attempt,
}
_ALLOWED_PARAMS = frozenset(
    {
        "channel",
        "target",
        "text",
        "public",
        "context",
        "requested_by",
        "approval_id",
        "attempt_id",
        "outcome",
        "resolved_by",
        "reason",
    }
)


def relay_manage(action: str, params: dict | None = None) -> dict:
    """Public-reply relay: gate an agent's draft for an external channel (task #1734).

    Actions:
    - ``check``: run the public-safety gates only; nothing recorded or queued.
    - ``draft``: gate, record the verdict as evidence (``relay.public.verdict``),
      and queue the gated text for human approval. A blocked draft is recorded
      and never queued.
    - ``send``: the identical draft after approval; consumes the one-shot pass,
      sends through the channel adapter, and records ``relay.public.sent``.
    - ``status``: an approval request's state (``params.approval_id``).
    - ``resolve_attempt``: verified human operator only (the caller's verified
      identity is used; ``resolved_by`` is not trusted). Records ``delivered``
      or ``not_delivered`` for a delivery attempt with no outcome
      (``params.attempt_id``, ``outcome``, ``reason``) in the current tenant,
      and not while its send is in flight. Never posts.

    ``params``: ``channel`` (``dry_run`` | ``github`` | ``zulip``), ``target``
    (``owner/repo#123`` for github, ``stream/topic`` for zulip), ``text``,
    optional ``public`` (only widens a private channel; the built-in channels are
    always public),
    ``context`` (adapter extras, e.g. ``agent_label`` for zulip) and
    ``requested_by`` (ignored when the caller is authenticated).

    Gates: secrets and private keys (block), hidden Unicode (block), channel
    length and ``UA_RELAY_BLOCKED_TERMS`` (block); private and tailnet IPs,
    internal hostnames (``*.ts.net``, ``*.local``, configured machine names),
    home paths, hub task/run/event/memory ids on public channels, emails and
    phone numbers (redact).
    """
    name = str(action or "").strip().lower()
    handler = _ACTIONS.get(name)
    if handler is None:
        raise ToolError(f"unknown relay_manage action {action!r}; use one of {sorted(_ACTIONS)}")
    params = dict(params or {})
    unknown = sorted(set(params) - _ALLOWED_PARAMS)
    if unknown:
        raise ToolError(f"unknown relay_manage params: {', '.join(unknown)}")
    return handler(params)
