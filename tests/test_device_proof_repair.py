"""Device proof replay fencing and first-login key publication (task #2132)."""

import io
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_hosted_device_key as hosted_tests

from unified_agent import device_key, device_login, node_queue, oauth, oauth_approver, storage


@pytest.fixture(autouse=True)
def replay_schema():
    # The release integrator owns storage.py. This exact schema also permits
    # testing this leaf branch before its central migration is integrated.
    # Production-schema integration is checked separately against that pin.
    conn = storage.connect()
    try:
        if os.environ.get("UA_TEST_REQUIRE_REPLAY_SCHEMA") == "1":
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_proof_replays'"
            ).fetchone(), "production replay schema is missing"
            assert conn.execute(
                "SELECT 1 FROM pragma_index_list('device_proof_replays') WHERE name=?",
                ("idx_device_proof_replays_expires",),
            ).fetchone(), "production replay expiry index is missing"
            return
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS device_proof_replays ("
            "device_id TEXT NOT NULL, request_id TEXT NOT NULL, expires_at INTEGER NOT NULL, "
            "PRIMARY KEY(device_id, request_id));"
            "CREATE INDEX IF NOT EXISTS idx_device_proof_replays_expires "
            "ON device_proof_replays(expires_at);"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def material(tmp_path):
    key = device_key.load_or_create(tmp_path / "token.json")
    kwargs = {
        "method": "POST",
        "path": "/api/node/claim",
        "access_token": secrets.token_urlsafe(32),
        "body": b"{}",
    }
    return key, kwargs


def sign(key, kwargs, now=1000):
    return device_key.sign_proof(key, **kwargs, now=now)


def verify(proof, key, kwargs, now=1000):
    device_key.verify_proof(proof, expected_node_id=device_key.thumbprint(key), **kwargs, now=now)


def replay_rows():
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT device_id, request_id, expires_at FROM device_proof_replays"
        ).fetchall()
    finally:
        conn.close()


def test_new_calls_have_distinct_signed_ids_but_replay_is_refused(material):
    key, kwargs = material
    first, second = sign(key, kwargs), sign(key, kwargs)
    assert first != second
    verify(first, key, kwargs)
    with pytest.raises(device_key.DeviceKeyError, match="already used"):
        verify(first, key, kwargs)
    verify(second, key, kwargs)
    assert len(replay_rows()) == 2


def test_concurrent_consumption_has_exactly_one_winner(material):
    key, kwargs = material
    proof = sign(key, kwargs)
    barrier = threading.Barrier(6)

    def consume(_index):
        barrier.wait(timeout=10)
        try:
            verify(proof, key, kwargs)
            return "accepted"
        except device_key.DeviceKeyError:
            return "refused"

    with ThreadPoolExecutor(6) as pool:
        outcomes = list(pool.map(consume, range(6)))
    assert outcomes.count("accepted") == 1
    assert outcomes.count("refused") == 5
    assert len(replay_rows()) == 1


@pytest.mark.parametrize("field", ["method", "path", "access_token", "body"])
def test_request_mutation_does_not_consume_the_valid_proof(material, field):
    key, kwargs = material
    proof = sign(key, kwargs)
    changed = dict(kwargs)
    changed[field] = b"changed" if field == "body" else str(kwargs[field]) + "?changed"
    with pytest.raises(device_key.DeviceKeyError):
        verify(proof, key, changed)
    assert replay_rows() == []
    verify(proof, key, kwargs)


def test_bad_signature_does_not_consume_the_request_id(material):
    key, kwargs = material
    proof = sign(key, kwargs)
    payload, encoded_signature = proof.split(".")
    signature = device_key._b64url_decode(encoded_signature)
    bad = bytes([signature[0] ^ 1]) + signature[1:]
    with pytest.raises(device_key.DeviceKeyError):
        verify(payload + "." + device_key._b64url(bad), key, kwargs)
    assert replay_rows() == []
    verify(proof, key, kwargs)


@pytest.mark.parametrize("request_id", [None, "", "0" * 31, "0" * 33, 12, "G" * 32])
def test_missing_or_malformed_request_ids_are_refused_before_consumption(material, request_id):
    key, kwargs = material
    payload = device_key._payload(key, **kwargs, now=1000)
    if request_id is None:
        payload.pop("request_id")
    else:
        payload["request_id"] = request_id
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = device_key._private_key(key).sign(raw)
    with pytest.raises(device_key.DeviceKeyError):
        verify(device_key._b64url(raw) + "." + device_key._b64url(signature), key, kwargs)
    assert replay_rows() == []


@pytest.mark.parametrize("failure", ["replay", "ambiguous"])
def test_transport_does_not_automatically_resign_or_retry_a_failed_mutation(
    tmp_path, monkeypatch, failure
):
    from unified_agent.node_agent import HubTransport, NodeTransportError

    path = tmp_path / "token.json"
    device_key.load_or_create(path)
    transport = HubTransport(
        hosted_tests.HUB, token_path=path, token_fn=lambda: secrets.token_urlsafe(32)
    )
    requests = []

    def refused(request, timeout):
        requests.append(request)
        if failure == "replay":
            raise urllib.error.HTTPError(
                request.full_url, 403, "refused", {}, io.BytesIO(b'{"error":"already used"}')
            )
        raise urllib.error.URLError("synthetic disconnected after send")

    monkeypatch.setattr(device_login, "open_no_redirect", refused)
    if failure == "replay":
        status, _ = transport("/api/node/claim", {"node": "synthetic-node"}, 1)
        assert status == 403
    else:
        with pytest.raises(NodeTransportError):
            transport("/api/node/claim", {"node": "synthetic-node"}, 1)
    assert len(requests) == 1


def test_consumption_survives_a_fresh_python_process(material):
    key, kwargs = material
    proof = sign(key, kwargs)
    verify(proof, key, kwargs)
    script = """
import json, sys
from unified_agent import device_key
data = json.load(sys.stdin)
data['body'] = bytes.fromhex(data['body'])
try:
    device_key.verify_proof(**data)
except device_key.DeviceKeyError:
    print('refused')
else:
    print('accepted')
"""
    child_input = {
        **kwargs,
        "body": kwargs["body"].hex(),
        "proof": proof,
        "expected_node_id": device_key.thumbprint(key),
        "now": 1000,
    }
    child = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(child_input),
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert child.stdout.strip() == "refused"


@pytest.mark.parametrize("limit_name", ["_MAX_REPLAY_ROWS", "_MAX_DEVICE_REPLAY_ROWS"])
def test_live_records_are_not_evicted_at_capacity_and_expiry_reopens_admission(
    material, monkeypatch, limit_name
):
    key, kwargs = material
    monkeypatch.setattr(device_key, limit_name, 2)
    first, second, third = [sign(key, kwargs) for _ in range(3)]
    verify(first, key, kwargs)
    verify(second, key, kwargs)
    with pytest.raises(device_key.DeviceKeyError, match="capacity"):
        verify(third, key, kwargs)
    assert len(replay_rows()) == 2
    with pytest.raises(device_key.DeviceKeyError, match="already used"):
        verify(first, key, kwargs, now=1120)  # inclusive freshness must retain its replay fence
    verify(sign(key, kwargs, now=1121), key, kwargs, now=1121)
    assert len(replay_rows()) == 1


def test_expiry_cleanup_has_a_fixed_per_request_bound(material, monkeypatch):
    key, kwargs = material
    monkeypatch.setattr(device_key, "_REPLAY_CLEANUP_LIMIT", 2)
    conn = storage.connect()
    try:
        conn.executemany(
            "INSERT INTO device_proof_replays VALUES (?,?,?)",
            [(secrets.token_hex(8), secrets.token_hex(16), 999) for _ in range(10)],
        )
        conn.commit()
    finally:
        conn.close()
    verify(sign(key, kwargs), key, kwargs)
    assert len(replay_rows()) == 9  # exactly two expired deletions, one fresh insertion


def test_missing_replay_store_fails_closed(material):
    key, kwargs = material
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE device_proof_replays")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(device_key.DeviceKeyError, match="unavailable"):
        verify(sign(key, kwargs), key, kwargs)


def test_first_key_publication_loads_the_same_winner_for_concurrent_callers(tmp_path, monkeypatch):
    generate = device_key._generate
    barrier = threading.Barrier(2)

    def together():
        key = generate()
        barrier.wait(timeout=10)
        return key

    monkeypatch.setattr(device_key, "_generate", together)
    path = tmp_path / "new-token.json"
    with ThreadPoolExecutor(2) as pool:
        keys = list(pool.map(device_key.load_or_create, [path, path]))
    assert device_key.thumbprint(keys[0]) == device_key.thumbprint(keys[1])
    stored = device_key._read_private(device_key.key_path(path))
    assert device_key.thumbprint(keys[0]) == device_key.thumbprint(stored)
    assert device_key.key_path(path).stat().st_mode & 0o777 == 0o600


def test_existing_or_damaged_key_is_never_replaced(tmp_path):
    path = tmp_path / "token.json"
    key = device_key.load_or_create(path)
    key_file = device_key.key_path(path)
    original = key_file.read_bytes()
    device_key._write_private(key_file, device_key._generate())
    assert key_file.read_bytes() == original
    assert device_key.thumbprint(device_key.load_or_create(path)) == device_key.thumbprint(key)
    key_file.write_text("damaged")
    device_key._write_private(key_file, device_key._generate())
    assert key_file.read_text() == "damaged"
    with pytest.raises(device_key.DeviceKeyError):
        device_key.load_or_create(path)


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    monkeypatch.setattr(hosted_tests, "AGENT", "review_agent_" + secrets.token_hex(6))
    monkeypatch.setattr(hosted_tests, "STATIC", secrets.token_urlsafe(32))
    monkeypatch.setattr(
        oauth_approver,
        "verify",
        lambda *_args, **_kwargs: oauth_approver.Approval(oauth_approver.VIA_WORKOS, "Operator"),
    )
    oauth.reset_provider()
    with hosted_tests._hub_app(monkeypatch, hosted=True) as client:
        yield client
    oauth.reset_provider()


def test_replayed_claim_does_not_drain_a_second_item(hosted, tmp_path):
    out = tmp_path / "sign-in.json"
    hosted_tests._login(hosted, out)
    saved = json.loads(out.read_text())
    key = device_key.load_or_create(out)

    def request(path, body, proof=None):
        raw = json.dumps(body).encode()
        proof = proof or device_key.sign_proof(
            key, method="POST", path=path, access_token=saved["access_token"], body=raw
        )
        return hosted.post(
            path,
            content=raw,
            headers={
                "Authorization": "Bearer " + saved["access_token"],
                "Content-Type": "application/json",
                device_key.PROOF_HEADER: proof,
            },
        ), proof

    registered, _ = request(
        "/api/node/register", {"node": hosted_tests.NODE, "facts": {"backends": ["codex"]}}
    )
    assert registered.status_code == 200
    items = [
        node_queue.enqueue(
            hosted_tests.NODE,
            prompt="synthetic",
            cwd="/tmp",
            backend="codex",
            created_by=hosted_tests.AGENT,
        )
        for _ in range(2)
    ]
    body = {"node": hosted_tests.NODE, "wait_s": 0}
    first, proof = request("/api/node/claim", body)
    replay, _ = request("/api/node/claim", body, proof=proof)
    assert first.status_code == 200
    assert replay.status_code == 403
    assert "already used" in replay.json()["error"]
    assert node_queue.get_item(items[1]["item_id"])["status"] == "queued"
    # A separate intentional claim gets a fresh signed ID, not a replay retry.
    second, _ = request("/api/node/claim", body)
    assert second.status_code == 200
    assert first.json()["item"]["item_id"] != second.json()["item"]["item_id"]


def test_concurrent_logins_keep_a_usable_key_and_token(hosted, tmp_path, monkeypatch):
    out = tmp_path / "racing-login.json"
    barrier = threading.Barrier(2)
    key_a_saved = threading.Event()
    token_b_saved = threading.Event()
    key_write, token_write = device_key._write_private, device_login._write_private

    def write_key(path, payload):
        barrier.wait(timeout=10)
        if threading.current_thread().name == "login-a":
            key_write(path, payload)
            key_a_saved.set()
        else:
            assert key_a_saved.wait(timeout=10)
            key_write(path, payload)

    def write_token(path, payload):
        if threading.current_thread().name == "login-a":
            assert token_b_saved.wait(timeout=10)
            token_write(path, payload)
        else:
            token_write(path, payload)
            token_b_saved.set()

    monkeypatch.setattr(device_key, "_write_private", write_key)
    monkeypatch.setattr(device_login, "_write_private", write_token)
    results, errors = [], []

    def login():
        try:
            results.append(hosted_tests._login(hosted, out)[0])
        except BaseException as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=login, name=name) for name in ("login-a", "login-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    saved = json.loads(out.read_text())
    key = device_key._read_private(device_key.key_path(out))
    who = hosted.get("/oauth/whoami", headers={"Authorization": "Bearer " + saved["access_token"]})
    assert who.status_code == 200
    assert who.json()["device_node"]["node_id"] == device_key.thumbprint(key)
    raw = b'{"node":"raced-node"}'
    proof = device_key.sign_proof(
        key, method="POST", path="/api/node/register", access_token=saved["access_token"], body=raw
    )
    registered = hosted.post(
        "/api/node/register",
        content=raw,
        headers={
            "Authorization": "Bearer " + saved["access_token"],
            "Content-Type": "application/json",
            device_key.PROOF_HEADER: proof,
        },
    )
    assert registered.status_code == 200
