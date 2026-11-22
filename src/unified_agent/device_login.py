"""`unified-agent login`: sign a device in to the hub like a connector (task #1320).

RFC 8628 device authorization grant against the hub's OAuth server. The device
asks for a short code, the operator approves it on any browser (phone included),
and the device receives a refreshable, device-bound token. No bearer is copied
between machines and nothing secret is printed: the token goes straight to a
0600 file under ``~/.unified_agent/``.

The sign-in also keeps an Ed25519 device key beside that file (mode 0600) and
sends the public key with the grant request. A hub with no tailnet peer — a
hosted hub — binds the token to that key's thumbprint, which the approval page
shows. A resolved tailnet peer still wins, and the key is not the binding.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import device_key

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLI_CLIENT_ID = "unified-agent-cli"
SLOW_DOWN_STEP_S = 5

PostFn = Callable[[str, dict[str, str]], tuple[int, dict[str, Any]]]
GetFn = Callable[[str, str], tuple[int, dict[str, Any]]]
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class LoginError(RuntimeError):
    """The device sign-in did not complete (denied, expired, or hub error)."""


def default_token_path() -> Path:
    """Saved sign-in location. ``UA_OAUTH_TOKEN_PATH`` points consumers (the
    executor's HubClient) at a file written with ``login --out``; otherwise
    ``$UA_HOME/oauth-token.json`` with ``~`` expanded."""
    explicit = os.environ.get("UA_OAUTH_TOKEN_PATH")
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.environ.get("UA_HOME") or "~/.unified_agent").expanduser()
    return home / "oauth-token.json"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects on requests that carry a bearer or refresh token:
    a redirect could deliver the credential to another host (PR #750 review 4)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _direct_opener():
    """Never let environment proxy settings intercept device credentials."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


_OPENER = _direct_opener()


def open_no_redirect(req: urllib.request.Request, *, timeout: float):
    """``urlopen`` that refuses redirects, for any request carrying a credential
    (the ua-node client, task #1327, sends its device token on every call)."""
    return _OPENER.open(req, timeout=timeout)  # noqa: S310 - operator-supplied hub URL


def _http_post(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with _OPENER.open(req, timeout=30) as resp:  # noqa: S310 - operator-supplied hub URL
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except ValueError:
            return err.code, {"error": f"http_{err.code}"}


def _http_get_bearer(url: str, token: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with _OPENER.open(req, timeout=15) as resp:  # noqa: S310 - operator-supplied hub URL
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def hub_origin(url: str) -> str:
    """scheme://host[:port] — how a saved sign-in is matched to a client's hub URL."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def transport_is_protected(url: str) -> bool:
    """HTTPS, or plain HTTP only where the network layer already encrypts or never
    leaves the host: loopback and the Tailscale CGNAT range / MagicDNS names
    (WireGuard). Anything else would expose device codes and refresh tokens."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    if host == "localhost" or host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip in _CGNAT


@contextlib.contextmanager
def _refresh_lock(token_path: Path):
    """Serialize refresh across processes sharing one saved sign-in. Refresh
    tokens rotate, so two concurrent refreshes would strand the loser."""
    lock_path = token_path.with_name(token_path.name + ".lock")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - non-POSIX
            pass
        yield
    finally:
        os.close(fd)


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    """Atomic 0600 write. mkstemp creates a fresh, unpredictable name with
    O_EXCL, so a pre-planted file or symlink can never receive the tokens."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def login(
    hub: str,
    *,
    out_path: Path | None = None,
    client_id: str = CLI_CLIENT_ID,
    post: PostFn = _http_post,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
    agent_hint: str | None = None,
    get: GetFn = _http_get_bearer,
    allow_insecure: bool = False,
) -> dict[str, Any]:
    """Run the device flow and save the token. Returns non-secret metadata."""
    base = hub.rstrip("/")
    if not allow_insecure and not transport_is_protected(base):
        raise LoginError(
            "refusing to sign in over plain HTTP to a non-tailnet host; "
            "use https:// (or --allow-insecure for a trusted lab network)"
        )
    out = out_path or default_token_path()
    try:
        signing_key = device_key.load_or_create(out)
    except device_key.DeviceKeyError as err:
        raise LoginError(str(err)) from err
    form_start = {
        "client_id": client_id,
        device_key.FORM_FIELD: device_key.public_jwk_json(signing_key),
    }
    if agent_hint:
        # A suggestion shown pre-filled on the approval page; the approver decides.
        form_start["agent_hint"] = agent_hint

    def _post(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        try:
            return post(url, form)
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise LoginError(f"could not reach the hub at {base}: {err}") from err

    status, start = _post(f"{base}/oauth/device_authorization", form_start)
    if status == 404:
        raise LoginError(
            "the hub's OAuth server is off at this URL (UA_MCP_OAUTH=1 with an HTTPS or "
            "loopback UA_MCP_OAUTH_ISSUER is required); use the issuer URL"
        )
    if status != 200 or "device_code" not in start:
        raise LoginError(f"hub refused device authorization: {start.get('error', status)}")

    echo(f"Device key thumbprint: {device_key.thumbprint(signing_key)}")
    echo(f"To sign this device in, open:\n  {start['verification_uri_complete']}")
    echo(f"and confirm the code  {start['user_code']}")
    if agent_hint:
        echo(f"Approve it as agent {agent_hint}.")

    interval = float(start.get("interval") or 5)
    # Wall-clock deadline from expires_in — slow_down may stretch the sleep
    # interval, so a fixed poll count would report expired_token while the
    # grant is still live.
    deadline = time.monotonic() + float(start.get("expires_in") or 600)
    form = {"grant_type": DEVICE_GRANT, "device_code": start["device_code"], "client_id": client_id}
    while time.monotonic() < deadline:
        sleep(interval)
        status, body = _post(f"{base}/token", form)
        if status == 200 and body.get("access_token"):
            now = int(time.time())
            # Record which agent this sign-in was approved as, so clients only
            # use it for that agent (never to impersonate another label).
            agent = None
            for _attempt in range(3):
                who_status, who = get(f"{base}/oauth/whoami", body["access_token"])
                agent = who.get("agent") if who_status == 200 else None
                if agent:
                    break
                sleep(1)
            if not agent:
                # Never save a credential whose identity is unconfirmed: clients
                # could not safely decide which agent it belongs to.
                raise LoginError(
                    "the device was approved but the hub could not confirm which agent "
                    "it was approved as (/oauth/whoami failed); run login again"
                )
            _write_private(
                out,
                {
                    "hub": base,
                    "agent": agent,
                    "client_id": client_id,
                    "access_token": body["access_token"],
                    "refresh_token": body.get("refresh_token"),
                    "expires_at": now + int(body.get("expires_in") or 0),
                    "scope": body.get("scope"),
                    "obtained_at": now,
                },
            )
            echo(f"Signed in as {agent}. Token saved to {out} (mode 600).")
            return {"hub": base, "path": str(out), "agent_hint": agent_hint, "agent": agent}
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += SLOW_DOWN_STEP_S
            continue
        raise LoginError(f"device sign-in failed: {error or status}")
    raise LoginError("device sign-in failed: expired_token")


def _read_saved(token_path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(token_path.read_text())
    except (OSError, ValueError):
        return None


def _fresh(saved: dict[str, Any], now: float, skew_s: int) -> str | None:
    access = saved.get("access_token")
    if access and int(saved.get("expires_at") or 0) - skew_s > now:
        return access
    return None


def stored_access_token(
    path: Path | None = None,
    *,
    hub: str | None = None,
    agent: str | None = None,
    post: PostFn = _http_post,
    now: Callable[[], float] = time.time,
    skew_s: int = 60,
) -> str | None:
    """Access token from a saved device sign-in, refreshed when near expiry.

    ``hub`` / ``agent``, when given, must match the sign-in (origin and approved
    label); otherwise None, so a client never sends one hub's token to another
    or acts under a label it was not configured for. Returns None on any failure
    — missing file, mismatch, unreachable issuer, refused refresh — so callers
    fall back to their other credential sources. Never logs token values.
    """
    token_path = path or default_token_path()
    saved = _read_saved(token_path)
    if not saved:
        return None
    if hub and hub_origin(saved.get("hub") or "") != hub_origin(hub):
        return None
    if agent and saved.get("agent") != agent:
        return None
    return _usable_token(token_path, saved, post=post, now=now, skew_s=skew_s)


def has_saved_signin(
    path: Path | None = None, *, hub: str | None = None, agent: str | None = None
) -> bool:
    """A saved sign-in exists for this hub (and agent, when recorded) — even if
    its access token is currently expired and the issuer unreachable."""
    saved = _read_saved(path or default_token_path())
    if not saved or not saved.get("refresh_token"):
        return False
    if hub and hub_origin(saved.get("hub") or "") != hub_origin(hub):
        return False
    return not agent or saved.get("agent") == agent


def saved_signin_agent(hub: str, path: Path | None = None) -> str | None:
    """Return only the verified label bound to this hub's saved device sign-in."""
    saved = _read_saved(path or default_token_path())
    if not saved or hub_origin(saved.get("hub") or "") != hub_origin(hub):
        return None
    agent = saved.get("agent")
    return agent if isinstance(agent, str) and agent else None


def _usable_token(
    token_path: Path,
    saved: dict[str, Any],
    *,
    post: PostFn,
    now: Callable[[], float],
    skew_s: int,
) -> str | None:
    fresh = _fresh(saved, now(), skew_s)
    if fresh:
        return fresh
    try:
        with _refresh_lock(token_path):
            # Another process may have rotated it while we waited for the lock.
            saved = _read_saved(token_path) or saved
            fresh = _fresh(saved, now(), skew_s)
            if fresh:
                return fresh
            refresh = saved.get("refresh_token")
            base = saved.get("hub")
            if not refresh or not base:
                return None
            status, body = post(
                f"{base}/token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": saved.get("client_id") or CLI_CLIENT_ID,
                },
            )
            if status != 200 or not body.get("access_token"):
                # Another process may have rotated while we held the lock and
                # our refresh was refused; pick up the winner's fresh token.
                saved = _read_saved(token_path) or saved
                return _fresh(saved, now(), skew_s)
            current = int(now())
            saved.update(
                {
                    "access_token": body["access_token"],
                    "refresh_token": body.get("refresh_token") or refresh,
                    "expires_at": current + int(body.get("expires_in") or 0),
                    "obtained_at": current,
                }
            )
            try:
                _write_private(token_path, saved)
            except OSError:
                # Hub already consumed the old refresh token. Drop the stale
                # file so we never retry the consumed R1; this process still
                # returns the new access token for the current call.
                with contextlib.suppress(OSError):
                    token_path.unlink()
            return body["access_token"]
    except Exception:  # noqa: BLE001 - offline issuer must not break the caller
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent login",
        description="Sign this device in to a unified-agent hub (OAuth device flow).",
    )
    parser.add_argument(
        "--hub",
        default=os.environ.get("UA_HUB_URL", ""),
        help="hub base URL, e.g. https://hub.tailnet.ts.net:8765 (default: $UA_HUB_URL)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="token file (default ~/.unified_agent/oauth-token.json)",
    )
    parser.add_argument(
        "--agent", default=None, help="agent label to approve this device as (shown as a hint)"
    )
    parser.add_argument(
        "--allow-insecure",
        action="store_true",
        help="allow plain HTTP to a non-tailnet host (lab networks only)",
    )
    parser.add_argument(
        "--configure",
        choices=("openai-compatible", "opencode"),
        help="write a private LLM harness config using a refresh-aware local proxy",
    )
    parser.add_argument("--model", help="hub-allowed model for --configure")
    parser.add_argument("--proxy-port", type=int, default=9127)
    args = parser.parse_args(argv)
    if not args.hub:
        parser.error("--hub is required (or set UA_HUB_URL)")
    if args.configure and not args.model:
        parser.error("--model is required with --configure")
    try:
        if args.configure:
            from . import llm_proxy

            llm_proxy.validate_config(
                args.configure, hub=args.hub, model=args.model, port=args.proxy_port
            )
        login(
            args.hub,
            out_path=args.out,
            agent_hint=args.agent,
            allow_insecure=args.allow_insecure,
        )
        if args.configure:
            target = llm_proxy.configure(
                args.configure, hub=args.hub, model=args.model, port=args.proxy_port
            )
            token_arg = f" --token-file {args.out}" if args.out else ""
            print(
                f"Harness config saved to {target} (mode 600). Start "
                f"`unified-agent llm-proxy --hub {hub_origin(args.hub)} "
                f"--port {args.proxy_port}{token_arg}`."
            )
    except (LoginError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
