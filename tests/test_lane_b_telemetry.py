"""Lane B validation-gate telemetry (task #731).

Ground-truth contract (pinned — evidence view builds against it):

    build_report(...) -> {
      "as_of": iso-utc,
      "repos": {
        "<slug>": {
          "slug": str,
          "github": {"owner": str, "repo": str},
          "gates": {
            "outside_installs": GatePayload,      # target 10, source github-traffic-clones
            "completed_workflows": GatePayload,   # target 5,  source hub-task-memory
            "repeat_users": GatePayload,          # target 3,  source vault-notes
            "direct_feedback": GatePayload,       # target 1,  source vault-and-hub-memory
          },
        },
        ...
      },
      "totals": { same four keys -> GatePayload },
    }

GatePayload when reachable (including honest zero):
    {"status": "ok", "count": int (>=0), "source": str, "target": int,
     "progress": {"count": int, "target": int, "met": bool}}

GatePayload when unreachable:
    {"status": "unavailable", "count": None, "source": str, "target": int,
     "reason": str, "progress": None}

Reachable-but-empty (count=0) and unreachable (status=unavailable) are
distinct facts — never collapse one into the other.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unified_agent import storage
from unified_agent.lane_b_telemetry import (
    DEFAULT_REPOS,
    GATE_TARGETS,
    SOURCE_COMPLETED_WORKFLOWS,
    SOURCE_DIRECT_FEEDBACK,
    SOURCE_OUTSIDE_INSTALLS,
    SOURCE_REPEAT_USERS,
    build_report,
    count_completed_workflows,
    count_direct_feedback,
    count_outside_installs,
    count_repeat_users,
    progress_for,
    sum_gate_counts,
)
from unified_agent.tools import memory as memory_tool
from unified_agent.tools import tasks as tasks_tool


@pytest.fixture(autouse=True)
def configured_targets(monkeypatch):
    monkeypatch.setenv(
        "UA_LANE_B_REPOS",
        json.dumps(
            [
                {"slug": "workroom", "github": {"owner": "example", "repo": "workroom"}},
                {"slug": "sample_app_b", "github": {"owner": "example", "repo": "sample_app_b"}},
            ]
        ),
    )


FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

# Recorded GitHub /repos/{owner}/{repo}/traffic/clones payload shapes.
CLONES_EMPTY = {"count": 0, "uniques": 0, "clones": []}
CLONES_WORKROOM = {
    "count": 42,
    "uniques": 7,
    "clones": [
        {"timestamp": "2026-08-20T00:00:00Z", "count": 10, "uniques": 3},
        {"timestamp": "2026-08-21T00:00:00Z", "count": 32, "uniques": 4},
    ],
}
CLONES_SAMPLE_APP_B = {
    "count": 15,
    "uniques": 4,
    "clones": [{"timestamp": "2026-08-22T00:00:00Z", "count": 15, "uniques": 4}],
}


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    storage.init_db()
    return tmp_path


@pytest.fixture
def vault_root(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "Wiki").mkdir()
    (root / "00 - Inbox").mkdir()
    return root


def _write_vault_evidence(
    vault: Path,
    *,
    name: str,
    repo: str,
    event_type: str,
    observed_at: str = "2026-08-20",
    participant_ref: str | None = None,
) -> Path:
    lines = [
        "---",
        "lane_b_evidence: true",
        f"repo: {repo}",
        f"event_type: {event_type}",
        f"observed_at: {observed_at}",
    ]
    if participant_ref is not None:
        lines.append(f"participant_ref: {participant_ref}")
    lines.extend(["---", "", f"# evidence {name}", ""])
    path = vault / "Wiki" / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _fixture_fetch(payloads: dict[str, dict]):
    """Injectable transport: url substring -> payload; KeyError raises."""

    def fetch(url: str):
        for key, payload in payloads.items():
            if key in url:
                return payload
        raise LookupError(f"no fixture for url={url}")

    return fetch


def _raising_fetch(exc: BaseException):
    def fetch(_url: str):
        raise exc

    return fetch


# ── (d) target progress computation ─────────────────────────────────


class TestProgressFor:
    def test_met_when_count_meets_target(self):
        assert progress_for(10, 10) == {"count": 10, "target": 10, "met": True}
        assert progress_for(11, 10) == {"count": 11, "target": 10, "met": True}

    def test_not_met_when_below_target(self):
        assert progress_for(0, 10) == {"count": 0, "target": 10, "met": False}
        assert progress_for(3, 5) == {"count": 3, "target": 5, "met": False}

    def test_gate_targets_are_pinned(self):
        assert GATE_TARGETS == {
            "outside_installs": 10,
            "completed_workflows": 5,
            "repeat_users": 3,
            "direct_feedback": 1,
        }


# ── (a) aggregation math ────────────────────────────────────────────


class TestAggregationMath:
    def test_outside_installs_uses_traffic_uniques(self):
        gate = count_outside_installs(
            "example",
            "workroom",
            fetch=_fixture_fetch({"workroom/traffic/clones": CLONES_WORKROOM}),
        )
        assert gate["status"] == "ok"
        assert gate["count"] == 7
        assert gate["source"] == SOURCE_OUTSIDE_INSTALLS
        assert gate["target"] == 10
        assert gate["progress"] == {"count": 7, "target": 10, "met": False}

    def test_sum_gate_counts_across_ok_repos(self):
        a = {
            "status": "ok",
            "count": 7,
            "source": SOURCE_OUTSIDE_INSTALLS,
            "target": 10,
            "progress": progress_for(7, 10),
        }
        b = {
            "status": "ok",
            "count": 4,
            "source": SOURCE_OUTSIDE_INSTALLS,
            "target": 10,
            "progress": progress_for(4, 10),
        }
        total = sum_gate_counts([a, b], source=SOURCE_OUTSIDE_INSTALLS, target=10)
        assert total["status"] == "ok"
        assert total["count"] == 11
        assert total["progress"] == {"count": 11, "target": 10, "met": True}

    def test_build_report_aggregates_per_repo_and_totals(
        self, isolated_db, vault_root, monkeypatch
    ):
        monkeypatch.setattr(
            "unified_agent.lane_b_telemetry._now",
            lambda: FIXED_NOW,
        )
        for _ in range(2):
            memory_tool.memory_save(
                "completed workflow evidence",
                title="wf-workroom",
                tags=[
                    "lane-b-evidence",
                    "repo:workroom",
                    "event:completed_workflow",
                ],
                author="Desktop_Agent4",
            )
        memory_tool.memory_save(
            "completed workflow evidence",
            title="wf-cite",
            tags=[
                "lane-b-evidence",
                "repo:sample_app_b",
                "event:completed_workflow",
            ],
            author="Desktop_Agent4",
        )
        tasks_tool.task_add(
            "Observed session",
            notes="[lane-b-evidence repo=workroom event=completed_workflow]",
        )
        _write_vault_evidence(
            vault_root,
            name="repeat-r1",
            repo="workroom",
            event_type="repeat_use",
            participant_ref="p:alice",
        )
        _write_vault_evidence(
            vault_root,
            name="fb-cite",
            repo="sample_app_b",
            event_type="feature_request",
        )
        memory_tool.memory_save(
            "user asked for paid support",
            title="fb-workroom",
            tags=["lane-b-evidence", "repo:workroom", "event:feature_request"],
            author="Desktop_Agent4",
        )

        report = build_report(
            vault_root=vault_root,
            fetch=_fixture_fetch(
                {
                    "workroom/traffic/clones": CLONES_WORKROOM,
                    "sample_app_b/traffic/clones": CLONES_SAMPLE_APP_B,
                }
            ),
        )

        assert report["as_of"] == FIXED_NOW.isoformat(timespec="seconds")
        assert set(report["repos"]) == {"workroom", "sample_app_b"}

        rr = report["repos"]["workroom"]["gates"]
        assert rr["outside_installs"]["count"] == 7
        assert rr["completed_workflows"]["count"] == 3  # 2 memory + 1 task
        assert rr["repeat_users"]["count"] == 1
        assert rr["direct_feedback"]["count"] == 1  # hub memory

        ct = report["repos"]["sample_app_b"]["gates"]
        assert ct["outside_installs"]["count"] == 4
        assert ct["completed_workflows"]["count"] == 1
        assert ct["repeat_users"]["count"] == 0
        assert ct["direct_feedback"]["count"] == 1  # vault note

        totals = report["totals"]
        assert totals["outside_installs"]["count"] == 11
        assert totals["completed_workflows"]["count"] == 4
        assert totals["repeat_users"]["count"] == 1
        assert totals["direct_feedback"]["count"] == 2
        assert totals["outside_installs"]["progress"]["met"] is True


# ── (b) honest zero-state ───────────────────────────────────────────


class TestHonestZeroState:
    def test_empty_clones_payload_is_zero_not_null(self):
        gate = count_outside_installs(
            "example",
            "workroom",
            fetch=_fixture_fetch({"workroom/traffic/clones": CLONES_EMPTY}),
        )
        assert gate["status"] == "ok"
        assert gate["count"] == 0
        assert gate["count"] is not None
        assert gate["progress"] == {"count": 0, "target": 10, "met": False}

    def test_empty_hub_and_vault_are_zero(self, isolated_db, vault_root, monkeypatch):
        monkeypatch.setattr(
            "unified_agent.lane_b_telemetry._now",
            lambda: FIXED_NOW,
        )
        report = build_report(
            vault_root=vault_root,
            fetch=_fixture_fetch(
                {
                    "workroom/traffic/clones": CLONES_EMPTY,
                    "sample_app_b/traffic/clones": CLONES_EMPTY,
                }
            ),
        )
        for slug in ("workroom", "sample_app_b"):
            gates = report["repos"][slug]["gates"]
            for key in GATE_TARGETS:
                assert gates[key]["status"] == "ok", key
                assert gates[key]["count"] == 0, key
                assert gates[key]["count"] is not None
                assert "reason" not in gates[key] or gates[key].get("reason") is None
        for key in GATE_TARGETS:
            assert report["totals"][key]["status"] == "ok"
            assert report["totals"][key]["count"] == 0

    def test_hub_workflow_counter_zero_on_empty_db(self, isolated_db):
        gate = count_completed_workflows("workroom")
        assert gate["status"] == "ok"
        assert gate["count"] == 0
        assert gate["source"] == SOURCE_COMPLETED_WORKFLOWS

    def test_vault_repeat_counter_zero_on_empty_vault(self, vault_root):
        gate = count_repeat_users("workroom", vault_root=vault_root)
        assert gate["status"] == "ok"
        assert gate["count"] == 0
        assert gate["source"] == SOURCE_REPEAT_USERS


# ── (c) unreachable-source gap reporting ─────────────────────────────


class TestUnreachableGaps:
    def test_github_http_error_is_unavailable_not_zero(self):
        gate = count_outside_installs(
            "example",
            "workroom",
            fetch=_raising_fetch(RuntimeError("HTTP 401 Unauthorized")),
        )
        assert gate["status"] == "unavailable"
        assert gate["count"] is None
        assert gate["source"] == SOURCE_OUTSIDE_INSTALLS
        assert gate["progress"] is None
        assert "401" in gate["reason"]
        assert gate["count"] != 0

    def test_github_404_is_unavailable(self):
        gate = count_outside_installs(
            "example",
            "missing",
            fetch=_raising_fetch(LookupError("HTTP 404 Not Found")),
        )
        assert gate["status"] == "unavailable"
        assert gate["count"] is None
        assert "404" in gate["reason"]

    def test_missing_vault_is_unavailable_for_repeat_users(self, tmp_path):
        missing = tmp_path / "no-such-vault"
        gate = count_repeat_users("workroom", vault_root=missing)
        assert gate["status"] == "unavailable"
        assert gate["count"] is None
        assert gate["source"] == SOURCE_REPEAT_USERS
        assert "vault" in gate["reason"].lower()

    def test_none_vault_root_is_unavailable(self):
        gate = count_repeat_users("workroom", vault_root=None)
        assert gate["status"] == "unavailable"
        assert gate["count"] is None

    def test_build_report_survives_partial_github_failure(
        self, isolated_db, vault_root, monkeypatch
    ):
        monkeypatch.setattr(
            "unified_agent.lane_b_telemetry._now",
            lambda: FIXED_NOW,
        )

        def fetch(url: str):
            if "workroom" in url:
                return CLONES_WORKROOM
            raise RuntimeError("HTTP 503 Service Unavailable")

        report = build_report(vault_root=vault_root, fetch=fetch)
        assert report["repos"]["workroom"]["gates"]["outside_installs"]["status"] == "ok"
        assert report["repos"]["workroom"]["gates"]["outside_installs"]["count"] == 7
        cite = report["repos"]["sample_app_b"]["gates"]["outside_installs"]
        assert cite["status"] == "unavailable"
        assert cite["count"] is None
        # Total must not silently sum past the gap.
        total = report["totals"]["outside_installs"]
        assert total["status"] == "unavailable"
        assert total["count"] is None
        assert "sample_app_b" in total["reason"]
        # Other gates still ok zeros — whole report did not crash.
        assert report["repos"]["workroom"]["gates"]["completed_workflows"]["count"] == 0

    def test_sum_propagates_unavailable(self):
        ok = {
            "status": "ok",
            "count": 7,
            "source": SOURCE_OUTSIDE_INSTALLS,
            "target": 10,
            "progress": progress_for(7, 10),
        }
        bad = {
            "status": "unavailable",
            "count": None,
            "source": SOURCE_OUTSIDE_INSTALLS,
            "target": 10,
            "reason": "HTTP 503 for sample_app_b",
            "progress": None,
        }
        total = sum_gate_counts([ok, bad], source=SOURCE_OUTSIDE_INSTALLS, target=10)
        assert total["status"] == "unavailable"
        assert total["count"] is None
        assert "503" in total["reason"]


# ── source naming + feedback composition ───────────────────────────────


class TestSourceNamingAndFeedback:
    def test_every_gate_names_its_source(self, isolated_db, vault_root, monkeypatch):
        monkeypatch.setattr(
            "unified_agent.lane_b_telemetry._now",
            lambda: FIXED_NOW,
        )
        report = build_report(
            vault_root=vault_root,
            fetch=_fixture_fetch(
                {
                    "workroom/traffic/clones": CLONES_EMPTY,
                    "sample_app_b/traffic/clones": CLONES_EMPTY,
                }
            ),
        )
        expected = {
            "outside_installs": SOURCE_OUTSIDE_INSTALLS,
            "completed_workflows": SOURCE_COMPLETED_WORKFLOWS,
            "repeat_users": SOURCE_REPEAT_USERS,
            "direct_feedback": SOURCE_DIRECT_FEEDBACK,
        }
        for slug in report["repos"]:
            for key, source in expected.items():
                assert report["repos"][slug]["gates"][key]["source"] == source
                assert report["totals"][key]["source"] == source

    def test_direct_feedback_sums_vault_and_hub(self, isolated_db, vault_root):
        memory_tool.memory_save(
            "feature ask",
            title="hub-fb",
            tags=["lane-b-evidence", "repo:workroom", "event:feature_request"],
            author="Desktop_Agent4",
        )
        _write_vault_evidence(
            vault_root,
            name="vault-fb",
            repo="workroom",
            event_type="design_partner_interest",
        )
        gate = count_direct_feedback("workroom", vault_root=vault_root)
        assert gate["status"] == "ok"
        assert gate["count"] == 2
        assert gate["source"] == SOURCE_DIRECT_FEEDBACK
        assert gate["target"] == 1
        assert gate["progress"]["met"] is True

    def test_default_repos_do_not_select_private_projects(self):
        assert DEFAULT_REPOS == []

    def test_report_is_json_serializable(self, isolated_db, vault_root, monkeypatch):
        monkeypatch.setattr(
            "unified_agent.lane_b_telemetry._now",
            lambda: FIXED_NOW,
        )
        report = build_report(
            vault_root=vault_root,
            fetch=_fixture_fetch(
                {
                    "workroom/traffic/clones": CLONES_EMPTY,
                    "sample_app_b/traffic/clones": CLONES_EMPTY,
                }
            ),
        )
        # Round-trip proves None stays None (unavailable) and 0 stays 0.
        raw = json.dumps(report)
        loaded = json.loads(raw)
        assert loaded["repos"]["workroom"]["gates"]["outside_installs"]["count"] == 0


# ── (e) vault root under a dot-directory (task #1970) ───────────────


class TestVaultUnderHiddenParent:
    """Hidden-directory skipping applies inside the vault, not to its location.

    The desktop's pytest temp root is ~/.cache/pytest-<pid> (the tmp guard
    hook), so tmp_path vaults sit under a dot-directory. Filtering on the
    absolute path dropped every vault note there, which made the vault halves
    of the aggregation and direct-feedback tests read zero (task #1970).
    """

    @pytest.fixture
    def hidden_parent_vault(self, tmp_path):
        root = tmp_path / ".hidden-parent" / "vault"
        (root / "Wiki").mkdir(parents=True)
        return root

    def test_vault_notes_count_when_vault_sits_under_dot_directory(self, hidden_parent_vault):
        _write_vault_evidence(
            hidden_parent_vault,
            name="repeat-r1",
            repo="workroom",
            event_type="repeat_use",
            participant_ref="p:alice",
        )
        gate = count_repeat_users("workroom", vault_root=hidden_parent_vault)
        assert gate["status"] == "ok"
        assert gate["count"] == 1

    def test_feedback_notes_count_when_vault_sits_under_dot_directory(
        self, isolated_db, hidden_parent_vault
    ):
        _write_vault_evidence(
            hidden_parent_vault,
            name="fb-workroom",
            repo="workroom",
            event_type="feature_request",
        )
        gate = count_direct_feedback("workroom", vault_root=hidden_parent_vault)
        assert gate["status"] == "ok"
        assert gate["count"] == 1

    def test_dot_directories_inside_vault_are_still_skipped(self, hidden_parent_vault):
        note = _write_vault_evidence(
            hidden_parent_vault,
            name="repeat-r1",
            repo="workroom",
            event_type="repeat_use",
            participant_ref="p:alice",
        )
        trash = hidden_parent_vault / ".trash"
        trash.mkdir()
        (trash / "old.md").write_text(
            note.read_text(encoding="utf-8").replace("p:alice", "p:bob"), encoding="utf-8"
        )
        gate = count_repeat_users("workroom", vault_root=hidden_parent_vault)
        assert gate["count"] == 1
