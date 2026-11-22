# Healthcare administration (non-clinical) template pack

Use this pack **only** for administrative healthcare documents — scheduling
or credentialing letters, billing or operations reports, policy or training
memos — with a named **compliance lead** as the intended reviewer.
The pack does not enforce that role. This pack is
scoped to non-clinical administration on purpose: it must never be used for
patient records, clinical notes, diagnoses, or any other protected health
information. An agent submits the file tagged with a `client` and an
`engagement` value; the submission parks in the Control Center Inbox until someone
other than the submitter approves it, and the released copy plus its
evidence chain (who approved it, from what device, against which model) land
under the pack's release folder. This pack ships its optional two-person
rule (`two_person_rule` in the YAML) **enabled by default**, naming a
`privacy_officer` as the second reviewer, given the regulatory sensitivity of
the vertical — but that field is descriptive metadata, not an enforced
control: the platform still releases on exactly one named approval today.
Enforce the second sign-off operationally (limit who can reach
`approval_resolve` to the two named roles, and record the second reviewer's
sign-off outside the hub before the platform approval) until work-product role enforcement is available —
see the comment in `healthcare-admin.yaml` and
[docs/pilot/reviewer-quick-guide.md](../../reviewer-quick-guide.md). To use
this pack, copy this directory, edit the reviewer role names to match your
real staff roles, and point `UA_WORK_PRODUCT_TEMPLATE_DIR` at the folder
holding the `.yaml` file — see
[docs/work-product-release.md](../../../work-product-release.md).
