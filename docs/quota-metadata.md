# Quota metadata reads

Quota projections and credential metadata reads use the initialized live SQLite
store through `storage.connect_read_only()`. They do not bootstrap a missing
store, run schema migrations, chmod the store, or write application metadata.
`mode=ro` prevents creation of the main `memory.db`, and `query_only` rejects SQL
writes through the connection. Missing, corrupt, retired, incompatible, or
inaccessible metadata is reported as unavailable rather than initialized.

This is application read-only access, not a promise of zero filesystem writes.
SQLite's normal WAL reader may create or write `memory.db-wal` and
`memory.db-shm` coordination files when they are absent. The main database bytes
and application rows remain unchanged by the read. A fully non-writable directory
containing a checkpointed WAL database without these sidecars is unsupported:
the reader reports metadata unavailable and the advisory projection retains an
explicit metadata error. Deployments needing that mount layout must provide a
SQLite-supported readable WAL/SHM arrangement or use a writable metadata store.

Readers retain normal WAL coordination because a live writer can commit rows
which are still in the WAL and have not reached the main database. `immutable=1`
is deliberately not used: it can ignore that committed live state. No copied
snapshot replaces the live metadata authority.
