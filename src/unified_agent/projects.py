"""Installation-owned project streams; these grant no agent filesystem authority."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from typing import Any

DEFAULT_PROJECT = "unified-agent"
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_DIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_DEFAULT = (
    {
        "id": DEFAULT_PROJECT,
        "label": "Unified Agent",
        "dirname": "unified_agent",
        "domain": "hobby",
    },
)


def registry() -> dict[str, dict[str, str]]:
    return {project: dict(row) for project, row in _configured_registry().items()}


def _configured_registry() -> dict[str, dict[str, str]]:
    raw = os.environ.get("UA_PROJECTS_JSON", "").strip()
    if len(raw.encode()) > 1024 * 1024:
        raise ValueError("project registry exceeds 1 MiB")
    return _parse_registry(raw)


@lru_cache(maxsize=16)
def _parse_registry(raw: str) -> dict[str, dict[str, str]]:
    """Read trusted host configuration, validating it before any consumer acts.

    UA_PROJECTS_JSON is a JSON array of project entries, also populated by
    [[projects.entries]] in settings.toml. The legacy queue id remains present
    so unscoped existing rows retain their meaning. Explicit configuration
    replaces the neutral starter; it never adds a project inferred from disk.
    """
    try:
        entries = json.loads(raw) if raw else _DEFAULT
    except json.JSONDecodeError as exc:
        raise ValueError("UA_PROJECTS_JSON must be a JSON array") from exc
    if not isinstance(entries, (list, tuple)) or not 1 <= len(entries) <= 1000:
        raise ValueError("project registry must contain 1 to 1000 entries")
    result: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {
            "id",
            "label",
            "dirname",
            "domain",
            "repo",
            "default_branch",
        }:
            raise ValueError("invalid project registry entry fields")
        project = entry.get("id")
        if (
            not isinstance(project, str)
            or not _ID.fullmatch(project)
            or project == "all"
            or project in result
        ):
            raise ValueError("project ids must be unique safe slugs, excluding all")
        label = entry.get("label", "Unified Agent" if project == DEFAULT_PROJECT else project)
        dirname = entry.get("dirname", "unified_agent" if project == DEFAULT_PROJECT else project)
        domain = entry.get("domain", "hobby")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 160
            or any(ord(c) < 32 for c in label)
        ):
            raise ValueError("project labels must be nonempty display text")
        if not isinstance(dirname, str) or not _DIR.fullmatch(dirname) or dirname in {".", ".."}:
            raise ValueError("project dirname must be one safe checkout directory name")
        if not isinstance(domain, str) or domain not in {"personal", "professional", "hobby"}:
            raise ValueError("project domain must be personal, professional or hobby")
        row = {"label": label.strip(), "dirname": dirname, "domain": domain}
        if "repo" in entry:
            repo = entry["repo"]
            if not isinstance(repo, str) or not _REPO.fullmatch(repo):
                raise ValueError("project repository must be owner/name without credentials")
            row["repo"] = repo.lower()
        if "default_branch" in entry:
            branch = entry["default_branch"]
            if (
                not isinstance(branch, str)
                or not branch
                or len(branch) > 255
                or any(c.isspace() or ord(c) < 32 for c in branch)
                or any(c in branch for c in "~^:?*[\\")
                or ".." in branch
                or "@{" in branch
                or branch.startswith(("-", "/", "."))
                or branch.endswith(("/", ".", ".lock"))
            ):
                raise ValueError("invalid project default branch")
            if (
                any(
                    not part or part.startswith(".") or part.endswith(".lock")
                    for part in branch.split("/")
                )
                or branch == "@"
            ):
                raise ValueError("invalid project default branch components")
            if "repo" not in row:
                raise ValueError("project default branch requires a repository")
            row["default_branch"] = branch
        result[project] = row
    if DEFAULT_PROJECT not in result:
        raise ValueError("project registry must retain the legacy default queue")
    return result


class ProjectIds(Sequence[str]):
    """A live compatibility view for callers that previously imported PROJECTS."""

    def __getitem__(self, index: Any) -> Any:
        return tuple(_configured_registry())[index]

    def __len__(self) -> int:
        return len(_configured_registry())

    def __iter__(self) -> Iterator[str]:
        return iter(_configured_registry())

    def __contains__(self, project: object) -> bool:
        return isinstance(project, str) and project in _configured_registry()

    def __repr__(self) -> str:
        return repr(tuple(self))


class ProjectField(Mapping[str, Any]):
    """Live dirname/label/repository views, without duplicated hardcoded lists."""

    def __init__(self, field: str):
        self.field = field

    def _values(self) -> dict[str, Any]:
        rows = registry()
        if self.field == "repo":
            return {
                project: {
                    key: value for key, value in row.items() if key in {"repo", "default_branch"}
                }
                for project, row in rows.items()
                if "repo" in row
            }
        return {project: row[self.field] for project, row in rows.items()}

    def __getitem__(self, project: str) -> Any:
        row = _configured_registry()[project]
        if self.field == "repo":
            if "repo" not in row:
                raise KeyError(project)
            return {key: row[key] for key in ("repo", "default_branch") if key in row}
        return row[self.field]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values())

    def __len__(self) -> int:
        return len(self._values())
