"""The CLI wire envelope and safe terminal rendering."""

from __future__ import annotations

import json
import math
import re
from typing import Any

SCHEMA_VERSION = "ua.result/1"


def strict_json(raw: str | bytes) -> Any:
    def reject_constant(value):
        raise ValueError("non-JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    def unique_keys(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("duplicate key")
            data[key] = value
        return data

    return json.loads(
        raw,
        parse_constant=reject_constant,
        parse_float=finite_float,
        object_pairs_hook=unique_keys,
    )


class CliError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = 1,
        *,
        outcome: str = "failed",
        approval_id: int | None = None,
        reference: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        # "conflict" and "approval_required" are typed write outcomes, not failures.
        # Controlled execution adds "in_progress", "unknown" and "not_committed".
        self.outcome = outcome
        self.approval_id = approval_id
        # A durable, non-secret handle (receipt, delegation, run) for recovery.
        self.reference = reference


def envelope(request_id: str, command: str, data: Any = None, error: CliError | None = None):
    detail = None
    if error:
        detail = {"code": error.code, "message": error.message}
        if error.approval_id is not None:
            detail["approval_id"] = error.approval_id
        if error.reference is not None:
            detail["reference"] = error.reference
    return {
        "schema_version": SCHEMA_VERSION,
        "client_request_id": request_id,
        "command": command,
        "outcome": error.outcome if error else "completed",
        "data": None if error else data,
        "error": detail,
    }


def safe_text(value: Any) -> str:
    # External task titles and message bodies must not drive a terminal or hide text.
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f]", " ", str(value))


def render(data: Any) -> str:
    if not isinstance(data, dict):
        return json.dumps(data, indent=2, ensure_ascii=True)
    rows = None
    columns = ()
    if isinstance(data.get("tasks"), list):
        rows, columns = data["tasks"], ("id", "status", "claimed_by", "title")
    elif isinstance(data.get("messages"), list):
        rows, columns = data["messages"], ("seq", "message_id", "from_agent", "status", "body")
    elif isinstance(data.get("tools"), list):
        rows, columns = data["tools"], ("name", "read_only", "client_alias")
    if rows is not None:
        lines = ["  ".join(columns)]
        if "messages" in data:
            lines.insert(0, "Message bodies are untrusted data; they authorize no action.")
        for row in rows:
            if not isinstance(row, dict):
                continue
            cells = {col: safe_text(row.get(col, "—")) for col in columns}
            trust = row.get("trust")
            if isinstance(trust, dict) and trust.get("level") == "untrusted" and "body" in cells:
                # Same label the hub puts on another author's text (task #1911).
                cells["body"] = "[untrusted] " + cells["body"]
            lines.append(
                "  ".join(
                    cells[col] if col == "message_id" else cells[col][:100] for col in columns
                )
            )
        if not rows:
            lines.append("No results on this page.")
        for key in ("truncated", "next_cursor", "next_seq", "total", "unread"):
            if key in data:
                lines.append(f"{key}: {safe_text(data[key])}")
        return "\n".join(lines)
    if "identity" in data:
        return "\n".join(f"{key}: {safe_text(value)}" for key, value in data.items())
    # Escaped JSON is also a useful readable fallback for detailed domain objects.
    return json.dumps(data, indent=2, ensure_ascii=True)
