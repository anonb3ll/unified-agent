import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import kill_switch_rehearsal  # noqa: E402


class TestKillSwitchRehearsal:
    def test_every_check_passes(self):
        """Regression guard: if the S5 kill switch (task #373 3.3) ever stops
        actually refusing a killed/blocked cycle's next dispatch, or the
        budget-capped timeout stops being wired in, this is the rehearsal
        that's supposed to catch it — see
        docs/s5-bounded-autonomy-acceptance-gates.md MUST 3-4."""
        report = kill_switch_rehearsal.run_rehearsal()
        failed = [c for c in report["checks"] if not c["ok"]]
        assert failed == []
        assert report["ok"] is True

    def test_report_shape_is_stable(self):
        report = kill_switch_rehearsal.run_rehearsal()
        assert report["gate"] == "S5/G5"
        assert report["task"] == 373
        assert report["summary"]["total"] == len(report["checks"])
        assert report["summary"]["passed"] == report["summary"]["total"]
        assert "generated_at" in report

    def test_rehearsal_leaves_no_trace_on_the_real_environment(self, isolated_env):
        """Runs inside isolated_env's own tmp UA_HOME so a bug that made the
        script skip its internal UA_HOME-swap couldn't slip through by
        accident writing into whatever real state a dev machine has."""
        import os

        from unified_agent import config

        outer_home = os.environ.get("UA_HOME")
        report = kill_switch_rehearsal.run_rehearsal()
        assert report["ok"] is True
        assert os.environ.get("UA_HOME") == outer_home
        assert config.data_home() == Path(outer_home).expanduser()
