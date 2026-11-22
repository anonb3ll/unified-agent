"""Component manifests compose the hub's tool registry (kernel split K4, #2449).

Each component in docs/architecture/components.toml declares its MCP tools in
``unified_agent.components.<component>``; ``server.py`` registers the
concatenation of ``components.load()``. These tests hold the composition to
the surface it replaced: the same names (pinned by docs/tool-surface-baseline.json),
the same functions behind them (so the same schemas, annotations and guard),
each tool in exactly one manifest, and each manifest listing only tools defined
in its own component's modules.
"""

import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from unified_agent import components, server, tool_schema_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_components", REPO_ROOT / "scripts" / "check_components.py"
)
check_components = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check_components  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(check_components)

_CONFIG = check_components.load_config(check_components.DEFAULT_CONFIG)
_BASELINE = REPO_ROOT / "docs" / "tool-surface-baseline.json"


def _manifest_tools():
    return [(c.name, fn) for c in components.load() for fn in c.tools]


def _reaches(wrapped, fn) -> bool:
    # Stop at ``fn``: some tools (task_update) are themselves wrappers.
    return inspect.unwrap(wrapped, stop=lambda f: f is fn) is fn


def test_ua_components_selects_the_named_components_and_always_the_adapter(monkeypatch):
    """``UA_COMPONENTS`` is the load selection (K4b), not a tool filter."""
    monkeypatch.setenv(components.COMPONENTS_ENV, "kernel")
    assert components.selected() == ("kernel", "mcp-adapter")
    assert components.partial()
    monkeypatch.setenv(components.COMPONENTS_ENV, "github, node")
    assert components.selected() == ("node", "github", "mcp-adapter")


def test_ua_components_unset_or_blank_loads_every_component(monkeypatch):
    monkeypatch.delenv(components.COMPONENTS_ENV, raising=False)
    assert components.selected() == components.ORDER
    assert not components.partial()
    monkeypatch.setenv(components.COMPONENTS_ENV, "  ")
    assert components.selected() == components.ORDER


def test_ua_components_rejects_an_unknown_name(monkeypatch):
    """An unknown name stops start-up; it never serves a smaller surface."""
    monkeypatch.setenv(components.COMPONENTS_ENV, "kernel,no-such-component")
    with pytest.raises(ValueError, match="unknown component"):
        components.selected()
    with pytest.raises(ValueError, match="unknown component"):
        components.enabled("no-such-component")


def test_selection_loads_exactly_what_was_named_and_does_not_expand_depends_on(monkeypatch):
    """``depends_on`` is an import permission, not a load requirement.

    mcp-adapter declares all 15 other components; expanding its depends_on
    would load every component and make ``UA_COMPONENTS`` do nothing.
    """
    monkeypatch.setenv(components.COMPONENTS_ENV, "github")
    assert components.selected() == ("github", "mcp-adapter")
    assert [c.name for c in components.load()] == ["github", "mcp-adapter"]


_KERNEL_ONLY_PROBE = """
import json
import sys

import unified_agent.components as components
import unified_agent.server as server

manifests = sorted(name for name in sys.modules if name.startswith("unified_agent.components."))
print(
    "RESULT "
    + json.dumps(
        {
            "tools": list(server.REGISTERED_TOOLS),
            "manifests": manifests,
            "selected": list(components.selected()),
        }
    )
)
"""


def test_kernel_only_process_registers_only_kernel_and_adapter_tools():
    """K4b's acceptance test: the kernel boots with no component loaded.

    This is the test that catches a manifest edge lost silently. A process
    started with ``UA_COMPONENTS=kernel`` must import, register exactly the
    kernel's and the adapter's tools, and import no other component's manifest.
    If the kernel cannot boot with components unloaded, conditional loading is
    not done.
    """
    env = {**os.environ, components.COMPONENTS_ENV: "kernel"}
    env.pop("UA_TOOLSETS", None)
    probe = subprocess.run(
        [sys.executable, "-c", _KERNEL_ONLY_PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert probe.returncode == 0, probe.stderr[-4000:]
    result = json.loads(probe.stdout.split("RESULT ", 1)[1])
    assert result["selected"] == ["kernel", "mcp-adapter"]
    assert result["tools"] == [fn.__name__ for fn in components.tools(["kernel", "mcp-adapter"])]
    assert result["manifests"] == [
        "unified_agent.components.base",
        "unified_agent.components.kernel",
        "unified_agent.components.mcp_adapter",
    ]


def test_delegate_stays_natively_self_gated_without_dev_tools(monkeypatch):
    """A kernel-only process must not lose ``delegate``'s own approval gate.

    ``delegate`` is a kernel tool, so gating it behind the dev-tools component
    check would send it down the generic wrapper path instead of
    ``native_approval_execution`` — in exactly the mode the kernel-only test
    boots, and on the tool whose approval gate matters most.
    """
    from unified_agent import server
    from unified_agent.tools import delegate

    monkeypatch.setenv(components.COMPONENTS_ENV, "kernel")
    assert server._native_self_gated(delegate.delegate) is True
    assert server._native_self_gated(lambda: None) is False


def test_run_cmd_and_fs_write_are_self_gated_only_with_dev_tools(monkeypatch):
    """The dev-tools pair may only be compared while that component is loaded."""
    from unified_agent import server
    from unified_agent.tools import files, shell

    monkeypatch.setenv(components.COMPONENTS_ENV, "kernel")
    assert server._native_self_gated(shell.run_cmd) is False
    assert server._native_self_gated(files.fs_write) is False

    monkeypatch.delenv(components.COMPONENTS_ENV, raising=False)
    assert server._native_self_gated(shell.run_cmd) is True
    assert server._native_self_gated(files.fs_write) is True


def test_kernel_only_hub_keeps_the_health_probes(monkeypatch):
    """Deployment health checks must not depend on the Control Center loading.

    `/healthz` and `/readyz` were Control Center routes, so UA_COMPONENTS=kernel
    answered 404 on a hub whose MCP surface was fine (task #2603 review).
    """
    from unified_agent import server

    monkeypatch.setenv(components.COMPONENTS_ENV, "kernel")
    assert [route.path for route in server._health_routes()] == ["/healthz", "/readyz"]


def test_unreadable_component_map_declares_every_source_module(monkeypatch):
    """The fail-closed fallback must widen the graph, never empty it.

    A broken map has to leave `components` depending on every source module, so
    selection cannot shrink silently. `SRC_DIR` already names the package
    directory, so the fallback compares against `src/unified_agent/`, not
    `src/unified_agent/unified_agent/` (task #2603 review).
    """
    from unified_agent import affected_tests

    monkeypatch.setattr(affected_tests, "declared_component_modules", lambda root: None)
    graph = affected_tests.ImportGraph(REPO_ROOT)
    anchor = "src/unified_agent/components/__init__.py"
    for changed in ("src/unified_agent/tools/dash.py", "src/unified_agent/server.py"):
        assert anchor in graph.eager_dependents[changed], changed


def test_load_returns_the_sixteen_components_in_map_order():
    names = [c.name for c in components.load()]
    assert names == [c.name for c in _CONFIG.components]
    assert names == list(components.ORDER)
    assert sorted(names) == sorted(check_components.COMPONENT_NAMES)
    assert all(isinstance(c.tools, tuple) for c in components.load())


def test_load_filters_in_map_order_and_rejects_an_unknown_name():
    assert [c.name for c in components.load(["mcp-adapter", "kernel"])] == [
        "kernel",
        "mcp-adapter",
    ]
    assert components.load(["node"])[0].tools == ()
    with pytest.raises(ValueError, match="unknown component"):
        components.load(["kernel", "no-such-component"])


def test_registered_names_are_the_committed_tool_surface():
    committed = set(json.loads(_BASELINE.read_text()))
    manifest_names = {fn.__name__ for _, fn in _manifest_tools()}
    assert set(server.REGISTERED_TOOLS) == committed
    assert manifest_names == committed
    assert {t.name for t in server.mcp._tool_manager.list_tools()} == committed


def test_registration_order_is_component_order_then_manifest_order():
    expected = [fn.__name__ for fn in components.tools()]
    assert list(server.REGISTERED_TOOLS) == expected
    assert [t.name for t in server.mcp._tool_manager.list_tools()] == expected


def test_each_registered_tool_wraps_its_manifest_function():
    """Same function object behind every name, so the same schema and annotations."""
    advertised = {t.name: t for t in server.mcp._tool_manager._tools.values()}
    for _, fn in _manifest_tools():
        assert _reaches(server.REGISTERED_TOOLS[fn.__name__], fn), fn.__name__
        assert _reaches(advertised[fn.__name__].fn, fn), fn.__name__
    bare = {fn.__name__: fn for _, fn in _manifest_tools()}
    assert server.registry_schema_fingerprint() == (
        tool_schema_fingerprint.compute_schema_fingerprint(bare)
    )


def test_no_tool_is_listed_in_two_manifests():
    seen: dict[str, str] = {}
    for component, fn in _manifest_tools():
        assert fn.__name__ not in seen, f"{fn.__name__} in {seen.get(fn.__name__)} and {component}"
        seen[fn.__name__] = component
    assert len(seen) == len(server.REGISTERED_TOOLS)


def test_every_tool_is_defined_in_a_module_of_its_own_component():
    placed = check_components.check(_CONFIG).modules
    wrong = []
    for component, fn in _manifest_tools():
        module = inspect.unwrap(fn).__module__.removeprefix("unified_agent.")
        if placed.get(module) != component:
            wrong.append(
                f"{fn.__name__}: manifest {component}, {module} is in {placed.get(module)}"
            )
    assert not wrong, "\n".join(wrong)


def test_server_names_no_tool_function():
    """The central tuple is gone: a new tool goes in its component's manifest."""
    source = (REPO_ROOT / "src" / "unified_agent" / "server.py").read_text()
    entry = re.compile(r"^\s+\w+\.(\w+),$", re.MULTILINE)
    named = sorted(set(entry.findall(source)) & set(server.REGISTERED_TOOLS))
    assert not named, f"server.py names tool functions directly: {named}"


def test_manifest_imports_are_static_so_a_tool_change_still_selects_server_tests():
    """A manifest loaded lazily or by a computed name hides server's edge to every tool.

    affected_tests follows only plain module-level imports. With
    `importlib.import_module(f"...{name}")`, or a `from . import x` inside a function, a
    change to tools/dash.py selected 135 test files instead of 319 and dropped the
    tool-surface and server tests.
    """
    edges = check_components.check(_CONFIG).edges
    manifests = {f"components.{name.replace('-', '_')}" for name in components.ORDER}
    assert manifests <= edges["components"]
    assert "components" in edges["server"]

    from unified_agent import affected_tests

    changes = affected_tests.parse_name_status(["src/unified_agent/tools/dash.py"])
    selection = affected_tests.select(REPO_ROOT, changes)
    assert "tests/test_tool_surface.py" in set(selection.tests)


def _graph_without_manifest_imports(declared_edges):
    """The graph as a computed or conditional manifest load would leave it.

    A manifest loaded by a computed name, or imported only when the component is
    selected, makes no static import of its tools; dropping every manifest from
    the dependents maps is that state, without touching product code.
    """
    from unified_agent import affected_tests

    graph = affected_tests.ImportGraph(REPO_ROOT, declared_edges=declared_edges)
    manifests = {
        f"src/unified_agent/components/{name.replace('-', '_')}.py" for name in components.ORDER
    }
    for table in (graph.eager_dependents, graph.lazy_dependents):
        for dependents in table.values():
            dependents -= manifests
    return graph


def test_declared_component_edges_keep_selection_whole_without_a_static_manifest():
    """K4b's prerequisite (task #2562), proved before anything becomes conditional.

    Removing the manifest -> tool imports is what a conditional or computed
    manifest load does to the graph. With the edge the component map declares,
    the selection is the same set as with static manifests; without it, it
    silently shrinks and drops the tool-surface and server tests — the 135-instead
    of-319 failure this card exists to prevent.
    """
    from unified_agent import affected_tests

    changes = affected_tests.parse_name_status(["src/unified_agent/tools/dash.py"])
    static = set(affected_tests.select(REPO_ROOT, changes).tests)
    declared = set(
        affected_tests.select(REPO_ROOT, changes, graph=_graph_without_manifest_imports(True)).tests
    )
    undeclared = set(
        affected_tests.select(
            REPO_ROOT, changes, graph=_graph_without_manifest_imports(False)
        ).tests
    )

    assert declared == static, "the declared edge must substitute for the static manifest import"
    assert undeclared < declared, "without the declared edge the loss must be visible, not silent"
    assert "tests/test_tool_surface.py" in declared
    assert "tests/test_tool_surface.py" not in undeclared
