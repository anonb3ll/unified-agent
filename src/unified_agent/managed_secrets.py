"""Write-only, provider-consumed secrets kept outside all agent workspaces.

Tenancy (task #1343): a secret is keyed by ``(org_id, name)``. With no org the
file lives at ``<root>/<name>`` exactly as before; a tenant's secret lives at
``<root>/orgs/<org_id>/<name>``. A tenant read never falls back to the root or
to another tenant, and an org-agnostic read of a name any tenant holds fails
closed rather than silently sharing one value across tenants.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from mcp.server.mcpserver.exceptions import ToolError

from . import agent_access, config, credential_fence


class SecretReadDenied(ToolError):
    """Managed secret values never have a public read path."""


def _root() -> Path:
    root = (config.data_home() / "managed-secrets").resolve()
    forbidden = [
        config.workspace_root().resolve(),
        *[p.resolve() for p in config.project_repo_roots()],
    ]
    from .safety import get_active_worktree_path

    active_worktree = get_active_worktree_path()
    if active_worktree is not None:
        forbidden.append(active_worktree.resolve())
    vault = config.vault_root()
    if vault:
        forbidden.append(Path(vault).expanduser().resolve())
    if any(root == parent or root.is_relative_to(parent) for parent in forbidden):
        raise ToolError("managed secret root must be outside workspace, project, and vault roots")
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


_TENANTS_DIR = "orgs"
_ENCRYPTION_MARKER = ".encryption-required"
_ORG_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


@contextlib.contextmanager
def _write_lock():
    """Serialize writes and deletes across threads and processes.

    Without it a delete could validate one file and unlink the replacement a
    concurrent rotation just renamed into place.
    """
    lock_path = _root() / ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


#: Secrets named ``agent-token:<label>`` are live hub bearers
#: (``config.agent_token_entries``).
AGENT_TOKEN_PREFIX = "agent-token:"


def _bearer_fence(name: str, consumer: str | None = None):
    """The credential fence for a write or delete that changes an effective bearer.

    Any write or delete of an ``agent-token:`` secret changes (or removes) a
    live bearer, whatever consumer it names, so it must not land between a
    policy publication's final verification and its POST (PR #1302 review).
    Other secrets are not fenced.
    """
    if name.startswith(AGENT_TOKEN_PREFIX) or consumer == "agent_token":
        return credential_fence.authority_fence()
    return contextlib.nullcontext()


def check_root_name(name: str) -> None:
    """Refuse the root-reserved name before any gate runs (it is a tenant dir)."""
    if name == _TENANTS_DIR:
        raise ToolError(f"secret name {_TENANTS_DIR!r} is reserved for tenant scopes")


def _check_org_id(org_id: str) -> str:
    if not isinstance(org_id, str) or not _ORG_ID_RE.fullmatch(org_id):
        raise ToolError("org_id must be 1-64 characters of [A-Za-z0-9_-]")
    return org_id


def _tenant_dir(org_id: str, *, create: bool) -> Path:
    tenants = _root() / _TENANTS_DIR
    path = tenants / _check_org_id(org_id)
    if create:
        for directory in (tenants, path):
            directory.mkdir(exist_ok=True)
            directory.chmod(0o700)
    return path


def _assert_tenant_access(org_id: str | None) -> None:
    """Under an enforced tenant boundary, only the caller's own tenant is reachable.

    Defense in depth below the tool layer: set, load, metadata, and delete all
    refuse another org's scope (and the org-less root) when
    ``edition.tenant_org_id()`` names a tenant.
    """
    from . import edition

    tenant = edition.tenant_org_id()
    if tenant is not None and org_id != tenant:
        raise ToolError("managed secret access is limited to the caller's own org_id")


def _path(name: str, org_id: str | None = None, *, create: bool = False) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ToolError("secret name must be one path component")
    _assert_tenant_access(org_id)
    if org_id is None:
        check_root_name(name)
        return _root() / name
    return _tenant_dir(org_id, create=create) / name


def tenant_orgs_holding(name: str) -> list[str]:
    """Org ids that hold a tenant-scoped secret under ``name`` (never values)."""
    tenants = _root() / _TENANTS_DIR
    if not tenants.is_dir():
        return []
    return sorted(
        entry.name
        for entry in tenants.iterdir()
        if _ORG_ID_RE.fullmatch(entry.name) and (entry / name).is_file()
    )


def _unbound_path(name: str) -> Path:
    """Root path for an org-agnostic read, refused when any tenant holds ``name``."""
    path = _path(name)
    if tenant_orgs_holding(name):
        raise ToolError(
            f"managed secret {name!r} is tenant-scoped; an org-agnostic read is refused "
            "(pass the tenant org_id)"
        )
    return path


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _cipher() -> MultiFernet | None:
    """Use the first configured key for writes and all keys for rotation reads."""
    raw = os.environ.get("UA_MANAGED_SECRET_KEYS", "")
    if not raw:
        return None
    try:
        keys = [Fernet(item.strip().encode()) for item in raw.split(",") if item.strip()]
        if not keys:
            raise ValueError("empty key list")
        return MultiFernet(keys)
    except (ValueError, TypeError) as exc:
        raise ToolError("UA_MANAGED_SECRET_KEYS must contain valid Fernet keys") from exc


def encryption_enabled() -> bool:
    return _cipher() is not None


def _seal(value: str, cipher: MultiFernet | None) -> dict[str, str]:
    if cipher is None:
        return {"value": value}
    return {"encoding": "fernet-v1", "ciphertext": cipher.encrypt(value.encode()).decode()}


def _marker_path() -> Path:
    return _root() / _ENCRYPTION_MARKER


def _check_write_key(path: Path, cipher: MultiFernet | None) -> None:
    marker = _marker_path()
    if marker.exists() or marker.is_symlink():
        _secure_path(marker)
        if cipher is None:
            raise ToolError("managed secret encryption key is required")
    if path.exists() or path.is_symlink():
        if cipher is not None:
            _secure_path(path)
        else:
            old = _read_payload(path)
            if old.get("encoding") == "fernet-v1":
                raise ToolError("managed secret encryption key is required")


def validate_write_configuration(name: str, org_id: str | None = None) -> None:
    """Fail before one-shot approval is consumed; set_secret repeats under lock."""
    path = _path(name, org_id)
    cipher = _cipher()
    with _write_lock():
        _check_write_key(path, cipher)


def _require_encryption_after_first_write() -> None:
    marker = _marker_path()
    if marker.exists() or marker.is_symlink():
        _secure_path(marker)
        return
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("fernet-v1\n")
        handle.flush()
        os.fsync(handle.fileno())


def _unseal(payload: dict[str, object]) -> str:
    if payload.get("encoding") == "fernet-v1":
        cipher = _cipher()
        if cipher is None or not isinstance(payload.get("ciphertext"), str):
            raise ToolError("managed secret encryption key is unavailable")
        try:
            return cipher.decrypt(payload["ciphertext"].encode()).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ToolError("managed secret decryption failed") from exc
    if "encoding" in payload or not isinstance(payload.get("value"), str):
        raise ToolError("managed secret payload is invalid")
    return payload["value"]


def _secure_path(path: Path) -> os.stat_result:
    """Validate the on-disk object before any provider or metadata read."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_mode & 0o777) != 0o600:
        raise ToolError("managed secret file must be a single owner-only regular file (600)")
    return info


def _read_payload(path: Path) -> dict[str, object]:
    _secure_path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    try:
        with os.fdopen(fd, encoding="utf-8") as handle:
            return json.load(handle)
    finally:
        # fdopen closes the descriptor on normal and exceptional exits.
        fd = -1


def set_secret(
    name: str,
    value: str,
    *,
    requested_by: str | None,
    consumer: str = "zulip",
    org_id: str | None = None,
    create_only: bool = False,
    expected_digest: str | None = None,
    operator_agent_token: bool = False,
) -> dict[str, object]:
    """Seal and store one secret; returns its digest metadata, never the value.

    ``operator_agent_token`` (task #2453) admits one write outside managed-secret
    mode: an operator enrolling an agent with ``unified-agent agent add`` on the
    hub host. It applies only to an ``agent-token:`` name for the ``agent_token``
    consumer. The read side (``config.agent_token_entries``) never needed the
    mode, and turning the mode on hub-wide to enroll an agent would block every
    unclassified tool (docs/installation.md), so the hub's own bearer store is
    exempt for this one caller. Every MCP path still checks the mode first in
    ``tools.profiles.managed_secret_payload``.
    """
    if operator_agent_token:
        if not (name.startswith(AGENT_TOKEN_PREFIX) and consumer == "agent_token"):
            raise ToolError("an operator agent-token write must name an agent-token secret")
    elif not agent_access.managed_secret_mode():
        raise ToolError("managed secret mode is disabled")
    if not requested_by:
        raise ToolError("managed secret writes require an authenticated identity")
    if not isinstance(value, str) or not value:
        raise ToolError("managed secret value must be a non-empty string")
    path = _path(name, org_id, create=True)
    root = path.parent
    with _bearer_fence(name, consumer), _write_lock():
        if create_only and path.exists():
            raise ToolError(
                f"managed secret {name!r} already exists; deployment_claim refuses to overwrite existing credentials"
            )
        if expected_digest is not None and path.exists():
            try:
                current_payload = _read_payload(path)
                if current_payload.get("digest") != expected_digest:
                    return {
                        "name": name,
                        "consumer": consumer,
                        "stored": False,
                        "digest": current_payload.get("digest"),
                    }
            except Exception:
                pass
        cipher = _cipher()
        _check_write_key(path, cipher)
        if cipher is not None:
            _require_encryption_after_first_write()
        payload = {"consumer": consumer, "digest": _digest(value), **_seal(value, cipher)}
        fd, temp_name = tempfile.mkstemp(prefix=f".{name}.", dir=root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            path.chmod(0o600)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    result: dict[str, object] = {
        "name": name,
        "consumer": consumer,
        "digest": payload["digest"],
        "stored": True,
    }
    if org_id is not None:
        result["org_id"] = org_id
    return result


def delete_secret(
    name: str, *, org_id: str | None = None, expected_digest: str | None = None
) -> bool:
    """Remove one secret (tenant-scoped when ``org_id`` is set). True if removed.

    Runs under the write lock, so it can never unlink a replacement that a
    concurrent rotation renamed in after the check. With ``expected_digest``
    it removes only that exact value and leaves a newer rotation in place.
    """
    path = _path(name, org_id)
    with _bearer_fence(name), _write_lock():
        try:
            payload = _read_payload(path)
        except FileNotFoundError:
            return False
        if expected_digest is not None and payload.get("digest") != expected_digest:
            return False
        path.unlink()
    return True


def read_secret(name: str) -> str:
    raise SecretReadDenied(f"managed secret {name!r} is write-only")


def _load_for_consumer(name: str, consumer: str, org_id: str | None = None) -> str:
    """Private provider path; public callers must use ``read_secret``."""
    path = _unbound_path(name) if org_id is None else _path(name, org_id)
    try:
        payload = _read_payload(path)
    except FileNotFoundError as exc:
        raise ToolError(f"managed secret {name!r} is not configured") from exc
    if payload.get("consumer") != consumer:
        raise ToolError("managed secret consumer mismatch")
    return _unseal(payload)


def metadata(name: str, org_id: str | None = None) -> dict[str, object]:
    path = _unbound_path(name) if org_id is None else _path(name, org_id)
    try:
        payload = _read_payload(path)
    except FileNotFoundError as exc:
        raise ToolError(f"managed secret {name!r} is not configured") from exc
    result: dict[str, object] = {
        "name": name,
        "consumer": payload.get("consumer"),
        "digest": payload.get("digest"),
    }
    if org_id is not None:
        result["org_id"] = org_id
    return result
