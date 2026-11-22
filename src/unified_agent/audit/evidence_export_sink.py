"""Scheduled signed evidence export to customer storage (task #1266, Epic G/F3).

Builds on the existing signed evidence export (task #1267/F1, this package's
``evidence_report`` + ``evidence_signing``): this module never re-implements
signing, it just runs ``evidence_report.build_evidence_report`` ->
``evidence_signing.sign_export`` on a schedule and lands the two resulting
files (the JSON export + its ``.sig.json`` sidecar) at a customer-controlled
storage destination, off by default.

Design:

- ``Destination`` is a tiny interface (``put``/``exists``/``get``/
  ``list_prefix``/``delete``/``run_lock``) so the export/retention logic is
  exercised in tests against ``LocalPathDestination`` alone. ``GCSDestination``
  and ``S3Destination`` wrap their SDKs behind a lazy import — neither
  ``google-cloud-storage`` nor ``boto3`` is a project dependency (checked
  against pyproject.toml/uv.lock for task #1266: neither is present), so
  importing this module never requires either package; only actually using
  that destination does, with a clear "install the package" error instead of
  an opaque ``ModuleNotFoundError`` deep in a stack trace.
- Every destination's credentials are named by environment variable only,
  same contract as ``connectors.ConnectorManifest.required_secrets`` and
  ``evidence_signing``'s own key lookup: this module never reads a secret's
  *value* except to hand GCS/S3 bucket names (not secrets) to their SDK
  constructors. The SDKs themselves resolve their own credentials from their
  own conventional environment (``GOOGLE_APPLICATION_CREDENTIALS``,
  ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY``, workload identity, an
  instance role, ...); this code never touches those values, so there is
  nothing here that could log or leak them.
- Every export reads the COMPLETE matching window (``build_evidence_report(...,
  complete=True)``, task #1266 review finding — the CLI/API default clamp
  of 50/500 records would otherwise let a signed export silently truncate).
- ``UA_EVIDENCE_EXPORT_WINDOW_DAYS`` counts inclusive UTC calendar dates
  ending on the last completed UTC date; zero explicitly means an empty date
  window. The publication date remains separate from the report window.
- Object naming is content-addressed and race-safe (task #1266 review
  finding — concurrent runs must never produce a corrupted cross-pair):
  the body/sidecar pair lives at
  ``evidence/<org_id>/<export-date>/<chain_head>-<sha256(body)[:16]>.json``
  (+ ``.sig.json`` at the same key). A run only ever writes a pair it built
  itself, so the pair is always internally self-consistent regardless of
  what any other concurrent run is doing at the same time. After both
  objects land, a small commit marker —
  ``evidence/<org_id>/<export-date>/<chain_head>.committed`` — is written
  naming the winning pair. ``run_once`` builds one complete report from an
  explicit SQLite read snapshot and fingerprints its stable content, window,
  and signing key ID. It skips only when that fingerprint matches and the
  referenced body hash, sidecar, chain head, and active-key signature all
  validate. A report is built before deciding to skip, so changed same-day
  inputs cannot reuse stale bytes. Missing or corrupt objects and malformed
  markers trigger repair. Two concurrent runs can each write a full
  pair; whichever marker write lands last "wins" (is what a subsequent
  read/retention pass treats as current), but every pair ever written still
  verifies on its own — nothing is ever a mix of one run's body and
  another's signature. ``LocalPathDestination.run_lock()`` additionally
  takes a real ``fcntl`` lock for the duration of one run, so two local
  runs on the same host serialize instead of doing redundant work; remote
  destinations (GCS/S3, no shared lock available) rely on the
  content-addressed design alone for correctness, which holds without a
  lock.
- The storage date segment is the export date, independent of the report's
  ``since`` bound. Retention therefore ages artifacts from publication even
  when an operator requests a much longer evidence window.
- Retention (``UA_EVIDENCE_EXPORT_RETENTION_DAYS``) only ever deletes
  objects under this connector's own ``evidence/<org_id>/`` prefix. A
  marker and its pair always share the exact same date segment (both are
  named from the same ``date_segment`` computed once per run), so a single
  date-keyed cutoff sweep naturally treats them as one unit — an old
  marker and its pair cross the cutoff together, never separately — and
  the same sweep also reaps uncommitted orphan pairs (a pair written by a
  run that crashed, or lost a race, before its marker landed) once their
  date segment is old enough, without needing to distinguish committed from
  orphaned.

``run_once()`` is the scheduler entry point (systemd timer / launchd
StartCalendarInterval — see ``deploy/systemd/ua-evidence-export.{service,timer}``
and ``deploy/mac-hub/com.unified-agent.evidence-export.plist.example``,
both shipped inert). ``unified-agent evidence export`` (``evidence_cli.py``)
is the human-facing CLI entry point for the same call.
"""

from __future__ import annotations

import abc
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import post_init
from . import evidence_report, evidence_signing

DESTINATION_LOCAL = "local"
DESTINATION_GCS = "gcs"
DESTINATION_S3 = "s3"
_KNOWN_DESTINATIONS = (DESTINATION_LOCAL, DESTINATION_GCS, DESTINATION_S3)

DEFAULT_RETENTION_DAYS = 90
DEFAULT_WINDOW_DAYS = 1
_PREFIX_ROOT = "evidence"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Signing readiness is OR-shaped (one of two names), the same
# required_secret_alternatives semantics `connectors.secret_status` uses for
# a manifest's declared alternative groups (task #1266 review finding 4:
# manifest_secret_status previously ignored signing entirely).
_SIGNING_KEY_VARS = ("UA_EVIDENCE_SIGNING_KEY", "UA_EVIDENCE_SIGNING_SECRET_NAME")


class DestinationError(RuntimeError):
    """A destination operation failed or the destination is misconfigured."""


class DestinationConfigError(DestinationError):
    """The destination is not configured (missing env var), named explicitly."""


# ---------------------------------------------------------------------------
# Object naming
# ---------------------------------------------------------------------------


def _sanitize_segment(value: str, *, label: str) -> str:
    value = (value or "").strip()
    if not value or not _SEGMENT_RE.fullmatch(value):
        raise DestinationConfigError(
            f"{label} must be non-empty and match {_SEGMENT_RE.pattern} (got {value!r})"
        )
    return value


def _base_prefix(org_id: str, date_segment: str, chain_head: str) -> tuple[str, str]:
    safe_org = _sanitize_segment(org_id, label="org_id")
    if not _DATE_RE.fullmatch(date_segment):
        raise DestinationConfigError(f"date_segment must be YYYY-MM-DD (got {date_segment!r})")
    safe_head = _sanitize_segment(chain_head or "empty", label="chain_head")
    return safe_org, safe_head


def object_key(
    org_id: str, date_segment: str, chain_head: str, content_hash: str, suffix: str
) -> str:
    """Content-addressed object key for one export pair:
    ``evidence/<org_id>/<date>/<chain_head>-<content_hash><suffix>``.

    ``content_hash`` (the first 16 hex chars of the body's sha256, by
    convention) makes the key unique to the exact bytes this run wrote, so
    two concurrent runs — even ones that land the same chain head — never
    share a key and can never produce a cross-pair (task #1266 review
    finding 2).
    """
    safe_org, safe_head = _base_prefix(org_id, date_segment, chain_head)
    safe_hash = _sanitize_segment(content_hash, label="content_hash")
    if suffix not in (".json", ".sig.json"):
        raise ValueError(f"unsupported object suffix: {suffix!r}")
    return f"{_PREFIX_ROOT}/{safe_org}/{date_segment}/{safe_head}-{safe_hash}{suffix}"


def marker_key(org_id: str, date_segment: str, chain_head: str) -> str:
    """Commit marker key: ``evidence/<org_id>/<date>/<chain_head>.committed``.

    Names the pair that "won" for this chain head/date — see the module
    docstring. Deliberately NOT content-addressed (unlike ``object_key``):
    exactly one marker exists per (org, date, chain_head), so
    ``run_once``'s idempotency check has one well-known key to look up.
    """
    safe_org, safe_head = _base_prefix(org_id, date_segment, chain_head)
    return f"{_PREFIX_ROOT}/{safe_org}/{date_segment}/{safe_head}.committed"


def _validate_key(key: str) -> None:
    if not key or key.startswith("/") or ".." in Path(key).parts:
        raise DestinationError(f"invalid object key: {key!r}")


def _cloud_error(provider: str, operation: str, key: str, exc: Exception) -> DestinationError:
    """Normalize SDK failures without copying provider-controlled details."""
    return DestinationError(f"{provider} {operation} failed for {key!r} ({type(exc).__name__})")


def _gcs_not_found(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if callable(code):
        code = code()
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return code == 404 or status_code == 404 or "404" in str(exc) or "No such object" in str(exc)


# ---------------------------------------------------------------------------
# Destination interface
# ---------------------------------------------------------------------------


class Destination(abc.ABC):
    """Minimal storage interface a scheduled export writes/retains against."""

    @abc.abstractmethod
    def put(self, key: str, body: bytes, *, content_type: str = "application/json") -> None: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def get(self, key: str) -> bytes | None: ...

    @abc.abstractmethod
    def list_prefix(self, prefix: str) -> list[str]: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    @contextlib.contextmanager
    def run_lock(self) -> Iterator[None]:
        """Best-effort mutual exclusion for one run's check-build-write-commit
        sequence. Default no-op: correctness for a remote destination (GCS/S3)
        comes from the content-addressed pair + last-marker-wins design
        (module docstring), not from a lock nothing there could share anyway.
        ``LocalPathDestination`` overrides this with a real ``fcntl`` lock.
        """
        yield


class LocalPathDestination(Destination):
    """Writes under a local directory. Fully implemented and tested — the
    reference destination other environments (a mounted network share, a
    sync agent watching a directory) can reuse as-is.

    Directories are created ``0700`` and files ``0600`` (task #1266 review
    finding 7) — an evidence export is exactly the kind of artifact that
    should never be world- or group-readable by default on a shared host.
    Existing directories and files are also checked for symlinks, ownership,
    and group/world permissions before they are used.
    """

    def __init__(self, base_dir: str | os.PathLike[str]) -> None:
        # Resolve the configured root once. A symlink explicitly used as the
        # configured root is an operator choice; symlinks below it are not.
        self.base_dir = Path(base_dir).expanduser().resolve()

    @staticmethod
    def _check_private_existing(path: Path, *, directory: bool) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            raise DestinationError(f"symlink is not allowed in local export path: {path}")
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not expected:
            kind = "directory" if directory else "regular file"
            raise DestinationError(f"local export path must be a {kind}: {path}")
        if info.st_uid != os.getuid():
            raise DestinationError(f"local export path is not owned by the current user: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise DestinationError(
                f"local export path is not private (expected mode 0700/0600): {path}"
            )
        return True

    def _resolve(self, key: str, *, final_directory: bool = False) -> Path:
        _validate_key(key)
        base = self.base_dir
        path = base / key
        if path != base and base not in path.parents:
            raise DestinationError(f"key escapes destination root: {key!r}")
        self._check_private_existing(base, directory=True)
        current = base
        parts = Path(key).parts
        for index, part in enumerate(parts):
            current /= part
            is_directory = index < len(parts) - 1 or final_directory
            self._check_private_existing(current, directory=is_directory)
        return path

    @staticmethod
    def _mkdir_private(path: Path) -> None:
        """Create ``path`` (and parents), forcing ``0700`` on every level
        this call newly creates — ``Path.mkdir(mode=...)`` alone is still
        subject to umask, so each newly created level is chmod'd explicitly
        afterward rather than trusted to land at the requested mode."""
        to_chmod: list[Path] = []
        probe = path
        while not probe.exists() and not probe.is_symlink():
            to_chmod.append(probe)
            probe = probe.parent
        try:
            info = probe.lstat()
        except FileNotFoundError as exc:
            raise DestinationError(f"local export parent disappeared: {probe}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise DestinationError(f"local export parent must be a real directory: {probe}")
        path.mkdir(parents=True, exist_ok=True)
        try:
            for created in to_chmod:
                os.chmod(created, 0o700)
            for created in to_chmod:
                LocalPathDestination._check_private_existing(created, directory=True)
        except OSError:
            for created in to_chmod:
                with contextlib.suppress(OSError):
                    created.rmdir()
            raise

    def put(self, key: str, body: bytes, *, content_type: str = "application/json") -> None:
        del content_type  # Local filesystem has no content-type metadata slot.
        path = self._resolve(key)
        self._mkdir_private(path.parent)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            os.chmod(tmp, 0o600)  # belt-and-suspenders against umask on O_CREAT
            tmp.replace(path)
            os.chmod(path, 0o600)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def get(self, key: str) -> bytes | None:
        path = self._resolve(key)
        return path.read_bytes() if path.is_file() else None

    def list_prefix(self, prefix: str) -> list[str]:
        prefix = prefix.strip("/")
        root = self._resolve(prefix, final_directory=True) if prefix else self.base_dir
        if not root.is_dir():
            return []
        return sorted(
            p.relative_to(self.base_dir).as_posix()
            for p in root.rglob("*")
            if p.is_file() and ".tmp-" not in p.name
        )

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    @contextlib.contextmanager
    def run_lock(self) -> Iterator[None]:
        self._mkdir_private(self.base_dir)
        lock_path = self.base_dir / ".evidence-export.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class GCSDestination(Destination):
    """Google Cloud Storage destination.

    Requires the ``google-cloud-storage`` package, which is NOT a project
    dependency (not present in pyproject.toml/uv.lock as of task #1266) —
    importing ``unified_agent`` never pulls it in; only constructing this
    class and calling one of its methods does, and a missing install fails
    with a clear message rather than a bare ``ModuleNotFoundError``.
    Credentials are resolved by the SDK itself from its own environment
    (``GOOGLE_APPLICATION_CREDENTIALS``, workload identity, ...); this class
    never reads or logs a credential value, only the bucket name/prefix
    (which are not secrets).
    """

    def __init__(self, bucket: str, *, prefix: str = "") -> None:
        self._bucket_name = bucket
        self._prefix = prefix.strip("/")
        self._client: Any = None

    def _client_(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import storage as gcs_storage  # type: ignore[import-not-found]
            except ImportError as exc:
                raise DestinationError(
                    "GCS destination requires the 'google-cloud-storage' package; "
                    "install it with `pip install google-cloud-storage` (or `uv add "
                    "google-cloud-storage`) before enabling UA_EVIDENCE_EXPORT_DESTINATION=gcs"
                ) from exc
            self._client = gcs_storage.Client()
        return self._client

    def _full_key(self, key: str) -> str:
        _validate_key(key)
        return f"{self._prefix}/{key}" if self._prefix else key

    def _bucket(self) -> Any:
        return self._client_().bucket(self._bucket_name)

    def put(self, key: str, body: bytes, *, content_type: str = "application/json") -> None:
        try:
            self._bucket().blob(self._full_key(key)).upload_from_string(
                body, content_type=content_type
            )
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures at the boundary
            raise _cloud_error("GCS", "put", key, exc) from exc

    def exists(self, key: str) -> bool:
        try:
            return bool(self._bucket().blob(self._full_key(key)).exists())
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures at the boundary
            raise _cloud_error("GCS", "exists", key, exc) from exc

    def get(self, key: str) -> bytes | None:
        try:
            blob = self._bucket().blob(self._full_key(key))
            if not blob.exists():
                return None
            return blob.download_as_bytes()
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures at the boundary
            if _gcs_not_found(exc):
                return None
            raise _cloud_error("GCS", "get", key, exc) from exc

    def list_prefix(self, prefix: str) -> list[str]:
        prefix = prefix.strip("/")
        full_prefix = f"{self._prefix}/{prefix}" if self._prefix else prefix
        strip = f"{self._prefix}/" if self._prefix else ""
        try:
            blobs = self._client_().list_blobs(self._bucket_name, prefix=full_prefix)
            return sorted(
                (name[len(strip) :] if strip and name.startswith(strip) else name)
                for name in (blob.name for blob in blobs)
            )
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures at the boundary
            raise _cloud_error("GCS", "list", prefix, exc) from exc

    def delete(self, key: str) -> None:
        try:
            blob = self._bucket().blob(self._full_key(key))
            blob.delete()
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize "already gone" across SDK versions
            if not _gcs_not_found(exc):
                raise _cloud_error("GCS", "delete", key, exc) from exc


class S3Destination(Destination):
    """Amazon S3 (or an S3-compatible endpoint) destination.

    Requires the ``boto3`` package, which is NOT a project dependency (not
    present in pyproject.toml/uv.lock as of task #1266); same lazy-import,
    clear-error contract as ``GCSDestination``. Credentials are resolved by
    boto3's own default chain (``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY``,
    a shared credentials file, an instance/task role, ...) — this class never
    reads or logs a credential value.
    """

    def __init__(self, bucket: str, *, prefix: str = "", region: str | None = None) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._region = region
        self._client: Any = None

    def _client_(self) -> Any:
        if self._client is None:
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:
                raise DestinationError(
                    "S3 destination requires the 'boto3' package; install it with "
                    "`pip install boto3` (or `uv add boto3`) before enabling "
                    "UA_EVIDENCE_EXPORT_DESTINATION=s3"
                ) from exc
            self._client = boto3.client("s3", region_name=self._region)
        return self._client

    def _full_key(self, key: str) -> str:
        _validate_key(key)
        return f"{self._prefix}/{key}" if self._prefix else key

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        code = (response or {}).get("Error", {}).get("Code") if isinstance(response, dict) else None
        return code in ("404", "NoSuchKey", "NotFound")

    def put(self, key: str, body: bytes, *, content_type: str = "application/json") -> None:
        try:
            self._client_().put_object(
                Bucket=self._bucket, Key=self._full_key(key), Body=body, ContentType=content_type
            )
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures at the boundary
            raise _cloud_error("S3", "put", key, exc) from exc

    def exists(self, key: str) -> bool:
        try:
            self._client_().head_object(Bucket=self._bucket, Key=self._full_key(key))
            return True
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - botocore raises a dynamic ClientError type
            if self._is_not_found(exc):
                return False
            raise _cloud_error("S3", "exists", key, exc) from exc

    def get(self, key: str) -> bytes | None:
        try:
            response = self._client_().get_object(Bucket=self._bucket, Key=self._full_key(key))
            return response["Body"].read()
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._is_not_found(exc):
                return None
            raise _cloud_error("S3", "get", key, exc) from exc

    def list_prefix(self, prefix: str) -> list[str]:
        prefix = prefix.strip("/")
        full_prefix = f"{self._prefix}/{prefix}" if self._prefix else prefix
        strip = f"{self._prefix}/" if self._prefix else ""
        try:
            client = self._client_()
            paginator = client.get_paginator("list_objects_v2")
            out: list[str] = []
            for page in paginator.paginate(Bucket=self._bucket, Prefix=full_prefix):
                for obj in page.get("Contents", []) or []:
                    name = obj["Key"]
                    out.append(name[len(strip) :] if strip and name.startswith(strip) else name)
            return sorted(out)
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures at the boundary
            raise _cloud_error("S3", "list", prefix, exc) from exc

    def delete(self, key: str) -> None:
        try:
            self._client_().delete_object(Bucket=self._bucket, Key=self._full_key(key))
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures at the boundary
            raise _cloud_error("S3", "delete", key, exc) from exc


def destination_from_env(env: Mapping[str, str] | None = None) -> Destination:
    """Build the configured ``Destination`` from environment settings.

    Raises ``DestinationConfigError`` naming exactly which env var is
    missing — never guesses a default bucket/path.
    """
    source = os.environ if env is None else env
    kind = (source.get("UA_EVIDENCE_EXPORT_DESTINATION") or "").strip().lower()
    if kind == DESTINATION_LOCAL:
        path = (source.get("UA_EVIDENCE_EXPORT_LOCAL_PATH") or "").strip()
        if not path:
            raise DestinationConfigError(
                "UA_EVIDENCE_EXPORT_LOCAL_PATH is required for UA_EVIDENCE_EXPORT_DESTINATION=local"
            )
        return LocalPathDestination(path)
    if kind == DESTINATION_GCS:
        bucket = (source.get("UA_EVIDENCE_EXPORT_GCS_BUCKET") or "").strip()
        if not bucket:
            raise DestinationConfigError(
                "UA_EVIDENCE_EXPORT_GCS_BUCKET is required for UA_EVIDENCE_EXPORT_DESTINATION=gcs"
            )
        prefix = (source.get("UA_EVIDENCE_EXPORT_GCS_PREFIX") or "").strip()
        return GCSDestination(bucket, prefix=prefix)
    if kind == DESTINATION_S3:
        bucket = (source.get("UA_EVIDENCE_EXPORT_S3_BUCKET") or "").strip()
        if not bucket:
            raise DestinationConfigError(
                "UA_EVIDENCE_EXPORT_S3_BUCKET is required for UA_EVIDENCE_EXPORT_DESTINATION=s3"
            )
        prefix = (source.get("UA_EVIDENCE_EXPORT_S3_PREFIX") or "").strip()
        region = (source.get("UA_EVIDENCE_EXPORT_S3_REGION") or "").strip() or None
        return S3Destination(bucket, prefix=prefix, region=region)
    raise DestinationConfigError(
        f"UA_EVIDENCE_EXPORT_DESTINATION must be one of {_KNOWN_DESTINATIONS!r} (got {kind!r})"
    )


# ---------------------------------------------------------------------------
# Connector manifest wiring (task #1204, Epic G) — see connectors.py's
# EVIDENCE_EXPORT_MANIFEST for the registration itself.
# ---------------------------------------------------------------------------


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Off-by-default runtime gate, same shape as ``github_app.enabled()``."""
    source = os.environ if env is None else env
    return (source.get("UA_EVIDENCE_EXPORT_ENABLED") or "0").strip() == "1"


def manifest_secret_status(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Destination- and signing-aware secret readiness for the evidence-export manifest.

    Two independent requirements, combined the same way
    ``connectors.secret_status`` combines ``required_secrets`` (AND) with
    ``required_secret_alternatives`` (OR-of-ANDs):

    - the *bucket/path* selector for the active
      ``UA_EVIDENCE_EXPORT_DESTINATION`` (an AND-style single named
      requirement — GCS/S3 credential env vars like
      ``GOOGLE_APPLICATION_CREDENTIALS`` are each just one of several
      equally valid ways those SDKs resolve credentials, so they are
      documented, not gated here);
    - evidence signing, which is genuinely OR-shaped: either
      ``UA_EVIDENCE_SIGNING_KEY`` or ``UA_EVIDENCE_SIGNING_SECRET_NAME``
      (task #1266 review finding 4 — ``run_once`` refuses to write an
      unsigned export, so readiness that ignores signing entirely was
      reporting a connector "satisfied" that could not actually run).

    An unset/unrecognized destination reports ``UA_EVIDENCE_EXPORT_DESTINATION``
    itself as missing, alongside signing if that is also unmet. Names only,
    same ``{present, missing, satisfied}`` shape as ``connectors.secret_status``.
    """
    source = os.environ if env is None else env

    def _present(name: str) -> bool:
        return bool((source.get(name) or "").strip())

    kind = (source.get("UA_EVIDENCE_EXPORT_DESTINATION") or "").strip().lower()
    if kind == DESTINATION_LOCAL:
        dest_names: list[str] | None = ["UA_EVIDENCE_EXPORT_LOCAL_PATH"]
    elif kind == DESTINATION_GCS:
        dest_names = ["UA_EVIDENCE_EXPORT_GCS_BUCKET"]
    elif kind == DESTINATION_S3:
        dest_names = ["UA_EVIDENCE_EXPORT_S3_BUCKET"]
    else:
        dest_names = None  # Destination itself is unset/unrecognized.

    signing_present = [name for name in _SIGNING_KEY_VARS if _present(name)]
    signing_missing = [] if signing_present else sorted(_SIGNING_KEY_VARS)

    if dest_names is None:
        missing = sorted({"UA_EVIDENCE_EXPORT_DESTINATION", *signing_missing})
        return {"present": sorted(signing_present), "missing": missing, "satisfied": False}

    dest_present = [name for name in dest_names if _present(name)]
    dest_missing = [name for name in dest_names if not _present(name)]

    present = sorted({*dest_present, *signing_present})
    missing = sorted({*dest_missing, *signing_missing})
    return {"present": present, "missing": missing, "satisfied": not missing}


# ---------------------------------------------------------------------------
# Retention (prefix-scoped: only ever touches this connector's own objects)
# ---------------------------------------------------------------------------


def _date_segment_from_key(key: str, prefix: str) -> str | None:
    if not key.startswith(prefix):
        return None
    rest = key[len(prefix) :]
    date_part = rest.split("/", 1)[0]
    return date_part if _DATE_RE.fullmatch(date_part) else None


def apply_retention(
    destination: Destination, *, org_id: str, retention_days: int, now: datetime
) -> dict[str, Any]:
    """Delete objects older than ``retention_days`` under this connector's own
    ``evidence/<org_id>/`` prefix. A non-positive ``retention_days`` disables
    deletion entirely (no retention knob turned on is the safe default).

    A marker and its pair share the same date segment by construction (both
    are named from one ``date_segment`` computed once per ``run_once`` call),
    so this single date-keyed sweep already treats "marker + pair" as one
    unit — they cross the cutoff together — and also reaps any uncommitted
    orphan pair (a pair whose run never got to write its marker) once its
    date segment is old enough, with no special-casing needed for either
    case (task #1266 review finding 2).
    """
    if retention_days <= 0:
        return {"applied": False, "cutoff": None, "deleted": []}
    safe_org = _sanitize_segment(org_id, label="org_id")
    prefix = f"{_PREFIX_ROOT}/{safe_org}/"
    cutoff = (now - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    deleted: list[str] = []
    for key in destination.list_prefix(prefix):
        date_segment = _date_segment_from_key(key, prefix)
        if date_segment is not None and date_segment < cutoff:
            destination.delete(key)
            deleted.append(key)
    return {"applied": True, "cutoff": cutoff, "deleted": sorted(deleted)}


def _retention_result(
    destination: Destination, *, org_id: str, retention_days: int, now: datetime
) -> dict[str, Any]:
    try:
        return {
            **apply_retention(destination, org_id=org_id, retention_days=retention_days, now=now),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - publication has already committed
        return {
            "applied": False,
            "cutoff": None,
            "deleted": [],
            "error": f"retention failed ({type(exc).__name__})",
        }


# ---------------------------------------------------------------------------
# The export itself
# ---------------------------------------------------------------------------


def _int_env(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name)
    if raw is None:
        return default
    if not raw.strip():
        raise DestinationConfigError(f"{name} must be an integer")
    try:
        return int(raw)
    except ValueError:
        raise DestinationConfigError(f"{name} must be an integer") from None


def _chain_head_only(conn: Any) -> str:
    """Cheap chain-head lookup, independent of the (possibly large) report
    body. Used to check the commit marker before ever building a report."""
    from .. import events

    info = events.verify_event_chain(conn)
    return str(info.get("head_hash") or "")


def _committed_pair(
    destination: Destination,
    marker_body: bytes | None,
    *,
    org_id: str,
    date_segment: str,
    chain_head: str,
    snapshot_fingerprint: str,
    window_days: int,
    key_material: tuple[bytes, str],
) -> dict[str, Any] | None:
    """Return a marker only when it names a present, correctly-scoped pair.

    Markers are hints that a publication finished, not permanent proof: an
    operator or bucket lifecycle policy may remove either immutable object.
    Invalid, stale, or malformed markers fall through to a rebuilding write.
    """
    if marker_body is None:
        return None
    try:
        committed = json.loads(marker_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(committed, dict):
        return None
    body_key = committed.get("key")
    sidecar_key = committed.get("sig_key")
    safe_org, safe_head = _base_prefix(org_id, date_segment, chain_head)
    body_prefix = f"{_PREFIX_ROOT}/{safe_org}/{date_segment}/{safe_head}-"
    if not isinstance(body_key, str) or not isinstance(sidecar_key, str):
        return None
    if not re.fullmatch(re.escape(body_prefix) + r"[0-9a-f]{16}\.json", body_key):
        return None
    if sidecar_key != body_key.removesuffix(".json") + ".sig.json":
        return None
    if committed.get("chain_head") != chain_head:
        return None
    body = destination.get(body_key)
    sidecar_body = destination.get(sidecar_key)
    if body is None or sidecar_body is None:
        return None
    if hashlib.sha256(body).hexdigest()[:16] != body_key.rsplit("-", 1)[-1].removesuffix(".json"):
        return None
    try:
        report = json.loads(body)
        sidecar = json.loads(sidecar_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(report, dict) or not isinstance(sidecar, dict):
        return None
    chain_integrity = report.get("chain_integrity")
    if (
        not isinstance(chain_integrity, dict)
        or str(chain_integrity.get("head_hash") or "") != chain_head
    ):
        return None
    if sidecar.get("chain_head") != chain_head or sidecar.get("key_id") != key_material[1]:
        return None
    if (
        _snapshot_fingerprint(report, window_days=window_days, signing_key_id=key_material[1])
        != snapshot_fingerprint
    ):
        return None
    if not _verify_export_with_key(body, sidecar, key_material):
        return None
    if committed.get("snapshot_fingerprint") != snapshot_fingerprint:
        return None
    return committed


def _stable_report(report: Any) -> Any:
    """Drop generation-only timestamps before fingerprinting report inputs."""
    if isinstance(report, dict):
        return {
            key: _stable_report(value)
            for key, value in report.items()
            if key not in {"generated_at", "attested_at"}
        }
    if isinstance(report, list):
        return [_stable_report(value) for value in report]
    return report


def _snapshot_fingerprint(report: dict[str, Any], *, window_days: int, signing_key_id: str) -> str:
    identity = {
        "report": _stable_report(report),
        "window_days": window_days,
        "signing_key_id": signing_key_id,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_export_with_key(
    body: bytes, sidecar: dict[str, Any], key_material: tuple[bytes, str]
) -> bool:
    if sidecar.get("schema") != evidence_signing.SIGNATURE_SCHEMA:
        return False
    if (
        sidecar.get("version") != "1"
        or sidecar.get("scheme") != evidence_signing.SCHEME_HMAC_SHA256_V1
    ):
        return False
    if sidecar.get("key_id") != key_material[1]:
        return False
    digest = hashlib.sha256(body).hexdigest()
    if sidecar.get("artifact_sha256") != digest:
        return False
    expected = hmac.new(key_material[0], body, hashlib.sha256).hexdigest()
    signature = sidecar.get("signature")
    return isinstance(signature, str) and hmac.compare_digest(expected, signature)


def _build_export_pair(
    conn: Any,
    *,
    org_id: str,
    date_segment: str,
    window_start: datetime,
    window_end: str | None = None,
    window_days: int = 0,
    env: Mapping[str, str] | None = None,
    key_material: tuple[bytes, str] | None = None,
) -> dict[str, Any]:
    """Build one signed export pair (JSON body + ``.sig.json`` sidecar) for
    window from ``window_start`` through the inclusive UTC date ``window_end``.
    Pure — never touches a
    ``Destination``; ``run_once`` decides when/whether to write the result,
    and tests exercising concurrent writers call this directly to build two
    independent pairs before interleaving their ``Destination.put`` calls
    (task #1266 review finding 2).

    Always reads the COMPLETE window (``complete=True`` — task #1266 review
    finding 1): a signed, customer-facing export must never silently
    truncate at the CLI/API's default 50/max-500 clamp.
    """
    if window_end is None:
        window_end = (date.fromisoformat(date_segment) - timedelta(days=1)).isoformat()
    report = evidence_report.build_evidence_report(
        since=window_start.isoformat(),
        # A date-only upper bound is inclusive through UTC end-of-day. It
        # remains fixed for this completed calendar date across same-day retries.
        until=window_end,
        complete=True,
        conn=conn,
    )
    chain_head = str((report.get("chain_integrity") or {}).get("head_hash") or "")
    if not evidence_signing.configured(env):
        raise evidence_signing.SigningUnavailable(
            "scheduled evidence export requires signing; set UA_EVIDENCE_SIGNING_KEY or "
            "UA_EVIDENCE_SIGNING_SECRET_NAME"
        )
    signing_material = key_material or evidence_signing._key_material(env)
    export_report = evidence_signing.with_report_metadata(report, key_material=signing_material)
    snapshot_fingerprint = _snapshot_fingerprint(
        export_report,
        window_days=window_days,
        signing_key_id=signing_material[1],
    )
    body = evidence_signing.encode_for_signing(
        evidence_report.format_evidence_report(export_report, fmt="json")
    )
    sidecar = evidence_signing.sign_export(
        body,
        chain_head=chain_head,
        generated_at=str(report.get("generated_at") or ""),
        key_material=signing_material,
    )
    sidecar_body = (json.dumps(sidecar, indent=2, sort_keys=True) + "\n").encode("utf-8")
    content_hash = hashlib.sha256(body).hexdigest()[:16]
    return {
        "chain_head": chain_head,
        "snapshot_fingerprint": snapshot_fingerprint,
        "signing_key_id": signing_material[1],
        "content_hash": content_hash,
        "json_key": object_key(org_id, date_segment, chain_head, content_hash, ".json"),
        "sig_key": object_key(org_id, date_segment, chain_head, content_hash, ".sig.json"),
        "body": body,
        "sidecar_body": sidecar_body,
        "records": report.get("total_records", 0),
        "complete": report.get("complete", False),
    }


def run_once(
    *,
    env: Mapping[str, str] | None = None,
    conn: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build, sign, and write one scheduled evidence export.

    Off by default (``UA_EVIDENCE_EXPORT_ENABLED`` unset/not ``"1"``) — a
    caller that wants an unconditional run (e.g. an operator smoke test) must
    still pass ``env={"UA_EVIDENCE_EXPORT_ENABLED": "1", ...}`` explicitly.

    Idempotent via a report fingerprint and fully verified commit pair, not
    marker presence or chain head alone (see the module docstring). One SQLite
    read transaction covers chain verification and every report query. The
    complete report is built before checking for a matching marker, so
    same-day changes to report inputs, window settings, or signing identity
    cannot reuse a stale artifact. A match returns ``status: "skipped"``
    without publishing new objects. Retention runs
    on both the skipped and exported paths (task #1266 review finding 3) —
    a quiet window should not also mean an indefinitely growing bucket.
    Wrapped in ``destination.run_lock()`` so two local runs serialize.

    The optional ``env`` mapping controls destination, schedule, organization,
    and signing settings consistently. Callers that omit it retain the usual
    process-environment behavior.
    """
    source = os.environ if env is None else env
    if not enabled(source):
        return {"status": "disabled", "reason": "UA_EVIDENCE_EXPORT_ENABLED is not set to '1'"}

    destination = destination_from_env(source)
    org_id = (source.get("UA_EVIDENCE_EXPORT_ORG_ID") or "default").strip() or "default"
    retention_days = _int_env(source, "UA_EVIDENCE_EXPORT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
    window_days = _int_env(source, "UA_EVIDENCE_EXPORT_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)
    if window_days < 0:
        raise DestinationConfigError("UA_EVIDENCE_EXPORT_WINDOW_DAYS must be zero or greater")

    now = (now or datetime.now(UTC)).astimezone(UTC)
    # Export only completed UTC dates. This ensures a daily 02:00 run picks up
    # the prior date in full, including rows created after the previous run.
    # Positive N covers exactly N inclusive dates ending yesterday; zero is
    # deliberately an empty report window.
    window_end_date = now.date() - timedelta(days=1)
    window_start_date = (
        window_end_date - timedelta(days=window_days - 1)
        if window_days > 0
        else window_end_date + timedelta(days=1)
    )
    window_start = datetime.combine(window_start_date, datetime.min.time(), tzinfo=now.tzinfo)
    # Retention measures artifact age, not the age of the evidence window.
    # Keep the report's ``since`` bound independent from this storage date.
    date_segment = now.strftime("%Y-%m-%d")

    if not evidence_signing.configured(source):
        raise evidence_signing.SigningUnavailable(
            "scheduled evidence export requires signing; set UA_EVIDENCE_SIGNING_KEY or "
            "UA_EVIDENCE_SIGNING_SECRET_NAME"
        )
    signing_material = evidence_signing._key_material(source)

    close_conn = False
    if conn is None:
        from .. import storage

        post_init.register_default_hooks()
        storage.init_db()
        conn = storage.connect()
        close_conn = True
    try:
        with destination.run_lock():
            owns_read_transaction = not conn.in_transaction
            if owns_read_transaction:
                conn.execute("BEGIN")
            try:
                cheap_chain_head = _chain_head_only(conn)
                pair = _build_export_pair(
                    conn,
                    org_id=org_id,
                    date_segment=date_segment,
                    window_start=window_start,
                    window_end=window_end_date.isoformat(),
                    window_days=window_days,
                    env=source,
                    key_material=signing_material,
                )
                if owns_read_transaction:
                    conn.commit()
            except BaseException:
                if owns_read_transaction and conn.in_transaction:
                    conn.rollback()
                raise

            marker = marker_key(org_id, date_segment, cheap_chain_head)
            existing_marker = destination.get(marker)
            committed_pair = _committed_pair(
                destination,
                existing_marker,
                org_id=org_id,
                date_segment=date_segment,
                chain_head=cheap_chain_head,
                snapshot_fingerprint=pair["snapshot_fingerprint"],
                window_days=window_days,
                key_material=signing_material,
            )
            if committed_pair is not None:
                retention_result = _retention_result(
                    destination, org_id=org_id, retention_days=retention_days, now=now
                )
                status = (
                    "skipped"
                    if retention_result["error"] is None
                    else "skipped_with_retention_error"
                )
                return {
                    "status": status,
                    "reason": "export already committed for this report snapshot",
                    "chain_head": cheap_chain_head,
                    "marker_key": marker,
                    "committed": committed_pair,
                    "retention": retention_result,
                }

            destination.put(pair["json_key"], pair["body"], content_type="application/json")
            destination.put(pair["sig_key"], pair["sidecar_body"], content_type="application/json")

            committed_marker_key = marker_key(org_id, date_segment, pair["chain_head"])
            marker_body = (
                json.dumps(
                    {
                        "key": pair["json_key"],
                        "sig_key": pair["sig_key"],
                        "chain_head": pair["chain_head"],
                        "snapshot_fingerprint": pair["snapshot_fingerprint"],
                        "committed_at": now.isoformat(),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            destination.put(committed_marker_key, marker_body, content_type="application/json")

            retention_result = _retention_result(
                destination, org_id=org_id, retention_days=retention_days, now=now
            )

            return {
                "status": "exported"
                if retention_result["error"] is None
                else "exported_with_retention_error",
                "chain_head": pair["chain_head"],
                "key": pair["json_key"],
                "sig_key": pair["sig_key"],
                "marker_key": committed_marker_key,
                "bytes": len(pair["body"]),
                "records": pair["records"],
                "complete": pair["complete"],
                "retention": retention_result,
            }
    finally:
        if close_conn:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    """Scheduler-friendly CLI entry point (``python -m unified_agent.audit.evidence_export_sink``,
    the ``ExecStart=`` of ``deploy/systemd/ua-evidence-export.service``)."""
    del argv  # No flags yet — all configuration is env-var driven, like vault_sync's service unit.
    try:
        result = run_once()
    except (DestinationError, evidence_signing.SigningUnavailable) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in ("exported", "skipped", "disabled") else 1


if __name__ == "__main__":
    raise SystemExit(main())
