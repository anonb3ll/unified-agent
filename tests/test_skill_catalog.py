"""Catalog adapters must retain registry, profile, approval, and org authority."""

import hashlib
from contextlib import contextmanager

import pytest
import yaml
from mcp.server.mcpserver.exceptions import ToolError
from test_skills_registry import make_skill

from unified_agent import config, config_revisions, org_registry, profiles, skills_registry
from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve


@pytest.fixture(autouse=True)
def _host_chief_of_staff(seat_chief_of_staff):
    seat_chief_of_staff("gcp_agent5")


@pytest.fixture(autouse=True)
def _human_bearer(monkeypatch):
    # A bearer for the human resolver, so approving a high evidence tier request
    # (config_revision_*, task #1672) can carry its signed producer stamp.
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:synthetic-bearer-h")


@contextmanager
def actor(label="gcp_agent5"):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def published(isolated_env, monkeypatch):
    from unified_agent import skill_catalog

    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY,
        "Desktop_Agent1",
        "steward",
    )
    make_skill(isolated_env, "type-hints")
    with actor():
        row = skills_registry.publish("type-hints", "type-hints")["skill"]
    return skill_catalog, row


@pytest.fixture
def org_file(isolated_env, monkeypatch):
    path = isolated_env / "org.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "offices": [
                    {
                        "id": "bench",
                        "name": "Bench",
                        "lead": "Desktop_Agent4",
                        "members": ["Desktop_Agent4"],
                        "decision_rights": ["Implement assigned work"],
                        "owned_epics": [1347],
                        "default_lanes": ["dispatch-ready"],
                    },
                    {
                        "id": "platform",
                        "name": "Platform",
                        "lead": "Desktop_Agent2",
                        "members": ["Desktop_Agent2"],
                    },
                ],
            }
        )
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(path))
    return path


def test_catalog_immutable_versions_hash_provenance_and_no_bodies(
    published, isolated_env, org_file
):
    catalog, first = published
    make_skill(
        isolated_env,
        "type-hints",
        body="# Updated procedure\n\n1. Inspect Python types.\n2. Add type hints.\n3. Verify with mypy.\n",
    )
    with actor():
        second = skills_registry.publish("type-hints", "type-hints")["skill"]
        data = catalog.catalog()
    assert [s["version"] for s in data["skills"]] == [2, 1]
    assert [s["latest"] for s in data["skills"]] == [True, False]
    assert data["skills"][1]["content_hash"] == first["content_hash"]
    assert data["skills"][0]["content_hash"] == second["content_hash"]
    assert data["skills"][0]["provenance"]["actor"] == "gcp_agent5"
    assert "content" not in data["skills"][0]


def test_install_exact_old_version_and_surface_drift(published, isolated_env, org_file):
    catalog, first = published
    make_skill(
        isolated_env,
        "type-hints",
        body="# Updated procedure\n\n1. Inspect Python types.\n2. Add type hints.\n3. Verify with mypy.\n",
    )
    with actor():
        skills_registry.publish("type-hints", "type-hints")
        result = catalog.install("type-hints", 1, first["content_hash"], "surface")
        data = catalog.catalog(surface_dir="surface")
    destination = isolated_env / "surface/type-hints/SKILL.md"
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == first["content_hash"]
    assert result["version"] == 1
    assert data["surfaces"][0]["result"]["stale"][0]["name"] == "type-hints"


@pytest.mark.parametrize("version", [True, 0, -1, 1.5, "1"])
def test_install_refuses_non_integer_version(published, version):
    catalog, row = published
    with actor(), pytest.raises(ToolError, match="positive integer"):
        catalog.install("type-hints", version, row["content_hash"], "surface")


def test_install_refuses_forged_hash_and_workspace_escape(published, isolated_env, tmp_path):
    catalog, row = published
    with actor(), pytest.raises(ToolError, match="hash"):
        catalog.install("type-hints", 1, "0" * 64, "surface")
    with actor(), pytest.raises(ToolError):
        catalog.install("type-hints", 1, row["content_hash"], str(tmp_path / "outside"))
    assert not (isolated_env / "surface").exists()


def test_install_preserves_local_edits_and_requires_compare_hash(published, isolated_env):
    catalog, row = published
    destination = isolated_env / "surface/type-hints/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("Local work")
    with actor(), pytest.raises(ToolError, match="existing"):
        catalog.install("type-hints", 1, row["content_hash"], "surface")
    assert destination.read_text() == "Local work"
    with actor():
        result = catalog.install(
            "type-hints",
            1,
            row["content_hash"],
            "surface",
            expected_hash=hashlib.sha256(b"Local work").hexdigest(),
        )
    assert result["installed"]


def test_install_profile_and_fs_write_approval_gates(published, isolated_env, monkeypatch):
    catalog, row = published
    with actor("Desktop_Agent1"), pytest.raises(ToolError, match="fs_write"):
        catalog.install("type-hints", 1, row["content_hash"], "surface")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "fs_write")
    with actor(), pytest.raises(ApprovalPendingError) as pending:
        catalog.install("type-hints", 1, row["content_hash"], "surface")
    assert not (isolated_env / "surface").exists()
    with actor("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="Install reviewed version")
    with actor():
        assert catalog.install("type-hints", 1, row["content_hash"], "surface")["installed"]


def test_install_rejects_symlink_surface(published, isolated_env):
    catalog, row = published
    real = isolated_env / "real"
    real.mkdir()
    (isolated_env / "surface").symlink_to(real, target_is_directory=True)
    with actor(), pytest.raises(ToolError, match="symlink"):
        catalog.install("type-hints", 1, row["content_hash"], "surface")
    assert not (real / "type-hints").exists()


def test_teams_derive_org_roles_scope_skill_versions_and_revision(published, org_file):
    catalog, row = published
    with actor():
        revision = config_revisions.capture("org")
        skills_registry.assign("type-hints", "Desktop_Agent4", version=1)
        data = catalog.catalog()
    bench = next(t for t in data["teams"] if t["id"] == "bench")
    assert bench["responsibility"] == ["Implement assigned work"]
    assert bench["permitted_scope"]["owned_epics"] == [1347]
    assert bench["members"][0]["role"] == "lead"
    assert bench["members"][0]["skills"][0]["content_hash"] == row["content_hash"]
    assert bench["version"] == revision["revision"]
    assert bench["provenance"]["actor"] == "gcp_agent5"


def test_team_install_from_revision_requires_exact_approval_and_preserves_other_offices(
    published, org_file
):
    catalog, _ = published
    with actor():
        revision = config_revisions.capture("org")
        original = next(t for t in catalog.catalog()["teams"] if t["id"] == "bench")
        document = yaml.safe_load(org_file.read_text())
        document["offices"][0]["name"] = "Changed Bench"
        document["offices"][1]["name"] = "New Platform"
        config_revisions.commit("org", document)
        with pytest.raises(ApprovalPendingError) as pending:
            catalog.install_team("bench", revision["revision"], original["content_hash"])
    assert org_registry.find_office("bench")["name"] == "Changed Bench"
    with actor("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="Restore reviewed team")
    with actor():
        result = catalog.install_team("bench", revision["revision"], original["content_hash"])
    assert result["installed"]
    assert org_registry.find_office("bench")["name"] == "Bench"
    assert org_registry.find_office("platform")["name"] == "New Platform"


def test_team_install_cannot_reuse_approval_after_live_org_change(published, org_file):
    catalog, _ = published
    with actor():
        revision = config_revisions.capture("org")
        original = next(t for t in catalog.catalog()["teams"] if t["id"] == "bench")
        document = yaml.safe_load(org_file.read_text())
        document["offices"][0]["name"] = "Changed Bench"
        config_revisions.commit("org", document)
        with pytest.raises(ApprovalPendingError) as first:
            catalog.install_team("bench", revision["revision"], original["content_hash"])
    with actor("Desktop_Person"):
        approval_resolve(first.value.approval_id, "approve", reason="Restore team")
    document["offices"][1]["name"] = "Concurrent edit"
    org_file.write_text(yaml.safe_dump(document))
    with actor(), pytest.raises(ApprovalPendingError) as second:
        catalog.install_team("bench", revision["revision"], original["content_hash"])
    assert second.value.approval_id != first.value.approval_id
    assert org_registry.find_office("bench")["name"] == "Changed Bench"


def test_team_install_denies_steward_and_forged_definition(published, org_file):
    catalog, _ = published
    with actor():
        revision = config_revisions.capture("org")
    with actor("Desktop_Agent1"), pytest.raises(ToolError, match="config_revision_manage"):
        catalog.install_team("bench", revision["revision"], "0" * 64)
    with actor(), pytest.raises(ToolError, match="hash"):
        catalog.install_team("bench", revision["revision"], "0" * 64)


def test_install_ignores_unreviewed_source_files_after_attestation(published, isolated_env):
    catalog, row = published
    (isolated_env / "type-hints/helper.py").write_text("print('support')\n")
    with actor():
        assert catalog.install("type-hints", 1, row["content_hash"], "surface")["installed"]
    assert not (isolated_env / "surface/type-hints/helper.py").exists()


def test_publish_refuses_missing_relative_asset(published, isolated_env):
    catalog, _ = published
    make_skill(
        isolated_env,
        "type-hints",
        body="# Type hints\n\n1. Read [the procedure](references/procedure.md).\n2. Add annotations.\n3. Verify types.\n",
    )
    with actor(), pytest.raises(ToolError, match="missing supporting"):
        skills_registry.publish("type-hints", "type-hints")


@pytest.fixture
def api_client(published, org_file, monkeypatch):
    import secrets

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from unified_agent import server
    from unified_agent.webui.routes import web_routes

    tokens = {
        label: f"catalog-test-{secrets.token_urlsafe(8)}"
        for label in ("gcp_agent5", "Desktop_Agent1", "Desktop_Agent4", "Desktop_Person")
    }
    monkeypatch.setenv(
        "UA_AGENT_TOKENS", ",".join(f"{label}:{token}" for label, token in tokens.items())
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    with TestClient(server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))) as client:
        yield client, tokens


def test_http_read_install_and_drift_use_canonical_registry(api_client):
    client, tokens = api_client
    headers = {"Authorization": f"Bearer {tokens['gcp_agent5']}"}
    response = client.get("/api/skills-teams", headers=headers)
    assert response.status_code == 200
    row = response.json()["skills"][0]
    response = client.post(
        "/api/action/skills-teams",
        headers=headers,
        json={
            "action": "install",
            "params": {
                "name": row["name"],
                "version": row["version"],
                "content_hash": row["content_hash"],
                "surface_dir": "surface",
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["installed"]
    response = client.get("/api/skills-teams?surface_dir=surface", headers=headers)
    assert response.json()["surfaces"][0]["result"]["synced"] == [
        {"name": row["name"], "version": row["version"]}
    ]


def test_http_catalog_authority_never_uses_missing_mcp_session_bypass(api_client):
    client, tokens = api_client
    assert client.get("/api/skills-teams").status_code == 401
    headers = {"Authorization": f"Bearer {tokens['Desktop_Agent4']}"}
    assert client.get("/api/skills-teams", headers=headers).status_code == 403
    headers = {"Authorization": f"Bearer {tokens['Desktop_Agent1']}"}
    read = client.get("/api/skills-teams", headers=headers)
    assert read.status_code == 200
    assert read.json()["permissions"] == {
        "drift": True,
        "publish": True,
        "install": False,
        "team_install": False,
    }
    assert (
        client.post(
            "/api/action/skills-teams", headers=headers, json={"action": "install", "params": {}}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/action/skills-teams",
            headers=headers,
            json={"action": "team_install", "params": {}},
        ).status_code
        == 403
    )


def test_http_publish_retains_skill_review_block(api_client, isolated_env):
    client, tokens = api_client
    make_skill(isolated_env, "broken", raw="# No frontmatter\n")
    response = client.post(
        "/api/action/skills-teams",
        headers={"Authorization": f"Bearer {tokens['Desktop_Agent1']}"},
        json={"action": "publish", "params": {"name": "broken", "path": "broken"}},
    )
    assert response.status_code == 200
    assert response.json()["blocked"]
    assert skills_registry.get("broken") is None


def test_http_install_parks_existing_fs_write_approval(api_client, isolated_env, monkeypatch):
    client, tokens = api_client
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "fs_write")
    row = skills_registry.get("type-hints")
    response = client.post(
        "/api/action/skills-teams",
        headers={"Authorization": f"Bearer {tokens['gcp_agent5']}"},
        json={
            "action": "install",
            "params": {
                "name": row["name"],
                "version": row["version"],
                "content_hash": row["content_hash"],
                "surface_dir": "surface",
            },
        },
    )
    assert response.status_code == 202
    assert response.json()["pending"]
    assert not (isolated_env / "surface").exists()


def test_install_does_not_bypass_policy(published, isolated_env, monkeypatch):
    catalog, row = published
    policy = isolated_env / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "default_action": "allow",
                "rules": [
                    {
                        "name": "deny-file-write",
                        "tool": "fs_write",
                        "type": "tool",
                        "action": "deny",
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    with actor(), pytest.raises(ToolError):
        catalog.install("type-hints", 1, row["content_hash"], "surface")
    assert not (isolated_env / "surface").exists()


def test_http_human_operator_reads_metadata_without_write_authority(api_client):
    client, tokens = api_client
    headers = {"Authorization": f"Bearer {tokens['Desktop_Person']}"}
    response = client.get("/api/skills-teams", headers=headers)
    assert response.status_code == 200
    assert response.json()["permissions"] == {
        "drift": False,
        "publish": False,
        "install": False,
        "team_install": False,
    }
    assert (
        client.post(
            "/api/action/skills-teams", headers=headers, json={"action": "publish", "params": {}}
        ).status_code
        == 403
    )
    assert client.get("/api/skills-teams?surface_dir=surface", headers=headers).status_code == 400

    for action in ("install", "team_install"):
        assert (
            client.post(
                "/api/action/skills-teams", headers=headers, json={"action": action, "params": {}}
            ).status_code
            == 403
        )


def test_catalog_historical_team_has_captured_definition_and_keeps_declared_skills(
    published, org_file
):
    catalog, skill = published
    document = yaml.safe_load(org_file.read_text())
    declared = [{"name": "type-hints", "version": 1, "content_hash": skill["content_hash"]}]
    document["offices"][0]["skill_versions"] = declared
    org_file.write_text(yaml.safe_dump(document))
    with actor():
        first = config_revisions.capture("org")
        document["offices"][0]["name"] = "Changed Bench"
        config_revisions.commit("org", document)
        rows = [t for t in catalog.catalog()["teams"] if t["id"] == "bench"]
    assert [t["name"] for t in rows] == ["Changed Bench", "Bench"]
    assert rows[1]["version"] == first["revision"]
    assert rows[1]["latest"] is False
    assert rows[1]["declared_skill_versions"] == declared
    assert rows[1]["permitted_scope"]["grants_authority"] is False


def test_install_rechecks_local_file_after_approval_hook(published, isolated_env, monkeypatch):
    catalog, skill = published
    from unified_agent.tools import approvals

    destination = isolated_env / "surface/type-hints/SKILL.md"

    def changed(*args, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("Concurrent work")
        return {}

    monkeypatch.setattr(approvals, "check_approval_gate", changed)
    with actor(), pytest.raises(ToolError, match="changed during approval"):
        catalog.install("type-hints", 1, skill["content_hash"], "surface")
    assert destination.read_text() == "Concurrent work"


def test_replacement_approval_does_not_authorize_new_local_base(
    published, isolated_env, monkeypatch
):
    catalog, skill = published
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "fs_write")
    destination = isolated_env / "surface/type-hints/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("Local version A")
    hash_a = hashlib.sha256(destination.read_bytes()).hexdigest()
    with actor(), pytest.raises(ApprovalPendingError) as first:
        catalog.install("type-hints", 1, skill["content_hash"], "surface", expected_hash=hash_a)
    with actor("Desktop_Person"):
        approval_resolve(first.value.approval_id, "approve", reason="Replace exactly A")
    destination.write_text("Local version B")
    hash_b = hashlib.sha256(destination.read_bytes()).hexdigest()
    with actor(), pytest.raises(ApprovalPendingError) as second:
        catalog.install("type-hints", 1, skill["content_hash"], "surface", expected_hash=hash_b)
    assert second.value.approval_id != first.value.approval_id
    assert destination.read_text() == "Local version B"


def test_new_surface_keeps_catalog_visible_and_can_be_installed(api_client, isolated_env):
    client, tokens = api_client
    headers = {"Authorization": f"Bearer {tokens['gcp_agent5']}"}
    response = client.get("/api/skills-teams?surface_dir=fresh-surface", headers=headers)
    assert response.status_code == 200
    assert response.json()["skills"]
    assert response.json()["surfaces"][0]["result"]["error"]
    row = response.json()["skills"][0]
    response = client.post(
        "/api/action/skills-teams",
        headers=headers,
        json={
            "action": "install",
            "params": {
                "name": row["name"],
                "version": row["version"],
                "content_hash": row["content_hash"],
                "surface_dir": "fresh-surface",
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["installed"]
    assert (isolated_env / "fresh-surface/type-hints/SKILL.md").is_file()


def test_catalog_publish_attests_support_package_before_creating_version(api_client, isolated_env):
    client, tokens = api_client
    make_skill(isolated_env, "package-skill")
    (isolated_env / "package-skill/helper.py").write_text("print('support')\n")
    response = client.post(
        "/api/action/skills-teams",
        headers={"Authorization": f"Bearer {tokens['gcp_agent5']}"},
        json={"action": "publish", "params": {"name": "package-skill", "path": "package-skill"}},
    )
    assert response.status_code == 200
    assert response.json()["ok"]
    assert (
        skills_registry.get("package-skill")["package_manifest"]["files"][1]["path"] == "helper.py"
    )


def test_absence_approval_is_not_reused_after_local_creation(published, isolated_env, monkeypatch):
    catalog, skill = published
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "fs_write")
    with actor(), pytest.raises(ApprovalPendingError) as first:
        catalog.install("type-hints", 1, skill["content_hash"], "surface")
    with actor("Desktop_Person"):
        approval_resolve(
            first.value.approval_id, "approve", reason="Install into absent destination"
        )
    destination = isolated_env / "surface/type-hints/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("New local work")
    with actor(), pytest.raises(ApprovalPendingError) as second:
        catalog.install(
            "type-hints",
            1,
            skill["content_hash"],
            "surface",
            expected_hash=hashlib.sha256(destination.read_bytes()).hexdigest(),
        )
    assert second.value.approval_id != first.value.approval_id
    assert destination.read_text() == "New local work"


@pytest.mark.parametrize("body", [[], {"action": {}}, {"action": "install", "params": []}])
def test_http_action_rejects_malformed_contract(api_client, body):
    client, tokens = api_client
    response = client.post(
        "/api/action/skills-teams",
        headers={"Authorization": f"Bearer {tokens['gcp_agent5']}"},
        json=body,
    )
    assert response.status_code == 400


def test_lower_writer_pre_mutation_refusal_restores_install_pass(
    published, isolated_env, monkeypatch
):
    catalog, skill = published
    from unified_agent.tools import approvals

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "fs_write")
    destination = isolated_env / "surface/type-hints/SKILL.md"
    with actor(), pytest.raises(ApprovalPendingError) as pending:
        catalog.install("type-hints", 1, skill["content_hash"], "surface")
    with actor("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="Install attested bytes")
    real_gate = approvals.check_approval_gate
    count = 0

    def edit_at_lower_gate(*args, **kwargs):
        nonlocal count
        result = real_gate(*args, **kwargs)
        count += 1
        if count == 1:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("Concurrent local work")
        return result

    monkeypatch.setattr(approvals, "check_approval_gate", edit_at_lower_gate)
    with actor(), pytest.raises(ToolError, match="changed during approval"):
        catalog.install("type-hints", 1, skill["content_hash"], "surface")
    assert destination.read_text() == "Concurrent local work"
    with actor():
        assert pending.value.approval_id in {
            row["id"] for row in approvals.approval_pending()["approved_unconsumed"]
        }


@pytest.mark.parametrize(
    "malformed",
    [
        "type-hints",
        {"name": "type-hints"},
        [{"name": {"bad": "shape"}, "version": 1, "content_hash": "0" * 64}],
        None,
    ],
)
def test_catalog_normalizes_accepted_invalid_declared_versions(published, org_file, malformed):
    catalog, _ = published
    document = yaml.safe_load(org_file.read_text())
    document["offices"][0]["skill_versions"] = malformed
    with actor():
        config_revisions.commit("org", document)
        data = catalog.catalog()
    team = next(t for t in data["teams"] if t["id"] == "bench")
    assert team["declared_skill_versions"] == []
    assert team["declared_skill_versions_error"]
    assert next(t for t in data["teams"] if t["id"] == "platform")["name"] == "Platform"


@pytest.mark.parametrize("action", ["install", "team_install"])
def test_nested_installer_approval_preserves_registry_pass_until_mutation(
    published, org_file, monkeypatch, action
):
    catalog, skill = published
    from unified_agent.tools.approvals import get_approval

    with actor():
        if action == "team_install":
            captured = config_revisions.capture("org")
            team = next(t for t in catalog.catalog()["teams"] if t["id"] == "bench")
            document = yaml.safe_load(org_file.read_text())
            document["offices"][0]["name"] = "Changed Bench"
            config_revisions.commit("org", document)
            params = {
                "team_id": "bench",
                "revision": captured["revision"],
                "content_hash": team["content_hash"],
            }
        else:
            params = {
                "name": "type-hints",
                "version": 1,
                "content_hash": skill["content_hash"],
                "surface_dir": "surface",
            }
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "skill_registry:fs_write")
    with actor(), pytest.raises(ApprovalPendingError) as first:
        catalog.call(action, params)
    with actor("Desktop_Person"):
        approval_resolve(first.value.approval_id, "approve", reason="Authorize catalog operation")
    with actor(), pytest.raises(ApprovalPendingError) as second:
        catalog.call(action, params)
    assert get_approval(first.value.approval_id)["status"] == "approved"
    with actor("Desktop_Person"):
        approval_resolve(
            second.value.approval_id, "approve", reason="Authorize exact installed payload"
        )
    with actor():
        result = catalog.call(action, params)
    assert result["installed"]
    assert get_approval(first.value.approval_id)["status"] == "consumed"
    assert get_approval(second.value.approval_id)["status"] == "consumed"


def test_unrelated_live_org_edit_retains_unchanged_office_capture(published, org_file):
    catalog, _ = published
    with actor():
        capture = config_revisions.capture("org")
    document = yaml.safe_load(org_file.read_text())
    document["offices"][1]["name"] = "Out of band Platform edit"
    org_file.write_text(yaml.safe_dump(document))
    with actor():
        data = catalog.catalog()
    bench = next(t for t in data["teams"] if t["id"] == "bench" and t["latest"])
    assert bench["version"] == capture["revision"]
    assert bench["provenance"]["actor"] == "gcp_agent5"
    platform = next(t for t in data["teams"] if t["id"] == "platform" and t["latest"])
    assert platform["version"] is None
    assert any(t["id"] == "platform" and t["version"] == capture["revision"] for t in data["teams"])


def test_relative_install_obeys_active_worktree_root(published, isolated_env, monkeypatch):
    catalog, skill = published
    from unified_agent import safety

    worktree = isolated_env / "active-worktree"
    worktree.mkdir()
    monkeypatch.setattr(safety, "get_active_worktree_path", lambda: worktree)
    with actor():
        result = catalog.install("type-hints", 1, skill["content_hash"], "surface")
    assert result["path"] == str(worktree / "surface/type-hints/SKILL.md")
    assert (worktree / "surface/type-hints/SKILL.md").exists()
    assert not (isolated_env / "surface").exists()


def test_catalog_pass_stays_spent_when_post_write_evidence_fails(
    published, isolated_env, monkeypatch
):
    catalog, skill = published
    from unified_agent import events
    from unified_agent.tools import approvals

    params = {
        "name": "type-hints",
        "version": 1,
        "content_hash": skill["content_hash"],
        "surface_dir": "surface",
    }
    destination = isolated_env / "surface/type-hints/SKILL.md"
    body = skills_registry.get("type-hints", 1)["content"]
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "skill_registry:fs_write")
    with actor(), pytest.raises(ApprovalPendingError) as outer:
        approvals.check_approval_gate("skill_registry", {"action": "install", "params": params})
    with actor(), pytest.raises(ApprovalPendingError) as lower:
        approvals.check_approval_gate(
            "fs_write",
            {
                "path": str(destination),
                "append": False,
                "content_sha256": skill["content_hash"],
                "bytes": len(body.encode()),
                "expected_content_sha256": "",
                "package_manifest": skill["package_manifest"],
                "removed_paths": [],
            },
        )
    with actor("Desktop_Person"):
        for pending in (outer, lower):
            approval_resolve(pending.value.approval_id, "approve", reason="Authorize exact install")

    real_event = events.record_event

    def unavailable(channel, *args, **kwargs):
        if channel == "agent.skill.installed":
            raise RuntimeError("Evidence append unavailable")
        return real_event(channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event", unavailable)
    with actor(), pytest.raises(RuntimeError, match="Evidence append"):
        catalog.call("install", params)
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == skill["content_hash"]
    assert approvals.get_approval(outer.value.approval_id)["status"] == "consumed"
    assert approvals.get_approval(lower.value.approval_id)["status"] == "consumed"


@pytest.mark.parametrize("action", ["install", "team_install"])
def test_unchanged_installer_does_not_spend_unused_registry_pass(
    published, org_file, monkeypatch, action
):
    catalog, skill = published
    from unified_agent.tools.approvals import get_approval

    with actor():
        if action == "install":
            catalog.install("type-hints", 1, skill["content_hash"], "surface")
            params = {
                "name": "type-hints",
                "version": 1,
                "content_hash": skill["content_hash"],
                "surface_dir": "surface",
            }
        else:
            capture = config_revisions.capture("org")
            team = next(t for t in catalog.catalog()["teams"] if t["id"] == "bench")
            params = {
                "team_id": "bench",
                "revision": capture["revision"],
                "content_hash": team["content_hash"],
            }
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "skill_registry:fs_write")
    with actor(), pytest.raises(ApprovalPendingError) as pending:
        catalog.call(action, params)
    with actor("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="Authorize catalog action")
    with actor():
        result = catalog.call(action, params)
    assert result["unchanged"]
    assert not result["installed"]
    assert get_approval(pending.value.approval_id)["status"] == "approved"


@pytest.mark.parametrize("boundary", ["precommit", "postcommit"])
def test_team_install_receipts_follow_durable_org_commit(
    published, org_file, monkeypatch, boundary
):
    catalog, _ = published
    from unified_agent.tools import approvals

    with actor():
        capture = config_revisions.capture("org")
        original = next(t for t in catalog.catalog()["teams"] if t["id"] == "bench")
        document = yaml.safe_load(org_file.read_text())
        document["offices"][0]["name"] = "Changed Bench"
        config_revisions.commit("org", document)
    params = {
        "team_id": "bench",
        "revision": capture["revision"],
        "content_hash": original["content_hash"],
    }
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "skill_registry")
    with actor(), pytest.raises(ApprovalPendingError) as outer:
        catalog.call("team_install", params)
    with actor("Desktop_Person"):
        approval_resolve(outer.value.approval_id, "approve", reason="Restore captured office")
    with actor(), pytest.raises(ApprovalPendingError) as lower:
        catalog.call("team_install", params)
    with actor("Desktop_Person"):
        approval_resolve(lower.value.approval_id, "approve", reason="Authorize exact org commit")
    count = config_revisions.list_revisions("org")["count"]

    def unavailable(*args, **kwargs):
        raise RuntimeError("Org revision boundary unavailable")

    monkeypatch.setattr(
        config_revisions, "_record" if boundary == "precommit" else "_publish", unavailable
    )
    with actor(), pytest.raises(RuntimeError, match="Org revision boundary") as call_exc:
        catalog.call("team_install", params)
    if boundary == "postcommit":
        assert isinstance(call_exc.value, config_revisions.ConfigFanoutError)
        assert call_exc.value.committed is True
        assert call_exc.value.result["installed"] is True
        assert call_exc.value.result["team_id"] == "bench"
        assert call_exc.value.result["notification_sent"] is False
        assert "Org revision boundary" in (call_exc.value.result.get("notification_error") or "")
        assert org_registry.find_office("bench")["name"] == "Bench"
        assert config_revisions.list_revisions("org")["count"] == count + 1
        expected = "consumed"
    else:
        assert org_registry.find_office("bench")["name"] == "Changed Bench"
        assert config_revisions.list_revisions("org")["count"] == count
        expected = "approved"
    assert approvals.get_approval(outer.value.approval_id)["status"] == expected
    assert approvals.get_approval(lower.value.approval_id)["status"] == expected


def test_catalog_retains_captures_beyond_complete_history_window(published, org_file):
    catalog, _ = published
    original = yaml.safe_load(org_file.read_text())
    with actor():
        captured = config_revisions.capture("org")
        first = next(t for t in catalog.catalog()["teams"] if t["id"] == "bench")
        document = yaml.safe_load(org_file.read_text())
        document["offices"][0]["name"] = "Later Bench"
        for revision in range(105):
            document["offices"][1]["name"] = f"Platform revision {revision}"
            config_revisions.commit("org", document)
    summary = config_revisions.list_revisions("org")
    assert summary["count"] == 106
    assert len(summary["revisions"]) == 50
    # Reintroducing an old definition outside the chain must retain its capture.
    document["offices"][0] = original["offices"][0]
    org_file.write_text(yaml.safe_dump(document))
    with actor():
        teams = catalog.catalog()["teams"]
    bench = next(t for t in teams if t["id"] == "bench" and t["latest"])
    assert bench["version"] == captured["revision"]
    assert bench["content_hash"] == first["content_hash"]
    assert bench["provenance"]["actor"] == "gcp_agent5"
    assert any(t["id"] == "platform" and t["version"] == captured["revision"] for t in teams)


def test_catalog_worktree_install_and_drift_share_scoped_root(
    published, org_file, tmp_path, monkeypatch
):
    catalog, skill = published
    from unified_agent import safety

    worktree = tmp_path / "project" / "active-worktree"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(safety, "get_active_worktree_path", lambda: worktree)
    with actor():
        installed = catalog.install("type-hints", 1, skill["content_hash"], "surface")
        for surface in ("surface", str(worktree / "surface")):
            report = catalog.catalog(surface)["surfaces"][0]["result"]
            assert report.get("root") == str(worktree / "surface")
            assert report["synced"] == [{"name": "type-hints", "version": 1}]
    assert installed["path"] == str(worktree / "surface/type-hints/SKILL.md")
    # The direct registry's broader audit API retains its existing roots.
    with pytest.raises(ToolError, match="auditable roots"):
        skills_registry.drift(str(worktree / "surface"))


def test_catalog_worktree_drift_does_not_read_outside_scoped_roots(
    published, org_file, tmp_path, monkeypatch
):
    catalog, skill = published
    from unified_agent import safety

    worktree = tmp_path / "project" / "active-worktree"
    worktree.mkdir(parents=True)
    outside = tmp_path / "unscoped"
    outside.mkdir()
    (outside / "SKILL.md").write_text("unrelated private content")
    monkeypatch.setattr(safety, "get_active_worktree_path", lambda: worktree)
    with actor():
        catalog.install("type-hints", 1, skill["content_hash"], "surface")
        (worktree / "surface" / "escape").symlink_to(outside, target_is_directory=True)
        report = catalog.catalog("surface")["surfaces"][0]["result"]
        denied = catalog.catalog(str(outside))["surfaces"][0]["result"]
    assert "error" in report and "synced" not in report
    assert "error" in denied and "unregistered" not in denied


def test_catalog_drift_retains_explicit_audit_roots(published, org_file, tmp_path, monkeypatch):
    catalog, _ = published
    audit_root = tmp_path / "read-only-audit-root"
    make_skill(audit_root, "type-hints")
    monkeypatch.setenv("UA_AUDIT_ROOTS", str(audit_root))
    with actor():
        report = catalog.catalog(str(audit_root))["surfaces"][0]["result"]
        legacy = skills_registry.drift(str(audit_root))
    assert report["root"] == str(audit_root)
    assert report["synced"] == legacy["synced"]
    assert report["synced"] == [{"name": "type-hints", "version": 1}]


def test_catalog_reports_drift_authority_without_widening_operator(published, org_file):
    catalog, _ = published
    with actor("Desktop_Person"):
        assert catalog.catalog()["permissions"]["drift"] is False
        with pytest.raises(ToolError, match="surface drift requires"):
            catalog.catalog("surface")
    with actor():
        assert catalog.catalog()["permissions"]["drift"] is True


def test_skills_teams_payload_returns_outcome_on_fanout_failure(published, org_file, monkeypatch):
    import anyio

    from unified_agent.webui import api

    catalog, _ = published
    with actor():
        captured = config_revisions.capture("org")
        first = next(t for t in catalog.catalog()["teams"] if t["id"] == "bench")
        document = yaml.safe_load(org_file.read_text())
        document["offices"][0]["name"] = "Changed Bench"
        config_revisions.commit("org", document)

    params = {
        "team_id": "bench",
        "revision": captured["revision"],
        "content_hash": first["content_hash"],
    }

    res1 = anyio.run(api.skills_teams_payload, "gcp_agent5", "team_install", params)
    assert res1.get("pending") is True
    approval_id = res1["approval_id"]

    with actor("Desktop_Person"):
        approval_resolve(approval_id, "approve", reason="authorize team install")

    def fail_publish(*_args, **_kwargs):
        raise RuntimeError("webui pubsub failure")

    monkeypatch.setattr(config_revisions, "_publish", fail_publish)

    result = anyio.run(api.skills_teams_payload, "gcp_agent5", "team_install", params)
    assert result["installed"] is True
    assert result["team_id"] == "bench"
    assert result["notification_sent"] is False
    assert "webui pubsub failure" in (result.get("notification_error") or "")
    assert org_registry.find_office("bench")["name"] == "Bench"
