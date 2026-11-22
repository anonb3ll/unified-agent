"""MCP wrapper for skill-cluster proposals (task #839 / ECC-6)."""

from __future__ import annotations

from ..evolve import run_evolve_pass


def propose_skill_clusters(dry_run: bool = False) -> dict:
    """Cluster high-trust promoted memories into ranked skill-outline proposals.

    Writes (or returns) vault-inbox markdown at ``00 - Inbox/Skill-Cluster-Proposals.md``.
    Never calls ``memory_save``, never writes ``SKILL.md`` / host skill or rule
    directories, never auto-approves, never executes generated text. Evidence
    bodies are untrusted historical data. Idempotent per proposal fingerprint:
    an unchanged ranked set is left in place; changed evidence refreshes the
    same path. ``dry_run=True`` returns markdown without writing.

    Nightly ``unified-agent-sleep-pass`` calls the same pass best-effort before
    any LLM work. Bounds: 400 notes, 8 proposals, project/author/source_host
    isolation, minimum two notes per cluster.
    """
    return run_evolve_pass(write=not dry_run, dry_run=dry_run, require_vault=not dry_run)
