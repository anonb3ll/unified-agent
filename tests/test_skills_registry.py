"""Skills registry: versions, assignment, dispatch injection, drift (task #1353)."""

from __future__ import annotations

import threading

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import skills_registry as reg

GOOD_DESC = (
    "Reviews a Python module for missing type hints and adds them. "
    "Use when asked to type-annotate, add hints, or fix mypy errors in Python code."
)

GOOD_BODY = """\
# Type hints

1. Run `uv run mypy <module>` and collect errors.
2. Annotate public functions first; use `from __future__ import annotations`.
3. Re-run mypy until clean.
"""


def make_skill(root, dirname, name=None, description=GOOD_DESC, body=GOOD_BODY, raw=None):
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    if raw is None:
        raw = f"---\nname: {name or dirname}\ndescription: {description}\n---\n\n{body}"
    (d / "SKILL.md").write_text(raw)
    return d


def test_publish_creates_version_one_and_is_idempotent(isolated_env):
    make_skill(isolated_env, "type-hints")
    result = reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    assert result["ok"] is True
    assert result["idempotent"] is False
    assert result["skill"]["version"] == 1
    assert "content" not in result["skill"]
    assert result["review"]["status"] == "pass"

    again = reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    assert again["ok"] is True
    assert again["idempotent"] is True
    assert again["skill"]["version"] == 1

    assert len(reg.list_skills("type-hints")["skills"]) == 1


def test_publish_blocked_by_high_severity_finding(isolated_env):
    make_skill(isolated_env, "broken", raw="# no frontmatter here\n\nsome body\n")
    result = reg.publish("broken", "broken", author="Desktop_Agent1")
    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["review"]["status"] == "fail"
    assert reg.list_skills("broken")["skills"] == []


def test_publish_increments_version_on_content_change(isolated_env):
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    make_skill(isolated_env, "type-hints", body=GOOD_BODY + "\nMore detail.\n")
    second = reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    assert second["ok"] is True
    assert second["idempotent"] is False
    assert second["skill"]["version"] == 2
    assert reg.get("type-hints")["version"] == 2
    assert reg.get("type-hints", version=1)["version"] == 1


def test_publish_rejects_bad_name(isolated_env):
    make_skill(isolated_env, "type-hints")
    try:
        reg.publish("Not Kebab", "type-hints", author="Desktop_Agent1")
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


@pytest.mark.parametrize("version", [1.5, True, "bad", 0, -1])
def test_get_rejects_invalid_versions(isolated_env, version):
    with pytest.raises(ToolError, match="positive integer"):
        reg.get("type-hints", version=version)


def test_assign_rejects_unknown_profile(isolated_env):
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    with pytest.raises(ToolError, match="unknown profile"):
        reg.assign("type-hints", "devloper", target_type="profile", author="a")


def test_unassign_normalizes_mixed_case_profile_targets(isolated_env):
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    reg.assign("type-hints", "Developer", target_type="profile", author="a")
    assert reg.resolve("Desktop_Agent1", profile="developer")["skills"]

    result = reg.unassign("type-hints", "DEVELOPER", target_type="profile", author="a")
    assert result["removed"] is True
    assert reg.resolve("Desktop_Agent1", profile="developer")["skills"] == []


def test_unassign_unknown_profile_is_idempotent(isolated_env):
    result = reg.unassign("type-hints", "devloper", target_type="profile", author="a")
    assert result["removed"] is False


def test_unassign_can_clean_up_a_retired_profile(isolated_env, monkeypatch):
    from unified_agent import profiles

    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    reg.assign("type-hints", "developer", target_type="profile", author="a")
    monkeypatch.setattr(profiles, "VALID_PROFILES", profiles.VALID_PROFILES - {"developer"})

    result = reg.unassign("type-hints", "DEVELOPER", target_type="profile", author="a")
    assert result["removed"] is True


def test_publish_rejects_credential_material(isolated_env):
    make_skill(
        isolated_env,
        "type-hints",
        body=f"{GOOD_BODY}\nTOKEN = 'abcdefghijklmnop'\n",
    )
    with pytest.raises(ToolError, match="credential material"):
        reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    assert reg.list_skills("type-hints")["skills"] == []


def test_publish_rejects_oversized_skill(isolated_env):
    make_skill(isolated_env, "type-hints", body="x" * (reg.MAX_SKILL_FILE_BYTES + 1))
    with pytest.raises(ToolError, match="publish limit"):
        reg.publish("type-hints", "type-hints", author="Desktop_Agent1")


def test_publish_rejects_invalid_utf8_before_creating_unreadable_version(isolated_env):
    skill_dir = make_skill(isolated_env, "type-hints")
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_bytes(skill_file.read_bytes() + b"\xff")
    with pytest.raises(ToolError, match="valid UTF-8"):
        reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    assert reg.list_skills("type-hints")["skills"] == []


def test_publish_rejects_skill_from_broad_audit_root(isolated_env, monkeypatch, tmp_path):
    audit_root = tmp_path / "audit-only"
    audit_root.mkdir()
    make_skill(audit_root, "type-hints")
    monkeypatch.setenv("UA_AUDIT_ROOTS", str(audit_root))

    with pytest.raises(ToolError, match="UA_SKILL_REGISTRY_ROOTS"):
        reg.publish("type-hints", str(audit_root / "type-hints"), author="Desktop_Agent1")
    assert reg.list_skills("type-hints")["skills"] == []


def test_publish_accepts_explicit_skill_registry_root(isolated_env, monkeypatch, tmp_path):
    registry_root = tmp_path / "approved-skill-source"
    registry_root.mkdir()
    skill_dir = make_skill(registry_root, "type-hints")
    monkeypatch.setenv("UA_AUDIT_ROOTS", str(registry_root))
    monkeypatch.setenv("UA_SKILL_REGISTRY_ROOTS", str(registry_root))

    result = reg.publish("type-hints", str(skill_dir), author="Desktop_Agent1")
    assert result["ok"] is True


def test_assign_requires_a_published_version(isolated_env):
    try:
        reg.assign("never-published", "Desktop_Agent1", author="Desktop_Agent1")
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


@pytest.mark.parametrize("version", [1.9, 0, -1, True, "1"])
def test_assign_rejects_non_positive_integer_pins(isolated_env, version):
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    with pytest.raises(ToolError, match="positive integer"):
        reg.assign("type-hints", "Desktop_Agent1", version=version, author="Desktop_Agent1")
    assert reg.resolve("Desktop_Agent1", profile="developer")["skills"] == []


def test_direct_assignment_keeps_priority_through_injection_budget(isolated_env):
    for name in ("alpha", "beta", "zeta"):
        make_skill(
            isolated_env,
            name,
            body=f"{GOOD_BODY}\n{name.upper()}_UNIQUE_MARKER\n" + ("x" * 4100),
        )
        assert reg.publish(name, name, author="Desktop_Agent1")["ok"] is True
    reg.assign("alpha", "developer", target_type="profile", author="a")
    reg.assign("beta", "developer", target_type="profile", author="a")
    reg.assign("zeta", "Desktop_Agent1", target_type="agent", author="a")

    resolved = reg.resolve("Desktop_Agent1", profile="developer")["skills"]
    assert [item["name"] for item in resolved] == ["zeta", "alpha", "beta"]
    prompt = reg.append_assigned_skills("Do the task.", "Desktop_Agent1", profile="developer")
    assert "ZETA_UNIQUE_MARKER" in prompt
    assert len(prompt) - len("Do the task.") <= reg.INJECTION_TOTAL_CAP


def test_concurrent_registry_mutations_publish_in_durable_order(monkeypatch, isolated_env):
    from unified_agent import storage

    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    published = []
    monkeypatch.setattr(reg.events, "publish", lambda event: published.append(event["ulid"]))
    barrier = threading.Barrier(6)

    def assign(index):
        barrier.wait()
        reg.assign("type-hints", f"Desktop_{index}", author="Desktop_Agent1")

    workers = [threading.Thread(target=assign, args=(i,)) for i in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT ulid FROM event_log WHERE channel='skill.assigned' ORDER BY ulid"
        ).fetchall()
    finally:
        conn.close()
    assert published == [row["ulid"] for row in rows]


def test_assign_and_resolve_agent_level(isolated_env):
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    reg.assign("type-hints", "Desktop_Agent1", target_type="agent", author="Desktop_Agent1")

    resolved = reg.resolve("Desktop_Agent1", profile="developer")
    names = {s["name"] for s in resolved["skills"]}
    assert names == {"type-hints"}
    assert resolved["skills"][0]["version"] == 1
    assert resolved["skills"][0]["hash"]
    assert "Type hints" in resolved["skills"][0]["content"]


def test_registry_mutations_fan_out_durable_events(monkeypatch, isolated_env):
    from unified_agent import storage

    published = []

    def receive(event):
        conn = storage.connect()
        try:
            row = conn.execute("SELECT 1 FROM event_log WHERE ulid=?", (event["ulid"],)).fetchone()
        finally:
            conn.close()
        assert row is not None, "live fan-out must happen after the event commits"
        published.append(event)

    monkeypatch.setattr(reg.events, "publish", receive)
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    reg.assign("type-hints", "Desktop_Agent1", author="Desktop_Agent1")
    reg.unassign("type-hints", "Desktop_Agent1", author="Desktop_Agent1")
    reg.unassign("type-hints", "Desktop_Agent1", author="Desktop_Agent1")

    assert [event["channel"] for event in published] == [
        "skill.published",
        "skill.assigned",
        "skill.unassigned",
    ]


def test_agent_assignment_wins_over_profile_assignment(isolated_env):
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    make_skill(isolated_env, "type-hints", body=GOOD_BODY + "\nProfile edition.\n")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")  # version 2

    reg.assign("type-hints", "developer", target_type="profile", version=2, author="a")
    reg.assign("type-hints", "Desktop_Agent1", target_type="agent", version=1, author="a")

    resolved = reg.resolve("Desktop_Agent1", profile="developer")
    assert resolved["skills"][0]["version"] == 1


def test_unassign_removes_the_link(isolated_env):
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    reg.assign("type-hints", "Desktop_Agent1", author="Desktop_Agent1")
    assert reg.resolve("Desktop_Agent1")["skills"]
    out = reg.unassign("type-hints", "Desktop_Agent1", author="Desktop_Agent1")
    assert out["removed"] is True
    assert reg.resolve("Desktop_Agent1")["skills"] == []
    assert reg.unassign("type-hints", "Desktop_Agent1", author="Desktop_Agent1")["removed"] is False


def test_resolve_is_bounded_and_never_crashes_on_empty_registry(isolated_env):
    assert reg.resolve("")["skills"] == []
    assert reg.resolve("Nobody_Registered")["skills"] == []


def test_append_assigned_skills_is_a_noop_without_assignments(isolated_env):
    prompt = "Do the task."
    assert reg.append_assigned_skills(prompt, "Desktop_Agent1") == prompt


def test_append_assigned_skills_decorates_the_prompt(isolated_env):
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    reg.assign("type-hints", "Desktop_Agent1", author="Desktop_Agent1")
    decorated = reg.append_assigned_skills("Do the task.", "Desktop_Agent1")
    assert decorated.startswith("Do the task.")
    assert "type-hints v1" in decorated
    assert len(decorated) - len("Do the task.") <= reg.INJECTION_TOTAL_CAP


def test_append_assigned_skills_caps_the_entire_suffix(monkeypatch):
    monkeypatch.setattr(
        reg,
        "resolve",
        lambda *_args, **_kwargs: {
            "skills": [
                {"name": f"skill-{i}", "version": 1, "hash": "a" * 64, "content": "x" * 4000}
                for i in range(reg.MAX_RESOLVED_SKILLS)
            ]
        },
    )
    prompt = "Do the task.   "
    decorated = reg.append_assigned_skills(prompt, "Desktop_Agent1")
    assert len(decorated) - len(prompt.rstrip()) <= reg.INJECTION_TOTAL_CAP


def test_drift_reports_missing_stale_unregistered_synced(isolated_env):
    surface = isolated_env / "surface-skills"
    surface.mkdir()
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    make_skill(isolated_env, "goal-writer")
    reg.publish("goal-writer", "goal-writer", author="Desktop_Agent1")

    # type-hints on the surface, in sync.
    make_skill(surface, "type-hints")
    # goal-writer on the surface, but stale content.
    make_skill(surface, "goal-writer", body=GOOD_BODY + "\nStale local edit.\n")
    # an on-disk skill the registry has never heard of.
    make_skill(surface, "not-in-registry")

    report = reg.drift("surface-skills")
    assert {s["name"] for s in report["synced"]} == {"type-hints"}
    assert {s["name"] for s in report["stale"]} == {"goal-writer"}
    assert {s["name"] for s in report["unregistered"]} == {"not-in-registry"}
    assert report["missing"] == []
    assert report["comparison"] == "latest_published"


def test_drift_bounds_surface_skill_count(isolated_env):
    surface = isolated_env / "large-surface"
    surface.mkdir()
    for index in range(reg.MAX_DRIFT_SKILLS + 1):
        (surface / f"skill-{index:03d}").mkdir()
    make_skill(surface, "one-actual-skill")
    assert reg.drift("large-surface")["unregistered"][0]["name"] == "one-actual-skill"


def test_drift_caps_actual_skill_directories(isolated_env):
    surface = isolated_env / "large-surface"
    surface.mkdir()
    for index in range(reg.MAX_DRIFT_SKILLS + 1):
        skill_dir = surface / f"skill-{index:03d}"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("skill\n")
    with pytest.raises(ToolError, match="directory limit"):
        reg.drift("large-surface")


def test_drift_bounds_surface_skill_file_size(isolated_env):
    surface = isolated_env / "surface-skills"
    surface.mkdir()
    make_skill(surface, "too-large", body="x" * (reg.MAX_SKILL_FILE_BYTES + 1))
    with pytest.raises(ToolError, match="drift scan limit"):
        reg.drift("surface-skills")


def test_drift_reports_missing_when_assigned_skill_absent_from_surface(isolated_env):
    surface = isolated_env / "surface-skills"
    surface.mkdir()
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    reg.assign("type-hints", "Desktop_Agent1", author="Desktop_Agent1")

    report = reg.drift("surface-skills", agent_label="Desktop_Agent1")
    assert {s["name"] for s in report["missing"]} == {"type-hints"}


def test_agent_drift_scopes_comparison_and_reports_unassigned_registered_skills(isolated_env):
    surface = isolated_env / "surface-skills"
    surface.mkdir()
    for name in ("assigned-skill", "unassigned-skill"):
        make_skill(isolated_env, name)
        reg.publish(name, name, author="Desktop_Agent1")
        make_skill(surface, name)
    make_skill(surface, "not-in-registry")
    reg.assign("assigned-skill", "Desktop_Agent1", author="Desktop_Agent1")

    report = reg.drift("surface-skills", agent_label="Desktop_Agent1")
    assert [item["name"] for item in report["synced"]] == ["assigned-skill"]
    assert [item["name"] for item in report["unassigned"]] == ["unassigned-skill"]
    assert [item["name"] for item in report["unregistered"]] == ["not-in-registry"]
    assert report["stale"] == []
    assert report["missing"] == []


def test_resolve_prioritizes_agent_assignments_before_profile_cap(isolated_env):
    for i in range(reg.MAX_RESOLVED_SKILLS):
        name = f"profile-skill-{i:02d}"
        make_skill(isolated_env, name)
        reg.publish(name, name, author="Desktop_Agent1")
        reg.assign(name, "developer", target_type="profile", author="Desktop_Agent1")
    name = "agent-only-skill"
    make_skill(isolated_env, name)
    reg.publish(name, name, author="Desktop_Agent1")
    reg.assign(name, "Desktop_Agent1", author="Desktop_Agent1")

    resolved = reg.resolve("Desktop_Agent1", profile="developer")["skills"]
    names = {item["name"] for item in resolved}
    assert len(resolved) == reg.MAX_RESOLVED_SKILLS
    assert "agent-only-skill" in names
    assert "profile-skill-19" not in names


def test_publish_rejects_content_changed_during_review(isolated_env, monkeypatch):
    skill_dir = make_skill(isolated_env, "type-hints")

    def edit_during_review(_path):
        (skill_dir / "SKILL.md").write_text("unreviewed content")
        return {"status": "pass", "findings": []}

    monkeypatch.setattr(reg, "skill_review", edit_during_review)
    try:
        reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
        raise AssertionError("expected ToolError")
    except ToolError as exc:
        assert "changed while it was being reviewed" in str(exc)
    assert reg.list_skills("type-hints")["skills"] == []


def test_drift_never_writes_to_the_surface_dir(isolated_env):
    surface = isolated_env / "surface-skills"
    surface.mkdir()
    make_skill(surface, "type-hints")
    before = sorted(p.name for p in surface.rglob("*"))
    reg.drift("surface-skills")
    after = sorted(p.name for p in surface.rglob("*"))
    assert before == after


def test_drift_rejects_a_non_directory(isolated_env):
    make_skill(isolated_env, "type-hints")
    try:
        reg.drift("type-hints/SKILL.md")
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


def test_skill_registry_tool_action_dispatch(isolated_env):
    make_skill(isolated_env, "type-hints")
    published = reg.skill_registry(
        "publish", {"name": "type-hints", "path": "type-hints", "author": "Desktop_Agent1"}
    )
    assert published["ok"] is True

    listed = reg.skill_registry("list", {})
    assert len(listed["skills"]) == 1

    got = reg.skill_registry("get", {"name": "type-hints"})
    assert got["version"] == 1

    assigned = reg.skill_registry(
        "assign",
        {"skill_name": "type-hints", "target": "Desktop_Agent1", "author": "Desktop_Agent1"},
    )
    assert assigned["assignment"]["target"] == "Desktop_Agent1"

    resolved = reg.skill_registry("resolve", {"agent_label": "Desktop_Agent1"})
    assert resolved["skills"][0]["name"] == "type-hints"

    unassigned = reg.skill_registry(
        "unassign",
        {"skill_name": "type-hints", "target": "Desktop_Agent1", "author": "Desktop_Agent1"},
    )
    assert unassigned["removed"] is True

    try:
        reg.skill_registry("bogus", {})
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


def test_skill_registry_tool_get_unknown_raises(isolated_env):
    try:
        reg.skill_registry("get", {"name": "nope"})
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


def test_drift_compares_a_pinned_assignment_against_its_pinned_version(isolated_env):
    surface = isolated_env / "surface-skills"
    surface.mkdir()
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    make_skill(surface, "type-hints")
    make_skill(isolated_env, "type-hints", body=GOOD_BODY + "\nVersion two.\n")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    reg.assign("type-hints", "Desktop_Agent1", version=1, author="Desktop_Agent1")

    pinned = reg.drift("surface-skills", agent_label="Desktop_Agent1")
    assert [s["name"] for s in pinned["synced"]] == ["type-hints"]
    assert pinned["stale"] == []
    # Without an agent, the surface is compared against the newest version.
    assert [s["name"] for s in reg.drift("surface-skills")["stale"]] == ["type-hints"]
    assert reg.drift("surface-skills")["comparison"] == "latest_published"
    assert pinned["comparison"] == "resolved_assignments"


def test_list_omits_skill_content_but_get_returns_it(isolated_env):
    make_skill(isolated_env, "type-hints")
    reg.publish("type-hints", "type-hints", author="Desktop_Agent1")
    listed = reg.list_skills()["skills"]
    assert listed and "content" not in listed[0]
    assert reg.get("type-hints")["content"]
