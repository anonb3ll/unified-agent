"""Control Center v2 settings routes: session credentials and the evidence ledger (#2516).

The real scoped app runs behind the hub auth middleware with the fixture identities
of ``test_route_scopes``: Laptop_Agent5 is chief-of-staff, Desktop_Agent2 is a
developer and Desktop_Operator is the verified human operator.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from test_route_scopes import OPERATOR, _bearer, _client
from test_route_scopes import _isolated as _isolated

from unified_agent import profiles, session_credentials, storage
from unified_agent.webui import routes

COS = "Laptop_Agent5"


@pytest.fixture(autouse=True)
def _pin_cos(monkeypatch):
    """Browser stand-in. cloud_agent13 is the only real chief of staff and cannot open /api."""
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, "Laptop_Agent5", profiles.CHIEF_OF_STAFF
    )


@pytest.fixture
def client() -> TestClient:
    return _client(routes.web_routes())


# --- session credentials ---------------------------------------------------------------


def _credential(credential_id: str, *, node: str = "n1", org: str | None = None, **cols) -> None:
    """One credential row, as ``session_credentials.mint`` writes it (no value kept)."""
    conn = storage.connect()
    try:
        session_credentials.ensure_schema(conn)
        row = {
            "credential_id": credential_id,
            "token_sha256": "sha-" + credential_id,
            "agent_label": "Desktop_Agent2",
            "device_agent": "Desktop_Agent2",
            "node": node,
            "item_id": "nd-" + credential_id,
            "attempt": 1,
            "task_id": 7,
            "tools_json": json.dumps(["task_list"]),
            "issued_at": "2026-11-11T00:00:00+00:00",
            "idle_expires_at": "2999-01-01T00:00:00+00:00",
            "expires_at": "2999-01-01T00:00:00+00:00",
            "org_id": org,
            **cols,
        }
        conn.execute(
            f"INSERT INTO session_credentials ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        conn.commit()
    finally:
        conn.close()


def test_session_credential_list_is_the_operators_and_carries_metadata_only(client):
    _credential("a" * 16)
    _credential("b" * 16, revoked_at="2026-11-11T01:00:00+00:00", revoke_reason="operator")
    _credential("c" * 16, expires_at="2000-01-01T00:00:00+00:00")
    _credential("d" * 16, org="org_other")
    res = client.get("/api/session-credentials", headers=_bearer(OPERATOR))
    assert res.status_code == 200, res.text
    assert res.headers["cache-control"] == "no-store"
    [active] = res.json()["credentials"]
    assert active["credential_id"] == "a" * 16 and active["tools"] == ["task_list"]
    assert active["state"] == "active"
    assert "sha-" not in res.text and "token" not in res.text
    every = client.get("/api/session-credentials?state=all", headers=_bearer(OPERATOR)).json()
    assert {c["credential_id"][0]: c["state"] for c in every["credentials"]} == {
        "a": "active",
        "b": "revoked",
        "c": "expired",
    }
    assert (
        client.get("/api/session-credentials?state=x", headers=_bearer(OPERATOR)).status_code == 400
    )
    assert client.get("/api/session-credentials", headers=_bearer(COS)).status_code == 403


def test_keyed_revoke_returns_what_it_revoked_and_replays_that_answer(client):
    _credential("a" * 16)
    _credential("e" * 16, node="n2")
    path = "/api/action/session-credential-revoke"
    headers = {**_bearer(OPERATOR), "Idempotency-Key": "revoke-1"}
    first = client.post(path, headers=headers, json={"node": "n1"})
    assert first.status_code == 200, first.text
    assert first.json()["revoked"] == ["a" * 16] and first.json()["count"] == 1
    [gone] = first.json()["credentials"]
    assert gone["node"] == "n1" and gone["revoke_reason"] == "operator"
    assert "token_sha256" not in gone
    again = client.post(path, headers=headers, json={"node": "n1"})
    assert again.json() == first.json()  # without the key a repeat would revoke nothing
    clash = client.post(path, headers=headers, json={"node": "n2"})
    assert clash.status_code == 409 and clash.json()["code"] == "conflict"
    unkeyed = client.post(path, headers=_bearer(OPERATOR), json={"node": "n1"})
    assert unkeyed.json()["revoked"] == []


# --- evidence ---------------------------------------------------------------------------


def test_evidence_pages_by_keyset_cursor_and_exports_every_record(client, monkeypatch):
    """/api/evidence was one page of at most 500 with no cursor; the export was that
    page too. Pages now follow next_cursor, and the export streams every record."""
    from unified_agent.audit import evidence_report
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_EDITION", "enterprise")
    ids = [tasks.task_add(title=f"evidence {n}")["id"] for n in range(5)]
    seen, cursor = [], None
    while True:
        query = "/api/evidence?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(query, headers=_bearer(COS))
        assert page.status_code == 200, page.text
        seen += [r["task_id"] for r in page.json()["records"]]
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(ids, reverse=True)
    assert client.get("/api/evidence?cursor=x", headers=_bearer(COS)).status_code == 400
    monkeypatch.setattr(evidence_report, "EXPORT_PAGE_SIZE", 2)
    export = client.get("/api/evidence/export?format=csv", headers=_bearer(OPERATOR))
    assert export.status_code == 200, export.text
    assert export.headers["x-evidence-complete"] == "true"
    lines = export.text.strip().splitlines()
    assert lines[0].startswith("record_type,task_id")
    assert [int(line.split(",")[1]) for line in lines[1:]] == sorted(ids, reverse=True)
    pdf = client.get(f"/api/evidence/export?format=pdf&task_id={ids[0]}", headers=_bearer(COS))
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert client.get("/api/evidence/export", headers=_bearer(COS)).status_code == 400
    public = client.get("/api/evidence/export?format=csv", headers=_bearer("server_agent8"))
    assert public.status_code == 403


def test_the_csv_export_survives_resuming_on_another_thread(monkeypatch):
    """Review #1538: StreamingResponse may resume the iterator on another worker; a
    connection held across yields raised sqlite3.ProgrammingError there."""
    import threading

    from unified_agent.audit import evidence_report
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_EDITION", "enterprise")
    for n in range(3):
        tasks.task_add(title=f"resume {n}")
    chunks = evidence_report.iter_evidence_report_csv(page_size=1)
    out = [next(chunks)]
    errors: list[BaseException] = []

    def rest():
        try:
            out.extend(chunks)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=rest)
    worker.start()
    worker.join()
    assert errors == []
    assert len("".join(out).strip().splitlines()) == 4


def test_the_signed_csv_export_is_paged_through_a_file_and_verifies(client, monkeypatch):
    """Review #1538: the signed CSV built every record and the whole body in memory.
    It now pages into a temporary file, signs while writing, and streams the file."""
    import os

    from unified_agent.audit import evidence_report, evidence_signing
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "k" * 32)
    ids = [tasks.task_add(title=f"signed {n}")["id"] for n in range(5)]
    monkeypatch.setattr(evidence_report, "EXPORT_PAGE_SIZE", 2)
    real_build = evidence_report.build_evidence_report
    pages: list[int] = []

    def paged_only(**kwargs):
        assert not kwargs.get("complete"), "the signed export read every record at once"
        assert kwargs.get("limit") == 2
        report = real_build(**kwargs)
        pages.append(len(report["records"]))
        return report

    monkeypatch.setattr(evidence_report, "build_evidence_report", paged_only)
    spooled: list[str] = []
    real_mkstemp = __import__("tempfile").mkstemp

    def track(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        spooled.append(path)
        return fd, path

    monkeypatch.setattr("tempfile.mkstemp", track)
    res = client.get("/api/evidence/export?format=csv", headers=_bearer(OPERATOR))
    assert res.status_code == 200, res.text
    assert pages == [2, 2, 1]
    lines = res.text.strip().splitlines()
    assert [int(line.split(",")[1]) for line in lines[1:]] == sorted(ids, reverse=True)
    sidecar = {
        "schema": evidence_signing.SIGNATURE_SCHEMA,
        "version": "1",
        "scheme": res.headers["x-evidence-signature-scheme"],
        "key_id": res.headers["x-evidence-signature-key-id"],
        "artifact_sha256": res.headers["x-evidence-signature-sha256"],
        "signature": res.headers["x-evidence-signature"],
    }
    assert evidence_signing.verify_export(res.content, sidecar)[0] is True
    assert spooled and not any(os.path.exists(path) for path in spooled)
