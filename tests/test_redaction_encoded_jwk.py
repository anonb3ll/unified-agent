"""Explicit YAML keys inside encoded log strings keep their oct secret private (task #2124).

A YAML mapping whose type is written as an explicit key with a quoted name
(``? "kty"`` with ``: oct`` on the next line) was masked in raw text, but not
once that text sat inside an ordinary encoded string (a JSON log field, a
``json.dumps`` of the text, a Python ``repr``). The decoded string was scanned
for oct blocks only outside its own quoted strings, and the quoted key name is
one of them; the encoded original spells the key's line break as ``\\n``, which
the lexical rule does not read as the end of an explicit key. Every consumer
(the log redactor, ``jwk.private_members``, the public-relay gate and the
secrets audit) left the generated value in place.

Every key-shaped fixture is generated at runtime and the type names are
assembled, so no line of this file opens a block for the security audit. Each
fixture is loaded with PyYAML (and JSON where encoded) first, to prove it is
the key the test claims.

Later sections: strings past the decode bound fail closed whole, only complete
validated quoted text proves child-string ownership, and decoded terminal
scalars keep their literal backslashes.
"""

from __future__ import annotations

import ast
import base64
import json
import re
import secrets
import socket
import unicodedata

import pytest
import yaml
from growth_budget import assert_cpu_growth, peak_growth

from unified_agent.audit.engine import run_audit
from unified_agent.audit.rules import check_secrets
from unified_agent.telemetry import jwk
from unified_agent.telemetry.redaction import redact_sensitive_content
from unified_agent.tools.public_relay import BLOCK, GateContext, _jwk_findings, run_gates

TYPE = "kt" + "y"
OCT = "o" + "ct"
RSA = "R" + "SA"
MARK = "[REDACTED_SECRET]"
KEEP = "keep-value-2124"


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


@pytest.fixture(autouse=True)
def _strict_offline(monkeypatch):
    """Scanner exceptions raise instead of masking whole; no test reaches the network."""

    def offline(*args, **kwargs):
        raise RuntimeError("offline probe")

    monkeypatch.setenv(jwk.STRICT_ENV, "1")
    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket, "create_connection", offline)


def secret() -> str:
    """A fresh, short base64url symmetric key; never a committed literal."""
    return "S" + base64.urlsafe_b64encode(secrets.token_bytes(12)).decode().rstrip("=")


def redact(text: str) -> str:
    return redact_sensitive_content(text, max_len=len(text) * 20 + 1000)


def jwk_lines(text: str) -> set[int]:
    return {
        finding.line
        for finding in check_secrets("fixture.log", text)
        if "JWK private member" in finding.message
    }


def lines_of(text: str, needle: str) -> set[int]:
    return {n for n, row in enumerate(text.splitlines(), 1) if needle in row}


def protect(text: str, value: str) -> str:
    """Every consumer hides or reports ``value``, attributed to its own line.

    Each check is a bare boolean, so a failure never prints the generated value.
    """
    assert (value in text) is True
    out = redact(text)
    masked = value not in out
    assert masked is True, "the redactor left the value in place"
    members = jwk.private_members(text)
    covered = any(value in text[m.start : m.end] for m in members)
    assert covered is True, "no private member covers the value"
    blocked = any(f.action == BLOCK and value in text[f.start : f.end] for f in _jwk_findings(text))
    assert blocked is True, "the public relay does not refuse the value"
    attributed = lines_of(text, value) <= jwk_lines(text)
    assert attributed is True, (sorted(lines_of(text, value)), sorted(jwk_lines(text)))
    return out


def untouched(text: str) -> None:
    """No consumer masks, reports or attributes anything in ``text``."""
    assert redact(text) == text
    assert jwk.private_members(text) == []
    assert _jwk_findings(text) == []
    assert jwk_lines(text) == set()


# The type name is runtime-generated in pieces, as in the filer's reproduction.
def _name() -> str:
    return "".join(["k", "t", "y"])


def _explicit_double(k: str) -> str:
    return '? "' + _name() + '"\n: ' + OCT + "\nk: " + k


def _explicit_single(k: str) -> str:
    return "? '" + _name() + "'\n: " + OCT + "\nk: " + k


def _explicit_quoted_value(k: str) -> str:
    return '? "' + _name() + '"\n: "' + OCT + '"\nk: ' + k


def _explicit_both_keys(k: str) -> str:
    return '? "' + _name() + '"\n: ' + OCT + '\n? "k"\n: ' + k


def _explicit_after_comment(k: str) -> str:
    return '? "' + _name() + '"\n# note\n\n: ' + OCT + "\nk: " + k


def _explicit_crlf(k: str) -> str:
    return '? "' + _name() + '"\r\n: ' + OCT + "\r\nk: " + k


def _explicit_nested(k: str) -> str:
    return "jwk:\n  ? '" + _name() + "'\n  : " + OCT + "\n  k: " + k


def _explicit_sequence_item(k: str) -> str:
    return 'keys:\n- ? "' + _name() + '"\n  : ' + OCT + "\n  k: " + k


FORMS = {
    "explicit-double": _explicit_double,
    "explicit-single": _explicit_single,
    "explicit-quoted-value": _explicit_quoted_value,
    "explicit-both-keys": _explicit_both_keys,
    "explicit-after-comment": _explicit_after_comment,
    "explicit-crlf": _explicit_crlf,
    "explicit-nested": _explicit_nested,
    "explicit-sequence-item": _explicit_sequence_item,
}


def _json_field(plain: str) -> str:
    return json.dumps({"log": plain})


def _json_string(plain: str) -> str:
    return json.dumps(plain)


def _python_repr(plain: str) -> str:
    return repr({"log": plain})


def _json_in_json(plain: str) -> str:
    return json.dumps({"event": json.dumps({"log": plain})})


def _json_line_in_file(plain: str) -> str:
    return "start\n" + json.dumps({"level": "info", "log": plain}) + "\nend " + KEEP + "\n"


ENCODINGS = {
    "raw": lambda plain: plain,
    "json-field": _json_field,
    "json-string": _json_string,
    "python-repr": _python_repr,
    "json-in-json": _json_in_json,
    "json-line-in-file": _json_line_in_file,
}


def _decoded(name: str, text: str) -> str:
    """The YAML text each encoding carries, read back with its own parser."""
    if name == "raw":
        return text
    if name == "json-field":
        return json.loads(text)["log"]
    if name == "json-string":
        return json.loads(text)
    if name == "python-repr":
        return ast.literal_eval(text)["log"]
    if name == "json-in-json":
        return json.loads(json.loads(text)["event"])["log"]
    return json.loads(text.splitlines()[1])["log"]


def _typed(node) -> dict:
    pending = [node]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get(TYPE) == OCT:
                return item
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    raise AssertionError("no symmetric key in the fixture")


@pytest.mark.parametrize("encoding", list(ENCODINGS))
@pytest.mark.parametrize("form", list(FORMS))
def test_an_encoded_explicit_key_jwk_masks_its_secret(form, encoding):
    k = secret()
    plain = FORMS[form](k)
    text = ENCODINGS[encoding](plain)
    decoded = _decoded(encoding, text)
    assert decoded == plain
    assert _typed(yaml.safe_load(decoded))["k"] == k
    out = protect(text, k)
    if encoding in ("json-field", "json-string"):
        json.loads(out)  # masking keeps a JSON log line JSON
    if encoding == "json-in-json":
        # The outer string holds the decoded key: it is masked whole, as JSON.
        assert json.loads(out) == {"event": MARK}
    if encoding == "json-line-in-file":
        assert out.splitlines()[0] == "start"
        assert out.splitlines()[2] == "end " + KEEP
        assert json.loads(out.splitlines()[1])["level"] == "info"


def test_the_filers_reproduction_is_masked_by_every_consumer():
    k = secret()
    plain = '? "' + ("kt" + "y") + '"\n: ' + ("o" + "ct") + "\nk: " + k
    assert yaml.safe_load(plain) == {TYPE: OCT, "k": k}
    assert any(m.name == "k" for m in jwk.private_members(plain))
    protect(plain, k)
    encoded = json.dumps({"log": plain})
    assert any(m.name == "k" for m in jwk.private_members(encoded))
    assert json.loads(protect(encoded, k)) == {"log": MARK}


def test_the_audit_names_the_physical_line_and_its_own_suppression(tmp_path):
    k = secret()
    log = json.dumps({"log": _explicit_double(k)})
    text = "# ua:allow on an unrelated line\nfirst = 1\n" + log + "\nlast = 2\n"
    assert jwk_lines(text) == {3}
    (tmp_path / "fixture.log").write_text(text)
    assert any(
        f["rule"] == "SECRETS-KEY" and f["line"] == 3 for f in run_audit(tmp_path)["findings"]
    )
    # Only the line holding the value can suppress its finding.
    (tmp_path / "fixture.log").write_text(text.replace(log, log + "  # ua:allow"))
    assert not any(f["rule"] == "SECRETS-KEY" for f in run_audit(tmp_path)["findings"])


def test_a_surrounding_rsa_object_is_not_retyped():
    d, k = secret(), secret()
    modulus = "pub-" + secrets.token_hex(4)
    outer = {TYPE: RSA, "n": modulus, "e": "AQAB", "d": d, "log": _explicit_double(k)}
    text = json.dumps(outer)
    out = protect(text, k)
    protect(text, d)
    loaded = json.loads(out)
    assert loaded[TYPE] == RSA
    assert loaded["n"] == modulus
    assert loaded["e"] == "AQAB"
    assert loaded["d"] == MARK
    assert loaded["log"] == MARK


@pytest.mark.parametrize("encoding", ["raw", "json-field", "json-string", "python-repr"])
@pytest.mark.parametrize(
    "plain",
    [
        '? "kind"\n: ' + OCT + "\nk: " + KEEP,
        '? "name"\n: value\nk: ' + KEEP,
        "? 'note'\n: plain text\nother: " + KEEP,
        "message: " + KEEP + "\nlevel: info",
    ],
    ids=["other-explicit-key", "explicit-no-type", "single-quoted-no-type", "no-key-type"],
)
def test_logs_without_a_key_type_are_unchanged(plain, encoding):
    text = ENCODINGS[encoding](plain)
    untouched(text)
    if encoding in ("json-field", "json-string"):
        assert json.loads(redact(text)) == json.loads(text)


@pytest.mark.parametrize("encoding", ["json-field", "json-string"])
def test_a_quoted_type_name_without_a_separator_after_it_is_unchanged(encoding):
    # The name is the whole content of a string that is not a key: no mapping.
    plain = 'note: "' + _name() + '"\nwhy: ' + KEEP
    text = ENCODINGS[encoding](plain)
    untouched(text)


def _many_encoded_keys(n: int) -> str:
    return "\n".join(json.dumps({"i": i, "log": _explicit_double("v" + str(i))}) for i in range(n))


def _many_quoted_names_in_one_string(n: int) -> str:
    # Many quoted names, each a candidate key, in one decoded string.
    body = "".join('? "' + _name() + '"\n: x' + str(i) + "\n" for i in range(n))
    return json.dumps({"log": body + "? '" + _name() + "'\n: " + OCT + "\nk: tail"})


def _many_unclosed_quoted_names(n: int) -> str:
    return json.dumps({"log": ('"' + _name() + " ") * n})


GROWING = {
    "many-encoded-keys": (_many_encoded_keys, 100, 400),
    "many-quoted-names-in-one-string": (_many_quoted_names_in_one_string, 250, 1_000),
    "many-unclosed-quoted-names": (_many_unclosed_quoted_names, 250, 1_000),
}


@pytest.mark.parametrize("name", list(GROWING))
def test_encoded_explicit_key_scanning_grows_linearly(name):
    build, low, high = GROWING[name]
    small, large = build(low), build(high)
    assert len(large) >= 3.5 * len(small)
    assert_cpu_growth(redact, small, large, limit=8, label=name)


@pytest.mark.parametrize("name", list(GROWING))
def test_encoded_explicit_key_scanning_peak_follows_the_cost_model(name):
    build, low, high = GROWING[name]
    small, large = build(low), build(high)
    growth = peak_growth(
        jwk.private_members,
        small,
        large,
        limit=8,
        budget=lambda found: 48 * len(large) + 400 * len(found),
    )
    assert growth.large < 48 * len(large) + 400 * len(growth.result)
    assert growth.ratio < 8
    if name == "many-encoded-keys":
        assert len({(m.start, m.end) for m in growth.result}) >= high


# Review at 79723aa6 (P1): past the decode bound (three nested decodes), a
# still-encoded string holding a key type and a private member name is masked
# whole, without decoding it. Depths 1-3 keep the decoded reading.


def _wrapped(plain: str, layers: int) -> str:
    text = plain
    for _ in range(layers):
        text = json.dumps({"log": text})
    return text


def _unwrapped(text: str, layers: int) -> str:
    for _ in range(layers):
        text = json.loads(text)["log"]
    return text


def _rsa_json(d: str) -> str:
    return json.dumps({TYPE: RSA, "n": "pub-" + secrets.token_hex(4), "e": "AQAB", "d": d})


def _rsa_block(d: str) -> str:
    return TYPE + ": " + RSA + "\nn: pub-" + secrets.token_hex(4) + "\ne: AQAB\nd: " + d


DEEP_FORMS = {
    "explicit-double": _explicit_double,
    "explicit-single": _explicit_single,
    "explicit-after-comment": _explicit_after_comment,
    "explicit-both-keys": _explicit_both_keys,
    "rsa-json-object": _rsa_json,
    "rsa-block-mapping": _rsa_block,
}
DEPTHS = [4, 5, 8]


@pytest.mark.parametrize("layers", DEPTHS)
@pytest.mark.parametrize("form", list(DEEP_FORMS))
def test_a_key_past_the_decode_bound_masks_the_whole_string(form, layers):
    value = secret()
    plain = DEEP_FORMS[form](value)
    if form.startswith("rsa-json"):
        assert json.loads(plain)["d"] == value
    else:
        parsed = yaml.safe_load(plain)
        assert value in (parsed.get("k"), parsed.get("d"))
    text = _wrapped(plain, layers)
    assert _unwrapped(text, layers) == plain
    out = protect(text, value)
    assert json.loads(out) == {"log": MARK}


@pytest.mark.parametrize("layers", DEPTHS)
def test_a_deep_key_on_a_log_line_is_audited_on_that_line(layers, tmp_path):
    value = secret()
    line = _wrapped(_explicit_double(value), layers)
    text = "first " + KEEP + "\n" + line + "\nlast " + KEEP + "\n"
    out = protect(text, value)
    assert jwk_lines(text) == {2}
    assert out.splitlines()[0] == "first " + KEEP
    assert out.splitlines()[2] == "last " + KEEP
    assert json.loads(out.splitlines()[1]) == {"log": MARK}
    (tmp_path / "fixture.log").write_text(text)
    assert {f["line"] for f in run_audit(tmp_path)["findings"] if f["rule"] == "SECRETS-KEY"} == {2}


@pytest.mark.parametrize("layers", DEPTHS)
def test_a_deep_key_keeps_the_surrounding_rsa_object(layers):
    d, value = secret(), secret()
    modulus = "pub-" + secrets.token_hex(4)
    outer = {TYPE: RSA, "n": modulus, "e": "AQAB", "d": d}
    outer["log"] = _wrapped(_explicit_double(value), layers - 1)
    text = json.dumps(outer)
    out = protect(text, value)
    protect(text, d)
    loaded = json.loads(out)
    assert (loaded[TYPE], loaded["n"], loaded["e"]) == (RSA, modulus, "AQAB")
    assert loaded["d"] == MARK and loaded["log"] == MARK


@pytest.mark.parametrize("layers", [1, 2, 3, *DEPTHS])
@pytest.mark.parametrize(
    "plain",
    [
        '? "kind"\n: ' + OCT + "\nk: " + KEEP,
        TYPE + ": " + RSA + "\nn: " + KEEP + "\ne: AQAB",
        "note: the " + TYPE + " field\nwhy: " + KEEP,
        json.dumps({TYPE: "EC", "crv": "P-256", "x": KEEP, "y": KEEP}),
        "message: " + KEEP + "\nd: " + KEEP,
    ],
    ids=["other-explicit-key", "public-rsa", "type-in-prose", "public-ec", "no-key-type"],
)
def test_deep_logs_without_a_private_key_are_unchanged(plain, layers):
    text = _wrapped(plain, layers)
    untouched(text)
    assert json.loads(redact(text)) == json.loads(text)


def _many_deep_keys(n: int) -> str:
    return "\n".join(_wrapped(_explicit_double("v" + str(i)), 4) for i in range(n))


def _one_deep_string_of_names(n: int) -> str:
    # Key-type and private-name candidates that never meet a separator.
    return _wrapped(("\\" * 7 + '"' + TYPE + " d " + "\\" * 3 + "n ") * n, 4)


DEEP_GROWING = {
    "many-deep-keys": (_many_deep_keys, 50, 200),
    "one-deep-string-of-names": (_one_deep_string_of_names, 250, 1_000),
}


@pytest.mark.parametrize("name", list(DEEP_GROWING))
def test_deep_fail_closed_scanning_grows_linearly(name):
    build, low, high = DEEP_GROWING[name]
    small, large = build(low), build(high)
    assert len(large) >= 3.5 * len(small)
    assert_cpu_growth(redact, small, large, limit=8, label=name)


@pytest.mark.parametrize("name", list(DEEP_GROWING))
def test_deep_fail_closed_scanning_peak_follows_the_cost_model(name):
    build, low, high = DEEP_GROWING[name]
    small, large = build(low), build(high)
    growth = peak_growth(
        jwk.private_members,
        small,
        large,
        limit=8,
        budget=lambda found: 48 * len(large) + 400 * len(found),
    )
    assert growth.large < 48 * len(large) + 400 * len(growth.result)
    assert growth.ratio < 8
    if name == "many-deep-keys":
        assert len({(m.start, m.end) for m in growth.result}) >= high


def test_child_strings_is_one_forward_pass():
    # Quoted key names among many child strings: each body is classified once.
    body = "".join('"c' + str(i) + '" ' for i in range(50)) + '"' + TYPE + '": ' + OCT
    quoted = jwk.oct_blocks._quote_context(body)[0]
    kept = jwk._child_strings(body, quoted)
    assert len(kept) == len(quoted) - 1
    assert body[slice(*[s for s in quoted if s not in kept][0])] == TYPE


# Review at 875394f0 (P1): past the decode bound, every spelling the decoded
# path reads must fail closed too. The check flattens escape layers (any run of
# backslashes, \u, \U, \x and octal escapes, quotes, YAML continuations) before
# it looks for the names, so no escape form is enumerated.

BACKSLASH = chr(92)


def _continued_type(k: str) -> str:
    # A YAML double-quoted key continued across a physical break: "kty".
    return '? "k' + BACKSLASH + '\n  ty"\n: ' + OCT + "\nk: " + k


def _json_unicode_names(k: str) -> str:
    def u(word: str) -> str:
        return "".join(BACKSLASH + "u" + format(ord(char), "04x") for char in word)

    return '{"' + u(TYPE) + '": "' + OCT + '", "' + u("k") + '": "' + k + '"}'


def _python_octal_names(k: str) -> str:
    def o(word: str) -> str:
        return "".join(BACKSLASH + format(ord(char), "o") for char in word)

    return "{'" + o(TYPE) + "': '" + OCT + "', '" + o("k") + "': '" + k + "'}"


def _python_mixed_names(k: str) -> str:
    name = BACKSLASH + "x6b" + "t" + BACKSLASH + "u0079"
    return "{'" + name + "': '" + OCT + "', '" + BACKSLASH + "153': '" + k + "'}"


def _json_object(k: str) -> str:
    return json.dumps({TYPE: OCT, "k": k})


def _rsa_unicode_names(d: str) -> str:
    def u(word: str) -> str:
        return "".join(BACKSLASH + "u" + format(ord(char), "04x") for char in word)

    return '{"' + u(TYPE) + '": "' + RSA + '", "n": "pub", "' + u("d") + '": "' + d + '"}'


def _parsed(form: str, plain: str) -> dict:
    if form == "continued-type":
        return yaml.safe_load(plain)
    if form in ("json-unicode-names", "json-object", "rsa-unicode-names"):
        return json.loads(plain)

    return ast.literal_eval(plain)


ESCAPED_FORMS = {
    "continued-type": _continued_type,
    "json-unicode-names": _json_unicode_names,
    "python-octal-names": _python_octal_names,
    "python-mixed-names": _python_mixed_names,
    "json-object": _json_object,
    "rsa-unicode-names": _rsa_unicode_names,
}


def _enveloped(plain: str, layers: int, quotes: bool) -> str:
    """``layers`` JSON envelopes; with ``quotes``, each spells its quotes \\u0022."""
    text = plain
    for _ in range(layers):
        text = json.dumps({"log": text})
        if quotes:
            text = text.replace(BACKSLASH + '"', BACKSLASH + "u0022")
    return text


def _opened(text: str, layers: int) -> str:
    for _ in range(layers):
        text = json.loads(text)["log"]
    return text


@pytest.mark.parametrize("quotes", [False, True], ids=["plain-envelopes", "u0022-envelopes"])
@pytest.mark.parametrize("layers", [4, 5, 8])
@pytest.mark.parametrize("form", list(ESCAPED_FORMS))
def test_an_escaped_spelling_past_the_decode_bound_fails_closed(form, layers, quotes):
    value = secret()
    plain = ESCAPED_FORMS[form](value)
    parsed = _parsed(form, plain)
    assert parsed[TYPE] in (OCT, RSA)
    assert value in (parsed.get("k"), parsed.get("d"))
    text = _enveloped(plain, layers, quotes)
    assert _opened(text, layers) == plain
    out = protect(text, value)
    assert json.loads(out) == {"log": MARK}


@pytest.mark.parametrize("quotes", [False, True], ids=["plain-envelopes", "u0022-envelopes"])
@pytest.mark.parametrize("layers", [4, 5, 8])
def test_an_escaped_spelling_on_a_log_line_is_audited_on_that_line(layers, quotes, tmp_path):
    value = secret()
    line = _enveloped(_continued_type(value), layers, quotes)
    text = "first " + KEEP + "\n" + line + "\nlast " + KEEP + "\n"
    out = protect(text, value)
    assert jwk_lines(text) == {2}
    assert out.splitlines()[0] == "first " + KEEP
    assert out.splitlines()[2] == "last " + KEEP
    (tmp_path / "fixture.log").write_text(text)
    findings = run_audit(tmp_path)["findings"]
    assert {f["line"] for f in findings if f["rule"] == "SECRETS-KEY"} == {2}


@pytest.mark.parametrize("layers", [5, 8])
def test_a_tab_before_an_explicit_key_comment_fails_closed(layers):
    # Raw-input evidence from the review (PyYAML rejects the tab excerpt).
    value = secret()
    plain = '? "' + TYPE + '"\t# c\n: ' + OCT + "\nk: " + value
    text = _enveloped(plain, layers, False)
    protect(text, value)


def _u(word: str) -> str:
    return "".join(BACKSLASH + "u" + format(ord(char), "04x") for char in word)


ESCAPED_CONTROLS = {
    "continued-other-key": '? "k' + BACKSLASH + '\n  ind"\n: ' + OCT + "\nk: " + KEEP,
    "unicode-other-key": '{"' + _u("kind") + '": "' + OCT + '", "' + _u("k") + '": "' + KEEP + '"}',
    "unicode-public-rsa": '{"' + _u(TYPE) + '": "' + RSA + '", "n": "' + KEEP + '", "e": "AQAB"}',
    # No private member name: since the review at 5a5b916b any whole-token
    # private name beside a type name past the bound masks (accepted there).
    "unicode-type-in-prose": '{"note": "the ' + _u(TYPE) + ' field", "kid": "' + KEEP + '"}',
}


@pytest.mark.parametrize("quotes", [False, True], ids=["plain-envelopes", "u0022-envelopes"])
@pytest.mark.parametrize("layers", [1, 3, 4, 5, 8])
@pytest.mark.parametrize("control", list(ESCAPED_CONTROLS))
def test_escaped_logs_without_a_private_key_are_unchanged(control, layers, quotes):
    text = _enveloped(ESCAPED_CONTROLS[control], layers, quotes)
    untouched(text)


def _many_escaped_deep_keys(n: int) -> str:
    return "\n".join(_enveloped(_json_unicode_names("v" + str(i)), 5, True) for i in range(n))


def _one_deep_run_of_escapes(n: int) -> str:
    # Escape prefixes and name candidates that never meet a separator.
    piece = BACKSLASH * 5 + "u006b" + BACKSLASH * 3 + "x74y " + BACKSLASH * 2 + "153 d "
    return _enveloped(piece * n, 4, True)


ESCAPED_GROWING = {
    "many-escaped-deep-keys": (_many_escaped_deep_keys, 50, 200),
    "one-deep-run-of-escapes": (_one_deep_run_of_escapes, 250, 1_000),
}


@pytest.mark.parametrize("name", list(ESCAPED_GROWING))
def test_escaped_fail_closed_scanning_grows_linearly(name):
    build, low, high = ESCAPED_GROWING[name]
    small, large = build(low), build(high)
    assert len(large) >= 3.5 * len(small)
    assert_cpu_growth(redact, small, large, limit=8, label=name)


@pytest.mark.parametrize("name", list(ESCAPED_GROWING))
def test_escaped_fail_closed_scanning_peak_follows_the_cost_model(name):
    build, low, high = ESCAPED_GROWING[name]
    small, large = build(low), build(high)
    growth = peak_growth(
        jwk.private_members,
        small,
        large,
        limit=8,
        budget=lambda found: 48 * len(large) + 400 * len(found),
    )
    assert growth.large < 48 * len(large) + 400 * len(growth.result)
    assert growth.ratio < 8
    if name == "many-escaped-deep-keys":
        assert len({(m.start, m.end) for m in growth.result}) >= high


# Review at 5a5b916b (two P1s): CRLF continuations and named/braced Unicode
# escapes were not flattened, and running out of flattening rounds released
# the string. Past the decode bound the check now treats every line-break
# spelling alike, decodes \N{...} and \u{...}, turns any other escape into a
# neutral separator, masks a string whose flattening is still unfinished after
# the round cap, and backs the separator search with a whole-token search.


def _crlf_continued_type(k: str) -> str:
    return '? "k' + BACKSLASH + '\r\n  ty"\r\n: ' + OCT + "\r\nk: " + k


def _crlf_continued_flow(k: str) -> str:
    # A flow mapping with an explicit, continued key (an implicit flow key
    # must fit on one line).
    return '{? "k' + BACKSLASH + '\r\n  ty"\r\n : ' + OCT + ", k: " + k + "}"


def _python_named_names(k: str) -> str:
    name = "".join(BACKSLASH + "N{LATIN SMALL LETTER " + c.upper() + "}" for c in TYPE)
    return "{'" + name + "': '" + OCT + "', 'k': '" + k + "'}"


def _javascript_braced_names(k: str) -> str:
    name = "".join(BACKSLASH + "u{" + format(ord(c), "x") + "}" for c in TYPE)
    return "{" + name + ': "' + OCT + '", k: "' + k + '"}'


def _parsed_round3(form: str, plain: str) -> dict:
    if form.startswith("crlf"):
        return yaml.safe_load(plain)
    if form == "python-named-names":
        return ast.literal_eval(plain)
    # A JavaScript IdentifierName with braced escapes: decode them and read
    # the object as YAML flow (the same shape, with plain keys).

    def braced(match):
        return chr(int(match.group(1), 16))

    return yaml.safe_load(re.sub(r"\\u\{([0-9a-fA-F]+)\}", braced, plain))


ROUND3_FORMS = {
    "crlf-continued-type": _crlf_continued_type,
    "crlf-continued-flow": _crlf_continued_flow,
    "python-named-names": _python_named_names,
    "javascript-braced-names": _javascript_braced_names,
}


@pytest.mark.parametrize("quotes", [False, True], ids=["plain-envelopes", "u0022-envelopes"])
@pytest.mark.parametrize("layers", [4, 5, 8])
@pytest.mark.parametrize("form", list(ROUND3_FORMS))
def test_crlf_and_named_escapes_past_the_decode_bound_fail_closed(form, layers, quotes):
    value = secret()
    plain = ROUND3_FORMS[form](value)
    parsed = _parsed_round3(form, plain)
    assert parsed == {TYPE: OCT, "k": value}
    text = _enveloped(plain, layers, quotes)
    assert _opened(text, layers) == plain
    out = protect(text, value)
    assert json.loads(out) == {"log": MARK}


def _backslash_envelopes(plain: str, layers: int) -> str:
    """The review's envelopes: every backslash and quote spelled \\u005c / \\u0022."""
    text = plain
    for _ in range(layers):
        text = json.dumps({"log": text})
        text = text.replace(BACKSLASH * 2, BACKSLASH + "u005c")
        text = text.replace(BACKSLASH + '"', BACKSLASH + "u0022")
    return text


@pytest.mark.parametrize("layers", [8, 12, 13, 16, 32, 64])
def test_running_out_of_flattening_rounds_fails_closed(layers):
    value = secret()
    plain = json.dumps({TYPE: OCT, "k": value})
    text = _backslash_envelopes(plain, layers)
    assert _opened(text, layers) == plain
    assert json.loads(_opened(text, layers))["k"] == value
    out = protect(text, value)
    assert json.loads(out) == {"log": MARK}


@pytest.mark.parametrize("build", ["crlf", "named", "rounds"])
def test_round3_spellings_are_audited_on_their_own_line(build, tmp_path):
    value = secret()
    if build == "crlf":
        line = _enveloped(_crlf_continued_type(value), 5, True)
    elif build == "named":
        line = _enveloped(_python_named_names(value), 5, False)
    else:
        line = _backslash_envelopes(json.dumps({TYPE: OCT, "k": value}), 13)
    text = "first " + KEEP + "\n" + line + "\nlast " + KEEP + "\n"
    out = protect(text, value)
    assert jwk_lines(text) == {2}
    assert out.splitlines()[0] == "first " + KEEP
    assert out.splitlines()[2] == "last " + KEEP
    (tmp_path / "fixture.log").write_text(text)
    findings = run_audit(tmp_path)["findings"]
    assert {f["line"] for f in findings if f["rule"] == "SECRETS-KEY"} == {2}


ROUND3_CONTROLS = {
    "crlf-continued-other-key": '? "k' + BACKSLASH + '\r\n  ind"\r\n: ' + OCT + "\r\nk: " + KEEP,
    "named-other-key": "{'"
    + "".join(BACKSLASH + "N{LATIN SMALL LETTER " + c.upper() + "}" for c in "kind")
    + "': '"
    + OCT
    + "', 'k': '"
    + KEEP
    + "'}",
    "public-ec-json": json.dumps({TYPE: "EC", "crv": "P-256", "x": KEEP, "y": KEEP}),
    "no-key-type-json": json.dumps({"kind": OCT, "k": KEEP}),
}


@pytest.mark.parametrize("layers", [1, 3, 4, 5, 8, 13])
@pytest.mark.parametrize("control", list(ROUND3_CONTROLS))
def test_round3_controls_are_unchanged(control, layers):
    plain = ROUND3_CONTROLS[control]
    untouched(_enveloped(plain, layers, True))
    if layers > 8:
        # Still encoded after the flattening round cap: past the bound that
        # fails closed whatever the text names (review at 454a2038), so the
        # whole string is masked. Accepted over-masking past the bound only.
        text = _backslash_envelopes(plain, layers)
        assert json.loads(redact(text)) == {"log": MARK}
        return
    untouched(_backslash_envelopes(plain, layers))


def _deep_envelope_depth(layers: int) -> str:
    return _backslash_envelopes(json.dumps({TYPE: OCT, "k": "v" * 8}), layers)


def test_adversarially_deep_envelopes_cost_grows_linearly():
    # 32 against 64 envelopes. Each envelope re-escapes every earlier one, so
    # the text grows about quadratically with depth (3.8 times here); the
    # bounded flattening rounds keep the scan linear in the text.
    small, large = _deep_envelope_depth(32), _deep_envelope_depth(64)
    assert len(large) >= 3.5 * len(small)
    assert_cpu_growth(redact, small, large, limit=8, label="deep-envelopes")
    growth = peak_growth(
        jwk.private_members,
        small,
        large,
        limit=8,
        budget=lambda found: 48 * len(large) + 400 * len(found),
    )
    assert growth.large < 48 * len(large) + 400 * len(growth.result)


# Review at 454a2038 (two P1s): an encoded type spelling still unresolved at
# the round cap, and braced escapes with leading zeroes. Past the decode bound
# a string whose flattening does not finish, or that holds an escape the pass
# cannot decode, is now masked whole without looking for a type at all.


def _round4_python(name: str, k: str) -> str:
    return "{'" + name + "ty': '" + OCT + "', 'k': " + repr(k) + "}"


ROUND4_NAMES = {
    "named": BACKSLASH + "N{LATIN SMALL LETTER K}",
    "octal": BACKSLASH + "153",
    "unicode": BACKSLASH + "u006b",
}


@pytest.mark.parametrize("layers", [13, 16])
@pytest.mark.parametrize("spelling", list(ROUND4_NAMES))
def test_an_unresolved_type_spelling_at_the_round_cap_fails_closed(spelling, layers):
    value = secret()
    plain = _round4_python(ROUND4_NAMES[spelling], value)
    assert ast.literal_eval(plain) == {TYPE: OCT, "k": value}
    text = _backslash_envelopes(plain, layers)
    assert _opened(text, layers) == plain
    out = protect(text, value)
    assert json.loads(out) == {"log": MARK}


def _braced_leading_zeros(k: str) -> str:
    return "{" + BACKSLASH + "u{00000006b}ty: " + json.dumps(OCT) + ", k: " + json.dumps(k) + "}"


@pytest.mark.parametrize("layers", [1, 3, 4, 5, 8])
def test_braced_escapes_with_leading_zeroes_fail_closed(layers):
    value = secret()
    plain = _braced_leading_zeros(value)
    decoded = re.sub(r"\\u\{([0-9a-fA-F]+)\}", lambda m: chr(int(m.group(1), 16)), plain)
    assert yaml.safe_load(decoded) == {TYPE: OCT, "k": value}
    text = _enveloped(plain, layers, False)
    assert _opened(text, layers) == plain
    protect(text, value)


@pytest.mark.parametrize("layers", [4, 5, 8])
def test_an_undecodable_escape_past_the_bound_fails_closed(layers):
    # Seventeen hex digits: longer than any braced escape the pass decodes.
    value = secret()
    name = BACKSLASH + "u{" + "0" * 15 + "6b}"
    plain = "{" + name + "ty: " + json.dumps(OCT) + ", k: " + json.dumps(value) + "}"
    text = _enveloped(plain, layers, False)
    assert json.loads(protect(text, value)) == {"log": MARK}


@pytest.mark.parametrize("layers", [1, 2, 3])
def test_undecodable_escapes_within_the_bound_are_unchanged(layers):
    # Past the bound only: within it the decoded path keeps its own reading.
    plain = json.dumps({"path": "C:" + BACKSLASH + "Users" + BACKSLASH + "x", "k": KEEP})
    untouched(_enveloped(plain, layers, False))


@pytest.mark.parametrize("layers", [13, 16])
def test_an_unfinished_flattening_round_cap_masks_without_a_type(layers):
    # Accepted over-masking past the bound: unresolved encoding is masked whole.
    plain = json.dumps({"kind": OCT, "note": KEEP})
    text = _backslash_envelopes(plain, layers)
    assert json.loads(redact(text)) == {"log": MARK}


# --- Decode-bound boundaries ----------------------------------------------------------------------


def _public(kind, note):
    fields = {"x": KEEP, "y": KEEP} if kind == "EC" else {"n": KEEP, "e": "AQAB"}
    return json.dumps({TYPE: kind, **fields, "note": note})


@pytest.mark.parametrize("depth", [13, 16])
@pytest.mark.parametrize("name", list(ROUND4_NAMES))
def test_original_cap_p1_is_closed(name, depth):
    value = secret()
    text = _backslash_envelopes(_round4_python(ROUND4_NAMES[name], value), depth)
    protect(text, value)


@pytest.mark.parametrize("depth", [1, 3, 4, 5, 8])
def test_original_braced_p1_is_closed(depth):
    value = secret()
    text = _enveloped(_braced_leading_zeros(value), depth, False)
    protect(text, value)


@pytest.mark.parametrize("kind", ["EC", "RSA"])
@pytest.mark.parametrize("tail", ["windows", "unknown"])
def test_three_envelopes_preserve_public_object_with_literal_backslash(kind, tail):
    note = "C:" + chr(92) + "Users" + chr(92) + "x" if tail == "windows" else chr(92) + "z"
    plain = _public(kind, note)
    text = _enveloped(plain, 3, False)
    assert _opened(text, 3) == plain
    untouched(text)


@pytest.mark.parametrize("kind", ["EC", "RSA"])
def test_undecodable_named_sequence_fails_closed_without_exception(kind):
    assert len(unicodedata.lookup("KEYCAP DIGIT ONE")) > 1
    plain = _public(kind, chr(92) + "N{KEYCAP DIGIT ONE}")
    text = _enveloped(plain, 4, False)
    assert json.loads(protect(text, KEEP)) == {"log": MARK}


@pytest.mark.parametrize("kind", ["EC", "RSA"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
@pytest.mark.parametrize("escape_name", [False, True])
@pytest.mark.parametrize("note_first", [False, True])
@pytest.mark.parametrize("note", ["C:" + chr(92) + "Users" + chr(92) + "x", chr(92) + "z"])
def test_decoded_public_scalars_keep_their_literal_backslashes(
    kind, depth, escape_name, note_first, note
):
    fields = json.loads(_public(kind, note))
    if note_first:
        fields = {"note": fields.pop("note"), **fields}
    plain = json.dumps(fields)
    if escape_name:
        plain = plain.replace(TYPE, chr(92) + "u006bty")
    assert json.loads(plain)[TYPE] == kind
    text = _enveloped(plain, depth, False)
    untouched(text)
    assert json.loads(redact(text)) == json.loads(text)


@pytest.mark.parametrize("depth", [3, 4, 5, 8, 13, 16])
@pytest.mark.parametrize("name", ["KEYCAP DIGIT ONE", "MISSING UNICODE NAME"])
def test_named_sequence_and_unknown_name_retain_uncertainty(depth, name):
    plain = _public("EC", chr(92) + "N{" + name + "}")
    text = _enveloped(plain, depth, False)
    if depth == 3:  # within the bound the note alone names no member: as main
        return untouched(text)
    assert json.loads(protect(text, KEEP)) == {"log": MARK}


@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5, 8, 13, 16])
@pytest.mark.parametrize("kind", ["EC", "RSA", OCT])
@pytest.mark.parametrize("tail", [chr(92) + "z", chr(92) + "N{KEYCAP DIGIT ONE}"])
def test_private_fields_with_the_same_scalar_notes_remain_private(depth, kind, tail):
    value = secret()
    private_name = "k" if kind == OCT else "d"
    plain = json.dumps({TYPE: kind, "note": tail, private_name: value})
    text = "start\n" + _enveloped(plain, depth, False) + "\nend"
    out = protect(text, value)
    assert jwk_lines(text) == {2}
    assert out.startswith("start\n") and out.endswith("\nend")
    json.loads(out.splitlines()[1])


# Reproduce all 216 expected-private controls from the independent recovery
# probe: eight spellings, three envelope forms, nine depths. Same bytes go to
# every consumer; failed/oversized escapes receive no parser-validity waiver.
PRIVATE_NAMES = {
    "named": chr(92) + "N{LATIN SMALL LETTER K}ty",
    "octal": chr(92) + "153ty",
    "unicode": chr(92) + "u006bty",
    "braced9": chr(92) + "u{00000006b}ty",
    "braced16": chr(92) + "u{" + "0" * 14 + "6b}ty",
    "braced17": chr(92) + "u{" + "0" * 15 + "6b}ty",
    "braced32": chr(92) + "u{" + "0" * 30 + "6b}ty",
    "continued": "k" + chr(92) + "\r\n  ty",
}


@pytest.mark.parametrize("name", list(PRIVATE_NAMES))
@pytest.mark.parametrize("encoding", ["normal", "quotes", "reencoded"])
@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5, 8, 12, 13, 16])
def test_all_independent_private_controls_still_protect(name, encoding, depth):
    value = secret()
    plain = "{'" + PRIVATE_NAMES[name] + "': '" + OCT + "', 'k': " + repr(value) + "}"
    text = (
        _backslash_envelopes(plain, depth)
        if encoding == "reencoded"
        else _enveloped(plain, depth, encoding == "quotes")
    )
    assert _opened(text, depth) == plain
    out = protect(text, value)
    assert json.loads(out) == {"log": MARK}


def _scalar_run(size):
    return "C:" + (chr(92) + "Users" + chr(92) + "x ") * size


def test_terminal_scalar_checks_keep_cpu_and_peak_bounds():
    def scan(text):
        return jwk._undecoded_private(text, scalar=True)

    small, large = _scalar_run(2000), _scalar_run(8000)
    assert scan(large) is None
    assert_cpu_growth(scan, small, large, limit=8, label="terminal-decoded-scalar")
    growth = peak_growth(scan, small, large)
    assert growth.large < 48 * len(large)
    assert growth.ratio < 8


@pytest.mark.parametrize("separator", list("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"))
def test_line_data_requires_decoded_field_provenance(separator):
    value = chr(92) + "z" + separator + KEEP
    assert jwk._undecoded_private(value, scalar=True) is None
    assert jwk._undecoded_private(value) is None  # no member name: not a JWK
    document = value + separator + TYPE + ": " + OCT + separator + "k: " + secret()
    assert jwk._undecoded_private(document, scalar=True) is not None


# --- Quote ownership in raw excerpts --------------------------------------------------------------


def orphan_plain(prefix, value):
    return prefix + '? "' + TYPE + '"\n: ' + OCT + "\nk: " + value


@pytest.mark.parametrize("prefix,depth", [("'\n", 1), ("'\n", 2), ("'\n", 3), ('"\n', 4)])
def test_reported_orphan_quotes_keep_raw_protection(prefix, depth, tmp_path):
    value = secret()
    plain = orphan_plain(prefix, value)
    raw_covered = any(value in plain[m.start : m.end] for m in jwk.private_members(plain))
    assert raw_covered is True
    text = _enveloped(plain, depth, False)
    assert _opened(text, depth) == plain
    text = "before\n" + text + "\nafter\n"
    covered = any(value in text[m.start : m.end] for m in jwk.private_members(text))
    masked = value not in redact(text)
    lines = jwk_lines(text)
    gate = run_gates(text, GateContext(max_chars=len(text) * 2 + 1000))
    (tmp_path / "fixture.log").write_text(text)
    report = run_audit(tmp_path)
    assert report["files_scanned"] == 1 and not report["truncated"]
    engine_lines = {f["line"] for f in report["findings"] if f["rule"] == "SECRETS-KEY"}
    outcome = (
        covered,
        masked,
        lines == {2},
        engine_lines == {2},
        gate.verdict == BLOCK and gate.text is None,
    )
    assert outcome == (True,) * 5


def test_formatted_child_ownership_does_not_build_an_expression_ast():
    def build(size):
        return "note=f'" + "{a}" * size + " " + TYPE + ": " + OCT + "' k: " + KEEP

    small, large = build(500), build(2000)
    assert jwk._undecoded_private(large) is None
    assert_cpu_growth(jwk._undecoded_private, small, large, limit=8, label="formatted-ownership")
    growth = peak_growth(jwk._undecoded_private, small, large)
    assert growth.large < 48 * len(large)
    assert growth.ratio < 8


QUOTE_FORMS = {
    "control": ("", ""),
    "single": ("'", ""),
    "double": ('"', ""),
    "backtick": ("`", ""),
    "triple-single": ("'''", ""),
    "triple-double": ('"""', ""),
    "bytes-single": ("b'", ""),
    "raw-double": ('r"', ""),
    "unicode-single": ("u'", ""),
    "formatted-single": ("f'", ""),
    "template-double": ('t"', ""),
    "raw-bytes": ('rb"', ""),
    "raw-formatted": ("fr'", ""),
    "raw-template": ('tr"', ""),
    "mispaired-single": ("'", '"'),
    "mispaired-double": ('"', "'"),
}


def ambiguous_document(shape, position, newline, value):
    prefix, suffix = QUOTE_FORMS[shape]
    head = '? "' + TYPE + '"' + newline + ": " + OCT + newline
    tail = "k: " + value
    orphan = prefix + newline if prefix else ""
    if position == "before":
        plain = orphan + head + tail
    elif position == "between":
        plain = head + orphan + tail
    else:
        plain = head + tail + (newline + orphan if orphan else "")
    return plain + (newline + suffix if suffix else "")


def private_case(text, value, tmp_path, *, encoded, form):
    members = jwk.private_members(text)
    out = redact(text)
    rules = jwk_lines(text)
    relay = _jwk_findings(text)
    gate = run_gates(text, GateContext(max_chars=len(text) * 2 + 1000))
    expected = {2} if encoded else lines_of(text, value)
    (tmp_path / "fixture.log").write_text(text)
    report = run_audit(tmp_path)
    assert report["files_scanned"] == 1 and not report["truncated"]
    lines = {f["line"] for f in report["findings"] if f["rule"] == "SECRETS-KEY"}
    outcome = (
        any(value in text[m.start : m.end] for m in members),
        value not in out,
        rules == expected if encoded else expected <= rules,
        any(f.action == BLOCK and value in text[f.start : f.end] for f in relay),
        lines == expected if encoded else expected <= lines,
        gate.verdict == BLOCK and gate.text is None,
    )
    assert outcome == (True,) * 6
    if encoded:
        assert out.startswith("before\n") and out.endswith("\nafter\n")
        (json.loads if form == "json" else ast.literal_eval)(out.splitlines()[1])


@pytest.mark.parametrize("shape", list(QUOTE_FORMS))
@pytest.mark.parametrize("position", ["before", "between", "after"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\n\t\n"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize("form", ["json", "repr"])
def test_ambiguous_quotes_cannot_separate_explicit_type_and_private_value(
    shape, position, newline, depth, form, tmp_path
):
    value = secret()
    plain = ambiguous_document(shape, position, newline, value)
    text = plain
    for _ in range(depth):
        text = json.dumps({"log": text}) if form == "json" else repr({"log": text})
    opened = text
    for _ in range(depth):
        opened = (json.loads if form == "json" else ast.literal_eval)(opened)["log"]
    assert opened == plain
    private_case("before\n" + text + "\nafter\n", value, tmp_path, encoded=bool(depth), form=form)


@pytest.mark.parametrize("prefix", ["", "r", "b", "u", "f", "t"])
@pytest.mark.parametrize("style", ["repr", "triple"])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("parent", ["RSA", "EC", "OKP"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
@pytest.mark.parametrize("form", ["json", "repr"])
def test_complete_inert_children_keep_public_parents(prefix, style, before, parent, depth, form):
    body = repr({TYPE: OCT})
    if prefix in ("f", "t"):
        body = body.replace("{", "{{").replace("}", "}}")
    literal = (
        prefix + repr(body) if style == "repr" else prefix + '"""header\n' + body + '\ntail"""'
    )
    args = ["note=" + literal, "k=" + repr(KEEP)]
    if before:
        args.reverse()
    snippet = "dict(" + ", ".join(args) + ")"
    ast.parse(snippet)
    text = json.dumps({TYPE: parent, "note": snippet, "k": KEEP})
    for _ in range(depth):
        text = json.dumps({"log": text}) if form == "json" else repr({"log": text})
    untouched(text)
    gate = run_gates(text, GateContext(max_chars=len(text) * 2 + 1000))
    assert gate.verdict != BLOCK and gate.text == text


QUOTE_REUSE = {
    "single": "f'{'" + TYPE + ": " + OCT + "'}'",
    "double": 'f"{"' + TYPE + ": " + OCT + '"}"',
    "subscript": "f'{value['x']} " + TYPE + ": " + OCT + "'",
    "multiline": "f'{(\n\"" + TYPE + ": " + OCT + "\"\n)}'",
    "nested-raw": "f'{r\"" + TYPE + ": " + OCT + "\"}'",
}


@pytest.mark.parametrize("literal", list(QUOTE_REUSE))
@pytest.mark.parametrize("parent", ["RSA", "EC", "OKP"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_quote_reusing_formatted_children_keep_public_parents(literal, parent, depth):
    # A valid PEP 701 child (quote reuse in a field) owns its own type name.
    snippet = "dict(note=" + QUOTE_REUSE[literal] + ', k="' + KEEP + '")'
    ast.parse(snippet)
    text = json.dumps({TYPE: parent, "note": snippet, "k": KEEP})
    for _ in range(depth):
        text = json.dumps({"log": text})
    text = "before\n" + text + "\nafter\n"
    untouched(text)
    gate = run_gates(text, GateContext(max_chars=len(text) * 2 + 1000))
    assert gate.verdict != BLOCK and gate.text == text


@pytest.mark.xfail(strict=True, reason="residual: invalid formatted literal ownership (#2248)")
def test_an_invalid_formatted_literal_cannot_prove_child_ownership(tmp_path):
    # Invalid f"""}...""" reads as a valid ordinary literal, so it owns k.
    value = secret()
    literal = 'f"""}\nk: ' + value + '\n"""'
    with pytest.raises(SyntaxError):
        ast.parse(literal, mode="eval")
    text = _wrapped('? "' + TYPE + '"\n: ' + OCT + "\n" + literal, 4)
    private_case("before\n" + text + "\nafter\n", value, tmp_path, encoded=True, form="json")


@pytest.mark.parametrize("prefix", ["'", '"', 'r"', "f'"])
@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5])
def test_encoded_nul_in_ambiguous_quote_keeps_the_explicit_key_block(prefix, depth, tmp_path):
    value = secret()
    plain = orphan_plain(prefix + "\x00\n", value)
    # NUL is inside the ambiguous quote prefix, not between the explicit key
    # and its type value. The raw block keeps its actual key/type grammar.
    protect(plain, value)
    text = "before\n" + _enveloped(plain, depth, False) + "\nafter\n"
    private_case(text, value, tmp_path, encoded=True, form="json")


@pytest.mark.parametrize("shape", ["long-orphan", "invalid-closed", "dense-key"])
def test_uncertain_quote_ownership_keeps_growth_and_peak_bounds(shape):
    def build(size):
        if shape == "long-orphan":
            return "'\n" + "padding " * size + orphan_plain("", KEEP)
        if shape == "invalid-closed":
            return '"\n' + "padding " * size + '"\n' + orphan_plain("", KEEP)
        return ('"' + TYPE + '": "' + OCT + '", "label": "public"; ') * size

    def scan(text):
        return jwk._child_strings(text, jwk.oct_blocks._quote_context(text)[0])

    small, large = build(500), build(2000)
    assert_cpu_growth(scan, small, large, limit=8, label=shape)
    growth = peak_growth(scan, small, large)
    assert growth.large < 48 * len(large)
    assert growth.ratio < 8


# --- Terminal scalar provenance -------------------------------------------------------------------

BS = chr(92)


def wrapped(text, depth, form="normal"):
    for _ in range(depth):
        text = repr({"log": text}) if form == "repr" else json.dumps({"log": text})
    return text


def public_text(kind, note, depth, form="normal", escaped=False, note_first=False):
    fields = {
        TYPE: kind,
        "x": "public-component",
        "y": "public-component",
        "n": "public-component",
        "e": "AQAB",
        "note": note,
    }
    if kind == "none":
        fields = {"kind": "public", "note": note}
    if note_first:
        fields = {"note": fields.pop("note"), **fields}
    plain = json.dumps(fields)
    if escaped:
        plain = plain.replace(TYPE, BS + "u006bty")
    return wrapped(plain, depth, form)


def public_observation(text):
    redacted = redact(text)
    gate = run_gates(text, GateContext(max_chars=len(text) * 2 + 1000))
    return [
        bool(jwk.private_members(text)),
        redacted != text,
        any("JWK private member" in f.message for f in check_secrets("fixture.log", text)),
        any(f.action == BLOCK for f in _jwk_findings(text)),
        gate.verdict == BLOCK,
    ]


PUBLIC_NOTES = {
    "windows": "C:" + BS + "Users" + BS + "x",
    "unc": BS * 2 + "server" + BS + "share",
    "unknown": BS + "z",
    "trailing": BS,
    "escaped_n": BS + "n",
    "path_colon": "prefix:" + BS + "z",
    "path_square": "C:" + BS + "Users" + BS + "x[0]",
    "fullwidth": "C：" + BS + "Users",
    "ordinary": "a public note",
    "unicode": "文書" + BS + "z",
    "emoji": "😀" + BS + "z",
    "word": TYPE + BS + "z",
    "slash": "a" + BS + "/b",
    "doubled": BS * 2 + "z",
    "control": "a\tb" + BS + "z",
}


@pytest.mark.parametrize("kind", ["EC", "RSA", "OKP"])
@pytest.mark.parametrize("note", list(PUBLIC_NOTES))
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
@pytest.mark.parametrize("form", ["normal", "repr"])
def test_astra_public_matrix_360(kind, note, depth, form):
    text = public_text(kind, PUBLIC_NOTES[note], depth, form, depth in [2, 3], bool(depth % 2))
    assert public_observation(text) == [False] * 5


BOUNDARY_NOTES = {
    "reports": "C:" + BS + "Users" + BS + "reports",
    "notes": "C:" + BS + "Users" + BS + "notes",
    "control": "C:" + BS + "Users" + BS + "x",
    "square": "C:" + BS + "Users" + BS + "reports[2026]",
}


@pytest.mark.parametrize("kind", ["EC", "RSA", "OKP"])
@pytest.mark.parametrize("note", list(BOUNDARY_NOTES))
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_astra_boundary_matrix_48(kind, note, depth):
    text = public_text(kind, BOUNDARY_NOTES[note], depth)
    assert public_observation(text) == [False] * 5


FIVE_NOTES = {
    **{k: BOUNDARY_NOTES[k] for k in ["reports", "notes", "square"]},
    "apostrophe": "C:" + BS + "Users" + BS + "O'Brien",
    "colon": "status: " + BS + "z",
}


@pytest.mark.parametrize("kind", ["EC", "RSA", "OKP", "none"])
@pytest.mark.parametrize("note", list(FIVE_NOTES))
def test_five_public_forms_at_boundary(kind, note):
    text = public_text(kind, FIVE_NOTES[note], 3)
    assert public_observation("before\n" + text + "\nafter") == [False] * 5


GENERAL_NOTES = {
    **{
        word: "C:" + BS + "Users" + BS + word
        for word in [
            "reports",
            "notes",
            "temp",
            "bin",
            "files",
            "archive",
            "vault",
            "unicode",
            "x86",
            "Named",
        ]
    },
    "double_quote": "C:" + BS + "Users" + BS + 'a"b',
    "single_quote": "C:" + BS + "Users" + BS + "O'Brien",
    "quoted_whole": '"C:' + BS + "Users" + BS + 'reports"',
    "brackets": "C:" + BS + "Users" + BS + "a[0]{year}(draft)",
    "two_drives": "C:" + BS + "Users and D:" + BS + "notes",
}


@pytest.mark.parametrize("kind", ["EC", "RSA", "OKP"])
@pytest.mark.parametrize("note", list(GENERAL_NOTES))
@pytest.mark.parametrize("form", ["normal", "repr"])
@pytest.mark.parametrize("note_first", [False, True])
def test_terminal_data_uses_literal_provenance(kind, note, form, note_first):
    text = public_text(kind, GENERAL_NOTES[note], 3, form, True, note_first)
    assert public_observation(text) == [False] * 5


@pytest.mark.parametrize("separator", list("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"))
@pytest.mark.parametrize("encoded", [False, True])
def test_literal_line_data_and_embedded_private_lines_keep_their_roles(separator, encoded):
    line = BS + "u" + format(ord(separator), "04x") if encoded else separator
    text = public_text("RSA", BS + "z" + line + "public second line", 3)
    assert public_observation(text) == [False] * 5
    value = secret()
    document = BS + "z" + line + TYPE + ": " + OCT + line + "k: " + value
    text = "before\n" + public_text("RSA", document, 3) + "\nafter"
    protect(text, value)
    assert jwk_lines(text) == {2}
    gate = run_gates(text, GateContext(max_chars=len(text) * 2 + 1000))
    assert gate.verdict == BLOCK and gate.text is None


def hostile_documents(value):
    plain = json.dumps({TYPE: OCT, "k": value})
    return {
        "json": plain,
        "explicit": '? "' + TYPE + '"\n: ' + OCT + "\nk: " + value,
        "crlf": '? "k' + BS + '\r\n ty"\r\n: ' + OCT + "\r\nk: " + value,
        "unicode": plain.replace(TYPE, BS + "u006bty"),
        "named": _round4_python(ROUND4_NAMES["named"], value),
        "octal": _round4_python(ROUND4_NAMES["octal"], value),
        "braced9": _braced_leading_zeros(value),
        **{
            "braced" + str(n): "{"
            + BS
            + "u{"
            + "0" * (n - 2)
            + "6b}ty: "
            + json.dumps(OCT)
            + ", k: "
            + json.dumps(value)
            + "}"
            for n in [16, 17, 32]
        },
        "unknown_name": json.dumps({TYPE: OCT, "k": value, "note": BS + "N{MISSING NAME}"}),
        "named_sequence": json.dumps({TYPE: OCT, "k": value, "note": BS + "N{KEYCAP DIGIT ONE}"}),
        "bad_codepoint": json.dumps({TYPE: OCT, "k": value, "note": BS + "U00110000"}),
        "unknown_escape": BS + "z\n" + TYPE + ": " + OCT + "\nk: " + value,
        "unclosed": plain[:-2] + BS,
        "malformed_tab": '? "' + TYPE + '"\t# comment\n: ' + OCT + "\nk: " + value,
    }


@pytest.mark.parametrize("shape", list(hostile_documents("runtime-placeholder")))
@pytest.mark.parametrize("depth", [3, 4, 8, 13, 16])
@pytest.mark.parametrize("public_parent", [False, True])
def test_hostile_documents_do_not_inherit_terminal_scalar_permission(shape, depth, public_parent):
    value = secret()
    plain = hostile_documents(value)[shape]
    if public_parent:
        plain = json.dumps({"note": plain, TYPE: "RSA", "n": "public", "e": "AQAB"})
    text = "before\n" + _backslash_envelopes(plain, depth) + "\nafter"
    out = protect(text, value)
    assert jwk_lines(text) == {2}
    json.loads(out.splitlines()[1])
    gate = run_gates(text, GateContext(max_chars=len(text) * 2 + 1000))
    assert gate.verdict == BLOCK and gate.text is None


@pytest.mark.parametrize("shape", ["long-name", "quoted-data", "public-log", "dense-unknown"])
def test_scalar_provenance_work_stays_within_existing_growth_and_peak_bounds(shape):
    def data(size):
        if shape == "long-name":
            return "a" * size + BS + "z"
        if shape == "dense-unknown":
            return (BS + "z") * size
        return ('literal [0] "quoted" ' + BS + "reports ") * size

    def build(size):
        value = data(size)
        return public_text("RSA", value, 3) if shape == "public-log" else value

    # A measured callable that reads a test parameter cannot be cache-scanned.
    if shape == "public-log":
        scan = jwk.private_members
    else:

        def scan(text):
            return jwk._undecoded_private(text, scalar=True)

    small, large = build(1000), build(4000)
    assert not scan(large)
    assert_cpu_growth(scan, small, large, limit=8, label=shape)
    growth = peak_growth(scan, small, large)
    assert growth.large < 48 * len(large)
    assert growth.ratio < 8
