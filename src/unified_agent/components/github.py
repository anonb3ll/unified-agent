"""MCP tools of the ``github`` component.

GitHub App and worker, PR watch, the review pipeline, held PRs, merge, cycle
eligibility, shipping preflight, release.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from .. import cycle_eligibility, reviewer
from ..tools import cycles, policy_authorization
from .base import Component

COMPONENT = Component(
    name="github",
    tools=(
        cycles.autonomous_cycle_start,
        cycles.autonomous_cycle_manage,
        cycles.autonomous_cycle_get,
        cycles.autonomous_cycle_transition,
        cycles.autonomous_cycle_record_usage,
        cycles.autonomous_cycle_list,
        cycles.autonomous_cycle_kill,
        policy_authorization.policy_authorization_record,
        policy_authorization.policy_authorization_list,
        reviewer.scan_for_reviewable_tasks,
        reviewer.review_and_notify,
        cycle_eligibility.autonomous_cycle_green_path_eligibility,
    ),
)
