# Contributing to unified-agent

Thank you for helping. This guide explains how to set up, check, and submit a
change. It is written for human contributors and for AI agents working on
their behalf.

Report security problems through [SECURITY.md](SECURITY.md), not in public
issues. Read [Change a safety boundary](#change-a-safety-boundary) before you
touch a sandbox, gate, or authentication code.

## Ways to contribute

- **Report a bug.** Open an issue with the version (`pyproject.toml`), your
  transport (stdio or HTTP hub), the exact command or tool call, and what you
  expected. Include a minimal reproduction when you can.
- **Improve the documentation.** Fix a wrong command, a stale claim, or a
  confusing page. Start from the [documentation map](docs/README.md).
- **Add or change a feature.** Open an issue first for anything larger than a
  small fix, so the design can be agreed before you write code.
- **Add a connector.** Read the [connector contract](docs/connectors.md). A
  connector declares a manifest and must pass the conformance harness in
  [`tests/connectors/`](tests/connectors/).
- **Add or change a tool.** Follow
  [Add or change a tool](docs/developer-guide.md#add-or-change-a-tool).
- **Review.** Reading a pull request and testing it locally is a real
  contribution.

## Development setup

The package declares Python 3.11 or newer (`requires-python` in
`pyproject.toml`). Development, CI, and the hub image pin Python 3.14
(`.python-version`, which `uv` and GitHub Actions read, and `python:3.14-slim`
in the `Dockerfile`). You need [uv](https://docs.astral.sh/uv/). You need Node
only when you change a package under [`packages/`](packages/).

```bash
# Set UA_REPO_URL to this repository's clone URL.
git clone "$UA_REPO_URL" unified_agent
cd unified_agent
uv sync                        # runtime dependencies plus the dev group
./scripts/install-hooks.sh     # once per clone
```

CI installs with `uv sync --locked --all-extras` to cover every optional
extra. Use that form if you touch optional integrations.

`./scripts/install-hooks.sh` writes a `pre-commit` hook. It is idempotent and
safe to re-run. The hook runs two gates on every commit:

1. `scripts/check_staged_conflict_markers.py` blocks staged merge-conflict
   markers.
2. `unified-agent-audit . --fail-on high` blocks high-severity security-audit
   findings.

The hook prefers the checkout's `.venv`, so run `uv sync` first.

### JavaScript packages

The packages under `packages/` are `protocol`, `client`, `conformance`, and
`web`. CI's JavaScript job uses Node 26. `packages/web` requires Node
`>=22.22.2` (`engines` in its `package.json`). `protocol`, `client`, and
`conformance` accept Node `>=20`; use 22.22.2 or newer locally so `web`
installs on the same machine. Build them in dependency order, because `client`
and `web` consume the built output of the packages before them:

```bash
cd packages/protocol && npm ci && npm run build
cd ../client && npm install && npm run build
cd ../web && npm ci
```

See [`packages/web/README.md`](packages/web/README.md) for the web scripts.

## Run the checks

Run these locally. GitHub Actions
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) does not run the
pytest shards on every pull request. Opening, pushing to, or labelling a pull
request runs only the cheap gates (lint, smoke, security audit, changelog,
test budget, local pytest attestation verify). The full suite runs on merge
batches through `gh workflow run ci.yml --ref <branch>` and on the nightly
schedule; affected-test runs on a merge queue apply only if one is enabled for
`main`. See [CI batch verification](docs/ci-batch-verify.md).

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run unified-agent-audit . --fail-on high
git diff --check
```

CI also runs `make smoke`, which exercises real optional dependencies in a
temporary state directory. It does not replace pytest. To run the whole CI
parity set in one command, use [`scripts/local-gate.sh`](scripts/local-gate.sh).

The suite has about 9,000 tests. Run the focused test first while you work,
and the full suite before you ask for review:

```bash
uv run pytest tests/test_safety.py -q              # one file
uv run pytest tests/test_safety.py -k dotdot -q    # one keyword
uv run pytest -q tests/e2e                         # product scenarios
uv run pytest tests/connectors -q                  # connector conformance
```

For a JavaScript change, run the tests inside the package you changed:

```bash
cd packages/<package> && npm test
```

The protocol package also has `npm run codegen:check`, which fails when its
generated files are out of date. The web package also has `npm run typecheck`
and `npm run test:a11y` (an accessibility gate that needs Playwright
Chromium).

### Embedding tests skip on some machines

About two dozen tests carry the `embeddings` marker. They need the fastembed
model, which is downloaded on first use. If the model cannot be acquired (a
fresh clone with no network, a locked-down runner), those tests **skip with an
explicit reason** instead of failing. They only skip for a model download or
cache failure. Any other failure in the embedding code still fails the test,
by design. Do not widen that skip. The probe lives in
[`tests/conftest.py`](tests/conftest.py).

## Branches, commits, and pull requests

- Branch from `main`. Use a short, descriptive branch name.
- Write [Conventional Commits](https://www.conventionalcommits.org/). The
  history uses `feat(scope): ...`, `fix(scope): ...`, `docs: ...`, and
  `test: ...`. Example: `fix(config): report durable commit outcome on
  fanout failure`.
- Keep pull requests small and focused. One behavior change per pull request.
  Do not mix a refactor with a fix.
- Write the test first for a behavior change. Include one case that must
  pass and one that must be rejected when you touch a boundary.
- Update the documentation in the same pull request. Docs are tested: see
  [`tests/test_docs_tool_count.py`](tests/test_docs_tool_count.py) and
  [`tests/test_docs_self_description.py`](tests/test_docs_self_description.py).
  They fail when a registered tool has no reference entry or when a
  documented count drifts from the code.
- Add a changelog fragment: one file `changelog.d/<task-or-pr-id>.<type>.md`
  (type is `added`, `changed`, `fixed`, `security`, `removed` or `deprecated`)
  holding the bullet as it should read in [CHANGELOG.md](CHANGELOG.md), for
  example `- **Title (#1234).** What changed for a user.` Describe the effect on
  a user, not the diff. Do not edit `CHANGELOG.md` for a new change: fragment
  names are unique per task, so a batch of merged PRs never conflicts on it,
  and `scripts/changelog_assemble.py` folds the fragments under
  `## [Unreleased]` at release time. Run
  `python scripts/changelog_assemble.py --check` to validate yours. See
  [changelog.d/README.md](changelog.d/README.md). A direct `CHANGELOG.md` edit
  is still accepted for work already in flight. The file follows
  [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
- Describe what changed, why, and how you verified it in the pull request
  body. Say which checks you ran.
- Do not commit secrets, tokens, `service.env`, or local state.

## Change a safety boundary

Path resolvers, the command allowlist, the HTTP authentication split, the
approval gate, and the audit rules are product behavior. Read the resolver,
its callers, and its tests before you edit. The rules are in
[Change a safety boundary](docs/developer-guide.md#change-a-safety-boundary)
and the design is in the [security model](docs/security.md). In short:

- Resolve symlinks before you check containment.
- Keep vault writes inside the configured write directories.
- Read tools may reach a project checkout. Mutating tools must not.
- Run commands with `shell=False`.
- Keep the public-path and authenticated-path split in HTTP code.
- Give each audit rule one clear owner.

Add a regression test for an allowed case and a rejected case. A change that
widens a sandbox, removes a gate, or adds a new way to reach the host needs an
explicit reason in the pull request description.

## Add a tool

Follow [Add or change a tool](docs/developer-guide.md#add-or-change-a-tool).
A tool that is not in the registration tuple in
[`server.py`](src/unified_agent/server.py) is not available. Document it in
[`docs/tools-reference.md`](docs/tools-reference.md) and run
`uv run pytest -q tests/test_docs_tool_count.py`.

## Documentation style

Follow [Writing and checking documentation](docs/README.md#writing-and-checking-documentation).

- Write short, direct sentences.
- Define a term before its first use.
- Link to the source file behind a design claim.
- Mark a proposal as a proposal. Do not turn a dated assessment into a
  current fact.
- Do not hand-edit the diagrams in `docs/assets/`. Edit
  [`scripts/render_doc_diagrams.py`](scripts/render_doc_diagrams.py) and run it.

After a documentation change, run:

```bash
uv run pytest -q tests/test_docs_tool_count.py tests/test_docs_self_description.py
uv run ruff check .
uv run unified-agent-audit . --fail-on high
git diff --check
```

## Questions and review

Ask questions in a GitHub issue or on the pull request. A maintainer reviews
every pull request. Expect requests for tests, documentation, or a smaller
scope. A reviewer must approve before merge. Every merge requires green
exact-head CI or a verified green batch containing that head; ordinary pull
requests use review and targeted tests while waiting for that release gate
([CI batch verification](docs/ci-batch-verify.md)).
Nobody merges their own change without review. This applies to maintainers
too.

Some documents in `docs/` (such as [COLLABORATION.md](docs/COLLABORATION.md))
describe how the maintainers coordinate their own fleet of agents. You do not
need to follow them to contribute.

## AI agents

If an AI agent prepares your change, you are responsible for it. Read the
diff, run the checks above, and do not let an agent weaken a test, skip a
hook (`--no-verify`), or widen a sandbox to make a change pass.

## Licensing

The project is currently proprietary (see [LICENSE](LICENSE)); no license is
granted to the code except by written agreement. A public license is under
review (docs/public-release/licensing.md).
By submitting a contribution you agree that the maintainers may distribute it
under the license they publish for the project. Third-party licenses are
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
