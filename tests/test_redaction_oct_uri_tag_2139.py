"""URI properties keep lexical ownership and the existing linear cost limits.

All credential-shaped values are generated at runtime. Resource tests retain
the original oct-block x4-time <8 and per-input/per-member peak model.
"""

from __future__ import annotations

import json
import random
import re
import secrets

import pytest
from growth_budget import assert_cpu_growth, cold, peak_growth

from unified_agent.audit.rules import check_secrets
from unified_agent.telemetry import jwk, oct_blocks
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
        pytest.fail("pure URI-tag tests must not access a database")

    for name in ("init_db", "connect", "connect_read_only"):
        monkeypatch.setattr(storage, name, refused)
    assert not config.db_path().exists()
    yield
    assert not config.db_path().exists()


def fake():
    return "q" + secrets.token_hex(8) + "z"


def redact(text):
    return redact_sensitive_content(text, max_len=len(text) * 20 + 1000)


def protect(text, private):
    assert private not in redact(text), "URI tag exposed a private value"
    assert any(private in text[m.start : m.end] for m in jwk.private_members(text))
    expected = {i for i, row in enumerate(text.splitlines(), 1) if private in row}
    assert expected <= {f.line for f in check_secrets("fixture.txt", text)}
    findings = _jwk_findings(text)
    assert any(f.action == BLOCK and private in text[f.start : f.end] for f in findings)


@pytest.mark.parametrize("prefix", ["", "# '\\'words ", "log: "])
@pytest.mark.parametrize(
    "properties",
    [
        "!<tag:yaml.org,2002:str> ",
        "!<" + "x" * 300,
        (TYPE + ":!<") * 80 + ">",
        (TYPE + ":!<") * 80,
        "!<outer" + (TYPE + ":!<>!<") * 30 + ">",
    ],
)
def test_uri_properties_keep_all_four_consumers_private(prefix, properties):
    private = fake()
    protect(prefix + TYPE + ":" + properties + OCT + ", k=" + private, private)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\u2028"])
@pytest.mark.parametrize("properties", ["!<tag:example> ", "!<a!<b> !<c> "])
def test_uri_tag_on_explicit_key_lookahead_keeps_source_lines(newline, properties):
    private = fake()
    text = "? " + TYPE + newline + "# comment" + newline
    text += ": " + properties + OCT + newline + "k: " + private
    protect(text, private)


@pytest.mark.parametrize("syntax", ["json", "python", "yaml"])
def test_public_types_with_uri_properties_remain_public(syntax):
    public = fake()
    if syntax == "json":
        text = json.dumps({TYPE: "RSA", "k": public, "note": "!<public-tag>"})
    elif syntax == "python":
        text = repr({TYPE: "RSA", "k": public, "note": "!<public-tag>"})
    else:
        text = TYPE + ": !<tag:example> RSA\nk: " + public
    assert redact(text) == text
    assert not jwk.private_members(text)
    assert not list(check_secrets("fixture.txt", text))
    assert not _jwk_findings(text)


def test_private_uri_comment_cannot_retype_public_source_owner():
    public, private = fake(), fake()
    comment = TYPE + ":!<" + (TYPE + ":!<") * 60 + ">" + OCT + ", k=" + private
    text = repr({TYPE: "RSA", "k": public})[:-1] + ", # " + comment + "\n}"
    protect(text, private)
    assert public in redact(text), "comment URI changed the public mapping owner"


def test_uri_decorated_scalar_alias_still_supplies_the_type_key():
    private = fake()
    text = "type: &kind !<tag:example> " + TYPE + "\n? *kind\n"
    text += ": !<tag:example> " + OCT + "\nk: " + private
    protect(text, private)


@pytest.mark.parametrize("outer_oct", [False, True])
def test_candidates_inside_uri_are_not_assigned_the_outer_value(outer_oct):
    inner_value, outer_value = ("RSA", OCT) if outer_oct else (OCT, "RSA")
    text = TYPE + ":!<" + TYPE + ":" + inner_value + ">" + outer_value
    expected = [0 if outer_oct else len(TYPE + ":!<")]
    assert oct_blocks._triggers(text, oct_blocks._Spans([])) == expected


@pytest.mark.parametrize("properties", ["!<tag:example> ", "&anchor!<tag> "])
def test_overbudget_uri_fallback_stays_in_its_own_frame(properties):
    private, public = fake(), fake()
    uncertain = "{" + (TYPE + ":" + properties) * 20 + "UNKNOWN, p:" + repr(private) + "}"
    text = "# '\\'words " + uncertain + " {" + TYPE + ":RSA,k:" + repr(public) + "}"
    protect(text, private)
    assert public in redact(text), "URI work fence escaped its owning recovery frame"
    assert not any(public in text[m.start : m.end] for m in jwk.private_members(text))
    assert not any(public in text[f.start : f.end] for f in _jwk_findings(text))


@pytest.mark.parametrize("kind,member", [("RSA", "d"), ("EC", "d"), ("OKP", "d"), (OCT, "k")])
@pytest.mark.parametrize("prefix", ["", "b'", "r'", '"'])
@pytest.mark.parametrize("properties", ["!<tag:example> ", "!<outer!<inner> !<next> "])
def test_recovery_type_tails_keep_quoted_prefixes_and_private_roles(
    kind, member, prefix, properties
):
    private = fake()
    close = prefix[-1:] if prefix else ""
    text = "# '\\'words " + TYPE + ":" + properties + prefix + kind + close
    protect(text + ", " + member + ":" + repr(private), private)


def test_indexed_recovery_prefix_and_type_tails_match_the_legacy_regex():
    rng = random.Random(2139)
    old_space = rf"(?:{oct_blocks._PYTHON_PREFIX}(?=\\*+['\"]))?{oct_blocks._SKIP}"
    old_prefix = re.compile(old_space)
    old_types = {
        kind: re.compile(
            old_space + jwk._recovery_spelling(kind) + oct_blocks._CONTINUATION + r"(?![\w$])"
        )
        for kind in ("RSA", "EC", "OKP", OCT)
    }
    pieces = [
        "!<",
        ">",
        " ",
        "\t",
        "\n",
        "\u2028",
        "&anchor!<tag> ",
        "!tag ",
        "[",
        "(",
        "'",
        '"',
        "b'",
        "t'",
        "\\n",
        "\\u0020",
        "- ",
        "|+ ",
        TYPE + ":",
        OCT,
        "RSA",
        "OKP",
        "EC",
        "# comment",
        "x" * 260,
    ]
    for _ in range(1000):
        text = "".join(rng.choices(pieces, k=20))
        properties = oct_blocks._ValueHeads(text)
        positions = {0, len(text)}
        positions.update(end for _, end in oct_blocks._kty_candidates(text))
        positions = list(positions)
        rng.shuffle(positions)
        for position in positions:
            prefix_start = jwk._RECOVERY_TYPE_START.match(text, position).end()
            value_low = properties.skip(prefix_start)
            assert value_low == old_prefix.match(text, position).end()
            for kind, old in old_types.items():
                expected = old.match(text, position)
                actual = jwk._RECOVERY_TYPES[kind].match(text, value_low)
                assert bool(actual) == bool(expected)
                if expected is not None:
                    assert actual.end() == expected.end()


def test_indexed_uri_heads_preserve_the_original_lexical_rule():
    rng = random.Random(2139)
    pieces = [
        "!<",
        ">",
        " ",
        "\t",
        "\n",
        "\u2028",
        "&anchor ",
        "!tag ",
        "[",
        "(",
        "'",
        '"',
        "b'",
        "\\n",
        "\\u0020",
        "- ",
        "|+ ",
        TYPE + ":",
        OCT,
        "RSA",
        "*alias",
        "# comment",
        "x" * 260,
    ]
    cases = [
        "!<" + TYPE + ":" + OCT + ">RSA",
        "!<" + TYPE + ":!<>!<" + TYPE + ":!<>" + OCT,
        "!<" + TYPE + ":" + OCT + " !<" + TYPE + ":RSA>",
    ]
    cases.extend(
        "!<" + "x" * count + ending
        for count in (0, 1, 253, 254, 255, 256, 257, 512)
        for ending in ("", " ", ">" + OCT, "\u2028" + OCT)
    )
    cases.extend("".join(rng.choices(pieces, k=20)) for _ in range(1000))
    for text in cases:
        heads = oct_blocks._ValueHeads(text)
        positions = {0, len(text)}
        positions.update(end for _, end in oct_blocks._kty_candidates(text))
        positions.update(i for i in range(len(text)) if text.startswith("!<", i))
        # Lookahead can arrive out of order; cached shared tails must still be
        # valid when a later caller starts inside a previously scanned URI.
        positions = list(positions)
        rng.shuffle(positions)
        for position in positions:
            original = oct_blocks._VALUE_HEAD.match(text, position)
            indexed = heads.match(position)
            assert indexed.lastgroup == original.lastgroup
            assert indexed.end() == original.end()
            if original.lastgroup is not None:
                assert indexed.start(original.lastgroup) == original.start(original.lastgroup)


def test_indexed_scalar_anchor_heads_keep_multiline_and_comment_boundaries():
    rng = random.Random(2139)
    pieces = [
        "!<",
        ">",
        " ",
        "\t",
        "\n",
        "\r\n",
        "\u2028",
        "&a ",
        "!tag ",
        "# ",
        "'",
        '"',
        "\\\n ",
        TYPE,
        TYPE + "extra",
        "x" * 260,
    ]
    cases = [
        "!<tag> # eof",
        "!<tag> # comment\n\n" + TYPE,
        '!<tag> "' + TYPE + '\\\nextra"',
        "!<" + '&a"!<' * 80,
    ]
    cases.extend("".join(rng.choices(pieces, k=20)) for _ in range(1000))
    for text in cases:
        heads = oct_blocks._ValueHeads(text)
        positions = {0, len(text)}
        positions.update(anchor.end() for anchor in oct_blocks._ANCHOR_RE.finditer(text))
        positions = list(positions)
        rng.shuffle(positions)
        for position in positions:
            original = oct_blocks._KEY_SCALAR_HEAD.match(text, position)
            assert heads.key_scalar(position) == (original.end(), original.lastgroup == "kty")


@pytest.mark.parametrize("ending", ["", ">" + OCT])
def test_nested_uri_anchor_heads_preserve_private_source_ownership(ending):
    private = fake()
    text = "# '\\'words " + ('&a"!<' + TYPE + ":!<") * 80 + ending + ", k=" + private
    protect(text, private)


def _nested(n, ending):
    return "# '\\'words " + (TYPE + ":!<") * n + ending + ", k=value"


GROWING = {
    "closed-nested-uri": lambda n: _nested(n, ">" + OCT),
    "unfinished-nested-uri": lambda n: _nested(n, ""),
    "whitespace-ended-uri": lambda n: _nested(n, " " + OCT),
    "adjacent-nested-uri-runs": lambda n: (
        "# '\\'words " + TYPE + ":!<" + (TYPE + ":!<>!<") * n + ">" + OCT + ", k=value"
    ),
    "many-valid-uri-triggers": lambda n: (TYPE + ":!<tag:example> " + OCT + ", k=value\n") * n,
    "long-uri-without-triggers": lambda n: "note: !<" + "x" * n + "> public",
    "closed-anchor-uri": lambda n: (
        "# '\\'words " + ('&a"!<' + TYPE + ":!<") * n + ">" + OCT + ", k=value"
    ),
    "unfinished-anchor-uri": lambda n: "# '\\'words " + ('&a"!<' + TYPE + ":!<") * n + ", k=value",
}


@pytest.mark.parametrize("name", list(GROWING))
def test_uri_scan_grows_linearly(name):
    small, large = GROWING[name](1_000), GROWING[name](4_000)
    assert len(small) >= 1_000 and len(large) >= 3.5 * len(small)
    # CPU time, best of interleaved samples (task #2188).
    growth = assert_cpu_growth(cold(redact), small, large, limit=8, label=name)
    print(
        json.dumps(
            {
                "case": name,
                "small_n": 1000,
                "large_n": 4000,
                "small_chars": len(small),
                "large_chars": len(large),
                "small_cpu": growth.small,
                "large_cpu": growth.large,
                "ratio": growth.ratio,
            }
        )
    )
    assert sum(len(m.value) for m in jwk.private_members(large)) <= len(large)


@pytest.mark.parametrize("name", list(GROWING))
def test_uri_peak_follows_existing_cost_model(name):
    small, large = GROWING[name](1_000), GROWING[name](4_000)
    # The unchanged constants from test_redaction_oct_blocks.py.
    growth = peak_growth(
        cold(jwk.private_members),
        small,
        large,
        limit=8,
        budget=lambda found: 48 * len(large) + 400 * len(found),
    )
    small_peak, large_peak, members = growth.small, growth.large, len(growth.result)
    budget = 48 * len(large) + 400 * members
    print(
        json.dumps(
            {
                "case": name,
                "small_n": 1000,
                "large_n": 4000,
                "small_chars": len(small),
                "large_chars": len(large),
                "small_peak": small_peak,
                "large_peak": large_peak,
                "large_members": members,
                "budget": budget,
                "ratio": large_peak / small_peak,
            }
        )
    )
    assert large_peak < budget, f"{name}: peak {large_peak} exceeds existing budget {budget}"
    assert large_peak / small_peak < 8, f"{name}: x4 input took x{large_peak / small_peak:.2f} peak"
