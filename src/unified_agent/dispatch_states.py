"""Approved-dispatch run states, shared by the coordinator and its readers.

The coordinator (``dispatch/``) moves runs between these states. Run records,
delivery SLOs, receipts and the agent lifecycle read them. Keeping the
vocabulary here lets those readers agree with the coordinator without
importing it.
"""

# A run that holds a claim: it may still launch or is running.
LIVE_STATES = frozenset({"claimed", "launched", "running"})

# A run that will not change again. Delivery receipts and the delivery SLO
# metrics both use exactly this set, so their notion of "terminal" cannot drift.
TERMINAL_RUN_STATES = frozenset({"finished", "failed", "blocked", "denied", "cancelled", "expired"})
