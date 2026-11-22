"""MCP tools of the ``audit`` component.

Security audit engine and rules, evidence reports, signing, anchoring and
export, the untrusted-content triage.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from ..tools import audit, evidence
from .base import Component

COMPONENT = Component(
    name="audit",
    tools=(
        audit.security_audit,
        audit.injection_triage,
        audit.audit_triage_tasks,
        evidence.evidence_report,
    ),
)
