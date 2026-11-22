"""MCP tools of the ``auth`` component.

Hub sign-in: OAuth server and approver, CIMD, passkeys, WorkOS, device login,
OAuth stores and session-scoped credentials.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from ..tools import oauth_devices
from .base import Component

COMPONENT = Component(
    name="auth",
    tools=(oauth_devices.oauth_device_manage,),
)
