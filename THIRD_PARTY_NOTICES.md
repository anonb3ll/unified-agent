# Third-party notices

unified-agent depends on open-source packages that keep their own licenses.
This file records the ones whose terms need attention when the software is
redistributed. It was produced from installed package metadata
(`.venv/**/METADATA`, `packages/*/node_modules/**/package.json`) on
2026-10-01; regenerate it before each release.

## Python

All direct runtime dependencies are under permissive licenses (MIT, BSD,
Apache-2.0, ISC, PSF). Items that need a note:

| Package | License | Note |
|---|---|---|
| `fpdf2` (optional `pdf` extra) | LGPL-3.0-only | Used unmodified as a separately installed library, so the LGPL's relinking terms are met by keeping it a normal, replaceable dependency. Do not vendor or statically bundle it. |
| `tld` (via trafilatura → courlan) | MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later | Used under the MPL-1.1 option. |
| `certifi`, `orjson`, `tqdm` | MPL-2.0 (with MIT/Apache for parts) | File-level copyleft; unmodified use only. |
| `fastembed` | Apache-2.0 | Its package classifier says "Other/Proprietary"; the upstream repository license is Apache-2.0. The ONNX model it downloads at runtime (`BAAI/bge-small-en-v1.5`, MIT) is not redistributed. |

## JavaScript (Control Center, `packages/web`)

| Package | License | Note |
|---|---|---|
| `caniuse-lite` | CC-BY-4.0 | Browser-support data, attribution: Alexis Deveria, caniuse.com. |
| MPL-2.0 packages (5) | MPL-2.0 | File-level copyleft; unmodified use only. |
| `khroma` | MIT (upstream) | Its package.json has no license field. |
| `@fontsource-variable/geist`, `@fontsource-variable/geist-mono` | OFL-1.1 | Geist fonts by Vercel, bundled as woff2 in the built UI. The OFL permits redistribution with the software; the fonts are not sold on their own and keep their name. Complete copyright notices and licenses ship in the web distribution at `/licenses/geist-OFL.txt` and `/licenses/geist-mono-OFL.txt` (sources in `packages/web/public/licenses`). |

No GPL or AGPL code is used at runtime in either ecosystem.
