"""Reviewed starter teams bound to existing roster and revisioned team specs.

The binding contains no executable definitions or permission changes. Workflows
are reconstructed only from the hash-pinned resource shipped with this build.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import config, config_revisions, operating_profiles, team_spec, trust_presets
from .team_policy import TeamSpecError

IDS = ("implementation-pair", "adversarial-review", "conveyor", "research-team")
PREFIX = "starter-"


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def definition(template_id):
    if not isinstance(template_id, str) or template_id not in IDS:
        raise TeamSpecError(f"unknown starter template; choose one of {IDS}")
    data = yaml.safe_load(
        (Path(__file__).parent / "starter_teams" / f"{template_id}.yaml").read_text()
    )
    return {**data, "content_hash": _hash(data)}


def catalog():
    return [definition(name) for name in IDS]


def _bound(template, team_id, bindings, operating_profile, trust_preset):
    if not isinstance(bindings, dict) or set(bindings) != set(template["roles"]):
        raise TeamSpecError("bindings must name every template role exactly once")
    if not isinstance(trust_preset, str) or trust_preset not in trust_presets.PRESETS:
        raise TeamSpecError("select an existing configured trust preset")
    cfg = operating_profiles.load_config()
    if not isinstance(operating_profile, str) or operating_profile not in cfg.profiles:
        raise TeamSpecError("select an existing operating profile")
    seats = []
    role_labels = {}
    for role, guidance in template["roles"].items():
        raw = bindings[role]
        if not isinstance(raw, dict) or set(raw) - {
            "label",
            "harness",
            "model",
            "host",
            "workspace",
        }:
            raise TeamSpecError(f"invalid binding for {role}")
        seats.append({**raw, "guidance": guidance})
        role_labels[role] = raw.get("label")
    body = {
        "description": template["description"],
        "guidance": template["guidance"],
        "operating_profile": operating_profile,
        "seats": seats,
        "edges": [
            {
                "from": role_labels[list(template["roles"])[0]],
                "to": role_labels[role],
                "type": "delegates_to" if "lead" in template["roles"] else "collaborates_with",
            }
            for role in list(template["roles"])[1:]
        ],
        "starter_template": {
            "id": template["id"],
            "version": template["version"],
            "content_hash": template["content_hash"],
            "roles": role_labels,
            "trust_preset": trust_preset,
        },
    }
    if template["id"] == "implementation-pair":
        body["edges"].append(
            {"from": role_labels["checker"], "to": role_labels["builder"], "type": "reviews"}
        )
    parsed = team_spec.resolve(team_id, body)
    problems = team_spec.roster_findings(parsed)
    if problems:
        raise TeamSpecError(problems[0]["detail"])
    for seat in parsed["seats"]:
        if trust_presets.selection(seat["label"]) != trust_preset:
            raise TeamSpecError(
                f"{seat['label']} is not already bound to trust preset {trust_preset!r}"
            )
    independent = template["independent_roles"]
    harnesses = {seat["label"]: seat["harness"] for seat in parsed["seats"]}
    if independent and len({harnesses[role_labels[r]] for r in independent}) != len(independent):
        raise TeamSpecError("independent review roles must use different harnesses")
    return body, parsed


def prepare(template_id, team_id, content_hash, bindings, operating_profile, trust_preset):
    template = definition(template_id)
    if content_hash != template["content_hash"]:
        raise TeamSpecError("starter template hash changed; refresh the catalog")
    body, parsed = _bound(template, team_id, bindings, operating_profile, trust_preset)
    return body, parsed


def preview(**params):
    from . import skill_catalog

    with skill_catalog._governed(
        "skill_registry", {"action": "starter_preview", "params": params}, read_only=True
    ):
        _body, parsed = prepare(**params)
        return {
            "spec": parsed,
            "workflow": workflow_for(parsed),
            "plan": team_spec.plan_spec(parsed, actor=config.verified_agent_identity()),
        }


def install(**params):
    from . import skill_catalog
    from .tools import approvals

    skill_catalog.require_tool("skill_registry")
    skill_catalog.require_tool("config_revision_manage")
    payload = {"action": "starter_install", "params": params}
    with (
        skill_catalog._governed("config_revision_manage", payload, join_action=True),
        config_revisions._lock,
    ):
        body, parsed = prepare(**params)
        mapping, raw = config_revisions._read_policy_file()
        if not raw:
            mapping = config_revisions._default_policy_document()
        base = config_revisions._raw_sha(raw)
        planned = copy.deepcopy(mapping)
        planned.setdefault("teams", {})[parsed["team"]] = body
        config_revisions._validate_policy_text(config_revisions._dump(planned))
        if config_revisions._policy_raw_sha() != base:
            raise ToolError("live policy changed during validation; preview and install again")
        if _hash(mapping) == _hash(planned):
            return {"installed": False, "unchanged": True, "team": parsed["team"]}
        approvals.check_approval_gate(
            config_revisions.COMMIT_TOOL,
            {
                "kind": "policy",
                "starter_template": parsed["starter_template"],
                "document_sha256": _hash(planned),
                "base_sha256": base,
            },
            requested_by=config.verified_agent_identity(),
            force=True,
        )
        if config_revisions._policy_raw_sha() != base:
            raise ToolError("live policy changed during approval; preview and install again")
        # Recheck roster, profile and trust after the approval hook.
        prepare(**params)
        try:
            result = config_revisions.commit("policy", planned, expected_base_sha256=base)
        except config_revisions.ConfigFanoutError as exc:
            exc.result.update(installed=True, team=parsed["team"])
            raise
        return {
            **result,
            "installed": True,
            "team": parsed["team"],
            "workflow": PREFIX + parsed["team"],
            "spec_sha256": team_spec.spec_sha256(parsed),
        }


def validate_installed(spec):
    binding = spec["starter_template"]
    seats = {s["label"]: s for s in spec["seats"]}
    bindings = {
        role: {k: v for k, v in seats[label].items() if k not in {"guidance", "skills"}}
        for role, label in binding["roles"].items()
    }
    prepare(
        template_id=binding["id"],
        team_id=spec["team"],
        content_hash=binding["content_hash"],
        bindings=bindings,
        operating_profile=spec["operating_profile"],
        trust_preset=binding["trust_preset"],
    )


def workflow_for(spec):
    binding = spec.get("starter_template")
    if not binding:
        raise TeamSpecError("team has no installed starter workflow")
    validate_installed(spec)
    template = definition(binding["id"])
    if (
        binding["version"] != template["version"]
        or binding["content_hash"] != template["content_hash"]
    ):
        raise TeamSpecError("installed starter differs from shipped template; review and reinstall")
    roles = binding["roles"]
    if set(roles) != set(template["roles"]):
        raise TeamSpecError("installed starter has incomplete role bindings")
    seats = {seat["label"]: seat for seat in spec["seats"]}
    independent = template["independent_roles"]
    if independent and len({seats[roles[r]]["harness"] for r in independent}) != len(independent):
        raise TeamSpecError("independent review roles must use different harnesses")
    steps = copy.deepcopy(template["steps"])
    for step in steps:
        lane = step["args"].get("lane", "")
        if lane.startswith("$"):
            step["args"]["lane"] = roles[lane[1:]]
    return {
        "name": PREFIX + spec["team"],
        "description": template["description"],
        "inputs": ["objective"] if template["id"] == "research-team" else ["objective", "head"],
        "roles": {
            r: {"team": spec["team"], "preferred_seats": [label]} for r, label in roles.items()
        },
        "steps": steps,
    }


def bind_inputs(spec, inputs):
    """Freeze mission/base context; builder evidence supplies the resulting review head."""
    objective = inputs.get("objective")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
        raise TeamSpecError("starter objective must contain 1 to 4000 characters")
    context = "Mission: " + objective.strip()
    args_context = "Mission: {{ inputs.objective }}"
    if "head" in spec["inputs"]:
        head = inputs.get("head")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
            raise TeamSpecError("starter head must be the full immutable commit SHA")
        label = (
            "Starting commit (context)"
            if any(s.get("handoff_commit") for s in spec["steps"])
            else "Exact review head"
        )
        context += "\n" + label + ": " + head
        args_context += "\n" + label + ": " + head
    result = copy.deepcopy(spec)
    for step in result["steps"]:
        step["objective"] += "\n\n" + context
        if step["tool"] == "task_add":
            # Substitution inserts input values once; raw mission text here
            # would reparse the user's literal {{ ... }} as workflow syntax.
            step["args"]["notes"] += "\n\n" + args_context
    return result


def workflow_definition(name):
    return workflow_for(team_spec.resolve(name[len(PREFIX) :]))


def workflow_names():
    return [
        PREFIX + team
        for team, spec in team_spec.load_specs().items()
        if spec.get("starter_template")
    ]
