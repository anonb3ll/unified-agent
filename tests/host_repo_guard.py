"""Scope git worktree/branch snapshots to the checkout under test."""

import json
import re
from pathlib import Path


def path_is_in_repo(path: str, repo: Path) -> bool:
    """True when `path` is this checkout or a descendant of it."""
    try:
        resolved = Path(path).expanduser().resolve()
        repo_r = repo.resolve()
    except OSError:
        return False
    return resolved == repo_r or repo_r in resolved.parents


def worktree_branch_name(line: str) -> str | None:
    """Branch from a `git worktree list` row (`[branch]`), or None if detached."""
    start = line.rfind("[")
    end = line.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    return line[start + 1 : end].strip() or None


def branch_list_name(line: str) -> str:
    """Strip `git branch --list` markers (`*`, `+`) to the branch name."""
    return line.strip().lstrip("*+").strip()


def settle_mismatch(before, first_after, poll, max_retries=1, same=None):
    """Resolve a before/after mismatch that may be transient concurrent churn.

    `scope_host_repo_state` handles the *spatial* half of concurrency: a
    worktree (and the branch it has checked out) that lives outside this
    checkout is dropped before comparison. It has no way to handle the
    *temporal* half. `git branch --list` is gitdir-global and read live, so
    another agent's `git fetch -q origin pull/523/head:pr-523-review` (or the
    matching `git branch -D` cleanup) can land squarely inside the window
    between a session's before/after snapshots. That branch has no worktree
    at all, so there is nothing for `scope_host_repo_state` to scope it out
    by — it is not misplaced, it is just mid-flight.

    Rather than trying to spatially exclude something that has no location
    to exclude by, treat it as what it actually is: a race. `first_after` is
    the mismatch already observed; re-read state via `poll()` up to
    `max_retries` more times. The moment a read matches `before` again, the
    mismatch was transient churn, not an escape, and this returns that match.
    A mismatch still present after every retry is persistent — the signature
    of a test that really did create or delete a branch and leave it that
    way — and is returned as-is for the caller to fail on. `poll` is a
    zero-arg callable so the real caller can inject a delay between reads
    (concurrent git operations settle in well under a second) while this
    function stays pure and testable with a canned sequence of return values.
    `same` overrides plain equality, for a caller whose notion of a match can
    widen between reads (the host guard's excused branches, task #1998).
    """
    if same is None:

        def same(old, new):
            return old == new

    current = first_after
    for _ in range(max_retries):
        if current is None or same(before, current):
            return current
        current = poll()
    return current


_HUMAN_WORKTREE = re.compile(
    r"(?P<path>.+?) +"
    r"(?:(?P<HEAD>[0-9a-fA-F]{4,64}) +"
    r"(?P<state>\[[^\s]+\]|\(detached HEAD\))|(?P<bare>\(bare\)))"
    r"(?P<flags>(?: +(?:locked|prunable))*)"
)


def _git_c_quoted(value: str) -> str:
    """Decode Git's quoted bytes, including octal UTF-8 and filesystem bytes."""
    if not value.startswith('"'):
        return value
    if len(value) < 2 or not value.endswith('"'):
        raise ValueError("unterminated Git quoted value")
    escapes = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
    output = bytearray()
    index = 1
    end = len(value) - 1
    while index < end:
        char = value[index]
        if char == '"' or ord(char) < 32:
            raise ValueError("invalid literal in Git quoted value")
        if char != "\\":
            output.extend(char.encode("utf-8", errors="surrogateescape"))
            index += 1
            continue
        index += 1
        if index >= end:
            raise ValueError("unterminated Git escape")
        char = value[index]
        if char in escapes:
            output.append(escapes[char])
            index += 1
        elif (
            char in "0123"
            and index + 3 <= end
            and all(digit in "01234567" for digit in value[index : index + 3])
        ):
            output.append(int(value[index : index + 3], 8))
            index += 3
        else:
            raise ValueError("invalid Git escape")
    if 0 in output:
        raise ValueError("NUL is not a Git path or porcelain value")
    return output.decode("utf-8", errors="surrogateescape")


_ADMIN_SNAPSHOT = "git-admin-snapshot-v1\0"


def administrative_worktree_snapshot(repo: Path, run) -> str:
    """Read older Git's worktree metadata without ambiguous LF path output.

    Git's linked-worktree gitdir file contains a raw path followed by one LF;
    removing exactly that separator preserves newline/trailing-space paths.
    Keep the original administrative bytes for equality, resolving symbolic
    HEAD as well so changes to a branch's commit remain visible. Only metadata
    which determines worktree identity/state is read; index and logs are not
    host-repository guard inputs.
    """

    def command(*args, allowed=()):
        result = run(["git", *args], cwd=repo, capture_output=True, text=False, timeout=30)
        if result.returncode != 0 and result.returncode not in allowed:
            raise RuntimeError("host Git administrative snapshot is unavailable")
        return result

    def terminated_path(raw: bytes, separator: bytes, *, base=repo, absolute=False) -> Path:
        if not raw.endswith(separator) or not raw[: -len(separator)]:
            raise RuntimeError("host Git administrative path is unavailable")
        value = Path(raw[: -len(separator)].decode("utf-8", errors="surrogateescape"))
        if absolute and not value.is_absolute():
            raise RuntimeError("host Git administrative path must be absolute")
        return value if value.is_absolute() else base / value

    common = terminated_path(command("rev-parse", "--git-common-dir").stdout, b"\n").resolve()
    current = terminated_path(command("rev-parse", "--git-dir").stdout, b"\n").resolve()
    bare = command(f"--git-dir={common}", "rev-parse", "--is-bare-repository").stdout == b"true\n"
    configured = command(
        f"--git-dir={common}", "config", "--null", "--get", "core.worktree", allowed=(1,)
    )
    if bare:
        main_path = common
    elif current == common:
        main_path = terminated_path(command("rev-parse", "--show-toplevel").stdout, b"\n")
    elif configured.returncode == 0:
        # core.worktree relative paths are relative to the common gitdir.
        main_path = terminated_path(configured.stdout, b"\0", base=common)
    else:
        main_path = common.parent

    def optional_bytes(path):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def record(admin, path, raw_files, *, is_bare=False):
        head = command(f"--git-dir={admin}", "rev-parse", "--verify", "HEAD", allowed=(128,))
        data = {
            "worktree": str(path),
            "admin": str(admin),
            "admin_files": {
                name: value.hex() if value is not None else None
                for name, value in raw_files.items()
            },
            "HEAD": head.stdout.decode("utf-8", errors="surrogateescape").removesuffix("\n")
            if head.returncode == 0
            else None,
        }
        if is_bare:
            data["bare"] = ""
        else:
            raw_head = raw_files["HEAD"] or b""
            symbolic = raw_head.removesuffix(b"\n")
            if symbolic.startswith(b"ref: refs/heads/"):
                data["branch"] = symbolic[5:].decode("utf-8", errors="surrogateescape")
            else:
                data["detached"] = ""
            git_file = path / ".git"
            data["git_file_exists"] = git_file.exists()
            data["worktree_exists"] = path.is_dir()
            if git_file.is_file():
                data["git_file_bytes"] = git_file.read_bytes().hex()
        return data

    records = [record(common, main_path, {"HEAD": optional_bytes(common / "HEAD")}, is_bare=bare)]
    worktrees_dir = common / "worktrees"
    if worktrees_dir.exists():
        for admin in sorted(worktrees_dir.iterdir()):
            if not admin.is_dir():
                raise RuntimeError("host Git administrative worktree entry is unavailable")
            raw_files = {
                name: optional_bytes(admin / name)
                for name in ("gitdir", "HEAD", "locked", "commondir")
            }
            raw_gitdir = raw_files["gitdir"]
            if raw_gitdir is None:
                records.append(
                    {
                        "opaque_admin": str(admin),
                        "admin_files": {
                            name: value.hex() if value is not None else None
                            for name, value in raw_files.items()
                        },
                    }
                )
                continue
            git_file = terminated_path(raw_gitdir, b"\n", absolute=True)
            if not git_file.is_absolute() or git_file.name != ".git":
                raise RuntimeError("host Git administrative worktree path is unavailable")
            records.append(record(admin, git_file.parent, raw_files))
    return _ADMIN_SNAPSHOT + json.dumps(records, sort_keys=True)


def _worktree_records(text: str):
    """Parse exact machine records, retaining malformed input as opaque state.

    Live snapshots prefer porcelain -z and use administrative metadata on
    older Git. Legacy LF callers retain original bytes for equality; Git C
    quoting is decoded only to scope paths. Ambiguous LF records stay opaque.
    Ordinary display rows remain supported for older callers/unit fixtures;
    only their column padding is normalized.
    """
    if text.startswith(_ADMIN_SNAPSHOT):
        for record in json.loads(text.removeprefix(_ADMIN_SNAPSHOT)):
            path = record.get("worktree")
            branch = record.get("branch")
            yield path, branch.removeprefix("refs/heads/") if branch else None, record
        return
    if "\0" in text:
        if not text.endswith("\0\0"):
            yield None, None, {"opaque": text}
            return
        for raw in text[:-2].split("\0\0"):
            record = {}
            valid = True
            for field in raw.split("\0"):
                name, separator, value = field.partition(" ")
                if (
                    name in record
                    or name
                    not in {"worktree", "HEAD", "branch", "detached", "bare", "locked", "prunable"}
                    or (name in {"worktree", "HEAD", "branch"} and not separator)
                    or (name in {"detached", "bare"} and separator)
                ):
                    valid = False
                    break
                record[name] = value
            path = record.get("worktree")
            branch = record.get("branch")
            valid = valid and bool(path) and Path(path).is_absolute()
            if "bare" in record:
                valid = valid and not any(key in record for key in ("HEAD", "branch", "detached"))
            else:
                valid = (
                    valid
                    and bool(
                        re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", record.get("HEAD", ""))
                    )
                    and ((branch is not None) != ("detached" in record))
                    and (
                        branch is None
                        or (branch.startswith("refs/heads/") and branch != "refs/heads/")
                    )
                )
            if valid:
                yield path, branch.removeprefix("refs/heads/") if branch else None, record
            else:
                yield None, None, {"opaque": raw}
        return
    if text.startswith("worktree "):
        if not text.endswith("\n\n"):
            yield None, None, {"opaque": text}
            return
        for raw in text[:-2].split("\n\n"):
            try:
                fields = []
                for field in raw.split("\n"):
                    name, separator, value = field.partition(" ")
                    if separator and name in {"worktree", "locked", "prunable"}:
                        value = _git_c_quoted(value)
                    fields.append(name + separator + value)
                parsed = list(_worktree_records("\0".join(fields) + "\0\0"))
                path, branch, record = parsed[0]
                if path is None or len(parsed) != 1:
                    yield None, None, {"opaque": raw}
                else:
                    # Equality retains original bytes, including literal quotes
                    # in reasons. Decoding is solely for path/branch scoping.
                    record["plain_porcelain_bytes"] = raw.encode(
                        "utf-8", errors="surrogateescape"
                    ).hex()
                    yield path, branch, record
            except (ValueError, UnicodeError):
                yield None, None, {"opaque": raw}
        return
    for raw in text.splitlines():
        if not raw.strip():
            continue
        match = _HUMAN_WORKTREE.fullmatch(raw.rstrip())
        if match is None or match["path"].startswith('"'):
            yield None, None, {"opaque": raw}
            continue
        record = {"worktree": match["path"]}
        branch = None
        if match["bare"]:
            record["bare"] = ""
        else:
            record["HEAD"] = match["HEAD"]
            if match["state"] == "(detached HEAD)":
                record["detached"] = ""
            else:
                branch = match["state"][1:-1]
                record["branch"] = "refs/heads/" + branch
        flags = match["flags"].split()
        if len(flags) != len(set(flags)):
            yield None, None, {"opaque": raw}
            continue
        record.update(dict.fromkeys(flags, ""))
        yield match["path"], branch, record


# One HEAD reflog line: `<old> <new> <ident> <unix-ts> <tz>\t<message>`.
_REFLOG_CHECKOUT = re.compile(r"> (?P<ts>\d+) [+-]\d{4}\tcheckout: moving from (\S+) to (\S+)$")


def _head_reflog(path: str, record: dict) -> Path | None:
    """The HEAD reflog of the worktree at `path`, or None if it cannot be found."""
    admin = record.get("admin")
    if admin:
        return Path(admin) / "logs" / "HEAD"
    dot_git = Path(path) / ".git"
    try:
        if dot_git.is_dir():
            return dot_git / "logs" / "HEAD"
        if dot_git.is_file():
            pointer = dot_git.read_text(encoding="utf-8", errors="surrogateescape").strip()
            if pointer.startswith("gitdir: "):
                gitdir = Path(pointer.removeprefix("gitdir: "))
                return (gitdir if gitdir.is_absolute() else Path(path) / gitdir) / "logs" / "HEAD"
    except OSError:
        return None
    return None


def external_branch_names(repo: Path, worktrees_text: str, *, since: float) -> set[str]:
    """Branches another checkout of this gitdir can account for (task #1998).

    `scope_host_repo_state` drops a branch only while a worktree outside
    `repo` has it checked out. Other agents' lanes do not hold still: a lane
    adds `worktree-agent-x`, then `git switch -c claude/<id>-...`, which leaves
    `worktree-agent-x` as a bare ref no worktree holds. That ref is not this
    suite's doing, yet a snapshot alone cannot tell it from a test escape.

    The external worktree's own HEAD reflog can: `checkout: moving from A to
    B` names both branches it held. Only switches made at or after `since`
    (the guard's start) count, so an old switch in a long-lived checkout
    cannot excuse a name a test reuses. A reflog that cannot be read
    attributes nothing, which keeps the guard on the failing side.
    """
    names: set[str] = set()
    for path, branch, record in _worktree_records(worktrees_text):
        if path is None or path_is_in_repo(path, repo):
            continue
        if branch:
            names.add(branch)
        log = _head_reflog(path, record)
        if log is None:
            continue
        try:
            lines = log.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _REFLOG_CHECKOUT.search(line)
            if match and int(match["ts"]) >= since:
                names.update((match[2], match[3]))
    return names


def drop_branches(state: tuple[str, str], names: set[str]) -> tuple[str, str]:
    """`state` without the `git branch --list` rows for `names`."""
    worktrees, branches = state
    kept = [raw for raw in branches.splitlines() if branch_list_name(raw) not in names]
    return worktrees, "\n".join(kept)


def scope_host_repo_state(repo: Path, worktrees_text: str, branches_text: str) -> tuple[str, str]:
    """Keep only mutations this checkout's tests can cause.

    `git worktree list` / `git branch --list` are repo-global, so a concurrent
    `task_claim` in another worktree (task #482 review: cockpit
    `.worktrees/task-483`) looks identical to a test escaping tmp_path. Scope
    worktrees to paths inside `repo`. Drop branches that are only the checkout
    of a worktree *outside* `repo`. In-checkout worktrees and local branches
    such as `agy/task-1-...` still compare, which is the original leak.
    """
    in_repo_lines: list[str] = []
    external_branches: set[str] = set()
    for path, branch, record in _worktree_records(worktrees_text):
        # Unknown records cannot safely be scoped out: retain them verbatim.
        if path is None or path_is_in_repo(path, repo):
            in_repo_lines.append(json.dumps(record, sort_keys=True))
        elif branch:
            external_branches.add(branch)
    scoped_branches = [
        raw.rstrip()
        for raw in branches_text.splitlines()
        if raw.strip() and branch_list_name(raw) not in external_branches
    ]
    return "\n".join(in_repo_lines), "\n".join(scoped_branches)
