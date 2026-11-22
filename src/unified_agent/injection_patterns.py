"""Prompt-injection phrase patterns shared by the audit and the read-path scanner (#2450).

The security audit's ``check_risky_instructions`` (``audit.rules``) and the
memory write scanner (``untrusted._scan_text``) count the same phrases. The
patterns are stdlib-only data that a kernel read path needs, so they live here,
below both; ``audit.rules`` re-exports them under their old names.
"""

from __future__ import annotations

import re

# Every pattern here is a verb phrase — something that appears in an attempt,
# not in a discussion of one. "system prompt" was the exception: a bare noun
# phrase that we use constantly in our own notes about agent design. It matched
# 1430 lines of one vault, none of them an extraction attempt, drowning the
# rule's real signal. It now needs an extraction verb or a direct interrogative
# within the same sentence.
_EXTRACT_VERBS = r"reveal|repeat|print|show|output|display|dump|disclose|leak|echo|send|give"

INJECTION_KEYWORDS = [
    r"ignore\s+(?:previous|all)\s+instructions",
    r"you\s+are\s+now\s+(?:a|an)\b",
    rf"(?:{_EXTRACT_VERBS})\b[^.\n]{{0,40}}?system\s+prompt\b",
    r"what(?:'s|\s+is|\s+are)\b[^.\n]{0,20}?system\s+prompt\b",
    r"new\s+persona\b",
    r"instead,\s+do\b",
    r"you\s+must\s+act\s+as\b",
]

INJECTION_RX = re.compile("|".join(INJECTION_KEYWORDS), re.IGNORECASE)
