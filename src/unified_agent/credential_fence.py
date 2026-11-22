"""One exclusive fence between hub credential changes and policy publication.

A policy-authorized Approval Ledger success is checked against the signer's
current bearer and then posted to GitHub (PR #1302 review). A remote POST
cannot be made atomic with that local check, so the publisher holds this fence
from its final verification through the POST and its post-POST check (with a
short request timeout), and every hub path that changes an effective bearer
takes it too:

- managed token issue and rotation (``tools.token_lifecycle._claim_lock``);
- any write or delete of an ``agent-token:`` managed secret, including a
  generic approved ``managed_secret_set`` (``managed_secrets.set_secret`` and
  ``delete_secret``);
- ``unified-agent init`` rewriting a file-backed ``UA_AGENT_TOKENS`` (which a
  running hub re-reads), and its publication rollback restoring the token
  stores (``bootstrap._update_token_file``, ``_restore_token_stores``).
  ``service.env`` itself is read only at process start, so writing it is not
  a live bearer change.

A rotation through hub code therefore lands either before the verification
(and nothing is posted) or after the post-POST check.

The fence is re-entrant within one thread, so a lifecycle operation that
writes its managed secret under the fence does not deadlock on itself. Across
threads and processes it is an exclusive ``flock``. Lock order is always this
fence first, then any narrower lock (the lifecycle lock, the managed-secret
write lock).

Changes outside hub code, such as hand-editing the token file or a restart
with a new environment, cannot take this fence. The publisher's re-check
straight after the POST, and reconciliation's re-check of any publication not
durably verified, bound that residual window and correct it.
"""

from __future__ import annotations

import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import config

LOCK_FILE = "credential-authority.lock"

_held = threading.local()


def authority_lock_path() -> Path:
    return config.data_home() / LOCK_FILE


def held_by_this_thread() -> bool:
    return getattr(_held, "depth", 0) > 0


@contextmanager
def authority_fence() -> Iterator[None]:
    """Hold the hub-wide credential fence (an exclusive ``flock``, re-entrant per thread)."""
    if held_by_this_thread():
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    path = authority_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
