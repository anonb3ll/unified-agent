"""Dash and Zeal offline documentation search and view tools."""

import plistlib
import re
import sqlite3
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import config


def discover_docsets() -> list[dict]:
    """Scan search paths for directories ending in .docset and parse their metadata."""
    docsets = []
    seen_paths = set()
    for parent in config.dash_docset_paths():
        if not parent.is_dir():
            continue
        try:
            for item in parent.iterdir():
                if item.is_dir() and item.name.lower().endswith(".docset"):
                    resolved = item.resolve()
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)

                    name = item.stem
                    doc_id = item.stem.lower()
                    platform = ""

                    plist_path = resolved / "Contents" / "Info.plist"
                    if plist_path.is_file():
                        try:
                            with open(plist_path, "rb") as f:
                                plist = plistlib.load(f)
                                name = plist.get("CFBundleName", name)
                                doc_id = plist.get("CFBundleIdentifier", doc_id)
                                platform = plist.get("DocSetPlatformFamily", platform)
                        except Exception:
                            pass

                    db_file = resolved / "Contents" / "Resources" / "docSet.dsidx"
                    if db_file.is_file():
                        docsets.append(
                            {
                                "name": name,
                                "id": doc_id,
                                "path": str(resolved),
                                "platform": platform,
                            }
                        )
        except Exception:
            pass
    return docsets


def dash_list() -> dict:
    """List all locally available Dash / Zeal docsets.

    Returns a dict containing available docsets under the key 'docsets'.
    """
    return {"docsets": discover_docsets()}


def dash_search(query: str, docset: str = None) -> dict:
    """Search matching documentation symbols / pages in available docsets.

    Fuzzy matches symbol names inside the docset sqlite index. If docset name or id
    is provided, limits the search to that docset.
    """
    if not query.strip():
        raise ToolError("Query must not be empty")

    docsets = discover_docsets()
    if not docsets:
        return {"results": []}

    if docset:
        target = docset.strip().lower()
        docsets = [
            d
            for d in docsets
            if d["id"].lower() == target
            or d["name"].lower() == target
            or d["id"].lower().startswith(target)
        ]
        if not docsets:
            raise ToolError(f"No docset matching '{docset}' found.")

    results = []
    for d in docsets:
        db_path = Path(d["path"]) / "Contents" / "Resources" / "docSet.dsidx"
        if not db_path.is_file():
            continue

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            # Retrieve rows from the docset index
            cursor.execute(
                "SELECT name, type, path FROM searchIndex WHERE name LIKE ? LIMIT 200",
                (f"%{query}%",),
            )
            rows = cursor.fetchall()
            conn.close()

            for name, item_type, rel_path in rows:
                name_lower = name.lower()
                query_lower = query.lower()

                if name_lower == query_lower:
                    score = 0
                elif name_lower.startswith(query_lower):
                    score = 1
                else:
                    score = 2

                results.append(
                    {
                        "docset_name": d["name"],
                        "docset_id": d["id"],
                        "name": name,
                        "type": item_type,
                        "path": rel_path,
                        "_score": score,
                    }
                )
        except Exception:
            pass

    # Sort results by match score, name length, and alphabetical name
    results.sort(key=lambda r: (r["_score"], len(r["name"]), r["name"].lower()))

    # Clean internal score key
    for r in results:
        del r["_score"]

    return {"results": results[:50]}


def dash_view(docset_id: str, path: str) -> dict:
    """Retrieve and render the content of a specific docset documentation page.

    Matches docset_id against available docset ids or names. The path matches the
    relative path returned from dash_search. Returns Markdown content.
    """
    if not docset_id.strip():
        raise ToolError("docset_id must not be empty")
    if not path.strip():
        raise ToolError("path must not be empty")

    docsets = discover_docsets()
    target_docset = None
    for d in docsets:
        if (
            d["id"].lower() == docset_id.strip().lower()
            or d["name"].lower() == docset_id.strip().lower()
        ):
            target_docset = d
            break

    if not target_docset:
        raise ToolError(f"Docset with id/name '{docset_id}' not found.")

    docset_root = Path(target_docset["path"]).resolve()
    clean_path = path.split("#")[0].split("?")[0]
    clean_path = re.sub(r"<[^>]+>", "", clean_path)
    docs_root = docset_root / "Contents" / "Resources" / "Documents"
    candidate = docs_root / clean_path
    resolved = candidate.resolve()

    if not (resolved == docs_root or resolved.is_relative_to(docs_root)):
        raise ToolError(f"Path {path!r} escapes the docset documents root {docs_root}")

    if not resolved.is_file():
        raise ToolError(f"Documentation page not found: {clean_path}")

    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            html_content = f.read()
    except Exception as e:
        raise ToolError(f"Failed to read documentation page: {e}") from e

    import trafilatura

    try:
        content = trafilatura.extract(html_content, output_format="markdown", include_links=False)
    except Exception:
        content = None

    if not content:
        content = html_content

    cap = 60000
    truncated = len(content) > cap

    return {
        "docset_name": target_docset["name"],
        "path": path,
        "content": content[:cap],
        "truncated": truncated,
    }
