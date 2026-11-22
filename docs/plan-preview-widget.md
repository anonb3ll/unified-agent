# Plan review `::preview` widget

Task #838 (ECC Plan Canvas analog). The **desktop** already owns the iframe:
`::preview{file="…"}` live-renders workspace HTML and `data-hermes-send` /
`window.hermes.send` posts a hidden user turn (trimmed, 500-character cap,
one intent per second). The **hub** owns plan state and the human
plan-confirmation gate. This file is the stable contract between them.

## Ownership

| Piece | Owner | Not |
|---|---|---|
| `::preview` iframe, `hermes.send` bridge, file read after the turn settles | Hermes desktop | Do not reimplement in unified-agent |
| Escaped plan HTML, fingerprint, rewrite of the same path | `plan_manage("preview")` | No arbitrary JS/HTML from the plan body |
| Approve / request-changes / annotate | `plan_manage("review", {intent})` | No second approval table |
| `[waiting-for: plan-approval]` → `[plan: approved @ <ts>]` | Existing task notes (task #65) | The review pipeline still never approves |

## Preview

```text
plan_manage("preview", {"plan_id": "<12-hex>"})
→ {
    html_path, html_relpath,
    directive: '::preview{file=".unified-agent/plan-preview/<id>.html"}',
    fingerprint, approved
  }
```

The agent emits `directive` as its own paragraph. Rewriting the same HTML
path is the live-reload mechanism: after the turn settles, desktop re-reads
the file. A later assistant message that re-emits the directive remounts the
frame.

Plan goal, step text, and annotation comments are HTML-escaped. The only
`data-hermes-send` values the generator emits are:

```text
plan-review v1 approve <plan_id>
plan-review v1 changes <plan_id>
```

Click-to-comment uses a fixed chrome script (no plan text inside it) that
prompts and sends:

```text
plan-review v1 annotate <plan_id> <step_idx> <comment>
```

`step_idx` is the 0-based plan step. Display labels are 1-based ("Section 1").

## Review

Pass the hidden user turn through unchanged:

```text
plan_manage("review", {"intent": "<exact payload>", "task_id": <optional>})
```

| Intent | Gate effect |
|---|---|
| `approve` | Requires a non-terminal task with `[waiting-for: plan-approval]` and `plan: <id>`. Replaces the waiting marker with `[plan: approved @ <ts>]`. Idempotent if already approved. Does not auto-complete the plan or dispatch. |
| `changes` | Same gate required. Appends `[plan: changes-requested @ <ts>] <fingerprint>`. **Keeps** the waiting marker. Idempotent on the current plan fingerprint. |
| `annotate` | Same gate required. Appends `[plan-annotation:<id>] @ <ts>: <comment>` to the step note. Idempotent on `(plan, step, comment)`. |

Off-schema intents are refused. Research may `preview` but not `review`.

## Security

- Plan body and comments are untrusted text: escaped in the widget, never
  interpolated into `<script>` or event-handler attributes.
- Bridge actions are a closed schema. The desktop still sandboxes the iframe
  (`sandbox="allow-scripts"` without `allow-same-origin`).
- Applying `approve` does not weaken the human gate: it is the same note
  rewrite the docs already describe for the operator. The automated
  `review_plan_approvals` consumer remains read-only.
