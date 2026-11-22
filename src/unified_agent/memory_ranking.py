"""Rank fusion for ``memory_search`` and session-packet memory (task #1913).

``memory_search`` used to add uncalibrated scores (cosine similarity, a clipped
bm25, recency, salience, access count, feedback, entity matches, recall votes)
into one weighted sum. The priors were added to every candidate whatever its
match quality, so a popular note outranked a better match, and every return
bumped the access count and a recall vote that fed the next ranking.

This module holds the replacement, kept apart from the write paths in
``tools/memory.py``:

- **Lexical leg.** FTS5 AND of every query token first; when that finds fewer
  than ``UA_MEMORY_FTS_OR_THRESHOLD`` notes, an OR of the non-stopword tokens
  adds partial matches after the AND hits. Both queries are ordered by bm25
  and bounded by ``UA_MEMORY_FTS_LIMIT``.
- **Reciprocal rank fusion.** Each leg (lexical, semantic, entity, recency,
  graph) is ranked on its own, and a note scores ``sum(w_leg / (k + rank_leg))``
  over the legs it appears in (``k`` is ``UA_MEMORY_RRF_K``). Ranks only compare
  notes within a leg, so no leg's raw scale can swamp another.
- **Bounded prior.** Salience, effective confidence, explicit upvotes and the
  age discount multiply the fused score by a factor clamped to [0.8, 1.2].
  Access count and the automatic recall count are not ranking inputs.
- **Evolving confidence (task #2097, M7).** A note's feedback counts are the
  evidence of a Beta posterior (``confidence_posterior``); each open
  ``contradicts`` link against the note adds pseudo-failures at read time
  (``effective_confidence``). Nothing is stored: the posterior is a pure
  function of the four persisted feedback counters, so it travels with them
  through export and import.
"""

from __future__ import annotations

import math
import os
import sqlite3
from collections.abc import Callable, Hashable, Iterable
from typing import Any

PRIOR_MIN = 0.8
PRIOR_MAX = 1.2
LEGS = ("lexical", "semantic", "entity", "recency", "graph")

# Leg weights. The env names predate fusion (they weighted raw scores in the
# old linear sum); they now scale a leg's reciprocal-rank contribution. The
# graph leg (task #2096) is one hop from the top hits over memory_links and
# shared entities; see memory_graph.
_LEG_WEIGHT_ENV = {
    "lexical": ("UA_MEMORY_WEIGHT_FTS", 1.0),
    "semantic": ("UA_MEMORY_WEIGHT_SEMANTIC", 1.0),
    "entity": ("UA_MEMORY_WEIGHT_ENTITY", 1.0),
    "recency": ("UA_MEMORY_WEIGHT_RECENCY", 0.2),
    "graph": ("UA_MEMORY_WEIGHT_GRAPH", 0.5),
}

# Words that carry no retrieval signal on their own. Only the OR fallback drops
# them: an OR of "the" would match nearly every note. The AND pass keeps every
# token, as before.
STOPWORDS = frozenset(
    """
    a about after again all am an and any are as at be been before being but by
    can cannot could did do does doing down during each few for from further had
    has have having he her here hers how i if in into is it its itself just me
    more most my no nor not now of off on once only or other our ours out over
    own same she should so some such than that the their theirs them then there
    these they this those through to too under until up very was we were what
    when where which while who whom why will with would you your yours
    """.split()
)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= minimum else default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value >= minimum else default


def rrf_k() -> float:
    """RRF smoothing constant: larger flattens the gap between adjacent ranks."""
    return _env_float("UA_MEMORY_RRF_K", 60.0, minimum=1.0)


def fts_limit() -> int:
    return _env_int("UA_MEMORY_FTS_LIMIT", 200)


def fts_or_threshold() -> int:
    """Run the OR fallback when the AND pass finds fewer notes than this."""
    return _env_int("UA_MEMORY_FTS_OR_THRESHOLD", 10, minimum=0)


def semantic_min_similarity() -> float:
    """Cosine floor for the semantic leg. bge-small scores unrelated short
    texts at 0.55-0.60, so the old 0.55 gate let noise in."""
    return _env_float("UA_MEMORY_SEMANTIC_MIN_SIMILARITY", 0.65)


def semantic_top_k() -> int:
    return _env_int("UA_MEMORY_SEMANTIC_TOP_K", 50)


def leg_weights() -> dict[str, float]:
    return {leg: _env_float(env, default) for leg, (env, default) in _LEG_WEIGHT_ENV.items()}


def _phrase(token: str) -> str:
    # A quoted phrase neutralises FTS5 operators; an embedded quote is the one
    # character that could close it early, so it is stripped.
    return '"' + token.replace('"', "") + '"'


def fts_and_query(tokens: Iterable[str]) -> str:
    return " ".join(_phrase(t) for t in tokens if t.replace('"', "").strip())


def or_tokens(tokens: Iterable[str]) -> list[str]:
    """Distinct non-stopword tokens for the OR fallback, in query order."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        cleaned = tok.replace('"', "").strip()
        key = cleaned.casefold().strip(".,;:!?()[]{}'")
        if not cleaned or key in STOPWORDS or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def fts_or_query(tokens: Iterable[str]) -> str:
    return " OR ".join(_phrase(t) for t in or_tokens(tokens))


def lexical_leg(
    conn: sqlite3.Connection,
    tokens: list[str],
    tenant: str | None,
    *,
    eligibility_sql: str = "",
    eligibility_params: list[Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """FTS matches in lexical rank order: AND hits by bm25, then OR-only hits.

    Returns ``{note_id: {"score": bm25, "snippet": str, "mode": "and"|"or"}}``;
    dict order is the leg's rank order. FTS syntax errors yield no matches.
    """
    limit = fts_limit()
    eligibility = (
        f" AND m.id IN (SELECT id FROM memory WHERE {eligibility_sql})" if eligibility_sql else ""
    )
    sql = (
        "SELECT m.id, bm25(memory_fts) AS score, "
        "snippet(memory_fts, 1, '[', ']', ' … ', 20) AS snip "
        "FROM memory_fts JOIN memory m ON m.rowid = memory_fts.rowid "
        "WHERE memory_fts MATCH ? AND m.org_id IS ? "
        f"{eligibility} ORDER BY score, m.id LIMIT ?"
    )
    matches: dict[str, dict[str, Any]] = {}

    def _run(query: str, mode: str) -> None:
        if not query:
            return
        try:
            rows = conn.execute(sql, (query, tenant, *(eligibility_params or []), limit)).fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            if row["id"] not in matches and len(matches) < limit:
                matches[row["id"]] = {"score": row["score"], "snippet": row["snip"], "mode": mode}

    _run(fts_and_query(tokens), "and")
    if len(matches) < fts_or_threshold():
        or_query = fts_or_query(tokens)
        # A single token's OR is the AND already run.
        if or_query and or_query != fts_and_query(tokens):
            _run(or_query, "or")
    return matches


def competition_ranks(items: Iterable[str], key: Callable[[str], Hashable]) -> dict[str, int]:
    """1-based ranks with ties sharing the best rank ("1224" ranking).

    ``key`` sorts ascending (best first). Tied notes must not be split by an
    arbitrary order inside one leg, so equal keys share a rank.
    """
    ordered = sorted(items, key=key)
    ranks: dict[str, int] = {}
    prev: Any = object()
    current = 0
    for index, item in enumerate(ordered, start=1):
        value = key(item)
        if value != prev:
            current = index
            prev = value
        ranks[item] = current
    return ranks


def slotted_ranks(
    base: Iterable[str], extra: Iterable[str], key: Callable[[str], Hashable]
) -> dict[str, int]:
    """Competition ranks for ``base``, plus where each ``extra`` item would slot.

    An ``extra`` item takes ``1 +`` the number of base items strictly better
    than it, as if it alone joined the ranking, so adding extras never moves a
    base item's rank. The graph leg (task #2096) uses it to give the notes it
    reached a recency and entity rank without shifting the content matches'.
    """
    base = list(base)
    ranks = competition_ranks(base, key=key)
    keys = sorted(key(i) for i in base)
    for item in extra:
        value = key(item)
        ranks[item] = 1 + sum(1 for k in keys if k < value)
    return ranks


def fuse(
    ranks: dict[str, int | None], weights: dict[str, float], k: float
) -> tuple[float, dict[str, float]]:
    """Weighted reciprocal rank fusion over the legs a note appears in."""
    parts = {
        leg: weights.get(leg, 0.0) / (k + rank)
        for leg, rank in ranks.items()
        if rank is not None and weights.get(leg, 0.0) > 0
    }
    return sum(parts.values()), parts


# Beta posterior over "this note is right and useful" (task #2097, M7).
# Prior Beta(1, 1): an unrated note sits at 0.5, which the prior maps to a
# zero input, so notes without feedback rank exactly as before.
CONFIDENCE_PRIOR = (1.0, 1.0)
# memory_feedback signal -> (alpha, beta) increment per count. helpful is one
# success. not_helpful is one failure. stale is half a failure: the note was
# probably right once and has aged. wrong is two failures: an explicit
# falsification outweighs one "did not help".
FEEDBACK_INCREMENTS = {
    "helpful": (1.0, 0.0),
    "not_helpful": (0.0, 1.0),
    "stale": (0.0, 0.5),
    "wrong": (0.0, 2.0),
}
# Each open contradiction (a live note disputing this one) is one
# pseudo-failure, the weight of a not_helpful. At most this many count, so a
# pile-on of disputing notes cannot drive the effective confidence to zero.
CONTRADICTION_BETA = 1.0
MAX_COUNTED_CONTRADICTIONS = 5


def confidence_posterior(
    helpful: int, not_helpful: int, stale: int, wrong: int
) -> tuple[float, float]:
    """(alpha, beta) of a note's Beta posterior from its feedback counts."""
    counts = {
        "helpful": helpful,
        "not_helpful": not_helpful,
        "stale": stale,
        "wrong": wrong,
    }
    alpha, beta = CONFIDENCE_PRIOR
    for signal, count in counts.items():
        da, db = FEEDBACK_INCREMENTS[signal]
        n = max(0, int(count or 0))
        alpha += da * n
        beta += db * n
    return alpha, beta


def effective_confidence(alpha: float, beta: float, open_contradictions: int) -> float:
    """Posterior mean after counting open contradictions as pseudo-failures."""
    counted = min(max(0, int(open_contradictions)), MAX_COUNTED_CONTRADICTIONS)
    return alpha / (alpha + beta + CONTRADICTION_BETA * counted)


def confidence_summary(
    helpful: int, not_helpful: int, stale: int, wrong: int, open_contradictions: int
) -> dict[str, float | int]:
    """The ``confidence_posterior`` block memory_get and memory_search report."""
    alpha, beta = confidence_posterior(helpful, not_helpful, stale, wrong)
    return {
        "alpha": round(alpha, 4),
        "beta": round(beta, 4),
        "posterior": round(alpha / (alpha + beta), 4),
        "open_contradictions": min(int(open_contradictions), MAX_COUNTED_CONTRADICTIONS),
        "effective": round(effective_confidence(alpha, beta, open_contradictions), 4),
    }


def prior_multiplier(
    *,
    salience: float | None,
    confidence: float,
    vote: float,
    age_discount: float,
    w_salience: float,
    w_feedback: float,
    w_vote: float,
) -> tuple[float, dict[str, float]]:
    """Bounded multiplier from the quality priors, in [PRIOR_MIN, PRIOR_MAX].

    Salience is centred on its 0.5 default (so an unset note is neutral),
    ``confidence`` is the effective confidence in [0, 1] (task #2097), centred
    the same way, and an explicit-upvote vote is in [0, 1). The weighted sum
    is squashed with tanh to +/-20 %, then the age discount (0.5..1 for notes
    past UA_MEMORY_DECAY_MONTHS) applies and the result is clamped. Returns
    the multiplier and each prior's signed input.
    """
    sal = float(salience if salience is not None else 0.5)
    conf = min(1.0, max(0.0, float(confidence)))
    inputs = {
        "salience": w_salience * (sal - 0.5) * 2.0,
        "confidence": w_feedback * (conf - 0.5) * 2.0,
        "vote": w_vote * vote,
    }
    raw = 1.0 + (PRIOR_MAX - 1.0) * math.tanh(sum(inputs.values()))
    return min(PRIOR_MAX, max(PRIOR_MIN, raw * age_discount)), inputs
