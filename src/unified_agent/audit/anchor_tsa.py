"""RFC-3161 Time-Stamp Authority anchor backend (task #1265, F2).

Builds a ``TimeStampReq`` over ``sha256(head_digest_hex_bytes)``, POSTs it to
a configured TSA, and stores the returned ``TimeStampToken`` (a CMS
``SignedData`` wrapping a ``TSTInfo``). Verification is entirely offline: it
recomputes the message imprint, re-derives the DER encoding CMS signs over,
and checks the signature against a pinned TSA signer certificate the *operator* supplies
(``UA_EVIDENCE_TSA_CERT_PATH``) — never a certificate embedded in the token
itself, since that would let a network attacker or a rogue TSA forge trust.

No off-the-shelf dependency in this repo's lockfile does RFC-3161 client work
end to end, but the ASN.1 building blocks already ship transitively via
``google-auth`` -> ``pyasn1``/``pyasn1-modules`` (the latter includes
``rfc3161`` and ``rfc5652`` schemas), and ``cryptography`` already ships
(via ``workos``/``pyjwt``) for the actual signature/cert math. Both are
promoted to direct dependencies here since this module imports them by name;
nothing new was added to the lockfile.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from pyasn1.codec.der import decoder as der_decoder
from pyasn1.codec.der import encoder as der_encoder
from pyasn1.error import PyAsn1Error
from pyasn1.type import univ
from pyasn1_modules import rfc3161, rfc5280, rfc5652

from .. import config
from .anchor import AnchorBackend

_OID_SHA256 = "2.16.840.1.101.3.4.2.1"
_OID_SHA384 = "2.16.840.1.101.3.4.2.2"
_OID_SHA512 = "2.16.840.1.101.3.4.2.3"
_OID_SHA1 = "1.3.14.3.2.26"

_DIGEST_OIDS: dict[str, Any] = {
    _OID_SHA256: hashlib.sha256,
    _OID_SHA384: hashlib.sha384,
    _OID_SHA512: hashlib.sha512,
    _OID_SHA1: hashlib.sha1,
}

# signatureAlgorithm OID -> (key family, hash factory). Covers the
# combinations any mainstream public TSA (freetsa.org included) issues.
_RSA_SIG_OIDS = {
    "1.2.840.113549.1.1.5": hashlib.sha1,
    "1.2.840.113549.1.1.11": hashlib.sha256,
    "1.2.840.113549.1.1.12": hashlib.sha384,
    "1.2.840.113549.1.1.13": hashlib.sha512,
}
_ECDSA_SIG_OIDS = {
    "1.2.840.10045.4.3.2": hashlib.sha256,
    "1.2.840.10045.4.3.3": hashlib.sha384,
    "1.2.840.10045.4.3.4": hashlib.sha512,
}

REQUEST_CONTENT_TYPE = "application/timestamp-query"
RESPONSE_CONTENT_TYPE = "application/timestamp-reply"


def _algorithm_identifier(oid: str) -> rfc5280.AlgorithmIdentifier:
    alg = rfc5280.AlgorithmIdentifier()
    alg["algorithm"] = univ.ObjectIdentifier(oid)
    alg["parameters"] = der_encoder.encode(univ.Null(""))
    return alg


def message_imprint_digest(head_digest: str) -> bytes:
    """The bytes a TSA hashes: sha256 over the head digest's UTF-8 hex text.

    Documented here because it is the one detail a third party must match
    exactly to reproduce verification by hand (see docs/evidence-report.md).
    """
    return hashlib.sha256(head_digest.encode("utf-8")).digest()


def build_timestamp_request(head_digest: str, *, nonce: int, cert_req: bool = True) -> bytes:
    """Build a DER-encoded RFC-3161 ``TimeStampReq`` for ``head_digest``."""
    imprint = rfc3161.MessageImprint()
    imprint["hashAlgorithm"] = _algorithm_identifier(_OID_SHA256)
    imprint["hashedMessage"] = univ.OctetString(message_imprint_digest(head_digest))

    req = rfc3161.TimeStampReq()
    req["version"] = 1
    req["messageImprint"] = imprint
    req["nonce"] = univ.Integer(nonce)
    req["certReq"] = bool(cert_req)
    return der_encoder.encode(req)


def _decode_token(token_der: bytes) -> tuple[Any, Any]:
    """Decode a ``TimeStampToken`` (CMS ``ContentInfo``/``SignedData``).

    Returns ``(signed_data, tst_info)``.
    """
    try:
        content_info, rest = der_decoder.decode(
            token_der, asn1Spec=rfc5652.ContentInfo(), decodeOpenTypes=True
        )
    except PyAsn1Error as exc:
        raise ToolError(f"TSA token is not valid DER/ASN.1: {exc}") from exc
    if rest:
        raise ToolError("TSA token has trailing bytes after the ContentInfo")
    if str(content_info["contentType"]) != str(rfc5652.id_signedData):
        raise ToolError("TSA token is not a CMS SignedData structure")
    signed_data = content_info["content"]
    econtent_type = str(signed_data["encapContentInfo"]["eContentType"])
    if econtent_type != str(rfc3161.id_ct_TSTInfo):
        raise ToolError("TSA token does not encapsulate a TSTInfo")
    econtent = signed_data["encapContentInfo"]["eContent"]
    if not econtent.isValue:
        raise ToolError("TSA token has no eContent to verify")
    try:
        tst_info, _ = der_decoder.decode(bytes(econtent), asn1Spec=rfc3161.TSTInfo())
    except PyAsn1Error as exc:
        raise ToolError(f"TSA token TSTInfo is malformed: {exc}") from exc
    return signed_data, tst_info


def parse_timestamp_response(
    resp_bytes: bytes, *, expected_nonce: int, expected_imprint: bytes
) -> bytes:
    """Parse a ``TimeStampResp``, validate it against the request, return the token DER."""
    try:
        resp, rest = der_decoder.decode(resp_bytes, asn1Spec=rfc3161.TimeStampResp())
    except PyAsn1Error as exc:
        raise ToolError(
            f"TSA reply is not a valid RFC-3161 TimeStampResp (is the URL a TSA?): {exc}"
        ) from exc
    if rest:
        raise ToolError("TSA reply has trailing bytes after the TimeStampResp")
    status = int(resp["status"]["status"])
    if status not in (0, 1):  # granted, grantedWithMods
        fail_info_field = resp["status"]["failInfo"]
        fail_info = fail_info_field if fail_info_field.isValue else None
        status_string_field = resp["status"]["statusString"]
        status_string = status_string_field if status_string_field.isValue else None
        raise ToolError(
            f"TSA request was not granted: status={status} failInfo={fail_info} "
            f"statusString={status_string}"
        )
    token = resp["timeStampToken"]
    if not token.isValue:
        raise ToolError("TSA response is missing timeStampToken")
    token_der = der_encoder.encode(token)

    _, tst_info = _decode_token(token_der)
    imprint = bytes(tst_info["messageImprint"]["hashedMessage"])
    if imprint != expected_imprint:
        raise ToolError("TSA response messageImprint does not match the request")
    nonce_field = tst_info["nonce"]
    if nonce_field.isValue and int(nonce_field) != expected_nonce:
        raise ToolError("TSA response nonce does not match the request")
    return token_der


def _retag_as_set(der_bytes: bytes) -> bytes:
    """Re-tag an ``IMPLICIT [0]`` SET encoding back to a universal SET (0x31).

    CMS signs ``signedAttrs`` as if it were an ordinary ``SET OF Attribute``
    even though it is carried IMPLICIT-tagged ``[0]`` inside ``SignerInfo``
    (RFC 5652 §5.4). This is the standard re-tagging trick every CMS
    implementation applies before hashing/verifying that field.
    """
    return b"\x31" + der_bytes[1:]


def _candidate_certs(cert_pem: bytes) -> list[Any]:
    from cryptography import x509

    try:
        return list(x509.load_pem_x509_certificates(cert_pem))
    except ValueError as exc:
        raise ToolError(f"could not parse TSA certificate/chain PEM: {exc}") from exc


def verify_timestamp_token(token_der: bytes, head_digest: str, cert_pem: bytes) -> dict[str, Any]:
    """Verify a stored TSA token against ``head_digest`` using an operator-supplied cert/chain.

    Trust runs *only* through ``cert_pem`` — certificates embedded in the
    token are read for the signer's algorithm choice but never treated as
    trust anchors.
    """
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    try:
        signed_data, tst_info = _decode_token(token_der)
    except ToolError as exc:
        return {"verified": False, "reason": f"malformed token: {exc}", "detail": {}}

    expected_imprint = message_imprint_digest(head_digest)
    actual_imprint = bytes(tst_info["messageImprint"]["hashedMessage"])
    if actual_imprint != expected_imprint:
        return {
            "verified": False,
            "reason": "message_imprint_mismatch",
            "detail": {"expected": expected_imprint.hex(), "actual": actual_imprint.hex()},
        }

    signer_infos = signed_data["signerInfos"]
    if len(signer_infos) < 1:
        return {"verified": False, "reason": "token has no signerInfo", "detail": {}}
    signer_info = signer_infos[0]

    econtent = bytes(signed_data["encapContentInfo"]["eContent"])
    digest_oid = str(signer_info["digestAlgorithm"]["algorithm"])
    digest_fn = _DIGEST_OIDS.get(digest_oid)
    if digest_fn is None:
        return {
            "verified": False,
            "reason": f"unsupported digestAlgorithm {digest_oid}",
            "detail": {},
        }

    signed_attrs = signer_info["signedAttrs"]
    if not signed_attrs.isValue:
        return {"verified": False, "reason": "token has no signedAttrs (unsupported)", "detail": {}}

    message_digest_attr = None
    content_type_attrs: list[str] = []
    for attr in signed_attrs:
        if str(attr["attrType"]) == "1.2.840.113549.1.9.3":  # id-contentType
            for raw_value in attr["attrValues"]:
                try:
                    oid = (
                        raw_value
                        if isinstance(raw_value, univ.ObjectIdentifier)
                        else der_decoder.decode(bytes(raw_value), asn1Spec=univ.ObjectIdentifier())[
                            0
                        ]
                    )
                    content_type_attrs.append(str(oid))
                except Exception:  # noqa: BLE001 - malformed attribute fails verification
                    return {
                        "verified": False,
                        "reason": "signed_attrs_content_type_invalid",
                        "detail": {},
                    }
        if str(attr["attrType"]) == "1.2.840.113549.1.9.4":  # id-messageDigest
            raw_value = attr["attrValues"][0]
            message_digest_attr = (
                bytes(raw_value)
                if isinstance(raw_value, univ.OctetString)
                else bytes(der_decoder.decode(bytes(raw_value), asn1Spec=univ.OctetString())[0])
            )
            break
    if message_digest_attr is None:
        return {"verified": False, "reason": "signedAttrs missing message-digest", "detail": {}}
    if content_type_attrs != [str(signed_data["encapContentInfo"]["eContentType"])]:
        return {"verified": False, "reason": "signed_attrs_content_type_mismatch", "detail": {}}
    if message_digest_attr != digest_fn(econtent).digest():
        return {"verified": False, "reason": "signed_attrs_digest_mismatch", "detail": {}}

    to_verify = _retag_as_set(der_encoder.encode(signed_attrs))
    signature = bytes(signer_info["signature"])
    sig_oid = str(signer_info["signatureAlgorithm"]["algorithm"])

    certs = _candidate_certs(cert_pem)
    if not certs:
        raise ToolError("TSA cert file contains no certificates")

    matched_cert = None
    verify_error: Exception | None = None
    for cert in certs:
        public_key = cert.public_key()
        try:
            if sig_oid in _RSA_SIG_OIDS and isinstance(public_key, rsa.RSAPublicKey):
                hash_cls = _hash_for_hashlib(_RSA_SIG_OIDS[sig_oid])
                public_key.verify(signature, to_verify, padding.PKCS1v15(), hash_cls())
            elif sig_oid in _ECDSA_SIG_OIDS and isinstance(public_key, ec.EllipticCurvePublicKey):
                hash_cls = _hash_for_hashlib(_ECDSA_SIG_OIDS[sig_oid])
                public_key.verify(signature, to_verify, ec.ECDSA(hash_cls()))
            else:
                continue
        except Exception as exc:  # noqa: BLE001 - any crypto failure means "try next cert"
            verify_error = exc
            continue
        matched_cert = cert
        break

    if matched_cert is None:
        reason = (
            "signature_invalid"
            if verify_error
            else "no supplied certificate matches signature algorithm"
        )
        return {"verified": False, "reason": reason, "detail": {"signature_algorithm_oid": sig_oid}}

    identity_problem = _signer_identity_problem(signer_info, matched_cert, tst_info)
    if identity_problem:
        return {
            "verified": False,
            "reason": identity_problem,
            "detail": {"tsa_subject": matched_cert.subject.rfc4514_string()},
        }

    return {
        "verified": True,
        "reason": "signature verified against supplied TSA certificate",
        "detail": {
            "gen_time": str(tst_info["genTime"]),
            "serial_number": str(tst_info["serialNumber"]),
            "policy": str(tst_info["policy"]),
            "tsa_subject": matched_cert.subject.rfc4514_string(),
            "digest_algorithm_oid": digest_oid,
            "signature_algorithm_oid": sig_oid,
        },
    }


_TIMESTAMPING_EKU = "1.3.6.1.5.5.7.3.8"


def _signer_identity_problem(signer_info: Any, cert: Any, tst_info: Any) -> str | None:
    """Why ``cert`` is not an acceptable signer for this token, or None if it is.

    The configured PEM is a pinned trust anchor, so a key that verifies the
    signature is necessary but not sufficient. The certificate must also be
    the one the SignerInfo names, carry the id-kp-timeStamping extended key
    usage (RFC 3161 §2.3), and have been valid at the token's genTime.
    """
    from datetime import datetime

    from cryptography import x509

    sid = signer_info["sid"]
    if sid.getName() == "issuerAndSerialNumber":
        serial = int(sid["issuerAndSerialNumber"]["serialNumber"])
        if serial != cert.serial_number:
            return "signer_mismatch: SignerInfo names a different certificate serial"
    elif sid.getName() == "subjectKeyIdentifier":
        try:
            ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
        except x509.ExtensionNotFound:
            return "signer_mismatch: certificate has no subjectKeyIdentifier to match"
        if bytes(sid["subjectKeyIdentifier"]) != ski:
            return "signer_mismatch: SignerInfo names a different subjectKeyIdentifier"

    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return "tsa_cert_not_for_timestamping: no extendedKeyUsage"
    if _TIMESTAMPING_EKU not in {usage.dotted_string for usage in eku}:
        return "tsa_cert_not_for_timestamping: extendedKeyUsage lacks id-kp-timeStamping"

    try:
        gen_time = datetime.strptime(str(tst_info["genTime"])[:14], "%Y%m%d%H%M%S").replace(
            tzinfo=UTC
        )
    except ValueError:
        return "malformed genTime"
    if not (cert.not_valid_before_utc <= gen_time <= cert.not_valid_after_utc):
        return "tsa_cert_not_valid_at_gen_time"
    return None


def _hash_for_hashlib(hashlib_fn: Any) -> Any:
    from cryptography.hazmat.primitives import hashes

    mapping = {
        hashlib.sha1: hashes.SHA1,
        hashlib.sha256: hashes.SHA256,
        hashlib.sha384: hashes.SHA384,
        hashlib.sha512: hashes.SHA512,
    }
    return mapping[hashlib_fn]


class TsaAnchorBackend(AnchorBackend):
    """RFC-3161 Time-Stamp Authority witness."""

    name = "tsa"
    MAX_RESPONSE_BYTES = 1_048_576

    def anchor(self, head_digest: str, *, day: str | None = None) -> dict[str, Any]:
        import secrets
        from datetime import UTC, datetime

        url = config.evidence_tsa_url()
        if not url:
            raise ToolError("UA_EVIDENCE_TSA_URL is not configured")
        nonce = secrets.randbits(63)
        imprint = message_imprint_digest(head_digest)
        request_der = build_timestamp_request(head_digest, nonce=nonce)
        try:
            with httpx.stream(
                "POST",
                url,
                content=request_der,
                headers={
                    "Content-Type": REQUEST_CONTENT_TYPE,
                    "Accept": RESPONSE_CONTENT_TYPE,
                },
                timeout=config.evidence_tsa_timeout_s(),
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > self.MAX_RESPONSE_BYTES:
                        raise ToolError("TSA response exceeds 1 MiB limit")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ToolError(f"TSA request to {url} failed: {exc}") from exc
        token_der = parse_timestamp_response(
            b"".join(chunks), expected_nonce=nonce, expected_imprint=imprint
        )

        ref: dict[str, Any] = {
            "backend": self.name,
            "tsa_url": url,
            "token_b64": base64.b64encode(token_der).decode("ascii"),
            "head_hash": head_digest,
            "anchored_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

        cert_path = config.evidence_tsa_cert_path()
        if cert_path and cert_path.is_file():
            verification = verify_timestamp_token(token_der, head_digest, cert_path.read_bytes())
            ref["verified_at_creation"] = verification["verified"]

        return ref

    def verify(self, ref: dict[str, Any], head_digest: str) -> dict[str, Any]:
        cert_path = config.evidence_tsa_cert_path()
        if cert_path is None:
            raise ToolError(
                "UA_EVIDENCE_TSA_CERT_PATH is not configured; supply a PEM containing the pinned TSA signer certificate "
                "to verify a tsa anchor"
            )
        if not cert_path.is_file():
            raise ToolError(f"TSA cert file not found: {cert_path}")
        token_b64 = ref.get("token_b64")
        if not token_b64:
            return {"verified": False, "reason": "anchor ref missing token_b64", "detail": {}}
        try:
            token_der = base64.b64decode(token_b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            return {
                "verified": False,
                "reason": f"anchor ref token is not valid base64: {exc}",
                "detail": {},
            }
        return verify_timestamp_token(token_der, head_digest, cert_path.read_bytes())

    def verification_recipe(self, ref: dict[str, Any]) -> str:
        return (
            "Verify `token_b64` against `anchor.anchored_head` with `unified-agent evidence verify` "
            "on the JSON export. Set UA_EVIDENCE_TSA_CERT_PATH to a PEM containing the pinned "
            "TSA signer certificate; a CA-only PEM is insufficient. If "
            "`chain_integrity.head_hash` differs, the later ledger link also needs verification."
        )
