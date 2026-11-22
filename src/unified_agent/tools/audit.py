"""Security-audit tool: read-only best-practice checks for AI-assisted projects."""

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, trust_presets
from .. import injection_triage as injection_triage_lib
from ..audit import run_audit
from ..audit.rules import SEVERITIES
from ..audit.triage import file_findings_as_tasks
from ..safety import get_active_worktree_path, resolve_for_audit, resolve_in_vault

_SEV_RANK = {sev: i for i, sev in enumerate(SEVERITIES)}
VAULT_AUDIT_DEFAULT_EXCLUDES = (
    "00 - Inbox/Security Audit *.md",
    "04 - Archive/**/Security Audit *.md",
)


def _under_vault(path) -> bool:
    vault = config.vault_root()
    if vault is None:
        return False
    root = vault.expanduser().resolve()
    return path == root or path.is_relative_to(root)


def _deny_binding_active() -> bool:
    return isinstance(config.workspace_root_var.get(), config.WorkspaceDenied)


def _under_workspace(path) -> bool:
    """True when ``path`` is inside the workspace or the active task worktree.

    Remediate writes follow the same union ``resolve_in_workspace`` uses.
    ``UA_AUDIT_ROOTS`` stays read-only unless the path is that active worktree.
    """
    resolved = path.resolve()
    roots = [config.workspace_root().resolve()]
    worktree = get_active_worktree_path()
    if worktree is not None:
        roots.append(worktree.resolve())
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def _vault_audit_excludes(root, excludes: list[str] | None) -> list[str]:
    # Keep the false-positive carve-out in the vault wrapper, not the engine:
    # only generated steward reports get skipped by default, and caller excludes
    # remain additive.
    combined = [*VAULT_AUDIT_DEFAULT_EXCLUDES, *(excludes or [])]
    vault = config.vault_root()
    if vault is None:
        return combined
    base = root.parent if root.is_file() else root
    vault_root = vault.expanduser().resolve()
    for pattern in VAULT_AUDIT_DEFAULT_EXCLUDES:
        for match in vault_root.glob(pattern):
            try:
                trust_presets.check_path(match.resolve())
            except ToolError:
                continue
            if match.is_file() and match.is_relative_to(base):
                combined.append(match.relative_to(base).as_posix())
    return combined


def security_audit(
    path: str = ".",
    min_severity: str = "info",
    remediate: bool = False,
    excludes: list[str] | None = None,
    vault: bool = False,
) -> dict:
    """Audit a project — or, with `vault=True`, a vault note/directory — for
    AI-development security best practices.

    Checks: committed secrets/keys, exposed .env files, permissive agent settings
    (bypassPermissions, broad Bash allows), risky hooks, inline secrets or unpinned
    packages in .mcp.json, hidden-Unicode prompt injection in instruction files,
    eval/exec/shell=True near LLM SDK usage, lockfile/.gitignore hygiene, and (for
    any .md/.txt/extensionless document) injection-keyword, hidden-Unicode, and
    base64-blob patterns that suggest untrusted/injected content.

    If `remediate` is True, applies safe auto-fixes (creating/updating .gitignore,
    creating CLAUDE.md, stripping hidden Unicode prompt injection characters).
    Not available with `vault=True` — vault audits are always read-only.
    Also refused when the caller's ``UA_AGENT_WORKSPACES`` binding is DENY, or
    when the resolved path sits under the Obsidian vault (even via
    ``resolve_for_audit``), so DENY/vault widening never becomes a write path.
    Remediate is confined to ``workspace_root()`` or the active task worktree.
    A path that is only reachable through ``UA_AUDIT_ROOTS`` stays read-only.

    `path` may be a file or a directory. Without `vault`, it resolves workspace-
    relative or as an absolute path under one of the roots in UA_AUDIT_ROOTS
    (the vault root is always auditable when configured). With `vault=True`, it
    resolves inside the Obsidian vault instead (the same read boundary as
    `vault_read` — anywhere except dot-directories) — use this to
    pre-ingest-scan `00 - Inbox` captures before wiki ingestion. Vault audits skip
    the repo-hygiene checks (.gitignore, lockfiles, agent settings, .mcp.json)
    since those assume a code-project tree, not vault content. Vault-root audits
    also skip generated steward `Security Audit *.md` reports in `00 - Inbox`
    and `04 - Archive`, because they quote prior audit findings and otherwise
    bury genuinely new vault findings.

    `min_severity` filters findings (high|medium|low|info). `excludes` is a list
    of glob patterns of paths to exclude.
    Returns {root, files_scanned, status, summary, findings, findings_total, truncated, truncated_by}; status is
    fail (any high) / warn (any medium) / pass. Lines containing `ua:allow` are
    exempt. Scans ≤5000 files, skips binaries and files >1 MB.
    """
    if min_severity not in SEVERITIES:
        raise ToolError(f"min_severity must be one of {', '.join(SEVERITIES)}")
    if vault:
        if remediate:
            raise ToolError("remediate is not supported for vault-scoped audits (vault=True)")
        root = resolve_in_vault(path)
        include_repo_rules = False
        excludes = _vault_audit_excludes(root, excludes)
    else:
        root = resolve_for_audit(path, for_write=remediate)
        include_repo_rules = not _under_vault(root)
    if not root.exists():
        raise ToolError(f"path {path!r} does not exist")
    if remediate and root.is_file():
        raise ToolError("remediate requires a directory path, not a single file")
    if remediate and _deny_binding_active():
        raise ToolError(
            "remediate is not available under a UA_AGENT_WORKSPACES DENY binding; "
            "security_audit is read-only for this identity"
        )
    if remediate and _under_vault(root):
        raise ToolError(
            "remediate is not supported for vault paths; vault audits are always read-only"
        )
    if remediate and not _under_workspace(root):
        raise ToolError(
            "remediate only writes inside the workspace or the active task worktree; "
            "UA_AUDIT_ROOTS stays read-only"
        )
    # The audit engine and repository rules follow file symlinks. Validate
    # each actual target before entering that engine when a preset is selected;
    # its broad audit roots must not become a way around the tighter scope.
    if trust_presets.selection() is not None and root.is_dir():
        for entry in root.rglob("*"):
            trust_presets.check_path(entry.resolve(), for_write=remediate)
    report = run_audit(
        root, remediate=remediate, excludes=excludes, include_repo_rules=include_repo_rules
    )
    cutoff = _SEV_RANK[min_severity]
    report["findings"] = [fd for fd in report["findings"] if _SEV_RANK[fd["severity"]] <= cutoff]
    return report


def injection_triage(
    path: str = ".",
    vault: bool = False,
    excludes: list[str] | None = None,
) -> dict:
    """Rank a document's INJECTION-* audit findings by how likely each is to be a
    real injection attempt rather than documentation that quotes one.

    **Advisory only — this decides nothing.** It runs `security_audit`, keeps
    every finding exactly as reported, and adds an ordering. The fail-closed
    rule is unchanged and lives with the caller: in steward-inbox, *any* finding
    still quarantines the capture. Use this to decide what to read first in a
    queue of quarantined captures, never to decide what to skip.

    Scoring is deterministic (no model call, no network) over signals like
    second-person imperative mood, exfiltration shape (a data-moving verb near a
    URL/address), and whether the match sits inside a code fence, a blockquote,
    or example framing. Hidden-Unicode findings are pinned high and are exempt
    from every de-escalating signal.

    Every de-escalating signal is forgeable — a hostile capture can wrap its
    payload in a code fence and score lower. That is tolerable because the score
    changes only review order, and it is why no band is named "safe": the lowest
    is `review-later`, and scores are floored above zero. Do not promote this
    into a gate.

    Returns {status, files_scanned, summary, findings, verdicts, note} where
    `verdicts` is highest-score-first and `findings` is the untouched audit
    output. `status` is passed through from the audit — triage never changes it.
    """
    report = security_audit(path=path, vault=vault, excludes=excludes)
    findings = report.get("findings", [])

    root = resolve_in_vault(path) if vault else resolve_for_audit(path)
    texts: dict[str, str] = {}
    for finding in findings:
        rel = finding.get("path")
        if not rel or rel in texts:
            continue
        candidate = root / rel if root.is_dir() else root
        try:
            texts[rel] = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            texts[rel] = ""  # unreadable: scored without context, never dropped

    verdicts = injection_triage_lib.triage(findings, texts)
    return {
        "status": report.get("status"),
        "files_scanned": report.get("files_scanned"),
        "summary": injection_triage_lib.summarize(verdicts),
        "findings": findings,
        "verdicts": [v.to_dict() for v in verdicts],
        "note": (
            "Advisory ordering only. Every finding still counts against the "
            "caller's fail-closed gate; no band here means 'safe to ignore'."
        ),
    }


def audit_triage_tasks(
    path: str = ".",
    min_severity: str = "high",
    excludes: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Run a security audit and file one triage task per group of findings.

    Files NOTHING when the audit is clean — the previous
    audit-triage-file-tasks workflow had two unconditional steps, so a clean
    audit still filed a high-priority "Triage security audit findings" task
    carrying no rule, file, line or severity, and every run filed another copy.

    Findings at or above `min_severity` (default high) are grouped by rule
    (falling back to file), and each group becomes one task whose notes list
    every finding as `[severity] path:line: message` plus its suggested fix, so
    triage starts from the evidence rather than from scratch.

    Dedup is by a stable `[audit-triage: <root>:<group>:<class>]` marker in the
    notes, where root is `<basename>@<first 8 hex of sha256(resolved path)>` so
    two audited directories with the same basename never share a task, and
    class is the first segment of the finding's path under the root (task
    #2157), so a task covers only the class it was filed for. It is matched
    against tasks that are still open or closed with `[accepted-risk]`. A group
    whose task was done, cancelled or archived and which then reappears is
    genuinely new work and files again (task #1630).

    Read-only with `dry_run=True`: reports what it would file without writing.
    Returns {status, files_scanned, groups, created, skipped, tasks}.
    """
    report = security_audit(path=path, min_severity=min_severity, excludes=excludes)
    result = file_findings_as_tasks(report, min_severity=min_severity, dry_run=dry_run)
    result["status"] = report.get("status")
    result["files_scanned"] = report.get("files_scanned")
    return result
