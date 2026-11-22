"""ua-node's herdr adapter for idle-session wakes (task #1873).

A fake ``herdr`` on ``PATH`` answers ``agent list`` from a JSON file and logs
every argv it is called with, so these tests never reach a real herdr server
or pane. Existing submission/lease tests explicitly stub the unsupported
composer-safety predicate; they verify only those independent mechanics, not
supported automatic delivery. Default-path regressions below prove hook-only
behavior even with a supposedly empty input flag. Scenario names follow the design's S1-S9 table (S5 injection, S7 busy,
S9 failure).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from unified_agent import node_wake_transport as transport

EXT = "sess-123"
FAKE = """#!{python}
import json, os, sys
state = json.load(open(os.environ["FAKE_HERDR_STATE"]))
with open(os.environ["FAKE_HERDR_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["agent", "list"]:
    if state.get("list_error"):
        sys.stderr.write(json.dumps({{"error": {{"code": "server_not_running"}}}}))
        sys.exit(1)
    print(json.dumps({{"id": "cli:agent:list", "result": {{"agents": state["agents"]}}}}))
    sys.exit(0)
if args[:2] == ["agent", "prompt"]:
    code = state.get("prompt_error")
    if code:
        sys.stderr.write(json.dumps({{"error": {{"code": code}}}}))
        sys.exit(1)
    print(json.dumps({{"id": "cli:agent:prompt", "result": {{"submitted": True}}}}))
    sys.exit(0)
sys.exit(2)
"""


def _agent(**over):
    agent = {
        "pane_id": "w1:p1",
        "name": None,
        "agent": "claude",
        "agent_status": "idle",
        "focused": False,
        "state_change_seq": 4,
        "agent_session": {"value": EXT, "agent": "claude", "kind": "id", "source": "herdr:claude"},
    }
    agent.update(over)
    return agent


class Herdr:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = tmp_path / "bin"
        self.dir.mkdir()
        self.state_path = tmp_path / "state.json"
        self.log_path = tmp_path / "argv.log"
        binary = self.dir / "herdr"
        binary.write_text(FAKE.format(python=sys.executable))
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.delenv("UA_HERDR_BIN", raising=False)
        monkeypatch.setenv("FAKE_HERDR_STATE", str(self.state_path))
        monkeypatch.setenv("FAKE_HERDR_LOG", str(self.log_path))
        self.set(agents=[_agent()])

    def set(self, **state):
        self.state_path.write_text(json.dumps(state))

    def calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def prompts(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:2] == ["agent", "prompt"]]


@pytest.fixture
def herdr(tmp_path, monkeypatch):
    return Herdr(tmp_path, monkeypatch)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _waker(herdr, *, idle_for_s=0.0, clock=None, sleeps=None):
    waker = transport.HerdrWaker(
        clock=clock or Clock(),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        idle_for_s=idle_for_s,
    )
    # Submission mechanics are unreachable in production on current Herdr.
    waker._refusal = waker._idle_refusal
    assert waker.binary() == str(herdr.dir / "herdr")  # never the real herdr
    return waker


def test_match_and_success_argv(herdr):
    assert _waker(herdr).deliver(EXT, 2) == ("delivered", None)
    assert herdr.prompts() == [
        ["agent", "prompt", "w1:p1", "You have 2 new hub messages; run message_inbox"]
    ]
    assert herdr.calls()[0] == ["agent", "list"]  # matched on a fresh list first


def test_idle_must_hold_for_thirty_seconds(herdr):
    clock = Clock()
    waker = _waker(herdr, idle_for_s=transport.IDLE_FOR_S, clock=clock)
    assert waker.deliver(EXT, 1) == ("skipped", "not_idle_long_enough")
    clock.now += 29
    assert waker.deliver(EXT, 1) == ("skipped", "not_idle_long_enough")
    clock.now += 2
    assert waker.deliver(EXT, 1) == ("delivered", None)


def test_a_new_turn_restarts_the_idle_clock(herdr):
    clock = Clock()
    waker = _waker(herdr, idle_for_s=30, clock=clock)
    waker.deliver(EXT, 1)
    clock.now += 31
    herdr.set(agents=[_agent(state_change_seq=5)])  # it worked and went idle again
    assert waker.deliver(EXT, 1) == ("skipped", "not_idle_long_enough")
    assert herdr.prompts() == []


def test_focused_pane_is_skipped(herdr):
    herdr.set(agents=[_agent(focused=True)])
    assert _waker(herdr).deliver(EXT, 1) == ("skipped", "focused")
    assert herdr.prompts() == []


@pytest.mark.parametrize(
    "interruption",
    [{"focused": True}, {"agent_status": "working"}, {"agent_status": "blocked"}],
    ids=["focused", "busy", "blocked"],
)
def test_a_focused_or_busy_sighting_restarts_the_idle_clock(herdr, interruption):
    """Idle 60 s, someone focuses (or it works), then unfocused with the same seq (#4267487477)."""
    clock = Clock()
    waker = _waker(herdr, idle_for_s=30, clock=clock)
    waker.deliver(EXT, 1)  # starts the clock
    clock.now += 60
    herdr.set(agents=[_agent(**interruption)])
    assert waker.deliver(EXT, 1)[0] == "skipped"
    herdr.set(agents=[_agent()])  # unchanged state_change_seq
    assert waker.deliver(EXT, 1) == ("skipped", "not_idle_long_enough")
    clock.now += 31
    assert waker.deliver(EXT, 1) == ("delivered", None)
    assert len(herdr.prompts()) == 1


def test_blocked_agent_is_skipped(herdr):
    herdr.set(agents=[_agent(agent_status="blocked")])
    assert _waker(herdr).deliver(EXT, 1) == ("skipped", "blocked")
    assert herdr.prompts() == []


def test_s7_busy_agent_is_skipped(herdr):
    herdr.set(agents=[_agent(agent_status="working")])
    assert _waker(herdr).deliver(EXT, 1) == ("skipped", "not_idle")
    herdr.set(agents=[_agent(agent_status="unknown")])
    assert _waker(herdr).deliver(EXT, 1) == ("skipped", "not_idle")
    assert herdr.prompts() == []


@pytest.mark.parametrize(
    "agents",
    [
        [],  # pane gone
        [_agent(agent_session={"value": "someone-else"})],  # pane reused by another agent
        [_agent(agent_session=None)],
        [_agent(), _agent(pane_id="w1:p2")],  # ambiguous
        [_agent(pane_id="--help")],  # not a pane id
    ],
)
def test_no_match_is_skipped_and_never_typed(herdr, agents):
    herdr.set(agents=agents)
    assert _waker(herdr).deliver(EXT, 1) == ("skipped", "no_match")
    assert herdr.prompts() == []


def test_prompt_refused_as_blocked_is_a_skip(herdr):
    herdr.set(agents=[_agent()], prompt_error="agent_blocked")
    assert _waker(herdr).deliver(EXT, 1) == ("skipped", "blocked")
    assert len(herdr.prompts()) == 1


def test_s9_herdr_down_retries_with_backoff_then_fails(herdr):
    herdr.set(list_error=True)
    sleeps: list[float] = []
    assert _waker(herdr, sleeps=sleeps).deliver(EXT, 1) == ("failed", "herdr_unavailable")
    assert len(herdr.calls()) == transport.ATTEMPTS
    assert sleeps == [1.0, 2.0]


def test_prompt_stalled_is_uncertain_and_never_retried(herdr):
    herdr.set(agents=[_agent()], prompt_error="agent_prompt_stalled")
    assert _waker(herdr).deliver(EXT, 1) == ("skipped", "delivery_uncertain")
    assert len(herdr.prompts()) == 1


def test_missing_herdr_is_a_final_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("UA_HERDR_BIN", raising=False)
    assert transport.HerdrWaker().deliver(EXT, 1) == ("skipped", "herdr_missing")


def test_s5_text_is_only_the_template_and_an_integer(herdr):
    with pytest.raises(ValueError):
        _waker(herdr).deliver(EXT, "1; rm -rf ~")
    assert herdr.calls() == []


def test_argv_list_no_shell_and_a_timeout():
    seen: list[tuple[list[str], dict]] = []

    class Proc:
        returncode = 0
        stdout = json.dumps({"result": {"agents": [_agent()]}})
        stderr = ""

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        proc = Proc()
        if argv[1:3] == ["agent", "prompt"]:
            proc.stdout = json.dumps({"result": {"submitted": True}})
        return proc

    waker = transport.HerdrWaker(binary="/fake/herdr", run=run, idle_for_s=0)
    waker._refusal = waker._idle_refusal
    assert waker.deliver(EXT, 1) == ("delivered", None)
    for argv, kwargs in seen:
        assert isinstance(argv, list) and argv[0] == "/fake/herdr"
        assert "shell" not in kwargs and kwargs["timeout"] == transport.HERDR_TIMEOUT_S
    assert "send-text" not in json.dumps([argv for argv, _ in seen])


def test_uncertain_prompt_stops_before_another_grant_check(herdr):
    herdr.set(agents=[_agent()], prompt_error="temporary_failure")
    decisions = iter([60.0, 0.0])
    waker = _waker(herdr)
    assert waker.deliver(EXT, 1, authorize=lambda: next(decisions)) == (
        "skipped",
        "delivery_uncertain",
    )
    assert len(herdr.prompts()) == 1


def test_offer_expiring_during_fresh_pane_listing_is_never_typed(herdr):
    clock = Clock()
    waker = transport.HerdrWaker(clock=clock, wall_clock=clock, idle_for_s=0)
    waker._refusal = waker._idle_refusal
    original = waker._agents

    def delayed(binary):
        agents = original(binary)
        clock.now += 20
        return agents

    waker._agents = delayed
    assert waker.deliver(EXT, 1, expires_at=1010.0, authorize=lambda: 60) == (
        "skipped",
        "offer_invalid",
    )
    assert herdr.prompts() == []


def test_authorization_latency_cannot_extend_server_lease(herdr):
    clock = Clock()
    waker = _waker(herdr, clock=clock)

    def delayed_check():
        clock.now += 3
        return 2.0

    assert waker.deliver(EXT, 1, authorize=delayed_check) == ("skipped", "offer_invalid")
    assert herdr.prompts() == []


def test_prompt_timeout_is_bounded_by_remaining_authorized_lease(herdr):
    clock = Clock()
    waker = _waker(herdr, clock=clock)
    original = waker._run
    timeouts = []

    def capture(*args, **kwargs):
        if args[0][1:3] == ["agent", "prompt"]:
            timeouts.append(kwargs["timeout"])
        return original(*args, **kwargs)

    waker._run = capture
    assert waker.deliver(EXT, 1, authorize=lambda: 0.5) == ("delivered", None)
    assert timeouts == [0.5]


def test_pane_focus_during_hub_check_is_refreshed_before_typing(herdr):
    waker = _waker(herdr)

    def authorize():
        herdr.set(agents=[_agent(focused=True)])
        return 60.0

    assert waker.deliver(EXT, 1, authorize=authorize) == ("skipped", "focused")
    assert herdr.prompts() == []


@pytest.mark.parametrize(
    "failure", ["timeout", "oserror", "malformed_failure", "malformed_success"]
)
def test_uncertain_post_submission_failure_never_retries_the_prompt(failure):
    import subprocess
    from types import SimpleNamespace

    prompts = []

    def run(argv, **kwargs):
        if argv[1:3] == ["agent", "list"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"result": {"agents": [_agent()]}}), stderr=""
            )
        prompts.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "oserror":
            raise OSError("response lost after possible submission")
        return SimpleNamespace(
            returncode=0 if failure == "malformed_success" else 1,
            stdout="not valid JSON",
            stderr="",
        )

    waker = transport.HerdrWaker(binary="fake", run=run, idle_for_s=0, sleep=lambda _: None)
    waker._refusal = waker._idle_refusal
    assert waker.deliver(EXT, 1, authorize=lambda: 60) == ("skipped", "delivery_uncertain")
    assert len(prompts) == 1


@pytest.mark.parametrize("untrusted_extra", [{}, {"draft": ""}, {"input_empty": True}])
def test_default_adapter_cannot_attest_empty_composer(herdr, untrusted_extra):
    """Real v0.9.3 canary stayed idle/unfocused with a retained draft for 31s."""
    herdr.set(agents=[_agent(**untrusted_extra)])
    clock = Clock()
    waker = transport.HerdrWaker(clock=clock)
    assert waker.deliver(EXT, 1) == ("skipped", "not_idle_long_enough")
    clock.now += 31
    authorizations = []
    assert waker.deliver(EXT, 1, authorize=lambda: authorizations.append(True)) == (
        "skipped",
        "input_state_unavailable",
    )
    assert authorizations == []
    assert herdr.prompts() == []


def test_default_predicate_never_marks_current_herdr_ready(herdr):
    clock = Clock()
    waker = transport.HerdrWaker(clock=clock)
    assert waker._refusal(_agent()) == "not_idle_long_enough"
    clock.now += 31
    assert waker._refusal(_agent()) == "input_state_unavailable"


NATIVE_CODEX_ID = "01a13abe-8139-79b0-86bc-aed26fa3b5d2"


def _native_codex(**over):
    return _agent(
        agent="codex",
        agent_session={
            "agent": "codex",
            "kind": "id",
            "source": "herdr:codex",
            "value": NATIVE_CODEX_ID,
            **over,
        },
    )


def test_real_codex_identity_maps_only_its_verified_harness():
    agent = _native_codex()
    assert transport.HerdrWaker._match([agent], "codex:" + NATIVE_CODEX_ID) == agent
    assert transport.HerdrWaker._match([agent], NATIVE_CODEX_ID) is None
    assert transport.HerdrWaker._match([agent], "hermes:" + NATIVE_CODEX_ID) is None


@pytest.mark.parametrize(
    "over",
    [
        {"agent": "claude"},
        {"kind": "path"},
        {"source": "external"},
        {"value": "not-a-uuid"},
        {"value": "00000000-0000-0000-0000-000000000000"},
        {"value": "01a13abe-8139-79b0-16bc-aed26fa3b5d2"},
        {"value": "codex:" + NATIVE_CODEX_ID},
    ],
)
def test_codex_mapping_rejects_inconsistent_or_malformed_metadata(over):
    assert transport.HerdrWaker._match([_native_codex(**over)], "codex:" + NATIVE_CODEX_ID) is None


def test_codex_mapping_requires_one_native_occupant_and_still_refuses_input(herdr):
    agent = _native_codex()
    duplicate = {**agent, "pane_id": "w1:p2"}
    external = "codex:" + NATIVE_CODEX_ID
    assert transport.HerdrWaker._match([agent, duplicate], external) is None
    assert transport.HerdrWaker._match([{**agent, "pane_id": "--help"}], external) is None
    assert transport.HerdrWaker._match([{**agent, "agent": "claude"}], external) is None
    herdr.set(agents=[agent])
    assert transport.HerdrWaker(idle_for_s=0).deliver(external, 1) == (
        "skipped",
        "input_state_unavailable",
    )
    assert herdr.prompts() == []


@pytest.mark.parametrize(
    "session",
    [
        {"value": EXT},
        {"value": EXT, "agent": "codex", "kind": "id", "source": "herdr:codex"},
        {"value": EXT, "agent": "claude", "kind": "path", "source": "herdr:claude"},
        {
            "value": "codex:" + NATIVE_CODEX_ID,
            "agent": "claude",
            "kind": "id",
            "source": "herdr:claude",
        },
    ],
)
def test_exact_id_matching_rejects_untrusted_or_cross_harness_metadata(session):
    agent = _agent(agent_session=session)
    assert transport.HerdrWaker._match([agent], session["value"]) is None


TUIOS_FAKE = """#!{python}
import json, os, sys
state = json.load(open(os.environ["FAKE_TUIOS_STATE"]))
with open(os.environ["FAKE_TUIOS_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[0] == "list-agents":
    if state.get("list_error"):
        sys.exit(1)
    agents = state["agents"]
    if state.get("typed"):
        typed = state["typed"]
        agents = [dict(a, state="working", ready=False, meta={{"prompt": typed}}) for a in agents]
    print(json.dumps({{"agents": agents, "success": True}}))
elif args[:2] == ["queue", "ls"]:
    print(json.dumps({{"entries": state.get("entries", []), "success": True}}))
elif args[:2] == ["queue", "rm"]:
    sys.exit(state.get("rm_exit", 0))
elif args[0] == "queue":
    if state.get("queue_error"):
        sys.exit(1)
    if state.get("takes", True):
        state["typed"] = state.get("other_turn") or args[-1]
        json.dump(state, open(os.environ["FAKE_TUIOS_STATE"], "w"))
    print(json.dumps({{"type": "prompt_queued", "id": "q1", "delivering": True}}))
else:
    sys.exit(2)
"""
WINDOW = "60243d1b-cfce-41e4-9e62-bdf5d5485c17"
CODEX_ID = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"


def _pane(**over):
    pane = {
        "window_id": WINDOW,
        "session": "main",
        "harness_id": "claude-code",
        "agent_session_id": EXT,
        "state": "idle",
        "ready": True,
        "focused": False,
        "draft_held": False,
        "needs_you": False,
        "blocked_by": "",
        "queued": 0,
        "completion_seq": 7,
    }
    pane.update(over)
    return pane


class Tuios:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = tmp_path / "tuios-bin"
        self.dir.mkdir()
        self.state_path = tmp_path / "tuios.json"
        self.log_path = tmp_path / "tuios.log"
        binary = self.dir / "tuios"
        binary.write_text(TUIOS_FAKE.format(python=sys.executable))
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.delenv("UA_TUIOS_BIN", raising=False)
        monkeypatch.setenv("FAKE_TUIOS_STATE", str(self.state_path))
        monkeypatch.setenv("FAKE_TUIOS_LOG", str(self.log_path))
        self.set(agents=[_pane()])

    def set(self, **state):
        self.state_path.write_text(json.dumps(state))

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def queued(self) -> list[list[str]]:
        return [c for c in self.calls() if c[0] == "queue" and c[1] == "--json"]

    def removed(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:2] == ["queue", "rm"]]


@pytest.fixture
def tuios(tmp_path, monkeypatch):
    return Tuios(tmp_path, monkeypatch)


def _tuios_waker(tuios, *, idle_for_s=0.0, clock=None):
    waker = transport.TuiosWaker(
        clock=clock or Clock(), sleep=lambda s: None, idle_for_s=idle_for_s
    )
    assert waker.binary() == str(tuios.dir / "tuios")  # never the real daemon
    return waker


def test_tuios_queues_the_fixed_line_for_the_matched_pane(tuios):
    assert _tuios_waker(tuios).deliver(EXT, 2) == ("delivered", None)
    line = "You have 2 new hub messages; run message_inbox"
    assert tuios.queued() == [["queue", "--json", "-s", "main", "-w", WINDOW, "--", line]]
    assert tuios.calls()[0] == ["list-agents", "--all-sessions", "--json"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"focused": True}, "focused"),
        ({"focused": None}, "focused"),
        ({"state": "needs_input"}, "blocked"),
        ({"needs_you": True}, "blocked"),
        ({"blocked_by": "permission"}, "blocked"),
        ({"state": "working"}, "not_idle"),
        ({"draft_held": True}, "draft_held"),
        ({"draft_held": None}, "draft_held"),
        ({"queued": 1}, "queue_busy"),
        ({"ready": False}, "not_idle"),
    ],
)
def test_tuios_never_queues_for_a_pane_that_is_not_safe(tuios, change, reason):
    tuios.set(agents=[_pane(**change)])
    assert _tuios_waker(tuios).deliver(EXT, 1) == ("skipped", reason)
    assert tuios.queued() == []


def test_tuios_idle_must_hold_and_a_finished_turn_restarts_it(tuios):
    clock = Clock()
    waker = _tuios_waker(tuios, idle_for_s=30.0, clock=clock)
    assert waker.deliver(EXT, 1) == ("skipped", "not_idle_long_enough")
    clock.now += 31
    tuios.set(agents=[_pane(completion_seq=8)])
    assert waker.deliver(EXT, 1) == ("skipped", "not_idle_long_enough")
    clock.now += 31
    assert waker.deliver(EXT, 1) == ("delivered", None)


@pytest.mark.parametrize(
    "change",
    [
        {"agent_session_id": ""},
        {"agent_session_id": "other"},
        {"harness_id": "aider"},
        {"window_id": "-w"},
        {"session": "--server"},
    ],
)
def test_tuios_unmatched_or_unsafe_ids_are_never_typed_at(tuios, change):
    tuios.set(agents=[_pane(**change)])
    assert _tuios_waker(tuios).deliver(EXT, 1) == ("skipped", "no_match")
    assert tuios.queued() == []


def test_tuios_two_panes_claiming_a_session_is_no_match(tuios):
    tuios.set(agents=[_pane(), _pane(window_id="other-window")])
    assert _tuios_waker(tuios).deliver(EXT, 1) == ("skipped", "no_match")


def test_tuios_codex_uses_the_hook_prefix():
    pane = transport.TuiosWaker._row(_pane(harness_id="codex", agent_session_id=CODEX_ID))
    assert transport.TuiosWaker._match([pane], f"codex:{CODEX_ID}") is pane
    assert transport.TuiosWaker._match([pane], CODEX_ID) is None


def test_tuios_held_entry_is_withdrawn_when_the_window_ends(tuios):
    tuios.set(agents=[_pane()], entries=[{"id": "q1", "state": "waiting"}])
    assert _tuios_waker(tuios).deliver(EXT, 1) == ("skipped", "queue_held")
    assert tuios.removed() == [["queue", "rm", "--json", "-s", "main", "-w", WINDOW, "q1"]]


@pytest.mark.parametrize("state", ["delivering", "stalled"])
def test_tuios_entry_that_may_be_typed_is_uncertain_and_not_retried(tuios, state):
    tuios.set(agents=[_pane()], entries=[{"id": "q1", "state": state}])
    assert _tuios_waker(tuios).deliver(EXT, 1) == ("skipped", "delivery_uncertain")
    assert len(tuios.queued()) == 1


def test_tuios_entry_gone_but_pane_unmoved_is_uncertain(tuios):
    tuios.set(agents=[_pane()], takes=False)
    assert _tuios_waker(tuios).deliver(EXT, 1) == ("skipped", "delivery_uncertain")


def test_tuios_dropped_entry_during_a_persons_turn_is_not_delivered(tuios):
    tuios.set(agents=[_pane()], other_turn="fix the failing build")
    assert _tuios_waker(tuios).deliver(EXT, 1) == ("skipped", "delivery_uncertain")


def test_tuios_prompt_report_may_lag_the_queue(tuios):
    text = "You have 1 new hub messages; run message_inbox"
    waker = _tuios_waker(tuios)
    before = waker._row(_pane())
    working = waker._row(_pane(state="working"))
    listings = iter([[working], [{**working, "meta": {"prompt": text}}]])
    waker._agents = lambda binary: next(listings)
    assert waker._confirm_taken("tuios", before, text) == ("delivered", None)


def test_tuios_refused_queue_is_uncertain_and_not_retried(tuios):
    tuios.set(agents=[_pane()], queue_error=True)
    assert _tuios_waker(tuios).deliver(EXT, 1) == ("skipped", "delivery_uncertain")
    assert len(tuios.queued()) == 1


def test_tuios_expired_grant_never_queues(tuios):
    outcome = _tuios_waker(tuios).deliver(EXT, 1, authorize=lambda: 0.0)
    assert outcome == ("skipped", "offer_invalid")
    assert tuios.queued() == []


def test_session_in_neither_terminal_falls_through_to_no_match(tuios, herdr):
    tuios.set(agents=[])
    herdr.set(agents=[])
    assert transport.default_waker().deliver(EXT, 1) == ("skipped", "no_match")


def test_herdr_session_still_reaches_the_herdr_refusal(tuios, herdr):
    tuios.set(agents=[])
    outcome = transport.FallbackWaker([_tuios_waker(tuios), _waker_unstubbed(herdr)]).deliver(
        EXT, 1
    )
    assert outcome == ("skipped", "input_state_unavailable")
    assert herdr.prompts() == []


def test_a_tuios_refusal_does_not_fall_through_to_herdr(tuios, herdr):
    tuios.set(agents=[_pane(focused=True)])
    outcome = transport.FallbackWaker([_tuios_waker(tuios), _waker(herdr)]).deliver(EXT, 1)
    assert outcome == ("skipped", "focused")
    assert herdr.calls() == []


def test_daemon_down_falls_through_and_reports_failure_if_nothing_matches(tuios, herdr):
    tuios.set(agents=[], list_error=True)
    herdr.set(agents=[])
    outcome = transport.FallbackWaker([_tuios_waker(tuios), _waker(herdr)]).deliver(EXT, 1)
    assert outcome == ("failed", "tuios_unavailable")


def _waker_unstubbed(herdr):
    return transport.HerdrWaker(clock=Clock(), sleep=lambda s: None, idle_for_s=0.0)


# --- live sessions, reported on the wake poll (task #2428) ---


def test_live_sessions_lists_verified_panes_only(herdr):
    other = {"value": "sess-b", "agent": "claude", "kind": "id", "source": "herdr:claude"}
    spoofed = {**other, "value": "sess-c", "source": "elsewhere"}
    herdr.set(
        agents=[
            _agent(),
            _agent(pane_id="w1:p2", agent_session=other),
            _agent(pane_id="w1:p3", agent_session=spoofed),
        ]
    )
    assert _waker(herdr).live_sessions() == {EXT, "sess-b"}


def test_live_sessions_leaves_out_a_session_two_panes_claim(herdr):
    herdr.set(agents=[_agent(), _agent(pane_id="w1:p2")])
    assert _waker(herdr).live_sessions() == set()


def test_live_sessions_is_empty_when_herdr_is_down_or_missing(herdr, tmp_path, monkeypatch):
    herdr.set(agents=[], list_error=True)
    assert _waker(herdr).live_sessions() == set()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert transport.HerdrWaker().live_sessions() == set()


def test_listing_live_sessions_types_nothing(herdr):
    _waker(herdr).live_sessions()
    assert herdr.prompts() == []


def test_tuios_live_sessions_uses_its_own_identity_fields(tuios):
    tuios.set(agents=[_pane(), _pane(window_id="w2", agent_session_id="other", harness_id="aider")])
    assert _tuios_waker(tuios).live_sessions() == {EXT}


def test_fallback_live_sessions_is_the_union():
    class Stub:
        def __init__(self, live):
            self.live = live

        def live_sessions(self):
            return self.live

    assert transport.FallbackWaker([Stub({"a"}), Stub({"b"})]).live_sessions() == {"a", "b"}
