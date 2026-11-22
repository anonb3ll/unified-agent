"""MCP tools of the ``node`` component.

ua-node: the node queue, registry, claims, follow-ups, resume, acceptance,
harness config and usage, host registry.

It registers no MCP tools today. The manifest exists so the map has one for
every component, and a selection naming ``node`` loads it like any other.
"""

from __future__ import annotations

from .base import Component

COMPONENT = Component(name="node", tools=())
