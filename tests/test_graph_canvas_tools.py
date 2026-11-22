"""Tests for the Excalidraw planning-canvas MCP tools (task #807).

`graph_canvas_read`/`graph_canvas_write` are the agent surface for the
Control Center's planning canvas. The label parser must be deterministic and
read-only: it turns labeled shapes + bound arrows into task/dependency
SUGGESTIONS — never into tasks, edges, or delegations.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tools import graph_canvas


def _rect(el_id: str) -> dict:
    return {"id": el_id, "type": "rectangle"}


def _text(el_id: str, container_id: str, text: str) -> dict:
    return {"id": el_id, "type": "text", "containerId": container_id, "text": text}


def _arrow(el_id: str, start: str, end: str) -> dict:
    return {
        "id": el_id,
        "type": "arrow",
        "startBinding": {"elementId": start},
        "endBinding": {"elementId": end},
    }


class TestParseCanvasLabels:
    def test_empty_scene_parses_to_zeroed_counts(self) -> None:
        parsed = graph_canvas.parse_canvas_labels({})
        assert parsed["node_count"] == 0
        assert parsed["edge_count"] == 0
        assert parsed["unparsed_count"] == 0
        assert parsed["task_suggestions"] == []
        assert parsed["dependency_suggestions"] == []

    def test_labeled_boxes_and_bound_arrows_become_suggestions(self) -> None:
        scene = {
            "elements": [
                _rect("n1"),
                _text("t1", "n1", "Write spec"),
                _rect("n2"),
                _text("t2", "n2", "Review spec"),
                _rect("n3"),
                _text("t3", "n3", "Ship"),
                _arrow("e1", "n1", "n2"),
                _arrow("e2", "n2", "n3"),
            ]
        }
        parsed = graph_canvas.parse_canvas_labels(scene)
        assert parsed["node_count"] == 3
        assert parsed["edge_count"] == 2
        assert parsed["unparsed_count"] == 0
        assert [t["title"] for t in parsed["task_suggestions"]] == [
            "Write spec",
            "Review spec",
            "Ship",
        ]
        # from → to means "to depends on from": Review depends on Write.
        deps = parsed["dependency_suggestions"]
        assert (deps[0]["from_title"], deps[0]["to_title"]) == (
            "Write spec",
            "Review spec",
        )
        assert (deps[1]["from_title"], deps[1]["to_title"]) == ("Review spec", "Ship")
        # excalidraw ids ride along so the human can find the elements.
        assert parsed["task_suggestions"][0]["excalidraw_id"] == "n1"
        assert deps[0]["from_id"] == "n1" and deps[0]["to_id"] == "n2"

    def test_unbound_arrow_counts_as_unparsed_not_edge(self) -> None:
        scene = {
            "elements": [
                {"id": "n1", "type": "rectangle"},
                {"id": "t1", "type": "text", "containerId": "n1", "text": "Solo"},
                {"id": "e1", "type": "arrow", "startBinding": {"elementId": "n1"}},
            ]
        }
        parsed = graph_canvas.parse_canvas_labels(scene)
        assert parsed["node_count"] == 1
        assert parsed["edge_count"] == 1
        assert parsed["unparsed_count"] == 1
        assert parsed["dependency_suggestions"] == []
        assert len(parsed["task_suggestions"]) == 1

    def test_unlabeled_shape_counts_unparsed_and_is_skipped(self) -> None:
        scene = {"elements": [{"id": "n9", "type": "rectangle"}]}
        parsed = graph_canvas.parse_canvas_labels(scene)
        assert parsed["node_count"] == 1
        assert parsed["unparsed_count"] == 1
        assert parsed["task_suggestions"] == []

    def test_multiline_label_uses_first_line_truncated(self) -> None:
        long_first = "x" * 260
        scene = {
            "elements": [
                {"id": "n1", "type": "rectangle"},
                {
                    "id": "t1",
                    "type": "text",
                    "containerId": "n1",
                    "text": f"{long_first}\nsecond line",
                },
            ]
        }
        parsed = graph_canvas.parse_canvas_labels(scene)
        title = parsed["task_suggestions"][0]["title"]
        assert title == long_first[:200]
        assert "\n" not in title

    def test_self_edge_is_unparsed(self) -> None:
        scene = {
            "elements": [
                {"id": "n1", "type": "rectangle"},
                {"id": "t1", "type": "text", "containerId": "n1", "text": "Self"},
                _arrow("e1", "n1", "n1"),
            ]
        }
        parsed = graph_canvas.parse_canvas_labels(scene)
        assert parsed["edge_count"] == 1
        assert parsed["unparsed_count"] == 1
        assert parsed["dependency_suggestions"] == []

    def test_oversized_scene_rejected(self) -> None:
        scene = {"elements": [{"id": f"n{i}", "type": "rectangle"} for i in range(1001)]}
        with pytest.raises(ToolError):
            graph_canvas.parse_canvas_labels(scene)

    def test_standalone_text_is_annotation_not_a_task(self) -> None:
        # A title floating on the canvas is annotation — it must never become
        # a task suggestion just because it carries words.
        scene = {"elements": [{"id": "s1", "type": "text", "text": "Sprint 42 sketch"}]}
        parsed = graph_canvas.parse_canvas_labels(scene)
        assert parsed["task_suggestions"] == []
        assert parsed["unparsed_count"] == 1

    def test_bound_text_child_does_not_double_count(self) -> None:
        # The text child of a shape is that shape's label — one node, not two.
        scene = {
            "elements": [
                {"id": "n1", "type": "rectangle"},
                {"id": "t1", "type": "text", "containerId": "n1", "text": "One node"},
            ]
        }
        parsed = graph_canvas.parse_canvas_labels(scene)
        assert parsed["node_count"] == 1
        assert parsed["unparsed_count"] == 0
        assert [t["title"] for t in parsed["task_suggestions"]] == ["One node"]


class TestGraphCanvasReadWrite:
    def setup_method(self) -> None:
        storage.init_db()

    def test_never_written_board_reads_as_exists_false(self) -> None:
        result = graph_canvas.graph_canvas_read(board_id="t807-never")
        assert result["exists"] is False
        assert result["scene"] == {}
        assert result["parsed"]["task_suggestions"] == []

    def test_write_then_read_round_trip(self) -> None:
        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            self._round_trip()
        finally:
            config.agent_identity_var.reset(token)

    def _round_trip(self) -> None:
        scene = {
            "type": "excalidraw",
            "elements": [
                {"id": "n1", "type": "rectangle"},
                {"id": "t1", "type": "text", "containerId": "n1", "text": "Plan A"},
            ],
        }
        saved = graph_canvas.graph_canvas_write(
            board_id="t807-roundtrip", scene=scene, agent="Desktop_Agent1"
        )
        assert saved["saved"] is True
        assert saved["updated_by"] == "Desktop_Agent1"

        result = graph_canvas.graph_canvas_read(board_id="t807-roundtrip")
        assert result["exists"] is True
        assert result["scene"] == scene
        assert [t["title"] for t in result["parsed"]["task_suggestions"]] == ["Plan A"]

    def test_write_requires_scene_object(self) -> None:
        with pytest.raises(ToolError):
            graph_canvas.graph_canvas_write(board_id="t807-bad", scene=None)
        with pytest.raises(ToolError):
            graph_canvas.graph_canvas_write(board_id="t807-bad", scene=[1, 2])

    def test_write_rejects_bad_elements_shape(self) -> None:
        with pytest.raises(ToolError):
            graph_canvas.graph_canvas_write(
                board_id="t807-bad", scene={"elements": {"not": "an array"}}
            )

    def test_write_rejects_oversized_element_count(self) -> None:
        scene = {"elements": [{"id": f"n{i}", "type": "rectangle"} for i in range(1001)]}
        with pytest.raises(ToolError):
            graph_canvas.graph_canvas_write(board_id="t807-big", scene=scene)

    def test_board_id_validation(self) -> None:
        with pytest.raises(ToolError):
            graph_canvas.graph_canvas_read(board_id="../etc")
        with pytest.raises(ToolError):
            graph_canvas.graph_canvas_read(board_id="")
        with pytest.raises(ToolError):
            graph_canvas.graph_canvas_write(board_id="Bad Board", scene={})

    def test_write_overwrites_prior_scene(self) -> None:
        scene = {"elements": [{"id": "n1", "type": "rectangle"}]}
        token = config.agent_identity_var.set("Desktop_agent3")
        try:
            graph_canvas.graph_canvas_write(board_id="t807-overwrite", scene={"elements": []})
            graph_canvas.graph_canvas_write(board_id="t807-overwrite", scene=scene)
        finally:
            config.agent_identity_var.reset(token)
        result = graph_canvas.graph_canvas_read(board_id="t807-overwrite")
        assert result["updated_by"] == "Desktop_agent3"
        assert len(result["scene"]["elements"]) == 1
