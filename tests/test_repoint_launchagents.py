"""Repointing installed LaunchAgents at the atomic-deploy release link (task #1912)."""

from __future__ import annotations

import importlib.util
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "mac-hub" / "repoint_launchagents.py"
PLIST_DIR = REPO / "deploy" / "mac-hub"
# The public export drops deploy/mac-hub/ (scripts/public_release/exclude.txt),
# so the exported tree cannot import the script: skip instead of failing collection.
# Gate the skip on being an export (#1597 review): in the private repo a renamed
# script must fail loudly here, not read as a green skip.
PUBLIC_EXPORT = not (REPO / "scripts" / "public_release").is_dir()
if PUBLIC_EXPORT and not SCRIPT.exists():
    pytest.skip(f"{SCRIPT.name} is not in this tree", allow_module_level=True)
# Installed plists name a real home; repo templates name it __HOME__ (#2322).
LINK = "/Users/someone/dev/ua-current"
LEGACY = "/Users/someone/dev/unified_agent"
TEMPLATE_LINK = "__HOME__/dev/ua-current"
TEMPLATE_LEGACY = "__HOME__/dev/unified_agent"

_spec = importlib.util.spec_from_file_location("repoint_launchagents", SCRIPT)
repoint = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# dataclasses resolve the module through sys.modules while executing it.
sys.modules["repoint_launchagents"] = repoint
_spec.loader.exec_module(repoint)


def _write(path: Path, data: dict) -> Path:
    with path.open("wb") as fh:
        plistlib.dump(data, fh)
    return path


def _read(path: Path) -> dict:
    with path.open("rb") as fh:
        return plistlib.load(fh)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    bins = tmp_path / "bin"
    bins.mkdir()
    launchctl = bins / "launchctl"
    launchctl.write_text(
        '#!/bin/sh\n[ "$1" = print ] && [ "$2" = "gui/'
        + str(os.getuid())
        + '/com.unified-agent.hub" ]\n'
    )
    launchctl.chmod(0o755)
    legacy = tmp_path / "dev" / "unified_agent"
    _write(
        agents / "com.unified-agent.pr-watch.plist",
        {
            "Label": "com.unified-agent.pr-watch",
            "ProgramArguments": [f"{legacy}/deploy/mac-hub/run-pr-watch.sh"],
            "WorkingDirectory": str(legacy),
            "StartInterval": 1800,
        },
    )
    _write(
        agents / "com.unified-agent.hub.plist",
        {
            "Label": "com.unified-agent.hub",
            "ProgramArguments": [str(tmp_path / ".unified_agent" / "run-hub-live-abc.sh")],
            "EnvironmentVariables": {"UA_HTTP_PORT": "8765"},
        },
    )
    _write(
        agents / "com.unified-agent.cursor-env.plist",
        {"Label": "com.unified-agent.cursor-env", "ProgramArguments": ["/bin/sh", "-c", "true"]},
    )
    # A backup next to the live plist must never be rewritten.
    _write(
        agents / "com.unified-agent.hub.plist.bak-20261002",
        {"Label": "com.unified-agent.hub", "ProgramArguments": [f"{legacy}/x.sh"]},
    )
    return tmp_path


def _release(home: Path) -> Path:
    release = home / "dev" / "ua-releases" / "abc123"
    (release / "deploy" / "mac-hub").mkdir(parents=True)
    (release / "deploy" / "mac-hub" / "run-hub.sh").write_text("#!/bin/bash\n")
    (release / ".venv" / "bin").mkdir(parents=True)
    (release / ".venv" / "bin" / "unified-agent").write_text("#!/bin/sh\n")
    (home / "dev" / "ua-current").symlink_to(release)
    return release


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": f"{home}/bin:/usr/bin:/bin"},
    )


def test_dry_run_reports_and_writes_nothing(home: Path):
    plist = home / "Library/LaunchAgents/com.unified-agent.pr-watch.plist"
    before = plist.read_bytes()
    result = _run(home, "--to-link")
    assert result.returncode == 0, result.stderr
    assert "run-pr-watch.sh" in result.stdout
    assert "Dry run" in result.stdout
    assert plist.read_bytes() == before
    assert not list(plist.parent.glob("*.bak-1912-*"))


def test_apply_refuses_without_a_usable_link(home: Path):
    result = _run(home, "--to-link", "--apply")
    assert result.returncode == 2
    assert "not a symlink" in result.stderr


def test_apply_repoints_jobs_and_sets_code_root(home: Path):
    _release(home)
    link = str(home / "dev" / "ua-current")
    result = _run(home, "--to-link", "--apply")
    assert result.returncode == 0, result.stderr

    agents = home / "Library/LaunchAgents"
    watch = _read(agents / "com.unified-agent.pr-watch.plist")
    assert watch["ProgramArguments"] == [f"{link}/deploy/mac-hub/run-pr-watch.sh"]
    assert watch["WorkingDirectory"] == link
    assert watch["EnvironmentVariables"] == {"UA_CODE_ROOT": link}
    assert watch["StartInterval"] == 1800

    hub = _read(agents / "com.unified-agent.hub.plist")
    # The hub's launcher lives outside the repo: only the code root changes,
    # and its existing environment is preserved.
    assert hub["ProgramArguments"][0].endswith("run-hub-live-abc.sh")
    assert hub["EnvironmentVariables"] == {"UA_HTTP_PORT": "8765", "UA_CODE_ROOT": link}
    assert "install-hub-launcher.sh" in result.stdout

    untouched = _read(agents / "com.unified-agent.cursor-env.plist")
    assert "EnvironmentVariables" not in untouched
    backup = _read(agents / "com.unified-agent.hub.plist.bak-20261002")
    assert "UA_CODE_ROOT" not in backup.get("EnvironmentVariables", {})
    assert len(list(agents.glob("*.bak-1912-*"))) == 2
    assert "launchctl bootout" in result.stdout
    assert (
        f'launchctl bootstrap "gui/$(id -u)" "{agents}/com.unified-agent.hub.plist"'
        in result.stdout
    )
    assert (
        f'launchctl bootstrap "gui/$(id -u)" "{agents}/com.unified-agent.pr-watch.plist"'
        not in result.stdout
    )


@pytest.mark.parametrize("failure", ["replace", "backup"])
def test_write_failure_restores_whole_cutover(home: Path, monkeypatch, capsys, failure):
    _release(home)
    paths = list((home / "Library/LaunchAgents").glob("*.plist"))
    before = {path: path.read_bytes() for path in paths}
    module = repoint.os if failure == "replace" else repoint.shutil
    name = "replace" if failure == "replace" else "copy2"
    real = getattr(module, name)
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise OSError("injected write failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, fail_second)
    monkeypatch.setenv("HOME", str(home))
    assert repoint.main(["--to-link", "--apply"]) == 2
    assert {path: path.read_bytes() for path in paths} == before
    assert not list((home / "Library/LaunchAgents").glob("tmp*"))
    output = capsys.readouterr()
    assert "failed" in output.err
    assert "launchctl bootstrap" not in output.out


def test_unknown_launchd_state_never_prints_activation_commands(home: Path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(repoint, "loaded_job", lambda label: None)
    assert repoint.main(["--to-link"]) == 0
    out = capsys.readouterr().out
    assert "load state unknown" in out
    assert "launchctl bootstrap" not in out


def test_failed_rollback_names_retained_recovery_backup(home: Path, monkeypatch, capsys):
    _release(home)
    real = repoint.os.replace
    calls = []

    def fail_write_and_restore(*args):
        calls.append(args)
        if len(calls) in (2, 3):
            raise OSError("injected storage failure")
        return real(*args)

    monkeypatch.setattr(repoint.os, "replace", fail_write_and_restore)
    monkeypatch.setenv("HOME", str(home))
    assert repoint.main(["--to-link", "--apply"]) == 2
    output = capsys.readouterr()
    assert "manual recovery required" in output.err
    assert ".bak-1912-" in output.err
    assert "launchctl bootstrap" not in output.out


def test_fresh_setup_stages_the_repoint_utility():
    text = (PLIST_DIR / "setup-agents-and-executors.sh").read_text()
    start = text.index("scp -q \\")
    end = text.index('"${MAC_SSH}:dev/unified_agent/deploy/mac-hub/"', start)
    assert '"$SCRIPT_DIR/repoint_launchagents.py"' in text[start:end]
    assert '"$SCRIPT_DIR/render_launchagent.py"' in text[start:end]


def test_apply_is_idempotent_and_reversible(home: Path):
    _release(home)
    agents = home / "Library/LaunchAgents"
    original = _read(agents / "com.unified-agent.pr-watch.plist")
    original_hub = _read(agents / "com.unified-agent.hub.plist")

    assert _run(home, "--to-link", "--apply").returncode == 0
    again = _run(home, "--to-link", "--apply")
    assert again.returncode == 0
    assert "Nothing to change" in again.stdout

    back = _run(home, "--to-legacy", "--apply")
    assert back.returncode == 0, back.stderr
    assert _read(agents / "com.unified-agent.pr-watch.plist") == original
    assert _read(agents / "com.unified-agent.hub.plist") == original_hub
    # A rapid rollback must keep the pre-cutover backup as well as the linked one.
    assert len(list(agents.glob("*.bak-1912-*"))) == 4


def test_program_only_plist_is_repointed(home: Path):
    path = home / "Library/LaunchAgents/com.unified-agent.node.plist"
    _write(path, {"Label": path.stem, "Program": f"{LEGACY}/.venv/bin/ua-node"})
    change = repoint.plan_plist(path, old_root=LEGACY, new_root=LINK, set_env=True)
    assert change is not None
    assert change.data["Program"] == f"{LINK}/.venv/bin/ua-node"
    assert change.data["EnvironmentVariables"] == {"UA_CODE_ROOT": LINK}


def test_apply_preserves_plist_permissions(home: Path):
    _release(home)
    path = home / "Library/LaunchAgents/com.unified-agent.hub.plist"
    path.chmod(0o600)
    result = _run(home, "--to-link", "--apply")
    assert result.returncode == 0, result.stderr
    assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_plist_refuses_whole_apply_before_writes(home: Path):
    _release(home)
    agents = home / "Library/LaunchAgents"
    hub = agents / "com.unified-agent.hub.plist"
    before = hub.read_bytes()
    (agents / "com.unified-agent.invalid.plist").write_text("not a plist")
    result = _run(home, "--to-link", "--apply")
    assert result.returncode != 0
    assert hub.read_bytes() == before
    assert not list(agents.glob("*.bak-1912-*"))


def test_templates_run_code_through_the_release_link():
    """Every repo template that runs checkout code goes through the link (#1912)."""
    checked = 0
    for plist in sorted(PLIST_DIR.glob("com.unified-agent.*.plist")):
        data = _read(plist)
        text = plist.read_text()
        assert TEMPLATE_LEGACY not in text, (
            f"{plist.name} still runs code from the in-place checkout"
        )
        args = data.get("ProgramArguments") or []
        if any(isinstance(a, str) and a.startswith(TEMPLATE_LINK + "/") for a in args):
            checked += 1
            assert data["EnvironmentVariables"]["UA_CODE_ROOT"] == TEMPLATE_LINK, plist.name
    assert checked >= 10


def test_node_template_runs_its_own_release_link_not_the_hub_link():
    """#2298: the node runs ~/.unified_agent/release/current, never ua-current."""
    data = _read(PLIST_DIR / "com.unified-agent.node.plist")
    node_link = "__HOME__/.unified_agent/release/current"
    assert data["ProgramArguments"][0] == f"{node_link}/.venv/bin/unified-agent"
    assert data["EnvironmentVariables"]["UA_CODE_ROOT"] == node_link
    assert TEMPLATE_LINK not in (PLIST_DIR / "com.unified-agent.node.plist").read_text()


def test_repoint_never_moves_a_node_on_its_release_link(home: Path):
    """--to-link/--to-legacy touch ~/dev paths only; the node's link is its own."""
    node_link = f"{home}/.unified_agent/release/current"
    path = home / "Library/LaunchAgents/com.unified-agent.node.plist"
    _write(
        path,
        {
            "Label": path.stem,
            "ProgramArguments": [f"{node_link}/.venv/bin/unified-agent", "node"],
            "EnvironmentVariables": {"UA_CODE_ROOT": node_link},
        },
    )
    for to_link in (True, False):
        old, new = (
            (f"{home}/dev/unified_agent", f"{home}/dev/ua-current")
            if to_link
            else (f"{home}/dev/ua-current", f"{home}/dev/unified_agent")
        )
        assert repoint.plan_plist(path, old_root=old, new_root=new, set_env=to_link) is None


def test_wrappers_resolve_the_code_root_once():
    """A wrapper started through the link must pin one physical release."""
    for script in sorted(PLIST_DIR.glob("run-*.sh")):
        text = script.read_text()
        if "UA_CODE_ROOT=" not in text:
            continue
        assert 'pwd -P)"; fi' in text, script.name
        assert "export UA_CODE_ROOT" in text, script.name


def test_wrapper_pins_the_link_target(tmp_path: Path):
    """run-hub.sh execs the venv of the release the link names at start."""
    release = tmp_path / "ua-releases" / "abc"
    (release / ".venv" / "bin").mkdir(parents=True)
    probe = release / ".venv" / "bin" / "unified-agent"
    probe.write_text('#!/bin/sh\necho "root=$UA_CODE_ROOT"\n')
    probe.chmod(0o755)
    link = tmp_path / "ua-current"
    link.symlink_to(release)
    result = subprocess.run(
        ["bash", str(PLIST_DIR / "run-hub.sh")],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "UA_CODE_ROOT": str(link)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"root={release.resolve()}"


def test_chronicle_pins_before_env_source_swaps_link(tmp_path: Path):
    first = tmp_path / "release-one"
    second = tmp_path / "release-two"
    link = tmp_path / "ua-current"
    for release in (first, second):
        (release / "deploy/mac-hub").mkdir(parents=True)
        (release / "deploy/mac-hub/chronicle_sync.py").write_text(
            'import os\nfrom pathlib import Path\nprint(Path(__file__).resolve())\nprint(os.environ["UA_CODE_ROOT"])\n'
        )
    link.symlink_to(first)
    env_dir = tmp_path / ".unified_agent"
    env_dir.mkdir()
    (env_dir / "service.env").write_text(f'rm "{link}"\nln -s "{second}" "{link}"\n')
    result = subprocess.run(
        ["bash", str(PLIST_DIR / "run-gbrain-chronicle.sh")],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "UA_CODE_ROOT": str(link)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(first / "deploy/mac-hub/chronicle_sync.py"),
        str(first),
    ]


@pytest.mark.parametrize(
    "script,probe",
    [
        ("run-weekly-export.sh", "deploy/weekly-export-cron.sh"),
        ("run-database-backup.sh", ".venv/bin/python"),
        ("run-audit-ledger-reanchor.sh", ".venv/bin/python"),
    ],
)
def test_calendar_job_waits_and_runs_after_deploy(tmp_path: Path, script, probe):
    flag = tmp_path / ".unified_agent/DEPLOY_IN_PROGRESS"
    flag.parent.mkdir()
    flag.touch()
    bins = tmp_path / "bin"
    bins.mkdir()
    sleep = bins / "sleep"
    link = tmp_path / "ua-current"
    link.symlink_to(tmp_path / "release")
    other = tmp_path / "other-release"
    other.mkdir()
    sleep.write_text(f'#!/bin/sh\nrm "{flag}" "{link}"\nln -s "{other}" "{link}"\n')
    sleep.chmod(0o755)
    target = tmp_path / "release" / probe
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\necho scheduled-job-ran\n")
    target.chmod(0o755)
    result = subprocess.run(
        ["bash", str(PLIST_DIR / script)],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": f"{bins}:/usr/bin:/bin",
            "UA_CODE_ROOT": str(link),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "scheduled-job-ran" in result.stdout
    assert "waiting" in result.stderr


def test_timer_wrapper_skips_during_deploy(tmp_path: Path):
    flag = tmp_path / ".unified_agent" / "DEPLOY_IN_PROGRESS"
    flag.parent.mkdir()
    flag.write_text("")
    result = subprocess.run(
        ["bash", str(PLIST_DIR / "run-dispatch-tick.sh")],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0
    assert "deploy in progress" in result.stderr


def test_setup_script_keeps_jobs_on_the_checkout_until_the_link_exists():
    """Installing link templates before the first atomic deploy would break every job."""
    text = (PLIST_DIR / "setup-agents-and-executors.sh").read_text()
    guard = text.index('if [[ ! -L "$HOME/dev/ua-current" ||')
    assert 'repoint_launchagents.py" --to-legacy --apply' in text[guard:]
    # The guard runs after every template copy and before any bootstrap.
    assert guard > text.rindex('render_launchagent.py" --home "$MAC_HOME"')
    assert guard < text.index('launchctl bootstrap "gui/$(id -u)" "$LA/${label}.plist"')


def test_migration_keeps_first_hub_boot_on_checkout():
    text = (PLIST_DIR / "migrate-hub-to-mac.sh").read_text()
    guard = text.index('if [[ ! -L "$HOME/dev/ua-current" ||')
    assert 'repoint_launchagents.py" --to-legacy --apply' in text[guard:]
    assert guard > text.index('"$HOME/Library/LaunchAgents/com.unified-agent.hub.plist"')
    assert guard < text.index('launchctl bootstrap "gui/$(id -u)"')
    assert 'scp "$SCRIPT_DIR/repoint_launchagents.py"' in text
    assert '"$SCRIPT_DIR/render_launchagent.py"' in text
