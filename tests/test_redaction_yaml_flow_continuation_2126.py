"""YAML double-quoted continuations decide a flow JWK's type (task #2126).

In a YAML double-quoted scalar a backslash before a physical line break escapes
the break, and the next line's leading blanks are dropped; an unescaped break
folds to one space. ``{kty: "oct\\<LF>    ", k: ...}`` is therefore an oct
key to every YAML loader. The JSON scan decided that object with Python string
semantics, which keep the indentation, so its type read as something other
than ``oct``, the object stayed decided and no member was masked.

Every key-shaped fixture is generated at runtime and the type names are
assembled, so no line of this file opens a block for the security audit.
"""

from __future__ import annotations

import json
import secrets

import pytest
import yaml
from growth_budget import assert_cpu_growth, cold, peak_growth

from unified_agent.audit.rules import check_secrets
from unified_agent.telemetry import jwk
from unified_agent.telemetry.redaction import redact_sensitive_content
from unified_agent.tools.public_relay import BLOCK, _jwk_findings

TYPE = "kt" + "y"
OCT = "o" + "ct"
KEPT = "kept-value-2126"


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


def fake() -> str:
    return "q" + secrets.token_hex(8) + "z"


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


def protect(text: str, private: str) -> str:
    """All three consumers hide or report ``private``, on its own line."""
    out = redact(text)
    assert private not in out, out
    assert any(private in text[m.start : m.end] for m in jwk.private_members(text))
    assert lines_of(text, private) <= jwk_lines(text)
    findings = _jwk_findings(text)
    assert any(f.action == BLOCK and private in text[f.start : f.end] for f in findings)
    return out


BREAKS = {"lf": "\n", "crlf": "\r\n", "cr": "\r", "nel": "\x85"}
SPELLINGS = {
    "literal": ["o", "c", "t"],
    "hex-middle": ["o", "\\x63", "t"],
    "all-escaped": ["\\x6f", "\\u0063", "\\U00000074"],
}


def continued(units: list[str], brk: str, count: int, indent: str = "    ") -> str:
    """A double-quoted scalar spelling ``units`` with ``count`` escaped breaks."""
    gap = "\\" + brk + indent
    if count == 1:
        # The task's reproduction: the break and its indentation trail the type.
        return '"' + "".join(units) + gap + '"'
    assert count == 3
    return '"' + gap + units[0] + gap + units[1] + gap + "\t" + units[2] + '"'


CONTEXTS = {
    "flow-in-block": "outer: {{{kty}: {v}, k: {s}}}",
    "flow-top": "{{{kty}: {v}, k: {s}}}",
    "flow-quoted-names": '{{"{kty}": {v}, "k": "{s}"}}',
    "nested-flow": "a:\n  b: {{c: {{{kty}: {v}, k: {s}, kid: {kept}}}}}",
    "flow-sequence": "keys: [{{{kty}: RSA, n: pub}}, {{{kty}: {v}, k: {s}}}]",
    "k-first": "outer: {{k: {s}, {kty}: {v}}}",
    "block": "{kty}: {v}\nk: {s}\nkid: {kept}",
    "block-nested": "jwk:\n  {kty}: {v}\n  k: {s}",
}


def build(context: str, value: str, private: str) -> str:
    return CONTEXTS[context].format(kty=TYPE, v=value, s=private, kept=KEPT)


def loaded_key(text: str) -> dict:
    """The mapping PyYAML builds that carries the key type."""
    pending = [yaml.safe_load(text)]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if TYPE in node and node[TYPE] != "RSA":
                return node
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    raise AssertionError("no typed mapping")


@pytest.mark.parametrize("context", list(CONTEXTS))
@pytest.mark.parametrize("spelling", list(SPELLINGS))
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("brk", list(BREAKS))
def test_a_continued_oct_type_masks_its_key(brk, count, spelling, context):
    private = fake()
    text = build(context, continued(SPELLINGS[spelling], BREAKS[brk], count), private)
    key = loaded_key(text)
    assert key[TYPE] == OCT and key["k"] == private  # the fixture is a real oct JWK
    out = protect(text, private)
    if "{kept}" in CONTEXTS[context]:
        assert KEPT in out or context.startswith("block")


@pytest.mark.parametrize("brk", list(BREAKS))
def test_the_task_reproduction_is_masked(brk):
    private = fake()
    text = f'outer: {{{TYPE}: "{OCT}\\{BREAKS[brk]}    ", k: {private}}}'
    assert yaml.safe_load(text)["outer"] == {TYPE: OCT, "k": private}
    out = protect(text, private)
    # The decided object keeps the JSON path's member-only masking.
    assert out.startswith(f'outer: {{{TYPE}: "{OCT}\\')
    assert out.endswith('k: "[REDACTED_SECRET]"}')


@pytest.mark.parametrize("brk", list(BREAKS))
def test_a_continued_type_inside_an_encoded_string_is_masked(brk):
    private = fake()
    inner = build("flow-top", continued(SPELLINGS["literal"], BREAKS[brk], 3), private)
    assert loaded_key(inner)["k"] == private
    text = json.dumps({"log": inner})
    out = protect(text, private)
    assert json.loads(out).keys() == {"log"}


def test_other_key_types_keep_their_public_members():
    private, ec_private = fake(), fake()
    rsa = f'{{{TYPE}: "R\\\n    SA", n: pub, e: AQAB, d: {private}}}'
    assert yaml.safe_load(rsa)[TYPE] == "RSA"
    out = protect(rsa, private)
    assert "n: pub" in out and "e: AQAB" in out
    ec = f'{{{TYPE}: "\\\n  E\\\n  C", crv: P-256, x: pub, d: {ec_private}}}'
    assert yaml.safe_load(ec)[TYPE] == "EC"
    out = protect(ec, ec_private)
    assert "x: pub" in out and "crv: P-256" in out


def test_a_continued_private_member_name_is_masked():
    # An explicit flow key may span lines; its name decodes the same way.
    private = fake()
    text = f'{{{TYPE}: "RSA", n: pub, ? "\\\n   d" : {private}}}'
    assert yaml.safe_load(text) == {TYPE: "RSA", "n": "pub", "d": private}
    protect(text, private)


@pytest.mark.parametrize("kind", ["RSA", OCT])
def test_a_continued_type_name_is_read_as_the_type(kind):
    private = fake()
    name = '"k\\\n   t\\\n   y"'
    member = "d" if kind == "RSA" else "k"
    value = '"R\\\n  SA"' if kind == "RSA" else '"o\\\n  ct"'
    text = f"{{? {name} : {value}, n: pub, {member}: {private}}}"
    assert yaml.safe_load(text) == {TYPE: kind, "n": "pub", member: private}
    out = protect(text, private)
    if kind == "RSA":
        assert "n: pub" in out


def test_audit_attribution_stays_on_the_private_line():
    private = fake()
    text = f'outer: {{\n  {TYPE}: "o\\\n      c\\\n      t",\n  kid: {KEPT},\n  k: {private}\n}}\n'
    assert yaml.safe_load(text)["outer"][TYPE] == OCT
    protect(text, private)
    # Only the k line is attributed to the JWK rule; the type lines are not.
    assert jwk_lines(text) == lines_of(text, private)
    finding = next(f for f in _jwk_findings(text) if private in text[f.start : f.end])
    assert finding.detail == "JWK symmetric key"
    # A bare flow value runs to its separator, as for any bare value.
    assert text[finding.start : finding.end].strip() == private


# --- JSON parity -------------------------------------------------------------------


def test_a_json_object_with_an_escaped_backslash_newline_type_is_unchanged():
    # JSON semantics: the type is "oct\\\n   " (a backslash and a newline), not
    # oct, in every reading. The decided object stays byte for byte.
    private = fake()
    text = json.dumps({TYPE: OCT + "\\\n   ", "k": private})
    assert json.loads(text)[TYPE] != OCT and yaml.safe_load(text)[TYPE] != OCT
    assert jwk.private_members(text) == []
    assert redact(text) == text


def test_json_and_python_types_keep_their_decision():
    private = fake()
    for text in (
        json.dumps({TYPE: OCT, "k": private, "kid": KEPT}),
        repr({TYPE: OCT, "k": private, "kid": KEPT}),
    ):
        out = protect(text, private)
        assert KEPT in out
    # A JSON type that is not a key type still decides the object unmasked.
    text = json.dumps({TYPE: "oct-like", "k": KEPT})
    assert redact(text) == text


def test_an_unescaped_break_folds_to_a_space_and_is_not_a_type():
    # YAML reads "oct\n  " as "oct ": not a JWK, so nothing new is masked.
    text = f'{{{TYPE}: "{OCT}\n  ", k: {KEPT}}}'
    assert yaml.safe_load(text)[TYPE] == OCT + " "
    assert jwk.private_members(text) == []


# --- The YAML decoder against PyYAML -------------------------------------------------

DECODER_CASES = [
    '"plain"',
    '"a\\\n   b"',
    '"a\\\r\n\t  b"',
    '"a\\\r b"',
    '"a\\\x85 b"',
    '"a  \\\n  b"',
    '"a\\\n\n   b"',
    '"a\\\n  \n \n b"',
    '"a\n b"',
    '"a   \n   b"',
    '"a\n\n b"',
    '"a\n \n\n b"',
    '"\\x6f\\u0063\\U00000074"',
    '"\\0\\a\\b\\t\\\t\\n\\v\\f\\r\\e\\ \\"\\/\\\\"',
    '"\\N\\_\\L\\P"',
    '"a\u2028 b"',
    '"a\\\u2029  b"',
]


@pytest.mark.parametrize("raw", DECODER_CASES)
def test_the_yaml_reading_matches_pyyaml(raw):
    assert jwk._yaml_double_quoted(raw) == yaml.safe_load(raw)


@pytest.mark.parametrize("raw", ['"\\q"', '"\\x6"', '"\\u00"', "'oct'", '"""oct"""'])
def test_the_yaml_reading_refuses_what_yaml_cannot_read(raw):
    assert jwk._yaml_double_quoted(raw) is None


# --- Growth ----------------------------------------------------------------------------


def _many_continuations(n: int) -> str:
    gap = "\\\n" + " " * 6
    rows = (f'- {{{TYPE}: "o{gap}c{gap}t", k: value{i}}}\n' for i in range(n))
    return "keys:\n" + "".join(rows)


def _one_long_type(n: int) -> str:
    return f'{{{TYPE}: "o' + "\\\n  " * n + 'ct", k: value}'


GROWING = {"many-objects": _many_continuations, "one-long-type": _one_long_type}


@pytest.mark.parametrize("name", list(GROWING))
def test_continuation_decoding_grows_linearly(name):
    small, large = GROWING[name](250), GROWING[name](1_000)
    assert len(large) >= 3.5 * len(small)
    assert_cpu_growth(cold(redact), small, large, limit=8, label=name)


@pytest.mark.parametrize("name", list(GROWING))
def test_continuation_decoding_peak_follows_the_cost_model(name):
    small, large = GROWING[name](250), GROWING[name](1_000)
    growth = peak_growth(
        cold(jwk.private_members),
        small,
        large,
        limit=8,
        budget=lambda found: 48 * len(large) + 400 * len(found),
    )
    assert growth.large < 48 * len(large) + 400 * len(growth.result)
    assert growth.ratio < 8
    if name == "many-objects":
        assert len(growth.result) == 1_000


# --- Review follow-ups (task #2200) ---------------------------------------------------


@pytest.mark.parametrize(("first", "last"), [("RSA", OCT), (OCT, "RSA")])
def test_a_second_continued_type_key_masks_the_union_of_both_types(first, last):
    # PyYAML keeps the last type. A repeated type masks the union of every
    # type's private members (task #2200, review r2): the last type's members
    # are masked as YAML reads them, and the first type's are over-masked.
    values = {name: fake() for name in ("d", "p", "k")}
    text = (
        f'{{{TYPE}: "{first}", ? "\\\n   {TYPE}" : "{last}", '
        f"d: {values['d']}, p: {values['p']}, k: {values['k']}}}"
    )
    assert yaml.safe_load(text) == {TYPE: last, **values}
    for name in ("d", "p", "k"):
        protect(text, values[name])


@pytest.mark.parametrize("raw", ['"\\xff"', '"\\uffff"', '"\\U0010ffff"'])
def test_the_yaml_reading_accepts_the_top_of_each_escape_width(raw):
    assert jwk._yaml_double_quoted(raw) == yaml.safe_load(raw)


def test_the_yaml_reading_refuses_a_code_point_over_the_unicode_limit():
    assert jwk._yaml_double_quoted('"\\U00110000"') is None
    with pytest.raises(ValueError):
        yaml.safe_load('"\\U00110000"')


@pytest.mark.parametrize("brk", ["\v", "\f"])
def test_an_escaped_vertical_tab_or_form_feed_break_over_masks_on_purpose(brk):
    # YAML does not accept these characters at all; the break set is the
    # oct-block rule's superset of YAML's, so the type reads as oct and the
    # key is masked. Intended over-masking, pinned so it cannot regress quietly.
    private = fake()
    text = f'{{{TYPE}: "o\\{brk}    ct", k: {private}}}'
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(text)
    assert jwk._yaml_double_quoted(f'"o\\{brk}    ct"') == OCT
    assert private not in redact(text)
    assert any(private in text[m.start : m.end] for m in jwk.private_members(text))


def test_a_decoder_run_pattern_that_cannot_match_does_not_raise(monkeypatch):
    import re

    monkeypatch.setattr(jwk, "_YAML_RUN", re.compile(r"(?!)"))
    assert jwk._yaml_double_quoted('"plain"') is None
    private = fake()
    text = f'{{{TYPE}: "o\\\n    ct", k: {private}}}'
    jwk.private_members(text)  # must not raise
    redact(text)
