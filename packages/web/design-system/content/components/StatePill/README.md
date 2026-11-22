StatePill shows one of six closed states as a colour, a glyph and an uppercase word (design protocol §6.1).

- **Kinds:** `decision` ◆ (`ua-decision`, a human must decide), `danger` ▲ (blocked, failed, denied), `live` ◐ (running), `warn` ◷ (waiting, stale), `ok` ✓ (verified, done), `unknown` ? (hatched `ua-unknown`; never read as idle or zero).
- **Consumer provides:** `kind`, optional `label` to replace the default word. Keep the word short; it is the accessible signal and the glyph is `aria-hidden`.
- The vocabulary is closed. Map a new status onto one of these kinds; do not add a colour.
