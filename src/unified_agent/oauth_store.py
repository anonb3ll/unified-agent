"""Durable backing for the hub OAuth authorization server (task #1321, Epic J2).

``oauth.HubOAuthProvider`` used to keep everything in process dicts, so a hub
restart logged every device out. This module persists the two things that must
survive a restart:

* **Registered clients** (RFC 7591 dynamic registrations). The client metadata
  is stored as JSON; a confidential client's secret is stored only as a SHA-256
  digest. After a restart the provider verifies a presented secret against that
  digest (see ``oauth._presented_client_secret``) instead of holding plaintext.
* **Refresh tokens**, keyed by the SHA-256 of the token value. No column can
  hold a plaintext refresh token. The device binding (tailnet node) and auth
  method ride along so rotation after a restart keeps them.

* **Access tokens**, also keyed by SHA-256 and linked to the refresh token that
  minted them, so a client holding a still-valid access token keeps working
  across a restart instead of getting 401 until its (up to week-long) expiry.

Pending RFC 8628 device grants stay in memory — they live ten minutes and are
re-started by ``unified-agent login`` in one step.

Every function here is safe to call with no hub DB: callers treat a
``sqlite3.Error`` as "persistence unavailable" and keep the in-memory path.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from . import storage

# Anonymous dynamic registration must not grow the hub DB without bound.
MAX_PERSISTED_CLIENTS = 2000
# A registered client with no live refresh token is pruned after this long.
STALE_CLIENT_S = 30 * 24 * 3600
# Public session handle shown to operators: a prefix of the token digest.
SESSION_ID_LEN = 16

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY,
  client_json TEXT NOT NULL,
  secret_sha256 TEXT,
  registered_at INTEGER NOT NULL,
  last_used_at INTEGER
);
CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
  token_sha256 TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  subject TEXT,
  scopes TEXT NOT NULL DEFAULT '[]',
  resource TEXT,
  expires_at INTEGER,
  claims TEXT NOT NULL DEFAULT '{}',
  device_node TEXT,
  device_node_id TEXT,
  issued_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_access_tokens (
  token_sha256 TEXT PRIMARY KEY,
  refresh_sha256 TEXT,
  client_id TEXT NOT NULL,
  subject TEXT,
  scopes TEXT NOT NULL DEFAULT '[]',
  resource TEXT,
  expires_at INTEGER,
  claims TEXT NOT NULL DEFAULT '{}',
  device_node TEXT,
  device_node_id TEXT,
  issued_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_passkeys (
  credential_id TEXT PRIMARY KEY,
  login TEXT NOT NULL,
  rp_id TEXT NOT NULL,
  public_key_spki TEXT NOT NULL,
  alg INTEGER NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0,
  label TEXT,
  registered_by TEXT,
  created_at INTEGER NOT NULL,
  last_used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_oauth_access_refresh ON oauth_access_tokens(refresh_sha256);
CREATE INDEX IF NOT EXISTS idx_oauth_access_device ON oauth_access_tokens(device_node_id);
CREATE INDEX IF NOT EXISTS idx_oauth_access_expires ON oauth_access_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_client ON oauth_refresh_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_device ON oauth_refresh_tokens(device_node_id);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_expires ON oauth_refresh_tokens(expires_at);
"""


def ensure_schema(conn) -> None:
    """Create the tables (called from ``storage._migrate``; idempotent)."""
    conn.executescript(_SCHEMA)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


# Process-local record of failed durable writes, so the doctor can tell a hub
# whose new sign-ins live only in memory from a healthy one.
_write_failures: dict[str, Any] = {"count": 0, "last_at": None, "last_kind": None}


def note_write_failure(kind: str) -> None:
    _write_failures["count"] += 1
    _write_failures["last_at"] = _now()
    _write_failures["last_kind"] = kind


def write_failure_status() -> dict[str, Any]:
    return dict(_write_failures)


def reset_write_failures() -> None:
    _write_failures.update({"count": 0, "last_at": None, "last_kind": None})


# -- clients -----------------------------------------------------------------


def save_client(client_id: str, client_json: dict[str, Any], secret: str | None) -> bool:
    """Persist one registered client. Returns False when the cap refuses it."""
    body = {k: v for k, v in client_json.items() if k != "client_secret"}
    conn = storage.connect()
    try:
        with conn:
            count = conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
            if count >= MAX_PERSISTED_CLIENTS:
                _prune_stale_clients(conn, _now())
                count = conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
                if count >= MAX_PERSISTED_CLIENTS:
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO oauth_clients"
                " (client_id, client_json, secret_sha256, registered_at) VALUES (?, ?, ?, ?)",
                (client_id, json.dumps(body), digest(secret) if secret else None, _now()),
            )
        return True
    finally:
        conn.close()


def ensure_client(
    client_id: str,
    client_json: dict[str, Any],
    *,
    secret: str | None,
    secret_digest: str | None,
) -> None:
    """Insert a client row only if it is missing (never overwrites a live row)."""
    body = {k: v for k, v in client_json.items() if k != "client_secret"}
    stored_digest = digest(secret) if secret else secret_digest
    conn = storage.connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO oauth_clients"
                " (client_id, client_json, secret_sha256, registered_at, last_used_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (client_id, json.dumps(body), stored_digest, _now(), _now()),
            )
    finally:
        conn.close()


def load_clients() -> list[tuple[dict[str, Any], str | None]]:
    """Every persisted client as ``(metadata_without_secret, secret_sha256)``."""
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT client_json, secret_sha256 FROM oauth_clients ORDER BY registered_at"
        ).fetchall()
    finally:
        conn.close()
    out: list[tuple[dict[str, Any], str | None]] = []
    for row in rows:
        try:
            out.append((json.loads(row["client_json"]), row["secret_sha256"]))
        except (TypeError, ValueError):
            continue
    return out


def load_client(client_id: str) -> tuple[dict[str, Any], str | None] | None:
    """One persisted client, for a lazy reload after a missed startup restore."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT client_json, secret_sha256 FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return json.loads(row["client_json"]), row["secret_sha256"]
    except (TypeError, ValueError):
        return None


def _prune_stale_clients(conn, now: int) -> int:
    # Age is measured from the client's last token issue, not its registration:
    # a client that keeps signing in is never pruned.
    cur = conn.execute(
        "DELETE FROM oauth_clients WHERE COALESCE(last_used_at, registered_at) < ?"
        " AND client_id NOT IN (SELECT DISTINCT client_id FROM oauth_refresh_tokens)",
        (now - STALE_CLIENT_S,),
    )
    return cur.rowcount or 0


# -- refresh tokens ----------------------------------------------------------


def save_refresh(
    token: str,
    *,
    client_id: str,
    subject: str | None,
    scopes: list[str],
    resource: str | None,
    expires_at: int | None,
    claims: dict[str, Any],
) -> None:
    node = claims.get("device_node") if isinstance(claims.get("device_node"), dict) else {}
    conn = storage.connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_refresh_tokens"
                " (token_sha256, client_id, subject, scopes, resource, expires_at, claims,"
                "  device_node, device_node_id, issued_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(token),
                    client_id,
                    subject,
                    json.dumps(list(scopes)),
                    resource,
                    expires_at,
                    json.dumps(claims),
                    (node or {}).get("node") or None,
                    (node or {}).get("node_id") or None,
                    _now(),
                ),
            )
            conn.execute(
                "UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?",
                (_now(), client_id),
            )
    finally:
        conn.close()


def _row_to_refresh(row) -> dict[str, Any]:
    try:
        scopes = json.loads(row["scopes"] or "[]")
    except ValueError:
        scopes = []
    try:
        claims = json.loads(row["claims"] or "{}")
    except ValueError:
        claims = {}
    return {
        "client_id": row["client_id"],
        "subject": row["subject"],
        "scopes": scopes,
        "resource": row["resource"],
        "expires_at": row["expires_at"],
        "claims": claims,
    }


def load_refresh(token: str) -> dict[str, Any] | None:
    """The stored record for a presented refresh token, or None."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE token_sha256 = ?", (digest(token),)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_refresh(row) if row is not None else None


def consume_refresh(token: str, client_id: str | None = None) -> dict[str, Any] | None:
    """Atomically delete and return a refresh record (single-use rotation).

    With ``client_id`` only that client's token is consumed: another client
    presenting it gets None and the owner's session is left intact. The access
    tokens it minted are deleted in the same transaction.
    """
    key = digest(token)
    conn = storage.connect()
    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM oauth_refresh_tokens WHERE token_sha256 = ?", (key,)
            ).fetchone()
            if row is None or (client_id is not None and row["client_id"] != client_id):
                return None
            cur = conn.execute("DELETE FROM oauth_refresh_tokens WHERE token_sha256 = ?", (key,))
            if not cur.rowcount:
                return None
            conn.execute("DELETE FROM oauth_access_tokens WHERE refresh_sha256 = ?", (key,))
        return _row_to_refresh(row)
    finally:
        conn.close()


def rotate_refresh(
    token: str,
    *,
    client_id: str,
    new_token: str,
    scopes: list[str] | None,
    expires_at: int | None = None,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Single-use rotation in one transaction: consume ``token``, store ``new_token``.

    Returns the consumed record plus ``rotated`` (True when the new row was
    written), or None when the token is absent or another client's (which is
    then left intact, as in ``consume_refresh``). An expired token is consumed
    and nothing replaces it. The new row carries the old row's subject,
    resource, device binding and claims (``signin_id`` included), so a sign-in
    family never has a moment with no durable row (task #1744).
    """
    key = digest(token)
    ts = now if now is not None else _now()
    conn = storage.connect()
    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM oauth_refresh_tokens WHERE token_sha256 = ?", (key,)
            ).fetchone()
            if row is None or row["client_id"] != client_id:
                return None
            cur = conn.execute("DELETE FROM oauth_refresh_tokens WHERE token_sha256 = ?", (key,))
            if not cur.rowcount:
                return None
            conn.execute("DELETE FROM oauth_access_tokens WHERE refresh_sha256 = ?", (key,))
            record = _row_to_refresh(row)
            live = record["expires_at"] is None or record["expires_at"] >= ts
            if live:
                conn.execute(
                    "INSERT INTO oauth_refresh_tokens"
                    " (token_sha256, client_id, subject, scopes, resource, expires_at, claims,"
                    "  device_node, device_node_id, issued_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        digest(new_token),
                        row["client_id"],
                        row["subject"],
                        json.dumps(list(scopes or record["scopes"])),
                        row["resource"],
                        expires_at,
                        row["claims"],
                        row["device_node"],
                        row["device_node_id"],
                        ts,
                    ),
                )
                conn.execute(
                    "UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?",
                    (ts, row["client_id"]),
                )
        return {**record, "rotated": live}
    finally:
        conn.close()


def delete_refresh(token: str) -> None:
    delete_refresh_digest(digest(token))


def delete_refresh_digest(token_sha256: str) -> None:
    """Delete a refresh token and every access token it minted."""
    conn = storage.connect()
    try:
        with conn:
            conn.execute("DELETE FROM oauth_refresh_tokens WHERE token_sha256 = ?", (token_sha256,))
            conn.execute(
                "DELETE FROM oauth_access_tokens WHERE refresh_sha256 = ?", (token_sha256,)
            )
    finally:
        conn.close()


# -- access tokens -----------------------------------------------------------


def save_access(
    token: str,
    *,
    refresh_token: str | None,
    client_id: str,
    subject: str | None,
    scopes: list[str],
    resource: str | None,
    expires_at: int | None,
    claims: dict[str, Any],
    issued_at: int,
) -> None:
    node = claims.get("device_node") if isinstance(claims.get("device_node"), dict) else {}
    conn = storage.connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_access_tokens"
                " (token_sha256, refresh_sha256, client_id, subject, scopes, resource,"
                "  expires_at, claims, device_node, device_node_id, issued_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(token),
                    digest(refresh_token) if refresh_token else None,
                    client_id,
                    subject,
                    json.dumps(list(scopes)),
                    resource,
                    expires_at,
                    json.dumps(claims),
                    (node or {}).get("node") or None,
                    (node or {}).get("node_id") or None,
                    issued_at,
                ),
            )
    finally:
        conn.close()


def load_access(token: str) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM oauth_access_tokens WHERE token_sha256 = ?", (digest(token),)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    record = _row_to_refresh(row)
    record["issued_at"] = row["issued_at"]
    record["refresh_sha256"] = row["refresh_sha256"]
    return record


def delete_access(token: str) -> str | None:
    """Delete one access token; return the digest of the refresh that minted it."""
    key = digest(token)
    conn = storage.connect()
    try:
        with conn:
            row = conn.execute(
                "SELECT refresh_sha256 FROM oauth_access_tokens WHERE token_sha256 = ?", (key,)
            ).fetchone()
            conn.execute("DELETE FROM oauth_access_tokens WHERE token_sha256 = ?", (key,))
    finally:
        conn.close()
    return row["refresh_sha256"] if row is not None else None


def prune_expired(now: int | None = None) -> dict[str, int]:
    """Drop expired refresh tokens, then clients left with none that are stale."""
    ts = now if now is not None else _now()
    conn = storage.connect()
    try:
        with conn:
            refresh = (
                conn.execute(
                    "DELETE FROM oauth_refresh_tokens"
                    " WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (ts,),
                ).rowcount
                or 0
            )
            conn.execute(
                "DELETE FROM oauth_access_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (ts,),
            )
            clients = _prune_stale_clients(conn, ts)
    finally:
        conn.close()
    return {"refresh_tokens": refresh, "clients": clients}


# -- operator views ----------------------------------------------------------


def list_sessions(now: int | None = None) -> list[dict[str, Any]]:
    """Live refresh-token sessions, newest first. Never includes a token value."""
    ts = now if now is not None else _now()
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT token_sha256, client_id, subject, claims, device_node, device_node_id,"
            " issued_at, expires_at FROM oauth_refresh_tokens"
            " WHERE expires_at IS NULL OR expires_at >= ? ORDER BY issued_at DESC",
            (ts,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            claims = json.loads(row["claims"] or "{}")
        except ValueError:
            claims = {}
        out.append(
            {
                "session_id": row["token_sha256"][:SESSION_ID_LEN],
                "client_id": row["client_id"],
                "agent": row["subject"],
                "auth_method": claims.get("auth_method"),
                "device_node": row["device_node"],
                "device_node_id": row["device_node_id"],
                "issued_at": row["issued_at"],
                "expires_at": row["expires_at"],
            }
        )
    return out


def expand_device(conn, device: str) -> tuple[set[str], set[str]]:
    """The node ids and names a revoke of ``device`` (a node id or name) reaches.

    A node id is the stable identity: a session bound to a *different*
    non-empty id is never reached through a shared name (a machine can rejoin
    under a new id, and two nodes can report the same name). Name matching
    applies only to name-only sessions (no id), using ``device`` itself and the
    names recorded on rows bound to ``device`` as an id.
    """
    ids, names = {device}, {device}
    for row in conn.execute(
        "SELECT DISTINCT device_node FROM oauth_refresh_tokens"
        " WHERE device_node_id = ? AND device_node IS NOT NULL",
        (device,),
    ).fetchall():
        names.add(row["device_node"])
    # ``device`` given as a name: it reaches the one node id carrying it. A name
    # carried by several ids is ambiguous and refused rather than widened.
    named_ids = {
        row["device_node_id"]
        for row in conn.execute(
            "SELECT DISTINCT device_node_id FROM oauth_refresh_tokens"
            " WHERE device_node = ? AND device_node_id IS NOT NULL AND device_node_id != ''",
            (device,),
        ).fetchall()
    } - {device}
    if len(named_ids) > 1:
        raise ValueError(
            f"device name {device!r} is carried by {len(named_ids)} node ids; revoke by node id"
        )
    ids |= named_ids
    return ids, names


def device_scope(device: str) -> tuple[set[str], set[str]]:
    conn = storage.connect()
    try:
        return expand_device(conn, device)
    finally:
        conn.close()


def on_device(ids: set[str], names: set[str], node: dict[str, Any] | None) -> bool:
    """True when a token's device binding falls inside a revoke's scope."""
    node = node or {}
    node_id = node.get("node_id") or None
    if node_id:
        return node_id in ids
    return bool(node.get("node")) and node.get("node") in names


def _device_where(ids: set[str], names: set[str]) -> tuple[str, tuple[str, ...]]:
    id_marks = ",".join("?" * len(ids))
    name_marks = ",".join("?" * len(names))
    clause = (
        f"(device_node_id IN ({id_marks}) OR ((device_node_id IS NULL OR device_node_id = '')"
        f" AND device_node IN ({name_marks})))"
    )
    return clause, (*sorted(ids), *sorted(names))


def revoke_sessions(
    *, device: str | None = None, session_id: str | None = None
) -> list[dict[str, Any]]:
    """Delete the refresh tokens for one device (node name or id) or one session.

    A session revoke takes every row of that session's sign-in family (same
    subject and ``signin_id``), see ``_signin_family_rows``.

    Returns the revoked rows (``session_id``, ``client_id``, ``agent``,
    ``device_node``, ``device_node_id``) so the caller can drop matching
    in-memory access tokens and write the audit event.
    """
    if bool(device) == bool(session_id):
        raise ValueError("pass exactly one of device or session_id")
    conn = storage.connect()
    try:
        with conn:
            if device:
                ids, names = expand_device(conn, device)
                where, params = _device_where(ids, names)
                rows = conn.execute(
                    "SELECT token_sha256, client_id, subject, device_node, device_node_id"
                    f" FROM oauth_refresh_tokens WHERE {where}",
                    params,
                ).fetchall()
            else:
                # Hex only: a LIKE wildcard (% or _) must never widen the match.
                if not re.fullmatch(rf"[0-9a-f]{{{SESSION_ID_LEN},64}}", session_id or ""):
                    raise ValueError(f"session_id must be {SESSION_ID_LEN}+ hex characters")
                rows = conn.execute(
                    "SELECT token_sha256, client_id, subject, claims, device_node,"
                    " device_node_id FROM oauth_refresh_tokens WHERE token_sha256 LIKE ?",
                    (f"{session_id}%",),
                ).fetchall()
                rows = _signin_family_rows(conn, rows)
            conn.executemany(
                "DELETE FROM oauth_refresh_tokens WHERE token_sha256 = ?",
                [(r["token_sha256"],) for r in rows],
            )
            # Access tokens minted from those sessions, plus (for a device) any
            # access token bound to the device directly.
            conn.executemany(
                "DELETE FROM oauth_access_tokens WHERE refresh_sha256 = ?",
                [(r["token_sha256"],) for r in rows],
            )
            if device:
                conn.execute(f"DELETE FROM oauth_access_tokens WHERE {where}", params)
    finally:
        conn.close()
    return [
        {
            "token_sha256": r["token_sha256"],
            "session_id": r["token_sha256"][:SESSION_ID_LEN],
            "client_id": r["client_id"],
            "agent": r["subject"],
            "device_node": r["device_node"],
            "device_node_id": r["device_node_id"],
        }
        for r in rows
    ]


def revoke_for_agent(conn, subject: str) -> list[dict[str, Any]]:
    """Delete every device sign-in of ``subject`` on the caller's transaction (task #2315).

    A sign-in is a refresh-token row and the access tokens minted from it;
    access tokens issued to ``subject`` with no refresh row (client
    credentials) go too. ``subject`` matches case-insensitively: a token issued
    under a case variant of the label is the same identity. Nothing is committed here: an agent retirement runs
    this inside its own write transaction, so the delete and the retirement
    commit or roll back together. Returns one row per deleted token
    (``token_sha256``, ``session_id``, ``client_id``, ``device_node``,
    ``device_node_id``, ``kind``) so the caller can drop in-memory copies.
    """
    refresh = conn.execute(
        "SELECT token_sha256, client_id, device_node, device_node_id"
        " FROM oauth_refresh_tokens WHERE subject = ? COLLATE NOCASE",
        (subject,),
    ).fetchall()
    access = conn.execute(
        "SELECT token_sha256, client_id, device_node, device_node_id"
        " FROM oauth_access_tokens WHERE subject = ? COLLATE NOCASE OR refresh_sha256 IN"
        " (SELECT token_sha256 FROM oauth_refresh_tokens WHERE subject = ? COLLATE NOCASE)",
        (subject, subject),
    ).fetchall()
    conn.execute(
        "DELETE FROM oauth_access_tokens WHERE subject = ? COLLATE NOCASE OR refresh_sha256 IN"
        " (SELECT token_sha256 FROM oauth_refresh_tokens WHERE subject = ? COLLATE NOCASE)",
        (subject, subject),
    )
    conn.execute("DELETE FROM oauth_refresh_tokens WHERE subject = ? COLLATE NOCASE", (subject,))
    return [
        {
            "token_sha256": row["token_sha256"],
            "session_id": row["token_sha256"][:SESSION_ID_LEN],
            "client_id": row["client_id"],
            "device_node": row["device_node"],
            "device_node_id": row["device_node_id"],
            "kind": kind,
        }
        for kind, rows in (("refresh", refresh), ("access", access))
        for row in rows
    ]


def live_signins_for(conn, subject: str, now: int | None = None) -> int:
    """Live (unexpired) refresh-token sign-ins and access tokens held by ``subject``."""
    ts = now if now is not None else _now()
    total = 0
    for table in ("oauth_refresh_tokens", "oauth_access_tokens"):
        total += conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE subject = ? COLLATE NOCASE"
            " AND (expires_at IS NULL OR expires_at >= ?)",
            (subject, ts),
        ).fetchone()[0]
    return int(total)


#: A row's ``signin_id`` claim, NULL for a row whose claims are not valid JSON.
#: ``json_extract`` raises on malformed JSON, which would fail the whole query
#: and so refuse every sign-in of the label; the CASE keeps the failure to that
#: one row, which then never matches (task #1893).
_SIGNIN_ID_SQL = "(CASE WHEN json_valid(claims) THEN json_extract(claims, '$.signin_id') END)"


def _signin_family_rows(conn, rows: list) -> list:
    """Every refresh row of the sign-in families ``rows`` belong to.

    A family normally has one row: rotation replaces it in one transaction. A
    store-down rotation can leave two (the old row, whose consume failed, and
    the successor the memory fallback saved), so a single-session revoke takes
    the whole family rather than leaving its twin alive (task #1893).
    """
    seen = {row["token_sha256"] for row in rows}
    family = list(rows)
    for row in rows:
        try:
            signin_id = json.loads(row["claims"] or "{}").get("signin_id")
        except (ValueError, AttributeError):
            continue
        if not isinstance(signin_id, str) or not signin_id:
            continue
        for twin in conn.execute(
            "SELECT token_sha256, client_id, subject, claims, device_node, device_node_id"
            f" FROM oauth_refresh_tokens WHERE subject IS ? AND {_SIGNIN_ID_SQL} = ?",
            (row["subject"], signin_id),
        ).fetchall():
            if twin["token_sha256"] not in seen:
                seen.add(twin["token_sha256"])
                family.append(twin)
    return family


def live_signin_exists(
    subject: str,
    signin_id: str | None,
    *,
    now: int | None = None,
    conn=None,
) -> bool:
    """Whether ``subject`` still holds the device sign-in ``signin_id``.

    A device-code sign-in gets one random ``signin_id`` claim at issue, which
    every refresh rotation carries forward, so this is one sign-in family:
    not the label's other sign-ins, and not another node reporting the same
    name. Session-scoped MCP credentials (task #1744) stay valid only while it
    lives; a device or session revoke deletes its rows. No id, no sign-in.
    """
    if not signin_id:
        return False
    ts = now if now is not None else _now()
    own = conn is None
    conn = conn or storage.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM oauth_refresh_tokens WHERE subject = ?"
            f" AND {_SIGNIN_ID_SQL} = ?"
            " AND (expires_at IS NULL OR expires_at >= ?) LIMIT 1",
            (subject, signin_id, ts),
        ).fetchone()
    finally:
        if own:
            conn.close()
    return row is not None


def counts(now: int | None = None) -> dict[str, int]:
    """Row counts for the doctor ``oauth_store`` row."""
    ts = now if now is not None else _now()
    conn = storage.connect()
    try:
        clients = conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
        live = conn.execute(
            "SELECT COUNT(*) FROM oauth_refresh_tokens WHERE expires_at IS NULL OR expires_at >= ?",
            (ts,),
        ).fetchone()[0]
        devices = conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(device_node_id, device_node))"
            " FROM oauth_refresh_tokens"
            " WHERE (device_node_id IS NOT NULL OR device_node IS NOT NULL)"
            " AND (expires_at IS NULL OR expires_at >= ?)",
            (ts,),
        ).fetchone()[0]
    finally:
        conn.close()
    return {"clients": int(clients), "refresh_tokens": int(live), "devices": int(devices)}
