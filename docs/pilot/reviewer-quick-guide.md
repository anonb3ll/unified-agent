# Reviewer quick guide

For the person who approves or denies work in the Control Center Inbox
(`/ui/inbox?kind=approval`). The steps below describe the current Inbox and
approval API. Ask your hub operator to confirm the deployed version before
using this guide.

## What an approval in the Inbox shows

The Inbox lists what is waiting on you first, then everything else. An
approval that came from the H2 work-product workflow (see
[work-product-release.md](../work-product-release.md)) is a document someone
wants released — a report, a filing draft, a remediation package, anything
an agent produced that needs your sign-off before it goes out.

Opening an approval shows its detail:

| Field | What it means |
| --- | --- |
| Tool | The internal action name being requested (for a document release, this is the work-product tool). |
| Requested by | The agent that submitted the work. **The system will not let the same identity that submitted a document also approve its own release** — that rule is enforced in the software, not just written policy. |
| Requested | How long ago the request was made, and when. |
| Why it needs approval | The policy gate that held the request. |
| Task, Run | The task and run the request belongs to, when it has them. |
| Work product, Content hash, Reviewer role, Previous version | For a document release: the document's title, the hash of the exact content, the reviewer role the template pack expects, and the previous version's ID. The Inbox does not preview the document itself. |

Each approval is in one state: pending (waiting on you), approved but
unconsumed (you already said yes, but the release hasn't completed yet — this
clears itself automatically once it does), or expired (you approved it, but
nobody completed the release before the pass's time limit ran out; it is now
inert and the submitter must resubmit).

## Approve vs. deny

- **Approve** creates a one-time pass. The submitting agent must retry
  the identical `work_product_submit` call; that retry consumes the pass
  and copies the document into the organization's release folder. You
  cannot approve your own submission.
- **Deny** blocks a retry of the exact submitted request. The submitter
  can change the document or reviewed metadata and submit a new request.
- There is no separate "request changes" button. If the document needs
  changes, deny it and ask for a revised submission. Submissions with the
  same title and pack link to their previous version.

The decision and resolver are recorded. An approved but unconsumed pass
can be revoked through the operator approval API; the Inbox shows Approve
and Deny only for pending requests.

## Two-person rule (if your organization uses one)

Some policy packs name a second reviewer role. **The software does not
enforce a second approval or bind a pack's `reviewer_role` to an identity.**
A single authorized identity other than the submitter can approve, after
which the submitter's identical retry releases the document. If your
organization requires two people, obtain and record the second reviewer's
sign-off in your own approved process *before* the first reviewer approves
in the platform. The Inbox has no reviewer-notes field (a denial can carry a
reason). The
role registry from #1289 does not change this work-product behavior.

## "Unverified device" and "agent-bearer only" — what they mean

You will not see these exact words on the approval in the Inbox itself; they show
up afterward, in the evidence record for a decision you already made (the
evidence report, the provenance graph, or `unified-agent work-product
evidence <id>` — see [evidence-guide.md](evidence-guide.md)). Knowing what
they mean helps you understand how strong the record of *your* approval
actually is:

- **Agent-bearer only** means the approval used a static credential
  tied to an account label, rather than a live WorkOS session. A valid
  signature from an account classified as human can count as verified human
  authority in the platform report. `verified_via: "agent_bearer"` tells
  you the proof is control of that credential; `verified_via: "idp"`
  indicates the stronger, live identity-provider session. An agent account
  does not become human authority merely because its signature verifies.
- **Unverified device** means the record could not confirm which specific
  computer or device you approved from — for example, if the request did
  not arrive over the organization's private network. It does not mean
  anything suspicious happened; it means that particular piece of evidence
  (which device you used) fails closed rather than guessing. A verified
  device instead shows the specific machine name.

Neither of these affects whether your approval is *valid* — it is. They
affect how strong a claim your organization can later make about *who*, on
*what device*, made the call, which matters if this evidence is ever used
for an audit or a customer's own compliance review.

## If something looks wrong

If a request's title, content hash, or tags don't match what you expected,
or the requester is not who you think submitted it, deny it and tell the
submitter why through your approved channel. The web detail drawer accepts
an optional denial reason; an operator using `approval_resolve` can also
record one. Do not approve a request you have not reviewed.
