"""scripts/check_components.py keeps src/unified_agent's imports inside the component map.

The map is docs/architecture/components.toml and the known undeclared imports
are in docs/architecture/components-baseline.txt. The repo test is the gate:
a new undeclared import, an unplaced module, or a baseline line for an edge
that is gone all fail it. The rest pin the checker on a small synthetic package.

Test ownership (K2, #2448): tests/component-owners.txt names each test file's
owner; test_repo_owners_file_is_fresh fails when it no longer matches the test
graph, and the synthetic ``unified_agent`` tree pins each ownership rule.
"""

import importlib.util
import sys
import textwrap
from pathlib import Path

from export_guard import is_public_export

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "check_components", REPO_ROOT / "scripts" / "check_components.py"
)
components = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = components  # dataclasses resolve annotations via sys.modules
spec.loader.exec_module(components)

# kernel <- feature <- adapter; "other" stands apart from "feature".
FIXTURE = [
    ("kernel", [], ["store", "util"]),
    ("feature", ["kernel"], ["feat", "feat.*"]),
    ("other", ["kernel"], ["other"]),
    ("mcp-adapter", ["kernel", "feature", "other"], ["server"]),
]
FILES = {
    "store.py": "from . import util\n",
    "util.py": "",
    "feat/__init__.py": "",
    "feat/run.py": "from ..store import save\n",
    "other.py": "",
    "server.py": "from .feat import run\nfrom . import other\n",
}


def make_pkg(tmp_path, files=None, component_map=None, baseline=""):
    """Write package ``app`` under tmp_path/src and a components.toml for it."""
    for rel, body in {"__init__.py": "", **(FILES if files is None else files)}.items():
        path = tmp_path / "src" / "app" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))
    toml = ['package = "app"', 'source = "src"', 'baseline = "baseline.txt"']
    for name, deps, modules in FIXTURE if component_map is None else component_map:
        toml += ["", "[[component]]", f'name = "{name}"', f"depends_on = {deps!r}"]
        toml += ["certifiable = false", f"modules = {modules!r}"]
    (tmp_path / "components.toml").write_text("\n".join(toml).replace("'", '"') + "\n")
    (tmp_path / "baseline.txt").write_text(baseline)
    return tmp_path / "components.toml"


def run(config_path):
    return components.check(components.load_config(config_path))


def test_repo_imports_match_the_component_map():
    config = components.load_config(components.DEFAULT_CONFIG)
    result = components.check(config, owners=False)  # owners: test_repo_owners_file_is_fresh
    problems = (
        [f"new undeclared import {s} -> {d}" for s, d in result.new]
        + [f"fixed, delete from baseline: {s} -> {d}" for s, d in result.stale]
        + [f"not in any component: {m}" for m in result.unmapped]
        + [f"in two components: {m} {h}" for m, h in result.ambiguous.items()]
        + [f"pattern matches nothing: {c} {p}" for c, p in result.unused_patterns]
        + [f"depends_on cycle: {' -> '.join(c)}" for c in result.cycles]
        + result.invalid
    )
    assert not problems, (
        "Component check failed. Run `uv run python scripts/check_components.py` for details "
        "and see docs/architecture/components.md.\n" + "\n".join(problems)
    )
    names = [c.name for c in config.components]
    assert sorted(names) == sorted(components.COMPONENT_NAMES)  # the spec's 16, each once
    assert not any(c.certifiable for c in config.components)  # K1: nothing certified yet


def test_repo_baseline_is_sorted_and_has_no_stale_line():
    config = components.load_config(components.DEFAULT_CONFIG)
    lines = [
        raw.strip()
        for raw in config.baseline.read_text().splitlines()
        if raw.strip() and not raw.startswith("#")
    ]
    assert lines == sorted(set(lines)), "keep components-baseline.txt sorted, one line per edge"
    assert components.check(config, owners=False).stale == []


def test_declared_and_transitive_imports_pass(tmp_path):
    files = {**FILES, "other.py": "from .feat import run\n"}
    component_map = [
        ("kernel", [], ["store", "util"]),
        ("feature", ["kernel"], ["feat", "feat.*"]),
        ("other", ["feature"], ["other"]),  # reaches kernel through feature
        ("mcp-adapter", ["kernel", "feature", "other"], ["server"]),
    ]
    result = run(make_pkg(tmp_path, files, component_map))
    assert result.ok
    assert result.edges["feat.run"] == {"store"}
    assert result.violations == set()


def test_kernel_importing_a_feature_is_new_even_when_lazy(tmp_path):
    files = {**FILES, "util.py": "def f():\n    from .feat import run\n    return run\n"}
    config = make_pkg(tmp_path, files)
    result = run(config)
    assert not result.ok
    assert result.new == [("util", "feat.run")]
    assert components.main(["--config", str(config)]) == 1


def test_undeclared_sibling_import_is_new(tmp_path):
    files = {**FILES, "other.py": "from .feat import run\n"}
    assert run(make_pkg(tmp_path, files)).new == [("other", "feat.run")]


def test_baseline_allows_known_edge_and_reports_fixed_once_it_is_gone(tmp_path):
    files = {**FILES, "store.py": "from . import other\n"}
    config = make_pkg(tmp_path, files, baseline="# known\nstore -> other\n")
    assert run(config).ok

    (tmp_path / "src" / "app" / "store.py").write_text("")
    result = run(config)
    assert not result.ok
    assert result.stale == [("store", "other")]
    assert components.main(["--config", str(config)]) == 1


def test_write_baseline_round_trips_sorted(tmp_path):
    files = {**FILES, "util.py": "from . import other\n", "store.py": "import app.feat.run\n"}
    config = make_pkg(tmp_path, files)
    assert components.main(["--config", str(config), "--write-baseline"]) == 0
    text = (tmp_path / "baseline.txt").read_text()
    assert [line for line in text.splitlines() if line and not line.startswith("#")] == [
        "store -> feat.run",
        "util -> other",
    ]
    assert components.main(["--config", str(config)]) == 0


def test_unmapped_ambiguous_and_unused_patterns_fail(tmp_path):
    files = {**FILES, "stray.py": "", "feat/other_cli.py": ""}
    component_map = [
        ("kernel", [], ["store", "util", "gone"]),
        ("feature", ["kernel"], ["feat", "feat.*"]),
        ("other", ["kernel"], ["other", "*_cli"]),
        ("mcp-adapter", ["kernel", "feature", "other"], ["server"]),
    ]
    result = run(make_pkg(tmp_path, files, component_map))
    assert result.unmapped == ["stray"]
    assert result.ambiguous == {"feat.other_cli": ["feature", "other"]}
    assert result.unused_patterns == [("kernel", "gone")]
    assert not result.ok


def test_depends_on_cycle_fails(tmp_path):
    component_map = [
        ("kernel", [], ["store", "util"]),
        ("feature", ["kernel", "other"], ["feat", "feat.*"]),
        ("other", ["feature"], ["other"]),
        ("mcp-adapter", ["kernel", "feature", "other"], ["server"]),
    ]
    result = run(make_pkg(tmp_path, component_map=component_map))
    assert result.cycles == [["feature", "other", "feature"]]
    assert not result.ok


def test_kernel_dependencies_and_depending_on_the_composition_root_are_invalid(tmp_path):
    component_map = [
        ("kernel", ["other"], ["store", "util"]),
        ("feature", ["kernel", "mcp-adapter", "missing"], ["feat", "feat.*"]),
        ("other", [], ["other"]),
        ("mcp-adapter", ["kernel", "feature", "other"], ["server"]),
    ]
    result = run(make_pkg(tmp_path, component_map=component_map))
    assert not result.ok
    assert len(result.invalid) == 3
    assert any("kernel must have depends_on = []" in p for p in result.invalid)
    assert any("depends on mcp-adapter" in p for p in result.invalid)
    assert any("unknown component 'missing'" in p for p in result.invalid)


def test_report_counts_modules_lines_and_kernel_edges(tmp_path):
    files = {**FILES, "util.py": "from . import other\nfrom .feat import run\n"}
    config_path = make_pkg(tmp_path, files)
    config = components.load_config(config_path)
    text = components.report(config, components.check(config))
    assert "| kernel | 2 | 3 | 2 | 0 |" in text
    assert "Kernel outbound edges: 2" in text
    assert "| feature | 1 |" in text and "| other | 1 |" in text
    assert "Components with no undeclared importer: kernel, mcp-adapter" in text


# -- test ownership (K2, #2448) ----------------------------------------------
# A tree in the real package name, which the affected_tests graph is built for.
OWNED_MAP = [
    ("kernel", [], ["store"]),
    ("feature", ["kernel"], ["feat", "feat.*"]),
    ("other", ["kernel"], ["other"]),
    ("mcp-adapter", ["kernel", "feature", "other"], ["server", "cli"]),
]
OWNED_SRC = {
    "store.py": "",
    "feat/__init__.py": "",
    "feat/run.py": "from ..store import save\n",
    "other.py": "",
    "server.py": "from . import feat, other\n",
    "cli.py": "",
}
OWNED_TESTS = {
    "test_kernel.py": "from unified_agent import store\n\ndef test_a():\n    pass\n",
    "test_feature.py": (
        "from unified_agent.feat import run\nfrom unified_agent import store\n\n"
        "def test_a():\n    pass\n\nasync def test_b():\n    pass\n"
    ),
    "test_adapter.py": "import unified_agent.cli\n",
    "test_feature_cli.py": "import unified_agent.cli\nimport unified_agent.feat.run\n",
    "test_two.py": (
        "import unified_agent.feat.run\n\n"
        "def test_a(monkeypatch):\n"
        '    monkeypatch.setattr("unified_agent.other.flag", 1)\n'
    ),
    "helper.py": "from unified_agent import other\n",
    "test_via_helper.py": "from helper import other\n",
    "test_server.py": "from unified_agent import server\n",
    "test_dynamic.py": "import importlib\n\ndef load(name):\n    return importlib.import_module(name)\n",
    "sub/__init__.py": "",
    "sub/conftest.py": "from unified_agent.feat import run\n",
    "sub/test_nested.py": "from unified_agent import store\n",
}
OWNED_EXPECTED = {
    "tests/sub/test_nested.py": "feature",  # its directory conftest imports feat
    "tests/test_adapter.py": "mcp-adapter",
    "tests/test_dynamic.py": "unresolved",
    "tests/test_feature.py": "feature",
    "tests/test_feature_cli.py": "feature",  # mcp-adapter is not counted
    "tests/test_kernel.py": "kernel",
    "tests/test_server.py": "integration:feature+other",  # server reaches every component
    "tests/test_two.py": "integration:feature+other",  # a monkeypatch string counts
    "tests/test_via_helper.py": "other",  # through the helper it imports
}


def make_owned_tree(tmp_path, tests=None):
    config = make_pkg(tmp_path, {}, OWNED_MAP)
    for rel, body in {"__init__.py": "", **OWNED_SRC}.items():
        path = tmp_path / "src" / "unified_agent" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    toml = (tmp_path / "components.toml").read_text().replace('"app"', '"unified_agent"')
    (tmp_path / "components.toml").write_text(toml)
    for rel, body in (OWNED_TESTS if tests is None else tests).items():
        path = tmp_path / "tests" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return config


def test_owner_rule():
    assert components.owner_of(set()) == "kernel"
    assert components.owner_of({"kernel"}) == "kernel"
    assert components.owner_of({"kernel", "mcp-adapter"}) == "mcp-adapter"
    assert components.owner_of({"mcp-adapter", "github"}) == "github"
    assert components.owner_of({"node", "kernel", "dispatch"}) == "integration:dispatch+node"


def test_each_test_file_gets_its_owner_from_direct_dependencies(tmp_path):
    config = components.load_config(make_owned_tree(tmp_path))
    own = components.check(config).ownership
    assert own.owners == OWNED_EXPECTED
    assert own.whole == {"tests/test_server.py"}
    assert own.test_counts["tests/test_feature.py"] == 2  # def and async def
    assert own.test_counts["tests/test_adapter.py"] == 0


def test_write_owners_round_trips_and_then_the_check_passes(tmp_path):
    config = make_owned_tree(tmp_path)
    assert components.main(["--config", str(config)]) == 1  # no owners file yet: UNOWNED
    assert components.main(["--config", str(config), "--write-owners"]) == 0
    text = (tmp_path / "tests" / "component-owners.txt").read_text()
    assert text.startswith("# ")
    body = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert body == [f"{t} {o}" for t, o in sorted(OWNED_EXPECTED.items())]
    assert components.main(["--config", str(config)]) == 0


def test_a_new_import_makes_the_owners_file_stale(tmp_path, capsys):
    config = make_owned_tree(tmp_path)
    components.main(["--config", str(config), "--write-owners"])
    (tmp_path / "tests" / "test_kernel.py").write_text("from unified_agent import other\n")
    result = components.check(components.load_config(config))
    assert result.stale_owners == ["tests/test_kernel.py: kernel -> other"]
    assert result.unowned == [] and not result.ok
    capsys.readouterr()
    assert components.main(["--config", str(config)]) == 1
    assert "STALE_OWNERS tests/test_kernel.py: kernel -> other; run --write-owners" in (
        capsys.readouterr().out
    )


def test_new_test_file_is_unowned_and_a_deleted_one_is_stale(tmp_path, capsys):
    config = make_owned_tree(tmp_path)
    components.main(["--config", str(config), "--write-owners"])
    (tmp_path / "tests" / "test_new.py").write_text("")
    (tmp_path / "tests" / "test_adapter.py").unlink()
    result = components.check(components.load_config(config))
    assert result.unowned == ["tests/test_new.py"]
    assert result.stale_owners == ["tests/test_adapter.py: mcp-adapter -> no such test file"]
    capsys.readouterr()
    assert components.main(["--config", str(config)]) == 1
    assert "UNOWNED   tests/test_new.py" in capsys.readouterr().out


def test_report_has_the_ownership_table_and_single_owner_share(tmp_path):
    config = components.load_config(make_owned_tree(tmp_path))
    text = components.report(config, components.check(config))
    assert "Test ownership: 9 files, 4 test functions" in text
    assert "| feature | 3 | 2 |" in text
    assert "| integration:* | 2 | 1 |" in text
    assert "Single owner: 6 of 9 files (66.7%), 3 of 4 tests (75.0%)." in text
    assert "Unresolved: 1 files, 0 tests." in text
    assert "| `tests/test_two.py` | 1 | feature, other |" in text
    assert "| `tests/test_server.py` | 0 | every component |" in text


def test_repo_owners_file_is_fresh():
    config = components.load_config(components.DEFAULT_CONFIG)
    result = components.check(config)
    assert result.ownership is not None
    stale = result.stale_owners
    if is_public_export(Path(__file__).resolve().parents[1]):
        # The export drops some test files but ships the owners map whole.
        stale = [line for line in stale if not line.endswith("-> no such test file")]
    problems = [f"unowned: {t}" for t in result.unowned] + stale
    assert not problems, (
        "tests/component-owners.txt is out of date. Run "
        "`uv run python scripts/check_components.py --write-owners` and commit it.\n"
        + "\n".join(problems[:20])
    )
