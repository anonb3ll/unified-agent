"""Excalidraw planning-canvas tools (task #807).

The Control Center's Canvas tab carries an Excalidraw pane where the operator
sketches agents/tasks/workflows as labeled boxes and arrows. These tools let
an agent read that scene and turn text labels into structured task/dependency
SUGGESTIONS — and, when asked, write an updated scene back.

Hard boundary: nothing here creates a task, an edge, or a delegation. The
parser output is a proposal payload for a human; execution would only ever
happen through the existing approval queue (`task_add` with review, or
`dependency_add` by an approved actor). This module deliberately has no call
path into `tasks`/`delegate`/`workflow`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from typing_extensions import TypedDict

from .. import config, storage

# Boards are opaque slugs; keep them filesystem/URL-safe and bounded.
_BOARD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SCENE_MAX_BYTES = 2_000_000  # mirrors fs_write's per-call cap

# Arrow-ish Excalidraw types that can carry a dependency edge. `arrow` covers
# the standard single-headed arrow; line/curve are treated as edges only when
# they carry a start/end binding, which the parser checks via `points`.
_EDGE_ELEMENT_TYPES = frozenset({"arrow", "line"})

# Labels that should not become task proposals: the sketch is planning-space,
# not a task list dump. Kept minimal and explicit.
_TITLE_MAX_CHARS = 200


class CanvasTaskSuggestion(TypedDict):
    id: str
    title: str
    excalidraw_id: str


class CanvasDependencySuggestion(TypedDict):
    from_id: str
    from_title: str
    to_id: str
    to_title: str


class CanvasParsed(TypedDict):
    node_count: int
    edge_count: int
    unparsed_count: int
    task_suggestions: list[CanvasTaskSuggestion]
    dependency_suggestions: list[CanvasDependencySuggestion]


class CanvasReadResult(TypedDict):
    board_id: str
    exists: bool
    updated_by: str | None
    updated_at: str | None
    scene: dict[str, Any]
    parsed: CanvasParsed


def _require_board_id(board_id: str) -> str:
    board = (board_id or "").strip().lower()
    if not _BOARD_ID_RE.match(board):
        raise ToolError(
            "board_id must match [a-z0-9][a-z0-9._-]{0,63} (e.g. 'default', 'sprint-42')"
        )
    return board


def _identity() -> str:
    verified = config.verified_agent_identity()
    if not verified or not config.is_known_agent_label(verified):
        raise ToolError("an authenticated known agent identity is required")
    return verified


def _scene_elements(scene: dict) -> list[dict]:
    elements = scene.get("elements")
    if not isinstance(elements, list):
        return []
    return [el for el in elements if isinstance(el, dict)]


def parse_canvas_labels(scene: dict) -> dict:
    """Parse labeled Excalidraw elements into task/dependency suggestions.

    Deterministic, read-only, no model call. Rules:

    - A rectangle/ellipse/diamond whose bound text child (or `label` attr) is
      non-empty becomes a ``task_suggestion`` (title = first line, truncated).
    - An arrow/line whose endpoints bind to two labeled nodes becomes a
      ``dependency_suggestion`` (from → to, i.e. ``to`` depends on ``from``).
    - Standalone text (no container) is sketch annotation — a canvas title,
      not a task — so it is counted under ``unparsed`` and never suggested.
    - Unbound arrows, empty labels, and non-graph elements are reported under
      ``unparsed`` counts so nothing silently disappears.

    Output shape (SUGGESTION ONLY — a human approves every downstream action):
    ``{node_count, edge_count, unparsed_count,
       task_suggestions: [{id, title, excalidraw_id}],
       dependency_suggestions: [{from_id, from_title, to_id, to_title}]}``
    """
    elements = _scene_elements(scene)
    if len(elements) > 1000:
        raise ToolError("scene exceeds 1000 elements; split the board")

    # Excalidraw binds text to its container via containerId; that bound text
    # is the shape's label. Standalone text (no container) is annotation.
    text_by_container: dict[str, str] = {}
    unparsed_standalone_text = 0
    for el in elements:
        if str(el.get("type") or "") != "text":
            continue
        text = str(el.get("text") or "").strip()
        if not text:
            continue
        container = el.get("containerId")
        if isinstance(container, str) and container:
            text_by_container[container] = text
        else:
            unparsed_standalone_text += 1

    def _label(el: dict) -> str:
        text = text_by_container.get(str(el.get("id", "")), "")
        if text:
            return text
        return str(el.get("label") or "").strip()

    def _title(text: str) -> str:
        first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
        return first_line[:_TITLE_MAX_CHARS]

    nodes_by_id: dict[str, dict] = {}
    node_count = 0
    unparsed = unparsed_standalone_text
    for el in elements:
        el_type = str(el.get("type") or "")
        el_id = str(el.get("id") or "")
        if el_type == "text":
            continue  # bound text is its container's label; standalone already counted
        if el_type in ("rectangle", "ellipse", "diamond"):
            node_count += 1
            label = _label(el)
            if not label or not el_id:
                unparsed += 1
                continue
            nodes_by_id[el_id] = {
                "id": el_id,
                "title": _title(label),
                "excalidraw_id": el_id,
            }
        else:
            continue

    task_suggestions = list(nodes_by_id.values())

    dependency_suggestions: list[dict] = []
    edge_count = 0
    for el in elements:
        if str(el.get("type") or "") not in _EDGE_ELEMENT_TYPES:
            continue
        edge_count += 1
        start_id = str((el.get("startBinding") or {}).get("elementId") or "")
        end_id = str((el.get("endBinding") or {}).get("elementId") or "")
        if not start_id or not end_id:
            unparsed += 1
            continue
        source = nodes_by_id.get(start_id)
        target = nodes_by_id.get(end_id)
        if source is None or target is None or source["id"] == target["id"]:
            unparsed += 1
            continue
        dependency_suggestions.append(
            {
                "from_id": source["id"],
                "from_title": source["title"],
                "to_id": target["id"],
                "to_title": target["title"],
            }
        )

    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "unparsed_count": unparsed,
        "task_suggestions": task_suggestions,
        "dependency_suggestions": dependency_suggestions,
    }


def graph_canvas_read(board_id: str = "default") -> CanvasReadResult:
    """Read the stored Excalidraw scene for a board and parse labels.

    Returns the raw `scene` (Excalidraw elements JSON) plus `parsed`, the
    deterministic task/dependency suggestions from `parse_canvas_labels`.
    Suggestions are PROPOSALS ONLY: nothing is created until a human approves
    the follow-up work through the normal task/approval flow.

    - **Errors:** unknown/empty board id; identity mismatch under HTTP auth.
    - **Returns:** `{board_id, exists, updated_by, updated_at, scene, parsed}`;
      `exists=False` with an empty scene and zeroed parse counts when the board
      has never been written (first sketch starts from nothing).
    """
    board = _require_board_id(board_id)
    row = storage.get_graph_canvas_scene(board)
    if row is None:
        return {
            "board_id": board,
            "exists": False,
            "updated_by": None,
            "updated_at": None,
            "scene": {},
            "parsed": parse_canvas_labels({}),
        }
    return {
        "board_id": board,
        "exists": True,
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
        "scene": row["scene"],
        "parsed": parse_canvas_labels(row["scene"]),
    }


def graph_canvas_write(
    board_id: str = "default",
    scene: dict | None = None,
    agent: str | None = None,
) -> dict:
    """Write an Excalidraw scene JSON for a board, attributed to the caller.

    The scene is stored verbatim (validated as a JSON object with an optional
    `elements` array; ≤2 MB). Writing does NOT parse or act on the sketch —
    pair with `graph_canvas_read` for suggestions. Attribution is always the
    verified caller identity; an optional `agent` label must match it.

    - **Errors:** invalid board id; scene not a JSON object; oversized scene;
      unauthenticated or unknown caller identity; `agent` spoof.
    - **Returns:** `{board_id, saved: True, updated_by, updated_at, size_bytes}`.
    """
    actor = _identity()
    if agent is not None and agent.strip() and agent.strip() != actor:
        raise ToolError("agent must match the authenticated caller")
    board = _require_board_id(board_id)
    if not isinstance(scene, dict):
        raise ToolError("scene must be a JSON object (Excalidraw scene export)")
    elements = scene.get("elements", [])
    if not isinstance(elements, list):
        raise ToolError("scene.elements must be an array of Excalidraw elements")
    if len(elements) > 1000:
        raise ToolError("scene exceeds 1000 elements; split the board")
    size = len(json.dumps(scene, sort_keys=True).encode("utf-8"))
    if size > _SCENE_MAX_BYTES:
        raise ToolError(f"scene too large: {size} bytes (limit {_SCENE_MAX_BYTES})")

    result = storage.put_graph_canvas_scene(board, scene, actor)
    return {
        "board_id": result["board_id"],
        "saved": True,
        "updated_by": result["updated_by"],
        "updated_at": result["updated_at"],
        "size_bytes": size,
    }
