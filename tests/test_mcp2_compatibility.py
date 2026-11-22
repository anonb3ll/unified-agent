"""Deterministic compatibility tests for MCP 2.x capabilities and client matrix (task #863).

Covers:
1. Tool Annotations: readOnlyHint, destructiveHint, idempotentHint taxonomy & consistency.
2. Structured Outputs: Output schema validation, typed structured_content & explicit failure modes.
3. Elicitation: Supported and unsupported capability combinations & challenge validation.
4. Progress Notifications: Worker thread & async absolute milestones and safe unbound no-op.
5. Resource Subscriptions: Live state resource URIs, profile filtering & explicit error paths.
6. Client Matrix: Matrix data integrity, compatibility checking, and missing capability diagnostics.
"""

from __future__ import annotations

import json

import anyio
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import (
    ElicitResult,
    InputRequiredResult,
)
from pydantic import ValidationError

from unified_agent import (
    config,
    elicitation,
    mcp_progress,
    profiles,
)
from unified_agent.mcp_live_state import (
    BOARD_URI,
    RUN_URI_TEMPLATE,
    TASK_URI_TEMPLATE,
    task_uri,
)
from unified_agent.mcp_matrix import (
    McpCapability,
    check_client_compatibility,
    get_client_matrix,
    render_matrix_markdown,
)
from unified_agent.server import (
    _DESTRUCTIVE_TOOLS,
    _READ_ONLY_TOOLS,
    REGISTERED_TOOLS,
    _tool_annotations,
    mcp,
)
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    check_approval_gate,
)
from unified_agent.tools.tasks import task_add


@pytest.fixture
def anyio_backend():
    return "asyncio"


# =========================================================================== #
# 1. Tool Annotations Tests                                                   #
# =========================================================================== #


class TestToolAnnotations:
    def test_all_registered_tools_have_valid_annotations(self):
        """Every tool registered on the MCP server must produce valid ToolAnnotations."""
        assert len(REGISTERED_TOOLS) > 0
        for _name, fn in REGISTERED_TOOLS.items():
            annotations = _tool_annotations(fn)
            assert annotations is not None
            assert isinstance(annotations.read_only_hint, bool)
            assert isinstance(annotations.destructive_hint, bool)
            assert isinstance(annotations.idempotent_hint, bool)

    def test_no_overlap_between_readonly_and_destructive(self):
        """A tool cannot be simultaneously classified as read-only and destructive."""
        intersection = _READ_ONLY_TOOLS & _DESTRUCTIVE_TOOLS
        assert intersection == frozenset(), f"Conflicting annotations for: {intersection}"

    def test_metered_and_unkeyed_tools_not_idempotent(self):
        """gemini_ask (metered quota) and lifecycle_record (unkeyed mutation) must NOT be idempotent."""
        gemini_annotations = _tool_annotations(REGISTERED_TOOLS["gemini_ask"])
        assert gemini_annotations.idempotent_hint is False
        assert gemini_annotations.read_only_hint is True

        lifecycle_annotations = _tool_annotations(REGISTERED_TOOLS["lifecycle_record"])
        assert lifecycle_annotations.idempotent_hint is False
        assert lifecycle_annotations.read_only_hint is False

    def test_destructive_tools_annotated_correctly(self):
        """Critical mutating tools must carry destructiveHint=True and readOnlyHint=False."""
        mutating = ["run_cmd", "fs_write", "git_commit_staged", "task_update", "task_delete"]
        for tool_name in mutating:
            if tool_name in REGISTERED_TOOLS:
                anno = _tool_annotations(REGISTERED_TOOLS[tool_name])
                assert anno.destructive_hint is True, f"{tool_name} missing destructive_hint"
                assert anno.read_only_hint is False, f"{tool_name} has invalid read_only_hint"

    # Provenance (task #944): the test name, the iterated tool tuple, and the
    # two `anno.*_hint is False` assertions originated as uncommitted work in a
    # Desktop_Agent2 worktree (/home/you/.codex/worktrees/96f9, off 83b2fc1,
    # never committed or branched). The docstring rationale, the
    # REGISTERED_TOOLS/_READ_ONLY_TOOLS set-membership assertions, and the
    # failure messages were added by Desktop_Agent1. Codex's worktree was read
    # only -- not modified, committed, reset, or cleaned.
    def test_credential_pool_mutators_are_not_read_only_or_idempotent(self):
        """Credential-registry writes must not advertise read-only or retry-safe semantics.

        credential_pool_register and credential_pool_revoke both mutate the
        SQLite registry. Advertising readOnlyHint=True would let a client skip
        an approval gate, and idempotentHint=True would invite a blind retry
        that re-registers or re-revokes a credential.
        """
        for tool_name in ("credential_pool_register", "credential_pool_revoke"):
            assert tool_name in REGISTERED_TOOLS, f"{tool_name} is not registered"
            assert tool_name not in _READ_ONLY_TOOLS, (
                f"{tool_name} mutates the registry and must not be classified read-only"
            )
            anno = _tool_annotations(REGISTERED_TOOLS[tool_name])
            assert anno.read_only_hint is False, f"{tool_name} has invalid read_only_hint"
            assert anno.idempotent_hint is False, f"{tool_name} has invalid idempotent_hint"

    @pytest.mark.anyio
    async def test_tools_list_exposes_annotations_to_client(self):
        """MCP client listing tools must receive populated annotations."""
        async with Client(mcp) as client:
            tools_result = await client.session.list_tools()
            tools_map = {t.name: t for t in tools_result.tools}

            assert "fs_read" in tools_map
            assert tools_map["fs_read"].annotations.read_only_hint is True
            assert tools_map["fs_read"].annotations.destructive_hint is False

            assert "run_cmd" in tools_map
            assert tools_map["run_cmd"].annotations.destructive_hint is True
            assert tools_map["run_cmd"].annotations.read_only_hint is False


# =========================================================================== #
# 2. Structured Outputs Tests                                                 #
# =========================================================================== #


class TestStructuredOutputs:
    def test_graph_canvas_read_output_adapter_validates_and_rejects_missing(self):
        """Tool output adapter validates conforming payloads and rejects incomplete payloads."""
        tool = mcp._tool_manager._tools["graph_canvas_read"]
        adapter = tool.fn_metadata._output_adapter(tool.fn_metadata.output_model)

        valid_payload = {
            "board_id": "test-board",
            "exists": False,
            "scene": {},
            "parsed": {
                "node_count": 0,
                "edge_count": 0,
                "unparsed_count": 0,
                "task_suggestions": [],
                "dependency_suggestions": [],
            },
            "vault_path": None,
            "revision": None,
            "updated_at": None,
            "updated_by": None,
        }
        validated = adapter.validate_python(valid_payload)
        assert validated["board_id"] == "test-board"
        assert validated["exists"] is False

        # Incomplete payload (missing required fields)
        with pytest.raises(ValidationError):
            adapter.validate_python({"board_id": "incomplete-board"})

    def test_evidence_report_output_adapter_validates_and_rejects_malformed(self):
        """Evidence report output adapter enforces structured schema."""
        tool = mcp._tool_manager._tools["evidence_report"]
        adapter = tool.fn_metadata._output_adapter(tool.fn_metadata.output_model)

        valid_payload = {
            "report_type": "user_attributable_evidence",
            "schema_version": "1.0",
            "generated_at": "2026-09-09T12:00:00Z",
            "attestation": {
                "attester": None,
                "attester_kind": "unknown",
                "human_attested": False,
                "verified_via": None,
                "key_id": None,
                "attested_at": "2026-09-09T12:00:00Z",
                "scope": {},
            },
            "chain_integrity": {"valid": True},
            # Task #1265 (Epic F, F2): `anchor` is a required field on the
            # wire-facing EvidenceReport TypedDict.
            "anchor": {"status": "not_anchored"},
            "filter": {},
            "total_records": 0,
            "records": [],
            "project_chain": None,
        }
        validated = adapter.validate_python(valid_payload)
        assert validated["report_type"] == "user_attributable_evidence"
        assert validated["attestation"]["attester_kind"] == "unknown"
        assert validated["attestation"]["human_attested"] is False
        assert validated["attestation"]["attested_at"] == valid_payload["generated_at"]

        # Invalid field types or missing required fields
        with pytest.raises(ValidationError):
            adapter.validate_python({"report_type": 12345})  # not a string

    @pytest.mark.anyio
    async def test_call_tool_returns_typed_structured_content(self):
        """MCP client calling structured tools receives structured_content dict."""
        async with Client(mcp) as client:
            res_graph = await client.session.call_tool(
                "graph_canvas_read", {"board_id": "compat-test"}
            )
            assert not res_graph.is_error
            assert res_graph.structured_content is not None
            assert res_graph.structured_content["board_id"] == "compat-test"
            assert res_graph.structured_content["exists"] is False

            res_evidence = await client.session.call_tool("evidence_report", {"task_id": 863})
            assert not res_evidence.is_error
            assert res_evidence.structured_content is not None
            assert res_evidence.structured_content["report_type"] == "user_attributable_evidence"


# =========================================================================== #
# 3. Elicitation Compatibility & Error Combinations Tests                     #
# =========================================================================== #


class _FakeSession:
    def __init__(self, supports: bool, session_id=None):
        self._supports = supports
        self.session_id = session_id

    def check_client_capability(self, _capability) -> bool:
        return self._supports


class _FakeContext:
    def __init__(self, *, supports=True, input_responses=None, request_state=None, session_id=None):
        self.session = _FakeSession(supports, session_id=session_id)
        self.input_responses = input_responses
        self.request_state = request_state


def _gated_fn(*, command="echo test"):
    check_approval_gate("run_cmd", {"command": command}, requested_by="Desktop_agent3")
    return {"executed": True, "command": command}


class TestElicitationCompatibility:
    @pytest.mark.anyio
    async def test_supported_combination_raises_input_required_and_resolves(
        self, monkeypatch, isolated_env
    ):
        """Supported: UA_ELICITATION_APPROVALS=1 + capability declared + operator profile."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
        monkeypatch.setattr("unified_agent.profiles.tool_is_allowed", lambda name: True)
        # The row is requested by Desktop_agent3. Accepting it requires a human.
        identity = config.agent_identity_var.set("Desktop_Person")
        try:
            await self._resolve_supported_elicitation()
        finally:
            config.agent_identity_var.reset(identity)

    async def _resolve_supported_elicitation(self):
        # Round 1: triggers elicitation request with CSPRNG challenge
        ctx1 = _FakeContext(supports=True)
        result1 = await elicitation.guarded_call(
            _gated_fn, {"command": "echo 1"}, tool_name="run_cmd", elicit_ctx=ctx1
        )
        assert isinstance(result1, InputRequiredResult)
        assert "approval" in result1.input_requests

        state = json.loads(result1.request_state)
        approval_id = state["approval_id"]
        challenge = state["challenge"]

        # Round 2: client answers with accept + approve + echoed challenge
        ctx2 = _FakeContext(
            supports=True,
            input_responses={
                "approval": ElicitResult(action="accept", content={"approve": True, "reason": "ok"})
            },
            request_state=json.dumps({"approval_id": approval_id, "challenge": challenge}),
        )
        result2 = await elicitation.guarded_call(
            _gated_fn, {"command": "echo 1"}, tool_name="run_cmd", elicit_ctx=ctx2
        )
        assert result2 == {"executed": True, "command": "echo 1"}

    @pytest.mark.anyio
    async def test_missing_client_capability_falls_back_to_ui_queue(
        self, monkeypatch, isolated_env
    ):
        """Unsupported: Client does NOT declare elicitation capability -> raises ApprovalPendingError."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
        monkeypatch.setattr("unified_agent.profiles.tool_is_allowed", lambda name: True)

        ctx = _FakeContext(supports=False)
        with pytest.raises(ApprovalPendingError) as exc_info:
            await elicitation.guarded_call(
                _gated_fn, {"command": "echo 2"}, tool_name="run_cmd", elicit_ctx=ctx
            )
        assert "run_cmd" in str(exc_info.value)
        assert "approval" in str(exc_info.value)

    @pytest.mark.anyio
    async def test_disabled_config_falls_back_to_ui_queue(self, monkeypatch, isolated_env):
        """Unsupported: UA_ELICITATION_APPROVALS=0 -> raises ApprovalPendingError."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        monkeypatch.setenv("UA_ELICITATION_APPROVALS", "0")
        monkeypatch.setattr("unified_agent.profiles.tool_is_allowed", lambda name: True)

        ctx = _FakeContext(supports=True)
        with pytest.raises(ApprovalPendingError):
            await elicitation.guarded_call(
                _gated_fn, {"command": "echo 3"}, tool_name="run_cmd", elicit_ctx=ctx
            )

    @pytest.mark.anyio
    async def test_restricted_profile_cannot_self_approve(self, monkeypatch, isolated_env):
        """Unsupported: profile denying approval_resolve cannot self-approve -> raises ApprovalPendingError."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
        monkeypatch.setattr(
            "unified_agent.profiles.tool_is_allowed", lambda name: name != "approval_resolve"
        )

        ctx = _FakeContext(supports=True)
        with pytest.raises(ApprovalPendingError):
            await elicitation.guarded_call(
                _gated_fn, {"command": "echo 4"}, tool_name="run_cmd", elicit_ctx=ctx
            )

    @pytest.mark.anyio
    async def test_replayed_or_invalid_challenge_leaves_row_pending(
        self, monkeypatch, isolated_env
    ):
        """Failure: Invalid challenge token leaves row pending and does not auto-approve."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
        monkeypatch.setattr("unified_agent.profiles.tool_is_allowed", lambda name: True)

        ctx1 = _FakeContext(supports=True)
        result1 = await elicitation.guarded_call(
            _gated_fn, {"command": "echo 5"}, tool_name="run_cmd", elicit_ctx=ctx1
        )
        approval_id = json.loads(result1.request_state)["approval_id"]

        # Fake challenge nonce
        ctx_bad = _FakeContext(
            supports=True,
            input_responses={"approval": ElicitResult(action="accept", content={"approve": True})},
            request_state=json.dumps({"approval_id": approval_id, "challenge": "forged_nonce"}),
        )
        with pytest.raises(ApprovalPendingError):
            await elicitation.guarded_call(
                _gated_fn, {"command": "echo 5"}, tool_name="run_cmd", elicit_ctx=ctx_bad
            )


# =========================================================================== #
# 4. Progress Notifications Tests                                             #
# =========================================================================== #


class _FakeProgressContext:
    def __init__(self):
        self.reports: list[tuple[float, float | None, str | None]] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ):
        self.reports.append((progress, total, message))


class TestProgressNotifications:
    @pytest.mark.anyio
    async def test_worker_thread_emits_ordered_milestones(self):
        """Worker thread reports absolute milestones to the bound progress handler."""
        ctx = _FakeProgressContext()

        def worker():
            mcp_progress.report_progress(0.0, 10.0, "step 0")
            mcp_progress.report_progress(5.0, 10.0, "step 5")
            mcp_progress.report_progress(10.0, 10.0, "step 10")

        with mcp_progress.progress_scope(ctx):
            await anyio.to_thread.run_sync(worker)

        assert ctx.reports == [
            (0.0, 10.0, "step 0"),
            (5.0, 10.0, "step 5"),
            (10.0, 10.0, "step 10"),
        ]

    def test_unbound_progress_silently_no_ops(self):
        """When no MCP progress context is bound, report_progress completes without error."""
        mcp_progress.report_progress(1.0, 5.0, "no-op")


# =========================================================================== #
# 5. Resource Subscriptions Tests                                             #
# =========================================================================== #


class TestResourceSubscriptions:
    @pytest.mark.anyio
    async def test_board_and_task_resources_read_and_templates(self):
        """Board and task resources read correctly and appear in template lists."""
        created = task_add("mcp compatibility test task")
        tid = created["id"]

        async with Client(mcp) as client:
            resources = await client.list_resources()
            uris = {str(r.uri) for r in resources.resources}
            assert BOARD_URI in uris

            templates = await client.list_resource_templates()
            t_uris = {t.uri_template for t in templates.resource_templates}
            assert TASK_URI_TEMPLATE in t_uris
            assert RUN_URI_TEMPLATE in t_uris

            # Read valid task
            task_res = await client.read_resource(task_uri(tid))
            payload = json.loads(task_res.contents[0].text)
            assert payload["task"]["id"] == tid
            assert payload["task"]["title"] == "mcp compatibility test task"

    @pytest.mark.anyio
    async def test_unknown_task_and_run_raise_not_found(self):
        """Accessing nonexistent tasks or runs raises ResourceNotFoundError."""
        async with Client(mcp) as client:
            with pytest.raises(MCPError, match="unknown task: 987654"):
                await client.read_resource("unified-agent://tasks/987654")

            with pytest.raises(MCPError, match="unknown task: 'invalid'"):
                await client.read_resource("unified-agent://tasks/invalid")

            with pytest.raises(MCPError, match="unknown dispatch run: nonexistent-run"):
                await client.read_resource("unified-agent://runs/nonexistent-run")

    @pytest.mark.anyio
    async def test_profile_denied_resource_raises_resource_error(self, monkeypatch):
        """When active profile does not allow reading the backing tool, ResourceError is raised."""
        monkeypatch.setattr(profiles, "tool_is_allowed", lambda name: False)

        async with Client(mcp) as client:
            with pytest.raises(MCPError, match="resource is not exposed by the active"):
                await client.read_resource(BOARD_URI)


# =========================================================================== #
# 6. Client Capability Matrix & Diagnostics Tests                            #
# =========================================================================== #


class TestClientCapabilityMatrix:
    def test_matrix_contains_all_core_clients(self):
        """Matrix must register Hermes, Claude, Codex, Agy, and Cursor."""
        clients = get_client_matrix()
        assert len(clients) == 5
        client_ids = {c["client_id"] for c in clients}
        assert client_ids == {"hermes", "claude", "codex", "agy", "cursor"}

    def test_hermes_compatibility_evaluation(self):
        """Hermes supports annotations and structured outputs; flags unsupported elicitation/progress."""
        result = check_client_compatibility(
            "Desktop_Agent5",
            transport="streamable_http",
            required_capabilities=[
                McpCapability.TOOL_ANNOTATIONS,
                McpCapability.STRUCTURED_OUTPUTS,
            ],
        )
        assert result["compatible"] is True
        assert len(result["missing_capabilities"]) == 0

        # Checking elicitation on Hermes
        elicit_check = check_client_compatibility(
            "Desktop_Agent5",
            transport="streamable_http",
            required_capabilities=[McpCapability.ELICITATION],
        )
        assert elicit_check["compatible"] is False
        assert "elicitation" in elicit_check["missing_capabilities"]
        assert any("unsupported by Desktop Agent5" in d for d in elicit_check["diagnostics"])
        assert any(
            "Fallback: Approval-gated tools park in /ui queue" in d
            for d in elicit_check["diagnostics"]
        )

    def test_claude_compatibility_evaluation(self):
        """Claude supports tool annotations, structured outputs, progress, and conditional elicitation."""
        result = check_client_compatibility(
            "Desktop_Agent1",
            transport="streamable_http",
            required_capabilities=[
                McpCapability.TOOL_ANNOTATIONS,
                McpCapability.STRUCTURED_OUTPUTS,
                McpCapability.PROGRESS,
            ],
        )
        assert result["compatible"] is True

        elicit_result = check_client_compatibility(
            "Desktop_Agent1",
            transport="streamable_http",
            required_capabilities=[McpCapability.ELICITATION],
        )
        assert elicit_result["compatible"] is True
        assert any("conditional for Claude" in d for d in elicit_result["diagnostics"])

    def test_agy_compatibility_evaluation(self):
        """Google Antigravity supports all MCP 2.x capabilities."""
        result = check_client_compatibility(
            "Desktop_Agent3",
            transport="streamable_http",
            required_capabilities=[
                McpCapability.TOOL_ANNOTATIONS,
                McpCapability.STRUCTURED_OUTPUTS,
                McpCapability.PROGRESS,
                McpCapability.RESOURCE_SUBSCRIPTIONS,
            ],
        )
        assert result["compatible"] is True
        assert len(result["missing_capabilities"]) == 0

    def test_unknown_client_reports_explicit_diagnostic(self):
        """Evaluating an unknown client name returns clear error without crashing."""
        result = check_client_compatibility("UnknownClient", transport="stdio")
        assert result["compatible"] is False
        assert "Unknown client 'UnknownClient'" in result["error"]
        assert len(result["diagnostics"]) > 0

    def test_matrix_markdown_rendering(self):
        """render_matrix_markdown produces valid markdown table with headers."""
        md = render_matrix_markdown()
        assert "# MCP 2.x Client Capability Matrix" in md
        assert "Desktop Agent5" in md
        assert "Claude Code / Claude Desktop" in md
        assert "Google Antigravity (Agy)" in md
        assert "Desktop_Agent2" in md
        assert "Cursor" in md
