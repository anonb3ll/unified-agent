"""Dictionary member unpacking cannot be inferred from binary power syntax."""

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
    # These pure scanner tests have no database consumers.
    return False


@pytest.fixture(autouse=True)
def _forbid_store_access(isolated_env, monkeypatch):
    from unified_agent import config, storage

    def refused(*args, **kwargs):
        pytest.fail("pure JWK operand tests must not initialize or open a database")

    for name in ("init_db", "connect", "connect_read_only"):
        monkeypatch.setattr(storage, name, refused)
    assert not config.db_path().exists()
    yield
    assert not config.db_path().exists()


def protect(text: str, private: str) -> str:
    ast.parse(text)
    members = jwk.private_members(text)
    assert members
    output = redact_sensitive_content(text, max_len=20 * len(text) + 1000)
    assert private not in output
    ast.parse(output)
    private_line = next(i for i, line in enumerate(text.splitlines(), 1) if private in line)
    assert private_line in {finding.line for finding in check_secrets("fixture.py", text)}
    findings = _jwk_findings(text)
    assert findings and all(finding.action == BLOCK for finding in findings)
    assert any(text[member.start : member.end].find(private) >= 0 for member in members)
    assert any(text[finding.start : finding.end].find(private) >= 0 for finding in findings)
    return output


@pytest.mark.parametrize(
    "left",
    [
        "{secret}",
        "Secret({secret})",
        "({secret})",
        "items[{secret}]",
        "Secret({secret}).value",
        'f"prefix{{{secret}}}"',
        "(lambda: {secret})",
        "({{'label': {secret}}})",
        "(Secret({secret}) # ** {{fake}}\n)",
    ],
)
@pytest.mark.parametrize("right", ["{obj}", "({obj})", "( # comment\n ({obj}))"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_power_operand_cannot_retype_parent(left, right, layers):
    private = "q" + secrets.token_hex(6) + "z"
    operand = left.format(secret=repr(private))
    public = repr({TYPE: "RSA", "k": "ordinary"})
    text = repr({TYPE: OCT})[:-1] + ", 'k': " + operand + " ** " + right.format(obj=public) + "}"
    ast.parse(text)
    assert not jwk._unpacked_parents(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    protect(text, private)


@pytest.mark.parametrize("prefix", ["", "'note': 'operand ** {ignored}', ", "# ** {ignored}\n"])
@pytest.mark.parametrize("spacing", ["", "(", "( # comment\n ("])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_real_member_unpack_still_supplies_parent_type(prefix, spacing, layers):
    private = "q" + secrets.token_hex(6) + "z"
    child = repr({TYPE: OCT})
    closing = ")" * spacing.count("(")
    text = "{" + prefix + "**" + spacing + child + closing + ", 'k': " + repr(private) + "}"
    ast.parse(text)
    assert jwk._unpacked_parents(text) == {text.index(child): 0}
    for _ in range(layers):
        text = json.dumps({"log": text})
    protect(text, private)


@pytest.mark.parametrize("left", ["1", "item", "call()", "items[0]", "(item)", "f'{item}'"])
def test_power_child_does_not_make_public_mapping_private(left):
    public = "q" + secrets.token_hex(6) + "z"
    child = repr({TYPE: OCT})
    text = repr({TYPE: "RSA", "k": public})[:-1] + ", 'note': " + left + " ** " + child + "}"
    ast.parse(text)
    assert not jwk._unpacked_parents(text)
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize("operator", ["+", "**"])
def test_nested_unpack_inside_power_keeps_its_own_owner(operator):
    private = "q" + secrets.token_hex(6) + "z"
    source = repr({TYPE: OCT})
    child = "{**" + source + ", 'k': " + repr(private) + "}"
    text = "{'payload': operand " + operator + " (" + child + ")}"
    assert jwk._unpacked_parents(text) == {text.index(source): text.index(child)}
    protect(text, private)


def test_real_call_unpack_stays_in_call_instead_of_retyping_metadata():
    public = "q" + secrets.token_hex(6) + "z"
    source = repr({TYPE: OCT})
    text = repr({TYPE: "RSA", "k": public})[:-1] + ", 'note': call(**" + source + ")}"
    calls = set()
    assert not jwk._unpacked_parents(text, calls)
    assert calls == {text.index(source)}
    assert public in redact_sensitive_content(text, max_len=10000)


@pytest.mark.parametrize("consumer", ["redact", "private-members", "audit", "relay"])
@pytest.mark.parametrize("right", ["{obj}", "({obj})"])
def test_runtime_fake_power_leak_is_blocked_by_each_consumer(consumer, right):
    private = "q" + secrets.token_hex(6) + "z"
    child = repr({TYPE: "RSA", "k": "public"})
    text = repr({TYPE: OCT})[:-1] + ", 'k': Secret(" + repr(private) + ") ** "
    text += right.format(obj=child) + "}"
    ast.parse(text)
    if consumer == "redact":
        assert private not in redact_sensitive_content(text, max_len=10000)
    elif consumer == "private-members":
        assert any(private in member.value for member in jwk.private_members(text))
    elif consumer == "audit":
        assert list(check_secrets("fixture.py", text))
    else:
        assert any(finding.action == BLOCK for finding in _jwk_findings(text))


@pytest.mark.parametrize("left", ["item", "call()", "items[0]", "(item)", 'f"{item}"'])
def test_power_argument_cannot_supply_call_type_or_hide_public_sibling(left):
    public = "q" + secrets.token_hex(6) + "z"
    child = repr({TYPE: OCT})
    text = "dict(" + TYPE + "='RSA', k=" + repr(public) + ", note=" + left + " ** " + child + ")"
    ast.parse(text)
    calls = set()
    assert not jwk._unpacked_parents(text, calls)
    assert not calls
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize("operand", ["x", "call()", "items[0]", "1", "'text'"])
@pytest.mark.parametrize("comment", ["# , **{ignored}", "# ( **({ignored})"])
def test_comment_without_preceding_space_cannot_add_unpack_ownership(operand, comment):
    text = "{'note': " + operand + comment + "\n}"
    ast.parse(text)
    calls = set()
    assert not jwk._unpacked_parents(text, calls)
    assert not calls


@pytest.mark.parametrize("prefix", ["x# , **{ignored}", "call()# (**{ignored})"])
def test_actual_unpack_after_unspaced_comment_keeps_source_offsets(prefix):
    source = repr({TYPE: OCT})
    text = "{'note': " + prefix + "\n, **(" + source + "), 'k': 'value'}"
    ast.parse(text)
    assert jwk._unpacked_parents(text) == {text.index(source): 0}


COMPUTED_UNPACKS = [
    ("list-index", "[{rsa}, {octet}][-1]", lambda rsa, octet: [rsa, octet][-1]),
    ("container-call", "dict({octet})", lambda rsa, octet: dict(octet)),
    ("conditional-empty", "{rsa} if False else {{}}", lambda rsa, octet: rsa if False else {}),
    ("conditional-oct", "{rsa} if False else {octet}", lambda rsa, octet: rsa if False else octet),
    ("union", "{rsa} | {octet}", lambda rsa, octet: rsa | octet),
    ("boolean-and", "{rsa} and {octet}", lambda rsa, octet: rsa and octet),
    ("boolean-or", "{{}} or {octet}", lambda rsa, octet: {} or octet),
    ("method-default", "{rsa}.get('missing', {{}})", lambda rsa, octet: rsa.get("missing", {})),
    ("method-chain", "{rsa}.copy() | {octet}", lambda rsa, octet: rsa.copy() | octet),
    (
        "index",
        "{{'choice': {rsa}, 'chosen': {octet}}}['chosen']",
        lambda rsa, octet: {"choice": rsa, "chosen": octet}["chosen"],
    ),
    ("attribute-call", "{rsa}.__class__({octet})", lambda rsa, octet: rsa.__class__(octet)),
]


@pytest.mark.parametrize(
    "name,template,compute", COMPUTED_UNPACKS, ids=[c[0] for c in COMPUTED_UNPACKS]
)
@pytest.mark.parametrize("parent_type", ["RSA", OCT])
@pytest.mark.parametrize("depth", [0, 1, 3])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_computed_unpack_cannot_downgrade_possible_private_type(
    name, template, compute, parent_type, depth, layers
):
    private = "q" + secrets.token_hex(6) + "z"
    rsa, octet = {TYPE: "RSA"}, {TYPE: OCT}
    computed = template.format(rsa=repr(rsa), octet=repr(octet))
    # Execute only this test's fixed container operation, never source text.
    actual = {TYPE: parent_type, "k": private, **compute(rsa, octet)}
    assert actual.get("k") == private
    assert actual[TYPE] in ("RSA", OCT)
    for _ in range(depth):
        computed = "{**(" + computed + ")}"
    text = repr({TYPE: parent_type, "k": private})[:-1] + ", **(" + computed + ")}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    # Unresolved computed unpacks can return another branch/type. Protect every
    # possibly private member even when this fixture's chosen result is public.
    protect(text, private)


@pytest.mark.parametrize(
    "tail",
    [
        ".missing",
        "[index]",
        "()",
        " + other",
        " if condition else other",
        " and other",
        " or other",
    ],
)
@pytest.mark.parametrize("depth", [0, 1, 3])
def test_literal_prefix_of_computed_operand_has_no_exact_owner(tail, depth):
    source = repr({TYPE: "RSA"})
    operand = "(" * depth + source + ")" * depth + tail
    text = "{" + repr(TYPE) + ":'oct', **(" + operand + ")}"
    ast.parse(text)
    assert not jwk._unpacked_parents(text)


@pytest.mark.parametrize(
    "template", ["{rsa} if False else {octet}", "{rsa} | {octet}", "{{}} or {octet}"]
)
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_computed_call_unpack_masks_private_sibling(template, layers):
    private = "q" + secrets.token_hex(6) + "z"
    computed = template.format(rsa=repr({TYPE: "RSA"}), octet=repr({TYPE: OCT}))
    text = "dict(**(" + computed + "), k=" + repr(private) + ")"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    protect(text, private)


@pytest.mark.parametrize("field", ["k", "d", "p", "q", "dp", "dq", "qi"])
@pytest.mark.parametrize("copied", [False, True])
@pytest.mark.parametrize("layers", [0, 2])
def test_uncertain_owner_protects_all_private_fields_and_copied_values(field, copied, layers):
    private = "q" + secrets.token_hex(6) + "z"
    member = repr({field: private})
    member = "**" + member if copied else member[1:-1]
    computed = repr({TYPE: "RSA"}) + " if condition else " + repr({TYPE: OCT})
    text = "{" + repr(TYPE) + ":'RSA', " + member + ", **(" + computed + ")}"
    for _ in range(layers):
        text = json.dumps({"log": text})
    protect(text, private)


@pytest.mark.parametrize("field", ["r", "d", "t"])
@pytest.mark.parametrize("depth", [0, 1, 3])
@pytest.mark.parametrize("layers", [0, 2])
def test_uncertainty_reaches_rsa_other_prime_entries_after_nested_unpack(field, depth, layers):
    private = "q" + secrets.token_hex(6) + "z"
    child = repr({TYPE: OCT}) + " | " + repr({TYPE: "RSA"})
    for _ in range(depth):
        child = "{**(" + child + ")}"
    text = "{" + repr(TYPE) + ":'oct', 'oth':[" + repr({field: private}) + "], **(" + child + ")}"
    for _ in range(layers):
        text = json.dumps({"log": text})
    protect(text, private)


@pytest.mark.parametrize("depth", [0, 1, 3, 8])
@pytest.mark.parametrize("layout", ["plain", "comment", "continuation"])
@pytest.mark.parametrize("layers", [0, 2])
def test_complete_public_literal_still_overrides_parent_exactly(depth, layout, layers):
    public = "q" + secrets.token_hex(6) + "z"
    operand = repr({TYPE: "RSA"})
    for _ in range(depth):
        operand = "{**(" + operand + ")}"
    wrapper = {
        "plain": "(" + operand + ")",
        "comment": "( # before\n (" + operand + ") # after\n)",
        "continuation": "\\\n(\\\n(" + operand + "))",
    }[layout]
    text = repr({TYPE: OCT, "k": public})[:-1] + ", **" + wrapper + "}"
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


def _computed_cost_text(shape, size):
    typed = repr({TYPE: OCT})
    if shape == "type-only-nested-mappings":
        return "{**(" * size + "(" + typed + " if choose else {})" + ")}" * size
    if shape == "nested-calls":
        return "f(**" * size + typed + ")" * size
    if shape == "lambda-bindings":
        return typed + "\n" + "(lambda value=" * size + "1" + ", **options: 'ordinary')" * size
    return ("{**(unknown), " + repr(TYPE) + ":'oct', 'k':'value'}\n") * size


@pytest.mark.parametrize(
    "shape", ["nested-calls", "sibling-mappings", "lambda-bindings", "type-only-nested-mappings"]
)
def test_computed_unpack_growth_and_peak_keep_existing_cost_budget(shape):
    small, large = (_computed_cost_text(shape, n) for n in (2000, 8000))
    # CPU time, best of interleaved samples: runner load cannot decide it (#2188).
    assert_cpu_growth(cold(jwk.private_members), small, large, limit=8, label=shape)

    # The existing oct-block contract: 48 bytes/input byte + 400/masked value.
    def budget(found):
        return 48 * len(large) + 400 * len(found)

    peak, members = peak_bytes(cold(jwk.private_members), large, budget=budget)
    if shape == "type-only-nested-mappings":
        assert not members
    assert peak < budget(members)


@pytest.mark.parametrize("shape", ["mapping", "call", "callback"])
@pytest.mark.parametrize("placement", ["before", "after", "separate-metadata"])
def test_unrelated_type_evidence_does_not_turn_kwargs_into_jwk(shape, placement):
    public = "q" + secrets.token_hex(6) + "z"
    expression = {
        "mapping": "{'k': " + repr(public) + ", **options}",
        "call": "callback(value=" + repr(public) + ", **options)",
        "callback": "register(lambda: callback(" + repr(public) + ", **options))",
    }[shape]
    typed = repr({TYPE: "RSA", "label": "metadata"})
    text = {
        "before": typed + "\n" + expression,
        "after": expression + "\n" + typed,
        "separate-metadata": "{'metadata': " + typed + ", 'result': " + expression + "}",
    }[placement]
    ast.parse(text)
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


def test_template_callback_type_name_arguments_are_not_key_type_values():
    public = "q" + secrets.token_hex(6) + "z"
    text = "template.format(" + TYPE + "=TYPE_NAME, label=" + repr(public) + ", **values)"
    text += "\n" + repr({TYPE: "RSA"})
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize("prefix", ["def", "async def"])
def test_variadic_function_parameters_are_not_call_unpack_owners(prefix):
    public = "q" + secrets.token_hex(6) + "z"
    text = prefix + " callback(metadata=" + repr({TYPE: "RSA"}) + ", **options):\n"
    text += "    return " + repr(public)
    ast.parse(text)
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


LAMBDA_PUBLIC = [
    "(lambda metadata={typed}, **options: {public})",
    "register(lambda metadata={typed}, **options: {public})",
    "[lambda metadata={typed}, **options: {public}]",
    "(lambda choice=(lambda metadata={typed}, **inside: {public}), **outside: {public})",
    "(lambda choice=lambda metadata={typed}, **inside: {public}, **outside: {public})",
    "(lambda *args, metadata={typed}, **options: {public})",
    "(lambda metadata=[{typed}][0], **options: {public})",
    "(lambda metadata=({typed},)[0:1], **options: {public})",
    "(lambda note=f'{{value:>8}}', metadata={typed}, **options: {public})",
    "(lambda metadata={typed}, # marker : lambda **ignored\n **options: {public})",
    "(lambda metadata={typed}, \\\n **options: {public})",
    "(lambda metadata=callback(**options), spec={typed}, **outside: {public})",
    "(lambda **options: {{'metadata': {typed}, 'note': {public}}})",
    "({typed}, lambda **options: {public})",
]


@pytest.mark.parametrize("template", LAMBDA_PUBLIC)
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_lambda_bindings_preserve_public_defaults_and_body(template, layers):
    public = "q" + secrets.token_hex(6) + "z"
    text = template.format(typed=repr({TYPE: "RSA"}), public=repr(public))
    ast.parse(text)
    for _ in range(layers):
        text = json.dumps({"log": text})
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize(
    "location",
    [
        "default-call",
        "default-map",
        "body-call",
        "body-map",
        "nested-body-call",
        "nested-default-call",
    ],
)
@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_lambda_defaults_and_body_keep_genuine_unpack_protection(location, literal, layers):
    private = "q" + secrets.token_hex(6) + "z"
    octet, rsa = repr({TYPE: OCT}), repr({TYPE: "RSA"})
    operand = octet if literal else rsa + " | " + octet
    call = "dict(**(" + operand + "), k=" + repr(private) + ")"
    mapping = "{" + repr(TYPE) + ":'RSA', 'k':" + repr(private) + ", **(" + operand + ")}"
    body = {
        "default-call": "(lambda metadata=" + call + ", **options: 'ordinary')",
        "default-map": "(lambda metadata=" + mapping + ", **options: 'ordinary')",
        "body-call": "(lambda **options: " + call + ")",
        "body-map": "(lambda **options: " + mapping + ")",
        "nested-body-call": "(lambda choice=lambda **inside: " + call + ", **outside: 'ordinary')",
        "nested-default-call": "(lambda choice=(lambda metadata="
        + call
        + ", **inside: 'ordinary'), **outside: 'ordinary')",
    }[location]
    ast.parse(body)
    for _ in range(layers):
        body = json.dumps({"log": body})
    protect(body, private)


@pytest.mark.parametrize("prefix", ["def", "async def"])
def test_generic_function_parameters_are_not_call_unpack_owners(prefix):
    import sys

    if sys.version_info < (3, 12):
        pytest.skip("Python type-parameter grammar requires 3.12")
    public = "q" + secrets.token_hex(6) + "z"
    text = prefix + " callback[T](metadata=" + repr({TYPE: "RSA"}) + ", **options):\n"
    text += "    return " + repr(public)
    ast.parse(text)
    assert not jwk.private_members(text)
    assert redact_sensitive_content(text, max_len=10000) == text
    assert not list(check_secrets("fixture.py", text))
    assert not _jwk_findings(text)


@pytest.mark.parametrize("kind,field", [(OCT, "k"), ("RSA", "oth")])
@pytest.mark.parametrize("depth", [0, 1, 3, 16])
def test_empty_type_frames_preserve_deferred_members_and_other_prime_sources(kind, field, depth):
    private = "q" + secrets.token_hex(6) + "z"
    payload = {field: [{"r": private}]} if field == "oth" else {field: private}
    typed = repr({TYPE: kind})
    for _ in range(depth):
        typed = "{**(" + typed + ")}"
    text = "{**" + repr(payload) + ", **(" + typed + "), 'public': 'ordinary'}"
    output = protect(text, private)
    assert "ordinary" in output


def test_lazy_member_buffers_are_independent_across_mapping_and_prime_owners():
    symmetric, prime, public = ("q" + secrets.token_hex(6) + "z" for _ in range(3))
    source = [
        {TYPE: OCT, "k": symmetric},
        {TYPE: "RSA", "oth": [{"r": prime}]},
        {TYPE: "RSA", "k": public},
        {"k": public},
    ]
    text = repr(source)
    output = protect(text, symmetric)
    assert prime not in output
    assert output.count(public) == 2
    assert len(jwk.private_members(text)) == 2


@pytest.mark.parametrize("operand", ["extra", "(extra)", "(\nextra\n)"])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_uncertain_sibling_unpack_keeps_call_syntax_and_public_type(operand, before, layers):
    private = "q" + secrets.token_hex(6) + "z"
    public_type = repr({TYPE: OCT})
    args = ["**" + operand, "**" + public_type] if before else ["**" + public_type, "**" + operand]
    text = "dict(" + ", ".join(args) + ", k=" + repr(private) + ")"
    for _ in range(layers):
        text = json.dumps({"log": text})
    output = protect(text, private)
    assert "extra" not in output
    if not layers:
        assert output.startswith("dict(")
        assert output.count("**") == 2
        assert public_type in output
        assert all("**" not in text[m.start : m.end] for m in jwk.private_members(text))
