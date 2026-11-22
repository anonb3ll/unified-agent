"""Doctor reports configuration and enforcement coverage without enabling presets."""

from unified_agent.tools import doctor


def _policy(tmp_path, monkeypatch, content):
    path = tmp_path / "policy.yaml"
    path.write_text(content)
    monkeypatch.setenv("UA_POLICY_FILE", str(path))


def test_unconfigured_presets_are_dormant(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, "version: 1\nrules: []\n")
    assert doctor._trust_presets_check()["status"] == "DORMANT"


def test_configured_presets_report_native_refusal(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, "version: 1\ntrust_presets:\n  default: inspection\n")
    row = doctor._trust_presets_check()
    assert row["status"] == "LIVE"
    assert "inspection" in row["detail"]
    assert "native launches denied" in row["detail"]
    assert "#1355" in row["detail"]


def test_invalid_presets_are_misconfigured(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, "version: 1\ntrust_presets:\n  default: arbitrary-root\n")
    assert doctor._trust_presets_check()["status"] == "MISCONFIGURED"


def test_doctor_includes_preset_coverage(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_POLICY_FILE", str(isolated_env / "absent-policy.yaml"))
    report = doctor.system_doctor()
    assert any(row["capability"] == "trust_presets" for row in report["checks"])
