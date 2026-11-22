"""Cold-start dependency guard: the installable surface must resolve to an mcp
2.x line.

`uv tool install .` re-resolves dependencies from pyproject.toml (it does not
honor uv.lock for non-workspace installs), so the pyproject constraint is the
contract for a fresh machine. unified-agent is migrated to mcp 2.x
(mcp.server.fastmcp → mcp.server.mcpserver, UA #818); the floor keeps 1.x out,
and an explicit `<3` cap freezes the surface against the next breaking major
(the exact failure mode mcp 2.0 caused — found live on the NMD-T2 cold-start
VM, hub task #793).
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _dependencies() -> list[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["project"]["dependencies"]


def test_mcp_constraint_pins_2x_line():
    deps = _dependencies()
    mcp = [d for d in deps if d.split("[")[0] == "mcp"]
    assert len(mcp) == 1, f"expected exactly one mcp dependency, got {mcp}"
    spec = mcp[0]

    # Floor must exclude the 1.x line the code no longer targets.
    assert ">=2" in spec, (
        f"mcp dependency '{spec}' has no >=2 floor: unified-agent is migrated "
        "to mcp 2.x (mcp.server.mcpserver); a 1.x resolution breaks every "
        "`unified-agent` entry point on a fresh `uv tool install .`"
    )

    # Upper bound must cap the major version below 3.0 so the next mcp major
    # cannot silently break the installable surface again.
    assert "<3" in spec, (
        f"mcp dependency '{spec}' has no <3 cap: the next mcp major may "
        "rename APIs out from under a fresh `uv tool install .`"
    )


def test_mcp_constraint_still_admits_locked_version():
    """The bounds must not exclude the version uv.lock actually pins."""
    deps = _dependencies()
    spec = next(d for d in deps if d.split("[")[0] == "mcp")

    locked = _locked_mcp_version()
    assert locked, "uv.lock does not pin an mcp version"

    # Minimal sanity: parse the floor/ceiling and check the locked version fits.
    floor = int(spec.split(">=")[1].split(",")[0])
    ceiling = int(spec.split("<")[1])
    major = int(locked.split(".")[0])
    assert floor <= major < ceiling, (
        f"uv.lock pins mcp {locked}, outside pyproject constraint '{spec}'"
    )


def _spec(package: str) -> str:
    deps = _dependencies()
    matches = [
        dep
        for dep in deps
        if dep == package or (dep.startswith(package) and dep[len(package)] in "><=!~[")
    ]
    assert len(matches) == 1, f"expected exactly one {package} dependency, got {matches}"
    return matches[0]


def test_embedding_and_ui_floors_stay_on_the_locked_minor():
    """Floors admit the locked release and stop the next breaking line."""
    assert _spec("fastembed") == "fastembed>=0.9,<0.10"
    assert _spec("textual") == "textual>=8,<9"
    assert _spec("langsmith") == "langsmith>=0.11,<1"


def test_direct_imports_are_declared():
    """Packages imported by name must not stay transitive-only."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert "tree-sitter" not in data["project"]["optional-dependencies"]
    for package, floor in {
        "numpy": ">=2.4.6",
        "anyio": ">=4.14.1",
        "pydantic": ">=2.13.4",
        "sse-starlette": ">=3.4.5",
        "uvicorn": ">=0.51.0",
        "starlette": ">=1.3.1",
    }.items():
        assert _spec(package).startswith(f"{package}{floor}")
    # pypdf's floor is raised by task #1523 (confined-worker PDF extraction)
    # to close DoS advisories (memory, CPU, infinite loop) in pypdf<6.16.1
    # that an untrusted PDF can trigger; see .github/dependency-audit-ignores.yml.
    # 6.21.4 closes the later 2026-11 pypdf advisories (CVE-2026-113786 through
    # CVE-2026-119209) that pip-audit reported against the 6.20.1 lock.
    pdf_extra = data["project"]["optional-dependencies"]["pdf"]
    dev = data["dependency-groups"]["dev"]
    assert "pypdf>=6.21.4" in pdf_extra
    assert "pypdf>=6.21.4" in dev


def test_pdf_dependencies_are_dev_and_optional_for_documented_pytest():
    """Default development sync must test PDF support without requiring it at runtime."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    dev_deps = data["dependency-groups"]["dev"]
    pdf_extra = data["project"]["optional-dependencies"]["pdf"]
    for package in ("pypdf", "fpdf2"):
        assert any(dependency.startswith(package) for dependency in dev_deps), (
            f"{package} must be a development dependency because `uv run pytest` "
            "exercises PDF behavior without selecting an extra"
        )
        assert any(dependency.startswith(package) for dependency in pdf_extra)
        assert not any(dependency.startswith(package) for dependency in deps)


def _locked_mcp_version() -> str:
    import re

    lock = (REPO_ROOT / "uv.lock").read_text()
    block = re.search(r'\[\[package\]\]\nname = "mcp"\nversion = "([^"]+)"', lock)
    return block.group(1) if block else ""
