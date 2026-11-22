"""scripts/check_layers.py keeps src/unified_agent's imports inside the layer map.

The map is docs/architecture/layers.toml and the known inversions are in
docs/architecture/layers-baseline.txt. The repo test is the gate: a new upward
import, an unplaced module, or a baseline line for an inversion that has been
fixed all fail it. The rest pin the checker on small synthetic packages.
"""

import importlib.util
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "check_layers", REPO_ROOT / "scripts" / "check_layers.py"
)
layers = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = layers  # dataclasses resolve annotations via sys.modules
spec.loader.exec_module(layers)


def make_pkg(tmp_path, files, layer_map, baseline="", seams=()):
    """Write package ``app`` under tmp_path/src and a layers.toml for it."""
    for rel, body in {"__init__.py": "", **files}.items():
        path = tmp_path / "src" / "app" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))
    toml = ['package = "app"', 'source = "src"', 'baseline = "baseline.txt"']
    for name, modules in layer_map:
        toml += ["", "[[layer]]", f'name = "{name}"', f"modules = {modules!r}"]
    for src, dst in seams:
        toml += ["", "[[seam]]", f'from = "{src}"', f'to = "{dst}"', 'why = "test"']
    (tmp_path / "layers.toml").write_text("\n".join(toml).replace("'", '"') + "\n")
    (tmp_path / "baseline.txt").write_text(baseline)
    return tmp_path / "layers.toml"


def run(config_path):
    return layers.check(layers.load_config(config_path))


TWO_LAYERS = [("top", ["web*"]), ("bottom", ["store", "util"])]


def test_repo_imports_match_the_layer_map():
    result = run(layers.DEFAULT_CONFIG)
    problems = (
        [f"new upward import {s} -> {d}" for s, d in result.new]
        + [f"fixed, delete from baseline: {s} -> {d}" for s, d in result.stale]
        + [f"not in any layer: {m}" for m in result.unmapped]
        + [f"in two layers: {m} {h}" for m, h in result.ambiguous.items()]
        + [f"pattern matches nothing: {lay} {p}" for lay, p in result.unused_patterns]
        + [f"seam is not an upward import: {s} -> {d}" for s, d in result.unused_seams]
    )
    assert not problems, (
        "Layer check failed. Run `uv run python scripts/check_layers.py` for details and "
        "see docs/architecture/layers.md.\n" + "\n".join(problems)
    )


def test_downward_and_same_layer_imports_pass(tmp_path):
    config = make_pkg(
        tmp_path,
        {
            "web/__init__.py": "",
            "web/api.py": "from ..store import save\nfrom . import views\n",
            "web/views.py": "",
            "store.py": "from . import util\n",
            "util.py": "",
        },
        TWO_LAYERS,
    )
    result = run(config)
    assert result.ok
    assert result.edges["web.api"] == {"store", "web.views"}
    assert result.edges["store"] == {"util"}


def test_lazy_upward_import_is_a_new_violation(tmp_path):
    config = make_pkg(
        tmp_path,
        {
            "web.py": "",
            "store.py": "def f():\n    from .web import handler\n    return handler\n",
            "util.py": "",
        },
        TWO_LAYERS,
    )
    result = run(config)
    assert not result.ok
    assert result.new == [("store", "web")]
    assert layers.main(["--config", str(config)]) == 1


def test_type_checking_and_absolute_imports_count(tmp_path):
    config = make_pkg(
        tmp_path,
        {
            "web.py": "",
            "store.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import app.web\n",
            "util.py": "from app.web import thing\n",
        },
        TWO_LAYERS,
    )
    assert run(config).new == [("store", "web"), ("util", "web")]


def test_baseline_allows_known_inversion_and_fails_once_it_is_fixed(tmp_path):
    files = {"web.py": "", "store.py": "from . import web\n", "util.py": ""}
    config = make_pkg(tmp_path, files, TWO_LAYERS, baseline="# known\nstore -> web\n")
    assert run(config).ok

    (tmp_path / "src" / "app" / "store.py").write_text("")
    result = run(config)
    assert not result.ok
    assert result.stale == [("store", "web")]


def test_write_baseline_round_trips(tmp_path):
    files = {"web.py": "", "store.py": "from . import web\n", "util.py": "from .web import x\n"}
    config = make_pkg(tmp_path, files, TWO_LAYERS)
    assert layers.main(["--config", str(config), "--write-baseline"]) == 0
    text = (tmp_path / "baseline.txt").read_text()
    assert "# bottom -> top (2)" in text
    assert layers.read_baseline(tmp_path / "baseline.txt") == {("store", "web"), ("util", "web")}
    assert layers.main(["--config", str(config)]) == 0


def test_every_module_must_be_placed_once(tmp_path):
    files = {"web.py": "", "store.py": "", "util.py": "", "stray.py": ""}
    result = run(make_pkg(tmp_path, files, TWO_LAYERS))
    assert result.unmapped == ["stray"]
    assert not result.ok


def test_two_globs_are_ambiguous_until_an_exact_name_decides(tmp_path):
    files = {"jobs/__init__.py": "", "jobs/cli.py": "", "util.py": ""}
    globbed = [("top", ["*.cli"]), ("bottom", ["jobs", "jobs.*", "util"])]
    result = run(make_pkg(tmp_path, files, globbed))
    assert result.ambiguous == {"jobs.cli": ["bottom", "top"]}

    exact = [("top", ["*.cli", "jobs.cli"]), ("bottom", ["jobs", "jobs.*", "util"])]
    result = run(make_pkg(tmp_path, files, exact))
    assert result.ok
    assert result.modules["jobs.cli"] == "top"


def test_pattern_that_matches_nothing_fails(tmp_path):
    files = {"web.py": "", "store.py": "", "util.py": ""}
    result = run(
        make_pkg(tmp_path, files, [("top", ["web", "gone"]), ("bottom", ["store", "util"])])
    )
    assert result.unused_patterns == [("top", "gone")]
    assert not result.ok


def test_attribute_import_resolves_to_its_module(tmp_path):
    files = {
        "web/__init__.py": "",
        "web/api.py": "",
        "store.py": "from .web.api import Handler\nfrom .web import api as alias\n",
        "util.py": "",
    }
    result = run(make_pkg(tmp_path, files, TWO_LAYERS))
    assert result.edges["store"] == {"web.api"}


def test_declared_seam_is_allowed_and_kept_apart_from_the_baseline(tmp_path):
    files = {"web.py": "", "store.py": "def load():\n    from . import web\n", "util.py": ""}
    result = run(make_pkg(tmp_path, files, TWO_LAYERS, seams=[("store", "web")]))
    assert result.ok
    assert result.seams == {("store", "web")}
    assert result.violations == set()


def test_seam_that_is_not_an_upward_import_fails(tmp_path):
    files = {"web.py": "from . import store\n", "store.py": "", "util.py": ""}
    result = run(make_pkg(tmp_path, files, TWO_LAYERS, seams=[("web", "store")]))
    assert result.unused_seams == [("web", "store")]
    assert not result.ok


def test_attribute_import_from_a_higher_package_is_kept_beside_a_child_import(tmp_path):
    # web is an adapter package, but its web.api child is placed in core by
    # exact name. Importing the child must not hide a separate attribute
    # import from the package itself (Devin review on PR #943).
    files = {
        "web/__init__.py": "setting = 1\n",
        "web/api.py": "",
        "store.py": "from .web import setting\nfrom .web import api\n",
        "util.py": "",
    }
    layer_map = [("top", ["web"]), ("bottom", ["web.api", "store", "util"])]
    result = run(make_pkg(tmp_path, files, layer_map))
    assert result.edges["store"] == {"web", "web.api"}
    assert result.new == [("store", "web")]


def test_child_module_import_does_not_add_the_parent_package(tmp_path):
    files = {
        "web/__init__.py": "",
        "web/api.py": "",
        "store.py": "from .web import api\n",
        "util.py": "",
    }
    layer_map = [("top", ["web"]), ("bottom", ["web.api", "store", "util"])]
    result = run(make_pkg(tmp_path, files, layer_map))
    assert result.edges["store"] == {"web.api"}
    assert result.ok
