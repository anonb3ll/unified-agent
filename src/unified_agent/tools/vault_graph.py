"""Embedding-free graph retrieval over the Obsidian [[wikilink]] graph.

Inspired by the obsidian-llm-wiki plugin: lexical seeding followed by
Personalized PageRank (Haveliwala 2002, "Topic-Sensitive PageRank") walked
over the note graph induced by [[wikilinks]]. No embedding index, no
network call, no third-party dependency — pure stdlib dicts of floats.
Complements vault_search (exact regex) and any external semantic search
when neither an index nor connectivity is available.
"""

import re
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import config
from ..safety import resolve_in_vault
from .vault import WIKILINK_RE, _iter_notes, _vault_rel

# Lowercase alnum runs of 2+ chars; short/stopword-ish tokens are dropped.
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# First H1 heading in a note (cheap title source; wiki.py's fuller resolver
# is not imported here to keep this module dependency-free of the wiki layer).
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

_DAMPING = 0.85
_MAX_ITERS = 30
_CONVERGENCE_EPS = 1e-6


def _tokenize(query: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(query.lower()) if len(t) >= 2]


def _note_title(content: str, stem: str) -> str:
    m = _H1_RE.search(content)
    return m.group(1).strip() if m else stem


def _snippet(content: str, terms: list[str]) -> str:
    """First line containing a query term; else the first non-empty line."""
    lines = content.splitlines()
    for line in lines:
        low = line.lower()
        if any(t in low for t in terms):
            return line.strip()[:200]
    for line in lines:
        if line.strip():
            return line.strip()[:200]
    return ""


def vault_query(query: str, path: str = ".", max_results: int = 10) -> dict:
    """Embedding-free graph retrieval over [[wikilinks]]: lexical seeds walked

    through Personalized PageRank (Haveliwala 2002). Complements vault_search
    (exact regex match) and any external semantic search index — this tool
    needs no index and no network, just the note graph on disk.

    Returns {results: [{path, title, score, lexical, pagerank, snippet,
    links_out, links_in}], graph: {notes, edges, seeds}}. max_results is
    capped at 50.
    """
    if not query or not query.strip():
        raise ToolError("query must not be empty")
    root = resolve_in_vault(path)
    if not root.is_dir():
        raise ToolError(f"not a directory: {path}")
    cap = max(1, min(max_results, 50))

    # --- Stage 1: corpus walk ------------------------------------------------
    # First note wins on stem collision; canonical id stays the vault-relative
    # path so callers can vault_read/vault_links straight off a result.
    notes: dict[str, dict] = {}
    order: list[str] = []
    for f in _iter_notes(root):
        if f.stat().st_size > config.SEARCH_FILE_MAX_BYTES:
            continue
        stem = f.stem
        if stem in notes:
            continue
        content = f.read_text(errors="replace")
        targets = [Path(m.group(1).strip()).stem for m in WIKILINK_RE.finditer(content)]
        notes[stem] = {
            "path": _vault_rel(f),
            "stem": stem,
            "title": _note_title(content, stem),
            "content": content,
            "content_lower": content.lower(),
            "targets": targets,
        }
        order.append(stem)

    if not order:
        return {"results": [], "graph": {"notes": 0, "edges": 0, "seeds": 0}}

    # --- Stage 2: graph build -------------------------------------------------
    # Directed out/in adjacency for reporting (links_out/links_in), plus a
    # bidirectional union used for the PPR walk so backlink neighborhoods are
    # reachable and dead ends (notes with only incoming links) are rare.
    out_adj: dict[str, set[str]] = {s: set() for s in order}
    in_adj: dict[str, set[str]] = {s: set() for s in order}
    for s in order:
        for t in notes[s]["targets"]:
            if t in notes and t != s:
                out_adj[s].add(t)
                in_adj[t].add(s)
    undirected = {s: out_adj[s] | in_adj[s] for s in order}
    edge_count = sum(len(v) for v in out_adj.values())

    # --- Stage 3: lexical seeding ----------------------------------------------
    terms = _tokenize(query)
    lexical: dict[str, float] = {}
    for s in order:
        note = notes[s]
        haystack = f"{note['stem']} {note['title']}".lower()
        score = 0.0
        for term in terms:
            if term in haystack:
                score += 3.0
            count = note["content_lower"].count(term)
            if count:
                score += 1.0 + 0.1 * min(count, 10)
        if score > 0:
            lexical[s] = score

    graph_stats = {"notes": len(order), "edges": edge_count, "seeds": len(lexical)}
    if not lexical:
        return {"results": [], "graph": graph_stats}

    # --- Stage 4: Personalized PageRank ----------------------------------------
    # Power iteration with damping d=0.85. Teleport vector is the seed scores
    # normalized to sum 1; a node's mass splits uniformly across its (union)
    # neighbors each step; dangling nodes (no neighbors) hand their mass back
    # to the teleport distribution instead of vanishing.
    seed_total = sum(lexical.values())
    teleport = {s: v / seed_total for s, v in lexical.items()}
    p = {s: teleport.get(s, 0.0) for s in order}
    for _ in range(_MAX_ITERS):
        next_p = {s: (1 - _DAMPING) * teleport.get(s, 0.0) for s in order}
        dangling_mass = 0.0
        for s in order:
            mass = _DAMPING * p[s]
            neighbors = undirected[s]
            if neighbors:
                share = mass / len(neighbors)
                for nb in neighbors:
                    next_p[nb] += share
            else:
                dangling_mass += mass
        if dangling_mass:
            for s in order:
                next_p[s] += dangling_mass * teleport.get(s, 0.0)
        delta = sum(abs(next_p[s] - p[s]) for s in order)
        p = next_p
        if delta < _CONVERGENCE_EPS:
            break

    # --- Stage 5: final ranking ----------------------------------------------
    # Normalize both signals to a 0..1 scale before combining so neither the
    # lexical formula's magnitude nor PPR's probability-mass scale dominates.
    max_lex = max(lexical.values())
    max_ppr = max(p.values()) if p else 0.0
    ranked = []
    for s in order:
        lex = lexical.get(s, 0.0)
        ppr = p.get(s, 0.0)
        if lex == 0.0 and ppr == 0.0:
            continue
        lex_norm = lex / max_lex if max_lex else 0.0
        ppr_norm = ppr / max_ppr if max_ppr else 0.0
        final = 0.5 * lex_norm + 0.5 * ppr_norm
        ranked.append((final, lex_norm, ppr_norm, s))
    ranked.sort(key=lambda r: r[0], reverse=True)

    results = []
    for final, lex_norm, ppr_norm, s in ranked[:cap]:
        note = notes[s]
        results.append(
            {
                "path": note["path"],
                "title": note["title"],
                "score": round(final, 4),
                "lexical": round(lex_norm, 4),
                "pagerank": round(ppr_norm, 4),
                "snippet": _snippet(note["content"], terms),
                "links_out": len(out_adj[s]),
                "links_in": len(in_adj[s]),
            }
        )

    return {"results": results, "graph": graph_stats}
