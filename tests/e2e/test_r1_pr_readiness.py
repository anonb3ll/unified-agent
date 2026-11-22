"""Dispatch slot behavior through the real task and dispatch entrypoints.

External PR review and merge readiness are verified by GitHub and CI.
"""

from __future__ import annotations

from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import CliProcessSpawner, RoutedLaunchBoundary, live_run_count
from unified_agent.tools import delegate as delegate_tool
from unified_agent.tools import tasks as tasks_tool


class _LiveCountBoundary:
    """Observe real dispatch rows without starting an agent process."""

    def __init__(self) -> None:
        self.observations: list[dict[str, int | str]] = []

    def launch(
        self,
        *,
        run_id: str,
        task_id: int,
        agent_label: str,
        delegation_id: int | None,
    ) -> dict:
        self.observations.append(
            {
                "run_id": run_id,
                "total": live_run_count(),
                "excluding_self": live_run_count(exclude_run_id=run_id),
            }
        )
        return {
            "ok": True,
            "stub": True,
            "run_id": run_id,
            "task_id": task_id,
            "agent_label": agent_label,
            "delegation_id": delegation_id,
        }


class _RecordingCliSpawner(CliProcessSpawner):
    """Real spawner type, so the live-run ceiling applies, that never execs."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict] = []

    def spawn(self, request: dict) -> dict:
        self.requests.append(request)
        return {"ok": True, "spawned": True, "pid": 4321 + len(self.requests)}


def _approved_dispatch_task(title: str, to_agent: str = "Desktop_Agent1") -> tuple[int, int]:
    """Create task and delegation through their public product tools."""
    task = tasks_tool.task_add(title=title, lane="dispatch-ready")
    result = delegate_tool.delegate(
        task_spec=(
            f"### Objective\n{title}\n"
            "### Scope\n- tests/e2e/test_r1_pr_readiness.py\n"
            "### Definition of Done\n- dispatch entrypoint returns a durable run id\n"
            "### TDD Constraints\n- Use real task and delegation persistence\n"
            "### Boundaries\n- Do not start an external process\n"
        ),
        from_agent="Desktop_Agent7",
        to_agent=to_agent,
        callback="none",
        parent_task_id=task["id"],
    )
    task_id = result["task_id"]
    tasks_tool.task_update(
        task_id,
        append_note="[plan: approved @ 2026-09-29T00:00:00Z]",
    )
    return task["id"], task_id


class TestR1FeatureCoverage:
    """Primary behavioral coverage for live dispatch slot accounting."""

    def test_real_dispatch_run_is_excluded_from_its_own_slot_count(self):
        _, first_task = _approved_dispatch_task("R1 slot counting first")
        boundary = _LiveCountBoundary()

        first = coordinator.claim_and_dispatch(task_id=first_task, launch=boundary)

        assert first["launched"] is True
        assert first["run_id"]
        assert boundary.observations == [
            {"run_id": first["run_id"], "total": 1, "excluding_self": 0}
        ]
        assert coordinator.get_run(first["run_id"])["task_id"] == first_task
        assert live_run_count() == 1
        assert live_run_count(exclude_run_id=first["run_id"]) == 0

    def test_live_count_includes_other_run_and_unknown_exclusion(self):
        _, first_task = _approved_dispatch_task("R1 slot counting existing")
        first = coordinator.claim_and_dispatch(
            task_id=first_task,
            launch=_LiveCountBoundary(),
        )
        assert first["launched"] is True

        _, second_task = _approved_dispatch_task("R1 slot counting next")
        boundary = _LiveCountBoundary()
        second = coordinator.claim_and_dispatch(task_id=second_task, launch=boundary)

        assert second["launched"] is True
        assert second["run_id"] != first["run_id"]
        assert boundary.observations == [
            {"run_id": second["run_id"], "total": 2, "excluding_self": 1}
        ]
        assert live_run_count(exclude_run_id="run-unknown-999") == 2

    def test_live_ceiling_applies_through_the_real_launch_boundary(self, monkeypatch):
        """At a ceiling of one, the coordinator's own claimed run still launches,
        and a second dispatch is refused before anything is spawned."""
        monkeypatch.setenv("UA_DISPATCH_MAX_LIVE_RUNS", "1")
        spawner = _RecordingCliSpawner()
        boundary = RoutedLaunchBoundary(spawner=spawner)

        _, first_task = _approved_dispatch_task("R1 ceiling first", to_agent="Desktop_agent3")
        first = coordinator.claim_and_dispatch(task_id=first_task, launch=boundary)

        assert first["launched"] is True, first
        assert [r["run_id"] for r in spawner.requests] == [first["run_id"]]

        _, second_task = _approved_dispatch_task("R1 ceiling second", to_agent="Desktop_agent3")
        second = coordinator.claim_and_dispatch(task_id=second_task, launch=boundary)

        assert second["launched"] is False, second
        assert "max_live_runs_reached:1/1" in str(second)
        assert len(spawner.requests) == 1, "the ceiling must refuse before spawning"
        assert live_run_count() == 1
