"""WebAuthn passkeys for the human who approves OAuth sign-ins (task #1322, J3).

The private edition proves the approver is a person, not a passphrase holder:
the approving browser must sit on a tailnet node owned by the configured human
login **and** present an assertion from a passkey that login registered once in
the Control Center.

Deliberately minimal and dependency-free (``cryptography`` is already pinned):

* Registration uses ``attestation: "none"``. The browser's
  ``AuthenticatorAttestationResponse.getPublicKey()`` hands over the credential
  key as SubjectPublicKeyInfo DER, so no CBOR/COSE parsing is needed. Trust in
  a registration comes from who may register (an authenticated human Control
  Center session on the approver's own tailnet node), not from attestation.
* Assertion verification is the WebAuthn §7.2 core: single-use challenge bound
  to one consent/device request, ``type``/``challenge``/``origin`` in
  clientDataJSON, rpIdHash, user-present **and** user-verified flags, a
  signature over ``authenticatorData || SHA-256(clientDataJSON)``, and a
  non-decreasing signature counter.
* ES256 (-7), EdDSA (-8) and RS256 (-257) keys are accepted.

Nothing secret is stored: a passkey row is a public key, its credential id,
the owning login and a counter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import config, storage

ALG_ES256 = -7
ALG_EDDSA = -8
ALG_RS256 = -257
SUPPORTED_ALGS = (ALG_ES256, ALG_EDDSA, ALG_RS256)

CHALLENGE_TTL_S = 600
_MAX_CHALLENGES = 512
_MAX_FIELD = 16 * 1024

_FLAG_UP = 0x01
_FLAG_UV = 0x04
_FLAG_AT = 0x40


class PasskeyError(ValueError):
    """A registration or assertion that must be refused. The message is safe to show."""


@dataclass
class _Challenge:
    purpose: str  # "register" | "assert"
    binding: str
    login: str
    expires_at: float


_challenges: dict[str, _Challenge] = {}
_lock = threading.Lock()


def _now() -> float:
    """Clock seam (tests replace it)."""
    return time.time()


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > _MAX_FIELD:
        raise PasskeyError("malformed passkey field")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise PasskeyError("malformed passkey field") from exc


def expected_origin() -> str | None:
    """The only origin a passkey ceremony may come from: the OAuth issuer's."""
    issuer = config.mcp_oauth_issuer()
    if not issuer:
        return None
    parts = urlsplit(issuer)
    return f"{parts.scheme}://{parts.netloc}".lower()


def issue_challenge(purpose: str, *, binding: str, login: str) -> str:
    """A fresh single-use challenge bound to one ceremony (``binding``) and login."""
    challenge = b64url_encode(secrets.token_bytes(32))
    now = _now()
    with _lock:
        for key in [k for k, c in _challenges.items() if c.expires_at < now]:
            _challenges.pop(key, None)
        if len(_challenges) >= _MAX_CHALLENGES:
            oldest = min(_challenges, key=lambda k: _challenges[k].expires_at)
            _challenges.pop(oldest, None)
        _challenges[challenge] = _Challenge(purpose, binding, login, now + CHALLENGE_TTL_S)
    return challenge


def _consume_challenge(challenge: str, purpose: str, binding: str) -> _Challenge:
    with _lock:
        entry = _challenges.pop(challenge, None)
    if entry is None or entry.expires_at < _now():
        raise PasskeyError("passkey challenge is unknown or expired; reload the page")
    if entry.purpose != purpose or not secrets.compare_digest(entry.binding, binding):
        raise PasskeyError("passkey challenge does not belong to this request")
    return entry


def reset_challenges() -> None:
    with _lock:
        _challenges.clear()


def _user_handle(login: str) -> str:
    return b64url_encode(hashlib.sha256(b"ua-approver:" + login.encode()).digest()[:16])


# -- store ---------------------------------------------------------------------


def list_passkeys(login: str | None = None) -> list[dict[str, Any]]:
    """Registered passkeys (public metadata only; never the key bytes)."""
    conn = storage.connect()
    try:
        if login:
            rows = conn.execute(
                "SELECT credential_id, login, rp_id, alg, sign_count, label, registered_by,"
                " created_at, last_used_at FROM oauth_passkeys WHERE login = ?"
                " ORDER BY created_at",
                (login,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT credential_id, login, rp_id, alg, sign_count, label, registered_by,"
                " created_at, last_used_at FROM oauth_passkeys ORDER BY created_at"
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _load(credential_id: str) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM oauth_passkeys WHERE credential_id = ?", (credential_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def delete_passkey(credential_id: str, *, login: str) -> bool:
    """Delete one passkey, only if it belongs to ``login``."""
    conn = storage.connect()
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM oauth_passkeys WHERE credential_id = ? AND login = ?",
                (credential_id, login),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


# -- crypto helpers -------------------------------------------------------------


def _load_public_key(spki: bytes, alg: int):
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    try:
        key = load_der_public_key(spki)
    except (ValueError, TypeError) as exc:
        raise PasskeyError("passkey public key is not valid SubjectPublicKeyInfo") from exc
    if alg == ALG_ES256 and isinstance(key, ec.EllipticCurvePublicKey):
        if key.curve.name != "secp256r1":
            raise PasskeyError("ES256 passkey must use P-256")
        return key
    if alg == ALG_EDDSA and isinstance(key, ed25519.Ed25519PublicKey):
        return key
    if alg == ALG_RS256 and isinstance(key, rsa.RSAPublicKey):
        if key.key_size < 2048:
            raise PasskeyError("RS256 passkey key is too small")
        return key
    raise PasskeyError("passkey algorithm is not supported")


def _verify_signature(key, alg: int, signature: bytes, message: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding

    try:
        if alg == ALG_ES256:
            key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        elif alg == ALG_EDDSA:
            key.verify(signature, message)
        elif alg == ALG_RS256:
            key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        else:
            return False
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def _check_client_data(raw: bytes, *, ceremony: str, challenge_check) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PasskeyError("clientDataJSON is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("type") != ceremony:
        raise PasskeyError("passkey ceremony type mismatch")
    if data.get("crossOrigin") is True:
        raise PasskeyError("cross-origin passkey ceremonies are refused")
    origin = str(data.get("origin") or "").lower().rstrip("/")
    expected = expected_origin()
    if not expected or origin != expected:
        raise PasskeyError("passkey origin does not match the hub issuer")
    challenge = data.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        raise PasskeyError("passkey challenge missing")
    challenge_check(challenge)
    return data


def _parse_auth_data(raw: bytes, rp_id: str) -> tuple[int, int, bytes]:
    """Return ``(flags, sign_count, rest)`` after checking rpIdHash and UP+UV."""
    if len(raw) < 37:
        raise PasskeyError("authenticatorData is truncated")
    if not secrets.compare_digest(raw[:32], hashlib.sha256(rp_id.encode()).digest()):
        raise PasskeyError("passkey was made for a different relying party")
    flags = raw[32]
    if not flags & _FLAG_UP or not flags & _FLAG_UV:
        raise PasskeyError("passkey must verify the user (PIN or biometric)")
    (count,) = struct.unpack(">I", raw[33:37])
    return flags, count, raw[37:]


# -- registration -----------------------------------------------------------------


def registration_options(login: str, *, display_name: str | None = None) -> dict[str, Any]:
    """PublicKeyCredentialCreationOptions (JSON-safe, base64url binaries) for ``login``."""
    rp_id = config.mcp_oauth_passkey_rp_id()
    if not rp_id:
        raise PasskeyError("OAuth issuer is not configured; passkeys need an RP id")
    challenge = issue_challenge("register", binding=f"register:{login}", login=login)
    try:
        existing = list_passkeys(login)
    except Exception:  # noqa: BLE001 - excludeCredentials is advisory
        existing = []
    return {
        "challenge": challenge,
        "rp": {"id": rp_id, "name": "Unified-Agent hub"},
        "user": {
            "id": _user_handle(login),
            "name": login,
            "displayName": display_name or login,
        },
        "pubKeyCredParams": [{"type": "public-key", "alg": alg} for alg in SUPPORTED_ALGS],
        "excludeCredentials": [
            {"type": "public-key", "id": row["credential_id"]} for row in existing
        ],
        "authenticatorSelection": {"userVerification": "required", "residentKey": "preferred"},
        "attestation": "none",
        "timeout": CHALLENGE_TTL_S * 1000,
    }


def register(
    *,
    login: str,
    credential_id: str,
    client_data_json: str,
    authenticator_data: str,
    public_key: str,
    public_key_algorithm: int,
    label: str | None = None,
    registered_by: str | None = None,
) -> dict[str, Any]:
    """Verify a ``navigator.credentials.create`` result and store the passkey."""
    rp_id = config.mcp_oauth_passkey_rp_id()
    if not rp_id:
        raise PasskeyError("OAuth issuer is not configured; passkeys need an RP id")
    try:
        alg = int(public_key_algorithm)
    except (TypeError, ValueError) as exc:
        raise PasskeyError("passkey algorithm is not supported") from exc
    cred_raw = b64url_decode(credential_id)
    if not 16 <= len(cred_raw) <= 1023:
        raise PasskeyError("credential id length is invalid")
    client_raw = b64url_decode(client_data_json)
    auth_raw = b64url_decode(authenticator_data)
    spki = b64url_decode(public_key)
    _check_client_data(
        client_raw,
        ceremony="webauthn.create",
        challenge_check=lambda c: _consume_challenge(c, "register", f"register:{login}"),
    )
    flags, count, rest = _parse_auth_data(auth_raw, rp_id)
    if not flags & _FLAG_AT or len(rest) < 18:
        raise PasskeyError("registration is missing attested credential data")
    (id_len,) = struct.unpack(">H", rest[16:18])
    if not secrets.compare_digest(rest[18 : 18 + id_len], cred_raw):
        raise PasskeyError("credential id does not match the authenticator data")
    _load_public_key(spki, alg)
    canonical_id = b64url_encode(cred_raw)
    now = int(_now())
    conn = storage.connect()
    try:
        with conn:
            exists = conn.execute(
                "SELECT 1 FROM oauth_passkeys WHERE credential_id = ?", (canonical_id,)
            ).fetchone()
            if exists:
                raise PasskeyError("this passkey is already registered")
            conn.execute(
                "INSERT INTO oauth_passkeys (credential_id, login, rp_id, public_key_spki, alg,"
                " sign_count, label, registered_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    canonical_id,
                    login,
                    rp_id,
                    b64url_encode(spki),
                    alg,
                    count,
                    (label or "").strip()[:80] or None,
                    registered_by,
                    now,
                ),
            )
    finally:
        conn.close()
    return {"credential_id": canonical_id, "login": login, "rp_id": rp_id, "alg": alg}


# -- assertion ----------------------------------------------------------------------


def assertion_options(login: str, *, binding: str) -> dict[str, Any] | None:
    """PublicKeyCredentialRequestOptions for one consent/device request, or None
    when ``login`` has no passkey on this RP id."""
    rp_id = config.mcp_oauth_passkey_rp_id()
    if not rp_id:
        return None
    rows = [row for row in list_passkeys(login) if row["rp_id"] == rp_id]
    if not rows:
        return None
    return {
        "challenge": issue_challenge("assert", binding=binding, login=login),
        "rpId": rp_id,
        "allowCredentials": [{"type": "public-key", "id": row["credential_id"]} for row in rows],
        "userVerification": "required",
        "timeout": CHALLENGE_TTL_S * 1000,
    }


def verify_assertion(
    *,
    login: str,
    binding: str,
    credential_id: str,
    client_data_json: str,
    authenticator_data: str,
    signature: str,
) -> dict[str, Any]:
    """Verify a ``navigator.credentials.get`` result for ``login`` on ``binding``.

    Returns non-secret facts about the passkey used; raises ``PasskeyError``.
    """
    rp_id = config.mcp_oauth_passkey_rp_id()
    if not rp_id:
        raise PasskeyError("OAuth issuer is not configured; passkeys need an RP id")
    cred_raw = b64url_decode(credential_id)
    canonical_id = b64url_encode(cred_raw)
    client_raw = b64url_decode(client_data_json)
    auth_raw = b64url_decode(authenticator_data)
    sig_raw = b64url_decode(signature)
    consumed: list[_Challenge] = []
    _check_client_data(
        client_raw,
        ceremony="webauthn.get",
        challenge_check=lambda c: consumed.append(_consume_challenge(c, "assert", binding)),
    )
    if not consumed or consumed[0].login != login:
        raise PasskeyError("passkey challenge was issued for another approver")
    row = _load(canonical_id)
    if row is None or row["login"] != login or row["rp_id"] != rp_id:
        raise PasskeyError("passkey is not registered for this approver")
    _flags, count, _rest = _parse_auth_data(auth_raw, rp_id)
    key = _load_public_key(b64url_decode(row["public_key_spki"]), int(row["alg"]))
    message = auth_raw + hashlib.sha256(client_raw).digest()
    if not _verify_signature(key, int(row["alg"]), sig_raw, message):
        raise PasskeyError("passkey signature did not verify")
    stored = int(row["sign_count"] or 0)
    if (count or stored) and count <= stored:
        # A counter that fails to advance means a cloned authenticator.
        raise PasskeyError("passkey signature counter did not advance")
    conn = storage.connect()
    try:
        with conn:
            conn.execute(
                "UPDATE oauth_passkeys SET sign_count = ?, last_used_at = ? WHERE credential_id = ?",
                (count, int(_now()), canonical_id),
            )
    finally:
        conn.close()
    return {"credential_id": canonical_id, "label": row.get("label"), "rp_id": rp_id}
