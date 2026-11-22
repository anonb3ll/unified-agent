"""Dev-server detection: read-only scan for launchable project servers."""

import json
import re
import tomllib
from pathlib import Path

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from ..safety import resolve_for_audit

_NODE_DEV_SCRIPT_NAMES = {"dev", "start", "serve", "develop"}
_NODE_DEV_SCRIPT_TOKENS = ("dev", "serve", "watch")
_NODE_LOCKFILES = {
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "bun.lockb": "bun",
    "package-lock.json": "npm",
}
_NODE_RUN_PREFIX = {"npm": ["run"], "yarn": [], "pnpm": ["run"], "bun": ["run"]}

_PORT_FLAG_RE = re.compile(r"(?:--port|-p)[\s=](\d{2,5})\b")
_PORT_ENV_RE = re.compile(r"\bPORT\s*=\s*(\d{2,5})\b")
_PORT_GETENV_RE = re.compile(r'getenv\(\s*["\']\w*PORT\w*["\']\s*,\s*["\']?(\d{2,5})')
_SERVER_CALL_RE = re.compile(
    r"(?:uvicorn\.run\(|app\.run\(|mcp\.run\(|\.listen\(|HTTPServer\("
    r"|\bserve_forever\b|\brunserver\b)"
)
_COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def _read_text(p: Path, limit: int = 200_000) -> str | None:
    try:
        if not p.is_file() or p.stat().st_size > limit:
            return None
        return p.read_text(errors="ignore")
    except OSError:
        return None


def _listening_ports() -> set[int]:
    """Best-effort read of locally listening TCP ports via /proc/net/tcp{,6} (Linux only).

    Silently returns an empty set on any other OS or if /proc is unreadable —
    this is an advisory conflict check, not a hard dependency.
    """
    ports: set[int] = set()
    for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proc_file) as f:
                next(f, None)
                for line in f:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "0A":  # 0A = TCP_LISTEN
                        continue
                    port_hex = fields[1].rsplit(":", 1)[-1]
                    ports.add(int(port_hex, 16))
        except (OSError, ValueError):
            continue
    return ports


def _detect_node(root: Path) -> tuple[list[dict], list[str]]:
    pkg_path = root / "package.json"
    text = _read_text(pkg_path)
    if text is None:
        return [], []
    try:
        pkg = json.loads(text)
    except json.JSONDecodeError:
        return [], [f"{pkg_path.name} is not valid JSON; skipped"]

    manager = "npm"
    for lockfile, mgr in _NODE_LOCKFILES.items():
        if (root / lockfile).is_file():
            manager = mgr
            break

    configs = []
    for name, cmd in (pkg.get("scripts") or {}).items():
        if not isinstance(cmd, str):
            continue
        if name not in _NODE_DEV_SCRIPT_NAMES and not any(
            tok in cmd for tok in _NODE_DEV_SCRIPT_TOKENS
        ):
            continue
        m = _PORT_FLAG_RE.search(cmd) or _PORT_ENV_RE.search(cmd)
        port = int(m.group(1)) if m else None
        configs.append(
            {
                "name": f"{pkg.get('name', root.name)} ({name})",
                "runtimeExecutable": manager,
                "runtimeArgs": _NODE_RUN_PREFIX[manager] + [name],
                "port": port,
                "source": pkg_path.name,
                "note": None
                if port
                else "no explicit port in the script; check the framework's default",
            }
        )
    return configs, []


def _resolve_entry_module(root: Path, target: str) -> Path | None:
    """`target` is "pkg.module:function" — locate pkg/module.py under a src/ layout or root."""
    module_path = target.split(":", 1)[0]
    rel = Path(*module_path.split("."))
    for base in (root / "src", root):
        candidate = (base / rel).with_suffix(".py")
        if candidate.is_file():
            return candidate
        candidate = base / rel / "__init__.py"
        if candidate.is_file():
            return candidate
    return None


def _detect_python(root: Path) -> tuple[list[dict], list[str]]:
    pyproject_path = root / "pyproject.toml"
    text = _read_text(pyproject_path)
    if text is None:
        return [], []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [], [f"{pyproject_path.name} failed to parse; skipped"]

    configs = []
    for name, target in ((data.get("project") or {}).get("scripts") or {}).items():
        if not isinstance(target, str):
            continue
        module_file = _resolve_entry_module(root, target)
        module_text = _read_text(module_file) if module_file else None
        if not module_text or not _SERVER_CALL_RE.search(module_text):
            continue  # CLI utility, not a server — no evidence it opens a port
        m = _PORT_GETENV_RE.search(module_text) or _PORT_ENV_RE.search(module_text)
        port = int(m.group(1)) if m else None
        configs.append(
            {
                "name": name,
                "runtimeExecutable": "uv",
                "runtimeArgs": ["run", name],
                "port": port,
                "source": str(module_file.relative_to(root)),
                "note": None
                if port
                else "server entry point found but no default port in source; "
                "set env vars or pass args",
            }
        )
    return configs, []


def _detect_compose(root: Path) -> tuple[list[dict], list[str]]:
    compose_path = next((root / n for n in _COMPOSE_NAMES if (root / n).is_file()), None)
    if compose_path is None:
        return [], []
    text = _read_text(compose_path)
    if text is None:
        return [], []
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return [], [f"{compose_path.name} failed to parse as YAML; skipped"]

    configs = []
    for svc_name, svc in (doc.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        port = None
        for mapping in svc.get("ports") or []:
            host_part = str(mapping).split(":")[0]
            if host_part.isdigit():
                port = int(host_part)
                break
        configs.append(
            {
                "name": svc_name,
                "runtimeExecutable": "docker",
                "runtimeArgs": ["compose", "up", svc_name],
                "port": port,
                "source": compose_path.name,
                "note": None if port else "no host port published for this service",
            }
        )
    return configs, []


def _detect_procfile(root: Path) -> tuple[list[dict], list[str]]:
    procfile_path = root / "Procfile"
    text = _read_text(procfile_path)
    if text is None:
        return [], []

    configs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        proc_name, cmd = line.split(":", 1)
        parts = cmd.strip().split()
        if not parts:
            continue
        configs.append(
            {
                "name": proc_name.strip(),
                "runtimeExecutable": parts[0],
                "runtimeArgs": parts[1:],
                "port": None,
                "source": "Procfile",
                "note": "Procfile processes typically read $PORT at runtime; "
                "set it explicitly if starting standalone",
            }
        )
    return configs, []


def detect_dev_servers(path: str = ".") -> dict:
    """Scan a project for launchable dev servers and propose .claude/launch.json entries.

    Read-only: inspects manifests at the given directory's top level, does not
    start anything or write launch.json itself. Sources checked: package.json
    ("scripts" whose name or command looks dev/serve-like, paired with the
    package manager inferred from its lockfile), pyproject.toml
    [project.scripts] entries whose target module contains a server-starting
    call (uvicorn.run, app.run, mcp.run, .listen(, HTTPServer, serve_forever,
    runserver — CLI-only entry points without one of these are skipped),
    docker-compose services (name + first published host port), and Procfile
    process types. Nested subprojects (e.g. monorepo packages/*) are not
    walked — only the given directory's own manifests.

    Ports are only set when found explicitly (a --port/-p flag, a PORT=NNNN
    default, or a compose port mapping); otherwise `port` is null and `note`
    explains why. Each candidate with a known port is checked against this
    machine's currently listening TCP ports (best-effort, Linux only via
    /proc/net/tcp) and flagged with `conflict` if something is already bound
    there — e.g. a server already running as a systemd service.

    `path` resolves like `security_audit` (workspace root or UA_AUDIT_ROOTS).
    Returns {root, configurations: [{name, runtimeExecutable, runtimeArgs,
    port, source, note, conflict}], warnings}.
    """
    root = resolve_for_audit(path)
    if not root.is_dir():
        raise ToolError(f"path {path!r} is not a directory")

    configs: list[dict] = []
    warnings: list[str] = []
    for detector in (_detect_node, _detect_python, _detect_compose, _detect_procfile):
        found, warned = detector(root)
        configs.extend(found)
        warnings.extend(warned)

    listening = _listening_ports()
    for c in configs:
        c["conflict"] = (
            f"port {c['port']} is already listening on this machine"
            if c["port"] is not None and c["port"] in listening
            else None
        )

    return {"root": str(root), "configurations": configs, "warnings": warnings}
