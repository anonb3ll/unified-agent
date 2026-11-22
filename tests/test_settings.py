"""settings.toml personalization and configurable operator identity."""

import json
import os
import sqlite3

import pytest

from unified_agent import bootstrap, config, personas, profiles, service_env, settings


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    monkeypatch.setenv("UA_SETTINGS_FILE", str(path))
    return path


@pytest.fixture
def neutral_operator(monkeypatch):
    """Undo the suite-wide legacy operator pin from conftest."""
    for name in ("UA_OPERATOR_LABEL", "UA_OPERATOR_NAME", "UA_OPERATOR_ALIASES"):
        monkeypatch.delenv(name, raising=False)


# --- file location ---------------------------------------------------------


def test_settings_path_defaults_to_ua_home(tmp_path):
    env = {"UA_HOME": str(tmp_path)}
    assert settings.settings_path(env) == tmp_path / "settings.toml"


def test_empty_settings_file_var_disables_the_file(tmp_path):
    assert settings.settings_path({"UA_HOME": str(tmp_path), "UA_SETTINGS_FILE": ""}) is None


def test_missing_file_is_empty(tmp_path):
    assert settings.load(tmp_path / "nope.toml") == {}


# --- parsing and precedence ------------------------------------------------


def test_typed_settings_map_to_env(settings_file):
    settings_file.write_text(
        """
schema_version = 1
[operator]
name = "Alex"
label = "Desktop_Alex"
aliases = ["alex", "A. Example"]
[paths]
workspace = "~/agents"
vault = ""
vault_write_dirs = ["Inbox", "Logs"]
[server]
port = 9000
[agents]
labels = ["Local_Worker", "Desktop_Agent2"]
"""
    )
    values, warnings = settings.to_env(settings.load(settings_file))
    assert warnings == []
    assert values == {
        "UA_OPERATOR_NAME": "Alex",
        "UA_OPERATOR_LABEL": "Desktop_Alex",
        "UA_OPERATOR_ALIASES": "alex,A. Example",
        "UA_WORKSPACE": "~/agents",
        "UA_VAULT_PATH": "",
        "UA_VAULT_WRITE_DIRS": "Inbox:Logs",
        "UA_HTTP_PORT": "9000",
        "UA_AGENT_LABELS": "Local_Worker:Desktop_Agent2",
    }


def test_environment_wins_over_settings(settings_file):
    settings_file.write_text('[operator]\nname = "FromFile"\nlabel = "Desktop_File"\n')
    env = {"UA_SETTINGS_FILE": str(settings_file), "UA_OPERATOR_NAME": "FromEnv"}
    applied = settings.apply_to_environ(env)
    assert env["UA_OPERATOR_NAME"] == "FromEnv"
    assert env["UA_OPERATOR_LABEL"] == "Desktop_File"
    assert applied == {"UA_OPERATOR_LABEL": "Desktop_File"}


def test_env_table_passthrough_and_secret_refusal(settings_file, capsys):
    settings_file.write_text(
        """
[env]
UA_EDITION = "community"
UA_STATE_SYNC = false
UA_TOKEN_MAX_AGE_DAYS = 30
UA_AGENT_TOKENS = "me:hunter2"
UA_GEMINI_API_KEY = "nope"
PATH = "/tmp/evil"
LD_PRELOAD = "/tmp/evil.so"
"""
    )
    env = {"UA_SETTINGS_FILE": str(settings_file)}
    applied = settings.apply_to_environ(env)
    assert applied == {
        "UA_EDITION": "community",
        "UA_STATE_SYNC": "0",
        "UA_TOKEN_MAX_AGE_DAYS": "30",
    }
    err = capsys.readouterr().err
    assert "UA_AGENT_TOKENS looks like a secret" in err
    assert "UA_GEMINI_API_KEY looks like a secret" in err
    # [env] reaches only the UA_* namespace, never the process environment at large.
    assert "'PATH' is not a UA_* variable name" in err
    assert "'LD_PRELOAD' is not a UA_* variable name" in err
    assert "hunter2" not in err


def test_unknown_keys_warn_but_do_not_fail(settings_file, capsys):
    settings_file.write_text('[operator]\nnickname = "x"\n[colors]\nbg = "red"\n')
    applied = settings.apply_to_environ({"UA_SETTINGS_FILE": str(settings_file)})
    assert applied == {}
    err = capsys.readouterr().err
    assert "unknown setting operator.nickname" in err
    assert "unknown settings section [colors]" in err


def test_unknown_keys_helper_suggests_close_matches():
    data = {
        "schema_version": 1,
        "operator": {"name": "Alex", "nmae": "x", "zzzzzz": 1, "workspace": "w"},
        "operater": {"name": "y"},
        "colors": {"bg": "red"},
        "env": {"UA_ANYTHING": "1"},
    }
    assert settings.unknown_keys(data) == [
        ("operator.nmae", "operator.name"),
        ("operator.zzzzzz", None),
        ("operator.workspace", "paths.workspace"),
        ("[operater]", "[operator]"),
        ("[colors]", None),
    ]


def test_exact_key_in_another_section_beats_a_fuzzy_same_section_match():
    # ``labels`` under [operator] is agents.labels, not the near-miss operator.label.
    data = {"schema_version": 1, "operator": {"labels": "a,b"}}
    assert settings.unknown_keys(data) == [("operator.labels", "agents.labels")]


def test_known_keys_report_nothing():
    data = {"schema_version": 1, "operator": {"name": "Alex"}, "server": {"port": 9000}}
    assert settings.unknown_keys(data) == []
    env, warnings = settings.to_env(data)
    assert env["UA_OPERATOR_NAME"] == "Alex"
    assert warnings == []


def test_to_env_warning_carries_the_suggestion_and_still_applies_known_keys():
    env, warnings = settings.to_env({"operator": {"nmae": "x", "name": "Alex"}})
    assert env == {"UA_OPERATOR_NAME": "Alex"}
    assert warnings == ["unknown setting operator.nmae (did you mean operator.name?); skipped"]


def test_startup_survives_a_typo_and_names_the_fix(settings_file, capsys):
    settings_file.write_text('[operator]\nnmae = "x"\nname = "Alex"\n')
    applied = settings.apply_to_environ({"UA_SETTINGS_FILE": str(settings_file)})
    assert applied == {"UA_OPERATOR_NAME": "Alex"}
    assert "did you mean operator.name?" in capsys.readouterr().err


def test_settings_check_reports_the_suggestion_and_exits_nonzero(settings_file, capsys):
    settings_file.write_text('[operator]\nnmae = "x"\n')
    assert settings.run_settings_cli(["check"]) == 1
    assert "did you mean operator.name?" in capsys.readouterr().err


def test_init_reports_unknown_keys_with_suggestions(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    home.mkdir()
    target = home / "settings.toml"
    target.write_text('schema_version = 1\n[operator]\nnmae = "x"\n[colors]\nbg = "red"\n')
    target.chmod(0o600)
    before = target.read_bytes()
    assert bootstrap.init_main(["--json"]) == 2
    err = capsys.readouterr().err
    assert "unknown setting operator.nmae (did you mean operator.name?)" in err
    assert "unknown settings section [colors]" in err
    assert "settings check" in err
    assert target.read_bytes() == before


def test_init_with_known_keys_reports_no_unknowns(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Old"]) == 0
    assert "unknown" not in capsys.readouterr().err


def test_malformed_file_is_ignored_not_fatal(settings_file, capsys):
    settings_file.write_text("this is = = not toml")
    assert settings.apply_to_environ({"UA_SETTINGS_FILE": str(settings_file)}) == {}
    assert "ignoring settings file" in capsys.readouterr().err


def test_wrong_schema_version_is_refused(settings_file):
    settings_file.write_text("schema_version = 99\n")
    with pytest.raises(settings.SettingsError, match="schema_version"):
        settings.load(settings_file)


def test_wrong_schema_version_still_fails_init(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    target = home / "settings.toml"
    target.write_text("schema_version = 99\n")
    target.chmod(0o600)
    assert bootstrap.init_main(["--json"]) == 2
    assert "schema_version" in capsys.readouterr().err


def test_bad_value_type_is_skipped(settings_file):
    settings_file.write_text('[server]\nport = "eighty"\n[agents]\nlabels = [1, 2]\n')
    values, warnings = settings.to_env(settings.load(settings_file))
    assert values == {}
    assert len(warnings) == 2


# --- writing and the CLI ---------------------------------------------------


def test_set_get_unset_round_trip(settings_file, monkeypatch, capsys):
    assert settings.run_settings_cli(["set", "operator.name", "Sam"]) == 0
    assert settings.run_settings_cli(["set", "agents.labels", "A_one,B_two"]) == 0
    assert settings.run_settings_cli(["set", "env.UA_EDITION", "community"]) == 0
    data = settings.load(settings_file)
    assert data["operator"] == {"name": "Sam"}
    assert data["agents"] == {"labels": ["A_one", "B_two"]}
    assert data["env"] == {"UA_EDITION": "community"}
    assert settings.run_settings_cli(["check"]) == 0

    monkeypatch.setenv("UA_OPERATOR_NAME", "Sam")
    capsys.readouterr()
    assert settings.run_settings_cli(["get", "operator.name"]) == 0
    assert capsys.readouterr().out.strip() == "Sam"

    assert settings.run_settings_cli(["unset", "operator.name"]) == 0
    assert "operator" not in settings.load(settings_file)


def test_set_refuses_unknown_and_secret_keys(settings_file, capsys):
    assert settings.run_settings_cli(["set", "operator.shoe_size", "9"]) == 2
    assert settings.run_settings_cli(["set", "env.UA_AGENT_TOKENS", "x:y"]) == 2
    assert settings.run_settings_cli(["set", "env.PYTHONPATH", "/tmp"]) == 2
    assert not settings_file.exists()


def test_render_round_trips(settings_file):
    data = {
        "schema_version": 1,
        "operator": {"name": 'Quote "Me"', "aliases": ["a", "b"]},
        "server": {"port": 8765},
        "env": {"UA_STATE_SYNC": False},
    }
    settings.write(data, settings_file)
    assert settings.load(settings_file) == data


def test_get_and_show_report_built_in_defaults(settings_file, neutral_operator, capsys):
    capsys.readouterr()
    assert settings.run_settings_cli(["get", "operator.label"]) == 0
    assert capsys.readouterr().out.strip() == "Desktop_Operator"
    assert settings.run_settings_cli(["show", "--json"]) == 0
    rows = {r["setting"]: r for r in json.loads(capsys.readouterr().out)["settings"]}
    assert rows["operator.name"] == {
        "setting": "operator.name",
        "env": "UA_OPERATOR_NAME",
        "value": "Operator",
        "source": "default",
    }
    # The name is not an alias: unset aliases read as empty.
    assert rows["operator.aliases"]["value"] == ""
    # No default exists for an unset closed roster: still exit 1.
    assert settings.run_settings_cli(["get", "agents.labels"]) == 1


def test_show_reports_source(settings_file, monkeypatch, capsys):
    monkeypatch.setenv("UA_OPERATOR_NAME", "Shown")
    assert settings.run_settings_cli(["show", "--json"]) == 0
    rows = {r["setting"]: r for r in json.loads(capsys.readouterr().out)["settings"]}
    assert rows["operator.name"]["value"] == "Shown"
    assert rows["operator.name"]["source"] == "environment"


def test_server_main_dispatches_settings_subcommand(monkeypatch, settings_file, capsys):
    from unified_agent import server

    monkeypatch.setattr("sys.argv", ["unified-agent", "settings", "path"])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == str(settings_file)


# --- operator identity -----------------------------------------------------


def test_neutral_defaults_name_no_real_person(neutral_operator):
    assert config.operator_name() == "Operator"
    assert config.operator_label() == "Desktop_Operator"
    assert config.human_identity_labels() == frozenset({"Desktop_Operator"})
    assert config.identity_alias_allowlist() == {"Desktop_Operator": frozenset({"Operator"})}
    # No catalogued label is a human: the only human is whoever is configured.
    catalog = config.fleet_agent_labels() - {config.operator_label()}
    assert all(config.actor_kind_for_identity(label) != "human" for label in catalog)


def test_custom_operator_flows_through_identity_policy(neutral_operator, monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "alex_m")

    assert config.actor_kind_for_identity("Desktop_Alex") == "human"
    assert config.actor_kind_for_identity("desktop_alex") == "human"
    assert config.actor_kind_for_identity("Alex") == "human"
    assert config.actor_kind_for_claim("alex_m") == "human"
    assert config.is_governed_agent_identity("Desktop_Alex") is False
    # Fleet-prefixed label is accepted once it is the configured operator.
    assert config.canonical_agent_label("desktop_alex") == "Desktop_Alex"
    assert "Desktop_Alex" in config.expected_unregistered_labels()

    assert profiles.default_for_identity("Desktop_Alex") == profiles.OPERATOR
    assert profiles.profiles_by_identity()["Desktop_Alex"] == profiles.OPERATOR
    assert profiles.default_for_identity("Desktop_Someone_Else") == profiles.DEVELOPER

    assert config.operator_dispatch_alias("Desktop_Alex") == "Alex"
    assert config.operator_dispatch_alias("Desktop_Agent2") == "Desktop_Agent2"
    assert personas.get_persona("Desktop_Alex")["display_name"] == "Alex"
    assert set(personas.operator_personas()) == {"Desktop_Alex", "Alex", "alex_m"}


def test_verify_write_identity_allows_only_operator_alias(neutral_operator, monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    token = config.agent_identity_var.set("Desktop_Alex")
    try:
        assert config.verify_write_identity("Alex") == "Alex"
        with pytest.raises(Exception, match="identity mismatch"):
            config.verify_write_identity("Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize("raw", ["not a label!", "Desktop_Agent2", "desktop_agent1"])
def test_bad_or_agent_operator_label_falls_back(neutral_operator, monkeypatch, raw):
    # An agent label must never be promoted to the human operator profile.
    monkeypatch.setenv("UA_OPERATOR_LABEL", raw)
    assert config.operator_label() == config.DEFAULT_OPERATOR_LABEL
    assert profiles.default_for_identity("Desktop_Agent2") != profiles.OPERATOR


@pytest.mark.parametrize("bad", ["Local_Worker", "local_worker"])
def test_configured_worker_cannot_become_operator(neutral_operator, monkeypatch, bad):
    monkeypatch.setenv("UA_AGENT_LABELS", "Local_Worker:Another_Worker")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Local_Worker:synthetic-worker-token")
    monkeypatch.setenv("UA_OPERATOR_LABEL", bad)
    monkeypatch.setenv("UA_OPERATOR_NAME", bad)
    monkeypatch.setenv("UA_OPERATOR_ALIASES", f"Alex,{bad}")
    assert config.operator_label() == config.DEFAULT_OPERATOR_LABEL
    assert config.operator_name() == "Alex"
    assert bad not in config.operator_aliases()
    assert config.actor_kind_for_identity(bad) == "agent"
    assert profiles.default_for_identity(bad) != profiles.OPERATOR


@pytest.mark.parametrize("label", ["", "bad label"])
def test_operator_fallback_cannot_promote_roster_worker(neutral_operator, monkeypatch, label):
    # The default label names a roster worker: no login is human, and the
    # per-request lookup refuses quietly instead of raising (a 500 per call).
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Operator:Local_Worker")
    monkeypatch.setenv("UA_OPERATOR_LABEL", label)
    assert config.operator_label() == config.UNASSIGNED_OPERATOR_LABEL
    assert not config.agent_label_is_well_formed(config.UNASSIGNED_OPERATOR_LABEL)
    assert profiles.default_for_identity("Desktop_Operator") != profiles.OPERATOR
    assert config.actor_kind_for_identity("Desktop_Operator") == "agent"


@pytest.mark.parametrize("payload", [b"not a database at all" * 64, b""])
def test_unreadable_credential_store_refuses_alias_without_raising(
    neutral_operator, tmp_path, monkeypatch, payload
):
    # A tombstoned or corrupt store cannot prove a name is human: the alias is
    # refused (fail closed) and every caller keeps working.
    db = tmp_path / "credentials.db"
    db.write_bytes(payload)
    if not payload:
        db.unlink()
        db.mkdir()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    assert "Alex" not in config.operator_aliases()
    assert config.operator_label() == "Desktop_Alex"
    assert config.actor_kind_for_identity("Desktop_Alex") == "human"
    assert config.actor_kind_for_identity("Alex") != "human"


def test_alias_lookup_cache_misses_after_any_commit(neutral_operator, tmp_path, monkeypatch):
    db = tmp_path / "credentials.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE agent_tokens (agent_label TEXT, org_id TEXT, status TEXT)")
    for _ in range(3):  # repeated lookups between writes are served from the cache
        assert "Local_Worker" in config.operator_aliases()
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO agent_tokens VALUES ('Local_Worker', NULL, 'active')")
    assert "Local_Worker" not in config.operator_aliases()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE agent_tokens SET status = 'revoked'")
    assert "Local_Worker" in config.operator_aliases()


@pytest.mark.parametrize("reconnect", ["closed_handle", "path_switch"])
def test_alias_lookup_reconnect_cannot_reuse_an_old_store_version(
    neutral_operator, tmp_path, monkeypatch, reconnect
):
    db = tmp_path / "credentials.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE agent_tokens (agent_label TEXT, org_id TEXT, status TEXT)")
    assert "Local_Worker" in config.operator_aliases()
    if reconnect == "closed_handle":
        held = config._MANAGED_WORKER_CONN
        connection = held[2] if isinstance(held, list) else held[str(db)][1]
        connection.close()
    else:
        other = tmp_path / "other.db"
        with sqlite3.connect(other) as conn:
            conn.execute("CREATE TABLE agent_tokens (agent_label TEXT, org_id TEXT, status TEXT)")
        monkeypatch.setattr(config, "db_path", lambda: other)
        assert "Local_Worker" in config.operator_aliases()
        monkeypatch.setattr(config, "db_path", lambda: db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO agent_tokens VALUES ('Local_Worker', NULL, 'active')")
    # A broken handle fails closed first; a later re-open must see the worker
    # even when PRAGMA data_version restarts at the old cached number.
    assert "Local_Worker" not in config.operator_aliases()
    assert "Local_Worker" not in config.operator_aliases()
    assert config.actor_kind_for_identity("Local_Worker") == "agent"


def test_settings_toml_cannot_override_a_preset_workspace(tmp_path):
    """service.env wins: settings.toml only fills a gap, never overrides.

    The sandbox root is a security boundary. `apply_to_environ` seeds the
    environment with `if name not in env`, so a variable already present from
    the service environment is left alone. That invariant is currently implicit
    in one line and load-bearing for every sandboxed tool, so it gets an
    explicit regression test (#1806).
    """
    target = tmp_path / "settings.toml"
    target.write_text('[paths]\nworkspace = "/from/settings-toml"\n')
    env = {"UA_WORKSPACE": "/from/service-env"}
    applied = settings.apply_to_environ(env, path=target)
    assert env["UA_WORKSPACE"] == "/from/service-env"
    # Nothing was taken from the file, so it is not even reported as applied.
    assert applied == {}


def test_settings_toml_supplies_workspace_when_the_env_leaves_it_unset(tmp_path):
    """The documented fallback: with no service.env value, settings.toml fills it."""
    target = tmp_path / "settings.toml"
    target.write_text('[paths]\nworkspace = "/from/settings-toml"\n')
    env: dict[str, str] = {}
    applied = settings.apply_to_environ(env, path=target)
    assert env["UA_WORKSPACE"] == "/from/settings-toml"
    assert applied == {"UA_WORKSPACE": "/from/settings-toml"}


def test_group_writable_settings_file_is_refused(tmp_path, capsys):
    target = tmp_path / "settings.toml"
    target.write_text('[operator]\nlabel = "Desktop_Mallory"\n')
    target.chmod(0o664)
    env: dict[str, str] = {}
    assert settings.apply_to_environ(env, path=target) == {}
    assert "UA_OPERATOR_LABEL" not in env
    assert "writable by group or other" in capsys.readouterr().err
    target.chmod(0o644)
    assert settings.apply_to_environ(env, path=target) == {"UA_OPERATOR_LABEL": "Desktop_Mallory"}


@pytest.mark.parametrize(
    "name",
    [
        "UA_ENFORCE_AUTH",
        "UA_APPROVAL_TOOLS",
        "UA_LEGACY_OPEN_POSTURE",
        "UA_HOME",
        "UA_SETTINGS_FILE",
        "UA_POLICY_AUTHORIZERS",
        "UA_POLICY_AUTHORIZATION_ALLOW_SELF",
    ],
)
def test_settings_env_table_cannot_set_security_posture(name):
    values, warnings = settings.to_env({"env": {name: "0", "UA_DEV_ROOT": "/tmp/x"}})
    assert name not in values
    assert values["UA_DEV_ROOT"] == "/tmp/x"
    assert any(name in message for message in warnings)


@pytest.mark.parametrize("key", ["UA_OPERATOR_NAME", "UA_OPERATOR_ALIASES"])
def test_open_roster_worker_bearer_cannot_be_human_alias(neutral_operator, monkeypatch, key):
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Local_Worker:synthetic-worker-token")
    monkeypatch.setenv(key, "Local_Worker")
    assert "Local_Worker" not in config.operator_aliases()
    assert config.actor_kind_for_identity("Local_Worker") == "agent"


@pytest.mark.parametrize("key", ["UA_OPERATOR_LABEL", "UA_OPERATOR_NAME", "UA_OPERATOR_ALIASES"])
def test_managed_worker_cannot_become_operator_or_alias(
    neutral_operator, tmp_path, monkeypatch, key
):
    db = tmp_path / "credentials.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE agent_tokens (agent_label TEXT, org_id TEXT, status TEXT)")
        conn.execute("INSERT INTO agent_tokens VALUES ('Local_Worker', NULL, 'active')")
    monkeypatch.setenv(key, "Local_Worker")
    if key == "UA_OPERATOR_LABEL":
        monkeypatch.setenv("UA_AGENT_LABELS", "Local_Worker")
    assert config.operator_label() != "Local_Worker"
    assert "Local_Worker" not in config.operator_aliases()
    assert config.actor_kind_for_identity("Local_Worker") == "agent"


def _oauth_store_db(tmp_path, monkeypatch):
    """A credential DB with the real OAuth tables and no agent_tokens rows."""
    from unified_agent import oauth_store

    db = tmp_path / "credentials.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    for name in ("UA_AGENT_LABELS", "UA_AGENT_TOKENS", "WORKOS_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE agent_tokens (agent_label TEXT, org_id TEXT, status TEXT)")
        conn.executescript(oauth_store._SCHEMA)
    return db


@pytest.mark.parametrize("table", ["oauth_access_tokens", "oauth_refresh_tokens"])
@pytest.mark.parametrize("key", ["UA_OPERATOR_NAME", "UA_OPERATOR_ALIASES"])
def test_oauth_granted_agent_cannot_be_human_alias(
    neutral_operator, tmp_path, monkeypatch, table, key
):
    """#1760: a roster agent that signs in through OAuth has no agent_tokens row."""
    db = _oauth_store_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv(key, "Local_Worker")
    assert "Local_Worker" in config.operator_aliases()  # no grant yet
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"INSERT INTO {table} (token_sha256, client_id, subject, expires_at, issued_at) "
            "VALUES ('h1', 'client-1', 'local_worker', ?, 1)",
            (None,),  # NULL expiry never lapses
        )
    assert "Local_Worker" not in config.operator_aliases()
    assert config.operator_name() != "Local_Worker"
    assert config.actor_kind_for_identity("Local_Worker") == "agent"


def test_expired_oauth_grant_no_longer_owns_the_name(neutral_operator, tmp_path, monkeypatch):
    db = _oauth_store_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO oauth_access_tokens (token_sha256, client_id, subject, expires_at, "
            "issued_at) VALUES ('h1', 'client-1', 'Local_Worker', 1, 1)"
        )
    assert "Local_Worker" in config.operator_aliases()


def test_unreadable_oauth_store_fails_closed(neutral_operator, tmp_path, monkeypatch):
    db = _oauth_store_db(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE oauth_access_tokens")
        conn.execute("CREATE TABLE oauth_access_tokens (token_sha256 TEXT)")  # wrong shape
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    assert "Local_Worker" not in config.operator_aliases()


def test_operator_login_stays_human_despite_oauth_grant(neutral_operator, tmp_path, monkeypatch):
    db = _oauth_store_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO oauth_access_tokens (token_sha256, client_id, subject, issued_at) "
            "VALUES ('h1', 'client-1', 'Desktop_Alex', 1)"
        )
    assert config.operator_label() == "Desktop_Alex"
    assert config.actor_kind_for_identity("Desktop_Alex") == "human"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("UA_MCP_OAUTH_CLIENTS", "Local_Worker:client-1:synthetic-secret"),
        ("UA_MCP_OAUTH_CLIENT_CREDENTIALS", "client-1|synthetic-secret|Local_Worker"),
        ("UA_MCP_OAUTH_AGENT", "Local_Worker"),
        ("UA_HUB_WORKOS_IDENTITY_MAP", '{"user_01SYNTHETIC": "Local_Worker"}'),
    ],
)
def test_pinned_oauth_or_workos_label_cannot_be_human_alias(
    neutral_operator, tmp_path, monkeypatch, variable, value
):
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "absent.db")
    for name in ("UA_AGENT_LABELS", "UA_AGENT_TOKENS", "WORKOS_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    assert "Local_Worker" in config.operator_aliases()
    monkeypatch.setenv(variable, value)
    assert "Local_Worker" not in config.operator_aliases()
    assert config.actor_kind_for_identity("Local_Worker") == "agent"


def test_client_credentials_file_label_cannot_be_human_alias(
    neutral_operator, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "absent.db")
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    creds = tmp_path / "creds.txt"
    creds.write_text("client-1|synthetic-secret|Local_Worker\n")
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    monkeypatch.setenv("UA_MCP_OAUTH_CLIENT_CREDENTIALS", str(creds))
    assert "Local_Worker" not in config.operator_aliases()


def test_workos_map_naming_the_operator_login_keeps_it_human(
    neutral_operator, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "absent.db")
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", '{"user_01SYNTHETIC": "Desktop_Alex"}')
    assert config.operator_label() == "Desktop_Alex"
    assert config.actor_kind_for_identity("Desktop_Alex") == "human"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("UA_MCP_OAUTH_CLIENT_CREDENTIALS", "client-1||Local_Worker"),
        ("UA_MCP_OAUTH_CLIENT_CREDENTIALS", "|synthetic-secret|Local_Worker"),
        ("UA_MCP_OAUTH_CLIENTS", "Local_Worker:client-1:"),
        ("UA_MCP_OAUTH_CLIENTS", "Local_Worker::synthetic-secret"),
        ("UA_MCP_OAUTH_CLIENTS", "Local_Worker:client-1"),
    ],
)
def test_malformed_oauth_pin_does_not_withhold_operator_name(
    neutral_operator, tmp_path, monkeypatch, variable, value
):
    """A pin its own parser rejects cannot authenticate, so it names no worker.

    Devin review, PR #978: ``id||Alice`` (empty secret) dropped Alice as the
    operator name although ``mcp_oauth_client_credentials`` skips that entry.
    """
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "absent.db")
    for name in ("UA_AGENT_LABELS", "UA_AGENT_TOKENS", "WORKOS_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_NAME", "Local_Worker")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    monkeypatch.setenv(variable, value)
    assert config.mcp_oauth_client_credentials() == []
    assert config.mcp_oauth_clients() == {}
    assert config.operator_name() == "Local_Worker"
    assert "Local_Worker" in config.operator_aliases()


def test_malformed_pinned_oauth_values_do_not_raise(neutral_operator, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "absent.db")
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", "{not json")
    monkeypatch.setenv("UA_MCP_OAUTH_CLIENTS", str(tmp_path / "missing-clients-file"))
    monkeypatch.setenv("UA_MCP_OAUTH_CLIENT_CREDENTIALS", "garbage")
    assert "Local_Worker" in config.operator_aliases()


@pytest.mark.parametrize("unreadable", ["missing-tokens-file", "a-directory"])
def test_unreadable_agent_token_file_refuses_operator_names_without_raising(
    neutral_operator, tmp_path, monkeypatch, unreadable
):
    """#1971: an unreadable UA_AGENT_TOKENS file cannot prove a name is human.

    The worker lookup used to let ``_parse_colon_pairs``'s ValueError escape,
    so ``operator_name()`` and ``operator_aliases()`` raised. It now fails
    closed: every claimed name that needs the token check is refused, the
    name falls back to the operator's login label, and that label (checked
    before the token file) stays human.
    """
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "absent.db")
    for name in ("UA_AGENT_LABELS", "WORKOS_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / unreadable
    if unreadable == "a-directory":
        path.mkdir()
    monkeypatch.setenv("UA_AGENT_TOKENS", str(path))
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    assert config.operator_label() == "Desktop_Alex"
    assert config.operator_name() == "Desktop_Alex"
    assert config.operator_aliases() == frozenset()
    assert config.actor_kind_for_identity("Desktop_Alex") == "human"
    assert bootstrap._name_from_aliases(["Local_Worker"]) == config.DEFAULT_OPERATOR_NAME


_PINNED_OAUTH_VARIABLES = (
    "UA_MCP_OAUTH_CLIENTS",
    "UA_MCP_OAUTH_CLIENT_CREDENTIALS",
    "UA_MCP_OAUTH_AGENT",
    "UA_HUB_WORKOS_IDENTITY_MAP",
)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("UA_MCP_OAUTH_CLIENTS", "Local_Worker:client-1:synthetic-secret"),
        ("UA_MCP_OAUTH_CLIENT_CREDENTIALS", "client-1|synthetic-secret|Local_Worker"),
        ("UA_MCP_OAUTH_AGENT", "Local_Worker"),
        ("UA_HUB_WORKOS_IDENTITY_MAP", '{"user_01SYNTHETIC": "Local_Worker"}'),
    ],
)
def test_service_env_oauth_pin_label_cannot_be_human_alias(
    neutral_operator, tmp_path, monkeypatch, variable, value
):
    """#1971: `unified-agent init` runs without the hub's service.env loaded.

    A label the hub's service.env pins to OAuth or WorkOS is a worker even
    when this process's environment does not carry the variable.
    """
    import shlex

    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "absent.db")
    for name in ("UA_AGENT_LABELS", "UA_AGENT_TOKENS", "WORKOS_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    for name in _PINNED_OAUTH_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    home = config.data_home()
    home.mkdir(parents=True, exist_ok=True)
    assert "Local_Worker" in config.operator_aliases()
    (home / "service.env").write_text(f"{variable}={shlex.quote(value)}\n", encoding="utf-8")
    assert "Local_Worker" not in config.operator_aliases()
    assert bootstrap._name_from_aliases(["Local_Worker"]) == config.DEFAULT_OPERATOR_NAME


def test_malformed_service_env_oauth_values_do_not_raise(neutral_operator, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "absent.db")
    for name in ("UA_AGENT_LABELS", "UA_AGENT_TOKENS", "WORKOS_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    for name in _PINNED_OAUTH_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    home = config.data_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "service.env").write_text(
        "UA_HUB_WORKOS_IDENTITY_MAP='{not json'\n"
        f"UA_MCP_OAUTH_CLIENTS={tmp_path / 'missing-clients-file'}\n"
        "UA_MCP_OAUTH_CLIENT_CREDENTIALS=garbage\n",
        encoding="utf-8",
    )
    assert "Local_Worker" in config.operator_aliases()


def test_worker_credential_registration_revokes_alias_without_stale_cache(
    neutral_operator, tmp_path, monkeypatch
):
    db = tmp_path / "credentials.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org-a")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE agent_tokens (agent_label TEXT, org_id TEXT, status TEXT)")
        conn.execute("INSERT INTO agent_tokens VALUES ('Local_Worker', 'org-b', 'active')")
    assert "Local_Worker" in config.operator_aliases()  # Other tenant's identity is distinct.
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO agent_tokens VALUES ('Local_Worker', 'org-a', 'active')")
    assert "Local_Worker" not in config.operator_aliases()


@pytest.mark.parametrize("label", ["Desktop_Operator", "Desktop_Alex"])
def test_managed_primary_operator_keeps_human_authority(
    neutral_operator, tmp_path, monkeypatch, label
):
    db = tmp_path / "credentials.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", label)
    monkeypatch.setenv("UA_OPERATOR_NAME", label)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE agent_tokens (agent_label TEXT, org_id TEXT, status TEXT)")
        conn.execute("INSERT INTO agent_tokens VALUES (?, NULL, 'active')", (label,))
    assert config.operator_label() == label
    assert config.actor_kind_for_identity(label) == "human"
    assert config.operator_name() == label
    assert profiles.default_for_identity(label) == profiles.OPERATOR


def test_default_operator_name_cannot_promote_worker(neutral_operator, monkeypatch):
    from unified_agent import agent_lifecycle

    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.setenv("UA_AGENT_TOKENS", "Operator:synthetic-worker-token")
    assert config.operator_name() == config.operator_label()
    assert "Operator" not in config.operator_aliases()
    assert "Operator" not in agent_lifecycle.operators()
    assert config.actor_kind_for_identity("Operator") == "agent"


def test_init_cannot_reuse_existing_worker_bearer(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Old"]) == 0
    env_file = home / "service.env"
    env_file.write_text("UA_AGENT_TOKENS=Local_Worker:synthetic-worker-token\n")
    settings_before = (home / "settings.toml").read_bytes()
    capsys.readouterr()
    assert bootstrap.init_main(["--json", "--operator-label", "Local_Worker"]) == 2
    assert "existing worker" in capsys.readouterr().err
    assert (home / "settings.toml").read_bytes() == settings_before
    assert env_file.read_text() == "UA_AGENT_TOKENS=Local_Worker:synthetic-worker-token\n"


def test_init_keeps_default_operator_bearer_without_changing_its_authority(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    """An implicit-default Desktop_Operator bearer is the human login at runtime.

    With no operator identity configured anywhere, the hub already resolves
    ``Desktop_Operator`` as the operator (fresh-install default). Re-running
    init keeps that bearer as-is: it grants nothing the runtime had not already
    granted, and the worker bearer next to it stays a worker.
    """
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    tokens = "Local_Worker:synthetic-worker-token,Desktop_Operator:synthetic-default-token"
    (home / "service.env").write_text(f"UA_AGENT_TOKENS={tokens}\n")
    monkeypatch.setenv("UA_AGENT_TOKENS", tokens)

    def profiles_by_bearer():
        return {
            bearer: bootstrap._whoami_payload(bearer)["profile"]
            for bearer in ("synthetic-worker-token", "synthetic-default-token")
        }

    before = profiles_by_bearer()
    assert before["synthetic-default-token"] == profiles.OPERATOR
    assert before["synthetic-worker-token"] != profiles.OPERATOR

    assert bootstrap.init_main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["env_file"]["action"] == "kept"
    assert out["operator"]["label"] == "Desktop_Operator"
    assert service_env.parse_env_file(home / "service.env")["UA_AGENT_TOKENS"] == tokens
    assert profiles_by_bearer() == before


@pytest.mark.parametrize("via", ["settings", "service_env"])
def test_init_refuses_default_label_when_it_is_a_worker_under_a_configured_operator(
    tmp_path, monkeypatch, capsys, neutral_operator, via
):
    """Once another human is configured, Desktop_Operator is just a worker bearer."""
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    env_body = "UA_AGENT_TOKENS=Desktop_Operator:synthetic-worker-token\n"
    if via == "settings":
        settings.write({"operator": {"name": "Sam", "label": "Desktop_Sam"}})
    else:
        env_body += "UA_OPERATOR_NAME=Sam\nUA_OPERATOR_LABEL=Desktop_Sam\n"
    env_file = home / "service.env"
    env_file.write_text(env_body)
    before = env_file.read_bytes()

    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Operator"]) == 2
    assert "cannot reuse an existing worker bearer" in capsys.readouterr().err
    assert env_file.read_bytes() == before


def test_operator_name_falls_back_to_first_alias(neutral_operator, monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_ALIASES", " , Sam, Samuel")
    assert config.operator_name() == "Sam"
    monkeypatch.setenv("UA_OPERATOR_NAME", "Samantha")
    assert config.operator_name() == "Samantha"


@pytest.mark.parametrize("bad", ["Desktop_Agent2", "desktop_agent1", "server_agent3"])
def test_aliases_and_name_never_promote_agent_labels(neutral_operator, monkeypatch, bad):
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", f"alex,{bad}")
    monkeypatch.setenv("UA_OPERATOR_NAME", bad)
    assert bad not in config.operator_aliases()
    assert config.operator_name() == "alex"
    assert config.actor_kind_for_identity(bad) == "agent"
    assert config.operator_dispatch_alias("Desktop_Alex") == "alex"


def test_lifecycle_operators_follow_settings(neutral_operator, monkeypatch):
    from unified_agent import agent_lifecycle

    monkeypatch.delenv("UA_LIFECYCLE_OPERATORS", raising=False)
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    assert agent_lifecycle.operators() == frozenset({"Desktop_Alex", "Alex"})


def test_default_workspace_lives_under_ua_home(monkeypatch, tmp_path):
    monkeypatch.delenv("UA_WORKSPACE", raising=False)
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    assert config.process_workspace_root() == tmp_path / "workspace"


# --- init writes personalization ------------------------------------------


def test_init_writes_settings_and_operator_token(tmp_path, monkeypatch, capsys, neutral_operator):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    rc = bootstrap.init_main(
        [
            "--json",
            "--print-token",
            "--operator-name",
            "Alex Example",
            "--workspace",
            str(tmp_path / "ws"),
            "--vault",
            "",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["operator"] == {"name": "Alex Example", "label": "Desktop_Alex_Example"}
    data = settings.load(home / "settings.toml")
    assert data["operator"] == {"name": "Alex Example", "label": "Desktop_Alex_Example"}
    assert data["paths"] == {"workspace": str(tmp_path / "ws"), "vault": ""}
    assert (tmp_path / "ws").is_dir()
    body = (home / "service.env").read_text()
    assert f"Desktop_Alex_Example:{out['operator_token']}" in body
    # Secrets stay in service.env, never in settings.toml.
    assert out["operator_token"] not in (home / "settings.toml").read_text()


@pytest.mark.parametrize(
    "args",
    [["--operator-label", "bad label"], ["--operator-label", "Desktop_Agent2"]],
)
def test_init_rejects_labels_the_hub_would_ignore(tmp_path, monkeypatch, args):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    with pytest.raises(SystemExit, match="operator label"):
        bootstrap.init_main(args)
    # Nothing is persisted for a refused label.
    assert not (home / "settings.toml").exists()
    assert not (home / "service.env").exists()


def test_init_derived_label_that_names_an_agent_is_refused(tmp_path, monkeypatch, neutral_operator):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    # The label segment, not the family name: the public export renames the
    # segments, so "Codex" derives a label that is no longer an agent there.
    with pytest.raises(SystemExit, match="catalogued agent"):
        bootstrap.init_main(["--operator-name", config.LABEL_BY_FAMILY["Codex"]])


def test_reinit_appends_operator_token_to_a_token_file(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    """UA_AGENT_TOKENS may name a token file: keep the path, append to the file."""
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json"]) == 0
    tokens = tmp_path / "tokens"
    tokens.write_text("worker:old-token")
    tokens.chmod(0o600)
    env_file = home / "service.env"
    # An already pinned workspace (#1825) keeps init from adding that stanza.
    env_text = f"UA_WORKSPACE={home / 'workspace'}\nUA_AGENT_TOKENS={tokens}\n"
    env_file.write_text(env_text)
    capsys.readouterr()

    argv = ["--json", "--print-token", "--operator-label", "Desktop_Alex"]
    assert bootstrap.init_main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["env_file"]["action"] == f"operator token added to {tokens}"
    assert env_file.read_text() == env_text  # path untouched
    assert tokens.read_text() == f"worker:old-token\nDesktop_Alex:{out['operator_token']}\n"
    assert tokens.stat().st_mode & 0o777 == 0o600
    pairs = config.agent_tokens(str(tokens), include_managed=False)
    assert pairs == {"old-token": "worker", out["operator_token"]: "Desktop_Alex"}


@pytest.mark.parametrize("mode", [0o640, 0o660, 0o644, 0o666])
def test_new_operator_token_is_owner_only(tmp_path, mode):
    home = tmp_path / "ua"
    home.mkdir()
    tokens = tmp_path / "tokens"
    tokens.write_text("worker:synthetic-worker-token\n")
    tokens.chmod(mode)
    env_file = home / "service.env"
    env_file.write_text(f"UA_AGENT_TOKENS={tokens}\n")
    _, token = bootstrap._ensure_operator_token(env_file, "Desktop_New")
    assert token is not None
    assert tokens.stat().st_mode & 0o777 == 0o600


def test_reinit_with_unreadable_token_file_changes_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Old"]) == 0
    env_file = home / "service.env"
    env_file.write_text(f"UA_AGENT_TOKENS={tmp_path / 'missing'}\n")
    settings_before = (home / "settings.toml").read_text()
    capsys.readouterr()
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Alex"]) == 2
    assert "settings.toml and service.env are unchanged" in capsys.readouterr().err
    assert env_file.read_text() == f"UA_AGENT_TOKENS={tmp_path / 'missing'}\n"
    # The new label is not saved, so the operator is never left without a bearer.
    assert (home / "settings.toml").read_text() == settings_before


@pytest.mark.parametrize(
    "aliases,expected",
    [
        (["Sam"], "Sam"),
        (["Desktop_Agent2", "Sam", "Other"], "Sam"),
        (["Desktop_Agent2", "Desktop_Agent1"], config.DEFAULT_OPERATOR_NAME),
        ([], config.DEFAULT_OPERATOR_NAME),
    ],
)
@pytest.mark.parametrize("source", ["settings", "service_env"])
def test_init_alias_only_operator_keeps_the_effective_alias_name(
    tmp_path, monkeypatch, capsys, neutral_operator, aliases, expected, source
):
    """With no name anywhere, the first accepted alias is already the name."""
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / settings.SETTINGS_FILENAME))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    if source == "settings":
        settings.write({"operator": {"aliases": aliases}})
    else:
        (home / "service.env").write_text(f"UA_OPERATOR_ALIASES={','.join(aliases)}\n")
    assert bootstrap.init_main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["operator"]["name"] == expected
    saved = settings.load(home / settings.SETTINGS_FILENAME)["operator"]
    assert saved["name"] == expected
    assert saved["aliases"] == aliases
    # Runtime resolves the same name it did before the explicit pin was saved.
    for key in ("UA_OPERATOR_NAME", "UA_OPERATOR_LABEL", "UA_OPERATOR_ALIASES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("UA_OPERATOR_ALIASES", ",".join(aliases))
    before = config.operator_name()
    monkeypatch.setenv("UA_OPERATOR_NAME", saved["name"])
    assert config.operator_name() == before == expected


def test_init_explicit_or_saved_name_outranks_aliases(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / settings.SETTINGS_FILENAME))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    settings.write({"operator": {"name": "Alex", "aliases": ["Sam"]}})
    assert bootstrap.init_main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["operator"]["name"] == "Alex"
    assert bootstrap.init_main(["--json", "--operator-name", "Kim"]) == 0
    assert json.loads(capsys.readouterr().out)["operator"]["name"] == "Kim"


def test_reinit_updates_a_symlinked_token_files_target(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json"]) == 0
    shared = tmp_path / "shared"
    shared.write_text("worker:old-token\n")
    shared.chmod(0o640)
    link = tmp_path / "current"
    link.symlink_to(shared)
    (home / "service.env").write_text(f"UA_AGENT_TOKENS={link}\n")
    capsys.readouterr()

    argv = ["--json", "--print-token", "--operator-label", "Desktop_Alex"]
    assert bootstrap.init_main(argv) == 0
    token = json.loads(capsys.readouterr().out)["operator_token"]
    assert link.is_symlink() and link.resolve() == shared
    assert shared.read_text() == f"worker:old-token\nDesktop_Alex:{token}\n"
    assert shared.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("scenario", ["new_bearer", "identity_pin"])
def test_reinit_updates_a_symlinked_service_env_target(
    tmp_path, monkeypatch, capsys, neutral_operator, scenario, force
):
    """A successful write goes through a service.env symlink to its shared target."""
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    shared = tmp_path / "shared-hub.env"
    shared.write_text(
        "# shared by the hub unit\nUA_AGENT_TOKENS=worker:old-token,Desktop_Old:old-operator\n"
        "UA_OPERATOR_NAME=Old\nUA_OPERATOR_LABEL=Desktop_Old\n"
    )
    shared.chmod(0o640)
    link = home / "service.env"
    link.symlink_to(shared)

    flags = ["--operator-label", "Desktop_Sam"] if scenario == "new_bearer" else []
    flags += ["--operator-name", "Sam", *(["--force"] if force else [])]
    assert bootstrap.init_main(["--json", "--print-token", *flags]) == 0
    out = json.loads(capsys.readouterr().out)

    assert link.is_symlink() and link.resolve() == shared
    assert not list(tmp_path.glob(".shared-hub.env.*.tmp"))
    reader = service_env.parse_env_file(shared)  # a consumer of the original target
    assert reader["UA_OPERATOR_NAME"] == "Sam"
    if not force:
        assert reader["UA_AGENT_TOKENS"].startswith("worker:old-token,Desktop_Old:old-operator")
    if scenario == "new_bearer":
        assert reader["UA_OPERATOR_LABEL"] == "Desktop_Sam"
        assert f"Desktop_Sam:{out['operator_token']}" in reader["UA_AGENT_TOKENS"]
    else:
        assert reader["UA_OPERATOR_LABEL"] == "Desktop_Old"
    assert shared.stat().st_mode & 0o777 == 0o600
    if not force:
        assert "# shared by the hub unit" in shared.read_text()


def test_reinit_with_unwritable_token_directory_changes_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Old"]) == 0
    tokens = tmp_path / "tokens"
    tokens.write_text("worker:old-token\n")
    (home / "service.env").write_text(f"UA_AGENT_TOKENS={tokens}\n")
    settings_before = (home / "settings.toml").read_text()

    def refuse(*args, **kwargs):
        raise PermissionError("read-only directory")

    monkeypatch.setattr(bootstrap, "_write_private", refuse)
    capsys.readouterr()
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Alex"]) == 2
    assert "cannot update token file" in capsys.readouterr().err
    assert tokens.read_text() == "worker:old-token\n"
    assert (home / "settings.toml").read_text() == settings_before


def test_private_writes_never_expose_tokens(tmp_path, monkeypatch):
    """The temp file is created with the final mode, whatever the umask."""
    import os

    created = []
    real_open = os.open
    root = str(tmp_path)

    def spy(path, flags, mode=0o777, *a, **kw):
        fd = real_open(path, flags, mode, *a, **kw)
        # Record only this test's files: os.open is process-wide, and any
        # other thread's call must pass through untouched.
        if "dir_fd" not in kw and str(path).startswith(root):
            created.append(os.fstat(fd).st_mode & 0o777)
        return fd

    old = os.umask(0o022)
    try:
        with monkeypatch.context() as m:
            m.setattr(os, "open", spy)
            target = tmp_path / "service.env"
            bootstrap._write_private(target, "UA_AGENT_TOKENS=a:b\n", 0o600)
            service_env.write_env_file(tmp_path / "other.env", {"UA_AGENT_TOKENS": "a:b"})
    finally:
        os.umask(old)
    assert created == [0o600, 0o600]
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))


def test_reinit_with_new_operator_label_mints_its_token(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json", "--operator-name", "Alex"]) == 0
    before = service_env.parse_env_file(home / "service.env")["UA_AGENT_TOKENS"]
    capsys.readouterr()

    # A name-only re-run keeps the saved label, so nothing changes.
    assert bootstrap.init_main(["--json", "--operator-name", "Alexander"]) == 0
    assert json.loads(capsys.readouterr().out)["env_file"]["action"] == "kept"

    argv = ["--json", "--print-token", "--operator-label", "Desktop_Sam"]
    assert bootstrap.init_main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["env_file"]["action"] == "operator token added"
    after = service_env.parse_env_file(home / "service.env")["UA_AGENT_TOKENS"]
    pairs = dict(p.split(":", 1) for p in after.split(","))
    assert pairs["Desktop_Sam"] == out["operator_token"]
    assert after.startswith(before + ",")  # existing tokens survive unchanged
    assert settings.load(home / "settings.toml")["operator"]["label"] == "Desktop_Sam"

    # Re-running with the same label adds nothing.
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Sam"]) == 0
    assert json.loads(capsys.readouterr().out)["env_file"]["action"] == "kept"


def test_settings_file_is_disabled_for_the_test_suite():
    # conftest disables the file so a developer's own settings never leak in.
    assert os.environ.get("UA_SETTINGS_FILE") is not None


@pytest.mark.parametrize("force", [False, True])
def test_init_refuses_before_touching_tokens_when_settings_cannot_be_saved(
    tmp_path, monkeypatch, capsys, force
):
    """settings.toml is validated and staged first: a bad file or a read-only
    directory must not leave freshly rotated or added tokens behind."""
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json"]) == 0
    env_before = (home / "service.env").read_text()
    extra = ["--force"] if force else []

    # 1. An unknown key in the existing file fails validation.
    with (home / "settings.toml").open("a") as fh:
        fh.write('nickname = "x"\n')  # lands in the last table: [paths]
    capsys.readouterr()
    rc = bootstrap.init_main(["--json", "--operator-label", "Desktop_Alex", *extra])
    assert rc == 2
    assert "nothing was changed" in capsys.readouterr().err
    assert (home / "service.env").read_text() == env_before

    # 2. A settings directory that cannot be written.
    (home / "settings.toml").write_text('schema_version = 1\n[operator]\nname = "Op"\n')

    def read_only(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(settings, "stage", read_only)
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Alex", *extra]) == 2
    assert (home / "service.env").read_text() == env_before
    assert not list(home.glob("*.tmp")) and not list(home.glob(".*.tmp"))


def test_failed_token_provisioning_discards_staged_settings(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Old"]) == 0
    (home / "service.env").write_text(f"UA_AGENT_TOKENS={tmp_path / 'missing'}\n")
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Alex"]) == 2
    assert not list(home.glob(".settings.toml.*.tmp"))
    assert settings.load(home / "settings.toml")["operator"]["label"] == "Desktop_Old"


def test_world_readable_token_file_loses_other_access(tmp_path, monkeypatch, capsys):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json"]) == 0
    tokens = tmp_path / "tokens"
    tokens.write_text("worker:old-token\n")
    tokens.chmod(0o644)
    (home / "service.env").write_text(f"UA_AGENT_TOKENS={tokens}\n")
    capsys.readouterr()
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Alex"]) == 0
    assert "allowed access by other users" in capsys.readouterr().err
    assert tokens.stat().st_mode & 0o777 == 0o600


def test_concurrent_stages_never_share_a_temp_file(settings_file):
    a_tmp, target = settings.stage({"operator": {"label": "Desktop_Alex"}}, settings_file)
    b_tmp, _ = settings.stage({"operator": {"label": "Desktop_Sam"}}, settings_file)
    assert a_tmp != b_tmp and a_tmp.parent == b_tmp.parent == target.parent
    a_tmp.replace(target)  # run A publishes its own staged file only
    assert settings.load(target)["operator"]["label"] == "Desktop_Alex"
    b_tmp.replace(target)  # run B is unaffected and publishes its own
    assert settings.load(target)["operator"]["label"] == "Desktop_Sam"


@pytest.mark.parametrize("token_store", ["inline", "env_symlink", "file", "symlink", "fresh"])
@pytest.mark.parametrize("force", [False, True])
def test_settings_publish_failure_restores_token_stores(
    tmp_path, monkeypatch, capsys, token_store, force
):
    from pathlib import Path

    home = tmp_path / "ua"
    home.mkdir()
    settings_file = home / "settings.toml"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    settings_file.write_text('[operator]\nname = "Old"\nlabel = "Desktop_Old"\n')
    settings_before = settings_file.read_bytes()
    env_file = home / "service.env"
    tokens = tmp_path / "tokens"
    if token_store in ("inline", "env_symlink"):
        if token_store == "env_symlink":
            env_target = tmp_path / "env-target"
            env_target.write_text("")
            env_file.symlink_to(env_target)
        env_file.write_text("# retain this comment\nUA_AGENT_TOKENS=Desktop_Old:fixture-old\n")
    elif token_store in ("file", "symlink"):
        tokens.write_text("# original token store\nDesktop_Old:fixture-old\n")
        tokens.chmod(0o644)
        token_path = tokens
        if token_store == "symlink":
            token_path = tmp_path / "token-link"
            token_path.symlink_to(tokens)
        env_file.write_text(f"# retain this comment\nUA_AGENT_TOKENS={token_path}\n")
    if env_file.exists():
        with env_file.open("a") as stream:
            stream.write(
                "UA_OPERATOR_NAME=Old\nUA_OPERATOR_LABEL=Desktop_Old\n"
                "UA_OPERATOR_ALIASES=OldAlias\n"
            )
        env_file.chmod(0o640)
        env_before = env_file.read_bytes()
        env_mode = env_file.stat().st_mode & 0o777
    if tokens.exists():
        tokens_before = tokens.read_bytes()
        token_mode = tokens.stat().st_mode & 0o777
    other_stage = home / ".settings.toml.other.tmp"
    other_stage.write_text("belongs to another invocation")
    real_replace = Path.replace

    def refuse_settings_publish(self, target):
        if Path(target) == settings_file:
            raise PermissionError("fixture settings publication denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", refuse_settings_publish)
    extra = ["--force"] if force else []
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_New", *extra]) == 2
    err = capsys.readouterr().err
    assert "cannot publish" in err and "restored" in err
    assert settings_file.read_bytes() == settings_before
    if token_store == "fresh":
        assert not env_file.exists()
    else:
        assert env_file.read_bytes() == env_before
        assert env_file.stat().st_mode & 0o777 == env_mode
    if tokens.exists():
        assert tokens.read_bytes() == tokens_before
        assert tokens.stat().st_mode & 0o777 == token_mode
    if token_store == "symlink":
        assert token_path.is_symlink()
    if token_store == "env_symlink":
        assert env_file.is_symlink()
        assert env_file.resolve() == env_target
    assert list(home.glob(".settings.toml.*.tmp")) == [other_stage]
    assert not list(tmp_path.glob(".tokens.*.tmp"))


@pytest.mark.parametrize("aliases", [["NewAlias"], []])
@pytest.mark.parametrize("force", [False, True])
def test_operator_migration_reconciles_service_env_overrides(
    tmp_path, monkeypatch, capsys, neutral_operator, aliases, force
):
    home = tmp_path / "ua"
    home.mkdir()
    settings_file = home / "settings.toml"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    settings_file.write_text(
        '[operator]\nname = "Old"\nlabel = "Desktop_Old"\n'
        + (f"aliases = {json.dumps(aliases)}\n" if aliases else "")
    )
    env_file = home / "service.env"
    env_file.write_text(
        "UA_OPERATOR_NAME=Old\nUA_OPERATOR_LABEL=Desktop_Old\n"
        "UA_OPERATOR_ALIASES=OldAlias\nUA_AGENT_TOKENS=Desktop_Old:fixture-old\n"
    )
    assert (
        bootstrap.init_main(
            [
                "--json",
                *(["--force"] if force else []),
                "--operator-name",
                "New",
                "--operator-label",
                "Desktop_New",
            ]
        )
        == 0
    )
    capsys.readouterr()
    runtime_env = service_env.parse_env_file(env_file)
    settings.apply_to_environ(runtime_env, path=settings_file)
    assert runtime_env["UA_OPERATOR_NAME"] == "New"
    assert runtime_env["UA_OPERATOR_LABEL"] == "Desktop_New"
    # No alias flag was supplied: the effective environment declaration wins
    # over both a stale TOML alias list and its default empty value.
    assert runtime_env["UA_OPERATOR_ALIASES"] == "OldAlias"
    token_labels = {
        label
        for label, _ in config._parse_colon_pairs("UA_AGENT_TOKENS", runtime_env["UA_AGENT_TOKENS"])
    }
    assert runtime_env["UA_OPERATOR_LABEL"] in token_labels
    for name in ("UA_OPERATOR_NAME", "UA_OPERATOR_LABEL", "UA_OPERATOR_ALIASES"):
        monkeypatch.setenv(name, runtime_env[name])
    assert config.actor_kind_for_identity("Desktop_New") == "human"
    assert config.actor_kind_for_identity("OldAlias") == "human"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("fail_first_publish", [False, True])
@pytest.mark.parametrize("lockfile_change", ["unlink", "replace"])
def test_concurrent_init_serializes_tokens_settings_and_rollback(
    tmp_path, monkeypatch, capsys, force, fail_first_publish, lockfile_change
):
    import fcntl
    import threading
    from pathlib import Path

    home = tmp_path / "ua"
    home.mkdir()
    settings_file = home / "settings.toml"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    monkeypatch.setattr(bootstrap.storage, "init_db", lambda: None)
    settings_file.write_text('[operator]\nname="Original"\nlabel="Desktop_Original"\n')
    env_file = home / "service.env"
    env_file.write_text("UA_AGENT_TOKENS=Desktop_Original:fixture-original\n")
    first_written = threading.Event()
    second_blocked = threading.Event()
    release_first = threading.Event()
    first_finished = threading.Event()
    second_personalized = threading.Event()
    results, errors = {}, []
    real_write = bootstrap._write_tokens
    real_personalization = bootstrap._personalization
    real_restore = bootstrap._restore_token_stores
    real_replace = Path.replace
    real_flock = fcntl.flock

    def pause_first_after_tokens(*args, **kwargs):
        result = real_write(*args, **kwargs)
        if threading.current_thread().name == "init-first":
            first_written.set()
            assert release_first.wait(10), "fixture did not release first initializer"
        return result

    def observe_lock(fd, operation):
        if threading.current_thread().name == "init-second" and operation == fcntl.LOCK_EX:
            # A real nonblocking probe proves A owns the lock at this exact
            # point; B then performs its normal blocking acquisition.
            try:
                real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                second_blocked.set()
            else:
                real_flock(fd, fcntl.LOCK_UN)
                raise AssertionError("second initializer entered first initializer's transaction")
        return real_flock(fd, operation)

    def observe_personalization(*args):
        if threading.current_thread().name == "init-second":
            assert first_finished.is_set(), "personalization ran before publication or rollback"
            second_personalized.set()
        return real_personalization(*args)

    def publish(path, target):
        if threading.current_thread().name == "init-first" and Path(target) == settings_file:
            if fail_first_publish:
                raise PermissionError("fixture first publication denied")
            result = real_replace(path, target)
            first_finished.set()
            return result
        return real_replace(path, target)

    def restore(snapshots):
        real_restore(snapshots)
        if threading.current_thread().name == "init-first":
            first_finished.set()

    def initialize(label):
        try:
            extra = ["--force"] if force else []
            results[label] = bootstrap.init_main(
                ["--json", "--operator-label", f"Desktop_{label}", *extra]
            )
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(bootstrap, "_write_tokens", pause_first_after_tokens)
    monkeypatch.setattr(bootstrap, "_personalization", observe_personalization)
    monkeypatch.setattr(bootstrap, "_restore_token_stores", restore)
    monkeypatch.setattr(Path, "replace", publish)
    monkeypatch.setattr(fcntl, "flock", observe_lock)
    first = threading.Thread(target=initialize, args=("First",), name="init-first")
    second = threading.Thread(target=initialize, args=("Second",), name="init-second")
    legacy_lock = home / ".init.lock"
    legacy_lock.write_text("legacy lock inode")
    first.start()
    try:
        assert first_written.wait(5), "first initializer never wrote fixture tokens"
        if lockfile_change == "unlink":
            legacy_lock.unlink()
        else:
            replacement = home / "replacement-lock"
            replacement.write_text("replacement inode")
            replacement.replace(legacy_lock)
        second.start()
        assert second_blocked.wait(5), "second initializer never contended on install lock"
        assert not second_personalized.is_set()
    finally:
        release_first.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert results == {"First": 2 if fail_first_publish else 0, "Second": 0}
    assert second_personalized.is_set()
    saved_operator = settings.load(settings_file)["operator"]["label"]
    assert saved_operator == "Desktop_Second"
    runtime_env = service_env.parse_env_file(env_file)
    token_labels = {
        label
        for label, _ in config._parse_colon_pairs("UA_AGENT_TOKENS", runtime_env["UA_AGENT_TOKENS"])
    }
    assert saved_operator in token_labels
    if force:
        assert "Desktop_First" not in token_labels
    assert not list(home.glob(".settings.toml.*.tmp"))
    capsys.readouterr()


@pytest.mark.parametrize("problem", ["acquire", "open", "home_symlink"])
def test_init_lock_failure_leaves_settings_and_tokens_unchanged(
    tmp_path, monkeypatch, capsys, problem
):
    import fcntl

    home = tmp_path / "ua"
    home.mkdir()
    settings_file = home / "settings.toml"
    env_file = home / "service.env"
    settings_file.write_text('[operator]\nlabel="Desktop_Original"\n')
    env_file.write_text("UA_AGENT_TOKENS=Desktop_Original:fixture-original\n")
    settings_before, tokens_before = settings_file.read_bytes(), env_file.read_bytes()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(
        bootstrap.storage, "init_db", lambda: pytest.fail("initialized before lock")
    )
    monkeypatch.setattr(
        bootstrap, "_personalization", lambda *args: pytest.fail("personalized before lock")
    )

    def deny_lock(*args, **kwargs):
        raise PermissionError("fixture directory lock permission denied")

    if problem == "acquire":
        monkeypatch.setattr(fcntl, "flock", deny_lock)
    elif problem == "open":
        monkeypatch.setattr(bootstrap.os, "open", deny_lock)
    else:
        target = tmp_path / "actual-installation"
        home.rename(target)
        home.symlink_to(target, target_is_directory=True)
    assert bootstrap.init_main(["--json", "--force", "--operator-label", "Desktop_New"]) == 2
    output = capsys.readouterr()
    assert "cannot acquire initialization lock" in output.err
    assert "unchanged" in output.err
    assert output.out == ""
    assert settings_file.read_bytes() == settings_before
    assert env_file.read_bytes() == tokens_before
    if problem == "home_symlink":
        assert home.is_symlink()
        assert home.resolve() == target
    assert not list(home.glob(".settings.toml.*.tmp"))
    assert not (home / ".init.lock").exists()


@pytest.mark.parametrize("kind", ["file", "fifo"])
def test_init_rejects_non_directory_home_without_personalization(
    tmp_path, monkeypatch, capsys, kind
):
    home = tmp_path / "ua"
    if kind == "file":
        home.write_text("unrelated fixture")
    else:
        os.mkfifo(home)
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setattr(
        bootstrap, "_personalization", lambda *args: pytest.fail("personalized invalid home")
    )
    assert bootstrap.init_main(["--json"]) == 2
    assert "cannot acquire initialization lock" in capsys.readouterr().err
    if kind == "file":
        assert home.read_text() == "unrelated fixture"


def test_init_directory_lock_releases_on_failure_without_changing_home(tmp_path):
    import fcntl

    home = tmp_path / "ua"
    home.mkdir(mode=0o750)
    original_stat = home.stat()
    with pytest.raises(RuntimeError, match="fixture transaction failed"):
        with bootstrap._init_lock(home):
            assert home.stat().st_ino == original_stat.st_ino
            assert home.stat().st_mode == original_stat.st_mode
            assert not (home / ".init.lock").exists()
            raise RuntimeError("fixture transaction failed")
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    with bootstrap._init_lock(home):
        assert home.stat().st_ino == original_stat.st_ino
        assert home.stat().st_mode == original_stat.st_mode
        assert not (home / ".init.lock").exists()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("relative", [False, True])
def test_settings_publish_failure_restores_dangling_service_env_link(
    tmp_path, monkeypatch, capsys, force, relative
):
    from pathlib import Path

    home = tmp_path / "ua"
    home.mkdir()
    settings_file = home / "settings.toml"
    settings_file.write_text('[operator]\nname="Original"\nlabel="Desktop_Original"\n')
    settings_before = settings_file.read_bytes()
    env_file = home / "service.env"
    target = tmp_path / "missing-environment"
    link = "../missing-environment" if relative else str(target)
    env_file.symlink_to(link)
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    monkeypatch.setattr(bootstrap.storage, "init_db", lambda: None)
    assert bootstrap._snapshot_token_stores(env_file, force=force) == [(env_file, None, None, link)]
    original_replace = Path.replace
    original_restore = bootstrap._restore_token_stores
    restored = []

    def deny_settings_publish(path, publish_target):
        if Path(publish_target) == settings_file:
            raise PermissionError("fixture publication denied")
        return original_replace(path, publish_target)

    def restore(snapshots):
        assert env_file.exists() and not env_file.is_symlink(), (
            "fixture did not provision replacement"
        )
        original_restore(snapshots)
        restored.append(True)

    monkeypatch.setattr(Path, "replace", deny_settings_publish)
    monkeypatch.setattr(bootstrap, "_restore_token_stores", restore)
    extra = ["--force"] if force else []
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_New", *extra]) == 2
    assert restored == [True]
    assert env_file.is_symlink() and not env_file.exists()
    assert os.readlink(env_file) == link
    assert not target.exists()
    assert settings_file.read_bytes() == settings_before
    assert "original token stores restored" in capsys.readouterr().err
    assert not list(home.glob(".settings.toml.*.tmp"))
    assert not list(home.glob(".service.env.*.tmp"))


def test_doctor_operator_identity_row(neutral_operator, monkeypatch):
    from unified_agent.tools import doctor

    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    monkeypatch.setattr(
        config, "agent_tokens", lambda *a, **k: {"t1": "Desktop_Alex", "t2": "Local_Worker"}
    )
    row = doctor._operator_identity_check()
    assert row["status"] == "LIVE", row
    assert "'Desktop_Alex'" in row["detail"] and "'Alex'" in row["detail"]

    # A refused label falls back silently at runtime; doctor must say so.
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Alex:Local_Worker")
    row = doctor._operator_identity_check()
    assert row["status"] == "MISCONFIGURED"
    assert "UA_OPERATOR_LABEL 'Desktop_Alex'" in row["detail"]
    assert "no bearer resolves to the operator login 'Desktop_Operator'" in row["detail"]

    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Local_Worker")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Local_Worker:synthetic-worker-token")
    row = doctor._operator_identity_check()
    assert row["status"] == "MISCONFIGURED"
    assert "Local_Worker" in row["detail"]


def test_expired_managed_worker_token_no_longer_owns_a_name(
    neutral_operator, tmp_path, monkeypatch
):
    db = tmp_path / "credentials.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Old_Worker,Live_Worker")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE agent_tokens (agent_label TEXT, org_id TEXT, status TEXT, expires_at TEXT)"
        )
        conn.execute(
            "INSERT INTO agent_tokens VALUES ('Old_Worker', NULL, 'active', '2000-01-01T00:00:00+00:00')"
        )
        conn.execute("INSERT INTO agent_tokens VALUES ('Live_Worker', NULL, 'active', NULL)")
    aliases = config.operator_aliases()
    assert "Old_Worker" in aliases  # expired: no longer authenticates, no longer a worker
    assert "Live_Worker" not in aliases


def test_settings_file_is_written_owner_private(tmp_path):
    target = settings.write({"operator": {"name": "Alex"}}, tmp_path / "settings.toml")
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_init_with_disabled_settings_persists_operator_in_service_env(
    tmp_path, monkeypatch, neutral_operator, capsys, existing, force
):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", "")
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    if existing:
        home.mkdir()
        (home / "service.env").write_text("UA_AGENT_TOKENS=Local_Worker:fixture-worker\n")
    assert (
        bootstrap.init_main(
            [
                "--json",
                "--operator-name",
                "Alex",
                "--operator-label",
                "Desktop_Alex",
                *(["--force"] if force else []),
            ]
        )
        == 0
    )
    capsys.readouterr()
    runtime = service_env.parse_env_file(home / "service.env")
    assert runtime["UA_OPERATOR_NAME"] == "Alex"
    assert runtime["UA_OPERATOR_LABEL"] == "Desktop_Alex"
    assert not (home / "settings.toml").exists()
    service_env.merge_write_env_file(
        home / "service.env", {"UA_OPERATOR_ALIASES": "Ops:Alex,AlexOther"}
    )
    # Reinitialization reads the saved pins before its own publication, even
    # when the caller has not sourced service.env into its process.
    assert bootstrap.init_main(["--json", *(["--force"] if force else [])]) == 0
    capsys.readouterr()
    runtime = service_env.parse_env_file(home / "service.env")
    assert runtime["UA_OPERATOR_NAME"] == "Alex"
    assert runtime["UA_OPERATOR_LABEL"] == "Desktop_Alex"
    assert runtime["UA_OPERATOR_ALIASES"] == "Ops:Alex,AlexOther"
    for key, value in runtime.items():
        monkeypatch.setenv(key, value)
    assert config.operator_label() == "Desktop_Alex"
    assert config.actor_kind_for_identity("Desktop_Alex") == "human"
    assert "Desktop_Alex" in {label for label, _ in config._parse_colon_pairs("UA_AGENT_TOKENS")}


def test_operator_pin_write_failure_restores_added_bearer(
    tmp_path, monkeypatch, neutral_operator, capsys
):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    env_file = home / "service.env"
    env_file.write_text(
        "UA_AGENT_TOKENS=Desktop_Old:fixture-old\n"
        "UA_OPERATOR_NAME=Old\nUA_OPERATOR_LABEL=Desktop_Old\n"
    )
    env_file.chmod(0o600)
    before = env_file.read_bytes()
    write = bootstrap.merge_write_env_file

    def deny_pins(path, updates, **kwargs):
        if "UA_OPERATOR_LABEL" in updates:
            raise PermissionError("fixture identity publication denied")
        return write(path, updates, **kwargs)

    monkeypatch.setattr(bootstrap, "merge_write_env_file", deny_pins)
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_New"]) == 2
    assert "unchanged" in capsys.readouterr().err
    assert env_file.read_bytes() == before
    assert not (home / "settings.toml").exists()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("existing_settings", [False, True])
@pytest.mark.parametrize("name_only", [False, True])
def test_no_identity_flags_keep_effective_env_operator(
    tmp_path, monkeypatch, neutral_operator, capsys, force, existing_settings, name_only
):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    settings_file = home / settings.SETTINGS_FILENAME
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    if existing_settings:
        settings_file.write_text('[operator]\nname="Old"\nlabel="Desktop_Old"\n')
    env_file = home / "service.env"
    env_file.write_text(
        "UA_OPERATOR_NAME=Alex\nUA_OPERATOR_LABEL=Desktop_Alex\n"
        "UA_OPERATOR_ALIASES=Ops:Alex,AlexOther\nUA_AGENT_TOKENS=Desktop_Alex:fixture-alex\n"
    )
    flags = ["--operator-name", "Sam"] if name_only else []
    assert bootstrap.init_main(["--json", *flags, *(["--force"] if force else [])]) == 0
    capsys.readouterr()
    runtime = service_env.parse_env_file(env_file)
    saved = settings.load(settings_file)["operator"]
    assert saved == {
        "name": "Sam" if name_only else "Alex",
        "label": "Desktop_Alex",
        "aliases": ["Ops:Alex", "AlexOther"],
    }
    assert runtime["UA_OPERATOR_LABEL"] == "Desktop_Alex"
    assert runtime["UA_OPERATOR_NAME"] == ("Sam" if name_only else "Alex")
    assert runtime["UA_OPERATOR_ALIASES"] == "Ops:Alex,AlexOther"
    for key, value in runtime.items():
        monkeypatch.setenv(key, value)
    assert config.actor_kind_for_identity("Desktop_Alex") == "human"


@pytest.mark.parametrize("new_bearer", [False, True])
def test_nonforce_identity_edit_preserves_raw_unrelated_env(
    tmp_path, monkeypatch, neutral_operator, capsys, new_bearer
):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / settings.SETTINGS_FILENAME))
    # The pinned workspace below expands $HOME; keep it inside the test tree.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    settings.write({"operator": {"name": "Alex", "label": "Desktop_Alex"}})
    env_file = home / "service.env"
    raw_workspace = "# keep shell expressions and comments\nUA_WORKSPACE=$HOME/workspace\n"
    env_file.write_text(
        raw_workspace + "UA_OPERATOR_NAME=Alex\nUA_OPERATOR_LABEL=Desktop_Alex\n"
        "UA_AGENT_TOKENS=Desktop_Alex:fixture-alex\n"
    )
    identity_flags = ["--operator-name", "Sam"]
    if new_bearer:
        identity_flags += ["--operator-label", "Desktop_Sam"]
    assert bootstrap.init_main(["--json", *identity_flags]) == 0
    capsys.readouterr()
    assert env_file.read_text().startswith(raw_workspace)
    assert service_env.parse_env_file(env_file)["UA_OPERATOR_NAME"] == "Sam"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("existing_bearer", [False, True])
@pytest.mark.parametrize("label_expression", ["${HUMAN_LABEL}", '"$HUMAN_LABEL"'])
def test_init_resolves_operator_variable_pins_without_rewriting_them(
    tmp_path, monkeypatch, neutral_operator, capsys, force, existing_bearer, label_expression
):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    settings_file = home / settings.SETTINGS_FILENAME
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    settings.write({"operator": {"name": "Old", "label": "Desktop_Old"}})
    monkeypatch.setenv("HUMAN_NAME", "Alex")
    monkeypatch.setenv("HUMAN_LABEL", "Desktop_Alex")
    monkeypatch.setenv("HUMAN_ALIASES", "Ops:Alex,AlexOther")
    # A TOML-seeded UA_OPERATOR_LABEL is not evidence of a sourced pin.
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Old")
    env_file = home / "service.env"
    pins = (
        f"UA_OPERATOR_NAME=$HUMAN_NAME\nUA_OPERATOR_LABEL={label_expression}\n"
        "UA_OPERATOR_ALIASES=$HUMAN_ALIASES\n"
    )
    # An already pinned workspace (#1825) keeps init from adding that stanza.
    env_file.write_text(
        pins
        + f"UA_WORKSPACE={tmp_path / 'ws'}\n"
        + "UA_AGENT_TOKENS="
        + ("Desktop_Alex:fixture-alex" if existing_bearer else "Local_Worker:fixture-worker")
        + "\n"
    )
    before = env_file.read_bytes()
    assert bootstrap.init_main(["--json", *(["--force"] if force else [])]) == 0
    capsys.readouterr()
    assert settings.load(settings_file)["operator"] == {
        "name": "Alex",
        "label": "Desktop_Alex",
        "aliases": ["Ops:Alex", "AlexOther"],
    }
    if not force:
        assert env_file.read_text().startswith(pins)
        if existing_bearer:
            assert env_file.read_bytes() == before
    labels = {
        label
        for label, _ in config._parse_colon_pairs(
            "UA_AGENT_TOKENS", service_env.parse_env_file(env_file)["UA_AGENT_TOKENS"]
        )
    }
    assert "Desktop_Alex" in labels


@pytest.mark.parametrize(
    "expression",
    [
        "$UNSET_HUMAN_LABEL",
        "$(touch forbidden)",
        "'$HUMAN_LABEL'",
        r"\$HUMAN_LABEL",
        r'"\$HUMAN_LABEL"',
    ],
)
def test_init_unresolved_operator_pin_fails_without_changes(
    tmp_path, monkeypatch, neutral_operator, capsys, expression
):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    settings_file = home / settings.SETTINGS_FILENAME
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    monkeypatch.delenv("UNSET_HUMAN_LABEL", raising=False)
    monkeypatch.setenv("HUMAN_LABEL", "Desktop_Alex")
    settings.write({"operator": {"name": "Alex", "label": "Desktop_Alex"}})
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    env_file = home / "service.env"
    env_file.write_text(
        "UA_OPERATOR_LABEL=" + expression + "\nUA_AGENT_TOKENS=Local_Worker:fixture\n"
    )
    before = env_file.read_bytes(), settings_file.read_bytes()
    assert bootstrap.init_main(["--json"]) == 2
    err = capsys.readouterr().err
    assert "cannot resolve UA_OPERATOR_LABEL" in err
    assert expression not in err
    assert (env_file.read_bytes(), settings_file.read_bytes()) == before
    # An explicit migration replaces an unresolved pin without executing it.
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_New"]) == 0
    capsys.readouterr()
    assert service_env.parse_env_file(env_file)["UA_OPERATOR_LABEL"] == "Desktop_New"


@pytest.mark.parametrize("saved", [False, True])
def test_init_honors_external_identity_without_installed_pins(
    tmp_path, monkeypatch, neutral_operator, capsys, saved
):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    settings_file = home / settings.SETTINGS_FILENAME
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    if saved:
        settings.write({"operator": {"name": "Old", "label": "Desktop_Old"}})
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Ops:Alex")
    monkeypatch.setattr(settings, "_APPLIED", {})
    assert bootstrap.init_main(["--json"]) == 0
    capsys.readouterr()
    assert settings.load(settings_file)["operator"] == {
        "name": "Alex",
        "label": "Desktop_Alex",
        "aliases": ["Ops:Alex"],
    }
    pairs = config._parse_colon_pairs(
        "UA_AGENT_TOKENS", service_env.parse_env_file(home / "service.env")["UA_AGENT_TOKENS"]
    )
    assert "Desktop_Alex" in {label for label, _ in pairs}


def test_init_does_not_treat_prior_toml_seed_as_external_identity(
    tmp_path, monkeypatch, neutral_operator, capsys
):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    settings_file = home / settings.SETTINGS_FILENAME
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    settings.write({"operator": {"name": "Alex", "label": "Desktop_Alex"}})
    stale = {"UA_OPERATOR_NAME": "Old", "UA_OPERATOR_LABEL": "Desktop_Old"}
    for key, value in stale.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "_APPLIED", stale)
    assert bootstrap.init_main(["--json"]) == 0
    capsys.readouterr()
    assert settings.load(settings_file)["operator"]["label"] == "Desktop_Alex"


@pytest.mark.parametrize("name,label", [("José", "Desktop_Jose"), ("李明", "Desktop_Operator")])
def test_init_unicode_display_name_has_valid_default_login(
    tmp_path, monkeypatch, neutral_operator, capsys, name, label
):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    settings_file = home / settings.SETTINGS_FILENAME
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    assert bootstrap.init_main(["--json", "--operator-name", name]) == 0
    capsys.readouterr()
    assert settings.load(settings_file)["operator"] == {"name": name, "label": label}
    assert config.agent_label_is_well_formed(label)


def _interactive_init(tmp_path, monkeypatch, capsys, answers, prior_vault):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    settings_file = home / settings.SETTINGS_FILENAME
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    workspace = str(tmp_path / "ws")
    seed = ["--operator-name", "Alex", "--workspace", workspace, "--vault", prior_vault]
    assert bootstrap.init_main(["--json", *seed]) == 0
    capsys.readouterr()
    prompts = []
    replies = iter(answers)

    def fake_input(prompt):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr("builtins.input", fake_input)
    assert bootstrap.init_main(["--json", "--interactive"]) == 0
    capsys.readouterr()
    return settings.load(settings_file)["paths"]["vault"], prompts


def test_interactive_blank_vault_answer_keeps_saved_path_and_says_so(
    tmp_path, monkeypatch, neutral_operator, capsys
):
    saved = str(tmp_path / "vault")
    vault, prompts = _interactive_init(tmp_path, monkeypatch, capsys, ["", "", "", ""], saved)
    assert vault == saved
    vault_prompt = next(p for p in prompts if p.startswith("Obsidian"))
    assert "blank keeps the current path" in vault_prompt
    assert "blank to disable" not in vault_prompt


def test_interactive_dash_disables_a_saved_vault(tmp_path, monkeypatch, neutral_operator, capsys):
    vault, _ = _interactive_init(
        tmp_path, monkeypatch, capsys, ["", "", "", "-"], str(tmp_path / "vault")
    )
    assert vault == ""


def test_interactive_blank_vault_answer_disables_when_none_is_saved(
    tmp_path, monkeypatch, neutral_operator, capsys
):
    vault, prompts = _interactive_init(tmp_path, monkeypatch, capsys, ["", "", "", ""], "")
    assert vault == ""
    assert "blank to disable" in next(p for p in prompts if p.startswith("Obsidian"))


def test_name_only_change_keeps_login_and_help_says_so(
    tmp_path, monkeypatch, neutral_operator, capsys
):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / settings.SETTINGS_FILENAME))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json", "--operator-name", "Alex"]) == 0
    assert bootstrap.init_main(["--json", "--operator-name", "Sam"]) == 0
    capsys.readouterr()
    saved = settings.load(home / settings.SETTINGS_FILENAME)["operator"]
    assert saved == {"name": "Sam", "label": "Desktop_Alex"}
    with pytest.raises(SystemExit):
        bootstrap.init_main(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "never renames the login" in help_text


@pytest.mark.parametrize(
    "value", ["Alex\nExample", "Alex\rExample", "Alex\tExample", "Alex\b\f\x1f\x7fExample"]
)
def test_settings_control_characters_round_trip(settings_file, value):
    settings.set_value("operator.name", value, settings_file)
    assert settings.load(settings_file)["operator"]["name"] == value


def test_settings_stage_rejects_unparseable_render_before_publication(settings_file, monkeypatch):
    settings.write({"operator": {"name": "Alex"}}, settings_file)
    before = settings_file.read_bytes()
    monkeypatch.setattr(settings, "render", lambda data: '[operator]\nname="Alex\nExample"\n')
    with pytest.raises(settings.SettingsError, match="rendered settings"):
        settings.write({"operator": {"name": "Example"}}, settings_file)
    assert settings_file.read_bytes() == before
    assert not list(settings_file.parent.glob(".settings.toml.*.tmp"))


@pytest.mark.parametrize(
    "dotted", ["operator.name", "operator.aliases", "paths.vault_write_dirs", "env.UA_CUSTOM_NAME"]
)
def test_settings_rejects_nul_without_replacing_file(settings_file, dotted):
    settings.write({"operator": {"name": "Alex", "label": "Desktop_Alex"}}, settings_file)
    before = settings_file.read_bytes()
    with pytest.raises(settings.SettingsError, match="contains NUL"):
        settings.set_value(dotted, "fixture\x00value", settings_file)
    assert settings_file.read_bytes() == before
    assert not list(settings_file.parent.glob(".settings.toml.*.tmp"))


def test_manual_nul_setting_does_not_crash_environment_loading(settings_file, monkeypatch, capsys):
    settings_file.write_text('[operator]\nname="Alex\\u0000Example"\nlabel="Desktop_Alex"\n')
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_OPERATOR_LABEL", raising=False)
    applied = settings.apply_to_environ(path=settings_file)
    assert "UA_OPERATOR_NAME" not in os.environ
    assert applied["UA_OPERATOR_LABEL"] == "Desktop_Alex"
    assert "contains NUL" in capsys.readouterr().err


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_explicit_migration_pins_changed_external_identity_for_restart(
    tmp_path, monkeypatch, neutral_operator, capsys, existing, force
):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / settings.SETTINGS_FILENAME))
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    monkeypatch.setattr(settings, "_APPLIED", {})
    if existing:
        assert bootstrap.init_main(["--json"]) == 0
        capsys.readouterr()
    assert (
        bootstrap.init_main(
            [
                "--json",
                *(["--force"] if force else []),
                "--operator-name",
                "Sam",
                "--operator-label",
                "Desktop_Sam",
            ]
        )
        == 0
    )
    capsys.readouterr()
    runtime = service_env.parse_env_file(home / "service.env")
    assert runtime["UA_OPERATOR_LABEL"] == "Desktop_Sam"
    assert runtime["UA_OPERATOR_NAME"] == "Sam"
    assert bootstrap.init_main(["--json", "--operator-label", "Desktop_Sam"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize("flag", ["--operator-name", "--operator-label"])
def test_repeating_effective_operator_flag_keeps_variable_pin(
    tmp_path, monkeypatch, neutral_operator, capsys, flag
):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    settings_file = home / settings.SETTINGS_FILENAME
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    monkeypatch.setenv("HUMAN_NAME", "Alex")
    monkeypatch.setenv("HUMAN_LABEL", "Desktop_Alex")
    env_file = home / "service.env"
    pins = "UA_OPERATOR_NAME=$HUMAN_NAME\nUA_OPERATOR_LABEL=$HUMAN_LABEL\n"
    # An already pinned workspace (#1825) keeps init from adding that stanza.
    env_file.write_text(
        pins + f"UA_WORKSPACE={tmp_path / 'ws'}\nUA_AGENT_TOKENS=Desktop_Alex:fixture-alex\n"
    )
    before = env_file.read_bytes()
    value = "Alex" if flag == "--operator-name" else "Desktop_Alex"
    assert bootstrap.init_main(["--json", flag, value]) == 0
    capsys.readouterr()
    assert env_file.read_bytes() == before
    # The preserved reference keeps working after its source variable changes.
    monkeypatch.setenv("HUMAN_NAME", "Sam")
    monkeypatch.setenv("HUMAN_LABEL", "Desktop_Sam")
    assert bootstrap.init_main(["--json"]) == 0
    capsys.readouterr()
    assert env_file.read_text().startswith(pins)
    assert settings.load(settings_file)["operator"]["label"] == "Desktop_Sam"


@pytest.mark.parametrize("env_first", [False, True])
@pytest.mark.parametrize(
    "field,key,value",
    [
        ("name", "UA_OPERATOR_NAME", "Alex"),
        ("label", "UA_OPERATOR_LABEL", "Desktop_Alex"),
        ("aliases", "UA_OPERATOR_ALIASES", ["Ops:Alex"]),
    ],
)
def test_invalid_env_override_keeps_valid_typed_operator(
    settings_file, field, key, value, env_first
):
    operator = {"operator": {field: value}}
    invalid = {"env": {key: "fixture\x00value"}}
    data = {**invalid, **operator} if env_first else {**operator, **invalid}
    mapped, warnings = settings.to_env(data)
    assert mapped[key] == ("Ops:Alex" if field == "aliases" else value)
    assert any(key in warning and "contains NUL" in warning for warning in warnings)
    settings_file.write_text(settings.render(data))
    env = {}
    applied = settings.apply_to_environ(env, path=settings_file)
    assert applied[key] == mapped[key]
    assert env[key] == mapped[key]


@pytest.mark.parametrize("rewrite", ["write", "set", "init"])
def test_project_tables_survive_settings_rewrites(
    settings_file, neutral_operator, rewrite, monkeypatch
):
    entries = [
        {
            "id": "unified-agent",
            "label": 'Demo "project"',
            "repo": "example/demo",
            "dirname": "demo",
        }
    ]
    settings_file.write_text(
        '[operator]\nname = "Alex"\nlabel = "Desktop_Alex"\n'
        '[projects]\nentries = [{id = "unified-agent", label = "Demo \\"project\\"", '
        'repo = "example/demo", dirname = "demo"}]\n'
    )
    if rewrite == "write":
        settings.write(settings.load(settings_file), settings_file)
    elif rewrite == "set":
        settings.set_value("server.port", "9123", settings_file)
    else:
        assert bootstrap.init_main(["--json"]) == 0
    loaded = settings.load(settings_file)
    assert loaded["projects"]["entries"] == entries
    values, warnings = settings.to_env(loaded)
    assert warnings == []
    assert json.loads(values["UA_PROJECTS_JSON"]) == entries
    monkeypatch.setenv("UA_PROJECTS_JSON", values["UA_PROJECTS_JSON"])
    assert config.project_registry()["unified-agent"]["dirname"] == "demo"


def test_settings_cli_sets_project_tables(settings_file, monkeypatch):
    entries = [{"id": "unified-agent", "label": "Demo", "repo": "example/demo", "dirname": "demo"}]
    assert settings.run_settings_cli(["set", "projects.entries", json.dumps(entries)]) == 0
    assert settings.load(settings_file)["projects"]["entries"] == entries
    values, warnings = settings.to_env(settings.load(settings_file))
    assert warnings == []
    monkeypatch.setenv("UA_PROJECTS_JSON", values["UA_PROJECTS_JSON"])
    assert config.project_registry()["unified-agent"]["dirname"] == "demo"
    original = settings_file.read_bytes()
    assert settings.run_settings_cli(["set", "projects.entries", "{invalid"]) == 2
    assert settings_file.read_bytes() == original


# --- forced init with a file-backed token store -----------------------------


def _file_backed_home(tmp_path, monkeypatch, body, *, mode=0o600, link=False):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    tokens = tmp_path / "tokens"
    tokens.write_text(body)
    tokens.chmod(mode)
    path = tokens
    if link:
        path = tmp_path / "current"
        path.symlink_to(tokens)
    env_file = home / "service.env"
    env_file.write_text(f"UA_HTTP_PORT=9999\nUA_AGENT_TOKENS={path}\n")
    return home, env_file, tokens, path


@pytest.mark.parametrize("link", [False, True])
def test_force_init_keeps_a_file_backed_token_store(
    tmp_path, monkeypatch, capsys, neutral_operator, link
):
    """--force rotates this host's two bearers inside the token file; the path
    stays in service.env and every other agent's token still authenticates."""
    label = bootstrap._default_token_label()
    body = f"# fleet tokens\nworker:old-token\n{label}:stale-host,other:other-token\n"
    home, env_file, tokens, path = _file_backed_home(
        tmp_path, monkeypatch, body, mode=0o644, link=link
    )
    argv = ["--json", "--print-token", "--force", "--operator-label", "Desktop_Alex"]
    assert bootstrap.init_main(argv) == 0
    out = json.loads(capsys.readouterr().out)

    env = service_env.parse_env_file(env_file)
    assert env["UA_AGENT_TOKENS"] == str(path)
    assert env["UA_HTTP_PORT"] == "9999"
    assert tokens.read_text() == (
        f"# fleet tokens\nworker:old-token\n{label}:{out['token']},other:other-token\n"
        f"Desktop_Alex:{out['operator_token']}\n"
    )
    assert tokens.stat().st_mode & 0o777 == 0o600
    if link:
        assert path.is_symlink() and path.resolve() == tokens
    assert config.agent_tokens(str(path), include_managed=False) == {
        "old-token": "worker",
        "other-token": "other",
        out["token"]: label,
        out["operator_token"]: "Desktop_Alex",
    }
    assert not list(tmp_path.glob(".tokens.*.tmp"))


def test_force_init_replaces_the_operator_entry_in_a_token_file(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    home, env_file, tokens, path = _file_backed_home(
        tmp_path, monkeypatch, "worker:old-token\ndesktop_alex:old-operator\n"
    )
    argv = ["--json", "--print-token", "--force", "--operator-label", "Desktop_Alex"]
    assert bootstrap.init_main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    label = bootstrap._default_token_label()
    assert tokens.read_text() == (
        f"worker:old-token\nDesktop_Alex:{out['operator_token']}\n{label}:{out['token']}\n"
    )


def test_force_init_with_an_unreadable_token_file_changes_nothing(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    home, env_file, tokens, path = _file_backed_home(tmp_path, monkeypatch, "worker:old-token\n")
    tokens.unlink()
    env_before = env_file.read_bytes()
    assert bootstrap.init_main(["--json", "--force"]) == 2
    assert "cannot update token file" in capsys.readouterr().err
    assert env_file.read_bytes() == env_before
    assert not tokens.exists()
    assert not (home / "settings.toml").exists()


def test_force_init_restores_the_token_file_when_service_env_cannot_be_written(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    home, env_file, tokens, path = _file_backed_home(
        tmp_path, monkeypatch, "worker:old-token\n", mode=0o640
    )
    env_before = env_file.read_bytes()

    def refuse(*args, **kwargs):
        raise PermissionError("fixture service.env write denied")

    monkeypatch.setattr(bootstrap, "write_env_file", refuse)
    assert bootstrap.init_main(["--json", "--force"]) == 2
    assert "unchanged" in capsys.readouterr().err
    assert tokens.read_text() == "worker:old-token\n"
    assert tokens.stat().st_mode & 0o777 == 0o640
    assert env_file.read_bytes() == env_before
    assert not (home / "settings.toml").exists()


def test_force_init_rotates_this_hosts_inline_tokens_and_keeps_others(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    """--force rotates only this host's two inline bearers (task #1797).

    It used to replace the whole inline store with the template's two labels,
    which removed every other agent's bearer.
    """
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    env_file = home / "service.env"
    env_file.write_text("UA_AGENT_TOKENS=worker:old-token\n")
    assert bootstrap.init_main(["--json", "--print-token", "--force"]) == 0
    out = json.loads(capsys.readouterr().out)
    inline = service_env.parse_env_file(env_file)["UA_AGENT_TOKENS"]
    assert inline == (
        f"worker:old-token,{out['token_label']}:{out['token']},"
        f"{out['operator']['label']}:{out['operator_token']}"
    )


# --- workspace pin in service.env (#1806, #1825) -----------------------------

# The block `init` wrote on settings-enabled installs before #1825.
_LEGACY_WORKSPACE_BLOCK = (
    "# Sandbox root for fs_*/run_cmd. It lives in settings.toml (paths.workspace);\n"
    "# edit it there. An uncommented line here would win over settings.toml.\n"
    "# UA_WORKSPACE={workspace}\n"
)


def _settings_install(tmp_path, monkeypatch):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    settings_file = home / settings.SETTINGS_FILENAME
    monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    return home, settings_file


def _uncommented_workspace_lines(env_file):
    return [
        line
        for line in env_file.read_text().splitlines()
        if line.strip().startswith("UA_WORKSPACE=")
    ]


def test_init_with_settings_enabled_pins_the_workspace_in_service_env(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    """After init, a paths.workspace edit in settings.toml must not move the sandbox."""
    home, settings_file = _settings_install(tmp_path, monkeypatch)
    assert bootstrap.init_main(["--json"]) == 0
    capsys.readouterr()
    env_file = home / "service.env"
    assert service_env.parse_env_file(env_file)["UA_WORKSPACE"] == str(home / "workspace")
    assert len(_uncommented_workspace_lines(env_file)) == 1
    assert "# UA_WORKSPACE=" not in env_file.read_text()
    assert "authoritative" in env_file.read_text()
    assert settings.load(settings_file)["paths"]["workspace"] == str(home / "workspace")

    # Simulate a restart: the unit loads service.env, then settings.toml seeds
    # whatever the environment file did not set. The edit cannot move the root.
    settings.write(
        {**settings.load(settings_file), "paths": {"workspace": str(tmp_path / "moved")}},
        settings_file,
    )
    runtime = dict(service_env.parse_env_file(env_file))
    settings.apply_to_environ(runtime, path=settings_file, quiet=True)
    assert runtime["UA_WORKSPACE"] == str(home / "workspace")


@pytest.mark.parametrize("force", [False, True])
def test_reinit_pins_the_effective_settings_workspace_over_a_commented_line(
    tmp_path, monkeypatch, capsys, neutral_operator, force
):
    """An install from before #1825 keeps the workspace it was running with."""
    home, settings_file = _settings_install(tmp_path, monkeypatch)
    effective = tmp_path / "custom-ws"
    settings.write(
        {
            "operator": {"name": "Alex", "label": "Desktop_Alex"},
            "paths": {"workspace": str(effective)},
        },
        settings_file,
    )
    env_file = home / "service.env"
    env_file.write_text(
        "UA_TRANSPORT=stdio\n"
        + _LEGACY_WORKSPACE_BLOCK.format(workspace=home / "workspace")
        + "UA_AGENT_TOKENS=Desktop_Alex:fixture-alex\n"
    )
    assert bootstrap.init_main(["--json", *(["--force"] if force else [])]) == 0
    capsys.readouterr()
    body = env_file.read_text()
    assert service_env.parse_env_file(env_file)["UA_WORKSPACE"] == str(effective)
    assert len(_uncommented_workspace_lines(env_file)) == 1
    assert "# UA_WORKSPACE=" not in body
    assert "edit it there" not in body
    assert settings.load(settings_file)["paths"]["workspace"] == str(effective)
    if not force:
        # A targeted edit keeps everything else in place.
        assert body.startswith("UA_TRANSPORT=stdio\n")
        assert "UA_AGENT_TOKENS=Desktop_Alex:fixture-alex" in body


@pytest.mark.parametrize("force", [False, True])
def test_init_warns_when_settings_workspace_differs_from_the_pin(
    tmp_path, monkeypatch, capsys, neutral_operator, force
):
    home, settings_file = _settings_install(tmp_path, monkeypatch)
    pinned = tmp_path / "pinned-ws"
    stale = tmp_path / "settings-ws"
    settings.write(
        {"operator": {"name": "Alex", "label": "Desktop_Alex"}, "paths": {"workspace": str(stale)}},
        settings_file,
    )
    env_file = home / "service.env"
    env_file.write_text(f"UA_WORKSPACE={pinned}\nUA_AGENT_TOKENS=Desktop_Alex:fixture-alex\n")
    assert bootstrap.init_main(["--json", *(["--force"] if force else [])]) == 0
    captured = capsys.readouterr()
    assert str(pinned) in captured.err
    assert str(stale) in captured.err
    assert "ignored" in captured.err
    assert json.loads(captured.out)["warnings"]
    assert service_env.parse_env_file(env_file)["UA_WORKSPACE"] == str(pinned)
    assert settings.load(settings_file)["paths"]["workspace"] == str(pinned)


def test_init_does_not_warn_when_settings_workspace_matches_the_pin(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    home, settings_file = _settings_install(tmp_path, monkeypatch)
    assert bootstrap.init_main(["--json"]) == 0
    capsys.readouterr()
    assert bootstrap.init_main(["--json"]) == 0
    captured = capsys.readouterr()
    assert "ignored" not in captured.err
    assert json.loads(captured.out)["warnings"] == []


@pytest.mark.parametrize("force", [False, True])
def test_init_with_settings_disabled_pins_the_workspace_in_service_env(
    tmp_path, monkeypatch, capsys, neutral_operator, force
):
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", "")
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    assert bootstrap.init_main(["--json"]) == 0
    if force:
        assert bootstrap.init_main(["--json", "--force", "--workspace", str(tmp_path / "ws")]) == 0
    capsys.readouterr()
    expected = tmp_path / "ws" if force else home / "workspace"
    assert service_env.parse_env_file(home / "service.env")["UA_WORKSPACE"] == str(expected)


@pytest.mark.parametrize("force", [False, True])
def test_reinit_without_workspace_flag_keeps_a_custom_pin(
    tmp_path, monkeypatch, capsys, neutral_operator, force
):
    """Re-running init never resets a pinned workspace to the code default."""
    home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", "")
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    custom = tmp_path / "ws"
    assert bootstrap.init_main(["--json", "--workspace", str(custom)]) == 0
    capsys.readouterr()
    assert bootstrap.init_main(["--json", *(["--force"] if force else [])]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["workspace"] == str(custom)
    assert service_env.parse_env_file(home / "service.env")["UA_WORKSPACE"] == str(custom)


@pytest.mark.parametrize("force", [False, True])
def test_init_with_an_explicit_workspace_moves_the_pin(
    tmp_path, monkeypatch, capsys, neutral_operator, force
):
    home, settings_file = _settings_install(tmp_path, monkeypatch)
    env_file = home / "service.env"
    env_file.write_text("UA_WORKSPACE=/old/pinned\nUA_TRANSPORT=stdio\n")
    new_ws = tmp_path / "ws"
    args = ["--json", "--workspace", str(new_ws), *(["--force"] if force else [])]
    assert bootstrap.init_main(args) == 0
    captured = capsys.readouterr()
    assert "ignored" not in captured.err
    assert service_env.parse_env_file(env_file)["UA_WORKSPACE"] == str(new_ws)
    assert len(_uncommented_workspace_lines(env_file)) == 1
    assert settings.load(settings_file)["paths"]["workspace"] == str(new_ws)

    # An env file with no workspace line at all also gets the pin.
    env_file.write_text("UA_TRANSPORT=stdio\n")
    assert bootstrap.init_main(args) == 0
    capsys.readouterr()
    assert service_env.parse_env_file(env_file)["UA_WORKSPACE"] == str(new_ws)


def test_init_refuses_an_unresolvable_workspace_pin_expression(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    home, settings_file = _settings_install(tmp_path, monkeypatch)
    monkeypatch.delenv("UA_TEST_UNSET_WS_ROOT", raising=False)
    env_file = home / "service.env"
    env_file.write_text("UA_WORKSPACE=$UA_TEST_UNSET_WS_ROOT/ws\n")
    before = env_file.read_bytes()
    assert bootstrap.init_main(["--json"]) == 2
    assert "--workspace" in capsys.readouterr().err
    assert env_file.read_bytes() == before
    assert not settings_file.exists()


# --- forced re-init revokes the replaced operator (task #1797 review) ----------


def _labels(raw: str) -> set[str]:
    return {
        entry.partition(":")[0].strip()
        for line in raw.splitlines()
        for entry in line.split(",")
        if ":" in entry and not line.lstrip().startswith("#")
    }


def test_force_init_retires_the_replaced_operator_in_a_token_file(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    home, env_file, tokens, path = _file_backed_home(
        tmp_path, monkeypatch, "worker:old-token\nDesktop_Old:old-operator\n"
    )
    (home / "settings.toml").write_text('[operator]\nname="Old"\nlabel="Desktop_Old"\n')
    argv = ["--json", "--print-token", "--force", "--operator-label", "Desktop_New"]
    assert bootstrap.init_main(argv) == 0
    capsys.readouterr()
    labels = _labels(tokens.read_text())
    assert "Desktop_Old" not in labels
    assert {"worker", "Desktop_New"} <= labels


def test_force_init_retires_the_replaced_operator_inline(
    tmp_path, monkeypatch, capsys, neutral_operator
):
    home = tmp_path / "ua"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_SETTINGS_FILE", str(home / "settings.toml"))
    monkeypatch.setattr(config, "db_path", lambda: home / "memory.db")
    (home / "settings.toml").write_text('[operator]\nname="Old"\nlabel="Desktop_Old"\n')
    env_file = home / "service.env"
    env_file.write_text("UA_AGENT_TOKENS=worker:keep,Desktop_Old:old-operator\n")
    argv = ["--json", "--print-token", "--force", "--operator-label", "Desktop_New"]
    assert bootstrap.init_main(argv) == 0
    capsys.readouterr()
    labels = _labels(service_env.parse_env_file(env_file)["UA_AGENT_TOKENS"])
    assert "Desktop_Old" not in labels
    assert {"worker", "Desktop_New"} <= labels


def test_previous_operator_follows_a_variable_pin_not_stale_settings(tmp_path, monkeypatch):
    """A $VAR pin is resolved as at runtime; settings.toml is not a fallback for it."""
    env_file = tmp_path / "service.env"
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text('[operator]\nname="Other"\nlabel="Desktop_Other"\n')
    env_file.write_text("UA_OPERATOR_LABEL=$OPLBL\n")
    monkeypatch.setenv("OPLBL", "Desktop_Cur")
    assert bootstrap._previous_operator_label(env_file, settings_file) == "Desktop_Cur"
    monkeypatch.delenv("OPLBL")
    assert bootstrap._previous_operator_label(env_file, settings_file) is None
    env_file.write_text("UA_OPERATOR_LABEL=$(whoami)\n")
    assert bootstrap._previous_operator_label(env_file, settings_file) is None
    env_file.write_text("UA_HTTP_PORT=9999\n")
    assert bootstrap._previous_operator_label(env_file, settings_file) == "Desktop_Other"
