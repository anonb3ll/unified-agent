"""Minimal, deterministic repro for the agy print-mode teardown race (task #1143).

Task #1166 established that forking/patching the `agy` CLI is void (docs-only
upstream repo, opaque ~224MB binary, ToS forbids modification). The remaining
legitimate path is: reproduce with a fixture, capture a trace, file upstream,
and keep `agy` on the executor path (with a documented workaround if one
exists) — see ``docs/upstream/`` and ``docs/automation.md`` ("Agy CLI
pitfalls").

This test replays a *live* capture (``tests/fixtures/agy_teardown_1143/``,
recorded 2026-09-21 against the locally installed ``agy 1.2.7`` via
``scripts/agy_teardown_canary.sh``) through the shipped, unmodified parsing
functions in ``src/unified_agent/executor/backends.py``. It needs no live
`agy` binary and is fully deterministic — the fixture is a byte-for-byte
recording, not a live invocation.

It intentionally does NOT assert the executor flags this attempt as a
failure: the text-level checks here still miss it, and the executor's
structural detector (task #1247) is pinned in
``tests/test_agy_false_success.py`` (see fixture README). The test
pins the text-level gap down as documented, known behavior so the reproduction survives
independent of any specific agy version being installed in CI, and so a
future upstream or detection fix shows up here as an intentional, reviewed
diff rather than a silent behavior change.
"""

from __future__ import annotations

from pathlib import Path

from unified_agent.executor import backends

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "agy_teardown_1143"


def _load(name: str) -> str:
    path = FIXTURE_DIR / name
    return path.read_text() if path.exists() else ""


def test_live_capture_reports_clean_success_with_marker_missing() -> None:
    """The captured live-agy trace parses as a clean SUCCESS, no error.

    This is the executor-facing contract (backends.py's ``parse_agy_stream_json``
    return shape): text/tools/files/events populated, error=None. Combined with
    the fixture README's record that the requested background write never
    landed on disk, this is exactly the "phantom success" failure mode task
    #1143 was opened to chase down — the backgrounded work silently vanished
    and nothing in the parsed result says so.
    """
    stdout = _load("live_1.2.7_run1.stdout")
    assert stdout, "fixture missing — see tests/fixtures/agy_teardown_1143/README.md"

    parsed = backends.parse_agy_stream_json(stdout)

    assert parsed["error"] is None
    assert parsed["text"] == "LAUNCHED\n"
    # The agent did call run_command twice: the reconnaissance `pwd`/`git
    # rev-parse` and the backgrounding `(sleep 8; ...) &`. Both are reported
    # DONE by the CLI itself — the tool call succeeded; it is the child it
    # spawned that vanished.
    assert parsed["tools_used"].count("run_command") == 2


def test_live_capture_has_no_known_teardown_signature() -> None:
    """detect_agy_background_teardown (task #1158) returns None on this trace.

    This is the core #1143 finding: on the currently installed agy 1.2.7, the
    CLI silently drops backgrounded work WITHOUT emitting any of the four
    teardown phrases #1158's detector keys off of (`root agent idle; waiting
    up to`, `terminating N background task(s)`, `Print mode: drain grace
    period expired`, `All your background tasks have been stopped`). That
    makes this strictly worse than the originally observed race: no signal at
    all, versus a detectable-and-handled teardown.

    If this assertion ever starts failing, it means agy started emitting a
    recognized signature again (or a new one was added to the regex) — that
    is progress, not a regression, and this test should be updated to match
    the new fixture/behavior rather than papered over.
    """
    stdout = _load("live_1.2.7_run1.stdout")
    stderr = _load("live_1.2.7_run1.stderr")
    assert stdout, "fixture missing — see tests/fixtures/agy_teardown_1143/README.md"

    assert backends.detect_agy_background_teardown(stdout, stderr) is None


def test_synthetic_1158_fixture_is_still_caught_for_contrast() -> None:
    """Sanity check: the #1158 detector still catches the *documented* signature.

    Contrasts with the two tests above — the gap is specific to the newer,
    signature-less silent-drop variant captured live against 1.2.7, not a
    wholesale break of #1158 detection. Mirrors
    ``tests/test_agy_teardown_resume.py::AGY_TEARDOWN_RACE_STDOUT``.
    """
    stdout_with_signature = "\n".join(
        [
            '{"event": "init", "init": {"cwd": "/tmp"}}',
            "root agent idle; waiting up to 5s for 1 background task(s)",
            "terminating 1 background task(s) on exit",
            '{"event": "result", "result": {"status": "SUCCESS", "response": "done", "error": ""}}',
        ]
    )

    reason = backends.detect_agy_background_teardown(stdout_with_signature, "")

    assert reason is not None
    assert backends.AGY_BACKGROUND_TEARDOWN_REASON in reason
