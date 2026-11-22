"""Safe local codebase symbol indexing and directory mapping using AST."""

import ast
import os
import threading
import time
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from ..safety import resolve_in_workspace
from ..storage import compatible_embedding_model_clause, connect

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".next",
    "target",
}


def parse_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Format argument signature of a function or method node."""
    parts = []
    # 1. Positional-only args
    if hasattr(node.args, "posonlyargs") and node.args.posonlyargs:
        for arg in node.args.posonlyargs:
            parts.append(arg.arg)
        parts.append("/")

    # 2. Standard positional args
    for arg in node.args.args:
        parts.append(arg.arg)

    # 3. *args
    if node.args.vararg:
        parts.append(f"*{node.args.vararg.arg}")

    # 4. Keyword-only args
    if node.args.kwonlyargs:
        if not node.args.vararg:
            parts.append("*")
        for arg in node.args.kwonlyargs:
            parts.append(arg.arg)

    # 5. **kwargs
    if node.args.kwarg:
        parts.append(f"**{node.args.kwarg.arg}")

    return f"({', '.join(parts)})"


def extract_symbols(node, parent_class=None) -> list[dict]:
    """Recursively extract class, method, and function symbols from an AST node."""
    symbols = []
    if isinstance(node, ast.ClassDef):
        doc = ast.get_docstring(node) or ""
        symbols.append(
            {
                "name": node.name,
                "type": "class",
                "parent": None,
                "signature": "",
                "docstring": doc.split("\n")[0] if doc else "",
                "line_no": node.lineno,
            }
        )
        for child in node.body:
            symbols.extend(extract_symbols(child, parent_class=node.name))
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        sig = parse_signature(node)
        doc = ast.get_docstring(node) or ""
        symbols.append(
            {
                "name": node.name,
                "type": "method" if parent_class else "function",
                "parent": parent_class,
                "signature": sig,
                "docstring": doc.split("\n")[0] if doc else "",
                "line_no": node.lineno,
            }
        )
    return symbols


def parse_file_symbols(content: str) -> list[dict]:
    """Parse python code and return top-level class, function, and class method declarations."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    symbols = []
    for node in tree.body:
        symbols.extend(extract_symbols(node))
    return symbols


def walk_ts_nodes(node, content_bytes, parent_class=None) -> list[dict]:
    """Recursively walk tree-sitter nodes to find classes, methods, and functions in JS/TS."""
    symbols = []
    node_type = node.type

    sym = None
    if node_type == "class_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = content_bytes[name_node.start_byte : name_node.end_byte].decode(
                "utf8", errors="replace"
            )
            sym = {
                "name": name,
                "type": "class",
                "parent": None,
                "signature": "",
                "docstring": "",
                "line_no": node.start_point[0] + 1,
            }
            symbols.append(sym)
            for child in node.children:
                symbols.extend(walk_ts_nodes(child, content_bytes, parent_class=name))
            return symbols

    elif node_type == "function_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = content_bytes[name_node.start_byte : name_node.end_byte].decode(
                "utf8", errors="replace"
            )
            sig = ""
            for child in node.children:
                if child.type == "formal_parameters":
                    sig = content_bytes[child.start_byte : child.end_byte].decode(
                        "utf8", errors="replace"
                    )
                    break
            sym = {
                "name": name,
                "type": "function",
                "parent": parent_class,
                "signature": sig,
                "docstring": "",
                "line_no": node.start_point[0] + 1,
            }

    elif node_type in (
        "method_definition",
        "public_method_definition",
        "private_method_definition",
    ):
        name_node = node.child_by_field_name("name")
        if name_node:
            name = content_bytes[name_node.start_byte : name_node.end_byte].decode(
                "utf8", errors="replace"
            )
            sig = ""
            for child in node.children:
                if child.type == "formal_parameters":
                    sig = content_bytes[child.start_byte : child.end_byte].decode(
                        "utf8", errors="replace"
                    )
                    break
            sym = {
                "name": name,
                "type": "method",
                "parent": parent_class,
                "signature": sig,
                "docstring": "",
                "line_no": node.start_point[0] + 1,
            }

    elif node_type == "variable_declarator":
        val_node = node.child_by_field_name("value")
        if val_node and val_node.type in ("arrow_function", "function_expression"):
            id_node = node.child_by_field_name("name") or node.child_by_field_name("id")
            if id_node and id_node.type == "identifier":
                name = content_bytes[id_node.start_byte : id_node.end_byte].decode(
                    "utf8", errors="replace"
                )
                sig = ""
                for child in val_node.children:
                    if child.type in ("formal_parameters", "parameter_list"):
                        sig = content_bytes[child.start_byte : child.end_byte].decode(
                            "utf8", errors="replace"
                        )
                        break
                sym = {
                    "name": name,
                    "type": "function",
                    "parent": parent_class,
                    "signature": sig,
                    "docstring": "",
                    "line_no": node.start_point[0] + 1,
                }

    if sym:
        symbols.append(sym)

    for child in node.children:
        symbols.extend(walk_ts_nodes(child, content_bytes, parent_class))

    return symbols


JS_TS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")

# Degradation reasons, ordered from "nothing is installed" to "this one file failed".
DEGRADED_NO_PROVIDER = "no_tree_sitter_provider"
DEGRADED_NO_GRAMMAR = "grammar_unavailable"
DEGRADED_PARSE_FAILED = "parse_failed"

_DEGRADED_REMEDIES = {
    DEGRADED_NO_PROVIDER: (
        "tree_sitter_language_pack is not importable; it is a required dependency (uv sync)"
    ),
    DEGRADED_NO_GRAMMAR: (
        "a tree-sitter provider is installed but its JS/TS grammar could not be loaded"
    ),
    DEGRADED_PARSE_FAILED: "the tree-sitter parser raised while parsing this file",
}


def tree_sitter_available() -> bool:
    """Whether a tree-sitter provider can be imported for JS/TS parsing.

    Deliberately uncached: the doctor's `tree_sitter_index` extra can be installed
    into a running hub, and a cached "missing" would keep reporting degraded long
    after the install fixed it.
    """
    return _load_ts_parser_factory() is not None


def _load_ts_parser_factory():
    """Return tree-sitter-language-pack's get_parser, or None if it is not importable."""
    try:
        from tree_sitter_language_pack import get_parser

        return get_parser
    except ImportError:
        return None


def parse_js_ts_symbols_with_status(content: str, filename: str) -> tuple[list[dict], str | None]:
    """Parse JS/TS symbols, reporting *why* nothing came back.

    Returns (symbols, degraded_reason). An empty list with a `None` reason means the
    file genuinely holds no symbols; an empty list with a reason means tree-sitter
    could not read it. Keeping those two cases apart is the whole point — callers
    previously saw an identical `[]` for "empty file" and "AST indexing is off",
    so a hub missing tree-sitter-language-pack reported a clean index over zero JS/TS
    symbols and nothing anywhere said otherwise.
    """
    get_parser = _load_ts_parser_factory()
    if get_parser is None:
        return [], DEGRADED_NO_PROVIDER

    ext = Path(filename).suffix.lower()
    lang_name = "javascript"
    if ext in (".ts", ".tsx"):
        lang_name = "typescript"

    try:
        parser = get_parser(lang_name)
    except Exception:
        # Graceful degradation if grammar is not installed
        return [], DEGRADED_NO_GRAMMAR

    try:
        content_bytes = bytes(content, "utf8")
        tree = parser.parse(content_bytes)
        return walk_ts_nodes(tree.root_node, content_bytes), None
    except Exception:
        return [], DEGRADED_PARSE_FAILED


def parse_js_ts_symbols(content: str, filename: str) -> list[dict]:
    """Parse JS/TS/JSX/TSX file content and extract symbols using tree-sitter."""
    symbols, _ = parse_js_ts_symbols_with_status(content, filename)
    return symbols


def _degraded_advisory(reasons: dict[str, int], files_affected: int) -> dict:
    """Build the advisory block attached to a degraded response."""
    primary = next(
        (
            r
            for r in (DEGRADED_NO_PROVIDER, DEGRADED_NO_GRAMMAR, DEGRADED_PARSE_FAILED)
            if r in reasons
        ),
        DEGRADED_PARSE_FAILED,
    )
    return {
        "mode": "degraded",
        "reason": primary,
        "detail": _DEGRADED_REMEDIES[primary],
        "js_ts_files_affected": files_affected,
        "reason_counts": dict(reasons),
        "warning": (
            f"Symbol indexing ran in degraded (non-AST) mode for {files_affected} "
            f"JS/TS file(s): {_DEGRADED_REMEDIES[primary]}. Results for those files are "
            "incomplete — treat a missing symbol as unknown, not absent."
        ),
    }


def fs_index_symbols(path: str = ".") -> dict:
    """Scan and index all code symbols in the specified directory.

    Updates the shared local SQLite symbol database.
    """
    resolved_path = resolve_in_workspace(path)
    if not resolved_path.exists():
        raise ToolError(f"Target path {path!r} does not exist.")

    workspace_root = str(resolve_in_workspace("."))

    files_scanned = 0
    symbols_found = []
    degraded_reasons: dict[str, int] = {}
    degraded_files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(resolved_path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith((".py",) + JS_TS_EXTENSIONS):
                continue
            full_path = Path(dirpath) / name
            try:
                rel_path = full_path.relative_to(workspace_root).as_posix()
            except ValueError:
                continue

            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if name.endswith(".py"):
                file_symbols = parse_file_symbols(content)
            else:
                file_symbols, reason = parse_js_ts_symbols_with_status(content, name)
                if reason:
                    degraded_reasons[reason] = degraded_reasons.get(reason, 0) + 1
                    if len(degraded_files) < 20:
                        degraded_files.append(rel_path)

            for sym in file_symbols:
                sym["file_path"] = rel_path
            symbols_found.extend(file_symbols)
            files_scanned += 1

    # Save to DB
    conn = connect()
    try:
        conn.execute("DELETE FROM workspace_symbols WHERE workspace = ?", (workspace_root,))
        conn.executemany(
            "INSERT INTO workspace_symbols (workspace, file_path, symbol_name, symbol_type, parent_name, signature, docstring, line_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    workspace_root,
                    sym["file_path"],
                    sym["name"],
                    sym["type"],
                    sym["parent"],
                    sym["signature"],
                    sym["docstring"],
                    sym["line_no"],
                )
                for sym in symbols_found
            ],
        )
        conn.commit()
    finally:
        conn.close()

    # Start symbol embeddings backfill in the background
    t = threading.Thread(target=_backfill_symbol_embeddings, daemon=True)
    t.start()
    _symbol_backfill_threads.append(t)

    result = {
        "workspace": workspace_root,
        "files_indexed": files_scanned,
        "symbols_indexed": len(symbols_found),
        "classes": sum(1 for s in symbols_found if s["type"] == "class"),
        "functions": sum(1 for s in symbols_found if s["type"] == "function"),
        "methods": sum(1 for s in symbols_found if s["type"] == "method"),
        "degraded": bool(degraded_reasons),
    }
    if degraded_reasons:
        advisory = _degraded_advisory(degraded_reasons, sum(degraded_reasons.values()))
        advisory["sample_files"] = degraded_files
        result["degraded_detail"] = advisory
        result["warning"] = advisory["warning"]
    return result


_symbol_backfill_threads: list[threading.Thread] = []

# Symbols embedded per write transaction in the background backfill.
SYMBOL_BACKFILL_COMMIT_EVERY = 50


def join_symbol_backfill_threads(timeout: float = 30.0) -> None:
    """Wait for fs_index_symbols background embedding backfill threads."""
    for t in _symbol_backfill_threads:
        t.join(timeout=timeout)


def _backfill_symbol_embeddings() -> None:
    """Best-effort backfill for workspace symbols that lack embeddings."""
    try:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, symbol_name, symbol_type, file_path, signature, docstring FROM workspace_symbols "
                "WHERE id NOT IN (SELECT symbol_id FROM workspace_symbol_embeddings)"
            ).fetchall()
            if rows:
                from ..config import backfill_batch_pause_s, embed_model
                from ..embeddings import (
                    embedding_to_bytes,
                    generate_embedding,
                    is_model_load_failure,
                )

                pause = backfill_batch_pause_s()
                model_unavailable = False
                for start in range(0, len(rows), SYMBOL_BACKFILL_COMMIT_EVERY):
                    if model_unavailable:
                        break
                    if start and pause:
                        # Yield between batches so request handling keeps up.
                        time.sleep(pause)
                    batch = rows[start : start + SYMBOL_BACKFILL_COMMIT_EVERY]
                    # Embed with no transaction open, then write and commit the
                    # batch, so other writers never wait on the model.
                    vectors = []
                    for row in batch:
                        try:
                            text = f"{row['symbol_type']} {row['symbol_name']} in {row['file_path']}: {row['signature'] or ''}\n\nDocstring: {row['docstring'] or ''}"
                            vectors.append(
                                (row["id"], embedding_to_bytes(generate_embedding(text)))
                            )
                        except Exception as exc:
                            # The model cannot be loaded: every later symbol
                            # would retry the load the same way (task #1850).
                            if is_model_load_failure(exc):
                                model_unavailable = True
                                break
                    for symbol_id, emb_bytes in vectors:
                        try:
                            # A reindex may have deleted the symbol meanwhile;
                            # ids are AUTOINCREMENT, so never reused.
                            conn.execute(
                                "INSERT OR REPLACE INTO workspace_symbol_embeddings "
                                "(symbol_id, embedding, model) "
                                "SELECT id, ?, ? FROM workspace_symbols WHERE id = ?",
                                (emb_bytes, embed_model(), symbol_id),
                            )
                        except Exception:
                            pass
                    conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _with_search_advisory(result: dict) -> dict:
    """Flag searches served from an index that could not have held JS/TS symbols.

    Search reads the database, so the degradation it must report happened earlier,
    at index time. Without a provider installed no JS/TS symbol can be in there at
    all, which makes "no results" indistinguishable from "not indexable" — the same
    ambiguity this task exists to remove, one layer up.
    """
    if tree_sitter_available():
        result["degraded"] = False
        return result
    result["degraded"] = True
    result["degraded_detail"] = {
        "mode": "degraded",
        "reason": DEGRADED_NO_PROVIDER,
        "detail": _DEGRADED_REMEDIES[DEGRADED_NO_PROVIDER],
        "scope": "js_ts_symbols_absent_from_index",
    }
    result["warning"] = (
        "Symbol index is degraded (non-AST): no tree-sitter provider is installed, so "
        "JS/TS symbols are absent from the index entirely. Python results are unaffected; "
        "a missing JS/TS symbol here means unknown, not absent. "
        f"Fix: {_DEGRADED_REMEDIES[DEGRADED_NO_PROVIDER]}."
    )
    return result


def fs_search_symbols(query: str, semantic: bool = False) -> dict:
    """Search for matching code symbols across the workspace database by name or semantic intent.

    If `semantic` is True, searches using generated embeddings and cosine similarity.
    When this workspace has no vector row the current model can score, including
    after `UA_EMBED_MODEL` changes and leaves older rows behind, the search falls
    back to a symbol-name substring match.
    """
    workspace_root = str(resolve_in_workspace("."))
    conn = connect()
    try:
        if semantic:
            try:
                from ..embeddings import embedding_to_bytes, generate_embedding

                query_emb = generate_embedding(query)
                query_emb_bytes = embedding_to_bytes(query_emb)
                model_sql, model_params = compatible_embedding_model_clause("e.model")
                # A model change leaves stored vectors in place and excludes
                # them from this join. An empty semantic result then used to
                # return immediately and skip the name search. Fall back when
                # this workspace has no vector row the current model can score.
                compatible = conn.execute(
                    "SELECT 1 FROM workspace_symbols s "
                    "JOIN workspace_symbol_embeddings e ON s.id = e.symbol_id "
                    f"WHERE s.workspace = ? AND {model_sql} LIMIT 1",
                    (workspace_root, *model_params),
                ).fetchone()
                if compatible is not None:
                    rows = conn.execute(
                        "SELECT s.file_path, s.symbol_name, s.symbol_type, s.parent_name, s.signature, s.docstring, s.line_no, "
                        "cosine_similarity(e.embedding, ?) AS similarity "
                        "FROM workspace_symbols s "
                        "JOIN workspace_symbol_embeddings e ON s.id = e.symbol_id "
                        f"WHERE s.workspace = ? AND {model_sql} "
                        "ORDER BY similarity DESC LIMIT 100",
                        (query_emb_bytes, workspace_root, *model_params),
                    ).fetchall()

                    results = []
                    for r in rows:
                        results.append(
                            {
                                "file_path": r["file_path"],
                                "name": r["symbol_name"],
                                "type": r["symbol_type"],
                                "parent": r["parent_name"],
                                "signature": r["signature"],
                                "docstring": r["docstring"],
                                "line_no": r["line_no"],
                                "similarity": r["similarity"],
                            }
                        )
                    return _with_search_advisory({"results": results})
            except Exception:
                # Fall back to standard search if semantic fails
                pass

        # Standard query
        rows = conn.execute(
            "SELECT file_path, symbol_name, symbol_type, parent_name, signature, docstring, line_no "
            "FROM workspace_symbols "
            "WHERE workspace = ? AND symbol_name LIKE ? "
            "ORDER BY symbol_name LIMIT 100",
            (workspace_root, f"%{query}%"),
        ).fetchall()

        results = []
        for r in rows:
            results.append(
                {
                    "file_path": r["file_path"],
                    "name": r["symbol_name"],
                    "type": r["symbol_type"],
                    "parent": r["parent_name"],
                    "signature": r["signature"],
                    "docstring": r["docstring"],
                    "line_no": r["line_no"],
                }
            )
        return _with_search_advisory({"results": results})
    finally:
        conn.close()


def fs_list_symbols(file_path: str) -> dict:
    """Retrieve the class and function symbol structure of a specific file in the workspace."""
    workspace_root = str(resolve_in_workspace("."))
    resolved_path = resolve_in_workspace(file_path)
    if not resolved_path.exists():
        raise ToolError(f"File {file_path!r} does not exist.")

    try:
        rel_path = resolved_path.relative_to(workspace_root).as_posix()
    except ValueError:
        raise ToolError(f"File {file_path!r} is outside the workspace.") from None

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT symbol_name, symbol_type, parent_name, signature, docstring, line_no "
            "FROM workspace_symbols "
            "WHERE workspace = ? AND file_path = ? "
            "ORDER BY line_no",
            (workspace_root, rel_path),
        ).fetchall()
    finally:
        conn.close()

    degraded_reason: str | None = None
    if not rows and resolved_path.is_file():
        # Parse on the fly if not indexed yet. Route on extension: sending a .ts file
        # through the Python ast parser returns [] for every file, which read as
        # "no symbols" rather than "wrong parser".
        try:
            content = resolved_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise ToolError(f"Failed to read file {file_path!r}: {e}") from e
        if resolved_path.suffix.lower() in JS_TS_EXTENSIONS:
            symbols_found, degraded_reason = parse_js_ts_symbols_with_status(
                content, resolved_path.name
            )
        else:
            symbols_found = parse_file_symbols(content)
    else:
        symbols_found = [
            {
                "name": r["symbol_name"],
                "type": r["symbol_type"],
                "parent": r["parent_name"],
                "signature": r["signature"],
                "docstring": r["docstring"],
                "line_no": r["line_no"],
            }
            for r in rows
        ]

    result = {"symbols": symbols_found, "degraded": bool(degraded_reason)}
    if degraded_reason:
        advisory = _degraded_advisory({degraded_reason: 1}, 1)
        advisory["sample_files"] = [rel_path]
        result["degraded_detail"] = advisory
        result["warning"] = advisory["warning"]
    return result
