"""Doctor ts_worker capability states — env-driven, no tokens needed."""

import importlib

from unified_agent.tools import doctor


def _clear_ts_env(monkeypatch):
    for name in (
        "UA_TS_DISCORD_TOKEN",
        "UA_TS_TELEGRAM_TOKEN",
        "UA_TS_DISCORD_ALLOWED_USER_IDS",
        "UA_TS_TELEGRAM_ALLOWED_USER_IDS",
        "UA_TS_DISCORD_CHANNELS",
    ):
        monkeypatch.delenv(name, raising=False)


def _ts_check(monkeypatch):
    # doctor reads env at call time through ts_transport helpers; reimport is
    # unnecessary, but clearing then setting env keeps cases independent.
    report = doctor.system_doctor()
    return next(c for c in report["checks"] if c["capability"] == "ts_worker")


def test_ts_worker_dormant_without_tokens(monkeypatch):
    _clear_ts_env(monkeypatch)
    check = _ts_check(monkeypatch)
    assert check["status"] == "DORMANT"
    assert "UA_TS_DISCORD_TOKEN" in check["detail"]


def test_ts_worker_misconfigured_with_token_but_no_allowlist(monkeypatch):
    _clear_ts_env(monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    check = _ts_check(monkeypatch)
    assert check["status"] == "MISCONFIGURED"
    assert "allowlist" in check["detail"]


def test_ts_worker_misconfigured_with_token_but_no_channels(monkeypatch):
    _clear_ts_env(monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", "111")
    check = _ts_check(monkeypatch)
    assert check["status"] == "MISCONFIGURED"
    assert "UA_TS_DISCORD_CHANNELS" in check["detail"]


def test_ts_worker_live_with_full_discord_config(monkeypatch):
    _clear_ts_env(monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    check = _ts_check(monkeypatch)
    assert check["status"] == "LIVE"
    assert "discord" in check["detail"]


def test_ts_worker_live_with_full_telegram_config_only(monkeypatch):
    _clear_ts_env(monkeypatch)
    monkeypatch.setenv("UA_TS_TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_TELEGRAM_ALLOWED_USER_IDS", "222")
    check = _ts_check(monkeypatch)
    assert check["status"] == "LIVE"
    assert "telegram" in check["detail"]


def test_ts_worker_check_present_exactly_once():
    importlib.reload(doctor)
    report = doctor.system_doctor()
    caps = [c["capability"] for c in report["checks"]]
    assert caps.count("ts_worker") == 1
