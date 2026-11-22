#!/usr/bin/env bash
# Sync per-agent unified-agent hub token files from UA_AGENT_TOKENS in service.env.
# Does not print token values. Run after hub token rotation or Mac migration.
set -euo pipefail

SERVICE_ENV="${UA_SERVICE_ENV:-$HOME/.unified_agent/service.env}"

if [[ ! -r "$SERVICE_ENV" ]]; then
  echo "sync-agent-tokens: missing $SERVICE_ENV" >&2
  exit 1
fi

python3 - "$SERVICE_ENV" <<'PY'
import os
import re
import sys
from pathlib import Path

service_env = Path(sys.argv[1])
text = service_env.read_text()
m = re.search(r'^UA_AGENT_TOKENS=(.*)$', text, re.M)
if not m:
    raise SystemExit("UA_AGENT_TOKENS not found in service.env")

def parse_token_segments(raw: str) -> list[str]:
    """Handle env-file quoting broken by mint-server-tokens appending outside quotes."""
    raw = raw.strip()
    if not raw.startswith('"'):
        return [raw]
    boundary = raw.find('",')
    if boundary != -1:
        segments = [raw[1:boundary]]
        rest = raw[boundary + 2 :].lstrip()
        if rest:
            segments.append(rest)
        return segments
    if raw.endswith('"'):
        return [raw[1:-1]]
    return [raw[1:]]

tokens: dict[str, str] = {}
for segment in parse_token_segments(m.group(1)):
    for part in segment.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        label, tok = part.split(":", 1)
        label = label.strip()
        tok = tok.strip()
        if tok.startswith("ua_"):
            tokens[label] = tok

targets = {
    "Desktop_Agent4": Path.home() / ".cursor" / "unified-agent-token",
    "Desktop_Agent6": Path.home() / ".copilot" / "unified-agent-token",
    "Desktop_Agent2": Path.home() / ".codex" / "unified-agent-token",
    "Desktop_Agent1": Path.home() / ".claude" / "unified-agent-token",
    "Desktop_agent3": Path.home() / ".gemini" / "unified-agent-token",
    "Desktop_Agent5": Path.home() / ".hermes" / "unified-agent-token",
    "Desktop_Agent7": Path.home() / ".devin" / "unified-agent-token",
    "Desktop_agent12": Path.home() / ".opencode" / "unified-agent-token",
}

updated = 0
for label, path in targets.items():
    tok = tokens.get(label)
    if not tok:
        print(f"skip (no token in service.env): {label}")
        continue
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text().strip() if path.exists() else ""
    if current == tok:
        print(f"ok (unchanged): {label} -> {path} len={len(tok)}")
        continue
    path.write_text(tok + "\n")
    os.chmod(path, 0o600)
    print(f"updated: {label} -> {path} len={len(tok)}")
    updated += 1

print(f"done: {updated} file(s) updated")
PY
