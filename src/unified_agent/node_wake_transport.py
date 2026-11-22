"""ua-node's terminal adapters for idle-session wakes (tasks #1873, #2376).

The hub says which session is due and how many messages are unread; this
module does the host-local part. Immediately before typing it lists herdr's
agents, matches verified native identity to the hub session's
``external_session_id`` (Codex uses the hook's ``codex:`` prefix), and
requires the agent to be unfocused, ready (``idle`` or ``done``) for at least
``IDLE_FOR_S``, and not ``blocked``. Current supported Herdr (0.9.3 and 0.9.4) cannot attest an
empty composer or atomically refuse a retained draft. Therefore readiness always
fails closed with ``input_state_unavailable`` and sessions remain hook-only.
The guarded submission mechanics below are not reachable through this predicate.
They retain fixed-line submission through ``herdr agent prompt``, which itself refuses a blocked agent. It never uses raw
``pane send-text``: a keystroke into a permission dialog could answer it.

The text is ``messaging.wake.wake_text(n)``: one constant and one integer.
herdr runs from an argv list (no shell) with a timeout. A session in no herdr
pane on this host (a desktop-app session, say) is skipped as ``no_match`` and
stays hook-only.

herdr reports no "idle since" time, so idleness is measured here: the first
time a pane is seen ready with a given ``state_change_seq`` starts its clock,
and any focused, blocked or busy sighting or a changed seq restarts it. A first sighting is
therefore skipped as ``not_idle_long_enough``; the hub re-offers after its
retry hold, by which time the clock has run.

``TuiosWaker`` does the same for agents in TUIOS panes, and can deliver: the
TUIOS daemon's queue (``tuios queue``) re-checks the pane when it types, holding
for a person's recent keys or text in the harness's input box and never typing
over a prompt the agent waits on. That is the composer guard Herdr lacks. The
adapter gives the queue one short window, then removes any entry still waiting,
so a wake never outlives its offer. ``FallbackWaker`` tries TUIOS, then Herdr.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from .messaging.wake import wake_text

logger = logging.getLogger("unified_agent.node.wake")

HERDR_TIMEOUT_S = 10.0
IDLE_FOR_S = 30.0
READY_STATES = frozenset({"idle", "done"})
#: Attempts per offer when herdr itself fails; backoff doubles from RETRY_BASE_S.
ATTEMPTS = 3
RETRY_BASE_S = 1.0
#: Between looks at a queued wake; TUIOS types within about a second at rest.
QUEUE_POLL_S = 0.25
#: Looks for the harness's report of the prompt once the queue entry has left.
CONFIRM_LOOKS = 8
#: herdr pane ids look like ``w1:p1``; anything else (a leading ``-``) is not typed at.
_PANE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,63}$")
_CODEX_SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class HerdrWaker:
    """Deliver one wake through herdr. Every side effect is injectable for tests."""

    name = "herdr"

    def __init__(
        self,
        *,
        binary: str | None = None,
        run: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = time.sleep,
        idle_for_s: float = IDLE_FOR_S,
    ):
        self._binary = binary
        self._run = run
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self.idle_for_s = idle_for_s
        # pane id -> (state_change_seq, when first seen ready with it)
        self._ready_since: dict[str, tuple[Any, float]] = {}

    def binary(self) -> str | None:
        return self._binary or os.environ.get("UA_HERDR_BIN") or shutil.which("herdr")

    def _herdr(
        self, binary: str, *args: str, timeout: float = HERDR_TIMEOUT_S
    ) -> tuple[bool, dict[str, Any]]:
        try:
            proc = self._run(
                [binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, {"error": {"code": type(exc).__name__}}
        raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return proc.returncode == 0, data if isinstance(data, dict) else {}

    def _agents(self, binary: str) -> list[dict[str, Any]] | None:
        ok, data = self._herdr(binary, "agent", "list")
        if not ok:
            return None
        result = data.get("result")
        agents = result.get("agents") if isinstance(result, dict) else result
        listed = [a for a in agents if isinstance(a, dict)] if isinstance(agents, list) else []
        live = {str(a.get("pane_id")) for a in listed}
        self._ready_since = {k: v for k, v in self._ready_since.items() if k in live}
        return listed

    @staticmethod
    def _external(agent: dict[str, Any]) -> str | None:
        """The hub's ``external_session_id`` for a pane, or ``None`` if unverified."""
        session = agent.get("agent_session")
        pane = agent.get("pane_id")
        if (
            not isinstance(session, dict)
            or not isinstance(pane, str)
            or _PANE_ID.fullmatch(pane) is None
            or session.get("kind") != "id"
        ):
            return None
        harness = agent.get("agent")
        if session.get("agent") != harness or session.get("source") != f"herdr:{harness}":
            return None
        native = session.get("value")
        if not isinstance(native, str) or not native:
            return None
        if harness == "codex" and _CODEX_SESSION_ID.fullmatch(native):
            # Real Herdr Codex metadata is a bare UUID; hub-inbox registers
            # the same key as codex:<UUID>. Other harnesses are not inferred.
            return f"codex:{native}"
        if harness == "claude" and ":" not in native:
            # Claude's existing hook contract uses its original bare key.
            return native
        return None

    @staticmethod
    def _match(agents: list[dict[str, Any]], external_session_id: str) -> dict[str, Any] | None:
        hits = [a for a in agents if HerdrWaker._external(a) == external_session_id]
        # Two panes claiming one session is not a match we can trust.
        return hits[0] if len(hits) == 1 else None

    def live_sessions(self) -> set[str]:
        """Session ids with exactly one verified pane here; empty if unreadable (#2428)."""
        binary = self.binary()
        agents = self._agents(binary) if binary else None
        seen = Counter(self._external(a) for a in agents or [])
        return {external for external, n in seen.items() if external and n == 1}

    def _refusal(self, agent: dict[str, Any]) -> str:
        refusal = self._idle_refusal(agent)
        if refusal is not None:
            return refusal
        # Herdr 0.9.3/0.9.4 exposes lifecycle state, not composer state. A real
        # retained draft stays idle/unfocused with an unchanged state_change_seq.
        # Rendered text or an invented metadata flag cannot attest empty input or
        # close the race before text+Enter. No authorization or input is safe.
        return "input_state_unavailable"

    def _idle_refusal(self, agent: dict[str, Any]) -> str | None:
        pane = agent["pane_id"]
        status = agent.get("agent_status")
        if status == "blocked":
            self._ready_since.pop(pane, None)
            return "blocked"
        if agent.get("focused") is not False:
            # Someone may be typing a draft; a wake would submit it. The time
            # they were here is not idle time, so the clock starts again.
            self._ready_since.pop(pane, None)
            return "focused"
        if status not in READY_STATES:
            self._ready_since.pop(pane, None)
            return "not_idle"
        seq = agent.get("state_change_seq")
        now = self._clock()
        seen = self._ready_since.get(pane)
        if seen is None or seen[0] != seq:
            seen = self._ready_since[pane] = (seq, now)
        if now - seen[1] < self.idle_for_s:
            return "not_idle_long_enough"
        return None

    def deliver(
        self,
        external_session_id: str,
        unread: int,
        *,
        expires_at: float | None = None,
        authorize: Callable[[], float] | None = None,
    ) -> tuple[str, str | None]:
        """``(outcome, reason)``: ``delivered``, ``skipped`` (with why) or ``failed``."""
        text = wake_text(unread)
        binary = self.binary()
        if not binary:
            return "skipped", f"{self.name}_missing"
        unavailable = f"{self.name}_unavailable"
        reason = unavailable
        for attempt in range(ATTEMPTS):
            if attempt:
                self._sleep(RETRY_BASE_S * 2 ** (attempt - 1))
            if expires_at is not None and self._wall_clock() >= expires_at:
                return "skipped", "offer_expired"
            agents = self._agents(binary)
            if agents is None:
                reason = unavailable
                continue
            # Matched and gated on this fresh listing, immediately before typing.
            agent = self._match(agents, external_session_id)
            if agent is None:
                return "skipped", "no_match"
            refusal = self._refusal(agent)
            if refusal is not None:
                return "skipped", refusal
            # A fresh hub check follows the local pane check on EVERY attempt.
            # Subtract the entire round trip from the server's remaining lease,
            # conservatively bounding the prompt even if the clocks disagree.
            remaining = HERDR_TIMEOUT_S
            if authorize is not None:
                started = self._clock()
                allowed = authorize()
                if (
                    isinstance(allowed, bool)
                    or not isinstance(allowed, (int, float))
                    or not 0 < allowed <= 120
                ):
                    return "skipped", "offer_invalid"
                remaining = min(remaining, allowed - max(0.0, self._clock() - started))
                if remaining <= 0:
                    return "skipped", "offer_invalid"
                # The hub round trip can take time. Refresh pane state again so
                # a person focusing it during that request cannot lose a draft.
                agents = self._agents(binary)
                if agents is None:
                    reason = unavailable
                    continue
                agent = self._match(agents, external_session_id)
                if agent is None:
                    return "skipped", "no_match"
                refusal = self._refusal(agent)
                if refusal is not None:
                    return "skipped", refusal
                remaining = min(remaining, allowed - max(0.0, self._clock() - started))
            if expires_at is not None:
                remaining = min(remaining, expires_at - self._wall_clock())
            if remaining <= 0:
                return "skipped", "offer_invalid"
            return self._submit(binary, agent, text, remaining)
        logger.warning("wake failed after %d attempts: %s", ATTEMPTS, reason)
        return "failed", reason

    def _submit(
        self, binary: str, agent: dict[str, Any], text: str, remaining: float
    ) -> tuple[str, str | None]:
        ok, data = self._herdr(binary, "agent", "prompt", agent["pane_id"], text, timeout=remaining)
        result = data.get("result")
        if ok and isinstance(result, dict) and result.get("submitted") is True:
            return "delivered", None
        error = data.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if code == "agent_blocked":
            return "skipped", "blocked"
        # Timeout, CLI/I/O failure, malformed error or prompt_stalled may
        # follow submission. Repeating this command could submit a second
        # model turn. Only list failures are safely retryable here; blocked
        # is the explicit pre-submission refusal documented by Herdr.
        return "skipped", "delivery_uncertain"


class TuiosWaker(HerdrWaker):
    """Deliver one wake through the TUIOS daemon's draft-safe queue."""

    name = "tuios"

    def binary(self) -> str | None:
        return self._binary or os.environ.get("UA_TUIOS_BIN") or shutil.which("tuios")

    def _agents(self, binary: str) -> list[dict[str, Any]] | None:
        ok, data = self._herdr(binary, "list-agents", "--all-sessions", "--json")
        agents = data.get("agents")
        if not ok or not isinstance(agents, list):
            return None
        listed = [self._row(a) for a in agents if isinstance(a, dict)]
        live = {a["pane_id"] for a in listed}
        self._ready_since = {k: v for k, v in self._ready_since.items() if k in live}
        return listed

    @staticmethod
    def _row(agent: dict[str, Any]) -> dict[str, Any]:
        """One listing row in the shape the shared idle clock reads."""
        status = agent.get("state")
        if agent.get("needs_you") is not False or agent.get("blocked_by"):
            status = "blocked"
        elif status == "needs_input":
            status = "blocked"
        return {
            **agent,
            "pane_id": str(agent.get("window_id")),
            "agent_status": status,
            "state_change_seq": agent.get("completion_seq"),
        }

    @staticmethod
    def _external(agent: dict[str, Any]) -> str | None:
        native = agent.get("agent_session_id")
        session = agent.get("session")
        if (
            not isinstance(native, str)
            or not native
            or _PANE_ID.fullmatch(agent["pane_id"]) is None
            or not isinstance(session, str)
            or _PANE_ID.fullmatch(session) is None
        ):
            return None
        harness = agent.get("harness_id")
        if harness == "codex" and _CODEX_SESSION_ID.fullmatch(native):
            return f"codex:{native}"
        if harness == "claude-code" and ":" not in native:
            return native
        return None

    @staticmethod
    def _match(agents: list[dict[str, Any]], external_session_id: str) -> dict[str, Any] | None:
        hits = [a for a in agents if TuiosWaker._external(a) == external_session_id]
        return hits[0] if len(hits) == 1 else None

    def _refusal(self, agent: dict[str, Any]) -> str | None:
        refusal = self._idle_refusal(agent)
        if refusal is not None:
            return refusal
        if agent.get("draft_held") is not False:
            return "draft_held"
        if agent.get("queued") != 0:
            return "queue_busy"
        return None if agent.get("ready") is True else "not_idle"

    def _submit(
        self, binary: str, agent: dict[str, Any], text: str, remaining: float
    ) -> tuple[str, str | None]:
        target = ("-s", agent["session"], "-w", agent["pane_id"])
        ok, data = self._herdr(binary, "queue", "--json", *target, "--", text, timeout=remaining)
        entry = data.get("id")
        if not ok or not isinstance(entry, str) or not entry:
            return "skipped", "delivery_uncertain"
        state = "unknown"
        deadline = self._clock() + remaining
        for _ in range(max(1, int(remaining / QUEUE_POLL_S))):
            self._sleep(QUEUE_POLL_S)
            state = self._entry_state(binary, target, entry, deadline)
            if state in {None, "stalled"}:
                break
            if self._clock() >= deadline:
                break
        if state is None:
            return self._confirm_taken(binary, agent, text)
        # Still queued at the end of the window. Only an entry never typed can be
        # withdrawn cleanly; TUIOS refuses to drop one while it is being typed.
        ok, _ = self._herdr(binary, "queue", "rm", "--json", *target, entry)
        return "skipped", "queue_held" if ok and state == "waiting" else "delivery_uncertain"

    def _entry_state(
        self, binary: str, target: tuple[str, ...], entry: str, deadline: float
    ) -> str | None:
        """The entry's queue state, ``None`` once it left, ``unknown`` if unreadable."""
        timeout = max(0.01, min(HERDR_TIMEOUT_S, deadline - self._clock()))
        ok, data = self._herdr(binary, "queue", "ls", "--json", *target, timeout=timeout)
        entries = data.get("entries")
        if not ok or not isinstance(entries, list):
            return "unknown"
        for row in entries:
            if isinstance(row, dict) and row.get("id") == entry:
                return str(row.get("state"))
        return None

    def _confirm_taken(
        self, binary: str, before: dict[str, Any], text: str
    ) -> tuple[str, str | None]:
        """Report delivery only if the pane moved and shows the wake as its latest prompt.

        An entry also leaves when TUIOS drops it, and a person can start a turn at
        that moment, so a moved pane alone proves nothing. The harness's report of
        the prompt can lag the queue, hence a few looks.
        """
        for look in range(CONFIRM_LOOKS):
            if look:
                self._sleep(QUEUE_POLL_S)
            agents = self._agents(binary)
            after = next((a for a in agents or [] if a["pane_id"] == before["pane_id"]), None)
            if after is None:
                continue
            meta = after.get("meta")
            moved = (
                after["agent_status"] not in READY_STATES
                or after["state_change_seq"] != before["state_change_seq"]
            )
            if moved and isinstance(meta, dict) and meta.get("prompt") == text:
                return "delivered", None
        return "skipped", "delivery_uncertain"


class FallbackWaker:
    """Try each transport until one recognises the session."""

    def __init__(self, wakers: Sequence[HerdrWaker]):
        self._wakers = wakers

    def deliver(
        self, external_session_id: str, unread: int, **kwargs: Any
    ) -> tuple[str, str | None]:
        results: list[tuple[str, str | None]] = []
        for waker in self._wakers:
            outcome, reason = waker.deliver(external_session_id, unread, **kwargs)
            if reason not in {"no_match", f"{waker.name}_missing", f"{waker.name}_unavailable"}:
                return outcome, reason
            results.append((outcome, reason))
        for wanted in ("failed", "no_match"):
            for result in results:
                if wanted in result:
                    return result
        return results[-1]

    def live_sessions(self) -> set[str]:
        """Every session id any transport can see."""
        return set().union(*(waker.live_sessions() for waker in self._wakers))


def default_waker() -> FallbackWaker:
    return FallbackWaker([TuiosWaker(), HerdrWaker()])
