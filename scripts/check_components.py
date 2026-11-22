"""Check that src/unified_agent imports respect the component map. Stdlib only.

The map lives in ``docs/architecture/components.toml``: a list of components,
each a named set of modules (exact names or globs) that ships one feature
family, with the components it may import (``depends_on``). This is the
vertical axis beside the layer map (``check_layers.py``); both checks run.

Rules (docs/architecture/kernel-split-spec.md, K1):

1. Every module is claimed by exactly one component. Exact names beat globs.
2. An import from A to B is allowed when they share a component, or when B's
   component is in the transitive ``depends_on`` of A's.
3. ``kernel`` has ``depends_on = []``. ``mcp-adapter``, the composition root,
   appears in no other component's ``depends_on``.
4. The ``depends_on`` graph has no cycle.
5. Every other import is a violation. The violations that existed when the
   map was drawn are listed in the baseline file, which only ever shrinks.

The check fails with one of these verdicts:

* ``UNMAPPED``: a module is in no component.
* ``AMBIGUOUS``: two globs in different components claim a module.
* ``NEW``: an import crosses components without a declared dependency and is
  not in the baseline.
* ``FIXED``: a baseline line no longer matches a violation; delete it.
* ``UNUSED``: a pattern matches no module.
* ``CYCLE``: the ``depends_on`` graph has a cycle.
* ``INVALID``: rule 3 is broken, or a ``depends_on`` names no component.

The import reader is the one in ``check_layers.py``: every ``import``
statement counts, including lazy ones and those under ``if TYPE_CHECKING:``.

Test ownership (K2, #2448). Every test file under ``tests/`` has an owner in
``tests/component-owners.txt``, computed from the source modules it depends on
*directly*: its own imports and string references, plus those of the test
helpers, scripts, package ``__init__`` files and directory conftests it pulls
in (the root ``tests/conftest.py`` is excluded, as in affected_tests). The
graph is the one ``unified_agent.affected_tests`` builds; src-to-src edges are
not followed. With N the non-kernel components those modules belong to,
``mcp-adapter`` left out:

* the file, or a helper it reaches, is marked unresolved -> ``unresolved``
* N empty and no mcp-adapter module -> ``kernel``
* N empty, some mcp-adapter module -> ``mcp-adapter``
* one component in N -> that component
* two or more -> ``integration:<a>+<b>+...``, sorted

A test that imports ``server`` (the composition root, which registers every
tool family) or scans the whole source tree as text reaches every component,
so its N is all 14 components that are neither kernel nor mcp-adapter.
mcp-adapter is never named in an ``integration:`` owner: a change to it runs
the full suite anyway (spec, K3).

The check adds two verdicts:

* ``UNOWNED``: a test file has no line in the owners file.
* ``STALE_OWNERS``: a line's owner, or the file it names, no longer matches
  what the graph gives; run ``--write-owners``.

Usage:

  uv run python scripts/check_components.py              # check, exit 1 on failure
  uv run python scripts/check_components.py --report     # markdown summary
  uv run python scripts/check_components.py --write-baseline   # after reclassifying
  uv run python scripts/check_components.py --write-owners     # after tests change
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import time
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "docs" / "architecture" / "components.toml"
KERNEL = "kernel"
COMPOSITION_ROOT = "mcp-adapter"
# The module that loads every component: a test that imports it reaches them all.
COMPOSITION_MODULE = "server"
UNRESOLVED = "unresolved"
INTEGRATION = "integration:"
OWNERS_FILE = Path("tests") / "component-owners.txt"
_TEST_DEF_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+test_", re.MULTILINE)
# The fixed component names (kernel-split spec, "Vocabulary"). Adding or
# renaming one is a change to the spec; tests/test_component_check.py pins it.
COMPONENT_NAMES = (
    "kernel",
    "mcp-adapter",
    "control-center",
    "dispatch",
    "node",
    "github",
    "chat-bridges",
    "audit",
    "telemetry",
    "llm",
    "retrieval",
    "cloud",
    "auth",
    "knowledge-jobs",
    "skills",
    "dev-tools",
)


# Two helpers loaded by path: the layer checker (module discovery, import reader
# and glob classifier, so both axes read one graph) and the test graph in
# src/unified_agent/affected_tests.py (stdlib only; this script only reads it).
# Each is loaded once per process, through module-level specs on static paths
# so that affected_tests itself can follow these loads.
_LAYERS_SPEC = importlib.util.spec_from_file_location(
    "ua_check_layers", REPO_ROOT / "scripts" / "check_layers.py"
)
_AFFECTED_SPEC = importlib.util.spec_from_file_location(
    "ua_affected_tests", REPO_ROOT / "src" / "unified_agent" / "affected_tests.py"
)
_layers_fresh = importlib.util.module_from_spec(_LAYERS_SPEC)
_affected_fresh = importlib.util.module_from_spec(_AFFECTED_SPEC)
# dataclasses resolve annotations via sys.modules, so register before running.
layers = sys.modules.setdefault("ua_check_layers", _layers_fresh)
affected_tests = sys.modules.setdefault("ua_affected_tests", _affected_fresh)
if layers is _layers_fresh:
    _LAYERS_SPEC.loader.exec_module(_layers_fresh)
if affected_tests is _affected_fresh:
    _AFFECTED_SPEC.loader.exec_module(_affected_fresh)


@dataclass
class Component:
    name: str
    description: str
    depends_on: list[str]
    certifiable: bool
    patterns: list[str]


@dataclass
class Config:
    package: str
    source: Path
    baseline: Path
    components: list[Component]


@dataclass
class Ownership:
    """Each test file's owner and the components behind it (module docstring)."""

    owners: dict[str, str] = field(default_factory=dict)  # tests/x.py -> owner
    components: dict[str, set[str]] = field(default_factory=dict)  # tests/x.py -> touched
    test_counts: dict[str, int] = field(default_factory=dict)  # tests/x.py -> def test_ lines
    whole: set[str] = field(default_factory=set)  # import server or scan the tree
    seconds: float = 0.0  # time to build the affected_tests graph


@dataclass
class Result:
    modules: dict[str, str] = field(default_factory=dict)  # module -> component
    lines: dict[str, int] = field(default_factory=dict)  # module -> line count
    edges: dict[str, set[str]] = field(default_factory=dict)
    violations: set[tuple[str, str]] = field(default_factory=set)
    new: list[tuple[str, str]] = field(default_factory=list)
    stale: list[tuple[str, str]] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    unused_patterns: list[tuple[str, str]] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    ownership: Ownership | None = None  # None when not computed or no tests/ to own
    unowned: list[str] = field(default_factory=list)
    stale_owners: list[str] = field(default_factory=list)  # "path: committed -> expected"

    @property
    def ok(self) -> bool:
        return not (
            self.unowned
            or self.stale_owners
            or self.new
            or self.stale
            or self.unmapped
            or self.ambiguous
            or self.unused_patterns
            or self.cycles
            or self.invalid
        )


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text())
    root = path.parent
    components = [
        Component(
            name=item["name"],
            description=item.get("description", ""),
            depends_on=list(item.get("depends_on", [])),
            certifiable=bool(item.get("certifiable", False)),
            patterns=list(item["modules"]),
        )
        for item in data["component"]
    ]
    return Config(
        package=data["package"],
        source=(root / data["source"]).resolve(),
        baseline=(root / data["baseline"]).resolve(),
        components=components,
    )


def validate(components: list[Component]) -> list[str]:
    """Rule 3 and dangling names. Cycles are reported apart (``find_cycles``)."""
    problems: list[str] = []
    names = [c.name for c in components]
    for name, count in Counter(names).items():
        if count > 1:
            problems.append(f"component {name!r} is declared {count} times")
    known = set(names)
    for comp in components:
        for dep in comp.depends_on:
            if dep not in known:
                problems.append(f"{comp.name} depends on unknown component {dep!r}")
            if dep == COMPOSITION_ROOT:
                problems.append(
                    f"{comp.name} depends on {COMPOSITION_ROOT}, the composition root; "
                    "nothing may depend on it"
                )
            if dep == comp.name:
                problems.append(f"{comp.name} lists itself in depends_on")
        if comp.name == KERNEL and comp.depends_on:
            problems.append(f"{KERNEL} must have depends_on = [] (has {comp.depends_on})")
    return problems


def find_cycles(components: list[Component]) -> list[list[str]]:
    """Each cycle in the depends_on graph once, as a path that returns to its start."""
    graph = {c.name: [d for d in c.depends_on if d != c.name] for c in components}
    cycles: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    state: dict[str, int] = {}  # 1 = on the stack, 2 = done
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for dep in graph.get(node, []):
            if state.get(dep) == 1:
                loop = stack[stack.index(dep) :]
                if frozenset(loop) not in seen:
                    seen.add(frozenset(loop))
                    cycles.append([*loop, dep])
            elif dep in graph and state.get(dep) is None:
                visit(dep)
        stack.pop()
        state[node] = 2

    for name in graph:
        if state.get(name) is None:
            visit(name)
    return cycles


def closure(components: list[Component]) -> dict[str, set[str]]:
    """Each component's transitive depends_on (cycle-safe)."""
    direct = {c.name: set(c.depends_on) for c in components}
    out: dict[str, set[str]] = {}
    for name in direct:
        reach: set[str] = set()
        todo = list(direct[name])
        while todo:
            dep = todo.pop()
            if dep not in reach:
                reach.add(dep)
                todo.extend(direct.get(dep, ()))
        reach.discard(name)
        out[name] = reach
    return out


def write_baseline(path: Path, violations: set[tuple[str, str]]) -> None:
    lines = [
        "# Imports that cross components without a declared depends_on, as they",
        "# stood when the component map was drawn (docs/architecture/components.md).",
        "# Generated by `scripts/check_components.py --write-baseline`. This list only",
        "# shrinks: remove an edge, then delete its line (the check fails until you",
        "# do). Never add a line by hand to make a new edge pass.",
        "",
    ]
    lines += [f"{src} -> {dst}" for src, dst in sorted(violations)]
    path.write_text("\n".join(lines) + "\n")


def repo_root(config: Config) -> Path:
    """The checkout: the directory holding ``source`` (``src``) and ``tests``."""
    return config.source.parent


def declare_component_edges(
    edges: dict[str, set[str]], modules: dict[str, Path], config: Config
) -> None:
    """``components`` depends on every module the component map declares (K4b).

    The manifests are no longer imported by a static block in
    ``components/__init__.py``: each one is loaded by name when the process
    selects it (task #2603). The map carries that edge instead, and
    ``affected_tests`` reads the same map for selection, so the checker and the
    selector cannot disagree about what ``components`` reaches.

    An unreadable map fails closed here too: every module is declared, which
    matches what ``affected_tests`` does.
    """
    anchor = "components"
    if anchor not in modules:
        return
    root = repo_root(config)
    by_path = {path.resolve(): name for name, path in modules.items()}
    declared = affected_tests.declared_component_modules(root)
    if declared is None:
        edges[anchor] |= {name for name in modules if name != anchor}
        return
    for rel in declared:
        name = by_path.get((root / rel).resolve())
        if name and name != anchor:
            edges[anchor].add(name)


def owner_of(touched: set[str]) -> str:
    """The owner of a test whose direct dependencies span ``touched`` components."""
    rest = touched - {KERNEL, COMPOSITION_ROOT}
    if not rest:
        return COMPOSITION_ROOT if COMPOSITION_ROOT in touched else KERNEL
    if len(rest) == 1:
        return next(iter(rest))
    return INTEGRATION + "+".join(sorted(rest))


def compute_ownership(config: Config, modules: dict[str, str]) -> Ownership | None:
    """Every test file's owner, from one build of the affected_tests graph."""
    root = repo_root(config)
    at = affected_tests
    if config.package != at.PACKAGE or not (root / "tests").is_dir():
        return None
    started = time.perf_counter()
    graph = at.ImportGraph(root)
    seconds = time.perf_counter() - started
    src_prefix = at.SRC_DIR + "/"
    pkg_prefix = at.PACKAGE + "."
    root_conftest = "tests/conftest.py"
    everything = set(modules.values()) - {KERNEL, COMPOSITION_ROOT}

    # Files named by a path string ("scripts/x.py", "unified_agent/y.py"): the
    # whole path or trailing segments with a directory in them. A bare
    # basename ("x.py") is too loose for ownership (scripts/affected_tests.py
    # and src/unified_agent/affected_tests.py share one), and a directory
    # prefix ("src/unified_agent") would tie the test to every module.
    by_path: dict[str, set[str]] = defaultdict(set)
    for rel in graph.nodes:
        if not at.is_test_file(rel):
            parts = rel.split("/")
            for i in range(len(parts) - 1):
                for referrer in graph.constant_index.get("/".join(parts[i:]), ()):
                    by_path[referrer].add(rel)

    def direct(rel: str) -> set[str]:
        found = set(by_path.get(rel, ()))
        for name in graph.nodes[rel].imports:
            found |= graph._resolve(rel, name)
        found.discard(rel)
        return found

    def conftests(rel: str) -> list[str]:
        parts = rel.split("/")[:-1]
        candidates = ("/".join(parts[:i]) + "/conftest.py" for i in range(2, len(parts) + 1))
        return [c for c in candidates if c in graph.nodes]

    out = Ownership(seconds=seconds)
    for test in sorted(graph.test_files):
        text = (root / test).read_text(errors="replace")
        out.test_counts[test] = len(_TEST_DEF_RE.findall(text))
        queue = [test, *conftests(test), *graph._parent_packages(test)]
        seen = set(queue)
        src: set[str] = set()
        unresolved = False
        while queue:
            rel = queue.pop()
            unresolved |= rel in graph.unresolved
            for target in direct(rel):
                if target.startswith(src_prefix):
                    src.add(target)
                elif target not in seen and target != root_conftest:
                    seen.add(target)
                    queue.append(target)
        names = {
            graph.nodes[rel].module.removeprefix(pkg_prefix)
            for rel in src
            if graph.nodes[rel].module.startswith(pkg_prefix)
        }
        touched = {modules[n] for n in names if n in modules}
        if COMPOSITION_MODULE in names or test in graph.tree_scanners:
            out.whole.add(test)
            touched |= everything
        out.components[test] = touched
        out.owners[test] = UNRESOLVED if unresolved else owner_of(touched)
    return out


def render_owners(owners: dict[str, str]) -> str:
    lines = [
        "# Owner of every test file: the components whose modules it uses directly.",
        "# Generated by `scripts/check_components.py --write-owners`; do not edit by",
        '# hand. Rules: docs/architecture/components.md, "Test ownership".',
        "",
    ]
    lines += [f"{test} {owner}" for test, owner in sorted(owners.items())]
    return "\n".join(lines) + "\n"


def read_owners(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    owners: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            test, _, owner = line.partition(" ")
            owners[test] = owner.strip()
    return owners


def compare_owners(
    committed: dict[str, str], expected: dict[str, str]
) -> tuple[list[str], list[str]]:
    """(unowned test files, stale lines) of the committed map against the generated one."""
    unowned = sorted(set(expected) - set(committed))
    stale = [
        f"{test}: {owner} -> {expected.get(test, 'no such test file')}"
        for test, owner in sorted(committed.items())
        if expected.get(test) != owner
    ]
    return unowned, stale


def check(config: Config, *, owners: bool = True) -> Result:
    found = layers.discover(config.source, config.package)
    assigned, unmapped, ambiguous, unused = layers.classify(found, config.components)
    edges = layers.build_edges(found, config.package)
    declare_component_edges(edges, found, config)
    reach = closure(config.components)
    violations = {
        (src, dst)
        for src, targets in edges.items()
        for dst in targets
        if src in assigned
        and dst in assigned
        and assigned[src] != assigned[dst]
        and assigned[dst] not in reach.get(assigned[src], set())
    }
    baseline = layers.read_baseline(config.baseline)
    ownership = compute_ownership(config, assigned) if owners else None
    unowned: list[str] = []
    stale_owners: list[str] = []
    if ownership is not None:
        committed = read_owners(repo_root(config) / OWNERS_FILE)
        unowned, stale_owners = compare_owners(committed, ownership.owners)
    return Result(
        modules=assigned,
        lines={m: len(p.read_text().splitlines()) for m, p in found.items()},
        edges=edges,
        violations=violations,
        new=sorted(violations - baseline),
        stale=sorted(baseline - violations),
        unmapped=sorted(unmapped),
        ambiguous=ambiguous,
        unused_patterns=unused,
        cycles=find_cycles(config.components),
        invalid=validate(config.components),
        ownership=ownership,
        unowned=unowned,
        stale_owners=stale_owners,
    )


def report(config: Config, result: Result) -> str:
    names = [c.name for c in config.components]
    sizes = Counter(result.modules.values())
    lines: Counter[str] = Counter()
    for module, comp in result.modules.items():
        lines[comp] += result.lines.get(module, 0)
    out_v = Counter(result.modules[s] for s, _ in result.violations)
    in_v = Counter(result.modules[d] for _, d in result.violations)
    out = [
        "| Component | Modules | Lines | Undeclared imports out | Undeclared importers in |",
        "|---|---|---|---|---|",
    ]
    for name in names:
        out.append(f"| {name} | {sizes[name]} | {lines[name]:,} | {out_v[name]} | {in_v[name]} |")
    out.append(
        f"| **total** | {sum(sizes.values())} | {sum(lines.values()):,} "
        f"| {len(result.violations)} | {len(result.violations)} |"
    )
    kernel_out = Counter(
        result.modules[d] for s, d in result.violations if result.modules[s] == KERNEL
    )
    out += ["", f"Kernel outbound edges: {sum(kernel_out.values())}", ""]
    if kernel_out:
        out += ["| Target component | Edges |", "|---|---|"]
        for comp, count in sorted(kernel_out.items(), key=lambda kv: (-kv[1], kv[0])):
            out.append(f"| {comp} | {count} |")
    clean = [n for n in names if not in_v[n]]
    out += ["", "Components with no undeclared importer: " + (", ".join(clean) or "none")]
    pairs = Counter((result.modules[s], result.modules[d]) for s, d in result.violations)
    if pairs:
        out += ["", "Undeclared edges by component pair:", ""]
        out += ["| From | To | Edges |", "|---|---|---|"]
        for (src, dst), count in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0])):
            out.append(f"| {src} | {dst} | {count} |")
    if result.ownership is not None:
        out += ["", *ownership_report(result.ownership)]
    return "\n".join(out)


def _single(owner: str) -> bool:
    return not owner.startswith(INTEGRATION) and owner != UNRESOLVED


def ownership_report(own: Ownership, top: int = 20) -> list[str]:
    files: Counter[str] = Counter()
    tests: Counter[str] = Counter()
    for test, owner in own.owners.items():
        key = INTEGRATION + "*" if owner.startswith(INTEGRATION) else owner
        files[key] += 1
        tests[key] += own.test_counts[test]
    total_files, total_tests = sum(files.values()), sum(tests.values())
    single_files = sum(n for k, n in files.items() if _single(k))
    single_tests = sum(n for k, n in tests.items() if _single(k))

    def pct(part: int, whole: int) -> str:
        return f"{100 * part / whole:.1f}%" if whole else "n/a"

    out = [
        f"Test ownership: {total_files} files, {total_tests:,} test functions "
        f"(graph built in {own.seconds:.1f} s).",
        "",
        "| Owner | Files | Tests |",
        "|---|---|---|",
    ]
    for key in sorted(files, key=lambda k: (-tests[k], k)):
        out.append(f"| {key} | {files[key]} | {tests[key]:,} |")
    out.append(f"| **total** | {total_files} | {total_tests:,} |")
    whole_tests = sum(own.test_counts[t] for t in own.whole)
    out += [
        "",
        f"Single owner: {single_files} of {total_files} files "
        f"({pct(single_files, total_files)}), {single_tests:,} of {total_tests:,} tests "
        f"({pct(single_tests, total_tests)}).",
        f"Unresolved: {files[UNRESOLVED]} files, {tests[UNRESOLVED]:,} tests. "
        f"Reach every component (import `{COMPOSITION_MODULE}` or scan the tree): "
        f"{len(own.whole)} files, {whole_tests:,} tests.",
    ]
    largest = sorted(
        (t for t, o in own.owners.items() if o.startswith(INTEGRATION)),
        key=lambda t: (-own.test_counts[t], t),
    )[:top]
    if largest:
        out += ["", f"Largest `integration:` files (top {top}):", ""]
        out += ["| File | Tests | Components |", "|---|---|---|"]
        for test in largest:
            names = own.owners[test].removeprefix(INTEGRATION).split("+")
            label = "every component" if test in own.whole else ", ".join(names)
            out.append(f"| `{test}` | {own.test_counts[test]} | {label} |")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", action="store_true", help="print a markdown summary")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite the baseline from current violations (only after reclassifying)",
    )
    parser.add_argument(
        "--write-owners",
        action="store_true",
        help="rewrite tests/component-owners.txt from the current test graph",
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    result = check(config)

    if args.write_baseline:
        if result.unmapped or result.ambiguous or result.cycles or result.invalid:
            print("refusing to write a baseline while the map is incomplete or invalid")
        else:
            write_baseline(config.baseline, result.violations)
            print(f"wrote {len(result.violations)} entries to {config.baseline}")
            result = check(config)

    if args.write_owners:
        if result.ownership is None:
            print("no tests/ directory to own; nothing written")
        elif result.unmapped or result.ambiguous:
            print("refusing to write owners while a module is unmapped or ambiguous")
        else:
            path = repo_root(config) / OWNERS_FILE
            path.write_text(render_owners(result.ownership.owners))
            print(f"wrote {len(result.ownership.owners)} owners to {path}")
            result.unowned, result.stale_owners = [], []

    if args.report:
        print(report(config, result))
        print()

    for problem in result.invalid:
        print(f"INVALID   {problem}")
    for cycle in result.cycles:
        print(f"CYCLE     depends_on loops: {' -> '.join(cycle)}; keep one direction")
    for module in result.unmapped:
        print(f"UNMAPPED  {module}: add it to a component in {args.config.name}")
    for module, hits in sorted(result.ambiguous.items()):
        print(f"AMBIGUOUS {module}: matched by {', '.join(hits)}; list it by exact name")
    for comp, pattern in result.unused_patterns:
        print(f"UNUSED    {comp}: pattern {pattern!r} matches no module; remove it")
    for src, dst in result.new:
        print(
            f"NEW       {src} ({result.modules[src]}) -> {dst} ({result.modules[dst]}): "
            f"{result.modules[src]} does not depend on {result.modules[dst]}"
        )
    for src, dst in result.stale:
        print(
            f"FIXED     {src} -> {dst}: no longer an undeclared import; delete it from the baseline"
        )
    for test in result.unowned:
        print(f"UNOWNED   {test}: not in {OWNERS_FILE}; run --write-owners")
    for line in result.stale_owners:
        print(f"STALE_OWNERS {line}; run --write-owners")
    status = "ok" if result.ok else "FAILED"
    print(
        f"components: {status} ({len(result.modules)} modules, "
        f"{len(config.components)} components, "
        f"{sum(map(len, result.edges.values()))} imports, "
        f"{len(result.violations)} undeclared, {len(result.new)} of them not in the baseline)"
    )
    if result.ownership is not None:
        own = result.ownership
        print(
            f"test owners: {len(own.owners)} files, {len(result.unowned)} unowned, "
            f"{len(result.stale_owners)} stale (graph built in {own.seconds:.1f} s)"
        )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
