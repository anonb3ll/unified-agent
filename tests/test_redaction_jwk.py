"""Contextual JWK redaction: structured keys and linear scans (task #2007).

Shared by the PR that integrates the redaction gaps (#1146) and the PR that
shares the detector with the public relay (#1147), so either can land first.
Every key-shaped fixture is assembled at runtime.
"""

from __future__ import annotations

import json
import secrets
import warnings

import pytest
from growth_budget import assert_cpu_growth, cold, cpu_growth, peak_growth

from unified_agent.audit.rules import check_secrets
from unified_agent.telemetry.jwk import private_members
from unified_agent.telemetry.redaction import redact_sensitive_content, sanitize_metadata
from unified_agent.tools.public_relay import BLOCK, _jwk_findings, run_gates


def redact(text: str) -> str:
    return redact_sensitive_content(text, max_len=len(text) + 100)


def kty_cases() -> list[tuple[dict, str]]:
    secret = "a1b2_c3d4-" * 5
    return [
        ({"kty": "EC", "crv": "P-256", "x": "pub", "d": secret}, secret),
        ({"kty": "OKP", "crv": "Ed25519", "x": "pub", "d": secret}, secret),
        ({"kty": "oct", "k": secret}, secret),
        ({"kty": "RSA", "n": "pub", "e": "AQAB", "qi": secret}, secret),
    ]


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("shape", ["top", "nested", "key-set"])
def test_structured_jwk_dicts_are_redacted_in_metadata(index, shape):
    # PR #1146 review P2-4: sanitize_metadata recursed one field at a time, so the
    # private member of a JWK held as a dict passed through verbatim.
    key, secret = kty_cases()[index]
    meta = {
        "top": key,
        "nested": {"wrapper": {"jwk": key}},
        "key-set": {"jwks": {"keys": [{"kty": "RSA", "n": "pub"}, key]}},
    }[shape]
    out = sanitize_metadata(meta)
    assert secret not in json.dumps(out)
    assert "[REDACTED_SECRET]" in json.dumps(out)
    if shape == "top":
        assert out["kty"] == key["kty"]


def test_a_dict_with_a_d_member_but_no_kty_is_kept():
    assert sanitize_metadata({"d": "2026-10-10", "k": "v"}) == {"d": "2026-10-10", "k": "v"}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('"' + '\\"' * 50_000 + "\\", id="escaped-quotes-then-lone-backslash"),
        pytest.param("'" + "\\'" * 50_000 + "\\", id="single-quoted-lone-backslash"),
        pytest.param(
            json.dumps({"payload": json.dumps({str(i): "x" for i in range(6000)})})[:-3] + "\\",
            id="json-in-json-cut-on-a-backslash",
        ),
        pytest.param('"\\\n' * 40_000, id="backslash-newline"),
        pytest.param('{"kty":"oct","k":"a"}' * 10_000, id="many-small-jwks"),  # ua:allow
    ],
)
def test_jwk_scan_is_linear_on_adversarial_inputs(text):
    # PR #1146 review P1-2 and P2-6: an unfinished string ending on a lone backslash was
    # rescanned from every later quote, and every redaction re-copied the text.
    # Quadratic behaviour took 9 s at 40 KB; linear is far under the bound.
    # CPU growth, not a wall-clock bound: a loaded CI shard can stall any
    # fixed-seconds check (#2201). A quarter-size prefix against the whole.
    assert len(text) >= 100_000
    assert_cpu_growth(cold(redact), text[: len(text) // 4], text, limit=8.0, samples=2)


def test_redacting_many_small_jwks_is_linear():
    # PR #1146 review P2-6: splicing each value back in re-copied the text per key
    # (14.8 s at 1 MB); one pass takes about a second on a loaded desktop.
    unit = '{"kty":"oct","k":"a"}'  # ua:allow
    assert_cpu_growth(cold(redact), unit * 12_500, unit * 50_000, limit=8.0, samples=2)


def test_many_small_jwks_are_all_redacted():
    text = '{"kty":"oct","k":"' + "a1b2_c3d4-" * 5 + '"}'
    out = redact(text * 1000)
    assert "a1b2_c3d4-" not in out
    assert out.count("[REDACTED_SECRET]") == 1000


def test_single_quoted_windows_paths_raise_no_syntax_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = redact("{'kty': 'EC', 'path': 'C:\\dir', 'd': '" + "a1b2_c3d4-" * 5 + "'}")
    assert "a1b2_c3d4-" not in out
    assert [w for w in caught if issubclass(w.category, SyntaxWarning)] == []


SYMMETRIC = "ab+cd/ef_gh-ij" * 4  # lower-case, with base64 (not base64url) characters


@pytest.mark.parametrize("ending", ["", "...", "…[truncated]", "\\"])
@pytest.mark.parametrize("member,kty", [("k", "oct"), ("d", "EC"), ("d", "OKP"), ("qi", "RSA")])
def test_a_private_value_cut_off_in_jwk_context_is_redacted(member, kty, ending):
    # Second review P1: once kty marks a member private, its value goes up to the
    # closing quote or the end of the text, whatever its alphabet or ending.
    out = redact('{"kty":"' + kty + '","' + member + '":"' + SYMMETRIC + ending)
    assert SYMMETRIC[:12] not in out


@pytest.mark.parametrize("member,kty", [("k", "oct"), ("d", "EC"), ("p", "RSA")])
def test_a_private_value_of_any_alphabet_in_jwk_context_is_redacted(member, kty):
    for value in (SYMMETRIC, "lower case words with spaces", "a"):
        out = redact(json.dumps({"kty": kty, member: value}))
        assert value not in out.replace("[REDACTED_SECRET]", "")


def rsa_multiprime() -> tuple[dict, list[str]]:
    secrets = ["oth-r-Pr1me_" * 4, "oth-d-Exp0nent" * 4, "oth-t-C0eff_" * 4]
    other = {"r": secrets[0], "d": secrets[1], "t": secrets[2]}
    key = {"n": "pub", "e": "AQAB", "oth": [other, dict(other)], "kty": "RSA"}
    return key, secrets


@pytest.mark.parametrize("encode", [json.dumps, repr, lambda k: json.dumps(k, indent=2)])
def test_rsa_multiprime_other_primes_are_redacted_in_text(encode):
    # Second review P2: "oth" holds objects with private r, d and t and no kty
    # of their own.
    key, secrets = rsa_multiprime()
    out = redact(encode(key))
    for secret in secrets:
        assert secret not in out
    assert "AQAB" in out


def test_rsa_multiprime_other_primes_are_withheld_in_metadata():
    key, secrets = rsa_multiprime()
    for meta in (key, {"wrapper": {"jwk": key}}):
        out = json.dumps(sanitize_metadata(meta))
        for secret in secrets:
            assert secret not in out


def test_an_oth_member_outside_an_rsa_key_is_kept():
    text = json.dumps({"kty": "EC", "oth": [{"r": "public-note"}]})
    assert redact(text) == text
    assert sanitize_metadata({"oth": [{"r": "x"}]}) == {"oth": [{"r": "x"}]}


def _rollout_line(output: str) -> str:
    # A synthetic agent-log record: ordinary fields around one tool output.
    return json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "id": "call-1", "output": output},
        }
    )


def _record_lines(count: int = 3) -> str:
    return "\n".join(
        json.dumps({"type": "event_msg", "payload": {"type": "turn", "turn_id": f"t-{i}"}})
        for i in range(count)
    )


@pytest.mark.parametrize(
    "fragment",
    ['{"kty":"RSA","oth":[', 'jwk = {"kty": "RSA", "n": "pub", "oth": [\n'],
    ids=["json", "python"],
)
def test_a_cut_off_oth_array_does_not_claim_later_records(fragment):
    # Task #2317: an unclosed "oth" array (an excerpt cut off in a tool
    # output) made every later object an "oth" entry with every member
    # private, so `payload`, `type`, `turn_id` and `id` were reported as key
    # material on single-line JSONL. Only r, d and t are an entry's private
    # members (RFC 7518 6.3.2.7).
    output = fragment + "\n...\n" + _record_lines()
    for text in (output, _rollout_line(output)):
        assert private_members(text) == []
        assert redact(text) == text
        assert not [f for f in check_secrets("rollout.jsonl", text) if "JWK" in f.message]


@pytest.mark.parametrize("cut", ['{"oth":[\n...\n', '{"oth":[{"r":"a"}\n'], ids=["open", "entry"])
def test_a_typed_key_after_an_untyped_cut_off_oth_keeps_its_own_type(cut):
    # PR #1378 review P2: an untyped owner's cut-off "oth" array made a later
    # EC key one of its entries, so the key lost its own type and its private
    # "d" passed both public redactors. An entry carries no kty; its own wins.
    value = "lower case words with spaces"
    text = cut + json.dumps({"kty": "EC", "crv": "P-256", "x": "pub", "d": value})  # ua:allow
    assert [member.name for member in private_members(text)] == ["d"]
    assert value not in redact(text)
    assert value not in json.dumps(sanitize_metadata({"output": text}))


def test_a_typed_entry_in_an_rsa_oth_array_keeps_every_private_reading():
    # Fail closed both ways: the entry's own EC type makes d private, and the
    # RSA owner still makes the entry's r and t private.
    text = '{"kty":"RSA","n":"pub","oth":[{"kty":"EC","r":"rv","d":"dv","t":"tv"}]}'  # ua:allow
    assert sorted(member.name for member in private_members(text)) == ["d", "r", "t"]


def test_a_cut_off_oth_entry_still_reports_its_private_members():
    # Fail closed on a malformed key that looks like one: the cut-off entry's
    # r, d and t stay private, and a well-formed key in the same line too.
    prime, exponent, coefficient = (secrets.token_urlsafe(32) for _ in range(3))
    entry = {"r": prime, "d": exponent, "t": coefficient}
    cut = json.dumps({"kty": "RSA", "n": "pub", "oth": [entry]})[: -len("]}")]
    private = secrets.token_urlsafe(32)
    whole = json.dumps({"kty": "EC", "crv": "P-256", "x": "pub", "d": private})  # ua:allow
    text = _rollout_line(cut + "\n" + _record_lines() + "\n" + whole)
    names = {member.name for member in private_members(_rollout_line(cut + "\n" + whole))}
    assert names == {"r", "d", "t"}
    names = {member.name for member in private_members(text)}
    assert names <= {"r", "d", "t"} and names
    out = redact(text)
    for value in (prime, exponent, coefficient, private):
        assert value not in out


# --- Codex review round 2 ------------------------------------------------------

OTHER = ["oth-r-Pr1me_" * 4, "oth-d-Exp0nent" * 4, "oth-t-C0eff_" * 4]


def _fill(template: str) -> str:
    for value in OTHER:
        template = template.replace("@", value, 1)
    return template


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            _fill('{"kty":"RSA","n":"pub","\\u006fth":[{"r":"@","d":"@","t":"@"}]}'),  # ua:allow
            id="escaped-oth",
        ),
        pytest.param(
            _fill(
                '{"\\u006bty":"\\u0052SA","oth":[{"\\u0072":"@","\\u0064":"@","t":"@"}]}'  # ua:allow
            ),  # ua:allow
            id="escaped-kty-and-members",
        ),
        pytest.param(
            _fill('{"kty":"oct","\\u006b":"@","d":"@","t":"@"}'),  # ua:allow
            id="escaped-k",  # ua:allow
        ),  # ua:allow
    ],
)
def test_escaped_member_names_keep_their_ownership(text):
    # Round 2 P1: "oth" is valid JSON for "oth"; names (and kty values)
    # are decoded before they are classified.
    assert json.loads(text)  # valid JSON
    out = redact(text)
    assert OTHER[0] not in out
    if 'u006b"' not in text:
        assert OTHER[1] not in out


@pytest.mark.parametrize("quote", ['"""', "'''"])
@pytest.mark.parametrize("layout", ["inline", "multiline"])
def test_a_jwk_in_a_python_triple_quoted_string_is_redacted(quote, layout):
    # Round 2 P1: the opening triple quote was read as an empty string plus an
    # opening quote, so the object's brace was swallowed and the key kept.
    secret = "Ab1_-Cd2" * 6
    body = '{"kty":"oct","k":"' + secret + '"}'  # ua:allow
    if layout == "multiline":
        body = '{\n    "kty": "oct",\n    "k": "' + secret + '"\n}'  # ua:allow
    text = "JWK_JSON = " + quote + body + quote + "\nnext = 1\n"
    out = redact(text)
    assert secret not in out
    assert "next = 1" in out


def test_deeply_nested_oth_entries_are_linear_and_redacted():
    # Round 2 P2: each nested "oth" entry copied all its descendants into its
    # parent on close, quadratic in the depth (720 KB took 12 s).
    def nested(depth: int) -> str:
        return '{"kty":"RSA","oth":[' + '{"r":"Pr1me_x","oth":[' * depth

    text = nested(30_000)
    assert len(text) > 450_000
    assert_cpu_growth(cold(redact), nested(7_500), text, limit=8.0, samples=2)
    out = redact(text)
    assert "Pr1me_x" not in out
    closed = '{"kty":"RSA","oth":[' + '{"r":"Pr1me_x","oth":[' * 50 + "]}" * 50 + "]}"  # ua:allow
    assert "Pr1me_x" not in redact(closed)


# --- Codex review round 3 ------------------------------------------------------


def test_every_private_member_of_an_encoded_jwk_is_reported():
    # Round 3 P1: an encoded JWK reported one member for its enclosing string,
    # the first one, so a placeholder first value exempted real p and q.
    from unified_agent.telemetry import jwk

    real = "Pr1me_Fact0r-" * 4
    key = dict(kty="RSA", n="pub", e="AQAB", d="[REDACTED]", p=real, q=real)
    text = json.dumps({"body": json.dumps(key)})
    members = jwk.private_members(text)
    assert {m.value for m in members} >= {"[REDACTED]", real}
    assert len({(m.start, m.end) for m in members}) == 1  # one enclosing span
    assert real not in redact(text)


VALUE = "Ab1_-Cd2" * 6


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            repr(dict(kty="oct"))[:-1]
            + ", 'k': ('"
            + VALUE[:24]
            + "' '"
            + VALUE[24:]
            + "'), 'kid': 'kept'}",
            id="implicit-concat",
        ),
        pytest.param("{'kty': 'oct', 'k': r'" + VALUE + "', 'kid': 'kept'}", id="raw-prefix"),
        pytest.param("{'kty': 'oct', 'k': b'" + VALUE + "', 'kid': 'kept'}", id="bytes-prefix"),
        pytest.param("{'kty': 'oct', 'k': f'" + VALUE + "', 'kid': 'kept'}", id="f-prefix"),
        pytest.param(
            '{"kty": "oct", "k": "'  # ua:allow
            + VALUE[:24]
            + '" + "'
            + VALUE[24:]
            + '", "kid": "kept"}',  # ua:allow
            id="plus",
        ),
        pytest.param(
            '{"kty": "oct", "k": "'  # ua:allow
            + VALUE[:24]
            + '" "'
            + VALUE[24:]
            + '", "kid": "kept"}',  # ua:allow
            id="adjacent",
        ),
        pytest.param(
            '{"kty": "EC", "d": ["' + VALUE + '"], "kid": "kept"}',  # ua:allow
            id="array",  # ua:allow
        ),  # ua:allow
        pytest.param(
            '{"kty": "EC", "d": {"v": "' + VALUE + '"}, "kid": "kept"}',  # ua:allow
            id="object",  # ua:allow
        ),  # ua:allow
        pytest.param(
            '{"kty": "EC", "kid": "kept", "d": (\n  "' + VALUE + '"\n)}',  # ua:allow
            id="last-member",  # ua:allow
        ),
    ],
)
def test_a_private_member_that_is_not_one_plain_string_is_redacted_whole(text):
    # Round 3 P2: a value that was not a single plain string token lost its
    # ownership. The whole value expression goes, up to the next ',' or '}' at
    # its own depth.
    out = redact(text)
    for part in (VALUE[:24], VALUE[24:]):
        assert part not in out
    assert "kept" in out


def test_a_private_member_expression_cut_off_is_redacted_to_the_end():
    out = redact(repr(dict(kty="oct"))[:-1] + ", 'k': ('" + VALUE + "' '" + VALUE)
    assert VALUE not in out and VALUE[:12] not in out


def test_nested_private_member_expressions_stay_linear():
    prefix = json.dumps(dict(kty="oct"), separators=(",", ":"))[:-1] + ',"k":('
    text = prefix * 30_000 + '"' + VALUE + '"'
    assert_cpu_growth(cold(redact), prefix * 7_500 + '"' + VALUE + '"', text, limit=8.0, samples=2)
    out = redact(text)
    assert VALUE not in out


# --- Codex review round 4 ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "const jwk = {kty: 'oct', k: '" + VALUE + "', kid: 'kept'};",  # ua:allow
        'const jwk = {kty: "EC", crv: "P-256", d: "' + VALUE + '", kid: "kept"};',
        "jwk = {\n  kty: 'RSA',\n  $d: 'x',\n  qi: '" + VALUE + "',\n  kid: 'kept'\n}",  # ua:allow
        "{kty: 'RSA', oth: [{r: '" + VALUE + "', d: '" + VALUE + "'}], kid: 'kept'}",  # ua:allow
    ],
    ids=["oct", "ec", "multiline-rsa", "rsa-oth"],
)
def test_bare_javascript_member_names_are_recognized(text):
    # Round 4 P2: unquoted JS member names were not read as members.
    out = redact(text)
    assert VALUE not in out and VALUE[:12] not in out
    assert "kept" in out


def test_bare_names_outside_a_jwk_are_kept():
    text = "{d: 'Ab1_-Cd2Ab1_-Cd2', k: 'v', note: 'x'} and see http://example.invalid/a"
    assert redact(text) == text


@pytest.mark.parametrize(
    "value",
    [
        "([{'r': '@', 'd': '@', 't': '@'}])",
        "tuple([{'r': '@', 'd': '@', 't': '@'}])",
        "'@'",
    ],
    ids=["tuple", "call", "string"],
)
def test_an_oth_value_that_is_not_a_plain_array_is_redacted_whole(value):
    # Round 4 P2: "oth" was excluded from expression capture.
    text = "{'kty': 'RSA', 'n': 'pub', 'oth': " + _fill(value) + ", 'kid': 'kept'}"  # ua:allow
    out = redact(text)
    for secret in OTHER:
        assert secret not in out
    assert "kept" in out and "pub" in out


def test_an_oth_expression_outside_an_rsa_key_is_kept():
    text = "{'kty': 'EC', 'oth': ([{'r': 'note'}]), 'kid': 'kept'}"
    assert redact(text) == text


# --- PR #1146 review P3 follow-ups (task #2019) ---------------------------------

#: Too short and weak for the no-context fallback: only the JWK context catches it.
WEAK = "fake" + "-weak-value"


@pytest.mark.parametrize(
    "kty,member", [("oct", "k"), ("EC", "d"), ("RSA", "qi")], ids=["oct", "ec", "rsa"]
)
@pytest.mark.parametrize(
    "lead",
    [
        'say "hi ',
        'a "b" and "c ',
        "it's 'odd ",
        "x = 'note: ",
    ],
    ids=["double", "balanced-then-stray", "apostrophe-then-single", "single"],
)
def test_an_unbalanced_quote_before_a_jwk_does_not_hide_it(lead, kty, member):
    # An unbalanced quote paired with the quote that opens the key's first member
    # name, so every later string was read inside out and kty was never seen.
    for key in (
        '{"kty":"' + kty + '","' + member + '":"' + WEAK + '"}',  # ua:allow
        "{'kty': '" + kty + "', '" + member + "': '" + WEAK + "'}",  # ua:allow
    ):
        out = redact(lead + key + " thanks")
        assert WEAK not in out, out
        assert out.endswith(" thanks")


def test_an_unbalanced_quote_inside_a_jwk_does_not_hide_a_later_member():
    text = '{"kty":"oct","note":"x"y","k":"' + WEAK + '"} thanks'  # ua:allow
    out = redact(text)
    assert WEAK not in out and out.endswith(" thanks")


STRONG = "FAKEkey0" * 6  # 48 base64url characters with upper case and digits


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"k": STRONG}),
        '"k": "' + STRONG + '"',
        '{"k": "' + STRONG,
        json.dumps({"payload": json.dumps({"k": STRONG})}),
    ],
    ids=["object", "member", "cut", "encoded"],
)
def test_the_no_kty_fallback_catches_an_oct_key_value(text):
    # The fallback for text with no visible kty listed d, p, q, dp, dq and qi
    # but not the symmetric key's k.
    assert STRONG not in redact(text)


def test_apostrophes_around_a_jwk_do_not_make_one_redacted_span():
    # An apostrophe opened a "string" that ran to the next apostrophe, so the
    # whole run was replaced and a stray quote inserted.
    text = "Here's the key " + '{"kty":"EC","d":"' + WEAK + '"}' + " and that's all"  # ua:allow
    expected = "Here's the key " + '{"kty":"EC","d":"[REDACTED_SECRET]"}' + " and that's all"
    assert redact(text) == expected
    assert redact("Here's a note, that's all, it's fine") == "Here's a note, that's all, it's fine"


@pytest.mark.parametrize("kty,private", [("EC", "d"), ("OKP", "d"), ("oct", "k")])
def test_rsa_only_member_names_are_kept_outside_an_rsa_key(kty, private):
    public = {"p": "page-2", "q": "search terms", "dp": "x", "dq": "y", "qi": "z"}
    key = {"kty": kty, private: WEAK, **public}
    out = redact(json.dumps(key))
    assert WEAK not in out
    for value in public.values():
        assert f'"{value}"' in out
    meta = sanitize_metadata(key)
    assert meta[private] == "[REDACTED_SECRET]"
    assert {name: meta[name] for name in public} == public


def test_rsa_member_names_are_still_redacted_in_an_rsa_key():
    names = ("d", "p", "q", "dp", "dq", "qi")
    key = {"kty": "RSA", "n": "pub", **{name: f"{WEAK}-{name}" for name in names}}
    out = redact(json.dumps(key))
    assert WEAK not in out
    meta = sanitize_metadata(key)
    assert all(meta[name] == "[REDACTED_SECRET]" for name in names)


@pytest.mark.parametrize("prefix", ["u", "b", "r", "rb"])
def test_a_prefixed_python_repr_of_a_jwk_is_redacted(prefix):
    # A Python 2 or bytes repr prefixes every value (u'oct'), so kty was never
    # read as a plain string and the key passed.
    text = "{%s'kty': %s'oct', %s'k': %s'%s', 'kid': 'kept'}" % ((prefix,) * 4 + (WEAK,))
    out = redact(text)
    assert WEAK not in out and "kept" in out


# --- // comment bodies with an orphan quote (tasks #2224, #2236) -------------
# A // body joins the isolated comment view like a # body, so an orphan quote
# in its prose cannot hide a braced or identity-escaped private key. Each
# member is reported once, and a placeholder never displaces a real value.


def _slash_fake():
    return "q" + secrets.token_hex(12) + "z"


def _slash_assert_private(text, value):
    expected = {i for i, line in enumerate(text.splitlines(), 1) if value in line}
    members = private_members(text)
    observed = {
        "redacted": value not in redact_sensitive_content(text, max_len=20 * len(text) + 1000),
        "scanner": any(value in text[m.start : m.end] for m in members),
        "audit": bool(expected)
        and expected
        <= {
            f.line
            for f in check_secrets("fixture.js", text)
            if f.rule == "SECRETS-KEY" and "JWK private member" in f.message
        },
        "relay": any(
            f.action == BLOCK and value in text[f.start : f.end] for f in _jwk_findings(text)
        ),
        "relay_gate": run_gates(text).verdict == BLOCK,
    }
    assert all(observed.values()), observed


def _slash_assert_public(text, value):
    assert value in redact_sensitive_content(text, max_len=20 * len(text) + 1000)
    assert not any(value in text[m.start : m.end] for m in private_members(text))
    assert not any(
        f.action == BLOCK and value in text[f.start : f.end] for f in _jwk_findings(text)
    )


def test_a_comment_between_a_member_name_and_its_colon_is_read():
    # Task #2165: the gap is legal JavaScript, and without it the member is
    # never read, so a private value stays public on all four consumers.
    for gap in TRIVIA_GAPS:
        value = _slash_fake()
        text = f'{{"kty":"RSA", d{gap}:"{value}"}}'  # ua:allow
        _slash_assert_private(text, value)


def test_a_jwk_inside_line_comment_prose_is_still_read():
    # PR #1417 review (Devin): reading a // gap would take a colon in the prose
    # as the member's own separator and swallow the kty evidence beside it, so a
    # JWK hidden in that prose stopped being detected. Line comments are not a
    # gap; the comment scan keeps reading them.
    value = _slash_fake()
    text = f'{{"kty":"RSA", d// kty:"oct", k:"{value}"\n:"pub"}}'  # ua:allow
    _slash_assert_private(text, value)


def test_a_comment_gap_does_not_make_a_public_member_private():
    # The other direction: reading the gap must not turn a public member into a
    # private one, nor redact the value behind it.
    for gap in TRIVIA_PUBLIC_GAPS:
        value = _slash_fake()
        text = f'{{"kty":"oct", "kid"{gap}:"{value}"}}'  # ua:allow
        _slash_assert_public(text, value)


@pytest.mark.parametrize("name", ["block_open_inside", "line_after_member"])
def test_repeated_comment_trivia_after_a_member_name_stays_linear(name):
    # The first attempt at this fix rescanned the same characters: `a//b `
    # repeated went 0.68s -> 15.97s at 32k, and a block-comment body past 25s.
    # Reading the gap must not reintroduce that.
    unit = "a/*b " if name == "block_open_inside" else "a//b "
    wrap = (lambda body: "/*" + body + "*/") if name == "block_open_inside" else (lambda body: body)
    assert_cpu_growth(
        cold(redact), "x" + wrap(unit * 8_000), "x" + wrap(unit * 32_000), limit=8.0, samples=2
    )


SLASH_BREAKS = ["\n", "\r\n", "\r", " ", " "]
# Task #2165: a comment between a member name and its colon is legal
# JavaScript. The gap must be read, and its body may not be consumed when it
# holds a quote, brace or colon (task #2361: a literal hidden in that prose
# must stay visible to the comment scan).
TRIVIA_GAPS = ["/*gap*/", "/*gap*/\n", "/* two\n   lines */", "/*a*b*/"]
TRIVIA_PUBLIC_GAPS = ["/*gap*/"]
SLASH_BRACED_TYPE = '"k\\u{74}y":"RSA"'
SLASH_PLAIN_TYPE = '"kt' + 'y":"RSA"'
SLASH_PREFIXES = [
    "// orphan ' ",
    '// orphan " ',
    "x = 1 // orphan ' ",
    "# // orphan ' ",
    "// note ' it's ",
]
# Each key spells dp for JS (braced or identity escapes, with or without a
# continuation at any position of the key).
SLASH_KEYS = [
    "\\d\\p",
    "\\u{64}p",
    "\\u{64}\\u{70}",
    "\\d\\{0}p",
    "\\{0}\\d\\p",
    "\\d\\p\\{0}",
    "\\u{64}\\{0}p",
]


# The #2239 shape: a plain type with a trailing continuation after "\d\p".
_PLAIN_TRAILING_IDENTITY = "\\d\\p\\{0}"
# The (key, kind) pairs of the sampled matrix. The plain type with a trailing
# identity key is its own strict xfail below, so this matrix carries no
# per-case marks and stays pairwise-sampled (tests/param_sampling.py).
_SLASH_KEY_KINDS = [
    (key, kind)
    for key in SLASH_KEYS
    for kind in ("braced", "plain")
    if (key, kind) != (_PLAIN_TRAILING_IDENTITY, "plain")
]


# Tasks #2227, #2245: shapes where only the JS reading of an identity-escaped
# key ("\\d\\p" is dp) names the private member. BR is a line break, VAL the
# private value. Each is decoder-private: JS evaluates the embedded object to
# an RSA key whose dp member is VAL.
_BRACED = SLASH_BRACED_TYPE
_IDENTITY_SHAPES = {
    "hash-plain": "# note {" + SLASH_PLAIN_TYPE + ',"\\d\\p":VAL}',
    "live-continued-type": "{" + SLASH_PLAIN_TYPE.replace("kt", "k\\BRt") + ',"\\d\\p":VAL}',
    "live-continued-type-value": "{"
    + SLASH_PLAIN_TYPE.replace("RSA", "R\\BRSA")
    + ',"\\d\\p":VAL}',
    "slash-leading-type": '// orphan \' {"\\BRk\\u{74}y":"RSA","\\d\\p":VAL}',
    "slash-middle-type": '// orphan \' {"k\\BR\\u{74}y":"RSA","\\d\\p":VAL}',
    "block-slash-orphan": "/* // orphan ' {" + _BRACED + ',"\\d\\p":VAL} */',
    "block-slash-middle-type": '/* // orphan \' {"k\\BR\\u{74}y":"RSA","\\d\\p":VAL} */',
    "block-slash-type-value": '/* // orphan \' {"k\\u{74}y":"R\\BRSA","\\d\\p":VAL} */',
    "block-slash-private-key": "/* // orphan ' {" + _BRACED + ',"\\d\\BRp":VAL} */',
    "block-slash-reversed": '/* // orphan \' {"\\d\\p":VAL,' + _BRACED + "} */",
    "block-hash-orphan": "/* # orphan ' {" + _BRACED + ',"\\d\\p":VAL} */',
    "block-plain": "/* {" + SLASH_PLAIN_TYPE + ',"\\d\\p":VAL} */',
    "slash-two-lines": "// orphan ' {" + _BRACED + ',BR// "\\u{64}p":VAL}',
    "slash-two-lines-identity": "// orphan ' {" + _BRACED + ',BR// "\\d\\p":VAL}',
    "slash-two-lines-plain": "// {" + SLASH_PLAIN_TYPE + ',BR// "\\d\\p":VAL}',
}
# The same comment views keep JS public roles: the last discriminator decides,
# a name private only for RSA stays public in an EC key, and live source with
# no JS-only evidence keeps its Python meaning ("\\d" is not d there).
_EC_TYPE = SLASH_PLAIN_TYPE.replace("RSA", "EC")
_IDENTITY_PUBLIC_SHAPES = {
    "hash-ec-rsa-only": "# note {" + _EC_TYPE + ',"\\p":VAL}',
    "hash-last-type-ec": "# note {" + SLASH_PLAIN_TYPE + "," + _EC_TYPE + ',"\\d\\p":VAL}',
    "hash-public-member": "# note {" + SLASH_PLAIN_TYPE + ',"\\d\\p":"x","\\e":VAL}',
    "block-ec-rsa-only": "/* {" + _EC_TYPE + ',"\\q":VAL} */',
    "live-plain-type": "{" + SLASH_PLAIN_TYPE + ',"\\d\\p":VAL}',
}


class TestSlashCommentOrphan:
    """Pure scanner tests: no store is bootstrapped or touched."""

    @pytest.fixture
    def bootstrap_test_store(self):
        return False

    @pytest.fixture(autouse=True)
    def _forbid_store_access(self, isolated_env, monkeypatch):
        from unified_agent import storage

        def refused(*args, **kwargs):
            pytest.fail("pure JWK scanner tests must not access a database")

        for name in ("init_db", "connect", "connect_read_only"):
            monkeypatch.setattr(storage, name, refused)

    @pytest.mark.parametrize("line", SLASH_BREAKS)
    @pytest.mark.parametrize("prefix", SLASH_PREFIXES)
    @pytest.mark.parametrize("key,kind", _SLASH_KEY_KINDS)
    def test_slash_comment_orphan_hides_no_escaped_private_key(self, line, prefix, key, kind):
        private = _slash_fake()
        member = '"' + key.replace("{0}", line) + '":' + json.dumps(private)
        text = (
            prefix
            + "{"
            + (SLASH_BRACED_TYPE if kind == "braced" else SLASH_PLAIN_TYPE)
            + ","
            + member
            + "}"
        )
        _slash_assert_private(text, private)

    # Task #2239: a plain type, an identity key and a trailing continuation
    # ("\\d\\p\\<break>"). The comment body is read with JS identity escapes,
    # with the object's last discriminator deciding the role.
    @pytest.mark.parametrize("line", SLASH_BREAKS)
    @pytest.mark.parametrize("prefix", SLASH_PREFIXES)
    def test_slash_comment_plain_type_trailing_identity(self, line, prefix):
        private = _slash_fake()
        member = '"' + _PLAIN_TRAILING_IDENTITY.replace("{0}", line) + '":' + json.dumps(private)
        _slash_assert_private(prefix + "{" + SLASH_PLAIN_TYPE + "," + member + "}", private)

    @pytest.mark.parametrize("line", SLASH_BREAKS)
    @pytest.mark.parametrize("shape", sorted(_IDENTITY_SHAPES))
    def test_identity_escaped_private_key_in_comment_or_continued_source(self, shape, line):
        # Tasks #2227, #2245: every scanned comment body (#, //, /* */, a //
        # object over two lines) and live source whose type key continues on
        # the next line read "\\d\\p" as dp.
        private = _slash_fake()
        text = _IDENTITY_SHAPES[shape].replace("BR", line).replace("VAL", json.dumps(private))
        _slash_assert_private(text, private)

    @pytest.mark.parametrize("shape", sorted(_IDENTITY_PUBLIC_SHAPES))
    def test_identity_escaped_comment_keys_keep_public_roles(self, shape):
        public = _slash_fake()
        text = _IDENTITY_PUBLIC_SHAPES[shape].replace("VAL", json.dumps(public))
        _slash_assert_public(text, public)
        assert not [
            f
            for f in check_secrets("fixture.js", text)
            if f.rule == "SECRETS-KEY" and "JWK private member" in f.message
        ]

    @pytest.mark.parametrize(
        "line",
        [
            "# note {" + SLASH_PLAIN_TYPE + ',"\\d\\p":"v"}\n',
            "/* // orphan ' {" + SLASH_PLAIN_TYPE + ',"\\d\\p":"v"} */\n',
            "{" + SLASH_PLAIN_TYPE.replace("kt", "k\\\nt") + ',"\\d\\p":"v"}\n',
            "// " + '"\\d\\p":' * 8 + "\n",
        ],
        ids=["hash", "block", "live-continued", "fields-without-type"],
    )
    def test_identity_escape_scan_is_linear(self, line):
        def build(size):
            return line * (size // len(line))

        small, large = build(20_000), build(80_000)
        growth = cpu_growth(cold(private_members), small, large, limit=8)
        assert growth.ratio < 8, f"x4 input took x{growth.ratio:.2f} time"
        peak = peak_growth(cold(private_members), small, large, limit=8)
        assert peak.ratio < 8, f"x4 input took x{peak.ratio:.2f} peak"

    @pytest.mark.parametrize(
        "tail",
        [lambda n: "}" * n, lambda n: " " * (8 * n), lambda n: "\n"],
        ids=["closed", "unterminated-whitespace", "unterminated"],
    )
    @pytest.mark.parametrize("prefix", ["# ", "// ", "/* "])
    def test_nested_identity_values_are_not_copied_per_level(self, prefix, tail):
        # Review at 8a5187cc: each nested "\\d\\p" value holds the next one, so
        # copying every level's value (and trimming the shared whitespace tail
        # once per level) grew quadratically. Contained members are dropped,
        # as the ordinary scan never reports them; the outer one masks all.
        def build(depth):
            nested = '{"kty":"RSA","\\d\\p":' * depth + json.dumps("v" * 16)
            return prefix + nested + tail(depth)

        small, large = build(400), build(1600)
        growth = cpu_growth(cold(private_members), small, large, limit=8)
        assert growth.ratio < 8, f"x4 depth took x{growth.ratio:.2f} time"
        peak = peak_growth(cold(private_members), small, large, limit=8)
        assert peak.ratio < 8, f"x4 depth took x{peak.ratio:.2f} peak"
        members = private_members(large)
        assert len(members) == len(private_members(small)) <= 2  # never one per level
        assert sum(len(member.value) for member in members) <= 2 * len(large)
        assert "v" * 16 not in redact_sensitive_content(large)

    @pytest.mark.parametrize("prefix", SLASH_PREFIXES)
    def test_slash_comment_orphan_keeps_public_members(self, prefix):
        private, modulus, exponent = _slash_fake(), _slash_fake(), _slash_fake()
        text = prefix + "{" + SLASH_BRACED_TYPE + ',"n":' + json.dumps(modulus)
        text += ',"\\u{65}":' + json.dumps(exponent) + ',"\\d\\p":' + json.dumps(private) + "}"
        _slash_assert_private(text, private)
        _slash_assert_public(text, modulus)
        _slash_assert_public(text, exponent)

    @pytest.mark.parametrize("line", SLASH_BREAKS)
    def test_floor_division_keeps_live_members(self, line):
        # Python division is not a comment: live members after it keep their roles.
        private, modulus = _slash_fake(), _slash_fake()
        text = "half = total // 2; key = {" + SLASH_PLAIN_TYPE + ',"n":' + json.dumps(modulus)
        text += ',"d":' + json.dumps(private) + "}" + line + "rest = a // b  # done" + line
        _slash_assert_private(text, private)
        _slash_assert_public(text, modulus)

    def test_floor_division_then_hash_comment_still_masks(self):
        private = _slash_fake()
        text = "half = total // 2  # {" + SLASH_PLAIN_TYPE + ',"d":' + json.dumps(private) + "}"
        _slash_assert_private(text, private)

    @pytest.mark.parametrize(
        "text,start,end,audit",
        [
            ('// {"kt' + 'y":"RSA",\\\n"d":"PUBLICXXX"}\n', 22, 33, (23, 32)),
            ('// {"kt' + 'y":"RSA","d":"PUBLICXXX"}\n', 20, 31, (21, 30)),
            ("// orphan ' {" + SLASH_BRACED_TYPE + ',"d":"PUBLICXXX"}\n', None, None, None),
        ],
    )
    def test_one_member_per_name_and_span(self, text, start, end, audit):
        # Task #2236: the snippet and recovery scans each found the same member.
        members = private_members(text)
        keys = [(m.name, m.start, m.end) for m in members]
        assert len(keys) == len(set(keys)), members
        if start is not None:
            assert [(m.name, m.start, m.end, m.audit_span) for m in members] == [
                ("d", start, end, audit)
            ]

    @pytest.mark.parametrize(
        "line",
        [
            "// orphan ' {" + SLASH_BRACED_TYPE + ',"\\d\\\n',
            "// orphan ' {" + SLASH_BRACED_TYPE + ',"\\d\\p":"v"}\n',
            "x = a // b  # {" + SLASH_PLAIN_TYPE + ',"n":"v"}\n',
            "// " + "'" * 32 + " {" + SLASH_PLAIN_TYPE + "\n",
        ],
        ids=["continued", "complete", "division-hash", "orphan-run"],
    )
    def test_slash_comment_scan_is_linear(self, line):
        def build(size):
            return line * (size // len(line))

        small, large = build(20_000), build(80_000)
        growth = cpu_growth(cold(private_members), small, large, limit=8)
        assert growth.ratio < 8, f"x4 input took x{growth.ratio:.2f} time"
        peak = peak_growth(cold(private_members), small, large, limit=8)
        assert peak.ratio < 8, f"x4 input took x{peak.ratio:.2f} peak"

    @pytest.mark.parametrize("placeholder", ["REDACTED", "${TOKEN}", "..."])
    @pytest.mark.parametrize("order", ["placeholder-first", "private-first"])
    def test_dedupe_never_trades_a_private_value_for_a_placeholder(self, placeholder, order):
        # Two members of one replaced literal share a span; a placeholder must not
        # displace the real value from the audit or relay (independent review).
        private = _slash_fake()
        keys = [{"kt" + "y": "RSA", "d": placeholder}, {"kt" + "y": "RSA", "d": private}]
        if order == "private-first":
            keys.reverse()
        text = "'''" + json.dumps(keys) + "'''"
        _slash_assert_private(text, private)
        assert any(member.value == private for member in private_members(text))

    def test_long_slash_line_without_type_is_linear(self):
        # Every // pair on one line used to rescan the rest of that line.
        public = _slash_fake()

        def build(size):
            return "// " * (size // 3) + '\n{"kt' + 'y":"RSA","n":' + json.dumps(public) + "}"

        small, large = build(4_000), build(16_000)
        _slash_assert_public(large, public)
        growth = cpu_growth(cold(private_members), small, large, limit=8)
        assert growth.ratio < 8, f"x4 input took x{growth.ratio:.2f} time"
        peak = peak_growth(cold(private_members), small, large, limit=8)
        assert peak.ratio < 8, f"x4 input took x{peak.ratio:.2f} peak"

    # --- // and block comments in a key gap (task #2366) ----------------------
    # The live scan keeps reading a comment body as prose, so no evidence in it
    # is lost; a separate view with the gap blanked reads the member across it.

    @pytest.mark.parametrize("line", SLASH_BREAKS)
    @pytest.mark.parametrize(
        "key,gap",
        [
            ("d", "//gap{0}"),
            ('"d"', "//gap{0}"),
            ("d", " // gap {0}  "),
            ("d", "//a{0}//b{0}"),
            ("d", "//a{0}/*b*/"),
            ("d", "/*a*///b{0}"),
            ("\\u{{64}}", "//gap{0}"),
        ],
        ids=["bare", "quoted", "spaced", "two-lines", "then-block", "after-block", "escaped-name"],
    )
    def test_a_line_comment_key_gap_hides_no_private_member(self, line, key, gap):
        value = _slash_fake()
        name = key.format()
        text = '{"kt' + 'y":"RSA", ' + name + gap.format(line) + ':"' + value + '"}'
        _slash_assert_private(text, value)

    @pytest.mark.parametrize("line", SLASH_BREAKS)
    def test_a_line_comment_key_gap_keeps_a_public_member_public(self, line):
        value = _slash_fake()
        _slash_assert_public('{"kt' + 'y":"oct", "kid"//gap' + line + ':"' + value + '"}', value)

    @pytest.mark.parametrize(
        "body",
        ["x" * 5000, 'it\'s {not: code} "at all"', "http://a.example/b"],
        ids=["past-the-old-cap", "structure-in-prose", "url"],
    )
    @pytest.mark.parametrize("form", ["//{0}\n", "/*{0}*/"], ids=["line", "block"])
    def test_a_comment_gap_body_of_any_length_or_prose_is_read(self, body, form):
        value = _slash_fake()
        gap = form.format(body)
        text = '{"kt' + 'y":"RSA", d' + gap + ':"' + value + '"}'
        _slash_assert_private(text, value)

    def test_prose_evidence_and_the_member_across_the_gap_are_both_read(self):
        # The reverted attempt read the body as trivia and lost what it held.
        hidden, across = _slash_fake(), _slash_fake()
        text = '{"kt' + 'y":"RSA", d// kt' + f'y:"oct", k:"{hidden}"\n:"{across}"}}'
        _slash_assert_private(text, hidden)
        _slash_assert_private(text, across)

    @pytest.mark.parametrize(
        "wrap",
        [
            lambda body: "'''" + body + "'''",
            lambda body: "/* " + body + " */",
            lambda body: json.dumps({"body": body}),
        ],
        ids=["triple-string", "block-comment", "encoded-string"],
    )
    def test_a_line_comment_key_gap_is_read_in_decoded_contexts(self, wrap):
        value = _slash_fake()
        text = wrap('{"kt' + 'y":"RSA", d//gap\n:"' + value + '"}')
        assert value not in redact_sensitive_content(text, max_len=20 * len(text) + 1000)
        assert any(f.action == BLOCK for f in _jwk_findings(text))

    def test_a_line_comment_gap_reports_each_member_once(self):
        value = _slash_fake()
        text = '{"kt' + 'y":"RSA", "n":"pub", "d":"' + value + '", q//gap\n:"' + value + 'z"}'
        members = private_members(text)
        keys = [(m.name, m.start, m.end) for m in members]
        assert len(keys) == len(set(keys)) and {m.name for m in members} == {"d", "q"}

    @pytest.mark.parametrize(
        "unit,suffix",
        [
            ("a//b ", ""),
            ("a//b ", "\n:1"),
            ("a//b\n", ""),
            ("d//b\n", ':"v"'),
            ("a/*b ", ""),
            ("a/*b*/ ", ""),
            ("d/*b*/\n", ':"v"'),
            ("a//b\n/*", ""),
        ],
        ids=[
            "one-line-run",
            "one-line-run-then-colon",
            "many-lines",
            "chain-then-colon",
            "unclosed-block",
            "closed-block",
            "block-chain-then-colon",
            "unclosed-block-after-line",
        ],
    )
    def test_comment_gap_scan_is_linear(self, unit, suffix):
        head = '{"kt' + 'y":"RSA", '

        def build(count):
            return head + unit * count + suffix

        small, large = build(8_000), build(32_000)
        growth = cpu_growth(cold(private_members), small, large, limit=8)
        assert growth.ratio < 8, f"x4 input took x{growth.ratio:.2f} time"
        peak = peak_growth(cold(private_members), small, large, limit=8)
        assert peak.ratio < 8, f"x4 input took x{peak.ratio:.2f} peak"


# --- the coarse fallback (task #2441) -----------------------------------------
# Syntaxes no grammar rule reads are covered by over-redaction near a key-type
# marker, never by a new rule. Decided objects and earlier members are left as
# they were; the secrets audit does not run the fallback.


def _coarse_key():
    # Real-shaped dummy material: 32 random bytes, base64url, with each class.
    return "Aa0" + secrets.token_urlsafe(32)


COARSE_SYNTAXES = {
    "toml": '[jwk]\nkty = "EC"\ncrv = "P-256"\nd = "VAL"\n',
    "http-header": "X-Jwk-Kty: OKP; X-Jwk-D: VAL",
    "enum-typed-call": 'JWK(kty=Kty.RSA, d=b64("VAL"))',
    "env-file": "JWK_KTY=RSA\nJWK_D=VAL\n",
}


@pytest.mark.parametrize("syntax", sorted(COARSE_SYNTAXES))
def test_coarse_fallback_masks_a_syntax_no_grammar_rule_reads(syntax):
    value = _coarse_key()
    text = COARSE_SYNTAXES[syntax].replace("VAL", value)
    assert value not in redact(text)
    assert any(value in text[m.start : m.end] for m in private_members(text, coarse=True))
    assert any(f.action == BLOCK and value in text[f.start : f.end] for f in _jwk_findings(text))


@pytest.mark.parametrize(
    "encode", [lambda t: t, lambda t: json.dumps({"msg": t})], ids=["raw", "encoded"]
)
def test_coarse_fallback_leaves_a_decided_public_jwks_and_its_neighbours(encode):
    modulus, request = secrets.token_urlsafe(256), _coarse_key()
    public = json.dumps({"keys": [{"kty": "RSA", "n": modulus, "e": "AQAB", "kid": "k1"}]})
    text = encode(public + " request=" + request)
    assert redact(text) == text
    assert private_members(text, coarse=True) == private_members(text) == []


def test_coarse_fallback_is_additive_and_disjoint():
    grammar, coarse = _coarse_key(), _coarse_key()
    text = '{"kty":"EC","d":"' + grammar + '"}\nJWK_KTY=OKP JWK_D=' + coarse
    plain = private_members(text, coarse=False)
    full = private_members(text)
    assert [m for m in full if m.name != "coarse"] == plain
    assert [m.value for m in full if m.name == "coarse"] == [coarse]
    spans = sorted((m.start, m.end) for m in full)
    assert all(end <= start for (_, end), (start, _) in zip(spans, spans[1:], strict=False))
    out = redact(text)
    assert grammar not in out and coarse not in out
    # The secrets audit keeps the grammar alone: no finding for the coarse run.
    assert len([f for f in check_secrets("fixture.txt", text) if "JWK" in f.message]) == 1


def test_coarse_fallback_reads_escaped_markers_and_keeps_escapes_whole():
    value = _coarse_key()
    for marker in ("\\u006bty: EC", "\\k\\t\\y = r\\\n  sa", "\\nkty=OKP"):
        text = marker + ' cfg "\\n' + value + '"'
        out = redact(text)
        assert value not in out and '"\\n[REDACTED_SECRET]"' in out, marker


def test_coarse_fallback_spares_short_single_case_and_distant_runs():
    hex_id, word = secrets.token_hex(20), "function_call_output_value"
    far = _coarse_key()
    text = f"kty=RSA id={hex_id} {word}" + " ." * 4100 + far
    assert redact(text) == text
    assert "mykty=RSA d=" + far == redact("mykty=RSA d=" + far)


@pytest.mark.parametrize(
    "unit",
    ["kty" + " " * 70, "kty RSA " + "Aa0_" * 6 + " ", "\\k\\t\\y\\u0045C\\\n ", "Aa0-" * 40],
    ids=["marker-past-reach", "marker-and-run", "escaped-marker", "one-long-run"],
)
def test_coarse_fallback_is_linear(unit):
    def build(count):
        return "kty: RSA " + unit * count

    small, large = build(4_000), build(16_000)
    assert_cpu_growth(cold(redact), small, large, limit=8.0, samples=2)
    peak = peak_growth(cold(lambda t: private_members(t, coarse=True)), small, large, limit=8)
    assert peak.ratio < 8, f"x4 input took x{peak.ratio:.2f} peak"


# --- residue inside decided objects (task #2445) --------------------------------
# The JSON scan can type an object and still miss a private member whose name is
# spelled in a syntax it does not read. Inside a decided object every run goes
# unless it is a plainly written public parameter.

RESIDUE_SYNTAXES = {
    "computed-key": '{"kty":"RSA", ["d"]: "VAL"}',
    "concatenated-name": '{"kty":"RSA", "d"+"": "VAL"}',
    "fullwidth-colon": '{"kty":"EC", "d"："VAL"}',
    "equals": '{"kty":"EC", "d" = "VAL"}',
    "arrow": '{"kty":"EC", "d" => "VAL"}',
    "pair-array": '{"kty":"OKP", "members": [["d", "VAL"]]}',
}


@pytest.mark.parametrize("syntax", sorted(RESIDUE_SYNTAXES))
def test_residue_inside_a_decided_object_is_masked(syntax):
    value = _coarse_key()
    text = RESIDUE_SYNTAXES[syntax].replace("VAL", value)
    assert not any(value in text[m.start : m.end] for m in private_members(text, coarse=False))
    assert value not in redact(text)


@pytest.mark.parametrize("indent", [None, 2], ids=["compact", "pretty"])
def test_residue_keeps_plain_public_parameters(indent):
    keys = [
        {"kty": "RSA", "n": secrets.token_urlsafe(256), "e": "AQAB", "alg": "RS256", "use": "sig"},
        {"kty": "EC", "crv": "P-256", "x": _coarse_key(), "y": _coarse_key(), "kid": _coarse_key()},
        {"kty": "OKP", "crv": "Ed25519", "x": _coarse_key(), "x5t#S256": _coarse_key()},
        {"kty": "oct", "kid": _coarse_key(), "key_ops": ["sign"]},
        {"kty": "RSA", "n": _coarse_key(), "e": "AQAB", "x5c": [_coarse_key(), _coarse_key()]},
    ]
    for key in keys:
        text = json.dumps(key, indent=indent)
        assert private_members(text) == [], key["kty"]
    script = "const jwk = {kty: 'EC', crv: 'P-256', x: '" + _coarse_key() + "'};"
    assert private_members(script) == []


def test_residue_masks_what_is_not_a_plain_public_parameter():
    trace, modulus = _coarse_key(), _coarse_key()
    text = json.dumps({"kty": "RSA", "n": modulus, "e": "AQAB", "meta": {"trace": trace}})
    out = redact(text)
    assert trace not in out and modulus in out
    # A public name with a non-plain value is not a public form: it goes too.
    computed = '{"kty":"EC", "x": f("' + trace + '")}'
    assert trace not in redact(computed)


def test_residue_is_linear():
    def build(count):
        return '{"kty":"RSA", "n": "' + "Aa0_" * 8 + '", ' + '"m" = "Aa0-Bb1_Cc2", ' * count + "}"

    small, large = build(2_000), build(8_000)
    assert_cpu_growth(cold(redact), small, large, limit=8.0, samples=2)
    peak = peak_growth(cold(lambda t: private_members(t, coarse=True)), small, large, limit=8)
    assert peak.ratio < 8, f"x4 input took x{peak.ratio:.2f} peak"
