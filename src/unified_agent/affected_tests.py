"""Select the test files a change can affect, from a static import graph (task #2231).

Stdlib only, and it never imports the code it reads: every file is parsed with
``ast`` and walked once. Every selection is a targeted subset, never a
certification (:data:`SUBSET_NOTE`). ``scripts/affected_tests.py`` is the
command-line front end; the review pipeline (``reviewer._targeted_test_plan``)
calls :func:`select` directly.

The analysis resolves only the forms it fully understands, listed below;
every other dynamic import, code string or name binding it meets marks the file
*unresolved* instead of being skipped. On the test side that is conservative:
an unresolved test, helper or script runs on every Python change
(:attr:`Selection.unresolved_tests`). On the src side it is not: an unresolved
src module is reported in :attr:`Selection.caveats` and what it loads is not
followed, because seeding those modules on every change would select 150 to 350
of ~650 test files each time. That is a deliberate trade-off for a precheck
that batch CI, which runs the full suite, always backs.

Threat model: this is a local and review-time precheck for honest test code,
always backed by mandatory batch CI. Constructs it cannot read statically fail
toward selection (the file is unresolved and runs). Deliberately adversarial
indirection is out of scope: fetching a loader by a computed ``getattr`` from a
module that is not a loader module, mutating another module's globals to feed
it code, and the like are not detected.

The graph's nodes are the Python files under ``src/unified_agent``, ``tests`` and
``scripts``. A file depends on another when it:

* imports it (``import unified_agent.x``, ``from unified_agent.x import y``,
  ``from . import x``, ``from .harness import y``, a test helper imported by its
  bare name such as ``from growth_budget import ...``, or a script a test puts
  on ``sys.path`` and imports by name);
* names it in a string, as decoded by the parser (so implicit concatenation
  counts): ``monkeypatch.setattr("unified_agent.x.y", ...)``, an entry point
  ``"unified_agent.x:func"``. A unified_agent name completed at run time
  (an f-string, ``+`` or ``.format`` template) is unresolved;
* declares it in the component map: ``docs/architecture/components.toml`` names
  each component's modules, and the ``components`` package is treated as
  depending on all of them (task #2562). That declared edge is what keeps a
  change to any component module reaching the tests that import ``server`` once
  manifest loading becomes conditional, which is why it is read here rather than
  inferred from the imports a conditional file happens to make. It is excluded
  from the unresolved-test closure (task #2652): an unresolved module's taint
  must not reach every test that imports the components hub;
* loads it through a loader identifier (:data:`_LOADERS`: ``import_module``,
  ``__import__``, ``find_spec``, ``run_module``, ``run_path``,
  ``spec_from_file_location``, ``exec``, ``eval``, ``compile``, ``exec_module``,
  ``load_module``, ``importorskip``), called as a bare name, an imported alias,
  or any attribute ending in one (``builtins.exec``, ``pytest.importorskip``),
  with a *static* argument (positional or keyword);
* runs it in a Python command line, parsed with CPython's option grammar
  (combined short flags such as ``-Bc``/``-Im``, ``-W``/``-X``/``-Q`` consuming
  their argument; ``--``, ``-`` and a script path end the options, and the
  script that follows is what runs). Every element up to and including what
  runs must be static; ``-h``/``-V``/``--help``/``--version`` run nothing;
  ``-m pytest`` makes every element count and each ``-p NAME`` a plugin
  import. ``python -``, or a python with no script given a stdin by its spawn
  call, reads its script from stdin: a static ``input=`` (a string, bytes or
  ``<static>.encode()``; sh's ``_in=``) gets the same analysis as a file, and
  any other stdin, or ``-`` with none, is unresolved. This applies to any list
  or tuple with an interpreter in it, to ``shlex.split(<static>)``, and to every
  process-spawn call: a call through any import of ``subprocess``, ``os``,
  ``asyncio``, ``pty``, ``sh`` or ``plumbum`` (attribute, imported name, or a
  static-name ``getattr``) to that module's spawn API (``subprocess.run``/
  ``Popen``/``call``/``check_*``/``getoutput``, ``os.system``/``popen``/
  ``exec*``/``spawn*``/``posix_spawn*``, ``asyncio.create_subprocess_*``,
  ``pty.spawn``), its command bound the way the real signature binds it
  (positional, ``args=``/``cmd=``/``command=``, ``shell=True`` strings; with
  ``shell=True`` only a list's first element is the command, and a computed
  ``shell=`` reads the command both ways). An sh or plumbum command is named by
  the code: ``sh.python3(...)``, ``sh.contrib.git``, ``plumbum.cmd.git``,
  ``from sh import git``, ``from plumbum.cmd import git``, whose program is
  that name and whose positional arguments follow it; ``sh.Command(x)``, a
  command reached past another attribute (``sh.python3.bake``) and an sh
  keyword option have arguments that are not read; ``local["git"]`` is a
  static program; any other plumbum use is unresolved. A fully static command
  is parsed; one whose program is a static literal that is not an
  interpreter is ignored (and a fixed sh or plumbum command that is not Python
  is fine however it is used); any other command could start Python and is
  unresolved. A spawn function bound exactly once in the file to a name
  (``RUN = subprocess.run``) makes calls through that name spawn calls. A
  process module is fine as the object of ``monkeypatch.setattr``/``delattr``,
  ``mock.patch.object``, ``hasattr`` or a static-name ``getattr`` of something
  that is not a spawn function, and as an attribute read straight off
  ``__import__("os")``; any other value use of one (``sp = subprocess``, a
  computed ``getattr`` on it or on a chain from it, its ``__dict__``, a process
  module loaded by name and kept), and a spawn function passed as an argument
  or bound more than once, is unresolved. ``multiprocessing`` and
  ``concurrent.futures`` run a callable, not a command: the child imports what
  the pickled callable names, which the parent references like any value, so
  a spawn function or loader handed to a pool is unresolved as above. What
  changes the child is read: ``set_executable`` must be static, and
  ``set_forkserver_preload`` must be a static list, one import edge per name.
  A missed alias only means a test is not selected locally; batch CI still
  runs it;
* names it in ``pytest_plugins`` (a static string, list or tuple), or as a
  ``-p NAME`` plugin in a static pytest ``addopts``, which loads it into every
  test (reaching one asks for the full suite).

Name resolution is coarse and file-global by construction; there is no scope
selection. A name resolves only when it has exactly one binding in the whole
file, across every scope (parameters, imports, ``def``/``class``, loop,
``with``, ``except``, comprehension and ``match`` targets, ``global``/
``nonlocal`` all count). For a loader callee that binding must be a
module-level import; for a name inside a loader argument it may be a plain
``NAME = <static>`` in any scope. Since the count is file-wide, a module
``CODE`` and a function's ``CODE`` are two bindings and resolve to nothing.
Repeated module-level imports of the same module count once.
A static argument is a literal, a ``+`` of static parts, ``dedent`` of one (only
``textwrap.dedent``, via a single module-level import), or such a name; a path
argument may also be literal parts joined onto ``__file__``. Loader-module
tokens (``builtins``, ``__builtins__``, ``importlib`` and its submodules,
``runpy``, ``pkgutil``, ``zipimport``, ``imp``, and any name an import binds to
one) mark the file unresolved
wherever they appear, except as the base of a direct static loader call (or
``module_from_spec``) on a single module-level import, so ``runtime = builtins``
is itself unresolved. ``getattr()`` with a computed attribute name is
unresolved when its object is a loader-module token or a name bound to one; a
loader stored or passed around is unresolved.
``exec_module``/``load_module`` resolve only when their spec traces, through
single module-level bindings, to a static ``spec_from_file_location`` or
``find_spec``. Code run this way gets the same analysis as a file. Three forms
are known safe: ``re.compile`` on a single module-level ``import re`` (or
``regex``), docstrings and bare string statements, and literal code that does
not parse (Python would not run it). A file also depends on another when it:

* names its path in a string constant (``REPO / "scripts" / "check_layers.py"``
  or ``"deploy/steward-audit-status.sh"``), which also ties tests and modules
  to data files such as ``org.yaml``;
* sits in a package whose ``__init__.py`` runs first on import or, for tests,
  under a directory-level ``conftest.py``.

How far a change travels (``depth``):

``transitive`` (default)
    Every test that depends on the changed file through any chain of
    module-level imports. A function-level import is followed only
    out of the changed file itself, or into a test or script, which is where
    the chain ends. Measured on this repo (task #2231), following lazy imports
    everywhere joins 337 of 382 src modules into one cycle, so nearly every
    change selected 619 of 652 test files; lazy imports are how those cycles
    are broken in the code, and the walk treats them the same way. A chain
    that needs a lazy hop past the first src module (``core <- mid`` eager,
    ``mid <- top`` lazy) is missed; batch CI, which always runs the full suite,
    is the backstop, and ``full`` covers it on demand.
``full``
    Every lazy edge too. Close to the whole suite for most src changes, but
    complete as far as static imports go.
``direct``
    Tests that import the changed file, directly or through test helpers,
    scripts and directory conftests, but not through other src modules. The
    review pipeline uses this tier under its file cap.

One approximation is deliberate. The root ``tests/conftest.py`` is loaded by
every test, so following its imports would make a change to any module it
patches for hermeticity (storage, config, doctor, ...) select the whole suite.
A change to that file itself does select everything; a change to a module it
imports selects that module's real dependents, and the always-run inventory
tests load the conftest too, so a fixture broken by such a change still fails
in the targeted run.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import shlex
import textwrap
import tomllib
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = "unified_agent"
SRC_DIR = "src/unified_agent"
#: The component map (task #2562). K4b makes manifest loading conditional, which
#: removes the real import edges from ``components`` to every component module —
#: the edges that carry ``server.py``'s reach to each tool. The map declares
#: them, so the selector reads them from here instead.
COMPONENTS_TOML = "docs/architecture/components.toml"
DEPTHS = ("transitive", "direct", "full")
# Every selection is a subset chosen by a static reading of the code. Callers
# say so wherever they report one, so it is never mistaken for a full run.
SUBSET_NOTE = (
    "a targeted subset from a static import graph, not a certification; "
    "batch CI (the full suite) is still required"
)

# Cross-cutting inventory tests: each freezes a property of the whole tree, so a
# change anywhere can break it even when nothing it imports changed (a new
# module, route or tool). Hand-picked targeted sets kept missing them and merge
# batches failed on exactly these, so every selection runs them.
ALWAYS_RUN: tuple[str, ...] = (
    "tests/test_webui_route_inventory.py",  # web_routes() freeze list
    "tests/test_route_scopes.py",  # every served route has a declared scope
    "tests/test_layer_check.py",  # every src module placed in docs/architecture/layers.toml
    "tests/test_profiles.py",  # default profile stays under the 100-tool cap
    "tests/test_docs_tool_count.py",  # docs/README.md tool count == registered tools
    "tests/test_component_check.py",  # src imports stay inside the component map
    "tests/test_test_budget.py",  # no issue-numbered or oversized test file
)

# Changes that reach every test without an import edge: packaging and the
# interpreter, pytest's own configuration, and the package root.
FULL_SUITE_FILES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "conftest.py",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "tests/conftest.py",
        f"{SRC_DIR}/__init__.py",
    }
)

# Trees no Python test reads: the JS packages and the native desktop shell have
# their own CI jobs (scripts/ci_changes.py). A change there selects nothing here.
NON_PYTHON_TREES = ("packages/", "native/")

TEST_FILE_RE = re.compile(r"^tests/(?:.+/)?(?:test_[^/]+|[^/]+_test)\.py$")
# A test that walks the source tree reads every module as text, not by import.
_TREE_SCAN_RE = re.compile(r"rglob\(\s*[\"'](?:\*\*/)?\*\.py[\"']\s*\)|os\.walk\(")
# Bare basenames too common to tie a constant to one file; a constant naming one
# needs a directory part ("docs/README.md") to count as a reference.
_AMBIGUOUS_BASENAMES = frozenset(
    {
        "__init__.py",
        "conftest.py",
        "__main__.py",
        "README.md",
        "CHANGELOG.md",
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        "SKILL.md",
        "package.json",
        "tsconfig.json",
        "index.ts",
        "index.js",
    }
)
_MAX_CONSTANT_LEN = 200
_STATUS_RE = re.compile(r"^[ACDMRTUXB]\d*$")


def is_test_file(path: str) -> bool:
    return bool(TEST_FILE_RE.match(path))


def is_markdown_doc(path: str) -> bool:
    """Same rule as ``scripts/ci_changes.py``: markdown outside code-adjacent trees."""
    return path.endswith(".md") and not path.startswith(("src/", "tests/", "packages/"))


# -- changes ------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One changed path. ``old_path`` is set for a rename or copy."""

    status: str
    path: str
    old_path: str | None = None

    @property
    def deleted(self) -> bool:
        return self.status.upper().startswith("D")

    @property
    def renamed(self) -> bool:
        return self.status.upper().startswith("R")


def parse_name_status(lines: Iterable[str]) -> list[Change]:
    """``git diff --name-status`` lines, or bare paths (read as modified)."""
    changes: list[Change] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and _STATUS_RE.match(parts[0].strip()):
            status = parts[0].strip()
            if len(parts) >= 3:
                changes.append(Change(status, parts[2].strip(), parts[1].strip()))
            else:
                changes.append(Change(status, parts[1].strip()))
        else:
            changes.append(Change("M", line.strip()))
    return changes


def changes_from_entries(entries: Iterable[dict]) -> list[Change]:
    """The reviewer's ``{"file": "old<TAB>new" | path, "status": ...}`` entries."""
    changes: list[Change] = []
    for entry in entries:
        status = str(entry.get("status") or "M").strip() or "M"
        parts = [p.strip() for p in str(entry.get("file") or "").split("\t") if p.strip()]
        if not parts:
            continue
        old = parts[0] if len(parts) > 1 else None
        changes.append(Change(status, parts[-1], old))
    return changes


# -- parsing ------------------------------------------------------------------


@dataclass
class _Node:
    path: str
    module: str  # dotted name it is imported as ("" when not importable)
    is_pkg: bool
    imports: set[str] = field(default_factory=set)  # every dotted name it imports or names
    eager: set[str] = field(default_factory=set)  # the subset imported at module level
    constants: set[str] = field(default_factory=set)
    tree_scan: bool = False
    # Imports whose target cannot be read statically (a computed name, or
    # ``-c`` code built at run time), as "line N: why".
    unresolved: list[str] = field(default_factory=list)


def declared_component_modules(root: Path | str) -> set[str] | None:
    """Repo-relative files of every module the component map declares.

    ``docs/architecture/components.toml`` names each component's modules as exact
    dotted names or globs. Loading is conditional since K4b (task #2603): the
    real import chain (``server`` imports ``components``, which imports each
    selected manifest) no longer carries an edge to a manifest that a process
    did not select, so the map has to supply it. Read it here, and a computed
    or conditional import can no longer shrink selection silently.

    ``None`` means the map is unreadable or malformed. Callers must fail
    closed on it by declaring every module, never by declaring none: a broken
    map must widen a selection, not quietly narrow it. A map on a checkout with
    no ``src`` directory still declares nothing.
    """
    base = Path(root) / SRC_DIR
    try:
        data = tomllib.loads((Path(root) / COMPONENTS_TOML).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not base.is_dir():
        return set()
    declared: set[str] = set()
    for component in data.get("component", []):
        for entry in component.get("modules", []):
            if any(char in entry for char in "*?["):
                for path in base.rglob("*.py"):
                    dotted = path.relative_to(base).with_suffix("").as_posix().replace("/", ".")
                    if dotted.endswith(".__init__"):
                        dotted = dotted[: -len(".__init__")]
                    if fnmatch.fnmatchcase(dotted, entry):
                        declared.add(path.relative_to(Path(root)).as_posix())
                continue
            stem = base.joinpath(*entry.split("."))
            for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
                if candidate.is_file():
                    declared.add(candidate.relative_to(Path(root)).as_posix())
                    break
    return declared


def _module_name(base: Path, rel_in: Path, *, by_path: bool) -> tuple[str, bool]:
    """Dotted name a file is imported as, relative to ``base``.

    ``by_path`` names it by its full path (the installed package). Otherwise
    pytest's rootdir insertion applies: the name starts at the highest
    directory that still has an ``__init__.py`` above the file.
    """
    is_pkg = rel_in.name == "__init__.py"
    dir_parts = list(rel_in.parent.parts)
    keep = 0
    if not by_path:
        keep = len(dir_parts)
        while keep > 0 and (base.joinpath(*dir_parts[:keep]) / "__init__.py").is_file():
            keep -= 1
    name_parts = dir_parts[keep:] if is_pkg else [*dir_parts[keep:], rel_in.stem]
    return ".".join(name_parts), is_pkg


def _resolve_relative(module: str, is_pkg: bool, level: int, target: str | None) -> str:
    base = module.split(".") if module else []
    if not is_pkg:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)] if level - 1 <= len(base) else []
    return ".".join([*base, *([target] if target else [])])


def _from_names(node: _Node, item: ast.ImportFrom) -> set[str]:
    if item.level:
        target = _resolve_relative(node.module, node.is_pkg, item.level, item.module)
    else:
        target = item.module or ""
    if not target:
        return set()
    return {target} | {f"{target}.{a.name}" for a in item.names if a.name != "*"}


def _stmt_names(node: _Node, item: ast.stmt) -> set[str]:
    if isinstance(item, ast.Import):
        return {alias.name for alias in item.names}
    if isinstance(item, ast.ImportFrom):
        return _from_names(node, item)
    return set()


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_statements(body: list[ast.stmt], lazy: bool, out: list[tuple[ast.stmt, bool]]) -> None:
    """Every import statement in ``body`` with whether it runs only on call.

    Imports inside a function body are lazy; module and class level imports,
    including ones under ``try``/``if``/``with``, run at import time. Imports
    under ``if TYPE_CHECKING:`` never run and count as lazy.
    """
    for item in body:
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            out.append((item, lazy))
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _import_statements(item.body, True, out)
        elif isinstance(item, ast.If) and _is_type_checking(item.test):
            _import_statements(item.body, True, out)
            _import_statements(item.orelse, lazy, out)
        else:
            for name in ("body", "orelse", "finalbody"):
                sub = getattr(item, name, None)
                if isinstance(sub, list):
                    _import_statements(sub, lazy, out)
            for handler in getattr(item, "handlers", ()):
                _import_statements(handler.body, lazy, out)
            for case in getattr(item, "cases", ()):
                _import_statements(case.body, lazy, out)


def _const_str(expr: ast.AST | None) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


def _splatted_default(call: ast.Call) -> bool:
    """``getattr(X, "name", *args)`` or ``getattr(X, "name", **kw)``."""
    return any(isinstance(a, ast.Starred) for a in call.args[2:]) or any(
        kw.arg is None for kw in call.keywords
    )


# -- static analysis --------------------------------------------------------
#
# A dynamic import, a code string or a name is resolved only in the few forms
# the analysis fully understands; every other form marks the file *unresolved*.
# For tests, helpers and scripts that is conservative: an unresolved one runs
# on every Python change. An unresolved src module is reported as a caveat and
# what it loads is not followed (see the module docstring for why).

# Loader identifiers: a call whose callee is one of these names, an alias of
# one, or any attribute ending in one (``builtins.exec``, ``x.exec``,
# ``importlib.import_module``) is a loader call. Each entry says where the
# argument that decides what is loaded sits: (position, keyword, kind).
_LOADERS: dict[str, tuple[int, str, str]] = {
    "import_module": (0, "name", "module"),
    "__import__": (0, "name", "module"),
    "run_module": (0, "mod_name", "module"),
    "find_spec": (0, "name", "module"),
    "spec_from_file_location": (1, "location", "path"),
    "run_path": (0, "path_name", "path"),
    "exec": (0, "source", "code"),
    "eval": (0, "source", "code"),
    "compile": (0, "source", "code"),
    "importorskip": (0, "modname", "module"),
    "exec_module": (0, "module", "spec"),
    "load_module": (0, "fullname", "spec"),
}
# The one attribute form known not to load code: compile() on a name every
# binding of which in the file is ``import re`` (or ``regex``).
_NOT_LOADERS = frozenset({("re", "compile"), ("regex", "compile")})
# Loader-module tokens. A name that is one of these, or that an import binds to
# a loader module, makes the file unresolved wherever it appears, whatever it
# is bound to, except as the base of a direct loader call with static
# arguments (or ``module_from_spec``) on a single module-level import.
_RADIOACTIVE = frozenset(
    {"builtins", "__builtins__", "importlib", "runpy", "pkgutil", "zipimport", "imp"}
)
# CPython options that take an argument (``-W ignore``, ``-Xdev``), and the ones
# that select what runs (``-c CODE``, ``-m MODULE``); see ``python --help``.
_PY_ARG_OPTIONS = frozenset("WXQ")
_PY_SELECTORS = frozenset("cm")
_PY_LONG_WITH_ARG = frozenset({"--check-hash-based-pycs"})
_PYTEST_MODULES = frozenset({"pytest", "py.test"})
# Process-spawn calls, by module, with the real API names. A call through any
# import binding of one of these modules (``subprocess.run``, ``from os import
# execv``) is a process spawn; its command is read the way the signature binds
# it. ``asyncio.run`` runs a coroutine, not a process, so it is not here.
_PROCESS_MODULES = frozenset({"subprocess", "os", "asyncio", "pty", "sh", "plumbum"})
# Modules whose commands are named by an attribute or an imported name rather
# than an argv: ``sh.python3("-m", "x")``, ``from plumbum.cmd import git``.
_COMMAND_MODULES = frozenset({"sh", "plumbum"})
# A process module's namespace read as a whole hands back its spawn functions.
_MODULE_NAMESPACE = frozenset({"__dict__", "__getattribute__", "__getattr__"})
# Loader identifiers that return the module they load.
_MODULE_RETURNING = frozenset({"import_module", "__import__", "importorskip"})
# multiprocessing (and concurrent.futures' ProcessPoolExecutor, built on it)
# runs a callable, not a command: the child imports what the pickled callable
# names, which the parent references like any other value. These calls change
# what the child runs instead: (position, keyword) of the argument.
_MP_KNOBS = {"set_executable": (0, "executable"), "set_forkserver_preload": (0, "module_names")}
# CPython options that print and exit without running a script.
_PY_INFO_SHORT = frozenset("hV?")
_PY_INFO_LONG = ("--help", "--version")
_SUBPROCESS_SPAWNS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
)
_SHELL_STRING_SPAWNS = frozenset(
    {"getoutput", "getstatusoutput", "system", "popen", "create_subprocess_shell"}
)


def _is_spawn(module: str, name: str) -> bool:
    if module == "subprocess":
        return name in _SUBPROCESS_SPAWNS
    if module == "os":
        return name in ("system", "popen") or name.startswith(("exec", "spawn", "posix_spawn"))
    if module == "asyncio":
        return name in ("create_subprocess_exec", "create_subprocess_shell")
    if module == "pty":
        return name == "spawn"
    if module in _COMMAND_MODULES:
        # Every attribute of sh is a command (``Command`` builds one); classes
        # and exceptions (``sh.ErrorReturnCode``, ``plumbum.FG``) are not.
        return not name[:1].isupper() or (module == "sh" and name == "Command")
    return False


_INTERP = object()  # an argv element that is the running interpreter (sys.executable)
_STAR = object()  # an argv element hidden behind *splat
_PIPED = object()  # a process's stdin is given, but is not a static script
_SPEC_PRODUCERS = frozenset({"spec_from_file_location", "find_spec"})
_PATH_CALLS = frozenset({"Path", "PurePath", "PosixPath", "join", "joinpath"})
_DOTTED_RE = re.compile(rf"(?<![\w.]){PACKAGE}(?:\.[A-Za-z_]\w*)+")
# A unified_agent name completed at run time: "unified_agent.x.{}", "...%s",
# or (with \0 for a formatted part) f"unified_agent.{name}".
_TEMPLATE_RE = re.compile(rf"(?<![\w.]){PACKAGE}(?:\.\w+)*\.?(?:[{{%\0])")
_OPEN_PREFIX_RE = re.compile(rf"(?<![\w.]){PACKAGE}(?:\.\w+)*\.?$")
_TOKEN_SPLIT_RE = re.compile(r"[\s\"'`,;:()\[\]{}<>=|*?!$&\\]+")
_MAX_STRING_SCAN = 20_000


def _path_parts(expr: ast.AST) -> list[str]:
    """The trailing string parts of a path expression: ``ROOT / "a" / "b"``,
    ``Path("a", "b")``, ``root.joinpath("a", "b")``, ``os.path.join(x, "a", "b")``."""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        right = _const_str(expr.right)
        return [*_path_parts(expr.left), right] if right is not None else []
    if isinstance(expr, ast.Call):
        func = expr.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _PATH_CALLS:
            head = _path_parts(func.value) if name == "joinpath" else []
            parts: list[str] = []
            for arg in expr.args:
                value = _const_str(arg)
                parts = [*parts, value] if value is not None else []
            return [*head, *parts] if len(parts) == len(expr.args) else parts
    value = _const_str(expr)
    return [value] if value is not None else []


def _join_parts(parts: list[str]) -> str:
    return "/".join(p.strip("/") for p in parts if p.strip("/")).removeprefix("./")


def _is_loader_module(module: str) -> bool:
    top = module.split(".")[0]
    return top in _RADIOACTIVE


def _is_interpreter(value: object) -> bool:
    return value is _INTERP or (
        isinstance(value, str) and value.rsplit("/", 1)[-1].startswith("python")
    )


def _selects_code_or_module(token: object) -> bool:
    """A CPython short-option token whose scan reaches ``-c`` or ``-m``."""
    if not isinstance(token, str) or not token.startswith("-") or token.startswith("--"):
        return False
    for letter in token[1:]:
        if letter in _PY_SELECTORS:
            return True
        if letter in _PY_ARG_OPTIONS:
            return False
    return False


class _Bindings:
    """Every binding of every name in one file, across all scopes (task #2231).

    Resolution is deliberately coarse and file-global: a name has a usable
    binding only when the *whole file* binds it exactly once, at module level,
    by an import or a plain ``NAME = <expr>``. Parameters, defaults' targets,
    loop/with/except targets, unpacking, walrus, augmented assignment,
    ``del``, ``def``/``class`` names, comprehension and ``match`` captures and
    ``global``/``nonlocal`` declarations all count as bindings, wherever they
    are, so any of them anywhere makes the name ambiguous. There is no scope
    selection to get wrong.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.of: dict[str, list[tuple[str, bool, object]]] = defaultdict(list)
        self.star = False
        handled: set[int] = set()
        stack: list[tuple[ast.AST, bool]] = [(tree, False)]
        while stack:
            item, nested = stack.pop()
            top = not nested
            if isinstance(item, ast.Assign) and len(item.targets) == 1:
                target = item.targets[0]
                if isinstance(target, ast.Name):
                    self.of[target.id].append(("assign", top, item.value))
                    handled.add(id(target))
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                kind = "assign" if item.value is not None else "other"
                self.of[item.target.id].append((kind, top, item.value))
                handled.add(id(item.target))
            elif isinstance(item, ast.Name) and not isinstance(item.ctx, ast.Load):
                if id(item) not in handled:
                    self.of[item.id].append(("other", top, None))
            elif isinstance(item, ast.arg):
                self.of[item.arg].append(("other", top, None))
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.of[item.name].append(("other", top, None))
            elif isinstance(item, ast.ExceptHandler) and item.name:
                self.of[item.name].append(("other", top, None))
            elif isinstance(item, (ast.MatchAs, ast.MatchStar)) and item.name:
                self.of[item.name].append(("other", top, None))
            elif isinstance(item, ast.MatchMapping) and item.rest:
                self.of[item.rest].append(("other", top, None))
            elif isinstance(item, (ast.Global, ast.Nonlocal)):
                for name in item.names:
                    self.of[name].append(("other", top, None))
            elif isinstance(item, ast.Import):
                for alias in item.names:
                    local = alias.asname or alias.name.split(".")[0]
                    module = alias.name if alias.asname else local
                    self.of[local].append(("import", top, module))
            elif isinstance(item, ast.ImportFrom):
                module = "." * item.level + (item.module or "")
                for alias in item.names:
                    if alias.name == "*":
                        self.star = True
                    else:
                        local = alias.asname or alias.name
                        self.of[local].append(("from", top, (module, alias.name)))
            inner = nested or isinstance(
                item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            )
            stack.extend((child, inner) for child in ast.iter_child_nodes(item))
        # Loader-module tokens: their own names, and every local name an import
        # binds to a loader module (``import importlib.util as iu``,
        # ``from importlib import util``).
        self.radioactive: set[str] = set(_RADIOACTIVE)
        # Local names an import binds to a loader identifier (``import_module as load``).
        self.loader_aliases: dict[str, set[str]] = defaultdict(set)
        # Names an import binds to a process module, and imported spawn functions.
        self.process_modules: dict[str, set[str]] = defaultdict(set)
        self.spawn_functions: dict[str, tuple[str, str]] = {}
        # Names imported as one fixed sh or plumbum command: name -> program.
        self.commands: dict[str, str] = {}
        for name, found in self.of.items():
            for kind, _, payload in found:
                if kind == "import" and str(payload).split(".")[0] in _PROCESS_MODULES:
                    self.process_modules[name].add(str(payload).split(".")[0])
                elif kind == "from":
                    full, original = str(payload[0]), str(payload[1])  # type: ignore[index]
                    module = full.split(".")[0]
                    if module in _PROCESS_MODULES and (
                        _is_spawn(module, original) or full == "plumbum.cmd"
                    ):
                        self.spawn_functions[name] = (module, original)
                        if (full == "sh" and original != "Command") or full == "plumbum.cmd":
                            self.commands[name] = original
        for name, found in self.of.items():
            for kind, _, payload in found:
                if kind == "import" and _is_loader_module(str(payload)):
                    self.radioactive.add(name)
                elif kind == "from":
                    module, original = payload  # type: ignore[misc]
                    if original in _LOADERS:
                        self.loader_aliases[name].add(original)
                    elif _is_loader_module(module):
                        self.radioactive.add(name)

    def holds_loader_module(self, expr: ast.AST) -> bool:
        """``expr`` is a loader-module token (``builtins``, ``importlib.util``)
        or a name any binding of which assigns one."""
        base = _chain_base(expr)
        if base is None:
            return False
        if base.id in self.radioactive:
            return True
        return any(
            kind == "assign"
            and payload is not None
            and (chain := _chain_base(payload)) is not None  # type: ignore[arg-type]
            and chain.id in self.radioactive
            for kind, _, payload in self.of.get(base.id, [])
        )

    def unbound(self, name: str) -> bool:
        return not self.star and name not in self.of

    def single(self, name: str) -> tuple[str, object] | None:
        """(kind, payload) of the one binding ``name`` has in the whole file,
        when that binding is at module level and is an import or a plain
        assignment; else None."""
        if self.star:
            return None
        found = self.of.get(name, [])
        # ``import importlib`` and ``import importlib.util`` both bind the same
        # module to the same name at module level: one binding, not two.
        if found and all(kind == "import" and top for kind, top, _ in found):
            if len({payload for _, _, payload in found}) == 1:
                found = found[:1]
        if len(found) != 1:
            return None
        kind, top, payload = found[0]
        return (kind, payload) if top and kind != "other" else None

    def value_of(self, name: str) -> ast.expr | None:
        """The expression of the one binding ``name`` has in the whole file, in
        any scope, when that binding is a plain ``NAME = <expr>``; else None.
        For loader *arguments*: still file-global, so a name bound in two
        places (module ``CODE`` and a function's ``CODE``) has no value."""
        if self.star:
            return None
        found = self.of.get(name, [])
        if len(found) != 1 or found[0][0] != "assign":
            return None
        return found[0][2]  # type: ignore[return-value]

    def module_of(self, name: str) -> str | None:
        """The module ``name`` is, when its single module-level binding imports one."""
        one = self.single(name)
        return str(one[1]) if one and one[0] == "import" else None


def _chain_base(expr: ast.AST) -> ast.Name | None:
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return expr if isinstance(expr, ast.Name) else None


def _is_docstring_like(item: ast.AST) -> bool:
    return isinstance(item, ast.Expr) and _const_str(item.value) is not None


class _Analyzer(ast.NodeVisitor):
    """Everything a file (or a code string it runs) depends on, as far as it can be read."""

    def __init__(self, node: _Node, tree: ast.AST, *, lazy: bool, where: str = "") -> None:
        self.node = node
        self.where = where  # "" for the file, "python -c code: " for nested code
        self.lazy = 1 if lazy else 0
        # Full pre-pass before any use is analyzed: every binding in the file.
        self.b = _Bindings(tree)
        self.tree = tree
        self.annotation = 0  # inside a type annotation: names there spawn nothing
        self.argv_done: set[int] = set()  # argv literals a spawn call already read
        self.module_attr_ok: set[int] = set()  # __import__("os").environ: not a spawn

    # -- recording --------------------------------------------------------------

    def _imports(self, names: set[str]) -> None:
        self.node.imports |= names
        if not self.lazy:
            self.node.eager |= names

    def _unresolved(self, item: ast.AST, why: str) -> None:
        self.node.unresolved.append(f"line {getattr(item, 'lineno', '?')}: {self.where}{why}")

    def _string(self, value: str, item: ast.AST) -> None:
        """A decoded string: dotted unified_agent names, and path-like tokens."""
        if len(value) > _MAX_STRING_SCAN:
            value = value[:_MAX_STRING_SCAN]
        if PACKAGE in value:
            dotted = {m.group(0) for m in _DOTTED_RE.finditer(value)}
            if dotted:
                self.node.imports |= dotted
                self.node.eager |= dotted
            if _TEMPLATE_RE.search(value):
                self._unresolved(item, "a unified_agent name completed at run time")
        for token in _TOKEN_SPLIT_RE.split(value):
            if ("/" in token or "." in token) and len(token) <= _MAX_CONSTANT_LEN:
                token = token.removeprefix("./").strip("/")
                if token:
                    self.node.constants.add(token)

    # -- static values (file-global, never scope-selected) ------------------------

    def _literal(self, expr: ast.AST | None, depth: int = 0) -> str | None:
        """A string known without running anything: a literal, a ``+`` of static
        parts, ``textwrap.dedent`` of one, or a name whose single module-level
        binding in the file is ``NAME = <static>``."""
        if expr is None or depth > 20:
            return None
        value = _const_str(expr)
        if value is not None:
            return value
        if isinstance(expr, ast.Name):
            bound = self.b.value_of(expr.id)
            return self._literal(bound, depth + 1) if bound is not None else None
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left = self._literal(expr.left, depth + 1)
            right = self._literal(expr.right, depth + 1)
            return left + right if left is not None and right is not None else None
        if (
            isinstance(expr, ast.Call)
            and len(expr.args) == 1
            and not expr.keywords
            and self._is_dedent(expr.func)
        ):
            inner = self._literal(expr.args[0], depth + 1)
            return textwrap.dedent(inner) if inner is not None else None
        return None

    def _is_dedent(self, func: ast.AST) -> bool:
        """``dedent`` from a single module-level ``from textwrap import dedent``,
        or ``textwrap.dedent`` on a single module-level ``import textwrap``."""
        if isinstance(func, ast.Name):
            return self.b.single(func.id) == ("from", ("textwrap", "dedent"))
        if isinstance(func, ast.Attribute) and func.attr == "dedent":
            return (
                isinstance(func.value, ast.Name) and self.b.module_of(func.value.id) == "textwrap"
            )
        return False

    def _path(self, expr: ast.AST | None, depth: int = 0) -> list[str] | None:
        """The literal parts of a path anchored at a literal or at ``__file__``,
        or None. ``ROOT / "scripts" / "x.py"`` with a single module-level
        ``ROOT = Path(__file__).resolve().parents[1]`` gives ``["scripts", "x.py"]``."""
        if expr is None or depth > 20:
            return None
        value = _const_str(expr)
        if value is not None:
            return [value]
        if isinstance(expr, ast.Name):
            if expr.id == "__file__" and self.b.unbound("__file__"):
                return []
            bound = self.b.value_of(expr.id)
            return self._path(bound, depth + 1) if bound is not None else None
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
            left, right = self._path(expr.left, depth + 1), self._path(expr.right, depth + 1)
            return left + right if left is not None and right is not None else None
        if isinstance(expr, ast.Attribute) and expr.attr == "parent":
            inner = self._path(expr.value, depth + 1)
            return inner[:-1] if inner else inner
        if (
            isinstance(expr, ast.Subscript)
            and isinstance(expr.value, ast.Attribute)
            and expr.value.attr == "parents"
            and isinstance(expr.slice, ast.Constant)
            and isinstance(expr.slice.value, int)
        ):
            inner = self._path(expr.value.value, depth + 1)
            return inner[: max(0, len(inner) - expr.slice.value - 1)] if inner else inner
        if isinstance(expr, ast.Call) and not expr.keywords:
            func = expr.func
            parts = [self._path(arg, depth + 1) for arg in expr.args]
            if any(p is None for p in parts):
                return None
            joined = [x for p in parts for x in p]  # type: ignore[union-attr]
            if isinstance(func, ast.Attribute):
                if func.attr in ("resolve", "absolute", "expanduser") and not expr.args:
                    return self._path(func.value, depth + 1)
                if func.attr == "joinpath":
                    base = self._path(func.value, depth + 1)
                    return base + joined if base is not None else None
                if func.attr in ("join", "fspath") and isinstance(func.value, ast.Attribute):
                    root = func.value.value
                    if isinstance(root, ast.Name) and self.b.module_of(root.id) == "os":
                        return joined
                if func.attr == "fspath" and isinstance(func.value, ast.Name):
                    return joined if self.b.module_of(func.value.id) == "os" else None
                return None
            if isinstance(func, ast.Name):
                if func.id == "str" and self.b.unbound("str") and len(expr.args) == 1:
                    return joined
                one = self.b.single(func.id)
                if (
                    one
                    and one[0] == "from"
                    and one[1]
                    in {
                        ("pathlib", "Path"),
                        ("pathlib", "PurePath"),
                        ("pathlib", "PosixPath"),
                    }
                ):
                    return joined
        return None

    # -- scopes only for eager/lazy, never for resolution --------------------------

    def _enter(self, item: ast.AST) -> None:
        self.lazy += 1
        returns = getattr(item, "returns", None)
        for field_name, value in ast.iter_fields(item):
            if field_name == "returns":
                continue
            if isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.AST):
                        self.visit(child)
            elif isinstance(value, ast.AST):
                self.visit(value)
        self._visit_annotation(returns)
        self.lazy -= 1

    def visit_FunctionDef(self, item: ast.FunctionDef) -> None:
        self._enter(item)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_If(self, item: ast.If) -> None:
        test = item.test
        if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        ):
            self.visit(test)
            self.lazy += 1
            for stmt in item.body:
                self.visit(stmt)
            self.lazy -= 1
            for stmt in item.orelse:
                self.visit(stmt)
            return
        self.generic_visit(item)

    def visit_Expr(self, item: ast.Expr) -> None:
        if not _is_docstring_like(item):  # a bare string statement runs nothing
            self.generic_visit(item)

    # -- strings ----------------------------------------------------------------

    def visit_Constant(self, item: ast.Constant) -> None:
        if isinstance(item.value, str):
            self._string(item.value, item)

    def visit_JoinedStr(self, item: ast.JoinedStr) -> None:
        rendered = "".join(
            part.value if isinstance(part, ast.Constant) else "\0" for part in item.values
        )
        if PACKAGE in rendered and _TEMPLATE_RE.search(rendered):
            self._unresolved(item, "an f-string completing a unified_agent name")
        self.generic_visit(item)

    def visit_BinOp(self, item: ast.BinOp) -> None:
        if isinstance(item.op, ast.Add):
            folded = self._literal(item)
            if folded is not None:
                self._string(folded, item)
            elif self._open_prefix(item):
                self._unresolved(item, "a unified_agent name built with + at run time")
        elif isinstance(item.op, ast.Div):
            parts = _path_parts(item)
            if len(parts) >= 2:
                self.node.constants.add(_join_parts(parts))
        self.generic_visit(item)

    def _open_prefix(self, item: ast.AST) -> bool:
        if isinstance(item, ast.BinOp) and isinstance(item.op, ast.Add):
            left = self._literal(item.left)
            if left is not None and _OPEN_PREFIX_RE.search(left):
                return True
            return self._open_prefix(item.left)
        return False

    # -- names: loader-module tokens and loader references ------------------------

    def visit_Name(self, item: ast.Name) -> None:
        if item.id in self.b.radioactive:
            # Reached only outside an allowed direct call: bound, passed,
            # stored, indexed, shadowed, whatever it is bound to.
            self._unresolved(item, f"{item.id} used other than in a direct static loader call")
        elif isinstance(item.ctx, ast.Load) and (
            item.id in self.b.loader_aliases or item.id in _LOADERS
        ):
            self._unresolved(item, f"{item.id} used other than as a direct call")
        elif self.annotation:
            return
        elif isinstance(item.ctx, ast.Load) and item.id in self.b.process_modules:
            # Reached only when the module itself is a value (sp = subprocess),
            # not as the base of an attribute.
            self._unresolved(item, f"process module {item.id} used as a value")
        elif (
            isinstance(item.ctx, ast.Load)
            and (item.id in self.b.spawn_functions or self._spawn_alias(item.id) is not None)
            and not self._harmless_command(item)
        ):
            self._unresolved(item, f"{item.id} used other than as a direct call")

    def visit_arg(self, item: ast.arg) -> None:
        if item.arg in self.b.radioactive:
            self._unresolved(item, f"a parameter named {item.arg}")
        self._visit_annotation(item.annotation)

    def _visit_annotation(self, annotation: ast.AST | None) -> None:
        if annotation is not None:
            self.annotation += 1
            self.visit(annotation)
            self.annotation -= 1

    def visit_AnnAssign(self, item: ast.AnnAssign) -> None:
        self.visit(item.target)
        self._visit_annotation(item.annotation)
        if item.value is not None and not self._is_spawn_alias_binding(item.target):
            self.visit(item.value)

    def visit_Assign(self, item: ast.Assign) -> None:
        for target in item.targets:
            self.visit(target)
        if not (len(item.targets) == 1 and self._is_spawn_alias_binding(item.targets[0])):
            self.visit(item.value)

    def _is_spawn_alias_binding(self, target: ast.AST) -> bool:
        """The one binding of a spawn alias (``RUN = subprocess.run``): calls
        through the name are analyzed, so the reference here is not an escape."""
        return isinstance(target, ast.Name) and self._spawn_alias(target.id) is not None

    def visit_Attribute(self, item: ast.Attribute) -> None:
        if (
            isinstance(item.ctx, ast.Load)
            and item.attr in _LOADERS
            and not self._not_a_loader(item)
        ):
            self._unresolved(item, f"{item.attr} used other than as a direct call")
        if (
            not self.annotation
            and isinstance(item.ctx, ast.Load)
            and self._spawn_name(item) is not None
            and not self._harmless_command(item)
        ):
            # subprocess.run passed around: direct calls never visit their callee.
            self._unresolved(item, f"{item.attr} used other than as a direct call")
        value = item.value
        loaded = self._loaded_process_module(value)
        if loaded is not None:
            if _is_spawn(loaded, item.attr) or item.attr in _MODULE_NAMESPACE:
                self._unresolved(item, f"{item.attr} of process module {loaded} loaded by name")
            else:
                self.module_attr_ok.add(id(value))  # __import__("os").environ
        if isinstance(value, ast.Name) and value.id in self.b.process_modules:
            if item.attr in _MODULE_NAMESPACE:  # subprocess.__dict__["run"]
                self._unresolved(item, f"process module {value.id} read through {item.attr}")
            return  # os.path, subprocess.PIPE: an attribute, not the module as a value
        self.generic_visit(item)

    def visit_Subscript(self, item: ast.Subscript) -> None:
        """``local["git"]`` (plumbum) with a static program that is not Python
        can only run that program; any other use of ``local`` is unresolved."""
        value = item.value
        program = self._literal(item.slice)
        if (
            isinstance(value, ast.Name)
            and self.b.spawn_functions.get(value.id) == ("plumbum", "local")
            and program is not None
            and not _is_interpreter(program)
        ):
            self.visit(item.slice)
            return
        self.generic_visit(item)

    # -- calls ------------------------------------------------------------------

    def _not_a_loader(self, item: ast.Attribute) -> bool:
        """``re.compile`` (or ``regex``) on a name whose single module-level
        binding imports that module."""
        value = item.value
        return isinstance(value, ast.Name) and (
            (self.b.module_of(value.id), item.attr) in _NOT_LOADERS
        )

    def _callee(self, func: ast.AST) -> tuple[str, str]:
        """("loader", identifier), ("unresolved", why) or ("none", "")."""
        if isinstance(func, ast.Name):
            name = func.id
            if self.b.unbound(name):
                return ("loader", name) if name in _LOADERS else ("none", "")
            aliases = self.b.loader_aliases.get(name)
            if name in _LOADERS or aliases:
                one = self.b.single(name)
                if one and one[0] == "from" and one[1][1] in _LOADERS:  # type: ignore[index]
                    return ("loader", one[1][1])  # type: ignore[index]
                return ("unresolved", f"{name} is a loader name with ambiguous bindings")
            return ("none", "")
        if isinstance(func, ast.Attribute) and func.attr in _LOADERS:
            if self._not_a_loader(func):
                return ("none", "")
            base = _chain_base(func)
            if base is not None and base.id in self.b.radioactive:
                module = self.b.module_of(base.id)
                if module is None or not _is_loader_module(module):
                    return ("unresolved", f"{base.id} is not a single module-level import")
            return ("loader", func.attr)
        return ("none", "")

    def _allowed_module_call(self, func: ast.AST) -> bool:
        """``<loader module>.module_from_spec(...)``: part of the spec chain,
        loads nothing itself, on a single module-level import."""
        if not (isinstance(func, ast.Attribute) and func.attr == "module_from_spec"):
            return False
        base = _chain_base(func)
        module = self.b.module_of(base.id) if base is not None else None
        return module is not None and _is_loader_module(module)

    def visit_Call(self, item: ast.Call) -> None:
        func = item.func
        kind, detail = self._callee(func)
        skip_func = False
        if kind == "loader":
            skip_func = self._loader_call(item, detail)
        elif kind == "unresolved":
            self._unresolved(item, detail)
        elif self._allowed_module_call(func):
            skip_func = True
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if callee in _PATH_CALLS:
            parts = _path_parts(item)
            if len(parts) >= 2:
                self.node.constants.add(_join_parts(parts))
        if self._spawn_name(func) is not None:
            self._process_call(item, self._spawn_name(func))  # type: ignore[arg-type]
            skip_func = True
        elif callee == "split" and isinstance(func, (ast.Attribute, ast.Name)):
            self._shlex_split(item)
        knob = self._mp_knob_name(func)
        if knob is not None:
            self._mp_knob(item, knob)
        if callee == "getattr" and len(item.args) >= 2:
            attr = _const_str(item.args[1])
            if attr is None:
                # Only a loader or process module can hand back a loader or a
                # spawn function by a computed name in honest code; a computed
                # getattr on anything else is out of scope (see the threat
                # model in the module docstring).
                if self.b.holds_loader_module(item.args[0]):
                    self._unresolved(item, "a loader module's attribute fetched by a computed name")
                elif self._on_process_module(item.args[0]):
                    self._unresolved(
                        item, "a process module's attribute fetched by a computed name"
                    )
            elif attr in _LOADERS:
                self._unresolved(item, f"{attr} fetched with getattr()")
            elif attr in _MODULE_NAMESPACE and self._on_process_module(item.args[0]):
                # getattr(subprocess, "__dict__") is subprocess.__dict__.
                self._unresolved(item, f"a process module's {attr} fetched with getattr()")
            elif self._spawn_name(item) is not None and not self._harmless_command(item):
                # Reached only when the result is not called directly: a direct
                # call through getattr(subprocess, "run") is a spawn call.
                self._unresolved(item, f"{attr} fetched with getattr()")
            elif (
                _splatted_default(item)
                and self._spawn_name(fetched := self._fetched_attribute(item, attr))
                # sh.git, plumbum.cmd.git: a literal non-Python command can only
                # run that program, whatever the splat supplies (#2286).
                and not self._harmless_command(fetched)
            ):
                # getattr(subprocess, "run", *opts): _static_getattr() does not
                # read a splatted default, so the spawn is neither recognised
                # nor its default visited.
                self._unresolved(item, "getattr() of a default built at run time")
        if not skip_func:
            self.visit(func)
        elif isinstance(func, ast.Attribute) and _chain_base(func) is None:
            self.visit(func.value)  # spec.loader.exec_module(...): visit the receiver
        elif isinstance(func, ast.Call) and getattr(func.func, "id", "") == "getattr":
            # getattr(subprocess, "run", <default>)(...): the default still runs.
            for extra in func.args[2:]:
                self.visit(extra)
            for kw in func.keywords:
                self.visit(kw.value)
        args = list(item.args)
        if args and self._probes_module_attribute(item):
            args = args[1:]  # monkeypatch.setattr(os, ...), hasattr(os, ...)
        for arg in args:
            self.visit(arg)
        for kw in item.keywords:
            self.visit(kw.value)

    @staticmethod
    def _fetched_attribute(call: ast.Call, attr: str) -> ast.Attribute:
        """``getattr(X, "name", ...)`` read as ``X.name``, ignoring the default."""
        return ast.Attribute(value=call.args[0], attr=attr, ctx=ast.Load())

    def _mp_knob_name(self, func: ast.AST) -> str | None:
        """The multiprocessing knob a call reaches: by its written name, or
        through an imported alias (``from multiprocessing import
        set_forkserver_preload as preload``)."""
        if isinstance(func, ast.Attribute):
            return func.attr if func.attr in _MP_KNOBS else None
        if not isinstance(func, ast.Name):
            return None
        if func.id in _MP_KNOBS:
            return func.id
        for kind, _, payload in self.b.of.get(func.id, []):
            if kind == "from" and str(payload[1]) in _MP_KNOBS:  # type: ignore[index]
                return str(payload[1])  # type: ignore[index]
        return None

    def _probes_module_attribute(self, call: ast.Call) -> bool:
        """A process module as the first argument of ``monkeypatch.setattr``/
        ``delattr``, ``mock.patch.object``, ``hasattr``, or ``getattr`` with a
        static name: an attribute probed or patched, not the module passed on
        to something that could spawn with it."""
        first = call.args[0]
        if not (isinstance(first, ast.Name) and first.id in self.b.process_modules):
            return False
        func = call.func
        if isinstance(func, ast.Name):
            if func.id == "hasattr":
                return True
            if func.id == "getattr":
                return len(call.args) >= 2 and _const_str(call.args[1]) is not None
            return False
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr in ("setattr", "delattr"):
            return isinstance(func.value, ast.Name) and func.value.id == "monkeypatch"
        if func.attr == "object":
            patch = func.value
            return (isinstance(patch, ast.Name) and patch.id == "patch") or (
                isinstance(patch, ast.Attribute) and patch.attr == "patch"
            )
        return False

    def _arg(self, call: ast.Call, position: int, keyword: str) -> tuple[bool, ast.expr | None]:
        """(present, expr) for one argument; a ``*args``/``**kwargs`` hides it."""
        if any(isinstance(a, ast.Starred) for a in call.args[: position + 1]) or any(
            kw.arg is None for kw in call.keywords
        ):
            return True, None
        if len(call.args) > position:
            return True, call.args[position]
        for kw in call.keywords:
            if kw.arg == keyword:
                return True, kw.value
        return False, None

    def _loader_call(self, call: ast.Call, name: str) -> bool:
        """Record a loader call. True when it resolved, so its callee chain
        (a loader module token) is an allowed direct use."""
        position, keyword, kind = _LOADERS[name]
        if kind == "spec":
            return self._spec_call(call, name)
        present, expr = self._arg(call, position, keyword)
        if not present:
            self._unresolved(call, f"{name}() without the argument that names what it loads")
            return False
        if kind == "code":
            code = self._literal(expr)
            if code is None:
                self._unresolved(call, f"{name}() of code built at run time")
                return False
            self._code(code, call, f"{name}() code")
            return True
        if kind == "path":
            parts = self._path(expr)
            if parts is None:
                self._unresolved(call, f"{name}() of a path built at run time")
                return False
            if parts:
                self.node.constants.add(_join_parts(parts))
            return True
        target = self._literal(expr)
        if target is None:
            self._unresolved(call, f"{name}() of a computed module name")
            return False
        if target.startswith("."):
            if name != "import_module":
                self._unresolved(call, f"{name}() of a relative name")
                return False
            present, package_expr = self._arg(call, 1, "package")
            package = self._literal(package_expr) if present else None
            if package is None:
                self._unresolved(call, f"{name}({target!r}) relative to an unknown package")
                return False
            level = len(target) - len(target.lstrip("."))
            base = package.split(".")
            base = base[: len(base) - (level - 1)] if level - 1 < len(base) else []
            rest = target.lstrip(".")
            target = ".".join([*base, *([rest] if rest else [])])
        names = {target}
        if name == "__import__":
            present, level_expr = self._arg(call, 4, "level")
            if present and not (isinstance(level_expr, ast.Constant) and level_expr.value == 0):
                self._unresolved(call, "__import__() with a relative level")
                return False
            present, fromlist = self._arg(call, 3, "fromlist")
            if (
                present
                and fromlist is not None
                and not (isinstance(fromlist, ast.Constant) and fromlist.value is None)
            ):
                items = fromlist.elts if isinstance(fromlist, (ast.List, ast.Tuple)) else None
                values = [self._literal(e) for e in items] if items is not None else None
                if values is None or None in values:
                    self._unresolved(call, "__import__() with a computed fromlist")
                    return False
                names |= {f"{target}.{v}" for v in values}
            elif present and fromlist is None:
                self._unresolved(call, "__import__() with a hidden fromlist")
                return False
        if (
            name in _MODULE_RETURNING
            and target.split(".")[0] in _PROCESS_MODULES
            and id(call) not in self.module_attr_ok
        ):
            # A process module loaded by name and kept or passed on: the spawn
            # calls made through it are not visible. (One attribute that is not
            # a spawn function, read straight off it, is visit_Attribute's.)
            self._unresolved(call, f"process module {target} loaded by {name}()")
        self._imports(names)
        return True

    def _static_spec(self, expr: ast.AST | None) -> bool:
        """A spec made, through single module-level bindings only, by a static
        spec_from_file_location() or find_spec()."""
        if isinstance(expr, ast.Name):
            expr = self.b.value_of(expr.id)
        if not isinstance(expr, ast.Call):
            return False
        kind, name = self._callee(expr.func)
        if kind != "loader" or name not in _SPEC_PRODUCERS:
            return False
        position, keyword, arg_kind = _LOADERS[name]
        present, arg = self._arg(expr, position, keyword)
        if not present:
            return False
        if arg_kind == "path":
            return self._path(arg) is not None
        return self._literal(arg) is not None

    def _spec_call(self, call: ast.Call, name: str) -> bool:
        """exec_module()/load_module() run whatever spec they are handed. They
        resolve only when that spec is traced, through single module-level
        bindings, to a static spec_from_file_location() or find_spec(); the
        producer recorded the dependency."""
        func = call.func
        receiver = func.value if isinstance(func, ast.Attribute) else None
        spec_ok = (
            isinstance(receiver, ast.Attribute)
            and receiver.attr == "loader"
            and self._static_spec(receiver.value)
        )
        if name == "exec_module":
            present, module = self._arg(call, 0, "module")
            if isinstance(module, ast.Name):
                module = self.b.value_of(module.id)
            from_spec = (
                isinstance(module, ast.Call)
                and self._allowed_module_call(module.func)
                and len(module.args) == 1
                and self._static_spec(module.args[0])
            )
            if present and from_spec and (receiver is None or spec_ok):
                return True
        elif spec_ok:
            return True
        self._unresolved(call, f"{name}() of a spec not traced to a static source")
        return False

    def _argv_value(self, elt: ast.expr) -> object:
        """A static string for one argv element, ``_INTERP``, ``_STAR``, or None."""
        if isinstance(elt, ast.Starred):
            return _STAR
        if isinstance(elt, ast.Attribute) and elt.attr == "executable":
            return _INTERP  # sys.executable
        value = self._literal(elt)
        if value is not None:
            return value
        parts = self._path(elt)
        if parts:
            return _join_parts(parts)
        if isinstance(elt, ast.Name) and "python" in elt.id.lower():
            return _INTERP
        return None

    def _argv(self, elts: list[ast.expr], where: ast.AST) -> None:
        self._argv_values([self._argv_value(e) for e in elts], where)

    def _argv_values(self, values: list[object], where: ast.AST, stdin: object = None) -> None:
        """A list that runs Python: one whose elements include an interpreter
        (``[sys.executable, ...]``, ``["uv", "run", "python3", ...]``), or one
        whose program is not static but which carries a CPython ``-c``/``-m``
        option. A literal program that is not Python (``git -c``, ``bash -c``)
        is not, and neither is a tuple of flags in a membership test. ``stdin``
        is what a spawn call feeds the process (see :meth:`_stdin`)."""
        start = next((i for i, v in enumerate(values) if _is_interpreter(v)), None)
        if start is not None:
            self._cpython(values, start + 1, where, stdin)
            return
        if not values or isinstance(values[0], str):
            return
        flag = next((i for i, v in enumerate(values) if _selects_code_or_module(v)), None)
        if flag is not None:
            self._cpython(values, flag, where, stdin)

    def _cpython(self, values: list[object], i: int, where: ast.AST, stdin: object = None) -> None:
        """Parse ``values[i:]`` with CPython's command-line grammar.

        Short options may be combined (``-Bc``, ``-Im``); ``-c`` and ``-m`` take
        the rest of their token or the next element and end the options, as
        do ``--``, ``-`` and a script path. ``-W``/``-X``/``-Q`` consume their
        argument; ``-h``/``-V``/``--help``/``--version`` run nothing. Every
        element up to what runs must be static; after ``-m pytest`` every
        element must be, and each ``-p NAME`` is a plugin import. ``-``, or no
        script at all when a spawn call gives the process a stdin, reads the
        script from stdin (:meth:`_stdin_script`).
        """
        n = len(values)
        while i < n:
            token = values[i]
            if not isinstance(token, str):
                self._unresolved(where, "python command line with an element built at run time")
                return
            if token == "-":
                self._stdin_script(where, stdin, explicit=True)
                return
            if token == "--":
                # The next element is the script that runs.
                script = values[i + 1] if i + 1 < n else None
                if isinstance(script, str):
                    self.node.constants.add(script.removeprefix("./").strip("/"))
                else:
                    self._unresolved(where, "python script after -- built at run time")
                return
            if token.startswith("--"):
                if token.startswith(_PY_INFO_LONG):
                    return
                i += 2 if token in _PY_LONG_WITH_ARG else 1
                continue
            if not token.startswith("-") or len(token) == 1:
                # The end of the options: this element is the script that runs.
                self.node.constants.add(token.removeprefix("./").strip("/"))
                return
            for j, letter in enumerate(token[1:], start=1):
                if letter in _PY_INFO_SHORT:
                    return
                if letter not in _PY_SELECTORS and letter not in _PY_ARG_OPTIONS:
                    continue
                rest = token[j + 1 :]
                if rest:
                    argument: object = rest
                else:
                    i += 1
                    if i >= n:
                        self._unresolved(where, f"python -{letter} with its argument hidden")
                        return
                    argument = values[i]
                if not isinstance(argument, str):
                    self._unresolved(where, f"python -{letter} with an argument built at run time")
                    return
                if letter == "c":
                    self._code(argument, where, "python -c code")
                    return
                if letter == "m":
                    self._imports({argument})
                    if argument in _PYTEST_MODULES:
                        self._pytest_args(values[i + 1 :], where)
                    return
                break  # -W/-X/-Q took its argument
            i += 1
        self._stdin_script(where, stdin, explicit=False)

    def _stdin_script(self, where: ast.AST, stdin: object, *, explicit: bool) -> None:
        """Python reads its script from stdin: a static ``input=`` script gets
        the same analysis as a file; any other stdin is unresolved. With no
        script and no stdin given, it reads the inherited stdin, which honest
        tests do not feed a script (``python -`` there stays unresolved)."""
        if isinstance(stdin, str):
            self._code(stdin, where, "python stdin script")
        elif explicit or stdin is not None:
            self._unresolved(where, "python reads its script from stdin")

    def _pytest_args(self, values: list[object], where: ast.AST) -> None:
        """``python -m pytest ...``: every element must be static, and each
        ``-p NAME`` / ``-pNAME`` loads a plugin (``-p no:NAME`` disables one)."""
        plugins: set[str] = set()
        for k, value in enumerate(values):
            if not isinstance(value, str):
                self._unresolved(where, "pytest command line with an element built at run time")
                return
            name = None
            if value == "-p" and k + 1 < len(values):
                name = values[k + 1]
            elif value.startswith("-p") and len(value) > 2 and not value.startswith("--"):
                name = value[2:]
            if isinstance(name, str) and not name.startswith("no:"):
                plugins.add(name)
        if plugins:
            self._imports(plugins)

    def _spawn_name(self, func: ast.AST) -> tuple[str, str] | None:
        """(module, function) when ``func`` is a process-spawn callable reached
        through any import binding of its module, else None."""
        if isinstance(func, ast.Attribute):
            base = _chain_base(func)
            if base is not None:
                for module in self.b.process_modules.get(base.id, ()):
                    if _is_spawn(module, func.attr) or (
                        module in _COMMAND_MODULES and self._fixed_command(func) is not None
                    ):
                        return (module, func.attr)
            return None
        if isinstance(func, ast.Name):
            if func.id in self.b.spawn_functions:
                return self.b.spawn_functions[func.id]
            return self._spawn_alias(func.id)
        fetched = self._static_getattr(func)
        return self._spawn_name(fetched) if fetched is not None else None

    @staticmethod
    def _static_getattr(expr: ast.AST | None) -> ast.Attribute | None:
        """``getattr(X, "name"[, default])`` read as ``X.name``."""
        if not (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "getattr"
            and len(expr.args) in (2, 3)
            and not expr.keywords
            and not any(isinstance(a, ast.Starred) for a in expr.args)
        ):
            return None
        attr = _const_str(expr.args[1])
        if attr is None:
            return None
        return ast.copy_location(ast.Attribute(value=expr.args[0], attr=attr, ctx=ast.Load()), expr)

    def _loaded_process_module(self, expr: ast.AST) -> str | None:
        """The process module ``expr`` loads by a static name
        (``__import__("os")``, ``importlib.import_module("subprocess")``)."""
        if not isinstance(expr, ast.Call):
            return None
        kind, name = self._callee(expr.func)
        if kind != "loader" or name not in _MODULE_RETURNING:
            return None
        position, keyword, _ = _LOADERS[name]
        target = self._literal(self._arg(expr, position, keyword)[1])
        top = target.split(".")[0] if target else None
        return top if top in _PROCESS_MODULES else None

    def _on_process_module(self, expr: ast.AST) -> bool:
        """``expr`` is a process module or an attribute chain from one
        (``os``, ``asyncio.subprocess``)."""
        base = _chain_base(expr)
        return base is not None and base.id in self.b.process_modules

    def _fixed_command(self, func: ast.AST | None) -> tuple[str, bool] | None:
        """``(program, exact)`` for one fixed sh or plumbum command: ``sh.git``,
        ``sh.contrib.git``, ``plumbum.cmd.git``, or a name imported by ``from sh
        import git``/``from plumbum.cmd import git`` (a single module-level
        binding). ``exact`` is False when more attributes follow
        (``sh.git.bake``): the command, with arguments that are not read."""
        func = self._static_getattr(func) or func
        parts: list[str] = []
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if not isinstance(func, ast.Name):
            return None
        parts.reverse()
        if func.id in self.b.commands:
            if self.b.single(func.id) is None:
                return None
            return self.b.commands[func.id], not parts
        module = self.b.module_of(func.id)
        full = [*module.split("."), *parts] if module else []
        if full[:1] == ["sh"]:
            rest = full[2:] if full[1:2] == ["contrib"] else full[1:]
            if not rest or rest[0] == "Command" or not _is_spawn("sh", rest[0]):
                return None
        elif full[:2] == ["plumbum", "cmd"]:
            rest = full[2:]
            if not rest or rest[0].startswith("_"):
                return None
        else:
            return None
        return rest[0], len(rest) == 1

    def _harmless_command(self, expr: ast.AST) -> bool:
        """A fixed sh or plumbum command whose program is a literal that is not
        Python: it can only run that program, however it is used."""
        fixed = self._fixed_command(expr)
        return fixed is not None and not _is_interpreter(fixed[0])

    def _spawn_alias(self, name: str) -> tuple[str, str] | None:
        """``RUN = subprocess.run``: a spawn function bound exactly once in the
        whole file to ``name``, so calls through ``name`` are spawn calls."""
        busy = self.__dict__.setdefault("_alias_busy", set())
        if name in busy:
            return None  # a = b; b = a
        bound = self.b.value_of(name)
        if not isinstance(bound, (ast.Attribute, ast.Name, ast.Call)):
            return None  # a Call only as getattr(subprocess, "run")
        busy.add(name)
        try:
            return self._spawn_name(bound)
        finally:
            busy.discard(name)

    def _shlex_values(self, expr: ast.AST | None) -> list[object] | None:
        command = self._literal(expr)
        if command is None:
            return None
        try:
            return list(shlex.split(command))
        except ValueError:
            return None

    def _command_values(self, expr: ast.AST | None, *, shell: bool) -> list[object] | None:
        """The argv a command expression spawns, or None when it is not static.

        A list or tuple (directly, or a name bound to one exactly once); a
        static string, split as a shell would; ``shlex.split(<static>)``. With
        ``shell`` a list's first element is the shell command and the rest are
        the shell's ``$0``, ``$1``...; the literal list itself is still scanned
        as an argv by :meth:`visit_List`."""
        if isinstance(expr, ast.Name):
            bound = self.b.value_of(expr.id)
            if isinstance(bound, (ast.List, ast.Tuple)):
                expr = bound
        if isinstance(expr, (ast.List, ast.Tuple)):
            values = [self._argv_value(e) for e in expr.elts]
            if shell and values:
                head = values[0]
                if head is _INTERP:
                    return [head]
                if not isinstance(head, str):
                    return None
                try:
                    return list(shlex.split(head))
                except ValueError:
                    return None
            return values
        if (
            isinstance(expr, ast.Call)
            and (getattr(expr.func, "attr", None) or getattr(expr.func, "id", None)) == "split"
            and len(expr.args) == 1
        ):
            return self._shlex_values(expr.args[0])
        return self._shlex_values(expr)

    def _process_call(self, call: ast.Call, spawn: tuple[str, str]) -> None:
        """A process spawn. Its command is bound the way the real signature
        binds it; a fully static command is parsed with CPython's grammar, a
        literal program that is not an interpreter is ignored, and any other
        command could start Python and makes the file unresolved."""
        module, name = spawn
        stdin: object = None
        readings: list[list[object] | None]
        if module in _COMMAND_MODULES:
            readings = [self._command_module_values(call, name)]
            stdin = self._stdin(call, "_in" if module == "sh" else None, None)
        elif name in _SHELL_STRING_SPAWNS:
            present, expr = self._arg(call, 0, "cmd" if name != "system" else "command")
            readings = [self._command_values(expr, shell=True) if present else None]
            stdin = self._stdin(call, None, "stdin" if module == "asyncio" else None)
        elif module == "subprocess" or (module == "pty" and name == "spawn"):
            keyword = "args" if module == "subprocess" else "argv"
            present, expr = self._arg(call, 0, keyword)
            shell = self._shell(call) if module == "subprocess" else False
            if not present:
                readings = [None]
            elif shell is None:  # computed: the command is read both ways
                readings = [
                    self._command_values(expr, shell=False),
                    self._command_values(expr, shell=True),
                ]
            else:
                readings = [self._command_values(expr, shell=shell)]
            if shell is False and isinstance(expr, (ast.List, ast.Tuple)):
                self.argv_done.add(id(expr))  # read here, with its stdin
            if module == "subprocess":
                stdin = self._stdin(call, "input", "stdin")
        elif module == "asyncio":  # create_subprocess_exec(program, *args)
            readings = [[self._argv_value(a) for a in call.args]]
            stdin = self._stdin(call, None, "stdin")
        else:
            readings = [self._os_values(call, name)]
        if any(values is None for values in readings):
            self._unresolved(call, f"{name}() of a command built at run time")
        for values in readings:
            if not values:
                continue
            if any(_is_interpreter(v) for v in values):
                self._argv_values(values, call, stdin)
            elif not isinstance(values[0], str):  # else a literal program that is not Python
                self._unresolved(call, f"{name}() of a program built at run time")
                self._argv_values(values, call, stdin)  # a -c/-m it carries is still read

    @staticmethod
    def _shell(call: ast.Call) -> bool | None:
        """``shell=``: False when absent or a literal false, True for a literal
        true, None when computed."""
        for kw in call.keywords:
            if kw.arg == "shell":
                return bool(kw.value.value) if isinstance(kw.value, ast.Constant) else None
        return False

    def _stdin(self, call: ast.Call, input_kw: str | None, stdin_kw: str | None) -> object:
        """What a spawn call feeds the process: the static script of
        ``input_kw`` (``input=``, sh's ``_in=``), ``_PIPED`` for any other
        input or ``stdin_kw`` that is not None, or None when it gives none."""
        if any(kw.arg is None for kw in call.keywords):
            return _PIPED  # **kwargs could carry either
        for kw in call.keywords:
            if input_kw is not None and kw.arg == input_kw:
                script = self._static_input(kw.value)
                return script if script is not None else _PIPED
        for kw in call.keywords:
            if stdin_kw is not None and kw.arg == stdin_kw:
                if not (isinstance(kw.value, ast.Constant) and kw.value.value is None):
                    return _PIPED
        return None

    def _static_input(self, expr: ast.AST) -> str | None:
        """A static ``input=``: a string, bytes, or ``<static>.encode()``."""
        if isinstance(expr, ast.Name):
            expr = self.b.value_of(expr.id) or expr
        if isinstance(expr, ast.Constant) and isinstance(expr.value, bytes):
            try:
                return expr.value.decode()
            except UnicodeDecodeError:
                return None
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Attribute)
            and expr.func.attr == "encode"
            and not expr.args
            and not expr.keywords
        ):
            return self._literal(expr.func.value)
        return self._literal(expr)

    def _command_module_values(self, call: ast.Call, name: str) -> list[object] | None:
        """sh and plumbum: the program is the command's name (``sh.python3``,
        ``from plumbum.cmd import python3``) and the positional arguments
        follow it. ``sh.Command(program)`` builds a command that runs later
        with arguments that are not read, and so does a command reached past
        another attribute (``sh.python3.bake``). sh turns a keyword without a
        leading ``_`` into an option, which is not read either. Anything else
        (``plumbum.local``, a command reached through a name that is not one
        fixed command) is not static."""
        func = call.func
        if isinstance(func, ast.Name) and self._spawn_alias(func.id) is not None:
            func = self.b.value_of(func.id)  # type: ignore[assignment]
        if name == "Command":
            if not call.args:
                return None
            return [self._argv_value(call.args[0]), None]
        fixed = self._fixed_command(func)
        if fixed is None:
            return None
        program, exact = fixed
        if not exact:
            return [program, None]
        values: list[object] = [program, *(self._argv_value(a) for a in call.args)]
        values += [None for kw in call.keywords if not (kw.arg or "").startswith("_")]
        return values

    def _mp_knob(self, call: ast.Call, name: str) -> None:
        """``set_executable(path)`` picks the interpreter multiprocessing's child
        runs, and ``set_forkserver_preload(names)`` imports modules in the
        forkserver: the first must be static, the second a static list (one
        import edge per name)."""
        _, expr = self._arg(call, *_MP_KNOBS[name])
        if name == "set_executable":
            if expr is None or self._argv_value(expr) in (None, _STAR):
                self._unresolved(call, "set_executable() of a path built at run time")
            return
        if isinstance(expr, ast.Name):
            expr = self.b.value_of(expr.id)
        items = expr.elts if isinstance(expr, (ast.List, ast.Tuple)) else None
        names = [self._literal(e) for e in items] if items is not None else None
        if names is None or None in names:
            self._unresolved(call, "set_forkserver_preload() of module names built at run time")
            return
        self._imports({n for n in names if n})

    def _os_values(self, call: ast.Call, name: str) -> list[object] | None:
        """``os.exec*``/``spawn*``/``posix_spawn*``: the program path, then the
        arguments after argv[0], from the list (``*v*``) or the varargs (``*l*``)."""
        if any(isinstance(a, ast.Starred) for a in call.args):
            return None
        args = list(call.args)
        if name.startswith("posix_spawn"):
            path_at, rest = 0, 1
        elif name.startswith("spawn"):
            path_at, rest = 1, 2
        elif name.startswith("exec"):
            path_at, rest = 0, 1
        else:
            return None
        if len(args) <= path_at:
            return None
        program = self._argv_value(args[path_at])
        # execl, execle, execlp, execlpe, spawnl, spawnle, ... take varargs;
        # the *e forms end with an environment, not an argument.
        suffix = name.removeprefix("exec") if name.startswith("exec") else name[len("spawn") :]
        variadic = not name.startswith("posix_spawn") and suffix[:1] == "l"
        env_last = variadic and suffix.endswith("e")
        if variadic:
            tail = args[rest + 1 : len(args) - 1 if env_last else None]
            return [program, *(self._argv_value(a) for a in tail)]
        if len(args) <= rest:
            return None
        argv = args[rest]
        if isinstance(argv, ast.Name):
            argv = self.b.value_of(argv.id) or argv
        if not isinstance(argv, (ast.List, ast.Tuple)):
            return None
        return [program, *(self._argv_value(e) for e in argv.elts[1:])]

    def _shlex_split(self, call: ast.Call) -> None:
        """``shlex.split(<static>)`` outside a spawn call: parse it if it is a
        Python command line."""
        func = call.func
        base = _chain_base(func) if isinstance(func, ast.Attribute) else None
        is_shlex = (base is not None and self.b.module_of(base.id) == "shlex") or (
            isinstance(func, ast.Name) and self.b.single(func.id) == ("from", ("shlex", "split"))
        )
        if is_shlex and call.args:
            values = self._shlex_values(call.args[0])
            if values:
                self._argv_values(values, call)

    def check_pytest_plugins(self) -> None:
        """``pytest_plugins = [...]`` loads each named module as a plugin: one
        import edge per static name, unresolved for anything else."""
        found = self.b.of.get("pytest_plugins")
        if not found:
            return
        value = self.b.value_of("pytest_plugins")
        if value is None or not found[0][1]:
            self._unresolved(value or self.tree, "pytest_plugins not a single static assignment")
            return
        items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        names = [self._literal(e) for e in items]
        if any(isinstance(e, ast.Starred) for e in items) or None in names:
            self._unresolved(value, "pytest_plugins built at run time")
            return
        self._imports({n for n in names if n})

    def visit_List(self, item: ast.List | ast.Tuple) -> None:
        if id(item) not in self.argv_done:
            self._argv(item.elts, item)
        self.generic_visit(item)

    visit_Tuple = visit_List

    def _code(self, code: str, where: ast.AST, label: str) -> None:
        """Run the same analysis on code the file executes."""
        try:
            tree = ast.parse(textwrap.dedent(code))
        except (SyntaxError, ValueError):
            try:
                tree = ast.parse(code.strip(), mode="eval")
            except (SyntaxError, ValueError):
                # Python refuses to run it, so it imports nothing: no edge, and
                # nothing hidden either. (A -c "user.name=x" given to git, say.)
                return
        _analyze(self.node, tree, lazy=bool(self.lazy), where=f"{label}: ")


def _analyze(node: _Node, tree: ast.AST, *, lazy: bool, where: str = "") -> None:
    body = tree.body if isinstance(tree, ast.Module) else []
    found: list[tuple[ast.stmt, bool]] = []
    _import_statements(body, lazy, found)
    for item, item_lazy in found:
        names = _stmt_names(node, item)
        node.imports |= names
        if not item_lazy:
            node.eager |= names
    analyzer = _Analyzer(node, tree, lazy=lazy, where=where)
    if not where:
        analyzer.check_pytest_plugins()
    analyzer.visit(tree)


def _parse(node: _Node, text: str) -> None:
    try:
        tree = ast.parse(text, filename=node.path)
    except (SyntaxError, ValueError) as exc:
        node.unresolved.append(f"line {getattr(exc, 'lineno', '?')}: does not parse")
    else:
        _analyze(node, tree, lazy=False)
    node.tree_scan = bool(_TREE_SCAN_RE.search(text))


# -- graph --------------------------------------------------------------------


class ImportGraph:
    """Reverse dependency graph over the repo's Python files."""

    def __init__(self, root: Path | str, *, declared_edges: bool = True) -> None:
        self.root = Path(root)
        self.nodes: dict[str, _Node] = {}
        # Namespaces: src modules are absolute; tests and scripts are what a
        # test or script imports by bare name once its directory is on sys.path.
        # A name can belong to several files (tests/a/support.py and
        # tests/b/support.py are both "support"); it maps to all of them.
        self.src_modules: dict[str, set[str]] = defaultdict(set)
        self.test_modules: dict[str, set[str]] = defaultdict(set)
        self.script_modules: dict[str, set[str]] = defaultdict(set)
        # src modules by their name inside the package ("tools.vault"), for a
        # bare import from a file that put src/unified_agent on sys.path.
        self.src_bare: dict[str, set[str]] = defaultdict(set)
        # file -> files that depend on it, by an eager or a lazy edge
        self.eager_dependents: dict[str, set[str]] = defaultdict(set)
        self.lazy_dependents: dict[str, set[str]] = defaultdict(set)
        # The subset of eager edges the component map declares rather than an
        # import (task #2652). They belong in change reachability but must not
        # carry an unresolved module's taint into every test that imports the
        # components hub.
        self.declared_dependents: dict[str, set[str]] = defaultdict(set)
        self.constant_index: dict[str, set[str]] = defaultdict(set)
        self.importers_by_name: dict[str, set[str]] = defaultdict(set)
        self.tree_scanners: set[str] = set()
        self.test_files: set[str] = set()
        # file -> imports it makes that cannot be read statically
        self.unresolved: dict[str, list[str]] = {}
        # Plugin modules loaded into every test by ``-p NAME`` in addopts.
        self.global_plugins: set[str] = set()
        self._build()
        if declared_edges:
            self._apply_declared_component_edges()
        for name in _addopts_plugins(self.root):
            self.global_plugins |= self._resolve("tests/conftest.py", name)

    def _apply_declared_component_edges(self) -> None:
        """``components`` depends on every module the component map declares.

        This is the edge K4b would otherwise lose (task #2603). It is a no-op on
        selection today, because each manifest already imports its own component's
        modules and ``components`` imports every manifest -- which is exactly what
        makes the equivalence checkable before anything becomes conditional.

        A map that cannot be read fails closed: every source module is declared,
        so a broken map widens the selection instead of shrinking it.

        The edge is recorded in ``declared_dependents`` as well as in
        ``eager_dependents``: selection follows it, but the unresolved-test
        closure excludes it (task #2652), so a dynamic import in a declared
        module does not taint every test that imports the components hub.
        Only edges the map adds are recorded there: a real import from
        ``components`` that coincides with a declared module keeps its taint.
        """
        anchor = f"{SRC_DIR}/components/__init__.py"
        if anchor not in self.nodes:
            return
        declared = declared_component_modules(self.root)
        if declared is None:
            declared = {
                rel for rel in self.nodes if rel.startswith(f"{SRC_DIR}/") and rel != anchor
            }
        for rel in declared:
            if rel in self.nodes and rel != anchor:
                if anchor not in self.eager_dependents[rel]:
                    self.declared_dependents[rel].add(anchor)
                self.eager_dependents[rel].add(anchor)

    def _discover(self, top: str, namespace: dict[str, set[str]], prefix: str = "") -> None:
        base = self.root / top
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel_in = path.relative_to(base)
            rel = path.relative_to(self.root).as_posix()
            module, is_pkg = _module_name(base, rel_in, by_path=bool(prefix))
            if prefix:
                module = f"{prefix}.{module}" if module else prefix
            self.nodes[rel] = _Node(rel, module, is_pkg)
            if module:
                namespace[module].add(rel)
                if prefix and module.startswith(prefix + "."):
                    self.src_bare[module[len(prefix) + 1 :]].add(rel)

    def _build(self) -> None:
        self._discover(SRC_DIR, self.src_modules, prefix=PACKAGE)
        self._discover("tests", self.test_modules)
        self._discover("scripts", self.script_modules)
        for rel, node in self.nodes.items():
            try:
                text = (self.root / rel).read_text(errors="replace")
            except OSError as exc:
                node.unresolved.append(f"cannot be read ({type(exc).__name__})")
                text = ""
            _parse(node, text)
            if node.unresolved:
                self.unresolved[rel] = node.unresolved
            if is_test_file(rel):
                self.test_files.add(rel)
                if node.tree_scan:
                    self.tree_scanners.add(rel)
            for constant in node.constants:
                self.constant_index[constant].add(rel)
            for name in node.imports:
                self.importers_by_name[name].add(rel)
                edges = self.eager_dependents if name in node.eager else self.lazy_dependents
                for target in self._resolve(rel, name) - {rel}:
                    edges[target].add(rel)
            # Importing a.b.c runs a/__init__ and a/b/__init__ first.
            for parent in self._parent_packages(rel):
                self.eager_dependents[parent].add(rel)
        for target, eager in self.eager_dependents.items():
            self.lazy_dependents[target] -= eager

    def _resolve(self, rel: str, name: str) -> set[str]:
        """Every file a dotted import name can land in, walking up to the nearest
        module. A unified_agent name lands in src. A bare name lands in the union
        of every file with that name in tests/, scripts/ and src, whoever imports
        it: which one Python picks depends on sys.path at run time (a test may
        put scripts/ first), which the graph cannot see."""
        if name == PACKAGE or name.startswith(PACKAGE + "."):
            spaces = [self.src_modules]
        else:
            spaces = [self.test_modules, self.script_modules, self.src_bare]
        found: set[str] = set()
        for space in spaces:
            probe = name
            while probe:
                if probe in space:
                    found |= space[probe]
                    break
                probe = probe.rpartition(".")[0]
        return found

    def _parent_packages(self, rel: str) -> set[str]:
        node = self.nodes[rel]
        if rel.startswith("src/"):
            space = self.src_modules
        elif rel.startswith("tests/"):
            space = self.test_modules
        else:
            space = self.script_modules
        parts = node.module.split(".") if node.module else []
        parents: set[str] = set()
        for i in range(1, len(parts)):
            parents |= space.get(".".join(parts[:i]), set())
        return parents - {rel}

    def path_referrers(self, path: str, *, prefixes: bool = True) -> set[str]:
        """Files whose string constants name ``path``: in full, by a trailing run
        of segments (``"scripts" / "x.py"`` leaves ``"x.py"``), or by a directory
        prefix of two or more segments (``".github/workflows"``)."""
        parts = path.split("/")
        found: set[str] = set()
        for i in range(len(parts)):
            suffix = "/".join(parts[i:])
            if i == len(parts) - 1 and suffix in _AMBIGUOUS_BASENAMES:
                continue
            found |= self.constant_index.get(suffix, set())
        for i in range(2, len(parts) if prefixes else 0):
            found |= self.constant_index.get("/".join(parts[:i]), set())
        found.discard(path)
        return found

    def importers_of_missing(self, path: str) -> set[str] | None:
        """Files that still import a deleted or renamed-away module by any name
        it could have been imported as.

        The names are the ones discovery gives a live file: under src, the
        dotted ``unified_agent.<path>`` plus the package-relative name and the
        bare basename (a file that put ``src/unified_agent`` on sys.path); under
        tests and scripts, the pytest rootdir name and the bare basename. Any
        file importing one of them counts, whatever its namespace, the same
        union :meth:`_resolve` uses. None when the path is not one the graph
        can name.
        """
        if not path.endswith(".py"):
            return None
        rel = Path(path)
        stem = rel.parent.name if rel.stem == "__init__" else rel.stem
        names: set[str] = {stem} if stem else set()
        if path.startswith(SRC_DIR + "/"):
            parts = path[len("src/") : -len(".py")].split("/")
            dotted = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            names.add(dotted)
            if dotted.startswith(PACKAGE + "."):
                names.add(dotted[len(PACKAGE) + 1 :])
        else:
            top = path.split("/", 1)[0]
            if top not in ("tests", "scripts"):
                return None
            module, _ = _module_name(self.root / top, Path(path[len(top) + 1 :]), by_path=False)
            if module:
                names.add(module)
        names.discard("")
        if not names or names == {PACKAGE}:
            return None
        hits: set[str] = set()
        for name, files in self.importers_by_name.items():
            if any(name == n or name.startswith(n + ".") for n in names):
                hits |= files
        hits.discard(path)
        return hits

    def conftest_scope(self, rel: str) -> set[str]:
        """Test files a directory-level conftest.py applies to (never the root one)."""
        if not rel.startswith("tests/") or not rel.endswith("/conftest.py"):
            return set()
        directory = rel[: -len("conftest.py")]
        if directory == "tests/":
            return set()
        return {t for t in self.test_files if t.startswith(directory)}

    def closure(
        self,
        seeds: Iterable[str],
        depth: str = "transitive",
        *,
        follow_declared: bool = True,
    ) -> set[str]:
        """Every file reachable backwards from ``seeds`` (seeds included).

        See the module docstring for what ``depth`` follows. ``follow_declared``
        controls the component-map edges (task #2652): selection follows them,
        the unresolved-test closure does not, so a declared module's dynamic
        import cannot taint every test that imports the components hub.
        """
        if depth not in DEPTHS:
            raise ValueError(f"unknown depth {depth!r}; expected one of {DEPTHS}")
        seeds = set(seeds)
        seen: set[str] = set()
        queue = deque(seeds)
        while queue:
            rel = queue.popleft()
            if rel in seen:
                continue
            seen.add(rel)
            is_seed = rel in seeds
            if depth == "direct" and not is_seed and rel.startswith(SRC_DIR + "/"):
                continue
            nxt = set(self.eager_dependents.get(rel, ()))
            if not follow_declared:
                nxt -= self.declared_dependents.get(rel, set())
            lazy = self.lazy_dependents.get(rel, set())
            if depth != "transitive" or is_seed or not rel.startswith(SRC_DIR + "/"):
                nxt |= lazy
            else:
                nxt |= {d for d in lazy if not d.startswith(SRC_DIR + "/")}
            nxt |= self.path_referrers(rel)
            nxt |= self.conftest_scope(rel)
            queue.extend(nxt - seen)
        return seen


# -- selection ----------------------------------------------------------------


@dataclass
class Selection:
    """What a change needs. ``full_reasons`` non-empty means run the whole suite."""

    tests: list[str]
    inventory: list[str]
    full_reasons: list[str]
    docs_changed: bool = False
    # Known blind spots of this selection: src modules whose dynamic imports
    # cannot be read, so what they load is not followed.
    caveats: list[str] = field(default_factory=list)
    # Tests selected only because they (or a helper or script they use) have an
    # import the analysis cannot read; they run on every Python change.
    unresolved_tests: list[str] = field(default_factory=list)
    # Tests selected only because they scan the whole source tree as text: they
    # run on any src change but do not exercise the changed module.
    scanner_tests: list[str] = field(default_factory=list)

    @property
    def full(self) -> bool:
        return bool(self.full_reasons)

    def paths(self, *, inventory: bool = True) -> list[str]:
        out = set(self.tests)
        if inventory:
            out |= set(self.inventory)
        return sorted(out)


def _addopts_plugins(root: Path) -> set[str]:
    """Plugins ``-p NAME`` in a static pytest ``addopts`` (pyproject.toml,
    pytest.ini, tox.ini or setup.cfg); ``-p no:NAME`` disables one."""
    import configparser
    import tomllib

    texts: list[str] = []
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text())
        addopts = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts")
        if isinstance(addopts, str):
            texts.append(addopts)
        elif isinstance(addopts, list):
            texts.append(" ".join(str(a) for a in addopts))
    except (OSError, ValueError):
        pass
    for name, section in (
        ("pytest.ini", "pytest"),
        ("tox.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
    ):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(root / name)
        except (OSError, configparser.Error):
            continue
        if parser.has_option(section, "addopts"):
            texts.append(parser.get(section, "addopts"))
    plugins: set[str] = set()
    for text in texts:
        try:
            tokens = shlex.split(text)
        except ValueError:
            continue
        for k, token in enumerate(tokens):
            name = None
            if token == "-p" and k + 1 < len(tokens):
                name = tokens[k + 1]
            elif token.startswith("-p") and len(token) > 2:
                name = token[2:]
            if name and not name.startswith("no:"):
                plugins.add(name)
    return plugins


def _present(root: Path, path: str) -> bool:
    """In the tree, even as a symlink whose target is gone."""
    full = root / path
    return full.exists() or full.is_symlink()


def _name_mapped(root: Path, path: str) -> set[str]:
    """The older convention: ``src/**/<stem>.py`` or ``scripts/<stem>.py`` maps to
    ``tests/**/test_<stem>.py`` (``__init__.py`` maps to its package name)."""
    if not path.endswith(".py") or not path.startswith(("src/", "scripts/")):
        return set()
    rel = Path(path)
    stem = rel.parent.name if rel.stem == "__init__" else rel.stem
    tests_dir = root / "tests"
    if not stem or not tests_dir.is_dir():
        return set()
    return {p.relative_to(root).as_posix() for p in tests_dir.rglob(f"test_{stem}.py")}


def select(
    root: Path | str,
    changes: Iterable[Change],
    *,
    depth: str = "transitive",
    graph: ImportGraph | None = None,
) -> Selection:
    """The test files ``changes`` can affect.

    * A file in :data:`FULL_SUITE_FILES` asks for the full suite.
    * A changed test file selects itself (when it still exists).
    * A Python file under ``src``, ``tests`` or ``scripts`` selects the tests
      that depend on it, plus ``tests/**/test_<stem>.py`` by name; a
      src change also selects the tests that read the whole source tree. A
      deleted or renamed-away module selects the files still importing it.
    * Markdown documentation selects the tests that name it. The command-line
      front end adds the docs scope of ``scripts/ci_changes.py``.
    * Anything under :data:`NON_PYTHON_TREES` selects the Python files that name
      its path (a conformance fixture, say), and nothing else.
    * Any other file selects the tests that name its path; when none does, the
      change cannot be mapped and asks for the full suite.

    * A deleted Python file whose import name cannot be rebuilt asks for the
      full suite.
    * A test, helper or script with an import the graph cannot read (a computed
      ``import_module`` name, ``python -c`` code built at run time) is selected
      for every Python change; a src module with one is reported in
      ``caveats`` for every src change, since its targets are unknown.

    :data:`ALWAYS_RUN` files present in the tree are returned as ``inventory``.
    Whatever the depth, the result is :data:`SUBSET_NOTE`.
    """
    root = Path(root)
    graph = graph or ImportGraph(root)
    seeds: set[str] = set()
    tests: set[str] = set()
    scanned: set[str] = set()
    full: list[str] = []
    docs_changed = False
    python_changed = src_changed = False
    for change in changes:
        paths = [(change.path, change.deleted)]
        if change.renamed and change.old_path:
            paths.append((change.old_path, True))
        for path, gone in paths:
            if path in FULL_SUITE_FILES:
                full.append(f"{path}: every test depends on it")
                continue
            if path.startswith(NON_PYTHON_TREES):
                # Their own CI jobs cover them; a Python file that names this one
                # still runs. Directory prefixes do not count here: a module that
                # serves "packages/web/dist" does not read the package's sources.
                seeds |= graph.path_referrers(path, prefixes=False)
                continue
            # By path, not is_file(): a dangling symlink is still a changed file.
            exists = not gone and (path in graph.nodes or _present(root, path))
            is_python = path.endswith(".py") and path.startswith(("src/", "tests/", "scripts/"))
            python_changed |= is_python
            src_changed |= is_python and path.startswith(SRC_DIR + "/")
            if is_python and gone:
                # Deleted or renamed away: whoever still names it is affected.
                missing = graph.importers_of_missing(path)
                if missing is None:
                    full.append(f"{path}: cannot tell what imported it")
                else:
                    seeds |= missing | graph.path_referrers(path)
                if path.startswith("tests/") and path.endswith("/conftest.py"):
                    # Its fixtures are gone from every test under it.
                    seeds.add(path)
            if is_test_file(path):
                if exists:
                    tests.add(path)
                    seeds.add(path)
                continue
            if is_python:
                if not gone:
                    tests |= _name_mapped(root, path)
                if path.startswith(SRC_DIR + "/"):
                    scanned |= graph.tree_scanners
                if exists and path in graph.nodes:
                    seeds.add(path)
                elif not gone:
                    # Present but not a readable module (a dangling symlink, say):
                    # its importers still name it.
                    seeds |= graph.importers_of_missing(path) or set()
                continue
            if is_markdown_doc(path):
                docs_changed = True
                seeds |= graph.path_referrers(path)
                continue
            referrers = graph.path_referrers(path)
            if referrers:
                seeds |= referrers
            elif not gone:
                full.append(f"{path}: no test or module names it")
    caveats: list[str] = []
    wildcards: set[str] = set()
    for rel, reasons in sorted(graph.unresolved.items()):
        more = f" (+{len(reasons) - 1} more)" if len(reasons) > 1 else ""
        if rel.startswith(SRC_DIR + "/"):
            # Seeding these for every change would select 150 to 350 of ~650
            # files each time (measured, task #2231), so they are reported.
            if src_changed:
                caveats.append(f"{rel} {reasons[0]}{more}: what it loads is not followed")
        elif python_changed:
            # Could load anything: run it, and whatever runs it.
            wildcards.add(rel)
    # The declared component-map edge is excluded here (task #2652): it is real
    # for change reachability, but following it would attribute every unresolved
    # module's taint to every test that imports the components hub.
    unresolved_tests = (
        graph.closure(wildcards, depth=depth, follow_declared=False) & graph.test_files
    )
    reached = graph.closure(seeds, depth=depth)
    for plugin in sorted(reached & graph.global_plugins):
        full.append(f"{plugin}: loaded into every test by -p in pytest addopts")
    tests |= (reached & graph.test_files) | unresolved_tests
    scanner_only = scanned - tests
    tests = {t for t in tests | scanned if _present(root, t)}
    inventory = [t for t in ALWAYS_RUN if (root / t).is_file()]
    return Selection(
        sorted(tests),
        inventory,
        full,
        docs_changed,
        caveats,
        sorted(t for t in unresolved_tests if _present(root, t)),
        sorted(t for t in scanner_only if _present(root, t)),
    )
