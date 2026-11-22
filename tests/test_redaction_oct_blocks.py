"""Symmetric JWK secrets outside a JSON object: the oct-block rule (task #2093).

Python keyword arguments and YAML mappings carry no braces, so the JSON scan in
``jwk.private_members`` never read them. ``telemetry.oct_blocks`` finds a kty of
oct lexically and masks every value in a bounded block around it. These tests
hold the rule's guarantees:

* every reviewer reproduction from PR #1214 (rounds 1 to 8) inside the rule's
  scope is masked, through the public redactor and the secrets audit;
* objects the JSON scan decides keep its output byte for byte;
* masking never breaks JSON structure;
* the scan is linear in time and its peak memory follows a cost model;
* the audit flags every line whose value an oct block masks.

Every key-shaped fixture is generated at runtime, and the kty name and its oct
value are assembled, so no line of this file opens a block for the security
audit, which reads this source with the same scanner.
"""

from __future__ import annotations

import ast
import json
import pathlib
import random
import secrets

import pytest
from growth_budget import assert_cpu_growth, cold, peak_growth

from unified_agent.audit import rules
from unified_agent.telemetry import jwk, oct_blocks
from unified_agent.telemetry.redaction import (
    CREDENTIAL_SHAPE_PATTERNS,
    redact_sensitive_content,
    secret_patterns,
)
from unified_agent.tools.public_relay import BLOCK, _jwk_findings

TYPE_NAME = "kt" + "y"
OCT_TYPE = "o" + "ct"
KEPT = "kept-value-2093"
MARK = "[REDACTED_SECRET]"


@pytest.fixture
def bootstrap_test_store():
    # Every case below calls pure lexical redaction/audit/relay primitives.
    # Keep the shared environment isolation, without seeding 21k databases.
    return False


@pytest.fixture(autouse=True)
def _forbid_store_access(isolated_env, monkeypatch):
    from unified_agent import config, storage

    def refused(*args, **kwargs):
        pytest.fail("pure redaction tests must not initialize or open a database")

    for name in ("init_db", "connect", "connect_read_only"):
        monkeypatch.setattr(storage, name, refused)
    assert not config.db_path().exists()
    yield
    assert not config.db_path().exists()


def redact(text: str) -> str:
    # Many short physical lines can expand to one marker each. Keep the
    # structural tests away from the redactor's intentional truncation path.
    return redact_sensitive_content(text, max_len=len(text) * 20 + 1000)


def form(template: str, **values: str) -> str:
    return template.format(
        kty=TYPE_NAME, oct_value=OCT_TYPE, oct_upper=OCT_TYPE.upper(), kept=KEPT, **values
    )


# Letters and digits only, a valid base64url `k`. The alphabet makes a
# credential shape rare: with no '-' or '_' a value cannot spell `sk-` or
# `ghp_` (task #2185), and with no uppercase `I` it cannot spell `AIza`, `AKIA`
# or `ASIA`. Rare is not enough for a fixture drawn thousands of times per run,
# so `symmetric()` also rejection-samples (task #2186): a draw that the
# redactor would change, that any credential shape matches, or that lacks the
# mixed case and digit of a real key is discarded and drawn again.
_SYMMETRIC_ALPHABET = "ABCDEFGHJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _is_plain_symmetric(value: str) -> bool:
    if not (
        any(c.isupper() for c in value)
        and any(c.islower() for c in value)
        and any(c.isdigit() for c in value)
    ):
        return False
    if any(pattern.search(value) for _name, pattern in CREDENTIAL_SHAPE_PATTERNS):
        return False
    if any(pattern.search(value) for pattern in secret_patterns()):
        return False
    # Bare and as a JSON member name, where the scanner reads it with context.
    member = f'{{"{value}": 1}}'
    return redact_sensitive_content(value) == value and redact(member) == member


# Rejection sampling is bounded: a scanner change that rejects (nearly) every
# draw must fail loudly rather than hang the run (task #2195).
_MAX_SYMMETRIC_DRAWS = 10_000


def symmetric() -> str:
    for _ in range(_MAX_SYMMETRIC_DRAWS):
        value = "".join(secrets.choice(_SYMMETRIC_ALPHABET) for _ in range(43))
        if _is_plain_symmetric(value):
            return value
    pytest.fail(
        f"symmetric(): no plain key-shaped draw in {_MAX_SYMMETRIC_DRAWS} attempts; "
        "the redactor or a credential shape now rejects (nearly) every 43-character "
        "letters-and-digits value, so _is_plain_symmetric or the alphabet needs review"
    )


def test_symmetric_fixture_rejection_samples_credential_shapes():
    for _ in range(2000):
        value = symmetric()
        assert len(value) == 43
        assert set(value) <= set(_SYMMETRIC_ALPHABET)
        assert _is_plain_symmetric(value)
        assert redact(f'{{"{value}": 1}}') == f'{{"{value}": 1}}'


def test_symmetric_fixture_discards_a_draw_that_matches_a_credential_shape():
    # The filter, not the alphabet, is the guarantee: a shape added to the
    # scanner later that the alphabet can spell is discarded the same way.
    assert not _is_plain_symmetric("ghp_" + "A1b" * 13)
    assert not _is_plain_symmetric("xoxb-" + "A1b" * 13)
    assert not _is_plain_symmetric("AIza" + "A1b" * 13)
    assert not _is_plain_symmetric("a" * 43)  # no uppercase or digit: not key-shaped


def _passes_all_but_class_rule(value: str) -> bool:
    """Every ``_is_plain_symmetric`` check except the character-class rule."""
    member = f'{{"{value}": 1}}'
    return (
        not any(pattern.search(value) for _name, pattern in CREDENTIAL_SHAPE_PATTERNS)
        and not any(pattern.search(value) for pattern in secret_patterns())
        and redact_sensitive_content(value) == value
        and redact(member) == member
    )


@pytest.mark.parametrize(
    ("value", "missing"),
    [
        ("AbCdEfGhJkLmNoPqRsTuVwXyZaBcDeFgHjKmNpQrStU", "digit"),
        ("ab3cd5ef7gh9jk2mn4pq6rs8tu3vw5xy7za9bc2de4f", "uppercase"),
        ("AB3CD5EF7GH9JK2MN4PQ6RS8TU3VW5XY7ZA9BC2DE4F", "lowercase"),
    ],
)
def test_symmetric_fixture_class_rule_alone_rejects(value, missing):
    # Proves the mixed-case-and-digit rule on its own (task #2196): each value
    # is in the alphabet, has the key's length, and clears every credential
    # shape and the redactor, so only the missing character class rejects it.
    assert len(value) == 43
    assert set(value) <= set(_SYMMETRIC_ALPHABET)
    assert _passes_all_but_class_rule(value), f"fixture must fail only on {missing}"
    assert not _is_plain_symmetric(value)


def assert_masked(text: str, *parts: str) -> str:
    out = redact(text)
    for part in parts:
        assert part not in out, out
    assert MARK in out
    return out


def flagged_lines(text: str) -> set[int]:
    return {finding.line for finding in rules.check_secrets("fixture.txt", text)}


def line_of(text: str, needle: str) -> int:
    return next(n for n, row in enumerate(text.splitlines(), 1) if needle in row)


def assert_masked_and_audited(text: str, secret: str) -> str:
    """The redactor hides the secret and the audit flags the line it is on."""
    out = assert_masked(text, secret)
    assert line_of(text, secret) in flagged_lines(text), text
    return out


# --- Python keyword arguments ---------------------------------------------------

KWARGS = {
    "dict": "dict({kty}='{oct_value}', k='{s}')",
    "jwcrypto": 'jwk.JWK({kty}="{oct_value}", k="{s}", kid="{kept}")',
    "k-first": "dict(k='{s}', {kty}='{oct_value}')",
    "multiline": "JWK(\n    {kty}='{oct_value}',\n    k='{s}',\n)",
    "k-first-multiline": "key = JWK(\n    k='{s}',\n    {kty}='{oct_value}',\n)",
    "k-first-aligned": "key = JWK(k='{s}',\n          {kty}='{oct_value}')",
    "expression": "dict({kty}='{oct_value}', k=b64decode('{s}'))",
    "bytes": "dict({kty}='{oct_value}', k=b'{s}')",
    "bytes-kty": "dict({kty}=b'{oct_value}', k=b'{s}')",
    "nested-call": "JWKSet(keys=[dict({kty}='{oct_value}', k='{s}')])",
    "upper-case-oct": "dict({kty}='{oct_upper}', k='{s}')",
    "upper-case-kty": "dict({kty_upper}='{oct_value}', k='{s}')",
    "spaces-around-equals": "dict( {kty} = '{oct_value}' , k = '{s}' )",
    "paren-in-a-string-argument": "dict({kty}='{oct_value}', note=')', k='{s}')",
    "paren-in-a-string-multiline": "x = dict({kty}='{oct_value}', note=')',\n         k='{s}')",
    # Codex round 1, P2-1: a comment's ")" closed the call; a comment hid kty.
    "comment-paren": "dict({kty}='{oct_value}', # )\n k='{s}')",
    "comment-after-kty": "dict(\n {kty}='{oct_value}' # type\n , k='{s}'\n)",
    # Devin round 1, P3-2: a call split from its name across a line.
    "line-continuation": "dict \\\n({kty}='{oct_value}', k='{s}')",
    "implicit-joining": "x = [dict\n({kty}='{oct_value}', k='{s}')]",
    # Devin round 3, P3-2 and rounds 4-5: attribute and subscript assignment.
    "attribute": '{kty}: {oct_value}\nkey.k = "{s}"',
    "subscript": '{kty}: {oct_value}\nkey["k"] = "{s}"',
    "subscript-chain": '{kty}: {oct_value}\nkey["k"]["j"] = "{s}"',
    "long-subscript-chain": '{kty}: {oct_value}\nx["k"]["a"]["b"]["c"]["d"]["e"] = "{s}"',
    "long-attribute-chain": "{kty}: {oct_value}\nd.k.a.b.c.d.e = '{s}'",
    "attribute-then-subscript": '{kty}: {oct_value}\nd.k["j"] = "{s}"',
}


@pytest.mark.parametrize("name", list(KWARGS))
def test_a_symmetric_jwk_written_as_python_kwargs_is_redacted(name):
    secret = symmetric()
    text = form(KWARGS[name], s=secret, kty_upper=TYPE_NAME.upper())
    out = assert_masked_and_audited(text, secret)
    assert TYPE_NAME in out.lower()


def test_kwargs_keep_their_names_and_quotes():
    secret = symmetric()
    out = redact(form('jwk.JWK({kty}="{oct_value}", k="{s}", kid="{kept}")', s=secret))
    assert out == form('jwk.JWK({kty}="{oct_value}", k="{m}", kid="{m}")', m=MARK)


def test_a_k_value_continued_on_the_next_line_is_masked_whole():
    head, tail = symmetric(), symmetric()
    text = form("JWK({kty}='{oct_value}', k='{a}'\n        '{b}')", a=head, b=tail)
    assert_masked(text, head, tail)
    assert {1, 2} <= flagged_lines(text)


@pytest.mark.parametrize("name", ["dict", "multiline", "comment-paren", "k-first"])
def test_kwargs_inside_an_encoded_string_are_redacted(name):
    secret = symmetric()
    text = json.dumps({"log": form(KWARGS[name], s=secret)})
    out = assert_masked_and_audited(text, secret)
    assert json.loads(out).keys() == {"log"}


# --- YAML -----------------------------------------------------------------------

YAML = {
    "plain": "{kty}: {oct_value}\nk: {s}",
    "double-quoted": '{kty}: "{oct_value}"\nk: "{s}"',
    "both-double-quoted": '"{kty}": "{oct_value}"\n"k": "{s}"',
    "single-quoted": "{kty}: '{oct_value}'\nk: '{s}'  # rotated monthly",
    "k-first": "k: {s}\nkid: {kept}\n{kty}: {oct_value}",
    "nested": "jwk:\n  {kty}: {oct_value}\n  k: {s}\nname: {kept}",
    "sequence": "keys:\n  - {kty}: RSA\n    n: pub\n  - {kty}: {oct_value}\n    k: {s}",
    "block-scalar": "{kty}: {oct_value}\nk: |\n  {s}\n\n  {t}\nkid: {kept}",
    "block-scalar-k-first": "k: |\n  {s}\n\n  {t}\n{kty}: {oct_value}",
    "folded-scalar": "{kty}: {oct_value}\nk: >-\n  {s}\n  {t}",
    "crlf": "{kty}: {oct_value}\r\nk: {s}\r\nkid: {kept}",
    "tabs": "{kty}:\t{oct_value}\nk:\t{s}",
    "kty-value-below": "{kty}:\n  {oct_value}\nk: {s}",
    "flow-mapping": "{{{kty}: {oct_value}, k: {s}}}",
    "flow-mapping-in-a-list": "keys: [{{{kty}: {oct_value}, k: {s}}}]",
    "list-value": "{kty}: {oct_value}\nk:\n  - {s}\n  - {t}\nkid: {kept}",
    # Codex round 1, P2-2 / Devin round 1, P3-3: a comment-only value; a
    # continued plain scalar.
    "comment-only-value": "{kty}: {oct_value}\nk: # value below\n  {s}",
    "plain-continuation": "{kty}: {oct_value}\nk: first-part\n  {s}",
    # Codex round 1, P2-3: anchors and aliases on the kty.
    "anchored-kty": "{kty}: &type {oct_value}\nk: {s}",
    "aliased-kty": "type: &type {oct_value}\n{kty}: *type\nk: {s}",
    # Codex round 1, P2-4: escaped quoted names.
    "escaped-kty": '"k\\u0074y": {oct_value}\n"k": {s}',
    "escaped-k": '{kty}: {oct_value}\n"\\u006b": {s}',
    "fully-escaped-kty": '"\\u006b\\u0074\\u0079": {oct_value}\n"k": {s}',
    # Devin round 1, P2-1: legal YAML keys between kty and k.
    "dotted-key": "{kty}: {oct_value}\nnote.1: x\nk: {s}",
    "digit-key": "{kty}: {oct_value}\n1k: x\nk: {s}",
    "question-key": "{kty}: {oct_value}\na?b: x\nk: {s}",
    "spaced-key": "{kty}: {oct_value}\nmy key: x\nrotation.period: 30d\nk: {s}",
    # Devin round 1, P3-4: a double-quoted scalar spanning lines.
    "multiline-quoted": 'x: "{kty}: {oct_value}\nk: {s}"',
    # Codex round 2, P2-3: a next-line alias, anchor or tag on the kty.
    "kty-alias-below": "type: &type {oct_value}\n{kty}:\n  *type\nk: {s}",
    "kty-anchor-then-oct-below": "{kty}: &type\n  {oct_value}\nk: {s}",
    "kty-tag-then-oct-below": "{kty}: !!str\n  {oct_value}\nk: {s}",
    # Codex round 2, P3-5: a k-first sequence item.
    "k-first-sequence-item": "- k: {s}\n  {kty}: {oct_value}",
    # Devin round 2, P3-2: a comment line or a block-scalar header before oct.
    "comment-before-oct": "{kty}:\n  # note\n  {oct_value}\nk: {s}",
    "folded-header": "{kty}: >\n  {oct_value}\nk: {s}",
    "literal-header": "{kty}: |\n  {oct_value}\nk: {s}",
    "literal-keep-header": "{kty}: |+\n  {oct_value}\nk: {s}",
    "folded-strip-header": "{kty}: >-\n  {oct_value}\nk: {s}",
    # Devin round 3, P3-1: comment lines less indented than the kty.
    "comment-below-at-column-0": "data:\n    {kty}: {oct_value}\n# note\n    k: {s}",
    "comment-above-mid-indent": "data:\n    k: {s}\n  # note\n    {kty}: {oct_value}",
    "tag-and-anchor-on-k": "{kty}: {oct_value}\nk: !tag &a {s}",
    "document-start": "---\n{kty}: {oct_value}\nk: {s}\n...",
    # PR #1221 Codex round 1, P2-2: a blank line does not end a YAML mapping.
    "blank-line-between-members": "{kty}: {oct_value}\n\nk: {s}",
    "nested-blank-line": "jwk:\n  {kty}: {oct_value}\n\n  k: {s}\n",
    "nested-blank-line-k-first": "jwk:\n  k: {s}\n\n  {kty}: {oct_value}\n",
    "many-blank-lines": "{kty}: {oct_value}\n" + "\n" * 40 + "k: {s}",
    # PR #1221 Devin round 1, P3-1: comment and blank lines before oct.
    "comment-lines-before-oct": "{kty}:\n" + "# c\n" * 20 + "  {oct_value}\nk: {s}",
    "blank-lines-before-oct": "{kty}:\n" + "\n" * 20 + "  {oct_value}\nk: {s}",
    # PR #1221 Devin round 1, P3-2: an explicit YAML key.
    "explicit-key": "? {kty}\n: {oct_value}\nk: {s}",
}


@pytest.mark.parametrize("name", list(YAML))
def test_a_symmetric_jwk_written_as_yaml_is_redacted(name):
    secret, tail = symmetric(), symmetric()
    text = form(YAML[name], s=secret, t=tail)
    assert_masked_and_audited(text, secret)
    if "{t}" in YAML[name]:
        assert_masked_and_audited(text, tail)


def test_yaml_keeps_its_keys_and_marks_bare_values_as_strings():
    secret = symmetric()
    out = redact(form("jwk:\n  {kty}: {oct_value}\n  k: {s}\n  kid: '{kept}'", s=secret))
    assert out == form("jwk:\n  {kty}: {oct_value}\n  k: \"{m}\"\n  kid: '{m}'", m=MARK)


@pytest.mark.parametrize("name", ["plain", "nested", "block-scalar", "fully-escaped-kty"])
def test_yaml_inside_an_encoded_string_is_redacted(name):
    secret, tail = symmetric(), symmetric()
    text = json.dumps({"config": form(YAML[name], s=secret, t=tail)})
    out = assert_masked_and_audited(text, secret)
    assert tail not in out
    assert json.loads(out).keys() == {"config"}


def test_a_fully_escaped_kty_in_encoded_yaml_is_redacted():
    # Codex round 2, P2-1: the decoded string held neither a literal kty nor a
    # brace, so neither earlier scan read it.
    secret = symmetric()
    escaped = "".join(f"\\u{ord(c):04x}" for c in TYPE_NAME)
    inner = f'"{escaped}": {OCT_TYPE}\n"k": {secret}'
    # The random secret may itself contain "kty"; only the key must be escaped.
    assert escaped in inner and TYPE_NAME not in inner.replace(secret, "")
    assert_masked_and_audited(json.dumps({"log": inner}), secret)
    upper = "".join(f"\\u{ord(c):04X}" for c in TYPE_NAME.upper())
    assert_masked_and_audited(json.dumps({"log": inner.replace(escaped, upper)}), secret)


# --- Lexical forms: strings, other separators -------------------------------------

LEXICAL = {
    # Devin round 2, P2-1: a k inside a quoted value in an oct block; a kty in a
    # string opening a block for the next line.
    "k-in-double-quotes": '{kty}: {oct_value}\nnote: "k: {s}"',
    "k-in-kwarg-string": 'dict({kty}="{oct_value}", blob="k: {s}")',
    "k-equals-in-single-quotes": "{kty}: {oct_value}\nnote: 'k={s}'",
    "kty-in-a-string": 'x = "{kty}: {oct_value}"\nk: {s}',
    # Codex round 2, P3-4: quoted names with "=".
    "quoted-k-equals": '{kty}: {oct_value}\n"k" = "{s}"',
    "quoted-kty-equals": '"{kty}" = "{oct_value}"\nk: {s}',
    # Devin round 2, P3-3: hash rockets; oct on the line after "kty = (".
    "hash-rocket": '{kty} => "{oct_value}"\nk => "{s}"',
    "paren-then-oct-below": "{kty} = (\n    '{oct_value}'\n)\nk = '{s}'",
    # Devin round 7, P3-4: an escaped quote in a single-quoted string.
    "escaped-single-quote-before-kty": "x = 'a\\' # ' + '{kty}: {oct_value}'\nk: {s}",
    "log-line": "2026-10-14 INFO loaded key {kty}={oct_value} k={s} user=bob",
}


@pytest.mark.parametrize("name", list(LEXICAL))
def test_the_rule_is_lexical_strings_and_other_separators_included(name):
    secret = symmetric()
    assert_masked_and_audited(form(LEXICAL[name], s=secret), secret)


def test_a_k_inside_a_string_is_masked_up_to_its_closing_quote_only():
    # Codex round 3, P2-2: the mask ran to the end of the line and took the
    # string's closing quote and the JSON after it.
    secret = symmetric()
    out = assert_masked(form("dict({kty}='{oct_value}', blob='k={s}', kid='x')", s=secret), secret)
    # Inside a value nothing is a member name (round 2): the whole string goes.
    assert out == form("dict({kty}='{oct_value}', blob='{m}', kid='{m}')", m=MARK)
    text = json.dumps({"task": {"title": "k: metric label", "notes": form("{kty}: {oct_value}")}})
    assert json.loads(redact(text)).keys() == {"task"}


# --- PR #1221 review round 1 ---------------------------------------------------------

ESCAPES = {
    "x": lambda c: f"\\x{ord(c):02x}",
    "u": lambda c: f"\\u{ord(c):04x}",
    "U": lambda c: f"\\U{ord(c):08x}",
}


@pytest.mark.parametrize("width", list(ESCAPES))
def test_a_value_written_entirely_as_escapes_is_masked(width):
    # Codex round 1, P2-1: escape tokens were kept as structure, so a k made of
    # \xNN, \uNNNN or \UNNNNNNNN escapes passed whole and decoded to the key.
    import yaml

    secret = symmetric()
    escaped = "".join(ESCAPES[width](c) for c in secret)
    kwargs = form("dict({kty}='{oct_value}', k='{e}')", e=escaped)
    out = redact(kwargs)
    assert escaped not in out and escaped[:24] not in out
    call = ast.parse(out, mode="eval").body
    assert ast.literal_eval(call.keywords[1].value) != secret
    assert 1 in flagged_lines(kwargs)
    document = form('{kty}: {oct_value}\nk: "{e}"\n', e=escaped)
    out = redact(document)
    assert escaped[:24] not in out
    assert yaml.safe_load(out)["k"] != secret
    assert 2 in flagged_lines(document)


@pytest.mark.parametrize(
    "separator", ["\\u003a", "\\x3a", "\\U0000003a", "\\u003d", "\\x3d", "\\u003D"]
)
def test_an_escaped_separator_opens_a_block(separator):
    # Devin round 1, P2-1: a producer escaping ":" or "=" defeated the opener.
    secret = symmetric()
    inner = TYPE_NAME + separator + " " + OCT_TYPE + "\\nk" + separator + " " + secret
    text = '{"log": "' + inner + '"}'
    out = assert_masked_and_audited(text, secret)
    assert json.loads(out).keys() == {"log"}
    bare = TYPE_NAME + separator + " " + OCT_TYPE + "\nk: " + secret
    assert_masked_and_audited(bare, secret)


def test_an_escaped_hash_rocket_opens_a_block():
    secret = symmetric()
    text = TYPE_NAME + " \\u003d\\u003e " + OCT_TYPE + "\nk => " + secret
    assert_masked_and_audited(text, secret)


@pytest.mark.parametrize("length", [17, 24, 200])
def test_an_aliased_block_scalar_is_masked_through_its_end(length):
    # Devin round 1, P2-2: an anchored value was cut at 16 deeper lines, so the
    # tail of a long aliased block scalar stayed visible.
    rows = [symmetric() for _ in range(length)]
    text = (
        "a: &x |\n"
        + "".join(f"  {row}\n" for row in rows)
        + "\n  # trailing comment\n"
        + FAR
        + form("{kty}: {oct_value}\nk: *x")
    )
    out = assert_masked(text, *rows)
    flagged = flagged_lines(text)
    assert {line_of(text, row) for row in rows} <= flagged
    assert out.count(MARK) >= length


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_an_aliased_anchor_masks_to_the_end_of_its_document_only(newline):
    # PR #1221 round 4 (coordinator): an anchored value runs from its definition
    # to the end of its YAML document; the documents before and after keep
    # their text byte for byte.
    secret = symmetric()
    before = "earlier: " + KEPT + "\n---\n"
    after = "---\nlater: " + KEPT + "\n...\nlast: " + KEPT + "\n"
    for marker in ("---", "..."):
        body = form(
            "top:\n  a: &x\n    b: {s}\n  after: {kept}\n" + FAR + "{kty}: {oct_value}\nk: *x\n",
            s=secret,
        )
        text = (before + body + after.replace("---", marker, 1)).replace("\n", newline)
        out = assert_masked_and_audited(text, secret)
        assert out.startswith(before.replace("\n", newline))
        assert out.endswith(after.replace("---", marker, 1).replace("\n", newline))
        assert "after: " + KEPT not in out  # inside the anchor's document: masked


# --- PR #1221 review round 2 ---------------------------------------------------------


@pytest.mark.parametrize("escape", ["\\n", "\\u000a", '\\"', "\\'", "\\r\\n", "\\x0a"])
@pytest.mark.parametrize("tail", [":", "=x", ": ", ""])
def test_an_escape_inside_a_value_never_ends_its_mask(escape, tail):
    # Codex round 2, P2-2: an escaped break or quote reset the value context,
    # so a following "<key>:" read as a member name and passed whole.
    import base64

    import yaml

    raw = secrets.token_bytes(32)
    fake = base64.urlsafe_b64encode(raw).decode()
    text = form("dict({kty}='{oct_value}', k='{e}{f}{t}')", e=escape, f=fake, t=tail)
    out = redact(text)
    assert fake not in out and fake[:16] not in out and fake[-16:] not in out, out
    value = ast.literal_eval(ast.parse(out, mode="eval").body.keywords[1].value)
    assert fake[:16] not in value
    assert 1 in flagged_lines(text)
    if escape != "\\'":
        document = form('{kty}: {oct_value}\nk: "{e}{f}{t}"\n', e=escape, f=fake, t=tail)
        out = redact(document)
        assert fake[:16] not in out and fake[-16:] not in out, out
        assert fake[:16] not in str(yaml.safe_load(out)["k"])
        assert 2 in flagged_lines(document)


def test_mixed_escapes_quotes_and_separators_inside_a_value_stay_masked():
    secret = symmetric()
    half = len(secret) // 2
    for value in (
        f"\\n{secret[:half]}:\\n{secret[half:]}=",
        f'{secret[:half]}\\"{secret[half:]}:',
        f"\\x41{secret[:half]}\\u003a{secret[half:]}",
        f"a: \\n{secret[:half]}: \\n{secret[half:]}: ",
    ):
        for text in (
            form("dict({kty}='{oct_value}', k='{v}')", v=value),
            json.dumps({"log": form("{kty}: {oct_value}\nk: ") + value}),
            form("{kty}: {oct_value}\nk: {v}", v=value),
        ):
            out = redact(text)
            assert secret[:half] not in out and secret[half:] not in out, (text, out)
            assert flagged_lines(text), text


@pytest.mark.parametrize("space", [" ", "\t", "\u2003", "\u3000"])
@pytest.mark.parametrize("width", list(ESCAPES))
def test_any_escaped_whitespace_before_oct_opens_a_block(width, space):
    # Devin round 2, P3: only \u0020 and \x20 were skipped, so \U00000020, \x09
    # or \x0a between the separator and oct opened nothing.
    if width == "x" and ord(space) > 0xFF:
        pytest.skip("\\x holds one byte")
    secret = symmetric()
    gap = ESCAPES[width](space)
    for separator in (":", "\\u003a"):
        text = TYPE_NAME + separator + gap + OCT_TYPE + "\nk: " + secret
        assert_masked_and_audited(text, secret)


def test_an_escaped_line_break_before_oct_opens_a_block():
    secret = symmetric()
    for gap in ("\\x0a", "\\x0d\\x0a", "\\U0000000a", "\\x0c", "\\x0b"):
        assert_masked_and_audited(TYPE_NAME + ":" + gap + OCT_TYPE + "\nk: " + secret, secret)


def test_an_anchor_on_a_comment_line_is_not_a_definition():
    # Devin round 2, P3: a commented-out anchor counted as defined but masked
    # nothing, so the unknown-alias fallback never ran and a real anchored
    # secret stayed visible.
    secret, other = symmetric(), symmetric()
    pad = "".join(f"pad{i:02d}: v\n" for i in range(40))
    for comment in ("# &x " + other, "  # &x " + other, "# &x"):
        text = (
            "real: &y " + secret + "\n" + pad + comment + "\n" + form("{kty}: {oct_value}\nk: *x\n")
        )
        out = assert_masked_and_audited(text, secret)
        assert other not in out


# --- PR #1221 independent review (Desktop_Agent2) at 18d6d796 -------------------------

#: Puts the oct block far below an anchored definition, outside its window.
ALIASED_FAR = "padding: ordinary\n" * 30 + "{kty}: {oct_value}\nk: *x\n"

FLOW_NODES = {
    # The three reproductions: a node continued at column 0.
    "escaped-newline-scalar": 'data: &x "\\\n{s}"\n',
    "flow-sequence": "data: &x [\n{s}\n]\n",
    "flow-mapping": "data: &x {{\nv: {s}\n}}\n",
    # Nested flow, quotes and comments inside it, tags and properties first.
    "nested-flow": "data: &x [\n  [a, {{v: [\n{s}\n]}}],\nb\n]\n",
    "brackets-inside-quotes": "data: &x [\n\"]\", '}}',\n{s}\n]\n",
    "comment-inside-flow": "data: &x [ # ]\n{s}\n]\n",
    "tagged-sequence": "data: &x !!seq [\n{s}\n]\n",
    "tag-then-anchor": 'data: !!str &x "\\\n{s}"\n',
    "anchor-then-tag": 'data: &x !t "\\\n{s}"\n',
    "double-quoted-escaped-quote": 'data: &x "a\\"\n{s}"\n',
    "single-quoted": "data: &x 'it''s\n{s}'\n",
    "single-quoted-flow-item": "data: &x ['a''b',\n{s}]\n",
    # Lexical forms the rule reads too, though a YAML parser rejects them.
    "anchor-then-anchor": 'data: &x &y "\\\n{s}"\n',
    "value-on-the-next-line": 'data: &x\n"\\\n{s}"\n',
    "sequence-item-anchor": "- &x [\n{s}\n]\n",
}


@pytest.mark.parametrize("name", list(FLOW_NODES))
def test_an_aliased_quoted_or_flow_node_is_masked_to_its_close(name):
    # Desktop_Agent2 review, P2: an anchored node's extent was measured by
    # indentation only, so a quoted scalar or flow collection continued at
    # column 0 leaked when aliased from a distant oct block.
    secret = symmetric()
    text = form(FLOW_NODES[name] + ALIASED_FAR, s=secret)
    assert_masked_and_audited(text, secret)


@pytest.mark.parametrize(
    "node",
    ['data: &x "\\\n{s}\n', "data: &x [\n{s}\n", "data: &x {{\nv: {s}\n", "data: &x '\n{s}\n"],
    ids=["unclosed-double", "unclosed-sequence", "unclosed-mapping", "unclosed-single"],
)
def test_an_unclosed_anchored_node_masks_to_the_next_document(node):
    secret, after = symmetric(), symmetric()
    text = form(node + "x: {t}\n" + ALIASED_FAR + "---\nlater: " + KEPT + "\n", s=secret, t=after)
    out = assert_masked_and_audited(text, secret)
    assert after not in out
    assert out.endswith("---\nlater: " + KEPT + "\n")  # the next document is its own


def test_an_unclosed_anchored_node_with_no_document_marker_masks_to_the_end():
    secret, after = symmetric(), symmetric()
    text = form("data: &x [\n{s}\n" + "tail: {t}\n" * 3 + ALIASED_FAR, s=secret, t=after)
    assert_masked(text, secret, after)


def test_an_aliased_flow_node_inside_json_keeps_the_json_valid():
    secret = symmetric()
    text = json.dumps({"a": "&x [", "b": secret, "log": form("{kty}: {oct_value} *x")}, indent=2)
    out = redact(text)
    assert json.loads(out).keys() == {"a", "b", "log"}
    assert secret not in out


# --- PR #1221 round 4: an aliased anchor masks to the end of its document ------------

ROUND_4 = {
    # Codex r4, P2-1: only the inner anchor of a nested node is aliased.
    "inner-anchor-quoted": 'data: &outer [&x "\\\n{s}"\n]\n',
    "inner-anchor-flow": "data: &outer [&x [\n{s}\n]]\n",
    # Codex r4, P2-2: a comment right after flow punctuation.
    "comment-after-open": "data: &x [# ]\n{s}\n]\n",
    "comment-after-comma": "data: &x [a,# ]\n{s}\n]\n",
    # Devin r3, P2: a block collection on the next line at the same indent.
    "same-indent-sequence": "data: &x\n- {s}\n- v2\n",
    "same-indent-sequence-after-comment": "data: &x # note\n- {s}\n- v2\n",
    "nested-same-indent-sequence": "outer:\n  k: &x\n  - {s}\n",
    # Devin r3, P3: a mismatched close inside a flow node (malformed).
    "mismatched-close": "data: &x [\n}}\n{s}\n]\n",
}
#: Which of these a YAML parser accepts, with the alias resolving to the secret.
ROUND_4_VALID = {
    "inner-anchor-quoted",
    "inner-anchor-flow",
    "comment-after-open",
    "comment-after-comma",
    "same-indent-sequence",
    "same-indent-sequence-after-comment",
    "nested-same-indent-sequence",
}


@pytest.mark.parametrize("name", list(ROUND_4))
def test_round_4_anchored_nodes_are_masked(name):
    import yaml

    secret = symmetric()
    text = form(ROUND_4[name] + ALIASED_FAR, s=secret)
    if name in ROUND_4_VALID:
        assert secret in json.dumps(yaml.safe_load(text)["k"])  # the alias is the secret
    assert_masked_and_audited(text, secret)


@pytest.mark.parametrize("name", list(FLOW_NODES))
def test_the_desktop_Agent2_shapes_stay_masked_under_the_document_rule(name):
    secret = symmetric()
    text = form(FLOW_NODES[name] + ALIASED_FAR + "---\nnext: " + KEPT + "\n", s=secret)
    out = assert_masked_and_audited(text, secret)
    assert out.endswith("---\nnext: " + KEPT + "\n")


def test_repeated_unclosed_documents_mask_each_document():
    # Codex r4, P2-3 (functional side): every unclosed node is bounded by its
    # own document.
    secrets_ = [symmetric() for _ in range(5)]
    text = "".join(f"a: &x [\n{s}\n---\n" for s in secrets_) + form("{kty}: {oct_value}\nk: *x\n")
    out = assert_masked(text, *secrets_)
    flagged = flagged_lines(text)
    assert {line_of(text, s) for s in secrets_} <= flagged
    assert out.count("---") == 5


# --- What stays ---------------------------------------------------------------------

KEPT_FORMS = {
    "no-kty": "dict(k='{kept}')",
    "rsa-kwargs": "dict({kty}='RSA', k='{kept}')",
    "blank-line-apart": "f({kty}='{oct_value}')\n\ng(k='{kept}')",
    "next-line-call": "f({kty}='{oct_value}')\ng(k='{kept}')",
    "comparisons": "{kty} == '{oct_value}' and k == '{kept}'",
    "yaml-no-kty": "k: {kept}\nname: other",
    "yaml-rsa": "{kty}: RSA\nk: {kept}",
    "annotations": "{kty}: str\nk: str",
    "octet": "{kty}: octet\nk: {kept}",
    "kty-variable": "dict({kty}=KTY_OCT, k='{kept}')",
    "sibling-mappings": "first:\n  {kty}: {oct_value}\nsecond:\n  k: {kept}",
    "separate-documents": "{kty}: {oct_value}\n---\nk: {kept}",
    "far-below": "{kty}: {oct_value}\n" + "x: 1\n" * (oct_blocks.WINDOW_LINES + 1) + "k: {kept}",
    "far-above": "k: {kept}\n" + "x: 1\n" * (oct_blocks.WINDOW_LINES + 1) + "{kty}: {oct_value}",
    "oct-too-far-below": "{kty}:\n"
    + "x: 1\n" * (oct_blocks.OCT_LOOKAHEAD + 1)
    + "y: {oct_value}\n\nk: {kept}",
    "kty-without-oct": 'note: "{kty} hint, k: {kept}"',
    "double-colon": "{kty}:: {oct_value}\nk: {kept}",
    "longer-names": (
        "my{kty}: {oct_value}\nfoo.{kty}: {oct_value}\nC:\\{kty}: {oct_value}\nk: {kept}"
    ),
    "oct-before-kty": "{oct_value}: {kty}\nk: {kept}",
    "template-value": "{{%s'{kty}': %s'{oct_value}', %s'k': %s'%s', 'kid': '{kept}'}}",
    # PR #1221 Devin round 1, P3-3: a kty held in an expression (a limit).
    "kty-expression": "dict({kty}=g(), k='{kept}')",
    # JWK_KTY is one longer name, not kty (Devin round 1, P3-5: deliberate).
    "env-style": "JWK_{kty_upper}={oct_value}\nJWK_K={kept}",
    # Out of scope by design: implicit concatenation of the oct value.
    "implicit-concatenation": "dict({kty}='oc' 't', k='{kept}')",
}


@pytest.mark.parametrize("name", list(KEPT_FORMS))
def test_a_k_outside_a_symmetric_jwk_block_is_kept(name):
    text = form(KEPT_FORMS[name], kty_upper=TYPE_NAME.upper())
    out = redact(text)
    if name.startswith("far-"):
        # The lines inside the window are masked; the k beyond it is not.
        kept = line_of(text, KEPT) - 1
        assert out.splitlines()[kept] == text.splitlines()[kept]
    else:
        assert out == text
        assert oct_blocks.oct_block_values(text, []) == []


def test_an_object_the_json_scan_decides_keeps_its_behaviour():
    # A nested object's k is not the oct key's, and the JSON scan says so; the
    # oct-block rule does not override it. A note in a decided object is the
    # JSON scan's to judge too (Devin round 2 listed it; out of scope here).
    for text in (
        json.dumps({TYPE_NAME: OCT_TYPE, "nested": {"k": KEPT}}),
        json.dumps({TYPE_NAME: OCT_TYPE, "nested": {"k": KEPT}}, indent=2),
        repr({TYPE_NAME: OCT_TYPE, "nested": {"k": KEPT}}),
        json.dumps({TYPE_NAME: OCT_TYPE, "note": "k: " + KEPT}),
        json.dumps({"body": json.dumps({TYPE_NAME: OCT_TYPE, "kid": KEPT})}),
    ):
        assert redact(text) == text
        found, decided = jwk._json_members(text, 0)
        assert jwk.private_members(text) == found and decided


def test_an_anchor_inside_a_decided_object_stays_the_json_scans():
    # Devin round 1, P3-4 (a documented limit): an alias in a block does not
    # reach into an object the JSON scan decided; that object is unchanged.
    decided = json.dumps({TYPE_NAME: "RSA", "a": "&x " + KEPT})
    text = form("{decided}\n{kty}: {oct_value}\nk: *x", decided=decided)
    assert redact(text).startswith(decided)


def test_a_decided_object_inside_a_block_is_cut_out_of_it():
    secret = symmetric()
    decided = json.dumps({TYPE_NAME: "RSA", "n": KEPT})
    text = form("{kty}: {oct_value}\nmeta: {decided}\nk: {s}", decided=decided, s=secret)
    out = assert_masked(text, secret)
    assert decided in out


def test_over_redaction_near_an_oct_kty_is_accepted():
    # Documented trade-off: every value near a kty of oct is masked, not only
    # k: a child mapping, the next sequence item, kid, a comment, and a function
    # name on the line. A commented-out type cannot classify live values.
    for template in (
        "{kty}: {oct_value}\nnested:\n  k: {kept}",
        "- {kty}: {oct_value}\n- k: {kept}",
        "{kty}: {oct_value}\nkid: {kept}",
        "{kty}: {oct_value}\n# {kept}\nk: v",
        "{kty}: {oct_value}\nf: {kept}(1)",
    ):
        assert KEPT not in redact(form(template)), template


@pytest.mark.parametrize("layers", [0, 1, 2])
def test_commented_type_does_not_classify_a_live_call_value(layers):
    text = form("dict( # {kty}='{oct_value}',\n k='{kept}'\n)")
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    assert redact(text) == text
    assert not jwk.private_members(text)
    assert not flagged_lines(text)
    assert not _jwk_findings(text)


@pytest.mark.parametrize("layers", [0, 1, 2])
def test_live_type_still_classifies_a_private_call_value(layers):
    secret = symmetric()
    text = form("dict({kty}='{oct_value}',\n k='{s}'\n)", s=secret)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    assert any(secret in text[m.start : m.end] for m in jwk.private_members(text))
    findings = _jwk_findings(text)
    assert findings and all(finding.action == BLOCK for finding in findings)
    assert any(secret in text[f.start : f.end] for f in findings)
    if layers:
        json.loads(out)
    else:
        ast.parse(out)


# --- Anchors and aliases --------------------------------------------------------------

ANCHORS = {
    # Codex round 1, P2-3: only "*secret" was masked, the anchored value stayed.
    "aliased-secret": "material: &secret {s}\n{kty}: {oct_value}\nk: *secret",
    # Devin round 5, P2: an anchored value below its anchor, or a block scalar.
    "anchored-value-below": "a: &x\n  {s}\n{kty}: {oct_value}\nk: *x",
    "anchored-literal-block": "a: &x |\n  {s}\n  {s}\n{kty}: {oct_value}\nk: *x",
    "anchored-folded-block": "a: &x >\n  {s}\n{kty}: {oct_value}\nk: *x",
    "anchored-sequence-item": "- &x\n  {s}\n- {kty}: {oct_value}\n  k: *x",
    "tagged-anchor-below": "a: !!str &x\n  {s}\n{kty}: {oct_value}\nk: *x",
    "comment-then-anchored-value": "a: &x\n  # note\n  {s}\n{kty}: {oct_value}\nk: *x",
    # Codex round 3, P2-1: a comment anchor shadowed the real one.
    "comment-anchor-after-alias": "a: &x {s}\n{kty}: {oct_value}\nk: *x # &x ignored",
    "comment-anchor-between": "a: &x {s}\n# b: &x not-an-anchor\n{kty}: {oct_value}\nk: *x",
    "two-definitions": "a: &x {s}\nb: &x {s}\n{kty}: {oct_value}\nk: *x",
    # Codex round 7: a property-only line before the alias on the next one.
    "tag-line-then-alias": "a: &x {s}\n{kty}: {oct_value}\nk: !tag\n  *x",
    "anchor-line-then-alias": "a: &x {s}\n{kty}: {oct_value}\nk: &other\n  *x",
    "property-below-then-alias": "a: &x {s}\n{kty}: {oct_value}\nk:\n  !tag\n  *x",
    "crlf-nested": "top:\r\n  a: &x {s}\r\n  {kty}: {oct_value}\r\n  k: !t\r\n    *x",
    "definition-after-the-alias": "{kty}: {oct_value}\nk: *x\nlater: &x {s}",
    "unknown-alias": "a: &x {s}\n{kty}: {oct_value}\nk: *missing",
    # Devin round 7, P2-1 to P2-3.
    "tag-ends-the-line": "a: &x {s}\n{kty}: {oct_value}\nk: !t\n  *x",
    "alias-in-a-flow-sequence": "a: &x {s}\n{kty}: {oct_value}\nk: [*x]",
    "alias-in-a-flow-mapping": "a: &x {s}\n{kty}: {oct_value}\nk: {{v: *x}}",
    "alias-in-parentheses": "a: &x {s}\n{kty}: {oct_value}\nk: (*x)",
    "alias-in-a-kwarg-call": "a: &x {s}\n{kty}: {oct_value}\nk=(*x)",
    "alias-after-a-first-item": "a: &x {s}\n{kty}: {oct_value}\nk: x, *x",
    "second-alias": "a: &x {s}\nb: &y v\n{kty}: {oct_value}\nk: *y *x",
    "anchor-after-equals": "a=&x {s}\n{kty}: {oct_value}\nk=*x",
    "anchor-after-a-parenthesis": "f(&x {s})\n{kty}: {oct_value}\nk: *x",
    "anchor-inside-quotes": "a: '&x {s}'\n{kty}: {oct_value}\nk: *x",
}

#: Padding that puts the anchored definition outside the oct block's window.
FAR = "pad: v\n" * (oct_blocks.WINDOW_LINES + 4) + "\n"


@pytest.mark.parametrize("far", [False, True], ids=["near", "far"])
@pytest.mark.parametrize("name", list(ANCHORS))
def test_every_anchor_an_alias_in_a_block_names_is_masked(name, far):
    secret = symmetric()
    template = ANCHORS[name]
    if far:
        # Move every definition line out of the window: the alias rule alone
        # must mask it.
        head, _, block = template.partition("{kty}")
        if "&" not in head:
            pytest.skip("the definition is after the kty")
        template = head + FAR + "{kty}" + block
    text = form(template, s=secret)
    assert_masked_and_audited(text, secret)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85", "\u2028", "\u2029"], ids=ascii)
@pytest.mark.parametrize("layout", ["block", "flow", "list", "chain", "explicit-key"])
@pytest.mark.parametrize("distance", [0, 24])
@pytest.mark.parametrize("source_kind", ["block", "flow-json"])
def test_yaml_merge_alias_consumers_are_masked(newline, layout, distance, source_kind, tmp_path):
    import yaml

    from unified_agent.audit.engine import run_audit
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    source = form("defaults: &kind\n  {kty}: {oct_value}\n")
    if source_kind == "flow-json":
        source = "defaults: &kind " + json.dumps({TYPE_NAME: OCT_TYPE}) + "\n"
    uses = {
        "block": "key:\n  <<: *kind\n  k: {s}",
        "flow": "key: {{<<: *kind, k: {s}}}",
        "list": "other: &other {{alg: test}}\nkey:\n  <<: [*other, *kind]\n  k: {s}",
        "chain": "middle: &middle\n  <<: *kind\nkey:\n  <<: *middle\n  k: {s}",
        "explicit-key": "key:\n  ? <<\n  : *kind\n  k: {s}",
    }
    text = (source + "pad: harmless\n" * distance + form(uses[layout], s=secret)).replace(
        "\n", newline
    )
    parsed = yaml.safe_load(text)
    assert parsed["key"][TYPE_NAME] == OCT_TYPE and parsed["key"]["k"] == secret
    out = assert_masked_and_audited(text, secret)
    if source_kind == "flow-json":
        assert json.dumps({TYPE_NAME: OCT_TYPE}) in out
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])
    (tmp_path / "fixture.txt").write_text(text)
    assert any(
        f["rule"] == "SECRETS-KEY" and f["line"] == line_of(text, secret)
        for f in run_audit(tmp_path)["findings"]
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85", "\u2028", "\u2029"], ids=ascii)
@pytest.mark.parametrize("marker", ["---", "..."])
def test_merge_alias_scope_stops_at_document_boundaries(newline, marker):
    import yaml

    secret = symmetric()
    text = form("defaults: &kind\n  {kty}: {oct_value}\nkey:\n  <<: *kind\n  k: {s}\n", s=secret)
    text += marker + "\n" + ("---\n" if marker == "..." else "")
    tail = "other: &unrelated " + KEPT + "\ncopy: *unrelated\n"
    text = (text + tail).replace("\n", newline)
    assert len(list(yaml.safe_load_all(text))) == 2
    out = assert_masked_and_audited(text, secret)
    assert out.endswith(tail.replace("\n", newline))


def test_merge_chain_arbitrary_depth_and_separate_suppression_lines(tmp_path):
    import yaml

    from unified_agent.audit.engine import run_audit

    secret = symmetric()
    text = form("defaults: &kind\n  {kty}: {oct_value}\n")
    previous = "kind"
    for index in range(24):
        current = "m" + str(index)
        text += current + ": &" + current + "\n  <<: *" + previous + "\n"
        previous = current
    text += "key:\n  <<: *" + previous + " # ua:allow\n  k: " + secret
    assert yaml.safe_load(text)["key"][TYPE_NAME] == OCT_TYPE
    assert_masked_and_audited(text, secret)
    (tmp_path / "fixture.txt").write_text(text)
    assert any(
        f["rule"] == "SECRETS-KEY" and f["line"] == line_of(text, secret)
        for f in run_audit(tmp_path)["findings"]
    )


@pytest.mark.parametrize(
    "merge_key,directive",
    [
        ("!!merge inherited", ""),
        ("!!%6derge inherited", ""),
        ("!<tag:yaml.org,2002:%6derge> inherited", ""),
        ("!m!%6derge inherited", "%TAG !m! tag:yaml.org,2002:\n---\n"),
        ("!%72ge inherited", "%TAG ! tag:yaml.org,2002:me\n---\n"),
    ],
)
def test_tagged_yaml_merge_consumers_remain_protected(merge_key, directive):
    import yaml

    secret = symmetric()
    text = directive + form("defaults: &kind\n  {kty}: {oct_value}\nkey:\n  ", s=secret)
    text += merge_key + ": *kind\n  k: " + secret
    assert yaml.safe_load(text)["key"][TYPE_NAME] == OCT_TYPE
    assert_masked_and_audited(text, secret)


@pytest.mark.parametrize("tag", ["!!merge", "!!%6derge", "!<tag:yaml.org,2002:%6derge>"])
@pytest.mark.parametrize(
    "key",
    [
        "? {tag}\n    inherited\n  : *kind",
        "?\n    {tag}\n    inherited\n  : *kind",
        "{tag} &label inherited: *kind",
        "&label {tag} inherited: *kind",
    ],
)
def test_merge_tag_context_preserves_explicit_keys_and_properties(tag, key):
    import yaml

    secret = symmetric()
    text = form("defaults: &kind\n  {kty}: {oct_value}\nkey:\n  ")
    text += key.format(tag=tag) + "\n  k: " + secret
    assert yaml.safe_load(text)["key"][TYPE_NAME] == OCT_TYPE
    assert_masked_and_audited(text, secret)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("'word'", "word"),
        (" 'word'", "word"),
        ("\t'word'", "word"),
        ("'a' 'b'", "ab"),
        ("b'word'", "'word'"),
        ("['word']", "'word']"),
    ],
)
def test_single_quoted_decoder_preserves_literal_semantics(raw, expected):
    assert jwk._string(raw) == expected


@pytest.mark.parametrize("callee", ["dict", "jwk.JWK"])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize(
    "wrapper",
    ["{obj}", "({obj})", "(\n # metadata\n ({obj})\n)", "\\\n {obj}", "(\\\r\n {obj})"],
)
@pytest.mark.parametrize("spelling", ["plain", "escaped", "upper"])
def test_literal_keyword_unpacking_protects_sibling_values(
    callee, before, wrapper, spelling, tmp_path
):
    from unified_agent.audit.engine import run_audit
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    name = TYPE_NAME if spelling != "escaped" else "\\u006bty"
    kind = OCT_TYPE.upper() if spelling == "upper" else OCT_TYPE
    obj = '{"' + name + '": "' + kind + '"}'
    unpack = "**" + wrapper.format(obj=obj)
    private = "k=" + repr(secret)
    args = [private, unpack] if before else [unpack, private]
    text = callee + "(\n " + ",\n ".join(args) + "\n)"
    ast.parse(text)
    assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])
    (tmp_path / "fixture.py").write_text(text)
    assert any(
        f["rule"] == "SECRETS-KEY" and f["line"] == line_of(text, secret)
        for f in run_audit(tmp_path)["findings"]
    )


@pytest.mark.parametrize("kind", ["RSA", "EC", "OKP", "ordinary"])
def test_non_oct_keyword_unpacking_preserves_public_siblings(kind):
    obj = json.dumps({TYPE_NAME: kind, "note": form("{kty}: {oct_value}")})
    text = "dict(**(" + obj + "), k=" + repr(KEPT) + ")"
    assert redact(text) == text


def test_python_unpacking_and_html_entities_do_not_seed_document_aliases(tmp_path):
    from unified_agent.audit.engine import run_audit

    text = form(
        'reference = "&#1234;"\ncase = {{"{kty}": "{oct_value}", "k": "REDACTED"}} # ua:allow\nresult = producer(*items)\npublic = "label"\n'
    )
    assert redact(text) == text.replace('"REDACTED"', '"' + MARK + '"')
    (tmp_path / "fixture.py").write_text(text)
    assert not any(f["rule"] == "SECRETS-KEY" for f in run_audit(tmp_path)["findings"])


@pytest.mark.parametrize("extension", ["js", "ts"])
@pytest.mark.parametrize("coercion", ["!!flag", "!!merge", "!!%6derge"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\u2028", "\u2029"], ids=ascii)
@pytest.mark.parametrize("continued", [False, True])
def test_javascript_coercion_does_not_seed_yaml_aliases(
    extension, coercion, newline, continued, tmp_path
):
    from unified_agent.audit.engine import run_audit

    text = form(
        'const reference = "&#1234;";\nconst sample = {{"{kty}": "{oct_value}", '
        '"k": "REDACTED"}}; // ua:allow\n'
    )
    text += "const ready = " + ("\n  " if continued else "") + coercion + ";\n"
    text += "const total = count *items;\nconst publicLabel = 'visible';\n"
    text = text.replace("\n", newline)
    assert redact(text) == text.replace('"REDACTED"', '"' + MARK + '"')
    (tmp_path / ("fixture." + extension)).write_text(text)
    assert not any(f["rule"] == "SECRETS-KEY" for f in run_audit(tmp_path)["findings"])


def test_existing_relay_test_source_has_no_spurious_private_key_findings(tmp_path):
    from unified_agent.audit.engine import run_audit

    path = pathlib.Path(__file__).with_name("test_public_relay.py")
    (tmp_path / path.name).write_text(path.read_text())
    assert not any(f["rule"] == "SECRETS-KEY" for f in run_audit(tmp_path)["findings"])


def test_aliases_inside_a_distant_anchored_value_are_followed():
    # Codex round 8, P2-3: an alias inside a selected anchor value pointed at
    # the real secret, which stayed visible.
    secret = symmetric()
    for value in ("[*y]", "{{v: *y}}", "!t\n  *y"):
        text = form(
            "b: &y {s}\na: &x " + value + "\n" + FAR + "{kty}: {oct_value}\nk: *x", s=secret
        )
        assert_masked_and_audited(text, secret)


@pytest.mark.parametrize("name", ["@x", "$x", "\N{ELECTRIC LIGHT BULB}", "?x", ":x", "x.y-1"])
def test_unusual_kty_alias_names_open_a_block(name):
    # Codex round 8, P2-2: alias names the narrow opener did not read.
    secret = symmetric()
    text = form("type: &" + name + " {oct_value}\n{kty}: *" + name + "\nk: {s}", s=secret)
    assert_masked_and_audited(text, secret)


LONG_NAMES = {
    "tag-before-alias": "a: &x {s}\n" + FAR + "{kty}: {oct_value}\nk: !{name} *x",
    "anchor-before-alias": "a: &x {s}\n" + FAR + "{kty}: {oct_value}\nk: &{name} *x",
    "long-anchor-name": "a: &{name} {s}\n" + FAR + "{kty}: {oct_value}\nk: *{name}",
    "kty-alias-after-a-tag": "a: &x {oct_value}\n{kty}: !{name} *x\nk: {s}",
    "kty-alias-after-an-anchor": "a: &x {oct_value}\n{kty}: &{name} *x\nk: {s}",
}


@pytest.mark.parametrize("length", [128, 129, 255, 256, 300, 10_000])
@pytest.mark.parametrize("case", list(LONG_NAMES))
def test_a_long_property_or_anchor_name_never_fails_open(case, length):
    # Codex and Devin round 6, P2: a cap on property length turned a longer tag
    # or anchor before an alias into no alias.
    secret = symmetric()
    assert_masked_and_audited(form(LONG_NAMES[case], s=secret, name="a" * length), secret)


@pytest.mark.parametrize("count", [4, 5, 6, 50])
@pytest.mark.parametrize(
    "template",
    [
        "a: &x {s}\n" + FAR + "{kty}: {oct_value}\nk: {props} *x",
        "t: &t {oct_value}\n{kty}: {props} *t\nk: {s}",
        "material: &x {s}\n" + FAR + "{kty}: {oct_value}\nk: !<" + "a" * 130 + "> *x",
        "t: &t {oct_value}\n{kty}: &a !<" + "a" * 130 + "> *t\nk: {s}",
    ],
    ids=["before-a-k-alias", "before-a-kty-alias", "verbatim-tag-k", "verbatim-tag-kty"],
)
def test_any_number_of_properties_before_an_alias_is_read(template, count):
    secret = symmetric()
    props = " ".join(f"&p{index}" for index in range(count))
    assert_masked_and_audited(form(template, s=secret, props=props), secret)


def _random_layout(rng: random.Random, secret: str) -> str:
    """A random anchor definition and an oct block aliasing it (Codex round 7)."""
    name = rng.choice(["x", "key-1", "a.b", "n" * rng.choice([1, 50, 300])])
    half = len(secret) // 2
    definition = rng.choice(
        [
            ["a: &{n} {s}"],
            ["a: &{n}", "  {s}"],
            ["a: &{n} |", "  {h1}", "  {h2}"],
            ["a: &{n} >-", "  {h1}", "", "  {h2}"],
            ["- &{n} {s}"],
            ["a: !!str &{n} {s}"],
            ["a: &{n} !tag {s}"],
            ["a: &{n}", "  # note", "  {s}"],
            ["a=&{n} {s}"],
            ["f(&{n} {s})"],
            ["a: '&{n} {s}'"],
        ]
    )
    properties = [
        rng.choice(["!t", "&other", "!!str", "!<" + "v" * rng.choice([1, 200]) + ">", "&p" * 3])
        for _ in range(rng.randint(0, 5))
    ]
    separator = rng.choice([": ", "=", " => "])
    alias_lines = ["k" + separator.rstrip()]
    alias = rng.choice(["*{n}", "[*{n}]", "{{v: *{n}}}", "(*{n})", "x, *{n}", "*y *{n}"])
    for piece in [*properties, alias]:
        if rng.random() < 0.5:
            alias_lines[-1] += " " + piece
        else:
            alias_lines.append("  " + piece)
    block = [form("{kty}: {oct_value}"), *alias_lines]
    if rng.random() < 0.3:
        block.insert(1, "# comment")
    padding = [FAR] if rng.random() < 0.5 else []
    lines = (
        [*definition, *padding, *block] if rng.random() < 0.7 else [*block, *padding, *definition]
    )
    text = "\n".join(lines).format(n=name, s=secret, h1=secret[:half], h2=secret[half:])
    if rng.random() < 0.2:
        text = text.replace("\n", "\r\n")
    return text


def test_random_alias_layouts_never_leave_the_anchored_secret_visible():
    rng = random.Random(2093)
    for _ in range(300):
        secret = symmetric()
        text = _random_layout(rng, secret)
        out = redact(text)
        half = len(secret) // 2
        for part in (secret, secret[:half], secret[half:]):
            assert part not in out, text
        flagged = flagged_lines(text)
        parts = [part for part in (secret, secret[:half]) if part in text]
        assert any(line_of(text, part) in flagged for part in parts), text


# --- The audit flags what the redactor masks --------------------------------------------

PARITY = {
    **{f"kwargs-{name}": template for name, template in KWARGS.items()},
    **{f"yaml-{name}": template for name, template in YAML.items()},
    **{f"lexical-{name}": template for name, template in LEXICAL.items()},
    **{f"anchor-{name}": template for name, template in ANCHORS.items()},
}


@pytest.mark.parametrize("name", list(PARITY))
def test_the_audit_flags_every_line_an_oct_block_masks(name):
    secret, tail = symmetric(), symmetric()
    text = form(PARITY[name], s=secret, t=tail, kty_upper=TYPE_NAME.upper())
    starts = [0]
    for row in text.splitlines(True):
        starts.append(starts[-1] + len(row))
    masked = {
        next(n for n in range(len(starts) - 1, -1, -1) if starts[n] <= member.start) + 1
        for member in jwk.private_members(text)
        if not rules._is_exact_placeholder(member.value)
    }
    assert masked, text
    assert masked <= flagged_lines(text), (text, masked - flagged_lines(text))


@pytest.mark.parametrize(
    "template,lines",
    [
        ("import jwk\nkey = jwk.JWK({kty}='{oct_value}', k='{s}')", {2}),
        ("{kty}: {oct_value}\nk: {s}\nname: demo", {2, 3}),
        # A blank line does not end a mapping (PR #1221 Codex round 1, P2-2).
        ("name: other\n\n{kty}: {oct_value}\nk: {s}\n\nafter: later", {1, 4, 6}),
        ("top:\n  {kty}: {oct_value}\n\n  k: {s}\nnext: x", {4}),
    ],
    ids=["kwargs", "yaml", "yaml-across-blank-lines", "nested-across-a-blank-line"],
)
def test_the_secrets_audit_reports_a_kwargs_or_yaml_jwk(template, lines):
    findings = list(rules.check_secrets("fixture.txt", form(template, s=symmetric())))
    assert {(f.rule, f.line) for f in findings} == {("SECRETS-KEY", n) for n in lines}


def test_the_public_relay_blocks_an_oct_block():
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    findings = pr._jwk_findings(form("{kty}: {oct_value}\nk: {s}", s=secret))
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("gap", ["\n", "# note\n", "\n  # note\n\n", "# note\n" * 40])
@pytest.mark.parametrize("comment", ["", " # key comment"])
def test_explicit_key_separation_lines_are_masked_audited_and_blocked(newline, gap, comment):
    import yaml

    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    text = form("? {kty}{comment}\n{gap}: {oct_value}\nk: {s}", gap=gap, comment=comment, s=secret)
    text = text.replace("\n", newline)
    assert yaml.safe_load(text) == {TYPE_NAME: OCT_TYPE, "k": secret}
    assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85"], ids=ascii)
@pytest.mark.parametrize("split", [0, 1, 2, 3, "both", "edges", "all"])
@pytest.mark.parametrize("spelling", ["literal", "unicode", "mixed"])
@pytest.mark.parametrize("layout", ["root", "nested", "sequence", "standalone", "tagged", "flow"])
def test_yaml_continued_key_names_are_masked_audited_and_blocked(newline, split, spelling, layout):
    import yaml

    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    letters = {
        "literal": list(TYPE_NAME),
        "unicode": [f"\\u{ord(c):04x}" for c in TYPE_NAME],
        "mixed": [TYPE_NAME[0], f"\\x{ord(TYPE_NAME[1]):02x}", TYPE_NAME[2]],
    }[spelling]
    continuation = "\\\n    "
    name = (continuation if split in (0, "edges", "all") else "") + "".join(
        (continuation if i and (split in ("both", "all") or i == split) else "") + letter
        for i, letter in enumerate(letters)
    )
    if split in (3, "edges", "all"):
        name += continuation
    templates = {
        "root": '? "{name}"\n: {oct_value} # ua:allow\nk: {s}',
        "nested": 'outer:\n  ? "{name}"\n  : {oct_value} # ua:allow\n  k: {s}',
        "sequence": '- ? "{name}"\n  : {oct_value} # ua:allow\n  k: {s}',
        "standalone": '?\n  "{name}"\n: {oct_value} # ua:allow\nk: {s}',
        "tagged": '? !!str &kind "{name}"\n: {oct_value} # ua:allow\nk: {s}',
        "flow": 'outer: {{? "{name}" : {oct_value}, k: {s}}}',
    }
    text = form(templates[layout], name=name, s=secret).replace("\n", newline)
    parsed = yaml.safe_load(text)
    if layout in ("nested", "flow"):
        parsed = parsed["outer"]
    elif layout == "sequence":
        parsed = parsed[0]
    assert parsed == {TYPE_NAME: OCT_TYPE, "k": secret}
    assert jwk.private_members(text)
    assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("position", ["middle", "leading", "trailing", "edges"])
def test_yaml_continued_key_name_actual_engine_reports_private_line(newline, position, tmp_path):
    from unified_agent.audit.engine import run_audit

    secret = "q" + secrets.token_hex(3) + "z"
    continuation = "\\\n  "
    name = {
        "middle": TYPE_NAME[:2] + continuation + TYPE_NAME[2:],
        "leading": continuation + TYPE_NAME,
        "trailing": TYPE_NAME + continuation,
        "edges": continuation + TYPE_NAME + continuation,
    }[position]
    text = form('? "{name}" # ua:allow\n: {oct_value} # ua:allow\nk: {s}', name=name, s=secret)
    text = text.replace("\n", newline)
    (tmp_path / "fixture.txt").write_bytes(text.encode())
    assert any(
        f["rule"] == "SECRETS-KEY" and f["line"] == line_of(text, secret)
        for f in run_audit(tmp_path)["findings"]
    )


@pytest.mark.parametrize("prefix,suffix", [("my", ""), ("foo.", ""), ("JWK_", ""), ("", "Extra")])
@pytest.mark.parametrize("value", [OCT_TYPE, "RSA", "EC"])
def test_continued_longer_key_names_do_not_open_oct_blocks(prefix, suffix, value):
    import yaml

    secret = "q" + secrets.token_hex(3) + "z"
    name = prefix + TYPE_NAME[:2] + "\\\n  " + TYPE_NAME[2:] + suffix
    text = '? "' + name + '"\n: ' + value + "\nk: " + secret
    assert yaml.safe_load(text) == {prefix + TYPE_NAME + suffix: value, "k": secret}
    assert not jwk.private_members(text)
    assert redact(text) == text


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85"], ids=ascii)
@pytest.mark.parametrize(
    "prefix,decoded",
    [("my", "my"), ("foo.", "foo."), ("JWK_", "JWK_"), ("$", "$"), (r"\u006d\u0079", "my")],
)
@pytest.mark.parametrize("layout", ["root", "nested"])
@pytest.mark.parametrize("repetitions", [1, 3])
def test_continued_identifier_prefix_does_not_supply_an_oct_key(
    newline, prefix, decoded, layout, repetitions
):
    import yaml

    from unified_agent.tools import public_relay as pr

    name = prefix + "\\\n    " * repetitions + TYPE_NAME
    text = form('? "{name}"\n: {oct_value}\nk: {kept}', name=name)
    if layout == "nested":
        text = "outer:\n" + "\n".join("  " + row for row in text.split("\n"))
    text = text.replace("\n", newline)
    parsed = yaml.safe_load(text)
    if layout == "nested":
        parsed = parsed["outer"]
    assert parsed == {decoded + TYPE_NAME: OCT_TYPE, "k": KEPT}
    assert not jwk.private_members(text)
    assert not flagged_lines(text)
    assert not pr._jwk_findings(text)
    assert redact(text) == text


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85"], ids=ascii)
@pytest.mark.parametrize("split", [0, 1, 2])
@pytest.mark.parametrize("layout", ["plain", "explicit", "alias-key"])
def test_yaml_continued_type_value_opens_a_block(newline, split, layout, tmp_path):
    import yaml

    from unified_agent.audit.engine import run_audit
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    continued = OCT_TYPE[:split] + "\\" + newline + "  " + OCT_TYPE[split:]
    keys = {
        "plain": TYPE_NAME + ":",
        "explicit": "? " + TYPE_NAME + "\n:",
        "alias-key": "type: &kt " + TYPE_NAME + "\n? *kt\n:",
    }
    text = (
        keys[layout].replace("\n", newline)
        + ' "'
        + continued
        + '" # ua:allow'
        + newline
        + "k: "
        + secret
    )
    assert yaml.safe_load(text)[TYPE_NAME] == OCT_TYPE
    assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])
    (tmp_path / "fixture.txt").write_text(text)
    assert any(
        f["rule"] == "SECRETS-KEY" and f["line"] == line_of(text, secret)
        for f in run_audit(tmp_path)["findings"]
    )


@pytest.mark.parametrize("position", ["leading", "middle", "trailing"])
@pytest.mark.parametrize("layout", ["root", "nested", "standalone", "flow"])
@pytest.mark.parametrize("repetitions", [20, 40])
def test_long_continued_key_token_does_not_spend_the_value_window(position, layout, repetitions):
    import yaml

    from unified_agent.tools import public_relay as pr

    continuation = "\\\n    " * repetitions
    name = {
        "leading": continuation + TYPE_NAME,
        "middle": TYPE_NAME[:1] + continuation + TYPE_NAME[1:],
        "trailing": TYPE_NAME + continuation,
    }[position]
    templates = {
        "root": '? "{name}"\n: {oct_value}\nk: {s}',
        "nested": 'outer:\n  ? "{name}"\n  : {oct_value}\n  k: {s}',
        "standalone": '?\n  "{name}"\n: {oct_value}\nk: {s}',
        "flow": 'outer: {{? "{name}" : {oct_value}, k: {s}}}',
    }
    secret = "q" + secrets.token_hex(3) + "z"
    text = form(templates[layout], name=name, s=secret)
    parsed = yaml.safe_load(text)
    if layout in ("nested", "flow"):
        parsed = parsed["outer"]
    assert parsed == {TYPE_NAME: OCT_TYPE, "k": secret}
    assert_masked_and_audited(text, secret)
    assert pr._jwk_findings(text)


def test_same_line_triggers_keep_the_complete_continued_key_window():
    # A log fragment can place an ordinary and a continued key on the same
    # first physical line; the short key's cached window cannot cut the latter.
    secret = "q" + secrets.token_hex(3) + "z"
    name = TYPE_NAME[:1] + "\\\n    " * 40 + TYPE_NAME[1:]
    text = form('{kty}: {oct_value}; ? "{name}"\n: {oct_value}\nk: {s}', name=name, s=secret)
    assert_masked_and_audited(text, secret)


def test_continued_key_keeps_the_neighboring_value_window_bounded():
    secret = "q" + secrets.token_hex(3) + "z"
    name = TYPE_NAME[:1] + "\\\n    " * 40 + TYPE_NAME[1:]
    text = form('? "{name}"\n: {oct_value}\n', name=name) + "pad: harmless\n" * 20 + "k: " + secret
    assert secret in redact(text)
    assert line_of(text, secret) not in flagged_lines(text)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85", "\u2028", "\u2029"], ids=ascii)
@pytest.mark.parametrize(
    "key", ["plain", "quoted", "escaped", "continued", "next-line", "tagged", "verbatim-tag"]
)
@pytest.mark.parametrize("layout", ["inline", "standalone", "nested", "flow"])
def test_yaml_alias_explicit_key_opens_a_block(newline, key, layout):
    import yaml

    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    scalar = {
        "plain": TYPE_NAME,
        "quoted": '"' + TYPE_NAME + '"',
        "escaped": '"' + "".join(f"\\u{ord(c):04x}" for c in TYPE_NAME) + '"',
        "continued": '"\\\n  ' + TYPE_NAME + '"',
        "next-line": "# definition\n  " + TYPE_NAME,
        "tagged": "!!str " + TYPE_NAME,
        "verbatim-tag": "!<tag:yaml.org,2002:str> " + TYPE_NAME,
    }[key]
    definition = "type: &kt " + scalar + "\n" + "pad: harmless\n" * 24
    templates = {
        "inline": "? *kt # name\n: {oct_value} # ua:allow\nk: {s}",
        "standalone": "? # name\n  *kt\n: {oct_value} # ua:allow\nk: {s}",
        "nested": "outer:\n  ? *kt\n  : {oct_value} # ua:allow\n  k: {s}",
        "flow": "outer: {{? *kt : {oct_value}, k: {s}}}",
    }
    text = (definition + form(templates[layout], s=secret)).replace("\n", newline)
    parsed = yaml.safe_load(text)
    if layout in ("nested", "flow"):
        parsed = parsed["outer"]
    assert parsed[TYPE_NAME] == OCT_TYPE and parsed["k"] == secret
    assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])


PHYSICAL_BREAKS = [
    "\n",
    "\r",
    "\r\n",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85", "\u2028", "\u2029"], ids=ascii)
@pytest.mark.parametrize("layout", ["root", "nested", "sequence", "nested-sequence"])
@pytest.mark.parametrize("gap", ["", "\n# separation\n", "# separation\n" * 40])
def test_standalone_explicit_key_uses_parent_indentation(newline, layout, gap, tmp_path):
    import yaml

    from unified_agent.audit.engine import run_audit
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    templates = {
        "root": "?\n{gap}  {kty} # ua:allow\n{gap}: {oct_value}\nk: {s}",
        "nested": "outer:\n  ? # marker\n{gap}    {kty} # ua:allow\n{gap}  : {oct_value}\n  k: {s}",
        "sequence": "- ? # marker\n{gap}    {kty} # ua:allow\n{gap}  : {oct_value}\n  k: {s}",
        "nested-sequence": "outer:\n  - ? # marker\n{gap}      {kty} # ua:allow\n{gap}    : {oct_value}\n    k: {s}",
    }
    text = form(templates[layout], gap=gap, s=secret).replace("\n", newline)
    parsed = yaml.safe_load(text)
    if layout.startswith("nested"):
        parsed = parsed["outer"]
    if "sequence" in layout:
        parsed = parsed[0]
    assert parsed == {TYPE_NAME: OCT_TYPE, "k": secret}
    assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])
    (tmp_path / "fixture.txt").write_text(text)
    report = run_audit(tmp_path)
    assert any(
        f["rule"] == "SECRETS-KEY" and f["line"] == line_of(text, secret)
        for f in report["findings"]
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85", "\u2028", "\u2029"], ids=ascii)
@pytest.mark.parametrize(
    "header",
    [
        "|-",
        ">-",
        "!!str",
        "&kind",
        "!!str |-",
        "&kind |-",
        "!!str &kind |2-",
        "&kind !!str |-2",
        "!<tag:yaml.org,2002:str>",
    ],
)
@pytest.mark.parametrize("layout", ["root", "nested", "sequence", "nested-sequence"])
def test_property_bearing_explicit_key_keeps_parent_indentation(newline, header, layout, tmp_path):
    import yaml

    from unified_agent.audit.engine import run_audit
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    templates = {
        "root": "? {header} # ua:allow\n  {kty} # key\n: {oct_value} # ua:allow\nk: {s}",
        "nested": "outer:\n  ? {header} # ua:allow\n    {kty} # key\n  : {oct_value} # ua:allow\n  k: {s}",
        "sequence": "- ? {header} # ua:allow\n    {kty} # key\n  : {oct_value} # ua:allow\n  k: {s}",
        "nested-sequence": "outer:\n  - ? {header} # ua:allow\n      {kty} # key\n    : {oct_value} # ua:allow\n    k: {s}",
    }
    # Block-scalar content includes a literal '#', unlike a plain key's comment.
    template = templates[layout]
    if "|" in header or ">" in header:
        template = template.replace("{kty} # key", "{kty}")
    text = form(template, header=header, s=secret).replace("\n", newline)
    parsed = yaml.safe_load(text)
    if layout.startswith("nested"):
        parsed = parsed["outer"]
    if "sequence" in layout:
        parsed = parsed[0]
    assert parsed == {TYPE_NAME: OCT_TYPE, "k": secret}
    assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])
    (tmp_path / "fixture.txt").write_text(text)
    assert any(
        f["rule"] == "SECRETS-KEY" and f["line"] == line_of(text, secret)
        for f in run_audit(tmp_path)["findings"]
    )


@pytest.mark.parametrize("properties", ["!!str\n  &kind", "&kind\n  !!str"])
def test_explicit_key_properties_on_separate_lines(properties):
    import yaml

    secret = symmetric()
    text = form(
        "?\n  {properties}\n  {kty}\n: {oct_value}\nk: {s}", properties=properties, s=secret
    )
    assert yaml.safe_load(text) == {TYPE_NAME: OCT_TYPE, "k": secret}
    assert_masked_and_audited(text, secret)


@pytest.mark.parametrize("newline", PHYSICAL_BREAKS, ids=ascii)
def test_oct_audit_suppression_stays_on_its_physical_line(newline, tmp_path):
    from unified_agent.audit.engine import run_audit
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    text = form("{kty}: {oct_value} # ua:allow{newline}k: {s}", newline=newline, s=secret)
    assert 2 in flagged_lines(text)
    assert_masked(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    assert secret not in json.dumps([f.to_dict() for f in findings])
    (tmp_path / "fixture.txt").write_text(text)
    report = run_audit(tmp_path)
    assert any(f["rule"] == "SECRETS-KEY" and f["line"] == 2 for f in report["findings"])


@pytest.mark.parametrize("newline", PHYSICAL_BREAKS, ids=ascii)
@pytest.mark.parametrize("layout", ["lookahead", "explicit", "escaped-break", "anchor-doc"])
def test_oct_line_model_covers_lookahead_escapes_and_document_boundaries(newline, layout):
    secret = symmetric()
    templates = {
        "lookahead": "{kty}:\n# comment\n\n  {oct_value}\nk: {s}",
        "explicit": "? {kty}\n# comment\n\n: {oct_value}\nk: {s}",
        "escaped-break": "{kty}: {oct_value} # ua:allow\\\nk: {s}",
        "anchor-doc": "data: &x {s}\n" + FAR + "{kty}: {oct_value}\nk: *x\n---\nnext: {kept}",
    }
    text = form(templates[layout], s=secret).replace("\n", newline)
    out = assert_masked_and_audited(text, secret)
    if layout == "anchor-doc":
        assert out.endswith("---" + newline + "next: " + KEPT)


@pytest.mark.parametrize("newline", ["\x85", "\u2028", "\u2029"], ids=ascii)
@pytest.mark.parametrize("tail", [", more", "; more", " [more]", " (more)"])
def test_physical_breaks_in_json_strings_keep_quotes_and_keys(newline, tail):
    secret = symmetric()
    content = form("{kty}: {oct_value}{newline}k: {s}{tail}", newline=newline, s=secret, tail=tail)
    original = {"log": content, "rest": {"key": "value"}}
    text = json.dumps(original, ensure_ascii=False)
    out = assert_masked(text, secret)
    assert _same_shape(_shape(original), _shape(json.loads(out)))
    # A physical break can also occur inside a JSON member name. That name
    # must stay intact even when its text happens to open an oct block.
    original = {content: "ordinary", "rest": {"key": "value"}}
    out = redact(json.dumps(original, ensure_ascii=False))
    assert _same_shape(_shape(original), _shape(json.loads(out)))


@pytest.mark.parametrize("newline", PHYSICAL_BREAKS, ids=ascii)
def test_comment_quotes_cannot_hide_a_later_secret(newline, tmp_path):
    from unified_agent.audit.engine import run_audit

    secret = symmetric()
    text = form('{kty}: {oct_value} # " ua:allow\nk: {s}\nfoo": bar # ua:allow\n', s=secret)
    text = text.replace("\n", newline)
    assert_masked_and_audited(text, secret)
    (tmp_path / "fixture.txt").write_text(text)
    report = run_audit(tmp_path)
    assert any(f["rule"] == "SECRETS-KEY" and f["line"] == 2 for f in report["findings"])


@pytest.mark.parametrize("newline", ["\x85", "\u2028", "\u2029"], ids=ascii)
@pytest.mark.parametrize("as_key", [False, True])
def test_quote_context_before_the_window_keeps_long_json_valid(newline, as_key):
    secret = symmetric()
    content = ("padding" + newline) * 40 + form(
        "{kty}: {oct_value}{nl}k: {s}", nl=newline, s=secret
    )
    original = {content: "ordinary"} if as_key else {"log": content}
    text = json.dumps(original, ensure_ascii=False)
    out = redact(text) if as_key else assert_masked(text, secret)
    assert _same_shape(_shape(original), _shape(json.loads(out)))


@pytest.mark.parametrize("newline", ["\x85", "\u2028", "\u2029"], ids=ascii)
@pytest.mark.parametrize("padding", [0, 40])
def test_disconnected_json_pieces_keep_member_names(newline, padding):
    original = {
        "a": newline,
        "{(x " + TYPE_NAME + ": " + OCT_TYPE + ")" + newline: "v",
        "padding": "padding" + newline * padding,
    }
    out = redact(json.dumps(original, ensure_ascii=False))
    assert _same_shape(_shape(original), _shape(json.loads(out)))


# --- JSON structure survives -------------------------------------------------------------


def _shape(value):
    if isinstance(value, dict):
        return ("dict", {key: _shape(item) for key, item in value.items()})
    if isinstance(value, list):
        return ("list", [_shape(item) for item in value])
    return type(value).__name__


def _same_shape(before, after) -> bool:
    """Same keys and containers; a masked leaf may have become a string."""
    if isinstance(before, tuple) and isinstance(after, tuple):
        if before[0] != after[0] or len(before[1]) != len(after[1]):
            return False
        if before[0] == "dict":
            return before[1].keys() == after[1].keys() and all(
                _same_shape(before[1][key], after[1][key]) for key in before[1]
            )
        return all(_same_shape(a, b) for a, b in zip(before[1], after[1], strict=True))
    return before == after or after == "str"


def _test_literals() -> list[str]:
    literals = []
    for path in sorted(pathlib.Path(__file__).parent.glob("**/*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        literals.extend(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    return literals


def _generated_json(rng: random.Random) -> list[str]:
    """JSON texts with JWK forms, and innocent kty, oct and k text, in fields."""
    secret = symmetric()
    forms = [
        form(t, s=secret, t=secret, kty_upper=TYPE_NAME.upper())
        for t in (*KWARGS.values(), *YAML.values(), *LEXICAL.values(), *ANCHORS.values())
    ]
    innocent = {
        "title": "k: metric label",
        "notes": form("investigate {kty}: {oct_value} handling"),
        "tags": ["k=v", form("{kty}={oct_value}"), {"k": KEPT, "q": 1}],
    }
    pieces = [
        form("{kty}: {oct_value}"),
        form("{kty}={oct_value}"),
        form('"{kty}": "{oct_value}"'),
        "k: v",
        "k=v",
        '"k": "v"',
        "&x v",
        "*x",
        "a, b",
        "(x",
        "y)",
        "[",
        "}",
        "\\",
        '"',
        "'",
        "#",
        "\n",
        "\r\n",
        "\t",
        "\x85",
        "\u2028",
        "\u2029",
        "padding\u2028" * 40,
        "é ✓",
        "\\u0022",
        secret,
    ]
    texts = []
    for indent in (None, 2, "\t"):
        texts.append(json.dumps({"task": innocent}, indent=indent))
        texts.append(json.dumps({"log": forms, "task": innocent}, indent=indent))
        texts.append(
            json.dumps({TYPE_NAME: OCT_TYPE, "note": "k: " + secret, "n": 1}, indent=indent)
        )
        texts.append(json.dumps({TYPE_NAME: [OCT_TYPE], "k": 12345, "x": [1, {"y": 2}]}))
        texts.append(json.dumps({"a": "&x", "log": form("{kty}: {oct_value} *missing")}))
        texts.append(json.dumps({TYPE_NAME: OCT_TYPE, "a": "&x", "k": "*x"}, indent=indent))
        texts.extend(json.dumps({"form": item}, indent=indent) for item in forms)
    for _ in range(400):

        def leaf() -> object:
            roll = rng.random()
            if roll < 0.6:
                return "".join(rng.choice(pieces) for _ in range(rng.randint(1, 8)))
            if roll < 0.75:
                return rng.choice([0, -1, 3.5, True, False, None])
            if roll < 0.9:
                return json.dumps({TYPE_NAME: rng.choice([OCT_TYPE, "RSA"]), "k": secret})
            return form(rng.choice(list(YAML.values())), s=secret, t=secret)

        def value(depth: int) -> object:
            if depth > 3 or rng.random() < 0.4:
                return leaf()
            if rng.random() < 0.5:
                return [value(depth + 1) for _ in range(rng.randint(0, 4))]
            keys = ["k", TYPE_NAME, "kid", "log", "a b", "x:y", "&x", "*x"]
            return {
                rng.choice(keys) + rng.choice(["", str(i)]): value(depth + 1)
                for i in range(rng.randint(0, 4))
            }

        texts.append(
            json.dumps(
                value(0),
                indent=rng.choice([None, 1, 2, "\t"]),
                ensure_ascii=rng.random() < 0.5,
                separators=rng.choice([None, (",", ":")]),
            )
        )
    return texts


def test_redacted_json_stays_valid_with_the_same_structure():
    # Codex rounds 3 and 8, P2: a JSON packet must still parse after redaction,
    # with the same keys and containers. Every JSON-valid literal in tests/ and
    # several hundred generated texts.
    corpus = []
    for literal in _test_literals():
        if literal[:1] in "{[":
            try:
                json.loads(literal)
            except ValueError:
                continue
            corpus.append(literal)
    corpus_literals = len(corpus)
    corpus.extend(_generated_json(random.Random(2093)))
    assert corpus_literals > 100 and len(corpus) - corpus_literals >= 500
    for text in corpus:
        out = redact(text)
        after = json.loads(out)
        assert _same_shape(_shape(json.loads(text)), _shape(after)), (text[:200], out[:200])


def test_a_team_snapshot_packet_with_kty_and_k_text_survives(monkeypatch):
    # Codex round 3, P2-2: _bounded_packet raised JSONDecodeError on the
    # redacted packet.
    from unified_agent import team_snapshot
    from unified_agent.lifecycle import packet

    secret = symmetric()
    built = {
        "task": {"id": 2093, "title": "k: metric label", "notes": form("{kty}: {oct_value}")},
        "jwk_note": {"config": form("{kty}: {oct_value}\nk: {s}", s=secret)},
    }
    monkeypatch.setattr(packet, "build_session_packet", lambda task_id: built)
    bounded = team_snapshot._bounded_packet(2093)
    assert bounded is not None and bounded.keys() == built.keys()
    assert bounded["task"].keys() == built["task"].keys()
    assert secret not in json.dumps(bounded)


# --- Text with no kty is untouched ---------------------------------------------------------


def test_text_with_no_kty_takes_the_json_path_unchanged():
    # Byte-for-byte parity with main for every test literal with no kty is
    # checked against main's own modules by scripts/redaction_corpus_diff.py;
    # here, that no such literal reaches the oct-block rule at all.
    checked = 0
    for literal in _test_literals():
        if "kty" in literal.lower() or "\\" in literal:
            continue
        assert jwk.private_members(literal) == jwk._json_members(literal, 0)[0]
        checked += 1
    assert checked > 10_000


# --- Linear time and memory ------------------------------------------------------------------


def _nested_unpack_corpus(n: int) -> str:
    obj = json.dumps({TYPE_NAME: OCT_TYPE})
    for _ in range(3):
        obj = "{**(" + obj + ")}"
    return ("dict(**(" + obj + "), k='v')\n") * n


def _nested_triple_corpus(n: int) -> str:
    body = ("**" + repr({TYPE_NAME: OCT_TYPE}) + "\n") * n
    snippet = "{" + repr(TYPE_NAME) + ":'RSA','note':\"\"\"" + body
    return "'''" + snippet + '""",\'k\':' + repr(KEPT) + "}'''"


GROWING = {
    "many-prefixed-kwargs": lambda n: form("dict({kty}='{oct_value}', k=br'v')\n") * n,
    "many-prefixed-call-values": lambda n: (
        form("dict({kty}='{oct_value}', k=[") + "r'v', b'v', f'v', t'v', " * n + "])"
    ),
    "encoded-empty-prefixed-values": lambda n: json.dumps(
        {"log": form("dict({kty}='{oct_value}', **{{}}, k=[") + "r'', " * n + "])"}
    ),
    "encoded-empty-prefix-values-without-braces": lambda n: json.dumps(
        {"log": form("dict({kty}='{oct_value}', k=[") + "r'', " * n + "])"}
    ),
    "prefixed-private-colon-decoys": lambda n: (
        form("dict({kty}='{oct_value}', note=r'") + "{field:value} " * n + "')"
    ),
    "many-sibling-unpack-operators": lambda n: (
        form("dict({kty}='{oct_value}', ") + "**{}, " * n + ")"
    ),
    "continued-type-suffix-run": lambda n: (
        TYPE_NAME + ': "' + OCT_TYPE + "\\\n " * n + 'agon"\nk: value\n'
    ),
    "many-continued-type-suffixes": lambda n: (
        (TYPE_NAME + ': "' + OCT_TYPE + '\\\n  agon"\nk: value\n') * n
    ),
    "nested-literal-unpacks": _nested_unpack_corpus,
    "physical-multiline-triple-unpacks": lambda n: (
        "'''" + ("{**" + repr({TYPE_NAME: OCT_TYPE}) + ",'k':'v'}\n") * n + "'''"
    ),
    "nested-triple-unpack-decoys": _nested_triple_corpus,
    "encoded-newline-triple-unpacks": lambda n: (
        "'''" + (r"# comment\n{**" + repr({TYPE_NAME: OCT_TYPE}) + r",'k':'v'}\n") * n + "'''"
    ),
    "triple-quoted-private-unpacks": lambda n: (
        "{"
        + repr(TYPE_NAME)
        + ":'RSA','note':'''["
        + ("{**" + repr({TYPE_NAME: OCT_TYPE}) + ", 'k':'v'},") * n
        + "]''','k':"
        + repr(KEPT)
        + "}"
    ),
    "triple-quoted-unpack-decoys": lambda n: (
        "{"
        + repr(TYPE_NAME)
        + ":'RSA','note':'''"
        + ("**" + repr({TYPE_NAME: OCT_TYPE}) + "\n") * n
        + "''','k':"
        + repr(KEPT)
        + "}"
    ),
    "many-oct-blocks": lambda n: form("{kty}: {oct_value}\nk: a\nkid: b\n\n") * (n // 4),
    "dense-k-lines": lambda n: (form("{kty}: {oct_value}\n") + "k: a\n" * 40) * (n // 40),
    "deepening-lines": lambda n: (
        ("".join(" " * i + "k: a\n" for i in range(40)) + form("{kty}: {oct_value}\n")) * (n // 40)
    ),
    "many-kwargs-lines": lambda n: form("dict({kty}='{oct_value}', k='a', kid='b')\n") * (n // 4),
    "one-long-line": lambda n: form("{kty}: {oct_value} ") + "k: value, " * n,
    "one-json-string": lambda n: json.dumps(
        {"log": form("{kty}: {oct_value} ") + "k: value, " * n, "after": 1}
    ),
    "many-k-in-one-call": lambda n: form("{kty}={oct_value}\nf(") + "k=v " * n + ")",
    "member-dense-line": lambda n: form("{kty}: {oct_value} ") + "'k: a', " * n,
    "many-kty-on-one-line": lambda n: form("{kty}: {oct_value} ") * n,
    "kty-in-one-comment": lambda n: form("{kty}: # ") + form("{kty}: # ") * n + "\n",
    "kty-lookahead-runs": lambda n: (
        (form("{kty}:\n") + "# c\n" * 8 + OCT_TYPE + "\nk: a\n\n") * (n // 10)
    ),
    "deep-nesting": lambda n: form("({kty}={oct_value}, ") * n + "k=v" + ")" * n,
    "deep-nesting-unclosed": lambda n: form("[{kty}={oct_value}, ") * n,
    "unclosed-brackets-above": lambda n: (
        ("(" * 50 + "\n" + form("{kty}: {oct_value}\nk: v\n")) * (n // 50)
    ),
    "dense-aliases": lambda n: (
        (form("a: &x v\n{kty}: {oct_value}\n") + "k: *x\n" * 12 + "\n") * (n // 10)
    ),
    "anchors-on-one-line": lambda n: form("{kty}: {oct_value}\nf(") + "&a v " * n + ")\nk: *a",
    "many-anchors-few-aliases": lambda n: (
        "".join(f"a{i}: &n{i} v\n" for i in range(n // 4))
        + form("{kty}: {oct_value}\n")
        + "".join(f"k: *n{i}\n" for i in range(12))
    ),
    "many-anchors-unknown-alias": lambda n: (
        "".join(f"a{i}: &n{i} v\n" for i in range(n // 4)) + form("{kty}: {oct_value}\nk: *zz\n")
    ),
    "alias-chain": lambda n: (
        "".join(f"a{i}: &n{i} [*n{i + 1}]\n" for i in range(n // 4))
        + "\n" * 2
        + form("{kty}: {oct_value}\nk: *n0\n")
    ),
    "no-space-anchor-fields": lambda n: form("{kty}={oct_value}\nf(") + "k=&x," * n + ")",
    "no-space-tag-fields": lambda n: form("{kty}={oct_value}\nf(") + "k=!x," * n + ")",
    "long-property-run": lambda n: form("a: &x v\n{kty}: {oct_value}\nk: ") + "!t " * n + "*x\n",
    "one-long-property": lambda n: form("{kty}: !") + "p" * (5 * n) + " " + OCT_TYPE + "\n",
    "crlf-blocks": lambda n: form("{kty}: {oct_value}\r\nk: v\r\n---\r\n") * (n // 2),
    "long-indents": lambda n: (
        (" " * 200 + form("{kty}: {oct_value}\n") + " " * 200 + "k: v\n") * (n // 50)
    ),
    "escapes": lambda n: form('"k\\u0074y": {oct_value}\n"\\u006b": a\\\\"\\n\n') * (n // 4),
    "json-wrapped-log": lambda n: json.dumps(
        {"log": form("{kty}={oct_value}\nf(") + "k=&x," * n + ")", "after": 1}
    ),
    "json-packets": lambda n: (
        (
            json.dumps({"title": "k: label", "notes": form("{kty}: {oct_value}"), "t": ["k=v", 1]})
            + "\n"
        )
        * (n // 6)
    ),
    "long-anchored-block-scalar": lambda n: (
        "a: &x |\n" + "  v\n" * n + "\n" + FAR + form("{kty}: {oct_value}\nk: *x\n")
    ),
    "nested-anchors-unknown-alias": lambda n: (
        "".join(" " * i + f"a{i}: &n{i}\n" for i in range(40)) * (n // 40)
        + form("{kty}: {oct_value}\nk: *zz\n")
    ),
    "long-node-under-nested-anchors": lambda n: (
        "".join(" " * i + f"a{i}: &n{i}\n" for i in range(40))
        + (" " * 41 + "v\n") * n
        + FAR
        + form("{kty}: {oct_value}\nk: *zz\n")
    ),
    "blank-lines-between-members": lambda n: (
        (form("{kty}: {oct_value}\n") + "\n" * 40 + "k: v\n" + "\n" * 40) * (n // 80)
    ),
    "kty-in-one-comment-then-comment-run": lambda n: (
        form("{kty}: # ") * n + "\n" + "# c\n" * n + OCT_TYPE + "\n"
    ),
    "escaped-values": lambda n: form("dict({kty}='{oct_value}', k='") + "\\x41" * n + "')\n",
    "escaped-separators": lambda n: json.dumps(
        {"log": (TYPE_NAME + "\\u003a " + OCT_TYPE + "\\nk\\u003a v ") * (n // 10)}
    ),
    # PR #1221 round 2, P2-1: kty on many distinct comment lines walked the
    # whole remaining comment run each (quadratic in both the lookahead and the
    # line window).
    "kty-on-every-comment-line": lambda n: ("# " + TYPE_NAME + ":\n") * n + "x: 1\n",
    "kty-comment-lines-then-comment-run": lambda n: (
        ("# " + TYPE_NAME + ": #\n") * n + "# c\n" * n + OCT_TYPE + "\nk: value\n"
    ),
    "oct-kty-on-every-comment-line": lambda n: form("# {kty}: {oct_value}\n") * n + "k: value\n",
    "escapes-and-quotes-in-values": lambda n: (
        form("dict({kty}='{oct_value}', k='") + '\\nab\\"cd:\\x41' * (n // 4) + "')\n"
    ),
    # Desktop_Agent2 review at 18d6d796: long multi-line flow anchors, many
    # flow anchors, a long property run before one, and an unclosed one.
    "long-flow-anchor": lambda n: (
        "data: &x [\n"
        + "v,\n" * n
        + "]\n"
        + FAR
        + ALIASED_FAR.format(kty=TYPE_NAME, oct_value=OCT_TYPE)
    ),
    "many-flow-anchors": lambda n: (
        "".join(f"a{i}: &x [\nv\n]\n" for i in range(n // 4)) + FAR + form(ALIASED_FAR)
    ),
    "property-run-before-flow": lambda n: (
        "data: " + "&p " * n + "[\nv\n]\n" + form("{kty}: {oct_value}\nk: *p\n")
    ),
    "nested-flow-anchors": lambda n: (
        "data: " + "&x [" * (n // 2) + "v" + "]" * (n // 2) + "\n" + FAR + form(ALIASED_FAR)
    ),
    "unclosed-quote-anchor": lambda n: 'data: &x "\n' + "line\n" * n + FAR + form(ALIASED_FAR),
    # PR #1221 round 4: repeated unclosed documents (Codex r4, P2-3), and many
    # anchors across many documents with known and unknown aliases.
    "repeated-unclosed-documents": lambda n: (
        "a: &x [\nv\n---\n" * (n // 3) + form("{kty}: {oct_value}\nk: *x\n")
    ),
    "many-anchors-many-documents": lambda n: (
        "".join(f"a{i}: &n{i} v\nb{i}: &m{i} [w]\n---\n" for i in range(n // 10))
        + form("{kty}: {oct_value}\n")
        + "".join(f"k: *n{i}\n" for i in range(0, n // 10, 7))
    ),
    "many-documents-unknown-alias": lambda n: (
        "a: &x v\nb: w\n---\n" * (n // 5) + form("{kty}: {oct_value}\nk: *zz\n")
    ),
    "blank-line-runs": lambda n: (
        (form("{kty}: {oct_value}\nk: |\n") + "\n" * 30 + "  v\n") * (n // 30)
    ),
    "explicit-key-separation-run": lambda n: (
        form("? {kty}\n") + "# comment\n\n" * n + form(": {oct_value}\nk: v\n")
    ),
    "explicit-keys-on-every-comment-line": lambda n: (
        ("# " + TYPE_NAME + "\n") * n + "# comment\n" * n + form(": {oct_value}\nk: v\n")
    ),
    "many-merge-consumers": lambda n: (
        form("defaults: &kind\n  {kty}: {oct_value}\n") + "key:\n  <<: *kind\n  k: value\n" * n
    ),
    "many-documents-with-merges": lambda n: (
        form("defaults: &kind\n  {kty}: {oct_value}\nkey:\n  <<: *kind\n  k: value\n---\n") * n
    ),
    "long-merge-chain": lambda n: (
        form("defaults: &kind\n  {kty}: {oct_value}\n")
        + "".join(
            f"m{i}: &m{i}\n  <<: *{'kind' if not i else 'm' + str(i - 1)}\n" for i in range(n)
        )
        + f"key:\n  <<: *m{n - 1}\n  k: value\n"
    ),
    "continued-oct-values": lambda n: TYPE_NAME + ': "oc' + "\\\n " * n + 't"\nk: value\n',
    "continued-key-names": lambda n: (
        '? "' + TYPE_NAME[:1] + "\\\n " * n + TYPE_NAME[1:] + '": ' + OCT_TYPE + "\nk: value\n"
    ),
    "continued-key-near-misses": lambda n: (
        ('? "' + TYPE_NAME[:2] + "\\\n " + TYPE_NAME[2:] + 'extra": value\n') * n
    ),
    "continued-key-trailing-run": lambda n: (
        '? "' + TYPE_NAME + "\\\n " * n + '": ' + OCT_TYPE + "\nk: value\n"
    ),
    "continued-key-prefix-run": lambda n: (
        '? "my' + "\\\n " * n + TYPE_NAME + '": ' + OCT_TYPE + "\nk: value\n"
    ),
    "many-continued-key-prefixes": lambda n: (
        ('? "my\\\n  ' + TYPE_NAME + '": ' + OCT_TYPE + "\nk: value\n") * n
    ),
    "many-continued-key-names": lambda n: (
        ('? "' + TYPE_NAME[:1] + "\\\n  " + TYPE_NAME[1:] + '"\n: ' + OCT_TYPE + "\nk: value\n") * n
    ),
    "aliased-key-property-run": lambda n: (
        "type: " + "&p " * n + TYPE_NAME + "\n? *p\n: " + OCT_TYPE + "\nk: value\n"
    ),
    "aliased-key-comment-run": lambda n: (
        "type: &p\n" + "# c\n" * n + TYPE_NAME + "\n? *p\n: " + OCT_TYPE + "\nk: value\n"
    ),
    "many-aliased-keys": lambda n: (
        "type: &p " + TYPE_NAME + "\n" + ("? *p\n: " + OCT_TYPE + "\nk: value\n") * n
    ),
    "unicode-physical-lines": lambda n: form("{kty}: {oct_value}\u2028k: value\u2029") * n,
    "unicode-long-string-window": lambda n: json.dumps(
        {"log": "padding\u2028" * n + form("{kty}: {oct_value}\u2028k: value")}, ensure_ascii=False
    ),
    "formatted-deep-expression": lambda n: (
        form("dict({kty}='{oct_value}', k=f\"{{") + "(" * n + "value" + ")" * n + '}")'
    ),
    "formatted-many-nested-literals": lambda n: (
        form("dict({kty}='{oct_value}', k=f\"") + "{b'private'}" * n + '")'
    ),
    "formatted-repeated-same-quote": lambda n: (
        form("dict({kty}='{oct_value}', k=f\"") + '{f"{private}"}' * n + '")'
    ),
    "public-objects-in-formatted-oct": lambda n: (
        form("dict({kty}='{oct_value}', k=f\"{{'private' if {{'{kty}':'RSA'}} else ''}}\")\n") * n
    ),
    "public-objects-in-triple-oct": lambda n: (
        form("dict({kty}='{oct_value}', k=\"\"\"private {{'{kty}':'RSA'}}\"\"\")\n") * n
    ),
    "many-public-objects-one-oct-string": lambda n: (
        form('dict({kty}=\'{oct_value}\', k=f"""')
        + ("{'private' if {" + repr(TYPE_NAME) + ":'RSA'} else ''}") * n
        + '""")'
    ),
    "compound-before-literals": lambda n: (
        form("dict({kty}='{oct_value}', k=prefix+b'private')\n") * n
    ),
    "compound-after-literals": lambda n: (
        form("dict({kty}='{oct_value}', k=b'private' + suffix)\n") * n
    ),
    "compound-long-literal-chain": lambda n: (
        form("dict({kty}='{oct_value}', k=b'private'") + "+middle+b'private'" * n + ")"
    ),
    "compound-structured-prefix": lambda n: (
        form("dict({kty}='{oct_value}', k=f(x) + b'private')\n") * n
    ),
    "compound-decided-child-prefix": lambda n: (
        form("dict({kty}='{oct_value}', k=f({{'{kty}':'RSA','public':1}}) + prefix + b'private')\n")
        * n
    ),
    "compound-long-call-chain": lambda n: (
        form("dict({kty}='{oct_value}', k=f(x)") + "+b'private'+g(y)" * n + ")"
    ),
    "bare-compound-tails": lambda n: form("dict({kty}='{oct_value}', k=f(x).private + tail)\n") * n,
    "bare-compound-long-chain": lambda n: (
        form("dict({kty}='{oct_value}', k=f(x)") + ".private(y) + g(z)" * n + ")"
    ),
    "bare-compound-public-child": lambda n: (
        form("dict({kty}='{oct_value}', k=f({{'{kty}':'RSA'}}).private + tail)\n") * n
    ),
    "compound-collection-operands": lambda n: (
        form("dict({kty}='{oct_value}', claims=base | {{'aud': private}})\n") * n
    ),
    "compound-collection-long-chain": lambda n: (
        form("dict({kty}='{oct_value}', claims=base") + " | {'aud': private}" * n + ")"
    ),
    "compound-decided-right-boundary": lambda n: (
        form("dict({kty}='{oct_value}', k=private | {{'{kty}':'RSA','public':1}})\n") * n
    ),
    "compound-conditional-lambda-boundary": lambda n: (
        form("dict({kty}='{oct_value}', k=(private if cond else lambda: private))\n") * n
    ),
    "exponentiation-oct-decoys": lambda n: (
        form("dict(note=x ** {{'{kty}':'{oct_value}'}}, k='public')\n") * n
    ),
    "ordinary-slice-values": lambda n: (
        form("dict({kty}='{oct_value}', k=REDACTED['private':])\n") * n
    ),
    "ordinary-slice-long-chain": lambda n: (
        form("dict({kty}='{oct_value}', k=REDACTED") + "[private:]" * n + ")"
    ),
    "lambda-many-defaults": lambda n: (
        form("dict({kty}='{oct_value}', k=(lambda arg=f(private): arg))\n") * n
    ),
    "lambda-dense-parameters": lambda n: (
        form("dict({kty}='{oct_value}', k=(lambda ") + "x," * n + "last: private))"
    ),
    "comprehension-many-values": lambda n: (
        form("dict({kty}='{oct_value}', k=[f(private) for x in xs])\n") * n
    ),
    "comprehension-long-clause-chain": lambda n: (
        form("dict({kty}='{oct_value}', k=[f(private)") + " for x in xs" * n + "])"
    ),
    "comprehension-public-child": lambda n: (
        form("dict({kty}='{oct_value}', k=[f({{'{kty}':'RSA'}},private) for x in xs])\n") * n
    ),
    "comprehension-computed-target": lambda n: (
        form("dict({kty}='{oct_value}', k=[f(private) for obj[private] in xs])\n") * n
    ),
    "comprehension-large-binding-tuple": lambda n: (
        form("dict({kty}='{oct_value}', k=[f(private) for (") + "x," * n + ") in xs])"
    ),
    "comprehension-long-binding-attribute": lambda n: (
        form("dict({kty}='{oct_value}', k=[f(private) for obj") + ".attr" * n + " in xs])"
    ),
    "standalone-explicit-key-separation": lambda n: (
        "? # marker\n" + "# comment\n\n" * n + form("  {kty}\n: {oct_value}\nk: value\n")
    ),
}


@pytest.mark.parametrize("name", list(GROWING))
def test_the_oct_block_scan_grows_linearly(name):
    # A fixed time budget at one size proves nothing about growth (Codex round
    # 1, P3-5). Four times the input must cost well under sixteen times as much
    # (quadratic); linear is about four. CPU time, best of interleaved samples,
    # so runner load cannot decide the ratio (task #2188).
    small, large = GROWING[name](2_000), GROWING[name](8_000)
    assert len(small) >= 2_000 and len(large) >= 3.5 * len(small)
    assert_cpu_growth(cold(redact), small, large, limit=8, label=name)
    # No overlapping copies: the masked text is at most the input.
    assert sum(len(member.value) for member in jwk.private_members(large)) <= len(large)


#: Peak scan memory: a fixed cost per input byte (line offsets, the bracket
#: stack, cached line facts) and per masked value (its member object and copy).
PEAK_PER_BYTE = 48
PEAK_PER_VALUE = 400


@pytest.mark.parametrize("name", list(GROWING))
def test_the_scan_peak_memory_follows_its_cost_model(name):
    small, large = GROWING[name](2_000), GROWING[name](8_000)
    extra = 0
    if name.startswith("json-wrapped") or name == "one-json-string":
        extra = len(jwk.private_members(json.loads(large)["log"]))

    def budget(found: list) -> int:
        return PEAK_PER_BYTE * len(large) + PEAK_PER_VALUE * (len(found) + extra)

    # Garbage collected first and re-measured before failing (task #2188).
    growth = peak_growth(cold(jwk.private_members), small, large, limit=8, budget=budget)
    members = len(growth.result) + extra
    assert growth.large < budget(growth.result), (
        f"{name}: peak {growth.large / len(large):.1f} bytes per input byte, {members} members"
    )
    assert growth.ratio < 8, f"{name}: x4 input took x{growth.ratio:.1f}"


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 16, 64])
@pytest.mark.parametrize("callee", ["dict", "jwk.JWK"])
@pytest.mark.parametrize("before", [False, True])
def test_nested_literal_unpacks_propagate_oct_to_call(depth, callee, before):
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    obj = json.dumps({TYPE_NAME: OCT_TYPE})
    for _ in range(depth):
        obj = "{**(" + obj + ")}"
    arguments = ["**(" + obj + ")", "k=" + repr(secret)]
    if before:
        arguments.reverse()
    text = callee + "(\n " + ",\n ".join(arguments) + "\n)"
    ast.parse(text)
    masked = secret not in redact(text)
    assert masked, "nested literal unpack leaves private sibling visible"
    assert line_of(text, secret) in flagged_lines(text)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    sanitized = secret not in json.dumps([f.to_dict() for f in findings])
    assert sanitized


@pytest.mark.parametrize("layout", ["fields", "comment", "escaped", "override"])
def test_nested_unpacks_respect_mapping_fields_and_quote_context(layout):
    secret = symmetric()
    obj = json.dumps({TYPE_NAME: OCT_TYPE})
    if layout == "escaped":
        obj = obj.replace(TYPE_NAME, "\\u006bty")
    for _ in range(3):
        if layout == "comment":
            obj = "{ # stray } ) ]\n **(" + obj + ")}"
        elif layout == "override":
            obj = "{" + repr(TYPE_NAME) + ":'RSA', **(" + obj + ")}"
        else:
            obj = '{"note":"brackets } ] (", **(' + obj + ")}"
    text = "dict(**(" + obj + "), k=" + repr(secret) + ")"
    ast.parse(text)
    masked = secret not in redact(text)
    assert masked
    assert_masked_and_audited(text, secret)


@pytest.mark.parametrize("boundary", ["call", "member", "text", "later_type", "later_unpack"])
def test_nested_oct_metadata_does_not_retype_an_unrelated_mapping(boundary):
    oct_obj = json.dumps({TYPE_NAME: OCT_TYPE})
    if boundary == "call":
        middle = '"meta": dict(**(' + oct_obj + "))"
    elif boundary == "member":
        middle = '"meta": {**(' + oct_obj + ")}"
    elif boundary == "text":
        middle = '"note": ' + json.dumps("**(" + oct_obj + ")")
    elif boundary == "later_type":
        middle = "**(" + oct_obj + "), " + repr(TYPE_NAME) + ":'RSA'"
    else:
        middle = "**(" + oct_obj + "), **(" + json.dumps({TYPE_NAME: "RSA"}) + ")"
    outer = "{" + repr(TYPE_NAME) + ":'RSA', " + middle + ", 'k':" + repr(KEPT) + "}"
    text = "dict(**(" + outer + "), k=" + repr(KEPT) + ")"
    ast.parse(text)
    assert redact(text) == text


@pytest.mark.parametrize("layers", [1, 2, 3])
def test_encoded_nested_unpacks_mask_the_enclosing_json_string(layers):
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    obj = json.dumps({TYPE_NAME: OCT_TYPE})
    for _ in range(3):
        obj = "{**(" + obj + ")}"
    text = "dict(**(" + obj + "), k=" + repr(secret) + ")"
    for _ in range(layers):
        text = json.dumps({"log": text, "public": KEPT})
    masked = secret not in redact(text)
    assert masked
    assert json.loads(redact(text))["public"] == KEPT
    assert line_of(text, secret) in flagged_lines(text)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)


@pytest.mark.parametrize("layout", ["retyped", "untyped", "later_source"])
def test_copied_members_use_the_final_enclosing_oct_type(layout):
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    if layout == "retyped":
        obj = "{**" + json.dumps({TYPE_NAME: "RSA", "k": secret}) + ", "
        obj += repr(TYPE_NAME) + ":" + repr(OCT_TYPE) + "}"
    elif layout == "untyped":
        obj = "{" + repr(TYPE_NAME) + ":" + repr(OCT_TYPE) + ", **"
        obj += json.dumps({"k": secret}) + "}"
    else:
        obj = "{**" + json.dumps({"k": secret}) + ", **"
        obj += json.dumps({TYPE_NAME: OCT_TYPE}) + "}"
    text = "dict(**" + obj + ")"
    ast.parse(text)
    assert line_of(text, secret) in flagged_lines(text)
    masked = secret not in redact(text)
    assert masked
    assert any(m.name == "k" for m in jwk.private_members(text))
    assert all(f.action == pr.BLOCK for f in pr._jwk_findings(text))


@pytest.mark.parametrize("depth", [0, 1, 3])
@pytest.mark.parametrize("layout", ["plain", "multiline", "parenthesized"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_type_only_unpacks_keep_the_operator_structural(depth, layout, layers):
    from unified_agent.tools import public_relay as pr

    obj = json.dumps({TYPE_NAME: OCT_TYPE})
    for _ in range(depth):
        obj = "{**(" + obj + ")}"
    operand = "**" + obj
    if layout == "multiline":
        operand = "**( \n" + obj + ")"
    elif layout == "parenthesized":
        operand = "**((" + obj + "))"
    text = "dict(" + operand + ")"
    for _ in range(layers):
        text = json.dumps({"log": text})
    ast.parse(text)
    assert not jwk.private_members(text), "an unpack operator is not a private value"
    assert not flagged_lines(text)
    assert not pr._jwk_findings(text)
    assert redact(text) == text
    ast.parse(redact(text))


@pytest.mark.parametrize("kind", ["kwargs", "literal-unpack"])
@pytest.mark.parametrize("operand", ["extra", "(extra)", "(\nextra\n)", "{}", "({'kty': 'RSA'})"])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_sibling_unpacks_keep_all_operators_structural(kind, operand, before, layers):
    from unified_agent.tools import public_relay as pr

    trigger = (
        form("{kty}='{oct_value}'") if kind == "kwargs" else "**" + repr({TYPE_NAME: OCT_TYPE})
    )
    sibling = "**" + operand
    args = [sibling, trigger] if before else [trigger, sibling]
    text = "dict(" + ", ".join(args) + ")"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = redact(text)
    ast.parse(out)
    if "extra" in operand:
        assert "extra" not in out
        members = jwk.private_members(text)
        assert members
        if not layers:
            assert "**" in out
            assert all("**" not in text[m.start : m.end] for m in members)
        assert flagged_lines(text)
        assert pr._jwk_findings(text)
    else:
        assert not jwk.private_members(text), "a sibling unpack operator carries no private value"
        assert not flagged_lines(text)
        assert not pr._jwk_findings(text)
        assert out == text


@pytest.mark.parametrize("spacing", ["", "  "])
@pytest.mark.parametrize("layout", ["keyword", "positional"])
def test_unpack_spelling_inside_private_string_is_not_structural(spacing, layout):
    secret = "**q" + secrets.token_hex(3) + "z"
    template = (
        "dict({kty}='{oct_value}', note='{spacing}{s}')"
        if layout == "keyword"
        else "dict('{spacing}{s}', {kty}='{oct_value}')"
    )
    text = form(template, s=secret, spacing=spacing)
    out = assert_masked_and_audited(text, secret)
    assert "**" not in out
    ast.parse(out)


PYTHON_PREFIXES = ("b", "r", "u", "f", "br", "rb", "fr", "rf", "t", "tr", "rt")
PYTHON_PREFIX_CASES = sorted(
    {
        variant
        for word in PYTHON_PREFIXES
        for variant in (word, word.upper(), word.capitalize(), word[0] + word[1:].upper())
    }
)


@pytest.mark.parametrize("prefix", PYTHON_PREFIX_CASES)
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
@pytest.mark.parametrize("layout", ["keyword", "positional", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_python_literal_prefixes_remain_structural(prefix, quote, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    literal = prefix + quote + secret + quote
    templates = {
        "keyword": "dict({kty}='{oct_value}', k={literal})",
        "positional": "dict({literal}, {kty}='{oct_value}')",
        "unpack": "dict({kty}='{oct_value}', **{literal})",
    }
    text = form(templates[layout], literal=literal)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    ast.parse(out)
    if not layers:
        assert prefix + quote in out
        assert all(m.value.lower() != prefix.lower() for m in jwk.private_members(text))
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)


@pytest.mark.parametrize("prefix", PYTHON_PREFIXES)
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
def test_empty_prefixed_literal_does_not_report_its_prefix_as_private(prefix, quote):
    from unified_agent.tools import public_relay as pr

    text = form("dict({kty}='{oct_value}', k={literal})", literal=prefix + quote * 2)
    ast.parse(text)
    assert not jwk.private_members(text)
    assert not flagged_lines(text)
    assert not pr._jwk_findings(text)
    assert redact(text) == text


@pytest.mark.parametrize("prefix", ["f", "F", "fr", "rF", "t", "T", "tr", "rT"])
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
@pytest.mark.parametrize("shape", ["expression", "conversion", "format", "braces"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_formatted_literal_prefix_preserves_parseable_private_content(prefix, quote, shape, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    body = {
        "expression": "{" + secret + "}",
        "conversion": "{" + secret + "!r}",
        "format": "{" + secret + ":>{width}}",
        "braces": "{{" + secret + "}}",
    }[shape]
    text = form("dict({kty}='{oct_value}', k={literal})", literal=prefix + quote + body + quote)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    if not layers:
        assert prefix + quote in out
    ast.parse(out)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("outer", ["f", "t"])
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
@pytest.mark.parametrize("prefix", PYTHON_PREFIXES)
@pytest.mark.parametrize("same_quote", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_formatted_nested_literal_prefixes_remain_parseable(
    outer, quote, prefix, same_quote, layers
):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    inner = quote[0] if same_quote else ('"' if quote[0] == "'" else "'")
    body = "{" + prefix + inner + secret + inner + "}"
    text = form("dict({kty}='{oct_value}', k={literal})", literal=outer + quote + body + quote)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    for _ in range(layers):
        if out == MARK:
            break  # The existing depth-bound scan may replace a whole log value.
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("outer", ["f", "t"])
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
@pytest.mark.parametrize(
    "shape",
    [
        "lambda",
        "walrus",
        "slice",
        "dict",
        "format-quote",
        "nested-format",
        "literal",
        "nested-literal",
        "comment",
        "escaped-brace",
    ],
)
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_formatted_expression_operators_remain_parseable(outer, quote, shape, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    literal_quote = '"' if quote[0] == "'" else "'"
    body = {
        "lambda": "{(lambda x: " + secret + ")}",
        "walrus": "{(x:=" + secret + ")}",
        "slice": "{" + secret + "[1:3]}",
        "dict": "{ {'key': " + secret + "} }",
        "format-quote": "{" + secret + ":" + literal_quote + " >10}",
        "nested-format": "{" + secret + ":>{(lambda x: x)(3)}}",
        "literal": "b" + literal_quote + secret + literal_quote,
        "nested-literal": "{f" + quote[0] + "{" + secret + "}" + quote[0] + "}",
        "comment": "{(\n# " + quote[0] + "\n" + secret + "\n)}",
        "escaped-brace": "\\{" + "b" + literal_quote + secret + literal_quote + "}",
    }[shape]
    if shape == "escaped-brace":
        outer = "r" + outer
    text = form("dict({kty}='{oct_value}', k={literal})", literal=outer + quote + body + quote)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    for _ in range(layers):
        if out == MARK:
            break  # The existing depth-bound scan may replace a whole log value.
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("prefix", PYTHON_PREFIXES)
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
@pytest.mark.parametrize(
    "shape",
    [
        "before",
        "after",
        "after-spaced",
        "between",
        "spaced",
        "multiline",
        "conditional",
        "conditional-after",
        "call",
    ],
)
@pytest.mark.parametrize("layout", ["keyword", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_compound_quoted_operands_remain_parseable(prefix, quote, shape, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    literal = prefix + quote + secret + quote
    other_quote = '"' if quote[0] == "'" else "'"
    other = prefix + other_quote + secret + other_quote
    value = {
        "before": "prefix+" + literal,
        "after": literal + "+suffix",
        "after-spaced": literal + " + suffix",
        "conditional-after": literal + " if cond else suffix",
        "between": literal + "+middle+" + other,
        "spaced": "prefix + " + literal,
        "multiline": "(\n" + literal + "\n +prefix+ " + other + "\n)",
        "conditional": "prefix if cond else " + literal,
        "call": "join(" + literal + ",suffix)",
    }[shape]
    template = (
        "dict({kty}='{oct_value}', k={value})"
        if layout == "keyword"
        else "dict(**{{'{kty}':'{oct_value}'}}, k={value})"
    )
    text = form(template, value=value)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    assert pr._jwk_findings(text)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)


@pytest.mark.parametrize(
    "shape",
    ["mapping", "set", "list", "group", "nested", "public", "conditional", "multiline", "comment"],
)
@pytest.mark.parametrize("layout", ["keyword", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_compound_collection_operands_remain_parseable(shape, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    public_object = json.dumps({TYPE_NAME: "RSA", "public": 1}, separators=(",", ":"))
    value = {
        "mapping": "base | {'aud': " + secret + "}",
        "set": "base | {" + secret + "}",
        "list": "base + [" + secret + "]",
        "group": "base + (" + secret + ")",
        "nested": "base | {'aud': [" + secret + "]}",
        "public": "base | {'aud': " + secret + ", 'metadata': " + public_object + "}",
        "conditional": secret + " if cond else {'aud': value}",
        "multiline": "base |\n {'aud': " + secret + "}",
        "comment": "base | # metadata\n {'aud': " + secret + "}",
    }[shape]
    template = (
        "dict({kty}='{oct_value}', claims={value}, kid='after')"
        if layout == "keyword"
        else "dict(**{{'{kty}':'{oct_value}'}}, claims={value}, kid='after')"
    )
    text = form(template, value=value)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)
        assert "kid=" in out
        if shape == "public":
            assert public_object in out


@pytest.mark.parametrize(
    "shape",
    [
        "public-direct",
        "public-call",
        "public-index",
        "public-conditional",
        "public-bool",
        "public-multiline",
        "lambda-direct",
        "lambda-default",
        "lambda-call",
        "lambda-nested",
        "lambda-public",
        "lambda-multiline",
    ],
)
@pytest.mark.parametrize("layout", ["keyword", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_compound_subtracted_operand_boundaries_remain_parseable(shape, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(6) + "z"
    public = json.dumps({TYPE_NAME: "RSA", "k": "PUBLIC"}, separators=(",", ":"))
    value = {
        "public-direct": secret + " | " + public,
        "public-call": "f(" + secret + ") | " + public,
        "public-index": "items[" + secret + "] | " + public,
        "public-conditional": secret + " if cond else " + public,
        "public-bool": secret + " and " + public,
        "public-multiline": "(" + secret + " |\n" + public + ")",
        "lambda-direct": "(" + secret + " if cond else lambda: " + secret + ")",
        "lambda-default": "(" + secret + " if cond else lambda arg=" + secret + ": arg)",
        "lambda-call": "f(" + secret + " if cond else lambda: " + secret + ")",
        "lambda-nested": "("
        + secret
        + " if cond else lambda: ("
        + secret
        + " if other else lambda: "
        + secret
        + "))",
        "lambda-public": "(" + secret + " if cond else lambda: f(" + public + "," + secret + "))",
        "lambda-multiline": "(" + secret + " if cond else\n lambda: " + secret + ")",
    }[shape]
    template = (
        "dict({kty}='{oct_value}', k={value}, kid='after')"
        if layout == "keyword"
        else "dict(**{{'{kty}':'{oct_value}'}}, k={value}, kid='after')"
    )
    text = form(template, value=value)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)
        if shape.startswith("public-") or shape == "lambda-public":
            assert public in out


@pytest.mark.parametrize("left", ["x", "f(x)", "items[x]", "(x)", "'x'", "2"])
@pytest.mark.parametrize("wrapper", ["{obj}", "({obj})", "(\n # metadata\n {obj})", "\\\n {obj}"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_exponentiation_does_not_supply_an_enclosing_oct_type(left, wrapper, layers):
    from unified_agent.tools import public_relay as pr

    obj = repr({TYPE_NAME: OCT_TYPE})
    text = "dict(note=" + left + " ** " + wrapper.format(obj=obj) + ", k=" + repr(KEPT) + ")"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    assert not jwk.private_members(text)
    assert redact(text) == text
    assert not flagged_lines(text)
    assert not pr._jwk_findings(text)


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "note='# comment', ",
        "note=f'{x}', ",
        "# comment\n ",
        "note=1, # comment\n ",
        "note=1, \\\n ",
    ],
)
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_unpack_boundary_after_strings_comments_and_continuations(prefix, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    text = "dict(" + prefix + "**" + repr({TYPE_NAME: OCT_TYPE}) + ", k=" + repr(secret) + ")"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    assert_masked_and_audited(text, secret)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("layout", ["keyword", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
@pytest.mark.parametrize(
    "shape",
    [
        "call-attribute",
        "call-binary",
        "call-conditional",
        "call-chain",
        "group-attribute",
        "group-binary",
        "index-attribute",
        "index-binary",
        "nested-keyword",
        "lambda-default",
        "multiline",
        "public-attribute",
        "public-binary",
        "public-chain",
        "public-spaced-attribute",
        "public-multiline-attribute",
        "untyped-attribute",
    ],
)
def test_bare_compound_tails_remain_parseable(shape, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    public_object = json.dumps({TYPE_NAME: "RSA", "public": 1}, separators=(",", ":"))
    value = {
        "call-attribute": "f(x)." + secret,
        "call-binary": "f(x) + " + secret,
        "call-conditional": "f(x) if cond else " + secret,
        "call-chain": "f(x)." + secret + "(y).tail + z",
        "group-attribute": "(x)." + secret,
        "group-binary": "(x) + " + secret,
        "index-attribute": "items[index]." + secret,
        "index-binary": "items[index] + " + secret,
        "nested-keyword": "outer(arg=f(x)." + secret + ").tail",
        "lambda-default": "lambda arg=f(x)." + secret + ": arg",
        "multiline": "(\n f(x)\n + " + secret + "\n)",
        "public-attribute": "f(" + public_object + ")." + secret,
        "public-binary": "f(" + public_object + ") + " + secret,
        "public-chain": "f(" + public_object + ")." + secret + "(y).tail",
        "public-spaced-attribute": "f(" + public_object + ") . " + secret,
        "public-multiline-attribute": "(f(" + public_object + ") .\n " + secret + ")",
        "untyped-attribute": 'f({"public":1}).' + secret,
    }[shape]
    template = (
        "dict({kty}='{oct_value}', k={value}, kid='after')"
        if layout == "keyword"
        else "dict(**{{'{kty}':'{oct_value}'}}, k={value}, kid='after')"
    )
    text = form(template, value=value)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    assert pr._jwk_findings(text)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)
        assert "kid=" in out
        if shape.startswith("public-"):
            assert public_object in out
        elif shape.startswith("untyped-"):
            assert '{"public":"' + MARK + '"}' in out


@pytest.mark.parametrize("allow_secret", [False, True])
@pytest.mark.parametrize("public_child", [False, True])
def test_bare_compound_audit_keeps_physical_suppression(allow_secret, public_child, tmp_path):
    from unified_agent.audit.engine import run_audit

    secret = "q" + secrets.token_hex(3) + "z"
    argument = repr({TYPE_NAME: "RSA"}) if public_child else "x"
    suffix = " # ua:allow" if allow_secret else ""
    text = form(
        "dict({kty}='{oct_value}', k=(f({argument}) # ua:allow\n + {secret}{suffix}\n))",
        argument=argument,
        secret=secret,
        suffix=suffix,
    )
    ast.parse(text)
    ast.parse(assert_masked(text, secret))
    (tmp_path / "fixture.py").write_text(text)
    lines = {
        item["line"]
        for item in run_audit(tmp_path, include_repo_rules=False)["findings"]
        if item["rule"] == "SECRETS-KEY"
    }
    assert (2 in lines) is not allow_secret


@pytest.mark.parametrize("prefix", PYTHON_PREFIXES)
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
@pytest.mark.parametrize(
    "shape",
    [
        "call",
        "index",
        "slice",
        "group",
        "walrus",
        "lambda",
        "nested-call",
        "keyword-call",
        "multiline",
        "chain",
        "conditional",
        "after-call",
        "public-call",
        "public-prefix",
        "public-conditional",
        "untyped-call",
        "untyped-prefix",
        "untyped-conditional",
        "lambda-default",
        "lambda-default-comma",
    ],
)
@pytest.mark.parametrize("layout", ["keyword", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_structured_compound_prefix_remains_parseable(prefix, quote, shape, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    literal = prefix + quote + secret + quote
    public_object = json.dumps({TYPE_NAME: "RSA", "public": 1}, separators=(",", ":"))
    value = {
        "call": "f(x) + " + literal,
        "index": "items[index] + " + literal,
        "slice": "items[1:3] + " + literal,
        "group": "(prefix + suffix) + " + literal,
        "walrus": "(x := prefix) + " + literal,
        "lambda": "(lambda x: x)(prefix) + " + literal,
        "nested-call": "outer(inner(x)) + " + literal,
        "keyword-call": "f(arg=x) + " + literal,
        "multiline": "(\n f(x)\n + " + literal + "\n)",
        "chain": "f(x) + " + literal + " + g(y) + " + literal,
        "conditional": "f(x) if cond else " + literal,
        "after-call": literal + " + f(x) + " + literal,
        "public-call": "f(" + public_object + ") + " + literal,
        "public-prefix": "f(" + public_object + ") + prefix + " + literal,
        "public-conditional": "f(" + public_object + ") if cond else " + literal,
        "untyped-call": 'f({"public":1}) + ' + literal,
        "untyped-prefix": 'f({"public":1}) + prefix + ' + literal,
        "untyped-conditional": 'f({"public":1}) if cond else ' + literal,
        "lambda-default": "lambda arg=f(x) + " + literal + ": arg",
        "lambda-default-comma": "lambda arg=f(x) + " + literal + ", other=0: arg",
    }[shape]
    template = (
        "dict({kty}='{oct_value}', k={value}, kid='after')"
        if layout == "keyword"
        else "dict(**{{'{kty}':'{oct_value}'}}, k={value}, kid='after')"
    )
    text = form(template, value=value)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    assert pr._jwk_findings(text)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)
        assert "kid=" in out
        if shape.startswith("public-"):
            assert public_object in out
        elif shape.startswith("untyped-"):
            assert '{"public":"' + MARK + '"}' in out


@pytest.mark.parametrize(
    "prefix,quote",
    [("", None)]
    + [(prefix, quote) for prefix in PYTHON_PREFIXES for quote in ("'", '"', "'" * 3, '"' * 3)],
)
@pytest.mark.parametrize(
    "shape",
    [
        "list",
        "set",
        "dict",
        "generator",
        "tuple-target",
        "multi-target",
        "nested",
        "filtered",
        "async",
        "typed-child",
        "subscript-target",
        "call-target",
    ],
)
@pytest.mark.parametrize("layout", ["keyword", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_comprehension_values_remain_parseable(prefix, quote, shape, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    body = "f(" + secret + ")" if quote is None else "f(x)+" + prefix + quote + secret + quote
    public_object = json.dumps({TYPE_NAME: "RSA", "public": 1}, separators=(",", ":"))
    index_value = secret if quote is None else prefix + quote + secret + quote
    value = {
        "list": "[" + body + " for x in xs]",
        "set": "{" + body + " for x in xs}",
        "dict": "{x:" + body + " for x in xs}",
        "generator": "(" + body + " for x in xs)",
        "tuple-target": "[" + body + " for (x,y) in xs]",
        "multi-target": "[" + body + " for x,y in xs]",
        "nested": "[" + body + " for x in xs for y in ys]",
        "filtered": "[" + body + " for x in xs if pred(x)]",
        "async": "[" + body + " async for x in xs]",
        "typed-child": "[f(" + public_object + "," + body + ") for x in xs]",
        "subscript-target": "[" + body + " for obj[" + index_value + "] in xs]",
        "call-target": "[" + body + " for factory(" + index_value + ").attr in xs]",
    }[shape]
    template = (
        "dict({kty}='{oct_value}', k={value}, kid='after')"
        if layout == "keyword"
        else "dict(**{{'{kty}':'{oct_value}'}}, k={value}, kid='after')"
    )
    text = form(template, value=value)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    assert pr._jwk_findings(text)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)
        assert "kid=" in out
        if shape == "typed-child":
            assert public_object in out


@pytest.mark.parametrize(
    "target",
    [
        "({value}).attr",
        "obj[{value}]",
        "factory({value}).attr",
        "obj[({value},0)]",
    ],
)
@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize("layers", [0, 1])
def test_comprehension_computed_targets_keep_values_private(target, literal, layers):
    from unified_agent.tools import public_relay as pr

    secret = str(int.from_bytes(secrets.token_bytes(12)))
    value = repr("é" + secret) if literal else secret
    text = form(
        "dict({kty}='{oct_value}', k=[f(x) for {target} in xs])", target=target.format(value=value)
    )
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    assert pr._jwk_findings(text)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)


@pytest.mark.parametrize("target", ["factory({value}).attr", "obj[{value}]"])
@pytest.mark.parametrize("layers", [0, 1])
def test_comprehension_computed_target_parsing_is_quiet(target, layers):
    import warnings

    secret = "q" + secrets.token_hex(3) + "z"
    value = "'\\q" + secret + "'"
    text = form(
        "dict({kty}='{oct_value}', k=[f(x) for {target} in xs])", target=target.format(value=value)
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert_masked_and_audited(text, secret)
    assert not caught


@pytest.mark.parametrize(
    "kind,prefix,quote",
    [("numeric", "", ""), ("identifier", "", "")]
    + [
        ("literal", prefix, quote)
        for prefix in PYTHON_PREFIXES
        for quote in ("'", '"', "'" * 3, '"' * 3)
    ],
)
@pytest.mark.parametrize(
    "shape",
    [
        "lower",
        "upper",
        "step",
        "all",
        "multidim",
        "nested",
        "grouped",
        "multiline",
    ],
)
@pytest.mark.parametrize("layout", ["keyword", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_comprehension_slice_components_are_values(kind, prefix, quote, shape, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = (
        str(int.from_bytes(secrets.token_bytes(12)))
        if kind == "numeric"
        else ("q" + secrets.token_hex(3) + "z")
    )
    body = ("é" if "b" not in prefix.lower() else "") + secret
    value = prefix + quote + body + quote if kind == "literal" else secret
    bound = {
        "lower": value + ":upper",
        "upper": "lower:" + value,
        "step": "lower:upper:" + value,
        "all": value + ":" + value + ":" + value,
        "multidim": value + ":upper,lower:" + value,
        "nested": "f(obj[" + value + ":upper])",
        "grouped": "(" + value + "):upper",
        "multiline": "(\n" + value + "\n):upper",
    }[shape]
    template = (
        "dict({kty}='{oct_value}', k=[f(x) for obj[{bound}] in xs])"
        if layout == "keyword"
        else "dict(**{{'{kty}':'{oct_value}'}}, k=[f(x) for obj[{bound}] in xs])"
    )
    text = form(template, bound=bound)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    assert any(secret in member.value for member in jwk.private_members(text))
    out = assert_masked_and_audited(text, secret)
    assert pr._jwk_findings(text)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)


@pytest.mark.parametrize("kind", ["numeric", "identifier", "literal", "bytes", "raw", "triple"])
@pytest.mark.parametrize(
    "shape",
    [
        "sole-lower",
        "sole-upper",
        "sole-step",
        "lower",
        "upper",
        "step",
        "all",
        "multidim",
        "nested",
        "grouped",
        "multiline",
        "public-child",
    ],
)
@pytest.mark.parametrize("layout", ["keyword", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_ordinary_slice_components_are_values(kind, shape, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = (
        str(int.from_bytes(secrets.token_bytes(12)))
        if kind == "numeric"
        else "q" + secrets.token_hex(3) + "z"
    )
    value = {
        "numeric": secret,
        "identifier": secret,
        "literal": repr(secret),
        "bytes": "b" + repr(secret),
        "raw": "r" + repr(secret),
        "triple": "'" * 3 + secret + "'" * 3,
    }[kind]
    public_object = json.dumps({TYPE_NAME: "RSA", "public": 1}, separators=(",", ":"))
    bound = {
        "sole-lower": value + ":",
        "sole-upper": ":" + value,
        "sole-step": "::" + value,
        "lower": value + ":upper",
        "upper": "lower:" + value,
        "step": "lower:upper:" + value,
        "all": value + ":" + value + ":" + value,
        "multidim": value + ":upper,lower:" + value,
        "nested": "REDACTED[" + value + ":]:",
        "grouped": "(" + value + "):",
        "multiline": "(\n" + value + "\n):",
        "public-child": "(" + public_object + "," + value + "):",
    }[shape]
    template = (
        "dict({kty}='{oct_value}', k=REDACTED[{bound}])"
        if layout == "keyword"
        else "dict(**{{'{kty}':'{oct_value}'}}, k=REDACTED[{bound}])"
    )
    text = form(template, bound=bound)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    assert any(secret in member.value for member in jwk.private_members(text))
    out = assert_masked_and_audited(text, secret)
    findings = pr._jwk_findings(text)
    assert findings and all(f.action == pr.BLOCK for f in findings)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)
        if shape == "public-child":
            assert public_object in out


@pytest.mark.parametrize(
    "shape",
    [
        "call",
        "group",
        "index",
        "nested-call",
        "keyword-call",
        "multiple-defaults",
        "bare-default",
        "no-default",
        "public-default",
        "public-body",
        "nested-lambda",
    ],
)
@pytest.mark.parametrize("layout", ["keyword", "unpack"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_lambda_bindings_and_complete_defaults_remain_parseable(shape, layout, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    public_object = json.dumps({TYPE_NAME: "RSA", "public": 1}, separators=(",", ":"))
    value = {
        "call": "(lambda arg=f(" + secret + "): arg)",
        "group": "(lambda arg=(" + secret + "): arg)",
        "index": "(lambda arg=items[" + secret + "]: arg)",
        "nested-call": "(lambda arg=f(g(" + secret + ")): arg)",
        "keyword-call": "(lambda arg=f(key=" + secret + "): arg)",
        "multiple-defaults": "(lambda arg=f(" + secret + "), other=g(x): arg)",
        "bare-default": "(lambda arg=" + secret + ": arg)",
        "no-default": "(lambda arg: " + secret + ")",
        "public-default": "(lambda arg=f(" + public_object + "," + secret + "): arg)",
        "public-body": "(lambda arg=f(" + secret + "): f(" + public_object + "))",
        "nested-lambda": "(lambda arg=lambda other=f(" + secret + "): other: arg)",
    }[shape]
    template = (
        "dict({kty}='{oct_value}', k={value})"
        if layout == "keyword"
        else "dict(**{{'{kty}':'{oct_value}'}}, k={value})"
    )
    text = form(template, value=value)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = assert_masked_and_audited(text, secret)
    assert pr._jwk_findings(text)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)["log"]
    if out != MARK:
        ast.parse(out)
        if shape.startswith("public-"):
            assert public_object in out


@pytest.mark.parametrize("layout", ["block", "flow"])
def test_lambda_shaped_yaml_values_stay_private(layout):
    secret = "q" + secrets.token_hex(3) + "z"
    value = "lambda " + secret
    text = (
        form("{kty}: {oct_value}\nk: {value}\nkid: after", value=value)
        if layout == "block"
        else form("{{{kty}: {oct_value}, k: {value}, kid: after}}", value=value)
    )
    assert_masked_and_audited(text, secret)


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("allow_secret", [False, True])
def test_comprehension_slice_audit_keeps_physical_suppression(quoted, allow_secret, tmp_path):
    from unified_agent.audit.engine import run_audit

    secret = str(int.from_bytes(secrets.token_bytes(12)))
    value = repr(secret) if quoted else secret
    marker = " # ua:allow" if allow_secret else ""
    text = form(
        "dict({kty}='{oct_value}', k=[f(x) for obj[ # ua:allow\n{value}{marker}\n:upper] in xs])",
        value=value,
        marker=marker,
    )
    ast.parse(text)
    ast.parse(assert_masked(text, secret))
    (tmp_path / "fixture.py").write_text(text)
    lines = {
        item["line"] for item in run_audit(tmp_path)["findings"] if item["rule"] == "SECRETS-KEY"
    }
    assert (2 in lines) is not allow_secret


@pytest.mark.parametrize(
    "spelling",
    [
        "kty",
        r"\x6b\x74\x79",
        r"\u006b\u0074\u0079",
        r"\U0000006b\U00000074\U00000079",
        r"\153\164\171",
        r"\N{LATIN SMALL LETTER K}\N{LATIN SMALL LETTER T}\N{LATIN SMALL LETTER Y}",
    ],
)
@pytest.mark.parametrize("encoding", ["json", "unicode"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_nested_scan_hint_preserves_encoded_key_spellings(spelling, encoding, layers):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    text = "{'" + spelling + "':'" + OCT_TYPE + "','k':'" + secret + "'}"
    node = ast.parse(text, mode="eval").body
    assert any(key.value == TYPE_NAME for key in node.keys)
    for _ in range(layers):
        text = (
            json.dumps({"log": text})
            if encoding == "json"
            else '"' + "".join(f"\\u{ord(char):04x}" for char in text) + '"'
        )
    out = redact(text)
    assert jwk.private_members(text)
    assert pr._jwk_findings(text)
    assert flagged_lines(text)
    for _ in range(layers):
        if out == MARK:
            break
        out = json.loads(out)
        if encoding == "json":
            out = out["log"]
    if out != MARK:
        node = ast.parse(out, mode="eval").body
        assert all(value.value != secret for value in node.values)


@pytest.mark.parametrize("prefix", ["", "r", "b", "u", "f", "t"])
@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("allow_secret", [False, True])
@pytest.mark.parametrize("public_child", [False, True])
def test_comprehension_audit_keeps_physical_suppression(
    prefix, quote, allow_secret, public_child, tmp_path
):
    from unified_agent.audit.engine import run_audit

    secret = "q" + secrets.token_hex(3) + "z"
    body = secret + (" # ua:allow" if allow_secret else "")
    literal = prefix + quote + "# ua:allow\n" + body + "\n" + quote
    public_object = json.dumps({TYPE_NAME: "RSA"}, separators=(",", ":"))
    head = "f(" + public_object + ",x)" if public_child else "f(x)"
    text = form(
        "dict({kty}='{oct_value}', k=[{head}+{literal} for x in xs])", head=head, literal=literal
    )
    ast.parse(text)
    ast.parse(assert_masked(text, secret))
    (tmp_path / "fixture.py").write_text(text)
    report = run_audit(tmp_path)
    lines = {item["line"] for item in report["findings"] if item["rule"] == "SECRETS-KEY"}
    assert (2 in lines) is not allow_secret


@pytest.mark.parametrize("prefix", ["", "r", "b", "u", "f", "t"])
@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("allow_secret", [False, True])
@pytest.mark.parametrize("head", ["f(x)", "items[index]", 'f({"public":1})'])
def test_structured_compound_audit_keeps_physical_suppression(
    prefix, quote, allow_secret, head, tmp_path
):
    from unified_agent.audit.engine import run_audit

    secret = "q" + secrets.token_hex(3) + "z"
    body = secret + (" # ua:allow" if allow_secret else "")
    literal = prefix + quote + "# ua:allow\n" + body + "\n" + quote
    text = form("dict({kty}='{oct_value}', k={head} + {literal})", head=head, literal=literal)
    ast.parse(text)
    ast.parse(assert_masked(text, secret))
    (tmp_path / "fixture.py").write_text(text)
    report = run_audit(tmp_path)
    lines = {item["line"] for item in report["findings"] if item["rule"] == "SECRETS-KEY"}
    assert (2 in lines) is not allow_secret


@pytest.mark.parametrize("prefix", ["", "r", "b", "u", "f", "t"])
@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("allow_secret", [False, True])
def test_compound_literal_audit_keeps_physical_suppression(prefix, quote, allow_secret, tmp_path):
    from unified_agent.audit.engine import run_audit

    secret = "q" + secrets.token_hex(3) + "z"
    body = secret + (" # ua:allow" if allow_secret else "")
    literal = prefix + quote + "# ua:allow\n" + body + "\n" + quote
    text = form("dict({kty}='{oct_value}', k=prefix_var + {literal})", literal=literal)
    ast.parse(text)
    out = assert_masked(text, secret)
    ast.parse(out)
    (tmp_path / "fixture.py").write_text(text)
    lines = {
        item["line"]
        for item in run_audit(tmp_path, include_repo_rules=False)["findings"]
        if item["rule"] == "SECRETS-KEY"
    }
    assert (2 in lines) is not allow_secret


@pytest.mark.parametrize("body", ["b", "r", "f", "br", 'r"word"', "b'word'"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_prefix_letters_inside_private_values_are_still_masked(body, layers):
    quote = '"' if '"' not in body else "'"
    text = form("dict({kty}='{oct_value}', k={literal})", literal=quote + body + quote)
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = redact(text)
    assert MARK in out
    assert body not in out
    ast.parse(out)


@pytest.mark.parametrize("prefix", PYTHON_PREFIX_CASES)
@pytest.mark.parametrize("ticks", ["`", "``"])
def test_backticks_do_not_turn_yaml_values_into_python_prefixes(prefix, ticks):
    import yaml

    from unified_agent.tools import public_relay as pr

    value = prefix + ticks
    text = form("{kty}: {oct_value}\nk: {s}", s=value)
    assert yaml.safe_load(text) == {TYPE_NAME: OCT_TYPE, "k": value}
    assert_masked_and_audited(text, value)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("prefix", ["b", "r", "u", "f", "t"])
@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_prefixed_literal_actual_engine_reports_private_body_line(prefix, quote, newline, tmp_path):
    from unified_agent.audit.engine import run_audit

    secret = "q" + secrets.token_hex(3) + "z"
    body = "{" + secret + ":>{width}}" if prefix in ("f", "t") else secret
    literal = prefix + quote + " # ua:allow\n" + body + "\n" + quote
    text = form("dict({kty}='{oct_value}', k={literal})", literal=literal).replace("\n", newline)
    ast.parse(text)
    out = assert_masked(text, secret)
    ast.parse(out)
    (tmp_path / "fixture.txt").write_bytes(text.encode())
    assert any(
        f["rule"] == "SECRETS-KEY" and f["line"] == line_of(text, secret)
        for f in run_audit(tmp_path)["findings"]
    )


@pytest.mark.parametrize("prefix", ["", "r", "b", "u"])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_decoded_child_literal_cannot_supply_the_enclosing_call_type(prefix, before, layers):
    from unified_agent.tools import public_relay as pr

    child = "note=" + prefix + repr(repr({TYPE_NAME: OCT_TYPE}))
    sibling = "k=" + repr(KEPT)
    args = [sibling, child] if before else [child, sibling]
    snippet = "dict(" + ", ".join(args) + ")"
    ast.parse(snippet)
    text = json.dumps({TYPE_NAME: "RSA", "note": snippet, "k": KEPT})
    for _ in range(layers):
        text = json.dumps({"log": text})
    assert not jwk.private_members(text)
    assert not flagged_lines(text)
    assert not pr._jwk_findings(text)
    assert redact(text) == text


@pytest.mark.parametrize(
    ("parent", "child"),
    [(parent, child) for parent in ("oct", "RSA", "none") for child in ("RSA", "EC", "other")]
    + [("oct", "oct")],
)
@pytest.mark.parametrize("prefix", ["", "r", "b", "u", "f", "t"])
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
@pytest.mark.parametrize("layout", ["keyword", "mapping"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_decoded_public_object_does_not_release_enclosing_oct_value(
    parent, child, prefix, quote, layout, layers
):
    from unified_agent.tools import public_relay as pr

    secret = "q" + secrets.token_hex(3) + "z"
    nested = repr({TYPE_NAME: child})
    if prefix in ("f", "t"):
        body = "{" + repr(secret) + " if " + nested + " else ''}"
    else:
        body = (secret + " " + nested).replace(quote[0], "\\" + quote[0])
    literal = prefix + quote + body + quote
    if layout == "keyword":
        type_arg = "" if parent == "none" else TYPE_NAME + "=" + repr(parent) + ", "
        text = "dict(" + type_arg + "k=" + literal + ")"
    else:
        type_arg = "" if parent == "none" else repr(TYPE_NAME) + ":" + repr(parent) + ", "
        text = "{" + type_arg + "'k':" + literal + "}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    out = redact(text)
    if parent == "oct":
        assert secret not in out
        assert MARK in out
        assert line_of(text, secret) in flagged_lines(text)
        assert pr._jwk_findings(text)
        for _ in range(layers):
            if out == MARK:
                break
            out = json.loads(out)["log"]
        if out != MARK:
            ast.parse(out)
    else:
        assert out == text
        assert not jwk.private_members(text)
        assert not flagged_lines(text)
        assert not pr._jwk_findings(text)


@pytest.mark.parametrize("prefix", ["", "r", "b", "u", "f", "t"])
@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("allow_secret", [False, True])
def test_public_child_cannot_hide_actual_audit_source_line(prefix, quote, allow_secret, tmp_path):
    from unified_agent.audit.engine import run_audit

    secret = "q" + secrets.token_hex(3) + "z"
    child = repr({TYPE_NAME: "RSA"})
    body = (
        "{" + repr(secret) + " if " + child + " else ''}"
        if prefix in ("f", "t")
        else secret + " " + child
    )
    if allow_secret:
        body += " # ua:allow"
    literal = prefix + quote + "# ua:allow\n" + body + "\n" + quote
    text = form("dict({kty}='{oct_value}', k={literal})", literal=literal)
    ast.parse(text)
    out = assert_masked(text, secret)
    ast.parse(out)
    (tmp_path / "fixture.py").write_text(text)
    lines = {
        item["line"]
        for item in run_audit(tmp_path, include_repo_rules=False)["findings"]
        if item["rule"] == "SECRETS-KEY"
    }
    assert (2 in lines) is not allow_secret


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85"], ids=ascii)
@pytest.mark.parametrize("repetitions", [1, 3, 30])
@pytest.mark.parametrize("spelling", ["literal", "unicode", "mixed"])
@pytest.mark.parametrize("layout", ["root", "nested", "explicit", "flow"])
@pytest.mark.parametrize("suffix", ["", "agon", "_name", "9", "$"])
def test_continued_type_value_checks_its_logical_right_boundary(
    newline, repetitions, spelling, layout, suffix
):
    import yaml

    from unified_agent.tools import public_relay as pr

    word = {
        "literal": OCT_TYPE,
        "unicode": "".join(f"\\u{ord(c):04x}" for c in OCT_TYPE),
        "mixed": OCT_TYPE[0] + f"\\x{ord(OCT_TYPE[1]):02x}" + OCT_TYPE[2],
    }[spelling]
    value = word + "\\\n    " * repetitions + suffix
    templates = {
        "root": '{kty}: "{value}"\nk: {kept}',
        "nested": 'outer:\n  {kty}: "{value}"\n  k: {kept}',
        "explicit": '? {kty}\n: "{value}"\nk: {kept}',
        "flow": 'outer: {{{kty}: "{value}", k: {kept}}}',
    }
    text = form(templates[layout], value=value).replace("\n", newline)
    parsed = yaml.safe_load(text)
    if layout in ("nested", "flow"):
        parsed = parsed["outer"]
    assert parsed == {TYPE_NAME: OCT_TYPE + suffix, "k": KEPT}
    if suffix:
        assert not jwk.private_members(text)
        assert not flagged_lines(text)
        assert not pr._jwk_findings(text)
        assert redact(text) == text
    else:
        # Existing WINDOW_LINES limits still apply after a long value token.
        # JSON-decided flow objects retain their type semantics; the existing
        # YAML-continuation gap at that separate boundary is task #2126.
        if layout != "flow" and repetitions < oct_blocks.WINDOW_LINES:
            assert_masked_and_audited(text, KEPT)
            assert pr._jwk_findings(text)
        assert oct_blocks._triggers(text, oct_blocks._Spans([]))


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85"], ids=ascii)
@pytest.mark.parametrize("repetitions", [1, 3])
@pytest.mark.parametrize("suffix", ["", "Extra"])
def test_continued_scalar_key_alias_checks_its_logical_right_boundary(newline, repetitions, suffix):
    import yaml

    from unified_agent.tools import public_relay as pr

    name = TYPE_NAME + "\\\n    " * repetitions + suffix
    text = form('name: &kind "{name}"\n? *kind\n: {oct_value}\nk: {kept}', name=name)
    text = text.replace("\n", newline)
    assert yaml.safe_load(text) == {
        "name": TYPE_NAME + suffix,
        TYPE_NAME + suffix: OCT_TYPE,
        "k": KEPT,
    }
    if suffix:
        assert not jwk.private_members(text)
        assert not flagged_lines(text)
        assert not pr._jwk_findings(text)
        assert redact(text) == text
    else:
        assert_masked_and_audited(text, KEPT)
        assert pr._jwk_findings(text)


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("prefix", ["", "r", "u"])
def test_triple_quoted_unpacks_do_not_retype_outer_mapping(quote, prefix):
    from unified_agent.tools import public_relay as pr

    snippet = "**" + repr({TYPE_NAME: OCT_TYPE})
    if quote == "'" * 3:
        snippet = "xx " + snippet + " yy"
    text = "{" + repr(TYPE_NAME) + ":'RSA','note':" + prefix + quote + snippet + quote
    text += ",'k':" + repr(KEPT) + "}"
    ast.parse(text)
    assert not any(m.name == "k" for m in jwk.private_members(text))
    assert not flagged_lines(text)
    assert not pr._jwk_findings(text)
    assert redact(text) == text


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
def test_triple_quoted_jwk_is_still_independently_scanned(quote):
    from unified_agent.tools import public_relay as pr

    secret = symmetric()
    snippet = repr({TYPE_NAME: OCT_TYPE, "k": secret})
    text = "{" + repr(TYPE_NAME) + ":'RSA','note':" + quote + snippet + quote
    text += ",'k':" + repr(KEPT) + "}"
    ast.parse(text)
    assert secret not in redact(text)
    assert KEPT in redact(text)
    assert line_of(text, secret) in flagged_lines(text)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
def test_real_unpack_after_triple_string_still_supplies_type(quote):
    secret = symmetric()
    snippet = "**" + repr({TYPE_NAME: "RSA"})
    text = "{" + repr(TYPE_NAME) + ":'RSA','note':" + quote + snippet + quote
    text += ", **" + repr({TYPE_NAME: OCT_TYPE}) + ",'k':" + repr(secret) + "}"
    ast.parse(text)
    assert secret not in redact(text)
    assert line_of(text, secret) in flagged_lines(text)


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("prefix", ["", "r", "u"])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("depth", [0, 3])
def test_triple_quoted_snippet_inherits_its_own_literal_unpack(quote, prefix, before, depth):
    from unified_agent.tools import public_relay as pr

    # Short material proves contextual detection, without a generic key heuristic.
    secret = secrets.token_hex(4)
    operand = repr({TYPE_NAME: OCT_TYPE})
    for _ in range(depth):
        operand = "{**(" + operand + ")}"
    fields = ["**(" + operand + ")", "'k':" + repr(secret)]
    if before:
        fields.reverse()
    snippet = "{" + ",".join(fields) + "}"
    text = "{" + repr(TYPE_NAME) + ":'RSA','note':" + prefix + quote + snippet + quote
    text += ",'k':" + repr(KEPT) + "}"
    ast.parse(text)
    out = redact(text)
    assert secret not in out
    assert KEPT in out
    ast.parse(out)
    assert any(m.name == "k" and secret in m.value for m in jwk.private_members(text))
    assert line_of(text, secret) in flagged_lines(text)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("layers", [1, 2, 3])
def test_encoded_triple_quoted_unpack_keeps_json_valid(quote, layers):
    from unified_agent.tools import public_relay as pr

    secret = secrets.token_hex(4)
    snippet = "{**" + repr({TYPE_NAME: OCT_TYPE}) + ",'k':" + repr(secret) + "}"
    text = "{" + repr(TYPE_NAME) + ":'RSA','note':" + quote + snippet + quote + "}"
    for _ in range(layers):
        text = json.dumps({"log": text, "public": KEPT})
    out = redact(text)
    assert secret not in out
    assert json.loads(out)["public"] == KEPT
    assert line_of(text, secret) in flagged_lines(text)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("newline", [r"\n", r"\r", r"\x0a", r"\u000a", r"\U0000000a"])
@pytest.mark.parametrize("prefix", ["", "r"])
def test_triple_snippet_comments_follow_decoded_newlines(quote, newline, prefix):
    from unified_agent.tools import public_relay as pr

    secret = secrets.token_hex(4)
    body = "{**" + repr({TYPE_NAME: OCT_TYPE}) + ",'k':" + repr(secret) + "}"
    snippet = "# comment" + newline + body
    literal = prefix + quote + snippet + quote
    # Raw escapes remain data, not physical comment boundaries. The independent
    # comment scanner still inspects the literal key construction in that data.
    decoded_break = "\r" if newline == r"\r" else "\n"
    expected = snippet if prefix == "r" else "# comment" + decoded_break + body
    assert jwk._triple_value(literal) == expected
    text = "{" + repr(TYPE_NAME) + ":'RSA','k':" + repr(KEPT)
    text += ",'note':" + literal + ",'public':" + repr(KEPT) + "}"
    ast.parse(text)
    out = assert_masked_and_audited(text, secret)
    outer = ast.literal_eval(out)
    assert outer["k"] == KEPT and outer["public"] == KEPT
    findings = pr._jwk_findings(text)
    assert findings and all(finding.action == pr.BLOCK for finding in findings)


@pytest.mark.parametrize("outer", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("private", [False, True])
def test_nested_triple_snippets_keep_independent_ownership(outer, private):
    from unified_agent.tools import public_relay as pr

    inner = '"' * 3 if outer == "'" * 3 else "'" * 3
    secret = secrets.token_hex(4)
    body = "**" + repr({TYPE_NAME: OCT_TYPE})
    if private:
        body = "{" + body + ",'k':" + repr(secret) + "}"
    snippet = "{" + repr(TYPE_NAME) + ":'RSA','note':" + inner + body + inner
    snippet += ",'k':" + repr(KEPT) + "}"
    text = outer + snippet + outer
    ast.parse(text)
    if private:
        assert secret not in redact(text)
        assert flagged_lines(text)
        assert pr._jwk_findings(text)
    else:
        assert redact(text) == text
        assert not flagged_lines(text)
        assert not pr._jwk_findings(text)


@pytest.mark.parametrize("layout", ["dict", "literal", "yaml", "escaped-braces"])
def test_decoded_triple_snippets_cover_each_oct_syntax(layout):
    from unified_agent.tools import public_relay as pr

    secret = secrets.token_hex(4)
    if layout == "dict":
        body = "dict(**" + repr({TYPE_NAME: OCT_TYPE}) + ",k=" + repr(secret) + ")"
    elif layout == "yaml":
        body = TYPE_NAME + ": " + OCT_TYPE + r"\nk: " + secret
    else:
        body = "{**" + repr({TYPE_NAME: OCT_TYPE}) + ",'k':" + repr(secret) + "}"
        if layout == "escaped-braces":
            body = r"\x7b" + body[1:-1] + r"\x7d"
    text = "{" + repr(TYPE_NAME) + ":'RSA','note':'''" + body + "''','k':" + repr(KEPT) + "}"
    ast.parse(text)
    out = redact(text)
    assert secret not in out
    assert KEPT in out
    ast.parse(out)
    assert flagged_lines(text)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("depth", [1, 2, 6])
@pytest.mark.parametrize("private", [False, True])
def test_repeated_decoded_triple_nesting_is_isolated(depth, private):
    from unified_agent.tools import public_relay as pr

    secret = secrets.token_hex(4)
    text = "**" + repr({TYPE_NAME: OCT_TYPE})
    if private:
        text = "{" + text + ",'k':" + repr(secret) + "}"
    for level in range(depth):
        quote = "'" * 3 if level % 2 == 0 else '"' * 3
        escaped = text.replace("\\", "\\\\").replace(quote[0], "\\" + quote[0])
        literal = quote + escaped + quote
        text = "{" + repr(TYPE_NAME) + ":'RSA','note':" + literal + ",'k':" + repr(KEPT) + "}"
    ast.parse(text)
    out = redact(text)
    if private:
        assert secret not in out
        assert flagged_lines(text)
        assert pr._jwk_findings(text)
    else:
        assert out == text
        assert not flagged_lines(text)
        assert not pr._jwk_findings(text)
    ast.parse(out)


@pytest.mark.parametrize("real_type", [False, True])
def test_nested_triple_type_does_not_supply_the_surrounding_call(real_type):
    from unified_agent.tools import public_relay as pr

    secret = secrets.token_hex(4)
    note = '"' * 3 + "**" + repr({TYPE_NAME: OCT_TYPE}) + '"' * 3
    args = "note=" + note + ",k=" + repr(secret if real_type else KEPT)
    if real_type:
        args = TYPE_NAME + "=" + repr(OCT_TYPE) + "," + args
    text = "'''dict(" + args + ")'''"
    ast.parse(text)
    if real_type:
        assert secret not in redact(text)
        assert flagged_lines(text)
        assert pr._jwk_findings(text)
    else:
        assert redact(text) == text
        assert not flagged_lines(text)
        assert not pr._jwk_findings(text)


@pytest.mark.parametrize("real_type", [False, True])
def test_parent_oct_call_still_masks_an_independently_scanned_triple_value(real_type):
    from unified_agent.tools import public_relay as pr

    secret = secrets.token_hex(4)
    args = 'k=r"""' + "\\data\\" + secret + '"""'
    if real_type:
        args = TYPE_NAME + "=" + repr(OCT_TYPE) + "," + args
    text = "r'''dict(" + args + ")'''"
    ast.parse(text)
    if real_type:
        assert secret not in redact(text)
        assert flagged_lines(text)
        assert pr._jwk_findings(text)
    else:
        assert redact(text) == text
        assert not flagged_lines(text)
        assert not pr._jwk_findings(text)


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("newline", [r"\n", r"\x0a"])
@pytest.mark.parametrize("tail", ["", "\\", r"\x"])
def test_unfinished_triple_snippet_decodes_earlier_comment_breaks(quote, newline, tail):
    from unified_agent.tools import public_relay as pr

    secret = secrets.token_hex(4)
    body = "# comment" + newline + "{**" + repr({TYPE_NAME: OCT_TYPE})
    body += ",'k':'" + secret + tail
    text = "{" + repr(TYPE_NAME) + ":'RSA','note':" + quote + body
    assert secret not in redact(text)
    assert flagged_lines(text)
    assert pr._jwk_findings(text)


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\u2028"])
@pytest.mark.parametrize("allow", ["opening", "secret", "closing", "none"])
def test_decoded_secret_audit_suppression_is_physical(tmp_path, quote, newline, allow):
    from unified_agent.audit.engine import run_audit
    from unified_agent.tools import public_relay as pr

    secret = secrets.token_hex(4)
    opening = " # ua:allow" if allow == "opening" else ""
    body = "{**" + repr({TYPE_NAME: OCT_TYPE}) + ",'k':" + repr(secret) + "}"
    if allow == "secret":
        body += " # ua:allow"
    text = "note = " + quote + opening + newline + body + newline + quote
    if allow == "closing":
        text += " # ua:allow"
    assert secret not in redact(text)
    assert flagged_lines(text) == {2}
    assert pr._jwk_findings(text)
    path = tmp_path / "fixture.py"
    path.write_text(text)
    findings = [
        item
        for item in run_audit(path, include_repo_rules=False)["findings"]
        if item["rule"] == "SECRETS-KEY"
    ]
    assert {item["line"] for item in findings} == (set() if allow == "secret" else {2})


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("prefix", ["", "r", "b"])
@pytest.mark.parametrize("join", [False, True])
def test_decoded_audit_offsets_survive_escapes_and_nested_strings(tmp_path, quote, prefix, join):
    from unified_agent.audit.engine import run_audit

    secret = secrets.token_hex(4)
    body = "{**" + repr({TYPE_NAME: OCT_TYPE}) + ",'k':" + repr(secret) + "}"
    if join and prefix != "r":
        opening = r"# ua:allow\u03a9\n" + "\\\n"
    else:
        opening = "# ua:allow\n"
    inner = '"' * 3 if quote == "'" * 3 else "'" * 3
    nested = prefix + inner + opening + body + "\n" + inner
    text = quote + "{'note':" + nested + "}" + quote
    # A literal physical newline cannot occur in a bytes-literal source containing
    # non-ASCII text; the Unicode escape above remains ASCII source.
    ast.parse(text)
    assert secret not in redact(text)
    assert line_of(text, secret) in flagged_lines(text)
    if prefix != "b" or not join:
        assert flagged_lines(text) == {line_of(text, secret)}
    path = tmp_path / "fixture.py"
    path.write_text(text)
    lines = {
        item["line"]
        for item in run_audit(path, include_repo_rules=False)["findings"]
        if item["rule"] == "SECRETS-KEY"
    }
    assert line_of(text, secret) in lines


def test_byte_snippet_audit_covers_both_halves_of_a_split_codepoint(tmp_path):
    from unified_agent.audit.engine import run_audit

    character = chr(0x400 + secrets.randbelow(0x100))
    pieces = [f"\\x{value:02x}" for value in character.encode("utf-8")]
    assert len(pieces) == 2
    text = "note = b'''# note\\n{**" + repr({TYPE_NAME: OCT_TYPE}) + ",'k':'"
    text += pieces[0] + "\\\n" + pieces[1] + "'} # ua:allow\n'''"
    ast.parse(text)
    assert all(piece not in redact(text) for piece in pieces)
    assert flagged_lines(text) == {1, 2}
    path = tmp_path / "fixture.py"
    path.write_text(text)
    findings = [
        item
        for item in run_audit(path, include_repo_rules=False)["findings"]
        if item["rule"] == "SECRETS-KEY"
    ]
    assert {item["line"] for item in findings} == {1}


@pytest.mark.parametrize("allow", ["opening", "secret"])
def test_nested_json_surrogate_pair_keeps_physical_attribution(tmp_path, allow):
    from unified_agent.audit.engine import run_audit

    secret = secrets.token_hex(4)
    payload = "meta " + chr(0x1F600) + " " + json.dumps({TYPE_NAME: OCT_TYPE, "k": secret})
    body = json.dumps({"payload": payload}).replace("\\", "\\\\")
    split = body.index(TYPE_NAME)
    opening = "# ua:allow\\n" if allow == "opening" else "# note\\n"
    text = "note = '''" + opening + body[:split] + "\\\n" + body[split:] + "'''"
    if allow == "secret":
        text += " # ua:allow"
    ast.parse(text)
    assert secret not in redact(text)
    assert flagged_lines(text) == {2}
    path = tmp_path / "fixture.py"
    path.write_text(text)
    findings = [
        item
        for item in run_audit(path, include_repo_rules=False)["findings"]
        if item["rule"] == "SECRETS-KEY"
    ]
    assert {item["line"] for item in findings} == (set() if allow == "secret" else {2})


@pytest.mark.parametrize("tail", ["\\", r"\x", r"\xg"])
def test_unfinished_byte_snippet_keeps_auditing_later_material(tmp_path, tail):
    from unified_agent.audit.engine import run_audit

    secret = secrets.token_hex(4)
    text = "note = b'''# ua:allow\n{**" + repr({TYPE_NAME: OCT_TYPE})
    text += ",'k':'" + secret + tail
    assert secret not in redact(text)
    assert 2 in flagged_lines(text)
    path = tmp_path / "fixture.py"
    path.write_text(text)
    assert any(
        item["rule"] == "SECRETS-KEY" and item["line"] == 2
        for item in run_audit(path, include_repo_rules=False)["findings"]
    )


def test_overlapping_decoded_audit_extents_have_bounded_deduplication(monkeypatch):
    count = 256
    text = 'log = "' + "\n".join(repr({TYPE_NAME: OCT_TYPE, "k": "v"}) for _ in range(count)) + '"'

    class CountedLines(set):
        checks = 0

        def __contains__(self, value):
            self.checks += 1
            return super().__contains__(value)

    seen = CountedLines()
    monkeypatch.setattr(rules, "set", lambda: seen, raising=False)
    findings = list(rules.check_secrets("fixture.txt", text))
    assert {finding.line for finding in findings} == set(range(1, count + 1))
    assert seen.checks <= 3 * count
