"""An added, removed or re-profiled MCP tool shows up as a reviewed diff (task #2416)."""

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).parent.parent / "scripts" / "tool_surface_baseline.py"
_SPEC = importlib.util.spec_from_file_location("tool_surface_baseline", _PATH)
baseline = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(baseline)


def test_the_tool_surface_matches_its_committed_baseline():
    differences = baseline.drift(baseline.load(), baseline.current())
    assert not differences, (
        "The MCP tool surface changed without a baseline update:\n  "
        + "\n  ".join(differences)
        + f"\nIf deliberate, run: {baseline.UPDATE_COMMAND}"
    )


def test_every_kind_of_change_is_reported():
    before = {"kept": ["developer"], "gone": ["developer"], "moved": ["developer"]}
    after = {"kept": ["developer"], "new": ["steward"], "moved": ["steward"]}
    assert baseline.drift(before, after) == [
        "added: new",
        "removed: gone",
        "re-profiled: moved gained ['steward'] lost ['developer']",
    ]


def test_a_missing_baseline_reports_every_tool(tmp_path):
    assert baseline.load(tmp_path / "absent.json") == {}
    assert baseline.drift({}, {"a": []}) == ["added: a"]


def test_the_rendered_baseline_round_trips_one_tool_per_line():
    surface = {"b": ["steward"], "a": ["developer", "steward"]}
    text = baseline.render(surface)
    assert json.loads(text) == surface
    assert len(text.splitlines()) == len(surface) + 2


def test_check_mode_names_the_update_command(monkeypatch, capsys):
    monkeypatch.setattr(baseline, "load", lambda: {})
    monkeypatch.setattr(baseline, "current", lambda: {"new_tool": ["developer"]})
    assert baseline.main([]) == 1
    out = capsys.readouterr().out
    assert "added: new_tool" in out and baseline.UPDATE_COMMAND in out
