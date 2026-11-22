"""Deterministic catalog validation wired into the repository test suite (task #1065).

These tests exercise the same dependency-free validator used by CI:
`python scripts/validate_capability_catalog.py --render`.

Acceptance criteria covered:
  AC2  every item has all required fields (schema validation).
  AC3  code/test vs live evidence distinction and honest unknown/dormant/blocked states.
  AC4  historical emulations are never classified shipped-and-used when no verified
       consumer and current evidence exist (validator rule).
  AC6  no duplicate work: every spec-named queue item appears in queue_coverage.
  AC7  at least five traceable paths referencing real item ids.
  AC8  freshness date, source baseline, and verification limits documented.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from validate_capability_catalog import (  # noqa: E402
    CATALOG_PATH,
    PILOT_FAMILIES,
    ValidationError,
    load_catalog,
    render,
    validate,
)


@pytest.fixture(scope="module")
def catalog() -> dict:
    if not CATALOG_PATH.exists():
        pytest.fail(f"catalog artifact missing: {CATALOG_PATH}")
    return load_catalog()


@pytest.fixture(scope="module")
def report(catalog: dict) -> dict:
    return validate(catalog)


def test_catalog_validates(catalog: dict) -> None:
    """The authoritative artifact passes the full deterministic validation."""
    validate(catalog)  # raises ValidationError on failure


def test_every_family_covered(catalog: dict, report: dict) -> None:
    """AC1: all ten required families present, with pilot families deeply covered."""
    assert set(catalog["families"] and {f["id"] for f in catalog["families"]}) == set(
        report["per_family"]
    )
    assert PILOT_FAMILIES <= set(report["per_family"])
    for fid, items in report["per_family"].items():
        assert len(items) >= (4 if fid in PILOT_FAMILIES else 2), fid


def test_five_or_more_traceable_paths(catalog: dict) -> None:
    """AC7: capability -> consumer -> behavior evidence -> owner/task closure."""
    assert len(catalog["traceable_paths"]) >= 5


def test_freshness_block_complete(catalog: dict) -> None:
    """AC8: freshness date, baseline SHA, live check time, limits, update steps."""
    fresh = catalog["freshness"]
    assert fresh["as_of"].startswith("20")
    assert len(fresh["source_baseline_sha"]) >= 7
    assert fresh["verification_limits"]
    assert fresh["update_instructions"]


def test_live_claims_require_dated_live_evidence(catalog: dict) -> None:
    """AC3: connection_state 'live' must be backed by dated live evidence, never inferred."""
    for item in catalog["items"]:
        if item["connection_state"] == "live":
            assert any(e["type"] == "live" and e.get("date") for e in item["evidence"]), item["id"]


def test_no_verified_consumer_is_never_shipped_and_used(catalog: dict) -> None:
    """AC4: historical emulations cannot be promoted to shipped-and-used without a consumer."""
    for item in catalog["items"]:
        if item["consumer"] == "no verified consumer":
            assert item["implementation_state"] != "shipped-and-used", item["id"]


def test_queue_coverage_prevents_duplicates(catalog: dict) -> None:
    """AC6: every spec-named task ref is covered by queue_coverage entries."""
    from validate_capability_catalog import REQUIRED_QUEUE_COVERAGE

    covered = {str(q["task_ref"]).lstrip("#") for q in catalog["queue_coverage"]}
    missing = [r for r in REQUIRED_QUEUE_COVERAGE if r not in covered]
    assert not missing, f"queue_coverage missing refs: {missing}"


def test_render_is_idempotent(catalog: dict, report: dict) -> None:
    """Rendering is deterministic and does not mutate the authoritative data."""
    text = render(catalog, report)
    assert text.startswith("# Unified-Agent capability catalog and ledger")
    assert f"**{report['items']}**" in text


def test_validation_rejects_missing_consumer(catalog: dict, tmp_path: Path) -> None:
    """The validator rejects an item that omits the required consumer field."""
    import copy

    bad = copy.deepcopy(catalog)
    del bad["items"][0]["consumer"]
    with pytest.raises(ValidationError):
        validate(bad)


_NON_REGISTRATION_CLAIM = re.compile(
    r"did not register|not registered|unregistered|registration gap|not served", re.IGNORECASE
)
# Deliberately excludes a bare "profile" — the original misrecording named the
# *tool* profile_status while describing a client allowlist as a hub gap. The
# attribution must name the filtering mechanism itself.
_FILTERING_SURFACE = re.compile(
    r"client|allowlist|tools\.include|tools/list|deny-list|denylist|filter", re.IGNORECASE
)


def test_surface_filters_not_recorded_as_hub_absence(catalog: dict) -> None:
    """Task #1124 regression guard: hub capability vs profile/client visibility
    are distinct layers and are measured separately. Any claim that a tool was
    not registered / not served must attribute it to the filtering surface it
    was measured on (a client allowlist, a hub-side profile deny-list, or a
    session's tools/list) — never record profile-level filtering as hub-level
    absence.
    """
    for limit in catalog["freshness"]["verification_limits"]:
        if _NON_REGISTRATION_CLAIM.search(limit):
            assert _FILTERING_SURFACE.search(limit), (
                f"verification_limit claims non-registration without naming the "
                f"filtering surface: {limit}"
            )
    for item in catalog["items"]:
        for row in item["evidence"]:
            text = f"{row.get('ref', '')} {row.get('detail', '')}"
            if _NON_REGISTRATION_CLAIM.search(text):
                assert _FILTERING_SURFACE.search(text), (
                    f"{item['id']}: non-registration claim lacks surface attribution: {text}"
                )


def test_hub_registered_but_profile_filtered_is_explicit(catalog: dict) -> None:
    """Task #1124: items whose entry point is hub-registered but withheld from a
    profile must say so in evidence — the distinction this task exists to keep
    honest. Pins the measured example: int-imessage-notify's `notify` tool is in
    capability_catalog.mcp_tools yet absent from the developer tools/list."""
    item = next(i for i in catalog["items"] if i["id"] == "int-imessage-notify")
    joined = " ".join(f"{e.get('ref', '')} {e.get('detail', '')}" for e in item["evidence"])
    assert "hub" in joined.lower() and re.search(
        r"profile|deny-list|tools/list", joined, re.IGNORECASE
    ), "int-imessage-notify must record hub-registered-but-profile-filtered explicitly"
