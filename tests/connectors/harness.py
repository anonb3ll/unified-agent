"""Connector conformance harness (task #1204).

``assert_connector_conforms`` is the single entry point every connector with a
registered ``ConnectorManifest`` must pass. It asserts the five clauses from
the task-#1204 DoD:

1. registers (and an invalid manifest is refused with a clear error);
2. reports state to ``system_doctor`` and the Connectors pane;
3. refuses to start without its named secrets;
4. emits hash-chained lifecycle events;
5. is invisible to profiles that lack it.

Each clause is also exposed as its own ``assert_*`` function so a connector's
own test module can call the subset that applies (a connector with no
approval-gated registration path, for instance, would still want clauses
2-5). ``docs/connectors.md`` documents how each clause maps to real code.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from unified_agent import config, connectors, events, profiles
from unified_agent.tools import approvals, doctor, horizon2


@contextmanager
def verified_identity(label: str = "Desktop_Agent2") -> Iterator[None]:
    """Set the verified agent identity for the duration of a ``with`` block."""
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _stub_approvals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the human approval queue the way the existing horizon2 tests do.

    Real registration is approval-gated by design (a human reviews scopes
    before a connector is durably registered) — the harness exercises the
    registration *path*, not the human-in-the-loop policy, which is already
    covered by ``tests/test_horizon2_tools.py``.
    """
    monkeypatch.setattr(
        approvals,
        "check_approval_gate",
        lambda tool, payload, **kwargs: {"gated": True, "approval_id": 9001},
    )


@contextmanager
def _isolated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep reusable conformance clauses out of the caller's SQLite database."""
    with TemporaryDirectory(prefix="connector-conformance-") as directory:
        db_path = Path(directory) / "memory.db"
        with pytest.MonkeyPatch.context() as isolated:
            isolated.setattr(config, "db_path", lambda: db_path)
            from unified_agent import storage

            storage.init_db()
            yield


def assert_registers(
    manifest: connectors.ConnectorManifest,
    monkeypatch: pytest.MonkeyPatch,
    *,
    server_id: str | None = None,
    scopes: list[str] | None = None,
) -> dict:
    """Clause 1a: a well-formed manifest's connector registers cleanly."""
    with _isolated_database(monkeypatch):
        _stub_approvals(monkeypatch)
        with verified_identity():
            result = horizon2.horizon2_server_register(
                server_id or f"{manifest.name}-conformance",
                manifest.name,
                "sha256:abcdef0123456789",
                scopes or ["read"],
                f"review:{manifest.name}-conformance",
            )
    assert result["server"]["connector"] == manifest.name
    assert result["server"]["active"] is True
    return result


def assert_invalid_manifest_refused(
    manifest: connectors.ConnectorManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clause 1b: a manifest whose entry point does not resolve is refused.

    Builds a broken copy of ``manifest`` (one entry point repointed at a
    function that does not exist) and registers it under the registry so
    ``horizon2_server_register`` looks it up by name, exactly as it would a
    real connector's manifest.
    """
    with _isolated_database(monkeypatch):
        capability = next(iter(manifest.capabilities))
        broken = replace(
            manifest,
            entry_points={
                **manifest.entry_points,
                capability: "unified_agent.connectors:_does_not_exist",
            },
        )
        connectors.register_manifest(broken, replace=True)
        try:
            _stub_approvals(monkeypatch)
            with verified_identity():
                with pytest.raises(Exception, match="invalid"):
                    horizon2.horizon2_server_register(
                        f"{manifest.name}-broken",
                        manifest.name,
                        "sha256:abcdef0123456789",
                        ["read"],
                        "review:broken",
                    )
        finally:
            connectors.register_manifest(manifest, replace=True)


def assert_reports_to_doctor_and_pane(manifest: connectors.ConnectorManifest) -> None:
    """Clause 2: system_doctor and horizon2_package_list (the pane's source) both see it."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        with _isolated_database(monkeypatch):
            report = doctor.system_doctor()
            capability_names = {check["capability"] for check in report["checks"]}
            assert f"connector:{manifest.name}" in capability_names

            packages = horizon2.horizon2_package_list()["packages"]
            entry = next(p for p in packages if p["package"] == manifest.name)
            assert entry["manifest"] == manifest.to_dict()
            assert set(entry["secrets"]) == {"present", "missing", "satisfied"}


def assert_refuses_to_start_without_secrets(
    manifest: connectors.ConnectorManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clause 3: the manifest-level start gate refuses when a named secret is absent.

    Alternatives-aware (task #1264): a manifest with no ``required_secrets``
    but at least one ``required_secret_alternatives`` group still has a real
    start gate to prove — clearing every name in every group must still
    refuse, and setting just ONE whole group (not all of them) must be
    enough to satisfy it. A manifest with neither is skipped, unchanged from
    before.
    """
    if not manifest.required_secrets and not manifest.required_secret_alternatives:
        pytest.skip(f"connector {manifest.name} declares no required secrets")
    for name in manifest.required_secrets:
        monkeypatch.delenv(name, raising=False)
    for group in manifest.required_secret_alternatives:
        for name in group:
            monkeypatch.delenv(name, raising=False)
    with pytest.raises(connectors.ConnectorSecretsMissingError):
        connectors.ensure_can_start(manifest)

    for name in manifest.required_secrets:
        monkeypatch.setenv(name, "test-value-not-a-real-secret")
    if manifest.required_secret_alternatives:
        # Any ONE whole group must be enough — prove that with the first one,
        # leaving every other group's names unset.
        for name in sorted(next(iter(manifest.required_secret_alternatives))):
            monkeypatch.setenv(name, "test-value-not-a-real-secret")
    status = connectors.ensure_can_start(manifest)
    assert status["satisfied"] is True
    assert status["missing"] == []
    # Names only — a value must never be echoed back.
    assert "test-value-not-a-real-secret" not in str(status)


def assert_emits_hash_chained_lifecycle_events(
    manifest: connectors.ConnectorManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clause 4: registration emits an event chained into the tamper-evident log."""
    with _isolated_database(monkeypatch):
        before = events.verify_event_chain()
        before_hash = before["head_hash"]

        _stub_approvals(monkeypatch)
        with verified_identity():
            result = horizon2.horizon2_server_register(
                f"{manifest.name}-hashchain",
                manifest.name,
                "sha256:abcdef0123456789",
                ["read"],
                "review:hashchain",
            )
        event_id = result["event_id"]
        matches = events.query_events(channels=["horizon2.connector.registered"], limit=50)
        row = next(e for e in matches if e["ulid"] == event_id)
        assert row["entry_hash"], "registration event must carry a chained entry_hash"
        assert row["prev_hash"] == (before_hash or events.GENESIS_HASH)

        verification = events.verify_event_chain()
        assert verification["status"] == "valid"


def assert_invisible_to_profiles_lacking_it(
    tool_name: str = "horizon2_server_register",
) -> None:
    """Clause 5: connector administration is unreachable to profiles that lack it.

    Reuses the existing profile exposure mechanism (``profiles.py``) rather
    than inventing a connector-specific visibility flag: research/operator/
    public are allow-list profiles that never named ``horizon2_*`` tools, so
    they stay unreachable automatically as new tools are added.
    """
    for absent_profile in (profiles.RESEARCH, profiles.OPERATOR, profiles.PUBLIC):
        assert not profiles.tool_is_allowed_for(absent_profile, tool_name), (
            f"{absent_profile} must not see {tool_name}"
        )
    for present_profile in (profiles.DEVELOPER, profiles.STEWARD, profiles.CHIEF_OF_STAFF):
        assert profiles.tool_is_allowed_for(present_profile, tool_name), (
            f"{present_profile} must be able to reach {tool_name}"
        )


def assert_connector_conforms(
    manifest: connectors.ConnectorManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run all five conformance clauses against one connector manifest."""
    assert_registers(manifest, monkeypatch)
    assert_invalid_manifest_refused(manifest, monkeypatch)
    assert_reports_to_doctor_and_pane(manifest)
    assert_refuses_to_start_without_secrets(manifest, monkeypatch)
    assert_emits_hash_chained_lifecycle_events(manifest, monkeypatch)
    assert_invisible_to_profiles_lacking_it()
