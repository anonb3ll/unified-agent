"""Discord connector manifest conformance (task #1269).

Discord's real code lives in ``unified_agent.ts_worker`` (task #770's
task-space worker; the ``ts`` is task-space, not TypeScript — there is no
separate TypeScript Discord implementation in this checkout). Everything
here drives a fake transport: no network call is ever made, and no token is
ever set to a real value.
"""

from __future__ import annotations

import pytest

from unified_agent import connectors

from .harness import assert_connector_conforms

DISCORD_MANIFEST = connectors.require_manifest("discord")


@pytest.fixture(autouse=True)
def _discord_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "test-token-not-real")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", "111111111111111111")
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "222222222222222222")


def test_discord_conforms_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    assert_connector_conforms(DISCORD_MANIFEST, monkeypatch)


def test_discord_manifest_describes_real_inert_entry_points() -> None:
    assert DISCORD_MANIFEST.default_enabled is False
    assert DISCORD_MANIFEST.capabilities == frozenset({"gateway", "worker"})
    assert DISCORD_MANIFEST.entry_points == {
        "gateway": "unified_agent.ts_worker.transport:DiscordGateway",
        "worker": "unified_agent.ts_worker.worker:TaskSpaceWorker",
    }
    assert DISCORD_MANIFEST.required_secrets == frozenset(
        {"UA_TS_DISCORD_TOKEN", "UA_TS_DISCORD_ALLOWED_USER_IDS", "UA_TS_DISCORD_CHANNELS"}
    )


def test_discord_gateway_resolves_to_the_real_fake_testable_adapter() -> None:
    """The `gateway` entry point is the class `tests/test_ts_worker.py::StubDiscord` subclasses."""
    from unified_agent.ts_worker import transport as ts_transport

    gateway_cls = DISCORD_MANIFEST.resolve_entry_point("gateway")
    assert gateway_cls is ts_transport.DiscordGateway
    # Constructing the real adapter never touches the network or a token —
    # only calling fetch_messages/send_message does, and this manifest never
    # calls them.
    instance = gateway_cls(transport=ts_transport.TsBotTransport())
    assert hasattr(instance, "fetch_messages")
    assert hasattr(instance, "send_message")


def test_discord_worker_resolves_to_the_real_ingestion_loop() -> None:
    from unified_agent.ts_worker import worker as ts_worker

    worker_cls = DISCORD_MANIFEST.resolve_entry_point("worker")
    assert worker_cls is ts_worker.TaskSpaceWorker
    assert hasattr(worker_cls, "poll_discord")


def test_discord_doctor_reports_inert_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from unified_agent.tools import doctor

    monkeypatch.delenv("UA_TS_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("UA_TS_DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("UA_TS_DISCORD_CHANNELS", raising=False)
    check = next(
        row for row in doctor.system_doctor()["checks"] if row["capability"] == "connector:discord"
    )
    assert check["status"] == "DORMANT"
    assert "UA_TS_DISCORD_TOKEN" in check["gate"]
    assert "test-token-not-real" not in str(check)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("UA_TS_DISCORD_TOKEN", "   "),
        ("UA_TS_DISCORD_ALLOWED_USER_IDS", ":"),
        ("UA_TS_DISCORD_CHANNELS", " : "),
    ],
)
def test_discord_secret_status_matches_runtime_normalization(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """A value the worker parses to nothing must not read as present."""
    monkeypatch.setenv(name, value)
    status = connectors.secret_status(DISCORD_MANIFEST)
    assert status["satisfied"] is False
    assert status["missing"] == [name]


def test_discord_secret_status_satisfied_with_real_values() -> None:
    status = connectors.secret_status(DISCORD_MANIFEST)
    assert status["satisfied"] is True
    assert "test-token-not-real" not in str(status)
