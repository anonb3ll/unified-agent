# Reviewed skill packages

Task #1547 extends `skill_versions`, rather than introducing a second package store.
New versions capture schema-v1 manifests: canonical relative member path, SHA-256,
byte length, and normalized executable mode (0644 or 0755). SKILL.md is always 0644.
Payload bytes are stored with the immutable version; public metadata and `get`
omit raw supporting-file payloads. SQLite triggers prohibit version/provenance
updates and deletes. Readers and dispatch verify the stored attestation.

Single-file packages preserve the SKILL.md SHA-256 API. Multi-file `content_hash`
is SHA-256 of the canonical sorted JSON manifest. A supporting-file or executable
mode change changes that hash and mints a version. Dispatch's `hash` identifies the
version package, while its bounded `content` remains SKILL.md reference text.

Limits: 128 files, 512 scanned entries, 8 path components, 240 characters per
relative path (NFC/casefold collisions are refused for Mac portability), 64 KiB SKILL.md, 512 KiB per supporting file, and 2 MiB total bytes.
Every entry must be a regular file or directory; symlinks, special files, hidden
or secret-bearing member paths, traversal, and absolute/local URI references are
refused. Binary payloads are allowed only as non-executable assets. Their decoded
bytes still undergo bounded credential/content screening; binaries are never
executed during review. Canonical skill_review findings and existing audit content
rules apply before publication. A complete post-review snapshot must match the
captured bytes/modes. Missing local references block publication.

Initialization adds nullable columns to existing versions; it never reconstructs
an attestation from mutable source files. Existing versions retain their hash and
provenance. Republishing an unchanged legacy single-file skill creates an explicit
new attested version, then repeats are idempotent. Legacy supporting-file refusal
remains until an attested version is published.

Installation reads only verified stored bytes. Existing filesystem/profile/trust,
protected-path, policy and approval gates govern every incoming and removed member.
The lower approval includes the complete manifest, removed paths and observed base
package hash (or absence). The base, full staged manifest and policy are rechecked before activation.
A private sibling stage contains the complete proposed tree; replacement moves the
old directory to a private backup and activates the staged directory. Exceptions
restore the old tree and remove staging; no mixed partial package is installed.
A process interruption between directory renames can leave the old tree in its
private backup, which is retained for recovery. Receipts are marked at the first
activation mutation, so staging/refusal restores them while post-mutation errors
retain consumption. Installation evidence carries exact version/hash/manifest and
publisher/source/time provenance.

Drift compares complete installed file membership, contents and executable modes
against the selected version; extra, missing and edited assets are stale. Agent
scopes honor pinned versions. The scanner remains read-only and does not follow
member symlinks or escape a selected surface.

Local links in every captured `.md` or `.markdown` document resolve relative to that document. Traversal, absolute paths, local URI schemes, and missing members are refused; the package-wide reference check stops at 4,096 references. An attested destination with supporting files but no `SKILL.md` is stale. An absent destination is missing. Existing empty directories retain an empty-manifest hash and require that exact expected hash to replace.

Reference validation uses markdown-it-py's canonical CommonMark tokens. Parsed
link/image destinations, titles and label text are checked; reference-definition
labels, unused destinations/titles and shadowed duplicates remain checked without
double-counting a used definition's destination/title; malformed prose
falls back to text without hiding later valid links. Literal inline/fenced
code retains bare supporting-file checks and does not interpret link examples.
CommonMark fences can interrupt a paragraph: a physical line beginning with
`~~~` opens a fence even when an earlier unmatched backtick looks like an inline
opener. That fence's Markdown examples remain literal; its bare file mentions
remain governed. Block/inline nesting and recursive image labels are bounded by
20 parser levels; exceeding the parser boundary refuses publication and stored
version verification instead of silently skipping destinations. Literal code
preserves unmatched brackets in supporting filenames. The existing package bounds
and 4096-reference cap still apply.
