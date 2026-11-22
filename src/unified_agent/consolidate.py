"""Assisted memory consolidation — proposal-based, reversible, non-destructive.

The "safe forgetting / merge" engine. It NEVER destroys anything:

  * `generate_proposal()` clusters near-duplicate notes via embedding cosine
    similarity and writes a human-readable PROPOSAL artifact (to the vault inbox,
    or a fallback report file). It is strictly read-only — it MUST NOT mutate the
    database. An LLM may add prose to the artifact after the fact, but the
    deterministic clustering path here only reads.

  * `apply_supersede(survivor_id, superseded_ids)` is the explicit, human-invoked
    apply step. It demotes the losing notes by pointing their `superseded_by`
    column at the survivor. Demoted notes vanish from memory_search / memory_list
    but stay fully retrievable by id and preserve all provenance (author,
    created_at, updated_at are left untouched — only `superseded_by` is set).

  * `restore_note(id)` re-promotes a superseded note or undoes a soft delete
    through the same guarded restore path as the memory tool.

Consolidation only ever proposes; the apply and restore steps are explicit and
reversible. This honours the standing rule "never delete work not meant to be
deleted": memory_delete stays a separate owner-or-human soft-delete and consolidation is
never routed through it.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, llm_client, memory_graph, post_init, storage

DEFAULT_THRESHOLD = 0.90

# Timestamps this close together are one authoring burst, not a considered
# rewrite, so recency stops being meaningful between them — see _pick_survivor.
_SURVIVOR_BURST_WINDOW_S = 300

# Matches an unindented `unified-agent-consolidate apply <survivor> <ids...>`
# line inside a (possibly human-edited) proposal markdown file — the exact
# shape `render_proposal_markdown()` and `sleep_pass.render_sleep_pass_markdown()`
# both emit inside their ```bash fences. apply_batch() below is the reader.
_APPLY_CMD_RX = re.compile(
    r"^unified-agent-consolidate apply (\S+)((?:[ \t]+\S+)+)[ \t]*$", re.MULTILINE
)
# The block apply_batch() honours (task #2012): a ```bash fence holding only
# apply lines, one or more (a reviewer may group commands). A matching line
# anywhere else (prose, a model's reason, a journal digest) is reported as
# ignored, never run.
_APPLY_BLOCK_RX = re.compile(
    r"^```bash[ \t]*\n"
    r"((?:unified-agent-consolidate apply \S+(?:[ \t]+\S+)+[ \t]*\n)+)"
    r"```[ \t]*$",
    re.MULTILINE,
)


def one_line(text: object) -> str:
    """Render untrusted text (a model's reason, a note title) on one line.

    Proposal files are parsed back by apply_batch(), so a newline in rendered
    text could start a line that reads as an apply command (task #2012).
    Collapsing every whitespace run, newlines included, rules that out.
    """
    return " ".join(str(text or "").split())


def inline_code(text: object, fallback: str = "") -> str:
    """Render untrusted text as one literal CommonMark code span (#2024).

    A fence longer than every input backtick run cannot be closed by the text.
    Space padding prevents edge backticks from joining the fence; CommonMark
    removes the padding when displaying non-blank content.
    """
    text = one_line(text) or one_line(fallback)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}"


def neutralize_apply_lines(text: str) -> str:
    """Quote any line of multi-line text that reads as an apply command.

    For blocks that must keep their line structure, such as a journal digest:
    a ``> `` prefix leaves the line readable but no longer matchable.
    """
    # Normalise CR first: apply_batch() reads the file with universal newlines,
    # so a lone CR here becomes a line break there.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"(?m)^([ \t]*)(unified-agent-consolidate apply\b)", r"\1> \2", text)


def fenced_apply_commands(text: str) -> list[tuple[str, str]]:
    """The (survivor, rest) apply commands apply_batch() would run from `text`."""
    return [m for body in _APPLY_BLOCK_RX.findall(text) for m in _APPLY_CMD_RX.findall(body)]


# A response we cannot parse is not the same fact as a model that considered the
# cluster and said UNRELATED, even though both must block the merge. Recording
# them as one verdict makes a broken or overflowing model look like a working
# one that simply found nothing — so unparseable output gets its own label and
# is rendered as such.
VERDICT_UNPARSED = "UNPARSED"
_VERDICT_UNPARSED = VERDICT_UNPARSED  # private name kept for existing callers

# A CONTRADICTION verdict whose own reason argues against it (task #1778). The
# 2026-10-01 sleep pass filed 84 of 132 clusters as contradictions, with
# rationales such as "suggesting a revision and not a contradiction" and
# "contain the same information but differ in format". The verdict field is
# already schema-constrained; this catches a judge whose label and rationale
# disagree. Such a cluster is neither a contradiction nor a merge candidate: it
# goes to a human as NEEDS_REVIEW, never to DUPLICATE, which could authorize a
# merge on the strength of a self-contradicting answer. The prose check below
# only annotates; the move to NEEDS_REVIEW is decided by the judge's structured
# self_consistent / conflicting_attributes fields (task #2022).
VERDICT_NEEDS_REVIEW = "NEEDS_REVIEW"
# The check is an allowlist and may only downgrade when it is confident.
# Keeping CONTRADICTION is the safe failure: a human still reviews it, whereas
# a wrong NEEDS_REVIEW hides a real conflict. Precision is preferred to recall,
# so the earlier cue-and-exception heuristic (which kept missing scoped and
# contrasted conflicts) is replaced by: a short, one-line reason that carries
# one of a few exact denial or sameness phrases and nothing else but filler.
_DOWNGRADE_MAX_CHARS = 300
# Annotate-only (task #1778): the verdict stays CONTRADICTION and the review
# carries the matched `consistency_flag`, which the reports render as a
# "disputes this label" marker. Successive reviews kept finding reasons the
# allowlist misread as denials, so prose never moves a cluster to
# NEEDS_REVIEW. Keep this False: the downgrade is now decided by the judge's
# structured fields (task #2022, `structured_self_consistency` below).
_CONSISTENCY_FLAG_DOWNGRADES = False
# Task #2022: the judge contract carries `self_consistent` (JSON boolean) and
# `conflicting_attributes` (JSON array of strings). A pair whose reply is
# exactly those plus verdict and reason, with verdict CONTRADICTION,
# self_consistent true and no conflicting attributes, goes to NEEDS_REVIEW.
# False turns the downgrade off without touching the contract.
_STRUCTURED_CONSISTENCY_DOWNGRADES = True
STRUCTURED_DOWNGRADE_REASON = (
    "downgraded on structured self_consistent=true, conflicting_attributes=[]"
)
DISPUTED_LABEL_MARKER = "judge's reason disputes this label"
_DOWNGRADE_PHRASE_RX = re.compile(
    r"\b(?:"
    r"not\s+(?:a\s+)?contradict(?:ion|ory)"
    r"|no\s+(?:(?:real|genuine|actual|substantive)\s+)?(?:contradiction|conflict)"
    r"|not\s+in\s+conflict"
    r"|(?:do|does)\s+not\s+conflict"
    r"|(?:the\s+)?same\s+(?:information|content|facts)"
    r"|identical\s+(?:content|information)"
    r"|duplicates?"
    r"|restate[sd]?"
    r"|(?:but\s+)?differs?\s+(?:only\s+)?in\s+(?:format|formatting|wording|phrasing)"
    r")\b",
    re.IGNORECASE,
)
# Every word left once the matched phrases are removed must come from this
# closed filler vocabulary; any other word keeps the verdict, because unknown
# residue can carry a conflict ("differ in wording from mandatory approval to
# optional approval"). No restrictive adverbs (only, just, merely, simply):
# "Only the same facts are not in conflict" is not a denial for the cluster.
# Only one trailing period may remain, plus at most one
# comma, and only as ", suggesting" (the task fixture's form). No
# count words ("two", "three"), which allow subset claims; "first" and
# "second" stay because only a pair is ever downgraded, and the task fixtures
# need them, as they need the rest of the last line.
_DOWNGRADE_FILLER = frozenset(
    "the these those both notes note entries entry memories memory they it this"
    " that is are was were a an and which suggesting suggests"
    " revision rephrasing reformatting update of each"
    " there between contain contains second first updates figure one's".split()
)
# The matched phrases are replaced by this marker so the residue checks can
# see where they were.
_PHRASE_MARK = "\x00"
_DOWNGRADE_RESIDUE_RX = re.compile(r"[A-Za-z',\s\x00]*")
_RESIDUE_TOKEN_RX = re.compile(r"[A-Za-z']+|\x00")
_COMMA_RX = re.compile(r",\s*suggesting\b", re.IGNORECASE)
# An auxiliary or copula in the residue can carry a second, elliptical claim
# ("..., the second is"); the only one allowed is a single copula right before
# a matched phrase ("The notes are not in conflict").
_AUXILIARIES = frozenset("is are was were do does did has have had be".split())
# An ordinal after the first phrase can open a claim about one note only
# ("..., and only the second").
_ORDINALS = frozenset({"first", "second"})
# A reason ending on a bare auxiliary is elliptical ("..., the others are").
_TRAILING_AUXILIARY_RX = re.compile(
    r"\b(?:is|are|was|were|do|does|did|has|have|had)\s*\.?\s*$", re.IGNORECASE
)
# A reply longer than this is never treated as fully read; the cap is applied
# before any scan. A downgradable reason is at most _DOWNGRADE_MAX_CHARS.
_REPLY_MAX_CHARS = 1000
_LEGACY_VERDICT_LINE_RX = re.compile(r"VERDICT:\s*(?:DUPLICATE|CONTRADICTION|UNRELATED)", re.I)
_ADJUDICATION_KEYS = frozenset({"verdict", "reason"})
_STRUCTURED_ADJUDICATION_KEYS = _ADJUDICATION_KEYS | {"self_consistent", "conflicting_attributes"}


# One duplicate-key rule for every parse of a judge reply (raises a ValueError).
_reject_duplicate_keys = llm_client.reject_duplicate_keys


def structured_self_consistency(text: object, *, note_count: int) -> bool:
    """Whether the judge's structured fields authorize CONTRADICTION -> NEEDS_REVIEW.

    Task #2022. Reads the contract, never the prose: True only when the
    cluster is a pair and the reply is one short JSON object with exactly the
    keys verdict, reason, self_consistent and conflicting_attributes, no
    duplicate key at any depth, verdict exactly "CONTRADICTION", a non-empty
    string reason, self_consistent the JSON boolean true (not "true", 1 or
    null) and conflicting_attributes an empty JSON array. Every other shape,
    including a reply that omits the new fields, is False, so a missing or
    malformed answer keeps CONTRADICTION.

    Boundary: ``text`` is the provider leg's normalized completion text
    (``response["text"]``), not the raw provider reply. With a response schema
    the claude_cli leg (``llm_client._strip_json_fence``) removes one ```json
    or bare ``` fence and the whitespace around it before returning, so the
    one-object and length rules apply after that step: a fenced raw reply, or
    one over the cap only because of padding, can still qualify. The step
    drops nothing but the fence lines and whitespace; any other text before,
    after or inside the fence fails the schema or this check.
    """
    if note_count != 2 or not isinstance(text, str) or len(text) > _REPLY_MAX_CHARS:
        return False
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        data = json.loads(stripped, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError):  # ValueError includes JSONDecodeError
        return False
    if not isinstance(data, dict) or set(data) != _STRUCTURED_ADJUDICATION_KEYS:
        return False
    reason = data["reason"]
    attributes = data["conflicting_attributes"]
    return (
        data["verdict"] == "CONTRADICTION"
        and isinstance(reason, str)
        and bool(reason.strip())
        and data["self_consistent"] is True
        and type(attributes) is list
        and not attributes
    )


def reply_fully_read(text: str) -> bool:
    """Whether the parsed (verdict, reason) covers all of the judge's reply.

    The parser reads only ``verdict`` and ``reason`` from a JSON object (and
    json.loads keeps the last of duplicate keys), and only the first REASON
    line of a legacy reply. Anything else may hold a conflict the consistency
    check never saw, so it counts as fully read only when it is short and is
    either a JSON object with exactly those two string keys, no duplicates, or
    exactly one bare VERDICT line and one REASON line with nothing else.
    Linear: one bounded split, no backtracking pattern over the whole reply.
    """
    if len(text) > _REPLY_MAX_CHARS:
        return False
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped, object_pairs_hook=_reject_duplicate_keys)
        except (ValueError, RecursionError):  # ValueError includes JSONDecodeError
            return False
        return (
            isinstance(data, dict)
            and set(data) == _ADJUDICATION_KEYS
            and all(isinstance(data[key], str) for key in _ADJUDICATION_KEYS)
        )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (
        len(lines) == 2
        and _LEGACY_VERDICT_LINE_RX.fullmatch(lines[0]) is not None
        and lines[1].startswith("REASON:")
    )


def verdict_consistency_flag(verdict: str, reason: str, *, note_count: int) -> str | None:
    """The phrase in ``reason`` that denies a CONTRADICTION verdict, or None.

    Fail-safe by construction: None (keep CONTRADICTION) unless the cluster is
    a pair (a reason about a larger cluster can describe agreement among some
    of its notes only), the reason is at most ``_DOWNGRADE_MAX_CHARS`` long,
    on one line, does not end on a bare auxiliary, contains an allowlisted
    denial or sameness phrase, and everything else in it is filler words,
    commas and one trailing period. Only CONTRADICTION is checked: it is the
    verdict a human is asked to act on, and the one the 2026-10-01 judge
    over-assigned.
    """
    if verdict != "CONTRADICTION" or note_count != 2:
        return None
    if not reason or len(reason) > _DOWNGRADE_MAX_CHARS:
        return None
    text = reason.strip()
    if "\n" in text or "\r" in text or _TRAILING_AUXILIARY_RX.search(text):
        return None
    phrase = _DOWNGRADE_PHRASE_RX.search(text)
    if not phrase:
        return None
    residue = _DOWNGRADE_PHRASE_RX.sub(f" {_PHRASE_MARK} ", text.removesuffix("."))
    if not _DOWNGRADE_RESIDUE_RX.fullmatch(residue) or not _residue_is_filler(residue):
        return None
    return one_line(phrase.group(0))


def disputed_label_marker(review: dict) -> str | None:
    """The one-line report marker for a contradiction its own reason disputes."""
    flag = review.get("consistency_flag")
    if review.get("verdict") != "CONTRADICTION" or not flag:
        return None
    return f'{DISPUTED_LABEL_MARKER}: "{one_line(flag)}"'


def _residue_is_filler(residue: str) -> bool:
    """Whether what is left around the matched phrases says nothing more."""
    commas = residue.count(",")
    if commas > 1 or (commas == 1 and not _COMMA_RX.search(residue)):
        return False
    tokens = [token.lower() for token in _RESIDUE_TOKEN_RX.findall(residue)]
    copulas = 0
    seen_phrase = False
    for i, token in enumerate(tokens):
        if token == _PHRASE_MARK:
            seen_phrase = True
            continue
        if token not in _DOWNGRADE_FILLER:
            return False
        if token in _AUXILIARIES:
            copulas += 1
            if copulas > 1 or i + 1 == len(tokens) or tokens[i + 1] != _PHRASE_MARK:
                return False
        if seen_phrase and token in _ORDINALS:
            return False
    return True


# Every note body in a cluster goes into one prompt. The default local leg is
# llama3:8b, so an unbounded concatenation of long notes silently overflows the
# context and the verdict degrades to whatever survived truncation — invisibly.
# Cap each note instead, and say so in the prompt so the model knows the body
# it is judging is partial.
_ADJUDICATION_NOTE_CHAR_CAP = 2000

# Total content budget for one adjudication prompt, independent of cluster size.
# The per-note cap alone still lets a large cluster produce a ~2000*len(cluster)
# character prompt, which overflows the default llama3:8b context on anything
# past a handful of notes and returns an arbitrary or UNPARSED verdict —
# defeating the point of adjudicating it at all (task #445 / PR #196 review).
# ~6000 chars (~1500 tokens) leaves headroom under the ~8192-token default
# context for the system prompt, note-id headers, and the model's reply.
_ADJUDICATION_PROMPT_CHAR_BUDGET = 6000

_ADJUDICATION_SYSTEM_PROMPT = (
    "You are reviewing a cluster of notes from a shared team memory database that "
    "grouped together by semantic similarity. Classify the cluster and reply in "
    "EXACTLY this two-line format, nothing else:\n"
    "VERDICT: DUPLICATE|CONTRADICTION|UNRELATED\n"
    "REASON: <one sentence>\n\n"
    "DUPLICATE = the notes say essentially the same thing in different words. "
    "CONTRADICTION = the notes make factual claims that conflict and cannot both be "
    "true. UNRELATED = topically similar but neither duplicate nor conflicting.\n\n"
    # Task #2022: the structured self-consistency fields. Only these, never the
    # reason text, can move a CONTRADICTION to human review.
    "When asked for a JSON object, also include: self_consistent = JSON boolean, "
    "true only if every claim in the notes can be true at the same time, otherwise "
    "false; conflicting_attributes = JSON array of strings naming each attribute the "
    'notes disagree on (for example "deploy host" or "rate limit"), or [] when they '
    "disagree on nothing."
)


def _threshold(override: float | None = None) -> float:
    """Cosine-similarity bar for calling two notes near-duplicates.

    An explicit override wins; otherwise `UA_CONSOLIDATE_THRESHOLD`, otherwise a
    conservative default. A malformed env value falls back to the default rather
    than making the pass over- or under-eager.
    """
    if override is not None:
        return override
    raw = os.environ.get("UA_CONSOLIDATE_THRESHOLD")
    if raw is None:
        return DEFAULT_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_THRESHOLD


def _load_candidates(conn) -> list[dict]:
    """Read every non-index, not-already-superseded note plus its embedding blob.

    Read-only. Index notes and already-demoted notes are excluded: an already
    superseded note is not a merge candidate.
    """
    rows = conn.execute(
        "SELECT m.id, m.title, m.content, m.updated_at, m.access_count, m.salience, "
        "m.superseded_by, m.tags, LENGTH(m.content) AS content_len, me.embedding, "
        "me.model, m.author, m.feedback_helpful, m.feedback_not_helpful, "
        "m.feedback_stale, m.feedback_wrong "
        "FROM memory m LEFT JOIN memory_embeddings me ON me.note_id = m.id "
        "WHERE m.id NOT LIKE 'masterindex%' AND m.id NOT LIKE 'tagindex_%' "
        "AND m.superseded_by IS NULL "
        "AND instr(m.tags, '\"meta:deleted\"') = 0 "
        # Tenant fence (task #1762): a merge never pairs notes across tenants.
        "AND m.org_id IS ?",
        (edition.tenant_org_id(),),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "content": r["content"],
            "updated_at": r["updated_at"],
            "access_count": r["access_count"] if "access_count" in r.keys() else 0,
            "salience": r["salience"] if "salience" in r.keys() else 0.5,
            # Length stands in for information content when picking a survivor;
            # the body is also sent to the LLM adjudication seam below.
            "content_len": r["content_len"] or 0,
            "embedding": r["embedding"],
            "model": r["model"] if "model" in r.keys() else None,
            "tags": json.loads(r["tags"]) if r["tags"] else [],
            "author": r["author"] if "author" in r.keys() else None,
            # Protective signals for the auto-apply gate below. A note a
            # human has voted on is never a silent merge candidate.
            "feedback_helpful": r["feedback_helpful"] or 0,
            "feedback_not_helpful": r["feedback_not_helpful"] or 0,
            "feedback_stale": r["feedback_stale"] or 0,
            "feedback_wrong": r["feedback_wrong"] or 0,
        }
        for r in rows
    ]


# Metadata templates to ignore when calculating semantic content overlap
_BOILERPLATE = re.compile(
    r"(filed-by:|claimed-by:|status:|priority:|parent_id:|created_at:|updated_at:|completed_at:|source_host:|branch:|worktree_path:)",
    re.IGNORECASE,
)


def _get_keywords(title: str, content: str | None) -> set[str]:
    """Extract significant keywords from note title and content."""
    text = f"{title} {content or ''}".lower()
    text = _BOILERPLATE.sub("", text)
    # Simple whitespace split, filtering out short/stop words could be added later
    return {word for word in text.split() if len(word) > 3}


def _jaccard_similarity(set1: set[str], set2: set[str]) -> float:
    """Calculate Jaccard similarity between two sets."""
    if not set1 or not set2:
        return 0.0
    intersection = len(set1.intersection(set2))
    union = len(set1.union(set2))
    return intersection / union


def _pairwise_cosine_matrix(embeddings: list[bytes]):
    """Cosine similarity for every embedding pair, one vectorized matmul.

    Numerically equivalent to calling `embeddings.cosine_similarity_bytes` for
    every (i, j) pair, but O(n^2) only in the BLAS matrix multiply rather than
    in Python-level iteration: decode once, normalize once, then a single
    `normalized @ normalized.T`. Added for task #997 — the nightly sleep
    pass's active candidate pool (~2,750 notes as of 2026-09-14) made the
    per-pair decode-and-compare loop the dominant cost of a run.

    A zero vector (or an empty/malformed blob) gets similarity 0.0 to
    everything, including itself — matching `cosine_similarity_bytes`'s
    zero-norm guard.
    """
    import numpy as np

    vectors = [np.frombuffer(b, dtype=np.float32) for b in embeddings]
    n = len(vectors)
    dim = max((v.shape[0] for v in vectors), default=0)
    if n == 0 or dim == 0:
        return np.zeros((n, n), dtype=np.float32)

    mat = np.zeros((n, dim), dtype=np.float32)
    for i, v in enumerate(vectors):
        if v.shape[0] == dim:
            mat[i] = v
        # A vector whose length doesn't match the common dim is left as all
        # zeros — a malformed/mismatched embedding compares as dissimilar to
        # everything rather than raising, matching cosine_similarity_bytes's
        # broad except-Exception-returns-0.0 contract.

    norms = np.linalg.norm(mat, axis=1)
    zero = norms == 0
    safe_norms = np.where(zero, 1.0, norms)
    normalized = mat / safe_norms[:, None]
    sim = normalized @ normalized.T
    sim[zero, :] = 0.0
    sim[:, zero] = 0.0
    np.clip(sim, -1.0, 1.0, out=sim)
    return sim


def cluster_duplicates(candidates: list[dict], threshold: float) -> list[list[dict]]:
    """Greedy connected-components clustering over pairwise cosine similarity
    with a Jaccard semantic filter to reject template-only matches.

    The cosine-similarity pass is vectorized via `_pairwise_cosine_matrix`
    (task #997): one matrix multiply over every candidate up front, then the
    Python loop below only runs the (comparatively expensive) agent-identity
    and Jaccard checks for pairs the matrix already says clear the threshold —
    typically a small fraction of all pairs. Semantics are unchanged from the
    original per-pair loop: same threshold, same agent-identity exclusion,
    same Jaccard secondary filter.

    Vectors from different models are not compared. NULL is the default model
    under the legacy rule (`storage.canonical_embedding_model`), so unlabeled
    rows still cluster with default-model rows and with nothing else. Each
    model is its own cohort; lexical checks stay inside that cosine pass.
    """
    embedded = [c for c in candidates if c["embedding"] is not None]
    cohorts: dict[str, list[dict]] = defaultdict(list)
    for note in embedded:
        cohorts[storage.canonical_embedding_model(note.get("model"))].append(note)
    found: list[list[dict]] = []
    for cohort in cohorts.values():
        found.extend(_cluster_same_model(cohort, threshold))
    return found


def _cluster_same_model(embedded: list[dict], threshold: float) -> list[list[dict]]:
    """Cluster one model cohort. Every embedding here shares a canonical model."""
    n = len(embedded)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    if n >= 2:
        import numpy as np

        sim_matrix = _pairwise_cosine_matrix([c["embedding"] for c in embedded])
        for i, j in np.argwhere(sim_matrix >= threshold):
            i, j = int(i), int(j)
            if i >= j:
                continue
            # Agent-identity notes (Agent label: X = tool on device Y) are
            # near-identical by design — they follow one naming convention on
            # purpose, so the parallel phrasing is the feature, not a
            # duplicate. Excluded from clustering entirely rather than raising
            # the threshold, which would just miss the next convention that
            # produces legitimately parallel notes. See memory 70aa52f9392f:
            # the weekly consolidation leg proposed merging four distinct
            # agents' identity notes while the nightly leg correctly called
            # them unrelated three nights running.
            if "agent-identity" in embedded[i]["tags"] or "agent-identity" in embedded[j]["tags"]:
                continue
            # Semantic filter: only cluster if content is also meaningfully similar
            kw1 = _get_keywords(embedded[i]["title"], None)
            kw2 = _get_keywords(embedded[j]["title"], None)
            if _jaccard_similarity(kw1, kw2) > 0.3:
                union(i, j)

    groups: dict[int, list[dict]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(embedded[i])
    return [g for g in groups.values() if len(g) >= 2]


def _burst_bucket(updated_at: str | None) -> int:
    """Which same-authoring-burst window `updated_at` falls in (0 if unparseable).

    Timestamps in the same bucket compare equal for survivor selection. Bucketing
    rather than pairwise-delta keeps `max()` a total order — a pairwise "within
    N seconds" test is not transitive, so a chain of near-neighbours could
    otherwise make the winner depend on cluster order.

    The cost of bucketing is a boundary: two notes seconds apart that straddle a
    window edge land in different buckets and recency decides after all. That is
    the acceptable direction to be wrong — it degrades to today's behaviour for a
    narrow slice of pairs, rather than making the winner order-dependent for all
    of them.
    """
    if not updated_at:
        return 0
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return 0
    return int(ts.timestamp()) // _SURVIVOR_BURST_WINDOW_S


def _pick_survivor(cluster: list[dict]) -> dict:
    """The note the others should defer to: newest *burst* wins, then most-accessed,
    then longest, then id.

    Recency is the right primary signal between authoring sessions — a considered
    rewrite supersedes what it rewrote — and the wrong one inside a single burst,
    where a few seconds carry no editorial meaning but the content differences do.
    So timestamps are compared at `_SURVIVOR_BURST_WINDOW_S` granularity, and
    within a burst the note that is actually used and says more wins.

    Access count leads length because a long note nobody reads is not evidence of
    value; length only separates notes with equal use. Ties break on id so a
    proposal stays reproducible.

    Written after the 2026-07-28 nightly pass (task #203) proposed demoting a
    note with 21 accesses under one with 1, on a 66-second recency edge. Demoting
    hides a note from `memory_search`, so picking the thinner survivor silently
    drops whatever only the demoted note said.
    """
    return max(
        cluster,
        key=lambda c: (
            _burst_bucket(c.get("updated_at")),
            c.get("access_count") or 0,
            c.get("content_len") or 0,
            c["id"],
        ),
    )


def _truncate_for_prompt(body: str, cap: int = _ADJUDICATION_NOTE_CHAR_CAP) -> str:
    """Cap one note body, marking the cut so the model knows the text is partial."""
    if len(body) <= cap:
        return body
    return body[:cap] + "\n… [truncated for adjudication]"


def _review_cluster(cluster: list[dict]) -> dict:
    """Ask the shared LLM client whether a semantic cluster is a true duplicate.

    Returns {verdict, reason, provider, note_ids}. `verdict` is one of DUPLICATE,
    CONTRADICTION, UNRELATED or UNPARSED. A CONTRADICTION whose own reason
    plainly denies it (see `verdict_consistency_flag`) also carries the matched
    `consistency_flag` (task #1778) and stays CONTRADICTION: prose never
    downgrades. A CONTRADICTION becomes NEEDS_REVIEW, with `judge_verdict` and
    `downgrade_reason`, only on the judge's structured fields (task #2022, see
    `structured_self_consistency`). Only DUPLICATE authorizes a merge
    proposal; the rest are carried through to the rendered artifact rather than
    dropped, because a CONTRADICTION verdict — two notes that cannot both be
    true — is the most valuable thing this pass can find, and an UNPARSED one is
    the signal that the model or its context budget is broken.
    """
    # Bound the *total* prompt, not just each note. Reserve space for each
    # note's identifier/title header before distributing the content budget.
    headers = [f"Note `{note['id']}` ({note['title'] or 'untitled'}):\n" for note in cluster]
    header_chars = sum(len(header) + 2 for header in headers)
    content_budget = max(1, _ADJUDICATION_PROMPT_CHAR_BUDGET - header_chars)
    per_note_cap = max(1, min(_ADJUDICATION_NOTE_CHAR_CAP, content_budget // max(1, len(cluster))))
    prompt = "\n\n".join(
        f"{header}{_truncate_for_prompt(note.get('content') or '', per_note_cap)}"
        for header, note in zip(headers, cluster, strict=True)
    )
    if len(prompt) > _ADJUDICATION_PROMPT_CHAR_BUDGET:
        prompt = prompt[:_ADJUDICATION_PROMPT_CHAR_BUDGET] + "\n… [prompt truncated]"
    # subscription_pool opt-in (task #1252): cluster adjudication is a named
    # zero-spend background call site — still gated on UA_SUBSCRIPTION_POOL_ENABLED.
    response = llm_client.complete(
        prompt,
        system=_ADJUDICATION_SYSTEM_PROMPT,
        response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA,
        subscription_pool=True,
        memory_agent=True,
    )
    parsed = llm_client.parse_adjudication_response(response["text"])
    if parsed is None:
        return {
            "verdict": _VERDICT_UNPARSED,
            "reason": response["text"].strip()[:200],
            "provider": response.get("provider", "unknown"),
            "note_ids": [note["id"] for note in cluster],
        }
    verdict, reason = parsed
    review = {
        "verdict": verdict,
        "reason": reason,
        "provider": response.get("provider", "unknown"),
        "note_ids": [note["id"] for note in cluster],
    }
    flag = (
        verdict_consistency_flag(verdict, reason, note_count=len(cluster))
        if reply_fully_read(response["text"])
        else None
    )
    if flag:
        review["consistency_flag"] = flag
        if _CONSISTENCY_FLAG_DOWNGRADES:
            review.update(verdict=VERDICT_NEEDS_REVIEW, judge_verdict=verdict)
    # Task #2022: the structured gate. It reads the reply itself, so it is
    # independent of the prose flag above (which never fires on a four-key
    # reply: reply_fully_read accepts only verdict and reason).
    if (
        _STRUCTURED_CONSISTENCY_DOWNGRADES
        and verdict == "CONTRADICTION"
        and structured_self_consistency(response["text"], note_count=len(cluster))
    ):
        review.update(
            verdict=VERDICT_NEEDS_REVIEW,
            judge_verdict=verdict,
            downgrade_reason=STRUCTURED_DOWNGRADE_REASON,
        )
    return review


# --- Auto-apply gate (task #965) -------------------------------------------
#
# Consolidation's contract is that applying a merge is explicit and reversible.
# Auto-apply does not change the second half of that — `apply_supersede` still
# only writes the `superseded_by` pointer, and `restore_note` still fully
# reverses it — but it does remove the human from the first half, so the bar
# here is deliberately much higher than the propose threshold.
#
# Default OFF. Turning it on is a policy decision about this fleet's memory,
# not a tuning knob, so there is no "mostly on" middle setting.
_AUTO_APPLY_ENV = "UA_CONSOLIDATE_AUTO_APPLY"
_AUTO_THRESHOLD_ENV = "UA_CONSOLIDATE_AUTO_THRESHOLD"
_AUTO_THRESHOLD_DEFAULT = 0.98
# A note the fleet keeps using is not a silent merge candidate, however
# similar its text looks.
_AUTO_MAX_SALIENCE = 0.7
_AUTO_MAX_ACCESS = 5


def auto_apply_enabled() -> bool:
    """Whether safe merges may be applied without human review (default False)."""
    return os.environ.get(_AUTO_APPLY_ENV, "0").strip().lower() in {"1", "true", "yes", "on"}


def auto_apply_threshold() -> float:
    """Similarity floor for an unreviewed merge. Never below the propose threshold."""
    raw = os.environ.get(_AUTO_THRESHOLD_ENV)
    if raw is None:
        return _AUTO_THRESHOLD_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return _AUTO_THRESHOLD_DEFAULT
    if not (0.0 < value <= 1.0):
        return _AUTO_THRESHOLD_DEFAULT
    # An auto-threshold at or under the propose threshold would auto-apply
    # everything that gets proposed, which is the opposite of a gate.
    return max(value, _threshold())


def _project_tags(tags: list[str]) -> set[str]:
    return {t for t in tags or [] if t.startswith("project:")}


def auto_apply_verdict(
    survivor: dict,
    loser: dict,
    similarity: float,
    cluster_size: int,
    votes: dict[str, dict[str, int]] | None = None,
) -> tuple[bool, str]:
    """Whether this one demotion is safe to apply unreviewed, and why not if it isn't.

    Every condition is a veto; the reason names the first that fails so the
    proposal artifact can explain itself to a reviewer. Pure — no I/O, no
    database, no clock — so the policy is testable on plain dicts.
    """
    votes = votes or {}
    if cluster_size != 2:
        return False, f"cluster of {cluster_size} notes (only a 1:1 merge can auto-apply)"
    floor = auto_apply_threshold()
    if similarity < floor:
        return False, f"similarity {similarity:.4f} < auto threshold {floor:.4f}"
    if _project_tags(survivor.get("tags", [])) != _project_tags(loser.get("tags", [])):
        return False, "survivor and demoted note carry different project: tags"
    if (survivor.get("author") or None) != (loser.get("author") or None):
        return False, "different authors"
    for note, which in ((survivor, "survivor"), (loser, "demoted note")):
        if (note.get("salience") or 0.0) > _AUTO_MAX_SALIENCE:
            return False, f"{which} salience {note.get('salience')} > {_AUTO_MAX_SALIENCE}"
        if (note.get("access_count") or 0) > _AUTO_MAX_ACCESS:
            return False, f"{which} accessed {note.get('access_count')}x (> {_AUTO_MAX_ACCESS})"
        for field in (
            "feedback_helpful",
            "feedback_not_helpful",
            "feedback_stale",
            "feedback_wrong",
        ):
            if note.get(field) or 0:
                return False, f"{which} carries human feedback ({field})"
        note_votes = votes.get(note.get("id"), {})
        if note_votes.get("upvoted") or note_votes.get("recalled"):
            return False, f"{which} has recall votes"
    return True, "eligible"


def build_proposals(conn, threshold: float) -> dict:
    """Cluster candidates, adjudicate each cluster, and shape the merges into proposals.

    Read-only. Returns {proposals, rejected}. Each proposal records the survivor
    and, for every note it would demote, the cosine similarity to the survivor
    (for the human reviewer).

    `rejected` carries every cluster the adjudicator declined to call a
    DUPLICATE, with its verdict, reason and provider. These used to be dropped
    silently, which made three different outcomes — no clusters detected, every
    cluster judged unrelated, and a model returning garbage — render as the same
    empty artifact. They are surfaced instead; a CONTRADICTION in particular is a
    finding in its own right, not a failed merge.
    """
    from .embeddings import cosine_similarity_bytes
    from .recall_curation import vote_rows

    candidates = _load_candidates(conn)
    clusters = cluster_duplicates(candidates, threshold)
    # Read-only: the auto-apply verdict is attached here so the artifact can
    # explain, per cluster, why a merge is or is not safe to apply unreviewed.
    # Applying is a separate, explicitly-invoked step (auto_apply_safe_merges).
    votes = vote_rows(conn)

    proposals = []
    rejected = []
    for cluster in clusters:
        review = _review_cluster(cluster)
        if review["verdict"] != "DUPLICATE":
            rejected.append(review)
            continue
        survivor = _pick_survivor(cluster)
        losers = []
        for note in cluster:
            if note["id"] == survivor["id"]:
                continue
            sim = cosine_similarity_bytes(survivor["embedding"], note["embedding"])
            losers.append(
                {"id": note["id"], "title": note["title"], "similarity": round(float(sim), 4)}
            )
        losers.sort(key=lambda x: x["similarity"], reverse=True)
        if losers:
            loser_note = next(n for n in cluster if n["id"] == losers[0]["id"])
            eligible, reason = auto_apply_verdict(
                survivor, loser_note, losers[0]["similarity"], len(cluster), votes
            )
        else:
            eligible, reason = False, "no demotable notes"
        proposals.append(
            {
                "survivor": {"id": survivor["id"], "title": survivor["title"]},
                "superseded": losers,
                "auto_apply": {"eligible": eligible, "reason": reason},
            }
        )
    proposals.sort(key=lambda p: p["survivor"]["id"])
    rejected.sort(key=lambda r: (r["verdict"], r["note_ids"]))
    return {"proposals": proposals, "rejected": rejected}


def render_proposal_markdown(
    proposals: list[dict],
    threshold: float,
    note_count: int,
    stale_notes: list[dict] | None = None,
    rejected: list[dict] | None = None,
) -> str:
    """A human-readable, human-approved-then-applied consolidation proposal.

    Each block hands the reviewer the exact `apply` command; nothing runs until a
    human invokes it. This artifact is advice, not an instruction to the DB.
    """
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    demote_count = sum(len(p["superseded"]) for p in proposals)
    lines = [
        "---",
        "title: Memory Consolidation Proposal",
        f"generated: {ts}",
        "tags: [memory, consolidation, proposal]",
        "---",
        "",
        "# Memory Consolidation Proposal",
        "",
        f"*Generated: `{ts}` · threshold: `{threshold}` · scanned `{note_count}` notes.*",
        "",
        "This is a **proposal only** — no memory has been changed. Consolidation "
        "never deletes: applying a block demotes the listed notes by pointing them "
        "at a survivor (`superseded_by`). Demoted notes leave search/list results "
        "but stay retrievable with `memory_get` and can be re-promoted at any time "
        "with `unified-agent-consolidate restore <id>`.",
        "",
    ]
    if proposals:
        lines += [
            f"Found **{len(proposals)}** cluster(s) covering **{demote_count}** demotable note(s).",
            "",
        ]
        for idx, p in enumerate(proposals, 1):
            surv = p["survivor"]
            lines += [
                f"## Cluster {idx}",
                "",
                f"**Survivor:** `{surv['id']}` — {inline_code(surv['title'], fallback='(untitled)')}",
                "",
                "**Would be superseded:**",
                "",
            ]
            for s in p["superseded"]:
                lines.append(
                    f"- `{s['id']}` — {inline_code(s['title'], fallback='(untitled)')} · similarity `{s['similarity']}`"
                )
            verdict = p.get("auto_apply")
            if verdict:
                lines += [
                    "",
                    (
                        "**Auto-apply:** eligible — this merge clears the unreviewed-merge "
                        f"gate (threshold `{auto_apply_threshold()}`)."
                        if verdict["eligible"]
                        else f"**Auto-apply:** held for review — {inline_code(verdict['reason'])}."
                    ),
                ]
            superseded_args = " ".join(s["id"] for s in p["superseded"])
            lines += [
                "",
                "Apply (explicit, reversible):",
                "",
                "```bash",
                f"unified-agent-consolidate apply {surv['id']} {superseded_args}",
                "```",
                "",
            ]
    elif rejected:
        lines += [
            "## No merges proposed",
            "",
            f"**{len(rejected)}** cluster(s) were detected at this threshold and every one was "
            "declined by adjudication. See *Reviewed but not proposed* below — this is not the "
            "same as finding nothing.",
            "",
        ]
    else:
        lines += [
            "## No clusters found",
            "",
            "No near-duplicate clusters were detected at this threshold. Nothing to propose.",
            "",
        ]

    contradictions = [r for r in (rejected or []) if r["verdict"] == "CONTRADICTION"]
    if contradictions:
        lines += [
            "## Contradictions — needs human review",
            "",
            f"Adjudication found **{len(contradictions)}** cluster(s) whose notes make claims "
            "that cannot all be true. These are **not** merge candidates: merging would pick a "
            "winner arbitrarily and hide the conflict. Resolve the facts first, by hand.",
            "",
        ]
        for r in contradictions:
            ids = ", ".join(f"`{n}`" for n in r["note_ids"])
            lines += [f"- {ids}", f"  - **Reason:** {inline_code(r['reason'])}"]
            marker = disputed_label_marker(r)
            if marker:
                lines.append(f"  - *{inline_code(marker)}*")
            lines.append(f"  - *adjudicated by {inline_code(r['provider'])}*")
        lines.append("")

    other_rejected = [r for r in (rejected or []) if r["verdict"] != "CONTRADICTION"]
    if other_rejected:
        lines += [
            "## Reviewed but not proposed",
            "",
            "Clusters that passed the similarity threshold but were declined by adjudication. "
            f"`{_VERDICT_UNPARSED}` means the model's reply could not be parsed — that blocks the "
            "merge conservatively, but it is a signal the adjudicator or its context budget is "
            "broken, not a finding about the notes."
            + (
                f" `{VERDICT_NEEDS_REVIEW}` means the judge said CONTRADICTION but its "
                "structured answer reported the notes self-consistent with no conflicting "
                "attributes; decide by hand."
                if any(r["verdict"] == VERDICT_NEEDS_REVIEW for r in other_rejected)
                else ""
            ),
            "",
        ]
        for r in other_rejected:
            ids = ", ".join(f"`{n}`" for n in r["note_ids"])
            lines += [
                f"- **{inline_code(r['verdict'])}** — {ids}",
                f"  - **Reason:** {inline_code(r['reason'])}",
            ]
            if r.get("downgrade_reason"):
                lines.append(
                    f"  - *judge said {inline_code(r.get('judge_verdict'))}; "
                    f"{inline_code(r['downgrade_reason'])}*"
                )
            if r.get("consistency_flag"):
                lines.append(
                    f"  - *judge said {inline_code(r.get('judge_verdict'))}; reason cue: "
                    f'"{inline_code(r["consistency_flag"])}"*'
                )
            lines.append(f"  - *adjudicated by {inline_code(r['provider'])}*")
        lines.append("")

    if stale_notes:
        stale_threshold_months = float(os.environ.get("UA_MEMORY_STALE_THRESHOLD_MONTHS", "6.0"))
        lines += [
            "## Stale High-Salience Notes for Re-confirmation",
            "",
            f"The following active high-salience notes (salience >= 0.7) have not been updated for "
            f"more than {stale_threshold_months} months. Please review them for correctness:",
            "",
        ]
        for sn in stale_notes:
            lines.append(
                f"- `{sn['id']}` — {inline_code(sn['title'], fallback='(untitled)')} · salience `{sn['salience']}` · "
                f"untouched for `{sn['age_months']}` months (last updated `{sn['updated_at'][:10]}`)"
            )
        lines.append("")

    return "\n".join(lines)


def _write_artifact(markdown: str) -> str | None:
    """Persist the proposal, preferring the vault inbox, falling back to a report file.

    Neither destination is the memory DB, so this stays within the read-only
    guarantee for the database. Returns the path written, or None if both fail.
    The vault write holds the per-note lock the hub's other note writers take
    (#2086); the report-file fallback is outside the vault and takes none.

    A note at the target name with frontmatter ``locked: true`` is never
    overwritten (#2091). Each run is its own timestamped report, so the run
    writes the next free sibling (``...-HHMMSS-2.md``) instead of refusing, and
    the returned path, which callers report as ``artifact_path``, names it.
    """
    name = f"Memory-Consolidation-Proposal-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.md"
    # Preferred: vault inbox, via the vault write chokepoint.
    try:
        from .note_lock import unlocked_note_slot
        from .safety import resolve_in_vault

        target = resolve_in_vault(f"00 - Inbox/{name}", for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with unlocked_note_slot(target) as slot:
            slot.write_text(markdown, encoding="utf-8")
        return str(slot)
    except (ToolError, OSError):
        pass
    # Fallback: a report file under the data home (never the DB).
    try:
        reports = config.data_home() / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        target = reports / name
        target.write_text(markdown, encoding="utf-8")
        return str(target)
    except OSError:
        return None


def generate_proposal(threshold: float | None = None, write: bool = True) -> dict:
    """Produce a consolidation proposal. STRICTLY READ-ONLY over the memory DB.

    Clusters near-duplicate notes by embedding cosine similarity and (when
    `write=True`) emits a markdown artifact. Does not, and must not, mutate the
    database — the caller applies changes explicitly via `apply_supersede`.

    Returns {proposals, threshold, note_count, cluster_count, demote_count,
    has_embeddings, artifact_path, stale_notes}.
    """
    thr = _threshold(threshold)
    conn = storage.connect()
    stale_notes = []
    try:
        candidates = _load_candidates(conn)
        has_embeddings = any(c["embedding"] is not None for c in candidates)
        built = build_proposals(conn, thr)
        proposals = built["proposals"]
        rejected = built["rejected"]

        stale_threshold_months = float(os.environ.get("UA_MEMORY_STALE_THRESHOLD_MONTHS", "6.0"))
        now_dt = datetime.now(UTC)
        for c in candidates:
            sal = c.get("salience", 0.5)
            if sal >= 0.7 and c.get("updated_at"):
                try:
                    updated_dt = datetime.fromisoformat(c["updated_at"].replace("Z", "+00:00"))
                    age_seconds = (now_dt - updated_dt).total_seconds()
                    age_days = max(0.0, age_seconds / 86400.0)
                    age_months = age_days / 30.44
                    if age_months >= stale_threshold_months:
                        stale_notes.append(
                            {
                                "id": c["id"],
                                "title": c["title"],
                                "salience": sal,
                                "age_months": round(age_months, 1),
                                "updated_at": c["updated_at"],
                            }
                        )
                except Exception:
                    pass
        stale_notes.sort(key=lambda x: x["age_months"], reverse=True)
    finally:
        conn.close()

    note_count = len(candidates)
    markdown = render_proposal_markdown(proposals, thr, note_count, stale_notes, rejected)
    artifact_path = _write_artifact(markdown) if write else None

    return {
        "proposals": proposals,
        "rejected": rejected,
        "threshold": thr,
        "note_count": note_count,
        # cluster_count is proposals only (merge candidates), kept for backward
        # compatibility with callers that treat it as "how many merges". A run
        # where every detected cluster was rejected (CONTRADICTION/UNRELATED/
        # UNPARSED) therefore also reports cluster_count=0, indistinguishable
        # from detecting nothing — detected_cluster_count is the total so a
        # caller can tell those two apart (task #445 / PR #196 review).
        "cluster_count": len(proposals),
        "detected_cluster_count": len(proposals) + len(rejected),
        "rejected_count": len(rejected),
        "contradiction_count": sum(1 for r in rejected if r["verdict"] == "CONTRADICTION"),
        "demote_count": sum(len(p["superseded"]) for p in proposals),
        "has_embeddings": has_embeddings,
        "artifact_path": artifact_path,
        "markdown": markdown,
        "stale_notes": stale_notes,
    }


# Console summary of the clusters adjudication refused (task #2088). UNPARSED
# leads because it says the adjudicator is broken, not that the notes differ.
_REFUSED_CLASS_ORDER = (_VERDICT_UNPARSED, VERDICT_NEEDS_REVIEW, "CONTRADICTION", "UNRELATED")
_SUMMARY_MAX_REFUSED = 20
_SUMMARY_MAX_CLUSTER_IDS = 12


def refused_clusters(reviews: list[dict], ids_of=lambda r: r["note_ids"]) -> list[dict]:
    """Project refused reviews to {note_ids, verdict}: no reason, no provider."""
    return [{"note_ids": list(ids_of(r)), "verdict": r["verdict"]} for r in reviews]


def refused_cluster_summary(refused: list[dict], limit: int = _SUMMARY_MAX_REFUSED) -> list[str]:
    """Console lines counting and naming every cluster adjudication refused (task #2088).

    Each entry is one of `refused_clusters`' {note_ids, verdict} records. Only
    the note ids and the verdict class are printed, never the model's reason,
    and both are collapsed to one line with the verdict in a literal code span,
    so model text cannot start a line that reads as an apply command. A verdict
    outside the known classes is counted as "other". The list is bounded; the
    rendered report carries the full detail.
    """
    order = {v: i for i, v in enumerate(_REFUSED_CLASS_ORDER)}
    counts = Counter(r["verdict"] if r["verdict"] in order else "other" for r in refused)
    parts = [f"{counts[c]} {c}" for c in (*_REFUSED_CLASS_ORDER, "other") if counts.get(c)]
    header = f"{len(refused)} cluster(s) refused by adjudication"
    lines = [f"{header}: {', '.join(parts)}." if parts else f"{header}."]
    ranked = sorted(
        refused,
        key=lambda r: (order.get(r["verdict"], len(order)), [str(i) for i in r["note_ids"]]),
    )
    for r in ranked[:limit]:
        ids = [one_line(i) for i in r["note_ids"]]
        rendered = ", ".join(ids[:_SUMMARY_MAX_CLUSTER_IDS])
        if len(ids) > _SUMMARY_MAX_CLUSTER_IDS:
            rendered += f", … (+{len(ids) - _SUMMARY_MAX_CLUSTER_IDS} more)"
        lines.append(f"  x {inline_code(r['verdict'])} {rendered}")
    if len(ranked) > limit:
        lines.append(f"  … (+{len(ranked) - limit} more; see the report)")
    return lines


def auto_apply_safe_merges(
    proposals: list[dict],
    actor: str | None = None,
    dry_run: bool = False,
    rejected: list[dict] | None = None,
) -> dict:
    """Apply the merges `build_proposals` marked auto-apply eligible.

    This is deliberately NOT part of `generate_proposal`, which is contractually
    read-only. Mutating memory is its own named, explicitly-invoked step — the
    same reason `apply_supersede` is not folded into the proposal path.

    Refuses outright unless ``UA_CONSOLIDATE_AUTO_APPLY`` is set, so enabling
    unreviewed merges is a deliberate act and never a side effect of running
    consolidation. Everything applied here writes only the `superseded_by`
    pointer and is reversible with `restore_note`, exactly like a human apply.

    Returns {enabled, applied, skipped, rejected, dry_run}, where `applied` and
    `skipped` each carry the survivor/demoted ids and the gate's reason, and
    `rejected` projects `build_proposals`' refused clusters to their note ids
    and verdict class (task #2088), so the summary can show what never reached
    the gate. The model's reason is dropped there; the `propose` report renders it.
    """
    refused = refused_clusters(rejected or [])
    if not auto_apply_enabled():
        return {
            "enabled": False,
            "applied": [],
            "skipped": [
                {
                    "survivor": p["survivor"]["id"],
                    "superseded": [s["id"] for s in p["superseded"]],
                    "reason": f"{_AUTO_APPLY_ENV} is not set",
                }
                for p in proposals
            ],
            "rejected": refused,
            "dry_run": dry_run,
        }

    applied: list[dict] = []
    skipped: list[dict] = []
    for proposal in proposals:
        verdict = proposal.get("auto_apply") or {"eligible": False, "reason": "no verdict recorded"}
        entry = {
            "survivor": proposal["survivor"]["id"],
            "superseded": [s["id"] for s in proposal["superseded"]],
            "reason": verdict["reason"],
        }
        if not verdict["eligible"]:
            skipped.append(entry)
            continue
        if not dry_run:
            apply_supersede(entry["survivor"], entry["superseded"], actor=actor)
        applied.append(entry)
    return {
        "enabled": True,
        "applied": applied,
        "skipped": skipped,
        "rejected": refused,
        "dry_run": dry_run,
    }


def apply_supersede(survivor_id: str, superseded_ids: list[str], actor: str | None = None) -> dict:
    """Explicit, human-invoked apply step: demote `superseded_ids` under `survivor_id`.

    Sets each demoted note's `superseded_by` to the survivor id. This is the ONLY
    place the consolidation flow mutates the DB, and it is never automatic. All
    provenance on the demoted notes (author, created_at, updated_at) is preserved
    — only the `superseded_by` pointer and the `valid_to` end of validity
    (task #2095: the survivor's valid_from, else now) are written — so
    `restore_note` reverses it exactly. Idempotent: a note already pointing at
    this survivor is left alone.
    """
    if not superseded_ids:
        raise ToolError("apply requires at least one note id to supersede")

    from .tools.memory import (
        _TENANT_SQL,
        _flush_indexes_best_effort,
        _is_deleted_row,
        _is_index_id,
        _mark_indexes_dirty,
        _note_tags,
        _supersede_valid_to,
    )

    # Tenant fence (task #1914): a note in another tenant reads exactly like a
    # missing id, as in tools/memory.py.
    org_id = edition.tenant_org_id()
    verified_actor = config.verified_agent_identity()
    if verified_actor:
        actor = config.verify_write_identity(actor)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        survivor = conn.execute(
            f"SELECT id, tags, superseded_by, valid_from FROM memory WHERE id=? AND {_TENANT_SQL}",
            (survivor_id, org_id),
        ).fetchone()
        if survivor is None:
            raise ToolError(f"survivor note {survivor_id!r} does not exist")
        if _is_deleted_row(survivor) or _is_index_id(survivor_id):
            raise ToolError(f"survivor note {survivor_id!r} is deleted or an index")
        if survivor["superseded_by"]:
            # Demoting notes onto an already-demoted survivor would build a
            # dangling chain that hides the survivor too. Refuse it.
            raise ToolError(
                f"survivor note {survivor_id!r} is itself superseded "
                f"(by {survivor['superseded_by']!r}); restore it first or pick another survivor"
            )

        demoted = []
        affected: set[str] = set()
        ts = storage.now()
        for sid in dict.fromkeys(superseded_ids):  # de-dupe, preserve order
            if sid == survivor_id:
                raise ToolError("a note cannot supersede itself")
            row = conn.execute(
                "SELECT id, tags, author, superseded_by, valid_from, created_at "
                f"FROM memory WHERE id=? AND {_TENANT_SQL}",
                (sid, org_id),
            ).fetchone()
            if row is None:
                raise ToolError(f"note {sid!r} does not exist")
            if _is_deleted_row(row) or _is_index_id(sid):
                raise ToolError(f"note {sid!r} is deleted or an index")
            if (
                verified_actor
                and config.actor_kind_for_identity(verified_actor) != "human"
                and row["author"] != verified_actor
            ):
                raise ToolError("only the note owner or a verified human can supersede it")
            if row["superseded_by"] == survivor_id:
                continue  # already demoted under this survivor — idempotent
            if row["superseded_by"]:
                # Re-pointing would silently move the note to another survivor
                # and orphan the earlier decision (task #1914).
                raise ToolError(
                    f"note {sid!r} is already superseded by {row['superseded_by']!r}; "
                    "restore it first to supersede it under another survivor"
                )
            # Only the pointer and valid_to are written: provenance columns are
            # left intact so the demotion is a clean, reversible overlay. The
            # IS NULL guard makes a concurrent apply lose rather than re-point.
            # valid_to (task #2095) invalidates rather than deletes: the
            # demoted note stopped being valid when the survivor's claim began.
            changed = conn.execute(
                "UPDATE memory SET superseded_by=?, valid_to=? "
                f"WHERE id=? AND superseded_by IS NULL AND {_TENANT_SQL}",
                (
                    survivor_id,
                    _supersede_valid_to(survivor["valid_from"], row, ts),
                    sid,
                    org_id,
                ),
            ).rowcount
            if changed != 1:
                raise ToolError(f"note {sid!r} was superseded concurrently; re-read it")
            # Chunks stay. Historical search still scores a superseded note,
            # and default search drops it with `superseded_by IS NULL`.
            # The actor goes in the activity log, in the same transaction, so
            # the demoted note's updated_by stays the author's (provenance).
            conn.execute(
                "INSERT INTO agent_events (agent, ts, kind, ref_id, summary) "
                "VALUES (?, ?, 'memory', ?, ?)",
                (actor, ts, sid, f"supersede: demoted under {survivor_id} (consolidate apply)"),
            )
            # The supersedes edge (task #2096), in the same transaction.
            memory_graph.add_link(
                conn, survivor_id, sid, "supersedes", actor=actor, created_at=ts, org_id=org_id
            )
            demoted.append(sid)
            affected |= set(_note_tags(row))
        if affected:
            _mark_indexes_dirty(conn, affected)
        conn.commit()
        for sid in demoted:
            try:
                from .events import mirror_agent_event

                mirror_agent_event(
                    "memory",
                    ref_id=sid,
                    agent=actor,
                    summary=f"supersede: demoted under {survivor_id} (consolidate apply)",
                )
            except Exception:
                pass
        _flush_indexes_best_effort(conn)
    finally:
        conn.close()

    if demoted:
        # Refresh offline snapshots; not a memory-row mutation.
        try:
            from .sync import sync_state_to_vault

            sync_state_to_vault()
        except Exception:
            pass

    return {
        "survivor_id": survivor_id,
        "superseded": demoted,
        "applied_count": len(demoted),
        "actor": actor,
        "ts": ts,
    }


def restore_note(note_id: str) -> dict:
    """Re-promote a demoted note or undo a soft delete through the shared restore path.

    Superseded and unchanged notes keep the existing {id, restored,
    was_superseded_by} result. Deleted notes also return the restored version
    and revision, subject to the tool's owner, human-delete, and tenant rules.
    """
    from .tools.memory import memory_restore

    result = memory_restore(note_id)
    if result["restored"]:
        # The CLI may exit immediately; keep its snapshot current before returning.
        try:
            from .sync import flush_pending_sync

            flush_pending_sync()
        except Exception:
            pass
    return result


def apply_batch(path: str, actor: str | None = None) -> dict:
    """Apply every surviving `apply` command found in a (possibly human-edited)
    proposal markdown file — the design doc's "Markdown Batch Apply" (D9).

    The file itself is the review UI: a reviewer deletes the code blocks for
    clusters they disagree with, then runs this against what's left. Commands
    are matched by exact text (`_APPLY_CMD_RX`), so a deleted block simply
    never appears here — no separate accept/reject schema is needed. Malformed
    or already-applied commands don't abort the batch: each is applied
    independently and its error (if any) is collected, so one bad block can't
    block the rest of a human's edits from landing.

    Only a ```bash fence holding nothing but apply lines counts (task #2012).
    An apply line outside such a fence came from rendered text, not from a
    proposal block, so it is counted in `ignored_unfenced` and never run.

    Returns {path, commands_found, applied: [apply_supersede results...],
    applied_count, errors: [{survivor_id, superseded_ids, error}, ...],
    ignored_unfenced}.
    """
    text = Path(path).read_text(encoding="utf-8")
    matches = fenced_apply_commands(text)
    ignored_unfenced = len(_APPLY_CMD_RX.findall(text)) - len(matches)

    applied = []
    errors = []
    for survivor_id, rest in matches:
        superseded_ids = rest.split()
        try:
            applied.append(apply_supersede(survivor_id, superseded_ids, actor=actor))
        except ToolError as e:
            errors.append(
                {"survivor_id": survivor_id, "superseded_ids": superseded_ids, "error": str(e)}
            )

    return {
        "path": path,
        "commands_found": len(matches),
        "applied": applied,
        "applied_count": sum(r["applied_count"] for r in applied),
        "errors": errors,
        "ignored_unfenced": ignored_unfenced,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assisted, reversible memory consolidation (propose / apply / restore)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_propose = sub.add_parser(
        "propose", help="Cluster near-duplicates and write a proposal artifact (read-only)."
    )
    p_propose.add_argument(
        "-t",
        "--threshold",
        type=float,
        default=None,
        help="Cosine similarity threshold (default: UA_CONSOLIDATE_THRESHOLD or 0.90).",
    )
    p_propose.add_argument(
        "-n",
        "--no-write",
        action="store_true",
        help="Do not write the artifact; just print the summary.",
    )

    p_apply = sub.add_parser(
        "apply", help="Explicitly demote notes under a survivor (sets superseded_by)."
    )
    p_apply.add_argument("survivor_id", help="Id of the surviving note.")
    p_apply.add_argument("superseded_ids", nargs="+", help="Ids of notes to supersede.")
    p_apply.add_argument("--actor", default=None, help="Optional actor label for the record.")

    p_restore = sub.add_parser("restore", help="Re-promote a demoted note (clears superseded_by).")
    p_restore.add_argument("note_id", help="Id of the note to restore.")

    p_apply_batch = sub.add_parser(
        "apply-batch",
        help="Apply every surviving 'apply' command in a (human-edited) proposal markdown file.",
    )
    p_apply_batch.add_argument("path", help="Path to the proposal markdown file.")
    p_apply_batch.add_argument("--actor", default=None, help="Optional actor label for the record.")

    p_auto = sub.add_parser(
        "auto-apply",
        help=(
            "Apply only the merges the safety gate marks eligible. "
            f"Requires {_AUTO_APPLY_ENV}; dry-run unless --apply is passed."
        ),
    )
    p_auto.add_argument(
        "-t",
        "--threshold",
        type=float,
        default=None,
        help="Propose threshold to cluster at (the auto gate has its own, higher floor).",
    )
    p_auto.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the superseded_by pointers (default: report what would apply).",
    )
    p_auto.add_argument("--actor", default=None, help="Optional actor label for the record.")

    args = parser.parse_args()

    try:
        post_init.register_default_hooks()
        storage.init_db()
        if args.command == "propose":
            res = generate_proposal(threshold=args.threshold, write=not args.no_write)
            rejected_note = (
                f" ({res['rejected_count']} rejected: {res['contradiction_count']} contradiction(s))"
                if res["rejected_count"]
                else ""
            )
            print(
                f"Scanned {res['note_count']} notes at threshold {res['threshold']}: "
                f"{res['detected_cluster_count']} cluster(s) detected, "
                f"{res['cluster_count']} merge proposal(s){rejected_note}, "
                f"{res['demote_count']} demotable note(s)."
            )
            if not res["has_embeddings"]:
                print(
                    "Note: no embeddings available — clustering degraded to no proposals.",
                    file=sys.stderr,
                )
            if res["artifact_path"]:
                print(f"Proposal written to: {res['artifact_path']}")
            elif not args.no_write:
                print("Could not write proposal artifact.", file=sys.stderr)
        elif args.command == "apply":
            res = apply_supersede(args.survivor_id, args.superseded_ids, actor=args.actor)
            print(
                f"Superseded {res['applied_count']} note(s) under survivor "
                f"{res['survivor_id']}: {', '.join(res['superseded']) or '(none / already applied)'}"
            )
        elif args.command == "restore":
            res = restore_note(args.note_id)
            if res.get("was_deleted"):
                print(f"Restored {res['id']} from deleted version {res['restored_version']}.")
            elif res["restored"]:
                print(f"Restored {res['id']} (was superseded by {res['was_superseded_by']}).")
            else:
                print(f"{res['id']} was not superseded; nothing to restore.")
        elif args.command == "auto-apply":
            thr = _threshold(args.threshold)
            conn = storage.connect()
            try:
                built = build_proposals(conn, thr)
            finally:
                conn.close()
            res = auto_apply_safe_merges(
                built["proposals"],
                actor=args.actor,
                dry_run=not args.apply,
                rejected=built["rejected"],
            )
            if not res["enabled"]:
                print(
                    f"auto-apply is off ({_AUTO_APPLY_ENV} unset) — "
                    f"{len(res['skipped'])} proposal(s) left for review."
                )
            else:
                verb = "would apply" if res["dry_run"] else "applied"
                print(
                    f"{verb} {len(res['applied'])} merge(s); "
                    f"{len(res['skipped'])} left for review "
                    f"(auto threshold {auto_apply_threshold():.4f})."
                )
                for entry in res["applied"]:
                    print(f"  + {entry['survivor']} <- {', '.join(entry['superseded'])}")
                for entry in res["skipped"]:
                    print(f"  - {entry['survivor']}: {entry['reason']}")
            for line in refused_cluster_summary(res["rejected"]):
                print(line)
        elif args.command == "apply-batch":
            res = apply_batch(args.path, actor=args.actor)
            print(
                f"Found {res['commands_found']} apply command(s) in {res['path']}: "
                f"{res['applied_count']} note(s) superseded across {len(res['applied'])} command(s)."
            )
            if res["ignored_unfenced"]:
                print(
                    f"{res['ignored_unfenced']} apply line(s) were not run: only a ```bash "
                    "fence holding nothing but apply lines is applied.",
                    file=sys.stderr,
                )
            if res["errors"]:
                print(f"{len(res['errors'])} command(s) failed:", file=sys.stderr)
                for err in res["errors"]:
                    print(f"  {err['survivor_id']}: {err['error']}", file=sys.stderr)
    except (ToolError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
