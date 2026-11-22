"""Static regression checks for the curl|bash installer + install gate (task #1195).

Pure file reads plus `bash -n` — no network, no uv; CI runs the real gate.
"""

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from export_guard import needs_exported_paths

REPO_ROOT = Path(__file__).parent.parent
INSTALL = REPO_ROOT / "install.sh"
POLICY = REPO_ROOT / "deploy" / "install-gate" / "policy.yaml"

# install-gate.yml ships as two hand-maintained copies: the GitHub original and the
# Depot CI mirror. Job invariants below run against BOTH — the mirror previously
# carried no coverage at all — while only the GitHub copy owns automatic triggers
# since task #1787 (the Depot copy is dispatch-only).
# `tests/test_ci_workflow_parity.py` guards the pair against drifting apart.
WORKFLOWS = {
    "github": REPO_ROOT / ".github" / "workflows" / "install-gate.yml",
    "depot": REPO_ROOT / ".depot" / "workflows" / "install-gate.yml",
}

# The Depot mirror is private CI and does not ship in the public export; its
# parameter skips there (export_guard), and still fails loudly when missing here.
WORKFLOW_PARAMS = [
    pytest.param(path, id=name, marks=needs_exported_paths(path, repo=REPO_ROOT))
    for name, path in WORKFLOWS.items()
]
needs_depot_mirror = needs_exported_paths(WORKFLOWS["depot"], repo=REPO_ROOT)


def test_install_script_exists_and_is_executable():
    assert INSTALL.is_file()
    assert os.access(INSTALL, os.X_OK), "install.sh must be executable"


def test_install_script_is_strict_bash():
    text = INSTALL.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text


def test_install_script_syntax_parses():
    subprocess.run(["bash", "-n", str(INSTALL)], check=True)


def test_install_script_refuses_root():
    assert "id -u" in INSTALL.read_text(), "install.sh must check for root and refuse"


def test_install_script_pins_uv_version_and_checksum():
    text = INSTALL.read_text()
    assert re.search(r'UV_VERSION="\$\{UA_UV_VERSION:-\d+\.\d+\.\d+\}"', text)
    assert re.search(r'UV_INSTALLER_SHA256="[0-9a-f]{64}"', text)


def test_source_install_and_docker_defaults_follow_available_artifacts():
    project_version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    source_tag = f"v{project_version}"
    assert project_version == "0.3.2"  # Keep the project metadata and release tag aligned.

    assert f'DEFAULT_REF="{source_tag}"' in INSTALL.read_text()
    guide = (REPO_ROOT / "docs" / "installation.md").read_text()
    assert f"--ref {source_tag}" in guide

    # Release images are published to Artifact Registry by
    # scripts/publish-image.sh, so Docker defaults follow the source tag.
    compose = (REPO_ROOT / "deploy" / "docker" / "compose.yaml").read_text()
    assert "${UA_IMAGE_REPOSITORY:?" in compose
    assert f"${{UA_IMAGE_TAG:-{source_tag}}}" in compose
    assert "UA_IMAGE_REPOSITORY" in guide


def test_publish_image_script_builds_the_tag_on_cloud_build():
    script = (REPO_ROOT / "scripts" / "publish-image.sh").read_text()
    assert 'git -C "${repo_root}" archive "${tag}"' in script
    assert "gcloud builds submit" in script
    assert "deploy/docker/cloudbuild.yaml" in script
    cloudbuild = (REPO_ROOT / "deploy" / "docker" / "cloudbuild.yaml").read_text()
    assert '_IMAGE: ""' in cloudbuild
    assert 'test -n "${_IMAGE}" && test -n "${_TAG}"' in cloudbuild
    assert "SOURCE_URL=${_SOURCE_URL}" in cloudbuild
    assert "${_IMAGE}:${_TAG}" in cloudbuild


def test_install_script_uses_documented_cold_start_path():
    text = INSTALL.read_text()
    assert "uv tool install --force" in text, "idempotent install = uv tool install --force"
    assert "unified-agent init" in text
    assert "unified-agent doctor" in text


def test_install_script_never_prints_the_token():
    text = INSTALL.read_text()
    assert "--print-token" not in text
    assert not re.search(r"echo.*\$.*token", text, re.I), "token must never reach stdout"


def test_cold_start_doctor_reads_initialized_service_env(tmp_path):
    """The first doctor run must validate the init-generated token on a fresh host."""
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    for name, script in {
        "uv": "#!/bin/sh\nexit 0\n",
        "unified-agent": (
            "#!/bin/sh\n"
            'if [ "$1" = init ]; then\n'
            '  mkdir -p "$HOME/.unified_agent"\n'
            "  printf 'UA_AGENT_TOKENS=gate:token\\n' > \"$HOME/.unified_agent/service.env\"\n"
            'elif [ "$1" = doctor ]; then\n'
            '  [ "$UA_AGENT_TOKENS" = gate:token ] || exit 21\n'
            '  [ "$UA_MCP_OAUTH" = 0 ] || exit 22\n'
            '  [ "${UA_APPROVAL_TOOLS+x}" = x ] || exit 23\n'
            "else exit 24; fi\n"
        ),
    }.items():
        path = bin_dir / name
        path.write_text(script)
        path.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    for key in (
        "UA_HOME",
        "UA_AGENT_TOKENS",
        "UA_MCP_OAUTH",
        "UA_APPROVAL_TOOLS",
        "UA_SKIP_DOCTOR",
    ):
        env.pop(key, None)
    result = subprocess.run(
        ["bash", str(INSTALL), "--local", str(REPO_ROOT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("transport", "enforce_auth", "skip_doctor", "auth_leg", "expect_success"),
    [
        ("stdio", "0", False, "", True),
        ("stdio", "0", True, "", True),
        ("http", "1", False, "", False),
        ("stdio", "1", False, "", False),
        ("stdio", "1", True, "UA_AGENT_TOKENS=Desktop_Agent2:token\n", False),
        ("http", "1", True, "", False),
        ("http", "1", True, "UA_AGENT_TOKENS=Desktop_Agent2:token\n", True),
        (
            "http",
            "1",
            False,
            "UA_MCP_OAUTH=1\nUA_MCP_OAUTH_ISSUER=https://auth.example.test\n"
            "UA_MCP_OAUTH_PASSPHRASE=test-only\n",
            True,
        ),
        (
            "http",
            "1",
            False,
            "UA_HUB_WORKOS_ORGANIZATION_ID=org-test\n"
            "UA_HUB_WORKOS_IDENTITY_MAP='{" + '"user-test":"Desktop_Agent2"' + "}'\n"
            "WORKOS_ISSUER=https://auth.example.test\nWORKOS_API_KEY=test-only\n"
            "WORKOS_CLIENT_ID=test-only\nWORKOS_REDIRECT_URI=https://app.example.test/callback\n"
            "WORKOS_COOKIE_PASSWORD=test-only\n",
            True,
        ),
    ],
)
def test_install_handles_tokenless_configs_by_transport(
    tmp_path, transport, enforce_auth, skip_doctor, auth_leg, expect_success
):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    custom_home = home / "custom ua home"
    custom_home.mkdir(parents=True)
    bin_dir.mkdir()
    env_file = custom_home / "service.env"
    original = (
        f"UA_TRANSPORT={transport}\nUA_ENFORCE_AUTH={enforce_auth}\nUA_AGENT_TOKENS=\n{auth_leg}"
    )
    env_file.write_text(original)
    for name, script in {
        "uv": "#!/bin/sh\nexit 0\n",
        "unified-agent": (
            "#!/bin/sh\n"
            'if [ "$1" = init ]; then exit 0; fi\n'
            'if [ "$1" = doctor ]; then [ "${UA_SKIP_DOCTOR:-0}" != 1 ] || exit 31; '
            '[ -z "${UA_AGENT_TOKENS:-}" ]; exit $?; fi\n'
            "exit 24\n"
        ),
    }.items():
        path = bin_dir / name
        path.write_text(script)
        path.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "UA_HOME": str(custom_home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    for key in ("UA_AGENT_TOKENS", "UA_SKIP_DOCTOR"):
        env.pop(key, None)
    if skip_doctor:
        env["UA_SKIP_DOCTOR"] = "1"
    result = subprocess.run(
        ["bash", str(INSTALL), "--local", str(REPO_ROOT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert (result.returncode == 0) is expect_success, result.stderr
    assert env_file.read_text() == original


@pytest.mark.parametrize("workflow", WORKFLOW_PARAMS)
class TestWorkflowInvariants:
    """Job invariants must hold in BOTH definitions; only GitHub owns automatic triggers."""

    def test_workflow_keeps_manual_dispatch(self, workflow):
        assert "workflow_dispatch:" in workflow.read_text()

    def test_workflow_verifies_install_script_checksum(self, workflow):
        text = workflow.read_text()
        assert "sha256sum install.sh" in text
        assert "checksum mismatch" in text

    def test_workflow_gates_on_doctor_healthz_and_auth(self, workflow):
        text = workflow.read_text()
        assert "unified-agent doctor" in text
        assert "/healthz" in text
        assert "401" in text and "403" in text, "must assert unauthenticated calls are refused"

    def test_workflow_builds_and_configures_http_ui(self, workflow):
        text = workflow.read_text()
        assert (
            text.index("npm --prefix packages/protocol run build")
            < text.index("npm --prefix packages/client run build")
            < text.index("npm --prefix packages/web run build")
        )
        assert "npm --prefix packages/web ci" in text
        assert "npm --prefix packages/web run build" in text
        assert "UA_WEB_DIST_DIR=" in text

    def test_workflow_installs_the_client_with_npm_install(self, workflow):
        """packages/client must not use `npm ci` in the install gate.

        Its lockfile does not satisfy `npm ci`, so that step exits non-zero and
        the gate never reaches the doctor/healthz/auth checks. The regular CI
        client job already uses `npm install` for the same reason; keep both
        hand-maintained install-gate copies aligned until the client lockfile is
        fixed (Devin finding on PR #697).
        """
        text = workflow.read_text()
        assert "npm --prefix packages/client install" in text
        assert "npm --prefix packages/client ci" not in text

    def test_automatic_workflow_runs_for_ui_dependency_changes(self, workflow):
        """Manifests and lockfiles trigger the gate; UI source does not (#1628).

        CI's protocol/client/web jobs build and test UI source on every PR, so
        re-running this ~30-minute gate for each UI edit only spends minutes.
        """
        if workflow == WORKFLOWS["depot"]:  # the automatic copy since task #2533
            text = workflow.read_text()
            for path in ("packages/*/package.json", "packages/*/package-lock.json", "uv.lock"):
                assert f"- {path}\n" in text
            for path in ("packages/protocol/**", "packages/client/**", "packages/web/**"):
                assert path not in text

    def test_workflow_exercises_park_resolve_consume(self, workflow):
        text = workflow.read_text()
        assert "approval_resolve" in text
        assert re.search(r"request \(\\d\+\) is queued", text), (
            "must capture the parked approval id"
        )
        assert '"decision": "approve"' in text
        assert "allow_fallback=False" in text, "gate must use the real hub, not in-process fallback"
        # #869: approval needs a verified human who is not the requester, and
        # no session may activate a profile. The resolve leg is a separate
        # human identity on its own operator default (task #1637).
        assert "agent = HubClient" in text and "human = HubClient" in text
        assert re.search(
            r'human = HubClient\(\s*agent_label=os\.environ\["GATE_HUMAN_LABEL"\]', text
        ), "the resolve leg must be the separate human identity"
        # The human is the operator init minted a token for (settings.toml),
        # not a compiled-in label.
        assert 'human_label="$(unified-agent settings get operator.label)"' in text
        assert "GATE_HUMAN_LABEL=$human_label" in text
        assert 'first="${UA_AGENT_TOKENS%%,*}"' in text, "init writes two token pairs"
        assert "profile_activate" not in text, "the gate must not escalate a profile"

    def test_workflow_renders_and_uploads_evidence(self, workflow):
        text = workflow.read_text()
        assert "unified-agent-audit verify" in text
        assert "evidence --markdown" in text
        assert "/api/evidence?format=pdf" in text
        assert "upload-artifact" in text

    def test_workflow_does_not_artifact_the_token(self, workflow):
        text = workflow.read_text()
        artifact_section = text.split("upload-artifact", 1)[1]
        assert "gate-token" not in artifact_section
        assert "service.env" not in artifact_section


@needs_depot_mirror
def test_automatic_install_gate_triggers_on_installer_assets_after_merge():
    """Lean policy (#1787): the gate runs on main pushes that move the installer
    or its gate policy, and by hand on a release tag; never on a PR. The
    automatic copy is Depot's since task #2533."""
    doc = yaml.load(WORKFLOWS["depot"].read_text(), Loader=yaml.BaseLoader)
    assert set(doc["on"]) == {"push", "workflow_dispatch"}
    paths = doc["on"]["push"]["paths"]
    assert "install.sh" in paths and "deploy/install-gate/**" in paths
    assert "gh workflow run install-gate.yml --ref v<version>" in WORKFLOWS["github"].read_text()


def test_gate_policy_gates_exactly_one_harmless_tool():
    policy = yaml.safe_load(POLICY.read_text())
    assert policy["enabled"] is True
    assert policy["default_action"] == "allow"
    approval_rules = [r for r in policy["rules"] if r["action"] == "require_approval"]
    assert len(approval_rules) == 1
    assert approval_rules[0]["tool"] == "system_doctor", (
        "the gated tool must stay read-only and harmless"
    )


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            name,
            marks=needs_exported_paths(REPO_ROOT / "deploy" / name, repo=REPO_ROOT),
        )
        for name in (
            "dispatch-tick-cron.sh",
            "gcp/lib.sh",
            "gcp/run-failover-rehearsal-on-worker.sh",
            "mac-hub/run-from-mac.sh",
        )
    ],
)
def test_deploy_scripts_do_not_hard_code_a_home_directory(script):
    """Shell wrappers default to $HOME-relative paths, never a fixed user's home (#1711)."""
    text = (REPO_ROOT / "deploy" / script).read_text()
    assert not re.search(r"/(?:home|Users)/[A-Za-z0-9_.-]+/", text), script


FAILOVER_REHEARSAL = REPO_ROOT / "deploy" / "gcp" / "run-failover-rehearsal-on-worker.sh"
needs_failover_rehearsal = needs_exported_paths(FAILOVER_REHEARSAL, repo=REPO_ROOT)


def test_dispatch_tick_cron_defaults_repo_under_home():
    text = (REPO_ROOT / "deploy" / "dispatch-tick-cron.sh").read_text()
    assert 'REPO="${UA_REPO:-$HOME/dev/unified_agent}"' in text


@needs_failover_rehearsal
def test_failover_rehearsal_swapfile_is_resolved_on_the_worker_not_locally():
    """The swap path is spliced into ssh commands, so it must expand remotely."""
    text = FAILOVER_REHEARSAL.read_text()
    (line,) = [ln for ln in text.splitlines() if ln.startswith("SWAPFILE=")]
    result = subprocess.run(
        ["bash", "-c", f'HOME=/local-desktop-home; {line}; printf "%s" "${{SWAPFILE}}"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "/local-desktop-home" not in result.stdout
    assert result.stdout.startswith("$HOME/")


@needs_failover_rehearsal
def test_failover_rehearsal_pins_the_install_sh_uv_installer():
    """The worker's uv install uses install.sh's pinned, checksum-verified installer."""
    text = FAILOVER_REHEARSAL.read_text()
    install = INSTALL.read_text()
    version = re.search(r'UV_VERSION="\$\{UA_UV_VERSION:-([\d.]+)\}"', install).group(1)
    sha = re.search(r'UV_INSTALLER_SHA256="([0-9a-f]{64})"', install).group(1)
    assert f'UV_VERSION="{version}"' in text
    assert f'UV_INSTALLER_SHA256="{sha}"' in text
    assert "sha256sum -c" in text
    assert "astral.sh/uv/install.sh" not in text


def test_http_installer_tolerates_only_an_unbuilt_ui_at_doctor():
    """An ordinary build of an unbuilt tree omits the UI; the HTTP installer
    must finish (doctor still reports web_ui) unless an override is broken."""
    text = INSTALL.read_text()
    branch = text[text.index("running unified-agent doctor") :]
    assert '"${UA_TRANSPORT:-stdio}" == "http" && -z "${UA_WEB_DIST_DIR:-}"' in branch
    assert "unified-agent doctor --allow-unbuilt-ui" in branch
    assert branch.count("unified-agent doctor\n") == 1
