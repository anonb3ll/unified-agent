"""Sampling of large parametrize matrices (tasks #2230, #2261).

Stacked ``@pytest.mark.parametrize`` decorators multiply: the redaction and JWK
scan tests grew to about 50,000 cases, most of the suite's 70,000, and an hour
of CPU per full run. Almost every one of those cases varies several
independent dimensions, and the full product of their values is only needed
when the code it exercises changes.

Three tiers, chosen per run (``tests/conftest.py``):

``representative`` (the default)
    ``select_representative`` keeps, for each test function, a subset in which
    every value of every dimension still occurs at least once, plus a stable
    sample of the rest of the product: the cases whose node id hashes into one
    bucket of ``SAMPLE_ONE_IN``. Matrices of at most
    ``MIN_REPRESENTATIVE_CASES`` cases keep every case.
``pairwise``
    ``select_pairwise`` keeps a subset in which every pair of values of every
    two dimensions still occurs at least once. A function with fewer than three
    dimensions, or no more than ``MIN_SAMPLED_CASES`` cases, keeps every case:
    one or two dimensions can only be covered pairwise by running all of them,
    and a small matrix is not worth sampling.
``exhaustive``
    Every case.

Only the files ``SAMPLED_FILES`` names are sampled. ``scripts/ci_changes.py``
runs every case whenever a change touches one of them, the scanners they
exercise, this module or the CI decision itself, so a new or changed matrix
always runs in full on the change that introduces it
(``tests/test_param_sampling.py`` pins the two lists together). Elsewhere every
case runs. ``@pytest.mark.exhaustive`` opts one function out of sampling
permanently.

Both selections are greedy passes in collection order. They depend only on
which cases share a parameter object and on node ids, so every pytest-xdist
worker, which collects independently and must see the same items, selects the
same cases. Nothing is random.
"""

from __future__ import annotations

import inspect
import re
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterable
from itertools import combinations

import pytest

#: A matrix of at most this many cases always runs in full under ``pairwise``.
MIN_SAMPLED_CASES = 32
#: Fewer dimensions than this cannot be reduced without losing a pair.
MIN_DIMENSIONS = 3
#: A matrix of at most this many cases always runs in full under
#: ``representative``.
MIN_REPRESENTATIVE_CASES = 8
#: ``representative`` also keeps about one case in this many of the product,
#: chosen by a CRC-32 of the node id.
SAMPLE_ONE_IN = 100
#: The combinatorial redaction, JWK and audit matrices. Keep in step with
#: ``EXHAUSTIVE`` in ``scripts/ci_changes.py``.
SAMPLED_FILES = re.compile(r"^tests/test_[^/]*(redact|jwk|secret|audit)[^/]*\.py$")


def _definition_marks(item: pytest.Item) -> list[pytest.Mark]:
    """``parametrize`` marks on the function, its class and its module.

    ``item.iter_markers`` also yields marks attached to one case through
    ``pytest.param(..., marks=...)``, which did not generate the case and must
    not count as parametrization.
    """
    marks: list[pytest.Mark] = []
    for owner in (
        getattr(item, "function", None),
        getattr(item, "cls", None),
        getattr(item, "module", None),
    ):
        owned = getattr(owner, "pytestmark", None) or ()
        # ``pytestmark = pytest.mark.x`` is a single mark, not a list of them.
        if not isinstance(owned, list | tuple):
            owned = [owned]
        for mark in owned:
            mark = getattr(mark, "mark", mark)
            if getattr(mark, "name", None) == "parametrize":
                marks.append(mark)
    return marks


# ``indirect`` may be passed positionally (``parametrize("f", vals, True)``),
# so a mark's arguments are bound the way pytest binds them.
_PARAMETRIZE = inspect.signature(pytest.Metafunc.parametrize)


def _direct_argnames(item: pytest.Item) -> set[str]:
    """Argnames set directly by ``@pytest.mark.parametrize`` (not indirect)."""
    names: set[str] = set()
    for mark in _definition_marks(item):
        try:
            bound = _PARAMETRIZE.bind(None, *mark.args, **mark.kwargs).arguments
        except TypeError:
            # A mark pytest itself would reject: treat nothing as direct.
            return set()
        argnames = bound["argnames"]
        if isinstance(argnames, str):
            argnames = [name.strip() for name in argnames.split(",") if name.strip()]
        indirect = bound.get("indirect", False)
        if indirect is True:
            continue
        names |= set(argnames) - set(indirect or ())
    return names


def _group_key(item: pytest.Item) -> tuple[str, str, tuple[str, ...]] | None:
    """Cases of one function: its collector, its name and its argnames.

    The collector's node id separates ``TestA.test_m`` from ``TestB.test_m``;
    the argname set keeps cases with a different parameter schema apart.
    A case that varies a fixture's ``params`` or an indirect argument, or that
    carries its own marks (``pytest.param(..., marks=...)``), is never
    sampled, and neither is the rest of its function: a fixture can tell equal
    parameter objects apart through ``request.param_index``, and a mark can
    skip or xfail one entry, so object identity alone does not identify such
    cases.
    """
    callspec = getattr(item, "callspec", None)
    if callspec is None or not callspec.params:
        return None
    path = item.nodeid.split("::", 1)[0]
    if not SAMPLED_FILES.match(path):
        return None
    if not set(callspec.params) <= _direct_argnames(item):
        return None
    parent = getattr(getattr(item, "parent", None), "nodeid", path)
    name = getattr(item, "originalname", None) or item.name
    return parent, name, tuple(sorted(callspec.params))


def _values(item: pytest.Item, names: tuple[str, ...]) -> list[int]:
    """One key per argname: the identity of the parameter object.

    pytest hands every case the very object from the parametrize list, so two
    cases share a key exactly when they receive the same object, which a test
    function cannot tell apart (fixture and indirect parameters, which can,
    are never sampled; see ``_group_key``). ``repr`` cannot be used (distinct objects
    can print the same), nor ``callspec.indices``, which pytest 9 records once
    per combined case rather than once per decorator.
    """
    params = item.callspec.params
    return [id(params[name]) for name in names]


def select_pairwise(items: list[pytest.Item]) -> list[pytest.Item]:
    """The cases of one function that cover every value pair, in order.

    All ``items`` share one argname set. Each argname is one dimension; names
    set by one decorator (``"a,b"``) always move together, so pairs between
    them cost no extra cases.

    Greedy by threshold: a first pass keeps only cases that add the most new
    pairs a case can add, and each later pass accepts one fewer, down to one.
    That stays close to the size the classic "most new pairs first" greedy
    reaches, at a cost linear in the cases per pass, which matters because
    every pytest-xdist worker repeats the collection.
    """
    if len(items) <= MIN_SAMPLED_CASES:
        return items
    names = tuple(sorted(items[0].callspec.params))
    if len(names) < MIN_DIMENSIONS:
        return items
    dims = list(combinations(range(len(names)), 2))
    case_pairs = []
    for item in items:
        values = _values(item, names)
        case_pairs.append({(a, values[a], b, values[b]) for a, b in dims})
    covered: set[tuple[int, int, int, int]] = set()
    kept = [False] * len(items)
    for threshold in range(len(dims), 0, -1):
        for i, pairs in enumerate(case_pairs):
            if not kept[i] and len(pairs - covered) >= threshold:
                covered |= pairs
                kept[i] = True
    return [item for item, keep in zip(items, kept, strict=True) if keep]


def select_representative(items: list[pytest.Item]) -> list[pytest.Item]:
    """Cases of one function that show every value at least once, in order.

    All ``items`` share one argname set. Every value of every dimension stays
    in at least one kept case, which keeps each distinct shape, consumer and
    layer the matrix names; the cases whose node id falls in the stable sample
    bucket (``SAMPLE_ONE_IN``) are kept as well, a fixed slice of their
    combinations, the same slice on every run and every worker.

    Greedy by threshold, as in ``select_pairwise``: a first pass keeps only
    cases whose every value is still unseen, and each later pass accepts one
    fewer new value, down to one. That keeps a matrix close to the size of its
    largest dimension.
    """
    if len(items) <= MIN_REPRESENTATIVE_CASES:
        return items
    names = tuple(sorted(items[0].callspec.params))
    case_values = [set(enumerate(_values(item, names))) for item in items]
    kept = [_in_stable_sample(item) for item in items]
    covered = {value for i, values in enumerate(case_values) if kept[i] for value in values}
    for threshold in range(len(names), 0, -1):
        for i, values in enumerate(case_values):
            if not kept[i] and len(values - covered) >= threshold:
                covered |= values
                kept[i] = True
    return [item for item, keep in zip(items, kept, strict=True) if keep]


def _in_stable_sample(item: pytest.Item) -> bool:
    return zlib.crc32(item.nodeid.encode()) % SAMPLE_ONE_IN == 0


Selector = Callable[[list[pytest.Item]], list[pytest.Item]]


def sample(
    items: Iterable[pytest.Item], select: Selector = select_representative
) -> tuple[list[pytest.Item], list[pytest.Item]]:
    """Split ``items`` into (kept, deselected) by ``select``, keeping order."""
    items = list(items)
    groups: dict[tuple[str, str, tuple[str, ...]], list[pytest.Item]] = defaultdict(list)
    for item in items:
        key = _group_key(item)
        if key is not None and item.get_closest_marker("exhaustive") is None:
            groups[key].append(item)
    dropped: set[int] = set()
    for group in groups.values():
        if any(item.callspec.marks for item in group):
            continue
        selected = {id(item) for item in select(group)}
        dropped |= {id(item) for item in group if id(item) not in selected}
    kept = [item for item in items if id(item) not in dropped]
    deselected = [item for item in items if id(item) in dropped]
    return kept, deselected


def pairwise_sample(items: Iterable[pytest.Item]) -> tuple[list[pytest.Item], list[pytest.Item]]:
    """``sample`` with the pairwise-covering selection."""
    return sample(items, select_pairwise)


def representative_sample(
    items: Iterable[pytest.Item],
) -> tuple[list[pytest.Item], list[pytest.Item]]:
    """``sample`` with the every-value-plus-stable-slice selection."""
    return sample(items, select_representative)


#: ``tests/conftest.py`` maps ``--param-sample`` to one of these.
TIERS: dict[str, Selector | None] = {
    "representative": select_representative,
    "pairwise": select_pairwise,
    "exhaustive": None,
}
