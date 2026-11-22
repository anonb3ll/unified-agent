"""An explicitly proposed human-attested exact-head review, never live authority."""

from unified_agent.independent_review import ATTESTATION_STATEMENT


def review_evidence(repo="example/project", number=123, head_sha="a" * 40):
    return {
        "version": 1,
        "repo": repo,
        "number": number,
        "head_sha": head_sha,
        "statement": ATTESTATION_STATEMENT,
        "authors": [
            {
                "label": "Desktop_Agent2",
                "subject": "t3:author-task-1",
                "provider": "codex",
                "model": "openai/gpt-6.1-sol",
                "execution_ref": "t3://tasks/author-task-1",
            }
        ],
        "reviewer": {
            "label": "Desktop_Agent2",
            "subject": "t3:review-task-1",
            "provider": "codex",
            "model": "openai/gpt-6-astra",
            "execution_ref": "t3://tasks/review-task-1",
        },
        "verdict": "approve",
        "review_ref": "https://github.com/example/project/pull/123#issuecomment-1",
    }
