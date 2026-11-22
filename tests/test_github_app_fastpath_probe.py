"""The conditional open-heads probe and the tick planner (task #2234, part b).

``open_heads_changed`` is one conditional GitHub request that tells the tick
planner whether open heads moved since the last poll, so ``pr-watch`` can run
often without listing every open pull request each time. GitHub is mocked.

Part (a), what happens after an approval decision, is in
``test_github_app_fastpath.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from unified_agent import (
    config,
    github_app,
    github_app_fastpath,
    github_app_worker,
    pr_watch,
)

SERVICE = github_app_worker.SERVICE_IDENTITY
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


# --- (b) before a poll: the conditional probe ------------------------------------------


def _list_page(*heads):
    return [{"number": n, "head": {"sha": sha}} for n, sha in heads]


def _included(status: int, etag: str | None, body: object | None, link: str = "") -> str:
    lines = [f"HTTP/2.0 {status} {'OK' if status == 200 else 'Not Modified'}"]
    if etag:
        lines.append(f'Etag: W/"{etag}"')
    if link:
        lines.append(f"Link: {link}")
    lines.append("X-Ratelimit-Remaining: 4999")
    text = "\r\n".join(lines) + "\r\n\r\n"
    return text + (json.dumps(body) if body is not None else "")


@pytest.fixture
def probe(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    state = SimpleNamespace(responses=[], calls=[])

    def gh(cmd, **_kwargs):
        state.calls.append(list(cmd))
        status, etag, body, *link = state.responses.pop(0)
        return SimpleNamespace(
            returncode=0 if status == 200 else 1, stdout=_included(status, etag, body, *link)
        )

    state.gh = gh
    return state


def _seen(heads: dict[int, str]) -> None:
    github_app_worker._save_open_heads("example/project", heads)


def test_probe_reports_a_new_head_then_answers_304_from_its_stored_page(probe):
    _seen({123: "head-1"})
    probe.responses.append((200, "v1", _list_page((123, "head-2"))))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is True
    [first] = probe.calls
    assert "--include" in first and not any(arg.startswith("If-None-Match") for arg in first)
    assert "sort=updated" in first[-1] and "base=main" in first[-1]

    # Unchanged on GitHub (304), but the poll has not caught up yet: still a change.
    probe.responses.append((304, None, None))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is True
    assert probe.calls[-1][-2:] == ["-H", 'If-None-Match: W/"v1"']

    # Once the poll records the new head, the same 304 means nothing to do.
    _seen({123: "head-2"})
    probe.responses.append((304, None, None))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is False


def test_probe_sees_a_new_pull_request_and_a_closed_one(probe):
    _seen({123: "head-1"})
    probe.responses.append((200, "v1", _list_page((124, "new"), (123, "head-1"))))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is True
    _seen({123: "head-1", 124: "new"})
    probe.responses.append((200, "v2", _list_page((124, "new"))))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is True


def test_probe_ignores_an_edit_that_moves_no_head(probe):
    _seen({123: "head-1"})
    probe.responses.append((200, "v2", _list_page((123, "head-1"))))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is False


def test_full_first_page_always_runs_the_full_poll(probe):
    # Review P3: one page of several cannot vouch for the others, so neither
    # its 200 nor a later 304 may skip the poll.
    heads = {n: f"sha-{n}" for n in range(1, github_app_fastpath.PROBE_PAGE + 1)}
    _seen(heads)
    probe.responses.append((200, "v1", _list_page(*heads.items())))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is True
    probe.responses.append((304, None, None))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is True


def test_a_next_page_link_always_runs_the_full_poll(probe):
    _seen({123: "head-1"})
    nxt = '<https://api.github.com/repositories/1/pulls?page=2>; rel="next"'
    probe.responses.append((200, "v1", _list_page((123, "head-1")), nxt))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is True
    # A 304 that still names a next page proves nothing either.
    probe.responses.append((304, None, None, nxt))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is True
    # A single short page with no next link is trusted.
    probe.responses.append((200, "v2", _list_page((123, "head-1"))))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is False
    probe.responses.append((304, None, None))
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is False


def test_probe_without_a_recent_poll_or_connector(probe, monkeypatch):
    # No open-head snapshot (no recent successful poll): poll, without a request.
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is True
    assert probe.calls == []
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is False
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.delenv("UA_GITHUB_CONNECTOR_REPO")
    assert github_app_fastpath.open_heads_changed(run_gh=probe.gh) is False


def test_probe_failure_raises_for_the_planner(probe):
    _seen({123: "head-1"})
    probe.responses.append((401, None, None))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        github_app_fastpath.open_heads_changed(run_gh=probe.gh)

    def broken(_cmd, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="")

    with pytest.raises(RuntimeError, match="status line"):
        github_app_fastpath.open_heads_changed(run_gh=broken)


def test_brokered_probe_sends_if_none_match_in_process(probe, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_SOURCE", "connector_oauth")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "service-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"service-bearer": SERVICE})
    monkeypatch.setattr(github_app, "resolve_user_token", lambda: "synthetic-broker-token")
    _seen({123: "head-1"})
    responses = [
        httpx.Response(200, json=_list_page((123, "head-1")), headers={"ETag": 'W/"b1"'}),
        httpx.Response(304, headers={"ETag": 'W/"b1"'}),
    ]
    seen = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            seen.append((url, kwargs["params"], dict(kwargs["headers"])))
            return responses.pop(0)

    monkeypatch.setattr(github_app_fastpath.httpx, "Client", Client)
    no_cli = lambda *_a, **_k: pytest.fail("brokered token must stay in process")  # noqa: E731
    assert github_app_fastpath.open_heads_changed(run_gh=no_cli) is False
    assert github_app_fastpath.open_heads_changed(run_gh=no_cli) is False
    assert seen[0][0] == "https://api.github.com/repos/example/project/pulls"
    assert seen[0][1]["sort"] == "updated"
    assert "If-None-Match" not in seen[0][2]
    assert seen[1][2]["If-None-Match"] == 'W/"b1"'
    assert seen[1][2]["Authorization"] == "Bearer synthetic-broker-token"


def test_parse_included_handles_crlf_and_lf():
    status, etag, body, more = github_app_fastpath._parse_included(_included(200, "x", [1]))
    assert (status, etag, json.loads(body), more) == (200, 'W/"x"', [1], False)
    parsed = github_app_fastpath._parse_included("HTTP/1.1 304 Not Modified\n\n")
    assert parsed == (304, None, "", False)
    linked = _included(304, None, None, '<u2>; rel="next", <u9>; rel="last"')
    assert github_app_fastpath._parse_included(linked)[3] is True


# --- (b) the tick planner --------------------------------------------------------------


def _state():
    return {"journal_last_run_at": T0.isoformat(), "connector_last_run_at": T0.isoformat()}


def _never():
    pytest.fail("consulted when it was not needed")


def test_changed_heads_make_the_connector_due_at_the_active_interval():
    plan = pr_watch.plan_tick(
        _state(), T0 + timedelta(seconds=120), in_flight=lambda: False, changed=lambda: True
    )
    assert plan.connector and not plan.journal
    assert "heads changed" in plan.reason
    idle = pr_watch.plan_tick(
        _state(), T0 + timedelta(seconds=120), in_flight=lambda: False, changed=lambda: False
    )
    assert not idle.connector and not idle.journal


def test_probe_is_skipped_inside_the_active_interval_and_while_in_flight():
    plan = pr_watch.plan_tick(
        _state(), T0 + timedelta(seconds=30), in_flight=_never, changed=_never
    )
    assert not plan.connector
    plan = pr_watch.plan_tick(
        _state(), T0 + timedelta(seconds=120), in_flight=lambda: True, changed=_never
    )
    assert plan.connector and "in flight" in plan.reason
    plan = pr_watch.plan_tick(
        _state(), T0 + timedelta(seconds=1800), in_flight=_never, changed=_never
    )
    assert plan.connector and plan.journal


def test_probe_error_fails_toward_polling():
    def boom():
        raise RuntimeError("github unavailable")

    plan = pr_watch.plan_tick(
        _state(), T0 + timedelta(seconds=150), in_flight=lambda: False, changed=boom
    )
    assert plan.connector
