"""Agent bearer-token lifecycle as a first-class platform feature (task #1198).

Why this exists
----------------
Nine distinct bearer tokens were disclosed in a probe log during the
2026-09-18 utilization audit (#1126). An earlier "do not rotate" decision
(#346, 2026-08-10) was premised on no exposure having occurred; that premise
changed, so rotation needs to be a supported, auditable operation rather than
a manual env-var edit nobody can prove happened.

This module builds the *capability*. It never rotates a live token itself —
that stays a deliberate, coordinated operator action (editing the hub's
``UA_AGENT_TOKENS``, restarting, verifying old tokens 401). What this module
gives that operation is: a durable record of when a token was issued and by
whom, an expiry-age warning surfaced through ``system_doctor``, and a
hash-chained evidence-ledger entry every time a rotation is recorded.

Hash-only guarantee
--------------------
No function in this module accepts a raw token value except to pass it
straight through, once, to ``profiles.managed_secret_set`` — the existing,
approval-gated secret-storage path (task requirement: reuse it, don't build a
parallel one). This module never independently hashes, stores, or logs the
raw value; it only ever holds the ``digest`` that ``managed_secret_set``
already computes and returns (``managed_secrets._digest``, SHA-256 hex). The
``agent_tokens`` table has no column capable of holding a plaintext token —
only ``token_hash`` — and every function here returns that same digest, never
the input value. ``RedactedValue`` wraps the raw value between the caller and
that one hand-off so an accidental ``str(value)``/``repr(value)``/f-string
elsewhere in a call chain (including a future one) renders as
``<redacted-token-value>`` instead of the secret.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import (
    agent_access,
    agent_lifecycle,
    config,
    credential_fence,
    edition,
    events,
    managed_secrets,
    storage,
)
from . import profiles as profile_tools

_VALID_STATUSES = ("active", "rotated", "revoked", "expired")


@contextlib.contextmanager
def _claim_lock():
    """Serialize token lifecycle and deployment_claim operations across threads and processes."""
    # A bearer change must not land between a policy publication's final
    # verification and its POST (PR #1302 review). The fence comes first: one
    # lock order everywhere, and it is re-entrant, so the managed-secret write
    # below (which takes it too) cannot deadlock on this thread's own hold.
    with credential_fence.authority_fence():
        lock_path = config.data_home() / ".deployment_claim.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def credential_lock():
    """The lock token lifecycle writes take; agent retirement takes it too (task #2305)."""
    return _claim_lock()


def revoke_for_agent(conn: Any, agent_label: str, *, revoked_at: str) -> list[int]:
    """Mark ``agent_label``'s active ``agent_tokens`` rows revoked in the caller's transaction.

    Used by agent retirement (task #2305). A managed bearer stops authenticating
    on the next request, because ``config.agent_tokens`` merges active rows
    only. It does not touch ``UA_AGENT_TOKENS``: a value listed there keeps
    authenticating until the operator removes it (``system_doctor`` row
    ``retired_agent_credentials`` reports that). The secret value itself stays
    in the managed store; deleting it is the operator's credential step.
    Returns the revoked row ids.
    """
    import sqlite3

    # A row stored under a case variant of the label is the same identity.
    where = "agent_label = ? COLLATE NOCASE AND status = 'active' AND org_id IS ?"
    params = (agent_label, edition.tenant_org_id())
    try:
        rows = conn.execute(f"SELECT id FROM agent_tokens WHERE {where} ORDER BY id", params)
        ids = [int(r["id"]) for r in rows.fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    if ids:
        conn.execute(
            f"UPDATE agent_tokens SET status = 'revoked', revoked_at = ? WHERE {where}",
            (revoked_at, *params),
        )
    return ids


class RedactedValue:
    """Wraps a raw secret so logging/printing it by accident cannot leak it.

    ``.reveal()`` is the only way out, and it exists to be called exactly
    once, at the single hand-off to ``managed_secret_set``.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ToolError("token value must be a non-empty string")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<redacted-token-value>"

    __str__ = __repr__


def _coerce(value: RedactedValue | str) -> RedactedValue:
    return value if isinstance(value, RedactedValue) else RedactedValue(value)


def _row_to_dict(row: Any) -> dict:
    return {
        "id": row["id"],
        "agent_label": row["agent_label"],
        "token_hash": row["token_hash"],
        "hash_algo": row["hash_algo"],
        "status": row["status"],
        "issued_at": row["issued_at"],
        "expires_at": row["expires_at"],
        "rotated_from": row["rotated_from"],
        "rotated_at": row["rotated_at"],
        "revoked_at": row["revoked_at"],
        "issued_by": row["issued_by"],
        "note": row["note"],
    }


def _expires_at(ttl_days: int | None, issued_at: str) -> str | None:
    if ttl_days is None:
        return None
    if ttl_days <= 0:
        raise ToolError("ttl_days must be positive when given")
    issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    return (issued + timedelta(days=ttl_days)).isoformat().replace("+00:00", "Z")


def agent_token_issue(
    agent_label: str,
    value: str,
    *,
    ttl_days: int | None = None,
    note: str | None = None,
) -> dict:
    """Record a new agent bearer token's metadata; never returns the raw value.

    ``value`` is handed once to ``managed_secret_set`` (consumer
    ``"agent_token"``) — the same approval-gated, digest-returning secret
    store already used by the Zulip credential path — and only the resulting
    digest is persisted here.

    This issues a live hub credential, not just its metadata:
    ``config.agent_tokens()``, which the HTTP bearer check calls on every
    request, merges active, unexpired managed tokens, so ``value``
    authenticates as ``agent_label`` from the next request on (task #1826).
    ``UA_AGENT_TOKENS`` is not edited; its entries keep authenticating
    alongside managed ones.
    """
    with _claim_lock():
        return _record_issue(agent_label, value, ttl_days=ttl_days, note=note, rotated_from=None)


#: The session-credential prefix (``session_credentials.PREFIX``). The bearer
#: check routes a value with it to that leg, so a minted bearer never starts so.
_SESSION_TOKEN_PREFIX = "uas_"


def _mint_value() -> RedactedValue:
    import secrets

    while True:
        value = secrets.token_urlsafe(32)
        if not value.startswith(_SESSION_TOKEN_PREFIX):
            return RedactedValue(value)


def sealed_at_rest() -> bool:
    """Whether the managed store encrypts a bearer it keeps (``UA_MANAGED_SECRET_KEYS``)."""
    return managed_secrets.encryption_enabled()


def require_sealed_at_rest(allow_plaintext: bool = False) -> None:
    """Refuse an operator issue that would store the bearer unencrypted.

    Without ``UA_MANAGED_SECRET_KEYS`` the managed store writes the value
    verbatim into a mode-0600 file. ``allow_plaintext`` is the operator's
    explicit opt-in (``unified-agent agent add --allow-plaintext-at-rest``).
    """
    if allow_plaintext or sealed_at_rest():
        return
    raise ToolError(
        "UA_MANAGED_SECRET_KEYS is not set, so the managed secret store would keep this "
        "bearer unencrypted, verbatim, in a mode-0600 file. Set UA_MANAGED_SECRET_KEYS "
        "(Fernet keys) in the hub's environment, or pass --allow-plaintext-at-rest to "
        "accept a file-permission-only bearer"
    )


#: Kinds an operator-issued token row may carry (``agent_tokens.agent_kind``).
TOKEN_KINDS = ("local", "cloud")


def operator_issue(
    agent_label: str,
    *,
    actor: str,
    ttl_days: int | None = None,
    note: str | None = None,
    agent_kind: str = "local",
    allow_plaintext_at_rest: bool = False,
) -> tuple[RedactedValue, dict]:
    """Mint and record a new bearer for an agent an operator is adding (task #2453).

    The hub's operator, on the hub host, enrolling an agent with ``unified-agent
    agent add``. Unlike :func:`agent_token_issue` it generates the value itself,
    so no caller ever chooses one, and it needs neither managed-secret mode nor
    an approval pass: the caller is the human the approval would go to, acting
    directly, and ``actor`` must be a lifecycle operator, the authority
    ``agent_retire`` requires. The value is sealed in the managed store (the
    store ``config.agent_tokens`` reads on every request) and the
    ``agent_tokens`` row keeps its digest, issue date, issuer and expiry, with
    a ``security.agent_token.issued`` event. The value goes back to the caller
    once, wrapped in :class:`RedactedValue`; nothing here logs or stores it.

    Refused when the label already has an active managed token: rotate it
    instead, so one label never holds two live managed bearers by accident.
    Refused too when the store would keep the value unencrypted, unless
    ``allow_plaintext_at_rest`` (:func:`require_sealed_at_rest`).

    ``agent_kind`` is written on the row in the same insert that makes it
    active, so the request that first presents the bearer already reads the
    kind from the row that authenticated it (``config.is_cloud_hosted_identity``),
    never from a cache that predates it.
    """
    if actor not in agent_lifecycle.operators():
        raise ToolError(
            f"{actor!r} may not add an agent: issuing its credential is an operator action "
            "(UA_LIFECYCLE_OPERATORS)"
        )
    label = agent_label.strip() if isinstance(agent_label, str) else ""
    if not config.agent_label_is_well_formed(label):
        raise ToolError(f"agent label {agent_label!r} is not well formed")
    if agent_kind not in TOKEN_KINDS:
        raise ToolError(f"agent_kind must be one of {', '.join(TOKEN_KINDS)}")
    require_sealed_at_rest(allow_plaintext_at_rest)
    value = _mint_value()
    secret_name = f"{managed_secrets.AGENT_TOKEN_PREFIX}{label}"
    tenant_org = edition.tenant_org_id()
    with _claim_lock():
        conn = storage.connect()
        try:
            active = conn.execute(
                "SELECT 1 FROM agent_tokens WHERE agent_label = ? COLLATE NOCASE "
                "AND status = 'active' AND org_id IS ?",
                (label, tenant_org),
            ).fetchone()
        finally:
            conn.close()
        if active is not None:
            raise ToolError(
                f"{label!r} already has an active managed token; rotate it with "
                "agent_token_rotate instead of issuing a second one"
            )
        stored = managed_secrets.set_secret(
            secret_name,
            value.reveal(),
            requested_by=actor,
            consumer="agent_token",
            org_id=tenant_org,
            operator_agent_token=True,
        )
        try:
            issued = _record_issue(
                label,
                value,
                ttl_days=ttl_days,
                note=note,
                rotated_from=None,
                caller_identity=actor,
                custom_stored=stored,
                agent_kind=agent_kind,
            )
        except Exception:
            with contextlib.suppress(Exception):
                managed_secrets.delete_secret(
                    secret_name, org_id=tenant_org, expected_digest=stored.get("digest")
                )
            raise
    return value, issued


def delete_revoked_secret(agent_label: str) -> bool:
    """Remove a label's stored bearer once no active ``agent_tokens`` row needs it.

    The operator's credential step after a retirement (task #2453): the row is
    already ``revoked`` and no longer authenticates, so this only stops the
    sealed value from lingering on disk. Returns True when a value was removed.
    """
    label = agent_label.strip()
    tenant_org = edition.tenant_org_id()
    with _claim_lock():
        conn = storage.connect()
        try:
            active = conn.execute(
                "SELECT 1 FROM agent_tokens WHERE agent_label = ? COLLATE NOCASE "
                "AND status = 'active' AND org_id IS ?",
                (label, tenant_org),
            ).fetchone()
        finally:
            conn.close()
        if active is not None:
            return False
        try:
            return managed_secrets.delete_secret(
                f"{managed_secrets.AGENT_TOKEN_PREFIX}{label}", org_id=tenant_org
            )
        except ToolError:
            return False


def deployment_claim(
    agent_label: str,
    value: str,
    *,
    client_nonce: str | None = None,
    ttl_days: int | None = None,
    note: str | None = None,
) -> dict:
    """Claim a local-trusted deployment, provisioning the first admin credential (task #1422).

    This implements the canonical claim flow transitioning an instance from
    ``local_trusted`` to an authenticated deployment mode.

    Preconditions / Safety Gates:
      1. Permitted only if the instance is in ``local_trusted`` mode.
      2. Permitted only if the instance has zero active agent tokens.
      3. If the deployment is already claimed (active tokens exist), this call
         fails closed with a ToolError directing the caller to ``agent_token_issue``.
    """
    agent_access.enforce("deployment_claim", read_only=False)
    mode = config.deployment_mode()
    if mode != config.DEPLOYMENT_MODE_LOCAL_TRUSTED:
        raise ToolError(
            f"deployment_claim is only permitted in local_trusted mode (current mode: {mode}); "
            "use agent_token_issue with an authenticated identity instead"
        )

    if not isinstance(agent_label, str) or not agent_label.strip():
        raise ToolError("agent_label must be a non-empty string")
    clean_label = agent_label.strip()
    if not config.is_known_agent_label(clean_label):
        raise ToolError(
            f"unknown agent label {clean_label!r}; deployment_claim requires a known fleet identity"
        )
    if not isinstance(value, str) or not value.strip():
        raise ToolError("token value must be a non-empty string")
    if ttl_days is not None:
        if not isinstance(ttl_days, int) or ttl_days <= 0:
            raise ToolError("ttl_days must be a positive integer or None")

    conn = storage.connect()
    try:
        active_count = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_tokens WHERE status = 'active' AND org_id IS ?",
            (edition.tenant_org_id(),),
        ).fetchone()["c"]
    finally:
        conn.close()

    if active_count > 0:
        raise ToolError(
            "deployment is already claimed and has active credentials; "
            "use agent_token_issue with an authenticated identity instead"
        )

    if not config.verified_agent_identity():
        if not client_nonce or not client_nonce.strip() or len(client_nonce.strip()) < 8:
            raise ToolError(
                "client_nonce (at least 8 characters) is required for deployment_claim "
                "to cryptographically bind the approval pass to the claimant"
            )
        claimant = f"claimant:{clean_label}:nonce:{client_nonce.strip()}"
    else:
        claimant = config.verified_agent_identity()

    secret_name = f"agent-token:{clean_label}"
    tenant_org = edition.tenant_org_id()

    payload = profile_tools.managed_secret_payload(
        secret_name,
        value,
        consumer="agent_token",
        org_id=tenant_org,
        caller_identity=claimant,
    )
    if ttl_days is not None:
        payload["ttl_days"] = ttl_days
    profile_tools.check_approval_gate(
        "managed_secret_set", payload, requested_by=claimant, force=True
    )

    claim_note = (
        f"[claim-flow: {mode} -> authenticated] {note}"
        if note
        else f"[claim-flow: {mode} -> authenticated]"
    )

    with _claim_lock():
        conn = storage.connect()
        try:
            active_count = conn.execute(
                "SELECT COUNT(*) AS c FROM agent_tokens WHERE status = 'active' AND org_id IS ?",
                (edition.tenant_org_id(),),
            ).fetchone()["c"]
        finally:
            conn.close()

        if active_count > 0:
            raise ToolError(
                "deployment is already claimed and has active credentials; "
                "use agent_token_issue with an authenticated identity instead"
            )

        secret_path = (
            managed_secrets._unbound_path(secret_name)
            if tenant_org is None
            else managed_secrets._path(secret_name, tenant_org)
        )
        if secret_path.exists():
            raise ToolError(
                f"managed secret {secret_name!r} already exists; deployment_claim refuses to overwrite existing credentials"
            )

        redacted = _coerce(value)
        stored = managed_secrets.set_secret(
            secret_name,
            redacted.reveal(),
            requested_by=claimant,
            consumer="agent_token",
            org_id=tenant_org,
            create_only=True,
        )

        try:
            issued = _record_issue(
                clean_label,
                redacted,
                ttl_days=ttl_days,
                note=claim_note,
                rotated_from=None,
                caller_identity=claimant,
                custom_stored=stored,
                claim_info={"deployment_mode": mode, "claimed_by": claimant},
            )
        except Exception:
            try:
                managed_secrets.delete_secret(
                    secret_name, org_id=tenant_org, expected_digest=stored.get("digest")
                )
            except Exception:
                pass
            raise

    issued["claimed"] = True
    issued["deployment_mode"] = mode
    issued["next_mode"] = config.DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE
    issued["activation_step"] = (
        "Set UA_ENFORCE_AUTH=1 and update any explicit deployment mode to "
        "UA_DEPLOYMENT_MODE=authenticated_private, and authenticate with the claimed Bearer token."
    )
    return issued


def _record_issue(
    agent_label: str,
    value: RedactedValue | str,
    *,
    ttl_days: int | None,
    note: str | None,
    rotated_from: int | None,
    caller_identity: str | None = None,
    custom_stored: dict | None = None,
    claim_info: dict[str, object] | None = None,
    agent_kind: str | None = None,
) -> dict:
    """Shared body for a fresh issuance, rotation, and claim flows.

    Kept private (leading underscore, not MCP-registered) so the public
    ``agent_token_issue`` signature stays a plain ``str`` for MCP schema
    generation, while ``agent_token_rotate`` can pass ``rotated_from`` without
    that internal linkage becoming a caller-facing parameter either tool
    exposes.
    """
    agent_access.enforce("agent_token_issue", read_only=False)
    if not isinstance(agent_label, str) or not agent_label.strip():
        raise ToolError("agent_label must be a non-empty string")
    identity = caller_identity or config.verified_agent_identity()
    if not identity:
        raise ToolError("agent token issuance requires an authenticated identity")

    if agent_lifecycle.retired_state(agent_label.strip()) is not None:
        raise ToolError(
            f"agent {agent_label.strip()!r} is retired; refusing to issue a token. "
            "An operator must unretire it explicitly"
        )

    redacted = _coerce(value)
    secret_name = f"agent-token:{agent_label}"
    tenant_org = edition.tenant_org_id()
    previous_secret: str | None = None
    if custom_stored is not None:
        stored = custom_stored
    else:
        try:
            previous_secret = managed_secrets._load_for_consumer(
                secret_name, consumer="agent_token", org_id=tenant_org
            )
        except Exception:
            previous_secret = None
        stored = profile_tools.managed_secret_set(
            name=secret_name, value=redacted.reveal(), consumer="agent_token"
        )
    token_hash = stored["digest"]

    issued_at = storage.now()
    expires_at = _expires_at(ttl_days, issued_at)

    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if agent_lifecycle.retired_state(agent_label.strip(), conn=conn) is not None:
            raise ToolError(
                f"agent {agent_label.strip()!r} is retired; refusing to issue a token. "
                "An operator must unretire it explicitly"
            )
        cur = conn.execute(
            "INSERT INTO agent_tokens "
            "(agent_label, token_hash, hash_algo, status, issued_at, expires_at, "
            " rotated_from, issued_by, note, org_id, agent_kind) "
            "VALUES (?, ?, 'sha256', 'active', ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_label,
                token_hash,
                issued_at,
                expires_at,
                rotated_from,
                identity,
                note,
                stored.get("org_id"),
                agent_kind,
            ),
        )
        token_id = cur.lastrowid

        issue_payload: dict[str, object] = {
            "token_id": token_id,
            "agent_label": agent_label,
            "token_hash": token_hash,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "rotated_from": rotated_from,
        }
        _, is_new_issue, pub_issue = events.record_event_once_on_connection(
            conn,
            "security.agent_token.issued",
            payload=issue_payload,
            originator=identity,
        )

        pub_claim = None
        is_new_claim = False
        if claim_info:
            claim_payload: dict[str, object] = {
                "token_id": token_id,
                "agent_label": agent_label,
                "token_hash": token_hash,
                **claim_info,
            }
            _, is_new_claim, pub_claim = events.record_event_once_on_connection(
                conn,
                "security.deployment.claimed",
                payload=claim_payload,
                originator=identity,
            )

        conn.commit()
    except storage.sqlite3.IntegrityError as exc:
        conn.rollback()
        if custom_stored is None:
            try:
                if previous_secret is not None:
                    managed_secrets.set_secret(
                        secret_name,
                        previous_secret,
                        requested_by=identity,
                        consumer="agent_token",
                        org_id=tenant_org,
                        expected_digest=stored.get("digest"),
                    )
                else:
                    managed_secrets.delete_secret(
                        secret_name,
                        org_id=stored.get("org_id"),
                        expected_digest=stored.get("digest"),
                    )
            except Exception:
                pass
        raise ToolError(
            "a token with this exact value's hash is already recorded — issue a "
            "distinct value, not the same one twice"
        ) from exc
    except Exception:
        conn.rollback()
        if custom_stored is None:
            try:
                if previous_secret is not None:
                    managed_secrets.set_secret(
                        secret_name,
                        previous_secret,
                        requested_by=identity,
                        consumer="agent_token",
                        org_id=tenant_org,
                        expected_digest=stored.get("digest"),
                    )
                else:
                    managed_secrets.delete_secret(
                        secret_name,
                        org_id=stored.get("org_id"),
                        expected_digest=stored.get("digest"),
                    )
            except Exception:
                pass
        raise
    finally:
        conn.close()

    if is_new_issue and pub_issue is not None:
        try:
            events.publish(pub_issue)
        except Exception:
            pass

    if claim_info and is_new_claim and pub_claim is not None:
        try:
            events.publish(pub_claim)
        except Exception:
            pass

    return {
        "token_id": token_id,
        "agent_label": agent_label,
        "token_hash": token_hash,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "rotated_from": rotated_from,
        "secret_name": secret_name,
    }


def agent_token_rotate(
    agent_label: str,
    new_value: str,
    *,
    ttl_days: int | None = None,
    note: str | None = None,
) -> dict:
    """Record a rotation: issue a new token row linked to the one it replaces.

    Live for managed tokens (task #1826): the new value replaces the old one
    in the managed secret store and the old row is marked ``rotated``, so on
    the next request the new value authenticates and the previous managed
    value no longer does. The new metadata row links to the old one via
    ``rotated_from``, and a hash-chained ``security.agent_token.rotated``
    event goes to the evidence ledger. It does not edit ``UA_AGENT_TOKENS``
    or restart the hub: a previous value listed there keeps authenticating
    until the operator removes it, a separate step outside this module.

    Not separately approval-gated: the risk-bearing action here is the
    secret write, and that is already force-gated one layer down inside
    ``managed_secret_set`` (via the issue path this function shares with
    ``agent_token_issue``). A second gate on top would ask a human to
    approve the same write twice under two different tool names.
    """
    agent_access.enforce("agent_token_rotate", read_only=False)
    if not isinstance(agent_label, str) or not agent_label.strip():
        raise ToolError("agent_label must be a non-empty string")
    identity = config.verified_agent_identity()
    if not identity:
        raise ToolError("agent token rotation requires an authenticated identity")

    with _claim_lock():
        conn = storage.connect()
        try:
            # Tenant-scoped (task #1343): `org_id IS ?` matches NULL for the
            # single-tenant hub and only the caller's org under a tenant boundary.
            previous = conn.execute(
                "SELECT id FROM agent_tokens WHERE agent_label = ? AND status = 'active' "
                "AND org_id IS ? ORDER BY issued_at DESC LIMIT 1",
                (agent_label, edition.tenant_org_id()),
            ).fetchone()
        finally:
            conn.close()
        previous_id = previous["id"] if previous else None

        issued = _record_issue(
            agent_label,
            new_value,
            ttl_days=ttl_days,
            note=note,
            rotated_from=previous_id,
        )

        rotated_at = storage.now()
        if previous_id is not None:
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE agent_tokens SET status = 'rotated', rotated_at = ? WHERE id = ?",
                    (rotated_at, previous_id),
                )
                conn.commit()
            finally:
                conn.close()

    events.record_event(
        "security.agent_token.rotated",
        payload={
            "agent_label": agent_label,
            "previous_token_id": previous_id,
            "new_token_id": issued["token_id"],
            "new_token_hash": issued["token_hash"],
            "rotated_at": rotated_at,
        },
        originator=identity,
    )

    return {
        "agent_label": agent_label,
        "previous_token_id": previous_id,
        "new_token_id": issued["token_id"],
        "new_token_hash": issued["token_hash"],
        "issued_at": issued["issued_at"],
        "expires_at": issued["expires_at"],
        "rotated_at": rotated_at,
    }


def agent_token_list(agent_label: str | None = None, status: str | None = None) -> dict:
    """List agent-token metadata rows. Returns hashes only, never values."""
    agent_access.enforce("agent_token_list", read_only=True)
    if status is not None and status not in _VALID_STATUSES:
        raise ToolError(f"status must be one of {_VALID_STATUSES}")

    clauses: list[str] = ["org_id IS ?"]
    params: list[Any] = [edition.tenant_org_id()]
    if agent_label is not None:
        clauses.append("agent_label = ?")
        params.append(agent_label)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}"

    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM agent_tokens {where} ORDER BY issued_at DESC",
            params,
        ).fetchall()
    finally:
        conn.close()
    return {"tokens": [_row_to_dict(r) for r in rows]}


def stale_active_tokens(max_age_days: int | None = None) -> list[dict]:
    """Active tokens whose ``issued_at`` is older than the policy window.

    Read-only helper shared by ``system_doctor``'s lifecycle check and by
    anything else that wants "which tokens need rotating" without
    duplicating the age math.
    """
    max_age = max_age_days if max_age_days is not None else config.token_max_age_days()
    cutoff = (datetime.now(UTC) - timedelta(days=max_age)).isoformat().replace("+00:00", "Z")
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_tokens WHERE status = 'active' AND issued_at < ? "
            "AND org_id IS ? ORDER BY issued_at ASC",
            (cutoff, edition.tenant_org_id()),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        try:
            issued = datetime.fromisoformat(d["issued_at"].replace("Z", "+00:00"))
            d["age_days"] = round((datetime.now(UTC) - issued).total_seconds() / 86400, 1)
        except (ValueError, AttributeError):
            d["age_days"] = None
        out.append(d)
    return out
