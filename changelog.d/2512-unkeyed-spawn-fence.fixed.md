- **A stalled spawn can no longer have a second job started beside it.** Only
  Control Center (keyed) launches fenced their spawn, so when a scheduler
  launcher stalled inside its spawn past its lease, reclaim handed the run back
  as `retryable` and the next unkeyed dispatch of the same task spawned again.
  Every dispatch run now marks its spawn (unkeyed ones under `run:<run_id>` in
  `dispatch_launch_keys`); reclaim keeps a run with an unsettled spawn claimed,
  claims and resumes of its task answer `launch_unsettled`, and the operator's
  `settle-spawn` route covers unkeyed runs under the same proof (#2512).
  A held run is announced once (a task note and an `agent.health.alert` event
  naming the settle route), listed by the scheduler tick and the queue-hygiene
  pass, and degrades the new `dispatch_unsettled_spawns` doctor row after 15
  minutes past its lease.
