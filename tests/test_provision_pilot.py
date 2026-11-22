from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "provision-pilot"


def _invoke(
    tmp_path: Path,
    *,
    origin: str = "https://pilot.example.ts.net",
    workos_redirect_uri: str = "https://auth.example.ts.net/pilot/callback",
    output_root: Path | None = None,
    identity_map: object | None = None,
    relative_binaries: bool = False,
) -> subprocess.CompletedProcess[str]:
    mapping = tmp_path / "identity-map.json"
    mapping.write_text(
        json.dumps(identity_map if identity_map is not None else {"user_01ABC": "Desktop_Agent5"}),
        encoding="utf-8",
    )
    args = [
        sys.executable,
        str(SCRIPT),
        "pilot-acme",
        "--workos-organization-id",
        "org_pilot123",
        "--workos-client-id",
        "client_pilot123",
        "--workos-redirect-uri",
        workos_redirect_uri,
        "--identity-map-file",
        str(mapping),
        "--tailnet-origin",
        origin,
        "--backup-bucket",
        "gs://ua-pilot-backups",
        "--private-home",
        str(tmp_path / "private-hub"),
        "--unified-agent-binary",
        "bin/unified-agent" if relative_binaries else str(tmp_path / "bin" / "unified-agent"),
        "--litestream-binary",
        "bin/litestream" if relative_binaries else str(tmp_path / "bin" / "litestream"),
    ]
    if output_root is not None:
        args.extend(["--output-root", str(output_root)])
    return subprocess.run(args, text=True, capture_output=True, check=False)


def test_renders_isolated_hub_replica_workos_and_tailnet_units(tmp_path: Path) -> None:
    output_root = tmp_path / "pilots"
    result = _invoke(tmp_path, output_root=output_root)

    assert result.returncode == 0, result.stderr
    bundle = output_root / "pilot-acme"
    env = (bundle / "pilot.env").read_text(encoding="utf-8")
    assert f'UA_HOME="{bundle}/state"' in env
    assert f'UA_WORKSPACE="{bundle}/workspace"' in env
    assert 'UA_EDITION="enterprise"' in env
    assert 'WORKOS_ORGANIZATION_ID="org_pilot123"' in env
    assert 'UA_HUB_WORKOS_ORGANIZATION_ID="org_pilot123"' in env
    assert 'WORKOS_REDIRECT_URI="https://auth.example.ts.net/pilot/callback"' in env
    identity_map = json.dumps({"user_01ABC": "Desktop_Agent5"}, separators=(",", ":"))
    assert f"UA_HUB_WORKOS_IDENTITY_MAP={json.dumps(identity_map)}" in env
    assert 'UA_ENFORCE_AUTH="1"' in env
    assert 'UA_STATE_SYNC="0"' in env
    assert 'UA_EVENT_BUS="0"' in env
    assert 'UA_HTTP_ALLOWED_ORIGINS="https://pilot.example.ts.net"' in env
    assert 'UA_REPLICA_GCS_URI="gs://ua-pilot-backups/ua-pilots/pilot-acme/memory"' in env

    litestream = (bundle / "litestream.yml").read_text(encoding="utf-8")
    assert "path: ${UA_HOME}/memory.db" in litestream
    assert "url: ${UA_REPLICA_GCS_URI}" in litestream
    # Sept 2026 incident: a 15 s check re-listed an unprunable prefix.
    assert "l0-retention-check-interval: 5m" in litestream
    assert "l0-retention-check-interval: 15s" not in litestream
    readme = (bundle / "README.md").read_text(encoding="utf-8")
    for precondition in (
        "can DELETE under",
        "lifecycle rule scoped to that prefix",
        "pilot host's GCP region",
        "egress alert covers this bucket",
        "Count the prefix's objects 30",
        "runs no separate nightly backup job",
    ):
        assert precondition in readme
    hub_unit = bundle / "systemd" / "unified-agent-pilot-acme.service"
    unit_text = hub_unit.read_text(encoding="utf-8")
    assert f"EnvironmentFile={bundle}/pilot.env" in unit_text
    assert "EnvironmentFile=" + f'"{bundle}/secrets.env"' not in unit_text
    assert f'"{bundle}/with-secrets" "hub" "{bundle}/secrets.env"' in unit_text
    assert (bundle / "secrets.env").read_text(encoding="utf-8").startswith("# Add values")
    assert stat.S_IMODE((bundle / "with-secrets").stat().st_mode) == 0o700
    assert (bundle / "state").is_dir()
    assert (bundle / "workspace").is_dir()
    assert stat.S_IMODE((bundle / "pilot.env").stat().st_mode) == 0o600
    assert stat.S_IMODE((bundle / "secrets.env").stat().st_mode) == 0o600
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700


def test_rejects_non_tailnet_origins_before_creating_bundle(tmp_path: Path) -> None:
    output_root = tmp_path / "pilots"
    result = _invoke(tmp_path, origin="https://pilot.example.com", output_root=output_root)

    assert result.returncode == 2
    assert "tailnet origin" in result.stderr
    assert not (output_root / "pilot-acme").exists()


def test_requires_path_on_separately_hosted_workos_callback(tmp_path: Path) -> None:
    output_root = tmp_path / "pilots"
    result = _invoke(
        tmp_path,
        workos_redirect_uri="https://pilot.example.ts.net/",
        output_root=output_root,
    )

    assert result.returncode == 2
    assert "separate auth frontend" in result.stderr
    assert not (output_root / "pilot-acme").exists()


def test_rejects_bundle_overlapping_private_hub_home(tmp_path: Path) -> None:
    output_root = tmp_path / "private-hub"
    result = _invoke(tmp_path, output_root=output_root)

    assert result.returncode == 2
    assert "non-overlapping" in result.stderr
    assert not (output_root / "pilot-acme").exists()


def test_escapes_environment_file_path_for_systemd(tmp_path: Path) -> None:
    output_root = tmp_path / "pilot $HOME bundles%"
    result = _invoke(tmp_path, output_root=output_root)

    assert result.returncode == 0, result.stderr
    unit_text = (output_root / "pilot-acme/systemd/unified-agent-pilot-acme.service").read_text(
        encoding="utf-8"
    )
    env_path = str(output_root / "pilot-acme/pilot.env")
    assert f"EnvironmentFile={env_path}".replace(" ", r"\x20").replace("%", "%%") in unit_text
    wrapper_path = str(output_root / "pilot-acme/with-secrets")
    assert f'"{wrapper_path.replace("%", "%%").replace("$", "$$")}" "hub"' in unit_text


def test_resolves_relative_binary_paths_for_systemd_units(tmp_path: Path) -> None:
    result = _invoke(tmp_path, output_root=tmp_path / "pilots", relative_binaries=True)
    bundle = tmp_path / "pilots" / "pilot-acme"
    hub_text = (bundle / "systemd/unified-agent-pilot-acme.service").read_text(encoding="utf-8")
    replication_text = (bundle / "systemd/unified-agent-pilot-acme-litestream.service").read_text(
        encoding="utf-8"
    )

    assert result.returncode == 0, result.stderr
    assert f'"{Path("bin/unified-agent").resolve()}"' in hub_text
    assert f'"{Path("bin/litestream").resolve()}"' in replication_text


def test_refuses_to_overwrite_existing_pilot_bundle(tmp_path: Path) -> None:
    output_root = tmp_path / "pilots"
    first = _invoke(tmp_path, output_root=output_root)
    bundle = output_root / "pilot-acme"
    marker = bundle / "operator-note.txt"
    marker.write_text("keep", encoding="utf-8")

    second = _invoke(tmp_path, output_root=output_root)

    assert first.returncode == 0
    assert second.returncode == 2
    assert "refusing to overwrite" in second.stderr
    assert marker.read_text(encoding="utf-8") == "keep"


def test_identity_map_must_be_nonempty_and_map_to_agent_labels(tmp_path: Path) -> None:
    result = _invoke(tmp_path, identity_map=[])

    assert result.returncode == 2
    assert "non-empty JSON object" in result.stderr
    assert not (tmp_path / ".local/share/unified-agent/pilots/pilot-acme").exists()


def test_secret_loader_executes_only_allowlisted_hub_values(tmp_path: Path) -> None:
    result = _invoke(tmp_path, output_root=tmp_path / "pilots")
    bundle = tmp_path / "pilots" / "pilot-acme"
    secrets = bundle / "secrets.env"
    secrets.write_text(
        "WORKOS_API_KEY=synthetic-key\n"
        "WORKOS_COOKIE_PASSWORD=synthetic-cookie-key\n"
        "GOOGLE_APPLICATION_CREDENTIALS=/secure/pilot/credentials.json\n",
        encoding="utf-8",
    )
    command = tmp_path / "capture-env.py"
    command.write_text(
        "import json, os\n"
        "print(json.dumps({key: os.environ.get(key) for key in ("
        "'HOME', 'TMPDIR', 'UA_HOME', 'WORKOS_API_KEY', 'WORKOS_COOKIE_PASSWORD', "
        "'UA_AGENT_TOKENS', 'GOOGLE_APPLICATION_CREDENTIALS')}))\n",
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    completed = subprocess.run(
        [
            sys.executable,
            str(bundle / "with-secrets"),
            "hub",
            str(secrets),
            sys.executable,
            str(command),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "UA_HOME": "/isolated/pilot",
            "UA_AGENT_TOKENS": "private-hub-token",
            "GOOGLE_APPLICATION_CREDENTIALS": "/private/hub/credentials.json",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "HOME": str(bundle / "runtime-home"),
        "TMPDIR": str(bundle / "tmp"),
        "UA_HOME": "/isolated/pilot",
        "WORKOS_API_KEY": "synthetic-key",
        "WORKOS_COOKIE_PASSWORD": "synthetic-cookie-key",
        "UA_AGENT_TOKENS": None,
        "GOOGLE_APPLICATION_CREDENTIALS": None,
    }


def test_secret_loader_rejects_private_hub_overrides(tmp_path: Path) -> None:
    result = _invoke(tmp_path, output_root=tmp_path / "pilots")
    bundle = tmp_path / "pilots" / "pilot-acme"
    secrets = bundle / "secrets.env"
    secrets.write_text(
        "WORKOS_API_KEY=unique-secret-value\n"
        "WORKOS_COOKIE_PASSWORD=unique-cookie-secret\n"
        "UA_HOME=/private/hub\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(bundle / "with-secrets"),
            "hub",
            str(secrets),
            sys.executable,
            "-c",
            "pass",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert completed.returncode != 0
    assert "not allowed for hub: UA_HOME" in completed.stderr
    assert "unique-secret-value" not in completed.stderr
    assert "unique-cookie-secret" not in completed.stderr


def test_replication_secret_loader_requires_explicit_isolated_adc_path(tmp_path: Path) -> None:
    result = _invoke(tmp_path, output_root=tmp_path / "pilots")
    bundle = tmp_path / "pilots" / "pilot-acme"
    secrets = bundle / "secrets.env"
    secrets.write_text(
        "WORKOS_API_KEY=synthetic-key\n"
        "WORKOS_COOKIE_PASSWORD=synthetic-cookie-key\n"
        "GOOGLE_APPLICATION_CREDENTIALS=/secure/pilot/credentials.json\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(bundle / "with-secrets"),
            "replication",
            str(secrets),
            sys.executable,
            "-c",
            "import os; print(os.environ['GOOGLE_APPLICATION_CREDENTIALS']); print(os.environ['HOME'])",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": "/private/service-user",
            "GOOGLE_APPLICATION_CREDENTIALS": "/private/service-user/.config/gcloud/application_default_credentials.json",
        },
    )

    assert result.returncode == 0, result.stderr
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "/secure/pilot/credentials.json",
        str(bundle / "runtime-home"),
    ]


def test_replication_refuses_ambient_adc_without_explicit_pilot_path(tmp_path: Path) -> None:
    result = _invoke(tmp_path, output_root=tmp_path / "pilots")
    bundle = tmp_path / "pilots" / "pilot-acme"
    secrets = bundle / "secrets.env"
    secrets.write_text(
        "WORKOS_API_KEY=synthetic-key\nWORKOS_COOKIE_PASSWORD=synthetic-cookie-key\n",
        encoding="utf-8",
    )
    marker = tmp_path / "replication-started"
    completed = subprocess.run(
        [
            sys.executable,
            str(bundle / "with-secrets"),
            "replication",
            str(secrets),
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GOOGLE_APPLICATION_CREDENTIALS": "/private/ambient.json"},
    )

    assert result.returncode == 0, result.stderr
    assert completed.returncode != 0
    assert "missing required pilot secrets: GOOGLE_APPLICATION_CREDENTIALS" in completed.stderr
    assert not marker.exists()
