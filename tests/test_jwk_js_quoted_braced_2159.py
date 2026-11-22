"""Quoted JavaScript Unicode escapes add only local private-role evidence."""

import json
import secrets

import pytest
from growth_budget import cold, cpu_growth, peak_growth

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
        pytest.fail("pure quoted JS tests must not access a database")

    for name in ("init_db", "connect", "connect_read_only"):
        monkeypatch.setattr(storage, name, refused)


def fake():
    return "q" + secrets.token_hex(12) + "z"


def assert_private(text, value):
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
    }
    assert all(observed.values()), observed
    assert all(0 <= m.start < m.end <= len(text) for m in members)


def assert_public(text, value):
    assert value in redact_sensitive_content(text, max_len=20 * len(text) + 1000)
    assert not any(value in text[m.start : m.end] for m in private_members(text))
    assert not any(
        f.action == BLOCK and value in text[f.start : f.end] for f in _jwk_findings(text)
    )


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("layers", [0, 1, 3])
@pytest.mark.parametrize(
    "kind,key",
    [
        ("RSA", r"\u{64}"),
        ("RSA", r"\u{70}"),
        ("RSA", r"\u{64}p"),
        ("EC", r"\u{64}"),
        ("OKP", r"\u{64}"),
        ("oct", r"\u{6b}"),
    ],
)
def test_quoted_private_braced_members_all_surfaces(quote, layers, kind, key):
    value, sibling = fake(), fake()
    text = f'const x={{kty:"{kind}",{quote}{key}{quote}:"{value}"}};'
    for _ in range(layers):
        text = json.dumps(text)
    text += f'\nconst publicKey={{kty:"RSA",n:"{sibling}"}};'
    assert_private(text, value)
    assert_public(text, sibling)


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("layers", [0, 1, 3])
@pytest.mark.parametrize("key", [r"\u{6b}ty", r"k\u{74}y", r"kt\u{79}"])
def test_quoted_discriminator_name(quote, layers, key):
    value = fake()
    text = f'{{{quote}{key}{quote}:"RSA",d:"{value}"}}'
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("layers", [0, 1, 3])
@pytest.mark.parametrize(
    "kind,member",
    [(r"\u{52}SA", "d"), (r"\u{45}C", "d"), (r"\u{4f}KP", "d"), (r"\u{6f}ct", "k")],
)
def test_quoted_discriminator_value(quote, layers, kind, member):
    value = fake()
    text = f'{{kty:{quote}{kind}{quote},{member}:"{value}"}}'
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("slashes", [2, 3, 4, 5, 6])
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_backslash_prefixed_literal_name_remains_public(slashes, quote, layers):
    public = fake()
    key = "\\" * slashes + "u{64}"
    text = f'{{kty:"RSA",{quote}{key}{quote}:"{public}"}}'
    for _ in range(layers):
        text = json.dumps(text)
    assert_public(text, public)


@pytest.mark.parametrize("kind", [r"\u{52}SA", r"\u{45}C"])
def test_interpreted_type_does_not_revoke_public_member_roles(kind):
    public = fake()
    member = "k" if kind.endswith("SA") else "p"
    text = f'{{kty:"{kind}",{member}:"{public}"}}'
    assert_public(text, public)
    assert list(check_secrets("fixture.js", text)) == []


@pytest.mark.parametrize("ending", ['"}', '"', ""])
@pytest.mark.parametrize(
    "shape", [r'{kty:"RSA","\u{64}":"<value>', r'`head ${{kty:"RSA","\u{64}":"<value>']
)
def test_quoted_members_in_cut_off_excerpts(ending, shape):
    value = fake()
    assert_private(shape.replace("<value>", value) + ending, value)


@pytest.mark.parametrize("width", [8, 128, 4096])
def test_long_leading_zeroes_decode_without_unbounded_integer_conversion(width):
    value = fake()
    key = "\\u{" + "0" * width + "64}"
    assert_private(f'{{kty:"RSA","{key}":"{value}"}}', value)


@pytest.mark.parametrize("key", [r"\u{110000}", r"\u{gg}", r"\u{}", r"\u{64"])
def test_invalid_or_unfinished_escape_does_not_invent_member_name(key):
    value = fake()
    assert_public(f'{{kty:"RSA","{key}":"{value}"}}', value)


@pytest.mark.parametrize("kind", [r"\\u{6f}ct", r"\\u{52}SA", r"\u{5c}u{6f}ct"])
def test_escaped_backslash_type_is_not_a_discriminator(kind):
    public = fake()
    assert_public(f'{{kty:"{kind}",d:"{public}",k:"{public}"}}', public)


def test_decoded_backslash_cannot_start_a_second_member_escape():
    public = fake()
    assert_public(r'{kty:"RSA","\u{5c}u{64}":"' + public + '"}', public)


@pytest.mark.parametrize("quoted_type", [r'"k\u{74}y":"oct"', r'kty:"\u{6f}ct"'])
def test_commented_type_cannot_reclassify_live_python_public_member(quoted_type):
    import ast

    public = fake()
    text = (
        "x={'kty':'RSA','k':"
        + repr(public)
        + ",'n':typeof / # /,"
        + quoted_type
        + ",\n denominator}"
    )
    ast.parse(text)
    assert_public(text, public)


@pytest.mark.parametrize("type_member", [r'"k\u{74}y":"oct"', r'kty:"\u{6f}ct"'])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_live_private_ancestor_role_survives_commented_child_type(type_member, layers):
    value = fake()
    child = '{kty:"RSA",k:' + repr(value) + ',n:typeof / # /,kty:"RSA",\n denominator}'
    text = "{**" + child + "," + type_member + "}"
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


def test_quoted_other_prime_names_retain_rsa_owner():
    value = fake()
    assert_private(r'{kty:"RSA","\u{6f}th":[{"\u{72}":"' + value + '"}]}', value)


@pytest.mark.parametrize("size", [128, 4096])
@pytest.mark.parametrize("ending", ["u{G}", "u{" + "0" * 128, "u{64}"])
def test_long_backslash_runs_keep_literal_names_public(size, ending):
    value, public = fake(), fake()
    key = "\\" * size + ending
    type_key = "kt" + "y"
    text = f'{{{type_key}:"RSA","{key}":"{public}",d:"{value}"}}'
    assert_private(text, value)
    assert_public(text, public)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_decoded_quote_or_backslash_cannot_terminate_member_token(quote):
    public, private = fake(), fake()
    key = r"\u{22}\u{27}\u{5c}\u{64}"
    type_key = "kt" + "y"
    text = f'{{{type_key}:"RSA",{quote}{key}{quote}:"{public}",d:"{private}"}}'
    assert_private(text, private)
    assert_public(text, public)


@pytest.mark.parametrize("key", [r'"\u0064"', r"'\x64'", r"'\144'"])
def test_existing_python_and_json_quoted_names_stay_private(key):
    import ast

    value = fake()
    text = "{'kty':'RSA'," + key + ":" + repr(value) + "}"
    ast.parse(text)
    assert_private(text, value)


# These are new adversarial inputs for the same cost model as the unchanged
# oct/URI resource fixtures. Root serializes timing/peak qualification; normal
# focused runs exclude test names containing linear or peak.
def _growing_quoted(case, n):
    if case == "malformed-backslash-run":
        key = "\\" * n + "u{G}"
    elif case == "unfinished-backslash-hex":
        key = "\\" * n + "u{" + "0" * n
    elif case == "leading-zero-member":
        key = "\\u{" + "0" * n + "64}"
    elif case == "leading-zero-type":
        return '{kty:"\\u{' + "0" * n + '52}SA",d:"value"}'
    elif case == "repeated-quoted-members":
        return '{kty:"RSA",' + ('"\\u{64}":"value",' * n) + 'n:"public"}'
    else:
        assert case == "repeated-invalid-escapes"
        key = "\\u{G}" * n
    return '{kty:"RSA","' + key + '":"value",d:"private"}'


_QUOTED_GROWING = [
    "malformed-backslash-run",
    "unfinished-backslash-hex",
    "leading-zero-member",
    "leading-zero-type",
    "repeated-quoted-members",
    "repeated-invalid-escapes",
]


@pytest.mark.parametrize("case", _QUOTED_GROWING)
def test_quoted_escape_scan_grows_linearly(case):
    def run(text):
        return redact_sensitive_content(text, max_len=20 * len(text) + 1000)

    small, large = _growing_quoted(case, 1000), _growing_quoted(case, 4000)
    assert len(large) >= 3.5 * len(small)
    # CPU time, best of interleaved samples: runner load cannot decide it (#2188).
    growth = cpu_growth(cold(run), small, large, limit=8)
    small_time, large_time, ratio = growth.small, growth.large, growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_cpu=small_time,
                large_cpu=large_time,
                ratio=ratio,
            )
        )
    )
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} time"
    assert sum(len(m.value) for m in private_members(large)) <= len(large)


@pytest.mark.parametrize("case", _QUOTED_GROWING)
def test_quoted_escape_peak_follows_existing_cost_model(case):
    small, large = _growing_quoted(case, 1000), _growing_quoted(case, 4000)

    def budget_for(found):
        return 48 * len(large) + 400 * len(found)

    # Garbage collected first and re-measured before failing (task #2188).
    growth = peak_growth(cold(private_members), small, large, limit=8, budget=budget_for)
    small_peak, large_peak, members = growth.small, growth.large, len(growth.result)
    budget = budget_for(growth.result)
    ratio = growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_peak=small_peak,
                large_peak=large_peak,
                large_members=members,
                budget=budget,
                ratio=ratio,
            )
        )
    )
    assert large_peak < budget, f"{case}: peak {large_peak} exceeds existing budget {budget}"
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} peak"


_RAW_PREFIX_ALIASES = [
    "r",
    "R",
    "b",
    "B",
    "br",
    "bR",
    "Br",
    "BR",
    "rb",
    "rB",
    "Rb",
    "RB",
    "fr",
    "fR",
    "Fr",
    "FR",
    "rf",
    "rF",
    "Rf",
    "RF",
    "tr",
    "tR",
    "Tr",
    "TR",
    "rt",
    "rT",
    "Rt",
    "RT",
]


def _prefixed_object(prefix, shape, value):
    type_key = "kt" + "y"
    if shape == "member":
        return "{" + repr(type_key) + ":'RSA'," + prefix + r"'\u{64}':" + repr(value) + "}"
    if shape == "typename":
        return "{" + prefix + r"'k\u{74}y':'RSA','d':" + repr(value) + "}"
    assert shape == "typevalue"
    return "{" + repr(type_key) + ":" + prefix + r"'\u{52}SA','d':" + repr(value) + "}"


@pytest.mark.parametrize("prefix", _RAW_PREFIX_ALIASES)
@pytest.mark.parametrize("shape", ["member", "typename", "typevalue"])
@pytest.mark.parametrize("layers", [0, 1, 3])
def test_attached_raw_or_bytes_prefix_retains_baseline_public_meaning(prefix, shape, layers):
    public, private = fake(), fake()
    text = _prefixed_object(prefix, shape, public)
    for _ in range(layers):
        text = json.dumps(text)
    type_key = "kt" + "y"
    text += "\n" + json.dumps({type_key: "RSA", "d": private})
    assert_public(text, public)
    public_lines = {i for i, line in enumerate(text.splitlines(), 1) if public in line}
    assert not any(
        f.line in public_lines and f.rule == "SECRETS-KEY" and "JWK private member" in f.message
        for f in check_secrets("fixture.py", text)
    )
    assert_private(text, private)


@pytest.mark.parametrize(
    "prefix",
    ["u", "f", "wordr", "$r", "obj.r", "rr", "rrb", "r ", r"\r", "\u200cr", "a\u0301r", "\u200dr"],
)
def test_nonprefix_or_unattached_prefix_cannot_waive_private_evidence(prefix):
    value = fake()
    # A single quote adjacent to an ordinary word is deliberately treated as
    # an apostrophe by the existing grammar; double quotes isolate this guard.
    assert_private(_prefixed_object(prefix, "member", value).replace("'", '"'), value)


@pytest.mark.parametrize("prefix", ["r", "b", "rb", "fr", "rt"])
def test_raw_prefix_only_applies_to_its_own_member(prefix):
    public, private = fake(), fake()
    text = _prefixed_object(prefix, "member", public)[:-1]
    text += r',"\u{64}":"' + private + '"}'
    assert_public(text, public)
    assert_private(text, private)


def test_unrelated_raw_literal_does_not_transfer_a_prefix_across_source_fences():
    private = fake()
    text = r'note = r"\u{64}"' + "\n" + r'{kty:"RSA","\u{64}":"' + private + '"}'
    assert_private(text, private)


def _r4_object(position, quote, form, value):
    type_key = "kt" + "y"
    continuation = {"LS": "\\\u2028", "PS": "\\\u2029", "LF": "\\\n", "CRLF": "\\\r\n"}
    if position == "member":
        spelling = r"\u{64}\p" if form == "identity" else r"\u{64}" + continuation[form]
        return "{" + type_key + ':"RSA",' + quote + spelling + quote + ":" + json.dumps(value) + "}"
    if position == "typename":
        spelling = r"k\u{74}\y" if form == "identity" else r"k\u{74}" + continuation[form] + "y"
        return "{" + quote + spelling + quote + ':"RSA",d:' + json.dumps(value) + "}"
    assert position == "typevalue"
    spelling = r"\u{52}\SA" if form == "identity" else r"\u{52}" + continuation[form] + "SA"
    return "{" + type_key + ":" + quote + spelling + quote + ",d:" + json.dumps(value) + "}"


@pytest.mark.parametrize("position", ["member", "typename", "typevalue"])
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("form", ["identity", "LS", "PS"])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_r4_js_identity_and_unicode_line_continuations(position, quote, form, layers):
    value = fake()
    text = _r4_object(position, quote, form, value)
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


def _r4_plain_braced(position, value):
    type_key = "kt" + "y"
    if position == "member":
        return "{" + type_key + r':"RSA","\u{64}":' + json.dumps(value) + "}"
    if position == "typename":
        return r'{"k\u{74}y":"RSA",d:' + json.dumps(value) + "}"
    assert position == "typevalue"
    return "{" + type_key + r':"\u{52}SA",d:' + json.dumps(value) + "}"


@pytest.mark.parametrize("position", ["member", "typename", "typevalue"])
@pytest.mark.parametrize("delimiter", ['"""', "'''"])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_r4_failed_triple_salvage_retains_braced_evidence(position, delimiter, layers):
    value = fake()
    text = delimiter + _r4_plain_braced(position, value) + delimiter
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("position", ["member", "typename", "typevalue"])
@pytest.mark.parametrize("delimiter", ['"""', "'''"])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_r4_raw_triple_literal_backslashes_remain_public(position, delimiter, layers):
    public = fake()
    body = _r4_plain_braced(position, public).replace("\\", "\\\\")
    text = "r" + delimiter + body + delimiter
    for _ in range(layers):
        text = json.dumps(text)
    assert_public(text, public)
    assert list(check_secrets("fixture.txt", text)) == []


@pytest.mark.parametrize("position", ["member", "typename", "typevalue"])
@pytest.mark.parametrize("form", ["identity", "LS", "PS"])
@pytest.mark.parametrize("delimiter", ['"""', "'''"])
@pytest.mark.parametrize("layers", [0, 1])
def test_r4_js_logical_escapes_inside_failed_triple_salvage(position, form, delimiter, layers):
    value = fake()
    text = delimiter + _r4_object(position, '"', form, value) + delimiter
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("position", ["typename", "typevalue"])
@pytest.mark.parametrize("orphan", ["'", '"'])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_r4_orphan_comment_quoted_braced_discriminator(position, orphan, layers):
    value = fake()
    text = "# orphan " + orphan + " " + _r4_plain_braced(position, value)
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("position", ["member", "typename", "typevalue"])
@pytest.mark.parametrize("orphan", ["'", '"'])
def test_r4_orphan_comment_uses_same_token_js_identity_semantics(position, orphan):
    value = fake()
    assert_private("# orphan " + orphan + " " + _r4_object(position, '"', "identity", value), value)


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("form", ["LF", "CRLF", "LS", "PS"])
@pytest.mark.parametrize("layers", [0, 1, 2, 3])
def test_r4_hint_keeps_encoded_continuation_discriminator_eligible(quote, form, layers):
    value = fake()
    text = _r4_object("typename", quote, form, value)
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


@pytest.mark.parametrize("prefix", ["r", "b", "rb", "fr", "rt"])
def test_untouched_identity_escape_keeps_existing_python_public_meaning(prefix):
    public, other = fake(), fake()
    type_key = "kt" + "y"
    text = "{" + repr(type_key) + ":'RSA'," + prefix + r"'\u{78}':" + repr(public)
    text += r",'\d':" + repr(other) + "}"
    assert_public(text, public)
    assert_public(text, other)
    assert list(check_secrets("fixture.py", text)) == []


@pytest.mark.parametrize("key", [r"k\nty", r"k\rty", r"k\r\nty", r"k\u2028ty", r"k\u2029ty"])
@pytest.mark.parametrize("layers", [0, 1, 2, 3])
def test_r4_escaped_control_key_is_not_a_type_discriminator(key, layers):
    public = fake()
    text = '{"' + key + '":"RSA",d:' + json.dumps(public) + "}"
    for _ in range(layers):
        text = json.dumps(text)
    assert_public(text, public)
    assert list(check_secrets("fixture.js", text)) == []


@pytest.mark.parametrize("prefix", ["r", "b", "rb", "fr", "rt"])
@pytest.mark.parametrize("position", ["typename", "typevalue"])
def test_recovered_orphan_raw_prefix_does_not_supply_js_type(prefix, position):
    public, private = fake(), fake()
    text = "# orphan ' " + _prefixed_object(prefix, position, public)
    text += '\n{ kty:"RSA", "\\u{64}":' + json.dumps(private) + "}"
    assert_private(text, private)
    assert_public(text, public)
    public_line = next(i for i, line in enumerate(text.splitlines(), 1) if public in line)
    assert public_line not in {f.line for f in check_secrets("fixture.py", text)}


@pytest.mark.parametrize("unknown", [r"\u{78}", r"\u{64}\pX", r"\\u{64}", r"\u{110000}"])
def test_recovery_unknown_name_keeps_base_token_extent_and_public_sibling(unknown):
    private, public, outside = fake(), fake(), fake()
    text = '# orphan \' {"k\\u{74}y":"RSA","' + unknown + '":' + json.dumps(public)
    text += ',"\\u{64}":' + json.dumps(private) + "}"
    text += '\n{kty:"RSA",n:' + json.dumps(outside) + "}"
    assert_private(text, private)
    assert_public(text, public)
    assert_public(text, outside)
    assert 2 not in {f.line for f in check_secrets("fixture.js", text)}


@pytest.mark.parametrize("uri", ["!<tag> ", "!<" + ("kt" + "y:!<") * 24 + ">", "&a !<tag> "])
def test_new_quoted_recovery_preserves_uri_frame_and_public_roles(uri):
    private, public, outside = fake(), fake(), fake()
    type_key = "kt" + "y"
    text = "# orphan ' {" + type_key + ":" + uri + "RSA,"
    text += '"\\u{64}":' + json.dumps(private) + ",n:" + json.dumps(public) + "}"
    text += "\n{" + type_key + ':"RSA",k:' + json.dumps(outside) + "}"
    assert_private(text, private)
    assert_public(text, public)
    assert_public(text, outside)
    assert 2 not in {f.line for f in check_secrets("fixture.js", text)}


_R4_GROWING = [
    "logical-identity-run",
    "logical-malformed-unicode",
    "failed-triple-salvage",
    "orphan-quoted-fields",
    "unknown-recovery-fallback",
    "encoded-hint-continuations",
    "mixed-new-type-even-literal-uri",
]


def _growing_r4(case, n):
    type_key = "kt" + "y"
    quoted_type_key = "k" + r"\u{74}" + "y"
    if case == "logical-identity-run":
        key = r"\u{64}" + r"\p" * n
        return "{" + type_key + ':"RSA","' + key + '":"public",d:"private"}'
    if case == "logical-malformed-unicode":
        key = r"\u{64}" + r"\u{" * n + r"\u{" + "f" * n + "}"
        return "{" + type_key + ':"RSA","' + key + '":"public",d:"private"}'
    if case == "failed-triple-salvage":
        key = r"\u{64}" + "\\" * n + r"\u{G}"
        return "'''{" + type_key + ':"RSA","' + key + '":"public",d:"private"}' + "'''"
    if case == "orphan-quoted-fields":
        return "# orphan ' " + (('{"' + quoted_type_key + '":"RSA",d:"private",n:"public"} ') * n)
    if case == "unknown-recovery-fallback":
        key = r"\u{78}" + (r"\"k\u{74}X" * n)
        return '# orphan \' {"k\\u{74}y":"RSA","' + key + '":"public",d:"private"}'
    if case == "mixed-new-type-even-literal-uri":
        frame = (
            '{"' + quoted_type_key + '":"RSA","\\\\u{64}":"public","\\u{64}":"private",n:"public"} '
        )
        uri = "{" + type_key + ':!<tag> RSA,k:"public"} '
        return "# orphan ' " + (frame + uri) * n
    assert case == "encoded-hint-continuations"
    return json.dumps('{"k\\u{74}' + "\\\n" * n + 'y":"RSA",d:"private"}')


@pytest.mark.parametrize("case", _R4_GROWING)
def test_r4_logical_escape_scan_grows_linearly(case):
    def run(text):
        return redact_sensitive_content(text, max_len=20 * len(text) + 1000)

    small, large = _growing_r4(case, 1000), _growing_r4(case, 4000)
    assert len(large) >= 3.5 * len(small)
    # CPU time, best of interleaved samples: runner load cannot decide it (#2188).
    growth = cpu_growth(cold(run), small, large, limit=8)
    small_time, large_time, ratio = growth.small, growth.large, growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_cpu=small_time,
                large_cpu=large_time,
                ratio=ratio,
            )
        )
    )
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} time"
    assert sum(len(m.value) for m in private_members(large)) <= len(large)


@pytest.mark.parametrize("case", _R4_GROWING)
def test_r4_logical_escape_peak_follows_existing_cost_model(case):
    small, large = _growing_r4(case, 1000), _growing_r4(case, 4000)

    def budget_for(found):
        return 48 * len(large) + 400 * len(found)

    # Garbage collected first and re-measured before failing (task #2188).
    growth = peak_growth(cold(private_members), small, large, limit=8, budget=budget_for)
    small_peak, large_peak, members = growth.small, growth.large, len(growth.result)
    budget = budget_for(growth.result)
    ratio = growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_peak=small_peak,
                large_peak=large_peak,
                large_members=members,
                budget=budget,
                ratio=ratio,
            )
        )
    )
    assert large_peak < budget, f"{case}: peak {large_peak} exceeds existing budget {budget}"
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} peak"


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("position", ["typename", "typevalue"])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_new_type_even_literal_public_boundary_with_private_sibling(quote, position, layers):
    public, private, outside = fake(), fake(), fake()
    discriminator = (
        quote + r"k\u{74}y" + quote + ':"RSA"'
        if position == "typename"
        else "kty:" + quote + r"\u{52}SA" + quote
    )
    text = "# orphan ' {" + discriminator + "," + quote + r"\\u{64}" + quote
    text += ":" + json.dumps(public) + ',"\\u{64}":' + json.dumps(private) + "}"
    for _ in range(layers):
        text = json.dumps(text)
    text += '\n{kty:"RSA",n:' + json.dumps(outside) + "}"
    assert_private(text, private)
    assert_public(text, outside)
    assert 2 not in {f.line for f in check_secrets("fixture.js", text)}
    if not layers:
        assert_public(text, public)
    else:
        # Same-input plain-type baseline already protects the entire encoded
        # outer string; no inner public extent is promised inside that fence.
        assert_private(text, public)


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("legacy_kind", ["RSA", "EC", "OKP"])
def test_even_literal_recovery_keeps_legacy_private_role(quote, legacy_kind):
    historical, private = fake(), fake()
    text = "# orphan ' {kty:" + json.dumps(legacy_kind) + ',"k\\u{74}y":"RSA",'
    text += quote + r"\\u{64}" + quote + ":" + json.dumps(historical)
    text += ',"\\u{64}":' + json.dumps(private) + "}"
    assert_private(text, historical)
    assert_private(text, private)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_even_literal_suppression_preserves_serialized_private_descendant(quote):
    private, sibling, outside = fake(), fake(), fake()
    type_key = "kt" + "y"
    inner = json.dumps({type_key: "RSA", "d": private})
    text = '# orphan \' {"k\\u{74}y":"RSA",' + quote + r"\\u{64}" + quote
    text += ":" + json.dumps(inner) + ',"\\u{64}":' + json.dumps(sibling) + "}"
    text += '\n{kty:"RSA",n:' + json.dumps(outside) + "}"
    assert_private(text, private)
    assert_private(text, sibling)
    assert_public(text, outside)


@pytest.mark.parametrize("suffix", ["", " + expression", "(", " + {'d':'later'}"])
def test_new_type_does_not_suppress_unproven_even_name_value_extent(suffix):
    value = fake()
    text = '# orphan \' {"k\\u{74}y":"RSA","\\\\u{64}":' + json.dumps(value) + suffix
    # A cutoff or expression is not the complete literal+tail proof required
    # to withhold BASE's coarse private candidate.
    assert_private(text, value)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_new_type_even_name_unfinished_escaped_closer_stays_protected(quote):
    value = fake()
    text = '# orphan \' {"k\\u{74}y":"RSA","\\\\u{64}":' + quote + value + "\\" + quote + "}"
    assert_private(text, value)


@pytest.mark.parametrize("prefix", ["f", "F", "t", "T"])
@pytest.mark.parametrize("position", ["member", "typename", "typevalue"])
@pytest.mark.parametrize("delimiter", ['"""', "'''"])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_r5_nonraw_fallback_preserves_genuine_braced_evidence(prefix, position, delimiter, layers):
    value = fake()
    text = prefix + delimiter + _r4_plain_braced(position, value) + delimiter
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, value)


def _r5_discriminator(position):
    if position == "typename":
        return '"' + "k" + r"\u{74}" + 'y":"RSA"'
    assert position == "typevalue"
    return "kt" + 'y:"' + r"\u{52}SA" + '"'


_R5_LITERAL_NAMES = [
    (2, "u{64}p"),
    (2, "u{64}q"),
    (3, "u{64}"),
    (3, "u{64}p"),
    (3, "u{64}q"),
    (3, "u{71}i"),
    (3, "u{6f}th"),
    (3, "u{70}"),
    (4, "u{64}p"),
    (4, "u{64}q"),
]


@pytest.mark.parametrize("slashes,spelling", _R5_LITERAL_NAMES)
@pytest.mark.parametrize("position", ["typename", "typevalue"])
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("sibling", [False, True])
def test_r5_new_type_cannot_publish_literal_suffix_member(
    slashes, spelling, position, quote, sibling
):
    public, private, outside = fake(), fake(), fake()
    name = "\\" * slashes + spelling
    text = "# orphan ' {" + _r5_discriminator(position) + "," + quote + name + quote
    text += ":" + json.dumps(public)
    if sibling:
        text += ',"\\u{64}":' + json.dumps(private)
    text += "}\n{" + "kt" + 'y:"RSA",n:' + json.dumps(outside) + "}"
    assert_public(text, public)
    assert_public(text, outside)
    if sibling:
        assert_private(text, private)
    else:
        assert list(check_secrets("fixture.js", text)) == []
    assert 2 not in {f.line for f in check_secrets("fixture.js", text)}


@pytest.mark.parametrize("name", [r"\u{5c}\u{64}p", r"\u{5c}dp", r"\x5c\u{64}p", r"\u005c\u{64}p"])
@pytest.mark.parametrize("position", ["typename", "typevalue"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_r5_produced_backslash_name_retains_public_value_and_private_sibling(name, position, quote):
    public, private, outside = fake(), fake(), fake()
    text = "# orphan ' {" + _r5_discriminator(position) + "," + quote + name + quote
    text += ":" + json.dumps(public) + ',"\\u{64}":' + json.dumps(private) + "}"
    text += "\n{" + "kt" + 'y:"RSA",n:' + json.dumps(outside) + "}"
    assert_public(text, public)
    assert_public(text, outside)
    assert_private(text, private)
    assert 2 not in {f.line for f in check_secrets("fixture.js", text)}


@pytest.mark.parametrize(
    "name", [r"foo\u{64}p", r"\u{78}dp", r"long\x5c\u{64}p", r"\u{5c}\u{64}\p"]
)
@pytest.mark.parametrize("quote", ['"', "'"])
def test_r5_general_closed_nonprivate_name_does_not_export_a_suffix(name, quote):
    public, private = fake(), fake()
    text = "# orphan ' {" + _r5_discriminator("typename") + "," + quote + name + quote
    text += ":" + json.dumps(public) + ',"\\u{64}":' + json.dumps(private) + "}"
    assert_public(text, public)
    assert_private(text, private)


@pytest.mark.parametrize("name", [r"\u0064", r"\144", r"\N{LATIN SMALL LETTER D}", r"\u{64}\p"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_r5_name_proof_must_not_hide_js_or_python_private_interpretation(name, quote):
    private = fake()
    text = "# orphan ' {" + _r5_discriminator("typename") + "," + quote + name + quote
    text += ":" + json.dumps(private) + "}"
    assert_private(text, private)


@pytest.mark.parametrize("prefix", ["", "r", "b", "u", "f", "fr", "t", "rt"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_r5_closed_literal_value_prefix_keeps_private_descendant_and_outside_public(prefix, quote):
    public, private, outside = fake(), fake(), fake()
    literal = prefix + quote + public + quote
    text = "# orphan ' {" + _r5_discriminator("typename") + ',"\\\\u{64}p":' + literal
    text += ',"\\u{64}":' + json.dumps(private) + "}"
    text += "\n{" + "kt" + 'y:"RSA",n:' + json.dumps(outside) + "}"
    assert_public(text, public)
    assert_public(text, outside)
    assert_private(text, private)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_r5_closed_public_field_name_keeps_private_construction_in_its_name(quote):
    private, public = fake(), fake()
    inner = json.dumps({"kt" + "y": "RSA", "d": private})
    # Opposite delimiter names exercise both proof-index states. The named
    # field's value is public; the serialized construction in the name is not.
    name = json.dumps(inner) if quote == '"' else repr(inner)
    text = (
        "# orphan ' {" + _r5_discriminator("typename") + "," + name + ":" + json.dumps(public) + "}"
    )
    assert_private(text, private)
    if quote == '"':
        # SAME-input d13/a9ad/ac9/643/2de all cover this value together with
        # the encoded private name. This is retained coarse baseline behavior.
        assert_private(text, public)
    else:
        assert_public(text, public)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_r5_new_proof_cannot_mark_nongenuine_identity_private_name_safe(quote):
    from unified_agent.telemetry import jwk

    # A private logical name cannot acquire a safe-public withholding proof.
    # R7 also checks the complete task2168 privacy pipeline below.
    raw = quote + r"\d" + quote
    assert not jwk._recovery_nonprivate_name(raw, 0, 0, len(raw) - 1)


@pytest.mark.parametrize(
    "raw,proven",
    [
        (r'"\d"', False),
        (r'"\N{LATIN SMALL LETTER D}"', False),
        (r'b"\x64"', False),
        (r'r"\u{64}p"', True),
        (r'b"\u{64}p"', True),
        (r'"\u{5c}dp"', True),
        (r'"long\u{64}p"', True),
        ('f"{dynamic}"', False),
        ('fr"{dynamic}"', False),
    ],
)
def test_r5_constant_name_proof_checks_source_prefix_and_both_logical_views(raw, proven):
    from unified_agent.telemetry import jwk

    quote = next(i for i, c in enumerate(raw) if c in "\"'")
    assert jwk._recovery_nonprivate_name(raw, 0, quote, len(raw) - 1) is proven


@pytest.mark.parametrize("prefix", ["wordr", "$r", "obj.r", "\\r", "x\u200dr"])
def test_r5_proof_cannot_cross_an_identifier_or_source_prefix_boundary(prefix):
    from unified_agent.telemetry import jwk

    raw = prefix + r'"\u{64}p"'
    start = len(prefix) - 1
    assert not jwk._recovery_nonprivate_name(raw, start, len(prefix), len(raw) - 1)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_r5_proof_index_never_reopens_escaped_quotes_or_triple_delimiters(quote):
    from unified_agent.telemetry import jwk

    escaped = "\\" + quote + "unknown" + "\\" + quote + ':"value"'
    assert not jwk._recovery_field_proofs(escaped)
    triple = quote * 3 + "private-looking" + quote * 3 + ':"value"'
    assert not jwk._recovery_field_proofs(triple)


@pytest.mark.parametrize("prefix", ["f", "fr", "t", "rt"])
def test_r5_public_field_formatted_value_keeps_independent_private_descendant(prefix):
    private, outside = fake(), fake()
    inner = "{'" + "kt" + "y':'RSA','d':" + repr(private) + "}"
    literal = prefix + '"""{' + inner + '}"""'
    text = "# orphan ' {" + _r5_discriminator("typename") + ',"\\\\u{64}p":' + literal + "}"
    text += "\n{" + "kt" + 'y:"RSA",n:' + json.dumps(outside) + "}"
    assert_private(text, private)
    assert_public(text, outside)


@pytest.mark.parametrize("legacy_kind", ["RSA", "EC", "OKP"])
@pytest.mark.parametrize("name", [r"\\u{64}p", r"\u{5c}dp", r"\\\u{64}p"])
def test_r5_new_name_proof_cannot_remove_independent_legacy_private_role(legacy_kind, name):
    private = fake()
    text = "# orphan ' {" + "kt" + "y:" + json.dumps(legacy_kind)
    text += "," + _r5_discriminator("typename") + ',"' + name + '":' + json.dumps(private) + "}"
    # RSA's p is a legacy private role; EC/OKP do not confer that role.
    if legacy_kind == "RSA":
        assert_private(text, private)
    else:
        assert_public(text, private)


_R6_GROWING = [
    "formatted-fallback-salvage",
    "long-escaped-quote-name",
    "mixed-proof-publication",
    "compact-public-fields",
]


def _growing_r6(case, n):
    type_key = "kt" + "y"
    escaped_type = "k" + r"\u{74}" + "y"
    if case == "formatted-fallback-salvage":
        name = r"\u{64}" + "\\" * n + r"\u{G}"
        return "f'''{" + type_key + ':"RSA","' + name + '":"public",d:"private"}' + "'''"
    if case == "long-escaped-quote-name":
        name = r"\u{78}" + (r"\"\'\\" * n) + r"\u{64}p"
        return "# orphan ' {\"" + escaped_type + '":"RSA","' + name + '":"public",d:"private"}'
    if case == "compact-public-fields":
        return "# orphan ' {\"" + escaped_type + '":"RSA",' + ('"x":"",' * n) + 'n:"public"}'
    assert case == "mixed-proof-publication"
    child = "{'" + type_key + "':'RSA','d':'private'}"
    value = 'f"""{' + child + '}"""'
    frame = '{"' + escaped_type + '":"RSA","\\u{5c}dp":' + value
    frame += ',"\\u{64}":"private",n:"public"} '
    uri = "{" + type_key + ':!<tag> RSA,k:"public"} '
    return "# orphan ' " + (frame + uri) * n


@pytest.mark.parametrize("case", _R6_GROWING[:3])
def test_r6_growing_fixture_has_actual_private_evidence(case):
    assert_private(_growing_r6(case, 3), "private")


def test_r6_compact_public_fixture_has_no_private_member_allowance():
    public = fake()
    text = _growing_r6("compact-public-fields", 8).replace('""', json.dumps(public))
    assert_public(text, public)
    assert private_members(text) == []
    assert list(check_secrets("fixture.js", text)) == []


@pytest.mark.parametrize("case", _R6_GROWING)
def test_r6_proof_index_scan_grows_linearly(case):
    def run(text):
        return redact_sensitive_content(text, max_len=20 * len(text) + 1000)

    small, large = _growing_r6(case, 1000), _growing_r6(case, 4000)
    assert len(large) >= 3.5 * len(small)
    # CPU time, best of interleaved samples: runner load cannot decide it (#2188).
    growth = cpu_growth(cold(run), small, large, limit=8)
    small_time, large_time, ratio = growth.small, growth.large, growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_cpu=small_time,
                large_cpu=large_time,
                ratio=ratio,
            )
        )
    )
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} time"
    assert sum(len(m.value) for m in private_members(large)) <= len(large)


# Peak-memory bytes per input character. 48 is the general cost model. These
# dense or deeply nested shapes cost more per character on the PR #1261 base
# (ac9cc562) as well: measured base -> this branch at 4000 units,
# compact-public-fields n/a (base skips the shape) -> 92, identity-unpack-roles
# 86 -> 190, identity-continuation-name 50 -> 57, mixed-proof-identity 36 -> 53.
# All stay flat per character out to 16000-64000 units; the x4 ratio checks
# guard against superlinear growth.
_PEAK_CHAR_BUDGET = {
    "compact-public-fields": 128,
    "identity-unpack-roles": 256,
    "identity-continuation-name": 80,
    "mixed-proof-identity": 72,
}


def _peak_char_budget(case):
    return _PEAK_CHAR_BUDGET.get(case, 48)


@pytest.mark.parametrize("case", _R6_GROWING)
def test_r6_proof_index_peak_follows_existing_cost_model(case):
    small, large = _growing_r6(case, 1000), _growing_r6(case, 4000)

    def budget_for(found):
        return _peak_char_budget(case) * len(large) + 400 * len(found)

    # Garbage collected first and re-measured before failing (task #2188).
    growth = peak_growth(cold(private_members), small, large, limit=8, budget=budget_for)
    small_peak, large_peak, members = growth.small, growth.large, len(growth.result)
    budget = budget_for(growth.result)
    ratio = growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_peak=small_peak,
                large_peak=large_peak,
                large_members=members,
                budget=budget,
                ratio=ratio,
            )
        )
    )
    assert large_peak < budget, f"{case}: peak {large_peak} exceeds existing budget {budget}"
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} peak"


def _r7_type(shape="name", kind="RSA"):
    if shape == "value":
        value = {"RSA": r"R\u{53}A", "EC": r"\u{45}C", "oct": r"o\u{63}t"}[kind]
        return '"kt' + 'y":"' + value + '"'
    key = "k" + r"\u{74}" + "y"
    return (
        (key if shape == "bare" else json.dumps(key).replace("\\\\", "\\")) + ":" + json.dumps(kind)
    )


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 2])
@pytest.mark.parametrize("key", [r"\d", r"\u{64}"])
def test_r7_task2168_private_identity_and_braced_control(quote, orphan, layers, key):
    private = fake()
    text = "{" + _r7_type() + "," + quote + key + quote + ":" + json.dumps(private) + "}"
    text = ("# orphan ' " if orphan else "") + text
    for _ in range(layers):
        text = json.dumps(text)
    assert_private(text, private)


@pytest.mark.parametrize("shape", ["name", "bare", "value"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize(
    "kind,key,private_role",
    [
        ("RSA", r"\p", True),
        ("RSA", r"\q", True),
        ("RSA", r"\d\p", True),
        ("RSA", r"\d\q", True),
        ("RSA", r"\q\i", True),
        ("RSA", r"\d\x70", True),
        ("oct", r"\k", True),
        ("EC", r"\p", False),
        ("RSA", r"\k", False),
        ("RSA", r"\\d", False),
        ("RSA", r"\x5cd", False),
        ("RSA", r"\u005cd", False),
        ("RSA", r"\description", False),
    ],
)
def test_r7_private_roles_discriminator_order_and_single_decode(
    shape, reverse, orphan, kind, key, private_role
):
    value, outside = fake(), fake()
    member = json.dumps(key).replace("\\\\", "\\") + ":" + json.dumps(value)
    parts = [_r7_type(shape, kind), member]
    if reverse:
        parts.reverse()
    text = ("# orphan ' " if orphan else "") + "{" + ",".join(parts) + "}"
    text += "\n{kt" + 'y:"RSA",n:' + json.dumps(outside) + "}"
    (assert_private if private_role else assert_public)(text, value)
    assert_public(text, outside)


@pytest.mark.parametrize("line", ["\n", "\r\n", "\u2028", "\u2029"])
@pytest.mark.parametrize("orphan", [False, True])
def test_r7_private_identity_with_physical_continuations(line, orphan):
    private = fake()
    text = ("# orphan ' " if orphan else "") + "{" + _r7_type()
    text += ',"\\d\\' + line + 'p":' + json.dumps(private) + "}"
    assert_private(text, private)


_LINES = ["\n", "\r\n", "\u2028", "\u2029"]
_HASH_PREFIXES = ["# orphan ' ", '# orphan " ', "# note ", "x = 1 # orphan ' "]


@pytest.mark.parametrize("line", _LINES)
@pytest.mark.parametrize("prefix", _HASH_PREFIXES)
@pytest.mark.parametrize("key", ["\\d\\{}p", "d\\{}p", "\\d\\{}", "\\{}\\d"])
def test_task2169_hash_comment_keeps_continued_private_key(line, prefix, key):
    # A Python comment ends at the break; the JS string continues (task #2169).
    private = fake()
    text = prefix + "{" + _r7_type() + ',"' + key.format(line) + '":' + json.dumps(private) + "}"
    assert_private(text, private)


@pytest.mark.parametrize("line", _LINES)
@pytest.mark.parametrize("prefix", _HASH_PREFIXES)
@pytest.mark.parametrize(
    "type_key,key", [("k\\{}ty", "dp"), ("k\\{}\\u{{74}}y", "\\d\\p"), ("k\\{}ty", "d\\{}p")]
)
def test_task2169_hash_comment_continued_type_key(line, prefix, type_key, key):
    private = fake()
    member = '"' + key.format(line) + '":' + json.dumps(private)
    text = prefix + '{"' + type_key.format(line) + '":"RSA",' + member + "}"
    assert_private(text, private)


@pytest.mark.parametrize("line", _LINES)
@pytest.mark.parametrize("prefix", _HASH_PREFIXES)
def test_task2169_hash_comment_continuation_keeps_public_members(line, prefix):
    private, modulus, exponent = fake(), fake(), fake()
    text = prefix + "{" + _r7_type() + ',"n\\' + line + '":' + json.dumps(modulus)
    text += ',"\\e":' + json.dumps(exponent) + ',"\\d\\' + line + 'p":' + json.dumps(private)
    text += "}"
    assert_private(text, private)
    assert_public(text, modulus)
    assert_public(text, exponent)


@pytest.mark.parametrize("line", _LINES)
def test_task2169_trailing_comment_backslash_does_not_retype_live_code(line):
    # The continued comment view only adds candidates; live public members stay.
    modulus, private = fake(), fake()
    text = "# path C:\\" + line + '{"kt' + 'y":"RSA","n":' + json.dumps(modulus)
    text += ',"d":' + json.dumps(private) + "}"
    assert_private(text, private)
    assert_public(text, modulus)


@pytest.mark.parametrize("line", _LINES)
@pytest.mark.parametrize("quote", ["'", '"'])
def test_task2169_slash_comment_orphan_continued_key(line, quote):
    private = fake()
    text = "// orphan " + quote + ' {"kt' + 'y":"RSA","d\\' + line + 'p":' + json.dumps(private)
    assert_private(text + "}", private)


@pytest.mark.parametrize("line", _LINES)
def test_task2169_slash_comment_orphan_braced_identity_gap(line):
    # Fixed by #2224: a // body joins the isolated comment view.
    private = fake()
    text = "// orphan ' {" + _r7_type() + ',"\\d\\' + line + 'p":' + json.dumps(private) + "}"
    assert_private(text, private)


_ALL_BREAKS = ["\n", "\r\n", "\r", " ", " ", "\x85"]


@pytest.mark.parametrize("line", ["\n", "\r\n", "\r", " ", " "])
def test_task2169_plain_comment_continued_type_and_identity_private(line):
    # A plain comment whose type key continues on the next physical line is
    # not Python source: a Python comment cannot continue. The continued body
    # is read with JS identity escapes, so "\d\p" is dp (independent review).
    private = fake()
    text = '# note {"k\\' + line + 'ty":"RSA","\\d\\p":' + json.dumps(private) + "}"
    assert_private(text, private)


@pytest.mark.parametrize("prefix", ["# note ", "# orphan ' "])
def test_task2169_nel_continued_yaml_key_private(prefix):
    # NEL ends a comment and is a YAML line break, so an escaped NEL continues
    # the YAML double-quoted key exactly as LF does (independent review).
    import yaml

    private = fake()
    body = '{"kt' + 'y":"RSA",? "d\\\x85p":' + json.dumps(private) + "}"
    assert yaml.safe_load(body) == {"kt" + "y": "RSA", "d" + "p": private}
    assert_private(prefix + body, private)


@pytest.mark.parametrize("line", _ALL_BREAKS)
@pytest.mark.parametrize("prefix", ["# note ", "# orphan ' ", "x = 1 # note "])
def test_task2169_every_break_continues_a_comment_body(line, prefix):
    # Each break that ends a comment also continues its escaped body.
    private = fake()
    text = prefix + '{"kty":"RSA",? "d\\' + line + 'p":' + json.dumps(private) + "}"
    assert_private(text, private)


@pytest.mark.parametrize("line", _ALL_BREAKS)
def test_task2169_plain_continued_comment_keeps_public_members(line):
    private, modulus = fake(), fake()
    text = '# note {"k\\' + line + 'ty":"RSA","n":' + json.dumps(modulus)
    text += ',"\\d\\p":' + json.dumps(private) + "}"
    assert_private(text, private)
    assert_public(text, modulus)


_EDGE_KEYS = {
    "key-leading": ("\\{}kty", "RSA"),
    "key-trailing": ("kty\\{}", "RSA"),
    "type-leading": ("k\\{}ty", "\\{}RSA"),
    "type-trailing": ("k\\{}ty", "RSA\\{}"),
    "both-edges": ("\\{0}k\\{0}ty\\{0}", "\\{0}R\\{0}SA\\{0}"),
}


@pytest.mark.parametrize("line", ["\n", "\r\n", "\r", " ", " "])
@pytest.mark.parametrize("edge", list(_EDGE_KEYS))
def test_task2169_continuations_at_key_and_type_edges(line, edge):
    # A continuation may sit at any position of the quoted key or type value,
    # including its first and last character (independent review, round 2).
    private = fake()
    key, kind = (part.format(line) for part in _EDGE_KEYS[edge])
    text = '# note {"' + key + '":"' + kind + '","\\d\\p":' + json.dumps(private) + "}"
    assert_private(text, private)


@pytest.mark.parametrize("line", _ALL_BREAKS)
@pytest.mark.parametrize("prefix", ["# orphan ' ", "# note "])
@pytest.mark.parametrize("key", ["dp\\{}", "\\{}dp", "\\{0}d\\{0}p\\{0}"])
def test_task2169_yaml_private_key_edge_continuations(line, prefix, key):
    import yaml

    private = fake()
    body = '{"kt' + 'y":"RSA",? "' + key.format(line) + '":' + json.dumps(private) + "}"
    if key.startswith("dp"):
        # A YAML double-quoted key folds an escaped break at its end, too.
        assert yaml.safe_load(body) == {"kt" + "y": "RSA", "d" + "p": private}
    assert_private(prefix + body, private)


@pytest.mark.parametrize("line", ["\n", " "])
def test_task2169_edge_continuations_keep_public_members(line):
    private, modulus = fake(), fake()
    text = '# note {"kty\\' + line + '":"\\' + line + 'RSA","n\\' + line + '":'
    text += json.dumps(modulus) + ',"\\d\\p":' + json.dumps(private) + "}"
    assert_private(text, private)
    assert_public(text, modulus)


def _cold(text):
    # Measure the scanner without the last-argument YAML lexer cache.
    from unified_agent.telemetry import yaml_jwk

    yaml_jwk._LEX_CACHE = None
    return private_members(text)


@pytest.mark.parametrize(
    "line",
    [
        "# " + "'" * 32 + ' {"k\\u{74}y":"RSA","\\d\\\n',
        "# " + "'\\" * 32 + ' {"k\\u{74}y":"RSA","\\d\\\n',
        "# " + "\"'" * 32 + ' {"k\\u{74}y":"RSA","\\d\\\n',
        '# note {"k\\\nty":"RSA","\\d\\\n',
        '# note {"kty":"RSA",? "d\\\x85',
    ],
    ids=["orphan", "orphan-slash", "orphan-pair", "plain", "plain-nel"],
)
def test_task2169_continued_comment_scan_is_linear(line):
    # Every comment ends in a continuation: one long continued comment body.
    def build(size):
        return line * (size // len(line))

    small, large = build(20_000), build(80_000)
    growth = cpu_growth(_cold, small, large, limit=8)
    assert growth.ratio < 8, f"x4 input took x{growth.ratio:.2f} time"
    peak = peak_growth(_cold, small, large, limit=8)
    assert peak.ratio < 8, f"x4 input took x{peak.ratio:.2f} peak"


_EMPTY_CONTINUED_LINES = {
    "key-lf": ("\\\n", '# note {"kty":"RSA","{}kid":'),
    "key-alternating": ("\\\n\\\r\n\\\r\\ \\ ", '# note {"kty":"RSA","{}kid":'),
    "type-value": ("\\\n", '# note {"kty":"{}XYZ","kid":'),
}


@pytest.mark.parametrize("shape", list(_EMPTY_CONTINUED_LINES))
def test_task2169_empty_continued_lines_before_public_field_are_linear(shape):
    # A long run of empty continued lines before an unrecognized name must be
    # read once, not once per remaining line (independent review, round 3).
    unit, template = _EMPTY_CONTINUED_LINES[shape]
    public = fake()

    def build(count):
        return template.replace("{}", unit * (count // unit.count("\\"))) + json.dumps(public) + "}"

    small, large = build(2_000), build(8_000)
    assert_public(large, public)
    growth = cpu_growth(_cold, small, large, limit=8)
    assert growth.ratio < 8, f"x4 input took x{growth.ratio:.2f} time"
    peak = peak_growth(_cold, small, large, limit=8)
    assert peak.ratio < 8, f"x4 input took x{peak.ratio:.2f} peak"


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "r",
        "R",
        "b",
        "B",
        "br",
        "rb",
        "Br",
        "rB",
        "u",
        "U",
        "f",
        "F",
        "fr",
        "rf",
        "t",
        "T",
        "tr",
        "rt",
    ],
)
@pytest.mark.parametrize("quote", ['"', "'"])
def test_r7_competing_python_prefixes_keep_public_identity_name(prefix, quote):
    public = fake()
    text = (
        "{'kt"
        + "y':'RSA',r'\\u{78}':'note',"
        + prefix
        + quote
        + r"\d"
        + quote
        + ":"
        + repr(public)
        + "}"
    )
    assert_public(text, public)


@pytest.mark.parametrize("prefix", ["r", "R", "b", "br", "rb", "u", "f", "t"])
def test_r7_attached_python_name_prefix_not_retyped_by_new_private_path(prefix):
    public = fake()
    text = "{" + _r7_type() + "," + prefix + '"\\d":' + json.dumps(public) + "}"
    assert_public(text, public)


@pytest.mark.parametrize("shape", ["raw-name", "raw-value", "unrelated", "comment"])
def test_r7_unrelated_raw_or_comment_evidence_cannot_grant_private_role(shape):
    public = fake()
    if shape == "raw-name":
        text = '{r"k\\u{74}y":"RSA","\\d":' + json.dumps(public) + "}"
    elif shape == "raw-value":
        text = '{"kt' + 'y":r"R\\u{53}A","\\d":' + json.dumps(public) + "}"
    elif shape == "unrelated":
        text = '{"kt' + 'y":"RSA","x\\u{78}":"note","\\d":' + json.dumps(public) + "}"
    else:
        text = '{"kt' + 'y":"RSA",# "k\\u{74}y":"oct"\n"\\k":' + json.dumps(public) + "}"
    assert_public(text, public)


@pytest.mark.parametrize("suffix", ["", ",", " + expression", " + expression}"])
@pytest.mark.parametrize("orphan", [False, True])
def test_r7_identity_private_cutoffs_and_expression_values(suffix, orphan):
    private = fake()
    text = (
        ("# orphan ' " if orphan else "")
        + "{"
        + _r7_type()
        + ',"\\d":'
        + json.dumps(private)
        + suffix
    )
    assert_private(text, private)


@pytest.mark.parametrize("source", ["child", "ancestor"])
@pytest.mark.parametrize("orphan", [False, True])
def test_r7_independent_private_type_roles_survive_literal_unpacks(source, orphan):
    private = fake()
    member = '"\\d":' + json.dumps(private)
    text = (
        "{" + member + ",**{" + _r7_type() + "}}"
        if source == "child"
        else "{" + _r7_type() + ",**{" + member + "}}"
    )
    text = ("# orphan ' " if orphan else "") + text
    assert_private(text, private)


_R7_GROWING = [
    "identity-field-fanout",
    "identity-continuation-name",
    "identity-unpack-roles",
    "mixed-proof-identity",
]


def _growing_r7(case, n):
    discriminator = '"k' + r"\u{74}" + 'y":"RSA"'
    member = '"\\d\\p":"private"'
    if case == "identity-field-fanout":
        return "{" + discriminator + "," + ",".join([member] * n) + "}"
    if case == "identity-continuation-name":
        return "{" + discriminator + ',"\\d' + ("\\\u2028" * n) + 'p":"private"}'
    if case == "identity-unpack-roles":
        return "{" + discriminator + ",**" + "{**" * (n - 1) + "{" + member + "}" * (n + 1)
    assert case == "mixed-proof-identity"
    public = '"\\\\u{64}p":"public"'
    return "# orphan ' {" + discriminator + "," + ",".join([public] * n) + "," + member + "}"


@pytest.mark.parametrize("case", _R7_GROWING)
def test_r7_resource_fixture_private_wrong_role_and_public_neighbor(case):
    private, public, outside = fake(), fake(), fake()
    text = (
        _growing_r7(case, 3)
        .replace('"private"', json.dumps(private))
        .replace('"public"', json.dumps(public))
    )
    text += "\n{kt" + 'y:"RSA",n:' + json.dumps(outside) + "}"
    assert_private(text, private)
    assert_public(text, outside)
    if case == "mixed-proof-identity":
        assert_public(text, public)
    wrong_role = text.replace(':"RSA"', ':"EC"', 1)
    assert_public(wrong_role, private)


@pytest.mark.parametrize("case", _R7_GROWING)
def test_r7_supplement_scan_grows_linearly(case):
    def run(text):
        return redact_sensitive_content(text, max_len=20 * len(text) + 1000)

    small, large = _growing_r7(case, 1000), _growing_r7(case, 4000)
    assert len(large) >= 3.5 * len(small)
    # CPU time, best of interleaved samples: runner load cannot decide it (#2188).
    growth = cpu_growth(cold(run), small, large, limit=8)
    small_time, large_time, ratio = growth.small, growth.large, growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_cpu=small_time,
                large_cpu=large_time,
                ratio=ratio,
            )
        )
    )
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} time"
    assert sum(len(m.value) for m in private_members(large)) <= len(large)


@pytest.mark.parametrize("case", _R7_GROWING)
def test_r7_supplement_peak_follows_existing_cost_model(case):
    small, large = _growing_r7(case, 1000), _growing_r7(case, 4000)

    def budget_for(found):
        return _peak_char_budget(case) * len(large) + 400 * len(found)

    # Garbage collected first and re-measured before failing (task #2188).
    growth = peak_growth(cold(private_members), small, large, limit=8, budget=budget_for)
    small_peak, large_peak, members = growth.small, growth.large, len(growth.result)
    budget = budget_for(growth.result)
    ratio = growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_peak=small_peak,
                large_peak=large_peak,
                large_members=members,
                budget=budget,
                ratio=ratio,
            )
        )
    )
    assert large_peak < budget, f"{case}: peak {large_peak} exceeds existing budget {budget}"
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} peak"


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("layers", [0, 1, 2])
@pytest.mark.parametrize("mutation", ['["kty"]', '["k"+"ty"]', "spread", "plain"])
@pytest.mark.parametrize("final", ["RSA", "EC"])
def test_a2_source_proved_last_discriminator(quote, orphan, layers, mutation, final):
    value, outside = fake(), fake()
    first = "EC" if final == "RSA" else "RSA"
    initial = quote + r"k\u{74}y" + quote + ":" + json.dumps(first)
    write = (
        '...{"kty":' + json.dumps(final) + ',"note":{}}'
        if mutation == "spread"
        else ('"kty"' if mutation == "plain" else mutation) + ":" + json.dumps(final)
    )
    text = ("# orphan ' " if orphan else "") + "{" + initial + "," + write
    text += "," + quote + r"\p" + quote + ":" + json.dumps(value) + "}"
    for _ in range(layers):
        text = json.dumps(text)
    text += '\n{"kty":"RSA","n":' + json.dumps(outside) + "}"
    (assert_private if final == "RSA" else assert_public)(text, value)
    assert_public(text, outside)


@pytest.mark.parametrize(
    "mutation",
    [
        '[slot]:"EC"',
        "kty",
        'get kty(){return "EC"}',
        "set kty(value){}",
        'kty(){return "EC"}',
        "...source",
    ],
)
@pytest.mark.parametrize("position", ["before", "after", "restored"])
def test_a2_unknown_mutation_does_not_keep_stale_supplement(mutation, position):
    from unified_agent.telemetry import jwk

    genuine = r'"k\u{74}y":"RSA"'
    fields = [mutation, genuine] if position == "before" else [genuine, mutation]
    if position == "restored":
        fields.append('"kty":"RSA"')
    text = "{" + ",".join(fields) + "}"
    # This tests source evidence, never the runtime value of slot/source/kty.
    masks = jwk._javascript_private_effects(text, {})
    assert masks.get(0, 0) == (0 if position == "after" else 1)


@pytest.mark.parametrize("tail", [' + ""', ".member", "[0]", "()", ' ? "RSA" : "EC"'])
def test_a2_type_requires_complete_own_expression(tail):
    from unified_agent.telemetry import jwk

    text = r'{"k\u{74}y":"RSA","kty":"EC"' + tail + "}"
    assert not jwk._javascript_private_effects(text, {}).get(0, 0)


@pytest.mark.parametrize(
    "fields,source_private,parent_private",
    [
        (r'"k\u{74}y":"EC",...{"kty":"RSA","\p":"<source>"},"\p":"<parent>"', True, True),
        (r'"k\u{74}y":"RSA",...{"kty":"EC","\p":"<source>"},"\p":"<parent>"', False, False),
        (
            r'"k\u{74}y":"EC",...{"kty":"RSA","\p":"<source>"},"kty":"EC","\p":"<parent>"',
            True,
            False,
        ),
        (
            r'"k\u{74}y":"EC",...{"kty":"EC","\p":"<source>"},"kty":"RSA","\p":"<parent>"',
            True,
            True,
        ),
    ],
)
def test_a2_source_role_and_copy_ancestor_role_are_distinct(fields, source_private, parent_private):
    source, parent = fake(), fake()
    text = "{" + fields.replace("<source>", source).replace("<parent>", parent) + "}"
    (assert_private if source_private else assert_public)(text, source)
    (assert_private if parent_private else assert_public)(text, parent)


def test_a2_getter_export_hazard_survives_later_own_type():
    from unified_agent.telemetry import jwk

    text = r'{"k\u{74}y":"EC",...{get note(){this.kty="EC"},"kty":"RSA"}}'
    assert not jwk._javascript_private_effects(text, {}).get(0, 0)


@pytest.mark.parametrize(
    "shape,private",
    [
        (r'{"kty":"RSA",obj.r"\u{64}":"<value>"}', True),
        (r"""# orphan ' {"k\u{74}y":"RSA",'\\u{64}p':"<value>"}""", False),
    ],
)
def test_a2_actual_literal_guard_precedes_delimiter_consumption(shape, private):
    value = fake()
    text = shape.replace("<value>", value)
    (assert_private if private else assert_public)(text, value)


_A2_GROWING = ("ordered-computed-zero-private", "literal-spread-source-roles")


def _growing_a2(case, n, value="private"):
    member = '"\\p":' + json.dumps(value)
    if case == "ordered-computed-zero-private":
        fields = '["k"+"ty"]:"RSA",["kty"]:"EC",'
        return '{"k\\u{74}y":"RSA",' + fields * n + member + "}"
    assert case == "literal-spread-source-roles"
    source = '{"k\\u{74}y":"RSA",' + member + "}"
    return '{"note":{},...' * n + source + ',"kty":"EC","\\p":"public"}' * n


@pytest.mark.parametrize("case", _A2_GROWING)
def test_a2_resource_fixture_source_roles_and_public_neighbors(case):
    value, outside = fake(), fake()
    text = _growing_a2(case, 3, value)
    text += '\n{"kty":"RSA","n":' + json.dumps(outside) + "}"
    if case == "ordered-computed-zero-private":
        assert_public(text, value)
        assert private_members(text) == []
        assert list(check_secrets("fixture.js", text)) == []
    else:
        assert_private(text, value)
        assert_public(text, "public")
    assert_public(text, outside)
    outside_line = len(text.splitlines())
    assert not any(f.line == outside_line for f in check_secrets("fixture.js", text))


@pytest.mark.parametrize("case", _A2_GROWING)
def test_a2_wrong_role_fixture_public_controls(case):
    value = fake()
    text = _growing_a2(case, 3, value)
    text = text.replace('"\\p":', '"\\k":')
    assert_public(text, value)
    assert not any(f.rule == "SECRETS-KEY" for f in check_secrets("fixture.js", text))


@pytest.mark.parametrize("case", _A2_GROWING)
def test_a2_ordered_scan_grows_linearly(case):
    def run(text):
        return redact_sensitive_content(text, max_len=20 * len(text) + 1000)

    small, large = _growing_a2(case, 1000), _growing_a2(case, 4000)
    assert len(large) >= 3.5 * len(small)
    # CPU time, best of interleaved samples: runner load cannot decide it (#2188).
    growth = cpu_growth(cold(run), small, large, limit=8)
    small_time, large_time, ratio = growth.small, growth.large, growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_cpu=small_time,
                large_cpu=large_time,
                ratio=ratio,
            )
        )
    )
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} time"
    assert sum(len(m.value) for m in private_members(large)) <= len(large)


@pytest.mark.parametrize("case", _A2_GROWING)
def test_a2_ordered_peak_follows_existing_cost_model(case):
    small, large = _growing_a2(case, 1000), _growing_a2(case, 4000)

    def budget_for(found):
        return 48 * len(large) + 400 * len(found)

    # Garbage collected first and re-measured before failing (task #2188).
    growth = peak_growth(cold(private_members), small, large, limit=8, budget=budget_for)
    small_peak, large_peak, members = growth.small, growth.large, len(growth.result)
    budget = budget_for(growth.result)
    ratio = growth.ratio
    print(
        json.dumps(
            dict(
                case=case,
                small_chars=len(small),
                large_chars=len(large),
                small_peak=small_peak,
                large_peak=large_peak,
                large_members=members,
                budget=budget,
                ratio=ratio,
            )
        )
    )
    assert large_peak < budget, f"{case}: peak {large_peak} exceeds existing budget {budget}"
    assert ratio < 8, f"{case}: x4 input took x{ratio:.2f} peak"


@pytest.mark.parametrize("final,expected", [("EC", 2), ("RSA", 1)])
@pytest.mark.parametrize("bundle", ["absent", "wrong-owner", "wrong-offset"])
@pytest.mark.parametrize("recovery", [False, True])
def test_a2_ignored_bundle_clears_only_transient_entry_state(final, expected, bundle, recovery):
    from unified_agent.telemetry import jwk

    text = r'{"k\u{74}y":"RSA",**opaque,"kty":' + json.dumps(final) + "}"
    at = text.index("**")
    accepted = (
        {}
        if bundle == "absent"
        else {
            at if bundle == "wrong-owner" else at + 1: (
                999 if bundle == "wrong-owner" else 0,
                -1,
                2,
            )
        }
    )
    # These deliberately incomplete/mismatched bundles test internal cleanup,
    # not whether a producer proves this visible source operator opaque.
    assert jwk._javascript_private_effects(text, accepted, recovery=recovery).get(0) == expected
