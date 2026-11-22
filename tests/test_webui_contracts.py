import pytest

from unified_agent.webui.contracts import DispatchRequest, dispatch_spec, parse_dispatch_request


def test_dispatch_contract_normalizes_optional_fields_and_deduplicates_context():
    request = parse_dispatch_request(
        {
            "agent": "Desktop_agent3",
            "project": "unified-agent",
            "prompt": "  Run the smoke check  ",
            "task_id": "252",
            "context": ["task_notes", "task_notes", "recent_validation"],
        }
    )
    assert request == DispatchRequest(
        agent="Desktop_agent3",
        prompt="Run the smoke check",
        task_id=252,
        context=("task_notes", "recent_validation"),
        project="unified-agent",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"agent": "Desktop_agent3", "project": "unified-agent", "prompt": ""},
        {"agent": "Desktop_agent3", "project": "unified-agent", "prompt": "x" * 1001},
        {"agent": "Desktop_agent3", "project": "unified-agent", "prompt": "x", "task_id": 0},
        {
            "agent": "Desktop_agent3",
            "project": "unified-agent",
            "prompt": "x",
            "context": ["raw_logs"],
        },
        {"agent": "Desktop_agent3", "prompt": "x"},
        {"agent": "Desktop_agent3", "project": "", "prompt": "x"},
        {"agent": "Desktop_agent3", "project": "not-a-real-project", "prompt": "x"},
    ],
)
def test_dispatch_contract_rejects_unsafe_or_incomplete_payloads(payload):
    with pytest.raises(ValueError):
        parse_dispatch_request(payload)


def test_dispatch_spec_contains_observable_definition_of_done_without_response_secrets():
    spec = dispatch_spec(
        DispatchRequest(
            agent="Desktop_agent3",
            prompt="Inspect the pane",
            task_id=252,
            context=("task_notes",),
            project="browser",
        )
    )
    assert all(
        section in spec
        for section in ("Objective", "Scope", "Definition of Done", "TDD Constraints", "Boundaries")
    )
    assert "Inspect the pane" in spec
    assert "raw prompts" in spec
    assert "browser" in spec
