"""Proposed review metadata is structural; human Ledger resolution is authority."""

from copy import deepcopy

import pytest
from review_evidence_fixture import review_evidence

from unified_agent import independent_review


def validate(data):
    return independent_review.validate(data, repo="example/project", number=123, head_sha="a" * 40)


def test_distinct_models_in_same_family_and_shared_fleet_label_are_supported():
    data = review_evidence()
    assert validate(data) == data
    assert data["authors"][0]["label"] == data["reviewer"]["label"]


@pytest.mark.parametrize("field", ["subject", "model"])
def test_every_author_execution_and_model_is_compared(field):
    data = review_evidence()
    other = {**data["authors"][0], "subject": "t3:second-author", "model": "google/gemini-2.5-pro"}
    other[field] = data["reviewer"][field]
    data["authors"].append(other)
    with pytest.raises(independent_review.ReviewEvidenceError):
        validate(data)


def test_different_runtime_provider_does_not_make_same_model_independent():
    data = review_evidence()
    data["reviewer"].update(provider="openrouter", model=data["authors"][0]["model"])
    with pytest.raises(independent_review.ReviewEvidenceError):
        validate(data)


def test_multiple_models_for_an_author_execution_are_preserved():
    data = review_evidence()
    data["authors"].append({**data["authors"][0], "model": "openai/gpt-6-sol"})
    assert len(validate(data)["authors"]) == 2


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", True),
        ("version", 2),
        ("repo", "other/project"),
        ("number", True),
        ("head_sha", "b" * 40),
        ("statement", "looks independent"),
        ("authors", []),
        ("authors", None),
        ("authors", [{}] * 33),
        ("reviewer", {"label": "Operator"}),
        ("verdict", "block"),
        ("review_ref", ""),
        ("review_ref", "x" * 1025),
    ],
)
def test_incomplete_unsupported_or_unbound_proposals_hold(key, value):
    data = review_evidence()
    data[key] = value
    with pytest.raises(independent_review.ReviewEvidenceError):
        validate(data)


@pytest.mark.parametrize("field", ["label", "subject", "provider", "model", "execution_ref"])
@pytest.mark.parametrize("value", [None, "", " leading", "line\nbreak", 123])
def test_actor_fields_must_be_complete_bounded_and_unambiguous(field, value):
    data = review_evidence()
    data["reviewer"][field] = value
    with pytest.raises(independent_review.ReviewEvidenceError):
        validate(data)


@pytest.mark.parametrize(
    "model",
    [
        "default",
        "openai/default",
        "openai/auto",
        "openai/unknown",
        "OpenAI/GPT-6",
        "openai/gpt-latest",
    ],
)
def test_unresolved_or_noncanonical_model_names_hold(model):
    data = review_evidence()
    data["reviewer"]["model"] = model
    with pytest.raises(independent_review.ReviewEvidenceError):
        validate(data)


def test_unknown_fields_and_duplicate_author_entries_hold():
    for mutate in (
        lambda d: d.update(native_verified=True),
        lambda d: d["reviewer"].update(native_verified=True),
        lambda d: d["authors"].append(deepcopy(d["authors"][0])),
    ):
        data = review_evidence()
        mutate(data)
        with pytest.raises(independent_review.ReviewEvidenceError):
            validate(data)


@pytest.mark.parametrize(
    "binding",
    [
        {"repo": None},
        {"repo": "bad"},
        {"number": True},
        {"number": 0},
        {"head_sha": "short"},
        {"head_sha": None},
    ],
)
def test_malformed_context_cannot_validate_matching_malformed_stored_binding(binding):
    data = review_evidence()
    context = {
        "repo": data["repo"],
        "number": data["number"],
        "head_sha": data["head_sha"],
        **binding,
    }
    data.update(binding)
    with pytest.raises(independent_review.ReviewEvidenceError):
        independent_review.validate(data, **context)


def test_total_escaped_evidence_size_is_bounded_for_native_comment_publication():
    data = review_evidence()
    data["authors"] = [
        {**data["authors"][0], "subject": f"t3:author-{i}", "execution_ref": "é" * 1024}
        for i in range(32)
    ]
    with pytest.raises(independent_review.ReviewEvidenceError, match="bounded evidence size"):
        validate(data)


def test_public_evidence_hashes_raw_references_and_redacts_secret_shaped_subjects():
    data = review_evidence()
    data["review_ref"] = "Authorization: Bearer private-review-reference"
    data["reviewer"]["execution_ref"] = "Authorization: Bearer private-execution-reference"
    data["reviewer"]["subject"] = "Authorization: Bearer private-subject-reference"
    public = independent_review.public_evidence(data)
    assert "private-review-reference" not in str(public)
    assert "private-execution-reference" not in str(public)
    assert "private-subject-reference" not in str(public)
    assert public["reviewer"]["model"] == data["reviewer"]["model"]
