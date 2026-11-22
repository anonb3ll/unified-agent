"""Per-project delivery modes (task #1026)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from unified_agent.delivery import (
    DeliveryPolicy,
    annotate_notes,
    apply_finish_markers,
    load_delivery_registry,
    resolve_delivery,
)
from unified_agent.webui import project_registry


def test_default_is_no_mistakes_without_yolo(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_PROJECT_DELIVERY_FILE", str(tmp_path / "missing.yaml"))
    policy = resolve_delivery("unified-agent")
    assert policy.mode == "no-mistakes"
    assert policy.yolo is False
    assert policy.source == "default"


def test_file_overrides_per_project(tmp_path, monkeypatch):
    path = tmp_path / "project_delivery.yaml"
    path.write_text(
        yaml.dump(
            {
                "default": {"mode": "no-mistakes", "yolo": False},
                "projects": {
                    "browser": {"mode": "local-only", "yolo": True},
                    "unified-agent": "direct-pr+yolo",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_PROJECT_DELIVERY_FILE", str(path))

    exo = resolve_delivery("browser")
    assert exo.mode == "local-only"
    assert exo.yolo is True
    assert exo.source == "file"

    ua = resolve_delivery("unified-agent")
    assert ua.mode == "direct-pr"
    assert ua.yolo is True
    assert ua.source == "file"

    other = resolve_delivery("sample-store")
    assert other.mode == "no-mistakes"
    assert other.yolo is False
    assert other.source == "default"


def test_note_override_beats_file(tmp_path, monkeypatch):
    path = tmp_path / "project_delivery.yaml"
    path.write_text(
        yaml.dump({"projects": {"unified-agent": {"mode": "direct-pr", "yolo": False}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_PROJECT_DELIVERY_FILE", str(path))
    notes = "work\n[delivery: mode=local-only yolo=on]\n"
    policy = resolve_delivery("unified-agent", notes=notes)
    assert policy.mode == "local-only"
    assert policy.yolo is True
    assert policy.source == "note"


def test_malformed_yaml_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "project_delivery.yaml"
    path.write_text("[[[not yaml", encoding="utf-8")
    monkeypatch.setenv("UA_PROJECT_DELIVERY_FILE", str(path))
    policy = resolve_delivery("unified-agent")
    assert policy.mode == "no-mistakes"
    assert policy.yolo is False


def test_apply_finish_markers_stamps_review_and_mode():
    from unified_agent.delivery import DeliveryPolicy

    local = DeliveryPolicy(mode="local-only", yolo=False, project="browser", source="file")
    stamped = apply_finish_markers("did the work", local)
    assert "[waiting-for: review]" in stamped
    assert "[delivery-run: mode=local-only yolo=off]" in stamped
    assert "no remote PR expected" in stamped

    direct = DeliveryPolicy(mode="direct-pr", yolo=True, project="ua", source="file")
    stamped2 = apply_finish_markers("", direct)
    assert "[waiting-for: review]" in stamped2
    assert "prefer-pr" in stamped2
    assert direct.marker() in stamped2


def test_annotate_notes_replaces_prior_marker():
    from unified_agent.delivery import DeliveryPolicy

    first = annotate_notes("hi", DeliveryPolicy(mode="direct-pr", yolo=False))
    second = annotate_notes(first, DeliveryPolicy(mode="local-only", yolo=True))
    assert second.count("[delivery-run: mode=") == 1
    assert "local-only" in second
    assert "yolo=on" in second


def test_project_registry_exposes_delivery(tmp_path, monkeypatch):
    path = tmp_path / "project_delivery.yaml"
    path.write_text(
        yaml.dump({"projects": {"unified-agent": {"mode": "direct-pr", "yolo": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_PROJECT_DELIVERY_FILE", str(path))
    (tmp_path / "unified_agent").mkdir()
    entries = project_registry.registry(dev_root=str(tmp_path))
    by_project = {e["project"]: e for e in entries}
    assert by_project["unified-agent"]["delivery_mode"] == "direct-pr"
    assert by_project["unified-agent"]["delivery_yolo"] is True
    assert by_project["browser"]["delivery_mode"] == "no-mistakes"
    assert by_project["browser"]["delivery_yolo"] is False


def test_load_delivery_registry_missing(tmp_path):
    data = load_delivery_registry(Path(tmp_path / "nope.yaml"))
    assert data["default"]["mode"] == "no-mistakes"
    assert data["projects"] == {}


def test_annotate_notes_keeps_operator_override_beside_the_generated_stamp():
    from unified_agent.delivery import DeliveryPolicy

    notes = "Before [delivery: mode=direct-pr yolo=on] after"
    stamped = annotate_notes(notes, DeliveryPolicy(mode="local-only", yolo=False))
    assert "[delivery: mode=direct-pr yolo=on]" in stamped
    assert stamped.count("[delivery-run: mode=local-only yolo=off]") == 1


def test_generated_stamp_is_not_an_operator_override(tmp_path, monkeypatch):
    path = tmp_path / "project_delivery.yaml"
    path.write_text(
        yaml.dump({"projects": {"p": {"mode": "direct-pr", "yolo": False}}}), encoding="utf-8"
    )
    monkeypatch.setenv("UA_PROJECT_DELIVERY_FILE", str(path))
    stamped = apply_finish_markers("work", DeliveryPolicy(mode="local-only", yolo=True))
    assert "[delivery-run: mode=local-only yolo=on]" in stamped
    policy = resolve_delivery("p", notes=stamped)
    assert (policy.mode, policy.yolo, policy.source) == ("direct-pr", False, "file")


def _yolo_registry(tmp_path, monkeypatch, project_yolo):
    entry = {"mode": "local-only"}
    if project_yolo is not None:
        entry["yolo"] = project_yolo
    path = tmp_path / "project_delivery.yaml"
    path.write_text(yaml.dump({"projects": {"p": entry}}), encoding="utf-8")
    monkeypatch.setenv("UA_PROJECT_DELIVERY_FILE", str(path))


@pytest.mark.parametrize(
    "project_yolo, org_yolo, note, expected",
    [
        (False, True, "[delivery: mode=direct-pr]", False),
        (True, False, "[delivery: mode=direct-pr]", True),
        (None, True, "[delivery: mode=direct-pr]", True),
        (None, False, "[delivery: mode=direct-pr]", False),
        (True, True, "[delivery: mode=direct-pr yolo=off]", False),
        (False, False, "[delivery: mode=direct-pr yolo=on]", True),
    ],
)
def test_note_yolo_beats_project_yolo_beats_profile_yolo(
    tmp_path, monkeypatch, project_yolo, org_yolo, note, expected
):
    _yolo_registry(tmp_path, monkeypatch, project_yolo)
    snapshot = {
        "settings": {"delivery": {"yolo": org_yolo}},
        "sources": {"delivery.yolo": "org:standard"},
    }
    policy = resolve_delivery("p", notes=note, snapshot=snapshot)
    assert policy.mode == "direct-pr" and policy.source == "note"
    assert policy.yolo is expected


def test_mode_only_note_with_no_yolo_anywhere_stays_off(tmp_path, monkeypatch):
    _yolo_registry(tmp_path, monkeypatch, None)
    policy = resolve_delivery("p", notes="[delivery: mode=direct-pr]")
    assert policy.yolo is False
