"""Read or write a file only if it is a regular file, never blocking (#2212).

Opening a FIFO with no writer (or, to write, with no reader) blocks forever,
and a device reports `st_size == 0`, so a size check alone never refuses one.
Every audit-package reader or writer of a path inside a scanned tree or the
vault anchor directory goes through this module: the engine (scanned files
and `.ua-audit-ignore`), the repo rules (agent settings, MCP configs,
`.gitignore`), remediation, and the vault anchor scans, verifier and
re-anchor writer.
"""

import errno
import io
import locale
import os
import stat
from pathlib import Path

_CHUNK = 1 << 20
_NOCTTY = getattr(os, "O_NOCTTY", 0)
_OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | _NOCTTY
# No O_TRUNC: the file is truncated only once fstat has shown it is regular.
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | _NOCTTY


class NotRegularFileError(OSError):
    """The path is a FIFO, socket, device or directory, so it was not written."""

    def __init__(self, path: Path | str) -> None:
        super().__init__(errno.EINVAL, "not a regular file", str(path))


def read_regular_file(path: Path | str, limit: int | None = None) -> bytes | None:
    """Return the bytes of `path`, or None if it is not a regular file (or a
    link to one), cannot be read, or is larger than `limit` bytes.

    The type is checked twice: on the path, so a device or FIFO is never
    opened, and on the open descriptor, so a file swapped for a FIFO between
    the check and the open cannot block. O_NONBLOCK makes that open return at
    once on a FIFO; it has no effect on a regular file. With a `limit`, at most
    one byte past it is read, so a file that grows after the check is refused
    rather than read whole."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if limit is not None and st.st_size > limit:
            return None
        chunks = []
        size = 0
        while limit is None or size <= limit:
            want = _CHUNK if limit is None else min(_CHUNK, limit + 1 - size)
            chunk = os.read(fd, want)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    if limit is not None and size > limit:
        return None
    return b"".join(chunks)


def read_regular_text(
    path: Path | str, *, encoding: str | None = "utf-8", errors: str = "strict"
) -> str | None:
    """`read_regular_file`, decoded with the newline handling of a text-mode
    `open()` (`\r\n` and `\r` become `\n`), so it can replace
    `Path.read_text(...)` or `open(...)` with the same arguments.
    `encoding=None` means what it means there: the locale's preferred
    encoding, or UTF-8 in UTF-8 mode. A decoding error is raised, as there,
    unless `errors` says otherwise."""
    data = read_regular_file(path)
    if data is None:
        return None
    if encoding is None:
        encoding = locale.getpreferredencoding(False)
    return io.StringIO(data.decode(encoding, errors=errors), newline=None).read()


def write_regular_file(path: Path | str, data: bytes) -> None:
    """`Path.write_bytes`, but only into a regular file (or a link to one).

    A missing file is created (mode 0o666 less the umask), as there. A FIFO,
    socket or device raises `NotRegularFileError` and is left untouched. The
    type is checked on the path and again on the descriptor; O_NONBLOCK makes
    the open of a FIFO with no reader fail with ENXIO instead of blocking."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise NotRegularFileError(path)
    except FileNotFoundError:
        pass  # created below, through a dangling link too, like write_bytes
    try:
        fd = os.open(path, _WRITE_FLAGS, 0o666)
    except OSError as exc:
        if exc.errno == errno.ENXIO:  # a FIFO with no reader
            raise NotRegularFileError(path) from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFileError(path)
        os.ftruncate(fd, 0)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def write_regular_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """`Path.write_text(text, encoding=...)` on POSIX, through `write_regular_file`."""
    write_regular_file(path, text.encode(encoding))
