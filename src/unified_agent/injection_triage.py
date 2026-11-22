"""Advisory triage for INJECTION-* audit findings (Roadmap Phase 5.1, task #130).

**This module never decides anything.** The security boundary is
`audit.rules.check_untrusted_injection` plus the steward-inbox rule that any
finding quarantines the capture — that gate stays fail-closed and untouched.
What this adds is *ordering*: given a pile of findings, which ones should a
human look at first.

The problem it solves is real but narrow. The audit's injection rules are
deliberately blunt regexes, so they fire the same way on a hostile capture
carrying `ignore previous instructions` and on a wiki page that *quotes* that
string while documenting the rule. Task #106 was exactly that — a recurring
false positive on `docs/security.md` — and this repo's own Wiki pages, which
quote injection examples under a `## Mentions in source` heading, trip it by
design. When every finding looks alike, the real one waits in line behind the
documentation.

## The honest limitation, stated up front

Every de-escalating signal here is **forgeable**. A hostile capture can wrap its
payload in a code fence, prefix it with "e.g.", or format it as a blockquote,
and this module will score it lower. That is acceptable *only* because the score
changes nothing about whether the capture is quarantined — it changes the order
a human reviews the queue in. It would be unacceptable as a gate, and must never
be promoted into one.

Two consequences are baked into the design rather than left to convention:

  - De-escalation is floored (`MIN_SCORE`). Nothing reaches zero, and no band is
    named "safe" or "clean" — the lowest is `review-later`. There is no output
    of this module that means "ignore this".
  - Hidden Unicode is floored *upward* (`HIDDEN_UNICODE_FLOOR`) and cannot be
    de-escalated at all. Invisible bidi characters have no legitimate authoring
    reason in a capture; a code fence around them is itself suspicious.

Deterministic and dependency-free on purpose: no model call, no network, so it
runs inside the ingest path without a second failure mode, and its tests can
pin exact scores.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# Bands. Deliberately not named "safe"/"clean" — see the module docstring.
BAND_REVIEW_LATER = "review-later"
BAND_UNCLEAR = "unclear"
BAND_REVIEW_FIRST = "review-first"

MIN_SCORE = 0.15
HIDDEN_UNICODE_FLOOR = 0.9
_UNCLEAR_AT = 0.34
_REVIEW_FIRST_AT = 0.67

# Framing that suggests the surrounding text is *about* injection rather than
# attempting it: documentation, test fixtures, rule definitions, quoted evidence.
_EXAMPLE_FRAMING_RX = re.compile(
    r"\b(?:e\.g\.|for example|such as|example|sample|fixture|pattern|regex|"
    r"detect(?:s|ed|ion)?|rule|test|documented|quoted|verbatim|INJECTION-[A-Z]+)\b",
    re.IGNORECASE,
)

# Exfiltration shape: a verb that moves data, near a destination.
_EXFIL_VERB_RX = re.compile(
    r"\b(?:send|post|upload|exfiltrate|transmit|forward|curl|wget|fetch)\b", re.IGNORECASE
)
_DESTINATION_RX = re.compile(r"(?:https?://|@[\w.-]+\.\w{2,}|\b\d{1,3}(?:\.\d{1,3}){3}\b)")

# Second-person imperative aimed at the reader — the grammatical shape of a live
# instruction, as opposed to a description of one.
_IMPERATIVE_RX = re.compile(
    r"\b(?:you\s+(?:must|will|should|are\s+now)|ignore\s+(?:all|previous)|"
    r"disregard|instead,?\s+do|do\s+not\s+tell|never\s+mention)\b",
    re.IGNORECASE,
)

# Paths whose content is authored by the operator or by Steward itself. Not trusted in
# the security sense — just less likely to be an attack, and very likely to quote
# injection strings for documentation reasons.
_AUTHORED_AREA_RX = re.compile(r"(?:^|/)(?:Wiki|docs?|tests?)/", re.IGNORECASE)
# Where genuinely untrusted content lands.
_UNTRUSTED_AREA_RX = re.compile(r"00 - Inbox|/untrusted/|\.fetched\.", re.IGNORECASE)


@dataclass
class TriageVerdict:
    """One finding, scored. `score` and `band` are advisory ordering hints only."""

    rule: str
    path: str | None
    line: int | None
    score: float
    band: str
    signals: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _line_context(lines: list[str], line_no: int | None) -> tuple[str, list[str]]:
    """The finding's own line, plus a small window around it (±2 lines)."""
    if not line_no or line_no < 1 or line_no > len(lines):
        return "", []
    idx = line_no - 1
    window = lines[max(0, idx - 2) : min(len(lines), idx + 3)]
    return lines[idx], window


def _in_fenced_block(lines: list[str], line_no: int | None) -> bool:
    """True if the line sits inside a ``` fence. Forgeable — see module docstring."""
    if not line_no:
        return False
    fences = 0
    for raw in lines[: line_no - 1]:
        if raw.lstrip().startswith("```"):
            fences += 1
    return fences % 2 == 1


def score_finding(finding: dict, text: str) -> TriageVerdict:
    """Score one INJECTION-* finding against the document it came from.

    Non-injection findings are returned at `MIN_SCORE` and left in place — this
    module has no opinion about secrets or hygiene rules.
    """
    rule = finding.get("rule", "")
    path = finding.get("path")
    line_no = finding.get("line")
    lines = text.splitlines()
    signals: list[str] = []

    if not rule.startswith("INJECTION-"):
        return TriageVerdict(
            rule, path, line_no, MIN_SCORE, BAND_REVIEW_LATER, ["not-an-injection-rule"]
        )

    # Hidden Unicode is not subject to any of the de-escalation below: there is
    # no authoring reason for invisible bidi characters in a capture, and a code
    # fence wrapped around them makes the case worse, not better.
    if rule == "INJECTION-HIDDEN-UNICODE":
        return TriageVerdict(
            rule,
            path,
            line_no,
            HIDDEN_UNICODE_FLOOR,
            BAND_REVIEW_FIRST,
            ["hidden-unicode-never-de-escalated"],
        )

    line, window = _line_context(lines, line_no)
    window_text = "\n".join(window)
    score = 0.5  # a bare keyword hit with no other signal is genuinely unclear

    # --- escalating signals -------------------------------------------------
    if _IMPERATIVE_RX.search(line):
        score += 0.15
        signals.append("second-person-imperative")
    if _EXFIL_VERB_RX.search(window_text) and _DESTINATION_RX.search(window_text):
        score += 0.25
        signals.append("exfiltration-shape")
    if path and _UNTRUSTED_AREA_RX.search(path):
        score += 0.10
        signals.append("untrusted-source-area")

    # --- de-escalating signals (all forgeable, all floored) -----------------
    if _in_fenced_block(lines, line_no):
        score -= 0.25
        signals.append("inside-code-fence")
    if line.lstrip().startswith(">"):
        score -= 0.20
        signals.append("inside-blockquote")
    if _EXAMPLE_FRAMING_RX.search(window_text):
        score -= 0.20
        signals.append("example-framing-nearby")
    if path and _AUTHORED_AREA_RX.search(path) and not _UNTRUSTED_AREA_RX.search(path):
        score -= 0.15
        signals.append("authored-area")

    score = max(MIN_SCORE, min(1.0, round(score, 2)))
    if score >= _REVIEW_FIRST_AT:
        band = BAND_REVIEW_FIRST
    elif score >= _UNCLEAR_AT:
        band = BAND_UNCLEAR
    else:
        band = BAND_REVIEW_LATER
    return TriageVerdict(rule, path, line_no, score, band, signals or ["no-distinguishing-signal"])


def triage(findings: list[dict], texts: dict[str, str]) -> list[TriageVerdict]:
    """Score and re-rank findings, highest first.

    `texts` maps a finding's `path` to that document's content. A path missing
    from `texts` is scored without context rather than skipped — an unreadable
    document is not a reason to drop its finding from the queue.

    Ties break on (path, line) so the order is stable across runs; a triage
    report that reshuffles itself between identical runs is not reviewable.
    """
    verdicts = [score_finding(f, texts.get(f.get("path") or "", "")) for f in findings]
    return sorted(verdicts, key=lambda v: (-v.score, v.path or "", v.line or 0))


def summarize(verdicts: list[TriageVerdict]) -> dict:
    """Band counts, for a caller that wants the shape of the queue at a glance."""
    counts = {BAND_REVIEW_FIRST: 0, BAND_UNCLEAR: 0, BAND_REVIEW_LATER: 0}
    for v in verdicts:
        counts[v.band] = counts.get(v.band, 0) + 1
    return counts
