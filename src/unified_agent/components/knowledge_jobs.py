"""MCP tools of the ``knowledge-jobs`` component.

Background knowledge work: consolidation, sleep pass, evolve, wiki, vault sync,
goals and planning, queue hygiene, lifecycle packets.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from .. import goals
from ..tools import evolve
from .base import Component

COMPONENT = Component(
    name="knowledge-jobs",
    tools=(
        goals.goal_manage,
        evolve.propose_skill_clusters,
    ),
)
