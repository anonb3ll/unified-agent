"""Cross-language fixture checks for generated Pydantic protocol models."""

from __future__ import annotations

import json
import unittest
from enum import Enum
from pathlib import Path

from generated.python import Priority as PublicPriority
from generated.python.models import (
    Agent,
    Approval,
    ApprovalRequest,
    ApprovalRequestResult,
    ApprovalResolveRequest,
    ApprovalResolveResult,
    ApprovalToken,
    Artifact,
    Capability,
    ContextEnum,
    Decision,
    DispatchRequest,
    Event,
    EvidenceCreateOutcome1,
    EvidenceCreateOutcome2,
    EvidenceCreateRequest,
    EvidenceCreateResult,
    Execution,
    Kind,
    Kind1,
    MemoryRecord,
    PathResolutionResult,
    PendingApprovalResult,
    PolicyDecision,
    Principal,
    Priority,
    Profile,
    Status,
    Status1,
    Status2,
    Status3,
    Task,
    TaskCreateRequest,
    TaskUpdateRequest,
    TaskWriteOutcome1,
    TaskWriteOutcome2,
    TaskWriteResult,
    Tool,
    ToolInvocation,
    WorkProductPending,
    WorkProductReleaseRequest,
    WorkProductRow,
    WorkProductSubmitRequest,
    WorkProductWriteOutcome1,
    WorkProductWriteOutcome2,
    WorktreePolicy,
)
from pydantic import TypeAdapter, ValidationError

ROOT = Path(__file__).parent
SCHEMA_DIR = ROOT.parent / "generated" / "json-schema"
FIXTURES = json.loads((ROOT / "fixtures" / "valid-protocol.json").read_text())
MODELS = {
    "Task": Task,
    "TaskCreateRequest": TaskCreateRequest,
    "TaskUpdateRequest": TaskUpdateRequest,
    "TaskWriteResult": TaskWriteResult,
    "PendingApprovalResult": PendingApprovalResult,
    "Tool": Tool,
    "ToolInvocation": ToolInvocation,
    "Capability": Capability,
    "Approval": Approval,
    "WorkProductSubmitRequest": WorkProductSubmitRequest,
    "WorkProductReleaseRequest": WorkProductReleaseRequest,
    "WorkProductRow": WorkProductRow,
    "WorkProductPending": WorkProductPending,
    "ApprovalRequest": ApprovalRequest,
    "ApprovalRequestResult": ApprovalRequestResult,
    "ApprovalResolveRequest": ApprovalResolveRequest,
    "ApprovalResolveResult": ApprovalResolveResult,
    "ApprovalToken": ApprovalToken,
    "Principal": Principal,
    "Profile": Profile,
    "PolicyDecision": PolicyDecision,
    "Execution": Execution,
    "Event": Event,
    "EvidenceCreateRequest": EvidenceCreateRequest,
    "EvidenceCreateResult": EvidenceCreateResult,
    "Artifact": Artifact,
    "Agent": Agent,
    "MemoryRecord": MemoryRecord,
    "DispatchRequest": DispatchRequest,
    "WorktreePolicy": WorktreePolicy,
    "PathResolutionResult": PathResolutionResult,
}
UNION_MODELS = {
    "TaskWriteOutcome": TypeAdapter(TaskWriteOutcome1 | TaskWriteOutcome2),
    "WorkProductWriteOutcome": TypeAdapter(WorkProductWriteOutcome1 | WorkProductWriteOutcome2),
    "EvidenceCreateOutcome": TypeAdapter(EvidenceCreateOutcome1 | EvidenceCreateOutcome2),
}
NON_NULLABLE_DEFAULT_FIELDS = (
    (Task, "Task", "priority"),
    (Task, "Task", "project"),
    (Tool, "Tool", "description"),
    (ToolInvocation, "ToolInvocation", "arguments"),
    (Capability, "Capability", "description"),
    (Capability, "Capability", "tools"),
    (Approval, "Approval", "payload"),
    (Principal, "Principal", "kind"),
    (Profile, "Profile", "denied_tools"),
    (Artifact, "Artifact", "kind"),
    (MemoryRecord, "MemoryRecord", "tags"),
    (DispatchRequest, "DispatchRequest", "context"),
    (Event, "Event", "payload"),
    (WorktreePolicy, "WorktreePolicy", "require_task_claim"),
    (WorktreePolicy, "WorktreePolicy", "branch_prefix"),
    (WorktreePolicy, "WorktreePolicy", "allow_outside_workspace"),
)
# Update request models: every optional field means "leave unchanged," never
# "reset to some default" (task #1300). Create requests use service defaults.
PATCH_MODELS = (TaskUpdateRequest,)


class GeneratedModelTests(unittest.TestCase):
    def test_generated_enum_classes_remain_public_and_model_values_remain_enums(
        self,
    ) -> None:
        """The generated Enum imports and parsed values are part of the Python SDK API."""
        enum_types = (
            Status,
            Status1,
            Decision,
            Kind,
            ContextEnum,
            Status2,
            Kind1,
            Status3,
            Priority,
        )
        self.assertIs(PublicPriority, Priority)
        for enum_type in enum_types:
            with self.subTest(enum_type=enum_type.__name__):
                self.assertTrue(issubclass(enum_type, Enum))

        task = Task.model_validate({**FIXTURES["Task"], "status": "doing", "priority": "high"})
        self.assertIs(task.status, Status3.doing)
        self.assertIs(task.priority, Priority.high)

        update = TaskUpdateRequest.model_validate({"id": 17, "status": "doing", "priority": "high"})
        self.assertIs(update.status, Status3.doing)
        self.assertIs(update.priority, Priority.high)

    def test_every_protocol_fixture_validates(self) -> None:
        self.assertEqual(set(FIXTURES), set(MODELS) | set(UNION_MODELS))
        for name, model in MODELS.items():
            with self.subTest(schema=name):
                instance = model.model_validate(FIXTURES[name])
                self.assertIsInstance(instance, model)
        for name, adapter in UNION_MODELS.items():
            with self.subTest(schema=name):
                self.assertIsNotNone(adapter.validate_python(FIXTURES[name]))

    def test_generated_models_reject_null_for_zod_defaults(self) -> None:
        for model, fixture_name, field in NON_NULLABLE_DEFAULT_FIELDS:
            with self.subTest(schema=fixture_name, field=field):
                invalid = {**FIXTURES[fixture_name], field: None}
                with self.assertRaises(ValidationError):
                    model.model_validate(invalid)

    def test_generated_models_reject_invalid_required_values(self) -> None:
        cases = (
            (Task, "Task", "title"),
            (Tool, "Tool", "name"),
            (PolicyDecision, "PolicyDecision", "reason"),
            (DispatchRequest, "DispatchRequest", "prompt"),
        )
        for model, fixture_name, field in cases:
            with self.subTest(schema=fixture_name, field=field):
                invalid = {**FIXTURES[fixture_name], field: ""}
                with self.assertRaises(ValidationError):
                    model.model_validate(invalid)

        policy = FIXTURES["PolicyDecision"]
        invalid_policy = {
            **policy,
            "principal": {**policy["principal"], "kind": None},
        }
        with self.assertRaises(ValidationError):
            PolicyDecision.model_validate(invalid_policy)

    def test_generated_work_product_row_rejects_malformed_release_hash(self) -> None:
        invalid = {**FIXTURES["WorkProductRow"], "released_sha256": "malformed"}
        with self.assertRaises(ValidationError):
            WorkProductRow.model_validate(invalid)

    def test_http_write_models_forbid_caller_identity_and_approval_decisions(
        self,
    ) -> None:
        cases = (
            (TaskCreateRequest, {"title": "x", "actor": "Operator"}),
            (TaskUpdateRequest, {"id": 1, "claimed_by": "Desktop_Agent2"}),
            (ApprovalRequest, {"tool": "task_update", "decision": "approve"}),
            (
                WorkProductSubmitRequest,
                {
                    "source_path": "/workspace/x",
                    "title": "Memo",
                    "template_pack": "legal",
                    "producing_agent": "Operator",
                },
            ),
            (WorkProductReleaseRequest, {"work_product_id": 27, "title": "Changed"}),
            (
                ApprovalResolveRequest,
                {"id": 1, "decision": "approve", "resolved_by": "Operator"},
            ),
            (
                EvidenceCreateRequest,
                {"channel": "task", "payload": {}, "originator": "Operator"},
            ),
        )
        for model, payload in cases:
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(payload)

    def test_task_update_does_not_inherit_defaults_for_omitted_fields(self) -> None:
        """Regression for #1300.

        TaskUpdateRequest.priority inherited the "med" default belonging to
        the Task read model. datamodel-codegen deduplicated their identical
        high/med/low enum into one shared Python Enum class and leaked that
        default onto every field typed with the class. A client validating
        {"id": 17} and calling model_dump() then silently reset a high/low
        priority task on write-back.
        """
        update = TaskUpdateRequest.model_validate({"id": 17})
        self.assertIsNone(update.priority)
        self.assertNotIn("priority", update.model_dump(exclude_unset=True))

    def test_task_create_keeps_service_medium_priority_default(self) -> None:
        create = TaskCreateRequest.model_validate({"title": "default priority"})
        self.assertIs(create.priority, Priority.med)
        self.assertIs(create.model_dump()["priority"], Priority.med)
        self.assertEqual(create.model_dump(mode="json")["priority"], "med")
        create_schema = json.loads((SCHEMA_DIR / "TaskCreateRequest.json").read_text())
        self.assertEqual(create_schema["properties"]["priority"]["default"], "med")
        self.assertNotIn("priority", create_schema["required"])

    def test_task_write_models_reject_whitespace_only_titles(self) -> None:
        for model, payload in (
            (TaskCreateRequest, {"title": " \t "}),
            (TaskUpdateRequest, {"id": 17, "title": " \t "}),
        ):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(payload)

    def test_work_product_write_models_preserve_reviewed_inputs(self) -> None:
        request = WorkProductSubmitRequest.model_validate(FIXTURES["WorkProductSubmitRequest"])
        self.assertEqual(request.title, "Synthetic memo")
        with self.assertRaises(ValidationError):
            WorkProductSubmitRequest.model_validate(
                {**FIXTURES["WorkProductSubmitRequest"], "title": " \t "}
            )
        with self.assertRaises(ValidationError):
            WorkProductReleaseRequest.model_validate({"work_product_id": 0})

    def test_generated_model_defaults_match_json_schema_defaults(self) -> None:
        """For each PATCH_MODELS entry, optional field defaults match its schema.

        A mismatch catches the #1300 shared-enum leak without constraining
        defaults on unrelated models.
        """
        checked_any_optional_field = False

        for model in PATCH_MODELS:
            path = SCHEMA_DIR / f"{model.__name__}.json"
            self.assertTrue(path.is_file(), f"missing schema for {model.__name__}")
            schema = json.loads(path.read_text())
            self.assertEqual(schema.get("type"), "object")
            self.assertIn("properties", schema)
            required = set(schema.get("required", []))

            for prop_name, prop_schema in schema["properties"].items():
                field_info = model.model_fields.get(prop_name)
                if field_info is None:
                    continue  # datamodel-codegen renamed the field (e.g. reserved word)
                if prop_name in required:
                    continue  # no default is meaningful on a required field

                checked_any_optional_field = True
                expected_default = prop_schema.get("default", None)
                with self.subTest(schema=path.stem, field=prop_name):
                    self.assertEqual(
                        field_info.default,
                        expected_default,
                        f"{model.__name__}.{prop_name}: generated Pydantic default "
                        f"{field_info.default!r} does not match its own JSON "
                        f"Schema default {expected_default!r}",
                    )

        self.assertTrue(checked_any_optional_field)

    def test_generated_artifacts_are_ruff_clean(self) -> None:
        """Regeneration must leave models.py and this fixture ruff-clean (#1314)."""
        import shutil
        import subprocess

        ruff = shutil.which("ruff") or shutil.which("uvx")
        self.assertIsNotNone(ruff, "ruff or uvx required to assert fixture cleanliness")
        models = ROOT.parent / "generated" / "python" / "models.py"
        self.assertTrue(models.is_file(), f"missing {models}")
        targets = [str(models), str(Path(__file__).resolve())]
        if Path(ruff).name == "uvx":
            base = [ruff, "--from", "ruff==0.15.21", "ruff"]
        else:
            base = [ruff]
        check = subprocess.run(
            [*base, "check", "--select", "I001,E,F,W", *targets],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            check.returncode,
            0,
            f"ruff check failed on generated artifacts:\n{check.stdout}{check.stderr}",
        )
        fmt = subprocess.run(
            [*base, "format", "--check", "--line-length", "100", *targets],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            fmt.returncode,
            0,
            f"ruff format --check failed on generated artifacts:\n{fmt.stdout}{fmt.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
