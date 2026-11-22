# Professional services (general) template pack

Use this pack for a general client deliverable — a report, a proposal, a
status memo — where an **engagement lead** is the intended reviewer before the
document goes to the client. The pack does not enforce that role. An agent submits the file tagged with
a `client` and an `engagement` value; the hub parks the submission in the
Control Center Inbox until someone other than the submitter approves it; after the submitter retries the identical call with an approved pass,
the exact bytes are copied into the release folder and the
evidence chain records who approved it, from what device, and against which
model. The optional two-person rule (`two_person_rule` in the YAML, off by
default here) documents a second named reviewer role for engagements you
consider higher-stakes; it is not automatically enforced by the platform
today — see the comment in `professional-services.yaml` and
[docs/pilot/reviewer-quick-guide.md](../../reviewer-quick-guide.md) for what
that means in practice. To use this pack, copy this directory somewhere your
hub operator manages, edit the reviewer role names to match your real staff
roles, and point `UA_WORK_PRODUCT_TEMPLATE_DIR` at the folder holding the
`.yaml` file (not this README) — see
[docs/work-product-release.md](../../../work-product-release.md).
