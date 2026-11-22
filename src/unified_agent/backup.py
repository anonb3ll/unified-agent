"""Complete, local SQLite backups for the unified-agent state database.

The vault mirror is intentionally not involved: it is a human-readable,
partial projection. These snapshots use SQLite's online backup API and retain
the entire database, including plans, cycles, delegations and the event log.

When ``UA_BACKUP_GCS_URI`` is set and the caller opts in with
``upload_to_gcs=True`` (the scheduled runner does), a successful local
snapshot is best-effort copied to that GCS prefix via ``gsutil`` or
``gcloud storage cp``. Local remains the source of truth: GCS failures are
recorded, never raised.

``UA_BACKUP_S3_URI`` (task #2585) is the same best-effort offsite copy to any
S3-compatible store, Cloudflare R2 in the hobby cloud setup (#2584). It needs
no CLI: the upload is one SigV4-signed HTTPS PUT over ``httpx``, so it works
inside the slim hosted container, where no Cloud SDK is installed.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from . import config

if TYPE_CHECKING:  # httpx is imported on use: ops scripts load this module with stdlib only
    import httpx

logger = logging.getLogger("unified_agent.backup")

_BACKUP_RE = re.compile(r"^memory-(\d{8}T\d{6}Z)\.db$")
DAILY_KEEP = 7
WEEKLY_KEEP = 4
_GCS_UPLOAD_TIMEOUT_S = 120
_GCS_SDK_BIN_CANDIDATES = (
    Path.home() / "google-cloud-sdk" / "bin",
    Path("/usr/lib/google-cloud-sdk/bin"),
)


def _resolve_gcs_tool(name: str) -> str | None:
    """Locate gsutil/gcloud on PATH or in documented Cloud SDK install dirs."""
    found = shutil.which(name)
    if found:
        return found
    for directory in _GCS_SDK_BIN_CANDIDATES:
        candidate = directory / name
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _backup_files(directory: Path) -> list[tuple[datetime, Path]]:
    files: list[tuple[datetime, Path]] = []
    if not directory.is_dir():
        return files
    for path in directory.iterdir():
        match = _BACKUP_RE.match(path.name)
        if not match or not path.is_file():
            continue
        try:
            files.append(
                (datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), path)
            )
        except ValueError:
            continue
    return sorted(files, reverse=True)


def verify_backup(path: Path) -> dict:
    """Return SQLite integrity and schema facts without modifying a backup."""
    try:
        # Immutable snapshots cannot change while they are being checked.  In
        # particular, this prevents SQLite from creating a shared-memory
        # sidecar when the snapshot was copied from a WAL-mode database.
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"ok": False, "detail": str(exc)}
    return {
        "ok": integrity == "ok",
        "detail": integrity,
        "schema_version": schema[0] if schema else None,
        "objects": tables,
    }


def prune_backups(
    directory: Path, *, daily_keep: int = DAILY_KEEP, weekly_keep: int = WEEKLY_KEEP
) -> list[Path]:
    """Apply 7-daily / 4-weekly generational retention and return deletions."""
    # A SQLite destination can briefly leave sidecars after the backup handle
    # closes. They are never valid snapshots and must not survive a retention
    # pass, including sidecars left by an older runner version.
    for sidecar in directory.glob("memory-*.db-wal"):
        sidecar.unlink(missing_ok=True)
    for sidecar in directory.glob("memory-*.db-shm"):
        sidecar.unlink(missing_ok=True)
    keep: set[Path] = set()
    daily_dates: set[str] = set()
    weekly_dates: set[tuple[int, int]] = set()
    for timestamp, path in _backup_files(directory):
        day = timestamp.date().isoformat()
        week = timestamp.isocalendar()[:2]
        if len(daily_dates) < daily_keep and day not in daily_dates:
            keep.add(path)
            daily_dates.add(day)
        elif (
            day not in daily_dates and len(weekly_dates) < weekly_keep and week not in weekly_dates
        ):
            keep.add(path)
            weekly_dates.add(week)

    removed = []
    for _timestamp_value, path in _backup_files(directory):
        if path not in keep:
            path.unlink()
            removed.append(path)
    return removed


def _gcs_object_uri(gcs_uri: str, filename: str) -> str:
    """Join a ``gs://bucket/prefix`` with a snapshot filename."""
    return f"{gcs_uri.rstrip('/')}/{filename}"


def _gcs_copy_commands(local_path: Path, dest_uri: str) -> list[list[str]]:
    """Candidate CLI invocations with resolved binaries; prefer gsutil."""
    commands: list[list[str]] = []
    gsutil = _resolve_gcs_tool("gsutil")
    if gsutil:
        commands.append([gsutil, "cp", str(local_path), dest_uri])
    gcloud = _resolve_gcs_tool("gcloud")
    if gcloud:
        commands.append([gcloud, "storage", "cp", str(local_path), dest_uri])
    return commands


def upload_backup_to_gcs(
    local_path: Path,
    *,
    gcs_uri: str | None = None,
) -> dict:
    """Best-effort copy of a local snapshot to ``UA_BACKUP_GCS_URI``.

    Returns a result dict with ``status`` of ``ok``, ``skipped``, or ``error``.
    Never raises for missing config, missing CLI, or remote failures.
    """
    uri = gcs_uri if gcs_uri is not None else config.backup_gcs_uri()
    if not uri:
        return {"status": "skipped", "reason": "UA_BACKUP_GCS_URI unset"}
    if not uri.startswith("gs://"):
        detail = f"UA_BACKUP_GCS_URI must start with gs://, got {uri!r}"
        logger.warning("GCS backup upload skipped: %s", detail)
        return {"status": "error", "detail": detail}

    dest = _gcs_object_uri(uri, local_path.name)
    commands = _gcs_copy_commands(local_path, dest)
    if not commands:
        detail = "no GCS client available (gsutil / gcloud)"
        logger.warning("GCS backup upload failed: %s", detail)
        return {"status": "error", "detail": detail, "uri": dest, "tried": []}

    last_detail = "no GCS client available (gsutil / gcloud)"
    tried: list[str] = []
    for cmd in commands:
        tool = Path(cmd[0]).name
        tried.append(tool)
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_GCS_UPLOAD_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            detail = f"{tool} timed out after {_GCS_UPLOAD_TIMEOUT_S}s"
            logger.warning("GCS backup upload failed: %s", detail)
            return {"status": "error", "detail": detail, "uri": dest, "tool": tool}
        except OSError as exc:
            last_detail = f"{tool}: {exc}"
            continue
        if completed.returncode == 0:
            return {"status": "ok", "uri": dest, "tool": tool}
        err = (completed.stderr or completed.stdout or "").strip()
        last_detail = err or f"{tool} exited {completed.returncode}"
        logger.warning("GCS backup upload failed via %s: %s", tool, last_detail)
        # Prefer the primary tool's failure over falling through silently when
        # a second client exists but the first already authenticated and failed.
        return {
            "status": "error",
            "detail": last_detail,
            "uri": dest,
            "tool": tool,
            "tried": tried,
        }

    logger.warning("GCS backup upload failed: %s", last_detail)
    return {
        "status": "error",
        "detail": last_detail,
        "uri": dest,
        "tried": tried,
    }


# --- S3-compatible offsite copy (task #2585) --------------------------------

_S3_URI_RE = re.compile(r"^s3://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])(?:/(.*))?$")
# S3 error codes are short CamelCase names such as SignatureDoesNotMatch. A
# code that matches this pattern cannot hold a key ID or signature, so it is
# the only part of a provider error body worth reporting.
_S3_ERROR_CODE_RE = re.compile(r"<Code>([A-Za-z]{1,64})</Code>")
_S3_UPLOAD_TIMEOUT_S = 600.0
# One PUT carries at most 5 GiB on S3 and R2; the hosted volume is smaller.
_S3_SINGLE_PUT_MAX_BYTES = 5 * 1024**3
_S3_CHUNK_BYTES = 1024 * 1024
_SIGV4_ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    now: datetime,
    service: str = "s3",
) -> dict[str, str]:
    """Return the headers that sign one request with AWS Signature Version 4.

    ``url`` must already carry its URI-encoded path (S3 does not re-encode
    it) and no query string. Every header in ``headers`` is signed alongside
    ``host``, ``x-amz-content-sha256`` and ``x-amz-date``. The result holds
    those two ``x-amz-*`` headers and ``Authorization``; the caller sends
    them together with ``headers``.
    """
    parts = urlsplit(url)
    if parts.query:
        raise ValueError("sigv4_headers does not sign query strings")
    amz_date = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    signed = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-content-sha256"] = payload_sha256
    signed["x-amz-date"] = amz_date
    names = sorted(signed)
    canonical_request = "\n".join(
        [
            method.upper(),
            parts.path or "/",
            "",
            "".join(f"{name}:{signed[name]}\n" for name in names),
            ";".join(names),
            payload_sha256,
        ]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            _SIGV4_ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = _hmac_sha256(f"AWS4{secret_key}".encode(), date_stamp)
    for part in (region, service, "aws4_request"):
        key = _hmac_sha256(key, part)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
        "Authorization": (
            f"{_SIGV4_ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}"
        ),
    }


def _file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as fh:
        while chunk := fh.read(_S3_CHUNK_BYTES):
            yield chunk


def upload_backup_to_s3(
    local_path: Path,
    *,
    s3_uri: str | None = None,
    endpoint: str | None = None,
    region: str | None = None,
    access_key: str | None = None,
    secret_key: str | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict:
    """Best-effort copy of a local snapshot to ``UA_BACKUP_S3_URI``.

    Path-style PUT to ``UA_BACKUP_S3_ENDPOINT`` (``https://`` only), signed
    with ``UA_BACKUP_S3_ACCESS_KEY_ID`` / ``UA_BACKUP_S3_SECRET_ACCESS_KEY``
    for ``UA_BACKUP_S3_REGION`` (default ``auto``, which R2 expects). Returns
    ``status`` ``ok``, ``skipped`` or ``error`` and never raises; no result
    or log line carries a credential or the signature.
    """
    uri = s3_uri if s3_uri is not None else config.backup_s3_uri()
    if not uri:
        return {"status": "skipped", "reason": "UA_BACKUP_S3_URI unset"}
    match = _S3_URI_RE.match(uri)
    if not match:
        detail = "UA_BACKUP_S3_URI must look like s3://bucket[/prefix]"
        logger.warning("S3 backup upload skipped: %s", detail)
        return {"status": "error", "detail": detail}
    bucket, prefix = match.group(1), (match.group(2) or "").strip("/")
    key = f"{prefix}/{local_path.name}" if prefix else local_path.name
    dest = f"s3://{bucket}/{key}"

    endpoint = endpoint if endpoint is not None else config.backup_s3_endpoint()
    try:
        parts = urlsplit(endpoint or "")
        # Accessing port validates its spelling and range. urlsplit alone
        # accepts missing hosts, credentials, queries and malformed ports.
        _ = parts.port
        origin_only = bool(
            endpoint
            and parts.scheme == "https"
            and parts.hostname
            and not parts.netloc.endswith(":")
            and parts.path in ("", "/")
            and parts.username is None
            and parts.password is None
            and "?" not in endpoint
            and "#" not in endpoint
            and "\\" not in endpoint
            and not any(char.isspace() or ord(char) < 32 for char in endpoint)
        )
    except ValueError:
        origin_only = False
    if not origin_only:
        detail = (
            "UA_BACKUP_S3_ENDPOINT must be an https:// origin with a host and optional port only"
        )
        logger.warning("S3 backup upload failed: %s", detail)
        return {"status": "error", "detail": detail, "uri": dest}
    if access_key is None or secret_key is None:
        env_access, env_secret = config.backup_s3_credentials()
        access_key = access_key if access_key is not None else env_access
        secret_key = secret_key if secret_key is not None else env_secret
    if not access_key or not secret_key:
        detail = "UA_BACKUP_S3_ACCESS_KEY_ID / UA_BACKUP_S3_SECRET_ACCESS_KEY unset"
        logger.warning("S3 backup upload failed: %s", detail)
        return {"status": "error", "detail": detail, "uri": dest}

    try:
        size = local_path.stat().st_size
        if size > _S3_SINGLE_PUT_MAX_BYTES:
            detail = f"snapshot is {size} bytes, over the single-PUT limit"
            logger.warning("S3 backup upload failed: %s", detail)
            return {"status": "error", "detail": detail, "uri": dest}
        digest = hashlib.sha256()
        for chunk in _file_chunks(local_path):
            digest.update(chunk)
    except OSError as exc:
        detail = f"cannot read snapshot ({type(exc).__name__})"
        logger.warning("S3 backup upload failed: %s", detail)
        return {"status": "error", "detail": detail, "uri": dest}

    import httpx

    owns_client = client is None
    http = client
    try:
        url = str(httpx.URL(endpoint.rstrip("/") + quote(f"/{bucket}/{key}", safe="/")))
        headers = {"Content-Type": "application/octet-stream"}
        headers.update(
            sigv4_headers(
                method="PUT",
                url=url,
                headers=headers,
                payload_sha256=digest.hexdigest(),
                access_key=access_key,
                secret_key=secret_key,
                region=region or config.backup_s3_region(),
                now=now or datetime.now(UTC),
            )
        )
        # Avoid chunked transfer encoding, which S3 PUT Object refuses.
        headers["Content-Length"] = str(size)
        if http is None:
            http = httpx.Client(timeout=_S3_UPLOAD_TIMEOUT_S)
        response = http.put(url, content=_file_chunks(local_path), headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
        # Transport/header errors may quote the signed request. Provider error
        # bodies can echo the access key and signature too: publish neither.
        detail = f"S3 request failed ({type(exc).__name__})"
        logger.warning("S3 backup upload failed: %s", detail)
        return {"status": "error", "detail": detail, "uri": dest}
    finally:
        if owns_client and http is not None:
            http.close()
    if response.status_code != 200:
        code = _S3_ERROR_CODE_RE.search(response.text[:4096])
        detail = f"HTTP {response.status_code}" + (f" {code.group(1)}" if code else "")
        logger.warning("S3 backup upload failed: %s", detail)
        return {"status": "error", "detail": detail, "uri": dest}
    return {"status": "ok", "uri": dest, "bytes": size, "etag": response.headers.get("etag")}


def _check_private_backup_directory(directory: Path) -> None:
    """Refuse a backup destination another local user could redirect or write.

    The directory itself (not its ancestors) must be a real directory, not a
    symlink, owned by the current user and not writable by group or others.
    """
    st = os.lstat(directory)
    if stat.S_ISLNK(st.st_mode):
        raise PermissionError(f"backup directory is a symlink: {directory}")
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(f"backup directory is not a directory: {directory}")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise PermissionError(f"backup directory is not owned by the current user: {directory}")
    if st.st_mode & 0o022:
        raise PermissionError(f"backup directory is writable by other users: {directory}")


def create_backup(
    *,
    source: Path | None = None,
    directory: Path | None = None,
    now: datetime | None = None,
    upload_to_gcs: bool = False,
    upload_to_s3: bool = False,
) -> dict:
    """Create an integrity-checked online snapshot and apply retention.

    The temporary file is atomically renamed only after SQLite reports an
    intact copy, so a failed or interrupted snapshot can never be mistaken for
    a successful backup by the doctor check.

    When ``upload_to_gcs`` is True and ``UA_BACKUP_GCS_URI`` is set, the new
    snapshot is best-effort uploaded afterward. Local success is never revoked
    by a GCS failure; see ``gcs_upload`` in the returned dict. Callers such as
    restore rehearsals and tests must leave ``upload_to_gcs`` False so a
    sourced ``service.env`` cannot push synthetic databases to production.
    ``upload_to_s3`` is the same opt-in for ``UA_BACKUP_S3_URI`` (task #2585).

    Local retention is ``UA_BACKUP_KEEP_DAILY`` / ``UA_BACKUP_KEEP_WEEKLY``
    generations (default 7 / 4); a small hosted volume keeps fewer and relies
    on the offsite copy for history.
    """
    source = source or config.db_path()
    directory = directory or config.backup_dir()
    if not source.is_file():
        raise FileNotFoundError(f"database does not exist: {source}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private_backup_directory(directory)
    destination = directory / f"memory-{_timestamp(now)}.db"
    if destination.exists():
        raise FileExistsError(f"backup already exists for this timestamp: {destination}")
    # Pre-create an unpredictable owner-only file BEFORE SQLite opens it.
    # mkdir(mode=0700) does not repair an existing directory, and SQLite's
    # ordinary file creation otherwise inherits the caller's umask (often 0644).
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".tmp", dir=directory
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        source_conn = sqlite3.connect(source)
        destination_conn = sqlite3.connect(temporary)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()
        verification = verify_backup(temporary)
        if not verification["ok"]:
            raise RuntimeError(f"backup integrity check failed: {verification['detail']}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
        # SQLite may create WAL/shared-memory sidecars for the temporary
        # destination. They are not part of the verified snapshot and must not
        # accumulate in the backup directory after the atomic rename.
        for sidecar in (
            Path(f"{temporary}-wal"),
            Path(f"{temporary}-shm"),
            Path(f"{destination}-wal"),
            Path(f"{destination}-shm"),
        ):
            sidecar.unlink(missing_ok=True)

    daily_keep, weekly_keep = config.backup_local_keep()
    removed = prune_backups(directory, daily_keep=daily_keep, weekly_keep=weekly_keep)
    if upload_to_gcs:
        gcs_upload = upload_backup_to_gcs(destination)
    else:
        gcs_upload = {"status": "skipped", "reason": "upload not requested"}
    if upload_to_s3:
        s3_upload = upload_backup_to_s3(destination)
    else:
        s3_upload = {"status": "skipped", "reason": "upload not requested"}
    return {
        "path": str(destination),
        "verification": verification,
        "removed": [str(p) for p in removed],
        "gcs_upload": gcs_upload,
        "s3_upload": s3_upload,
    }


class RestoreDestinationError(ValueError):
    """Raised when a restore rehearsal would land on the live database or UA_HOME."""


class FailoverBindError(ValueError):
    """Raised when a failover drill would bind a public MCP listener."""


_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_PUBLIC_BIND_ALIASES = frozenset({"0.0.0.0", "::", "[::]", "*", "::0", "[::0]"})


def assert_private_failover_bind(host: str) -> str:
    """Refuse public/wildcard HTTP binds for throwaway failover drills.

    Allowed: IPv4/IPv6 loopback, or Tailscale CGNAT (``100.64.0.0/10``).
    Hostnames are rejected so the drill cannot accidentally resolve to a
    public address. This does not change the live Mac hub bind.
    """
    raw = (host or "").strip()
    if not raw:
        raise FailoverBindError("refusing empty UA_HTTP_HOST")
    if raw in _PUBLIC_BIND_ALIASES:
        raise FailoverBindError(f"refusing public bind {raw!r}: Tailscale CGNAT or loopback only")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise FailoverBindError(
            f"refusing hostname bind {raw!r}: failover drills must use a "
            "loopback or Tailscale CGNAT literal"
        ) from exc
    if ip.is_unspecified:
        raise FailoverBindError(f"refusing unspecified bind {raw}")
    if ip.is_loopback or ip in _TAILSCALE_CGNAT:
        return raw
    raise FailoverBindError(
        f"refusing bind {raw}: not loopback and not Tailscale CGNAT (100.64.0.0/10)"
    )


def assert_scratch_restore_destination(dest_dir: Path) -> Path:
    """Reject destinations that could overwrite the live hub database.

    Restore rehearsals copy a snapshot into scratch only. The current
    process ``UA_HOME`` and its ``memory.db`` are never valid targets.
    """
    dest = dest_dir.expanduser().resolve()
    live = config.db_path().expanduser().resolve()
    home = config.data_home().expanduser().resolve()
    if dest == live or dest == home:
        raise RestoreDestinationError(
            f"refusing restore destination {dest}: live database/UA_HOME is not a drill target"
        )
    try:
        dest.relative_to(home)
    except ValueError:
        pass
    else:
        raise RestoreDestinationError(
            f"refusing restore destination {dest}: path is inside UA_HOME {home}"
        )
    staged_live_name = (dest / "memory.db").resolve()
    if staged_live_name == live:
        raise RestoreDestinationError(
            f"refusing restore destination {dest}: would write the live database {live}"
        )
    return dest


def readonly_snapshot_facts(path: Path) -> dict:
    """Integrity plus row counts for a snapshot opened immutable."""
    verification = verify_backup(path)
    counts: dict[str, int | None] = {}
    if verification.get("ok"):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            try:
                for table in ("tasks", "memory", "delegations"):
                    try:
                        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except sqlite3.Error:
                        counts[table] = None
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return {"verification": verification, "counts": counts, "count_error": str(exc)}
    return {"verification": verification, "counts": counts}


def stage_backup_snapshot(src: Path, dest_dir: Path, *, as_ua_home: bool = False) -> dict:
    """Copy a snapshot into scratch and verify it. Never writes the live DB.

    ``as_ua_home=True`` names the copy ``memory.db`` so a throwaway
    ``UA_HOME=dest_dir`` process can open it. The destination must still pass
    ``assert_scratch_restore_destination``.
    """
    dest = assert_scratch_restore_destination(dest_dir)
    src = src.expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"snapshot does not exist: {src}")
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = dest / ("memory.db" if as_ua_home else src.name)
    if target.resolve() == config.db_path().expanduser().resolve():
        raise RestoreDestinationError(f"refusing to write live database {target}")
    if src.resolve() != target.resolve():
        shutil.copy2(src, target)
    facts = readonly_snapshot_facts(target)
    return {"path": str(target), **facts}


def backup_status(directory: Path | None = None, *, now: datetime | None = None) -> dict:
    """Describe the latest successful-looking local snapshot for system_doctor."""
    directory = directory or config.backup_dir()
    files = _backup_files(directory)
    if not files:
        return {"exists": False, "directory": str(directory)}
    timestamp, path = files[0]
    verification = verify_backup(path)
    age_seconds = max(0.0, ((now or datetime.now(UTC)).astimezone(UTC) - timestamp).total_seconds())
    return {
        "exists": True,
        "directory": str(directory),
        "path": str(path),
        "created_at": timestamp.isoformat(),
        "age_seconds": age_seconds,
        "verification": verification,
    }
