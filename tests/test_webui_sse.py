"""The live activity stream.

The stream is backed by a ULID cursor poll over the durable `event_log`, not by
`events.subscribe()`. These tests pin the properties that choice buys — events from any
process, no replay, no gaps, and a stream that still works with the socket bus down —
because a socket-backed implementation would pass a naive "does it emit?" test while
shipping an empty feed in this deployment.

The generator is driven directly rather than through `TestClient.stream`: an SSE
response never completes, so an HTTP-level read of it hangs the suite instead of
failing it.
"""

import json

import anyio
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, profiles, server, storage, webui
from unified_agent.webui import sse


@pytest.fixture(autouse=True)
def _fast_stream(monkeypatch):
    monkeypatch.setenv("UA_UI_STREAM_POLL_S", "0.02")
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    yield


async def _take(gen, count, timeout=5.0):
    """Pull `count` frames, then close the generator."""
    frames = []
    with anyio.fail_after(timeout):
        try:
            while len(frames) < count:
                frames.append(await gen.__anext__())
        finally:
            await gen.aclose()
    return frames


class TestStream:
    def test_opens_with_a_cursor_frame(self):
        """A client connecting during a quiet hour still gets a resume point."""

        async def scenario():
            frames = await _take(sse.event_stream(), 1)
            assert frames[0]["event"] == "cursor"
            assert "cursor" in json.loads(frames[0]["data"])

        anyio.run(scenario)

    def test_a_new_event_reaches_the_stream(self):
        async def scenario():
            gen = sse.event_stream()
            first = await gen.__anext__()
            assert first["event"] == "cursor"
            # Recorded *after* the stream opened, so it can only arrive by polling.
            await anyio.to_thread.run_sync(
                lambda: events.record_event("agent.task.updated", payload={"summary": "after"})
            )
            with anyio.fail_after(5.0):
                frame = await gen.__anext__()
            await gen.aclose()
            assert frame["event"] == "message"
            assert json.loads(frame["data"])["payload"]["summary"] == "after"

        anyio.run(scenario)

    def test_history_is_not_replayed_to_a_fresh_client(self):
        """Opening the dashboard must not dump every event ever recorded."""
        for i in range(5):
            events.record_event("agent.task.updated", payload={"n": i})

        async def scenario():
            frames = await _take(sse.event_stream(), 1)
            cursor = json.loads(frames[0]["data"])["cursor"]
            assert cursor is not None
            # The opening cursor is the tail of the log: nothing older can follow.
            assert events.query_events(since=cursor, limit=10) == []

        anyio.run(scenario)

    def test_channel_filter_is_honoured(self):
        events.record_event("agent.task.updated", payload={"k": "task"})

        async def scenario():
            gen = sse.event_stream(channels=["hub.auth.*"])
            await gen.__anext__()  # cursor
            await anyio.to_thread.run_sync(
                lambda: events.record_event("agent.task.updated", payload={"k": "ignored"})
            )
            await anyio.to_thread.run_sync(
                lambda: events.record_event("hub.auth.anonymous", payload={"k": "wanted"})
            )
            with anyio.fail_after(5.0):
                frame = await gen.__anext__()
            await gen.aclose()
            assert json.loads(frame["data"])["payload"]["k"] == "wanted"

        anyio.run(scenario)

    def test_cursor_is_exclusive(self):
        first = events.record_event("agent.task.updated", payload={"n": 1})
        events.record_event("agent.task.updated", payload={"n": 2})
        remaining = events.query_events(since=first, limit=10)
        assert [e["payload"]["n"] for e in remaining] == [2], "cursor redelivered an event"

    def test_stream_works_with_the_socket_bus_down(self, monkeypatch):
        """The reason for the cursor design: publish() returns False in this
        deployment today, so a socket-backed stream would ship nothing at all."""
        monkeypatch.setattr(events, "publish", lambda *a, **k: False)
        events.record_event("agent.task.updated", payload={"summary": "still delivered"})
        assert events.query_events(limit=5)[-1]["payload"]["summary"] == "still delivered"


class TestStreamLimits:
    def test_cap_returns_an_error_frame(self, monkeypatch):
        """A browser left open in a dozen tabs must not pile up DB readers."""
        monkeypatch.setenv("UA_UI_MAX_STREAMS", "1")

        async def scenario():
            first = sse.event_stream()
            await first.__anext__()  # occupies the only slot
            try:
                frame = await sse.event_stream().__anext__()
                assert frame["event"] == "error"
                assert "too many" in json.loads(frame["data"])["error"]
            finally:
                await first.aclose()

        anyio.run(scenario)

    def test_slot_is_released_on_disconnect(self, monkeypatch):
        monkeypatch.setenv("UA_UI_MAX_STREAMS", "1")

        async def scenario():
            gen = sse.event_stream()
            await gen.__anext__()
            assert sse.active_streams() == 1
            await gen.aclose()
            assert sse.active_streams() == 0, "a disconnect leaked a stream slot"

        anyio.run(scenario)


class TestStreamRoute:
    def test_route_builds_an_event_source_response(self):
        """Asserted at the handler, not over HTTP: an SSE body never completes, so a
        TestClient read of it hangs the suite rather than failing it."""
        from sse_starlette.sse import EventSourceResponse
        from starlette.requests import Request

        from unified_agent.webui import routes as webui_routes

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/events/stream",
            "headers": [(b"host", b"127.0.0.1")],
            "query_string": b"",
            "ua_identity": "Desktop_Agent1",
        }

        async def scenario():
            response = await webui_routes.events_stream(Request(scope))
            assert isinstance(response, EventSourceResponse)

        anyio.run(scenario)

    @pytest.mark.parametrize("enforce", ["0", "1"])
    @pytest.mark.parametrize(
        "query", [b"", b"since=01J8ZQ4T7X9K2M5N7P8R0S3T6W&channels=agent.approval.*"]
    )
    def test_restricted_identity_cannot_open_live_or_replay_stream(
        self, monkeypatch, query, enforce
    ):
        """Use the real HTTP auth binding; never begin an infinite response."""
        identity = "server_agent8"
        monkeypatch.setenv("UA_AGENT_TOKENS", f"{identity}:fixture-token")
        monkeypatch.setenv("UA_ENFORCE_AUTH", enforce)
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        assert not profiles.tool_is_allowed_for(
            profiles.default_for_identity(identity), "event_log_query"
        )
        events.record_event("agent.approval.requested", payload={"private": "fixture-private"})

        constructed = []

        def forbidden_stream(*args, **kwargs):
            constructed.append(True)

            async def empty_stream():
                if False:
                    yield {}  # finite sentinel: a bypass returns 200 instead of hanging

            return empty_stream()

        monkeypatch.setattr(sse, "event_stream", forbidden_stream)
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get(
                "/api/events/stream?" + query.decode(),
                headers={"Authorization": "Bearer fixture-token"},
            )
        assert response.status_code == 403
        assert constructed == []
        assert response.json() == {"error": "event_log_query is unavailable to this profile"}
        assert "fixture-private" not in response.text
        assert sse.active_streams() == 0

    @pytest.mark.parametrize(
        "identity", ["Desktop_Agent2", "Desktop_Person", "Desktop_Agent8", None]
    )
    def test_authorized_and_warn_mode_preserve_replay_filter_and_live_events(
        self, monkeypatch, identity
    ):
        """Real HTTP auth binding; a test adapter consumes the stream finitely."""
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        from unified_agent.webui import routes as webui_routes

        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        if identity:
            monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
            monkeypatch.setenv("UA_AGENT_TOKENS", f"{identity}:fixture-token")
        first = events.record_event("agent.task.updated", payload={"n": 1})
        events.record_event("agent.task.updated", payload={"n": 2})
        events.record_event("agent.approval.requested", payload={"n": "filtered"})

        async def consume_stream(request):
            # The normal middleware must bind both contexts before the actual
            # route checks the canonical profile. Nothing sets them in this test.
            assert request.scope.get("ua_identity") == identity
            assert config.verified_agent_identity() == identity
            response = await webui_routes.events_stream(request)
            if response.status_code != 200:
                return response
            gen = response.body_iterator
            try:
                with anyio.fail_after(5):
                    opening = await gen.__anext__()
                    replay = await gen.__anext__()
                    events.record_event("agent.task.updated", payload={"n": 3})
                    live = await gen.__anext__()
                return JSONResponse({"frames": [opening, replay, live]})
            finally:
                await gen.aclose()

        app = server.HubAuthASGIMiddleware(
            Starlette(routes=[Route("/api/events/stream", consume_stream)])
        )
        headers = {"Authorization": "Bearer fixture-token"} if identity else {}
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get(
                f"/api/events/stream?since={first}&channels=agent.task.*", headers=headers
            )
        assert response.status_code == 200
        opening, replay, live = response.json()["frames"]
        assert json.loads(opening["data"])["cursor"] == first
        assert json.loads(replay["data"])["payload"] == {"n": 2}
        assert json.loads(live["data"])["payload"] == {"n": 3}
        assert sse.active_streams() == 0

    def test_endpoint_requires_auth_under_enforce(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:tok")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            assert client.get("/api/events/stream").status_code == 401


def _open_stream(query: str, headers: dict[str, str], *, frames: int, extra_wait: float = 0.0):
    """GET the real route and read ``frames`` SSE frames, then close the stream.

    An SSE body never completes, so a finite adapter reads it, as the other route
    tests do. ``extra_wait`` keeps listening that long and records any further
    frame, to prove a replay returned *exactly* the gap and nothing more.
    """
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from unified_agent.webui import routes as webui_routes

    async def consume(request):
        response = await webui_routes.events_stream(request)
        if response.status_code != 200:
            return response
        gen = response.body_iterator
        got: list[dict] = []
        try:
            with anyio.fail_after(5):
                while len(got) < frames:
                    got.append(await gen.__anext__())
            if extra_wait:
                with anyio.move_on_after(extra_wait):
                    got.append(await gen.__anext__())
        finally:
            await gen.aclose()
        return JSONResponse({"frames": got})

    app = server.HubAuthASGIMiddleware(Starlette(routes=[Route("/api/events/stream", consume)]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/events/stream?" + query, headers=headers)
    return response


@pytest.fixture
def _http_hosts(monkeypatch):
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")


def _log_four():
    """Four task events; returns (ulids, seqs) in commit order."""
    ulids = [events.record_event("agent.task.updated", payload={"n": n}) for n in range(1, 5)]
    rows = events.query_events(channels=["agent.task.*"], after_seq=0, limit=10)
    assert [r["ulid"] for r in rows] == ulids
    return ulids, [r["seq"] for r in rows]


def _payload_ns(frames):
    return [json.loads(f["data"])["payload"]["n"] for f in frames if f["event"] == "message"]


@pytest.mark.usefixtures("_http_hosts")
class TestLastEventIdResume:
    """A browser EventSource reconnect sends only ``Last-Event-ID`` (task #1811)."""

    def test_ulid_header_returns_exactly_the_gap(self):
        ulids, seqs = _log_four()
        response = _open_stream(
            "channels=agent.task.*", {"Last-Event-ID": ulids[1]}, frames=3, extra_wait=0.3
        )
        frames = response.json()["frames"]
        assert response.status_code == 200
        assert json.loads(frames[0]["data"])["seq"] == seqs[1]
        assert _payload_ns(frames) == [3, 4], "the gap was lost or an old event was replayed"
        assert len(frames) == 3, "an extra frame arrived after the gap"
        assert [f["id"] for f in frames[1:]] == ulids[2:]
        assert sse.active_streams() == 0

    def test_no_cursor_at_all_still_opens_at_the_tail(self):
        """Fresh clients are unchanged: no cursor opens at the tail, no history replay."""
        _log_four()
        response = _open_stream("channels=agent.task.*", {}, frames=1, extra_wait=0.3)
        frames = response.json()["frames"]
        assert _payload_ns(frames) == []

    def test_seq_header_from_the_opening_frame_resumes_there(self):
        ulids, seqs = _log_four()
        response = _open_stream(
            "channels=agent.task.*", {"Last-Event-ID": f"seq:{seqs[2]}"}, frames=2, extra_wait=0.3
        )
        frames = response.json()["frames"]
        assert _payload_ns(frames) == [4]
        assert len(frames) == 2

    def test_opening_frame_id_is_its_seq_so_a_quiet_reconnect_resumes(self):
        _log_four()
        frames = _open_stream("channels=agent.task.*", {}, frames=1).json()["frames"]
        seq = json.loads(frames[0]["data"])["seq"]
        assert frames[0]["id"] == f"seq:{seq}"
        # The event that lands after that frame is the gap, found from the id alone.
        gap = events.record_event("agent.task.updated", payload={"n": 5})
        resumed = _open_stream(
            "channels=agent.task.*", {"Last-Event-ID": frames[0]["id"]}, frames=2
        ).json()["frames"]
        assert [f["id"] for f in resumed[1:]] == [gap]

    def test_valid_header_beats_a_query_after_seq(self):
        """Regression: a re-opened stream keeps its resume cursor in its URL for life.

        After a later network drop the browser reconnects to that same URL with a
        newer ``Last-Event-ID``. Honouring the stale query cursor replayed everything
        since it ([2, 3, 4] here) and duplicated rows in the panes.
        """
        ulids, seqs = _log_four()
        response = _open_stream(
            f"channels=agent.task.*&after_seq={seqs[0]}",
            {"Last-Event-ID": ulids[2]},
            frames=2,
            extra_wait=0.3,
        )
        assert _payload_ns(response.json()["frames"]) == [4]

    def test_valid_header_beats_a_query_since(self):
        ulids, _ = _log_four()
        response = _open_stream(
            f"channels=agent.task.*&since={ulids[0]}",
            {"Last-Event-ID": ulids[2]},
            frames=2,
            extra_wait=0.3,
        )
        assert _payload_ns(response.json()["frames"]) == [4]

    def test_seq_header_beats_a_query_cursor(self):
        _, seqs = _log_four()
        response = _open_stream(
            f"channels=agent.task.*&after_seq={seqs[0]}",
            {"Last-Event-ID": f"seq:{seqs[2]}"},
            frames=2,
            extra_wait=0.3,
        )
        assert _payload_ns(response.json()["frames"]) == [4]

    def test_query_cursor_is_used_when_there_is_no_header(self):
        """The first connect of a re-opened stream has no header: its URL cursor rules."""
        _, seqs = _log_four()
        response = _open_stream(
            f"channels=agent.task.*&after_seq={seqs[1]}", {}, frames=3, extra_wait=0.3
        )
        assert _payload_ns(response.json()["frames"]) == [3, 4]

    @pytest.mark.parametrize(
        "bad",
        [
            "garbage",
            "01J8ZQ4T7X9K2M5N7P8R0S3T6",
            "seq:",
            "seq:abc",
            "seq:-1",
            "seq:99999999999999999999",
        ],
    )
    def test_malformed_header_is_a_400_not_a_silent_tail_restart(self, bad):
        _log_four()
        response = _open_stream("channels=agent.task.*", {"Last-Event-ID": bad}, frames=1)
        assert response.status_code == 400
        assert "Last-Event-ID" in response.json()["error"]
        assert sse.active_streams() == 0

    def test_malformed_header_is_ignored_when_an_explicit_cursor_is_given(self):
        """A header that cannot be a cursor is ignored when the query supplies one."""
        _, seqs = _log_four()
        response = _open_stream(
            f"channels=agent.task.*&after_seq={seqs[2]}",
            {"Last-Event-ID": "garbage"},
            frames=2,
            extra_wait=0.3,
        )
        assert response.status_code == 200
        assert _payload_ns(response.json()["frames"]) == [4]

    def test_header_parser(self):
        from unified_agent.webui import routes as webui_routes

        ulid = events.new_ulid()
        parse = webui_routes._last_event_id_cursor
        assert parse(None) == (None, None)
        assert parse("  ") == (None, None)
        assert parse(ulid) == (ulid, None)
        assert parse(f" {ulid} ") == (ulid, None)
        assert parse("seq:0") == (None, 0)
        assert parse("seq:0042") == (None, 42)


@pytest.mark.usefixtures("_http_hosts")
class TestCapacityRefusalIsHttp429:
    def test_full_house_is_429_with_retry_after_and_leaks_nothing(self, monkeypatch):
        monkeypatch.setenv("UA_UI_MAX_STREAMS", "1")
        assert sse._acquire_stream()  # another stream holds the only slot
        try:
            response = _open_stream("", {}, frames=1)
            assert sse.active_streams() == 1, "a refusal took or released a slot"
        finally:
            sse._release_stream()
        assert response.status_code == 429
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"error": "too many concurrent streams"}
        assert int(response.headers["retry-after"]) == sse.CAPACITY_RETRY_MS // 1000
        assert sse.active_streams() == 0

    def test_slot_returns_to_baseline_after_a_normal_disconnect(self, monkeypatch):
        monkeypatch.setenv("UA_UI_MAX_STREAMS", "2")
        baseline = sse.active_streams()
        response = _open_stream("", {}, frames=1)
        assert response.status_code == 200
        assert sse.active_streams() == baseline

    @staticmethod
    def _direct_request():
        from starlette.requests import Request

        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/events/stream",
                "headers": [(b"host", b"127.0.0.1")],
                "query_string": b"",
            }
        )

    def test_a_response_that_is_never_consumed_does_not_leak_its_slot(self, monkeypatch):
        """The slot is taken before the body starts, so it must not depend on it."""
        import gc

        from unified_agent.webui import routes as webui_routes

        monkeypatch.setenv("UA_UI_MAX_STREAMS", "1")

        async def scenario():
            response = await webui_routes.events_stream(self._direct_request())
            assert response.status_code == 200
            assert sse.active_streams() == 1
            # The client went away before the first frame: the response ends, and its
            # background task runs although the generator never did.
            await response.background()
            assert sse.active_streams() == 0
            await response.background()  # idempotent: no double release
            assert sse.active_streams() == 0

        anyio.run(scenario)

        async def dropped():
            response = await webui_routes.events_stream(self._direct_request())
            assert sse.active_streams() == 1
            del response

        anyio.run(dropped)
        gc.collect()
        assert sse.active_streams() == 0, "a dropped, never-iterated stream leaked its slot"

    def test_slot_is_released_once_when_the_generator_also_releases(self):
        async def scenario():
            slot = sse.reserve_stream()
            assert slot is not None
            gen = sse.event_stream(after_seq=0, slot=slot)
            await gen.__anext__()
            assert sse.active_streams() == 1
            await gen.aclose()
            assert sse.active_streams() == 0
            other = sse.reserve_stream()
            slot.release()  # a late second release must not free the other holder's slot
            assert sse.active_streams() == 1
            other.release()
            assert sse.active_streams() == 0

        anyio.run(scenario)


class TestLatestUlid:
    def test_empty_log_returns_none(self):
        assert events.latest_ulid() is None

    def test_returns_the_newest(self):
        events.record_event("a.b", payload={})
        last = events.record_event("a.b", payload={})
        assert events.latest_ulid() == last

    def test_respects_the_channel_filter(self):
        events.record_event("agent.task.updated", payload={})
        auth_ulid = events.record_event("hub.auth.anonymous", payload={})
        events.record_event("agent.task.updated", payload={})
        assert events.latest_ulid(["hub.auth.*"]) == auth_ulid
