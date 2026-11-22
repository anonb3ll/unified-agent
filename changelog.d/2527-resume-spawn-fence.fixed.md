- **A resumed run's spawn is fenced like a new claim's.** `resume_run` refused
  while a spawn of the task was unsettled, but spawned its own relaunch without
  marking it. When that spawn stalled past its lease, reclaim handed the run
  back as `retryable` and the next scheduler dispatch started a second job
  beside it. A resume now arms its own spawn mark (`run:<run_id>:resume`) in
  the transaction that claims the run, then spawns through the same fence,
  outcome and in-flight steps as a claim. A stalled resumed spawn holds its run
  and task, is reported like any unsettled spawn, and the operator's
  `settle-spawn` route covers it. The claim's own mark is left as it was, so a
  Control Center key's launch evidence survives a resume (#2527).
