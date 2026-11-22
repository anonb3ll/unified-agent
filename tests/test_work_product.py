"""Tests for the work-product submit -> approval -> release wedge (task #1278, H2).

Every document, tag, and identity here is synthetic: no client, matter, or
customer data belongs in the fleet's own tests (task boundary).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, platform_readers, profiles, storage
from unified_agent.tools import approvals, work_product
from unified_agent.tools.events import event_log_verify
from unified_agent.tools.work_product_cli import run_work_product_cli

LEGAL_TAGS = {"matter": "M-0001-synthetic", "client": "Example Client LLC"}
A11Y_TAGS = {"wcag_criterion": "1.4.3", "target_system": "synthetic-portal"}
DEVICE = {"node": "reviewer-laptop", "node_id": "nSYNTH1", "user": "reviewer@example"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_WORK_PRODUCT_ENABLED", "1")
    monkeypatch.setenv("UA_WORK_PRODUCT_RELEASE_DIR", str(tmp_path / "released"))
    monkeypatch.delenv("UA_WORK_PRODUCT_TEMPLATE_DIR", raising=False)
    # The reviewer has a bearer, so a work_product_release approval (high
    # evidence tier, task #1672) can carry its signed producer stamp.
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:synthetic-bearer-h")
    storage._connection = None
    storage.init_db()
    token = config.agent_identity_var.set("Desktop_Agent2")
    yield
    config.agent_identity_var.reset(token)
    storage._connection = None


def _doc(tmp_path: Path, name: str = "memo.md", body: str = "# Memo\n\nDraft one.\n") -> Path:
    path = tmp_path / "workspace" / "drafts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _submit(path: Path, *, title: str = "Synthetic memo", pack: str = "legal", tags=None):
    return work_product.work_product_submit(
        str(path), title, pack, tags=LEGAL_TAGS if tags is None else tags, model="model-x"
    )


def _park(path: Path, **kwargs) -> int:
    with pytest.raises(approvals.ApprovalPendingError) as pending:
        _submit(path, **kwargs)
    assert pending.value.approval_id is not None
    return int(pending.value.approval_id)


def _resolve(approval_id: int, decision: str, *, reason: str | None = None, device=DEVICE):
    """Resolve as the human reviewer, from a tailnet device, like the /ui would."""
    identity = config.agent_identity_var.set("Desktop_Person")
    dev = config.peer_device_var.set(device)
    try:
        approvals.approval_resolve(approval_id, decision, reason=reason)
    finally:
        config.peer_device_var.reset(dev)
        config.agent_identity_var.reset(identity)


def _channels() -> list[str]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT channel FROM event_log WHERE channel LIKE 'work_product.%' ORDER BY ulid"
        ).fetchall()
    finally:
        conn.close()
    return [r["channel"] for r in rows]


# --- gating and validation ----------------------------------------------------


def test_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("UA_WORK_PRODUCT_ENABLED")
    with pytest.raises(ToolError, match="UA_WORK_PRODUCT_ENABLED=1"):
        _submit(_doc(tmp_path))


def test_requires_an_identity(monkeypatch, tmp_path):
    monkeypatch.delenv("UA_AGENT_LABEL", raising=False)
    monkeypatch.delenv("AGENT_LABEL", raising=False)
    token = config.agent_identity_var.set(None)
    try:
        with pytest.raises(ToolError, match="authenticated agent identity"):
            _submit(_doc(tmp_path))
    finally:
        config.agent_identity_var.reset(token)


def test_missing_required_tags_rejected_before_anything_is_stored(tmp_path):
    with pytest.raises(ToolError, match=r"requires tags \['client'\]"):
        _submit(_doc(tmp_path), tags={"matter": "M-1"})
    assert work_product.list_work_products()["work_products"] == []


def test_submitter_can_never_approve_its_own_submission(tmp_path):
    assert "approval_resolve" in profiles.DEVELOPER_DENIED_TOOLS
    assert "work_product_submit" in profiles.DEFAULT_PROFILE_PRUNED_TOOLS
    approval_id = _park(_doc(tmp_path))
    # Even a profile that can reach approval_resolve cannot approve its own request.
    with pytest.raises(ToolError, match="separation of duties"):
        approvals.approval_resolve(approval_id, "approve")
    assert approvals.get_approval(approval_id)["status"] == "pending"
    # Withdrawing is never an escalation: the submitter may still deny it.
    approvals.approval_resolve(approval_id, "deny", reason="withdrawn by submitter")
    assert approvals.get_approval(approval_id)["status"] == "denied"


def test_work_products_table_has_hash_and_chain_columns():
    conn = storage.connect()
    try:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(work_products)")}
    finally:
        conn.close()
    assert {
        "content_sha256",
        "producing_agent",
        "producing_model",
        "template_pack",
        "tags",
        "status",
        "previous_version_id",
        "approval_id",
        "released_sha256",
        "resolved_by",
    } <= columns


def test_missing_source_file_and_unknown_pack_rejected(tmp_path):
    with pytest.raises(ToolError, match="not a regular file"):
        _submit(tmp_path / "workspace" / "nope.md")
    with pytest.raises(ToolError, match="unknown template pack"):
        _submit(_doc(tmp_path), pack="no-such-pack")


def test_retry_before_approval_stays_one_row_and_one_approval(tmp_path):
    path = _doc(tmp_path)
    first = _park(path)
    second = _park(path)
    assert first == second
    assert len(work_product.list_work_products()["work_products"]) == 1
    assert len(approvals.approval_pending()["pending"]) == 1
    assert _channels() == ["work_product.submitted"]


def test_source_outside_the_workspace_is_refused(tmp_path):
    outside = tmp_path / "elsewhere" / "secret.md"
    outside.parent.mkdir()
    outside.write_text("not for release\n", encoding="utf-8")
    with pytest.raises(ToolError):
        _submit(outside)
    assert work_product.list_work_products()["work_products"] == []


def test_changed_tags_or_model_start_a_fresh_review(tmp_path):
    path = _doc(tmp_path)
    first = _park(path)
    other_client = _park(path, tags={"matter": "M-0001-synthetic", "client": "Other LLC"})
    assert other_client != first
    with pytest.raises(approvals.ApprovalPendingError) as pending:
        work_product.work_product_submit(
            str(path), "Synthetic memo", "legal", tags=LEGAL_TAGS, model="model-y"
        )
    assert pending.value.approval_id not in {first, other_client}
    rows = work_product.list_work_products()["work_products"]
    assert len(rows) == 3
    # The pass is bound to the reviewed metadata.
    assert approvals.get_approval(other_client)["payload"]["tags"]["client"] == "Other LLC"


# --- template packs -----------------------------------------------------------


def test_two_synthetic_packs_ship_and_are_vertical_neutral():
    packs = {p["id"]: p for p in work_product.list_template_packs()}
    assert set(packs) >= {"legal", "accessibility-remediation"}
    assert packs["legal"]["required_tags"] == ["matter", "client"]
    assert packs["accessibility-remediation"]["reviewer_role"] == "accessibility_lead"
    # The module hardcodes no vertical: no vertical word in any code string
    # literal (docstrings, which explain the design, are excluded).
    tree = ast.parse(Path(work_product.__file__).read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
    }
    literals = [
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    for vertical_word in ("matter", "client", "attorney", "wcag"):
        assert not any(vertical_word in literal for literal in literals), vertical_word


def test_pack_dir_override_and_fail_closed_on_bad_packs(monkeypatch, tmp_path):
    pack_dir = tmp_path / "packs"
    pack_dir.mkdir()
    (pack_dir / "clinical.yaml").write_text(
        "id: clinical\nreviewer_role: attending\nrequired_tags: [record]\n", encoding="utf-8"
    )
    (pack_dir / "mismatch.yaml").write_text("id: other\nreviewer_role: x\n", encoding="utf-8")
    (pack_dir / "noreviewer.yaml").write_text("id: noreviewer\n", encoding="utf-8")
    monkeypatch.setenv("UA_WORK_PRODUCT_TEMPLATE_DIR", str(pack_dir))

    assert [p["id"] for p in work_product.list_template_packs()] == ["clinical"]
    assert work_product.load_template_pack("clinical").release_subfolder == "clinical"
    with pytest.raises(ToolError, match="must match its filename"):
        work_product.load_template_pack("mismatch")
    with pytest.raises(ToolError, match="non-empty reviewer_role"):
        work_product.load_template_pack("noreviewer")
    with pytest.raises(ToolError, match="unknown template pack"):
        work_product.load_template_pack("legal")
    with pytest.raises(ToolError, match="invalid template pack id"):
        work_product.load_template_pack("../etc/passwd")


# --- end to end ---------------------------------------------------------------


def test_end_to_end_submit_approve_release_with_verified_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:synthetic-bearer-h")
    path = _doc(tmp_path)
    approval_id = _park(path)

    row = work_product.list_work_products()["work_products"][0]
    assert row["status"] == "submitted"
    assert row["approval_id"] == approval_id
    pending = approvals.approval_pending()["pending"][0]
    assert pending["tool"] == "work_product_release"

    _resolve(approval_id, "approve")
    released = _submit(path)

    assert released["status"] == "released"
    assert released["resolved_by"] == "Desktop_Person"
    released_path = Path(released["released_path"])
    assert released_path.parent == tmp_path / "released" / "legal"
    assert released_path.read_bytes() == path.read_bytes()
    assert released["released_sha256"] == released["content_sha256"]
    db_path = Path(config.db_path()).resolve()
    assert not db_path.with_name(f".{db_path.name}.work-product-{released['id']}.lock").exists()
    assert db_path.with_name(f".{db_path.name}.work-product-locks.lock").exists()
    assert _channels() == [
        "work_product.submitted",
        "work_product.approved",
        "work_product.released",
    ]

    evidence = work_product.work_product_evidence(released["id"])
    chain = evidence["chain"]
    assert chain["human"] == {
        "identity": "Desktop_Person",
        "verified": True,
        "verified_via": "agent_bearer",
        "producer_event": chain["human"]["producer_event"],
    }
    assert chain["device"] == {"verified": True, "node": "reviewer-laptop", "node_id": "nSYNTH1"}
    assert chain["agent"] == "Desktop_Agent2"
    assert chain["model"] == "model-x"
    assert chain["document_sha256"] == released["content_sha256"]
    assert chain["approval_id"] == approval_id
    assert evidence["ledger"]["chain_valid"] is True
    assert len(evidence["ledger"]["events"]) == 3
    assert event_log_verify()["status"] == "valid"

    # A replay after release is a no-op, not a second copy or event.
    again = _submit(path)
    assert again["note"] == "already released"
    assert _channels().count("work_product.released") == 1


def test_release_approval_refuses_without_a_signed_producer_event(monkeypatch, tmp_path):
    # No UA_AGENT_TOKENS bearer for the resolver -> no signed producer event.
    # work_product_release is high evidence tier (task #1672): before, the
    # release went out with chain.human.verified False; now the approval
    # refuses and the submission stays parked.
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    path = _doc(tmp_path)
    approval_id = _park(path)
    with pytest.raises(ToolError, match="evidence gap"):
        _resolve(approval_id, "approve")
    assert approvals.approval_pending()["pending"][0]["id"] == approval_id
    with pytest.raises(approvals.ApprovalPendingError):
        _submit(path)
    assert not (tmp_path / "released").exists()


def test_release_event_never_records_the_agent_device_as_the_humans(tmp_path):
    path = _doc(tmp_path)
    _resolve(_park(path), "approve")
    agent_device = config.peer_device_var.set({"node": "agent-box", "node_id": "nAGENT"})
    try:
        released = _submit(path)
    finally:
        config.peer_device_var.reset(agent_device)
    conn = storage.connect()
    try:
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM event_log WHERE channel='work_product.released'"
            ).fetchone()["payload"]
        )
    finally:
        conn.close()
    assert "device" not in payload
    assert payload["releasing_device"]["node"] == "agent-box"
    # The human's device is the one that resolved the approval, never the agent's.
    assert work_product.work_product_evidence(released["id"])["chain"]["device"] == {
        "verified": True,
        "node": "reviewer-laptop",
        "node_id": "nSYNTH1",
    }


def test_denied_submission_is_rejected_and_never_released(tmp_path):
    path = _doc(tmp_path)
    approval_id = _park(path)
    _resolve(approval_id, "deny", reason="cites the wrong exhibit")

    with pytest.raises(ToolError, match="cites the wrong exhibit"):
        _submit(path)
    row = work_product.list_work_products()["work_products"][0]
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "cites the wrong exhibit"
    assert not (tmp_path / "released").exists()
    assert _channels() == ["work_product.submitted", "work_product.rejected"]


def test_verify_self_heals_a_denial_nobody_retried(tmp_path):
    path = _doc(tmp_path)
    approval_id = _park(path)
    _resolve(approval_id, "deny", reason="no")
    wp_id = work_product.list_work_products()["work_products"][0]["id"]

    result = work_product.work_product_verify(wp_id)
    assert result["status"] == "rejected"
    work_product.work_product_verify(wp_id)
    assert _channels().count("work_product.rejected") == 1


def test_file_swap_between_submit_and_retry_never_releases_mismatched_content(tmp_path):
    path = _doc(tmp_path)
    first = _park(path)
    _resolve(first, "approve")
    path.write_text("# Memo\n\nSwapped after approval.\n", encoding="utf-8")

    # The swapped bytes are a different submission: a fresh approval, no release.
    second = _park(path)
    assert second != first
    rows = work_product.list_work_products()["work_products"]
    assert [r["status"] for r in rows] == ["submitted", "submitted"]
    assert not (tmp_path / "released").exists()


def test_edit_after_release_is_flagged_as_tampered(tmp_path, capsys):
    path = _doc(tmp_path)
    _resolve(_park(path), "approve")
    released = _submit(path)

    clean = work_product.work_product_verify(released["id"])
    assert clean["tampered"] is False
    assert clean["released_hash_matches"] is True

    released_path = Path(released["released_path"])
    released_path.write_bytes(released_path.read_bytes() + b"x")
    result = work_product.work_product_verify(released["id"])
    assert result["tampered"] is True
    assert result["released_hash_matches"] is False
    assert result["status"] == "tampered"
    assert _channels()[-1] == "work_product.tampered"
    # Idempotent past first detection.
    work_product.work_product_verify(released["id"])
    assert _channels().count("work_product.tampered") == 1

    assert run_work_product_cli(["verify", str(released["id"])]) == 1


def test_editing_the_draft_after_release_is_reported_not_tampering(tmp_path):
    path = _doc(tmp_path)
    _resolve(_park(path), "approve")
    released = _submit(path)
    path.write_text("# Memo\n\nEdited after release.\n", encoding="utf-8")
    result = work_product.work_product_verify(released["id"])
    assert result["source_hash_matches"] is False
    assert result["released_hash_matches"] is True
    assert result["tampered"] is False
    assert result["status"] == "released"

    # Deleting the released record, though, is tampering.
    Path(released["released_path"]).unlink()
    assert work_product.work_product_verify(released["id"])["tampered"] is True


def test_losing_the_draft_before_release_is_tampering(tmp_path):
    path = _doc(tmp_path)
    _park(path)
    wp_id = work_product.list_work_products()["work_products"][0]["id"]
    path.unlink()
    result = work_product.work_product_verify(wp_id)
    assert result["source_exists"] is False
    assert result["tampered"] is True


def test_failed_release_keeps_the_approval_for_the_retry(monkeypatch, tmp_path):
    path = _doc(tmp_path)
    approval_id = _park(path)
    _resolve(approval_id, "approve")

    real_write = work_product._write_release_copy

    def unwritable(*args, **kwargs):
        raise PermissionError("release folder unwritable")

    monkeypatch.setattr(work_product, "_write_release_copy", unwritable)
    with pytest.raises(PermissionError):
        _submit(path)
    assert approvals.get_approval(approval_id)["status"] == "approved"

    monkeypatch.setattr(work_product, "_write_release_copy", real_write)
    released = _submit(path)
    assert released["status"] == "released"
    assert released["approval_id"] == approval_id
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_retry_after_revoked_approval_relinks_product_to_fresh_pass(tmp_path):
    path = _doc(tmp_path)
    first_approval = _park(path)
    _resolve(first_approval, "approve")
    _resolve(first_approval, "revoke")

    second_approval = _park(path)

    row = work_product.list_work_products(limit=10)["work_products"][0]
    assert second_approval != first_approval
    assert row["approval_id"] == second_approval
    assert approvals.get_approval(first_approval)["status"] == "revoked"
    assert approvals.get_approval(second_approval)["status"] == "pending"


def test_concurrent_retry_during_release_cannot_park_an_orphan_approval(monkeypatch, tmp_path):
    """Concurrent identical retries serialize before inspecting the pass."""
    import threading

    path = _doc(tmp_path)
    approval_id = _park(path)
    _resolve(approval_id, "approve")

    entered_write = threading.Event()
    release_write = threading.Event()
    real_write = work_product._write_release_copy
    real_find_or_create = work_product._find_or_create
    both_submissions_found = threading.Barrier(2)

    def synchronized_find_or_create(**kwargs):
        result = real_find_or_create(**kwargs)
        # Both retries now refer to the same open work-product row. The
        # release lock must arbitrate them before either checks or consumes
        # the linked approval.
        both_submissions_found.wait(timeout=5)
        return result

    def blocking_write(dest_path, data, *, expected_sha256):
        entered_write.set()
        assert release_write.wait(timeout=5), "test deadlocked waiting to resume the release"
        return real_write(dest_path, data, expected_sha256=expected_sha256)

    results: list[dict] = [{}, {}]

    def call_submit(index):
        # contextvars are not inherited by a new OS thread; set the same
        # verified identity the main thread's _isolated fixture set.
        token = config.agent_identity_var.set("Desktop_Agent2")
        try:
            results[index]["value"] = _submit(path)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            results[index]["error"] = exc
        finally:
            config.agent_identity_var.reset(token)

    monkeypatch.setattr(work_product, "_find_or_create", synchronized_find_or_create)
    monkeypatch.setattr(work_product, "_write_release_copy", blocking_write)
    threads = [threading.Thread(target=call_submit, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    assert entered_write.wait(timeout=5), "release never reached _write_release_copy"

    # Hold the first release after consuming the pass. The other caller has
    # completed the same row lookup, but must not create another approval.
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    for thread in threads:
        thread.join(timeout=1)
    assert sum(thread.is_alive() for thread in threads) == 1

    release_write.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    completed = [result for result in results if "value" in result]
    refused = [result for result in results if "error" in result]
    assert len(completed) == 1
    assert len(refused) == 1
    assert isinstance(refused[0]["error"], ToolError)
    assert "already in progress" in str(refused[0]["error"])
    released = completed[0]["value"]
    assert released["status"] == "released"

    assert approvals.approval_pending()["pending"] == []
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    assert len(work_product.list_work_products()["work_products"]) == 1
    assert _channels().count("work_product.released") == 1

    # A later retry is the normal, harmless no-op replay.
    monkeypatch.setattr(work_product, "_find_or_create", real_find_or_create)
    again = _submit(path)
    assert again["note"] == "already released"


def test_retry_recovers_consumed_pass_after_release_process_exits(tmp_path):
    """A consumed pass with a free release lock is resumable after a crash."""
    path = _doc(tmp_path)
    approval_id = _park(path)
    _resolve(approval_id, "approve")

    approval_row = approvals.get_approval(approval_id)
    assert approval_row["status"] == "approved"
    approvals.check_approval_gate(
        "work_product_release",
        approval_row["payload"],
        requested_by="Desktop_Agent2",
        force=True,
    )
    assert approvals.get_approval(approval_id)["status"] == "consumed"

    released = _submit(path)

    assert released["status"] == "released"
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    assert approvals.approval_pending()["pending"] == []
    assert _channels().count("work_product.approved") == 1
    assert _channels().count("work_product.released") == 1


def test_failed_release_copy_never_leaves_a_partial_destination(monkeypatch, tmp_path):
    dest = tmp_path / "released.txt"
    data = b"approved bytes"

    def fail_fsync(_fd):
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(work_product.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="interrupted write"):
        work_product._write_release_copy(
            dest, data, expected_sha256=work_product.hashlib.sha256(data).hexdigest()
        )

    assert not dest.exists()
    assert not (tmp_path / ".released.txt.tmp").exists()


def test_release_copy_rejects_unapproved_bytes_before_linking(tmp_path):
    dest = tmp_path / "released.txt"
    data = b"unapproved changed bytes"

    with pytest.raises(ToolError, match="refusing to release bytes"):
        work_product._write_release_copy(
            dest,
            data,
            expected_sha256=work_product.hashlib.sha256(b"approved bytes").hexdigest(),
        )

    assert not dest.exists()
    assert not (tmp_path / ".released.txt.tmp").exists()


def test_release_copy_retry_removes_stale_temporary_file(tmp_path):
    dest = tmp_path / "released.txt"
    temporary = tmp_path / ".released.txt.tmp"
    temporary.write_bytes(b"partial abandoned bytes")
    data = b"approved bytes"

    work_product._write_release_copy(
        dest, data, expected_sha256=work_product.hashlib.sha256(data).hexdigest()
    )

    assert dest.read_bytes() == data
    assert not temporary.exists()


def test_atomic_release_copy_preserves_umask_based_file_permissions(tmp_path):
    dest = tmp_path / "released.txt"
    data = b"approved bytes"
    previous_umask = work_product.os.umask(0o022)
    try:
        work_product._write_release_copy(
            dest, data, expected_sha256=work_product.hashlib.sha256(data).hexdigest()
        )
    finally:
        work_product.os.umask(previous_umask)

    assert dest.stat().st_mode & 0o777 == 0o644


def test_release_row_and_event_commit_together(monkeypatch, tmp_path):
    from unified_agent import events

    path = _doc(tmp_path)
    approval_id = _park(path)
    _resolve(approval_id, "approve")

    real_append = events.record_event_once_on_connection

    def failing_release_event(conn, channel, *args, **kwargs):
        if channel == "work_product.released":
            raise RuntimeError("ledger append failed")
        return real_append(conn, channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event_once_on_connection", failing_release_event)
    with pytest.raises(RuntimeError, match="ledger append failed"):
        _submit(path)
    row = work_product.list_work_products()["work_products"][0]
    assert row["status"] == "approved"
    assert "work_product.released" not in _channels()

    monkeypatch.setattr(events, "record_event_once_on_connection", real_append)
    released = _submit(path)
    assert released["status"] == "released"
    assert _channels().count("work_product.released") == 1


def test_evidence_reads_the_ledger_not_the_mutable_row(tmp_path):
    path = _doc(tmp_path)
    _resolve(_park(path), "approve")
    released = _submit(path)
    assert work_product.work_product_evidence(released["id"])["consistent"] is True

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE work_products SET producing_model='model-forged' WHERE id=?",
            (released["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    evidence = work_product.work_product_evidence(released["id"])
    assert evidence["chain"]["model"] == "model-x"
    assert evidence["consistent"] is False
    assert any("producing_model" in m for m in evidence["mismatches"])
    assert run_work_product_cli(["evidence", str(released["id"])]) == 1


def test_anonymous_approval_of_a_release_is_refused(tmp_path):
    approval_id = _park(_doc(tmp_path))
    token = config.agent_identity_var.set(None)
    try:
        with pytest.raises(ToolError, match="named approver"):
            approvals.approval_resolve(approval_id, "approve")
    finally:
        config.agent_identity_var.reset(token)
    assert approvals.get_approval(approval_id)["status"] == "pending"


def test_release_never_overwrites_an_existing_different_file(tmp_path):
    path = _doc(tmp_path)
    approval_id = _park(path)
    _resolve(approval_id, "approve")
    wp_id = work_product.list_work_products()["work_products"][0]["id"]
    squatter = tmp_path / "released" / "legal" / f"{wp_id}-{path.name}"
    squatter.parent.mkdir(parents=True)
    squatter.write_text("someone else's record\n", encoding="utf-8")

    with pytest.raises(ToolError, match="refusing to overwrite"):
        _submit(path)
    assert squatter.read_text(encoding="utf-8") == "someone else's record\n"
    # Nothing terminal happened, so the approval survives for a retry.
    assert approvals.get_approval(approval_id)["status"] == "approved"


def test_pack_edited_after_approval_cannot_redirect_the_release(monkeypatch, tmp_path):
    path = _doc(tmp_path)
    first = _park(path)
    _resolve(first, "approve")
    pack_dir = tmp_path / "packs"
    pack_dir.mkdir()
    (pack_dir / "legal.yaml").write_text(
        "id: legal\nreviewer_role: x\nrequired_tags: [matter, client]\n"
        "release_subfolder: somewhere-else\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_WORK_PRODUCT_TEMPLATE_DIR", str(pack_dir))
    real_link = work_product._link_approval

    def approve_before_link(work_product_id, approval_id, expected_payload):
        # The reviewer can resolve the new request after the gate commits it
        # but before the submitting call records the work-product link.
        _resolve(approval_id, "approve")
        real_link(work_product_id, approval_id, expected_payload)

    monkeypatch.setattr(work_product, "_link_approval", approve_before_link)
    # The destination is in the approval payload, so the edited pack asks for
    # a fresh approval instead of spending the old one on a new folder. Link
    # the already-approved request even though the old, now-stale approval
    # remains approved.
    second = _park(path)
    assert second != first
    assert approvals.get_approval(first)["status"] == "approved"
    assert approvals.get_approval(second)["status"] == "approved"
    row = work_product.list_work_products()["work_products"][0]
    assert row["approval_id"] == second
    assert not (tmp_path / "released").exists()

    # Model process exit after the replacement pass is consumed but before
    # _release updates the product row. The retry must find and resume it.
    second_row = approvals.get_approval(second)
    with approvals.approval_invocation():
        approvals.check_approval_gate(
            "work_product_release",
            second_row["payload"],
            requested_by="Desktop_Agent2",
            force=True,
        )
    assert approvals.get_approval(second)["status"] == "consumed"

    released = _submit(path)
    assert released["status"] == "released"
    assert released["approval_id"] == second
    assert Path(released["released_path"]).parent.name == "somewhere-else"
    assert approvals.get_approval(first)["status"] == "approved"


def test_platform_read_matches_the_published_schema(tmp_path):
    import jsonschema

    from unified_agent import object_model

    _park(_doc(tmp_path))
    wp_id = work_product.list_work_products()["work_products"][0]["id"]
    jsonschema.validate(
        platform_readers.read_work_product(str(wp_id)),
        object_model.by_kind("work_product").schema,
    )


# --- versions and the reviewer diff --------------------------------------------


def test_new_version_links_previous_and_diffs_for_the_reviewer(tmp_path):
    v1_path = _doc(tmp_path, body="# Memo\n\nDraft one.\n")
    _resolve(_park(v1_path), "approve")
    v1 = _submit(v1_path)

    v2_path = _doc(tmp_path, name="memo-v2.md", body="# Memo\n\nDraft two.\n")
    v2_approval = _park(v2_path)
    v2 = work_product.list_work_products()["work_products"][0]
    assert v2["previous_version_id"] == v1["id"]
    pending = approvals.get_approval(v2_approval)
    assert pending["payload"]["previous_version_id"] == v1["id"]

    diff = work_product.work_product_diff(v2["id"])
    assert diff["diffable"] is True
    assert "-Draft one." in diff["diff"]
    assert "+Draft two." in diff["diff"]

    first = work_product.work_product_diff(v1["id"])
    assert first == {
        "work_product_id": v1["id"],
        "previous_version_id": None,
        "diffable": False,
        "reason": "first version: nothing to diff against",
    }


def test_diff_refuses_binary_and_tampered_versions(tmp_path):
    pdf_v1 = tmp_path / "workspace" / "drafts" / "filing.pdf"
    pdf_v1.parent.mkdir(parents=True)
    pdf_v1.write_bytes(b"%PDF-1.4 synthetic one")
    _park(pdf_v1, title="Filing")
    pdf_v1.write_bytes(b"%PDF-1.4 synthetic two")
    _park(pdf_v1, title="Filing")
    newest = work_product.list_work_products()["work_products"][0]
    assert work_product.work_product_diff(newest["id"])["reason"].startswith("not a text")

    text = _doc(tmp_path, name="note.md", body="one\n")
    _park(text, title="Note")
    text.write_text("two\n", encoding="utf-8")
    _park(text, title="Note")
    # v1's only copy (the source) now hashes to v2's content: never diffed.
    newest = work_product.list_work_products()["work_products"][0]
    assert "fail their hash" in work_product.work_product_diff(newest["id"])["reason"]


# --- surfaces -------------------------------------------------------------------


def test_platform_reader_serves_the_row(tmp_path):
    _park(_doc(tmp_path))
    wp_id = work_product.list_work_products()["work_products"][0]["id"]
    result = platform_readers.read_work_product(str(wp_id))
    assert result["uri"] == f"unified-agent://work-products/{wp_id}"
    assert result["work_product"]["tags"] == LEGAL_TAGS


def test_cli_submit_pending_then_evidence_and_packs(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("UA_AGENT_LABEL", "Desktop_Agent2")
    path = _doc(tmp_path, name="fix.md", body="Contrast raised to 4.5:1.\n")
    argv = ["submit", str(path), "--pack", "accessibility-remediation"]
    for key, value in A11Y_TAGS.items():
        argv += ["--tag", f"{key}={value}"]

    assert run_work_product_cli(argv) == 0
    assert "pending human approval" in capsys.readouterr().out

    wp_id = work_product.list_work_products()["work_products"][0]["id"]
    assert run_work_product_cli(["evidence", str(wp_id)]) == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["template_pack"] == "accessibility-remediation"
    assert evidence["chain"]["human"]["identity"] is None

    assert run_work_product_cli(["packs"]) == 0
    assert "legal" in capsys.readouterr().out
    assert run_work_product_cli(["nope"]) == 2


def test_cli_remote_submit_and_release_use_typed_sdk_without_local_storage(monkeypatch, capsys):
    from types import SimpleNamespace

    from unified_agent.tools import work_product_cli

    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "synthetic-secret")
    monkeypatch.setattr(
        work_product_cli.storage,
        "init_db",
        lambda: (_ for _ in ()).throw(AssertionError("remote CLI opened local DB")),
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert "synthetic-secret" not in argv
        assert argv[0] == "node"
        assert argv[-1] == "UA_AGENT_BEARER_TOKEN"
        assert kwargs["capture_output"] is True
        assert "timeout" not in kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"status": "pending", "approval_id": 18, "work_product_id": 27}
                if argv[2] == "submit"
                else {"id": 27, "status": "released"}
            ),
            stderr="",
        )

    monkeypatch.setattr(work_product_cli, "_sdk_adapter_available", lambda _script: True)
    monkeypatch.setattr(work_product_cli.subprocess, "run", fake_run)
    assert (
        run_work_product_cli(
            [
                "submit",
                "/hub/workspace/drafts/memo.md",
                "--pack",
                "legal",
                "--title",
                "Synthetic memo",
                "--tag",
                "matter=M-1",
                "--tag",
                "client=Example",
                "--hub",
                "http://hub",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["work_product_id"] == 27
    assert json.loads(calls[0][1]["input"]) == {
        "source_path": "/hub/workspace/drafts/memo.md",
        "title": "Synthetic memo",
        "template_pack": "legal",
        "tags": {"matter": "M-1", "client": "Example"},
        "model": None,
    }
    assert run_work_product_cli(["release", "27", "--hub", "http://hub"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "released"
    assert json.loads(calls[1][1]["input"]) == {"work_product_id": 27}


def test_cli_remote_write_requires_bearer_before_sdk_invocation(monkeypatch, capsys):
    from unified_agent.tools import work_product_cli

    monkeypatch.delenv("UA_AGENT_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(
        work_product_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SDK invoked without bearer")
        ),
    )
    assert run_work_product_cli(["release", "27", "--hub", "http://hub"]) == 2
    assert "unset or empty" in capsys.readouterr().err


def test_cli_remote_write_from_installed_wheel_uses_http_fallback(monkeypatch, capsys):
    from unified_agent.tools import work_product_cli

    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "synthetic-secret")
    monkeypatch.setattr(
        work_product_cli,
        "__file__",
        "/tmp/installed/site-packages/unified_agent/tools/work_product_cli.py",
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return work_product_cli.httpx.Response(200, json={"id": 27, "status": "released"})

    monkeypatch.setattr(work_product_cli.httpx, "post", fake_post)
    assert run_work_product_cli(["release", "27", "--hub", "http://hub"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "released"
    assert calls[0][0] == "http://hub/api/work-products/27/release"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer synthetic-secret"}
    assert calls[0][1]["json"] == {}
    assert calls[0][1]["timeout"].read is None
