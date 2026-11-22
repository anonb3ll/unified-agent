from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.devservers import detect_dev_servers


def make_project(ws, name: str, files: dict[str, str]):
    root = ws / name
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def names(report) -> set[str]:
    return {c["name"] for c in report["configurations"]}


class TestNodeDetection:
    def test_dev_script_with_yarn_lock(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {
                "package.json": '{"name": "app", "scripts": {"dev": "vite"}}',
                "yarn.lock": "",
            },
        )
        report = detect_dev_servers("proj")
        assert names(report) == {"app (dev)"}
        cfg = report["configurations"][0]
        assert cfg["runtimeExecutable"] == "yarn"
        assert cfg["runtimeArgs"] == ["dev"]
        assert cfg["port"] is None
        assert cfg["note"]

    def test_explicit_port_extracted(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {"package.json": '{"name": "app", "scripts": {"dev": "next dev --port 3001"}}'},
        )
        report = detect_dev_servers("proj")
        cfg = report["configurations"][0]
        assert cfg["port"] == 3001
        assert cfg["note"] is None
        assert cfg["runtimeExecutable"] == "npm"  # no lockfile match -> default
        assert cfg["runtimeArgs"] == ["run", "dev"]

    def test_non_dev_scripts_ignored(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {
                "package.json": (
                    '{"name": "app", "scripts": '
                    '{"test": "jest", "build": "tsc", "lint": "eslint ."}}'
                )
            },
        )
        report = detect_dev_servers("proj")
        assert report["configurations"] == []

    def test_pnpm_lockfile_detected(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {
                "package.json": '{"name": "app", "scripts": {"serve": "http-server"}}',
                "pnpm-lock.yaml": "",
            },
        )
        report = detect_dev_servers("proj")
        assert report["configurations"][0]["runtimeExecutable"] == "pnpm"

    def test_invalid_json_warns_not_raises(self, isolated_env):
        make_project(isolated_env, "proj", {"package.json": "{not json"})
        report = detect_dev_servers("proj")
        assert report["configurations"] == []
        assert any("package.json" in w for w in report["warnings"])


class TestPythonDetection:
    def test_server_entry_point_detected(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {
                "pyproject.toml": '[project.scripts]\napp-server = "app.server:main"\n',
                "app/server.py": (
                    "import os, uvicorn\n"
                    "def main():\n"
                    '    uvicorn.run("app.server:api", port=int(os.getenv("APP_PORT", "8000")))\n'
                ),
            },
        )
        report = detect_dev_servers("proj")
        assert names(report) == {"app-server"}
        cfg = report["configurations"][0]
        assert cfg["runtimeExecutable"] == "uv"
        assert cfg["runtimeArgs"] == ["run", "app-server"]
        assert cfg["port"] == 8000
        assert cfg["source"] == "app/server.py"

    def test_cli_only_entry_point_skipped(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {
                "pyproject.toml": '[project.scripts]\napp-cli = "app.cli:main"\n',
                "app/cli.py": "def main():\n    print('hi')\n",
            },
        )
        report = detect_dev_servers("proj")
        assert report["configurations"] == []

    def test_src_layout_resolved(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {
                "pyproject.toml": '[project.scripts]\napp-server = "app.server:main"\n',
                "src/app/server.py": "def main():\n    HTTPServer(('', 8080), None)\n",
            },
        )
        report = detect_dev_servers("proj")
        assert names(report) == {"app-server"}
        assert report["configurations"][0]["source"] == "src/app/server.py"

    def test_no_port_evidence_notes_why(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {
                "pyproject.toml": '[project.scripts]\napp-server = "app.server:main"\n',
                "app/server.py": "def main():\n    app.run(debug=True)\n",
            },
        )
        report = detect_dev_servers("proj")
        cfg = report["configurations"][0]
        assert cfg["port"] is None
        assert "port" in cfg["note"]

    def test_invalid_toml_warns_not_raises(self, isolated_env):
        make_project(isolated_env, "proj", {"pyproject.toml": "not [ valid toml"})
        report = detect_dev_servers("proj")
        assert report["configurations"] == []
        assert any("pyproject.toml" in w for w in report["warnings"])


class TestComposeDetection:
    def test_service_with_published_port(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {
                "docker-compose.yml": (
                    'services:\n  web:\n    image: nginx\n    ports:\n      - "8080:80"\n'
                )
            },
        )
        report = detect_dev_servers("proj")
        cfg = report["configurations"][0]
        assert cfg["name"] == "web"
        assert cfg["runtimeExecutable"] == "docker"
        assert cfg["runtimeArgs"] == ["compose", "up", "web"]
        assert cfg["port"] == 8080

    def test_service_without_ports_notes_why(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {"docker-compose.yml": "services:\n  worker:\n    image: busybox\n"},
        )
        report = detect_dev_servers("proj")
        cfg = report["configurations"][0]
        assert cfg["port"] is None
        assert cfg["note"]

    def test_invalid_yaml_warns_not_raises(self, isolated_env):
        make_project(isolated_env, "proj", {"docker-compose.yml": "services: [unclosed"})
        report = detect_dev_servers("proj")
        assert report["configurations"] == []
        assert any("docker-compose.yml" in w for w in report["warnings"])


class TestProcfileDetection:
    def test_process_types_listed(self, isolated_env):
        make_project(
            isolated_env,
            "proj",
            {"Procfile": "web: gunicorn app:app\nworker: celery -A app worker\n"},
        )
        report = detect_dev_servers("proj")
        assert names(report) == {"web", "worker"}
        web = next(c for c in report["configurations"] if c["name"] == "web")
        assert web["runtimeExecutable"] == "gunicorn"
        assert web["runtimeArgs"] == ["app:app"]
        assert web["port"] is None


class TestGeneral:
    def test_no_manifests_returns_empty(self, isolated_env):
        make_project(isolated_env, "proj", {"README.md": "hello\n"})
        report = detect_dev_servers("proj")
        assert report["configurations"] == []
        assert report["warnings"] == []

    def test_path_outside_roots_rejected(self, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(ToolError, match="outside the auditable roots"):
            detect_dev_servers(str(outside))

    def test_file_path_rejected(self, isolated_env):
        (isolated_env / "app.py").write_text("x = 1\n")
        with pytest.raises(ToolError, match="not a directory"):
            detect_dev_servers("app.py")

    def test_conflict_flagged_for_listening_port(self, isolated_env):
        port = 43123
        with patch("unified_agent.tools.devservers._listening_ports", return_value={port}):
            make_project(
                isolated_env,
                "proj",
                {
                    "package.json": (
                        f'{{"name": "app", "scripts": {{"dev": "next dev --port {port}"}}}}'
                    )
                },
            )
            report = detect_dev_servers("proj")
            cfg = report["configurations"][0]
            assert cfg["port"] == port
            assert cfg["conflict"] is not None
            assert str(port) in cfg["conflict"]

    def test_no_conflict_for_free_port(self, isolated_env):
        free_port = 43124
        with patch("unified_agent.tools.devservers._listening_ports", return_value=set()):
            make_project(
                isolated_env,
                "proj",
                {
                    "package.json": (
                        f'{{"name": "app", "scripts": {{"dev": "next dev --port {free_port}"}}}}'
                    )
                },
            )
            report = detect_dev_servers("proj")
            assert report["configurations"][0]["conflict"] is None
