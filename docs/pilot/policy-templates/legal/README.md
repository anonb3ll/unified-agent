# Legal template pack

Use this pack for legal work product — a memo, a filing draft, contract
markup, correspondence — where a named **supervising attorney** is the intended reviewer
before release. The pack does not enforce that role. An agent submits the file
tagged with a `matter` and a `client` value; the submission parks in the
Control Center Inbox until someone other than the submitter approves it, and the
released copy plus its evidence chain (who approved it, from what device,
against which model) land under the pack's release folder. This pack ships
its optional two-person rule (`two_person_rule` in the YAML) **enabled by
default**, naming a `reviewing_partner` as the second reviewer — but that
field is descriptive metadata, not an enforced control: the platform still
releases on exactly one named approval today. Enforce the second sign-off
operationally (limit who can reach `approval_resolve` to the two named
roles, and record the reviewing partner's sign-off outside the hub before the platform approval)
until work-product role enforcement is available — see the comment in
`legal.yaml` and
[docs/pilot/reviewer-quick-guide.md](../../reviewer-quick-guide.md). Nothing
here substitutes for the attorney's own professional judgment or for
jurisdiction-specific ethics rules on AI-assisted work product. This pack
shares its `id` (`legal`) with the synthetic fixture shipped in
`src/unified_agent/templates/work_product/legal.yaml`; pointing
`UA_WORK_PRODUCT_TEMPLATE_DIR` at a copy of this directory overrides that
fixture for a real deployment — see
[docs/work-product-release.md](../../../work-product-release.md).
