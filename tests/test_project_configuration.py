"""Neutral defaults and installation-owned project routing boundaries."""

import json

import pytest

from unified_agent import config, project_bindings, projects, settings
from unified_agent.tools import memory, tasks
from unified_agent.webui import project_registry


def configure(monkeypatch, **changes):
    row = {
        "id": "client-one",
        "label": "Client One",
        "dirname": "client_checkout",
        "domain": "professional",
        **changes,
    }
    monkeypatch.setenv("UA_PROJECTS_JSON", json.dumps([{"id": "unified-agent"}, row]))


def test_fresh_install_has_no_private_portfolio_or_repo_seed(monkeypatch):
    monkeypatch.delenv("UA_PROJECTS_JSON", raising=False)
    assert tuple(tasks.PROJECTS) == ("unified-agent",)
    assert dict(config.PROJECT_REPO_SEED) == {}
    assert projects.registry()["unified-agent"]["dirname"] == "unified_agent"


def test_custom_stream_reaches_queue_domain_search_and_registry(monkeypatch, tmp_path):
    configure(monkeypatch, repo="ExampleOrg/ClientRepo", default_branch="main")
    monkeypatch.setenv("UA_DEV_ROOT", str(tmp_path))
    (tmp_path / "client_checkout").mkdir()
    task = tasks.task_add("Configured stream", project="client-one")
    rows = tasks.task_list(project="client-one", status="all")["tasks"]
    assert next(row for row in rows if row["id"] == task["id"])["domain"] == "professional"
    assert config.project_repo_path("client-one") == tmp_path / "client_checkout"
    assert {row["project"] for row in project_registry.registry()} == {
        "unified-agent",
        "client-one",
    }
    assert project_bindings.effective_binding("client-one")["repo"] == "exampleorg/clientrepo"
    assert {tuple(row.values()) for row in memory._extract_query_entities("client-one")} >= {
        ("project", "client-one"),
        ("service", "client-one"),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "../escape"},
        {"id": "all"},
        {"id": "unified-agent"},
        {"dirname": "../outside"},
        {"dirname": "/absolute"},
        {"dirname": "nested/child"},
        {"dirname": ".."},
        {"label": "\nHidden"},
        {"domain": "other"},
        {"domain": []},
        {"repo": "https://example.com/owner/repo"},
        {"repo": "user:password@host/repo"},
        {"default_branch": "--help"},
        {"repo": "owner/repo", "default_branch": "feature//topic"},
        {"repo": "owner/repo", "default_branch": "feature/.hidden"},
        {"repo": "owner/repo", "default_branch": "topic.lock/next"},
    ],
)
def test_invalid_installation_config_fails_before_routing(monkeypatch, changes):
    configure(monkeypatch, **changes)
    with pytest.raises(ValueError):
        projects.registry()


def test_existing_configured_stream_is_not_inferred_from_task_rows(monkeypatch):
    configure(monkeypatch)
    task = tasks.task_add("Existing stream", project="client-one")
    monkeypatch.delenv("UA_PROJECTS_JSON")
    assert tuple(tasks.PROJECTS) == ("unified-agent",)
    assert any(row["id"] == task["id"] for row in tasks.task_list(status="all")["tasks"])
    assert config.project_repo_path("client-one") is None


def test_checkout_symlink_cannot_extend_internal_project_roots(monkeypatch, tmp_path):
    configure(monkeypatch)
    root = tmp_path / "dev"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "client_checkout").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("UA_DEV_ROOT", str(root))
    assert config.project_repo_path("client-one") is None
    row = next(row for row in project_registry.registry() if row["project"] == "client-one")
    assert row["checked_out"] is False
    assert row["repo_path"] is None
    assert project_bindings.resolve("client-one")["local_path"] is None


def test_settings_project_tables_and_environment_precedence(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(
        'schema_version = 1\n[[projects.entries]]\nid = "unified-agent"\n[[projects.entries]]\nid = "client-one"\nlabel = "Client One"\ndirname = "client_checkout"\n'
    )
    env = {"UA_PROJECTS_JSON": '[{"id":"unified-agent"}]'}
    assert settings.apply_to_environ(env, path=path, quiet=True) == {}
    applied = settings.apply_to_environ({}, path=path, quiet=True)
    assert [row["id"] for row in json.loads(applied["UA_PROJECTS_JSON"])] == [
        "unified-agent",
        "client-one",
    ]


def test_id_only_default_preserves_neutral_checkout_name(monkeypatch):
    monkeypatch.setenv("UA_PROJECTS_JSON", '[{"id":"unified-agent"}]')
    assert projects.registry()["unified-agent"]["dirname"] == "unified_agent"
    assert projects.registry()["unified-agent"]["label"] == "Unified Agent"


def test_registry_views_parse_once_and_do_not_expose_mutable_cache(monkeypatch):
    configure(monkeypatch)
    projects._parse_registry.cache_clear()
    assert tuple(tasks.PROJECTS) == ("unified-agent", "client-one")
    assert "client-one" in tasks.PROJECTS
    assert config.PROJECT_REPO_DIRNAMES["client-one"] == "client_checkout"
    assert projects._parse_registry.cache_info().misses == 1
    public = projects.registry()
    public["client-one"]["dirname"] = "changed"
    assert config.PROJECT_REPO_DIRNAMES["client-one"] == "client_checkout"
    monkeypatch.setenv("UA_PROJECTS_JSON", '[{"id":"unified-agent"}]')
    assert "client-one" not in tasks.PROJECTS


def test_checkout_symlink_policy_is_consistent_inside_root(monkeypatch, tmp_path):
    configure(monkeypatch)
    (tmp_path / "real_checkout").mkdir()
    (tmp_path / "client_checkout").symlink_to(tmp_path / "real_checkout", target_is_directory=True)
    monkeypatch.setenv("UA_DEV_ROOT", str(tmp_path))
    assert config.project_repo_path("client-one") is None
    assert project_bindings.resolve("client-one")["local_path"] is None
    assert not next(row for row in project_registry.registry() if row["project"] == "client-one")[
        "checked_out"
    ]
