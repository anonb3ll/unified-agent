- **The pre-push gate checks the worktree being pushed, and fails closed.**
  A `git push` from `.worktrees/task-<id>` was linted and audited in the
  checkout that holds the hook, so the primary tree decided the push. The
  gate now runs ruff and the audit in that worktree. An unexpected error
  while those checks run blocks the push.
