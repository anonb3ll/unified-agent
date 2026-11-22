"""Application-level audit ledger tamper detection."""

from __future__ import annotations

import json

from unified_agent import events, storage


class TestAuditLedgerTamper:
    """Tamper detection for a chain of recorded events."""

    def test_recorded_event_payload_tampering_breaks_chain(self):
        """A changed event payload invalidates the cryptographic audit chain."""
        events.record_event("example.one", payload={"step": 1})
        events.record_event("example.two", payload={"step": 2})
        events.record_event("example.three", payload={"step": 3})
        e4 = events.record_event("example.four", payload={"step": 4})

        # Initial clean audit verification
        pre_check = events.verify_event_chain()
        assert pre_check["status"] == "valid"
        assert pre_check["verified"] is True
        assert pre_check["total_verified"] >= 4

        # Adversarial tampering: modify one event payload in storage.
        conn = storage.connect()
        try:
            with events.ledger_maintenance(conn):
                conn.execute(
                    "UPDATE event_log SET payload=? WHERE ulid=?",
                    (json.dumps({"step": "tampered"}), e4),
                )
            conn.commit()
        finally:
            conn.close()

        # 3. Post-tampering audit verification detects discrepancy immediately
        post_check = events.verify_event_chain()
        assert post_check["status"] == "tampered"
        assert post_check["verified"] is False
        assert post_check["tampered_at"]["ulid"] == e4
