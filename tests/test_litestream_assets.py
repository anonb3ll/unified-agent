"""Static regression checks for continuous WAL replication (task #1197).

Retired in task #1411 — the tests below now guard the *retirement*: the
auto-start agent must stay archived, the runner must stay opt-in, and the
doctor's replication probe must keep working for a deliberate re-enable.

File reads of the deploy assets plus a mocked-out _replication_check probe — no litestream
binary or GCS access required; the live drill runs on the Mac hub.
"""

import ast
import inspect
import os
import re
import subprocess
import textwrap
from pathlib import Path

from export_guard import needs_exported_paths

REPO_ROOT = Path(__file__).parent.parent
CONFIG = REPO_ROOT / "deploy" / "litestream" / "litestream.yml"
RUNNER = REPO_ROOT / "deploy" / "mac-hub" / "run-litestream.sh"
# The plist was archived in task #1411; the old deploy path must stay empty.
PLIST = REPO_ROOT / "deploy" / "mac-hub" / "com.unified-agent.litestream.plist"
RETIRED_DIR = REPO_ROOT / "deploy" / "mac-hub" / "retired"
RETIRED_PLIST = RETIRED_DIR / "com.unified-agent.litestream.plist"
SETUP = REPO_ROOT / "deploy" / "mac-hub" / "setup-agents-and-executors.sh"
REHEARSAL = REPO_ROOT / "scripts" / "db_restore_rehearsal.py"
DOCS = REPO_ROOT / "docs" / "database-operations.md"
# deploy/mac-hub/ is dropped from the public export (task #2615), so the
# bodies that read the runner, the retired plist and the setup script skip
# there; in the private repo a renamed file still fails them.
needs_mac_hub_assets = needs_exported_paths(RUNNER, PLIST, RETIRED_PLIST, SETUP, repo=REPO_ROOT)


def test_litestream_config_exists_and_uses_env_expansion():
    text = CONFIG.read_text()
    assert "dbs:" in text
    assert "${UA_HOME}/memory.db" in text
    assert "${UA_REPLICA_GCS_URI}" in text
    assert "sync-interval: 30s" in text
    assert "- interval: 5m" in text
    assert "retention: 24h" in text
    assert "l0-retention-check-interval: 15s" in text


@needs_mac_hub_assets
def test_runner_sources_service_env_and_requires_replica_uri():
    text = RUNNER.read_text()
    assert os.access(RUNNER, os.X_OK), "run-litestream.sh must be executable"
    assert 'source "$ENV_FILE"' in text or "source $ENV_FILE" in text
    assert "UA_REPLICA_GCS_URI:?" in text, "runner must fail when the replica target is unset"
    assert "exec litestream replicate -config" in text


@needs_mac_hub_assets
def test_litestream_stays_retired_and_not_installed_by_default():
    """Replication was retired in task #1411 (GCS list-egress bleed).

    These assertions fail if anyone quietly re-stages the auto-start agent or
    turns the archived plist back into a supervised daemon.
    """
    assert not PLIST.exists(), (
        "the litestream plist must not be re-staged in deploy/mac-hub/ — "
        "it lives in retired/ (task #1411)"
    )
    text = RETIRED_PLIST.read_text()
    assert "com.unified-agent.litestream" in text
    assert "run-litestream.sh" in text
    # RunAtLoad stays true so a deliberate bootstrap prints the retirement
    # notice once; KeepAlive must be false so a stray copy cannot restart the
    # guard every 10 s and spam litestream.err.
    assert re.search(r"<key>RunAtLoad</key>\s*<true/>", text), "RunAtLoad must be true"
    assert re.search(r"<key>KeepAlive</key>\s*<false/>", text), (
        "KeepAlive must be false while retired (task #1411)"
    )

    # The deploy script must not stage the plist (the runner still ships — it is
    # inert without the opt-in). The name appears only in the explaining comment.
    setup = SETUP.read_text()
    assert not re.search(
        r'^\s*"\$SCRIPT_DIR/com\.unified-agent\.litestream\.plist"', setup, re.M
    ), "setup-agents-and-executors.sh must not stage the litestream plist"


@needs_mac_hub_assets
def test_setup_enforces_retirement_on_a_host_that_still_has_it_loaded():
    """Staging cannot stop an already-running job (Devin finding on PR #793).

    An installed plist holds unconditional KeepAlive and its wrapper predates
    the runner's opt-in gate, so a host that had the old agent keeps listing its
    GCS replica after this script runs — the ~$65/day bleed the retirement
    exists to end. The script must bootout, disable, and remove it.
    """
    setup = SETUP.read_text()
    assert re.search(
        r'launchctl bootout "gui/\$\(id -u\)/com\.unified-agent\.litestream"', setup
    ), "setup must bootout a loaded litestream agent"
    assert re.search(
        r'launchctl disable "gui/\$\(id -u\)/com\.unified-agent\.litestream"', setup
    ), "setup must persist the disabled override so launchd cannot restart it"
    assert re.search(r'rm -f "\$LA/com\.unified-agent\.litestream\.plist"', setup), (
        "setup must remove the installed litestream plist"
    )
    # Ordering matters: disabling before bootout would leave the running job up.
    assert setup.index('launchctl bootout "gui/$(id -u)/com.unified-agent.litestream"') < (
        setup.index('rm -f "$LA/com.unified-agent.litestream.plist"')
    )


def _opt_in_snippet(setup: str) -> str:
    """The exact command substitution the script uses to resolve the opt-in."""
    match = re.search(r'opted_in="\$\(([^\n]*)\)"', setup)
    assert match, "the opt-in must be resolved by a single command substitution"
    return match.group(1)


@needs_mac_hub_assets
def test_setup_retirement_respects_a_deliberate_opt_in(tmp_path):
    """Re-running the reconciliation must not stop an opted-in replica.

    This script doubles as a reconciliation command, so an operator who followed
    the documented opt-in and then re-ran setup would otherwise have their live
    replica stopped and disabled while `service.env` still said it was enabled.
    The decision must be read from the host, it must gate the mutation, and it
    must resolve the EFFECTIVE value the runner honours — `run-litestream.sh`
    sources that file under `set -a`, so `export UA_LITESTREAM_ENABLED=1` counts
    as opted in and a later assignment clearing an earlier one does not. The
    cases below run the script's own snippet, so the test cannot drift from it.
    """
    setup = SETUP.read_text()
    assert setup.index("opted_in=") < setup.index(
        'launchctl bootout "gui/$(id -u)/com.unified-agent.litestream"'
    ), "the opt-in check must come before any launchctl mutation"

    snippet = _opt_in_snippet(setup)
    env_file = tmp_path / "service.env"

    def resolve(body: str) -> str:
        env_file.write_text(body)
        proc = subprocess.run(
            ["bash", "-c", snippet],
            env={**os.environ, "ENV_FILE": str(env_file)},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    assert resolve("UA_LITESTREAM_ENABLED=1\n") == "1"
    assert resolve("export UA_LITESTREAM_ENABLED=1\n") == "1", (
        "an exported opt-in is still an opt-in — stopping that replica is the bug"
    )
    assert resolve('UA_LITESTREAM_ENABLED="1"\n') == "1", "quoting must not defeat the read"
    assert resolve("UA_LITESTREAM_ENABLED=1\nUA_LITESTREAM_ENABLED=0\n") == "0", (
        "a later assignment clears an earlier opt-in — the legacy replica must be retired"
    )
    assert resolve("") == "0", "an absent opt-in must default to retirement"


@needs_mac_hub_assets
def test_setup_retirement_runs_before_the_fallible_reconciliation():
    """A failed scp under `set -e` must not leave a legacy replica running.

    The retirement step has to execute before the executor/env transfers: those
    scp calls abort the script, and an already-loaded pre-opt-in agent would then
    survive the very run meant to retire it.
    """
    setup = SETUP.read_text()
    retire_at = setup.index('launchctl bootout "gui/$(id -u)/com.unified-agent.litestream"')
    first_fallible = setup.index('scp -q "$HOME/.config/unified-agent/executor.env"')
    assert retire_at < first_fallible, "retirement must precede the fallible executor env transfer"


@needs_mac_hub_assets
def test_re_enable_runbook_clears_the_disabled_launchd_override():
    """Retirement persists `launchctl disable`; bootstrap fails without enable.

    Both the archived-plist runbook and §4 of the database runbook must carry
    the re-enable step, or a deliberate (and correctly gated) re-enable lands on
    a host where launchd refuses the job.
    """
    for path in (RETIRED_DIR / "README.md", DOCS):
        text = path.read_text()
        assert re.search(
            r'launchctl enable "gui/\$\(id -u\)/com\.unified-agent\.litestream"', text
        ), f"{path.name} must re-enable the launchd override before bootstrap"
    # The runbook that also shows the bootstrap command must order them correctly:
    # a bootstrap attempted while the override is still disabled fails.
    docs = DOCS.read_text()
    assert docs.index("launchctl enable") < docs.index("launchctl bootstrap"), (
        "database-operations.md: enable must precede bootstrap"
    )


def test_rehearsal_docs_do_not_overstate_the_snapshot_drill():
    """The default drill seeds its own temp DB (Devin finding on PR #793).

    `run_rehearsal()` never reads the live database, the nightly GCS snapshot,
    or the audit chain, so docs must not sell it as off-host recoverability.
    """
    docs = DOCS.read_text()
    assert "Know what green means" in docs
    assert "code path" in docs, "the local drill's scope must be stated"
    assert "Either source restores" not in docs, (
        "the shared-wording claim that both modes verify the restored chain is false"
    )
    rehearsal = REHEARSAL.read_text()
    # Guard the claim itself, not just the prose: the chain check lives only in
    # the replica path, and the default path restores a locally created backup.
    default_path = rehearsal[rehearsal.index("def run_rehearsal()") :]
    assert "verify_event_chain" not in default_path, (
        "run_rehearsal() must not claim a chain verification it does not perform"
    )


@needs_mac_hub_assets
def test_runner_refuses_to_start_without_explicit_opt_in():
    text = RUNNER.read_text()
    assert "UA_LITESTREAM_ENABLED" in text, "runner must gate on the explicit opt-in"
    assert '"${UA_LITESTREAM_ENABLED:-0}" != "1"' in text, (
        "the opt-in must default to off, so retirement is the default"
    )
    assert text.index("UA_LITESTREAM_ENABLED") < text.index("exec litestream replicate"), (
        "the opt-in gate must run before litestream is exec'd"
    )
    assert "UA_REPLICA_GCS_URI:?" in text, "runner must still fail when the replica target is unset"


def test_doctor_reports_replication_leg(monkeypatch, tmp_path):
    import unified_agent.tools.doctor as doctor

    # The doctor run includes the leg: _run_system_doctor calls _replication_check.
    run_tree = ast.parse(textwrap.dedent(inspect.getsource(doctor._run_system_doctor)))
    called = {
        node.func.id
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_replication_check" in called
    assert isinstance(doctor.REPLICATION_LAG_BUDGET_S, int | float)

    # Gated on UA_REPLICA_GCS_URI: unset is DORMANT under the leg's capability.
    dormant = _patch_probe(monkeypatch, tmp_path / "off", uri=None)._replication_check()
    assert dormant["capability"] == "wal_replication"
    assert dormant["status"] == "DORMANT"
    assert "UA_REPLICA_GCS_URI" in dormant["detail"]

    # Replica-side state is authoritative, but not via a full LTX enumeration:
    # `ltx -level all` lists every object and exceeds its own timeout. The local
    # side is read with `litestream status -json`; the replica with a bounded sample.
    doctor = _patch_probe(
        monkeypatch,
        tmp_path / "on",
        uri="gs://b/r",
        status=[{"local_txid": "0000000000000005"}],
        ltx=[{"max_txid": "0000000000000005", "timestamp": "2099-01-01T00:00:00Z"}],
    )
    argvs: list[list[str]] = []
    recorded_json = doctor._litestream_json

    def recording_json(argv, env):
        argvs.append(list(argv))
        return recorded_json(argv, env)

    monkeypatch.setattr(doctor, "_litestream_json", recording_json)
    result = doctor._replication_check()
    assert result["capability"] == "wal_replication"
    assert result["status"] == "LIVE"
    assert [argv[1:3] for argv in argvs] == [["status", "-json"]]
    assert not any("-level" in argv for argv in argvs)
    assert doctor._replica_sample_calls["n"] == 1


def test_rehearsal_supports_replica_restore_and_audit_verify():
    text = REHEARSAL.read_text()
    assert "--from-replica" in text
    assert "litestream" in text and '"restore"' in text
    assert "verify_event_chain" in text


def test_runbook_documents_sole_authority_and_manual_promotion():
    text = DOCS.read_text()
    assert "sole write authority" in text
    assert "--from-replica" in text
    assert "com.unified-agent.litestream" in text
    assert "UA_REPLICA_GCS_URI" in text
    assert re.search(r"promotion.*manual|manual.*promotion", text, re.I)


def _patch_probe(monkeypatch, tmp_path, *, uri=None, binary="/bin/true", status=None, ltx=None):
    import unified_agent.tools.doctor as doctor

    monkeypatch.setenv("UA_REPLICA_GCS_URI", uri) if uri else monkeypatch.delenv(
        "UA_REPLICA_GCS_URI", raising=False
    )
    monkeypatch.setattr(doctor, "_litestream_binary", lambda: binary)
    # _hub_setting reads the hub unit's env first — isolate it so the monkeypatched
    # process env is the only source (a real dev hub may legitimately set these).
    monkeypatch.setattr(doctor, "_service_env", lambda unit: {})
    cfg_dir = tmp_path / "deploy" / "litestream"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "litestream.yml").write_text("dbs: []\n")
    monkeypatch.setattr(doctor, "_repo_deploy_dir", lambda: tmp_path / "deploy")
    responses = {"status": status or [], "ltx": ltx or []}
    sample_calls = {"n": 0}

    def fake_json(argv, env):
        if argv[1] == "ltx":
            raise AssertionError("replication check must not shell out to litestream ltx")
        return list(responses[argv[1]]), None

    def fake_sample(uri, local_txid, env):
        sample_calls["n"] += 1
        return list(responses["ltx"]), None

    monkeypatch.setattr(doctor, "_litestream_json", fake_json)
    monkeypatch.setattr(doctor, "_replica_sample", fake_sample)
    monkeypatch.setattr(doctor, "_replica_cache", None)
    doctor._replica_sample_calls = sample_calls
    return doctor


def test_replication_check_dormant_when_unconfigured(monkeypatch, tmp_path):
    doctor = _patch_probe(monkeypatch, tmp_path, uri=None, binary=None)
    result = doctor._replication_check()
    assert result["status"] == "DORMANT"
    assert result["capability"] == "wal_replication"


def test_replication_check_misconfigured_uri_without_binary(monkeypatch, tmp_path):
    doctor = _patch_probe(monkeypatch, tmp_path, uri="gs://b/r", binary=None)
    result = doctor._replication_check()
    assert result["status"] == "MISCONFIGURED"


def test_replication_check_live_when_caught_up(monkeypatch, tmp_path):
    doctor = _patch_probe(
        monkeypatch,
        tmp_path,
        uri="gs://b/r",
        status=[{"local_txid": "0000000000000005"}],
        ltx=[{"max_txid": "0000000000000005", "timestamp": "2099-01-01T00:00:00Z"}],
    )
    result = doctor._replication_check()
    assert result["status"] == "LIVE"
    assert "caught up" in result["detail"]


def test_replication_check_misconfigured_on_stale_gap(monkeypatch, tmp_path):
    doctor = _patch_probe(
        monkeypatch,
        tmp_path,
        uri="gs://b/r",
        status=[{"local_txid": "0000000000000009"}],
        ltx=[{"max_txid": "0000000000000005", "timestamp": "2000-01-01T00:00:00Z"}],
    )
    result = doctor._replication_check()
    assert result["status"] == "MISCONFIGURED"
    assert "lag budget" in result["detail"]


def test_replication_check_misconfigured_when_never_synced(monkeypatch, tmp_path):
    doctor = _patch_probe(
        monkeypatch,
        tmp_path,
        uri="gs://b/r",
        status=[{"local_txid": "0000000000000001"}],
        ltx=[],
    )
    result = doctor._replication_check()
    assert result["status"] == "MISCONFIGURED"
    assert "never synced" in result["detail"]


def test_full_enumeration_timeout_does_not_mark_caught_up_replica_down(monkeypatch, tmp_path):
    """A probe that costs more than its budget must not turn a caught-up replica into MISCONFIGURED."""
    doctor = _patch_probe(
        monkeypatch,
        tmp_path,
        uri="gs://b/r",
        status=[{"local_txid": "0000000000000005"}],
        ltx=[{"max_txid": "0000000000000005", "timestamp": "2099-01-01T00:00:00Z"}],
    )

    def exploding_ltx(argv, env):
        if len(argv) > 1 and argv[1] == "ltx":
            return None, "Command '['ltx', '-json', '-level', 'all']' timed out after 45.0 seconds"
        return [{"local_txid": "0000000000000005"}], None

    monkeypatch.setattr(doctor, "_litestream_json", exploding_ltx)
    result = doctor._replication_check()
    assert result["status"] == "LIVE"
    assert "timed out" not in result["detail"]


def test_newest_l0_row_finds_covering_file_without_scanning_the_prefix(monkeypatch, tmp_path):
    import unified_agent.tools.doctor as doctor

    seen = []

    def fetch_one(start_offset: str):
        seen.append(start_offset)
        # File min 4 covers local txid 5. Offsets at or above 5 sort after it.
        tail = start_offset.rsplit("/", 1)[-1]
        if int(tail, 16) <= 4:
            return {
                "name": "litestream/memory/ltx/0/0000000000000004-0000000000000005.ltx",
                "metadata": {"litestream-timestamp": "2099-01-01T00:00:00Z"},
            }
        return None

    row, err = doctor._newest_l0_row("litestream/memory/ltx/0/", 5, fetch_one)
    assert err is None
    assert row["max_txid"] == "0000000000000005"
    assert row["timestamp"] == "2099-01-01T00:00:00Z"
    assert len(seen) < 16


def test_newest_l0_row_finds_a_low_file_in_a_large_txid_space():
    import unified_agent.tools.doctor as doctor

    def fetch_one(start_offset: str):
        tail = int(start_offset.rsplit("/", 1)[-1], 16)
        if tail <= 1:
            return {
                "name": "litestream/memory/ltx/0/0000000000000001-0000000000000001.ltx",
                "metadata": {"litestream-timestamp": "2099-01-01T00:00:00Z"},
            }
        return None

    row, err = doctor._newest_l0_row("litestream/memory/ltx/0/", 0x400000000, fetch_one)
    assert err is None
    assert row["max_txid"] == "0000000000000001"


def test_budget_exhaustion_is_not_an_empty_replica(monkeypatch):
    import unified_agent.tools.doctor as doctor

    doctor._replica_cache = None
    monkeypatch.setattr(doctor, "_gcs_access_token", lambda env: "tok")
    monkeypatch.setattr(doctor, "REPLICA_SAMPLE_BUDGET_S", -1)
    monkeypatch.setattr(doctor, "_gcs_first_object", lambda *args: None)
    rows, err = doctor._replica_sample("gs://b/litestream/memory", 10, {})
    assert rows is None
    assert err and "budget" in err


def test_empty_levels_take_one_list_each(monkeypatch):
    import unified_agent.tools.doctor as doctor

    doctor._replica_cache = None
    monkeypatch.setattr(doctor, "_gcs_access_token", lambda env: "tok")
    calls = {"n": 0}

    def first(bucket, prefix, start, token):
        calls["n"] += 1
        return None

    monkeypatch.setattr(doctor, "_gcs_first_object", first)
    rows, err = doctor._replica_sample("gs://b/litestream/memory", 0x400000000, {})
    assert err is None
    assert rows == []
    assert calls["n"] == 10


def test_higher_level_error_keeps_a_fresh_lower_level_row(monkeypatch):
    import unified_agent.tools.doctor as doctor

    doctor._replica_cache = None
    monkeypatch.setattr(doctor, "_gcs_access_token", lambda env: "tok")

    def first(bucket, prefix, start, token):
        level = prefix.rstrip("/").rsplit("/", 1)[-1]
        if level == "0":
            return {
                "name": f"{prefix}0000000000000009-0000000000000009.ltx",
                "metadata": {"litestream-timestamp": "2099-01-01T00:00:00Z"},
            }
        if level == "1":
            raise RuntimeError("list timed out")
        return None

    monkeypatch.setattr(doctor, "_gcs_first_object", first)
    rows, err = doctor._replica_sample("gs://b/litestream/memory", 10, {})
    assert rows[0]["max_txid"] == "0000000000000009"
    assert err  # incomplete, so the caller must not cache it
    cached_rows, cached_err = doctor._cached_replica_sample("gs://b/litestream/memory", 10, {})
    assert cached_rows[0]["max_txid"] == "0000000000000009"
    assert cached_err
    assert doctor._replica_cache is None


def test_empty_l0_falls_back_to_a_higher_level(monkeypatch):
    import unified_agent.tools.doctor as doctor

    doctor._replica_cache = None
    monkeypatch.setattr(doctor, "_gcs_access_token", lambda env: "tok")

    def first(bucket, prefix, start, token):
        level = prefix.rstrip("/").rsplit("/", 1)[-1]
        tail_s = start.rsplit("/", 1)[-1]
        try:
            tail = int(tail_s, 16)
        except ValueError:
            tail = 0
        if level == "0":
            return None
        if level == "1" and tail <= 5:
            return {
                "name": f"{prefix}0000000000000005-0000000000000005.ltx",
                "metadata": {"litestream-timestamp": "2099-01-01T00:00:00Z"},
            }
        return None

    monkeypatch.setattr(doctor, "_gcs_first_object", first)
    rows, err = doctor._replica_sample("gs://b/litestream/memory", 5, {})
    assert err is None
    assert rows[0]["max_txid"] == "0000000000000005"


def test_replica_cache_refreshes_when_local_txid_advances(monkeypatch, tmp_path):
    doctor = _patch_probe(
        monkeypatch,
        tmp_path,
        uri="gs://b/r",
        status=[{"local_txid": "0000000000000005"}],
        ltx=[{"max_txid": "0000000000000005", "timestamp": "2099-01-01T00:00:00Z"}],
    )
    assert doctor._replication_check()["status"] == "LIVE"
    assert doctor._replica_sample_calls["n"] == 1
    doctor._replica_sample = lambda uri, local_txid, env: (
        doctor._replica_sample_calls.__setitem__("n", doctor._replica_sample_calls["n"] + 1)
        or [{"max_txid": f"{local_txid:016x}", "timestamp": "2099-01-01T00:00:00Z"}],
        None,
    )

    # The check reads local txid from status, so advance that too.
    def status_only(argv, env):
        return [{"local_txid": "0000000000000006"}], None

    monkeypatch.setattr(doctor, "_litestream_json", status_only)
    assert doctor._replication_check()["status"] == "LIVE"
    assert doctor._replica_sample_calls["n"] == 2


def test_replica_sample_is_not_rerun_inside_the_cache_window(monkeypatch, tmp_path):
    doctor = _patch_probe(
        monkeypatch,
        tmp_path,
        uri="gs://b/r",
        status=[{"local_txid": "0000000000000005"}],
        ltx=[{"max_txid": "0000000000000005", "timestamp": "2099-01-01T00:00:00Z"}],
    )
    first = doctor._replication_check()
    second = doctor._replication_check()
    assert first["status"] == "LIVE"
    assert second["status"] == "LIVE"
    assert doctor._replica_sample_calls["n"] == 1
