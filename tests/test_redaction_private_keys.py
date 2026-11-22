"""Private-key blocks are redacted whole, never just their header (task #1983).

Markers are assembled at runtime so the repository's secret scanner does not
flag the test file itself.
"""

from __future__ import annotations

import json

import pytest
from growth_budget import assert_cpu_growth, cold

from unified_agent.telemetry.redaction import (
    PEM_PRIVATE_KEY_BLOCK_RE,
    redact_sensitive_content,
)

DASHES = "-" * 5
BODY = (
    "MIIEowIBAAKCAQEAx1y2z3w4v5u6t7s8r9q0p1o2n3m4l5k6j7i8h9g0f1e2d3c4\n"
    "b5a6Z7Y8X9W0V1U2T3S4R5Q6P7O8N9M0L1K2J3I4H5G6F7E8D9C0B1A2=="
)
BODY_LINES = BODY.split("\n")


def marker(edge: str, kind: str) -> str:
    return f"{DASHES}{edge} {kind}PRIVATE KEY{' BLOCK' if kind == 'PGP ' else ''}{DASHES}"


def block(kind: str = "RSA ", body: str = BODY, sep: str = "\n") -> str:
    lines = [marker("BEGIN", kind), *body.split("\n"), marker("END", kind)]
    return sep.join(lines)


def redact(text: str) -> str:
    return redact_sensitive_content(text, max_len=len(text) + 100)


def assert_no_body(out: str) -> None:
    for line in BODY_LINES:
        assert line not in out


@pytest.mark.parametrize("kind", ["RSA ", "EC ", "OPENSSH ", "ENCRYPTED ", "", "PGP "])
def test_a_whole_block_is_redacted_and_the_surrounding_text_kept(kind):
    out = redact(f"before\n{block(kind)}\nafter")
    assert_no_body(out)
    assert out == "before\n[REDACTED_SECRET]\nafter"


def test_a_json_encoded_block_is_redacted():
    out = redact(json.dumps({"notes": f"see\n{block()}\nthanks"}))
    assert_no_body(out)
    assert "thanks" in out


def test_crlf_lines_are_redacted():
    assert_no_body(redact(block(sep="\r\n")))


def test_an_unterminated_block_redacts_the_key_lines_only():
    from unified_agent.telemetry.redaction import PEM_PRIVATE_KEY_UNTERMINATED_RE

    text = f"x {marker('BEGIN', 'RSA ')}\nProc-Type: 4,ENCRYPTED\n\n{BODY}\nthanks\nnext: value"
    out = PEM_PRIVATE_KEY_UNTERMINATED_RE.sub("[REDACTED_SECRET]", text)
    assert_no_body(out)
    assert "Proc-Type" not in out
    # A plain word and a lowercase key: value line are not key material.
    assert out.endswith("\nthanks\nnext: value")
    # Redaction itself fails closed on raw text with no END line (PR #1146
    # review round 3): everything from the header on goes.
    assert redact(text) == "x [REDACTED_SECRET]"


def test_an_unterminated_json_encoded_block_is_redacted():
    out = redact(json.dumps(f"x {marker('BEGIN', 'RSA ')}\n{BODY}"))
    assert_no_body(out)


def test_the_shared_block_pattern_matches_pgp_and_digits():
    assert PEM_PRIVATE_KEY_BLOCK_RE.search(block("PGP "))
    assert PEM_PRIVATE_KEY_BLOCK_RE.search(block("ED25519 "))


@pytest.mark.parametrize(
    "shape",
    [
        "key: |\n  {B}\n  {L1}\n  {L2}",  # an indented YAML block scalar
        "{B}   \n{L1}  \n{L2}\t",  # trailing whitespace
        "{B}\n\t{L1}\n\t{L2}",  # tab-indented
    ],
)
def test_an_unterminated_block_with_whitespace_is_redacted(shape):
    text = shape.format(B=marker("BEGIN", "RSA "), L1=BODY_LINES[0], L2=BODY_LINES[1])
    assert_no_body(redact(text))


def test_many_unterminated_headers_stay_linear():
    # CPU growth, not a wall-clock bound (#2201).
    unit = marker("BEGIN", "RSA ") + "\nx\n"
    assert_cpu_growth(cold(redact), unit * 1500, unit * 6000, limit=8.0, samples=2)


def test_long_whitespace_after_a_header_stays_linear():
    header = marker("BEGIN", "RSA ")
    one = header + "\n"
    assert_cpu_growth(cold(redact), one + " " * 25_000, one + " " * 100_000, limit=8.0, samples=2)
    row = "\n" + " " * 50
    assert_cpu_growth(cold(redact), header + row * 1250, header + row * 5000, limit=8.0, samples=2)


def test_gcloud_metadata_scrubbing_covers_an_unterminated_key():
    from unified_agent.tools import gcloud

    out = gcloud._scrub_secret_patterns(f"#!/bin/sh\n{marker('BEGIN', 'RSA ')}\n{BODY}")
    assert_no_body(out)


@pytest.mark.parametrize("last", ["QUJD", "QUJDREVG", "QUJDREVGR0hJ"])
@pytest.mark.parametrize("ending", ["", "\n" + DASHES + "END RS", "..."])
def test_a_short_unpadded_last_line_is_redacted(last, ending):
    out = redact(f"{marker('BEGIN', 'RSA ')}\n{BODY_LINES[0]}\n{last}{ending}")
    assert last not in out and BODY_LINES[0] not in out


@pytest.mark.parametrize("mark", ["...", "\u2026[truncated]"])
def test_a_key_line_cut_with_a_truncation_mark_is_redacted(mark):
    cut = BODY_LINES[0][:40]
    out = redact(f"{marker('BEGIN', 'RSA ')}\n{BODY_LINES[0]}\n{cut}{mark}")
    assert cut not in out


def test_a_pgp_checksum_line_is_taken_with_the_body():
    from unified_agent.telemetry.redaction import PEM_PRIVATE_KEY_UNTERMINATED_RE

    text = f"{marker('BEGIN', 'PGP ')}\n\n{BODY_LINES[0]}\n=AbCd\nnext: value"
    out = PEM_PRIVATE_KEY_UNTERMINATED_RE.sub("[REDACTED_SECRET]", text)
    assert "=AbCd" not in out and out.endswith("\nnext: value")
    assert "=AbCd" not in redact(text)
