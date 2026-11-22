"""Shared ordered review markers in task notes (quoted mentions are historical)."""

import re

FORWARD_LOOKING_REVIEW_QUALIFIERS = (" when done", " once shipped")
REVIEW_REQUEST = re.compile(
    r"\[waiting-for: review\](?!"
    + "|".join(re.escape(qualifier) for qualifier in FORWARD_LOOKING_REVIEW_QUALIFIERS)
    + ")",
    re.IGNORECASE,
)
REVIEW_RESOLUTION = re.compile(
    r"\[review:\s*(?:approved|auto-merged|merged(?:-upstream)?|no-merge|"
    r"rejected|stale|unresolved-repo)\b",
    re.IGNORECASE,
)
REVIEW_COMPLETION = re.compile(
    r"\[review:\s*(?:merged|merged-upstream|approved|no-merge)\b|\[merged commit:",
    re.IGNORECASE,
)
# Backticks/double quotes, never apostrophes: contractions and possessives
# must not hide a real request. Preserve offsets and stay on the same line.
_QUOTED_SPAN_RX = re.compile(r'`[^`\n]*`|"[^"\n]*"')


def strip_quoted_spans(text: str) -> str:
    """Blank quoted marker mentions without changing their offsets."""
    return _QUOTED_SPAN_RX.sub(lambda match: " " * len(match.group(0)), text)


def latest_review_request_end(notes: str | None) -> int | None:
    """End offset of the latest unquoted request, including resolved requests."""
    requests = list(REVIEW_REQUEST.finditer(strip_quoted_spans(notes or "")))
    return requests[-1].end() if requests else None


def live_review_request_end(notes: str | None) -> int | None:
    """End offset of the latest unresolved review request, or None."""
    text = strip_quoted_spans(notes or "")
    end = latest_review_request_end(text)
    if end is None or REVIEW_RESOLUTION.search(text, end):
        return None
    return end


def review_cycle_notes(notes: str | None, *, keep_quoted: bool = False) -> str:
    """Current-cycle notes, sliced at canonical unquoted marker offsets.

    Literal filenames/error details need their original text. Marker consumers
    keep the default quote blanking; neither mode counts quoted requests.
    """
    raw = notes or ""
    visible = strip_quoted_spans(raw)
    end = latest_review_request_end(visible)
    text = raw if keep_quoted else visible
    return text[end:] if end is not None else text


def current_review_is_complete(notes: str | None) -> bool:
    """A completion outcome must be the latest outcome of this request."""
    scope = review_cycle_notes(notes)
    outcomes = list(REVIEW_RESOLUTION.finditer(scope)) + list(REVIEW_COMPLETION.finditer(scope))
    if not outcomes:
        return False
    latest = max(outcomes, key=lambda match: match.start())
    if REVIEW_COMPLETION.match(scope, latest.start()) is not None:
        return True
    if latest.group(0).lower().endswith("auto-merged"):
        # git_merge_task writes a landed receipt before the YOLO verdict.
        # It must belong to this request, not an older or quoted cycle.
        return any(
            marker.group(0).lower().startswith("[merged commit:")
            for marker in REVIEW_COMPLETION.finditer(scope[: latest.start()])
        )
    return False
