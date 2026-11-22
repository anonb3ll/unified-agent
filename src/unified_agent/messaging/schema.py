"""Speech-act vocabulary for typed inter-agent messages (Task #483)."""

from __future__ import annotations

import re
from enum import StrEnum

CHANNEL_MESSAGE_PREFIX = "agent.message"


class MessageAct(StrEnum):
    REQUEST = "request"
    INFORM = "inform"
    QUERY = "query"
    DONE = "done"
    HANDOFF = "handoff"


MESSAGE_ACTS = frozenset(act.value for act in MessageAct)

DEFAULT_MAX_HOPS = 8
DEFAULT_MAX_BODY_BYTES = 4096

# A ``reply_to`` in one of these namespaces points at a message outside the hub
# mailbox (the task-space bridge replies to a Discord or Telegram message id).
# It is stored as given and is not checked against ``message_index``.
EXTERNAL_REPLY_PREFIXES = ("ts:",)

MAX_LIST_LIMIT = 200

# An id-safe conversation id (#1426, #1982). A sender may choose the id, and it
# reaches task notes, the Messages pane, events and bridges, so nothing that
# could forge a note marker there (``]``, a newline) is accepted.
CONVERSATION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
CONVERSATION_ID_RULE = "conversation_id must be 1-200 letters, digits, '.', '_', ':' or '-'"

# Typed handoff fields (task #2390). A ``handoff`` act may carry these as a
# structured payload beside the free-text body, so the receiver can check each
# claim against live state instead of trusting prose. ``repo`` is the default
# ``owner/name`` for PR, CI and commit evidence.
HANDOFF_FIELDS = (
    "done",
    "next_action",
    "why",
    "open_questions",
    "evidence",
    "reviewed_head_sha",
    "repo",
)
#: The fields a typed handoff should carry; a missing one is warned, not refused.
HANDOFF_RECOMMENDED = ("done", "next_action", "evidence")
#: What one evidence item may point at. The resolver decides the grounding tier.
EVIDENCE_KINDS = ("message", "commit", "pr", "ci_run", "task", "doc")
EVIDENCE_FIELDS = ("kind", "ref", "repo", "expect", "claim")
HANDOFF_MAX_ITEMS = 20
HANDOFF_MAX_EVIDENCE = 15
HANDOFF_MAX_TEXT = 1000
HANDOFF_MAX_ITEM_TEXT = 500
HANDOFF_MAX_REF = 300
HANDOFF_MAX_BYTES = 8192
