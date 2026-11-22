# Existing delegation execution approval

Creating a delegation and executing it are separate approval scopes. A consumed
`delegate` creation grant or a GitHub review approval cannot authorize execution
of an existing child. The plan widget needs a linked waiting plan and the normal
Dispatch form creates a new child; neither repairs an existing child's missing
execution approval.

An authenticated requester with the existing approval-request capability may send
`POST /api/approvals/request` with the following exact body after this fix is deployed:

```json
{"tool":"dispatch_claim","payload":{"action":"dispatch","task_id":1974,"delegation_id":64}}
```

For task 1975 use delegation 65. Positive integer IDs must identify the same
existing, visible organization-owned pair. Other fields, identity overrides,
foreign or ambiguous approval rows and non-dispatchable states are refused.
This request queues or returns the existing exact approval; it never creates a
delegation, launches a worker, inserts an approved marker or consumes a pass.
Ordinary tools retain their existing registered/configured-gate requirements.

The human handoff is Control Center → Approvals → Pending → open each returned
approval ID. Verify **tool `dispatch_claim`**, **action `dispatch`** and the
matching task/delegation IDs, then select **Approve** while authenticated as
the human resolver. The server records the verified resolver and grant time.
Requester self-approval and agent impersonation remain refused. The API response
is only a request receipt, not evidence that the human approved or a worker ran.

Then refresh `GET /api/dispatch-stack` for each exact task, project and lane.
Only when eligibility succeeds, submit the existing task-pinned
`POST /api/action/dispatch-stack` once and verify its actual run/claim/launch
receipt. Dependencies, capacity, quota, current lifecycle and the coordinator's
locked approval recheck still apply. A revoked or expired pass cannot launch;
a fresh request requires a new human decision. No credential or gate configuration
change is needed. Do not use the ordinary Dispatch form to retry these children.

On the deployed predecessor the internal `dispatch_claim` tool is rejected by
the generic request endpoint, so the returned approval IDs do not yet exist.
Do not imply that repeated conversational approvals create those records.


The winning claim consumes only the exact tenant-bound grant and records its
grant generation and run lineage in the existing approval-consumption ledger.
Mixed foreign/null-tenant rows refuse authority without changing other grants.
Retry or resume requires that proof and the original grant lifetime; it cannot
reuse a completed run's consumed pass. A verified human may use the supported
`approval_resolve(id, "revoke")` operation to withdraw a consumed grant's proven
outstanding recovery authority after a failed launch. The resolver and claim/
resume use the same write lock. This does not reopen completed grants or grant
agents a human identity. The WebUI's approve/deny endpoint is unchanged.

Invalid or ambiguous organization/grant state is distinct from a genuinely
absent execution grant. Preview, locked claim, resume and consumption reject
that invalid state before considering an older operator plan marker. Valid
marker-only legacy approval remains supported when the exact pair is owned by
the current organization and no execution grant exists.
