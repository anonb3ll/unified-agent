"""deploy/ templates name no account's home; installers render them (task #2322).

Mac LaunchAgent templates write the account's home as ``__HOME__`` (launchd
expands neither ``~`` nor ``$HOME``), and every installer and documented install
step renders it with ``deploy/mac-hub/render_launchagent.py``, which
substitutes through plistlib and replaces the installed copy atomically. systemd
units use the ``%h`` specifier, which systemd expands itself. A verbatim copy of a ``__HOME__`` template would install
a job that execs a path that does not exist, so these tests run the installers'
own install steps against a fake home and parse what they write.
"""

from __future__ import annotations

import importlib.util
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MAC = REPO / "deploy" / "mac-hub"
SYSTEMD = REPO / "deploy" / "systemd"
TOKEN = "__HOME__"
# An absolute home directory of some account: /Users/<name>/ or /home/<name>/.
HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+")

PLISTS = sorted(MAC.glob("com.unified-agent.*.plist"))
UNITS = sorted([*SYSTEMD.glob("*.service"), *SYSTEMD.glob("*.timer")]) + sorted(
    (REPO / "deploy").glob("*.service")
)


RENDERER = MAC / "render_launchagent.py"
# The public export drops deploy/mac-hub/ (scripts/public_release/exclude.txt),
# so the exported tree has no renderer and no templates: skip, do not fail collection.
# Gate the skip on being an export (#1597 review): a renamed renderer in the private
# repo must fail loudly here instead of skipping green.
PUBLIC_EXPORT = not (REPO / "scripts" / "public_release").is_dir()
if PUBLIC_EXPORT and not RENDERER.exists():
    pytest.skip(f"{RENDERER.name} is not in this tree", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("render_launchagent", RENDERER)
renderer = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["render_launchagent"] = renderer
_spec.loader.exec_module(renderer)


def _render(template: Path, home: Path) -> bytes:
    """The installers' render step, run as they run it (a separate process)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / f"{template.stem}.plist"
        res = subprocess.run(
            [sys.executable, str(RENDERER), "--home", str(home), str(template), str(out)],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        return out.read_bytes()


@pytest.mark.parametrize("template", PLISTS + UNITS, ids=lambda p: p.name)
def test_template_names_no_account_home(template: Path):
    hits = HOME_PATH.findall(template.read_text(encoding="utf-8"))
    assert not hits, f"{template.name} names {hits}; use {TOKEN} (plist) or %h (unit)"


@pytest.mark.parametrize("template", PLISTS, ids=lambda p: p.name)
def test_plist_template_renders_to_a_valid_plist_under_home(template: Path, tmp_path: Path):
    home = tmp_path / "Users" / "someone"
    rendered = _render(template, home)
    assert TOKEN.encode() not in rendered
    data = plistlib.loads(rendered)
    assert data["Label"] == template.stem
    for arg in data.get("ProgramArguments") or []:
        if arg.startswith("/Users/") or arg.startswith(str(tmp_path)):
            assert arg.startswith(f"{home}/"), (template.name, arg)


def test_node_templates_run_the_release_link():
    """#2298: the node supervisors name only ~/.unified_agent/release/current."""
    unit = (SYSTEMD / "ua-node.service").read_text(encoding="utf-8")
    assert "\nExecStart=%h/.unified_agent/release/current/.venv/bin/unified-agent node\n" in unit
    home = Path("/Users/someone")
    data = plistlib.loads(_render(MAC / "com.unified-agent.node.plist", home))
    link = f"{home}/.unified_agent/release/current"
    assert data["ProgramArguments"] == [f"{link}/.venv/bin/unified-agent", "node"]
    assert data["EnvironmentVariables"]["UA_CODE_ROOT"] == link


def _stub_bin(tmp_path: Path) -> Path:
    bins = tmp_path / "bin"
    bins.mkdir()
    for name in ("launchctl", "sleep"):
        stub = bins / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "$STUB_LOG"\n')
        stub.chmod(0o755)
    return bins


def _heredoc(script: Path, after: str) -> str:
    text = script.read_text(encoding="utf-8")
    start = text.index("<<'REMOTE'\n", text.index(after)) + len("<<'REMOTE'\n")
    return text[start : text.index("\nREMOTE\n", start)]


def _fake_mac_home(tmp_path: Path) -> Path:
    home = tmp_path / "Users" / "someone"
    checkout = home / "dev" / "unified_agent" / "deploy" / "mac-hub"
    checkout.mkdir(parents=True)
    for src in MAC.iterdir():
        if src.is_file():
            shutil.copy2(src, checkout / src.name)
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    # A usable release link, so the #1912 legacy-repoint guard leaves the
    # rendered link paths alone and the test sees the installer's own output.
    release = tmp_path / "release"
    (release / ".venv" / "bin").mkdir(parents=True)
    exe = release / ".venv" / "bin" / "unified-agent"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (home / "dev" / "ua-current").symlink_to(release)
    return home


def _installed(home: Path) -> dict[str, dict]:
    agents = home / "Library" / "LaunchAgents"
    out = {}
    for path in sorted(agents.glob("*.plist")):
        raw = path.read_bytes()
        assert TOKEN.encode() not in raw, f"{path.name} installed unrendered"
        out[path.stem] = plistlib.loads(raw)
    return out


def _run_remote(body: str, home: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    log = tmp_path / "stub.log"
    log.touch()
    return subprocess.run(
        ["bash", "-c", body],
        cwd=home,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": f"{_stub_bin(tmp_path)}:/usr/bin:/bin",
            "STUB_LOG": str(log),
        },
        timeout=60,
    )


def test_setup_installer_renders_every_launchagent_it_installs(tmp_path: Path):
    home = _fake_mac_home(tmp_path)
    body = _heredoc(MAC / "setup-agents-and-executors.sh", 'log "Installing Mac executor')
    res = _run_remote(body, home, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr

    installed = _installed(home)
    # The three jobs it used to `cp` verbatim are rendered like the rest.
    for label in ("executor-agy", "executor-codex", "database-backup", "dispatch-tick"):
        assert f"com.unified-agent.{label}" in installed
    for label, data in installed.items():
        text = repr(data)
        assert str(home) in text, f"{label}: no path under the account's home"
        assert not re.search(r"/Users/(?!someone/)", text.replace(str(tmp_path), "")), label


def test_hub_migration_renders_the_hub_launchagent(tmp_path: Path):
    home = _fake_mac_home(tmp_path)
    text = (MAC / "migrate-hub-to-mac.sh").read_text(encoding="utf-8")
    start = text.index('ssh "$MAC_HOST" \'set -euo pipefail', text.index('log "5/9'))
    body = text[text.index("'", start) + 1 : text.index("'\n", text.index("bootstrap", start))]
    res = _run_remote(body, home, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr

    hub = _installed(home)["com.unified-agent.hub"]
    assert hub["EnvironmentVariables"]["UA_CODE_ROOT"] == f"{home}/dev/ua-current"
    assert hub["ProgramArguments"][0].startswith(f"{home}/")


@pytest.mark.parametrize(
    "installer",
    [
        MAC / "setup-agents-and-executors.sh",
        MAC / "migrate-hub-to-mac.sh",
        MAC / "com.unified-agent.node.plist",
        REPO / "docs" / "installation.md",
        REPO / "docs" / "deploy" / "node-release-roll.md",
    ],
    ids=lambda p: p.name,
)
def test_no_install_step_copies_a_template_verbatim(installer: Path):
    """`cp <template> ~/Library/LaunchAgents/` would install __HOME__ unrendered."""
    for line in installer.read_text(encoding="utf-8").splitlines():
        if re.search(r"\bcp\b.*com\.unified-agent\.[a-z-]+\.plist", line):
            assert "retired/" in line, f"{installer.name}: verbatim copy: {line.strip()}"


@pytest.mark.parametrize("name", ["some one", "a&b", "a|b", "a#b", "a<b>", "q'uo\"te"])
def test_render_handles_any_home(tmp_path: Path, name: str):
    """A sed render broke on & and |, and & or < in its output was invalid XML."""
    home = tmp_path / "Users" / name
    home.mkdir(parents=True)
    raw = _render(MAC / "com.unified-agent.node.plist", home)
    assert TOKEN.encode() not in raw
    data = plistlib.loads(raw)
    assert data["WorkingDirectory"] == str(home)
    link = f"{home}/.unified_agent/release/current"
    assert data["ProgramArguments"] == [f"{link}/.venv/bin/unified-agent", "node"]
    assert data["EnvironmentVariables"]["UA_CODE_ROOT"] == link


def _existing_dest(tmp_path: Path) -> tuple[Path, bytes]:
    dest = tmp_path / "LaunchAgents" / "com.unified-agent.node.plist"
    dest.parent.mkdir()
    before = plistlib.dumps({"Label": "com.unified-agent.node", "Program": "/old"})
    dest.write_bytes(before)
    return dest, before


@pytest.mark.parametrize(
    ("template_text", "home"),
    [
        ("not a plist", "/Users/someone"),
        ("<plist><dict><key>Label</key><string>x</string></dict>", "/Users/someone"),
        (None, "relative/home"),
        (None, ""),
        (None, "/Users/ctrl\x01char"),
        (None, "/Users/__HOME__"),
    ],
    ids=["garbage", "truncated", "relative-home", "empty-home", "control-char", "token-home"],
)
def test_failed_render_leaves_the_installed_copy_untouched(tmp_path, template_text, home):
    dest, before = _existing_dest(tmp_path)
    template = MAC / "com.unified-agent.node.plist"
    if template_text is not None:
        template = tmp_path / "bad.plist"
        template.write_text(template_text)
    assert renderer.main(["--home", home, str(template), str(dest)]) == 1
    assert dest.read_bytes() == before
    assert sorted(p.name for p in dest.parent.iterdir()) == [dest.name]


def test_token_in_a_key_is_refused(tmp_path: Path):
    dest, before = _existing_dest(tmp_path)
    template = tmp_path / "keyed.plist"
    template.write_bytes(plistlib.dumps({"Label": "x", "EnvironmentVariables": {TOKEN: "v"}}))
    assert renderer.main(["--home", "/Users/someone", str(template), str(dest)]) == 1
    assert dest.read_bytes() == before


def test_failed_replace_leaves_the_installed_copy_untouched(tmp_path, monkeypatch):
    dest, before = _existing_dest(tmp_path)

    def boom(*_args):
        raise OSError("injected rename failure")

    monkeypatch.setattr(renderer.os, "replace", boom)
    template = MAC / "com.unified-agent.node.plist"
    assert renderer.main(["--home", "/Users/someone", str(template), str(dest)]) == 1
    assert dest.read_bytes() == before
    assert sorted(p.name for p in dest.parent.iterdir()) == [dest.name]


def test_render_replaces_an_existing_copy_with_mode_644(tmp_path: Path):
    dest, _ = _existing_dest(tmp_path)
    dest.chmod(0o600)
    template = MAC / "com.unified-agent.node.plist"
    assert renderer.main(["--home", "/Users/someone", str(template), str(dest)]) == 0
    assert plistlib.loads(dest.read_bytes())["WorkingDirectory"] == "/Users/someone"
    assert dest.stat().st_mode & 0o777 == 0o644


# --- no command loads or copies a __HOME__ template directly ------------------

SCAN_ROOTS = [REPO / "docs", REPO / "scripts", REPO / "deploy", REPO / "README.md"]
SCAN_SKIP = (
    "docs/archive/",
    "docs/audits/",
    "deploy/mac-hub/retired/",
    "scripts/public_release/",
)
TOKEN_TEMPLATES = {p.name for p in PLISTS if TOKEN in p.read_text(encoding="utf-8")}
# A template named from the repo: a path through the checkout, not LaunchAgents.
REPO_REF = re.compile(
    r"(?:mac-hub/|\$ROOT/|\$\{ROOT\}/|\$SCRIPT_DIR/|deploy/)"
    r"(com\.unified-agent\.(?:\$\{label\}|\$label|[a-z-]+)\.plist)"
)
ACTION = re.compile(
    r"launchctl\s+(?:bootstrap|load)|\b(?:cp|ln|mv|sed|ditto|rsync|cat)\s|\binstall\s+-"
    r"|>\s*[\"'~$/]"
)


def _logical_lines(path: Path):
    joined, start = "", 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not joined:
            start = number
        if line.rstrip().endswith("\\"):
            joined += line.rstrip()[:-1] + " "
            continue
        yield start, joined + line
        joined = ""
    if joined:
        yield start, joined


def _direct_loads() -> list[str]:
    found = []
    for root in SCAN_ROOTS:
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in files:
            rel = path.relative_to(REPO).as_posix()
            if not path.is_file() or rel.startswith(SCAN_SKIP):
                continue
            if path.suffix not in {".md", ".sh", ".py", ".plist", ".example"}:
                continue
            for number, line in _logical_lines(path):
                if "render_launchagent.py" in line or re.search(r"\bscp\b", line):
                    continue  # rendering, or staging a template into a checkout
                refs = [m.group(1) for m in REPO_REF.finditer(line)]
                hits = [r for r in refs if "label" in r or r in TOKEN_TEMPLATES]
                if hits and ACTION.search(line):
                    found.append(f"{rel}:{number}: {line.strip()[:160]}")
    return found


def test_token_templates_exist_for_the_scan():
    assert {"com.unified-agent.node.plist", "com.unified-agent.dispatch-tick.plist"} <= (
        TOKEN_TEMPLATES
    )


def test_no_doc_or_script_loads_or_copies_a_template_directly():
    """#2322 review: bootstrapping or copying a __HOME__ template as-is never runs."""
    assert _direct_loads() == []


def test_scan_catches_a_direct_bootstrap(tmp_path, monkeypatch):
    doc = tmp_path / "docs" / "x.md"
    doc.parent.mkdir()
    doc.write_text(
        'launchctl bootstrap "gui/$(id -u)" \\\n'
        "  ~/dev/unified_agent/deploy/mac-hub/com.unified-agent.dispatch-tick.plist\n"
        "cp deploy/mac-hub/com.unified-agent.node.plist ~/Library/LaunchAgents/\n"
        'sed "s|__HOME__|$HOME|g" "$ROOT/com.unified-agent.${label}.plist" > "$LA/x.plist"\n'
        "launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.unified-agent.node.plist\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "SCAN_ROOTS", [tmp_path / "docs"])
    found = _direct_loads()
    assert [f.split(":")[1] for f in found] == ["1", "3", "4"]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_node_unit_passes_systemd_analyze(tmp_path: Path):
    unit = tmp_path / "ua-node.service"
    unit.write_text((SYSTEMD / "ua-node.service").read_text(encoding="utf-8"))
    # A user-mode verify needs a runtime directory. A CI sandbox without a
    # login session has no XDG_RUNTIME_DIR ("Failed to initialize manager").
    # Keep it short: the user units' socket paths must fit in sun_path.
    with tempfile.TemporaryDirectory(prefix="ua-run-", dir="/tmp") as runtime:
        res = subprocess.run(
            ["systemd-analyze", "--user", "verify", str(unit)],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(tmp_path), "XDG_RUNTIME_DIR": runtime},
            timeout=60,
        )
    # The executable is absent in a fake home; nothing else may be wrong.
    problems = [
        line
        for line in res.stderr.splitlines()
        if line.strip() and "is not executable" not in line and "No such file" not in line
    ]
    assert not problems, res.stderr
