"""MCP tools of the ``chat-bridges`` component.

Slack and Zulip: config, outbound, gateway, chat workers, approval-in-Slack,
completion return to the asking thread.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from ..tools import public_relay, zulip
from .base import Component

COMPONENT = Component(
    name="chat-bridges",
    tools=(
        zulip.zulip_send_message,
        zulip.zulip_fetch_messages,
        public_relay.relay_manage,
    ),
)
