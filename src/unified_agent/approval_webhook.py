"""Signed webhook for approval requested and decided events.

Off unless both ``UA_APPROVAL_WEBHOOK_URL`` and ``UA_APPROVAL_WEBHOOK_SECRET``
are set. The body is JSON. ``X-UA-Signature-256`` is ``sha256=`` plus the hex
HMAC-SHA256 of those exact bytes, so a client platform can verify the caller
and identify retry duplicates by ``delivery_id``. Callbacks are notifications,
not approval authority: consumers must reconcile the current hub state.

Delivery runs off the approval path. Transport failures, HTTP 429 and 5xx are
retried with backoff. Other 4xx are not. Two delivery workers drain a bounded
queue and coalesce duplicate outstanding delivery ids. Two transport slots
remain occupied until their actual workers exit, even when DNS outlives the
whole-attempt deadline; a stuck resolver cannot create replacement threads.
Queue overflow is best-effort notification loss, never a change to an approval.
The URL, the secret and the signature
are never logged: httpx is not used because it logs request URLs, and error
text is reduced to an exception class. Logs name the host only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from . import approval_push
from .approval_push import push_host

logger = logging.getLogger(__name__)

URL_ENV = "UA_APPROVAL_WEBHOOK_URL"
SECRET_ENV = "UA_APPROVAL_WEBHOOK_SECRET"
SIGNATURE_HEADER = "X-UA-Signature-256"
MAX_ATTEMPTS = 3
BACKOFF_S = (0.5, 2.0)
TIMEOUT_S = 5.0
DEADLINE_S = 10.0
MAX_QUEUED = 32
MAX_WORKERS = 2
MAX_TRANSPORTS = 2

_lock = threading.Lock()
_pending: deque[tuple[str, str, bytes, str, str, str]] = deque()
_outstanding: set[str] = set()
_workers = 0
_overflow_reported = False
_transport_slots = threading.BoundedSemaphore(MAX_TRANSPORTS)
_current = threading.local()

_EVENT_REQUESTED = "approval.requested"
_EVENT_DECIDED = "approval.decided"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would replay the signed body to a different host."""

    def redirect_request(self, *args, **kwargs):
        return None


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    """Reuse the push transport's connection cancellation and remaining budget."""

    def do_open(self, http_class, req, **http_conn_args):
        attempt = getattr(_current, "attempt", None)
        if attempt is None:
            return super().do_open(http_class, req, **http_conn_args)

        def connection(host, **kwargs):
            return attempt.track(http_class(host, **kwargs))

        return super().do_open(connection, req, **http_conn_args)


# Empty ProxyHandler: HTTPS_PROXY must not see the URL or the signature.
_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect, _DeadlineHTTPSHandler()
)


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="approval-webhook", daemon=True).start()


def _drain() -> None:
    global _workers
    while True:
        with _lock:
            if not _pending:
                _workers -= 1
                return
            delivery_id, event, body, url, secret, host = _pending.popleft()
        try:
            _deliver(event, body, url, secret, host)
        except Exception as exc:
            logger.warning("approval webhook worker failed error=%s", type(exc).__name__)
        finally:
            with _lock:
                _outstanding.discard(delivery_id)


def _enqueue(delivery_id: str, event: str, body: bytes, url: str, secret: str, host: str) -> None:
    global _workers, _overflow_reported
    start = False
    overflow = False
    with _lock:
        if delivery_id in _outstanding:
            return
        if len(_pending) >= MAX_QUEUED:
            overflow = not _overflow_reported
            _overflow_reported = True
        else:
            _pending.append((delivery_id, event, body, url, secret, host))
            _outstanding.add(delivery_id)
            _overflow_reported = False
        if _pending and _workers < MAX_WORKERS:
            _workers += 1
            start = True
    if overflow:
        logger.warning("approval webhook queue full; notification dropped")
    if start:
        try:
            _spawn(_drain)
        except BaseException:
            with _lock:
                _workers -= 1
            raise


def _sleep(delay: float) -> None:
    time.sleep(delay)


def _post(url: str, body: bytes, headers: Mapping[str, str]) -> int:
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with _opener.open(req, timeout=TIMEOUT_S) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        return int(exc.code)


def _post_within_deadline(url: str, body: bytes, headers: Mapping[str, str]) -> int:
    # A timed-out DNS lookup cannot be killed. Keep its slot until the worker
    # actually finishes, so retries and new events cannot multiply stuck threads.
    if not _transport_slots.acquire(blocking=False):
        raise TimeoutError("approval webhook transport capacity exhausted")
    slots = _transport_slots
    attempt = approval_push._Attempt(time.monotonic() + DEADLINE_S)
    done = threading.Event()
    result: dict[str, Any] = {}

    def work() -> None:
        _current.attempt = attempt
        try:
            attempt.check()
            result["status"] = _post(url, body, headers)
        except BaseException as exc:
            result["error"] = exc
        finally:
            slots.release()
            done.set()

    try:
        threading.Thread(target=work, name="approval-webhook-send", daemon=True).start()
    except BaseException:
        slots.release()
        raise
    if not done.wait(max(0.0, attempt.remaining())):
        attempt.cancel()
        raise TimeoutError("approval webhook deadline passed")
    if "error" in result:
        raise result["error"]
    return result["status"]


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _configured() -> tuple[str, str, str] | None:
    """``(url, secret, host)`` when delivery is possible. Otherwise None.

    A URL without a secret, or a secret without an https URL, does not send.
    The warning names the missing setting, never the values.
    """
    import os

    url = os.environ.get(URL_ENV, "").strip()
    secret = os.environ.get(SECRET_ENV, "")
    if not url and not secret:
        return None
    host = push_host(url)
    if not url or host is None:
        logger.warning("approval webhook skipped: %s is not an https URL", URL_ENV)
        return None
    if not secret:
        logger.warning("approval webhook skipped: %s is unset", SECRET_ENV)
        return None
    return url, secret, host


def _deliver(event: str, body: bytes, url: str, secret: str, host: str) -> None:
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: _sign(secret, body),
    }
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            status = _post_within_deadline(url, body, headers)
        except Exception as exc:  # noqa: BLE001 - the message may contain the URL
            logger.warning(
                "approval webhook failed event=%s host=%s attempt=%s error=%s",
                event,
                host,
                attempt,
                type(exc).__name__,
            )
            status = None
        else:
            if 200 <= status < 300:
                logger.info(
                    "approval webhook delivered event=%s host=%s attempt=%s",
                    event,
                    host,
                    attempt,
                )
                return
            logger.warning(
                "approval webhook rejected event=%s host=%s attempt=%s status=%s",
                event,
                host,
                attempt,
                status,
            )
            if status != 429 and status < 500:
                return
        if attempt < MAX_ATTEMPTS:
            _sleep(BACKOFF_S[attempt - 1])
    logger.warning("approval webhook gave up event=%s host=%s", event, host)


def _emit(event: str, payload: dict[str, Any]) -> None:
    """Queue one delivery. Never raises and never blocks the approval."""
    try:
        configured = _configured()
        if configured is None:
            return
        url, secret, host = configured
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        _enqueue(str(payload["delivery_id"]), event, body, url, secret, host)
    except Exception as exc:  # noqa: BLE001 - the approval must not notice
        logger.warning("approval webhook not queued event=%s error=%s", event, type(exc).__name__)


def notify_requested(approval_id: int, tool: str, requested_by: str | None) -> None:
    """An approval row was created and is waiting."""
    approval_id = int(approval_id)
    _emit(
        _EVENT_REQUESTED,
        {
            "event": _EVENT_REQUESTED,
            "delivery_id": f"{_EVENT_REQUESTED}:{approval_id}",
            "approval_id": approval_id,
            "tool": tool,
            "status": "pending",
            "requested_by": requested_by,
        },
    )


def notify_decided(
    approval_id: int,
    tool: str,
    status: str,
    decision: str,
    requested_by: str | None,
    resolved_by: str | None,
) -> None:
    """A human approved, denied, or revoked an approval."""
    approval_id = int(approval_id)
    _emit(
        _EVENT_DECIDED,
        {
            "event": _EVENT_DECIDED,
            "delivery_id": f"{_EVENT_DECIDED}:{approval_id}:{status}",
            "approval_id": approval_id,
            "tool": tool,
            "status": status,
            "decision": decision,
            "requested_by": requested_by,
            "resolved_by": resolved_by,
        },
    )
