FreshnessStamp says how current a region is (design protocol §6.2): "updated 4s ago", "stale · 26h", "partial", "no access", "unavailable".

- **Consumer provides:** `state` (a `TruthState`), `updatedAt` (epoch ms of the oldest successful read, or null), optional `now` for tests.
- Stale and partial use `ua-warn`; unavailable uses `ua-danger`. The exact time is in the `title`.
- Every Region header carries one; the top bar carries the stream's.
