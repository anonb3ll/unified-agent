"""Load-robust growth checks for the scanner's linear-cost tests (task #2188).

The redaction and JWK scan tests prove that four times the input costs well
under sixteen times as much, the cost of a quadratic scan. They used to compare
the median of three wall-clock samples per size, and a burst of load on a
shared CI runner landing on the large samples pushed that ratio past the limit
with nothing wrong in the code (8.04 and 9.03 against 8).

``cpu_growth`` keeps the check and drops the noise:

* It measures the calling thread's CPU time (``time.thread_time``), so time the
  scheduler gives to other processes is not counted against the scan.
* It runs one untimed warm-up call, then interleaves the small and large runs,
  so a slow stretch of the machine falls on both sizes rather than on one.
* It keeps the minimum sample for each size. Noise only ever adds time, so the
  minimum is the sample closest to the code's real cost.
* When the ratio is over the limit it takes another round of fresh samples, up
  to ``rounds`` rounds, and keeps the minimum across all of them. A single bad
  stretch cannot fail the test. A quadratic scan stays near 16 in every
  round and still fails; ``tests/test_growth_budget.py`` checks that.

``peak_bytes`` and ``peak_growth`` do the same for ``tracemalloc`` peaks: the
cyclic garbage collector runs before each traced call, and a peak over its
limit is measured again before the test may fail. A scan whose peak really
grows still fails on every measurement.

Every measured callable must be referentially transparent in cost: each call
with the same argument does the same work and allocates the same, with no
first-call-only or cached cost (a lazy import, a pattern compiled on first use,
a memoised result). Retries keep the minimum, so a cost paid only on the first
call is measured once and then discarded by the re-measurement; the helpers
cannot tell it from noise, and a regression hiding in it passes. Warm such
caches before the measured call, or assert the first call on its own
(task #2195). ``cpu_growth``'s untimed warm-up exists for the same reason.

A cache keyed on the last argument skews the ratio the other way. The warm-up
ends on the small input and the first timed call repeats it, so the small
minimum is a cache hit while the alternating large input never hits; with
``loops > 1`` every repeat inside a sample hits too. A re-measured peak on the
same text hits the cache and drops the cached allocation. The redactor has one
such cache (``yaml_jwk._LEX_CACHE``, the last text's YAML lexer), so every
growth or peak test whose callable can reach the JWK scan measures
``cold(run)``, which clears each cache in ``LAST_ARGUMENT_CACHES`` before every
call. A new cache of that kind belongs in that tuple (task #2218).

``cold`` clears each entry by its shape: it calls ``cache_clear()`` (an lru
cache, or a memoising closure that exposes one), drops a ``cached_property``
from its instance, resets plain data (``None``, a tuple, a string, ...) to
``None``, and empties any other object with ``clear()`` in place. It refuses
anything else, such as a closure or an instance with no clearing hook, rather
than guess.

``tests/test_growth_budget.py`` fails on module-level state that a function
writes and that is neither registered nor in ``NOT_LAST_ARGUMENT_CACHES``.
``growth_cache_scan`` decides which modules to scan: every first-party module
that a growth or peak test's measured callable reaches by name (the test's own
functions, helper modules beside the tests, imports and the module-level
functions and classes those name), plus the whole ``unified_agent.telemetry``
package. ``module_state_writes`` then reads each module's AST. It counts:

* a ``global`` that a function rebinds;
* an item or attribute stored, augmented or deleted, a mutating method
  (``clear``, ``update``, ``setdefault``, ``append``, ``move_to_end``, ...) or a
  mutating function (``setattr``, ``heappush``, ...) applied to module-level
  state. That state may be reached by name, through a local alias, an imported
  module, ``globals()``, ``vars()``, ``sys.modules[__name__]`` or
  ``getattr(obj, "name")``, or through a module-level helper that writes its
  parameter;
* ``functools.lru_cache``/``cache`` on a module-level function or method, also
  under an import alias;
* a closure or cache made by a call at import time (``f = memoize(f)``,
  ``@memoize``) whose factory writes a free variable or builds an lru cache;
* class state written through ``cls``, ``type(self)``, ``self.__class__`` or an
  instance that has no attribute of its own by that name; and the attributes
  (``cached_property`` included) that the methods of a module-level instance
  write after ``__init__``.

State made per call is not counted: locals, parameters, nested caches and
classes, closures over a call's locals, and work done at import time. A write
the scan cannot pin to one attribute (``globals()[name]``) is reported as
``DYNAMIC``, and it can only be allow-listed.

Deliberate limits. The scan does not follow a factory, decorator or class
defined in another module (``_x = other.memoize(f)``, ``_POOL = other.Pool()``),
a factory reached through a method (``K.make()``), a mutating helper called as
a method, state passed into a container and mutated later, aliases made by
tuple unpacking or a ``for`` loop, or writes through ``importlib``, ``exec`` or a
non-constant ``getattr``. The reach does not follow a callable that is only
dispatched dynamically: an attribute of an object whose class no followed code
names, a callback registered elsewhere, or a star import. A measured callable
that is a test parameter (a fixture or a ``parametrize`` value) cannot be
followed at all, so the guard fails on it. Measure a named function instead.
Ruff's B006 already refuses a mutable default argument.
"""

from __future__ import annotations

import ast
import functools
import gc
import inspect
import math
import sys
import time
import tracemalloc
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: A timed sample shorter than this repeats the call, so timer resolution
#: cannot decide a ratio for inputs that scan in well under a millisecond.
MIN_SAMPLE_SECONDS = 0.002
_MAX_LOOPS = 4096

#: Caches keyed on the last argument, as (module, dotted attribute). ``cold``
#: clears each before every call; see the module docstring.
LAST_ARGUMENT_CACHES: tuple[tuple[str, str], ...] = (
    ("unified_agent.telemetry.yaml_jwk", "_LEX_CACHE"),
)

#: Module-level state the growth tests reach that is not a cross-call cache, as
#: (module, dotted attribute) -> why ``cold`` need not clear it.
NOT_LAST_ARGUMENT_CACHES: dict[tuple[str, str], str] = {
    ("unified_agent.tools.worktree_policy", "_last_unknown_cause"): (
        "the reason the last liveness scan could not judge processes, kept for the "
        "cleanup report; no scan reads it to skip work"
    ),
    ("unified_agent.tools.worktree_policy", "_survey_cache"): (
        "the shared worktree survey behind cached_survey_worktrees(); the measured "
        "_ps_process_paths() neither reads nor fills it"
    ),
    ("unified_agent.tools.worktree_policy", "_SCAN_RESOLVER"): (
        "a ContextVar holding a fresh _Resolver for the duration of one decorated "
        "scan, reset afterwards; nothing it holds survives a call"
    ),
}

#: A write the scan cannot pin to one attribute (``globals()[name] = ...``,
#: ``setattr(obj, name, ...)``). It can only be allow-listed, with a reason.
DYNAMIC = "<dynamic>"

#: Values ``cold`` resets to ``None``: the cold value of a rebound cache.
_PLAIN_DATA = (type(None), bool, int, float, str, bytes, tuple, frozenset)


def _resolve(name: str, dotted: str) -> tuple[Any, str]:
    """The object holding ``dotted`` in module ``name``, and the final attribute."""
    # Static imports, deliberately inside the function: they cost nothing until a
    # growth test first clears a cache, and they give the import graph in
    # ``unified_agent.affected_tests`` the edges this helper really has, instead
    # of one computed ``import_module(name)`` that left every test reaching this
    # file unownable (task #2458). A table entry may name any module a test has
    # already put in ``sys.modules``, so ``_resolve`` still accepts the
    # monkeypatched tables ``tests/test_growth_budget.py`` builds.
    import unified_agent.audit.rules  # noqa: F401
    import unified_agent.telemetry.yaml_jwk  # noqa: F401
    import unified_agent.tools.worktree_policy  # noqa: F401

    owner: Any = sys.modules.get(name)
    if owner is None:
        raise AttributeError(
            f"{name} is not imported by tests/growth_budget.py; add a static import "
            "for it above, or register the module in sys.modules (LAST_ARGUMENT_CACHES)"
        )
    *parents, attr = dotted.split(".")
    for part in parents:
        owner = getattr(owner, part, None)
    # A renamed cache must fail here, not be shadowed by a new attribute.
    missing = object()
    if owner is None or inspect.getattr_static(owner, attr, missing) is missing:
        raise AttributeError(f"{name}.{dotted} is gone; update LAST_ARGUMENT_CACHES")
    return owner, attr


def _clear(owner: Any, attr: str, label: str) -> None:
    if not isinstance(owner, (type, types.ModuleType)) and isinstance(
        inspect.getattr_static(type(owner), attr, None), functools.cached_property
    ):
        vars(owner).pop(attr, None)  # Forget it; the next read computes it afresh.
        return
    value = getattr(owner, attr)
    if callable(getattr(value, "cache_clear", None)):
        value.cache_clear()  # functools.lru_cache / functools.cache, or a closure's hook
    elif isinstance(value, _PLAIN_DATA):
        setattr(owner, attr, None)  # A rebound global such as ``_LEX_CACHE``.
    elif callable(getattr(value, "clear", None)) and not isinstance(value, type):
        # Emptied in place: the code may hold it by reference or call .clear() on it.
        value.clear()
    else:
        raise TypeError(
            f"cold() cannot clear {label} ({type(value).__name__}): give it a "
            "cache_clear() (a memoising closure can expose its dict's clear), keep "
            "the cached value in a registered container, or rebind it with None as "
            "the cold value"
        )


def cold(run: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """``run`` with every cache in ``LAST_ARGUMENT_CACHES`` cleared before each call."""
    caches = [
        (*_resolve(name, dotted), f"{name}.{dotted}") for name, dotted in LAST_ARGUMENT_CACHES
    ]
    for owner, attr, label in caches:
        _clear(owner, attr, label)  # An unsupported shape fails here, before any timing.

    @functools.wraps(run)
    def call(arg: Any) -> Any:
        for owner, attr, label in caches:
            _clear(owner, attr, label)
        return run(arg)

    return call


# --- finding module-level caches in source (the guard in test_growth_budget) ---------

#: Methods that change the object they are called on.
_MUTATING_METHODS = frozenset(
    """
    __delitem__ __iadd__ __ior__ __setitem__ add append appendleft clear
    difference_update discard extend extendleft insert intersection_update move_to_end
    pop popitem popleft remove reverse rotate setdefault sort
    symmetric_difference_update update write
    """.split()
)
#: Functions that change the object passed as their first argument.
_MUTATING_FUNCTIONS = frozenset(
    """
    delattr delitem heapify heappop heappush heappushpop heapreplace insort insort_left
    insort_right setattr setitem
    """.split()
)
#: Methods returning an object held inside the container they are called on.
_ELEMENT_METHODS = frozenset({"get", "setdefault"})
_CACHE_FACTORIES = frozenset({"cache", "lru_cache"})
_INIT_METHODS = frozenset({"__init__", "__new__", "__post_init__", "__init_subclass__"})
_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

# An address says where an expression's object lives, as a tuple:
#   ("obj", parts)              module-level object at dotted ``parts`` (() is the module)
#   ("in", parts)               an object held inside the one at ``parts``
#   ("dict", parts)             the ``__dict__`` of the one at ``parts``
#   ("self", cls, scope)        the instance in a method of module-level class ``cls``
#   ("attr", cls, name, inside, scope)  ``self.name``, or (inside) an object held in it
#   ("param", scope, name)      a parameter of the function at ``scope``
#   ("closure", scope)          a local of an enclosing function at ``scope``
# None means per-call state: a local, or the result of an ordinary call.
_Address = tuple


def _target_names(node: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


def _parameters(args: ast.arguments) -> list[str]:
    every = [*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg]
    return [arg.arg for arg in every if arg is not None]


def _is_reference(node: ast.expr | None) -> bool:
    """An expression whose value can be module state that the caller then mutates."""
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return True
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in {"getattr", "globals", "vars", "type"}
    return isinstance(func, ast.Attribute) and func.attr in _ELEMENT_METHODS


def _end(node: ast.AST) -> tuple[int, int]:
    return (node.end_lineno or 0, node.end_col_offset or 0)


def _start(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


class _Scope:
    """One function, lambda or comprehension being scanned."""

    def __init__(self, node: ast.AST, special: dict[str, _Address], init: bool = False) -> None:
        self.function = isinstance(node, _FUNCTION_SCOPES)
        self.special = special  # ``self`` and ``cls`` in a method
        self.init = init
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()
        self.locals: set[str] = set()
        self.imports: set[str] = set()
        self.params: list[str] = []
        self.param_writes: set[str] = set()
        # name -> [(position, block depth, aliased expression or None for a rebinding)]
        self.events: dict[str, list[tuple[tuple[int, int], int, ast.expr | None]]] = {}
        if isinstance(node, _COMPREHENSIONS):
            for generator in node.generators:
                self.locals.update(_target_names(generator.target))
            return
        self.params = _parameters(node.args)
        self.locals.update(self.params)
        if isinstance(node, ast.Lambda):
            return
        for child in self._own(node):
            if isinstance(child, ast.Global):
                self.globals.update(child.names)
            elif isinstance(child, ast.Nonlocal):
                self.nonlocals.update(child.names)
            elif isinstance(child, ast.Name) and not isinstance(child.ctx, ast.Load):
                self.locals.add(child.id)
            elif isinstance(child, _DEFINITIONS):
                self.locals.add(child.name)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                self.imports.update((a.asname or a.name).split(".")[0] for a in child.names)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                self.locals.add(child.name)
        self.locals -= self.globals | self.nonlocals | self.imports
        self._record_events(node.body, 0)

    @staticmethod
    def _own(scope: ast.AST):
        """Nodes in ``scope``'s own body, not inside a nested scope or comprehension."""
        pending = list(ast.iter_child_nodes(scope))
        while pending:
            node = pending.pop()
            yield node
            if not isinstance(node, (*_DEFINITIONS, ast.Lambda, *_COMPREHENSIONS)):
                pending.extend(ast.iter_child_nodes(node))

    def _event(self, name: str, node: ast.AST, depth: int, value: ast.expr | None) -> None:
        self.events.setdefault(name, []).append((_end(node), depth, value))

    def _record_events(self, statements: list[ast.stmt], depth: int) -> None:
        # ``cache = _CACHE`` makes ``cache.clear()`` a write to ``_CACHE`` until a
        # rebinding that every later statement passes through (one at depth 0).
        for stmt in statements:
            if isinstance(stmt, _DEFINITIONS):
                self._event(stmt.name, stmt, depth, None)
                continue
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    alias = stmt.value if isinstance(target, ast.Name) else None
                    for name in _target_names(target):
                        self._event(name, stmt, depth, alias if _is_reference(alias) else None)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                for name in _target_names(stmt.target):
                    self._event(name, stmt.target, depth + 1, None)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    if item.optional_vars is not None:
                        for name in _target_names(item.optional_vars):
                            self._event(name, stmt, depth, None)
            for field in ("body", "orelse", "finalbody"):
                self._record_events(getattr(stmt, field, []), depth + 1)
            for handler in getattr(stmt, "handlers", []):
                self._record_events(handler.body, depth + 1)
            for case in getattr(stmt, "cases", []):
                self._record_events(case.body, depth + 1)

    def alias(self, name: str, at: tuple[int, int] | None) -> ast.expr | None:
        """The expression ``name`` aliases at position ``at`` (None: anywhere)."""
        events = self.events.get(name, [])
        if at is None:
            aliases = [value for _, _, value in events if value is not None]
            return aliases[-1] if aliases else None
        for position, depth, value in reversed(events):
            if position > at:
                continue
            if value is not None:
                return value
            if depth == 0:
                return None
        # Nothing decides it before ``at``: a later alias may reach it round a loop.
        later = [value for position, _, value in events if position > at and value is not None]
        return later[-1] if later else None


class _ClassInfo:
    def __init__(self) -> None:
        self.attributes: set[str] = set()  # bound in the class body
        self.cached: set[str] = set()  # functools.cached_property
        self.assigned: set[str] = set()  # ``self.name = ...`` anywhere
        self.mutated: set[str] = set()  # contents of ``self.name`` changed
        self.rewritten: set[str] = set()  # either, outside __init__ and friends


class _StateWrites(ast.NodeVisitor):
    def __init__(self, tree: ast.Module, factories: set[str], mutators: dict) -> None:
        self.found: set[str] = set()
        self.factories = factories  # module functions whose result keeps state
        self.mutators = mutators  # module function -> (parameters, the ones it writes)
        self.qualified = self._imports(tree)  # local name -> what it imports
        self.classes: dict[str, _ClassInfo] = {}
        self.instances: list[tuple[str, str]] = []  # (module-level name, class)
        self._scopes: list[_Scope] = []
        self._names: list[str] = []  # qualified-name prefix: classes and functions
        self._top: str | None = None  # the module-level function being scanned
        self._class: str | None = None  # module-level class whose body is being scanned

    @staticmethod
    def _imports(tree: ast.Module) -> dict[str, str]:
        qualified = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    qualified[local] = alias.name if alias.asname else local
            elif isinstance(node, ast.ImportFrom):
                prefix = "." * node.level + (f"{node.module}." if node.module else "")
                for alias in node.names:
                    qualified[alias.asname or alias.name] = prefix + alias.name
        return qualified

    def _dotted(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.qualified.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._dotted(node.value)
            return None if base is None else f"{base}.{node.attr}"
        return None

    def _is_named(self, node: ast.expr, names: frozenset[str]) -> bool:
        """``node`` is functools' ``names`` (or a bare one), called or not."""
        dotted = self._dotted(node.func if isinstance(node, ast.Call) else node)
        if dotted is None:
            return False
        module, _, name = dotted.rpartition(".")
        return name in names and module in {"", "functools"}

    def _is_factory_call(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func.func if isinstance(node.func, ast.Call) else node.func
        named = isinstance(func, ast.Name) and func.id in self.factories
        return named or self._is_named(node.func, _CACHE_FACTORIES)

    def _in_function(self) -> bool:
        return any(scope.function for scope in self._scopes)

    def _mark_factory(self) -> None:
        if self._top:
            self.factories.add(self._top)

    # Where an expression's object lives.

    def _root(self, name: str, depth: int, at: tuple[int, int]) -> _Address | None:
        crossed = False  # a function boundary between the use and the binding
        for index in range(depth - 1, -1, -1):
            scope = self._scopes[index]
            if name in scope.globals:
                return ("obj", (name,))
            if name in scope.special:
                return scope.special[name]
            alias = scope.alias(name, None if crossed else at)
            if alias is not None:
                return self._address(alias, index + 1)
            if name in scope.imports:
                return ("obj", (name,))
            if name in scope.locals:
                if crossed and scope.function:
                    return ("closure", index)
                if name in scope.params and scope.function:
                    return ("param", index, name)
                return None
            crossed = crossed or scope.function
        return ("obj", (name,))

    def _address(self, node: ast.expr, depth: int | None = None) -> _Address | None:
        depth = len(self._scopes) if depth is None else depth
        if isinstance(node, ast.Name):
            return self._root(node.id, depth, _start(node))
        if isinstance(node, ast.Attribute):
            return self._member(self._address(node.value, depth), node.attr)
        if isinstance(node, ast.Subscript):
            return self._item(self._address(node.value, depth), node.slice)
        if isinstance(node, ast.Call):
            return self._call_result(node, depth)
        return None

    @staticmethod
    def _member(base: _Address | None, attr: str | None) -> _Address | None:
        if base is None:
            return None
        kind = base[0]
        if kind == "obj":
            if attr is None:
                return ("in", base[1])
            return ("dict", base[1]) if attr == "__dict__" else ("obj", (*base[1], attr))
        if kind == "self":
            if attr == "__class__":
                return ("obj", tuple(base[1].split(".")))
            return ("attr", base[1], attr, False, base[2]) if attr else base
        if kind == "attr":
            return ("attr", base[1], base[2], True, base[4])
        return ("in", base[1]) if kind in ("in", "dict") else base

    @staticmethod
    def _constant(node: ast.expr | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def _item(self, base: _Address | None, key: ast.expr) -> _Address | None:
        if base is None:
            return None
        if base[0] == "dict":
            name = self._constant(key)
            return ("obj", (*base[1], name)) if name else ("in", base[1])
        if base == ("obj", ("sys", "modules")):
            if isinstance(key, ast.Name) and key.id == "__name__":
                return ("obj", ())  # this module
            return ("in", (DYNAMIC,))
        return self._member(base, None)

    def _call_result(self, node: ast.Call, depth: int) -> _Address | None:
        func, args = node.func, node.args
        if isinstance(func, ast.Attribute) and func.attr in _ELEMENT_METHODS:
            return self._member(self._address(func.value, depth), None)
        if not isinstance(func, ast.Name) or self._root(func.id, depth, _start(func)) != (
            "obj",
            (func.id,),
        ):
            return None  # An ordinary call returns new, per-call state.
        if func.id == "globals" and not args:
            return ("dict", ())
        if func.id == "vars":
            if args:
                base = self._address(args[0], depth)
                return ("dict", base[1]) if base and base[0] == "obj" else self._member(base, None)
            return None if self._in_function() else ("dict", ())
        if func.id == "getattr" and len(args) >= 2:
            return self._member(self._address(args[0], depth), self._constant(args[1]))
        if func.id == "type" and len(args) == 1:
            base = self._address(args[0], depth)
            return self._member(base, "__class__") if base and base[0] == "self" else None
        return None

    # Writes.

    def _record(self, address: _Address | None) -> None:
        if address is None or not self._in_function():
            return  # Import-time set-up is not a cross-call cache.
        kind = address[0]
        if kind in ("obj", "in", "dict"):
            self.found.add(".".join(address[1]) or DYNAMIC)
        elif kind == "param":
            self._scopes[address[1]].param_writes.add(address[2])
        elif kind == "closure":
            self._mark_factory()
        elif kind == "attr":
            _, cls, name, inside, scope = address
            info = self.classes[cls]
            (info.mutated if inside else info.assigned).add(name)
            if not self._scopes[scope].init:
                info.rewritten.add(name)

    def _write(self, target: ast.expr | None) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._write(element)
        elif isinstance(target, ast.Starred):
            self._write(target.value)
        elif isinstance(target, (ast.Subscript, ast.Attribute)):
            # ``_X[k] = v`` changes ``_X``; ``globals()["_X"] = v`` and ``m._X = v`` rebind it.
            self._record(self._address(target))

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) or not self._scopes:
            return
        scope = self._scopes[-1]
        if node.id in scope.globals and self._in_function():
            self.found.add(node.id)  # A rebound global.
        elif node.id in scope.nonlocals:
            self._mark_factory()  # A closure rebinding its enclosing function's local.

    def visit_Assign(self, node: ast.Assign) -> None:
        self._import_time_binding(node.targets, node.value)
        for target in node.targets:
            self._write(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._import_time_binding([node.target], node.value)
        self._write(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._write(node.target)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._write(target)
        self.generic_visit(node)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self._write(node.target)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_withitem(self, node: ast.withitem) -> None:
        self._write(node.optional_vars)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if self._in_function():
            if self._is_factory_call(node):
                self._mark_factory()  # It returns a cache or a stateful closure.
            if isinstance(func, ast.Attribute) and func.attr in _MUTATING_METHODS:
                self._record(self._member(self._address(func.value), None))
            elif isinstance(func, ast.Name) and func.id in _MUTATING_FUNCTIONS:
                self._mutating_function(func.id, node.args)
            elif isinstance(func, ast.Attribute) and func.attr in _MUTATING_FUNCTIONS:
                self._mutating_function(func.attr, node.args)
            elif isinstance(func, ast.Name) and func.id in self.mutators:
                self._helper_call(func.id, node)
        self.generic_visit(node)

    def _mutating_function(self, name: str, args: list[ast.expr]) -> None:
        if not args:
            return
        address = self._address(args[0])
        if name in {"setattr", "delattr"} and len(args) > 1:
            self._record(self._member(address, self._constant(args[1])))
        else:
            self._record(self._member(address, None))  # Its contents change.

    def _helper_call(self, name: str, node: ast.Call) -> None:
        if self._root(name, len(self._scopes), _start(node)) != ("obj", (name,)):
            return
        params, written = self.mutators[name]
        for param, arg in zip(params, node.args, strict=False):
            if param in written:
                self._record(self._address(arg))
        for keyword in node.keywords:
            if keyword.arg in written:
                self._record(self._address(keyword.value))

    # Import-time bindings: a closure or cache made once and kept.

    def _qualname(self, name: str) -> str:
        return ".".join([*self._names, name])

    def _import_time_binding(self, targets: list[ast.expr], value: ast.expr) -> None:
        if self._in_function() or self._scopes:
            return
        names = [self._qualname(t.id) for t in targets if isinstance(t, ast.Name)]
        if any(self._is_factory_call(n) for n in ast.walk(value)):
            self.found.update(names)
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            cls = value.func.id
            if not self._names and cls in self.classes:
                self.instances.extend((name, cls) for name in names)

    # Scopes.

    def _enter(self, scope: _Scope, body: list[ast.AST]) -> None:
        self._scopes.append(scope)
        for child in body:
            self.visit(child)
        self._scopes.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for child in [*node.decorator_list, node.args, *([node.returns] if node.returns else [])]:
            self.visit(child)
        decorated = [
            self._dotted(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list
        ]
        caching = any(self._is_named(d, _CACHE_FACTORIES) for d in node.decorator_list) or any(
            isinstance(d.func if isinstance(d, ast.Call) else d, ast.Name)
            and (d.func if isinstance(d, ast.Call) else d).id in self.factories
            for d in node.decorator_list
        )
        if caching:
            if self._in_function():
                self._mark_factory()  # Made per call, kept only if the call's result is.
            else:
                self.found.add(self._qualname(node.name))
        special: dict[str, _Address] = {}
        params = _parameters(node.args)
        method_of = self._class if not self._scopes else None
        if method_of and params and "staticmethod" not in decorated:
            if "classmethod" in decorated:
                special[params[0]] = ("obj", tuple(method_of.split(".")))
            else:
                special[params[0]] = ("self", method_of, len(self._scopes))
            if any(self._is_named(d, frozenset({"cached_property"})) for d in node.decorator_list):
                self.classes[method_of].cached.add(node.name)
        top = self._top
        if not self._scopes and not self._names:
            self._top = node.name
        self._names.append(f"{node.name}.<locals>")
        scope = _Scope(node, special, init=bool(method_of) and node.name in _INIT_METHODS)
        self._enter(scope, node.body)
        self._names.pop()
        self._top = top
        if not self._scopes and not self._names and scope.param_writes:
            self.mutators[node.name] = (scope.params, frozenset(scope.param_writes))

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)
        self._enter(_Scope(node, {}), [node.body])

    def _visit_comprehension(self, node: ast.AST) -> None:
        parts = [getattr(node, field, None) for field in ("elt", "key", "value")]
        self._enter(_Scope(node, {}), [*node.generators, *(p for p in parts if p is not None)])

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comprehension

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for child in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(child)
        outer = self._class
        local = bool(self._scopes) or (self._names and self._class is None)
        qualname = self._qualname(node.name)
        self._class = None if local else qualname
        if not local:
            info = self.classes.setdefault(qualname, _ClassInfo())
            for statement in node.body:
                if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value:
                    value = statement.value
                    if isinstance(value, ast.Call) and self._dotted(value.func) in {
                        "field",
                        "dataclasses.field",
                    }:
                        continue  # A dataclass field: each instance gets its own.
                    targets = getattr(statement, "targets", None) or [statement.target]
                    info.attributes.update(t.id for t in targets if isinstance(t, ast.Name))
        self._names.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self._names.pop()
        self._class = outer

    def finish(self) -> set[str]:
        for cls, info in self.classes.items():
            # Class state shared by every instance, changed through one of them.
            shared = (info.mutated & info.attributes) - info.assigned
            self.found.update(f"{cls}.{name}" for name in shared)
        for name, cls in self.instances:
            info = self.classes[cls]
            self.found.update(f"{name}.{attr}" for attr in info.cached | info.rewritten)
        return self.found


def _scan(source: str) -> _StateWrites:
    tree = _parse(source)
    factories: set[str] = set()
    mutators: dict[str, tuple[list[str], frozenset[str]]] = {}
    while True:  # Until no new factory or mutating helper turns up: both only grow.
        known = (set(factories), dict(mutators))
        scan = _StateWrites(tree, factories, mutators)
        scan.visit(tree)
        scan.finish()
        if (factories, mutators) == known:
            return scan


@functools.lru_cache(maxsize=64)
def _parse(source: str) -> ast.Module:
    """A parsed source, shared: nothing here mutates a tree."""
    return ast.parse(source)


@functools.lru_cache(maxsize=64)
def _scanned(source: str) -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
    """What ``_scan`` finds, and the import map; a pure function of the source text."""
    scan = _scan(source)
    return frozenset(scan.found), tuple(scan.qualified.items())


def module_state_writes(source: str) -> set[str]:
    """Dotted names of module-level state that the functions in ``source`` write.

    Conservative; the module docstring lists what counts and the limits.
    """
    return set(_scanned(source)[0])


def module_state_findings(
    module: str, source: str, *, package: bool = False, is_module: Callable[[str], bool]
) -> set[tuple[str, str]]:
    """``module_state_writes`` as (module, attribute), each in the module that owns it.

    A write through an imported module (``yaml_jwk._LEX_CACHE = x`` in ``jwk``)
    is reported against that module, where ``LAST_ARGUMENT_CACHES`` names it.
    """
    found, imports = _scanned(source)
    qualified = dict(imports)
    findings = set()
    for path in found:
        root, _, rest = path.partition(".")
        target = qualified.get(root)
        if target and rest:
            parts = _absolute(module, package, target).split(".") + rest.split(".")
            for cut in range(len(parts) - 1, 0, -1):
                owner = ".".join(parts[:cut])
                if is_module(owner):
                    findings.add((owner, ".".join(parts[cut:])))
                    break
            else:
                findings.add((module, path))
        else:
            findings.add((module, path))
    return findings


def _absolute(module: str, package: bool, name: str) -> str:
    """``name`` from an import in ``module``, with any leading dots resolved."""
    level = len(name) - len(name.lstrip("."))
    if not level:
        return name
    parts = module.split(".") if package else module.split(".")[:-1]
    base = parts[: len(parts) - (level - 1)]
    return ".".join([*base, name[level:]] if name[level:] else base)


# --- which modules the growth tests reach --------------------------------------------

_GROWTH_HELPERS = frozenset({"assert_cpu_growth", "cpu_growth", "peak_bytes", "peak_growth"})
#: Scanned in full whatever the growth tests measure today.
CACHE_SCAN_FLOOR = ("unified_agent.telemetry",)
_FIRST_PARTY = "unified_agent"

_Binding = tuple  # ("import", dotted) | ("node", def) | ("expr", value) | ("param",) | ("local",)


@dataclass
class _Unit:
    name: str
    path: Path
    package: bool
    first_party: bool
    tree: ast.Module
    bindings: dict[str, _Binding]


def _bind_import(unit_name: str, package: bool, node: ast.AST, into: dict) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            into[local] = ("import", alias.name if alias.asname else local)
    elif isinstance(node, ast.ImportFrom):
        base = _absolute(unit_name, package, "." * node.level + (node.module or ""))
        for alias in node.names:
            if alias.name != "*":
                into[alias.asname or alias.name] = ("import", f"{base}.{alias.name}".lstrip("."))


def _module_bindings(unit_name: str, package: bool, tree: ast.Module) -> dict[str, _Binding]:
    bindings: dict[str, _Binding] = {}
    pending = list(tree.body)
    while pending:
        stmt = pending.pop(0)
        if isinstance(stmt, _DEFINITIONS):
            bindings[stmt.name] = ("node", stmt)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            _bind_import(unit_name, package, stmt, bindings)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                for name in _target_names(target):
                    bindings[name] = ("expr", stmt.value)
        else:
            for field in ("body", "orelse", "finalbody"):
                pending.extend(getattr(stmt, field, []))
            for handler in getattr(stmt, "handlers", []):
                pending.extend(handler.body)
    return bindings


def _context(unit: _Unit, node: ast.AST, params: str) -> dict[str, _Binding]:
    """Names bound anywhere inside ``node``; its own parameters are ``params``."""
    context: dict[str, _Binding] = {}
    for child in ast.walk(node):
        if child is not node and isinstance(child, _DEFINITIONS):
            context[child.name] = ("node", child)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            _bind_import(unit.name, unit.package, child, context)
        elif isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value is not None:
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                for name in _target_names(target):
                    context[name] = ("expr", child.value)
        elif child is not node and isinstance(child, ast.arguments):
            for name in _parameters(child):
                context.setdefault(name, ("local",))
    if isinstance(node, _FUNCTION_SCOPES):
        for name in _parameters(node.args):
            context[name] = (params,)
    return context


class _Reach:
    """First-party modules whose code a measured callable can run, followed by name."""

    def __init__(self, tests_root: Path, src_root: Path) -> None:
        self.tests_root, self.src_root = tests_root, src_root
        self.modules: set[str] = set()
        self.unresolved: list[str] = []
        self._units: dict[str, _Unit | None] = {}
        self._done: set[tuple[str, int]] = set()

    def unit(self, name: str) -> _Unit | None:
        if name not in self._units:
            self._units[name] = self._load(name)
        return self._units[name]

    def _load(self, name: str) -> _Unit | None:
        if name == _FIRST_PARTY or name.startswith(_FIRST_PARTY + "."):
            base = self.src_root.joinpath(*name.split("."))
            candidates = [(base / "__init__.py", True), (base.with_suffix(".py"), False)]
            first_party = True
        else:  # A helper module beside the tests, such as growth_budget.
            candidates = [(self.tests_root / f"{name}.py", False)] if "." not in name else []
            first_party = False
        for path, package in candidates:
            if path.is_file():
                tree = _parse(path.read_text(encoding="utf-8"))
                bindings = _module_bindings(name, package, tree)
                return _Unit(name, path, package, first_party, tree, bindings)
        return None

    def test_unit(self, path: Path, text: str) -> _Unit:
        tree = _parse(text)
        return _Unit(path.stem, path, False, False, tree, _module_bindings(path.stem, False, tree))

    def follow_dotted(self, dotted: str, where: str) -> None:
        parts = dotted.split(".")
        for cut in range(len(parts), 0, -1):
            unit = self.unit(".".join(parts[:cut]))
            if unit is not None:
                break
        else:
            return  # Not first-party code, nor a test helper module.
        if unit.first_party:
            self.modules.add(unit.name)
        rest = parts[cut:]
        if rest:
            self._binding(unit, unit.bindings.get(rest[0]), None, rest[1:], where)

    def _binding(self, unit, binding, context, attrs, where) -> None:
        if binding is None:
            return
        kind = binding[0]
        if kind == "import":
            self.follow_dotted(".".join([binding[1], *attrs]), where)
        elif kind in ("node", "expr"):
            self.follow(unit, binding[1], context, where)
        elif kind == "param":
            self.unresolved.append(where)

    def _name(self, unit, context, name, attrs, where) -> None:
        if name in context:
            self._binding(unit, context[name], context, attrs, where)
        elif name in unit.bindings:
            self._binding(unit, unit.bindings[name], None, attrs, where)

    def follow(self, unit: _Unit, node: ast.AST, context: dict | None, where: str) -> None:
        key = (unit.name, id(node))
        if key in self._done:
            return
        self._done.add(key)
        if unit.first_party:
            self.modules.add(unit.name)
        if context is None:
            context = _context(unit, node, "local") if isinstance(node, _DEFINITIONS) else {}
        skip: set[int] = set()
        for child in ast.walk(node):
            if id(child) in skip:
                continue
            if isinstance(child, ast.Attribute):
                chain, base = [], child
                while isinstance(base, ast.Attribute):
                    chain.append(base)
                    base = base.value
                if isinstance(base, ast.Name):
                    skip.update(id(link) for link in [*chain, base])
                    attrs = [link.attr for link in reversed(chain)]
                    self._name(unit, context, base.id, attrs, where)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                self._name(unit, context, child.id, [], where)


@dataclass(frozen=True)
class CacheScan:
    modules: frozenset[str]  # first-party modules scanned
    findings: frozenset[tuple[str, str]]  # (module, attribute) written module state
    unresolved: tuple[str, ...]  # measured callables the reach could not follow


def _growth_helper(unit: _Unit, context: dict, func: ast.AST) -> str | None:
    """The growth helper ``func`` calls: by its own name, as an attribute
    (``growth_budget.cpu_growth``), or through an import alias
    (``from growth_budget import cpu_growth as measure``)."""
    if isinstance(func, ast.Attribute):
        return func.attr if func.attr in _GROWTH_HELPERS else None
    if not isinstance(func, ast.Name):
        return None
    if func.id in _GROWTH_HELPERS:
        return func.id
    binding = context.get(func.id) or unit.bindings.get(func.id)
    if binding is not None and binding[0] == "import":
        module, _, attr = binding[1].rpartition(".")
        if module.rpartition(".")[2] == "growth_budget" and attr in _GROWTH_HELPERS:
            return attr
    return None


def growth_cache_scan(tests_root: Path, src_root: Path) -> CacheScan:
    """Scan every module a growth or peak test's measured callable can reach.

    Each call to a growth helper in ``tests_root`` is followed by name from its
    first argument: through the test's own functions and assignments, helper
    modules beside the tests, and first-party imports, into every module-level
    function and class those reach. The ``CACHE_SCAN_FLOOR`` packages are
    scanned in full on top. A callable that is a test parameter cannot be
    followed and is reported in ``unresolved``.
    """
    reach = _Reach(tests_root, src_root)
    for path in sorted(tests_root.rglob("test*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(helper in text for helper in _GROWTH_HELPERS):
            continue
        unit = reach.test_unit(path, text)
        for top in unit.tree.body:
            context = _context(unit, top, "param") if isinstance(top, _DEFINITIONS) else {}
            for node in ast.walk(top):
                if not isinstance(node, ast.Call):
                    continue
                name = _growth_helper(unit, context, node.func)
                measured = node.args[0] if node.args else None
                measured = measured or next(
                    (k.value for k in node.keywords if k.arg == "run"), None
                )
                if name is not None and measured is not None:
                    where = f"{path.name}:{node.lineno} measures {ast.unparse(measured)}"
                    reach.follow(unit, measured, context, where)
    for package in CACHE_SCAN_FLOOR:
        root = src_root.joinpath(*package.split("."))
        for path in root.rglob("*.py"):
            parts = path.relative_to(src_root).with_suffix("").parts
            reach.modules.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    findings: set[tuple[str, str]] = set()

    def is_module(name: str) -> bool:
        unit = reach.unit(name)
        return unit is not None and unit.first_party

    for module in sorted(reach.modules):
        unit = reach.unit(module)
        if unit is not None:
            source = unit.path.read_text(encoding="utf-8")
            findings |= module_state_findings(
                module, source, package=unit.package, is_module=is_module
            )
    return CacheScan(frozenset(reach.modules), frozenset(findings), tuple(reach.unresolved))


@dataclass(frozen=True)
class Growth:
    small: float
    large: float
    ratio: float
    rounds: tuple[float, ...]
    loops: int


def _cpu_seconds(run: Callable[[Any], object], arg: Any, loops: int) -> float:
    started = time.thread_time()
    for _ in range(loops):
        run(arg)
    return time.thread_time() - started


def cpu_growth(
    run: Callable[[Any], object],
    small: Any,
    large: Any,
    *,
    limit: float = 8.0,
    samples: int = 3,
    rounds: int = 3,
) -> Growth:
    """Per-call CPU cost of ``run(large)`` over ``run(small)``, settled against noise."""
    gc.collect()
    # The warm-up pays for lazy compiles and caches before anything is timed
    # and picks a loop count that makes one sample long enough to measure.
    loops = 1
    while _cpu_seconds(run, small, loops) < MIN_SAMPLE_SECONDS and loops < _MAX_LOOPS:
        loops *= 4
    best_small = best_large = math.inf
    history: list[float] = []
    for _ in range(rounds):
        for _ in range(samples):
            best_small = min(best_small, _cpu_seconds(run, small, loops) / loops)
            best_large = min(best_large, _cpu_seconds(run, large, loops) / loops)
        history.append(best_large / max(best_small, 1e-9))
        if history[-1] < limit:
            break
    return Growth(best_small, best_large, history[-1], tuple(history), loops)


def assert_cpu_growth(
    run: Callable[[Any], object],
    small: Any,
    large: Any,
    *,
    limit: float = 8.0,
    label: str = "",
    samples: int = 3,
    rounds: int = 3,
) -> Growth:
    """Fail unless ``run(large)`` costs under ``limit`` times ``run(small)``."""
    growth = cpu_growth(run, small, large, limit=limit, samples=samples, rounds=rounds)
    scale = len(large) / max(len(small), 1)
    assert growth.ratio < limit, (
        f"{label}: x{scale:.1f} input took x{growth.ratio:.2f} CPU time "
        f"(per-round ratios {', '.join(f'{r:.2f}' for r in growth.rounds)}; "
        f"best {growth.small:.6f}s vs {growth.large:.6f}s)"
    )
    return growth


def _traced_peak(run: Callable[[Any], Any], arg: Any) -> tuple[int, Any]:
    gc.collect()
    tracemalloc.start()
    try:
        result = run(arg)
        return tracemalloc.get_traced_memory()[1], result
    finally:
        tracemalloc.stop()


def peak_bytes(
    run: Callable[[Any], Any],
    arg: Any,
    *,
    budget: Callable[[Any], float] | None = None,
    rounds: int = 3,
) -> tuple[int, Any]:
    """Traced peak of ``run(arg)`` and its result; re-measured while over ``budget``.

    ``budget`` maps the result to the byte limit the caller will assert. The
    lowest peak across the measurements is returned, so ``run`` must be
    referentially transparent in cost: an allocation made only on its first
    call (a cache fill, a lazy import) is dropped by the re-measurement rather
    than failing the budget. See the module docstring.
    """
    peak, result = _traced_peak(run, arg)
    for _ in range(rounds - 1):
        if budget is None or peak < budget(result):
            break
        peak = min(peak, _traced_peak(run, arg)[0])
    return peak, result


@dataclass(frozen=True)
class PeakGrowth:
    small: int
    large: int
    ratio: float
    result: Any


def peak_growth(
    run: Callable[[Any], Any],
    small: Any,
    large: Any,
    *,
    limit: float = 8.0,
    budget: Callable[[Any], float] | None = None,
    rounds: int = 3,
) -> PeakGrowth:
    """Traced peaks of ``run`` on both sizes, re-measured while either check fails.

    ``result`` is ``run(large)``'s return value; ``budget`` (if given) maps it
    to the absolute byte limit the caller asserts on the large peak. Each size
    keeps its lowest peak, so ``run`` must be referentially transparent in
    cost: a first-call-only allocation (the small run warming a cache the large
    run reuses, or either size's own first call) is hidden by re-measurement.
    See the module docstring.
    """
    small_peak, _ = _traced_peak(run, small)
    large_peak, result = _traced_peak(run, large)
    for _ in range(rounds - 1):
        over_budget = budget is not None and large_peak >= budget(result)
        if large_peak / max(small_peak, 1) < limit and not over_budget:
            break
        small_peak = min(small_peak, _traced_peak(run, small)[0])
        large_peak = min(large_peak, _traced_peak(run, large)[0])
    return PeakGrowth(small_peak, large_peak, large_peak / max(small_peak, 1), result)
