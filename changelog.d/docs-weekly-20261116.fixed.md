- **Operator docs cover this week's node, mail, approval, and review pitfalls.**
  Supervised ua-node commands (`node_command_start` / `status` / `adopt`) are
  off until `command` is in `UA_NODE_BACKENDS` and `argv[0]` is listed exactly
  in `UA_NODE_COMMANDS`. Session mail left in an ended session is stranded, not
  visible to the label's other sessions. An approval pass is spendable only by
  its requester or the human who resolved it; `task_delete` and
  `langgraph_governed_call` mint an operator self-approval. Reconcile no longer
  treats a local branch already in main as merged when a linked PR is still
  open. `unified-agent-consolidate restore` undeletes through `memory_restore`.
  A new `UA_HOME` is created mode 0700 despite umask. The Antigravity pre-push
  hook lints the worktree being pushed.
