"""Orchestration for one bounded SYSTEM bug patch cycle."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, storage, task_ownership
from ..reviewer import WAITING_FOR_REVIEW, _changed_files_vs_main
from ..tools import cycles as cycles_tool
from ..tools import tasks as tasks_tool
from .eligibility import discover_eligible_tasks, is_system_bug_eligible
from .gates import red_tier_blocked, run_verification_gates
from .state import transition_worker_state

logger = logging.getLogger("unified_agent.bug_worker")

BUG_WORKER_MARKER = "[bug-worker:"
ATTEMPT_MARKER = "[bug-worker-attempt:"


def _kill_cycle(cycle_id: str, agent: str, reason: str) -> None:
    """Best-effort cycle stop — failures must not block task bookkeeping."""
    try:
        cycles_tool.autonomous_cycle_kill(cycle_id, agent, reason)
    except ToolError:
        pass


def _try_record_pr_cycle(cycle_id: str, task_id: int, agent: str, pr_url: str) -> str:
    """Attempt pr transition; return final cycle state (reviewing if approval pending)."""
    cycles_tool.autonomous_cycle_transition(cycle_id, "reviewing", f"review-{task_id}", agent)
    try:
        cycles_tool.autonomous_cycle_transition(
            cycle_id,
            "pr",
            f"pr-{task_id}",
            agent,
            pr_url=pr_url,
        )
        return "pr"
    except Exception as exc:
        from ..tools.approvals import ApprovalPendingError

        if isinstance(exc, ApprovalPendingError):
            return "reviewing"
        raise


@dataclass
class PatchResult:
    status: str
    regression_test_added: bool
    impossibility_documented: bool = False
    detail: str = ""
    changed_files: list[dict] | None = None
    pr_url: str | None = None


class PatcherProtocol(Protocol):
    def patch(
        self,
        *,
        task: dict,
        worktree_path: Path,
        repo_root: Path,
    ) -> PatchResult: ...


def _find_task(task_id: int) -> dict | None:
    """One task by id across the whole store (#1494).

    The lookups used to scan the first 500 rows of ``status="all"``, sorted by
    priority then age, so any task past that window read as "no task with id".
    """
    return next((t for t in tasks_tool.task_list_all(status="all") if t["id"] == task_id), None)


def _attempt_count(notes: str) -> int:
    return notes.count(ATTEMPT_MARKER)


def _append_attempt_note(notes: str, agent: str, detail: str) -> str:
    line = f"{ATTEMPT_MARKER} {storage.now()} agent={agent}] {detail[:200]}"
    return (notes or "").rstrip() + f"\n\n{line}"


def _resolve_repo_root(worktree_rel: str | None) -> Path | None:
    """Absolute path to the git repo containing the task worktree.

    Task #994: a ``[code-task]`` worktree is registered with an absolute
    ``worktree_path`` cut from its project's own checkout, not the cockpit
    workspace. ``resolve_task_worktree`` is the canonical, project-aware
    resolver the pipeline's other gate runners already use for exactly this
    (``reviewer._changed_files_vs_main``, ``merge.py``) — reuse it here
    instead of re-deriving the same workspace-vs-project guess.
    """
    if not worktree_rel:
        return None
    from ..safety import resolve_task_worktree

    try:
        resolved = resolve_task_worktree(worktree_rel)
    except Exception:
        return None
    return resolved if resolved.is_dir() else None


class BugWorker:
    """Bounded SYSTEM bug worker — one ticket per run, human merge required."""

    def __init__(
        self,
        agent_label: str,
        *,
        max_attempts: int | None = None,
        patcher: PatcherProtocol | None = None,
        skip_smoke: bool = False,
        skip_audit: bool = False,
    ):
        self.agent_label = agent_label
        self.max_attempts = max_attempts or config.bug_worker_max_attempts()
        self.patcher = patcher
        self.skip_smoke = skip_smoke
        self.skip_audit = skip_audit

    @staticmethod
    def enabled() -> bool:
        return config.bug_worker_enabled()

    def discover(self, *, limit: int = 20) -> list[dict]:
        if not self.enabled():
            return []
        return discover_eligible_tasks(limit=limit)

    def claim_one(self, task_id: int) -> dict:
        """Atomically claim one eligible task by id."""
        if not self.enabled():
            raise ToolError("bug worker is disabled (UA_BUG_WORKER_ENABLED=0)")
        task = _find_task(task_id)
        if task is None:
            raise ToolError(f"no task with id {task_id}")
        ok, reason = is_system_bug_eligible(task)
        if not ok:
            raise ToolError(f"task {task_id} is not eligible: {reason}")
        claimed = tasks_tool.task_claim(
            task_id,
            agent=self.agent_label,
            expected_project=task.get("project"),
        )
        if task_ownership.is_safe_wait(claimed):
            wait = task_ownership.describe_wait(claimed["safe_wait"])
            raise ToolError(f"task {task_id} was not claimed: {wait}")
        return claimed

    def submit_for_review(
        self,
        task_id: int,
        *,
        pr_url: str,
        notes_append: str = "",
    ) -> dict:
        """Set waiting_review + review marker — never auto-merge."""
        task = _find_task(task_id)
        if task is None:
            raise ToolError(f"no task with id {task_id}")
        notes = task.get("notes") or ""
        from ..reviewer import _has_live_review_marker

        if not _has_live_review_marker(notes):
            notes = notes.rstrip() + f"\n\n{WAITING_FOR_REVIEW}"
        marker = f"{BUG_WORKER_MARKER} handoff @ {storage.now()} pr={pr_url}]"
        notes = notes.rstrip() + f"\n\n{marker}"
        if notes_append.strip():
            notes = notes.rstrip() + f"\n\n{notes_append.strip()}"
        return tasks_tool.task_update(
            task_id,
            status="waiting_review",
            evidence_ref=pr_url,
            notes=notes,
        )

    def run_once(
        self,
        *,
        task_id: int | None = None,
        dry_run: bool = False,
    ) -> dict:
        """One full cycle: claim → patch → verify → waiting_review handoff."""
        if not self.enabled():
            return {"status": "disabled", "reason": "UA_BUG_WORKER_ENABLED=0"}

        state = "queued"
        if task_id is None:
            candidates = self.discover(limit=1)
            if not candidates:
                return {"status": "idle", "reason": "no eligible SYSTEM bug tickets"}
            task_id = candidates[0]["id"]

        if dry_run:
            task = _find_task(task_id)
            if task is None:
                raise ToolError(f"no task with id {task_id}")
            ok, reason = is_system_bug_eligible(task)
            return {
                "status": "dry_run",
                "task_id": task_id,
                "eligible": ok,
                "reason": reason,
                "worker_state": state,
            }

        if self.patcher is None:
            raise ToolError(
                "no patcher configured for bug worker run — inject a PatcherProtocol "
                "implementation or use the CLI with a configured backend"
            )

        claimed = self.claim_one(task_id)
        state = transition_worker_state(state, "claimed")
        notes = claimed.get("notes") or ""

        if _attempt_count(notes) >= self.max_attempts:
            state = transition_worker_state(state, "escalated")
            fail_notes = _append_attempt_note(
                notes, self.agent_label, "retry budget exhausted before run"
            )
            tasks_tool.task_update(task_id, status="blocked", notes=fail_notes)
            return {
                "status": "escalated",
                "task_id": task_id,
                "worker_state": state,
                "reason": "retry budget exhausted",
            }

        cycle = cycles_tool.autonomous_cycle_start(
            self.agent_label,
            task_id=task_id,
            max_rework=0,
            budgets={"retries": self.max_attempts, "time_s": 3600},
        )
        cycle_id = cycle["cycle"]["id"]

        worktree_rel = claimed.get("worktree_path")
        repo_root = _resolve_repo_root(worktree_rel)
        if repo_root is None:
            state = transition_worker_state(state, "blocked")
            _kill_cycle(cycle_id, self.agent_label, "no isolated worktree available")
            tasks_tool.task_update(
                task_id,
                status="blocked",
                notes=_append_attempt_note(notes, self.agent_label, "no worktree"),
            )
            return {
                "status": "blocked",
                "task_id": task_id,
                "worker_state": state,
                "reason": "no worktree",
            }

        cycles_tool.autonomous_cycle_transition(
            cycle_id, "delegated", f"delegate-{task_id}", self.agent_label
        )
        state = transition_worker_state(state, "reproducing")
        cycles_tool.autonomous_cycle_transition(
            cycle_id, "executing", f"exec-{task_id}", self.agent_label
        )
        state = transition_worker_state(state, "patching")

        try:
            patch_result = self.patcher.patch(
                task=claimed,
                worktree_path=repo_root,
                repo_root=repo_root,
            )
        except Exception as exc:
            state = transition_worker_state(state, "blocked")
            _kill_cycle(cycle_id, self.agent_label, f"patch raised: {exc}")
            tasks_tool.task_update(
                task_id,
                status="blocked",
                notes=_append_attempt_note(notes, self.agent_label, f"patch error: {exc}"),
            )
            return {
                "status": "blocked",
                "task_id": task_id,
                "worker_state": state,
                "reason": str(exc),
            }

        if patch_result.status != "success":
            if not patch_result.regression_test_added and not patch_result.impossibility_documented:
                detail = "missing regression test and no documented impossibility"
            else:
                detail = patch_result.detail or patch_result.status
            attempts = _attempt_count(notes) + 1
            new_notes = _append_attempt_note(notes, self.agent_label, detail)
            if attempts >= self.max_attempts:
                state = transition_worker_state(state, "escalated")
                tasks_tool.task_update(task_id, status="blocked", notes=new_notes)
                _kill_cycle(
                    cycle_id,
                    self.agent_label,
                    "retry budget exhausted after patch failure",
                )
                return {
                    "status": "escalated",
                    "task_id": task_id,
                    "worker_state": state,
                    "reason": detail,
                }
            tasks_tool.task_update(task_id, status="open", notes=new_notes)
            cycles_tool.autonomous_cycle_transition(
                cycle_id,
                "rework",
                f"rework-{task_id}",
                self.agent_label,
                reason=detail,
            )
            return {
                "status": "retry",
                "task_id": task_id,
                "worker_state": "blocked",
                "reason": detail,
                "attempts": attempts,
            }

        branch = claimed.get("branch") or ""
        # Never trust patcher-reported paths alone — inspect the worktree,
        # including uncommitted edits the patch protocol may leave staged.
        changed = worktree_changed_files(repo_root, branch)
        red_hits = red_tier_blocked(changed)
        if red_hits:
            state = transition_worker_state(state, "blocked")
            reason = f"red-tier files touched: {', '.join(red_hits)}"
            _kill_cycle(cycle_id, self.agent_label, reason)
            tasks_tool.task_update(
                task_id,
                status="blocked",
                notes=_append_attempt_note(notes, self.agent_label, reason),
            )
            return {
                "status": "blocked",
                "task_id": task_id,
                "worker_state": state,
                "reason": reason,
                "red_tier_files": red_hits,
            }

        state = transition_worker_state(state, "validating")
        cycles_tool.autonomous_cycle_transition(
            cycle_id, "validating", f"validate-{task_id}", self.agent_label
        )
        gate_results = run_verification_gates(
            repo_root,
            skip_smoke=self.skip_smoke,
            skip_audit=self.skip_audit,
        )
        if not gate_results.passed:
            detail = gate_results.summary()
            attempts = _attempt_count(notes) + 1
            new_notes = _append_attempt_note(notes, self.agent_label, detail)
            if attempts >= self.max_attempts:
                state = transition_worker_state(state, "escalated")
                tasks_tool.task_update(task_id, status="blocked", notes=new_notes)
                _kill_cycle(cycle_id, self.agent_label, detail)
                return {
                    "status": "escalated",
                    "task_id": task_id,
                    "worker_state": state,
                    "reason": detail,
                }
            tasks_tool.task_update(task_id, status="open", notes=new_notes)
            return {
                "status": "retry",
                "task_id": task_id,
                "worker_state": "blocked",
                "reason": detail,
            }

        pr_url = (patch_result.pr_url or "").strip()
        if not pr_url:
            detail = "patcher omitted pr_url — refusing synthetic review evidence"
            attempts = _attempt_count(notes) + 1
            new_notes = _append_attempt_note(notes, self.agent_label, detail)
            if attempts >= self.max_attempts:
                state = transition_worker_state(state, "escalated")
                tasks_tool.task_update(task_id, status="blocked", notes=new_notes)
                _kill_cycle(cycle_id, self.agent_label, detail)
                return {
                    "status": "escalated",
                    "task_id": task_id,
                    "worker_state": state,
                    "reason": detail,
                }
            tasks_tool.task_update(task_id, status="open", notes=new_notes)
            return {
                "status": "retry",
                "task_id": task_id,
                "worker_state": "blocked",
                "reason": detail,
                "attempts": attempts,
            }
        cycle_state = _try_record_pr_cycle(cycle_id, task_id, self.agent_label, pr_url)
        # Deliberately stop before 'completed' — human merge gate preserved.
        state = transition_worker_state(state, "waiting_review")
        updated = self.submit_for_review(
            task_id,
            pr_url=pr_url,
            notes_append=f"[bug-worker: gates passed @ {storage.now()}] {gate_results.summary()}",
        )
        return {
            "status": "waiting_review",
            "task_id": task_id,
            "worker_state": state,
            "pr_url": pr_url,
            "gates": gate_results.summary(),
            "task": updated,
            "cycle_id": cycle_id,
            "cycle_state": cycle_state,
        }


def git_changed_files(repo_root: Path, branch: str) -> list[dict]:
    """Helper for patchers: diff branch against main."""
    return _changed_files_vs_main(str(repo_root), branch)


def worktree_changed_files(repo_root: Path, branch: str) -> list[dict]:
    """Union of branch-vs-main and uncommitted worktree paths for red-tier checks.

    Task #994: ``repo_root`` may now be a project-sourced worktree living
    outside the cockpit workspace. ``tools.git._run_git`` resolves its cwd
    through the cockpit-only ``resolve_in_workspace`` sandbox, so it silently
    refused this path and made the uncommitted-file half of the scan go
    empty — a patch that only ever writes uncommitted files (never commits)
    then passed the red-tier gate unnoticed. Use the same pipeline-only,
    project-aware escape hatch ``_changed_files_vs_main`` already uses for
    the committed half of this same check.
    """
    files: dict[str, dict] = {}
    for item in _changed_files_vs_main(str(repo_root), branch or "HEAD"):
        path = item.get("file")
        if isinstance(path, str) and path:
            files[path] = item
    try:
        from ..merge import _run_git_in_repo
        from ..safety import resolve_task_worktree

        cwd = str(resolve_task_worktree(str(repo_root)))
        res = _run_git_in_repo(["status", "--porcelain=v1", "-z", "-uall"], cwd)
    except Exception:
        return list(files.values())
    if getattr(res, "returncode", 1) != 0:
        return list(files.values())
    entries = (res.stdout or "").split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status = entry[:2].strip() or "M"
        path = entry[3:]
        if path:
            files[path] = {"file": path, "status": status}
        # In porcelain -z output, rename/copy entries are followed by the
        # source path as a second NUL-delimited field. Both sides matter to
        # the red-tier gate: moving a protected file is itself protected.
        if ("R" in entry[:2] or "C" in entry[:2]) and index < len(entries):
            source = entries[index]
            index += 1
            if source:
                files[source] = {"file": source, "status": status}
    return list(files.values())
