"""Schema for proposed execution review claims, attested by a human release decision.

Validation alone grants no authority. Only an existing authenticated human
Approval Ledger resolution over the complete release payload attests these
claims. Roster preferences, task prose and caller-reported usage are not proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

ATTESTATION_STATEMENT = (
    "By approving this release I attest that these authors cover every execution and model "
    "contributing to this exact head; I inspected the execution and review references, "
    "resolved model aliases to canonical underlying identities, and verified that the "
    "reviewer is an independent actor distinct from every author execution and model. "
    "The reviewer entry covers the complete review execution using that single actual model; "
    "mixed-model review is unsupported. The reviewer reviewed the full exact-head diff "
    "with no unresolved blocking findings."
)
_FIELDS = {
    "version",
    "repo",
    "number",
    "head_sha",
    "statement",
    "authors",
    "reviewer",
    "verdict",
    "review_ref",
}
_ACTOR_FIELDS = {"label", "subject", "provider", "model", "execution_ref"}
_MODEL = re.compile(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*")
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9._-]*")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_HEAD = re.compile(r"[0-9a-f]{40}")
_SUBJECT = re.compile(r"[a-z][a-z0-9._-]*:[^\s]+")
_UNRESOLVED = re.compile(
    r"(?:^|[/._-])(?:auto|default|unknown|unresolved|latest|inherit)(?:$|[._-])"
)
_MAX_ENCODED_BYTES = 24_000


class ReviewEvidenceError(ValueError):
    """Static reason a proposed review cannot satisfy the shipping prerequisite."""


def _text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value == value.strip()
        and all(not unicodedata.category(c).startswith("C") for c in value)
    )


def _actor(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _ACTOR_FIELDS:
        raise ReviewEvidenceError("Independent review lacks complete execution evidence.")
    if any(not _text(value[key], 1024 if key == "execution_ref" else 200) for key in value):
        raise ReviewEvidenceError("Independent review has invalid execution evidence fields.")
    if (
        _MODEL.fullmatch(value["model"]) is None
        or _UNRESOLVED.search(value["model"])
        or _PROVIDER.fullmatch(value["provider"]) is None
        or _SUBJECT.fullmatch(value["subject"]) is None
    ):
        raise ReviewEvidenceError(
            "Independent review requires resolved canonical model identities."
        )
    return dict(value)


def validate(value: object, *, repo: str, number: int, head_sha: str) -> dict:
    """Check a bounded proposal; callers must separately verify human provenance.

    A subject is an execution identity, while label is shared attribution.
    Human attestation verifies the references and distinct actors, including
    canonical model identity across runtime providers. Syntax is not telemetry.
    """
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ReviewEvidenceError("Selected approval lacks supported independent-review evidence.")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["repo"] != repo
        or not _text(repo, 200)
        or _REPO.fullmatch(repo) is None
        or type(number) is not int
        or number < 1
        or type(value["number"]) is not int
        or value["number"] < 1
        or value["number"] != number
        or value["head_sha"] != head_sha
        or not isinstance(head_sha, str)
        or _HEAD.fullmatch(head_sha) is None
        or value["statement"] != ATTESTATION_STATEMENT
        or value["verdict"] != "approve"
        or not _text(value["review_ref"], 1024)
    ):
        raise ReviewEvidenceError(
            "Independent review is not an explicit approval of this exact head."
        )
    if not isinstance(value["authors"], list) or not 1 <= len(value["authors"]) <= 32:
        raise ReviewEvidenceError(
            "Independent review must identify every author execution and model."
        )
    authors = [_actor(author) for author in value["authors"]]
    reviewer = _actor(value["reviewer"])
    if len({(a["subject"], a["model"]) for a in authors}) != len(authors):
        raise ReviewEvidenceError("Independent review contains duplicate author execution models.")
    if any(reviewer["subject"] == a["subject"] or reviewer["model"] == a["model"] for a in authors):
        raise ReviewEvidenceError(
            "Independent review must use a distinct actor and model from every author."
        )
    result = {**value, "authors": authors, "reviewer": reviewer}
    # The approved evidence is rendered twice in the existing signed GitHub
    # comment envelope. Bound escaped JSON, including Unicode expansion, so
    # a valid proposal remains publishable within the comment size limit.
    if len(json.dumps(result, sort_keys=True).encode("utf-8")) > _MAX_ENCODED_BYTES:
        raise ReviewEvidenceError("Independent review exceeds the bounded evidence size.")
    return result


def public_evidence(review: dict) -> dict:
    """Hash evidence pointers and redact attribution for the hub-local CLI.

    The complete immutable attestation stays in the native Ledger. A human
    decision does not authorize exporting arbitrary proposed reference text.
    Public GitHub comments use only generic counts and the native row pointer.
    """
    from .telemetry.redaction import redact_sensitive_content

    def actor(entry: dict) -> dict:
        return {
            **{
                field: redact_sensitive_content(entry[field], max_len=200)
                for field in ("label", "subject", "provider", "model")
            },
            "execution_ref_sha256": hashlib.sha256(entry["execution_ref"].encode()).hexdigest(),
        }

    return {
        "version": review["version"],
        "authors": [actor(author) for author in review["authors"]],
        "reviewer": actor(review["reviewer"]),
        "verdict": review["verdict"],
        "review_ref_sha256": hashlib.sha256(review["review_ref"].encode()).hexdigest(),
    }
