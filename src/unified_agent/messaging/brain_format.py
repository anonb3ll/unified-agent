"""The unified_brain thread file format, read and written in process (task #1863).

The brain repo's ``scripts/brain.py`` is the protocol's reference tool, but the
hub never runs it: that repo is not branch-protected, so executing a file from
it would hand code execution on the hub host to anyone who can push there. This
module mirrors exactly what ``brain.py reply`` writes to a thread file (the
frontmatter rendering, the reply block and the header fields it updates), and
``tests/test_messaging_bridges.py`` compares its output with a verbatim excerpt
of ``brain.py`` byte for byte. ``brain.py reply`` also regenerates ``INBOX.md``;
that stays with whoever commits the brain repo (``brain.py index``).

Pure functions over text: no file or hub access here.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any

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
STATUSES = ("open", "claimed", "waiting", "answered", "done", "cancelled")
CLOSED = ("done", "cancelled")
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.@/-]+$")
REPLY_RE = re.compile(r"^## Reply\s+[—–-]\s+(?P<label>[^,]+),\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$")
PLACEHOLDER = "<!-- Write your reply here. -->"


class ThreadFormatError(ValueError):
    """A thread file brain.py itself would refuse to parse."""


def today() -> str:
    return _dt.datetime.now(_dt.UTC).date().isoformat()


def reply_headings(body: str) -> list[tuple[str, str]]:
    """(label, date) for every well-formed reply heading, in order."""
    out = []
    for line in body.splitlines():
        match = REPLY_RE.match(line)
        if match:
            out.append((match.group("label").strip(), match.group("date")))
    return out


def parse_value(raw: str) -> Any:
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


def format_value(value: Any) -> str:
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


def split_thread(text: str, name: str) -> tuple[dict[str, Any], str]:
    """(meta, body), parsed as ``brain.py``'s ``parse_thread`` does, strictly."""
    if not text.startswith("---\n"):
        raise ThreadFormatError(f"{name}: must start with a '---' frontmatter line")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ThreadFormatError(f"{name}: frontmatter is not closed with '---'")
    meta: dict[str, Any] = {}
    for n, line in enumerate(text[4:end].splitlines(), start=2):
        if not line.strip():
            continue
        if ":" not in line:
            raise ThreadFormatError(f"{name}:{n}: expected 'key: value'")
        key, raw = line.split(":", 1)
        key = key.strip()
        if key in meta:
            raise ThreadFormatError(f"{name}:{n}: duplicate key {key!r}")
        try:
            meta[key] = parse_value(raw)
        except json.JSONDecodeError as exc:
            raise ThreadFormatError(f"{name}:{n}: bad value for {key!r}: {exc}") from exc
    for key in LIST_FIELDS:
        if isinstance(meta.get(key), str):
            meta[key] = [meta[key]]
        meta.setdefault(key, [])
    return meta, text[end + len("\n---\n") :]


def render_thread(meta: dict[str, Any], body: str) -> str:
    """The file text ``brain.py``'s ``write_thread`` produces."""
    lines = ["---"]
    for key in FIELDS:
        if key in meta and not (key in ("cc", "refs") and not meta[key]):
            lines.append(f"{key}: {format_value(meta[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.lstrip("\n").rstrip() + "\n"


def append_reply(
    text: str,
    name: str,
    *,
    author: str,
    message: str,
    status: str | None = None,
    date: str | None = None,
) -> str:
    """The thread text after ``brain.py reply <id> --from <author> [--status S]``.

    ``author`` must already be a canonical roster label. Only the statuses this
    bridge sets are supported (not ``waiting``, which needs ``--waiting-on``).
    """
    if status is not None and (status not in STATUSES or status == "waiting"):
        raise ThreadFormatError(f"unsupported status {status!r}")
    meta, body = split_thread(text, name)
    day = date or today()
    block = f"\n\n## Reply — {author}, {day}\n\n{(message or PLACEHOLDER).strip()}\n"
    body = body.rstrip() + block
    if status:
        meta["status"] = status
        if status == "claimed":
            meta["claimed_by"] = author
        meta["waiting_on"] = None
    meta["replies"] = len(reply_headings(body))
    meta["updated"] = max(str(meta.get("updated") or ""), day)
    return render_thread(meta, body)
