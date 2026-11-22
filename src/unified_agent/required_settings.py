"""Settings an enabled feature cannot run without (task #1783).

Removing a code default is safe for a fresh install, which never relied on it,
and silently breaks an existing one that did: #954 removed the built-in
Approval Ledger repository and vault path, and the pr-watch job on the hub then
exited 1 on every run while ``system_doctor`` still reported the ledger LIVE.

Each entry here pairs a feature's enable condition with the setting it needs.
``system_doctor`` reports any enabled feature whose setting is unset or invalid
as MISCONFIGURED, so a removed default shows up at deploy time rather than as a
quiet backlog. Add an entry whenever a default is removed from code and an
installation may have depended on it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import github_app

_OWNER_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+")

# Scheduled job names, as reported by the caller's ``jobs`` set.
PR_WATCH_JOB = "pr-watch"


def is_owner_repo(value: str) -> bool:
    """True for a plain ``owner/repository`` name (the Approval Ledger target)."""
    return bool(_OWNER_REPO.fullmatch(value)) and value.rsplit("/", 1)[-1] not in {".", ".."}


def _is_directory(value: str) -> bool:
    return Path(value).expanduser().is_dir()


@dataclass(frozen=True)
class Requirement:
    """One feature, its enable condition and the setting it requires."""

    feature: str
    enabled_by: str
    env: str
    setting: str
    enabled: Callable[[Mapping[str, str], frozenset[str]], bool]
    valid: Callable[[str], bool] | None = None
    expected: str = "a value"
    # The scheduled job whose own environment the feature runs in, when it
    # does not run in the hub; judged against that environment when known.
    consumer: str | None = None
    # Report a feature switched on for the hub but off where it runs.
    gate_must_match: bool = False


def _connector_enabled(env: Mapping[str, str], _jobs: frozenset[str]) -> bool:
    return github_app.enabled(env)


def _pr_watch_journal_enabled(env: Mapping[str, str], jobs: frozenset[str]) -> bool:
    if PR_WATCH_JOB not in jobs:
        return False
    # Broker-only mode skips the legacy journal, which is the vault writer.
    try:
        broker_only = (
            github_app.enabled(env)
            and github_app.token_mode(env) == github_app.TOKEN_MODE_USER
            and github_app.user_token_source(env) == "connector_oauth"
        )
    except ValueError:
        broker_only = False
    return not broker_only


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        feature="Approval Ledger connector",
        enabled_by="UA_GITHUB_APP_ENABLED=1",
        env="UA_GITHUB_CONNECTOR_REPO",
        setting="github.connector_repo",
        enabled=_connector_enabled,
        valid=is_owner_repo,
        expected="an owner/repository name",
        # The connector polls inside the pr-watch job (github_app_worker.run_once).
        consumer=PR_WATCH_JOB,
        gate_must_match=True,
    ),
    Requirement(
        feature="pr-watch PR journal",
        enabled_by="the pr-watch job is installed on this host",
        env="UA_VAULT_PATH",
        setting="paths.vault",
        enabled=_pr_watch_journal_enabled,
        valid=_is_directory,
        expected="an existing vault directory",
        consumer=PR_WATCH_JOB,
    ),
)


def evaluate(
    env: Mapping[str, str],
    jobs: frozenset[str] = frozenset(),
    requirements: tuple[Requirement, ...] = REQUIREMENTS,
    consumer_envs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, list[str]]:
    """Return ``{"enabled": [feature...], "unmet": [message...]}`` for this environment.

    ``env`` is the hub's environment. ``consumer_envs`` maps a scheduled job
    to its own effective environment; a requirement with that ``consumer`` is
    judged there, because the hub's settings never reach the job's process.
    """
    enabled: list[str] = []
    unmet: list[str] = []
    for req in requirements:
        own_env = (consumer_envs or {}).get(req.consumer or "")
        target = env if own_env is None else own_env
        suffix = "" if own_env is None else f" in the {req.consumer} job environment"
        if not req.enabled(target, jobs):
            if own_env is not None and req.gate_must_match and req.enabled(env, jobs):
                unmet.append(
                    f"{req.feature} is enabled for the hub ({req.enabled_by}) but not{suffix}, "
                    "which is where it runs"
                )
            continue
        enabled.append(req.feature)
        value = (target.get(req.env) or "").strip()
        where = f"{req.env} ({req.setting} in settings.toml){suffix}"
        if not value:
            unmet.append(f"{req.feature} is enabled ({req.enabled_by}) but {where} is unset")
        elif req.valid is not None and not req.valid(value):
            unmet.append(
                f"{req.feature} is enabled ({req.enabled_by}) but {where} is invalid; "
                f"expected {req.expected}"
            )
    return {"enabled": enabled, "unmet": unmet}
