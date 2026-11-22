"""Add, remove and list agents with one operator command each (task #2453).

``unified-agent agent add|remove|list`` is a thin CLI over this module; an MCP
tool could call the same functions. Each composes paths that already exist:

* **add** mints a bearer through ``token_lifecycle.operator_issue`` (an
  ``agent_tokens`` row with digest, issue date, issuer and expiry, plus a
  ``security.agent_token.issued`` event) and seats the label with
  ``agent_roster`` (office, profile, kind). The hub accepts the bearer from the
  next request: ``config.agent_tokens`` merges active managed rows on every
  request, and the seat makes the label known to a closed roster.
* **remove** releases ``doing`` claims through ``claim_release`` when asked,
  retires the label through ``agent_lifecycle`` (which revokes its managed
  tokens, session credentials and device sign-ins in one transaction), deletes
  the sealed bearer and records the seat's removal.
* **list** joins the seats, the org chart, the configured roster, token
  metadata, lifecycle state and the agent registry's last-seen time.

Every write needs a lifecycle operator (``agent_lifecycle.operators()``), the
authority ``agent_retire`` requires. No function here logs, stores or returns a
token in clear: :func:`add_agent` returns it wrapped in
``token_lifecycle.RedactedValue`` for the caller to show once.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import (
    agent_lifecycle,
    agent_roster,
    claim_release,
    config,
    edition,
    events,
    org_registry,
    profiles,
    registry,
    storage,
)
from .tools import token_lifecycle

_log = logging.getLogger(__name__)

DEFAULT_PROFILE = profiles.DEVELOPER
#: Seconds ``unified-agent agent add`` waits for the first heartbeat by default.
DEFAULT_WAIT_S = 120.0
#: Pauses between attempts at the compensating revoke after a failed seat write.
#: The likeliest cause of that failure is database contention, which the same
#: revoke meets, so it is retried rather than tried once.
REVOKE_RETRY_DELAYS_S = (0.25, 0.5, 1.0, 2.0)


@contextlib.contextmanager
def _acting_as(actor: str) -> Iterator[None]:
    token = config.agent_identity_var.set(actor)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _authorize(actor: str | None, verb: str) -> str:
    who = (actor or "").strip()
    if not who:
        raise ToolError(f"agent {verb} requires an operator identity")
    if who not in agent_lifecycle.operators():
        raise ToolError(
            f"{who!r} may not {verb} an agent: that is an operator action "
            "(UA_LIFECYCLE_OPERATORS, default the configured operator label)"
        )
    return who


def _env_token_labels() -> set[str]:
    """Labels with a ``UA_AGENT_TOKENS`` entry. Labels only; values are dropped here."""
    try:
        entries = config.agent_token_entries(None, False)
    except Exception:  # noqa: BLE001 - an unreadable token file names no labels
        return set()
    return {label for label, _value, _source in entries}


def _check_label(label: str) -> str:
    name = (label or "").strip()
    if not config.agent_label_is_well_formed(name):
        raise ToolError(
            f"agent label {label!r} is not well formed: a letter, then letters, digits, "
            "'_' or '-', at most 64 characters"
        )
    if config.is_human_identity(name):
        raise ToolError(f"{name!r} is the operator's own identity, not an agent")
    if name in config.expected_unregistered_labels():
        raise ToolError(f"{name!r} is reserved for a hub service identity")
    return config.canonical_identity(name)


def add_agent(
    label: str,
    *,
    office: str,
    actor: str,
    profile: str | None = None,
    kind: str = "local",
    expires_days: int | None = None,
    note: str | None = None,
    allow_plaintext_at_rest: bool = False,
) -> dict[str, Any]:
    """Issue a bearer for ``label`` and seat it in ``office``.

    Returns the seat and token metadata, with the bearer under ``"token"`` as a
    ``RedactedValue``: the caller reveals it once. ``warnings`` lists what the
    operator still has to do by hand (an env-listed bearer that keeps working).

    Refused when the managed secret store would keep the bearer unencrypted
    (``UA_MANAGED_SECRET_KEYS`` unset), unless ``allow_plaintext_at_rest``:
    then the bearer sits verbatim in a mode-0600 file under the hub's data
    directory, and the result says so.
    """
    who = _authorize(actor, "add")
    name = _check_label(label)
    token_lifecycle.require_sealed_at_rest(allow_plaintext_at_rest)
    if kind not in agent_roster.KINDS:
        raise ToolError(f"kind must be one of {', '.join(agent_roster.KINDS)}")
    if expires_days is not None and (
        not isinstance(expires_days, int) or isinstance(expires_days, bool) or expires_days <= 0
    ):
        raise ToolError("expires_days must be a positive number of days")
    chosen = (profile or DEFAULT_PROFILE).strip()
    problem = profiles.seat_profile_problem(name, chosen)
    if problem:
        raise ToolError(problem)
    try:
        offices = org_registry.load_registry()
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"the org registry cannot be read: {exc}") from exc
    found = org_registry.find_office(office or "", offices)
    if found is None:
        raise ToolError(
            f"no office {office!r} in the org registry ({org_registry.registry_path()}); "
            f"offices: {', '.join(o['id'] for o in offices)}"
        )
    conflict = add_conflict(name)
    if conflict is not None:
        raise ToolError(conflict)

    warnings: list[str] = []
    if allow_plaintext_at_rest and not token_lifecycle.sealed_at_rest():
        warnings.append(
            "UA_MANAGED_SECRET_KEYS is unset: the bearer is stored unencrypted, verbatim, in "
            "a mode-0600 file in the hub's managed secret store. Anyone who can read that "
            "file as the hub user can use it; set UA_MANAGED_SECRET_KEYS and rotate it"
        )
    if name in _env_token_labels():
        warnings.append(
            f"{name} also has a UA_AGENT_TOKENS entry, which keeps authenticating; remove "
            "it from the hub's token list once the new bearer is in place"
        )
    if kind == "cloud":
        warnings.append(
            "cloud kind: this bearer is for a client that sends an Authorization header. "
            "A harness that connects only through OAuth (a claude.ai connector) cannot use "
            "it; register an OAuth client for it instead (docs/installation.md)."
        )

    with _acting_as(who):
        value, issued = token_lifecycle.operator_issue(
            name,
            actor=who,
            ttl_days=expires_days,
            note=note or f"agent add ({kind})",
            agent_kind=kind,
            allow_plaintext_at_rest=allow_plaintext_at_rest,
        )
        seat = {
            "agent": name,
            "office": found["id"],
            "profile": chosen,
            "agent_kind": kind,
            "token_id": issued["token_id"],
        }
        try:
            _record_seat(agent_roster.KIND_ADDED, seat, who)
        except Exception as exc:
            # Never leave a live bearer without its seat: revoke what was issued,
            # or say exactly which row is still live.
            _revoke_issued(name, int(issued["token_id"]), exc)
            raise
    return {
        "agent": name,
        "office": found["id"],
        "profile": chosen,
        "kind": kind,
        "token": value,
        "token_id": issued["token_id"],
        "token_hash_prefix": issued["token_hash"][:12],
        "issued_at": issued["issued_at"],
        "expires_at": issued["expires_at"],
        "warnings": warnings,
    }


def add_conflict(name: str) -> str | None:
    """Why ``name`` cannot be added in its current state, or None.

    A seat or an active managed token in this organization means it is already
    added; a retired label must be unretired first. :func:`add_agent` checks this
    when it runs, and the Control Center route checks it before the approval
    gate, so a repeated add is refused instead of queueing an approval.
    """
    if agent_roster.seat(name) is not None:
        return f"{name!r} is already added; remove it first or rotate its token"
    if _has_tenant_token(name, active_only=True):
        return f"{name!r} is already added: it holds an active managed token; rotate it instead"
    if agent_lifecycle.retired_state(name) is not None:
        return f"{name!r} is retired; an operator must unretire it before it can be added again"
    return None


def _record_seat(kind: str, seat: dict[str, Any], actor: str) -> None:
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _, is_new, published = agent_roster.record_on_connection(conn, kind, seat, actor=actor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        agent_roster.invalidate()
    if is_new and published is not None:
        with contextlib.suppress(Exception):
            events.publish(published)


def _revoke_issued(
    label: str,
    token_id: int,
    cause: BaseException,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Revoke the bearer :func:`add_agent` just issued, after its seat write failed.

    Retried under the credential lock, because the failure that broke the seat
    (most often a locked database) meets the revoke too. Returns once the row
    is no longer active. If every attempt fails, logs and raises a ``ToolError``
    naming the label and token id of the row that is still live, so the
    operator revokes it by hand instead of being told only that the add failed.
    """
    last: BaseException | None = None
    for attempt, delay in enumerate((0.0, *REVOKE_RETRY_DELAYS_S)):
        if delay:
            sleep(delay)
        try:
            with token_lifecycle.credential_lock():
                conn = storage.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    token_lifecycle.revoke_for_agent(conn, label, revoked_at=storage.now())
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            break
        except Exception as exc:  # noqa: BLE001 - retried, then reported below
            last = exc
            _log.warning(
                "agent add: revoke of token #%s for %s failed (attempt %d): %s",
                token_id,
                label,
                attempt + 1,
                exc,
            )
    else:
        _log.error(
            "agent add: the seat for %s was not recorded and its token #%s is still "
            "active; revoke it by hand (unified-agent agent remove %s): %s",
            label,
            token_id,
            label,
            last,
        )
        raise ToolError(
            f"agent add failed for {label!r} ({cause}), and token #{token_id} for {label!r} "
            f"could not be revoked ({last}): it is still active. Revoke it with "
            f"`unified-agent agent remove {label} --reason 'failed add'`"
        ) from cause
    with contextlib.suppress(Exception):
        # Only the stored value; the row is already revoked and cannot authenticate.
        token_lifecycle.delete_revoked_secret(label)


def seen_since(row: dict[str, Any] | None, since: str | None) -> bool:
    """Whether a registry row records a sighting at or after ``since`` (any, when None).

    ``since`` is a UTC ISO 8601 timestamp in ``storage.now()`` form, such as
    the ``issued_at`` an add returns; the comparison is the stored text's.
    """
    if not row or not row.get("last_seen"):
        return False
    return since is None or row["last_seen"] >= since


def wait_for_heartbeat(
    label: str,
    since: str,
    timeout_s: float,
    *,
    poll_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """The agent's registry row once it has been seen at or after ``since``, else None.

    The hub records a sighting on every authenticated request
    (``registry.touch_agent``), so the first call with the new bearer is the
    first heartbeat. Bounded by ``timeout_s``; zero checks once.
    """
    deadline = clock() + max(0.0, timeout_s)
    while True:
        row = registry.get_agent(label)
        if seen_since(row, since):
            return row
        if clock() >= deadline:
            return None
        sleep(min(poll_s, max(0.0, deadline - clock())))


def _has_tenant_token(label: str, *, active_only: bool = False) -> bool:
    """Whether ``label`` holds a token row in the hub's tenant (any status, or active)."""
    status = " AND status = 'active'" if active_only else ""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM agent_tokens WHERE agent_label = ? COLLATE NOCASE AND org_id IS ?"
            + status
            + " LIMIT 1",
            (label, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _doing_tasks(label: str) -> list[dict[str, Any]]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, current_run_id, claimed_at FROM tasks "
            "WHERE claimed_by = ? COLLATE NOCASE AND status = 'doing' ORDER BY id",
            (label,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def remove_agent(
    label: str, *, reason: str, actor: str, force_release: bool = False
) -> dict[str, Any]:
    """Retire ``label``, revoke its credentials and drop its seat.

    Refused while it holds ``doing`` claims unless ``force_release``, which
    releases each through ``claim_release.task_claim_release`` with the reason
    recorded. Needs the dispatch and node stop handlers registered in this
    process (the CLI imports them), like any retire.
    """
    who = _authorize(actor, "remove")
    name = _check_label(label)
    text = (reason or "").strip()
    if not text:
        raise ToolError("remove requires a reason")
    seated = agent_roster.seat(name)
    if seated is None and not config.is_known_agent_label(name):
        raise ToolError(f"{name!r} is not a known agent label")
    if (
        seated is None
        and edition.tenant_org_id() is not None
        and not _has_tenant_token(name)
        and name not in _env_token_labels()
    ):
        # A partitioned hub retires only its own organization's agents (task
        # #2470): with no seat, token row or env entry here, the label is not ours.
        raise ToolError(f"{name!r} is not a known agent label in this organization")
    missing = agent_lifecycle.missing_stop_handlers()
    if missing:
        raise ToolError(
            f"cannot remove {name!r}: this process has not registered the stop handler(s) "
            f"{', '.join(missing)}. Nothing was changed."
        )
    doing = _doing_tasks(name)
    if doing and not force_release:
        ids = ", ".join(f"#{t['id']}" for t in doing)
        raise ToolError(
            f"{name!r} holds doing claims ({ids}); release them first or pass --force-release"
        )

    released: list[dict[str, Any]] = []
    with _acting_as(who):
        grant = (
            claim_release.human_operator_release(who)
            if who == config.operator_label()
            else contextlib.nullcontext()
        )
        with grant:
            for task in doing:
                out = claim_release.task_claim_release(
                    int(task["id"]),
                    expected_claimant=name,
                    reason=f"agent removed: {text}",
                    expected_run_id=task["current_run_id"],
                )
                released.append(
                    {"task_id": task["id"], "event_ulid": out["released"].get("event_ulid")}
                )
        state = agent_lifecycle.set_agent_state(name, "retire", text)
        secret_deleted = token_lifecycle.delete_revoked_secret(name)
        if seated is not None:
            _record_seat(
                agent_roster.KIND_REMOVED,
                {
                    "agent": name,
                    "office": seated.get("office"),
                    "profile": seated.get("profile"),
                    "agent_kind": seated.get("agent_kind"),
                    "reason": text,
                },
                who,
            )
    revoked = state.get("revoked") or {}
    warnings = []
    if name in _env_token_labels():
        warnings.append(
            f"{name} still has a UA_AGENT_TOKENS entry: the hub refuses it (403 "
            "agent_retired), but remove it from the token list and restart the hub"
        )
    return {
        "agent": name,
        "state": state.get("state"),
        "changed": state.get("changed"),
        "released": released,
        "revoked_tokens": len(revoked.get("agent_tokens") or []),
        "revoked_session_credentials": len(revoked.get("session_credentials") or []),
        "secret_deleted": secret_deleted,
        "seat_removed": seated is not None,
        "warnings": warnings,
    }


def list_agents() -> list[dict[str, Any]]:
    """One row per agent the hub knows: seats, chart members, roster and token holders."""
    seats = agent_roster.seats()
    try:
        offices = org_registry.load_registry()
    except Exception:  # noqa: BLE001 - list what can be read
        offices = []
    env_labels = _env_token_labels()
    tokens: dict[str, dict[str, Any]] = {}
    conn = storage.connect()
    try:
        # The hub's tenant only (task #2470): another organization's token rows
        # are neither listed nor counted toward a label.
        for row in conn.execute(
            "SELECT agent_label, status, issued_at, expires_at FROM agent_tokens "
            "WHERE org_id IS ? ORDER BY id",
            (edition.tenant_org_id(),),
        ).fetchall():
            current = tokens.get(row["agent_label"])
            # Prefer the active row; otherwise the newest.
            if current is None or row["status"] == "active" or current["status"] != "active":
                tokens[row["agent_label"]] = dict(row)
    finally:
        conn.close()
    labels = set(seats) | set(config.agent_labels()) | set(tokens) | env_labels
    for office in offices:
        labels.update(office["members"])
    humans = {config.operator_label(), *config.operator_aliases()}
    rows = []
    for label in sorted(labels - humans, key=str.casefold):
        seat = seats.get(label)
        token = tokens.get(label)
        seen = registry.get_agent(label)
        if token is not None:
            source = "managed"
        elif label in env_labels:
            source = "env"
        else:
            source = None
        rows.append(
            {
                "agent": label,
                "office": (seat or {}).get("office")
                or org_registry.office_for_identity(label, offices),
                "profile": profiles.default_for_identity(label),
                "kind": (seat or {}).get("agent_kind")
                or ("cloud" if config.is_cloud_hosted_identity(label) else "local"),
                "added": seat is not None,
                "state": agent_lifecycle.agent_state(label)["state"],
                "token_source": source,
                "token_status": token["status"] if token else None,
                "token_issued_at": token["issued_at"] if token else None,
                "token_expires_at": token["expires_at"] if token else None,
                "last_seen": seen.get("last_seen") if seen else None,
            }
        )
    return rows


#: The ``list_agents`` fields any signed-in identity may read (task #2523): the
#: seat (office, profile, kind, whether an operator added it), the lifecycle state
#: and the last sighting. Every one is already readable without the operator role
#: (``GET /api/org`` lists office members with profile and last seen, ``GET
#: /api/agent-lifecycle`` the lifecycle state). The ``token_*`` fields describe a
#: credential (whether a bearer exists, its source, status and dates) and stay
#: with the operator's ``GET /api/agent-roster``.
SEAT_FIELDS = ("agent", "office", "profile", "kind", "added", "state", "last_seen")


def list_seats() -> list[dict[str, Any]]:
    """``list_agents`` cut to :data:`SEAT_FIELDS`: no credential metadata."""
    return [{field: row[field] for field in SEAT_FIELDS} for row in list_agents()]


def seat_for(label: str) -> dict[str, Any] | None:
    """The :func:`list_seats` row of ``label`` (exact spelling), or None.

    Tenant-scoped like :func:`list_agents`: a seat or token recorded under
    another organization does not make the label known here.
    """
    return next((row for row in list_seats() if row["agent"] == label), None)
