"""A run of properties shared by many kty triggers is walked once (task #2203).

``kty: !<kty: !<...`` with a blank after each separator: every ``!<`` is an
unfinished URI (the blank ends it), so the property rule takes ``!<kty:`` and
the walk goes on to the next one. Each trigger's value scan used to walk that
run to the end of the text, so n triggers cost n squared. ``kty: !kty: ...``
is the same run built from plain tag properties, with no URI anywhere.

The memoized walk must keep the reference regex's answer at every position,
so oct-block output stays byte for byte what it was.
"""

from __future__ import annotations

import json
import random

import pytest
from growth_budget import assert_cpu_growth, cold, peak_growth

from unified_agent.telemetry import jwk, oct_blocks
from unified_agent.telemetry.redaction import redact_sensitive_content

TYPE = "kt" + "y"
OCT = "o" + "ct"


@pytest.fixture
def bootstrap_test_store():
    return False


def redact(text):
    return redact_sensitive_content(text, max_len=len(text) * 20 + 1000)


GROWING = {
    "spaced-unfinished-uri-triggers": lambda n: (TYPE + ": !<") * n,
    "braced-spaced-unfinished-uri-triggers": lambda n: "{" + (TYPE + ": !<") * n,
    "spaced-tag-property-triggers": lambda n: (TYPE + ": !") * n,
    "spaced-anchor-property-triggers": lambda n: (TYPE + ": &") * n,
    "tag-property-triggers-then-uri": lambda n: (TYPE + ": !") * n + "!<",
    "spaced-property-run-ending-oct": lambda n: (TYPE + ": !<") * n + " " + OCT + ", k=value",
}


@pytest.mark.parametrize("name", list(GROWING))
def test_shared_property_runs_grow_linearly(name):
    small, large = GROWING[name](1_000), GROWING[name](4_000)
    assert len(small) >= 1_000 and len(large) >= 3.5 * len(small)
    # CPU time, best of interleaved samples (task #2188); never a wall-clock bound.
    growth = assert_cpu_growth(cold(redact), small, large, limit=8, label=name)
    print(json.dumps({"case": name, "ratio": growth.ratio, "large_cpu": growth.large}))


@pytest.mark.parametrize("name", list(GROWING))
def test_shared_property_run_peak_follows_the_cost_model(name):
    small, large = GROWING[name](1_000), GROWING[name](4_000)
    # The unchanged constants from test_redaction_oct_blocks.py.
    growth = peak_growth(
        cold(jwk.private_members),
        small,
        large,
        limit=8,
        budget=lambda found: 48 * len(large) + 400 * len(found),
    )
    budget = 48 * len(large) + 400 * len(growth.result)
    assert growth.large < budget, f"{name}: peak {growth.large} exceeds budget {budget}"
    assert growth.ratio < 8, f"{name}: x4 input took x{growth.ratio:.2f} peak"


def _assert_heads_match_reference(text, rng):
    heads = oct_blocks._ValueHeads(text)
    positions = {0, len(text)}
    positions.update(end for _, end in oct_blocks._kty_candidates(text))
    positions.update(i for i, char in enumerate(text) if char in "!&<: \n")
    # Out-of-order queries, as lookahead makes them: a memoized tail must hold
    # for a later caller that starts before, after or inside an earlier walk.
    positions = list(positions)
    rng.shuffle(positions)
    for position in positions:
        original = oct_blocks._VALUE_HEAD.match(text, position)
        indexed = heads.match(position)
        assert (indexed.lastgroup, indexed.end()) == (original.lastgroup, original.end()), (
            text,
            position,
        )
        if original.lastgroup is not None:
            assert indexed.start(original.lastgroup) == original.start(original.lastgroup)


@pytest.mark.parametrize("name", list(GROWING))
def test_memoized_walk_keeps_the_reference_answer_on_the_growth_shapes(name):
    rng = random.Random(2203)
    for n in (1, 2, 3, 7, 40):
        _assert_heads_match_reference(GROWING[name](n), rng)


@pytest.mark.parametrize("with_uri", [False, True])
def test_memoized_walk_keeps_the_reference_answer_on_random_runs(with_uri):
    rng = random.Random(22030 + with_uri)
    pieces = [
        TYPE + ": ",
        TYPE + ":",
        "!",
        "&",
        "!t ",
        "&a ",
        "!!str ",
        " ",
        "\t",
        "\n",
        "'",
        '"',
        "[",
        "{",
        "b'",
        "\\n",
        "- ",
        "|+ ",
        OCT,
        "RSA",
        "*alias",
        "# c",
        "p" * 260,
    ]
    if with_uri:
        pieces += ["!<", ">", "!<tag:x>"]
    for _ in range(1500):
        _assert_heads_match_reference("".join(rng.choices(pieces, k=24)), rng)


@pytest.mark.parametrize("shape", [TYPE + ": !<", TYPE + ": !"])
def test_shared_run_ending_in_oct_still_masks_its_neighbours(shape):
    private = "q" + "5f3a9c" * 3 + "z"
    text = shape * 50 + " " + OCT + ", k=" + private
    assert private not in redact(text)
