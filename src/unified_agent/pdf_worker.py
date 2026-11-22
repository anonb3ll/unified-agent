"""Bounded, confined PDF text extraction (hub task #1523).

``tools/web.py`` previously called ``pypdf.PdfReader`` directly, in-process,
on bytes downloaded from an arbitrary URL. That gave an untrusted PDF the
same process, memory, filesystem, and network access as the hub itself. This
module moves extraction into a short-lived subprocess worker and hands it
the PDF bytes over **stdin only** — the worker applies its resource limits
(and, on macOS, is already wrapped in a ``sandbox-exec`` seatbelt profile at
exec time) before it ever reads those bytes.

Background: Cursor PR #864 first built this worker but bundled it with
unrelated SSRF work and was vetoed for macOS bugs: ``os.setresuid``/
``os.setresgid`` do not exist on macOS, a missing ``sandbox-exec`` binary
aborted before the documented base64 fallback, the seatbelt profile granted
broad ``/opt/homebrew``/``/Library`` reads, and the untrusted bytes were
written to a temp file and read before confinement was applied. This module
fixes all four: no privilege-drop syscalls are used (the worker never needs
them — see below), a missing ``sandbox-exec`` fails closed, the seatbelt
profile is built from paths computed at runtime, and the PDF bytes travel
over a pipe, read only after resource limits (and, on macOS, the seatbelt)
are already in effect.

Platform support
-----------------
* **macOS**: the worker runs under ``/usr/bin/sandbox-exec`` with a minimal
  generated SBPL profile: ``deny default``, ``deny network*``,
  ``deny file-write*``, and ``allow file-read*`` only for the paths the
  interpreter and pypdf actually need — computed at runtime from
  ``sys.prefix``, ``sys.base_prefix``, and pypdf's located (never imported) package dir (see
  ``_read_roots``). There is no fallback that widens this to
  ``/opt/homebrew`` or ``/Library``. If ``sandbox-exec`` is missing, the
  worker is never started and extraction fails closed.
* **Linux**: the worker runs under ``/usr/bin/bwrap`` (bubblewrap,
  unprivileged, no root needed) with ``--unshare-all`` — including network
  (no interfaces exist inside the sandbox) and pid (the worker becomes PID 1
  of its own pid namespace, so killing it tears down anything it forked) —
  plus ``--die-with-parent``, a ``--clearenv``'d environment, and
  ``--ro-bind`` for only the same computed roots used on macOS, the host's
  core shared-library directories (see ``_bwrap_wrap``), a private
  ``/proc``, ``/dev`` and ``/tmp``, and no bind of ``$HOME``. Resource
  limits (``RLIMIT_AS``/``RLIMIT_DATA``, ``PR_SET_NO_NEW_PRIVS``) still
  apply inside the sandbox as defense in depth. If ``bwrap`` is missing, or
  a preflight run of the exact same sandbox setup fails (see
  ``_bwrap_preflight``), extraction fails closed — there is no unconfined
  fallback path on Linux.
* **Anything else** (an unsupported platform, macOS without
  ``sandbox-exec``, or Linux without a working ``bwrap``): fails closed. No
  extraction is attempted; the caller falls back to the existing
  non-extracted (base64) response.

``RLIMIT_AS`` on macOS
----------------------
Verified live on the Mac hub (task #1523): ``resource.getrlimit`` reports an
unlimited (``RLIM_INFINITY``) hard ceiling for both ``RLIMIT_AS`` and
``RLIMIT_DATA`` on Darwin, yet ``setrlimit`` still refuses to install any
lower soft limit for either one ("current limit exceeds maximum limit") —
XNU does not actually support a settable address-space/data-segment cap
here, regardless of what it reports. The worker still tries both (Darwin
tries ``RLIMIT_DATA`` then ``RLIMIT_AS``; Linux, where both are real and
enforced, tries the reverse order) in case a future macOS or a different
BSD accepts one, but a failure to set either is only treated as fatal
("confinement") on non-Darwin platforms. On macOS the memory ceiling is
accordingly best-effort only; the seatbelt profile (network/write denied,
a narrow read allowlist) plus the parent's wall-clock timeout are the real
confinement there, not an address-space limit.

PR #876 review fixes
---------------------
Four issues Devin found in the first version of this module, all fixed here:

1. The Linux worker ran with no filesystem/network confinement at all
   (``RLIMIT_AS``/``PR_SET_NO_NEW_PRIVS`` bound neither). Linux now runs
   under ``bwrap`` (see Platform support above); a missing or non-working
   ``bwrap`` fails closed instead of falling back to an unconfined worker.
2. A forked descendant that outlived the direct worker process could
   survive a timeout kill: ``os.getpgid(proc.pid)`` raises
   ``ProcessLookupError`` once the direct child has already exited, even
   though its process group (and any descendant still in it) is alive.
   ``_kill_process_group`` now signals the process-group id captured at
   spawn time directly, without re-resolving it.
3. The base64 fallback note for an image-only PDF (parsed successfully,
   ``status="empty"``) said the parser "could not read" it, which reads as
   a parser failure rather than "there is no text layer here; try OCR" —
   fixed in ``tools/web.py``'s ``_pdf_base64_note``.
4. The parent used ``Popen.communicate()``, which buffers the worker's
   entire stdout before any size check runs — a size cap enforced only
   after full buffering does not bound the parent's own memory use.
   ``extract_pdf_text_confined`` now reads stdout incrementally
   (``_read_stdout_capped``) and kills the worker the moment it exceeds the
   live byte ceiling, writing stdin from a separate thread
   (``_write_stdin_async``) so the two directions cannot deadlock each
   other.
"""

from __future__ import annotations

import importlib.util
import json
import os
import selectors
import signal
import subprocess
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass

# 10s is long enough for a normal document inside the 2 MB download cap and
# short enough that a hung parser cannot pin a hub worker.
PDF_EXTRACT_TIMEOUT_S = 10.0
PDF_MAX_PAGES = 50
PDF_MAX_OUTPUT_CHARS = 200_000
# Independent of (but expected to match) config.FETCH_MAX_BYTES: this module
# has no dependency on the rest of the package so it can be unit tested and
# reasoned about on its own.
PDF_MAX_INPUT_BYTES = 2 * 1024 * 1024
PDF_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
# Bytes of stdout the parent still trusts past the requested character cap
# (JSON wrapper overhead). The worker itself enforces max_output_chars; this
# is a second, independent live ceiling enforced while reading (see
# _read_stdout_capped), not a check applied after everything is buffered.
_STDOUT_ENVELOPE = 4096

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_BWRAP = "/usr/bin/bwrap"
# The host's core shared-library directories. Bound read-only unconditionally
# on Linux (see _bwrap_wrap for why gating this on the interpreter's own
# install path is not sufficient).
_LINUX_LIB_DIRS = ("/usr/lib", "/usr/lib64", "/lib", "/lib64")

_TERMINAL_STATUSES = frozenset(
    {"ok", "missing", "empty", "oversize", "confinement", "unreadable", "error", "timeout"}
)


class _OutputCeilingExceeded(Exception):
    """The worker wrote more stdout than the parent will ever trust."""


@dataclass(frozen=True)
class PdfExtractionResult:
    """Outcome of one confined extraction attempt. Never raises for bad input.

    ``status`` is one of:
      * ``ok``          - non-blank text was extracted; ``text`` is set.
      * ``oversize``     - the input exceeded ``max_input_bytes`` and was not
        sent to a worker at all.
      * ``confinement``  - the worker could not establish its resource
        limits or (on macOS) its sandbox, so the parser never ran.
      * ``missing``      - the worker started but pypdf is not importable.
      * ``unreadable``   - the parser raised, or the worker's output could
        not be parsed as the expected JSON envelope.
      * ``empty``        - the parser ran but returned no extractable text.
      * ``timeout``      - the wall-clock deadline was hit; the worker's
        process group was killed.
      * ``error``        - an unexpected local failure running the worker
        (spawn failure, I/O error) unrelated to the PDF's own content.
    """

    status: str
    text: str = ""
    page_capped: bool = False
    char_capped: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _read_roots() -> list[str]:
    """Filesystem roots the worker needs: interpreter + pypdf, nothing else.

    Shared by both the macOS seatbelt profile and the Linux bwrap sandbox —
    the same narrow allowlist backs both platforms' file-read confinement.

    Deliberately computed at runtime rather than hardcoded:
      * ``sys.prefix``      - the running venv (its site-packages, including
        pypdf and its dependencies, when installed there).
      * ``sys.base_prefix`` / ``sys.base_exec_prefix`` - the interpreter's
        own install (stdlib, shared libraries, framework bits) when running
        inside a venv.
      * ``sysconfig`` stdlib/platstdlib - belt and suspenders for interpreter
        layouts where these are not simply under ``base_prefix``.
      * pypdf's package directory, found with ``importlib.util.find_spec``
        (never imported in the parent) - covers a pypdf install
        that lives outside every path above (e.g. editable installs).

    This never resolves to ``/opt/homebrew`` or ``/Library`` as a whole on
    macOS, nor to ``$HOME`` as a whole on Linux (even though a venv often
    lives under it): if a real interpreter happens to live under one of
    those trees, only its own prefix directory is granted, not the tree it
    sits in.
    """
    roots: set[str] = set()

    def add(path: str | None) -> None:
        if not path:
            return
        real = os.path.realpath(path)
        if real and real != os.path.sep:
            roots.add(real)

    add(sys.prefix)
    add(sys.base_prefix)
    add(getattr(sys, "base_exec_prefix", None))
    add(sysconfig.get_path("stdlib"))
    add(sysconfig.get_path("platstdlib"))

    # Locate pypdf without importing it: the parent is the hub, and running
    # the package's import-time code here would execute it unconfined.
    try:
        spec = importlib.util.find_spec("pypdf")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin:
        add(os.path.dirname(os.path.realpath(spec.origin)))

    return sorted(roots)


def _quote_sbpl_path(path: str) -> str:
    if '"' in path or "\\" in path:
        raise ValueError(f"unsafe path for SBPL profile: {path!r}")
    return path


def build_macos_sandbox_profile(roots: list[str], executables: list[str] | None = None) -> str:
    """A minimal SBPL profile: deny default/network/write, allow read of ``roots``.

    No ``allow network*`` rule exists anywhere in this profile — ``deny
    default`` combined with the absence of such a rule is what blocks all
    outbound (and inbound) network access. There is no PDF path granted:
    the worker receives the PDF over stdin, never from a file on disk.

    ``executables`` names the exact interpreter binary path(s) ``sandbox-exec``
    itself needs ``process-exec`` rights for to launch the worker at all (its
    own ``execve`` is a sandboxed operation, checked before the target image
    ever starts running). Only those literal paths are granted exec rights —
    never a subpath or a blanket ``(allow process-exec)``.

    Verified live on the Mac hub (task #1523): a plain ``deny default`` plus
    ``allow file-read*`` on only the computed roots aborts the interpreter
    before it can even run ``print(1+1)`` (SIGABRT, no stderr); the kernel
    sandbox log shows ``deny(1) file-read-data /`` — something in CPython's
    own startup (observed on both a uv-managed standalone build and a
    Homebrew framework build) reads the root directory itself. Granting a
    single ``(literal "/")`` read — the root path only, never ``(subpath "/")``,
    so no recursive listing of the filesystem — fixed it without widening
    anything else.
    """
    if not roots:
        raise ValueError("no read roots computed for macOS sandbox profile")
    quoted = [_quote_sbpl_path(r) for r in roots]
    allow_reads = " ".join(f'(subpath "{r}")' for r in quoted)
    exec_paths = sorted({_quote_sbpl_path(p) for p in (executables or [])})
    allow_exec = (
        "(allow process-exec " + " ".join(f'(literal "{p}")' for p in exec_paths) + ")"
        if exec_paths
        else ""
    )
    return (
        "(version 1)"
        "(deny default)"
        "(deny network*)"
        "(deny file-write*)"
        f'(allow file-read* {allow_reads} (literal "/"))'
        "(allow file-read-metadata)"
        f"{allow_exec}"
        "(allow process-fork)"
        "(allow signal (target self))"
        "(allow sysctl-read)"
        '(allow file-ioctl (literal "/dev/null") (literal "/dev/urandom"))'
    )


# Standalone worker source. Deliberately self-contained (no import of this
# package): on macOS the seatbelt profile only grants the interpreter and
# pypdf's own paths, so importing unified_agent itself would not be
# readable, and the worker should not need it anyway. Resource limits are
# applied, and only then is stdin read — confinement is in place before any
# untrusted byte is touched.
_WORKER_SOURCE = r"""
import json
import sys


def _emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def main():
    try:
        max_pages = int(sys.argv[1])
        max_chars = int(sys.argv[2])
        as_bytes = int(sys.argv[3])
        cpu_seconds = int(sys.argv[4])
    except (IndexError, ValueError):
        _emit({"status": "confinement", "detail": "bad worker arguments"})
        return

    # The parent computes these via sysconfig and passes them explicitly
    # (see _worker_site_dirs) so pypdf resolves regardless of how this
    # interpreter was invoked. On Linux the sandboxed worker execs the
    # fully realpath'd interpreter binary directly (see _worker_argv) so
    # bwrap's otherwise-empty mount namespace does not need to replicate an
    # entire venv-launcher symlink chain — but that also bypasses the
    # interpreter's own automatic venv site-packages activation, which
    # walks up from sys.executable's original (unresolved) path to find
    # pyvenv.cfg. Harmless to insert on macOS too: already-present entries
    # are skipped.
    for _extra in sys.argv[5:7]:
        if _extra and _extra not in sys.path:
            sys.path.insert(0, _extra)

    try:
        import resource
    except ImportError:
        _emit({"status": "confinement", "detail": "resource module unavailable"})
        return

    # Verified live on the real Mac hub (task #1523): both RLIMIT_AS and
    # RLIMIT_DATA report an unlimited hard ceiling (getrlimit returns
    # RLIM_INFINITY for both) yet setrlimit still refuses any lower soft
    # limit ("current limit exceeds maximum limit") — macOS does not
    # actually support a settable address-space/data-segment cap here.
    # On Linux both are real and enforced; failing to set either means no
    # meaningful confinement exists at all there, so that is fatal. On
    # macOS the seatbelt (network/write deny, narrow read allowlist) plus
    # the parent's wall-clock timeout are the confinement, and a memory
    # ceiling is a best-effort extra: if it cannot be set, that does not by
    # itself stop the parser from running.
    order = ("RLIMIT_DATA", "RLIMIT_AS") if sys.platform == "darwin" else ("RLIMIT_AS", "RLIMIT_DATA")
    applied = False
    for name in order:
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        try:
            resource.setrlimit(limit, (as_bytes, as_bytes))
            applied = True
            break
        except (ValueError, OSError):
            continue
    if not applied and sys.platform != "darwin":
        _emit({"status": "confinement", "detail": "no address-space limit available"})
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except (ValueError, OSError, AttributeError):
        pass

    if sys.platform.startswith("linux"):
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            # PR_SET_NO_NEW_PRIVS = 38. Best effort, defense in depth: the
            # bwrap sandbox (namespaces + the read-only bind allowlist) is
            # the load-bearing confinement on Linux, not this call — failure
            # here does not abort the worker.
            libc.prctl(38, 1, 0, 0, 0)
        except Exception:
            pass

    try:
        # No size argument: the parent already refused anything over
        # max_input_bytes before spawning this worker at all, and passing
        # the (much larger) address-space limit here would make read() try
        # to preallocate a buffer that size and blow the limit itself.
        pdf_bytes = sys.stdin.buffer.read()
    except MemoryError:
        _emit({"status": "confinement", "detail": "input exceeded memory limit"})
        return
    except Exception as exc:
        _emit({"status": "error", "detail": "stdin read failed: " + type(exc).__name__})
        return

    try:
        from io import BytesIO

        from pypdf import PdfReader
    except ImportError:
        _emit({"status": "missing"})
        return

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        page_capped = page_count > max_pages
        parts = []
        used = 0
        char_capped = False
        for index in range(min(page_count, max_pages)):
            room = max_chars - used
            if room <= 0:
                char_capped = True
                break
            text = reader.pages[index].extract_text() or ""
            if not text:
                continue
            if len(text) > room:
                text = text[:room]
                char_capped = True
            parts.append(text)
            used += len(text) + 1
            if char_capped:
                break
        text_out = "\n".join(parts)
        if not text_out.strip():
            _emit({"status": "empty"})
            return
        _emit(
            {
                "status": "ok",
                "text": text_out,
                "page_capped": page_capped,
                "char_capped": char_capped,
            }
        )
    except MemoryError:
        _emit({"status": "confinement", "detail": "parser exceeded memory limit"})
    except Exception as exc:
        _emit({"status": "unreadable", "detail": type(exc).__name__})


main()
"""


def _worker_env() -> dict[str, str]:
    """Environment for the extractor. Hub credentials are never copied in."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _worker_site_dirs() -> tuple[str, str]:
    """purelib/platlib dirs the worker should add to ``sys.path`` explicitly.

    Needed because the Linux worker execs the fully realpath'd interpreter
    binary directly (see ``_worker_argv``), not the (possibly symlinked)
    venv launcher at ``sys.executable`` — bwrap's mount namespace starts
    empty, and replicating an entire venv-launcher symlink chain inside it
    (each hop can live outside the narrow ``_read_roots()`` allowlist) is
    both fragile and unnecessary. That bypass also skips the interpreter's
    own automatic venv site-packages activation, so pypdf would not be
    importable without passing these explicitly.
    """
    return sysconfig.get_path("purelib") or "", sysconfig.get_path("platlib") or ""


def _bwrap_wrap(command: list[str], roots: list[str]) -> list[str]:
    """Wrap ``command`` in a bubblewrap sandbox confined to ``roots``.

    ``--unshare-all`` drops every namespace bwrap knows how to, including
    network (no interfaces exist inside, so a socket syscall fails outright)
    and pid (the sandboxed command becomes PID 1 of its own pid namespace,
    so killing it tears down anything it forked, independent of the
    parent's own process-group kill). ``--die-with-parent`` means a
    killed/crashed hub process takes the sandbox down with it. ``--clearenv``
    plus a fixed set of ``--setenv`` calls (from ``_worker_env``) is the
    only environment the command sees. The mount namespace starts empty:
    only ``roots`` (the same computed interpreter/pypdf roots used for the
    macOS seatbelt) are bound read-only, plus the host's core
    shared-library directories, a private ``/proc``, ``/dev`` and ``/tmp``.
    There is no bind of ``$HOME`` anywhere in this list.

    ``/usr/lib``, ``/usr/lib64``, ``/lib`` and ``/lib64`` (whichever exist)
    are bound read-only unconditionally, not gated on the interpreter's own
    path being under ``/usr``: verified locally that even a fully
    self-contained interpreter build (a uv-managed standalone CPython
    living entirely under ``$HOME``) still dynamically links against the
    host's libc/ld.so under ``/usr/lib`` — gating the bind on the
    interpreter's own install path would silently break extraction on
    exactly that kind of setup. They are never a stand-in for ``roots``:
    nothing else under them (or under any other system directory such as
    ``/etc``, ``/var``, or ``/opt``) is bound, and nothing is writable.
    """
    args = [
        _BWRAP,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    for root in roots:
        args += ["--ro-bind", root, root]
    for lib_dir in _LINUX_LIB_DIRS:
        if lib_dir not in roots and os.path.isdir(lib_dir):
            args += ["--ro-bind", lib_dir, lib_dir]
    for key, value in _worker_env().items():
        args += ["--setenv", key, value]
    args.append("--")
    args.extend(command)
    return args


def _bwrap_preflight(roots: list[str]) -> str | None:
    """Return ``None`` if bwrap can sandbox this interpreter, else why not.

    Wraps a trivial ``-c "pass"`` run of the real interpreter binary in the
    exact same ``_bwrap_wrap`` call the real worker invocation will use, so
    a preflight pass means the real sandbox setup (namespaces, binds, exec)
    will also work — not merely that the ``bwrap`` binary is present.
    """
    if not os.path.exists(_BWRAP):
        return "bwrap missing"
    python_real = os.path.realpath(sys.executable)
    argv = _bwrap_wrap([python_real, "-c", "pass"], roots)
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except OSError as exc:
        return f"bwrap spawn failed: {exc}"
    except subprocess.TimeoutExpired:
        return "bwrap preflight timed out"
    if proc.returncode != 0:
        lines = proc.stderr.decode(errors="replace").strip().splitlines()
        suffix = f" ({lines[0]})" if lines else ""
        return f"bwrap preflight failed: exit {proc.returncode}{suffix}"
    return None


def _worker_argv(worker_args: list[str]) -> tuple[list[str] | None, str | None]:
    """Return ``(argv, None)`` or ``(None, failure_reason)``.

    ``None`` argv means: do not start a worker at all, fail closed.
    """
    python = sys.executable
    site_dirs = list(_worker_site_dirs())
    if sys.platform == "darwin":
        # -P: do not prepend the current working directory to sys.path for
        # -c (PYTHONSAFEPATH, set in _worker_env, was not sufficient by
        # itself when verified live on the Mac hub — the interpreter still
        # put '' first and a sandboxed stat() of an arbitrary cwd raised
        # PermissionError deep inside import machinery, aborting the worker
        # before it read stdin).
        base = [python, "-P", "-c", _WORKER_SOURCE, *worker_args, *site_dirs]
        if not os.path.exists(_SANDBOX_EXEC):
            return None, "sandbox-exec missing"
        roots = _read_roots()
        # sandbox-exec's own execve of the interpreter is itself a sandboxed
        # process-exec operation. Grant it only for the exact binary path(s)
        # actually invoked: the literal argv[0] and, when a venv's python is
        # a symlink, the real binary it resolves to.
        executables = {python, os.path.realpath(python)}
        # A framework build (Homebrew python@3.x, python.org installers)
        # re-execs bin/python3.x into Resources/Python.app/Contents/MacOS/Python;
        # without this grant every worker exits 1 with a posix_spawn error.
        # It lives under base_prefix, which _read_roots already grants.
        app = os.path.join(sys.base_prefix, "Resources/Python.app/Contents/MacOS/Python")
        if os.path.isfile(app):
            executables.add(os.path.realpath(app))
        try:
            profile = build_macos_sandbox_profile(roots, executables=sorted(executables))
        except ValueError as exc:
            return None, f"unsafe sandbox profile: {exc}"
        return [_SANDBOX_EXEC, "-p", profile, *base], None
    if sys.platform.startswith("linux"):
        roots = _read_roots()
        failure = _bwrap_preflight(roots)
        if failure:
            return None, failure
        # The real (non-symlinked) interpreter binary: bwrap's mount
        # namespace starts empty, so a venv launcher symlink whose target
        # hops outside the narrow read-root allowlist (e.g. through an
        # unversioned intermediate symlink directory one level up from the
        # resolved interpreter prefix) cannot be resolved inside it. Using
        # the realpath directly sidesteps needing to replicate that chain.
        python_real = os.path.realpath(python)
        base = [python_real, "-P", "-c", _WORKER_SOURCE, *worker_args, *site_dirs]
        return _bwrap_wrap(base, roots), None
    return None, f"unsupported platform: {sys.platform}"


def _kill_process_group(leader_pid: int, proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group led by ``leader_pid``.

    ``leader_pid`` must be the pid captured immediately after ``Popen`` with
    ``start_new_session=True``: at that instant ``pid == pgid``, and the
    pgid keeps identifying the group for as long as any member is alive —
    including after the direct child (the group leader) has itself already
    exited, e.g. because it forked a descendant and exited while that
    descendant kept the stdout pipe open. ``os.getpgid(proc.pid)`` is
    deliberately not used: once the leader has exited, ``getpgid`` raises
    ``ProcessLookupError`` even though the group, and any surviving
    descendant in it, is still alive — resolving the pgid at kill time
    rather than using the one captured at spawn time was exactly the bug
    that let a forked descendant outlive a timeout.
    """
    try:
        os.killpg(leader_pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _write_stdin_async(proc: subprocess.Popen, data: bytes) -> threading.Thread:
    """Write ``data`` to the worker's stdin off the main thread.

    Reading the worker's stdout incrementally (``_read_stdout_capped``)
    means the parent can no longer use ``Popen.communicate()``, which
    writes stdin and reads stdout concurrently internally to avoid a
    classic pipe deadlock (the child blocked writing a full stdout pipe
    while the parent is still blocked writing a large stdin pipe). Writing
    from a separate thread here preserves that property.
    """

    def _write() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(data)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass

    thread = threading.Thread(target=_write, daemon=True)
    thread.start()
    return thread


def _reap(proc: subprocess.Popen, *, writer: threading.Thread | None = None) -> None:
    """Best-effort cleanup after a kill or an I/O exception.

    Joins the stdin-writer thread, closes both pipes, and collects the
    zombie so a killed worker does not linger as a defunct process.
    """
    if writer is not None:
        writer.join(timeout=1)
    for stream in (proc.stdin, proc.stdout):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _read_stdout_capped(proc: subprocess.Popen, *, deadline: float, max_bytes: int) -> bytes:
    """Read stdout incrementally, never buffering past ``max_bytes``.

    Raises ``TimeoutError`` if ``deadline`` (a ``time.monotonic()``
    timestamp) passes before the worker's stdout reaches EOF, or
    ``_OutputCeilingExceeded`` the moment more than ``max_bytes`` has
    arrived. Either way the caller is expected to kill the process group
    immediately rather than keep reading. This replaces
    ``Popen.communicate()``, which buffers the child's entire output in
    memory before the parent ever gets a chance to check its size — a
    check applied only after full buffering does not bound the parent's
    own memory use against an overproducing (or hostile) worker.
    """
    if proc.stdout is None:
        return b""
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    chunks: list[bytes] = []
    total = 0
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("pdf worker stdout deadline exceeded")
            if not selector.select(min(remaining, 0.25)):
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise _OutputCeilingExceeded()
    finally:
        selector.close()
    return b"".join(chunks)


def extract_pdf_text_confined(
    pdf_bytes: bytes,
    *,
    timeout_s: float = PDF_EXTRACT_TIMEOUT_S,
    max_pages: int = PDF_MAX_PAGES,
    max_output_chars: int = PDF_MAX_OUTPUT_CHARS,
    max_input_bytes: int = PDF_MAX_INPUT_BYTES,
    address_space_bytes: int = PDF_ADDRESS_SPACE_BYTES,
) -> PdfExtractionResult:
    """Extract PDF text in a short-lived, confined subprocess.

    The PDF bytes are handed to the worker over stdin only, after the worker
    has already applied its resource limits (and, on macOS, after the whole
    process has already been wrapped in a ``sandbox-exec`` profile at exec
    time; on Linux, after the whole process has already been wrapped in a
    ``bwrap`` sandbox) — nothing in the worker reads the untrusted bytes
    before that confinement takes effect.

    On timeout, or if the worker's stdout exceeds its live byte ceiling, the
    worker's whole process group is killed using the pid captured at spawn
    time (``start_new_session=True`` + ``os.killpg`` on that saved pid, not
    a freshly re-resolved one — see ``_kill_process_group``), not just the
    direct child, so a forked descendant cannot outlive the deadline even if
    the direct child has already exited.

    Never raises for an untrusted-input or confinement failure; every such
    case comes back as a non-"ok" ``PdfExtractionResult`` so the caller can
    fall back to the existing non-extracted (base64) response.
    """
    if len(pdf_bytes) > max_input_bytes:
        return PdfExtractionResult(status="oversize", detail=f"{len(pdf_bytes)} bytes")

    if os.name != "posix":
        return PdfExtractionResult(
            status="confinement", detail=f"unsupported platform: {sys.platform}"
        )

    worker_args = [
        str(max_pages),
        str(max_output_chars),
        str(address_space_bytes),
        str(max(1, int(timeout_s))),
    ]
    argv, failure = _worker_argv(worker_args)
    if argv is None:
        return PdfExtractionResult(status="confinement", detail=failure or "sandbox unavailable")

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_worker_env(),
            start_new_session=True,
        )
    except OSError as exc:
        return PdfExtractionResult(status="confinement", detail=f"spawn failed: {exc}")

    # Captured immediately: with start_new_session=True this pid IS the
    # process-group id for as long as ANY member of the group is alive,
    # including a forked descendant that outlives the direct child. See
    # _kill_process_group for why this must not be re-resolved later.
    leader_pid = proc.pid
    writer = _write_stdin_async(proc, pdf_bytes)
    deadline = time.monotonic() + timeout_s
    # The worker's own source (not attacker-controlled — only the PDF bytes
    # it parses are) already enforces max_output_chars before it emits
    # anything, so this is the parent's own defense-in-depth ceiling, not a
    # hostile-worker's expected path. The multiplier tolerates JSON's
    # worst-case \\uXXXX escaping (~6x) plus the small envelope.
    max_stdout_bytes = max_output_chars * 6 + _STDOUT_ENVELOPE

    try:
        stdout = _read_stdout_capped(proc, deadline=deadline, max_bytes=max_stdout_bytes)
    except TimeoutError:
        _kill_process_group(leader_pid, proc)
        _reap(proc, writer=writer)
        return PdfExtractionResult(status="timeout", detail=f"{timeout_s:g}s")
    except _OutputCeilingExceeded:
        _kill_process_group(leader_pid, proc)
        _reap(proc, writer=writer)
        return PdfExtractionResult(status="unreadable", detail="worker exceeded the output ceiling")
    except Exception as exc:
        _kill_process_group(leader_pid, proc)
        _reap(proc, writer=writer)
        return PdfExtractionResult(status="error", detail=type(exc).__name__)

    _reap(proc, writer=writer)

    if proc.returncode not in (0, None):
        return PdfExtractionResult(status="unreadable", detail=f"exit {proc.returncode}")

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return PdfExtractionResult(status="unreadable", detail="non-JSON worker output")

    if not isinstance(payload, dict):
        return PdfExtractionResult(status="unreadable", detail="malformed worker output")

    status = payload.get("status")
    if status == "ok":
        text = payload.get("text", "")
        char_capped = bool(payload.get("char_capped", False))
        if not isinstance(text, str) or not text.strip():
            return PdfExtractionResult(status="empty")
        if len(text) > max_output_chars:
            text = text[:max_output_chars]
            char_capped = True
        return PdfExtractionResult(
            status="ok",
            text=text,
            page_capped=bool(payload.get("page_capped", False)),
            char_capped=char_capped,
        )
    if status in _TERMINAL_STATUSES:
        return PdfExtractionResult(status=status, detail=str(payload.get("detail", "")))
    return PdfExtractionResult(status="unreadable", detail="unrecognized worker status")
