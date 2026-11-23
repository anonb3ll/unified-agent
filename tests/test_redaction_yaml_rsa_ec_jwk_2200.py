"""YAML RSA, EC and OKP JWKs keep their private members private (task #2200).

The JSON scan typed an object only from a quoted type string, and the oct-block
rule reads YAML only for symmetric keys. Nothing typed a YAML RSA, EC or OKP
key, so an unquoted asymmetric type in a flow mapping and every block-mapping JWK
passed all three consumers with the private scalar in place.

Every key-shaped fixture is generated at runtime and the type names are
assembled, so no line of this file opens a block for the security audit. Each
fixture is loaded with PyYAML first, to prove it is the key the test claims.
"""

from __future__ import annotations

import base64
import json
import secrets

import pytest
import yaml
from growth_budget import assert_cpu_growth, cold, peak_growth

from unified_agent.audit.rules import check_secrets
from unified_agent.telemetry import jwk, yaml_jwk
from unified_agent.telemetry.redaction import redact_sensitive_content
from unified_agent.tools.public_relay import BLOCK, _jwk_findings

TYPE = "kt" + "y"
RSA, EC, OKP, OCT = "R" + "SA", "E" + "C", "O" + "KP", "o" + "ct"
KINDS = (RSA, EC, OKP)
PRIVATE = {RSA: ("d", "p", "q", "dp", "dq", "qi"), EC: ("d",), OKP: ("d",)}
PUBLIC = {
    RSA: ("n", "e", "kid", "alg", "use"),
    EC: ("crv", "x", "y", "kid"),
    OKP: ("crv", "x", "kid", "use"),
}
KEEP = "keep-value-2200"
# Assembled, as the type name is: a literal merge key or alias key in a fixture
# string would make this file read as a YAML document that uses them.
MERGE = "<" + "<"
STAR = "*"
BREAKS = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}


@pytest.fixture
def bootstrap_test_store():
    return False


@pytest.fixture(autouse=True)
def _forbid_store_access(isolated_env, monkeypatch):
    from unified_agent import config, storage

    def refused(*args, **kwargs):
        pytest.fail("pure redaction tests must not access a database")

    for name in ("init_db", "connect", "connect_read_only"):
        monkeypatch.setattr(storage, name, refused)
    assert not config.db_path().exists()
    yield
    assert not config.db_path().exists()


def secret() -> str:
    """A fresh base64url private scalar; never a committed literal."""
    return "S" + base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")


def public(name: str) -> str:
    return f"pub-{name}-{secrets.token_hex(3)}"


def redact(text: str) -> str:
    return redact_sensitive_content(text, max_len=len(text) * 20 + 1000)


def lines_of(text: str, needle: str) -> set[int]:
    return {n for n, row in enumerate(text.splitlines(), 1) if needle in row}


def jwk_lines(text: str) -> set[int]:
    """Lines the secrets audit attributes to its JWK private-member rule."""
    return {
        finding.line
        for finding in check_secrets("fixture.yaml", text)
        if "JWK private member" in finding.message
    }


def protect(text: str, privates, publics=(), *, kept: bool = True) -> str:
    """All three consumers hide or report every private fragment, on its own line.

    Public fragments stay in the redacted text (when ``kept``) and are never on
    a line the audit attributes to a private member, unless they share it.
    """
    out = redact(text)
    members = jwk.private_members(text)
    findings = _jwk_findings(text)
    audited = jwk_lines(text)
    private_lines: set[int] = set()
    for fragment in privates:
        assert fragment not in out, out
        assert any(fragment in text[m.start : m.end] for m in members), fragment
        assert lines_of(text, fragment) <= audited, (fragment, audited)
        assert any(f.action == BLOCK and fragment in text[f.start : f.end] for f in findings)
        private_lines |= lines_of(text, fragment)
    for fragment in publics:
        if kept:
            assert fragment in out, (fragment, out)
            assert not any(fragment in text[m.start : m.end] for m in members), fragment
        lines = lines_of(text, fragment)
        if not lines & private_lines:
            assert not lines & audited, (fragment, audited)
    return out


def unchanged(text: str, fragments) -> None:
    """Nothing in ``fragments`` is masked, reported or attributed."""
    out = redact(text)
    members = jwk.private_members(text)
    for fragment in fragments:
        assert fragment in out, (fragment, out)
        assert not any(fragment in text[m.start : m.end] for m in members), fragment
        assert not any(fragment in text[f.start : f.end] for f in _jwk_findings(text))
        assert not lines_of(text, fragment) & jwk_lines(text)


def typed(node) -> list[dict]:
    """Every mapping PyYAML builds that carries a key type."""
    found, pending = [], [node]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if TYPE in item:
                found.append(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def assert_key(text: str, kind: str, members: dict) -> None:
    """PyYAML reads ``text`` as holding a ``kind`` key with exactly these members."""
    keys = [m for m in typed(yaml.safe_load(text)) if m[TYPE] == kind]
    assert any(all(m.get(k) == v for k, v in members.items()) for m in keys), (keys, members)


# --- The task's reproductions ------------------------------------------------------


def test_the_reported_flow_and_block_leaks_are_closed():
    d = secret()
    rsa = f"outer: {{{TYPE}: {RSA}, n: abc, e: AQAB, d: {d}}}"
    ec = f"outer: {{{TYPE}: {EC}, crv: P-256, x: a, y: b, d: {d}}}"
    block = f"outer:\n  {TYPE}: {RSA}\n  d: {d}\n"
    for text, kind in ((rsa, RSA), (ec, EC), (block, RSA)):
        assert_key(text, kind, {"d": d})
        protect(text, [d])
    out = protect(rsa, [d])
    assert "n: abc" in out and "e: AQAB" in out
    out = protect(ec, [d])
    assert "crv: P-256" in out and "x: a" in out and "y: b" in out
    # The unquoted type now finds the member, as a quoted type already did.
    assert len(jwk.private_members(rsa)) == 1
    assert len(jwk.private_members(rsa.replace(f": {RSA},", f': "{RSA}",'))) == 1


# --- Flow mappings with a plain type -----------------------------------------------

NAME_QUOTES = {"plain": "{}", "double": '"{}"', "single": "'{}'"}


def flow_members(kind: str, quote: str, layout: str):
    privates = {name: secret() for name in PRIVATE[kind]}
    publics = {name: public(name) for name in PUBLIC[kind]}
    q = NAME_QUOTES[quote].format
    pairs = [f"{q(name)}: {value}" for name, value in publics.items()]
    pairs += [f"{q(name)}: {value}" for name, value in privates.items()]
    head = f"{TYPE}: {kind}"
    if layout == "type-first":
        body = "{" + ", ".join([head, *pairs]) + "}"
    elif layout == "type-last":
        body = "{" + ", ".join([*pairs, head]) + "}"
    elif layout == "type-comment":
        # The plain type ends at a comment; the separator is on the next line.
        body = "{" + head + " # the type\n  , " + ",\n  ".join(pairs) + "\n}"
    elif layout == "type-tagged":
        body = "{" + ", ".join([f"{TYPE}: !!str {kind}", *pairs]) + "}"
    else:
        assert layout == "multiline"
        body = "{\n  " + head + ",  # the type\n  " + ",\n  ".join(pairs) + "\n}"
    return body, privates, publics


FLOW_CONTEXTS = {
    "outer": "outer: {obj}",
    "top": "{obj}",
    "nested": "a:\n  b: {{c: {obj}}}",
    "flow-sequence": "keys: [{{{t}: {other}, n: {keep}}}, {obj}]",
    "block-sequence": "keys:\n  - {obj}",
}
FLOW_LAYOUTS = ("type-first", "type-last", "type-comment", "type-tagged", "multiline")


@pytest.mark.parametrize("layout", FLOW_LAYOUTS)
@pytest.mark.parametrize("quote", list(NAME_QUOTES))
@pytest.mark.parametrize("context", list(FLOW_CONTEXTS))
@pytest.mark.parametrize("kind", KINDS)
def test_a_flow_mapping_with_a_plain_type_masks_only_private_members(kind, context, quote, layout):
    body, privates, publics = flow_members(kind, quote, layout)
    other = EC if kind == RSA else RSA
    text = FLOW_CONTEXTS[context].format(obj=body, t=TYPE, other=other, keep=KEEP)
    assert_key(text, kind, privates | publics)
    protect(text, privates.values(), publics.values())
    if context == "flow-sequence":
        assert KEEP in redact(text)


@pytest.mark.parametrize("kind", KINDS)
def test_a_plain_type_in_an_encoded_string_masks_the_string(kind):
    body, privates, _ = flow_members(kind, "plain", "type-first")
    inner = f"outer: {body}"
    assert_key(inner, kind, privates)
    text = json.dumps({"log": inner})
    out = protect(text, privates.values())
    assert json.loads(out).keys() == {"log"}


def test_rsa_other_primes_in_a_flow_mapping_are_masked():
    r, d, t = secret(), secret(), secret()
    text = f"{{{TYPE}: {RSA}, n: pub, oth: [{{r: {r}, d: {d}, t: {t}}}]}}"
    assert yaml.safe_load(text)["oth"] == [{"r": r, "d": d, "t": t}]
    out = protect(text, [r, d, t])
    assert "n: pub" in out


def test_ec_and_okp_keep_rsa_only_names_in_a_flow_mapping():
    d = secret()
    for kind in (EC, OKP):
        text = f"{{{TYPE}: {kind}, p: {KEEP}, qi: {KEEP}x, d: {d}}}"
        assert_key(text, kind, {"d": d, "p": KEEP})
        out = protect(text, [d], [KEEP])
        assert f"qi: {KEEP}x" in out


def test_a_plain_oct_type_inside_a_decided_object_masks_k():
    # The decided RSA object opens no oct block, so the inner symmetric key's
    # unquoted type must be read by the JSON scan itself.
    k = secret()
    text = f'{{"{TYPE}": "{RSA}", "n": "pub", "inner": {{{TYPE}: {OCT}, k: {k}}}}}'
    assert yaml.safe_load(text)["inner"] == {TYPE: OCT, "k": k}
    out = protect(text, [k])
    assert '"n": "pub"' in out


def test_a_plain_rsa_type_inside_a_decided_oct_object_masks_d():
    d = secret()
    text = f'{{"{TYPE}": "{OCT}", "kid": "{KEEP}", "inner": {{{TYPE}: {RSA}, n: pub, d: {d}}}}}'
    assert yaml.safe_load(text)["inner"]["d"] == d
    out = protect(text, [d])
    assert KEEP in out and "n: pub" in out


@pytest.mark.parametrize(
    "spelling", ["RSAx", "rsa", "RSA extra", "RSA.value", "R SA", "xRSA", "EC2", "OKP-1"]
)
def test_a_flow_type_that_is_not_a_key_type_masks_nothing(spelling):
    text = f"outer: {{{TYPE}: {spelling}, d: {KEEP}, p: {KEEP}p}}"
    assert yaml.safe_load(text)["outer"][TYPE] != RSA
    unchanged(text, [KEEP, KEEP + "p"])


def test_a_flow_ec_key_keeps_its_public_members_byte_for_byte():
    d = secret()
    text = f"jwk: {{{TYPE}: {EC}, crv: P-256, x: xx, y: yy, kid: k1, d: {d}}}"
    assert redact(text) == text.replace(d, '"[REDACTED_SECRET]"')


# --- Block mappings ----------------------------------------------------------------

BLOCK_CONTEXTS = ("top", "nested", "deep", "sequence", "compact-sequence", "second-item")
BLOCK_LAYOUTS = ("type-first", "type-last", "type-middle", "commented", "quoted")


def block_fixture(kind: str, context: str, layout: str, brk: str):
    privates = {name: secret() for name in PRIVATE[kind]}
    publics = {name: public(name) for name in PUBLIC[kind]}
    head = f"{TYPE}: {kind}"
    pairs = [f"{name}: {value}" for name, value in publics.items()]
    secret_pairs = [f"{name}: {value}" for name, value in privates.items()]
    if layout == "type-first":
        rows = [head, *pairs, *secret_pairs]
    elif layout == "type-last":
        rows = [*secret_pairs, *pairs, head]
    elif layout == "type-middle":
        rows = [*secret_pairs[:1], *pairs, head, *secret_pairs[1:]]
    elif layout == "commented":
        rows = [f"{head}  # the type", "# a comment line between members"]
        rows += [f"{pair}  # a public member" for pair in pairs]
        rows += ["", *[f"{pair}  # private" for pair in secret_pairs]]
    else:
        assert layout == "quoted"
        rows = [f'"{TYPE}": "{kind}"', *pairs]
        rows += [
            (f'"{name}": {value}' if i % 2 else f"'{name}': '{value}'")
            for i, (name, value) in enumerate(privates.items())
        ]

    def indented(prefix_first: str, prefix_rest: str) -> list[str]:
        return [
            (prefix_first if i == 0 else prefix_rest) + row if row else row
            for i, row in enumerate(rows)
        ]

    if context == "top":
        lines = indented("", "")
    elif context == "nested":
        lines = ["jwk:", *indented("  ", "  "), "other: " + KEEP]
    elif context == "deep":
        lines = ["a:", "  b:", "    c:", *indented("      ", "      "), "  z: " + KEEP]
    elif context == "sequence":
        lines = ["keys:", *indented("  - ", "    "), "  - n: " + KEEP]
    elif context == "compact-sequence":
        lines = ["keys:", *indented("- ", "  "), "use: " + KEEP]
    else:
        assert context == "second-item"
        lines = ["keys:", f"  - {TYPE}: {OCT}x", "    d: " + KEEP, *indented("  - ", "    ")]
    text = BREAKS[brk].join(lines) + BREAKS[brk]
    return text, privates, publics


@pytest.mark.parametrize("brk", list(BREAKS))
@pytest.mark.parametrize("layout", BLOCK_LAYOUTS)
@pytest.mark.parametrize("context", BLOCK_CONTEXTS)
@pytest.mark.parametrize("kind", KINDS)
def test_a_block_mapping_jwk_masks_only_its_private_members(kind, context, layout, brk):
    text, privates, publics = block_fixture(kind, context, layout, brk)
    assert_key(text, kind, privates | publics)
    out = protect(text, privates.values(), publics.values())
    assert KEEP in out or context == "top"
    # Every line keeps its place: the redaction never joins or splits lines.
    assert len(out.splitlines()) == len(text.splitlines())


def multiline_value(style: str, indent: str, brk: str):
    """A private value spanning lines, and the fragments it must hide."""
    first, second = secret(), secret()
    deeper = indent + "  "
    if style == "plain":
        return f"{first}{brk}{deeper}{second}", [first, second], f"{first} {second}"
    if style == "double":
        return f'"{first}{brk}{deeper}{second}"', [first, second], f"{first} {second}"
    if style == "double-column-0":
        # PyYAML lets a quoted scalar continue at any indentation.
        return f'"{first}{brk}{second}"', [first, second], f"{first} {second}"
    if style == "single":
        return f"'{first}{brk}{deeper}{second}'", [first, second], f"{first} {second}"
    if style == "literal":
        value = f"|{brk}{deeper}{first}{brk}{brk}{deeper}{second}"
        return value, [first, second], f"{first}\n\n{second}\n"
    if style == "folded":
        return f">-{brk}{deeper}{first}{brk}{deeper}{second}", [first, second], f"{first} {second}"
    if style == "next-line":
        return f"{brk}{deeper}{first}", [first], first
    if style == "next-line-comment":
        return f"  # the value follows{brk}{deeper}{first}", [first], first
    assert style == "tagged"
    return f"!!str {first}", [first], first


MULTILINE = (
    "plain",
    "double",
    "double-column-0",
    "single",
    "literal",
    "folded",
    "next-line",
    "next-line-comment",
    "tagged",
)


@pytest.mark.parametrize("brk", list(BREAKS))
@pytest.mark.parametrize("nesting", ["top", "nested", "sequence"])
@pytest.mark.parametrize("style", MULTILINE)
def test_a_multi_line_private_value_is_masked_whole(style, nesting, brk):
    newline = BREAKS[brk]
    indent = {"top": "", "nested": "  ", "sequence": "    "}[nesting]
    value, fragments, loaded = multiline_value(style, indent, newline)
    n = public("n")
    first = {"top": "", "nested": "  ", "sequence": "  - "}[nesting]
    rows = [f"{first}{TYPE}: {RSA}", f"{indent}d: {value}", f"{indent}n: {n}"]
    head = {"top": [], "nested": ["jwk:"], "sequence": ["keys:"]}[nesting]
    text = newline.join([*head, *rows]) + newline
    assert_key(text, RSA, {"d": loaded, "n": n})
    out = protect(text, fragments, [n])
    assert f"n: {n}" in out


@pytest.mark.parametrize("brk", list(BREAKS))
@pytest.mark.parametrize("layout", ["indented", "compact"])
def test_rsa_other_primes_in_a_block_mapping_are_masked(layout, brk):
    r, d, t, n = secret(), secret(), secret(), public("n")
    item = "    " if layout == "indented" else "  "
    rows = [
        "jwk:",
        f"  {TYPE}: {RSA}",
        "  oth:",
        f"{item}- r: {r}",
        f"{item}  d: {d}",
        f"{item}  t: {t}",
        f"  n: {n}",
    ]
    text = BREAKS[brk].join(rows) + BREAKS[brk]
    assert yaml.safe_load(text)["jwk"]["oth"] == [{"r": r, "d": d, "t": t}]
    protect(text, [r, d, t], [n])


def test_a_type_on_the_next_line_types_the_mapping():
    d = secret()
    text = f"jwk:\n  {TYPE}:\n    {RSA}\n  d: {d}\n"
    assert_key(text, RSA, {"d": d})
    protect(text, [d])


@pytest.mark.parametrize("value", ['"R\\x53A"', '"R\\\n    SA"', "!!str RSA", "&anchor RSA"])
def test_a_quoted_escaped_or_tagged_block_type_is_read(value):
    d = secret()
    text = f"jwk:\n  {TYPE}: {value.replace('RSA', RSA)}\n  d: {d}\n"
    assert_key(text, RSA, {"d": d})
    protect(text, [d])


def test_escaped_member_names_are_read():
    d, p = secret(), secret()
    text = f'jwk:\n  "\\x6b\\x74y": {RSA}\n  "\\x64": {d}\n  "\\u0070": {p}\n'
    assert_key(text, RSA, {"d": d, "p": p})
    protect(text, [d, p])


def test_many_keys_in_one_document_are_each_typed_by_their_own_mapping():
    d1, d2, p = secret(), secret(), public("p")
    text = (
        f"keys:\n  - {TYPE}: {RSA}\n    d: {d1}\n"
        f"  - {TYPE}: {EC}\n    p: {p}\n    d: {d2}\n"
        f"  - {TYPE}: {OCT}x\n    d: {KEEP}\n"
    )
    loaded = yaml.safe_load(text)["keys"]
    assert loaded[0]["d"] == d1 and loaded[1]["d"] == d2 and loaded[2]["d"] == KEEP
    protect(text, [d1, d2], [p, KEEP])


def test_a_block_jwk_inside_an_encoded_string_masks_the_string():
    d = secret()
    inner = f"jwk:\n  {TYPE}: {RSA}\n  n: pub\n  d: {d}\n"
    assert_key(inner, RSA, {"d": d})
    text = json.dumps({"log": inner})
    out = protect(text, [d])
    assert json.loads(out).keys() == {"log"}


def test_a_block_jwk_inside_a_python_string_is_masked():
    d = secret()
    inner = f"jwk:\n  {TYPE}: {RSA}\n  n: pub\n  d: {d}\n"
    text = f'CONFIG = """\n{inner}"""\n'
    out = protect(text, [d])
    assert "n: pub" in out


def test_a_commented_out_block_jwk_is_masked():
    d = secret()
    text = f"settings:\n  # jwk:\n  #   {TYPE}: {RSA}\n  #   n: pub\n  #   d: {d}\n  x: 1\n"
    assert yaml.safe_load(text) == {"settings": {"x": 1}}
    out = protect(text, [d])
    assert "n: pub" in out


def test_audit_attribution_stays_on_the_private_lines():
    d, p, n = secret(), secret(), public("n")
    text = f"jwk:\n  {TYPE}: {RSA}\n  n: {n}\n  d: {d}\n  p: |\n    {p}\n  e: AQAB\n"
    assert_key(text, RSA, {"d": d, "p": p + "\n", "n": n})
    protect(text, [d, p], [n])
    # The d line, the p header and the p content; never the type or a public line.
    assert jwk_lines(text) == {4, 5, 6}
    finding = next(f for f in _jwk_findings(text) if d in text[f.start : f.end])
    assert finding.detail == "JWK private member"
    assert text[finding.start : finding.end] == d


# --- Negative cases ----------------------------------------------------------------


def test_a_d_at_a_different_indentation_level_is_not_a_member():
    d = secret()
    text = (
        f"d: {KEEP}top\n"
        f"jwk:\n  {TYPE}: {RSA}\n  n: pub\n  d: {d}\n"
        f"  meta:\n    d: {KEEP}child\n    p: {KEEP}childp\n"
        f"  more:\n  - d: {KEEP}item\n"
    )
    loaded = yaml.safe_load(text)
    assert loaded["d"] == KEEP + "top" and loaded["jwk"]["meta"]["d"] == KEEP + "child"
    assert loaded["jwk"]["more"] == [{"d": KEEP + "item"}]
    protect(text, [d])
    unchanged(text, [KEEP + "top", KEEP + "child", KEEP + "childp", KEEP + "item"])


def test_a_d_in_an_unrelated_mapping_is_not_a_member():
    d = secret()
    text = (
        f"before:\n  d: {KEEP}before\n"
        f"jwk:\n  {TYPE}: {EC}\n  d: {d}\n"
        f"after:\n  d: {KEEP}after\n  p: {KEEP}p\n"
        f"keys:\n- {TYPE}: {EC}\n  x: pub\n- d: {KEEP}item\n"
    )
    loaded = yaml.safe_load(text)
    assert loaded["after"]["d"] == KEEP + "after" and loaded["keys"][1] == {"d": KEEP + "item"}
    protect(text, [d])
    unchanged(text, [KEEP + "before", KEEP + "after", KEEP + "p", KEEP + "item"])


def test_a_document_marker_ends_the_mapping():
    text = f"{TYPE}: {RSA}\nn: pub\n---\nd: {KEEP}\n"
    assert list(yaml.safe_load_all(text))[1] == {"d": KEEP}
    unchanged(text, [KEEP])


@pytest.mark.parametrize("spelling", ["RSAx", "rsa", "RSA extra", "'RSA '", '"RSA-1"', "ECDH"])
def test_a_block_type_that_is_not_a_key_type_masks_nothing(spelling):
    text = f"jwk:\n  {TYPE}: {spelling}\n  d: {KEEP}\n  p: {KEEP}p\n"
    assert yaml.safe_load(text)["jwk"][TYPE] not in KINDS
    unchanged(text, [KEEP, KEEP + "p"])


def test_ec_and_okp_keep_rsa_only_names_in_a_block_mapping():
    for kind in (EC, OKP):
        d = secret()
        text = f"jwk:\n  {TYPE}: {kind}\n  p: {KEEP}\n  qi: {KEEP}qi\n  oth: {KEEP}oth\n  d: {d}\n"
        assert_key(text, kind, {"d": d, "p": KEEP})
        protect(text, [d], [KEEP, KEEP + "qi", KEEP + "oth"])


def test_a_mapping_without_a_type_is_unchanged():
    text = f"jwk:\n  n: pub\n  d: {KEEP}\n  p: {KEEP}p\n"
    assert redact(text) == text
    assert jwk.private_members(text) == []


# --- JSON parity -------------------------------------------------------------------


def test_a_multi_line_json_rsa_object_is_unchanged_byte_for_byte():
    d, p = secret(), secret()
    obj = {TYPE: RSA, "n": "pub", "e": "AQAB", "d": d, "p": p}
    for text in (json.dumps(obj, indent=2), "jwk: " + json.dumps(obj), repr(obj)):
        expected = text
        for value in (d, p):
            expected = expected.replace(f'"{value}"', '"[REDACTED_SECRET]"')
            expected = expected.replace(f"'{value}'", '"[REDACTED_SECRET]"')
        assert redact(text) == expected
        protect(text, [d, p])


def test_a_json_rsa_object_inside_a_block_mapping_keeps_its_decision():
    d = secret()
    obj = json.dumps({TYPE: RSA, "n": "pub", "d": d})
    text = f"wrapper:\n  {TYPE}: {RSA}\n  inner: {obj}\n  q: {KEEP}\n"
    out = redact(text)
    assert out == text.replace(f'"{d}"', '"[REDACTED_SECRET]"').replace(KEEP, '"[REDACTED_SECRET]"')
    protect(text, [d])


# --- Growth ------------------------------------------------------------------------


def _many_block_keys(n: int) -> str:
    rows = (f"  - {TYPE}: {RSA}\n    n: n{i}\n    d: value{i}\n    e: AQAB\n" for i in range(n))
    return "keys:\n" + "".join(rows)


def _one_long_mapping(n: int) -> str:
    rows = "".join(f"  m{i}: v{i}\n  d: value{i}\n" for i in range(n))
    return f"jwk:\n  {TYPE}: {EC}\n" + rows


def _one_deep_mapping(n: int) -> str:
    rows = "".join(
        f"{'  ' * i}{TYPE}: {RSA}\n{'  ' * i}d: value{i}\n{'  ' * i}oth:\n" for i in range(n)
    )
    return rows + "  " * n + "r: last\n"


def _many_flow_keys(n: int) -> str:
    rows = (f"  - {{{TYPE}: {EC}, crv: P-256, x: x{i}, d: value{i}}}\n" for i in range(n))
    return "keys:\n" + "".join(rows)


def _many_aliased_anchors_on_one_line(n: int) -> str:
    anchors = ", ".join(f'a{i}: &v{i} "x{i}"' for i in range(n))
    rows = "".join(f"  - {{{TYPE}: {EC}, crv: P-256, d: *v{i}}}\n" for i in range(n))
    return "vals: {" + anchors + "}\nkeys:\n" + rows


def _many_merges(n: int) -> str:
    rows = "".join(f"  - {MERGE}: *b\n    n: n{i}\n    d: value{i}\n" for i in range(n))
    return f"base: &b {{{TYPE}: {RSA}}}\nkeys:\n" + rows


def _many_alias_keys(n: int) -> str:
    rows = "".join(f"  - {TYPE}: {RSA}\n    n: n{i}\n    {STAR}k : value{i}\n" for i in range(n))
    return "name: &k d\nkeys:\n" + rows


def _flow_types_after_unfinished_tags(n: int) -> str:
    # No blank after the separator: with one, the oct-block rule's URI skip is
    # quadratic on this shape already on main (reported separately).
    return "{" + (TYPE + ":!<") * n + ", d: value}"


def _flow_types_after_nested_comments(n: int) -> str:
    return "# {" + (TYPE + ": # ") * n + "\nx: 1\n"


def _conservative_flow_properties(n: int) -> str:
    head = f"base: &b {{{TYPE}: {RSA}}}\njwk:\n  {MERGE}: *b\n  d: value\n"
    return head + "x: {" + ", !<a" * n + "}\n"


def _many_anchor_candidates(n: int) -> str:
    fakes = "".join(f"# fake &v {i}\nnote{i}: text &v here\n" for i in range(n))
    rows = "".join(f"  - {TYPE}: {RSA}\n    d: *v\n" for i in range(n))
    return "value: &v secret\n" + fakes + "keys:\n" + rows


def _many_duplicate_types(n: int) -> str:
    return "".join(f'- {{{TYPE}: "none", {TYPE}: {RSA}, n: n{i}, d: value{i}}}\n' for i in range(n))


def _long_interpolated_template(n: int) -> str:
    body = "".join(f"  k${{part}}y: {RSA}\\n  n: ${{n{i}}}\\n  d: value{i}\\n" for i in range(n))
    return "const keys = `keys:\\n" + body + "`;\n"


def _many_merge_forms_and_anchored_items(n: int) -> str:
    rows = "".join(
        f"  - ? !!merge {MERGE}\n    : *b\n    d: value{i}\n  - &a{i}\n    p: x{i}\n"
        for i in range(n)
    )
    return f"b: &b {{{TYPE}: {RSA}}}\nkeys:\n" + rows


def _many_commented_anchored_flow_nodes(n: int) -> str:
    rows = "".join(f"  - &a{i} # note\n    # more\n    {{d: value{i}}}\n" for i in range(n))
    return f"pieces:\n{rows}typed:\n  {TYPE}: {RSA}\n  {MERGE}: *a0\n"


def _long_anchor_names(n: int) -> str:
    rows = "".join(f"  - &{'a' * 300}{i} {{d: value{i}}}\n" for i in range(n))
    return f"pieces:\n{rows}typed:\n  {TYPE}: {RSA}\n  {MERGE}: *{'a' * 300}0\n"


def _many_quoted_hash_keys(n: int) -> str:
    rows = "".join(f"\"k # {i}\" 'q # {i}': &a{i} # c\n  {{d: value{i}}}\n" for i in range(n))
    return rows + f"typed:\n  {TYPE}: {RSA}\n  {MERGE}: *a0\n"


def _many_glued_flow_comments(n: int) -> str:
    rows = "".join(f"  a{i}: [1]#c\n  ,#d\n" for i in range(n))
    return f"b: &b {{{TYPE}: {RSA}}}\njwk: {{#c\n{rows}  {MERGE}: *b, d: value}}\n"


def _long_flow_line(n: int) -> str:
    items = ", ".join(f"a{i}: x\"#c{i}, b{i}: 'q{i}'#c" for i in range(n))
    return f"b: &b {{{TYPE}: {RSA}}}\njwk: {{{items}, {MERGE}: *b, d: value}}\n"


def _many_glued_hash_scalars(n: int) -> str:
    rows = "".join(f'  - {{{TYPE}: {RSA}, a: x"#{i}, d: value{i}}}\n' for i in range(n))
    return "keys:\n" + rows


def _many_block_scalars(n: int) -> str:
    rows = "".join(f"  k{i}: |\n    # not a comment {i}\n    'x\" #y\n" for i in range(n))
    return f"jwk:\n  {TYPE}: {RSA}\n{rows}  d: value\n"


def _deep_divergent_flow(n: int) -> str:
    opening = f'{{{TYPE}: {RSA}, a: x"#c, oth: [' * n
    return "jwk: " + opening + "{r: value}" + "]}" * n + "\n"


def _many_documents_in_one_literal(n: int) -> str:
    # "''''" opens a Python triple quote the object scan runs to the end: every
    # member it finds takes that literal's span, clamped to its own document.
    rows = "".join(f"---\njwk: {{{TYPE}: {OKP}, d: value{i}}}\n" for i in range(n))
    return "x: ''''\n" + rows


GROWING = {
    "many-block-keys": (_many_block_keys, 250, 1_000),
    "one-long-mapping": (_one_long_mapping, 250, 1_000),
    # Indentation makes the text quadratic in depth: double the depth, 4x the text.
    "one-deep-mapping": (_one_deep_mapping, 60, 120),
    "many-flow-keys": (_many_flow_keys, 250, 1_000),
    "many-aliased-anchors-on-one-line": (_many_aliased_anchors_on_one_line, 250, 1_000),
    "many-merges": (_many_merges, 250, 1_000),
    "many-alias-keys": (_many_alias_keys, 250, 1_000),
    "flow-types-after-unfinished-tags": (_flow_types_after_unfinished_tags, 1_000, 4_000),
    "flow-types-after-nested-comments": (_flow_types_after_nested_comments, 1_000, 4_000),
    "conservative-flow-properties": (_conservative_flow_properties, 1_000, 4_000),
    "many-anchor-candidates": (_many_anchor_candidates, 250, 1_000),
    "many-duplicate-types": (_many_duplicate_types, 250, 1_000),
    "long-interpolated-template": (_long_interpolated_template, 250, 1_000),
    "many-merge-forms-and-anchored-items": (_many_merge_forms_and_anchored_items, 250, 1_000),
    "many-commented-anchored-flow-nodes": (_many_commented_anchored_flow_nodes, 250, 1_000),
    "long-anchor-names": (_long_anchor_names, 100, 400),
    "many-quoted-hash-keys": (_many_quoted_hash_keys, 250, 1_000),
    "many-glued-flow-comments": (_many_glued_flow_comments, 250, 1_000),
    "long-flow-line": (_long_flow_line, 250, 1_000),
    "many-glued-hash-scalars": (_many_glued_hash_scalars, 250, 1_000),
    "many-block-scalars": (_many_block_scalars, 250, 1_000),
    "deep-divergent-flow": (_deep_divergent_flow, 250, 1_000),
    "many-documents-in-one-literal": (_many_documents_in_one_literal, 250, 1_000),
}


@pytest.mark.parametrize("name", list(GROWING))
def test_yaml_jwk_scanning_grows_linearly(name):
    build, low, high = GROWING[name]
    small, large = build(low), build(high)
    assert len(large) >= 3.5 * len(small)
    assert_cpu_growth(cold(redact), small, large, limit=8, label=name)


@pytest.mark.parametrize("name", list(GROWING))
def test_yaml_jwk_scanning_peak_follows_the_cost_model(name):
    build, low, high = GROWING[name]
    small, large = build(low), build(high)
    growth = peak_growth(
        cold(jwk.private_members),
        small,
        large,
        limit=8,
        budget=lambda found: 48 * len(large) + 400 * len(found),
    )
    assert growth.large < 48 * len(large) + 400 * len(growth.result)
    assert growth.ratio < 8
    if name in ("many-block-keys", "many-flow-keys"):
        assert len(growth.result) == high
    if name == "one-long-mapping":
        assert len(growth.result) == high
    if name in ("many-merges", "many-alias-keys"):
        assert len(growth.result) == high
    if name == "many-aliased-anchors-on-one-line":
        # Each alias token and the quoted node it names.
        assert len(growth.result) == 2 * high
    if name == "one-deep-mapping":
        # The outermost other-primes value covers every deeper level.
        assert len(growth.result) >= 1


@pytest.mark.parametrize("quote", ["'", '"""'])
def test_a_block_jwk_in_a_python_literal_with_escaped_breaks_masks_the_literal(quote):
    d = secret()
    body = f"jwk:\\n  {TYPE}: {RSA}\\n  n: pub\\n  d: {d}\\n"
    text = f"CONFIG = {quote}{body}{quote}\n"
    assert_key(body.encode().decode("unicode_escape"), RSA, {"d": d})
    out = protect(text, [d])
    assert out.startswith("CONFIG = ")


@pytest.mark.parametrize("brk", list(BREAKS))
def test_explicit_keys_are_read_as_members_and_types(brk):
    d, p, n = secret(), secret(), public("n")
    rows = [
        "jwk:",
        f"  ? {TYPE}",
        f"  : {RSA}",
        f"  n: {n}",
        "  ? d",
        f"  : {d}",
        "  ? 'p'  # quoted explicit key",
        f"  : {p}",
        f"  ? q{KEEP}",
        f"  : {KEEP}",
    ]
    text = BREAKS[brk].join(rows) + BREAKS[brk]
    assert_key(text, RSA, {"d": d, "p": p, "n": n})
    protect(text, [d, p], [n, KEEP])


# --- Review r1: merges, aliases, explicit keys, flow properties, templates -------------
#
# Each form is valid YAML that PyYAML loads as an RSA key holding the secret.
# Where full YAML resolution is out of reach the scanners fail toward masking:
# over-masking a document that uses these features is accepted, a leak is not.


def loads_key(text: str, secret_value: str) -> None:
    """PyYAML resolves ``text`` to a typed mapping that holds ``secret_value``."""
    found = [m for doc in yaml.safe_load_all(text) for m in typed(doc)]
    assert any(m[TYPE] in KINDS and secret_value in json.dumps(m, default=str) for m in found), (
        found
    )


MERGES = {
    "private-from-flow-anchor": "priv: &p {{d: {s}}}\njwk:\n  {t}: {k}\n  {m}: *p\n",
    "private-from-block-anchor": "priv: &p\n  d: {s}\njwk:\n  {t}: {k}\n  {m}: *p\n",
    "type-from-flow-anchor": "base: &b {{{t}: {k}}}\njwk:\n  {m}: *b\n  d: {s}\n",
    "type-from-block-anchor": "base: &b\n  {t}: {k}\njwk:\n  {m}: *b\n  d: {s}\n",
    "type-from-quoted-anchor": 'base: &b {{"{t}": "{k}"}}\njwk:\n  {m}: *b\n  d: {s}\n',
    "type-from-escaped-anchor": 'base: &b {{{t}: "R\\x53A"}}\njwk:\n  {m}: *b\n  d: {s}\n',
    "merge-list": "a: &a {{{t}: {k}}}\nb: &b {{n: pub}}\njwk:\n  {m}: [*a, *b]\n  d: {s}\n",
    "tagged-merge-key": "base: &b {{{t}: {k}}}\njwk:\n  !!merge {m}: *b\n  d: {s}\n",
    "flow-receiver": "base: &b {{{t}: {k}}}\njwk: {{{m}: *b, d: {s}}}\n",
    "sequence-item": "base: &b {{{t}: {k}}}\nkeys:\n  - {m}: *b\n    d: {s}\n",
    "private-in-merged-oth": "o: &o {{oth: [{{r: {s}}}]}}\njwk:\n  {t}: {k}\n  {m}: *o\n",
}


@pytest.mark.parametrize("brk", ["lf", "crlf"])
@pytest.mark.parametrize("form", list(MERGES))
def test_a_merge_key_cannot_carry_a_private_member_or_a_type_past_the_mask(form, brk):
    s = secret()
    text = MERGES[form].format(t=TYPE, k=RSA, s=s, m=MERGE).replace("\n", BREAKS[brk])
    loads_key(text, s)
    protect(text, [s])


def test_a_document_with_merges_but_no_key_type_is_unchanged():
    text = f"base: &b {{x: 1}}\nm:\n  {MERGE}: *b\n  d: {KEEP}\n  p: {KEEP}p\n"
    assert yaml.safe_load(text)["m"]["d"] == KEEP
    unchanged(text, [KEEP, KEEP + "p"])


def test_merge_masking_stays_inside_its_document():
    s = secret()
    text = f"base: &b {{{TYPE}: {RSA}}}\njwk:\n  {MERGE}: *b\n  d: {s}\n---\nother:\n  d: {KEEP}\n"
    loads_key(text, s)
    protect(text, [s])
    unchanged(text, [KEEP])


ANCHORED_VALUES = {
    "plain": ("value: &v {s}\n", "{s}"),
    "double-quoted": 'value: &v "{s}"\n',
    "single-quoted": "value: &v '{s}'\n",
    "literal": "value: &v |\n  {s}\n",
    "folded-multi-line": "value: &v >-\n  {s}\n  {s2}\n",
    "plain-multi-line": "value: &v {s}\n  {s2}\n",
    "sequence-item": "values:\n  - &v {s}\n",
    "inside-flow": "vals: {{a: &v {s}, b: c}}\n",
    "tagged": "value: !!str &v {s}\n",
}


@pytest.mark.parametrize("receiver", ["block", "flow"])
@pytest.mark.parametrize("form", list(ANCHORED_VALUES))
def test_an_aliased_private_value_is_masked_at_its_anchor(form, receiver):
    s, s2, n = secret(), secret(), public("n")
    anchored = ANCHORED_VALUES[form]
    if isinstance(anchored, tuple):
        anchored = anchored[0]
    head = anchored.format(s=s, s2=s2)
    if receiver == "block":
        text = head + f"jwk:\n  {TYPE}: {RSA}\n  n: {n}\n  d: *v\n"
    else:
        text = head + f"jwk: {{{TYPE}: {RSA}, n: {n}, d: *v}}\n"
    loads_key(text, s)
    fragments = [s, s2] if "{s2}" in anchored else [s]
    # In the one-line flow receiver the alias member shares n's line, which
    # the audit then rightly attributes; n itself must still be kept.
    out = protect(text, fragments, [n] if receiver == "block" else [])
    assert n in out


def test_an_aliased_other_primes_sequence_is_masked_at_its_anchor():
    r, d = secret(), secret()
    text = f"o: &o\n  - r: {r}\n    d: {d}\njwk:\n  {TYPE}: {RSA}\n  n: pub\n  oth: *o\n"
    loads_key(text, d)
    protect(text, [r, d])


def test_an_alias_of_a_public_value_stays_public():
    s, n = secret(), public("n")
    text = f"nval: &nv {n}\njwk:\n  {TYPE}: {RSA}\n  n: *nv\n  d: {s}\n"
    loads_key(text, s)
    protect(text, [s], [n])


def test_an_alias_value_outside_any_key_is_unchanged():
    text = f"value: &v {KEEP}\nother:\n  d: *v\n"
    assert yaml.safe_load(text)["other"]["d"] == KEEP
    unchanged(text, [KEEP])


ALIAS_KEYS = {
    "block": "name: &n d\njwk:\n  {t}: {k}\n  n: pub\n  {star}n : {s}\n",
    "block-explicit": "name: &n d\njwk:\n  {t}: {k}\n  ? {star}n\n  : {s}\n",
    "flow": "name: &n d\njwk: {{{t}: {k}, {star}n : {s}}}\n",
    "type-key": "t: &t {t}\njwk:\n  {star}t : {k}\n  d: {s}\n",
    "quoted-anchor": 'name: &n "d"\njwk:\n  {t}: {k}\n  {star}n : {s}\n',
}


@pytest.mark.parametrize("form", list(ALIAS_KEYS))
def test_an_alias_key_naming_a_private_member_is_masked(form):
    s = secret()
    text = ALIAS_KEYS[form].format(t=TYPE, k=RSA, s=s, star=STAR)
    loads_key(text, s)
    protect(text, [s])


EXPLICIT = {
    "tagged-name": "jwk:\n  {t}: {k}\n  ? !!str d\n  : {s}\n",
    "verbatim-tagged-type": "jwk:\n  ? !<tag:yaml.org,2002:str> {t}\n  : {k}\n  d: {s}\n",
    "anchored-name": "jwk:\n  {t}: {k}\n  ? &a d\n  : {s}\n",
    "name-on-next-line": "jwk:\n  {t}: {k}\n  ?\n    d\n  : {s}\n",
    "tag-then-name-below": "jwk:\n  {t}: {k}\n  ? !<tag:yaml.org,2002:str>\n    d\n  : {s}\n",
    "quoted-name-below": 'jwk:\n  {t}: {k}\n  ?\n    "d"\n  : {s}\n',
    "type-name-below": "jwk:\n  ?\n    {t}\n  : {k}\n  d: {s}\n",
}


@pytest.mark.parametrize("brk", list(BREAKS))
@pytest.mark.parametrize("form", list(EXPLICIT))
def test_an_explicit_key_is_read_as_a_node(form, brk):
    s = secret()
    text = EXPLICIT[form].format(t=TYPE, k=RSA, s=s).replace("\n", BREAKS[brk])
    loads_key(text, s)
    protect(text, [s])


BLOCK_PROPERTIES = {
    "tagged-type": "jwk:\n  {t}: !!str {k}\n  n: pub\n  d: {s}\n",
    "anchored-type": "jwk:\n  {t}: &a {k}\n  n: pub\n  d: {s}\n",
    "verbatim-type": "jwk:\n  {t}: !<tag:yaml.org,2002:str> {k}\n  n: pub\n  d: {s}\n",
    "verbatim-name": "jwk:\n  {t}: {k}\n  n: pub\n  !<tag:yaml.org,2002:str> d: {s}\n",
    "anchored-name": "jwk:\n  {t}: {k}\n  n: pub\n  &x d: {s}\n",
}


@pytest.mark.parametrize("form", list(BLOCK_PROPERTIES))
def test_node_properties_on_block_names_and_types_are_skipped(form):
    s = secret()
    text = BLOCK_PROPERTIES[form].format(t=TYPE, k=RSA, s=s)
    loads_key(text, s)
    out = protect(text, [s])
    assert "n: pub" in out


FLOW_TYPES = {
    "tagged-quoted": '{{{t}: !!str "{k}", n: pub, d: {s}}}',
    "anchored-quoted": '{{{t}: &a "{k}", n: pub, d: {s}}}',
    "verbatim-tag": "{{{t}: !<tag:yaml.org,2002:str> {k}, n: pub, d: {s}}}",
    "verbatim-tag-quoted": "{{{t}: !<tag:yaml.org,2002:str> '{k}', n: pub, d: {s}}}",
    "properties-then-break": "{{{t}: !!str\n  {k}, n: pub, d: {s}}}",
    "comment-then-break": "{{{t}: # the type follows\n  {k}, n: pub, d: {s}}}",
    "comment-then-quoted": '{{{t}: # the type follows\n  "{k}", n: pub, d: {s}}}',
    "alias-type": "t: &t {k}\nk: {{{t}: *t, n: pub, d: {s}}}",
}


@pytest.mark.parametrize("form", list(FLOW_TYPES))
@pytest.mark.parametrize("kind", KINDS)
def test_a_flow_type_after_properties_comments_or_breaks_is_read(form, kind):
    s = secret()
    text = FLOW_TYPES[form].format(t=TYPE, k=kind, s=s)
    loads_key(text, s)
    out = protect(text, [s])
    if form != "alias-type":
        assert "n: pub" in out


def test_a_tagged_quoted_flow_type_is_decided_like_a_quoted_type():
    s = secret()
    text = f'{{{TYPE}: !!str "{RSA}", n: pub, p: {s}, k: {KEEP}}}'
    loads_key(text, s)
    out = protect(text, [s], [KEEP])
    assert "n: pub" in out


def cooked(body: str) -> str:
    """The fixture's template literal as JavaScript reads it, interpolations opaque."""
    return body.replace("${name}", "x").encode().decode("unicode_escape")


TEMPLATES = {
    "escaped-breaks": "jwk:\\n  {t}: {k}\\n  n: pub\\n  d: {s}\\n",
    "interpolated": "jwk:\\n  {t}: {k}\\n  kid: ${{name}}\\n  d: {s}\\n",
    "escaped-type": "jwk:\\n  k\\x74y: {k}\\n  d: {s}\\n",
    "crlf-escapes": "jwk:\\r\\n  {t}: {k}\\r\\n  d: {s}\\r\\n",
}


@pytest.mark.parametrize("form", list(TEMPLATES))
def test_a_block_jwk_in_a_javascript_template_literal_masks_the_literal(form):
    s = secret()
    body = TEMPLATES[form].format(t=TYPE, k=RSA, s=s)
    loads_key(cooked(body), s)
    text = f"const config = `{body}`;\nconst other = 1;\n"
    out = protect(text, [s])
    assert out.startswith("const config = ") and out.endswith("const other = 1;\n")


# --- Review r2: fake anchors, flow collections, duplicate types, interpolation --------


FAKE_ANCHORS = {
    "comment": ("value: &v {s}\n# fake &v\njwk:\n  {t}: {k}\n  d: *v\n", "value"),
    "comment-after-key": ("value: &v {s}\njwk:\n  {t}: {k}  # see &v\n  d: *v\n", "value"),
    "plain-text": ("value: &v {s}\nnote: a fake &v here\njwk:\n  {t}: {k}\n  d: *v\n", "value"),
    "quoted": ('value: &v {s}\nq: "a, &v x"\njwk:\n  {t}: {k}\n  d: *v\n', "value"),
    "block-scalar": (
        "value: &v {s}\ntext: |\n  &v not an anchor\njwk:\n  {t}: {k}\n  d: *v\n",
        "value",
    ),
    "alias-key-plain-text": (
        "name: &n d\njwk:\n  {t}: {k}\n  note: fake &n x\n  {star}n : {s}\n",
        "key",
    ),
    "alias-key-comment": ("name: &n d\n# &n x\njwk:\n  {t}: {k}\n  {star}n : {s}\n", "key"),
}


@pytest.mark.parametrize("form", list(FAKE_ANCHORS))
def test_a_fake_anchor_cannot_steal_the_real_one(form):
    s = secret()
    template, _ = FAKE_ANCHORS[form]
    text = template.format(t=TYPE, k=RSA, s=s, star=STAR)
    loads_key(text, s)
    protect(text, [s])


ANCHORED_COLLECTIONS = {
    "flow-sequence": "value: &v [{s}, {s2}]\n",
    "flow-mapping": "value: &v {{a: {s}, b: {s2}}}\n",
    "multi-line-sequence": "value: &v [\n  {s},\n  {s2}\n]\n",
    "nested": "value: &v [{{a: {s}}}, [{s2}, x]]\n",
    "quoted-commas": "value: &v ['{s}, x', \"{s2}, ]\"]\n",
}


@pytest.mark.parametrize("member", ["d", "oth"])
@pytest.mark.parametrize("form", list(ANCHORED_COLLECTIONS))
def test_an_aliased_flow_collection_is_masked_to_its_closing_bracket(form, member):
    s, s2 = secret(), secret()
    text = ANCHORED_COLLECTIONS[form].format(s=s, s2=s2)
    text += f"jwk:\n  {TYPE}: {RSA}\n  n: pub\n  {member}: *v\n"
    loads_key(text, s)
    loads_key(text, s2)
    out = protect(text, [s, s2])
    assert "n: pub" in out


DUPLICATE_TYPES = {
    "quoted-none-then-plain": ('{{{t}: "none", {t}: {k}, n: pub, {m}: {s}}}', True),
    "quoted-ec-then-plain-rsa": ('{{{t}: "EC", {t}: {k}, n: pub, {m}: {s}}}', True),
    "plain-then-quoted-none": ('{{{t}: {k}, {t}: "none", n: pub, {m}: {s}}}', False),
    "quoted-then-quoted": ('{{"{t}": "EC", "{t}": "{k}", "n": "pub", "{m}": "{s}"}}', True),
    "plain-ec-then-plain-rsa": ("{{{t}: EC, {t}: {k}, n: pub, {m}: {s}}}", True),
    "block": ("jwk:\n  {t}: none\n  {t}: {k}\n  n: pub\n  {m}: {s}\n", True),
}


@pytest.mark.parametrize("member", ["d", "p", "qi"])
@pytest.mark.parametrize("form", list(DUPLICATE_TYPES))
def test_a_duplicate_type_masks_the_union_of_private_sets(form, member):
    s = secret()
    template, yaml_key = DUPLICATE_TYPES[form]
    text = template.format(t=TYPE, k=RSA, s=s, m=member)
    loaded = typed(yaml.safe_load(text))
    if yaml_key:  # PyYAML keeps the last type: an RSA key
        assert any(m[TYPE] == RSA and m[member] == s for m in loaded), loaded
    else:  # PyYAML reads the last type, "none": masking it is accepted over-masking
        assert any(m[TYPE] == "none" and m[member] == s for m in loaded), loaded
    out = protect(text, [s])
    assert "pub" in out


def test_a_conflicting_type_proves_no_member_public():
    k = secret()
    # oct then RSA: k is private in one reading, so it must not be proved public.
    text = f'{{"{TYPE}": "{OCT}", "{TYPE}": "{RSA}", "n": "pub", "k": "{k}"}}'
    out = protect(text, [k])
    assert '"n": "pub"' in out


def cooked_interpolated(body: str, value: str) -> str:
    return body.replace("${kind}", value).replace("${part}", "t").encode().decode("unicode_escape")


TEMPLATE_INTERPOLATIONS = {
    "interpolated-type": ("jwk:\\n  {t}: ${{kind}}\\n  n: pub\\n  d: {s}\\n", RSA),
    "interpolated-inside-type": ("jwk:\\n  {t}: R${{kind}}A\\n  d: {s}\\n", "S"),
    "interpolated-type-name": ("jwk:\\n  k${{part}}y: {k}\\n  d: {s}\\n", None),
    "interpolated-whole-name": ("jwk:\\n  ${{kind}}: {k}\\n  d: {s}\\n", TYPE),
    "real-breaks": ("jwk:\n  {t}: ${{kind}}\n  n: pub\n  d: {s}\n", RSA),
    "real-breaks-name": ("jwk:\n  k${{part}}y: {k}\n  d: {s}\n", None),
    "flow-interpolated-type": ("{{{t}: ${{kind}}, n: pub, d: {s}}}", RSA),
    "flow-interpolated-name": ("{{k${{part}}y: {k}, n: pub, d: {s}}}", None),
}


@pytest.mark.parametrize("form", list(TEMPLATE_INTERPOLATIONS))
def test_a_template_interpolation_is_unknown_and_fails_toward_masking(form):
    s = secret()
    template, value = TEMPLATE_INTERPOLATIONS[form]
    body = template.format(t=TYPE, k=RSA, s=s)
    loads_key(cooked_interpolated(body, value or ""), s)
    text = f"const config = `{body}`;\n"
    protect(text, [s])


def test_an_interpolated_template_without_private_names_is_unchanged():
    text = "const s = `jwk:\\n  name: ${who}\\n  n: " + KEEP + "\\n`;\n"
    assert redact(text) == text


def test_a_tag_directive_applies_to_its_own_document_only():
    s = secret()
    directive = "%TAG !e! tag:example.com,2000:\n"
    first = "plain: 1\n"
    keyed = f"jwk:\n  {TYPE}: {RSA}\n  n: pub\n  d: {s}\nother:\n  d: {KEEP}\n"
    text = f"{directive}---\n{first}...\n---\n{keyed}"
    docs = list(yaml.safe_load_all(text))
    assert docs[1]["other"] == {"d": KEEP} and docs[1]["jwk"]["d"] == s
    # The directive belongs to the first document only: the second, with no
    # merge or alias key, keeps its unrelated d.
    protect(text, [s])
    unchanged(text, [KEEP])


# --- Ordinary code is never read as YAML merges or aliases (CI gate, r2) --------------


def code_fixtures() -> dict[str, str]:
    """Source files mixing key-type strings and YAML fixtures with code syntax."""
    yaml_fixture = f"base: &b {{{TYPE}: {RSA}}}\\njwk:\\n  {MERGE}: *b\\n  n: pub\\n"
    python = (
        "import pytest\n\n"
        f'FIXTURES = {{"merged": "{yaml_fixture}", "alias": "name: &n x\\n{STAR}n : v"}}\n'
        f'PUBLIC = {{"{TYPE}": "{RSA}", "n": "pub", "e": "AQAB"}}\n\n\n'
        "def refused(*args, **kwargs):\n"
        '    pytest.fail("pure tests must not access a database")\n'
        '    for name in ("init_db", "connect"):\n'
        "        print(name, *args, **kwargs)\n\n\n"
        "class Key:\n"
        "    p: str = 'prime'\n"
        "    q: int = 1\n"
        "    d: bytes = b''\n\n\n"
        f'ROW = {{"tags": ["k=v", fmt(x), {{"k": "{KEEP}", "q": 1}}], "kind": "{RSA}"}}\n'
        "result = call(a, *rest, key=b, **options)\n"
    )
    javascript = (
        f'const base = {{...defaults, {TYPE}: "{RSA}", n: pub}};\n'
        "const merged = {...base, ...overrides};\n"
        "fn(...args, ...more);\n"
        "const {p, q, ...rest} = config;\n"
        f'const fixture = "jwk:\\n  {MERGE}: *b\\n  d: x";\n'
        f"const keep = {{q: '{KEEP}', d: other}};\n"
    )
    shell = (
        "#!/bin/sh\n"
        f'grep -r \'"{TYPE}": "{RSA}"\' *.json\n'
        'for f in *.yaml **/*.yml; do echo "$f"; done\n'
        "rm -rf ./build/* && cp -r src/* dist/\n"
        'case "$1" in *) d="$2" ;; esac\n'
    )
    c_source = (
        f"/* {TYPE}: {RSA} keys are parsed elsewhere */\n"
        "int *p = &storage;\n"
        "char **argv;\n"
        "struct key *k = &key;\n"
        "q = flag ? *p : *r;\n"
        "*d = *q;\n"
        "default: *p = 0;\n"
    )
    return {"python": python, "javascript": javascript, "shell": shell, "c": c_source}


@pytest.mark.parametrize("language", list(code_fixtures()))
def test_ordinary_code_triggers_no_yaml_merge_alias_or_conservative_rule(language):
    text = code_fixtures()[language]
    assert redact(text) == text
    assert jwk.private_members(text) == []
    assert _jwk_findings(text) == []
    assert not [f for f in check_secrets(f"fixture.{language}", text) if f.rule == "SECRETS-KEY"]


def test_a_plain_dict_in_a_merge_using_document_is_not_masked():
    # A real merge makes the document conservative, but only mappings that
    # take part in merging or aliasing are read that way: a plain mapping's q
    # stays, the receiving mapping's d is masked.
    s = secret()
    text = (
        f"base: &b {{{TYPE}: {RSA}}}\njwk:\n  {MERGE}: *b\n  d: {s}\n"
        f"plain:\n  q: {KEEP}\nrow: {{k: x, q: {KEEP}flow}}\n"
    )
    loads_key(text, s)
    protect(text, [s])
    unchanged(text, [KEEP, KEEP + "flow"])


# --- Review r3: merge forms, anchored items, interpolated private keys ----------------

BASE = "b: &b {{{t}: {k}}}\n"
MERGE_FORMS = {
    "explicit-merge-value-below": "jwk:\n  ? {m}\n  : *b\n  d: {s}\n",
    "explicit-merge-in-sequence-item": "keys:\n  - ? {m}\n    : *b\n    d: {s}\n",
    "flow-explicit-merge": "jwk: {{? {m}: *b, d: {s}}}\n",
    "flow-explicit-merge-spaced": "jwk: {{? {m} : *b, d: {s}}}\n",
    "merge-after-explicit-value": "? k\n: {m}: *b\n  d: {s}\n",
    "tagged-double-quoted-merge": 'jwk:\n  !!merge "{m}": *b\n  d: {s}\n',
    "tagged-single-quoted-merge": "jwk:\n  !!merge '{m}': *b\n  d: {s}\n",
    "anchored-tagged-merge": "jwk:\n  &m !!merge {m}: *b\n  d: {s}\n",
    "explicit-tagged-merge": "jwk:\n  ? !!merge {m}\n  : *b\n  d: {s}\n",
    "verbatim-tagged-merge": "jwk:\n  !<tag:yaml.org,2002:merge> {m}: *b\n  d: {s}\n",
}


@pytest.mark.parametrize("form", list(MERGE_FORMS))
def test_every_yaml_merge_form_is_merge_evidence(form):
    s = secret()
    text = (BASE + MERGE_FORMS[form]).format(t=TYPE, k=RSA, s=s, m=MERGE)
    loads_key(text, s)
    protect(text, [s])


@pytest.mark.parametrize("quote", ['"', "'"])
def test_a_quoted_merge_key_is_merge_evidence(quote):
    # libyaml and Psych merge on a quoted '<<' too; PyYAML keeps it as a key.
    s = secret()
    text = (BASE + "jwk:\n  {q}{m}{q}: *b\n  d: {s}\n").format(t=TYPE, k=RSA, s=s, m=MERGE, q=quote)
    assert yaml.safe_load(text)["jwk"]["d"] == s
    protect(text, [s])


ANCHORED_ITEMS = {
    "sequence-item": "pieces:\n  - &a\n    d: {s}\n",
    "standalone-properties-line": "priv:\n  &a\n  d: {s}\n",
    "deeper-sequence-item": "x:\n  y:\n    - &a\n      d: {s}\n",
    "tagged-sequence-item": "pieces:\n  - &a !!map\n    d: {s}\n",
}


@pytest.mark.parametrize("form", list(ANCHORED_ITEMS))
def test_an_anchored_item_mapping_merged_into_a_key_is_masked(form):
    s = secret()
    text = (ANCHORED_ITEMS[form] + "typed:\n  {t}: {k}\n  {m}: *a\n").format(
        t=TYPE, k=RSA, s=s, m=MERGE
    )
    loads_key(text, s)
    protect(text, [s])


def test_an_inline_anchored_item_key_is_read_conservatively():
    # '- &a d: X' anchors the key scalar (PyYAML refuses the merge); a parser
    # that anchors the item mapping would merge d: masked, failing toward it.
    s = secret()
    text = f"pieces:\n  - &a d: {s}\ntyped:\n  {TYPE}: {RSA}\n  {MERGE}: *a\n"
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(text)
    protect(text, [s])


INTERPOLATED_PRIVATE_KEYS = {
    "json-interpolated-key": '{{"{t}":"{k}","${{n}}":"{s}"}}',
    "flow-interpolated-key": "{{{t}: {k}, ${{n}}: {s}}}",
    "compact-json-interpolated-type-name": '{{"k${{p}}ty":"{k}","d":"{s}"}}',
    "compact-json-interpolated-type": '{{"{t}":"${{v}}","d":"{s}"}}',
    "block-interpolated-key": "jwk:\\n  {t}: {k}\\n  ${{n}}: {s}\\n",
}


@pytest.mark.parametrize("form", list(INTERPOLATED_PRIVATE_KEYS))
def test_an_interpolated_private_key_masks_the_template(form):
    s = secret()
    body = INTERPOLATED_PRIVATE_KEYS[form].format(t=TYPE, k=RSA, s=s)
    text = f"const config = `{body}`;\n"
    protect(text, [s])


def test_an_interpolated_key_without_a_key_type_is_unchanged():
    text = 'const s = `{"name":"x","${field}":"' + KEEP + '"}`;\n'
    assert redact(text) == text


def _snippet_schema(snippet_value: object, **extra: object) -> str:
    """A vendored editor JSON Schema (#2222): `${1:3000}` snippet keys, and
    properties named like JWK members that describe a string, not hold one."""
    props = {name: {"type": "string", "description": KEEP} for name in [*PRIVATE[RSA], "k"]}
    body = {"defaultSnippets": [{"body": {"${1:3000}": snippet_value}}]}
    return json.dumps({"type": "object", "properties": props, "ports": body, **extra}, indent=2)


def test_a_snippet_key_with_a_mapping_value_is_not_a_private_member():
    text = _snippet_schema({"label": "${2:Application}", "onAutoForward": "notify"})
    assert (jwk.private_members(text), redact(text), jwk_lines(text)) == ([], text, set())


def test_a_schema_with_snippet_keys_still_reports_a_private_scalar():
    s = secret()  # a lone snippet key holding a string is not (test_redaction_jwk.py)
    value = {TYPE: RSA, "n": public("n"), "d": s}
    protect(_snippet_schema({"label": "x"}, keys={"${1:key}": value}), [s])


# --- Review r4: glued flow anchors and merges, the flow-type crash -------------------

TYPED_MERGE_OF_A = "typed:\n  {t}: {k}\n  {m}: *a\n"
GLUED_FLOW_ANCHORS = {
    "anchor-after-opening-bracket": "pieces: [&a {{d: {s}}}]\n",
    "anchor-after-comma": "pieces: [x,&a {{d: {s}}}]\n",
    "anchor-then-tag": "pieces: [&a !!map {{d: {s}}}]\n",
    "anchor-ending-the-line": "pieces: [&a\n  {{d: {s}}}]\n",
    "anchor-on-the-line-before": "pieces:\n  &a\n  {{d: {s}}}\n",
}


@pytest.mark.parametrize("form", list(GLUED_FLOW_ANCHORS))
def test_an_anchored_flow_node_merged_into_a_key_is_masked(form):
    s = secret()
    text = (GLUED_FLOW_ANCHORS[form] + TYPED_MERGE_OF_A).format(t=TYPE, k=RSA, s=s, m=MERGE)
    loads_key(text, s)
    protect(text, [s])


GLUED_FLOW_MERGES = {
    "plain-before-sequence": ("{m}:[*b]", True),
    "plain-before-mapping": ("{m}:{{{t}: {k}}}", True),
    "double-quoted-before-alias": ('"{m}":*b', False),
    "single-quoted-before-alias": ("'{m}':*b", False),
}


@pytest.mark.parametrize("form", list(GLUED_FLOW_MERGES))
def test_a_glued_colon_flow_merge_is_merge_evidence(form):
    s = secret()
    merge, pyyaml_merges = GLUED_FLOW_MERGES[form]
    text = (BASE + "jwk: {{" + merge + ", d: {s}}}\n").format(t=TYPE, k=RSA, s=s, m=MERGE)
    if pyyaml_merges:
        loads_key(text, s)
    else:  # libyaml and Psych merge on a quoted '<<'; PyYAML keeps it as a key
        assert yaml.safe_load(text)["jwk"]["d"] == s
    protect(text, [s])


def test_a_single_pair_merge_in_a_flow_sequence_is_masked_at_its_source():
    # [<<: *b] is a sequence holding one single-pair mapping, {<<: *b}: nothing
    # can stand beside the merge in that mapping, so the merged values are only
    # those of the anchored node, which is masked where it is defined.
    s = secret()
    text = f"b: &b {{{TYPE}: {RSA}, n: pub, d: {s}}}\nx: [{MERGE}: *b]\n"
    assert yaml.safe_load(text)["x"] == [{TYPE: RSA, "n": "pub", "d": s}]
    out = protect(text, [s])
    assert "n: pub" in out


@pytest.mark.parametrize(
    "text", ['{kty:! "kty', '{kty:! "kty"', '{kty:&a "kty', '{kty:! ! "kty', '{kty:!!str "RSA"']
)
def test_a_quoted_flow_type_after_properties_never_raises(text):
    text = text.replace("kty", TYPE)
    jwk.private_members(text)
    redact(text)
    _jwk_findings(text)
    list(check_secrets("fixture.yaml", text))


def test_an_unexpected_scan_error_fails_closed(monkeypatch):
    # Production masks the whole text; the test suite runs strict and raises.
    def broken(text):
        raise RuntimeError("scanner bug")

    monkeypatch.setattr(jwk, "_private_members", broken)
    with pytest.raises(RuntimeError):
        jwk.private_members("anything")
    monkeypatch.delenv(jwk.STRICT_ENV, raising=False)
    text = f"jwk:\n  {TYPE}: {RSA}\n  d: whatever\n"
    assert [(m.name, m.start, m.end) for m in jwk.private_members(text)] == [
        ("unparsed", 0, len(text))
    ]
    assert redact(text) == '"[REDACTED_SECRET]"'
    assert [f.action for f in _jwk_findings(text)] == [BLOCK]
    assert jwk_lines(text) == {1, 2, 3}


def test_a_document_marker_clears_a_pending_anchor():
    text = f"x:\n  &a\n---\nplain:\n  d: {KEEP}\n"
    unchanged(text, [KEEP])


# --- Review r5: comments in the property gap, long property tokens -------------------

COMMENTED_GAPS = {
    "comment-after-flow-anchor": "pieces: [&a # note\n  {{d: {s}}}]\n",
    "comment-after-lone-anchor": "x:\n  &a # note\n  {{d: {s}}}\n",
    "comment-line-after-lone-anchor": "x:\n  &a\n  # note\n  {{d: {s}}}\n",
    "comment-after-anchor-and-tag": "pieces: [&a !!map # note\n  {{d: {s}}}]\n",
    "comment-after-item-anchor": "pieces:\n  - &a # note\n    {{d: {s}}}\n",
    "comment-lines-after-item-anchor": "pieces:\n  - &a\n    # one\n    # two\n    {{d: {s}}}\n",
}


@pytest.mark.parametrize("form", list(COMMENTED_GAPS))
def test_a_comment_between_an_anchor_and_its_flow_node_keeps_the_anchor(form):
    s = secret()
    text = (COMMENTED_GAPS[form] + TYPED_MERGE_OF_A).format(t=TYPE, k=RSA, s=s, m=MERGE)
    loads_key(text, s)
    protect(text, [s])


@pytest.mark.parametrize("merge", ["{m}:[*b]", "{m}: *b", '"{m}":*b'])
def test_a_comment_before_a_flow_merge_keeps_the_merge(merge):
    s = secret()
    text = (BASE + "jwk: {{a: 1, # c\n  " + merge + ", d: {s}}}\n").format(
        t=TYPE, k=RSA, s=s, m=MERGE
    )
    if '"' not in merge:
        loads_key(text, s)
    protect(text, [s])


LONG = "a" * 300


@pytest.mark.parametrize(
    "node",
    [
        "pieces: [&{n} {{d: {s}}}]\n",
        "pieces: [&{n} !!map {{d: {s}}}]\n",
        "pieces:\n  - &{n}\n    d: {s}\n",
        "pieces: &{n} {{d: {s}}}\n",
    ],
)
def test_a_long_anchor_name_still_marks_its_node(node):
    s = secret()
    text = (node + "typed:\n  {t}: {k}\n  {m}: *{n}\n").format(t=TYPE, k=RSA, s=s, m=MERGE, n=LONG)
    loads_key(text, s)
    protect(text, [s])


def test_a_long_property_before_a_flow_merge_keeps_the_merge():
    s = secret()
    text = (BASE + "jwk: {{&{n} {m}: *b, d: {s}}}\n").format(t=TYPE, k=RSA, s=s, m=MERGE, n=LONG)
    loads_key(text, s)
    protect(text, [s])


# --- Review r6: YAML's comment rule, quoted '#' and glued flow comments --------------

QUOTED_HASH_LINES = {
    "double-quoted-key": '"k # x": &a\n  {{d: {s}}}\n',
    "single-quoted-key": "'k # x': &a\n  {{d: {s}}}\n",
    "flow-mapping-key": 'x: {{"k # x": &a\n  {{d: {s}}}}}\n',
    "flow-sequence-item": 'x: ["k # x", &a\n  {{d: {s}}}]\n',
    "tab-before-hash": '"k\t#x": &a\n  {{d: {s}}}\n',
    "quoted-hash-then-comment": '"k # x": &a # real\n  {{d: {s}}}\n',
}


@pytest.mark.parametrize("form", list(QUOTED_HASH_LINES))
def test_a_hash_inside_a_quoted_scalar_is_not_a_comment(form):
    s = secret()
    text = (QUOTED_HASH_LINES[form] + TYPED_MERGE_OF_A).format(t=TYPE, k=RSA, s=s, m=MERGE)
    loads_key(text, s)
    protect(text, [s])


GLUED_FLOW_COMMENTS = {
    "after-comma": BASE + "jwk: {{a: 1,#c\n  {m}: *b, d: {s}}}\n",
    "after-opening-brace": BASE + "jwk: {{#c\n  {m}: *b, d: {s}}}\n",
    "in-a-sequence-item": BASE + "jwk: [{{#c\n  {m}: *b, d: {s}}}]\n",
    "after-closing-bracket": BASE + "jwk: {{a: [1]#c\n  , {m}: *b, d: {s}}}\n",
    "after-a-quoted-value": BASE + 'jwk: {{a: "q"#c\n  , {m}: *b, d: {s}}}\n',
    "glued-before-anchored-node": "pieces: [#c\n  &a {{d: {s}}}]\n" + TYPED_MERGE_OF_A,
}


@pytest.mark.parametrize("form", list(GLUED_FLOW_COMMENTS))
def test_a_comment_glued_to_a_flow_indicator_is_a_comment(form):
    s = secret()
    text = GLUED_FLOW_COMMENTS[form].format(t=TYPE, k=RSA, s=s, m=MERGE)
    loads_key(text, s)
    protect(text, [s])


def test_a_block_plain_value_with_a_glued_hash_is_one_scalar():
    # In block context 'a,#c' is one plain scalar: no comment, nothing cut.
    s = secret()
    text = f"jwk:\n  {TYPE}: {RSA}\n  d: {s},#c\n  n: pub\n"
    assert yaml.safe_load(text)["jwk"]["d"] == s + ",#c"
    out = protect(text, [s + ",#c"])
    assert "n: pub" in out


def test_a_quoted_key_holding_a_hash_does_not_cut_its_plain_value():
    s = secret()
    text = f'jwk:\n  {TYPE}: {RSA}\n  "n # x": pub\n  d: {s}\n'
    assert yaml.safe_load(text)["jwk"]["n # x"] == "pub"
    protect(text, [s])


# --- Review r7: one YAML comment lexer for every comment decision --------------------


class AnyTagLoader(yaml.SafeLoader):
    """PyYAML's safe loader, building nodes with unknown tags (!t'x) by kind."""


def _construct_any(loader, suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


AnyTagLoader.add_multi_constructor("!", _construct_any)


def loads_key_any_tag(text: str, secret_value: str) -> None:
    found = [m for doc in yaml.load_all(text, Loader=AnyTagLoader) for m in typed(doc)]
    assert any(m[TYPE] in KINDS and secret_value in json.dumps(m, default=str) for m in found)


PLAIN_QUOTE_HASH_MERGES = {
    "double-quote-then-hash": 'x"#c',
    "single-quote-then-hash": "x'#c",
    "quote-word-hash": 'x"y#c',
    "hash-only": "x#c",
}


@pytest.mark.parametrize("brk", list(BREAKS))
@pytest.mark.parametrize("value", list(PLAIN_QUOTE_HASH_MERGES))
def test_a_quote_or_hash_inside_a_flow_plain_scalar_keeps_the_merge(value, brk):
    s = secret()
    plain = PLAIN_QUOTE_HASH_MERGES[value]
    text = (BASE + "jwk: {{a: {v}, {m}: *b, d: {s}}}\n").format(
        t=TYPE, k=RSA, s=s, m=MERGE, v=plain
    )
    text = text.replace("\n", BREAKS[brk])
    loads_key(text, s)
    protect(text, [s])


GLUED_AFTER_QUOTED_KEY = {
    "single-quoted": "  ? 'd'#c\n  : {s}\n",
    "double-quoted": '  ? "d"#c\n  : {s}\n',
    "name-below": "  ?\n    'd'#c\n  : {s}\n",
    "anchored": "  ? &x 'd'#c\n  : {s}\n",
    "tagged": "  ? !!str 'd'#c\n  : {s}\n",
}


@pytest.mark.parametrize("brk", list(BREAKS))
@pytest.mark.parametrize("form", list(GLUED_AFTER_QUOTED_KEY))
def test_a_comment_glued_to_a_closed_quote_ends_the_key(form, brk):
    s = secret()
    text = ("jwk:\n  {t}: {k}\n" + GLUED_AFTER_QUOTED_KEY[form]).format(t=TYPE, k=RSA, s=s)
    text = text.replace("\n", BREAKS[brk])
    loads_key(text, s)
    protect(text, [s])


TAG_APOSTROPHES = {
    "sequence-item": "pieces:\n  - &a !t'x # note\n    {{d: {s}}}\n",
    "flow-item": "pieces: [&a !t'x # note\n  {{d: {s}}}]\n",
    "lone-line": "x:\n  &a !t'x # note\n  {{d: {s}}}\n",
    "two-apostrophes": "pieces:\n  - &a !t'x'y # note\n    {{d: {s}}}\n",
}


@pytest.mark.parametrize("form", list(TAG_APOSTROPHES))
def test_an_apostrophe_in_a_tag_is_not_a_quote(form):
    s = secret()
    text = (TAG_APOSTROPHES[form] + TYPED_MERGE_OF_A).format(t=TYPE, k=RSA, s=s, m=MERGE)
    loads_key_any_tag(text, s)
    protect(text, [s])


OBJECT_SCAN_DIVERGENCES = {
    "plain-type": 'jwk: {{{t}: {k}, a: x"#c, n: pub, d: {s}}}\n',
    "single-quote-hash": "jwk: {{{t}: {k}, a: x'#c, n: pub, d: {s}}}\n",
    "quote-in-plain": 'jwk: {{{t}: {k}, a: x"y, n: pub, d: {s}}}\n',
    "decided-object": 'jwk: {{"{t}": "{k}", a: x"#c, n: pub, d: {s}}}\n',
    "ec-key": 'jwk: {{{t}: EC, a: x"#c, crv: P-256, d: {s}}}\n',
    "multi-line": 'jwk: {{\n  {t}: {k},\n  a: x"#c,\n  n: pub,\n  d: {s}\n}}\n',
}


@pytest.mark.parametrize("form", list(OBJECT_SCAN_DIVERGENCES))
def test_a_yaml_flow_mapping_the_object_scan_misreads_is_still_masked(form):
    s = secret()
    text = OBJECT_SCAN_DIVERGENCES[form].format(t=TYPE, k=RSA, s=s)
    loads_key(text, s)
    out = protect(text, [s])
    assert "pub" in out or "P-256" in out


def test_a_yaml_flow_mapping_without_a_type_is_unchanged():
    text = f'jwk: {{a: x"#c, d: {KEEP}, p: {KEEP}p}}\n'
    assert yaml.safe_load(text)["jwk"]["d"] == KEEP
    unchanged(text, [KEEP, KEEP + "p"])


def test_a_python_comment_after_a_dict_is_still_a_comment():
    text = f'row = {{"name": "x", "n": 1}}  # d: {KEEP}\nother = 2\n'
    assert redact(text) == text


def test_the_yaml_lexer_runs_once_per_text(monkeypatch):
    built: list[int] = []
    original = yaml_jwk._YamlLex.__init__

    def counting(self, text):
        built.append(id(text))
        original(self, text)

    monkeypatch.setattr(yaml_jwk._YamlLex, "__init__", counting)
    monkeypatch.setattr(yaml_jwk, "_LEX_CACHE", None)
    text = _long_flow_line(50) + "\n" + _many_block_scalars(20)
    jwk.private_members(text)
    assert built.count(id(text)) == 1


@pytest.mark.parametrize("form", list(OBJECT_SCAN_DIVERGENCES))
def test_a_secret_spelling_the_type_cannot_hide_a_yaml_member(form):
    # The object scan reads the glued '#...' as a Python comment; a secret
    # holding 'KtY' made that comment a type-bearing region, which once cut the
    # YAML reading's member out (found by the differential, review r7).
    s = secret() + "KtY" + secret()
    text = OBJECT_SCAN_DIVERGENCES[form].format(t=TYPE, k=RSA, s=s)
    loads_key(text, s)
    protect(text, [s])


@pytest.mark.parametrize("value", list(PLAIN_QUOTE_HASH_MERGES))
def test_a_secret_spelling_the_type_cannot_hide_a_merged_member(value):
    s = secret() + "kTy"
    text = (BASE + "jwk: {{a: {v}, {m}: *b, n: pub, d: {s}}}\n").format(
        t=TYPE, k=RSA, s=s, m=MERGE, v=PLAIN_QUOTE_HASH_MERGES[value]
    )
    loads_key(text, s)
    protect(text, [s])


def test_merge_and_alias_evidence_inside_code_strings_is_not_yaml():
    # A merge tag in an escaped one-line string, an alias key inside a quoted
    # or plain scalar, and structure after a flow collection that never closes
    # (code read as YAML) make no document conservative.
    tag = "!!" + "merge"
    text = (
        f'X = "jwk:\\n  {tag} {MERGE}: *b\\n"\n'
        'Y = "q = flag ? *p : *r;"\n'
        f"Z = f'{{{{#c\\n'\n"
        f"b = &p 1\nrow = {{n: {{p: {KEEP}}}, {STAR}p : 2}}\n"
        f'PUBLIC = {{"{TYPE}": "{RSA}", "n": "pub"}}\nkeep = {{"q": "{KEEP}q"}}\n'
    )
    unchanged(text, [KEEP, KEEP + "q"])


def test_code_inside_a_flow_yaml_never_closes_is_not_swept():
    # Python source: YAML opens a flow at '["/,"' and its quote pairing never
    # closes it, so the braces inside are a runaway reading. A private name in
    # a code string there is not a member.
    text = (
        '    "comment", ["/," + "kt" + "y:\'oct\',", "{note:typeof /#/," + "kt" + "y:\'oct\'"]\n'
        f"    text = \"{{'{TYPE}':'{RSA}','k':\" + repr(public) + \", 'n':\" + operand"
        ' + " / # " + comment + "\\n 2}"\n'
        f'    text = "({{n:" + expression + ",{TYPE}:\'{RSA}\',d:" + "{KEEP}" + ",k:\'public\'}})"\n'
    )
    unchanged(text, [KEEP])


def test_a_truncated_flow_excerpt_is_still_masked():
    s = secret()
    # A YAML flow mapping cut off before its closing brace.
    text = f'log:\n  jwk: {{{TYPE}: {RSA}, a: x"#c, n: pub, d: {s}'
    protect(text, [s])


# --- Review r8: the object scan's lexemes, '?' then properties, '|N' indentation -----

BACKTICK = "`"
# Each form puts a lexeme the object scan reads as a string, a template, a
# comment or a regular expression before the private member, so that scan
# swallows the member; only the YAML reading still sees it. Some are excerpts
# no YAML loader accepts (an unclosed quote ends at its line for the lexer).
SWALLOWING_LEXEMES = {
    "unclosed-double-quote": ('jwk: {{{t}: {k}, x: "a\n, d: {s}, n: pub}}\n', False),
    "unclosed-single-quote": ("jwk: {{{t}: {k}, x: 'a\n, d: {s}, n: pub}}\n", False),
    "unclosed-triple-quote": ('jwk: {{{t}: {k}, x: """a\n, d: {s}, n: pub}}\n', False),
    "unclosed-quote-ec": ('jwk: {{{t}: EC, x: "a\n, d: {s}, n: pub}}\n', False),
    "unclosed-quote-quoted-key": ('jwk: {{"{t}": "{k}", x: "a\n, "d": {s}}}\n', False),
    "backtick-in-plain": ("jwk: {{{t}: {k}, a: x" + BACKTICK + "c, d: {s}, n: pub}}\n", True),
    "backtick-starting-value": (
        "jwk: {{{t}: {k}, x: " + BACKTICK + "a\n, d: {s}, n: pub}}\n",
        False,
    ),
    "block-comment-in-plain": ("jwk: {{{t}: {k}, x: a /* c, d: {s}, n: pub}}\n", True),
    "line-comment-in-plain": ("jwk: {{{t}: {k}, x: a // c, d: {s}, n: pub}}\n", True),
    "regex-in-plain": ("jwk: {{{t}: {k}, x: a/b, d: {s}, n: c/}}\n", True),
    # Quoted scalars YAML closes but the object scan pairs differently: a
    # doubled quote ('') or a glued third quote opens a triple-quoted string.
    "doubled-quote-run": ("jwk: {{{t}: {k}, x: 'a''''', d: {s}, n: pub}}\n", True),
    "escaped-quote-only": ("jwk: {{{t}: {k}, x: '''', d: {s}, n: pub}}\n", True),
    "glued-quoted-value": ("jwk: {{{t}: {k}, \"x\":'''', d: {s}}}\n", True),
    "triple-double-opening": ('jwk: {{{t}: {k}, x: """a"\n, d: {s}, n: pub}}\n', False),
    "quadruple-single-opening": ("jwk: {{{t}: {k}, x: ''''a\n, d: {s}, n: pub}}\n", False),
}


@pytest.mark.parametrize("form", list(SWALLOWING_LEXEMES))
def test_a_lexeme_that_swallows_a_member_for_the_object_scan_is_divergent(form):
    s = secret()
    template, valid = SWALLOWING_LEXEMES[form]
    text = template.format(t=TYPE, k=RSA, s=s)
    if valid:
        loads_key(text, s)
    else:
        with pytest.raises(yaml.YAMLError):
            list(yaml.safe_load_all(text))
    protect(text, [s])


EXPLICIT_FLOW_PROPERTIES = {
    "tagged-merge": "jwk: {{? !!merge {m}: *b, d: {s}}}\n",
    "anchored-merge": "jwk: {{? &a {m}: *b, d: {s}}}\n",
    "tagged-other-key": "jwk: {{? !!merge x: *b, d: {s}}}\n",
    "tag-then-anchor": "jwk: {{? !!merge &a {m}: *b, d: {s}}}\n",
}


@pytest.mark.parametrize("form", list(EXPLICIT_FLOW_PROPERTIES))
def test_an_explicit_flow_key_with_properties_keeps_its_key(form):
    s = secret()
    text = (BASE + EXPLICIT_FLOW_PROPERTIES[form]).format(t=TYPE, k=RSA, s=s, m=MERGE)
    loads_key(text, s)
    protect(text, [s])


def test_a_block_scalar_indentation_indicator_fixes_its_content_indent():
    # '|2' at the top level: content needs two spaces, so ' kty: ...' (one)
    # is not content and its ' # c' is a comment (malformed for PyYAML).
    s = secret()
    text = f"|2\n {TYPE}: {RSA} # c\n d: {s}\n"
    with pytest.raises(yaml.YAMLError):
        list(yaml.safe_load_all(text))
    protect(text, [s])


@pytest.mark.parametrize("header", ["|2", ">2", "|2-", "|+2", "| # c", "|2 # c", "|", ">-"])
def test_block_scalar_comments_follow_pyyaml(header):
    from unified_agent.telemetry import yaml_jwk

    # With a digit the content indent is fixed (here 4), so a deeper first
    # line is content; without one the first content line sets it.
    first, second = (
        ("      deeper", "    text")
        if any(c.isdigit() for c in header)
        else (
            "    text",
            "      deeper",
        )
    )
    text = f"jwk:\n  {TYPE}: {RSA}\n  note: {header}\n\n{first} # in\n{second} # in\n  n: pub # c\n"
    list(yaml.parse(text))
    lex = yaml_jwk._YamlLex(text)
    hashes = [i for i, ch in enumerate(text) if ch == "#"]
    comments = set(lex.comment_starts)
    in_content = [i for i in hashes if text[:i].count("\n") in (4, 5)]
    assert not comments & set(in_content), (header, sorted(comments))
    assert text.rindex("# c") in comments


@pytest.mark.parametrize(
    "template",
    ["|2\n {t}: {k} # c\nd: {s}\n", " {t}: {k}\nd: {s}\n", "x:\n   {t}: {k}\n  d: {s}\n"],
)
def test_a_key_dedented_to_no_open_mapping_inherits_the_type(template):
    # Malformed for every YAML reader: the dedented key's mapping takes the
    # type of the mapping its line closed (failing toward masking).
    s = secret()
    text = template.format(t=TYPE, k=RSA, s=s)
    with pytest.raises(yaml.YAMLError):
        list(yaml.safe_load_all(text))
    protect(text, [s])


def test_sequence_items_after_a_typed_item_keep_their_own_type():
    s = secret()
    text = f"keys:\n- {TYPE}: {RSA}\n  d: {s}\n- n: x\n  d: {KEEP}\n"
    assert yaml.safe_load(text)["keys"][1] == {"n": "x", "d": KEEP}
    protect(text, [s])
    unchanged(text, [KEEP])


# --- Review r1 threads: properties before a compact sequence or a next-line type -------

# Only node properties after a private member's colon, then a compact sequence
# at the key's own column (YAML's indentless sequence): the items are its value.
PROPERTIES_THEN_COMPACT = {
    "tag": "{i}d: !!seq\n{i}- {s}\n{i}- {s2}\n{i}n: pub\n",
    "anchor": "{i}d: &a\n{i}- {s}\n{i}- {s2}\n{i}n: pub\n",
    "tag-anchor-comment": "{i}d: !!seq &a # c\n{i}- {s}\n{i}- {s2}\n{i}n: pub\n",
    "oth-items": "{i}oth: !!seq\n{i}- r: {s}\n{i}  d: {s2}\n{i}n: pub\n",
}


@pytest.mark.parametrize("nesting", ["top", "nested"])
@pytest.mark.parametrize("form", list(PROPERTIES_THEN_COMPACT))
def test_properties_before_a_compact_sequence_keep_the_sequence_in_the_value(form, nesting):
    s, s2 = secret(), secret()
    indent = "  " if nesting == "nested" else ""
    head = "jwk:\n" if nesting == "nested" else ""
    body = PROPERTIES_THEN_COMPACT[form].format(i=indent, s=s, s2=s2)
    text = f"{head}{indent}{TYPE}: {RSA}\n{body}"
    loads_key(text, s)
    loads_key(text, s2)
    protect(text, [s, s2], ["n: pub"])


# 'kty:' with its type below, after a line holding only node properties.
NEXT_LINE_TYPE_PROPERTIES = {
    "tag": "{i}{t}:\n{i}  !!str\n{i}  {k}\n",
    "anchor": "{i}{t}:\n{i}  &a\n{i}  {k}\n",
    "tag-and-anchor": "{i}{t}:\n{i}  !!str &a\n{i}  {k}\n",
    "two-property-lines": "{i}{t}:\n{i}  !!str\n{i}  &a\n{i}  {k}\n",
    "commented": "{i}{t}: # the type follows\n{i}  !!str # c\n{i}  {k}\n",
}


@pytest.mark.parametrize("nesting", ["top", "nested"])
@pytest.mark.parametrize("form", list(NEXT_LINE_TYPE_PROPERTIES))
def test_a_type_below_a_properties_line_types_the_mapping(form, nesting):
    s = secret()
    indent = "  " if nesting == "nested" else ""
    head = "jwk:\n" if nesting == "nested" else ""
    body = NEXT_LINE_TYPE_PROPERTIES[form].format(i=indent, t=TYPE, k=RSA)
    text = f"{head}{body}{indent}d: {s}\n{indent}n: pub\n"
    loads_key(text, s)
    protect(text, [s], ["pub"])


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("nesting", ["top", "nested"])
def test_a_quoted_private_value_keeps_its_trailing_comment(quote, nesting):
    s, note = secret(), public("note")
    indent = "  " if nesting == "nested" else ""
    head = "jwk:\n" if nesting == "nested" else ""
    text = (
        f"{head}{indent}{TYPE}: {RSA}\n"
        f"{indent}d: {quote}{s}{quote} # {note}\n"
        f"{indent}p: {quote}{s}\n{indent}  {s}{quote}  # {note}-2\n"
        f"{indent}n: pub\n"
    )
    loads_key(text, s)
    out = protect(text, [s], [note, f"{note}-2", "n: pub"])
    assert out.count(f" # {note}") == 2  # both comments outlive their masked values


@pytest.mark.parametrize("nesting", ["top", "nested"])
def test_an_explicit_type_key_after_a_sibling_entry_types_the_mapping(nesting):
    # '? kty' then ': RSA', after a closed entry at the same column.
    s = secret()
    i = "  " if nesting == "nested" else ""
    head = "jwk:\n" if nesting == "nested" else ""
    text = f"{head}{i}n: pub\n{i}? {TYPE}\n{i}: {RSA}\n{i}d: {s}\n"
    loads_key(text, s)
    protect(text, [s], ["pub"])


# Review r9: YAML naming inside an object the object scan decided (its type
# quoted). That scan reads JSON: it never resolves a merge, an alias key or an
# alias value's anchor, so the YAML reading keeps those members there.
DECIDED_YAML_NAMING = {
    "merge-alias": BASE + '{j}: {{"{t}": "{k}", "{j}": {{{m}: *b, d: {s}}}}}\n',
    "merge-list": BASE + '{j}: {{"{t}": "{k}", "{j}": {{{m}: [*b], d: {s}}}}}\n',
    "explicit-merge": BASE + '{j}: {{"{t}": "{k}", "{j}": {{? {m}: *b, d: {s}}}}}\n',
    "quoted-merge": BASE + '{j}: {{"{t}": "{k}", "{j}": {{"{m}": *b, d: {s}}}}}\n',
    "inline-merge": '{j}: {{"{t}": "{k}", "{j}": {{{m}: {{{t}: {e}, x: 1}}, d: {s}}}}}\n',
    "alias-key": 'nm: &n d\n{j}: {{"{t}": "{k}", *n : {s}}}\n',
    "alias-value": '{j}: {{"{t}": "{k}", "v": &v {s}, "d": *v}}\n',
}


@pytest.mark.parametrize("brk", list(BREAKS))
@pytest.mark.parametrize("form", list(DECIDED_YAML_NAMING))
def test_yaml_naming_inside_a_decided_object_is_resolved(form, brk):
    s = secret()
    text = DECIDED_YAML_NAMING[form].format(t=TYPE, k=RSA, e=EC, s=s, m=MERGE, j="j")
    text = text.replace("\n", BREAKS[brk])
    loads_key(text, s)
    protect(text, [s])


def test_a_decided_object_beside_a_merge_keeps_its_public_members():
    keep = public("p")
    text = (BASE + 'x: {{{m}: *b, n: 1}}\nj: {{"{t}": "{e}", "p": "{v}"}}\n').format(
        t=TYPE, k=RSA, e=EC, m=MERGE, v=keep
    )
    yaml.safe_load(text)
    unchanged(text, [keep])


def test_a_json_string_naming_an_anchor_in_a_decided_object_is_not_an_alias():
    keep = public("v")
    s = secret()
    text = f'v: &v {keep}\nj: {{"{TYPE}": "{RSA}", "d": "{STAR}v", "q": "{s}"}}\n'
    loads_key(text, s)
    protect(text, [s], [keep])


def _dense_private_keys(n: int) -> str:
    # One divergent typed flow mapping ('`' parts the readings) holding a long
    # run of private-named keys, each inside the previous one's plain value.
    return f"jwk: {{{TYPE}: {RSA}, a: x`c, " + "d: x " * n + "}\n"


def test_dense_private_keys_in_a_divergent_flow_sweep_linearly():
    small, large = _dense_private_keys(1_000), _dense_private_keys(4_000)
    assert len(large) >= 3.5 * len(small)
    assert_cpu_growth(cold(redact), small, large, limit=8.0, samples=2, label="dense-private-keys")


@pytest.mark.parametrize("brk", list(BREAKS))
def test_each_document_reports_its_own_private_member(brk):
    # The object scan reads "''''" as a Python triple quote running to the end
    # of the text: its member, found in the second document, took the whole
    # literal's span and hid the first document's own member (review r9).
    s1, s2 = secret(), secret()
    text = (
        f"jwk:\n  {TYPE}: \"{OKP}\"\n  x: ''''\n"
        "  a: !<tag:yaml.org,2002:str> |+2 # note\n     q:1\n     second line#'x\"\n"
        f"  d: {s1}\n...\n---\njwk: {{{TYPE}: {OKP}, d: {s2}}}\n"
    ).replace("\n", BREAKS[brk])
    loads_key(text, s1)
    loads_key(text, s2)
    protect(text, [s1, s2])  # each document's line is attributed
    if brk != "lf":
        # A CR in the literal makes its decoded offsets inexact: the member
        # keeps an audit span over both documents (both lines attributed).
        return
    spans = [jwk.audit_span(m, text) for m in jwk.private_members(text)]
    for value in (s1, s2):
        at = text.index(value)
        assert (at, at + len(value)) in spans, (value, spans)
    end_of_first = text.index("...")
    for value, first in ((s1, True), (s2, False)):
        at = text.index(value)
        (holder,) = [m for m in jwk.private_members(text) if m.start <= at < m.end]
        assert (holder.end <= end_of_first) == first, holder
        assert (holder.start >= end_of_first) != first, holder


# Review r10: the same YAML naming carrying an oct key's 'k'. Inside a decided
# object no oct-block reading is left, so the merging mapping keeps it.
DECIDED_OCT_NAMING = {
    "merge-alias": 'b: &b {{{t}: {o}}}\nj: {{"{t}": "{o}", "j": {{{m}: *b, k: {s}}}}}\n',
    "alias-key": 'nm: &n k\nj: {{"{t}": "{o}", *n : {s}}}\n',
    "inline-merge": 'j: {{"{t}": "{o}", "j": {{{m}: {{{t}: {o}, x: 1}}, k: {s}}}}}\n',
    "tagged-merge": 'b: &b {{{t}: {o}}}\nj: {{"{t}": "{o}", "j": {{? !!merge {m}: *b, k: {s}}}}}\n',
    "oct-into-rsa": 'b: &b {{{t}: {o}}}\nj: {{"{t}": "{r}", "j": {{{m}: *b, k: {s}}}}}\n',
    "merge-chain": (
        'a: &a {{{t}: {o}}}\nb: &b\n  {m}: *a\nj: {{"{t}": "{o}", "j": {{{m}: *b, k: {s}}}}}\n'
    ),
}


def loads_oct_key(text: str, secret_value: str) -> None:
    """PyYAML resolves ``text`` to an oct-typed mapping whose ``k`` is ``secret_value``."""
    found = [m for doc in yaml.safe_load_all(text) for m in typed(doc)]
    assert any(m[TYPE] == OCT and m.get("k") == secret_value for m in found), found


@pytest.mark.parametrize("brk", list(BREAKS))
@pytest.mark.parametrize("form", list(DECIDED_OCT_NAMING))
def test_an_oct_key_named_inside_a_decided_object_is_masked(form, brk):
    s = secret()
    text = DECIDED_OCT_NAMING[form].format(t=TYPE, o=OCT, r=RSA, s=s, m=MERGE)
    text = text.replace("\n", BREAKS[brk])
    loads_oct_key(text, s)
    protect(text, [s])


def test_an_oct_word_beside_a_merge_without_a_key_type_is_unchanged():
    keep = public("k")
    text = f"base: &b {{month: {OCT}}}\nrow: {{{MERGE}: *b, k: {keep}, d: {keep}}}\n"
    yaml.safe_load(text)
    unchanged(text, [keep])
