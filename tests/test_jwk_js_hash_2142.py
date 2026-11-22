"""JavaScript literal hashes retain their live JWK's lexical owner.

Raw source ambiguity deliberately retains baseline privacy: the paired 83-case
review matrix protects 46 private cases, retains 32 conservative masks, and
leaves five public controls unchanged. It does not promise language validity.
"""

import ast
import json
import secrets

import pytest
from growth_budget import assert_cpu_growth, cold, peak_bytes

from unified_agent.audit.rules import check_secrets
from unified_agent.telemetry import jwk
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
        pytest.fail("pure JavaScript JWK tests must not access a database")

    for name in ("init_db", "connect", "connect_read_only"):
        monkeypatch.setattr(storage, name, refused)


def assert_private(text, value):
    members = private_members(text)
    findings = list(check_secrets("fixture.js", text))
    relay = _jwk_findings(text)
    expected = {i for i, line in enumerate(text.splitlines(), 1) if value in line}
    observed = {
        "redacted": value not in redact_sensitive_content(text, max_len=20 * len(text) + 1000),
        "scanner": any(value in text[m.start : m.end] for m in members),
        "audit": bool(expected) and expected <= {finding.line for finding in findings},
        "relay": any(value in text[f.start : f.end] and f.action == BLOCK for f in relay),
    }
    assert all(observed.values()), observed


@pytest.mark.parametrize(
    "note",
    [
        "/#/",
        "/[#]/",
        "/[{}#]/u",
        r"/\/#\//g",
        "`#`",
        "`#${1}`",
        r"`escaped \` #`",
        "`#${`nested #${1}`}`",
        "`#${({x: /[}#]/}).x}`",
        '`#${"}#"}`',
        "+/#/",
        "~/#/",
        "1 + /#/",
        "(() => /#/)()",
        "(() => {return /#/;})()",
        "/* # */ 1",
    ],
)
@pytest.mark.parametrize(
    "kind,member", [("RSA", "d"), ("RSA", "p"), ("EC", "d"), ("OKP", "d"), ("oct", "k")]
)
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_js_literal_hash_cannot_hide_private_member(note, kind, member, before, layers):
    value = "q" + secrets.token_hex(12) + "z"
    fields = "kty:" + repr(kind) + "," + member + ":" + repr(value)
    text = (
        "const x = {"
        + (fields + ",note:" + note if before else "note:" + note + "," + fields)
        + "};"
    )
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("literal", ["`# <value>`", '`#${"<value>"}`', "/[#},]<value>/"])
def test_private_javascript_expression_consumes_complete_literal(literal):
    value = "q" + secrets.token_hex(12) + "z"
    text = "const x = {kty:'RSA', d:" + literal.replace("<value>", value) + ", n:'public'};"
    output = redact_sensitive_content(text, max_len=10000)
    assert_private(text, value)
    assert "n:'public'" in output


@pytest.mark.parametrize("shape", ["`# {<body>}`", "/#{<body>}/", "`#${({<body>})}`"])
def test_javascript_literal_contents_retain_independent_private_evidence(shape):
    value = "q" + secrets.token_hex(12) + "z"
    body = "kty:'RSA',d:" + repr(value)
    text = "const x = {note:" + shape.replace("<body>", body) + ",kty:'RSA',k:'public'};"
    assert_private(text, value)
    assert "k:'public'" in redact_sensitive_content(text, max_len=10000)


@pytest.mark.parametrize("depth", [1, 4, 16, 128])
def test_nested_template_source_is_not_an_encoded_string_layer(depth):
    value = "q" + secrets.token_hex(12) + "z"
    body = "{kty:'RSA',d:" + repr(value) + "}"
    text = "`" + "${`" * depth + body + "`}" * depth + "`"
    assert_private(text, value)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\u2028", "\u2029"])
def test_template_line_comments_use_all_javascript_line_terminators(newline):
    value = "q" + secrets.token_hex(12) + "z"
    note = "`#${1 // } ` ignored" + newline + "}`"
    text = "const x={note:" + note + ",kty:'RSA',d:" + repr(value) + "};"
    assert_private(text, value)


@pytest.mark.parametrize("declaration", ["const x=", "let x =\n", "var x = ", "const x = ("])
@pytest.mark.parametrize("keyword", ["typeof", "void", "delete"])
@pytest.mark.parametrize("split", [False, True])
def test_javascript_declaration_preserves_keyword_regexp_fields(declaration, keyword, split):
    value = "q" + secrets.token_hex(12) + "z"
    note = "note:" + keyword + " /#/"
    body = (
        "d:" + repr(value) + "," + note + ",kty:'RSA'"
        if split
        else note + ",kty:'RSA',d:" + repr(value)
    )
    text = declaration + "{" + body + "}" + (");" if declaration.endswith("(") else ";")
    assert_private(text, value)


@pytest.mark.parametrize("prefix", ["# const x =\n", "note='const x='\n", "const_other = {}\n"])
def test_unrelated_javascript_spelling_cannot_enable_python_comment_retyping(prefix):
    value = "q" + secrets.token_hex(12) + "z"
    text = prefix + "x={'kty':'oct','n':typeof / # /, 'kty':'RSA',\n 2,'k':" + repr(value) + "}"
    ast.parse(text)
    assert_private(text, value)


@pytest.mark.parametrize("keyword", ["typeof", "void", "delete"])
@pytest.mark.parametrize(
    "regexp",
    [
        "/#/",
        "/[#]/",
        "/#[a-z]/g",
        r"/\/#\//u",
        "/#.*/",
        "/#(?:x)/",
        r"/#\d+/",
        "/#(?=x)/",
        r"/#\p{ASCII}/u",
        r"/#(?<x>a)\k<x>/",
        r"/#\p{Script=Latin}/u",
    ],
)
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_valid_alternate_javascript_mapping_unions_private_spans(keyword, regexp, layers):
    value = "q" + secrets.token_hex(12) + "z"
    text = "({d:" + repr(value) + ", n:" + keyword + " " + regexp + ",kty:'RSA',\n denominator,})"
    if not layers and regexp.startswith("/#"):
        ast.parse(text, mode="eval")
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize(
    "tail",
    [
        "2",
        "throw",
        "case",
        "instanceof",
        "typeof",
        "void",
        "delete",
        "this",
        "null",
        "true",
        "false",
    ],
)
@pytest.mark.parametrize("regexp", ["/#/", "/[#]/"])
def test_malformed_alternate_mapping_retains_baseline_conservative_privacy(tail, regexp):
    value = "public" + secrets.token_hex(12) + "end"
    text = "({d:" + repr(value) + ",n:typeof " + regexp + ",kty:'RSA',\n " + tail + "})"
    # These are raw ambiguous/malformed excerpts. Baseline also protects the
    # private candidate; a partial JavaScript validity proof is no privacy gate.
    assert_private(text, value)


@pytest.mark.parametrize("regexp", ["/#/z", "/#/gg", "/#/uv", "/(/", "/[z-a#]/", "/[#/", "/#"])
def test_malformed_regexp_retains_baseline_conservative_privacy(regexp):
    value = "public" + secrets.token_hex(12) + "end"
    text = "({d:" + repr(value) + ",n:typeof " + regexp + ",kty:'RSA',\n denominator})"
    # These are raw ambiguous/malformed excerpts. Baseline also protects the
    # private candidate; a partial JavaScript validity proof is no privacy gate.
    assert_private(text, value)


@pytest.mark.parametrize("kind,member", [("RSA", "k"), ("EC", "p"), ("OKP", "q")])
def test_alternate_javascript_public_fields_stay_public(kind, member):
    value = "public" + secrets.token_hex(12) + "end"
    text = (
        "({" + member + ":" + repr(value) + ",n:typeof /#/,kty:" + repr(kind) + ",\n denominator})"
    )
    assert value in redact_sensitive_content(text, max_len=10000)
    assert not private_members(text)


@pytest.mark.parametrize(
    "suffix", [" + 1", " ? 1 : 2", " && 1", ".test('q')", '["source"]', " && (()=>1)()", ",...{}"]
)
def test_alternate_javascript_regexp_expression_is_not_a_size_or_grammar_cutoff(suffix):
    value = "q" + secrets.token_hex(12) + "z"
    text = "({d:" + repr(value) + ",n:typeof /#/" + suffix + ",kty:'RSA',\n denominator})"
    assert_private(text, value)


@pytest.mark.parametrize("padding", [5000, 12000])
def test_late_javascript_mapping_closure_keeps_its_private_fields(padding):
    value = "q" + secrets.token_hex(12) + "z"
    text = "({d:" + repr(value) + ",n:typeof /#/,kty:'RSA',\n" + " " * padding + "denominator})"
    assert_private(text, value)


@pytest.mark.parametrize("shape", ["siblings", "one-context", "nested-failure"])
def test_alternate_context_growth_and_peak_keep_existing_budgets(shape):
    value = "q" + secrets.token_hex(12) + "z"

    def source(size):
        if shape == "siblings":
            return (
                "["
                + ",\n".join(
                    "({d:" + repr(value) + ",n:typeof /#/,kty:'RSA',\n denominator})"
                    for _ in range(size)
                )
                + "]"
            )
        if shape == "one-context":
            return (
                "({d:"
                + repr(value)
                + ","
                + "\n".join(f"n{i}:typeof /#/," for i in range(size))
                + "\nkty:'RSA',denominator})"
            )
        return "({n:typeof /#/,\n child:" * size + "{bad:1,2}" + "})" * size

    small, text = source(200), source(800)
    # CPU time, best of interleaved samples: runner load cannot decide it (#2188).
    assert_cpu_growth(cold(private_members), small, text, limit=8, label=shape)

    def budget(found):
        return 48 * len(text) + 400 * len(found)

    peak, members = peak_bytes(cold(private_members), text, budget=budget)
    assert peak < budget(members)


@pytest.mark.parametrize("note", ["/#/", "/[#]/", "`#`", "`#${1}`"])
@pytest.mark.parametrize("kind,member", [("RSA", "k"), ("EC", "p"), ("OKP", "q")])
def test_javascript_note_does_not_retype_public_fields(note, kind, member):
    value = "public" + secrets.token_hex(12) + "end"
    text = "const x={note:" + note + ",kty:" + repr(kind) + "," + member + ":" + repr(value) + "};"
    assert value in redact_sensitive_content(text, max_len=10000)
    assert not private_members(text)
    assert not list(check_secrets("fixture.js", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize("comment", ["'kty':'RSA',", "} {", "`/", "'unclosed /#/"])
@pytest.mark.parametrize("operator", ["/", "//"])
def test_python_division_does_not_consume_a_real_comment(operator, comment):
    value = "q" + secrets.token_hex(12) + "z"
    text = "x={'kty':'oct', 'n': 4 " + operator + " # " + comment + "\n 2, 'k':" + repr(value) + "}"
    ast.parse(text)
    assert_private(text, value)
    ast.parse(redact_sensitive_content(text, max_len=10000))


@pytest.mark.parametrize("name", ["typeof", "void", "delete", "throw", "case", "instanceof"])
def test_javascript_word_spelling_can_still_be_a_python_division_operand(name):
    value = "q" + secrets.token_hex(12) + "z"
    text = "x={'kty':'oct','n':" + name + " / # /, 'kty':'RSA',\n 2,'k':" + repr(value) + "}"
    ast.parse(text)
    assert_private(text, value)


@pytest.mark.parametrize("size", [2000, 8000])
def test_malformed_regexp_class_and_nested_templates_remain_bounded(size):
    value = "q" + secrets.token_hex(12) + "z"
    text = "const x={note:/[" + "/#" * size + ",kty:'RSA',d:" + repr(value) + "};"
    assert jwk._javascript_literal(text, text.index("/"))[0] == len(text)
    nested = "`" + "${`" * size + "#" + "`}" * size + "`"
    assert jwk._javascript_literal(nested, 0)[0] == len(nested)


@pytest.mark.parametrize("shape", ["raw", "triple", "encoded"])
def test_alternate_private_span_keeps_physical_line_and_public_neighbor(shape):
    private = "q" + secrets.token_hex(12) + "z"
    public = "public" + secrets.token_hex(12) + "end"
    text = "({\n d:" + repr(private) + ",\n n:typeof /#/,kty:'RSA',k:" + repr(public)
    text += ",\n denominator})"
    if shape == "triple":
        text = "'''" + text + "'''"
    elif shape == "encoded":
        text = json.dumps(text)
    assert_private(text, private)
    if shape == "raw":
        assert public in redact_sensitive_content(text, max_len=10000)
        assert all(public not in text[m.start : m.end] for m in private_members(text))


@pytest.mark.parametrize("operand", ["4", "number", "(4)"])
@pytest.mark.parametrize(
    "comment", ["/," + "kt" + "y:'oct',", "{note:typeof /#/," + "kt" + "y:'oct'", "`#/${1}`"]
)
def test_real_python_division_comment_cannot_supply_alternate_parent_type(operand, comment):
    public = "public" + secrets.token_hex(12) + "end"
    text = "{'kty':'RSA','k':" + repr(public) + ", 'n':" + operand + " / # " + comment + "\n 2}"
    ast.parse(text)
    assert not private_members(text)
    assert public in redact_sensitive_content(text, max_len=10000)
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize("suffix", [" ?? 1 || 2", " ++ 1", " -- 1"])
@pytest.mark.parametrize("regexp", ["/#/", r"/#\k<missing>/u", r"/#[\k<missing>]/u"])
def test_unknown_expression_or_regexp_validity_does_not_veto_private_evidence(suffix, regexp):
    private = "q" + secrets.token_hex(12) + "z"
    text = "({d:" + repr(private) + ",n:typeof " + regexp + suffix + ",kty:'RSA',\n denominator})"
    # Deliberate baseline-compatible privacy for ambiguous/malformed raw text.
    assert_private(text, private)


@pytest.mark.parametrize(
    "expression", ["(()=>{if(1) /#/;})()", "(()=>{while(false) /#/;})()", "(()=>{if(1) {} /#/;})()"]
)
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_control_statement_regexp_cannot_hide_the_containing_private_jwk(expression, layers):
    private = "q" + secrets.token_hex(12) + "z"
    text = "({n:" + expression + ",kty:'RSA',d:" + repr(private) + ",k:'public'})"
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, private)
    if not layers:
        assert "k:'public'" in redact_sensitive_content(text, max_len=10000)


@pytest.mark.parametrize("comment", ["kt" + "y:'oct',", "} {", "'unclosed"])
def test_ambiguous_nested_metadata_retains_real_comment_and_sibling_public_roles(comment):
    private = "q" + secrets.token_hex(12) + "z"
    public = "public" + secrets.token_hex(12) + "end"
    text = "[{d:" + repr(private) + ",n:(()=>{typeof /#/;})(),kty:'RSA'},\n"
    text += "{kty:'RSA',k:" + repr(public) + ", # " + comment + "\n}]"
    text = "{keys:" + text + "}"
    assert_private(text, private)
    assert public in redact_sensitive_content(text, max_len=10000)
    assert all(public not in text[m.start : m.end] for m in private_members(text))


@pytest.mark.parametrize("layers", [0, 1, 3])
@pytest.mark.parametrize(
    "note",
    [
        "(()=>{if(true) /#/.test(''); return 1})()",
        "(()=>{if /*note*/ (true) /#/.test(''); return 1})()",
        "(()=>{if //note\n(true) /#/.test(''); return 1})()",
        "(()=>{if(true) /*note*/ /#/.test(''); return 1})()",
        "(()=>{if(true) //note\n/#/.test(''); return 1})()",
        "(()=>{if(true) /*#note*/ /#/.test(''); return 1})()",
        "(()=>{if(true) {} /*note*/ /#/.test(''); return 1})()",
        "(()=>{if(false) {} else /#/.test(''); return 1})()",
        "(()=>{if(false) {} else /*note*/ /#/.test(''); return 1})()",
        "(()=>{do /#/.test(''); while(false); return 1})()",
        "(()=>{function f(){} /#/.test(''); return 1})()",
        "(()=>{class C{} /#/.test(''); return 1})()",
        "(()=>{try{}finally{} /#/.test(''); return 1})()",
        "(()=>{try{}catch(e){} /#/.test(''); return 1})()",
        "(()=>{label: /#/.test(''); return 1})()",
        "(()=>{while /*note*/ (false) /#/.test(''); return 1})()",
        "(()=>{for /*note*/ (;false;) /#/.test(''); return 1})()",
        "`#${(()=>{if(true) /[}`#]/.test(''); return 1})()}`",
        "`#${(()=>{if(true) /['#]/.test(''); return 1})()}`",
        "`#${(()=>{if(true) /[({#]/.test(''); return 1})()}`",
        "`#${(()=>{if(true) /[)}#]/.test(''); return 1})()}`",
        "`#${(()=>{if(true) /[`#]/.test(''); return `#${1}`})()}`",
    ],
    ids=[
        "plain-if",
        "if-before-header-comment",
        "if-before-header-line-comment",
        "after-header-block-comment",
        "after-header-line-comment",
        "after-header-block-comment-hash",
        "after-block-comment",
        "else-regexp",
        "else-comment-regexp",
        "do-regexp",
        "function-declaration-regexp",
        "class-declaration-regexp",
        "try-finally-regexp",
        "try-catch-regexp",
        "label-regexp",
        "while-comment",
        "for-comment",
        "template-control-braces",
        "template-control-quote",
        "template-control-unbalanced-open",
        "template-control-unbalanced-close",
        "nested-template-control",
    ],
)
def test_shared_statement_and_template_boundaries_retain_private_source(note, layers):
    private = "q" + secrets.token_hex(12) + "z"
    text = "{note:" + note + ",kty:'RSA',d:" + repr(private) + ",k:'public-value'}"
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, private)
    if not layers:
        assert "k:'public-value'" in redact_sensitive_content(text, max_len=10000)


@pytest.mark.parametrize("name", ["do", "typeof", "void", "delete"])
@pytest.mark.parametrize("trivia", ["", "/*note*/"])
def test_alternate_statement_words_do_not_remove_primary_python_private_ownership(name, trivia):
    private = "q" + secrets.token_hex(12) + "z"
    text = "{'kty':'oct','n':" + name + " / # /,kty:'RSA',\n 2,'k':" + repr(private) + "}"
    if trivia:
        # Python reads the comment's trivia as inert payload; it cannot become
        # a source-language switch or remove the live oct field's primary mask.
        text = text.replace("# /", "# " + trivia + " /")
    ast.parse(text)
    assert_private(text, private)


@pytest.mark.parametrize("call", ["switch", "catch", "obj.switch", "obj.catch", "function", "do"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("role", ["private", "escaped", "public", "no-type"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_ambiguous_header_call_cannot_replace_python_comment_ownership(call, newline, role, layers):
    value = "q" + secrets.token_hex(12) + "z"
    if role == "no-type":
        head = "'ordinary':0"
    else:
        name = r"\x6bty" if role == "escaped" else "kty"
        kind = "oct" if role in {"private", "escaped"} else "RSA"
        head = "'" + name + "':" + repr(kind)
    text = (
        "{"
        + head
        + ",'n':"
        + call
        + "(1) / # /,'kty':'RSA',"
        + newline
        + "2,'k':"
        + repr(value)
        + ",'kid':'public-neighbor'}"
    )
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text)
    if role in {"private", "escaped"}:
        assert_private(text, value)
    else:
        assert_public_target(text, value)
    if not layers:
        assert "'kid':'public-neighbor'" in redact_sensitive_content(text, max_len=10000)


def assert_public_target(text, value):
    assert value in redact_sensitive_content(text, max_len=20 * len(text) + 1000)
    assert not any(value in text[m.start : m.end] for m in private_members(text))
    lines = {i for i, line in enumerate(text.splitlines(), 1) if value in line}
    assert not lines.intersection(f.line for f in check_secrets("fixture.py", text))
    assert not any(value in text[m.start : m.end] for m in _jwk_findings(text))


_STATEMENT_NOTES = {
    "standalone-block": "(()=>{{} /#/.test('');return 1})()",
    "semicolon-block": "(()=>{;{} /#/.test('');return 1})()",
    "labelled-block": "(()=>{label:{} /#/.test('');return 1})()",
    "optional-catch": "(()=>{try{throw 0}catch{} /#/.test('');return 1})()",
    "optional-catch-trivia": "(()=>{try{}catch /*note*/ {} /*note*/ /#/.test('');return 1})()",
    "for-await": "(async()=>{for await(const n of []) /#/.test('');return 1})()",
    "for-await-trivia": "(async()=>{for /*note*/ await /*note*/ (const n of []) /#/.test('');return 1})()",
    "async-function": "(()=>{async function f(){} /#/.test('');return 1})()",
    "generator": "(()=>{function* f(){} /#/.test('');return 1})()",
    "class-static": "(()=>{class C{static{}} /#/.test('');return 1})()",
    "else": "(()=>{if(false) {} else /#/.test('');return 1})()",
    "return": "(()=>{return /#/.test('')})()",
}


@pytest.mark.parametrize("name,note", list(_STATEMENT_NOTES.items()))
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_statement_body_regexp_retains_live_fields(name, note, before, layers):
    value = "q" + secrets.token_hex(12) + "z"
    fields = "kty:'RSA',d:" + repr(value)
    text = "{" + ("note:" + note + "," + fields if before else fields + ",note:" + note)
    text += ",k:'public-neighbor'}"
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)
    if not layers:
        assert "k:'public-neighbor'" in redact_sensitive_content(text, max_len=10000)


@pytest.mark.parametrize("note", list(_STATEMENT_NOTES.values())[:7])
@pytest.mark.parametrize("regexp", ["/['#]/", "/[`#]/", "/[)}#]/"])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_template_statement_regexp_cannot_own_outer_fields(note, regexp, before, layers):
    note = "`#${" + note.replace("/#/", regexp) + "}`"
    value = "q" + secrets.token_hex(12) + "z"
    fields = "kty:'RSA',d:" + repr(value)
    text = "{" + ("note:" + note + "," + fields if before else fields + ",note:" + note)
    text += ",k:'public-neighbor'}"
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)
    if not layers:
        assert "k:'public-neighbor'" in redact_sensitive_content(text, max_len=10000)


@pytest.mark.parametrize("call", ["switch", "catch", "obj.switch", "obj.catch"])
@pytest.mark.parametrize(
    "kind,member,fake", [("RSA", "k", "oct"), ("EC", "p", "RSA"), ("OKP", "q", "RSA")]
)
@pytest.mark.parametrize("unpacked", [False, True])
@pytest.mark.parametrize("expression", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_alternate_type_cannot_revoke_primary_public_or_unpack_proof(
    call, kind, member, fake, unpacked, expression, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    head = "'kty':" + repr(kind)
    if unpacked:
        head = "**{" + head + "}"
    operand = "(" + repr(value) + ")" if expression else repr(value)
    text = (
        "{"
        + head
        + ",'n':"
        + call
        + "(1) / # /,'kty':"
        + repr(fake)
        + ",\n2,"
        + repr(member)
        + ":"
        + operand
        + "}"
    )
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text)
    assert_public_target(text, value)


@pytest.mark.parametrize(
    "call",
    [
        "obj.function",
        "obj.do",
        "obj.typeof",
        "obj.void",
        "obj.delete",
        "obj['catch']",
        "obj.method",
        "(switch)",
        "getattr(obj,'catch')",
    ],
)
@pytest.mark.parametrize("operator", ["/", "//"])
@pytest.mark.parametrize("kind", ["oct", "RSA"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_property_and_indirect_calls_do_not_gain_control_header_authority(
    call, operator, kind, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    text = (
        "{'kty':"
        + repr(kind)
        + ",'n':"
        + call
        + "(1) "
        + operator
        + " # /,'kty':'RSA',\n2,'k':"
        + repr(value)
        + "}"
    )
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text)
    if kind == "oct":
        assert_private(text, value)
    else:
        assert_public_target(text, value)


_GENERAL_BODY_NOTES = [
    "({method(){ {} /#/.test('');return 1 }}).method()",
    "({async method(){ label:{} /#/.test('');return 1 }}).method()",
    "(()=>{class C{static{ {} /#/.test('') }};return 1})()",
    "(()=>{class C{method(){ {} /#/.test('');return 1 }};return new C().method()})()",
    "(()=>{function f() /*note*/ { first:second:{} /#/.test('');return 1 };return f()})()",
    "(async()=>{for await /*note*/ (const n of []) { label:{} /#/.test('') };return 1})()",
    "(()=>{try{}catch /*note*/ { label:{} /#/.test('') };return 1})()",
]


@pytest.mark.parametrize("note", _GENERAL_BODY_NOTES)
@pytest.mark.parametrize("regexp", ["/#/", "/['`({#]/"])
@pytest.mark.parametrize("template", [False, True])
@pytest.mark.parametrize("kind,member", [("RSA", "d"), ("oct", "k")])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_general_delimiter_bodies_share_source_and_template_boundaries(
    note, regexp, template, kind, member, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    note = note.replace("/#/", regexp)
    if template:
        note = "`#${" + note + "}`"
    text = "{note:" + note + ",kty:" + repr(kind) + "," + member + ":" + repr(value)
    text += ",kid:'public-neighbor'}"
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)
    if not layers:
        assert "kid:'public-neighbor'" in redact_sensitive_content(text, max_len=10000)


@pytest.mark.parametrize("condition", ["(1)", "(1 + 2)", "((1))"])
@pytest.mark.parametrize("comprehension", [False, True])
@pytest.mark.parametrize("kind", ["oct", "RSA"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_python_expression_if_does_not_prove_a_javascript_statement(
    condition, comprehension, kind, newline, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    prefix, suffix = ("[x for x in [1] if", "]") if comprehension else ("1 if", " else 0")
    text = (
        "{'kty':"
        + repr(kind)
        + ",'n':"
        + prefix
        + condition
        + " / # /,'kty':'RSA',"
        + newline
        + "2"
        + suffix
        + ",'k':"
        + repr(value)
        + "}"
    )
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text)
    if kind == "oct":
        assert_private(text, value)
    else:
        assert_public_target(text, value)


@pytest.mark.parametrize("outer,field", [("RSA", "k"), ("EC", "p"), ("OKP", "q")])
@pytest.mark.parametrize(
    "inner,member,private",
    [("RSA", "d", True), ("oct", "k", True), ("RSA", "k", False), (None, "d", False)],
)
@pytest.mark.parametrize(
    "shape", ["(<child>)", "[<child>]", "Object(<child>)", "true?<child>:null"]
)
@pytest.mark.parametrize("operand", ["typeof", "void"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_public_expression_proof_belongs_to_its_member_not_typed_descendants(
    outer, field, inner, member, private, shape, operand, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    child = "{" + member + ":" + repr(value) + ",n:" + operand + " /#/,"
    child += ("kty:" + repr(inner) + "," if inner else "") + "\n denominator}"
    text = "({kty:" + repr(outer) + "," + field + ":" + shape.replace("<child>", child)
    text += ",kid:'public-neighbor'})"
    for _ in range(layers):
        text = json.dumps(text)
    if private:
        assert_private(text, value)
    else:
        assert_public_target(text, value)
    if not layers:
        assert "kid:'public-neighbor'" in redact_sensitive_content(text, max_len=10000)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\u2028", "\u2029"])
@pytest.mark.parametrize("trivia", ["<break>", "/*<break>note*/", "//note<break>"])
@pytest.mark.parametrize("template", [False, True])
@pytest.mark.parametrize("kind,member,private", [("RSA", "d", True), ("RSA", "k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_line_completed_statement_regexp_keeps_source_and_template_owners(
    newline, trivia, template, kind, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    note = "(()=>{debugger" + trivia.replace("<break>", newline)
    note += "/['`({#]/.test('');return 1})()"
    if template:
        note = "`#${" + note + "}`"
    text = "({note:" + note + ",kty:" + repr(kind) + "," + member + ":" + repr(value)
    text += ",kid:'public-neighbor'})"
    for _ in range(layers):
        text = json.dumps(text)
    if private:
        assert_private(text, value)
    else:
        assert_public_target(text, value)
    if not layers:
        assert "kid:'public-neighbor'" in redact_sensitive_content(text, max_len=10000)


@pytest.mark.parametrize("operand", ["debugger", "obj.debugger", "(debugger)", "debugger(1)"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("kind", ["oct", "RSA", None])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_line_completed_identifier_evidence_cannot_replace_primary_python_type(
    operand, newline, kind, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    head = "'kty':" + repr(kind) if kind else "'ordinary':0"
    comment_type = repr("kty") + ":" + repr("oct")
    text = "{" + head + ",'n':" + operand + newline + " / # /," + comment_type + ","
    text += newline + "2,'k':" + repr(value) + ",'kid':'public-neighbor'}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text)
    # With no primary type, bare debugger plus a line break supplies a local
    # alternate interpretation. Baseline also conservatively masks that raw
    # ambiguity; a proved primary RSA role still protects the public member.
    if kind == "oct" or (kind is None and operand == "debugger"):
        assert_private(text, value)
    else:
        assert_public_target(text, value)
    if not layers:
        assert "'kid':'public-neighbor'" in redact_sensitive_content(text, max_len=10000)


_ASI_COMPLETION_NOTES_2142 = {
    "break": ("(()=>{do{", "}while(false);return 1})()"),
    "continue": ("(()=>{for(const x of []){", "}return 1})()"),
    "break label": ("(()=>{label:{", "}return 1})()"),
    "continue label": ("(()=>{label:for(const x of []){", "}return 1})()"),
    "debugger": ("(()=>{", ";return 1})()"),
    "return": ("(()=>{", ";})()"),
    "yield": ("(function*(){", ";})()"),
}
_ASI_TRIVIA_2142 = [
    (" ", "<break>"),
    ("\ufeff", "\ufeff<break>"),
    ("/*before*/\ufeff", "/*after*/\ufeff<break>"),
    ("\u00a0", "/*<break>*/\ufeff"),
    ("/*before*/ ", "//after<break>\ufeff"),
]


def _asi_completion_source_2142(control, trivia, newline, template, kind, member, value):
    prefix, suffix = _ASI_COMPLETION_NOTES_2142[control]
    gap, ending = trivia
    transfer = control.replace(" ", gap)
    regexp = "/['`({#]/" if template else "/#/"
    note = prefix + transfer + ending.replace("<break>", newline)
    note += regexp + ".test('')" + suffix
    if template:
        note = "`#${" + note + "}`"
    text = "({note:" + note + ",kty:" + repr(kind) + "," + member + ":" + repr(value)
    return text + ",kid:'public-neighbor'})"


def _assert_asi_surfaces_and_bounds_2142(text, value, private, layers):
    if private:
        assert_private(text, value)
    else:
        assert_public_target(text, value)
    members = private_members(text)
    relay = _jwk_findings(text)
    assert all(0 <= m.start < m.end <= len(text) for m in members + relay)
    assert all(
        0 <= jwk.audit_span(m, text)[0] < jwk.audit_span(m, text)[1] <= len(text) for m in members
    )
    # Encoded literals are replaced as a whole, as in the existing controls.
    if not layers:
        assert "public-neighbor" in redact_sensitive_content(text, max_len=20 * len(text) + 1000)


@pytest.mark.parametrize("control", _ASI_COMPLETION_NOTES_2142)
@pytest.mark.parametrize("trivia", _ASI_TRIVIA_2142)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\u2028", "\u2029"])
@pytest.mark.parametrize("template", [False, True])
@pytest.mark.parametrize("kind,member,private", [("RSA", "d", True), ("RSA", "k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_asi_transfer_family_retains_local_private_regexp_evidence(
    control, trivia, newline, template, kind, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    text = _asi_completion_source_2142(control, trivia, newline, template, kind, member, value)
    for _ in range(layers):
        text = json.dumps(text, ensure_ascii=False)
    _assert_asi_surfaces_and_bounds_2142(text, value, private, layers)


@pytest.mark.parametrize(
    "operand",
    [
        "getattr(obj,'break')",
        "obj['continue']",
        "obj.debugger",
        "(debugger)",
        "debugger(1)",
        "obj . debugger",
        "debugger[0]",
        "debugger + label",
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("kind", ["oct", "RSA", None])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_asi_property_and_expression_controls_keep_primary_python_ownership(
    operand, newline, kind, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    head = "'kty':" + repr(kind) if kind else "'ordinary':0"
    comment_type = repr("kty") + ":" + repr("oct")
    text = "{" + head + ",'n':" + operand + newline + " / # /," + comment_type + ","
    text += newline + "2,'k':" + repr(value) + ",'kid':'public-neighbor'}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text)
    _assert_asi_surfaces_and_bounds_2142(text, value, kind == "oct", layers)


@pytest.mark.parametrize(
    "control", ["debugger", "break", "continue", "break label", "continue label"]
)
@pytest.mark.parametrize("trivia", _ASI_TRIVIA_2142)
def test_asi_completion_never_publishes_primary_statement_literals(control, trivia):
    value = "q" + secrets.token_hex(12) + "z"
    text = _asi_completion_source_2142(control, trivia, "\n", False, "RSA", "d", value)
    primary, alternate = {}, {}
    owners = jwk._alternate_intervals(text, primary, alternate)
    slash = text.index("/#/")
    assert slash not in primary
    assert slash in alternate and owners


_IDENTIFIER_LABELS_2142 = [
    "label",
    "a名",
    "a_π",
    "$label",
    "π",
    "名",
    "\U00010400",
    "a\u0301",
    "a\u200c",
    "a\u200d",
    "\u2118",
    "a\u00b7",
    r"\u0061",
    r"\u{61}",
    r"a\u0062",
    r"a\u{62}",
    r"\u{10400}",
    r"a\u0301",
    r"a\u200c",
    r"a\u{200d}",
]
_IDENTIFIER_TRIVIA_2142 = [(" ", "\n"), ("/*a*/\ufeff", "/*b*/\ufeff\n")]


def _identifier_label_source_2142(label, control, trivia, template, member, value):
    opening = label + (":{" if control == "break" else ":for(const x of []){")
    regexp = "/['`({#]/" if template else "/#/"
    before, after = trivia
    note = "(()=>{" + opening + control + before + label + after
    note += regexp + ".test('')}return 1})()"
    if template:
        note = "`#${" + note + "}`"
    return (
        "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value) + ",kid:'public-neighbor'})"
    )


@pytest.mark.parametrize("label", _IDENTIFIER_LABELS_2142)
@pytest.mark.parametrize("control", ["break", "continue"])
@pytest.mark.parametrize("trivia", _IDENTIFIER_TRIVIA_2142)
@pytest.mark.parametrize("template", [False, True])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_identifier_label_family_keeps_private_only_source_and_template_evidence(
    label, control, trivia, template, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    text = _identifier_label_source_2142(label, control, trivia, template, member, value)
    for _ in range(layers):
        text = json.dumps(text, ensure_ascii=False)
    _assert_asi_surfaces_and_bounds_2142(text, value, private, layers)


@pytest.mark.parametrize("label", _IDENTIFIER_LABELS_2142)
@pytest.mark.parametrize("control", ["break", "continue"])
def test_identifier_label_completion_has_no_primary_literal_authority(label, control):
    value = "q" + secrets.token_hex(12) + "z"
    text = _identifier_label_source_2142(
        label, control, _IDENTIFIER_TRIVIA_2142[0], False, "d", value
    )
    primary, alternate = {}, {}
    owners = jwk._alternate_intervals(text, primary, alternate)
    slash = text.index("/#/")
    assert owners and slash in alternate and slash not in primary


@pytest.mark.parametrize("operand", ["obj.π", "obj.名", "obj.\U00010400", "obj.a\u0301"])
@pytest.mark.parametrize("unpack", [False, True])
@pytest.mark.parametrize("kind", ["oct", "RSA", None])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_unicode_identifiers_preserve_python_division_comment_and_unpack_proofs(
    operand, unpack, kind, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    head = "'ordinary':0" if kind is None else "'kty':" + repr(kind)
    if unpack:
        head = "**{" + head + "}"
    comment_type = repr("kty") + ":" + repr("oct")
    text = "{" + head + ",'n':" + operand + "\n / # /," + comment_type
    text += ",\n 2,'k':" + repr(value) + ",'kid':'public-neighbor'}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text, ensure_ascii=False)
    _assert_asi_surfaces_and_bounds_2142(text, value, kind == "oct", layers)


@pytest.mark.parametrize(
    "label", [r"\u", r"\u0", r"\uZZZZ", r"\u{61", r"\u{110000}", r"\u{ZZ}", "\\u{a_\u0301}"]
)
@pytest.mark.parametrize("template", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_incomplete_identifier_escape_evidence_stays_private_and_source_local(
    label, template, layers
):
    # Malformed raw excerpts carry no JavaScript syntax-validity premise.
    value = "q" + secrets.token_hex(12) + "z"
    text = _identifier_label_source_2142(
        label, "break", _IDENTIFIER_TRIVIA_2142[0], template, "d", value
    )
    for _ in range(layers):
        text = json.dumps(text, ensure_ascii=False)
    _assert_asi_surfaces_and_bounds_2142(text, value, True, layers)


@pytest.mark.parametrize(
    "label",
    _IDENTIFIER_LABELS_2142
    + [r"\u", r"\u0", r"\uZZZZ", r"\u{61", r"\u{110000}", r"\u{ZZ}", "\\u{a_\u0301}"],
)
def test_identifier_extent_stops_before_source_delimiters_and_trivia(label):
    for suffix in ["\n/#/", "/*x*/", ",kty:'RSA'", ";next()", "\ufeffnext"]:
        text = label + suffix
        assert jwk._javascript_identifier_end(text, 0) == len(label)


# Unknown lexical tokens retain bounded private evidence; none of these raw
# malformed excerpts is asserted to be valid JavaScript.
_UNKNOWN_LABELS_2142 = [
    "a\u200b",
    "a\u2060",
    "a\u00ad",
    "a\u202e",
    "a\ud800",
    "a\x00",
    "a\x1f",
    "a\x7f",
    r"\u{a-b}",
    r"\u{a.b}",
    r"\u{a\u0062}",
    r"\u{a@b}",
    r"\u{a,b}",
    r"\u{a+b}",
    r"\u{a?b}",
    r"\u{a!b}",
    r"\u{a&b}",
    r"\u{a.b",
    r"\u{a-b",
    r"\u{a@b",
    r"\u{a,b",
    r"\u{a\u0062",
    r"\u{a\u{62}",
    r"\x61",
    r"\u006",
    "a\udfff",
    "a\u0378",
    "a\ue000",
    "a\u200b" + r"\u0062",
    "a\ud800" + r"\u0062",
    r"\u{a.b]",
    r"\u{a.b)",
    r"\u{a)b}",
    r"\u{a[b}",
    r"\u{a/b}",
    r"\u{a:b}",
    r"\u{a;b}",
    r"\u{a=b}",
    r"\u{a{b}",
    "\u0378",
    "\x00",
    "\udfff",
]


@pytest.mark.parametrize("label", _UNKNOWN_LABELS_2142)
@pytest.mark.parametrize("control", ["break", "continue"])
@pytest.mark.parametrize("template", [False, True])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_unknown_label_retains_bounded_private_owner(
    label, control, template, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    text = _identifier_label_source_2142(label, control, (" ", "\n"), template, member, value)
    for _ in range(layers):
        text = json.dumps(text, ensure_ascii=True)
    _assert_asi_surfaces_and_bounds_2142(text, value, private, layers)


@pytest.mark.parametrize("label", _UNKNOWN_LABELS_2142)
@pytest.mark.parametrize("template", [False, True])
def test_unknown_label_owner_does_not_cross_sibling_or_source_fence(label, template):
    value = "q" + secrets.token_hex(12) + "z"
    sibling = "q" + secrets.token_hex(12) + "z"
    child = _identifier_label_source_2142(label, "break", (" ", "\n"), template, "d", value)
    text = child + ";\n({kty:'RSA',k:" + repr(sibling) + ",kid:'public-neighbor'})"
    assert_private(text, value)
    assert_public_target(text, sibling)
    owners = jwk._alternate_intervals(text, {}, {})
    if template:
        begin = text.index("`#${")
        end = jwk._javascript_literal(text, begin)[0]
        assert end == len(child) - len(",kty:'RSA',d:" + repr(value) + ",kid:'public-neighbor'})")
    else:
        assert owners and all(high <= len(child) for low, high in owners)
    assert all(0 <= low < high <= len(child) for low, high in owners)
    assert all(m.end <= len(child) for m in private_members(text))


@pytest.mark.parametrize("label", _UNKNOWN_LABELS_2142)
def test_unknown_label_completion_never_grants_primary_authority(label):
    value = "q" + secrets.token_hex(12) + "z"
    text = _identifier_label_source_2142(label, "break", (" ", "\n"), False, "d", value)
    primary, alternate = {}, {}
    owners = jwk._alternate_intervals(text, primary, alternate)
    slash = text.index("/#/")
    assert owners and slash in alternate and slash not in primary


@pytest.mark.parametrize("label", _UNKNOWN_LABELS_2142)
@pytest.mark.parametrize("kind", ["oct", "RSA", None])
@pytest.mark.parametrize("unpack", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_unknown_spelling_in_python_literal_cannot_retype_live_members(label, kind, unpack, layers):
    value = "q" + secrets.token_hex(12) + "z"
    head = "'ordinary':0" if kind is None else "'kty':" + repr(kind)
    if unpack:
        head = "**{" + head + "}"
    text = "{" + head + ",'n':obj[" + repr(label) + "]\n / # /,'kty':'oct',\n 2,'k':"
    text += repr(value) + ",'kid':'public-neighbor'}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text, ensure_ascii=True)
    _assert_asi_surfaces_and_bounds_2142(text, value, kind == "oct", layers)


@pytest.mark.parametrize(
    "ending", [")", "]", ":", ";", "=", "'literal'", '"literal"', "`#`", "/#/"]
)
def test_unknown_escape_releases_real_source_delimiters(ending):
    text = r"\u{a.b" + ending
    context = jwk._JavascriptBoundary(completion_end=0, completion_label=True)
    end, pending = jwk._javascript_identifier_token(text, 0)
    context.token(text, text[:end], 0, end, 0, identifier=True)
    context.identifier_escape = pending
    assert pending and end == 4
    assert context.unknown_identifier_piece(text, ".", end, end + 1)
    assert context.unknown_identifier_piece(text, "b", end + 1, end + 2)
    at = end + 2
    assert not context.unknown_identifier_piece(text, ending, at, len(text))
    assert not context.identifier_escape
    assert not context.safe_label and not context.safe_statement
    assert context.word == ""


@pytest.mark.parametrize("spelling", [r"\u0062reak", r"br\u0065ak", "break\u200b", "debugger\x7f"])
def test_unknown_or_escaped_keyword_spelling_has_no_completion_authority(spelling):
    context = jwk._JavascriptBoundary()
    end, pending = jwk._javascript_identifier_token(spelling, 0)
    context.token(spelling, spelling[:end], 0, end, 0, identifier=True)
    assert end == len(spelling) and not pending
    assert context.completion_end == context.prefix_end == -1
    assert not context.safe_statement


# R9: these exact malformed raw witnesses have baseline-covered private owners.
# Declaration/transfer placement matters; compiler rejection is no privacy waiver.
_UNCERTAIN_NESTED_LABELS_2142 = [
    r"\u{{a[b}}",
    r"\u{{a}}",
    r"\u{{{a}}}",
    r"\u{a`b}",
    r"\u{a``b}",
    r'\u{a"b}',
    r"\u{a'b}",
    "label",
]


def _uncertain_nested_source_2142(label, placement, shape, member, value):
    declaration = label if placement != "transfer" else "label"
    transfer = label if placement != "declaration" else "label"
    regexp = "/#/" if shape == "source" else "/['`({#]/"
    note = "(()=>{" + declaration + ":{break " + transfer
    note += "\n" + regexp + ".test('')}return 1})()"
    if shape == "template":
        note = "`head#${" + note + "}`"
    elif shape == "nested-template":
        note = "`head#${({inner:`nested#${" + note + "}`}).inner}`"
    return (
        "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value) + ",kid:'public-neighbor'})"
    )


@pytest.mark.parametrize("label", _UNCERTAIN_NESTED_LABELS_2142)
@pytest.mark.parametrize("placement", ["same", "transfer", "declaration"])
@pytest.mark.parametrize("shape", ["source", "template", "nested-template"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_uncertain_nested_escape_and_literal_keep_target_private_evidence(
    label, placement, shape, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    text = _uncertain_nested_source_2142(label, placement, shape, member, value)
    for _ in range(layers):
        text = json.dumps(text)
    _assert_asi_surfaces_and_bounds_2142(text, value, private, layers)


@pytest.mark.parametrize("label", _UNCERTAIN_NESTED_LABELS_2142[:-1])
@pytest.mark.parametrize("placement", ["same", "transfer", "declaration"])
def test_uncertain_nested_escape_completion_stays_alternate_only(label, placement):
    value = "q" + secrets.token_hex(12) + "z"
    text = _uncertain_nested_source_2142(label, placement, "source", "d", value)
    primary, alternate = {}, {}
    owners = jwk._alternate_intervals(text, primary, alternate)
    slash = text.index("/#/")
    assert slash not in primary and slash in alternate
    low = text.index(value)
    assert any(start <= low < low + len(value) <= end for start, end in owners)


@pytest.mark.parametrize("label", _UNCERTAIN_NESTED_LABELS_2142[:-1])
@pytest.mark.parametrize("placement", ["same", "transfer", "declaration"])
@pytest.mark.parametrize("shape", ["source", "template", "nested-template"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_uncertain_nested_escape_keeps_sibling_and_physical_source_fences(
    label, placement, shape, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    sibling = "q" + secrets.token_hex(12) + "z"
    child = _uncertain_nested_source_2142(label, placement, shape, "d", value)
    text = child + ";\n({kty:'RSA',k:" + repr(sibling) + ",kid:'public-neighbor'})"
    if shape == "source":
        owners = jwk._alternate_intervals(text, {}, {})
        assert owners and all(0 <= low < high <= len(child) for low, high in owners)
    else:
        begin = text.index("`head#${")
        end = jwk._javascript_literal(text, begin)[0]
        assert end == len(child) - len(",kty:'RSA',d:" + repr(value) + ",kid:'public-neighbor'})")
    assert all(m.end <= len(child) for m in private_members(text))
    neighbor = "({kty:'RSA',k:" + repr(sibling) + ",kid:'public-neighbor'})"
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    # Separate physical source contexts retain a meaningful negative audit-line
    # predicate even when each context is encoded into one physical line.
    text = child + ";\n" + neighbor
    _assert_asi_surfaces_and_bounds_2142(text, value, True, layers)
    assert_public_target(text, sibling)


@pytest.mark.parametrize("label", _UNCERTAIN_NESTED_LABELS_2142[:-1])
@pytest.mark.parametrize("kind", ["oct", "RSA", None])
@pytest.mark.parametrize("unpack", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_uncertain_nested_spelling_literal_preserves_primary_comment_and_unpack(
    label, kind, unpack, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    head = "'ordinary':0" if kind is None else "'kty':" + repr(kind)
    if unpack:
        head = "**{" + head + "}"
    text = "{" + head + ",'n':obj[" + repr(label) + "]\n / # /,'kty':'oct',\n 2,'k':"
    text += repr(value) + ",'kid':'public-neighbor'}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text)
    _assert_asi_surfaces_and_bounds_2142(text, value, kind == "oct", layers)


# R10: every unfinished escape opener owns one close, including openers
# consumed together and those after opaque punctuation. These are raw excerpts,
# with no JavaScript-validity premise or authority to retype primary members.
_NESTED_OPENERS_2142 = [
    r"\u{a\u{b.c}}",
    r"\u{a\u{b[}c}",
    r"\u{a`\u{b`c}}",
    r'\u{a"\u{b"c}}',
    r"\u{a'\u{b'c}}",
    r"\u{a\u{b\u{c.d}}}",
    r"\u{a`\u{b'\u{c.d}}}",
    r"\u{a[\u{b.c}}",
    r"\u{a(\u{b.c}}",
    r"\u{a\u{b.c}",
    r"\u{a`\u{b.c}",
    r"\u{a\u{b.c",
]


@pytest.mark.parametrize("label", _NESTED_OPENERS_2142)
@pytest.mark.parametrize("placement", ["same", "declaration", "transfer"])
@pytest.mark.parametrize("shape", ["source", "template", "nested-template"])
@pytest.mark.parametrize(
    "kind,member,private", [("RSA", "d", True), ("EC", "d", True), ("RSA", "k", False)]
)
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_nested_escape_openers_retain_each_private_owner(
    label, placement, shape, kind, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    text = _uncertain_nested_source_2142(label, placement, shape, member, value)
    text = text.replace("kty:'RSA'", "kty:" + repr(kind))
    for _ in range(layers):
        text = json.dumps(text)
    _assert_asi_surfaces_and_bounds_2142(text, value, private, layers)


@pytest.mark.parametrize("label", _NESTED_OPENERS_2142)
@pytest.mark.parametrize("placement", ["same", "declaration", "transfer"])
@pytest.mark.parametrize("shape", ["source", "template", "nested-template"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_nested_escape_openers_preserve_real_sibling_and_template_fences(
    label, placement, shape, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    sibling = "q" + secrets.token_hex(12) + "z"
    child = _uncertain_nested_source_2142(label, placement, shape, "d", value)
    if shape == "source":
        primary, alternate = {}, {}
        owners = jwk._alternate_intervals(child, primary, alternate)
        slash = child.index("/#/")
        assert slash in alternate and slash not in primary
        assert owners and all(0 <= low < high <= len(child) for low, high in owners)
        at = child.index(value)
        assert any(low <= at < at + len(value) <= high for low, high in owners)
    else:
        begin = child.index("`head#${")
        end = jwk._javascript_literal(child, begin)[0]
        assert end == len(child) - len(",kty:'RSA',d:" + repr(value) + ",kid:'public-neighbor'})")
    neighbor = "({kty:'RSA',k:" + repr(sibling) + ",kid:'public-neighbor'})"
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    text = child + ";\n" + neighbor
    _assert_asi_surfaces_and_bounds_2142(text, value, True, layers)
    assert_public_target(text, sibling)


@pytest.mark.parametrize("label", _NESTED_OPENERS_2142)
@pytest.mark.parametrize("kind", ["RSA", "oct", None])
@pytest.mark.parametrize("unpack", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_nested_escape_literal_preserves_real_primary_public_and_unpack(
    label, kind, unpack, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    head = "'ordinary':0" if kind is None else "'kty':" + repr(kind)
    if unpack:
        head = "**{" + head + "}"
    text = "{" + head + ",'n':obj[" + repr(label) + "]\n / # /,'kty':'oct',\n 2,'k':"
    text += repr(value) + ",'kid':'public-neighbor'}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text)
    _assert_asi_surfaces_and_bounds_2142(text, value, kind == "oct", layers)


@pytest.mark.parametrize("label", [r"\u{a\u{b.c}", r"\u{a\u{b.c", r"\u{a`\u{b.c}"])
@pytest.mark.parametrize("shape", ["source", "repeated", "nested"])
def test_nested_escape_unfinished_at_real_template_fence(label, shape):
    text = "`head#${(" + label + ")}`"
    if shape == "repeated":
        text = text[:-1] + "${1}`"
    elif shape == "nested":
        text = "`outer#${({inner:" + text + "}).inner}tail`"
    child = text
    text += ";\n({kty:'RSA',k:'public-neighbor'})"
    assert jwk._javascript_literal(text, 0)[0] == len(child)
    assert_public_target(text, "public-neighbor")


@pytest.mark.parametrize("label", [r"\u{a\u{b\u{c.d", r"\u{a`\u{b`\u{c.d"])
@pytest.mark.parametrize("shape", ["repeated", "nested"])
def test_nested_escape_pending_owners_end_at_physical_template_boundary(label, shape):
    inner = "`head#${" + label + "}`"
    if shape == "repeated":
        child = inner[:-1] + "${1}`"
    else:
        child = "`outer#${({inner:" + inner + "}).inner}${1}tail`"
    text = child + ";\n({kty:'RSA',k:'public-neighbor'})"
    assert jwk._javascript_literal(text, 0)[0] == len(child)
    assert_public_target(text, "public-neighbor")


# R11: the preceding close belongs to a synthetic escape or a real nested
# delimiter, not necessarily the enclosing interpolation. Malformed spellings
# have no compiler-validity premise. Each pending owner keeps its punctuation.
_INTERLEAVED_ESCAPE_CLOSES_2142 = [
    r"\u{a\u{b.c}`d}",
    r'\u{a\u{b.c}"d}',
    r"\u{a\u{b.c}'d}",
    r"\u{a\u{b.c}`\u{d.e}}",
    r"\u{a\u{b.c}`}",
    r"\u{a{b}\u{c.d}`e}",
    r"\u{a\u{b.c}`d\u{e.f}}",
    r"\u{a\u{b.c}\u{d.e}`f}",
]


def _assert_interleaved_surfaces_2142(text, value, private, layers):
    _assert_asi_surfaces_and_bounds_2142(text, value, private, layers)
    if private:
        at = text.index(value)
        assert any(m.start <= at and at + len(value) <= m.end for m in private_members(text))
        assert any(
            f.action == BLOCK and f.start <= at and at + len(value) <= f.end
            for f in _jwk_findings(text)
        )


def _interleaved_escape_source_2142(label, placement, shape, member, value):
    declaration = "outer" if placement == "transfer" else label
    transfer = "outer" if placement == "declaration" else label
    body = "(()=>{" + declaration + ":{break " + transfer + "\n/#/.test('')}return 1})()"
    if shape == "source":
        note = body
    elif shape == "template":
        note = "`#${" + body + "}`"
    elif shape == "repeated":
        note = "`#${" + body + '}${({kty:"RSA",k:"public-neighbor"}).k}`'
    else:
        note = "`outer#${({inner:`inner#${" + body + "}${1}`}).inner}${2}tail`"
    child = "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value)
    return child + ",kid:'public-neighbor'})", note


@pytest.mark.parametrize("label", _INTERLEAVED_ESCAPE_CLOSES_2142)
@pytest.mark.parametrize("placement", ["declaration", "transfer", "both"])
@pytest.mark.parametrize("shape", ["source", "template", "repeated", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_interleaved_escape_close_cannot_close_real_template(
    label, placement, shape, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    sibling = "q" + secrets.token_hex(12) + "z"
    child, note = _interleaved_escape_source_2142(label, placement, shape, member, value)
    if shape != "source":
        start = child.index(note)
        assert jwk._javascript_literal(child, start)[0] == start + len(note)
    else:
        primary, alternate = {}, {}
        owners = jwk._alternate_intervals(child, primary, alternate)
        at = child.index(value)
        assert any(low <= at < at + len(value) <= high <= len(child) for low, high in owners)
        slash = child.index("/#/")
        assert slash in alternate and slash not in primary
    neighbor = "({kty:'RSA',k:" + repr(sibling) + ",kid:'public-neighbor'})"
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    text = child + ";\n" + neighbor
    _assert_interleaved_surfaces_2142(text, value, private, layers)
    assert_public_target(text, sibling)


@pytest.mark.parametrize("close_kind", ["escape", "brace", "ragged"])
@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("quote", ["`", "'", '"'])
@pytest.mark.parametrize("continuation", ["", "d", r"\u{d.e}"])
@pytest.mark.parametrize("shape", ["source", "template", "repeated", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_close_owner_transition_matrix_retains_local_private_evidence(
    close_kind, pending, quote, continuation, shape, member, private, layers
):
    prefix = r"\u{a" * pending
    if close_kind == "escape":
        label = prefix + r"\u{b.c}" + quote + continuation + "}" * pending
    elif close_kind == "brace":
        label = prefix + "{b}" + quote + continuation + "}" * pending
    else:
        label = prefix + r"\u{b[(c}" + quote + continuation + "}" * pending
    value = "q" + secrets.token_hex(12) + "z"
    sibling = "q" + secrets.token_hex(12) + "z"
    child, note = _interleaved_escape_source_2142(label, "transfer", shape, member, value)
    if shape != "source":
        start = child.index(note)
        assert jwk._javascript_literal(child, start)[0] == start + len(note)
    neighbor = "({kty:'RSA',k:" + repr(sibling) + ",kid:'public-neighbor'})"
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    text = child + ";\n" + neighbor
    _assert_interleaved_surfaces_2142(text, value, private, layers)
    assert_public_target(text, sibling)


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("shape", ["source", "repeated", "nested"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_interpolation_close_releases_only_its_pending_escape_context(pending, shape, layers):
    inner = "`head#${" + r"\u{a" * pending + ".b}`"
    if shape == "repeated":
        child = inner[:-1] + "${({kty:'RSA',k:'public-neighbor'}).k}`"
    elif shape == "nested":
        child = "`outer#${({inner:" + inner + "}).inner}${1}tail`"
    else:
        child = inner
    assert jwk._javascript_literal(child, 0)[0] == len(child)
    sibling = "q" + secrets.token_hex(12) + "z"
    neighbor = "({kty:'RSA',k:" + repr(sibling) + ",kid:'public-neighbor'})"
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    assert_public_target(child + ";\n" + neighbor, sibling)


@pytest.mark.parametrize("label", _INTERLEAVED_ESCAPE_CLOSES_2142)
@pytest.mark.parametrize("kind", ["RSA", "oct", None])
@pytest.mark.parametrize("unpack", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_interleaved_escape_literals_preserve_primary_public_comment_and_unpack(
    label, kind, unpack, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    head = "'ordinary':0" if kind is None else "'kty':" + repr(kind)
    if unpack:
        head = "**{" + head + "}"
    text = "{" + head + ",'n':obj[" + repr(label) + "]\n / # /,'kty':'oct',\n 2,'k':"
    text += repr(value) + ",'kid':'public-neighbor'}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps(text)
    _assert_interleaved_surfaces_2142(text, value, kind == "oct", layers)


@pytest.mark.parametrize("label", _INTERLEAVED_ESCAPE_CLOSES_2142)
@pytest.mark.parametrize("shape", ["source", "repeated", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_direct_interpolation_escape_close_retains_opaque_continuation(
    label, shape, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    sibling = "q" + secrets.token_hex(12) + "z"
    inner = "`#${" + label + "\n/#/.test('')}`"
    if shape == "repeated":
        note = inner[:-1] + "${1}`"
    elif shape == "nested":
        note = "`outer#${({x:" + inner + "}).x}tail`"
    else:
        note = inner
    child = "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value)
    child += ",kid:'public-neighbor'})"
    assert jwk._javascript_literal(child, child.index(note))[0] == child.index(note) + len(note)
    neighbor = "({kty:'RSA',k:" + repr(sibling) + ",kid:'public-neighbor'})"
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    _assert_interleaved_surfaces_2142(child + ";\n" + neighbor, value, private, layers)
    assert_public_target(child + ";\n" + neighbor, sibling)


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("shape", ["repeated", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_real_interpolation_fence_keeps_next_interpolation_role_local(
    pending, shape, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    inner = "`head#${" + r"\u{a" * pending + ".b}"
    inner += "${({kty:'RSA'," + member + ":" + repr(value)
    inner += ",kid:'public-neighbor'})." + member + "}`"
    child = inner if shape == "repeated" else "`outer#${({inner:" + inner + "}).inner}tail`"
    assert jwk._javascript_literal(child, 0)[0] == len(child)
    sibling = "q" + secrets.token_hex(12) + "z"
    neighbor = "({kty:'RSA',k:" + repr(sibling) + ",kid:'public-neighbor'})"
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    text = child + ";\n" + neighbor
    _assert_interleaved_surfaces_2142(text, value, private, layers)
    assert_public_target(text, sibling)
    assert all(m.end <= len(child) for m in private_members(text))


@pytest.mark.parametrize("pending", [1, 3])
@pytest.mark.parametrize("suffix", [";", "\n", "\nconst next=1;", " \n", ".field;"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_real_template_closer_after_pending_escape_keeps_following_source(
    pending, suffix, nested, layers
):
    literal = "`head#${" + r"\u{a" * pending + ".b}`"
    if nested:
        literal = "`outer#${({inner:" + literal + "}).inner}tail`"
    child = "({note:" + literal + "})"
    value = "q" + secrets.token_hex(12) + "z"
    neighbor = "({kty:'RSA',k:" + repr(value) + ",kid:'public-neighbor'})"
    text = child + suffix + "\n" + neighbor
    start = child.index(literal)
    assert jwk._javascript_literal(text, start)[0] == start + len(literal)
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    text = child + suffix + "\n" + neighbor
    assert_public_target(text, value)
    assert not private_members(text)
    assert not _jwk_findings(text)


# R12: closers after an opaque backtick still belong to pending escapes.
# These are malformed raw excerpts, with no compiler-validity premise.
_AFTER_BACKTICK_FOLLOWERS_2142 = [".tail", "(x)", "[x]", ":x", ",x", ";x", "?x", "+x", " x", "\nx"]


def _after_backtick_source_2142(pending, tail, shape, member, value):
    label = r"\u{left" * pending + r"\u{inner.x}`" + "}" * pending + tail
    expression = label + "\n/#/.test('')"
    if shape in {"function", "source"}:
        expression = "(()=>{scope:{break " + expression + "}return 0})()"
    elif shape == "mapping":
        expression = "({value:" + expression + "}).value"
    inner = "`#${" + expression + "}`"
    note = expression if shape == "source" else inner
    if shape == "repeat":
        note = inner[:-1] + "${0}`"
    elif shape == "nested":
        note = "`top#${({inside:" + inner + "}).inside}${0}end`"
    child = "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value)
    return child + ",kid:'public-neighbor'})", note


def _assert_after_backtick_surfaces_2142(child, value, private, layers):
    sibling = "q" + secrets.token_hex(12) + "z"
    neighbor = "({kty:'RSA',k:" + repr(sibling) + "})"
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    text = child + ";\n" + neighbor
    _assert_interleaved_surfaces_2142(text, value, private, layers)
    assert_public_target(text, sibling)
    neighbor_lines = {i for i, line in enumerate(text.splitlines(), 1) if sibling in line}
    assert not neighbor_lines.intersection(f.line for f in check_secrets("neighbor.js", text))


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("tail", _AFTER_BACKTICK_FOLLOWERS_2142)
@pytest.mark.parametrize("shape", ["direct", "repeat", "nested", "function", "mapping", "source"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_after_backtick_closer_ownership_retains_target_and_physical_extent(
    pending, tail, shape, member, private, layers
):
    value = "q" + secrets.token_hex(12) + "z"
    child, note = _after_backtick_source_2142(pending, tail, shape, member, value)
    if shape != "source":
        start = child.index(note)
        assert jwk._javascript_literal(child, start)[0] == start + len(note)
    _assert_after_backtick_surfaces_2142(child, value, private, layers)


# A finite covering matrix adds placement and real/synthetic delimiter mixes.
# Real parentheses/braces remain inside the template until their own closes.
@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("real_frame", ["interpolation", "mapping", "function"])
@pytest.mark.parametrize("placement", ["declaration", "transfer", "both"])
@pytest.mark.parametrize("shape", ["source", "template", "repeated", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_after_backtick_transition_matrix_keeps_real_and_synthetic_owners_separate(
    pending, real_frame, placement, shape, member, private, layers
):
    # Rotate all continuation classes with trivia across the finite dimensions.
    index = (
        pending
        - 1
        + ["interpolation", "mapping", "function"].index(real_frame)
        + ["declaration", "transfer", "both"].index(placement)
        + ["source", "template", "repeated", "nested"].index(shape)
    )
    tail = _AFTER_BACKTICK_FOLLOWERS_2142[index % len(_AFTER_BACKTICK_FOLLOWERS_2142)]
    label = r"\u{left" * pending + r"\u{inner.x}`" + "}" * pending + tail
    declaration = "outer" if placement == "transfer" else label
    transfer = "outer" if placement == "declaration" else label
    body = declaration + ":{break " + transfer + "\n/#/.test('')}return 1"
    if real_frame == "function":
        body = "(function(){" + body + "})()"
    else:
        body = "(()=>{" + body + "})()"
        if real_frame == "mapping":
            body = "({value:" + body + "}).value"
    inner = "`#${" + body + "}`"
    note = body if shape == "source" else inner
    if shape == "repeated":
        note = inner[:-1] + "${0}`"
    elif shape == "nested":
        note = "`outer#${({inside:" + inner + "}).inside}${0}end`"
    value = "q" + secrets.token_hex(12) + "z"
    child = "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value)
    child += ",kid:'public-neighbor'})"
    if shape != "source":
        start = child.index(note)
        assert jwk._javascript_literal(child, start)[0] == start + len(note)
    _assert_after_backtick_surfaces_2142(child, value, private, layers)


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("source_frame", ["mapping", "call", "list"])
@pytest.mark.parametrize("trivia", ["", " ", "\n", "/* boundary */"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_after_backtick_genuine_source_fence_cleans_pending_owners(
    pending, source_frame, trivia, layers
):
    literal = "`head#${" + r"\u{left" * pending + ".x}`"
    if source_frame == "mapping":
        child = "({note:" + literal + "})"
    elif source_frame == "call":
        child = "consume(" + literal + ")"
    else:
        child = "[" + literal + "]"
    value = "q" + secrets.token_hex(12) + "z"
    raw = child + trivia + ";\n({kty:'RSA',k:" + repr(value) + "})"
    start = raw.index(literal)
    assert jwk._javascript_literal(raw, start)[0] == start + len(literal)
    for _ in range(layers):
        raw = json.dumps(raw)
    assert_public_target(raw, value)


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("kind", ["RSA", "oct", None])
@pytest.mark.parametrize("unpack", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_after_backtick_literals_keep_python_comment_public_unpack_and_comment_secret(
    pending, kind, unpack, layers
):
    label = r"\u{left" * pending + r"\u{inner.x}`" + "}" * pending + ".tail"
    value = "q" + secrets.token_hex(12) + "z"
    head = "'ordinary':0" if kind is None else "'kty':" + repr(kind)
    if unpack:
        head = "**{" + head + "}"
    raw = "{" + head + ",'n':obj[" + repr(label) + "]\n / # /,'kty':'oct',\n 2,'k':"
    raw += repr(value) + ",'kid':'public-neighbor'}"
    ast.parse(raw)
    text = raw
    for _ in range(layers):
        text = json.dumps(text)
    _assert_interleaved_surfaces_2142(text, value, kind == "oct", layers)
    # An independent braced comment secret has its own source and physical line.
    secret = "q" + secrets.token_hex(12) + "z"
    comment = "{'kty':'RSA','d':" + repr(secret) + "}"
    for _ in range(layers):
        comment = json.dumps(comment)
    _assert_interleaved_surfaces_2142(text + "\n# " + comment, secret, True, layers)


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("expression", ["mapping", "function"])
@pytest.mark.parametrize("placement", ["transfer", "both"])
@pytest.mark.parametrize("shape", ["source", "template", "repeated", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_uncertain_transfer_expression_retains_source_local_private_evidence(
    pending, expression, placement, shape, member, private, layers
):
    # A transfer expression is malformed, but baseline privacy still covers its
    # unknown escape and later private source. Syntax rejection is no waiver.
    index = pending - 1 + ["mapping", "function"].index(expression) + 2
    label = r"\u{left" * pending + r"\u{inner.x}`" + "}" * pending
    label += _AFTER_BACKTICK_FOLLOWERS_2142[index]
    if expression == "mapping":
        label = "({value:" + label + "}).value"
    else:
        label = "(()=>{" + label + "})()"
    value = "q" + secrets.token_hex(12) + "z"
    child, note = _interleaved_escape_source_2142(label, placement, shape, member, value)
    _assert_after_backtick_surfaces_2142(child, value, private, layers)
    primary, alternate = {}, {}
    jwk._alternate_intervals(child, primary, alternate)
    if shape == "source":
        slash = child.index("/#/")
        assert slash in alternate and slash not in primary


# R13: trivia cannot promote an escape-owned close into an interpolation close.
# Malformed raw excerpts carry no compiler-validity premise.
_FENCE_GAPS_2142 = ["", " ", "\n", "/*gap*/", "\ufeff", "/*\ngap*/"]


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("gap", _FENCE_GAPS_2142)
@pytest.mark.parametrize("real_frame", ["plain", "brace", "call", "list"])
@pytest.mark.parametrize("shape", ["direct", "repeat", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_trivia_after_opaque_backtick_retains_owned_closes_and_full_target(
    pending, gap, real_frame, shape, member, private, layers
):
    label = r"\u{out" * pending + r"\u{in.x}`" + gap + "}" * pending + ".next"
    if real_frame == "brace":
        label = "({inside:" + label + "}).inside"
    elif real_frame == "call":
        label = "use(" + label + ")"
    elif real_frame == "list":
        label = "[" + label + "][0]"
    note = "`head#${" + label + "\n/#/.test('')}`"
    if shape == "repeat":
        note = note[:-1] + "${1}end`"
    elif shape == "nested":
        note = "`outer#${({inner:" + note + "}).inner}end`"
    value = "q" + secrets.token_hex(12) + "z"
    child = "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value) + "})"
    start = child.index(note)
    assert jwk._javascript_literal(child, start)[0] == start + len(note)
    _assert_trivia_fence_surfaces_2142(child, value, private, layers)


def _assert_trivia_fence_surfaces_2142(child, value, private, layers):
    neighbor_value = "q" + secrets.token_hex(12) + "z"
    neighbor = "({kty:'RSA',k:" + repr(neighbor_value) + "})"
    for _ in range(layers):
        child, neighbor = json.dumps(child), json.dumps(neighbor)
    text = child + ";\n" + neighbor
    if private:
        assert_private(text, value)
        at = text.index(value)
        assert any(m.start <= at and at + len(value) <= m.end for m in private_members(text))
        assert any(
            f.action == BLOCK and f.start <= at and at + len(value) <= f.end
            for f in _jwk_findings(text)
        )
    else:
        assert_public_target(text, value)
    assert_public_target(text, neighbor_value)
    members = private_members(text)
    relay = _jwk_findings(text)
    assert all(0 <= m.start < m.end <= len(text) for m in members + relay)
    assert all(
        0 <= jwk.audit_span(m, text)[0] < jwk.audit_span(m, text)[1] <= len(text) for m in members
    )


_FENCE_CONTINUATIONS_2142 = [
    "",
    " ",
    "\n",
    "\ufeff",
    "/*gap*/",
    "/*\ngap*/",
    "\r\n",
    "\r",
    "\u2028",
    "\u2029",
    "\t",
    "\u00a0",
    "\u2003",
    "//gap\n",
    ",",
    ":",
    ";",
    " , ",
    "/*gap*/;/*gap*/",
]


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("placement", ["declaration", "transfer", "both"])
@pytest.mark.parametrize("shape", ["source", "template", "repeated", "nested"])
@pytest.mark.parametrize("follower", [".next", "[0]", "(0)"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_fence_arm_ownership_covering_matrix_keeps_private_continuation_local(
    pending, placement, shape, follower, member, private, layers
):
    index = (
        17 * pending
        + 7 * ["declaration", "transfer", "both"].index(placement)
        + 5 * ["source", "template", "repeated", "nested"].index(shape)
        + [".next", "[0]", "(0)"].index(follower)
    )
    gap = _FENCE_CONTINUATIONS_2142[index % len(_FENCE_CONTINUATIONS_2142)]
    label = r"\u{out" * pending + r"\u{in.x}`" + gap + "}" * pending + follower
    value = "q" + secrets.token_hex(12) + "z"
    child, note = _interleaved_escape_source_2142(label, placement, shape, member, value)
    if shape != "source":
        start = child.index(note)
        assert jwk._javascript_literal(child, start)[0] == start + len(note)
    _assert_trivia_fence_surfaces_2142(child, value, private, layers)
    if shape == "source":
        primary, alternate = {}, {}
        owners = jwk._alternate_intervals(child, primary, alternate)
        at = child.index(value)
        assert any(low <= at < at + len(value) <= high for low, high in owners)
        slash = child.index("/#/")
        assert slash in alternate and slash not in primary


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("event", ["real", "synthetic"])
@pytest.mark.parametrize(
    "arm",
    [
        "space",
        "FEFF",
        "comma",
        "colon",
        "semicolon",
        "paren",
        "bracket",
        "brace",
        "comment",
        "line-comment",
        "EOF",
    ],
)
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_fence_accept_arms_retain_genuine_source_cleanup_and_eof(
    pending, event, arm, member, private, layers
):
    body = r"\u{out" * pending + ".x"
    if event == "real":
        body = "(" + body + ")"
    literal = "`head#${" + body + "}`"
    if arm == "paren":
        child = "use(" + literal + ")"
    elif arm == "bracket":
        child = "[" + literal + "]"
    elif arm == "brace":
        child = "({note:" + literal + "})"
    else:
        suffix = {
            "space": " value;",
            "FEFF": "\ufeffvalue;",
            "comma": ",value;",
            "colon": ":value;",
            "semicolon": ";value;",
            "comment": "/*gap*/;",
            "line-comment": "//gap\n;",
            "EOF": "",
        }[arm]
        child = literal + suffix
    value = "q" + secrets.token_hex(12) + "z"
    # EOF has no following continuation. Its independent typed source is before
    # the literal; every other arm also checks a later separate source.
    target = "({kty:'RSA'," + member + ":" + repr(value) + "})"
    raw = target + ";\n" + child if arm == "EOF" else child + ";\n" + target
    start = raw.index(literal)
    assert jwk._javascript_literal(raw, start)[0] == start + len(literal)
    _assert_trivia_fence_surfaces_2142(raw, value, private, layers)


_FENCE_ORDINARY_GAPS_2142 = [" tail", "\n tail", ",tail", ":tail", ";tail", "/*gap*/tail"]


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("gap", _FENCE_ORDINARY_GAPS_2142)
@pytest.mark.parametrize("real_frame", ["plain", "brace", "call", "list"])
@pytest.mark.parametrize("shape", ["direct", "repeat", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_ordinary_continuation_after_opaque_backtick_keeps_pending_ownership(
    pending, gap, real_frame, shape, member, private, layers
):
    label = r"\u{out" * pending + r"\u{in.x}`" + gap + "}" * pending + ".next"
    if real_frame == "brace":
        label = "({inside:" + label + "}).inside"
    elif real_frame == "call":
        label = "use(" + label + ")"
    elif real_frame == "list":
        label = "[" + label + "][0]"
    note = "`head#${" + label + "\n/#/.test('')}`"
    if shape == "repeat":
        note = note[:-1] + "${1}end`"
    elif shape == "nested":
        note = "`outer#${({inner:" + note + "}).inner}end`"
    value = "q" + secrets.token_hex(12) + "z"
    child = "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value) + "})"
    start = child.index(note)
    assert jwk._javascript_literal(child, start)[0] == start + len(note)
    _assert_trivia_fence_surfaces_2142(child, value, private, layers)


_FENCE_EXPRESSION_GAPS_2142 = [
    " 'opaque'",
    ' "opaque"',
    " ({value:0})",
    " use(0)",
    " [0]",
    " tail+more",
]


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("gap", _FENCE_EXPRESSION_GAPS_2142)
@pytest.mark.parametrize("real_frame", ["plain", "brace", "call", "list"])
@pytest.mark.parametrize("shape", ["direct", "repeat", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_expression_continuation_after_opaque_backtick_keeps_pending_ownership(
    pending, gap, real_frame, shape, member, private, layers
):
    label = r"\u{out" * pending + r"\u{in.x}`" + gap + "}" * pending + ".next"
    if real_frame == "brace":
        label = "({inside:" + label + "}).inside"
    elif real_frame == "call":
        label = "use(" + label + ")"
    elif real_frame == "list":
        label = "[" + label + "][0]"
    note = "`head#${" + label + "\n/#/.test('')}`"
    if shape == "repeat":
        note = note[:-1] + "${1}end`"
    elif shape == "nested":
        note = "`outer#${({inner:" + note + "}).inner}end`"
    value = "q" + secrets.token_hex(12) + "z"
    child = "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value) + "})"
    start = child.index(note)
    assert jwk._javascript_literal(child, start)[0] == start + len(note)
    _assert_trivia_fence_surfaces_2142(child, value, private, layers)


_FENCE_LITERAL_GAPS_2142 = [
    " `opaque`",
    " `head${0}`",
    " /x/",
    " tail /x/",
    " 'a}b'",
    ' "a}b"',
    " 'a}`b'",
    ' "a}`b"',
]


@pytest.mark.parametrize("pending", [1, 2, 3])
@pytest.mark.parametrize("gap", _FENCE_LITERAL_GAPS_2142)
@pytest.mark.parametrize("real_frame", ["plain", "brace", "call", "list"])
@pytest.mark.parametrize("shape", ["direct", "repeat", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_literal_continuation_after_opaque_backtick_keeps_actual_close_ownership(
    pending, gap, real_frame, shape, member, private, layers
):
    label = r"\u{out" * pending + r"\u{in.x}`" + gap + "}" * pending + ".next"
    if real_frame == "brace":
        label = "({inside:" + label + "}).inside"
    elif real_frame == "call":
        label = "use(" + label + ")"
    elif real_frame == "list":
        label = "[" + label + "][0]"
    note = "`head#${" + label + "\n/#/.test('')}`"
    if shape == "repeat":
        note = note[:-1] + "${1}end`"
    elif shape == "nested":
        note = "`outer#${({inner:" + note + "}).inner}end`"
    value = "q" + secrets.token_hex(12) + "z"
    child = "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value) + "})"
    start = child.index(note)
    assert jwk._javascript_literal(child, start)[0] == start + len(note)
    _assert_trivia_fence_surfaces_2142(child, value, private, layers)


# A real quoted expression owns braces/backticks after punctuation and comments
# as well as whitespace. The excerpt remains raw and deliberately malformed.
_QUOTED_FENCE_GAPS_2142 = [
    ",'a}b'",
    ":'a}b'",
    ";'a}b'",
    "/*g*/'a}b'",
    "('a}b')",
    "['a}b']",
    "({v:'a}b'})",
    "='a}b'",
    ",'a}`b'",
    "/*g*/'a}`b'",
]


@pytest.mark.parametrize("pending", [1, 2])
@pytest.mark.parametrize("gap", _QUOTED_FENCE_GAPS_2142)
@pytest.mark.parametrize("real_frame", ["plain", "call"])
@pytest.mark.parametrize("shape", ["direct", "nested"])
@pytest.mark.parametrize("member,private", [("d", True), ("k", False)])
@pytest.mark.parametrize("layers", [0, 3])
def test_quoted_expression_after_opaque_punctuation_keeps_actual_source_owner(
    pending, gap, real_frame, shape, member, private, layers
):
    label = r"\u{out" * pending + r"\u{in.x}`" + gap + "}" * pending + ".next"
    if real_frame == "call":
        label = "use(" + label + ")"
    note = "`head#${" + label + "\n/#/.test('')}`"
    if shape == "nested":
        note = "`outer#${({inner:" + note + "}).inner}end`"
    value = "q" + secrets.token_hex(12) + "z"
    child = "({note:" + note + ",kty:'RSA'," + member + ":" + repr(value) + "})"
    start = child.index(note)
    assert jwk._javascript_literal(child, start)[0] == start + len(note)
    _assert_trivia_fence_surfaces_2142(child, value, private, layers)
