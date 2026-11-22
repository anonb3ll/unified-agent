#!/usr/bin/env bash
# Common timer entrypoint for systemd and the Mac LaunchAgent wrapper.
set -euo pipefail

ROOT="${UA_CODE_ROOT:-${HOME}/dev/unified_agent}"

# Publish START before environment setup, which can fail under set -e.
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
run_id=$("${ROOT}/.venv/bin/python" -c \
  'from unified_agent.scheduled_status import begin_scheduled_run; print(begin_scheduled_run("sleep-pass"))')

# Resolve through the same UA_HOME-aware path helper doctor uses, including ~.
ENV_FILE=$("${ROOT}/.venv/bin/python" -c \
  'from unified_agent import config; print(config.data_home() / "sleep-pass.env")')

if [[ -r "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export UA_SLEEP_PASS_RUN_ID="$run_id"
exec "${ROOT}/.venv/bin/unified-agent-sleep-pass" --scheduled
