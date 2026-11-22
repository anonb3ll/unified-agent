"""MCP tools of the ``skills`` component.

Skill registry, sources and catalog, workflow engine and roles, the capability
tool.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from .. import skills_registry
from ..tools import capability, skills, synthetic_workflow, workflow
from .base import Component

COMPONENT = Component(
    name="skills",
    tools=(
        skills.skill_review,
        synthetic_workflow.synthetic_workflow,
        skills_registry.skill_registry,
        capability.check_agent_capability,
        capability.capability_catalog,
        workflow.workflow_run,
        workflow.workflow_list,
    ),
)
