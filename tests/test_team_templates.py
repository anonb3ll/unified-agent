"""Starter catalog bindings retain existing revision, launch and workflow gates."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_team_spec import _register

from unified_agent import (
    config,
    config_revisions,
    storage,
    team_spec,
    team_templates,
    tool_surface,
    workflow_roles,
)
from unified_agent.tools import approvals, tasks, workflow

LABELS = ["Desktop_Agent2", "Desktop_Agent1", "Desktop_Agent4", "Desktop_agent3"]


@pytest.fixture(autouse=True)
def _host_chief_of_staff(seat_chief_of_staff):
    seat_chief_of_staff("gcp_agent5")


@pytest.fixture
def starter_env(isolated_env, monkeypatch):
    org = isolated_env / "org.yaml"
    org.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "offices": [
                    {"id": "example", "name": "Example", "lead": LABELS[0], "members": LABELS}
                ],
            }
        )
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(org))
    monkeypatch.setenv("UA_AGENT_LABELS", ":".join(LABELS + ["gcp_agent5"]))
    path = isolated_env / "policy.yaml"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "enabled": True,
                "default_action": "allow",
                "rules": [],
                "operating_profiles": {"profiles": {"standard": {"comms": {"mode": "direct"}}}},
                "trust_presets": {"agents": dict.fromkeys(LABELS, "active-worktree")},
            }
        )
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    token = config.agent_identity_var.set("gcp_agent5")
    storage.init_db()
    try:
        yield path
    finally:
        config.agent_identity_var.reset(token)


def params(name="implementation-pair"):
    template = team_templates.definition(name)
    return dict(
        template_id=name,
        team_id="example",
        content_hash=template["content_hash"],
        operating_profile="standard",
        trust_preset="active-worktree",
        bindings={
            role: {"label": LABELS[i], "harness": ["codex", "claude", "cursor", "gemini"][i]}
            for i, role in enumerate(template["roles"])
        },
    )


@pytest.mark.parametrize("name", team_templates.IDS)
def test_all_templates_bind_validate_and_are_public_safe(starter_env, name):
    data = params(name)
    body, spec = team_templates.prepare(**data)
    assert set(spec["starter_template"]["roles"]) == set(data["bindings"])
    generated = team_templates.workflow_for(spec)
    workflow_roles.validate(generated)
    assert all(step["tool"] in workflow._tool_registry() for step in generated["steps"])
    text = json.dumps(team_templates.definition(name))
    assert all(label not in text for label in LABELS)
    assert "100." not in text and "/home/" not in text
    if name in {"research-team", "adversarial-review"}:
        assignments = [s for s in generated["steps"] if s["tool"] == "task_add"]
        assert len(assignments) == 2
        assert len({s["args"]["lane"] for s in assignments}) == 2
        assert generated["steps"][-1]["actor_role"] == "lead"
    assert body["operating_profile"] == "standard"


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda p: p.update(content_hash="0" * 64), "hash"),
        (lambda p: p.update(operating_profile="unknown"), "operating profile"),
        (lambda p: p.update(trust_preset="inspection"), "already bound"),
        (lambda p: p.update(trust_preset=[]), "configured trust preset"),
        (lambda p: p.update(trust_preset={}), "configured trust preset"),
        (lambda p: p["bindings"]["checker"].update(harness="codex"), "different harnesses"),
        (lambda p: p["bindings"]["checker"].update(label="Unregistered"), "roster"),
        (lambda p: p["bindings"]["checker"].update(label=LABELS[0]), "repeats a label"),
        (
            lambda p: p["bindings"].update(extra={"label": LABELS[2], "harness": "cursor"}),
            "every template role",
        ),
    ],
)
def test_bindings_fail_closed(starter_env, mutation, match):
    data = params()
    mutation(data)
    before = starter_env.read_bytes()
    with pytest.raises(ToolError, match=match):
        team_templates.prepare(**data)
    assert starter_env.read_bytes() == before


def test_install_parks_without_writes_then_revision_and_generated_loader(starter_env, monkeypatch):
    before = starter_env.read_bytes()
    with pytest.raises(approvals.ApprovalPendingError):
        team_templates.install(**params())
    assert starter_env.read_bytes() == before
    gates = []

    def approved(tool, payload, **kwargs):
        gates.append((tool, payload, kwargs))
        return {"status": "approved"}

    monkeypatch.setattr(approvals, "check_approval_gate", approved)
    result = team_templates.install(**params())
    assert result["installed"]
    assert result["workflow"] == "starter-example"
    assert any(g[2].get("force") and g[1].get("base_sha256") for g in gates)
    assert config_revisions.list_revisions("policy")["revisions"]
    loaded = workflow._load_definition("starter-example")
    assert loaded["roles"]["checker"]["preferred_seats"] == [LABELS[1]]
    assert "starter-example" in {w["name"] for w in workflow.workflow_list()["workflows"]}
    assert team_templates.install(**params())["unchanged"]
    monkeypatch.setattr(team_templates.trust_presets, "selection", lambda _label: "inspection")
    with pytest.raises(ToolError, match="already bound"):
        workflow._load_definition("starter-example")


def test_policy_changed_during_gate_is_preserved(starter_env, monkeypatch):
    def raced(*args, **kwargs):
        starter_env.write_text(starter_env.read_text() + "\n# external operator edit\n")
        return {"status": "approved"}

    monkeypatch.setattr(approvals, "check_approval_gate", raced)
    with pytest.raises(ToolError, match="changed during approval"):
        team_templates.install(**params())
    assert "external operator edit" in starter_env.read_text()
    assert "starter_template" not in starter_env.read_text()


def test_preview_and_boot_use_existing_launch_gate_and_double(starter_env, monkeypatch):
    body, spec = team_templates.prepare(**params())
    for i, seat in enumerate(spec["seats"]):
        _register(f"test-node-{i}", seat["label"], [f"acp:{seat['harness']}"])
    refused = team_templates.preview(**params())["plan"]
    assert not refused["ready"]
    assert any(r["code"] == "trust_preset" for r in refused["refusals"])
    # Native containment remains refused in production for configured presets.
    # This readiness double allows exercising boot without a native process.
    monkeypatch.setattr(team_spec, "live_checks", lambda *a, **k: [])
    preview = team_templates.preview(**params())
    assert preview["plan"]["ready"]
    original = approvals.check_approval_gate
    with pytest.raises(approvals.ApprovalPendingError):
        team_spec.boot(spec, actor="gcp_agent5")
    calls = []

    class Launcher:
        def launch(self, seat, **kwargs):
            calls.append((seat["label"], kwargs["prompt"]))
            return {"status": "queued", "item_id": f"test-{len(calls)}"}

    monkeypatch.setattr(approvals, "check_approval_gate", lambda *a, **k: {"status": "approved"})
    result = team_spec.boot(
        spec,
        actor="gcp_agent5",
        launcher=Launcher(),
        expected_plan_sha256=preview["plan"]["plan_sha256"],
    )
    assert result["booted"] and len(calls) == 2
    assert "exact builder commit" in calls[1][1]
    assert team_spec.boot(
        spec, actor="gcp_agent5", launcher=Launcher(), expected_plan_sha256="stale"
    )["stale_plan"]
    assert len(calls) == 2
    monkeypatch.setattr(approvals, "check_approval_gate", original)


@pytest.mark.parametrize(
    "template,objective",
    [
        ("research-team", "Compare cited primary sources"),
        ("research-team", "Document the literal template {{ user.name }}"),
        ("adversarial-review", "Document the literal template {{ inputs.head }}"),
    ],
)
def test_parallel_tasks_start_through_governed_workflow(
    starter_env, monkeypatch, template, objective
):
    monkeypatch.setattr(approvals, "check_approval_gate", lambda *a, **k: {"status": "approved"})
    team_templates.install(**params(template))
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *a, **k: None)
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: True)
    # Registered invocation doubles retain the durable workflow projector while
    # avoiding native/model dispatch and this fixture's intentionally tight trust.
    for fn in (tasks.task_add, tasks.task_update, tasks.task_list):
        monkeypatch.setitem(tool_surface.registry(), fn.__name__, fn)
    inputs = {"objective": objective}
    if template == "adversarial-review":
        inputs["head"] = "a" * 40
    state = workflow.workflow_run("starter-example", inputs=inputs)
    assert state["frontier"]["actor_role"] == "lead"
    assert state["status"] == "waiting"
    token = config.agent_identity_var.set(LABELS[0])
    try:
        for _ in range(2):
            task = tasks.task_claim(
                state["frontier"]["task_id"], LABELS[0], expected_lane=LABELS[0]
            )
            state = workflow.workflow_run(
                "starter-example",
                instance_id=state["instance_id"],
                closure={
                    "exit": "handoff",
                    "reason": "handed_off_to",
                    "target": "lead",
                    "task_id": task["id"],
                    "expected_updated_at": task["updated_at"],
                },
            )
        queued = tasks.task_list(status="open")["tasks"]
        workers = [t for t in queued if t["lane"] in LABELS[1:3]]
        assert len(workers) == 2
        assert {t["lane"] for t in workers} == set(LABELS[1:3])
        assert all(objective in t["notes"] and objective == t["title"] for t in workers)
        assert state["frontier"]["actor_role"] == "lead"
        assert len(state["trail"]) == 2
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize("template", ["implementation-pair", "conveyor"])
def test_resulting_builder_commit_reaches_review_before_effect(starter_env, monkeypatch, template):
    monkeypatch.setattr(approvals, "check_approval_gate", lambda *a, **k: {"status": "approved"})
    team_templates.install(**params(template))
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *a, **k: None)
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: True)
    effects = []

    def listed(**kwargs):
        effects.append(kwargs)
        return tasks.task_list(**kwargs)

    for fn in (tasks.task_add, tasks.task_update):
        monkeypatch.setitem(tool_surface.registry(), fn.__name__, fn)
    monkeypatch.setitem(tool_surface.registry(), "task_list", listed)
    base, resulting = "a" * 40, "b" * 40
    with pytest.raises(ToolError, match="immutable commit SHA"):
        workflow.workflow_run(
            "starter-example", inputs={"objective": "Implement acceptance", "head": "main"}
        )
    state = workflow.workflow_run(
        "starter-example", inputs={"objective": "Implement acceptance", "head": base}
    )
    generated = workflow._load_definition("starter-example")
    for index, step in enumerate(generated["steps"][:-1]):
        label = state["frontier"]["agent"]
        token = config.agent_identity_var.set(label)
        try:
            task = tasks.task_claim(state["frontier"]["task_id"], label, expected_lane=label)
            closure = {
                "exit": "handoff",
                "reason": "handed_off_to",
                "target": generated["steps"][index + 1]["actor_role"],
                "task_id": task["id"],
                "expected_updated_at": task["updated_at"],
            }
            if step["actor_role"] == "builder":
                assert "Starting commit (context): " + base in task["notes"]
                assert "Exact review head:" not in task["notes"]
                before = len(effects)
                for evidence in (None, "commit:main", "commit:" + resulting + " extra"):
                    if evidence is not None:
                        task = tasks.task_update(task["id"], evidence_ref=evidence)
                        closure["expected_updated_at"] = task["updated_at"]
                    with pytest.raises(ToolError, match="full resulting SHA"):
                        workflow.workflow_run(
                            "starter-example", instance_id=state["instance_id"], closure=closure
                        )
                    assert len(effects) == before
                    assert (
                        workflow.workflow_run("starter-example", instance_id=state["instance_id"])[
                            "frontier"
                        ]["task_id"]
                        == task["id"]
                    )
                task = tasks.task_update(task["id"], evidence_ref="commit:" + resulting)
                closure["expected_updated_at"] = task["updated_at"]
            state = workflow.workflow_run(
                "starter-example", instance_id=state["instance_id"], closure=closure
            )
        finally:
            config.agent_identity_var.reset(token)
    reviewer = tasks.task_list(status="open", lane=state["frontier"]["agent"])["tasks"][0]
    assert "Exact review head: " + resulting in reviewer["notes"]
    assert "Exact review head: " + base not in reviewer["notes"]
    assert "Implement acceptance" in reviewer["notes"]
    # Resume from durable state, including the new review head.
    with storage.connect() as conn:
        persisted = json.loads(
            conn.execute(
                "SELECT spec FROM workflow_instances WHERE id=?", (state["instance_id"],)
            ).fetchone()["spec"]
        )
    assert "Exact review head: " + resulting in persisted["steps"][-1]["objective"]


def test_http_catalog_preview_and_install_authority(starter_env, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from unified_agent import server
    from unified_agent.webui.routes import web_routes

    monkeypatch.setenv(
        "UA_AGENT_TOKENS", "gcp_agent5:starter-test-owner,Desktop_Agent4:starter-test-reader"
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    headers = {"Authorization": "Bearer starter-test-owner"}
    with TestClient(server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))) as client:
        response = client.get("/api/skills-teams", headers=headers)
        assert response.status_code == 200
        assert {t["id"] for t in response.json()["starter_templates"]} == set(team_templates.IDS)
        malformed = params()
        malformed["trust_preset"] = []
        response = client.post(
            "/api/action/skills-teams",
            headers=headers,
            json={"action": "starter_preview", "params": malformed},
        )
        assert response.status_code == 400
        assert "configured trust preset" in response.json()["error"]
        response = client.post(
            "/api/action/skills-teams",
            headers=headers,
            json={"action": "starter_preview", "params": params()},
        )
        assert response.status_code == 200
        assert response.json()["plan"]["ready"] is False
        before = starter_env.read_bytes()
        response = client.post(
            "/api/action/skills-teams",
            headers=headers,
            json={"action": "starter_install", "params": params()},
        )
        assert response.status_code == 202 and response.json()["pending"]
        assert starter_env.read_bytes() == before
        response = client.post(
            "/api/action/skills-teams",
            headers={"Authorization": "Bearer starter-test-reader"},
            json={"action": "starter_install", "params": params()},
        )
        assert response.status_code == 403
        assert starter_env.read_bytes() == before


def test_direct_preview_obeys_policy_and_missing_identity(starter_env):
    from unified_agent import skills_registry
    from unified_agent.policy import PolicyViolation

    data = json.loads(starter_env.read_text())
    data["default_action"] = "deny"
    starter_env.write_text(json.dumps(data))
    before = starter_env.read_bytes()
    with pytest.raises(PolicyViolation):
        team_templates.preview(**params())
    with pytest.raises(PolicyViolation):
        skills_registry.skill_registry("starter_preview", params())
    assert starter_env.read_bytes() == before
    token = config.agent_identity_var.set(None)
    try:
        with pytest.raises(ToolError, match="authenticated identity"):
            team_templates.preview(**params())
    finally:
        config.agent_identity_var.reset(token)


def test_stale_starter_listing_keeps_ordinary_workflows(starter_env, monkeypatch):
    monkeypatch.setattr(approvals, "check_approval_gate", lambda *a, **k: {"status": "approved"})
    team_templates.install(**params())
    monkeypatch.setattr(team_templates.trust_presets, "selection", lambda _label: "inspection")
    listed = workflow.workflow_list()["workflows"]
    assert any(row["name"] == "smoke-echo" for row in listed)
    unavailable = next(row for row in listed if row["name"] == "starter-example")
    assert "already bound" in unavailable["error"]
    with pytest.raises(ToolError, match="already bound"):
        workflow._load_definition("starter-example")


def test_policy_write_during_candidate_validation_is_preserved(starter_env, monkeypatch):
    original = config_revisions._validate_policy_text
    gates = []

    def raced(text, **kwargs):
        original(text, **kwargs)
        data = json.loads(starter_env.read_text())
        data["default_action"] = "deny"
        starter_env.write_text(json.dumps(data))

    monkeypatch.setattr(config_revisions, "_validate_policy_text", raced)
    monkeypatch.setattr(approvals, "check_approval_gate", lambda *a, **k: gates.append(a))
    with pytest.raises(ToolError, match="changed during validation"):
        team_templates.install(**params())
    assert json.loads(starter_env.read_text())["default_action"] == "deny"
    assert "starter_template" not in starter_env.read_text()
    assert gates == []


@pytest.mark.parametrize(
    "template,role,harness",
    [
        ("implementation-pair", "checker", " CODEX "),
        ("adversarial-review", "reviewer_b", " CLAUDE "),
    ],
)
def test_normalized_duplicate_harnesses_refuse_before_approval(
    starter_env, monkeypatch, template, role, harness
):
    data = params(template)
    data["bindings"][role]["harness"] = harness
    before = starter_env.read_bytes()
    gates = []
    monkeypatch.setattr(approvals, "check_approval_gate", lambda *a, **k: gates.append(a))
    for action in (team_templates.preview, team_templates.install):
        with pytest.raises(ToolError, match="different harnesses"):
            action(**data)
    assert starter_env.read_bytes() == before
    assert gates == []


@pytest.mark.parametrize("stage", ["postapproval-prepare", "commit-validation", "write-validation"])
def test_approved_base_survives_late_prepare_and_writer_validation(starter_env, monkeypatch, stage):
    original_prepare = team_templates.prepare
    original_validate = config_revisions._validate_policy_text
    prepare_calls = validate_calls = 0
    edits = []

    def external_edit(*, revisioned=False):
        data = json.loads(starter_env.read_text())
        data["default_action"] = "deny"
        if revisioned:
            # A separate supported writer commits its own action before the
            # install reaches the final writer. Keep its execution scope apart.
            with approvals.approval_invocation():
                config_revisions.commit("policy", data)
        else:
            starter_env.write_text(json.dumps(data))
        edits.append(starter_env.read_bytes())

    def prepared(**kwargs):
        nonlocal prepare_calls
        result = original_prepare(**kwargs)
        prepare_calls += 1
        if stage == "postapproval-prepare" and prepare_calls == 2:
            external_edit(revisioned=True)
        return result

    def validated(text, *, conn=None):
        nonlocal validate_calls
        original_validate(text, conn=conn)
        if conn is None:
            validate_calls += 1
        if (stage == "commit-validation" and conn is None and validate_calls == 2) or (
            stage == "write-validation" and conn is not None
        ):
            external_edit()

    monkeypatch.setattr(team_templates, "prepare", prepared)
    monkeypatch.setattr(config_revisions, "_validate_policy_text", validated)
    monkeypatch.setattr(approvals, "check_approval_gate", lambda *a, **k: {"status": "approved"})
    with pytest.raises(ToolError, match="changed since the approved base"):
        team_templates.install(**params())
    assert len(edits) == 1
    assert starter_env.read_bytes() == edits[0]
    assert "starter_template" not in starter_env.read_text()
    assert config_revisions.list_revisions("policy")["count"] == (
        1 if stage == "postapproval-prepare" else 0
    )


@pytest.mark.parametrize("stage", ["before-capture", "capture-validation"])
def test_fenced_unchanged_policy_cannot_capture_a_different_live_file(
    starter_env, monkeypatch, stage
):
    document, raw = config_revisions._read_policy_file()
    base = config_revisions._raw_sha(raw)
    original_append = config_revisions._append_live
    original_validate = config_revisions._validate_team_block
    edits = []

    def external_edit():
        data = json.loads(starter_env.read_text())
        data["default_action"] = "deny"
        starter_env.write_text(json.dumps(data))
        edits.append(starter_env.read_bytes())

    def append(*args, **kwargs):
        if stage == "before-capture":
            external_edit()
        return original_append(*args, **kwargs)

    def validated(text):
        original_validate(text)
        if stage == "capture-validation":
            external_edit()

    monkeypatch.setattr(config_revisions, "_append_live", append)
    monkeypatch.setattr(config_revisions, "_validate_team_block", validated)
    with pytest.raises(ToolError, match="changed since the approved base"):
        config_revisions.commit("policy", document, expected_base_sha256=base)
    assert len(edits) == 1
    assert starter_env.read_bytes() == edits[0]
    assert config_revisions.list_revisions("policy")["count"] == 0


def test_crlf_policy_installs_and_fenced_unchanged_capture_preserves_bytes(
    starter_env, monkeypatch
):
    document = json.loads(starter_env.read_text())
    starter_env.write_bytes(config_revisions._dump(document).replace("\n", "\r\n").encode())
    assert b"\r\n" in starter_env.read_bytes()
    monkeypatch.setattr(approvals, "check_approval_gate", lambda *a, **k: {"status": "approved"})
    assert team_templates.install(**params())["installed"]
    # The unchanged/capture contract uses the same established text snapshot
    # hash, without rewriting a valid file's original newline representation.
    starter_env.write_bytes(starter_env.read_text().replace("\n", "\r\n").encode())
    before = starter_env.read_bytes()
    document, raw = config_revisions._read_policy_file()
    captured = config_revisions.commit(
        "policy", document, expected_base_sha256=config_revisions._raw_sha(raw)
    )
    assert captured["unchanged"]
    assert team_templates.install(**params())["unchanged"]
    assert starter_env.read_bytes() == before
