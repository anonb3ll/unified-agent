- **Flushed partial harness output keeps a run active (#1916).** Executor stall
  detection tracks received stdout and stderr chunks without waiting for a
  newline. Silent children still stop, and captured output retains its text
  decoding and newline normalization.
