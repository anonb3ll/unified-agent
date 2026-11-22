"""File audit findings as hub tasks with stable ``[audit-triage:]`` markers.

Shared by ``audit_triage_tasks`` (MCP) and ``unified-agent-audit surfaces
--file-tasks`` (scheduled CLI). Dedup is by marker against tasks that are
still open — a group whose task was closed (``done``, ``cancelled`` or
archived) and which reappears is new work, unless the closed task is marked
as an accepted risk (below).

The marker is scoped to the audited root, ``[audit-triage: <root>:<rule>]``,
because ``surfaces`` triages once per root: a rule-only marker let one task
for ``~/.cursor`` suppress the same rule on every other root (task #1630).
Tasks filed before that carry the legacy ``[audit-triage: <rule>]`` marker;
an open one still dedupes, but only for the root its notes name.

Within a root and rule, findings are split by class (task #2157): the first
segment of the finding's path relative to the root (``sessions``,
``auth.json``, ``src``), or ``(repo)`` for a finding with no path. The marker
is ``[audit-triage: <root>:<rule>:<class>]``, so a task covers only the class
it was filed for. Before this, one ``[accepted-risk]`` task written for
``~/.codex`` credential stores (``auth.json``) also swallowed a later, new
class of the same rule in ``~/.codex/sessions``, and nothing was filed.

Markers without a class (``<root>:<rule>``, and the rule-only legacy form)
are still honoured, for the classes they were filed for: every filed task
carries, under its ``Auto-filed from ...`` header, one
``- [severity] path[:line]: message`` line per finding (its title counts
them), and those paths name its classes. Only that block is read, so a line
appended to the notes later cannot widen the task. An old task therefore
keeps covering what it was opened or accepted for, and nothing already
accepted is refiled. A legacy task without that header (written by hand)
covers every class of its rule, as it did before.

Accepted risk: to stop a group from being refiled after review, close its
task (``done`` or ``cancelled``) and add ``[accepted-risk]`` to its notes,
keeping the ``[audit-triage: ...]`` marker. A closed task carrying both
suppresses the group on every later run, for the same root and class only;
delete the tag (or reopen the task) to have the group tracked again. A closed
task without the tag still means "addressed", so a reappearing group is
refiled.

The tag is by class; the gate is by value. A finding in a file the operator
baseline holds, at a value it does not hold (the report's ``baseline.rotated``
paths), fails the gate no matter what any task says, so the tag does not
suppress that group (task #2419): it refiles once, the new open task owns it,
and ``baseline accept`` on the new value quiets the file again. A task's
finding text is deliberately not compared: it changes whenever a log does,
and that churn is what #2157 removed.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePath

from .rules import SEVERITIES

_SEV_RANK = {sev: i for i, sev in enumerate(SEVERITIES)}

# Statuses that end tracking. Archived rows are already excluded by
# task_list's default ``archived=False``.
_CLOSED_STATUSES = frozenset({"done", "cancelled"})

# Notes tag on a closed triage task: the operator accepted the risk, so the
# group is not refiled (see the module docstring).
ACCEPTED_RISK_TAG = "[accepted-risk]"


def group_key(finding: dict) -> str:
    """Group findings by rule, then by file, then a catch-all.

    A Finding carries ``rule`` and ``path``. An earlier prototype grouped on
    ``rule_id``/``file``, neither of which exists, so every finding collapsed
    into a single "general" bucket.
    """
    return finding.get("rule") or finding.get("path") or "general"


def root_label(root: str | None) -> str | None:
    """Short, human-readable name for an audited root: its basename
    (``/Users/x/.hermes`` -> ``.hermes``), or None when the report has none.
    Used in titles only; two roots can share a basename."""
    if not root:
        return None
    return PurePath(str(root)).name or str(root)


def root_key(root: str | None) -> str | None:
    """Unique dedup identity for an audited root: ``<basename>@<digest>``.

    The digest covers the full root path, so two roots with the same
    basename (``~/a/.hermes`` and ``~/b/.hermes``) never share a marker and
    one root's open task cannot suppress the other's findings.
    """
    label = root_label(root)
    if label is None:
        return None
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{label}@{digest}"


# Class of a finding with no path (repo rules), spelled as its notes line
# renders the path. Real paths go through `_safe_class`, which drops
# parentheses, so no file (not even one named "(repo)") can share it.
REPO_CLASS = "(repo)"
_CLASS_UNSAFE_RX = re.compile(r"[^A-Za-z0-9._@+-]")
_CLASS_MAX = 48
# The header `file_findings_as_tasks` writes above its finding lines, and the
# count its title ends with. Together they bound the block a classless
# (pre-#2157) task's coverage is read from.
_AUTO_FILED_HEADER = "Auto-filed from security_audit over "
_TITLE_COUNT_RX = re.compile(r"— (?P<n>\d+) finding\(s\)\s*$")
# One finding line in that block: "- [high] path:12: message",
# "- [high] path: message" or "- [high] (repo): message", and the indented
# "  fix: ..." line that may follow it.
_NOTES_FINDING_RX = re.compile(r"^- \[(?:high|medium|low|info)\] (?P<rest>.+)$")
_NOTES_FIX_PREFIX = "  fix: "
_LINE_NUMBER_SPLIT_RX = re.compile(r":\d+: ")


def _safe_class(segment: str) -> str:
    """A path segment as a marker token: no ``]``, ``:`` or whitespace, bounded.
    A segment that had to change gets a digest so two cannot collide."""
    safe = _CLASS_UNSAFE_RX.sub("_", segment)
    if safe == segment and len(safe) <= _CLASS_MAX:
        return safe
    digest = hashlib.sha256(segment.encode("utf-8")).hexdigest()[:8]
    return f"{safe[: _CLASS_MAX - 9]}~{digest}"


def path_class(path: str | None) -> str:
    """Class of a finding at ``path`` (relative to its audited root): the first
    path segment, so ``sessions/2026/10/x.jsonl`` is ``sessions`` and a file
    at the root is its own name. Stable across runs and readable in a marker.
    Only a finding with no path has `REPO_CLASS`."""
    if not path:
        return REPO_CLASS
    first = path.strip("/").split("/", 1)[0]
    if not first or first == ".":
        return REPO_CLASS
    return _safe_class(first)


def finding_class(finding: dict) -> str:
    """The discriminator a triage marker carries after the rule (task #2157)."""
    return path_class(finding.get("path"))


def _notes_line_class(rest: str) -> str | None:
    """Class of one notes finding line, after its ``- [severity] `` prefix.

    The path is not escaped, so the split is a best effort that only ever
    errs toward a refile: a line number (``:12: ``) ends the path when there
    is one, so a ``": "`` inside a path segment is kept; otherwise the first
    ``": "`` does. ``(repo)`` here is how a path-less finding was rendered."""
    m = _LINE_NUMBER_SPLIT_RX.search(rest)
    if m:
        path = rest[: m.start()]
    else:
        cut = rest.find(": ")
        if cut <= 0:
            return None
        path = rest[:cut]
    return REPO_CLASS if path == REPO_CLASS else path_class(path)


def _filed_classes(task: dict) -> set[str] | None:
    """Classes a classless-marker task was filed for, read only from the block
    its filer wrote: the finding lines after the auto-filed header, at most
    as many as the title counts, ending at the first other line. A line
    appended later is not part of it, so it cannot widen the task. ``None``
    when the notes have no auto-filed header (a task written by hand)."""
    lines = (task.get("notes") or "").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(_AUTO_FILED_HEADER)), None)
    if start is None:
        return None
    count = _TITLE_COUNT_RX.search(task.get("title") or "")
    remaining = int(count.group("n")) if count else None
    classes: set[str] = set()
    body = lines[start + 1 :]
    while body and not body[0].strip():
        body = body[1:]
    for line in body:
        if remaining == 0:
            break
        if line.startswith(_NOTES_FIX_PREFIX):
            continue
        m = _NOTES_FINDING_RX.match(line)
        if not m:
            break
        cls = _notes_line_class(m.group("rest"))
        if cls is not None:
            classes.add(cls)
        if remaining is not None:
            remaining -= 1
    return classes


def _covers(task: dict, cls: str) -> bool:
    """A classless (pre-#2157) marker task covers the classes of the findings
    it was filed with. A hand-written one (no auto-filed block) keeps its old
    meaning and covers every class."""
    filed = _filed_classes(task)
    return filed is None or cls in filed


def _tracking_tasks(marker: str, *, honor_accepted: bool = True) -> list[dict]:
    """Tasks carrying ``marker`` that still own their group: open ones, plus
    closed ones the operator tagged ``[accepted-risk]`` unless the group's
    value rotated (``honor_accepted=False``). Full rows, because compact
    listings drop the notes the tag lives in."""
    from ..tools.tasks import task_list_all

    # notes_contain is case-insensitive; markers are generated, so require the
    # exact spelling (classes `README.md` and `readme.md` are different files).
    return [
        t
        for t in task_list_all(status="all", notes_contain=marker, fields="full")
        if marker in (t.get("notes") or "")
        and (
            t["status"] not in _CLOSED_STATUSES
            or (honor_accepted and ACCEPTED_RISK_TAG in (t.get("notes") or ""))
        )
    ]


def _marker(scope: str | None, key: str, cls: str) -> str:
    return f"[audit-triage: {scope + ':' if scope else ''}{key}:{cls}]"


def _already_tracked(
    key: str, cls: str, root: str | None, scope: str | None, *, honor_accepted: bool = True
) -> bool:
    def tracking(marker: str) -> list[dict]:
        return _tracking_tasks(marker, honor_accepted=honor_accepted)

    if tracking(_marker(scope, key, cls)):
        return True
    # Classless markers (before #2157) cover the classes their notes list.
    # The substring search cannot confuse the forms: a classless marker ends
    # in "<key>]" and a classed one in "<key>:<class>]".
    if scope is None:
        return any(_covers(t, cls) for t in tracking(f"[audit-triage: {key}]"))
    if any(_covers(t, cls) for t in tracking(f"[audit-triage: {scope}:{key}]")):
        return True
    # Legacy rule-only marker (before #1630): counts only for the root its
    # notes were filed against, so it cannot suppress the rule on another root.
    filed_for = f"Auto-filed from security_audit over {root} ("
    return any(
        filed_for in (t.get("notes") or "") and _covers(t, cls)
        for t in tracking(f"[audit-triage: {key}]")
    )


def file_findings_as_tasks(
    report: dict,
    *,
    min_severity: str = "high",
    dry_run: bool = False,
) -> dict:
    """File one triage task per group of findings at/above ``min_severity``.

    Files NOTHING when the (filtered) finding list is empty. Returns
    ``{status, files_scanned, groups, created, skipped, tasks, dry_run}``.
    """
    if min_severity not in SEVERITIES:
        raise ValueError(f"min_severity must be one of {', '.join(SEVERITIES)}")
    cutoff = _SEV_RANK[min_severity]
    findings = [
        fd for fd in report.get("findings") or () if _SEV_RANK.get(fd.get("severity"), 99) <= cutoff
    ]

    groups: dict[tuple[str, str], list[dict]] = {}
    for finding in findings:
        groups.setdefault((group_key(finding), finding_class(finding)), []).append(finding)

    from ..tools.tasks import task_add

    root = report.get("root")
    label = root_label(root)
    scope = root_key(root)
    created: list[dict] = []
    skipped: list[str] = []
    rotated_paths = set((report.get("baseline") or {}).get("rotated") or ())
    for key, cls in sorted(groups):
        group = groups[(key, cls)]
        rotated = any(f.get("path") in rotated_paths for f in group)
        if _already_tracked(key, cls, root, scope, honor_accepted=not rotated):
            skipped.append(f"{key}:{cls}")
            continue

        marker = _marker(scope, key, cls)
        title = (
            f"[Security Audit] {label + ': ' if label else ''}{key} ({cls})"
            f" — {len(group)} finding(s)"
        )
        lines = [
            f"- [{f.get('severity')}] {f.get('path') or '(repo)'}"
            f"{':' + str(f['line']) if f.get('line') else ''}: {f.get('message')}"
            + (f"\n  fix: {f['fix']}" if f.get("fix") else "")
            for f in group
        ]
        notes = (
            f"{marker}\n\nAuto-filed from security_audit over {root} "
            f"(min_severity={min_severity}).\n\n" + "\n".join(lines)
        )
        if rotated:
            notes += (
                "\n\nThe operator baseline holds an older value for this file and this "
                "value is not in it, so the gate fails whatever an [accepted-risk] task "
                "says. Review it, then fix it or run `unified-agent-audit baseline accept`."
            )
        entry = {"title": title, "group": key, "class": cls, "findings": len(group)}
        if dry_run:
            created.append({"id": None, **entry})
            continue
        # Med + default (not high): auto-filed audit rows are triage candidates,
        # not page-now incidents. A dedicated `triage` system lane is deferred —
        # `default` keeps them claimable in the open pool without flooding high.
        added = task_add(title=title, notes=notes, priority="med", lane="default")
        created.append({"id": added["id"], **entry})

    return {
        "status": report.get("status"),
        "files_scanned": report.get("files_scanned"),
        "groups": len(groups),
        "created": len(created),
        "skipped": skipped,
        "tasks": created,
        "dry_run": dry_run,
    }
