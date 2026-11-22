"""MCP tools of the ``dev-tools`` component.

Workspace developer tools: files, shell, git, worktrees, web, lint, export,
obsidian, doctor and service probes, bug worker, demos, bootstrap and backup.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from ..tools import (
    bug_worker,
    dash,
    devservers,
    export,
    files,
    lint,
    media,
    obsidian,
    red_team,
    repo_map,
    shell,
    web,
)
from ..tools import format as fmt
from .base import Component

COMPONENT = Component(
    name="dev-tools",
    tools=(
        web.web_search,
        web.web_fetch,
        dash.dash_list,
        dash.dash_search,
        dash.dash_view,
        files.fs_read,
        files.fs_write,
        files.fs_list,
        files.fs_search,
        shell.run_cmd,
        shell.run_cmd_check,
        devservers.detect_dev_servers,
        lint.python_lint_and_format,
        repo_map.fs_repo_map,
        export.export_tasks,
        export.export_data,
        export.export_memory,
        export.export_journal,
        export.export_migration_manifest,
        export.compare_migration_manifests,
        export.import_migration_manifest,
        export.rollback_migration_import,
        media.read_image,
        fmt.format_render,
        bug_worker.bug_worker_manage,
        red_team.red_team_qa,
        obsidian.obsidian_status,
        obsidian.obsidian_open,
        obsidian.obsidian_active_note,
        obsidian.obsidian_search,
        obsidian.obsidian_append,
    ),
)
