# `@unified-agent/conformance`

Cross-runtime golden fixtures for the Unified Agent policy boundary.

The Python runtime remains the canonical policy implementation. This package
parses shared fixtures and checks that TypeScript consumers preserve the exact
decision shape; it does not reimplement path, shell, profile, approval, or
storage policy.

The first fixture suites cover workspace containment / symlink escapes and
shell-policy decisions:

```bash
npm ci
npm test
npm run typecheck
npm run build
```

Python reference tests from the repository root:

```bash
uv run pytest -q tests/test_conformance_path.py tests/test_conformance_shell.py
```
