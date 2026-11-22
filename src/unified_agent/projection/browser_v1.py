"""Browser-to-control-plane projection contract, version 1.

Producers minimize context at the source. This receiving contract treats all
arriving records as untrusted and fails closed without reading or writing
storage. The separate ``tools.projection.browser_derive_task_reference``
wrapper is default-off and profile-restricted; no authenticated producer client
is verified to call it.

See docs/browser-projection-contract-v1.md for the field allowlist,
compatibility policy, and retention schedule.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

CONTRACT_VERSION = "1.0"
SUPPORTED_MAJOR = 1

MAX_RECORD_BYTES = 8 * 1024
MAX_FOCUS_ENTITY_CHARS = 200
MAX_STATE_DIGEST_CHARS = 280
MAX_ORIGIN_REF_CHARS = 32
MAX_TASK_REF_CHARS = 64

ORIGIN_SURFACES = frozenset({"browser", "desktop", "vault"})
TRUST_TIERS = frozenset({"local", "remote", "vault"})
CONSENT_BASES = frozenset({"explicit_user_action", "standing_local_capture", "derived_aggregate"})

INTENT_CATEGORIES = frozenset(
    {
        "Deep Work (Drafting/Coding)",
        "Research & Gathering",
        "Task Execution & Ops",
        "Communication & Async",
        "Distraction / Rabbit Hole",
        "Idle / Transition",
    }
)
COGNITIVE_LOAD_LEVELS = frozenset({"Low", "Medium", "High", "Overload"})

# The wire allowlist. A field absent from its kind's set is refused, so a new
# producer field cannot reach the control plane without an edit here plus the
# corresponding edit in the TypeScript projector.
KIND_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "focus_anchor": frozenset(
            {
                "focus_entity",
                "intent_category",
                "cognitive_load_level",
                "state_digest",
                "origin_ref",
            }
        ),
        "cognitive_frame": frozenset(
            {
                "focus_entity",
                "intent_category",
                "cognitive_load_level",
                "state_digest",
                "friction_score",
                "dwell_seconds",
                "origin_ref",
            }
        ),
        "checkpoint": frozenset(
            {"goal_title", "focus_entity", "state_digest", "open_context_count", "origin_ref"}
        ),
        "telemetry": frozenset(
            {
                "cognitive_load_score",
                "fatigue_index",
                "context_switch_cost",
                "intent_category",
                "tab_switches_5m",
                "session_minutes",
            }
        ),
        "task_reference": frozenset({"task_ref", "relation", "confidence", "focus_entity"}),
        "vault_proposal": frozenset(
            {"note_title", "summary_digest", "suggested_folder", "focus_entity"}
        ),
    }
)

# Days a projected record may be retained by the control plane before deletion.
RETENTION_DAYS: Mapping[str, int] = MappingProxyType(
    {
        "focus_anchor": 30,
        "cognitive_frame": 14,
        "checkpoint": 30,
        "telemetry": 90,
        "task_reference": 180,
        "vault_proposal": 30,
    }
)

TEXT_FIELD_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "focus_entity": MAX_FOCUS_ENTITY_CHARS,
        "goal_title": MAX_FOCUS_ENTITY_CHARS,
        "note_title": MAX_FOCUS_ENTITY_CHARS,
        "suggested_folder": MAX_FOCUS_ENTITY_CHARS,
        "state_digest": MAX_STATE_DIGEST_CHARS,
        "summary_digest": MAX_STATE_DIGEST_CHARS,
        "relation": 64,
        "task_ref": MAX_TASK_REF_CHARS,
        "origin_ref": MAX_ORIGIN_REF_CHARS,
    }
)

_URL_LIKE = re.compile(r"(?:[a-z][a-z0-9+.-]*://|\bwww\.)", re.IGNORECASE)
_ORIGIN_REF = re.compile(r"^[0-9a-f]{16}$")
_TASK_REF = re.compile(r"^ua://task/[1-9][0-9]{0,8}$")


class ProjectionError(ValueError):
    """Raised when an inbound projection record violates the contract."""


@dataclass(frozen=True)
class Provenance:
    origin_machine: str
    origin_surface: str
    captured_at: int
    projected_at: int
    consent_basis: str
    trust_tier: str


@dataclass(frozen=True)
class ProjectedRecord:
    contract_version: str
    kind: str
    record_id: str
    provenance: Provenance
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProjectedRecord):
            return NotImplemented
        return to_wire(self) == to_wire(other)

    def __hash__(self) -> int:
        return hash((self.contract_version, self.kind, self.record_id))


def to_wire(record: ProjectedRecord) -> dict[str, Any]:
    """Serialise back to the plain JSON-compatible wire shape."""
    return {
        "contract_version": record.contract_version,
        "kind": record.kind,
        "record_id": record.record_id,
        "provenance": asdict(record.provenance),
        "payload": dict(record.payload),
    }


def parse_record(raw: object) -> ProjectedRecord:
    """Validate an untrusted projection record. Raises ProjectionError on anything odd."""
    body = _decode(raw)

    version = _require_str(body, "contract_version", limit=16)
    _check_version(version)

    kind = _require_str(body, "kind", limit=64)
    if kind not in KIND_FIELDS:
        raise ProjectionError(f"unsupported kind: {kind!r}")

    record_id = _require_str(body, "record_id", limit=128)

    unexpected = set(body) - {"contract_version", "kind", "record_id", "provenance", "payload"}
    if unexpected:
        raise ProjectionError(f"unexpected field at record level: {sorted(unexpected)}")

    provenance = _parse_provenance(body.get("provenance"))
    payload = _parse_payload(kind, body.get("payload"))

    encoded = json.dumps(body, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ProjectionError(f"record exceeds {MAX_RECORD_BYTES} bytes")

    return ProjectedRecord(
        contract_version=version,
        kind=kind,
        record_id=record_id,
        provenance=provenance,
        payload=payload,
    )


def focus_anchor_to_task_reference(
    anchor: ProjectedRecord, *, task_id: int, confidence: float
) -> ProjectedRecord:
    """The S2 vertical slice: derive a task reference from a projected focus anchor.

    Provenance is carried through untouched, so a laptop-origin anchor still
    produces a laptop-origin, remote-tier task reference.
    """
    if not isinstance(anchor, ProjectedRecord) or anchor.kind != "focus_anchor":
        raise ProjectionError("expected a focus_anchor record")
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
        raise ProjectionError("task_id must be a positive integer")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ProjectionError("confidence must be a number in [0, 1]")
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ProjectionError("confidence must be a number in [0, 1]")

    return parse_record(
        {
            "contract_version": anchor.contract_version,
            "kind": "task_reference",
            "record_id": f"tr_{anchor.record_id}_{task_id}",
            "provenance": asdict(anchor.provenance),
            "payload": {
                "task_ref": f"ua://task/{task_id}",
                "relation": "suggested_by_focus",
                "confidence": round(float(confidence), 4),
                "focus_entity": anchor.payload["focus_entity"],
            },
        }
    )


def _decode(raw: object) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProjectionError(f"record is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ProjectionError("record must be a JSON object")
    return raw


def _check_version(version: str) -> None:
    major, _, minor = version.partition(".")
    if not major.isdigit() or not minor.isdigit():
        raise ProjectionError(f"malformed contract_version: {version!r}")
    if int(major) != SUPPORTED_MAJOR:
        raise ProjectionError(
            f"unsupported contract major version {major}; this hub speaks {SUPPORTED_MAJOR}.x"
        )


def _require_str(body: Mapping[str, Any], key: str, *, limit: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProjectionError(f"{key} is required and must be a non-empty string")
    if len(value) > limit:
        raise ProjectionError(f"{key} exceeds {limit} characters")
    return value


def _parse_provenance(raw: object) -> Provenance:
    if not isinstance(raw, dict):
        raise ProjectionError("provenance is required and must be an object")

    expected = {
        "origin_machine",
        "origin_surface",
        "captured_at",
        "projected_at",
        "consent_basis",
        "trust_tier",
    }
    missing = expected - set(raw)
    if missing:
        raise ProjectionError(f"provenance is missing {sorted(missing)}")
    unexpected = set(raw) - expected
    if unexpected:
        raise ProjectionError(f"unexpected field in provenance: {sorted(unexpected)}")

    origin_machine = _require_str(raw, "origin_machine", limit=64)
    origin_surface = _require_str(raw, "origin_surface", limit=32)
    if origin_surface not in ORIGIN_SURFACES:
        raise ProjectionError(f"unknown origin_surface: {origin_surface!r}")
    consent_basis = _require_str(raw, "consent_basis", limit=64)
    if consent_basis not in CONSENT_BASES:
        raise ProjectionError(f"unknown consent_basis: {consent_basis!r}")
    trust_tier = _require_str(raw, "trust_tier", limit=32)
    if trust_tier not in TRUST_TIERS:
        raise ProjectionError(f"unknown trust_tier: {trust_tier!r}")

    return Provenance(
        origin_machine=origin_machine,
        origin_surface=origin_surface,
        captured_at=_epoch_ms(raw["captured_at"], "captured_at"),
        projected_at=_epoch_ms(raw["projected_at"], "projected_at"),
        consent_basis=consent_basis,
        trust_tier=trust_tier,
    )


def _epoch_ms(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectionError(f"{key} must be an epoch-millisecond number")
    if not math.isfinite(value) or value < 0:
        raise ProjectionError(f"{key} must be a non-negative finite epoch-millisecond number")
    return int(value)


def _parse_payload(kind: str, raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProjectionError("payload is required and must be an object")

    allowed = KIND_FIELDS[kind]
    unexpected = set(raw) - allowed
    if unexpected:
        raise ProjectionError(f"unexpected field in {kind} payload: {sorted(unexpected)}")

    payload: dict[str, Any] = {}
    for key, value in raw.items():
        payload[key] = _check_value(kind, key, value)
    return payload


def _check_value(kind: str, key: str, value: object) -> Any:
    if isinstance(value, bool) or value is None:
        raise ProjectionError(f"{key} must be a string or a finite number")

    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ProjectionError(f"{key} must be a finite number")
        if key == "confidence" and not 0.0 <= value <= 1.0:
            raise ProjectionError("confidence must be in [0, 1]")
        return value

    if not isinstance(value, str):
        # Lists, nested objects, and blobs are structurally forbidden: they are how
        # tab context, embeddings, and page text would smuggle themselves across.
        raise ProjectionError(
            f"{key} must be a string or a finite number, not {type(value).__name__}"
        )

    limit = TEXT_FIELD_LIMITS.get(key)
    if limit is not None and len(value) > limit:
        raise ProjectionError(f"{key} exceeds {limit} characters")

    # task_ref carries the internal ua:// scheme, so it is exempted from the
    # url-like sweep and pinned by its own far stricter regex instead.
    if key != "task_ref" and _URL_LIKE.search(value):
        raise ProjectionError(f"{key} contains url-like content, which never crosses the boundary")

    if key == "origin_ref" and not _ORIGIN_REF.match(value):
        raise ProjectionError("origin_ref must be a 16-character hex digest")
    if key == "task_ref" and not _TASK_REF.match(value):
        raise ProjectionError("task_ref must look like ua://task/<id>")
    if key == "intent_category" and value not in INTENT_CATEGORIES:
        raise ProjectionError(f"unknown intent_category: {value!r}")
    if key == "cognitive_load_level" and value not in COGNITIVE_LOAD_LEVELS:
        raise ProjectionError(f"unknown cognitive_load_level: {value!r}")

    return value
