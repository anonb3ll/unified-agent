"""Evidence-backed team memory and bounded experience context (task #1459)."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.lifecycle.packet import build_session_packet
from unified_agent.tools import memory as memory_tools
from unified_agent.tools import tasks as task_tools
from unified_agent.tools.registry import session_end, session_start

ROOT = Path(__file__).resolve().parents[1]


def _save(*, title: str, content: str, **kwargs) -> dict:
    kwargs.setdefault("author", "Desktop_Agent2")
    kwargs.setdefault("confidence", 0.9)
    return memory_tools.memory_save(content=content, title=title, **kwargs)


def _task(title: str, project: str = "unified-agent") -> int:
    return task_tools.task_add(title=title, project=project)["id"]


def _search_ids(query: str, **kwargs) -> list[str]:
    return [hit["id"] for hit in memory_tools.memory_search(query, **kwargs)["results"]]


def _packet_ids(packet: dict) -> list[str]:
    return [hit["id"] for hit in packet.get("memory") or []]


def _ended_session(task_id: int, *, agent: str, status: str, summary: str) -> str:
    sid = session_start(kind="coding", agent=agent, task_id=task_id)["session_id"]
    assert session_end(sid, status=status, summary=summary)["closed"] is True
    return sid


# --- changed fact supersedes the stale answer -------------------------------


def test_changed_fact_supersedes_stale_answer():
    old = _save(
        title="Zirconexport format",
        content="Zirconexport format is CSV.",
        tags=["project:unified-agent", "fact"],
        epistemic="fact",
    )
    new = _save(
        title="Zirconexport format",
        content="Zirconexport format is JSON Lines from 2026-03-01.",
        tags=["project:unified-agent", "fact"],
        epistemic="fact",
        valid_from="2026-03-01",
        supersedes=old["id"],
    )
    assert new["supersedes"] == old["id"]

    assert _search_ids("Zirconexport format") == [new["id"]]
    packet = build_session_packet(task_id=_task("Zirconexport format"))
    assert _packet_ids(packet) == [new["id"]]
    assert packet["memory"][0]["valid_from"] == "2026-03-01T00:00:00+00:00"

    # The stale note is demoted, never destroyed, and the link reads both ways.
    stale = memory_tools.memory_get(old["id"])
    assert stale["superseded_by"] == new["id"]
    assert stale["content"] == "Zirconexport format is CSV."
    assert memory_tools.memory_get(new["id"])["supersedes"] == [old["id"]]


def test_supersede_guards_chain_and_update_path():
    old = _save(title="Zirconchain v1", content="Zirconchain value one.")
    mid = _save(title="Zirconchain v2", content="Zirconchain value two.", supersedes=old["id"])
    with pytest.raises(ToolError, match="already superseded"):
        _save(title="Zirconchain v3", content="Zirconchain value three.", supersedes=old["id"])
    with pytest.raises(ToolError, match="only supported when creating"):
        _save(title="x", content="y", id=mid["id"], supersedes=old["id"])
    with pytest.raises(ToolError, match="no memory note"):
        _save(title="Zirconchain v4", content="Zirconchain four.", supersedes="nope0000")


def test_superseding_a_peer_note_is_refused_without_overwrite():
    peer = _save(
        title="Zirconpeer deploy target",
        content="Zirconpeer deploy target: staging.",
        tags=["project:unified-agent"],
        author="Desktop_Agent2",
    )
    with pytest.raises(ToolError, match="owned by 'Desktop_Agent2'") as exc:
        _save(
            title="Zirconpeer deploy target",
            content="Zirconpeer deploy target: production.",
            tags=["project:unified-agent"],
            author="Desktop_Agent4",
            supersedes=peer["id"],
        )
    # The refusal does not coach the bypass (2026-07-12 guard drill).
    assert "overwrite" not in str(exc.value)
    assert memory_tools.memory_get(peer["id"])["superseded_by"] is None

    # contradicts links the dispute; both notes stay visible to the owner.
    mine = _save(
        title="Zirconpeer deploy target",
        content="Zirconpeer deploy target: production.",
        tags=["project:unified-agent"],
        author="Desktop_Agent4",
        contradicts=[peer["id"]],
    )
    assert set(_search_ids("Zirconpeer deploy target")) == {peer["id"], mine["id"]}
    assert memory_tools.memory_get(mine["id"])["contradicts"] == [peer["id"]]

    replaced = _save(
        title="Zirconpeer deploy target",
        content="Zirconpeer deploy target: production (confirmed).",
        author="Desktop_Agent4",
        supersedes=peer["id"],
        overwrite=True,
    )
    assert memory_tools.memory_get(peer["id"])["superseded_by"] == replaced["id"]


def test_broadcast_cannot_be_superseded_by_an_agent():
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        broadcast = memory_tools.memory_save(
            content="Zirconbroadcast rollout paused.",
            title="Zirconbroadcast",
            tags=["broadcast"],
            author="Desktop_Person",
        )
    finally:
        config.agent_identity_var.reset(token)
    with pytest.raises(ToolError, match="verified human"):
        _save(
            title="Zirconbroadcast",
            content="Zirconbroadcast rollout resumed.",
            author="Desktop_Person",
            supersedes=broadcast["id"],
        )
    assert memory_tools.memory_get(broadcast["id"])["superseded_by"] is None


# --- facts vs inferred conclusions -------------------------------------------


def test_inferred_claim_must_carry_evidence():
    with pytest.raises(ToolError, match="inferred note must carry evidence"):
        _save(title="Zirconinfer", content="Zirconinfer guess.", epistemic="inferred")
    with pytest.raises(ToolError, match="kind one of"):
        _save(
            title="Zirconinfer",
            content="Zirconinfer guess.",
            epistemic="inferred",
            evidence=["hunch:42"],
        )
    with pytest.raises(ToolError, match="epistemic must be one of"):
        _save(title="Zirconinfer", content="Zirconinfer guess.", epistemic="rumour")

    inferred = _save(
        title="Zirconinfer flaky test",
        content="Zirconinfer flaky test fails only under xdist.",
        tags=["project:unified-agent"],
        epistemic="inferred",
        evidence=["session:abc123", "task:1459", "event:01HZXYZ", "session:abc123"],
    )
    fact = _save(
        title="Zirconinfer flaky test run",
        content="Zirconinfer flaky test failed in run 7.",
        tags=["project:unified-agent"],
        epistemic="fact",
        evidence=["run:7"],
    )
    legacy = _save(
        title="Zirconinfer flaky test legacy",
        content="Zirconinfer flaky test legacy note.",
        tags=["project:unified-agent"],
    )
    got = memory_tools.memory_get(inferred["id"])
    assert got["epistemic"] == "inferred"
    assert got["evidence"] == ["session:abc123", "task:1459", "event:01HZXYZ"]
    assert "epistemic" not in memory_tools.memory_get(legacy["id"])

    packet = build_session_packet(task_id=_task("Zirconinfer flaky test"))
    hits = {hit["id"]: hit for hit in packet["memory"]}
    assert hits[inferred["id"]]["epistemic"] == "inferred"
    assert hits[inferred["id"]]["evidence"] == ["session:abc123", "task:1459", "event:01HZXYZ"]
    assert hits[inferred["id"]]["author"] == "Desktop_Agent2"
    assert hits[fact["id"]]["epistemic"] == "fact"
    assert hits[legacy["id"]]["epistemic"] == "unlabeled"
    assert "evidence" not in hits[legacy["id"]]


def test_peer_may_add_evidence_but_not_relabel_owner_note():
    note = _save(
        title="Zirconowner",
        content="Zirconowner cache is warm after deploy.",
        epistemic="inferred",
        evidence=["session:s1"],
        author="Desktop_Agent2",
    )
    memory_tools.memory_save(
        id=note["id"],
        content="Seen again after the 2026-09-30 deploy.",
        append=True,
        evidence=["session:s2"],
        author="Desktop_Agent4",
    )
    got = memory_tools.memory_get(note["id"])
    assert got["evidence"] == ["session:s1", "session:s2"]
    assert got["epistemic"] == "inferred"
    with pytest.raises(ToolError, match="stay with its owner"):
        memory_tools.memory_save(
            id=note["id"],
            content="It is a fact.",
            append=True,
            epistemic="fact",
            author="Desktop_Agent4",
        )
    # The owner may relabel; the accumulated evidence stays attached.
    memory_tools.memory_save(
        id=note["id"],
        content="Confirmed by run 9.",
        append=True,
        epistemic="fact",
        evidence=["run:9"],
        author="Desktop_Agent2",
    )
    got = memory_tools.memory_get(note["id"])
    assert got["epistemic"] == "fact"
    assert got["evidence"] == ["session:s1", "session:s2", "run:9"]


# --- experience: session outcomes become attributable context -----------------


def test_session_outcome_becomes_attributable_experience():
    task = _task("Zirconoutcome migrate cache")
    other_task = _task("Zirconoutcome unrelated")
    failed = _ended_session(
        task, agent="Desktop_Agent2", status="failed", summary="Migration failed on lock."
    )
    done = _ended_session(
        task, agent="Desktop_Agent4", status="completed", summary="Migrated with WAL off."
    )
    _ended_session(other_task, agent="Desktop_Agent2", status="completed", summary="Other work.")

    packet = build_session_packet(task_id=task)
    experience = packet["experience"]
    assert [item["evidence"][0] for item in experience] == [f"session:{done}", f"session:{failed}"]
    latest = experience[0]
    assert latest["epistemic"] == "fact"
    assert latest["summary_source"] == "agent_reported"
    assert latest["agent"] == "Desktop_Agent4"
    assert latest["status"] == "completed"
    assert latest["ended_at"]
    assert f"task:{task}" in latest["evidence"]
    assert "Other work" not in json.dumps(experience)

    # The caller's own session is not reported back to it as prior experience.
    own = build_session_packet(task_id=task, session_id=done)
    assert [item["evidence"][0] for item in own["experience"]] == [f"session:{failed}"]


def test_experience_shares_the_injection_token_budget(monkeypatch):
    monkeypatch.setenv("UA_MEMORY_INJECT_TOKEN_CAP", "20")
    task = _task("Zirconbudget task")
    _ended_session(
        task,
        agent="Desktop_Agent2",
        status="completed",
        summary=" ".join(["word"] * 30),
    )
    short = _ended_session(task, agent="Desktop_Agent2", status="completed", summary="Short.")
    experience = build_session_packet(task_id=task)["experience"]
    # Half of 20 tokens: the 30-word record does not fit, the short one does.
    assert [item["evidence"][0] for item in experience] == [f"session:{short}"]


def test_restart_receives_current_memory_and_experience(tmp_path):
    task = _task("Zirconrestart pipeline")
    old = _save(
        title="Zirconrestart pipeline stage",
        content="Zirconrestart pipeline resumes at stage alpha.",
        tags=["project:unified-agent"],
        epistemic="fact",
    )
    current = _save(
        title="Zirconrestart pipeline stage",
        content="Zirconrestart pipeline resumes at stage delta.",
        tags=["project:unified-agent"],
        epistemic="inferred",
        evidence=["task:1459"],
        supersedes=old["id"],
    )
    sid = _ended_session(
        task, agent="Desktop_Agent2", status="completed", summary="Stopped after stage gamma."
    )
    # Cold interpreter, fresh connection: the packet comes from the store alone.
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
            "PYTHONPATH": str(ROOT / "src"),
            "UA_HOME": str(config.data_home()),
            "UA_WORKSPACE": str(config.workspace_root()),
            "UA_STATE_SYNC": "0",
            "UA_EVENT_BUS": "0",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    packet = json.loads(result.stdout)
    assert _packet_ids(packet) == [current["id"]]
    assert packet["memory"][0]["epistemic"] == "inferred"
    assert packet["memory"][0]["evidence"] == ["task:1459"]
    assert [item["evidence"][0] for item in packet["experience"]] == [f"session:{sid}"]
    assert packet["experience"][0]["summary"] == "Stopped after stage gamma."
    assert packet["labels"]["authority"] is False


# --- negative scope -----------------------------------------------------------


def test_other_project_inference_is_not_injected():
    foreign = _save(
        title="Zirconscope cache rule",
        content="Zirconscope cache rule for browser.",
        tags=["project:browser"],
        epistemic="inferred",
        evidence=["session:x1"],
    )
    local = _save(
        title="Zirconscope cache rule",
        content="Zirconscope cache rule for the hub.",
        tags=["project:unified-agent"],
        epistemic="inferred",
        evidence=["session:x2"],
    )
    ids = _packet_ids(build_session_packet(task_id=_task("Zirconscope cache rule")))
    assert ids == [local["id"]]
    assert foreign["id"] not in ids


def test_cross_org_links_and_experience_are_refused(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    theirs = _save(title="Zirconorg fact", content="Zirconorg fact for org a.")
    task_a = _task("Zirconorg task")
    _ended_session(task_a, agent="Desktop_Agent2", status="completed", summary="Org A outcome.")

    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    with pytest.raises(ToolError, match="no memory note"):
        _save(title="Zirconorg fact", content="Zirconorg fact b.", supersedes=theirs["id"])
    with pytest.raises(ToolError, match="no memory note"):
        _save(title="Zirconorg fact", content="Zirconorg fact b.", contradicts=[theirs["id"]])
    # Another tenant's task yields neither its memory nor its session outcomes.
    packet = build_session_packet(task_id=task_a)
    assert packet["experience"] == []
    assert packet["memory"] == []
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    assert memory_tools.memory_get(theirs["id"])["superseded_by"] is None


# --- retrieval comparison against the frozen #1458 baseline -------------------


def _eval_module():
    path = ROOT / "scripts" / "eval_memory_1458.py"
    spec = importlib.util.spec_from_file_location("eval_memory_1458", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


def _baseline_report(tmp_path: Path) -> dict:
    module, path = _eval_module()
    home = tmp_path / "baseline"
    home.mkdir()
    for flag in ("--seed-child", "--query-child"):
        result = subprocess.run(
            [sys.executable, str(path), flag, str(home)],
            env=module._env(home),
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    return json.loads(result.stdout)


def test_retrieval_matches_frozen_baseline_through_supersedes(tmp_path, monkeypatch):
    """Seed the frozen v2 fixture through memory_save(supersedes=...) instead of
    the baseline's direct superseded_by writes; every case must score the same,
    and the session packet must honour the same exclusions."""
    from unified_agent import embeddings

    monkeypatch.setattr(
        embeddings,
        "generate_embedding",
        lambda _text: (_ for _ in ()).throw(RuntimeError("offline fixture")),
    )
    monkeypatch.setenv("UA_MEMORY_INJECT_CONFIDENCE_THRESHOLD", "0")
    baseline = _baseline_report(tmp_path)
    module, _path = _eval_module()
    fixture = module._fixture()
    successor_of = {
        note["superseded_by"]: note["id"] for note in fixture["notes"] if note.get("superseded_by")
    }
    id_map: dict[str, str] = {}
    for note in fixture["notes"]:
        broadcast = "broadcast" in note["tags"]
        author = "Desktop_Person" if broadcast else "Desktop_Agent2"
        token = config.agent_identity_var.set(author) if broadcast else None
        try:
            saved = memory_tools.memory_save(
                title=note["title"],
                content=note["content"],
                tags=note["tags"],
                author=author,
                epistemic="fact",
                evidence=[f"vault:fixture/{note['id']}"],
                valid_from=note["created_at"],
                supersedes=id_map.get(successor_of.get(note["id"], "")),
            )
        finally:
            if token is not None:
                config.agent_identity_var.reset(token)
        id_map[note["id"]] = saved["id"]
    with storage.connect() as conn:
        for note in fixture["notes"]:
            conn.execute(
                "UPDATE memory SET source_host=? WHERE id=?",
                (note["source_host"], id_map[note["id"]]),
            )

    baseline_cases = {case["case"]: case for case in baseline["cases"]}
    reverse = {v: k for k, v in id_map.items()}
    assert baseline["summary"]["passed"] == baseline["summary"]["case_count"]
    for case in fixture["cases"]:
        result = memory_tools.memory_search(
            case["query"], tags=case.get("tags"), source_host=case.get("source_host"), limit=10
        )
        labels = {reverse.get(hit["id"], hit["id"]) for hit in result["results"]}
        expected = set(case.get("expected_ids", []))
        excluded = set(case.get("excluded_ids", []))
        recall = len(expected & labels) / len(expected) if expected else None
        passed = expected <= labels and not excluded & labels
        base = baseline_cases[case["id"]]
        # Same per-case outcome and recall@10 as the baseline arm.
        assert (passed, recall) == (base["passed"], base["recall_at_10"]), case["id"]
        assert sorted(excluded & labels) == base["excluded_fixture_labels_present"]

        # Injected context: the same exclusions hold, inside the project scope.
        project = next(
            (t.split(":", 1)[1] for t in case.get("tags") or [] if t.startswith("project:")),
            None,
        )
        if project is None:
            continue
        # Fixture projects are synthetic, so bind the task to one directly.
        task = task_tools.task_add(title=case["query"])["id"]
        with storage.connect() as conn:
            conn.execute("UPDATE tasks SET project=? WHERE id=?", (project, task))
        packet = build_session_packet(task_id=task)
        packet_labels = {reverse.get(i, i) for i in _packet_ids(packet)}
        assert not set(case.get("excluded_ids", [])) & packet_labels, case["id"]
        assert set(case.get("expected_ids", [])) <= packet_labels, case["id"]
        for hit in packet["memory"]:
            assert hit["epistemic"] == "fact"
            assert hit["evidence"][0].startswith("vault:fixture/")


# --- learning proposals keep supporting ids and contradiction links ----------


def _proposal(sid: str, summary: str) -> tuple[dict, str]:
    from unified_agent.tools.learning import propose_session_learnings

    written = session_end(sid, status="completed", summary=summary)["learning_proposal"]
    assert written["written"] is True
    text = (Path(config.vault_root()) / written["path"]).read_text(encoding="utf-8")
    # The dry run returns the same candidates as structured data.
    return propose_session_learnings(sid, dry_run=True), text


def test_learning_proposal_candidates_carry_evidence_and_validity():
    Path(config.vault_root()).mkdir(parents=True, exist_ok=True)
    task = _task("Zirconlearn claim")
    sid = session_start(kind="coding", agent="Desktop_Agent1", task_id=task)["session_id"]
    proposal, text = _proposal(sid, "Always claim with task_claim before product edits.")
    [cand] = proposal["candidates"]
    assert cand["epistemic"] == "inferred"
    assert cand["evidence"] == [f"session:{sid}", f"task:{task}"]
    assert cand["observed_at"]
    assert cand["contradicts"] == []
    assert "- epistemic: inferred" in text
    assert f"`session:{sid}`" in text
    assert "- observed_at: " in text
    # Still a proposal: nothing reached memory.
    assert memory_tools.memory_list(limit=50)["items"] == []


def test_conflicting_near_duplicate_is_linked_not_dropped_or_overwritten():
    Path(config.vault_root()).mkdir(parents=True, exist_ok=True)
    peer = _save(
        title="Ruff format",
        content="Always run ruff format: enabled for this repo.",
        tags=["project:unified-agent"],
        author="Desktop_Agent2",
    )
    sid = session_start(kind="coding", agent="Desktop_Agent1")["session_id"]
    proposal, text = _proposal(sid, "Always run ruff format: disabled for this repo.")
    [cand] = proposal["candidates"]
    assert cand["contradicts"] == [{"id": peer["id"], "author": "Desktop_Agent2"}]
    assert f"- contradicts: `{peer['id']}` (owner `Desktop_Agent2`)" in text
    peer_now = memory_tools.memory_get(peer["id"])
    assert peer_now["superseded_by"] is None
    assert peer_now["content"] == "Always run ruff format: enabled for this repo."


# --- review follow-ups (PR #951) ----------------------------------------------


def test_proposal_fingerprint_tracks_evidence_and_contradictions():
    from unified_agent.tools.learning import _proposal_fingerprint

    base = {"kind": "durable_rule", "text": "Always x.", "confidence": 0.7}
    one = _proposal_fingerprint([{**base, "evidence": ["session:a"], "contradicts": []}])
    more = _proposal_fingerprint(
        [{**base, "evidence": ["session:a", "event:01X"], "contradicts": []}]
    )
    linked = _proposal_fingerprint(
        [{**base, "evidence": ["session:a"], "contradicts": [{"id": "n1", "author": "x"}]}]
    )
    assert len({one, more, linked}) == 3


def test_new_conflicting_note_refreshes_an_existing_proposal_once():
    """Contradiction links are part of the fingerprint, so the Inbox file updates."""
    from unified_agent.tools.learning import propose_session_learnings

    Path(config.vault_root()).mkdir(parents=True, exist_ok=True)
    sid = session_start(kind="coding", agent="Desktop_Agent1")["session_id"]
    _, first = _proposal(sid, "Always run ruff format: disabled for this repo.")
    assert "- contradicts:" not in first
    peer = _save(
        title="Ruff format",
        content="Always run ruff format: enabled for this repo.",
        tags=["project:unified-agent"],
        author="Desktop_Agent2",
    )
    refreshed = propose_session_learnings(sid)
    assert refreshed["written"] is True and refreshed["applied"] is False
    text = (Path(config.vault_root()) / refreshed["path"]).read_text(encoding="utf-8")
    assert f"- contradicts: `{peer['id']}` (owner `Desktop_Agent2`)" in text
    # Unchanged evidence and links leave the refreshed file alone.
    again = propose_session_learnings(sid)
    assert again["written"] is False and again["existing"] is True


def test_labelled_note_defaults_valid_from_to_creation():
    labelled = _save(title="Zirconvalid", content="Zirconvalid fact.", epistemic="fact")
    plain = _save(title="Zirconvalid plain", content="Zirconvalid plain note.")
    got = memory_tools.memory_get(labelled["id"])
    assert got["valid_from"] == got["created_at"]
    assert "valid_from" not in memory_tools.memory_get(plain["id"])


def test_superseded_note_leaves_tag_and_master_indexes():
    old = _save(title="Zirconindex old", content="Zirconindex v1.", tags=["zirconidx"])
    new = _save(
        title="Zirconindex new",
        content="Zirconindex v2.",
        tags=["zirconidx-v2"],
        supersedes=old["id"],
    )
    master = memory_tools.memory_get(memory_tools.MASTER_INDEX_ID)["content"]
    assert "`zirconidx-v2`" in master
    # The old note's only tag now has no live note: its index is dropped.
    assert "**`zirconidx`**" not in master
    with pytest.raises(ToolError):
        memory_tools.memory_get(memory_tools._tag_index_id("zirconidx"))
    tag_index = memory_tools.memory_get(memory_tools._tag_index_id("zirconidx-v2"))["content"]
    assert new["id"] in tag_index and old["id"] not in tag_index
    # Restoring brings it back.
    memory_tools.memory_restore(old["id"])
    restored = memory_tools.memory_get(memory_tools._tag_index_id("zirconidx"))["content"]
    assert old["id"] in restored


def test_injected_evidence_counts_against_the_token_budget():
    from unified_agent.lifecycle.packet import _experience_token_count, _hit_token_count

    hit = {"title": "a b", "snippet": "c d", "epistemic": "inferred", "author": "X"}
    bare = _hit_token_count(hit)
    assert _hit_token_count({**hit, "evidence": ["session:1", "task:2", "event:3"]}) == bare + 3
    item = {"agent": "X", "status": "completed", "summary": "done", "epistemic": "fact"}
    assert _experience_token_count({**item, "evidence": ["session:1", "task:2"]}) == (
        _experience_token_count(item) + 2
    )


def test_memory_export_and_manifest_round_trip_evidence_fields():
    from unified_agent.tools import export as export_tools

    note = _save(
        title="Zirconexport fields",
        content="Zirconexport fields note.",
        epistemic="inferred",
        evidence=["session:s9", "task:1459"],
        valid_from="2026-09-01",
    )
    other = _save(title="Zirconexport other", content="Zirconexport other.")
    _save(
        title="Zirconexport dispute",
        content="Zirconexport dispute.",
        contradicts=[other["id"]],
    )
    exported = json.loads(
        export_tools.export_memory(format="json", write_to_vault=False)["content"]
    )
    row = next(r for r in exported if r["id"] == note["id"])
    assert row["epistemic"] == "inferred"
    assert row["evidence"] == ["session:s9", "task:1459"]
    assert row["valid_from"] == "2026-09-01T00:00:00+00:00"

    manifest = export_tools.export_migration_manifest()["manifest"]
    with storage.connect() as conn:
        conn.execute("DELETE FROM memory WHERE id=?", (note["id"],))
    result = export_tools.import_migration_manifest(manifest, apply=True)
    assert note["id"] in result["memory"]["inserted"]
    back = memory_tools.memory_get(note["id"])
    assert back["epistemic"] == "inferred"
    assert back["evidence"] == ["session:s9", "task:1459"]
    assert back["valid_from"] == "2026-09-01T00:00:00+00:00"


def test_update_by_id_refuses_another_tenants_note(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    theirs = _save(
        title="Zircontenantupd",
        content="Zircontenantupd org a.",
        epistemic="inferred",
        evidence=["session:a1"],
    )
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    with pytest.raises(ToolError, match="no memory note"):
        memory_tools.memory_save(
            id=theirs["id"],
            content="Appended from org b.",
            append=True,
            evidence=["session:b1"],
            author="Desktop_Agent4",
        )
    with pytest.raises(ToolError, match="no memory note"):
        memory_tools.memory_save(
            id=theirs["id"], content="Replaced.", author="Desktop_Agent2", overwrite=True
        )
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    got = memory_tools.memory_get(theirs["id"])
    assert got["content"] == "Zircontenantupd org a."
    assert got["evidence"] == ["session:a1"]


def test_proposals_never_link_another_tenants_note(monkeypatch):
    from unified_agent.tools.learning import propose_session_learnings

    Path(config.vault_root()).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    theirs = _save(
        title="Ruff format",
        content="Always run ruff format: enabled for this repo.",
        author="Desktop_Agent2",
    )
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    sid = session_start(kind="coding", agent="Desktop_Agent1")["session_id"]
    session_end(sid, status="completed", summary="Always run ruff format: disabled for this repo.")
    [cand] = propose_session_learnings(sid, dry_run=True)["candidates"]
    assert cand["contradicts"] == []
    assert theirs["id"] not in json.dumps(cand)


def test_memory_exports_are_tenant_scoped(monkeypatch):
    from unified_agent.tools import export as export_tools

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    theirs = _save(
        title="Zirconexporttenant a",
        content="Zirconexporttenant org a.",
        epistemic="inferred",
        evidence=["session:secret-a"],
    )
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    mine = _save(
        title="Zirconexporttenant b",
        content="Zirconexporttenant org b.",
        epistemic="inferred",
        evidence=["session:b1"],
    )
    content = export_tools.export_memory(format="json", write_to_vault=False)["content"]
    ids = {row["id"] for row in json.loads(content)}
    assert mine["id"] in ids and theirs["id"] not in ids
    assert "secret-a" not in content
    manifest = export_tools.export_migration_manifest()["manifest"]
    manifest_ids = {row["id"] for row in manifest["memory"]}
    assert mine["id"] in manifest_ids and theirs["id"] not in manifest_ids
    assert "secret-a" not in json.dumps(manifest)


# --- full-diff review follow-ups (PR #951 @ 6e0c9146) -------------------------


def test_free_text_exit_status_is_redacted_and_capped():
    task = _task("Zirconexit probe")
    note = _save(
        title="Zirconexit probe",
        content="Zirconexit probe fact.",
        tags=["project:unified-agent"],
    )
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5"
    probe = secret + "x" * 14000
    sid = session_start(kind="coding", agent="Desktop_Agent2", task_id=task)["session_id"]
    session_end(sid, status="failed", exit_status=probe, summary="Probe run.")
    packet = build_session_packet(task_id=task)
    encoded = json.dumps(packet)
    assert secret not in encoded
    [item] = packet["experience"]
    assert len(item["exit_status"]) <= 64
    # The packet keeps its sections instead of collapsing under the byte bound.
    assert "_truncated" not in packet
    assert _packet_ids(packet) == [note["id"]]
    assert packet["recent_events"] is not None


def test_manifest_tasks_and_events_are_tenant_scoped(monkeypatch):
    from unified_agent.tools import export as export_tools

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    theirs = _task("Zircontenanttask org a")
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, originator, created_at, org_id) "
            "VALUES ('01ZZZZTENANTA', 'agent.test', '{}', 'x', '2026-10-01', 'org_a')"
        )
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    mine = _task("Zircontenanttask org b")
    manifest = export_tools.export_migration_manifest()["manifest"]
    task_ids = {row["id"] for row in manifest["tasks"]}
    assert mine in task_ids and theirs not in task_ids
    assert "01ZZZZTENANTA" not in {row["ulid"] for row in manifest["event_log"]}


def test_supersede_race_loses_cleanly(monkeypatch):
    old = _save(title="Zirconrace", content="Zirconrace v1.")
    winner = _save(title="Zirconrace", content="Zirconrace v2.", supersedes=old["id"])
    real = memory_tools._linked_note

    def stale_read(conn, note_id, *, field):
        # Simulate the check running before the competing supersede committed.
        row = dict(real(conn, note_id, field=field))
        row["superseded_by"] = None
        return row

    monkeypatch.setattr(memory_tools, "_linked_note", stale_read)
    with pytest.raises(ToolError, match="superseded concurrently"):
        _save(title="Zirconrace", content="Zirconrace v3.", supersedes=old["id"])
    monkeypatch.setattr(memory_tools, "_linked_note", real)
    assert memory_tools.memory_get(old["id"])["superseded_by"] == winner["id"]
    assert _search_ids("Zirconrace") == [winner["id"]]


def test_comparable_packet_is_caller_independent():
    from unified_agent.lifecycle.packet import comparable_packet

    task = _task("Zirconcompare task")
    first = _ended_session(task, agent="Desktop_Agent2", status="completed", summary="One.")
    second = _ended_session(task, agent="Desktop_Agent4", status="completed", summary="Two.")
    a = build_session_packet(task_id=task, session_id=first)
    b = build_session_packet(task_id=task, session_id=second)
    assert a["experience"] != b["experience"]
    assert "experience" not in comparable_packet(a)
    assert comparable_packet(a) == comparable_packet(b)


@pytest.mark.parametrize("session_count", [0, 1, 2, 4])
def test_comparable_memory_budget_is_independent_of_excluded_session(monkeypatch, session_count):
    from unified_agent.lifecycle.packet import (
        _experience_token_count,
        _hit_token_count,
        comparable_packet,
    )

    monkeypatch.setenv("UA_MEMORY_INJECT_TOKEN_CAP", "40")
    monkeypatch.setenv("UA_MEMORY_INJECT_CONFIDENCE_THRESHOLD", "0")
    task = _task("Zirconcomparebudget")
    for _ in range(2):
        _save(title="Zirconcomparebudget", content="Zirconcomparebudget " + "word " * 8)
    sessions = [
        _ended_session(
            task,
            agent="Desktop_Agent2",
            status="completed",
            summary="Done." if index == 0 else " ".join(["word"] * 13),
        )
        for index in range(session_count)
    ]
    conn = storage.connect()
    try:
        conn.executemany(
            "UPDATE agent_sessions SET exit_status='ok' WHERE id=?", [(sid,) for sid in sessions]
        )
        conn.commit()
    finally:
        conn.close()
    # A sole ended caller still reserves the same half as a new caller;
    # four sessions exercise the extra row exposed by caller exclusion.
    packets = [build_session_packet(task_id=task, session_id=sid) for sid in [None, *sessions]]
    assert all(comparable_packet(packet) == comparable_packet(packets[0]) for packet in packets)
    assert all(len(packet["memory"]) == (1 if sessions else 2) for packet in packets)
    for packet in packets:
        memory_cost = sum(_hit_token_count(hit) for hit in packet["memory"])
        experience_cost = sum(_experience_token_count(item) for item in packet["experience"])
        assert all(_hit_token_count(hit) == 12 for hit in packet["memory"])
        assert memory_cost + experience_cost <= 40
    if session_count == 2:
        assert sum(_experience_token_count(item) for item in packets[1]["experience"]) == 20
        assert sum(_experience_token_count(item) for item in packets[2]["experience"]) == 8


def test_labelling_a_legacy_note_sets_valid_from_to_creation():
    legacy = _save(title="Zirconlegacy", content="Zirconlegacy note.")
    assert "valid_from" not in memory_tools.memory_get(legacy["id"])
    memory_tools.memory_save(
        id=legacy["id"],
        content="Zirconlegacy note.",
        epistemic="fact",
        author="Desktop_Agent2",
    )
    got = memory_tools.memory_get(legacy["id"])
    assert got["valid_from"] == got["created_at"]


def test_old_manifest_without_new_columns_is_not_a_conflict():
    from unified_agent.tools import export as export_tools

    _save(title="Zirconmanifest", content="Zirconmanifest note.")
    current = export_tools.export_migration_manifest()["manifest"]
    legacy = json.loads(json.dumps(current))
    for row in legacy["memory"]:
        for key in ("epistemic", "evidence", "valid_from", "contradicts"):
            row.pop(key, None)
    diff = export_tools.compare_migration_manifests(legacy, current)
    assert diff["summary"]["memory"]["changed"] == 0
    assert not [c for c in diff["conflicts"] if c["type"] == "conflicting_edit"]


def test_owner_can_replace_evidence_and_clear_contradicts():
    other = _save(title="Zirconclear other", content="Zirconclear other.")
    refs = [f"run:{i}" for i in range(20)]
    note = _save(
        title="Zirconclear",
        content="Zirconclear note.",
        epistemic="inferred",
        evidence=refs,
        contradicts=[other["id"]],
    )
    with pytest.raises(ToolError, match="evidence_replace"):
        memory_tools.memory_save(
            id=note["id"],
            content="More.",
            append=True,
            evidence=["run:99"],
            author="Desktop_Agent2",
        )
    # A peer cannot replace the owner's evidence or clear its links.
    with pytest.raises(ToolError, match="stay with its owner"):
        memory_tools.memory_save(
            id=note["id"],
            content="Peer.",
            append=True,
            evidence=["run:1"],
            evidence_replace=True,
            author="Desktop_Agent4",
        )
    with pytest.raises(ToolError, match="stay with its owner"):
        memory_tools.memory_save(
            id=note["id"], content="Peer.", append=True, contradicts=[], author="Desktop_Agent4"
        )
    memory_tools.memory_save(
        id=note["id"],
        content="Trimmed.",
        append=True,
        evidence=["run:99", "session:s1"],
        evidence_replace=True,
        contradicts=[],
        author="Desktop_Agent2",
    )
    got = memory_tools.memory_get(note["id"])
    assert got["evidence"] == ["run:99", "session:s1"]
    assert "contradicts" not in got
    # Cross-author replacement is possible only with an explicit overwrite.
    memory_tools.memory_save(
        id=note["id"],
        content="Overwritten by peer.",
        evidence=["session:p1"],
        evidence_replace=True,
        author="Desktop_Agent4",
        overwrite=True,
    )
    assert memory_tools.memory_get(note["id"])["evidence"] == ["session:p1"]


def test_manifest_agent_events_are_tenant_attributed(monkeypatch):
    from unified_agent.tools import export as export_tools

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    their_note = _save(title="Zirconevents a", content="Zirconevents org a.")
    their_task = _task("Zirconevents task a")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    my_note = _save(title="Zirconevents b", content="Zirconevents org b.")
    my_task = _task("Zirconevents task b")
    storage.record_agent_event("fleet", ref_id=None, agent="Desktop_Agent2", summary="unowned")
    with storage.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0]

    manifest = export_tools.export_migration_manifest()["manifest"]
    refs = {(row["kind"], str(row["ref_id"])) for row in manifest["agent_events"]}
    assert ("memory", my_note["id"]) in refs and ("task", str(my_task)) in refs
    assert ("memory", their_note["id"]) not in refs
    assert ("task", str(their_task)) not in refs
    assert "unowned" not in json.dumps(manifest["agent_events"])
    assert manifest["agent_events_withheld"] == total - len(manifest["agent_events"]) > 0

    # Private NULL is also a scope, never installation-admin wildcard reach.
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    private_task = _task("Zirconevents private task")
    private_note = _save(title="Zirconevents private note", content="Private scope.")
    hub = export_tools.export_migration_manifest()["manifest"]
    refs = {(row["kind"], str(row["ref_id"])) for row in hub["agent_events"]}
    assert refs == {("task", str(private_task)), ("memory", private_note["id"])}
    assert hub["agent_events_withheld"] == total


def test_manifest_attributes_delegation_and_run_recorder_events(monkeypatch):
    from unified_agent.tools import export as export_tools

    def delegation(org: str, task_id: int) -> int:
        with storage.connect() as conn:
            return conn.execute(
                "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, status, "
                "created_at, updated_at, org_id) VALUES ('A', 'B', ?, 'spec', 'pending', "
                "'2026-10-01', '2026-10-01', ?)",
                (task_id, org),
            ).lastrowid

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    their_task = _task("Zirconkinds task a")
    their_del = delegation("org_a", their_task)
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    my_task = _task("Zirconkinds task b")
    my_del = delegation("org_b", my_task)
    for kind, ref in [
        ("delegation", my_del),
        ("executor_shadow", my_del),
        ("delegation", their_del),
        ("run_recorder", my_task),
        ("run_recorder", their_task),
        ("workflow", "nightly"),
        ("plan", "plan-1"),
    ]:
        storage.record_agent_event(kind, ref_id=str(ref), agent="X", summary=f"{kind} {ref}")

    manifest = export_tools.export_migration_manifest()["manifest"]
    refs = {(row["kind"], str(row["ref_id"])) for row in manifest["agent_events"]}
    assert ("delegation", str(my_del)) in refs
    assert ("executor_shadow", str(my_del)) in refs
    assert ("run_recorder", str(my_task)) in refs
    assert ("delegation", str(their_del)) not in refs
    assert ("run_recorder", str(their_task)) not in refs
    # Kinds with no tenant-bearing owner are withheld, and counted.
    assert not {kind for kind, _ in refs} & {"workflow", "plan"}
    assert manifest["agent_events_withheld"] >= 4


def test_tenant_agent_events_batch_mixed_refs_and_use_pk_index(monkeypatch):
    from unified_agent.tools import export as export_tools

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    their_task = _task("Zirconmixed task a")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    my_task = _task("Zirconmixed task b")
    my_note = _save(title="Zirconmixed note", content="Zirconmixed note b.")
    rows = [
        {"kind": "task", "ref_id": str(my_task)},
        {"kind": "run_recorder", "ref_id": str(my_task)},
        {"kind": "task", "ref_id": str(their_task)},
        {"kind": "task", "ref_id": "not-a-number"},
        {"kind": "task", "ref_id": None},
        {"kind": "memory", "ref_id": my_note["id"]},
        {"kind": "memory", "ref_id": "missing0000"},
        {"kind": "workflow", "ref_id": "nightly"},
    ] + [{"kind": "task", "ref_id": str(10_000 + i)} for i in range(1200)]
    with storage.connect() as conn:
        kept = export_tools._tenant_agent_events(conn, rows)
        plan = " ".join(
            str(r[-1])
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM tasks WHERE org_id IS ? AND id IN (?, ?)",
                ("org_b", 1, 2),
            )
        )
    assert kept == [rows[0], rows[1], rows[5]]
    # An indexed SEARCH on the key (PK or the org_id+rowid covering index), never a SCAN.
    assert plan.startswith("SEARCH tasks") and "SCAN" not in plan
    assert "rowid=?" in plan or "PRIMARY KEY" in plan
