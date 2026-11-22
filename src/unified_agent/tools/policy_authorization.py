"""MCP surface for policy authorizations (task #1798).

The library half landed with the ledger work (task #1407):
``github_app.record_policy_authorization`` appends a signed row bound to one
exact pull-request head, ``verified_policy_authorization`` is the merge-time
read, and ``list_policy_authorizations`` is the audit read this module
exposes. This module only wraps them — authority stays with
``UA_POLICY_AUTHORIZERS`` and ``UA_POLICY_AUTHORIZATION_ALLOW_SELF``
(deployment environment the operator sets) and with the caller's verified
identity, which no argument can name or widen.
"""

from mcp.server.mcpserver.exceptions import ToolError

from .. import github_app


def policy_authorization_record(
    repo: str, pr: int, head_sha: str, policy_ref: str, task_id: int
) -> dict:
    """Record that a standing policy covers merging one exact PR head (task #1798).

    The caller's verified identity is the authorizer — nothing in the
    arguments can name someone else. Only identities listed in
    ``UA_POLICY_AUTHORIZERS`` may record (default: nobody), and the task's
    claimant must differ from the authorizer unless
    ``UA_POLICY_AUTHORIZATION_ALLOW_SELF=1``. This is a pre-merge step:
    ``task_id`` must still be open and still carry its worktree branch.

    `repo` is `owner/name`, `pr` the pull request number, `head_sha` the
    full 40-character head commit the policy covers (exact-head binding: any
    other head of the same PR is not covered), `policy_ref` a short
    reference such as `deploy-steward-2026-10-01` or `operator-chat:<ref>`
    (never conversation text), and `task_id` the hub task the merge belongs
    to.

    Returns the public record: `{status: "recorded", authorization_id, kind,
    evidence_class, human_approval, repo, number, head_sha, policy_ref,
    task_id, task_claimant, task_branch, authorized_by, red_tier}` — never
    the signature. Raises ToolError when the caller may not authorize this
    merge or the binding is malformed.
    """
    try:
        return github_app.record_policy_authorization(
            repo=repo,
            number=pr,
            head_sha=head_sha,
            policy_ref=policy_ref,
            task_id=task_id,
        )
    except (ValueError, PermissionError) as exc:
        raise ToolError(str(exc)) from exc


def policy_authorization_list(repo: str, pr: int, head_sha: str | None = None) -> dict:
    """List recorded policy authorizations for one pull request, newest first.

    `repo` is `owner/name`, `pr` the pull request number, and `head_sha`
    (optional) narrows the list to one exact 40-character head. Every row
    carries `valid`: the same checks the ledger applies at merge time — a
    live hash chain, a signature that still verifies against the
    authorizer's current bearer, the authorizer's current membership in
    `UA_POLICY_AUTHORIZERS`, and the self-authorization rule — minus the
    red-tier check, which reads the pull request's changed files and stays
    with the merge-time caller.

    Returns `{repo, number, head_sha, authorizations, count,
    valid_for_head}`. `valid_for_head` is the newest valid row when
    `head_sha` was given (the merge-time answer for that exact head), else
    None. Signatures are never returned.
    """
    try:
        rows = github_app.list_policy_authorizations(repo=repo, number=pr, head_sha=head_sha)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    valid_for_head = next((row for row in rows if row["valid"]), None) if head_sha else None
    return {
        "repo": repo,
        "number": pr,
        "head_sha": head_sha,
        "authorizations": rows,
        "count": len(rows),
        "valid_for_head": valid_for_head,
    }
