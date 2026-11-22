# Building and checking a self-contained hub package

The release wheel carries the Control Center build and capability catalog under
`unified_agent/_resources`. An installed hub does not need Node, a source
checkout, or `UA_WEB_DIST_DIR` to serve them. `UA_WEB_DIST_DIR` still selects a
deliberate custom UI without changing the packaged catalog.

Release builders need Node and npm to build the existing frontend. Build in an
isolated product worktree, using the repository's supported build toolchain and
locks, then create the Python artifacts:

```sh
npm --prefix packages/protocol ci
npm --prefix packages/protocol run build
npm --prefix packages/client ci
npm --prefix packages/client run build
npm --prefix packages/web ci
npm --prefix packages/web run build
UA_RELEASE_BUILD=1 uv build
uv run python scripts/check_wheel_resources.py dist/unified_agent-<version>-py3-none-any.whl
```

With `UA_RELEASE_BUILD=1` the hook refuses an incomplete UI/catalog release.
Without it (an ordinary `uv tool install .`, `install.sh` or `git+` install of
an unbuilt tree) the hook warns and builds a wheel without `_resources`, whose
`/ui` answers 503 with a build hint; symlinked resources are refused in both
modes. It does not run npm, access
providers, initialize state, or read operator settings during a package build.
The sdist embeds the same prepared assets, so building its wheel requires only
Python/build dependencies. Editable development installs and the current Docker
editable builder retain their source-tree behavior; source UI builds remain the
developer's step. An unbuilt editable checkout still reports the existing 503.

The artifact checker extracts the actual wheel to an unrelated temporary path,
imports product code from that wheel, uses disposable state and a synthetic
bearer, and checks `/ui`, its referenced assets, authenticated catalog discovery,
and rejection of missing/incorrect bearers. It reuses the invoking interpreter's
dependencies. It does not prove a cold resolver install, browser rendering,
Docker execution, or every supported OS/Python version.

For a release, also check wheel rebuilding from the sdist with no frontend source
or Node step, compare normalized repeat builds, run the required exact-head CI
and review, and record source SHA, wheel/sdist/UI/catalog hashes and dependency
evidence. The dependency repair in PR #972 is now merged and integrated; retain npm audit evidence
for the rebuilt release rather than treating asset inclusion as dependency proof. Installed-artifact evidence
must supplement the existing checkout-based Install Gate.

Service templates, multi-human enrollment and SDK publication are separate
adoption requirements tracked in [the audit disposition](adopter-audit-disposition.md).
No artifact created by this change is published automatically.
