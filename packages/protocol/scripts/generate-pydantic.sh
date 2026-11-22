#!/usr/bin/env bash
# Generate Pydantic v2 models from exported JSON Schema.
# Requires: uv (for uvx datamodel-code-generator).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMA_DIR="$ROOT/generated/json-schema"
COMBINED="$ROOT/generated/unified-agent-protocol.schema.json"
OUT_DIR="$ROOT/generated/python"
OUT_FILE="$OUT_DIR/models.py"
# Pin emitters so `npm run codegen:check` is reproducible across hosts (#929/#934).
# Never inherit a parent pyproject.toml / ruff.toml — hub vs standalone trees
# otherwise format models.py differently (Hermes review blocker).
DATAMODEL_CODEGEN_VERSION="0.30.2"
RUFF_VERSION="0.15.21"
# The emitter is pinned, but uvx would still resolve it on this host's default
# interpreter — and datamodel-code-generator 0.30.2 evaluates its default target
# version at import time:
#   DEFAULT_TARGET_PYTHON_VERSION = PythonVersion(f"{sys.version_info.major}.{sys.version_info.minor}")
# On a Python 3.14 host that raises ValueError before --target-python-version
# below is ever parsed, so codegen cannot run at all. Pin the tool interpreter so
# `npm run codegen` / `codegen:check` behave the same on every host.
DATAMODEL_CODEGEN_PYTHON="3.12"
# Match unified_agent [tool.ruff] line-length so mirrored models.py also
# passes the hub's repo-wide `uv run ruff format --check .` (CI test job).
RUFF_LINE_LENGTH="100"
# Keep generated Python Enum classes as part of the public SDK surface. Fix
# datamodel-codegen's shared-enum default leakage with a targeted post-process
# below so callers keep the previous enum imports and model field values.

if [[ ! -d "$SCHEMA_DIR" ]]; then
  echo "missing $SCHEMA_DIR — run: npm run export:json-schema" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

python3 - <<'PY' "$SCHEMA_DIR" "$COMBINED"
import json
import sys
from pathlib import Path

schema_dir = Path(sys.argv[1])
out = Path(sys.argv[2])
defs = {}
for path in sorted(schema_dir.glob("*.json")):
    if path.name == "index.json":
        continue
    defs[path.stem] = json.loads(path.read_text())

doc = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "UnifiedAgentProtocol",
    "type": "object",
    "definitions": defs,
    "properties": {name: {"$ref": f"#/definitions/{name}"} for name in defs},
}
out.write_text(json.dumps(doc, indent=2) + "\n")
PY

# env -u PYTHONPATH -u PYTHONHOME: a host that exports a foreign site-packages
# (a tool-managed venv for another interpreter) otherwise shadows the pinned tool
# env — e.g. pydantic_core built for 3.14 inside a 3.12 env raises
# ModuleNotFoundError: pydantic_core._pydantic_core. The emitter env is hermetic.
env -u PYTHONPATH -u PYTHONHOME \
  uvx --python "${DATAMODEL_CODEGEN_PYTHON}" \
  --from "datamodel-code-generator[http]==${DATAMODEL_CODEGEN_VERSION}" datamodel-codegen \
  --input "$COMBINED" \
  --input-file-type jsonschema \
  --output "$OUT_FILE" \
  --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.11 \
  --use-standard-collections \
  --use-union-operator \
  --strict-nullable \
  --collapse-root-models \
  --disable-timestamp

# datamodel-codegen deduplicates structurally-identical inline enums. The
# Task read model's legitimate priority default (`med`) can leak onto the
# update request, whose schema has no default. The create schema now declares
# its own default; normalize that literal to its Enum member for clean
# Pydantic serialization. Correct only these two generated fields.
python3 - "$OUT_FILE" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
source = path.read_text()
class_pattern = re.compile(
    r"(^class (?:TaskCreateRequest|TaskUpdateRequest)\(BaseModel\):\n)(.*?)(?=^class |\Z)",
    re.MULTILINE | re.DOTALL,
)
patched = set()

def fix_patch_model(match: re.Match[str]) -> str:
    block = match.group(0)
    class_name = re.match(r"^class (\w+)", block, re.MULTILINE).group(1)
    replacement = "Priority.med" if class_name == "TaskCreateRequest" else "None"
    block, count = re.subn(
        rf"(^    priority: .+?) = ['\"]med['\"]$",
        rf"\1 = {replacement}",
        block,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit(f"expected one leaked priority default in {class_name}")
    patched.add(class_name)
    return block

source = class_pattern.sub(fix_patch_model, source)
expected = {"TaskCreateRequest", "TaskUpdateRequest"}
if patched != expected:
    raise SystemExit(f"patched {sorted(patched)}, expected {sorted(expected)}")
path.write_text(source)
PY

cat > "$OUT_DIR/__init__.py" <<'PY'
"""Generated Pydantic models for @unified-agent/protocol.

Do not edit by hand. Regenerate via:
  cd packages/protocol && npm run codegen
"""

from .models import *  # noqa: F403
PY

# The generator may emit RootModel even when collapsed schemas do not use it.
# Keep generated artifacts reproducible and clean under the hub's Ruff gate.
uvx --from "ruff==${RUFF_VERSION}" ruff check \
  --isolated --select F401 --fix --quiet "$OUT_FILE"

# Format with a pinned Ruff, isolated from any ambient project config.
uvx --from "ruff==${RUFF_VERSION}" ruff format \
  --isolated \
  --line-length="${RUFF_LINE_LENGTH}" \
  --quiet \
  "$OUT_FILE" "$OUT_DIR/__init__.py"

# Keep the hand-maintained model-fixture test ruff-clean on every codegen run
# (task #1314 — stops the #27-#29 style-only PR churn on this file).
TEST_FIXTURE="$ROOT/tests/test_generated_models.py"
if [[ -f "$TEST_FIXTURE" ]]; then
  # Apply auto-fixes (import order) then format; fail if anything remains.
  uvx --from "ruff==${RUFF_VERSION}" ruff check \
    --select I001 --fix --quiet "$TEST_FIXTURE" || true
  uvx --from "ruff==${RUFF_VERSION}" ruff format \
    --line-length="${RUFF_LINE_LENGTH}" \
    --quiet \
    "$TEST_FIXTURE"
  uvx --from "ruff==${RUFF_VERSION}" ruff check \
    --select I001,E,F,W --quiet \
    "$TEST_FIXTURE"
  uvx --from "ruff==${RUFF_VERSION}" ruff format \
    --check \
    --line-length="${RUFF_LINE_LENGTH}" \
    --quiet \
    "$TEST_FIXTURE"
fi

echo "wrote $OUT_FILE"
