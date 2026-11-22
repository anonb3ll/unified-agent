"""PageRank-ranked token-budgeted codebase repository mapping tool (Task #125 / Phase 2.1)."""

import math

from mcp.server.mcpserver.exceptions import ToolError

from ..safety import resolve_in_workspace
from ..storage import connect
from .indexer import fs_index_symbols


def _estimate_tokens(text: str) -> int:
    """Rough estimate of token count (~4 characters per token)."""
    return math.ceil(len(text) / 4)


def fs_repo_map(path: str = ".", token_budget: int = 1000) -> dict:
    """Generate a PageRank-ranked, token-budgeted codebase structure map.

    Args:
        path: Workspace-relative path to map (default ".").
        token_budget: Target maximum token budget for the returned summary (default 1000).

    Returns:
        Dict containing workspace root, target path, token stats, and markdown repo_map.
    """
    resolved_target = resolve_in_workspace(path)
    if not resolved_target.exists():
        raise ToolError(f"Target path {path!r} does not exist.")

    workspace_root = str(resolve_in_workspace("."))
    try:
        rel_target = resolved_target.relative_to(workspace_root).as_posix()
    except ValueError:
        raise ToolError(f"Target path {path!r} is outside workspace.") from None

    if rel_target == ".":
        path_prefix = ""
    else:
        path_prefix = rel_target if rel_target.endswith("/") else rel_target + "/"

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, file_path, symbol_name, symbol_type, parent_name, signature, docstring, line_no "
            "FROM workspace_symbols WHERE workspace = ?",
            (workspace_root,),
        ).fetchall()
    finally:
        conn.close()

    # If workspace symbols are missing, run indexer
    if not rows:
        fs_index_symbols(".")
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, file_path, symbol_name, symbol_type, parent_name, signature, docstring, line_no "
                "FROM workspace_symbols WHERE workspace = ?",
                (workspace_root,),
            ).fetchall()
        finally:
            conn.close()

    # Filter symbols within requested target path
    if path_prefix:
        symbols = [
            r
            for r in rows
            if r["file_path"].startswith(path_prefix) or r["file_path"] == rel_target
        ]
    else:
        symbols = list(rows)

    if not symbols:
        map_text = f"# Repository Map: {path}\n\n*No indexed symbols found under {path!r}.*"
        return {
            "workspace": workspace_root,
            "path": path,
            "token_budget": token_budget,
            "estimated_tokens": _estimate_tokens(map_text),
            "repo_map": map_text,
        }

    # Group symbols by file
    files_map: dict[str, list[dict]] = {}
    for r in symbols:
        f_path = r["file_path"]
        files_map.setdefault(f_path, []).append(dict(r))

    # Graph construction for PageRank
    # Nodes = file paths and symbol identifiers
    nodes = list(files_map.keys())
    symbol_nodes = [f"{s['file_path']}::{s['symbol_name']}" for s in symbols]
    nodes.extend(symbol_nodes)

    num_nodes = len(nodes)
    if num_nodes == 0:
        map_text = f"# Repository Map: {path}\n\n*Empty codebase.*"
        return {
            "workspace": workspace_root,
            "path": path,
            "token_budget": token_budget,
            "estimated_tokens": _estimate_tokens(map_text),
            "repo_map": map_text,
        }

    # Adjacency list: out_edges[u] = list of v where u -> v
    out_edges: dict[str, list[str]] = {n: [] for n in nodes}
    in_edges: dict[str, list[str]] = {n: [] for n in nodes}

    def add_edge(u: str, v: str) -> None:
        if u in out_edges and v in in_edges:
            out_edges[u].append(v)
            in_edges[v].append(u)

    # Containment edges: File -> Symbol, Class -> Method
    for s in symbols:
        sym_id = f"{s['file_path']}::{s['symbol_name']}"
        add_edge(s["file_path"], sym_id)
        if s["parent_name"]:
            parent_id = f"{s['file_path']}::{s['parent_name']}"
            add_edge(parent_id, sym_id)

    # Symbol cross-reference edges: If symbol A's name appears in file B's symbols/docstrings/signatures
    sym_name_map = {s["symbol_name"]: f"{s['file_path']}::{s['symbol_name']}" for s in symbols}
    for s in symbols:
        sym_id = f"{s['file_path']}::{s['symbol_name']}"
        sig = s["signature"] or ""
        doc = s["docstring"] or ""
        combined_text = f"{sig} {doc}"

        for other_name, other_id in sym_name_map.items():
            if other_id != sym_id and len(other_name) > 3 and other_name in combined_text:
                add_edge(sym_id, other_id)

    # Power iteration PageRank
    d = 0.85
    pr = {n: 1.0 / num_nodes for n in nodes}

    for _ in range(20):
        new_pr = {}
        sink_sum = sum(pr[n] for n in nodes if not out_edges[n])
        for n in nodes:
            rank_sum = sum(pr[u] / len(out_edges[u]) for u in in_edges[n] if out_edges[u])
            new_pr[n] = ((1 - d) / num_nodes) + d * (rank_sum + sink_sum / num_nodes)
        pr = new_pr

    # Rank files by total PageRank score of file + contained symbols
    file_scores: list[tuple[str, float]] = []
    for f_path in files_map:
        total_score = pr.get(f_path, 0.0) + sum(
            pr.get(f"{f_path}::{s['symbol_name']}", 0.0) for s in files_map[f_path]
        )
        file_scores.append((f_path, total_score))

    file_scores.sort(key=lambda x: x[1], reverse=True)

    # Format Markdown map adhering to token budget
    lines = [f"# Codebase Map: {path}", ""]
    current_tokens = _estimate_tokens("\n".join(lines))

    for f_path, _ in file_scores:
        if current_tokens >= token_budget:
            lines.append("... [truncated to fit token budget]")
            break

        file_symbols = files_map[f_path]
        # Sort symbols in file by line_no
        file_symbols.sort(key=lambda s: s["line_no"])

        file_lines = [f"## `{f_path}`"]
        classes = [s for s in file_symbols if s["symbol_type"] == "class"]
        functions = [s for s in file_symbols if s["symbol_type"] == "function"]
        methods = [s for s in file_symbols if s["symbol_type"] == "method"]

        # Add top-level classes and their methods
        for c in classes:
            c_sig = f"  - `class {c['symbol_name']}`"
            if c["docstring"]:
                c_sig += f" — *{c['docstring']}*"
            file_lines.append(c_sig)

            c_methods = [m for m in methods if m["parent_name"] == c["symbol_name"]]
            for m in c_methods:
                m_sig = f"    - `def {m['symbol_name']}{m['signature']}`"
                file_lines.append(m_sig)

        # Add top-level functions
        for func in functions:
            if not func["parent_name"]:
                fn_sig = f"  - `def {func['symbol_name']}{func['signature']}`"
                if func["docstring"]:
                    fn_sig += f" — *{func['docstring']}*"
                file_lines.append(fn_sig)

        file_lines.append("")
        block_text = "\n".join(file_lines)
        block_tokens = _estimate_tokens(block_text)

        if current_tokens + block_tokens > token_budget and len(lines) > 2:
            lines.append("... [truncated to fit token budget]")
            break

        lines.extend(file_lines)
        current_tokens += block_tokens

    map_text = "\n".join(lines).strip()
    return {
        "workspace": workspace_root,
        "path": path,
        "token_budget": token_budget,
        "estimated_tokens": _estimate_tokens(map_text),
        "repo_map": map_text,
    }
