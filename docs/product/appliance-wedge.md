# The appliance is the wedge

Task #1440. This note records the pull-through criterion and the one wedge
the fleet builds through. It does not change product positioning, pricing,
or outreach. Those stay with the operator (CHARTER). Product 1 remains the
Approval Ledger appliance.

## The two products

- **Product 1** is the single-tenant Approval Ledger appliance: evidence,
  approval, and attribution (epic #1276, H1 #1277). The customer-readiness
  guide describes that pilot boundary (`docs/customer-readiness.md`).
- **Product 2** is the multi-organization control plane: one deployment,
  many organizations, one portfolio view (#1416). The structural `org_id`
  chokepoint is still open (#1433).

Mission #1450 already says the appliance stays the wedge of that platform.
This note makes the pull-through measurable. It does not open a second
entry product.

## Falsifiable thesis

P1 is a wedge of P2 only if all three hold:

1. **Primitives.** Building P1 forces P2's hard primitives at one-org
   scope: org identity, verified human identity, a partition boundary, an
   evidence chain, and install/restore. What is already in the tree:
   evidence report (`docs/evidence-report.md`), the approval queue, the
   optional WorkOS identity leg (#1202 / #1249, described in
   `docs/customer-readiness.md`), and installable hub packaging (epic A,
   #1186). #1416 and #1433 are not done, so "forced" is not "P2 shipped."
2. **Second purchase.** The customer's second purchase is the platform
   (shared teams, context, autonomy, or a cross-org graph), not another
   isolated appliance. This is the measurable test below. It is not
   assumed true.
3. **One codebase.** P1 stays the same tree at one-org scope. No forked
   appliance roadmap.

If (2) fails in observation, P1 is a product and not a wedge. Say that
plainly when the count comes in. Do not paper over it with parity-feature
counts.

## Pull-through criterion

A **pull-through event** is an operator who already runs one appliance and
then asks for a second team or a second organization, where the thing they
cannot get from a second box is one of:

| Asked for | Platform track |
|---|---|
| Shared context across those teams | #1453, #1459 |
| A joint plan that both sides revise | #1455 |
| An autonomy mode that applies to both | #1457 |
| A work-graph view across both | #1460 |

A second isolated appliance, with none of those, is a **failed**
pull-through. Review the count at M2 (2026-10-11). Zero events does not
by itself kill the wedge; it means (2) is still unobserved. An observed
pattern of "just buy another box" does kill it.

## The two candidate wedges

| | (i) Compliance / evidence | (ii) Fleet / portfolio |
|---|---|---|
| Buyer | The person who must show which human approved what an agent did | Eng leadership feeling agent sprawl today (#1319) |
| Check | Slower procurement, larger | Faster, smaller, already multi-team |
| Time to sign | Longer | Shorter |
| What it teaches | Evidence, approval, attribution — the primitives that make the platform defensible | Fleet console and portfolio view |

**Wedge chosen: (i) the Approval Ledger appliance.** Reasons: mission #1450
already names the appliance as the wedge; the acceptance gate for this
task is that internal roadmap work must not rewrite product positioning;
(ii) would make the Fleet Console the entry product. Teams, shared
context, autonomy modes, and the work graph stay the pull-through, not a
second wedge.

## Anti-patterns

- A forked P1 roadmap with its own features.
- External on-ramps before the multi-org boundary (#1416, and #1429).
- Treating a count of parity features as pull-through.
- Pricing changes or customer outreach from this note.

## What this note does not decide

Pricing, outreach, and any customer-facing rewrite of positioning remain
the operator's. This record chooses which wedge engineering builds through, and
the measurement that would show the thesis fail.
