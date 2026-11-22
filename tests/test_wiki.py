import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from unified_agent.wiki import parse_markdown_file, run_custodian


@pytest.mark.parametrize("populated", [False, True])
def test_custodian_dry_run_does_not_create_missing_directories(tmp_path, monkeypatch, populated):
    vault = tmp_path / "vault"
    vault.mkdir()
    if populated:
        (vault / "Existing.md").write_text("# Existing note\n")
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    monkeypatch.setenv("UA_HOSTED_WORKSPACE_GUARD", "0")
    before = sorted(path.relative_to(vault) for path in vault.rglob("*"))

    result = run_custodian(dry_run=True)

    assert result["index_rebuilt"] is False
    assert sorted(path.relative_to(vault) for path in vault.rglob("*")) == before
    assert not (vault / "Wiki").exists()
    assert not (vault / "00 - Inbox").exists()


def test_parse_markdown_file(tmp_path):
    note_path = tmp_path / "test-note.md"
    note_path.write_text(
        "---\n"
        'title: "Test Note Title"\n'
        "tags: [project/test, dev]\n"
        "---\n"
        "# Overridden Title (H1)\n"
        "This is a body with a [[WikiLinkTarget]] and some tag #inline-tag.\n"
    )

    data = parse_markdown_file(note_path)

    assert data["name"] == "test-note"
    assert data["title"] == "Test Note Title"
    assert "project/test" in data["tags"]
    assert "dev" in data["tags"]
    assert "inline-tag" in data["tags"]
    assert "WikiLinkTarget" in data["wikilinks"]


def test_parse_markdown_file_aliases_inline_list(tmp_path):
    note_path = tmp_path / "inline-aliases.md"
    note_path.write_text("---\ntitle: Inline Aliases\naliases: [Foo, Bar Baz]\n---\nContent.\n")

    data = parse_markdown_file(note_path)

    assert set(data["aliases"]) == {"Foo", "Bar Baz"}


def test_parse_markdown_file_aliases_single_scalar(tmp_path):
    note_path = tmp_path / "scalar-alias.md"
    note_path.write_text('---\ntitle: Scalar Alias\naliases: "Solo Alias"\n---\nContent.\n')

    data = parse_markdown_file(note_path)

    assert data["aliases"] == ["Solo Alias"]


def test_parse_markdown_file_aliases_block_list(tmp_path):
    note_path = tmp_path / "block-aliases.md"
    note_path.write_text(
        "---\ntitle: Block Aliases\naliases:\n  - Alpha Alias\n  - Beta Alias\n---\nContent.\n"
    )

    data = parse_markdown_file(note_path)

    assert set(data["aliases"]) == {"Alpha Alias", "Beta Alias"}
    # Aliases preserve original case (unlike the case-insensitive comparison
    # run_custodian does at resolution time).
    assert "alpha alias" not in data["aliases"]


def test_parse_markdown_file_body_chars(tmp_path):
    note_path = tmp_path / "short.md"
    note_path.write_text("---\ntitle: Short\n---\nTiny.")

    data = parse_markdown_file(note_path)

    assert data["body_chars"] == len("Tiny.")


def test_run_custodian(tmp_path):
    # Setup test vault structure
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    inbox_dir = vault_dir / "00 - Inbox"

    wiki_dir.mkdir(parents=True)
    inbox_dir.mkdir(parents=True)

    # 1. Create linked notes
    (wiki_dir / "A.md").write_text("---\ntitle: Note A\ntags: [tag1]\n---\nLinks to [[B]].")
    (wiki_dir / "B.md").write_text("---\ntitle: Note B\ntags: [tag2]\n---\nContent.")
    # 2. Create orphan note
    (wiki_dir / "Orphan.md").write_text("---\ntitle: Orphan Note\ntags: [tag1]\n---\nContent.")
    # 3. Create note with broken link
    (wiki_dir / "Broken.md").write_text("---\ntitle: Broken Link Note\n---\nLinks to [[Missing]].")
    # 4. Create inbox note
    (inbox_dir / "InboxCapture.md").write_text("Raw notes from Operator.")

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        # Dry Run check
        res_dry = run_custodian(dry_run=True)
        assert res_dry["wiki_notes_count"] == 4
        assert res_dry["inbox_notes_count"] == 1
        assert res_dry["index_rebuilt"] is False
        assert any(
            link["source"] == "Broken" and link["target"] == "Missing"
            for link in res_dry["broken_links"]
        )
        # Orphans: A, Orphan, Broken (since B is linked by A)
        assert "A" in res_dry["orphans"]
        assert "Orphan" in res_dry["orphans"]
        assert "Broken" in res_dry["orphans"]
        assert "B" not in res_dry["orphans"]

        # Verify the generated Health report does not exist yet
        assert not (wiki_dir / "Health.md").exists()

        # Real Run rebuild
        res_real = run_custodian(dry_run=False)
        assert res_real["index_rebuilt"] is True

        # Verify Health.md is created (the curated Index.md is left to Steward)
        index_file = wiki_dir / "Health.md"
        assert index_file.exists()
        assert not (wiki_dir / "Index.md").exists()

        content = index_file.read_text(encoding="utf-8")
        assert "# Wiki Health Report" in content
        assert "### #tag1" in content
        assert "### #tag2" in content
        assert "🕸️ Orphan Pages" in content
        assert "[[Orphan]]" in content
        assert "⚠️ Broken Links" in content
        assert "broken link to `[[Missing]]`" in content
        assert "00 - Inbox/InboxCapture" in content


def test_wikilink_anchor_resolves_and_block_tags(tmp_path):
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    wiki_dir.mkdir(parents=True)
    (vault_dir / "00 - Inbox").mkdir()

    # Block-style YAML tags + a wikilink carrying a #heading anchor.
    (wiki_dir / "Source.md").write_text(
        "---\ntitle: Source\ntags:\n  - alpha\n  - beta\n---\nSee [[Target#Section]]."
    )
    (wiki_dir / "Target.md").write_text("---\ntitle: Target\n---\nContent.")

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    # The anchored link resolves to Target: not broken, and Target is not an orphan.
    assert res["broken_links"] == []
    assert "Target" not in res["orphans"]
    # Block-style tags parsed without an empty-string tag.
    data = parse_markdown_file(wiki_dir / "Source.md")
    assert set(data["tags"]) == {"alpha", "beta"}


def test_alias_resolved_link_clears_orphan(tmp_path):
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    wiki_dir.mkdir(parents=True)
    (vault_dir / "00 - Inbox").mkdir()

    (wiki_dir / "Canonical.md").write_text(
        "---\ntitle: Canonical\naliases: [Canon, Old Name]\n---\n"
        "Content long enough to dodge the stub-page flag for this fixture note.\n"
    )
    (wiki_dir / "Source.md").write_text(
        "---\ntitle: Source\n---\n"
        "See [[old name]] for details — content long enough to dodge the stub flag.\n"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    # The link targets the alias in a different case; it should resolve to
    # Canonical rather than being reported broken or leaving Canonical an orphan.
    assert res["broken_links"] == []
    assert "Canonical" not in res["orphans"]


def test_duplicate_candidate_via_alias(tmp_path):
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    wiki_dir.mkdir(parents=True)
    (vault_dir / "00 - Inbox").mkdir()

    (wiki_dir / "PageOne.md").write_text(
        "---\ntitle: Page One\naliases: [Shared Name]\n---\n"
        "Some longer content so this page is not flagged as a stub for the test.\n"
    )
    (wiki_dir / "PageTwo.md").write_text(
        "---\ntitle: Page Two\naliases: [shared name]\n---\n"
        "Some longer content so this page is not flagged as a stub for the test.\n"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    # Duplicate detection is report-only: it flags the collision but never merges.
    assert len(res["duplicate_candidates"]) == 1
    dup = res["duplicate_candidates"][0]
    assert {dup["a"], dup["b"]} == {"PageOne", "PageTwo"}
    assert dup["via"] == "shared name"


def test_empty_page_flagged(tmp_path):
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    wiki_dir.mkdir(parents=True)
    (vault_dir / "00 - Inbox").mkdir()

    (wiki_dir / "Stub.md").write_text("---\ntitle: Stub\n---\nTiny.")
    (wiki_dir / "Full.md").write_text("---\ntitle: Full\n---\n" + ("Long enough content. " * 10))

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert "Stub" in res["empty_pages"]
    assert "Full" not in res["empty_pages"]


def test_stale_page_flagged(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    wiki_dir.mkdir(parents=True)
    (vault_dir / "00 - Inbox").mkdir()

    old_note = wiki_dir / "OldNote.md"
    old_note.write_text(
        "---\ntitle: Old Note\n---\nContent long enough to dodge the stub-page flag entirely.\n"
    )
    fresh_note = wiki_dir / "FreshNote.md"
    fresh_note.write_text(
        "---\ntitle: Fresh Note\n---\nContent long enough to dodge the stub-page flag entirely.\n"
    )

    # Backdate OldNote's mtime by 120 days; FreshNote keeps its just-written mtime.
    old_time = (datetime.now() - timedelta(days=120)).timestamp()
    os.utime(old_note, (old_time, old_time))

    monkeypatch.setenv("UA_WIKI_STALE_DAYS", "90")

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert "OldNote" in res["stale_pages"]
    assert "FreshNote" not in res["stale_pages"]


def test_health_md_updated_at_is_utc(tmp_path, monkeypatch, request):
    """The Z-suffixed frontmatter stamp is UTC, not local time with a Z."""
    import re
    import time
    from datetime import UTC

    monkeypatch.setenv("TZ", "Etc/GMT-14")  # UTC+14: local and UTC never agree
    time.tzset()
    request.addfinalizer(time.tzset)  # runs after monkeypatch restores TZ
    vault_dir = tmp_path / "vault"
    (vault_dir / "Wiki").mkdir(parents=True)
    (vault_dir / "00 - Inbox").mkdir()

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        run_custodian(dry_run=False)

    content = (vault_dir / "Wiki" / "Health.md").read_text(encoding="utf-8")
    stamp = re.search(r"^updated_at: (\S+)$", content, re.M).group(1)
    written = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - written) < timedelta(minutes=5)


def test_health_md_contains_new_sections(tmp_path):
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    wiki_dir.mkdir(parents=True)
    (vault_dir / "00 - Inbox").mkdir()

    (wiki_dir / "PageOne.md").write_text(
        "---\ntitle: Page One\naliases: [Shared Name]\n---\n"
        "Long enough body content to dodge the stub flag for this fixture note.\n"
    )
    (wiki_dir / "PageTwo.md").write_text(
        "---\ntitle: Page Two\naliases: [shared name]\n---\n"
        "Long enough body content to dodge the stub flag for this fixture note.\n"
    )
    (wiki_dir / "Stub.md").write_text("---\ntitle: Stub\n---\nTiny.")

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        run_custodian(dry_run=False)

    content = (wiki_dir / "Health.md").read_text(encoding="utf-8")
    assert "### 👯 Duplicate Candidates" in content
    assert "### 🏷️ Missing Aliases" in content
    assert "### 📄 Empty / Stub Pages" in content
    assert "### 🕰️ Stale Pages (>90d)" in content
    assert "[[Stub]] — 5 chars" in content
    assert "shared identity: `shared name`" in content


def test_dotted_target_and_table_escaped_alias_resolve(tmp_path):
    """A note whose own name contains a dot is not a broken link.

    Regression: the resolver used `Path(target).stem`, which read the ".1 ..."
    of "Spec - Phase 3.1 Foo" as a file suffix and truncated the target. The
    table-escaped form ([[Target\\|Alias]], Obsidian's required escape inside a
    table cell) was likewise reported as dangling because the capture kept the
    backslash.
    """
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    inbox_dir = vault_dir / "00 - Inbox"
    wiki_dir.mkdir(parents=True)
    inbox_dir.mkdir()

    (inbox_dir / "Spec - Phase 3.1 Foo.md").write_text("Inbox capture body.\n")
    (wiki_dir / "TargetPage.md").write_text(
        "---\ntitle: Target Page\naliases: [target]\n---\n"
        "Long enough body content to dodge the stub flag for this fixture note.\n"
    )
    (wiki_dir / "Source.md").write_text(
        "---\ntitle: Source\naliases: [src]\n---\n"
        "Cites [[00 - Inbox/Spec - Phase 3.1 Foo]] and, in a table:\n\n"
        "| Item | Page |\n|---|---|\n| a | [[TargetPage\\|Target Page]] |\n"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["broken_links"] == []
    assert "TargetPage" not in res["orphans"]


def test_explicit_md_extension_still_strips(tmp_path):
    """[[folder/Note.md]] keeps resolving to "Note" after the suffix change."""
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    wiki_dir.mkdir(parents=True)
    (vault_dir / "00 - Inbox").mkdir()

    (wiki_dir / "Note.md").write_text(
        "---\ntitle: Note\naliases: [note]\n---\n"
        "Long enough body content to dodge the stub flag for this fixture note.\n"
    )
    (wiki_dir / "Linker.md").write_text(
        "---\ntitle: Linker\naliases: [linker]\n---\n"
        "Points at [[Wiki/Note.md]] with an explicit extension.\n"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["broken_links"] == []
    assert "Note" not in res["orphans"]


def test_numeric_references_are_not_tags(tmp_path):
    """ "#122" in prose is a task reference, not a tag (Obsidian's own rule)."""
    note_path = tmp_path / "refs.md"
    note_path.write_text(
        "---\ntitle: Refs\n---\n"
        "Landed via PR #78, closing tasks #104/#105 and #122.\n"
        "Real tags: #phase-4-2 #project/unified-agent #_scratch #v2beta\n"
    )

    tags = parse_markdown_file(note_path)["tags"]

    assert "122" not in tags
    assert "104/105" not in tags
    assert "78" not in tags
    assert "phase-4-2" in tags
    assert "project/unified-agent" in tags
    assert "_scratch" in tags
    assert "v2beta" in tags


def _make_vault(tmp_path):
    vault_dir = tmp_path / "vault"
    (vault_dir / "Wiki").mkdir(parents=True)
    (vault_dir / "00 - Inbox").mkdir()
    return vault_dir


# Body long enough to clear the 80-char stub threshold, so fixtures land in the
# finding lists we are actually asserting on rather than in empty_pages.
_BODY = "Long enough body content to comfortably clear the 80-char stub-page flag in fixtures.\n"


def test_same_named_pages_in_subdirs_are_both_audited(tmp_path):
    """Two pages sharing a filename in different subdirs are two pages.

    Regression: the inventory was keyed by file stem, so the second
    Overview.md overwrote the first and the shadowed page became invisible to
    every downstream check while the report read all-clean.
    """
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "Antigravity").mkdir()
    (wiki_dir / "Claude Code").mkdir()

    (wiki_dir / "Antigravity" / "Overview.md").write_text(
        f"---\ntitle: Antigravity Overview\naliases: [ag overview]\n---\n{_BODY}"
    )
    # No aliases and a stub body: findings that must be attributed to *this*
    # page specifically, which is impossible if the two share one key.
    (wiki_dir / "Claude Code" / "Overview.md").write_text("---\ntitle: CC Overview\n---\nTiny.")

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["wiki_notes_count"] == 2
    # Colliding names are reported by vault-relative path so they are
    # distinguishable; unique names keep their bare stem.
    assert "Wiki/Claude Code/Overview" in res["empty_pages"]
    assert "Wiki/Antigravity/Overview" not in res["empty_pages"]
    assert "Wiki/Claude Code/Overview" in res["missing_aliases"]
    assert "Wiki/Antigravity/Overview" not in res["missing_aliases"]
    assert set(res["orphans"]) == {"Wiki/Antigravity/Overview", "Wiki/Claude Code/Overview"}


def test_ambiguous_page_name_reported(tmp_path):
    """A name resolving to more than one page is a finding, not a coin flip."""
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "A").mkdir()
    (wiki_dir / "B").mkdir()

    (wiki_dir / "A" / "Shared.md").write_text(f"---\ntitle: A Shared\naliases: [a s]\n---\n{_BODY}")
    (wiki_dir / "B" / "Shared.md").write_text(f"---\ntitle: B Shared\naliases: [b s]\n---\n{_BODY}")
    (wiki_dir / "Source.md").write_text(
        f"---\ntitle: Source\naliases: [src]\n---\nSee [[Shared]].\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert len(res["ambiguous_names"]) == 1
    amb = res["ambiguous_names"][0]
    assert amb["name"] == "Shared"
    assert amb["paths"] == ["Wiki/A/Shared.md", "Wiki/B/Shared.md"]
    # An ambiguous link is not a broken link, and it must not manufacture
    # orphans: both candidates are credited rather than one guessed at.
    assert res["broken_links"] == []
    assert "Wiki/A/Shared" not in res["orphans"]
    assert "Wiki/B/Shared" not in res["orphans"]


def test_folder_qualified_link_resolves_to_one_page(tmp_path):
    """[[A/Shared]] addresses exactly one of two same-named pages.

    The resolver dropped the folder prefix before matching, discarding the one
    piece of information that disambiguates the link.
    """
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "A").mkdir()
    (wiki_dir / "B").mkdir()

    (wiki_dir / "A" / "Shared.md").write_text(f"---\ntitle: A Shared\naliases: [a s]\n---\n{_BODY}")
    (wiki_dir / "B" / "Shared.md").write_text(f"---\ntitle: B Shared\naliases: [b s]\n---\n{_BODY}")
    (wiki_dir / "Source.md").write_text(
        f"---\ntitle: Source\naliases: [src]\n---\nSee [[A/Shared]].\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["broken_links"] == []
    # Only the addressed page loses orphan status.
    assert "Wiki/A/Shared" not in res["orphans"]
    assert "Wiki/B/Shared" in res["orphans"]


def test_nested_index_md_is_still_audited(tmp_path):
    """Only Wiki/Index.md is exempt — a subdirectory Index.md is a content page."""
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "Section").mkdir()

    (wiki_dir / "Index.md").write_text("---\ntitle: Index\n---\nCurated map.\n")
    (wiki_dir / "Section" / "Index.md").write_text("---\ntitle: Section Index\n---\nTiny.")

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["wiki_notes_count"] == 1
    assert "Wiki/Section/Index" in res["empty_pages"]


def test_unindexed_page_reported_and_indexed_page_is_not(tmp_path):
    """A page absent from the curated Index.md is a finding of its own.

    Orphan detection asks "has backlinks?", which a freshly written page linked
    from one other page passes — so index absence went unreported.
    """
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"

    (wiki_dir / "Index.md").write_text(
        "---\ntitle: Index\n---\n# Map\n- [[Listed]] — a curated entry.\n"
    )
    (wiki_dir / "Listed.md").write_text(f"---\ntitle: Listed\naliases: [listed]\n---\n{_BODY}")
    # Has a backlink (so not an orphan) but never made it into the index.
    (wiki_dir / "Unlisted.md").write_text(
        f"---\ntitle: Unlisted\naliases: [unlisted]\n---\n{_BODY}"
    )
    (wiki_dir / "Listed2.md").write_text(
        f"---\ntitle: Listed Two\naliases: [l2]\n---\nSee [[Unlisted]].\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["index_present"] is True
    assert "Unlisted" in res["unindexed_pages"]
    assert "Unlisted" not in res["orphans"]  # the point: not an orphan, still unindexed
    assert "Listed" not in res["unindexed_pages"]


def test_index_alias_entry_counts_as_coverage(tmp_path):
    """An index entry written against a page's alias still covers that page."""
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"

    (wiki_dir / "Index.md").write_text("---\ntitle: Index\n---\n- [[old name]]\n")
    (wiki_dir / "Canonical.md").write_text(
        f"---\ntitle: Canonical\naliases: [Canon, Old Name]\n---\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["unindexed_pages"] == []


def test_absent_index_is_reported_not_silently_clean(tmp_path):
    """No Index.md means coverage is unknown, which must not read as clean."""
    vault_dir = _make_vault(tmp_path)
    (vault_dir / "Wiki" / "Page.md").write_text(f"---\ntitle: Page\naliases: [p]\n---\n{_BODY}")

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=False)

    assert res["index_present"] is False
    assert res["unindexed_pages"] == []
    content = (vault_dir / "Wiki" / "Health.md").read_text(encoding="utf-8")
    assert "no `Wiki/Index.md`" in content


def test_health_md_reports_ambiguous_and_unindexed(tmp_path):
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "A").mkdir()
    (wiki_dir / "B").mkdir()

    (wiki_dir / "Index.md").write_text("---\ntitle: Index\n---\nEmpty map.\n")
    (wiki_dir / "A" / "Shared.md").write_text(f"---\ntitle: A Shared\naliases: [a s]\n---\n{_BODY}")
    (wiki_dir / "B" / "Shared.md").write_text(f"---\ntitle: B Shared\naliases: [b s]\n---\n{_BODY}")
    # The bare link is what makes the collision an actual ambiguity, and so
    # what puts it in the rendered report at all (#206).
    (wiki_dir / "Source.md").write_text(
        f"---\ntitle: Source\naliases: [src]\n---\nSee [[Shared]].\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        run_custodian(dry_run=False)

    content = (wiki_dir / "Health.md").read_text(encoding="utf-8")
    assert "### ❓ Ambiguous Page Names" in content
    assert "### 🧭 Pages Missing From Index" in content
    assert "`Wiki/A/Shared.md`" in content
    # Health.md links a colliding page by its full vault path, which is what
    # Obsidian needs to resolve it unambiguously.
    assert "[[Wiki/A/Shared]]" in content


def test_stem_collision_with_disjoint_aliases_is_not_a_duplicate(tmp_path):
    """Parallel doc trees legitimately hold a page per topic (#206).

    Wiki/Antigravity/Overview and Wiki/Claude Code/Overview collide on the file
    stem and nothing else: their alias sets are disjoint, so the author has
    already disambiguated them. Reporting the pair forever taught the reader to
    skip the whole check.
    """
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "Antigravity").mkdir()
    (wiki_dir / "Claude Code").mkdir()

    (wiki_dir / "Antigravity" / "Overview.md").write_text(
        f"---\ntitle: Antigravity Overview\naliases: [Antigravity overview, AGY overview]\n---\n"
        f"{_BODY}"
    )
    (wiki_dir / "Claude Code" / "Overview.md").write_text(
        f"---\ntitle: Claude Code Overview\naliases: [Claude Code overview]\n---\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["duplicate_candidates"] == []


def test_stem_collision_still_duplicate_when_an_alias_matches(tmp_path):
    """Suppression is narrow: an alias colliding with the other page's stem bites."""
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "A").mkdir()
    (wiki_dir / "B").mkdir()

    (wiki_dir / "A" / "Shared.md").write_text(f"---\ntitle: A Shared\naliases: [a s]\n---\n{_BODY}")
    # This page calls itself "shared" — the very name the other page owns.
    (wiki_dir / "B" / "Shared.md").write_text(
        f"---\ntitle: B Shared\naliases: [shared]\n---\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert len(res["duplicate_candidates"]) == 1
    assert res["duplicate_candidates"][0]["via"] == "shared"


def test_stem_collision_without_aliases_is_still_a_duplicate(tmp_path):
    """A page with no aliases has disambiguated nothing — disjointness is vacuous."""
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "A").mkdir()
    (wiki_dir / "B").mkdir()

    (wiki_dir / "A" / "Shared.md").write_text(f"---\ntitle: A Shared\naliases: [a s]\n---\n{_BODY}")
    (wiki_dir / "B" / "Shared.md").write_text(f"---\ntitle: B Shared\n---\n{_BODY}")

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert len(res["duplicate_candidates"]) == 1
    assert res["duplicate_candidates"][0]["via"] == "shared"


def test_ambiguous_name_silent_when_every_link_is_qualified(tmp_path):
    """A collision nothing links ambiguously is latent, not a defect (#206)."""
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "A").mkdir()
    (wiki_dir / "B").mkdir()

    (wiki_dir / "A" / "Shared.md").write_text(f"---\ntitle: A Shared\naliases: [a s]\n---\n{_BODY}")
    (wiki_dir / "B" / "Shared.md").write_text(f"---\ntitle: B Shared\naliases: [b s]\n---\n{_BODY}")
    (wiki_dir / "Source.md").write_text(
        f"---\ntitle: Source\naliases: [src]\n---\nSee [[Wiki/A/Shared]] and [[B/Shared]].\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["ambiguous_names"] == []
    assert res["broken_links"] == []


def test_ambiguous_name_fires_on_a_bare_link_from_outside_the_wiki(tmp_path):
    """The gate is vault-wide: a bare [[Shared]] in a journal is a real ambiguity."""
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "A").mkdir()
    (wiki_dir / "B").mkdir()

    (wiki_dir / "A" / "Shared.md").write_text(f"---\ntitle: A Shared\naliases: [a s]\n---\n{_BODY}")
    (wiki_dir / "B" / "Shared.md").write_text(f"---\ntitle: B Shared\naliases: [b s]\n---\n{_BODY}")
    (vault_dir / "Logs").mkdir()
    (vault_dir / "Logs" / "2026-07-28.md").write_text(
        "09:00 — reread [[Shared]] before the sweep.\n"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert [a["name"] for a in res["ambiguous_names"]] == ["Shared"]


def test_bare_link_inside_code_span_is_a_mention_not_a_link(tmp_path):
    """Obsidian renders no link inside backticks, so neither does this gate (#206).

    The journal writes prose *about* the collision (`[[Shared]]`), and the
    generated state mirror echoes it back — which kept the finding alive on
    every sweep with no ambiguous link anywhere in the vault.
    """
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "A").mkdir()
    (wiki_dir / "B").mkdir()

    (wiki_dir / "A" / "Shared.md").write_text(f"---\ntitle: A Shared\naliases: [a s]\n---\n{_BODY}")
    (wiki_dir / "B" / "Shared.md").write_text(f"---\ntitle: B Shared\naliases: [b s]\n---\n{_BODY}")
    (vault_dir / "Logs").mkdir()
    (vault_dir / "Logs" / "2026-07-28.md").write_text(
        "Sweep: a bare `[[Shared]]` link would be ambiguous, but none exists.\n"
        "```\nexample: [[Shared]]\n```\n"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["ambiguous_names"] == []


def test_svg_embed_of_existing_asset_is_not_a_broken_link(tmp_path):
    """`![[diagram.svg]]` resolves to a real file anywhere in the vault (#855).

    The custodian only indexed `.md` stems, so every rendered-diagram embed
    showed up as a broken link even though Obsidian resolves it fine.
    """
    vault_dir = _make_vault(tmp_path)
    (vault_dir / "Assets" / "diagrams").mkdir(parents=True)
    (vault_dir / "Assets" / "diagrams" / "topology.svg").write_text("<svg></svg>")
    (vault_dir / "Wiki" / "Infra.md").write_text(
        f"---\ntitle: Infra\naliases: [infra]\n---\n{_BODY}\n![[topology.svg]]\n"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["broken_links"] == []


def test_missing_svg_embed_is_still_a_broken_link(tmp_path):
    """The asset-resolution relaxation must not swallow a genuinely absent file."""
    vault_dir = _make_vault(tmp_path)
    (vault_dir / "Wiki" / "Infra.md").write_text(
        f"---\ntitle: Infra\naliases: [infra]\n---\n{_BODY}\n![[nowhere.svg]]\n"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert [b["target"] for b in res["broken_links"]] == ["nowhere.svg"]


def test_code_span_wikilink_in_a_wiki_page_is_not_a_broken_link(tmp_path):
    """A page documenting wikilink syntax (`[[Foo]]` in backticks) links nothing.

    parse_markdown_file strips code before extracting links, so the broken-link
    and backlink checks never see a mention as a reference (#855).
    """
    vault_dir = _make_vault(tmp_path)
    (vault_dir / "Wiki" / "Obsidian.md").write_text(
        f"---\ntitle: Obsidian\naliases: [obsidian]\n---\n"
        f"Write a link as `[[Some Page]]` or an embed as `![[drawing.excalidraw]]`.\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["broken_links"] == []


def test_bare_link_in_a_state_mirror_does_not_trigger_ambiguous(tmp_path):
    """`Logs/state_memory.*` / `state_tasks.*` are machine-generated dumps (#855).

    They echo memory and task text verbatim — including stale copies of this
    report's own tag directory — so a bare `[[Shared]]` there is not an authored
    link. A hand-written journal line still counts (covered elsewhere).
    """
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "A").mkdir()
    (wiki_dir / "B").mkdir()
    (wiki_dir / "A" / "Shared.md").write_text(f"---\ntitle: A Shared\naliases: [a s]\n---\n{_BODY}")
    (wiki_dir / "B" / "Shared.md").write_text(f"---\ntitle: B Shared\naliases: [b s]\n---\n{_BODY}")
    (vault_dir / "Logs").mkdir()
    (vault_dir / "Logs" / "state_memory.some-host.local.md").write_text(
        "- [[Shared]] — A — Shared\n- [[Shared]] — B — Shared\n"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["ambiguous_names"] == []


def test_cross_linked_landing_and_deep_page_are_not_a_duplicate(tmp_path):
    """A hub page and its deep page share an alias on purpose (#855).

    Cursor-Platform-Docs aliases "cursor mcp" and links Cursor/MCP, which also
    aliases "cursor mcp"; both are in Index.md. Deliberate pair, not a dup.
    """
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "Cursor").mkdir()
    (wiki_dir / "Cursor-Platform-Docs.md").write_text(
        f'---\ntitle: Cursor Platform Docs\naliases: ["cursor docs", "cursor mcp"]\n---\n'
        f"Landing page. Deep dive: [[Cursor/MCP]].\n{_BODY}"
    )
    (wiki_dir / "Cursor" / "MCP.md").write_text(
        f'---\ntitle: Cursor MCP\naliases: ["cursor mcp"]\n---\n'
        f"See also [[Cursor-Platform-Docs]].\n{_BODY}"
    )
    (wiki_dir / "Index.md").write_text(
        f"---\ntitle: Index\n---\n[[Cursor-Platform-Docs]] · [[Cursor/MCP]]\n{_BODY}"
    )

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert res["duplicate_candidates"] == []


def test_shared_alias_without_cross_link_is_still_a_duplicate_candidate(tmp_path):
    """The suppression needs BOTH a mutual link and index coverage — not just a
    shared alias — so a genuine accidental dup still reports."""
    vault_dir = _make_vault(tmp_path)
    wiki_dir = vault_dir / "Wiki"
    (wiki_dir / "One.md").write_text(f'---\ntitle: One\naliases: ["thesame"]\n---\n{_BODY}')
    (wiki_dir / "Two.md").write_text(f'---\ntitle: Two\naliases: ["thesame"]\n---\n{_BODY}')

    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        res = run_custodian(dry_run=True)

    assert any(d["via"] == "thesame" for d in res["duplicate_candidates"]), res[
        "duplicate_candidates"
    ]


def _custodian_in_thread(vault_dir):
    """Run a non-dry custodian pass in a daemon thread; returns (thread, outcome)."""
    import threading

    outcome: dict = {}

    def run():
        try:
            with patch("unified_agent.config.vault_root", return_value=vault_dir):
                outcome["result"] = run_custodian(dry_run=False)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, outcome


def test_health_report_write_waits_for_the_note_lock(tmp_path):
    """The custodian takes the per-note lock vault_write takes (#2058)."""
    from unified_agent.tools.vault import note_write_lock

    vault_dir = _make_vault(tmp_path)
    (vault_dir / "Wiki" / "Page.md").write_text(f"---\ntitle: Page\n---\n{_BODY}")
    health = vault_dir / "Wiki" / "Health.md"
    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        held = note_write_lock(health)
        held.__enter__()
    try:
        worker, outcome = _custodian_in_thread(vault_dir)
        worker.join(timeout=2)
        assert worker.is_alive(), "Health.md was written without the note lock"
        assert not health.exists()
    finally:
        held.__exit__(None, None, None)
    worker.join(timeout=10)
    assert "error" not in outcome, outcome
    assert outcome["result"]["index_rebuilt"] is True
    assert "# Wiki Health Report" in health.read_text(encoding="utf-8")


def test_health_report_lock_guard_is_checked_under_the_note_lock(tmp_path):
    """A note locked by the writer the custodian waited on is still a human hold."""
    from unified_agent.tools.vault import note_write_lock

    vault_dir = _make_vault(tmp_path)
    (vault_dir / "Wiki" / "Page.md").write_text(f"---\ntitle: Page\n---\n{_BODY}")
    health = vault_dir / "Wiki" / "Health.md"
    with patch("unified_agent.config.vault_root", return_value=vault_dir):
        held = note_write_lock(health)
        held.__enter__()
    try:
        worker, outcome = _custodian_in_thread(vault_dir)
        worker.join(timeout=2)
        assert worker.is_alive()
        health.write_text("---\nlocked: true\n---\nHUMAN\n", encoding="utf-8")
    finally:
        held.__exit__(None, None, None)
    worker.join(timeout=10)
    assert "error" not in outcome, outcome
    assert outcome["result"]["health_report_skipped"] == "locked"
    assert health.read_text(encoding="utf-8") == "---\nlocked: true\n---\nHUMAN\n"
