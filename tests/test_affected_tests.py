"""Import-graph test selection (task #2231): unified_agent.affected_tests and its CLI.

Most cases build a small synthetic repo (``src/unified_agent``, ``tests``,
``scripts``) so each rule is pinned on its own; the last few check the real tree.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from unified_agent import affected_tests as at

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "affected_tests_cli", REPO_ROOT / "scripts" / "affected_tests.py"
)
cli = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cli
_spec.loader.exec_module(cli)


def make_repo(root: Path, files: dict[str, str]) -> Path:
    base = {"src/unified_agent/__init__.py": "", "pyproject.toml": "", **files}
    for rel, body in base.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))
    return root


def selected(root: Path, *paths: str, depth: str = "transitive", status: str = "M") -> list[str]:
    sel = at.select(root, [at.Change(status, p) for p in paths], depth=depth)
    assert not sel.full, sel.full_reasons
    return sel.tests


@pytest.fixture
def repo(tmp_path):
    return make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "X = 1\n",
            "src/unified_agent/mid.py": "from unified_agent.core import X\n",
            "src/unified_agent/top.py": "from . import mid\n",
            "src/unified_agent/lazy_user.py": "def f():\n    from .core import X\n",
            "src/unified_agent/above_lazy.py": "import unified_agent.lazy_user\n",
            "src/unified_agent/lazy_mid_user.py": "def f():\n    from . import mid\n",
            "src/unified_agent/tools/__init__.py": "",
            "src/unified_agent/tools/vault.py": "from ..core import X\n",
            "tests/helper_mod.py": "from unified_agent import mid\n",
            "tests/test_core.py": "from unified_agent.core import X\n",
            "tests/test_top.py": "import unified_agent.top\n",
            "tests/test_via_helper.py": "from helper_mod import mid\n",
            "tests/test_lazy_in_test.py": "def test_x():\n    from unified_agent import mid\n",
            "tests/test_patch_target.py": (
                'def test_x(monkeypatch):\n    monkeypatch.setattr("unified_agent.top.mid", None)\n'
            ),
            "tests/test_above_lazy.py": "from unified_agent import above_lazy\n",
            "tests/test_lazy_user.py": "from unified_agent import lazy_user\n",
            "tests/test_lazy_mid_user.py": "from unified_agent import lazy_mid_user\n",
            "tests/test_vault.py": "from unified_agent.tools import vault\n",
            "tests/test_unrelated.py": "import json\n",
        },
    )


def test_transitive_walk_follows_module_level_imports(repo):
    assert selected(repo, "src/unified_agent/core.py") == [
        "tests/test_above_lazy.py",  # eager importer of that lazy importer
        "tests/test_core.py",
        "tests/test_lazy_in_test.py",  # lazy import inside a test still counts
        "tests/test_lazy_user.py",  # lazy importer of the changed file itself
        "tests/test_patch_target.py",  # dotted patch target names top
        "tests/test_top.py",
        "tests/test_vault.py",  # relative import from a subpackage
        "tests/test_via_helper.py",  # through a test helper imported by bare name
    ]


def test_lazy_import_is_followed_only_out_of_the_changed_file(repo):
    # lazy_mid_user imports mid inside a function. Changing mid reaches it;
    # changing core reaches mid first, and the lazy edge above mid is not
    # followed, which is what keeps the src import cycles from joining up.
    assert "tests/test_lazy_mid_user.py" in selected(repo, "src/unified_agent/mid.py")
    assert "tests/test_lazy_mid_user.py" not in selected(repo, "src/unified_agent/core.py")


def test_full_depth_follows_every_lazy_edge(repo):
    """Review finding: core -> mid (eager) -> lazy_mid_user (lazy) -> test is cut
    by the default walk; ``full`` follows it for anyone who wants completeness."""
    full = selected(repo, "src/unified_agent/core.py", depth="full")
    assert "tests/test_lazy_mid_user.py" in full
    assert set(selected(repo, "src/unified_agent/core.py")) < set(full)


def test_direct_depth_stops_at_other_src_modules(repo):
    # test_vault imports tools.vault, and test_lazy_user lazy_user: both reach
    # core only through another src module.
    assert selected(repo, "src/unified_agent/core.py", depth="direct") == ["tests/test_core.py"]
    # Test helpers are not src modules, so the direct tier walks through them.
    assert selected(repo, "src/unified_agent/mid.py", depth="direct") == [
        "tests/test_lazy_in_test.py",
        "tests/test_via_helper.py",
    ]
    assert "tests/test_core.py" in selected(repo, "src/unified_agent/core.py")


def test_package_init_change_selects_everything_inside_it(repo):
    assert selected(repo, "src/unified_agent/tools/__init__.py") == ["tests/test_vault.py"]


def test_changed_test_and_helper_files(repo):
    assert selected(repo, "tests/test_unrelated.py") == ["tests/test_unrelated.py"]
    assert selected(repo, "tests/helper_mod.py") == ["tests/test_via_helper.py"]
    assert selected(repo, "tests/test_gone.py", status="D") == []


def test_name_convention_still_maps(repo):
    (repo / "tests" / "test_newmod.py").write_text("")
    # A new module nothing imports yet still maps to tests/test_<stem>.py.
    assert selected(repo, "src/unified_agent/newmod.py", status="A") == ["tests/test_newmod.py"]


def test_deleted_module_selects_the_files_still_importing_it(repo):
    (repo / "src" / "unified_agent" / "core.py").unlink()
    tests = selected(repo, "src/unified_agent/core.py", status="D")
    assert {"tests/test_core.py", "tests/test_vault.py"} <= set(tests)


def test_rename_covers_both_paths(repo):
    sel = at.select(
        repo, at.parse_name_status(["R090\tsrc/unified_agent/core.py\tsrc/unified_agent/mid.py"])
    )
    assert "tests/test_core.py" in sel.tests  # still imports the old name
    assert "tests/test_via_helper.py" in sel.tests  # depends on the new path


def test_path_references_reach_scripts_and_data_files(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "src/unified_agent/roster.py": 'NAME = "org.yaml"\n',
            "src/unified_agent/org.yaml": "",
            "scripts/check_thing.py": "from unified_agent import core\n",
            "scripts/run.sh": "",
            "deploy/unit.service": "",
            "tests/test_check_thing.py": (
                "from pathlib import Path\n"
                'SCRIPT = Path(__file__).parents[1] / "scripts" / "check_thing.py"\n'
            ),
            "tests/test_run_sh.py": 'RUN = "scripts/run.sh"\n',
            "tests/test_deploy_dir.py": 'UNITS = "deploy/unit.service"\n',
            "tests/test_roster.py": "from unified_agent import roster\n",
        },
    )
    # A src change reaches a test through a script that imports it.
    assert selected(repo, "src/unified_agent/core.py") == ["tests/test_check_thing.py"]
    assert selected(repo, "scripts/run.sh") == ["tests/test_run_sh.py"]
    assert selected(repo, "deploy/unit.service") == ["tests/test_deploy_dir.py"]
    # A packaged data file reaches the tests of the module that reads it.
    assert selected(repo, "src/unified_agent/org.yaml") == ["tests/test_roster.py"]


def test_directory_conftest_applies_to_its_subtree(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "tests/e2e/__init__.py": "",
            "tests/e2e/conftest.py": "from unified_agent import core\n",
            "tests/e2e/test_flow.py": "def test_x():\n    pass\n",
            "tests/test_other.py": "",
        },
    )
    assert selected(repo, "tests/e2e/conftest.py") == ["tests/e2e/test_flow.py"]
    assert selected(repo, "src/unified_agent/core.py") == ["tests/e2e/test_flow.py"]


def test_tree_scanning_tests_run_for_any_src_change(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "tests/test_scan.py": 'def test_x():\n    list(SRC.rglob("*.py"))\n',
        },
    )
    assert selected(repo, "src/unified_agent/core.py") == ["tests/test_scan.py"]


@pytest.mark.parametrize(
    "path",
    ["pyproject.toml", "uv.lock", "tests/conftest.py", "src/unified_agent/__init__.py"],
)
def test_files_every_test_depends_on_ask_for_the_full_suite(repo, path):
    sel = at.select(repo, [at.Change("M", path)])
    assert sel.full and path in sel.full_reasons[0]


def test_unmapped_file_asks_for_the_full_suite_but_docs_and_js_do_not(repo):
    (repo / "Makefile").write_text("")
    assert at.select(repo, [at.Change("M", "Makefile")]).full
    docs = at.select(repo, [at.Change("M", "docs/guide.md")])
    assert not docs.full and docs.docs_changed and docs.tests == []
    js = at.select(repo, [at.Change("M", "packages/web/src/app.ts")])
    assert not js.full and js.tests == []
    # Review finding: a Python test that reads a file in a JS package still runs.
    (repo / "tests" / "test_conformance_path.py").write_text(
        'FIXTURE = "packages/conformance/fixtures/path-decisions.json"\n'
    )
    fixture = at.select(repo, [at.Change("M", "packages/conformance/fixtures/path-decisions.json")])
    assert not fixture.full and fixture.tests == ["tests/test_conformance_path.py"]
    # Neither a directory prefix nor a bare common basename ties a module to a
    # package's sources (one that serves packages/web/dist, or names README.md).
    (repo / "tests" / "test_serves_dist.py").write_text(
        'DIST = "packages/web/dist"\nDOC = "README.md"\n'
    )
    for path in ("packages/web/dist/app.js", "packages/web/README.md"):
        assert at.select(repo, [at.Change("M", path)]).tests == []
    # A deleted file nothing names cannot break a test.
    assert not at.select(repo, [at.Change("D", "old/thing.txt")]).full


def test_inventory_lists_only_present_always_run_files(repo):
    assert at.select(repo, []).inventory == []
    (repo / at.ALWAYS_RUN[0]).write_text("")
    sel = at.select(repo, [at.Change("M", "tests/test_core.py")])
    assert sel.inventory == [at.ALWAYS_RUN[0]]
    assert sel.paths() == sorted(["tests/test_core.py", at.ALWAYS_RUN[0]])
    assert sel.paths(inventory=False) == ["tests/test_core.py"]


def test_unknown_depth_is_rejected(repo):
    with pytest.raises(ValueError, match="unknown depth"):
        at.select(repo, [], depth="deep")


def test_parse_name_status_and_reviewer_entries():
    assert at.parse_name_status(["M\ta.py", "R087\told.py\tnew.py", "", "bare/path.md"]) == [
        at.Change("M", "a.py"),
        at.Change("R087", "new.py", "old.py"),
        at.Change("M", "bare/path.md"),
    ]
    assert at.changes_from_entries(
        [{"file": "old.py\tnew.py", "status": "R100"}, {"file": "x.py"}, {"file": ""}]
    ) == [at.Change("R100", "new.py", "old.py"), at.Change("M", "x.py")]


def test_imports_are_read_from_the_syntax_tree(tmp_path):
    """Review finding: imports that do not start a physical line still count."""
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "src/unified_agent/typed.py": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from unified_agent import core\n"
            ),
            "tests/test_backslash.py": "from unified_agent.core \\\n    import X\n",
            "tests/test_inline.py": "import os; import unified_agent.core\n",
            "tests/test_relative_dynamic.py": (
                'import importlib\ncore = importlib.import_module(".core", "unified_agent")\n'
            ),
            "tests/test_dash_c.py": (
                "import subprocess, sys\n"
                "SCRIPT = 'import json\\n' + 'import unified_agent.core'\n"
                "def test_x():\n"
                '    subprocess.run([sys.executable, "-c", "from unified_agent import core"])\n'
                '    subprocess.run([sys.executable, "-I", "-c", SCRIPT])\n'
            ),
            "tests/test_git_dash_c.py": (
                "import subprocess\n"
                "def test_x(name):\n"
                '    subprocess.run(["git", "-c", name, "status"])\n'
            ),
            "tests/test_typed.py": "from unified_agent import typed\n",
        },
    )
    graph = at.ImportGraph(repo)
    assert not graph.unresolved  # `git -c` is not Python code
    assert selected(repo, "src/unified_agent/core.py") == [
        "tests/test_backslash.py",
        "tests/test_dash_c.py",
        "tests/test_inline.py",
        "tests/test_relative_dynamic.py",
        "tests/test_typed.py",  # a lazy edge out of the changed file is followed
    ]
    # An import under `if TYPE_CHECKING:` never runs, so it is only a lazy edge.
    assert "src/unified_agent/typed.py" in graph.lazy_dependents["src/unified_agent/core.py"]
    assert "src/unified_agent/typed.py" not in graph.eager_dependents["src/unified_agent/core.py"]


def test_dynamic_imports_that_cannot_be_read_are_never_dropped(tmp_path):
    """Review finding: a computed import_module() name or run-time -c code must
    not silently map to nothing."""
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "src/unified_agent/plugins.py": (
                "import importlib\ndef load(name):\n    return importlib.import_module(name)\n"
            ),
            "tests/test_computed.py": (
                "import importlib\n"
                "def test_x(name):\n"
                '    importlib.import_module(f"unified_agent.{name}")\n'
            ),
            "tests/test_runtime_code.py": (
                "import subprocess, sys\n"
                "def test_x(body):\n"
                '    subprocess.run([sys.executable, "-c", f"import {body}"])\n'
            ),
            "tests/test_unrelated.py": "",
        },
    )
    graph = at.ImportGraph(repo)
    assert set(graph.unresolved) == {
        "src/unified_agent/plugins.py",
        "tests/test_computed.py",
        "tests/test_runtime_code.py",
    }
    sel = at.select(repo, [at.Change("M", "src/unified_agent/core.py")])
    # Tests that could load anything run for every Python change...
    assert sel.tests == ["tests/test_computed.py", "tests/test_runtime_code.py"]
    # ...and a src module that could is reported, not silently ignored.
    assert len(sel.caveats) == 1 and sel.caveats[0].startswith("src/unified_agent/plugins.py")
    # A non-Python change does not drag them in.
    assert at.select(repo, [at.Change("M", "tests/test_unrelated.py")]).tests == [
        "tests/test_computed.py",
        "tests/test_runtime_code.py",
        "tests/test_unrelated.py",
    ]
    (repo / "notes.txt").write_text("")
    (repo / "tests" / "test_notes.py").write_text('N = "notes.txt"\n')
    assert at.select(repo, [at.Change("M", "notes.txt")]).tests == ["tests/test_notes.py"]


def test_deleted_test_and_script_helpers_select_their_importers(tmp_path):
    """Review finding: a deleted helper under tests/ or scripts/ still has users."""
    repo = make_repo(
        tmp_path,
        {
            "tests/test_helper_user.py": "from helper import VALUE\n",
            "tests/e2e/__init__.py": "",
            "tests/e2e/test_flow.py": "from .support import thing\n",
            "tests/test_script_user.py": ("import sys\ndef test_x():\n    import release_tool\n"),
            "tests/test_unrelated.py": "",
        },
    )
    assert selected(repo, "tests/helper.py", status="D") == ["tests/test_helper_user.py"]
    assert selected(repo, "tests/e2e/support.py", status="D") == ["tests/e2e/test_flow.py"]
    assert selected(repo, "scripts/release_tool.py", status="D") == ["tests/test_script_user.py"]
    renamed = at.select(repo, at.parse_name_status(["R100\ttests/helper.py\ttests/helpers.py"]))
    assert "tests/test_helper_user.py" in renamed.tests
    # A deleted Python file outside the graph's roots cannot be named: run everything.
    gone = at.select(repo, [at.Change("D", "src/other_pkg/mod.py")])
    assert gone.full and "src/other_pkg/mod.py" in gone.full_reasons[0]


# Shared fixture for the #2652 cases below.
_DECLARED_EDGE_REPO = {
    "src/unified_agent/components/manifest.py": "X = 1\n",
    "src/unified_agent/tools/__init__.py": "",
    "src/unified_agent/tools/thing.py": "X = 1\n",
    "src/unified_agent/dynamic.py": (
        'import importlib\n\nHELPER = "tests/helper.py"\n\n'
        "def load(name):\n    return importlib.import_module(name)\n"
    ),
    "tests/helper.py": "import importlib\n\ndef load(name):\n    return importlib.import_module(name)\n",
    "tests/test_components.py": "from unified_agent import components\n",
    "docs/architecture/components.toml": (
        'package = "unified_agent"\nsource = "../../src"\n\n'
        '[[component]]\nname = "kernel"\nmodules = ["dynamic"]\n'
    ),
}


def declared_edge_repo(tmp_path, components_init):
    files = {**_DECLARED_EDGE_REPO, "src/unified_agent/components/__init__.py": components_init}
    return make_repo(tmp_path, files)


def test_declared_component_edge_does_not_taint_unresolved_tests(tmp_path):
    """Task #2652: the declared edge keeps reachability but not unresolved taint.

    An unreadable import in a declared module must not mark every test that
    imports ``components`` as unownable, while a real change still reaches them.
    """
    repo = declared_edge_repo(tmp_path, "from . import manifest\n")
    sel = at.select(repo, [at.Change("M", "src/unified_agent/dynamic.py")])
    assert "tests/test_components.py" in sel.tests
    assert "tests/test_components.py" not in sel.unresolved_tests
    assert any("dynamic.py" in caveat for caveat in sel.caveats)


def test_real_import_that_coincides_with_a_declared_edge_still_taints(tmp_path):
    """Task #2652 review: only edges the map adds are declared, so a real import taints."""
    repo = declared_edge_repo(tmp_path, "from . import manifest\nfrom .. import dynamic\n")
    sel = at.select(repo, [at.Change("M", "src/unified_agent/tools/thing.py")])
    assert sel.unresolved_tests == ["tests/test_components.py"]


def test_qualified_reference_to_a_common_basename_is_rebuilt(tmp_path):
    """Review round 2: ``ROOT / "deploy" / "ci-runner" / "README.md"`` names that
    README even though a bare ``"README.md"`` is too common to count."""
    repo = make_repo(
        tmp_path,
        {
            "deploy/ci-runner/README.md": "",
            "deploy/other/README.md": "",
            "tests/test_ci_runner_assets.py": (
                "from pathlib import Path\n"
                "REPO_ROOT = Path(__file__).resolve().parents[1]\n"
                'DOC = REPO_ROOT / "deploy" / "ci-runner" / "README.md"\n'
            ),
            "tests/test_joined.py": (
                'import os\nDOC = os.path.join(os.getcwd(), "deploy/ci-runner", "README.md")\n'
            ),
            "tests/test_conformance_path.py": "",
        },
    )
    sel = at.select(
        repo,
        at.parse_name_status(
            ["M\tdeploy/ci-runner/README.md", "M\ttests/test_conformance_path.py"]
        ),
        depth="full",
    )
    assert sel.tests == [
        "tests/test_ci_runner_assets.py",
        "tests/test_conformance_path.py",
        "tests/test_joined.py",
    ]
    # The other README is not named by either test.
    assert selected(repo, "deploy/other/README.md") == []


def test_deleted_directory_conftest_selects_its_subtree(tmp_path):
    """Review round 2: a deleted tests/sub/conftest.py takes its fixtures from
    every test under tests/sub, even when nothing imports it."""
    repo = make_repo(
        tmp_path,
        {
            "tests/sub/test_flow.py": "def test_flow(value):\n    assert value\n",
            "tests/sub/deeper/test_more.py": "def test_more(value):\n    pass\n",
            "tests/test_elsewhere.py": "",
        },
    )
    assert selected(repo, "tests/sub/conftest.py", status="D", depth="full") == [
        "tests/sub/deeper/test_more.py",
        "tests/sub/test_flow.py",
    ]
    renamed = at.select(
        repo, at.parse_name_status(["R100\ttests/sub/conftest.py\ttests/sub/fixtures.py"])
    )
    assert renamed.tests == ["tests/sub/deeper/test_more.py", "tests/sub/test_flow.py"]


def test_dash_c_code_names_never_borrow_another_binding(tmp_path):
    """Review round 2: two tests that each bind ``code`` must not share one value."""
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "tests/test_two_scripts.py": (
                "import subprocess, sys\n"
                "def test_core():\n"
                '    code = "from unified_agent import core"\n'
                '    subprocess.run([sys.executable, "-c", code])\n'
                "def test_json():\n"
                '    code = "import json"\n'
                '    subprocess.run([sys.executable, "-c", code])\n'
            ),
            "tests/test_rebound.py": (
                "import subprocess, sys\n"
                "def test_x():\n"
                '    code = "import json"\n'
                '    code = "from unified_agent import core"\n'
                '    subprocess.run([sys.executable, "-c", code])\n'
            ),
            "tests/test_param.py": (
                "import subprocess, sys\n"
                'code = "import json"\n'
                "def run(code):\n"
                '    subprocess.run([sys.executable, "-c", code])\n'
            ),
            "tests/test_unrelated.py": "",
        },
    )
    graph = at.ImportGraph(repo)
    # Round 5: resolution is file-global. ``code`` bound in two functions has
    # two bindings in the file, so it never borrows either one's value; the
    # file is unresolved and runs on every change.
    assert set(graph.unresolved) == {
        "tests/test_two_scripts.py",
        "tests/test_rebound.py",
        "tests/test_param.py",
    }
    tests = selected(repo, "src/unified_agent/core.py", depth="full")
    assert "tests/test_two_scripts.py" in tests
    assert {"tests/test_rebound.py", "tests/test_param.py"} <= set(tests)
    assert "tests/test_unrelated.py" not in tests


def test_dangling_symlink_module_still_selects_its_importers(tmp_path):
    """Review round 2: a changed module that is now a broken symlink is still a
    changed module, not an absent one."""
    repo = make_repo(
        tmp_path,
        {
            "tests/test_consumer.py": "from unified_agent import linked\n",
            "tests/test_unrelated.py": "",
        },
    )
    (repo / "src" / "unified_agent" / "linked.py").symlink_to(repo / "missing-target.py")
    sel = at.select(repo, [at.Change("M", "src/unified_agent/linked.py")], depth="full")
    assert not sel.full
    assert "tests/test_consumer.py" in sel.tests
    assert any("linked.py" in caveat for caveat in sel.caveats)


_CORE_CODE = '"from unified_agent import core"'


@pytest.mark.parametrize(
    "rebinding",
    [
        f"for code in [{_CORE_CODE}]:\n        pass",
        f"code, other = {_CORE_CODE}, 1",
        f"(code := {_CORE_CODE})",
        "code += ''",
        "with open(__file__) as code:\n        pass",
        "try:\n        pass\n    except Exception as code:\n        pass",
        f"global code\n    code = {_CORE_CODE}",
        "del code",
        f"_ = [0 for code in [{_CORE_CODE}]]",
        "match 1:\n        case code:\n            pass",
        "import json as code",
        f"[*code] = [{_CORE_CODE}]",
    ],
)
def test_any_binding_but_one_plain_assignment_leaves_a_name_unresolved(tmp_path, rebinding):
    """Review round 3: only a single ``name = <static>`` in its scope resolves."""
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "tests/test_rebinds.py": (
                "import subprocess, sys\n"
                'code = "import json"\n'
                "def test_x():\n"
                f"    {rebinding}\n"
                '    subprocess.run([sys.executable, "-c", code])\n'
            ),
            "tests/test_unrelated.py": "",
        },
    )
    graph = at.ImportGraph(repo)
    assert "tests/test_rebinds.py" in graph.unresolved, graph.unresolved
    sel = at.select(repo, [at.Change("M", "src/unified_agent/core.py")])
    assert sel.tests == ["tests/test_rebinds.py"] == sel.unresolved_tests


def test_parameter_binding_leaves_a_name_unresolved(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "tests/test_param.py": (
                "import subprocess, sys\n"
                f"def test_x(code={_CORE_CODE}):\n"
                '    subprocess.run([sys.executable, "-c", code])\n'
            ),
        },
    )
    assert "tests/test_param.py" in at.ImportGraph(repo).unresolved


def test_string_references_use_decoded_values(tmp_path):
    """Review round 3: implicit concatenation is one decoded string; a
    unified_agent name completed at run time is unresolved."""
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "VALUE = 1\n",
            "tests/test_implicit.py": (
                "def test_x(monkeypatch):\n"
                '    monkeypatch.setattr("unified_agent." "core.VALUE", 2)\n'
            ),
            "tests/test_fstring.py": (
                "def test_x(monkeypatch, name):\n"
                '    monkeypatch.setattr(f"unified_agent.{name}.VALUE", 2)\n'
            ),
            "tests/test_plus.py": (
                "def test_x(monkeypatch, name):\n"
                '    monkeypatch.setattr("unified_agent." + name, 2)\n'
            ),
            "tests/test_format.py": (
                "def test_x(monkeypatch, name):\n"
                '    monkeypatch.setattr("unified_agent.{}.VALUE".format(name), 2)\n'
            ),
            "tests/test_docstring.py": '"""Mentions unified_agent.core in prose only."""\n',
        },
    )
    graph = at.ImportGraph(repo)
    assert set(graph.unresolved) == {
        "tests/test_fstring.py",
        "tests/test_plus.py",
        "tests/test_format.py",
    }
    sel = at.select(repo, [at.Change("M", "src/unified_agent/core.py")])
    assert "tests/test_implicit.py" in sel.tests  # a real reference, not a wildcard
    assert "tests/test_implicit.py" not in sel.unresolved_tests
    assert "tests/test_docstring.py" not in sel.tests  # docstrings run nothing


def test_dash_c_code_gets_the_same_analysis_as_a_file(tmp_path):
    """Review round 3: dynamic imports inside -c code are followed or flagged."""
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "tests/test_static_inside.py": (
                "import subprocess, sys\n"
                "CODE = \"import importlib; importlib.import_module('unified_agent.' + 'core')\"\n"
                "def test_x():\n"
                '    subprocess.run([sys.executable, "-c", CODE])\n'
            ),
            "tests/test_computed_inside.py": (
                "import subprocess, sys\n"
                "CODE = 'import importlib, sys; importlib.import_module(sys.argv[1])'\n"
                "def test_x():\n"
                '    subprocess.run([sys.executable, "-c", CODE, "unified_agent.core"])\n'
            ),
            "tests/test_dash_m.py": (
                "import subprocess, sys\n"
                "def test_x():\n"
                '    subprocess.run([sys.executable, "-m", "unified_agent.core"])\n'
            ),
        },
    )
    graph = at.ImportGraph(repo)
    assert set(graph.unresolved) == {"tests/test_computed_inside.py"}
    assert "python -c code: " in graph.unresolved["tests/test_computed_inside.py"][0]
    assert selected(repo, "src/unified_agent/core.py") == [
        "tests/test_computed_inside.py",
        "tests/test_dash_m.py",
        "tests/test_static_inside.py",
    ]


def test_every_loader_form_resolves_or_is_flagged(tmp_path):
    """Review round 3: keyword arguments, aliases and escaped loaders."""
    resolved = {
        "tests/test_kw_name.py": 'importlib.import_module(name="unified_agent.core")',
        "tests/test_dunder_kw.py": '__import__(name="unified_agent.core")',
        "tests/test_fromlist.py": '__import__("unified_agent", fromlist=["core"])',
        "tests/test_run_module.py": 'runpy.run_module(mod_name="unified_agent.core")',
    }
    flagged = {
        "tests/test_kw_computed.py": "importlib.import_module(name=n)",
        "tests/test_dunder_level.py": '__import__("core", globals(), None, [], 1)',
        "tests/test_run_module_computed.py": "runpy.run_module(mod_name=n)",
        "tests/test_run_path_computed.py": "runpy.run_path(path_name=n)",
        "tests/test_spec_computed.py": 'importlib.util.spec_from_file_location("m", location=n)',
        "tests/test_alias.py": "load(n)",
        "tests/test_escape.py": "loader = importlib.import_module",
        "tests/test_exec.py": "exec(n)",
        "tests/test_star_args.py": "importlib.import_module(*n)",
    }
    files = {"src/unified_agent/core.py": "", "tests/test_unrelated.py": ""}
    header = (
        "import importlib, importlib.util, runpy\nfrom importlib import import_module as load\n"
    )
    for rel, call in {**resolved, **flagged}.items():
        files[rel] = f"{header}def test_x(n):\n    {call}\n"
    repo = make_repo(tmp_path, files)
    graph = at.ImportGraph(repo)
    assert set(graph.unresolved) == set(flagged)
    tests = selected(repo, "src/unified_agent/core.py")
    assert set(resolved) | set(flagged) == set(tests)
    # A static run_path() is a path reference.
    (repo / "scripts").mkdir()
    (repo / "scripts" / "tool.py").write_text("")
    (repo / "tests" / "test_run_path.py").write_text(
        'import runpy\ndef test_x():\n    runpy.run_path(path_name="scripts/tool.py")\n'
    )
    assert "tests/test_run_path.py" in selected(repo, "scripts/tool.py")


def test_duplicate_helper_names_map_to_every_file(tmp_path):
    """Review round 3: tests/a/support.py and tests/b/support.py are both "support"."""
    repo = make_repo(
        tmp_path,
        {
            "tests/a/support.py": "X = 1\n",
            "tests/b/support.py": "Y = 1\n",
            "tests/a/test_a.py": "from support import X\n",
            "tests/b/test_b.py": "from support import Y\n",
            "tests/test_unrelated.py": "",
        },
    )
    expected = ["tests/a/test_a.py", "tests/b/test_b.py"]
    assert selected(repo, "tests/a/support.py") == expected
    assert selected(repo, "tests/b/support.py") == expected


def test_loader_aliases_are_known_before_any_use(tmp_path):
    """Review round 4: an alias imported below its first use still counts."""
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "tests/test_static_above.py": (
                "def test_x():\n"
                '    load("unified_agent.core")\n'
                "from importlib import import_module as load\n"
            ),
            "tests/test_computed_above.py": (
                "def test_x(n):\n    load(n)\nfrom importlib import import_module as load\n"
            ),
            "tests/test_stored_above.py": (
                "def test_x():\n    fn = load\nfrom importlib import import_module as load\n"
            ),
            "tests/test_unrelated.py": "",
        },
    )
    graph = at.ImportGraph(repo)
    assert set(graph.unresolved) == {"tests/test_computed_above.py", "tests/test_stored_above.py"}
    assert selected(repo, "src/unified_agent/core.py") == [
        "tests/test_computed_above.py",
        "tests/test_static_above.py",
        "tests/test_stored_above.py",
    ]


@pytest.mark.parametrize(
    "body",
    [
        "builtins.exec(n)",
        "builtins.eval(n)",
        "builtins.compile(n, 'x', 'exec')",
        "builtins.__import__(n)",
        "thing.exec(n)",
        "thing.exec()",
        "importlib.util.find_spec(n)",
        "fn = builtins.exec",
        "callbacks = [importlib.import_module]",
        "getattr(builtins, 'exec')(n)",
        "getattr(importlib, n)",
        "vars(builtins)",
        "__builtins__['exec']",
        "re = thing\n    re.compile(n)",
        "spec.loader.exec_module(module)",
        "loader.load_module()",
    ],
)
def test_qualified_and_indirect_loaders_are_flagged(tmp_path, body):
    """Review round 4: any callee whose name or final attribute is a loader
    identifier is a loader; any other reference to one is unresolved."""
    repo = make_repo(
        tmp_path,
        {
            "tests/test_loader.py": (
                "import builtins, importlib, importlib.util, re\n"
                "def test_x(n, thing, spec, module, loader):\n"
                f"    {body}\n"
            ),
        },
    )
    assert "tests/test_loader.py" in at.ImportGraph(repo).unresolved


def test_static_qualified_loaders_and_known_non_loaders_resolve(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "scripts/tool.py": "",
            "tests/test_builtins_exec.py": (
                "import builtins\n"
                "def test_x():\n"
                '    builtins.exec("from unified_agent import core")\n'
            ),
            "tests/test_spec_chain.py": (
                "import importlib.util\n"
                "from pathlib import Path\n"
                "ROOT = Path(__file__).resolve().parents[1]\n"
                'spec = importlib.util.spec_from_file_location("tool", ROOT / "scripts" / "tool.py")\n'
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
            ),
            "tests/test_find_spec.py": (
                "import importlib.util\n"
                'spec = importlib.util.find_spec("unified_agent.core")\n'
                "spec.loader.load_module()\n"
            ),
            "tests/test_regex.py": (
                "import re\nimport re as regex_mod\n"
                "def test_x(n):\n"
                "    re.compile(n)\n"
                "    regex_mod.compile(n)\n"
            ),
            "tests/test_unrelated.py": "",
        },
    )
    graph = at.ImportGraph(repo)
    assert graph.unresolved == {}
    assert selected(repo, "src/unified_agent/core.py") == [
        "tests/test_builtins_exec.py",
        "tests/test_find_spec.py",
    ]
    assert selected(repo, "scripts/tool.py") == ["tests/test_spec_chain.py"]


def test_bare_names_map_across_tests_scripts_and_src(tmp_path):
    """Review round 4: a bare name lands in every file with it, whatever the
    importer's namespace, since sys.path decides which one runs."""
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "src/unified_agent/vector.py": "",
            "tests/support.py": "X = 1\n",
            "scripts/support.py": "from unified_agent import core\n",
            "tests/test_scripts_support.py": (
                'import sys\nsys.path.insert(0, "scripts")\nimport support\n'
            ),
            "tests/test_bare_src.py": "import vector\n",
            "tests/test_unrelated.py": "",
        },
    )
    # A change to core reaches scripts/support.py and so the test that may load it.
    assert selected(repo, "src/unified_agent/core.py") == ["tests/test_scripts_support.py"]
    assert selected(repo, "tests/support.py") == ["tests/test_scripts_support.py"]
    assert selected(repo, "src/unified_agent/vector.py") == ["tests/test_bare_src.py"]


def test_explain_separates_conservative_tests_from_src_caveats(tmp_path, capsys):
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "src/unified_agent/plugins.py": (
                "import importlib\ndef load(n):\n    return importlib.import_module(n)\n"
            ),
            "tests/test_computed.py": "import importlib\ndef test_x(n):\n    importlib.import_module(n)\n",
        },
    )
    rc = cli.main(["--root", str(repo), "--files", "src/unified_agent/core.py", "--explain"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "test-side analysis is conservative: 1 of these run on every Python change" in err
    assert (
        "1 src module(s) with dynamic imports that cannot be read are reported, not followed" in err
    )
    assert "conservative by construction" not in err


def _consumer_repo(tmp_path, body, extra=None):
    return make_repo(
        tmp_path,
        {
            "tests/r5_target.py": "VALUE = 713\n",
            "tests/test_consumer.py": textwrap.dedent(body),
            **(extra or {}),
        },
    )


def _consumer_unresolved_and_selected(repo, change="tests/r5_target.py", status="M"):
    graph = at.ImportGraph(repo)
    assert "tests/test_consumer.py" in graph.unresolved, graph.unresolved
    sel = at.select(repo, [at.Change(status, change)], graph=graph, depth="full")
    assert "tests/test_consumer.py" in sel.tests


def test_loader_aliases_in_different_functions_are_ambiguous(tmp_path):
    """Review round 5, finding 1: one alias name bound to two loaders in two
    functions has two bindings in the file, so neither is trusted."""
    repo = _consumer_repo(
        tmp_path,
        """
        def test_import():
            from importlib import import_module as load
            return load("r5_target")
        def test_exec():
            from builtins import exec as load
            load("pass")
        """,
    )
    _consumer_unresolved_and_selected(repo)


@pytest.mark.parametrize(
    "body",
    [
        # Round 5, finding 2: a loader module reached through another name.
        """
        import builtins
        runtime = builtins
        def test_import(loader):
            getattr(runtime, loader)("import r5_target")
        """,
        # ...and a parameter named like a loader module elsewhere in the file.
        """
        import builtins
        def test_import(loader):
            getattr(builtins, loader)("import r5_target")
        def unrelated(builtins):
            return builtins
        """,
        # A computed getattr() on a loader submodule.
        """
        import importlib.util
        def test_import(name):
            getattr(importlib.util, name)("r5_target")
        """,
        # A loader module used other than as a direct static loader call.
        """
        import importlib
        def test_import():
            importlib.reload(importlib)
        """,
        # Round 6: pkgutil enumerates and imports modules dynamically.
        """
        import pkgutil
        def test_import():
            pkgutil.anything()
        """,
        # Round 6: zipimport enumerates and imports modules dynamically.
        """
        import zipimport
        def test_import():
            zipimport.anything()
        """,
        # Round 6: imp enumerates and imports modules dynamically.
        """
        import imp
        def test_import():
            imp.anything()
        """,
    ],
)
def test_loader_module_tokens_are_radioactive(tmp_path, body):
    _consumer_unresolved_and_selected(_consumer_repo(tmp_path, body))


def test_computed_getattr_is_flagged_only_on_loader_modules(tmp_path):
    """Round 5 relaxation 1: getattr(x, computed) is unresolved only when x is a
    loader-module token or a name bound to one. Finding 2 stays caught:
    ``runtime = builtins`` is itself a token use outside a direct call."""
    repo = _consumer_repo(
        tmp_path,
        """
        import builtins
        runtime = builtins
        def test_import(loader):
            getattr(runtime, loader)("import r5_target")
        """,
    )
    reasons = at.ImportGraph(repo).unresolved["tests/test_consumer.py"]
    assert any("builtins used other than in a direct static loader call" in r for r in reasons)
    assert any("fetched by a computed name" in r for r in reasons)
    _consumer_unresolved_and_selected(repo)
    # Honest code: a computed getattr on an ordinary object loads nothing, and
    # adversarial indirection through one is out of scope (module docstring).
    plain = make_repo(
        tmp_path / "plain",
        {"tests/test_plain.py": "def test_x(obj, name):\n    return getattr(obj, name)\n"},
    )
    assert at.ImportGraph(plain).unresolved == {}


def test_argument_names_resolve_from_one_binding_anywhere(tmp_path):
    """Round 5 relaxation 2: a loader argument name resolves when the whole
    file binds it exactly once, in any scope. Finding 3 stays caught: a module
    ``CODE`` and a function-local ``CODE`` are two bindings."""
    repo = make_repo(
        tmp_path,
        {
            "src/unified_agent/core.py": "",
            "tests/test_local_code.py": (
                "import subprocess, sys\n"
                "def test_x():\n"
                '    code = "from unified_agent import core"\n'
                '    subprocess.run([sys.executable, "-c", code])\n'
            ),
            "tests/test_module_and_local.py": (
                "import subprocess, sys\n"
                'CODE = "from unified_agent import core"\n'
                "def test_x():\n"
                '    subprocess.run([sys.executable, "-c", CODE])\n'
                "def test_y():\n"
                '    CODE = "pass"\n'
            ),
            "tests/test_unrelated.py": "",
        },
    )
    graph = at.ImportGraph(repo)
    assert set(graph.unresolved) == {"tests/test_module_and_local.py"}
    sel = at.select(repo, [at.Change("M", "src/unified_agent/core.py")])
    assert sel.tests == ["tests/test_local_code.py", "tests/test_module_and_local.py"]
    assert sel.unresolved_tests == ["tests/test_module_and_local.py"]


def test_default_argument_names_resolve_file_globally(tmp_path):
    """Review round 5, finding 3: a default runs in the enclosing scope, but a
    name bound twice anywhere in the file has no trusted value at all."""
    repo = _consumer_repo(
        tmp_path,
        """
        CODE = "import r5_target"
        def test_import(result=exec(CODE)):
            CODE = "pass"
        """,
    )
    _consumer_unresolved_and_selected(repo)


def test_only_textwrap_dedent_is_trusted(tmp_path):
    """Review round 5, finding 4: a function that happens to be named dedent
    is not textwrap.dedent."""
    repo = _consumer_repo(
        tmp_path,
        """
        def dedent(s):
            return "import " + s
        def test_import():
            exec(dedent("r5_target"))
        """,
    )
    _consumer_unresolved_and_selected(repo)
    for header, call in (
        ("from textwrap import dedent", "dedent"),
        ("import textwrap", "textwrap.dedent"),
    ):
        trusted = make_repo(
            tmp_path / call,
            {
                "tests/r5_target.py": "",
                "tests/test_consumer.py": (
                    f"{header}\n"
                    "def test_import():\n"
                    f'    exec({call}("""\n    import r5_target\n    """))\n'
                ),
            },
        )
        assert at.ImportGraph(trusted).unresolved == {}
        assert selected(trusted, "tests/r5_target.py") == ["tests/test_consumer.py"]


@pytest.mark.parametrize("path", ["src/unified_agent/r5_target.py", "scripts/r5_target.py"])
def test_deleted_module_keeps_its_bare_name_consumers(tmp_path, path):
    """Review round 5, finding 5: a deleted file maps to every name it could be
    imported as (dotted and bare), like a live one."""
    repo = make_repo(
        tmp_path,
        {path: "VALUE = 713\n", "tests/test_consumer.py": "import r5_target\n"},
    )
    assert selected(repo, path) == ["tests/test_consumer.py"]
    (repo / path).unlink()
    assert selected(repo, path, status="D") == ["tests/test_consumer.py"]


_EDGE, _UNRESOLVED, _NONE = "edge", "unresolved", "none"


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        # Round 6, finding 1: the flag through a name, and a combined short flag.
        (
            'FLAG = "-c"\ndef test_x():\n'
            '    subprocess.run([sys.executable, FLAG, "import r6_target"])\n',
            _EDGE,
        ),
        ('def test_x():\n    subprocess.run([sys.executable, "-Bc", "import r6_target"])\n', _EDGE),
        # The selector takes the rest of its token, and -m works combined too.
        ('def test_x():\n    subprocess.run([sys.executable, "-cimport r6_target"])\n', _EDGE),
        ('def test_x():\n    subprocess.run([sys.executable, "-Im", "r6_target"])\n', _EDGE),
        # -W/-X consume their argument before the selector.
        (
            "def test_x():\n    subprocess.run("
            '[sys.executable, "-W", "ignore", "-Xdev", "-c", "import r6_target"])\n',
            _EDGE,
        ),
        # An interpreter after a launcher, os.exec*l, shlex.split and a shell string.
        ('def test_x():\n    subprocess.run(["uv", "run", "python3", "-m", "r6_target"])\n', _EDGE),
        (
            'def test_x():\n    os.execl(sys.executable, sys.executable, "-c", "import r6_target")\n',
            _EDGE,
        ),
        (
            "def test_x():\n"
            "    subprocess.run(shlex.split(\"python3 -B -c 'import r6_target'\"))\n",
            _EDGE,
        ),
        ('def test_x():\n    subprocess.run("python3 -m r6_target", shell=True)\n', _EDGE),
        # pytest -p loads a plugin; -pNAME too.
        (
            'def test_x():\n    subprocess.run([sys.executable, "-m", "pytest", "-p", "r6_target"])\n',
            _EDGE,
        ),
        (
            "def test_x():\n    subprocess.run("
            '[sys.executable, "-m", "pytest", "-pr6_target", "-q"])\n',
            _EDGE,
        ),
        # Anything computed up to what runs, a splat, or a computed pytest argument.
        (
            'def test_x(flag):\n    subprocess.run([sys.executable, flag, "import r6_target"])\n',
            _UNRESOLVED,
        ),
        (
            "def test_x(opts):\n"
            '    subprocess.run([sys.executable, *opts, "-c", "import r6_target"])\n',
            _UNRESOLVED,
        ),
        ('def test_x(code):\n    subprocess.run([sys.executable, "-Bc", code])\n', _UNRESOLVED),
        ("def test_x(script):\n    subprocess.run([sys.executable, str(script)])\n", _UNRESOLVED),
        (
            "def test_x(extra):\n"
            '    subprocess.run([sys.executable, "-m", "pytest", "-p", extra])\n',
            _UNRESOLVED,
        ),
        # Round 6, finding 2's class: pytest_plugins that is not one static value.
        ("pytest_plugins = [name]\n", _UNRESOLVED),
        ("pytest_plugins = [*names]\n", _UNRESOLVED),
        ('pytest_plugins = ["r6_target"]\npytest_plugins += extra\n', _UNRESOLVED),
        # Round 7, finding 1: a process-spawn call binds its arguments the way
        # the real signature does, through any import of its module.
        ('def test_x():\n    subprocess.run(args="python3 -m r6_target", shell=True)\n', _EDGE),
        (
            "from subprocess import run\n"
            'def test_x():\n    run("python3 -m r6_target", shell=True)\n',
            _EDGE,
        ),
        ('def test_x():\n    os.popen("python3 -m r6_target")\n', _EDGE),
        (
            'def test_x():\n    os.execv(sys.executable, [sys.executable, "-m", "r6_target"])\n',
            _EDGE,
        ),
        (
            "import asyncio\nasync def test_x():\n"
            '    await asyncio.create_subprocess_exec(sys.executable, "-m", "r6_target")\n'
            '    await asyncio.create_subprocess_shell("python3 -m r6_target")\n',
            _EDGE,
        ),
        ('import pty\ndef test_x():\n    pty.spawn([sys.executable, "-m", "r6_target"])\n', _EDGE),
        # Round 7, finding 3: the script after -- is what runs.
        ('def test_x():\n    subprocess.run([sys.executable, "--", "r6_target.py"])\n', _EDGE),
        (
            'def test_x(script):\n    subprocess.run([sys.executable, "--", str(script)])\n',
            _UNRESOLVED,
        ),
        ('def test_x(code):\n    subprocess.run([sys.executable, "-"], input=code)\n', _UNRESOLVED),
        # Round 7, finding 2: a command that could start Python but is computed.
        (
            "def test_x():\n"
            '    module = "r6_target"\n'
            '    subprocess.run(f"python3 -m {module}", shell=True)\n',
            _UNRESOLVED,
        ),
        ('def test_x(tool):\n    subprocess.run([tool, "-m", "r6_target"])\n', _UNRESOLVED),
        ("def test_x(cmd):\n    subprocess.run(cmd)\n", _UNRESOLVED),
        # Aliases of process modules and spawn functions. A spawn function bound
        # once to a name makes calls through it spawn calls; any other use of a
        # process module or spawn function as a value stays unresolved.
        ("sp = subprocess\n", _UNRESOLVED),
        (
            'RUN = subprocess.run\ndef test_x():\n    RUN([sys.executable, "-m", "r6_target"])\n',
            _EDGE,
        ),
        ("RUN = subprocess.run\ndef test_x(cmd):\n    RUN(cmd)\n", _UNRESOLVED),
        ("RUN = subprocess.run\nRUN = subprocess.Popen\n", _UNRESOLVED),
        ("def test_x(helper):\n    helper(subprocess.run)\n", _UNRESOLVED),
        ("def test_x(name):\n    return getattr(os, name)\n", _UNRESOLVED),
        # Task #2243, gap 1: a spawn function fetched with getattr() (static or
        # computed name, on a process module or a chain from one), through a
        # module's __dict__, or from a process module loaded by name.
        ('def test_x(cmd):\n    getattr(subprocess, "run")(cmd)\n', _UNRESOLVED),
        ('def test_x():\n    getattr(os, "system")("python3 -m r6_target")\n', _EDGE),
        (
            'RUN = getattr(subprocess, "getoutput")\ndef test_x():\n    RUN("python3 -m r6_target")\n',
            _EDGE,
        ),
        ('def test_x(helper):\n    helper(getattr(os, "posix_spawn", None))\n', _UNRESOLVED),
        ("import asyncio\ndef test_x(name):\n    getattr(asyncio.subprocess, name)\n", _UNRESOLVED),
        ('def test_x(cmd):\n    subprocess.__dict__["run"](cmd)\n', _UNRESOLVED),
        # #2273: the default of a spawn getattr is still read for its imports.
        (
            "import importlib\ndef test_x():\n"
            '    getattr(subprocess, "run", importlib.import_module("r6_target"))(["true"])\n',
            _EDGE,
        ),
        # #2276: a default splatted in at run time cannot be read.
        ('def test_x(opts):\n    getattr(subprocess, "run", *opts)(["true"])\n', _UNRESOLVED),
        ('def test_x(kw):\n    getattr(subprocess, "run", **kw)(["true"])\n', _UNRESOLVED),
        ('def test_x(opts):\n    return getattr(subprocess, "PIPE", *opts)\n', _NONE),
        # #2286: a literal non-Python sh/plumbum command can only run that program;
        # a Python interpreter or a computed name behind the splat stays unresolved.
        ('import sh\ndef test_x(opts):\n    getattr(sh, "git", *opts)("status")\n', _NONE),
        ('import sh\ndef test_x(kw):\n    getattr(sh, "git", **kw)("status")\n', _NONE),
        (
            'import sh\ndef test_x(opts):\n    getattr(sh, "python3", *opts)("-m", "x")\n',
            _UNRESOLVED,
        ),
        ('import sh\ndef test_x(opts, n):\n    getattr(sh, n, *opts)("status")\n', _UNRESOLVED),
        # PR #1342 review: the namespace through a static getattr is the same read.
        ('def test_x(cmd):\n    getattr(subprocess, "__dict__")["run"](cmd)\n', _UNRESOLVED),
        (
            "import importlib\ndef test_x(cmd):\n"
            '    importlib.import_module("subprocess").run(cmd)\n',
            _UNRESOLVED,
        ),
        # Gap 2: sh and plumbum commands are spawns whose program is the name;
        # multiprocessing's child-interpreter knobs are read too. A spawn
        # function handed to a process pool stays unresolved.
        ('import sh\ndef test_x():\n    sh.python3("-m", "r6_target")\n', _EDGE),
        ('import sh\ndef test_x():\n    sh.python3("-", _in="import r6_target")\n', _EDGE),
        ("from sh import python3\ndef test_x(args):\n    python3(*args)\n", _UNRESOLVED),
        ('import sh\ndef test_x(prog):\n    sh.Command(prog)("-m", "r6_target")\n', _UNRESOLVED),
        ('import sh\nPY = sh.python3.bake("-m", "r6_target")\n', _UNRESOLVED),
        ('from plumbum.cmd import python3\ndef test_x():\n    python3("-m", "r6_target")\n', _EDGE),
        (
            'from plumbum import local\ndef test_x():\n    local["python3"]["-m", "r6_target"]()\n',
            _UNRESOLVED,
        ),
        (
            "import multiprocessing\n"
            'def test_x():\n    multiprocessing.set_forkserver_preload(["r6_target"])\n',
            _EDGE,
        ),
        # PR #1342 review: an imported alias of a knob is still that knob.
        (
            "from multiprocessing import set_forkserver_preload as preload\n"
            'preload(["r6_target"])\n',
            _EDGE,
        ),
        (
            "from multiprocessing import set_executable as use\ndef test_x(exe):\n    use(exe)\n",
            _UNRESOLVED,
        ),
        (
            "import multiprocessing\ndef test_x(names):\n"
            '    multiprocessing.get_context("forkserver").set_forkserver_preload(names)\n',
            _UNRESOLVED,
        ),
        (
            "import multiprocessing\ndef test_x(exe):\n    multiprocessing.set_executable(exe)\n",
            _UNRESOLVED,
        ),
        (
            "from concurrent.futures import ProcessPoolExecutor\ndef test_x():\n"
            '    ProcessPoolExecutor().submit(os.system, "python3 -m r6_target")\n',
            _UNRESOLVED,
        ),
        # Gap 3: a computed shell= reads the command both ways, without over-
        # splitting a list whose program is not a string.
        (
            "def test_x(flag):\n"
            '    subprocess.run([sys.executable, "-m", "r6_target"], shell=flag)\n',
            _EDGE,
        ),
        ('def test_x(flag):\n    subprocess.run(["python3 -m r6_target"], shell=flag)\n', _EDGE),
        (
            'def test_x(flag, code):\n    subprocess.run([sys.executable, "-c", code], shell=flag)\n',
            _UNRESOLVED,
        ),
        # Gap 4: python reading its script from a static input= runs that script;
        # any other stdin it reads a script from is unresolved.
        (
            'def test_x():\n    subprocess.run([sys.executable, "-"], input="import r6_target")\n',
            _EDGE,
        ),
        (
            "def test_x():\n"
            '    subprocess.run([sys.executable], input=b"from r6_target import VALUE")\n',
            _EDGE,
        ),
        (
            'SCRIPT = "import r6_target"\ndef test_x():\n'
            '    subprocess.check_output([sys.executable, "-I", "-"], input=SCRIPT.encode())\n',
            _EDGE,
        ),
        (
            "def test_x():\n    subprocess.Popen([sys.executable], stdin=subprocess.PIPE)\n",
            _UNRESOLVED,
        ),
        # -W eats the next element even when it looks like -c; git -c and a
        # disabled pytest plugin load nothing.
        (
            "def test_x():\n"
            '    subprocess.run([sys.executable, "-W", "-c", "import r6_target"])\n'
            '    subprocess.run(["git", "-c", "import r6_target", "status"])\n'
            '    subprocess.run([sys.executable, "-m", "pytest", "-p", "no:r6_target"])\n'
            '    subprocess.run(["git", "status"], check=True)\n'
            "def spawn() -> subprocess.Popen:\n"
            "    procs: list[subprocess.Popen] = []\n"
            "    return procs\n"
            "from unittest import mock\n"
            "def test_patches(monkeypatch):\n"
            '    monkeypatch.setattr(os, "killpg", None)\n'
            '    monkeypatch.delattr(subprocess, "run")\n'
            '    assert hasattr(os, "getpid") and getattr(os, "getpid")\n'
            '    with mock.patch.object(subprocess, "run"):\n'
            "        pass\n"
            # Task #2243: a fixed command that is not Python, however it is used;
            # an informational python option reads no script; data on stdin.
            "import sh\n"
            "from plumbum import local\n"
            "from plumbum.cmd import git\n"
            "def test_commands():\n"
            '    sh.git("status")\n'
            '    sh.git.bake("-C", "x")\n'
            "    try:\n"
            "        sh.ls()\n"
            "    except sh.ErrorReturnCode:\n"
            "        pass\n"
            '    git["status"]()\n'
            '    local["git"]("status")\n'
            '    subprocess.run([sys.executable, "--version"], input="")\n'
            '    subprocess.run([sys.executable, "-m", "r6_other"], input="data")\n',
            _NONE,
        ),
    ],
)
def test_python_entry_points_resolve_or_are_flagged(tmp_path, body, outcome):
    """Review round 6, findings 1 and 2 and their classes: any argv that runs
    Python is parsed with CPython's option grammar, and pytest_plugins must be
    one static value; anything else is unresolved, never dropped."""
    repo = make_repo(
        tmp_path,
        {
            "tests/r6_target.py": "VALUE = 713\n",
            "tests/test_consumer.py": "import os, shlex, subprocess, sys\n" + body,
            "tests/test_unrelated.py": "",
        },
    )
    graph = at.ImportGraph(repo)
    assert ("tests/test_consumer.py" in graph.unresolved) == (outcome == _UNRESOLVED), (
        graph.unresolved
    )
    expected = [] if outcome == _NONE else ["tests/test_consumer.py"]
    assert selected(repo, "tests/r6_target.py") == expected


def test_pytest_plugins_are_import_edges(tmp_path):
    """Review round 6, finding 2: pytest_plugins names modules pytest loads."""
    repo = make_repo(
        tmp_path,
        {
            "tests/r6_target.py": "",
            "tests/r6_other.py": "",
            "tests/test_list.py": 'pytest_plugins = ["r6_target", "r6_other"]\n',
            "tests/test_str.py": 'pytest_plugins = "r6_target"\n',
            "tests/sub/conftest.py": 'PLUGIN = "r6_other"\npytest_plugins = (PLUGIN,)\n',
            "tests/sub/test_in_sub.py": "",
            "tests/test_unrelated.py": "",
        },
    )
    assert at.ImportGraph(repo).unresolved == {}
    assert selected(repo, "tests/r6_target.py") == ["tests/test_list.py", "tests/test_str.py"]
    assert selected(repo, "tests/r6_other.py") == ["tests/sub/test_in_sub.py", "tests/test_list.py"]


def test_addopts_plugins_are_loaded_into_every_test(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-q -p r6_plugin -p no:cacheprovider"\n',
            "tests/r6_plugin.py": "from unified_agent import core\n",
            "src/unified_agent/core.py": "",
            "tests/test_unrelated.py": "",
        },
    )
    graph = at.ImportGraph(repo)
    assert graph.global_plugins == {"tests/r6_plugin.py"}
    sel = at.select(repo, [at.Change("M", "src/unified_agent/core.py")], graph=graph)
    assert sel.full and "addopts" in sel.full_reasons[0]


def test_importorskip_is_a_loader(tmp_path):
    """Review round 6, finding 3: pytest.importorskip imports its argument."""
    repo = make_repo(
        tmp_path,
        {
            "tests/r6_target.py": "",
            "tests/test_static.py": 'import pytest\nmod = pytest.importorskip("r6_target")\n',
            "tests/test_computed.py": (
                "import pytest\n"
                "@pytest.mark.parametrize('name', ['r6_target'])\n"
                "def test_x(name):\n"
                "    pytest.importorskip(name)\n"
            ),
            "tests/test_unrelated.py": "",
        },
    )
    graph = at.ImportGraph(repo)
    assert set(graph.unresolved) == {"tests/test_computed.py"}
    assert selected(repo, "tests/r6_target.py") == [
        "tests/test_computed.py",
        "tests/test_static.py",
    ]


# -- CLI ------------------------------------------------------------------------


def run_cli(capsys, *args: str) -> tuple[int, list[str], str]:
    rc = cli.main(list(args))
    out, err = capsys.readouterr()
    return rc, out.splitlines(), err


def test_cli_files_and_stdin(repo, capsys, monkeypatch):
    rc, lines, _ = run_cli(capsys, "--root", str(repo), "--files", "src/unified_agent/mid.py")
    assert rc == 0 and "tests/test_via_helper.py" in lines and "tests/test_core.py" not in lines
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("M\ttests/test_core.py\n"))
    rc, lines, err = run_cli(capsys, "--root", str(repo), "--from-stdin", "--explain")
    assert rc == 0 and lines == ["tests/test_core.py"]
    assert "1 of " in err and "depth transitive" in err
    assert at.SUBSET_NOTE in err and "batch CI (the full suite) is still required" in err


def test_cli_full_depth_and_unfollowed_imports_in_explain(repo, capsys):
    (repo / "src" / "unified_agent" / "plugins.py").write_text(
        "import importlib\ndef load(n):\n    return importlib.import_module(n)\n"
    )
    rc, lines, err = run_cli(
        capsys, "--root", str(repo), "--files", "src/unified_agent/core.py", "--explain"
    )
    assert rc == 0 and "not followed: src/unified_agent/plugins.py" in err
    rc, full_lines, err = run_cli(
        capsys, "--root", str(repo), "--files", "src/unified_agent/core.py", "--depth", "full"
    )
    assert rc == 0 and set(lines) < set(full_lines)


def test_cli_prints_the_all_sentinel(repo, capsys):
    rc, lines, err = run_cli(capsys, "--root", str(repo), "--files", "uv.lock", "--explain")
    assert rc == 0 and lines == [cli.FULL_SENTINEL]
    assert "uv.lock" in err


def test_cli_docs_only_change_adds_the_docs_scope(repo, capsys):
    (repo / "tests" / "test_reads_docs.py").write_text('DOC = "docs/guide.md"\n')
    (repo / "tests" / "test_mentions_readme.py").write_text("# README\n")
    rc, lines, _ = run_cli(capsys, "--root", str(repo), "--files", "docs/other.md")
    assert rc == 0 and lines == ["tests/test_mentions_readme.py", "tests/test_reads_docs.py"]
    # Mixed with code, a doc keeps only the tests that name it.
    rc, lines, _ = run_cli(
        capsys, "--root", str(repo), "--files", "docs/guide.md", "tests/test_core.py"
    )
    assert lines == ["tests/test_core.py", "tests/test_reads_docs.py"]


def test_cli_reads_the_branch_diff_and_uncommitted_changes(repo, capsys):
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
    (repo / "tests" / "test_core.py").write_text("# edited\n")
    (repo / "tests" / "test_new.py").write_text("")
    rc, lines, _ = run_cli(capsys, "--root", str(repo), "--base", "main")
    assert rc == 0 and lines == ["tests/test_core.py", "tests/test_new.py"]
    rc, _, err = run_cli(capsys, "--root", str(repo), "--base", "no-such-ref")
    assert rc == 2 and "git diff" in err


# -- the real tree -----------------------------------------------------------------


def test_always_run_inventory_tests_exist():
    missing = [p for p in at.ALWAYS_RUN if not (REPO_ROOT / p).is_file()]
    assert not missing, f"ALWAYS_RUN names missing files: {missing}; update the list"


def test_always_run_includes_the_component_and_budget_gates():
    """Finding F-05: both gates freeze a property of the whole tree."""
    assert "tests/test_component_check.py" in at.ALWAYS_RUN
    assert "tests/test_test_budget.py" in at.ALWAYS_RUN


def test_real_tree_reviewer_change_selects_its_pipeline_tests():
    sel = at.select(REPO_ROOT, [at.Change("M", "src/unified_agent/reviewer.py")], depth="direct")
    assert not sel.full
    assert "tests/test_reviewer_pipeline.py" in sel.tests
    assert set(at.ALWAYS_RUN) == set(sel.inventory)
