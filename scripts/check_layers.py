"""Check that src/unified_agent imports respect the layer map. Stdlib only.

The map lives in ``docs/architecture/layers.toml``: an ordered list of layers,
top first, each claiming modules by exact name or glob. A module may import
from its own layer or any layer below it. An import that points *up* is a
violation, unless it is listed in the baseline file, which records the
inversions that existed when the map was drawn, or declared as a seam.

A seam (``[[seam]]`` in the map) is a deliberate upward import with a stated
reason, such as the one lazy lookup of the composition root that fills a
registry. Seams are design, not debt, so they are kept apart from the baseline.

The baseline only ever shrinks. The check fails when:

* a module imports upward and the edge is neither baselined nor a seam;
* a baseline line no longer matches a real upward import (paid-off debt:
  delete the line so it cannot come back unnoticed);
* a module is in no layer, or in two (every new module must be placed);
* a pattern or a seam matches nothing.

Every ``import`` statement counts, including ones inside functions and under
``if TYPE_CHECKING:``. A lazy import is still a dependency; it only hides the
cycle from the interpreter. Because every module is mapped, checking direct
edges is enough: any upward chain contains at least one upward edge.

Usage:

  uv run python scripts/check_layers.py              # check, exit 1 on failure
  uv run python scripts/check_layers.py --report     # markdown summary
  uv run python scripts/check_layers.py --write-baseline   # after reclassifying
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import sys
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "docs" / "architecture" / "layers.toml"


@dataclass
class Layer:
    name: str
    description: str
    patterns: list[str]


@dataclass
class Config:
    package: str
    source: Path
    baseline: Path
    layers: list[Layer]
    seams: dict[tuple[str, str], str] = field(default_factory=dict)  # edge -> why


@dataclass
class Result:
    modules: dict[str, str] = field(default_factory=dict)  # module -> layer
    edges: dict[str, set[str]] = field(default_factory=dict)
    violations: set[tuple[str, str]] = field(default_factory=set)
    seams: set[tuple[str, str]] = field(default_factory=set)
    new: list[tuple[str, str]] = field(default_factory=list)
    stale: list[tuple[str, str]] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    unused_patterns: list[tuple[str, str]] = field(default_factory=list)
    unused_seams: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.new
            or self.stale
            or self.unmapped
            or self.ambiguous
            or self.unused_patterns
            or self.unused_seams
        )


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text())
    root = path.parent
    layers = [
        Layer(item["name"], item.get("description", ""), list(item["modules"]))
        for item in data["layer"]
    ]
    seams = {(item["from"], item["to"]): item["why"] for item in data.get("seam", [])}
    return Config(
        package=data["package"],
        source=(root / data["source"]).resolve(),
        baseline=(root / data["baseline"]).resolve(),
        layers=layers,
        seams=seams,
    )


def discover(source: Path, package: str) -> dict[str, Path]:
    """Every module in the package, keyed by its name relative to the package."""
    pkg_dir = source / package
    modules: dict[str, Path] = {}
    for path in sorted(pkg_dir.rglob("*.py")):
        parts = list(path.relative_to(pkg_dir).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules[".".join(parts)] = path
    return modules


def _imported_names(tree: ast.AST, module: str, is_pkg: bool, package: str) -> set[str]:
    """Dotted names (relative to ``package``) that each import statement names.

    ``from .web import x`` yields ``web.x``: the caller resolves it to the
    module ``web.x`` when one exists, and otherwise to the package ``web``
    (``x`` is an attribute). Each name is resolved on its own, so a child
    module import never hides an attribute import from its parent package.
    """
    names: set[str] = set()
    prefix = package + "."
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(prefix):
                    names.add(alias.name[len(prefix) :])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = [package, *module.split(".")] if module else [package]
                if not is_pkg:
                    base = base[:-1]
                base = base[: len(base) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target != package and not target.startswith(prefix):
                continue
            rel = target[len(prefix) :] if target != package else ""
            for alias in node.names:
                names.add(f"{rel}.{alias.name}".lstrip("."))
    return names


def build_edges(modules: dict[str, Path], package: str) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for module, path in modules.items():
        tree = ast.parse(path.read_text(), filename=str(path))
        targets: set[str] = set()
        for name in _imported_names(tree, module, path.name == "__init__.py", package):
            # ``from .tools import approvals`` names a module; ``from .config
            # import X`` names an attribute. Walk up to the nearest module.
            while name and name not in modules:
                name = name.rpartition(".")[0]
            if name and name != module:
                targets.add(name)
        edges[module] = targets
    return edges


def classify(
    modules: dict[str, Path], layers: list[Layer]
) -> tuple[dict[str, str], list[str], dict[str, list[str]], list[tuple[str, str]]]:
    """Assign each module a layer. Exact names win over globs."""
    exact: dict[str, list[str]] = defaultdict(list)
    globs: list[tuple[str, str]] = []
    for layer in layers:
        for pattern in layer.patterns:
            if any(ch in pattern for ch in "*?["):
                globs.append((layer.name, pattern))
            else:
                exact[pattern].append(layer.name)
    assigned: dict[str, str] = {}
    unmapped: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    used: set[tuple[str, str]] = set()
    for module in modules:
        matched = [(name, pat) for name, pat in globs if fnmatch.fnmatchcase(module, pat)]
        used.update(matched)  # a glob an exact name overrides is still in use
        if module in exact:
            hits = exact[module]
            used.update((name, module) for name in hits)
        else:
            hits = sorted({name for name, _ in matched})
        if not hits:
            unmapped.append(module)
        elif len(hits) > 1:
            ambiguous[module] = hits
        else:
            assigned[module] = hits[0]
    unused = [
        (layer.name, pattern)
        for layer in layers
        for pattern in layer.patterns
        if (layer.name, pattern) not in used
    ]
    return assigned, unmapped, ambiguous, unused


def read_baseline(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    entries = set()
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        src, sep, dst = line.partition("->")
        if not sep:
            raise ValueError(f"{path}: malformed baseline line {raw!r} (want 'a -> b')")
        entries.add((src.strip(), dst.strip()))
    return entries


def write_baseline(path: Path, violations: set[tuple[str, str]], modules: dict[str, str]) -> None:
    by_pair: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for src, dst in violations:
        by_pair[(modules[src], modules[dst])].append((src, dst))
    lines = [
        "# Upward imports that existed when the layer map was drawn.",
        "# Generated by `scripts/check_layers.py --write-baseline`. This list only",
        "# shrinks: fix an inversion, then delete its line (the check fails until",
        "# you do). Never add a line by hand to make a new inversion pass.",
    ]
    for (src_layer, dst_layer), pairs in sorted(by_pair.items()):
        lines += ["", f"# {src_layer} -> {dst_layer} ({len(pairs)})"]
        lines += [f"{src} -> {dst}" for src, dst in sorted(pairs)]
    path.write_text("\n".join(lines) + "\n")


def check(config: Config) -> Result:
    found = discover(config.source, config.package)
    assigned, unmapped, ambiguous, unused = classify(found, config.layers)
    edges = build_edges(found, config.package)
    rank = {layer.name: i for i, layer in enumerate(config.layers)}  # 0 = top
    upward = {
        (src, dst)
        for src, targets in edges.items()
        for dst in targets
        if src in assigned and dst in assigned and rank[assigned[src]] > rank[assigned[dst]]
    }
    seams = upward & set(config.seams)
    violations = upward - seams
    baseline = read_baseline(config.baseline)
    return Result(
        modules=assigned,
        edges=edges,
        violations=violations,
        seams=seams,
        unused_seams=sorted(set(config.seams) - seams),
        new=sorted(violations - baseline),
        stale=sorted(baseline - violations),
        unmapped=sorted(unmapped),
        ambiguous=ambiguous,
        unused_patterns=unused,
    )


def report(config: Config, result: Result) -> str:
    names = [layer.name for layer in config.layers]
    sizes = Counter(result.modules.values())
    out = ["| Layer | Modules | Imports up (out) | Imported from below (in) |", "|---|---|---|---|"]
    out_up = Counter(result.modules[s] for s, _ in result.violations)
    in_up = Counter(result.modules[d] for _, d in result.violations)
    for name in names:
        out.append(f"| {name} | {sizes[name]} | {out_up[name]} | {in_up[name]} |")
    out += ["", f"Upward imports (debt): {len(result.violations)}"]
    out += [f"Declared seams: {len(result.seams)}", ""]
    out += ["| from \\ to | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]
    pairs = Counter((result.modules[s], result.modules[d]) for s, d in result.violations)
    for src in names:
        cells = [str(pairs[(src, dst)] or "") for dst in names]
        out.append(f"| {src} | " + " | ".join(cells) + " |")
    out += ["", "Most-imported-from-below targets:", ""]
    for module, count in Counter(d for _, d in result.violations).most_common(15):
        out.append(f"- `{module}` ({result.modules[module]}): {count}")
    out += ["", "Modules with the most upward imports:", ""]
    for module, count in Counter(s for s, _ in result.violations).most_common(15):
        out.append(f"- `{module}` ({result.modules[module]}): {count}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", action="store_true", help="print a markdown summary")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite the baseline from current violations (only after reclassifying)",
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    result = check(config)

    if args.write_baseline:
        if result.unmapped or result.ambiguous:
            print("refusing to write a baseline while modules are unmapped or ambiguous")
        else:
            write_baseline(config.baseline, result.violations, result.modules)
            print(f"wrote {len(result.violations)} entries to {config.baseline}")
            result = check(config)

    if args.report:
        print(report(config, result))
        print()

    for module in result.unmapped:
        print(f"UNMAPPED  {module}: add it to a layer in {args.config.name}")
    for module, hits in sorted(result.ambiguous.items()):
        print(f"AMBIGUOUS {module}: matched by {', '.join(hits)}; list it by exact name")
    for layer, pattern in result.unused_patterns:
        print(f"UNUSED    {layer}: pattern {pattern!r} matches no module; remove it")
    for src, dst in result.new:
        print(
            f"NEW       {src} ({result.modules[src]}) -> {dst} ({result.modules[dst]}): "
            "a lower layer may not import a higher one"
        )
    for src, dst in result.unused_seams:
        print(f"UNUSED    seam {src} -> {dst} is not an upward import; remove it")
    for src, dst in result.stale:
        print(f"FIXED     {src} -> {dst}: no longer imported upward; delete it from the baseline")
    status = "ok" if result.ok else "FAILED"
    print(
        f"layers: {status} ({len(result.modules)} modules, "
        f"{sum(map(len, result.edges.values()))} imports, "
        f"{len(result.violations)} upward, {len(result.new)} of them not in the baseline, "
        f"{len(result.seams)} declared seams)"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
