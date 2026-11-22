Card groups related content on `ua-surface` with a hairline `ua-border` and `radius-lg`.

- **Compose** with `Card.Header` (flex row; `bordered` adds a rule), `Card.Title` (h3 by default, `card-title`), `Card.Description` (`ua-muted`), `Card.Body`, `Card.Footer` (12px muted metadata; `bordered` adds a rule).
- **Variants:** `default`/`surface`, `sunken` (on `ua-bg`), `interactive`. Padding `none`, `sm`, `md` (default, `space-4`), `lg`.
- **Interactive:** passing `onClick` or `interactive` makes it a focusable `role="button"` with an accent hover border; Enter and Space activate it only when the card itself has focus, so nested inputs and buttons keep working. Give it an `aria-label`.
- `as` picks the element: `article`, `section`, `div` (default) or `li`.
- **Don't** nest cards, or add shadows: depth is surface on ground plus border.
