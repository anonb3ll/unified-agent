"""Typed inter-agent messages on the existing event log (tasks #483, #1859).

Not a second ledger, task system, or permission system. Messages are
inspectable `event_log` rows, and ``message_index`` is a derived per-recipient
mailbox over them (read/ack state, reply tracking), written in the same
transaction. Delegation and approval boundaries are unchanged.
"""

from .schema import CHANNEL_MESSAGE_PREFIX, MESSAGE_ACTS, MessageAct
from .send import ack_messages, list_messages, send_message

__all__ = [
    "CHANNEL_MESSAGE_PREFIX",
    "MESSAGE_ACTS",
    "MessageAct",
    "ack_messages",
    "list_messages",
    "send_message",
]
