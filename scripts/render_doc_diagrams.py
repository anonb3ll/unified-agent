"""Render the README and docs diagrams as light and dark SVGs.

GitHub picks the variant that matches the reader's theme through a
``<picture>`` element, so every diagram is written twice from one layout. Edit
the layout here, then regenerate:

    uv run python scripts/render_doc_diagrams.py

Output lands in ``docs/assets/``. The SVGs are committed; this script is the
only thing that should write them.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets"
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"


@dataclass(frozen=True)
class Palette:
    text: str
    muted: str
    card: str
    panel: str
    border: str
    blue: str
    green: str
    purple: str
    orange: str
    blue_soft: str
    green_soft: str
    purple_soft: str
    orange_soft: str


LIGHT = Palette(
    text="#1f2328",
    muted="#59636e",
    card="#ffffff",
    panel="#f6f8fa",
    border="#d1d9e0",
    blue="#0969da",
    green="#1a7f37",
    purple="#8250df",
    orange="#bc4c00",
    blue_soft="#ddf4ff",
    green_soft="#dafbe1",
    purple_soft="#fbefff",
    orange_soft="#fff1e5",
)

DARK = Palette(
    text="#e6edf3",
    muted="#9198a1",
    card="#0d1117",
    panel="#151b23",
    border="#3d444d",
    blue="#4493f8",
    green="#3fb950",
    purple="#ab7df8",
    orange="#f0883e",
    blue_soft="#121d2f",
    green_soft="#12261e",
    purple_soft="#1e1631",
    orange_soft="#2a1b0f",
)


class Canvas:
    """A tiny SVG builder. Coordinates are absolute pixels."""

    def __init__(self, width: int, height: int, title: str, desc: str) -> None:
        self.width = width
        self.height = height
        self.parts: list[str] = []
        self.title = title
        self.desc = desc

    def rect(self, x, y, w, h, fill, stroke=None, rx=10, dash=None, sw=1.5) -> None:
        extra = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        if dash:
            extra += f' stroke-dasharray="{dash}"'
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{extra}/>'
        )

    def text(self, x, y, s, fill, size=14, weight=400, anchor="start", italic=False) -> None:
        style = ' font-style="italic"' if italic else ""
        self.parts.append(
            f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}"{style}>{escape(s)}</text>'
        )

    def line(self, x1, y1, x2, y2, stroke, sw=2, marker=None, dash=None) -> None:
        extra = f' marker-end="url(#{marker})"' if marker else ""
        if dash:
            extra += f' stroke-dasharray="{dash}"'
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
            f'stroke-width="{sw}" stroke-linecap="round"{extra}/>'
        )

    def arrowhead(self, name: str, color: str) -> None:
        self.parts.insert(
            0,
            f'<defs><marker id="{name}" viewBox="0 0 10 10" refX="8" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker></defs>',
        )

    def chip(self, x, y, label, fg, bg, border, h=30, pad=14, size=13) -> int:
        """Draw a pill and return its width."""
        w = int(len(label) * size * 0.56) + pad * 2
        self.rect(x, y, w, h, bg, border, rx=h // 2, sw=1)
        self.text(
            x + w / 2, y + h / 2 + size * 0.36, label, fg, size=size, weight=500, anchor="middle"
        )
        return w

    def render(self) -> str:
        body = "\n  ".join(self.parts)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}" font-family="{FONT}" role="img" '
            f'aria-labelledby="t d">\n'
            f'  <title id="t">{escape(self.title)}</title>\n'
            f'  <desc id="d">{escape(self.desc)}</desc>\n'
            f"  {body}\n</svg>\n"
        )


def chip_row(c: Canvas, x: int, y: int, labels: list[str], fg, bg, border, gap=10, **kw) -> int:
    for label in labels:
        x += c.chip(x, y, label, fg, bg, border, **kw) + gap
    return x - gap


def centered_chip_row(
    c: Canvas, cx: float, y: int, labels, fg, bg, border, gap=10, size=13
) -> None:
    widths = [int(len(s) * size * 0.56) + 28 for s in labels]
    total = sum(widths) + gap * (len(labels) - 1)
    chip_row(c, int(cx - total / 2), y, labels, fg, bg, border, gap=gap, size=size)


def overview(p: Palette) -> str:
    c = Canvas(
        1200,
        760,
        "unified-agent: a meta harness for agents, devices, and people",
        "Humans approve and decide through the Control Center. Agents in any harness on "
        "any device connect to one hub through MCP, HTTP, CLI, or the SDK. The hub holds "
        "authority (identity, profiles, policy, approvals, autonomy modes), shared work "
        "(tasks, plans, delegations, runs, memory), and a hash-chained evidence ledger. "
        "Executors, device nodes, worktrees, and connectors carry out approved work.",
    )
    c.arrowhead("arrow", p.muted)

    # Top left: people.
    c.rect(20, 20, 380, 236, p.panel, p.border, rx=14)
    c.text(40, 50, "PEOPLE", p.orange, size=12, weight=700)
    c.text(40, 72, "Decide, approve, review", p.muted, size=13)
    centered_chip_row(
        c, 210, 96, ["Operator", "Reviewers & approvers"], p.orange, p.orange_soft, p.orange
    )
    centered_chip_row(
        c, 210, 136, ["Office leads", "Stakeholders"], p.orange, p.orange_soft, p.orange
    )
    c.text(210, 204, "Control Center · phone · chat", p.muted, size=13, anchor="middle")
    c.text(210, 226, "signed, attributable decisions", p.muted, size=13, anchor="middle")

    # Top right: agents and harnesses on devices.
    c.rect(420, 20, 760, 236, p.panel, p.border, rx=14)
    c.text(440, 50, "AGENTS & HARNESSES", p.purple, size=12, weight=700)
    c.text(440, 72, "Any harness, any device", p.muted, size=13)
    for y, row in (
        (96, ["Claude Code", "Codex", "Cursor", "Copilot", "Antigravity"]),
        (136, ["Hermes", "Devin", "OpenCode", "LangGraph", "ACP agents"]),
        (176, ["Your own agent"]),
    ):
        centered_chip_row(c, 800, y, row, p.purple, p.purple_soft, p.purple)
    c.text(
        800,
        236,
        "desktop · laptop · server · cloud VM",
        p.muted,
        size=13,
        anchor="middle",
        italic=True,
    )

    # Arrows into the hub.
    c.line(210, 258, 210, 290, p.muted, sw=2.5, marker="arrow")
    c.text(222, 280, "approve · decide · review", p.muted, size=12)
    c.line(800, 258, 800, 290, p.muted, sw=2.5, marker="arrow")
    c.text(812, 280, "one identity per agent · token or device login", p.muted, size=12)

    # Hub.
    c.rect(20, 296, 1160, 330, p.card, p.blue, rx=16, sw=2)
    c.text(40, 326, "unified-agent hub", p.text, size=18, weight=700)
    c.text(206, 326, "one authority store · one database · one policy", p.muted, size=13)

    adapters = ["MCP", "HTTP / JSON", "CLI", "Control Center", "TypeScript SDK"]
    c.text(40, 360, "Adapters", p.muted, size=12, weight=600)
    chip_row(c, 120, 342, adapters, p.blue, p.blue_soft, p.blue, size=13)

    cols = [
        (
            "Authority",
            p.orange,
            p.orange_soft,
            [
                "Identities, devices, offices",
                "Profiles (what each may call)",
                "Policy: allow · deny · approve",
                "Approval gate & separation of duties",
                "Autonomy modes & budgets",
            ],
        ),
        (
            "Shared work",
            p.blue,
            p.blue_soft,
            [
                "Tasks, claims & worktrees",
                "Plans, delegations & handoffs",
                "Runs, receipts & routines",
                "Memory & knowledge vault",
                "Workflows & goals",
            ],
        ),
        (
            "Evidence",
            p.green,
            p.green_soft,
            [
                "Hash-chained event ledger",
                "Signed producer events",
                "External chain anchors",
                "Evidence reports & exports",
                "Provenance graph",
            ],
        ),
    ]
    x = 40
    for title, fg, soft, items in cols:
        c.rect(x, 384, 360, 226, soft, fg, rx=12, sw=1.2)
        c.text(x + 18, 412, title, fg, size=15, weight=700)
        for i, item in enumerate(items):
            y = 444 + i * 34
            c.rect(x + 18, y - 15, 6, 6, fg, rx=3)
            c.text(x + 34, y - 7, item, p.text, size=14)
        x += 380

    # Arrow out of the hub.
    c.line(600, 626, 600, 656, p.muted, sw=2.5, marker="arrow")
    c.text(612, 648, "only approved, policy-checked work runs", p.muted, size=12)

    # Band 3: execution and integrations.
    c.rect(20, 662, 1160, 78, p.panel, p.border, rx=14)
    c.text(40, 692, "EXECUTION & INTEGRATIONS", p.green, size=12, weight=700)
    c.text(40, 714, "Where work happens", p.muted, size=13)
    centered_chip_row(
        c,
        740,
        686,
        [
            "Executors",
            "ua-node workers",
            "Git worktrees",
            "GitHub",
            "Slack",
            "Discord",
            "Obsidian",
            "LLM broker",
        ],
        p.green,
        p.green_soft,
        p.green,
        size=13,
        gap=8,
    )
    return c.render()


def evidence_chain(p: Palette) -> str:
    c = Canvas(
        1200,
        240,
        "The five-link accountability chain",
        "Every governed action is traced from the human who authorized it, through the "
        "device and the agent, to the action and its proof. Each link is recorded on a "
        "hash-chained ledger, signed by the producer, and can be anchored outside the hub.",
    )
    c.arrowhead("arrow", p.muted)
    links = [
        ("Human", "who authorized it", p.orange, p.orange_soft),
        ("Device", "which machine was used", p.orange, p.orange_soft),
        ("Agent", "which identity acted", p.purple, p.purple_soft),
        ("Action", "the task, tool, or release", p.blue, p.blue_soft),
        ("Proof", "PR, commit, hash, receipt", p.green, p.green_soft),
    ]
    w, gap, y = 196, 50, 30
    x = (1200 - (5 * w + 4 * gap)) // 2
    for i, (name, caption, fg, soft) in enumerate(links):
        c.rect(x, y, w, 100, soft, fg, rx=14, sw=1.8)
        c.text(x + w / 2, y + 44, name, fg, size=20, weight=700, anchor="middle")
        c.text(x + w / 2, y + 72, caption, p.text, size=13, anchor="middle")
        if i < len(links) - 1:
            c.line(x + w + 6, y + 50, x + w + gap - 6, y + 50, p.muted, sw=2.5, marker="arrow")
        x += w + gap

    c.rect(60, 160, 1080, 64, p.panel, p.border, rx=12)
    centered_chip_row(
        c,
        600,
        177,
        [
            "hash-chained ledger",
            "HMAC-signed producers",
            "TSA / git / vault anchors",
            "signed exports",
        ],
        p.green,
        p.green_soft,
        p.green,
        gap=14,
    )
    return c.render()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("overview", overview), ("evidence-chain", evidence_chain)):
        for theme, palette in (("light", LIGHT), ("dark", DARK)):
            path = OUT / f"{name}-{theme}.svg"
            path.write_text(build(palette), encoding="utf-8")
            print(f"wrote {path.relative_to(OUT.parent.parent)}")


if __name__ == "__main__":
    main()
