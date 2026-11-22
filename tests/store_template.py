"""One initialized store per process, copied into each test's home (#2238).

``storage.init_db()`` on an empty ``UA_HOME`` creates every table, index,
trigger and virtual table, then runs the post-init hooks: about two to three
seconds, paid by every stateful test before its body runs. On a home that
already holds an initialized ``memory.db`` the same call takes well under a
tenth of a second, because each ``CREATE ... IF NOT EXISTS`` and migration
finds its work done.

``seed_store`` therefore copies a template ``memory.db`` into the test's home
and then runs ``init_db`` as before, so every schema script, migration and
post-init hook still runs against the test's own file. The template is the
store the first such test in this process produced, saved before that test's
body ran. A fresh store holds no per-home paths or rows (schema, FTS config
and the one-time backfill flags a fresh init sets anyway), so the copy is the
store a fresh init would have built.

A template is kept per ``UA_*`` environment (paths under the test's own
``tmp_path`` left out), so a module that changes the environment the store is
built under gets its own. ``UA_TEST_FRESH_STORE=1`` turns the copy off.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path


def fingerprint(tmp_path: Path, environ: dict[str, str] | None = None) -> str:
    """The ``UA_*`` environment a store is built under, minus per-test paths."""
    environ = os.environ if environ is None else environ
    own = str(tmp_path)
    items = sorted(
        (key, value) for key, value in environ.items() if key.startswith("UA_") and own not in value
    )
    return hashlib.sha256(repr(items).encode()).hexdigest()[:16]


def seed_store(
    template_dir: Path,
    tmp_path: Path,
    db_path: Path,
    init_db: Callable[[], None],
) -> None:
    """Initialize the store at ``db_path``, starting from a template if one exists."""
    if os.environ.get("UA_TEST_FRESH_STORE") == "1":
        init_db()
        return
    template = template_dir / f"{fingerprint(tmp_path)}.db"
    if template.is_file() and not db_path.exists():
        db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy(template, db_path)  # keeps the file's 0600 mode
    init_db()
    if template.is_file():
        return
    # A store at rest is the single file; a leftover WAL or journal means
    # pages a plain copy would miss, so that store is not used as a template.
    if any(Path(f"{db_path}{suffix}").exists() for suffix in ("-wal", "-journal")):
        return
    partial = template.with_suffix(".partial")
    shutil.copy(db_path, partial)
    partial.replace(template)
