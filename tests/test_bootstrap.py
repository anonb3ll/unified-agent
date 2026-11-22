"""Cold-start bootstrap: `unified-agent init` and `unified-agent doctor`."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from unified_agent import bootstrap, config


class TestInit:
    def test_generated_env_is_sourceable_when_home_contains_spaces(
        self, isolated_env, monkeypatch, tmp_path
    ):
        home = tmp_path / "home with spaces"
        monkeypatch.setenv("UA_HOME", str(home))
        bootstrap.init_main([])
        env_path = home / "service.env"
        result = subprocess.run(
            ["bash", "-c", 'set -a; . "$1"; printf %s "$UA_WORKSPACE"', "bash", str(env_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == str(home / "workspace")

    def test_generated_env_parses_back_when_home_contains_apostrophe(
        self, isolated_env, monkeypatch, tmp_path
    ):
        """init shlex.quote()s the workspace path; doctor's EnvironmentFile
        parse must round-trip quoting that a naive quote-strip mangles
        (a path like /home/x/it'"'"'s ws becomes '...'\"'\"'s ws')."""
        from unified_agent.tools import doctor as doctor_mod

        home = tmp_path / "it's ws"
        monkeypatch.setenv("UA_HOME", str(home))
        bootstrap.init_main([])
        env_path = home / "service.env"
        parsed = doctor_mod._load_env_file(env_path)
        assert parsed["UA_WORKSPACE"] == str(home / "workspace")
        # ...and the value still matches what sourcing the file yields.
        result = subprocess.run(
            ["bash", "-c", 'set -a; . "$1"; printf %s "$UA_WORKSPACE"', "bash", str(env_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == str(home / "workspace")

    def test_creates_home_db_and_env(self, isolated_env, capsys):
        # isolated_env pre-creates UA_HOME + DB; wipe them for a true cold start.
        home = config.data_home()
        import shutil

        shutil.rmtree(home, ignore_errors=True)

        rc = bootstrap.init_main(["--json", "--print-token"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)

        assert home.is_dir()
        assert config.db_path().exists()
        assert out["db_created"] is True
        env_path = home / "service.env"
        assert env_path.exists()
        assert oct(env_path.stat().st_mode)[-3:] == "600"

        body = env_path.read_text()
        assert f"UA_AGENT_TOKENS={out['token_label']}:{out['token']}" in body
        assert f"UA_WORKSPACE={home / 'workspace'}" in body
        assert "UA_MCP_OAUTH=0" in body
        assert len(out["token"]) >= 32
        # Unset, not 0: an explicit off token would beat HTTP's fail-closed default.
        from unified_agent.service_env import parse_env_file

        parsed = parse_env_file(env_path)
        assert "UA_ENFORCE_AUTH" not in parsed
        assert config.enforce_auth_from_settings(
            parsed.get("UA_ENFORCE_AUTH"), "http", parsed.get("UA_LEGACY_OPEN_POSTURE")
        )

    def test_fresh_init_rejects_anonymous_http_tool_calls(self, isolated_env, monkeypatch):
        """HTTP started from a fresh init file rejects tools/list and memory_save.

        init leaves UA_ENFORCE_AUTH unset and UA_TRANSPORT=stdio. Serving that
        file over HTTP must enforce auth. An explicit 0 in the file would let
        an anonymous client list every tool and call memory_save.
        """
        import shutil

        from starlette.testclient import TestClient

        from unified_agent import server
        from unified_agent.service_env import parse_env_file

        shutil.rmtree(config.data_home(), ignore_errors=True)
        assert bootstrap.init_main([]) == 0
        parsed = parse_env_file(config.data_home() / "service.env")
        monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
        monkeypatch.delenv("UA_LEGACY_OPEN_POSTURE", raising=False)
        for key, value in parsed.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("UA_TRANSPORT", "http")

        assert "UA_ENFORCE_AUTH" not in parsed
        assert config.enforce_auth() is True
        assert config.agent_tokens()

        client = TestClient(server.mcp.streamable_http_app())
        list_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        save_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "memory_save",
                "arguments": {"content": "should not land", "title": "anon"},
            },
        }
        tools_list = client.post("/mcp", json=list_body)
        memory_save = client.post("/mcp", json=save_body)
        assert tools_list.status_code == 401
        assert memory_save.status_code == 401
        assert "Unauthorized" in tools_list.json()["error"]

    def test_rerun_keeps_existing_env_and_token(self, isolated_env):
        bootstrap.init_main([])
        env_path = config.data_home() / "service.env"
        original = env_path.read_text()

        rc = bootstrap.init_main([])  # no --force
        assert rc == 0
        assert env_path.read_text() == original  # token not rotated

    def test_force_rewrites_env(self, isolated_env):
        bootstrap.init_main([])
        env_path = config.data_home() / "service.env"
        original = env_path.read_text()

        bootstrap.init_main(["--force"])
        assert env_path.read_text() != original  # fresh token

    def test_force_keeps_existing_deployment_settings(self, isolated_env):
        """#1774: --force rotates bearers but never resets transport/auth posture."""
        from unified_agent.service_env import merge_write_env_file, parse_env_file

        bootstrap.init_main([])
        env_path = config.data_home() / "service.env"
        before = parse_env_file(env_path)
        pinned = {
            "UA_TRANSPORT": "http",
            "UA_ENFORCE_AUTH": "1",
            "UA_APPROVAL_TOOLS": "fs_write,run_cmd",
            "UA_MCP_OAUTH": "1",
            "UA_HTTP_PORT": "9999",
        }
        merge_write_env_file(env_path, pinned)

        assert bootstrap.init_main(["--force"]) == 0
        after = parse_env_file(env_path)
        for key, value in pinned.items():
            assert after[key] == value, key
        assert after["UA_AGENT_TOKENS"] != before["UA_AGENT_TOKENS"]  # bearers rotated

    def test_force_rotates_only_this_hosts_inline_bearers(self, isolated_env, capsys):
        """#1797: inline UA_AGENT_TOKENS keeps every other agent's bearer, as the
        token-file case does; only this host's two labels get new tokens."""
        from unified_agent.service_env import merge_write_env_file, parse_env_file

        bootstrap.init_main([])
        env_path = config.data_home() / "service.env"
        mine = dict(
            pair.split(":", 1) for pair in parse_env_file(env_path)["UA_AGENT_TOKENS"].split(",")
        )
        others = {"other_worker": "tok-other", "peer_worker": "tok-peer"}
        inline = ",".join(
            [*(f"{k}:{v}" for k, v in mine.items()), *(f"{k}:{v}" for k, v in others.items())]
        )
        merge_write_env_file(env_path, {"UA_AGENT_TOKENS": inline})
        capsys.readouterr()

        assert bootstrap.init_main(["--json", "--force", "--print-token"]) == 0
        out = json.loads(capsys.readouterr().out)
        after = dict(
            pair.split(":", 1) for pair in parse_env_file(env_path)["UA_AGENT_TOKENS"].split(",")
        )
        for label, tok in others.items():
            assert after[label] == tok, label
        assert set(after) == set(mine) | set(others)
        assert after[out["token_label"]] == out["token"]
        for label, old in mine.items():
            assert after[label] != old, label

    def test_rotate_inline_tokens_keeps_order_and_drops_stale_repeats(self):
        rotated = bootstrap._rotate_inline_tokens(
            "a:1, B:2 ,c:3,b:stale,junk", {"b": "new", "d": "4"}
        )
        assert rotated == "a:1,b:new,c:3,junk,d:4"

    def test_force_keeps_shell_defined_deployment_settings(self, isolated_env):
        """A preserved pin written as a shell expression keeps its meaning.

        Re-rendering the parsed value would quote ``$MODE`` into a literal the
        transport reader rejects (Devin review, PR #978).
        """
        bootstrap.init_main([])
        env_path = config.data_home() / "service.env"
        with env_path.open("a", encoding="utf-8") as fh:
            fh.write(
                "UA_TRANSPORT=$MODE\n"
                'UA_ENFORCE_AUTH="${AUTH_PIN:-1}"\n'
                "export UA_APPROVAL_TOOLS=$GATED\n"
                "UA_MCP_OAUTH=1\n"
            )

        def sourced() -> list[str]:
            script = (
                'set -a; . "$1"; printf "%s\\n" "$UA_TRANSPORT" "$UA_ENFORCE_AUTH" '
                '"$UA_APPROVAL_TOOLS" "$UA_MCP_OAUTH"'
            )
            result = subprocess.run(
                ["bash", "-c", script, "bash", str(env_path)],
                capture_output=True,
                text=True,
                check=True,
                env={"PATH": os.environ.get("PATH", ""), "MODE": "http", "GATED": "run_cmd"},
            )
            return result.stdout.splitlines()

        expected = ["http", "1", "run_cmd", "1"]
        assert sourced() == expected
        assert bootstrap.init_main(["--force"]) == 0
        assert sourced() == expected

    def test_force_fills_deployment_settings_the_file_lacks(self, isolated_env):
        from unified_agent.service_env import parse_env_file, write_env_file

        bootstrap.init_main([])
        env_path = config.data_home() / "service.env"
        write_env_file(env_path, {"UA_TRANSPORT": "http"})
        assert bootstrap.init_main(["--force"]) == 0
        after = parse_env_file(env_path)
        assert after["UA_TRANSPORT"] == "http"
        # Absent auth stays unset so this HTTP hub enforces, instead of
        # inheriting a template UA_ENFORCE_AUTH=0 that would serve anonymously.
        assert "UA_ENFORCE_AUTH" not in after
        assert config.enforce_auth_from_settings(
            after.get("UA_ENFORCE_AUTH"), after["UA_TRANSPORT"], None
        )
        assert after["UA_MCP_OAUTH"] == "0"

    def test_summary_names_the_token_file_when_bearers_live_there(
        self, isolated_env, tmp_path, capsys
    ):
        from unified_agent.service_env import write_env_file

        bootstrap.init_main([])
        tokens = tmp_path / "tokens"
        tokens.write_text("worker:synthetic-token\n")
        tokens.chmod(0o600)
        env_path = config.data_home() / "service.env"
        write_env_file(env_path, {"UA_AGENT_TOKENS": str(tokens)})
        capsys.readouterr()

        assert bootstrap.init_main(["--force"]) == 0
        out = capsys.readouterr().out
        assert "(in token file)" in out
        assert "(in env file)" not in out
        assert bootstrap.init_main(["--json", "--force"]) == 0
        assert json.loads(capsys.readouterr().out)["token_file"] == str(tokens.resolve())

    def test_summary_names_the_env_file_for_inline_tokens(self, isolated_env, capsys):
        import shutil

        shutil.rmtree(config.data_home(), ignore_errors=True)
        assert bootstrap.init_main([]) == 0
        out = capsys.readouterr().out
        assert "(in env file)" in out
        assert "(in token file)" not in out

    def test_rerun_does_not_recreate_db(self, isolated_env, capsys):
        bootstrap.init_main(["--json"])
        capsys.readouterr()
        bootstrap.init_main(["--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["db_created"] is False


class TestDefaultTokenLabel:
    """#1661: the minted label must be one the hub accepts as an agent identity."""

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("desktop", "local_desktop_agent"),
            ("Desktop", "local_Desktop_agent"),
            ("laptop", "local_laptop_agent"),
            ("server", "local_server_agent"),
            ("SERVER.example.test", "local_SERVER_agent"),
            ("build-box", "build-box_agent"),
            ("my-desktop", "my-desktop_agent"),
            ("workstation.lan", "workstation_agent"),
        ],
    )
    def test_label_is_accepted_by_the_hub(self, monkeypatch, host, expected):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        monkeypatch.setattr(bootstrap.socket, "gethostname", lambda: host)
        label = bootstrap._default_token_label()
        assert label == expected
        assert config.is_known_agent_label(label)

    @pytest.mark.parametrize("host", ["", ".", "123", "-_-", "host name", "a" * 100])
    def test_odd_hostnames_still_give_a_well_formed_accepted_label(self, monkeypatch, host):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        monkeypatch.setattr(bootstrap.socket, "gethostname", lambda: host)
        label = bootstrap._default_token_label()
        assert config.agent_label_is_well_formed(label)
        assert config.is_known_agent_label(label)

    def test_fleet_looking_label_is_the_defect_this_guards(self):
        assert not config.is_known_agent_label("desktop_agent")

    def test_init_writes_the_accepted_label(self, isolated_env, monkeypatch, capsys):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        monkeypatch.setattr(bootstrap.socket, "gethostname", lambda: "desktop")
        assert bootstrap.init_main(["--json", "--print-token"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["token_label"] == "local_desktop_agent"
        body = (config.data_home() / "service.env").read_text()
        assert f"UA_AGENT_TOKENS=local_desktop_agent:{out['token']}" in body
        assert config.is_known_agent_label(out["token_label"])


class TestDoctor:
    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    @pytest.mark.parametrize("selection", ["dispatch_scheduler", "dispatch_tick_timer", "both"])
    @pytest.mark.parametrize("json_output", [True, False])
    def test_unknown_scheduler_probe_fails_strict(
        self, monkeypatch, capsys, platform, selection, json_output
    ):
        from types import SimpleNamespace

        from unified_agent import monitor
        from unified_agent.tools import doctor

        monkeypatch.setattr(monitor.sys, "platform", platform)
        calls = []

        def failed_probe(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=1, stdout="", stderr="Permission denied")

        monkeypatch.setattr(monitor.subprocess, "run", failed_probe)
        rows = [
            doctor._dispatch_scheduler_check(),
            doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer"),
        ]
        assert len(calls) == 2
        assert all(row["status"] == "UNKNOWN" for row in rows)
        if selection != "both":
            rows = [row for row in rows if row["capability"] == selection]
        report = {
            "checks": rows,
            "summary": {"live": 0, "dormant": 0, "misconfigured": 0, "unknown": len(rows)},
        }
        monkeypatch.setattr(bootstrap, "system_doctor", lambda: report)
        args = ["--json"] if json_output else []
        assert bootstrap.doctor_main(args) == 0
        normal = capsys.readouterr().out
        assert bootstrap.doctor_main([*args, "--strict"]) == 1
        strict = capsys.readouterr().out
        assert normal == strict
        if json_output:
            assert json.loads(strict) == report
        else:
            assert "UNKNOWN" in strict

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_proven_scheduler_retirement_still_fails_strict(self, monkeypatch, capsys, platform):
        from types import SimpleNamespace

        from unified_agent import monitor
        from unified_agent.tools import doctor

        monkeypatch.setattr(monitor.sys, "platform", platform)
        monkeypatch.setattr(monitor.os, "getuid", lambda: 501)

        def absent_probe(cmd, **kwargs):
            if platform == "linux":
                return SimpleNamespace(
                    returncode=0, stdout="LoadState=not-found\nActiveState=inactive\n", stderr=""
                )
            if cmd[1] == "print-disabled":
                return SimpleNamespace(
                    returncode=0, stdout='"com.unified-agent.dispatch-tick" => disabled', stderr=""
                )
            return SimpleNamespace(
                returncode=113,
                stdout="",
                stderr=(
                    'Could not find service "com.unified-agent.dispatch-tick" in domain for user gui: 501'
                ),
            )

        monkeypatch.setattr(monitor.subprocess, "run", absent_probe)
        rows = [
            doctor._dispatch_scheduler_check(),
            doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer"),
        ]
        assert all(row["status"] == "DORMANT" for row in rows)
        report = {
            "checks": rows,
            "summary": {"live": 0, "dormant": 2, "misconfigured": 0, "unknown": 0},
        }
        monkeypatch.setattr(bootstrap, "system_doctor", lambda: report)
        assert bootstrap.doctor_main(["--json"]) == 0
        capsys.readouterr()
        assert bootstrap.doctor_main(["--json", "--strict"]) == 1
        assert json.loads(capsys.readouterr().out) == report

    @pytest.mark.parametrize("args", [["--json", "--strict"], ["--strict"]])
    def test_healthy_surface_passes_strict(self, monkeypatch, capsys, args):
        report = {
            "checks": [{"capability": "healthy", "status": "LIVE", "detail": "ok", "gate": "X"}],
            "summary": {"live": 1, "dormant": 0, "misconfigured": 0, "unknown": 0},
        }
        monkeypatch.setattr(bootstrap, "system_doctor", lambda: report)
        assert bootstrap.doctor_main(args) == 0
        assert "LIVE" in capsys.readouterr().out

    def test_exit_zero_when_no_misconfigured(self, monkeypatch, capsys):
        monkeypatch.setattr(
            bootstrap,
            "system_doctor",
            lambda: {
                "checks": [
                    {"capability": "a", "status": "LIVE", "detail": "ok", "gate": "X"},
                    {"capability": "b", "status": "DORMANT", "detail": "off", "gate": "Y"},
                ],
                "summary": {"live": 1, "dormant": 1, "misconfigured": 0, "unknown": 0},
            },
            raising=False,
        )
        assert bootstrap.doctor_main([]) == 0
        assert "0 misconfigured" in capsys.readouterr().out

    def test_exit_one_on_misconfigured(self, monkeypatch):
        monkeypatch.setattr(
            bootstrap,
            "system_doctor",
            lambda: {
                "checks": [
                    {"capability": "b", "status": "MISCONFIGURED", "detail": "broken", "gate": "Y"},
                ],
                "summary": {"live": 0, "dormant": 0, "misconfigured": 1, "unknown": 0},
            },
            raising=False,
        )
        assert bootstrap.doctor_main([]) == 1

    def test_allow_unbuilt_ui_tolerates_only_the_unbuilt_web_ui(self, monkeypatch):
        unbuilt = {
            "capability": "web_ui",
            "status": "MISCONFIGURED",
            "detail": "packages/web/dist/index.html missing",
            "gate": "GET /ui on the HTTP hub",
            "reason": "ui_not_built",
        }
        override_missing = {k: v for k, v in unbuilt.items() if k != "reason"}
        other = {"capability": "b", "status": "MISCONFIGURED", "detail": "broken", "gate": "Y"}

        def run(checks, argv):
            summary = {"live": 0, "dormant": 0, "misconfigured": len(checks), "unknown": 0}
            report = {"checks": checks, "summary": summary}
            monkeypatch.setattr(bootstrap, "system_doctor", lambda: report, raising=False)
            return bootstrap.doctor_main(argv)

        assert run([unbuilt], []) == 1
        assert run([unbuilt], ["--allow-unbuilt-ui"]) == 0
        assert run([unbuilt, other], ["--allow-unbuilt-ui"]) == 1
        assert run([override_missing], ["--allow-unbuilt-ui"]) == 1

    def test_degraded_fails_normal_and_strict_doctor(self, monkeypatch):
        monkeypatch.setattr(
            bootstrap,
            "system_doctor",
            lambda: {
                "checks": [
                    {
                        "capability": "connector:int-gcp-agent20",
                        "status": "DEGRADED",
                        "detail": "permission denied",
                        "gate": "gcloud_inventory",
                    }
                ],
                "summary": {
                    "live": 0,
                    "dormant": 0,
                    "degraded": 1,
                    "misconfigured": 0,
                    "unknown": 0,
                },
            },
            raising=False,
        )
        assert bootstrap.doctor_main([]) == 1
        assert bootstrap.doctor_main(["--strict"]) == 1

    def test_strict_fails_on_dormant(self, monkeypatch):
        report = {
            "checks": [{"capability": "b", "status": "DORMANT", "detail": "off", "gate": "Y"}],
            "summary": {"live": 0, "dormant": 1, "misconfigured": 0, "unknown": 0},
        }
        monkeypatch.setattr(bootstrap, "system_doctor", lambda: report, raising=False)
        assert bootstrap.doctor_main([]) == 0
        assert bootstrap.doctor_main(["--strict"]) == 1

    def test_json_output(self, monkeypatch, capsys):
        report = {
            "checks": [{"capability": "b", "status": "LIVE", "detail": "ok", "gate": "Y"}],
            "summary": {"live": 1, "dormant": 0, "misconfigured": 0, "unknown": 0},
        }
        monkeypatch.setattr(bootstrap, "system_doctor", lambda: report, raising=False)
        bootstrap.doctor_main(["--json"])
        assert json.loads(capsys.readouterr().out) == report


def test_verify_reports_hub_identity(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["allow_fallback"] is False

        def call_tool(self, name, arguments):
            assert name == "profile_status"
            assert arguments == {}
            return {
                "identity": "server_agent5",
                "profile": "public",
                "auth_method": "client_credentials",
                "scopes": ["mcp"],
                "token_age_s": 4.2,
            }

    monkeypatch.setattr("unified_agent.executor.daemon.HubClient", FakeClient)
    assert bootstrap.verify_main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["identity"] == "server_agent5"


class TestColdStartIntegration:
    """A fresh UA_HOME with nothing else wired: init then a real system_doctor
    run must not report any MISCONFIGURED capability — every opt-in feature is
    DORMANT, which is correct, not broken."""

    @pytest.fixture(autouse=True)
    def fresh_process_health(self, isolated_env):
        from unified_agent import embeddings

        # UA_HOME isolation supplies a fresh database; cold-start checks also
        # need the health counters of a fresh process, not an earlier test.
        embeddings.reset_embedding_state()
        try:
            yield
        finally:
            embeddings.reset_embedding_state()

    def test_hermetic_against_prior_embedding_failures(self, tmp_path):
        """A prior test's process health must not become cold-install health."""
        prior_test = tmp_path / "test_prior_embedding_failure.py"
        prior_test.write_text(
            "from unified_agent import embeddings\n"
            "import pytest\n\n"
            "def test_prior_failure(monkeypatch):\n"
            "    def fail(*args, **kwargs):\n"
            "        raise RuntimeError('synthetic prior-test failure')\n"
            "    monkeypatch.setattr(embeddings, 'get_embedding_model', fail)\n"
            "    with pytest.raises(RuntimeError):\n"
            "        embeddings.generate_embedding('prior test')\n"
            "    assert embeddings.embedding_state()['degraded']\n"
        )
        target = (
            f"{Path(__file__).resolve()}::TestColdStartIntegration::test_init_then_doctor_is_clean"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(prior_test),
                target,
                "-q",
                "-p",
                "no:cacheprovider",
                f"--basetemp={tmp_path / 'child-temp'}",
            ],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_failure_during_fresh_install_is_still_misconfigured(self, monkeypatch):
        from unified_agent import embeddings
        from unified_agent.tools import doctor

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic current-process failure")

        monkeypatch.setattr(embeddings, "get_embedding_model", fail)
        with pytest.raises(RuntimeError):
            embeddings.generate_embedding("current process")
        assert embeddings.embedding_state()["failures"] == 1
        assert doctor._semantic_search_check()["status"] == "MISCONFIGURED"

    def test_init_then_doctor_is_clean(self, isolated_env, monkeypatch):
        from unified_agent.tools import doctor as doctor_mod

        # A bare install (or CI): every unit is definitively not installed, as
        # systemd/launchd report it (LoadState not-found), matching a real
        # foreign machine. A None probe means "cannot tell", not "absent".
        monkeypatch.setattr(
            doctor_mod, "_service_probe", lambda unit, timeout: {"LoadState": "not-found"}
        )
        # A foreign machine also has no installed units' env files: _hub_setting
        # must not fall through to the REAL host's service.env (a Mac dev hub
        # legitimately configures UA_MCP_OAUTH=1, which leaked LIVE here).
        monkeypatch.setattr(doctor_mod, "_service_env", lambda unit: {})
        # Vault sync state is host-local and intentionally outside this test's
        # cold-install doctor contract.
        monkeypatch.setattr(
            doctor_mod,
            "_vault_sync_check",
            lambda: doctor_mod._check("vault_sync", "DORMANT", "not installed", "test"),
        )

        # A fresh install has no operator pin; the suite-wide legacy pin from
        # conftest would name a login init never minted a bearer for.
        for name in ("UA_OPERATOR_LABEL", "UA_OPERATOR_NAME", "UA_OPERATOR_ALIASES"):
            monkeypatch.delenv(name, raising=False)
        bootstrap.init_main([])
        service_env = doctor_mod._load_env_file(config.data_home() / "service.env")
        for name in ("UA_AGENT_TOKENS", "UA_ENFORCE_AUTH", "UA_APPROVAL_TOOLS", "UA_MCP_OAUTH"):
            if name in service_env:
                monkeypatch.setenv(name, service_env[name])
            else:
                monkeypatch.delenv(name, raising=False)
        report = doctor_mod.system_doctor()
        checks = {check["capability"]: check for check in report["checks"]}
        assert checks["mcp_oauth"]["status"] == "DORMANT"
        assert checks["connector:int-gcp-agent20"]["status"] == "DORMANT"
        assert "health" not in checks["connector:int-gcp-agent20"]
        misconfigured = [c for c in report["checks"] if c["status"] == "MISCONFIGURED"]
        assert misconfigured == [], misconfigured
        assert bootstrap.doctor_main([]) == 0

    def test_hermetic_against_ambient_llm_provider_env(self):
        """Task #1294: on a machine with e.g. GEMINI_API_KEY exported in the
        shell (any real dev box, or a Mac hub that legitimately sets one),
        `test_init_then_doctor_is_clean` must still pass — a fresh install
        assumes no provider config, and inherited provider env must not flip
        llm_provider_chain to MISCONFIGURED.

        `isolated_env`'s monkeypatch.delenv only proves the fixture clears
        *pytest-internal* env; it says nothing about env genuinely inherited
        from the OS, which is what a real shell leak looks like. So this runs
        the target test in a subprocess whose environment is seeded with fake
        values for every name in llm_client.PROVIDER_CHAIN_ENV_VARS *before*
        pytest (and isolated_env) starts — the same shape of leak the
        fixture is meant to neutralize.
        """
        from unified_agent import llm_client

        env = dict(os.environ)
        for name in llm_client.PROVIDER_CHAIN_ENV_VARS:
            env[name] = "definitely-not-a-real-value"
        env["UA_SUBSCRIPTION_POOL_ENABLED"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"{Path(__file__).name}::TestColdStartIntegration::test_init_then_doctor_is_clean",
                "-q",
            ],
            cwd=Path(__file__).resolve().parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("sub", ["init", "doctor", "verify"])
def test_server_main_dispatches_subcommand(monkeypatch, sub):
    """Bootstrap subcommands route to bootstrap, not the server."""
    from unified_agent import server

    called = {}

    def fake(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setattr(bootstrap, f"{sub}_main", fake)
    monkeypatch.setattr(server.sys, "argv", ["unified-agent", sub, "--json"])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0
    assert called["argv"] == ["--json"]


@pytest.mark.parametrize("flag", ["--version", "-V", "--help", "-h"])
def test_version_and_help_skip_the_task_store_on_a_hub_client(monkeypatch, tmp_path, capsys, flag):
    """#2297: with NO_LOCAL_MEMORY_DB present, --version/--help exit 0 before init_db."""
    from unified_agent import server, storage

    (tmp_path / "NO_LOCAL_MEMORY_DB").write_text("DO NOT CREATE\n", encoding="utf-8")
    monkeypatch.setenv("UA_HOME", str(tmp_path))

    def boom():
        raise AssertionError("the task store must not be opened for " + flag)

    monkeypatch.setattr(storage, "init_db", boom)
    monkeypatch.setattr(storage, "connect", boom)
    monkeypatch.setattr(server.sys, "argv", ["unified-agent", flag])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert ("unified-agent" in out) and (("usage:" in out) == (flag in ("--help", "-h")))
