Region is the one wrapper for hub data (design protocol §5). It owns the seven truth states so a pane cannot turn a failed read into "Nothing waiting".

- **States:** `loading` (skeleton rows, `aria-busy`), `ready`, `empty` (the `emptyText` fact sentence, only after every read succeeded), `partial` (data plus "1 of 2 sources failed"), `stale` (last known data, warn stamp), `denied` (401/403: "Your profile can't read …"), `unavailable` (hatched block with the failure and Retry; never a zero).
- **Consumer provides:** `title`, `queries` (TanStack results or anything with `status`, `data`, `error`, `dataUpdatedAt`, `refetch`), `isEmpty`, `emptyText`, optional `staleAfterMs`, `actions`, and `children` as a function so it only runs with data.
- Counts elsewhere (badges, tiles) use `countLabel(state, n)`, which returns "—" unless the region is `ready` or `empty`.
