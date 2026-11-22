"""Task #1362: every *-with-hub harness wrapper ships in-repo with one installer.

Source-level + subprocess tests against fake binaries and a fake $HOME; no
real CLI or token is touched.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from export_guard import exported_skip, needs_exported_paths

from unified_agent.dispatch import routing
from unified_agent.executor import backends

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INSTALLER = SCRIPTS / "install-with-hub-wrappers.sh"
DEVIN_SHIM = ROOT / "deploy" / "mac-hub" / "install-devin-wrapper.sh"
# The Devin shim lives with the owner's Mac deploy files, which the public
# export drops; its tests skip there and still fail loudly here if it goes.
needs_devin_shim = needs_exported_paths(DEVIN_SHIM, repo=ROOT)

# Wrappers that are not executor defaults but give interactive editors/apps the
# same bearer isolation. A new wrapper must be added here or be an executor
# default, or the drift test fails.
INTERACTIVE_LAUNCHERS = {
    "cursor-with-hub",
    "code-with-hub",
    "github-copilot-desktop-with-hub",
    "opencode-with-hub",
}


def _repo_wrappers() -> set[str]:
    return {p.name for p in SCRIPTS.glob("*-with-hub") if p.is_file()}


def _executor_default_wrappers() -> set[str]:
    from_routing = {b for b in routing._DEFAULT_BIN.values() if b.endswith("-with-hub")}
    from_backends = {
        value
        for name, value in vars(backends).items()
        if name.startswith("DEFAULT_")
        and name.endswith("_BIN")
        and isinstance(value, str)
        and value.endswith("-with-hub")
    }
    assert from_routing == from_backends, (from_routing, from_backends)
    return from_routing


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, env=env)


def _base_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("UA_WRAPPER_INSTALL_DIR", None)
    env.pop("UA_DEVIN_BIN_INSTALL", None)
    return env


# --- installer + drift ------------------------------------------------------


def test_installer_and_wrappers_have_valid_bash_syntax():
    shims = [] if exported_skip(ROOT, DEVIN_SHIM) else [DEVIN_SHIM]
    for path in [INSTALLER, *shims, *sorted(SCRIPTS.glob("*-with-hub"))]:
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, (path, result.stderr)


def test_repo_wrappers_are_executable_in_git():
    """The installer sets 0755 at install time, but a repo checkout used via
    PATH directly must be executable too."""
    for path in [INSTALLER, *SCRIPTS.glob("*-with-hub")]:
        assert path.stat().st_mode & stat.S_IXUSR, path


def test_installer_dry_run_lists_every_wrapper_and_writes_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = _run(["bash", str(INSTALLER), "--dry-run"], _base_env(home))
    assert result.returncode == 0, result.stderr
    listed = set(re.findall(r"/([\w.-]+-with-hub) \(mode 0755\)", result.stdout))
    assert listed == _repo_wrappers()
    assert not (home / ".local").exists()


def test_installer_installs_all_wrappers_0755_and_is_idempotent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = _base_env(home)
    first = _run(["bash", str(INSTALLER)], env)
    assert first.returncode == 0, first.stderr
    bindir = home / ".local" / "bin"
    installed = {p.name for p in bindir.iterdir()}
    assert installed == _repo_wrappers()
    for p in bindir.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o755, p
        assert p.read_bytes() == (SCRIPTS / p.name).read_bytes()

    # Loosen one mode and re-run: content unchanged, mode restored.
    (bindir / "agent-with-hub").chmod(0o600)
    second = _run(["bash", str(INSTALLER)], env)
    assert second.returncode == 0, second.stderr
    assert "installed" not in second.stdout
    assert second.stdout.count("unchanged") == len(_repo_wrappers())
    assert stat.S_IMODE((bindir / "agent-with-hub").stat().st_mode) == 0o755


def test_installer_replaces_stale_copy(tmp_path):
    dest = tmp_path / "bin"
    dest.mkdir()
    stale = dest / "copilot-with-hub"
    stale.write_text('#!/usr/bin/env bash\nexec copilot "$@"\n')
    env = _base_env(tmp_path)
    result = _run(["bash", str(INSTALLER), "--dest", str(dest), "--only", "copilot-with-hub"], env)
    assert result.returncode == 0, result.stderr
    assert stale.read_bytes() == (SCRIPTS / "copilot-with-hub").read_bytes()
    assert {p.name for p in dest.iterdir()} == {"copilot-with-hub"}


def test_installer_rejects_unknown_wrapper(tmp_path):
    result = _run(["bash", str(INSTALLER), "--dry-run", "--only", "nope"], _base_env(tmp_path))
    assert result.returncode == 2
    assert "unknown wrapper" in result.stderr


def test_installed_basenames_match_executor_defaults(tmp_path):
    """Drift guard (pattern from #1237): every wrapper-named executor default
    must be installed by the installer, and every installed wrapper must be
    either an executor default or a declared interactive launcher."""
    home = tmp_path / "home"
    home.mkdir()
    result = _run(["bash", str(INSTALLER)], _base_env(home))
    assert result.returncode == 0, result.stderr
    installed = {p.name for p in (home / ".local" / "bin").iterdir()}

    defaults = _executor_default_wrappers()
    assert defaults <= installed, defaults - installed
    assert installed == defaults | INTERACTIVE_LAUNCHERS
    assert backends.DEFAULT_CURSOR_BIN in installed


@needs_devin_shim
def test_devin_shim_delegates_to_installer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = _run(["bash", str(DEVIN_SHIM)], _base_env(home))
    assert result.returncode == 0, result.stderr
    bindir = home / ".local" / "bin"
    assert {p.name for p in bindir.iterdir()} == {"devin-with-hub"}
    assert stat.S_IMODE((bindir / "devin-with-hub").stat().st_mode) == 0o755


@needs_devin_shim
def test_devin_shim_honours_install_override_and_rejects_renames(tmp_path):
    env = _base_env(tmp_path)
    env["UA_DEVIN_BIN_INSTALL"] = str(tmp_path / "custom" / "devin-with-hub")
    ok = _run(["bash", str(DEVIN_SHIM)], env)
    assert ok.returncode == 0, ok.stderr
    assert (tmp_path / "custom" / "devin-with-hub").is_file()

    env["UA_DEVIN_BIN_INSTALL"] = str(tmp_path / "custom" / "devin")
    bad = _run(["bash", str(DEVIN_SHIM)], env)
    assert bad.returncode == 2
    assert not (tmp_path / "custom" / "devin").exists()


# --- secret hygiene ---------------------------------------------------------

_SECRET_LITERAL = re.compile(
    r"(ua_[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}"
    r"|Bearer\s+[A-Za-z0-9._-]{16,}|[A-Za-z0-9_-]{40,})"
)


@pytest.mark.parametrize("name", sorted(_repo_wrappers()))
def test_wrappers_hold_no_secret_literals_or_personal_paths(name):
    text = (SCRIPTS / name).read_text()
    assert not _SECRET_LITERAL.search(text), name
    assert "/home/" not in text and "/Users/" not in text, name
    # Every wrapper sources its hub token from a file at runtime.
    assert re.search(r"tr -d '\\r\\n' < \"\$\w*TOKEN_FILE\"", text), name
    assert "export UA_AGENT_BEARER_TOKEN=" in text, name


# --- behaviour: fail closed, isolate parent token ---------------------------


@dataclass(frozen=True)
class WrapperSpec:
    name: str
    token_env: str
    bin_env: str
    extra_token_env: str | None = None  # a second required token file


SPECS = [
    WrapperSpec("agent-with-hub", "UA_CURSOR_TOKEN_FILE", "CURSOR_AGENT_LINK"),
    WrapperSpec("cursor-with-hub", "UA_CURSOR_TOKEN_FILE", "CURSOR_BIN"),
    WrapperSpec("code-with-hub", "UA_COPILOT_TOKEN_FILE", "CODE_BIN"),
    WrapperSpec("copilot-with-hub", "UA_COPILOT_TOKEN_FILE", "COPILOT_BIN"),
    WrapperSpec("github-copilot-desktop-with-hub", "UA_COPILOT_TOKEN_FILE", "COPILOT_DESKTOP_BIN"),
    WrapperSpec("opencode-with-hub", "UA_OC_TOKEN_FILE", "OPENCODE_BIN", "GBRAIN_OC_TOKEN_FILE"),
]


def test_behaviour_specs_cover_every_wrapper_except_devin():
    # devin-with-hub has its own behaviour tests in
    # test_macbook_pro_migration_scripts.py (task #1237).
    assert {s.name for s in SPECS} | {"devin-with-hub"} == _repo_wrappers()


def _wrapper_env(tmp_path: Path, spec: WrapperSpec, token: str | None) -> dict[str, str]:
    env = _base_env(tmp_path)
    fake = tmp_path / "fake-cli"
    fake.write_text('#!/usr/bin/env bash\necho "UA=${UA_AGENT_BEARER_TOKEN} ARGS=$*"\n')
    fake.chmod(0o755)
    env[spec.bin_env] = str(fake)
    token_file = tmp_path / "token"
    if token is not None:
        token_file.write_text(token)
        token_file.chmod(0o600)
    env[spec.token_env] = str(token_file)
    if spec.extra_token_env:
        extra = tmp_path / "extra-token"
        extra.write_text("gbrain_test_token\n")
        env[spec.extra_token_env] = str(extra)
    env["UA_AGENT_BEARER_TOKEN"] = "parent-token-must-not-leak"
    return env


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_wrapper_fails_closed_when_token_missing(tmp_path, spec):
    result = _run(["bash", str(SCRIPTS / spec.name)], _wrapper_env(tmp_path, spec, None))
    assert result.returncode == 1
    assert "unavailable" in result.stderr
    assert "UA=" not in result.stdout


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_wrapper_fails_closed_when_token_empty(tmp_path, spec):
    result = _run(["bash", str(SCRIPTS / spec.name)], _wrapper_env(tmp_path, spec, "\n"))
    assert result.returncode == 1
    assert "empty" in result.stderr
    assert "UA=" not in result.stdout


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_wrapper_exports_own_token_over_parent_and_passes_args(tmp_path, spec):
    env = _wrapper_env(tmp_path, spec, "ua_test_wrapper_token\n")
    result = _run(["bash", str(SCRIPTS / spec.name), "-p", "hi"], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "UA=ua_test_wrapper_token ARGS=-p hi"
    assert "ua_test_wrapper_token" not in result.stderr


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_wrapper_fails_when_binary_missing(tmp_path, spec):
    env = _wrapper_env(tmp_path, spec, "ua_test_wrapper_token\n")
    env[spec.bin_env] = str(tmp_path / "does-not-exist")
    env["PATH"] = "/usr/bin:/bin"  # no cursor-agent/copilot fallback on PATH
    result = _run(["bash", str(SCRIPTS / spec.name)], env)
    assert result.returncode != 0
    assert "UA=" not in result.stdout
    assert "ua_test_wrapper_token" not in result.stderr


@pytest.mark.parametrize(
    "spec", [s for s in SPECS if s.name != "agent-with-hub"], ids=lambda s: s.name
)
def test_wrapper_resolves_bare_binary_override_via_path(tmp_path, spec):
    """Devin Review on #772: CODE_BIN=code-insiders must resolve through PATH,
    not be tested as a path relative to the cwd."""
    env = _wrapper_env(tmp_path, spec, "ua_test_wrapper_token\n")
    env[spec.bin_env] = "fake-cli"
    env["PATH"] = f"{tmp_path}{os.pathsep}/usr/bin:/bin"
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    result = subprocess.run(
        ["bash", str(SCRIPTS / spec.name)], capture_output=True, text=True, env=env, cwd=cwd
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("UA=ua_test_wrapper_token")


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_wrapper_never_inherits_parent_gbrain_token(tmp_path, spec):
    """Devin Review on #772: with no gbrain token file of its own, the child
    must not keep the parent's gbrain bearer."""
    env = _wrapper_env(tmp_path, spec, "ua_test_wrapper_token\n")
    (tmp_path / "fake-cli").write_text(
        '#!/usr/bin/env bash\necho "GB=${GBRAIN_BEARER_TOKEN:-}|${GBRAIN_TOKEN:-}"\n'
    )
    env["GBRAIN_BEARER_TOKEN"] = "parent-gbrain-must-not-leak"
    env["GBRAIN_TOKEN"] = "parent-gbrain-must-not-leak"
    if spec.extra_token_env is None:
        env["GBRAIN_TOKEN_FILE"] = str(tmp_path / "no-gbrain-token")
    result = _run(["bash", str(SCRIPTS / spec.name)], env)
    assert result.returncode == 0, result.stderr
    assert "parent-gbrain-must-not-leak" not in result.stdout


def test_sync_agent_tokens_provisions_every_wrapper_token_file():
    """Each wrapper's default hub-token file must be written by
    scripts/sync-agent-tokens.sh, or a fresh host can't launch it."""
    sync = (SCRIPTS / "sync-agent-tokens.sh").read_text()
    for directory in (".cursor", ".copilot", ".devin", ".opencode"):
        assert f'Path.home() / "{directory}" / "unified-agent-token"' in sync, directory
