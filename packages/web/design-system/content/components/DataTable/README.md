DataTable renders rows of records in a scrollable, labelled region with keyboard row navigation.

- **Consumer provides:** `columns` (`header`, and either `accessor` — a key or function — or `cell(row, i)`; optional `align`, `width`, class names), `data`, a stable key via `getRowKey` or `keyField`, and a `caption` (screen-reader only unless `captionVisuallyHidden={false}`).
- `onRowClick` makes rows focusable with a roving tabindex: Arrow Up/Down, Home and End move, Enter or Space opens. Events from controls inside cells are ignored, so call `stopPropagation` in a cell button's click handler.
- Empty: `emptyText` (default "No data available") or `renderEmpty`, announced with `role="status"`. Write it as a sentence: "No open sessions."
- Headers are `table-head` (uppercase, `ua-muted`); cells are `body` in `ua-fg`. Put ids in `mono`, states in StatusIndicator, agents in AgentPersonaBadge.
