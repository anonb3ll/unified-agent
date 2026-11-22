import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import gate_e_rollback_rehearsal as gate_e  # noqa: E402


class TestGateERehearsal:
    def test_every_check_passes(self):
        """Regression guard: if Gate C's import/rollback mechanism ever breaks,
        this is the rehearsal that's supposed to catch it before a real
        cutover does."""
        report = gate_e.run_rehearsal()
        failed = [c for c in report["checks"] if not c["ok"]]
        assert failed == []
        assert report["ok"] is True

    def test_report_shape_is_stable(self):
        """The report is read by a human (Operator) as cutover evidence, not
        just asserted on by CI -- keep its shape predictable."""
        report = gate_e.run_rehearsal()
        assert report["gate"] == "E"
        assert report["task"] == 285
        assert report["summary"]["total"] == len(report["checks"])
        assert report["summary"]["passed"] == report["summary"]["total"]
        assert "generated_at" in report

    def test_rehearsal_leaves_no_trace_on_the_real_environment(self, isolated_env, monkeypatch):
        """Runs inside isolated_env's own tmp UA_HOME/UA_WORKSPACE so a bug that
        made the script skip its internal UA_HOME-swap couldn't slip through
        by accident writing into whatever real state a dev machine has."""
        import os

        from unified_agent import config

        outer_home = os.environ.get("UA_HOME")
        report = gate_e.run_rehearsal()
        assert report["ok"] is True
        # The rehearsal's own tempdir is gone; UA_HOME as this test's fixture
        # set it is untouched (the script only ever mutates it transiently
        # inside its own TemporaryDirectory context).
        assert os.environ.get("UA_HOME") == outer_home
        assert config.data_home() == Path(outer_home).expanduser()
