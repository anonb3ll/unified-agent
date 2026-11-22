"""Canonical named-agent / profile routing for approved dispatch (task #549/#568).

Resolves roster labels and interactive ``/agentname`` aliases to that agent's
native headless CLI, dispatch profile, and execution identity. Unknown or
ambiguous inputs fail closed — free-form slash text never becomes a launch
target, and Pi is never used as a stand-in for a missing persona.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..profiles import CTO, DEVELOPER, default_for_identity
from . import harness_sandbox

# Native headless CLI per developer family. Desktop_*/Laptop_* (PascalCase
# family) and Mac-local server_* (lowercase family) are dispatchable;
# roster-only identities (the operator, pi, pi_worker) stay valid for
# delegation/auth but are not spawn targets.
_HOSTS = ("Desktop", "Laptop")
_CLI_BY_FAMILY = {
    "Claude": "claude",
    "Codex": "codex",
    "agy": "agy",
    "Cursor": "cursor",
    "Hermes": "hermes",
    "Copilot": "copilot",
}
# Label segment per family lives in config so construction here and family
# resolution elsewhere share one source. Identity in the product; the public
# export's scrub neutralises the values. Aliases, binaries and roster keys keep
# using the family name, so neutralising a label never renames a CLI or a bare
# interactive stem.
_LABEL_BY_FAMILY = config.LABEL_BY_FAMILY
_SEGMENT_TO_FAMILY = config.SEGMENT_TO_FAMILY
_SEGMENT_TO_FAMILY_LOWER = config.SEGMENT_TO_FAMILY_LOWER
_EXTRA_ROUTES = {"Desktop_Agent7": ("Desktop", "Devin", "devin")}
# Mac hub executors use server_<family> (lowercase); see migration findings.
_SERVER_FAMILY_BY_TOKEN = {
    "claude": "Claude",
    "codex": "Codex",
    "agy": "agy",
    "cursor": "Cursor",
    "hermes": "Hermes",
}
DISPATCHABLE_LABELS: frozenset[str] = frozenset(
    {f"{host}_{_LABEL_BY_FAMILY[family]}" for host in _HOSTS for family in _CLI_BY_FAMILY}
    | {
        f"server_{_LABEL_BY_FAMILY[_SERVER_FAMILY_BY_TOKEN[token]].lower()}"
        for token in _SERVER_FAMILY_BY_TOKEN
    }
    | set(_EXTRA_ROUTES)
)

_BINARY_ENV = {
    "claude": "UA_CLAUDE_BIN",
    "codex": "UA_CODEX_BIN",
    "agy": "UA_AGY_BIN",
    "cursor": "UA_CURSOR_BIN",
    "hermes": "UA_HERMES_BIN",
    "copilot": "UA_COPILOT_BIN",
    "devin": "UA_DEVIN_BIN",
}

# Cursor defaults to agent-with-hub so the child inherits Desktop_Agent4's
# hub token rather than whatever UA_AGENT_BEARER_TOKEN the parent shell has.
# Copilot defaults to copilot-with-hub for the same isolation as Desktop_Agent6.
# Devin defaults to devin-with-hub for the same isolation as Desktop_Agent7
# (defense-in-depth: today mcp_config.json bakes a static token rather than
# interpolating UA_AGENT_BEARER_TOKEN, but the wrapper keeps that true even if
# it is ever regenerated to match Cursor's/Copilot's env-interpolated config).
_DEFAULT_BIN = {
    "claude": "claude",
    "codex": "codex",
    "agy": "agy",
    "cursor": "agent-with-hub",
    "hermes": "hermes",
    "copilot": "copilot-with-hub",
    "devin": "devin-with-hub",
}

DEVIN_PERMISSION_MODES = ("auto", "accept-edits", "smart", "dangerous")
DEFAULT_DEVIN_PERMISSION_MODE = "accept-edits"


def resolve_devin_permission_mode(value: str | None = None) -> str:
    """Validate Devin's explicit permission posture, defaulting to edits only."""
    mode = (
        value or os.environ.get("UA_DEVIN_PERMISSION_MODE") or DEFAULT_DEVIN_PERMISSION_MODE
    ).strip()
    if mode not in DEVIN_PERMISSION_MODES:
        expected = ", ".join(DEVIN_PERMISSION_MODES)
        raise ValueError(f"invalid Devin permission mode {mode!r}; expected one of {expected}")
    return mode


# Short interactive stems that map to more than one dispatchable label. These
# are CLI tokens, not label segments: a bare ``/claude`` stays valid even when
# the export neutralises the labels to ``Desktop_Agent1``. ``_STEM_TO_SEGMENT``
# bridges the two so the ambiguity probe matches labels by their segment.
_AMBIGUOUS_STEMS = frozenset(_CLI_BY_FAMILY.values())
_STEM_TO_SEGMENT = {
    _CLI_BY_FAMILY[family].lower(): _LABEL_BY_FAMILY[family].lower() for family in _CLI_BY_FAMILY
}

_SECRET_FRAGMENT_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._\-+=/]+|sk-[a-z0-9]{8,}|api[_-]?key\s*[:=]\s*\S+"
    r"|authorization\s*[:=]\s*\S+|UA_AGENT_BEARER_TOKEN\s*[:=]\s*\S+)"
)

_MAX_TASK_SPEC_CHARS = 4000


@dataclass(frozen=True)
class AgentRoute:
    """Deterministic launch route for one named developer agent."""

    agent_label: str
    host: str
    family: str
    cli_backend: str
    profile: str
    binary: str
    binary_env: str
    aliases: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_label": self.agent_label,
            "host": self.host,
            "family": self.family,
            "cli_backend": self.cli_backend,
            "profile": self.profile,
            "binary": self.binary,
            "binary_env": self.binary_env,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class ResolveResult:
    ok: bool
    reason: str
    route: AgentRoute | None = None
    candidates: tuple[str, ...] = ()
    input: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "route": self.route.as_dict() if self.route else None,
            "candidates": list(self.candidates),
            "input": self.input,
        }


@dataclass(frozen=True)
class WorkspaceResolveResult:
    ok: bool
    reason: str
    workspace: Path | None = None
    binding_source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "workspace": str(self.workspace) if self.workspace else None,
            "binding_source": self.binding_source,
        }


def _split_label(label: str) -> tuple[str, str] | None:
    if "_" not in label:
        return None
    if label in _EXTRA_ROUTES:
        return _EXTRA_ROUTES[label][:2]
    host, segment = label.split("_", 1)
    if host in {"Desktop", "Laptop"}:
        family = _SEGMENT_TO_FAMILY.get(segment)
        if family is not None:
            return host, family
    if host == "server":
        family = _SEGMENT_TO_FAMILY_LOWER.get(segment)
        if family is not None:
            return host, family
    return None


def _aliases_for(label: str, host: str, family: str) -> tuple[str, ...]:
    """Unambiguous interactive forms that all map to ``label`` only."""
    lower = label.lower()
    hyphen = f"{host}-{family}".lower()
    slash_label = f"/{label}"
    slash_lower = f"/{lower}"
    slash_hyphen = f"/{hyphen}"
    # Host-qualified short forms remain unambiguous (Desktop_Agent1 ≠ Laptop_Agent1).
    host_family = f"{host.lower()}_{family.lower()}"
    return tuple(
        dict.fromkeys(
            (
                label,
                lower,
                hyphen,
                host_family,
                slash_label,
                slash_lower,
                slash_hyphen,
                f"/{host_family}",
            )
        )
    )


def _binary_for(cli_backend: str) -> tuple[str, str]:
    env_key = _BINARY_ENV[cli_backend]
    binary = (os.environ.get(env_key) or _DEFAULT_BIN[cli_backend]).strip() or _DEFAULT_BIN[
        cli_backend
    ]
    return binary, env_key


# Profiles a dispatch route may carry. A route runs with the label's own
# default when that default is one of these (the CTO seat keeps its CTO
# surface and skills under dispatch, task #1884); any other default, such as
# chief-of-staff, is never carried into an unattended launch and the route
# stays developer.
_DISPATCH_PROFILES = frozenset({DEVELOPER, CTO})


def _route_profile(agent_label: str) -> str:
    profile = default_for_identity(agent_label)
    return profile if profile in _DISPATCH_PROFILES else DEVELOPER


def build_route(agent_label: str) -> AgentRoute | None:
    """Build the canonical route for an exact dispatchable label, or None."""
    if agent_label not in DISPATCHABLE_LABELS:
        return None
    parts = _split_label(agent_label)
    if parts is None:
        return None
    host, family = parts
    cli_backend = _CLI_BY_FAMILY.get(family) or _EXTRA_ROUTES[agent_label][2]
    binary, env_key = _binary_for(cli_backend)
    return AgentRoute(
        agent_label=agent_label,
        host=host,
        family=family,
        cli_backend=cli_backend,
        profile=_route_profile(agent_label),
        binary=binary,
        binary_env=env_key,
        aliases=_aliases_for(agent_label, host, family),
    )


def all_routes() -> dict[str, AgentRoute]:
    """Every supported dispatch route, keyed by canonical label."""
    return {label: route for label in sorted(DISPATCHABLE_LABELS) if (route := build_route(label))}


def scheduler_runtime_state() -> dict[str, Any]:
    """Describe the retired tick from the live unit probe and legacy gates."""
    from .. import monitor

    props = monitor._service_probe("ua-dispatch-tick.timer", monitor.SERVICE_CHECK_TIMEOUT_S)
    if props is None:
        return {"enabled": False, "real_spawn": False, "status": "unknown", "mode": "unknown"}
    if props.get("LoadState") == "not-found":
        if (
            sys.platform != "darwin"
            or monitor._launchd_disabled_override(
                "com.unified-agent.dispatch-tick", monitor.SERVICE_CHECK_TIMEOUT_S
            )
            is True
        ):
            return {"enabled": False, "real_spawn": False, "status": "retired", "mode": "retired"}
        return {"enabled": False, "real_spawn": False, "status": "unknown", "mode": "unknown"}
    if props.get("LoadState") == "loaded":
        active_state = props.get("ActiveState", "unknown")
        if active_state not in {"active", "inactive"}:
            active_state = "unknown"
        # A loaded Mac periodic LaunchAgent can fire again after a failed
        # tick; a systemd timer with inactive state has no next firing.
        scheduled = sys.platform == "darwin" or active_state == "active"
        enabled = scheduled and config.dispatch_scheduler_enabled()
        real_spawn = enabled and config.dispatch_real_spawn()
        return {
            "enabled": enabled,
            "real_spawn": real_spawn,
            "status": f"misconfigured_{active_state}",
            "mode": "native_cli" if real_spawn else "stub" if enabled else "disabled",
        }
    return {"enabled": False, "real_spawn": False, "status": "unknown", "mode": "unknown"}


def route_inventory(scheduler_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return a secret-free, read-only preflight view of native dispatch routes."""
    scheduler_state = scheduler_state or scheduler_runtime_state()
    rows: list[dict[str, Any]] = []
    for route in all_routes().values():
        workspace = resolve_workspace_for_route(route)
        binary_name = Path(route.binary).name or route.binary
        binary_available = bool(shutil.which(route.binary))
        if workspace.ok and binary_available:
            status = "ready"
        elif not workspace.ok:
            status = "forbidden"
        else:
            status = "unavailable"
        rows.append(
            {
                "agent_label": route.agent_label,
                "aliases": list(route.aliases),
                "backend": route.cli_backend,
                "binary": binary_name,
                "binary_available": binary_available,
                "workspace": workspace.as_dict(),
                "profile": route.profile,
                "approval_boundary": "governed_dispatch",
                "status": status,
                "click_spawn": {
                    "enabled": True,
                    "mode": "native_cli",
                    "next_action": "dispatch through Dispatch Studio",
                },
                "scheduler_spawn": {
                    "enabled": scheduler_state["enabled"],
                    "mode": scheduler_state["mode"],
                    "next_action": (
                        "retired timer loaded; disable it"
                        if scheduler_state["status"].startswith("misconfigured_")
                        else "scheduler liveness unknown"
                        if scheduler_state["status"] == "unknown"
                        else "scheduler retired by #1435"
                    ),
                },
            }
        )
    return rows


def _normalize_token(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    # Strip a single leading slash used by interactive aliases.
    if text.startswith("/"):
        text = text[1:]
    text = text.strip()
    # Collapse separators so Desktop-Agent1 / Desktop_Agent1 / Desktop Agent1 match.
    text = re.sub(r"[\s\-]+", "_", text)
    return text


def resolve_named_agent(raw: str) -> ResolveResult:
    """Resolve a label or interactive alias to one AgentRoute (fail closed)."""
    original = raw if isinstance(raw, str) else ""
    if not isinstance(raw, str) or not raw.strip():
        return ResolveResult(ok=False, reason="malformed_route", input=original or None)

    token = _normalize_token(raw)
    if not token:
        return ResolveResult(ok=False, reason="malformed_route", input=original)

    # Exact (case-sensitive) dispatchable label wins immediately.
    if token in DISPATCHABLE_LABELS:
        route = build_route(token)
        assert route is not None
        return ResolveResult(ok=True, reason="ok", route=route, input=original)

    lower = token.lower()

    # Bare family stems (/claude, codex, agy) are ambiguous across hosts.
    if lower in _AMBIGUOUS_STEMS:
        segment = _STEM_TO_SEGMENT.get(lower, lower)
        candidates = tuple(
            sorted(lbl for lbl in DISPATCHABLE_LABELS if lbl.lower().endswith(f"_{segment}"))
        )
        return ResolveResult(
            ok=False,
            reason="ambiguous_label",
            candidates=candidates,
            input=original,
        )

    # Case-insensitive / hyphen / slash alias match against each route's alias set.
    matches: list[AgentRoute] = []
    for route in all_routes().values():
        alias_set = {a.lower().lstrip("/") for a in route.aliases}
        alias_set.add(route.agent_label.lower())
        # Also accept normalized hyphen→underscore forms already applied to ``lower``.
        if lower in alias_set or lower.replace("_", "-") in {
            a.replace("_", "-") for a in alias_set
        }:
            matches.append(route)

    # Deduplicate by label while preserving order.
    seen: set[str] = set()
    uniq: list[AgentRoute] = []
    for route in matches:
        if route.agent_label not in seen:
            seen.add(route.agent_label)
            uniq.append(route)

    if len(uniq) == 1:
        return ResolveResult(ok=True, reason="ok", route=uniq[0], input=original)
    if len(uniq) > 1:
        return ResolveResult(
            ok=False,
            reason="ambiguous_label",
            candidates=tuple(r.agent_label for r in uniq),
            input=original,
        )

    # Known roster identity that is not a CLI dispatch target.
    roster = config.agent_labels()
    if token in roster or any(lbl.lower() == lower for lbl in roster):
        return ResolveResult(ok=False, reason="not_dispatchable", input=original)

    return ResolveResult(ok=False, reason="unknown_label", input=original)


def resolve_workspace_for_route(
    route: AgentRoute,
    *,
    expected_workspace: Path | str | None = None,
) -> WorkspaceResolveResult:
    """Bind the launch cwd/workspace for a route; fail closed on deny/mismatch.

    ``UA_AGENT_WORKSPACES=Desktop_*:DENY`` blocks hub-mediated ``fs_*`` /
    ``run_cmd`` for remote Desktop clients. Local CLI spawn on the hub host is
    different: the child still needs a cwd on *this* machine. For ``Desktop_*``
    routes only, an *explicit solitary* DENY therefore falls back to
    ``process_workspace_root()`` (``binding_source=process_default_after_deny``).
    Conflicting duplicate bindings stay fail-closed. ``Laptop_*`` / unbound
    device-scoped identities stay ``workspace_denied``.
    """
    binding = config.resolve_workspace_binding(route.agent_label)
    if isinstance(binding, config.WorkspaceDenied):
        # Only an intentional solitary DENY gets the Desktop spawn-cwd fallback.
        # Conflict fail-closed and unbound device-scoped denies stay denied.
        if route.host == "Desktop" and config.explicit_workspace_deny(route.agent_label):
            workspace = config.process_workspace_root()
            source = "process_default_after_deny"
        else:
            return WorkspaceResolveResult(
                ok=False, reason="workspace_denied", binding_source="deny"
            )
    elif binding is None:
        workspace = config.process_workspace_root()
        source = "process_default"
    else:
        workspace = binding
        source = "agent_workspaces"

    if expected_workspace is not None:
        expected = Path(expected_workspace).expanduser().resolve()
        actual = workspace.expanduser().resolve()
        if actual != expected:
            return WorkspaceResolveResult(
                ok=False,
                reason="workspace_mismatch",
                workspace=actual,
                binding_source=source,
            )

    return WorkspaceResolveResult(
        ok=True,
        reason="ok",
        workspace=workspace.expanduser(),
        binding_source=source,
    )


def bound_task_spec(notes: str | None, *, max_chars: int = _MAX_TASK_SPEC_CHARS) -> str:
    """Truncate and redact secret-like fragments from a task notes payload."""
    text = (notes or "").strip()
    if not text:
        return ""
    redacted = _SECRET_FRAGMENT_RE.sub("[REDACTED]", text)
    if len(redacted) > max_chars:
        return redacted[: max_chars - 1] + "…"
    return redacted


def codex_prompt(prompt: str) -> str:
    """A prompt safe to pass to ``codex exec`` after ``--``.

    ``--`` ends options (verified against codex-cli 0.159.0: ``codex exec --
    --flag`` takes ``--flag`` as the prompt), but a bare ``-`` still means
    "read the prompt from stdin", so it is refused rather than sent.
    """
    if prompt.strip() == "-":
        raise ValueError("prompt '-' would make codex read stdin; refusing it")
    return prompt


# A lone word after ``--`` is still dispatched by commander to a
# ``cursor-agent`` subcommand of that name (``logout``, ``update``, ...), and
# the subcommand set changes between CLI versions. Rather than track one
# version's list, refuse every single-word prompt: a real task prompt is never
# one bare word, and this holds for any installed version.
_SINGLE_WORD_RX = re.compile(r"[A-Za-z0-9][\w.:-]*")


def cursor_prompt(prompt: str) -> str:
    """A prompt safe to pass to ``cursor-agent`` as the operand after ``--``.

    ``--`` ends option parsing (commander treats everything after it as an
    operand), but a lone operand that names a subcommand is still dispatched
    to it, so any single-word prompt is refused rather than sent.
    """
    if _SINGLE_WORD_RX.fullmatch(prompt):
        raise ValueError(
            f"single-word prompt {prompt!r} could run a cursor-agent subcommand; refusing it"
        )
    return prompt


def headless_argv(
    cli_backend: str,
    binary: str,
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    skip_permissions: bool | None = None,
    dispatch_settings_path: str | Path | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    sandbox_posture: harness_sandbox.HarnessSandboxPosture | None = None,
) -> list[str]:
    """Native non-interactive argv for one persona CLI.

    The prompt is omitted from the observable (stored) form so task notes never
    leak into argv logs. ``run_id`` / identity live in the launch env, not here:
    these CLIs do not accept ``--ua-dispatch`` / ``--backend=`` aliases.

    For Claude, ``dispatch_settings_path`` is the dispatch-only fail-closed
    sandbox file. When it is set, ``--setting-sources`` is cleared so user,
    project, and local ``settings.json`` files cannot merge hostile sandbox or
    permission rules into the dispatch session; ``--dangerously-skip-permissions``
    is then attached so MCP tools work non-interactively *inside* that sandbox.
    ``UA_CLAUDE_SKIP_PERMISSIONS=1`` is the same flag: it is attached only when
    ``harness_sandbox.resolve`` confirms that file (``allowUnsandboxedCommands``
    false, sandbox forced on) and the host probe passes. Hermes ``--yolo``
    goes through the same ``require`` chokepoint and is refused while Hermes
    has no known sandbox switch. Devin ``--permission-mode dangerous`` does
    too: ``resolve("devin")`` has no sandbox switch, so the launch is refused.

    ``model`` (task #1153) appends ``--model`` for CLIs that support it
    (claude, codex, agy, cursor). Unset keeps each CLI's own default.

    Copilot, Cursor and Agy (task #1948) get their blanket-permission flags
    (``--allow-all-tools``/``--allow-all``, ``--trust``/``--force``,
    ``--dangerously-skip-permissions``) only together with the harness's own
    sandbox switch, and only when ``sandbox_posture`` is a verified posture for
    that backend (``harness_sandbox.resolve``). A launch argv (``prompt`` set)
    that would need one without it raises ``HarnessSandboxUnavailable``. The
    observable form (no prompt) is never executed and shows the sandboxed
    shape, the only one that can launch.
    """

    def _skip(env_name: str) -> bool:
        if skip_permissions is not None:
            return skip_permissions
        return os.environ.get(env_name) == "1"

    def _sandbox_for(flags: str) -> list[str]:
        if prompt is None:
            return list(harness_sandbox.planned_argv(cli_backend))
        return list(harness_sandbox.require(cli_backend, sandbox_posture, flags=flags))

    use_model = (model or "").strip() or None

    if cli_backend == "claude":
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose"]
        blanket = "--dangerously-skip-permissions"
        if dispatch_settings_path is not None:
            # Block ~/.claude and .claude/settings*.json from merging array-shaped
            # sandbox/permission rules into the dispatch session (#321).
            settings_argv = list(harness_sandbox.claude_settings_argv(dispatch_settings_path))
            # Dispatch-local only: the fail-closed file is the sandbox. The env
            # knob is the same flag and cannot skip this check.
            if skip_permissions is not False:
                posture = sandbox_posture or harness_sandbox.resolve(
                    "claude", cwd=cwd, settings_path=dispatch_settings_path
                )
                settings_argv = list(harness_sandbox.require("claude", posture, flags=blanket))
                argv.extend(settings_argv)
                argv.append(blanket)
            else:
                argv.extend(settings_argv)
        elif _skip("UA_CLAUDE_SKIP_PERMISSIONS"):
            posture = sandbox_posture or harness_sandbox.resolve(
                "claude", cwd=cwd, settings_path=None
            )
            argv.extend(harness_sandbox.require("claude", posture, flags=blanket))
            argv.append(blanket)
        if use_model:
            argv.extend(["--model", use_model])
        if prompt is not None:
            # ``--`` ends options, so a prompt that starts with ``-`` is never
            # parsed as a flag (verified against Claude Code 2.1.287 ``-p``).
            argv.extend(["--", prompt])
        return argv
    if cli_backend == "codex":
        from ..executor.backends import _resolve_codex_sandbox

        sandbox = _resolve_codex_sandbox()
        argv = [binary, "exec", "--json", "--sandbox", sandbox]
        if use_model:
            argv.extend(["--model", use_model])
        if cwd:
            argv.extend(["--cd", cwd])
        if prompt is not None:
            argv.extend(["--", codex_prompt(prompt)])
        return argv
    if cli_backend == "agy":
        # Agy's -p/--print takes the next token as the prompt (unlike Claude's
        # bare -p flag). Putting ``-p`` before ``--output-format`` makes Agy
        # treat ``--output-format`` as the prompt and exit 2 — live soak
        # run-626-aa0bdb13. Match executor.backends.AgyBackend.build_command:
        # ``[bin, -p, prompt, --output-format, stream-json]``. Observable form
        # (no prompt) omits ``-p`` entirely, same as Copilot.
        #
        # The same soak shows ``-p`` binds the next token even when it starts
        # with ``--``, so a flag-looking prompt stays a prompt (no separator
        # or refusal needed for agy).
        #
        # ``skip_permissions=True`` is the dispatch-only unattended path
        # (CliProcessSpawner). Global ``UA_AGY_SKIP_PERMISSIONS=1`` remains
        # opt-in for interactive/dev use — do not set it on the hub by default.
        #
        # Skip-permissions rides only with ``--sandbox`` and a verified posture
        # (task #1948); without it agy stays in request-review (supervised).
        argv = [binary]
        if prompt is not None:
            argv.extend(["-p", prompt])
        argv.extend(["--output-format", "stream-json"])
        if use_model:
            argv.extend(["--model", use_model])
        if _skip("UA_AGY_SKIP_PERMISSIONS"):
            argv.extend(_sandbox_for("--dangerously-skip-permissions"))
            argv.append("--dangerously-skip-permissions")
        return argv
    if cli_backend == "cursor":
        # Headless cursor needs --trust (workspace trust without a prompt). It
        # rides only with ``--sandbox enabled``, which exits 1 in print mode
        # when the sandbox cannot start (task #1948).
        #
        # --force ("Run Everything") is refused outright. cursor-agent --help
        # calls it "Force allow commands unless explicitly denied" and aliases
        # ``--yolo`` to it. ``--sandbox`` is a separate enabled|disabled
        # override. Nothing available shows that --force stays inside the
        # sandbox, so it is refused even when a sandbox posture would verify
        # (review 5449508428).
        if _skip("UA_CURSOR_SKIP_PERMISSIONS"):
            raise harness_sandbox.HarnessSandboxUnavailable(
                harness_sandbox.refusal_message(
                    "cursor",
                    "--force",
                    "--force (Run Everything) is not shown to keep commands inside "
                    "cursor's sandbox; unset UA_CURSOR_SKIP_PERMISSIONS",
                )
            )
        argv = [binary, "-p", "--output-format", "stream-json"]
        argv.extend(_sandbox_for("--trust"))
        argv.append("--trust")
        if use_model:
            argv.extend(["--model", use_model])
        if cwd:
            argv.extend(["--workspace", cwd])
        if prompt is not None:
            # ``--`` ends options so a prompt such as ``--force`` stays an
            # operand (commander ``[prompt...]``; checked against commander 10).
            argv.extend(["--", cursor_prompt(prompt)])
        return argv
    if cli_backend == "hermes":
        argv = [binary]
        if cwd:
            argv.extend(["--in", cwd])
        if _skip("UA_HERMES_SKIP_PERMISSIONS"):
            # hermes --help: ``--yolo`` bypasses dangerous-command approval and
            # names no sandbox switch. The flag is attached only with a
            # verified posture from this chokepoint; resolve() does not produce
            # one.
            posture = sandbox_posture or harness_sandbox.resolve("hermes", cwd=cwd)
            argv.extend(harness_sandbox.require("hermes", posture, flags="--yolo"))
            argv.append("--yolo")
        if prompt is None:
            argv.append("--oneshot")
        else:
            # ``--oneshot`` takes its value from the next token, and argparse
            # refuses a flag-looking one ("expected one argument"). The
            # ``--oneshot=<prompt>`` form always binds the value (verified
            # against hermes 0.21.5: ``--oneshot=--x --version`` parses).
            argv.append(f"--oneshot={prompt}")
        return argv
    if cli_backend == "copilot":
        # Non-interactive Copilot CLI requires --allow-all-tools. -p needs a
        # prompt string, so the observable (stored) form omits -p entirely.
        # The flag rides only with ``--sandbox`` and a verified posture
        # (task #1948).
        allow_all = _skip("UA_COPILOT_SKIP_PERMISSIONS")
        # Keep the native version whose effective settings were queried:
        # an already downloaded update must not apply between query and run.
        argv = [binary, "--no-auto-update", "--output-format", "json"]
        argv.extend(
            _sandbox_for("--allow-all-tools --allow-all" if allow_all else "--allow-all-tools")
        )
        argv.append("--allow-all-tools")
        if cwd:
            argv.extend(["-C", cwd])
        if allow_all:
            argv.append("--allow-all")
        if prompt is not None:
            # ``--prompt=<prompt>`` binds the value even when it starts with
            # ``-``; a separate ``-p <prompt>`` token would be parsed as a flag
            # (verified against copilot 1.0.92: ``--prompt=--bogus --version``
            # parses, ``--bogus`` alone is an unexpected argument).
            argv.append(f"--prompt={prompt}")
        return argv
    if cli_backend == "devin":
        mode = resolve_devin_permission_mode(permission_mode)
        argv = [binary]
        if mode == "dangerous":
            # ``devin --permission-mode dangerous`` auto-approves every tool
            # and names no sandbox switch. The flag is attached only with a
            # verified posture; resolve() does not produce one (task #1965).
            posture = sandbox_posture or harness_sandbox.resolve("devin", cwd=cwd)
            argv.extend(
                harness_sandbox.require("devin", posture, flags="--permission-mode dangerous")
            )
        argv.extend(["--permission-mode", mode])
        if use_model:
            argv.extend(["--model", use_model])
        if prompt is not None:
            # ``-p [<PROMPT>]`` takes an optional value that clap will not bind
            # to a flag-looking token; ``--print=<prompt>`` always binds it
            # (verified against devin 3000.11: ``--print=--bogus --version``
            # parses, ``-p --bogus`` is an unexpected argument).
            argv.append(f"--print={prompt}")
        return argv
    raise ValueError(f"no native headless argv for cli_backend={cli_backend!r}")


def build_launch_argv(
    route: AgentRoute, *, run_id: str, task_id: int, model: str | None = None
) -> list[str]:
    """Observable native-headless argv for the route — no secrets, no prompt.

    ``run_id`` and ``task_id`` are accepted for call-site compatibility; they
    are not placed on argv (native CLIs do not take ``--ua-dispatch`` flags).
    """
    del run_id, task_id
    return headless_argv(route.cli_backend, route.binary, model=model)
