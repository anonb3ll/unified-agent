"""Escaped JS IdentifierName members retain private evidence on every surface."""

import json
import secrets

import pytest

from unified_agent.audit.rules import check_secrets
from unified_agent.telemetry.jwk import private_members
from unified_agent.telemetry.redaction import redact_sensitive_content
from unified_agent.tools.public_relay import BLOCK, _jwk_findings


@pytest.fixture
def bootstrap_test_store():
    return False


@pytest.fixture(autouse=True)
def _forbid_store_access(isolated_env, monkeypatch):
    from unified_agent import storage

    def refused(*args, **kwargs):
        pytest.fail("pure JS identifier tests must not access a database")

    for name in ("init_db", "connect", "connect_read_only"):
        monkeypatch.setattr(storage, name, refused)


def assert_private(text, value):
    expected = {i for i, line in enumerate(text.splitlines(), 1) if value in line}
    observed = {
        "redacted": value not in redact_sensitive_content(text, max_len=20 * len(text) + 1000),
        "scanner": any(value in text[m.start : m.end] for m in private_members(text)),
        "audit": bool(expected)
        and expected <= {finding.line for finding in check_secrets("fixture.js", text)},
        "relay": any(
            value in text[f.start : f.end] and f.action == BLOCK for f in _jwk_findings(text)
        ),
    }
    assert all(observed.values()), observed


@pytest.mark.parametrize("key", [r"\u0064", r"\u{64}", r"\u{000064}", r"\\u0064"])
@pytest.mark.parametrize(
    "shape",
    [
        'const k = {"kty":"RSA",<key>:"<value>",kid:"public"};',
        'const t = `head ${{"kty":"RSA",<key>:"<value>"}} tail`;',
        'const t = `head ${`nested ${{kty:"RSA",<key>:"<value>"}}`}`;',
        'const keys = {nested:[{kty:"RSA",<key>:"<value>"}]};',
    ],
)
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_escaped_private_identifier_all_surfaces(key, shape, layers):
    value = "q" + secrets.token_hex(12) + "z"
    text = shape.replace("<key>", key).replace("<value>", value)
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("key", [r"k\u0074y", r"\u{6b}\u{74}\u{79}", r"\u006bty"])
@pytest.mark.parametrize("private", [r"d\u0070", r"\u0064p", r"\u{64}\u{70}"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_escaped_type_and_mixed_private_identifier(key, private, layers):
    value = "q" + secrets.token_hex(12) + "z"
    text = f'const x = {{{key}:"RSA",{private}:"{value}"}};'
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("key", [r"\u006fth", r"o\u{74}h"])
@pytest.mark.parametrize("member", [r"\u0072", r"\u{64}", r"\u0074"])
def test_escaped_other_primes_members_have_rsa_owner(key, member):
    value = "q" + secrets.token_hex(12) + "z"
    text = f'const x = {{kty:"RSA",{key}:[{{{member}:"{value}"}}]}};'
    assert_private(text, value)


@pytest.mark.parametrize("ending", ['"}', '"', ""])
@pytest.mark.parametrize("key", [r"\u0064", r"\u{64}"])
def test_malformed_mapping_and_string_still_retain_private_evidence(key, ending):
    value = "q" + secrets.token_hex(12) + "z"
    text = f'{{kty:"RSA",{key}:"{value}' + ending
    assert_private(text, value)


@pytest.mark.parametrize(
    "kind,key,type_key",
    [
        ("RSA", r"\u006e", "kty"),
        ("RSA", r"\u006b", "kty"),
        ("EC", r"\u0070", "kty"),
        ("EC", r"\u{64}escription", "kty"),
        ("RSA", r"\u006b", r"k\u0074y"),
        ("RSA", "n", r"\u{6b}\u{74}\u{79}"),
    ],
)
def test_escaped_identifiers_keep_public_roles(kind, key, type_key):
    value = "public" + secrets.token_hex(12) + "end"
    text = f'const x = {{{type_key}:"{kind}",{key}:"{value}"}};'
    assert redact_sensitive_content(text, max_len=10000) == text
    assert private_members(text) == []
    assert list(check_secrets("fixture.js", text)) == []
    assert not any(f.action == BLOCK for f in _jwk_findings(text))


@pytest.mark.parametrize("key", [r"k\u0074y", r"k\u{74}y"])
@pytest.mark.parametrize("member", ["k", r"\u006b"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_last_escaped_type_can_make_an_earlier_member_private(key, member, layers):
    value = "q" + secrets.token_hex(12) + "z"
    text = f'const x={{kty:"RSA",{member}:"{value}",{key}:"oct"}};'
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("width", [8, 128, 4096])
def test_braced_identifier_escape_with_leading_zeroes_has_bounded_decoding(width):
    value = "q" + secrets.token_hex(12) + "z"
    key = "\\u{" + "0" * width + "64}"
    text = f'const x={{kty:"RSA",{key}:"{value}"}};'
    assert_private(text, value)


@pytest.mark.parametrize("shape", ["`#${{<body>}}`", "`#${`#${{<body>}}`}`", "/#{<body>}/"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_literal_hash_cannot_hide_escaped_private_identifier(shape, layers):
    value = "q" + secrets.token_hex(12) + "z"
    body = 'kty:"RSA",' + r"\u{64}:" + repr(value)
    text = "const x=" + shape.replace("<body>", body) + ";"
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize(
    "kind,key", [("RSA", r"\u0070"), ("EC", r"\u0064"), ("OKP", r"\u{64}"), ("oct", r"\u006b")]
)
def test_escaped_identifiers_apply_existing_key_type_roles(kind, key):
    value = "q" + secrets.token_hex(12) + "z"
    text = f'const x={{kty:"{kind}",{key}:"{value}"}};'
    assert_private(text, value)


@pytest.mark.parametrize("key", [r"k\u0074y", r"k\u{74}y"])
def test_escaped_type_in_real_python_comment_cannot_retype_live_member(key):
    import ast

    value = "public" + secrets.token_hex(12) + "end"
    text = (
        "x={'kty':'RSA','k':" + repr(value) + ",'n':typeof / # /," + key + ":'oct',\n denominator}"
    )
    ast.parse(text)
    assert value in redact_sensitive_content(text, max_len=10000)
    assert not any(value in text[m.start : m.end] for m in private_members(text))
    assert not any(
        value in text[f.start : f.end] and f.action == BLOCK for f in _jwk_findings(text)
    )


@pytest.mark.parametrize("layers", [0, 1, 2])
@pytest.mark.parametrize("type_key", [r"k\u0074y", r"k\u{74}y"])
@pytest.mark.parametrize("copies", [1, 4, 32])
def test_live_private_unpack_ancestor_survives_commented_child_type(type_key, copies, layers):
    value = "q" + secrets.token_hex(12) + "z"
    child = '{kty:"RSA",k:' + repr(value) + ",n:typeof / # /," + type_key + ':"RSA",\n denominator}'
    text = "{**" * copies + child + "}" * (copies - 1) + "," + type_key + ':"oct"}'
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("layers", [0, 1, 2])
@pytest.mark.parametrize("control", ["no-comment-type", "plain-ancestor-type", "plain-child"])
def test_private_unpack_provenance_controls(control, layers):
    value = "q" + secrets.token_hex(12) + "z"
    key = r"k\u0074y"
    child = '{kty:"RSA",k:' + repr(value)
    if control != "plain-child":
        child += ",n:typeof / # /,"
        if control != "no-comment-type":
            child += key + ':"RSA",'
        child += "\n denominator"
    child += "}"
    outer_key = "kty" if control == "plain-ancestor-type" else key
    text = "{**" + child + "," + outer_key + ':"oct"}'
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


def test_live_public_unpack_ancestor_cannot_authorize_commented_private_type():
    value = "public" + secrets.token_hex(12) + "end"
    key = r"k\u0074y"
    text = (
        '{**{kty:"RSA",k:'
        + repr(value)
        + ",n:typeof / # /,"
        + key
        + ':"oct",\n denominator},'
        + key
        + ':"RSA"}'
    )
    assert value in redact_sensitive_content(text, max_len=10000)
    assert not any(value in text[m.start : m.end] for m in private_members(text))
    assert not any(
        value in text[f.start : f.end] and f.action == BLOCK for f in _jwk_findings(text)
    )


def _comment_type_wrapper(body, key, kind, before):
    note = "n:typeof / # /," + key + ":" + repr(kind) + ",\n denominator"
    return "{" + (note + ",**" + body if before else "**" + body + "," + note) + "}"


@pytest.mark.parametrize(
    "public_kind,private_kind,member",
    [("RSA", "oct", "k"), ("EC", "RSA", "p"), ("EC", "RSA", "dp")],
)
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_live_private_type_in_middle_of_comment_owned_ancestors(
    public_kind, private_kind, member, before, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    key = r"k\u0074y"
    leaf = "{kty:" + repr(public_kind) + "," + member + ":" + repr(value) + "}"
    child = _comment_type_wrapper(leaf, key, public_kind, not before)
    middle = "{**" + child + "," + key + ":" + repr(private_kind) + "}"
    text = _comment_type_wrapper(middle, key, public_kind, before)
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize(
    "public_kind,comment_kind,member",
    [("RSA", "oct", "k"), ("EC", "RSA", "p"), ("EC", "RSA", "dp")],
)
@pytest.mark.parametrize("before", [False, True])
def test_unrelated_live_middle_type_cannot_authorize_comment_private_roles(
    public_kind, comment_kind, member, before
):
    value = "public" + secrets.token_hex(12) + "end"
    key = r"k\u0074y"
    leaf = "{kty:" + repr(public_kind) + "," + member + ":" + repr(value) + "}"
    child = _comment_type_wrapper(leaf, key, comment_kind, not before)
    middle = "{**" + child + "," + key + ":" + repr(public_kind) + "}"
    text = _comment_type_wrapper(middle, key, comment_kind, before)
    assert value in redact_sensitive_content(text, max_len=10000)
    assert not any(value in text[m.start : m.end] for m in private_members(text))
    assert not any(
        value in text[f.start : f.end] and f.action == BLOCK for f in _jwk_findings(text)
    )
