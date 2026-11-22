import datetime as dt
import json
import plistlib
import sqlite3
import subprocess
from pathlib import Path

import pytest
from export_guard import needs_exported_paths

from unified_agent import config, planning_loop
from unified_agent.planning_loop import (
    build_decision_packet,
    build_goal_proposal,
    build_next_work_view,
    build_weekly_plan,
    render_next_work_markdown,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
# The public export drops deploy/mac-hub/ (task #2615): the two bodies that
# read the Mac LaunchAgent assets skip there and still run here.
needs_mac_hub_assets = needs_exported_paths(
    _REPO_ROOT / "deploy" / "mac-hub" / "com.unified-agent.planning-loop.plist",
    _REPO_ROOT / "deploy" / "mac-hub" / "run-planning-loop.sh",
    repo=_REPO_ROOT,
)


def test_weekly_plan_maps_explicit_coverage_and_flags_missing_child():
    tasks = [
        {
            "id": 10,
            "title": "Epic",
            "notes": "[project-spec] [product-1]\nDoD (epic): API read is live; no task is filed.",
            "status": "open",
            "parent_id": None,
            "priority": "high",
            "domain": "professional",
            "lane": "default",
        },
        {
            "id": 11,
            "title": "API reader",
            "notes": "[covers: API read is live]",
            "status": "doing",
            "parent_id": 10,
            "priority": "high",
            "domain": "professional",
            "lane": "dispatch-ready",
        },
        {
            "id": 12,
            "title": "Internal project",
            "notes": "[project-spec]\nDoD: excluded from the Product 1 plan",
            "status": "open",
            "parent_id": None,
            "priority": "med",
            "lane": "default",
        },
    ]

    report = build_weekly_plan(tasks, release_tags=["v0.3.0"])

    assert "| #10 | Epic | doing | 1/2 DoD clauses covered |" in report
    assert "API read is live → #11 API reader" in report
    assert "no task is filed → GAP" in report
    assert "v0.3.0" in report
    assert "| #12 | Internal project |" not in report


def test_weekly_plan_uses_child_spec_text_for_coverage():
    tasks = [
        {
            "id": 40,
            "title": "Parent",
            "notes": "[project-spec] [product-1]\nDoD: (1) signed export verifies; (2) tampering fails.",
            "status": "open",
            "parent_id": None,
            "priority": "med",
            "lane": "default",
        },
        {
            "id": 41,
            "title": "Signed export",
            "notes": "Definition of Done:\n- signed export verifies",
            "status": "open",
            "parent_id": 40,
            "priority": "med",
            "lane": "dispatch-ready",
        },
    ]

    report = build_weekly_plan(tasks)

    assert "1/2 DoD clauses covered" in report
    assert "signed export verifies → #41 Signed export" in report
    assert "tampering fails → GAP" in report


def test_decision_packet_includes_human_only_and_two_edition_default():
    tasks = [
        {
            "id": 20,
            "title": "Choose pilot",
            "notes": "[decision-packet item]\nRecommendation: defer",
            "status": "open",
            "parent_id": None,
            "priority": "high",
            "domain": "professional",
            "lane": "default",
        },
        {
            "id": 21,
            "title": "Rotate secret",
            "notes": "Recommendation: yes",
            "status": "open",
            "parent_id": None,
            "priority": "high",
            "domain": "professional",
            "lane": "operator-only",
        },
        {
            "id": 22,
            "title": "Record missing recommendation",
            "notes": "[decision-packet item]",
            "status": "open",
            "parent_id": None,
            "priority": "med",
            "domain": "professional",
            "lane": "default",
        },
        {
            "id": 23,
            "title": "Already completed",
            "notes": "[decision-packet item]\nRecommendation: yes",
            "status": "done",
            "parent_id": None,
            "priority": "high",
            "domain": "professional",
            "lane": "default",
        },
    ]

    packet = build_decision_packet(
        tasks,
        prior_undecided_editions={20: 1, 21: 1, 22: 1},
    )

    assert "#20 Choose pilot" in packet
    assert "Recommendation: defer" in packet
    assert "Defaulted after two consecutive editions: defer" in packet
    assert "#21 Rotate secret" in packet
    assert "Operator-only" in packet
    assert "Decision: undecided (edition 2 of 2)" in packet
    assert "Defaulted after two consecutive editions: yes" not in packet
    assert "#22 Record missing recommendation" in packet
    assert "MISSING — add a recorded recommendation to the task." in packet
    assert "Decision: undecided (edition 2 of 2)" in packet
    assert "#23 Already completed" not in packet


def test_goal_proposal_has_spec_sections_and_never_files_children():
    epic = {
        "id": 30,
        "title": "Approval Ledger",
        "notes": "[project-spec]\nDoD:\n- Export signed evidence",
        "status": "open",
        "parent_id": None,
        "priority": "high",
        "domain": "professional",
        "lane": "default",
    }

    proposal = build_goal_proposal(
        "Make evidence exports verifiable",
        epic,
        ["Export signed evidence"],
    )

    assert "Proposal only — human or Chief of Staff acceptance required" in proposal
    assert "Objective:" in proposal
    assert "Scope:" in proposal
    assert "Boundaries:" in proposal
    assert "DoD:" in proposal
    assert "Verification:" in proposal
    assert "Parent epic: #30" in proposal


def test_runner_writes_only_generated_vault_artifacts(monkeypatch, tmp_path):
    tasks = [
        {
            "id": 50,
            "title": "Epic",
            "notes": "[project-spec] [product-1]\nDoD: ship it",
            "status": "open",
            "parent_id": None,
            "priority": "high",
            "lane": "default",
        }
    ]
    written = []
    monkeypatch.setattr(planning_loop, "_read_tasks", lambda: tasks)
    monkeypatch.setattr(planning_loop, "_release_tags", lambda _repo: ["v1.0.0"])
    monkeypatch.setattr(planning_loop.config, "vault_root", lambda: tmp_path)
    monkeypatch.setattr(
        "unified_agent.tools.vault.vault_write",
        lambda path, content, **_kw: written.append((path, content)),
    )

    result = planning_loop.run_planning_loop(
        generated_at=dt.date(2026, 9, 21),
        write=True,
    )

    assert result["status"] == "written"
    assert {path for path, _ in written} == {
        "Wiki/Product-1-Approval-Ledger-Plan.md",
        "Wiki/Decision-Packets/Decision-Packet-2026-09-21.md",
    }
    assert all("task_add" not in content for _, content in written)


def test_previous_packet_edition_state_tracks_undecided_and_defaulted(tmp_path):
    folder = tmp_path / "Wiki" / "Decision-Packets"
    folder.mkdir(parents=True)
    (folder / "Decision-Packet-2026-09-14.md").write_text(
        "## #60 Needs a choice\n"
        "- Decision: undecided (edition 1 of 2)\n\n"
        "## #61 Already defaulted\n"
        "- Defaulted after two consecutive editions: defer\n",
        encoding="utf-8",
    )

    undecided, defaulted = planning_loop._prior_decision_state(
        tmp_path,
        dt.date(2026, 9, 21),
    )

    assert undecided == {60: 1}
    assert defaulted == {61: "defer"}


def test_generated_plan_replaces_only_its_block_and_keeps_manual_sections():
    original = (
        "---\ntitle: Product plan\n---\n"
        "## 1. Live status (verified against the hub, 2026-09-19)\n"
        "manual epic table and deployment notes\n\n"
        "## 2. The buyer-facing five\n"
        "positioning and horizon\n"
    )
    generated = "## Ledger-derived forward plan\n\nfresh queue"

    first = planning_loop.merge_weekly_plan(
        original,
        generated,
        dt.date(2026, 9, 21),
    )
    second = planning_loop.merge_weekly_plan(
        first,
        "## Ledger-derived forward plan\n\nnewer queue",
        dt.date(2026, 9, 28),
    )

    assert "manual epic table and deployment notes" in first
    assert "Historical live-status snapshot (manual, 2026-09-19)" in first
    assert "positioning and horizon" in first
    assert "fresh queue" not in second
    assert "newer queue" in second
    assert second.count("planning-loop:begin") == 1


def test_generated_plan_refuses_to_clobber_unrecognized_manual_page():
    with pytest.raises(planning_loop.ToolError, match="refusing to replace"):
        planning_loop.merge_weekly_plan(
            "# the operator's manual page\nContent stays protected.",
            "generated plan",
            dt.date(2026, 9, 21),
        )


@needs_mac_hub_assets
def test_weekly_timer_configuration_is_enabled_by_weekly_schedule():
    timer = Path("deploy/systemd/ua-planning-loop.timer").read_text(encoding="utf-8")
    with Path("deploy/mac-hub/com.unified-agent.planning-loop.plist").open("rb") as stream:
        launchd = plistlib.load(stream)

    assert "OnCalendar=Mon *-*-* 04:00:00" in timer
    assert "Persistent=true" in timer
    assert launchd["StartCalendarInterval"] == {"Weekday": 1, "Hour": 4, "Minute": 0}
    assert launchd["RunAtLoad"] is False


def test_weekly_planning_loop_reads_the_hub_service_environment():
    """The scheduled job must honour UA_HOME/UA_VAULT_PATH like its sibling units.

    Without the EnvironmentFile it silently falls back to the default database
    and vault, so the generated plan/packet can describe the wrong ledger.
    """
    service = Path("deploy/systemd/ua-planning-loop.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=-%h/.unified_agent/service.env" in service


@needs_mac_hub_assets
def test_mac_planning_wrapper_sources_the_hub_service_environment(tmp_path):
    """The Mac LaunchAgent wrapper must load service.env before exec, like the
    hub and every sibling Mac wrapper, so both platforms read the same ledger."""
    wrapper = Path("deploy/mac-hub/run-planning-loop.sh").read_text(encoding="utf-8")
    source_at = wrapper.index('source "$ENV_FILE"')
    assert 'ENV_FILE="${HOME}/.unified_agent/service.env"' in wrapper
    assert "set -a" in wrapper[:source_at]
    assert source_at < wrapper.index("exec ")


def test_ledger_reader_uses_read_only_database_connection(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, notes TEXT, "
            "status TEXT, parent_id INTEGER, archived_at TEXT)"
        )
        conn.execute("INSERT INTO tasks VALUES (1, 'Epic', '[project-spec]', 'open', NULL, NULL)")
    monkeypatch.setattr(planning_loop.config, "db_path", lambda: db)

    tasks = planning_loop._read_tasks()

    assert tasks == [
        {
            "id": 1,
            "title": "Epic",
            "notes": "[project-spec]",
            "status": "open",
            "parent_id": None,
            "archived_at": None,
        }
    ]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_coverage_requires_marker_or_exact_child_dod_and_escapes_titles():
    tasks = [
        {
            "id": 70,
            "title": "Plan | next\nweek",
            "parent_id": None,
            "notes": "[project-spec] [product-1]\nDoD: signed export verifies; access is scoped; tampering fails",
            "status": "open",
            "priority": "high",
        },
        {
            "id": 71,
            "title": "Signer\nworker",
            "parent_id": 70,
            "notes": "We discussed signed export verifies, but this task does not cover it.\n"
            "DoD: access is scoped",
            "status": "open",
            "priority": "high",
        },
        {
            "id": 72,
            "title": "Tamper proof",
            "parent_id": 70,
            "notes": "[covers: dod-3]",
            "status": "open",
            "priority": "med",
        },
    ]
    report = build_weekly_plan(tasks)
    assert r"| #70 | Plan \| next week |" in report
    assert "signed export verifies → GAP" in report
    assert "access is scoped → #71 Signer worker" in report
    assert "tampering fails → #72 Tamper proof" in report


def test_dod_parser_reenters_after_other_headings():
    notes = (
        "## DoD: first clause\n"
        "## Notes\nbackground\n"
        "### DoD (cont): second clause\n"
        "- third clause\n"
        "## Verification\nnot a clause\n"
        "DoD: fourth clause"
    )
    assert planning_loop._dod_clauses(notes) == [
        "first clause",
        "second clause",
        "third clause",
        "fourth clause",
    ]


def test_prior_packet_scans_past_skipped_edition(tmp_path):
    folder = tmp_path / "Wiki" / "Decision-Packets"
    folder.mkdir(parents=True)
    (folder / "Decision-Packet-2026-09-07.md").write_text(
        "## #80 Still pending\n- Decision: undecided (edition 1 of 2)\n"
        "## #81 Defaulted\n- Defaulted after two consecutive editions: defer\n"
    )
    (folder / "Decision-Packet-2026-09-14.md").write_text(
        "## #82 New item\n- Decision: undecided (edition 1 of 2)\n"
    )
    undecided, defaulted = planning_loop._prior_decision_state(tmp_path, dt.date(2026, 9, 21))
    assert undecided == {80: 1, 82: 1}
    assert defaulted == {81: "defer"}


def test_legacy_plan_migration_accepts_other_section_titles():
    original = (
        "## 1. Live status (checked 2026-09-19)\nold snapshot\n\n"
        "## 2. Buyer readiness\nmanual content\n"
    )
    merged = planning_loop.merge_weekly_plan(original, "generated content", dt.date(2026, 9, 21))
    assert "generated content" in merged
    assert "old snapshot" in merged
    assert "## 2. Buyer readiness\nmanual content" in merged


def test_release_metadata_degrades_on_non_git_install(tmp_path):
    assert planning_loop._release_tags(tmp_path) == []
    assert planning_loop._release_description(tmp_path) == ""


def test_prior_packet_decision_resets_older_undecided_state(tmp_path):
    folder = tmp_path / "Wiki" / "Decision-Packets"
    folder.mkdir(parents=True)
    (folder / "Decision-Packet-2026-09-07.md").write_text(
        "## #80 Reconsider later\n- Decision: undecided (edition 1 of 2)\n"
    )
    (folder / "Decision-Packet-2026-09-14.md").write_text(
        "## #80 Reconsider later\n- Decision recorded on task: no\n"
    )
    undecided, defaulted = planning_loop._prior_decision_state(tmp_path, dt.date(2026, 9, 21))
    assert undecided == {}
    assert defaulted == {}


def _packet_tasks():
    return [
        {
            "id": 90,
            "title": "Pick a pilot",
            "notes": "[decision-packet item]\nRecommendation: defer",
            "status": "open",
            "parent_id": None,
            "priority": "high",
            "lane": "default",
        }
    ]


def test_decision_packet_emits_frontmatter_with_aliases():
    import yaml

    packet = build_decision_packet(_packet_tasks(), packet_date=dt.date(2026, 9, 21))

    assert packet.startswith("---\n")
    _, fm_text, body = packet.split("---\n", 2)
    meta = yaml.safe_load(fm_text)
    assert meta["title"] == "Weekly decision packet 2026-09-21"
    assert meta["aliases"] == [
        "Decision Packet 2026-09-21",
        "Weekly decision packet 2026-09-21",
    ]
    assert meta["type"] == "wiki"
    assert str(meta["updated"]) == "2026-09-21"
    assert meta["author"] == "planning-loop"
    assert meta["tags"] == ["decision-packet", "founder-cadence", "generated"]
    assert meta["generated_by"] == "planning_loop.run_planning_loop"
    # Body is unchanged: still opens on the H1 the packet always carried.
    assert body.startswith("# Weekly decision packet\n")
    assert "## #90 Pick a pilot" in body


def test_runner_writes_packet_with_frontmatter(monkeypatch, tmp_path):
    written = {}
    monkeypatch.setattr(planning_loop, "_read_tasks", _packet_tasks)
    monkeypatch.setattr(planning_loop, "_release_tags", lambda _repo: [])
    monkeypatch.setattr(planning_loop.config, "vault_root", lambda: tmp_path)
    monkeypatch.setattr(
        "unified_agent.tools.vault.vault_write",
        lambda path, content, **_kw: written.__setitem__(path, content),
    )

    planning_loop.run_planning_loop(generated_at=dt.date(2026, 9, 21), write=True)

    packet = written["Wiki/Decision-Packets/Decision-Packet-2026-09-21.md"]
    assert packet.startswith("---\ntitle: Weekly decision packet 2026-09-21\n")


def test_generated_packet_clears_custodian_missing_aliases(monkeypatch, tmp_path):
    from unified_agent import wiki

    folder = tmp_path / "Wiki" / "Decision-Packets"
    folder.mkdir(parents=True)
    (tmp_path / "00 - Inbox").mkdir()
    (folder / "Decision-Packet-2026-09-21.md").write_text(
        build_decision_packet(_packet_tasks(), packet_date=dt.date(2026, 9, 21)),
        encoding="utf-8",
    )
    monkeypatch.setattr(wiki.config, "vault_root", lambda: tmp_path)

    result = wiki.run_custodian(dry_run=True)

    assert result["wiki_notes_count"] == 1
    assert result["missing_aliases"] == []


def test_prior_state_reads_both_legacy_and_frontmatter_editions(tmp_path):
    folder = tmp_path / "Wiki" / "Decision-Packets"
    folder.mkdir(parents=True)
    # Legacy edition, written before frontmatter existed.
    (folder / "Decision-Packet-2026-09-07.md").write_text(
        "# Weekly decision packet\n\n"
        "## #91 Legacy item\n- Decision: undecided (edition 1 of 2)\n"
        "## #92 Legacy default\n- Defaulted after two consecutive editions: no\n",
        encoding="utf-8",
    )
    # New edition, rendered with frontmatter.
    tasks = _packet_tasks()
    (folder / "Decision-Packet-2026-09-14.md").write_text(
        build_decision_packet(tasks, packet_date=dt.date(2026, 9, 14)),
        encoding="utf-8",
    )

    undecided, defaulted = planning_loop._prior_decision_state(tmp_path, dt.date(2026, 9, 21))

    assert undecided == {90: 1, 91: 1}
    assert defaulted == {92: "no"}


# --- next-work projection (task #1468) --------------------------------------


def _nw_item(task_id, rank, **overrides):
    item = {
        "rank": rank,
        "task_id": task_id,
        "title": f"Task {task_id}",
        "stretch": "A",
        "status_at_review": "open",
        "remaining_action": "do the thing",
        "hard_dependencies": [],
        "preferred_after": [],
        "acceptance_gate": "No special gate.",
        "collision_groups": [f"grp-{task_id}"],
    }
    item.update(overrides)
    return item


def _nw_manifest(items, *, tail=None, mission_task_id=900, as_of="2026-09-27"):
    return {
        "schema_version": 1,
        "as_of": as_of,
        "mission_task_id": mission_task_id,
        "plan_id": "8c4e20274e79",
        "authority": "Live hub tasks/claims/dependencies/policy is authoritative.",
        "items": items,
        "optional_tail": tail or [],
    }


def _nw_task(
    task_id,
    *,
    status="open",
    lane="default",
    claimed_by=None,
    lock_expires_at=None,
    parent_id=None,
    title=None,
    notes=None,
    evidence_ref=None,
):
    return {
        "id": task_id,
        "title": title or f"Task {task_id}",
        "status": status,
        "lane": lane,
        "claimed_by": claimed_by,
        "claimed_at": None,
        "lock_expires_at": lock_expires_at,
        "parent_id": parent_id,
        "notes": notes,
        "evidence_ref": evidence_ref,
    }


_NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.UTC)


def test_next_work_view_excludes_done_and_cancelled_rows():
    manifest = _nw_manifest([_nw_item(101, 1), _nw_item(102, 2)])
    tasks = [_nw_task(101, status="done"), _nw_task(102, status="cancelled")]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["candidates"] == []
    assert set(view["disappeared_task_ids"]) == {101, 102}
    assert all(row["task_id"] not in (101, 102) for row in view["evaluated"])


def test_next_work_view_flags_unmet_hard_dependency_and_shows_blocker():
    manifest = _nw_manifest([_nw_item(201, 1, hard_dependencies=[200]), _nw_item(202, 2)])
    tasks = [_nw_task(200, status="open"), _nw_task(201), _nw_task(202)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [202]
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 201)
    assert blocked["selected"] is False
    assert "#200" in blocked["reason"]
    assert blocked["blockers"] == [200]


def test_next_work_view_cancelled_prerequisite_with_no_marker_fails_closed():
    manifest = _nw_manifest([_nw_item(211, 1, hard_dependencies=[210])])
    tasks = [_nw_task(210, status="cancelled"), _nw_task(211)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["candidates"] == []
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 211)
    assert blocked["blockers"] == [210]
    assert "no `[replaced-by: #N]` marker" in blocked["reason"]
    assert "failing closed" in blocked["reason"]


def test_next_work_view_cancelled_prerequisite_evidence_ref_alone_is_not_replacement():
    # evidence_ref is an optional pointer to a task's own work, not proof a
    # cancelled prerequisite was replaced — task_add allows it before
    # completion (Devin #4135656507). It must not unblock on its own.
    manifest = _nw_manifest([_nw_item(213, 1, hard_dependencies=[212])])
    cancelled_with_evidence = _nw_task(212, status="cancelled", evidence_ref="https://example/pr/1")
    tasks = [cancelled_with_evidence, _nw_task(213)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["candidates"] == []
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 213)
    assert blocked["blockers"] == [212]


def test_next_work_view_cancelled_prerequisite_with_done_replacement_unblocks():
    manifest = _nw_manifest([_nw_item(223, 1, hard_dependencies=[220])])
    cancelled_replaced = _nw_task(220, status="cancelled", notes="[replaced-by: #221]")
    tasks = [cancelled_replaced, _nw_task(221, status="done"), _nw_task(223)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [223]


def test_next_work_view_cancelled_prerequisite_with_undone_replacement_still_blocks():
    manifest = _nw_manifest([_nw_item(233, 1, hard_dependencies=[230])])
    cancelled_replaced = _nw_task(230, status="cancelled", notes="[replaced-by: #231]")
    tasks = [cancelled_replaced, _nw_task(231, status="open"), _nw_task(233)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["candidates"] == []
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 233)
    assert "replaced-by #231 is not done" in blocked["reason"]


def test_next_work_markdown_summarises_eligible_rows_held_back():
    manifest = _nw_manifest([_nw_item(401 + i, i + 1) for i in range(4)])
    tasks = [_nw_task(401 + i) for i in range(4)]

    markdown = render_next_work_markdown(build_next_work_view(manifest, tasks, live_read_at=_NOW))

    assert "### Ineligible or gated" not in markdown
    assert "2 further eligible row(s) held back" in markdown


def test_next_work_view_hides_gated_interactive_and_human_lane_rows():
    manifest = _nw_manifest(
        [
            _nw_item(301, 1, human_gate=True, acceptance_gate="Interactive ruling still required."),
            _nw_item(302, 2, collision_groups=["human-decision"]),
            _nw_item(303, 3),
        ]
    )
    tasks = [
        _nw_task(301),
        _nw_task(302, lane="default"),
        _nw_task(303),
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [303]
    gated_301 = next(r for r in view["evaluated"] if r["task_id"] == 301)
    gated_302 = next(r for r in view["evaluated"] if r["task_id"] == 302)
    assert "human review" in gated_301["reason"].lower()
    assert "human-decision" in gated_302["reason"]


def test_next_work_gate_ignores_negated_interactive_prose():
    manifest = _nw_manifest([_nw_item(304, 1, acceptance_gate="Non-interactive test run")])
    view = build_next_work_view(manifest, [_nw_task(304)], live_read_at=_NOW)
    assert [row["task_id"] for row in view["candidates"]] == [304]


def test_next_work_uses_live_title_and_reports_manifest_drift():
    manifest = _nw_manifest([_nw_item(305, 1, title="Old title")])
    task = _nw_task(305)
    task["title"] = "Current title"
    view = build_next_work_view(manifest, [task], live_read_at=_NOW)
    assert view["candidates"][0]["title"] == "Current title"
    assert view["candidates"][0]["manifest_title"] == "Old title"
    assert any("manifest title differs" in warning for warning in view["drift_warnings"])


def test_next_work_claim_is_available_at_its_expiry_second():
    expiry = _NOW.replace(microsecond=0)
    task = _nw_task(306, claimed_by="Desktop_Agent1", lock_expires_at=expiry.isoformat())
    assert planning_loop._next_work_in_flight(task, expiry - dt.timedelta(seconds=1))
    assert not planning_loop._next_work_in_flight(task, expiry)
    assert not planning_loop._next_work_in_flight(task, expiry + dt.timedelta(milliseconds=500))
    assert not planning_loop._next_work_in_flight(task, expiry + dt.timedelta(seconds=1))


@pytest.mark.parametrize("lane", ["operator-only", config.LEGACY_OPERATOR_LANE])
def test_next_work_view_human_only_lane_is_gated(lane):
    manifest = _nw_manifest([_nw_item(310, 1), _nw_item(311, 2)])
    tasks = [_nw_task(310, lane=lane), _nw_task(311)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [311]
    gated = next(r for r in view["evaluated"] if r["task_id"] == 310)
    assert "human/interactive decision" in gated["reason"]


def test_next_work_view_hides_in_flight_claims_and_occupies_collision_group():
    manifest = _nw_manifest(
        [
            _nw_item(401, 1, collision_groups=["shared"]),
            _nw_item(402, 2, collision_groups=["shared"]),
            _nw_item(403, 3, collision_groups=["other"]),
        ]
    )
    tasks = [
        _nw_task(
            401,
            status="doing",
            claimed_by="Desktop_Agent2",
            lock_expires_at="2026-09-30T00:00:00+00:00",
        ),
        _nw_task(402),
        _nw_task(403),
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["in_flight"] and view["in_flight"][0]["task_id"] == 401
    assert view["in_flight"][0]["claimed_by"] == "Desktop_Agent2"
    # 402 shares the in-flight collision group and must not be suggested.
    assert [c["task_id"] for c in view["candidates"]] == [403]
    skipped_402 = next(r for r in view["evaluated"] if r["task_id"] == 402)
    assert "shared" in skipped_402["reason"]


def test_next_work_view_expired_lock_is_not_in_flight():
    manifest = _nw_manifest([_nw_item(410, 1)])
    tasks = [
        _nw_task(
            410,
            status="doing",
            claimed_by="server_agent3",
            lock_expires_at="2020-01-01T00:00:00+00:00",
        )
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["in_flight"] == []
    assert [c["task_id"] for c in view["candidates"]] == [410]


def test_next_work_view_selects_top_two_eligible_non_conflicting():
    manifest = _nw_manifest(
        [
            _nw_item(501, 1, collision_groups=["grp-x"]),
            _nw_item(502, 2, collision_groups=["grp-x"]),
            _nw_item(503, 3, collision_groups=["grp-y"]),
        ]
    )
    tasks = [_nw_task(501), _nw_task(502), _nw_task(503)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [501, 503]
    skipped_502 = next(r for r in view["evaluated"] if r["task_id"] == 502)
    assert skipped_502["eligible"] is True
    assert skipped_502["selected"] is False
    assert "grp-x" in skipped_502["reason"]


def test_next_work_view_stalled_top_candidate_yields_alternative_with_reason():
    manifest = _nw_manifest(
        [
            _nw_item(601, 1, hard_dependencies=[600]),
            _nw_item(602, 2),
            _nw_item(603, 3),
        ]
    )
    tasks = [_nw_task(600, status="open"), _nw_task(601), _nw_task(602), _nw_task(603)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [602, 603]
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 601)
    assert blocked["reason"].startswith("blocked on unmet hard dependency")


def test_next_work_view_merges_live_dependency_graph_with_manifest_hard_dependencies():
    manifest = _nw_manifest([_nw_item(701, 1, hard_dependencies=[])])
    tasks = [_nw_task(700, status="open"), _nw_task(701)]
    dependencies = {701: [700]}

    view = build_next_work_view(manifest, tasks, dependencies, live_read_at=_NOW)

    assert view["candidates"] == []
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 701)
    assert blocked["blockers"] == [700]


def test_next_work_view_excludes_tracking_lane_rows():
    manifest = _nw_manifest([_nw_item(801, 1), _nw_item(802, 2)])
    tasks = [_nw_task(801, lane="tracking"), _nw_task(802)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [802]
    assert all(row["task_id"] != 801 for row in view["evaluated"])


def test_next_work_view_unknown_manifest_row_flagged_as_drift():
    manifest = _nw_manifest([_nw_item(901, 1), _nw_item(902, 2)])
    tasks = [_nw_task(902)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["unknown_to_hub_task_ids"] == [901]
    assert any("#901" in warning for warning in view["drift_warnings"])
    assert [c["task_id"] for c in view["candidates"]] == [902]


def test_next_work_view_status_and_gate_lane_drift():
    manifest = _nw_manifest(
        [
            _nw_item(1001, 1, status_at_review="open"),
            _nw_item(1002, 2, collision_groups=["human-decision"]),
        ]
    )
    tasks = [
        _nw_task(1001, status="waiting_review"),
        _nw_task(1002, lane="default"),
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert any(
        "status_at_review='open'" in w and "live status='waiting_review'" in w
        for w in view["drift_warnings"]
    )
    assert any(
        "human-decision gate but live lane is 'default'" in w for w in view["drift_warnings"]
    )


def test_next_work_view_mission_gap_lists_open_tasks_missing_from_manifest():
    manifest = _nw_manifest([_nw_item(1101, 1)], mission_task_id=1100)
    tasks = [
        _nw_task(1100, status="open", parent_id=None, title="Mission root"),
        _nw_task(1101, parent_id=1100),
        _nw_task(1102, status="open", parent_id=1100, title="Untracked child"),
        _nw_task(1103, status="done", parent_id=1100, title="Finished, no gap"),
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["mission_gap_task_ids"] == [1102]
    assert any("#1102" in w for w in view["drift_warnings"])


def test_next_work_view_tail_candidate_used_when_ranked_rows_exhausted():
    manifest = _nw_manifest(
        [_nw_item(1201, 1, status_at_review="done")],
        tail=[
            {"task_id": 1301, "title": "Optional follow-on", "hard_dependencies": []},
        ],
    )
    tasks = [_nw_task(1201, status="done"), _nw_task(1301)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [1301]
    assert view["candidates"][0]["tail"] is True


def test_next_work_view_rejects_unsupported_schema_version():
    manifest = _nw_manifest([_nw_item(1, 1)])
    manifest["schema_version"] = 2

    with pytest.raises(planning_loop.ToolError, match="schema_version"):
        build_next_work_view(manifest, [_nw_task(1)], live_read_at=_NOW)


def test_render_next_work_markdown_includes_candidates_in_flight_and_drift():
    manifest = _nw_manifest(
        [
            _nw_item(1401, 1, collision_groups=["grp-a"]),
            _nw_item(1402, 2, collision_groups=["grp-b"]),
            _nw_item(1403, 3, hard_dependencies=[9999]),
        ]
    )
    tasks = [
        _nw_task(1401),
        _nw_task(
            1402,
            status="doing",
            claimed_by="Desktop_Agent1",
            lock_expires_at="2026-12-01T00:00:00+00:00",
        ),
        _nw_task(1403),
    ]

    view = build_next_work_view(manifest, tasks, manifest_revision="deadbeef", live_read_at=_NOW)
    rendered = render_next_work_markdown(view)

    assert "## Current next-work view" in rendered
    assert "#1401" in rendered
    assert "deadbeef" in rendered
    assert "Desktop_Agent1" in rendered
    assert "#1403" in rendered
    assert "blocked on unmet hard dependency" in rendered


def test_next_work_manifest_path_defaults_to_process_workspace_root(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))

    path = planning_loop._next_work_manifest_path()

    assert path == tmp_path / "docs" / "queue" / "NEXT-WORK.json"


def test_next_work_manifest_path_accepts_injected_workspace_root(tmp_path):
    path = planning_loop._next_work_manifest_path(tmp_path)

    assert path == tmp_path / "docs" / "queue" / "NEXT-WORK.json"


def test_next_work_manifest_revision_is_none_outside_a_git_repo(tmp_path):
    assert planning_loop._next_work_manifest_revision(tmp_path / "NEXT-WORK.json") is None


def _patch_next_work_live_snapshot(
    monkeypatch, tasks, dependencies=None, archived=None, queued_routine=None
):
    monkeypatch.setattr(
        planning_loop,
        "_read_next_work_live_snapshot",
        lambda: (tasks, dependencies or {}, archived or {}, queued_routine or set()),
    )
    monkeypatch.setattr(planning_loop.storage, "require_live_task_store", lambda: None)


def test_remote_client_uses_hub_projection_and_keeps_local_manifest_metadata(monkeypatch, tmp_path):
    manifest_path = tmp_path / "NEXT-WORK.json"
    manifest_path.write_text(json.dumps(_nw_manifest([_nw_item(307, 1)])), encoding="utf-8")

    def retired_store():
        raise planning_loop.storage.LiveStoreError("retired")

    monkeypatch.setattr(planning_loop.storage, "require_live_task_store", retired_store)
    expected = build_next_work_view(
        _nw_manifest([_nw_item(307, 1)]), [_nw_task(307)], live_read_at=_NOW
    )
    monkeypatch.setattr(planning_loop, "_read_remote_next_work_view", lambda manifest: expected)
    monkeypatch.setattr(
        planning_loop,
        "_read_next_work_live_snapshot",
        lambda: pytest.fail("retired local store must not be opened"),
    )
    view = planning_loop.run_next_work_view(manifest_path=manifest_path)
    assert [row["task_id"] for row in view["candidates"]] == [307]
    assert view["live_read_at"] == _NOW.isoformat()
    assert view["manifest_content_hash"] == planning_loop._next_work_manifest_content_hash(
        manifest_path.read_text()
    )
    assert view["manifest_path"] == str(manifest_path)


def test_remote_projection_reader_uses_authenticated_hub_client(monkeypatch):
    from unified_agent import device_login, hub_client

    class FakeHubClient:
        def __init__(self, *, agent_label, hub_url, allow_fallback):
            assert (agent_label, hub_url, allow_fallback) == (
                "Desktop_Agent2",
                "http://hub.example/mcp",
                False,
            )

    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://hub.example/mcp")
    monkeypatch.setattr(planning_loop.config, "executor_agent", lambda: "")
    monkeypatch.setattr(device_login, "saved_signin_agent", lambda hub: "Desktop_Agent2")
    monkeypatch.setattr(hub_client, "HubClient", FakeHubClient)

    manifest = _nw_manifest([_nw_item(307, 1)])

    def projection(client, path, body):
        assert isinstance(client, FakeHubClient)
        assert path == "/api/tasks/next-work-projection"
        assert body == manifest
        return {"candidates": [{"task_id": 307}], "evaluated": []}

    monkeypatch.setattr(hub_client, "_post_json", projection)
    view = planning_loop._read_remote_next_work_view(manifest)
    assert view["candidates"] == [{"task_id": 307}]


def test_remote_projection_requires_explicit_hub_url(monkeypatch):
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    with pytest.raises(planning_loop.ToolError, match="UA_HTTP_HUB_URL is required"):
        planning_loop._read_remote_next_work_view(_nw_manifest([_nw_item(307, 1)]))


def test_saved_signin_agent_is_bound_to_hub(tmp_path):
    from unified_agent import device_login

    path = tmp_path / "signin.json"
    path.write_text(
        json.dumps({"hub": "https://hub.example", "agent": "Desktop_Agent2"}),
        encoding="utf-8",
    )
    assert device_login.saved_signin_agent("https://hub.example/mcp", path) == "Desktop_Agent2"
    assert device_login.saved_signin_agent("https://other.example/mcp", path) is None


def test_hub_projection_handles_large_dependency_graph_without_returning_notes(monkeypatch):
    live = _nw_task(307)
    live["title"] = "Cross-project private task title"
    live["notes"] = "private task details"
    ids = list(range(1000, 2200))
    statuses = {task_id: {"status": "done", "parent_id": None} for task_id in ids}
    statuses[307] = live
    unrelated = [_nw_task(task_id) for task_id in range(3000, 7000)]
    unrelated[0]["notes"] = "cross-project secret"
    monkeypatch.setattr(
        planning_loop,
        "_read_next_work_live_snapshot",
        lambda: ([live, *unrelated], {307: ids}, statuses, set()),
    )
    manifest = _nw_manifest([_nw_item(307, 1, hard_dependencies=ids[:350])])
    view = planning_loop.next_work_projection_payload(manifest)
    assert [row["task_id"] for row in view["candidates"]] == [307]
    assert view["candidates"][0]["title"] == "Task 307"
    assert "Cross-project private task title" not in json.dumps(view)
    assert any("manifest title differs" in warning for warning in view["drift_warnings"])
    assert "private task details" not in json.dumps(view)
    assert "cross-project secret" not in json.dumps(view)
    assert len(json.dumps(view).encode("utf-8")) < 2 * 1024 * 1024


def test_hub_projection_rejects_malformed_manifest_without_reading_tasks(monkeypatch):
    monkeypatch.setattr(
        planning_loop,
        "_read_next_work_live_snapshot",
        lambda: pytest.fail("invalid manifest must not read task store"),
    )
    with pytest.raises(ValueError, match="positive integers"):
        planning_loop.next_work_projection_payload(
            _nw_manifest([_nw_item(307, 1, hard_dependencies=[-1])])
        )


def test_run_next_work_view_reads_manifest_tasks_and_dependencies(monkeypatch, tmp_path):
    manifest_dir = tmp_path / "docs" / "queue"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "NEXT-WORK.json"
    manifest_path.write_text(json.dumps(_nw_manifest([_nw_item(1501, 1)])), encoding="utf-8")
    _patch_next_work_live_snapshot(monkeypatch, [_nw_task(1501)])
    monkeypatch.setattr(planning_loop, "_next_work_manifest_revision", lambda path: "abc123")

    view = planning_loop.run_next_work_view(workspace_root=tmp_path)

    assert view["manifest_path"] == str(manifest_path)
    assert view["manifest_revision"] == "abc123"
    assert [c["task_id"] for c in view["candidates"]] == [1501]


def test_run_next_work_view_manifest_path_override_wins_over_workspace_root(monkeypatch, tmp_path):
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps(_nw_manifest([_nw_item(1601, 1)])), encoding="utf-8")
    _patch_next_work_live_snapshot(monkeypatch, [_nw_task(1601)])

    view = planning_loop.run_next_work_view(manifest_path=other, workspace_root=tmp_path)

    assert view["manifest_path"] == str(other)


def test_run_next_work_view_missing_manifest_raises_tool_error(monkeypatch, tmp_path):
    _patch_next_work_live_snapshot(monkeypatch, [])

    with pytest.raises(planning_loop.ToolError, match="cannot read next-work manifest"):
        planning_loop.run_next_work_view(workspace_root=tmp_path)


def test_run_next_work_view_missing_manifest_error_names_ua_workspace(monkeypatch, tmp_path):
    _patch_next_work_live_snapshot(monkeypatch, [])

    with pytest.raises(planning_loop.ToolError, match="UA_WORKSPACE"):
        planning_loop.run_next_work_view(workspace_root=tmp_path)


# --- PR #877 review fixes ----------------------------------------------------


def test_next_work_view_blocked_and_waiting_review_are_ineligible_before_dependency_checks():
    manifest = _nw_manifest(
        [
            _nw_item(1701, 1, collision_groups=["a"]),
            _nw_item(1702, 2, collision_groups=["b"]),
            _nw_item(1703, 3, collision_groups=["c"]),
        ]
    )
    tasks = [
        _nw_task(1701, status="blocked"),
        _nw_task(1702, status="waiting_review"),
        _nw_task(1703, status="open"),
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [1703]
    blocked_row = next(r for r in view["evaluated"] if r["task_id"] == 1701)
    waiting_row = next(r for r in view["evaluated"] if r["task_id"] == 1702)
    assert blocked_row["reason"] == "status is 'blocked', not open"
    assert blocked_row["eligible"] is False
    assert waiting_row["reason"] == "status is 'waiting_review', not open"
    assert waiting_row["eligible"] is False


def test_next_work_view_blocked_status_wins_over_dependency_and_collision_reasons():
    # A blocked row also has an unmet dependency and a colliding group; the
    # reported reason must be the status gate, since that's checked first.
    manifest = _nw_manifest(
        [
            _nw_item(1710, 1, collision_groups=["shared"]),
            _nw_item(1711, 2, hard_dependencies=[9999], collision_groups=["shared"]),
        ]
    )
    tasks = [_nw_task(1710), _nw_task(1711, status="blocked")]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    row = next(r for r in view["evaluated"] if r["task_id"] == 1711)
    assert row["reason"] == "status is 'blocked', not open"


def test_next_work_view_doing_with_expired_lease_is_not_blocked_by_status():
    # `doing` is not in the not-ready set: once its lease expires it is
    # reclaimable, so it falls through to the ordinary eligibility checks
    # rather than being denied by status alone.
    manifest = _nw_manifest([_nw_item(1720, 1)])
    tasks = [
        _nw_task(
            1720,
            status="doing",
            claimed_by="server_agent3",
            lock_expires_at="2020-01-01T00:00:00+00:00",
        )
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["in_flight"] == []
    assert [c["task_id"] for c in view["candidates"]] == [1720]


def test_next_work_view_archived_prerequisite_status_unblocks_successor():
    # #1810 is done but archived, so it is absent from the live `tasks`
    # snapshot; its status must still be resolved from the archived lookup.
    manifest = _nw_manifest([_nw_item(1811, 1, hard_dependencies=[1810])])
    tasks = [_nw_task(1811)]
    archived = {1810: {"status": "done", "evidence_ref": None}}

    view = build_next_work_view(manifest, tasks, archived_task_statuses=archived, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [1811]


def test_next_work_view_truly_absent_prerequisite_still_fails_closed():
    # Not live, not in the archived lookup either: still blocks (fail closed).
    manifest = _nw_manifest([_nw_item(1821, 1, hard_dependencies=[1820])])
    tasks = [_nw_task(1821)]

    view = build_next_work_view(manifest, tasks, archived_task_statuses={}, live_read_at=_NOW)

    assert view["candidates"] == []
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 1821)
    assert blocked["blockers"] == [1820]


def test_next_work_view_archived_prerequisite_not_done_still_blocks():
    manifest = _nw_manifest([_nw_item(1831, 1, hard_dependencies=[1830])])
    tasks = [_nw_task(1831)]
    archived = {1830: {"status": "blocked", "evidence_ref": None}}

    view = build_next_work_view(manifest, tasks, archived_task_statuses=archived, live_read_at=_NOW)

    assert view["candidates"] == []


def test_next_work_view_expiry_compared_as_aware_datetimes_not_strings():
    # Stored lease has second precision ("...:00+00:00"); "now" has
    # microsecond precision half a second later. task_claim compares at
    # whole-second precision and allows takeover at the exact expiry second.
    manifest = _nw_manifest([_nw_item(1901, 1)])
    tasks = [
        _nw_task(
            1901,
            status="doing",
            claimed_by="Desktop_Agent2",
            lock_expires_at="2026-09-29T12:00:00+00:00",
        )
    ]
    now = dt.datetime(2026, 9, 29, 12, 0, 0, 500000, tzinfo=dt.UTC)

    view = build_next_work_view(manifest, tasks, live_read_at=now)

    assert view["in_flight"] == []
    assert [r["task_id"] for r in view["candidates"]] == [1901]


def test_next_work_view_expiry_naive_timestamp_treated_as_utc():
    manifest = _nw_manifest([_nw_item(1902, 1, collision_groups=["g"])])
    tasks = [
        _nw_task(
            1902,
            status="doing",
            claimed_by="Desktop_Agent2",
            lock_expires_at="2026-12-01T00:00:00",  # naive, no offset
        )
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["in_flight"] and view["in_flight"][0]["task_id"] == 1902


def test_next_work_parse_timestamp_treats_naive_as_utc():
    parsed = planning_loop._next_work_parse_timestamp("2026-01-01T00:00:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == dt.timedelta(0)


def test_next_work_view_claimed_open_task_is_in_flight_regardless_of_status():
    # An agent can move its own claimed task back to `open` without
    # release_claim=True; task_claim still rejects a new claimant while the
    # lease is valid, so this must read as in-flight, not free.
    manifest = _nw_manifest(
        [
            _nw_item(2001, 1, collision_groups=["shared"]),
            _nw_item(2002, 2, collision_groups=["shared"]),
        ]
    )
    tasks = [
        _nw_task(
            2001,
            status="open",
            claimed_by="Desktop_Agent2",
            lock_expires_at="2026-12-01T00:00:00+00:00",
        ),
        _nw_task(2002),
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["in_flight"] and view["in_flight"][0]["task_id"] == 2001
    assert view["candidates"] == []
    row_2002 = next(r for r in view["evaluated"] if r["task_id"] == 2002)
    assert "shared" in row_2002["reason"]


def _init_next_work_git_repo(tmp_path):
    def run(*args):
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            },
        )

    manifest_dir = tmp_path / "docs" / "queue"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "NEXT-WORK.json"
    manifest_path.write_text(json.dumps(_nw_manifest([_nw_item(1, 1)])), encoding="utf-8")
    run("init", "-q")
    run("add", "docs/queue/NEXT-WORK.json")
    run("commit", "-q", "-m", "add manifest")
    revision = subprocess.run(
        ["git", "log", "-1", "--format=%H"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return manifest_path, revision


def test_next_work_manifest_revision_clean_tree_has_no_dirty_suffix(tmp_path):
    manifest_path, revision = _init_next_work_git_repo(tmp_path)

    assert planning_loop._next_work_manifest_revision(manifest_path) == revision


def test_next_work_manifest_revision_flags_uncommitted_edits_as_dirty(tmp_path):
    manifest_path, revision = _init_next_work_git_repo(tmp_path)
    manifest_path.write_text(
        json.dumps(_nw_manifest([_nw_item(1, 1), _nw_item(2, 2)])), encoding="utf-8"
    )

    assert planning_loop._next_work_manifest_revision(manifest_path) == f"{revision}+dirty"


def test_next_work_manifest_revision_of_untracked_override_is_none(tmp_path):
    # An override manifest that exists but has no git history at all (never
    # committed) must not silently report the workspace's own revision
    # (Devin #4135657158).
    _repo_manifest_path, _revision = _init_next_work_git_repo(tmp_path)
    other = tmp_path / "untracked.json"
    other.write_text(json.dumps(_nw_manifest([_nw_item(1, 1)])), encoding="utf-8")

    assert planning_loop._next_work_manifest_revision(other) is None


def test_next_work_manifest_revision_outside_any_repository_is_none(tmp_path):
    other_root = tmp_path / "no-repo-here"
    other_root.mkdir()
    manifest_path = other_root / "NEXT-WORK.json"
    manifest_path.write_text(json.dumps(_nw_manifest([_nw_item(1, 1)])), encoding="utf-8")

    assert planning_loop._next_work_manifest_revision(manifest_path) is None


def test_next_work_manifest_content_hash_matches_sha256_of_bytes_read():
    import hashlib

    raw = json.dumps(_nw_manifest([_nw_item(1, 1)]))

    assert (
        planning_loop._next_work_manifest_content_hash(raw)
        == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    )


def test_run_next_work_view_reports_dirty_revision_and_content_hash(monkeypatch, tmp_path):
    manifest_path, revision = _init_next_work_git_repo(tmp_path)
    dirty_manifest = _nw_manifest([_nw_item(1, 1), _nw_item(1701, 2)])
    manifest_path.write_text(json.dumps(dirty_manifest), encoding="utf-8")
    _patch_next_work_live_snapshot(monkeypatch, [_nw_task(1), _nw_task(1701)])

    view = planning_loop.run_next_work_view(workspace_root=tmp_path)

    assert view["manifest_revision"] == f"{revision}+dirty"
    expected_hash = planning_loop._next_work_manifest_content_hash(
        manifest_path.read_text(encoding="utf-8")
    )
    assert view["manifest_content_hash"] == expected_hash


def test_next_work_view_selection_follows_rank_not_array_order():
    # Array order is deliberately reversed relative to rank.
    manifest = _nw_manifest(
        [
            _nw_item(2101, 2, collision_groups=["a"]),
            _nw_item(2102, 1, collision_groups=["b"]),
        ]
    )
    tasks = [_nw_task(2101), _nw_task(2102)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [2102, 2101]
    assert any("array order disagrees with rank order" in w for w in view["drift_warnings"])


def test_next_work_view_no_rank_drift_warning_when_array_matches_rank():
    manifest = _nw_manifest([_nw_item(2201, 1), _nw_item(2202, 2)])
    tasks = [_nw_task(2201), _nw_task(2202)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert not any("array order disagrees with rank order" in w for w in view["drift_warnings"])


def test_next_work_view_tail_rows_keep_given_order_after_ranked_rows():
    manifest = _nw_manifest(
        [_nw_item(2301, 1, status_at_review="done")],
        tail=[
            {
                "task_id": 2401,
                "title": "Tail A",
                "hard_dependencies": [],
                "collision_groups": ["ta"],
            },
            {
                "task_id": 2402,
                "title": "Tail B",
                "hard_dependencies": [],
                "collision_groups": ["tb"],
            },
        ],
    )
    tasks = [_nw_task(2301, status="done"), _nw_task(2401), _nw_task(2402)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [2401, 2402]


def test_next_work_manifest_path_docstring_and_cli_help_name_ua_workspace():
    assert "UA_WORKSPACE" in (planning_loop._next_work_manifest_path.__doc__ or "")


def test_cli_reference_documents_next_work_cockpit_workspace_requirement():
    doc = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    assert "UA_WORKSPACE" in doc
    assert "--next-work" in doc


# --- PR #877 second review round ---------------------------------------------


def test_next_work_view_cancelled_ancestor_blocks_an_open_child():
    # task_claim's _assert_no_cancelled_ancestor rejects any claim under a
    # cancelled parent; a candidate the view suggests must actually be
    # claimable (Devin #4135656706).
    manifest = _nw_manifest([_nw_item(901, 1)], mission_task_id=900)
    tasks = [
        _nw_task(900, status="cancelled", title="Mission"),
        _nw_task(901, parent_id=900),
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["candidates"] == []
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 901)
    assert "cancelled ancestor #900" in blocked["reason"]


def test_next_work_view_cancelled_grandparent_blocks_a_deeper_child():
    manifest = _nw_manifest([_nw_item(1002, 1)])
    tasks = [
        _nw_task(1000, status="cancelled"),
        _nw_task(1001, parent_id=1000),
        _nw_task(1002, parent_id=1001),
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert view["candidates"] == []
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 1002)
    assert "cancelled ancestor #1000" in blocked["reason"]


def test_next_work_view_open_ancestor_does_not_block():
    manifest = _nw_manifest([_nw_item(1102, 1)])
    tasks = [
        _nw_task(1100, status="open"),
        _nw_task(1101, parent_id=1100),
        _nw_task(1102, parent_id=1101),
    ]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [1102]


def test_next_work_view_archived_cancelled_ancestor_still_blocks():
    # The ancestor chain must also see archived ancestors, not just live ones
    # (Devin #4135656706's "include ancestor statuses from archived rows").
    manifest = _nw_manifest([_nw_item(1202, 1)])
    tasks = [_nw_task(1201, parent_id=1200), _nw_task(1202, parent_id=1201)]
    archived = {
        1200: {"status": "cancelled", "evidence_ref": None, "notes": None, "parent_id": None}
    }

    view = build_next_work_view(manifest, tasks, archived_task_statuses=archived, live_read_at=_NOW)

    assert view["candidates"] == []
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 1202)
    assert "cancelled ancestor #1200" in blocked["reason"]


def test_next_work_view_archived_completed_manifest_task_is_disappeared_not_unknown():
    # A manifest row that was completed and then archived must read as
    # "disappeared" (it shipped), not "unknown to the hub" (Devin
    # #4135656891).
    manifest = _nw_manifest([_nw_item(1301, 1), _nw_item(1302, 2)])
    tasks = [_nw_task(1302)]
    archived = {1301: {"status": "done", "evidence_ref": None, "notes": None, "parent_id": None}}

    view = build_next_work_view(manifest, tasks, archived_task_statuses=archived, live_read_at=_NOW)

    assert 1301 in view["disappeared_task_ids"]
    assert 1301 not in view["unknown_to_hub_task_ids"]
    assert all(row["task_id"] != 1301 for row in view["evaluated"])
    assert not any("#1301" in w for w in view["drift_warnings"])


def test_next_work_view_archived_cancelled_manifest_task_is_also_disappeared():
    manifest = _nw_manifest([_nw_item(1311, 1)])
    tasks: list[dict] = []
    archived = {
        1311: {"status": "cancelled", "evidence_ref": None, "notes": None, "parent_id": None}
    }

    view = build_next_work_view(manifest, tasks, archived_task_statuses=archived, live_read_at=_NOW)

    assert view["disappeared_task_ids"] == [1311]
    assert view["unknown_to_hub_task_ids"] == []


def test_next_work_view_archived_nonterminal_status_is_flagged_not_disappeared():
    # An archived row with a non-terminal status is unusual data; it must
    # neither silently disappear nor be indistinguishable from a genuinely
    # nonexistent id.
    manifest = _nw_manifest([_nw_item(1321, 1)])
    tasks: list[dict] = []
    archived = {1321: {"status": "open", "evidence_ref": None, "notes": None, "parent_id": None}}

    view = build_next_work_view(manifest, tasks, archived_task_statuses=archived, live_read_at=_NOW)

    assert view["unknown_to_hub_task_ids"] == [1321]
    assert view["disappeared_task_ids"] == []
    row = next(r for r in view["evaluated"] if r["task_id"] == 1321)
    assert "archived" in row["reason"]


def test_next_work_view_genuinely_unknown_id_is_still_unknown():
    manifest = _nw_manifest([_nw_item(1401, 1)])
    tasks: list[dict] = []

    view = build_next_work_view(manifest, tasks, archived_task_statuses={}, live_read_at=_NOW)

    assert view["unknown_to_hub_task_ids"] == [1401]
    row = next(r for r in view["evaluated"] if r["task_id"] == 1401)
    assert row["reason"] == "unknown to the hub"


def test_cli_next_work_format_json_prints_only_json(monkeypatch, capsys):
    fake_view = {"candidates": [], "in_flight": [], "evaluated": [], "drift_warnings": []}
    monkeypatch.setattr(planning_loop, "run_next_work_view", lambda manifest_path=None: fake_view)
    monkeypatch.setattr(
        "sys.argv", ["unified-agent-planning-loop", "--next-work", "--format", "json"]
    )

    planning_loop.main()

    out = capsys.readouterr().out
    assert json.loads(out) == fake_view
    assert "Current next-work view" not in out


def test_cli_next_work_format_markdown_prints_only_markdown(monkeypatch, capsys):
    fake_view = {
        "candidates": [],
        "in_flight": [],
        "evaluated": [],
        "drift_warnings": [],
        "mission_task_id": None,
        "manifest_as_of": None,
        "manifest_revision": None,
        "manifest_content_hash": None,
        "live_read_at": "2026-09-29T00:00:00+00:00",
        "staleness_days": None,
        "disappeared_task_ids": [],
    }
    monkeypatch.setattr(planning_loop, "run_next_work_view", lambda manifest_path=None: fake_view)
    monkeypatch.setattr(
        "sys.argv", ["unified-agent-planning-loop", "--next-work", "--format", "markdown"]
    )

    planning_loop.main()

    out = capsys.readouterr().out
    assert "## Current next-work view" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_next_work_format_both_keeps_original_combined_output(monkeypatch, capsys):
    fake_view = {
        "candidates": [],
        "in_flight": [],
        "evaluated": [],
        "drift_warnings": [],
        "mission_task_id": None,
        "manifest_as_of": None,
        "manifest_revision": None,
        "manifest_content_hash": None,
        "live_read_at": "2026-09-29T00:00:00+00:00",
        "staleness_days": None,
        "disappeared_task_ids": [],
    }
    monkeypatch.setattr(planning_loop, "run_next_work_view", lambda manifest_path=None: fake_view)
    monkeypatch.setattr("sys.argv", ["unified-agent-planning-loop", "--next-work"])

    planning_loop.main()

    out = capsys.readouterr().out
    json_part, _, markdown_part = out.partition("\n\n")
    assert json.loads(json_part) == fake_view
    assert "## Current next-work view" in markdown_part


# --- PR #877 round 4 ---------------------------------------------------------


def test_next_work_view_mission_gap_bridges_an_archived_parent():
    # Mission #900 -> archived child #901 -> live open grandchild #902.
    # #901 being archived (absent from `tasks`) must not break the chain.
    manifest = _nw_manifest([], mission_task_id=900)
    tasks = [_nw_task(900), _nw_task(902, parent_id=901)]
    archived = {901: {"status": "done", "evidence_ref": None, "notes": None, "parent_id": 900}}

    view = build_next_work_view(manifest, tasks, archived_task_statuses=archived, live_read_at=_NOW)

    assert view["mission_gap_task_ids"] == [902]
    assert any("#902" in w for w in view["drift_warnings"])


def test_next_work_view_mission_gap_never_reports_the_archived_node_itself():
    manifest = _nw_manifest([], mission_task_id=900)
    tasks = [_nw_task(900)]
    archived = {901: {"status": "done", "evidence_ref": None, "notes": None, "parent_id": 900}}

    view = build_next_work_view(manifest, tasks, archived_task_statuses=archived, live_read_at=_NOW)

    assert 901 not in view["mission_gap_task_ids"]
    assert view["mission_gap_task_ids"] == []


def test_next_work_view_duplicate_manifest_row_does_not_consume_two_slots():
    manifest = _nw_manifest(
        [
            _nw_item(10, 1, collision_groups=[]),
            _nw_item(10, 2, collision_groups=[]),
            _nw_item(11, 3, collision_groups=[]),
        ]
    )
    tasks = [_nw_task(10), _nw_task(11)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [10, 11]
    assert any("duplicate task id" in w and "#10" in w for w in view["drift_warnings"])


def test_next_work_view_duplicate_across_ranked_and_tail_keeps_ranked_first():
    manifest = _nw_manifest(
        [_nw_item(20, 1, collision_groups=[])],
        tail=[{"task_id": 20, "title": "Tail dupe", "hard_dependencies": []}],
    )
    tasks = [_nw_task(20)]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert len(view["candidates"]) == 1
    assert view["candidates"][0]["tail"] is False


def test_next_work_view_queued_routine_task_is_ineligible():
    manifest = _nw_manifest([_nw_item(30, 1), _nw_item(31, 2)])
    tasks = [_nw_task(30), _nw_task(31)]

    view = build_next_work_view(manifest, tasks, queued_routine_task_ids={30}, live_read_at=_NOW)

    assert [c["task_id"] for c in view["candidates"]] == [31]
    blocked = next(r for r in view["evaluated"] if r["task_id"] == 30)
    assert blocked["reason"] == "queued routine work; waiting for scheduler release"


def test_next_work_view_queued_routine_check_precedes_dependency_and_collision():
    # A queued routine task that also has an unmet dependency and a
    # colliding group must still report the queued-routine reason first.
    manifest = _nw_manifest(
        [
            _nw_item(40, 1, hard_dependencies=[9999], collision_groups=["shared"]),
            _nw_item(41, 2, collision_groups=["shared"]),
        ]
    )
    tasks = [_nw_task(40), _nw_task(41)]

    view = build_next_work_view(manifest, tasks, queued_routine_task_ids={40}, live_read_at=_NOW)

    row = next(r for r in view["evaluated"] if r["task_id"] == 40)
    assert row["reason"] == "queued routine work; waiting for scheduler release"


def test_next_work_view_tail_row_status_drift_is_reported():
    manifest = _nw_manifest(
        [_nw_item(50, 1, status_at_review="done")],
        tail=[
            {"task_id": 60, "title": "Tail", "hard_dependencies": [], "status_at_review": "open"}
        ],
    )
    tasks = [_nw_task(50, status="done"), _nw_task(60, status="blocked")]

    view = build_next_work_view(manifest, tasks, live_read_at=_NOW)

    assert any(
        "#60" in w and "status_at_review='open'" in w and "live status='blocked'" in w
        for w in view["drift_warnings"]
    )


def test_read_queued_routine_task_ids_mirrors_task_claim_query(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE routine_runs (id INTEGER PRIMARY KEY, routine_id INTEGER, "
            "fire_key TEXT, scheduled_for TEXT, status TEXT, task_id INTEGER, created_at TEXT)"
        )
        conn.execute("INSERT INTO routine_runs VALUES (1, 1, 'k1', 't', 'queued', 501, 'now')")
        conn.execute("INSERT INTO routine_runs VALUES (2, 1, 'k2', 't', 'created', 502, 'now')")
        conn.execute("INSERT INTO routine_runs VALUES (3, 1, 'k3', 't', 'queued', NULL, 'now')")
    monkeypatch.setattr(planning_loop.config, "db_path", lambda: db)

    assert planning_loop._read_queued_routine_task_ids() == {501}


def test_run_next_work_view_reads_queued_routine_ids_from_snapshot(monkeypatch, tmp_path):
    manifest_dir = tmp_path / "docs" / "queue"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "NEXT-WORK.json"
    manifest_path.write_text(
        json.dumps(_nw_manifest([_nw_item(70, 1), _nw_item(71, 2)])), encoding="utf-8"
    )
    _patch_next_work_live_snapshot(monkeypatch, [_nw_task(70), _nw_task(71)], queued_routine={70})

    view = planning_loop.run_next_work_view(workspace_root=tmp_path)

    assert [c["task_id"] for c in view["candidates"]] == [71]
