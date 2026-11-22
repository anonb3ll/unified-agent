"""GitHub App connector manifest conformance (task #1264).

Runs the generic five-clause harness against ``connectors.GITHUB_APP_MANIFEST``
(task #1201's poll-first connector, given a real manifest here) and proves
two connector-specific things the generic harness cannot:

1. The two token modes (task #1261) are *alternatives*, not both-required.
   ``GITHUB_APP_MANIFEST`` declares them under ``required_secret_alternatives``
   (an OR of ANDs), not the unconditionally-ANDed ``required_secrets`` --
   PR #703 review caught that the original shape made every valid
   single-mode configuration read as missing/MISCONFIGURED, because
   ``ConnectorManifest.required_secrets`` has AND semantics.
2. The connector stays inert even with every named secret present and an
   active registration, because ``github_app.enabled()`` is a separate
   runtime gate (``UA_GITHUB_APP_ENABLED``) the manifest alone does not flip.
"""

from __future__ import annotations

import pytest

from unified_agent import connectors, github_app

from .harness import assert_connector_conforms

GITHUB_APP_MANIFEST = connectors.require_manifest("github-app")

_APP_MODE_SECRETS = {
    "GITHUB_APP_ID": "123456",
    "GITHUB_APP_PRIVATE_KEY": "test-private-key-not-real",
}
_USER_MODE_SECRETS = {"UA_GITHUB_USER_TOKEN": "test-user-token-not-real"}
_USER_MODE_ENV = {**_USER_MODE_SECRETS, "UA_GITHUB_TOKEN_MODE": "user"}


@pytest.fixture(autouse=True)
def _no_github_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline: neither mode's secrets are set; tests opt into one or both."""
    for name in (*_APP_MODE_SECRETS, *_USER_MODE_SECRETS):
        monkeypatch.delenv(name, raising=False)


def _set(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_github_app_conforms_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    assert_connector_conforms(GITHUB_APP_MANIFEST, monkeypatch)


def test_github_app_manifest_describes_real_resolvable_entry_points() -> None:
    assert GITHUB_APP_MANIFEST.default_enabled is False
    assert GITHUB_APP_MANIFEST.capabilities == frozenset(
        {"poll", "check_runs", "commit_statuses", "evidence_comments"}
    )
    assert GITHUB_APP_MANIFEST.entry_points == {
        "poll": "unified_agent.pr_watch:run",
        "check_runs": "unified_agent.github_app:GitHubAppHttpTransport",
        "commit_statuses": "unified_agent.github_app:GitHubUserTokenTransport",
        "evidence_comments": "unified_agent.github_app:process_pr_event",
    }
    # No unconditional secret -- the App path and the user-token path are
    # alternatives, never both required at once.
    assert GITHUB_APP_MANIFEST.required_secrets == frozenset()
    assert set(GITHUB_APP_MANIFEST.required_secret_alternatives) == {
        frozenset({"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"}),
        frozenset({"UA_GITHUB_USER_TOKEN"}),
    }
    # Every declared entry point actually resolves to a real, importable callable.
    resolved = connectors.validate_entry_points_resolve(GITHUB_APP_MANIFEST)
    assert set(resolved) == GITHUB_APP_MANIFEST.capabilities


def test_app_mode_secrets_alone_satisfy_the_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, _APP_MODE_SECRETS)
    status = connectors.secret_status(GITHUB_APP_MANIFEST)
    assert status["satisfied"] is True
    assert status["missing"] == []
    assert set(status["present"]) == set(_APP_MODE_SECRETS)
    assert "installation token provider readiness is unknown" in status["error"]
    # The generic start gate must not raise for a real, single-mode config.
    connectors.ensure_can_start(GITHUB_APP_MANIFEST)


def test_user_token_mode_secret_alone_satisfies_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, _USER_MODE_ENV)
    status = connectors.secret_status(GITHUB_APP_MANIFEST)
    assert status["satisfied"] is True
    assert status["missing"] == []
    assert set(status["present"]) == set(_USER_MODE_SECRETS)
    connectors.ensure_can_start(GITHUB_APP_MANIFEST)


def test_only_the_active_modes_credentials_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default app mode with only a user token: not ready (build_transport can't use it).
    _set(monkeypatch, _USER_MODE_SECRETS)
    assert connectors.secret_status(GITHUB_APP_MANIFEST)["satisfied"] is False
    # User mode with only App credentials: not ready either.
    _set(monkeypatch, {**_APP_MODE_SECRETS, "UA_GITHUB_TOKEN_MODE": "user"})
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN", raising=False)
    status = connectors.secret_status(GITHUB_APP_MANIFEST)
    assert status["satisfied"] is False
    assert status["missing"] == ["UA_GITHUB_USER_TOKEN"]


def test_user_mode_honours_a_renamed_token_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN", raising=False)
    _set(
        monkeypatch,
        {
            "UA_GITHUB_TOKEN_MODE": "user",
            "UA_GITHUB_USER_TOKEN_VAR": "MY_GH_PAT",
            "MY_GH_PAT": "test-token-not-real",
        },
    )
    status = connectors.secret_status(GITHUB_APP_MANIFEST)
    assert status == {"present": ["MY_GH_PAT"], "missing": [], "satisfied": True}


def test_a_satisfied_alternative_reports_only_missing_base_secrets() -> None:
    manifest = connectors.ConnectorManifest(
        name="synthetic-alt",
        version="1.0.0",
        capabilities=frozenset({"poll"}),
        entry_points={"poll": "unified_agent.pr_watch:run"},
        required_secrets=frozenset({"TENANT"}),
        required_secret_alternatives=(frozenset({"TOKEN_A"}), frozenset({"TOKEN_B"})),
    )
    status = connectors.secret_status(manifest, env={"TOKEN_A": "x"})
    assert status == {"present": ["TOKEN_A"], "missing": ["TENANT"], "satisfied": False}


def test_manifests_without_new_fields_keep_their_serialized_shape() -> None:
    from unified_agent.connectors import SLACK_MANIFEST

    assert "required_secret_alternatives" not in SLACK_MANIFEST.to_dict()
    assert "secret_status_hook" not in SLACK_MANIFEST.to_dict()


def test_neither_mode_configured_is_missing_and_names_both_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = connectors.secret_status(GITHUB_APP_MANIFEST)
    assert status["satisfied"] is False
    # Default (app) mode: its own credentials are what's missing.
    assert set(status["missing"]) == {"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"}

    description = connectors.describe_required_secrets(GITHUB_APP_MANIFEST)
    assert "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY" in description
    assert "UA_GITHUB_USER_TOKEN" in description
    assert " or " in description

    with pytest.raises(connectors.ConnectorSecretsMissingError) as excinfo:
        connectors.ensure_can_start(GITHUB_APP_MANIFEST)
    message = str(excinfo.value)
    assert "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY" in message
    assert "UA_GITHUB_USER_TOKEN" in message


def test_github_app_doctor_reports_inert_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from unified_agent.tools import doctor

    check = next(
        row
        for row in doctor.system_doctor()["checks"]
        if row["capability"] == "connector:github-app"
    )
    assert check["status"] == "DORMANT"
    assert "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY" in check["gate"]
    assert "UA_GITHUB_USER_TOKEN" in check["gate"]


@pytest.mark.parametrize("mode_secrets", [_APP_MODE_SECRETS, _USER_MODE_ENV])
def test_github_app_doctor_row_per_mode(
    monkeypatch: pytest.MonkeyPatch, mode_secrets: dict[str, str]
) -> None:
    """Either mode alone reports satisfied secrets; still DORMANT (unregistered)."""
    from unified_agent.tools import doctor, horizon2

    # doctor prefers the hub SERVICE's own environment over the caller's
    # (see `doctor._service_env`'s docstring); on this host that unit is
    # readable but empty, so route the fixture's secrets through the same
    # seam Slack's equivalent doctor test uses rather than relying on
    # `os.environ` never being consulted.
    monkeypatch.setattr(doctor, "_service_env", lambda _unit: dict(mode_secrets))

    report = doctor.system_doctor()
    check = next(row for row in report["checks"] if row["capability"] == "connector:github-app")
    # Secrets present, but no active Horizon 2 registration -> still dormant,
    # never LIVE from secrets alone.
    if mode_secrets is _APP_MODE_SECRETS:
        assert check["status"] == "UNKNOWN"
        assert "installation token provider readiness is unknown" in check["detail"]
    else:
        assert check["status"] == "DORMANT"
        assert "no active Horizon 2 registration" in check["detail"]

    package = next(
        p for p in horizon2.horizon2_package_list()["packages"] if p["package"] == "github-app"
    )
    assert package["secrets"]["satisfied"] is True
    assert package["manifest"]["default_enabled"] is False


def test_github_app_runtime_stays_disabled_even_with_secrets_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest's ``default_enabled=False`` is structural; the real runtime

    gate is ``github_app.enabled()`` (``UA_GITHUB_APP_ENABLED``). A connector
    conforming to the manifest contract must still refuse to act until that
    separate, connector-specific gate is flipped -- registering a manifest,
    or even satisfying every named secret, must never itself turn a connector
    on.
    """
    _set(monkeypatch, _APP_MODE_SECRETS)
    monkeypatch.delenv("UA_GITHUB_APP_ENABLED", raising=False)
    assert github_app.enabled() is False

    from unified_agent.github_app import NormalizedPREvent, process_pr_event

    event = NormalizedPREvent(
        repo="example/repo",
        number=1,
        event_type="opened",
        title="test",
        url="https://github.com/example/repo/pull/1",
        state="OPEN",
        head_sha="deadbeef",
        base_branch="main",
        head_branch="feature",
        author_login="octocat",
    )
    result = process_pr_event(event, transport=object())  # type: ignore[arg-type]
    assert result == {"status": "disabled", "event_id": event.event_id}
