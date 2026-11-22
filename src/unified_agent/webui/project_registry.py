"""Device-local project registry for the Command Center (task #356).

Resolves each known project stream against this host's ~/dev checkouts so the
Command Center can show a real target path — or an honest "not checked out
here" — before a session is dispatched, instead of a stream name with no
grounding in what actually exists on this machine.

Existence checks only (`os.path.isdir`). This never lists directory contents
or reads outside the fixed, hardcoded path per project — it cannot be used to
browse the filesystem, and it grants no reach beyond what these five paths
already are.
"""

from __future__ import annotations

import os

from .. import config, project_bindings
from ..delivery import resolve_delivery
from ..tools.tasks import PROJECTS

PROJECT_LABELS = config._projects.ProjectField("label")


def registry(dev_root: str | None = None) -> list[dict]:
    """Return one entry per known project stream, resolved against `dev_root`.

    `dev_root` defaults to `~/dev` on this host; tests pass a tmp_path so the
    result does not depend on what happens to be checked out on whatever
    machine runs the suite.

    Dirname mapping is config.PROJECT_REPO_DIRNAMES — the same source of
    truth task #371's executor worktree routing uses, so this display and
    that execution behavior can't drift apart (previously this module kept
    its own separate copy of the same dict).

    Each entry also carries the resolved delivery mode / yolo flag
    (task #1026) so the Command Center can show per-project ship posture.

    Task #1425: each entry also carries the project's bound source repository
    (DB binding, or a config-seeded default -- see ``project_bindings.py``),
    resolved alongside the existing local path with an honest
    ``repo_local_state`` of ``"checked_out"``, ``"not_checked_out_here"``, or
    ``"unknown"``, plus ``repo_remote_matches`` (bool|None). This never widens
    what this module can see on disk: the binding is display-only data, and
    the only added filesystem reach is a direct, non-following read of the
    checkout's own ``.git/config`` file (see
    ``project_bindings._read_git_origin``), not listing or opening anything
    else inside it.
    """
    root = dev_root if dev_root is not None else str(config.dev_root())
    entries = []
    for project in PROJECTS:
        dirname = config.PROJECT_REPO_DIRNAMES.get(project, project)
        path = os.path.join(root, dirname)
        checked_out = (
            not os.path.islink(path)
            and os.path.isdir(path)
            and os.path.commonpath((os.path.realpath(root), os.path.realpath(path)))
            == os.path.realpath(root)
        )
        delivery = resolve_delivery(project)
        repo_resolution = project_bindings.resolve(project, dev_root=root)
        entries.append(
            {
                "project": project,
                "label": PROJECT_LABELS.get(project, project),
                "repo_path": path if checked_out else None,
                "checked_out": checked_out,
                "delivery_mode": delivery.mode,
                "delivery_yolo": delivery.yolo,
                "delivery_source": delivery.source,
                "repo": repo_resolution["repo"],
                "repo_source": repo_resolution["repo_source"],
                "repo_default_branch": repo_resolution["default_branch"],
                "repo_local_state": repo_resolution["local_state"],
                "repo_remote_matches": repo_resolution["remote_matches"],
            }
        )
    return entries
