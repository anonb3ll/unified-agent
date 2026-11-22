# Self-Organization Architecture & Safety Invariant

**Date:** 2026-09-30  
**Task:** #1431  
**Status:** Implemented  

## Executive Summary

Task #1431 introduces self-organization as a toggleable platform capability in `unified_agent`. It operationalizes the core principle:
> **Agents always propose organizational changes; humans and policy always gate applying them.**

"Always on" governs the **proposing path only**. Applying any proposed change to roles, lanes, routing, delegation, or routines is **never automatic** and stays strictly behind the human approval gate with separation-of-duties enforcement.

---

## Architectural Pillars

### 1. Safety Invariant: Propose vs. Apply Boundary
- **Proposing**: Open by default to all authenticated agents within their legitimate office membership scope.
- **Applying**: Strictly gated.
  - Requires explicit human actor provenance (`config.actor_kind_for_identity(actor) == "human"`) or an approved policy gate via `approvals.check_approval_gate("org_proposal_apply", ..., force=True)`.
  - Agents attempting to self-apply are unconditionally refused with `PermissionError`.
  - Added `"org_proposal_apply"` to `SEPARATION_OF_DUTIES_TOOLS`, preventing any proposing agent from approving or applying their own change.

### 2. Feature Toggle via `control_flags`
- Reuses the existing `control_flags` table (`key="self_organization"`).
- Default behavior: **Enabled** (`True` if the flag is absent or not set to `"0"` / `"false"`).
- When toggled off via `set_self_organization_toggle(False)` or `org_proposal(action="toggle", params={"enabled": False})`, any call to `propose_org_change` raises a `RuntimeError` stating that org proposals are currently disabled.
- Proposing suppression is immediate and deterministic across all hub sessions.

### 3. Scope Enforcement (Scope-as-Membership, Task #704)
- Proposals are scoped to specific organizational offices (e.g. `product_engineering`, `platform_engineering`, `executive_office`).
- An agent proposing an organizational change must be an active member of that office per `org_registry.offices_for_identity(author)`.
- If an agent attempts to submit a proposal for an office outside its membership, `validate_proposal_scope` rejects the request immediately with `ScopeEscapeError`.
- System administrators or human actors (`Desktop_Person`, etc.) retain global scope authority.

### 4. Canonical Object Model & Lifecycle
- Declared as `ObjectType(kind="org_proposal", ...)` in `object_model.py`.
- Enforces an immutable SQL lifecycle constraint in `storage.py`:
  ```sql
  CHECK(status IN ('proposed', 'reviewed', 'applied', 'rejected'))
  ```
- Terminal states: `applied`, `rejected`.
- Writers: `propose`, `review`, `apply`, `reject`.
- Served in platform readers via `read_org_proposal` under the `task_list` permission gate.

### 5. Unified MCP Tool Dispatcher & Developer Profile Cap
- Exposed via a unified MCP tool `org_proposal(action, params)`:
  - `action="propose"`: Submit a proposal (`title`, `scope`, `rationale`, `proposed_changes`).
  - `action="list"`: Query proposals filtered by `scope` and `status`.
  - `action="get"`: Fetch proposal details by `proposal_id`.
  - `action="review"`: Record an assessment (`decision="reviewed"|"rejected"`, `review_notes`).
  - `action="apply"`: Gate-checked application by a human administrator.
  - `action="reject"`: Mark proposal rejected.
  - `action="toggle"`: Enable or disable the `self_organization` control flag.
- Dispatching all actions through a single MCP tool prevents inflating tool counts and keeps the default developer profile strictly under the 100-tool ceiling (`DEFAULT_PROFILE_MAX_TOOLS`).

### 6. WebUI Exposure
- Read-only visibility into pending, reviewed, applied, and rejected proposals via:
  - `GET /api/org-proposals`: List proposals with optional `scope` and `status` query filters.
  - `GET /api/org-proposals/{proposal_id}`: Retrieve detailed proposal specification and metadata.
- Integrated into `webui/routes.py` with full route inventory locking in `tests/test_webui_route_inventory.py`.

### 7. Execution Boundary: Ledger State vs Configuration Mutation
`apply_org_proposal` records the human authorization and transitions the proposal lifecycle to `applied` in the canonical ledger. In accordance with safety principles, mutating production configuration (e.g., editing `org.yaml`, updating routing rules, or provisioning new routines) is executed as a separate governed step by the human operator, steward, or follow-up execution pipeline based on the applied proposal specification.

---

## Verification & Test Matrix

- `tests/test_self_organization.py`:
  - Default-on proposing behavior.
  - Suppression when toggled off via `control_flags`.
  - Re-enabling via toggle.
  - Scope validation and `ScopeEscapeError` prevention.
  - Separation-of-duties enforcement and refusal of agent self-applying.
  - Successful human-gated approval and application.
  - Rejection and review lifecycle transitions.
  - Platform reader `read_org_proposal` integration.
  - Unified tool `org_proposal` action dispatching.
  - WebUI API endpoint responses.
- `tests/test_object_model.py`: Kind, table, served status, lifecycle, and writer seams verified.
- `tests/test_tool_classification.py`: Classification counts verified.
- `tests/test_server.py`: MCP tool enumeration and roundtrip verified.
- `tests/test_webui_route_inventory.py`: Exact Starlette routes verified.
