"""Task #2157: agent session transcripts are not source; triage markers are class-scoped.

The daily surfaces audit failed on ~/.codex with HIGH "possible JWK private
member ... committed in source" findings, all in Codex session rollouts and
memories that had read synthetic JWK test fixtures. The directory is not a
git repo. And no task was filed, because an old ``[accepted-risk]`` task for a
different class under the same root and rule (vendor auth stores) matched the
``<root>:<rule>`` marker.

Every key below is an obviously fake fixture assembled at runtime, so the
repo's own audit gate stays clean.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess

import pytest

from unified_agent.audit import engine, run_audit
from unified_agent.audit.grade import gated
from unified_agent.audit.surfaces import run_surface_scan
from unified_agent.audit.triage import (
    ACCEPTED_RISK_TAG,
    REPO_CLASS,
    file_findings_as_tasks,
    path_class,
    root_key,
)
from unified_agent.tools.tasks import task_add, task_list, task_update

TRANSCRIPT_MESSAGE = "in an agent session transcript, not source control"
# Task #2296: an artifact whose path and minted name place it in an agent-log
# store but whose content fails the closed schema is not a transcript (never
# LOW), and its fixture-only SECRETS-KEY findings are MEDIUM, not HIGH. The
# agent-log class itself is tested in test_audit_surfaces.py.
UNVERIFIED_LOG = "medium"
GEMINI_SESSION = ".gemini/tmp/demo/chats/session-2026-10-25T10-00-10ba09d7.json"
HERMES_SESSION = ".hermes/sessions/session_20261025_100000_10ba09d7.json"
HERMES_JSONL = ".hermes/sessions/20261025_100000_10ba09d7.jsonl"
SESSION_TIME = "2026-10-25T10:00:00.000Z"
NATIVE_ID = "00000000-0000-4000-8000-000000000001"
CODEX_NAME = f"rollout-2026-10-17T10-00-00-{NATIVE_ID}.jsonl"
R3_CODEX = f".codex/sessions/2026/10/25/rollout-2026-10-25T11-01-23-{NATIVE_ID}.jsonl"
R3_CLAUDE = f".claude/projects/demo/{NATIVE_ID}.jsonl"
R3_SUBAGENT = f".claude/projects/demo/{NATIVE_ID}/subagents/agent-settings.jsonl"


def _codex_records(text, stamp="2026-10-17T10:00:00Z"):
    return [
        {
            "timestamp": stamp,
            "type": "session_meta",
            "payload": {
                "id": NATIVE_ID,
                "session_id": NATIVE_ID,
                "timestamp": stamp,
                "cwd": "/workspace/demo",
                "originator": "codex_cli_rs",
                "cli_version": "0.164.0",
                "source": "cli",
                "model_provider": "openai",
                "history_mode": "legacy",
                "base_instructions": {"text": "normal system instructions"},
            },
        },
        {
            "timestamp": stamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    ]


def _claude_record(text):
    return {
        "type": "user",
        "sessionId": NATIVE_ID,
        "cwd": "/workspace/demo",
        "uuid": "10000000-0000-4000-8000-000000000001",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "version": "2.1.310",
        "gitBranch": "main",
        "timestamp": "2026-10-25T11:01:23Z",
        "message": {"role": "user", "content": text},
    }


def _jsonl(records):
    return "".join(json.dumps(record) + "\n" for record in records)


def _complete_message(harness, content="normal chat template"):
    if harness == "gemini":
        return {
            "id": "10000000-0000-4000-8000-000000000001",
            "timestamp": SESSION_TIME,
            "type": "user",
            "content": content,
            "displayContent": "normal display",
        }
    return {
        "role": "user",
        "content": content,
        "timestamp": 1792922400.0,
        "tool_name": None,
        "tool_calls": None,
        "tool_call_id": None,
        "effect_disposition": None,
        "token_count": 3,
        "finish_reason": None,
        "reasoning": None,
        "reasoning_content": None,
        "reasoning_details": None,
        "codex_reasoning_items": None,
        "codex_message_items": None,
        "_compressed_summary": False,
        "api_content": None,
        "display_kind": None,
        "display_metadata": None,
        "platform_message_id": "fixture-1",
        "observed": False,
    }


def _complete_envelope(harness, records):
    if harness == "gemini":
        return {
            "sessionId": "10ba09d7-cdc5-4b66-ab08-0eef1b8fc819",
            "projectHash": "b" * 64,
            "startTime": SESSION_TIME,
            "lastUpdated": SESSION_TIME,
            "messages": records,
            "summary": "normal history summary",
            "kind": "main",
            "directories": ["/workspace/demo"],
            "memoryScratchpad": {
                "version": 1,
                "workflowSummary": "normal workflow",
                "toolSequence": ["fixture"],
                "touchedPaths": ["src/fixture.py"],
                "validationStatus": "passed",
            },
        }
    return {
        "session_id": "20261025_100000_10ba09d7",
        "model": "fixture-model",
        "base_url": "https://example.test/v1",
        "platform": "cli",
        "session_start": "2026-10-25T10:00:00",
        "last_updated": "2026-10-25T10:01:00",
        "system_prompt": "normal system context",
        "tools": ["terminal"],
        "message_count": len(records),
        "messages": records,
    }


def _complete_text(harness, records):
    if harness == "hermes_jsonl":
        return "".join(json.dumps(record) + "\n" for record in records)
    return json.dumps(_complete_envelope(harness, records)) + "\n"


def _fake_jwk() -> dict:
    """A symmetric JWK whose private member is plainly not a key."""
    kty, member = "kty", "k"
    return {kty: "oct", member: "FAKE-FIXTURE-NOT-A-KEY-" + "0" * 24, "kid": "fixture"}


def _jsonl_record(harness="codex") -> str:
    """Actual supported native record envelopes with the fixture in a payload."""
    text = json.dumps(_fake_jwk(), indent=2)
    return _jsonl([_claude_record(text)] if harness == "claude" else _codex_records(text))


def _plain_json() -> str:
    return json.dumps(_fake_jwk(), indent=2) + "\n"


def _session_json(harness: str) -> str:
    message = {"content": json.dumps(_fake_jwk())}
    message["type" if harness == "gemini" else "role"] = "user"
    return _harness_records(harness, [message])


def _vendor_key() -> str:
    return "sk-" + "ant-" + "Fx9q" * 8


def _write(root, rel, text, mode=0o600):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _secrets(report):
    return [fd for fd in report["findings"] if fd["rule"] == "SECRETS-KEY"]


# Cached plugin code: an LLM import, a key and an eval (task #2316).
_CACHED_PLUGIN = f'import anthropic\nKEY = "{_vendor_key()}"\nx = eval(r.text)\n'


@pytest.fixture
def reach_within_tmp(monkeypatch, tmp_path):
    """Bound the store reachability walk below tmp_path (pytest's temp root is
    owner-only, which would hide every readable store)."""
    real = engine._ancestor_dirs

    def _within_tmp(path):
        return [d for d in real(path) if d != tmp_path and d.is_relative_to(tmp_path)]

    monkeypatch.setattr(engine, "_ancestor_dirs", _within_tmp)


class TestTranscriptsAreNotSource:
    @pytest.mark.parametrize(
        "rel",
        [
            f"sessions/2026/10/17/{CODEX_NAME}",
            f"archived_sessions/{CODEX_NAME}",
            "memories/raw_memories.md",
            "memories/rollout_summaries/jwk_fixture_summary.md",
        ],
    )
    def test_jwk_member_in_a_codex_transcript_is_low_not_high(self, tmp_path, rel):
        root = tmp_path / ".codex"
        text = _jsonl_record() if rel.endswith(".jsonl") else _plain_json()
        _write(root, rel, text)

        report = run_audit(root)

        found = _secrets(report)
        assert found, "the finding is downgraded, not dropped"
        assert {fd["severity"] for fd in found} == {"low"}
        for fd in found:
            assert fd["path"] == rel
            assert TRANSCRIPT_MESSAGE in fd["message"]
            assert "committed in source" not in fd["message"]
            assert "purge it from git history" not in fd["fix"]
            assert "no git history to purge" in fd["fix"]
        assert report["summary"]["high"] == 0
        assert not gated(report["findings"], "high")

    @pytest.mark.parametrize(
        "rel",
        [
            f".codex/sessions/2026/10/17/{CODEX_NAME}",
            ".claude/projects/-work-proj/00000000-0000-4000-8000-000000000001.jsonl",
            ".claude/projects/-work-proj/memory/MEMORY.md",
            ".gemini/tmp/0abc/chats/session-2026-10-25T10-00-10ba09d7.json",
            HERMES_SESSION,
            ".hermes/memories/MEMORY.md",
        ],
    )
    def test_other_harness_transcripts_from_a_parent_root(self, tmp_path, rel):
        root = tmp_path / "home"
        text = (
            _jsonl_record("claude" if ".claude/" in rel else "codex")
            if rel.endswith(".jsonl")
            else _plain_json()
        )
        if rel.endswith(".json"):
            text = _session_json("gemini" if ".gemini/" in rel else "hermes")
        _write(root, rel, text)

        found = _secrets(run_audit(root))

        assert found
        assert {fd["severity"] for fd in found} == {"low"}

    @pytest.mark.parametrize(
        "rel",
        [
            "src/keys.json",
            "fixtures/session.jsonl",
            "notes.md",
        ],
    )
    def test_same_content_in_an_ordinary_file_stays_high(self, tmp_path, rel):
        root = tmp_path / "proj"
        _write(root, rel, _jsonl_record() if rel.endswith(".jsonl") else _plain_json())

        found = _secrets(run_audit(root))

        assert found
        assert {fd["severity"] for fd in found} == {"high"}
        assert all("committed in source" in fd["message"] for fd in found)

    @pytest.mark.parametrize(
        "rel",
        [
            # Not a transcript file type, though inside a session store.
            "sessions/2026/10/17/helper.py",
            "memories/notes.toml",
            # Next to a store, not inside one.
            "notes.json",
            "sessions.json",
        ],
    )
    def test_transcript_class_is_narrow_inside_codex(self, tmp_path, rel):
        root = tmp_path / ".codex"
        _write(root, rel, _plain_json())

        found = _secrets(run_audit(root))

        assert found
        assert {fd["severity"] for fd in found} == {"high"}

    def test_claude_sessions_dir_is_not_a_transcript_store(self, tmp_path):
        """`.claude/sessions` holds runtime state and key files, not transcripts."""
        root = tmp_path / ".claude"
        _write(root, "sessions/4242.json", _plain_json())

        assert {fd["severity"] for fd in _secrets(run_audit(root))} == {"high"}

    def test_tracked_file_in_a_transcript_path_is_source(self, tmp_path):
        root = tmp_path / ".hermes"
        rel = "sessions/session_20261025_100000_10ba09d7.json"
        _write(root, rel, _session_json("hermes"))
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["add", rel],
            ["commit", "-qm", "init"],
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

        found = _secrets(run_audit(root))

        assert {fd["severity"] for fd in found} == {"high"}
        assert all("committed in source" in fd["message"] for fd in found)

    def test_untracked_transcript_inside_a_dot_dir_repo_is_a_transcript(self, tmp_path):
        root = tmp_path / ".hermes"
        _write(root, "sessions/session_20261025_100000_10ba09d7.json", _session_json("hermes"))
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)

        assert {fd["severity"] for fd in _secrets(run_audit(root))} == {"low"}

    def test_vendor_shaped_key_in_a_transcript_stays_high_with_an_accurate_message(self, tmp_path):
        """Only the generic JWK heuristic is downgraded: a live vendor key
        echoed into a transcript is still a key on disk."""
        root = tmp_path / ".codex"
        record = _jsonl(_codex_records(f"export KEY={_vendor_key()}"))
        _write(root, f"sessions/2026/10/17/{CODEX_NAME}", record)

        found = _secrets(run_audit(root))

        assert [fd["severity"] for fd in found] == ["high"]
        assert TRANSCRIPT_MESSAGE in found[0]["message"]
        assert "rotate it" in found[0]["fix"]

    def test_transcript_check_runs_only_for_a_secrets_finding(self, tmp_path, monkeypatch):
        """The git lookup behind the classification is lazy: a transcript
        tree without a SECRETS-KEY finding never pays for it."""
        root = tmp_path / ".codex"
        _write(root, f"sessions/2026/10/17/{CODEX_NAME}", _jsonl(_codex_records("normal chat")))

        def refused(*args, **kwargs):
            pytest.fail("classified a file that had no SECRETS-KEY finding")

        monkeypatch.setattr(engine, "_agent_log_class", refused)
        run_audit(root)


class TestTranscriptRecognition2221:
    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize(
        "rel",
        [
            ".claude/projects/demo/settings.json",
            ".claude/projects/demo/config.md",
            ".claude/projects/demo/session.json",
            ".claude/projects/demo/settings.jsonl",
            ".claude/projects/demo/config.jsonl",
            ".claude/projects/demo/session.jsonl",
            ".claude/projects/demo/nested/session.jsonl",
            ".claude/projects/demo/memory/settings.json",
            ".codex/sessions/2026/10/17/settings.json",
            ".codex/sessions/2026/10/17/config.md",
            ".codex/sessions/2026/10/17/session.jsonl",
            ".codex/sessions/rollout-misplaced.jsonl",
            ".codex/archived_sessions/settings.json",
            ".codex/archived_sessions/config.md",
            ".codex/archived_sessions/session.jsonl",
            ".codex/memories/settings.json",
            ".codex/memories/config.md",
            ".codex/memories/rollout_summaries/settings.json",
            ".gemini/tmp/demo/chats/settings.json",
            ".gemini/tmp/demo/chats/config.md",
            ".gemini/tmp/demo/chats/session-config.json",
            ".gemini/tmp/demo/chats/nested/session-fixture.json",
            ".hermes/sessions/settings.json",
            ".hermes/sessions/config.md",
            ".hermes/sessions/session_config.json",
            ".hermes/sessions/settings.jsonl",
            ".hermes/sessions/nested/session_fixture.json",
            ".hermes/memories/settings.json",
            ".hermes/memories/config.md",
        ],
    )
    def test_configuration_in_a_store_stays_high_with_the_same_jwk(self, tmp_path, rel, repo):
        root = tmp_path / "home"
        # Identical bytes: the recognized Claude JSONL path is the only difference.
        text = _jsonl_record("claude")
        _write(root, rel, text)
        transcript = ".claude/projects/demo/00000000-0000-4000-8000-000000000001.jsonl"
        _write(root, transcript, text)
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)

        found = _secrets(run_audit(root))

        assert {fd["severity"] for fd in found if fd["path"] == rel} == {"high"}
        assert {fd["severity"] for fd in found if fd["path"] == transcript} == {"low"}

    @pytest.mark.parametrize(
        "rel",
        [
            ".codex/memories/MEMORY.md",
            ".codex/memories/memory_summary.md",
            ".claude/projects/demo/memory/topic.md",
            ".claude/projects/demo/00000000-0000-4000-8000-000000000001/subagents/agent-abc123.jsonl",
            ".hermes/memories/USER.md",
        ],
    )
    def test_known_memory_and_subagent_artifacts_remain_low(self, tmp_path, rel):
        _write(tmp_path, rel, _jsonl_record("claude" if rel.endswith(".jsonl") else "codex"))

        assert {fd["severity"] for fd in _secrets(run_audit(tmp_path))} == {"low"}

    def test_hermes_message_jsonl_remains_low(self, tmp_path):
        record = {"role": "tool", "content": json.dumps(_fake_jwk())}
        _write(tmp_path, HERMES_JSONL, json.dumps(record) + "\n")

        assert {fd["severity"] for fd in _secrets(run_audit(tmp_path))} == {"low"}

    @pytest.mark.parametrize("harness", ["gemini", "hermes"])
    @pytest.mark.parametrize("content", ["jwk", "empty", "nonmessages", "malformed"])
    def test_session_shaped_json_requires_messages(self, tmp_path, harness, content):
        rel = GEMINI_SESSION if harness == "gemini" else HERMES_SESSION
        text = {
            "jwk": _plain_json(),
            "empty": json.dumps({"messages": [], "config": _fake_jwk()}),
            "nonmessages": json.dumps({"messages": [_fake_jwk()]}),
            "malformed": _session_json(harness) + "{",
        }[content]
        _write(tmp_path, rel, text)

        assert {fd["severity"] for fd in _secrets(run_audit(tmp_path))} == {UNVERIFIED_LOG}


class TestReviewR1Regressions2221:
    """The reviewer's 18 mismatches, plus the two multipart cases in untracked Git."""

    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize(
        "case",
        [
            "gemini_numeric",
            "gemini_object",
            "gemini_unknown_name",
            "hermes_numeric",
            "hermes_object",
            "hermes_unknown_name",
            "hermes_unknown_jsonl",
            "hermes_false_calls",
            "gemini_bad_parts",
            "hermes_bad_parts",
        ],
    )
    def test_message_templates_and_malformed_records_are_not_transcripts(
        self, tmp_path, repo, case
    ):
        root = tmp_path / "home"
        gemini = case.startswith("gemini")
        jsonl = case in {"hermes_unknown_jsonl", "hermes_false_calls"}
        known = GEMINI_SESSION if gemini else HERMES_JSONL if jsonl else HERMES_SESSION
        rel = known
        jwk = _fake_jwk()
        field = "type" if gemini else "role"
        message = {field: "user", "content": json.dumps(jwk)}

        def encode(record):
            harness = "gemini" if gemini else "hermes_jsonl" if jsonl else "hermes"
            return _harness_records(harness, [record])

        control = encode(message)
        bad = dict(message)
        if case.endswith("_numeric"):
            bad.update(content=17, config=jwk)
        elif case.endswith("_object"):
            bad["content"] = {"config": jwk}
        elif case.endswith("_bad_parts"):
            bad["content"] = [17, {"config": jwk}]
        elif case == "hermes_false_calls":
            bad = {"role": "user", "tool_calls": False, "config": jwk}
        else:
            filename = (
                "session-config.json"
                if gemini
                else "settings.jsonl"
                if jsonl
                else "session_config.json"
            )
            rel = known.rsplit("/", 1)[0] + "/" + filename
        text = encode(bad)
        if "unknown" in case:
            assert text == control, "name controls must use identical bytes"
        _write(root, "case/" + rel, text)
        _write(root, "control/" + known, control)
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)

        found = _secrets(run_audit(root, include_repo_rules=False))

        assert {fd["severity"] for fd in found if fd["path"] == "case/" + rel} == {
            "high" if "unknown" in case else UNVERIFIED_LOG
        }
        assert {fd["severity"] for fd in found if fd["path"] == "control/" + known} == {"low"}


def _harness_records(harness, records):
    if harness == "gemini":
        records = [
            {"id": f"10000000-0000-4000-8000-{index + 1:012x}", "timestamp": SESSION_TIME, **record}
            if isinstance(record, dict)
            else record
            for index, record in enumerate(records)
        ]
    if harness == "hermes_jsonl":
        return "".join(json.dumps(record) + "\n" for record in records)
    return json.dumps(_complete_envelope(harness, records)) + "\n"


def _harness_rel(harness):
    return {"gemini": GEMINI_SESSION, "hermes": HERMES_SESSION, "hermes_jsonl": HERMES_JSONL}[
        harness
    ]


class TestReviewR1SupportedSchemas2221:
    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize("harness", ["gemini", "hermes", "hermes_jsonl"])
    def test_supported_strings_parts_and_assistant_calls(self, tmp_path, repo, harness):
        root = tmp_path / "home"
        root.mkdir()
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        jwk = _fake_jwk()
        text = json.dumps(jwk)
        field = "type" if harness == "gemini" else "role"
        if harness == "gemini":
            contents = [
                text,
                {"text": text},
                ["prefix", {"text": text}],
                {"text": text, "thought": True, "thoughtSignature": "fixture"},
                {"functionCall": {"name": "fixture", "args": jwk}},
                {"functionResponse": {"name": "fixture", "response": jwk}},
                [{"text": text}, {"inlineData": {"mimeType": "image/png", "data": "AA=="}}],
                [{"text": text}, {"fileData": {"fileUri": "gs://example/fixture"}}],
                [{"text": text}, {"executableCode": {"language": "PYTHON", "code": "pass"}}],
                [{"text": text}, {"codeExecutionResult": {"outcome": "OUTCOME_OK"}}],
            ]
        else:
            contents = [
                text,
                ["prefix", {"type": "text", "text": text}],
                [{"type": "input_text", "text": text}],
                [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}],
                [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": "https://example.test/image"}},
                ],
                [
                    {"type": "text", "text": text},
                    {"type": "input_image", "image_url": "https://example.test/image"},
                ],
                [
                    {"type": "text", "text": text},
                    {"type": "image", "source": {"type": "url", "url": "https://example.test/i"}},
                ],
                [
                    {"type": "text", "text": text},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "AA==",
                        },
                    },
                ],
            ]
        records = [{field: "user", "content": content} for content in contents]
        if harness != "gemini":
            calls = [
                {
                    "id": "call_fixture",
                    "type": "function",
                    "function": {
                        "name": "fixture",
                        "arguments": text,
                    },
                }
            ]
            records.extend(
                {"role": "assistant", "content": value, "tool_calls": calls}
                for value in (None, "", [])
            )
            records.append({"role": "assistant", "tool_calls": calls})
        else:
            records.append(
                {
                    "type": "gemini",
                    "content": "",
                    "toolCalls": [
                        {
                            "id": "call_fixture",
                            "name": "fixture",
                            "args": jwk,
                            "status": "success",
                            "timestamp": "2026-10-25T10:00:00Z",
                            "result": [{"text": text}],
                        }
                    ],
                }
            )
        for index, record in enumerate(records):
            rel = f"variant-{index}/" + _harness_rel(harness)
            _write(root, rel, _harness_records(harness, [record]))
            found = _secrets(run_audit(root / f"variant-{index}", include_repo_rules=False))
            assert {fd["severity"] for fd in found} == {"low"}, index

    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize("harness", ["gemini", "hermes", "hermes_jsonl"])
    def test_every_record_and_block_must_have_a_supported_shape(self, tmp_path, repo, harness):
        root = tmp_path / "home"
        root.mkdir()
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        field = "type" if harness == "gemini" else "role"
        good = {field: "user", "content": json.dumps(_fake_jwk())}
        contents = [
            17,
            True,
            False,
            None,
            {},
            {"config": _fake_jwk()},
            [17],
            [True],
            [None],
            [{}],
            [["nested"]],
            ["good", 17],
            [{"text": 17}],
        ]
        if harness == "gemini":
            contents.extend(
                [
                    {"text": "good", "config": _fake_jwk()},
                    {"functionCall": {"name": "fixture", "args": False}},
                    {"functionResponse": {"name": "fixture", "response": []}},
                    {"inlineData": {"mimeType": "image/png", "data": False}},
                    {"text": "good", "thought": 1},
                    {"text": "good", "thoughtSignature": {}},
                    {"executableCode": {"language": "config", "code": "pass"}},
                    {"codeExecutionResult": {"outcome": "config"}},
                ]
            )
        else:
            contents.extend(
                [
                    [{"type": "text", "text": True}],
                    [{"type": "text", "text": "good", "config": _fake_jwk()}],
                    [{"type": "image_url", "image_url": 17}],
                    [{"type": "image_url", "image_url": {"url": False}}],
                    [{"type": "text", "text": "good", "cache_control": False}],
                ]
            )
        malformed = [{field: "user", "content": value} for value in contents]
        malformed.extend([{field: [], "content": "good"}, {field: "user"}, False])
        call_field = "toolCalls" if harness == "gemini" else "tool_calls"
        for calls in (False, 17, {}, [17], [{}]):
            malformed.append(
                {
                    field: "gemini" if harness == "gemini" else "assistant",
                    "content": "good",
                    call_field: calls,
                }
            )
        if harness != "gemini":
            for function in (
                {"name": 17, "arguments": "{}"},
                {"name": "fixture", "arguments": False},
                {"name": "fixture", "arguments": {}},
            ):
                malformed.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_fixture", "type": "function", "function": function},
                        ],
                    }
                )
        for index, bad in enumerate(malformed):
            for records in ([good, bad], [bad, good]):
                _write(root, _harness_rel(harness), _harness_records(harness, records))
                found = _secrets(run_audit(root, include_repo_rules=False))
                assert {fd["severity"] for fd in found} == {UNVERIFIED_LOG}, index

    @pytest.mark.parametrize("harness", ["gemini", "hermes", "hermes_jsonl"])
    @pytest.mark.parametrize("git_state", ["tracked", "corrupt_index"])
    def test_recognized_valid_records_stay_high_in_git(self, tmp_path, harness, git_state):
        rel = _harness_rel(harness)
        field = "type" if harness == "gemini" else "role"
        _write(
            tmp_path,
            rel,
            _harness_records(
                harness,
                [
                    {field: "user", "content": json.dumps(_fake_jwk())},
                ],
            ),
        )
        _git_init_commit(tmp_path, rel)
        if git_state == "corrupt_index":
            _corrupt_index(tmp_path)
        assert {fd["severity"] for fd in _secrets(run_audit(tmp_path))} == {"high"}

    def test_only_minted_hermes_ids_and_gemini_names_qualify(self, tmp_path):
        ids = [
            "20261025_100000_abcdef",
            "20261025_100000_abcdef12",
            "20261025_100000_abcdef123456",
            "cron_abcdef123456_20261025_100000",
            "bg_100000_abcdef",
            "bg_abcdef",
            "room_" + "a" * 32,
            "api_1792922400_abcdef12",
            "api-" + "a" * 16,
            "run_" + "a" * 32,
            "00000000-0000-4000-8000-000000000001",
        ]
        text = _session_json("hermes")
        jsonl = json.dumps({"role": "user", "content": json.dumps(_fake_jwk())}) + "\n"
        for sid in ids:
            for name, data in ((f"session_{sid}.json", text), (f"{sid}.jsonl", jsonl)):
                path = _write(tmp_path, ".hermes/sessions/" + name, data)
                assert {fd["severity"] for fd in _secrets(run_audit(path))} == {"low"}
        for sid in (
            "20261025_fixture",
            "20261025_100000_fixture",
            "20261025_100000_abcdef_bad",
            "20269999_100000_abcdef",
            "20261025_990000_abcdef",
            "bg_990000_abcdef",
            "cron_job-1_20261025_100000",
            "api_123_fake",
            "config",
        ):
            for name, data in ((f"session_{sid}.json", text), (f"{sid}.jsonl", jsonl)):
                path = _write(tmp_path, ".hermes/sessions/" + name, data)
                assert {fd["severity"] for fd in _secrets(run_audit(path))} == {"high"}
        for name in (
            "session-2026-99-25T10-00-10ba09d7.json",
            "session-2026-10-25T99-00-10ba09d7.json",
            "session-2026-10-25T10-00-config.json",
            "session-2026-10-25T10-00-1-10ba09d7.json",
        ):
            path = _write(tmp_path, ".gemini/tmp/demo/chats/" + name, _session_json("gemini"))
            assert {fd["severity"] for fd in _secrets(run_audit(path))} == {"high"}


class TestReviewR2Regressions2221:
    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize(
        "case",
        [
            *[
                (h, field)
                for h in ("gemini", "hermes", "hermes_jsonl")
                for field in (
                    "record_config",
                    "later_record_config",
                    "record_id",
                    "record_timestamp",
                )
            ],
            *[
                (h, field)
                for h in ("gemini", "hermes")
                for field in ("envelope_config", "envelope_sessionId")
            ],
            *[
                ("gemini", "followup_" + field)
                for field in (
                    "envelope_config",
                    "record_config",
                    "record_id",
                    "record_timestamp",
                    "envelope_sessionId",
                )
            ],
        ],
    )
    def test_complete_artifact_rejects_unrelated_configuration(self, tmp_path, repo, case):
        harness, mutation = case
        mutation = mutation.removeprefix("followup_")
        root = tmp_path / "home"
        jwk = _fake_jwk()
        control = _complete_text(harness, [_complete_message(harness, json.dumps(jwk))])
        records = [_complete_message(harness)]
        if mutation == "later_record_config":
            records.append(_complete_message(harness))
            records[-1]["config"] = jwk
        elif mutation.startswith("record_"):
            records[0][mutation.removeprefix("record_")] = jwk
        if harness == "hermes_jsonl":
            text = "".join(json.dumps(record) + "\n" for record in records)
        else:
            envelope = _complete_envelope(harness, records)
            if mutation.startswith("envelope_"):
                envelope[mutation.removeprefix("envelope_")] = jwk
            text = json.dumps(envelope) + "\n"
        rel = _harness_rel(harness)
        _write(root, "case/" + rel, text)
        _write(root, "control/" + rel, control)
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        found = _secrets(run_audit(root, include_repo_rules=False))
        assert {fd["severity"] for fd in found if fd["path"] == "case/" + rel} == {UNVERIFIED_LOG}
        assert {fd["severity"] for fd in found if fd["path"] == "control/" + rel} == {"low"}


def _rich_metadata(harness, text):
    record = _complete_message(harness, text)
    if harness == "gemini":
        record.update(
            type="gemini",
            model="gemini-fixture",
            tokens={"input": 10, "output": 2, "cached": 0, "total": 12, "thoughts": 0, "tool": 0},
            thoughts=[
                {
                    "subject": "normal thought",
                    "description": "normal description",
                    "timestamp": SESSION_TIME,
                }
            ],
            toolCalls=[
                {
                    "id": "call_fixture",
                    "name": "fixture",
                    "args": {"query": text},
                    "result": [{"text": "normal result"}],
                    "status": "success",
                    "timestamp": SESSION_TIME,
                    "agentId": "agent_fixture",
                    "displayName": "Fixture",
                    "description": "normal tool description",
                    "resultDisplay": "normal result display",
                    "renderOutputAsMarkdown": True,
                }
            ],
        )
    else:
        record.update(
            role="assistant",
            reasoning="normal reasoning",
            reasoning_content="normal thought",
            reasoning_details=[
                {
                    "type": "reasoning.summary",
                    "summary": "normal summary",
                    "signature": None,
                    "id": "rd_fixture",
                    "format": "openai",
                    "index": 0,
                }
            ],
            message_uid="a" * 32,
            _tool_call_uid="b" * 32,
            _absorbed_message_uids=["c" * 32],
            _tool_call_uids={"call_fixture": "b" * 32},
            _row_id=1,
            tool_name="fixture",
            tool_call_id="call_fixture",
            effect_disposition="observed",
            finish_reason="stop",
            display_kind="normal",
            api_content="normal original history",
            name="fixture",
            message_id="fixture-1",
            display_metadata={
                "display_text": "normal display",
                "title_preview": "normal title",
                "error": "normal history",
                "error_surface": "tool",
                "gateway_input_owner": "fixture_owner",
                "reply_expected": True,
                "notification_category": "result",
                "data_use": ["fixture"],
                "compaction_inflight_task_restatement": False,
            },
        )
    return record


class TestReviewR2ClosedSchemas2221:
    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize("harness", ["gemini", "hermes", "hermes_jsonl"])
    def test_declared_metadata_types_and_unknown_members_are_closed(self, tmp_path, repo, harness):
        root = tmp_path / "home"
        root.mkdir()
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        jwk = _fake_jwk()
        record = _rich_metadata(harness, json.dumps(jwk))
        artifact = [record] if harness == "hermes_jsonl" else _complete_envelope(harness, [record])
        record_path = (0,) if harness == "hermes_jsonl" else ("messages", 0)

        def at(value, path):
            for key in path:
                value = value[key]
            return value

        def encode(value):
            return (
                "".join(json.dumps(row) + "\n" for row in value)
                if harness == "hermes_jsonl"
                else json.dumps(value)
            )

        path = _write(root, _harness_rel(harness), encode(artifact))
        assert {fd["severity"] for fd in _secrets(run_audit(path))} == {"low"}
        objects = [record_path]
        if harness != "hermes_jsonl":
            objects.append(())
        if harness == "gemini":
            objects += [
                ("memoryScratchpad",),
                record_path + ("tokens",),
                record_path + ("thoughts", 0),
                record_path + ("toolCalls", 0),
            ]
        else:
            objects += [
                record_path + ("display_metadata",),
                record_path + ("reasoning_details", 0),
                record_path + ("_tool_call_uids",),
            ]
        # These slots intentionally hold structured transcript payload, not metadata.
        payloads = {"content", "displayContent", "args", "result"}
        for object_path in objects:
            for key in ["config", *(k for k in at(artifact, object_path) if k not in payloads)]:
                changed = copy.deepcopy(artifact)
                at(changed, object_path)[key] = jwk
                path.write_text(encode(changed))
                found = _secrets(run_audit(path))
                assert {fd["severity"] for fd in found} == {UNVERIFIED_LOG}, (object_path, key)
        # A valid later record cannot hide the bad earlier one, or vice versa.
        for records in ([record, dict(record, config=jwk)], [dict(record, config=jwk), record]):
            path.write_text(_complete_text(harness, records))
            assert {fd["severity"] for fd in _secrets(run_audit(path))} == {UNVERIFIED_LOG}

    @pytest.mark.parametrize("harness", ["gemini", "hermes", "hermes_jsonl"])
    def test_booleans_and_nonfinite_numbers_do_not_grant_classification(self, tmp_path, harness):
        record = _rich_metadata(harness, json.dumps(_fake_jwk()))
        for number in (True, False, float("nan"), float("inf"), -float("inf"), -1):
            changed = copy.deepcopy(record)
            if harness == "gemini":
                changed["tokens"]["input"] = number
            else:
                changed["timestamp"] = number
            path = _write(tmp_path, _harness_rel(harness), _complete_text(harness, [changed]))
            assert {fd["severity"] for fd in _secrets(run_audit(path))} == {UNVERIFIED_LOG}
        for field in ("timestamp", "id") if harness == "gemini" else ("timestamp", "token_count"):
            changed = copy.deepcopy(record)
            changed[field] = "not-a-contract-value"
            path.write_text(_complete_text(harness, [changed]))
            assert {fd["severity"] for fd in _secrets(run_audit(path))} == {UNVERIFIED_LOG}
        text = _complete_text(harness, [record])
        text = (
            text.replace('"input": 10', '"input": 1e999')
            if harness == "gemini"
            else (text.replace('"timestamp": 1792922400.0', '"timestamp": 1e999'))
        )
        path.write_text(text)
        assert {fd["severity"] for fd in _secrets(run_audit(path))} == {UNVERIFIED_LOG}

    @pytest.mark.parametrize("harness", ["gemini", "hermes", "hermes_jsonl"])
    def test_duplicate_structural_members_are_ambiguous(self, tmp_path, harness):
        record = _rich_metadata(harness, json.dumps(_fake_jwk()))
        text = _complete_text(harness, [record])
        duplicate = '"timestamp":' + json.dumps(_fake_jwk()) + ',"timestamp":'
        path = _write(tmp_path, _harness_rel(harness), text.replace('"timestamp":', duplicate, 1))
        assert {fd["severity"] for fd in _secrets(run_audit(path))} == {UNVERIFIED_LOG}

    def test_structured_payload_slots_remain_payloads(self, tmp_path):
        jwk = _fake_jwk()
        record = _rich_metadata(
            "gemini", {"functionResponse": {"name": "fixture", "response": jwk}}
        )
        record["toolCalls"][0]["args"] = {"config": jwk}
        record["toolCalls"][0]["result"] = {
            "functionResponse": {"name": "fixture", "response": jwk}
        }
        path = _write(tmp_path, GEMINI_SESSION, _complete_text("gemini", [record]))
        assert {fd["severity"] for fd in _secrets(run_audit(path))} == {"low"}
        # The same structured value in metadata is rejected.
        record["tokens"] = jwk
        path.write_text(_complete_text("gemini", [record]))
        assert {fd["severity"] for fd in _secrets(run_audit(path))} == {UNVERIFIED_LOG}


class TestReviewR3Regressions2221:
    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize(
        "where,variant",
        [
            (where, variant)
            for where in ("codex_unknown", "codex_archived_unknown", "codex", "claude", "subagent")
            for variant in ("bare", "invalid_tail", "valid_chat", "later_config", "duplicate")
            if variant != "valid_chat" or where.endswith("unknown")
        ],
    )
    def test_unvalidated_native_jsonl_is_not_a_transcript(self, tmp_path, repo, where, variant):
        paths = {
            "codex_unknown": ".codex/sessions/2026/10/25/rollout-settings.jsonl",
            "codex_archived_unknown": ".codex/archived_sessions/rollout-config.jsonl",
            "codex": R3_CODEX,
            "claude": R3_CLAUDE,
            "subagent": R3_SUBAGENT,
        }
        jwk = _fake_jwk()
        secret = json.dumps(jwk)
        records = (
            _codex_records(secret, "2026-10-25T11:01:23Z")
            if where.startswith("codex")
            else [_claude_record(secret)]
        )
        text = {
            "bare": secret + "\n",
            "invalid_tail": secret + "\n{",
            "valid_chat": _jsonl(records),
            "later_config": _jsonl([*records, {"config": jwk}]),
            "duplicate": json.dumps(jwk)[:-1] + ', "k": "public"}\n',
        }[variant]
        root = tmp_path / "home"
        _write(root, paths[where], text)
        # Ordinary config uses identical bytes and must have the same severity.
        _write(root, "settings.json", text)
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        found = _secrets(run_audit(root, include_repo_rules=False))
        assert {fd["severity"] for fd in found if fd["path"] == "settings.json"} == {"high"}
        assert {fd["severity"] for fd in found if fd["path"] == paths[where]} == {
            "high" if where.endswith("unknown") else UNVERIFIED_LOG
        }


def _rich_native(harness, text=None):
    text = json.dumps(_fake_jwk()) if text is None else text
    if harness in ("codex", "archive"):
        rows = _codex_records(text, "2026-10-25T11:01:23Z")
        rows[0]["payload"].update(
            git={
                "commit_hash": "a" * 40,
                "branch": "main",
                "repository_url": "https://example.test/repo",
            },
            runtime_workspace_roots=["/workspace/demo"],
            context_window=None,
            multi_agent_version=None,
        )
        for kind, payload in [
            (
                "response_item",
                {
                    "type": "message",
                    "id": "msg_fixture",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
                    ],
                },
            ),
            (
                "response_item",
                {
                    "type": "function_call",
                    "id": "fc_fixture",
                    "call_id": "call_fixture",
                    "name": "fixture",
                    "arguments": text,
                    "status": "completed",
                },
            ),
            (
                "response_item",
                {"type": "function_call_output", "call_id": "call_fixture", "output": text},
            ),
            (
                "event_msg",
                {
                    "type": "user_message",
                    "message": text,
                    "images": None,
                    "local_images": [],
                    "text_elements": [],
                },
            ),
            (
                "event_msg",
                {
                    "type": "agent_message",
                    "message": text,
                    "phase": "final_answer",
                    "memory_citation": None,
                    "delivery": None,
                    "questions": None,
                },
            ),
        ]:
            rows.append({"timestamp": "2026-10-25T11:01:24Z", "type": kind, "payload": payload})
        usage = {
            "input_tokens": 1,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 2,
            "reasoning_output_tokens": 0,
            "total_tokens": 3,
        }
        rows.append(
            {
                "timestamp": "2026-10-25T11:01:25Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": usage,
                        "last_token_usage": dict(usage),
                        "model_context_window": 64000,
                    },
                    "rate_limits": None,
                },
            }
        )
        for index, row in enumerate(rows):
            row["ordinal"] = index
        return rows
    rows = [_claude_record(text)]
    rows[0]["isSidechain"] = harness == "subagent"
    assistant = copy.deepcopy(rows[0])
    assistant.update(
        type="assistant",
        uuid="20000000-0000-4000-8000-000000000001",
        parentUuid=rows[0]["uuid"],
        requestId="req_fixture",
    )
    assistant["message"] = {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": "claude-fixture",
        "content": [
            {"type": "text", "text": text, "citations": None},
            {"type": "thinking", "thinking": "normal thought", "signature": "fixture"},
            {
                "type": "tool_use",
                "id": "toolu_fixture",
                "name": "fixture",
                "input": {"config": _fake_jwk()},
                "caller": {"type": "direct"},
            },
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "container": None,
        "stop_details": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            "server_tool_use": {"web_search_requests": 0},
            "service_tier": "standard",
            "inference_geo": "us",
            "output_tokens_details": None,
        },
    }
    rows.append(assistant)
    result = copy.deepcopy(rows[0])
    result.update(
        uuid="30000000-0000-4000-8000-000000000001",
        parentUuid=assistant["uuid"],
        sourceToolAssistantUUID=assistant["uuid"],
    )
    result["message"]["content"] = [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_fixture",
            "is_error": False,
            "content": [{"type": "text", "text": text}],
        }
    ]
    rows.append(result)
    return rows


def _native_rel(harness):
    return {
        "codex": R3_CODEX,
        "archive": ".codex/archived_sessions/" + R3_CODEX.rsplit("/", 1)[1],
        "claude": R3_CLAUDE,
        "subagent": R3_SUBAGENT,
    }[harness]


class TestReviewR3SupportedNative2221:
    @pytest.mark.parametrize("harness", ["codex", "archive", "claude", "subagent"])
    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    def test_complete_supported_transcripts_and_closed_metadata(self, tmp_path, harness, repo):
        root = tmp_path / "home"
        rows = _rich_native(harness)
        path = _write(root, _native_rel(harness), _jsonl(rows))
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        assert {fd["severity"] for fd in _secrets(run_audit(path))} == {"low"}

        def objects(value, pointer=()):
            if isinstance(value, dict):
                yield pointer
                for key, child in value.items():
                    # Only Anthropic tool input is an arbitrary structured payload.
                    if key == "input" and value.get("type") == "tool_use":
                        continue
                    yield from objects(child, pointer + (key,))
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    yield from objects(child, pointer + (i,))

        def at(value, pointer):
            for key in pointer:
                value = value[key]
            return value

        for pointer in objects(rows):
            target = at(rows, pointer)
            for key in ("config", *target):
                if key == "input" and target.get("type") == "tool_use":
                    continue
                bad = copy.deepcopy(rows)
                at(bad, pointer)[key] = _fake_jwk()
                path.write_text(_jsonl(bad))
                assert {fd["severity"] for fd in _secrets(run_audit(path))} == {UNVERIFIED_LOG}, (
                    pointer,
                    key,
                )

    @pytest.mark.parametrize("harness", ["codex", "archive", "claude", "subagent"])
    def test_jsonl_parser_and_later_records_fail_closed(self, tmp_path, harness):
        rows = _rich_native(harness)
        raw = _jsonl(rows)
        variants = [raw + "{", raw + "[" * 1500 + "]" * 1500 + "\n"]
        for value in (None, False, 17, {"config": _fake_jwk()}):
            variants += [_jsonl([*rows, value]), _jsonl([value, *rows])]
        variants += [raw.replace('"timestamp":', '"timestamp":null,"timestamp":', 1)]
        for number in (float("nan"), float("inf"), -float("inf"), True):
            bad = copy.deepcopy(rows)
            if harness in ("codex", "archive"):
                bad[-1]["payload"]["info"]["total_token_usage"]["input_tokens"] = number
            else:
                bad[1]["message"]["usage"]["input_tokens"] = number
            variants.append(_jsonl(bad))
        for text in variants:
            path = _write(tmp_path, _native_rel(harness), text)
            assert {fd["severity"] for fd in _secrets(run_audit(path))} == {UNVERIFIED_LOG}

    def test_codex_basename_and_calendar_are_minted(self, tmp_path):
        raw = _jsonl(_rich_native("codex"))
        for rel in (
            R3_CODEX.replace("2026-10-25T11-01-23", "2026-99-25T11-01-23"),
            R3_CODEX.replace("2026-10-25T11-01-23", "2025-02-29T11-01-23"),
            R3_CODEX.replace("T11-01-23", "T25-01-23"),
            R3_CODEX.replace("/10/25/", "/10/24/"),
            R3_CODEX.replace(NATIVE_ID, "settings"),
            R3_CODEX.replace(".jsonl", "_" + NATIVE_ID + ".jsonl"),
        ):
            path = _write(tmp_path, rel, raw)
            assert {fd["severity"] for fd in _secrets(run_audit(path))} == {"high"}

    @pytest.mark.parametrize("state", ["tracked", "corrupt"])
    def test_fifty_cross_harness_git_controls(self, tmp_path, state):
        paths = [
            R3_CODEX,
            R3_CLAUDE,
            R3_SUBAGENT,
            ".codex/sessions/2026/10/25/rollout-settings.jsonl",
            ".codex/archived_sessions/rollout-config.jsonl",
        ]
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
        if state == "corrupt":
            (tmp_path / ".git/index").write_bytes(b"invalid-index")
        checked = 0
        for rel in paths:
            rows = _rich_native("codex" if ".codex/" in rel else "claude")
            secret = json.dumps(_fake_jwk())
            for text in (
                secret,
                secret + "\n{",
                _jsonl(rows),
                _jsonl([*rows, {"config": _fake_jwk()}]),
                secret[:-1] + ', "k": "public"}\n',
            ):
                path = _write(tmp_path, rel, text)
                if state == "tracked":
                    subprocess.run(
                        ["git", "add", rel], cwd=tmp_path, check=True, capture_output=True
                    )
                assert {fd["severity"] for fd in _secrets(run_audit(path))} == {"high"}
                checked += 1
        assert checked == 25

    @pytest.mark.parametrize("harness", ["codex", "claude"])
    def test_native_classification_once_for_multiple_findings(self, tmp_path, monkeypatch, harness):
        path = _write(tmp_path, _native_rel(harness), _jsonl(_rich_native(harness)))
        original = engine._native_jsonl
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(engine, "_native_jsonl", counted)
        found = _secrets(run_audit(path))
        assert len(found) > 1
        assert {fd["severity"] for fd in found} == {"low"}
        assert calls == 1


class TestCredentialChecksStayActiveInHarnessRoots:
    @pytest.mark.usefixtures("reach_within_tmp")
    def test_readable_auth_json_still_reports_store_perms(self, tmp_path):
        root = tmp_path / ".codex"
        _write(root, "auth.json", json.dumps({"tokens": {"id_token": _vendor_key()}}), 0o644)
        _write(root, f"sessions/2026/10/17/{CODEX_NAME}", _jsonl_record())

        report = run_audit(root)

        perms = [fd for fd in report["findings"] if fd["rule"] == "SECRETS-STORE-PERMS"]
        assert [fd["path"] for fd in perms] == ["auth.json"]
        assert perms[0]["severity"] == "medium"
        assert all(fd["path"] != "auth.json" for fd in _secrets(report))

    @pytest.mark.usefixtures("reach_within_tmp")
    def test_store_named_file_inside_a_session_dir_is_still_a_store(self, tmp_path):
        root = tmp_path / ".codex"
        _write(root, "sessions/auth.json", json.dumps({"key": _fake_jwk()}), 0o644)

        report = run_audit(root)

        assert "SECRETS-STORE-PERMS" in {fd["rule"] for fd in report["findings"]}
        assert not _secrets(report)

    @pytest.mark.usefixtures("reach_within_tmp")
    def test_codex_home_tmp_cache_is_skipped_but_stores_are_not(self, tmp_path, monkeypatch):
        # Task #2316: Codex keeps its curated plugin clone and marketplace
        # caches under $CODEX_HOME/.tmp. Walking them filled MAX_FILES and
        # reported cached plugin code (LLM-EVAL, SECRETS-KEY) as ours.
        monkeypatch.setattr(engine, "MAX_FILES", 5)
        for var in ("UA_NODE_CODEX_HOME", "CODEX_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        root = tmp_path / "home" / ".codex"
        _write(root, "auth.json", json.dumps({"tokens": {"id_token": _vendor_key()}}), 0o644)
        for i in range(20):
            _write(root, f".tmp/plugins/plugins/p{i}/scripts/run.py", _CACHED_PLUGIN)
        _write(root, ".tmp/marketplaces/m/plugin.py", _CACHED_PLUGIN)

        report = run_audit(root)

        assert report["truncated"] is False
        assert not [fd for fd in report["findings"] if (fd["path"] or "").startswith(".tmp/")]
        perms = [fd for fd in report["findings"] if fd["rule"] == "SECRETS-STORE-PERMS"]
        assert [fd["path"] for fd in perms] == ["auth.json"]
        # Bound to the Codex home, not the name: another `.codex/.tmp` is source.
        other = tmp_path / "copy" / ".codex"
        _write(other, ".tmp/run.py", _CACHED_PLUGIN)
        assert [fd["path"] for fd in _secrets(run_audit(other))] == [".tmp/run.py"]

    @pytest.mark.parametrize("state", ["committed", "staged"])
    def test_codex_tmp_inside_a_repository_is_source(self, tmp_path, monkeypatch, state):
        # PR #1378 review P1: a `.codex/.tmp` in a work tree is repository
        # source, even when that `.codex` is the configured Codex home, and the
        # staged-blob inspection must see it too.
        repo = tmp_path / "repo"
        monkeypatch.setenv("UA_NODE_CODEX_HOME", str(repo / ".codex"))
        rel = ".codex/.tmp/source.py"
        path = _write(repo, rel, _CACHED_PLUGIN)
        if state == "committed":
            _git_init_commit(repo, rel)
        else:
            _git_init_commit(repo)
            subprocess.run(["git", "add", rel], cwd=repo, check=True, capture_output=True)
            path.unlink()  # only the index holds it now

        report = run_audit(repo)

        assert (rel, "high") in {(fd["path"], fd["severity"]) for fd in _secrets(report)}
        assert report["status"] == "fail"

    def test_owner_only_auth_json_reports_nothing(self, tmp_path):
        root = tmp_path / ".codex"
        _write(root, "auth.json", json.dumps({"tokens": {"id_token": _vendor_key()}}), 0o600)

        rules = {fd["rule"] for fd in run_audit(root)["findings"]}

        assert not {"SECRETS-KEY", "SECRETS-STORE-PERMS"} & rules

    def test_surfaces_scan_of_a_codex_tree_passes_the_high_gate(self, tmp_path):
        root = tmp_path / ".codex"
        _write(root, f"sessions/2026/10/17/{CODEX_NAME}", _jsonl_record())
        _write(root, "memories/raw_memories.md", _plain_json())

        report = run_surface_scan(roots=[str(root)], allowlist=[tmp_path], file_tasks=True)

        assert report["summary"]["high"] == 0
        assert report["summary"]["low"] >= 2
        assert not gated(report["findings"], "high")
        assert report["tasks"]["created"] == 0
        assert task_list(status="all")["tasks"] == []


def _git_init_commit(root, *tracked):
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        *(["add", rel] for rel in tracked),
        ["commit", "-qm", "init", "--allow-empty"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _corrupt_index(root):
    """git now fails (exit 128) on any index read: tracked state is unknown."""
    (root / ".git" / "index").write_bytes(b"not an index")


class TestUnknownGitStateFailsClosed:
    """PR #1299 review: `git ls-files --error-unmatch` exits 128 on a corrupt
    index, and any non-zero exit used to read as "untracked", which relaxed a
    tracked file's findings. Unknown must keep them at full severity."""

    def test_git_file_tracked_distinguishes_unknown(self, tmp_path):
        from unified_agent.audit.rules import _git_file_tracked

        root = tmp_path / "repo"
        _write(root, "tracked.json", "{}")
        _write(root, "untracked.json", "{}")
        _git_init_commit(root, "tracked.json")
        assert _git_file_tracked(root, "tracked.json") is True
        assert _git_file_tracked(root, "untracked.json") is False

        _corrupt_index(root)

        assert _git_file_tracked(root, "tracked.json") is None
        assert _git_file_tracked(root, "untracked.json") is None

    def test_git_file_tracked_timeout_is_unknown(self, tmp_path, monkeypatch):
        from unified_agent.audit import rules

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr(rules.subprocess, "run", timeout)
        assert rules._git_file_tracked(tmp_path, "x.json") is None

    def test_tracked_transcript_with_a_corrupt_index_stays_high(self, tmp_path):
        """Unknown Git must keep a recognized rollout at full severity."""
        root = tmp_path / ".codex"
        rel = f"sessions/2026/10/17/{CODEX_NAME}"
        _write(root, rel, _jsonl_record())
        _git_init_commit(root, rel)
        _corrupt_index(root)

        report = run_audit(root)

        assert {fd["severity"] for fd in _secrets(report)} == {"high"}
        assert gated(report["findings"], "high")

    def test_credential_store_with_a_corrupt_index_keeps_secrets_key(self, tmp_path):
        """#1846 path: an unknown index must not turn a file into a store."""
        root = tmp_path / ".hermes"
        _write(root, "auth.json", json.dumps({"tokens": {"id_token": _vendor_key()}}))
        _git_init_commit(root)
        _corrupt_index(root)

        found = _secrets(run_audit(root))

        assert [fd["path"] for fd in found] == ["auth.json"]
        assert found[0]["severity"] == "high"

    def test_mcp_config_with_a_corrupt_index_is_not_skipped(self, tmp_path):
        from unified_agent.audit.rules import _mcp_secret_severity

        root = tmp_path / "repo"
        _write(root, ".mcp.json", "{}")
        _git_init_commit(root)
        assert _mcp_secret_severity(root, ".mcp.json") is None  # untracked: skipped

        _corrupt_index(root)

        assert _mcp_secret_severity(root, ".mcp.json") == "high"


@pytest.fixture
def unreadable_git(request):
    """Make a repo's `.git` directory mode 000 for one test, then restore it
    so pytest can clean up the temp tree."""
    locked = []

    def lock(root):
        git_dir = root / ".git"
        git_dir.chmod(0)
        locked.append(git_dir)
        if os.access(git_dir, os.R_OK | os.X_OK):
            pytest.skip("running with privileges that ignore directory modes")

    yield lock
    for git_dir in locked:
        git_dir.chmod(0o755)


class TestUnreadableGitMarkerFailsClosed:
    """PR #1299 review, round 2: an unreadable `.git` directory used to read
    as "no git here", so the walk ran past it and a tracked file counted as
    outside every work tree. A `.git` that exists must count as a work tree
    whose state is unknown."""

    def test_tracked_transcript_under_an_unreadable_git_dir_stays_high(
        self, tmp_path, unreadable_git
    ):
        root = tmp_path / ".codex"
        rel = f"sessions/2026/10/17/{CODEX_NAME}"
        _write(root, rel, _jsonl_record())
        _git_init_commit(root, rel)
        unreadable_git(root)

        report = run_audit(root)

        assert {fd["severity"] for fd in _secrets(report)} == {"high"}
        assert gated(report["findings"], "high")

    def test_tracked_credential_store_under_an_unreadable_git_dir_keeps_secrets_key(
        self, tmp_path, unreadable_git
    ):
        root = tmp_path / ".hermes"
        _write(root, "auth.json", json.dumps({"tokens": {"id_token": _vendor_key()}}))
        _git_init_commit(root, "auth.json")
        unreadable_git(root)

        found = _secrets(run_audit(root))

        assert [fd["path"] for fd in found] == ["auth.json"]
        assert found[0]["severity"] == "high"

    def test_a_dangling_git_symlink_counts_as_a_work_tree(self, tmp_path):
        from unified_agent.audit.engine import _is_git_marker

        (tmp_path / ".git").symlink_to(tmp_path / "missing")
        assert _is_git_marker(tmp_path / ".git") is True

    def test_an_empty_git_dir_is_still_ignored_as_git_ignores_it(self, tmp_path):
        from unified_agent.audit.engine import _is_git_marker

        (tmp_path / ".git").mkdir()
        assert _is_git_marker(tmp_path / ".git") is False
        assert _is_git_marker(tmp_path / "absent" / ".git") is False


ROOT = "/srv/audit-a/.codex"


def _finding(path, line=1, rule="SECRETS-KEY"):
    return {
        "rule": rule,
        "severity": "high",
        "message": "possible secret",
        "fix": "rotate it",
        "path": path,
        "line": line,
    }


def _report(*findings, root=ROOT):
    return {"root": root, "files_scanned": len(findings), "status": "fail", "findings": findings}


def _legacy_accepted_store_task(root=ROOT):
    """The shape of the real pre-#2157 task: classless `<root>:<rule>` marker,
    one finding line per vendor credential store, closed as accepted risk."""
    added = task_add(
        title="[Security Audit] .codex: SECRETS-KEY — 3 finding(s)",
        notes=(
            f"[audit-triage: {root_key(root)}:SECRETS-KEY]\n\nAuto-filed from security_audit "
            f"over {root} (min_severity=high).\n\n"
            "- [high] .codex-global-state.json:1: possible JSON web token committed in source\n"
            "  fix: move the credential\n"
            "- [high] auth.json:5: possible JSON web token committed in source\n"
            "- [high] auth.json:6: possible JSON web token committed in source\n"
            "[note] mode fixed; the directory is owner-only."
        ),
    )
    task_update(
        added["id"],
        status="done",
        append_note=f"{ACCEPTED_RISK_TAG} vendor auth stores, owner-only directory",
        actor="test-agent",
    )
    return added["id"]


class TestClassScopedTriage:
    def test_path_class(self):
        assert path_class("sessions/2026/10/17/x.jsonl") == "sessions"
        assert path_class("auth.json") == "auth.json"
        assert path_class(None) == REPO_CLASS
        # A root file literally named "(repo)" is a file, not the sentinel.
        assert path_class("(repo)") != REPO_CLASS
        odd = path_class("my notes]/x.md")
        assert "]" not in odd and " " not in odd and ":" not in odd
        assert odd != path_class("my_notes_/x.md")
        assert len(path_class("a" * 200 + "/x")) <= 48

    def test_old_class_accepted_risk_does_not_suppress_a_new_class(self):
        _legacy_accepted_store_task()

        res = file_findings_as_tasks(
            _report(_finding("sessions/2026/10/17/rollout-a.jsonl", 3), _finding("auth.json", 5))
        )

        assert res["created"] == 1
        assert res["skipped"] == ["SECRETS-KEY:auth.json"]
        assert res["tasks"][0]["class"] == "sessions"
        new = next(t for t in task_list(status="all")["tasks"] if t["status"] == "open")
        assert new["notes"].startswith(f"[audit-triage: {root_key(ROOT)}:SECRETS-KEY:sessions]")
        assert "(sessions)" in new["title"]

    def test_old_class_accepted_risk_still_suppresses_its_own_classes(self):
        _legacy_accepted_store_task()

        res = file_findings_as_tasks(
            _report(_finding("auth.json", 5), _finding(".codex-global-state.json", 1))
        )

        assert res["created"] == 0
        assert sorted(res["skipped"]) == [
            "SECRETS-KEY:.codex-global-state.json",
            "SECRETS-KEY:auth.json",
        ]

    def test_legacy_marker_does_not_cross_roots(self):
        _legacy_accepted_store_task()

        res = file_findings_as_tasks(_report(_finding("auth.json", 5), root="/srv/audit-b/.codex"))

        assert res["created"] == 1

    def test_new_marker_accepted_risk_covers_only_its_class(self):
        first = file_findings_as_tasks(_report(_finding("sessions/a.jsonl")))
        task_update(
            first["tasks"][0]["id"],
            status="done",
            append_note=f"{ACCEPTED_RISK_TAG} fixtures an agent read",
            actor="test-agent",
        )

        again = file_findings_as_tasks(
            _report(_finding("sessions/b.jsonl"), _finding("config.toml"))
        )

        assert again["skipped"] == ["SECRETS-KEY:sessions"]
        assert again["created"] == 1
        assert again["tasks"][0]["class"] == "config.toml"

    def test_one_task_per_class_and_reruns_dedupe(self):
        report = _report(
            _finding("sessions/a.jsonl"),
            _finding("sessions/b.jsonl"),
            _finding("memories/raw_memories.md"),
        )

        first = file_findings_as_tasks(report)
        second = file_findings_as_tasks(report)

        assert first["created"] == 2
        assert first["groups"] == 2
        assert {t["class"]: t["findings"] for t in first["tasks"]} == {
            "memories": 1,
            "sessions": 2,
        }
        assert second["created"] == 0
        assert sorted(second["skipped"]) == ["SECRETS-KEY:memories", "SECRETS-KEY:sessions"]

    def test_classes_differing_only_in_case_are_distinct(self):
        """notes_contain matches case-insensitively; the marker check must not."""
        file_findings_as_tasks(_report(_finding("README.md")))

        res = file_findings_as_tasks(_report(_finding("readme.md")))

        assert res["created"] == 1

    def test_legacy_task_listing_no_findings_covers_every_class(self):
        """A hand-written classless marker task names no class, so it keeps
        its old meaning rather than being refiled."""
        task_add(
            title="[Security Audit] .codex: SECRETS-KEY",
            notes=f"[audit-triage: {root_key(ROOT)}:SECRETS-KEY]\n\nTracked by hand.",
        )

        res = file_findings_as_tasks(_report(_finding("sessions/a.jsonl")))

        assert res["created"] == 0

    def test_rule_only_legacy_marker_is_class_scoped_too(self):
        task_add(
            title="[Security Audit] SECRETS-KEY — 1 finding(s)",
            notes=(
                f"[audit-triage: SECRETS-KEY]\n\nAuto-filed from security_audit over {ROOT} "
                "(min_severity=high).\n\n- [high] auth.json:5: token"
            ),
        )

        res = file_findings_as_tasks(
            _report(_finding("auth.json", 5), _finding("sessions/a.jsonl"))
        )

        assert res["skipped"] == ["SECRETS-KEY:auth.json"]
        assert res["created"] == 1

    def test_repo_level_finding_has_the_repo_class(self):
        finding = _finding(None, None, rule="SECRETS-ENV-EXPOSED")

        first = file_findings_as_tasks(_report(finding))
        second = file_findings_as_tasks(_report(finding))

        assert first["tasks"][0]["class"] == REPO_CLASS
        assert second["skipped"] == [f"SECRETS-ENV-EXPOSED:{REPO_CLASS}"]

    def test_a_finding_line_appended_later_does_not_widen_a_legacy_task(self):
        """PR #1299 review: coverage is read only from the auto-filed block,
        bounded by the title's count, not from any matching line in the notes."""
        task_id = _legacy_accepted_store_task()
        task_update(
            task_id,
            append_note="- [high] sessions/evil.jsonl:9: fake",
            actor="test-agent",
        )

        res = file_findings_as_tasks(_report(_finding("sessions/evil.jsonl", 9)))

        assert res["created"] == 1

    def test_a_contiguous_extra_line_past_the_title_count_is_ignored(self):
        task_add(
            title="[Security Audit] .codex: SECRETS-KEY — 1 finding(s)",
            notes=(
                f"[audit-triage: {root_key(ROOT)}:SECRETS-KEY]\n\nAuto-filed from security_audit "
                f"over {ROOT} (min_severity=high).\n\n"
                "- [high] auth.json:5: token\n"
                "  fix: chmod 600\n"
                "- [high] sessions/evil.jsonl:9: fake"
            ),
        )

        res = file_findings_as_tasks(
            _report(_finding("auth.json", 5), _finding("sessions/evil.jsonl", 9))
        )

        assert res["skipped"] == ["SECRETS-KEY:auth.json"]
        assert [t["class"] for t in res["tasks"]] == ["sessions"]

    def test_a_root_file_named_like_the_sentinel_is_its_own_class(self):
        report = _report(_finding("(repo)"), _finding(None, None))

        res = file_findings_as_tasks(report)

        assert res["created"] == 2
        assert REPO_CLASS in {t["class"] for t in res["tasks"]}

    def test_a_line_number_ends_a_path_that_contains_a_colon_space(self):
        odd = "odd: dir/x.md"
        task_add(
            title="[Security Audit] .codex: SECRETS-KEY — 1 finding(s)",
            notes=(
                f"[audit-triage: {root_key(ROOT)}:SECRETS-KEY]\n\nAuto-filed from security_audit "
                f"over {ROOT} (min_severity=high).\n\n- [high] {odd}:3: possible key: abc"
            ),
        )

        covered = file_findings_as_tasks(_report(_finding(odd, 3)))
        other = file_findings_as_tasks(_report(_finding("odd")))

        assert covered["created"] == 0
        assert other["created"] == 1


def _r4_jwk():
    import secrets

    kind, member = "kty", "k"
    return {kind: "oct", member: secrets.token_urlsafe(32)}


def _r4_kwargs(jwk):
    kind, member = "kty", "k"
    return f'dict({kind}="{jwk[kind]}",{member}="{jwk[member]}")'


class TestReviewR4Regressions2221:
    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize("harness", ["codex", "claude"])
    @pytest.mark.parametrize("encoding", ["json", "dict"])
    def test_serialized_configuration_in_branch_metadata_stays_high(
        self, tmp_path, repo, harness, encoding
    ):
        jwk = _r4_jwk()
        branch = json.dumps(jwk) if encoding == "json" else _r4_kwargs(jwk)
        if encoding == "dict":
            assert (
                subprocess.run(
                    ["git", "check-ref-format", "--branch", branch], capture_output=True
                ).returncode
                == 0
            )
        rows = (
            _codex_records("ordinary chat", "2026-10-25T11:01:23Z")
            if harness == "codex"
            else [_claude_record("ordinary chat")]
        )
        if harness == "codex":
            rows[0]["payload"]["git"] = {"branch": branch}
        else:
            rows[0]["gitBranch"] = branch
        text = _jsonl(rows)
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
        ordinary = _write(tmp_path, "settings.json", text)
        path = _write(tmp_path, R3_CODEX if harness == "codex" else R3_CLAUDE, text)
        assert {fd["severity"] for fd in _secrets(run_audit(ordinary))} == {"high"}
        assert {fd["severity"] for fd in _secrets(run_audit(path))} == {"high"}


def _r4_fixture(harness):
    if harness in ("codex", "claude"):
        rows = _rich_native(harness, "ordinary chat")
        if harness == "claude":
            rows[1]["message"]["content"][2]["input"] = {}
        return rows, _native_rel(harness), _jsonl
    message = _rich_metadata(harness, "ordinary chat")
    if harness != "gemini":
        message["tool_calls"] = [
            {
                "id": "call_fixture",
                "type": "function",
                "function": {"name": "fixture", "arguments": "{}"},
            }
        ]
    artifact = (
        {"messages": [message]}
        if harness == "hermes_jsonl"
        else _complete_envelope(harness, [message])
    )
    encode = (lambda a: _jsonl(a["messages"])) if harness == "hermes_jsonl" else json.dumps
    return artifact, _harness_rel(harness), encode


def _r4_set(artifact, path, value):
    for key in path[:-1]:
        artifact = artifact[key]
    artifact[path[-1]] = value


class TestReviewR4Metadata2221:
    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize("harness", ["codex", "claude", "gemini", "hermes", "hermes_jsonl"])
    @pytest.mark.parametrize("encoding", ["pretty", "compact", "dict", "yaml"])
    def test_secret_metadata_keeps_whole_artifact_high(self, tmp_path, repo, harness, encoding):
        jwk = _r4_jwk()
        value = {
            "pretty": json.dumps(jwk, indent=2),
            "compact": json.dumps(jwk, separators=(",", ":")),
            "dict": _r4_kwargs(jwk),
            "yaml": "\n".join(f"{key}: {item}" for key, item in jwk.items()) + "\n",
        }[encoding]
        artifact, rel, encode = _r4_fixture(harness)
        fields = {
            "codex": [
                (0, "payload", "git", "branch"),
                (0, "payload", "git", "repository_url"),
                (0, "payload", "agent_nickname"),
                (0, "payload", "agent_path"),
                (0, "payload", "cwd"),
                (0, "payload", "runtime_workspace_roots", 0),
            ],
            "claude": [
                *(
                    (i, key)
                    for i in (0, 1, 2)
                    for key in ("gitBranch", "slug", "teamName", "agentName", "cwd")
                ),
                (1, "message", "content", 1, "signature"),
            ],
            "gemini": [
                ("directories", 0),
                ("memoryScratchpad", "touchedPaths", 0),
                ("memoryScratchpad", "toolSequence", 0),
                ("messages", 0, "toolCalls", 0, "displayName"),
                ("messages", 0, "toolCalls", 0, "description"),
            ],
            "hermes": [
                ("base_url",),
                ("messages", 0, "display_metadata", "data_use", 0),
                ("messages", 0, "reasoning_details", 0, "signature"),
                ("messages", 0, "tool_calls", 0, "function", "name"),
            ],
            "hermes_jsonl": [
                ("messages", 0, "display_metadata", "data_use", 0),
                ("messages", 0, "reasoning_details", 0, "signature"),
                ("messages", 0, "tool_calls", 0, "function", "name"),
            ],
        }[harness]
        if harness == "gemini":
            artifact["directories"] = ["/workspace"]
            artifact["memoryScratchpad"] = {
                "version": 1,
                "workflowSummary": "ordinary history",
                "toolSequence": ["fixture"],
                "touchedPaths": ["file.py"],
            }
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
        for pointer in fields:
            changed = copy.deepcopy(artifact)
            injected = (
                "/workspace/" + value
                if pointer[-1] == "cwd" or "runtime_workspace_roots" in pointer
                else value
            )
            if pointer == ("base_url",):
                injected = "https://example.test/" + value
            _r4_set(changed, pointer, injected)
            raw = encode(changed)
            path = _write(tmp_path, rel, raw)
            ordinary = _write(tmp_path, "settings.json", raw)
            assert {f["severity"] for f in _secrets(run_audit(ordinary))} == {"high"}, pointer
            assert {f["severity"] for f in _secrets(run_audit(path))} == {"high"}, pointer

    @pytest.mark.parametrize("harness", ["codex", "claude", "gemini", "hermes", "hermes_jsonl"])
    def test_declared_history_and_tool_payloads_remain_low(self, tmp_path, harness):
        value = _r4_kwargs(_r4_jwk())
        artifact, rel, encode = _r4_fixture(harness)
        fields = {
            "codex": [
                (0, "payload", "base_instructions", "text"),
                (1, "payload", "content", 0, "text"),
                (3, "payload", "arguments"),
                (4, "payload", "output"),
                (5, "payload", "message"),
            ],
            "claude": [
                (0, "message", "content"),
                (1, "message", "content", 0, "text"),
                (1, "message", "content", 1, "thinking"),
                (1, "message", "content", 2, "input"),
                (2, "message", "content", 0, "content"),
            ],
            "gemini": [
                ("summary",),
                ("messages", 0, "content"),
                ("messages", 0, "thoughts", 0, "description"),
                ("messages", 0, "toolCalls", 0, "args"),
                ("memoryScratchpad", "workflowSummary"),
            ],
            "hermes": [
                ("system_prompt",),
                ("messages", 0, "content"),
                ("messages", 0, "tool_calls", 0, "function", "arguments"),
                ("messages", 0, "reasoning"),
            ],
            "hermes_jsonl": [
                ("messages", 0, "content"),
                ("messages", 0, "tool_calls", 0, "function", "arguments"),
                ("messages", 0, "reasoning"),
            ],
        }[harness]
        if harness == "gemini":
            artifact["memoryScratchpad"] = {"version": 1, "workflowSummary": "normal workflow"}
        for pointer in fields:
            changed = copy.deepcopy(artifact)
            payload = {"config": value} if pointer[-1] in ("args", "input") else value
            _r4_set(changed, pointer, payload)
            path = _write(tmp_path, rel, encode(changed))
            assert {f["severity"] for f in _secrets(run_audit(path))} == {"low"}, pointer
            # Metadata anywhere in the same artifact still blocks file-wide LOW.
            if harness in ("codex", "claude"):
                _r4_set(
                    changed,
                    (0, "payload", "agent_nickname") if harness == "codex" else (0, "slug"),
                    value,
                )
            elif harness == "gemini":
                changed["directories"] = ["/workspace/" + value]
            else:
                changed["messages"][0]["tool_calls"][0]["function"]["name"] = value
            path.write_text(encode(changed))
            assert {f["severity"] for f in _secrets(run_audit(path))} == {"high"}, pointer

    @pytest.mark.parametrize("harness", ["codex", "claude"])
    def test_real_branches_and_absent_git_metadata(self, tmp_path, harness):
        for branch in (
            "main",
            "feature/nested-fix",
            "修复/历史",
            "release/v1.2",
            "topic/@name",
            "topic/-dash",
            "@",
        ):
            rows = _rich_native(harness)
            if harness == "codex":
                rows[0]["payload"]["git"]["branch"] = branch
            else:
                for row in rows:
                    row["gitBranch"] = branch
            path = _write(tmp_path, _native_rel(harness), _jsonl(rows))
            assert {f["severity"] for f in _secrets(run_audit(path))} == {"low"}
        # Codex collect_git_info omits branch for detached HEAD, and git entirely
        # outside a repository. No speculative string sentinel is exempted.
        rows = _rich_native(harness)
        if harness == "codex":
            del rows[0]["payload"]["git"]["branch"]
        else:
            for row in rows:
                del row["gitBranch"]
        path.write_text(_jsonl(rows))
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"low"}
        if harness == "codex":
            rows[0]["payload"].pop("git")
            path.write_text(_jsonl(rows))
            assert {f["severity"] for f in _secrets(run_audit(path))} == {"low"}

    def test_branch_syntax_matches_git_without_invoking_git_in_classifier(
        self, tmp_path, monkeypatch
    ):
        branches = [
            "main",
            "修复/历史",
            "feature/nested-fix",
            "topic/@name",
            "topic/-dash",
            "@",
            "a.locked",
            "",
            "HEAD",
            "-main",
            "/main",
            "main/",
            "a//b",
            ".main",
            "a/.b",
            "a.lock",
            "a.lock/b",
            "main.",
            "a..b",
            "a@{b",
            "has space",
            "a\tb",
            "a\nb",
            "a\x7fb",
            "a~b",
            "a^b",
            "a:b",
            "a?b",
            "a*b",
            "a[b",
            "a\\b",
            "@{-1}",
        ]
        expected = [
            subprocess.run(
                ["git", "check-ref-format", "--branch", b], cwd=tmp_path, capture_output=True
            ).returncode
            == 0
            for b in branches
        ]

        def no_process(*args, **kwargs):
            raise AssertionError("branch validation must be pure")

        monkeypatch.setattr(subprocess, "run", no_process)
        assert [engine._git_branch(b) for b in branches] == expected

    @pytest.mark.parametrize("harness", ["codex", "claude", "gemini", "hermes", "hermes_jsonl"])
    def test_metadata_scanned_once_per_artifact(self, tmp_path, monkeypatch, harness):
        artifact, rel, encode = _r4_fixture(harness)
        if harness in ("codex", "claude"):
            artifact = _rich_native(harness)
        else:
            artifact["messages"][0]["content"] = json.dumps(_fake_jwk())
        calls = []
        original = engine.check_secrets

        def counted(relpath, text):
            calls.append(len(text))
            return original(relpath, text)

        monkeypatch.setattr(engine, "check_secrets", counted)
        path = _write(tmp_path, rel, encode(artifact))
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"low"}
        assert len(calls) == 1
        assert calls[0] < len(path.read_text())

    @pytest.mark.parametrize("harness", ["claude", "gemini", "hermes", "hermes_jsonl"])
    def test_part_metadata_is_scanned_but_tool_objects_are_payloads(self, tmp_path, harness):
        jwk = _r4_jwk()
        value = _r4_kwargs(jwk)
        artifact, rel, encode = _r4_fixture(harness)
        if harness == "gemini":
            artifact["messages"][0]["content"] = {"functionCall": {"name": value, "args": {}}}
        else:
            message = artifact[0]["message"] if harness == "claude" else artifact["messages"][0]
            message["content"] = [
                {"type": "image", "source": {"type": "base64", "media_type": value, "data": "AA=="}}
            ]
        path = _write(tmp_path, rel, encode(artifact))
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"high"}
        # A structured payload is not walked even if it resembles metadata.
        artifact, rel, encode = _r4_fixture(harness)
        if harness == "claude":
            artifact[1]["message"]["content"][2]["input"] = {
                "cwd": value,
                "metadata": {"branch": jwk},
            }
        elif harness == "gemini":
            artifact["messages"][0]["content"] = {
                "functionResponse": {
                    "name": "fixture",
                    "response": {"cwd": value, "metadata": {"branch": jwk}},
                }
            }
        else:
            artifact["messages"][0]["tool_calls"][0]["function"]["arguments"] = json.dumps(
                {"cwd": value, "metadata": {"branch": jwk}}
            )
        path.write_text(encode(artifact))
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"low"}

    @pytest.mark.parametrize("harness", ["hermes", "hermes_jsonl"])
    def test_dynamic_metadata_keys_are_scanned(self, tmp_path, harness):
        import secrets

        prefix = "gh" + "p_"
        credential = prefix + secrets.token_hex(18)
        artifact, rel, encode = _r4_fixture(harness)
        artifact["messages"][0]["_tool_call_uids"] = {credential: "a" * 32}
        raw = encode(artifact)
        path = _write(tmp_path, rel, raw)
        ordinary = _write(tmp_path, "settings.json", raw)
        assert {f["severity"] for f in _secrets(run_audit(ordinary))} == {"high"}
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"high"}


def _r5_fragment():
    kind = "kty"
    return "{" + json.dumps(kind) + ':"RSA",'


def _r5_fixture(harness, value, fragment, *, branch=False):
    if harness == "codex":
        rows = _codex_records("ordinary chat", "2026-10-25T11:01:23Z")
        meta = rows[0]["payload"]
        if branch:
            meta["git"] = {"branch": value}
        else:
            meta["agent_nickname"] = value
        meta["agent_path"] = fragment
        return rows, R3_CODEX, _jsonl
    if harness == "claude":
        row = _claude_record("ordinary chat")
        row["gitBranch" if branch else "slug"] = value
        row["teamName"] = fragment
        return [row], R3_CLAUDE, _jsonl
    row = _complete_message(harness, "ordinary chat")
    if harness != "gemini":
        row["display_metadata"] = {"data_use": [value, fragment]}
    if harness == "hermes_jsonl":
        return [row], HERMES_JSONL, _jsonl
    envelope = _complete_envelope(harness, [row])
    if harness == "gemini":
        envelope["directories"] = [value, fragment]
    return envelope, _harness_rel(harness), json.dumps


class TestReviewR5Regressions2221:
    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize("harness", ["codex", "claude", "gemini", "hermes", "hermes_jsonl"])
    @pytest.mark.parametrize("encoding", ["kwargs", "yaml"])
    def test_metadata_scalars_cannot_change_neighbor_ownership(
        self, tmp_path, repo, harness, encoding
    ):
        jwk = _r4_jwk()
        value = (
            _r4_kwargs(jwk)
            if encoding == "kwargs"
            else "\n".join(f"{key}: {item}" for key, item in jwk.items()) + "\n"
        )
        artifact, rel, encode = _r5_fixture(harness, value, _r5_fragment())
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True, check=True)
        raw = encode(artifact)
        path = _write(tmp_path, rel, raw)
        ordinary = _write(tmp_path, "settings.json", raw)
        assert {f["severity"] for f in _secrets(run_audit(ordinary))} == {"high"}
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"high"}

    @pytest.mark.parametrize("repo", [False, True], ids=["nonrepo", "untracked"])
    @pytest.mark.parametrize("harness", ["codex", "claude"])
    def test_valid_git_branch_cannot_be_hidden_by_other_metadata(self, tmp_path, repo, harness):
        value = _r4_kwargs(_r4_jwk())
        branch_valid = (
            subprocess.run(
                ["git", "check-ref-format", "--branch", value], capture_output=True
            ).returncode
            == 0
        )
        assert branch_valid
        artifact, rel, encode = _r5_fixture(harness, value, _r5_fragment(), branch=True)
        if repo:
            subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True, check=True)
        raw = encode(artifact)
        path = _write(tmp_path, rel, raw)
        ordinary = _write(tmp_path, "settings.json", raw)
        assert {f["severity"] for f in _secrets(run_audit(ordinary))} == {"high"}
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"high"}

    @pytest.mark.parametrize("poison_later", [False, True])
    def test_record_boundaries_isolate_metadata_scalars(self, tmp_path, poison_later):
        rows, rel, encode = _r5_fixture("claude", _r4_kwargs(_r4_jwk()), "ordinary")
        rows.append(copy.deepcopy(rows[0]))
        rows[1].pop("slug")
        rows[int(poison_later)]["teamName"] = _r5_fragment()
        raw = encode(rows)
        path = _write(tmp_path, rel, raw)
        ordinary = _write(tmp_path, "settings.json", raw)
        assert {f["severity"] for f in _secrets(run_audit(ordinary))} == {"high"}
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"high"}


class TestReviewR5ScalarFraming2221:
    @pytest.mark.parametrize("harness", ["codex", "claude", "gemini", "hermes", "hermes_jsonl"])
    def test_literal_boundaries_survive_order_escapes_and_decode_depth(self, tmp_path, harness):
        jwk = _r4_jwk()
        kind, member = "kty", "k"
        encoded_names = (
            json.dumps(jwk)
            .replace(kind, "\\u006b\\u0074\\u0079")
            .replace(json.dumps(member) + ":", '"\\u006b":')
        )
        values = [
            _r4_kwargs(jwk),
            "\n".join(f"{key}: {item}" for key, item in jwk.items()) + "\n",
            json.dumps(jwk),
            encoded_names,
        ]
        fragments = [
            _r5_fragment(),
            _r5_fragment() + '"unfinished',
            '"\\\u2028\x00\r\n雪𝄞' + _r5_fragment(),
            _r5_fragment() * 128,
        ]
        checked = 0
        for form, original in enumerate(values):
            value = original
            # Exercise the shared scanner's bounded decoded-string reader near
            # and beyond its ordinary three-level recursion boundary.
            for depth in range(7):
                for fragment_index, fragment in enumerate(fragments):
                    for order in (False, True):
                        artifact, rel, encode = _r5_fixture(
                            harness, fragment if order else value, value if order else fragment
                        )
                        for sort_keys in (False, True):
                            for ensure_ascii in (False, True):
                                raw = (
                                    "".join(
                                        json.dumps(
                                            r, sort_keys=sort_keys, ensure_ascii=ensure_ascii
                                        )
                                        + "\n"
                                        for r in artifact
                                    )
                                    if isinstance(artifact, list)
                                    else json.dumps(
                                        artifact, sort_keys=sort_keys, ensure_ascii=ensure_ascii
                                    )
                                )
                                label = (
                                    harness,
                                    form,
                                    depth,
                                    fragment_index,
                                    order,
                                    sort_keys,
                                    ensure_ascii,
                                )
                                raw_high = bool(list(engine.check_secrets("ordinary.json", raw)))
                                assert raw_high, label
                                eligible = engine._in_transcript_store(tmp_path / rel, raw)
                                assert not eligible, label
                                checked += 1
                value = json.dumps(value)
        assert checked == 896

    @pytest.mark.parametrize("harness", ["codex", "claude", "gemini", "hermes", "hermes_jsonl"])
    def test_large_dense_fragments_preserve_actual_audit_severity(self, tmp_path, harness):
        value = _r4_kwargs(_r4_jwk())
        # The original artifact remains valid JSON: only a scalar contains the
        # unfinished syntax. Exercise strings and nested lists without reparsing
        # or concatenating their contents into a different source language.
        artifact, rel, encode = _r5_fixture(harness, value, _r5_fragment() * 2048)
        raw = encode(artifact)
        path = _write(tmp_path, rel, raw)
        ordinary = _write(tmp_path, "settings.json", raw)
        assert {f["severity"] for f in _secrets(run_audit(ordinary))} == {"high"}
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"high"}

    @pytest.mark.parametrize("harness", ["codex", "claude", "gemini", "hermes", "hermes_jsonl"])
    def test_single_scan_uses_literal_boundaries_for_metadata_only(
        self, tmp_path, monkeypatch, harness
    ):
        artifact, rel, encode = _r5_fixture(harness, _r4_kwargs(_r4_jwk()), _r5_fragment())
        calls = []
        original = engine.check_secrets

        def counted(relpath, text):
            if relpath == "transcript-metadata":
                decoded = json.loads(text)
                calls.append(isinstance(decoded, list) and all(isinstance(v, str) for v in decoded))
            return original(relpath, text)

        monkeypatch.setattr(engine, "check_secrets", counted)
        path = _write(tmp_path, rel, encode(artifact))
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"high"}
        assert calls == [True]

    @pytest.mark.parametrize("harness", ["codex", "claude", "gemini", "hermes", "hermes_jsonl"])
    def test_history_payload_next_to_public_metadata_fragment_stays_low(self, tmp_path, harness):
        artifact, rel, encode = _r5_fixture(harness, "ordinary metadata", _r5_fragment())
        value = _r4_kwargs(_r4_jwk())
        if harness == "codex":
            artifact[1]["payload"]["content"][0]["text"] = value
        elif harness == "claude":
            artifact[0]["message"]["content"] = value
        elif harness == "hermes_jsonl":
            artifact[0]["content"] = value
        else:
            artifact["messages"][0]["content"] = value
        raw = encode(artifact)
        path = _write(tmp_path, rel, raw)
        ordinary = _write(tmp_path, "settings.json", raw)
        assert {f["severity"] for f in _secrets(run_audit(ordinary))} == {"high"}
        assert {f["severity"] for f in _secrets(run_audit(path))} == {"low"}
