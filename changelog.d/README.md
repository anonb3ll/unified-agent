# Changelog fragments

Every pull request adds **one fragment file** here instead of editing
`CHANGELOG.md`. Fragment names are unique per task, so two PRs never touch the
same file and a batch of squash merges does not conflict on the changelog.

## Convention

```text
changelog.d/<task-or-pr-id>.<type>.md
```

- `<task-or-pr-id>`: the hub task or PR number (`1808`). If one task needs more
  than one fragment of the same type, add a suffix (`1808-2`).
- `<type>`: one of `added`, `changed`, `fixed`, `security`, `removed`,
  `deprecated`.
- Content: the markdown bullet(s) exactly as they should appear in
  `CHANGELOG.md`, starting with `- `. Describe the effect on a user, not the
  diff. No headings: the assembler adds `### Added` and the rest.

Example, `changelog.d/1234.fixed.md`:

```markdown
- **Dispatch no longer drops a stack on a partial read (#1234).** The launch
  now fails closed when a task or dependency read is incomplete.
```

## Checking and assembling

```bash
python scripts/changelog_assemble.py --check     # validate names, types, content
python scripts/changelog_assemble.py --dry-run   # print the resulting Unreleased section
python scripts/changelog_assemble.py             # write CHANGELOG.md, delete the fragments
```

The assembler runs at release time (and may be run by whoever cuts a batch). It
groups fragments by type in Keep a Changelog order (Added, Changed, Deprecated,
Removed, Fixed, Security) and sorts them newest first (highest numeric id first,
then name), like hand-written entries. It inserts them directly under the
matching `### <Type>` heading under `## [Unreleased]` (the first one, if the
section repeats), creating the subsection when it is missing. Released sections
are never touched, dotfiles in this directory are ignored, and the changelog is
replaced atomically. Fragments are deleted only after that; a fragment whose
exact text is already in Unreleased (a run that died before deleting) is skipped
and deleted, so re-running never duplicates an entry. Re-running with no
fragments does nothing.

Do not hand-edit `CHANGELOG.md` for a new change. A PR that still edits it
directly is accepted for now so that work already in flight can land.
