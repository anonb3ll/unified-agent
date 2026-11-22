"""Skill-review tool: rule-based quality checks for agent skills (SKILL.md files).

Deterministic complement to a judgment review. Hosts load only the frontmatter
``name`` and ``description`` before invocation, so the description is the entire
trigger surface and descriptions compete with sibling skills; the body is read
only after the skill fires. The rules here verify that contract: frontmatter
validity, description trigger quality and sibling overlap, body size, and
support-file reference hygiene.
"""

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from ..audit.rules import SEVERITIES, Finding
from ..safety import resolve_for_audit

NAME_MAX = 64
DESC_MAX = 1024
DESC_MIN = 20
BODY_MAX_LINES = 500
BODY_MIN_CONTENT_LINES = 3
MAX_SKILLS = 100
OVERLAP_THRESHOLD = 0.4

KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TRIGGER_RE = re.compile(
    r"\buse\s+(when|this|it|for)\b|\bwhen\s+(the\s+user|asked|you)\b|\btrigger\b", re.I
)
FIRST_PERSON_RE = re.compile(r"\bI\b|\bmy\b")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#\s][^)]*)\)")

HYPE_WORDS = (
    "powerful",
    "comprehensive",
    "advanced",
    "robust",
    "seamless",
    "state-of-the-art",
    "best-in-class",
    "cutting-edge",
)

FILLER_PHRASES = (
    "think carefully",
    "use your best judgment",
    "be smart",
    "be thorough",
    "do your best",
    "high-quality output",
)

# Words too common in skill descriptions to signal genuine overlap.
STOPWORDS = frozenset(
    "a an the to for of in on and or with when where while use uses using used this that "
    "these those it its is are be been was were as by from at into your you skill skills "
    "file files user users asked want wants".split()
)

_SEV_RANK = {sev: i for i, sev in enumerate(SEVERITIES)}


@dataclass
class ParsedSkill:
    dir: Path
    relpath: str  # SKILL.md path shown in findings
    meta: dict = field(default_factory=dict)
    meta_error: str | None = None
    body: str = ""
    body_start: int = 1  # 1-based line where the body begins
    fm_lines: list[str] = field(default_factory=list)

    @property
    def name(self) -> str | None:
        v = self.meta.get("name")
        return v if isinstance(v, str) else None

    @property
    def description(self) -> str | None:
        v = self.meta.get("description")
        return v if isinstance(v, str) else None


def _parse_skill(skill_dir: Path, relpath: str) -> ParsedSkill:
    text = (skill_dir / "SKILL.md").read_text(errors="replace")
    skill = ParsedSkill(dir=skill_dir, relpath=relpath)
    if not text.startswith("---\n"):
        skill.meta_error = "file does not start with a `---` YAML frontmatter block"
        skill.body = text
        return skill
    end = text.find("\n---\n", 4)
    if end == -1:
        skill.meta_error = "frontmatter block is never closed with `---`"
        skill.body = text
        return skill
    fm_text = text[4:end]
    skill.fm_lines = fm_text.splitlines()
    skill.body = text[end + 5 :]
    skill.body_start = len(skill.fm_lines) + 3
    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        skill.meta_error = f"frontmatter is not valid YAML: {e}"
        return skill
    if not isinstance(meta, dict):
        skill.meta_error = "frontmatter is not a YAML mapping"
        return skill
    skill.meta = meta
    return skill


def _meta_line(skill: ParsedSkill, key: str) -> int | None:
    """1-based line number of a top-level frontmatter key, for clickable findings."""
    for i, line in enumerate(skill.fm_lines, 2):  # line 1 is the opening ---
        if line.startswith(f"{key}:"):
            return i
    return None


def _desc_tokens(description: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9']+", description.lower())
        if len(w) > 2 and w not in STOPWORDS
    }


def _check_frontmatter(skill: ParsedSkill):
    if skill.meta_error:
        yield Finding(
            rule="SKILL-FRONTMATTER",
            severity="high",
            path=skill.relpath,
            line=1,
            message=skill.meta_error,
            fix="add a YAML frontmatter block with `name` and `description`; hosts load "
            "only those two fields to decide when to invoke the skill",
        )
        return
    for key in ("name", "description"):
        if not getattr(skill, key):
            yield Finding(
                rule="SKILL-FRONTMATTER",
                severity="high",
                path=skill.relpath,
                line=1,
                message=f"frontmatter is missing `{key}` (or it is not a string)",
                fix=f"add a `{key}:` field; without it the skill cannot be matched to requests",
            )


def _check_name(skill: ParsedSkill):
    name = skill.name
    if name is None:
        return
    line = _meta_line(skill, "name")
    if name != skill.dir.name:
        yield Finding(
            rule="SKILL-NAME",
            severity="medium",
            path=skill.relpath,
            line=line,
            message=f"name {name!r} does not match its directory {skill.dir.name!r}",
            fix="rename the directory or the `name` field so they match exactly",
        )
    if not KEBAB_RE.match(name):
        yield Finding(
            rule="SKILL-NAME",
            severity="medium",
            path=skill.relpath,
            line=line,
            message=f"name {name!r} is not kebab-case (lowercase letters, digits, hyphens)",
            fix="use a kebab-case name, e.g. `skill-review`",
        )
    if len(name) > NAME_MAX:
        yield Finding(
            rule="SKILL-NAME",
            severity="medium",
            path=skill.relpath,
            line=line,
            message=f"name is {len(name)} chars (limit {NAME_MAX})",
            fix="shorten the name",
        )


def _check_description(skill: ParsedSkill):
    desc = skill.description
    if desc is None:
        return
    line = _meta_line(skill, "description")
    if len(desc) > DESC_MAX:
        yield Finding(
            rule="SKILL-DESC-LENGTH",
            severity="medium",
            path=skill.relpath,
            line=line,
            message=f"description is {len(desc)} chars (limit {DESC_MAX})",
            fix="tighten the description; keep what it does plus concrete trigger scenarios",
        )
    elif len(desc) < DESC_MIN:
        yield Finding(
            rule="SKILL-DESC-LENGTH",
            severity="low",
            path=skill.relpath,
            line=line,
            message=f"description is only {len(desc)} chars — too thin to carry trigger vocabulary",
            fix="state what the skill does and when to use it, with words a user would type",
        )
    if not TRIGGER_RE.search(desc):
        buried = bool(TRIGGER_RE.search(skill.body))
        yield Finding(
            rule="SKILL-DESC-TRIGGER",
            severity="medium" if buried else "low",
            path=skill.relpath,
            line=line,
            message=(
                "trigger guidance ('use when …') lives in the body but not the description; "
                "the body is only loaded after invocation, so it cannot help triggering"
                if buried
                else "description has no 'Use when …' trigger guidance"
            ),
            fix="move/add concrete trigger scenarios into the `description` field",
        )
    if FIRST_PERSON_RE.search(desc):
        yield Finding(
            rule="SKILL-DESC-VOICE",
            severity="low",
            path=skill.relpath,
            line=line,
            message="description uses first person; hosts match descriptions written "
            "in third person",
            fix="rewrite as 'Reviews …. Use when …', not 'I review …'",
        )
    hits = [w for w in HYPE_WORDS if w in desc.lower()]
    if hits:
        yield Finding(
            rule="SKILL-DESC-HYPE",
            severity="info",
            path=skill.relpath,
            line=line,
            message=f"quality adjectives do not help request matching: {', '.join(hits)}",
            fix="replace adjectives with concrete tasks and trigger phrases",
        )


def _check_body(skill: ParsedSkill):
    lines = skill.body.splitlines()
    content_lines = [ln for ln in lines if ln.strip()]
    if len(lines) > BODY_MAX_LINES:
        yield Finding(
            rule="SKILL-BODY-LENGTH",
            severity="medium",
            path=skill.relpath,
            line=skill.body_start,
            message=f"body is {len(lines)} lines (guideline ≤{BODY_MAX_LINES})",
            fix="move reference material into support files the body points to, loaded on demand",
        )
    if len(content_lines) < BODY_MIN_CONTENT_LINES and not skill.meta_error:
        yield Finding(
            rule="SKILL-BODY-EMPTY",
            severity="medium",
            path=skill.relpath,
            line=skill.body_start,
            message=f"body has {len(content_lines)} non-blank lines — no payload beyond "
            "the description",
            fix="add the concrete instructions the skill exists to deliver, or delete it",
        )
    lower_lines = [ln.lower() for ln in lines]
    for phrase in FILLER_PHRASES:
        for i, ln in enumerate(lower_lines):
            if phrase in ln:
                yield Finding(
                    rule="SKILL-BODY-FILLER",
                    severity="info",
                    path=skill.relpath,
                    line=skill.body_start + i,
                    message=f"generic filler {phrase!r} restates default model behavior",
                    fix="delete it or replace with an operational instruction",
                )
                break  # one finding per phrase is enough
    for i, ln in enumerate(lines):
        for m in MD_LINK_RE.finditer(ln):
            target = m.group(1).strip()
            if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("/"):
                continue  # URL, mailto:, or absolute path — not a support file
            if not (skill.dir / target).exists():
                yield Finding(
                    rule="SKILL-BROKEN-REF",
                    severity="medium",
                    path=skill.relpath,
                    line=skill.body_start + i,
                    message=f"linked support file {target!r} does not exist",
                    fix="fix the link or add the file; broken references break "
                    "progressive disclosure",
                )


def _check_overlap(pairs):
    for a, b in pairs:
        ta, tb = _desc_tokens(a.description or ""), _desc_tokens(b.description or "")
        if len(ta) < 8 or len(tb) < 8:
            continue
        similarity = len(ta & tb) / len(ta | tb)
        if similarity >= OVERLAP_THRESHOLD:
            yield Finding(
                rule="SKILL-DESC-OVERLAP",
                severity="medium",
                path=a.relpath,
                line=_meta_line(a, "description"),
                message=f"description overlaps sibling skill {b.dir.name!r} "
                f"({similarity:.0%} shared vocabulary); invocation between them "
                "is unreliable",
                fix="differentiate the two descriptions and state the boundary "
                "('not for X — use Y for that')",
            )


def _collect_skill_dirs(root: Path, path: str) -> tuple[list[Path], Path]:
    """Return (skill dirs to review, base dir findings paths are relative to)."""
    if root.is_file():
        if root.name != "SKILL.md":
            raise ToolError(f"{path!r} is a file but not a SKILL.md")
        return [root.parent], root.parent
    if not root.is_dir():
        raise ToolError(f"not found: {path!r}")
    if (root / "SKILL.md").is_file():
        return [root], root
    children = sorted(d for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
    if not children:
        raise ToolError(f"no SKILL.md found at {path!r} or in its immediate subdirectories")
    return children, root


def skill_review(path: str = ".", min_severity: str = "info") -> dict:
    """Review agent skills (SKILL.md) against auto-invocation and payload rules (read-only).

    Checks: frontmatter validity (name/description present, kebab-case name ≤64 chars
    matching the directory, description ≤1024 chars), trigger quality (description
    carries 'use when …' vocabulary — the body is only loaded after invocation),
    description overlap between sibling skills (invocation is competitive), body size
    (≤500-line guideline, non-empty payload), filler phrases, and broken support-file
    links.

    `path` may be a SKILL.md file, one skill directory, or a directory of skill
    directories; workspace-relative or absolute under UA_AUDIT_ROOTS. A single skill
    is also compared against its sibling directories. `min_severity` filters findings
    (high|medium|low|info). Returns {root, skills_reviewed, status, summary, findings,
    truncated}; status is fail (any high) / warn (any medium) / pass. Reviews ≤100
    skills per call.
    """
    if min_severity not in SEVERITIES:
        raise ToolError(f"min_severity must be one of {', '.join(SEVERITIES)}")
    root = resolve_for_audit(path)
    skill_dirs, base = _collect_skill_dirs(root, path)
    return _review_dirs(skill_dirs, base, min_severity)


def review_skill_dir(skill_dir: Path, min_severity: str = "info") -> dict:
    """Review one skill directory the caller has already authorized.

    No sandbox resolution: for internal callers that materialized the package
    themselves (the git skill-source importer, task #1750) and must never be
    reachable with a caller-supplied path. Siblings are compared as usual, so
    pass a directory whose parent holds only this skill.
    """
    if min_severity not in SEVERITIES:
        raise ToolError(f"min_severity must be one of {', '.join(SEVERITIES)}")
    if not (skill_dir / "SKILL.md").is_file():
        raise ToolError("no SKILL.md in the materialized skill directory")
    return _review_dirs([skill_dir], skill_dir, min_severity)


def _review_dirs(skill_dirs: list[Path], base: Path, min_severity: str) -> dict:
    truncated = len(skill_dirs) > MAX_SKILLS
    skill_dirs = skill_dirs[:MAX_SKILLS]

    def rel(d: Path) -> str:
        md = d / "SKILL.md"
        return md.relative_to(base).as_posix() if md.is_relative_to(base) else str(md)

    skills = [_parse_skill(d, rel(d)) for d in skill_dirs]

    findings: list[Finding] = []
    for skill in skills:
        findings.extend(_check_frontmatter(skill))
        findings.extend(_check_name(skill))
        findings.extend(_check_description(skill))
        findings.extend(_check_body(skill))

    if len(skills) > 1:
        pairs = itertools.combinations(skills, 2)
    else:
        # Single skill: compare against unreviewed siblings, since their
        # descriptions compete with it at invocation time.
        target = skills[0]
        siblings = [
            _parse_skill(s, rel(s))
            for s in sorted(target.dir.parent.iterdir())
            if s.is_dir() and s != target.dir and (s / "SKILL.md").is_file()
        ]
        pairs = ((target, s) for s in siblings)
    findings.extend(_check_overlap(pairs))

    findings.sort(key=lambda fd: (_SEV_RANK[fd.severity], fd.path or "", fd.line or 0))
    summary = {sev: 0 for sev in SEVERITIES}
    for fd in findings:
        summary[fd.severity] += 1
    status = "fail" if summary["high"] else ("warn" if summary["medium"] else "pass")
    cutoff = _SEV_RANK[min_severity]
    return {
        "root": str(base),
        "skills_reviewed": [s.dir.name for s in skills],
        "status": status,
        "summary": summary,
        "findings": [fd.to_dict() for fd in findings if _SEV_RANK[fd.severity] <= cutoff],
        "truncated": truncated,
    }
