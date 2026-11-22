import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.skills import skill_review

GOOD_DESC = (
    "Reviews a Python module for missing type hints and adds them. "
    "Use when asked to type-annotate, add hints, or fix mypy errors in Python code."
)

GOOD_BODY = """\
# Type hints

1. Run `uv run mypy <module>` and collect errors.
2. Annotate public functions first; use `from __future__ import annotations`.
3. Re-run mypy until clean.
"""


def make_skill(root, dirname, name=None, description=GOOD_DESC, body=GOOD_BODY, raw=None):
    d = root / dirname
    d.mkdir(parents=True)
    if raw is None:
        raw = f"---\nname: {name or dirname}\ndescription: {description}\n---\n\n{body}"
    (d / "SKILL.md").write_text(raw)
    return d


def rules(report):
    return {f["rule"] for f in report["findings"]}


def test_clean_skill_passes(isolated_env):
    make_skill(isolated_env, "type-hints")
    report = skill_review("type-hints")
    assert report["status"] == "pass"
    assert report["findings"] == []
    assert report["skills_reviewed"] == ["type-hints"]


def test_missing_frontmatter_fails(isolated_env):
    make_skill(isolated_env, "no-fm", raw="# Just a heading\n\nSome body text here.\n")
    report = skill_review("no-fm")
    assert report["status"] == "fail"
    assert "SKILL-FRONTMATTER" in rules(report)


def test_name_directory_mismatch_and_case(isolated_env):
    make_skill(isolated_env, "my-skill", name="Other_Name")
    report = skill_review("my-skill")
    assert report["status"] == "warn"
    msgs = [f["message"] for f in report["findings"] if f["rule"] == "SKILL-NAME"]
    assert any("does not match its directory" in m for m in msgs)
    assert any("kebab-case" in m for m in msgs)


def test_description_over_limit(isolated_env):
    make_skill(isolated_env, "long-desc", description="Use when needed. " + "x" * 1100)
    report = skill_review("long-desc")
    assert any(
        f["rule"] == "SKILL-DESC-LENGTH" and f["severity"] == "medium" for f in report["findings"]
    )


def test_trigger_guidance_buried_in_body_is_medium(isolated_env):
    make_skill(
        isolated_env,
        "buried",
        description="Reviews Python modules for missing type annotations and adds them.",
        body="Use this skill when the user asks for type hints.\n\nThen run mypy.\n",
    )
    report = skill_review("buried")
    fd = next(f for f in report["findings"] if f["rule"] == "SKILL-DESC-TRIGGER")
    assert fd["severity"] == "medium"
    assert "body" in fd["message"]


def test_missing_trigger_guidance_is_low(isolated_env):
    make_skill(
        isolated_env,
        "no-trigger",
        description="Reviews Python modules for missing type annotations and adds them.",
    )
    report = skill_review("no-trigger")
    fd = next(f for f in report["findings"] if f["rule"] == "SKILL-DESC-TRIGGER")
    assert fd["severity"] == "low"


def test_sibling_description_overlap(isolated_env):
    base = "Refines an agent skill by tightening scope, reasoning discipline, and clarity. "
    make_skill(isolated_env / "set", "pass-one", description=base + "Use when drafting.")
    make_skill(isolated_env / "set", "pass-two", description=base + "Use when polishing.")
    report = skill_review("set")
    assert report["skills_reviewed"] == ["pass-one", "pass-two"]
    assert "SKILL-DESC-OVERLAP" in rules(report)


def test_single_skill_compared_against_unreviewed_siblings(isolated_env):
    base = "Refines an agent skill by tightening scope, reasoning discipline, and clarity. "
    make_skill(isolated_env / "set", "target", description=base + "Use when drafting.")
    make_skill(isolated_env / "set", "sibling", description=base + "Use when polishing.")
    report = skill_review("set/target")
    assert report["skills_reviewed"] == ["target"]
    overlap = next(f for f in report["findings"] if f["rule"] == "SKILL-DESC-OVERLAP")
    assert "sibling" in overlap["message"]


def test_distinct_descriptions_do_not_overlap(isolated_env):
    make_skill(isolated_env / "set", "type-hints")
    make_skill(
        isolated_env / "set",
        "changelog",
        description="Generates a changelog entry from merged pull requests. "
        "Use when preparing a release or asked to update CHANGELOG.md.",
    )
    report = skill_review("set")
    assert "SKILL-DESC-OVERLAP" not in rules(report)


def test_broken_support_file_link(isolated_env):
    make_skill(
        isolated_env,
        "broken-ref",
        body="See [the rubric](references/rubric.md) for details.\n\nMore steps here.\n",
    )
    report = skill_review("broken-ref")
    fd = next(f for f in report["findings"] if f["rule"] == "SKILL-BROKEN-REF")
    assert "references/rubric.md" in fd["message"]


def test_existing_support_file_and_urls_ok(isolated_env):
    d = make_skill(
        isolated_env,
        "good-refs",
        body="See [rubric](references/rubric.md) and [docs](https://example.com/x).\n"
        "Extra line.\nExtra line.\n",
    )
    (d / "references").mkdir()
    (d / "references" / "rubric.md").write_text("rubric")
    report = skill_review("good-refs")
    assert "SKILL-BROKEN-REF" not in rules(report)


def test_body_length_and_empty_body(isolated_env):
    make_skill(isolated_env, "too-long", body="line\n" * 501)
    make_skill(isolated_env, "too-thin", body="\n")
    assert "SKILL-BODY-LENGTH" in rules(skill_review("too-long"))
    assert "SKILL-BODY-EMPTY" in rules(skill_review("too-thin"))


def test_filler_and_hype_are_info(isolated_env):
    make_skill(
        isolated_env,
        "fluffy",
        description="A powerful, comprehensive helper. Use when asked to do anything.",
        body="Think carefully and use your best judgment.\nThen do the work.\nDone.\n",
    )
    report = skill_review("fluffy")
    assert report["status"] == "pass"  # info/low findings only
    assert "SKILL-DESC-HYPE" in rules(report)
    assert "SKILL-BODY-FILLER" in rules(report)


def test_min_severity_filters_but_status_unchanged(isolated_env):
    make_skill(
        isolated_env,
        "fluffy",
        description="A powerful helper for tasks. Use when asked to do a task carefully.",
    )
    report = skill_review("fluffy", min_severity="medium")
    assert report["findings"] == []
    assert report["summary"]["info"] >= 1


def test_directory_without_skills_raises(isolated_env):
    (isolated_env / "empty").mkdir()
    with pytest.raises(ToolError, match="no SKILL.md"):
        skill_review("empty")


def test_path_outside_roots_rejected(isolated_env):
    with pytest.raises(ToolError, match="outside the auditable roots"):
        skill_review("/etc")


def test_bad_min_severity(isolated_env):
    make_skill(isolated_env, "type-hints")
    with pytest.raises(ToolError, match="min_severity"):
        skill_review("type-hints", min_severity="urgent")
