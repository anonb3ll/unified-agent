"""Passphrase-fallback rate limit (per IP and global)."""

from __future__ import annotations

import pytest

from unified_agent.oauth_approver import (
    PASSPHRASE_GLOBAL_LIMIT,
    PASSPHRASE_IP_LIMIT,
    PASSPHRASE_WINDOW_S,
    VIA_PASSPHRASE,
    VIA_WORKOS,
    Approval,
    ApprovalRefused,
    passphrase_ok,
    reset_passphrase_limits,
    verify,
)

SECRET = "correct horse battery"


class _Client:
    def __init__(self, host: str):
        self.host = host


class _Request:
    def __init__(self, host: str):
        self.client = _Client(host)
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    reset_passphrase_limits()
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", SECRET)
    monkeypatch.delenv("UA_MCP_OAUTH_APPROVER_LOGINS", raising=False)
    yield
    reset_passphrase_limits()


def _verify(ip: str, passphrase: str):
    return verify(_Request(ip), {"passphrase": passphrase}, binding="consent")


def test_sixth_attempt_is_refused_before_compare_even_if_correct(monkeypatch):
    seen: list[str] = []

    def spy(got: str) -> bool:
        seen.append(got)
        return passphrase_ok(got)

    monkeypatch.setattr("unified_agent.oauth_approver.passphrase_ok", spy)
    ip = "203.0.113.10"
    for _ in range(PASSPHRASE_IP_LIMIT):
        with pytest.raises(ApprovalRefused, match="Passphrase refused"):
            _verify(ip, "wrong")
    assert len(seen) == PASSPHRASE_IP_LIMIT
    with pytest.raises(ApprovalRefused, match="Passphrase refused"):
        _verify(ip, SECRET)
    assert len(seen) == PASSPHRASE_IP_LIMIT


def test_a_different_ip_still_gets_an_attempt():
    ip = "203.0.113.10"
    for _ in range(PASSPHRASE_IP_LIMIT):
        with pytest.raises(ApprovalRefused):
            _verify(ip, "wrong")
    approval = _verify("203.0.113.11", SECRET)
    assert approval.verified_via == VIA_PASSPHRASE
    with pytest.raises(ApprovalRefused):
        _verify(ip, SECRET)


def test_success_resets_only_that_ip():
    ip = "203.0.113.20"
    for _ in range(PASSPHRASE_IP_LIMIT - 1):
        with pytest.raises(ApprovalRefused):
            _verify(ip, "wrong")
    assert _verify(ip, SECRET).verified_via == VIA_PASSPHRASE
    for _ in range(PASSPHRASE_IP_LIMIT):
        with pytest.raises(ApprovalRefused):
            _verify(ip, "wrong")
    with pytest.raises(ApprovalRefused):
        _verify(ip, SECRET)


def test_global_ceiling_blocks_a_fresh_ip_before_compare(monkeypatch):
    seen: list[str] = []

    def spy(got: str) -> bool:
        seen.append(got)
        return False

    monkeypatch.setattr("unified_agent.oauth_approver.passphrase_ok", spy)
    for n in range(PASSPHRASE_GLOBAL_LIMIT):
        with pytest.raises(ApprovalRefused):
            _verify(f"198.51.100.{n % 200}", "wrong")
    seen.clear()
    with pytest.raises(ApprovalRefused):
        _verify("203.0.113.50", SECRET)
    assert seen == []


def test_workos_approval_ignores_another_ips_failures(monkeypatch):
    ip = "203.0.113.30"
    for _ in range(PASSPHRASE_IP_LIMIT):
        with pytest.raises(ApprovalRefused):
            _verify(ip, "wrong")

    def approved(request):
        return Approval(VIA_WORKOS, "Desktop_Person")

    monkeypatch.setattr("unified_agent.oauth_approver._workos_approval", approved)
    approval = verify(_Request(ip), {"passphrase": "wrong"}, binding="consent")
    assert approval.verified_via == VIA_WORKOS


def test_failures_age_out_of_the_window(monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr("unified_agent.oauth_approver.time.monotonic", lambda: clock["now"])
    ip = "203.0.113.40"
    for _ in range(PASSPHRASE_IP_LIMIT):
        with pytest.raises(ApprovalRefused):
            _verify(ip, "wrong")
    with pytest.raises(ApprovalRefused):
        _verify(ip, SECRET)
    clock["now"] += PASSPHRASE_WINDOW_S + 1
    approval = _verify(ip, SECRET)
    assert approval.verified_via == VIA_PASSPHRASE
