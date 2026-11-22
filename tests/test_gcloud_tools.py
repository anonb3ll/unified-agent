"""Tests for gcloud_inventory and gcloud_ops tools (task #537)."""

from unittest.mock import MagicMock, patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.gcloud import (
    _run_gcloud_json,
    _validate_identifier,
    classify_gcp_Agent20_health,
    gcloud_inventory,
    gcloud_ops,
)


@pytest.fixture(autouse=True)
def _explicit_cloud_configuration(monkeypatch):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")


def test_validate_identifier():
    assert _validate_identifier("example-worker", "project") == "example-worker"
    assert _validate_identifier("gs://my-bucket/prefix", "target") == "gs://my-bucket/prefix"
    assert _validate_identifier(None, "project") is None
    assert _validate_identifier("   ", "project") is None

    with pytest.raises(ToolError, match="invalid character in project"):
        _validate_identifier("example-worker; rm -rf /", "project")


def test_validate_identifier_rejects_leading_dash():
    """A leading '-' would be parsed by gcloud as a flag, not a resource name."""
    with pytest.raises(ToolError, match="not start with"):
        _validate_identifier("--help", "target")
    with pytest.raises(ToolError, match="not start with"):
        _validate_identifier("-q", "zone")


def test_gcloud_inventory_validates_configured_default_project(monkeypatch):
    """UA_GCP_PROJECT must pass the same identifier allowlist as an explicit project."""
    monkeypatch.setenv("UA_GCP_PROJECT", "--help")
    with pytest.raises(ToolError, match="not start with"):
        gcloud_inventory()


def test_gcloud_inventory_mocked():
    sample_project = {
        "projectId": "example-worker",
        "projectNumber": "123456789012",
        "name": "example-worker",
        "lifecycleState": "ACTIVE",
        "createTime": "2026-08-21T15:53:06.674Z",
    }
    sample_instances = [
        {
            "name": "example-worker",
            "status": "RUNNING",
            "zone": "https://www.googleapis.com/compute/v1/projects/example-worker/zones/us-central1-a",
            "machineType": "https://www.googleapis.com/compute/v1/projects/example-worker/zones/us-central1-a/machineTypes/e2-micro",
            "networkInterfaces": [
                {
                    "networkIP": "192.0.2.10",
                    "accessConfigs": [],
                }
            ],
            "labels": {"role": "ua-tailscale-worker"},
            "tags": {"items": ["tailscale"]},
        }
    ]
    sample_buckets = [
        {
            "name": "example-worker-backups-123456789012",
            "storage_url": "gs://example-worker-backups-123456789012/",
            "location": "US-CENTRAL1",
            "default_storage_class": "STANDARD",
            "versioning_enabled": True,
        }
    ]
    sample_budgets = [
        {
            "displayName": "example-worker-10",
            "amount": {"specifiedAmount": {"currencyCode": "USD", "units": "10"}},
            "budgetFilter": {
                "calendarPeriod": "MONTH",
                "projects": ["projects/123456789012"],
            },
            "thresholdRules": [{"thresholdPercent": 0.5}],
        }
    ]

    def mock_run_json(args, timeout_s=30):
        if args[:2] == ["billing", "projects"]:
            return {"billingAccountName": "billingAccounts/AAAAAA-BBBBBB-CCCCCC"}, None
        if "projects" in args:
            return sample_project, None
        if "instances" in args:
            return sample_instances, None
        if "buckets" in args:
            return sample_buckets, None
        if "budgets" in args:
            return sample_budgets, None
        return {}, None

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker")
        assert inv["status"] == "ok"
        assert inv["project_id"] == "example-worker"
        assert inv["project"]["projectId"] == "example-worker"
        assert len(inv["instances"]) == 1
        assert inv["instances"][0]["name"] == "example-worker"
        assert inv["instances"][0]["machineType"] == "e2-micro"
        assert inv["instances"][0]["internalIP"] == "192.0.2.10"
        assert inv["instances"][0]["externalIP"] is None
        assert len(inv["buckets"]) == 1
        assert inv["buckets"][0]["name"] == "example-worker-backups-123456789012"
        assert len(inv["budgets"]) == 1
        assert inv["budgets"][0]["displayName"] == "example-worker-10"
        assert inv["errors"] == []


def test_gcloud_inventory_section_filtering():
    def mock_run_json(args, timeout_s=30):
        if "projects" in args:
            return {"projectId": "example-worker"}, None
        return None, "should not be called"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["project"])
        assert inv["status"] == "ok"
        assert inv["project"]["projectId"] == "example-worker"
        assert inv["instances"] == []
        assert inv["buckets"] == []
        assert inv["budgets"] == []
        assert inv["errors"] == []


def test_gcloud_inventory_error_handling():
    def mock_run_json(args, timeout_s=30):
        return None, "API not enabled"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker")
        assert inv["status"] == "error"
        assert len(inv["errors"]) == 4


def test_gcloud_ops_allowed_actions():
    def mock_run_json(args, timeout_s=30):
        return {"status": "RUNNING"}, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate") as mock_gate,
    ):
        res = gcloud_ops(action="instance_describe", target="example-worker")
        assert res["status"] == "ok"
        assert res["target"] == "example-worker"
        assert res["details"] == {"status": "RUNNING"}
        mock_gate.assert_called_once()


def test_gcloud_ops_mutating_actions(monkeypatch):
    # Isolate from an ambient UA_APPROVAL_TOOLS=gcloud_ops in the environment,
    # which would otherwise park these calls in the approval queue instead.
    monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)

    def mock_run_json(args, timeout_s=30):
        return {"operation": "done"}, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate") as mock_gate,
    ):
        res_start = gcloud_ops(action="instance_start", target="example-worker")
        assert res_start["status"] == "ok"
        assert res_start["result"] == {"operation": "done"}

        res_stop = gcloud_ops(action="instance_stop", target="example-worker")
        assert res_stop["status"] == "ok"
        assert res_stop["result"] == {"operation": "done"}
        assert mock_gate.call_count == 2


def test_gcloud_ops_rejected_actions():
    with pytest.raises(ToolError, match="action 'delete_vm' is not allowed"):
        gcloud_ops(action="delete_vm", target="example-worker")


def test_gcloud_inventory_budgets_only_filtering():
    sample_budgets = [
        {
            "displayName": "example-worker-10",
            "amount": {"specifiedAmount": {"currencyCode": "USD", "units": "10"}},
            "budgetFilter": {
                "projects": ["projects/123456789012"],
            },
        },
        {
            "displayName": "other-project-budget",
            "amount": {"specifiedAmount": {"currencyCode": "USD", "units": "100"}},
            "budgetFilter": {
                "projects": ["projects/999999999999"],
            },
        },
    ]

    def mock_run_json(args, timeout_s=30):
        if args[:2] == ["billing", "projects"]:
            return {"billingAccountName": "billingAccounts/123456789012-AAAAAA-BBBBBB"}, None
        if "projects" in args:
            return {"projectNumber": "123456789012"}, None
        if "budgets" in args:
            return sample_budgets, None
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["budgets"])
        assert inv["status"] == "ok"
        assert len(inv["budgets"]) == 1
        assert inv["budgets"][0]["displayName"] == "example-worker-10"


def test_gcloud_ops_approval_gate(monkeypatch):
    from unified_agent.tools.approvals import (
        ApprovalPendingError,
        approval_pending,
        approval_resolve,
    )

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "gcloud_ops")

    def mock_run_json(args, timeout_s=30):
        return {"operation": "done"}, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate"),
    ):
        # Read-only actions bypass the approval queue even when gcloud_ops is gated.
        res_describe = gcloud_ops(action="instance_describe", target="example-worker")
        assert res_describe["status"] == "ok"
        assert approval_pending(tool="gcloud_ops")["pending"] == []

        # Mutating call is parked in approval queue
        with pytest.raises(ApprovalPendingError, match="queued"):
            gcloud_ops(action="instance_start", target="example-worker")

        pending = approval_pending(tool="gcloud_ops")["pending"]
        assert len(pending) == 1
        assert pending[0]["tool"] == "gcloud_ops"
        assert pending[0]["payload"]["action"] == "instance_start"

        # Approve and retry
        approval_resolve(pending[0]["id"], "approve", resolved_by="Operator")
        res = gcloud_ops(action="instance_start", target="example-worker")
        assert res["status"] == "ok"
        assert res["result"] == {"operation": "done"}


def test_gcloud_ops_read_only_actions_bypass_approval_gate(monkeypatch):
    """Read-only gcloud_ops actions must not queue approval even when gated."""
    from unified_agent.tools.approvals import approval_pending

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "gcloud_ops")

    def mock_run_json(args, timeout_s=30):
        if args[:3] == ["compute", "instances", "describe"]:
            return {"name": "example-worker", "status": "RUNNING"}, None
        if args[:3] == ["storage", "buckets", "describe"]:
            return {"name": "example-worker-backups"}, None
        if args[:2] == ["storage", "ls"]:
            return ["gs://example-worker-backups/file.db"], None
        return {}, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate"),
    ):
        for action, target in (
            ("instance_describe", "example-worker"),
            ("bucket_describe", "example-worker-backups"),
            ("bucket_list_objects", "example-worker-backups"),
        ):
            res = gcloud_ops(action=action, target=target)
            assert res["status"] == "ok"
            assert approval_pending(tool="gcloud_ops")["pending"] == []


def test_gcloud_ops_instance_describe_redacts_metadata():
    instance_data = {
        "name": "example-worker",
        "status": "RUNNING",
        "metadata": {
            "items": [
                {"key": "startup-script", "value": "echo hello"},
                {"key": "api-key", "value": "sk-abcdef123456"},
                {"key": "DEPLOY_PASSWORD", "value": "hunter2"},
                {"key": "ssh-keys", "value": "anonb3ll:ssh-ed25519 AAAA..."},
            ]
        },
    }

    def mock_run_json(args, timeout_s=30):
        return instance_data, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate"),
    ):
        res = gcloud_ops(action="instance_describe", target="example-worker")

    items = {i["key"]: i["value"] for i in res["details"]["metadata"]["items"]}
    assert items["startup-script"] == "echo hello"
    assert items["api-key"] == "[REDACTED_SECRET]"
    assert items["DEPLOY_PASSWORD"] == "[REDACTED_SECRET]"
    # ssh-keys holds public key material (OS Login), not a secret — left intact.
    assert items["ssh-keys"] == "anonb3ll:ssh-ed25519 AAAA..."


def test_gcloud_ops_instance_describe_redacts_secret_patterns_in_values():
    """Secrets embedded in non-sensitive keys (e.g. startup-script) must scrub."""
    instance_data = {
        "name": "example-worker",
        "status": "RUNNING",
        "metadata": {
            "items": [
                {
                    "key": "startup-script",
                    "value": "export TOKEN='bearer ya29.abcdef123456'\necho ok",
                },
            ]
        },
    }

    def mock_run_json(args, timeout_s=30):
        return instance_data, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate"),
    ):
        res = gcloud_ops(action="instance_describe", target="example-worker")

    value = res["details"]["metadata"]["items"][0]["value"]
    assert "ya29." not in value
    assert "abcdef123456" not in value
    assert "[REDACTED_SECRET]" in value
    assert "echo ok" in value


def test_gcloud_inventory_budgets_resolves_billing_account_per_project():
    calls = []

    def mock_run_json(args, timeout_s=30):
        calls.append(args)
        if args[:2] == ["projects", "describe"]:
            return {"projectNumber": "123456789012"}, None
        if args[:2] == ["billing", "projects"]:
            return {"billingAccountName": "billingAccounts/ABCDEF-123456-GHIJKL"}, None
        if args[:2] == ["billing", "budgets"]:
            assert "--billing-account=ABCDEF-123456-GHIJKL" in args
            return [], None
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="some-other-project", sections=["budgets"])

    assert inv["status"] == "ok"
    assert any(c[:2] == ["billing", "projects"] for c in calls)
    assert any(c[:2] == ["billing", "projects"] and "some-other-project" in c for c in calls)


def test_gcloud_inventory_budgets_no_billing_account_degrades_cleanly():
    def mock_run_json(args, timeout_s=30):
        if args[:2] == ["projects", "describe"]:
            return {"projectNumber": "123456789012"}, None
        if args[:2] == ["billing", "projects"]:
            return {}, None  # no billingAccountName — project has no linked account
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="unlinked-project", sections=["budgets"])

    assert inv["status"] == "error"
    assert inv["budgets"] == []
    assert any("no linked billing account" in e["error"] for e in inv["errors"])


def test_gcloud_inventory_null_billing_account_name_degrades_cleanly():
    """gcloud can emit "billingAccountName": null, not just omit the key."""

    def mock_run_json(args, timeout_s=30):
        if args[:2] == ["projects", "describe"]:
            return {"projectNumber": "123456789012"}, None
        if args[:2] == ["billing", "projects"]:
            return {"billingAccountName": None}, None
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="unlinked-project", sections=["budgets"])

    assert inv["status"] == "error"
    assert inv["budgets"] == []
    assert any("no linked billing account" in e["error"] for e in inv["errors"])


def test_gcloud_inventory_degraded_status_counts_successful_budgets():
    """A budgets-only success alongside another section's failure is 'degraded', not 'error'."""

    def mock_run_json(args, timeout_s=30):
        if args[:2] == ["billing", "projects"]:
            return {"billingAccountName": "billingAccounts/AAAAAA-BBBBBB-CCCCCC"}, None
        if "projects" in args:
            return {"projectId": "example-worker", "projectNumber": "123456789012"}, None
        if "instances" in args:
            return None, "compute API disabled"
        if "budgets" in args:
            return [
                {
                    "displayName": "example-worker-10",
                    "amount": {"specifiedAmount": {"currencyCode": "USD", "units": "10"}},
                    "budgetFilter": {"projects": ["projects/123456789012"]},
                }
            ], None
        return {}, None

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["instances", "budgets"])

    assert inv["budgets"], "test setup should have produced a non-empty budgets result"
    assert inv["status"] == "degraded"


def test_gcloud_inventory_degraded_status_counts_empty_but_successful_section():
    """A section that legitimately returns zero resources must not read as unattempted.

    A real project can have zero buckets. If 'buckets' succeeds with an empty
    list while 'instances' fails, the overall status must still be 'degraded'
    (a real section succeeded), not 'error' — truthiness of the empty list
    must not stand in for whether the section actually ran.
    """

    def mock_run_json(args, timeout_s=30):
        if "instances" in args:
            return None, "compute API disabled"
        if "buckets" in args:
            return [], None
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["instances", "buckets"])

    assert inv["buckets"] == []
    assert inv["status"] == "degraded"


def test_gcloud_inventory_budgets_not_counted_success_without_project_number():
    """A budgets-list call that succeeds is not a real success if every result

    gets filtered out because the project-number lookup (its prerequisite for
    safely scoping budgets to this project) failed first. That must report
    'error', not 'degraded' — the caller got zero usable budget data.
    """

    def mock_run_json(args, timeout_s=30):
        if args[:2] == ["projects", "describe"]:
            return None, "projects describe failed"
        if args[:2] == ["billing", "projects"]:
            return {"billingAccountName": "billingAccounts/AAAAAA-BBBBBB-CCCCCC"}, None
        if args[:2] == ["billing", "budgets"]:
            return [
                {
                    "displayName": "example-worker-10",
                    "amount": {"specifiedAmount": {"currencyCode": "USD", "units": "10"}},
                    "budgetFilter": {"projects": ["projects/123456789012"]},
                }
            ], None
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["budgets"])

    assert inv["budgets"] == []
    assert inv["status"] == "error"
    assert len([e for e in inv["errors"] if e["section"] == "budgets"]) == 1


def test_gcloud_inventory_budgets_errors_when_project_describe_lacks_project_number():
    """'project' + 'budgets' together must not silently report 'ok'.

    If 'project' is explicitly requested and its describe call succeeds but the
    response has no projectNumber field, the project section itself is a real
    success (no error) — but budgets can never be safely scoped without a
    project number. That must surface as an explicit budgets error, and the
    billing/budgets-list calls must be skipped rather than wastefully run and
    silently discarded.
    """
    calls = []

    def mock_run_json(args, timeout_s=30):
        calls.append(args)
        if args[:2] == ["projects", "describe"]:
            return {"projectId": "example-worker"}, None  # no projectNumber
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["project", "budgets"])

    assert inv["project"] == {
        "projectId": "example-worker",
        "projectNumber": None,
        "name": None,
        "lifecycleState": None,
        "createTime": None,
    }
    assert inv["budgets"] == []
    assert inv["status"] == "degraded"
    assert any(
        e["section"] == "budgets" and "project number unavailable" in e["error"]
        for e in inv["errors"]
    )
    # Only one 'projects describe' call — budgets must not re-fetch what the
    # project section already resolved (or failed to).
    assert len([c for c in calls if c[:2] == ["projects", "describe"]]) == 1


def test_gcloud_inventory_rejects_unknown_sections():
    with pytest.raises(ToolError, match="unknown inventory section"):
        gcloud_inventory(project="example-worker", sections=["instnaces"])


def test_gcloud_inventory_rejects_empty_sections():
    with pytest.raises(ToolError, match="sections cannot be empty"):
        gcloud_inventory(project="example-worker", sections=[])


def test_gcloud_inventory_collects_external_ips_from_all_nics():
    instances = [
        {
            "name": "example-worker",
            "status": "RUNNING",
            "zone": "zones/us-central1-a",
            "machineType": "machineTypes/e2-micro",
            "networkInterfaces": [
                {
                    "networkIP": "192.0.2.10",
                    "accessConfigs": [{"natIP": "203.0.113.1"}],
                },
                {
                    "networkIP": "10.0.0.3",
                    "accessConfigs": [{"natIP": "203.0.113.2"}],
                },
            ],
        }
    ]

    def mock_run_json(args, timeout_s=30):
        if "instances" in args:
            return instances, None
        return {}, None

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["instances"])
        assert inv["instances"][0]["externalIP"] == "203.0.113.1"
        assert inv["instances"][0]["externalIPs"] == ["203.0.113.1", "203.0.113.2"]


def test_gcloud_inventory_collects_ipv6_external_addresses():
    instances = [
        {
            "name": "ipv6-only",
            "status": "RUNNING",
            "zone": "zones/us-central1-a",
            "machineType": "machineTypes/e2-micro",
            "networkInterfaces": [
                {
                    "networkIP": "192.0.2.10",
                    "accessConfigs": [],
                    "ipv6AccessConfigs": [{"externalIpv6": "2001:db8::10"}],
                },
            ],
        }
    ]

    def mock_run_json(args, timeout_s=30):
        if "instances" in args:
            return instances, None
        return {}, None

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["instances"])
        assert inv["instances"][0]["externalIP"] == "2001:db8::10"
        assert inv["instances"][0]["externalIPs"] == ["2001:db8::10"]


def test_gcloud_ops_instance_describe_redacts_pem_in_startup_script():
    """Multiline PEM private keys in startup-script values must scrub."""
    pem_body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
    dash5 = "-" * 5
    begin = f"{dash5}BEGIN PRIVATE KEY{dash5}"
    end = f"{dash5}END PRIVATE KEY{dash5}"
    pem_value = f"export PRIVATE_KEY='{begin}\n{pem_body}...\n{end}\necho ok"
    instance_data = {
        "name": "example-worker",
        "status": "RUNNING",
        "metadata": {
            "items": [
                {"key": "startup-script", "value": pem_value},
            ]
        },
    }

    def mock_run_json(args, timeout_s=30):
        return instance_data, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate"),
    ):
        res = gcloud_ops(action="instance_describe", target="example-worker")

    value = res["details"]["metadata"]["items"][0]["value"]
    assert pem_body not in value
    assert begin not in value
    assert "[REDACTED_SECRET]" in value
    assert "echo ok" in value


def test_gcloud_ops_instance_describe_redacts_ghp_token_in_startup_script():
    """GitHub tokens embedded in startup-script values must scrub."""
    ghp_token = "ghp_" + "1" * 36

    instance_data = {
        "name": "example-worker",
        "status": "RUNNING",
        "metadata": {
            "items": [
                {
                    "key": "startup-script",
                    "value": f"export GITHUB_TOKEN={ghp_token}",
                },
            ]
        },
    }

    def mock_run_json(args, timeout_s=30):
        return instance_data, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate"),
    ):
        res = gcloud_ops(action="instance_describe", target="example-worker")

    value = res["details"]["metadata"]["items"][0]["value"]
    assert ghp_token not in value
    assert "[REDACTED_SECRET]" in value


def test_gcloud_ops_instance_describe_redacts_db_pass_in_startup_script():
    """Abbreviated credential env vars (DB_PASS=) in startup-script must scrub."""
    instance_data = {
        "name": "example-worker",
        "status": "RUNNING",
        "metadata": {
            "items": [
                {
                    "key": "startup-script",
                    "value": "export DB_PASS=hunter2secret\necho ok",
                },
            ]
        },
    }

    def mock_run_json(args, timeout_s=30):
        return instance_data, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate"),
    ):
        res = gcloud_ops(action="instance_describe", target="example-worker")

    value = res["details"]["metadata"]["items"][0]["value"]
    assert "hunter2secret" not in value
    assert "[REDACTED_SECRET]" in value
    assert "echo ok" in value


def test_gcloud_ops_instance_describe_redacts_api_key_and_database_password():
    """Codex P1: generic API_KEY= and DATABASE_PASSWORD= assignments must scrub."""
    instance_data = {
        "name": "example-worker",
        "status": "RUNNING",
        "metadata": {
            "items": [
                {
                    "key": "startup-script",
                    "value": "export API_KEY=AIzaSyC1234567890\nDATABASE_PASSWORD=hunter2",
                },
            ]
        },
    }

    def mock_run_json(args, timeout_s=30):
        return instance_data, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate"),
    ):
        res = gcloud_ops(action="instance_describe", target="example-worker")

    value = res["details"]["metadata"]["items"][0]["value"]
    assert "AIzaSyC" not in value
    assert "hunter2" not in value
    assert value.count("[REDACTED_SECRET]") >= 2


def test_gcloud_ops_instance_describe_redacts_unknown_metadata_keys():
    """Custom metadata keys must not pass through unredacted."""
    instance_data = {
        "name": "example-worker",
        "status": "RUNNING",
        "metadata": {
            "items": [
                {"key": "custom-deploy-config", "value": "db_password=hunter2"},
            ]
        },
    }

    def mock_run_json(args, timeout_s=30):
        return instance_data, None

    with (
        patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json),
        patch("unified_agent.safety.check_policy_gate"),
    ):
        res = gcloud_ops(action="instance_describe", target="example-worker")

    value = res["details"]["metadata"]["items"][0]["value"]
    assert value == "[REDACTED_METADATA]"
    assert "hunter2" not in value


def test_gcloud_secret_redaction():
    secret_output = "Error with token bearer ya29.abcdef123456 private_key in output"
    mock_res = MagicMock()
    mock_res.returncode = 1
    mock_res.stderr = secret_output

    with (
        patch("unified_agent.tools.gcloud._resolve_gcloud_bin", return_value="gcloud"),
        patch("subprocess.run", return_value=mock_res),
    ):
        data, err = _run_gcloud_json(["compute", "instances", "list"])
        assert data is None
        assert "ya29" not in err
        assert "[REDACTED_SECRET]" in err


# --------------------------------------------------------------------------- #
# Health-signal integrity (task #1043 / #1044 prerequisite)
#
# Self-healing and the Control Center health panel both key off
# gcloud_inventory. A section that cannot be read must never be reported as a
# section that is empty.
# --------------------------------------------------------------------------- #


def test_gcloud_inventory_empty_stdout_section_is_unavailable_not_empty():
    """An empty gcloud stdout must not read as 'zero resources, all healthy'.

    `_run_gcloud_json` returns ({}, None) for empty stdout. For a list command
    that dict fails the isinstance(data, list) guard, so before this was fixed
    the section recorded neither success nor error: instances stayed [] and the
    overall status stayed "ok". This reproduces the live false-healthy seen
    against example-worker, where a hub credential lacking compute.instances.list
    yielded {"status": "ok", "instances": [], "errors": []} while the instance
    was RUNNING.
    """

    def mock_run_json(args, timeout_s=30):
        # Empty stdout -> ({}, None), the silent-drop path.
        return {}, None

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["instances"])

    assert inv["instances"] == []
    assert inv["status"] == "error", "unreadable section must not report ok"
    assert inv["sections"]["instances"] == "unavailable"
    assert any(e["section"] == "instances" for e in inv["errors"]), (
        "a section that produced no usable data must say so"
    )


def test_gcloud_inventory_sections_map_distinguishes_ok_from_unavailable():
    """The per-section map is what lets a caller tell 'none' from 'could not look'."""

    def mock_run_json(args, timeout_s=30):
        if "instances" in args:
            return [], None  # genuinely zero instances, read successfully
        if "buckets" in args:
            return None, "PERMISSION_DENIED: storage.buckets.list"
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["instances", "buckets"])

    # Both lists are empty; only the sections map separates them.
    assert inv["instances"] == []
    assert inv["buckets"] == []
    assert inv["sections"] == {"instances": "ok", "buckets": "unavailable"}
    assert inv["status"] == "degraded"


def test_gcloud_inventory_ok_requires_every_requested_section():
    """'ok' means every requested section was actually read."""

    def mock_run_json(args, timeout_s=30):
        if "instances" in args:
            return [{"name": "example-worker", "status": "RUNNING"}], None
        if "buckets" in args:
            return [], None
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["instances", "buckets"])

    assert inv["status"] == "ok"
    assert inv["sections"] == {"instances": "ok", "buckets": "ok"}
    assert inv["errors"] == []


def test_gcloud_inventory_running_instance_is_not_masked_by_permission_loss():
    """The #1043 guard: a recovery rule must not see a live worker as absent.

    Regression for the live case -- the credential can list nothing, so the
    naive reading is "no instances exist". Status and the sections map must
    both mark that unreadable, so a self-healing rule fails closed instead of
    acting on a phantom 'instance missing' signal.
    """

    def mock_run_json(args, timeout_s=30):
        if "instances" in args:
            return {}, None  # empty stdout under an under-privileged credential
        return None, "unexpected call"

    with patch("unified_agent.tools.gcloud._run_gcloud_json", side_effect=mock_run_json):
        inv = gcloud_inventory(project="example-worker", sections=["instances"])

    healthy_and_absent = inv["status"] == "ok" and not inv["instances"]
    assert not healthy_and_absent, (
        "an unreadable instance list must never present as a healthy empty project"
    )


@pytest.mark.parametrize(
    "inventory,expected,detail",
    [
        (
            {
                "sections": {"instances": "unavailable"},
                "instances": [],
                "errors": [{"section": "instances", "error": "permission denied"}],
            },
            "DEGRADED",
            "permission denied",
        ),
        ({"sections": {"instances": "ok"}, "instances": []}, "DEGRADED", "not found"),
        (
            {
                "sections": {"instances": "ok"},
                "instances": [{"name": "example-worker", "status": "RUNNING"}],
            },
            "LIVE",
            "RUNNING",
        ),
        (
            {
                "sections": {"instances": "ok"},
                "instances": [{"name": "example-worker", "status": "TERMINATED"}],
            },
            "DEGRADED",
            "TERMINATED",
        ),
    ],
)
def test_classify_gcp_Agent20_health_requires_readable_running_worker(inventory, expected, detail):
    health = classify_gcp_Agent20_health(inventory)
    assert health["status"] == expected
    assert detail in health["detail"]


@pytest.mark.parametrize("first_state", ["RUNNING", "TERMINATED"])
def test_cloud_health_duplicate_worker_names_are_unknown(monkeypatch, first_state):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    inventory = {
        "sections": {"instances": "ok"},
        "instances": [
            {"name": "example-worker", "status": first_state, "zone": "europe-west1-b"},
            {"name": "example-worker", "status": "TERMINATED", "zone": "us-central1-a"},
        ],
    }
    result = classify_gcp_Agent20_health(inventory)
    assert result["status"] == "UNKNOWN"
    assert "ambiguous" in result["detail"]
