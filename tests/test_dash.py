import plistlib
import sqlite3
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.dash import dash_list, dash_search, dash_view


@pytest.fixture
def mock_dash_env(tmp_path, monkeypatch):
    docsets_dir = tmp_path / "docsets"
    docsets_dir.mkdir()

    # Create a mock docset
    docset_path = docsets_dir / "TestDocset.docset"
    docset_path.mkdir(parents=True)

    # Info.plist
    contents_dir = docset_path / "Contents"
    contents_dir.mkdir()
    info_plist = contents_dir / "Info.plist"
    plist_data = {
        "CFBundleName": "Test Docset",
        "CFBundleIdentifier": "test-id",
        "DocSetPlatformFamily": "test-platform",
    }
    info_plist.write_bytes(plistlib.dumps(plist_data))

    # SQLite docSet.dsidx
    resources_dir = contents_dir / "Resources"
    resources_dir.mkdir()
    db_path = resources_dir / "docSet.dsidx"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT)"
    )
    cursor.executemany(
        "INSERT INTO searchIndex (name, type, path) VALUES (?, ?, ?)",
        [
            ("my_func", "Function", "api/my_func.html"),
            ("my_class", "Class", "api/my_class.html"),
            ("func_extra", "Function", "api/extra.html#func_extra"),
        ],
    )
    conn.commit()
    conn.close()

    # Documents
    docs_dir = resources_dir / "Documents"
    docs_dir.mkdir()

    api_dir = docs_dir / "api"
    api_dir.mkdir()

    (api_dir / "my_func.html").write_text(
        "<html><body><h1>my_func documentation</h1><p>Details about my_func.</p></body></html>",
        encoding="utf-8",
    )
    (api_dir / "my_class.html").write_text(
        "<html><body><h1>my_class docs</h1><p>Details about class.</p></body></html>",
        encoding="utf-8",
    )
    (api_dir / "extra.html").write_text(
        "<html><body><h1>extra</h1></body></html>", encoding="utf-8"
    )

    monkeypatch.setenv("UA_DASH_DOCSETS", str(docsets_dir))
    monkeypatch.setattr("unified_agent.tools.dash.config.dash_docset_paths", lambda: [docsets_dir])
    return docset_path


def test_dash_list(mock_dash_env):
    out = dash_list()
    assert "docsets" in out
    assert len(out["docsets"]) == 1
    doc = out["docsets"][0]
    assert doc["name"] == "Test Docset"
    assert doc["id"] == "test-id"
    assert doc["platform"] == "test-platform"
    assert Path(doc["path"]) == mock_dash_env


def test_dash_search(mock_dash_env):
    # Match exact
    out = dash_search("my_func")
    assert len(out["results"]) == 1
    assert out["results"][0]["name"] == "my_func"
    assert out["results"][0]["docset_id"] == "test-id"

    # Match multiple
    out = dash_search("my")
    assert len(out["results"]) == 2
    # "my_func" and "my_class" both match. We check scoring/ranking.
    names = [r["name"] for r in out["results"]]
    assert "my_func" in names
    assert "my_class" in names

    # Filter by docset
    out = dash_search("my_func", docset="test-id")
    assert len(out["results"]) == 1

    # Filter by non-existent docset
    with pytest.raises(ToolError, match="No docset matching"):
        dash_search("my_func", docset="nonexistent")

    # Empty query
    with pytest.raises(ToolError, match="Query must not be empty"):
        dash_search("  ")


def test_dash_view(mock_dash_env):
    out = dash_view("test-id", "api/my_func.html")
    assert out["docset_name"] == "Test Docset"
    # trafilatura >=2.3 emits valid Markdown, escaping underscores in prose
    # (my\\_func) while leaving code verbatim; compare the rendered text.
    rendered = out["content"].replace("\\_", "_")
    assert "my_func documentation" in rendered
    assert "Details about my_func." in rendered

    # Match using name instead of ID
    out = dash_view("Test Docset", "api/my_class.html")
    assert out["docset_name"] == "Test Docset"

    # Test with query params/anchors in path
    out = dash_view("test-id", "api/extra.html#func_extra")
    assert "extra" in out["content"]

    # Test with angle-bracket metadata tags in path
    out = dash_view("test-id", "<dash_entry_tag=1>api/extra.html#func_extra")
    assert "extra" in out["content"]

    # Traversal escape check
    with pytest.raises(ToolError, match="escapes the docset documents root"):
        dash_view("test-id", "../../../Info.plist")

    # Docset not found
    with pytest.raises(ToolError, match="not found"):
        dash_view("nonexistent", "api/my_func.html")

    # Page not found
    with pytest.raises(ToolError, match="not found"):
        dash_view("test-id", "api/missing.html")


def test_dash_docset_paths_config(tmp_path, monkeypatch):
    """Test config.dash_docset_paths with custom UA_DASH_DOCSETS and default paths."""
    from unified_agent import config

    d1 = tmp_path / "custom1"
    d2 = tmp_path / "custom2"
    d1.mkdir()
    d2.mkdir()

    # Custom paths in env
    monkeypatch.setenv("UA_DASH_DOCSETS", f"{d1}:{d2}:{tmp_path / 'nonexistent'}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    paths = config.dash_docset_paths()
    assert d1.resolve() in paths
    assert d2.resolve() in paths
    assert (tmp_path / "nonexistent").resolve() not in paths


def test_discover_docsets_resilience(tmp_path, monkeypatch):
    """Test discover_docsets handling of invalid plists and missing indices."""
    docsets_dir = tmp_path / "docsets"
    docsets_dir.mkdir()

    # Docset with missing docSet.dsidx
    ds1 = docsets_dir / "Broken.docset"
    ds1.mkdir()
    (ds1 / "Contents").mkdir()
    (ds1 / "Contents" / "Info.plist").write_bytes(b"garbage-plist-data")

    # Valid docset next to it
    ds2 = docsets_dir / "Valid.docset"
    (ds2 / "Contents" / "Resources").mkdir(parents=True)
    (ds2 / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleName": "Valid", "CFBundleIdentifier": "valid-id"})
    )
    conn = sqlite3.connect(ds2 / "Contents" / "Resources" / "docSet.dsidx")
    conn.execute(
        "CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT)"
    )
    conn.close()

    monkeypatch.setattr("unified_agent.tools.dash.config.dash_docset_paths", lambda: [docsets_dir])
    found = dash_list()
    assert len(found["docsets"]) == 1
    assert found["docsets"][0]["name"] == "Valid"


def test_live_docsets_installed_and_searchable():
    """Verify live docsets in ~/.local/share/Zeal/Zeal/docsets are discoverable and searchable."""
    from pathlib import Path

    zeal_dir = Path.home() / ".local/share/Zeal/Zeal/docsets"
    if not (zeal_dir / "Python_3.docset").exists():
        pytest.skip("Offline docsets not yet installed on host")

    # 1. dash_list includes Python 3, Git, SQLite
    listing = dash_list()
    names = {d["name"] for d in listing["docsets"]}
    assert "Python 3" in names or "Python" in names
    assert "Git" in names
    assert "SQLite" in names

    # 2. dash_search('asyncio') returns matches in Python 3
    py_matches = dash_search("asyncio")
    assert any(r["docset_name"] in ("Python 3", "Python") for r in py_matches["results"])

    # 3. dash_search('commit') and dash_search('rebase') return matches in Git
    git_commit_matches = dash_search("commit", docset="git")
    assert len(git_commit_matches["results"]) > 0
    git_rebase_matches = dash_search("rebase", docset="git")
    assert len(git_rebase_matches["results"]) > 0

    # 4. dash_search('SELECT') and dash_search('CREATE') return matches in SQLite
    sql_select_matches = dash_search("SELECT", docset="sqlite")
    assert len(sql_select_matches["results"]) > 0
    sql_create_matches = dash_search("CREATE", docset="sqlite")
    assert len(sql_create_matches["results"]) > 0

    # 5. dash_view returns valid markdown content
    view_py = dash_view("python_3", py_matches["results"][0]["path"])
    assert len(view_py["content"]) > 0
    view_git = dash_view("git", git_commit_matches["results"][0]["path"])
    assert len(view_git["content"]) > 0
    view_sql = dash_view("sqlite", sql_select_matches["results"][0]["path"])
    assert len(view_sql["content"]) > 0


def test_dash_search_and_view_section_entries():
    """Verify dash_search and dash_view successfully find and view section entries."""
    from pathlib import Path

    zeal_dir = Path.home() / ".local/share/Zeal/Zeal/docsets"
    if not (zeal_dir / "Python_3.docset").exists():
        pytest.skip("Offline docsets not yet installed on host")

    results = dash_search("DocTestRunner", docset="python_3")
    assert len(results["results"]) > 0
    section_entry = next(
        (r for r in results["results"] if "DocTestRunner" in r["name"]),
        None,
    )
    assert section_entry is not None, "DocTestRunner section entry not found in searchIndex"
    assert not section_entry["path"].startswith("<"), (
        f"Path still contains metadata prefix: {section_entry['path']}"
    )
    assert section_entry["type"] == "Section"

    view_out = dash_view("python_3", section_entry["path"])
    assert view_out["docset_name"] == "Python 3"
    assert len(view_out["content"]) > 0
    assert "doctest" in view_out["content"].lower()

    # Also verify that dash_view handles legacy angle-bracket metadata tags gracefully
    legacy_path = f"<dash_entry_tag=1>{section_entry['path']}"
    legacy_view = dash_view("python_3", legacy_path)
    assert len(legacy_view["content"]) > 0


def _git_htmldocs_fixture() -> bytes:
    """Local stand-in for the kernel.org git-htmldocs tarball.

    Enough ``git-*.html`` pages for the installer to index more than 100 symbols
    without fetching a mirror.
    """
    import io
    import tarfile

    buf = io.BytesIO()
    payload = b"<html><body><h1>git</h1></body></html>"
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for index in range(40):
            name = f"git-cmd{index}.html"
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_installer_git_docset_assembly(tmp_path, monkeypatch):
    """Test installer script assembly of standard Git docset bundle."""
    import importlib.util

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "install_core_docsets.py"
    spec = importlib.util.spec_from_file_location("install_core_docsets", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    install_git_docset = mod.install_git_docset

    fixture = _git_htmldocs_fixture()
    fetched: list[str] = []

    class _FixtureResponse:
        def read(self) -> bytes:
            return fixture

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

    def _urlopen(req, timeout=30):
        fetched.append(getattr(req, "full_url", str(req)))
        return _FixtureResponse()

    # Skip any host-local git-doc tree so assembly always consumes the fixture.
    monkeypatch.setattr(mod, "GIT_DOCS_LOCAL_DIRS", [])
    monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)

    dest = install_git_docset(tmp_path, force=True)
    assert fetched, "installer did not request the docset tarball"
    assert all(url.startswith("https://") for url in fetched)
    assert dest.exists()
    assert (dest / "Contents" / "Info.plist").is_file()

    # Verify plist
    with open(dest / "Contents" / "Info.plist", "rb") as f:
        plist = plistlib.load(f)
    assert plist["CFBundleName"] == "Git"
    assert plist["CFBundleIdentifier"] == "git"
    assert plist["DocSetPlatformFamily"] == "git"

    # Verify SQLite index
    db_path = dest / "Contents" / "Resources" / "docSet.dsidx"
    assert db_path.is_file()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM searchIndex")
    count = cur.fetchone()[0]
    assert count > 100

    cur.execute("SELECT type, path FROM searchIndex WHERE name = 'commit'")
    rows = cur.fetchall()
    assert len(rows) > 0

    cur.execute("SELECT type, path FROM searchIndex WHERE name = 'rebase'")
    rows = cur.fetchall()
    assert len(rows) > 0
    conn.close()

    # Verify Documents folder
    docs_dir = dest / "Contents" / "Resources" / "Documents"
    assert docs_dir.is_dir()
    assert any(docs_dir.glob("*.html"))
