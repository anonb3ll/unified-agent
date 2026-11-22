"""Tests for the Delegation Spec Standard (task #160).

The standard exists because four defects shipped between 2026-07-22 and 07-26
with every gate green — an Browser content script unloadable in Chrome since
Phase 1, a memory tag-escaping regression that silently dropped notes, a cleanup
tool that rmtree'd whatever it could not identify, and the executor auth bypass
(#82). Every one passed its Definition of Done, because every DoD was a variant
of "the build passes".

Those four are the worked examples below: each gets a test showing the DoD it
actually shipped with is rejected, and the DoD that would have caught it passes.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools import delegate as delegate_tool
from unified_agent.tools.delegation_spec import (
    delegation_spec_check,
    delegation_spec_template,
    enforce_delegation_spec,
)

GOOD_SPEC = """\
### Objective
Make the content script actually load in Chrome.

### Scope
- vite.config.ts
- src/content.ts

### Definition of Done
- dist/content.js parses as a classic script and contains no top-level import
- loading the unpacked extension runs the sensor layer end-to-end
- Gates: uv run pytest, ruff check

### TDD Constraints
The test suite is ground truth; no test may be skipped to make this pass.
- Stub: the network
- Must be real: the built artifact

### Boundaries
- Do not change the manifest version.
"""


class TestRequiredSections:
    def test_complete_spec_passes(self):
        res = delegation_spec_check(GOOD_SPEC)
        assert res["ok"] is True
        assert res["errors"] == []
        assert res["sections_missing"] == []

    def test_missing_sections_are_all_reported_at_once(self):
        res = delegation_spec_check("### Objective\nDo the thing.\n")
        assert res["ok"] is False
        assert "scope" in res["errors"][0]
        assert "definition of done" in res["errors"][0]
        # One error listing everything — a delegation rejected one line at a
        # time is worse than no check.
        assert len([e for e in res["errors"] if "missing required section" in e]) == 1

    def test_empty_spec(self):
        res = delegation_spec_check("")
        assert res["ok"] is False
        assert res["sections_missing"] == [
            "objective",
            "scope",
            "definition of done",
            "tdd",
            "boundaries",
        ]

    def test_accepts_the_bare_uppercase_style_already_in_the_queue(self):
        """Specs on tasks #167-#174 use OBJECTIVE / SCOPE / DEFINITION OF DONE
        as bare uppercase labels rather than markdown headings."""
        spec = """\
OBJECTIVE
Fix the thing.

SCOPE (files you may touch)
- src/x.py

DEFINITION OF DONE
- a call with a bad token is refused and the refusal is asserted
- uv run pytest green

TDD BOUNDARY
Stub the HTTP layer.

BOUNDARIES
Do not weaken the allowlist.
"""
        assert delegation_spec_check(spec)["ok"] is True


class TestBuildStatusIsNeverSufficient:
    """The one rule that matters: a DoD must name observable behaviour."""

    def _spec_with_dod(self, dod: str) -> str:
        return f"""\
### Objective
Something.

### Scope
- src/x.py

### Definition of Done
{dod}

### TDD Constraints
Tests are ground truth.

### Boundaries
- None.
"""

    def test_build_status_only_dod_is_rejected(self):
        res = delegation_spec_check(
            self._spec_with_dod("- the build passes\n- uv run pytest green\n- ruff clean")
        )
        assert res["ok"] is False
        assert any("no observable behaviour" in e for e in res["errors"])
        assert res["behavioural_criteria"] == 0

    def test_one_behavioural_criterion_is_enough_to_pass(self):
        res = delegation_spec_check(
            self._spec_with_dod(
                "- the daemon refuses to start without a token, asserted in a test\n"
                "- uv run pytest green"
            )
        )
        assert res["ok"] is True
        assert res["behavioural_criteria"] == 1

    def test_single_criterion_warns_about_the_failure_mode(self):
        res = delegation_spec_check(
            self._spec_with_dod("- verify the happy path returns the parsed note")
        )
        assert res["ok"] is True
        assert any("failure mode" in w for w in res["warnings"])

    def test_empty_dod_section_is_rejected(self):
        spec = """\
### Objective
X.

### Scope
- a.py

### Definition of Done

### TDD Constraints
Ground truth.

### Boundaries
- None.
"""
        res = delegation_spec_check(spec)
        assert res["ok"] is False
        assert any("empty" in e for e in res["errors"])


class TestTheFourWorkedExamples:
    """Each defect's real DoD is rejected; the DoD that would have caught it passes."""

    def _spec(self, dod_lines: list[str]) -> str:
        body = "\n".join(f"- {line}" for line in dod_lines)
        return f"""\
### Objective
Ship the feature.

### Scope
- src/x.py

### Definition of Done
{body}

### TDD Constraints
Tests are ground truth.

### Boundaries
- None.
"""

    @pytest.mark.parametrize(
        "shipped_dod,catching_dod",
        [
            # 1. Browser content script, unloadable in Chrome since Phase 1.
            (
                ["bun run build succeeds", "no TypeScript errors"],
                ["dist/content.js contains no top-level import and loads in Chrome"],
            ),
            # 2. Memory tag-escaping regression that silently dropped notes.
            (
                ["all tests pass", "ruff clean"],
                ["a note tagged with a quote is still returned by memory_search"],
            ),
            # 3. Cleanup tool that rmtree'd what it could not identify.
            (
                ["the build passes"],
                ["a directory that is not a worktree is refused, never removed"],
            ),
            # 4. Executor auth bypass (#82).
            (
                ["pytest green", "CI is green"],
                ["a 401 never reaches in-process dispatch, asserted with a stubbed hub"],
            ),
        ],
    )
    def test_shipped_dod_rejected_catching_dod_accepted(self, shipped_dod, catching_dod):
        assert delegation_spec_check(self._spec(shipped_dod))["ok"] is False
        assert delegation_spec_check(self._spec(catching_dod))["ok"] is True


class TestEnforcement:
    """These are the tests that are ABOUT the standard, so they opt back into
    enforcement (conftest turns it off for the rest of the suite)."""

    @pytest.fixture(autouse=True)
    def _enforcing(self, monkeypatch):
        monkeypatch.setenv("UA_DELEGATION_SPEC_ENFORCE", "1")

    def test_delegate_refuses_a_spec_that_fails_the_standard(self):
        with pytest.raises(ToolError, match="Delegation Spec Standard"):
            delegate_tool.delegate(
                task_spec="Fix the executor. Build must pass.",
                from_agent="Desktop_Agent1",
                to_agent="Desktop_agent3",
            )

    def test_delegate_accepts_a_conforming_spec(self):
        res = delegate_tool.delegate(
            task_spec=GOOD_SPEC,
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        assert res["task_id"] > 0
        assert "spec_warnings" not in res

    def test_enforcement_can_be_downgraded(self, monkeypatch):
        bad = "Just do it."
        with pytest.raises(ToolError):
            enforce_delegation_spec(bad, "Desktop_agent3")

        monkeypatch.setenv("UA_DELEGATION_SPEC_ENFORCE", "0")
        res = enforce_delegation_spec(bad, "Desktop_agent3")
        assert res["ok"] is False  # still reported, just not fatal

    def test_downgraded_enforcement_surfaces_the_problems_on_the_result(self, monkeypatch):
        monkeypatch.setenv("UA_DELEGATION_SPEC_ENFORCE", "0")
        res = delegate_tool.delegate(
            task_spec="Fix the executor. Build must pass.",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        assert res["spec_warnings"], "a bypassed check must still be visible on the result"


def test_template_is_itself_a_valid_spec():
    """The canonical shape must pass its own validator."""
    template = delegation_spec_template()["template"]
    res = delegation_spec_check(template)
    assert res["ok"] is True, res["errors"]


def test_enforcement_is_on_by_default_in_production(monkeypatch):
    """conftest turns it off for the suite; the shipped default must be ON.

    Turning it off restores exactly the condition the four 07-22 -> 07-26
    defects shipped under.
    """
    from unified_agent.tools.delegation_spec import spec_enforcement_enabled

    monkeypatch.delenv("UA_DELEGATION_SPEC_ENFORCE", raising=False)
    assert spec_enforcement_enabled() is True


class TestDelegationSpecBuild:
    def test_build_valid_spec(self):
        from unified_agent.tools.delegation_spec import delegation_spec_build

        res = delegation_spec_build(
            objective="Wire the new endpoint into the router.",
            scope_files=["src/routes.py", "src/api.py"],
            dod_criteria=[
                "a POST request returns 200 and the created record, asserted in a test",
                "a request without a token is rejected with 401, asserted in a test",
            ],
            tdd_stub=["network"],
            tdd_must_be_real=["database"],
            boundaries=["Do not modify auth token verification."],
        )
        assert res["ok"] is True
        spec = res["spec"]
        assert "### Objective" in spec
        assert "Wire the new endpoint" in spec
        assert "### Scope" in spec
        assert "- src/routes.py" in spec
        assert "### Definition of Done" in spec
        assert "POST request returns 200" in spec
        assert "Gates: uv run pytest" in spec
        assert "### TDD Constraints" in spec
        assert "### Boundaries" in spec

    def test_build_with_string_inputs(self):
        from unified_agent.tools.delegation_spec import delegation_spec_build

        res = delegation_spec_build(
            objective="Implement feature X.",
            scope_files="src/a.py\nsrc/b.py",
            dod_criteria="- verify happy path loads\n- verify failure mode refuses invalid inputs",
            boundaries="Do not touch config.",
        )
        assert res["ok"] is True
        assert "- src/a.py" in res["spec"]
        assert "verify happy path loads" in res["spec"]

    def test_build_preserves_leading_characters_that_are_not_a_list_marker(self):
        """lstrip("-*0123456789.) ") used to strip any run from that charset,
        not just an actual bullet/numbered prefix — mangling values like
        dotfiles, filenames starting with digits, and criteria starting with a
        status code (task #445 / PR #191 review)."""
        from unified_agent.tools.delegation_spec import delegation_spec_build

        res = delegation_spec_build(
            objective="Fix CI config handling.",
            scope_files=[".github/workflows/ci.yml", "123.py"],
            dod_criteria=["401 Unauthorized is returned for a missing token, asserted in a test"],
            boundaries=["- Do not touch unrelated workflows."],
        )
        assert res["ok"] is True
        spec = res["spec"]
        assert "- .github/workflows/ci.yml" in spec
        assert "- 123.py" in spec
        assert "401 Unauthorized is returned" in spec
        assert "Do not touch unrelated workflows." in spec

    def test_build_requires_objective(self):
        from unified_agent.tools.delegation_spec import delegation_spec_build

        with pytest.raises(ToolError, match="objective is required"):
            delegation_spec_build(objective="")

    def test_build_fails_on_build_status_only_criteria_when_auto_validate(self):
        from unified_agent.tools.delegation_spec import delegation_spec_build

        with pytest.raises(ToolError, match="no observable behaviour"):
            delegation_spec_build(
                objective="Implement feature X.",
                dod_criteria=["the build passes", "pytest is green"],
                auto_validate=True,
            )

    def test_delegation_manage_spec_build_action(self):
        from unified_agent.tools.delegate import delegation_manage

        res = delegation_manage(
            action="spec_build",
            params={
                "objective": "Build and expose new tool.",
                "scope_files": ["src/tool.py"],
                "dod_criteria": [
                    "exercising the tool returns expected payload, asserted in test",
                    "calling with invalid args raises ToolError, asserted in test",
                ],
            },
        )
        assert res["ok"] is True
        assert "### Objective" in res["spec"]
