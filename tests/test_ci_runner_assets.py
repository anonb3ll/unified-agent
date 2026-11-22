"""Static checks for the self-hosted CI runner setup (task #1787).

Pure file reads plus `bash -n` and the printed gcloud plan: nothing here calls
gcloud, GitHub or systemd.
"""

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "deploy" / "ci-runner" / "setup-runner.sh"
README = REPO_ROOT / "deploy" / "ci-runner" / "README.md"


def _embedded(tag: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"<<'{tag}'\n") + len(f"<<'{tag}'\n")
    return text[start : text.index(f"\n{tag}\n", start)]


def test_script_and_helpers_are_strict_bash():
    assert os.access(SCRIPT, os.X_OK)
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    for tag in ("PREP", "RUN", "CLEAN"):
        subprocess.run(["bash", "-n"], input=_embedded(tag), text=True, check=True)


def test_gcloud_appears_only_in_the_printed_plan():
    """The script prints gcloud commands for the operator; it never runs one."""
    text = SCRIPT.read_text()
    plan = text[text.index("gcloud_plan() {") : text.index("install_packages() {")]
    rest = text.replace(plan, "")
    code = [line for line in rest.splitlines() if not line.lstrip().startswith("#")]
    # "gcloud " as a command word; the gcloud-plan subcommand name is fine.
    assert not any(re.search(r"(^|[\s;|&(])gcloud\s", line) for line in code)
    assert "--no-service-account --no-scopes" in plan
    assert "--provisioning-model=SPOT" in plan


def test_plan_renders_the_validated_repo_into_every_command():
    """Devin #969: the printed commands must work in a fresh shell (no ${REPO})."""
    env = {"PATH": os.environ["PATH"], "PROJECT": "p", "REPO": "acme/app"}
    plan = subprocess.run(
        ["bash", str(SCRIPT), "gcloud-plan"], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert "${REPO}" not in plan and "$REPO" not in plan
    assert "repos/acme/app/actions/runners/registration-token" in plan
    assert "sudo REPO=acme/app " in plan
    for bad in ("", "not a repo", "acme/app;rm -rf /"):
        res = subprocess.run(
            ["bash", str(SCRIPT), "gcloud-plan"],
            env=env | {"REPO": bad},
            capture_output=True,
            text=True,
        )
        assert res.returncode != 0, bad


def test_install_refuses_without_root():
    if os.geteuid() == 0:
        return
    res = subprocess.run(["bash", str(SCRIPT), "install"], capture_output=True, text=True)
    assert res.returncode != 0 and "must run as root" in res.stderr


def test_credentials_stay_root_only():
    """#969 review: job code must never read the registration token or the PAT."""
    text = SCRIPT.read_text()
    assert 'install -d -m 700 -o root -g root "$STATE_DIR"' in text
    assert 'install -m 600 -o root -g root "$TOKEN_FILE" "$STATE_DIR/registration-token"' in text
    assert 'install -m 600 -o root -g root "$PAT_FILE" "$STATE_DIR/pat"' in text
    # Only the root helper (ExecStartPre=+) reads them.
    assert "ExecStartPre=+$ROOT_DIR/bin/prepare-slot %i" in text
    assert "/etc/ua-ci-runner" not in _embedded("RUN")
    prep = _embedded("PREP")
    # The token reaches config.sh through root's environment, never argv.
    assert 'ACTIONS_RUNNER_INPUT_TOKEN="$(cat "$state/registration-token")"' in prep
    assert "--token" not in text
    # The PAT reaches curl as a root-only header file, not on the command line.
    assert '-H @"$work/hdr"' in prep
    assert "shred -u" in text


def test_slot_users_have_no_sudo_no_docker_and_no_new_privileges():
    text = SCRIPT.read_text()
    assert "NOPASSWD" not in text
    assert "usermod -aG docker" not in text
    assert 'gpasswd -d "$user" docker' in text
    assert "NoNewPrivileges=yes" in text
    assert "User=ua-ci-%i" in text, "one user per slot: slots cannot read each other"


def test_jit_configs_are_per_slot_and_atomically_replaced():
    """Devin #969: concurrent slots must never write one shared token file."""
    prep = _embedded("PREP")
    assert 'work="$(mktemp -d /run/ua-ci/.mint-XXXXXX)"' in prep
    assert 'mv -f "$work/jit" "/run/ua-ci/slot-$slot.jit"' in prep
    assert "registration-token.new" not in SCRIPT.read_text()
    run = _embedded("RUN")
    assert 'rm -f "$jit"' in run and "exec ./run.sh --jitconfig" in run


def test_runner_tarball_is_checksum_verified_and_mode_is_validated():
    text = SCRIPT.read_text()
    assert "sha256sum -c" in text and "RUNNER_SHA256 is required" in text
    assert "--ephemeral" in text and "RUNNER_MODE must be ephemeral or persistent" in text


def test_readme_states_the_trust_boundary_and_the_label():
    readme = README.read_text()
    assert "gh variable set UA_CI_RUNNER --body ua-ci" in readme
    assert "not a trust boundary" in readme
    assert "RUNNER_LABELS:-ua-ci" in SCRIPT.read_text()
