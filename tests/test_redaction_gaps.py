"""Redaction gaps beyond terminated PEM blocks (task #1994).

PuTTY PPK bodies, JWK private members, the credential-in-URL pattern's
quadratic scan, and unterminated private keys in flattened or re-encoded text.
Every key-shaped fixture is assembled at runtime so the repository's secret
scanner does not flag this file.
"""

from __future__ import annotations

import json
import time

import pytest

from unified_agent.telemetry.redaction import (
    CREDENTIAL_SHAPE_PATTERNS,
    PEM_PRIVATE_KEY_UNTERMINATED_RE,
    PUTTY_PRIVATE_KEY_RE,
    redact_sensitive_content,
)

DASHES = "-" * 5
#: Two full-width (64 character) key body lines, unpadded, then a padded last line.
L1 = "MIIEowIBAAKCAQEAx1y2z3w4v5u6t7s8r9q0/1o2n3m4l5k6j7i8h9g0f1e2d3c4"
L2 = "b5a6Z7Y8X9W0V1U2T3S4R5Q6+7O8N9M0L1K2J3I4H5G6F7E8D9C0B1A2Z3/4X5W6"
L3 = "V7U8T9S0R1Q2P3O4N5M6=="
LINES = [L1, L2, L3]


def begin(kind: str = "RSA ") -> str:
    return f"{DASHES}BEGIN {kind}PRIVATE" + f" KEY{DASHES}"


def redact(text: str) -> str:
    return redact_sensitive_content(text, max_len=len(text) + 100)


def scan(pattern, text: str) -> str:
    """One key pattern alone. Since PR #1146 review round 3, redaction of a raw
    unterminated key is structural and runs to the end of the text (fail
    closed); the patterns still bound the key itself, which these pin."""
    return pattern.sub("[REDACTED_SECRET]", text)


def assert_no_key(out: str, lines: list[str] = LINES) -> None:
    for line in lines:
        assert line not in out, f"leaked {line!r} in {out!r}"
        # A JSON '\/' escape must not leave the rest of a line behind either.
        for part in line.split("/"):
            if len(part) >= 12:
                assert part not in out, f"leaked {part!r} in {out!r}"


def elapsed(fn, *args) -> float:
    started = time.monotonic()
    fn(*args)
    return time.monotonic() - started


# --- 1. PuTTY PPK -----------------------------------------------------------


def ppk(version: int = 3, with_mac: bool = True) -> tuple[str, list[str]]:
    public = ["AAAAC3NzaC1lZDI1NTE5AAAAIGq0rQ8vZ1k2b3c4d5e6f7g8h9i0j1k2l3m4n5o6"]
    private = ["AAAAIBk9xYzA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8", "QUJD"]
    lines = [
        "PuTTY-User-Key-File-" + f"{version}: ssh-ed25519",
        "Encryption: none",
        "Comment: eddsa-key-fixture",
        f"Public-Lines: {len(public)}",
        *public,
        f"Private-Lines: {len(private)}",
        *private,
    ]
    if with_mac:
        lines.append("Private-MAC: " + "0f" * 32)
    return "\n".join(lines), [*public, private[0], "0f" * 32]


@pytest.mark.parametrize("version", [2, 3])
def test_a_putty_key_body_is_redacted_and_following_text_kept(version):
    text, secrets = ppk(version)
    out = redact(f"before\n{text}\nthanks")
    for secret in secrets:
        assert secret not in out
    assert "\nQUJD" not in out  # the short last Private-Lines line
    assert out == "before\n[REDACTED_SECRET]\nthanks"


def test_an_unterminated_putty_key_is_redacted():
    text, secrets = ppk(with_mac=False)
    out = redact(text)
    for secret in secrets[:-1]:
        assert secret not in out


def test_a_json_encoded_putty_key_is_redacted():
    text, secrets = ppk()
    out = redact(json.dumps({"file": text, "next": "kept"}))
    for secret in secrets:
        assert secret not in out
    assert '"next": "kept"' in out


@pytest.mark.parametrize(
    "prose",
    ["the quick brown fox jumps over the lazy dog", "Thanks for looking."],
)
def test_prose_after_a_putty_key_is_kept(prose):
    # PR #1117 review: a short base64-alphabet word is not key material, so the
    # scan must stop at prose instead of swallowing every word after a header.
    header = "PuTTY-User-Key-File-" + "3: k"
    text = f"{header}\nPublic-Lines: 1\nAAAA\n{prose}"
    assert scan(PUTTY_PRIVATE_KEY_RE, text).endswith(f"\n{prose}")
    assert redact(text) == "[REDACTED_SECRET]"  # no MAC line: fail closed


def test_a_short_public_last_line_does_not_stop_the_scan():
    # An ed25519 public blob is 68 base64 characters: 64, then a short unpadded
    # line right before Private-Lines. The private body after it must still go.
    private = "AAAAIBk9xYzA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8"
    text = "\n".join(
        [
            "PuTTY-User-Key-File-" + "3: ssh-ed25519",
            "Public-Lines: 2",
            "AAAAC3NzaC1lZDI1NTE5AAAAIGq0rQ8vZ1k2b3c4d5e6f7g8h9i0j1k2l3m4n5o6",
            "p7q8",
            "Private-Lines: 1",
            private,
        ]
    )
    assert private not in redact(text)


# --- 2. JWK private members -------------------------------------------------


def b64url(seed: str, length: int) -> str:
    return (seed * length)[:length]


def jwk() -> tuple[dict, list[str]]:
    private = {
        "d": b64url("dPrv-Exp_0nent", 342),
        "p": b64url("pPrime_Fact0r-", 171),
        "q": b64url("qPrime_Fact0r-", 171),
        "dp": b64url("dpCrtExp0nent_", 171),
        "dq": b64url("dqCrtExp0nent_", 171),
        "qi": b64url("qiCrtCoeff1c-_", 171),
    }
    key = {"kty": "RSA", "n": b64url("nModulus", 342), "e": "AQAB", "kid": "fixture", **private}
    return key, list(private.values())


@pytest.mark.parametrize(
    "encode",
    [
        json.dumps,
        lambda key: json.dumps(key, separators=(",", ":")),
        lambda key: json.dumps(json.dumps(key)),  # JSON inside a JSON string
        repr,  # a Python dict repr
    ],
)
def test_jwk_private_members_are_redacted(encode):
    key, secrets = jwk()
    out = redact(encode(key))
    for secret in secrets:
        assert secret not in out
    if encode(key).startswith('"'):
        # Encoded JSON is redacted as one enclosing string: source offsets
        # cannot safely be inferred from decoded positions through escapes.
        assert json.loads(out) == "[REDACTED_SECRET]"
        return
    # Public members and the member names stay readable.
    assert key["n"] in out and "AQAB" in out and "fixture" in out
    for name in ("d", "p", "q", "dp", "dq", "qi"):
        assert f"{name}" in out


def test_an_ec_jwk_private_scalar_is_redacted():
    d = b64url("Ec_Pr1vate-Scalar", 43)
    key = {"kty": "EC", "crv": "P-256", "x": b64url("xCoord", 43), "d": d}  # ua:allow
    assert d not in redact(json.dumps(key))


@pytest.mark.parametrize("name", ["d", "p", "q", "dp", "dq", "qi"])
@pytest.mark.parametrize("whitespace", ["\n", "\r\n", " \t\r\n"])
@pytest.mark.parametrize("side", ["before_colon", "after_colon"])
def test_multiline_json_jwk_members_are_redacted(name, whitespace, side):
    secret = b64url("Ec_Pr1vate-Scalar", 43)
    before = whitespace if side == "before_colon" else ""
    after = whitespace if side == "after_colon" else ""
    text = '{"kty":"EC", "' + name + '"' + before + ":" + after + '"' + secret + '"}'
    assert json.loads(text)[name] == secret
    out = redact(text)
    assert secret not in out
    assert "[REDACTED_SECRET]" in out
    assert json.loads(out)["kty"] == "EC"


def test_ordinary_short_members_named_like_jwk_ones_are_kept():
    text = json.dumps({"q": "search terms", "p": 2, "d": "2026-10-10", "qi": "short_value"})
    assert redact(text) == text


@pytest.mark.parametrize("name", ["d", "p", "q", "dp", "dq", "qi"])
def test_a_long_slug_under_a_jwk_member_name_is_kept(name):
    # PR #1117 review: a kebab-case slug is not a key; real values are random
    # base64url and carry upper-case letters.
    text = json.dumps({name: "deployment-2026-10-10-production-release-candidate-7"})
    assert redact(text) == text


@pytest.mark.parametrize("kty,member", [("EC", "d"), ("OKP", "d"), ("RSA", "d"), ("oct", "k")])
@pytest.mark.parametrize("encoded", [False, True])
def test_lowercase_private_jwk_members_are_redacted(kty, member, encoded):
    secret = "a1b2_c3d4-" * 5
    text = json.dumps({"kty": kty, member: secret})
    if encoded:
        text = json.dumps({"encoded_key": text})
    out = redact(text)
    assert secret not in out
    assert "[REDACTED_SECRET]" in out


@pytest.mark.parametrize("ending", ["", "...", "…[truncated]"])
def test_an_unterminated_jwk_value_is_redacted(ending):
    # PR #1117 review: a value cut before its closing quote still leaks otherwise.
    d = b64url("Ec_Pr1vate-Scalar", 43)
    out = redact('{"kty": "EC", "crv": "P-256", "d": "' + d + ending)
    assert d not in out and d[:30] not in out


# --- 3. Credential in URL: linear time --------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a-" * 50_000, id="dashes"),
        pytest.param("a." * 50_000, id="dots"),
        pytest.param("a+" * 50_000, id="pluses"),
    ],
)
def test_the_credential_in_url_pattern_is_linear(text):
    pattern = dict(CREDENTIAL_SHAPE_PATTERNS)["credential in URL"]
    assert elapsed(pattern.sub, "", text) < 1.0
    assert elapsed(redact, text) < 2.0


def test_the_credential_in_url_pattern_still_matches():
    password = "s3cr" + "etpass"
    userinfo = "user" + ":" + password + "@"
    for scheme, rest in (
        ("https", "example.invalid/path"),
        ("postgres+psycopg", "db.invalid:5432/x"),
        ("see -git+ssh", "host.invalid"),
    ):
        url = scheme + ":" + "//" + userinfo + rest
        assert password not in redact(url)
    assert redact("https://example.invalid/a:b@c") == "https://example.invalid/a:b@c"


# --- 4. Unterminated private keys in other encodings ------------------------


def json_escape_slashes(text: str) -> str:
    return text.replace("/", "\\/")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(f"{begin()} {L1} {L2} {L3}", id="collapsed-onto-one-line"),
        pytest.param(f"{begin()}{L1}\n{L2}\n{L3}", id="body-on-the-header-line"),
        pytest.param(f"{begin()}{L1}{L2}{L3}", id="flattened-no-separators"),
        pytest.param(
            json.dumps(json.dumps(f"{begin()}\n{L1}\n{L2}\n{L3}")), id="double-json-escaped"
        ),
        pytest.param(f"{begin()}\\u000a{L1}\\u000a{L2}\\u000a{L3}", id="u000a-escapes"),
        pytest.param(f"<p>{begin()}<br>{L1}<br/>{L2}<br />{L3}</p>", id="html-br"),
        pytest.param(f"{begin()}&#10;{L1}&#13;&#10;{L2}&#x0a;{L3}", id="html-entities"),
        pytest.param(f"{begin()}\r{L1}\r{L2}\r{L3}", id="lone-cr"),
        pytest.param(repr([begin(), L1, L2, L3]), id="python-repr-list"),
        pytest.param(
            json_escape_slashes(json.dumps(f"{begin()}\n{L1}\n{L2}")), id="json-escaped-slashes"
        ),
        pytest.param(
            f"{begin()}\nproc-type: 4,ENCRYPTED\ndek-info: AES\n\n{L1}", id="lowercase-proc-type"
        ),
    ],
)
def test_unterminated_keys_in_other_encodings_are_redacted(text):
    out = redact(text)
    assert_no_key(out)


@pytest.mark.parametrize("last", ["QUJD", "QUJDREVG", "QUJDREVGR0hJ"])
def test_a_short_last_line_followed_by_ordinary_text_is_redacted(last):
    text = f"{begin()}\n{L1}\n{L2}\n{last}\nthanks for looking\nnext: value"
    out = scan(PEM_PRIVATE_KEY_UNTERMINATED_RE, text)
    assert_no_key(out, [L1, L2, last])
    assert out.endswith("\nthanks for looking\nnext: value")
    assert redact(text) == "[REDACTED_SECRET]"


def test_text_after_a_padded_last_line_is_kept():
    text = f"{begin()}\n{L1}\n{L3}\nthanks\nNote that this is kept"
    out = scan(PEM_PRIVATE_KEY_UNTERMINATED_RE, text)
    assert_no_key(out)
    assert out.endswith("\nthanks\nNote that this is kept")
    assert redact(text) == "[REDACTED_SECRET]"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param((begin() + " ") * 20_000, id="pem-headers-and-spaces"),
        pytest.param((begin() + "\\") * 20_000, id="pem-headers-and-backslashes"),
        pytest.param(begin() + "\\" * 100_000, id="pem-backslash-run"),
        pytest.param(begin() + "\\/" * 50_000, id="pem-escaped-slash-run"),
        pytest.param(begin() + "<br>" * 25_000, id="pem-br-run"),
        pytest.param(begin() + "', '" * 25_000, id="pem-repr-separator-run"),
        pytest.param((begin() + "\n" + "A" * 15 + ":") * 5_000, id="pem-near-miss-lines"),
        pytest.param(begin() + "\n" + "A" * 100_000 + ":", id="pem-long-near-miss"),
        pytest.param(("PuTTY-User-Key-File-" + "3: x\n") * 10_000, id="ppk-headers"),
        pytest.param("PuTTY-User-Key-File-" + "3: x" + "\nA" * 50_000, id="ppk-short-lines"),
        pytest.param('{"d": "' * 20_000, id="jwk-names"),
        pytest.param('"qi":"' + "A" * 100_000, id="jwk-unclosed-value"),
        pytest.param('"qi":"' + "a" * 100_000, id="jwk-unclosed-lowercase-value"),
        pytest.param(
            "PuTTY-User-Key-File-" + "3: x\nAAAA" + " " * 100_000, id="ppk-short-line-then-blanks"
        ),
        pytest.param(
            "PuTTY-User-Key-File-" + "3: x\n" + "the quick brown fox " * 5_000, id="ppk-prose"
        ),
        pytest.param("\\" * 100_000 + '"', id="jwk-backslash-run"),
        pytest.param("x://" * 25_000, id="url-schemes"),
        pytest.param("x://u:" + "p" * 100_000, id="url-no-at"),
    ],
)
def test_pathological_inputs_stay_linear(text):
    assert elapsed(redact, text) < 2.0


# --- 5. PR #1146 review repairs ----------------------------------------------


PGP_BEGIN = f"{DASHES}BEGIN PGP PRIVATE" + f" KEY BLOCK{DASHES}"
FIELD_VALUES = ["<user@example.invalid>", "Tom & Jerry", "C:\\dir\\key", "a <b> & 'c'"]


@pytest.mark.parametrize("value", FIELD_VALUES)
def test_a_pgp_armor_field_with_markup_characters_does_not_stop_the_scan(value):
    # Review P1-1: a field value stopped at '<', '&' or '\', and the body after
    # that line was left verbatim (a regression from main).
    out = redact(f"{PGP_BEGIN}\nVersion: x\nComment: {value}\n\n{L1}\n{L2}\n")
    assert_no_key(out, [L1, L2])


@pytest.mark.parametrize("value", FIELD_VALUES)
def test_gcloud_scrubbing_covers_a_pgp_field_with_markup_characters(value):
    from unified_agent.tools import gcloud

    out = gcloud._scrub_secret_patterns(f"#!/bin/sh\n{PGP_BEGIN}\nComment: {value}\n\n{L1}\n{L2}")
    assert_no_key(out, [L1, L2])


def test_encoded_breaks_after_a_field_still_continue_the_scan():
    for brk in ("\\n", "<br>", "&#10;", "\\\\n"):
        out = redact(f"{PGP_BEGIN}{brk}Comment: x{brk}{brk}{L1}{brk}{L2}")
        assert_no_key(out, [L1, L2])


@pytest.mark.parametrize("value", FIELD_VALUES)
@pytest.mark.parametrize("where", ["comment", "header"])
def test_a_putty_field_with_markup_characters_does_not_stop_the_scan(value, where):
    text, secrets = ppk()
    if where == "comment":
        text = text.replace("Comment: eddsa-key-fixture", f"Comment: {value}")
    else:
        text = text.replace("ssh-ed25519", f"ssh-ed25519 {value}", 1)
    out = redact(text)
    for secret in secrets:
        assert secret not in out


def test_a_short_last_putty_private_line_before_prose_is_redacted():
    # Review P2-5: no MAC line, and prose right after the short last line.
    text, secrets = ppk(with_mac=False)
    for out in (scan(PUTTY_PRIVATE_KEY_RE, f"{text}\nThanks for looking."), redact(text)):
        for secret in secrets[:-1]:
            assert secret not in out
        assert "QUJD" not in out
    assert scan(PUTTY_PRIVATE_KEY_RE, f"{text}\nThanks for looking.").endswith(
        "\nThanks for looking."
    )


def test_a_prose_word_after_a_full_pem_line_is_kept():
    # A short last line is a whole number of base64 quads; "thanks" is not one.
    out = scan(PEM_PRIVATE_KEY_UNTERMINATED_RE, f"{begin()}\n{L1}\nthanks for looking")
    assert_no_key(out, [L1])
    assert out.endswith("\nthanks for looking")


@pytest.mark.parametrize("word", ["That", "Somewhat", "Thanksgiving"])
def test_a_quad_length_prose_word_after_a_full_line_is_kept(word):
    # PR #1146 review P3: a 4, 8 or 12 letter word is a whole number of base64
    # quads, so the word that opens the prose after a full line was taken as
    # the key's last line. A last line ends its line; prose goes on.
    prose = f"{word} is all for now"
    out = scan(PEM_PRIVATE_KEY_UNTERMINATED_RE, f"{begin()}\n{L1}\n{prose}")
    assert_no_key(out, [L1])
    assert out.endswith(f"\n{prose}")
    text, secrets = ppk(with_mac=False)
    text = text.replace("\nQUJD", "")  # end the private block on a full line
    out = scan(PUTTY_PRIVATE_KEY_RE, f"{text}\n{prose}")
    assert out.endswith(f"\n{prose}")
    assert secrets[1] not in out


@pytest.mark.parametrize("last", ["Abcd", "Somewhat", "abcdEFGHijkl"])
def test_a_word_shaped_last_line_that_ends_its_line_is_still_redacted(last):
    for tail in ("", "\n", "\nthanks for looking", "\n" + DASHES + "END", "'"):
        text = f"{begin()}\n{L1}\n{last}{tail}"
        assert last not in scan(PEM_PRIVATE_KEY_UNTERMINATED_RE, text), text


def test_a_long_url_scheme_with_userinfo_is_still_redacted():
    # A 33+ character scheme defeated the word-boundary anchor of the bounded
    # scheme pattern; main redacted it.
    password = "s3cr" + "etpass"
    url = "a" * 40 + "://" + "user:" + password + "@host.invalid/"
    assert password not in redact(url)
    assert password not in redact("x_https://" + "user:" + password + "@host.invalid/")


@pytest.mark.parametrize(
    "value",
    ["HowDoIResetMyPasswordWhenLockedOutOfAccount", "WhyDoesTheNightlyBuildFailOnTheMacRunner"],
)
@pytest.mark.parametrize("name", ["d", "q", "qi"])
def test_a_long_camel_case_value_outside_a_jwk_is_kept(name, value):
    # PR #1144 review: with no JWK context the name-and-shape fallback took any
    # long camel-case text. Random base64url of 40+ characters all but always
    # holds a digit, '-' or '_' as well, so the fallback now requires one.
    text = json.dumps({name: value})
    assert redact(text) == text


# --- 6. Escape-like text inside a header field (PR #1146 second review) --------

ESCAPE_LIKE_VALUES = [
    "C:\\new",
    "C:\\new-key",
    "C:\\keys\\notes\\new",
    "C:\\rsa\\n",
    "a\\u000a b",
    "x\\nQUJD",
]


@pytest.mark.parametrize("value", ESCAPE_LIKE_VALUES)
def test_an_escape_like_header_value_does_not_end_the_pgp_scan(value):
    # A literal "\n" in raw text read as an escaped line break ended the scan,
    # leaving the body. When unsure whether text is still inside a key, redact.
    out = redact(f"{PGP_BEGIN}\nComment: {value}\n\n{L1}\n{L2}\n")
    assert_no_key(out, [L1, L2])


@pytest.mark.parametrize("value", ESCAPE_LIKE_VALUES)
def test_an_escape_like_header_value_does_not_end_the_putty_scan(value):
    text, secrets = ppk()
    out = redact(text.replace("Comment: eddsa-key-fixture", f"Comment: {value}"))
    for secret in secrets:
        assert secret not in out


@pytest.mark.parametrize("value", ESCAPE_LIKE_VALUES)
def test_an_escape_like_header_value_in_json_does_not_end_the_scan(value):
    text, secrets = ppk()
    encoded = json.dumps({"file": text.replace("eddsa-key-fixture", value), "next": "kept"})
    out = redact(encoded)
    for secret in secrets:
        assert secret not in out
    pgp = json.dumps({"key": f"{PGP_BEGIN}\nComment: {value}\n\n{L1}\n{L2}", "next": "kept"})
    assert_no_key(redact(pgp), [L1, L2])


@pytest.mark.parametrize("value", ESCAPE_LIKE_VALUES)
def test_gcloud_scrubbing_covers_an_escape_like_header_value(value):
    from unified_agent.tools import gcloud

    out = gcloud._scrub_secret_patterns(f"#!/bin/sh\n{PGP_BEGIN}\nComment: {value}\n\n{L1}\n{L2}")
    assert_no_key(out, [L1, L2])


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(PGP_BEGIN + "\nComment: x" + "\\n" * 50_000, id="escaped-break-run"),
        pytest.param(PGP_BEGIN + "\nComment: x" + "\\nA" * 50_000, id="escaped-break-short-lines"),
        pytest.param(
            PGP_BEGIN + "\nComment: x" + ("\\n" + "A" * 15 + "!") * 5_000, id="near-miss-lines"
        ),
        pytest.param(
            "PuTTY-User-Key-File-" + "3: x" + "\\n<br>&#10;" * 20_000, id="ppk-mixed-breaks"
        ),
    ],
)
def test_escape_like_header_values_stay_linear(text):
    assert elapsed(redact, text) < 2.0


# --- 7. Structural fail-closed key regions (PR #1146 review round 3) ------------

WINDOWS_COMMENT = "C:\\newkeybackupdirectory\\notes.txt"


def ppk_v2(comment: str = WINDOWS_COMMENT, with_mac: bool = True) -> tuple[str, list[str]]:
    text, secrets = ppk(version=2, with_mac=with_mac)
    return text.replace("Comment: eddsa-key-fixture", f"Comment: {comment}"), secrets


def pgp_unterminated(comment: str = WINDOWS_COMMENT) -> str:
    return f"{PGP_BEGIN}\nVersion: x\nComment: {comment}\n\n{L1}\n{L2}\n=AbCd"


@pytest.mark.parametrize("with_mac", [True, False])
@pytest.mark.parametrize("form", ["raw", "json", "gcloud"])
def test_a_windows_path_comment_cannot_hide_a_putty_v2_body(with_mac, form):
    # Round 3 P1: the directory name after a literal "\n" looked like base64, so
    # the field scan ended inside the comment and the private blob survived.
    text, secrets = ppk_v2(with_mac=with_mac)
    if form == "raw":
        out = redact(f"{text}\nafter")
    elif form == "json":
        out = redact(json.dumps({"file": text, "next": "kept"}))
        assert '"next": "kept"' in out
    else:
        from unified_agent.tools import gcloud

        out = gcloud._scrub_secret_patterns(f"#!/bin/sh\n{text}\nafter")
    for secret in secrets:
        assert secret not in out, form
    if with_mac and form != "json":
        assert out.endswith("\nafter")  # a closed key region ends at its MAC line


@pytest.mark.parametrize("form", ["raw", "json", "gcloud", "repr"])
def test_a_windows_path_comment_cannot_hide_an_unterminated_pgp_body(form):
    text = pgp_unterminated()
    if form == "raw":
        out = redact(text)
    elif form == "json":
        out = redact(json.dumps({"key": text, "next": "kept"}))
        assert '"next": "kept"' in out
    elif form == "repr":
        out = redact(repr({"key": text, "next": "kept"}))
        assert "'next': 'kept'" in out
    else:
        from unified_agent.tools import gcloud

        out = gcloud._scrub_secret_patterns(f"#!/bin/sh\n{text}")
    assert_no_key(out, [L1, L2])
    assert "=AbCd" not in out


def test_everything_between_an_opener_and_its_end_marker_is_redacted():
    body = f"Comment: {WINDOWS_COMMENT}\nnot base64 at all: {L1[:20]}!\n{L2}"
    text = f"before\n{PGP_BEGIN}\n{body}\n{DASHES}END PGP PRIVATE KEY BLOCK{DASHES}\nafter"
    out = redact(text)
    assert out == "before\n[REDACTED_SECRET]\nafter"


def test_an_unterminated_raw_key_is_redacted_to_the_end_of_the_text():
    out = redact(f"before\n{begin()}\nComment: odd \\n text\nprose that is not key {L1}")
    assert out == "before\n[REDACTED_SECRET]"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param((begin() + " x ") * 20_000, id="many-raw-openers"),
        pytest.param(('"' + begin() + '\\n x", ') * 20_000, id="many-string-openers"),
        pytest.param(("PuTTY-User-Key-File-" + "2: x\\n") * 20_000, id="many-ppk-openers"),
        pytest.param(("'" + begin() + "', 'x', ") * 20_000, id="many-repr-openers"),
        pytest.param('"' + begin() + "\\\\" * 100_000, id="opener-then-backslashes"),
    ],
)
def test_structural_key_regions_stay_linear(text):
    assert elapsed(redact, text) < 3.0


# --- 8. PR #1146 review round 4 --------------------------------------------------


@pytest.mark.parametrize(
    "lead", ["Here's the exported key: ", "user's key ", "it's\n", "'quoted' then ", "x "]
)
@pytest.mark.parametrize("form", ["telemetry", "gcloud"])
def test_an_apostrophe_in_prose_does_not_make_a_raw_key_a_string(lead, form):
    # Round 4 P1: a prose apostrophe opened a "string" on the opener's line, and
    # the region then ended at the comment's apostrophe, leaving the body.
    text = f"{lead}{PGP_BEGIN}\nComment: user's {WINDOWS_COMMENT}\n\n{L1}\n{L2}\n"
    if form == "telemetry":
        out = redact(text)
    else:
        from unified_agent.tools import gcloud

        out = gcloud._scrub_secret_patterns(text)
    assert_no_key(out, [L1, L2])


@pytest.mark.parametrize(
    "wrap",
    [
        lambda t: json.dumps({"key": t, "next": "kept"}),
        lambda t: json.dumps({"key": "\n" + t, "next": "kept"}),
        lambda t: repr({"key": t, "next": "kept"}),
    ],
    ids=["json", "json-after-escaped-break", "repr"],
)
def test_a_key_that_starts_a_string_ends_with_that_string(wrap):
    out = redact(wrap(f"{PGP_BEGIN}\nComment: user's {WINDOWS_COMMENT}\n\n{L1}\n{L2}"))
    assert_no_key(out, [L1, L2])
    assert "next" in out and "kept" in out


@pytest.mark.parametrize("form", ["raw", "json", "gcloud"])
def test_a_mac_line_inside_a_comment_does_not_close_a_putty_key(form):
    # Round 4 P1: "Private-MAC:" matched anywhere, so a comment holding it
    # closed the region before the private blob.
    text, secrets = ppk_v2(comment="Private-MAC: 1234 see " + WINDOWS_COMMENT)
    if form == "raw":
        out = redact(text)
    elif form == "json":
        out = redact(json.dumps({"file": text}))
    else:
        from unified_agent.tools import gcloud

        out = gcloud._scrub_secret_patterns(text)
    for secret in secrets:
        assert secret not in out, form
