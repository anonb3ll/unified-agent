# Explicit agent-cloud-agent17 hub host relay (task #1789)

This source-only adapter is a resumable workflow for a cloud conversation whose
approved executor can run on the hub host. It uses the existing authenticated hub MCP
APIs. It adds no cloud tools, network reachability, installed hooks or background
service. A live relay depends on that executor connection being available.

Run `python -m unified_agent.cloud_bridge_cli` from the reviewed product checkout,
using its installed dependencies. Every invocation supplies `--state` (a JSON
file in an owner-private directory), `--issuer` (the existing HTTPS OAuth origin),
`--parent-thread`, `--parent-request` (stable across retries), and `--executor`.
Parent correlation ids and the executor label are **caller-reported declarations**,
not authenticated hub attribution. The hub principal must independently verify as `agent-cloud-agent17`, profile
`developer`, workspace `denied`. A policy overlay that downgrades the
identity makes the relay refuse operations. Ambient executor credentials are never used.

The private dedicated OAuth credential stays on the hub host. Its identity, owner,
mode, issuer and expiry are checked. The default transport is the existing
`http://127.0.0.1:8765/mcp` alias; public metadata must still identify the original
HTTPS OAuth issuer/resource before a bearer request is sent. The exact HTTPS
resource is also supported with normal TLS verification. No credential creation,
refresh, copy or configuration occurs. Authenticated MCP redirects are refused,
including same-origin redirects; the approved resource endpoint must stay exact.
SDK transport exception groups produce a generic failure message without raw
transport diagnostics; the checkpoint remains available for reconciliation.

## Packet and retry contract

- `snapshot` checks live identity/context and reads agent-cloud-agent17's label inbox in
  commit sequence order plus bounded queue pages. `snapshot --onboarding` also
  reads 1–16 explicitly selected Wiki notes through authenticated `vault_read`.
  Supply one `--vault-source Wiki/<note>.md` per chosen note; there are no
  installation-specific defaults. Paths must be unique, relative and below `Wiki/`.
  A pending packet must be consumed before changing the requested source list.
- Source content, dates, superseding annotations and truncation metadata remain
  intact. The registry distinguishes live hub authority, canonical repo contracts
  and dated Wiki guidance. Queue pages can be incomplete; examine pagination
  metadata before making scheduling decisions. Text in mail or notes never
  grants action permission.
- The packet is atomically checkpointed before output. Retries return the same
  pending packet, after a fresh identity/context check. Its `checked_at` is the
  original observation time. New mail waits until this packet is consumed.
- `consume --packet-id <exact-id>` explicitly commits **only the local cursor**
  after the parent has consumed the packet. It does not mark hub messages read,
  acknowledge them or register a session. Concurrent invocations fail closed.

## Explicit messages and human gates

`stage-message --action-id <stable-id> --request <private-reviewed-json>` stores
an intent locally. It sends nothing. Bodies and routing/correlation fields the hub would redact are refused
before staging or sending; sanitize the text and review that exact replacement
first. The JSON names an exact `to_agent`, `act`,
`body` and positive `task_id`; the caller must check the live task exists
before authorizing the intent. Optional fields are `conversation_id`, `reply_to`
and `expects_reply`.

`send-message` with the same arguments is an explicit outbound action and needs
its existing action-specific authorization and native approval review. It does
not circumvent an approval rejection. It verifies agent-cloud-agent17 before writing,
persists the intent, then uses the existing `message_send` idempotency key.
After a lost response, retry the same state, request and action id. Changing the
payload under that id is refused. An executor handoff reuses the same checkpoint
and preserves its action ledger. Keep that checkpoint; recreating it removes the
local history, so the adapter independently compares the actual stored hub message
before retrying and after receiving a send receipt. Sender/id dedupe alone never
proves the requested payload was accepted. A conflict or pruned/redacted body fails
closed for operator reconciliation. A delivery receipt is not a recipient reply,
task claim, technical verdict or human approval.

No automatic `delegate`, `task_claim`, session registration, acknowledgements,
merge, deploy or restart is supported. The existing non-idempotent delegation
API must not be blindly retried. Engineering work still requires its live claim,
technical review and exact-head shipping gates. The current explicit merge hold
remains in force.

## Acceptance still required

The source tests cover identity denial, private credential inputs, authority
matching, packet replay/cursor commitment, lock exclusion and one hub message
under a lost-response retry. They do not prove a live cloud-to-fleet cycle.

A full cycle must separately demonstrate parent packet consumption, an authorized
handoff to the existing owner, a correlated recipient reply/claim, technical
review, any required human decision, and returned checked evidence. Desktop
Hermes plugin loading/turn delivery, #1873 wake behavior, #1863 brain roundtrip
and #1864 browser acceptance retain their existing scopes and owners. Host hooks
do not propagate into cloud/voice sessions. No live message is sent merely to
test this draft.
