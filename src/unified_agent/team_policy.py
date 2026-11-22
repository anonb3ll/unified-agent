"""Team spec policy: parse and validate the ``teams:`` block (task #1736).

Core layer. ``config_revisions`` validates every policy commit, rollback and
capture through :func:`validate_policy_text`, and ``team_spec`` (services)
plans and boots from :func:`load_specs`. Nothing here reaches above core:
the harness vocabulary is a local tuple kept equal to
``acp_matrix.ACP_HARNESSES`` by a test, and reads of the live policy file obey
the same size limit as config revisions and are cached by file stat.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from typing import Any

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import config, org_registry

# Kept equal to acp_matrix.ACP_HARNESSES (tests/test_team_spec.py checks it).
# A local copy keeps this core module free of the runtime launch matrix.
HARNESSES = ("gemini", "codex", "claude", "copilot", "cursor", "opencode", "hermes")
# Same limit as config_revisions.MAX_RAW_BYTES (checked by a test).
MAX_POLICY_BYTES = 256_000

BLOCK = "teams"
EDGE_TYPES = ("delegates_to", "reviews", "can_observe", "collaborates_with", "escalates_to")
WORKTREE_POLICIES = ("task", "shared")
MAX_SEATS = 16
MAX_SKILLS = 16
MAX_GUIDANCE = 4000
MAX_EDGES = 64

_LABEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")
_SKILL_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_PROFILE_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REPO_RE = re.compile(r"[A-Za-z0-9._/@+-]{1,512}")

_TEAM_KEYS = {
    "starter_template",
    "description",
    "operating_profile",
    "mission_id",
    "guidance",
    "skills",
    "seats",
    "edges",
}
_SEAT_KEYS = {"label", "harness", "model", "host", "workspace", "guidance", "skills"}
_WORKSPACE_KEYS = {"repo", "worktree"}
_EDGE_KEYS = {"from", "to", "type"}


class TeamSpecError(ToolError):
    """A team spec failed validation; nothing was planned or launched."""


# ---------------------------------------------------------------------------
# Parsing (fails closed on unknown keys and values)
# ---------------------------------------------------------------------------


def _mapping(raw: object, where: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TeamSpecError(f"{where} must be a mapping")
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise TeamSpecError(f"{where} has unknown key(s): {', '.join(unknown)}")
    return raw


def _text(raw: object, where: str, limit: int) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TeamSpecError(f"{where} must be a string")
    text = raw.strip()
    if len(text) > limit:
        raise TeamSpecError(f"{where} exceeds {limit} characters")
    return text or None


def _token(raw: object, where: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(raw, str) or not pattern.fullmatch(raw.strip()):
        raise TeamSpecError(f"{where} is invalid: {raw!r}")
    return raw.strip()


def _skills(raw: object, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TeamSpecError(f"{where} must be a list of skill names")
    if len(raw) > MAX_SKILLS:
        raise TeamSpecError(f"{where} allows at most {MAX_SKILLS} skills")
    names = [_token(name, f"{where} entry", _SKILL_RE) for name in raw]
    if len(set(names)) != len(names):
        raise TeamSpecError(f"{where} repeats a skill")
    return names


def _workspace(raw: object, where: str) -> dict[str, Any]:
    if raw is None:
        return {"repo": None, "worktree": "task"}
    data = _mapping(raw, where, _WORKSPACE_KEYS)
    repo = data.get("repo")
    if repo is not None:
        if isinstance(repo, str) and (repo.strip().startswith("~") or ".." in repo.split("/")):
            raise TeamSpecError(
                f"{where}.repo must be absolute or relative to a node worktree root, "
                "without '~' or '..'"
            )
        repo = _token(repo, f"{where}.repo", _REPO_RE)
        if ".." in repo.split("/"):
            raise TeamSpecError(
                f"{where}.repo must be absolute or relative to a node worktree root, "
                "without '~' or '..'"
            )
    worktree = data.get("worktree", "task")
    if worktree not in WORKTREE_POLICIES:
        raise TeamSpecError(f"{where}.worktree must be one of {WORKTREE_POLICIES}")
    return {"repo": repo, "worktree": worktree}


def _seat(raw: object, where: str) -> dict[str, Any]:
    data = _mapping(raw, where, _SEAT_KEYS)
    label = _token(data.get("label"), f"{where}.label", _LABEL_RE)
    harness = str(data.get("harness") or "").strip().lower()
    if harness not in HARNESSES:
        raise TeamSpecError(
            f"{where}.harness must be one of {sorted(HARNESSES)}, got {data.get('harness')!r}"
        )
    model = data.get("model")
    host = data.get("host")
    return {
        "label": label,
        "harness": harness,
        "model": None if model is None else _token(model, f"{where}.model", _MODEL_RE),
        "host": None if host is None else _token(host, f"{where}.host", _HOST_RE),
        "workspace": _workspace(data.get("workspace"), f"{where}.workspace"),
        "guidance": _text(data.get("guidance"), f"{where}.guidance", MAX_GUIDANCE),
        "skills": _skills(data.get("skills"), f"{where}.skills"),
    }


def _edges(raw: object, where: str, seat_labels: list[str]) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TeamSpecError(f"{where} must be a list")
    if len(raw) > MAX_EDGES:
        raise TeamSpecError(f"{where} allows at most {MAX_EDGES} edges")
    seats = set(seat_labels)
    out: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        at = f"{where}[{index}]"
        data = _mapping(item, at, _EDGE_KEYS)
        kind = data.get("type")
        if kind not in EDGE_TYPES:
            raise TeamSpecError(f"{at}.type must be one of {EDGE_TYPES}")
        source = _token(data.get("from"), f"{at}.from", _LABEL_RE)
        target = _token(data.get("to"), f"{at}.to", _LABEL_RE)
        if source not in seats:
            raise TeamSpecError(f"{at}.from {source!r} is not a seat on this team")
        # Escalation may leave the team (to a lead or the operator); every
        # other edge connects two seats of this team.
        if kind != "escalates_to" and target not in seats:
            raise TeamSpecError(f"{at}.to {target!r} is not a seat on this team")
        if source == target:
            raise TeamSpecError(f"{at} connects {source!r} to itself")
        edge = {"from": source, "to": target, "type": kind}
        if edge in out:
            raise TeamSpecError(f"{at} repeats an edge")
        out.append(edge)
    return out


def launch_order(seat_labels: list[str], edges: list[dict[str, str]]) -> list[str]:
    """Seats in boot order: a seat starts after every seat it delegates to.

    Delegates come up first so a delegating seat never hands work to a seat
    that is not running yet. Ties keep spec order. A ``delegates_to`` cycle is
    refused, since no order satisfies it.
    """
    waits_on: dict[str, set[str]] = {label: set() for label in seat_labels}
    for edge in edges:
        if edge["type"] == "delegates_to":
            waits_on[edge["from"]].add(edge["to"])
    order: list[str] = []
    started: set[str] = set()
    while len(order) < len(seat_labels):
        ready = [
            label for label in seat_labels if label not in started and waits_on[label] <= started
        ]
        if not ready:
            stuck = [label for label in seat_labels if label not in started]
            raise TeamSpecError(f"delegates_to edges form a cycle among {', '.join(stuck)}")
        order.append(ready[0])
        started.add(ready[0])
    return order


def _starter(raw, labels):
    if raw is None:
        return None
    data = _mapping(
        raw, "starter_template", {"id", "version", "content_hash", "roles", "trust_preset"}
    )
    template_id = _token(data.get("id"), "starter_template.id", _PROFILE_RE)
    if type(data.get("version")) is not int or data["version"] != 1:
        raise TeamSpecError("starter_template.version must be 1")
    digest = _token(
        data.get("content_hash"), "starter_template.content_hash", re.compile(r"[0-9a-f]{64}")
    )
    roles = data.get("roles")
    if not isinstance(roles, dict) or not roles or len(roles) > MAX_SEATS:
        raise TeamSpecError("starter_template.roles must be a non-empty bounded mapping")
    for role, label in roles.items():
        _token(role, "starter_template role", _PROFILE_RE)
        if label not in labels:
            raise TeamSpecError("starter_template role must bind a team seat")
    if len(set(roles.values())) != len(roles):
        raise TeamSpecError("starter_template roles require distinct seats")
    preset = data.get("trust_preset")
    if not isinstance(preset, str) or preset not in {
        "inspection",
        "configured-workspace",
        "active-worktree",
    }:
        raise TeamSpecError("starter_template requires a configured trust preset")
    return {
        "id": template_id,
        "version": 1,
        "content_hash": digest,
        "roles": dict(roles),
        "trust_preset": preset,
    }


def parse_spec(team_id: object, raw: object) -> dict[str, Any]:
    """Validate one spec's shape. Roster and live checks happen in the plan."""
    team = _token(team_id, "team id", re.compile(org_registry.OFFICE_ID_PATTERN))
    where = f"teams.{team}"
    data = _mapping(raw, where, _TEAM_KEYS)
    seats_raw = data.get("seats")
    if not isinstance(seats_raw, list) or not seats_raw:
        raise TeamSpecError(f"{where}.seats must be a non-empty list")
    if len(seats_raw) > MAX_SEATS:
        raise TeamSpecError(f"{where}.seats allows at most {MAX_SEATS} seats")
    seats = [_seat(item, f"{where}.seats[{index}]") for index, item in enumerate(seats_raw)]
    labels = [seat["label"] for seat in seats]
    if len(set(labels)) != len(labels):
        raise TeamSpecError(f"{where}.seats repeats a label; one seat per roster label")
    edges = _edges(data.get("edges"), f"{where}.edges", labels)
    mission = data.get("mission_id")
    if mission is not None and (isinstance(mission, bool) or not isinstance(mission, int)):
        raise TeamSpecError(f"{where}.mission_id must be an integer goal id")
    if mission is not None and mission <= 0:
        raise TeamSpecError(f"{where}.mission_id must be positive")
    profile = data.get("operating_profile")
    spec = {
        "team": team,
        "description": _text(data.get("description"), f"{where}.description", 500),
        "operating_profile": None
        if profile is None
        else _token(profile, f"{where}.operating_profile", _PROFILE_RE),
        "mission_id": mission,
        "guidance": _text(data.get("guidance"), f"{where}.guidance", MAX_GUIDANCE),
        "skills": _skills(data.get("skills"), f"{where}.skills"),
        "seats": seats,
        "edges": edges,
    }
    if data.get("starter_template") is not None:
        spec["starter_template"] = _starter(data["starter_template"], labels)
    spec["launch_order"] = launch_order(labels, edges)
    return spec


def parse_block(raw: object) -> dict[str, dict[str, Any]]:
    """Parse the ``teams:`` block of a policy mapping (missing means no teams)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TeamSpecError("teams must be a mapping of office id to team spec")
    return {str(team): parse_spec(team, spec) for team, spec in raw.items()}


def validate_policy_text(text: str) -> None:
    """Validate the ``teams:`` block of a candidate policy file, roster included.

    ``config_revisions`` calls this before a policy commit or rollback lands,
    so a committed spec always names real offices and roster labels.
    """
    data = yaml.safe_load(text) if text.strip() else {}
    if not isinstance(data, dict):
        return
    specs = parse_block(data.get(BLOCK))
    if not specs:
        return
    offices = org_registry.load_registry()
    for spec in specs.values():
        problems = roster_findings(spec, offices)
        if problems:
            raise TeamSpecError(f"teams.{spec['team']}: {problems[0]['detail']}")


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "specs": {}}


def load_specs() -> dict[str, dict[str, Any]]:
    """Every committed team spec in the live policy file, keyed by office id.

    The file is refused above ``MAX_POLICY_BYTES`` before it is parsed, and a
    parsed result is reused while the file's size and mtime are unchanged, so
    repeated team reads do not re-parse a large drifted file.
    """
    path = config.policy_file_path()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise TeamSpecError(f"cannot read teams from {path}: {exc}") from exc
    if stat.st_size > MAX_POLICY_BYTES:
        raise TeamSpecError(
            f"policy file {path} exceeds {MAX_POLICY_BYTES} bytes; teams are not read"
        )
    key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino)
    with _cache_lock:
        if _cache["key"] == key:
            return copy.deepcopy(_cache["specs"])
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TeamSpecError(f"cannot read teams from {path}: {exc}") from exc
    if len(raw) > MAX_POLICY_BYTES:
        raise TeamSpecError(
            f"policy file {path} exceeds {MAX_POLICY_BYTES} bytes; teams are not read"
        )
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise TeamSpecError(f"cannot read teams from {path}: {exc}") from exc
    specs = parse_block(data.get(BLOCK)) if isinstance(data, dict) else {}
    # The key came from a stat taken before the read. Publish it only when the
    # file still has that identity afterwards; a writer that replaced it in
    # between left bytes we cannot prove belong to ``key``, and a later
    # rollback to the old signature must not serve them.
    try:
        after = path.stat()
        settled = key == (str(path), after.st_size, after.st_mtime_ns, after.st_ino)
    except OSError:
        settled = False
    if settled:
        with _cache_lock:
            _cache["key"] = key
            _cache["specs"] = specs
    return copy.deepcopy(specs)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def spec_sha256(spec: dict[str, Any]) -> str:
    return _sha({key: value for key, value in spec.items() if key != "launch_order"})


def roster_findings(
    spec: dict[str, Any], offices: list[dict[str, Any]] | None = None
) -> list[dict[str, str]]:
    """Refusals for an office or label the org chart does not know."""
    rows = offices if offices is not None else org_registry.load_registry()
    by_id = {office["id"]: office for office in rows}
    roster = {label for office in rows for label in office["members"]}
    out: list[dict[str, str]] = []
    if spec["team"] not in by_id:
        out.append(
            {
                "seat": "",
                "code": "unknown_office",
                "detail": f"team id {spec['team']!r} is not an org.yaml office",
            }
        )
    for seat in spec["seats"]:
        if seat["label"] not in roster:
            out.append(
                {
                    "seat": seat["label"],
                    "code": "unknown_label",
                    "detail": f"{seat['label']!r} is not an org.yaml roster label",
                }
            )
    for edge in spec["edges"]:
        if edge["type"] == "escalates_to" and edge["to"] not in roster:
            out.append(
                {
                    "seat": edge["from"],
                    "code": "unknown_label",
                    "detail": f"escalation target {edge['to']!r} is not an org.yaml roster label",
                }
            )
    return out
