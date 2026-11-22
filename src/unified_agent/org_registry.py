"""Read-only projection of a neutral starter or installation organization chart."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ORG_PATH = Path(__file__).with_name("org.yaml")
OFFICE_ID_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"


def registry_path() -> Path:
    """Installation chart from ``UA_ORG_REGISTRY_PATH``, otherwise the neutral starter."""
    override = os.environ.get("UA_ORG_REGISTRY_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return _ORG_PATH


OPERATOR_PLACEHOLDERS = ("$operator_name", "$operator")


def resolve_placeholders(value: Any) -> Any:
    """Substitute ``$operator`` / ``$operator_name`` with the configured operator.

    Lets a shipped or shared chart name "the human who runs this hub" without
    naming a person. Applied recursively to keys and values.
    """
    from . import config

    if isinstance(value, str):
        if value == "$operator":
            return config.operator_label()
        if value == "$operator_name":
            return config.operator_name()
        return value
    if isinstance(value, list):
        return [resolve_placeholders(v) for v in value]
    if isinstance(value, dict):
        return {resolve_placeholders(k): resolve_placeholders(v) for k, v in value.items()}
    return value


def load_raw() -> Any:
    """Parsed chart file with operator placeholders resolved."""
    return resolve_placeholders(yaml.safe_load(registry_path().read_text(encoding="utf-8")))


def load_service_identities(raw: Any | None = None) -> list[str]:
    """Labels the chart declares as service senders rather than agents.

    A service identity (for example an in-process bridge that writes hub
    messages under its own label) sits in a closed ``UA_AGENT_LABELS`` roster
    so the label validator accepts it, but it holds no bearer, runs no
    sessions and belongs to no office. Declaring it here is catalog metadata
    only: it grants nothing and activates no profile. Each label must pass the
    same shape check every identity label passes, so a malformed entry cannot
    match an equally malformed roster token and read as healthy.
    """
    from . import config

    data = raw if raw is not None else load_raw()
    if not isinstance(data, dict):
        return []
    rows = data.get("service_identities", [])
    if rows is None:
        return []
    if not isinstance(rows, list) or any(not isinstance(label, str) for label in rows):
        raise ValueError("org registry service_identities must be a list of identity labels")
    labels = [label.strip() for label in rows]
    malformed = [label for label in labels if not config.agent_label_is_well_formed(label)]
    if malformed:
        raise ValueError(f"org registry service_identities has malformed labels: {malformed!r}")
    return list(dict.fromkeys(labels))


def load_registry() -> list[dict[str, Any]]:
    """Load the selected office registry with current identity profile defaults.

    Profile defaults are read from the existing profiles module. This registry
    does not activate profiles or grant capabilities.
    """
    raw = load_raw()
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("org registry must declare schema_version: 1")
    rows = raw.get("offices")
    if not isinstance(rows, list):
        raise ValueError("org registry offices must be a list")
    # Validate declared services from the same parsed document, so a chart the
    # doctor would reject cannot load (or be committed) as valid.
    load_service_identities(raw)

    from . import profiles

    offices: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_office in rows:
        if not isinstance(raw_office, dict):
            raise ValueError("org registry office rows must be objects")
        office_id = raw_office.get("id")
        members = raw_office.get("members")
        if (
            not isinstance(office_id, str)
            or re.fullmatch(OFFICE_ID_PATTERN, office_id) is None
            or office_id in seen_ids
        ):
            raise ValueError(f"invalid or duplicate org office id: {office_id!r}")
        if not isinstance(members, list) or any(not isinstance(label, str) for label in members):
            raise ValueError(f"office {office_id!r} members must be a list of identities")
        lead = raw_office.get("lead")
        if lead not in members:
            raise ValueError(f"office {office_id!r} lead must be one of its members")
        seen_ids.add(office_id)
        offices.append(
            {
                "id": office_id,
                "name": str(raw_office.get("name") or office_id),
                "lead": lead,
                "members": list(dict.fromkeys(members)),
                "owned_epics": [int(epic) for epic in raw_office.get("owned_epics", [])],
                "default_lanes": list(raw_office.get("default_lanes", [])),
                "decision_rights": list(raw_office.get("decision_rights", [])),
                "default_profiles": {
                    label: profiles.default_for_identity(label) for label in members
                },
            }
        )
    _overlay_seats(offices)
    return offices


def _overlay_seats(offices: list[dict[str, Any]]) -> None:
    """Add each operator-added agent (``agent_roster``, task #2453) to its office.

    The chart file stays the source of offices; a seat only adds a member to an
    office the chart defines. A seat whose office the chart no longer has is
    left out, so the doctor's ``org_registry`` check reports the agent as an
    active identity without an office instead of inventing one.
    """
    try:
        from . import agent_roster

        seated = agent_roster.seats()
    except Exception:  # noqa: BLE001 - the chart alone still loads
        return
    if not seated:
        return
    from . import profiles

    by_id = {office["id"]: office for office in offices}
    for label, seat in sorted(seated.items()):
        office = by_id.get(seat.get("office"))
        if office is None or label in office["members"]:
            continue
        office["members"].append(label)
        office["default_profiles"][label] = profiles.default_for_identity(label)


def find_office(value: str, offices: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Resolve an office by its stable id or case-insensitive display name."""
    token = value.strip().casefold()
    return next(
        (
            office
            for office in (offices if offices is not None else load_registry())
            if office["id"].casefold() == token or office["name"].casefold() == token
        ),
        None,
    )


def offices_for_identity(label: str, offices: list[dict[str, Any]] | None = None) -> list[str]:
    """Return every office containing an identity, in charter order."""
    return [
        office["id"]
        for office in (offices if offices is not None else load_registry())
        if label in office["members"]
    ]


def office_for_identity(label: str, offices: list[dict[str, Any]] | None = None) -> str | None:
    """Return the first office for an identity that belongs to multiple offices."""
    matches = offices_for_identity(label, offices)
    return matches[0] if matches else None


def office_for_epic(epic_id: int | str, offices: list[dict[str, Any]] | None = None) -> str | None:
    """Return the owning office for a numeric epic task id."""
    try:
        wanted = int(epic_id)
    except (TypeError, ValueError):
        return None
    return next(
        (
            office["id"]
            for office in (offices if offices is not None else load_registry())
            if wanted in office["owned_epics"]
        ),
        None,
    )


def validate_registry(
    agent_labels: set[str],
    offices: list[dict[str, Any]] | None = None,
    *,
    active_labels: set[str] | None = None,
    service_labels: set[str] | None = None,
) -> list[dict[str, str | None]]:
    """Report charter members outside UA_AGENT_LABELS and active labels without an office.

    ``service_labels`` are the chart's declared service identities: they are
    expected to have no office, so they never raise
    ``active_identity_without_office``. A declared service identity that a
    closed roster does not list is reported as
    ``service_identity_not_in_agent_labels`` so the chart and the roster cannot
    silently disagree about it.
    """
    rows = offices if offices is not None else load_registry()
    office_by_label = {label: office["id"] for office in rows for label in office["members"]}
    services = set(service_labels or set())
    findings: list[dict[str, str | None]] = []
    for label in sorted(services):
        if label in office_by_label:
            findings.append(
                {
                    "identity": label,
                    "office": office_by_label[label],
                    "issue": "service_identity_in_office",
                }
            )
        elif label not in agent_labels:
            findings.append(
                {
                    "identity": label,
                    "office": None,
                    "issue": "service_identity_not_in_agent_labels",
                }
            )
    for office in rows:
        for label in office["members"]:
            if label not in agent_labels:
                findings.append(
                    {
                        "identity": label,
                        "office": office["id"],
                        "issue": "member_not_in_agent_labels",
                    }
                )
    for label in sorted(active_labels or set()):
        if label not in office_by_label and label not in services:
            findings.append(
                {"identity": label, "office": None, "issue": "active_identity_without_office"}
            )
    return findings
