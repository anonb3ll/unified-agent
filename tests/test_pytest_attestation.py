"""Local pytest runs signed as in-toto attestations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from unified_agent.pytest_attestation import (
    PUBLIC_KEY_ENV,
    REQUIRED_ENV,
    AttestationError,
    CaseResult,
    canonical_json,
    main,
    pae,
    predicate_digest,
    private_key_from_env,
    public_key_from_env,
    sample_size,
    sample_tests,
    sign_statement,
    statement_from_outcomes,
    verify_envelope,
)

REPO = Path(__file__).resolve().parents[1]


def _pem_pair() -> tuple[str, str, Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem, private


def _signed(private: Ed25519PrivateKey, outcomes: list[CaseResult]) -> tuple[dict, dict]:
    statement = statement_from_outcomes(outcomes)
    return statement, sign_statement(statement, private)


def _tamper(envelope: dict, mutate) -> dict:
    payload = json.loads(base64.b64decode(envelope["payload"]))
    mutate(payload)
    for field in ("passedTests", "failedTests", "warnedTests"):
        payload["predicate"][field] = sorted(payload["predicate"][field])
    payload["subject"][0]["digest"]["sha256"] = predicate_digest(payload["predicate"])
    body = canonical_json(payload)
    return {
        "payload": base64.b64encode(body).decode("ascii"),
        "payloadType": envelope["payloadType"],
        "signatures": envelope["signatures"],
    }


def _passed(*names: str) -> list[CaseResult]:
    return [CaseResult(name, "passed") for name in names]


def test_dsse_pae_matches_the_spec_vector():
    assert pae("http://example.com/HelloWorld", b"hello world") == (
        b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
    )


def test_valid_attestation_verifies():
    _private_pem, _public_pem, private = _pem_pair()
    statement, envelope = _signed(
        private,
        _passed("tests/test_a.py::test_two", "tests/test_a.py::test_one"),
    )
    verified = verify_envelope(envelope, private.public_key())
    assert verified == statement
    assert verified["predicate"]["result"] == "PASSED"
    assert verified["predicate"]["passedTests"] == [
        "tests/test_a.py::test_one",
        "tests/test_a.py::test_two",
    ]
    raw = base64.b64decode(envelope["payload"])
    assert raw == canonical_json(json.loads(raw))
    encoded = json.dumps(envelope)
    seed = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    assert base64.b64encode(seed).decode("ascii") not in encoded


@pytest.mark.parametrize("kind", ["outcome", "test-list"])
def test_tampered_attestation_fails_verification(kind: str):
    _private_pem, _public_pem, private = _pem_pair()
    _statement, envelope = _signed(
        private,
        _passed("tests/test_a.py::test_one", "tests/test_a.py::test_two"),
    )

    def mutate(payload: dict) -> None:
        if kind == "outcome":
            payload["predicate"]["result"] = "FAILED"
            moved = payload["predicate"]["passedTests"].pop()
            payload["predicate"]["failedTests"].append(moved)
            return
        payload["predicate"]["passedTests"].append("tests/test_a.py::test_injected")

    tampered = _tamper(envelope, mutate)
    with pytest.raises(AttestationError, match="signature is invalid"):
        verify_envelope(tampered, private.public_key())


def test_verification_rejects_a_different_key():
    _private_pem, _public_pem, private = _pem_pair()
    _statement, envelope = _signed(private, _passed("tests/test_a.py::test_one"))
    with pytest.raises(AttestationError, match="key id"):
        verify_envelope(envelope, Ed25519PrivateKey.generate().public_key())


def test_sampler_is_deterministic_under_a_seed():
    names = [f"tests/test_mod.py::test_{i:02d}" for i in range(20)]
    seed = "head-sha"
    first = sample_tests(names, seed=seed, count=5)
    assert first == sample_tests(list(reversed(names)), seed=seed, count=5)
    expected = sorted(
        sorted(
            names,
            key=lambda name: (
                hmac.new(seed.encode(), name.encode(), hashlib.sha256).digest(),
                name,
            ),
        )[:5]
    )
    assert first == expected
    assert set(first) < set(names)
    other = "other-seed"
    assert sample_tests(names, seed=other, count=5) == sample_tests(
        list(reversed(names)), seed=other, count=5
    )
    with pytest.raises(AttestationError, match="seed"):
        sample_tests(names, seed="", count=1)


def test_keys_come_from_the_environment(tmp_path, monkeypatch):
    private_pem, public_pem, private = _pem_pair()
    monkeypatch.setenv(PUBLIC_KEY_ENV, public_pem.replace("\n", "\\n"))
    monkeypatch.setenv("UA_TEST_ATTESTATION_KEY", private_pem.replace("\n", "\\n"))
    assert private_key_from_env().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    raw_public = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode("ascii")
    monkeypatch.setenv(PUBLIC_KEY_ENV, raw_public)
    statement, envelope = _signed(private, _passed("tests/test_a.py::test_one"))
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert main(["verify", str(path)]) == 0
    assert public_key_from_env()
    monkeypatch.delenv(PUBLIC_KEY_ENV, raising=False)
    assert main(["verify", str(path)]) == 2


def test_signed_failure_verifies_but_ci_rejects_it(tmp_path, monkeypatch):
    _private_pem, public_pem, private = _pem_pair()
    _statement, envelope = _signed(private, [CaseResult("tests/test_a.py::test_bad", "failed")])
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    monkeypatch.setenv(PUBLIC_KEY_ENV, public_pem)
    assert main(["verify", str(path)]) == 0
    assert main(["ci", str(path), "--seed", "head"]) == 1


def test_ci_does_not_rerun_an_unsafe_test_id(tmp_path, monkeypatch, capsys):
    _private_pem, public_pem, private = _pem_pair()
    _statement, envelope = _signed(private, _passed("-k"))
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    monkeypatch.setenv(PUBLIC_KEY_ENV, public_pem)
    monkeypatch.chdir(tmp_path)
    assert main(["ci", str(path), "--seed", "head", "--run"]) == 1
    assert "unsafe" in capsys.readouterr().err


def test_missing_attestation_skips_unless_required(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(REQUIRED_ENV, raising=False)
    assert main(["ci", "--seed", "head"]) == 0
    assert "verification skipped" in capsys.readouterr().out
    monkeypatch.setenv(REQUIRED_ENV, "true")
    assert main(["ci", "--seed", "head"]) == 1


def test_ci_reruns_only_the_deterministic_sample(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    assert not work.resolve().is_relative_to(REPO)
    functions = []
    for i in range(8):
        functions.append(
            f"def test_{i:02d}():\n"
            "    with open('ran.txt', 'a', encoding='utf-8') as handle:\n"
            f"        handle.write('test_{i:02d}\\n')\n"
        )
    (work / "test_batch.py").write_text("\n".join(functions), encoding="utf-8")
    private_pem, public_pem, _private = _pem_pair()
    env = os.environ.copy()
    env["UA_TEST_ATTESTATION_KEY"] = private_pem
    env[PUBLIC_KEY_ENV] = public_pem
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    out = work / "attestation" / "local-tests.intoto.json"
    sign = subprocess.run(
        [
            sys.executable,
            "-m",
            "unified_agent.pytest_attestation",
            "sign",
            "--output",
            str(out),
            "--",
            "test_batch.py",
            "-q",
            "--noconftest",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sign.returncode == 0, sign.stderr
    (work / "ran.txt").unlink()
    ci = subprocess.run(
        [
            sys.executable,
            "-m",
            "unified_agent.pytest_attestation",
            "ci",
            str(out),
            "--seed",
            "seed-1",
            "--run",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ci.returncode == 0, ci.stderr
    names = [f"test_batch.py::test_{i:02d}" for i in range(8)]
    expected = sample_tests(names, seed="seed-1", count=sample_size(len(names)))
    assert [line for line in ci.stdout.splitlines() if "::" in line] == expected
    assert (work / "ran.txt").read_text(encoding="utf-8").split() == [expected[0].split("::", 1)[1]]
    again = subprocess.run(
        [
            sys.executable,
            "-m",
            "unified_agent.pytest_attestation",
            "ci",
            str(out),
            "--seed",
            "seed-1",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert again.returncode == 0, again.stderr
    assert [line for line in again.stdout.splitlines() if "::" in line] == expected
