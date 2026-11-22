"""Every process sharing a data directory must share the tenant (task #1996).

``edition.tenant_org_id()`` reads the process environment. An executor or CLI
that opens a tenanted data directory without ``WORKOS_ORGANIZATION_ID`` stamps
NULL ``org_id`` on its rows, which the hub can then neither see nor end. Each
process that opens the store records the tenant it stamps in a per-process
beacon under ``$UA_HOME/tenant-beacons``; the ``tenant_consistency`` doctor row
compares the recent beacons with the hub's tenant.
"""

from __future__ import annotations

import errno
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unified_agent import config, edition, storage
from unified_agent.tools import doctor

ORG = "org_01TESTTENANT"


@pytest.fixture
def home(monkeypatch, tmp_path):
    ua_home = tmp_path / "ua"
    monkeypatch.setenv("UA_HOME", str(ua_home))
    for name in ("UA_EDITION", "WORKOS_ORGANIZATION_ID", "UA_HUB_WORKOS_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    # The hub unit "sets nothing": the doctor reads the hub's tenant from
    # this process's environment, as it does on an unreadable unit.
    monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: "")
    monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [])
    monkeypatch.setattr(edition, "_beacons_written", {})
    # No real heartbeat thread from these tests: mark this process's
    # heartbeat as started and give it an empty, test-local dir set.
    monkeypatch.setattr(edition, "_heartbeat_dirs", set())
    monkeypatch.setattr(edition, "_heartbeat_pid", os.getpid())
    monkeypatch.setattr(edition, "_heartbeat_dirs_pid", os.getpid())
    storage.init_db()
    return ua_home


def _beacon(home, pid, org_id, *, process="unified-agent-executor", age=timedelta(0)):
    directory = home / edition.BEACON_DIRNAME
    directory.mkdir(mode=0o700, exist_ok=True)
    seen = (datetime.now(UTC) - age).isoformat(timespec="seconds")
    (directory / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "process": process,
                "edition": "enterprise",
                "org_id": org_id,
                "seen_at": seen,
            }
        )
    )


def _row():
    return doctor._tenant_consistency_check()


# ---------------------------------------------------------------- the beacon


def test_opening_the_store_records_this_process_tenant(home, monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    monkeypatch.setattr(edition, "_beacons_written", {})

    storage.connect().close()

    path = home / edition.BEACON_DIRNAME / f"{os.getpid()}.json"
    beacon = json.loads(path.read_text())
    assert beacon["pid"] == os.getpid()
    assert beacon["org_id"] == ORG
    assert beacon["edition"] == "enterprise"
    assert path.stat().st_mode & 0o777 == 0o600
    assert (home / edition.BEACON_DIRNAME).stat().st_mode & 0o777 == 0o700


def test_beacon_follows_a_tenant_change_and_core_edition_stamps_none(home, monkeypatch):
    path = home / edition.BEACON_DIRNAME / f"{os.getpid()}.json"
    storage.connect().close()
    assert json.loads(path.read_text())["org_id"] is None

    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    storage.connect().close()
    assert json.loads(path.read_text())["org_id"] == ORG

    monkeypatch.setenv("UA_EDITION", "core")
    storage.connect().close()
    assert json.loads(path.read_text())["org_id"] is None


def test_beacon_is_written_once_per_refresh_interval(home, monkeypatch):
    monkeypatch.setattr(edition, "_beacons_written", {})
    writes = []
    real_replace = os.replace
    monkeypatch.setattr(
        edition.os, "replace", lambda src, dst: writes.append(dst) or real_replace(src, dst)
    )
    for _ in range(5):
        storage.connect().close()
    assert len(writes) == 1


def test_beacon_write_failure_never_breaks_the_store(home, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(edition.os, "replace", boom)
    conn = storage.connect()
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_tenant_for_matches_tenant_org_id(monkeypatch):
    monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
    for ed in (None, "enterprise", "core", "bogus"):
        for org in (None, "", ORG):
            if ed is None:
                monkeypatch.delenv("UA_EDITION", raising=False)
            else:
                monkeypatch.setenv("UA_EDITION", ed)
            if org is None:
                monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
            else:
                monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)
            assert edition.tenant_for(ed, org) == edition.tenant_org_id(), (ed, org)


# ------------------------------------------------------------ the doctor row


def test_degraded_when_a_writer_stamps_no_tenant_on_a_tenanted_hub(home, monkeypatch):
    """The #1995 case: an executor without WORKOS_ORGANIZATION_ID."""
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    _beacon(home, 4242, None, process="unified-agent-executor")

    row = _row()

    assert row["capability"] == "tenant_consistency"
    assert row["status"] == "DEGRADED", row
    assert "unified-agent-executor (pid 4242) stamps no tenant" in row["detail"]
    assert ORG in row["detail"]
    assert "WORKOS_ORGANIZATION_ID" in row["gate"]


def test_degraded_when_a_writer_stamps_another_org(home, monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    _beacon(home, 4243, "org_OTHER", process="unified-agent-review")

    row = _row()

    assert row["status"] == "DEGRADED", row
    assert "unified-agent-review (pid 4243) stamps org_OTHER" in row["detail"]


def test_degraded_when_a_writer_stamps_a_tenant_the_untenanted_hub_cannot_see(home):
    _beacon(home, 4244, ORG)

    row = _row()

    assert row["status"] == "DEGRADED", row
    assert f"stamps {ORG}" in row["detail"]


def test_live_when_the_only_org_is_the_hosted_workos_organization(home, monkeypatch):
    """Hosted hub: UA_HUB_WORKOS_ORGANIZATION_ID is the edition tenant."""
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", ORG)
    monkeypatch.setattr(edition, "_beacons_written", {})
    storage.connect().close()

    row = _row()

    assert row["status"] == "LIVE", row
    assert ORG in row["detail"]
    beacon = json.loads((home / edition.BEACON_DIRNAME / f"{os.getpid()}.json").read_text())
    assert beacon["org_id"] == ORG


def test_degraded_when_a_writer_misses_the_hosted_workos_org(home, monkeypatch):
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", ORG)
    _beacon(home, 8, None, process="unified-agent")

    row = _row()

    assert row["status"] == "DEGRADED", row
    assert "unified-agent (pid 8) stamps no tenant" in row["detail"]
    assert ORG in row["detail"]


def test_live_when_every_recent_writer_stamps_the_hub_tenant(home, monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    storage.connect().close()  # this process's own beacon, now tenanted
    _beacon(home, 4245, ORG)

    row = _row()

    assert row["status"] == "LIVE", row
    assert "2 process(es)" in row["detail"]


def test_old_beacons_are_not_recent_writers(home, monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    storage.connect().close()
    _beacon(home, 4246, None, age=timedelta(days=2))

    assert _row()["status"] == "LIVE"


def test_dormant_when_neither_hub_nor_writers_are_tenanted(home):
    storage.connect().close()
    _beacon(home, 4247, None)

    row = _row()

    assert row["status"] == "DORMANT", row


def test_unreadable_beacons_are_skipped(home, monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    storage.connect().close()
    (home / edition.BEACON_DIRNAME / "9999.json").write_text("{not json")

    assert _row()["status"] == "LIVE"


def test_unknown_when_a_tenanted_hub_has_no_recent_beacon(home, monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    for path in (home / edition.BEACON_DIRNAME).glob("*.json"):
        path.unlink()

    row = _row()

    assert row["status"] == "UNKNOWN", row


def test_row_reads_the_hub_unit_tenant_not_the_callers(home, monkeypatch, tmp_path):
    """A shell running the doctor without the tenant env must not decide the
    hub's tenant: the unit's WORKOS_ORGANIZATION_ID does."""
    monkeypatch.setattr(
        doctor,
        "_unit_property",
        lambda unit, prop: (
            f"WORKOS_ORGANIZATION_ID={ORG} UA_HOME={config.data_home()}"
            if prop == "Environment"
            else ""
        ),
    )
    (home / edition.BEACON_DIRNAME / f"{os.getpid()}.json").unlink()
    _beacon(home, 4248, ORG)

    assert _row()["status"] == "LIVE"


@pytest.mark.parametrize("hub_matches", [False, True])
def test_readable_hub_unit_omitting_home_uses_hub_default(home, monkeypatch, tmp_path, hub_matches):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "hub-user"))
    Path.home().mkdir(parents=True)
    hub_home = Path.home() / ".unified_agent"
    hub_home.mkdir()
    monkeypatch.setattr(
        doctor,
        "_unit_property",
        lambda unit, prop: (
            f"UA_EDITION=enterprise WORKOS_ORGANIZATION_ID={ORG}" if prop == "Environment" else ""
        ),
    )
    for path in (home / edition.BEACON_DIRNAME).glob("*.json"):
        path.unlink()
    _beacon(home, 5151, None if hub_matches else ORG)
    _beacon(hub_home, 5252, ORG if hub_matches else None)
    assert doctor._hub_posture_setting("UA_HOME") == (None, True)
    row = _row()
    assert row["status"] == ("LIVE" if hub_matches else "DEGRADED"), row
    if not hub_matches:
        assert "pid 5252" in row["detail"]


@pytest.mark.parametrize("pid", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_pid_does_not_hide_valid_beacons(home, pid):
    _beacon(home, 5353, ORG)
    _beacon(home, pid, None)
    beacons = edition.read_tenant_beacons(home, max_age_s=86400)
    assert any(b["pid"] == 5353 for b in beacons)
    assert all(b["pid"] > 0 for b in beacons)


def test_same_pid_writes_use_separate_atomic_temps(home, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr(edition, "_beacons_written", {})
    barrier = Barrier(2)
    sources, published = [], []
    real_replace = os.replace

    def replace(src, dst):
        sources.append(str(src))
        barrier.wait(timeout=5)
        real_replace(src, dst)
        published.append(json.loads(dst.read_text()))

    monkeypatch.setattr(edition.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: edition.record_tenant_beacon(home), range(2)))
    assert len(set(sources)) == 2
    assert len(published) == 2
    assert all(b["pid"] == os.getpid() for b in published)
    assert not list((home / edition.BEACON_DIRNAME).glob(".*.tmp"))


def test_failed_beacon_replace_cleans_own_temp(home, monkeypatch):
    monkeypatch.setattr(edition, "_beacons_written", {})
    monkeypatch.setattr(edition.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("fixture")))
    edition.record_tenant_beacon(home)
    assert not list((home / edition.BEACON_DIRNAME).glob(".*.tmp"))


# ------------------------------------------------- review follow-ups (#2010)


def _age_own_beacon(home, age):
    path = home / edition.BEACON_DIRNAME / f"{os.getpid()}.json"
    data = json.loads(path.read_text())
    data["seen_at"] = (datetime.now(UTC) - age).isoformat(timespec="seconds")
    path.write_text(json.dumps(data))


def test_idle_process_holding_the_store_keeps_its_beacon_fresh(home, monkeypatch):
    """A process that keeps one connection open for days opens no new one, so
    connect() alone cannot refresh its beacon; the heartbeat must."""
    monkeypatch.setattr(edition, "_heartbeat_dirs", set())
    monkeypatch.setattr(edition, "_heartbeat_pid", os.getpid())  # no real thread
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    conn = storage.connect()  # the hub's own beacon, tenant ORG
    try:
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID")
        # The idle writer: its beacon says "no tenant" and is two days old.
        edition._beacons_written.clear()
        edition.record_tenant_beacon(home)
        _age_own_beacon(home, timedelta(days=2))
        assert edition.read_tenant_beacons(home, max_age_s=86400) == []

        edition._refresh_tenant_beacons()

        beacons = edition.read_tenant_beacons(home, max_age_s=86400)
        assert [b["org_id"] for b in beacons] == [None]
    finally:
        conn.close()


def test_heartbeat_starts_once_per_process(home, monkeypatch):
    started = []
    monkeypatch.setattr(edition, "_heartbeat_dirs", set())
    monkeypatch.setattr(edition, "_heartbeat_pid", None)
    monkeypatch.setattr(edition, "_start_heartbeat_thread", lambda: started.append(1))
    for _ in range(3):
        storage.connect().close()
    assert started == [1]
    assert str(home) in edition._heartbeat_dirs


def test_heartbeat_forgets_a_removed_data_dir(home, tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    monkeypatch.setattr(edition, "_heartbeat_dirs", {str(gone)})
    edition._refresh_tenant_beacons()
    assert edition._heartbeat_dirs == set()
    assert not gone.exists()


def test_future_beacon_is_not_a_recent_writer(home, monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    storage.connect().close()
    _beacon(home, 4250, None, age=-timedelta(days=3))

    assert _row()["status"] == "LIVE"


def test_small_clock_skew_still_counts(home, monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG)
    storage.connect().close()
    _beacon(home, 4251, None, age=-timedelta(seconds=30))

    assert _row()["status"] == "DEGRADED"


# ------------------------------------------ independent review follow-ups (#2010)


def test_heartbeat_retries_when_the_thread_fails_to_start(home, monkeypatch):
    attempts = []

    def start():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(edition, "_heartbeat_pid", None)
    monkeypatch.setattr(edition, "_start_heartbeat_thread", start)
    edition.record_tenant_beacon(home)
    assert edition._heartbeat_pid is None
    edition.record_tenant_beacon(home)
    assert len(attempts) == 2
    assert edition._heartbeat_pid == os.getpid()
    assert str(home) in edition._heartbeat_dirs


def test_fork_while_the_heartbeat_lock_is_held_does_not_deadlock_the_child(home, monkeypatch):
    import signal
    import time
    import warnings

    monkeypatch.setattr(edition, "_start_heartbeat_thread", lambda: None)
    with edition._heartbeat_lock:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # child: the lock was held at fork time
            code = 1
            try:
                edition._register_heartbeat(home)
                code = 0 if edition._heartbeat_pid == os.getpid() else 2
            finally:
                os._exit(code)
    deadline = time.monotonic() + 10
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("child deadlocked on the inherited heartbeat lock")
        time.sleep(0.01)
    assert os.waitstatus_to_exitcode(status) == 0


def test_hub_edition_from_its_settings_file_counts(home, monkeypatch):
    """A hub whose UA_EDITION comes from settings.toml stamps no tenant even
    with WORKOS_ORGANIZATION_ID in its unit; the row must see the same."""
    monkeypatch.setattr(
        doctor,
        "_unit_property",
        lambda unit, prop: (
            f"WORKOS_ORGANIZATION_ID={ORG} UA_HOME={home}" if prop == "Environment" else ""
        ),
    )
    settings_file = home / "settings.toml"
    settings_file.write_text('[env]\nUA_EDITION = "core"\n')
    settings_file.chmod(0o600)
    for path in (home / edition.BEACON_DIRNAME).glob("*.json"):
        path.unlink()
    _beacon(home, 6161, None, process="unified-agent")

    row = _row()

    assert row["status"] == "DORMANT", row


def test_service_env_edition_beats_the_settings_file(home, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "_unit_property",
        lambda unit, prop: (
            f"UA_EDITION=enterprise WORKOS_ORGANIZATION_ID={ORG} UA_HOME={home}"
            if prop == "Environment"
            else ""
        ),
    )
    settings_file = home / "settings.toml"
    settings_file.write_text('[env]\nUA_EDITION = "core"\n')
    settings_file.chmod(0o600)
    for path in (home / edition.BEACON_DIRNAME).glob("*.json"):
        path.unlink()
    _beacon(home, 6262, None)

    assert _row()["status"] == "DEGRADED"


# ------------------------------------- independent review, round 2 (#2010)


def test_heartbeat_registers_absolute_dirs(home, monkeypatch, tmp_path):
    here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
    for directory in (here / "rel", elsewhere / "rel"):
        directory.mkdir(parents=True)
    monkeypatch.chdir(here)
    edition.record_tenant_beacon(Path("rel"))
    assert str(here / "rel") in edition._heartbeat_dirs
    for path in (here / "rel" / edition.BEACON_DIRNAME).glob("*.json"):
        path.unlink()

    monkeypatch.chdir(elsewhere)
    edition._refresh_tenant_beacons()

    assert list((here / "rel" / edition.BEACON_DIRNAME).glob("*.json"))
    assert not (elsewhere / "rel" / edition.BEACON_DIRNAME).exists()


def _relative_home_hub(monkeypatch, tmp_path, workdir):
    def prop(unit, name):
        if name == "Environment":
            return f"UA_EDITION=enterprise WORKOS_ORGANIZATION_ID={ORG} UA_HOME=ua-rel"
        if name == "WorkingDirectory":
            return workdir
        return ""

    monkeypatch.setattr(doctor, "_unit_property", prop)


def test_relative_hub_home_resolves_against_the_unit_working_directory(home, monkeypatch, tmp_path):
    hub_wd, caller_wd = tmp_path / "hub-wd", tmp_path / "caller-wd"
    (hub_wd / "ua-rel").mkdir(parents=True)
    (caller_wd / "ua-rel").mkdir(parents=True)
    _relative_home_hub(monkeypatch, tmp_path, str(hub_wd))
    monkeypatch.chdir(caller_wd)
    _beacon(hub_wd / "ua-rel", 7171, None)
    _beacon(caller_wd / "ua-rel", 7272, ORG)

    row = _row()

    assert row["status"] == "DEGRADED", row
    assert "pid 7171" in row["detail"] and str(hub_wd / "ua-rel") in row["detail"]


def test_relative_hub_home_with_unknown_working_directory_is_unknown(home, monkeypatch, tmp_path):
    _relative_home_hub(monkeypatch, tmp_path, None)
    monkeypatch.chdir(tmp_path)

    row = _row()

    assert row["status"] == "UNKNOWN", row
    assert "working directory" in row["detail"]


def _mac_hub_plist(monkeypatch, tmp_path, workdir):
    """A Darwin hub: no systemd, a LaunchAgent plist setting a relative UA_HOME."""
    import plistlib
    import sys

    mac_home = tmp_path / "mac-home"
    agents = mac_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = {
        "Label": "com.unified-agent.hub",
        "ProgramArguments": ["/usr/bin/true"],
        "EnvironmentVariables": {
            "UA_EDITION": "enterprise",
            "WORKOS_ORGANIZATION_ID": ORG,
            "UA_HOME": "ua-rel",
        },
    }
    if workdir is not None:
        plist["WorkingDirectory"] = workdir
    (agents / "com.unified-agent.hub.plist").write_bytes(plistlib.dumps(plist))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(doctor, "_mac_home", lambda: mac_home)
    monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: None)


def test_relative_hub_home_on_darwin_resolves_against_the_plist_working_directory(
    home, monkeypatch, tmp_path
):
    # #1153: launchd has no systemd property; the plist's WorkingDirectory is the base.
    hub_wd, caller_wd = tmp_path / "hub-wd", tmp_path / "caller-wd"
    (hub_wd / "ua-rel").mkdir(parents=True)
    (caller_wd / "ua-rel").mkdir(parents=True)
    _mac_hub_plist(monkeypatch, tmp_path, str(hub_wd))
    monkeypatch.chdir(caller_wd)
    _beacon(hub_wd / "ua-rel", 7171, None)
    _beacon(caller_wd / "ua-rel", 7272, ORG)

    row = _row()

    assert row["status"] == "DEGRADED", row
    assert "pid 7171" in row["detail"] and str(hub_wd / "ua-rel") in row["detail"]


def test_relative_hub_home_on_darwin_without_working_directory_uses_launchd_root(
    home, monkeypatch, tmp_path
):
    # launchd starts a job without WorkingDirectory in /.
    _mac_hub_plist(monkeypatch, tmp_path, None)
    monkeypatch.chdir(tmp_path)

    row = _row()

    assert "working directory" not in row["detail"], row
    assert str(Path("/") / "ua-rel") in row["detail"]


def test_relative_hub_home_on_darwin_with_relative_plist_working_directory_is_unknown(
    home, monkeypatch, tmp_path
):
    _mac_hub_plist(monkeypatch, tmp_path, "relative-wd")
    monkeypatch.chdir(tmp_path)

    row = _row()

    assert row["status"] == "UNKNOWN", row
    assert "working directory" in row["detail"]


def test_beacons_never_touch_a_patched_global_clock(home, monkeypatch):
    """A test that swaps time.monotonic for a finite clock must not see it
    drained by the beacon written inside storage.connect() (CI, #2010)."""
    import time

    def patched():
        raise AssertionError("beacon code read the patched global clock")

    own = home / edition.BEACON_DIRNAME / f"{os.getpid()}.json"
    own.unlink()
    monkeypatch.setattr(edition, "_beacons_written", {})
    monkeypatch.setattr(time, "monotonic", patched)
    monkeypatch.setattr(time, "sleep", patched)
    edition.record_tenant_beacon(home)

    assert own.is_file()


# ------------------------------------------- prune vs. PID reuse (#2018)


def _expired_beacon(home, pid):
    _beacon(home, pid, None)
    path = home / edition.BEACON_DIRNAME / f"{pid}.json"
    old = path.stat().st_mtime - edition.BEACON_RETENTION_S - 60
    os.utime(path, (old, old))
    return path


def _publish(path, org_id):
    """What _write_tenant_beacon does: a new file swapped in by os.replace."""
    tmp = path.with_name(f".{path.stem}.fixture.tmp")
    tmp.write_text(
        json.dumps(
            {
                "pid": int(path.stem),
                "process": "reused-pid",
                "edition": "enterprise",
                "org_id": org_id,
                "seen_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
    )
    os.replace(tmp, path)


def _no_tombstones(directory):
    return [p.name for p in directory.iterdir() if p.name.startswith(".")] == []


def test_an_expired_beacon_is_pruned(home):
    path = _expired_beacon(home, 4300)
    edition._prune_tenant_beacons(path.parent)
    assert not path.exists()
    assert _no_tombstones(path.parent)


def test_prune_keeps_a_beacon_republished_after_its_expiry_check(home, monkeypatch):
    """A process reusing the PID publishes between the pruner's stat and its
    removal; the replacement must survive (task #2018)."""
    path = _expired_beacon(home, 4301)
    real_stat = Path.stat
    fired = []

    def stat_then_race(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == path and not fired:
            fired.append(True)
            _publish(path, ORG)
        return result

    monkeypatch.setattr(Path, "stat", stat_then_race)
    edition._prune_tenant_beacons(path.parent)
    monkeypatch.undo()

    assert fired
    assert json.loads(path.read_text())["org_id"] == ORG
    assert _no_tombstones(path.parent)


def test_restoring_a_moved_beacon_never_clobbers_a_newer_one(home, monkeypatch):
    """Two publishes race the prune: the newest beacon is the one kept."""
    path = _expired_beacon(home, 4302)
    real_stat, real_link = Path.stat, os.link
    fired = []

    def stat_then_race(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == path and not fired:
            fired.append(True)
            _publish(path, "org_first")
        return result

    def newer_then_link(src, dst, *args, **kwargs):
        _publish(path, "org_newest")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_then_race)
    monkeypatch.setattr(os, "link", newer_then_link)
    edition._prune_tenant_beacons(path.parent)
    monkeypatch.undo()

    assert fired
    assert json.loads(path.read_text())["org_id"] == "org_newest"
    assert _no_tombstones(path.parent)


def test_a_tombstone_left_by_a_crashed_pruner_is_removed(home):
    path = _expired_beacon(home, 4303)
    tomb = path.with_name(f".{path.stem}.dead.prune")
    os.rename(path, tomb)
    edition._prune_tenant_beacons(path.parent)
    assert not tomb.exists()


@pytest.mark.parametrize("failure", [errno.EPERM, errno.ENOTSUP])
@pytest.mark.parametrize("newer", [False, True])
def test_restore_without_hardlinks_preserves_newest_beacon(home, monkeypatch, failure, newer):
    path = _expired_beacon(home, 4310)
    real_stat = Path.stat
    fired = []

    def stat_then_publish(self, *args, **kwargs):
        seen = real_stat(self, *args, **kwargs)
        if self == path and not fired:
            fired.append(True)
            _publish(path, "org_first")
        return seen

    def unsupported_link(src, dst, *args, **kwargs):
        if newer:
            _publish(path, "org_newest")
        raise OSError(failure, "hard links unavailable")

    monkeypatch.setattr(Path, "stat", stat_then_publish)
    monkeypatch.setattr(os, "link", unsupported_link)
    edition._prune_tenant_beacons(path.parent)
    monkeypatch.undo()

    assert json.loads(path.read_text())["org_id"] == ("org_newest" if newer else "org_first")
    assert _no_tombstones(path.parent)


def test_exclusive_restore_never_overwrites_a_concurrent_publication(home, monkeypatch):
    path = _expired_beacon(home, 4311)
    tomb = path.with_name(".4311.restore.prune")
    os.rename(path, tomb)
    real_rename = edition._rename_tenant_beacon_exclusive

    def unsupported_link(*args, **kwargs):
        raise OSError(errno.EPERM, "hard links unavailable")

    def publish_then_rename(source, target):
        _publish(path, "org_newest")
        return real_rename(source, target)

    monkeypatch.setattr(os, "link", unsupported_link)
    monkeypatch.setattr(edition, "_rename_tenant_beacon_exclusive", publish_then_rename)
    edition._restore_tenant_beacon(tomb, path)
    assert json.loads(path.read_text())["org_id"] == "org_newest"


@pytest.mark.parametrize("failure", [errno.ENOSPC, errno.ENOTSUP])
def test_failed_atomic_restore_preserves_complete_tomb_for_retry(home, monkeypatch, failure):
    path = _expired_beacon(home, 4312)
    original = path.read_bytes()
    tomb = path.with_name(".4312.restore.prune")
    os.rename(path, tomb)

    def unsupported_link(*args, **kwargs):
        raise OSError(errno.EPERM, "hard links unavailable")

    def failed_rename(*args, **kwargs):
        raise OSError(failure, "restore unavailable")

    monkeypatch.setattr(os, "link", unsupported_link)
    with monkeypatch.context() as limited:
        limited.setattr(edition, "_rename_tenant_beacon_exclusive", failed_rename)
        with pytest.raises(OSError):
            edition._restore_tenant_beacon(tomb, path)
    assert not path.exists()
    assert tomb.read_bytes() == original
    edition._restore_tenant_beacon(tomb, path)
    assert path.read_bytes() == original
    assert not tomb.exists()
