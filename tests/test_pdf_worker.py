"""Tests for unified_agent.pdf_worker — the confined PDF extraction subprocess.

No mock of pypdf itself here: pdf_worker spawns a real subprocess, so the
useful boundary to test is the subprocess's observable behavior (limits,
timeout, fail-closed, truncation), plus the generated SBPL profile text. A
real end-to-end extraction under the macOS seatbelt is exercised too, but
skipped unless running on darwin with /usr/bin/sandbox-exec present (hub
task #1523 requires this to be verified live on the Mac, not just mocked).
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from unified_agent import pdf_worker

pypdf = pytest.importorskip("pypdf")


def _real_pdf(pages: int = 1, text: str = "Hello confined world") -> bytes:
    """A real, parseable single/multi-page PDF with extractable text."""
    fpdf = pytest.importorskip("fpdf")
    pdf = fpdf.FPDF()
    for i in range(pages):
        pdf.add_page()
        pdf.set_font("Helvetica", size=14)
        pdf.cell(0, 10, f"{text} page {i}")
    return bytes(pdf.output())


def _blank_pdf() -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


# --- basic extraction -------------------------------------------------


class TestBasicExtraction:
    def test_real_pdf_extracts_text(self):
        body = _real_pdf(text="MARKER123")
        result = pdf_worker.extract_pdf_text_confined(body, timeout_s=5)
        assert result.ok
        assert "MARKER123" in result.text
        assert result.page_capped is False
        assert result.char_capped is False

    def test_blank_pdf_reports_empty(self):
        body = _blank_pdf()
        result = pdf_worker.extract_pdf_text_confined(body, timeout_s=5)
        assert result.status == "empty"
        assert result.text == ""

    def test_garbage_bytes_report_unreadable(self):
        result = pdf_worker.extract_pdf_text_confined(b"not a pdf at all", timeout_s=5)
        assert result.status == "unreadable"

    def test_missing_pypdf_reports_missing(self, monkeypatch):
        """Simulate an interpreter without pypdf installed."""
        original = pdf_worker._WORKER_SOURCE
        patched = original.replace(
            "from pypdf import PdfReader",
            "raise ImportError('pypdf')",
        )
        monkeypatch.setattr(pdf_worker, "_WORKER_SOURCE", patched)
        result = pdf_worker.extract_pdf_text_confined(_real_pdf(), timeout_s=5)
        assert result.status == "missing"


# --- limits -------------------------------------------------------------


class TestLimits:
    def test_max_input_bytes_rejects_oversized_body_without_spawning(self, monkeypatch):
        spawned = []
        monkeypatch.setattr(
            pdf_worker.subprocess,
            "Popen",
            lambda *a, **k: spawned.append(1) or pytest.fail("should not spawn a worker"),
        )
        result = pdf_worker.extract_pdf_text_confined(b"x" * 100, max_input_bytes=10)
        assert result.status == "oversize"
        assert "100 bytes" in result.detail
        assert not spawned

    def test_max_pages_caps_extraction(self):
        body = _real_pdf(pages=5, text="PAGE")
        result = pdf_worker.extract_pdf_text_confined(body, timeout_s=5, max_pages=2)
        assert result.ok
        assert result.page_capped is True
        # Only the first two pages' text should appear.
        assert result.text.count("PAGE") == 2

    def test_max_output_chars_truncates_and_marks_capped(self):
        body = _real_pdf(text="A" * 500)
        result = pdf_worker.extract_pdf_text_confined(body, timeout_s=5, max_output_chars=20)
        assert result.ok
        assert len(result.text) <= 20
        assert result.char_capped is True

    def test_output_within_json_escaping_slack_still_truncates_cleanly(self):
        """A worker honoring max_output_chars still parses and truncates
        correctly; the parent's budget has headroom for JSON-escaping
        overhead, not just the raw character count."""
        body = _real_pdf(text="A" * 500)
        result = pdf_worker.extract_pdf_text_confined(body, timeout_s=5, max_output_chars=50)
        assert result.ok
        assert len(result.text) <= 50
        assert result.char_capped is True

    def test_worker_lying_past_the_output_budget_fails_safe(self, monkeypatch):
        """The worker's own source enforces max_output_chars — it is not
        attacker-controlled, only the PDF content it parses is. This exercises
        the defensive path for a buggy/misbehaving worker: grossly exceeding
        the parent's stdout budget must never be trusted as "ok", even if the
        truncated bytes happen to still look plausible."""
        # The huge string is built inside the child (not embedded in argv —
        # that would blow the OS argument-list-too-long limit).
        patched = (
            "import json, sys\n"
            "sys.stdout.write(json.dumps({'status': 'ok', 'text': 'Z' * 10_000_000}))\n"
        )
        monkeypatch.setattr(pdf_worker, "_WORKER_SOURCE", patched)
        result = pdf_worker.extract_pdf_text_confined(_real_pdf(), timeout_s=5, max_output_chars=10)
        assert not result.ok
        assert result.status == "unreadable"


# --- timeout / process-group kill ---------------------------------------


def _process_gone(pid: int) -> bool:
    """True once ``pid`` no longer runs (a zombie awaiting its reaper counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def _kill_survivor(pid: int | None) -> None:
    """Kill a recorded descendant a failed test left running, so it cannot
    act after the test (or a later test) has moved on."""
    if pid is None or _process_gone(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class TestTimeout:
    def test_hung_worker_is_killed_within_timeout(self, monkeypatch):
        # Reads stdin (so the parent's write does not block/deadlock) then
        # sleeps well past the deadline.
        monkeypatch.setattr(
            pdf_worker,
            "_WORKER_SOURCE",
            "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(30)\n",
        )
        start = time.monotonic()
        result = pdf_worker.extract_pdf_text_confined(_real_pdf(), timeout_s=1)
        elapsed = time.monotonic() - start
        assert result.status == "timeout"
        # Generous ceiling: the kill path itself waits up to ~2s more.
        assert elapsed < 5

    def test_forked_grandchild_does_not_survive_timeout(self, monkeypatch, tmp_path):
        """The whole process group is killed, not just the direct child."""
        pidfile = tmp_path / "grandchild.pid"
        # Unsandboxed, so the pid the grandchild records is a host pid.
        monkeypatch.setattr(
            pdf_worker,
            "_worker_argv",
            lambda worker_args: (
                [sys.executable, "-P", "-c", pdf_worker._WORKER_SOURCE, *worker_args],
                None,
            ),
        )
        monkeypatch.setattr(
            pdf_worker,
            "_WORKER_SOURCE",
            "import os, sys, time\n"
            "sys.stdin.buffer.read()\n"
            "if os.fork() == 0:\n"
            f"    open({str(pidfile)!r} + '.tmp', 'w').write(str(os.getpid()))\n"
            f"    os.replace({str(pidfile)!r} + '.tmp', {str(pidfile)!r})\n"
            "    time.sleep(10)\n"
            "    os._exit(0)\n"
            "time.sleep(10)\n",
        )
        grandchild = None
        try:
            result = pdf_worker.extract_pdf_text_confined(_real_pdf(), timeout_s=1)
            assert result.status == "timeout"
            # Never vacuous: the grandchild's pid must be on record (a survivor
            # of a leader-only kill may publish it late), and it must be dead.
            deadline = time.monotonic() + 5
            while not pidfile.exists():
                assert time.monotonic() < deadline, "the grandchild never published its pid"
                time.sleep(0.01)
            grandchild = int(pidfile.read_text())
            deadline = time.monotonic() + 5
            while not _process_gone(grandchild):
                assert time.monotonic() < deadline, f"grandchild {grandchild} outlived the kill"
                time.sleep(0.01)
        finally:
            _kill_survivor(grandchild)

    def test_sleeper_holding_stdout_is_killed_after_leader_exits(self, monkeypatch):
        """Devin finding 4132878357: the direct worker forks a descendant
        that holds stdout open, then the LEADER exits — os.getpgid(proc.pid)
        then raises ProcessLookupError because the leader is already gone,
        and the old code left the descendant running as a result.

        Confinement (the macOS seatbelt denies all writes; Linux's bwrap
        gives the worker a private pid namespace and an isolated /tmp) means
        the worker cannot hand its descendant's real, host-visible pid back
        to the test through the filesystem the normal way. This test isolates
        the Python-level fix under test — _kill_process_group using the pid
        captured at spawn time rather than a freshly re-resolved one — by
        bypassing the platform sandbox wrapper (_worker_argv) for this one
        case, so the descendant's pid is directly host-visible and its death
        can be verified with os.kill(pid, 0), not just inferred from timing.
        """
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as f:
            pid_file = f.name
        real_worker_argv = pdf_worker._worker_argv

        def _unsandboxed_argv(worker_args):
            return [sys.executable, "-P", "-c", pdf_worker._WORKER_SOURCE, *worker_args], None

        monkeypatch.setattr(pdf_worker, "_worker_argv", _unsandboxed_argv)
        try:
            monkeypatch.setattr(
                pdf_worker,
                "_WORKER_SOURCE",
                "import os, sys, time\n"
                "sys.stdin.buffer.read()\n"
                "child = os.fork()\n"
                "if child == 0:\n"
                f"    open({pid_file!r}, 'w').write(str(os.getpid()))\n"
                "    time.sleep(30)\n"  # holds the inherited stdout fd open
                "    os._exit(0)\n"
                "else:\n"
                "    os._exit(0)\n",  # leader exits immediately; sleeper survives it
            )
            result = pdf_worker.extract_pdf_text_confined(_real_pdf(), timeout_s=1)
            assert result.status == "timeout"

            sleeper_pid_text = ""
            for _ in range(200):
                sleeper_pid_text = open(pid_file).read().strip()
                if sleeper_pid_text:
                    break
                time.sleep(0.01)
            assert sleeper_pid_text, (
                "sleeper never wrote its pid — test is not exercising the fork path"
            )
            sleeper_pid = int(sleeper_pid_text)

            # The sleeper must actually be dead now, not merely orphaned.
            deadline = time.monotonic() + 2
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(sleeper_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.01)
            assert not alive, f"sleeper pid {sleeper_pid} outlived the leader's exit + timeout kill"
        finally:
            monkeypatch.setattr(pdf_worker, "_worker_argv", real_worker_argv)
            recorded = open(pid_file).read().strip()
            _kill_survivor(int(recorded) if recorded.isdigit() else None)
            os.unlink(pid_file)


# --- fail-closed ----------------------------------------------------------


class TestFailClosed:
    def test_darwin_without_sandbox_exec_fails_closed(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(pdf_worker.os.path, "exists", lambda p: False)
        result = pdf_worker.extract_pdf_text_confined(_real_pdf())
        assert result.status == "confinement"
        assert "sandbox-exec" in result.detail

    def test_unsupported_platform_fails_closed(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        result = pdf_worker.extract_pdf_text_confined(_real_pdf())
        assert result.status == "confinement"
        assert "win32" in result.detail

    def test_non_posix_fails_closed_before_any_spawn(self, monkeypatch):
        spawned = []
        monkeypatch.setattr(pdf_worker.os, "name", "nt")
        monkeypatch.setattr(
            pdf_worker.subprocess,
            "Popen",
            lambda *a, **k: spawned.append(1) or pytest.fail("should not spawn a worker"),
        )
        result = pdf_worker.extract_pdf_text_confined(_real_pdf())
        assert result.status == "confinement"
        assert not spawned

    def test_worker_reports_confinement_when_it_cannot_set_a_memory_limit(self, monkeypatch):
        patched = (
            "import json, sys\n"
            "sys.stdout.write(json.dumps({'status': 'confinement', 'detail': 'no rlimit'}))\n"
        )
        monkeypatch.setattr(pdf_worker, "_WORKER_SOURCE", patched)
        result = pdf_worker.extract_pdf_text_confined(_real_pdf())
        assert result.status == "confinement"
        assert result.detail == "no rlimit"

    def test_a_failed_memory_limit_is_fatal_off_darwin(self, monkeypatch):
        """Linux/other posix: RLIMIT_AS/RLIMIT_DATA are the load-bearing
        confinement, so a platform that cannot set either fails closed."""
        preamble = (
            "import sys\n"
            "sys.platform = 'freebsd13'\n"
            "import resource\n"
            "resource.setrlimit = lambda *a, **k: (_ for _ in ()).throw(OSError('nope'))\n"
        )
        monkeypatch.setattr(pdf_worker, "_WORKER_SOURCE", preamble + pdf_worker._WORKER_SOURCE)
        result = pdf_worker.extract_pdf_text_confined(_real_pdf())
        assert result.status == "confinement"
        assert "address-space" in result.detail

    def test_a_failed_memory_limit_is_best_effort_on_darwin(self, monkeypatch):
        """macOS: RLIMIT_AS/RLIMIT_DATA are verified-broken (setrlimit refuses
        any lower soft limit even though getrlimit reports an unlimited hard
        ceiling); a platform reporting darwin must still run the parser when
        neither rlimit call succeeds — the seatbelt + timeout are the real
        confinement there, not the memory ceiling."""
        preamble = (
            "import sys\n"
            "sys.platform = 'darwin'\n"
            "import resource\n"
            "resource.setrlimit = lambda *a, **k: (_ for _ in ()).throw(OSError('nope'))\n"
        )
        monkeypatch.setattr(pdf_worker, "_WORKER_SOURCE", preamble + pdf_worker._WORKER_SOURCE)
        result = pdf_worker.extract_pdf_text_confined(_real_pdf(text="STILL_RUNS"))
        assert result.ok
        assert "STILL_RUNS" in result.text

    def test_spawn_failure_reports_confinement(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("no such file")

        monkeypatch.setattr(pdf_worker.subprocess, "Popen", _boom)
        result = pdf_worker.extract_pdf_text_confined(_real_pdf())
        assert result.status == "confinement"
        assert "spawn failed" in result.detail


# --- live output ceiling (Devin finding 4132878875) ------------------------


class TestOutputCeiling:
    def test_overproducing_worker_is_cut_off_quickly_not_after_full_buffering(self, monkeypatch):
        """A worker that never stops writing stdout must be killed the
        moment it crosses the live byte ceiling, not once it has finished
        (it never finishes) or once the whole timeout has elapsed. This is
        the difference between _read_stdout_capped and Popen.communicate(),
        which would buffer indefinitely until timeout_s regardless of size."""
        monkeypatch.setattr(
            pdf_worker,
            "_WORKER_SOURCE",
            "import sys\n"
            "sys.stdin.buffer.read()\n"
            "while True:\n"
            "    sys.stdout.write('A' * 65536)\n"
            "    sys.stdout.flush()\n",
        )
        start = time.monotonic()
        result = pdf_worker.extract_pdf_text_confined(
            _real_pdf(), timeout_s=10, max_output_chars=100
        )
        elapsed = time.monotonic() - start
        assert result.status == "unreadable"
        assert "output ceiling" in result.detail
        # Cut off almost immediately, nowhere near the 10s timeout.
        assert elapsed < 3

    def test_stdin_write_thread_does_not_deadlock_a_slow_reader(self):
        """A real (small) PDF still round-trips correctly now that stdin is
        written from a separate thread instead of via Popen.communicate()."""
        result = pdf_worker.extract_pdf_text_confined(_real_pdf(text="THREAD_OK"), timeout_s=5)
        assert result.ok
        assert "THREAD_OK" in result.text


# --- Linux bwrap confinement (Devin finding 4132879144) --------------------


class TestLinuxBwrapConfinement:
    def test_argv_is_wrapped_in_bwrap_with_unshare_all_and_no_home_bind(self, monkeypatch):
        # This tests argv construction only, independent of whether bwrap is
        # actually installed on the machine running the suite (e.g. macOS,
        # which has no bwrap at all) — the preflight itself is covered by
        # its own dedicated tests below.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pdf_worker, "_bwrap_preflight", lambda roots: None)
        argv, failure = pdf_worker._worker_argv(["50", "1000", "1000000", "5"])
        assert failure is None
        assert argv[0] == pdf_worker._BWRAP
        assert "--unshare-all" in argv
        assert "--die-with-parent" in argv
        assert "--clearenv" in argv
        home = os.path.expanduser("~")
        # No argument binds $HOME itself (a narrow root under it is fine).
        assert home not in argv
        # The worker command itself must be present after the "--" separator.
        assert "--" in argv
        assert any(a.endswith("-c") for a in argv)

    def test_bwrap_missing_fails_closed(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pdf_worker.os.path, "exists", lambda p: False)
        result = pdf_worker.extract_pdf_text_confined(_real_pdf())
        assert result.status == "confinement"
        assert "bwrap" in result.detail

    def test_bwrap_preflight_failure_fails_closed(self, monkeypatch):
        """bwrap exists but cannot actually run the sandbox (e.g. unprivileged
        userns disabled) — the preflight catches this before ever handing the
        worker any PDF bytes."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            pdf_worker,
            "_bwrap_preflight",
            lambda roots: (
                "bwrap preflight failed: exit 1 (bwrap: setting up uid map: Permission denied)"
            ),
        )
        result = pdf_worker.extract_pdf_text_confined(_real_pdf())
        assert result.status == "confinement"
        assert "preflight" in result.detail

    def test_lib_dirs_bound_unconditionally_even_off_usr(self):
        """Verified locally: a fully self-contained interpreter build still
        dynamically links against the host's libc under /usr/lib, so the
        bind must not be gated on the interpreter's own install path."""
        argv = pdf_worker._bwrap_wrap(["/some/fake/python", "-c", "pass"], ["/fake/root"])
        existing_lib_dirs = [d for d in pdf_worker._LINUX_LIB_DIRS if os.path.isdir(d)]
        assert existing_lib_dirs, "test host has none of the expected lib dirs"
        for lib_dir in existing_lib_dirs:
            assert lib_dir in argv


@pytest.mark.skipif(
    sys.platform != "linux" or not os.path.exists("/usr/bin/bwrap"),
    reason="requires Linux with /usr/bin/bwrap",
)
class TestLinuxRealBwrapExtraction:
    def test_real_extraction_under_bwrap(self):
        body = _real_pdf(text="BWRAP_MARKER")
        result = pdf_worker.extract_pdf_text_confined(body, timeout_s=10)
        assert result.ok, result
        assert "BWRAP_MARKER" in result.text

    def test_worker_argv_is_wrapped_in_bwrap(self):
        argv, failure = pdf_worker._worker_argv(["50", "1000", "1000000", "5"])
        assert failure is None
        assert argv[0] == "/usr/bin/bwrap"

    def test_bwrap_sandbox_denies_network_and_ssh_read(self):
        """Real proof, using pdf_worker's own argv/roots construction (not a
        hand-rolled profile): a socket connect fails, and reading ~/.ssh
        (structurally absent from the sandbox's mount namespace, since only
        the narrow computed roots are bound) fails too."""
        argv, failure = pdf_worker._worker_argv(["50", "1000", "268435456", "5"])
        assert failure is None
        probe = (
            "import socket, os, sys, json\n"
            "results = {}\n"
            "try:\n"
            "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "    s.settimeout(2)\n"
            "    s.connect(('1.1.1.1', 443))\n"
            "    results['network'] = 'CONNECTED'\n"
            "except OSError as e:\n"
            "    results['network'] = 'denied'\n"
            "try:\n"
            "    os.listdir(os.path.expanduser('~') + '/.ssh')\n"
            "    results['ssh_read'] = 'SUCCEEDED'\n"
            "except OSError as e:\n"
            "    results['ssh_read'] = 'denied'\n"
            "print(json.dumps(results))\n"
        )
        idx = argv.index("-c") + 1
        probe_argv = list(argv)
        probe_argv[idx] = probe
        import subprocess

        proc = subprocess.run(probe_argv, input=b"", capture_output=True, timeout=10)
        assert proc.returncode == 0, proc.stderr.decode(errors="replace")
        payload = json.loads(proc.stdout.decode())
        assert payload["network"] == "denied"
        assert payload["ssh_read"] == "denied"


# --- SBPL profile contents -------------------------------------------------


class TestMacosSandboxProfile:
    def test_profile_denies_default_and_network_and_write(self):
        profile = pdf_worker.build_macos_sandbox_profile(["/tmp/fake-root"])
        assert "(deny default)" in profile
        assert "(deny network*)" in profile
        assert "(deny file-write*)" in profile

    def test_process_exec_is_scoped_to_named_binaries_only(self):
        """sandbox-exec needs process-exec rights for its own execve of the
        interpreter, but never a blanket grant — only the exact binary
        path(s) passed in."""
        profile = pdf_worker.build_macos_sandbox_profile(
            ["/tmp/fake-root"], executables=["/tmp/fake-root/bin/python3"]
        )
        assert '(allow process-exec (literal "/tmp/fake-root/bin/python3"))' in profile
        assert "(allow process-exec)" not in profile

    def test_framework_python_app_relaunch_binary_is_granted(self, monkeypatch, tmp_path):
        """A macOS framework build (Homebrew python@3.x) re-execs its
        ``bin/python3.x`` stub into ``Resources/Python.app/Contents/MacOS/Python``.
        Without an exec grant for that binary every extraction exits 1 with
        ``posix_spawn: ... Python.app/...``; grant exactly that one path."""
        app = tmp_path / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
        app.parent.mkdir(parents=True)
        app.write_bytes(b"")
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "base_prefix", str(tmp_path))
        monkeypatch.setattr(pdf_worker, "_SANDBOX_EXEC", sys.executable)
        argv, failure = pdf_worker._worker_argv(["50", "1000", "1000000", "5"])
        assert failure is None
        profile = argv[2]
        assert f'(literal "{os.path.realpath(app)}")' in profile
        assert "(allow process-exec)" not in profile

    def test_no_executables_means_no_exec_grant_at_all(self):
        profile = pdf_worker.build_macos_sandbox_profile(["/tmp/fake-root"])
        assert "process-exec" not in profile

    def test_profile_has_no_network_allow_rule(self):
        profile = pdf_worker.build_macos_sandbox_profile(["/tmp/fake-root"])
        assert "allow network" not in profile

    def test_profile_grants_only_computed_roots_not_broad_system_paths(self):
        roots = ["/fake/venv", "/fake/interpreter"]
        profile = pdf_worker.build_macos_sandbox_profile(roots)
        assert '(subpath "/fake/venv")' in profile
        assert '(subpath "/fake/interpreter")' in profile
        # Never a blanket grant of Homebrew or /Library as a whole.
        assert "/opt/homebrew" not in profile
        assert '"/Library"' not in profile
        assert '"/usr"' not in profile

    def test_profile_rejects_unsafe_paths(self):
        with pytest.raises(ValueError):
            pdf_worker.build_macos_sandbox_profile(['/tmp/evil")(allow file-write*'])

    def test_profile_requires_at_least_one_root(self):
        with pytest.raises(ValueError):
            pdf_worker.build_macos_sandbox_profile([])

    def test_macos_read_roots_are_computed_from_sys_and_pypdf(self):
        roots = pdf_worker._read_roots()
        assert roots, "expected at least one computed read root"
        # Every root must be an absolute, realpath'd filesystem path.
        for root in roots:
            assert os.path.isabs(root)
            assert root == os.path.realpath(root)


# --- real macOS end-to-end (skipped off-darwin) ----------------------------


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
    reason="requires macOS with /usr/bin/sandbox-exec",
)
class TestMacosRealExtraction:
    def test_real_extraction_under_seatbelt(self):
        body = _real_pdf(text="SEATBELT_MARKER")
        result = pdf_worker.extract_pdf_text_confined(body, timeout_s=10)
        assert result.ok, result
        assert "SEATBELT_MARKER" in result.text

    def test_worker_argv_is_wrapped_in_sandbox_exec(self):
        argv, failure = pdf_worker._worker_argv(["50", "1000", "1000000", "5"])
        assert failure is None
        assert argv[0] == "/usr/bin/sandbox-exec"
        assert argv[1] == "-p"


def test_read_roots_locate_pypdf_without_importing_it(monkeypatch):
    """The hub parent must never execute pypdf's import-time code (Devin, PR #876)."""
    import builtins

    monkeypatch.delitem(sys.modules, "pypdf", raising=False)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "pypdf" or name.startswith("pypdf."):
            raise AssertionError("pypdf imported in the parent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    roots = pdf_worker._read_roots()
    spec = importlib.util.find_spec("pypdf")
    assert os.path.dirname(os.path.realpath(spec.origin)) in roots
    assert "pypdf" not in sys.modules
