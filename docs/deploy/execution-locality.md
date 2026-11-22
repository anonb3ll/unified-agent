# Execution locality: the cloud hub coordinates, devices execute

Task #2109, epic #2107. A hub with `UA_EXECUTION_LOCALITY=devices` never
starts an agent in its own process tree. Work goes to an enrolled device through
the ua-node queue. The cloud control plane on Railway runs this way.

## Setting

| Value | When | Effect |
|---|---|---|
| `local` | Default on an ordinary host | Unchanged behaviour on standalone hosts. On a hosted platform startup refuses this value and other processes fail closed to devices |
| `devices` | Default when `RAILWAY_ENVIRONMENT_ID` is set or `UA_HOSTED=1`; or set explicitly | Every surface below refuses or routes to a device |
| anything else | — | Fails closed to `devices`. The hub refuses to start, and doctor reports MISCONFIGURED |

A hosted platform cannot choose `local`: startup refuses it and doctor reports
MISCONFIGURED. Standalone hosts retain the `local` setting and default.

## What `devices` changes

| Surface | Behaviour |
|---|---|
| Control Center dispatch (`/api/action/dispatch`, `dispatch-stack`) | The click routes to the agent's ua-node device. With no device registered for that agent, the run goes to `retryable` with `no_device_for_agent:<label>` and nothing starts on the hub |
| Scheduler ticks, `dispatch_resume` | With `UA_DISPATCH_REAL_SPAWN=1`: the same node-only boundary, with no local fallback. Without it: the stub, so an unattended tick starts no work anywhere (the opt-in is kept). An offline or backend-less node still fails closed (`node_offline`, `node_backend_unavailable`) |
| `CliProcessSpawner`, the detached dispatch supervisor | Refuse before any process starts (`server_execution_refused`) |
| `pi_run_task` | ToolError |
| `llm_client` `claude_cli` leg | Absent. Completions use the HTTP legs (OpenRouter, AI Studio, the subscription pool), and doctor reads the leg as unconfigured |
| `gemini_ask` with `UA_PREFER_AGY_CLI=1` | The agy CLI is skipped and the API key path is used |
| `run_cmd` / `run_cmd_check`, and the workflow steps built on them | Refused whole. A program allowlist cannot hold: a wrapper (`env`, `timeout`, `nohup`), an interpreter, or a repository-configured git helper (`core.fsmonitor`, run by a plain `git status`) each starts a child that the hub's audit hook cannot see |
| Git repository operations | Refused by the Git tool and process backstop, including other hub Git consumers. Existing repository configuration can execute fsmonitor, hooks, filters, diff/textconv, signing or helpers; preventing new `.git/config` writes is insufficient |
| `python_lint_and_format` | Refused: a writable working directory can shadow the installed `ruff` Python module |
| Project-code runners: `red_team_qa`, the merge test gate, bug-worker gates, `session_end` checks | Refused (or skipped, for `session_end`). They execute the project's own code, which can start anything |
| `unified-agent-executor`, `unified-agent node` | Refuse to start; run them on the device |

LangGraph runs (`langgraph_run`, `langgraph_workflow_run`) stay allowed. They
execute in-process with a deterministic planner, and they start no model
session and no agent process.

## Backstop: the process guard

The guard covers launches **from the hub's own process**. It cannot see a
child's own exec: `env claude` passes the hook as `env`. Descendants are
covered by refusing every surface that runs caller- or project-supplied code
(above) and by an image that ships no harness binaries. Kernel-level
confinement of descendants (seccomp, Landlock) is not provided.


At start, a devices-only hub installs a `sys.addaudithook` hook. It refuses any
`subprocess.Popen`, `os.system`, `os.exec*` or `os.posix_spawn` whose program is a known
agent harness (`claude`, `codex`, `agy`, `gemini`, `cursor-agent`, `opencode`,
`hermes`, `pi`, `copilot`, `devin`, ...), a shell `-c` running one, or a
unified-agent agent module started with `python -m` (the dispatch supervisor,
the executor, the node). It also refuses Git, including a shell command starting
Git, because configured helpers execute outside the hook. Python module detection
parses interpreter options and stops at a script, `-c`, or `--`; ordinary `-m`
arguments to other programs are data. A module name in a `gh` comment body is
also data. Git commands are refused for their helper risk regardless of arguments.
An audit hook cannot be removed. A launch path added later that misses the
explicit checks above is still refused.

`tests/test_execution_locality.py::TestSpawnSiteInventory` is the review
tripwire. Every module that spawns a process is listed with the reason it is
safe, and a new one fails the test until someone classifies it.

## Doctor

The `execution_locality` row reports:
- LIVE for devices-only, naming any harness binaries present on PATH, all of which are refused;
- MISCONFIGURED for a hosted platform configured with `local`;
- DORMANT for an ordinary local host;
- MISCONFIGURED for an unrecognised value.
