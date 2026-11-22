"""The load-robust growth helpers still fail a superlinear scan (task #2188).

``growth_budget`` exists so runner load cannot fail a linear scan. These tests
hold the other half of the bargain: a scan that really is quadratic, in time or
in memory, still fails every check that the redaction tests now use.
"""

from __future__ import annotations

import functools
import re
import sys
import types
from collections import OrderedDict, deque
from pathlib import Path

import growth_budget
import pytest
from growth_budget import (
    NOT_LAST_ARGUMENT_CACHES,
    assert_cpu_growth,
    cold,
    cpu_growth,
    module_state_writes,
    peak_bytes,
    peak_growth,
)

# The scanner's classic quadratic shape: an unanchored pattern that rescans to
# the end of the text from every start position and then fails.
_RESCAN = re.compile(r"a*b")
# The same search, allowed to start only at the beginning of a run: linear.
_ONE_PASS = re.compile(r"(?<!a)a*b")


@pytest.fixture
def bootstrap_test_store():
    return False


def _quadratic(text: str) -> object:
    return _RESCAN.search(text)


def _linear(text: str) -> object:
    return _ONE_PASS.search(text)


def test_a_quadratic_scan_fails_the_cpu_growth_check():
    small, large = "a" * 2_000, "a" * 8_000
    growth = cpu_growth(_quadratic, small, large, limit=8)
    # Every round was over the limit, so every round ran.
    assert len(growth.rounds) == 3 and min(growth.rounds) >= 8, growth
    with pytest.raises(AssertionError, match="x4.0 input took"):
        assert_cpu_growth(_quadratic, small, large, limit=8, label="rescan")


def test_a_linear_scan_passes_the_cpu_growth_check():
    growth = assert_cpu_growth(_linear, "a" * 20_000, "a" * 80_000, limit=8, label="one-pass")
    assert growth.ratio < 8
    # On a slow CPU one call can already fill a sample, so only the floor is
    # asserted here; the loop-growth rule is pinned deterministically below.
    assert growth.loops >= 1


def _fake_cpu_seconds(per_call: float):
    """A ``_cpu_seconds`` stand-in charging ``per_call`` seconds per loop, never timing."""
    seen: list[int] = []

    def fake(run, arg, loops):
        seen.append(loops)
        return per_call * loops

    return fake, seen


@pytest.mark.parametrize(
    ("per_call", "expected_loops"),
    [
        # 16 loops cost 1.6 ms (< 2 ms), so the warm-up quadruples once more.
        (0.0001, 64),
        # One call already fills a sample: no repetition.
        (0.01, 1),
        # A call too fast to register is capped rather than looping forever.
        (0.0, growth_budget._MAX_LOOPS),
    ],
)
def test_cpu_growth_picks_the_loop_count_from_the_sample_floor(
    monkeypatch, per_call, expected_loops
):
    fake, seen = _fake_cpu_seconds(per_call)
    monkeypatch.setattr(growth_budget, "_cpu_seconds", fake)
    growth = cpu_growth(_linear, "a", "aaaa", limit=8)
    assert growth.loops == expected_loops
    # The warm-up quadrupled from 1 and every timed sample used the chosen count.
    warmup = seen[: seen.index(expected_loops) + 1]
    assert warmup == [4**i for i in range(len(warmup))]
    assert set(seen[len(warmup) :]) == {expected_loops}


def test_peak_growth_fails_a_quadratic_allocation():
    def suffixes(text: str) -> list[str]:
        return [text[i:] for i in range(len(text))]

    growth = peak_growth(suffixes, "a" * 500, "a" * 2_000, limit=8)
    assert growth.ratio >= 8, growth
    linear = peak_growth(lambda text: [ord(c) for c in text], "a" * 500, "a" * 2_000, limit=8)
    assert linear.ratio < 8, linear


def test_peak_bytes_measures_again_only_while_over_budget():
    calls = []

    def first_call_spikes(text: str) -> int:
        calls.append(text)
        if len(calls) == 1:
            spike = bytearray(1_000_000)
            del spike
        return len(text)

    peak, result = peak_bytes(first_call_spikes, "x" * 10, budget=lambda _: 100_000)
    assert result == 10 and peak < 100_000 and len(calls) == 2

    calls.clear()
    peak, _ = peak_bytes(lambda text: bytes(1_000_000), "x", budget=lambda _: 100_000)
    assert peak >= 1_000_000, "a steady overrun is still reported"


# --- cold(): last-argument caches (task #2218) ---------------------------------------


def _lexer_builds(monkeypatch) -> list[int]:
    from unified_agent.telemetry import yaml_jwk

    built: list[int] = []
    original = yaml_jwk._YamlLex.__init__

    def counting(self, text):
        built.append(id(text))
        original(self, text)

    monkeypatch.setattr(yaml_jwk._YamlLex, "__init__", counting)
    monkeypatch.setattr(yaml_jwk, "_LEX_CACHE", None)
    return built


def test_cold_rebuilds_the_yaml_lexer_on_every_call(monkeypatch):
    from unified_agent.telemetry import jwk

    built = _lexer_builds(monkeypatch)
    # A divergent typed flow mapping: the shape that flaked at x8.26 in CI.
    text = "jwk: {" + "kt" + "y: " + "R" + "SA, a: x`c, " + "d: x " * 10 + "}\n"
    for _ in range(3):
        jwk.private_members(text)
    assert built.count(id(text)) == 1, "the redactor caches the last text's lexer"
    measured = cold(jwk.private_members)
    for _ in range(3):
        measured(text)
    assert built.count(id(text)) == 4, "cold() pays the lexer on every call"


def test_cold_refuses_a_cache_that_no_longer_exists(monkeypatch):
    monkeypatch.setattr(
        growth_budget,
        "LAST_ARGUMENT_CACHES",
        (("unified_agent.telemetry.yaml_jwk", "_RENAMED_CACHE"),),
    )
    with pytest.raises(AttributeError, match="LAST_ARGUMENT_CACHES"):
        cold(len)


_ROOT = Path(__file__).resolve().parents[1]


def _scan():
    return growth_budget.growth_cache_scan(_ROOT / "tests", _ROOT / "src")


def test_every_module_level_cache_a_growth_test_reaches_is_cleared_by_cold():
    # Module-level state that a function writes (a rebound global, a container
    # filled in place, an lru cache, an import-time closure) is how a cross-call
    # cache looks; every one the growth tests can reach must be registered so
    # cold() clears it, or allow-listed with the reason it is not a cache.
    scan = _scan()
    assert not scan.unresolved, scan.unresolved
    assert scan.findings, "the scan found no module state; the path or the cache moved"
    registered = set(growth_budget.LAST_ARGUMENT_CACHES) | set(NOT_LAST_ARGUMENT_CACHES)
    assert scan.findings <= registered, sorted(scan.findings - registered)
    stale = set(NOT_LAST_ARGUMENT_CACHES) - scan.findings
    assert not stale, f"allow-listed state is no longer written: {sorted(stale)}"


GUARD = test_every_module_level_cache_a_growth_test_reaches_is_cleared_by_cold

_REVIEW_REBIND = """
_REVIEW_CACHE = None
def _review_cache(text):
    global _REVIEW_CACHE
    _REVIEW_CACHE = text
    return _REVIEW_CACHE
"""
# A single-entry cache filled in place: no ``global`` statement is needed.
_REVIEW_MUTATE = """
_REVIEW_CACHE = {}
def _review_cache(text):
    if text not in _REVIEW_CACHE:
        _REVIEW_CACHE.clear()
        _REVIEW_CACHE[text] = len(text)
    return _REVIEW_CACHE[text]
"""
# Review round 2: a memoising closure built once at import time.
_REVIEW_CLOSURE = """
def _review_make_cached(run):
    cache = {}
    def cached(text):
        if text not in cache:
            cache.clear()
            cache[text] = run(text)
        return cache[text]
    return cached
_lex_for = _review_make_cached(_lex_for)
"""
_REVIEW_GLOBALS = """
_REVIEW_CACHE = {}
_review_uncached = _lex_for
def _review_cached(text):
    cache = globals()["_REVIEW_CACHE"]
    if text not in cache:
        cache.clear()
        cache[text] = _review_uncached(text)
    return cache[text]
_lex_for = _review_cached
"""
_REVIEW_GETATTR = """
import types as _review_types
_REVIEW_STATE = _review_types.SimpleNamespace(cache={})
_review_uncached = _lex_for
def _review_cached(text):
    cache = getattr(_REVIEW_STATE, "cache")
    if text not in cache:
        cache.clear()
        cache[text] = _review_uncached(text)
    return cache[text]
_lex_for = _review_cached
"""
_REVIEW_AUDIT = """
_REVIEW_AUDIT_CACHE = {}
_review_check_secrets = check_secrets
def check_secrets(path, text):
    key = (path, text)
    if key not in _REVIEW_AUDIT_CACHE:
        _REVIEW_AUDIT_CACHE.clear()
        _REVIEW_AUDIT_CACHE[key] = tuple(_review_check_secrets(path, text))
    yield from _REVIEW_AUDIT_CACHE[key]
"""


def _inject(monkeypatch, injected, suffix="telemetry/yaml_jwk.py"):
    """Append ``injected`` to one source file as every scan reads it."""
    original = Path.read_text
    seen = []

    def read(path, *args, **kwargs):
        contents = original(path, *args, **kwargs)
        seen.append(path.as_posix())
        return contents + "\n" + injected if path.as_posix().endswith(suffix) else contents

    monkeypatch.setattr(Path, "read_text", read)
    return seen


def _source_id(value: str) -> str:
    return " ".join(value.split()) if "\n" in value else value


@pytest.mark.parametrize(
    ("injected", "name"),
    [
        (_REVIEW_REBIND, "_REVIEW_CACHE"),
        (_REVIEW_MUTATE, "_REVIEW_CACHE"),
        (_REVIEW_CLOSURE, "_lex_for"),
        (_REVIEW_GLOBALS, "_REVIEW_CACHE"),
        (_REVIEW_GETATTR, "_REVIEW_STATE.cache"),
    ],
    ids=["rebind", "mutate", "closure", "globals", "getattr"],
)
def test_the_cache_guard_fails_on_an_unregistered_cache(monkeypatch, injected, name):
    _inject(monkeypatch, injected)
    with pytest.raises(AssertionError, match=re.escape(name)):
        GUARD()


def test_the_cache_guard_scans_the_audit_module_a_growth_test_measures(monkeypatch):
    seen = _inject(monkeypatch, _REVIEW_AUDIT, "audit/rules.py")
    with pytest.raises(AssertionError, match="_REVIEW_AUDIT_CACHE"):
        GUARD()
    assert any(path.endswith("audit/rules.py") for path in seen)


_FLAGGED = [
    (_REVIEW_REBIND, "_REVIEW_CACHE"),
    (_REVIEW_MUTATE, "_REVIEW_CACHE"),
    ("_C = {}\ndef f(t):\n    _C[t] = 1\n", "_C"),
    ("_C = {}\ndef f(t):\n    _C[t][0] = 1\n", "_C"),
    ("_C = {}\ndef f(t):\n    _C[t] += 1\n", "_C"),
    ("_C = {}\ndef f(t):\n    del _C[t]\n", "_C"),
    ("_C = {}\ndef f(t):\n    _C.clear()\n", "_C"),
    ("_C = {}\ndef f(t):\n    _C.update({t: 1})\n", "_C"),
    ("_C = {}\ndef f(t):\n    return _C.setdefault(t, 1)\n", "_C"),
    ("_C = {}\ndef f(t):\n    _C.pop(t, None)\n", "_C"),
    ("_C = []\ndef f(t):\n    _C.append(t)\n", "_C"),
    ("_C = set()\ndef f(t):\n    _C.add(t)\n", "_C"),
    ("_C = OrderedDict()\ndef f(t):\n    _C.move_to_end(t)\n", "_C"),
    ("_C = deque()\ndef f(t):\n    _C.appendleft(t)\n", "_C"),
    ("_C = []\ndef f(t):\n    heapq.heappush(_C, t)\n", "_C"),
    ("import weakref\n_C = weakref.WeakValueDictionary()\ndef f(t, v):\n    _C[t] = v\n", "_C"),
    # Through a local alias, and through another module's attribute.
    ("_C = {}\ndef f(t):\n    cache = _C\n    cache.clear()\n", "_C"),
    ("_C = {}\ndef f(t):\n    cache: dict = _C\n    cache[t] = 1\n", "_C"),
    ("_C = {}\ndef f(t):\n    _C.setdefault(t, []).append(1)\n", "_C"),
    ("def f(t):\n    other._CACHE = t\n", "other._CACHE"),
    ("def f(t):\n    other._CACHE[t] = 1\n", "other._CACHE"),
    ("def f(t):\n    setattr(other, '_CACHE', t)\n", "other._CACHE"),
    (
        "def f(t):\n    from unified_agent.telemetry import yaml_jwk\n    yaml_jwk._LEX_CACHE = t\n",
        "yaml_jwk._LEX_CACHE",
    ),
    # Through globals(), vars(), sys.modules and getattr().
    ("_C = {}\ndef f(t):\n    globals()['_C'][t] = 1\n", "_C"),
    ("def f(t):\n    globals()['_C'] = t\n", "_C"),
    ("def f(t, name):\n    globals()[name] = t\n", growth_budget.DYNAMIC),
    ("def f(t):\n    globals().update(c=t)\n", growth_budget.DYNAMIC),
    ("import sys\n_C = {}\ndef f(t):\n    sys.modules[__name__]._C[t] = 1\n", "_C"),
    ("import sys\n_C = {}\ndef f(t):\n    vars(sys.modules[__name__])['_C'][t] = 1\n", "_C"),
    ("import sys\n_C = {}\ndef f(t):\n    getattr(sys.modules[__name__], '_C')[t] = 1\n", "_C"),
    ("import sys\ndef f(t):\n    setattr(sys.modules[__name__], '_C', t)\n", "_C"),
    ("_S = object()\ndef f(t, name):\n    getattr(_S, name)[t] = 1\n", "_S"),
    # From a nested function, a lambda, a comprehension, a method and a helper.
    ("def f():\n    def g(t):\n        _C.append(t)\n    return g\n", "_C"),
    ("_F = lambda t: _C.append(t)\n", "_C"),
    ("def f(ts):\n    return [_C.add(t) for t in ts]\n", "_C"),
    ("class K:\n    def m(self, t):\n        _C[t] = self\n", "_C"),
    ("_C = {}\ndef store(cache, t):\n    cache[t] = 1\ndef f(t):\n    store(_C, t)\n", "_C"),
    (
        "_C = {}\ndef f(t):\n    store(_C, t)\ndef store(cache, t):\n    put(cache, t)\n"
        "def put(into, t):\n    into[t] = 1\n",
        "_C",
    ),
    # lru caches, decorated, wrapped or imported under another name.
    ("@functools.lru_cache(maxsize=1)\ndef f(t):\n    return t\n", "f"),
    ("@lru_cache\ndef f(t):\n    return t\n", "f"),
    ("@functools.cache\ndef f(t):\n    return t\n", "f"),
    ("from functools import lru_cache as memo\n@memo(maxsize=1)\ndef f(t):\n    return t\n", "f"),
    ("import functools as ft\n@ft.cache\ndef f(t):\n    return t\n", "f"),
    ("class K:\n    @functools.lru_cache\n    def m(self, t):\n        return t\n", "K.m"),
    ("f = functools.lru_cache(maxsize=1)(g)\n", "f"),
    ("class K:\n    m = functools.cache(g)\n", "K.m"),
    # Closures created by a call made at import time.
    (
        "def make():\n    cache = {}\n    def f(t):\n        cache[t] = 1\n    return f\nf = make()\n",
        "f",
    ),
    (
        "def make():\n    last = None\n    def f(t):\n        nonlocal last\n        last = t\n"
        "    return f\nf = make()\n",
        "f",
    ),
    (
        "def memoize(run):\n    seen = {}\n    def f(t):\n        seen[t] = run(t)\n        "
        "return seen[t]\n    return f\n@memoize\ndef g(t):\n    return t\n",
        "g",
    ),
    (
        "def make():\n    @functools.cache\n    def g(t):\n        return t\n    return g\n"
        "_G = make()\n",
        "_G",
    ),
    ("def memo(f):\n    return functools.lru_cache(maxsize=1)(f)\n_G = memo(len)\n", "_G"),
    (
        "def make():\n    cache = {}\n    def f(t):\n        cache[t] = 1\n    return f\n"
        "def wrap():\n    return make()\n_G = wrap()\n",
        "_G",
    ),
    # Class state written through cls, type(self) or self.__class__.
    (
        "class K:\n    cache = {}\n    @classmethod\n    def f(cls, t, v):\n        cls.cache[t] = v\n",
        "K.cache",
    ),
    (
        "import weakref\nclass K:\n    cache = weakref.WeakValueDictionary()\n    @classmethod\n"
        "    def f(cls, t, v):\n        cls.cache[t] = v\n",
        "K.cache",
    ),
    ("class K:\n    cache = {}\n    def f(self, t):\n        type(self).cache[t] = 1\n", "K.cache"),
    (
        "class K:\n    seen = []\n    def f(self, t):\n        self.__class__.seen.append(t)\n",
        "K.seen",
    ),
    ("class K:\n    shared = []\n    def f(self, t):\n        self.shared.append(t)\n", "K.shared"),
    # State held by a module-level instance across calls.
    (
        "from functools import cached_property\nclass K:\n    @cached_property\n    def cache(self):\n"
        "        return {}\n    def f(self, t):\n        self.cache[t] = 1\n_HOLDER = K()\n",
        "_HOLDER.cache",
    ),
    (
        "class K:\n    def __init__(self):\n        self.last = None\n    def f(self, t):\n"
        "        self.last = t\n_K = K()\n",
        "_K.last",
    ),
]


@pytest.mark.parametrize(("source", "expected"), _FLAGGED, ids=_source_id)
def test_the_cache_scan_flags_module_state_written_from_a_function(source, expected):
    assert expected in module_state_writes(source)


@pytest.mark.parametrize(
    "source",
    [
        # Local state, however it is named, is not a cross-call cache.
        "_C = {}\ndef f():\n    _C = {}\n    _C.clear()\n    _C['a'] = 1\n",
        "def f(_C):\n    _C.append(1)\n",
        "def f(self):\n    self.items.append(1)\n    self.cache[1] = 2\n",
        "def f(ts):\n    return [_C.append(1) for _C in ts]\n",
        "def f(t):\n    cache = {}\n    cache[t] = 1\n    return cache\n",
        "_C = {}\ndef f():\n    cache = _C\n    cache = {}\n    cache.clear()\n",
        "def f(t):\n    vars()['x'] = t\n",
        # Per-call closures, caches and classes: rebuilt on every call.
        "def f():\n    out = []\n    def g(x):\n        out.append(x)\n    g(1)\n    return out\n",
        "def make():\n    cache = {}\n    def f(t):\n        cache[t] = 1\n    return f\n",
        "def f(t):\n    @functools.cache\n    def g(x):\n        return x\n    return g(t)\n",
        "def f(t):\n    class K:\n        cache = {}\n    K.cache[t] = 1\n    return K.cache\n",
        "def f(ts):\n    return [x for x in ts if (seen := x)]\n",
        # Built at import time, or only read afterwards.
        "_C = []\n_C.append(1)\n_C[0] = 2\n",
        "_TABLE = {}\n_SETUP = [_TABLE.setdefault(k, k) for k in range(3)]\n",
        "def f(t):\n    return _TABLE.get(t), _TABLE[t], _RE.search(t)\n",
        "_TABLE = {}\ndef f(t):\n    global _TABLE\n    return _TABLE.get(t)\n",
        "_RE = re.compile('a')\ndef f(t):\n    parts = _RE.findall(t)\n    parts.append(1)\n",
        "class K:\n    seen = {}\n",
        # Instance state: per instance unless the instance is module-level.
        "class K:\n    def __init__(self):\n        self.items = []\n    def f(self, t):\n"
        "        self.items.append(t)\n",
        "class K:\n    def __init__(self):\n        self.items = []\n_K = K()\n",
        "@dataclass\nclass K:\n    items: list = field(default_factory=list)\n    def f(self, t):\n"
        "        self.items.append(t)\n",
    ],
    ids=_source_id,
)
def test_the_cache_scan_ignores_local_and_read_only_state(source):
    assert module_state_writes(source) == set()


def test_the_cache_scan_names_a_cache_in_the_module_that_owns_it():
    source = "from . import yaml_jwk\ndef f(t):\n    yaml_jwk._LEX_CACHE = t\n"
    findings = growth_budget.module_state_findings(
        "unified_agent.telemetry.jwk",
        source,
        is_module=lambda name: name == "unified_agent.telemetry.yaml_jwk",
    )
    assert findings == {("unified_agent.telemetry.yaml_jwk", "_LEX_CACHE")}


# --- which modules the growth tests reach ---------------------------------------------


def test_the_scan_follows_what_each_growth_test_measures():
    scan = _scan()
    measured = {
        "unified_agent.audit.rules",  # test_public_relay: cold(scan) over check_secrets
        "unified_agent.telemetry.jwk",
        "unified_agent.telemetry.oct_blocks",
        "unified_agent.telemetry.redaction",
        "unified_agent.telemetry.yaml_jwk",
        "unified_agent.tools.worktree_policy",  # test_worktree_policy: _ps_process_paths
    }
    assert measured <= scan.modules, sorted(measured - scan.modules)


def _growth_tests(tmp_path, body: str) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_measured.py").write_text(body, encoding="utf-8")
    return tests


def test_a_growth_test_measuring_another_module_widens_the_scan(tmp_path):
    tests = _growth_tests(
        tmp_path,
        "from growth_budget import cold, cpu_growth\n"
        "from unified_agent import vector_math\n\n"
        "def test_grows():\n"
        "    def run(text):\n"
        "        return vector_math.cosine_similarity_bytes(text.encode(), text.encode())\n"
        "    cpu_growth(cold(run), 'a', 'aaaa')\n",
    )
    scan = growth_budget.growth_cache_scan(tests, _ROOT / "src")
    assert "unified_agent.vector_math" in scan.modules
    # A module outside the telemetry code with a cache of its own now fails the guard.
    assert ("unified_agent.vector_math", "_query_vec_cache") in scan.findings


def test_a_growth_helper_imported_under_another_name_is_still_followed(tmp_path):
    """#2228: a helper imported under an alias still has its callable followed."""
    tests = _growth_tests(
        tmp_path,
        "from growth_budget import cold, cpu_growth as measure\n"
        "from unified_agent import vector_math\n\n"
        "def test_grows():\n"
        "    def run(text):\n"
        "        return vector_math.cosine_similarity_bytes(text.encode(), text.encode())\n"
        "    measure(cold(run), 'a', 'aaaa')\n",
    )
    scan = growth_budget.growth_cache_scan(tests, _ROOT / "src")
    assert "unified_agent.vector_math" in scan.modules


def test_a_growth_test_measuring_a_parameter_cannot_be_followed(tmp_path):
    tests = _growth_tests(
        tmp_path,
        "import pytest\nfrom growth_budget import cold, cpu_growth\n\n"
        "@pytest.mark.parametrize('run', [len])\n"
        "def test_grows(run):\n"
        "    cpu_growth(cold(run), 'a', 'aaaa')\n",
    )
    scan = growth_budget.growth_cache_scan(tests, _ROOT / "src")
    assert scan.unresolved and "test_measured.py:6" in scan.unresolved[0]


# --- cold() clears each cache the way its code uses it ------------------------------


@pytest.fixture
def fake_caches(monkeypatch):
    """A throwaway module whose caches are registered for cold()."""
    module = types.ModuleType("fake_growth_caches")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def register(registry=None, **caches):
        for name, value in caches.items():
            setattr(module, name, value)
        names = caches if registry is None else registry
        monkeypatch.setattr(
            growth_budget, "LAST_ARGUMENT_CACHES", tuple((module.__name__, n) for n in names)
        )
        return module

    return register


@pytest.mark.parametrize(
    "container",
    [{"warm": 1}, ["warm"], {"warm"}, OrderedDict(warm=1), deque(["warm"])],
    ids=["dict", "list", "set", "ordereddict", "deque"],
)
def test_cold_empties_a_container_cache_in_place(fake_caches, container):
    module = fake_caches(_CACHE=container)
    seen = []
    cold(lambda _: seen.append((module._CACHE is container, len(container))))("x")
    # Rebinding it to None would break code that calls .clear() on it.
    assert seen == [(True, 0)]


def test_cold_clears_lru_caches_on_functions_and_methods(fake_caches):
    @functools.lru_cache(maxsize=1)
    def lexed(text):
        return text

    class Lexer:
        @functools.lru_cache(maxsize=1)  # noqa: B019 - the shape the scan must handle
        def lexed(self, text):
            return text

    module = fake_caches(lexed=lexed, Lexer=Lexer, registry=("lexed", "Lexer.lexed"))
    lexer = Lexer()
    lexed("warm"), lexer.lexed("warm")
    sizes = []
    cold(lambda _: sizes.append((lexed.cache_info().currsize, Lexer.lexed.cache_info().currsize)))(
        "x"
    )
    assert sizes == [(0, 0)] and module.lexed is lexed


def test_cold_resets_a_rebound_cache_to_none(fake_caches):
    module = fake_caches(_LAST=("warm", object()))
    seen = []
    cold(lambda _: seen.append(module._LAST))("x")
    assert seen == [None]


def test_cold_rebuilds_a_registered_mutable_cache_on_every_call(monkeypatch):
    # The review's runtime probe: a dict cache filled in place, now registered.
    from unified_agent.telemetry import yaml_jwk

    monkeypatch.setattr(yaml_jwk, "_REVIEW_CACHE", {}, raising=False)
    monkeypatch.setattr(
        growth_budget,
        "LAST_ARGUMENT_CACHES",
        (
            *growth_budget.LAST_ARGUMENT_CACHES,
            ("unified_agent.telemetry.yaml_jwk", "_REVIEW_CACHE"),
        ),
    )
    builds = []

    def cached(text):
        cache = yaml_jwk._REVIEW_CACHE
        if text not in cache:
            cache.clear()
            builds.append(text)
            cache[text] = len(text)
        return cache[text]

    measured = cold(cached)
    for _ in range(3):
        measured("review")
    assert len(builds) == 3, "a registered mutable cache still hits across cold calls"


def test_cold_refuses_a_dotted_cache_that_no_longer_exists(fake_caches):
    fake_caches(Lexer=type("Lexer", (), {}), registry=("Lexer.lexed",))
    with pytest.raises(AttributeError, match="LAST_ARGUMENT_CACHES"):
        cold(len)


def test_cold_refuses_a_cache_module_that_was_never_imported(monkeypatch):
    """An entry names an imported module; ``_resolve`` reads ``sys.modules``.

    It must not import by name: a computed ``import_module(name)`` made this
    helper unreadable to the import graph, so all fifteen test files reaching it
    were unownable and ran on every Python change (task #2458).
    """
    monkeypatch.setattr(
        growth_budget, "LAST_ARGUMENT_CACHES", (("not_an_imported_module", "_CACHE"),)
    )
    with pytest.raises(AttributeError, match="LAST_ARGUMENT_CACHES"):
        cold(len)


# --- cold() refuses state it cannot reset ----------------------------------------------


def test_cold_refuses_a_closure_without_cache_clear(fake_caches):
    def make():
        cache = {}

        def cached(text):
            return cache.setdefault(text, len(text))

        return cached

    fake_caches(_lex_for=make())
    with pytest.raises(TypeError, match="cache_clear"):
        cold(len)


def test_cold_refuses_a_stateful_instance(fake_caches):
    fake_caches(_HOLDER=types.SimpleNamespace(last="warm"))
    with pytest.raises(TypeError, match="_HOLDER"):
        cold(len)


def test_cold_calls_cache_clear_on_a_closure_that_exposes_it(fake_caches):
    cache = {"warm": 1}

    def cached(text):
        return cache.setdefault(text, len(text))

    cached.cache_clear = cache.clear
    fake_caches(_lex_for=cached)
    seen = []
    cold(lambda _: seen.append(len(cache)))("x")
    assert seen == [0]


def test_cold_forgets_a_cached_property_without_computing_it(fake_caches):
    computed = []

    class Holder:
        @functools.cached_property
        def cache(self):
            computed.append(1)
            return {}

    holder = Holder()
    holder.cache["warm"] = 1
    fake_caches(_HOLDER=holder, registry=("_HOLDER.cache",))
    seen = []
    cold(lambda _: seen.append("cache" in vars(holder)))("x")
    assert seen == [False] and computed == [1]


# --- the review's source-visible mutants, once registered, are cold -------------------


def _registered_mutant(monkeypatch, source):
    """Install ``source`` over yaml_jwk and register every cache the scan finds in it."""
    from unified_agent.telemetry import yaml_jwk

    namespace = dict(vars(yaml_jwk))
    exec(compile(source, "<review-mutant>", "exec"), namespace)
    found = module_state_writes(source)
    for path in found:
        root = path.split(".")[0]
        monkeypatch.setattr(yaml_jwk, root, namespace[root], raising=False)
    monkeypatch.setattr(
        growth_budget,
        "LAST_ARGUMENT_CACHES",
        (
            *growth_budget.LAST_ARGUMENT_CACHES,
            *(("unified_agent.telemetry.yaml_jwk", path) for path in sorted(found)),
        ),
    )
    monkeypatch.setattr(yaml_jwk, "_lex_for", namespace["_lex_for"])


@pytest.mark.parametrize("mutant", [_REVIEW_GLOBALS, _REVIEW_GETATTR], ids=["globals", "getattr"])
@pytest.mark.parametrize("consumer", ["private_members", "redact"])
def test_cold_rebuilds_the_lexer_behind_a_registered_mutant_cache(monkeypatch, mutant, consumer):
    from unified_agent.telemetry import jwk, redaction

    built = _lexer_builds(monkeypatch)
    _registered_mutant(monkeypatch, mutant)
    run = (
        jwk.private_members if consumer == "private_members" else redaction.redact_sensitive_content
    )
    text = "jwk: {" + "kt" + "y: " + "R" + "SA, a: x`c, " + "d: x " * 10 + "}\n"
    measured = cold(run)
    results = [measured(text) for _ in range(3)]
    assert results[0] == results[1] == results[2]
    assert built.count(id(text)) == 3


def test_cold_refuses_the_registered_closure_mutant(monkeypatch):
    _lexer_builds(monkeypatch)
    _registered_mutant(monkeypatch, _REVIEW_CLOSURE)
    with pytest.raises(TypeError, match="_lex_for"):
        cold(len)


def test_cold_rebuilds_a_registered_audit_cache(monkeypatch):
    from unified_agent.audit import rules
    from unified_agent.telemetry import jwk

    calls = []
    original = jwk.private_members

    def counted(text, **kwargs):
        calls.append(text)
        return original(text, **kwargs)

    monkeypatch.setattr(jwk, "private_members", counted)
    namespace = dict(vars(rules))
    exec(compile(_REVIEW_AUDIT, "<review-audit-mutant>", "exec"), namespace)
    monkeypatch.setattr(rules, "check_secrets", namespace["check_secrets"])
    monkeypatch.setattr(
        rules, "_REVIEW_AUDIT_CACHE", namespace["_REVIEW_AUDIT_CACHE"], raising=False
    )
    monkeypatch.setattr(
        growth_budget,
        "LAST_ARGUMENT_CACHES",
        (("unified_agent.audit.rules", "_REVIEW_AUDIT_CACHE"),),
    )
    measured = cold(lambda text: list(rules.check_secrets("fixture.json", text)))
    text = '{"kty":"oct","k":"Ab1_-Cd2"}\n'  # ua:allow
    results = [measured(text) for _ in range(3)]
    assert results[0] and results[0] == results[1] == results[2]
    assert len(calls) == 3
