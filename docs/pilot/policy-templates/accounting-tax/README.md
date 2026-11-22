# Accounting / tax template pack

Use this pack for accounting or tax work product — a draft return, a
workpaper, a reconciliation memo, a management letter — where a **reviewing
CPA** is the intended approver before release, mirroring the
preparer/reviewer separation most accounting and tax practices already
require. An agent submits the file tagged with a `client` and an
`engagement` value; the submission parks in the Control Center Inbox until someone
other than the submitter approves it, and the released copy plus its
evidence chain (who approved it, from what device, against which model) land
under the pack's release folder. This pack ships its optional two-person
rule (`two_person_rule` in the YAML) **enabled by default**, naming an
`engagement_partner` as the second reviewer — but that field is descriptive
metadata, not an enforced control: the platform still releases on exactly
one named approval today. Enforce the second sign-off operationally (limit
who can reach `approval_resolve` to the two named roles, and record the second review externally before approving in the platform) until work-product role enforcement is available — see the comment in `accounting-tax.yaml` and
[docs/pilot/reviewer-quick-guide.md](../../reviewer-quick-guide.md). To use
this pack, copy this directory, edit the reviewer role names to match your
real staff roles, and point `UA_WORK_PRODUCT_TEMPLATE_DIR` at the folder
holding the `.yaml` file — see
[docs/work-product-release.md](../../../work-product-release.md).
