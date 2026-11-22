"""Per-project delivery modes (FirstMate-style; task #1026).

Each known project stream can select how finished work is shipped:

- ``no-mistakes`` — full validate+hold pipeline; prefer a PR URL as the
  merge target; never auto-merge unless ``yolo`` is on.
- ``direct-pr`` — executor expects a pushed branch / open PR; review still
  validates then waits for merge authority (or ``yolo``).
- ``local-only`` — executor does not expect a remote PR; review holds (or
  ``yolo``-merges) against the local worktree branch.

``yolo`` is orthogonal: when on, green non-red-tier work is merged locally
via the existing ``git_merge_task`` path after the same validation gates.
Remote ``gh pr merge`` is intentionally out of scope for this slice —
fail closed to human merge when there is no mergeable local worktree.

Config file (``UA_PROJECT_DELIVERY_FILE``, default
``~/.unified_agent/project_delivery.yaml``)::

    default:
      mode: no-mistakes
      yolo: false
    projects:
      unified-agent:
        mode: direct-pr
        yolo: false
      example-project:
        mode: local-only
        yolo: true

Absent / empty file → every project resolves to ``no-mistakes`` with
``yolo=false`` (today's global human-merge posture).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from . import config

logger = logging.getLogger("unified_agent.delivery")

DeliveryMode = Literal["no-mistakes", "direct-pr", "local-only"]
DELIVERY_MODES: frozenset[str] = frozenset({"no-mistakes", "direct-pr", "local-only"})
DEFAULT_MODE: DeliveryMode = "no-mistakes"

WAITING_FOR_REVIEW = "[waiting-for: review]"
WAITING_FOR_HUMAN_MERGE = "[waiting-for: human-merge]"

# Optional note-line override: [delivery: mode=direct-pr yolo=on]
_NOTE_OVERRIDE_RE = re.compile(
    r"\[delivery:\s*mode=(?P<mode>no-mistakes|direct-pr|local-only)"
    r"(?:\s+yolo=(?P<yolo>on|off|true|false|1|0))?\]",
    re.IGNORECASE,
)
# The executor-stamped record of the policy a finished run resolved. It uses its
# own tag so a later run never reads it back as an operator override: the
# operator's ``[delivery: mode=…]`` note is the only note-level input
# (Devin #961). Review reads the finished run's frozen snapshot, not this stamp.
_GENERATED_MARKER_RE = re.compile(
    r"\[delivery-run:\s*mode=(?:no-mistakes|direct-pr|local-only)"
    r"(?:\s+yolo=(?:on|off))?\]",
    re.IGNORECASE,
)
_REVIEW_MARKER_LIVE = re.compile(r"\[waiting-for: review\](?! when done)", re.IGNORECASE)


@dataclass(frozen=True)
class DeliveryPolicy:
    """Resolved delivery posture for one project (or task override)."""

    mode: DeliveryMode = DEFAULT_MODE
    yolo: bool = False
    project: str | None = None
    source: str = "default"  # default | file | note | env | profile
    layer: str | None = None  # operating-profile layer label when source is "profile"

    def marker(self) -> str:
        """The generated record of this policy (never an operator override)."""
        yolo_flag = "on" if self.yolo else "off"
        return f"[delivery-run: mode={self.mode} yolo={yolo_flag}]"

    def wait_marker(self) -> str:
        """Review wait annotation the executor should stamp on finish.

        All modes still use ``[waiting-for: review]`` so the existing
        validate-and-hold scanner finds the task. ``local-only`` is expressed
        via the delivery marker (no remote PR expected), not a different wait
        token.
        """
        return WAITING_FOR_REVIEW

    def expects_remote_pr(self) -> bool:
        return self.mode in ("no-mistakes", "direct-pr")


def project_delivery_file_path() -> Path:
    """YAML registry of per-project delivery modes."""
    return config.project_delivery_file_path()


def _normalize_mode(raw: Any) -> DeliveryMode | None:
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("_", "-")
    aliases = {
        "no-mistakes": "no-mistakes",
        "nomistakes": "no-mistakes",
        "direct-pr": "direct-pr",
        "directpr": "direct-pr",
        "local-only": "local-only",
        "localonly": "local-only",
        "local": "local-only",
    }
    mapped = aliases.get(text)
    if mapped in DELIVERY_MODES:
        return mapped  # type: ignore[return-value]
    return None


def _normalize_yolo(raw: Any) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on", "+yolo", "yolo"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_entry(raw: Any) -> tuple[DeliveryMode | None, bool | None]:
    if raw is None:
        return None, None
    if isinstance(raw, str):
        # Allow "direct-pr+yolo" / "local-only +yolo"
        text = raw.strip().lower()
        yolo = "+yolo" in text.replace(" ", "")
        mode_part = text.split("+", 1)[0].strip()
        return _normalize_mode(mode_part), (True if yolo else None)
    if isinstance(raw, dict):
        return _normalize_mode(raw.get("mode") or raw.get("delivery")), _normalize_yolo(
            raw.get("yolo") if "yolo" in raw else raw.get("+yolo")
        )
    return None, None


def load_delivery_registry(file_path: Path | None = None) -> dict[str, Any]:
    """Load the delivery YAML. Missing file → empty defaults (safe).

    Malformed YAML fails closed to the safe default (no-mistakes, yolo off)
    and logs a warning — delivery posture must never crash the review loop.
    """
    path = file_path if file_path is not None else project_delivery_file_path()
    if not path.exists():
        return {"default": {"mode": DEFAULT_MODE, "yolo": False}, "projects": {}}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("delivery config unreadable at %s: %s — using safe defaults", path, exc)
        return {"default": {"mode": DEFAULT_MODE, "yolo": False}, "projects": {}}
    if not isinstance(data, dict):
        logger.warning("delivery config at %s is not a mapping — using safe defaults", path)
        return {"default": {"mode": DEFAULT_MODE, "yolo": False}, "projects": {}}
    return data


def _apply_tier(
    mode: DeliveryMode, yolo: bool, tier: dict[str, Any]
) -> tuple[DeliveryMode, bool, bool]:
    """Apply one operating-profile tier's ``mode``/``yolo``; report whether it set any."""
    applied = False
    tier_mode = _normalize_mode(tier.get("mode"))
    if tier_mode is not None:
        mode, applied = tier_mode, True
    if isinstance(tier.get("yolo"), bool):
        yolo, applied = tier["yolo"], True
    return mode, yolo, applied


def resolve_delivery(
    project: str | None,
    *,
    notes: str | None = None,
    file_path: Path | None = None,
    snapshot: dict[str, Any] | None = None,
) -> DeliveryPolicy:
    """Resolve delivery mode for a project, with optional per-task note override.

    Precedence (highest first):
    1. Sprint- or run-layer ``delivery`` in the run's operating snapshot
       (task #1756; ``snapshot`` is ``operating_profiles.run_snapshot``)
    2. Task note ``[delivery: mode=… yolo=…]``
    3. Per-project entry in the delivery YAML
    4. Org- or unit-layer ``delivery`` in the run's operating snapshot
    5. YAML ``default`` block
    6. Built-in safe default (``no-mistakes``, yolo off)

    ``mode`` and ``yolo`` are layered independently (a note that omits
    ``yolo`` defers to the project entry, then the org/unit tier, else is
    off). Only an operator's ``[delivery: …]`` note counts: the executor's
    generated ``[delivery-run: …]`` stamp is ignored, so a finished run's
    policy never governs the next run. Without a snapshot the result is
    exactly the pre-#1756 resolution.
    """
    tiers: dict[str, dict[str, Any]] = {"high": {}, "low": {}}
    if snapshot is not None:
        from .operating_profiles import delivery_layers

        tiers = delivery_layers(snapshot)
    high, low = tiers["high"], tiers["low"]

    note_match = _NOTE_OVERRIDE_RE.search(notes) if notes else None
    layer: str | None = None
    data = load_delivery_registry(file_path)
    projects = data.get("projects") or {}
    has_entry = isinstance(projects, dict) and bool(project) and project in projects
    p_mode, p_yolo = _parse_entry(projects[project] if has_entry else None)
    if note_match:
        mode: DeliveryMode = _normalize_mode(note_match.group("mode")) or DEFAULT_MODE
        yolo_raw = note_match.group("yolo")
        if yolo_raw is not None:
            yolo = bool(_normalize_yolo(yolo_raw))
        elif p_yolo is not None:
            # A note that sets only ``mode`` leaves ``yolo`` to the next layers:
            # the project's own entry first, then the org/unit profile.
            yolo = p_yolo
        elif isinstance(low.get("yolo"), bool):
            yolo = low["yolo"]
        else:
            # Legacy implicit off when nothing else sets it.
            yolo = False
        source = "note"
    else:
        default_mode, default_yolo = _parse_entry(data.get("default"))
        mode = default_mode or DEFAULT_MODE
        yolo = bool(default_yolo) if default_yolo is not None else False
        source = "default"

        mode, yolo, applied = _apply_tier(mode, yolo, low)
        if applied:
            source, layer = "profile", low.get("layer")

        if has_entry:
            if p_mode is not None:
                mode = p_mode
            if p_yolo is not None:
                yolo = p_yolo
            source, layer = "file", None

    mode, yolo, applied = _apply_tier(mode, yolo, high)
    if applied:
        source, layer = "profile", high.get("layer")

    return DeliveryPolicy(mode=mode, yolo=yolo, project=project, source=source, layer=layer)


def annotate_notes(notes: str, policy: DeliveryPolicy) -> str:
    """Ensure notes carry exactly one generated ``[delivery-run: …]`` stamp for policy.

    An operator's ``[delivery: mode=…]`` override is left in place: it is the
    task's own instruction, not something a finished run may rewrite.
    """
    text = notes or ""
    marker = policy.marker()
    if text.count(marker) == 1 and len(_GENERATED_MARKER_RE.findall(text)) == 1:
        return text
    # Replace only marker tokens: the same line can carry a human explanation.
    body = _GENERATED_MARKER_RE.sub("", text).rstrip()
    if body:
        return f"{body}\n\n{marker}"
    return marker


def apply_finish_markers(notes: str, policy: DeliveryPolicy, *, fresh_review: bool = False) -> str:
    """Stamp delivery and review; a new executor handoff requests fresh review."""
    stamped = annotate_notes(notes, policy)
    if fresh_review or not _REVIEW_MARKER_LIVE.search(stamped or ""):
        stamped = stamped.rstrip() + f"\n\n{WAITING_FOR_REVIEW}"
    if policy.mode == "local-only" and "[delivery: local-branch]" not in stamped:
        stamped = stamped.rstrip() + "\n[delivery: local-branch] no remote PR expected"
    elif policy.expects_remote_pr() and "[delivery: prefer-pr]" not in stamped:
        stamped = stamped.rstrip() + "\n[delivery: prefer-pr] push + open PR before merge"
    return stamped
