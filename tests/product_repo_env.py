"""Point the default project at a test checkout (task #2041)."""

import json
import os
from pathlib import Path


def point_default_project_at(repo: Path) -> None:
    """Make ``repo`` the default project's checkout for the current test.

    Claim-time worktrees are cut only from a project checkout, never from a
    bare workspace. Both variables are set by the autouse ``isolated_env``
    fixture's monkeypatch, so its teardown restores them.
    """
    os.environ["UA_DEV_ROOT"] = str(repo.parent)
    projects = json.loads(os.environ["UA_PROJECTS_JSON"])
    for project in projects:
        if project["id"] == "unified-agent":
            project["dirname"] = repo.name
    os.environ["UA_PROJECTS_JSON"] = json.dumps(projects)
