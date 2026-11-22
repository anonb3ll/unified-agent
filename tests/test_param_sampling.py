"""Sampling of large parametrize matrices (tasks #2230, #2261)."""

import importlib.util
import os
import subprocess
import sys
import textwrap
import zlib
from dataclasses import dataclass, field
from itertools import combinations, product
from pathlib import Path
from types import SimpleNamespace

import pytest
from param_sampling import (
    MIN_REPRESENTATIVE_CASES,
    MIN_SAMPLED_CASES,
    SAMPLE_ONE_IN,
    SAMPLED_FILES,
    pairwise_sample,
    representative_sample,
    select_pairwise,
    select_representative,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FILE = "tests/test_jwk_matrix.py"


@dataclass
class _CallSpec:
    params: dict
    marks: list = field(default_factory=list)


@dataclass
class _Item:
    """The parts of a pytest item the sampler reads."""

    nodeid: str
    originalname: str
    callspec: _CallSpec | None
    markers: set = field(default_factory=set)
    function: object = None

    @property
    def name(self):
        return self.nodeid.rsplit("::", 1)[-1]

    @property
    def parent(self):
        return SimpleNamespace(nodeid=self.nodeid.rsplit("::", 1)[0])

    def get_closest_marker(self, name):
        return name if name in self.markers else None


def _matrix(name="test_m", markers=(), path=FILE, cls=None, fixture_params=(), **dims):
    names = list(dims)
    prefix = f"{path}::{cls}::" if cls else f"{path}::"
    function = SimpleNamespace(
        pytestmark=[
            pytest.mark.parametrize(arg, list(values)).mark
            for arg, values in dims.items()
            if arg not in fixture_params
        ]
    )
    return [
        _Item(
            f"{prefix}{name}[{'-'.join(map(str, values))}]",
            name,
            _CallSpec(dict(zip(names, values, strict=True))),
            set(markers),
            function,
        )
        for values in product(*dims.values())
    ]


def _pairs(items):
    out = set()
    for item in items:
        params = item.callspec.params
        for a, b in combinations(sorted(params), 2):
            out.add((a, id(params[a]), b, id(params[b])))
    return out


def test_every_value_pair_survives_and_the_matrix_shrinks():
    items = _matrix(a=range(20), b=range(4), c=range(3), d=range(2), e=range(3))
    kept = select_pairwise(items)
    assert _pairs(kept) == _pairs(items)
    assert len(kept) < len(items) // 10


def test_selection_is_deterministic_and_keeps_collection_order():
    items = _matrix(a=range(6), b=range(5), c=range(4))
    first = select_pairwise(items)
    assert first == select_pairwise(list(items))
    assert first == [item for item in items if item in first]


def test_one_or_two_dimensions_and_small_matrices_run_in_full():
    assert select_pairwise(_matrix(a=range(50), b=range(3))) == _matrix(a=range(50), b=range(3))
    small = _matrix(a=range(4), b=range(4), c=range(2))
    assert len(small) == MIN_SAMPLED_CASES
    assert select_pairwise(small) == small


def test_exhaustive_marker_and_unparametrized_items_are_never_sampled():
    marked = _matrix("test_full", markers={"exhaustive"}, a=range(5), b=range(5), c=range(5))
    sampled = _matrix("test_sampled", a=range(5), b=range(5), c=range(5))
    plain = _Item(f"{FILE}::test_plain", "test_plain", None)
    kept, deselected = pairwise_sample([plain, *marked, *sampled])
    assert kept[0] is plain and all(item in kept for item in marked)
    assert deselected and all(item in sampled for item in deselected)
    assert len(kept) + len(deselected) == 1 + len(marked) + len(sampled)


def test_same_named_methods_in_different_classes_are_sampled_apart():
    a = _matrix(cls="TestA", a=range(4), b=range(4), c=range(3))
    b = _matrix(cls="TestB", a=range(4), b=range(4), c=range(3))
    kept, _ = pairwise_sample([*a, *b])
    assert _pairs([i for i in kept if i in a]) == _pairs(a)
    assert _pairs([i for i in kept if i in b]) == _pairs(b)


def test_cases_with_a_different_argname_schema_are_sampled_apart():
    first = _matrix(a=range(4), b=range(4), c=range(3))
    second = _matrix(x=range(4), y=range(4), z=range(3))
    kept, _ = pairwise_sample([*first, *second])
    assert _pairs([i for i in kept if i in second]) == _pairs(second)


class _Same:
    def __repr__(self):
        return "same"


def test_distinct_values_that_print_alike_stay_distinct():
    values = [_Same() for _ in range(4)]
    items = _matrix(a=values, b=list(values), c=range(3))
    kept = select_pairwise(items)
    assert _pairs(kept) == _pairs(items)


def test_only_the_listed_matrix_files_are_sampled():
    elsewhere = _matrix(path="tests/test_tasks.py", a=range(5), b=range(5), c=range(5))
    kept, deselected = pairwise_sample(elsewhere)
    assert kept == elsewhere and deselected == []


def test_fixture_and_indirect_parameters_are_never_sampled():
    fixture = _matrix(fixture_params=("f",), f=[None, None], a=range(5), b=range(4))
    assert pairwise_sample(fixture) == (fixture, [])
    indirect = _matrix(a=range(5), b=range(4), c=range(3))
    marks = SimpleNamespace(pytestmark=[pytest.mark.parametrize("a,b,c", [], indirect=["c"]).mark])
    for item in indirect:
        item.function = marks
    assert pairwise_sample(indirect) == (indirect, [])


def test_a_function_with_per_case_marks_is_never_sampled():
    items = _matrix(a=range(5), b=range(4), c=range(3))
    items[0].callspec.marks = [pytest.mark.skip.mark]
    assert pairwise_sample(items) == (items, [])


def _values_seen(items):
    out = set()
    for item in items:
        params = item.callspec.params
        out |= {(name, id(value)) for name, value in params.items()}
    return out


def test_representative_keeps_every_value_and_shrinks_below_pairwise():
    items = _matrix(a=range(20), b=range(4), c=range(3), d=range(2), e=range(3))
    kept = select_representative(items)
    assert _values_seen(kept) == _values_seen(items)
    assert len(kept) < len(select_pairwise(items)) < len(items)


def test_representative_keeps_the_stable_slice_of_the_product():
    items = _matrix(a=range(12), b=range(10), c=range(10))
    slice_ = [i for i in items if zlib.crc32(i.nodeid.encode()) % SAMPLE_ONE_IN == 0]
    assert slice_, "the product is large enough to hit the sample bucket"
    kept = select_representative(items)
    assert all(item in kept for item in slice_)
    assert len(kept) <= 12 + len(slice_)
    assert len(select_representative(items)) == len(kept)


def test_representative_is_deterministic_and_keeps_collection_order():
    items = _matrix(a=range(6), b=range(5), c=range(4))
    first = select_representative(items)
    assert first == select_representative(list(items))
    assert first == [item for item in items if item in first]


def test_representative_thins_two_dimensions_but_runs_small_matrices_in_full():
    two = _matrix(a=range(50), b=range(3))
    kept = select_representative(two)
    assert _values_seen(kept) == _values_seen(two) and len(kept) < len(two)
    small = _matrix(a=range(4), b=range(2))
    assert len(small) == MIN_REPRESENTATIVE_CASES
    assert select_representative(small) == small


def test_representative_values_that_print_alike_stay_distinct():
    values = [_Same() for _ in range(4)]
    items = _matrix(a=values, b=list(values), c=range(3))
    assert _values_seen(select_representative(items)) == _values_seen(items)


def test_representative_sample_honours_the_same_exclusions_as_pairwise():
    marked = _matrix("test_full", markers={"exhaustive"}, a=range(5), b=range(5), c=range(5))
    sampled = _matrix("test_sampled", a=range(5), b=range(5), c=range(5))
    elsewhere = _matrix(path="tests/test_tasks.py", a=range(5), b=range(5), c=range(5))
    with_marks = _matrix("test_marked", a=range(5), b=range(4), c=range(3))
    with_marks[0].callspec.marks = [pytest.mark.skip.mark]
    kept, deselected = representative_sample([*marked, *sampled, *elsewhere, *with_marks])
    assert deselected and all(item in sampled for item in deselected)
    assert all(item in kept for item in [*marked, *elsewhere, *with_marks])


def test_every_sampled_file_forces_an_exhaustive_ci_run():
    spec = importlib.util.spec_from_file_location(
        "ci_changes", REPO_ROOT / "scripts" / "ci_changes.py"
    )
    ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci)
    sampled = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "tests").rglob("test_*.py"))
        if SAMPLED_FILES.match(path.relative_to(REPO_ROOT).as_posix())
    ]
    assert sampled, "the sampled-file pattern matches nothing"
    assert [path for path in sampled if not ci.exhaustive([path])] == []


def _clean_env(**extra):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"UA_TEST_EXHAUSTIVE", "UA_TEST_PARAM_SAMPLE"}
    }
    return {**env, **extra}


def _probe(tmp_path, body):
    """Run a real pytest session over one sampled-named file with the sampler."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(REPO_ROOT / "tests")!r})
            from param_sampling import pairwise_sample

            def pytest_collection_modifyitems(config, items):
                kept, deselected = pairwise_sample(items)
                if deselected:
                    config.hook.pytest_deselected(items=deselected)
                    items[:] = kept
            """
        )
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_jwk_probe.py").write_text(textwrap.dedent(body))
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rN"],
        cwd=tmp_path,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return run.stdout.strip().splitlines()[-1]


def test_real_session_samples_a_plain_matrix(tmp_path):
    summary = _probe(
        tmp_path,
        """
        import pytest

        @pytest.mark.parametrize("a", range(5))
        @pytest.mark.parametrize("b", range(4))
        @pytest.mark.parametrize("c", range(3))
        def test_m(a, b, c):
            pass
        """,
    )
    assert "deselected" in summary and "failed" not in summary


def test_real_session_runs_every_enabled_case_beside_a_skipped_entry(tmp_path):
    summary = _probe(
        tmp_path,
        """
        import pytest

        @pytest.mark.parametrize(
            "a",
            [
                pytest.param(None, marks=pytest.mark.skip(reason="off"), id="disabled"),
                pytest.param(None, id="enabled"),
            ],
        )
        @pytest.mark.parametrize("b", range(5))
        @pytest.mark.parametrize("c", range(4))
        def test_m(a, b, c):
            pass
        """,
    )
    assert "20 passed" in summary and "20 skipped" in summary
    assert "deselected" not in summary


def test_real_session_never_samples_fixture_params_marked_per_entry(tmp_path):
    summary = _probe(
        tmp_path,
        """
        import pytest

        @pytest.fixture(
            params=[
                pytest.param(None, marks=pytest.mark.parametrize("f", [None])),
                pytest.param(None, marks=pytest.mark.parametrize("f", [None])),
            ]
        )
        def f(request):
            return request.param_index

        @pytest.mark.parametrize("b", range(5))
        @pytest.mark.parametrize("c", range(4))
        def test_m(f, b, c):
            pass
        """,
    )
    assert "deselected" not in summary and "error" not in summary


def test_real_session_accepts_a_single_module_mark(tmp_path):
    summary = _probe(
        tmp_path,
        """
        import pytest

        pytestmark = pytest.mark.parametrize("a", range(5))

        @pytest.mark.parametrize("b", range(4))
        @pytest.mark.parametrize("c", range(3))
        def test_m(a, b, c):
            pass
        """,
    )
    assert "deselected" in summary and "error" not in summary


@pytest.mark.parametrize("indirect", ["True", '["f"]'])
def test_real_session_never_samples_a_positional_indirect_argument(tmp_path, indirect):
    summary = _probe(
        tmp_path,
        f"""
        import pytest

        @pytest.fixture
        def f(request):
            return request.param_index

        @pytest.mark.parametrize("f", [None, None], {indirect})
        @pytest.mark.parametrize("b", range(5))
        @pytest.mark.parametrize("c", range(4))
        def test_m(f, b, c):
            assert f == 0
        """,
    )
    assert summary.startswith("20 failed, 20 passed") and "deselected" not in summary


def test_two_fresh_collections_select_the_same_cases():
    """Each xdist worker collects in its own process and must agree."""
    target = "tests/test_jwk_comment_ownership_2137.py"
    runs = [
        subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
            + [target],
            cwd=REPO_ROOT,
            env=_clean_env(),
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        ).stdout
        for _ in range(2)
    ]
    selected = [[line for line in run.splitlines() if "::" in line] for run in runs]
    assert selected[0] == selected[1]
    assert "deselected" in runs[0]


def _collected(*args, **env):
    """Last summary line of collecting one real matrix file with the repo conftest."""
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
        + ["tests/test_jwk_unpack_operand_2135.py", *args],
        cwd=REPO_ROOT,
        env=_clean_env(**env),
        capture_output=True,
        text=True,
        timeout=300,
    )
    return run.returncode, (run.stdout + run.stderr).strip().splitlines()[-1]


def _count(summary):
    return int(summary.split()[0].split("/")[0])


def test_tiers_order_representative_below_pairwise_below_exhaustive():
    """The default tier is the smallest; both opt-ins reach the full matrix."""
    default = _count(_collected()[1])
    pairwise = _count(_collected("--param-sample=pairwise")[1])
    by_env = _count(_collected(UA_TEST_PARAM_SAMPLE="pairwise")[1])
    full = [
        _count(_collected("--exhaustive")[1]),
        _count(_collected(UA_TEST_EXHAUSTIVE="1")[1]),
        _count(_collected("--param-sample=exhaustive")[1]),
        _count(_collected(UA_TEST_PARAM_SAMPLE="exhaustive")[1]),
        # CI's UA_TEST_EXHAUSTIVE wins over a narrower tier.
        _count(_collected("--param-sample=pairwise", UA_TEST_EXHAUSTIVE="1")[1]),
    ]
    assert default < pairwise == by_env < full[0]
    assert len(set(full)) == 1


def test_an_unknown_tier_fails_the_session():
    code, summary = _collected(UA_TEST_PARAM_SAMPLE="most")
    assert code != 0 and "UA_TEST_PARAM_SAMPLE" in summary
