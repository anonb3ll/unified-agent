"""Wiki Custodian & Auto-Documenter CLI.

Audits vault cross-links, orphans, inbox notes, and rebuilds Wiki/Health.md.

Wiki/Health.md is the machine-generated hygiene report (tag catalog, recent
updates, pending inbox, broken links, orphans, duplicate candidates, missing
aliases, empty/stub pages, stale pages), regenerated wholesale on every run.
It is deliberately separate from Wiki/Index.md, the curated human-facing
map that Steward/steward-inbox maintains — so the custodian never clobbers
curation.
"""

import argparse
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config
from .hosted_capabilities import refuse_unpopulated_vault
from .safety import VaultLockedError, resolve_in_vault
from .tools.vault import WIKILINK_RE, _is_locked_path, note_write_lock

# WIKILINK_RE is shared with tools/vault.py (single source of truth): its capture
# group is the bare target with any #heading/#^block anchor and |alias already
# stripped, so callers never re-implement that normalization.
# Inline #tag. The lookahead requires at least one letter or underscore, which
# is Obsidian's own rule: a purely numeric #123 is not a tag. Without it, every
# task/PR reference in prose ("#122", "tasks #104/#105") became a tag and the
# generated tag directory filled with 100+ single-note pseudo-tags.
TAG_REGEX = re.compile(r"\B#(?=[a-zA-Z0-9_\-/]*[a-zA-Z_])([a-zA-Z0-9_\-/]+)")

# Note extensions a wikilink may spell out explicitly ([[folder/Note.md]]).
_NOTE_SUFFIXES = (".md", ".markdown", ".txt")


def _link_target_name(target: str) -> str:
    """Resolve a wikilink target to the note name it addresses.

    Drops any folder prefix and a trailing note extension, but preserves dots
    inside the name itself: Obsidian note titles routinely carry version-ish
    fragments ("Spec - Phase 3.1 Foo"), and treating those as file suffixes
    truncates the name and reports a live note as a broken link.
    """
    name = Path(target.strip()).name
    for suffix in _NOTE_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def _link_target_path(target: str) -> str:
    """Normalize a wikilink target to a comparable path, keeping any folders.

    The counterpart to _link_target_name: where that one throws the folder away
    to get at the note name, this one keeps it, because a folder-qualified link
    is the only way an author can address one of two same-named pages.
    """
    path = target.strip().strip("/")
    for suffix in _NOTE_SUFFIXES:
        if path.lower().endswith(suffix):
            return path[: -len(suffix)]
    return path


# Fenced blocks first, then inline spans (longest run of backticks wins, so a
# ``code with ` inside`` span is consumed whole rather than half).
_FENCED_CODE_RE = re.compile(r"^(?P<fence>```+|~~~+).*?(?:^(?P=fence)\s*$|\Z)", re.M | re.S)
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(?:(?!(?P=ticks)).)*?(?P=ticks)", re.S)


def _strip_code(text: str) -> str:
    """Blank out fenced blocks and inline code spans, keeping offsets meaningless.

    Used only where a wikilink must be a *link* rather than a mention: Obsidian
    renders neither ```[[X]]``` nor `[[X]]` as a link, so text inside code is
    prose about a page, not a reference to it.
    """
    return _INLINE_CODE_RE.sub(" ", _FENCED_CODE_RE.sub(" ", text))


def _rel_key(path: Path, v_root: Path) -> str:
    """Vault-relative POSIX path of a note — the identity a page is audited under.

    The stem is not an identity: two pages may share one across subdirectories
    (Wiki/Antigravity/Overview.md vs Wiki/Claude Code/Overview.md), and keying
    the inventory by stem silently dropped one of them from every check while
    the report still read all-clean.
    """
    return path.relative_to(v_root).as_posix()


def _display_key(rel: str, name_to_keys: dict) -> str:
    """Human/Obsidian-facing identifier for a page.

    A page whose stem is unique keeps that bare stem, so findings and generated
    [[links]] read the way they always have. A page whose stem collides is
    identified by its vault-relative path minus the extension — which is also
    the form Obsidian needs to resolve [[Wiki/Antigravity/Overview]] to exactly
    one page.
    """
    name = Path(rel).stem
    if len(name_to_keys.get(name, ())) > 1:
        return rel[: -len(".md")] if rel.lower().endswith(".md") else rel
    return name


def parse_markdown_file(path: Path) -> dict:
    """Parse a markdown file for title, frontmatter tags, inline tags, wikilinks."""
    content = ""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Warning reading {path.name}: {e}", file=sys.stderr)

    mtime = datetime.fromtimestamp(path.stat().st_mtime)

    title = None
    tags = set()
    aliases = set()
    wikilinks = set()

    # 1. Parse YAML Frontmatter
    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    content_body = content
    if frontmatter_match:
        frontmatter_text = frontmatter_match.group(1)
        content_body = content[frontmatter_match.end() :]

        # Parse simple frontmatter keys
        fm_lines = frontmatter_text.splitlines()
        i = 0
        while i < len(fm_lines):
            line = fm_lines[i].strip()
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip().strip("\"'")
            elif line.startswith("tags:"):
                # Support inline lists ([a, b]), a single scalar value, and
                # block-style YAML lists (tags:\n  - a\n  - b). Never record an
                # empty-string tag, which would emit a blank "### #" index section.
                t_val = line.split(":", 1)[1].strip()
                if t_val.startswith("[") and t_val.endswith("]"):
                    items = [x.strip().strip("\"'") for x in t_val[1:-1].split(",")]
                    tags.update(x for x in items if x)
                elif t_val:
                    tags.add(t_val.strip("\"'"))
                else:
                    j = i + 1
                    while j < len(fm_lines) and fm_lines[j].lstrip().startswith("- "):
                        item = fm_lines[j].lstrip()[2:].strip().strip("\"'")
                        if item:
                            tags.add(item)
                        j += 1
                    i = j - 1
            elif line.startswith("aliases:"):
                # Same three shapes as tags: inline list, single scalar, or
                # block-style YAML list. Case is preserved (unlike tags) since
                # aliases are matched case-insensitively at comparison time by
                # run_custodian; never record an empty-string alias.
                a_val = line.split(":", 1)[1].strip()
                if a_val.startswith("[") and a_val.endswith("]"):
                    items = [x.strip().strip("\"'") for x in a_val[1:-1].split(",")]
                    aliases.update(x for x in items if x)
                elif a_val:
                    aliases.add(a_val.strip("\"'"))
                else:
                    j = i + 1
                    while j < len(fm_lines) and fm_lines[j].lstrip().startswith("- "):
                        item = fm_lines[j].lstrip()[2:].strip().strip("\"'")
                        if item:
                            aliases.add(item)
                        j += 1
                    i = j - 1
            i += 1

    # 2. Extract first H1 Heder as fallback title
    if not title:
        for line in content_body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        if not title:
            title = path.stem

    # 3. Extract inline tags
    inline_tags = TAG_REGEX.findall(content_body)
    tags.update(inline_tags)

    # 4. Extract wikilinks (target name; anchors/aliases already stripped by
    # WIKILINK_RE). Code spans are blanked first: Obsidian renders neither
    # ```[[X]]``` nor `[[X]]` as a link, so a page *documenting* wikilink syntax
    # (Excalidraw-Plugin, Obsidian) is not linking to `[[wikilinks]]` or
    # `drawing.excalidraw`. Every consumer of this list — the broken-link check,
    # backlink credit, index coverage — wants links, not mentions.
    for link in WIKILINK_RE.findall(_strip_code(content)):
        wikilinks.add(link.strip())

    return {
        "path": str(path),
        "name": path.stem,
        "title": title,
        "tags": sorted(list(tags)),
        "aliases": sorted(list(aliases)),
        "wikilinks": sorted(list(wikilinks)),
        "mtime": mtime.strftime("%Y-%m-%d %H:%M:%S"),
        "size": path.stat().st_size,
        "body_chars": len(content_body.strip()),
    }


def run_custodian(dry_run: bool = False) -> dict:
    """Audit the Obsidian Wiki and rebuild the index.

    A locked `Wiki/Health.md` (`locked: true` frontmatter) is not overwritten:
    the run skips the rewrite, prints a notice, and returns
    `health_report_skipped="locked"` with `index_rebuilt=False`.
    """
    refuse_unpopulated_vault("vault_wiki_custodian")
    v_root = config.vault_root()
    if not v_root or not v_root.exists():
        raise ValueError("Vault directory path is not set or does not exist.")

    wiki_dir = v_root / "Wiki"
    inbox_dir = v_root / "00 - Inbox"

    if not dry_run:
        wiki_dir.mkdir(parents=True, exist_ok=True)
        inbox_dir.mkdir(parents=True, exist_ok=True)

    # 1. Gather all notes in Wiki/, keyed by vault-relative path.
    #
    # Path-keyed, not stem-keyed: same-named pages in different subdirectories
    # are distinct pages, and collapsing them dropped the shadowed one out of
    # every downstream check without the report saying so.
    wiki_notes = {}
    index_note = None
    excluded_keys = []

    for root, _, files in os.walk(wiki_dir):
        for f in files:
            if f.endswith(".md"):
                path = Path(root) / f
                rel = _rel_key(path, v_root)
                # Exclude the curated Index and the generated Health report from
                # the parsed page set: neither is a content page, and counting
                # their all-pages link lists would mask genuine orphans. Matched
                # on the exact vault path — a Wiki/Section/Index.md is an
                # ordinary page and must still be audited.
                if rel == "Wiki/Health.md":
                    excluded_keys.append(rel)
                    continue
                note_data = parse_markdown_file(path)
                note_data["rel"] = rel
                if rel == "Wiki/Index.md":
                    index_note = note_data
                    excluded_keys.append(rel)
                    continue
                wiki_notes[rel] = note_data

    # Stem -> keys. Link resolution stays name-based because that is how
    # Obsidian addresses pages; a stem mapping to several keys is genuinely
    # ambiguous and is reported rather than silently resolved to one.
    name_to_keys = {}
    for rel in sorted(wiki_notes):
        name_to_keys.setdefault(Path(rel).stem, []).append(rel)

    # The two excluded files are still files: a Wiki/Section/Index.md collides
    # with the curated Wiki/Index.md just as surely as with another content
    # page, so they count toward name collisions even though they are never
    # audited as pages themselves.
    all_name_keys = {name: list(keys) for name, keys in name_to_keys.items()}
    for rel in sorted(excluded_keys):
        all_name_keys.setdefault(Path(rel).stem, []).append(rel)

    for rel, note in wiki_notes.items():
        note["display"] = _display_key(rel, all_name_keys)
    display_of = {rel: note["display"] for rel, note in wiki_notes.items()}

    colliding_names = {
        name: sorted(keys) for name, keys in sorted(all_name_keys.items()) if len(keys) > 1
    }

    # Lowercased alias set for any page key, including the two excluded files —
    # a collision check spanning Wiki/Index.md needs their aliases too.
    _notes_by_key = dict(wiki_notes)
    if index_note is not None:
        _notes_by_key[index_note["rel"]] = index_note

    def aliases_lower_of(key: str) -> set[str]:
        return {a.lower() for a in _notes_by_key.get(key, {}).get("aliases", [])}

    # Folder-qualified targets ([[Antigravity/Overview]]) resolve exactly. Both
    # the vault-rooted form and the Wiki-relative form are accepted, since
    # Obsidian lets an author write either.
    path_to_key = {}
    for rel in wiki_notes:
        stripped = rel[: -len(".md")]
        path_to_key[stripped.lower()] = rel
        if stripped.startswith("Wiki/"):
            path_to_key.setdefault(stripped[len("Wiki/") :].lower(), rel)

    # 2. Gather all notes in 00 - Inbox/
    inbox_notes = []
    try:
        inbox_entries = os.listdir(inbox_dir)
    except FileNotFoundError:
        inbox_entries = []
    for f in inbox_entries:
        path = inbox_dir / f
        if path.is_file() and not f.startswith("."):
            # If it's markdown, parse it, otherwise just list it
            if f.endswith(".md"):
                inbox_notes.append(parse_markdown_file(path))
            else:
                inbox_notes.append(
                    {
                        "name": path.stem,
                        "title": path.name,
                        "path": str(path),
                        "tags": [],
                        "wikilinks": [],
                        "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "size": path.stat().st_size,
                    }
                )

    # 3. Analyze Links & Orphans
    broken_links = []
    incoming_links = {rel: set() for rel in wiki_notes}

    # Build the set of every note stem in the vault once (single pass), skipping
    # hidden dirs like .git/.obsidian so their internal files (e.g. "config",
    # "HEAD") don't mask genuinely broken wikilinks. O(files) instead of the
    # previous O(targets x files) full re-walk per unresolved link.
    #
    # The same pass collects every *bare* [[Name]] target written anywhere in
    # the vault (lowercased). A name collision only bites when someone links by
    # bare name: if every inbound link is folder-qualified, Obsidian resolves
    # them all and so does this audit. Wiki/Health.md is skipped — it is this
    # report, regenerated each run, so its own links must not feed the check.
    # Code spans are stripped first: Obsidian does not render a wikilink inside
    # backticks, so prose *about* a link (journal entries quoting `[[Overview]]`,
    # the generated Logs/state_tasks mirror echoing this very task) is not a
    # link. `parse_markdown_file` strips code the same way, so wiki_notes[...]
    # ["wikilinks"] the broken-link and orphan checks read is already clean.
    #
    # `all_vault_filenames` keeps the full basename (with extension) of every
    # file, so an embed of a non-note asset — `![[vault-flow-para.svg]]`,
    # `![[drawing.excalidraw]]` — resolves to the real file the way Obsidian's
    # shortest-path match does, instead of being reported as a broken link
    # because only `.md` stems were indexed.
    all_vault_stems = set()
    all_vault_filenames = set()
    bare_link_names = set()
    for _root, dirs, files in os.walk(v_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            all_vault_stems.add(Path(f).stem)
            all_vault_filenames.add(f.lower())
            if not f.endswith(".md"):
                continue
            note_path = Path(_root) / f
            rel = _rel_key(note_path, v_root)
            if rel == "Wiki/Health.md":
                continue
            # The auto-generated `state_memory` / `state_tasks` mirrors under
            # `Logs/` are not authored content — they dump memory and task text
            # verbatim, including a stale copy of this report's own tag directory
            # (`- [[MCP]] — Claude Code — MCP`). A hand-written journal line with
            # a bare `[[Shared]]` is still a real ambiguity; a mirror echoing one
            # is not (task #206).
            if Path(f).name.startswith(("state_memory", "state_tasks")):
                continue
            try:
                text = note_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for target in WIKILINK_RE.findall(_strip_code(text)):
                if "/" not in _link_target_path(target):
                    bare_link_names.add(_link_target_name(target).lower())

    # A stem collision is ambiguous when a human wrote a bare link to it in
    # non-mirror content. `[[Index]]` is exempt: `_resolve` (and the special
    # case in the broken-link loop) already canonicalise a bare `[[Index]]` to
    # the top-level `Wiki/Index.md`, the same page Obsidian's shortest-path
    # match picks, so a bare `[[Index]]` is not a coin flip in practice.
    ambiguous_names = [
        {"name": name, "paths": keys}
        for name, keys in colliding_names.items()
        if name.lower() in bare_link_names and name != "Index"
    ]

    # Build an alias -> page-name map (case-insensitive) so a wikilink written
    # against a page's alias resolves to that page instead of tripping the
    # broken-link/orphan checks. Iterate in sorted page-name order so that if
    # two pages ever share an alias, resolution is deterministic (first wins).
    alias_to_page = {}
    for rel in sorted(wiki_notes.keys()):
        for alias in wiki_notes[rel]["aliases"]:
            alias_to_page.setdefault(alias.lower(), rel)

    def _resolve(target: str):
        """Wikilink target -> ([page keys], resolved?).

        Returns every candidate key: a folder-qualified target resolves to one,
        a bare stem shared by several pages resolves to all of them. Crediting
        all candidates is deliberate — guessing one would invent an orphan out
        of the page that lost the coin flip.
        """
        # WIKILINK_RE already dropped any #heading/#^block anchor, leaving the
        # bare target. Try the folder-qualified form first: it is the only way
        # an author can address one of two same-named pages, and dropping the
        # folder before matching discarded exactly that information.
        target_path = _link_target_path(target)
        if "/" in target_path and target_path.lower() in path_to_key:
            return [path_to_key[target_path.lower()]], True
        # Only strip a trailing note extension — a bare `.stem` would truncate
        # any target whose own name contains a dot (e.g. [[00 - Inbox/Spec -
        # Phase 3.1 Foo]] → "Spec - Phase 3"), reporting a live note as broken.
        target_clean = _link_target_name(target)
        if target_clean in name_to_keys:
            return list(name_to_keys[target_clean]), True
        if target_clean.lower() in alias_to_page:
            return [alias_to_page[target_clean.lower()]], True
        return [], False

    for source_rel, note in wiki_notes.items():
        for target in note["wikilinks"]:
            keys, resolved = _resolve(target)
            if resolved:
                for key in keys:
                    incoming_links[key].add(display_of[source_rel])
            elif (
                _link_target_name(target) == "Index"
                or _link_target_name(target) in all_vault_stems
                or Path(target.strip()).name.lower() in all_vault_filenames
            ):
                pass  # Resolves to the index, another note, or an embedded asset elsewhere in the vault
            else:
                broken_links.append({"source": note["display"], "target": target})

    orphans = [display_of[rel] for rel, incoming in incoming_links.items() if not incoming]

    # 3a. Index coverage: a page can carry backlinks (so it is no orphan) and
    # still be missing from the curated map — the shape every freshly authored
    # page takes. Absence of Index.md is reported as unknown, not as clean.
    index_present = index_note is not None
    unindexed_pages = []
    covered: set[str] = set()
    if index_note is not None:
        for target in index_note["wikilinks"]:
            keys, resolved = _resolve(target)
            # An ambiguous index entry covers neither candidate: it is already
            # reported in ambiguous_names, and treating it as coverage would
            # hide both pages behind a link Obsidian cannot resolve either.
            if resolved and len(keys) == 1:
                covered.add(keys[0])
        unindexed_pages = sorted(display_of[rel] for rel in wiki_notes if rel not in covered)

    # 3b. Duplicate candidates: pages whose identity set — {lowercased stem}
    # union {lowercased aliases} — collides with another page's identity set.
    # Report-only; never merge automatically.
    #
    # A collision on the file stem *alone*, between two pages that each carry
    # aliases and share none of them, is not a duplicate: it is one topic
    # documented per product in parallel trees (Antigravity/Overview vs Claude
    # Code/Overview), with the author's alias sets already telling them apart.
    # Suppression is deliberately narrow — an alias on either side matching the
    # collision still reports, and a page with no aliases has disambiguated
    # nothing, so it keeps firing (and shows up in missing_aliases besides).
    #
    # Second suppression: a landing/hub page and its deep page (Cursor-Platform-Docs
    # ↔ Cursor/MCP) collide via a deliberately shared alias, and the alias check
    # above lets that through. But if the two pages link to each other AND both
    # are reachable from Index.md, the author has built a cross-linked pair on
    # purpose — not two copies of one note that drifted apart.
    aliases_of = {rel: aliases_lower_of(rel) for rel in wiki_notes}
    identities = {rel: {Path(rel).stem.lower()} | aliases_of[rel] for rel in wiki_notes}

    def _cross_linked_pair(key_a: str, key_b: str) -> bool:
        a_links_b = display_of[key_a] in incoming_links.get(key_b, ())
        b_links_a = display_of[key_b] in incoming_links.get(key_a, ())
        both_indexed = key_a in covered and key_b in covered
        return (a_links_b or b_links_a) and both_indexed

    duplicate_candidates = []
    sorted_keys = sorted(wiki_notes.keys())
    for idx, key_a in enumerate(sorted_keys):
        for key_b in sorted_keys[idx + 1 :]:
            shared = identities[key_a] & identities[key_b]
            if shared:
                if (
                    aliases_of[key_a]
                    and aliases_of[key_b]
                    and not (shared & (aliases_of[key_a] | aliases_of[key_b]))
                ):
                    continue
                if _cross_linked_pair(key_a, key_b):
                    continue
                duplicate_candidates.append(
                    {"a": display_of[key_a], "b": display_of[key_b], "via": sorted(shared)[0]}
                )

    # 3c. Missing aliases, empty/stub pages, and stale pages.
    missing_aliases = [note["display"] for note in wiki_notes.values() if not note["aliases"]]
    empty_pages = [note["display"] for note in wiki_notes.values() if note["body_chars"] < 80]

    raw_stale_days = os.environ.get("UA_WIKI_STALE_DAYS", "").strip()
    try:
        stale_days = int(raw_stale_days) if raw_stale_days else 90
    except ValueError:
        stale_days = 90
    stale_cutoff = datetime.now() - timedelta(days=stale_days)
    stale_pages = [
        note["display"]
        for note in wiki_notes.values()
        if datetime.strptime(note["mtime"], "%Y-%m-%d %H:%M:%S") < stale_cutoff
    ]

    # 4. Sort notes by modified time for recent updates
    recent_updates = sorted(wiki_notes.values(), key=lambda x: x["mtime"], reverse=True)[:10]

    # Findings carry display keys; the report needs the note behind each one.
    note_by_display = {note["display"]: note for note in wiki_notes.values()}

    # 5. Build Wiki/Health.md (generated hygiene report)

    # Generate tag catalog
    tag_catalog = {}
    for note in wiki_notes.values():
        if not note["tags"]:
            tag_catalog.setdefault("untagged", []).append(note)
        for t in note["tags"]:
            tag_catalog.setdefault(t, []).append(note)

    # Format Wiki/Health.md Markdown
    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md_lines = [
        "---",
        "title: Wiki Health",
        f"updated_at: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "tags: [wiki, meta, health]",
        "---",
        "",
        "# Wiki Health Report",
        "",
        "*Auto-generated by the Wiki Custodian — do not edit by hand. The curated "
        "map is [[Index]].*",
        "",
        f"*Last rebuilt: `{ts_now}`.*",
        "",
        "---",
        "",
        "## Tag Directory",
        "",
    ]

    for tag in sorted(tag_catalog.keys()):
        md_lines.append(f"### #{tag}")
        for note in sorted(tag_catalog[tag], key=lambda x: x["title"]):
            md_lines.append(f"- [[{note['display']}]] — {note['title']}")
        md_lines.append("")

    md_lines.extend(
        ["## Recent Updates", "", "| Note | Title | Last Modified |", "| --- | --- | --- |"]
    )
    for note in recent_updates:
        md_lines.append(f"| [[{note['display']}]] | {note['title']} | {note['mtime']} |")
    md_lines.append("")

    md_lines.extend(
        [
            "## Pending Inbox Capture Tasks",
            "",
            "These are raw notes waiting to be synthesized into PARA Wiki pages:",
            "",
        ]
    )
    if inbox_notes:
        for note in inbox_notes:
            md_lines.append(f"- `00 - Inbox/{note['title']}` — *Modified: {note['mtime']}*")
    else:
        md_lines.append("*No pending inbox notes. Clear!*")
    md_lines.append("")

    md_lines.extend(["## Wiki Health Audit", ""])

    # Broken links
    md_lines.append("### ⚠️ Broken Links")
    if broken_links:
        for link in broken_links:
            md_lines.append(f"- In [[{link['source']}]]: broken link to `[[{link['target']}]]`")
    else:
        md_lines.append("- *No broken links found. Clean!*")
    md_lines.append("")

    # Orphans
    md_lines.append("### 🕸️ Orphan Pages")
    if orphans:
        for name in sorted(orphans):
            note = note_by_display[name]
            md_lines.append(f"- [[{name}]] — {note['title']}")
    else:
        md_lines.append("- *No orphan notes found. Highly cross-linked!*")
    md_lines.append("")

    # Duplicate candidates
    md_lines.append("### 👯 Duplicate Candidates")
    if duplicate_candidates:
        for dup in sorted(duplicate_candidates, key=lambda d: (d["a"], d["b"])):
            md_lines.append(f"- [[{dup['a']}]] ↔ [[{dup['b']}]] — shared identity: `{dup['via']}`")
    else:
        md_lines.append("- *No duplicate candidates found. Clean!*")
    md_lines.append("")

    # Missing aliases
    md_lines.append("### 🏷️ Missing Aliases")
    if missing_aliases:
        for name in sorted(missing_aliases):
            note = note_by_display[name]
            md_lines.append(f"- [[{name}]] — {note['title']}")
    else:
        md_lines.append("- *No pages missing aliases. Clean!*")
    md_lines.append("")

    # Empty / stub pages
    md_lines.append("### 📄 Empty / Stub Pages")
    if empty_pages:
        for name in sorted(empty_pages):
            note = note_by_display[name]
            md_lines.append(f"- [[{name}]] — {note['body_chars']} chars")
    else:
        md_lines.append("- *No empty or stub pages found. Clean!*")
    md_lines.append("")

    # Stale pages
    md_lines.append(f"### 🕰️ Stale Pages (>{stale_days}d)")
    if stale_pages:
        for name in sorted(stale_pages):
            note = note_by_display[name]
            md_lines.append(f"- [[{name}]] — last modified {note['mtime']}")
    else:
        md_lines.append("- *No stale pages found. Clean!*")
    md_lines.append("")

    # Ambiguous page names
    md_lines.append("### ❓ Ambiguous Page Names")
    if ambiguous_names:
        md_lines.append(
            "*One name, several pages, and something in the vault links it bare: that "
            "`[[link]]` cannot be resolved to a single page — by this audit or by "
            "Obsidian. Qualify the link with its folder, or rename the page.*"
        )
        md_lines.append("")
        for amb in ambiguous_names:
            paths = ", ".join(f"`{p}`" for p in amb["paths"])
            md_lines.append(f"- `{amb['name']}` → {paths}")
    else:
        md_lines.append(
            "- *No ambiguous links. Names shared across folders are fine while every "
            "link to them is folder-qualified.*"
        )
    md_lines.append("")

    # Index coverage
    md_lines.append("### 🧭 Pages Missing From Index")
    if not index_present:
        md_lines.append("- *Coverage unknown: there is no `Wiki/Index.md` to check against.*")
    elif unindexed_pages:
        md_lines.append(
            "*These pages exist but [[Index]] does not link them. Backlinks from "
            "elsewhere keep them off the orphan list, so this is the only check "
            "that sees them.*"
        )
        md_lines.append("")
        for name in unindexed_pages:
            note = note_by_display[name]
            md_lines.append(f"- [[{name}]] — {note['title']}")
    else:
        md_lines.append("- *Every page is linked from the curated index. Clean!*")
    md_lines.append("")

    result = {
        "wiki_notes_count": len(wiki_notes),
        "inbox_notes_count": len(inbox_notes),
        "broken_links": broken_links,
        "orphans": orphans,
        "duplicate_candidates": duplicate_candidates,
        "missing_aliases": missing_aliases,
        "empty_pages": empty_pages,
        "stale_pages": stale_pages,
        "ambiguous_names": ambiguous_names,
        "index_present": index_present,
        "unindexed_pages": unindexed_pages,
        "index_rebuilt": False,
    }

    if not dry_run:
        # Route the only mutation through the vault write chokepoint: confines
        # the report to the UA_VAULT_WRITE_DIRS allowlist (Wiki/) by code, not
        # convention, and rejects .md/.txt escapes.
        report_path = None
        try:
            report_path = resolve_in_vault("Wiki/Health.md", for_write=True)
        except VaultLockedError:
            # A cloud caller's resolver refuses a locked note outright.
            pass
        if report_path is None:
            locked = True
        else:
            # Check and write under the note lock vault_write takes (#2058), so a
            # hub writer cannot lock or rewrite Health.md between the two.
            with note_write_lock(report_path):
                locked = _is_locked_path(report_path)
                if not locked:
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text("\n".join(md_lines), encoding="utf-8")
                    result["index_rebuilt"] = True
        if locked:
            # A locked Health.md is a human hold: skip the rewrite, do not fail the run.
            print("Skipped Wiki/Health.md: note is locked", file=sys.stderr)
            result["health_report_skipped"] = "locked"

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Wiki Custodian - Maintains indices and audits vault health."
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Dry run: perform analysis but do not write Health.md.",
    )
    args = parser.parse_args()

    print("🧹 Running Wiki Custodian check...")
    try:
        res = run_custodian(dry_run=args.dry_run)
        print(
            f"✔ Audited {res['wiki_notes_count']} wiki notes and {res['inbox_notes_count']} inbox notes."
        )
        print(f"✔ Broken links found: {len(res['broken_links'])}")
        print(f"✔ Orphan pages found: {len(res['orphans'])}")
        if res["index_rebuilt"]:
            print("✔ Rebuilt Wiki/Health.md successfully.")
        else:
            print("✔ Dry run validation completed without writing.")
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
