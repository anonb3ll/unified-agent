Badge is a small, read-only, uppercase label for a category or a count.

- **Variants:** `neutral` (default, `ua-muted` text), `info` (`ua-info` border, `ua-fg` text), `success`, `warning`, `danger` (state colour on text and border, always on `ua-surface`).
- **Consumer provides:** `children` (one or two words), optional `dot` for a leading dot in the current colour, `size` `sm` (default) or `md`.
- Not interactive. For a filter or toggle use Chip; for the live state of an agent or task use StatusIndicator.
