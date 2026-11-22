import base64
import json

import pytest

from unified_agent.audit.rules import check_secrets


def test_marked_stand_in_values_are_not_findings():
    # Repro snippets in agent notes name their key "<runtime fake>" (task #2213).
    text = 'x = dict(kty="oct", k="<runtime fake>")\n'  # ua:allow
    assert list(check_secrets("notes.py", text)) == []


def test_real_looking_symmetric_key_is_still_a_finding():
    text = 'x = dict(kty="oct", k="q3VzX0tleV9yZWFsXzMyYnl0ZXNfa2V5X3ZhbHVl")\n'  # ua:allow
    findings = list(check_secrets("config.py", text))
    assert findings and findings[0].rule == "SECRETS-KEY"


@pytest.mark.parametrize(
    "value",
    [
        "<runtime fake>",
        "<example key>",
        "<dummy key>",
        "<a placeholder value>",
        "<FAKE KEY>",
        "<a synthetic example key>",
    ],
)
def test_complete_marked_labels_are_placeholders(value):
    text = json.dumps({"kty": "oct", "k": value})  # ua:allow - runtime-only test fixture
    assert list(check_secrets("notes.py", text)) == []


@pytest.mark.parametrize(
    "value",
    [
        "prefix <runtime fake>",
        "<runtime fake> suffix",
        "<runtime faker>",
        "<runtime fake><other material>",
        "<<runtime fake>>",
        "<runtime fake\rmaterial>",
    ],
)
def test_marker_inside_other_material_does_not_suppress_a_finding(value):
    text = json.dumps({"kty": "oct", "k": value})  # ua:allow - runtime-only test fixture
    findings = list(check_secrets("config.py", text))
    assert findings and all(item.severity == "high" for item in findings)


def test_key_shaped_value_containing_marker_word_stays_high():
    value = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=") + "dummy"
    # ua:allow - runtime-only test fixture
    text = json.dumps({"kty": "oct", "k": value})  # ua:allow
    findings = list(check_secrets("config.py", text))
    assert findings and findings[0].severity == "high"


@pytest.mark.parametrize("marker", ["fake", "dummy", "placeholder", "example"])
@pytest.mark.parametrize("marker_first", [True, False], ids=["before-key", "after-key"])
def test_label_wrapping_key_material_stays_high(marker, marker_first):
    material = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    words = (marker, material) if marker_first else (material, marker)
    value = "<" + " ".join(words) + ">"
    text = json.dumps({"kty": "oct", "k": value})  # ua:allow - runtime-only fixture
    findings = list(check_secrets("fixture.json", text))
    assert findings and all(item.severity == "high" for item in findings)


@pytest.mark.parametrize("kind", ["RSA", "EC"])
def test_public_members_do_not_become_secret_findings(kind):
    value = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    public = {
        "kty": kind,
        **({"n": value, "e": "AQAB"} if kind == "RSA" else {"x": value, "y": value}),
    }
    assert list(check_secrets("public.json", json.dumps(public))) == []


@pytest.mark.parametrize("form", ["kwargs", "yaml", "unfinished-json"])
@pytest.mark.parametrize("boundary", list("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029<>"))
def test_escaped_label_boundary_does_not_suppress_a_finding(form, boundary):
    value = "<runtime fake" + boundary + "material>"
    literal = json.dumps(value)
    if boundary in "<>":
        escaped = chr(92) + f"u{ord(boundary):04x}"
        literal = literal.replace(boundary + "material", escaped + "material")
    if form == "kwargs":
        text = 'x = dict(kty="oct", k=' + literal + ")"  # ua:allow - runtime-only fixture
    elif form == "yaml":
        text = "kty: oct\nk: " + literal + "\n"  # ua:allow - runtime-only fixture
    else:
        text = '{"kty":"oct","k":' + literal[:-1]  # ua:allow - unfinished runtime fixture
    findings = list(check_secrets("fixture.txt", text))
    assert findings and all(item.severity == "high" for item in findings)
