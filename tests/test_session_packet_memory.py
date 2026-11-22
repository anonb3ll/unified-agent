"""Automatic session-packet memory injection (task #836)."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unified_agent import config, storage
from unified_agent.lifecycle.packet import _hit_token_count, build_session_packet
from unified_agent.tools import memory as memory_tools
from unified_agent.tools import tasks as task_tools


def _save(*, title: str, content: str, **kwargs) -> dict:
    return memory_tools.memory_save(
        content=content,
        title=title,
        author=kwargs.pop("author", "Desktop_Agent4"),
        **kwargs,
    )


def _packet_for_task(title: str, *, project: str = "unified-agent") -> dict:
    task_id = task_tools.task_add(title=title, project=project)["id"]
    return build_session_packet(task_id=task_id, agent="Desktop_Agent4")


def _ids(packet: dict) -> list[str]:
    return [hit["id"] for hit in packet.get("memory") or []]


def test_no_context_fallback_does_not_dump_memories():
    _save(
        title="Zirconprobe unrelated high confidence",
        content="Zirconprobe fact that must not leak without a task.",
        tags=["project:unified-agent"],
        confidence=0.99,
        salience=0.8,
    )
    packet = build_session_packet(agent="Desktop_Agent4")
    assert packet["memory"] == []
    assert packet["task"] is None


def test_deleted_note_is_not_injected_into_a_session_packet():
    from unified_agent.tools.memory import memory_delete

    note = _save(
        title="Zircondeleted deployment",
        content="Zircondeleted deployment fact.",
        tags=["project:unified-agent"],
        confidence=0.99,
        salience=0.8,
    )
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        assert memory_delete(note["id"])["deleted"] is True
    finally:
        config.agent_identity_var.reset(token)
    packet = _packet_for_task("Zircondeleted deployment")
    assert note["id"] not in _ids(packet)


def test_short_title_does_not_broaden_into_all_memories():
    _save(
        title="Zirconprobe high confidence",
        content="Zirconprobe should stay out of an unsearchable title.",
        tags=["project:unified-agent"],
        confidence=0.99,
    )
    packet = _packet_for_task("ok")
    assert packet["memory"] == []


def test_confidence_threshold_filters_injection_but_not_explicit_search(monkeypatch):
    monkeypatch.setenv("UA_MEMORY_INJECT_CONFIDENCE_THRESHOLD", "0.7")
    kept = _save(
        title="Zirconprobe kept",
        content="Zirconprobe above-threshold fact.",
        tags=["project:unified-agent", "fact"],
        confidence=0.9,
    )
    dropped = _save(
        title="Zirconprobe dropped",
        content="Zirconprobe below-threshold fact.",
        tags=["project:unified-agent", "fact"],
        confidence=0.4,
    )
    packet = _packet_for_task("Zirconprobe")
    injected = _ids(packet)
    assert kept["id"] in injected
    assert dropped["id"] not in injected

    explicit = memory_tools.memory_search("Zirconprobe")
    explicit_ids = {row["id"] for row in explicit["results"]}
    assert dropped["id"] in explicit_ids
    assert kept["id"] in explicit_ids


def test_item_and_token_caps_are_strict(monkeypatch):
    monkeypatch.setenv("UA_MEMORY_INJECT_ITEM_CAP", "2")
    # Since #1459 the budget also charges each hit's epistemic/author labels.
    monkeypatch.setenv("UA_MEMORY_INJECT_TOKEN_CAP", "14")
    for idx in range(4):
        _save(
            title=f"Zirconcap note {idx} extra filler words here",
            content=f"Zirconcap note number {idx}.",
            tags=["project:unified-agent"],
            confidence=0.95,
        )
    packet = _packet_for_task("Zirconcap")
    hits = packet["memory"]
    assert len(hits) <= 2
    tokens = sum(_hit_token_count(hit) for hit in hits)
    assert tokens <= 14
    assert hits, "at least one ranked hit should fit the token budget"


def test_foreign_project_is_excluded_despite_higher_confidence():
    relevant = _save(
        title="Zirconrank project local",
        content="Zirconrank project-scoped fact.",
        tags=["project:unified-agent", "fact"],
        confidence=0.72,
    )
    unrelated = _save(
        title="Zirconrank foreign",
        content="Zirconrank unrelated higher-confidence fact.",
        tags=["project:browser", "fact"],
        confidence=0.95,
    )
    packet = _packet_for_task("Zirconrank", project="unified-agent")
    injected = _ids(packet)
    assert relevant["id"] in injected
    assert unrelated["id"] not in injected


def test_stable_ordering_breaks_ties_by_title_then_id():
    beta = _save(
        title="Zircontie beta-note",
        content="Zircontie tie-break beta.",
        tags=["project:unified-agent"],
        confidence=0.88,
    )
    alpha = _save(
        title="Zircontie alpha-note",
        content="Zircontie tie-break alpha.",
        tags=["project:unified-agent"],
        confidence=0.88,
    )
    first = _ids(_packet_for_task("Zircontie"))
    second = _ids(_packet_for_task("Zircontie"))
    assert first == second
    assert first[:2] == [alpha["id"], beta["id"]]


def test_expired_and_untrusted_notes_are_not_injected(monkeypatch):
    monkeypatch.setenv("UA_UNTRUSTED_AGENTS", "Mallory")
    expired = _save(
        title="Zircontrust expired",
        content="Zircontrust expired fact.",
        tags=["project:unified-agent"],
        confidence=0.99,
        expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )
    untrusted = _save(
        title="Zircontrust untrusted",
        content="Zircontrust untrusted fact.",
        tags=["project:unified-agent"],
        confidence=0.99,
        author="Mallory",
    )
    kept = _save(
        title="Zircontrust trusted",
        content="Zircontrust trusted fact.",
        tags=["project:unified-agent"],
        confidence=0.8,
    )
    packet = _packet_for_task("Zircontrust")
    injected = _ids(packet)
    assert kept["id"] in injected
    assert expired["id"] not in injected
    assert untrusted["id"] not in injected


@pytest.mark.parametrize("tags", [["project:browser"], ["project:unified-agent-other"]])
def test_foreign_only_matches_are_not_a_context_fallback(tags):
    _save(title="Zirconforeign note", content="Zirconforeign fact.", tags=tags, confidence=0.99)
    assert _packet_for_task("Zirconforeign")["memory"] == []


@pytest.mark.parametrize("tag", ["project:unified_agent", " PROJECT:Unified-Agent "])
def test_known_project_aliases_and_case_match(tag):
    note = _save(title="Zirconalias note", content="Zirconalias fact.", tags=[tag], confidence=0.9)
    assert note["id"] in _ids(_packet_for_task("Zirconalias"))


def test_global_and_shared_project_notes_preserve_compatibility():
    global_note = _save(
        title="Zirconglobal global", content="Zirconglobal fact.", tags=["fact"], confidence=0.99
    )
    shared = _save(
        title="Zirconglobal shared",
        content="Zirconglobal fact.",
        tags=["project:browser", "project:unified-agent"],
        confidence=0.7,
    )
    ids = _ids(_packet_for_task("Zirconglobal"))
    assert ids == [shared["id"], global_note["id"]]


def test_foreign_candidates_do_not_exhaust_the_candidate_limit():
    # Seed via the real save seam: >50 foreign FTS matches precede the local one.
    for idx in range(51):
        _save(
            title=f"Zirconstarve foreign {idx}",
            content="Zirconstarve fact.",
            tags=["project:browser"],
            confidence=0.99,
        )
    local = _save(
        title="Zirconstarve local",
        content="Zirconstarve fact.",
        tags=["project:unified-agent"],
        confidence=0.8,
    )
    assert _ids(_packet_for_task("Zirconstarve")) == [local["id"]]


def test_scope_uses_full_tags_before_bounded_packet_projection():
    foreign = _save(
        title="Zirconhidden foreign", content="Zirconhidden fact.", tags=["fact"], confidence=0.99
    )
    # Simulate an older/imported row with project attribution after the display cap.
    with storage.connect() as conn:
        conn.execute(
            "UPDATE memory SET tags=? WHERE id=?",
            (json.dumps([f"tag:{i}" for i in range(12)] + ["project:browser"]), foreign["id"]),
        )
    assert _packet_for_task("Zirconhidden")["memory"] == []


@pytest.mark.parametrize("active_org", [None, "org_a"])
def test_org_scope_never_treats_null_as_wildcard(monkeypatch, active_org):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    notes = {}
    for org in [None, "org_a", "org_b"]:
        if org:
            monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)
        else:
            monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        notes[org] = _save(
            title=f"Zircontenant {org}",
            content="Zircontenant fact.",
            tags=["fact"],
            confidence=0.99,
        )
    if active_org:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", active_org)
    else:
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    assert _ids(_packet_for_task("Zircontenant")) == [notes[active_org]["id"]]


def test_task_org_mismatch_does_not_inject_active_org_context(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    task = task_tools.task_add(title="Zirconanchor", project="unified-agent")["id"]
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    _save(
        title="Zirconanchor note",
        content="Zirconanchor fact.",
        tags=["project:unified-agent"],
        confidence=0.99,
    )
    assert build_session_packet(task_id=task)["memory"] == []


def test_restart_recalls_only_current_scoped_memory(tmp_path):
    old = _save(
        title="Zirconrevision old",
        content="Zirconrevision old fact.",
        tags=["project:unified-agent"],
        confidence=0.99,
    )
    current = _save(
        title="Zirconrevision current",
        content="Zirconrevision current fact.",
        tags=["project:unified-agent"],
        confidence=0.9,
    )
    with storage.connect() as conn:
        conn.execute("UPDATE memory SET superseded_by=? WHERE id=?", (current["id"], old["id"]))
    task = task_tools.task_add(title="Zirconrevision", project="unified-agent")["id"]
    first = build_session_packet(task_id=task)
    # Cold interpreter, fresh connection: no inherited packet cache or credentials.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from unified_agent.lifecycle.packet import build_session_packet; "
            + f"print(json.dumps(build_session_packet(task_id={task})))",
        ],
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "UA_HOME": str(config.data_home()),
            "UA_WORKSPACE": str(config.workspace_root()),
            "UA_STATE_SYNC": "0",
            "UA_EVENT_BUS": "0",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    second = json.loads(result.stdout)
    assert _ids(first) == _ids(second) == [current["id"]]
    assert second["labels"]["authority"] is False


@pytest.mark.parametrize(
    "project, tag", [("client-portal", "project:tap"), ("sample-store", "project:sample-store")]
)
def test_checkout_aliases_use_existing_project_mapping(project, tag):
    note = _save(
        title="Zirconcheckout note", content="Zirconcheckout fact.", tags=[tag], confidence=0.9
    )
    assert _ids(_packet_for_task("Zirconcheckout", project=project)) == [note["id"]]


@pytest.mark.parametrize(
    "raw_tags", ["not-json", '{"project:browser": true}', '[42, "project:browser"]']
)
def test_malformed_attribution_does_not_become_global_context(raw_tags):
    note = _save(
        title="Zirconmalformed note",
        content="Zirconmalformed fact.",
        tags=["fact"],
        confidence=0.99,
    )
    with storage.connect() as conn:
        conn.execute("UPDATE memory SET tags=? WHERE id=?", (raw_tags, note["id"]))
    assert _packet_for_task("Zirconmalformed")["memory"] == []


@pytest.mark.parametrize("ineligible", ["expired", "untrusted", "below-threshold"])
def test_ineligible_candidates_do_not_crowd_out_valid_context(monkeypatch, ineligible):
    monkeypatch.setenv("UA_UNTRUSTED_AGENTS", "Mallory")
    monkeypatch.setenv("UA_MEMORY_INJECT_CONFIDENCE_THRESHOLD", "0.7")
    for idx in range(51):
        note = _save(
            title=f"Zirconeligible invalid {idx}",
            content="Zirconeligible fact.",
            tags=["project:unified-agent"],
            confidence=0.99,
            author="Mallory" if ineligible == "untrusted" else "Desktop_Agent4",
            expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()
            if ineligible == "expired"
            else None,
        )
        if ineligible == "below-threshold":
            # Imported malformed confidence sorts ahead of numeric values in SQL,
            # but the existing confidence reader falls back to sub-threshold salience.
            with storage.connect() as conn:
                conn.execute(
                    "UPDATE memory SET confidence='invalid', salience=0.6 WHERE id=?", (note["id"],)
                )
    valid = _save(
        title="Zirconeligible valid",
        content="Zirconeligible fact.",
        tags=["project:unified-agent"],
        confidence=0.8,
    )
    assert _ids(_packet_for_task("Zirconeligible")) == [valid["id"]]
