"""Team specs: validate, plan and boot a declared team (task #1736)."""

from __future__ import annotations

import copy
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    config,
    config_revisions,
    events,
    node_queue,
    server,
    storage,
    team_spec,
)
from unified_agent.tools import graph
from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve
from unified_agent.webui.routes import web_routes

ROOT = "/home/dev-user/dev"


# A bearer for the human resolver, so approving a high evidence tier request
# (config_revision_*, task #1672) can carry its signed producer stamp.
HUMAN_BEARER = f"Desktop_Person:tok-human-{secrets.token_urlsafe(8)}"


@pytest.fixture(autouse=True)
def _human_bearer(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", HUMAN_BEARER)


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "policy.yaml"
    path.write_text("version: 1\nenabled: true\ndefault_action: allow\nrules: []\n")
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    return path


@pytest.fixture
def org_file(tmp_path, monkeypatch):
    path = tmp_path / "org.yaml"
    path.write_text(
        "schema_version: 1\n"
        "offices:\n"
        "  - id: founder-ceo\n"
        "    name: Founder\n"
        "    lead: Desktop_Person\n"
        "    members: [Desktop_Person]\n"
        "  - id: platform-engineering\n"
        "    name: Platform Engineering\n"
        "    lead: Desktop_Agent1\n"
        "    members: [Desktop_Agent1, Desktop_Agent2]\n"
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(path))
    return path


@pytest.fixture
def launcher():
    calls: list[dict] = []

    class Double:
        fail_for: set[str] = set()

        def launch(self, seat, *, prompt, team_run_id, actor):
            if seat["label"] in self.fail_for:
                raise RuntimeError(f"node refused {seat['label']}")
            calls.append(
                {"seat": seat, "prompt": prompt, "team_run_id": team_run_id, "actor": actor}
            )
            return {"item_id": f"nd-{len(calls):016x}", "node": seat["node"], "status": "queued"}

    double = Double()
    double.calls = calls
    team_spec.set_launcher_factory(lambda: double)
    yield double
    team_spec.set_launcher_factory(None)


def _repo(name: str = "unified_agent", realpath: str | None = None) -> dict:
    path = f"{ROOT}/{name}"
    return {
        "root": ROOT,
        "root_realpath": ROOT,
        "name": name,
        "path": path,
        "realpath": realpath or path,
    }


def _register(
    node: str,
    agent: str,
    backends: list[str],
    roots=(ROOT,),
    repos=None,
    harness_config=None,
):
    facts = {
        "backends": backends,
        "node_features": ["session_settings", "cwd_realpath"],
        "worktree_roots": [{"path": root, "exists": True} for root in roots],
        "repos": [_repo()] if repos is None else repos,
    }
    if harness_config is not None:
        facts["harness_config"] = harness_config
    node_queue.register(node, identity=agent, device_node="desktop", facts=facts)


@pytest.fixture
def nodes(org_file, policy_file):
    storage.init_db()
    _register("desktop-agent1", "Desktop_Agent1", ["acp:claude"])
    _register("desktop-agent2", "Desktop_Agent2", ["acp:codex"])


def _spec() -> dict:
    return {
        "description": "Builder and checker on different harnesses",
        "operating_profile": "standard",
        "guidance": "Ship small PRs.",
        "seats": [
            {
                "label": "Desktop_Agent1",
                "harness": "claude",
                "model": "opus",
                "host": "desktop",
                "workspace": {"repo": "unified_agent", "worktree": "task"},
                "guidance": "You review the exact head.",
            },
            {
                "label": "Desktop_Agent2",
                "harness": "codex",
                "model": "gpt-5-codex",
                "host": "desktop",
                "workspace": {"repo": "unified_agent", "worktree": "task"},
            },
        ],
        "edges": [
            {"from": "Desktop_Agent1", "to": "Desktop_Agent2", "type": "delegates_to"},
            {"from": "Desktop_Agent1", "to": "Desktop_Agent2", "type": "reviews"},
            {"from": "Desktop_Agent2", "to": "Desktop_Person", "type": "escalates_to"},
        ],
    }


def _commit(spec: dict | None = None, team: str = "platform-engineering") -> dict:
    with _as("Desktop_Agent5"):
        return team_spec.team_spec_commit(team, spec or _spec())


# --- validator refusals --------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s["seats"][0].update(harness="tmux"), "harness must be one of"),
        (lambda s: s["seats"][0].update(shell="bash"), "unknown key"),
        (lambda s: s["seats"].append(copy.deepcopy(s["seats"][0])), "repeats a label"),
        (lambda s: s.update(seats=[]), "non-empty list"),
        (lambda s: s["seats"][0].update(model="bad model!"), "model is invalid"),
        (lambda s: s["seats"][0]["workspace"].update(repo="~/dev/x"), "without '~'"),
        (lambda s: s["seats"][0]["workspace"].update(repo="../etc"), "without '~'"),
        (lambda s: s["seats"][0]["workspace"].update(worktree="yolo"), "worktree must be"),
        (
            lambda s: s["edges"].append({"from": "X_Y", "to": "Desktop_Agent2", "type": "reviews"}),
            "not a seat",
        ),
        (
            lambda s: s["edges"].append(
                {"from": "Desktop_Agent2", "to": "Desktop_Agent2", "type": "reviews"}
            ),
            "itself",
        ),
        (
            lambda s: s["edges"].append(
                {"from": "Desktop_Agent2", "to": "Desktop_Agent1", "type": "spawns"}
            ),
            "type must be",
        ),
        (
            lambda s: s["edges"].append(
                {"from": "Desktop_Agent2", "to": "Desktop_Agent1", "type": "delegates_to"}
            ),
            "cycle",
        ),
        (lambda s: s.update(mission_id="1450"), "mission_id must be an integer"),
        (lambda s: s.update(operating_profile="Has Spaces"), "operating_profile is invalid"),
        (lambda s: s["seats"][0].update(skills=["Not_Kebab"]), "skills entry is invalid"),
    ],
)
def test_validator_refuses_malformed_specs(mutate, message):
    spec = _spec()
    mutate(spec)
    with pytest.raises(team_spec.TeamSpecError, match=message):
        team_spec.parse_spec("platform-engineering", spec)


def test_launch_order_starts_delegates_before_their_delegator():
    parsed = team_spec.parse_spec("platform-engineering", _spec())
    assert parsed["launch_order"] == ["Desktop_Agent2", "Desktop_Agent1"]


def test_commit_refuses_unknown_office_and_label(nodes):
    with pytest.raises(ToolError, match="not an org.yaml office"):
        _commit(team="no-such-office")
    spec = _spec()
    spec["seats"][1]["label"] = "Desktop_Ghost"
    spec["edges"] = []
    with pytest.raises(ToolError, match="not an org.yaml roster label"):
        _commit(spec)


def test_commit_requires_config_revision_authority(nodes, monkeypatch):
    from unified_agent import profiles

    monkeypatch.setattr(profiles, "tool_is_allowed", lambda name: False)
    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="config_revision_manage"):
        team_spec.team_spec_commit("platform-engineering", _spec())


# --- revision round-trip -------------------------------------------------------


def test_spec_round_trips_through_policy_revisions(nodes, policy_file):
    first = _commit()
    assert first["kind"] == "policy" and first["revision"] >= 1
    stored = yaml.safe_load(policy_file.read_text())
    assert stored["teams"]["platform-engineering"]["seats"][1]["harness"] == "codex"
    assert stored["default_action"] == "allow"  # the rest of the policy is untouched

    changed = _spec()
    changed["seats"][1]["model"] = "gpt-5"
    second = _commit(changed)
    assert second["revision"] == first["revision"] + 1
    assert second["spec_sha256"] != first["spec_sha256"]
    shown = config_revisions.show_revision("policy", second["revision"])
    assert shown["document"]["teams"]["platform-engineering"]["seats"][1]["model"] == "gpt-5"
    diff = config_revisions.diff_revisions("policy", first["revision"], second["revision"])
    assert "gpt-5" in diff["diff"]

    # Rollback is approval-gated like every other config kind.
    with _as("Desktop_Agent5"), pytest.raises(ApprovalPendingError) as pending:
        config_revisions.rollback("policy", first["revision"])
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="restore team")
    with _as("Desktop_Agent5"):
        config_revisions.rollback("policy", first["revision"])
    assert team_spec.load_specs()["platform-engineering"]["seats"][1]["model"] == "gpt-5-codex"


def test_hand_edited_bad_teams_block_fails_policy_commit(nodes):
    with _as("Desktop_Agent5"), pytest.raises(ToolError, match="harness must be one of"):
        config_revisions.commit(
            "policy",
            {
                "version": 1,
                "enabled": True,
                "default_action": "allow",
                "rules": [],
                "teams": {"platform-engineering": {"seats": [{"label": "X", "harness": "vim"}]}},
            },
        )


# --- plan ----------------------------------------------------------------------


def test_plan_is_ready_stable_and_diffable(nodes):
    _commit()
    with _as("Desktop_Agent1"):
        first = team_spec.team_up(team="platform-engineering")
        second = team_spec.team_up(team="platform-engineering")
    assert first == second
    assert first["ready"] is True, first["refusals"]
    assert first["launch_order"] == ["Desktop_Agent2", "Desktop_Agent1"]
    codex, claude = first["seats"]
    assert codex["node"] == "desktop-agent2" and codex["backend"] == "acp:codex"
    assert claude["workspace"]["cwd"] == f"{ROOT}/unified_agent"
    assert claude["guidance"] == "Ship small PRs.\n\nYou review the exact head."
    assert claude["edges"]["outgoing"] == [
        {"type": "delegates_to", "to": "Desktop_Agent2"},
        {"type": "reviews", "to": "Desktop_Agent2"},
    ]
    assert first["operating_profile"]["status"] == "unvalidated"
    assert "recorded unvalidated" in first["operating_profile"]["note"]
    assert "READY" in first["plan_text"]
    assert "unvalidated" in first["plan_text"]
    assert "timestamp" not in first["plan_text"]


def test_plan_accepts_an_inline_draft_without_committing(nodes, policy_file):
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(team="platform-engineering", spec=_spec())
    assert plan["ready"] is True
    assert "teams" not in yaml.safe_load(policy_file.read_text())


def _codes(plan: dict) -> set[str]:
    return {row["code"] for row in plan["refusals"]}


def test_plan_refuses_harness_unavailable_on_host(nodes):
    spec = _spec()
    spec["seats"][1]["harness"] = "gemini"
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    assert plan["ready"] is False
    assert "harness_unavailable_on_host" in _codes(plan)


def test_plan_refuses_unknown_label_host_and_untrusted_workspace(nodes):
    spec = _spec()
    spec["seats"][0]["workspace"]["repo"] = "/etc/project"
    spec["seats"][1]["host"] = "laptop"
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    assert {"workspace", "unknown_host"} <= _codes(plan)

    spec = _spec()
    spec["seats"][1]["label"] = "Desktop_Ghost"
    spec["edges"] = []
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    assert {"unknown_label", "host_not_bound_to_label"} <= _codes(plan)


def test_plan_refuses_offline_node(nodes):
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE nodes SET last_heartbeat='2020-01-01T00:00:00+00:00' WHERE node=?",
            ("desktop-agent2",),
        )
        conn.commit()
    finally:
        conn.close()
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=_spec(), team="platform-engineering")
    assert "host_offline" in _codes(plan)


def test_plan_refuses_node_with_future_dated_heartbeat(nodes):
    """A heartbeat stamped an hour ahead does not make the node live (#1997)."""
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    conn = storage.connect()
    try:
        conn.execute("UPDATE nodes SET last_heartbeat=? WHERE node=?", (future, "desktop-agent2"))
        conn.commit()
    finally:
        conn.close()
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=_spec(), team="platform-engineering")
    assert "host_offline" in _codes(plan)


def test_plan_refuses_wip_cap_and_budget(nodes, policy_file, monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "1")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (title, status, claimed_by, created_at, updated_at) "
            "VALUES ('held', 'doing', 'Desktop_Agent2', ?, ?)",
            (storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=_spec(), team="platform-engineering")
    refused = [row for row in plan["refusals"] if row["code"] == "wip_cap"]
    assert [row["seat"] for row in refused] == ["Desktop_Agent2"]

    monkeypatch.setenv("UA_AGENT_WIP_MAX", "5")
    conn = storage.connect()
    try:
        # An in-flight run holds its reserve against the budget.
        conn.execute(
            "INSERT INTO tasks (title, status, claimed_by, created_at, updated_at) "
            "VALUES ('held', 'doing', 'Desktop_Agent1', ?, ?)",
            (storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    policy_file.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules: []\n"
        "budgets:\n  - name: claude-zero\n    agents: [Desktop_Agent1]\n"
        "    period: month\n    limit_usd: 2\n    run_reserve_usd: 1.5\n    action: block\n"
    )
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=_spec(), team="platform-engineering")
    assert [row["seat"] for row in plan["refusals"] if row["code"] == "budget"] == [
        "Desktop_Agent1"
    ]


def test_plan_refuses_trust_preset_and_paused_agent(nodes, monkeypatch):
    from unified_agent import agent_lifecycle, run_records

    monkeypatch.setattr(
        run_records,
        "native_execution_status",
        lambda agent, originator=None: {
            "native_execution_allowed": agent != "Desktop_Agent2",
            "native_execution_denial": "trust preset requires unsupported native containment",
        },
    )
    monkeypatch.setattr(
        agent_lifecycle,
        "claim_block",
        lambda conn, agent: (
            "agent 'Desktop_Agent1' is paused" if agent == "Desktop_Agent1" else None
        ),
    )
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=_spec(), team="platform-engineering")
    assert {(r["seat"], r["code"]) for r in plan["refusals"]} >= {
        ("Desktop_Agent2", "trust_preset"),
        ("Desktop_Agent1", "lifecycle"),
    }


def test_plan_refuses_unpublished_skills(nodes):
    spec = _spec()
    spec["skills"] = ["review-code"]
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    assert "skill_not_published" in _codes(plan)


def test_plan_requires_skills_applied_on_the_node(nodes):
    from unified_agent import harness_config

    harness_config.publish_bundle(
        {"version": "v1", "skills": [{"name": "review-code", "body": "Review."}]},
        actor="Desktop_Agent5",
    )
    spec = _spec()
    spec["skills"] = ["review-code"]
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    refused = [r for r in plan["refusals"] if r["code"] == "skill_not_applied"]
    assert {r["seat"] for r in refused} == {"Desktop_Agent1", "Desktop_Agent2"}
    assert "run harness_config apply" in refused[0]["detail"]

    applied = {
        "bundle_version": "v1",
        "in_sync": ["codex"],
        "skill_surfaces": ["codex"],
        "skills": ["review-code"],
    }
    _register("desktop-agent1", "Desktop_Agent1", ["acp:claude"], harness_config=applied)
    _register("desktop-agent2", "Desktop_Agent2", ["acp:codex"], harness_config=applied)
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    # Only the codex surface is synced and it delivers skills; a Claude seat on
    # the same node would not receive them, so it is not ready.
    assert [(r["seat"], r["code"]) for r in plan["refusals"]] == [
        ("Desktop_Agent1", "skill_not_applied")
    ]
    assert "not to harness claude" in plan["refusals"][0]["detail"]
    codex_only = _spec()
    codex_only["seats"][1]["skills"] = ["review-code"]
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=codex_only, team="platform-engineering")
    assert plan["ready"] is True, plan["refusals"]

    stale = {**applied, "bundle_version": "v0"}
    _register("desktop-agent2", "Desktop_Agent2", ["acp:codex"], harness_config=stale)
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=codex_only, team="platform-engineering")
    assert [r["seat"] for r in plan["refusals"]] == ["Desktop_Agent2"]


def test_unverified_skill_reports_do_not_satisfy_readiness(nodes):
    """Skills with no matching in-sync, skill-bearing surface are dropped by the hub."""
    from unified_agent import harness_config
    from unified_agent.node_registry import bounded_config_state, list_nodes

    harness_config.publish_bundle(
        {"version": "v1", "skills": [{"name": "review-code", "body": "Review."}]},
        actor="Desktop_Agent5",
    )
    bare = {"bundle_version": "v1", "in_sync": ["claude"], "skills": ["review-code"]}
    assert bounded_config_state(bare)["skills"] == []
    # A skill surface the node did not list as in sync is not evidence either.
    liar = {**bare, "skill_surfaces": ["codex"]}
    assert bounded_config_state(liar) == {
        "bundle_version": "v1",
        "in_sync": ["claude"],
        "skill_surfaces": [],
        "skills": [],
    }
    _register("desktop-agent2", "Desktop_Agent2", ["acp:codex"], harness_config=bare)
    assert list_nodes()["desktop-agent2"]["harness_config"]["skills"] == []
    spec = _spec()
    spec["seats"][1]["skills"] = ["review-code"]
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    assert [(r["seat"], r["code"]) for r in plan["refusals"]] == [
        ("Desktop_Agent2", "skill_not_applied")
    ]


def test_plan_refuses_hermes_seats(nodes):
    _register("desktop-agent1", "Desktop_Agent1", ["acp:claude", "acp:hermes"])
    spec = _spec()
    spec["seats"][0]["harness"] = "hermes"
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    assert [r["seat"] for r in plan["refusals"] if r["code"] == "hermes_profile_unsupported"] == [
        "Desktop_Agent1"
    ]


def test_workspace_must_be_a_node_reported_checkout_inside_its_root(nodes):
    # A symlinked checkout whose real path leaves the root is refused.
    escaping = _repo(realpath="/etc/elsewhere")
    _register("desktop-agent2", "Desktop_Agent2", ["acp:codex"], repos=[escaping])
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=_spec(), team="platform-engineering")
    refused = [r for r in plan["refusals"] if r["seat"] == "Desktop_Agent2"]
    assert [r["code"] for r in refused] == ["workspace"]
    assert "outside its root" in refused[0]["detail"]

    # A checkout the node did not report is unverified, not trusted by name.
    _register("desktop-agent2", "Desktop_Agent2", ["acp:codex"], repos=[])
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=_spec(), team="platform-engineering")
    assert {r["code"] for r in plan["refusals"] if r["seat"] == "Desktop_Agent2"} == {
        "workspace_unverified"
    }

    # Absolute paths resolve the same way.
    _register("desktop-agent2", "Desktop_Agent2", ["acp:codex"])
    spec = _spec()
    spec["seats"][1]["workspace"]["repo"] = f"{ROOT}/unified_agent"
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    assert plan["ready"] is True, plan["refusals"]


def test_harness_vocabulary_matches_the_acp_matrix():
    from unified_agent import acp_matrix, team_policy

    assert set(team_policy.HARNESSES) == set(acp_matrix.ACP_HARNESSES)
    assert team_policy.MAX_POLICY_BYTES == config_revisions.MAX_RAW_BYTES


def test_team_reads_obey_the_policy_size_limit_and_cache(nodes, policy_file, monkeypatch):
    from unified_agent import team_policy

    _commit()
    calls = {"n": 0}
    real = team_policy.parse_block

    def counting(raw):
        calls["n"] += 1
        return real(raw)

    monkeypatch.setattr(team_policy, "parse_block", counting)
    team_spec.load_specs()
    team_spec.team_list()
    assert calls["n"] <= 1  # unchanged file: parsed at most once
    policy_file.write_text(
        policy_file.read_text() + "#" + "x" * team_policy.MAX_POLICY_BYTES + "\n"
    )
    with pytest.raises(ToolError, match="exceeds"):
        team_spec.team_list()


def test_policy_cache_ignores_a_read_that_raced_a_writer(nodes, policy_file, monkeypatch):
    """A read that saw another version is never cached under the earlier stat key."""
    import os
    import pathlib

    from unified_agent import team_policy

    spec = _spec()
    spec["description"] = "aaaa"
    _commit(spec)
    version_a = policy_file.read_text()
    version_b = version_a.replace("aaaa", "bbbb")
    assert len(version_a) == len(version_b)
    before = policy_file.stat()
    team_policy._cache.update(key=None, specs={})

    real_read = pathlib.Path.read_bytes
    raced = {"done": False}

    def racing_read(self):
        if self == policy_file and not raced["done"]:
            raced["done"] = True
            policy_file.write_text(version_b)  # a writer lands between stat and read
        return real_read(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", racing_read)
    first = team_spec.load_specs()
    monkeypatch.setattr(pathlib.Path, "read_bytes", real_read)
    assert first["platform-engineering"]["description"] == "bbbb"

    # Roll the file back to A with its original size and mtime.
    policy_file.write_text(version_a)
    os.utime(policy_file, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert policy_file.stat().st_mtime_ns == before.st_mtime_ns
    assert team_spec.load_specs()["platform-engineering"]["description"] == "aaaa"


def test_capture_and_unchanged_commit_validate_the_teams_block(nodes, policy_file):
    bad = (
        "version: 1\nenabled: true\ndefault_action: allow\nrules: []\n"
        "teams:\n  platform-engineering:\n    seats:\n"
        "      - {label: Desktop_Ghost, harness: claude}\n"
    )
    policy_file.write_text(bad)
    with _as("Desktop_Agent5"), pytest.raises(ToolError, match="roster label"):
        config_revisions.capture("policy")
    with _as("Desktop_Agent5"), pytest.raises(ToolError, match="roster label"):
        config_revisions.commit("policy", yaml.safe_load(bad))
    assert config_revisions.list_revisions("policy")["count"] == 0


def test_plan_text_shows_what_the_approval_binds(nodes):
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=_spec(), team="platform-engineering")
    claude = next(s for s in plan["seats"] if s["label"] == "Desktop_Agent1")
    assert f"prompt={claude['prompt_sha256'][:16]}" in plan["plan_text"]
    assert "  | You review the exact head." in plan["plan_text"]


def test_plan_validates_operating_profile_when_module_is_configured(nodes, monkeypatch):
    import sys
    import types

    fake = types.ModuleType("unified_agent.operating_profiles")
    fake.load_config = lambda: types.SimpleNamespace(configured=True, profiles={"lean": object()})
    monkeypatch.setitem(sys.modules, "unified_agent.operating_profiles", fake)
    # Once the real module is installed (#1735) it is also bound on the package,
    # and ``from . import operating_profiles`` resolves that attribute first.
    import unified_agent

    monkeypatch.setattr(unified_agent, "operating_profiles", fake, raising=False)
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=_spec(), team="platform-engineering")
    assert plan["operating_profile"]["status"] == "unknown"
    assert "operating_profile" in _codes(plan)
    spec = _spec()
    spec["operating_profile"] = "lean"
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(spec=spec, team="platform-engineering")
    assert plan["operating_profile"]["status"] == "validated"


def test_default_action_review_mission_opens_a_review_instead_of_refusing(nodes, launcher):
    from unified_agent import autonomy_modes, goals

    with _as("Desktop_Person"):
        goal = goals.goal_add("Ship the team spec", author="Desktop_Person")["goal"]
    spec = _spec()
    spec["mission_id"] = int(goal["id"])
    _commit(spec)
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(team="platform-engineering")
    # A mission with no explicit mode is action-review: the plan is still
    # ready, and boot opens the mission review instead of refusing.
    assert plan["autonomy"]["decision"] == "pause_for_human"
    assert plan["ready"] is True, plan["refusals"]
    with _as("Desktop_Agent1"):
        paused = team_spec.team_up(team="platform-engineering", plan=False)
    assert paused["booted"] is False
    pending_id = paused["autonomy_review"]["pending_id"]
    assert pending_id
    with _as("Desktop_Person"):
        autonomy_modes.review_action(pending_id, "Desktop_Person")
    _approve_boot()
    with _as("Desktop_Agent1"):
        booted = team_spec.team_up(team="platform-engineering", plan=False)
    assert booted["booted"] is True, booted
    assert len(launcher.calls) == 2


# --- boot ----------------------------------------------------------------------


def _approve_boot(team: str = "platform-engineering") -> int:
    with _as("Desktop_Agent1"), pytest.raises(ApprovalPendingError) as pending:
        team_spec.team_up(team=team, plan=False)
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="boot the pair")
    return pending.value.approval_id


def test_two_seat_mixed_harness_team_boots_in_edge_order(nodes, launcher):
    _commit()
    approval_id = _approve_boot()
    with _as("Desktop_Agent1"):
        result = team_spec.team_up(team="platform-engineering", plan=False)
    assert result["booted"] is True
    assert result["approval_id"] == approval_id
    assert [call["seat"]["label"] for call in launcher.calls] == [
        "Desktop_Agent2",
        "Desktop_Agent1",
    ]
    assert [call["seat"]["backend"] for call in launcher.calls] == ["acp:codex", "acp:claude"]
    assert {call["team_run_id"] for call in launcher.calls} == {result["team_run_id"]}
    assert "seat Desktop_Agent1 on team platform-engineering" in launcher.calls[1]["prompt"]
    assert "you delegates_to Desktop_Agent2" in launcher.calls[1]["prompt"]
    assert [seat["outcome"] for seat in result["seats"]] == ["launched", "launched"]

    rows = events.query_events([team_spec.RUN_CHANNEL], limit=10)
    kinds = sorted(row["payload"]["kind"] for row in rows)
    assert kinds == ["seat_launched", "seat_launched", "team_run_started"]
    assert {row["payload"]["team_run_id"] for row in rows} == {result["team_run_id"]}

    # The approval was one-shot: a second boot needs a new one.
    with _as("Desktop_Agent1"), pytest.raises(ApprovalPendingError):
        team_spec.team_up(team="platform-engineering", plan=False)


def test_boot_refuses_a_plan_with_refusals_before_the_gate(nodes, launcher, monkeypatch):
    _commit()
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "1")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (title, status, claimed_by, created_at, updated_at) "
            "VALUES ('held', 'doing', 'Desktop_Agent1', ?, ?)",
            (storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    with _as("Desktop_Agent1"):
        result = team_spec.team_up(team="platform-engineering", plan=False)
    assert result["booted"] is False
    assert "wip_cap" in _codes(result["plan"])
    assert launcher.calls == []
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM approvals").fetchone()["n"] == 0
    finally:
        conn.close()


def test_boot_needs_a_committed_spec_and_a_fresh_plan(nodes, launcher):
    with _as("Desktop_Agent1"), pytest.raises(ToolError, match="committed spec only"):
        team_spec.team_up(team="platform-engineering", spec=_spec(), plan=False)
    _commit()
    with _as("Desktop_Agent1"):
        stale = team_spec.team_up(
            team="platform-engineering", plan=False, expected_plan_sha256="0" * 64
        )
    assert stale["booted"] is False and stale["stale_plan"] is True
    assert launcher.calls == []


def test_failed_seat_stops_later_seats_and_reports_each(nodes, launcher):
    _commit()
    launcher.fail_for = {"Desktop_Agent2"}
    _approve_boot()
    with _as("Desktop_Agent1"):
        result = team_spec.team_up(team="platform-engineering", plan=False)
    assert result["booted"] is False
    assert [(s["label"], s["outcome"]) for s in result["seats"]] == [
        ("Desktop_Agent2", "failed"),
        ("Desktop_Agent1", "not_started"),
    ]
    assert launcher.calls == []
    # Nothing launched, so the approval pass was given back.
    assert result["approval_restored"] is True
    with _as("Desktop_Agent1"):
        again = team_spec.team_up(team="platform-engineering", plan=False)
    assert again["seats"][0]["outcome"] == "failed"


def test_seat_refused_at_launch_is_skipped_and_reported(nodes, launcher, monkeypatch):
    """Readiness is re-read right before each enqueue, not taken from the preview."""
    _commit()
    _approve_boot()
    from unified_agent import agent_lifecycle

    real = agent_lifecycle.claim_block
    calls = {"n": 0}

    def paused_after_plan(conn, agent):
        # The boot's own plan sees Desktop_Agent2 active; the launch-time
        # recheck sees it paused.
        if agent == "Desktop_Agent2":
            calls["n"] += 1
            if calls["n"] > 1:
                return "agent 'Desktop_Agent2' is paused (by Desktop_Person)"
        return real(conn, agent)

    monkeypatch.setattr(agent_lifecycle, "claim_block", paused_after_plan)
    with _as("Desktop_Agent1"):
        result = team_spec.team_up(team="platform-engineering", plan=False)
    outcomes = {s["label"]: s for s in result["seats"]}
    assert outcomes["Desktop_Agent2"]["outcome"] == "refused"
    assert "lifecycle" in outcomes["Desktop_Agent2"]["reason"]
    assert outcomes["Desktop_Agent1"]["outcome"] == "not_started"
    assert launcher.calls == []


def test_independent_seat_still_launches_after_a_refusal(nodes, launcher, monkeypatch):
    spec = _spec()
    spec["edges"] = [{"from": "Desktop_Agent1", "to": "Desktop_Agent2", "type": "reviews"}]
    _commit(spec)
    _approve_boot()
    from unified_agent import budgets

    real = budgets.evaluate_claim_or_config_error
    seen = {"n": 0}

    def blocked_at_launch(conn, agent, **kw):
        if agent == "Desktop_Agent1":
            seen["n"] += 1
            if seen["n"] > 1:
                return budgets.ClaimDecision(agent=agent, blocked="budget hard stop: test")
        return real(conn, agent, **kw)

    monkeypatch.setattr(budgets, "evaluate_claim_or_config_error", blocked_at_launch)
    with _as("Desktop_Agent1"):
        result = team_spec.team_up(team="platform-engineering", plan=False)
    assert [(s["label"], s["outcome"]) for s in result["seats"]] == [
        ("Desktop_Agent1", "refused"),
        ("Desktop_Agent2", "launched"),
    ]
    assert [c["seat"]["label"] for c in launcher.calls] == ["Desktop_Agent2"]


def test_record_failures_never_strand_a_boot(nodes, launcher, monkeypatch):
    _commit()
    approval_id = _approve_boot()
    real = team_spec._record_run

    def flaky(kind, payload, actor, key):
        if kind == "seat_launched" and payload["label"] == "Desktop_Agent2":
            raise RuntimeError("ledger busy")
        return real(kind, payload, actor, key)

    monkeypatch.setattr(team_spec, "_record_run", flaky)
    with _as("Desktop_Agent1"):
        result = team_spec.team_up(team="platform-engineering", plan=False)
    assert [s["outcome"] for s in result["seats"]] == ["launched", "launched"]
    assert result["seats"][0]["record_error"] == "ledger busy"
    assert result["seats"][1]["run_ref"]
    assert result["approval_id"] == approval_id

    # If the run itself cannot be recorded, nothing launches and the pass returns.
    monkeypatch.setattr(
        team_spec, "_record_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    _approve_boot()
    with _as("Desktop_Agent1"):
        stranded = team_spec.team_up(team="platform-engineering", plan=False)
    assert stranded["booted"] is False and stranded["approval_restored"] is True
    assert "could not record" in stranded["reason"]
    assert len(launcher.calls) == 2


def _mission_spec() -> dict:
    from unified_agent import goals

    with _as("Desktop_Person"):
        goal = goals.goal_add("Ship the team spec", author="Desktop_Person")["goal"]
    spec = _spec()
    spec["mission_id"] = int(goal["id"])
    return spec


def test_globally_gated_team_up_opens_the_mission_approval_and_boots(nodes, launcher, monkeypatch):
    from unified_agent import autonomy_modes

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "team_up")
    _commit(_mission_spec())
    with _as("Desktop_Agent1"):
        paused = team_spec.team_up(team="platform-engineering", plan=False)
    review = paused["autonomy_review"]
    # The pause names the mission-bound approval request it opened, so there is
    # something for a human to approve (it used to open nothing).
    assert review["global_approval_status"] == "pending"
    assert str(review["global_approval_id"]) in paused["reason"]
    with _as("Desktop_Agent1"):
        again = team_spec.team_up(team="platform-engineering", plan=False)
    assert again["autonomy_review"]["global_approval_id"] == review["global_approval_id"]

    with _as("Desktop_Person"):
        with pytest.raises(ToolError, match="global approval must be live"):
            autonomy_modes.review_action(review["pending_id"], "Desktop_Person")
        approval_resolve(review["global_approval_id"], "approve", reason="open the mission")
        autonomy_modes.review_action(review["pending_id"], "Desktop_Person")
    _approve_boot()
    with _as("Desktop_Agent1"):
        booted = team_spec.team_up(team="platform-engineering", plan=False)
    assert booted["booted"] is True, booted
    assert len(launcher.calls) == 2


def test_boot_that_launches_nothing_keeps_the_mission_review(nodes, launcher, monkeypatch):
    from unified_agent import agent_lifecycle, autonomy_modes

    _commit(_mission_spec())
    with _as("Desktop_Agent1"):
        paused = team_spec.team_up(team="platform-engineering", plan=False)
    pending_id = paused["autonomy_review"]["pending_id"]
    with _as("Desktop_Person"):
        autonomy_modes.review_action(pending_id, "Desktop_Person")
    _approve_boot()

    real = agent_lifecycle.claim_block
    calls = {"n": 0}

    def paused_after_plan(conn, agent):
        if agent == "Desktop_Agent2":
            calls["n"] += 1
            if calls["n"] > 1:
                return "agent 'Desktop_Agent2' is paused (by Desktop_Person)"
        return real(conn, agent)

    monkeypatch.setattr(agent_lifecycle, "claim_block", paused_after_plan)
    with _as("Desktop_Agent1"):
        refused = team_spec.team_up(team="platform-engineering", plan=False)
    assert refused["booted"] is False and refused["approval_restored"] is True
    assert launcher.calls == []
    # No seat reached an enqueue, so the review was not spent.
    assert [
        row["id"] for row in autonomy_modes.preview(_mission_id(pending_id))["pending_not_started"]
    ] == [pending_id]

    monkeypatch.setattr(agent_lifecycle, "claim_block", real)
    with _as("Desktop_Agent1"):
        booted = team_spec.team_up(team="platform-engineering", plan=False)
    assert booted["booted"] is True, booted
    assert len(launcher.calls) == 2

    # Once a launch was attempted the review is spent: the next boot pauses again.
    with _as("Desktop_Agent1"):
        again = team_spec.team_up(team="platform-engineering", plan=False)
    assert again["autonomy_review"]["pending_id"] != pending_id


def _mission_id(pending_id: int) -> int:
    conn = storage.connect()
    try:
        return int(
            conn.execute(
                "SELECT mission_id FROM autonomy_pending_actions WHERE id=?", (pending_id,)
            ).fetchone()["mission_id"]
        )
    finally:
        conn.close()


def test_readiness_recheck_errors_become_seat_outcomes_and_return_the_pass(
    nodes, launcher, monkeypatch
):
    import sqlite3

    _commit()
    approval_id = _approve_boot()
    from unified_agent import budgets

    real = budgets.evaluate_claim_or_config_error
    seen = {"n": 0}

    def flaky(conn, agent, **kw):
        if agent == "Desktop_Agent2":
            seen["n"] += 1
            if seen["n"] > 1:
                raise sqlite3.OperationalError("database is locked")
        return real(conn, agent, **kw)

    monkeypatch.setattr(budgets, "evaluate_claim_or_config_error", flaky)
    with _as("Desktop_Agent1"):
        result = team_spec.team_up(team="platform-engineering", plan=False)
    assert result["booted"] is False
    assert [(s["label"], s["outcome"]) for s in result["seats"]] == [
        ("Desktop_Agent2", "failed"),
        ("Desktop_Agent1", "not_started"),
    ]
    assert "readiness recheck failed" in result["seats"][0]["reason"]
    assert result["approval_restored"] is True and result["approval_id"] == approval_id
    assert launcher.calls == []

    monkeypatch.setattr(budgets, "evaluate_claim_or_config_error", real)
    with _as("Desktop_Agent1"):
        retry = team_spec.team_up(team="platform-engineering", plan=False)
    assert retry["booted"] is True and retry["approval_id"] == approval_id


def test_restore_failure_is_reported_with_its_error(nodes, launcher, monkeypatch):
    _commit()
    launcher.fail_for = {"Desktop_Agent2"}
    _approve_boot()
    monkeypatch.setattr(team_spec, "_restore", lambda approval_id: "ledger locked")
    with _as("Desktop_Agent1"):
        result = team_spec.team_up(team="platform-engineering", plan=False)
    assert result["approval_restored"] is False
    assert result["approval_restore_error"] == "ledger locked"


# --- seat view and MCP surface -------------------------------------------------


def test_agent_reads_its_own_seat_team_and_edges(nodes, launcher):
    _commit()
    with _as("Desktop_Agent2"):
        seat = graph.coordination_manage("team_seat")
        with pytest.raises(ToolError, match="only your own seat"):
            team_spec.team_seat("Desktop_Agent1")
    assert seat["agent"] == "Desktop_Agent2"
    (row,) = seat["seats"]
    assert row["team"] == "platform-engineering"
    assert row["edges"]["incoming"] == [
        {"type": "delegates_to", "from": "Desktop_Agent1"},
        {"type": "reviews", "from": "Desktop_Agent1"},
    ]
    assert row["edges"]["outgoing"] == [{"type": "escalates_to", "to": "Desktop_Person"}]
    from unified_agent.tools.profiles import profile_status

    with _as("Desktop_Agent2"):
        status = profile_status()
    assert status["team_seats"][0]["team"] == "platform-engineering"
    with _as("Desktop_Person"):
        assert "team_seats" not in profile_status()


def test_coordination_manage_exposes_plan_and_list(nodes):
    _commit()
    with _as("Desktop_Agent1"):
        listed = graph.coordination_manage("team_list")
        plan = graph.coordination_manage("team_up", {"team": "platform-engineering"})
    assert listed["teams"][0]["seats"][1] == {
        "label": "Desktop_Agent2",
        "harness": "codex",
        "model": "gpt-5-codex",
    }
    assert plan["ready"] is True


def test_node_seat_launcher_queues_an_acp_item(nodes):
    _commit()
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(team="platform-engineering")
    seat = plan["seats"][0]
    spec = team_spec.load_specs()["platform-engineering"]
    queued = team_spec.NodeSeatLauncher().launch(
        seat,
        prompt=team_spec.startup_prompt(spec, seat),
        team_run_id="tr-x",
        actor="Desktop_Agent1",
    )
    item = node_queue.get_item(queued["item_id"])
    assert item["backend"] == "acp:codex"
    assert item["cwd"] == f"{ROOT}/unified_agent"
    assert item["cwd_realpath"] == f"{ROOT}/unified_agent"
    assert item["model"] == "gpt-5-codex"


def test_old_node_cannot_plan_a_bound_checkout(nodes):
    _commit()
    node_queue.register(
        "desktop-agent2",
        identity="Desktop_Agent2",
        device_node="desktop",
        facts={
            "backends": ["acp:codex"],
            "node_features": ["session_settings"],
            "worktree_roots": [{"path": ROOT, "exists": True}],
            "repos": [_repo()],
        },
    )
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(team="platform-engineering")
    assert plan["ready"] is False
    assert any(r["code"] == "cwd_realpath_unsupported" for r in plan["refusals"])


def test_auto_picked_node_prefers_one_that_enforces_the_realpath(nodes):
    """#2011: with no explicit host, an older node sorting first must not
    shadow a capable node of the same label for a bound checkout."""
    spec = _spec()
    for seat in spec["seats"]:
        seat["host"] = None
    _commit(spec)
    node_queue.register(
        "a-old-codex",
        identity="Desktop_Agent2",
        device_node="desktop",
        facts={
            "backends": ["acp:codex"],
            "node_features": ["session_settings"],
            "worktree_roots": [{"path": ROOT, "exists": True}],
            "repos": [_repo()],
        },
    )
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(team="platform-engineering")
    assert plan["ready"] is True, plan["refusals"]
    codex = next(s for s in plan["seats"] if s["label"] == "Desktop_Agent2")
    assert codex["node"] == "desktop-agent2"


def test_explicit_host_with_only_old_nodes_still_refuses(nodes):
    spec = _spec()
    for seat in spec["seats"]:
        seat["host"] = None
    spec["seats"][1]["host"] = "a-old-codex"
    _commit(spec)
    node_queue.register(
        "a-old-codex",
        identity="Desktop_Agent2",
        device_node="laptop",
        facts={
            "backends": ["acp:codex"],
            "node_features": ["session_settings"],
            "worktree_roots": [{"path": ROOT, "exists": True}],
            "repos": [_repo()],
        },
    )
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(team="platform-engineering")
    assert plan["ready"] is False
    assert any(r["code"] == "cwd_realpath_unsupported" for r in plan["refusals"])


def test_plan_text_shows_the_bound_checkout_realpath(nodes):
    """The operator approving the plan can see which checkout it binds (#2011).

    Two in-root bindings with the same path string but different real paths
    must not render byte-identical plan text.
    """
    _commit()
    rendered = {}
    for target in ("checkout-a", "checkout-b"):
        _register(
            "desktop-agent2",
            "Desktop_Agent2",
            ["acp:codex"],
            repos=[_repo(realpath=f"{ROOT}/checkouts/{target}")],
        )
        with _as("Desktop_Agent1"):
            plan = team_spec.team_up(team="platform-engineering")
        assert plan["ready"] is True, plan["refusals"]
        rendered[target] = plan["plan_text"]
        assert f"cwd={ROOT}/unified_agent -> {ROOT}/checkouts/{target}" in plan["plan_text"]
        assert f"  binds cwd_realpath={ROOT}/checkouts/{target}" in plan["plan_text"]
    assert rendered["checkout-a"] != rendered["checkout-b"]


def test_plan_text_states_an_unbound_bare_root(nodes):
    spec = _spec()
    spec["seats"][1]["workspace"] = {"repo": None, "worktree": "task"}
    _commit(spec)
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(team="platform-engineering")
    assert f"cwd={ROOT}  worktree=" in plan["plan_text"]
    assert "  binds cwd_realpath=- (bare worktree root, not bound)" in plan["plan_text"]


def test_plan_and_approval_bind_the_checkout_realpath(nodes):
    """Task #1770: a symlinked checkout binds its node-measured real path.

    The plan, the approval payload and the queued item carry the realpath the
    node reported, so a retarget inside the roots changes the plan hash and the
    node can refuse a launch whose resolved cwd differs.
    """
    _commit()
    linked = _repo(realpath=f"{ROOT}/checkouts/ua-a")
    _register("desktop-agent2", "Desktop_Agent2", ["acp:codex"], repos=[linked])
    with _as("Desktop_Agent1"):
        plan = team_spec.team_up(team="platform-engineering")
    assert plan["ready"] is True, plan["refusals"]
    seat = plan["seats"][0]
    assert seat["workspace"]["cwd"] == f"{ROOT}/unified_agent"
    assert seat["workspace"]["cwd_realpath"] == f"{ROOT}/checkouts/ua-a"
    payload = team_spec._approval_payload(plan)
    assert payload["seats"][0]["cwd_realpath"] == f"{ROOT}/checkouts/ua-a"
    spec = team_spec.load_specs()["platform-engineering"]
    queued = team_spec.NodeSeatLauncher().launch(
        seat, prompt=team_spec.startup_prompt(spec, seat), team_run_id="tr-x", actor="x"
    )
    assert node_queue.get_item(queued["item_id"])["cwd_realpath"] == f"{ROOT}/checkouts/ua-a"

    # Retargeted inside the same root: same path string, new plan hash.
    retargeted = _repo(realpath=f"{ROOT}/checkouts/ua-b")
    _register("desktop-agent2", "Desktop_Agent2", ["acp:codex"], repos=[retargeted])
    with _as("Desktop_Agent1"):
        replanned = team_spec.team_up(team="platform-engineering")
    assert replanned["seats"][0]["workspace"]["cwd"] == seat["workspace"]["cwd"]
    assert replanned["plan_sha256"] != plan["plan_sha256"]


# --- HTTP ----------------------------------------------------------------------


def test_http_plan_and_boot_routes(nodes, launcher, monkeypatch):
    _commit()
    token = f"tok-team-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{token},{HUMAN_BEARER}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        listed = client.get("/api/teams", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["teams"][0]["team"] == "platform-engineering"
        plan = client.get("/api/teams/plan?team=platform-engineering", headers=headers)
        assert plan.status_code == 200
        body = plan.json()
        assert body["ready"] is True
        missing = client.get("/api/teams/plan?team=nope", headers=headers)
        assert missing.status_code == 400

        stale = client.post(
            "/api/action/team-up",
            headers=headers,
            json={"team": "platform-engineering", "plan_sha256": "0" * 64},
        )
        assert stale.status_code == 409
        parked = client.post(
            "/api/action/team-up",
            headers=headers,
            json={"team": "platform-engineering", "plan_sha256": body["plan_sha256"]},
        )
        assert parked.status_code == 202
        assert parked.json()["pending"] is True
        with _as("Desktop_Person"):
            approval_resolve(parked.json()["approval_id"], "approve", reason="boot")
        booted = client.post(
            "/api/action/team-up",
            headers=headers,
            json={"team": "platform-engineering", "plan_sha256": body["plan_sha256"]},
        )
        assert booted.status_code == 201, booted.text
        assert booted.json()["booted"] is True
        assert len(launcher.calls) == 2
        assert client.get("/api/teams").status_code == 401


def test_http_team_up_applies_the_mcp_profile_and_policy_gates(nodes, launcher, monkeypatch):
    _commit()
    operator = f"tok-op-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Person:{operator}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {operator}"}
        denied = client.post(
            "/api/action/team-up",
            headers=headers,
            json={"team": "platform-engineering", "plan_sha256": "a" * 64},
        )
        assert denied.status_code == 403
        assert "profile" in denied.json()["error"]
        assert (
            client.get("/api/teams/plan?team=platform-engineering", headers=headers).status_code
            == 403
        )
    assert launcher.calls == []


# --- node-reported facts -------------------------------------------------------


def test_node_reports_checkouts_with_resolved_paths(tmp_path):
    from unified_agent import node_agent

    root = tmp_path / "dev"
    (root / "real" / ".git").mkdir(parents=True)
    (root / "plain").mkdir()
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    (root / "escape").symlink_to(outside)
    repos = {row["name"]: row for row in node_agent.collect_repos([root])}
    assert set(repos) == {"real", "escape"}
    assert repos["escape"]["realpath"] == str(outside.resolve())
    assert repos["real"]["root_realpath"] == str(root.resolve())


def test_node_config_state_lists_skills_only_from_skill_bearing_surfaces():
    from unified_agent import node_agent

    bundle = {"version": "v1", "skills": [{"name": "review-code"}]}
    state = node_agent._applied_config_state(
        bundle, "v1", drifted={"codex", "opencode"}, applied=[]
    )
    assert state["skills"] == []
    state = node_agent._applied_config_state(
        bundle, "v1", drifted={"opencode"}, applied=[{"surface": "codex"}]
    )
    assert state == {
        "bundle_version": "v1",
        "in_sync": ["agents", "claude", "codex", "cursor"],
        "skill_surfaces": ["codex"],
        "skills": ["review-code"],
    }


def test_heartbeat_merges_harness_config_state(nodes):
    node_queue.heartbeat(
        "desktop-agent2",
        identity="Desktop_Agent2",
        device_node="desktop",
        facts={
            "surfaces": {},
            "harness_config": {
                "bundle_version": "v1",
                "in_sync": ["codex"],
                "skill_surfaces": ["codex"],
                "skills": ["x"],
            },
        },
    )
    from unified_agent.node_registry import list_nodes

    row = list_nodes()["desktop-agent2"]
    assert row["harness_config"]["skills"] == ["x"]
    assert row["repos"][0]["name"] == "unified_agent"  # full facts kept
    assert row["backends"] == ["acp:codex"]


def test_live_checks_show_a_retired_seat_as_refused():
    """Task #2305: a retired seat label is an identity refusal in the plan."""
    from unified_agent import agent_lifecycle, events

    label = "Local_RetiredSeat"
    events.record_event(
        agent_lifecycle.CHANNEL,
        payload={
            "kind": agent_lifecycle.KIND_CHANGED,
            "agent": label,
            "state": "retired",
            "reason": "test",
            "actor": "test",
        },
    )
    conn = storage.connect()
    try:
        rows = team_spec.live_checks(conn, label, actor=None, now=datetime.now(UTC))
    finally:
        conn.close()
    assert ("identity", False, f"{label} is retired", "retired") in rows


def test_live_checks_wip_row_uses_the_seats_override_cap(monkeypatch):
    """UA_AGENT_WIP_OVERRIDES sets the cap the plan's wip row checks a seat against."""
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_AGENT_WIP_MAX", "2")
    monkeypatch.setenv("UA_AGENT_WIP_OVERRIDES", "lead_agent:3")
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "50")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "50")
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *args, **kwargs: None)
    for agent in ("lead_agent", "worker_agent"):
        for _ in range(2):
            tid = tasks.task_add("work", lane="dispatch-ready")["id"]
            with _as(agent):
                tasks.task_claim(tid, agent, expected_lane="dispatch-ready")
    conn = storage.connect()
    try:
        now = datetime.now(UTC)
        rows = {
            label: next(
                r for r in team_spec.live_checks(conn, label, actor=None, now=now) if r[0] == "wip"
            )
            for label in ("lead_agent", "worker_agent")
        }
    finally:
        conn.close()
    _, lead_ok, lead_text, lead_code = rows["lead_agent"]
    _, worker_ok, worker_text, _ = rows["worker_agent"]
    assert lead_ok and lead_code == "wip_cap" and "cap 3" in lead_text
    assert not worker_ok and "cap 2" in worker_text
