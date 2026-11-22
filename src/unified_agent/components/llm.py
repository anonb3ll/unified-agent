"""MCP tools of the ``llm`` component.

LLM clients and broker, provider credentials, LangGraph runners, the Gemini and
Pi tools.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from .. import credential_pool
from ..tools import gemini, langgraph, pi_runner
from .base import Component

COMPONENT = Component(
    name="llm",
    tools=(
        gemini.gemini_ask,
        credential_pool.credential_pool_register,
        credential_pool.credential_pool_list,
        credential_pool.credential_pool_revoke,
        credential_pool.model_quota_status,
        pi_runner.pi_run_task,
        langgraph.langgraph_run,
        langgraph.langgraph_progress_note,
        langgraph.langgraph_workflow_run,
        langgraph.langgraph_checkpoints,
    ),
)
