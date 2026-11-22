import json
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, safety

FIXTURE_PATH = (
    Path(__file__).parents[1] / "packages" / "conformance" / "fixtures" / "path-decisions.json"
)


def _reference_decision(path: str, workspace: Path) -> dict[str, object]:
    token = config.workspace_root_var.set(workspace)
    try:
        try:
            safety.resolve_in_workspace(path, use_worktree=False)
        except ToolError:
            return {
                "allow": False,
                "reason": "path-escapes-workspace",
                "rule": "workspace-containment",
                "tool": "resolve_in_workspace",
            }
        return {
            "allow": True,
            "reason": "path-resolved-inside-workspace",
            "rule": "workspace-containment",
            "tool": "resolve_in_workspace",
        }
    finally:
        config.workspace_root_var.reset(token)


def test_python_reference_matches_shared_path_fixture(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "conformance-fixtures").mkdir(parents=True)
    outside.mkdir()
    (workspace / "conformance-fixtures" / "outside-link").symlink_to(
        outside, target_is_directory=True
    )

    for case in fixture["cases"]:
        assert _reference_decision(case["path"], workspace) == case["expected"]
