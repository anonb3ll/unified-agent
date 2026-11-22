"""Server-sent events for the live activity feed.

**Why a cursor poll and not `events.subscribe()`.** The socket bus is the lower-latency
option and it is already written — but no `EventBusServer` has ever run in this
deployment, so `publish()` returns False everywhere and a socket-backed stream would
ship *empty*. That is the exact "green tests, dead feature" failure this codebase has
hit repeatedly. `events.query_events()` reads the durable `event_log`, which is a
superset of anything the socket could fan out: it sees events from every process,
survives the bus being down, and its cursor is exclusive and monotonic so events
are never missed or redelivered.

**The cursor is `seq` (the ROWID), not the ULID** (task #1640). ROWID order is commit
order. ULID order was not: a row could commit behind one carrying a newer ULID, and a
`ulid > since` poll whose cursor had already passed it skipped it permanently. Clients
may still pass a ULID `since` — it is translated to a `seq` once, at connect — or pass
`after_seq` directly. Every frame carries `seq`; resume with the last one seen.

**Resume ids (task #1811).** A browser ``EventSource`` reconnects on its own and
sends the last SSE ``id`` it saw as ``Last-Event-ID``. A ``message`` frame's id is the
event's ULID, and the opening ``cursor`` frame's id is ``seq:<n>`` (its start seq), so
a client that has seen only the opening frame still resumes exactly where it began. The
route maps a valid header onto ``since`` (ULID) or ``after_seq`` (``seq:<n>``), ahead of
any cursor query parameter: the header is the newer position, since a re-opened stream
keeps its cursor in its URL for life; see ``routes.events_stream``.

The cost is up to one poll interval of latency (default 1s), which is imperceptible on
a human dashboard.

The transport is kept swappable: `_event_source()` is an async iterator, so a future
`UA_UI_STREAM_MODE=socket` can substitute a `subscribe()`-backed iterator producing the
identical wire format without touching the route.
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio

from .. import config, events

# Concurrent-stream accounting. Each stream is a repeating DB reader, so a browser
# left open in a dozen tabs must not be able to pile them up unbounded.
_stream_lock = threading.Lock()
_active_streams = 0


def active_streams() -> int:
    with _stream_lock:
        return _active_streams


def _acquire_stream() -> bool:
    global _active_streams
    with _stream_lock:
        if _active_streams >= config.ui_max_streams():
            return False
        _active_streams += 1
        return True


def _release_stream() -> None:
    global _active_streams
    with _stream_lock:
        _active_streams = max(0, _active_streams - 1)


#: How long a refused client should wait before retrying, in ms. Sent as the
#: ``Retry-After`` header (seconds) of the HTTP 429 capacity refusal, and as the SSE
#: ``retry`` field of the in-stream refusal frame that a direct generator caller gets.
CAPACITY_RETRY_MS = 30_000


class StreamSlot:
    """One reserved concurrent-stream slot, released at most once.

    The route reserves it before it returns the response, so a full house is an
    HTTP 429 and not a 200 whose first frame is an error. Three things release
    it, any of which may run first: the generator's ``finally`` (the normal end
    and a cancellation after the first frame), the response's background task (a
    disconnect or an error that ends the response), and a finalizer on the
    generator (an async generator that was never iterated never runs its
    ``finally``). ``release`` is idempotent, so the double call is harmless.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held = True

    def release(self) -> None:
        with self._lock:
            held, self._held = self._held, False
        if held:
            _release_stream()


def reserve_stream() -> StreamSlot | None:
    """Take a stream slot, or None when the concurrent-stream cap is full."""
    return StreamSlot() if _acquire_stream() else None


class StreamCapacityError(RuntimeError):
    """The concurrent-stream cap refused a stream (reported to ``on_open``)."""

    reason_code = "stream_capacity"


def _query(channels: list[str] | None, after_seq: int, limit: int) -> list[dict]:
    return events.query_events(channels=channels, after_seq=after_seq, limit=limit)


async def _event_source(
    channels: list[str] | None,
    after_seq: int,
    on_first_batch: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[dict]:
    """Yield event dicts as they land, in commit order, after `after_seq`.

    ``on_first_batch`` is awaited once, after the first query succeeds.
    """
    cursor = after_seq
    interval = config.ui_stream_poll_s()
    while True:
        batch = await anyio.to_thread.run_sync(_query, channels, cursor, 200)
        if on_first_batch is not None:
            await on_first_batch()
            on_first_batch = None
        for event in batch:
            cursor = event["seq"]
            yield event
        await anyio.sleep(interval)


async def event_stream(
    channels: list[str] | None = None,
    since: str | None = None,
    after_seq: int | None = None,
    on_open: Callable[[BaseException | None], None] | None = None,
    slot: StreamSlot | None = None,
) -> AsyncIterator[dict]:
    """SSE frames for EventSourceResponse: an opening cursor frame, then events.

    The opening frame matters — it gives the client a cursor to resume from even if no
    event ever arrives, so a reconnect after a quiet hour does not replay history. It
    carries both `cursor` (a ULID, for existing clients) and `seq`.

    ``on_open`` (task #1684), when given, is called exactly once with how opening
    ended: ``None`` once the stream holds a slot, resolved its start and ran its
    first replay query; otherwise the exception that stopped it first
    (``StreamCapacityError`` for the concurrent-stream cap, or a startup, replay or
    disconnect error). It runs in a worker thread. The route records its gateway
    decision through it.

    ``slot`` (task #1811): a slot the caller already reserved with ``reserve_stream``.
    The stream then owns it and releases it when it ends, and a full cap is the
    caller's to refuse (HTTP 429). Without one, the stream reserves its own and
    answers a full cap with an in-stream ``error`` frame.
    """
    reported = False

    async def report(error: BaseException | None) -> None:
        # Exactly once, and never on the event loop: ``on_open`` writes the
        # gateway decision to SQLite, which may wait on a busy lock. The scope is
        # shielded so a cancellation (a client disconnect) cannot drop the
        # failure report it caused; the worker thread finishes the write first.
        nonlocal reported
        if reported:
            return
        reported = True
        if on_open is not None:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(on_open, error)

    async def report_open() -> None:
        await report(None)

    if slot is None:
        slot = reserve_stream()
    if slot is None:
        await report(StreamCapacityError("too many concurrent streams"))
        yield {
            "event": "error",
            "data": json.dumps({"error": "too many concurrent streams"}),
            # EventSource reconnects when this stream ends; ask it to wait.
            "retry": CAPACITY_RETRY_MS,
        }
        return

    try:
        try:
            if after_seq is not None:
                start = max(0, int(after_seq))
            elif since is not None:
                start = await anyio.to_thread.run_sync(events.seq_for_ulid_cursor, since)
            else:
                # Start a fresh client at the END of the log, not the top of history:
                # without this, opening the dashboard replays every event ever
                # recorded. Both halves of the opening cursor come from one snapshot:
                # two reads let an event commit in between and fall behind the start.
                since, start = await anyio.to_thread.run_sync(events.tail_cursor, channels)

            yield {
                "event": "cursor",
                # The id makes a reconnect before any event arrives resume at
                # ``start``; without it the client has no Last-Event-ID yet and a
                # reconnect restarts at the tail, dropping whatever landed in between.
                "id": f"seq:{start}",
                "data": json.dumps({"cursor": since, "seq": start}),
            }

            async for event in _event_source(channels, start, on_first_batch=report_open):
                yield {
                    "event": "message",
                    "id": event.get("ulid"),
                    "data": json.dumps(event, default=str),
                }
        except BaseException as exc:
            await report(exc)
            raise
    finally:
        slot.release()
