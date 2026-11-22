"""Keep `docs/decisions.md` and the Gate F re-inventory from rotting.

`docs/decisions.md` exists because decisions kept being made where no agent
could see them. It routes each kind of decision to the channel that actually
reads it, and carries an open-decision register naming the document each gate
closes in. Both parts are claims about the tree, and both go stale silently —
which is the exact failure the page was written to stop. So they are asserted
here rather than trusted.

The Gate F check is the pointed one. `docs/gate-f-retirement-proposal.md` §7
records that `c5368f0` already shipped §4's whole removal scope, verified on
2026-08-16. If any of those symbols is ever reintroduced, that section becomes
false and a decision would be taken against a document that misdescribes the
tree — the same failure the assessment caught the first time. Then this test
fails instead.
"""

import re
from pathlib import Path

_REPO = Path(__file__).parent.parent
_DOCS = _REPO / "docs"
_DECISIONS = _DOCS / "decisions.md"

# Removed wholesale by c5368f0 (Gate F, task #352, PR #158). `is_laptop` is
# matched as a definition/attribute rather than a bare word: `webui/api.py`
# legitimately uses `is_laptop` as a local variable, and that is not this.
_RETIRED_SYMBOLS = (
    r"def is_laptop\b",
    r"config\.is_laptop\b",
    r"\bvault_inbox_dir\b",
    r"\bvault_journal_dir\b",
    r"\b_laptop_topology_check\b",
)

# The Keep list from the same proposal. Deleting these would over-execute the
# gate, which §7 explicitly warns against.
_RETAINED_SYMBOLS = (
    "state_sync_dir",
    "state_mirror_name",
)


def test_decisions_page_links_resolve():
    """Every relative link in the register points at a file that exists."""
    text = _DECISIONS.read_text()
    links = re.findall(r"\]\(([^)#]+\.md)[^)]*\)", text)
    assert links, "decisions.md has no document links — the register lost its targets"
    missing = sorted({link for link in links if not (_DOCS / link).exists()})
    assert not missing, (
        "docs/decisions.md points at documents that do not exist: "
        + ", ".join(missing)
        + ". Update the register to name the document a decision now lands in, "
        "rather than deleting the row."
    )


def test_decisions_page_names_the_gate_documents():
    """The register must keep naming where each open decision is recorded."""
    text = _DECISIONS.read_text()
    for doc in (
        "s5-bounded-autonomy-acceptance-gates.md",
        "gate-f-retirement-proposal.md",
        "deploy-boundary-proposal.md",
    ):
        assert doc in text, (
            f"docs/decisions.md no longer names {doc}. If that gate closed, replace "
            "its row with the decision and its date — do not drop it silently."
        )


def test_gate_f_removed_symbols_stay_removed():
    """§7's claim that c5368f0 shipped the §4 removal scope must stay true."""
    sources = [p for p in (_REPO / "src").rglob("*.py")]
    assert sources, "no sources found — the glob broke, not the tree"
    for pattern in _RETIRED_SYMBOLS:
        hits = sorted(
            f"{p.relative_to(_REPO)}"
            for p in sources
            if re.search(pattern, p.read_text(encoding="utf-8", errors="replace"))
        )
        assert not hits, (
            f"{pattern!r} is back in {', '.join(hits)}, but "
            "docs/gate-f-retirement-proposal.md §7 records it as removed by c5368f0. "
            "Reintroducing it reopens Gate F — update §7 deliberately if that is intended."
        )


def test_gate_f_retained_symbols_stay_present():
    """§4's Keep list is not collateral damage of the gate."""
    from unified_agent import config

    for symbol in _RETAINED_SYMBOLS:
        assert callable(getattr(config, symbol, None)), (
            f"{symbol!r} is gone from config.py, but it is on Gate F's explicit Keep "
            "list — hostname-scoped state mirroring is not part of the laptop "
            "namespacing retirement (see the proposal §1 and §7)."
        )
