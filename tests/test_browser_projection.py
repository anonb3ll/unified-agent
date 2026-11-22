"""Contract tests for the Browser -> unified-agent projection (task #279, gate G2).

The golden records here are duplicated verbatim in the Browser repo at
tests/projection-contract-v1.test.ts. If you change one, change both: that pair
is the only thing proving the TypeScript projector and the Python validator
agree on the wire shape.
"""

from __future__ import annotations

import json

import pytest

from unified_agent.projection import browser_v1 as proj


def _provenance(**overrides: object) -> dict:
    base = {
        "origin_machine": "desktop",
        "origin_surface": "browser",
        "captured_at": 1_754_524_800_000,
        "projected_at": 1_754_524_801_000,
        "consent_basis": "standing_local_capture",
        "trust_tier": "local",
    }
    base.update(overrides)
    return base


def _focus_anchor(**overrides: object) -> dict:
    payload = {
        "focus_entity": "S2 projection contract",
        "intent_category": "Deep Work (Drafting/Coding)",
        "cognitive_load_level": "High",
        "state_digest": "Drafting the versioned projection schema for gate G2.",
        "origin_ref": "b7e4f1a2c9d03e58",
    }
    payload.update(overrides.pop("payload", {}))  # type: ignore[arg-type]
    record = {
        "contract_version": "1.0",
        "kind": "focus_anchor",
        "record_id": "fa_01J9Z6Q4X0",
        "provenance": _provenance(),
        "payload": payload,
    }
    record.update(overrides)
    return record


# --- golden path ---------------------------------------------------------


def test_valid_focus_anchor_round_trips():
    record = proj.parse_record(_focus_anchor())
    assert record.kind == "focus_anchor"
    assert record.payload["focus_entity"] == "S2 projection contract"
    assert proj.parse_record(proj.to_wire(record)) == record


def test_accepts_a_json_string_as_well_as_a_mapping():
    assert proj.parse_record(json.dumps(_focus_anchor())) == proj.parse_record(_focus_anchor())


# --- versioning ----------------------------------------------------------


def test_same_major_newer_minor_is_accepted_forward_compatibly():
    assert proj.parse_record(_focus_anchor(contract_version="1.7")).contract_version == "1.7"


def test_future_major_version_is_refused():
    with pytest.raises(proj.ProjectionError, match="major"):
        proj.parse_record(_focus_anchor(contract_version="2.0"))


def test_missing_version_is_refused():
    bad = _focus_anchor()
    del bad["contract_version"]
    with pytest.raises(proj.ProjectionError):
        proj.parse_record(bad)


# --- provenance ----------------------------------------------------------


@pytest.mark.parametrize(
    ("surface", "tier"),
    [("browser", "local"), ("desktop", "local"), ("vault", "vault"), ("browser", "remote")],
)
def test_provenance_survives_a_round_trip(surface: str, tier: str):
    record = _focus_anchor()
    record["provenance"] = _provenance(origin_surface=surface, trust_tier=tier)
    parsed = proj.parse_record(proj.to_wire(proj.parse_record(record)))
    assert parsed.provenance.origin_surface == surface
    assert parsed.provenance.trust_tier == tier


def test_local_remote_and_vault_records_stay_distinguishable():
    tiers = set()
    for tier in ("local", "remote", "vault"):
        rec = _focus_anchor(record_id=f"fa_{tier}")
        rec["provenance"] = _provenance(trust_tier=tier)
        tiers.add(proj.parse_record(proj.to_wire(proj.parse_record(rec))).provenance.trust_tier)
    assert tiers == {"local", "remote", "vault"}


def test_unknown_trust_tier_is_refused():
    rec = _focus_anchor()
    rec["provenance"] = _provenance(trust_tier="trusted")
    with pytest.raises(proj.ProjectionError, match="trust_tier"):
        proj.parse_record(rec)


def test_missing_provenance_is_refused():
    rec = _focus_anchor()
    del rec["provenance"]
    with pytest.raises(proj.ProjectionError, match="provenance"):
        proj.parse_record(rec)


def test_provenance_cannot_omit_consent_basis():
    rec = _focus_anchor()
    prov = _provenance()
    del prov["consent_basis"]
    rec["provenance"] = prov
    with pytest.raises(proj.ProjectionError, match="consent_basis"):
        proj.parse_record(rec)


# --- field allowlist -----------------------------------------------------


def test_focus_anchor_allowlist_is_pinned():
    """A new field cannot reach the wire without being added here deliberately."""
    assert proj.KIND_FIELDS["focus_anchor"] == frozenset(
        {
            "focus_entity",
            "intent_category",
            "cognitive_load_level",
            "state_digest",
            "origin_ref",
        }
    )


def test_task_reference_allowlist_is_pinned():
    assert proj.KIND_FIELDS["task_reference"] == frozenset(
        {"task_ref", "relation", "confidence", "focus_entity"}
    )


def test_declared_kinds_are_pinned():
    assert set(proj.KIND_FIELDS) == {
        "focus_anchor",
        "cognitive_frame",
        "checkpoint",
        "telemetry",
        "task_reference",
        "vault_proposal",
    }


def test_field_outside_the_allowlist_is_refused():
    with pytest.raises(proj.ProjectionError, match="unexpected field"):
        proj.parse_record(_focus_anchor(payload={"page_title": "Private Account Overview"}))


def test_unknown_kind_is_refused():
    with pytest.raises(proj.ProjectionError, match="kind"):
        proj.parse_record(_focus_anchor(kind="raw_page"))


# --- privacy: the MUST NOTs from baseline 2026-08-07 section 6 -----------


ADVERSARIAL_SOURCE_LEAKS = [
    ("url", "https://banking.example.com/account?token=secret123"),
    ("pageTitle", "Private Account Overview"),
    ("cleanTextExcerpt", "CONFIDENTIAL BALANCE: $1,000,000"),
    ("textContent", "full page body text"),
    ("openTabsContext", [{"url": "https://mail.example.com/inbox"}]),
    ("browsing_history", ["https://a.example.com", "https://b.example.com"]),
    ("vectorAnchor", [0.1] * 384),
    ("embedding", [0.2] * 384),
]


@pytest.mark.parametrize(("field", "value"), ADVERSARIAL_SOURCE_LEAKS)
def test_internal_browser_fields_never_cross_the_boundary(field: str, value: object):
    with pytest.raises(proj.ProjectionError, match="unexpected field"):
        proj.parse_record(_focus_anchor(payload={field: value}))


@pytest.mark.parametrize(
    "digest",
    [
        "Reading https://banking.example.com/account?token=secret123",
        "See http://internal.example.com/wiki",
        "Opened file:///home/you/secrets.txt",
        "Sourced from chrome-extension://abcdef/page.html",
    ],
)
def test_a_url_smuggled_into_an_allowed_text_field_is_refused(digest: str):
    with pytest.raises(proj.ProjectionError, match="url-like"):
        proj.parse_record(_focus_anchor(payload={"state_digest": digest}))


def test_origin_ref_must_be_an_opaque_digest_not_a_url():
    with pytest.raises(proj.ProjectionError, match="origin_ref"):
        proj.parse_record(_focus_anchor(payload={"origin_ref": "https://example.com/a"}))


def test_a_realistic_adversarial_record_leaks_nothing_if_it_somehow_parses():
    """Belt-and-braces: whatever survives parsing must be free of raw browsing data."""
    record = proj.parse_record(_focus_anchor())
    wire = json.dumps(proj.to_wire(record))
    for probe in ("http://", "https://", "file://", "CONFIDENTIAL", "banking.example.com"):
        assert probe not in wire


# --- size limits ---------------------------------------------------------


def test_oversized_record_is_rejected_not_truncated():
    with pytest.raises(proj.ProjectionError, match="exceeds"):
        proj.parse_record(
            _focus_anchor(payload={"state_digest": "x" * (proj.MAX_RECORD_BYTES + 1)})
        )


def test_oversized_single_field_is_rejected():
    with pytest.raises(proj.ProjectionError, match="state_digest"):
        proj.parse_record(
            _focus_anchor(payload={"state_digest": "y" * (proj.MAX_STATE_DIGEST_CHARS + 1)})
        )


def test_oversized_focus_entity_is_rejected():
    with pytest.raises(proj.ProjectionError, match="focus_entity"):
        proj.parse_record(
            _focus_anchor(payload={"focus_entity": "z" * (proj.MAX_FOCUS_ENTITY_CHARS + 1)})
        )


# --- malformed / untrusted ----------------------------------------------


@pytest.mark.parametrize("raw", [None, 42, "not json", [], '"a string"', b"bytes"])
def test_malformed_input_is_refused(raw: object):
    with pytest.raises(proj.ProjectionError):
        proj.parse_record(raw)


def test_payload_must_be_an_object():
    rec = _focus_anchor()
    rec["payload"] = "nope"
    with pytest.raises(proj.ProjectionError, match="payload"):
        proj.parse_record(rec)


def test_non_finite_numbers_are_refused():
    rec = _focus_anchor()
    rec["provenance"] = _provenance(captured_at=float("inf"))
    with pytest.raises(proj.ProjectionError):
        proj.parse_record(rec)


def test_negative_capture_time_is_refused():
    rec = _focus_anchor()
    rec["provenance"] = _provenance(captured_at=-1)
    with pytest.raises(proj.ProjectionError, match="captured_at"):
        proj.parse_record(rec)


# --- the vertical slice: focus anchor -> task reference ------------------


def test_focus_anchor_projects_to_a_task_reference():
    anchor = proj.parse_record(_focus_anchor())
    ref = proj.focus_anchor_to_task_reference(anchor, task_id=279, confidence=0.8)
    assert ref.kind == "task_reference"
    assert ref.payload["task_ref"] == "ua://task/279"
    assert ref.payload["relation"] == "suggested_by_focus"
    assert ref.payload["focus_entity"] == "S2 projection contract"
    assert proj.parse_record(proj.to_wire(ref)) == ref


def test_derived_task_reference_inherits_provenance_unchanged():
    anchor_raw = _focus_anchor()
    anchor_raw["provenance"] = _provenance(trust_tier="remote", origin_machine="laptop")
    anchor = proj.parse_record(anchor_raw)
    ref = proj.focus_anchor_to_task_reference(anchor, task_id=279, confidence=0.5)
    assert ref.provenance.trust_tier == "remote"
    assert ref.provenance.origin_machine == "laptop"


def test_task_reference_derivation_refuses_a_non_anchor():
    anchor = proj.parse_record(_focus_anchor())
    ref = proj.focus_anchor_to_task_reference(anchor, task_id=279, confidence=0.5)
    with pytest.raises(proj.ProjectionError, match="focus_anchor"):
        proj.focus_anchor_to_task_reference(ref, task_id=280, confidence=0.5)


@pytest.mark.parametrize("task_id", [0, -1, True])
def test_task_reference_refuses_a_bad_task_id(task_id: object):
    anchor = proj.parse_record(_focus_anchor())
    with pytest.raises(proj.ProjectionError, match="task_id"):
        proj.focus_anchor_to_task_reference(anchor, task_id=task_id, confidence=0.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("confidence", [-0.1, 1.1, "high"])
def test_task_reference_refuses_a_bad_confidence(confidence: object):
    anchor = proj.parse_record(_focus_anchor())
    with pytest.raises(proj.ProjectionError, match="confidence"):
        proj.focus_anchor_to_task_reference(anchor, task_id=279, confidence=confidence)  # type: ignore[arg-type]


# --- cross-repo interop --------------------------------------------------


# Verbatim output of projectFocusAnchor() in the Browser repo, generated on
# 2026-08-07 from src/lib/projection/contract-v1.ts. The same literal is pinned
# in that repo's tests/projection-contract-v1.test.ts ("interop golden"). If the
# two ever drift, one of these two tests fails and the wire format is broken.
INTEROP_GOLDEN = {
    "contract_version": "1.0",
    "kind": "focus_anchor",
    "record_id": "fa_me0mrk00_9251127f9a34ba6f",
    "provenance": {
        "origin_machine": "desktop",
        "origin_surface": "browser",
        "captured_at": 1754524800000,
        "projected_at": 1754524801000,
        "consent_basis": "standing_local_capture",
        "trust_tier": "local",
    },
    "payload": {
        "focus_entity": "S2 projection contract",
        "intent_category": "Deep Work (Drafting/Coding)",
        "cognitive_load_level": "High",
        "state_digest": "Drafting the versioned projection schema for gate G2.",
        "origin_ref": "9251127f9a34ba6f",
    },
}


def test_real_typescript_projector_output_is_accepted():
    record = proj.parse_record(INTEROP_GOLDEN)
    assert record.kind == "focus_anchor"
    assert record.provenance.trust_tier == "local"
    assert proj.to_wire(record) == INTEROP_GOLDEN


def test_the_vertical_slice_runs_end_to_end_on_real_projector_output():
    anchor = proj.parse_record(INTEROP_GOLDEN)
    ref = proj.focus_anchor_to_task_reference(anchor, task_id=279, confidence=0.9)
    assert ref.payload["task_ref"] == "ua://task/279"
    assert ref.provenance == anchor.provenance


# --- retention -----------------------------------------------------------


def test_retention_days_are_declared_for_every_kind():
    assert set(proj.RETENTION_DAYS) == set(proj.KIND_FIELDS)
    assert all(v > 0 for v in proj.RETENTION_DAYS.values())
