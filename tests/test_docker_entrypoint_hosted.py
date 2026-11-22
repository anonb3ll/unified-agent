"""Hosted (Railway) behaviour of the container entrypoint (task #2108).

Runs the real ``deploy/docker/entrypoint.sh`` under bash with a stub
``unified-agent`` on PATH and the command passthrough, so the resulting hub
environment can be inspected without a docker daemon.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
ENTRYPOINT = REPO_ROOT / "deploy" / "docker" / "entrypoint.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"

AGENT_TOKEN = "stub-agent-token-0123456789"
OPERATOR_TOKEN = "stub-operator-token-9876543210"

# Mimics `unified-agent init`: writes the template-default service.env (stdio,
# auth off, generated tokens) and prints the JSON summary.
_STUB = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if not args:  # the hub start: report the environment it would run with
    print("ENVJSON" + json.dumps(dict(os.environ)))
    sys.exit(0)
if args[:1] != ["init"]:
    sys.exit("stub only implements init")
home = os.environ["UA_HOME"]
os.makedirs(home, exist_ok=True)
path = os.path.join(home, "service.env")
with open(os.path.join(home, "init-seen.json"), "w") as seen:
    json.dump({{
        "WORKOS_ORGANIZATION_ID": os.environ.get("WORKOS_ORGANIZATION_ID"),
        "UA_HUB_WORKOS_ORGANIZATION_ID": os.environ.get("UA_HUB_WORKOS_ORGANIZATION_ID"),
    }}, seen)
with open(path, "w") as fh:
    fh.write("UA_TRANSPORT=stdio\\nUA_ENFORCE_AUTH=0\\n")
    fh.write("UA_AGENT_TOKENS=stub_agent:{AGENT_TOKEN},Desktop_Op:{OPERATOR_TOKEN}\\n")
os.chmod(path, 0o600)
out = {{"token_label": "stub_agent"}}
if "--print-token" in args:
    out["token"] = "{AGENT_TOKEN}"
print(json.dumps(out))
"""

_DUMP = "import json, os; print('ENVJSON' + json.dumps(dict(os.environ)))"


@pytest.fixture
def stub_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "unified-agent"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    return bin_dir


def _run(stub_bin, home: Path, extra_env: dict[str, str]):
    env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home.parent),
        "UA_HOME": str(home),
        # Dockerfile ENV defaults.
        "UA_WORKSPACE": str(home / "workspace"),
        "UA_TRANSPORT": "http",
        "UA_HTTP_HOST": "0.0.0.0",
        "UA_HTTP_PORT": "8765",
        "UA_ENFORCE_AUTH": "1",
        **extra_env,
    }
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT), sys.executable, "-c", _DUMP],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    hub_env = None
    logged = []
    for line in proc.stdout.splitlines():
        if line.startswith("ENVJSON"):
            hub_env = json.loads(line[len("ENVJSON") :])
        else:
            logged.append(line)
    # What the platform log would retain: everything the entrypoint itself wrote.
    proc.logged = "\n".join(logged) + "\n" + proc.stderr
    return proc, hub_env


def _railway(tmp_path, **extra):
    mount = tmp_path / "vol"
    env = {
        "RAILWAY_ENVIRONMENT_ID": "env-123",
        "RAILWAY_VOLUME_MOUNT_PATH": str(mount),
        "RAILWAY_PUBLIC_DOMAIN": "ua-hub.up.railway.app",
        "RAILWAY_PRIVATE_DOMAIN": "ua-hub.railway.internal",
        "PORT": "9123",
    }
    env.update(extra)
    return mount, env


def test_railway_start_never_prints_the_bootstrap_token(tmp_path, stub_bin):
    mount, env = _railway(tmp_path)
    proc, hub_env = _run(stub_bin, mount, env)
    assert proc.returncode == 0, proc.stderr
    assert AGENT_TOKEN not in proc.logged
    assert OPERATOR_TOKEN not in proc.logged
    assert "service.env" in proc.stdout
    # The tokens are still provisioned, on the volume, private to the owner.
    service_env = mount / "service.env"
    assert AGENT_TOKEN in service_env.read_text()
    assert service_env.stat().st_mode & 0o077 == 0
    assert hub_env["UA_AGENT_TOKENS"].endswith(OPERATOR_TOKEN)


def test_railway_start_binds_port_and_allows_platform_hosts(tmp_path, stub_bin):
    mount, env = _railway(tmp_path)
    proc, hub_env = _run(stub_bin, mount, env)
    assert proc.returncode == 0, proc.stderr
    assert hub_env["UA_HTTP_PORT"] == "9123"
    assert hub_env["UA_HTTP_HOST"] == "0.0.0.0"
    assert hub_env["UA_TRANSPORT"] == "http"
    assert hub_env["UA_ENFORCE_AUTH"] == "1"
    assert hub_env["UA_PUBLIC_URL"] == "https://ua-hub.up.railway.app"
    assert hub_env["UA_HOSTED"] == "1"
    allowed = hub_env["UA_HTTP_ALLOWED_HOSTS"].split(",")
    for host in (
        "127.0.0.1:*",
        "ua-hub.up.railway.app",
        "ua-hub.up.railway.app:*",
        "ua-hub.railway.internal:*",
        "healthcheck.railway.app",
    ):
        assert host in allowed
    # The public domain serves the Control Center too, so it must not land in
    # UA_HTTP_PUBLIC_HOSTS (which restricts a Host to MCP and OAuth only).
    assert "UA_HTTP_PUBLIC_HOSTS" not in hub_env


def test_platform_variables_outrank_the_volume_service_env(tmp_path, stub_bin):
    mount, env = _railway(
        tmp_path,
        UA_AGENT_TOKENS="Desktop_Person:injected-sealed-value",
        UA_PUBLIC_URL="https://hub.example.com",
        UA_APPROVAL_TOOLS="delegate",
    )
    proc, hub_env = _run(stub_bin, mount, env)
    assert proc.returncode == 0, proc.stderr
    assert hub_env["UA_AGENT_TOKENS"] == "Desktop_Person:injected-sealed-value"
    assert hub_env["UA_PUBLIC_URL"] == "https://hub.example.com"
    assert hub_env["UA_APPROVAL_TOOLS"] == "delegate"
    assert "injected UA_AGENT_TOKENS" in proc.stdout
    assert "injected-sealed-value" not in proc.logged


def test_platform_variables_win_on_a_later_start_too(tmp_path, stub_bin):
    mount, env = _railway(tmp_path)
    first, _ = _run(stub_bin, mount, env)
    assert first.returncode == 0, first.stderr
    env["UA_AGENT_TOKENS"] = "Desktop_Person:rotated"
    second, hub_env = _run(stub_bin, mount, env)
    assert second.returncode == 0, second.stderr
    assert hub_env["UA_AGENT_TOKENS"] == "Desktop_Person:rotated"
    assert "bootstrap" not in second.stdout


def _dockerfile_default(name: str) -> str:
    """The value of one ``ENV name=value`` line in the image Dockerfile."""
    match = re.search(rf"^[ \t]*{re.escape(name)}=(\S+)", DOCKERFILE.read_text(), re.M)
    assert match is not None, name
    return match.group(1)


def test_baked_image_defaults_do_not_override_service_env_or_runtime(tmp_path, stub_bin):
    """Image ENV yields to service.env, and a real runtime value stays (F-03).

    Hosted start snapshots the process environment and evals it after sourcing
    service.env. A key still at its Dockerfile default must keep the service.env
    value. A runtime value of a defaulted key must not be replaced by that default.
    """
    edition = _dockerfile_default("UA_EDITION")
    workspace = _dockerfile_default("UA_WORKSPACE")
    service_edition = f"{edition}-from-service"
    runtime_workspace = f"{workspace}-runtime"
    mount, env = _railway(tmp_path, UA_EDITION=edition, UA_WORKSPACE=runtime_workspace)
    mount.mkdir()
    (mount / "service.env").write_text(f"UA_EDITION={service_edition}\n")
    proc, hub_env = _run(stub_bin, mount, env)
    assert proc.returncode == 0, proc.stderr
    assert hub_env is not None
    assert {
        "UA_EDITION": hub_env["UA_EDITION"],
        "UA_WORKSPACE": hub_env["UA_WORKSPACE"],
    } == {
        "UA_EDITION": service_edition,
        "UA_WORKSPACE": runtime_workspace,
    }


def test_entrypoint_default_table_matches_the_dockerfile_env():
    """The entrypoint's copy of the image defaults must track the Dockerfile.

    A default added to or changed in the Dockerfile but not here would be
    snapshotted as a platform override and shadow service.env again (F-03).
    """
    env_block = re.search(r"^ENV (.*?)(?:\n\n|\Z)", DOCKERFILE.read_text(), re.M | re.S)
    assert env_block is not None
    image = dict(re.findall(r"([A-Z_][A-Z0-9_]*)=(\S+?)\s*(?:\\|$)", env_block.group(1), re.M))
    image.pop("PATH")
    table = re.search(r"^defaults = \{\n(.*?)^\}", ENTRYPOINT.read_text(), re.M | re.S)
    assert table is not None
    copied = dict(re.findall(r'^\s*"([A-Z_][A-Z0-9_]*)": "([^"]*)",$', table.group(1), re.M))
    assert copied == image


def test_railway_without_a_volume_refuses_to_start(tmp_path, stub_bin):
    home = tmp_path / "ephemeral"
    proc, hub_env = _run(stub_bin, home, {"RAILWAY_ENVIRONMENT_ID": "env-123", "PORT": "9123"})
    assert proc.returncode == 64
    assert hub_env is None
    assert "no Railway volume" in proc.stderr
    assert not (home / "service.env").exists()


def test_ephemeral_override_allows_a_throwaway_start(tmp_path, stub_bin):
    home = tmp_path / "ephemeral"
    proc, hub_env = _run(
        stub_bin,
        home,
        {"RAILWAY_ENVIRONMENT_ID": "env-123", "UA_ALLOW_EPHEMERAL_STATE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert hub_env is not None


def test_ua_home_off_the_volume_refuses_to_start(tmp_path, stub_bin):
    _, env = _railway(tmp_path)
    home = tmp_path / "elsewhere"
    proc, hub_env = _run(stub_bin, home, env)
    assert proc.returncode == 64
    assert hub_env is None
    assert "not on the" in proc.stderr
    assert not (home / "service.env").exists()


def test_hosted_ignores_the_insecure_escape_hatch(tmp_path, stub_bin):
    mount, env = _railway(tmp_path, UA_DOCKER_ALLOW_INSECURE="1")
    proc, hub_env = _run(stub_bin, mount, env)
    assert proc.returncode == 0, proc.stderr
    assert hub_env["UA_ENFORCE_AUTH"] == "1"


def test_ua_hosted_flag_applies_the_hosted_path_off_railway(tmp_path, stub_bin):
    home = tmp_path / "data"
    proc, hub_env = _run(stub_bin, home, {"UA_HOSTED": "1", "PORT": "7000"})
    assert proc.returncode == 0, proc.stderr
    assert AGENT_TOKEN not in proc.logged
    assert hub_env["UA_HTTP_PORT"] == "7000"
    assert "healthcheck.railway.app" not in hub_env["UA_HTTP_ALLOWED_HOSTS"]


def test_self_hosted_docker_behaviour_is_unchanged(tmp_path, stub_bin):
    """compose/docker run: token shown once; service.env still wins; PORT ignored."""
    home = tmp_path / "data"
    proc, hub_env = _run(stub_bin, home, {"PORT": "9999", "UA_AGENT_TOKENS": "x:y"})
    assert proc.returncode == 0, proc.stderr
    assert f"token: {AGENT_TOKEN}" in proc.stdout
    assert hub_env["UA_HTTP_PORT"] == "8765"
    assert hub_env["UA_AGENT_TOKENS"].startswith("stub_agent:")
    assert hub_env["UA_HTTP_ALLOWED_HOSTS"] == "127.0.0.1:*,localhost:*"
    assert hub_env.get("UA_HOSTED") != "1"


def test_hosted_entrypoint_gives_every_process_the_workos_tenant(tmp_path, stub_bin):
    """Init and the hub both stamp UA_HUB_WORKOS_ORGANIZATION_ID.

    `unified-agent init` opens the store before service.env exists. Without
    the copy, that process's beacon says no tenant while the hub, which
    learned the org later, cannot see the rows init wrote.
    """
    org = "org_01HOSTEDTENANT"
    mount, env = _railway(tmp_path, UA_HUB_WORKOS_ORGANIZATION_ID=org)
    proc, hub_env = _run(stub_bin, mount, env)
    assert proc.returncode == 0, proc.stderr
    assert hub_env["WORKOS_ORGANIZATION_ID"] == org
    assert hub_env["UA_HUB_WORKOS_ORGANIZATION_ID"] == org
    seen = json.loads((mount / "init-seen.json").read_text())
    assert seen["WORKOS_ORGANIZATION_ID"] == org
    assert seen["UA_HUB_WORKOS_ORGANIZATION_ID"] == org


def test_hosted_platform_org_beats_a_blank_service_env_org(tmp_path, stub_bin):
    mount, env = _railway(tmp_path, UA_HUB_WORKOS_ORGANIZATION_ID="org_01PLATFORM")
    first, _ = _run(stub_bin, mount, env)
    assert first.returncode == 0, first.stderr
    service = mount / "service.env"
    service.write_text(service.read_text() + 'WORKOS_ORGANIZATION_ID=""\n')
    second, hub_env = _run(stub_bin, mount, env)
    assert second.returncode == 0, second.stderr
    assert hub_env["WORKOS_ORGANIZATION_ID"] == "org_01PLATFORM"


def test_explicit_platform_workos_org_is_not_replaced(tmp_path, stub_bin):
    mount, env = _railway(
        tmp_path,
        WORKOS_ORGANIZATION_ID="org_01EXPLICIT",
        UA_HUB_WORKOS_ORGANIZATION_ID="org_01HOSTED",
    )
    proc, hub_env = _run(stub_bin, mount, env)
    assert proc.returncode == 0, proc.stderr
    assert hub_env["WORKOS_ORGANIZATION_ID"] == "org_01EXPLICIT"
    seen = json.loads((mount / "init-seen.json").read_text())
    assert seen["WORKOS_ORGANIZATION_ID"] == "org_01EXPLICIT"


def test_self_hosted_does_not_copy_the_hosted_org(tmp_path, stub_bin):
    home = tmp_path / "data"
    proc, hub_env = _run(stub_bin, home, {"UA_HUB_WORKOS_ORGANIZATION_ID": "org_01HOSTED"})
    assert proc.returncode == 0, proc.stderr
    assert "WORKOS_ORGANIZATION_ID" not in hub_env


def test_dockerfile_has_no_volume_instruction():
    """Railway rejects VOLUME (task #1441's failed builds)."""
    assert not re.search(r"^\s*VOLUME\b", DOCKERFILE.read_text(), re.M)


def test_root_start_drops_privileges_before_anything_else():
    """RAILWAY_RUN_UID=0 starts us as root; the hub must never run as root."""
    text = ENTRYPOINT.read_text()
    root_branch = text.find('if [ "$(id -u)" = "0" ]')
    drop = text.find("os.setuid(uid)", root_branch)
    first_init = text.find("$(unified-agent init")
    assert root_branch != -1 and drop != -1
    assert drop < first_init
    assert "privilege drop failed" in text


# --- Host-neutral VM start (task #2148): same image, no Railway variables ----

_MOUNTINFO = Path("/proc/self/mountinfo")
_NEEDS_MOUNTINFO = pytest.mark.skipif(
    not _MOUNTINFO.is_file(), reason="needs Linux /proc/self/mountinfo"
)


def _mount_points() -> list[str]:
    points = []
    for line in _MOUNTINFO.read_text().splitlines():
        point = line.split()[4]
        points.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), point))
    return points


def _mount_of(path: Path) -> str:
    """The deepest mount point holding ``path``: what the entrypoint must accept."""
    real = os.path.realpath(path).rstrip("/") + "/"
    return max(
        (p for p in _mount_points() if real.startswith(p.rstrip("/") + "/")),
        key=len,
    )


def _vm(mount: str, **extra):
    env = {
        "UA_HOSTED": "1",
        "UA_EXECUTION_LOCALITY": "devices",
        "UA_STATE_MOUNT_PATH": mount,
        "UA_PUBLIC_URL": "https://hub-host.tailnet.example",
        "UA_PUBLIC_HOSTS": "hub-host.tailnet.example,hub-host.tailnet.example:*",
    }
    env.update(extra)
    return env


@_NEEDS_MOUNTINFO
def test_vm_start_uses_the_hosted_path_without_railway_variables(tmp_path, stub_bin):
    home = tmp_path / "data"
    proc, hub_env = _run(stub_bin, home, _vm(_mount_of(tmp_path)))
    assert proc.returncode == 0, proc.stderr
    assert AGENT_TOKEN not in proc.logged and OPERATOR_TOKEN not in proc.logged
    assert hub_env["UA_HOSTED"] == "1"
    assert hub_env["UA_EXECUTION_LOCALITY"] == "devices"
    assert hub_env["UA_HTTP_PORT"] == "8765"
    assert hub_env["UA_ENFORCE_AUTH"] == "1"
    assert hub_env["UA_PUBLIC_URL"] == "https://hub-host.tailnet.example"
    allowed = hub_env["UA_HTTP_ALLOWED_HOSTS"].split(",")
    assert "hub-host.tailnet.example:*" in allowed and "127.0.0.1:*" in allowed
    assert "healthcheck.railway.app" not in allowed


@_NEEDS_MOUNTINFO
def test_state_mount_that_is_not_a_mount_point_refuses_to_start(tmp_path, stub_bin):
    mount = tmp_path / "not-mounted"
    home = mount / "data"
    proc, hub_env = _run(stub_bin, home, _vm(str(mount)))
    assert proc.returncode == 64
    assert hub_env is None
    assert "is not a mount point" in proc.stderr
    assert not (home / "service.env").exists()


def test_ua_home_outside_the_state_mount_refuses_to_start(tmp_path, stub_bin):
    home = tmp_path / "elsewhere"
    proc, hub_env = _run(stub_bin, home, _vm("/no-such-mount"))
    assert proc.returncode == 64
    assert hub_env is None
    assert "is not under" in proc.stderr
    assert not (home / "service.env").exists()


@_NEEDS_MOUNTINFO
def test_dotdot_out_of_the_state_mount_refuses_to_start(tmp_path, stub_bin):
    """A lexical prefix would accept <mount>/../<elsewhere>; the resolved path does not."""
    mount = _mount_of(tmp_path)
    if mount == "/":
        pytest.skip("tmp_path sits on the root mount; no sibling outside it")
    escape = f"{mount.rstrip('/')}/../ua-escape-{os.getpid()}"
    proc, hub_env = _run(stub_bin, Path(escape), _vm(mount))
    assert proc.returncode == 64
    assert hub_env is None
    assert "is not under" in proc.stderr


@_NEEDS_MOUNTINFO
def test_symlink_out_of_the_state_mount_refuses_to_start(tmp_path, stub_bin):
    if "/proc" not in _mount_points():
        pytest.skip("needs /proc mounted")
    # A symlink inside the claimed state mount that points off it. /proc is a
    # mount point the test cannot write to, so claim tmp_path's own mount and
    # point the link into /proc's filesystem instead.
    mount = _mount_of(tmp_path)
    link = tmp_path / "state-link"
    link.symlink_to("/proc/self")
    proc, hub_env = _run(stub_bin, link / "data", _vm(mount))
    assert proc.returncode == 64
    assert hub_env is None


@_NEEDS_MOUNTINFO
def test_nested_mount_inside_the_state_mount_refuses_to_start(tmp_path, stub_bin):
    """UA_HOME under the claimed mount but on another filesystem mounted inside it."""
    if "/proc" not in _mount_points():
        pytest.skip("needs /proc mounted")
    proc, hub_env = _run(stub_bin, Path("/proc/ua-state"), _vm("/"))
    assert proc.returncode == 64
    assert hub_env is None
    assert "is not a mount point holding UA_HOME" in proc.stderr
    assert "'/proc'" in proc.stderr


def test_state_mount_guard_honours_the_ephemeral_override(tmp_path, stub_bin):
    mount = tmp_path / "not-mounted"
    proc, hub_env = _run(
        stub_bin,
        mount / "data",
        _vm(str(mount), UA_ALLOW_EPHEMERAL_STATE="1"),
    )
    assert proc.returncode == 0, proc.stderr
    assert hub_env is not None


# Stands in for the backup schedule (task #2585): records that it started and
# under which environment, then exits.
_LOOP_STUB = f"""#!{sys.executable}
import json, os
with open(os.path.join(os.environ["UA_HOME"], "backup-loop-started.json"), "w") as fh:
    json.dump({{"interval": os.environ.get("UA_BACKUP_INTERVAL_S"),
                "transport": os.environ.get("UA_TRANSPORT")}}, fh)
"""


def _start_hub(stub_bin, home: Path, extra_env: dict[str, str]):
    loop = stub_bin / "unified-agent-backup-loop"
    loop.write_text(_LOOP_STUB)
    loop.chmod(0o755)
    env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home.parent),
        "UA_HOME": str(home),
        "UA_WORKSPACE": str(home / "workspace"),
        **extra_env,
    }
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT)], env=env, capture_output=True, text=True, timeout=60
    )
    marker = home / "backup-loop-started.json"
    for _ in range(50):  # the loop is started in the background
        if marker.exists() and marker.stat().st_size:
            break
        time.sleep(0.1)
    return proc, marker


def test_hosted_start_runs_the_backup_schedule_beside_the_hub(tmp_path, stub_bin):
    mount, env = _railway(tmp_path, UA_BACKUP_INTERVAL_S="86400", UA_ALLOW_EPHEMERAL_STATE="1")
    proc, marker = _start_hub(stub_bin, mount, env)
    assert proc.returncode == 0, proc.stderr
    assert "ENVJSON" in proc.stdout
    assert json.loads(marker.read_text()) == {"interval": "86400", "transport": "http"}


@pytest.mark.parametrize("hosted", [True, False])
def test_backup_schedule_needs_hosted_and_an_interval(tmp_path, stub_bin, hosted):
    if hosted:
        mount, env = _railway(tmp_path, UA_ALLOW_EPHEMERAL_STATE="1")
    else:
        mount, env = tmp_path / "home", {"UA_BACKUP_INTERVAL_S": "86400"}
    proc, marker = _start_hub(stub_bin, mount, env)
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
