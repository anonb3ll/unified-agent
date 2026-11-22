- **Vulnerable dependencies upgraded (repo review 2026-11-07).** pip-audit
  reported 76 advisories against the lock: pillow 12.3.0 → 13.0.0 (via
  fastembed 0.9.0 → 0.9.1, which lifts its `pillow<13` cap), pypdf 6.20.1 →
  6.21.4, filelock 4.1.1 → 4.2.9, pyasn1 0.6.4 → 0.6.5 and certifi →
  2026.10.23. The `pdf` extra and dev group now floor pypdf at 6.21.4 so an
  install that ignores `uv.lock` cannot pick up a vulnerable release. npm
  lockfiles: zod 4.6.7 in `packages/conformance`, undici in `packages/web`.
