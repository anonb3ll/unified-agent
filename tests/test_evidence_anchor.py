"""Tests for Task #1265 (Epic F, F2): pluggable chain-head anchor backends.

Covers the ``vault`` (legacy/default), ``tsa`` (RFC-3161), and ``git``
(signed commit) backends behind ``audit/anchor.py``: anchor/verify round
trips, the evidence-report header, ``unified-agent evidence verify``'s anchor
check, and — per the task's DoD — a one-byte tamper (in the head digest, and
in the anchor's own token/ref) making verification fail for every backend
that carries a checkable payload.

The TSA fixtures build a throwaway CA + TSA leaf certificate and a real,
structurally valid ``TimeStampResp``/CMS ``SignedData`` token in-process
(rather than committing a binary fixture) so the test suite has no external
binary blob to keep in sync and stays fully deterministic; the HTTP POST is
always mocked — no network call happens in this file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pyasn1.codec.der import decoder as der_decoder
from pyasn1.codec.der import encoder as der_encoder
from pyasn1.type import univ, useful
from pyasn1_modules import rfc3161, rfc5280, rfc5652

from unified_agent import config, events, storage
from unified_agent.audit import (
    anchor,
    anchor_git,
    anchor_tsa,
    evidence_cli,
    evidence_report,
    ledger_cli,
)


@pytest.fixture(autouse=True)
def init_clean_db(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    yield


def _head_digest() -> str:
    events.record_event("agent.task.updated", payload={"task_id": 1})
    return events.verify_event_chain()["head_hash"]


# --------------------------------------------------------------------------
# TSA fixture: throwaway CA + TSA leaf cert, and a hand-built CMS token.
# --------------------------------------------------------------------------


def _build_tsa_identity():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Anchor CA")])
    now = datetime.now(UTC)
    _ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now.replace(microsecond=0))
        .not_valid_after(now.replace(microsecond=0).replace(year=now.year + 5))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    tsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tsa_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test TSA")])
    tsa_cert = (
        x509.CertificateBuilder()
        .subject_name(tsa_name)
        .issuer_name(ca_name)
        .public_key(tsa_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now.replace(microsecond=0))
        .not_valid_after(now.replace(microsecond=0).replace(year=now.year + 5))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    cert_pem = tsa_cert.public_bytes(serialization.Encoding.PEM)
    return tsa_key, tsa_cert, cert_pem


_SHA256_OID = "2.16.840.1.101.3.4.2.1"
_ID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
_ID_CONTENT_TYPE_ATTR = "1.2.840.113549.1.9.3"
_SHA256_RSA_OID = "1.2.840.113549.1.1.11"


def _alg_id(oid: str) -> rfc5280.AlgorithmIdentifier:
    alg = rfc5280.AlgorithmIdentifier()
    alg["algorithm"] = univ.ObjectIdentifier(oid)
    alg["parameters"] = der_encoder.encode(univ.Null(""))
    return alg


def _build_timestamp_resp(
    *,
    head_digest: str,
    nonce: int,
    tsa_key,
    tsa_cert,
    status: int = 0,
    signed_content_type: str | None = None,
) -> bytes:
    """Build a real TimeStampResp/CMS SignedData token for ``head_digest``."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    imprint_digest = anchor_tsa.message_imprint_digest(head_digest)

    mi = rfc3161.MessageImprint()
    mi["hashAlgorithm"] = _alg_id(_SHA256_OID)
    mi["hashedMessage"] = univ.OctetString(imprint_digest)

    tst = rfc3161.TSTInfo()
    tst["version"] = 1
    tst["policy"] = univ.ObjectIdentifier("1.2.3.4.5")
    tst["messageImprint"] = mi
    tst["serialNumber"] = univ.Integer(424242)
    # Within the fixture cert's validity window (valid from "now").
    tst["genTime"] = useful.GeneralizedTime(
        (datetime.now(UTC) + timedelta(minutes=1)).strftime("%Y%m%d%H%M%SZ")
    )
    tst["ordering"] = False
    tst["nonce"] = univ.Integer(nonce)
    tst_der = der_encoder.encode(tst)

    econtent_digest = hashlib.sha256(tst_der).digest()

    eci = rfc5652.EncapsulatedContentInfo()
    eci["eContentType"] = univ.ObjectIdentifier(str(rfc3161.id_ct_TSTInfo))
    eci["eContent"] = eci.getComponentType()["eContent"].asn1Object.clone(tst_der)

    attrs = rfc5652.SignedAttributes()
    a1 = rfc5652.Attribute()
    a1["attrType"] = univ.ObjectIdentifier(_ID_CONTENT_TYPE_ATTR)
    a1["attrValues"].append(
        rfc5652.AttributeValue(
            der_encoder.encode(
                univ.ObjectIdentifier(signed_content_type or str(rfc3161.id_ct_TSTInfo))
            )
        )
    )
    a2 = rfc5652.Attribute()
    a2["attrType"] = univ.ObjectIdentifier(_ID_MESSAGE_DIGEST)
    a2["attrValues"].append(
        rfc5652.AttributeValue(der_encoder.encode(univ.OctetString(econtent_digest)))
    )
    attrs.append(a1)
    attrs.append(a2)

    to_sign = anchor_tsa._retag_as_set(der_encoder.encode(attrs))
    signature = tsa_key.sign(to_sign, padding.PKCS1v15(), hashes.SHA256())

    signer_info = rfc5652.SignerInfo()
    signer_info["version"] = 1
    ias = rfc5652.IssuerAndSerialNumber()
    issuer_der = tsa_cert.issuer.public_bytes()
    issuer_name, _ = der_decoder.decode(issuer_der, asn1Spec=rfc5280.Name())
    ias["issuer"] = issuer_name
    ias["serialNumber"] = univ.Integer(tsa_cert.serial_number)
    sid = rfc5652.SignerIdentifier()
    sid["issuerAndSerialNumber"] = ias
    signer_info["sid"] = sid
    signer_info["digestAlgorithm"] = _alg_id(_SHA256_OID)
    sa_field = signer_info.getComponentType()["signedAttrs"].asn1Object.clone()
    for a in attrs:
        sa_field.append(a)
    signer_info["signedAttrs"] = sa_field
    signer_info["signatureAlgorithm"] = _alg_id(_SHA256_RSA_OID)
    signer_info["signature"] = univ.OctetString(signature)

    signed_data = rfc5652.SignedData()
    signed_data["version"] = 3
    signed_data["digestAlgorithms"].append(_alg_id(_SHA256_OID))
    signed_data["encapContentInfo"] = eci
    cert_der = tsa_cert.public_bytes(serialization.Encoding.DER)
    cert_asn1, _ = der_decoder.decode(cert_der, asn1Spec=rfc5280.Certificate())
    cert_choice = rfc5652.CertificateChoices()
    cert_choice["certificate"] = cert_asn1
    signed_data["certificates"].append(cert_choice)
    signed_data["signerInfos"].append(signer_info)

    content_info = rfc5652.ContentInfo()
    content_info["contentType"] = univ.ObjectIdentifier(str(rfc5652.id_signedData))
    content_info["content"] = der_encoder.encode(signed_data)

    status_info = rfc3161.PKIStatusInfo()
    status_info["status"] = status
    resp = rfc3161.TimeStampResp()
    resp["status"] = status_info
    resp["timeStampToken"] = content_info
    return der_encoder.encode(resp)


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def iter_bytes(self):
        yield self.content


def _patch_tsa_post(monkeypatch, resp_bytes: bytes):
    calls: list[bytes] = []

    def _fake_post(method, url, *, content, headers, timeout):
        assert method == "POST"
        calls.append(content)
        return _FakeResponse(resp_bytes)

    monkeypatch.setattr(anchor_tsa.httpx, "stream", _fake_post)
    return calls


# --------------------------------------------------------------------------
# TSA backend
# --------------------------------------------------------------------------


def test_tsa_anchor_and_verify_round_trip(monkeypatch, tmp_path):
    head = _head_digest()
    tsa_key, tsa_cert, cert_pem = _build_tsa_identity()
    cert_path = tmp_path / "tsa.pem"
    cert_path.write_bytes(cert_pem)

    monkeypatch.setenv("UA_EVIDENCE_TSA_URL", "https://example.invalid/tsr")
    monkeypatch.setenv("UA_EVIDENCE_TSA_CERT_PATH", str(cert_path))

    # Patch build_timestamp_request's nonce indirectly by capturing whatever
    # nonce the backend picked, then answering with a response for that nonce.
    captured = {}
    real_build = anchor_tsa.build_timestamp_request

    def _capture_build(head_digest, *, nonce, cert_req=True):
        captured["nonce"] = nonce
        captured["head_digest"] = head_digest
        return real_build(head_digest, nonce=nonce, cert_req=cert_req)

    monkeypatch.setattr(anchor_tsa, "build_timestamp_request", _capture_build)

    def _fake_post(method, url, *, content, headers, timeout):
        assert method == "POST"
        resp_bytes = _build_timestamp_resp(
            head_digest=captured["head_digest"],
            nonce=captured["nonce"],
            tsa_key=tsa_key,
            tsa_cert=tsa_cert,
        )
        return _FakeResponse(resp_bytes)

    monkeypatch.setattr(anchor_tsa.httpx, "stream", _fake_post)

    backend = anchor_tsa.TsaAnchorBackend()
    ref = backend.anchor(head)
    assert ref["backend"] == "tsa"
    assert ref["head_hash"] == head
    assert ref["verified_at_creation"] is True

    result = backend.verify(ref, head)
    assert result["verified"] is True
    assert result["detail"]["gen_time"]
    assert result["detail"]["tsa_subject"] == "CN=Test TSA"


def test_tsa_rejects_signed_content_type_mismatch():
    head = _head_digest()
    tsa_key, tsa_cert, cert_pem = _build_tsa_identity()
    token = anchor_tsa.parse_timestamp_response(
        _build_timestamp_resp(
            head_digest=head,
            nonce=7,
            tsa_key=tsa_key,
            tsa_cert=tsa_cert,
            signed_content_type="1.2.840.113549.1.7.1",
        ),
        expected_nonce=7,
        expected_imprint=anchor_tsa.message_imprint_digest(head),
    )
    result = anchor_tsa.verify_timestamp_token(token, head, cert_pem)
    assert result["verified"] is False
    assert result["reason"] == "signed_attrs_content_type_mismatch"


def test_tsa_verify_fails_without_configured_cert(monkeypatch, tmp_path):
    head = _head_digest()
    tsa_key, tsa_cert, _cert_pem = _build_tsa_identity()
    monkeypatch.delenv("UA_EVIDENCE_TSA_CERT_PATH", raising=False)
    token_der = anchor_tsa.parse_timestamp_response(
        _build_timestamp_resp(head_digest=head, nonce=7, tsa_key=tsa_key, tsa_cert=tsa_cert),
        expected_nonce=7,
        expected_imprint=anchor_tsa.message_imprint_digest(head),
    )
    ref = {"backend": "tsa", "token_b64": base64.b64encode(token_der).decode(), "head_hash": head}
    with pytest.raises(ToolError, match="UA_EVIDENCE_TSA_CERT_PATH"):
        anchor_tsa.TsaAnchorBackend().verify(ref, head)


def test_tsa_one_byte_tamper_in_head_digest_fails(monkeypatch, tmp_path):
    head = _head_digest()
    tsa_key, tsa_cert, cert_pem = _build_tsa_identity()
    cert_path = tmp_path / "tsa.pem"
    cert_path.write_bytes(cert_pem)
    monkeypatch.setenv("UA_EVIDENCE_TSA_CERT_PATH", str(cert_path))

    resp_bytes = _build_timestamp_resp(
        head_digest=head, nonce=11, tsa_key=tsa_key, tsa_cert=tsa_cert
    )
    token_der = anchor_tsa.parse_timestamp_response(
        resp_bytes, expected_nonce=11, expected_imprint=anchor_tsa.message_imprint_digest(head)
    )
    ref = {"backend": "tsa", "token_b64": base64.b64encode(token_der).decode(), "head_hash": head}

    good = anchor_tsa.TsaAnchorBackend().verify(ref, head)
    assert good["verified"] is True

    # Flip exactly one character in the middle of the head digest hex string.
    tampered_head = head[:10] + ("0" if head[10] != "0" else "1") + head[11:]
    bad = anchor_tsa.TsaAnchorBackend().verify(ref, tampered_head)
    assert bad["verified"] is False
    assert bad["reason"] == "message_imprint_mismatch"


def test_tsa_one_byte_tamper_in_token_fails(monkeypatch, tmp_path):
    head = _head_digest()
    tsa_key, tsa_cert, cert_pem = _build_tsa_identity()
    cert_path = tmp_path / "tsa.pem"
    cert_path.write_bytes(cert_pem)
    monkeypatch.setenv("UA_EVIDENCE_TSA_CERT_PATH", str(cert_path))

    resp_bytes = _build_timestamp_resp(
        head_digest=head, nonce=13, tsa_key=tsa_key, tsa_cert=tsa_cert
    )
    token_der = anchor_tsa.parse_timestamp_response(
        resp_bytes, expected_nonce=13, expected_imprint=anchor_tsa.message_imprint_digest(head)
    )

    good = anchor_tsa.verify_timestamp_token(token_der, head, cert_pem)
    assert good["verified"] is True

    # Flip the last byte: the signature field is the last thing in the DER
    # encoding (no unsignedAttrs follow it), so this tampers the signature
    # octets specifically without corrupting the outer ASN.1 structure.
    tampered_token = token_der[:-1] + bytes([token_der[-1] ^ 0x01])
    bad = anchor_tsa.verify_timestamp_token(tampered_token, head, cert_pem)
    assert bad["verified"] is False


def test_tsa_request_rejected_status_raises(monkeypatch, tmp_path):
    head = _head_digest()
    resp = rfc3161.TimeStampResp()
    status_info = rfc3161.PKIStatusInfo()
    status_info["status"] = 2  # rejection
    resp["status"] = status_info
    resp_bytes = der_encoder.encode(resp)
    monkeypatch.setenv("UA_EVIDENCE_TSA_URL", "https://example.invalid/tsr")
    _patch_tsa_post(monkeypatch, resp_bytes)

    with pytest.raises(ToolError, match="not granted"):
        anchor_tsa.TsaAnchorBackend().anchor(head)


def test_tsa_response_over_limit_is_rejected_before_parsing(monkeypatch):
    head = _head_digest()
    monkeypatch.setattr(
        anchor_tsa.httpx,
        "stream",
        lambda *args, **kwargs: _FakeResponse(
            b"x" * (anchor_tsa.TsaAnchorBackend.MAX_RESPONSE_BYTES + 1)
        ),
    )
    with pytest.raises(ToolError, match="exceeds 1 MiB"):
        anchor_tsa.TsaAnchorBackend().anchor(head)


def test_git_verify_timeout_is_a_controlled_error(monkeypatch, tmp_path):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(anchor_git.subprocess, "run", timed_out)
    with pytest.raises(ToolError, match="timed out after 5s"):
        anchor_git._run_git(tmp_path, ["verify-commit", "abc"], timeout_s=5)


def test_git_missing_executable_is_a_controlled_error(monkeypatch, tmp_path):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(anchor_git.subprocess, "run", missing)
    with pytest.raises(ToolError, match="could not start"):
        anchor_git._run_git(tmp_path, ["verify-commit", "abc"])


def test_tsa_never_hits_the_network(monkeypatch, tmp_path):
    """Guard against a future change accidentally letting a real request through."""
    head = _head_digest()

    def _boom(*args, **kwargs):
        raise AssertionError("no network call is allowed in tests")

    monkeypatch.setattr(httpx, "post", _boom)
    monkeypatch.setattr(anchor_tsa.httpx, "stream", _boom)
    tsa_key, tsa_cert, cert_pem = _build_tsa_identity()

    def _fake_post(method, url, *, content, headers, timeout):
        assert method == "POST"
        req, _ = der_decoder.decode(content, asn1Spec=rfc3161.TimeStampReq())
        nonce = int(req["nonce"])
        return _FakeResponse(
            _build_timestamp_resp(head_digest=head, nonce=nonce, tsa_key=tsa_key, tsa_cert=tsa_cert)
        )

    monkeypatch.setattr(anchor_tsa.httpx, "stream", _fake_post)
    ref = anchor_tsa.TsaAnchorBackend().anchor(head)
    assert ref["head_hash"] == head


# --------------------------------------------------------------------------
# git backend
# --------------------------------------------------------------------------


def _init_witness_repo(path, *, signed: bool = True) -> None:
    """A witness repo that signs commits with a throwaway SSH key (or doesn't).

    SSH signing needs only ssh-keygen, so the round trip proves a real
    `git verify-commit` pass without depending on a GPG agent.
    """
    path.mkdir(parents=True, exist_ok=True)

    def cfg(*args):
        subprocess.run(["git", "config", *args], cwd=path, check=True)

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    cfg("user.email", "witness@example.invalid")
    cfg("user.name", "Witness Repo")
    if not signed:
        # Deterministically unsigned regardless of the host's global gitconfig.
        cfg("commit.gpgsign", "false")
        return
    key = path.parent / f"{path.name}-signing-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "witness", "-f", str(key)],
        check=True,
    )
    allowed = path.parent / f"{path.name}-allowed-signers"
    allowed.write_text(
        "witness@example.invalid " + (key.with_suffix(".pub")).read_text(), encoding="utf-8"
    )
    cfg("gpg.format", "ssh")
    cfg("user.signingkey", str(key))
    cfg("gpg.ssh.allowedSignersFile", str(allowed))
    cfg("commit.gpgsign", "true")


def _pin_witness_signer(monkeypatch, repo) -> str:
    output = subprocess.run(
        ["ssh-keygen", "-lf", str(repo.parent / f"{repo.name}-signing-key.pub")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fingerprint = output.split()[1]
    monkeypatch.setenv("UA_EVIDENCE_ANCHOR_GIT_SIGNER_FINGERPRINT", fingerprint)
    return fingerprint


def test_git_anchor_and_verify_round_trip(tmp_path, monkeypatch):
    head = _head_digest()
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo)
    _pin_witness_signer(monkeypatch, repo)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)

    backend = anchor_git.GitAnchorBackend()
    ref = backend.anchor(head, day="2026-09-06")
    assert ref["anchor_action"] == "created"
    assert ref["repo"] == str(repo)
    assert (repo / ref["path"]).is_file()

    result = backend.verify(ref, head)
    assert result["verified"] is True
    assert result["detail"]["signature_status"] == "valid"

    # A same-day re-anchor of an unchanged head is a no-op, like the vault backend.
    ref2 = backend.anchor(head, day="2026-09-06")
    assert ref2["anchor_action"] == "unchanged"
    assert ref2["commit_sha"] == ref["commit_sha"]


def test_git_verification_rejects_unconfigured_repo_before_running_git(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted"
    foreign = tmp_path / "foreign"
    trusted.mkdir()
    foreign.mkdir()
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: trusted)

    def forbidden(*args, **kwargs):
        raise AssertionError("untrusted repo must never execute git")

    monkeypatch.setattr(anchor_git, "_run_git", forbidden)
    ref = {"repo": str(foreign), "path": "anchors/x.json", "commit_sha": "a" * 40}
    result = anchor_git.GitAnchorBackend().verify(ref, "b" * 64)
    assert result["verified"] is False
    assert "configured witness" in result["reason"]


def test_git_anchor_refuses_pre_staged_files(tmp_path, monkeypatch):
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo, signed=False)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    secret = repo / "unrelated.txt"
    secret.write_text("operator material")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=repo, check=True)
    with pytest.raises(ToolError, match="staged changes"):
        anchor_git.GitAnchorBackend().anchor("a" * 64, day="2026-09-06")
    assert (
        subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "unrelated.txt"
    )


def test_git_anchor_retries_after_signing_is_corrected(tmp_path, monkeypatch):
    head = "a" * 64
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo, signed=False)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    backend = anchor_git.GitAnchorBackend()
    unsigned = backend.anchor(head, day="2026-09-06")
    assert backend.verify(unsigned, head)["verified"] is False

    _init_witness_repo(repo, signed=True)
    _pin_witness_signer(monkeypatch, repo)
    signed = backend.anchor(head, day="2026-09-06")
    assert signed["commit_sha"] != unsigned["commit_sha"]
    assert backend.verify(signed, head)["verified"] is True
    retry = backend.anchor(head, day="2026-09-06")
    assert retry["anchor_action"] == "unchanged"
    assert retry["commit_sha"] == signed["commit_sha"]


def test_git_failed_commit_cleans_only_its_generated_path(tmp_path, monkeypatch):
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo, signed=False)
    subprocess.run(["git", "config", "commit.gpgsign", "true"], cwd=repo, check=True)
    subprocess.run(["git", "config", "gpg.format", "ssh"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.signingkey", "/missing/key"], cwd=repo, check=True)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    backend = anchor_git.GitAnchorBackend()
    with pytest.raises(ToolError):
        backend.anchor("a" * 64, day="2026-09-06")
    assert not (repo / "evidence-anchors" / "2026-09-06.json").exists()
    staged = subprocess.run(
        ["git", "ls-files", "--stage"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert staged == ""

    _init_witness_repo(repo, signed=True)
    _pin_witness_signer(monkeypatch, repo)
    signed = backend.anchor("a" * 64, day="2026-09-06")
    assert backend.verify(signed, "a" * 64)["verified"] is True


def test_concurrent_git_reanchor_reuses_one_commit(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    head = _head_digest()
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo)
    _pin_witness_signer(monkeypatch, repo)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    backend = anchor_git.GitAnchorBackend()

    with ThreadPoolExecutor(max_workers=2) as executor:
        refs = list(executor.map(lambda _: backend.anchor(head, day="2026-09-06"), range(2)))

    assert {ref["commit_sha"] for ref in refs} == {refs[0]["commit_sha"]}
    assert {ref["anchor_action"] for ref in refs} == {"created", "unchanged"}


def test_git_anchor_rejects_unpinned_or_different_signer(tmp_path, monkeypatch):
    head = _head_digest()
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    ref = anchor_git.GitAnchorBackend().anchor(head, day="2026-09-06")

    monkeypatch.delenv("UA_EVIDENCE_ANCHOR_GIT_SIGNER_FINGERPRINT", raising=False)
    assert anchor_git.GitAnchorBackend().verify(ref, head)["verified"] is False
    monkeypatch.setenv("UA_EVIDENCE_ANCHOR_GIT_SIGNER_FINGERPRINT", "SHA256:other-key")
    assert anchor_git.GitAnchorBackend().verify(ref, head)["verified"] is False
    _pin_witness_signer(monkeypatch, repo)
    assert anchor_git.GitAnchorBackend().verify(ref, head)["verified"] is True


def test_git_unsigned_commit_never_verifies(tmp_path, monkeypatch):
    head = _head_digest()
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo, signed=False)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)

    backend = anchor_git.GitAnchorBackend()
    result = backend.verify(backend.anchor(head, day="2026-09-06"), head)
    assert result["verified"] is False
    assert result["detail"]["content_matches"] is True
    assert "signature" in result["reason"]


def test_git_anchor_dir_cannot_escape_the_repo(tmp_path, monkeypatch):
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo, signed=False)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    monkeypatch.setattr(config, "evidence_anchor_git_dir", lambda: "../outside")
    with pytest.raises(ToolError, match="inside"):
        anchor_git.GitAnchorBackend().anchor(_head_digest(), day="2026-09-06")
    assert not (tmp_path / "outside").exists()


def test_git_missing_repo_config_raises(monkeypatch):
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: None)
    with pytest.raises(ToolError, match="UA_EVIDENCE_ANCHOR_GIT_REPO"):
        anchor_git.GitAnchorBackend().anchor("deadbeef")


def test_git_one_byte_tamper_in_head_digest_fails(tmp_path, monkeypatch):
    head = _head_digest()
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)

    backend = anchor_git.GitAnchorBackend()
    ref = backend.anchor(head, day="2026-09-06")

    tampered_head = head[:5] + ("0" if head[5] != "0" else "1") + head[6:]
    result = backend.verify(ref, tampered_head)
    assert result["verified"] is False
    assert result["reason"] == "head_hash mismatch"


def test_git_one_byte_tamper_in_ref_commit_sha_fails(tmp_path, monkeypatch):
    head = _head_digest()
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)

    backend = anchor_git.GitAnchorBackend()
    ref = backend.anchor(head, day="2026-09-06")

    sha = ref["commit_sha"]
    tampered_sha = sha[:-1] + ("0" if sha[-1] != "0" else "1")
    tampered_ref = {**ref, "commit_sha": tampered_sha}
    result = backend.verify(tampered_ref, head)
    assert result["verified"] is False
    assert result["reason"] == "commit does not contain the anchor file"


# --------------------------------------------------------------------------
# vault backend (legacy/default) + dispatcher
# --------------------------------------------------------------------------


def test_vault_backend_is_default_and_matches_reanchor_chain_head(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    monkeypatch.delenv("UA_EVIDENCE_ANCHOR_BACKEND", raising=False)

    events.record_event("agent.task.updated", payload={"task_id": 1})
    before_rows = _event_log_row_count()

    result = anchor.anchor_chain_head(day="2026-09-06")
    assert result["anchor_backend"] == "vault"
    assert result["anchor_path"] == "Logs/audit-ledger-heads/2026-09-06.md"

    # The vault path must not add any event_log rows (task #768 contract).
    assert _event_log_row_count() == before_rows


def _event_log_row_count() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"]
    finally:
        conn.close()


def test_vault_header_lookup_scans_notes_without_event_log_cache(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))

    head = _head_digest()
    result = anchor.anchor_chain_head(day="2026-09-06", backend="vault")
    assert result["anchor_backend"] == "vault"

    header = anchor.header_for_head(head)
    assert header["status"] == "anchored"
    assert header["backend"] == "vault"
    assert header["verification"]["verified"] is True
    assert "vault note" in header["recipe"]


def test_header_for_head_not_anchored_when_nothing_matches(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    assert anchor.header_for_head("no-such-head") == {
        "status": "not_anchored",
        "backend": None,
        "ref": None,
        "anchored_head": None,
        "entries_after_anchor": None,
        "anchored_at": None,
        "verification": None,
        "recipe": None,
    }
    assert anchor.header_for_head(None)["status"] == "not_anchored"


def test_git_backend_anchor_is_cached_in_event_log_for_header_lookup(tmp_path, monkeypatch):
    head = _head_digest()
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo)
    _pin_witness_signer(monkeypatch, repo)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    before_rows = _event_log_row_count()

    result = anchor.anchor_chain_head(backend="git", day="2026-09-06")
    assert result["anchor_backend"] == "git"
    assert _event_log_row_count() == before_rows + 1

    header = anchor.header_for_head(head)
    assert header["status"] == "anchored"
    assert header["backend"] == "git"
    assert header["verification"]["verified"] is True

    # Recording the anchor advanced the chain: the head a report actually
    # sees is one entry past the anchored one, and must still find it.
    from unified_agent import events

    current_head = events.verify_event_chain()["head_hash"]
    assert current_head != head
    later = anchor.header_for_head(current_head)
    assert later["status"] == "anchored"
    assert later["anchored_head"] == head
    assert later["entries_after_anchor"] == 1
    assert later["verification"]["verified"] is True

    repeat = anchor.anchor_chain_head(backend="git", day="2026-09-06")
    assert repeat["anchor_ref"]["anchor_action"] == "unchanged"
    assert _event_log_row_count() == before_rows + 1


def test_dated_tsa_retry_reuses_verified_cache(monkeypatch):
    _head_digest()
    calls = 0

    class FakeTsa:
        name = "tsa"

        def anchor(self, head_digest, *, day=None):
            nonlocal calls
            calls += 1
            return {"backend": "tsa", "head_hash": head_digest, "token_b64": "fixture"}

        def verify(self, ref, head_digest):
            return {"verified": ref["head_hash"] == head_digest, "reason": "fixture", "detail": {}}

    monkeypatch.setattr(anchor, "get_backend", lambda name: FakeTsa())
    before_rows = _event_log_row_count()
    anchor.anchor_chain_head(backend="tsa", day="2026-09-06")
    repeat = anchor.anchor_chain_head(backend="tsa", day="2026-09-06")
    assert repeat["anchor_ref"]["anchor_action"] == "unchanged"
    assert calls == 1
    assert _event_log_row_count() == before_rows + 1


def test_malformed_anchor_header_is_a_controlled_failure():
    malformed = {
        "anchor": {"status": "anchored", "backend": "git", "ref": ["bad"]},
        "chain_integrity": {"head_hash": "a" * 64},
    }
    result = evidence_cli._check_anchor(json.dumps(malformed).encode())
    assert result["code"] == 2
    assert "malformed" in result["message"]


def test_unknown_backend_raises():
    with pytest.raises(ToolError, match="unknown evidence anchor backend"):
        anchor.get_backend("carrier-pigeon")


def test_anchor_chain_head_rejects_empty_chain_for_non_vault_backend(monkeypatch, tmp_path):
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    with pytest.raises(ToolError, match="no hashed entries"):
        anchor.anchor_chain_head(backend="git")


# --------------------------------------------------------------------------
# evidence report header + `evidence verify` CLI integration
# --------------------------------------------------------------------------


def _insert_done_task(task_id: int) -> None:
    # A direct row insert alone leaves event_log (and so the chain head)
    # untouched; anchoring needs a real, non-empty chain to point at.
    events.record_event("agent.task.updated", payload={"task_id": task_id, "status": "done"})
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "completed_at, source_host, claimed_by, evidence_ref, notes, project, domain, lane) "
            "VALUES (?, 'anchor test', 'done', 'med', ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
            (
                task_id,
                "2026-09-22T10:00:00+00:00",
                "2026-09-22T10:00:00+00:00",
                "2026-09-22T10:00:00+00:00",
                "test-host",
                "Desktop_Agent1",
                "commit:test",
                "unified-agent",
                "professional",
                "default",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_evidence_report_header_carries_anchor(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")

    _insert_done_task(1301)
    head = events.verify_event_chain()["head_hash"]
    anchor.anchor_chain_head(day="2026-09-22", backend="vault")

    report = evidence_report.build_evidence_report(task_id=1301)
    assert report["schema_version"] == "1.8"
    assert report["anchor"]["status"] == "anchored"
    assert report["anchor"]["backend"] == "vault"
    assert report["chain_integrity"]["head_hash"] == head

    md = evidence_report.format_evidence_report(report, fmt="markdown")
    assert "Chain Anchor" in md
    text = evidence_report.format_evidence_report(report, fmt="text")
    assert "Chain Anchor" in text


def test_evidence_report_header_not_anchored_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")
    _insert_done_task(1302)
    report = evidence_report.build_evidence_report(task_id=1302)
    assert report["anchor"]["status"] == "not_anchored"


def test_evidence_verify_cli_checks_anchor_present_and_tampered(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1")
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")

    _insert_done_task(1303)
    anchor.anchor_chain_head(day="2026-09-22", backend="vault")

    artifact_path = tmp_path / "evidence.json"
    assert (
        evidence_cli.run_evidence_cli(["--task", "1303", "--json", "--out", str(artifact_path)])
        == 0
    )

    assert evidence_cli.run_evidence_cli(["verify", str(artifact_path)]) == 0
    out = capsys.readouterr().out
    assert "anchor verified" in out

    # Tamper the vault note the anchor points at: one byte inside the head
    # hash it records is enough to break the binding.
    note_path = vault / "Logs" / "audit-ledger-heads" / "2026-09-22.md"
    text = note_path.read_text()
    tampered = text.replace("head_hash:", "head_hash: tampered-")
    note_path.write_text(tampered)

    assert evidence_cli.run_evidence_cli(["verify", str(artifact_path)]) == 1
    out = capsys.readouterr().out
    assert "anchor verification failed" in out


def test_evidence_verify_cli_reports_not_anchored_without_failing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1")
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")
    _insert_done_task(1304)

    artifact_path = tmp_path / "evidence.json"
    assert (
        evidence_cli.run_evidence_cli(["--task", "1304", "--json", "--out", str(artifact_path)])
        == 0
    )
    assert evidence_cli.run_evidence_cli(["verify", str(artifact_path)]) == 0
    out = capsys.readouterr().out
    assert "anchor: not anchored" in out


def test_later_report_head_requires_verified_ledger_link(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    first_head = _head_digest()
    anchor.anchor_chain_head(day="2026-09-22", backend="vault")
    events.record_event("agent.task.updated", payload={"task_id": 2})
    later_head = events.verify_event_chain()["head_hash"]
    report = {
        "chain_integrity": {"head_hash": later_head},
        "anchor": anchor.header_for_head(later_head),
    }
    assert report["anchor"]["anchored_head"] == first_head
    assert evidence_cli._check_anchor(json.dumps(report).encode())["code"] == 0

    report["chain_integrity"]["head_hash"] = "f" * 64
    failed = evidence_cli._check_anchor(json.dumps(report).encode())
    assert failed["code"] == 1
    assert "report-head link failed" in failed["message"]


def test_unsigned_json_still_checks_its_anchor(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UA_EVIDENCE_SIGNING_KEY", raising=False)
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    _insert_done_task(1305)
    anchor.anchor_chain_head(day="2026-09-22", backend="vault")
    artifact_path = tmp_path / "unsigned.json"
    assert (
        evidence_cli.run_evidence_cli(["--task", "1305", "--json", "--out", str(artifact_path)])
        == 0
    )

    assert evidence_cli.run_evidence_cli(["verify", str(artifact_path)]) == 1
    out = capsys.readouterr().out
    assert "unsigned export" in out
    assert "anchor: anchor verified" in out


def test_ledger_cli_reanchor_backend_flag_defaults_to_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    events.record_event("agent.task.updated", payload={"task_id": 1})
    assert ledger_cli.run_reanchor_cli(["--date", "2026-09-06"]) == 0
    note = vault / "Logs" / "audit-ledger-heads" / "2026-09-06.md"
    assert note.is_file()


def test_ledger_cli_reanchor_git_backend(tmp_path, monkeypatch):
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo)
    _pin_witness_signer(monkeypatch, repo)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    events.record_event("agent.task.updated", payload={"task_id": 1})
    assert ledger_cli.run_reanchor_cli(["--date", "2026-09-06", "--backend", "git"]) == 0


def test_reanchor_unsigned_git_does_not_report_success(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "witness-repo"
    _init_witness_repo(repo, signed=False)
    monkeypatch.setattr(config, "evidence_anchor_git_repo", lambda: repo)
    events.record_event("agent.task.updated", payload={"task_id": 1})

    assert ledger_cli.run_reanchor_cli(["--backend", "git"]) != 0
    assert "did not verify" in capsys.readouterr().err


def test_anchor_io_failure_is_controlled_tool_error(monkeypatch):
    class BrokenBackend:
        name = "git"

        def verify(self, ref, head_digest):
            raise OSError("witness unavailable")

    monkeypatch.setattr(anchor, "get_backend", lambda name: BrokenBackend())
    with pytest.raises(ToolError, match="witness unavailable"):
        anchor.verify_anchor({"backend": "git"}, "a" * 64)


def test_config_defaults():
    assert config.evidence_anchor_backend() == "vault"
    assert config.evidence_tsa_url() == "https://freetsa.org/tsr"
    assert config.evidence_anchor_git_dir() == "evidence-anchors"
    assert config.evidence_tsa_cert_path() is None
    assert config.evidence_anchor_git_repo() is None


def test_malformed_tsa_reply_is_a_clean_tool_error():
    from unified_agent.audit import anchor_tsa

    with pytest.raises(ToolError, match="not a valid RFC-3161"):
        anchor_tsa.parse_timestamp_response(
            b"<html>proxy error</html>", expected_nonce=1, expected_imprint=b"x"
        )


def test_tsa_cert_without_timestamping_usage_is_rejected(monkeypatch, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID

    from unified_agent.audit import anchor_tsa

    head = _head_digest()
    tsa_key, tsa_cert, _ = _build_tsa_identity()
    # Same key and serial, but a certificate that is not for time-stamping.
    plain = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Not A TSA")]))
        .issuer_name(tsa_cert.issuer)
        .public_key(tsa_key.public_key())
        .serial_number(tsa_cert.serial_number)
        .not_valid_before(tsa_cert.not_valid_before_utc)
        .not_valid_after(tsa_cert.not_valid_after_utc)
        .sign(tsa_key, hashes.SHA256())
    )
    resp = _build_timestamp_resp(head_digest=head, nonce=7, tsa_key=tsa_key, tsa_cert=tsa_cert)
    token = anchor_tsa.parse_timestamp_response(
        resp, expected_nonce=7, expected_imprint=anchor_tsa.message_imprint_digest(head)
    )
    result = anchor_tsa.verify_timestamp_token(
        token, head, plain.public_bytes(serialization.Encoding.PEM)
    )
    assert result["verified"] is False
    assert result["reason"].startswith("tsa_cert_not_for_timestamping")
