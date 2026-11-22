"""Operating profiles (task #1732) and per-run comms policy (task #1735)."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, config_revisions, events, operating_profiles, run_records, storage
from unified_agent.goals import goal_manage
from unified_agent.operating_profiles import CommsPolicyDenied, OperatingConfigError
from unified_agent.tools import approvals, delegate, tasks
from unified_agent.tools import messages as message_tools

ORG = {
    "schema_version": 1,
    "offices": [
        {
            "id": "founder",
            "name": "Founder",
            "lead": "Desktop_Person",
            "members": ["Desktop_Person"],
        },
        {
            "id": "staff",
            "name": "Staff",
            "lead": "Desktop_Agent5",
            "members": ["Desktop_Agent5"],
        },
        {
            "id": "platform",
            "name": "Platform",
            "lead": "Desktop_Agent2",
            "members": ["Desktop_Agent2", "Desktop_agent3"],
        },
    ],
}

PROFILES = {
    "default": "standard",
    "profiles": {
        "standard": {
            "description": "Anyone may reach the operator.",
            "comms": {"mode": "direct"},
            "delivery": {"mode": "no-mistakes"},
            "planning": "P2",
        },
        "crew": {
            "extends": "standard",
            "comms": {"mode": "funnel", "liaison": "office:staff"},
            "autonomy": "checkpoint",
        },
        "locked": {
            "comms": {
                "mode": "custom",
                "allow": [
                    {"from": "liaison", "to": "operator"},
                    {"from": "office:platform", "to": "liaison"},
                ],
                "liaison": "Desktop_Agent5",
            }
        },
    },
    "units": {"platform": "crew"},
}


@pytest.fixture(autouse=True)
def env(isolated_env, tmp_path, monkeypatch):
    org = tmp_path / "org.yaml"
    org.write_text(json.dumps(ORG))
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(org))
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: True)
    storage.init_db()


def write_policy(tmp_path, monkeypatch, block=PROFILES):
    path = tmp_path / "policy.yaml"
    path.write_text(json.dumps({"operating_profiles": block}))
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    return path


def as_identity(label):
    return config.agent_identity_var.set(label)


def human(fn, *args, **kwargs):
    token = as_identity("Desktop_Person")
    try:
        return fn(*args, **kwargs)
    finally:
        config.agent_identity_var.reset(token)


def claim(task_id, agent):
    token = as_identity(agent)
    try:
        return tasks.task_claim(task_id, agent)
    finally:
        config.agent_identity_var.reset(token)


def send(sender, to, **kwargs):
    token = as_identity(sender)
    try:
        return message_tools.message_send(act="inform", to_agent=to, body="hi", **kwargs)
    finally:
        config.agent_identity_var.reset(token)


# --- parsing ---------------------------------------------------------------


def test_absent_block_is_unconfigured_and_never_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "missing.yaml"))
    assert operating_profiles.load_config().configured is False
    tid = tasks.task_add("t")["id"]
    claim(tid, "Desktop_Agent2")
    assert send("Desktop_Agent2", "Desktop_Person", task_id=tid)["accepted"]


@pytest.mark.parametrize(
    "block, match",
    [
        ({"profiles": {"a": {"comms": {"mode": "loud"}}}}, "mode must be one of"),
        ({"profiles": {"a": {"comms": {"mode": "funnel"}}}}, "needs a liaison"),
        ({"profiles": {"a": {"comms": {"mode": "custom"}}}}, "at least one allow rule"),
        ({"profiles": {"a": {"surprise": 1}}}, "unknown keys"),
        ({"profiles": {"a": {"extends": "b"}}}, "extends unknown"),
        ({"profiles": {"a": {"extends": "b"}, "b": {"extends": "a"}}}, "loops"),
        ({"profiles": {"a": {}}, "default": "zzz"}, "unknown profile"),
        ({"profiles": {"a": {}}, "units": {"nowhere": "a"}}, "unknown office"),
        ({"profiles": {"a": {"budget": {"max_cost_usd": -1}}}}, "positive"),
        ({"profiles": {"a": {"budget": {"max_cost_usd": float("nan")}}}}, "positive"),
        ({"profiles": {"a": {"budget": {"max_cost_usd": float("inf")}}}}, "positive"),
        ({"profiles": {"a": {"planning": "P9"}}}, "must be one of"),
        ({"profiles": {"A b": {}}}, "lowercase slugs"),
    ],
)
def test_malformed_blocks_fail_closed(block, match):
    with pytest.raises(OperatingConfigError, match=match):
        operating_profiles.parse_config(block, offices=ORG["offices"])


def test_extends_merges_dict_fields_key_by_key():
    cfg = operating_profiles.parse_config(PROFILES, offices=ORG["offices"])
    flat = operating_profiles.flatten(cfg, "crew")
    assert flat["comms"] == {"mode": "funnel", "liaison": "office:staff"}
    assert flat["delivery"] == {"mode": "no-mistakes"}
    assert flat["planning"] == "P2"
    assert flat["autonomy"] == "checkpoint"


def test_policy_revision_validator_rejects_bad_block(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    with pytest.raises(config_revisions.ConfigRevisionError, match="needs a liaison"):
        config_revisions._validate_policy_text(
            json.dumps({"operating_profiles": {"profiles": {"x": {"comms": {"mode": "funnel"}}}}})
        )


# --- resolution ------------------------------------------------------------


def test_layers_resolve_org_unit_sprint_run_with_provenance(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("layered")["id"]
    claim(tid, "Desktop_Agent2")

    base = operating_profiles.resolve(task_id=tid)
    assert base["unit"] == "platform"
    assert base["settings"]["comms"]["mode"] == "funnel"
    assert base["sources"]["comms.mode"] == "unit:platform:crew"
    # The unit layer applies its whole flattened profile, inherited fields too.
    assert base["sources"]["planning"] == "unit:platform:crew"
    assert base["layers"][0]["layer"] == "org:standard"
    assert base["enforcement"]["comms"] == "enforced"
    assert base["enforcement"]["planning"] == "enforced"

    sprint = human(
        operating_profiles.sprint_add, "Sprint 1", overrides={"planning": "P4"}, profile=None
    )
    human(operating_profiles.sprint_link_task, sprint["id"], tid)
    mid = operating_profiles.resolve(task_id=tid)
    assert mid["sprint_id"] == sprint["id"]
    assert mid["settings"]["planning"] == "P4"
    assert mid["sources"]["planning"] == f"sprint:{sprint['id']}:overrides"

    human(
        operating_profiles.run_settings_set,
        tid,
        profile="standard",
        overrides={"model": "claude-opus-5-5"},
    )
    top = operating_profiles.resolve(task_id=tid)
    assert top["settings"]["comms"]["mode"] == "direct"
    assert top["sources"]["comms.mode"] == f"run:task-{tid}:standard"
    assert top["settings"]["model"] == "claude-opus-5-5"
    assert top["fingerprint"] != base["fingerprint"]

    cleared = human(operating_profiles.run_settings_clear, tid)
    assert cleared["settings"]["comms"]["mode"] == "funnel"


def test_sprint_org_unit_beats_claimant_offices(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("unit from sprint")["id"]
    claim(tid, "Desktop_Agent5")
    assert operating_profiles.resolve(task_id=tid)["unit"] is None
    sprint = human(operating_profiles.sprint_add, "Platform push", org_unit="platform")
    human(operating_profiles.sprint_link_task, sprint["id"], tid)
    assert operating_profiles.resolve(task_id=tid)["unit"] == "platform"


def test_claim_freezes_snapshot_on_the_run(tmp_path, monkeypatch):
    path = write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("frozen")["id"]
    claimed = claim(tid, "Desktop_Agent2")
    run = run_records._open_run_on(storage.connect(), tid)
    snap = run["operating_profile"]
    assert snap["settings"]["comms"]["mode"] == "funnel"
    assert snap["unit"] == "platform"
    assert run["run_id"] == claimed["current_run_id"]
    # A later policy edit does not rewrite the running run's posture.
    changed = dict(PROFILES, units={})
    path.write_text(json.dumps({"operating_profiles": changed}))
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=tid)


# --- comms enforcement -----------------------------------------------------


def test_funnel_refuses_crew_to_operator_and_names_the_liaison(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("crew work")["id"]
    claim(tid, "Desktop_Agent2")
    with pytest.raises(CommsPolicyDenied, match="through Desktop_Agent5"):
        send("Desktop_Agent2", "Desktop_Person", task_id=tid)
    denied = events.query_events(channels=[operating_profiles.DENIED_CHANNEL])
    assert denied and denied[-1]["payload"]["set_by_layer"] == "unit:platform:crew"
    # Crew to liaison and crew to crew still flow.
    assert send("Desktop_Agent2", "Desktop_Agent5", task_id=tid)["accepted"]
    assert send("Desktop_Agent2", "Desktop_agent3", task_id=tid)["accepted"]


def test_liaison_and_operator_are_never_funnelled(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("crew work")["id"]
    claim(tid, "Desktop_Agent2")
    assert send("Desktop_Agent5", "Desktop_Person")["accepted"]
    assert send("Desktop_Person", "Desktop_Agent2", task_id=tid)["accepted"]


def test_untied_message_must_pass_every_live_run(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("crew work")["id"]
    claim(tid, "Desktop_Agent2")
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person")


def test_per_run_toggle_switches_funnel_off_for_the_next_run(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("toggle")["id"]
    human(operating_profiles.run_settings_set, tid, overrides={"comms": {"mode": "direct"}})
    claim(tid, "Desktop_Agent2")
    assert send("Desktop_Agent2", "Desktop_Person", task_id=tid)["accepted"]


def test_custom_allowlist(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("locked")["id"]
    human(operating_profiles.run_settings_set, tid, profile="locked")
    claim(tid, "Desktop_Agent2")
    assert send("Desktop_Agent2", "Desktop_Agent5", task_id=tid)["accepted"]
    with pytest.raises(CommsPolicyDenied, match="no allow rule"):
        send("Desktop_Agent2", "Desktop_agent3", task_id=tid)
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=tid)


def test_notify_and_delegate_callback_respect_funnel(tmp_path, monkeypatch):
    from unified_agent.tools import notify as notify_tool

    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("crew work")["id"]
    claim(tid, "Desktop_Agent2")
    monkeypatch.setattr(notify_tool, "_notify_impl", lambda *_a, **_k: {"delivered": True})
    token = as_identity("Desktop_Agent2")
    try:
        with pytest.raises(CommsPolicyDenied):
            notify_tool.notify("done")
    finally:
        config.agent_identity_var.reset(token)
    with pytest.raises(CommsPolicyDenied):
        delegate.delegate(
            task_spec="x", from_agent="Desktop_Agent2", to_agent="Desktop_agent3", callback="notify"
        )
    # An unverified caller passes only as a named hub-internal delivery (#1775).
    with pytest.raises(CommsPolicyDenied, match="unverified caller"):
        notify_tool.notify("system")
    with notify_tool.internal_route("anomaly_alert"):
        assert notify_tool.notify("system")["delivered"]
    with notify_tool.internal_route("made_up"), pytest.raises(CommsPolicyDenied):
        notify_tool.notify("system")


# --- operator writes -------------------------------------------------------


def test_agent_writes_wait_for_human_approval(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("agent asks")["id"]
    token = as_identity("Desktop_Agent2")
    try:
        with pytest.raises(approvals.ApprovalPendingError):
            operating_profiles.run_settings_set(tid, overrides={"comms": {"mode": "direct"}})
    finally:
        config.agent_identity_var.reset(token)
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM run_settings").fetchone()[0] == 0
    finally:
        conn.close()


def test_verified_agent_cannot_claim_to_be_operator(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    token = as_identity("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="verified as"):
            operating_profiles.sprint_add("x", actor="Desktop_Person")
    finally:
        config.agent_identity_var.reset(token)


def test_unknown_profile_and_bad_override_refused(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("bad")["id"]
    with pytest.raises(ToolError, match="unknown operating profile"):
        human(operating_profiles.run_settings_set, tid, profile="nope")
    with pytest.raises(OperatingConfigError):
        human(operating_profiles.run_settings_set, tid, overrides={"comms": {"mode": "x"}})


def test_goal_manage_surface(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    view = goal_manage("operating_profiles")
    assert view["default"] == "standard" and "crew" in view["profiles"]
    assert view["profiles"]["crew"]["effective"]["planning"] == "P2"
    sprint = human(goal_manage, "operating_sprint_add", {"name": "S", "profile": "crew"})
    assert goal_manage("operating_sprint_get", {"sprint_id": sprint["id"]})["profile"] == "crew"
    human(goal_manage, "operating_sprint_update", {"sprint_id": sprint["id"], "status": "active"})
    listed = goal_manage("operating_sprint_list", {"status": "active"})["sprints"]
    assert [s["id"] for s in listed] == [sprint["id"]]
    with pytest.raises(ToolError, match="operating action must be one of"):
        goal_manage("operating_nope")


# --- review fixes (Devin, PR #958) -------------------------------------------


def test_policy_mode_mirrors_match_their_sources():
    from unified_agent import autonomy_modes, delivery, operating_policy

    assert operating_policy.DELIVERY_MODES == delivery.DELIVERY_MODES
    assert tuple(operating_policy.AUTONOMY_MODES) == tuple(autonomy_modes.MODES)


def test_delegate_callback_is_judged_by_delegator_policy_at_fire_time(tmp_path, monkeypatch):
    from unified_agent.tools import notify as notify_tool

    write_policy(tmp_path, monkeypatch, block=dict(PROFILES, units={}))
    made = delegate.delegate(
        task_spec="Inspect",
        from_agent="Desktop_Agent2",
        to_agent="Desktop_agent3",
        callback="notify",
    )
    # The delegator's office adopts a funnel after delegating.
    write_policy(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        notify_tool, "_notify_impl", lambda msg, *_a, **_k: sent.append(msg) or {"delivered": True}
    )
    delegate.fire_completion_callbacks(made["task_id"])
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT callback_result FROM delegations WHERE task_id=?", (made["task_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert sent == []
    assert "CommsPolicyDenied" in row["callback_result"]


def test_deleted_tasks_leave_no_sprint_links_or_run_settings(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("doomed")["id"]
    sprint = human(operating_profiles.sprint_add, "S")
    human(operating_profiles.sprint_link_task, sprint["id"], tid)
    human(operating_profiles.run_settings_set, tid, profile="standard")
    tasks.task_delete(tid)
    assert operating_profiles.sprint_get(sprint["id"])["task_ids"] == []
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM run_settings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sprint_tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_incomplete_comms_override_is_refused_before_it_is_stored(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("needs liaison")["id"]
    with pytest.raises(OperatingConfigError, match="needs a liaison"):
        human(operating_profiles.run_settings_set, tid, overrides={"comms": {"mode": "funnel"}})
    with pytest.raises(OperatingConfigError, match="needs a liaison"):
        human(operating_profiles.sprint_add, "bad", overrides={"comms": {"mode": "funnel"}})
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM run_settings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sprints").fetchone()[0] == 0
    finally:
        conn.close()
    # The task can still be claimed.
    assert claim(tid, "Desktop_Agent2")["current_run_id"]


def test_finished_run_keeps_its_frozen_policy(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("finished")["id"]
    claim(tid, "Desktop_Agent2")
    token = as_identity("Desktop_Agent2")
    try:
        tasks.task_update(tid, status="done")
    finally:
        config.agent_identity_var.reset(token)
    human(operating_profiles.run_settings_set, tid, overrides={"comms": {"mode": "direct"}})
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=tid)


def test_foreign_task_policy_grants_nothing(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    open_task = tasks.task_add("someone else's direct run")["id"]
    human(operating_profiles.run_settings_set, open_task, profile="standard")
    claim(open_task, "Desktop_Agent5")
    mine = tasks.task_add("mine")["id"]
    claim(mine, "Desktop_Agent2")
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=open_task)


def test_removing_profiles_keeps_frozen_run_restrictions(tmp_path, monkeypatch):
    path = write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("frozen funnel")["id"]
    claim(tid, "Desktop_Agent2")
    path.write_text("{}")
    assert operating_profiles.load_config().configured is False
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=tid)
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person")


@pytest.mark.parametrize(
    "starts, ends, match",
    [
        ("2026-10-10", "2026-10-01", "must not be after"),
        ("next tuesday", None, "ISO-8601"),
        (None, 20261001, "ISO-8601"),
    ],
)
def test_sprint_dates_are_iso_and_ordered(tmp_path, monkeypatch, starts, ends, match):
    write_policy(tmp_path, monkeypatch)
    with pytest.raises(ToolError, match=match):
        human(operating_profiles.sprint_add, "S", starts_at=starts, ends_at=ends)
    ok = human(
        operating_profiles.sprint_add, "S", starts_at="2026-10-01", ends_at="2026-10-14T17:00:00Z"
    )
    with pytest.raises(ToolError, match="must not be after"):
        human(operating_profiles.sprint_update, ok["id"], ends_at="2026-09-01")


# --- pre-rollout hardening (#1764, #1766) ------------------------------------


def test_finished_direct_task_cannot_bypass_current_funnel(tmp_path, monkeypatch):
    path = write_policy(tmp_path, monkeypatch, dict(PROFILES, units={}))
    old = tasks.task_add("old direct")["id"]
    claim(old, "Desktop_Agent2")
    token = as_identity("Desktop_Agent2")
    try:
        tasks.task_update(old, status="done")
    finally:
        config.agent_identity_var.reset(token)
    path.write_text(json.dumps({"operating_profiles": PROFILES}))
    live = tasks.task_add("current funnel")["id"]
    claim(live, "Desktop_Agent2")
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=old)
    assert send("Desktop_Agent2", "Desktop_Agent5", task_id=old)["accepted"]


def test_tied_direct_run_must_pass_other_live_runs(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    direct = tasks.task_add("direct")["id"]
    human(operating_profiles.run_settings_set, direct, profile="standard")
    claim(direct, "Desktop_Agent2")
    funnel = tasks.task_add("funnel")["id"]
    claim(funnel, "Desktop_Agent2")
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=direct)


@pytest.mark.parametrize("reference", ["sprint", "run"])
@pytest.mark.parametrize(
    "document",
    [
        {},
        {
            "operating_profiles": dict(
                PROFILES, profiles={"standard": PROFILES["profiles"]["standard"]}, units={}
            )
        },
    ],
)
def test_policy_revision_cannot_orphan_stored_profile(tmp_path, monkeypatch, reference, document):
    write_policy(tmp_path, monkeypatch)
    if reference == "sprint":
        human(operating_profiles.sprint_add, "Crew", profile="crew")
    else:
        tid = tasks.task_add("configured")["id"]
        human(operating_profiles.run_settings_set, tid, profile="crew")
    with pytest.raises(config_revisions.ConfigRevisionError, match="removes profiles still in use"):
        config_revisions._validate_policy_text(json.dumps(document))


def test_closed_sprint_does_not_keep_profile_in_use(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    sprint = human(operating_profiles.sprint_add, "Crew", profile="crew")
    human(operating_profiles.sprint_update, sprint["id"], status="closed")
    config_revisions._validate_policy_text("{}")


@pytest.mark.parametrize("reference", ["sprint", "run"])
def test_stale_profile_degrades_claim_and_comms_with_daily_event(tmp_path, monkeypatch, reference):
    path = write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("stale")["id"]
    if reference == "sprint":
        sprint = human(operating_profiles.sprint_add, "Crew", profile="locked")
        human(operating_profiles.sprint_link_task, sprint["id"], tid)
    else:
        human(operating_profiles.run_settings_set, tid, profile="locked")
    block = json.loads(json.dumps(PROFILES))
    del block["profiles"]["locked"]
    path.write_text(json.dumps({"operating_profiles": block}))
    claimed = claim(tid, "Desktop_Agent2")
    conn = storage.connect()
    try:
        run = run_records._open_run_on(conn, tid)
        assert run["run_id"] == claimed["current_run_id"]
        assert "no longer in policy" in str(run["operating_profile"]["warnings"])
    finally:
        conn.close()
    assert send("Desktop_Agent2", "Desktop_Agent5")["accepted"]
    assert send("Desktop_Agent2", "Desktop_Agent5")["accepted"]
    degraded = events.query_events(channels=[operating_profiles.DEGRADED_CHANNEL])
    assert len(degraded) == 1
    assert degraded[0]["payload"]["task_id"] == tid
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person")


@pytest.mark.parametrize(
    "overrides, expected_mode",
    [
        ("{bad", "direct"),
        (json.dumps({"planning": "P9"}), "direct"),
        (json.dumps({"comms": {"mode": "funnel"}}), "funnel"),
    ],
)
def test_bad_stored_overrides_degrade_and_incomplete_comms_stays_closed(
    tmp_path, monkeypatch, overrides, expected_mode
):
    write_policy(tmp_path, monkeypatch, dict(PROFILES, units={}))
    tid = tasks.task_add("legacy override")["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO run_settings(task_id, profile, overrides, set_by, set_at) VALUES (?,NULL,?,?,?)",
            (tid, overrides, "Desktop_Person", storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    claimed = claim(tid, "Desktop_Agent2")
    assert claimed["current_run_id"]
    resolved = operating_profiles.resolve(task_id=tid)
    assert resolved["settings"]["comms"]["mode"] == expected_mode
    assert resolved["warnings"]
    with pytest.raises(OperatingConfigError):
        operating_profiles.resolve(task_id=tid, strict=True)
    if expected_mode == "funnel":
        with pytest.raises(CommsPolicyDenied, match="no resolvable liaison"):
            send("Desktop_Agent2", "Desktop_Person")
    else:
        assert send("Desktop_Agent2", "Desktop_Person")["accepted"]


def test_removed_office_binding_degrades_live_but_strict_revision_refuses(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    from unified_agent import org_registry

    monkeypatch.setattr(org_registry, "load_registry", lambda: ORG["offices"][:2])
    cfg = operating_profiles.load_config()
    assert cfg.units == {}
    assert "binding is ignored" in str(cfg.warnings)
    with pytest.raises(config_revisions.ConfigRevisionError, match="unknown office"):
        config_revisions._validate_policy_text(json.dumps({"operating_profiles": PROFILES}))
    assert send("Desktop_Agent2", "Desktop_Person")["accepted"]
    assert events.query_events(channels=[operating_profiles.DEGRADED_CHANNEL])


def test_funnel_caller_flushes_liaison_digest_without_losing_notifications(tmp_path, monkeypatch):
    from unified_agent.tools import digest
    from unified_agent.tools import notify as notify_tool

    write_policy(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        notify_tool, "_notify_impl", lambda msg, *_a: sent.append(msg) or {"delivered": True}
    )
    token = as_identity("Desktop_Agent5")
    try:
        digest.batch_notify(["approved liaison entry"], agent="Desktop_Agent5")
    finally:
        config.agent_identity_var.reset(token)
    token = as_identity("Desktop_Agent2")
    try:
        with pytest.raises(CommsPolicyDenied):
            digest.batch_notify(["crew cannot queue"], agent="Desktop_Agent5")
        flushed = digest.flush_digest()
    finally:
        config.agent_identity_var.reset(token)
    assert flushed["delivered"] and flushed["flushed_count"] == 1
    assert len(sent) == 1 and "approved liaison entry" in sent[0]
    assert "crew cannot queue" not in sent[0]


def test_comms_contexts_do_not_read_event_log(tmp_path, monkeypatch):
    import sqlite3

    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("keyed frozen run")["id"]
    claimed = claim(tid, "Desktop_Agent2")
    conn = storage.connect()
    try:
        snap = conn.execute(
            "SELECT snapshot FROM run_operating_snapshots WHERE run_id=?",
            (claimed["current_run_id"],),
        ).fetchone()
        assert json.loads(snap["snapshot"])["settings"]["comms"]["mode"] == "funnel"
        conn.set_authorizer(
            lambda action, table, *_args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "event_log"
                else sqlite3.SQLITE_OK
            )
        )
        contexts = operating_profiles._contexts(
            conn, "Desktop_Agent2", tid, operating_profiles.load_config()
        )
        assert len(contexts) == 1 and contexts[0]["run_id"] == claimed["current_run_id"]
    finally:
        conn.close()


def test_snapshot_backfill_preserves_old_claim_and_runs_once(tmp_path, monkeypatch):
    import sqlite3

    from unified_agent import operating_policy

    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("before upgrade")["id"]
    claimed = claim(tid, "Desktop_Agent2")
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM run_operating_snapshots")
        conn.execute("DELETE FROM meta WHERE key='operating_snapshots_backfilled'")
        operating_policy.migrate(conn)
        row = conn.execute(
            "SELECT snapshot FROM run_operating_snapshots WHERE run_id=?",
            (claimed["current_run_id"],),
        ).fetchone()
        assert json.loads(row["snapshot"])["settings"]["comms"]["mode"] == "funnel"
        conn.set_authorizer(
            lambda action, table, *_args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "event_log"
                else sqlite3.SQLITE_OK
            )
        )
        operating_policy.migrate(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_sprint_rename_validates_before_storage(tmp_path, monkeypatch, name):
    write_policy(tmp_path, monkeypatch)
    sprint = human(operating_profiles.sprint_add, "Original")
    with pytest.raises(ToolError, match="1-200"):
        human(operating_profiles.sprint_update, sprint["id"], name=name)
    assert operating_profiles.sprint_get(sprint["id"])["name"] == "Original"


def test_funnel_delegation_default_notify_refused_but_none_allowed(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    with pytest.raises(CommsPolicyDenied):
        delegate.delegate(task_spec="x", from_agent="Desktop_Agent2", to_agent="Desktop_agent3")
    made = delegate.delegate(
        task_spec="x", from_agent="Desktop_Agent2", to_agent="Desktop_agent3", callback="none"
    )
    assert made["task_id"]


# --- Devin review follow-ups (PR #962) ---------------------------------------


def test_unreadable_registry_skips_offices_and_preserves_claim_snapshot(tmp_path, monkeypatch):
    from unified_agent import org_registry

    write_policy(tmp_path, monkeypatch)

    def unreadable():
        raise OSError("registry unavailable")

    monkeypatch.setattr(org_registry, "load_registry", unreadable)
    cfg = operating_profiles.load_config()
    assert cfg.units == {}
    assert "office bindings ignored" in str(cfg.warnings)
    tid = tasks.task_add("remaining org layer")["id"]
    claimed = claim(tid, "Desktop_Agent2")
    assert claimed["current_run_id"]
    resolved = operating_profiles.resolve(task_id=tid)
    assert resolved["settings"]["comms"]["mode"] == "direct"
    assert resolved["warnings"]
    assert send("Desktop_Agent2", "Desktop_Person", task_id=tid)["accepted"]
    assert events.query_events(channels=[operating_profiles.DEGRADED_CHANNEL])
    with pytest.raises(config_revisions.ConfigRevisionError, match="cannot read the org registry"):
        config_revisions._validate_policy_text(json.dumps({"operating_profiles": PROFILES}))


def test_closed_sprint_with_linked_tasks_keeps_its_profile_reference(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch, dict(PROFILES, units={}))
    tid = tasks.task_add("still linked")["id"]
    sprint = human(operating_profiles.sprint_add, "Crew", profile="crew")
    human(operating_profiles.sprint_link_task, sprint["id"], tid)
    human(operating_profiles.sprint_update, sprint["id"], status="closed")
    with pytest.raises(config_revisions.ConfigRevisionError, match="removes profiles still in use"):
        config_revisions._validate_policy_text("{}")
    assert operating_profiles.resolve(task_id=tid)["settings"]["comms"]["mode"] == "funnel"
    human(operating_profiles.sprint_link_task, sprint["id"], tid, unlink=True)
    config_revisions._validate_policy_text("{}")


@pytest.mark.parametrize("new_agent", ["Desktop_Agent2", "Desktop_agent3"])
def test_unprofiled_reclaim_supersedes_only_the_reclaimers_policy(tmp_path, monkeypatch, new_agent):
    path = write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("reclaimed without a profile")["id"]
    first = claim(tid, "Desktop_Agent2")
    token = as_identity("Desktop_Agent2")
    try:
        tasks.task_update(tid, status="done")
        tasks.task_update(tid, status="open")
    finally:
        config.agent_identity_var.reset(token)
    path.write_text("{}")
    newest = claim(tid, new_agent)
    assert newest["current_run_id"] != first["current_run_id"]
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT snapshot FROM run_operating_snapshots WHERE run_id=?",
            (newest["current_run_id"],),
        ).fetchone()
        assert json.loads(row["snapshot"]) == {}
    finally:
        conn.close()
    assert send(new_agent, "Desktop_Person", task_id=tid)["accepted"]
    if new_agent == "Desktop_Agent2":
        # Its own unprofiled reclaim is its newest run of the task.
        assert send("Desktop_Agent2", "Desktop_Person", task_id=tid)["accepted"]
    else:
        # Another agent's reclaim never retires Codex's own frozen funnel (#1775).
        with pytest.raises(CommsPolicyDenied):
            send("Desktop_Agent2", "Desktop_Person", task_id=tid)


def test_backfill_indexes_unprofiled_claims_to_supersede_older_snapshots(tmp_path, monkeypatch):
    from unified_agent import operating_policy

    path = write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("old then new")["id"]
    claim(tid, "Desktop_Agent2")
    token = as_identity("Desktop_Agent2")
    try:
        tasks.task_update(tid, status="done")
        tasks.task_update(tid, status="open")
    finally:
        config.agent_identity_var.reset(token)
    path.write_text("{}")
    newest = claim(tid, "Desktop_Agent2")
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM run_operating_snapshots")
        conn.execute("DELETE FROM meta WHERE key='operating_snapshots_backfilled'")
        operating_policy.migrate(conn)
        row = conn.execute(
            "SELECT snapshot FROM run_operating_snapshots WHERE run_id=?",
            (newest["current_run_id"],),
        ).fetchone()
        assert json.loads(row["snapshot"]) == {}
        conn.commit()
    finally:
        conn.close()
    assert send("Desktop_Agent2", "Desktop_Person", task_id=tid)["accepted"]


@pytest.mark.parametrize("reference", ["sprint", "run"])
def test_policy_commit_rechecks_references_after_early_validation(tmp_path, monkeypatch, reference):
    path = write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("concurrent setting")["id"]
    previous = path.read_bytes()
    original = config_revisions._validate_policy_text
    inserted = False

    def validate_then_interleave(text, *, conn=None):
        nonlocal inserted
        original(text, conn=conn)
        if conn is None and not inserted:
            inserted = True
            if reference == "sprint":
                human(operating_profiles.sprint_add, "Interleaved", profile="crew")
            else:
                human(operating_profiles.run_settings_set, tid, profile="crew")

    monkeypatch.setattr(config_revisions, "_validate_policy_text", validate_then_interleave)
    block = dict(PROFILES, profiles={"standard": PROFILES["profiles"]["standard"]}, units={})
    with pytest.raises(config_revisions.ConfigRevisionError, match="removes profiles still in use"):
        human(config_revisions.commit, "policy", {"operating_profiles": block})
    assert inserted
    assert path.read_bytes() == previous


@pytest.mark.parametrize("writer", ["sprint_add", "sprint_update", "run_set"])
def test_settings_write_reloads_policy_after_early_validation(tmp_path, monkeypatch, writer):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("policy race")["id"]
    sprint = human(operating_profiles.sprint_add, "Existing", profile="standard")
    original = operating_profiles._connect
    changed = False

    def interleaved_connect():
        nonlocal changed
        if not changed:
            changed = True
            block = dict(
                PROFILES, profiles={"standard": PROFILES["profiles"]["standard"]}, units={}
            )
            human(config_revisions.commit, "policy", {"operating_profiles": block})
        return original()

    monkeypatch.setattr(operating_profiles, "_connect", interleaved_connect)
    with pytest.raises(OperatingConfigError, match="unknown profile"):
        if writer == "sprint_add":
            human(operating_profiles.sprint_add, "Late sprint", profile="crew")
        elif writer == "sprint_update":
            human(operating_profiles.sprint_update, sprint["id"], profile="crew")
        else:
            human(operating_profiles.run_settings_set, tid, profile="crew")
    assert changed
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM sprints WHERE profile='crew'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM run_settings").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("recipient", ["Desktop_Agent5", "Desktop_Person"])
@pytest.mark.parametrize("registry_failure", [False, True])
def test_degraded_events_keep_distinct_tasks_with_identical_fingerprints(
    tmp_path, monkeypatch, recipient, registry_failure
):
    write_policy(tmp_path, monkeypatch)
    conn = storage.connect()
    try:
        for title, profile in [("first broken", "old-a"), ("second broken", "old-b")]:
            tid = tasks.task_add(title)["id"]
            conn.execute(
                "INSERT INTO run_settings(task_id, profile, overrides, set_by, set_at) VALUES (?,?, '{}',?,?)",
                (tid, profile, "Desktop_Person", storage.now()),
            )
            conn.commit()
            claim(tid, "Desktop_Agent2")
    finally:
        conn.close()
    if registry_failure:
        from unified_agent import org_registry

        def unreadable():
            raise OSError("registry unavailable")

        monkeypatch.setattr(org_registry, "load_registry", unreadable)
    for _ in range(2):
        if recipient == "Desktop_Person":
            with pytest.raises(CommsPolicyDenied):
                send("Desktop_Agent2", recipient)
        else:
            assert send("Desktop_Agent2", recipient)["accepted"]
    degraded = events.query_events(channels=[operating_profiles.DEGRADED_CHANNEL])
    assert len(degraded) == 2
    assert len({row["payload"]["task_id"] for row in degraded}) == 2
    assert "old-a" in str(degraded) and "old-b" in str(degraded)
    if registry_failure:
        # Both complete warning sets include the new lookup failure, even
        # for a later run after the first denial. No partial-set event exists.
        assert all("registry unavailable" in str(row["payload"]["warnings"]) for row in degraded)


def test_later_profile_activation_keeps_unprofiled_live_run_frozen(tmp_path, monkeypatch):
    path = write_policy(tmp_path, monkeypatch, dict(PROFILES, units={}))
    path.write_text("{}")
    tid = tasks.task_add("claimed before operating rollout")["id"]
    claim(tid, "Desktop_Agent2")
    path.write_text(json.dumps({"operating_profiles": PROFILES}))
    # Present empty keyed snapshot is the run's posture, not missing data.
    assert send("Desktop_Agent2", "Desktop_Person", task_id=tid)["accepted"]
    assert send("Desktop_Agent2", "Desktop_Person")["accepted"]
    token = as_identity("Desktop_Agent2")
    try:
        tasks.task_update(tid, status="done")
    finally:
        config.agent_identity_var.reset(token)
    # With no live run, the current office funnel applies again.
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=tid)


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("mode", ["funnel", "custom"])
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_unavailable_comms_offices_fail_closed_without_breaking_funnel_peers(
    tmp_path, monkeypatch, frozen, mode, error_type
):
    from unified_agent import org_registry

    comms = {"mode": mode, "liaison": "office:staff"}
    if mode == "custom":
        comms["allow"] = [
            {"from": "liaison", "to": "operator"},
            {"from": "office:platform", "to": "liaison"},
        ]
    path = write_policy(
        tmp_path,
        monkeypatch,
        {"default": "office-policy", "profiles": {"office-policy": {"comms": comms}}, "units": {}},
    )
    tid = None
    if frozen:
        tid = tasks.task_add("frozen office communications")["id"]
        claim(tid, "Desktop_Agent2")
        # With no current profile block, only the frozen run can report the
        # office-lookup failure. Its restriction must still remain in force.
        path.write_text("{}")

    def unreadable():
        raise error_type("registry unavailable")

    monkeypatch.setattr(org_registry, "load_registry", unreadable)
    if mode == "funnel":
        assert send("Desktop_Agent2", "Desktop_Agent5", task_id=tid)["accepted"]
        assert send("Desktop_Agent2", "Desktop_agent3", task_id=tid)["accepted"]
    else:
        with pytest.raises(CommsPolicyDenied, match="no allow rule"):
            send("Desktop_Agent2", "Desktop_Agent5", task_id=tid)
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=tid)
    warnings = events.query_events(channels=[operating_profiles.DEGRADED_CHANNEL])
    assert "registry unavailable" in str(warnings)
    assert "no liaison or members" in str(warnings)
    assert operating_profiles.comms_decision(comms, "Desktop_Agent5", "Desktop_Person")[0] is False


# --- wired fields (task #1756) ---------------------------------------------


def write_raw_policy(tmp_path, monkeypatch, **body):
    path = tmp_path / "policy.yaml"
    path.write_text(json.dumps(body))
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    return path


def open_snapshot(tid):
    conn = storage.connect()
    try:
        return operating_profiles.run_snapshot(conn, tid)
    finally:
        conn.close()


def test_run_snapshot_reads_open_run_only_unless_finished(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("snap")["id"]
    assert open_snapshot(tid) is None
    claim(tid, "Desktop_Agent2")
    assert open_snapshot(tid)["settings"]["autonomy"] == "checkpoint"
    token = as_identity("Desktop_Agent2")
    try:
        tasks.task_update(tid, status="done")
    finally:
        config.agent_identity_var.reset(token)
    assert open_snapshot(tid) is None
    finished = operating_profiles.task_run_snapshot(tid, finished=True)
    assert finished["settings"]["autonomy"] == "checkpoint"


def test_run_snapshot_reads_the_keyed_store_not_the_claim_event(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("keyed")["id"]
    claim(tid, "Desktop_Agent2")
    conn = storage.connect()
    try:
        # Strip the snapshot from the claim event: the keyed row alone answers.
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload=json_remove(payload, '$.operating_profile') "
                "WHERE json_extract(payload, '$.task_id')=?",
                (tid,),
            )
        conn.commit()
        assert operating_profiles.run_snapshot(conn, tid)["settings"]["autonomy"] == "checkpoint"
        # Dropping the keyed row (an unprofiled run keys an empty snapshot) reads as None.
        conn.execute("UPDATE run_operating_snapshots SET snapshot='{}' WHERE task_id=?", (tid,))
        conn.commit()
        assert operating_profiles.run_snapshot(conn, tid) is None
        assert operating_profiles.run_snapshot(conn, tid, finished=True) is None
    finally:
        conn.close()


def test_relay_field_parses_and_defaults_off(tmp_path, monkeypatch):
    parsed = operating_profiles.parse_settings({"relay": {"autosend": ["slack", "slack", "mail"]}})
    assert parsed["relay"] == {"autosend": ["slack", "mail"]}
    with pytest.raises(OperatingConfigError, match="lowercase slugs"):
        operating_profiles.parse_settings({"relay": {"autosend": ["Bad Channel"]}})
    with pytest.raises(OperatingConfigError, match="unknown keys"):
        operating_profiles.parse_settings({"relay": {"loud": True}})
    block = dict(PROFILES)
    block["profiles"] = dict(PROFILES["profiles"], standard=dict(PROFILES["profiles"]["standard"]))
    block["profiles"]["standard"]["relay"] = {"autosend": ["slack"]}
    write_policy(tmp_path, monkeypatch, block)
    tid = tasks.task_add("relay")["id"]
    claim(tid, "Desktop_Agent5")
    snap = open_snapshot(tid)
    assert operating_profiles.relay_autosend(snap, "slack") is True
    assert operating_profiles.relay_autosend(snap, "mail") is False
    assert operating_profiles.relay_autosend(None, "slack") is False
    assert operating_profiles.resolve(task_id=tid)["enforcement"]["relay"] == "recorded"


def delivery_file(tmp_path, monkeypatch, body):
    import yaml

    path = tmp_path / "project_delivery.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setenv("UA_PROJECT_DELIVERY_FILE", str(path))
    return path


def test_delivery_sprint_layer_beats_file_and_note(tmp_path, monkeypatch):
    from unified_agent.delivery import resolve_delivery

    write_policy(tmp_path, monkeypatch)
    delivery_file(
        tmp_path, monkeypatch, {"projects": {"unified-agent": {"mode": "direct-pr", "yolo": True}}}
    )
    tid = tasks.task_add("ship", project="unified-agent")["id"]
    sprint = human(
        operating_profiles.sprint_add, "S", overrides={"delivery": {"mode": "local-only"}}
    )
    human(operating_profiles.sprint_link_task, sprint["id"], tid)
    claim(tid, "Desktop_Agent2")
    snap = open_snapshot(tid)
    note = "[delivery: mode=no-mistakes yolo=on]"
    policy = resolve_delivery("unified-agent", notes=note, snapshot=snap)
    assert (policy.mode, policy.source) == ("local-only", "profile")
    assert policy.layer == f"sprint:{sprint['id']}:overrides"
    # yolo was not set by the sprint, so the note's yolo stands.
    assert policy.yolo is True
    # Frozen: a sprint edit after the claim does not reach the running run.
    human(
        operating_profiles.sprint_update,
        sprint["id"],
        overrides={"delivery": {"mode": "direct-pr"}},
    )
    assert resolve_delivery("unified-agent", snapshot=open_snapshot(tid)).mode == "local-only"


def test_delivery_org_layer_sits_below_project_file(tmp_path, monkeypatch):
    from unified_agent.delivery import resolve_delivery

    write_policy(tmp_path, monkeypatch)  # org standard: delivery.mode no-mistakes
    delivery_file(
        tmp_path,
        monkeypatch,
        {"default": {"mode": "local-only"}, "projects": {"unified-agent": {"mode": "direct-pr"}}},
    )
    tid = tasks.task_add("ship")["id"]
    claim(tid, "Desktop_Agent5")
    snap = open_snapshot(tid)
    assert snap["sources"]["delivery.mode"] == "org:standard"
    on_file = resolve_delivery("unified-agent", snapshot=snap)
    assert (on_file.mode, on_file.source) == ("direct-pr", "file")
    # A project with no file entry: the org profile beats the file default.
    other = resolve_delivery("other", snapshot=snap)
    assert (other.mode, other.source, other.layer) == ("no-mistakes", "profile", "org:standard")
    # The note marker still beats an org-layer setting.
    noted = resolve_delivery("other", notes="[delivery: mode=direct-pr]", snapshot=snap)
    assert (noted.mode, noted.source) == ("direct-pr", "note")
    # No snapshot: unchanged legacy resolution.
    assert resolve_delivery("other").mode == "local-only"


def test_executor_finish_stamps_the_run_delivery(tmp_path, monkeypatch):
    from unified_agent.delivery import resolve_delivery

    write_policy(tmp_path, monkeypatch)
    delivery_file(tmp_path, monkeypatch, {"default": {"mode": "direct-pr"}})
    tid = tasks.task_add("ship")["id"]
    human(operating_profiles.run_settings_set, tid, overrides={"delivery": {"mode": "local-only"}})
    claim(tid, "Desktop_Agent2")
    conn = storage.connect()
    try:
        policy = resolve_delivery(None, snapshot=operating_profiles.run_snapshot(conn, tid))
    finally:
        conn.close()
    assert policy.mode == "local-only"
    assert policy.layer == f"run:task-{tid}:overrides"


def _mission_for(tid):
    from unified_agent.goals import goal_add, link_task

    gid = goal_add("Mission", author="Desktop_Person")["goal"]["id"]
    link_task(tid, gid, author="Desktop_Person")
    return gid


def test_autonomy_profile_is_the_mission_default(tmp_path, monkeypatch):
    from unified_agent import autonomy_modes

    path = write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("autonomy")["id"]
    gid = _mission_for(tid)
    # No run yet: the strict default stays.
    assert autonomy_modes.profile_default_mode(gid, "Desktop_Agent2") is None
    assert autonomy_modes._decide(gid, "merge", "Desktop_Agent2")[3] == "action-review"
    claim(tid, "Desktop_Agent2")  # unit platform -> crew -> checkpoint
    assert autonomy_modes.profile_default_mode(gid, "Desktop_Agent2") == "checkpoint"
    assert autonomy_modes._decide(gid, "merge", "Desktop_Agent2")[3] == "checkpoint"
    # A merge context names the task: its run decides.
    ctx = {"task_id": tid}
    assert autonomy_modes.profile_default_mode(gid, "Desktop_Agent5", ctx) == "checkpoint"
    # Frozen: dropping the unit binding after the claim leaves the run's mode.
    path.write_text(json.dumps({"operating_profiles": dict(PROFILES, units={})}))
    assert autonomy_modes._decide(gid, "merge", "Desktop_Agent2")[3] == "checkpoint"


def test_autonomy_explicit_mode_wins_and_ceiling_tightens(tmp_path, monkeypatch):
    from unified_agent import autonomy_modes

    block = dict(PROFILES, units={})
    block["profiles"] = dict(PROFILES["profiles"], loose={"autonomy": "observe"})
    write_raw_policy(tmp_path, monkeypatch, operating_profiles=block, autonomy_ceiling="exception")
    tid = tasks.task_add("loose")["id"]
    gid = _mission_for(tid)
    human(operating_profiles.run_settings_set, tid, profile="loose")
    claim(tid, "Desktop_Agent2")
    assert autonomy_modes.profile_default_mode(gid, "Desktop_Agent2") == "observe"
    # The org ceiling still tightens a profile default.
    assert autonomy_modes._decide(gid, "merge", "Desktop_Agent2")[3] == "exception"
    # An explicit mission mode is never replaced by the profile.
    autonomy_modes.set_mode(gid, "action-review", "Desktop_Person")
    assert autonomy_modes._decide(gid, "merge", "Desktop_Agent2")[3] == "action-review"


def test_profile_model_keeps_the_provider(monkeypatch):
    for key in ("UA_MODEL_CLAUDE", "UA_MODEL_CODEX", "UA_MODEL_DEFAULT"):
        monkeypatch.delenv(key, raising=False)
    snap = {"settings": {"model": "claude-opus-5-5"}}
    assert operating_profiles.profile_model(
        snap, agent_label="Desktop_Agent1", backend="claude"
    ) == ("claude-opus-5-5", "applied")
    refused, reason = operating_profiles.profile_model(
        snap, agent_label="Desktop_Agent2", backend="codex", backend_fallback="gpt-6-luna"
    )
    assert refused is None and "refused" in reason
    # A multi-provider harness on an unknown baseline cannot prove the provider.
    assert (
        operating_profiles.profile_model(snap, agent_label="Desktop_Agent4", backend="cursor")[0]
        is None
    )
    assert operating_profiles.profile_model(None, agent_label="x", backend="claude") == (
        None,
        "unset",
    )


def test_launch_uses_the_run_model_unless_overridden(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from unified_agent.dispatch import launch

    for key in ("UA_MODEL_CLAUDE", "UA_MODEL_DEFAULT"):
        monkeypatch.delenv(key, raising=False)
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("model")["id"]
    human(operating_profiles.run_settings_set, tid, overrides={"model": "claude-opus-5-5"})
    route = SimpleNamespace(cli_backend="claude", agent_label="Desktop_Agent1")
    # Before the claim, the launch resolves what the claim will freeze.
    assert launch._run_model_for(route, "do it", tid) == "claude-opus-5-5"
    claim(tid, "Desktop_Agent1")
    # Frozen: a run-settings edit after the claim is for the next run.
    human(operating_profiles.run_settings_set, tid, overrides={"model": "claude-haiku-5"})
    assert launch._run_model_for(route, "do it", tid) == "claude-opus-5-5"
    # An explicit [model:] marker beats the profile.
    assert launch._run_model_for(route, "do it [model: sonnet-5]", tid) == "sonnet-5"
    # A cross-provider profile model is refused; the run keeps its default.
    codex = SimpleNamespace(cli_backend="codex", agent_label="Desktop_Agent2")
    assert launch._run_model_for(codex, "do it", tid) != "claude-opus-5-5"


def test_cycle_budget_fills_missing_caps_from_the_run(tmp_path, monkeypatch):
    from unified_agent.tools import cycles

    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("budget")["id"]
    human(
        operating_profiles.run_settings_set,
        tid,
        overrides={"budget": {"max_cost_usd": 2.5, "max_time_s": 600, "max_tokens": 9000}},
    )
    claim(tid, "Desktop_Agent1")
    # Frozen: a later edit does not change this run's caps.
    human(operating_profiles.run_settings_set, tid, overrides={"budget": {"max_tokens": 1}})
    started = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=tid, budgets={"time_s": 100})
    cycle = started["cycle"]
    assert cycle["max_time_s"] == 100  # explicit wins
    assert cycle["max_tokens"] == 9000
    assert cycle["max_cost_cents"] == 250
    assert started["operating_budget"] == ["cost_cents", "tokens"]
    plain = cycles.autonomous_cycle_start("Desktop_Agent1")
    assert "operating_budget" not in plain


def test_brief_and_packet_carry_planning_and_instructions(tmp_path, monkeypatch):
    from unified_agent.lifecycle.packet import build_session_packet

    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("brief")["id"]
    sprint = human(
        operating_profiles.sprint_add,
        "S",
        overrides={"planning": "P4", "instructions": "Cite the task id in every commit."},
    )
    human(operating_profiles.sprint_link_task, sprint["id"], tid)
    brief = operating_profiles.append_operating_brief("Do the task.", tid, agent="Desktop_Agent1")
    assert brief.startswith("Do the task.\n\n## Operating profile")
    assert "Planning P4: Full design review" in brief
    assert f"Instructions (set by sprint:{sprint['id']}:overrides):" in brief
    assert "Cite the task id in every commit." in brief
    claim(tid, "Desktop_Agent1")
    human(operating_profiles.sprint_update, sprint["id"], overrides={"planning": "P0"})
    packet = build_session_packet(task_id=tid)["operating_profile"]
    assert packet["planning"] == "P4"  # frozen at claim
    assert packet["instructions"] == "Cite the task id in every commit."
    assert "## Operating profile" in packet["brief"]
    assert operating_profiles.append_operating_brief("x", None) == "x"


# --- review follow-ups (PR #961) -------------------------------------------


def test_claim_response_carries_the_snapshot_for_remote_executors(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("remote")["id"]
    human(
        operating_profiles.run_settings_set,
        tid,
        overrides={"model": "claude-opus-5-5", "instructions": "Write a design first"},
    )
    claimed = claim(tid, "Desktop_Agent1")
    present, snap = operating_profiles.claim_snapshot(claimed)
    assert present and snap["settings"]["model"] == "claude-opus-5-5"

    # A remote executor's local store is a tombstone: nothing may read it.
    def unreadable():
        raise storage.LiveStoreError("tombstone")

    monkeypatch.setattr(storage, "connect", unreadable)
    brief = operating_profiles.append_operating_brief(
        "Do it.", tid, agent="Desktop_Agent1", claim=claimed
    )
    assert "Write a design first" in brief
    assert operating_profiles.profile_model(
        snap, agent_label="Desktop_Agent1", backend="claude"
    ) == ("claude-opus-5-5", "applied")
    # Without a claim response the local read fails and the brief is bare.
    assert operating_profiles.append_operating_brief("Do it.", tid, agent="Desktop_Agent1") == (
        "Do it."
    )


def test_unreadable_snapshot_never_fails_a_claim(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("snapshot read fails")["id"]

    def broken(conn, task_id, **kwargs):
        raise RuntimeError("run record unreadable")

    monkeypatch.setattr(operating_profiles, "run_snapshot", broken)
    claimed = claim(tid, "Desktop_Agent1")
    assert claimed["claimed_by"] == "Desktop_Agent1"
    assert claimed["operating_profile"] is None


def test_claim_response_without_a_profile_is_not_a_local_lookup(tmp_path, monkeypatch):
    write_raw_policy(tmp_path, monkeypatch, unrelated={})
    tid = tasks.task_add("none")["id"]
    claimed = claim(tid, "Desktop_Agent1")
    assert operating_profiles.claim_snapshot(claimed) == (True, None)
    # A hub that predates the field has no key: callers fall back locally.
    assert operating_profiles.claim_snapshot({"id": tid}) == (False, None)
    assert operating_profiles.append_operating_brief("x", tid, claim=claimed) == "x"


def test_autonomy_preview_reports_the_profile_mode_per_pending_action(tmp_path, monkeypatch):
    from unified_agent import autonomy_modes

    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("preview")["id"]
    gid = _mission_for(tid)
    claim(tid, "Desktop_Agent2")  # unit platform -> crew -> checkpoint
    autonomy_modes.evaluate(gid, "merge", "Desktop_Agent2")
    mission = autonomy_modes.preview(gid)
    # The mission-level scalar is only the stored fallback.
    assert mission["effective_mode"] == "action-review"
    assert mission["effective_mode_scope"] == "mission-fallback"
    (pending,) = mission["pending_not_started"]
    assert pending["effective_mode"] == "checkpoint"
    assert "merge" in pending["checkpoint_action_classes"]
    # An action with no mode surfaces with the profile mode, not the fallback.
    exact = autonomy_modes.preview_action(gid, "merge", "Desktop_Agent2")
    assert exact["pending_id"] == pending["id"]


@pytest.mark.parametrize(
    "tier_yolo, note, expect_yolo",
    [
        (True, "[delivery: mode=direct-pr]", True),
        (True, "[delivery: mode=direct-pr yolo=off]", False),
        (None, "[delivery: mode=direct-pr]", False),
        (False, "[delivery: mode=direct-pr yolo=on]", True),
    ],
)
def test_note_that_omits_yolo_defers_to_the_org_profile(
    tmp_path, monkeypatch, tier_yolo, note, expect_yolo
):
    from unified_agent.delivery import resolve_delivery

    delivery = {"mode": "no-mistakes"}
    if tier_yolo is not None:
        delivery["yolo"] = tier_yolo
    block = {"default": "standard", "profiles": {"standard": {"delivery": delivery}}}
    write_policy(tmp_path, monkeypatch, block=block)
    delivery_file(tmp_path, monkeypatch, {"projects": {"p": {"mode": "local-only"}}})
    tid = tasks.task_add("note")["id"]
    claim(tid, "Desktop_Agent5")
    policy = resolve_delivery("p", notes=note, snapshot=open_snapshot(tid))
    assert policy.mode == "direct-pr" and policy.source == "note"
    assert policy.yolo is expect_yolo


def test_non_finite_budget_never_becomes_a_cycle_cap(tmp_path, monkeypatch):
    from unified_agent.tools import cycles

    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("budget")["id"]
    claim(tid, "Desktop_Agent1")
    for bad in (float("inf"), float("nan"), 1e308):
        snap = {"settings": {"budget": {"max_cost_usd": bad}}}
        assert "cost_cents" not in operating_profiles.budget_caps(snap)
    with pytest.raises(ToolError, match="finite"):
        cycles.autonomous_cycle_start("Desktop_Agent1", budgets={"cost_cents": float("inf")})
    with pytest.raises(ToolError, match="finite"):
        cycles.autonomous_cycle_start("Desktop_Agent1", budgets={"cost_cents": float("nan")})


def test_finished_run_delivery_stamp_does_not_govern_the_next_run(tmp_path, monkeypatch):
    from unified_agent.delivery import apply_finish_markers, resolve_delivery

    write_policy(tmp_path, monkeypatch)
    delivery_file(
        tmp_path, monkeypatch, {"projects": {"unified-agent": {"mode": "direct-pr", "yolo": False}}}
    )
    tid = tasks.task_add("two runs", project="unified-agent")["id"]
    sprint = human(
        operating_profiles.sprint_add,
        "S",
        overrides={"delivery": {"mode": "local-only", "yolo": True}},
    )
    human(operating_profiles.sprint_link_task, sprint["id"], tid)
    claim(tid, "Desktop_Agent2")
    notes = "work"
    # Run A finishes: the executor stamps the policy its snapshot resolved.
    policy_a = resolve_delivery("unified-agent", notes=notes, snapshot=open_snapshot(tid))
    assert (policy_a.mode, policy_a.yolo) == ("local-only", True)
    stamped = apply_finish_markers(notes, policy_a, fresh_review=True)
    # Review of run A still reads A's frozen snapshot, not the note stamp.
    finished = operating_profiles.task_run_snapshot(tid, finished=True)
    assert resolve_delivery("unified-agent", notes=stamped, snapshot=finished).yolo is True

    # The operator removes the sprint override, then run B starts.
    human(operating_profiles.sprint_update, sprint["id"], overrides={})
    token = as_identity("Desktop_Agent2")
    try:
        tasks.task_update(tid, status="done")
        tasks.task_update(tid, status="open")
    finally:
        config.agent_identity_var.reset(token)
    claim(tid, "Desktop_Agent2")
    policy_b = resolve_delivery("unified-agent", notes=stamped, snapshot=open_snapshot(tid))
    assert (policy_b.mode, policy_b.yolo, policy_b.source) == ("direct-pr", False, "file")
    # The run B review reads B's snapshot, so A's stamp cannot merge B's work.
    review_b = resolve_delivery(
        "unified-agent",
        notes=stamped,
        snapshot=operating_profiles.task_run_snapshot(tid, finished=True),
    )
    assert (review_b.mode, review_b.yolo) == ("direct-pr", False)


def test_run_overrides_without_named_profiles_are_frozen_into_the_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent-policy.yaml"))
    assert not operating_profiles.load_config().configured
    tid = tasks.task_add("overrides only")["id"]
    other = tasks.task_add("no settings")["id"]
    resolved = human(
        operating_profiles.run_settings_set,
        tid,
        overrides={"planning": "P4", "budget": {"max_tokens": 9000}},
    )
    assert resolved["settings"]["planning"] == "P4"
    claimed = claim(tid, "Desktop_Agent2")
    snap = open_snapshot(tid)
    assert snap["settings"]["planning"] == "P4"
    assert snap["settings"]["budget"] == {"max_tokens": 9000}
    assert snap["sources"]["planning"] == f"run:task-{tid}:overrides"
    assert claimed["operating_profile"]["settings"]["planning"] == "P4"
    brief = operating_profiles.append_operating_brief("Do it.", tid, agent="Desktop_Agent2")
    assert "P4" in brief
    # With no sprint or run settings at all nothing is frozen, as before.
    claim(other, "Desktop_Agent2")
    assert open_snapshot(other) is None
    assert operating_profiles.settings_for_launch(other, "Desktop_Agent2") is None


def _two_runs_with_a_backward_clock(tmp_path, monkeypatch):
    """Run A (checkpoint autonomy) then run B (none); B's wall-clock stamp is older."""
    write_policy(tmp_path, monkeypatch)
    tid = tasks.task_add("clock step")["id"]
    claim(tid, "Desktop_Agent2")  # platform -> crew -> autonomy checkpoint
    token = as_identity("Desktop_Agent2")
    try:
        tasks.task_update(tid, status="done")
        tasks.task_update(tid, status="open")
    finally:
        config.agent_identity_var.reset(token)
    claim(tid, "Desktop_Agent5")  # no unit: standard profile, no autonomy
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE run_operating_snapshots SET recorded_at='2000-01-01T00:00:00+00:00' "
            "WHERE agent='Desktop_Agent5'"
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def test_finished_snapshot_is_the_newest_claim_even_when_the_clock_stepped_back(
    tmp_path, monkeypatch
):
    tid = _two_runs_with_a_backward_clock(tmp_path, monkeypatch)
    finished = operating_profiles.task_run_snapshot(tid, finished=True)
    assert "autonomy" not in finished["settings"]
    conn = storage.connect()
    try:
        tied = operating_profiles._tied_context(
            conn, "Desktop_Agent5", tid, operating_profiles.load_config()
        )
    finally:
        conn.close()
    assert tied is not None


def test_claim_order_survives_a_migration_of_pre_column_rows(tmp_path, monkeypatch):
    from unified_agent import operating_policy

    tid = _two_runs_with_a_backward_clock(tmp_path, monkeypatch)
    conn = storage.connect()
    try:
        # A database from before claim_ulid: rows exist without it, and no backfill ran.
        conn.execute("UPDATE run_operating_snapshots SET claim_ulid=NULL")
        conn.execute("DELETE FROM meta WHERE key='operating_snapshots_claim_ulid_backfilled'")
        operating_policy.migrate(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT agent, claim_ulid FROM run_operating_snapshots WHERE task_id=? "
            "ORDER BY claim_ulid",
            (tid,),
        ).fetchall()
        assert [r["agent"] for r in rows] == ["Desktop_Agent2", "Desktop_Agent5"]
        assert all(r["claim_ulid"] for r in rows)
        assert (
            "autonomy" not in operating_profiles.run_snapshot(conn, tid, finished=True)["settings"]
        )
    finally:
        conn.close()


# --- sender-scoped snapshots and unverified callers (#1775) ------------------


def test_another_agents_later_claim_keeps_the_senders_frozen_policy(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch, dict(PROFILES, units={"platform": "crew"}))
    tid = tasks.task_add("handed over")["id"]
    claim(tid, "Desktop_Agent2")  # platform office: frozen funnel
    token = as_identity("Desktop_Agent2")
    try:
        tasks.task_update(tid, status="done")
        tasks.task_update(tid, status="open")
    finally:
        config.agent_identity_var.reset(token)
    claim(tid, "Desktop_Agent5")  # staff office: direct, now the task's newest run
    with pytest.raises(CommsPolicyDenied):
        send("Desktop_Agent2", "Desktop_Person", task_id=tid)
    assert send("Desktop_Agent5", "Desktop_Person", task_id=tid)["accepted"]


def test_unverified_notify_is_ungated_only_without_profiles(tmp_path, monkeypatch):
    from unified_agent.tools import notify as notify_tool

    monkeypatch.setattr(notify_tool, "_notify_impl", lambda *_a, **_k: {"delivered": True})
    assert notify_tool.notify("nothing configured")["delivered"]
    write_policy(tmp_path, monkeypatch)
    with pytest.raises(CommsPolicyDenied, match="agent token"):
        notify_tool.notify("configured")
    for route in sorted(operating_profiles.INTERNAL_ROUTES):
        assert notify_tool.notify_as("system", sender=None, route=route)["delivered"]
    conn = storage.connect()
    try:
        denied = conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.reason')='unverified caller'",
            (operating_profiles.DENIED_CHANNEL,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert denied == 1


def test_unverified_batch_notify_is_refused_while_profiles_apply(tmp_path, monkeypatch):
    from unified_agent.tools import digest

    write_policy(tmp_path, monkeypatch)
    with pytest.raises(CommsPolicyDenied):
        digest.batch_notify(["queued without a token"])


def test_internal_route_does_not_apply_inside_a_tool_call(tmp_path, monkeypatch):
    """check_anomalies_and_notify reached over MCP is judged as its caller (#1775)."""
    from unified_agent import tool_surface
    from unified_agent.tools import notify as notify_tool

    write_policy(tmp_path, monkeypatch)
    monkeypatch.setattr(notify_tool, "_notify_impl", lambda *_a, **_k: {"delivered": True})
    token = tool_surface._IN_TOOL_CALL.set(True)
    try:
        with notify_tool.internal_route("anomaly_alert"), pytest.raises(CommsPolicyDenied):
            notify_tool.notify("alert")
    finally:
        tool_surface._IN_TOOL_CALL.reset(token)
    with notify_tool.internal_route("anomaly_alert"):
        assert notify_tool.notify("alert")["delivered"]


def test_registered_tools_mark_the_governed_call(monkeypatch):
    from unified_agent import tool_surface

    monkeypatch.setattr(tool_surface, "_guard", lambda fn: fn)

    def probe_tool_call_marker() -> bool:
        return tool_surface.in_tool_call()

    try:
        wrapped = tool_surface.register(probe_tool_call_marker)
        assert wrapped() is True
        assert tool_surface.in_tool_call() is False
        assert probe_tool_call_marker() is False
    finally:
        tool_surface.REGISTERED_TOOLS.pop("probe_tool_call_marker", None)


def test_sender_scoped_snapshot_lookup_has_an_index():
    conn = storage.connect()
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='run_operating_snapshots'"
            )
        }
        plan = " ".join(
            str(r[-1])
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT run_id FROM run_operating_snapshots "
                "WHERE task_id=1 AND agent='a' ORDER BY recorded_at DESC LIMIT 1"
            )
        )
    finally:
        conn.close()
    assert "idx_run_operating_snapshots_task_agent" in names
    assert "idx_run_operating_snapshots_task_agent" in plan


def test_sprint_cannot_bind_another_tenants_goal(tmp_path, monkeypatch):
    """The goal check is tenant-fenced (task #1999): a foreign goal reads as unknown."""
    write_policy(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    a_goal = goal_manage("add", {"title": "Tenant A mission", "author": "Desktop_Person"})
    a_goal_id = a_goal["goal"]["id"]
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    with pytest.raises(ToolError, match="unknown goal"):
        human(operating_profiles.sprint_add, "Hijack", goal_id=a_goal_id)
    b_goal = goal_manage("add", {"title": "Tenant B mission", "author": "Desktop_Person"})
    sprint = human(operating_profiles.sprint_add, "Own", goal_id=b_goal["goal"]["id"])
    assert sprint["goal_id"] == b_goal["goal"]["id"]


def _as_org(monkeypatch, org):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)


def _sprint_org(sprint_id):
    conn = storage.connect()
    try:
        return conn.execute("SELECT org_id FROM sprints WHERE id=?", (sprint_id,)).fetchone()[0]
    finally:
        conn.close()


def test_sprints_are_tenant_stamped_and_fenced(tmp_path, monkeypatch):
    """Task #2014: a foreign sprint is unknown to get, list, update and link."""
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    a_sprint = human(operating_profiles.sprint_add, "Tenant A secret sprint")["id"]
    a_task = tasks.task_add("Tenant A work")["id"]
    assert _sprint_org(a_sprint) == "org_a"
    _as_org(monkeypatch, "org_b")
    b_sprint = human(operating_profiles.sprint_add, "Tenant B sprint")["id"]
    b_task = tasks.task_add("Tenant B work")["id"]
    assert _sprint_org(b_sprint) == "org_b"
    listed = operating_profiles.sprint_list()["sprints"]
    assert [s["id"] for s in listed] == [b_sprint]
    for call in (
        lambda: operating_profiles.sprint_get(a_sprint),
        lambda: goal_manage("operating_sprint_get", {"sprint_id": a_sprint}),
        lambda: human(operating_profiles.sprint_update, a_sprint, name="Hijacked"),
        lambda: human(operating_profiles.sprint_link_task, a_sprint, b_task),
    ):
        with pytest.raises(ToolError, match="unknown sprint"):
            call()
    with pytest.raises(ToolError, match="unknown task"):
        human(operating_profiles.sprint_link_task, b_sprint, a_task)
    # A pre-fence link of A's task to B's sprint does not disclose A's task id.
    conn = storage.connect()
    conn.execute(
        "INSERT INTO sprint_tasks (task_id, sprint_id, linked_by, linked_at) VALUES (?,?,?,?)",
        (a_task, b_sprint, "Desktop_Person", storage.now()),
    )
    conn.commit()
    conn.close()
    human(operating_profiles.sprint_link_task, b_sprint, b_task)
    assert operating_profiles.sprint_get(b_sprint)["task_ids"] == [b_task]
    _as_org(monkeypatch, "org_a")
    assert operating_profiles.sprint_get(a_sprint)["name"] == "Tenant A secret sprint"


def test_untenanted_hub_sees_only_untenanted_sprints(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    own = human(operating_profiles.sprint_add, "Private hub sprint")["id"]
    assert _sprint_org(own) is None
    _as_org(monkeypatch, "org_a")
    human(operating_profiles.sprint_add, "Tenant A sprint")
    monkeypatch.delenv("UA_EDITION")
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID")
    assert [s["id"] for s in operating_profiles.sprint_list()["sprints"]] == [own]


def test_existing_sprints_take_their_tenant_from_the_bound_goal(tmp_path, monkeypatch):
    """Task #2014: the one-time backfill reads the bound goal's org_id."""
    from unified_agent import operating_policy

    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    goal = goal_manage("add", {"title": "Tenant A mission", "author": "Desktop_Person"})
    bound = human(operating_profiles.sprint_add, "Bound", goal_id=goal["goal"]["id"])["id"]
    loose = human(operating_profiles.sprint_add, "Loose")["id"]
    conn = storage.connect()
    try:
        conn.execute("DROP INDEX IF EXISTS idx_sprints_org_id")
        conn.execute("ALTER TABLE sprints DROP COLUMN org_id")
        operating_policy.ensure_schema(conn)
        operating_policy.ensure_schema(conn)  # idempotent
        conn.commit()
    finally:
        conn.close()
    assert _sprint_org(bound) == "org_a"
    assert _sprint_org(loose) is None


# --- resolve and run settings tenant fence (task #2043) ----------------------------


def _untenanted(monkeypatch):
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)


def _link(task_id, sprint_id):
    """A sprint link written directly, as a pre-fence hub could have."""
    conn = storage.connect()
    conn.execute(
        "INSERT INTO sprint_tasks (task_id, sprint_id, linked_by, linked_at) VALUES (?,?,?,?)",
        (task_id, sprint_id, "Desktop_Person", storage.now()),
    )
    conn.commit()
    conn.close()


def test_resolve_refuses_another_tenants_sprint_id(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    secret = {"planning": "P4"}
    a_sprint = human(operating_profiles.sprint_add, "A", profile="crew", overrides=secret)["id"]
    _as_org(monkeypatch, "org_b")
    for call in (
        lambda: operating_profiles.resolve(sprint_id=a_sprint),
        lambda: goal_manage("operating_resolve", {"sprint_id": a_sprint}),
    ):
        with pytest.raises(ToolError, match="unknown sprint"):
            call()
    _as_org(monkeypatch, "org_a")
    assert operating_profiles.resolve(sprint_id=a_sprint)["settings"]["planning"] == "P4"


def test_task_resolves_only_a_sprint_of_its_own_tenant(tmp_path, monkeypatch):
    """The task row's tenant decides, not the caller's: claims and the executor run
    untenanted. A pre-fence link to a foreign sprint refuses, as a missing sprint does."""
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    a_sprint = human(
        operating_profiles.sprint_add, "A", profile="crew", overrides={"planning": "P4"}
    )["id"]
    _as_org(monkeypatch, "org_b")
    b_sprint = human(operating_profiles.sprint_add, "B", profile="crew")["id"]
    b_task = tasks.task_add("Tenant B work")["id"]
    b_own = tasks.task_add("Tenant B linked work")["id"]
    _link(b_task, a_sprint)
    human(operating_profiles.sprint_link_task, b_sprint, b_own)
    with pytest.raises(ToolError, match="unknown sprint"):
        goal_manage("operating_resolve", {"task_id": b_own, "sprint_id": a_sprint})
    _untenanted(monkeypatch)
    with pytest.raises(ToolError, match="unknown sprint"):
        operating_profiles.resolve(task_id=b_task)
    with pytest.raises(ToolError, match="unknown sprint"):
        operating_profiles.resolve(task_id=b_own, sprint_id=a_sprint)
    own = operating_profiles.resolve(task_id=b_own)
    assert own["sprint_id"] == b_sprint
    assert own["sources"]["autonomy"] == f"sprint:{b_sprint}:crew"
    # The foreign-linked task does not block an update of tenant B's own sprint.
    _as_org(monkeypatch, "org_b")
    human(operating_profiles.sprint_update, b_sprint, name="B renamed")


def test_run_settings_and_resolve_refuse_another_tenants_task(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    a_task = tasks.task_add("Tenant A work")["id"]
    _as_org(monkeypatch, "org_b")
    for call in (
        lambda: human(operating_profiles.run_settings_set, a_task, profile="crew"),
        lambda: human(operating_profiles.run_settings_clear, a_task),
        lambda: goal_manage("operating_resolve", {"task_id": a_task}),
    ):
        with pytest.raises(ToolError, match="unknown task"):
            call()
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM run_settings").fetchone()[0] == 0
    finally:
        conn.close()
    _as_org(monkeypatch, "org_a")
    assert human(operating_profiles.run_settings_set, a_task, profile="crew")["task_id"] == a_task


async def test_webui_resolve_refuses_another_tenants_task_and_sprint(tmp_path, monkeypatch):
    from unified_agent.webui import api

    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    a_task = tasks.task_add("Tenant A work")["id"]
    a_sprint = human(operating_profiles.sprint_add, "A")["id"]
    _as_org(monkeypatch, "org_b")
    with pytest.raises(ToolError, match="unknown task"):
        await api.operating_resolve_payload(a_task, None)
    with pytest.raises(ToolError, match="unknown sprint"):
        await api.operating_resolve_payload(None, a_sprint)


def test_untenanted_hub_resolves_task_sprint_and_run_layers(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    _untenanted(monkeypatch)
    sprint = human(operating_profiles.sprint_add, "S", profile="crew")["id"]
    tid = tasks.task_add("work")["id"]
    human(operating_profiles.sprint_link_task, sprint, tid)
    human(operating_profiles.run_settings_set, tid, overrides={"planning": "P3"})
    resolved = goal_manage("operating_resolve", {"task_id": tid})
    assert resolved["sprint_id"] == sprint
    assert resolved["sources"]["planning"] == f"run:task-{tid}:overrides"
    assert operating_profiles.resolve(sprint_id=sprint)["sprint_id"] == sprint


def test_agent_write_to_unknown_or_foreign_task_queues_no_approval(tmp_path, monkeypatch):
    """Task #2046: the fenced task check runs before the approval gate, so an agent's
    write against a task it cannot see is refused at once instead of on replay."""
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    a_task = tasks.task_add("Tenant A work")["id"]
    _as_org(monkeypatch, "org_b")
    b_task = tasks.task_add("Tenant B work")["id"]
    token = as_identity("Desktop_Agent2")
    try:
        for tid in (a_task, 999_999):
            with pytest.raises(ToolError, match="unknown task"):
                operating_profiles.run_settings_set(tid, profile="crew")
            with pytest.raises(ToolError, match="unknown task"):
                operating_profiles.run_settings_clear(tid)
        assert approvals.approval_pending()["pending"] == []
        with pytest.raises(approvals.ApprovalPendingError) as parked:
            operating_profiles.run_settings_set(b_task, profile="crew")
    finally:
        config.agent_identity_var.reset(token)
    [request] = approvals.approval_pending()["pending"]
    assert request["id"] == parked.value.approval_id


def test_run_clear_with_a_foreign_sprint_link_refuses_without_deleting(tmp_path, monkeypatch):
    """A clear the response cannot resolve rolls back, like run_set (review #1180):
    the caller never gets an error for a deletion that committed."""
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    a_sprint = human(operating_profiles.sprint_add, "A")["id"]
    _as_org(monkeypatch, "org_b")
    b_task = tasks.task_add("Tenant B work")["id"]
    human(operating_profiles.run_settings_set, b_task, profile="crew")
    _link(b_task, a_sprint)
    with pytest.raises(ToolError, match="unknown sprint"):
        human(operating_profiles.run_settings_clear, b_task)
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM run_settings WHERE task_id=?", (b_task,)).fetchone()[
                0
            ]
            == 1
        )
    finally:
        conn.close()
