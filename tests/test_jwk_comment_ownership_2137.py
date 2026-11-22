"""Python comments own their JWKs without changing the surrounding mapping."""

from __future__ import annotations

import ast
import json
import secrets

import pytest
from growth_budget import assert_cpu_growth, cold, peak_bytes

from unified_agent.audit.rules import check_secrets
from unified_agent.telemetry import jwk
from unified_agent.telemetry.redaction import redact_sensitive_content
from unified_agent.tools.public_relay import BLOCK, _jwk_findings

TYPE = "kt" + "y"
OCT = "o" + "ct"


@pytest.fixture
def bootstrap_test_store():
    return False


@pytest.fixture(autouse=True)
def _forbid_store_access(isolated_env, monkeypatch):
    from unified_agent import config, storage

    def refused(*args, **kwargs):
        pytest.fail("pure JWK comment tests must not access a database")

    for name in ("init_db", "connect", "connect_read_only"):
        monkeypatch.setattr(storage, name, refused)
    assert not config.db_path().exists()
    yield
    assert not config.db_path().exists()


def fake() -> str:
    return "q" + secrets.token_hex(8) + "z"


def protect(text: str, private: str, *, python: bool = True) -> str:
    if python:
        ast.parse(text)
    members = jwk.private_members(text)
    output = redact_sensitive_content(text, max_len=20 * len(text) + 1000)
    masked = private not in output
    assert masked, "comment ownership exposed a private value"
    if python:
        ast.parse(output)
    expected = {i for i, line in enumerate(text.splitlines(), 1) if private in line}
    reported = {finding.line for finding in check_secrets("fixture.py", text)}
    assert expected <= reported
    findings = _jwk_findings(text)
    assert findings and all(finding.action == BLOCK for finding in findings)
    covered = any(private in text[member.start : member.end] for member in members)
    assert covered, "private member replacement lost source ownership"
    blocked = any(private in text[finding.start : finding.end] for finding in findings)
    assert blocked, "relay finding lost source ownership"
    return output


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "comment",
    [
        "'" + TYPE + "': 'RSA',",
        '"' + TYPE + '": "RSA",',
        "}",
        "{",
        "} ) ] { ( [",
        "'" + TYPE + "': 'RSA', # \"unclosed",
        "'unclosed",
    ],
)
@pytest.mark.parametrize("placement", ["after-type", "after-private", "after-expression"])
def test_inline_comment_cannot_retype_open_close_or_hide_mapping(comment, placement, newline):
    private = fake()
    prefix = "{" + repr(TYPE) + ":'oct',"
    value = "'k':" + repr(private)
    if placement == "after-type":
        text = prefix + " # " + comment + newline + value + "}"
    elif placement == "after-private":
        text = prefix + value + ", # " + comment + newline + "}"
    else:
        text = prefix + "'k': Secret(" + repr(private) + ")# " + comment + newline + "}"
    protect(text, private)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("with_comma", [False, True])
def test_comment_after_plain_type_value_preserves_type(newline, with_comma):
    private = fake()
    comma = "," if with_comma else ""
    text = "{" + repr(TYPE) + ":'oct'" + comma + "# '" + TYPE + "':'RSA'," + newline
    text += ("" if with_comma else ",") + "'k':" + repr(private) + "}"
    protect(text, private)


@pytest.mark.parametrize("layers", [0, 1, 3])
@pytest.mark.parametrize("shape", ["mapping", "unpack", "encoded", "triple", "keyword"])
@pytest.mark.parametrize("owner", ["public", "private", "none"])
def test_complete_comment_secrets_keep_independent_ownership(shape, owner, layers):
    private, public = fake(), fake()
    comment = repr({TYPE: OCT, "k": private})
    if shape == "unpack":
        comment = "{**" + repr({TYPE: OCT}) + ", 'k':" + repr(private) + "}"
    elif shape == "encoded":
        comment = json.dumps(comment)
    elif shape == "triple":
        comment = "'''" + comment + "'''"
    elif shape == "keyword":
        comment = "dict(" + TYPE + "='oct', k=" + repr(private) + ")"
    if owner == "none":
        text = "# " + comment
    else:
        text = "{" + repr(TYPE) + ":" + repr("RSA" if owner == "public" else OCT)
        text += ", 'k':" + repr(public) + ", # " + comment + "\n}"
    for _ in range(layers):
        text = json.dumps({"log": text})
    output = protect(text, private)
    if owner == "public" and layers == 0:
        preserved = public in output
        assert preserved, "comment key type changed an unrelated public mapping"


@pytest.mark.parametrize("comment", ["'" + TYPE + "':'oct',", "{", "}", '"unclosed'])
def test_comment_type_cannot_make_public_fields_private(comment):
    public = fake()
    text = repr({TYPE: "RSA", "k": public})[:-1] + ", # " + comment + "\n}"
    ast.parse(text)
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_unclosed_comment_quote_at_eof_cannot_hide_previous_secret(ending):
    private = fake()
    text = repr({TYPE: OCT, "k": private}) + " # '\"unclosed" + ending
    protect(text, private)


@pytest.mark.parametrize("prefix", ["#", "left#right", "\\#", "'''#'''"])
def test_quoted_hash_is_value_content(prefix):
    private = prefix + fake()
    text = repr({TYPE: OCT, "k": private, "note": "# " + TYPE + ": RSA"})
    protect(text, private)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("encoded", [False, True])
def test_multiline_snippet_comment_members_have_physical_source_attribution(newline, encoded):
    first, second = fake(), fake()
    body = "{" + repr(TYPE) + ":'oct', # } '" + TYPE + "':'RSA'," + newline
    body += "'k':" + repr(first) + ", # " + repr({TYPE: OCT, "k": second}) + newline + "}"
    if encoded:
        body = json.dumps({"log": body})
    text = "snippet = '''" + newline + body + newline + "'''"
    protect(text, first)
    protect(text, second)
    for private in (first, second):
        at = text.index(private)
        attributed = any(
            low <= at < high
            for member in jwk.private_members(text)
            for low, high in [jwk.audit_span(member, text)]
        )
        assert attributed, "decoded comment lost physical source extent"


@pytest.mark.parametrize("cut", ["", "}", " # '\"unfinished"])
def test_malformed_log_excerpt_retains_private_detection(cut):
    private = fake()
    text = "log: {" + repr(TYPE) + ":'oct', # } '" + TYPE + "':'RSA',\n'k':" + repr(private) + cut
    protect(text, private, python=False)


def cost_text(shape: str, size: int) -> str:
    if shape == "escaped-orphan-run":
        return "# '" + "\\'" * size + "words {**" + repr({TYPE: OCT}) + ", 'k':'value'}"
    if shape == "unfinished-formatted":
        return "# f'words " + "text " * size + "{**" + repr({TYPE: OCT}) + ", 'k':'value'}"
    if shape == "short-ambiguous-comments":
        return "# '\\'words {**" + repr({TYPE: OCT}) + ", 'k':'value'}\n" + "#\n" * size
    if shape == "many-ambiguous-comments":
        return ("# '\\'words {**" + repr({TYPE: OCT}) + ", 'k':'value'}\n") * size
    if shape == "extra-hash-run":
        return "# " + "#" * size + " {**" + repr({TYPE: OCT}) + ", 'k':'value'}"
    if shape == "many-extra-hashes":
        return "# " + "# " * size + "dict(**" + repr({TYPE: OCT}) + ", k='value')"
    if shape == "orphan-quote-prefix":
        return "# '" + "words " * size + "{**" + repr({TYPE: OCT}) + ", 'k':'value'}"
    if shape == "formatted-hash-tails":
        return ("# ## {" + repr(TYPE) + ":'oct', 'k':f'{fn('safe')}#value'}\n") * size
    if shape == "long-comment":
        return repr({TYPE: OCT, "k": "value"}) + " #" + "# '" * size
    if shape == "short-comments":
        return "# " + repr({TYPE: OCT, "k": "value"}) + "\n" + "#\n" * size
    if shape == "many-comments":
        return ("# " + repr({TYPE: OCT, "k": "value"}) + "\n") * size
    if shape == "type-only-comments":
        return ("# " + repr({TYPE: "RSA"}) + "\n") * size
    if shape == "multiline-mapping":
        return "{" + repr(TYPE) + ":'oct',\n" + ("'k':'value', # }\n" * size) + "}"
    return "'''" + ("# " + repr({TYPE: OCT, "k": "value"}) + r"\n") * size + "'''"


@pytest.mark.parametrize(
    "shape",
    [
        "escaped-orphan-run",
        "unfinished-formatted",
        "short-ambiguous-comments",
        "many-ambiguous-comments",
        "extra-hash-run",
        "many-extra-hashes",
        "orphan-quote-prefix",
        "formatted-hash-tails",
        "long-comment",
        "short-comments",
        "many-comments",
        "type-only-comments",
        "multiline-mapping",
        "triple",
    ],
)
def test_comment_growth_and_peak_preserve_existing_budgets(shape):
    small, large = (cost_text(shape, n) for n in (2000, 8000))
    # CPU time, best of interleaved samples: runner load cannot decide it (#2188).
    assert_cpu_growth(cold(jwk.private_members), small, large, limit=8, label=shape)

    def budget(found):
        return 48 * len(large) + 400 * len(found)

    peak, members = peak_bytes(cold(jwk.private_members), large, budget=budget)
    assert peak < budget(members)
    assert sum(len(member.value) for member in members) <= len(large)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("gap", ["", "x = 1\n", "# ordinary note\n"])
@pytest.mark.parametrize("encoded", [False, True])
def test_multiline_commented_mapping_keeps_its_own_source(newline, reverse, gap, encoded):
    private, public = fake(), fake()
    lines = [repr(TYPE) + ":'oct',", "'k':" + repr(private) + ","]
    if reverse:
        lines.reverse()
    comments = "# {\n# " + lines[0] + "\n" + gap + "# " + lines[1] + "\n# }"
    text = repr({TYPE: "RSA", "k": public}) + "\n" + comments
    text = text.replace("\n", newline)
    if encoded:
        text = json.dumps({"log": text})
    output = protect(text, private)
    if not encoded:
        preserved = public in output
        assert preserved, "comment mapping retyped surrounding public source"
        expected = text.index(private)
        spans = [jwk.audit_span(member, text) for member in jwk.private_members(text)]
        assert any(low <= expected < high for low, high in spans)


def test_comment_inside_private_expression_does_not_copy_values_twice():
    private = "q" + secrets.token_hex(1000) + "z"
    comment = repr({TYPE: OCT, "k": private})
    text = "{" + repr(TYPE) + ":'oct','k':call( # " + comment + "\n 'arg')}"
    protect(text, private)
    members = jwk.private_members(text)
    assert sum(len(member.value) for member in members) <= len(text)
    assert len(members) == 1


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("gap", ["", "x = 1\n", "# ordinary comment\n"])
@pytest.mark.parametrize("shape", ["mapping", "encoded", "triple"])
@pytest.mark.parametrize("kty", ["RSA", OCT])
def test_unterminated_comment_ownership_cannot_exclude_later_live_call(newline, gap, shape, kty):
    private = fake()
    comment = "{" + repr(TYPE) + ":" + repr(kty) + ","
    if shape == "encoded":
        comment = repr(comment + "\n unfinished")
    elif shape == "triple":
        comment = "'" * 3 + comment + "'" * 3
    text = "x = { # " + comment + "\n}\n" + gap
    text += "y = dict(" + TYPE + "='oct', k=" + repr(private) + ")"
    protect(text.replace("\n", newline), private)


@pytest.mark.parametrize("layers", [0, 1, 3])
def test_unterminated_multiline_comment_cannot_reserve_noncomment_source(layers):
    private = fake()
    text = "x = { # {\n# " + repr(TYPE) + ":'RSA',\n}\n"
    text += "y = dict(" + TYPE + "='oct', k=" + repr(private) + ")"
    for _ in range(layers):
        text = json.dumps({"log": text})
    protect(text, private)


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("newline", [r"\n", r"\r", r"\x0a", r"\u000a", r"\U0000000a"])
def test_raw_triple_comment_still_detects_direct_typed_private_fields(quote, newline):
    private = fake()
    snippet = "# comment" + newline + repr({TYPE: OCT, "k": private})
    text = "snippet = r" + quote + snippet + quote
    protect(text, private)


@pytest.mark.parametrize("quote", ["'" * 3, '"' * 3])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_raw_triple_physical_newline_allows_real_unpack_ownership(quote, newline):
    private = fake()
    snippet = "# comment" + newline + "{**" + repr({TYPE: OCT}) + ", 'k':" + repr(private) + "}"
    text = "snippet = r" + quote + snippet + quote
    protect(text, private)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_partially_overlapping_comment_and_live_members_cover_the_live_tail(newline, layers):
    private = fake()
    text = "# {" + repr(TYPE) + ":'oct','k':(\nx={" + repr(TYPE) + ":'oct','k':(\n"
    text += "'prefix' # )}\n" + repr(private) + "\n)}"
    text = text.replace("\n", newline)
    ast.parse(text)
    raw_members = jwk.private_members(text)
    assert len(raw_members) == 2
    first, second = raw_members
    assert first.start < second.start < first.end < second.end
    for _ in range(layers):
        text = json.dumps({"log": text})
    protect(text, private)


@pytest.mark.parametrize(
    ("spans", "markers"),
    [
        ([(2, 7), (5, 10)], 1),
        ([(2, 6), (4, 8), (7, 12)], 1),
        ([(2, 12), (4, 7), (8, 10)], 1),
        ([(2, 7), (2, 5), (5, 10), (10, 13)], 2),
    ],
    ids=["partial", "chain", "contained", "equal-start-and-touching"],
)
@pytest.mark.parametrize("inline", [(False, False), (True, True), (False, True), (True, False)])
def test_overlapping_replacement_spans_consume_union_without_exposing_tail(
    monkeypatch, spans, markers, inline
):
    from unified_agent.telemetry.redaction import _redact_jwk_members

    text = "abcdefghijklmnop"
    members = [
        jwk.PrivateMember("k", text[low:high], low, high, inline[index % 2])
        for index, (low, high) in enumerate(spans)
    ]
    monkeypatch.setattr(jwk, "private_members", lambda source, **_: members)
    out = _redact_jwk_members(text)
    low, high = spans[0][0], max(end for _, end in spans)
    # Touching spans may use another marker; neither partial nor contained
    # overlap may restore source bytes from inside the masked union.
    for char in text[low:high]:
        assert char not in out
    assert out.startswith(text[:low]) and out.endswith(text[high:])
    assert out.count("[REDACTED_SECRET]") == markers


@pytest.mark.parametrize("inline", [False, True])
def test_inline_and_whole_containment_preserve_json_shape(monkeypatch, inline):
    private = fake()
    text = json.dumps({"private": "prefix " + private + " suffix", "public": "kept"})
    low, high = text.index('"prefix'), text.index('suffix"') + len('suffix"')
    if inline:
        low, high = low + 1, high - 1
    inside = text.index(private)
    members = [
        jwk.PrivateMember("k", text[low:high], low, high, inline),
        jwk.PrivateMember("k", private, inside, inside + len(private), not inline),
    ]
    monkeypatch.setattr(jwk, "private_members", lambda source, **_: members)
    out = protect(text, private)
    value = json.loads(out)
    assert value == {"private": "[REDACTED_SECRET]", "public": "kept"}


@pytest.mark.parametrize(
    "prefix", ["# ", "## ", "### ", "ordinary # ", "ordinary# ", "\\# ", "#" * 128]
)
@pytest.mark.parametrize(
    "shape", ["mapping", "unpack", "computed", "call-unpack", "call-computed", "keyword"]
)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_nested_hashes_have_one_comment_payload_context(prefix, shape, newline):
    private, live = fake() + "#quoted", fake()
    source = repr({TYPE: OCT})
    if shape == "mapping":
        payload = repr({TYPE: OCT, "note": "#literal", "k": private})
    elif shape == "unpack":
        payload = "{**" + source + ", 'k':" + repr(private) + "}"
    elif shape == "computed":
        payload = "{**(" + source + " | other), 'k':" + repr(private) + "}"
    elif shape == "call-unpack":
        payload = "dict(**" + source + ", k=" + repr(private) + ")"
    elif shape == "call-computed":
        payload = "dict(**(" + source + " | other), k=" + repr(private) + ")"
    else:
        payload = "dict(" + TYPE + "='oct', k=" + repr(private) + ")"
    ast.parse(payload)
    text = "x=dict(" + TYPE + "='oct', # " + prefix + payload + newline
    text += "k=" + repr(live) + ")"
    protect(text, private)
    protect(text, live)


@pytest.mark.parametrize("break_text", [r"\n", r"\r", r"\x0a", "\n", "\r\n"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_extra_hash_context_survives_actual_and_literal_newlines(break_text, layers):
    private = fake()
    payload = "{**" + repr({TYPE: OCT}) + ", 'k':" + repr(private) + "}"
    text = "# ## prefix" + break_text + "# ## " + payload
    for _ in range(layers):
        text = json.dumps({"log": text})
    protect(text, private)


@pytest.mark.parametrize("prefix", ["f", "fr", "rf", "t", "tr", "rt"])
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
def test_hash_projection_preserves_complete_formatted_literal_and_reused_quotes(prefix, quote):
    private, live = fake(), fake()
    literal = prefix + quote + "{fn('safe')}#" + private + quote
    payload = "{" + repr(TYPE) + ":'oct', 'k':" + literal + "}"
    ast.parse(payload)
    text = "x=dict(" + TYPE + "='oct', # ## " + payload + "\n k=" + repr(live) + ")"
    protect(text, private)
    protect(text, live)


@pytest.mark.parametrize(
    "prefix", ["'", '"', "'unclosed prose ", '"unclosed prose ', "## '", '## "']
)
@pytest.mark.parametrize("shape", ["unpack", "computed", "call-unpack", "call-computed"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_malformed_comment_quotes_recover_before_all_helper_parsers(prefix, shape, newline):
    private, public = fake() + "#quoted", fake()
    source = repr({TYPE: OCT})
    if shape == "unpack":
        payload = "{**" + source + ", 'k':" + repr(private) + "}"
    elif shape == "computed":
        payload = "{**(" + source + " | other), 'k':" + repr(private) + "}"
    elif shape == "call-unpack":
        payload = "dict(**" + source + ", k=" + repr(private) + ")"
    else:
        payload = "dict(**(" + source + " | other), k=" + repr(private) + ")"
    text = "x=dict(" + TYPE + "='RSA', # " + prefix + payload + newline + " k=" + repr(public) + ")"
    out = protect(text, private)
    preserved = public in out
    assert preserved, "comment payload recovery changed a public live value"
    at = text.index(private)
    attributed = any(
        low <= at < high
        for member in jwk.private_members(text)
        for low, high in [jwk.audit_span(member, text)]
    )
    assert attributed, "projected comment lost physical audit attribution"


@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
@pytest.mark.parametrize("prefix", ["", "r", "b", "f", "fr", "rf", "t", "tr", "rt"])
def test_comment_projection_retains_quoted_hash_bytes_and_physical_offsets(prefix, quote):
    private = fake() + "#quoted"
    literal = prefix + quote + private + quote
    text = "## " + literal + " #\r\n"
    projected, ambiguous = jwk._comment_content(text)
    assert not ambiguous
    assert len(projected) == len(text)
    assert projected[3 : 3 + len(literal)] == literal
    assert projected[:3].isspace() and projected[3 + len(literal) :].isspace()
    assert projected.endswith("\r\n")


def ambiguous_prefix(name: str) -> str:
    quote = '"' if "double" in name else "'"
    if name.startswith("escaped"):
        count = 4 if "run" in name else 1
        return quote + ("\\" + quote) * count + "words " + ("# " if "hash" in name else "")
    if name == "multiple-orphans":
        return "'" + "\\'" + 'words "\\"words '
    if name == "triple-single":
        return "'" * 3 + "words "
    if name == "raw-triple-double":
        return "r" + '"' * 3 + "words "
    prefix = name.split("-")[0]
    return prefix + quote + "words "


@pytest.mark.parametrize(
    "prefix_name",
    [
        "escaped-single",
        "escaped-double",
        "escaped-single-run",
        "escaped-double-run",
        "escaped-single-hash",
        "multiple-orphans",
        "f-single",
        "rf-single",
        "t-single",
        "rt-double",
        "triple-single",
        "raw-triple-double",
    ],
)
@pytest.mark.parametrize("shape", ["unpack", "computed", "call-unpack", "call-computed"])
@pytest.mark.parametrize("layers", [0, 1, 3])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("owner", ["RSA", OCT])
def test_ambiguous_comment_recovery_keeps_lexical_ownership(
    prefix_name, shape, layers, nested, owner
):
    private, live, outside = fake() + "#quoted", fake(), fake()
    source = repr({TYPE: OCT})
    if shape == "unpack":
        payload = "{**" + source + ", 'k':" + repr(private) + "}"
    elif shape == "computed":
        payload = "{**(" + source + " | other), 'k':" + repr(private) + "}"
    elif shape == "call-unpack":
        payload = "dict(**" + source + ", k=" + repr(private) + ")"
    else:
        payload = "dict(**(" + source + " | other), k=" + repr(private) + ")"
    text = "x=dict(" + TYPE + "=" + repr(owner) + ", # " + ambiguous_prefix(prefix_name) + payload
    text += "\n k=" + repr(live) + ")"
    if nested:
        quote = "'" * 3 if '"' * 3 in text else '"' * 3
        text = "snippet = " + quote + "\n" + text + "\n" + quote
    text += "\npublic = " + repr(outside)
    for _ in range(layers):
        text = json.dumps({"log": text, "public": outside})
    out = protect(text, private)
    preserved = outside in out
    assert preserved, "ambiguous comment ownership masked a separate public sibling"
    if owner == OCT:
        protect(text, live)
    elif not layers and not nested:
        preserved = live in out
        assert preserved, "ambiguous comment ownership reached a live public argument"
    if not layers:
        at = text.index(private)
        attributed = any(
            low <= at < high
            for member in jwk.private_members(text)
            for low, high in [jwk.audit_span(member, text)]
        )
        assert attributed, "ambiguous comment mask lost original physical source attribution"


@pytest.mark.parametrize("count", [1, 2, 16, 128, 2048])
@pytest.mark.parametrize("prefix", ["", "r", "b", "u", "f", "rf", "t", "rt"])
def test_quote_escape_runs_do_not_need_repeated_normalization(count, prefix):
    private, public = fake(), fake()
    orphan = prefix + "'" + "\\'" * count + "words "
    payload = "{**" + repr({TYPE: OCT}) + ", 'k':" + repr(private) + "}"
    text = "x=dict(" + TYPE + "='RSA', # " + orphan + payload + "\n k=" + repr(public) + ")"
    out = protect(text, private)
    preserved = public in out
    assert preserved, "quote-run recovery changed live public ownership"


@pytest.mark.parametrize("prefix", ["f", "fr", "rf", "t", "tr", "rt"])
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
def test_unclosed_formatted_comment_literal_is_explicitly_ambiguous(prefix, quote):
    private = fake()
    text = prefix + quote + "words {**" + repr({TYPE: OCT}) + ", 'k':" + repr(private) + "}"
    _, ambiguous = jwk._comment_content(text)
    assert ambiguous
    source = "# " + text + "\npublic = 'kept'"
    out = protect(source, private)
    assert "public = 'kept'" in out


@pytest.mark.parametrize("prefix", ["f", "fr", "rf", "t", "tr", "rt"])
@pytest.mark.parametrize("quote", ["'", '"', "'" * 3, '"' * 3])
@pytest.mark.parametrize("key_quotes", ["single", "double"])
def test_complete_formatted_comment_key_constructions_keep_one_owner(prefix, quote, key_quotes):
    private, public = fake(), fake()
    if key_quotes == "single":
        source = repr({TYPE: OCT})
        field = ", 'k':" + repr(private)
    else:
        source = json.dumps({TYPE: OCT})
        field = ', "k":' + json.dumps(private)
    literal = prefix + quote + "words { {**" + source + field + "} }" + quote
    ast.parse(literal)
    text = "x=dict(" + TYPE + "='RSA', # " + literal + "\n k=" + repr(public) + ")"
    out = protect(text, private)
    preserved = public in out
    assert preserved, "formatted comment ambiguity changed a live public value"


def escaped_word(word: str, style: str) -> str:
    import unicodedata

    if style == "unicode":
        return "".join("\\u" + format(ord(char), "04x") for char in word)
    if style == "hex":
        return "".join("\\x" + format(ord(char), "02x") for char in word)
    if style == "octal":
        return "".join("\\" + format(ord(char), "03o") for char in word)
    if style == "named":
        return "".join("\\N{" + unicodedata.name(char) + "}" for char in word)
    return word


@pytest.mark.parametrize("style", ["plain", "unicode", "hex", "octal", "named"])
@pytest.mark.parametrize(
    "kty,member", [(OCT, "k"), ("RSA", "d"), ("RSA", "p"), ("EC", "d"), ("OKP", "d")]
)
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_ambiguous_recovery_keeps_escaped_field_and_type_semantics(style, kty, member, layers):
    private = fake()
    key = "'" + escaped_word(TYPE, style) + "'"
    value = "'" + escaped_word(kty, style) + "'"
    field = "'" + escaped_word(member, style) + "'"
    text = "# '\\'words {" + key + ":" + value + ", " + field + ":" + repr(private) + "}"
    for _ in range(layers):
        text = json.dumps({"log": text})
    protect(text, private)


@pytest.mark.parametrize("kty,member", [("RSA", "k"), ("EC", "p"), ("OKP", "q")])
@pytest.mark.parametrize("prefix", ["'\\'words ", "f'words ", "rf'words ", "t'words "])
def test_ambiguous_comment_recovery_preserves_known_public_member_roles(kty, member, prefix):
    public = fake()
    text = "# " + prefix + repr({TYPE: kty, member: public})
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize("placeholder", ["...", "REDACTED", "****", "${TOKEN}"])
def test_ambiguous_recovery_retains_exact_placeholder_identity(placeholder):
    text = "# '\\'words " + repr({TYPE: OCT, "k": placeholder})
    members = jwk.private_members(text)
    assert members and all(member.value == placeholder for member in members)
    assert not list(check_secrets("fixture.py", text))
    findings = _jwk_findings(text)
    if placeholder in ("REDACTED", "${TOKEN}"):
        assert not findings
    else:
        assert findings and all(finding.action == BLOCK for finding in findings)


@pytest.mark.parametrize(
    "type_value",
    [
        "no actual type",
        "octagon",
        "oct-extra",
        "oct extra",
        "RSA-extra",
        "RSA extra",
        "EC-extra",
        "OKP extra",
    ],
)
def test_malformed_prose_with_type_name_without_real_type_evidence_is_kept(type_value):
    public = fake()
    text = "# '\\'words " + repr({TYPE: type_value, "k": public, "d": public}) + "\npublic = 'kept'"
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_escaped_orphan_comment_recovery_keeps_original_physical_line(newline):
    private, public = fake(), fake()
    text = "# unrelated prose" + newline
    text += "x=dict(" + TYPE + "='RSA', # '\\'words " + repr({TYPE: OCT, "k": private}) + newline
    text += "k=" + repr(public) + ")"
    out = protect(text, private)
    preserved = public in out
    assert preserved
    assert {finding.line for finding in check_secrets("fixture.py", text)} == {2}


@pytest.mark.parametrize("prefix", ["'\\'words ", "f'words ", "rf'words ", "t'words "])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("gap", [" ", "\n# ", "\ncode = 'kept'\n# "])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_ambiguous_comment_type_evidence_stays_with_its_object(prefix, reverse, gap, layers):
    private, public, outside = fake(), fake(), fake()
    pieces = [repr({TYPE: OCT, "k": private}), repr({TYPE: "RSA", "k": public})]
    if reverse:
        pieces.reverse()
    text = "# " + prefix + gap.join(pieces) + "\npublic = " + repr(outside)
    for _ in range(layers):
        text = json.dumps({"log": text, "public": outside})
    out = protect(text, private)
    if not layers:
        assert public in out
    assert outside in out


@pytest.mark.parametrize("public_type,field", [("RSA", "k"), ("EC", "p"), ("OKP", "q")])
@pytest.mark.parametrize("nested", [False, True])
def test_ambiguous_nested_public_objects_do_not_inherit_unrelated_types(public_type, field, nested):
    private, public = fake(), fake()
    child = repr({TYPE: public_type, field: public})
    payload = "{" + repr(TYPE) + ":'oct', 'k':" + repr(private)
    payload += ", 'note':" + (child if nested else "'kept'") + "}"
    if not nested:
        payload += " " + child
    text = "# '\\'words " + payload
    out = protect(text, private)
    assert public in out


@pytest.mark.parametrize("call", [False, True])
@pytest.mark.parametrize("known_literal", [False, True])
def test_ambiguous_computed_unpacks_keep_existing_uncertain_ownership(call, known_literal):
    private, public = fake(), fake()
    operand = "(" + repr({TYPE: "RSA"}) + " | unknown)" if known_literal else "make_key()"
    if call:
        payload = "dict(**" + operand + ", kty='RSA', k=" + repr(private) + ")"
    else:
        payload = "{**" + operand + ", " + repr(TYPE) + ":'RSA', 'k':" + repr(private) + "}"
    ast.parse(payload)
    text = "# '\\'words " + payload + "\n# " + repr({TYPE: "RSA", "k": public})
    out = protect(text, private)
    assert public in out


@pytest.mark.parametrize("middle", ["}", "]", ")", "}])", "#{])"])
def test_private_expression_literals_keep_braces_inside_their_value(middle):
    private, public = fake() + middle + fake(), fake()
    text = "# '\\'words {" + repr(TYPE) + ":'oct', 'k':fn(" + repr(private) + ")}"
    text += "\n# " + repr({TYPE: "RSA", "k": public})
    out = protect(text, private)
    assert public in out


@pytest.mark.parametrize("ragged", ["]", ")", "])"])
def test_ragged_comment_closers_never_end_another_frame_kind(ragged):
    private = fake()
    text = "# '\\'words {" + repr(TYPE) + ":'oct', " + ragged + " 'k':" + repr(private)
    protect(text, private)


@pytest.mark.parametrize("shape", ["mapping", "unpack", "keyword", "call-unpack"])
@pytest.mark.parametrize("encoding", ["json", "repr", "hex", "unicode", "type-delimiters"])
@pytest.mark.parametrize("prefix", ["", "f'words ", "rf'words ", "t'words "])
@pytest.mark.parametrize("tail", [' "unclosed', '\n# "unclosed', "\ncode = 'kept'\n# '\\'words"])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_complete_comment_literals_keep_decoding_beside_ambiguous_regions(
    shape, encoding, prefix, tail, layers
):
    private, public = fake(), fake()
    if shape == "mapping":
        payload = repr({TYPE: OCT, "k": private})
    elif shape == "unpack":
        payload = "{**" + repr({TYPE: OCT}) + ", 'k':" + repr(private) + "}"
    elif shape == "keyword":
        payload = "dict(" + TYPE + "='oct', k=" + repr(private) + ")"
    else:
        payload = "dict(**" + repr({TYPE: OCT}) + ", k=" + repr(private) + ")"
    if encoding == "json":
        literal = json.dumps(payload)
    elif encoding == "repr":
        literal = repr(payload)
    elif encoding in ("hex", "unicode"):
        escape, width = ("x", 2) if encoding == "hex" else ("u", 4)
        literal = (
            '"'
            + "".join(
                char
                if char in private
                else "\\" + escape + format(ord(char), "0" + str(width) + "x")
                for char in payload
            )
            + '"'
        )
    else:
        literal = '"' + payload.replace("'oct'", r"\u0027oct\u0027") + '"'
    text = "# " + prefix + literal + tail + "\npublic = " + repr(public)
    for _ in range(layers):
        text = json.dumps({"log": text, "public": public})
    out = protect(text, private)
    assert public in out
    if layers:
        json.loads(out)
    elif "code = 'kept'" in text:
        assert "code = 'kept'" in out


@pytest.mark.parametrize("style", ["hex", "unicode", "octal", "named"])
@pytest.mark.parametrize(
    "kty,field,private_role",
    [(OCT, "k", True), ("RSA", "d", True), ("RSA", "k", False), ("EC", "p", False)],
)
@pytest.mark.parametrize("layers", [0, 1])
def test_double_quoted_python_escapes_keep_field_roles_and_source(
    style, kty, field, private_role, layers
):
    value, public = fake(), fake()

    def quote(word):
        return '"' + escaped_word(word, style) + '"'

    payload = "{" + quote(TYPE) + ":" + quote(kty) + ", " + quote(field) + ":" + repr(value) + "}"
    text = "public = " + repr(public) + "\nkey = " + payload
    ast.parse(text)
    if layers:
        text = json.dumps({"log": text, "public": public})
    if private_role:
        out = protect(text, value)
        if not layers:
            assert {finding.line for finding in check_secrets("fixture.py", text)} == {2}
    else:
        assert not jwk.private_members(text)
        assert not list(check_secrets("fixture.py", text))
        assert not _jwk_findings(text)
        out = redact_sensitive_content(text, max_len=10000)
        assert out == text
    assert public in out
    if layers:
        json.loads(out)


@pytest.mark.parametrize("style", ["hex", "octal", "named"])
def test_shared_literal_reader_keeps_raw_escape_bytes(style):
    spelling = escaped_word(TYPE, style)
    assert jwk._string('r"' + spelling + '"') == spelling


def hash_payload(kind_literal, field, value, shape, reverse=False, hashes="#"):
    typed = repr(TYPE) + ":" + kind_literal
    private = repr(field) + ":" + repr(value)
    if shape == "keyword":
        typed = TYPE + "=" + kind_literal
        private = field + "=" + repr(value)
    if shape in ("unpack", "call-unpack"):
        typed = "**{" + typed + hashes + " type note\n}"
        if shape == "call-unpack":
            private = field + "=" + repr(value)
    entries = [typed, private]
    if reverse:
        entries.reverse()
    opening, closing = ("dict(", ")") if shape in ("keyword", "call-unpack") else ("{", "}")
    return opening + (hashes + " note\n,").join(entries) + hashes + " tail\n" + closing


@pytest.mark.parametrize(
    "kind,field,private_role",
    [
        (OCT, "k", True),
        ("RSA", "d", True),
        ("EC", "d", True),
        ("OKP", "d", True),
        ("RSA", "k", False),
        ("EC", "p", False),
    ],
)
@pytest.mark.parametrize("shape", ["mapping", "unpack", "keyword", "call-unpack"])
@pytest.mark.parametrize("prefix", ["", "'\\'words ", "f'words ", "rf'words ", "t'words "])
@pytest.mark.parametrize("layout", ["type-first", "private-first", "live-gap"])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_hash_boundaries_keep_comment_type_and_value_ownership(
    kind, field, private_role, shape, prefix, layout, layers
):
    value, public = fake() + "#inside", fake()
    payload = hash_payload(repr(kind), field, value, shape, layout == "private-first")
    ast.parse(payload)
    lines = ["# " + line for line in payload.splitlines()]
    lines[0] = "# " + prefix + lines[0][2:]
    if layout == "live-gap":
        lines.insert(1, "kept = " + repr(public))
    text = "\n".join(lines) + '\n# "unclosed\npublic = ' + repr(public)
    for _ in range(layers):
        text = json.dumps({"log": text, "public": public})
    if private_role:
        out = protect(text, value)
    else:
        assert not jwk.private_members(text)
        assert not list(check_secrets("fixture.py", text))
        assert not _jwk_findings(text)
        out = redact_sensitive_content(text, max_len=100000)
        assert out == text
    assert public in out
    if layers:
        json.loads(out)


@pytest.mark.parametrize("kind", [OCT, "RSA", "EC", "OKP"])
@pytest.mark.parametrize(
    "spelling",
    [
        "single",
        "double",
        "triple-single",
        "triple-double",
        "raw",
        "unicode",
        "bytes",
        "formatted",
        "template",
    ],
)
@pytest.mark.parametrize("hashes", ["#", "##", "###"])
@pytest.mark.parametrize("shape", ["mapping", "keyword"])
def test_quoted_type_spellings_share_the_same_hash_separator(kind, spelling, hashes, shape):
    private = fake()
    if spelling == "single":
        literal = repr(kind)
    elif spelling == "double":
        literal = json.dumps(kind)
    elif spelling == "triple-single":
        literal = "'''" + kind + "'''"
    elif spelling == "triple-double":
        literal = '"""' + kind + '"""'
    else:
        prefix = {"raw": "r", "unicode": "u", "bytes": "b", "formatted": "f", "template": "t"}[
            spelling
        ]
        literal = prefix + repr(kind)
    field = "k" if kind == OCT else "d"
    payload = hash_payload(literal, field, private, shape, hashes=hashes)
    ast.parse(payload)
    text = "\n".join("# " + line for line in payload.splitlines()) + '\n# "unclosed'
    protect(text, private)


@pytest.mark.parametrize(
    "kind", ["oct#inside", "RSA#inside", "oct-extra", "RSA-extra", "oct extra"]
)
@pytest.mark.parametrize("quote", ["'", '"', "'''", '"""'])
def test_a_hash_inside_the_quoted_type_never_supplies_a_boundary(kind, quote):
    value = fake()
    literal = quote + kind + quote
    text = "# " + hash_payload(literal, "k", value, "mapping").replace("\n", "\n# ")
    text += '\n# "unclosed'
    assert not jwk.private_members(text)
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)
    assert redact_sensitive_content(text, max_len=100000) == text


@pytest.mark.parametrize("placeholder", ["...", "REDACTED", "****", "${TOKEN}"])
def test_hash_after_a_private_literal_preserves_its_placeholder_identity(placeholder):
    text = "\n".join(
        "# " + line for line in hash_payload(repr(OCT), "k", placeholder, "mapping").splitlines()
    )
    text += '\n# "unclosed'
    members = jwk.private_members(text)
    assert members and all(member.value == placeholder for member in members)
    assert not list(check_secrets("fixture.py", text))
    findings = _jwk_findings(text)
    if placeholder in ("REDACTED", "${TOKEN}"):
        assert not findings
    else:
        assert findings and all(finding.action == BLOCK for finding in findings)


@pytest.mark.parametrize("note", ["}", "{", "} {", repr(TYPE) + ":'RSA'", repr(TYPE) + ":'oct'"])
@pytest.mark.parametrize("prefix", ["", "'\\'words ", "f'words "])
def test_inline_note_structure_never_owns_the_enclosing_comment_mapping(note, prefix):
    private, public = fake(), fake()
    text = "# " + prefix + "{" + repr(TYPE) + ":'oct'# " + note
    text += "\n# ,'k':" + repr(private) + '}\n# "unclosed\npublic = ' + repr(public)
    out = protect(text, private)
    assert public in out


@pytest.mark.parametrize(
    "shape", ["function", "list", "object", "encoded-private", "malformed-private"]
)
@pytest.mark.parametrize("unpacked", [False, True])
def test_proven_public_expressions_keep_independent_nested_private_evidence(shape, unpacked):
    private, public = fake(), fake()
    if shape == "function":
        value = "make_public(" + repr(public) + ")"
    elif shape == "list":
        value = "[" + repr(public) + "]"
    elif shape == "object":
        value = repr({TYPE: OCT, "k": private, "note": public})
    elif shape == "encoded-private":
        value = repr(repr({TYPE: OCT, "k": private, "note": public}))
    else:
        value = repr("# '\\'words {**" + repr({TYPE: OCT}) + ", 'k':" + repr(private) + "}")
    owner = "**" + repr({TYPE: "RSA"}) if unpacked else repr(TYPE) + ":'RSA'"
    text = "# '\\'words {" + owner + "# } { " + repr(TYPE) + ":'oct'\n# ,'k':" + value + "}"
    text += '\n# "unclosed\npublic = ' + repr(public)
    if shape in ("object", "encoded-private", "malformed-private"):
        out = protect(text, private)
        assert public in out or shape != "object"
        # A decoded private value may replace the enclosing public literal.
        assert "public = " + repr(public) in out
    else:
        assert not jwk.private_members(text)
        assert not list(check_secrets("fixture.py", text))
        assert not _jwk_findings(text)
        assert redact_sensitive_content(text, max_len=100000) == text


@pytest.mark.parametrize("computed", [False, True])
@pytest.mark.parametrize("prefix", ["'\\'words ", "f'words ", "rf'words ", "t'words "])
def test_orphan_recovery_cannot_prove_public_before_a_private_unpack_override(prefix, computed):
    private = fake()
    source = repr({TYPE: OCT})
    if computed:
        source = "(" + source + " | unknown)"
    text = "# " + prefix + "{" + repr(TYPE) + ":'RSA', **" + source
    text += "# note\n# ,'k':" + repr(private) + '}\n# "unclosed'
    protect(text, private)


@pytest.mark.parametrize("unpacked", [False, True])
@pytest.mark.parametrize("prefix", ["", "'\\'words ", "f'words "])
def test_a_private_unpack_inside_a_trailing_note_remains_independent(unpacked, prefix):
    private, public = fake(), fake()
    owner = "**" + repr({TYPE: "RSA"}) if unpacked else repr(TYPE) + ":'RSA'"
    note = "{**" + repr({TYPE: OCT}) + ", 'k':" + repr(private) + "}"
    text = "# " + prefix + "{" + owner + "# " + note
    text += "\n# ,'k':" + repr(public) + '}\n# "unclosed'
    out = protect(text, private)
    assert public in out


@pytest.mark.parametrize("private_role", [False, True])
@pytest.mark.parametrize("shape", ["mapping", "unpack", "keyword", "call-unpack"])
@pytest.mark.parametrize("orphan", ["none", "same-line", "other-paragraph", "before"])
@pytest.mark.parametrize("note_shape", ["opposite-type", "close", "open", "ragged", "private"])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_inline_note_ownership_does_not_depend_on_unrelated_orphan(
    private_role, shape, orphan, note_shape, layers
):
    value, note_private, public = fake(), fake(), fake()
    kind = OCT if private_role else "RSA"
    opposite = "RSA" if private_role else OCT
    note = repr(TYPE) + ":" + repr(opposite)
    if note_shape == "close":
        note = "} " + note
    elif note_shape == "open":
        note = "{ " + note
    elif note_shape == "ragged":
        note = ") ] } { ( [ " + note
    elif note_shape == "private":
        note = "{**" + repr({TYPE: OCT}) + ", 'k':" + repr(note_private) + "}"
    typed = repr(TYPE) + ":" + repr(kind)
    field = "'k':" + repr(value)
    if shape in ("keyword", "call-unpack"):
        field = "k=" + repr(value)
        if shape == "keyword":
            typed = TYPE + "=" + repr(kind)
    if shape in ("unpack", "call-unpack"):
        typed = "**{" + typed + "}"
    opening, closing = ("dict(", ")") if shape in ("keyword", "call-unpack") else ("{", "}")
    payload = opening + typed + "# " + note + "\n," + field + closing
    ast.parse(payload)
    text = "\n".join("# " + line for line in payload.splitlines())
    if orphan == "same-line":
        text += ' "unclosed'
    elif orphan == "other-paragraph":
        text += '\n\n# "unclosed'
    elif orphan == "before":
        text = '# "unclosed\n\n' + text
    text += "\npublic = " + repr(public)
    for _ in range(layers):
        text = json.dumps({"log": text, "public": public})
    if private_role:
        out = protect(text, value)
    elif note_shape != "private":
        assert not jwk.private_members(text)
        assert not list(check_secrets("fixture.py", text))
        assert not _jwk_findings(text)
        out = redact_sensitive_content(text, max_len=100000)
        assert out == text
    else:
        out = redact_sensitive_content(text, max_len=100000)
    if note_shape == "private":
        out = protect(text, note_private)
        if not private_role and not layers:
            assert value in out
    assert public in out
    if layers:
        json.loads(out)


@pytest.mark.parametrize(
    "shape,prefix",
    [
        (shape, prefix)
        for shape in (
            "unknown",
            "computed",
            "literal-override",
            "final-public",
            "grouped-public",
            "sibling-public",
            "nested-private",
        )
        for prefix in ("", "'\\'words ", "rf'words ")
        if (shape, prefix) != ("final-public", "rf'words ")
    ],
)
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_comment_call_proof_reuses_unpack_ownership_and_uncertainty(shape, prefix, layers):
    private, public, kept = fake(), fake(), fake()
    typed = TYPE + "='RSA'"
    private_role = shape in ("unknown", "computed", "literal-override")
    value = repr(private if private_role else public)
    if shape == "unknown":
        typed += ", **unknown"
    elif shape == "computed":
        typed += ", **(" + repr({TYPE: "RSA"}) + " | unknown)"
    elif shape == "literal-override":
        typed += ", **" + repr({TYPE: OCT})
    elif shape == "final-public":
        typed = "**" + repr({TYPE: OCT}) + ", " + typed
    elif shape == "grouped-public":
        typed = "**(" + repr({TYPE: "RSA"}) + ")"
    elif shape == "sibling-public":
        typed = repr({TYPE: OCT}) + ", " + typed
    else:
        value = "[" + repr({TYPE: OCT, "k": private}) + ", " + repr(public) + "]"
    text = "# " + prefix + "dict(" + typed + "# } ] { " + repr(TYPE) + ":'oct'"
    text += "\n# ,k=" + value + ")\npublic = " + repr(kept)
    for _ in range(layers):
        text = json.dumps({"log": text, "public": kept})
    if private_role or shape == "nested-private":
        out = protect(text, private)
        if shape == "nested-private" and not layers:
            assert public in out
    else:
        assert not jwk.private_members(text)
        assert not list(check_secrets("fixture.py", text))
        assert not _jwk_findings(text)
        out = redact_sensitive_content(text, max_len=100000)
        assert out == text
    assert kept in out
    if layers:
        json.loads(out)


def test_recovered_formatted_prefix_does_not_own_the_final_public_call_value():
    public, kept = fake(), fake()
    text = "# rf'words dict(**" + repr({TYPE: OCT}) + ", " + TYPE + "='RSA'"
    text += "# } ] { " + repr(TYPE) + ":'oct'\n# ,k=" + repr(public) + ")"
    text += "\npublic = " + repr(kept)
    # The earlier formatted fragment may independently produce conservative
    # findings. It cannot take the later field's proven public value or code.
    out = redact_sensitive_content(text, max_len=100000)
    assert public in out and kept in out


@pytest.mark.parametrize("body_shape", ["mapping", "keyword", "encoded"])
@pytest.mark.parametrize("quote", ["'''", '"""'])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_normal_triple_descendants_retain_source_and_encoded_depth(body_shape, quote, layers):
    private, public = fake(), fake()
    if body_shape == "keyword":
        body = "dict(" + TYPE + "='oct', k=" + repr(private) + ")"
    else:
        body = repr({TYPE: "RSA", "d": private})
        if body_shape == "encoded":
            body = json.dumps(body)
    if quote in body:
        body = body.replace(quote, "\\" + quote)
    value = quote + body + quote
    text = "# '\\'words {" + repr(TYPE) + ":'RSA', 'k':" + value + "}"
    text += '\n# "unclosed\npublic = ' + repr(public)
    for _ in range(layers):
        text = json.dumps({"log": text, "public": public})
    out = protect(text, private)
    assert public in out
    if layers:
        json.loads(out)


def test_ordinary_encoded_literals_keep_the_existing_three_layer_budget():
    # The comment's literal sits past the three-layer decode bound, which stays
    # in place: it is not decoded again. Since task #2124 (review at 79723aa6)
    # a string left undecoded that holds a key type and a private member name
    # fails closed instead of passing its private value through.
    private = fake()
    body = repr({TYPE: "RSA", "d": private})
    literal = (
        '"'
        + "".join(char if char in private else "\\u" + format(ord(char), "04x") for char in body)
        + '"'
    )
    text = "# " + literal + '\n# "unclosed'
    for _ in range(3):
        text = json.dumps({"log": text, "public": "kept"})
    out = protect(text, private, python=False)
    assert json.loads(out)["public"] == "kept"
