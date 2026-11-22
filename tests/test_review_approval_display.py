"""The human sees the complete bounded attestation before one release decision."""

import json

from review_evidence_fixture import review_evidence
from test_shipping_preflight import approval

from unified_agent import storage
from unified_agent.webui import api


async def test_detail_preserves_all_32_authors_full_statement_head_and_long_references():
    aid = approval()
    conn = storage.connect()
    payload = json.loads(
        conn.execute("SELECT payload FROM approvals WHERE id=?", (aid,)).fetchone()[0]
    )
    review = review_evidence()
    review["authors"] = [
        {
            **review["authors"][0],
            "subject": f"t3:author-{i}",
            "execution_ref": "t3://tasks/" + "segment/" * 30 + str(i),
        }
        for i in range(32)
    ]
    payload["independent_review"] = review
    conn.execute("UPDATE approvals SET payload=? WHERE id=?", (json.dumps(payload), aid))
    conn.commit()
    conn.close()
    detail = await api.approval_detail_payload(aid)
    assert detail["review_display_complete"] is True
    assert detail["payload"]["independent_review"] == review
    assert len(detail["payload"]["independent_review"]["authors"]) == 32


async def test_redacted_review_metadata_is_explicitly_incomplete():
    aid = approval()
    conn = storage.connect()
    payload = json.loads(
        conn.execute("SELECT payload FROM approvals WHERE id=?", (aid,)).fetchone()[0]
    )
    payload["independent_review"]["review_ref"] = "Authorization: Bearer secret-review-credential"
    conn.execute("UPDATE approvals SET payload=? WHERE id=?", (json.dumps(payload), aid))
    conn.commit()
    conn.close()
    detail = await api.approval_detail_payload(aid)
    assert detail["review_display_complete"] is False
    assert "secret-review-credential" not in json.dumps(detail)


async def test_invalid_attestation_is_never_marked_complete():
    aid = approval()
    conn = storage.connect()
    payload = json.loads(
        conn.execute("SELECT payload FROM approvals WHERE id=?", (aid,)).fetchone()[0]
    )
    payload["independent_review"]["statement"] = "approve without attesting"
    conn.execute("UPDATE approvals SET payload=? WHERE id=?", (json.dumps(payload), aid))
    conn.commit()
    conn.close()
    detail = await api.approval_detail_payload(aid)
    assert detail["review_display_complete"] is False
