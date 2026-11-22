"""Static regression checks for the Docker release artifact (task #1194).

Pure file reads — no docker daemon required; CI builds the image for real.
"""

import os
import re
from pathlib import Path

import pytest
from export_guard import needs_exported_paths

REPO_ROOT = Path(__file__).parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "deploy" / "docker" / "entrypoint.sh"
COMPOSE = REPO_ROOT / "deploy" / "docker" / "compose.yaml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

# docker.yml ships as two hand-maintained copies: the GitHub original and the Depot CI
# mirror. Job invariants run against BOTH — the mirror previously had no coverage at
# all — while only the GitHub copy owns automatic triggers since task #1787 (the
# Depot copy is dispatch-only). `tests/test_ci_workflow_parity.py` guards the pair
# against drifting apart and pins the post-merge, path-filtered trigger.
WORKFLOWS = {
    "github": REPO_ROOT / ".github" / "workflows" / "docker.yml",
    "depot": REPO_ROOT / ".depot" / "workflows" / "docker.yml",
}

# The Depot mirror is private CI and does not ship in the public export; its
# parameter skips there (export_guard), and still fails loudly when missing here.
WORKFLOW_PARAMS = [
    pytest.param(path, id=name, marks=needs_exported_paths(path, repo=REPO_ROOT))
    for name, path in WORKFLOWS.items()
]
needs_depot_mirror = needs_exported_paths(WORKFLOWS["depot"], repo=REPO_ROOT)


def test_dockerfile_exists():
    assert DOCKERFILE.is_file()


def test_dockerfile_runs_as_non_root_user_after_copies():
    text = DOCKERFILE.read_text()
    last_copy = text.rfind("\nCOPY")
    user_idx = text.find("USER ua", last_copy)
    assert user_idx != -1, "no `USER ua` directive after the last COPY"


def test_dockerfile_has_healthcheck():
    assert "HEALTHCHECK" in DOCKERFILE.read_text()


def test_entrypoint_is_committed_executable_and_referenced():
    assert ENTRYPOINT.is_file()
    assert os.access(ENTRYPOINT, os.X_OK), "entrypoint.sh must be executable"
    assert "/app/deploy/docker/entrypoint.sh" in DOCKERFILE.read_text()


def test_entrypoint_reapplies_auth_enforcement_after_sourcing_env():
    text = ENTRYPOINT.read_text()
    source_idx = text.find('. "$UA_HOME/service.env"')
    enforce_idx = text.find("UA_ENFORCE_AUTH=1", source_idx)
    assert source_idx != -1, "entrypoint must source $UA_HOME/service.env"
    assert enforce_idx != -1, (
        "UA_ENFORCE_AUTH=1 must be re-applied AFTER sourcing service.env "
        "(an explicit UA_ENFORCE_AUTH=0 in service.env must not win in the container)"
    )


def test_entrypoint_execs_hub():
    assert re.search(r"^exec unified-agent\s*$", ENTRYPOINT.read_text(), re.M)


def test_entrypoint_has_command_passthrough():
    # `docker exec <name> entrypoint.sh <cmd>` must run <cmd> with the sourced
    # hub environment — bare `docker exec` sees only the Dockerfile ENV.
    assert 'exec "$@"' in ENTRYPOINT.read_text()


@pytest.mark.parametrize("workflow", WORKFLOW_PARAMS)
def test_workflow_runs_doctor_through_entrypoint(workflow):
    text = workflow.read_text()
    assert 'docker exec "$UA_CTR" /app/deploy/docker/entrypoint.sh unified-agent doctor' in text


@pytest.mark.parametrize("workflow", WORKFLOW_PARAMS)
def test_smoke_uses_per_run_names_and_a_random_port(workflow):
    """#1787: parallel jobs on one self-hosted host must not collide."""
    text = workflow.read_text()
    assert "UA_CTR: ua-${{ github.run_id }}-${{ github.run_attempt }}" in text
    assert "tags: ${{ env.UA_IMG }}" in text
    assert "-p 127.0.0.1::8765" in text and "-p 8765:8765" not in text
    for fixed in ("--name ua ", "docker logs ua\n", "ua-smoke:/data", "unified-agent:smoke\n"):
        assert fixed not in text, fixed


def test_compose_publishes_loopback_only():
    text = COMPOSE.read_text()
    assert '"127.0.0.1:8765:8765"' in text
    assert not re.search(r'-\s*"8765:8765"', text), "compose must not publish on all interfaces"


@pytest.mark.parametrize("workflow", WORKFLOW_PARAMS)
def test_workflow_publishes_only_on_version_tags(workflow):
    text = workflow.read_text()
    assert "github.ref, 'refs/tags/v'" in text or "refs/tags/v" in text
    assert ":latest" not in text, "publish job must never tag :latest"
    assert "packages: write" in text


def test_github_docker_tells_release_cutters_to_dispatch_on_the_tag():
    """Lean policy (#1787) dropped the automatic v* tag trigger; the release
    build is a manual dispatch on the tag, and the workflow says how."""
    text = WORKFLOWS["github"].read_text()
    assert "gh workflow run docker.yml --ref v<version>" in text


@needs_depot_mirror
def test_automatic_docker_trigger_skips_ui_source():
    """Task #1628: UI source edits are built by CI's web job; the image build
    fires on manifests and lockfiles, not on every packages/** change. The
    automatic copy is Depot's since task #2533."""
    text = WORKFLOWS["depot"].read_text()
    for path in ("packages/*/package.json", "packages/*/package-lock.json", "uv.lock"):
        assert f"- {path}\n" in text
    for path in ("packages/protocol/**", "packages/client/**", "packages/web/**"):
        assert path not in text


def test_dockerignore_excludes_git_and_databases():
    text = DOCKERIGNORE.read_text()
    assert re.search(r"^\.git$", text, re.M)
    assert re.search(r"^\*\.db$", text, re.M)


@pytest.mark.parametrize("workflow", WORKFLOW_PARAMS)
def test_ghcr_publish_runs_only_when_dispatched_on_a_tag(workflow):
    """Task #1257: images publish via Cloud Build; the GHCR publish job runs
    only when someone dispatches the workflow on a v* tag."""
    text = workflow.read_text()
    assert (
        "if: startsWith(github.ref, 'refs/tags/v') && github.event_name == 'workflow_dispatch'"
        in text
    )


def test_github_ghcr_publish_uses_the_job_token():
    """GitHub-hosted runners authenticate to ghcr.io with GITHUB_TOKEN; the
    repo has no GHCR_TOKEN secret, which only the Depot copy needs (#1787)."""
    text = WORKFLOWS["github"].read_text()
    assert "password: ${{ secrets.GITHUB_TOKEN }}" in text
    assert "GHCR_TOKEN }}" not in text
