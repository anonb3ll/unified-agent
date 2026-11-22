"""Thin gcloud inventory and ops tools for GCP slice-1 infrastructure (task #537).

Provides read-only inspection (project, compute instances, storage buckets, billing budgets)
and approval-gated operational actions (instance start/stop, bucket object listing).
Zero tokens, private keys, or credentials are ever returned.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, safety
from ..telemetry.redaction import (
    _SECRET_PATTERNS,
    ENV_CREDENTIAL_ASSIGNMENT_RE,
    PEM_PRIVATE_KEY_BLOCK_RE,
    redact_private_keys,
)

_ALLOWED_OPS_ACTIONS = frozenset(
    {
        "instance_describe",
        "instance_start",
        "instance_stop",
        "bucket_describe",
        "bucket_list_objects",
    }
)

_MUTATING_ACTIONS = frozenset(
    {
        "instance_start",
        "instance_stop",
    }
)

# Leading '-' rejected: gcloud would parse a value like "--help" as a flag
# rather than a resource name (argument injection, not shell injection —
# there's no shell involved, but it still changes what command runs).
_SAFE_IDENTIFIER_RE = re.compile(r"^(?!-)[a-zA-Z0-9_.:/*-]+$")
_SECRET_PATTERN_RE = re.compile(
    r"(bearer\s+[a-zA-Z0-9_\-.]+|ya29\.[a-zA-Z0-9_\-]+|private_key[^\r\n]+|\"private_key\":\s*\"[^\"]+\")",
    re.IGNORECASE,
)
# Multiline PEM blocks (e.g. embedded in startup-script metadata).
_PEM_BLOCK_RE = PEM_PRIVATE_KEY_BLOCK_RE
_SENSITIVE_METADATA_KEY_RE = re.compile(
    r"(secret|password|passwd|token|api[-_]?key|credential|auth|private)", re.IGNORECASE
)
# Keys whose values may pass through after pattern scrubbing. All other metadata
# keys are redacted entirely — operators stash arbitrary secrets in custom keys.
_SAFE_METADATA_KEYS = frozenset(
    {
        "enable-oslogin",
        "serial-port-enable",
        "shutdown-script",
        "ssh-keys",
        "startup-script",
    }
)
# Shell-style credential assignments in startup/shutdown scripts (e.g. DB_PASS=…).
_ENV_CREDENTIAL_ASSIGNMENT_RE = ENV_CREDENTIAL_ASSIGNMENT_RE


def _scrub_secret_patterns(value: str) -> str:
    """Apply shared telemetry secret patterns without truncating the value."""
    # Whole private keys (PEM, PGP, PuTTY), fail closed: see redact_private_keys.
    redacted = redact_private_keys(value)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    redacted = _ENV_CREDENTIAL_ASSIGNMENT_RE.sub("[REDACTED_SECRET]", redacted)
    return redacted


def _redact_instance_metadata(details: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-like values from instance metadata.items before return.

    Instance metadata is a `[{key, value}]` list that operators commonly use
    to stash arbitrary secrets (API keys, passwords, startup-script tokens).
    Redact by sensitive key name *and* scrub values matching known token
    patterns (so a secret embedded in `startup-script` still cannot leak).
    """
    metadata = details.get("metadata")
    if not isinstance(metadata, dict):
        return details
    items = metadata.get("items")
    if not isinstance(items, list):
        return details
    redacted_items = []
    for item in items:
        if not isinstance(item, dict):
            redacted_items.append(item)
            continue
        key = item.get("key", "")
        value = item.get("value")
        if isinstance(key, str) and _SENSITIVE_METADATA_KEY_RE.search(key):
            redacted_items.append({**item, "value": "[REDACTED_SECRET]"})
        elif isinstance(key, str) and key in _SAFE_METADATA_KEYS:
            if isinstance(value, str):
                redacted_items.append({**item, "value": _scrub_secret_patterns(value)})
            else:
                redacted_items.append(item)
        else:
            redacted_items.append({**item, "value": "[REDACTED_METADATA]"})
    details["metadata"] = {**metadata, "items": redacted_items}
    return details


def _validate_identifier(val: str | None, name: str) -> str | None:
    if val is None:
        return None
    val = val.strip()
    if not val:
        return None
    if not _SAFE_IDENTIFIER_RE.match(val):
        raise ToolError(
            f"invalid character in {name} {val!r}: must match [a-zA-Z0-9_.:/*-]+ "
            "and not start with '-'"
        )
    return val


def _resolve_target_project(project: str | None) -> str:
    """Resolve and validate the effective GCP project id (caller override or config default)."""
    validated = _validate_identifier(project or config.gcp_project(), "project")
    if not validated:
        raise ToolError(
            "configure UA_GCP_PROJECT (cloud.project); no default cloud project is selected"
        )
    return validated


def _resolve_gcloud_bin() -> str:
    bin_path = config.gcloud_bin()
    if shutil.which(bin_path) or (bin_path.startswith("/") and os.path.isfile(bin_path)):
        return bin_path
    found = shutil.which("gcloud")
    if found:
        return found
    raise ToolError(
        f"gcloud binary not found at {bin_path!r} or on PATH. "
        "Install Google Cloud SDK or configure UA_GCLOUD_BIN."
    )


def _run_gcloud_json(args: list[str], timeout_s: int = 30) -> tuple[Any, str | None]:
    """Run gcloud command and parse JSON output. Returns (parsed_json, error_message)."""
    try:
        bin_path = _resolve_gcloud_bin()
    except ToolError as exc:
        return None, str(exc)

    cmd = [bin_path] + args + ["--format=json"]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if res.returncode != 0:
            err = res.stderr.strip() or f"process exited with return code {res.returncode}"
            err = _SECRET_PATTERN_RE.sub("[REDACTED_SECRET]", err)
            return None, err
        stdout = res.stdout.strip()
        if not stdout:
            return {}, None
        sanitized = _SECRET_PATTERN_RE.sub("[REDACTED_SECRET]", stdout)
        return json.loads(sanitized), None
    except subprocess.TimeoutExpired:
        return None, f"gcloud command timed out after {timeout_s}s: {' '.join(args[:3])}..."
    except json.JSONDecodeError as exc:
        return None, f"failed to parse gcloud JSON output: {exc}"
    except OSError as exc:
        return None, f"could not execute gcloud: {exc}"


def _resolve_billing_account(project: str) -> tuple[str | None, str | None]:
    """Resolve the billing account actually linked to `project`.

    Returns (billing_account_id, error). Never hardcode a billing account —
    a `project` override that lives on a different account must query that
    project's own linked account, not silently miss/mismatch budgets.
    """
    data, err = _run_gcloud_json(["billing", "projects", "describe", project])
    if err:
        return None, f"could not resolve billing account for {project}: {err}"
    if not isinstance(data, dict):
        return None, f"unexpected billing info for {project}"
    acct_name = data.get("billingAccountName") or ""
    if not isinstance(acct_name, str) or not acct_name.startswith("billingAccounts/"):
        return None, f"project {project} has no linked billing account"
    return acct_name.split("/", 1)[1], None


def gcloud_inventory(
    project: str | None = None,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    """Read-only inventory of GCP project resources (project, instances, buckets, budgets).

    Args:
        project: GCP project ID (defaults to explicitly configured UA_GCP_PROJECT).
        sections: Optional list of sections to inspect: ['project', 'instances', 'buckets', 'budgets'].
                  Defaults to all sections.

    Returns:
        Structured dict with non-secret resource metadata and any encountered errors.
    """
    target_project = _resolve_target_project(project)
    valid_sections = {"project", "instances", "buckets", "budgets"}
    chosen_sections = set(sections) if sections else valid_sections
    if sections is not None:
        if not sections:
            raise ToolError(
                f"sections cannot be empty. Choose from: {', '.join(sorted(valid_sections))}"
            )
        unknown = chosen_sections - valid_sections
        if unknown:
            raise ToolError(
                f"unknown inventory section(s): {', '.join(sorted(unknown))}. "
                f"Choose from: {', '.join(sorted(valid_sections))}"
            )

    result: dict[str, Any] = {
        "status": "ok",
        "project_id": target_project,
        "project": None,
        "instances": [],
        "buckets": [],
        "budgets": [],
        "errors": [],
    }
    # Sections that completed without error. Truthiness of the result data
    # can't stand in for this: a real project can have zero instances, zero
    # buckets, or zero budgets, and an empty-but-successful section must not
    # read the same as a section that was never fetched or that failed.
    succeeded_sections: set[str] = set()

    # 1. Project details
    proj_number: str | None = None
    if "project" in chosen_sections:
        proj_data, proj_err = _run_gcloud_json(["projects", "describe", target_project])
        if proj_err:
            result["errors"].append({"section": "project", "error": proj_err})
        elif isinstance(proj_data, dict):
            succeeded_sections.add("project")
            if proj_data.get("projectNumber"):
                proj_number = str(proj_data["projectNumber"])
            result["project"] = {
                "projectId": proj_data.get("projectId"),
                "projectNumber": proj_data.get("projectNumber"),
                "name": proj_data.get("name"),
                "lifecycleState": proj_data.get("lifecycleState"),
                "createTime": proj_data.get("createTime"),
            }

    # 2. Compute instances
    if "instances" in chosen_sections:
        inst_data, inst_err = _run_gcloud_json(
            ["compute", "instances", "list", f"--project={target_project}"]
        )
        if inst_err:
            result["errors"].append({"section": "instances", "error": inst_err})
        elif isinstance(inst_data, list):
            succeeded_sections.add("instances")
            clean_instances = []
            for inst in inst_data:
                nics = inst.get("networkInterfaces", [])
                internal_ip = nics[0].get("networkIP") if nics else None
                ext_ips: list[str] = []
                for nic in nics:
                    for ac in nic.get("accessConfigs", []):
                        nat = ac.get("natIP")
                        if nat:
                            ext_ips.append(nat)
                    for ac in nic.get("ipv6AccessConfigs", []):
                        v6 = ac.get("externalIpv6")
                        if v6:
                            ext_ips.append(v6)
                machine_type = inst.get("machineType", "").split("/")[-1]
                zone = inst.get("zone", "").split("/")[-1]

                clean_instances.append(
                    {
                        "name": inst.get("name"),
                        "status": inst.get("status"),
                        "zone": zone,
                        "machineType": machine_type,
                        "internalIP": internal_ip,
                        "externalIP": ext_ips[0] if ext_ips else None,
                        "externalIPs": ext_ips,
                        "labels": inst.get("labels", {}),
                        "tags": inst.get("tags", {}).get("items", []),
                    }
                )
            result["instances"] = clean_instances

    # 3. Storage buckets
    if "buckets" in chosen_sections:
        bucket_data, bucket_err = _run_gcloud_json(
            ["storage", "buckets", "list", f"--project={target_project}"]
        )
        if bucket_err:
            result["errors"].append({"section": "buckets", "error": bucket_err})
        elif isinstance(bucket_data, list):
            succeeded_sections.add("buckets")
            clean_buckets = []
            for b in bucket_data:
                clean_buckets.append(
                    {
                        "name": b.get("name"),
                        "storage_url": b.get("storage_url"),
                        "location": b.get("location"),
                        "default_storage_class": b.get("default_storage_class"),
                        "versioning_enabled": b.get("versioning_enabled", False),
                    }
                )
            result["buckets"] = clean_buckets

    # 4. Billing budgets
    if "budgets" in chosen_sections:
        if not proj_number and "project" not in chosen_sections:
            # Resolve project number for target project to prevent leaking other projects'
            # budgets. Skipped when 'project' was also requested — that section's own
            # describe call already ran; re-running it here would just duplicate the
            # error below if it failed, or the request if it lacked a projectNumber.
            proj_data, proj_err = _run_gcloud_json(["projects", "describe", target_project])
            if isinstance(proj_data, dict) and proj_data.get("projectNumber"):
                proj_number = str(proj_data["projectNumber"])
            elif proj_err:
                result["errors"].append(
                    {
                        "section": "budgets",
                        "error": f"could not resolve project number for {target_project}: {proj_err}",
                    }
                )

        if not proj_number:
            # Without a project number no budget can ever pass the scoping filter
            # below — record that explicitly instead of silently returning an
            # empty, apparently-successful budgets list.
            if not any(e.get("section") == "budgets" for e in result["errors"]):
                result["errors"].append(
                    {
                        "section": "budgets",
                        "error": (
                            f"project number unavailable for {target_project}; "
                            "cannot scope budgets safely"
                        ),
                    }
                )
        else:
            billing_acct, billing_acct_err = _resolve_billing_account(target_project)
            if billing_acct_err:
                result["errors"].append({"section": "budgets", "error": billing_acct_err})
                billing_acct = None

            budgets_data, budget_err = (
                _run_gcloud_json(
                    ["billing", "budgets", "list", f"--billing-account={billing_acct}"]
                )
                if billing_acct
                else (None, None)
            )
            if budget_err:
                result["errors"].append({"section": "budgets", "error": budget_err})
            elif isinstance(budgets_data, list):
                succeeded_sections.add("budgets")
                clean_budgets = []
                for bg in budgets_data:
                    bg_projects = bg.get("budgetFilter", {}).get("projects", [])
                    # Only include budgets explicitly scoped to the targeted project,
                    # to prevent leaking cross-project budget data.
                    if f"projects/{proj_number}" not in bg_projects:
                        continue
                    clean_budgets.append(
                        {
                            "displayName": bg.get("displayName"),
                            "amount": bg.get("amount", {}).get("specifiedAmount", {}),
                            "calendarPeriod": bg.get("budgetFilter", {}).get("calendarPeriod"),
                            "thresholdRules": bg.get("thresholdRules", []),
                        }
                    )
                result["budgets"] = clean_budgets

    # Reconcile every requested section against what actually succeeded.
    #
    # A section can fall through both branches above without leaving a trace:
    # `_run_gcloud_json` returns ({}, None) for an empty stdout, and a list
    # command's `isinstance(data, list)` guard then rejects that dict without
    # recording an error. The section silently reports zero resources and the
    # overall status stays "ok".
    #
    # That produced a real false-healthy against the live worker: a hub
    # credential without compute.instances.list returned
    # {"status": "ok", "instances": [], "errors": []} while the configured worker was
    # RUNNING. Health checks and self-healing keyed on this surface cannot be
    # allowed to read "not permitted to see it" as "it isn't there".
    for section in sorted(chosen_sections - succeeded_sections):
        if not any(e.get("section") == section for e in result["errors"]):
            result["errors"].append(
                {
                    "section": section,
                    "error": (
                        f"section {section!r} returned no usable data and reported no "
                        "error (unexpected gcloud response shape, or insufficient "
                        "permission); treating as unavailable rather than empty"
                    ),
                }
            )

    # Per-section outcome, so a caller can tell "zero resources" from "could
    # not look". Truthiness of result[section] cannot carry this: an empty
    # list is a legitimate success.
    result["sections"] = {
        section: ("ok" if section in succeeded_sections else "unavailable")
        for section in sorted(chosen_sections)
    }

    # Status is derived from coverage, not from the error list alone. A run
    # where nothing succeeded is "error" even if no section managed to report
    # why, and "ok" now means every requested section was actually read.
    if not succeeded_sections:
        result["status"] = "error"
    elif succeeded_sections < chosen_sections:
        result["status"] = "degraded"
    else:
        result["status"] = "ok"

    return result


def classify_gcp_Agent20_health(inventory: Any, worker_name: str | None = None) -> dict[str, str]:
    """Classify worker health only from a successful, read-only instances inventory.

    An approval-gated ``gcloud_ops`` call proves that a mutation was reviewed,
    not that the cloud integration or worker is healthy. Unavailable inventory
    (including IAM permission denial) is degraded rather than an empty healthy
    project. The input is the normal ``gcloud_inventory(sections=["instances"])``
    result so callers can test this boundary without invoking gcloud.
    """
    try:
        _, configured_worker = _configured_cloud_target()
        worker_name = _validate_identifier(worker_name, "worker") or configured_worker
    except ToolError as exc:
        return {"status": "UNKNOWN", "detail": str(exc)}

    if not isinstance(inventory, dict):
        return {"status": "UNKNOWN", "detail": "instances inventory returned an invalid result"}

    sections = inventory.get("sections")
    if not isinstance(sections, dict) or sections.get("instances") != "ok":
        errors = inventory.get("errors")
        reason = "instances inventory unavailable"
        if isinstance(errors, list):
            matching = [
                str(row.get("error"))
                for row in errors
                if isinstance(row, dict) and row.get("section") == "instances" and row.get("error")
            ]
            if matching:
                reason = matching[0][:200]
        return {"status": "DEGRADED", "detail": reason}

    instances = inventory.get("instances")
    if not isinstance(instances, list) or any(not isinstance(row, dict) for row in instances):
        return {
            "status": "UNKNOWN",
            "detail": "instances inventory returned an invalid instance list",
        }

    matches = [row for row in instances if row.get("name") == worker_name]
    if len(matches) > 1:
        return {"status": "UNKNOWN", "detail": f"{worker_name} is ambiguous in readable inventory"}
    worker = matches[0] if matches else None
    if worker is None:
        return {
            "status": "DEGRADED",
            "detail": f"{worker_name} was not found in readable inventory",
        }
    state = str(worker.get("status") or "UNKNOWN").upper()
    if state == "RUNNING":
        return {"status": "LIVE", "detail": f"{worker_name} is RUNNING in readable inventory"}
    return {"status": "DEGRADED", "detail": f"{worker_name} is {state} in readable inventory"}


def _configured_cloud_target() -> tuple[str, str]:
    """Require the server's project and worker before any cloud probe."""
    project = _resolve_target_project(None)
    worker = _validate_identifier(config.gcp_worker(), "worker")
    if not worker:
        raise ToolError("configure UA_GCP_WORKER (cloud.worker); no default worker is selected")
    return project, worker


def gcp_Agent20_health() -> dict[str, str]:
    """Return read-only health for an explicitly configured project and worker."""
    try:
        project, worker = _configured_cloud_target()
    except ToolError as exc:
        return {"status": "UNKNOWN", "detail": str(exc)}
    try:
        inventory = gcloud_inventory(project=project, sections=["instances"])
    except Exception as exc:  # noqa: BLE001 - doctor/pane expose health, never raise
        return {"status": "DEGRADED", "detail": f"instances inventory failed: {str(exc)[:200]}"}
    return classify_gcp_Agent20_health(inventory, worker)


def gcloud_ops(
    action: str,
    target: str,
    project: str | None = None,
    zone: str | None = None,
) -> dict[str, Any]:
    """Execute bounded GCP operations against infrastructure.

    Only mutating actions (`instance_start`, `instance_stop`) require approval;
    read-only inspections (`instance_describe`, `bucket_describe`,
    `bucket_list_objects`) run without one.

    Args:
        action: Operation to perform. Allowed:
                - 'instance_describe': Inspect a VM instance (read-only)
                - 'instance_start': Start a VM instance (approval-gated)
                - 'instance_stop': Stop a VM instance (approval-gated)
                - 'bucket_describe': Describe a GCS storage bucket (read-only)
                - 'bucket_list_objects': List objects under a bucket or prefix (read-only)
        target: Name of the instance or bucket (e.g. 'example-worker' or 'example-backups')
        project: Optional project override (defaults to configured UA_GCP_PROJECT)
        zone: Optional zone for compute instance operations (defaults to 'us-central1-a')

    Returns:
        Structured dict with the operation outcome. Zero credentials or tokens in output.
    """
    if action not in _ALLOWED_OPS_ACTIONS:
        raise ToolError(
            f"action {action!r} is not allowed. Choose from: {', '.join(sorted(_ALLOWED_OPS_ACTIONS))}"
        )

    target_clean = _validate_identifier(target, "target")
    if not target_clean:
        raise ToolError("target cannot be empty")

    target_project = _resolve_target_project(project)
    target_zone = _validate_identifier(zone, "zone") or "us-central1-a"

    # Evaluate safety policy gate
    safety.check_policy_gate(
        "gcloud_ops",
        {
            "action": action,
            "target": target_clean,
            "project": target_project,
            "zone": target_zone,
        },
    )

    # Evaluate approval gate for mutating actions only (task #62 · C).
    # Read-only inspections (describe, list) proceed without a one-shot pass.
    if action in _MUTATING_ACTIONS:
        from .approvals import check_approval_gate

        check_approval_gate(
            "gcloud_ops",
            {
                "action": action,
                "target": target_clean,
                "project": target_project,
                "zone": target_zone,
            },
            requested_by=config.verified_agent_identity(),
        )

    if action == "instance_describe":
        data, err = _run_gcloud_json(
            [
                "compute",
                "instances",
                "describe",
                target_clean,
                f"--project={target_project}",
                f"--zone={target_zone}",
            ]
        )
        if err:
            raise ToolError(f"instance_describe failed: {err}")
        if isinstance(data, dict):
            data = _redact_instance_metadata(data)
        return {"status": "ok", "action": action, "target": target_clean, "details": data}

    elif action == "instance_start":
        data, err = _run_gcloud_json(
            [
                "compute",
                "instances",
                "start",
                target_clean,
                f"--project={target_project}",
                f"--zone={target_zone}",
            ]
        )
        if err:
            raise ToolError(f"instance_start failed: {err}")
        return {"status": "ok", "action": action, "target": target_clean, "result": data}

    elif action == "instance_stop":
        data, err = _run_gcloud_json(
            [
                "compute",
                "instances",
                "stop",
                target_clean,
                f"--project={target_project}",
                f"--zone={target_zone}",
            ]
        )
        if err:
            raise ToolError(f"instance_stop failed: {err}")
        return {"status": "ok", "action": action, "target": target_clean, "result": data}

    elif action == "bucket_describe":
        bucket_name = target_clean.replace("gs://", "").rstrip("/")
        data, err = _run_gcloud_json(
            [
                "storage",
                "buckets",
                "describe",
                f"gs://{bucket_name}",
                f"--project={target_project}",
            ]
        )
        if err:
            raise ToolError(f"bucket_describe failed: {err}")
        return {"status": "ok", "action": action, "target": bucket_name, "details": data}

    elif action == "bucket_list_objects":
        bucket_target = target_clean if target_clean.startswith("gs://") else f"gs://{target_clean}"
        data, err = _run_gcloud_json(
            [
                "storage",
                "ls",
                bucket_target,
                f"--project={target_project}",
            ]
        )
        if err:
            raise ToolError(f"bucket_list_objects failed: {err}")
        return {"status": "ok", "action": action, "target": bucket_target, "objects": data}

    raise ToolError(f"unhandled action {action!r}")
