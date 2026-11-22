Chip is a toggleable filter button, optionally with a count or a remove action.

- **Use** for filter sets and quick toggles above lists and boards. Pressed chips fill with `ua-accent` and expose `aria-pressed`.
- **Consumer provides:** `children`, `pressed` (or `selected`), `onClick`, optional `count` (shown as "(14)" in tabular numerals), `size` `sm` or `md` (default), optional `onRemove`.
- With `onRemove` it renders two sibling buttons: the chip and a "Remove" ×. Backspace or Delete on the chip also removes it.
- Exported from the same source file as Badge (`src/lib/ui/Badge.tsx`).
