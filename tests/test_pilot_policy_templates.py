"""Pilot policy template packs (task #1280, H4) load through the real

work-product loader so they cannot silently rot out of sync with the schema
``unified_agent.tools.work_product.load_template_pack`` actually enforces.
Each pack lives under ``docs/pilot/policy-templates/<vertical>/`` as a
``<vertical>.yaml`` (the loader requires ``id`` to match the filename stem,
same rule the shipped fixture packs under
``src/unified_agent/templates/work_product/`` follow) plus a ``README.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from unified_agent.tools import work_product

PILOT_TEMPLATES_ROOT = (
    Path(__file__).resolve().parent.parent / "docs" / "pilot" / "policy-templates"
)

# vertical id -> expected reviewer_role / required_tags, per the pack's own YAML.
VERTICALS: dict[str, dict[str, object]] = {
    "professional-services": {
        "reviewer_role": "engagement_lead",
        "required_tags": {"client", "engagement"},
    },
    "accounting-tax": {
        "reviewer_role": "reviewing_cpa",
        "required_tags": {"client", "engagement"},
    },
    "healthcare-admin": {
        "reviewer_role": "compliance_lead",
        "required_tags": {"client", "engagement"},
    },
    "legal": {
        "reviewer_role": "supervising_attorney",
        "required_tags": {"matter", "client"},
    },
}


def _pack_dir(vertical: str) -> Path:
    return PILOT_TEMPLATES_ROOT / vertical


def test_all_required_verticals_present():
    present = {p.name for p in PILOT_TEMPLATES_ROOT.iterdir() if p.is_dir()}
    assert present >= set(VERTICALS)


@pytest.mark.parametrize("vertical", sorted(VERTICALS))
def test_pilot_policy_template_loads_through_the_real_loader(vertical, monkeypatch):
    """Every shipped pack must parse with zero ToolErrors through the exact

    function ``work_product_submit`` calls at runtime — not a hand-rolled
    YAML sanity check.
    """
    pack_dir = _pack_dir(vertical)
    yaml_path = pack_dir / f"{vertical}.yaml"
    readme_path = pack_dir / "README.md"
    assert yaml_path.is_file(), f"missing template pack YAML for {vertical}"
    assert readme_path.is_file(), f"missing README for {vertical}"

    monkeypatch.setenv("UA_WORK_PRODUCT_TEMPLATE_DIR", str(pack_dir))

    pack = work_product.load_template_pack(vertical)

    assert pack.id == vertical
    assert pack.reviewer_role == VERTICALS[vertical]["reviewer_role"]
    assert set(pack.required_tags) == VERTICALS[vertical]["required_tags"]
    # release_subfolder is validated by the loader itself (_SEGMENT_RE); a
    # non-empty value here means it passed that check.
    assert pack.release_subfolder
    assert pack.title
    assert pack.description

    # list_template_packs() (used by the `unified-agent work-product packs`
    # CLI and the packs MCP surface) must also see exactly this one pack when
    # scoped to this directory, and it must round-trip through to_dict().
    listed = {p["id"]: p for p in work_product.list_template_packs()}
    assert listed == {vertical: pack.to_dict()}


@pytest.mark.parametrize("vertical", sorted(VERTICALS))
def test_pilot_policy_template_two_person_rule_is_documented_not_enforced(vertical):
    """The optional two-person rule is descriptive metadata today (the

    loader has no field for a second approver, and ``work_product.py``
    itself describes ``reviewer_role`` the same way until the role registry,
    #1289, binds roles to identities — see docs/work-product-release.md).
    Every pack must declare the block with an explicit boolean, and every
    README must say plainly that it is not automatically enforced, so a
    pilot operator never mistakes it for a platform-enforced control.
    """
    pack_dir = _pack_dir(vertical)
    raw = yaml.safe_load((pack_dir / f"{vertical}.yaml").read_text(encoding="utf-8"))

    assert "two_person_rule" in raw, f"{vertical}: two_person_rule block missing"
    rule = raw["two_person_rule"]
    assert isinstance(rule, dict)
    assert isinstance(rule.get("enabled"), bool)
    if rule["enabled"]:
        assert rule.get("second_reviewer_role"), f"{vertical}: enabled but no second reviewer"

    # A pack with two_person_rule metadata must still load cleanly through
    # the real loader — the extra key is ignored, not rejected (fields the
    # loader doesn't know about are simply never read; see
    # work_product._load_pack_file).
    readme_text = (pack_dir / "README.md").read_text(encoding="utf-8")
    lowered = readme_text.lower()
    assert "not" in lowered and "enforce" in lowered, (
        f"{vertical}: README must explicitly say the two-person rule is not "
        "enforced by the platform yet"
    )


def test_no_pack_shares_a_vertical_word_it_should_not(tmp_path, monkeypatch):
    """Sanity check that each pack's id matches its own filename/dir name —

    the same invariant load_template_pack() enforces per-file — across the
    whole set in one pass, so a copy/paste error (e.g. two packs both
    declaring id: legal in different directories) fails loudly here instead
    of surfacing only when an operator picks the wrong one.
    """
    seen_ids: dict[str, str] = {}
    for vertical in VERTICALS:
        pack_dir = _pack_dir(vertical)
        monkeypatch.setenv("UA_WORK_PRODUCT_TEMPLATE_DIR", str(pack_dir))
        pack = work_product.load_template_pack(vertical)
        assert pack.id == vertical
        seen_ids[vertical] = pack.id
    assert len(seen_ids) == len(VERTICALS)
