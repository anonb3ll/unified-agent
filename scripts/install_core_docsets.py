#!/usr/bin/env python3
"""Automated installer for core offline Dash / Zeal docsets.

Installs standard offline docsets for:
  - Python 3 (Python_3.docset)
  - SQLite (SQLite.docset)
  - Git (Git.docset)

Each docset adheres to the standard Dash/Zeal specification:
  - Contents/Info.plist with CFBundleName, CFBundleIdentifier, DocSetPlatformFamily
  - Contents/Resources/docSet.dsidx SQLite 3 database with searchIndex(id, name, type, path)
  - Contents/Resources/Documents/ containing documentation HTML pages
"""

from __future__ import annotations

import argparse
import html
import io
import plistlib
import re
import shutil
import sqlite3
import tarfile
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_TARGET_DIR = Path.home() / ".local/share/Zeal/Zeal/docsets"

PYTHON_URLS = [
    "https://sanfrancisco.kapeli.com/feeds/Python.tgz",
    "https://frankfurt.kapeli.com/feeds/Python.tgz",
    "https://newyork.kapeli.com/feeds/Python.tgz",
    "https://london.kapeli.com/feeds/Python.tgz",
    "https://go.zealdocs.org/d/com.kapeli/Python_3/latest",
]

SQLITE_URLS = [
    "https://sanfrancisco.kapeli.com/feeds/SQLite.tgz",
    "https://frankfurt.kapeli.com/feeds/SQLite.tgz",
    "https://newyork.kapeli.com/feeds/SQLite.tgz",
    "https://london.kapeli.com/feeds/SQLite.tgz",
    "https://go.zealdocs.org/d/com.kapeli/SQLite/latest",
]

GIT_DOCS_LOCAL_DIRS = [
    Path("/usr/share/doc/git-doc"),
    Path("/usr/share/doc/git/html"),
]

GIT_DOCS_URLS = [
    "https://mirrors.edge.kernel.org/pub/software/scm/git/git-htmldocs-2.48.1.tar.xz",
    "https://www.kernel.org/pub/software/scm/git/git-htmldocs-2.48.1.tar.gz",
]


def download_stream(urls: list[str], timeout: int = 30) -> bytes:
    """Download bytes from the first working mirror in the URLs list."""
    last_err: Exception | None = None
    for url in urls:
        try:
            print(f"Connecting to {url} ...")
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Zeal/Dash Docset Installer)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                print(f"Downloaded {len(data)} bytes from {url}")
                return data
        except Exception as e:
            print(f"Failed to fetch from {url}: {e}")
            last_err = e
    raise RuntimeError(f"All download mirrors failed. Last error: {last_err}")


def install_python_docset(target_dir: Path, force: bool = False) -> Path:
    """Acquire, unpack, and index Python 3 docset."""
    dest = target_dir / "Python_3.docset"
    if dest.exists() and not force:
        db_path = dest / "Contents" / "Resources" / "docSet.dsidx"
        if db_path.is_file():
            print(f"Python 3 docset already exists at {dest}. Skipping (use --force to reinstall).")
            return dest

    print("Installing Python 3 docset...")
    data = download_stream(PYTHON_URLS)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        print("Unpacking Python archive...")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            tar.extractall(path=tmp_path, filter="data")

        # Locate extracted docset folder
        extracted_dirs = [
            p for p in tmp_path.iterdir() if p.is_dir() and p.name.endswith(".docset")
        ]
        if not extracted_dirs:
            raise RuntimeError("No .docset directory found in Python archive")
        src_docset = extracted_dirs[0]

        # Update Info.plist
        plist_path = src_docset / "Contents" / "Info.plist"
        plist_data = {}
        if plist_path.is_file():
            try:
                with open(plist_path, "rb") as f:
                    plist_data = plistlib.load(f)
            except Exception:
                pass
        plist_data["CFBundleName"] = "Python 3"
        plist_data["CFBundleIdentifier"] = "python_3"
        plist_data["DocSetPlatformFamily"] = "python"
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)

        # Ensure searchIndex exists and is populated in docSet.dsidx
        db_path = src_docset / "Contents" / "Resources" / "docSet.dsidx"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='searchIndex'")
        if not cur.fetchone():
            print("Populating standard searchIndex table from Apple CoreData tokens...")
            cur.execute(
                "CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT)"
            )
            cur.execute(
                """
                SELECT
                    t.ZTOKENNAME,
                    COALESCE(tt.ZTYPENAME, 'cl'),
                    CASE
                        WHEN m.ZANCHOR IS NOT NULL AND m.ZANCHOR != '' THEN fp.ZPATH || '#' || m.ZANCHOR
                        ELSE fp.ZPATH
                    END
                FROM ZTOKEN t
                LEFT JOIN ZTOKENTYPE tt ON t.ZTOKENTYPE = tt.Z_PK
                LEFT JOIN ZTOKENMETAINFORMATION m ON t.ZMETAINFORMATION = m.Z_PK
                LEFT JOIN ZFILEPATH fp ON m.ZFILE = fp.Z_PK
                """
            )
            raw_rows = cur.fetchall()
            cleaned_rows = [
                (name, sym_type, re.sub(r"<[^>]+>", "", raw_path) if raw_path else "")
                for name, sym_type, raw_path in raw_rows
            ]
            cur.executemany(
                "INSERT INTO searchIndex (name, type, path) VALUES (?, ?, ?)",
                cleaned_rows,
            )
            cur.execute("CREATE INDEX IF NOT EXISTS ix_searchIndex_name ON searchIndex(name)")
            conn.commit()
        else:
            cur.execute("SELECT id, path FROM searchIndex WHERE path LIKE '<%'")
            updates = [
                (re.sub(r"<[^>]+>", "", p) if p else "", row_id) for row_id, p in cur.fetchall()
            ]
            if updates:
                cur.executemany("UPDATE searchIndex SET path = ? WHERE id = ?", updates)
            cur.execute("CREATE INDEX IF NOT EXISTS ix_searchIndex_name ON searchIndex(name)")
            conn.commit()

        cur.execute("SELECT count(*) FROM searchIndex")
        count = cur.fetchone()[0]
        conn.close()
        print(f"Python 3 searchIndex ready with {count} symbols.")

        # Atomic move into destination
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src_docset), str(dest))

    print(f"Installed Python 3 docset to {dest}")
    return dest


def install_sqlite_docset(target_dir: Path, force: bool = False) -> Path:
    """Acquire and unpack SQLite docset."""
    dest = target_dir / "SQLite.docset"
    if dest.exists() and not force:
        db_path = dest / "Contents" / "Resources" / "docSet.dsidx"
        if db_path.is_file():
            print(f"SQLite docset already exists at {dest}. Skipping (use --force to reinstall).")
            return dest

    print("Installing SQLite docset...")
    data = download_stream(SQLITE_URLS)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        print("Unpacking SQLite archive...")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            tar.extractall(path=tmp_path, filter="data")

        extracted_dirs = [
            p for p in tmp_path.iterdir() if p.is_dir() and p.name.endswith(".docset")
        ]
        if not extracted_dirs:
            raise RuntimeError("No .docset directory found in SQLite archive")
        src_docset = extracted_dirs[0]

        # Verify Info.plist
        plist_path = src_docset / "Contents" / "Info.plist"
        plist_data = {}
        if plist_path.is_file():
            try:
                with open(plist_path, "rb") as f:
                    plist_data = plistlib.load(f)
            except Exception:
                pass
        plist_data["CFBundleName"] = "SQLite"
        plist_data["CFBundleIdentifier"] = "sqlite"
        plist_data["DocSetPlatformFamily"] = "sqlite"
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)

        # Verify searchIndex
        db_path = src_docset / "Contents" / "Resources" / "docSet.dsidx"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE INDEX IF NOT EXISTS ix_searchIndex_name ON searchIndex(name)")
        conn.commit()
        cur.execute("SELECT count(*) FROM searchIndex")
        count = cur.fetchone()[0]
        conn.close()
        print(f"SQLite searchIndex ready with {count} symbols.")

        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src_docset), str(dest))

    print(f"Installed SQLite docset to {dest}")
    return dest


def install_git_docset(target_dir: Path, force: bool = False) -> Path:
    """Acquire, structure, and index Git docset."""
    dest = target_dir / "Git.docset"
    if dest.exists() and not force:
        db_path = dest / "Contents" / "Resources" / "docSet.dsidx"
        if db_path.is_file():
            print(f"Git docset already exists at {dest}. Skipping (use --force to reinstall).")
            return dest

    print("Installing Git docset...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        docset_dir = tmp_path / "Git.docset"
        contents_dir = docset_dir / "Contents"
        resources_dir = contents_dir / "Resources"
        docs_dir = resources_dir / "Documents"
        docs_dir.mkdir(parents=True, exist_ok=True)

        # 1. Acquire HTML documentation files
        found_local = False
        for local_dir in GIT_DOCS_LOCAL_DIRS:
            if local_dir.is_dir() and any(local_dir.glob("*.html")):
                print(f"Using local Git documentation from {local_dir}...")
                for item in local_dir.iterdir():
                    if item.is_file():
                        shutil.copy2(item, docs_dir / item.name)
                    elif item.is_dir():
                        shutil.copytree(item, docs_dir / item.name, dirs_exist_ok=True)
                found_local = True
                break

        if not found_local:
            print(
                "Local Git documentation not found; downloading official kernel.org git-htmldocs..."
            )
            data = download_stream(GIT_DOCS_URLS)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
                tar.extractall(path=docs_dir, filter="data")

        # 2. Write Info.plist
        plist_path = contents_dir / "Info.plist"
        plist_data = {
            "CFBundleName": "Git",
            "CFBundleIdentifier": "git",
            "DocSetPlatformFamily": "git",
            "dashIndexFilePath": "git.html",
            "isDashDocset": True,
        }
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)

        # 3. Create and populate docSet.dsidx
        db_path = resources_dir / "docSet.dsidx"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT)"
        )
        cur.execute("CREATE INDEX ix_searchIndex_name ON searchIndex(name)")

        symbols: list[tuple[str, str, str]] = []
        seen_keys: set[tuple[str, str, str]] = set()

        def add_symbol(name: str, sym_type: str, path: str):
            clean_name = name.strip()
            if not clean_name:
                return
            key = (clean_name.lower(), sym_type, path)
            if key in seen_keys:
                return
            seen_keys.add(key)
            symbols.append((clean_name, sym_type, path))

        # Index commands and guides from HTML files
        html_files = sorted(docs_dir.glob("*.html"))
        for hfile in html_files:
            stem = hfile.stem
            rel_path = hfile.name
            if stem.startswith("git-"):
                cmd = stem[4:]
                add_symbol(f"git {cmd}", "Command", rel_path)
                add_symbol(cmd, "Command", rel_path)
                add_symbol(stem, "Command", rel_path)
            elif stem.startswith("git"):
                add_symbol(stem, "Guide", rel_path)
            else:
                add_symbol(stem, "Guide", rel_path)

            # Extract command-line flags and options
            try:
                text = hfile.read_text(encoding="utf-8", errors="ignore")
                flags = re.findall(r'<dt class="hdlist1">\s*<code>(-{1,2}[a-zA-Z0-9_-]+)', text)
                for flag in set(flags):
                    add_symbol(f"{stem} {flag}", "Option", rel_path)
                    add_symbol(flag, "Option", rel_path)
            except Exception:
                pass

        # Index glossary definitions
        glossary_file = docs_dir / "gitglossary.html"
        if glossary_file.is_file():
            try:
                text = glossary_file.read_text(encoding="utf-8", errors="ignore")
                terms = re.findall(
                    r'<dt class="hdlist1">\s*(?:<a id="([^"]+)"></a>\s*)?([^<\n]+)', text
                )
                for anchor, term in terms:
                    clean_term = html.unescape(term).strip()
                    if clean_term and clean_term not in (
                        "Git internal format",
                        "RFC 2822",
                        "ISO 8601",
                    ):
                        term_path = f"gitglossary.html#{anchor}" if anchor else "gitglossary.html"
                        add_symbol(clean_term, "Concept", term_path)
            except Exception:
                pass

        # Standard core Git concepts and workflows
        core_concepts = [
            ("commit", "Command", "git-commit.html"),
            ("rebase", "Command", "git-rebase.html"),
            ("checkout", "Command", "git-checkout.html"),
            ("branch", "Command", "git-branch.html"),
            ("merge", "Command", "git-merge.html"),
            ("push", "Command", "git-push.html"),
            ("pull", "Command", "git-pull.html"),
            ("clone", "Command", "git-clone.html"),
            ("status", "Command", "git-status.html"),
            ("log", "Command", "git-log.html"),
            ("diff", "Command", "git-diff.html"),
            ("reset", "Command", "git-reset.html"),
            ("stash", "Command", "git-stash.html"),
            ("tag", "Command", "git-tag.html"),
            ("worktree", "Command", "git-worktree.html"),
            ("cherry-pick", "Command", "git-cherry-pick.html"),
            ("blame", "Command", "git-blame.html"),
            ("bisect", "Command", "git-bisect.html"),
            ("submodule", "Command", "git-submodule.html"),
        ]
        for name, sym_type, path in core_concepts:
            add_symbol(name, sym_type, path)

        cur.executemany(
            "INSERT INTO searchIndex (name, type, path) VALUES (?, ?, ?)",
            symbols,
        )
        conn.commit()
        cur.execute("SELECT count(*) FROM searchIndex")
        count = cur.fetchone()[0]
        conn.close()
        print(f"Git searchIndex ready with {count} symbols.")

        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(docset_dir), str(dest))

    print(f"Installed Git docset to {dest}")
    return dest


def install_all(
    target_dir: Path = DEFAULT_TARGET_DIR, force: bool = False, docsets: list[str] | None = None
) -> list[Path]:
    """Install selected docsets into target directory."""
    target_dir.mkdir(parents=True, exist_ok=True)
    targets = [d.lower().strip() for d in (docsets or ["python3", "sqlite", "git"])]
    installed: list[Path] = []

    if any(t in targets for t in ("python", "python3", "python_3")):
        installed.append(install_python_docset(target_dir, force=force))

    if any(t in targets for t in ("sqlite", "sqlite3")):
        installed.append(install_sqlite_docset(target_dir, force=force))

    if any(t in targets for t in ("git",)):
        installed.append(install_git_docset(target_dir, force=force))

    return installed


def main():
    parser = argparse.ArgumentParser(description="Install core offline Dash/Zeal docsets.")
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=DEFAULT_TARGET_DIR,
        help=f"Target docset installation directory (default: {DEFAULT_TARGET_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force reinstallation and overwrite existing docset bundles",
    )
    parser.add_argument(
        "--docsets",
        type=str,
        default="python3,sqlite,git",
        help="Comma-separated list of docsets to install (default: python3,sqlite,git)",
    )
    args = parser.parse_args()

    selected = [s.strip() for s in args.docsets.split(",") if s.strip()]
    installed = install_all(target_dir=args.target_dir, force=args.force, docsets=selected)
    print(f"\nSuccessfully installed {len(installed)} docsets:")
    for path in installed:
        print(f"  - {path}")


if __name__ == "__main__":
    main()
