"""What a task's notes and evidence_ref claim about a merge (#2450).

``tools.tasks`` counts merge-verified descendants of a project root and the
evidence report (``audit.evidence_report``) builds a task's chain; both read
the same PR-URL and SHA shapes and the same note markers. These are pure
helpers with no imports from the package, so they live in the kernel and
``audit.evidence_report`` re-exports them under their old names.
"""

from __future__ import annotations

import re
from typing import Any

_PR_URL_RE = re.compile(r"https://github\.com/[^\s/]+/[^\s/]+/pull/\d+")
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_MERGED_RE = re.compile(
    r"\[review:\s*(?:merged|merged-upstream)\b|\[merged commit:",
    re.IGNORECASE,
)


def _merge_claim(notes: str, reported_pr_url: str | None) -> dict[str, Any]:
    """Describe note claims; notes cannot authenticate a merge."""
    if not _MERGED_RE.search(notes):
        return {"status": "none", "source": "none", "verified": False, "conflict": False}
    note_prs = set(_PR_URL_RE.findall(notes))
    conflict = bool(reported_pr_url and note_prs and reported_pr_url not in note_prs)
    return {
        "status": "conflict" if conflict else "inferred",
        "source": "task.notes",
        "verified": False,
        "conflict": conflict,
        "reported_pr_url": reported_pr_url,
        "note_pr_urls": sorted(note_prs),
    }
