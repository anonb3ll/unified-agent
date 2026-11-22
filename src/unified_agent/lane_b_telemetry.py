"""Lane B external-user validation-gate evidence view (task #731).

Read-only aggregation over three existing sources — GitHub traffic clones,
hub task/memory records, and vault evidence notes. No new tables, no webui
endpoint, no silent zeros when a source is unreachable.

Pinned report shape (tests/test_lane_b_telemetry.py is ground truth):

    {
      "as_of": iso-utc,
      "repos": {
        "<slug>": {
          "slug": str,
          "github": {"owner": str, "repo": str},
          "gates": {
            "outside_installs": GatePayload,      # target 10
            "completed_workflows": GatePayload,   # target 5
            "repeat_users": GatePayload,          # target 3
            "direct_feedback": GatePayload,       # target 1
          },
        },
        ...
      },
      "totals": { same four keys -> GatePayload },
    }

GatePayload when reachable (including honest zero):
    {"status": "ok", "count": int, "source": str, "target": int,
     "progress": {"count": int, "target": int, "met": bool}}

GatePayload when unreachable:
    {"status": "unavailable", "count": None, "source": str, "target": int,
     "reason": str, "progress": None}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from . import config, edition, post_init, storage

FetchFn = Callable[[str], Any]

SOURCE_OUTSIDE_INSTALLS = "github-traffic-clones"
SOURCE_COMPLETED_WORKFLOWS = "hub-task-memory"
SOURCE_REPEAT_USERS = "vault-notes"
SOURCE_DIRECT_FEEDBACK = "vault-and-hub-memory"

GATE_TARGETS: dict[str, int] = {
    "outside_installs": 10,
    "completed_workflows": 5,
    "repeat_users": 3,
    "direct_feedback": 1,
}

DEFAULT_REPOS: list[dict[str, Any]] = []


def configured_repos() -> list[dict[str, Any]]:
    """Explicit report targets; no ambient personal project selection."""
    raw = os.environ.get("UA_LANE_B_REPOS", "").strip()
    if not raw:
        return []
    try:
        repos = json.loads(raw)
        if not isinstance(repos, list):
            raise ValueError
        seen = set()
        for entry in repos:
            slug = entry["slug"]
            github = entry["github"]
            if (
                not all(
                    isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
                    for value in (slug, github["owner"], github["repo"])
                )
                or slug in seen
            ):
                raise ValueError
            seen.add(slug)
        return repos
    except (ValueError, TypeError, KeyError):
        raise ValueError(
            "UA_LANE_B_REPOS must be a JSON list of unique slug/github owner/repo entries"
        ) from None


_FEEDBACK_EVENTS = frozenset({"feature_request", "design_partner_interest"})
_TASK_EVIDENCE_RX = re.compile(
    r"\[lane-b-evidence\s+repo=(?P<repo>[^\s\]]+)\s+event=(?P<event>[^\s\]]+)\]"
)
_GITHUB_CLONES_URL = "https://api.github.com/repos/{owner}/{repo}/traffic/clones"


class ProgressDict(TypedDict):
    count: int
    target: int
    met: bool


def _now() -> datetime:
    """Clock seam — tests monkeypatch this for a pinned as_of."""
    return datetime.now(UTC)


def progress_for(count: int, target: int) -> ProgressDict:
    """Progress against a gate target. ``met`` is inclusive (>=)."""
    return {"count": count, "target": target, "met": count >= target}


def _ok_gate(count: int, *, source: str, target: int) -> dict[str, Any]:
    return {
        "status": "ok",
        "count": int(count),
        "source": source,
        "target": target,
        "progress": progress_for(int(count), target),
    }


def _unavailable_gate(reason: str, *, source: str, target: int) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "count": None,
        "source": source,
        "target": target,
        "reason": reason,
        "progress": None,
    }


def _default_fetch(url: str) -> Any:
    """Live GitHub GET. Raises on non-2xx so callers can surface a gap."""
    import httpx

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "unified-agent-lane-b-telemetry/0.1",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(url, headers=headers, timeout=20.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} {resp.reason_phrase}")
    return resp.json()


def count_outside_installs(
    owner: str,
    repo: str,
    *,
    fetch: FetchFn | None = None,
) -> dict[str, Any]:
    """Outside installs ≈ unique cloners from GitHub traffic/clones.

    Uses ``uniques`` (not raw ``count``) so one prolific cloner does not look
    like ten installs. Reachable empty payload → count 0; transport errors →
    unavailable with the exception text as reason.
    """
    target = GATE_TARGETS["outside_installs"]
    url = _GITHUB_CLONES_URL.format(owner=owner, repo=repo)
    do_fetch = fetch if fetch is not None else _default_fetch
    try:
        payload = do_fetch(url)
    except Exception as exc:
        return _unavailable_gate(
            f"{exc} ({owner}/{repo})",
            source=SOURCE_OUTSIDE_INSTALLS,
            target=target,
        )
    if not isinstance(payload, dict):
        return _unavailable_gate(
            f"unexpected clones payload type for {owner}/{repo}",
            source=SOURCE_OUTSIDE_INSTALLS,
            target=target,
        )
    uniques = payload.get("uniques", 0)
    try:
        count = int(uniques or 0)
    except (TypeError, ValueError):
        return _unavailable_gate(
            f"non-integer uniques in clones payload for {owner}/{repo}",
            source=SOURCE_OUTSIDE_INSTALLS,
            target=target,
        )
    return _ok_gate(count, source=SOURCE_OUTSIDE_INSTALLS, target=target)


def _memory_rows_for_event(repo: str, event: str) -> list[Any]:
    """Notes tagged lane-b-evidence + repo:<slug> + event:<type>."""
    needed = {
        "lane-b-evidence",
        f"repo:{repo}",
        f"event:{event}",
    }
    conn = storage.connect()
    try:
        # Only the caller's tenant's evidence counts (task #1796, same rule as #1762).
        rows = conn.execute(
            "SELECT id, tags FROM memory WHERE tags LIKE ? AND org_id IS ?",
            ("%lane-b-evidence%", edition.tenant_org_id()),
        ).fetchall()
    finally:
        conn.close()
    matched: list[Any] = []
    for row in rows:
        try:
            tags = set(json.loads(row["tags"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            continue
        if needed <= tags:
            matched.append(row)
    return matched


def _task_evidence_count(repo: str, event: str) -> int:
    """Tasks whose notes carry ``[lane-b-evidence repo=… event=…]``."""
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT notes FROM tasks WHERE notes LIKE ? AND org_id IS ?",
            ("%[lane-b-evidence%", edition.tenant_org_id()),
        ).fetchall()
    finally:
        conn.close()
    n = 0
    for row in rows:
        notes = row["notes"] or ""
        for m in _TASK_EVIDENCE_RX.finditer(notes):
            if m.group("repo") == repo and m.group("event") == event:
                n += 1
    return n


def count_completed_workflows(repo: str) -> dict[str, Any]:
    """Completed target workflows from hub memory tags + task note markers."""
    target = GATE_TARGETS["completed_workflows"]
    try:
        mem = len(_memory_rows_for_event(repo, "completed_workflow"))
        tasks = _task_evidence_count(repo, "completed_workflow")
    except Exception as exc:  # noqa: BLE001 — hub read failure is a gap
        return _unavailable_gate(str(exc), source=SOURCE_COMPLETED_WORKFLOWS, target=target)
    return _ok_gate(mem + tasks, source=SOURCE_COMPLETED_WORKFLOWS, target=target)


def _parse_frontmatter(text: str) -> dict[str, str] | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    block = text[3:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip()
    return out


def _iter_vault_evidence(vault_root: Path) -> list[dict[str, str]]:
    """Walk vault for notes with ``lane_b_evidence: true`` frontmatter."""
    records: list[dict[str, str]] = []
    for path in vault_root.rglob("*.md"):
        # Hidden dirs (.obsidian, .trash) are skipped relative to the vault: a
        # vault that itself sits under a dot-directory still counts (task #1970).
        if any(part.startswith(".") for part in path.relative_to(vault_root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_frontmatter(text)
        if not meta:
            continue
        flag = meta.get("lane_b_evidence", "").lower()
        if flag not in {"true", "yes", "1"}:
            continue
        records.append(meta)
    return records


def count_repeat_users(
    repo: str,
    *,
    vault_root: Path | str | None,
) -> dict[str, Any]:
    """Seven-day repeat users from vault evidence notes (unique participant)."""
    target = GATE_TARGETS["repeat_users"]
    if vault_root is None:
        return _unavailable_gate(
            "vault path missing (vault_root is None)",
            source=SOURCE_REPEAT_USERS,
            target=target,
        )
    root = Path(vault_root)
    if not root.is_dir():
        return _unavailable_gate(
            f"vault path missing or not a directory: {root}",
            source=SOURCE_REPEAT_USERS,
            target=target,
        )
    seen: set[str] = set()
    anonymous = 0
    for meta in _iter_vault_evidence(root):
        if meta.get("repo") != repo:
            continue
        if meta.get("event_type") != "repeat_use":
            continue
        pref = meta.get("participant_ref")
        if pref:
            seen.add(pref)
        else:
            anonymous += 1
    return _ok_gate(len(seen) + anonymous, source=SOURCE_REPEAT_USERS, target=target)


def count_direct_feedback(
    repo: str,
    *,
    vault_root: Path | str | None,
) -> dict[str, Any]:
    """Direct feedback = vault evidence + hub memory feature/partner events."""
    target = GATE_TARGETS["direct_feedback"]
    if vault_root is None:
        return _unavailable_gate(
            "vault path missing (vault_root is None)",
            source=SOURCE_DIRECT_FEEDBACK,
            target=target,
        )
    root = Path(vault_root)
    if not root.is_dir():
        return _unavailable_gate(
            f"vault path missing or not a directory: {root}",
            source=SOURCE_DIRECT_FEEDBACK,
            target=target,
        )

    vault_n = 0
    for meta in _iter_vault_evidence(root):
        if meta.get("repo") != repo:
            continue
        if meta.get("event_type") in _FEEDBACK_EVENTS:
            vault_n += 1

    hub_n = 0
    for event in _FEEDBACK_EVENTS:
        hub_n += len(_memory_rows_for_event(repo, event))

    return _ok_gate(vault_n + hub_n, source=SOURCE_DIRECT_FEEDBACK, target=target)


def sum_gate_counts(
    gates: list[dict[str, Any]],
    *,
    source: str,
    target: int,
) -> dict[str, Any]:
    """Sum ok counts across repos; any unavailable component makes the total a gap."""
    if not gates:
        return _unavailable_gate(
            "no configured report repositories (UA_LANE_B_REPOS)", source=source, target=target
        )
    reasons: list[str] = []
    total = 0
    for gate in gates:
        if gate.get("status") != "ok":
            reasons.append(str(gate.get("reason") or "unavailable"))
            continue
        total += int(gate.get("count") or 0)
    if reasons:
        return _unavailable_gate(
            "; ".join(reasons),
            source=source,
            target=target,
        )
    return _ok_gate(total, source=source, target=target)


def build_report(
    *,
    vault_root: Path | str | None = None,
    fetch: FetchFn | None = None,
    repos: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the per-repo + totals evidence report. Never raises on a gap."""
    configuration_error = None
    try:
        repo_list = repos if repos is not None else configured_repos()
    except ValueError as exc:
        repo_list = []
        configuration_error = str(exc)
    if repo_list and vault_root is None:
        vault_root = config.vault_root()

    repo_payloads: dict[str, Any] = {}
    per_gate: dict[str, list[dict[str, Any]]] = {k: [] for k in GATE_TARGETS}

    for entry in repo_list:
        slug = entry["slug"]
        gh = entry["github"]
        owner, name = gh["owner"], gh["repo"]
        gates = {
            "outside_installs": count_outside_installs(owner, name, fetch=fetch),
            "completed_workflows": count_completed_workflows(slug),
            "repeat_users": count_repeat_users(slug, vault_root=vault_root),
            "direct_feedback": count_direct_feedback(slug, vault_root=vault_root),
        }
        repo_payloads[slug] = {
            "slug": slug,
            "github": {"owner": owner, "repo": name},
            "gates": gates,
        }
        for key, gate in gates.items():
            per_gate[key].append(gate)

    totals = {
        key: sum_gate_counts(
            per_gate[key],
            source={
                "outside_installs": SOURCE_OUTSIDE_INSTALLS,
                "completed_workflows": SOURCE_COMPLETED_WORKFLOWS,
                "repeat_users": SOURCE_REPEAT_USERS,
                "direct_feedback": SOURCE_DIRECT_FEEDBACK,
            }[key],
            target=GATE_TARGETS[key],
        )
        for key in GATE_TARGETS
    }

    if configuration_error:
        for gate in totals.values():
            gate["reason"] = configuration_error
    return {
        "as_of": _now().isoformat(timespec="seconds"),
        "repos": repo_payloads,
        "totals": totals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lane B validation-gate evidence report (read-only, UA #731).",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="Vault root (default: UA_VAULT_PATH / config.vault_root())",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Print the report as JSON (default).",
    )
    args = parser.parse_args(argv)
    try:
        repos = configured_repos()
    except ValueError:
        repos = []
    if repos:
        post_init.register_default_hooks()
        storage.init_db()
    report = build_report(vault_root=args.vault)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
