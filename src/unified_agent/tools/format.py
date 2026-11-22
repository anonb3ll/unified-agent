"""Chat-formatting primitives that always carry a plain-text fallback.

Every renderer returns {"markdown": ..., "text": ...}. The iMessage bridge
strips markup, so the text form must stand on its own — nothing important may
live only in the markdown. The library functions are importable for in-process
use; `format_render` is the single MCP surface over them.
"""

from mcp.server.mcpserver.exceptions import ToolError


def _cell(value) -> str:
    return "" if value is None else str(value)


def format_table(columns: list[str], rows: list[list]) -> dict:
    """A markdown table + an aligned plain-text table.

    Rows are padded/truncated to the column count so a ragged input never
    produces a malformed table.
    """
    if not columns:
        raise ToolError("format_table needs at least one column")
    width = len(columns)
    norm = []
    for r in rows:
        cells = [_cell(c) for c in r][:width]
        cells += [""] * (width - len(cells))
        norm.append(cells)

    md = [
        "| " + " | ".join(_cell(c).replace("|", "\\|") for c in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for cells in norm:
        md.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")

    widths = [len(_cell(columns[i])) for i in range(width)]
    for cells in norm:
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c.replace("\n", " ")))
    txt_lines = ["  ".join(_cell(columns[i]).ljust(widths[i]) for i in range(width)).rstrip()]
    txt_lines.append("  ".join("-" * widths[i] for i in range(width)))
    for cells in norm:
        txt_lines.append(
            "  ".join(c.replace("\n", " ").ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
        )
    return {"markdown": "\n".join(md), "text": "\n".join(txt_lines)}


def format_section(summary: str, body: str) -> dict:
    """A collapsible <details> section in markdown; a labeled block in text."""
    if not summary.strip():
        raise ToolError("format_section needs a non-empty summary")
    md = f"<details>\n<summary>{summary}</summary>\n\n{body}\n</details>"
    text = f"{summary}\n{'-' * len(summary)}\n{body}"
    return {"markdown": md, "text": text}


def format_code(content: str, filename: str | None = None) -> dict:
    """A fenced code block (with an optional filename header) + plain fallback."""
    header = f"`{filename}`\n" if filename else ""
    lang = ""
    if filename and "." in filename:
        lang = {
            "py": "python",
            "js": "javascript",
            "ts": "typescript",
            "sh": "bash",
            "json": "json",
            "md": "markdown",
            "yaml": "yaml",
            "yml": "yaml",
        }.get(filename.rsplit(".", 1)[-1], "")
    md = f"{header}```{lang}\n{content}\n```"
    text = (f"{filename}:\n" if filename else "") + content
    return {"markdown": md, "text": text}


def format_chips(mapping: dict) -> dict:
    """Inline status chips (`key: value`) in markdown; `key=value` list in text."""
    if not mapping:
        raise ToolError("format_chips needs at least one key/value pair")
    md = " ".join(f"`{k}: {_cell(v)}`" for k, v in mapping.items())
    text = " · ".join(f"{k}={_cell(v)}" for k, v in mapping.items())
    return {"markdown": md, "text": text}


def format_render(kind: str, payload: dict) -> dict:
    """Render chat output with an automatic plain-text fallback.

    `kind` is 'table' | 'section' | 'code' | 'chips'; `payload` carries that
    renderer's arguments:
      - table:   {columns: [str], rows: [[…]]}
      - section: {summary: str, body: str}
      - code:    {content: str, filename?: str}
      - chips:   {mapping: {k: v}}
    Returns {markdown, text}. The text form is self-contained for the
    markup-stripping iMessage path.
    """
    if not isinstance(payload, dict):
        raise ToolError("payload must be an object")
    try:
        if kind == "table":
            return format_table(payload["columns"], payload.get("rows", []))
        if kind == "section":
            return format_section(payload["summary"], payload.get("body", ""))
        if kind == "code":
            return format_code(payload["content"], payload.get("filename"))
        if kind == "chips":
            return format_chips(payload["mapping"])
    except KeyError as e:
        raise ToolError(f"payload for kind={kind!r} is missing required key {e}") from None
    raise ToolError(f"kind must be one of table|section|code|chips, got {kind!r}")
