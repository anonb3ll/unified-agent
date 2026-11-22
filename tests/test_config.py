import os
from pathlib import Path

import pytest

from unified_agent import bootstrap, config, storage


def test_vault_sync_trees_deduplicate_overlapping_paths(monkeypatch):
    monkeypatch.setenv("UA_VAULT_SYNC_TREES", "Wiki:Wiki:Logs/Laptop:Logs:Logs")
    assert config.vault_sync_synced_trees() == ("Wiki", "Logs")


def test_vault_sync_respects_disabled_vault_unless_root_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_VAULT_PATH", "")
    monkeypatch.delenv("UA_VAULT_SYNC_LOCAL_ROOT", raising=False)
    with pytest.raises(ValueError, match="UA_VAULT_PATH is disabled"):
        config.vault_sync_local_root()
    monkeypatch.setenv("UA_VAULT_SYNC_LOCAL_ROOT", str(tmp_path))
    assert config.vault_sync_local_root() == tmp_path


class TestAgentWipOverrides:
    """UA_AGENT_WIP_OVERRIDES parsing and the per-agent cap lookup."""

    @pytest.fixture(autouse=True)
    def _default_cap(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WIP_MAX", "2")
        monkeypatch.setattr(config, "_POSTURE_WARNED", set())

    def test_malformed_entries_are_ignored_and_warn_once(self, monkeypatch, capsys):
        raw = "nocolon, :5, lead_agent:abc, worker_agent:3, worker_agent:9"
        monkeypatch.setenv("UA_AGENT_WIP_OVERRIDES", raw)
        assert config.agent_wip_max_for("lead_agent") == 2
        assert config.agent_wip_max_for("worker_agent") == 3  # first duplicate wins
        assert config.agent_wip_max_for("nocolon") == 2
        err = capsys.readouterr().err
        assert err.count("UA_AGENT_WIP_OVERRIDES") == 1
        assert "nocolon" in err

    def test_parsing_tolerates_whitespace_and_floors_at_one(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WIP_OVERRIDES", "  lead_agent : 8 ,worker_agent:0,")
        assert config.agent_wip_overrides() == {"lead_agent": 8, "worker_agent": 1}

    def test_unset_means_default_for_everyone(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_WIP_OVERRIDES", raising=False)
        assert config.agent_wip_max_for("lead_agent") == 2
        assert config.agent_wip_max_for(None) == 2


class TestWipPolicyDefaults:
    """The documented WIP policy and the enforced one are the same numbers (#2759)."""

    @pytest.fixture(autouse=True)
    def _unset(self, monkeypatch):
        for var in (
            "UA_AGENT_WIP_MAX",
            "UA_AGENT_WIP_OVERRIDES",
            "UA_FLEET_WIP_MAX",
            "UA_FLEET_WIP_HARD_MAX",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_defaults_match_the_documented_policy(self):
        assert config.agent_wip_max() == 20
        assert config.fleet_wip_max() == 40
        assert config.fleet_wip_hard_max() == 80

    def test_fleet_ceiling_sits_above_the_per_agent_cap(self):
        assert config.fleet_wip_max() > config.agent_wip_max()
        assert config.fleet_wip_hard_max() > config.fleet_wip_max()

    def test_malformed_values_fall_back_to_the_defaults(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WIP_MAX", "many")
        monkeypatch.setenv("UA_FLEET_WIP_MAX", "lots")
        monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "plenty")
        assert config.agent_wip_max() == 20
        assert config.fleet_wip_max() == 40
        assert config.fleet_wip_hard_max() == 80

    def test_hard_cap_never_sits_below_the_soft_cap(self, monkeypatch):
        monkeypatch.setenv("UA_FLEET_WIP_MAX", "50")
        monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
        assert config.fleet_wip_hard_max() == 50


class TestWorkspaceRoot:
    def test_defaults_to_env_var(self, monkeypatch):
        monkeypatch.setenv("UA_WORKSPACE", "/tmp/some-workspace")
        assert config.workspace_root() == Path("/tmp/some-workspace")

    def test_contextvar_overrides_env_var(self, monkeypatch):
        monkeypatch.setenv("UA_WORKSPACE", "/tmp/env-workspace")
        token = config.workspace_root_var.set(Path("/tmp/context-workspace"))
        try:
            assert config.workspace_root() == Path("/tmp/context-workspace")
        finally:
            config.workspace_root_var.reset(token)
        # env var value returns once the contextvar is reset
        assert config.workspace_root() == Path("/tmp/env-workspace")


class TestReviewPytestTimeoutS:
    """Task #1254: UA_REVIEW_PYTEST_TIMEOUT_S overrides merge._run_pytest's
    wall-clock ceiling (default 1800, was hardcoded at 600)."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("UA_REVIEW_PYTEST_TIMEOUT_S", raising=False)
        assert config.review_pytest_timeout_s() == 1800

    def test_default_when_empty(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_PYTEST_TIMEOUT_S", "")
        assert config.review_pytest_timeout_s() == 1800

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_PYTEST_TIMEOUT_S", "900")
        assert config.review_pytest_timeout_s() == 900

    def test_non_integer_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_PYTEST_TIMEOUT_S", "not-a-number")
        assert config.review_pytest_timeout_s() == 1800

    def test_zero_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_PYTEST_TIMEOUT_S", "0")
        assert config.review_pytest_timeout_s() == 1800

    def test_negative_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_PYTEST_TIMEOUT_S", "-30")
        assert config.review_pytest_timeout_s() == 1800


class TestReviewTimeoutMaxRetries:
    """Task #1254 follow-up: UA_REVIEW_TIMEOUT_MAX_RETRIES bounds how many
    times the review pipeline retries a pytest timeout at one sha."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("UA_REVIEW_TIMEOUT_MAX_RETRIES", raising=False)
        assert config.review_timeout_max_retries() == 2

    def test_default_when_empty(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_TIMEOUT_MAX_RETRIES", "")
        assert config.review_timeout_max_retries() == 2

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_TIMEOUT_MAX_RETRIES", "5")
        assert config.review_timeout_max_retries() == 5

    def test_non_integer_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_TIMEOUT_MAX_RETRIES", "not-a-number")
        assert config.review_timeout_max_retries() == 2

    def test_zero_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_TIMEOUT_MAX_RETRIES", "0")
        assert config.review_timeout_max_retries() == 2

    def test_negative_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("UA_REVIEW_TIMEOUT_MAX_RETRIES", "-1")
        assert config.review_timeout_max_retries() == 2


class TestAgentScopes:
    def test_parses_scopes_and_revocations(self):
        assert config.agent_scopes("AgentA:merge|delete,AgentB:deploy") == {
            "AgentA": frozenset({"merge", "delete"}),
            "AgentB": frozenset({"deploy"}),
        }
        assert config.revoked_agents("AgentB:AgentC") == frozenset({"AgentB", "AgentC"})

    def test_conflicting_scope_registration_fails_closed(self):
        assert config.agent_scopes("AgentA:merge,AgentA:delete")["AgentA"] == frozenset()

    def test_malformed_scope_config_fails_closed(self):
        # A non-empty value that parses to nothing must raise, not silently
        # disable scope enforcement (empty mapping = nobody restricted).
        with pytest.raises(ValueError, match="UA_AGENT_SCOPES"):
            config.agent_scopes("Desktop_Agent2:")

    def test_unset_scopes_remain_unrestricted(self):
        assert config.agent_scopes("") == {}

    def test_pinned_oauth_clients_map_to_known_agents_without_exposing_secrets(self):
        assert config.mcp_oauth_clients(
            "server_agent5:bot-client:bot-secret,Desktop_Agent1:claude-client:claude-secret"
        ) == {
            "bot-client": ("server_agent5", "bot-secret"),
            "claude-client": ("Desktop_Agent1", "claude-secret"),
        }

    def test_pinned_oauth_client_collisions_fail_closed(self):
        assert config.mcp_oauth_clients(
            "AgentA:shared:secret-a,AgentB:shared:secret-b,AgentA:other:secret-c,AgentC:unique:secret-d"
        ) == {"unique": ("AgentC", "secret-d")}

    def test_process_workspace_root_ignores_deny_binding(self, monkeypatch):
        monkeypatch.setenv("UA_WORKSPACE", "/tmp/hub-workspace")
        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            from mcp.server.mcpserver.exceptions import ToolError

            with pytest.raises(ToolError, match="denied"):
                config.workspace_root()
            assert config.process_workspace_root() == Path("/tmp/hub-workspace")
        finally:
            config.workspace_root_var.reset(token)

    def test_process_workspace_root_preserves_normal_binding(self, tmp_path):
        token = config.workspace_root_var.set(tmp_path)
        try:
            assert config.process_workspace_root() == tmp_path
        finally:
            config.workspace_root_var.reset(token)

    def test_ensure_dirs_does_not_raise_for_deny_binding(self, monkeypatch, tmp_path):
        # Task #1317: ensure_dirs() creates hub-owned directories, so a DENY
        # caller (e.g. system_doctor served to Desktop_Agent1) must not make
        # storage.init_db() fail with the fs-access ToolError.
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "hub-workspace"))
        monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            config.ensure_dirs()
        finally:
            config.workspace_root_var.reset(token)
        assert (tmp_path / "hub-workspace").is_dir()
        assert (tmp_path / "ua-home").is_dir()


def test_fresh_data_home_is_private_under_group_umask(isolated_env, tmp_path, monkeypatch):
    """Fresh UA_HOME is 0700 under umask 0002; an existing directory is not chmod'd."""
    previous = os.umask(0o002)
    try:
        root_mode = tmp_path.stat().st_mode & 0o777
        home = tmp_path / "new-parent" / "fresh-home"
        monkeypatch.setenv("UA_HOME", str(home))
        monkeypatch.delenv("UA_WORKSPACE", raising=False)
        assert config.data_home() == home
        assert not home.exists()
        config.ensure_dirs()
        assert home.stat().st_mode & 0o777 == 0o700
        assert home.parent.stat().st_mode & 0o777 == 0o700
        assert (home / "workspace").is_dir()
        assert tmp_path.stat().st_mode & 0o777 == root_mode
        conn = storage.connect()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()

        init_home = tmp_path / "init-home"
        monkeypatch.setenv("UA_HOME", str(init_home))
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "init-workspace"))
        assert bootstrap.init_main(["--vault", ""]) == 0
        assert init_home.stat().st_mode & 0o777 == 0o700

        chosen = tmp_path / "chosen"
        chosen.mkdir()
        os.chmod(chosen, 0o775)
        monkeypatch.setenv("UA_HOME", str(chosen))
        monkeypatch.setenv("UA_WORKSPACE", str(isolated_env))
        config.ensure_dirs()
        assert chosen.stat().st_mode & 0o777 == 0o775
        with pytest.raises(storage.LiveStoreError, match="group- or world-writable"):
            storage.connect()
        assert chosen.stat().st_mode & 0o777 == 0o775
    finally:
        os.umask(previous)


class TestProjectRepoPaths:
    """Task #371: the fixed, existence-checked set of known project repos."""

    def test_unknown_project_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_DEV_ROOT", str(tmp_path))
        assert config.project_repo_path("not-a-real-project") is None
        assert config.project_repo_path(None) is None

    def test_known_but_not_checked_out_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_DEV_ROOT", str(tmp_path))
        assert config.project_repo_path("browser") is None

    def test_known_and_checked_out_resolves(self, monkeypatch, tmp_path):
        (tmp_path / "browser").mkdir()
        monkeypatch.setenv("UA_DEV_ROOT", str(tmp_path))
        assert config.project_repo_path("browser") == (tmp_path / "browser").resolve()

    def test_dirname_override_applies(self, monkeypatch, tmp_path):
        (tmp_path / "unified_agent").mkdir()
        monkeypatch.setenv("UA_DEV_ROOT", str(tmp_path))
        assert config.project_repo_path("unified-agent") == (tmp_path / "unified_agent").resolve()

    def test_project_repo_roots_only_lists_checked_out_projects(self, monkeypatch, tmp_path):
        (tmp_path / "browser").mkdir()
        (tmp_path / "tap").mkdir()
        monkeypatch.setenv("UA_DEV_ROOT", str(tmp_path))
        roots = config.project_repo_roots()
        assert (tmp_path / "browser").resolve() in roots
        assert (tmp_path / "tap").resolve() in roots
        assert len(roots) == 2


class TestAgentWorkspaces:
    def test_empty_by_default(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        assert config.agent_workspaces() == {}

    def test_parses_inline_pairs(self, monkeypatch):
        monkeypatch.setenv(
            "UA_AGENT_WORKSPACES",
            "AgentA:/home/user/dev/myagent,AgentB:/home/user/dev/myagent-brain",
        )
        result = config.agent_workspaces()
        assert result == {
            "AgentA": Path("/home/user/dev/myagent"),
            "AgentB": Path("/home/user/dev/myagent-brain"),
        }

    def test_expands_user_paths(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "AgentA:~/dev/sample-myagent")
        result = config.agent_workspaces()
        assert result["AgentA"] == Path("~/dev/sample-myagent").expanduser()

    def test_drops_malformed_entries(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "no-colon-here,:missing-label,AgentA:")
        assert config.agent_workspaces() == {}

    def test_reads_from_file(self, monkeypatch, tmp_path):
        mapping_file = tmp_path / "workspaces.txt"
        mapping_file.write_text("AgentA:/home/user/dev/myagent\n")
        monkeypatch.setenv("UA_AGENT_WORKSPACES", str(mapping_file))
        assert config.agent_workspaces() == {"AgentA": Path("/home/user/dev/myagent")}

    def test_duplicate_label_same_value_is_not_a_conflict(self, monkeypatch):
        monkeypatch.setenv(
            "UA_AGENT_WORKSPACES", "AgentA:/home/user/dev/myagent,AgentA:/home/user/dev/myagent"
        )
        assert config.agent_workspaces() == {"AgentA": Path("/home/user/dev/myagent")}

    def test_duplicate_label_conflicting_values_fails_closed(self, monkeypatch, capsys):
        """A stale/duplicate registration (task #299) must deny, not last-wins."""
        monkeypatch.setenv(
            "UA_AGENT_WORKSPACES", "Laptop_Agent1:DENY,Laptop_Agent1:/home/user/dev/myagent"
        )
        result = config.agent_workspaces()
        assert result["Laptop_Agent1"] is config.WORKSPACE_DENIED
        assert "conflicting duplicate" in capsys.readouterr().err

    def test_duplicate_label_conflicting_paths_fails_closed(self, monkeypatch):
        monkeypatch.setenv(
            "UA_AGENT_WORKSPACES",
            "AgentA:/home/user/dev/myagent,AgentA:/home/user/dev/myagent-typo",
        )
        assert config.agent_workspaces()["AgentA"] is config.WORKSPACE_DENIED


class TestExplicitWorkspaceDeny:
    """Task #610: a spawn-cwd fallback may follow an intentional DENY only."""

    def test_solitary_deny_is_explicit(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "Desktop_Agent1:DENY")
        assert config.explicit_workspace_deny("Desktop_Agent1") is True

    def test_repeated_identical_deny_is_still_explicit(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "Desktop_Agent1:DENY,Desktop_Agent1:DENY")
        assert config.explicit_workspace_deny("Desktop_Agent1") is True

    def test_case_varied_duplicates_track_agent_workspaces(self, monkeypatch):
        """agent_workspaces() compares raw values, so DENY/deny is a conflict there.

        The two functions must agree: whatever agent_workspaces() treats as a
        conflicting duplicate is not an explicit deny here either.
        """
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "Desktop_Agent1:DENY,Desktop_Agent1:deny")
        assert config.agent_workspaces()["Desktop_Agent1"] is config.WORKSPACE_DENIED
        assert config.explicit_workspace_deny("Desktop_Agent1") is False

    def test_conflicting_entries_are_not_an_explicit_deny(self, monkeypatch):
        """The fail-closed sentinel from a conflict must not read as intent."""
        monkeypatch.setenv(
            "UA_AGENT_WORKSPACES", "Desktop_Agent1:DENY,Desktop_Agent1:/tmp/intended"
        )
        assert config.agent_workspaces()["Desktop_Agent1"] is config.WORKSPACE_DENIED
        assert config.explicit_workspace_deny("Desktop_Agent1") is False

    def test_path_binding_is_not_a_deny(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "Desktop_Agent1:/tmp/ws")
        assert config.explicit_workspace_deny("Desktop_Agent1") is False

    def test_no_entry_is_not_a_deny(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        assert config.explicit_workspace_deny("Desktop_Agent1") is False


class TestAgentTokens:
    def test_empty_by_default(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
        assert config.agent_tokens() == {}

    def test_parses_inline_pairs(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:tok-a,AgentB:tok-b")
        assert config.agent_tokens() == {"tok-a": "AgentA", "tok-b": "AgentB"}

    def test_token_collision_authenticates_no_identity(self, monkeypatch, capsys):
        """Two labels sharing one token (task #299) must fail closed, not last-wins."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "Laptop_Agent1:shared-tok,Laptop_Agent2:shared-tok")
        result = config.agent_tokens()
        assert "shared-tok" not in result
        assert "collision" in capsys.readouterr().err

    def test_token_collision_does_not_affect_other_tokens(self, monkeypatch):
        monkeypatch.setenv(
            "UA_AGENT_TOKENS", "AgentA:tok-a,Laptop_Agent1:shared-tok,Laptop_Agent2:shared-tok"
        )
        assert config.agent_tokens() == {"tok-a": "AgentA"}


class TestResolveWorkspaceBinding:
    def test_device_scoped_identity_predicate_covers_laptop_and_server(self):
        assert config._is_device_scoped_identity("Laptop_Agent2")
        assert config._is_device_scoped_identity("server_agent2")
        assert not config._is_device_scoped_identity("Desktop_Agent2")

    def test_device_scoped_identity_unbound_fails_closed(self, monkeypatch, capsys):
        """Laptop_* with no UA_AGENT_WORKSPACES entry must deny, never fall through
        to the shared desktop UA_WORKSPACE root (task #299)."""
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        assert config.resolve_workspace_binding("Laptop_Agent1") is config.WORKSPACE_DENIED
        assert "failing closed" in capsys.readouterr().err

    def test_device_scoped_identity_bound_gets_its_path(self, monkeypatch, tmp_path):
        laptop_repo = tmp_path / "laptop-local-checkout"
        laptop_repo.mkdir()
        monkeypatch.setenv("UA_AGENT_WORKSPACES", f"Laptop_Agent1:{laptop_repo}")
        assert config.resolve_workspace_binding("Laptop_Agent1") == laptop_repo

    def test_server_identity_unbound_fails_closed(self, monkeypatch, capsys):
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        assert config.resolve_workspace_binding("server_agent2") is config.WORKSPACE_DENIED
        assert "server_agent2" in capsys.readouterr().err

    def test_device_scoped_identity_explicit_deny_still_denies(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "Laptop_Agent1:DENY")
        assert config.resolve_workspace_binding("Laptop_Agent1") is config.WORKSPACE_DENIED

    def test_non_device_scoped_identity_unbound_falls_through(self, monkeypatch):
        """Desktop_*/other identities keep today's shared-workspace fallback."""
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        assert config.resolve_workspace_binding("Desktop_Agent1") is None

    def test_all_four_laptop_identities_unbound_fail_closed(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        for label in (
            "Laptop_Agent1",
            "Laptop_Agent2",
            "Laptop_agent3",
            "Laptop_Agent5",
            "Laptop_Agent4",
            "Laptop_Agent6",
        ):
            assert config.resolve_workspace_binding(label) is config.WORKSPACE_DENIED


class TestAgentLabels:
    def test_unset_roster_is_empty_and_has_no_fleet_names(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        labels = config.agent_labels()
        assert labels == set()
        assert config.DEFAULT_AGENT_LABELS == ""
        compiled = config.DEFAULT_AGENT_LABELS.lower()
        for token in ("desktop_", "laptop_", "server_", "operator"):
            assert token not in compiled

    def test_open_roster_accepts_well_formed_labels(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        assert config.is_known_agent_label("Local_Worker")
        assert config.is_known_agent_label("pi")
        assert config.is_known_agent_label("server_agent3")
        assert config.is_known_agent_label("Desktop_Agent2")
        assert config.is_known_agent_label("desktop_agent2")
        assert config.canonical_agent_label("desktop_agent2") == "Desktop_Agent2"
        assert not config.is_known_agent_label("server_ghost")
        assert not config.is_known_agent_label("Desktop_Claud")

    def test_open_roster_rejects_empty_and_garbage(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        for bad in ("", "   ", "bad:name", "???", "1leading", "has space", "a" * 65):
            assert not config.is_known_agent_label(bad)

    def test_closed_roster_rejects_unknown(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Local_A:Local_B")
        assert config.is_known_agent_label("Local_A")
        assert not config.is_known_agent_label("Local_C")
        assert not config.is_known_agent_label("")

    def test_comma_inside_token_warns_and_keeps_bogus_label(self, monkeypatch, capsys):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_agent12:Desktop_Agent8,server_agent8")
        labels = config.agent_labels()
        assert "Desktop_agent12" in labels
        assert "Desktop_Agent8,server_agent8" in labels
        assert "server_agent8" not in labels
        err = capsys.readouterr().err
        assert "ua-audit: [Warning]" in err
        assert "Desktop_Agent8,server_agent8" in err
        assert "colon-separated" in err

    def test_malformed_token_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_ok:bad!!label:ok2")
        labels = config.agent_labels()
        assert "Desktop_ok" in labels
        assert "bad!!label" in labels
        err = capsys.readouterr().err
        assert "ua-audit: [Warning]" in err
        assert "well-formed" in err

    def test_executor_agent_has_no_compiled_fleet_default(self, monkeypatch):
        monkeypatch.delenv("UA_EXECUTOR_AGENT", raising=False)
        assert config.DEFAULT_EXECUTOR_AGENT == ""
        assert config.executor_agent() == ""
        assert "Desktop_agent3" not in config.DEFAULT_EXECUTOR_AGENT
        monkeypatch.setenv("UA_EXECUTOR_AGENT", "Local_Worker")
        assert config.executor_agent() == "Local_Worker"

    def test_closed_roster_lanes_are_configured_labels(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Local_A")
        assert config.is_valid_task_lane("default")
        assert config.is_valid_task_lane("Local_A")
        assert not config.is_valid_task_lane("Local_B")

    def test_open_roster_lanes_accept_well_formed_agent_names(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        assert config.is_valid_task_lane("Desktop_Agent5")
        assert config.is_valid_task_lane("Local_Worker")
        assert not config.is_valid_task_lane("???")
        assert config.is_valid_task_lane("desktop_agent2")
        assert not config.is_valid_task_lane("Desktop_Claud")

    def test_grok_skips_task_worktree_and_is_a_valid_lane(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        assert config.agent_skips_task_worktree("Desktop_Agent8")
        assert config.agent_skips_task_worktree("Laptop_Agent8")
        assert not config.agent_skips_task_worktree("Desktop_Agent1")
        assert config.is_known_agent_label("Desktop_Agent8")
        assert config.is_valid_task_lane("Desktop_Agent8")

    def test_omp_is_a_known_fleet_label(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        assert config.is_known_agent_label("Desktop_agent10")
        assert config.is_valid_task_lane("Desktop_agent10")
        assert not config.agent_skips_task_worktree("Desktop_agent10")

    def test_mcp_oauth_requires_flag_issuer_and_passphrase(self, monkeypatch):
        monkeypatch.delenv("UA_MCP_OAUTH", raising=False)
        monkeypatch.delenv("UA_MCP_OAUTH_ISSUER", raising=False)
        monkeypatch.delenv("UA_MCP_OAUTH_PASSPHRASE", raising=False)
        monkeypatch.delenv("UA_MCP_OAUTH_AGENT", raising=False)
        assert config.mcp_oauth_enabled() is False
        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://ua.example.test")
        assert config.mcp_oauth_enabled() is False
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "x")
        assert config.mcp_oauth_enabled() is True
        assert config.mcp_oauth_agent() == "Remote_Agent"
        monkeypatch.setenv("UA_MCP_OAUTH_AGENT", "Desktop_Agent1")
        assert config.mcp_oauth_agent() == "Desktop_Agent1"

    def test_mcp_oauth_issuer_rejects_malformed_port(self):
        # A non-numeric port slips past urlparse() itself (it only parses the
        # port lazily, on .port access) and would otherwise be accepted as a
        # valid issuer (task #1514).
        assert config._mcp_oauth_issuer_ok("https://hub.example.ts.net:bad") is False
        assert config._mcp_oauth_issuer_ok("http://localhost:bad") is False

    def test_mcp_oauth_issuer_rejects_out_of_range_port(self):
        assert config._mcp_oauth_issuer_ok("https://hub.example.ts.net:99999") is False
        assert config._mcp_oauth_issuer_ok("https://hub.example.ts.net:-1") is False

    def test_mcp_oauth_issuer_rejects_port_zero(self):
        assert config._mcp_oauth_issuer_ok("https://hub.example.ts.net:0") is False

    def test_mcp_oauth_issuer_accepts_valid_https_and_loopback_http(self):
        # Preserve existing valid behavior: HTTPS with any host, and loopback
        # HTTP, both with and without an explicit well-formed port.
        assert config._mcp_oauth_issuer_ok("https://hub.example.ts.net") is True
        assert config._mcp_oauth_issuer_ok("https://hub.example.ts.net:8443") is True
        assert config._mcp_oauth_issuer_ok("http://localhost") is True
        assert config._mcp_oauth_issuer_ok("http://localhost:8000") is True
        assert config._mcp_oauth_issuer_ok("http://127.0.0.1:8000") is True

    def test_mcp_oauth_issuer_still_rejects_non_loopback_http(self):
        assert config._mcp_oauth_issuer_ok("http://hub.example.ts.net:8443") is False

    def test_mcp_oauth_client_credentials_parse_and_fail_closed(self, monkeypatch):
        monkeypatch.delenv("UA_MCP_OAUTH_CLIENT_CREDENTIALS", raising=False)
        assert config.mcp_oauth_client_credentials() == []
        monkeypatch.setenv(
            "UA_MCP_OAUTH_CLIENT_CREDENTIALS",
            "c1|sec:ret|Desktop_Agent1,bad,c2|x|Desktop_NonexistentFleetAgent,c1|other|Desktop_agent3",
        )
        # Duplicate conflicting client_id is dropped; unknown fleet agent skipped.
        assert config.mcp_oauth_client_credentials() == []
        monkeypatch.setenv(
            "UA_MCP_OAUTH_CLIENT_CREDENTIALS",
            "bot|s3cret|server_agent8\nother|abc|Desktop_Agent5",
        )
        rows = config.mcp_oauth_client_credentials()
        assert ("bot", "s3cret", "server_agent8") in rows
        assert ("other", "abc", "Desktop_Agent5") in rows

    def test_executor_main_refuses_unset_identity(self, monkeypatch):
        monkeypatch.delenv("UA_EXECUTOR_AGENT", raising=False)
        from unified_agent.executor import daemon as executor_daemon

        with pytest.raises(SystemExit) as exc:
            executor_daemon.main()
        assert exc.value.code == 2

    def test_agent_model_family_keeps_custom_multi_segment_and_pi_suffixes(self, monkeypatch):
        monkeypatch.setitem(config.SEGMENT_TO_FAMILY, "Agent2", "Codex")
        assert config._agent_model_family("Desktop_Agent2") == "codex"
        assert config._agent_model_family("Team_West_Codex") == "codex"
        assert config._agent_model_family("Team_pi") == "pi"
        assert config._agent_model_family("Custom_Pi") == "pi"
