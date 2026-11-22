from unified_agent import project_bindings
from unified_agent.webui import project_registry


def test_registry_reports_checked_out_paths(tmp_path):
    (tmp_path / "unified_agent").mkdir()
    (tmp_path / "browser").mkdir()
    # sample-store and tap intentionally absent — held streams (task #331 decision).

    entries = project_registry.registry(dev_root=str(tmp_path))
    by_project = {e["project"]: e for e in entries}

    assert by_project["unified-agent"]["checked_out"] is True
    assert by_project["unified-agent"]["repo_path"] == str(tmp_path / "unified_agent")
    assert by_project["browser"]["checked_out"] is True

    assert by_project["sample-store"]["checked_out"] is False
    assert by_project["sample-store"]["repo_path"] is None
    assert by_project["client-portal"]["checked_out"] is False
    assert by_project["client-portal"]["repo_path"] is None


def test_registry_uses_expected_directory_names(tmp_path):
    (tmp_path / "tap").mkdir()
    (tmp_path / "sample-store").mkdir()

    entries = project_registry.registry(dev_root=str(tmp_path))
    by_project = {e["project"]: e for e in entries}

    assert by_project["client-portal"]["checked_out"] is True
    assert by_project["sample-store"]["checked_out"] is True


def test_registry_covers_every_known_project():
    entries = project_registry.registry(dev_root="/nonexistent")
    projects = {e["project"] for e in entries}
    assert projects == {"unified-agent", "browser", "sample-store", "client-portal"}
    assert all(e["checked_out"] is False and e["repo_path"] is None for e in entries)
    assert all(e["label"] for e in entries)


def test_registry_reports_config_seeded_repo_binding():
    entries = project_registry.registry(dev_root="/nonexistent")
    by_project = {e["project"]: e for e in entries}
    assert by_project["unified-agent"]["repo"] == "anonb3ll/unified-agent"
    assert by_project["unified-agent"]["repo_source"] == "config_default"
    assert by_project["browser"]["repo"] is None
    assert by_project["browser"]["repo_source"] is None


def test_registry_reports_db_binding_and_local_state(tmp_path):
    (tmp_path / "browser").mkdir()
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")

    entries = project_registry.registry(dev_root=str(tmp_path))
    by_project = {e["project"]: e for e in entries}

    assert by_project["browser"]["repo"] == "someowner/browser"
    assert by_project["browser"]["repo_source"] == "db"
    assert by_project["browser"]["repo_local_state"] == "checked_out"
    # No git repo initialized under tmp_path/browser, so origin can't be read.
    assert by_project["browser"]["repo_remote_matches"] is None

    assert by_project["sample-store"]["repo_local_state"] == "not_checked_out_here"
