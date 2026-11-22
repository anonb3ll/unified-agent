# Direct task completion

Merge and reviewer completion use the task/run identity captured before their
validation, GitHub lookup, or merge work begins. The snapshot includes status,
claimant, cancellation, the current run and owner, notes and revision, and the
open ledger run. The task row and ledger are read in one SQLite snapshot.

`complete_direct` checks that identity under `BEGIN IMMEDIATE`, applies any
reviewer delegation blocker, and writes the task completion and run finish in
the same transaction. A failed or declined run record rolls back the task
write. A same-agent release and reclaim is a successor and cannot be completed
by the old operation. Note changes are checked even when timestamps have the
same second. A dangling run on an already-finished lifecycle is repaired as
`abandoned`, with unknown duration (`wall_s=null`). A previously handed-off run
is not finished twice.

The reviewer carries its snapshot through the nested merge call. Its CI note is
an atomic compare-and-swap of that snapshot and advances only the fields the
reviewer itself wrote. An identity mismatch is a transient refusal; it does not
leave a permanent same-head `yolo-merge-failed` marker that suppresses a later
valid review. All scoped review notes use the same atomic identity check,
including classification verdicts and done-task failure notes. Successful
legacy and mission closeout advance the scope only to their own task/ledger
snapshot captured inside the owning transaction, never a later fresh read.
Post-merge verification warnings and legacy delegation closeout notes follow
the same rule: their own writes advance the snapshot, while a successor stops
the annotation or closeout.
Distinct unclaimed completions use unique finish-event identities; replaying
the original snapshot cannot complete a reopened lifecycle.
Successor provisioning clears the predecessor's review markers,
so the successor owner must explicitly submit their own work for review.

Legacy merges check the snapshot again before starting Git mutations and
complete the original task/run before checkout cleanup. A refused completion
preserves the checkout. Git effects already performed are not rolled back by
this task/run transaction. This API does not supply full Git-effect ownership
serialization for legacy tasks: a takeover during Git or after successful
completion remains outside its guarantee. Mission-linked merges retain their
existing separate source, task, delegation, and cleanup fences.

The old `record_direct_completion` helper performed best-effort bookkeeping
after an unfenced task SQL write. It has been removed. Callers must capture the
original identity and use `complete_direct` for both the task write and ledger
finish; there is no successful post-write compatibility fallback.

These fixes recover the compatible direct-completion changes from the paused
Hermes audit lane. Its older executor-wide SQLite lock and remote-execution
refusal design are not part of this change. Executor run isolation and canonical
delegation fencing continue to use the implementation delivered by PR #895.

`tests/test_direct_completion_fencing.py` exercises real isolated SQLite state
and deterministic same-agent takeovers in merge, reconciliation, and review.
It also covers ledger rollback, writer serialization, abandoned-run duration,
CI annotations, and retrying a superseded review. Git and authority lookup
seams are intercepted in those race tests; the existing reviewer integration
tests also exercise real temporary repositories.
