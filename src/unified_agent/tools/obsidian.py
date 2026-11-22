"""Runtime Obsidian tools backed by the Local REST API community plugin."""

import codecs
import hashlib
import time
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .. import config, profiles
from ..safety import VaultLockedError, resolve_in_vault
from .vault import _LOCK_READ_CEILING, _is_locked, _is_locked_path, note_write_lock

REQUEST_TIMEOUT_S = 20.0
# After a PATCH whose outcome is unknown, keep the note locked this long
# waiting for the plugin's late write to land (Devin, PR #1175).
APPEND_SETTLE_S = 5.0
# Sent, but no answer: the plugin may still commit the write.
_UNKNOWN_OUTCOME = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


def _not_configured() -> dict:
    missing = []
    if not config.obsidian_api_url():
        missing.append("UA_OBSIDIAN_API_URL")
    if not config.obsidian_api_key():
        missing.append("UA_OBSIDIAN_API_KEY")
    return {"configured": False, "reason": "not-configured", "missing": missing}


def _settings() -> tuple[str, str] | None:
    url = config.obsidian_api_url()
    key = config.obsidian_api_key()
    if not url or not key:
        return None
    parsed = None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        is_loopback = host and (host.lower() == "localhost" or ip_address(host).is_loopback)
    except ValueError:
        is_loopback = False
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or not is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ToolError(
            "UA_OBSIDIAN_API_URL must be an http(s) URL targeting localhost or a loopback address; remote endpoints are not supported"
        )
    return url.rstrip("/"), key


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    settings = _settings()
    if settings is None:
        raise ToolError(
            "Obsidian Local REST API is not configured; set UA_OBSIDIAN_API_URL and UA_OBSIDIAN_API_KEY"
        )
    base_url, key = settings
    headers = {"Accept": "application/json", "Authorization": f"Bearer {key}"}
    headers.update(kwargs.pop("headers", {}))
    try:
        with httpx.Client(verify=False, timeout=REQUEST_TIMEOUT_S) as client:
            response = client.request(
                method, f"{base_url}/{path.lstrip('/')}", headers=headers, **kwargs
            )
            if response.status_code == 412:
                # Only obsidian_append sends a precondition (`ifMatch`, #1890).
                raise ToolError(
                    "note changed or locked since check: the plugin refused the write "
                    "(412 Precondition Failed) and nothing was written; re-read the note "
                    "and retry"
                )
            response.raise_for_status()
            return response
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        message = str(exc).replace(key, "[REDACTED]")
        raise ToolError(f"Obsidian request failed: {type(exc).__name__}: {message}") from exc


def _payload(response: httpx.Response):
    if response.status_code == 204:
        return {}
    try:
        return response.json()
    except ValueError:
        return response.text


def _resolve_note(path: str) -> tuple[Path, str]:
    """Resolve once: the vault-boundary-checked file and its vault-relative path."""
    resolved = resolve_in_vault(path, for_write=True)
    root = config.vault_root()
    assert root is not None
    return resolved, resolved.relative_to(root.resolve()).as_posix()


def _vault_path(path: str) -> str:
    return _resolve_note(path)[1]


def obsidian_status() -> dict:
    """Return Local REST API server status, or a clean not-configured result."""
    if _settings() is None:
        return _not_configured()
    payload = _payload(_request("GET", "/"))
    return (
        {"configured": True, **payload}
        if isinstance(payload, dict)
        else {
            "configured": True,
            "response": payload,
        }
    )


def obsidian_open(path: str) -> dict:
    """Open an allowlisted vault note in the Obsidian UI."""
    if _settings() is None:
        return _not_configured()
    if not path or not path.strip():
        raise ToolError("path must not be empty")
    relative = _vault_path(path)
    response = _request("POST", f"/open/{quote(relative, safe='/')}")
    return {"path": relative, "opened": True, "response": _payload(response)}


def obsidian_active_note() -> dict:
    """Return the content and vault-relative path of Obsidian's active note."""
    if _settings() is None:
        return _not_configured()
    response = _request("GET", "/active/")
    return {"path": response.headers.get("Content-Location"), "content": response.text}


def obsidian_search(query: str) -> dict:
    """Search notes through Obsidian's live full-text index."""
    if not query or not query.strip():
        raise ToolError("query must not be empty")
    if _settings() is None:
        return _not_configured()
    payload = _payload(_request("POST", "/search/simple/", params={"query": query}))
    return {"query": query, "results": payload}


def _stamp(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _lock_and_version(note: Path) -> tuple[bool, str]:
    """Lock verdict and the plugin's ``ifMatch`` version, in bounded reads.

    One pass over one open file, so both come from the same bytes. The plugin
    reads the note with Obsidian's ``vault.read`` (UTF-8, invalid bytes
    replaced, one leading U+FEFF dropped) and versions that text as the first 6
    hex chars of its sha256 (markdown-patch 2.0.0 ``versionOf``). A decoding
    mismatch can only cause a spurious 412 refusal, never a write past a lock.
    The note is unbounded, so it is never held whole (Devin, PR #1175).
    """
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with open(note, "rb") as f:
        chunk = f.read(_LOCK_READ_CEILING)
        if _is_locked(chunk.decode("utf-8", "replace")):
            return True, ""
        # Obsidian 1.14.4 Vault.read drops a leading BOM before the plugin hashes.
        chunk = chunk.removeprefix(codecs.BOM_UTF8)
        while chunk:
            digest.update(decoder.decode(chunk).encode())
            chunk = f.read(_LOCK_READ_CEILING)
    digest.update(decoder.decode(b"", final=True).encode())
    return False, digest.hexdigest()[:6]


def obsidian_append(
    path: str,
    content: str,
    target_type: str = "heading",
    target: list[str] | str | None = None,
) -> dict:
    """Append Markdown to an allowlisted note using the plugin's patch API.

    A note marked `locked: true` is refused for every caller, and so is any
    frontmatter patch that targets the `locked` key itself; use
    `vault_write(force=True)` only with human/steward approval to override.

    Concurrency (#1890): the check, the PATCH and a re-check run under the
    per-note advisory lock that vault_write and vault_journal also take, so no
    hub writer can change the note in between. The PATCH carries `ifMatch`, the
    plugin's content-hash version of the bytes the hub checked; if the note
    changed before the plugin read it, the plugin answers 412 and the call is
    refused with "note changed or locked since check". If the lock is present
    after the write, the result carries `locked_after_write: true` and a
    `warning`; the appended text may already be in the note (it is not undone).

    Not fenced: the plugin reads, patches and writes with no version check at
    commit, and Obsidian does not take the hub's lock. An edit made in the
    Obsidian UI (or by sync or another non-hub writer) after the plugin's own
    read is overwritten by the plugin's write, and a lock added that way is then
    lost without detection. A note that does not exist yet is sent without
    `ifMatch` (there is no version to match) but is still held under the note
    lock, so no hub writer can create or lock it during the append.

    A PATCH that was sent but got no answer (read timeout, dropped connection)
    may still be committed. The call then fails with "outcome unknown" and keeps
    the note lock for up to APPEND_SETTLE_S waiting for the note to change. A
    plugin write later than that can still overwrite a lock or edit made in
    between; the hub cannot fence it.
    """
    if profiles.cloud_hosted_caller():
        # The plugin writes the file itself, after the hub's lock check, so the
        # check could go stale and a frontmatter patch could even edit the lock.
        # Cloud callers append through vault_write(mode="append"), which
        # re-checks the lock immediately before its own write (task #1799).
        raise ToolError(
            "obsidian_append is not available to cloud-hosted callers; use "
            'vault_write(path, content, mode="append"), which re-checks the note lock'
        )
    if _settings() is None:
        return _not_configured()
    if not path or not path.strip():
        raise ToolError("path must not be empty")
    if not content:
        raise ToolError("content must not be empty")
    if target_type not in {"heading", "block", "frontmatter"}:
        raise ToolError("target_type must be one of heading, block, frontmatter")
    if target_type != "heading" and target is None:
        raise ToolError(f"target is required for {target_type} patches")
    note, relative = _resolve_note(path)
    target_value = [] if target is None else target
    if target_type == "frontmatter":
        keys = [target_value] if isinstance(target_value, str) else list(target_value)
        if any(isinstance(key, str) and key.strip().lower() == "locked" for key in keys):
            # A frontmatter patch on `locked` would set or clear the guard itself.
            raise ToolError(
                "obsidian_append cannot patch the `locked` frontmatter key; "
                "use vault_write(force=True) with human/steward approval"
            )
    if target_type == "heading" and isinstance(target_value, str):
        target_value = [target_value]
    payload = {
        "operation": "append",
        "targetType": target_type,
        "target": target_value,
        "content": content,
    }
    with note_write_lock(note):
        # Same lock check vault_write applies, for every caller. A note that does
        # not exist yet is not locked, so first creation behaves as before.
        if note.is_file():
            try:
                locked, version = _lock_and_version(note)
            except OSError as exc:
                raise ToolError(f"cannot read note {path!r} to check its lock: {exc}") from exc
            if locked:
                raise VaultLockedError(
                    f"note {path!r} is locked (frontmatter `locked: true`); use "
                    "vault_write(force=True) only with human/steward approval to override"
                )
            payload["ifMatch"] = version
        before = _stamp(note)
        try:
            response = _request("PATCH", f"/vault/{quote(relative, safe='/')}", json=payload)
        except ToolError as exc:
            if not isinstance(exc.__cause__, _UNKNOWN_OUTCOME):
                raise
            # Still under the note lock: a hub writer cannot lock the note before
            # a late plugin write that lands within the settle window.
            deadline = time.monotonic() + APPEND_SETTLE_S
            while _stamp(note) == before and time.monotonic() < deadline:
                time.sleep(0.05)
            if _stamp(note) != before:
                detail = "the note changed since, so the append has probably landed; re-read it"
            else:
                detail = (
                    f"the note did not change within {APPEND_SETTLE_S:g}s; the plugin may still "
                    "write the append after this call returns, over any lock or edit made in "
                    "between; re-read the note and its lock before retrying"
                )
            raise ToolError(f"obsidian_append outcome unknown ({exc}): {detail}") from exc
        result = {"path": relative, "response": _payload(response)}
        # ifMatch does not cover a non-hub edit after the plugin's own read.
        # Re-read the lock so one that survived the write is reported. The
        # append has landed, so an unreadable note is a warning, not an error
        # that would invite a duplicate retry.
        try:
            locked_after = _is_locked_path(note)
        except ToolError as exc:
            result["warning"] = f"the append landed but the hub could not re-check the lock: {exc}"
            return result
    if locked_after:
        result["locked_after_write"] = True
        result["warning"] = (
            f"note {path!r} became locked while the append was in flight; the "
            "appended text may already be in it and was not undone. Review the "
            "note with the steward."
        )
    return result
