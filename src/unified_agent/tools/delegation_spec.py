"""Delegation Spec Standard: structural guardrails on delegated work (task #160).

WHY THIS EXISTS
---------------
Between 2026-07-22 and 07-26, four defects shipped through this cluster with
every gate green:

1. A browser extension MV3 content script that had been unloadable in Chrome since
   Phase 1 — `dist/content.js` was emitted with a top-level ESM import, which a
   classic-injected content script cannot execute, so the sensor layer never
   ran once.
2. A memory tag-escaping regression that silently dropped notes from
   memory_search and from their own tag index.
3. A cleanup tool that `rmtree`'d whatever it could not identify as a worktree.
4. The executor auth bypass (#82): a fabricated credential, a 401 swallowed
   into in-process dispatch, requirement 1 of Phase 4.2 failing open for days.

Every one of them passed its Definition of Done, because every DoD was some
variant of "the build passes". A green build proves the code compiles and that
the assertions someone wrote agree with the code someone wrote. It says nothing
about whether the artifact does its job — none of the four had a test that
exercised the actual capability.

So this module encodes one rule: **a DoD must name an observable behaviour, not
a build status.** `pytest` passing is necessary and is never sufficient.

WHAT IT IS NOT
--------------
Not a security boundary and not a quality oracle. It is a structural lint: it
checks that a delegation *states* how it will be proven, so a reviewer has
something to hold the work against. A spec can satisfy every rule here and still
describe bad work. The point is to make "the build passes" impossible to submit
as the whole answer.
"""

import os
import re

from mcp.server.mcpserver.exceptions import ToolError

# Sections every delegation to a developer agent must carry. Matched as markdown
# headings or bare uppercase labels, so the specs already written by hand in the
# task queue (OBJECTIVE / SCOPE / DEFINITION OF DONE / ...) validate as-is.
REQUIRED_SECTIONS = (
    "objective",
    "scope",
    "definition of done",
    "tdd",
    "boundaries",
)

# One leading bullet or numbered-list marker: "-", "*", "1.", "12)". Matches at
# most once, so it strips only an actual list prefix rather than any run of
# characters drawn from that set — the latter mangled ordinary structured
# values like ".github/workflows/ci.yml" (-> "github/workflows/ci.yml"),
# "123.py" (-> "py"), and a criterion starting with a status code like
# "401 Unauthorized" (-> "Unauthorized") (task #445 / PR #191 review).
_LIST_MARKER_RX = re.compile(r"^(?:[-*]|\d+[.)])\s+")

_SECTION_ALIASES = {
    "objective": ("objective", "goal"),
    "scope": ("scope", "files you may touch"),
    "definition of done": ("definition of done", "dod", "acceptance criteria", "verification"),
    "tdd": ("tdd", "tdd constraints", "tdd boundary", "test constraints"),
    "boundaries": ("boundaries", "out of scope", "non-goals", "do not"),
}

# Phrases that describe the toolchain agreeing with itself. A DoD built only
# from these is the exact pattern that let all four defects through.
_BUILD_STATUS_PATTERNS = (
    r"\bbuilds?\b",
    r"\bci\b",
    r"\bgates?\b",
    r"\bpytest\b",
    r"\bruff\b",
    r"\blint(?:er|ing)?\b",
    r"\bcompiles?\b",
    r"\btype[- ]?check\w*\b",
    r"\bno (?:new )?(?:errors?|warnings?)\b",
    r"\baudit pass(?:es)?\b",
    r"\ball tests? pass(?:es|ing)?\b",
    r"\btests? (?:are )?green\b",
)
_BUILD_STATUS_RE = re.compile("|".join(_BUILD_STATUS_PATTERNS), re.IGNORECASE)

# A behavioural criterion asserts something about what the artifact DOES. These
# are the verbs that showed up in the DoDs which would have caught the four
# defects ("loads in Chrome", "the note is returned by memory_search", "refuses
# to remove a directory it cannot identify", "a 401 never reaches local
# dispatch").
_BEHAVIOURAL_PATTERNS = (
    r"\bassert\w*\b",
    r"\bverif\w+\b",
    r"\breproduc\w+\b",
    r"\bexercis\w+\b",
    r"\bload\w*\b",
    r"\brun\w*\b",
    r"\breturn\w*\b",
    r"\brefus\w+\b",
    r"\breject\w*\b",
    r"\bnever\b",
    r"\bmust not\b",
    r"\bfails? (?:closed|with)\b",
    r"\bround[- ]?trip\b",
    r"\bend[- ]to[- ]end\b",
    r"\bobserv\w+\b",
    r"\bbyte[- ]identical\b",
    r"\bunchanged\b",
)
_BEHAVIOURAL_RE = re.compile("|".join(_BEHAVIOURAL_PATTERNS), re.IGNORECASE)

_MIN_DOD_CRITERIA = 2

DELEGATION_SPEC_TEMPLATE = """\
### Objective
<What must be true afterwards that is not true now. One paragraph.>

### Scope
Files this delegation may touch:
- <path>
- <path>

### Definition of Done
At least one criterion must name an OBSERVABLE BEHAVIOUR of the artifact.
"the build passes" is necessary and never sufficient — every defect this
standard exists to prevent shipped with a green build.
- <the capability, exercised: what is called, with what input, and what is
  asserted about the result>
- <the failure mode, exercised: what goes wrong, and what the code does about
  it — assert the refusal, not just the happy path>
- Gates: uv run pytest, ruff check, ruff format --check, unified-agent-audit

### TDD Constraints
The test suite is GROUND TRUTH. Tests may not be muted, skipped, deleted, or
weakened to make a change pass. A test that appears wrong is an escalation, not
an edit. State here what may be stubbed and what must be real:
- Stub: <e.g. the HTTP layer, the clock>
- Must be real: <e.g. the git repository, the SQLite schema>

### Boundaries
- <what this delegation must NOT change>
- <the restrictive-direction rule: which invariant may not be relaxed to make
  the work easier>
"""


def _has_section(spec: str, section: str) -> bool:
    for alias in _SECTION_ALIASES[section]:
        # A heading (### Foo), a bare uppercase label (FOO), or "Foo:".
        pattern = rf"(?:^|\n)\s*(?:#{{1,6}}\s*|\*\*)?{re.escape(alias)}\b"
        if re.search(pattern, spec, re.IGNORECASE):
            return True
    return False


def _dod_body(spec: str) -> str:
    """Text between the Definition of Done heading and the next heading.

    Returns "" for a heading with nothing under it — an empty DoD is a distinct
    failure from a missing one, so this must not run on into the next section
    looking for content.
    """
    next_heading = r"(?=\n\s*(?:#{1,6}\s*[A-Za-z]|\*\*[A-Za-z]|[A-Z][A-Z /]{3,}(?:\n|\s*\()))"
    for alias in _SECTION_ALIASES["definition of done"]:
        m = re.search(
            # Only spaces/tabs may follow the heading text before its newline —
            # \s would swallow the blank line of an empty section and start the
            # body at the next heading instead of before it.
            rf"(?:^|\n)[ \t]*(?:#{{1,6}}[ \t]*|\*\*)?{re.escape(alias)}\b[:*\t ]*\r?\n(.*?)"
            rf"(?:{next_heading}|\Z)",
            spec,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            return m.group(1)
    return ""


def _criteria(body: str) -> list[str]:
    """Bullet/numbered lines in a DoD body, else its non-empty lines."""
    bullets = [
        ln.strip().lstrip("-*0123456789.) ").strip()
        for ln in body.splitlines()
        if re.match(r"\s*(?:[-*]|\d+[.)])\s+\S", ln)
    ]
    if bullets:
        return [b for b in bullets if b]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def delegation_spec_check(task_spec: str) -> dict:
    """Lint a delegation spec against the Delegation Spec Standard (task #160).

    Read-only and advisory by itself — `delegate` is what enforces it. Checks
    that the spec carries the required sections and, crucially, that its
    Definition of Done names at least one OBSERVABLE BEHAVIOUR rather than only
    build/lint/test status.

    That last rule is the whole point. Four defects shipped between 2026-07-22
    and 07-26 with every gate green — an unloadable browser content script, a
    silent memory-index regression, a cleanup tool that deleted what it could
    not identify, and the executor auth bypass — because every DoD was a variant
    of "the build passes". A green build says the code compiles and that the
    assertions someone wrote agree with the code someone wrote; it says nothing
    about whether the artifact does its job.

    Returns {ok, errors, warnings, sections_present, sections_missing,
    dod_criteria, behavioural_criteria}.
    """
    spec = (task_spec or "").strip()
    errors: list[str] = []
    warnings: list[str] = []

    if not spec:
        return {
            "ok": False,
            "errors": ["task_spec is empty"],
            "warnings": [],
            "sections_present": [],
            "sections_missing": list(REQUIRED_SECTIONS),
            "dod_criteria": 0,
            "behavioural_criteria": 0,
        }

    present = [s for s in REQUIRED_SECTIONS if _has_section(spec, s)]
    missing = [s for s in REQUIRED_SECTIONS if s not in present]
    if missing:
        errors.append(
            "missing required section(s): "
            + ", ".join(missing)
            + " — see delegation_spec_template()"
        )

    body = _dod_body(spec)
    criteria = _criteria(body)
    # Build status takes precedence: "uv run pytest green" is a gate, not a
    # behaviour, even though it contains the verb "run". Classifying it the
    # other way is how a DoD of pure gates would sneak through.
    build_only = [c for c in criteria if _BUILD_STATUS_RE.search(c)]
    behavioural = [c for c in criteria if c not in build_only and _BEHAVIOURAL_RE.search(c)]

    if "definition of done" in present:
        if not criteria:
            errors.append("Definition of Done section is empty")
        elif not behavioural:
            errors.append(
                "Definition of Done names no observable behaviour — every criterion is "
                "build/lint/test status. A green build proves the code compiles and that "
                "the assertions someone wrote agree with the code someone wrote; it does "
                "not prove the artifact does its job. State what is exercised and what is "
                "asserted about the result."
            )
        elif len(criteria) < _MIN_DOD_CRITERIA:
            warnings.append(
                f"Definition of Done has only {len(criteria)} criterion — consider also "
                "stating the failure mode that must be exercised, not just the happy path"
            )
        if build_only and len(build_only) == len(criteria):
            # Defensive: covered by the `not behavioural` branch above.
            errors.append("Definition of Done is build status only")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "sections_present": present,
        "sections_missing": missing,
        "dod_criteria": len(criteria),
        "behavioural_criteria": len(behavioural),
    }


def delegation_spec_template() -> dict:
    """Return the canonical delegation spec template (task #160).

    Paste it into `delegate(task_spec=...)` and fill it in. The sections are
    what `delegation_spec_check` validates.
    Returns {template, required_sections}.
    """
    return {
        "template": DELEGATION_SPEC_TEMPLATE,
        "required_sections": list(REQUIRED_SECTIONS),
    }


def spec_enforcement_enabled() -> bool:
    """Whether `delegate` refuses a spec that fails the standard.

    On by default. Set UA_DELEGATION_SPEC_ENFORCE=0 to downgrade to a warning —
    an escape hatch for scripted/legacy callers, not a default. Turning it off
    restores exactly the condition the four 07-22→07-26 defects shipped under.
    """
    return os.environ.get("UA_DELEGATION_SPEC_ENFORCE", "1") != "0"


def enforce_delegation_spec(task_spec: str, to_agent: str) -> dict:
    """Gate used by `delegate`: raise on a spec that fails the standard.

    Returns the check result so the caller can surface warnings. Raises
    ToolError listing every problem at once — a delegation rejected one line at
    a time is worse than no check.
    """
    result = delegation_spec_check(task_spec)
    if result["ok"] or not spec_enforcement_enabled():
        return result
    raise ToolError(
        f"delegation to {to_agent!r} does not meet the Delegation Spec Standard:\n  - "
        + "\n  - ".join(result["errors"])
        + "\n\nCall delegation_spec_template() for the canonical shape. "
        "(UA_DELEGATION_SPEC_ENFORCE=0 downgrades this to a warning.)"
    )


def delegation_spec_build(
    objective: str,
    scope_files: list[str] | str | None = None,
    dod_criteria: list[str] | str | None = None,
    tdd_stub: list[str] | str | None = None,
    tdd_must_be_real: list[str] | str | None = None,
    boundaries: list[str] | str | None = None,
    include_gates: bool = True,
    auto_validate: bool = True,
) -> dict:
    """Assemble and validate a Delegation Spec from structured arguments.

    Generates a markdown specification matching the Delegation Spec Standard (task #160).
    """
    obj = (objective or "").strip()
    if not obj:
        raise ToolError("objective is required")

    def _strip_list_marker(s: str) -> str:
        return _LIST_MARKER_RX.sub("", s.strip(), count=1).strip()

    def _normalize_list(val: list[str] | str | None) -> list[str]:
        if val is None:
            return []
        if isinstance(val, str):
            lines = [_strip_list_marker(ln) for ln in val.splitlines()]
            return [ln for ln in lines if ln]
        if isinstance(val, list):
            out = []
            for item in val:
                if isinstance(item, str) and item.strip():
                    out.append(_strip_list_marker(item))
            return out
        return []

    files = _normalize_list(scope_files)
    dods = _normalize_list(dod_criteria)
    stubs = _normalize_list(tdd_stub)
    reals = _normalize_list(tdd_must_be_real)
    bounds = _normalize_list(boundaries)

    lines: list[str] = [
        "### Objective",
        obj,
        "",
        "### Scope",
        "Files this delegation may touch:",
    ]
    if files:
        for f in files:
            lines.append(f"- {f}")
    else:
        lines.append("- (Unrestricted or specified in task prompt)")
    lines.append("")

    lines.append("### Definition of Done")
    if dods:
        for d in dods:
            lines.append(f"- {d}")
    if include_gates:
        lines.append("- Gates: uv run pytest, ruff check, ruff format --check, unified-agent-audit")
    lines.append("")

    lines.append("### TDD Constraints")
    lines.append(
        "The test suite is GROUND TRUTH. Tests may not be muted, skipped, deleted, or weakened to make a change pass."
    )
    if stubs:
        lines.append(f"- Stub: {', '.join(stubs)}")
    else:
        lines.append("- Stub: none")
    if reals:
        lines.append(f"- Must be real: {', '.join(reals)}")
    else:
        lines.append("- Must be real: whole test harness and persistent storage")
    lines.append("")

    lines.append("### Boundaries")
    if bounds:
        for b in bounds:
            lines.append(f"- {b}")
    else:
        lines.append("- Do not relax existing test assertions, security gates, or allowlists.")

    spec = "\n".join(lines).strip() + "\n"
    check_result = delegation_spec_check(spec)

    if auto_validate and not check_result["ok"]:
        raise ToolError(
            "Generated delegation spec failed validation:\n  - "
            + "\n  - ".join(check_result["errors"])
        )

    return {
        "spec": spec,
        "ok": check_result["ok"],
        "check_result": check_result,
    }
