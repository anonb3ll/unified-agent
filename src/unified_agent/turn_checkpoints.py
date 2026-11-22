"""Per-turn workspace checkpoints for node-launched sessions (task #1743).

At the start and end of every agent turn the node snapshots the session's git
working tree into a hidden ref, ``refs/ua/checkpoints/<session>/<turn>/<phase>``,
so the operator can see exactly what a turn changed without the agent having
committed anything.

A snapshot never touches the user's branch, index or working tree: it copies
the real index into a private temporary index (``GIT_INDEX_FILE``), stages the
working tree there, writes a dangling commit with ``commit-tree`` and points
only the hidden ref at it. Files ignored by ``.gitignore`` are not captured,
nor are untracked files larger than ``MAX_UNTRACKED_BYTES``. A workspace that
is not a git repository simply has no checkpoints.

Git is run so that nothing the repository configures executes on the node:
hook scripts, config-defined hooks and fsmonitor are disabled, every configured
clean/smudge/process filter driver is overridden to nothing, and diffs use
neither textconv nor an external diff tool. Inherited ``GIT_*`` variables are dropped, so the snapshot always
targets the session's own repository.

The change summary sent to the hub is bounded and redacted. The content diff
is read through a pipe that stops git at ``MAX_DIFF_READ_BYTES``; a bigger
diff is dropped (``diff_too_large``), so node memory and disk stay bounded
whatever a turn wrote (task #1992). Credential shapes
are redacted over the whole diff before it is cut; files whose names look like
secrets (``.env``, keys, certificates, credentials) and any file whose old or
new content holds a private-key ``BEGIN`` marker are listed but their content
is withheld (the blobs are checked, not just the hunk, so editing one line in
the middle of a key does not leak its body); and the whole summary is fitted
to ``MAX_SUMMARY_BYTES`` of JSON so it always fits the node event cap.

Note: the hidden refs keep snapshot objects reachable. ``git push --mirror``
or a ``refs/*`` refspec would publish them; ordinary branch pushes do not.

Ref lifetime: refs of the newest ``KEEP_SESSIONS`` sessions per repository are
kept, and the current session's refs are never pruned. A session's age is its
latest checkpoint's sequence number, recorded as ``seq=`` in the commit
message, because committer dates tie within a second (task #1992); older sessions' refs
are deleted (in one batch) when a session ends. Revert is intentionally not
offered: no backend here has verified conversation rollback.

Set ``UA_TURN_CHECKPOINTS=0`` to turn checkpoints off on a node.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .telemetry.redaction import redact_sensitive_content

REF_ROOT = "refs/ua/checkpoints"
MAX_FILES = 100
MAX_PATH_CHARS = 300
MAX_DIFF_BYTES = 16_384
#: Largest raw diff read back from disk for redaction; a bigger one is dropped.
MAX_DIFF_READ_BYTES = 2 * 1024 * 1024
#: JSON budget for one summary; the node event cap is 32 KiB.
MAX_SUMMARY_BYTES = 28_000
MAX_UNTRACKED_BYTES = 5 * 1024 * 1024
KEEP_SESSIONS = 20
#: Wall-clock budget for one snapshot (all git steps together).
CAPTURE_BUDGET_S = 20.0

_REF_PART_RE = re.compile(r"[^A-Za-z0-9_-]+")
_BIDI_RE = re.compile("[‪-‮⁦-⁩‎‏]")
#: Names whose content is never sent, only listed.
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials*",
    "*credentials.json",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*.secret",
    "*.secrets",
)
# A fixed identity so a checkpoint commit never borrows the operator's.
_IDENTITY = {
    "GIT_AUTHOR_NAME": "ua-checkpoint",
    "GIT_AUTHOR_EMAIL": "checkpoint@ua.invalid",
    "GIT_COMMITTER_NAME": "ua-checkpoint",
    "GIT_COMMITTER_EMAIL": "checkpoint@ua.invalid",
}
# Never run anything the repository (or its config includes) configures.
_SAFE_CONFIG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.pager=cat",
    "-c",
    "gc.auto=0",
    "-c",
    "diff.external=",
    # Config-defined hooks (``hook.<name>.event``) ignore core.hooksPath; switch
    # off every event our git calls can fire. Named hooks are also disabled
    # one by one in ``_overrides``.
    "-c",
    "hook.reference-transaction.enabled=false",
    "-c",
    "hook.post-index-change.enabled=false",
)


_SEQ_LOCK = threading.Lock()
_SEQ_LAST = 0
_SEQ_RE = re.compile(r"\bseq=(\d+)\s*$")


def _next_seq() -> int:
    """A checkpoint sequence number (task #1992).

    The wall clock in nanoseconds, kept strictly increasing within this
    process, so sessions that checkpoint in the same second still order by
    creation.
    """
    global _SEQ_LAST
    with _SEQ_LOCK:
        _SEQ_LAST = max(time.time_ns(), _SEQ_LAST + 1)
        return _SEQ_LAST


def enabled() -> bool:
    return os.environ.get("UA_TURN_CHECKPOINTS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _ref_part(value: str) -> str:
    """A safe, collision-free single ref component."""
    raw = str(value)
    part = _REF_PART_RE.sub("-", raw).strip("-")[:64]
    if part != raw or not part:
        digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:10]
        part = f"{part or 'id'}-{digest}"
    return part


def is_secret_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_PATTERNS)


def display_path(path: str) -> str:
    """A path safe to show: credential shapes redacted, bidi controls that could
    spoof it removed, then bounded (a file name can carry a token too)."""
    text = path.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    text = redact_sensitive_content(text, max_len=len(text) + 1)
    return _BIDI_RE.sub("", text)[:MAX_PATH_CHARS]


def _base_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(_IDENTITY)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


class CheckpointError(RuntimeError):
    """A git step failed; the caller records it and carries on."""


class CheckpointTimeout(CheckpointError):
    """A snapshot ran out of its budget; checkpoints stop for the session."""


class TurnCheckpoints:
    """Hidden-ref snapshots of one session's repository."""

    def __init__(self, root: Path, session: str) -> None:
        self.root = root
        self.session = _ref_part(session)
        self.disabled_reason: str | None = None
        self._deadline = float("inf")
        self._override_env: dict[str, str] | None = None

    @classmethod
    def open(cls, cwd: str | os.PathLike[str], session: str) -> TurnCheckpoints | None:
        """The checkpoint store for ``cwd``'s repository, or None outside git."""
        if not enabled():
            return None
        try:
            out = subprocess.run(
                ["git", *_SAFE_CONFIG, "-C", str(cwd), "rev-parse", "--show-toplevel"],
                capture_output=True,
                timeout=CAPTURE_BUDGET_S,
                env=_base_env(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        top = out.stdout.decode("utf-8", "replace").strip()
        if out.returncode != 0 or not top:
            return None
        return cls(Path(top), session)

    # --- git plumbing ------------------------------------------------------------

    def _overrides(self) -> dict[str, str]:
        """Config that turns every filter driver and named hook into nothing.

        Passed through ``GIT_CONFIG_COUNT`` rather than ``-c``: a subsection
        name may contain ``=``, which ``-c`` would split on.
        """
        if self._override_env is None:
            listed = self._git_raw(
                "config",
                "--name-only",
                "-z",
                "--get-regexp",
                r"^(filter|hook)\.",
                ok=(0, 1),
                overrides=False,
            )
            names = {
                name.decode("utf-8", "surrogateescape").rsplit(".", 1)[0]
                for name in listed.split(b"\0")
                if name.count(b".") >= 2
            }
            pairs: list[tuple[str, str]] = []
            for name in sorted(names):
                if name.startswith("hook."):
                    pairs.append((f"{name}.enabled", "false"))
                    continue
                for key, value in (
                    ("clean", ""),
                    ("smudge", ""),
                    ("process", ""),
                    ("required", "false"),
                ):
                    pairs.append((f"{name}.{key}", value))
            config = {"GIT_CONFIG_COUNT": str(len(pairs))}
            for i, (key, value) in enumerate(pairs):
                config[f"GIT_CONFIG_KEY_{i}"] = key
                config[f"GIT_CONFIG_VALUE_{i}"] = value
            self._override_env = config
        return self._override_env

    def _git_raw(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        ok: tuple[int, ...] = (0,),
        overrides: bool = True,
    ) -> bytes:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise CheckpointTimeout("snapshot budget exhausted")
        extra = self._overrides() if overrides else {}
        try:
            out = subprocess.run(
                ["git", *_SAFE_CONFIG, "-C", str(self.root), *args],
                capture_output=True,
                input=stdin,
                timeout=min(remaining, CAPTURE_BUDGET_S),
                env={**_base_env(), **extra, **(env or {})},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CheckpointTimeout(f"git {args[0]} timed out") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise CheckpointError(f"git {args[0]}: {type(exc).__name__}") from exc
        if out.returncode not in ok:
            raise CheckpointError(f"git {args[0]} exited {out.returncode}")
        return out.stdout

    def _git(self, *args: str, **kwargs: Any) -> str:
        return self._git_raw(*args, **kwargs).decode("utf-8", "replace")

    def _budget(self) -> None:
        self._deadline = time.monotonic() + CAPTURE_BUDGET_S

    def _guard(self, step):
        """Run one budgeted step; a timeout turns checkpoints off for the session."""
        if self.disabled_reason:
            raise CheckpointError(self.disabled_reason)
        self._budget()
        try:
            return step()
        except CheckpointTimeout as exc:
            self.disabled_reason = f"checkpoints off for this session: {exc}"
            raise CheckpointError(self.disabled_reason) from exc
        finally:
            self._deadline = float("inf")

    # --- snapshot --------------------------------------------------------------------

    def ref(self, turn_id: str, phase: str) -> str:
        return f"{REF_ROOT}/{self.session}/{_ref_part(turn_id)}/{_ref_part(phase)}"

    def capture(self, turn_id: str, phase: str) -> str:
        """Snapshot the working tree; return the checkpoint commit id."""
        return self._guard(lambda: self._capture(turn_id, phase))

    def _capture(self, turn_id: str, phase: str) -> str:
        with tempfile.TemporaryDirectory(prefix="ua-checkpoint-") as tmp:
            index = Path(tmp) / "index"
            env = {"GIT_INDEX_FILE": str(index)}
            head = self._head()
            real = Path(self._git("rev-parse", "--git-path", "index").strip())
            real = real if real.is_absolute() else self.root / real
            try:
                # Seeding from the real index keeps stat data (no full re-hash)
                # and sparse-checkout bits; the real index is only read.
                shutil.copyfile(real, index)
            except OSError:
                if head:
                    self._git("read-tree", head, env=env)
            # Tracked files: modifications and deletions.
            self._git("add", "--update", env=env)
            # Untracked, not ignored, and not too large.
            listed = self._git_raw("ls-files", "-z", "--others", "--exclude-standard", env=env)
            small = [
                name
                for name in listed.split(b"\0")
                if name and self._small(name.decode("utf-8", "surrogateescape"))
            ]
            if small:
                self._git_raw(
                    "add",
                    "--pathspec-from-file=-",
                    "--pathspec-file-nul",
                    "--",
                    # A file name is a name, never pathspec magic.
                    env={**env, "GIT_LITERAL_PATHSPECS": "1"},
                    stdin=b"\0".join(small),
                )
            tree = self._git("write-tree", env=env).strip()
        parents = ["-p", head] if head else []
        commit = self._git(
            "commit-tree",
            tree,
            *parents,
            "-m",
            f"ua checkpoint {self.session} {_ref_part(turn_id)} {_ref_part(phase)}"
            f" seq={_next_seq()}",
        ).strip()
        self._git("update-ref", self.ref(turn_id, phase), commit)
        return commit

    def _small(self, name: str) -> bool:
        try:
            return (self.root / name).stat().st_size <= MAX_UNTRACKED_BYTES
        except OSError:
            return False

    def _head(self) -> str | None:
        try:
            out = self._git("rev-parse", "--verify", "--quiet", "HEAD^{commit}", ok=(0, 1))
            return out.strip() or None
        except CheckpointError:
            return None  # an unborn branch has no HEAD yet

    # --- summary -----------------------------------------------------------------------

    def changes(self, start: str, end: str) -> dict[str, Any]:
        """Bounded, redacted summary of what changed between two checkpoints."""
        return self._guard(lambda: self._changes(start, end))

    def _changes(self, start: str, end: str) -> dict[str, Any]:
        fields = self._git_raw("diff", "--name-status", "--no-renames", "-z", start, end).split(
            b"\0"
        )
        changed: list[tuple[str, str]] = []
        for code, path in zip(fields[0::2], fields[1::2], strict=False):
            if code:
                # surrogateescape keeps a non-UTF-8 name byte-exact, so the
                # exclude pathspec below still matches it (os.fsencode round-trips).
                changed.append(
                    (code.decode("ascii", "replace")[:1], path.decode("utf-8", "surrogateescape"))
                )
        withheld = sorted({path for _, path in changed if is_secret_path(path)})
        # A hunk inside a key need not carry its BEGIN line, so look for the
        # marker in the whole old and new blobs (task #1990).
        keyed = self._private_key_paths(
            start, end, sorted({path for _, path in changed if path not in withheld})
        )
        excludes = [f":(exclude,literal){path}" for path in [*withheld, *keyed]]
        diff, too_large = self._bounded_diff(start, end, excludes)
        diff = _withhold_sections(diff) + "".join(
            f"diff --git a/{shown} b/{shown}\n[content withheld: private key]\n"
            for shown in map(display_path, keyed)
        )
        # Redact the whole text first, then cut: a cut must never leave part of
        # a credential behind too short to match its pattern (task #1893).
        diff = redact_sensitive_content(diff, max_len=len(diff) + 1)
        files = [
            {
                "status": code,
                "path": display_path(path),
                **({"withheld": True} if path in withheld else {}),
            }
            for code, path in changed[:MAX_FILES]
        ]
        summary: dict[str, Any] = {
            "files": files,
            "files_total": len(changed),
            "files_truncated": len(changed) > len(files),
            "withheld": len(withheld),
            "diff": diff,
            "diff_truncated": too_large,
            "diff_too_large": too_large,
        }
        return _fit(summary)

    def _bounded_diff(self, start: str, end: str, excludes: list[str]) -> tuple[str, bool]:
        """The content diff, read through a bounded pipe (task #1992).

        Returns ``("", True)`` when the raw diff is over ``MAX_DIFF_READ_BYTES``:
        git is stopped at the ceiling, so neither node memory nor a temporary
        file (``/tmp`` may be RAM) holds more than the ceiling, whatever a turn
        wrote.
        """
        raw = self._git_bounded(
            MAX_DIFF_READ_BYTES,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            start,
            end,
            "--",
            ".",
            *excludes,
        )
        if raw is None:
            return "", True
        return raw.decode("utf-8", "replace"), False

    def _git_bounded(self, limit: int, *args: str) -> bytes | None:
        """Run git; its stdout, or None once it passes ``limit`` bytes (git is
        killed there). Same environment, overrides and deadline as ``_git_raw``."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise CheckpointTimeout("snapshot budget exhausted")
        try:
            proc = subprocess.Popen(
                ["git", *_SAFE_CONFIG, "-C", str(self.root), *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**_base_env(), **self._overrides()},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CheckpointError(f"git {args[0]}: {type(exc).__name__}") from exc
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(min(remaining, CAPTURE_BUDGET_S), expire)
        timer.daemon = True
        timer.start()
        chunks: list[bytes] = []
        size = 0
        over = False
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(min(65_536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    over = True
                    proc.kill()
                    break
        finally:
            timer.cancel()
            if proc.stdout is not None:
                proc.stdout.close()
            proc.wait()
        if timed_out.is_set():
            raise CheckpointTimeout(f"git {args[0]} timed out")
        if over:
            return None
        if proc.returncode != 0:
            raise CheckpointError(f"git {args[0]} exited {proc.returncode}")
        return b"".join(chunks)

    def _private_key_paths(self, start: str, end: str, paths: list[str]) -> list[str]:
        """The paths whose blob at ``start`` or ``end`` holds a private-key marker."""
        found: set[str] = set()
        for i in range(0, len(paths), _GREP_CHUNK):
            out = self._git_raw(
                "grep",
                "-l",
                "-z",
                "-E",
                "-e",
                _PRIVATE_KEY_GREP,
                start,
                end,
                "--",
                *paths[i : i + _GREP_CHUNK],
                env={"GIT_LITERAL_PATHSPECS": "1"},
                ok=(0, 1),  # 1: no match
            )
            for item in out.split(b"\0"):
                name = item.decode("utf-8", "surrogateescape")
                for rev in (start, end):
                    if name.startswith(f"{rev}:"):
                        found.add(name[len(rev) + 1 :])
                        break
        return sorted(found)

    # --- housekeeping ------------------------------------------------------------------

    def prune(self, keep: int = KEEP_SESSIONS) -> int:
        """Delete refs of all but the newest ``keep`` sessions; never this session's."""
        return self._guard(lambda: self._prune(keep))

    def _prune(self, keep: int) -> int:
        # %00 separates the fields; a subject holds anything but a newline.
        listing = self._git(
            "for-each-ref",
            "--format=%(refname)%00%(committerdate:raw)%00%(contents:subject)",
            REF_ROOT,
        )
        newest: dict[str, int] = {}
        by_session: dict[str, list[str]] = {}
        for line in listing.splitlines():
            refname, _, fields = line.partition("\0")
            parts = refname.split("/")
            if len(parts) < 4 or "/".join(parts[:3]) != REF_ROOT:
                continue
            session = parts[3]
            by_session.setdefault(session, []).append(refname)
            newest[session] = max(newest.get(session, 0), _checkpoint_seq(fields))
        # Newest first by sequence; the session name breaks an exact tie stably.
        order = sorted(by_session, key=lambda session: (-newest[session], session))
        doomed = [
            refname
            for session in order[max(keep, 0) :]
            if session != self.session
            for refname in by_session[session]
        ]
        if doomed:
            # One transaction: one packed-refs rewrite, not one per ref.
            script = "".join(f"delete {refname}\n" for refname in doomed).encode()
            self._git_raw("update-ref", "--stdin", stdin=script)
        return len(doomed)


def _checkpoint_seq(fields: str) -> int:
    """A checkpoint's sequence: ``seq=`` from its subject or, for a checkpoint
    written before task #1992, its committer time in nanoseconds."""
    date, _, subject = fields.partition("\0")
    match = _SEQ_RE.search(subject)
    if match:
        return int(match.group(1))
    try:
        return int(date.split()[0]) * 1_000_000_000
    except (IndexError, ValueError):
        return 0


_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")
#: The same marker as a POSIX ERE, for ``git grep -E`` over whole blobs.
_PRIVATE_KEY_GREP = "-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"
#: Pathspecs per ``git grep`` call, so the argument list stays well bounded.
_GREP_CHUNK = 128


def _section_is_secret(header: str) -> bool:
    """Whether a ``diff --git`` header names a secret file.

    The text after the last `` b/`` is a suffix of the real path that ends in
    its whole file name, which is all ``is_secret_path`` looks at.
    """
    tail = header.rsplit(' "b/', 1) if ' "b/' in header else header.rsplit(" b/", 1)
    return len(tail) == 2 and is_secret_path(tail[1].rstrip('"'))


def _withhold_sections(diff: str) -> str:
    """Replace every per-file section of a secret file or carrying a private key.

    Secret files, and files whose blobs hold a private key, are already
    excluded from the diff by pathspec; this is the second line of defence
    should a name ever slip past that.
    """
    sections = re.split(r"(?m)^(?=diff --git )", diff)
    kept = []
    for section in sections:
        header = section.split("\n", 1)[0]
        if header.startswith("diff --git ") and _section_is_secret(header):
            kept.append(f"{header}\n[content withheld: secret file]\n")
        elif _PRIVATE_KEY_RE.search(section):
            kept.append(f"{header}\n[content withheld: private key]\n")
        else:
            kept.append(section)
    return "".join(kept)


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


def _fit(summary: dict[str, Any]) -> dict[str, Any]:
    """Shrink the diff, then the file list, until the JSON fits the budget."""
    while _encoded_size(summary) > MAX_SUMMARY_BYTES and summary["diff"]:
        encoded = summary["diff"].encode("utf-8")
        keep = min(len(encoded) // 2, MAX_DIFF_BYTES)
        summary["diff"] = encoded[:keep].decode("utf-8", "ignore") if keep > 64 else ""
        summary["diff_truncated"] = True
    while _encoded_size(summary) > MAX_SUMMARY_BYTES and summary["files"]:
        summary["files"] = summary["files"][: len(summary["files"]) // 2]
        summary["files_truncated"] = True
    if len(summary["diff"].encode("utf-8")) > MAX_DIFF_BYTES:
        summary["diff"] = summary["diff"].encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", "ignore")
        summary["diff_truncated"] = True
    return summary
