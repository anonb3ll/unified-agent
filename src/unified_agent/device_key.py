"""Per-device Ed25519 key for ua-node enrollment off the tailnet.

``unified-agent login`` writes this key, mode 0600, beside the token file and
sends the public JWK with the RFC 8628 request. When the hub cannot resolve a
tailnet peer (a hosted hub — ``UA_TAILNET_PEER_MODE`` is forced off), the
issued token's ``device_node`` is ``device-key:<thumbprint>``. The approval
page shows that thumbprint.

Possession is a signed ``UA-Device-Proof`` header on each node call (DPoP
style: method, path, access-token hash, body hash, public key, and a unique
request ID). Valid proofs are consumed once in the hub database. A replay
is refused, including after a worker restart. A
copied token does not carry the private key, so register, heartbeat, and
claim fail. A resolved tailnet peer still binds the token; this key is not
the binding then.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PREFIX = "device-key:"
FORM_FIELD = "device_key"
PROOF_HEADER = "UA-Device-Proof"
_PROOF_VERSION = 1
_MAX_AGE_S = 120
_MAX_FUTURE_S = 60
_MAX_PROOF_CHARS = 2048
_MAX_FORM_CHARS = 512
_THUMB_RE_LEN = 43
_MAX_REPLAY_ROWS = 100_000
_MAX_DEVICE_REPLAY_ROWS = 1_000
_REPLAY_CLEANUP_LIMIT = 256

MISSING_PROOF = "this sign-in is bound to a device key; rerun unified-agent login on this device"
BAD_PROOF = "device-key proof does not match this sign-in; rerun unified-agent login on this device"
DAMAGED_KEY = "device key file is damaged; remove it and rerun unified-agent login"
REPLAYED_PROOF = (
    "device proof was already used; do not retry this request automatically; "
    "reconcile its result before starting a new request"
)
REPLAY_STORE_UNAVAILABLE = "device proof replay protection is unavailable; request refused"
REPLAY_STORE_CAPACITY = "device proof replay protection is at capacity; request refused"


class DeviceKeyError(ValueError):
    """The device key or its proof cannot be used."""


def key_path(token_path: Path) -> Path:
    """Private key file stored beside a saved sign-in."""
    return token_path.with_name(token_path.name + ".device-key")


def load_or_create(token_path: Path) -> dict[str, str]:
    """The device key for ``token_path``, creating it mode 0600 on first use.

    The same file is reused across logins so a device keeps one thumbprint.
    A damaged file is not overwritten: replacing it would orphan every token
    already bound to the old key.
    """
    path = key_path(token_path)
    if path.exists():
        return _read_private(path)
    key = _generate()
    _write_private(path, key)
    # Another first login may have published first. Both callers must use
    # that established key, never a losing generated key or an overwritten one.
    return _read_private(path)


def public_jwk_json(key: dict[str, str]) -> str:
    """Public JWK for the device-authorization form. No private scalar."""
    return json.dumps(_public_jwk(key), separators=(",", ":"), sort_keys=True)


def thumbprint(key: dict[str, str]) -> str:
    """RFC 7638 thumbprint of the public key (``crv``, ``kty``, ``x``)."""
    digest = hashlib.sha256(
        json.dumps(_public_jwk(key), separators=(",", ":"), sort_keys=True).encode()
    ).digest()
    return _b64url(digest)


def binding_from_form(raw: str) -> dict[str, str] | None:
    """``device_node`` claim for a public JWK, or None when it is not Ed25519.

    A private scalar (``d``) is refused: the form carries the public key only.
    """
    if not raw or len(raw) > _MAX_FORM_CHARS:
        return None
    try:
        jwk = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(jwk, dict) or "d" in jwk:
        return None
    tp = _thumbprint_jwk(jwk)
    if tp is None:
        return None
    return {"node": PREFIX + tp, "node_id": tp}


def is_key_binding(node: Any) -> bool:
    """True when ``node`` is a device-key ``device_node`` claim."""
    if not isinstance(node, dict):
        return False
    node_id = node.get("node_id")
    name = node.get("node")
    if not isinstance(node_id, str) or not isinstance(name, str):
        return False
    return name == PREFIX + node_id and len(node_id) == _THUMB_RE_LEN and _b64url_ok(node_id)


def sign_proof(
    key: dict[str, str],
    *,
    method: str,
    path: str,
    access_token: str,
    body: bytes,
    now: int | None = None,
) -> str:
    """A fresh, single-use proof over one new request, signed by ``key``.

    A transport must not automatically re-sign and retry an ambiguous mutation:
    it may already have run. Reconcile that outcome before starting a new call.
    """
    payload = _payload(key, method=method, path=path, access_token=access_token, body=body, now=now)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = _private_key(key).sign(raw)
    return _b64url(raw) + "." + _b64url(signature)


def proof_header(
    token_path: Path | None,
    *,
    method: str,
    path: str,
    access_token: str,
    body: bytes,
) -> str | None:
    """Proof for a saved sign-in, or None when that sign-in has no device key.

    Tailnet-bound tokens predate this file and do not send a proof. A damaged
    key file raises :class:`DeviceKeyError` rather than silently omitting it.
    """
    if token_path is None:
        return None
    path_on_disk = key_path(token_path)
    if not path_on_disk.is_file():
        return None
    return sign_proof(
        _read_private(path_on_disk),
        method=method,
        path=path,
        access_token=access_token,
        body=body,
    )


def verify_proof(
    proof: str,
    *,
    method: str,
    path: str,
    access_token: str,
    body: bytes,
    expected_node_id: str,
    now: int | None = None,
) -> None:
    """Verify and atomically consume a proof of possession of the bound key.

    Every signature and request binding is checked before touching the replay
    store. Consuming the ID precedes any queue mutation; a failed downstream
    operation still spends it, so a retry cannot repeat an ambiguous mutation.
    """
    if not proof or not proof.strip():
        raise DeviceKeyError(MISSING_PROOF)
    if len(proof) > _MAX_PROOF_CHARS or proof.count(".") != 1:
        raise DeviceKeyError(BAD_PROOF)
    encoded, encoded_sig = proof.split(".", 1)
    try:
        raw = _b64url_decode(encoded)
        signature = _b64url_decode(encoded_sig)
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        raise DeviceKeyError(BAD_PROOF) from None
    if not isinstance(payload, dict) or payload.get("v") != _PROOF_VERSION:
        raise DeviceKeyError(BAD_PROOF)
    jwk = payload.get("jwk")
    if not isinstance(jwk, dict) or "d" in jwk:
        raise DeviceKeyError(BAD_PROOF)
    got_id = _thumbprint_jwk(jwk)
    if (
        got_id is None
        or len(got_id) != len(expected_node_id)
        or not hmac.compare_digest(got_id, expected_node_id)
    ):
        raise DeviceKeyError(BAD_PROOF)
    if payload.get("htm") != method.upper() or payload.get("htu") != path:
        raise DeviceKeyError(BAD_PROOF)
    issued = payload.get("iat")
    if type(issued) is not int:
        raise DeviceKeyError(BAD_PROOF)
    request_id = payload.get("request_id")
    if (
        not isinstance(request_id, str)
        or len(request_id) != 32
        or any(ch not in "0123456789abcdef" for ch in request_id)
    ):
        raise DeviceKeyError(BAD_PROOF)
    current = int(time.time()) if now is None else now
    if issued > current + _MAX_FUTURE_S or current - issued > _MAX_AGE_S:
        raise DeviceKeyError(BAD_PROOF)
    if not _digest_matches(payload.get("ath"), access_token.encode()) or not _digest_matches(
        payload.get("bth"), body
    ):
        raise DeviceKeyError(BAD_PROOF)
    # The transmitted bytes are what was signed. Re-serializing would accept a
    # different encoding of the same object.
    expected_raw = json.dumps(
        {
            "v": _PROOF_VERSION,
            "htm": method.upper(),
            "htu": path,
            "iat": issued,
            "request_id": request_id,
            "ath": _token_hash(access_token),
            "bth": _b64url(hashlib.sha256(body).digest()),
            "jwk": _public_jwk(jwk),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if raw != expected_raw:
        raise DeviceKeyError(BAD_PROOF)
    try:
        _public_key(jwk).verify(signature, raw)
    except (InvalidSignature, ValueError):
        raise DeviceKeyError(BAD_PROOF) from None
    _consume_proof(expected_node_id, request_id, issued, current)


def _consume_proof(device_id: str, request_id: str, issued: int, current: int) -> None:
    """One durable consume across workers; bounded cleanup and retained records.

    The release's central storage schema owns ``device_proof_replays``. Missing
    or broken storage fails closed. A freshness boundary at ``iat + 120`` is
    inclusive, so its replay record expires one second after that boundary.
    Neither capacity limit evicts a still-valid fence. Expired cleanup commits
    even on refusal, so a full expired store makes bounded forward progress.
    """
    from . import storage

    conn = None
    try:
        conn = storage.connect()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM device_proof_replays WHERE rowid IN ("
            "SELECT rowid FROM device_proof_replays WHERE expires_at<=? "
            "ORDER BY expires_at LIMIT ?)",
            (current, _REPLAY_CLEANUP_LIMIT),
        )
        if conn.execute(
            "SELECT 1 FROM device_proof_replays WHERE device_id=? AND request_id=?",
            (device_id, request_id),
        ).fetchone():
            conn.commit()
            raise DeviceKeyError(REPLAYED_PROOF)
        device_count = conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM device_proof_replays WHERE device_id=? LIMIT ?)",
            (device_id, _MAX_DEVICE_REPLAY_ROWS),
        ).fetchone()[0]
        total_count = conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM device_proof_replays LIMIT ?)",
            (_MAX_REPLAY_ROWS,),
        ).fetchone()[0]
        if device_count >= _MAX_DEVICE_REPLAY_ROWS or total_count >= _MAX_REPLAY_ROWS:
            conn.commit()
            raise DeviceKeyError(REPLAY_STORE_CAPACITY)
        conn.execute(
            "INSERT INTO device_proof_replays (device_id, request_id, expires_at) VALUES (?,?,?)",
            (device_id, request_id, issued + _MAX_AGE_S + 1),
        )
        conn.commit()
    except (sqlite3.Error, OSError):
        raise DeviceKeyError(REPLAY_STORE_UNAVAILABLE) from None
    finally:
        if conn is not None:
            conn.close()


def _payload(
    key: dict[str, str],
    *,
    method: str,
    path: str,
    access_token: str,
    body: bytes,
    now: int | None,
) -> dict[str, Any]:
    return {
        "v": _PROOF_VERSION,
        "htm": method.upper(),
        "htu": path,
        "iat": int(time.time()) if now is None else now,
        "request_id": secrets.token_hex(16),
        "ath": _token_hash(access_token),
        "bth": _b64url(hashlib.sha256(body).digest()),
        "jwk": _public_jwk(key),
    }


def _token_hash(access_token: str) -> str:
    return _b64url(hashlib.sha256(access_token.encode()).digest())


def _digest_matches(got: Any, material: bytes) -> bool:
    if not isinstance(got, str):
        return False
    expected = _b64url(hashlib.sha256(material).digest())
    return len(got) == len(expected) and hmac.compare_digest(got, expected)


def _public_jwk(key: dict[str, Any]) -> dict[str, str]:
    x = key.get("x")
    if key.get("kty") not in (None, "OKP") or key.get("crv") not in (None, "Ed25519"):
        raise DeviceKeyError(DAMAGED_KEY)
    if not isinstance(x, str) or _raw_x(x) is None:
        raise DeviceKeyError(DAMAGED_KEY)
    return {"crv": "Ed25519", "kty": "OKP", "x": x}


def _thumbprint_jwk(jwk: dict[str, Any]) -> str | None:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        return None
    x = jwk.get("x")
    if not isinstance(x, str) or _raw_x(x) is None:
        return None
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True
    ).encode()
    return _b64url(hashlib.sha256(canonical).digest())


def _raw_x(x: str) -> bytes | None:
    try:
        raw = _b64url_decode(x)
    except ValueError:
        return None
    return raw if len(raw) == 32 else None


def _public_key(jwk: dict[str, Any]) -> Ed25519PublicKey:
    raw = _raw_x(str(jwk.get("x") or ""))
    if raw is None:
        raise DeviceKeyError(BAD_PROOF)
    return Ed25519PublicKey.from_public_bytes(raw)


def _private_key(key: dict[str, str]) -> Ed25519PrivateKey:
    d = key.get("d")
    if not isinstance(d, str):
        raise DeviceKeyError(DAMAGED_KEY)
    try:
        raw = _b64url_decode(d)
    except ValueError:
        raise DeviceKeyError(DAMAGED_KEY) from None
    if len(raw) != 32:
        raise DeviceKeyError(DAMAGED_KEY)
    private = Ed25519PrivateKey.from_private_bytes(raw)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if _raw_x(key.get("x") or "") != public:
        raise DeviceKeyError(DAMAGED_KEY)
    return private


def _generate() -> dict[str, str]:
    private = Ed25519PrivateKey.generate()
    d = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    x = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key = {"kty": "OKP", "crv": "Ed25519", "x": _b64url(x)}
    key["d"] = _b64url(d)
    return key


def _read_private(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise DeviceKeyError(DAMAGED_KEY) from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("x"), str)
        or not isinstance(data.get("d"), str)
    ):
        raise DeviceKeyError(DAMAGED_KEY)
    key = {"kty": "OKP", "crv": "Ed25519", "x": data["x"]}
    key["d"] = data["d"]
    # Touch the private key so a swapped ``x`` cannot sit on disk until a call.
    _private_key(key)
    return key


def _write_private(path: Path, payload: dict[str, str]) -> None:
    """Publish one complete 0600 key without replacing an established key.

    A hard link publishes the closed temporary file atomically and exclusively
    on the same filesystem. A concurrent winner (including a damaged file or
    symlink) is left untouched; the caller reads and validates that winner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump(payload, fh)
            fh.write("\n")
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(tmp)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or not _b64url_ok(value):
        raise ValueError("not base64url")
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _b64url_ok(value: str) -> bool:
    return all(ch.isalnum() or ch in "-_" for ch in value)
