"""Web control center for the unified-agent hub.

Mounted onto the existing MCPServer Starlette app rather than run as a second server —
one process, one identity plane, one port to expose over Tailscale.

Public surface is deliberately just `web_routes()`; everything else is an
implementation detail of the mount.
"""

from .routes import web_routes

__all__ = ["web_routes"]
