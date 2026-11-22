"""Phone push when an approval is waiting (task #1988).

Off unless ``UA_APPROVAL_PUSH_URL`` names an ntfy-compatible topic URL. The
approval gate calls :func:`notify` after it commits a new pending row; the
push goes out on a timer thread, so a slow or dead push host never delays or
fails the approval itself.

What is sent is fixed text, "Approval waiting (N pending)", plus a Click link
to the Approvals pane when a Control Center base URL is configured. Nothing
from the approval (tool, payload, requester) is ever sent. The topic URL is a
bearer capability and ``UA_APPROVAL_PUSH_TOKEN`` a credential: neither is
logged, and errors name only the push host.

Coalescing: at most one push per ``COALESCE_S``. A request inside the window
is owed one trailing push when it reopens, which reports the pending count
then and links the newest request still pending. If nothing is pending by
then, it stays quiet.

Only https push URLs are used, and environment proxies are bypassed: the URL
and token must reach the push host and nothing in between.

Bounded (task #2059): ``TIMEOUT_S`` caps each socket operation, and
``DEADLINE_S`` caps how long a send can hold its caller, DNS lookup included. The
transport runs on a daemon worker; at the deadline the attempt counts as failed
and an open connection is shut down. A DNS lookup cannot be interrupted, so a
worker wedged in ``getaddrinfo`` stays blocked until the resolver returns. At most
one send starts per ``COALESCE_S``, which bounds how many such workers pile up
under a wedged resolver. Push timers are daemon threads, and at exit an immediate
push gets at most ``EXIT_WAIT_S`` to finish, so a short-lived CLI never hangs on a
push host.
"""

from __future__ import annotations

import atexit
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import config, edition, storage

logger = logging.getLogger(__name__)

COALESCE_S = 30.0
TIMEOUT_S = 5.0  # each socket operation
DEADLINE_S = 10.0  # the whole send: DNS + connect + write + read
EXIT_WAIT_S = 3.0  # how long interpreter exit waits for an immediate push
_MAX_SEEN = 10_000

_lock = threading.Lock()
_seen: set[int] = set()
_owed = False  # a request is waiting for the next push
_scheduled = False
_last_sent = float("-inf")  # _clock() of the last push attempt
_clock = time.monotonic


_inflight: set[threading.Thread] = set()  # immediate pushes, drained at exit
# Own lock, not _lock: notify() starts timers under _lock, and this one is a leaf.
_inflight_lock = threading.Lock()


def _start_timer(delay: float, fn) -> threading.Timer:
    # Daemon, so a hung push can never keep the interpreter alive. An immediate
    # push is drained at exit for up to EXIT_WAIT_S, so a short-lived CLI that
    # queues an approval and exits still sends it to a responsive push host.
    # ponytail: a delayed trailing push is not drained, so that CLI may drop it
    # rather than wait up to COALESCE_S; the hub process lives long enough.
    def run() -> None:
        try:
            fn()
        finally:
            with _inflight_lock:
                _inflight.discard(threading.current_thread())

    timer = threading.Timer(delay, run)
    timer.daemon = True
    with _inflight_lock:
        # Registered before start() so a fast run() can discard it, and removed
        # again if start() fails: the exit drain must never join an unstarted thread.
        if delay <= 0:
            _inflight.add(timer)
        try:
            timer.start()
        except BaseException:
            _inflight.discard(timer)
            raise
    return timer


@atexit.register
def _drain_at_exit() -> None:
    end = time.monotonic() + EXIT_WAIT_S
    with _inflight_lock:  # timer threads add and discard concurrently
        threads = list(_inflight)
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))


_schedule = _start_timer


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would replay the auth header to wherever a redirect points."""

    def redirect_request(self, *args, **kwargs):
        return None


class _Attempt:
    """One send's deadline, and the connections to tear down when it passes.

    ``cancel`` can only shut down a connection that has a socket. During the DNS
    lookup there is none, so the worker stays blocked in ``getaddrinfo`` until the
    resolver returns. The caller is still released at the deadline, and the
    re-check after ``connect`` stops that worker from sending anything.
    """

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline  # time.monotonic(); the fake test _clock is not real time
        self._lock = threading.Lock()
        self._cancelled = False
        self._conns: list = []

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def check(self) -> None:
        if self._cancelled or self.remaining() <= 0:
            raise TimeoutError("approval push deadline passed")

    def track(self, conn):
        with self._lock:
            self.check()
            self._conns.append(conn)
        connect = conn.connect

        def bounded_connect() -> None:
            self.check()
            conn.timeout = max(0.001, min(conn.timeout or TIMEOUT_S, self.remaining()))
            connect()  # DNS (uninterruptible) + TCP (+ TLS); re-check after
            self.check()

        conn.connect = bounded_connect
        return conn

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            conns = list(self._conns)
        for conn in conns:
            sock = getattr(conn, "sock", None)
            try:
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)  # wakes a recv blocked on the worker
            except OSError:
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


_current = threading.local()  # the worker's _Attempt, read by the HTTPS handler


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    """Register each connection with the running attempt so it can be torn down."""

    def do_open(self, http_class, req, **http_conn_args):
        attempt = getattr(_current, "attempt", None)
        if attempt is None:
            return super().do_open(http_class, req, **http_conn_args)

        def connection(host, **kwargs):
            return attempt.track(http_class(host, **kwargs))

        return super().do_open(connection, req, **http_conn_args)


# stdlib on purpose: httpx logs every request URL at INFO, and the URL is secret.
# The empty ProxyHandler stops HTTPS_PROXY & co. from seeing the URL or token.
_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect, _DeadlineHTTPSHandler()
)


def reset() -> None:
    """Forget de-dupe and coalescing state (tests)."""
    global _owed, _scheduled, _last_sent
    with _lock:
        _seen.clear()
        _owed = False
        _scheduled = False
        _last_sent = float("-inf")


def push_host(url: str | None, *, allow_http: bool = False) -> str | None:
    """Host of a supported, credential-free URL (https, or allowed http). Safe to show."""
    raw = url or ""
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None  # urlsplit strips some raw controls before parsing
    try:
        parts = urlsplit(raw.strip())
        host = parts.hostname
        port = parts.port  # validates numeric syntax and the supported range
    except ValueError:
        return None
    schemes = ("http", "https") if allow_http else ("https",)
    if parts.scheme not in schemes or not host:
        return None
    if (
        "%" in parts.netloc
        or not parts.netloc.isascii()
        or any(char.isspace() for char in parts.netloc)
    ):
        # urllib unquotes authority escapes; http.client requires ASCII without
        # whitespace. Support literal DNS/punycode/IPv6, never a transformed copy.
        return None
    if parts.username is not None or parts.password is not None:
        return None  # urllib retains userinfo in the authority and Host header
    if parts.netloc.endswith(":"):
        return None  # an explicit port must not be empty
    if parts.netloc.startswith("["):
        suffix = parts.netloc.partition("]")[2]
        if suffix and (not suffix.startswith(":") or port is None):
            return None
    return host


def notify(approval_id: int) -> None:
    """Queue a push for a newly pending approval. Never raises, never blocks."""
    global _owed, _scheduled
    try:
        if not config.approval_push_url():
            return
        approval_id = int(approval_id)
        with _lock:
            # A failed trailing schedule leaves debt that an id re-poll may retry.
            if approval_id in _seen and not (_owed and not _scheduled):
                return
            if len(_seen) >= _MAX_SEEN:
                _seen.clear()  # ponytail: ids are monotonic, an old id never recurs
            _seen.add(approval_id)
            owed, _owed = _owed, True
            if _scheduled:
                return
            delay = max(0.0, _last_sent + COALESCE_S - _clock())
            try:
                _schedule(delay, _flush)
            except Exception:
                # No timer runs: forget this id so a re-poll can retry it.
                _seen.discard(approval_id)
                _owed = owed
                raise
            _scheduled = True
    except Exception as exc:  # noqa: BLE001 - the approval must not notice
        logger.warning("approval push not queued (%s)", type(exc).__name__)


def _flush() -> None:
    global _owed, _scheduled
    with _lock:
        owed, _owed = _owed, False
        # Keep the flush active through the pending lookup and transport attempt.
        # Concurrent notifications set _owed without scheduling against an old clock.
    try:
        if owed:
            _send()
    finally:
        with _lock:
            _scheduled = False
            if _owed:
                delay = max(0.0, _last_sent + COALESCE_S - _clock())
                try:
                    _schedule(delay, _flush)
                except Exception as exc:
                    logger.warning("approval push not queued (%s)", type(exc).__name__)
                else:
                    _scheduled = True


def _pending() -> tuple[int, int | None]:
    """(pending count, newest pending id), read together so the link is still pending."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*), MAX(id) FROM approvals WHERE status='pending' AND org_id IS ?",
            (edition.tenant_org_id(),),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]), row[1]


def _base_url() -> str | None:
    """Control Center origin: UA_PUBLIC_URL (as completion_return), else the OAuth issuer."""
    for raw in (
        os.environ.get("UA_PUBLIC_URL", ""),
        os.environ.get("UA_MCP_OAUTH_ISSUER", config.mcp_oauth_issuer() or ""),
    ):
        base = raw.strip().rstrip("/")
        if push_host(raw, allow_http=True) and "?" not in base and "#" not in base:
            return base
    return None


def _post(req: urllib.request.Request) -> int:
    """Open ``req`` and return the HTTP status (runs on the send worker)."""
    try:
        with _opener.open(req, timeout=TIMEOUT_S) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code


def _post_within_deadline(req: urllib.request.Request) -> int:
    """``_post`` on a daemon worker, abandoned and torn down after ``DEADLINE_S``."""
    attempt = _Attempt(time.monotonic() + DEADLINE_S)
    done = threading.Event()
    result: dict = {}

    def work() -> None:
        _current.attempt = attempt
        try:
            result["status"] = _post(req)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            result["error"] = exc
        finally:
            done.set()

    threading.Thread(target=work, name="approval-push-send", daemon=True).start()
    if not done.wait(max(0.0, attempt.remaining())):
        attempt.cancel()
        raise TimeoutError("approval push deadline passed")
    if "error" in result:
        raise result["error"]
    return result["status"]


def _send() -> bool:
    global _last_sent
    url = config.approval_push_url()
    # Validate before the config reader's strip can discard raw controls.
    host = push_host(os.environ.get("UA_APPROVAL_PUSH_URL", url))
    if host is None:
        logger.warning("approval push skipped: UA_APPROVAL_PUSH_URL is not an https URL")
        return False
    try:
        pending, newest = _pending()
        if pending == 0:
            return False  # answered before the window reopened
        headers = {"Title": "Approval waiting"}
        base = _base_url()
        if base:
            headers["Click"] = f"{base}/ui/inbox?kind=approval&item={int(newest)}"
        token = config.approval_push_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = f"Approval waiting ({pending} pending)".encode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with _lock:
            _last_sent = _clock()  # real attempts, including failures, consume the window
        status = _post_within_deadline(req)
    except Exception as exc:  # noqa: BLE001 - redacted: the message may carry the URL
        logger.warning("approval push failed: %s (%s)", host, type(exc).__name__)
        return False
    if status >= 300:
        logger.warning("approval push failed: %s answered HTTP %s", host, status)
        return False
    return True
