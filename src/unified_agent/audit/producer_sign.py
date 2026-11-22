"""Signed authenticated-producer evidence events (task #1155 / audit gap 12).

Binds a verified hub identity to the hash chain using the agent's existing
bearer from ``UA_AGENT_TOKENS`` as an HMAC key. No new key infrastructure.

Fail-closed rules:
- No verified identity → no signed event.
- No bearer mapped for that label → no signed event (OAuth/cookie-only
  identities without a ``UA_AGENT_TOKENS`` entry cannot be re-verified later).
- Ambiguous or missing signature material → verification returns False.
- Secrets never appear in payloads, reports, or logs — only the identity label
  and an opaque hex HMAC.

IdP-verified producer class (task #1249 / Epic E, E2): when the caller
authenticated through the hub's WorkOS leg (``workos_hub_auth`` — JWKS, exact
issuer, the one configured organization, ``sid``/``sub`` agreement, and a live
membership/session re-check on every request), the producer event carries the
verified session claims (subject, organization, session id, expiry) and is
HMAC-signed with the leg's own configured secret (``WORKOS_COOKIE_PASSWORD``).
That closes the v1 hole above — a WorkOS-mapped human identity needs no bearer
— while keeping the no-new-key-infrastructure rule. Report-time verification
recomputes the HMAC and re-checks the claims against the *current* leg
configuration (organization id and subject→identity map), so a wrong org, a
missing/rotated mapping, or a tampered signature fails closed. The sealed
session and access token are never stored — the event records the verified
*claims*, not the credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from .. import config, events
from ..dispatch.child_env import resolve_agent_bearer_token

logger = logging.getLogger(__name__)

CHANNEL_PRODUCER_AUTHENTICATED = "agent.evidence.producer_authenticated"
SCHEME_HMAC_SHA256_V1 = "hmac-sha256-v1"
SCHEME_WORKOS_IDP_V1 = "workos-idp-v1"

# How a verified producer's signature was backed: an agent bearer from
# UA_AGENT_TOKENS, or a WorkOS session the hub verified against the IdP.
VERIFIED_VIA_AGENT_BEARER = "agent_bearer"
VERIFIED_VIA_IDP = "idp"

_PRODUCER_KINDS = frozenset({"approval", "delegation"})


def _canonical_signing_payload(
    *,
    identity: str,
    kind: str,
    ref_id: str,
    task_id: str | None,
) -> str:
    """Deterministic UTF-8 JSON covering the attribution claim (no signature)."""
    body: dict[str, Any] = {
        "identity": identity,
        "kind": kind,
        "ref_id": str(ref_id),
        "scheme": SCHEME_HMAC_SHA256_V1,
    }
    if task_id is not None and str(task_id).strip():
        body["task_id"] = str(task_id).strip()
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def sign_producer_claim(
    *,
    identity: str,
    kind: str,
    ref_id: str,
    task_id: str | None,
    bearer_token: str,
) -> str:
    """HMAC-SHA256 hex digest over the canonical claim using the bearer token."""
    msg = _canonical_signing_payload(identity=identity, kind=kind, ref_id=ref_id, task_id=task_id)
    return hmac.new(
        bearer_token.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _canonical_idp_signing_payload(
    *,
    identity: str,
    kind: str,
    ref_id: str,
    task_id: str | None,
    subject: str,
    organization_id: str,
    session_id: str,
    expires_at: int,
) -> str:
    """Deterministic UTF-8 JSON covering the IdP-attested attribution claim."""
    body: dict[str, Any] = {
        "identity": identity,
        "kind": kind,
        "ref_id": str(ref_id),
        "scheme": SCHEME_WORKOS_IDP_V1,
        "workos_subject": subject,
        "workos_organization_id": organization_id,
        "workos_session_id": session_id,
        "workos_expires_at": int(expires_at),
    }
    if task_id is not None and str(task_id).strip():
        body["task_id"] = str(task_id).strip()
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _idp_signing_key() -> bytes | None:
    """The WorkOS leg's own configured secret, used as the attestation key.

    ``WORKOS_COOKIE_PASSWORD`` is required (≥32 chars) whenever the leg is
    configured, so its absence means the leg cannot have verified a session —
    fail closed rather than sign with anything weaker.
    """
    raw = os.environ.get("WORKOS_COOKIE_PASSWORD", "")
    return raw.encode("utf-8") if raw else None


def _idp_identity_map() -> dict[str, str]:
    """The operator's subject→UA-label mapping, lazily imported to keep this
    module free of the pilot/SDK dependency chain."""
    from ..identity_core import _identity_map_from_environment

    return _identity_map_from_environment()


def sign_idp_producer_claim(
    *,
    identity: str,
    kind: str,
    ref_id: str,
    task_id: str | None,
    subject: str,
    organization_id: str,
    session_id: str,
    expires_at: int,
    key: bytes,
) -> str:
    """HMAC-SHA256 hex digest over the canonical IdP claim."""
    msg = _canonical_idp_signing_payload(
        identity=identity,
        kind=kind,
        ref_id=ref_id,
        task_id=task_id,
        subject=subject,
        organization_id=organization_id,
        session_id=session_id,
        expires_at=expires_at,
    )
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_hmac_v1(payload: dict[str, Any]) -> bool:
    """Return True only when the payload's HMAC verifies against current tokens.

    Fail-closed on any missing/malformed field, unknown scheme, unknown kind,
    or absent/rotated bearer for the claimed identity.
    """
    if not isinstance(payload, dict):
        return False
    identity = payload.get("identity")
    kind = payload.get("kind")
    ref_id = payload.get("ref_id")
    scheme = payload.get("scheme")
    signature = payload.get("signature")
    if not isinstance(identity, str) or not identity.strip():
        return False
    if kind not in _PRODUCER_KINDS:
        return False
    if not isinstance(ref_id, str) and not isinstance(ref_id, int):
        return False
    if scheme != SCHEME_HMAC_SHA256_V1:
        return False
    if not isinstance(signature, str) or not signature.strip():
        return False
    task_id = payload.get("task_id")
    if task_id is not None and not isinstance(task_id, (str, int)):
        return False
    bearer = resolve_agent_bearer_token(identity.strip())
    if not bearer:
        return False
    expected = sign_producer_claim(
        identity=identity.strip(),
        kind=str(kind),
        ref_id=str(ref_id),
        task_id=str(task_id) if task_id is not None else None,
        bearer_token=bearer,
    )
    return hmac.compare_digest(expected, signature.strip())


def _verify_workos_idp_v1(payload: dict[str, Any]) -> bool:
    """Return True only when a ``workos-idp-v1`` event verifies.

    Recomputes the HMAC over the recorded claims *and* re-checks them against
    the current leg configuration: the organization must equal the configured
    ``UA_HUB_WORKOS_ORGANIZATION_ID`` and the subject must still map to the
    claimed identity in ``UA_HUB_WORKOS_IDENTITY_MAP``. Fail-closed on any
    missing/malformed field, wrong org, missing/rotated mapping, absent leg
    secret, or signature mismatch.
    """
    if not isinstance(payload, dict):
        return False
    identity = payload.get("identity")
    kind = payload.get("kind")
    ref_id = payload.get("ref_id")
    signature = payload.get("signature")
    subject = payload.get("workos_subject")
    organization_id = payload.get("workos_organization_id")
    session_id = payload.get("workos_session_id")
    expires_at = payload.get("workos_expires_at")
    if not isinstance(identity, str) or not identity.strip():
        return False
    if kind not in _PRODUCER_KINDS:
        return False
    if not isinstance(ref_id, str) and not isinstance(ref_id, int):
        return False
    if not isinstance(signature, str) or not signature.strip():
        return False
    for value in (subject, organization_id, session_id):
        if not isinstance(value, str) or not value.strip():
            return False
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return False
    task_id = payload.get("task_id")
    if task_id is not None and not isinstance(task_id, (str, int)):
        return False
    configured_org = os.environ.get("UA_HUB_WORKOS_ORGANIZATION_ID", "").strip()
    if not configured_org or organization_id != configured_org:
        return False
    if _idp_identity_map().get(subject.strip()) != identity.strip():
        return False
    key = _idp_signing_key()
    if key is None:
        return False
    expected = sign_idp_producer_claim(
        identity=identity.strip(),
        kind=str(kind),
        ref_id=str(ref_id),
        task_id=str(task_id) if task_id is not None else None,
        subject=subject.strip(),
        organization_id=organization_id.strip(),
        session_id=session_id.strip(),
        expires_at=int(expires_at),
        key=key,
    )
    return hmac.compare_digest(expected, signature.strip())


def verify_producer_signature(payload: dict[str, Any]) -> bool:
    """Dispatch a producer event's signature check by scheme. Fail-closed on
    any missing/malformed field, unknown scheme, or signature mismatch."""
    if not isinstance(payload, dict):
        return False
    scheme = payload.get("scheme")
    if scheme == SCHEME_HMAC_SHA256_V1:
        return _verify_hmac_v1(payload)
    if scheme == SCHEME_WORKOS_IDP_V1:
        return _verify_workos_idp_v1(payload)
    return False


def _append_producer_event(payload: dict[str, Any], originator: str, conn: Any = None) -> str:
    """Append a producer event, inside ``conn``'s open transaction when given.

    With ``conn`` the caller owns the write transaction (task #1672): the stamp
    commits or rolls back with the mutation it attests, so a resolver that loses
    a status race never leaves a signed stamp behind. No socket fan-out then;
    the caller's own event carries the notification.
    """
    if conn is None:
        return events.record_event(
            CHANNEL_PRODUCER_AUTHENTICATED,
            payload=payload,
            originator=originator,
        )
    ulid, _, _ = events.record_event_once_on_connection(
        conn,
        CHANNEL_PRODUCER_AUTHENTICATED,
        payload=payload,
        originator=originator,
    )
    return ulid


def _stamp_idp_producer_event(
    *,
    verified: str,
    kind: str,
    ref_id: str | int,
    task_id: str | int | None,
    metadata: dict[str, Any],
    conn: Any = None,
) -> str | None:
    """Stamp a producer event attesting a WorkOS-session identity (E2).

    ``metadata`` is the request's ``auth_metadata`` — populated by the HTTP
    middleware only after ``workos_hub_auth.resolve`` verified the sealed
    session (JWKS, issuer, org, live re-check). The event records the verified
    claims, signed with the leg's configured secret. Fail-closed to None on
    any inconsistency — an approval whose session context cannot attest itself
    simply produces no signed event.
    """
    subject = metadata.get("workos_user_id")
    organization_id = metadata.get("workos_organization_id")
    session_id = metadata.get("workos_session_id")
    expires_at = metadata.get("exp")
    for value in (subject, organization_id, session_id):
        if not isinstance(value, str) or not value.strip():
            logger.info("producer stamp skipped: incomplete WorkOS session metadata")
            return None
    if (
        not isinstance(expires_at, (int, float))
        or isinstance(expires_at, bool)
        or expires_at <= time.time()
    ):
        logger.info("producer stamp skipped: WorkOS session expiry missing or past")
        return None
    configured_org = os.environ.get("UA_HUB_WORKOS_ORGANIZATION_ID", "").strip()
    if not configured_org or organization_id.strip() != configured_org:
        logger.info("producer stamp skipped: WorkOS organization does not match the configured leg")
        return None
    # The mapping is the whole authorization decision on this leg; re-check it
    # at stamp time so a session fact that disagrees with the current map can
    # never be attested.
    if _idp_identity_map().get(subject.strip()) != verified:
        logger.info("producer stamp skipped: WorkOS subject does not map to the verified identity")
        return None
    key = _idp_signing_key()
    if key is None:
        logger.info("producer stamp skipped: WorkOS leg secret not configured")
        return None
    task_key = str(task_id).strip() if task_id is not None and str(task_id).strip() else None
    signature = sign_idp_producer_claim(
        identity=verified,
        kind=kind,
        ref_id=str(ref_id),
        task_id=task_key,
        subject=subject.strip(),
        organization_id=organization_id.strip(),
        session_id=session_id.strip(),
        expires_at=int(expires_at),
        key=key,
    )
    payload: dict[str, Any] = {
        "identity": verified,
        "kind": kind,
        "ref_id": str(ref_id),
        "scheme": SCHEME_WORKOS_IDP_V1,
        "signature": signature,
        "producer": True,
        "workos_subject": subject.strip(),
        "workos_organization_id": organization_id.strip(),
        "workos_session_id": session_id.strip(),
        "workos_expires_at": int(expires_at),
    }
    if task_key is not None:
        payload["task_id"] = task_key
    return _append_producer_event(payload, verified, conn)


def stamp_authenticated_producer_event(
    *,
    kind: str,
    ref_id: str | int,
    task_id: str | int | None = None,
    identity: str | None = None,
    conn: Any = None,
) -> str | None:
    """Append a signed producer event for the verified caller.

    ``conn``, when given, is a connection holding an open write transaction;
    the stamp is appended inside it and commits or rolls back with the caller's
    mutation (task #1672).

    Returns the event ULID on success, or None when stamping is refused
    (unauthenticated, no bearer mapping, unknown kind). Never raises — the
    primary mutation (approval resolve / delegation complete) must not fail
    because attribution bookkeeping did. Callers that need fail-closed
    *reporting* still see provenance_verified=false when this returns None.

    When the request authenticated through the WorkOS hub leg
    (``auth_metadata["auth_method"] == "workos"``), the IdP-verified scheme is
    stamped instead — the session claims, not a bearer, are the evidence.
    """
    try:
        if kind not in _PRODUCER_KINDS:
            return None
        verified = (identity if identity is not None else config.verified_agent_identity()) or None
        if not isinstance(verified, str) or not verified.strip():
            return None
        verified = verified.strip()
        metadata = config.current_auth_metadata()
        if isinstance(metadata, dict) and metadata.get("auth_method") == "workos":
            return _stamp_idp_producer_event(
                verified=verified,
                kind=kind,
                ref_id=ref_id,
                task_id=task_id,
                metadata=metadata,
                conn=conn,
            )
        bearer = resolve_agent_bearer_token(verified)
        if not bearer:
            logger.info(
                "producer stamp skipped: no UA_AGENT_TOKENS bearer for identity=%s",
                verified,
            )
            return None
        task_key = str(task_id).strip() if task_id is not None and str(task_id).strip() else None
        signature = sign_producer_claim(
            identity=verified,
            kind=kind,
            ref_id=str(ref_id),
            task_id=task_key,
            bearer_token=bearer,
        )
        payload: dict[str, Any] = {
            "identity": verified,
            "kind": kind,
            "ref_id": str(ref_id),
            "scheme": SCHEME_HMAC_SHA256_V1,
            "signature": signature,
            # Include kind/ref_id shape used by evidence_report task matching
            # as a belt-and-suspenders when task_id is absent.
            "producer": True,
        }
        if task_key is not None:
            payload["task_id"] = task_key
        # Tailnet device provenance (task #1250, E3): the verified node
        # identity of the calling device, resolved by the hub middleware from
        # local tailscaled. Absent for loopback/non-tailnet/unresolvable
        # peers — never a bare hostname claim. Integrity rides the event
        # hash chain (verify requires chain_valid), so it is payload data,
        # not part of the signed claim.
        device = config.current_peer_device()
        if isinstance(device, dict) and device.get("node"):
            payload["device"] = device
        return _append_producer_event(payload, verified, conn)
    except Exception:
        logger.exception("producer stamp failed (fail-closed: no signed event)")
        return None


def parse_event_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def verifying_producer_events(
    event_rows: list[dict[str, Any]],
    *,
    chain_valid: bool,
) -> list[dict[str, Any]]:
    """Return producer events whose signature verifies; empty when chain is invalid.

    Each returned producer carries ``verified_via`` — ``"agent_bearer"`` for
    ``hmac-sha256-v1`` events or ``"idp"`` for ``workos-idp-v1`` events — plus
    the non-secret WorkOS claim fields for IdP-verified events, so report
    surfaces can distinguish a human link verified-by-idp from one verified
    only by an agent bearer.
    """
    if not chain_valid:
        return []
    out: list[dict[str, Any]] = []
    for row in event_rows:
        if row.get("channel") != CHANNEL_PRODUCER_AUTHENTICATED:
            continue
        if not row.get("entry_hash"):
            continue
        payload = parse_event_payload(row.get("payload"))
        if not verify_producer_signature(payload):
            continue
        verified_via = (
            VERIFIED_VIA_IDP
            if payload.get("scheme") == SCHEME_WORKOS_IDP_V1
            else VERIFIED_VIA_AGENT_BEARER
        )
        producer: dict[str, Any] = {
            "ulid": row.get("ulid"),
            "identity": payload.get("identity"),
            "kind": payload.get("kind"),
            "ref_id": payload.get("ref_id"),
            "task_id": payload.get("task_id"),
            "scheme": payload.get("scheme"),
            "verified_via": verified_via,
            "entry_hash": row.get("entry_hash"),
            "created_at": row.get("created_at"),
        }
        # Device provenance (task #1250): only ever present on chain-valid,
        # signature-verified events, so a report can trust the node claim.
        device = payload.get("device")
        if isinstance(device, dict) and device.get("node"):
            producer["device"] = device
        if verified_via == VERIFIED_VIA_IDP:
            producer["workos_subject"] = payload.get("workos_subject")
            producer["workos_organization_id"] = payload.get("workos_organization_id")
            producer["workos_session_id"] = payload.get("workos_session_id")
        out.append(producer)
    return out
