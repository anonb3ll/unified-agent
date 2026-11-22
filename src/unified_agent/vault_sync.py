"""Continuous hub-authoritative vault sync (task #1288, extends task #816/PR #421).

Task #816 shipped ``deploy/vault-sync.sh``: a bidirectional ``git pull --rebase``
between the desktop and Mac hub copies of ``~/vaults/main``, installed by hand and
never scheduled. Two gaps showed up after that shipped:

- Whole-repo rebase has no per-path conflict semantics. A desktop-side edit to a
  hub-authoritative note either blocks the rebase outright or (worse) merges
  clean and silently overwrites the operator's edit — there is no "keep both,
  flag it" path.
- A retired tree can be resurrected. Task #1256 found ``Logs/Laptop/`` — a
  directory retired on the desktop side — reappearing in the hub vault because
  nothing filtered it out of the sync.

This module replaces the transport (SSH file sync, not a git rebase) and
keeps the same operational shape (desktop-initiated, SSH to the Mac hub) while
adding path-scoped conflict handling:

- The hub is authoritative for ``Wiki/``, ``Logs/`` and ``00 - Inbox/``
  (``DEFAULT_SYNCED_TREES``). A hub file newer than its desktop counterpart is
  pulled and overwrites the desktop copy.
- A desktop file newer than its hub counterpart is never silently overwritten.
  The incoming hub content is written alongside it as
  ``<name>.conflict-<UTC timestamp>.<ext>`` instead, and the desktop original is
  left untouched. Same handling for a desktop-only file under a synced tree that
  the hub has never seen. A conflict already captured by an existing copy of
  the same remote version is not re-copied on every run (task #705 review).
- A path under a retired prefix (``DEFAULT_RETIRED_PATHS``) is skipped in both
  directions — it is not pulled from the hub and not flagged as a desktop-side
  divergence. This is what keeps #1256 fixed permanently rather than once.
- Conflict artifacts (already-suffixed ``*.conflict-*`` files) are never
  themselves treated as new divergences, so a conflict pull does not create an
  infinite chain of conflict-of-a-conflict copies.
- A remote-reported path that would resolve outside ``local_root`` (absolute,
  ``..``-traversing, or otherwise escaping) is never touched — recorded as
  ``unsafe_skipped`` instead (task #705 review: the hub is a lower-trust input
  source than the desktop's own filesystem walk).

Transport is an injectable interface (``VaultSyncTransport``) precisely so the
sync engine (``sync_once``) can be exercised in tests with a plain local
directory standing in for the remote (``LocalDirTransport``), with no SSH, no
network and no real vault involved. ``SSHTransport`` is the production
implementation, used by ``main()``. Its remote file listing shells out to
``python3`` on the remote host (not ``find -printf``, which is GNU-only and
fails outright on the Mac hub's BSD ``find`` — task #705 review caught this
before it ever ran against the real hub), so behavior is identical whether the
remote userland is GNU or BSD.

Every run writes a small JSON state file (``config.vault_sync_state_path()``)
that ``tools/doctor.py``'s ``vault_sync`` check reads to report freshness and
outstanding conflict counts — see that module for the LIVE/DORMANT/MISCONFIGURED
contract. An unreachable or failed run never erases the previous run's
outstanding-conflict picture, and a dry run never advances the success
timestamp — both were task #705 review findings against the first version of
this state-merge logic.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import fnmatch
import glob
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from . import config
from .config import (  # noqa: F401 - defined in config; re-exported here
    DEFAULT_RETIRED_PATHS,
    DEFAULT_SYNCED_TREES,
)

# Same-file mtime jitter tolerance. Some filesystem copy paths round mtime to
# whole seconds; without slack, a file that round-trips
# unchanged can read as "differs" purely from float rounding and get flagged
# as a same_mtime_diff conflict every single run.
MTIME_TOLERANCE_S = 2.0

_CONFLICT_MARKER = ".conflict-"
_CONFLICT_ARTIFACT_RE = re.compile(
    r"^.+\.conflict-(?:\d{8}T\d{6}Z(?:-[0-9a-f]{6})?|desktop-[0-9a-f]{16})(?:\..+)?$"
)
_CONFLICT_TS_FMT = "%Y%m%dT%H%M%SZ"


class VaultSyncTransportError(RuntimeError):
    """Remote enumeration or transfer failed — never silently treated as 'empty'."""


class VaultSyncStateError(RuntimeError):
    """Existing sync state is unreadable; preserve it for operator recovery."""


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat(timespec="microseconds")


def _conflict_filename(local_path: Path) -> str:
    stamp = _now().strftime(_CONFLICT_TS_FMT)
    return f"{local_path.stem}{_CONFLICT_MARKER}{stamp}-{secrets.token_hex(3)}{local_path.suffix}"


def _is_conflict_artifact(relpath: str) -> bool:
    return _CONFLICT_ARTIFACT_RE.fullmatch(Path(relpath).name) is not None


def _is_ignored(relpath: str, ignored_globs: Sequence[str]) -> bool:
    """Machine-local noise that is never synced in either direction."""
    return any(fnmatch.fnmatchcase(relpath, pattern) for pattern in ignored_globs)


def _is_retired(relpath: str, retired_paths: Sequence[str]) -> bool:
    for retired in retired_paths:
        retired = retired.strip().strip("/")
        if not retired:
            continue
        if relpath == retired or relpath.startswith(retired + "/"):
            return True
    return False


def _is_safe_relpath(local_root: Path, relpath: str) -> bool:
    """False for anything that would resolve outside ``local_root``.

    The hub is a lower-trust input relative to the desktop's own filesystem
    walk: its file listing is attacker-reachable in principle (a crafted or
    corrupted remote path), while the desktop-only-file loop only ever walks
    paths this process already knows are under ``local_root``. Rejects an
    absolute path, a Windows-style rooted path, and any ``..`` traversal
    segment outright before even joining, then confirms the joined-and-
    resolved candidate is still contained (task #705 security review).
    """
    if not relpath or relpath.startswith("/") or relpath.startswith("\\"):
        return False
    candidate = Path(relpath)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    # Even a symlink that currently points inside the vault may be replaced
    # before transfer. Never use symlinked destination components.
    current = local_root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            return False
    try:
        resolved_root = local_root.resolve()
        resolved_candidate = (local_root / candidate).resolve()
    except OSError:
        return False
    return resolved_candidate.is_relative_to(resolved_root)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteFile:
    relpath: str
    mtime: float
    size: int


class VaultSyncTransport(Protocol):
    """What the sync engine needs from "the remote" — real or faked in tests."""

    def reachable(self) -> bool: ...

    def list_files(self, trees: Sequence[str]) -> dict[str, RemoteFile]: ...

    def stat_file(self, relpath: str) -> RemoteFile: ...

    def pull_file(self, relpath: str, dest: Path) -> None: ...

    def push_conflict_copy(self, relpath_hint: str, src: Path) -> None: ...

    def hash_files(self, relpaths: Sequence[str]) -> dict[str, str]: ...


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pushed_conflict_name(relpath_hint: str, src: Path, *, content_hash: str | None = None) -> str:
    """Give every source path and byte version a distinct hub triage filename."""
    identity = f"{relpath_hint}\0{content_hash or _sha256_path(src)}".encode(
        "utf-8", "surrogateescape"
    )
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return f"{src.stem[:80]}.conflict-desktop-{digest}{src.suffix}"


@contextmanager
def _upload_snapshot(src: Path, *, directory: Path | None = None):
    """Stage one stable byte version for naming, probing, and uploading."""
    fd, name = tempfile.mkstemp(prefix=".vault-sync-upload-", dir=directory)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as target, src.open("rb") as source:
            before = os.fstat(source.fileno())
            shutil.copyfileobj(source, target)
            after = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise VaultSyncTransportError("conflict source changed during upload staging")
        yield staged, _sha256_path(staged)
    finally:
        staged.unlink(missing_ok=True)


def _fingerprint(path: Path) -> tuple[int, int, int, int] | None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _publish_staged(
    local_root: Path,
    relpath: str,
    staged: Path,
    expected: tuple[int, int, int, int] | None,
) -> bool:
    """Atomically install a download only if the destination has not changed.

    Walk parent directories through directory descriptors with O_NOFOLLOW, so
    replacing a vault directory with a symlink cannot redirect the write.
    ``False`` means another local writer changed the destination; the caller
    keeps the downloaded hub version as a conflict copy instead.
    """
    parts = Path(relpath).parts
    if not parts or Path(relpath).is_absolute() or ".." in parts:
        raise VaultSyncTransportError("unsafe local destination")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(local_root, flags)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        try:
            st = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        else:
            if stat.S_ISLNK(st.st_mode):
                raise VaultSyncTransportError("symlink at local destination")
            current = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if current != expected:
            return False
        os.replace(staged, parts[-1], dst_dir_fd=parent_fd)
        return True
    finally:
        os.close(parent_fd)


def _stage_pull(local_root: Path, transport: VaultSyncTransport, remote: RemoteFile) -> Path:
    relpath = remote.relpath
    fd, name = tempfile.mkstemp(prefix=".vault-sync-", dir=local_root)
    os.close(fd)
    staged = Path(name)
    try:
        if transport.stat_file(relpath) != remote:
            raise VaultSyncTransportError("hub file changed since enumeration")
        expected_hash = transport.hash_files([relpath]).get(relpath)
        if not expected_hash:
            raise VaultSyncTransportError("hub file hash unavailable before pull")
        transport.pull_file(relpath, staged)
        if staged.stat().st_size != remote.size or _sha256_path(staged) != expected_hash:
            raise VaultSyncTransportError("incomplete or changed hub file during pull")
        if (
            transport.stat_file(relpath) != remote
            or transport.hash_files([relpath]).get(relpath) != expected_hash
        ):
            raise VaultSyncTransportError("hub file changed during pull")
        os.utime(staged, (remote.mtime, remote.mtime))
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _publish_conflict(local_root: Path, relpath: str, staged: Path) -> Path:
    local_path = local_root / relpath
    for _ in range(3):
        conflict_path = local_path.with_name(_conflict_filename(local_path))
        conflict_rel = conflict_path.relative_to(local_root).as_posix()
        if _publish_staged(local_root, conflict_rel, staged, None):
            return conflict_path
    raise VaultSyncTransportError("could not allocate a unique conflict filename")


def _pull_version(
    local_root: Path,
    transport: VaultSyncTransport,
    remote: RemoteFile,
    expected: tuple[int, int, int, int] | None,
) -> Path | None:
    """Return a conflict-copy path if a desktop edit raced the hub download."""
    relpath = remote.relpath
    staged = _stage_pull(local_root, transport, remote)
    try:
        if _publish_staged(local_root, relpath, staged, expected):
            return None
        return _publish_conflict(local_root, relpath, staged)
    finally:
        staged.unlink(missing_ok=True)


def _pull_conflict(local_root: Path, transport: VaultSyncTransport, remote: RemoteFile) -> Path:
    relpath = remote.relpath
    staged = _stage_pull(local_root, transport, remote)
    try:
        return _publish_conflict(local_root, relpath, staged)
    finally:
        staged.unlink(missing_ok=True)


class LocalDirTransport:
    """Test/dry-run transport: another local directory stands in for the hub.

    Every method mirrors ``SSHTransport``'s contract with plain filesystem
    operations, so ``sync_once`` runs identically against a tmp_path fixture
    and against the real Mac hub — the engine under test never has to know
    which one it is talking to.
    """

    def __init__(self, remote_root: Path) -> None:
        self.remote_root = Path(remote_root)

    def reachable(self) -> bool:
        return self.remote_root.is_dir()

    def list_files(self, trees: Sequence[str]) -> dict[str, RemoteFile]:
        out: dict[str, RemoteFile] = {}
        for tree in trees:
            base = self.remote_root / tree
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.remote_root).as_posix()
                st = path.stat()
                out[rel] = RemoteFile(rel, st.st_mtime, st.st_size)
        return out

    def list_all_files(self) -> dict[str, RemoteFile]:
        # "." normalizes to the vault root, so this reuses list_files' walk.
        return self.list_files(["."])

    def pull_file(self, relpath: str, dest: Path) -> None:
        src = self.remote_root / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def stat_file(self, relpath: str) -> RemoteFile:
        st = (self.remote_root / relpath).stat()
        return RemoteFile(relpath, st.st_mtime, st.st_size)

    def push_conflict_copy(self, relpath_hint: str, src: Path) -> None:
        inbox = self.remote_root / "00 - Inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        with _upload_snapshot(src, directory=inbox) as (staged, content_hash):
            dest = inbox / _pushed_conflict_name(relpath_hint, src, content_hash=content_hash)
            if dest.is_symlink():
                raise VaultSyncTransportError("unsafe hub conflict destination")
            if dest.exists():
                if _sha256_path(dest) != content_hash:
                    raise VaultSyncTransportError("existing hub conflict copy differs")
                return
            try:
                os.link(staged, dest)
            except FileExistsError:
                if _sha256_path(dest) != content_hash:
                    raise VaultSyncTransportError("existing hub conflict copy differs") from None

    def hash_files(self, relpaths: Sequence[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for rel in relpaths:
            path = self.remote_root / rel
            if path.is_file():
                out[rel] = _sha256_path(path)
        return out


# Remote-side file lister, run via `python3 -c` over SSH instead of shelling
# out to `find`. `find -printf` is GNU-only; the Mac hub ships BSD `find`
# (macOS coreutils), which rejects `-printf` outright and — before task #705
# review caught it — made every real run against the hub silently list zero
# files while reporting success. python3 is present on every host this syncs
# with, and `os.walk`/`os.stat` behave identically on GNU and BSD userlands,
# so this sidesteps the find/stat dialect split entirely rather than trying
# to detect and branch on it. NUL-delimited records (tab-separated fields)
# survive spaces (`00 - Inbox`), tabs, and newlines in filenames.
_REMOTE_LISTER_SCRIPT = r"""
import os, sys
def fail(exc):
    raise exc
for tree in sys.argv[1:]:
    try:
        os.stat(tree)
    except FileNotFoundError:
        continue
    for dirpath, _dirnames, filenames in os.walk(tree, onerror=fail):
        for name in filenames:
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            sys.stdout.buffer.write(
                ("%s\t%s\t%d\0" % (path, st.st_mtime, st.st_size)).encode(
                    "utf-8", "surrogateescape"
                )
            )
"""


# Remote-side hasher for the few files whose size matches but mtime differs
# (a copy or checkout that only bumped timestamps). Paths arrive NUL-separated
# on stdin, so any filename is safe; output is NUL-separated "path\tsha256".
_REMOTE_HASHER_SCRIPT = r"""
import hashlib, sys
for raw in sys.stdin.buffer.read().split(b"\0"):
    if not raw:
        continue
    try:
        h = hashlib.sha256()
        with open(raw, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        continue
    sys.stdout.buffer.write(raw + b"\t" + h.hexdigest().encode() + b"\0")
"""


_REMOTE_READER_SCRIPT = r"""
import base64, pathlib, shutil, sys
rel = pathlib.PurePosixPath(base64.urlsafe_b64decode(sys.argv[1]).decode('utf-8', 'surrogateescape'))
if rel.is_absolute() or '..' in rel.parts:
    raise ValueError('unsafe relative path')
with open(str(rel), 'rb') as source:
    shutil.copyfileobj(source, sys.stdout.buffer)
"""

_REMOTE_STAT_SCRIPT = r"""
import base64, os, pathlib, sys
rel = pathlib.PurePosixPath(base64.urlsafe_b64decode(sys.argv[1]).decode('utf-8', 'surrogateescape'))
if rel.is_absolute() or '..' in rel.parts:
    raise ValueError('unsafe relative path')
st = os.stat(str(rel))
sys.stdout.write('%s\t%d' % (st.st_mtime, st.st_size))
"""

_REMOTE_WRITER_SCRIPT = r"""
import base64, hashlib, os, pathlib, stat, sys, tempfile
name = base64.urlsafe_b64decode(sys.argv[1]).decode('utf-8', 'surrogateescape')
expected_hash = sys.argv[2]
if not name or '/' in name or name in ('.', '..'):
    raise ValueError('unsafe conflict name')
inbox = pathlib.Path('00 - Inbox')
inbox.mkdir(exist_ok=True)
dest = inbox / name
def existing_matches():
    try:
        fd = os.open(dest, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('unsafe conflict destination')
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(1 << 20), b''):
            digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        raise ValueError('existing conflict copy differs')
    return True
if len(sys.argv) > 3 and sys.argv[3] == 'probe':
    try:
        print('match' if existing_matches() else 'absent')
    except ValueError:
        print('mismatch')
    sys.exit(0)
if not existing_matches():
    fd, staged_name = tempfile.mkstemp(prefix='.vault-sync-', dir=inbox)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, 'wb') as target:
            for chunk in iter(lambda: sys.stdin.buffer.read(1 << 20), b''):
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != expected_hash:
            raise ValueError('incomplete conflict upload')
        try:
            os.link(staged_name, dest)
        except FileExistsError:
            if not existing_matches():
                raise ValueError('conflict destination changed during upload')
    finally:
        os.unlink(staged_name)
"""


class SSHTransport:
    """Production transport: SSH against the Mac hub vault.

    Same host/reachability contract as the #816 script (``ssh -o BatchMode=yes
    -o ConnectTimeout=... $REMOTE_HOST true``) so an unreachable hub is a clean
    no-op run, not an error. ``list_files`` runs the base64-embedded
    ``_REMOTE_LISTER_SCRIPT`` via ``python3 -c`` over one SSH round trip — see
    that script's comment for why this replaced a ``find -printf`` command
    that never worked against the real (BSD-``find``) Mac hub.

    Every call in a run rides one multiplexed connection (``ControlMaster``,
    task #1492). A pull costs five SSH round trips per file, and one fresh
    connection each made the hub refuse exec after a burst; ``close()`` ends
    the master when the run finishes, and ``ControlPersist`` bounds it if a run
    dies before that.
    """

    CONTROL_PERSIST_S = 60
    MAX_SOCKET_PATH = 104

    def __init__(
        self,
        host: str,
        remote_root: str,
        *,
        connect_timeout_s: float = 5.0,
        list_timeout_s: float = 30.0,
        transfer_timeout_s: float = 60.0,
        multiplex: bool | None = None,
    ) -> None:
        self.host = host
        self.remote_root = remote_root.rstrip("/")
        self.connect_timeout_s = connect_timeout_s
        self.list_timeout_s = list_timeout_s
        self.transfer_timeout_s = transfer_timeout_s
        if multiplex is None:
            multiplex = config.vault_sync_ssh_multiplex_enabled()
        self._control_path: str | None = None
        control_path = str(config.vault_sync_ssh_control_dir() / "vs-%C")
        # %C expands to 40 hex chars; past the 104-byte macOS sun_path limit
        # (bytes, not characters) ssh cannot bind the socket. That, or a
        # control dir we cannot create, falls back to plain connections
        # rather than failing the sync.
        if multiplex and len(os.fsencode(control_path)) - 2 + 40 < self.MAX_SOCKET_PATH:
            control_dir = Path(control_path).parent
            try:
                control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                control_dir.chmod(0o700)
            except OSError:
                return
            self._control_path = control_path

    def _ssh_opts(self) -> list[str]:
        opts = [
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, int(self.connect_timeout_s))}",
        ]
        if self._control_path:
            opts += [
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={self._control_path}",
                "-o",
                f"ControlPersist={self.CONTROL_PERSIST_S}",
            ]
        return opts

    def close(self) -> None:
        """End the shared master connection; best-effort, never raises."""
        if not self._control_path:
            return
        try:
            subprocess.run(
                ["ssh", *self._ssh_opts(), "-O", "exit", self.host],
                capture_output=True,
                timeout=self.connect_timeout_s + 2,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def reachable(self) -> bool:
        try:
            proc = subprocess.run(
                ["ssh", *self._ssh_opts(), self.host, "true"],
                capture_output=True,
                timeout=self.connect_timeout_s + 2,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def list_files(self, trees: Sequence[str]) -> dict[str, RemoteFile]:
        """Enumerate ``trees`` on the remote, or raise ``VaultSyncTransportError``.

        Deliberately never returns ``{}`` on failure — an empty mapping is
        also the legitimate result for a genuinely empty hub, so it cannot
        double as "the listing failed" (task #705 review: a swallowed
        failure here previously flagged every desktop file as local-only and
        still recorded the run as a success). ``sync_once``'s existing
        exception boundary around this call turns a raise into ``ok=False``
        with ``error`` set and the previous success timestamp preserved.
        """
        b64_script = base64.b64encode(_REMOTE_LISTER_SCRIPT.encode("utf-8")).decode("ascii")
        quoted_trees = " ".join(shlex.quote(t) for t in trees)
        python_cmd = (
            "python3 -c 'import base64;exec(base64.b64decode(\""
            + b64_script
            + "\"))' "
            + quoted_trees
        )
        remote_cmd = f"cd {shlex.quote(self.remote_root)} && {python_cmd}"
        try:
            proc = subprocess.run(
                ["ssh", *self._ssh_opts(), self.host, remote_cmd],
                capture_output=True,
                timeout=self.list_timeout_s,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VaultSyncTransportError(f"remote vault enumeration failed: {exc}") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            raise VaultSyncTransportError(
                f"remote vault enumeration exited {proc.returncode}: {detail[-300:]}"
            )
        out: dict[str, RemoteFile] = {}
        for entry in proc.stdout.split(b"\0"):
            if not entry.strip():
                continue
            try:
                text = entry.decode("utf-8", "surrogateescape")
                # rsplit: the path may itself contain tabs; mtime and size never do.
                relpath, mtime_s, size_s = text.rsplit("\t", 2)
                out[relpath] = RemoteFile(relpath, float(mtime_s), int(size_s))
            except (UnicodeDecodeError, ValueError) as exc:
                raise VaultSyncTransportError(f"malformed remote vault listing: {exc}") from exc
        return out

    def list_all_files(self) -> dict[str, RemoteFile]:
        """Every file under the remote root, keyed by vault-relative path.

        Reuses ``list_files`` by walking the vault root (``"."``); the remote
        sweep returns ``./``-prefixed paths, which are stripped here. Retained
        for explicit callers; the sync engine does not enumerate the whole
        vault or use this method to infer capture identity.
        """
        return {
            (rel[2:] if rel.startswith("./") else rel): RemoteFile(
                rel[2:] if rel.startswith("./") else rel, rfile.mtime, rfile.size
            )
            for rel, rfile in self.list_files(["."]).items()
        }

    def hash_files(self, relpaths: Sequence[str]) -> dict[str, str]:
        """sha256 of the given remote paths in one SSH round trip; raises on failure."""
        if not relpaths:
            return {}
        b64_script = base64.b64encode(_REMOTE_HASHER_SCRIPT.encode("utf-8")).decode("ascii")
        remote_cmd = (
            f"cd {shlex.quote(self.remote_root)} && "
            "python3 -c 'import base64;exec(base64.b64decode(\"" + b64_script + "\"))'"
        )
        try:
            proc = subprocess.run(
                ["ssh", *self._ssh_opts(), self.host, remote_cmd],
                input=b"\0".join(r.encode("utf-8", "surrogateescape") for r in relpaths),
                capture_output=True,
                timeout=self.list_timeout_s,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VaultSyncTransportError(f"remote vault hashing failed: {exc}") from exc
        if proc.returncode != 0:
            raise VaultSyncTransportError(f"remote vault hashing exited {proc.returncode}")
        out: dict[str, str] = {}
        for entry in proc.stdout.split(b"\0"):
            if not entry:
                continue
            path, _, digest = entry.rpartition(b"\t")
            out[path.decode("utf-8", "surrogateescape")] = digest.decode("ascii")
        return out

    def pull_file(self, relpath: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        encoded = base64.urlsafe_b64encode(relpath.encode("utf-8", "surrogateescape")).decode()
        command = (
            f"cd {shlex.quote(self.remote_root)} && "
            f"python3 -c {shlex.quote(_REMOTE_READER_SCRIPT)} {encoded}"
        )
        with dest.open("wb") as target:
            subprocess.run(
                ["ssh", *self._ssh_opts(), self.host, command],
                stdout=target,
                stderr=subprocess.PIPE,
                check=True,
                timeout=self.transfer_timeout_s,
            )

    def stat_file(self, relpath: str) -> RemoteFile:
        encoded = base64.urlsafe_b64encode(relpath.encode("utf-8", "surrogateescape")).decode()
        command = (
            f"cd {shlex.quote(self.remote_root)} && "
            f"python3 -c {shlex.quote(_REMOTE_STAT_SCRIPT)} {encoded}"
        )
        try:
            proc = subprocess.run(
                ["ssh", *self._ssh_opts(), self.host, command],
                capture_output=True,
                check=True,
                timeout=self.transfer_timeout_s,
            )
            mtime_s, size_s = proc.stdout.decode("ascii").split("\t")
            return RemoteFile(relpath, float(mtime_s), int(size_s))
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError) as exc:
            raise VaultSyncTransportError(f"remote vault stat failed: {exc}") from exc

    def push_conflict_copy(self, relpath_hint: str, src: Path) -> None:
        with _upload_snapshot(src) as (staged, expected_hash):
            name = _pushed_conflict_name(relpath_hint, src, content_hash=expected_hash)
            encoded = base64.urlsafe_b64encode(name.encode("utf-8", "surrogateescape")).decode()
            command = (
                f"cd {shlex.quote(self.remote_root)} && "
                f"python3 -c {shlex.quote(_REMOTE_WRITER_SCRIPT)} {encoded} {expected_hash}"
            )
            probe = subprocess.run(
                ["ssh", *self._ssh_opts(), self.host, command + " probe"],
                capture_output=True,
                check=True,
                timeout=self.transfer_timeout_s,
            )
            if probe.stdout.strip() == b"match":
                return
            if probe.stdout.strip() != b"absent":
                raise VaultSyncTransportError("hub conflict destination differs or probe failed")
            with staged.open("rb") as source:
                subprocess.run(
                    ["ssh", *self._ssh_opts(), self.host, command],
                    stdin=source,
                    capture_output=True,
                    check=True,
                    timeout=self.transfer_timeout_s,
                )


# --------------------------------------------------------------------------
# Sync engine
# --------------------------------------------------------------------------


@dataclass
class SyncConflict:
    relpath: str
    reason: str  # "local_newer" | "local_only" | "same_mtime_diff"
    conflict_file: str | None
    detected_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SyncResult:
    ok: bool
    reachable: bool
    dry_run: bool
    started_at: str
    finished_at: str
    pulled: list[str] = field(default_factory=list)
    conflicts: list[SyncConflict] = field(default_factory=list)
    filed_upstream: list[str] = field(default_factory=list)
    retired_skipped: list[str] = field(default_factory=list)
    unsafe_skipped: list[str] = field(default_factory=list)
    file_errors: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["conflicts"] = [c.to_dict() for c in self.conflicts]
        d["conflict_count"] = len(self.conflicts)
        d["filed_upstream_count"] = len(self.filed_upstream)
        d["pulled_count"] = len(self.pulled)
        d["retired_skipped_count"] = len(self.retired_skipped)
        d["unsafe_skipped_count"] = len(self.unsafe_skipped)
        d["file_errors_count"] = len(self.file_errors)
        return d


def _differs(remote: RemoteFile, local_path: Path) -> bool:
    if not local_path.exists():
        return True
    st = local_path.stat()
    if remote.size != st.st_size:
        return True
    return abs(remote.mtime - st.st_mtime) > MTIME_TOLERANCE_S


def _find_matching_conflict_copy(
    local_path: Path, remote: RemoteFile, remote_hash: str | None
) -> Path | None:
    """An existing ``*.conflict-*`` copy that already captures this exact remote version.

    Without this, a ``local_newer`` conflict that persists across many
    10-minute timer runs got a brand new timestamped copy every single run
    even though the remote version driving the conflict never changed —
    unbounded growth flagged in task #705 review. ``pull_file``
    (``shutil.copy2``/the SSH stream's explicit ``os.utime``) preserves source mtime.
    Compare the bytes too: a hub edit can retain both size and mtime.
    """
    if not remote_hash or not local_path.parent.is_dir():
        return None
    pattern = f"{glob.escape(local_path.stem)}{_CONFLICT_MARKER}*{glob.escape(local_path.suffix)}"
    for candidate in sorted(local_path.parent.glob(pattern)):
        try:
            st = candidate.stat()
        except OSError:
            continue
        if st.st_size == remote.size and abs(st.st_mtime - remote.mtime) <= MTIME_TOLERANCE_S:
            try:
                if _sha256_path(candidate) == remote_hash:
                    return candidate
            except OSError:
                continue
    return None


def sync_once(
    local_root: Path,
    transport: VaultSyncTransport,
    *,
    synced_trees: Sequence[str] = DEFAULT_SYNCED_TREES,
    retired_paths: Sequence[str] = DEFAULT_RETIRED_PATHS,
    ignored_globs: Sequence[str] = (),
    push_conflicts: bool = False,
    dry_run: bool = False,
) -> SyncResult:
    """Pull hub-authoritative trees into ``local_root``; never clobber a newer local edit.

    Direction is hub -> desktop only: nothing here ever writes to the hub
    except an opt-in (``push_conflicts=True``) best-effort copy of a
    desktop-side conflict into the hub's ``00 - Inbox`` for human triage. The
    hub's own copy under a synced tree is never modified.

    Never raises for an unreachable remote (a clean no-op, matching the #816
    script's behavior). Raises are caught at two boundaries and turned into
    ``SyncResult`` fields instead: enumerating the remote tree failing sets
    ``ok=False`` with ``error`` set (nothing in this run is trustworthy, so
    nothing is attempted); a single file's pull/push failing mid-loop is
    caught per-call, recorded in ``file_errors``, and the rest of the run
    continues — one bad transfer must not abort transfers that would have
    succeeded (task #705 review).
    """
    started_at = _now_iso()
    local_root = Path(local_root).resolve()

    for tree in synced_trees:
        if (
            not tree
            or tree.startswith("/")
            or "\\" in tree
            or any(part in ("", ".", "..") for part in tree.split("/"))
        ):
            return SyncResult(
                ok=False,
                reachable=False,
                dry_run=dry_run,
                started_at=started_at,
                finished_at=_now_iso(),
                error=f"invalid synced tree: {tree!r}",
            )

    if not transport.reachable():
        return SyncResult(
            ok=True,
            reachable=False,
            dry_run=dry_run,
            started_at=started_at,
            finished_at=_now_iso(),
        )

    try:
        remote_files = transport.list_files(synced_trees)
    except Exception as exc:  # noqa: BLE001 - surfaced via SyncResult.error
        return SyncResult(
            ok=False,
            reachable=True,
            dry_run=dry_run,
            started_at=started_at,
            finished_at=_now_iso(),
            error=str(exc)[:500],
        )

    pulled: list[str] = []
    conflicts: list[SyncConflict] = []
    retired_skipped: list[str] = []
    unsafe_skipped: list[str] = []
    file_errors: list[dict] = []

    # Metadata equality does not establish content equality: same-sized edits
    # can retain the same mtime. Hash every same-sized pair in one remote round
    # trip, then fail closed for a path whose hash could not be obtained.
    same_size: list[str] = []
    identical: set[str] = set()
    hash_unavailable: set[str] = set()
    remote_hashes: dict[str, str] = {}
    for rel, rfile in remote_files.items():
        if (
            _is_conflict_artifact(rel)
            or not _is_safe_relpath(local_root, rel)
            or _is_retired(rel, retired_paths)
            or _is_ignored(rel, ignored_globs)
        ):
            continue
        try:
            local_path = local_root / rel
            if local_path.is_file() and local_path.stat().st_size == rfile.size:
                same_size.append(rel)
        except OSError as exc:
            hash_unavailable.add(rel)
            file_errors.append({"relpath": rel, "action": "stat", "detail": str(exc)[:300]})
    if same_size:
        try:
            remote_hashes = transport.hash_files(same_size)
        except Exception as exc:  # noqa: BLE001 - never trust metadata on hash failure
            remote_hashes = {}
            file_errors.append({"relpath": "*", "action": "hash", "detail": str(exc)[:300]})
        for rel in same_size:
            if rel not in remote_hashes:
                hash_unavailable.add(rel)
                continue
            try:
                if remote_hashes[rel] == _sha256_path(local_root / rel):
                    identical.add(rel)
            except OSError as exc:
                hash_unavailable.add(rel)
                file_errors.append({"relpath": rel, "action": "hash", "detail": str(exc)[:300]})
    if hash_unavailable:
        conflicts.extend(
            SyncConflict(rel, "hash_unavailable", None, _now_iso())
            for rel in sorted(hash_unavailable)
        )
        file_errors.append(
            {
                "relpath": "*",
                "action": "hash",
                "detail": f"hash unavailable for {len(hash_unavailable)} file(s)",
            }
        )

    for relpath, rfile in remote_files.items():
        if _is_conflict_artifact(relpath):
            continue
        if not _is_safe_relpath(local_root, relpath):
            unsafe_skipped.append(relpath)
            continue
        if _is_ignored(relpath, ignored_globs):
            continue
        if _is_retired(relpath, retired_paths):
            retired_skipped.append(relpath)
            continue
        if relpath in hash_unavailable:
            continue

        local_path = local_root / relpath
        if not local_path.exists():
            if not dry_run:
                try:
                    conflict_path = _pull_version(local_root, transport, rfile, None)
                except Exception as exc:  # noqa: BLE001 - recorded, run continues
                    file_errors.append(
                        {"relpath": relpath, "action": "pull", "detail": str(exc)[:300]}
                    )
                    continue
                if conflict_path is not None:
                    conflicts.append(
                        SyncConflict(
                            relpath, "concurrent_local_edit", conflict_path.name, _now_iso()
                        )
                    )
                    continue
            pulled.append(relpath)
            continue

        try:
            if relpath in identical or (
                relpath not in same_size and not _differs(rfile, local_path)
            ):
                continue
            st = local_path.stat()
        except OSError as exc:
            file_errors.append({"relpath": relpath, "action": "stat", "detail": str(exc)[:300]})
            continue
        if st.st_mtime > rfile.mtime + MTIME_TOLERANCE_S:
            # Desktop edited this after the hub's last change: preserve the
            # incoming hub content as a conflict copy, leave the desktop
            # original untouched. Reuse an existing copy of this exact
            # remote version instead of writing a fresh one every run.
            remote_hash = remote_hashes.get(relpath)
            if remote_hash is None:
                try:
                    remote_hash = transport.hash_files([relpath]).get(relpath)
                except Exception as exc:  # noqa: BLE001 - a fresh copy is safer than stale reuse
                    file_errors.append(
                        {"relpath": relpath, "action": "hash", "detail": str(exc)[:300]}
                    )
            existing = _find_matching_conflict_copy(local_path, rfile, remote_hash)
            if existing is not None:
                if push_conflicts and not dry_run:
                    try:
                        transport.push_conflict_copy(relpath, existing)
                    except Exception as exc:  # noqa: BLE001 - retry on the next run
                        file_errors.append(
                            {
                                "relpath": relpath,
                                "action": "push_conflict",
                                "detail": str(exc)[:300],
                            }
                        )
                conflicts.append(SyncConflict(relpath, "local_newer", existing.name, _now_iso()))
                continue
            conflict_name = _conflict_filename(local_path) if dry_run else None
            if not dry_run:
                try:
                    conflict_path = _pull_conflict(local_root, transport, rfile)
                    conflict_name = conflict_path.name
                except Exception as exc:  # noqa: BLE001 - recorded, run continues
                    file_errors.append(
                        {"relpath": relpath, "action": "pull_conflict", "detail": str(exc)[:300]}
                    )
                    conflicts.append(SyncConflict(relpath, "local_newer", None, _now_iso()))
                    continue
                if push_conflicts:
                    try:
                        transport.push_conflict_copy(relpath, conflict_path)
                    except Exception as exc:  # noqa: BLE001 - best-effort, recorded only
                        file_errors.append(
                            {
                                "relpath": relpath,
                                "action": "push_conflict",
                                "detail": str(exc)[:300],
                            }
                        )
            conflicts.append(SyncConflict(relpath, "local_newer", conflict_name, _now_iso()))
        elif rfile.mtime > st.st_mtime + MTIME_TOLERANCE_S:
            # Hub is newer: it wins outright.
            if not dry_run:
                try:
                    conflict_path = _pull_version(
                        local_root,
                        transport,
                        rfile,
                        (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns),
                    )
                except Exception as exc:  # noqa: BLE001 - recorded, run continues
                    file_errors.append(
                        {"relpath": relpath, "action": "pull", "detail": str(exc)[:300]}
                    )
                    continue
                if conflict_path is not None:
                    conflicts.append(
                        SyncConflict(
                            relpath, "concurrent_local_edit", conflict_path.name, _now_iso()
                        )
                    )
                    continue
            pulled.append(relpath)
        else:
            # Mtimes are within tolerance of each other but content differs
            # (size mismatch survived the tolerance check above) — ambiguous,
            # so treat conservatively as a conflict with nothing overwritten.
            conflicts.append(SyncConflict(relpath, "same_mtime_diff", None, _now_iso()))

    # Desktop-only files under a synced tree the hub has never seen.
    local_only: list[tuple[str, Path]] = []
    for tree in synced_trees:
        base = local_root / tree
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(local_root).as_posix()
            if (
                rel in remote_files
                or _is_conflict_artifact(rel)
                or _is_retired(rel, retired_paths)
                or _is_ignored(rel, ignored_globs)
            ):
                continue
            local_only.append((rel, path))

    # Retain serialized compatibility with older states, but a basename or
    # matching content alone does not prove that a capture was filed/moved.
    # Do not enumerate the rest of the vault to infer identity; ambiguous
    # captures remain visible conflicts and retain their optional upload.
    filed_upstream: list[str] = []
    for rel, path in local_only:
        conflicts.append(SyncConflict(rel, "local_only", None, _now_iso()))
        if push_conflicts and not dry_run:
            try:
                transport.push_conflict_copy(rel, path)
            except Exception as exc:  # noqa: BLE001 - best-effort, recorded only
                file_errors.append(
                    {"relpath": rel, "action": "push_conflict", "detail": str(exc)[:300]}
                )

    return SyncResult(
        ok=not file_errors,
        reachable=True,
        dry_run=dry_run,
        started_at=started_at,
        finished_at=_now_iso(),
        pulled=pulled,
        conflicts=conflicts,
        filed_upstream=filed_upstream,
        retired_skipped=retired_skipped,
        unsafe_skipped=unsafe_skipped,
        file_errors=file_errors,
    )


# --------------------------------------------------------------------------
# State file (read by tools/doctor.py's vault_sync check)
# --------------------------------------------------------------------------


def read_state(path: Path | None = None) -> dict | None:
    """Last-recorded run state; None only when no state file exists."""
    state_path = path or config.vault_sync_state_path()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VaultSyncStateError(f"cannot read sync state {state_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VaultSyncStateError(f"sync state {state_path} must be a JSON object")
    for key in ("pulled", "retired_skipped", "unsafe_skipped", "filed_upstream"):
        values = payload.get(key, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise VaultSyncStateError(f"sync state {state_path} has invalid {key}")
    conflicts = payload.get("conflicts", [])
    if not isinstance(conflicts, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("relpath"), str)
        and isinstance(item.get("reason"), str)
        and (item.get("conflict_file") is None or isinstance(item.get("conflict_file"), str))
        for item in conflicts
    ):
        raise VaultSyncStateError(f"sync state {state_path} has invalid conflicts")
    for key, entries in (
        ("pulled_count", payload.get("pulled", [])),
        ("retired_skipped_count", payload.get("retired_skipped", [])),
        ("unsafe_skipped_count", payload.get("unsafe_skipped", [])),
        ("conflict_count", conflicts),
        ("filed_upstream_count", payload.get("filed_upstream", [])),
    ):
        value = payload.get(key, 0)
        if type(value) is not int or value != len(entries):
            raise VaultSyncStateError(f"sync state {state_path} has invalid {key}")
    return payload


def _write_state_locked(result: SyncResult, state_path: Path) -> dict:
    """Persist ``result``, merging with the previous state rather than replacing it.

    Two things must never regress just because this particular run didn't
    fully succeed (task #705 review):

    - An unreachable or errored run carries forward the *previous* run's
      ``conflicts``/``conflict_count``/``pulled``/``retired_skipped`` and
      ``last_success_utc`` — "couldn't check" must never read as "found
      nothing wrong" (an empty result from a failed enumeration is
      indistinguishable from a genuinely clean vault otherwise), and it must
      never make already-known conflicts vanish from the report doctor reads.
    - A dry run never advances ``last_success_utc`` — it inventories and
      classifies but performs no writes, so it is a preview, not a completed
      sync. Its conflict/pulled counts are still recorded (informational: a
      preview of what a real run would do), just not treated as evidence of
      a converged vault.

    ``last_run_utc``/``last_run_ok``/``last_run_reachable`` always reflect
    *this* attempt, successful or not, so "the timer is still firing" stays
    observable even while the tracked sync state itself is stale.
    """
    previous = read_state(state_path) or {}
    # Did this run actually see the hub's listing? Only then are its
    # conflict/pulled findings a fresh picture. A per-file transfer failure
    # (file_errors) still enumerated correctly, so its findings stand — it
    # just isn't a successful sync.
    enumerated = result.reachable and result.error is None

    payload = result.to_dict()
    payload["last_run_utc"] = result.finished_at
    payload["last_run_ok"] = result.ok
    payload["last_run_reachable"] = result.reachable

    if enumerated:
        # A hash failure cannot decide whether an old divergence resolved.
        # Retain that path's prior triage reason and conflict copy, while a
        # newly unhashable path remains marked hash_unavailable.
        prior_conflicts = {
            item.get("relpath"): item
            for item in previous.get("conflicts", [])
            if isinstance(item, dict)
        }
        payload["conflicts"] = [
            prior_conflicts.get(item["relpath"], item)
            if item.get("reason") == "hash_unavailable"
            else item
            for item in payload["conflicts"]
        ]
        payload["last_success_utc"] = (
            result.finished_at
            if result.ok and not result.dry_run
            else previous.get("last_success_utc")
        )
    else:
        payload["last_success_utc"] = previous.get("last_success_utc")
        payload["pulled"] = previous.get("pulled", [])
        payload["pulled_count"] = previous.get("pulled_count", 0)
        payload["conflicts"] = previous.get("conflicts", [])
        payload["conflict_count"] = previous.get("conflict_count", 0)
        payload["retired_skipped"] = previous.get("retired_skipped", [])
        payload["retired_skipped_count"] = previous.get("retired_skipped_count", 0)
        payload["filed_upstream"] = previous.get("filed_upstream", [])
        payload["filed_upstream_count"] = previous.get("filed_upstream_count", 0)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=state_path.parent,
        prefix=f"{state_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as staged:
        tmp_path = Path(staged.name)
        json.dump(payload, staged, indent=2, sort_keys=True)
    try:
        tmp_path.replace(state_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return payload


def write_state(result: SyncResult, *, path: Path | None = None) -> dict:
    """Atomically merge and publish state across concurrent timer/manual runs."""
    state_path = path or config.vault_sync_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(state_path.suffix + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _write_state_locked(result, state_path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# CLI entry point (invoked by ua-vault-sync.service)
# --------------------------------------------------------------------------


def run(*, dry_run: bool = False) -> dict:
    remote_host = config.vault_sync_remote_host()
    remote_root = config.vault_sync_remote_root()
    if not remote_host or not remote_root:
        return {
            "ok": False,
            "status": "unconfigured",
            "error": "set UA_VAULT_SYNC_REMOTE_HOST and UA_VAULT_SYNC_REMOTE_ROOT",
        }
    try:
        local_root = config.vault_sync_local_root()
    except ValueError:
        return {
            "ok": False,
            "status": "unconfigured",
            "error": "set UA_VAULT_PATH or UA_VAULT_SYNC_LOCAL_ROOT",
        }
    state_path = config.vault_sync_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # The timer and a manual CLI run must not race on the same destination.
    # Lock the entire listing/pull/state cycle, not just the final state write.
    with state_path.with_suffix(state_path.suffix + ".run.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            read_state(state_path)  # Fail before changing vault files if existing state is corrupt.
            transport = SSHTransport(remote_host, remote_root)
            try:
                result = sync_once(
                    local_root,
                    transport,
                    synced_trees=config.vault_sync_synced_trees(),
                    retired_paths=config.vault_sync_retired_paths(),
                    ignored_globs=config.vault_sync_ignored_globs(),
                    push_conflicts=config.vault_sync_push_conflicts_enabled(),
                    dry_run=dry_run,
                )
            finally:
                close = getattr(transport, "close", None)
                if close is not None:
                    close()
            return write_state(result, path=state_path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


TARGET_SETTINGS = ("vault_sync.remote_host", "vault_sync.remote_root", "paths.vault")


def missing_target_settings(service_env_path: Path) -> list[str]:
    """Settings keys ``ua-vault-sync.service`` still lacks (task #1813).

    Resolves the target the way the unit's process will: its ``service.env``
    first (``EnvironmentFile=``), then ``settings.toml`` at the path that
    environment selects, never overriding a key already present. A blank
    ``service.env`` assignment therefore masks a settings value here exactly
    as it does in the service. ``paths.vault`` is reported only when neither
    ``UA_VAULT_SYNC_LOCAL_ROOT`` nor ``UA_VAULT_PATH`` resolves.
    """
    from . import service_env, settings

    env = service_env.parse_env_file(service_env_path)
    settings.apply_to_environ(env, quiet=True)
    missing: list[str] = []
    if not config.vault_sync_remote_host(env):
        missing.append("vault_sync.remote_host")
    if not config.vault_sync_remote_root(env):
        missing.append("vault_sync.remote_root")
    try:
        config.vault_sync_local_root(env)
    except ValueError:
        missing.append("paths.vault")
    return missing


def target_settings_file(service_env_path: Path) -> Path | None:
    """The settings file ``ua-vault-sync.service`` reads, or None when disabled.

    ``service.env`` can choose it (``UA_SETTINGS_FILE`` or ``UA_HOME``), so a
    writer fixing the sync target must write here, not to its own default.
    """
    from . import service_env, settings

    return settings.settings_path(service_env.parse_env_file(service_env_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull hub-authoritative vault trees (Wiki/Logs/00 - Inbox) onto this host."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without pulling or writing conflict copies.",
    )
    parser.add_argument(
        "--missing-target",
        metavar="SERVICE_ENV",
        type=Path,
        help="Print the settings keys the sync service still lacks (space-separated) and exit.",
    )
    parser.add_argument(
        "--target-settings-file",
        metavar="SERVICE_ENV",
        type=Path,
        help="Print the settings file the sync service reads (empty when disabled) and exit.",
    )
    args = parser.parse_args(argv)
    if args.target_settings_file is not None:
        target = target_settings_file(args.target_settings_file)
        print(target if target is not None else "")
        return 0
    if args.missing_target is not None:
        print(" ".join(missing_target_settings(args.missing_target)))
        return 0
    dry_run = args.dry_run or config.vault_sync_dry_run_default()
    payload = run(dry_run=dry_run)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
