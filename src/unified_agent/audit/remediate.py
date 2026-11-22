"""Auto-remediation logic for security audit findings.

Applies safe, non-destructive file updates (e.g. creating .gitignore, appending env patterns,
and stripping hidden Unicode characters).
"""

from pathlib import Path

from .rules import HIDDEN_CHAR_RX, Finding
from .safe_read import read_regular_text, write_regular_text

DEFAULT_GITIGNORE = """# Standard .gitignore created by unified-agent auto-remediation
.env*
__pycache__/
*.py[cod]
*$py.class
.venv/
venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.DS_Store
"""

DEFAULT_CLAUDE_MD = """# CLAUDE.md

Guidelines for AI assistants working in this repository.

## Build and Test Commands
- Build/Install: `uv sync` (or custom install command)
- Run Tests: `uv run pytest` (or custom test command)

## Development Constraints
- Follow standard style guidelines.
- Do not disable confirmation settings or bypass safety rail permissions.
"""


def remediate_findings(root: Path, findings: list[Finding]) -> list[Finding]:
    """Apply safe automated fixes for specific security audit findings.

    Every read and write goes through `safe_read`, so a FIFO, socket or
    device at a target path (static, or swapped in after the audit read it)
    leaves the finding unfixed with an [AUTO-FIX-FAILED] message instead of
    blocking the audit (#2212)."""
    for fd in findings:
        if fd.fixed:
            continue

        if fd.rule == "HYGIENE-NO-GITIGNORE":
            try:
                gitignore_path = root / ".gitignore"
                write_regular_text(gitignore_path, DEFAULT_GITIGNORE)
                fd.fixed = True
                fd.message = f"[AUTO-FIXED] Created default .gitignore: {fd.message}"
            except Exception as e:
                fd.message = f"[AUTO-FIX-FAILED] Failed to create .gitignore: {e}. {fd.message}"

        elif fd.rule == "SECRETS-ENV-EXPOSED":
            try:
                gitignore_path = root / ".gitignore"
                if gitignore_path.exists():
                    current = read_regular_text(gitignore_path, errors="replace")
                    if current is None:
                        raise OSError(f"{gitignore_path} is not a readable regular file")
                    # Check if already has .env pattern to avoid duplicates
                    if not any(pat in current for pat in (".env*", "*.env", ".env")):
                        new_content = (
                            current + "\n# Added by unified-agent auto-remediation\n.env*\n"
                        )
                        write_regular_text(gitignore_path, new_content)
                else:
                    write_regular_text(gitignore_path, DEFAULT_GITIGNORE)
                fd.fixed = True
                fd.message = f"[AUTO-FIXED] Added `.env*` to .gitignore: {fd.message}"
            except Exception as e:
                fd.message = f"[AUTO-FIX-FAILED] Failed to update .gitignore: {e}. {fd.message}"

        elif fd.rule == "AGENT-HIDDEN-UNICODE":
            if fd.path:
                try:
                    file_path = root / fd.path
                    if file_path.is_file():
                        original_text = read_regular_text(file_path, errors="replace")
                        if original_text is None:
                            raise OSError(f"{file_path} is not a readable regular file")
                        cleaned_text = HIDDEN_CHAR_RX.sub("", original_text)
                        if cleaned_text != original_text:
                            write_regular_text(file_path, cleaned_text)
                            fd.fixed = True
                            fd.message = f"[AUTO-FIXED] Stripped hidden/bidi Unicode prompt injection characters: {fd.message}"
                except Exception as e:
                    fd.message = (
                        f"[AUTO-FIX-FAILED] Failed to clean hidden Unicode: {e}. {fd.message}"
                    )

        elif fd.rule == "AGENT-NO-INSTRUCTIONS":
            try:
                claude_md_path = root / "CLAUDE.md"
                # Double check that no other instruction file exists before writing template
                instruction_exists = False
                for f in ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules"):
                    if (root / f).is_file():
                        instruction_exists = True
                        break
                if not instruction_exists:
                    write_regular_text(claude_md_path, DEFAULT_CLAUDE_MD)
                    fd.fixed = True
                    fd.message = (
                        f"[AUTO-FIXED] Created default instruction file CLAUDE.md: {fd.message}"
                    )
            except Exception as e:
                fd.message = f"[AUTO-FIX-FAILED] Failed to create CLAUDE.md: {e}. {fd.message}"

    return findings
