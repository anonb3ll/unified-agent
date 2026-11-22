"""Keep unified-agent's self-description platform-framed (UA #1074).

Every reader — and every connected agent, which receives the MCP
``instructions`` string at initialize — is taught what unified-agent is by a
handful of surfaces: the server instructions, the README opening, the
``AGENTS.md`` opening, the ua-sync persona template injected into every agent
workspace, the CLI reference opening, and the conductor product card. Before
#1074 they all opened with
"tool server" framing, so an agent onboarding cold learned that the project is
an MCP tool server and never learned there is a platform behind it: an
authority store (identity, policy, approvals), domain objects, an evidence
ledger, executors, and a web control center, with MCP one adapter among several.

Prose fixups rot (the 2026-08-12 assessment diagnosed exactly this and named
the cure: widen the doc-assertion tests rather than hand-fix prose again). This
file is that enforcement, in the same family as ``test_docs_tool_count.py`` and
``test_docs_decisions.py``: it asserts the frame on the five surfaces so a
regression to tool-server self-description fails CI instead of silently
re-teaching every agent the wrong model.
"""

import re
from pathlib import Path

from export_guard import needs_exported_paths

from unified_agent import server

_REPO = Path(__file__).parent.parent

# The framing the platform is NOT. "tool server" covers "MCP tool server" and
# "Cross-IDE tool server"; "is an MCP server" covers README's old opening
# "is an MCP server for an operator". Neutral operational references ("the
# host's MCP server config", "Starts the MCP server") name the adapter
# instance and never appear on these self-describing surfaces.
_FORBIDDEN = (
    "tool server",
    "is an MCP server",
    "are an MCP server",
)

# What the frame must teach: an authority store, an approval gate, an evidence
# ledger, and a control center all in one breath.
_PLATFORM_MARKERS = ("authority", "approval", "evidence", "control center")


def _assert_platform_surface(name: str, text: str) -> None:
    lower = text.lower()
    for phrase in _FORBIDDEN:
        assert phrase not in lower, (
            f"{name} regressed to tool-server framing ({phrase!r} found). "
            "unified-agent is a platform whose MCP surface is one adapter among "
            "several — fix the self-description, not this test."
        )
    for marker in _PLATFORM_MARKERS:
        assert marker in lower, (
            f"{name} no longer teaches the platform frame (missing {marker!r}). "
            "The surface must state the authority store, the approval gate, the "
            "evidence ledger, and the control center."
        )


def test_mcp_instructions_describe_the_platform() -> None:
    _assert_platform_surface("server MCP instructions", server.MCP_INSTRUCTIONS)
    assert "adapter" in server.MCP_INSTRUCTIONS.lower(), (
        "the instructions must name MCP as one adapter among several"
    )
    assert re.search(r"\bMCP\b", server.MCP_INSTRUCTIONS)


def test_mcp_initialize_serves_the_asserted_instructions() -> None:
    assert server.mcp.instructions == server.MCP_INSTRUCTIONS, (
        "the MCPServer is not wired to MCP_INSTRUCTIONS — the assertion above "
        "pins a copy, not what initialize serves"
    )


def test_readme_opening_describes_the_platform() -> None:
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    opening = readme.split("## Start here", 1)[0]
    _assert_platform_surface("README.md opening", opening)
    assert "adapter" in opening.lower()


@needs_exported_paths(_REPO / "AGENTS.md", repo=_REPO)
def test_agents_md_opening_describes_the_platform() -> None:
    agents = (_REPO / "AGENTS.md").read_text(encoding="utf-8")
    opening = agents.split("## Tool catalog", 1)[0]
    _assert_platform_surface("AGENTS.md opening", opening)
    assert "adapter" in opening.lower()


def test_cli_reference_opening_describes_the_platform() -> None:
    cli = (_REPO / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    opening = cli.split("`pyproject.toml` installs", 1)[0]
    _assert_platform_surface("docs/cli-reference.md opening", opening)
    assert "adapter" in opening.lower()


def test_sync_persona_template_describes_the_platform() -> None:
    from unified_agent.sync_cli import generate_rules_markdown

    block = generate_rules_markdown(
        role="Desktop_Test",
        location="Desktop",
        client_name="test-client",
        project_name="demo-project",
        tasks=[],
        memories=[],
    )
    _assert_platform_surface("ua-sync persona template", block)
    assert "adapter" in block.lower() and re.search(r"\bMCP\b", block), (
        "the ua-sync persona template must describe the platform and name the "
        "agent's place in it via MCP-as-one-adapter"
    )
    assert "unified-agent" in block.lower()


@needs_exported_paths(_REPO / "conductor" / "product.md", repo=_REPO)
def test_conductor_product_card_describes_the_platform() -> None:
    product = (_REPO / "conductor" / "product.md").read_text(encoding="utf-8")
    _assert_platform_surface("conductor/product.md", product)
    assert "adapter" in product.lower()


def test_ready_is_ci_green_and_the_ledger() -> None:
    """Merge policy: ready is CI green on the head, and the Ledger."""
    automation = (_REPO / "docs" / "automation.md").read_text(encoding="utf-8")
    collab = (_REPO / "docs" / "COLLABORATION.md").read_text(encoding="utf-8")
    for name, text in (("docs/automation.md", automation), ("docs/COLLABORATION.md", collab)):
        assert "coderabbit" not in text.lower(), name
        lowered = text.lower()
        assert "devin review is clean on that exact head" not in lowered, name
        assert "ci is green" in lowered, name
    ready = automation.lower()
    assert "ci is green on that head" in ready
    assert "approval ledger has approved that head" in ready
    src_hits = sorted(
        str(path.relative_to(_REPO))
        for path in (_REPO / "src").rglob("*.py")
        if "coderabbit" in path.read_text(encoding="utf-8").lower()
    )
    assert src_hits == ["src/unified_agent/held_pr.py"]
