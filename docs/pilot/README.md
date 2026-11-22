# Pilot onboarding kit

Task #1280 (Epic H, H4). Everything a pilot organization's non-developer
staff need to run the H2 work-product release workflow
([work-product-release.md](../work-product-release.md)): policy starting
points, a reviewer's field guide, how to check the evidence, and how to get
support or leave cleanly. Written for a COO, risk owner, or reviewer at a
regulated professional-services organization — not for a developer.
The operational steps reflect the source reviewed for this kit; check the
deployed hub version before using them.

| If you need to... | Read this |
| --- | --- |
| Get an overview of what the pilot covers and does not cover | [Customer readiness](../customer-readiness.md) |
| Pick a starting policy for your vertical (professional services, accounting/tax, healthcare admin, legal) | [Policy template packs](policy-templates/) |
| Know what to do as a reviewer of approvals in the Inbox | [Reviewer quick guide](reviewer-quick-guide.md) |
| Export and check the evidence behind an approved release | [Evidence guide](evidence-guide.md) |
| Reach support, turn the pilot off, or export your data before exiting | [Support and rollback](support-and-rollback.md) |
| See which shipped features map to EU AI Act / ISO 42001 / SOC 2 controls | [Controls narrative](../controls-narrative.md) |
| Render and activate a dedicated single-tenant pilot appliance | [Appliance bundle runbook](appliance-bundle.md) |

## Policy template packs

Four synthetic starting points under [policy-templates/](policy-templates/),
one per vertical: [professional-services](policy-templates/professional-services/),
[accounting-tax](policy-templates/accounting-tax/),
[healthcare-admin](policy-templates/healthcare-admin/) (non-clinical
administration only), and [legal](policy-templates/legal/). Each is a YAML
file that loads through the same schema
(`unified_agent.tools.work_product.load_template_pack`) the platform's
shipped fixture packs use, plus a one-paragraph README. The
`tests/test_pilot_policy_templates.py` suite loads every pack through that
real loader; the Python test job checks them in CI.

Each pack names a `reviewer_role` and an optional `two_person_rule` block.
**Read the reviewer quick guide before assuming the two-person rule is
enforced by the software — it is not, yet.** It is documentation the pilot's
own account provisioning and process must carry until role-to-identity
work-product reviewer-role enforcement and multiple approvals are implemented.

## Honesty notes carried through this kit

- **`UA_WORK_PRODUCT_ENABLED` is off by default.** Nothing in this kit works
  until a hub operator turns it on.
- **An agent's cryptographic signature is never presented as human
  sign-off.** See [evidence-guide.md](evidence-guide.md#what-provenance_verified-means--and-does-not-mean).
- **The two-person rule in the policy packs is descriptive, not enforced.**
  See [reviewer-quick-guide.md](reviewer-quick-guide.md#two-person-rule-if-your-organization-uses-one).
- **This kit does not establish regulatory compliance, liability transfer,
  or multi-organization isolation.** See
  [Customer readiness](../customer-readiness.md) for the full scope
  statement.
