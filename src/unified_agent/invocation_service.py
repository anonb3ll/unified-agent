"""Shared invocation transaction owner and isolated synthetic effect proof.

Trusted composition supplies repository, authorization and required evidence
ports. The selected registered task writer uses the same owner primitives with
its existing native receipts and approval gates. The synthetic receipt schema
and effect state model remain unregistered and are not installed on the hub.
This module opens no database or installs a schema or tool on import.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from . import config

RECEIPT_SCHEMA = """
CREATE TABLE invocation_receipts (
    org_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    client_key TEXT NOT NULL,
    digest TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('mutation', 'effect')),
    state TEXT NOT NULL CHECK(
        state IN ('pending', 'dispatching', 'indeterminate', 'completed', 'no_effect')
    ),
    fence INTEGER NOT NULL CHECK(fence > 0),
    result_json TEXT,
    PRIMARY KEY (org_id, subject, actor, operation, version, client_key)
);
"""


class InvocationDenied(RuntimeError):
    """Current authenticated authority or resource access is missing."""


class InvocationConflict(RuntimeError):
    """A scoped client key is already bound to a different semantic request."""


class StaleInvocation(RuntimeError):
    """Another attempt owns this operation's completion fence."""


class TransactionOwnershipError(RuntimeError):
    """A repository/evidence port attempted to own the transaction."""


def _json_object(value: dict) -> str:
    def validate(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ValueError("arguments must contain only JSON values")

    if not isinstance(value, dict):
        raise ValueError("arguments/result must be a JSON object")
    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Principal:
    organization: str
    subject: str
    actor: str


def principal_from_request() -> Principal:
    """Accept only the existing verified WorkOS request binding, never env/body.

    Legacy bearer/single-org mapping is not invented by the synthetic proof.
    It needs an explicit compatibility decision before production activation.
    """
    metadata = config.current_auth_metadata() or {}
    actor = config.verified_agent_identity()
    expiry = metadata.get("exp")
    if (
        not config.workos_request_is_bound()
        or not actor
        or isinstance(expiry, bool)
        or not isinstance(expiry, (int, float))
        or not math.isfinite(expiry)
        or expiry <= time.time()
    ):
        raise InvocationDenied("a current verified organization request is required")
    organization = metadata.get("workos_organization_id")
    subject = metadata.get("workos_user_id")
    if not isinstance(organization, str) or not organization.strip():
        raise InvocationDenied("verified organization is missing")
    if not isinstance(subject, str) or not subject.strip():
        raise InvocationDenied("verified subject is missing")
    return Principal(organization, subject, actor)


class Transaction:
    """A trusted port's connection view; the service alone commits/rolls back.

    This enforces a port API, not a sandbox for adversarial Python callbacks.
    Cursors and repositories must not escape the transaction to their callers.
    """

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        # SQLite execute accepts one statement. Strip comments so BEGIN/COMMIT
        # cannot accidentally bypass the view through a leading comment.
        command = re.sub(r"(?s)/\*.*?\*/|--[^\n]*(?:\n|$)", " ", sql).lstrip()
        matched = re.match(r"[\s;]*([A-Za-z]+)", command)
        word = matched.group(1).upper() if matched else ""
        if word in {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
            raise TransactionOwnershipError("the invocation service owns transaction control")
        return self._connection.execute(sql, parameters)

    def commit(self) -> None:
        raise TransactionOwnershipError("repository/evidence ports must not commit")

    def rollback(self) -> None:
        raise TransactionOwnershipError("repository/evidence ports must not roll back")

    def executescript(self, sql: str) -> None:
        raise TransactionOwnershipError("executescript can implicitly commit")


@dataclass(frozen=True)
class Request:
    principal: Principal
    operation: str
    version: int
    client_key: str
    digest: str
    arguments_json: str

    @property
    def arguments(self) -> dict[str, Any]:
        # Ports receive separate snapshots; mutating a supplied dictionary
        # cannot alter the payload after its receipt digest was computed.
        return json.loads(self.arguments_json)

    @property
    def key(self) -> tuple:
        p = self.principal
        return (p.organization, p.subject, p.actor, self.operation, self.version, self.client_key)


@dataclass(frozen=True)
class Outcome:
    status: Literal["completed", "pending", "indeterminate", "no_effect"]
    operation_id: str
    fence: int
    result: dict[str, Any] | None = None
    replayed: bool = False


@dataclass(frozen=True)
class Observation:
    status: Literal["known", "unknown", "no_effect"]
    result: dict[str, Any] | None = None


class SyntheticProvider(Protocol):
    is_synthetic: bool

    def send(self, operation_id: str, digest: str, arguments: dict[str, Any]) -> dict: ...

    def lookup(self, operation_id: str, digest: str) -> Observation: ...


Authorize = Callable[[Transaction, Request, bool], None]
Evidence = Callable[[Transaction, str, Request, str, int], None]
Mutation = Callable[[Transaction, Request], dict[str, Any]]


class InvocationService:
    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        authorize: Authorize,
        evidence: Evidence,
        *,
        authority: Callable[[], Principal] = principal_from_request,
    ):
        self._connect = connect
        self._authorize = authorize
        self._evidence = evidence
        self._authority = authority

    @staticmethod
    def begin_mutation(connect: Callable[[], sqlite3.Connection]) -> sqlite3.Connection:
        """Open the owner transaction; selected repositories retain their SQL.

        Current authentication and authorization stay in their trusted existing
        composition. No authority is derived from this connection factory.
        """
        connection = connect()
        try:
            if connection.in_transaction:
                raise TransactionOwnershipError("connection already belongs to a transaction")
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def complete_mutation(
        connection: sqlite3.Connection,
        prepare: Callable[[Transaction], None] | None = None,
    ) -> None:
        """Prepare required state/evidence through restricted ports, then commit.

        The caller acknowledges only after this returns. A preparation or actual
        commit failure rolls back; neither a repository nor evidence port may
        commit, roll back, or implicitly finish the owner's transaction.
        """
        if not connection.in_transaction:
            raise TransactionOwnershipError("an active owner transaction is required")
        try:
            if prepare is not None:
                prepare(Transaction(connection))
            if not connection.in_transaction:
                raise TransactionOwnershipError("a port ended the owner transaction")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _request(self, operation: str, version: int, client_key: str, arguments: dict) -> Request:
        principal = self._authority()
        if not isinstance(principal, Principal) or any(
            not isinstance(value, str) or not value.strip()
            for value in (principal.organization, principal.subject, principal.actor)
        ):
            raise InvocationDenied("an explicit authenticated principal is required")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation name is required")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("operation version must be a positive integer")
        if not isinstance(client_key, str) or not client_key.strip() or len(client_key) > 200:
            raise ValueError("client key must be 1-200 characters")
        encoded = _json_object(arguments)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        return Request(principal, operation, version, client_key, digest, encoded)

    def request(self, operation: str, version: int, client_key: str, arguments: dict) -> Request:
        """The canonical request, for a caller that needs its key before invoking."""
        return self._request(operation, version, client_key, arguments)

    def find(self, transaction: Transaction, request: Request, kind: str) -> sqlite3.Row | None:
        """The receipt already held for ``request``, refusing a conflicting reuse of its key."""
        return self._find(transaction, request, kind)

    def _open(self, request: Request) -> tuple[sqlite3.Connection, Transaction]:
        connection = self.begin_mutation(self._connect)
        try:
            transaction = Transaction(connection)
            self._access(transaction, request, False)
            return connection, transaction
        except BaseException:
            connection.close()
            raise

    def _access(self, transaction: Transaction, request: Request, executing: bool) -> None:
        if self._authority() != request.principal:
            raise InvocationDenied("authenticated request context changed")
        self._authorize(transaction, request, executing)

    def _find(self, transaction: Transaction, request: Request, kind: str) -> sqlite3.Row | None:
        row = transaction.execute(
            "SELECT * FROM invocation_receipts WHERE org_id=? AND subject=? AND actor=? "
            "AND operation=? AND version=? AND client_key=?",
            request.key,
        ).fetchone()
        if row is not None and (row["digest"] != request.digest or row["kind"] != kind):
            raise InvocationConflict("client key is bound to another semantic request")
        return row

    @staticmethod
    def _outcome(row: sqlite3.Row, *, replayed: bool = True) -> Outcome:
        state = row["state"]
        status = "indeterminate" if state == "dispatching" else state
        result = json.loads(row["result_json"]) if row["result_json"] is not None else None
        return Outcome(status, row["operation_id"], row["fence"], result, replayed)

    @staticmethod
    def _insert(transaction: Transaction, request: Request, kind: str) -> str:
        operation_id = uuid.uuid4().hex
        transaction.execute(
            "INSERT INTO invocation_receipts "
            "(org_id,subject,actor,operation,version,client_key,digest,operation_id,kind,state,fence) "
            "VALUES (?,?,?,?,?,?,?,?,?,'pending',1)",
            (*request.key, request.digest, operation_id, kind),
        )
        return operation_id

    def mutate(
        self, operation: str, version: int, client_key: str, arguments: dict, mutation: Mutation
    ) -> Outcome:
        request = self._request(operation, version, client_key, arguments)
        connection, transaction = self._open(request)
        try:
            existing = self._find(transaction, request, "mutation")
            if existing is not None:
                self.complete_mutation(connection)
                return self._outcome(existing)
            # Revision belongs in the new-mutation repository callback, after
            # authorized scoped replay lookup. Both evidence and receipt are
            # required parts of this same transaction.
            self._access(transaction, request, True)
            operation_id = self._insert(transaction, request, "mutation")
            result = mutation(transaction, request)
            if not isinstance(result, dict):
                raise ValueError("domain result must be a JSON object")
            encoded = _json_object(result)

            def prepare_completion(owner: Transaction) -> None:
                self._evidence(owner, "mutation_completed", request, operation_id, 1)
                owner.execute(
                    "UPDATE invocation_receipts SET state='completed',result_json=? WHERE operation_id=?",
                    (encoded, operation_id),
                )

            self.complete_mutation(connection, prepare_completion)
            return Outcome("completed", operation_id, 1, json.loads(encoded))
        finally:
            connection.close()  # SQLite rolls back any uncommitted transaction.

    @staticmethod
    def _provider(provider: SyntheticProvider) -> None:
        if getattr(provider, "is_synthetic", False) is not True:
            raise InvocationDenied("only an explicitly synthetic provider is supported")

    def effect(
        self,
        operation: str,
        version: int,
        client_key: str,
        arguments: dict,
        provider: SyntheticProvider,
    ) -> Outcome:
        self._provider(provider)
        request = self._request(operation, version, client_key, arguments)
        connection, transaction = self._open(request)
        try:
            existing = self._find(transaction, request, "effect")
            if existing is not None and existing["state"] != "pending":
                self.complete_mutation(connection)
                return self._outcome(existing)
            if existing is None:
                self._access(transaction, request, True)
                operation_id = self._insert(transaction, request, "effect")
                fence = 1
                self._evidence(transaction, "effect_intent", request, operation_id, fence)
            else:
                operation_id, fence = existing["operation_id"], existing["fence"]
            self.complete_mutation(connection)
        finally:
            connection.close()

        # Intent and dispatch marker are distinct durable boundaries. Authority
        # and the executing port are checked again before the marker commits.
        connection, transaction = self._open(request)
        try:
            row = self._find(transaction, request, "effect")
            if row["fence"] != fence:
                raise StaleInvocation("effect attempt changed before dispatch")
            if row["state"] != "pending":
                self.complete_mutation(connection)
                return self._outcome(row)
            self._access(transaction, request, True)
            transaction.execute(
                "UPDATE invocation_receipts SET state='dispatching' WHERE operation_id=? AND fence=?",
                (operation_id, fence),
            )
            self._evidence(transaction, "effect_dispatch", request, operation_id, fence)
            self.complete_mutation(connection)
        finally:
            connection.close()

        try:
            result = provider.send(operation_id, request.digest, request.arguments)
            return self._complete(request, operation_id, fence, result)
        except Exception:
            # A provider/terminal-ledger failure leaves the committed dispatch
            # marker. It means uncertainty, including marker-before-send gaps;
            # it never grants an automatic resend or approval restoration.
            return Outcome("indeterminate", operation_id, fence)

    def _complete(self, request: Request, operation_id: str, fence: int, result: dict) -> Outcome:
        if not isinstance(result, dict):
            raise ValueError("provider result must be a JSON object")
        encoded = _json_object(result)
        connection, transaction = self._open(request)
        try:
            row = self._find(transaction, request, "effect")
            if row["operation_id"] != operation_id or row["fence"] != fence:
                raise StaleInvocation("effect completion belongs to a stale attempt")
            if row["state"] == "completed":
                self.complete_mutation(connection)
                return self._outcome(row)
            if row["state"] not in {"dispatching", "indeterminate", "no_effect"}:
                raise StaleInvocation("effect has not crossed its dispatch marker")
            # A recorded no_effect can only be corrected upward, by the fact
            # that the effect did land (a send in flight at the time).
            # Current access still applies when recording a factual outcome;
            # execution authorization is not consumed again after the effect.
            self._evidence(transaction, "effect_completed", request, operation_id, fence)
            transaction.execute(
                "UPDATE invocation_receipts SET state='completed',result_json=? WHERE operation_id=? "
                "AND fence=?",
                (encoded, operation_id, fence),
            )
            self.complete_mutation(connection)
            return Outcome("completed", operation_id, fence, json.loads(encoded))
        finally:
            connection.close()

    def reconcile(
        self,
        operation: str,
        version: int,
        client_key: str,
        arguments: dict,
        provider: SyntheticProvider,
        *,
        expected_fence: int,
    ) -> Outcome:
        """Explicit fenced status reconciliation; this path never sends."""
        if (
            isinstance(expected_fence, bool)
            or not isinstance(expected_fence, int)
            or expected_fence < 1
        ):
            raise ValueError("expected fence must be a positive integer")
        self._provider(provider)
        request = self._request(operation, version, client_key, arguments)
        connection, transaction = self._open(request)
        recheck_only = False
        try:
            row = self._find(transaction, request, "effect")
            if row is None:
                raise InvocationConflict("no committed effect intent")
            if row["state"] == "completed":
                if row["fence"] != expected_fence:
                    raise StaleInvocation("effect recovery fence changed")
                self.complete_mutation(connection)
                return self._outcome(row)
            if row["state"] == "no_effect":
                # Never resent or reopened for sending, but still re-checked:
                # a send already in flight when no_effect was observed may land
                # later, and that fact must be able to upgrade the record. The
                # re-check is fenced like any reconcile: the caller must hold
                # the fence the no_effect was recorded under (task #1986).
                if row["fence"] != expected_fence:
                    raise StaleInvocation("effect recovery fence changed")
                operation_id, fence = row["operation_id"], row["fence"]
                recheck_only = True
                self.complete_mutation(connection)
            else:
                if row["fence"] != expected_fence:
                    raise StaleInvocation("effect recovery fence changed")
                if row["state"] == "pending":
                    self.complete_mutation(connection)
                    return self._outcome(row)
                operation_id, fence = row["operation_id"], row["fence"] + 1
                transaction.execute(
                    "UPDATE invocation_receipts SET state='indeterminate',fence=? "
                    "WHERE operation_id=? AND fence=?",
                    (fence, operation_id, expected_fence),
                )
                self._evidence(transaction, "effect_recovery", request, operation_id, fence)
                self.complete_mutation(connection)
        finally:
            connection.close()
        try:
            observation = provider.lookup(operation_id, request.digest)
        except Exception:
            return Outcome("no_effect" if recheck_only else "indeterminate", operation_id, fence)
        if observation.status == "known":
            return self._complete(request, operation_id, fence, observation.result)
        if observation.status not in {"unknown", "no_effect"}:
            raise ValueError("invalid provider observation")
        if recheck_only:
            # Only a known effect changes a recorded no_effect; never downgrade.
            return Outcome("no_effect", operation_id, fence)
        if observation.status == "no_effect":
            return self._record_no_effect(request, operation_id, fence)
        return Outcome("indeterminate", operation_id, fence)

    def _record_no_effect(self, request: Request, operation_id: str, fence: int) -> Outcome:
        """Persist the provider's definitive no-effect answer under the fence.

        Later reads report it instead of uncertainty. It is terminal for
        sending: a retry still needs a separately reviewed recovery
        disposition and the existing approval contract, never an automatic
        resend. It is not terminal for the record: a later fenced reconcile
        whose lookup finds the effect landed (a send still in flight when
        no_effect was observed) upgrades it to ``completed``, so any future
        resend-after-no_effect recovery must allow for that upgrade.
        """
        connection, transaction = self._open(request)
        try:
            row = self._find(transaction, request, "effect")
            if row["operation_id"] != operation_id or row["fence"] != fence:
                raise StaleInvocation("no-effect observation belongs to a stale attempt")
            if row["state"] in {"completed", "no_effect"}:
                self.complete_mutation(connection)
                return self._outcome(row)
            if row["state"] != "indeterminate":
                raise StaleInvocation("effect has not crossed its dispatch marker")
            self._evidence(transaction, "effect_no_effect", request, operation_id, fence)
            transaction.execute(
                "UPDATE invocation_receipts SET state='no_effect' WHERE operation_id=? AND fence=?",
                (operation_id, fence),
            )
            self.complete_mutation(connection)
            return Outcome("no_effect", operation_id, fence)
        finally:
            connection.close()
