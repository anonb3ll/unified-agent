"""Verbatim excerpt of unified_brain ``scripts/brain.py`` (commit e5d96d6).

Test fixture only. It holds, unedited, the definitions ``brain.py reply`` uses
to write a thread file, so tests can check that the hub's in-process writer
(``unified_agent.messaging.brain_format``) produces the same bytes. Two things
are not upstream code: ``write_index`` is a no-op (``INBOX.md`` is out of scope
for the bridge) and ``main`` registers only the ``reply`` subcommand, with the
same arguments as upstream. Refresh by re-extracting these definitions.
Formatting is upstream's (``# fmt: off``).
"""

# fmt: off
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
THREADS = REPO / "threads"
ROSTER = REPO / "roster.json"

STATUSES = ("open", "claimed", "waiting", "answered", "done", "cancelled")


FIELDS = (
    "id",
    "type",
    "title",
    "from",
    "to",
    "cc",
    "status",
    "priority",
    "created",
    "updated",
    "claimed_by",
    "waiting_on",
    "hub_task",
    "refs",
    "replies",
)


LIST_FIELDS = ("to", "cc", "refs")


GROUPS = ("operator", "local", "cloud", "any")


SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.@/-]+$")


REPLY_RE = re.compile(r"^## Reply\s+[—–-]\s+(?P<label>[^,]+),\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$")


PLACEHOLDER = "<!-- Write your reply here. -->"


def reply_headings(body: str) -> list[tuple[str, str]]:
    """(label, date) for every well-formed '## Reply — <label>, <date>' heading, in order."""
    out = []
    for line in body.splitlines():
        m = REPLY_RE.match(line)
        if m:
            out.append((m.group("label").strip(), m.group("date")))
    return out


class BrainError(Exception):
    pass


def load_roster() -> dict:
    data = json.loads(ROSTER.read_text(encoding="utf-8"))
    members = {}
    aliases = {}
    for m in data["members"]:
        label = m["label"]
        if m.get("where") not in ("operator", "local", "cloud"):
            raise BrainError(f"roster: {label} has invalid 'where' {m.get('where')!r}")
        members[label] = m
        for alias in m.get("aliases", []):
            aliases[alias] = label
    return {"members": members, "aliases": aliases}


def canonical(label: str, roster: dict) -> str | None:
    if label in roster["members"]:
        return label
    return roster["aliases"].get(label)


def known_target(target: str, roster: dict) -> bool:
    return target in GROUPS or canonical(target, roster) is not None


def _parse_value(raw: str):
    raw = raw.strip()
    if raw in ("", "null", "~"):
        return None
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith('"'):
        return json.loads(raw)
    if raw.startswith("["):
        try:
            return [str(x) for x in json.loads(raw)]
        except json.JSONDecodeError:
            inner = raw[1:-1].strip() if raw.endswith("]") else raw[1:]
            return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
    return raw


def _format_value(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if all(SAFE_TOKEN.match(str(v)) for v in value):
            return "[" + ", ".join(str(v) for v in value) + "]"
        return json.dumps([str(v) for v in value], ensure_ascii=False)
    text = str(value)
    return text if SAFE_TOKEN.match(text) else json.dumps(text, ensure_ascii=False)


def parse_thread(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise BrainError(f"{path.name}: must start with a '---' frontmatter line")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise BrainError(f"{path.name}: frontmatter is not closed with '---'")
    meta: dict = {}
    for n, line in enumerate(text[4:end].splitlines(), start=2):
        if not line.strip():
            continue
        if ":" not in line:
            raise BrainError(f"{path.name}:{n}: expected 'key: value'")
        key, raw = line.split(":", 1)
        key = key.strip()
        if key in meta:
            raise BrainError(f"{path.name}:{n}: duplicate key {key!r}")
        try:
            meta[key] = _parse_value(raw)
        except json.JSONDecodeError as exc:
            raise BrainError(f"{path.name}:{n}: bad value for {key!r}: {exc}") from exc
    for key in LIST_FIELDS:
        if isinstance(meta.get(key), str):
            meta[key] = [meta[key]]
        meta.setdefault(key, [])
    body = text[end + len("\n---\n") :]
    return meta, body


def write_thread(path: Path, meta: dict, body: str) -> None:
    lines = ["---"]
    for key in FIELDS:
        if key in meta and not (key in ("cc", "refs") and not meta[key]):
            lines.append(f"{key}: {_format_value(meta[key])}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n\n" + body.lstrip("\n").rstrip() + "\n", encoding="utf-8")


def thread_path(thread_id: str) -> Path:
    path = THREADS / f"{thread_id}.md"
    if not path.exists():
        raise BrainError(f"no thread {thread_id!r} (looked for {path.relative_to(REPO)})")
    return path


def today() -> str:
    return _dt.datetime.now(_dt.UTC).date().isoformat()


def _require_label(label: str, roster: dict, what: str) -> str:
    c = canonical(label, roster)
    if c is None:
        raise BrainError(f"{what} '{label}' is not in roster.json; add it in a PR first")
    return c


def _touch(meta: dict) -> None:
    meta["updated"] = max(str(meta.get("updated") or ""), today())


def cmd_reply(args, roster) -> int:
    path = thread_path(args.thread)
    meta, body = parse_thread(path)
    sender = _require_label(args.sender, roster, "--from")
    text = args.message
    if args.message_file:
        text = Path(args.message_file).read_text(encoding="utf-8")
    block = f"\n\n## Reply — {sender}, {today()}\n\n{(text or PLACEHOLDER).strip()}\n"
    body = body.rstrip() + block
    if args.status:
        _apply_status(meta, args.status, sender, args.waiting_on, roster)
    meta["replies"] = len(reply_headings(body))
    _touch(meta)
    write_thread(path, meta, body)
    write_index(roster)
    print(path.relative_to(REPO))
    if not text:
        print(f"Replace the placeholder in {path.name}, then run check.", file=sys.stderr)
    return 0


def _apply_status(meta: dict, status: str, actor: str, waiting_on: str | None, roster) -> None:
    if status not in STATUSES:
        raise BrainError(f"status must be one of {', '.join(STATUSES)}")
    meta["status"] = status
    if status == "claimed":
        meta["claimed_by"] = actor
    if status == "waiting":
        if not waiting_on:
            raise BrainError("status 'waiting' needs --waiting-on <label or group>")
        if not known_target(waiting_on, roster):
            raise BrainError(f"'{waiting_on}' is not a roster label or group")
        meta["waiting_on"] = waiting_on
    elif status != "waiting":
        meta["waiting_on"] = None

def write_index(roster) -> None:  # not upstream: INBOX.md is out of scope here
    return None


def main(argv=None) -> int:  # not upstream: only the reply subcommand
    p = argparse.ArgumentParser(prog="brain.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("reply")
    s.add_argument("thread")
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--message")
    s.add_argument("--message-file")
    s.add_argument("--status", choices=STATUSES)
    s.add_argument("--waiting-on")
    s.set_defaults(fn=cmd_reply)
    args = p.parse_args(argv)
    try:
        return args.fn(args, load_roster())
    except BrainError as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
