"""GCP control-plane stand-up scripts (task #2148).

bootstrap.sh runs against a stub ``gcloud`` that records every call and
reports no existing resources, so the dry-run plan can be checked without a
GCP project, network access, or credentials.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
CP = REPO_ROOT / "deploy" / "gcp" / "control-plane"
BOOTSTRAP = CP / "bootstrap.sh"
# A planted marker that must never reach output (not a credential).
PLANTED = "planted-marker-7f3a9c"

_STUB_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GCLOUD_LOG"
case "$*" in
  *" describe "*)
    if [[ -n "${GCLOUD_EXISTS:-}" && "$*" == *"$GCLOUD_EXISTS"* ]]; then exit 0; fi
    exit 1 ;;
  "config get-value project") exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gcloud"
    stub.write_text(_STUB_GCLOUD)
    stub.chmod(0o755)
    hub_env = tmp_path / "hub.env"
    hub_env.write_text(f"UA_AGENT_TOKENS=Desktop_Op:{PLANTED}\nWORKOS_API_KEY=\n")
    hub_env.chmod(0o600)
    authkey = tmp_path / "ts-key"
    authkey.write_text(f"tskey-{PLANTED}\n")
    authkey.chmod(0o600)
    return {
        "tmp": tmp_path,
        "hub_env": hub_env,
        "authkey": authkey,
        "log": tmp_path / "gcloud.log",
        "vars": {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(tmp_path),
            "GCLOUD_LOG": str(tmp_path / "gcloud.log"),
            "UA_GCP_PROJECT": "example-project",
        },
    }


def _bootstrap(env, *args):
    return subprocess.run(
        ["bash", str(BOOTSTRAP), *args],
        env=env["vars"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )


def _full(env, *extra):
    return _bootstrap(
        env,
        "--ref=HEAD",
        f"--env-file={env['hub_env']}",
        f"--tailscale-authkey-file={env['authkey']}",
        "--backup-gcs-uri=gs://example-bucket/memory-db/ua-control-plane",
        *extra,
    )


def test_dry_run_is_the_default_and_only_reads(env):
    proc = _full(env)
    assert proc.returncode == 0, proc.stderr
    calls = env["log"].read_text().splitlines()
    assert calls, "the plan should describe existing resources"
    for call in calls:
        assert " describe " in f" {call} ", f"dry-run made a non-read gcloud call: {call}"
    assert "dry-run only; nothing was created" in proc.stdout


def test_dry_run_plan_matches_the_task_spec(env):
    out = _full(env).stdout
    create = next(line for line in out.splitlines() if "compute instances create" in line)
    assert "--zone=us-central1-a" in create
    assert "--machine-type=e2-medium" in create
    assert "no-address" in create, "no external IP"
    assert "device-name=ua-data" in create and "auto-delete=no" in create
    assert "--deletion-protection" in create
    assert "--tags=tailscale,ua-control-plane" in create
    disk = next(line for line in out.splitlines() if "compute disks create" in line)
    assert "--type=pd-standard" in disk and "--size=64GB" in disk
    assert "--resource-policies=ua-control-plane-daily" in disk
    policy = next(line for line in out.splitlines() if "snapshot-schedule" in line)
    assert "--region=us-central1" in policy and "--daily-schedule" in policy
    # Firewall: only Tailscale UDP and IAP SSH, never an app or MCP port.
    rules = [line for line in out.splitlines() if "firewall-rules create" in line]
    assert {re.search(r"--rules=(\S+)", r).group(1) for r in rules} == {"udp:41641", "tcp:22"}
    assert not re.search(r"tcp:(8765|443|80)\b", out)
    # Automation SSH: IAP tunnel, file key, no interactive agent.
    assert "--tunnel-through-iap" in out and "IdentityAgent=none" in out
    assert "install.sh --apply" in out and "verify.sh --restart" in out
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert f"--vcs-ref={head}" in out


def test_secrets_are_never_printed(env):
    proc = _full(env)
    assert proc.returncode == 0, proc.stderr
    assert PLANTED not in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "line",
    ["UA_EXECUTION_LOCALITY=local", "UA_HOSTED=0", "UA_STATE_MOUNT_PATH=/tmp", "RAILWAY_X=1"],
)
def test_env_file_cannot_override_pinned_keys(env, line):
    env["hub_env"].write_text(f"{line}\n")
    proc = _full(env)
    assert proc.returncode == 1
    assert "pins" in proc.stderr
    assert not env["log"].exists(), "refused before any gcloud call"


def test_secret_files_must_be_private(env):
    env["hub_env"].chmod(0o644)
    proc = _full(env)
    assert proc.returncode == 1
    assert "0600" in proc.stderr


@pytest.mark.parametrize(
    "arg",
    ["--data-disk-gb=30", "--data-disk-gb=200", "--machine-type=e2-small", "--zone=us-east1-b"],
)
def test_sizing_and_region_are_enforced(env, arg):
    proc = _full(env, arg)
    assert proc.returncode == 1, proc.stdout


def test_apply_requires_an_artifact_and_env_file(env):
    proc = _bootstrap(env, "--apply")
    assert proc.returncode == 1
    assert "--ref" in proc.stderr
    assert not env["log"].exists()


def test_unit_pins_the_hosted_posture_and_loopback_publish():
    unit = (CP / "ua-hub.service").read_text()
    for pinned in (
        "-e UA_HOSTED=1",
        "-e UA_EXECUTION_LOCALITY=devices",
        "-e UA_STATE_MOUNT_PATH=/data",
        "-v /mnt/ua-data:/data",
        "-p 127.0.0.1:8765:8765",
        "RequiresMountsFor=/mnt/ua-data",
    ):
        assert pinned in unit
    # Pinned values come after both env files, so neither can override them.
    assert unit.index("--env-file /etc/unified-agent/hub.env") < unit.index("-e UA_HOSTED=1")


def _service_directives(name: str) -> list[tuple[str, str]]:
    """The [Service] section of a unit as ordered (key, value) pairs.

    Backslash continuations are joined and comments dropped, so repeated keys
    (ExecStartPre) keep the order systemd runs them in.
    """
    logical, buf = [], ""
    for raw in (CP / name).read_text().splitlines():
        if not buf and raw.lstrip().startswith("#"):
            continue
        if raw.endswith("\\"):
            buf += raw[:-1] + " "
            continue
        logical.append(buf + raw)
        buf = ""
    section, pairs = "", []
    for line in logical:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "Service" and "=" in line:
            key, value = line.split("=", 1)
            pairs.append((key.strip(), " ".join(value.split())))
    return pairs


# Hardening for units whose only processes are the docker CLI (or backup.sh and
# gcloud) as root. The container itself is created by dockerd, outside the
# unit's namespaces, so none of these reach the hub process.
HARDENING = {
    "NoNewPrivileges": "yes",
    "PrivateTmp": "yes",
    "ProtectSystem": "full",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectControlGroups": "yes",
    "RestrictSUIDSGID": "yes",
}


@pytest.mark.parametrize("unit", ["ua-hub.service", "ua-hub-backup.service"])
def test_units_are_hardened_without_cutting_docker_or_the_data_disk(unit):
    directives = dict(_service_directives(unit))
    for key, value in HARDENING.items():
        assert directives.get(key) == value, f"{unit}: {key}={directives.get(key)}"
    # strict would make /mnt/ua-data and /root read-only for the unit; full
    # leaves them alone and protects /usr, /boot, /efi and /etc.
    assert directives["ProtectSystem"] != "strict"
    # The docker CLI may run a registry credential helper (gcloud) that writes
    # /root/.config/gcloud, and backup.sh's gcloud upload does too.
    assert "ProtectHome" not in directives
    # Nothing may hide /run/docker.sock or the data disk, or forbid AF_UNIX.
    for key in ("InaccessiblePaths", "ReadOnlyPaths", "TemporaryFileSystem"):
        assert not re.search(r"(^|\s|-)/(run|mnt)(/|\s|$)", directives.get(key, "")), key
    families = directives.get("RestrictAddressFamilies")
    assert families is None or "AF_UNIX" in families
    assert "PrivateNetwork" not in directives and "User" not in directives


def test_hub_unit_drains_a_running_container_before_replacing_it():
    pairs = _service_directives("ua-hub.service")
    pre = [value for key, value in pairs if key == "ExecStartPre"]
    directives = dict(pairs)
    # Mount check first, then a graceful stop, then remove whatever is left.
    assert pre[0] == "/usr/bin/mountpoint -q /mnt/ua-data"
    stop = re.fullmatch(r"-/usr/bin/docker stop -t (\d+) ua-hub", pre[1])
    assert stop, pre
    assert pre[2] == "-/usr/bin/docker rm ua-hub"
    assert len(pre) == 3
    # No SIGKILL-first path is left anywhere in the unit.
    assert not any("rm -f" in value for _, value in pairs)
    grace = int(stop.group(1))
    exec_stop = re.fullmatch(r"/usr/bin/docker stop -t (\d+) ua-hub", directives["ExecStop"])
    assert exec_stop and int(exec_stop.group(1)) == grace, "one grace period for both stops"
    # The grace must cover the hub's own bounded uvicorn drain, and systemd's
    # timeouts must cover the grace, or systemd kills the docker CLI mid-stop.
    from unified_agent.config import HTTP_GRACEFUL_SHUTDOWN_MAX_S

    assert grace > HTTP_GRACEFUL_SHUTDOWN_MAX_S
    assert int(directives["TimeoutStopSec"]) > grace
    assert int(directives["TimeoutStartSec"]) > grace


def test_install_exposes_only_through_tailscale_serve():
    text = (CP / "install.sh").read_text()
    assert "tailscale serve --bg" in text
    assert "funnel" not in text.replace("Never Funnel", "").lower()
    lib = (CP / "install-lib.sh").read_text()
    # The disk and secret-file steps live in the tested library, not inline.
    assert "prepare_data_disk" in text and "install_hub_env" in text
    assert "mkfs" not in text and "blkid" not in text
    assert "blkid -p" in lib and lib.index("blkid -p") < lib.index("mkfs.ext4")
    # The auth key reaches tailscale as a file, never as an argument.
    assert "--auth-key=file:" in text


def test_install_and_bootstrap_refuse_the_same_keys():
    install = (CP / "install.sh").read_text()
    keys = re.search(r"FORBIDDEN_KEYS=\(([^)]*)\)", install).group(1).split()
    bootstrap = BOOTSTRAP.read_text()
    pattern = re.search(r"grep -oE '\^\(([^)]*)\)=\.'", bootstrap).group(1).split("|")
    assert set(keys) == set(pattern) - {"RAILWAY_[A-Z_]*"}


def test_env_template_carries_no_values():
    for line in (CP / "hub.env.template").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            assert re.fullmatch(r"[A-Z_][A-Z0-9_]*=", line), line


@pytest.mark.parametrize("script", sorted(p.name for p in CP.iterdir() if p.is_file()))
def test_control_plane_files_hold_no_host_specific_values(script):
    text = (CP / script).read_text()
    assert not re.search(r"/(?:home|Users)/[A-Za-z0-9_.-]+/", text), script
    assert not re.search(r"\b100\.\d+\.\d+\.\d+\b", text), script
    assert ".ts.net" not in text.replace("<tailnet>.ts.net", ""), script
    if script.endswith(".sh"):
        assert subprocess.run(["bash", "-n", str(CP / script)]).returncode == 0


def test_the_run_that_creates_the_disk_may_initialise_it(env):
    out = _full(env).stdout
    assert "--init-data-disk" in out.split("install.sh --apply", 1)[1].splitlines()[0]


def test_an_existing_disk_is_never_initialised(env):
    env["vars"]["GCLOUD_EXISTS"] = "disks describe ua-control-plane-data"
    out = _full(env).stdout
    install_line = out.split("install.sh --apply", 1)[1].splitlines()[0]
    assert "--init-data-disk" not in install_line
    assert "compute disks create" not in out


def test_local_only_backup_is_passed_explicitly(env):
    proc = _bootstrap(
        env,
        "--ref=HEAD",
        f"--env-file={env['hub_env']}",
        "--backup-gcs-uri=none",
    )
    assert proc.returncode == 0, proc.stderr
    assert "--backup-gcs-uri=none" in proc.stdout


# --- install-lib.sh: the steps that can destroy or leak state ---------------

LIB = CP / "install-lib.sh"
DEVICE_TOKEN = "ua-data-device"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f'#!/usr/bin/env bash\nprintf "%s %s\\n" "{name}" "$*" >> "$CALLS"\n{body}\n')
    path.chmod(0o755)


@pytest.fixture
def lib(tmp_path):
    bin_dir = tmp_path / "lib-bin"
    bin_dir.mkdir()
    # blkid: the UUID read succeeds; the probe exits $BLKID_RC printing $BLKID_TYPE.
    _stub(
        bin_dir,
        "blkid",
        'if [[ "$*" == *UUID* ]]; then echo 1111-2222; exit 0; fi\n'
        '[[ -n "${BLKID_TYPE:-}" ]] && echo "$BLKID_TYPE"\nexit "${BLKID_RC:-2}"',
    )
    _stub(bin_dir, "wipefs", 'printf "%s" "${WIPEFS_OUT:-}"\nexit "${WIPEFS_RC:-0}"')
    _stub(bin_dir, "lsblk", 'printf "%b" "${LSBLK_OUT:-disk \\n}"\nexit "${LSBLK_RC:-0}"')
    for name in ("mkfs.ext4", "mount", "chown", "chmod"):
        _stub(bin_dir, name, "exit 0")
    _stub(bin_dir, "mountpoint", "exit 1")
    _stub(bin_dir, "gcloud", 'exit "${GCLOUD_RC:-0}"')
    device = tmp_path / DEVICE_TOKEN
    device.write_text("")
    etc = tmp_path / "etc"
    etc.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    fstab = tmp_path / "fstab"
    fstab.write_text("")
    calls = tmp_path / "calls.log"

    def call(function: str, **env_extra: str):
        setup = (
            "set -euo pipefail\n"
            f". {LIB}\n"
            "APPLY=1 HUB_UID=10001 GCP_PROJECT=example-project\n"
            f'DATA_DEVICE="{device}" DATA_MOUNT="{tmp_path / "mnt"}" FSTAB="{fstab}"\n'
            f'ETC_DIR="{etc}" STAGE="{stage}"\n'
            'INIT_DATA_DISK="${INIT_DATA_DISK:-0}" BACKUP_GCS_URI="${BACKUP_GCS_URI:-}"\n'
            "FORBIDDEN_KEYS=(UA_HOSTED UA_EXECUTION_LOCALITY UA_STATE_MOUNT_PATH)\n"
            f"{function}\n"
        )
        proc = subprocess.run(
            ["bash", "-c", setup],
            env={
                "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
                "CALLS": str(calls),
                **env_extra,
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        proc.calls = calls.read_text() if calls.exists() else ""
        calls.unlink(missing_ok=True)
        return proc

    return {"call": call, "etc": etc, "stage": stage, "fstab": fstab}


def test_a_provably_blank_disk_is_formatted_only_with_init(lib):
    proc = lib["call"]("prepare_data_disk", INIT_DATA_DISK="1")
    assert proc.returncode == 0, proc.stderr
    assert "mkfs.ext4" in proc.calls and DEVICE_TOKEN in proc.calls
    assert "UUID=1111-2222" in lib["fstab"].read_text()

    proc = lib["call"]("prepare_data_disk", INIT_DATA_DISK="0")
    assert proc.returncode != 0
    assert "--init-data-disk" in proc.stderr
    assert "mkfs" not in proc.calls


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param({"BLKID_RC": "1"}, id="blkid-error"),
        pytest.param({"BLKID_RC": "4"}, id="blkid-usage-error"),
        pytest.param({"BLKID_RC": "8"}, id="blkid-ambiguous"),
        pytest.param({"BLKID_RC": "0", "BLKID_TYPE": "xfs"}, id="foreign-filesystem"),
        pytest.param({"WIPEFS_OUT": "0x438 ext4 53ef"}, id="wipefs-finds-signature"),
        pytest.param({"WIPEFS_RC": "1"}, id="wipefs-error"),
        pytest.param({"LSBLK_OUT": "disk \\npart ext4\\n"}, id="partitions"),
        pytest.param({"LSBLK_OUT": "disk LVM2_member\\n"}, id="lsblk-fstype"),
        pytest.param({"LSBLK_RC": "32"}, id="lsblk-error"),
    ],
)
def test_an_error_or_ambiguous_probe_never_formats(lib, probe):
    proc = lib["call"]("prepare_data_disk", INIT_DATA_DISK="1", **probe)
    assert proc.returncode != 0
    assert "nothing was formatted" in proc.stderr
    assert "mkfs" not in proc.calls
    assert "mount" not in proc.calls


def test_an_existing_ext4_disk_is_mounted_not_formatted(lib):
    proc = lib["call"]("prepare_data_disk", INIT_DATA_DISK="1", BLKID_RC="0", BLKID_TYPE="ext4")
    assert proc.returncode == 0, proc.stderr
    assert "mkfs" not in proc.calls
    assert "mount " in proc.calls


def test_hub_env_is_replaced_only_after_a_complete_render(lib):
    target = lib["etc"] / "hub.env"
    target.write_text("OLD=kept\n")
    target.chmod(0o600)
    # The first key renders, then a pinned key aborts the render part-way.
    (lib["stage"] / "hub.env").write_text(f"UA_AGENT_TOKENS=a:{PLANTED}\nUA_HOSTED=0\n")
    proc = lib["call"]("install_hub_env")
    assert proc.returncode != 0
    assert target.read_text() == "OLD=kept\n"
    assert sorted(p.name for p in lib["etc"].iterdir()) == ["hub.env"], "temp file left behind"
    assert PLANTED not in proc.stdout + proc.stderr


def test_hub_env_keeps_the_old_file_when_secret_manager_fails(lib):
    target = lib["etc"] / "hub.env"
    target.write_text("OLD=kept\n")
    (lib["stage"] / "hub.env").write_text(
        "UA_OPERATOR_LABEL=op\nWORKOS_API_KEY=secretmanager:workos-key\n"
    )
    proc = lib["call"]("install_hub_env", GCLOUD_RC="1")
    assert proc.returncode != 0
    assert "Secret Manager" in proc.stderr
    assert target.read_text() == "OLD=kept\n"
    assert sorted(p.name for p in lib["etc"].iterdir()) == ["hub.env"]


def test_hub_env_render_reports_a_write_failure_with_errexit_off(lib):
    # install_hub_env runs the renderer under `if !`, where errexit is off; a
    # failed write (a full disk) must still fail the render, or a truncated
    # file would replace the working hub.env.
    (lib["stage"] / "hub.env").write_text("UA_OPERATOR_LABEL=op\nUA_EDITION=enterprise\n")
    proc = lib["call"]("if ! ( render_hub_env ) >/dev/full; then exit 3; fi")
    assert proc.returncode == 3, proc.stderr


def test_hub_env_render_success_is_private(lib):
    (lib["stage"] / "hub.env").write_text(f"# comment\nUA_AGENT_TOKENS=a:{PLANTED}\nUA_EDITION=\n")
    proc = lib["call"]("install_hub_env")
    assert proc.returncode == 0, proc.stderr
    target = lib["etc"] / "hub.env"
    assert target.read_text() == f"UA_AGENT_TOKENS=a:{PLANTED}\n"
    assert target.stat().st_mode & 0o077 == 0
    assert PLANTED not in proc.stdout + proc.stderr
    assert "UA_AGENT_TOKENS" in proc.stderr


def test_switching_to_local_only_backup_removes_the_destination(lib):
    backup_env = lib["etc"] / "backup.env"
    proc = lib["call"]("install_backup_env", BACKUP_GCS_URI="gs://example-bucket/memory-db/x")
    assert proc.returncode == 0, proc.stderr
    assert backup_env.read_text() == "UA_CP_BACKUP_GCS_URI=gs://example-bucket/memory-db/x\n"

    proc = lib["call"]("install_backup_env", BACKUP_GCS_URI="")
    assert proc.returncode == 0 and backup_env.exists(), "an absent flag leaves it as is"

    proc = lib["call"]("install_backup_env", BACKUP_GCS_URI="none")
    assert proc.returncode == 0, proc.stderr
    assert not backup_env.exists()


# --- review round 2: injection, infra-only, cleanup, gcloud on the VM -------

INJECTIONS = {
    "--image": ["registry.example/ua:1$(touch /tmp/pwned)", "registry.example/ua:1;id", "ua image"],
    "--backup-gcs-uri": ["gs://bucket/x$(touch /tmp/pwned)", "gs://bucket/x;id", "gs://b/../x"],
    "--ref": ["HEAD;id", "$(touch /tmp/pwned)", "-oHEAD"],
}


@pytest.mark.parametrize(
    ("flag", "value"),
    [(flag, value) for flag, values in INJECTIONS.items() for value in values],
)
def test_shell_syntax_in_a_value_is_refused_before_anything_runs(env, flag, value):
    args = [f"--env-file={env['hub_env']}"]
    if flag == "--backup-gcs-uri":
        args.append("--ref=HEAD")
    elif flag == "--image":
        args.append("--backup-gcs-uri=none")
    else:
        args.append("--backup-gcs-uri=none")
    proc = _bootstrap(env, *args, f"{flag}={value}")
    assert proc.returncode == 1, proc.stdout
    assert not env["log"].exists(), "refused before any gcloud call"
    assert not Path("/tmp/pwned").exists()


@pytest.mark.parametrize("image", ["ghcr.io/team/ua:release--1", "ghcr.io/team/ua__worker:v1"])
def test_valid_image_references_with_repeated_separators_are_accepted(env, image):
    proc = _bootstrap(
        env, f"--image={image}", f"--env-file={env['hub_env']}", "--backup-gcs-uri=none"
    )
    assert proc.returncode == 0, proc.stderr


def test_remote_install_arguments_are_shell_quoted(env):
    proc = _bootstrap(
        env,
        "--image=registry.example/team/ua:v1.2@sha256:" + "a" * 64,
        f"--env-file={env['hub_env']}",
        "--backup-gcs-uri=gs://example-bucket/memory-db/ua-control-plane",
    )
    assert proc.returncode == 0, proc.stderr
    text = BOOTSTRAP.read_text()
    assert "printf '%q '" in text, "remote argv is built with printf %q"
    line = next(ln for ln in proc.stdout.splitlines() if "install.sh" in ln and "--apply" in ln)
    assert "--image=registry.example/team/ua:v1.2@sha256:" in line


def test_infra_only_does_not_need_a_backup_bucket(env):
    env["vars"]["UA_GCP_PROJECT"] = "unrelated-project"
    proc = _bootstrap(env, "--infra-only")
    assert proc.returncode == 0, proc.stderr
    assert "compute instances create" in proc.stdout


def test_staged_inputs_are_removed_on_success_and_failure(env, tmp_path):
    out = _full(env).stdout
    line = next(ln for ln in out.splitlines() if "install.sh" in ln and "--apply" in ln)
    assert "--cleanup-stage" in line
    assert "rm -f /root/ua-stage/hub.env" not in out, "no separate success-only cleanup"
    # install.sh removes the stage when it fails early, too.
    stage = tmp_path / "ua-stage"
    stage.mkdir()
    (stage / "hub.env").write_text("UA_AGENT_TOKENS=x:y\n")

    def install(*flags):
        argv = ["bash", str(CP / "install.sh"), f"--stage={stage}", "--cleanup-stage", *flags]
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)

    # A dry run never deletes anything, even with --cleanup-stage.
    assert install().returncode != 0
    assert (stage / "hub.env").exists()
    proc = install("--apply")
    assert proc.returncode != 0
    assert not stage.exists()


def test_backup_finishes_before_the_snapshot_window():
    timer = (CP / "ua-hub-backup.timer").read_text()
    service = (CP / "ua-hub-backup.service").read_text()
    on_calendar = re.search(r"OnCalendar=\*-\*-\* (\d\d):(\d\d):\d\d UTC", timer)
    delay = int(re.search(r"RandomizedDelaySec=(\d+)", timer).group(1))
    timeout = re.search(r"TimeoutStartSec=(\d+)min", service)
    assert on_calendar and timeout
    start = int(on_calendar.group(1)) * 60 + int(on_calendar.group(2))
    latest_end = start + delay // 60 + int(timeout.group(1))
    snapshot = re.search(r"SNAPSHOT_START=(\d\d):(\d\d)", BOOTSTRAP.read_text())
    assert latest_end < int(snapshot.group(1)) * 60 + int(snapshot.group(2))


def test_verify_checks_for_a_gcs_client():
    assert "command -v gcloud" in (CP / "verify.sh").read_text()


@pytest.fixture
def bare(tmp_path):
    """A PATH holding only stubs and the few real tools write_file needs."""
    bin_dir = tmp_path / "bare-bin"
    bin_dir.mkdir()
    for tool in ("bash", "mktemp", "chmod", "cat", "mv", "rm"):
        real = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True, text=True)
        (bin_dir / tool).symlink_to(real.stdout.strip())
    _stub(bin_dir, "curl", "echo armored-key")
    _stub(bin_dir, "gpg", 'cat >/dev/null; for a in "$@"; do last=$a; done; : > "$last"')
    _stub(
        bin_dir,
        "apt-get",
        f'if [[ "$*" == *google-cloud-cli* ]]; then printf "#!/bin/sh\\n" > {bin_dir}/gcloud;'
        f" /bin/chmod +x {bin_dir}/gcloud; fi",
    )
    _stub(bin_dir, "env", 'shift; "$@"')
    calls = tmp_path / "bare-calls.log"
    keyrings = tmp_path / "keyrings"
    sources = tmp_path / "sources"
    keyrings.mkdir()
    sources.mkdir()

    def call(function: str):
        setup = (
            f'. {LIB}\nAPPLY=1\nKEYRING_DIR="{keyrings}" APT_SOURCES_DIR="{sources}"\n{function}\n'
        )
        proc = subprocess.run(
            ["/bin/bash", "-c", setup],
            env={"PATH": str(bin_dir), "CALLS": str(calls)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        proc.calls = calls.read_text() if calls.exists() else ""
        return proc

    return {"call": call, "bin": bin_dir, "sources": sources, "keyrings": keyrings}


def test_ensure_gcloud_installs_google_cloud_cli_from_a_signed_repo(bare):
    proc = bare["call"]("ensure_gcloud")
    assert proc.returncode == 0, proc.stderr
    assert "google-cloud-cli" in proc.calls
    listing = (bare["sources"] / "google-cloud-sdk.list").read_text()
    keyring = bare["keyrings"] / "cloud.google.gpg"
    assert f"signed-by={keyring}" in listing and "packages.cloud.google.com/apt" in listing
    assert keyring.exists()


def test_ensure_gcloud_keeps_an_existing_install(bare):
    _stub(bare["bin"], "gcloud", "exit 0")
    proc = bare["call"]("ensure_gcloud")
    assert proc.returncode == 0, proc.stderr
    assert "apt-get" not in proc.calls
