"""Host registry aggregation for the Devices pane (task #798)."""

import asyncio
from datetime import UTC, datetime, timedelta

from unified_agent import config, host_registry, registry, storage
from unified_agent.tools.doctor import system_doctor
from unified_agent.tools.registry import agent_context, agent_heartbeat, agent_list


def _by_host(payload: dict) -> dict:
    return {row["host"]: row for row in payload["hosts"]}


class TestHostIdForAgent:
    def test_measured_hostname_wins(self):
        assert host_registry.host_id_for_agent("Desktop_Agent4", "DesktopHost") == "DesktopHost"

    def test_identity_map_roles(self):
        assert host_registry.host_id_for_agent("Desktop_Agent4", None) == "desktop"
        assert host_registry.host_id_for_agent("Laptop_Agent1", None) == "laptop"
        assert host_registry.host_id_for_agent("server_agent3", None) == "hub"

    def test_unknown_label_is_unmapped(self):
        assert host_registry.host_id_for_agent("GrokBot", None) is None


class TestDoctorFreshness:
    def test_missing_when_no_timestamp(self):
        assert host_registry.doctor_freshness(None) == "missing"

    def test_live_when_recent(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_STATUS_THRESHOLDS", "120,900,86400")
        as_of = storage.now()
        assert host_registry.doctor_freshness(as_of) == "live"

    def test_stale_when_old(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_STATUS_THRESHOLDS", "120,900,86400")
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
        assert host_registry.doctor_freshness(old) == "stale"


class TestSnapshotAndAggregation:
    def test_local_doctor_snapshot_is_live_not_guessed(self, monkeypatch):
        monkeypatch.setenv("UA_SOURCE_HOST", "testhub")
        report = system_doctor()
        assert report["host"] == "testhub"
        assert report["as_of"]
        assert "hosts" in report
        row = _by_host(report)["testhub"]
        assert row["doctor_freshness"] == "live"
        assert row["doctor"] is not None
        assert row["doctor"]["summary"] == report["summary"]
        assert row["executor"]["reported"] is True
        assert row["worktrees"]["reported"] is True
        assert row["as_of"]
        assert row["staleness"] in ("online", "idle")

    def test_mcp_identity_group_does_not_invent_a_host(self, monkeypatch):
        monkeypatch.setenv("UA_SOURCE_HOST", "testhub")
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        registry.touch_agent("Desktop_Agent4", via="hub_request", force=True)
        hosts = _by_host(host_registry.fleet_payload())
        assert "desktop" not in hosts
        assert "Desktop_Agent4" not in hosts

    def test_missing_doctor_does_not_invent_a_laptop_host(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        registry.touch_agent("Laptop_Agent1", via="hub_request", force=True)
        hosts = _by_host(host_registry.fleet_payload())
        assert "laptop" not in hosts
        assert "Laptop_Agent1" not in hosts

    def test_heartbeat_host_and_doctor_snapshot(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        token = config.agent_identity_var.set("Desktop_agent3")
        try:
            agent_heartbeat(
                host="vps-1",
                version="1.2.3",
                doctor={
                    "checks": [
                        {
                            "capability": "hub_service",
                            "status": "LIVE",
                            "detail": "active",
                            "gate": "unit",
                        }
                    ],
                    "summary": {"live": 1, "dormant": 0, "misconfigured": 0, "unknown": 0},
                },
            )
        finally:
            config.agent_identity_var.reset(token)
        row = _by_host(host_registry.fleet_payload())["vps-1"]
        assert row["version"] == "1.2.3"
        assert row["doctor_freshness"] == "live"
        assert row["doctor"]["summary"]["live"] == 1
        assert row["executor"]["reported"] is True

    def test_server_agent_binding_folds_into_local_hub_row(self, monkeypatch):
        monkeypatch.setenv("UA_SOURCE_HOST", "testhub")
        monkeypatch.setenv("UA_AGENT_HOSTS", "server_agent4:testhub")
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        host_registry.record_local_doctor_snapshot(
            {"checks": [], "summary": {"live": 0, "dormant": 0, "misconfigured": 0, "unknown": 0}}
        )
        registry.touch_agent("server_agent4", via="hub_request", force=True)
        payload = host_registry.fleet_payload()
        hosts = _by_host(payload)
        assert "hub" not in hosts
        assert "testhub" in hosts
        labels = {agent["label"] for agent in hosts["testhub"]["agents"]}
        assert "server_agent4" in labels


def _insert_host_report(host: str, *, doctor: bool = False, via: str = "mcp_http_session") -> None:
    """Plant a historical host_reports row without going through the write guard."""
    ts = storage.now()
    doctor_as_of = ts if doctor else None
    doctor_json = '{"checks": [], "summary": {}}' if doctor else None
    conn = storage.connect()
    try:
        conn.execute(
            """
            INSERT INTO host_reports
              (host, last_seen, last_seen_via, version, doctor_as_of, doctor_json, updated_at)
            VALUES (?, ?, ?, '0', ?, ?, ?)
            """,
            (host, ts, via, doctor_as_of, doctor_json, ts),
        )
        conn.commit()
    finally:
        conn.close()


class TestRosterHygiene:
    """Host rows are doctor snapshots or ua-nodes. Agent rows are roster or ua-node.

    Attribution strings stay in the agents / host_reports tables. Fleet views
    omit them. Nothing here rewrites historical events.
    """

    def test_agent_label_stored_as_host_is_not_a_host_row(self, monkeypatch):
        monkeypatch.setenv("UA_SOURCE_HOST", "testhub")
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:Desktop_Agent4")
        _insert_host_report("Desktop_Agent1")
        _insert_host_report("Operator")
        _insert_host_report("Claude Code (Omnigent spike)")
        _insert_host_report("testhub", doctor=True)
        registry.touch_agent("Desktop_Agent1", via="hub_request", force=True)

        hosts = _by_host(host_registry.fleet_payload())
        assert set(hosts) == {"testhub"}
        assert hosts["testhub"]["doctor_freshness"] == "live"

        conn = storage.connect()
        try:
            kept = {row["host"] for row in conn.execute("SELECT host FROM host_reports").fetchall()}
        finally:
            conn.close()
        assert "Desktop_Agent1" in kept
        assert "Operator" in kept

    def test_identity_prefix_does_not_invent_a_host(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent4:Laptop_Agent1")
        registry.reset_throttle()
        registry.touch_agent("Desktop_Agent4", via="hub_request", force=True)
        registry.touch_agent("Laptop_Agent1", via="hub_request", force=True)
        hosts = _by_host(host_registry.fleet_payload())
        assert "desktop" not in hosts
        assert "laptop" not in hosts
        assert "unattributed" not in hosts
        assert "Desktop_Agent4" not in hosts
        assert "Laptop_Agent1" not in hosts

    def test_heartbeat_only_hostname_is_not_a_host_until_doctor_or_node(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        assert host_registry.touch_host_seen("vps-heartbeat-only", via="heartbeat_tool") is True
        assert "vps-heartbeat-only" not in _by_host(host_registry.fleet_payload())
        host_registry.record_doctor_snapshot(
            "vps-1",
            {"checks": [], "summary": {"live": 0, "dormant": 0, "misconfigured": 0, "unknown": 0}},
        )
        assert "vps-1" in _by_host(host_registry.fleet_payload())

    def test_new_writes_refuse_agent_labels_as_hosts(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:gcp_agent5")
        assert host_registry.touch_host_seen("Desktop_Agent1", via="mcp_http_session") is False
        assert host_registry.touch_host_seen("gcp_agent5", via="mcp_http_session") is False
        assert (
            host_registry.touch_host_seen(
                "Operator (authorized in chat via Desktop_Agent5)", via="mcp_http_session"
            )
            is False
        )
        conn = storage.connect()
        try:
            rows = conn.execute("SELECT host FROM host_reports").fetchall()
        finally:
            conn.close()
        assert rows == []

    def test_closed_roster_agent_list_drops_attributions(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:gcp_agent5")
        registry.reset_throttle()
        for label in (
            "Desktop_Agent1",
            "gcp_agent5",
            "Operator",
            "Desktop_Person",
            "manual-test",
            "pi_worker",
            "unified-agent-cron",
            "weekly-export-cron",
            "Desktop_Agent1 (steward)",
            "Claude Code (Omnigent spike)",
        ):
            registry.touch_agent(label, via="agent_event", force=True)

        listed = agent_list()
        labels = {agent["label"] for agent in listed["agents"]}
        assert labels == {"Desktop_Agent1", "gcp_agent5"}
        assert sum(listed["counts"].values()) == 2
        assert "Operator" not in labels
        assert "manual-test" not in labels
        # Historical registry rows stay. Fleet view is a filter, not a rewrite.
        assert registry.get_agent("Operator")["label"] == "Operator"
        assert registry.get_agent("manual-test") is not None
        registry.touch_agent("desktop_agent1", via="hub_request", force=True)
        again = {agent["label"] for agent in agent_list()["agents"]}
        assert "desktop_agent1" not in again
        assert config.is_known_agent_label("desktop_agent1") is False

    def test_open_roster_keeps_catalog_and_does_not_close_authorization(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        registry.touch_agent("Desktop_Agent4", via="hub_request", force=True)
        registry.touch_agent("Local_Worker", via="hub_request", force=True)
        registry.touch_agent("manual-test", via="agent_event", force=True)
        registry.touch_agent("Operator", via="agent_event", force=True)

        listed = agent_list()
        labels = {agent["label"] for agent in listed["agents"]}
        assert "Desktop_Agent4" in labels
        assert "Local_Worker" not in labels
        assert "manual-test" not in labels
        assert "Operator" not in labels
        # Open roster still accepts a custom label for authorization.
        assert config.agent_labels() == set()
        assert config.is_known_agent_label("Local_Worker") is True
        assert config.is_known_agent_label("manual-test") is True

    def test_registered_node_agent_is_a_fleet_agent_and_a_host(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        registry.touch_agent("pi_worker", via="hub_request", host="desk-01", force=True)

        def nodes():
            return {
                "desk-01": {
                    "agent": "pi_worker",
                    "last_heartbeat": storage.now(),
                    "heartbeat_floor": None,
                },
                "desk-02": {
                    "agent": "field-sensor",
                    "last_heartbeat": storage.now(),
                    "heartbeat_floor": None,
                },
            }

        monkeypatch.setattr("unified_agent.node_registry.list_nodes", nodes)
        listed = agent_list()
        labels = {agent["label"] for agent in listed["agents"]}
        assert "pi_worker" in labels
        assert "field-sensor" in labels
        assert registry.get_agent("field-sensor") is None
        hosts = _by_host(host_registry.fleet_payload())
        assert "desk-01" in hosts
        assert hosts["desk-01"]["host_source"] == "node"
        assert "pi_worker" in {agent["label"] for agent in hosts["desk-01"]["agents"]}
        assert "pi_worker" not in hosts
        # A node-bound agent with no registry row still belongs to its node's
        # host row: agent_list synthesizes it, so Devices must show it too.
        assert "field-sensor" in {agent["label"] for agent in hosts["desk-02"]["agents"]}
        sensor = next(agent for agent in listed["agents"] if agent["label"] == "field-sensor")
        assert sensor["status"] == "online"
        assert sensor["last_seen"]
        assert registry.get_agent("field-sensor") is None
        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            peers = {peer["label"]: peer for peer in agent_context()["peers"]}
        finally:
            config.agent_identity_var.reset(token)
        assert peers["field-sensor"]["status"] == "online"
        assert registry.get_agent("field-sensor") is None

    def test_future_dated_node_heartbeat_does_not_place_an_agent_as_live(self, monkeypatch):
        """A node heartbeat an hour ahead is the last known node, not a live one (#1997)."""
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        now = datetime.now(UTC)
        beats = {
            "desk-live": now.isoformat(),
            "desk-future": (now + timedelta(hours=1)).isoformat(),
        }

        def nodes():
            return {
                "desk-live": {
                    "agent": "live-sensor",
                    "last_heartbeat": beats["desk-live"],
                    "heartbeat_floor": None,
                },
                "desk-future": {
                    "agent": "future-sensor",
                    "last_heartbeat": beats["desk-future"],
                    "heartbeat_floor": None,
                },
            }

        monkeypatch.setattr("unified_agent.node_registry.list_nodes", nodes)
        hosts = _by_host(host_registry.fleet_payload(now=now))
        live = {a["label"]: a for a in hosts["desk-live"]["agents"]}
        future = {a["label"]: a for a in hosts["desk-future"]["agents"]}
        assert live["live-sensor"]["host_source"] == "node"
        assert future["future-sensor"]["host_source"] == "node_last"

    def test_future_dated_node_heartbeat_shows_host_and_agent_offline(self, monkeypatch):
        """Devices status for a node-derived row follows the routing rule (#1997).

        Before the fix the host's staleness and the node-only agent's status
        came from ``registry.derive_status``, which reads every future
        timestamp as online, so Devices said online while routing said
        ``node_offline``.
        """
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        now = datetime.now(UTC)
        beats = {
            "desk-live": now.isoformat(),
            "desk-future": (now + timedelta(hours=1)).isoformat(),
        }

        def nodes():
            return {
                "desk-live": {
                    "agent": "live-sensor",
                    "last_heartbeat": beats["desk-live"],
                    "heartbeat_floor": None,
                },
                "desk-future": {
                    "agent": "future-sensor",
                    "last_heartbeat": beats["desk-future"],
                    "heartbeat_floor": None,
                },
            }

        monkeypatch.setattr("unified_agent.node_registry.list_nodes", nodes)
        hosts = _by_host(host_registry.fleet_payload(now=now))
        assert hosts["desk-live"]["staleness"] == "online"
        assert hosts["desk-live"]["last_seen_status"] == "online"
        assert hosts["desk-future"]["staleness"] == "offline"
        assert hosts["desk-future"]["last_seen_status"] == "offline"
        live = {a["label"]: a for a in hosts["desk-live"]["agents"]}
        future = {a["label"]: a for a in hosts["desk-future"]["agents"]}
        assert live["live-sensor"]["status"] == "online"
        assert future["future-sensor"]["status"] == "offline"
        peers = {a["label"]: a for a in agent_list()["agents"]}
        assert peers["future-sensor"]["status"] == "offline"

    def test_registered_node_future_heartbeat_reads_offline_with_a_report_row(self, monkeypatch):
        """Real registration writes a host_reports row; its node-derived status
        must take the shared liveness rule too (#1997, review round 3).

        Before the fix this path graded the row with ``registry.derive_status``,
        so a heartbeat an hour ahead read ``online`` while routing called the
        node offline.
        """
        from unified_agent import node_queue

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        now = datetime.now(UTC)
        node_queue.register("desk-live", identity="live-sensor", now=now)
        node_queue.register("desk-future", identity="future-sensor", now=now + timedelta(hours=1))
        hosts = _by_host(host_registry.fleet_payload(now=now))
        # The rows came from host_reports (the node measured its executor).
        assert hosts["desk-future"]["executor"]["reported"] is True
        assert hosts["desk-future"]["last_seen_via"] == "ua_node"
        assert hosts["desk-live"]["staleness"] == "online"
        assert hosts["desk-live"]["last_seen_status"] == "online"
        assert hosts["desk-future"]["staleness"] == "offline"
        assert hosts["desk-future"]["last_seen_status"] == "offline"
        future = {a["label"]: a for a in hosts["desk-future"]["agents"]}
        assert future["future-sensor"]["status"] == "offline"

    def test_node_row_future_dated_after_a_fresh_node_report_reads_offline(self, monkeypatch):
        """The routing input is ``nodes.last_heartbeat``. A ua_node report row
        mirrors it; when the node row is future-dated the host is not online."""
        from unified_agent import node_queue

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        now = datetime.now(UTC)
        node_queue.register("desk-future", identity="future-sensor", now=now)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE nodes SET last_heartbeat=? WHERE node=?",
                ((now + timedelta(hours=1)).isoformat(timespec="seconds"), "desk-future"),
            )
            conn.commit()
        finally:
            conn.close()
        hosts = _by_host(host_registry.fleet_payload(now=now))
        assert hosts["desk-future"]["staleness"] == "offline"
        assert hosts["desk-future"]["last_seen_status"] == "offline"

    def test_independent_fresh_host_report_keeps_its_own_status(self, monkeypatch):
        """A hub-stamped host report is not node-derived: a fresh one still
        reads online even while the node heartbeat is future-dated."""
        from unified_agent import node_queue

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        now = datetime.now(UTC)
        node_queue.register("desk-future", identity="future-sensor", now=now + timedelta(hours=1))
        assert host_registry.touch_host_seen("desk-future", via="heartbeat_tool")
        hosts = _by_host(host_registry.fleet_payload(now=now))
        assert hosts["desk-future"]["last_seen_via"] == "heartbeat_tool"
        assert hosts["desk-future"]["last_seen_status"] == "online"
        assert hosts["desk-future"]["staleness"] == "online"

    def test_old_doctor_report_does_not_borrow_a_future_node_beat(self, monkeypatch):
        """Staleness falls back to the host's own doctor age, not the bad beat."""
        from unified_agent import node_queue

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        now = datetime.now(UTC)
        host_registry.record_doctor_snapshot(
            "desk-future",
            {"checks": [], "summary": {}},
            as_of=(now - timedelta(minutes=30)).isoformat(timespec="seconds"),
        )
        node_queue.register("desk-future", identity="future-sensor", now=now + timedelta(hours=1))
        hosts = _by_host(host_registry.fleet_payload(now=now))
        assert hosts["desk-future"]["doctor"] is not None
        assert hosts["desk-future"]["last_seen_status"] == "offline"
        # 30 minutes old: the registry grades that "stale", not "online".
        assert hosts["desk-future"]["staleness"] == "stale"

    def test_agent_own_future_heartbeat_keeps_registry_status(self, monkeypatch):
        """Only node-derived status takes the node bound. An agent's own
        last_seen (hub-stamped) keeps derive_status, skew tolerance included."""
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        now = datetime.now(UTC)
        registry.touch_agent("Desktop_Agent1", via="hub_request", force=True)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agents SET last_seen=? WHERE label=?",
                ((now + timedelta(minutes=5)).isoformat(timespec="seconds"), "Desktop_Agent1"),
            )
            conn.commit()
        finally:
            conn.close()
        peers = {a["label"]: a for a in agent_list()["agents"]}
        assert peers["Desktop_Agent1"]["status"] == "online"

    def test_doctor_snapshot_keeps_hostname_that_matches_a_label(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        written = host_registry.record_doctor_snapshot(
            "Desktop_Agent1",
            {"checks": [], "summary": {"live": 0, "dormant": 0, "misconfigured": 0, "unknown": 0}},
        )
        assert written
        row = _by_host(host_registry.fleet_payload())["Desktop_Agent1"]
        assert row["doctor"] is not None
        assert row["doctor_freshness"] == "live"
        assert host_registry.touch_host_seen("Desktop_Agent1", via="mcp_http_session") is False
        # A configured or OS hostname may contain spaces, a person's name, or
        # a roster label. Sentence markers and parentheses are attribution.
        for hostname in (
            "Mac hub",
            "The Mac hub",
            "Operator Mac hub",
            "Desktop_Agent5 Mac hub",
            "Mac hub in Office",
            "Mac hub by the Bay",
            "Workstation",
            "Report Server by the Bay",
            # A role word elsewhere in the name is part of the machine name,
            # not attribution prose (Devin finding on #797).
            "Agent PC by the Bay",
            "Steward Workstation by the Rack",
        ):
            written_host = host_registry.record_doctor_snapshot(
                hostname,
                {
                    "checks": [],
                    "summary": {"live": 0, "dormant": 0, "misconfigured": 0, "unknown": 0},
                },
            )
            assert written_host
        assert (
            host_registry.record_doctor_snapshot(
                "Claude Code (Omnigent spike)",
                {"checks": [], "summary": {}},
            )
            is None
        )
        for sentence in (
            "Operator authorized in chat via Desktop_Agent5",
            "Operator working with Desktop_Agent5",
            "Desktop_Agent2 by Operator",
            "Alice working with Bob",
            "Alice by Bob",
            "Report by the operator",
        ):
            assert (
                host_registry.record_doctor_snapshot(sentence, {"checks": [], "summary": {}})
                is None
            )
            _insert_host_report(sentence, doctor=True)
        _insert_host_report("Claude Code (Omnigent spike)", doctor=True)
        hosts = _by_host(host_registry.fleet_payload())
        assert "Claude Code (Omnigent spike)" not in hosts
        assert "Operator authorized in chat via Desktop_Agent5" not in hosts
        assert "Operator working with Desktop_Agent5" not in hosts
        assert "Desktop_Agent2 by Operator" not in hosts
        assert "Alice working with Bob" not in hosts
        assert "Alice by Bob" not in hosts
        assert "Report by the operator" not in hosts
        assert "Mac hub" in hosts
        assert "Operator Mac hub" in hosts
        assert "Desktop_Agent5 Mac hub" in hosts
        assert "The Mac hub" in hosts
        assert "Mac hub in Office" in hosts
        assert "Mac hub by the Bay" in hosts
        assert "Workstation" in hosts
        assert "Report Server by the Bay" in hosts
        assert "Agent PC by the Bay" in hosts
        assert "Steward Workstation by the Rack" in hosts

    def test_agents_page_uses_the_roster_filter(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        registry.reset_throttle()
        registry.touch_agent("Desktop_Agent1", via="hub_request", force=True)
        registry.touch_agent("manual-test", via="agent_event", force=True)
        from unified_agent.webui.api import agents_payload

        body = asyncio.run(agents_payload())
        labels = {agent["label"] for agent in body["agents"]}
        assert labels == {"Desktop_Agent1"}
        assert sum(body["counts"].values()) == 1
        assert registry.get_agent("manual-test") is not None

    def test_open_roster_custom_agent_stays_a_peer_not_a_fleet_row(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        registry.touch_agent("Local_Worker", via="hub_request", force=True)
        registry.touch_agent("manual-test", via="agent_event", force=True)
        registry.touch_agent("Operator", via="agent_event", force=True)
        listed = {agent["label"] for agent in agent_list()["agents"]}
        assert "Local_Worker" not in listed
        assert "manual-test" not in listed
        token = config.agent_identity_var.set("Desktop_Agent4")
        try:
            peers = {peer["label"] for peer in agent_context()["peers"]}
        finally:
            config.agent_identity_var.reset(token)
        assert "Local_Worker" in peers
        assert "manual-test" in peers
        assert "Operator" not in peers
        assert config.is_known_agent_label("Local_Worker") is True

    def test_explicit_roster_membership_beats_unregistered_exemption(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:unified-agent-cron")
        registry.reset_throttle()
        registry.touch_agent("unified-agent-cron", via="agent_event", force=True)
        registry.touch_agent("weekly-export-cron", via="agent_event", force=True)
        labels = {agent["label"] for agent in agent_list()["agents"]}
        assert "unified-agent-cron" in labels
        assert "weekly-export-cron" not in labels

    def test_open_roster_lists_cloud_Agent1_as_a_fleet_agent(self, monkeypatch):
        """Task #1556 review: cloud_agent1 is an office member, so an open-roster hub lists it."""
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        registry.touch_agent("cloud_agent1", via="hub_request", force=True)
        assert config.is_fleet_roster_label("cloud_agent1") is True
        assert "cloud_agent1" in {agent["label"] for agent in agent_list()["agents"]}

    def test_open_roster_lowercase_human_is_not_a_fleet_agent(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        registry.touch_agent("desktop_person", via="hub_request", force=True)
        registry.touch_agent("Desktop_Person", via="hub_request", force=True)
        labels = {agent["label"] for agent in agent_list()["agents"]}
        assert "desktop_person" not in labels
        assert "Desktop_Person" not in labels
        token = config.agent_identity_var.set("Desktop_Agent4")
        try:
            peers = {peer["label"] for peer in agent_context()["peers"]}
        finally:
            config.agent_identity_var.reset(token)
        assert "desktop_person" not in peers
        assert "Desktop_Person" not in peers
