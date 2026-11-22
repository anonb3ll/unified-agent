"""Detached signatures for evidence export bytes.

The default implementation deliberately uses stdlib HMAC.  Ed25519 can be
introduced later without changing the sidecar or CLI contract; HMAC is the
portable fallback for installations that do not ship a crypto package.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import managed_secrets

SCHEME_HMAC_SHA256_V1 = "hmac-sha256-v1"
SIDECAR_SUFFIX = ".sig.json"
SIGNATURE_SCHEMA = "unified-agent.evidence-export-signature"
CRYPTO_FAILURES = frozenset({"artifact digest mismatch", "signature mismatch"})


class SigningUnavailable(RuntimeError):
    """No protected signing key is configured for this process."""


def sidecar_path(artifact: str | os.PathLike[str]) -> Path:
    return Path(artifact).expanduser().with_name(Path(artifact).name + SIDECAR_SUFFIX)


def _key_material(env: Mapping[str, str] | None = None) -> tuple[bytes, str]:
    source = os.environ if env is None else env
    raw_key = source.get("UA_EVIDENCE_SIGNING_KEY", "")
    if raw_key:
        return raw_key.encode("utf-8"), source.get("UA_EVIDENCE_SIGNING_KEY_ID", "env") or "env"

    secret_name = source.get("UA_EVIDENCE_SIGNING_SECRET_NAME", "").strip()
    if secret_name:
        try:
            from .. import edition

            value = managed_secrets._load_for_consumer(
                secret_name, "evidence-signing", org_id=edition.tenant_org_id()
            )
        except Exception as exc:  # noqa: BLE001 - signing configuration becomes a clean CLI/API error
            raise SigningUnavailable(
                f"managed evidence signing secret is unavailable: {exc}"
            ) from exc
        return value.encode("utf-8"), f"managed:{secret_name}"
    raise SigningUnavailable(
        "evidence signing is not configured; set UA_EVIDENCE_SIGNING_KEY or "
        "UA_EVIDENCE_SIGNING_SECRET_NAME"
    )


def configured(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return bool(source.get("UA_EVIDENCE_SIGNING_KEY")) or bool(
        source.get("UA_EVIDENCE_SIGNING_SECRET_NAME", "").strip()
    )


def attestation_key_id(env: Mapping[str, str] | None = None) -> str | None:
    """The configured signing key id, or ``None`` when signing is unconfigured.

    Resolves only the public identifier, never loading or returning key
    material. Safe to embed in a report body (task #1267, F4 attestation
    block) the same way `report_metadata()` already does for the `signature`
    header.
    """
    source = os.environ if env is None else env
    if source.get("UA_EVIDENCE_SIGNING_KEY"):
        return source.get("UA_EVIDENCE_SIGNING_KEY_ID", "env") or "env"
    secret_name = source.get("UA_EVIDENCE_SIGNING_SECRET_NAME", "").strip()
    return f"managed:{secret_name}" if secret_name else None


def report_metadata(
    report: dict[str, Any], *, key_material: tuple[bytes, str] | None = None
) -> dict[str, str]:
    """Return public metadata that can be embedded in the exported report."""
    _, key_id = key_material or _key_material()
    chain = report.get("chain_integrity") or {}
    return {
        "scheme": SCHEME_HMAC_SHA256_V1,
        "key_id": key_id,
        "chain_head": str(chain.get("head_hash") or ""),
        "generated_at": str(report.get("generated_at") or ""),
    }


def with_report_metadata(
    report: dict[str, Any], *, key_material: tuple[bytes, str] | None = None
) -> dict[str, Any]:
    enriched = dict(report)
    enriched["signature"] = report_metadata(report, key_material=key_material)
    attestation = report.get("attestation")
    if isinstance(attestation, dict) and key_material is not None:
        attestation = dict(attestation)
        attestation["key_id"] = key_material[1]
        enriched["attestation"] = attestation
    return enriched


def sign_export(
    artifact: bytes,
    *,
    chain_head: str,
    generated_at: str,
    key_material: tuple[bytes, str] | None = None,
) -> dict[str, str]:
    key, key_id = key_material or _key_material()
    digest = hashlib.sha256(artifact).hexdigest()
    signature = hmac.new(key, artifact, hashlib.sha256).hexdigest()
    return {
        "schema": SIGNATURE_SCHEMA,
        "version": "1",
        "scheme": SCHEME_HMAC_SHA256_V1,
        "key_id": key_id,
        "artifact_sha256": digest,
        "signature": signature,
        "chain_head": chain_head,
        "generated_at": generated_at,
    }


class ExportSigner:
    """``sign_export`` computed chunk by chunk while an export is written (task #2516).

    ``update`` each chunk in order, then ``finish`` once: the sidecar equals what
    ``sign_export`` returns for the concatenated body, without holding it.
    """

    def __init__(self, *, key_material: tuple[bytes, str] | None = None) -> None:
        key, self._key_id = key_material or _key_material()
        self._digest = hashlib.sha256()
        self._mac = hmac.new(key, digestmod=hashlib.sha256)

    def update(self, chunk: bytes) -> None:
        self._digest.update(chunk)
        self._mac.update(chunk)

    def finish(self, *, chain_head: str, generated_at: str) -> dict[str, str]:
        return {
            "schema": SIGNATURE_SCHEMA,
            "version": "1",
            "scheme": SCHEME_HMAC_SHA256_V1,
            "key_id": self._key_id,
            "artifact_sha256": self._digest.hexdigest(),
            "signature": self._mac.hexdigest(),
            "chain_head": chain_head,
            "generated_at": generated_at,
        }


def verify_export(artifact: bytes, sidecar: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(sidecar, dict) or sidecar.get("schema") != SIGNATURE_SCHEMA:
        return False, "invalid sidecar schema"
    if sidecar.get("version") != "1" or sidecar.get("scheme") != SCHEME_HMAC_SHA256_V1:
        return False, "unsupported signature scheme"
    digest = sidecar.get("artifact_sha256")
    signature = sidecar.get("signature")
    if not isinstance(digest, str) or not isinstance(signature, str):
        return False, "sidecar is missing digest or signature"
    if not hmac.compare_digest(hashlib.sha256(artifact).hexdigest(), digest):
        return False, "artifact digest mismatch"
    try:
        key, key_id = _key_material()
    except SigningUnavailable as exc:
        return False, str(exc)
    if sidecar.get("key_id") != key_id:
        return False, "signing key id mismatch"
    expected = hmac.new(key, artifact, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"
    return True, "valid"


def verify_exit_code(valid: bool, reason: str) -> int:
    """Map verification to 0 valid, 1 tampered, or 2 invalid/configured wrong."""
    if valid:
        return 0
    return 1 if reason in CRYPTO_FAILURES else 2


def load_sidecar(path: str | os.PathLike[str]) -> dict[str, Any]:
    with Path(path).expanduser().open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("signature sidecar must contain a JSON object")
    return value


def dump_sidecar(path: str | os.PathLike[str], sidecar: dict[str, Any]) -> None:
    Path(path).expanduser().write_text(
        json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def response_headers(sidecar: dict[str, str]) -> dict[str, str]:
    return {
        "x-evidence-signature": sidecar["signature"],
        "x-evidence-signature-scheme": sidecar["scheme"],
        "x-evidence-signature-key-id": sidecar["key_id"],
        "x-evidence-signature-sha256": sidecar["artifact_sha256"],
        "x-evidence-signature-chain-head": sidecar["chain_head"],
        "x-evidence-signature-generated-at": sidecar["generated_at"],
    }


def encode_for_signing(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")
