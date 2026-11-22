GraphCanvas draws a graph of nodes and labelled edges over `@xyflow/react`, with pan, zoom and fit controls, keyboard-focusable nodes (Enter or Space selects) and a Table view listing every node and its links, the equivalent for screen readers.

- **Consumer provides:** `label`, `nodes` (`id`, `label`, optional `description`, `tone`, `position`), `edges` (`id`, `source`, `target`, `label` read source to target: "blocks", "parent of"), optional `onNodeSelect` and `legend`.
- Nodes without a position are laid out in layers from the edges.
- Not in the artifact bundle: the graph library and its stylesheet are too heavy. See it in the app's UI showcase (`/ui/?showcase=primitives`).
