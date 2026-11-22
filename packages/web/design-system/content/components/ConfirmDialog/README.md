ConfirmDialog is the only way to confirm an action. It names the object and the consequence; `danger` opens with focus on Cancel.

- Never use `window.confirm`.
- **Form controls:** pass a reason field or picker as `children`; the dialog is then a form, `confirmDisabled` keeps Confirm (and Enter) off until the fields are valid, and Enter in a single-line field confirms.
- **Enter in a field:** confirms by default, except for `danger`, where the person must activate the confirm button; pass `submitOnEnter` to opt in.
