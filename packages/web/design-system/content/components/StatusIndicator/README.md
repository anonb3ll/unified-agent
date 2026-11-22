StatusIndicator shows a state as a coloured dot with a glyph and an uppercase word.

- **Mapping** (case-insensitive): running, online, live, success, ok → `ua-ok` ●; done, completed, approved → `ua-ok` ✓; doing, in_progress, open → `ua-live` ◐; waiting → `ua-warn` ◐; error, blocked, failed, denied, danger, misconfigured → `ua-danger` ▲; warning, warn, pending → `ua-warn` ▲; anything else → `ua-muted` ○.
- **Consumer provides:** `status` (any string; the word defaults to it uppercased, "UNKNOWN" if empty), optional `label` to override the word, `size` `sm`/`md`/`lg`, `pulse` for live activity (stops under reduced motion), `showGlyph`.
- The word is the accessible signal; the dot is `aria-hidden`. Never show the dot without the word.
