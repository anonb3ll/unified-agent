"""Sign a local pytest run as an in-toto Statement in a DSSE envelope.

Agents run this before opening a pull request. The Ed25519 private key is
``UA_TEST_ATTESTATION_KEY``; CI checks ``UA_TEST_ATTESTATION_PUBLIC_KEY`` and
re-runs a deterministic sample of the passed tests. Skipped tests are recorded
as ``warnedTests``. Key material is read only from the environment.

    python -m unified_agent.pytest_attestation sign -- tests/test_area.py -q
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://in-toto.io/attestation/test-result/v0.1"
PAYLOAD_TYPE = "application/vnd.in-toto+json"
SUBJECT_NAME = "local-pytest"
DEFAULT_PATH = Path("attestation/local-tests.intoto.json")
PRIVATE_KEY_ENV = "UA_TEST_ATTESTATION_KEY"
PUBLIC_KEY_ENV = "UA_TEST_ATTESTATION_PUBLIC_KEY"
REQUIRED_ENV = "UA_TEST_ATTESTATION_REQUIRED"
SAMPLE_DENOMINATOR = 10
SAMPLE_LIMIT = 20
_MAX_BYTES = 2_000_000
_MAX_NODEID = 4000
_HEX_DIGEST = 64

_OUTCOMES = {"passed": "passedTests", "failed": "failedTests", "skipped": "warnedTests"}


class AttestationError(Exception):
    """The attestation cannot be signed or verified."""

    def __init__(self, message: str, *, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


class CaseResult:
    def __init__(self, name: str, outcome: str) -> None:
        self.name = name
        self.outcome = outcome


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding (DSSEv1)."""
    kind = payload_type.encode("utf-8")
    return (
        b"DSSEv1 "
        + str(len(kind)).encode("ascii")
        + b" "
        + kind
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def predicate_digest(predicate: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(predicate))).hexdigest()


def key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def _pem_newlines(value: str) -> str:
    text = value.strip()
    if "\\n" in text and text.replace("\\n", "\n").lstrip().startswith("-----BEGIN "):
        return text.replace("\\n", "\n")
    return text


def _raw_32(text: str) -> bytes:
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise AttestationError("key must be PEM or 32-byte standard base64") from None
    if len(raw) != 32:
        raise AttestationError("raw key must be 32 bytes")
    return raw


def load_private_key(value: str) -> Ed25519PrivateKey:
    text = _pem_newlines(value)
    if not text:
        raise AttestationError(f"{PRIVATE_KEY_ENV} is not set")
    if text.lstrip().startswith("-----BEGIN "):
        try:
            key = serialization.load_pem_private_key(text.encode(), password=None)
        except (ValueError, TypeError):
            raise AttestationError(
                f"{PRIVATE_KEY_ENV} is not a usable Ed25519 private key"
            ) from None
        if not isinstance(key, Ed25519PrivateKey):
            raise AttestationError(f"{PRIVATE_KEY_ENV} must be an Ed25519 private key")
        return key
    return Ed25519PrivateKey.from_private_bytes(_raw_32(text))


def load_public_key(value: str) -> Ed25519PublicKey:
    text = _pem_newlines(value)
    if not text:
        raise AttestationError(f"{PUBLIC_KEY_ENV} is not set")
    if text.lstrip().startswith("-----BEGIN "):
        try:
            key = serialization.load_pem_public_key(text.encode())
        except ValueError:
            raise AttestationError(f"{PUBLIC_KEY_ENV} is not a usable Ed25519 public key") from None
        if not isinstance(key, Ed25519PublicKey):
            raise AttestationError(f"{PUBLIC_KEY_ENV} must be an Ed25519 public key")
        return key
    return Ed25519PublicKey.from_public_bytes(_raw_32(text))


def private_key_from_env() -> Ed25519PrivateKey:
    key = load_private_key(os.environ.get(PRIVATE_KEY_ENV, ""))
    configured = os.environ.get(PUBLIC_KEY_ENV, "").strip()
    if configured and key_id(load_public_key(configured)) != key_id(key.public_key()):
        raise AttestationError(f"{PUBLIC_KEY_ENV} does not match {PRIVATE_KEY_ENV}")
    return key


def public_key_from_env() -> Ed25519PublicKey:
    return load_public_key(os.environ.get(PUBLIC_KEY_ENV, ""))


def statement_from_outcomes(outcomes: Sequence[CaseResult]) -> dict[str, Any]:
    buckets: dict[str, list[str]] = {name: [] for name in _OUTCOMES}
    seen: set[str] = set()
    for item in outcomes:
        if item.outcome not in buckets:
            raise AttestationError(f"unknown test outcome: {item.outcome}")
        if not item.name or item.name in seen:
            raise AttestationError("test names must be unique and non-empty")
        seen.add(item.name)
        buckets[item.outcome].append(item.name)
    predicate = {
        "failedTests": sorted(buckets["failed"]),
        "passedTests": sorted(buckets["passed"]),
        "result": "FAILED" if buckets["failed"] else "PASSED",
        "warnedTests": sorted(buckets["skipped"]),
    }
    return {
        "_type": STATEMENT_TYPE,
        "predicate": predicate,
        "predicateType": PREDICATE_TYPE,
        "subject": [{"digest": {"sha256": predicate_digest(predicate)}, "name": SUBJECT_NAME}],
    }


def sign_statement(statement: Mapping[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    payload = canonical_json(dict(statement))
    signature = private_key.sign(pae(PAYLOAD_TYPE, payload))
    return {
        "payload": base64.b64encode(payload).decode("ascii"),
        "payloadType": PAYLOAD_TYPE,
        "signatures": [
            {"keyid": key_id(private_key.public_key()), "sig": base64.b64encode(signature).decode()}
        ],
    }


def _b64(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise AttestationError(f"attestation {label} is missing", code=1)
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise AttestationError(f"attestation {label} is not base64", code=1) from None


def _test_names(predicate: Mapping[str, Any], field: str) -> list[str]:
    value = predicate.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise AttestationError(f"attestation {field} must be a list of test names", code=1)
    if any(len(item) > _MAX_NODEID for item in value):
        raise AttestationError(f"attestation {field} has a test id that is too long", code=1)
    return value


def verify_envelope(envelope: Mapping[str, Any], public_key: Ed25519PublicKey) -> dict[str, Any]:
    if envelope.get("payloadType") != PAYLOAD_TYPE:
        raise AttestationError("unsupported attestation payload type", code=1)
    signatures = envelope.get("signatures")
    if (
        not isinstance(signatures, list)
        or len(signatures) != 1
        or not isinstance(signatures[0], dict)
    ):
        raise AttestationError("attestation must carry one signature", code=1)
    payload = _b64(envelope.get("payload"), "payload")
    signature = _b64(signatures[0].get("sig"), "signature")
    expected = key_id(public_key)
    got = signatures[0].get("keyid")
    if (
        not isinstance(got, str)
        or len(got) != len(expected)
        or not hmac.compare_digest(got, expected)
    ):
        raise AttestationError(
            "attestation key id does not match the configured public key", code=1
        )
    try:
        public_key.verify(signature, pae(PAYLOAD_TYPE, payload))
    except InvalidSignature:
        raise AttestationError("attestation signature is invalid", code=1) from None
    try:
        statement = json.loads(payload)
    except json.JSONDecodeError:
        raise AttestationError("attestation payload is not JSON", code=1) from None
    _require_statement(statement)
    return statement


def _require_statement(statement: object) -> None:
    if not isinstance(statement, dict):
        raise AttestationError("attestation payload must be a JSON object", code=1)
    if statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != PREDICATE_TYPE:
        raise AttestationError("attestation is not an in-toto test-result statement", code=1)
    subject = statement.get("subject")
    predicate = statement.get("predicate")
    if (
        not isinstance(subject, list)
        or len(subject) != 1
        or not isinstance(subject[0], dict)
        or subject[0].get("name") != SUBJECT_NAME
        or not isinstance(predicate, dict)
    ):
        raise AttestationError("attestation subject or predicate is malformed", code=1)
    digest = subject[0].get("digest")
    recorded = digest.get("sha256") if isinstance(digest, dict) else None
    if (
        not isinstance(recorded, str)
        or len(recorded) != _HEX_DIGEST
        or any(char not in "0123456789abcdef" for char in recorded)
    ):
        raise AttestationError("attestation subject digest is malformed", code=1)
    if predicate.get("result") not in {"PASSED", "FAILED"}:
        raise AttestationError("attestation result must be PASSED or FAILED", code=1)
    passed = _test_names(predicate, "passedTests")
    failed = _test_names(predicate, "failedTests")
    warned = _test_names(predicate, "warnedTests")
    names = passed + failed + warned
    if len(names) != len(set(names)):
        raise AttestationError("attestation repeats a test", code=1)
    if (predicate["result"] == "PASSED") != (not failed):
        raise AttestationError("attestation result does not match the failed tests", code=1)
    if not hmac.compare_digest(recorded, predicate_digest(predicate)):
        raise AttestationError("attestation subject does not match the test results", code=1)


def sample_tests(tests: Sequence[str], *, seed: str, count: int) -> list[str]:
    """Return ``count`` names. The same seed and names always return the same set."""
    if not isinstance(seed, str) or seed == "":
        raise AttestationError("sample seed must be a non-empty string")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise AttestationError("sample count must be a positive integer")
    if isinstance(tests, (str, bytes)) or not isinstance(tests, Sequence):
        raise AttestationError("tests must be a list of names")
    if any(not isinstance(name, str) or name == "" for name in tests):
        raise AttestationError("test names must be non-empty strings")
    ordered = sorted(set(tests))
    if not ordered:
        return []
    ranked = sorted(
        ordered,
        key=lambda name: (
            hmac.new(seed.encode("utf-8"), name.encode("utf-8"), hashlib.sha256).digest(),
            name,
        ),
    )
    return sorted(ranked[: min(count, len(ranked))])


def sample_size(total: int) -> int:
    """Ten percent of the attested tests, at least one, and never more than twenty."""
    if total < 1:
        return 0
    return min(total, SAMPLE_LIMIT, max(1, (total + SAMPLE_DENOMINATOR - 1) // SAMPLE_DENOMINATOR))


def safe_node_ids(node_ids: Sequence[str], *, cwd: Path) -> list[str]:
    """Keep relative pytest node ids whose files sit inside ``cwd``."""
    root = cwd.resolve()
    safe: list[str] = []
    for node_id in node_ids:
        path = node_id.split("::", 1)[0]
        if (
            not node_id
            or node_id.startswith("-")
            or "\\" in node_id
            or any(ord(char) < 32 for char in node_id)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise AttestationError("attestation has an unsafe test id", code=1)
        target = (root / path).resolve()
        if not target.is_file() or not target.is_relative_to(root):
            raise AttestationError(f"attested test file is not in the workspace: {path}", code=1)
        safe.append(node_id)
    return safe


def rerun(node_ids: Sequence[str], *, cwd: Path) -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", *node_ids],
        cwd=cwd,
        check=False,
    )
    return int(completed.returncode)


def _report_outcome(failed: bool, skipped: bool) -> str:
    if failed:
        return "failed"
    if skipped:
        return "skipped"
    return "passed"


def collect(pytest_args: Sequence[str]) -> tuple[int, list[CaseResult]]:
    try:
        import pytest
    except ImportError:
        raise AttestationError("pytest is not installed") from None
    found: dict[str, str] = {}

    class Collector:
        def pytest_runtest_logreport(self, report: object) -> None:
            nodeid = getattr(report, "nodeid", "")
            when = getattr(report, "when", "")
            if not isinstance(nodeid, str) or not nodeid or not isinstance(when, str):
                return
            outcome = _report_outcome(
                bool(getattr(report, "failed", False)), bool(getattr(report, "skipped", False))
            )
            if when == "call":
                found[nodeid] = outcome
            elif when == "setup" and nodeid not in found and outcome != "passed":
                found[nodeid] = outcome
            elif when == "teardown" and outcome == "failed":
                found[nodeid] = "failed"

    code = int(pytest.main(list(pytest_args), plugins=[Collector()]))
    return code, [CaseResult(name, outcome) for name, outcome in sorted(found.items())]


def write_envelope(path: Path, envelope: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_envelope(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AttestationError(f"missing test attestation: {path}", code=1)
    if path.stat().st_size > _MAX_BYTES:
        raise AttestationError("attestation file is too large", code=1)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AttestationError(f"cannot read attestation: {exc}", code=1) from None
    if not isinstance(value, dict):
        raise AttestationError("attestation must be a JSON object", code=1)
    return value


def _required() -> bool:
    return os.environ.get(REQUIRED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _pytest_args(remainder: list[str]) -> list[str]:
    args = remainder[1:] if remainder[:1] == ["--"] else remainder
    if not args:
        raise AttestationError("pass the pytest arguments after --")
    return args


def _command_sign(output: Path, pytest_args: list[str]) -> int:
    key = private_key_from_env()
    code, outcomes = collect(pytest_args)
    if code >= 2 or not outcomes:
        detail = "pytest collected no tests" if not outcomes else f"pytest could not run ({code})"
        raise AttestationError(detail, code=1)
    statement = statement_from_outcomes(outcomes)
    write_envelope(output, sign_statement(statement, key))
    result = statement["predicate"]["result"]
    print(f"wrote {output} ({result}, {len(outcomes)} tests)")
    return 0 if result == "PASSED" and code == 0 else 1


def _command_verify(path: Path) -> int:
    statement = verify_envelope(read_envelope(path), public_key_from_env())
    predicate = statement["predicate"]
    print(
        "attestation signature verified: "
        f"{predicate['result']} ({len(predicate['passedTests'])} passed, "
        f"{len(predicate['failedTests'])} failed)"
    )
    return 0


def _command_ci(path: Path, seed: str, run: bool) -> int:
    if not path.is_file():
        if _required():
            raise AttestationError(f"missing test attestation: {path}", code=1)
        print(f"no test attestation at {path}; verification skipped")
        return 0
    if not seed:
        raise AttestationError("ci needs --seed")
    statement = verify_envelope(read_envelope(path), public_key_from_env())
    predicate = statement["predicate"]
    if predicate["result"] != "PASSED":
        raise AttestationError("attestation result is not PASSED", code=1)
    passed = predicate["passedTests"]
    if not passed:
        raise AttestationError("attestation lists no passed tests", code=1)
    chosen = sample_tests(passed, seed=seed, count=sample_size(len(passed)))
    print(f"attestation verified; sampling {len(chosen)} of {len(passed)}")
    print("\n".join(chosen))
    if not run:
        return 0
    return rerun(safe_node_ids(chosen, cwd=Path.cwd()), cwd=Path.cwd())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m unified_agent.pytest_attestation")
    commands = parser.add_subparsers(dest="command", required=True)
    sign = commands.add_parser("sign", help="run pytest and sign the results")
    sign.add_argument("--output", type=Path, default=DEFAULT_PATH)
    sign.add_argument("pytest_args", nargs=argparse.REMAINDER)
    verify = commands.add_parser("verify", help="verify an attestation signature")
    verify.add_argument("path", type=Path)
    ci = commands.add_parser("ci", help="verify the attestation and re-run a sample")
    ci.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    ci.add_argument("--seed", default="")
    ci.add_argument("--run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "sign":
            return _command_sign(args.output, _pytest_args(args.pytest_args))
        if args.command == "verify":
            return _command_verify(args.path)
        if args.command == "ci":
            return _command_ci(args.path, args.seed, args.run)
        raise AttestationError(f"unknown command: {args.command}")
    except AttestationError as exc:
        print(exc, file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
