"""User settings: a per-install ``settings.toml`` that personalizes a deployment.

Every knob in unified-agent is an ``UA_*`` environment variable (see
``config.py``). That works for a service unit, but it is a poor way for a new
user to say "my name is Alex, my workspace is ~/agents, my notes live in
~/Obsidian". This module adds one human-editable file on top of the env vars
without changing how any setting is read:

    precedence:  real environment  >  settings.toml  >  code default

``apply_to_environ()`` reads the file and *seeds* ``os.environ`` with
``setdefault`` — a variable already present in the environment always wins, so
a systemd ``Environment=`` line, ``service.env``, or a test's ``monkeypatch``
keeps working exactly as before. ``config`` calls it once at import, so every
entry point (hub, CLI subcommands, workers) sees the same settings.

Location: ``$UA_SETTINGS_FILE`` if set, else ``$UA_HOME/settings.toml``
(``~/.unified_agent/settings.toml``). Set ``UA_SETTINGS_FILE=`` (empty) to
disable the file entirely; the test suite does this so a developer's own
settings never leak into a run.

Secrets do not belong here. The file is meant to be readable, diffable, and
safe to copy between machines; bearer tokens, API keys and passphrases stay in
``service.env`` (mode 0600). Keys that look secret are refused with a warning.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import projects

SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.toml"


@dataclass(frozen=True)
class SettingSpec:
    """One typed, documented setting and the env var it feeds."""

    section: str
    key: str
    env: str
    help: str
    kind: str = "str"  # str | int | bool | list | json
    sep: str = ":"  # join separator for kind == "list"

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.key}"


# The typed surface. Anything else can still be set through the [env] table.
SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        "operator",
        "name",
        "UA_OPERATOR_NAME",
        "Your name as the human operator (approvals, evidence reports, Control Center).",
    ),
    SettingSpec(
        "operator",
        "label",
        "UA_OPERATOR_LABEL",
        "Identity label of your human login (bearer token / web UI), e.g. Desktop_Alex.",
    ),
    SettingSpec(
        "operator",
        "aliases",
        "UA_OPERATOR_ALIASES",
        "Other names that refer to you (comma-separated in env).",
        kind="list",
        sep=",",
    ),
    SettingSpec(
        "operator",
        "timezone",
        "UA_OPERATOR_TZ",
        "IANA time zone for 'today' windows, e.g. Europe/Berlin.",
    ),
    SettingSpec("paths", "workspace", "UA_WORKSPACE", "Sandbox root for fs_*/run_cmd/git_* tools."),
    SettingSpec(
        "paths", "vault", "UA_VAULT_PATH", "Obsidian-style vault; empty disables vault tools."
    ),
    SettingSpec(
        "paths",
        "vault_write_dirs",
        "UA_VAULT_WRITE_DIRS",
        "Vault-relative folders agents may write to.",
        kind="list",
    ),
    SettingSpec("paths", "dev_root", "UA_DEV_ROOT", "Directory holding your project checkouts."),
    SettingSpec("server", "transport", "UA_TRANSPORT", "stdio (single host) or http (hub)."),
    SettingSpec("server", "host", "UA_HTTP_HOST", "Bind address for the HTTP hub."),
    SettingSpec("server", "port", "UA_HTTP_PORT", "Port for the HTTP hub.", kind="int"),
    SettingSpec(
        "server",
        "deployment_mode",
        "UA_DEPLOYMENT_MODE",
        "local_trusted | authenticated_private | authenticated_public.",
    ),
    SettingSpec(
        "server",
        "allowed_hosts",
        "UA_HTTP_ALLOWED_HOSTS",
        "Host headers the hub answers (DNS-rebinding guard).",
        kind="list",
        sep=",",
    ),
    SettingSpec(
        "agents",
        "labels",
        "UA_AGENT_LABELS",
        "Closed roster of agent identities; empty keeps the roster open.",
        kind="list",
    ),
    SettingSpec(
        "projects",
        "entries",
        "UA_PROJECTS_JSON",
        "Installation project entries: id, label, dirname, domain, optional repo/default_branch.",
        kind="json",
    ),
    SettingSpec("cloud", "project", "UA_GCP_PROJECT", "Explicit GCP project; no built-in target."),
    SettingSpec("cloud", "worker", "UA_GCP_WORKER", "VM name eligible for health and recovery."),
    SettingSpec("chat", "zulip_site", "UA_ZULIP_SITE", "Explicit HTTPS Zulip site allowlist."),
    SettingSpec(
        "github",
        "connector_repo",
        "UA_GITHUB_CONNECTOR_REPO",
        "Explicit owner/repository for the Approval Ledger poller.",
    ),
    SettingSpec(
        "github",
        "connector_base",
        "UA_GITHUB_CONNECTOR_BASE",
        "Base branch the Approval Ledger poller watches (default main).",
    ),
    SettingSpec(
        "vault_sync",
        "remote_host",
        "UA_VAULT_SYNC_REMOTE_HOST",
        "SSH alias for the authoritative vault.",
    ),
    SettingSpec(
        "vault_sync",
        "remote_root",
        "UA_VAULT_SYNC_REMOTE_ROOT",
        "Remote vault path; no built-in destination.",
    ),
    SettingSpec(
        "fleet",
        "nodes",
        "UA_FLEET_NODES",
        "ua-node names that must be live; doctor's fleet_nodes row checks each.",
        kind="list",
        sep=",",
    ),
    SettingSpec(
        "fleet",
        "optional_nodes",
        "UA_FLEET_OPTIONAL_NODES",
        "ua-node names that may be absent by decision (reported, never a fault).",
        kind="list",
        sep=",",
    ),
    SettingSpec(
        "telemetry",
        "lane_b_repos",
        "UA_LANE_B_REPOS",
        "JSON list of slug/github owner/repo report targets.",
    ),
    SettingSpec("search", "provider", "UA_SEARCH_PROVIDER", "Web search backend for web_search."),
    SettingSpec("memory", "embed_model", "UA_EMBED_MODEL", "fastembed model for semantic memory."),
    SettingSpec(
        "queue",
        "liveness_sweep",
        "UA_LIVENESS_SWEEP",
        "Task liveness sweep: off | report (default; digest entries only) | enforce "
        "(also moves tasks with no action path to blocked).",
    ),
)

_SPECS_BY_DOTTED = {spec.dotted: spec for spec in SPECS}
_SECTIONS = frozenset(spec.section for spec in SPECS)

# Name fragments that mark a value as a credential. Matched per underscore
# component so UA_TOKEN_MAX_AGE_DAYS (a policy knob) is still allowed.
_SECRET_COMPONENTS = frozenset(
    {"TOKEN", "TOKENS", "SECRET", "SECRETS", "PASSWORD", "PASSPHRASE", "KEY", "CREDENTIALS"}
)
_SECRET_ALLOW = frozenset({"UA_TOKEN_MAX_AGE_DAYS"})
# [env] only reaches unified-agent's own namespace: a copied settings file
# must not be able to set PATH, PYTHONPATH, LD_PRELOAD or a provider's
# variables for the hub and every subprocess it spawns.
_ENV_NAME_RE = re.compile(r"^UA_[A-Z0-9_]+$")
# Security posture and the file's own location stay in the real environment.
# A readable, copyable settings file must not be able to turn authentication
# or approvals off, relocate the store, or re-point itself.
_ENV_DENY = frozenset(
    {
        "UA_ENFORCE_AUTH",
        "UA_LEGACY_OPEN_POSTURE",
        "UA_APPROVAL_TOOLS",
        "UA_TRUST_STDIO",
        "UA_REQUIRE_CALLER_IDENTITY",
        "UA_LIFECYCLE_OPERATORS",
        "UA_REVIEW_READERS",
        "UA_POLICY_AUTHORIZERS",
        "UA_POLICY_AUTHORIZATION_ALLOW_SELF",
        "UA_HOME",
        "UA_SETTINGS_FILE",
    }
)


class SettingsError(ValueError):
    """The settings file is unreadable or violates the schema."""


def is_secret_env_name(name: str) -> bool:
    """True when ``name`` looks like it carries a credential."""
    if name in _SECRET_ALLOW:
        return False
    parts = name.upper().split("_")
    return any(part in _SECRET_COMPONENTS for part in parts) or name.upper().endswith("_KEY")


def settings_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """Resolved settings file path, or None when disabled (``UA_SETTINGS_FILE=``)."""
    env = os.environ if environ is None else environ
    if "UA_SETTINGS_FILE" in env:
        raw = env["UA_SETTINGS_FILE"].strip()
        return Path(raw).expanduser() if raw else None
    home = Path(env.get("UA_HOME", "~/.unified_agent")).expanduser()
    return home / SETTINGS_FILENAME


def load(path: Path | None = None) -> dict:
    """Parse the settings file. Missing file → ``{}``; malformed → SettingsError."""
    target = settings_path() if path is None else path
    if target is None or not target.is_file():
        return {}
    try:
        with target.open("rb") as fh:
            _check_trusted(target, os.fstat(fh.fileno()))
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"cannot read {target}: {exc}") from exc
    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise SettingsError(
            f"{target}: schema_version {version!r} is not supported (expected {SCHEMA_VERSION})"
        )
    return data


def _check_trusted(target: Path, st: os.stat_result) -> None:
    """Refuse a settings file another local user could have written.

    It names the human operator and seeds configuration for every process, so
    it gets the same ownership rule as the credential store beside it.
    """
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise SettingsError(f"{target} is not owned by the current user; refusing it")
    if st.st_mode & 0o022:
        raise SettingsError(
            f"{target} is writable by group or other users (mode "
            f"{st.st_mode & 0o777:o}); refusing it. Run: chmod 600 {target}"
        )


def _render_value(spec: SettingSpec, value: object) -> str:
    if spec.kind == "json":
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise SettingsError(f"{spec.dotted} must be an array of project tables")
        return json.dumps(value, separators=(",", ":"))
    if spec.kind == "list":
        if isinstance(value, str):
            return value
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"{spec.dotted} must be a list of strings")
        return spec.sep.join(v.strip() for v in value if v.strip())
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise SettingsError(f"{spec.dotted} must be an integer")
        try:
            return str(int(value))
        except ValueError as exc:
            raise SettingsError(f"{spec.dotted} must be an integer") from exc
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{spec.dotted} must be true or false")
        return "1" if value else "0"
    if not isinstance(value, str):
        raise SettingsError(f"{spec.dotted} must be a string")
    return value


def _environment_value(name: str, value: str) -> str:
    """Validate before assignment so an invalid override keeps a good value."""
    if "\x00" in value:
        raise SettingsError(f"{name} contains NUL")
    if name == "UA_TRANSPORT" and value not in {"stdio", "http"}:
        raise SettingsError(f"{name} must be stdio or http")
    if name == "UA_HTTP_PORT":
        try:
            port = int(value)
        except ValueError as exc:
            raise SettingsError(f"{name} must be an integer TCP port") from exc
        # Port zero retains the server's supported ephemeral-port binding.
        if not 0 <= port <= 65535:
            raise SettingsError(f"{name} must be between 0 and 65535")
    if name == "UA_OPERATOR_TZ" and value:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SettingsError(f"{name} must be an available IANA time zone") from exc
    if name in {"UA_WORKSPACE", "UA_VAULT_PATH", "UA_DEV_ROOT"} and re.search(
        r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})", value
    ):
        raise SettingsError(f"{name} contains a shell variable; use a literal path or ~/ prefix")
    if name == "UA_PROJECTS_JSON":
        try:
            if len(value.encode()) > 1024 * 1024:
                raise ValueError("project registry exceeds 1 MiB")
            projects._parse_registry(value.strip())
        except ValueError as exc:
            raise SettingsError(f"{name}: {exc}") from exc
    return value


def _closest(word: str, candidates: Iterable[str]) -> str | None:
    matches = difflib.get_close_matches(word, sorted(set(candidates)), n=1)
    return matches[0] if matches else None


def unknown_keys(data: Mapping[str, object]) -> list[tuple[str, str | None]]:
    """Settings names the typed schema does not know, with a likely correction.

    Returns ``(name, suggestion)`` pairs in file order. A section is named
    ``[section]``, a key ``section.key``. The suggestion is the closest known
    name (same form) or ``None``. ``[env]`` is free-form and not checked here.
    ``to_env`` and the init report both read this, so they cannot drift.
    """
    found: list[tuple[str, str | None]] = []
    for section, body in data.items():
        if section in ("schema_version", "env"):
            continue
        if section not in _SECTIONS:
            guess = _closest(str(section), _SECTIONS | {"env"})
            found.append((f"[{section}]", f"[{guess}]" if guess else None))
            continue
        if not isinstance(body, Mapping):
            continue
        for key in body:
            if f"{section}.{key}" in _SPECS_BY_DOTTED:
                continue
            exact = [s.dotted for s in SPECS if s.key == key]
            same = {spec.key for spec in SPECS if spec.section == section}
            guess = _closest(str(key), same)
            if exact:
                # The exact key in another section beats a fuzzy same-section
                # match (``labels`` under [operator] is agents.labels, not
                # operator.label).
                suggestion: str | None = exact[0]
            elif guess is not None:
                suggestion = f"{section}.{guess}"
            else:
                # Right key, wrong section (e.g. workspace under [operator]).
                elsewhere = _closest(str(key), (spec.key for spec in SPECS))
                suggestion = next(
                    (s.dotted for s in SPECS if elsewhere is not None and s.key == elsewhere),
                    None,
                )
            found.append((f"{section}.{key}", suggestion))
    return found


def describe_unknown(name: str, suggestion: str | None) -> str:
    """One human-readable line for an ``unknown_keys`` entry."""
    kind = "settings section" if name.startswith("[") else "setting"
    hint = f" (did you mean {suggestion}?)" if suggestion else ""
    return f"unknown {kind} {name}{hint}"


def to_env(data: Mapping[str, object]) -> tuple[dict[str, str], list[str]]:
    """Flatten parsed settings into ``{ENV_NAME: value}`` plus warnings.

    Unknown sections/keys and secret-looking ``[env]`` entries are reported as
    warnings and skipped, so one typo never stops the hub from starting.
    """
    out: dict[str, str] = {}
    warnings: list[str] = [
        f"{describe_unknown(name, hint)}; skipped" for name, hint in unknown_keys(data)
    ]
    for section, body in data.items():
        if section == "schema_version":
            continue
        if section == "env":
            if not isinstance(body, Mapping):
                warnings.append("[env] must be a table of NAME = value")
                continue
            for name, value in body.items():
                if not _ENV_NAME_RE.fullmatch(str(name)):
                    warnings.append(f"[env] {name!r} is not a UA_* variable name; skipped")
                    continue
                if name in _ENV_DENY:
                    warnings.append(
                        f"[env] {name} sets security posture or the settings location; "
                        "keep it in the service environment (skipped)"
                    )
                    continue
                if is_secret_env_name(name):
                    warnings.append(
                        f"[env] {name} looks like a secret; keep it in service.env, not "
                        "settings.toml (skipped)"
                    )
                    continue
                if isinstance(value, bool):
                    rendered = "1" if value else "0"
                elif isinstance(value, int | float | str):
                    rendered = str(value)
                elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                    rendered = ":".join(value)
                else:
                    warnings.append(f"[env] {name} has an unsupported value type; skipped")
                    continue
                try:
                    out[name] = _environment_value(name, rendered)
                except SettingsError as exc:
                    warnings.append(f"{exc}; skipped")
            continue
        if section not in _SECTIONS:
            continue  # reported by unknown_keys above
        if not isinstance(body, Mapping):
            warnings.append(f"settings section [{section}] must be a table; skipped")
            continue
        for key, value in body.items():
            spec = _SPECS_BY_DOTTED.get(f"{section}.{key}")
            if spec is None:
                continue  # reported by unknown_keys above
            try:
                out[spec.env] = _environment_value(spec.env, _render_value(spec, value))
            except SettingsError as exc:
                warnings.append(f"{exc}; skipped")
    return out, warnings


_APPLIED: dict[str, str] = {}


def apply_to_environ(
    environ: MutableMapping[str, str] | None = None,
    *,
    path: Path | None = None,
    quiet: bool = False,
) -> dict[str, str]:
    """Seed ``environ`` from the settings file without overriding anything set.

    Returns the variables this call actually set. Errors and warnings go to
    stderr (never raised): a broken settings file must not take the hub down,
    and the code defaults still apply.
    """
    env = os.environ if environ is None else environ
    target = settings_path(env) if path is None else path
    try:
        data = load(target) if target is not None else {}
    except SettingsError as exc:
        if not quiet:
            print(f"ua-settings: [Warning] {exc}; ignoring settings file", file=sys.stderr)
        return {}
    values, warnings = to_env(data)
    if not quiet:
        for message in warnings:
            print(f"ua-settings: [Warning] {message}", file=sys.stderr)
    applied: dict[str, str] = {}
    for name, value in values.items():
        if name not in env:
            env[name] = value
            applied[name] = value
    if environ is None:
        _APPLIED.update(applied)
    return applied


def applied_from_file() -> dict[str, str]:
    """Env vars this process took from settings.toml (for doctor/`settings show`)."""
    return dict(_APPLIED)


def runtime_default(spec: SettingSpec) -> str | None:
    """The value the code uses for ``spec`` when its variable is unset.

    Read from the same ``config`` accessors the hub uses, so ``settings get``
    and ``settings show`` report what is actually in effect. None when the
    setting has no default (for example an open agent roster).
    """
    from . import config  # lazy: config imports this module at load time

    resolvers = {
        "operator.name": config.operator_name,
        "operator.label": config.operator_label,
        # Only explicitly configured aliases; the name itself is not one.
        "operator.aliases": lambda: "",
        "paths.workspace": lambda: str(config.process_workspace_root()),
        "paths.vault": lambda: str(config.vault_root() or ""),
        "paths.vault_write_dirs": lambda: ":".join(config.vault_write_dirs()),
        "paths.dev_root": lambda: str(config.dev_root()),
        "projects.entries": lambda: json.dumps(
            [{"id": project, **row} for project, row in config.project_registry().items()]
        ),
        "server.transport": config.transport,
        "server.host": config.http_host,
        "server.port": lambda: str(config.http_port()),
        "server.deployment_mode": config.deployment_mode,
        "search.provider": config.search_provider,
        "memory.embed_model": config.embed_model,
    }
    resolve = resolvers.get(spec.dotted)
    if resolve is None:
        return None
    try:
        return resolve()
    except Exception:  # noqa: BLE001 - a broken default must not break `show`
        return None


def effective(environ: Mapping[str, str] | None = None) -> list[dict[str, str | None]]:
    """Each typed setting with its effective value and where it came from."""
    env = os.environ if environ is None else environ
    rows: list[dict[str, str | None]] = []
    for spec in SPECS:
        value = env.get(spec.env)
        if value is None:
            source = "default"
            value = runtime_default(spec) if environ is None else None
        elif _APPLIED.get(spec.env) == value:
            source = "settings.toml"
        else:
            source = "environment"
        rows.append({"setting": spec.dotted, "env": spec.env, "value": value, "source": source})
    return rows


# ---------------------------------------------------------------------------
# Writing


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Mapping):
        return (
            "{ "
            + ", ".join(
                f"{_toml_scalar(str(key))} = {_toml_scalar(item)}" for key, item in value.items()
            )
            + " }"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    # JSON's basic string escapes are also TOML escapes. DEL additionally
    # needs an explicit escape because TOML forbids it in a basic string.
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def render(data: Mapping[str, object]) -> str:
    """Serialize a settings mapping to TOML (sections in SPECS order, then [env])."""
    lines = [
        "# unified-agent settings — personalizes this install.",
        "# Precedence: environment variables > this file > built-in defaults.",
        "# Secrets (tokens, API keys) belong in service.env, never here.",
        "# Reference: docs/settings.md",
        "",
        f"schema_version = {SCHEMA_VERSION}",
    ]
    ordered = [s for s in dict.fromkeys(spec.section for spec in SPECS) if s in data]
    ordered += [s for s in data if s not in ordered and s not in ("schema_version", "env")]
    if "env" in data:
        ordered.append("env")
    for section in ordered:
        body = data.get(section)
        if not isinstance(body, Mapping) or not body:
            continue
        lines += ["", f"[{section}]"]
        for key, value in body.items():
            lines.append(f"{key} = {_toml_scalar(value)}")
    return "\n".join(lines) + "\n"


def stage(data: Mapping[str, object], path: Path | None = None) -> tuple[Path, Path]:
    """Validate ``data`` and write it beside the settings file, not over it.

    Returns ``(staged, target)``; ``staged.replace(target)`` publishes it.
    Lets a caller (``unified-agent init``) prove the save will succeed before
    it changes anything else, and publish only once everything else has.
    """
    target = settings_path() if path is None else path
    if target is None:
        raise SettingsError("settings file is disabled (UA_SETTINGS_FILE is empty)")
    _, warnings = to_env(data)
    if warnings:
        raise SettingsError("; ".join(warnings))
    rendered = render(data)
    try:
        tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(
            "rendered settings are not valid TOML; existing file unchanged"
        ) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call and created exclusively: overlapping `init` or
    # `settings set` runs never share (or publish) each other's staged file.
    import secrets

    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(rendered)
    except BaseException:
        if fd != -1:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    return tmp, target


def write(data: Mapping[str, object], path: Path | None = None) -> Path:
    """Write ``data`` to the settings file (mode 0600: it names the operator)."""
    tmp, target = stage(data, path)
    try:
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def _coerce_cli_value(spec: SettingSpec | None, raw: str) -> object:
    if spec is None:
        return raw
    if spec.kind == "json":
        return json.loads(raw)
    if spec.kind == "list":
        sep = spec.sep if spec.sep in raw else ("," if "," in raw else spec.sep)
        return [part.strip() for part in raw.split(sep) if part.strip()]
    if spec.kind == "int":
        return int(raw)
    if spec.kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return raw


def set_value(dotted: str, raw: str, path: Path | None = None) -> Path:
    """Set one setting (``section.key`` or ``env.NAME``) and write the file."""
    section, _, key = dotted.partition(".")
    if not key:
        raise SettingsError(f"expected section.key, got {dotted!r}")
    spec = _SPECS_BY_DOTTED.get(dotted)
    if section != "env" and spec is None:
        raise SettingsError(f"unknown setting {dotted!r}; see `unified-agent settings keys`")
    data = load(path if path is not None else settings_path())
    data = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in data.items()}
    data.setdefault("schema_version", SCHEMA_VERSION)
    body = data.setdefault(section, {})
    try:
        body[key] = _coerce_cli_value(spec, raw)
    except ValueError as exc:
        raise SettingsError(f"{dotted}: {exc}") from exc
    return write(data, path)


def unset_value(dotted: str, path: Path | None = None) -> Path:
    section, _, key = dotted.partition(".")
    data = load(path if path is not None else settings_path())
    data = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in data.items()}
    body = data.get(section)
    if isinstance(body, dict):
        body.pop(key, None)
        if not body:
            data.pop(section, None)
    return write(data, path)


# ---------------------------------------------------------------------------
# CLI: `unified-agent settings ...`


def run_settings_cli(argv: list[str]) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="unified-agent settings",
        description="Show or edit this install's settings.toml (personalization).",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("path", help="print the settings file path")
    show = sub.add_parser("show", help="effective value and source of each setting")
    show.add_argument("--json", action="store_true")
    sub.add_parser("keys", help="list the typed settings and their env vars")
    get = sub.add_parser("get", help="print one effective value")
    get.add_argument("key", help="section.key, e.g. operator.name")
    setp = sub.add_parser("set", help="set a value in settings.toml")
    setp.add_argument("key", help="section.key, or env.UA_SOMETHING for any non-secret var")
    setp.add_argument("value")
    unset = sub.add_parser("unset", help="remove a value from settings.toml")
    unset.add_argument("key")
    sub.add_parser("check", help="validate the file; exit 1 on any warning")
    args = parser.parse_args(argv)

    path = settings_path()
    if args.cmd in (None, "path"):
        print(path if path is not None else "(disabled: UA_SETTINGS_FILE is empty)")
        return 0
    if args.cmd == "keys":
        for spec in SPECS:
            print(f"{spec.dotted:<26} {spec.env:<24} {spec.help}")
        print(f"{'env.<NAME>':<26} {'<NAME>':<24} Any other non-secret UA_* variable.")
        return 0
    if args.cmd == "show":
        rows = effective()
        if args.json:
            print(json.dumps({"path": str(path) if path else None, "settings": rows}, indent=2))
        else:
            print(f"settings file: {path if path else '(disabled)'}")
            for row in rows:
                value = row["value"] if row["value"] is not None else "-"
                print(f"  {row['setting']:<26} {value!s:<36} ({row['source']})")
        return 0
    if args.cmd == "get":
        spec = _SPECS_BY_DOTTED.get(args.key)
        name = spec.env if spec else args.key.removeprefix("env.")
        value = os.environ.get(name)
        if value is None and spec is not None:
            value = runtime_default(spec)
        if value is None:
            return 1
        print(value)
        return 0
    try:
        if args.cmd == "set":
            target = set_value(args.key, args.value, path)
            print(f"set {args.key} in {target}")
            return 0
        if args.cmd == "unset":
            target = unset_value(args.key, path)
            print(f"unset {args.key} in {target}")
            return 0
        if args.cmd == "check":
            _, warnings = to_env(load(path))
            for message in warnings:
                print(f"warning: {message}", file=sys.stderr)
            if not warnings:
                print(f"ok: {path}")
            return 1 if warnings else 0
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 2
