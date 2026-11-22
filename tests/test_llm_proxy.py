"""Local harness proxy reads fresh device OAuth tokens for each call."""

import io
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from unified_agent import device_login, llm_proxy


def test_device_hub_opener_ignores_environment_proxies(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "getproxies", lambda: {"https": "http://untrusted-proxy.invalid:8080"}
    )
    opener = device_login._direct_opener()
    handlers = [
        handler for handler in opener.handlers if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert not handlers


def test_bad_proxy_port_is_rejected_before_device_sign_in(monkeypatch, capsys):
    monkeypatch.setattr(
        device_login,
        "login",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("login started")),
    )
    assert (
        device_login.main(
            [
                "--hub",
                "http://desktop.ts.net:8765",
                "--configure",
                "opencode",
                "--model",
                "allowed/model",
                "--proxy-port",
                "0",
            ]
        )
        == 1
    )
    assert "invalid proxy port" in capsys.readouterr().err


def test_configure_emits_private_local_credential(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    path = llm_proxy.configure("opencode", hub="http://desktop.ts.net:8765", model="allowed/model")
    data = json.loads(path.read_text())
    key = json.loads((path.parent / "local-key.json").read_text())["key"]
    provider = data["provider"]["unified-agent"]
    assert provider["options"]["apiKey"] == key
    assert provider["options"]["baseURL"] == "http://127.0.0.1:9127/v1"
    assert path.stat().st_mode & 0o777 == 0o600
    assert (path.parent / "local-key.json").stat().st_mode & 0o777 == 0o600
    other = llm_proxy.configure(
        "openai-compatible", hub="http://desktop.ts.net:8765", model="allowed/model"
    )
    assert json.loads(other.read_text())["api_key"] == key
    assert json.loads((path.parent / "local-key.json").read_text())["key"] == key


def test_configurations_for_two_hubs_keep_independent_proxy_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    first = llm_proxy.configure(
        "openai-compatible", hub="http://first.ts.net:8765", model="allowed/model", port=9127
    )
    second = llm_proxy.configure(
        "openai-compatible", hub="http://second.ts.net:8765", model="allowed/model", port=9128
    )
    assert first != second
    assert first.parent != second.parent
    assert json.loads(first.read_text())["api_key"] != json.loads(second.read_text())["api_key"]
    assert json.loads((first.parent / "local-key.json").read_text())["port"] == 9127
    assert json.loads((second.parent / "local-key.json").read_text())["port"] == 9128

    class Server:
        def __init__(self, address, handler):
            self.address = address
            self.handler = handler

        def serve_forever(self):
            return None

        def server_close(self):
            return None

    monkeypatch.setattr(llm_proxy, "ThreadingHTTPServer", Server)
    assert llm_proxy.main(["--hub", "http://first.ts.net:8765", "--port", "9127"]) == 0
    assert llm_proxy.main(["--hub", "http://second.ts.net:8765", "--port", "9128"]) == 0


def test_matching_legacy_proxy_key_is_migrated_without_rotation(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    hub = "http://desktop.ts.net:8765"
    old = llm_proxy.config_dir() / "local-key.json"
    device_login._write_private(old, {"key": "legacy-key", "hub": hub, "port": 9127})
    assert llm_proxy._saved_key(hub, 9128) is None
    saved = llm_proxy._saved_key(hub, 9127)
    assert saved["key"] == "legacy-key"
    migrated = llm_proxy._endpoint_dir(hub, 9127) / "local-key.json"
    assert json.loads(migrated.read_text())["key"] == "legacy-key"
    snippet = llm_proxy.configure("openai-compatible", hub=hub, model="allowed/model")
    assert json.loads(snippet.read_text())["api_key"] == "legacy-key"


def test_proxy_refuses_unauthorized_client_and_refreshes_each_call(monkeypatch, tmp_path):
    tokens = iter(("fresh-1", "fresh-2", "fresh-3"))
    forwarded = []

    class Upstream(io.BytesIO):
        status = 200
        headers = {"Content-Type": "application/json", "X-UA-Broker-Audit": "result-unavailable"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        llm_proxy.device_login, "stored_access_token", lambda *_a, **_k: next(tokens)
    )

    def open_hub(req, *, timeout):
        assert timeout == 120
        forwarded.append((req.full_url, req.headers["Authorization"], req.data))
        return Upstream(b'{"choices":[]}')

    monkeypatch.setattr(llm_proxy.device_login, "open_no_redirect", open_hub)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        llm_proxy._handler("http://desktop.ts.net:8765", tmp_path / "token.json", "local-key"),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=b"{}"), timeout=5)
            raise AssertionError("local authorization required")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        cross_origin = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Authorization": "Bearer local-key", "Origin": "https://untrusted.invalid"},
        )
        try:
            urllib.request.urlopen(cross_origin, timeout=5)
            raise AssertionError("browser origin must not spend a device token")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        uppercase_host = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "Authorization": "Bearer local-key",
                "Host": f"LOCALHOST:{server.server_port}",
            },
        )
        with urllib.request.urlopen(uppercase_host, timeout=5) as response:
            assert response.read() == b'{"choices":[]}'
        for _ in range(2):
            req = urllib.request.Request(
                url, data=b"{}", headers={"Authorization": "Bearer local-key"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.read() == b'{"choices":[]}'
                assert response.headers["X-UA-Broker-Audit"] == "result-unavailable"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert [row[1] for row in forwarded] == ["Bearer fresh-1", "Bearer fresh-2", "Bearer fresh-3"]
    assert all(
        row[0] == "http://desktop.ts.net:8765/api/llm/v1/chat/completions" for row in forwarded
    )


def test_proxy_keeps_done_frame_after_near_limit_stream(monkeypatch, tmp_path):
    body = b"data: " + b"x" * (4 * 1024 * 1024 - 100) + b"\n\ndata: [DONE]\n\n"

    class Upstream(io.BytesIO):
        status = 200
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(llm_proxy.device_login, "stored_access_token", lambda *_a, **_k: "fresh")
    monkeypatch.setattr(
        llm_proxy.device_login, "open_no_redirect", lambda *_a, **_k: Upstream(body)
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        llm_proxy._handler("http://desktop.ts.net:8765", tmp_path / "token.json", "local-key"),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            data=b"{}",
            headers={"Authorization": "Bearer local-key"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            result = response.read()
        assert result == body
        assert result.endswith(b"data: [DONE]\n\n")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_incomplete_upload_times_out_without_forwarding(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_proxy, "_UPLOAD_TIMEOUT", 0.2)
    monkeypatch.setattr(
        llm_proxy.device_login,
        "stored_access_token",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("incomplete body forwarded")),
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        llm_proxy._handler("http://desktop.ts.net:8765", tmp_path / "token.json", "local-key"),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as client:
            client.settimeout(2)
            client.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                + f"Host: 127.0.0.1:{server.server_port}\r\n".encode()
                + b"Authorization: Bearer local-key\r\n"
                b"Content-Length: 10\r\n\r\n{}"
            )
            response = client.recv(4096)
        assert b"408" in response
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_streaming_proxy_reads_available_frames_without_waiting_for_buffer(monkeypatch, tmp_path):
    frames = b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
    reads = []

    class Upstream(io.BytesIO):
        status = 200
        headers = {"Content-Type": "text/event-stream"}

        def read(self, size=-1):
            if size > 1:
                raise AssertionError("buffered read delays SSE frames")
            return super().read(size)

        def read1(self, size=-1):
            reads.append(size)
            return super().read(min(size, 8))

    monkeypatch.setattr(llm_proxy.device_login, "stored_access_token", lambda *_a, **_k: "fresh")
    monkeypatch.setattr(
        llm_proxy.device_login, "open_no_redirect", lambda *_a, **_k: Upstream(frames)
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        llm_proxy._handler("http://desktop.ts.net:8765", tmp_path / "token.json", "local-key"),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            data=b"{}",
            headers={"Authorization": "Bearer local-key"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.read() == frames
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert reads


def test_module_never_touches_the_usage_ledger():
    """Task #1526: the proxy relays to the hub's own broker endpoint, which
    already records one hub-observed row per completed call. This module must
    not import usage_ledger or storage — doing so would risk a second,
    double-counted row for the same call from a process that has no business
    opening hub storage in the first place (it runs on the device)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(llm_proxy))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
    assert "usage_ledger" not in imported
    assert "storage" not in imported
