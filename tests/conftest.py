import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# A developer's own ~/.unified_agent/settings.toml must never leak into a run:
# disable the settings file before anything imports unified_agent.config.
os.environ["UA_SETTINGS_FILE"] = ""
# The suite predates configurable operator identity and its fixtures name the
# original operator. Pin it here so they keep exercising the same paths; the
# neutral defaults and custom operators are covered in test_settings.py.
# UA_OPERATOR_NAME stays unset: it is also an evidence input, so setting it
# suite-wide would add an operator claim to every evidence record. The name
# falls back to the first alias instead.
os.environ.setdefault("UA_OPERATOR_LABEL", "Desktop_Person")
os.environ.setdefault("UA_OPERATOR_ALIASES", "Operator")
# The JWK scan fails closed in production (an unexpected exception masks the
# whole text); in tests it must raise, so a scanner bug cannot hide (#2200).
os.environ.setdefault("UA_REDACTION_STRICT", "1")

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
from host_repo_guard import (  # noqa: E402
    administrative_worktree_snapshot,
    drop_branches,
    external_branch_names,
    scope_host_repo_state,
    settle_mismatch,
)
from param_sampling import TIERS, sample  # noqa: E402
from product_repo_env import point_default_project_at  # noqa: E402
from store_template import seed_store  # noqa: E402

# Delay between the host-repo guard's retry reads when a mismatch is found at
# session teardown (see `host_repo_unchanged`). Long enough for a concurrent
# `git fetch`/`git branch -D` from another agent to finish; short enough not
# to be felt on a real escape, which fails either way.
_HOST_REPO_GUARD_RETRY_DELAY_SECONDS = 0.3


def _shorten_tmpdir_for_unix_sockets() -> None:
    """Point the process temp dir at a short path so ``AF_UNIX`` sockets fit.

    pytest's ``tmp_path`` is ``$TMPDIR/pytest-of-<user>/pytest-N/<test-name>0``.
    A long test name plus a socket filename (e.g. ``events.sock``) then pushes
    the full path past the ``sun_path`` limit — 104 bytes on macOS, 108 on
    Linux — and the event-bus tests fail with ``OSError: AF_UNIX path too
    long``.

    This used to be treated as macOS-only, because Linux's default ``/tmp`` is
    short. It is not macOS-only any more: the cockpit's pytest-tmp-guard hook
    sets ``TMPDIR`` and ``PYTEST_DEBUG_TEMPROOT`` to ``~/.cache/pytest-<pid>``,
    which is already 35+ characters *before* pytest appends its own
    ``pytest-of-<user>/pytest-N/<test-name>0`` suffix. On a GitHub runner the
    checkout path alone (``/home/runner/work/unified_agent/unified_agent``) is
    over 50. Both overflow, so the shortening applies wherever ``AF_UNIX`` is
    in play, not only on Darwin (task #2293).

    Setting ``TMPDIR`` to a short, stable path before pytest and ``tempfile``
    resolve it keeps every ``tmp_path`` short. pytest prefers
    ``PYTEST_DEBUG_TEMPROOT`` over ``TMPDIR`` for its basetemp, and a long value
    there is shortened the same way (task #2040). Concurrent runs stay apart
    because pytest allocates its own ``pytest-N`` basetemp under this root with
    a lock, exactly as it does on macOS.

    Windows is left alone: its ``AF_UNIX`` support does not use ``sun_path`` the
    same way. The dir is created mode 0o700 so the event-bus socket inherits the
    same owner-only posture the server enforces on its own socket dir.
    """
    if sys.platform == "win32":
        return
    short = Path("/tmp/ua-pytest")
    short.mkdir(mode=0o700, parents=True, exist_ok=True)

    def too_long(value: str) -> bool:
        # Respect an operator-provided short dir rather than overriding it.
        return len(Path(value).resolve().as_posix()) > 20

    tmpdir = os.environ.get("TMPDIR")
    if not tmpdir or too_long(tmpdir):
        os.environ["TMPDIR"] = str(short)
        tempfile.tempdir = str(short)
    root = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    if root and too_long(root):
        os.environ["PYTEST_DEBUG_TEMPROOT"] = str(short)


_shorten_tmpdir_for_unix_sockets()


_EMBEDDINGS_AVAILABLE: bool | None = None
_EMBEDDINGS_ERROR = ""


def embeddings_available() -> tuple[bool, str]:
    """Probe the fastembed model once per session; cache the verdict.

    fastembed downloads its ONNX model on first use, so a fresh clone, a
    locked-down CI runner, or a sandbox with no route to the model host cannot
    embed anything. Before this probe existed, that condition surfaced as 24
    assertion failures reading like `assert set() == {'2160d689dc5e', ...}` —
    indistinguishable from a real semantic-search regression, which is exactly
    the confusion `docs/assessment-2026-08-12.md` §3.2 is about.

    The probe is the *real* code path (`generate_embedding`), not an import
    check: the model imports fine and then fails on download, so an import
    check would report available and the tests would still fail.

    Only a *model acquisition* failure grants the skip. Catching every
    `Exception` here would collapse "this host has no model" into "the
    embedding code is broken" and resolve it the wrong way — a regression in
    `generate_embedding` would skip the whole semantic tier and leave CI green,
    which is the exact outcome the marker exists to prevent. Anything that is
    not an acquisition failure reports *available*, so the marked tests run and
    fail on their own merits.
    """
    global _EMBEDDINGS_AVAILABLE, _EMBEDDINGS_ERROR
    if _EMBEDDINGS_AVAILABLE is None:
        from unified_agent import embeddings

        try:
            vector = embeddings.generate_embedding("probe")
            _EMBEDDINGS_AVAILABLE = bool(vector)
            if not vector:
                _EMBEDDINGS_ERROR = "model returned an empty vector"
        except Exception as exc:
            if embeddings.is_model_acquisition_error(exc):
                _EMBEDDINGS_AVAILABLE = False
                _EMBEDDINGS_ERROR = f"{type(exc).__name__}: {exc}"
            else:
                # A defect, not an unavailable host. Let the tier run and fail.
                _EMBEDDINGS_AVAILABLE = True
                _EMBEDDINGS_ERROR = ""
        # The probe itself is a legitimate use, but its recorded failure would
        # otherwise be indistinguishable from a real one in any test asserting
        # on embedding_state().
        embeddings.reset_embedding_state()
    return _EMBEDDINGS_AVAILABLE, _EMBEDDINGS_ERROR


def pytest_addoption(parser):
    parser.addoption(
        "--exhaustive",
        action="store_true",
        default=False,
        help="run every case of large parametrize matrices instead of a sample "
        "(also UA_TEST_EXHAUSTIVE=1; same as --param-sample=exhaustive)",
    )
    parser.addoption(
        "--param-sample",
        choices=sorted(TIERS),
        default=None,
        help="how large redaction/JWK parametrize matrices are sampled: "
        "representative (default; every value at least once plus a stable slice), "
        "pairwise, or exhaustive (also UA_TEST_PARAM_SAMPLE; see tests/param_sampling.py)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "embeddings: requires a working fastembed model; skipped with an explicit "
        "reason when the model cannot be loaded, never failed",
    )
    config.addinivalue_line(
        "markers",
        "exhaustive: always run every case of this parametrize matrix; never "
        "pairwise-sampled (tests/param_sampling.py)",
    )
    config.addinivalue_line(
        "markers",
        "live_process_scan: use worktree_policy's real live-process scan instead "
        "of the empty process table every other test gets (task #2281)",
    )


def _sample_tier(config) -> str:
    """``exhaustive`` when asked for by flag or UA_TEST_EXHAUSTIVE=1, which CI
    sets; otherwise ``--param-sample``, then UA_TEST_PARAM_SAMPLE, then
    ``representative``. An unknown environment value fails the session rather
    than silently picking a tier."""
    if config.getoption("--exhaustive") or os.environ.get("UA_TEST_EXHAUSTIVE") == "1":
        return "exhaustive"
    tier = (
        config.getoption("--param-sample")
        or os.environ.get("UA_TEST_PARAM_SAMPLE")
        or "representative"
    )
    if tier not in TIERS:
        raise pytest.UsageError(
            f"UA_TEST_PARAM_SAMPLE={tier!r}: expected one of {', '.join(sorted(TIERS))}"
        )
    return tier


def _org_partition_exempt() -> list[str]:
    """Node ids from ``tests/org_partition_exempt.txt``; the file states each group's reason."""
    text = (Path(__file__).parent / "org_partition_exempt.txt").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def pytest_collection_modifyitems(config, items):
    """Sample large parametrize matrices, then gate the embedding tier.

    Large matrices in the redaction and JWK files run a representative subset
    by default: every value of every dimension at least once plus a stable
    slice of the product (tasks #2230, #2261, tests/param_sampling.py). The
    cases left out are reported as deselected, so a run's summary still shows
    them.

    The embedding tier is skipped with a reason rather than left to fail.
    A skip that names the cause keeps the distinction the assessment asks for:
    "the model is unavailable" and "semantic search regressed" must not look
    the same. Tests opt in with `pytest.mark.embeddings`.

    A run on a partitioned store leaves out the tests that cannot apply to one
    (task #2340), so a failure in that run is a defect.
    """
    select = TIERS[_sample_tier(config)]
    if select is not None:
        kept, deselected = sample(items, select)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = kept
    if os.environ.get("UA_TEST_ORG_PARTITION") == "1":
        exempt = set(_org_partition_exempt())
        deselected = [item for item in items if item.nodeid in exempt]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = [item for item in items if item.nodeid not in exempt]
    if not any(item.get_closest_marker("embeddings") for item in items):
        return
    available, error = embeddings_available()
    if available:
        return
    skip = pytest.mark.skip(reason=f"embedding model unavailable, semantic tier not run ({error})")
    for item in items:
        if item.get_closest_marker("embeddings"):
            item.add_marker(skip)


def _host_repo_texts() -> tuple[Path, str, str] | None:
    """Raw worktree and branch listings of the repository these tests run from.

    None means Git is absent or this source directory is not a repository.
    Unsupported -z never disables the guard; unreadable existing repos fail.
    """
    repo = Path(__file__).resolve().parent.parent
    try:
        worktrees = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            cwd=repo,
            capture_output=True,
            text=False,
            timeout=30,
        )
        if worktrees.returncode != 0:
            probe = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=repo,
                capture_output=True,
                text=False,
                timeout=30,
            )
            if probe.returncode != 0:
                if not (repo / ".git").exists():
                    return None
                raise RuntimeError("host Git worktree snapshot is unavailable")
            # This fixed valid command reports unsupported -z as usage status
            # 129. Do not inspect localized stderr; the verified repository's
            # byte-exact administrative snapshot remains the authority.
            if worktrees.returncode == 129:
                worktrees_text = administrative_worktree_snapshot(repo, subprocess.run)
            else:
                raise RuntimeError("host Git worktree snapshot is unavailable")
        else:
            if not worktrees.stdout:
                raise RuntimeError("host Git worktree snapshot is empty")
            worktrees_text = worktrees.stdout.decode("utf-8", errors="surrogateescape")
        branches = subprocess.run(
            ["git", "branch", "--list"], cwd=repo, capture_output=True, text=False, timeout=30
        )
        if branches.returncode != 0:
            raise RuntimeError("host Git branch snapshot is unavailable")
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("host Git repository snapshot is unavailable") from exc
    return repo, worktrees_text, branches.stdout.decode("utf-8", errors="surrogateescape")


def _host_repo_state() -> tuple[str, str] | None:
    """Snapshot exact Git metadata, with an administrative older-Git fallback.

    None means Git is absent or this source directory is not a repository.
    Unsupported -z never disables the guard; unreadable existing repos fail.
    """
    texts = _host_repo_texts()
    return None if texts is None else scope_host_repo_state(*texts)


def _external_branches(since: float) -> set[str]:
    """Branches other checkouts of this gitdir account for (task #1998).

    Attribution only ever excuses a mismatch, so a failure to read it
    attributes nothing and leaves the guard to fail on what it sees.
    """
    try:
        texts = _host_repo_texts()
    except RuntimeError:
        return set()
    if texts is None:
        return set()
    repo, worktrees_text, _ = texts
    return external_branch_names(repo, worktrees_text, since=since)


@pytest.fixture(scope="session", autouse=True)
def host_repo_unchanged():
    """Fail the session if the suite mutates the git repo it is being run from.

    test_executor.py::test_finish_to_review_lifecycle used to register a worktree
    and create an `agy/task-1-...` branch in the developer's real checkout even
    though UA_WORKSPACE pointed at tmp_path: the executor's worktree creation
    shelled out through os.system, which runs in the *process* cwd, so git bound
    to whatever repository pytest was launched from. Nothing caught it — the exit
    status went to /dev/null, and re-runs failed silently because the branch was
    already there.

    This guard is the net for that whole class of defect, not just that one test:
    any test that escapes its tmp_path and mutates *this checkout's* worktrees
    or local branches fails the run. Concurrent worktrees elsewhere in the
    same gitdir (a live `task_claim`) are out of scope — see
    ``scope_host_repo_state``.

    That spatial scoping only helps when the mutation has a worktree to be
    "elsewhere" relative to. A concurrent `git fetch -q origin
    pull/523/head:pr-523-review` (or its `git branch -D` cleanup) creates or
    removes a bare branch ref with no worktree at all, so it is invisible to
    ``scope_host_repo_state`` and looks identical to a real escape — a false
    positive on ordinary multi-agent concurrency in a shared gitdir, not a
    scoping gap ``scope_host_repo_state`` can close. See ``settle_mismatch``:
    on a mismatch this re-reads state a couple more times a beat apart, and
    only fails once the difference persists. Concurrent fetch/delete churn
    settles within that window; a test that genuinely creates or deletes a
    branch and leaves it that way does not, so it still fails.

    Other agents' lanes also leave branches behind for good (task #1998): a
    lane switches its worktree to `claude/<id>-...`, leaving the branch it was
    created on with no worktree, and a finished lane removes its worktree but
    keeps the branch. Branch rows are therefore compared without the branches
    an external worktree held at any snapshot, or switched to or from since
    the guard started (``external_branch_names``). This checkout's own
    worktree record is never excused, so a test that moves its HEAD still
    fails, as does a new branch no other checkout accounts for.
    """
    started = int(time.time()) - 1
    before = _host_repo_state()
    excused = _external_branches(started)
    yield
    if before is None:
        return
    after = _host_repo_state()
    if after is None:
        pytest.fail("host Git repository snapshot became unavailable while its guard was active")
    if after == before:
        return
    excused |= _external_branches(started)

    def _same(old, new):
        return drop_branches(old, excused) == drop_branches(new, excused)

    def _poll():
        time.sleep(_HOST_REPO_GUARD_RETRY_DELAY_SECONDS)
        state = _host_repo_state()
        excused.update(_external_branches(started))
        return state

    after = settle_mismatch(before, after, _poll, max_retries=2, same=_same)
    if after is None:
        pytest.fail("host Git repository snapshot became unavailable while its guard was active")
    if _same(before, after):
        return
    before, after = drop_branches(before, excused), drop_branches(after, excused)

    def _diff(label: str, old: str, new: str) -> list[str]:
        added = [ln for ln in new.splitlines() if ln not in old.splitlines()]
        removed = [ln for ln in old.splitlines() if ln not in new.splitlines()]
        out = []
        if added:
            out.append(f"  {label} added:   " + ", ".join(repr(ln.strip()) for ln in added))
        if removed:
            out.append(f"  {label} removed: " + ", ".join(repr(ln.strip()) for ln in removed))
        return out

    report = _diff("worktree", before[0], after[0]) + _diff("branch", before[1], after[1])
    pytest.fail(
        "the test suite mutated the git repository it is running from — a test "
        "escaped its tmp_path:\n" + "\n".join(report),
        pytrace=False,
    )


@pytest.fixture
def workspace_is_product_repo(isolated_env):
    """Make the workspace the default project's checkout (task #2041)."""
    point_default_project_at(isolated_env)
    return isolated_env


@pytest.fixture
def bootstrap_test_store():
    """Stateful tests seed a real store; pure modules may forbid store access."""
    return True


@pytest.fixture(scope="session")
def _store_templates(tmp_path_factory):
    """Where this process keeps its initialized template stores (#2238)."""
    return tmp_path_factory.mktemp("store-templates")


@pytest.fixture(autouse=True)
def _empty_process_table(request, monkeypatch):
    """Worktree cleanup sees no running processes unless a test says otherwise (task #2281).

    The real scan reads the host's /proc (or lsof), so its answer depends on
    what else runs there: one same-user process that refuses a read (a
    non-dumpable ssh-agent, say) makes liveness unknown, and cleanup then
    retains everything. Tests that exercise liveness pass their own process
    table, usually a /proc look-alike; `live_process_scan` keeps the real one.
    """
    if request.node.get_closest_marker("live_process_scan"):
        return
    from unified_agent.tools import worktree_policy

    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: frozenset())


def _init_partitioned_store(storage, monkeypatch):
    """``init_db`` followed by the org_id sentinel backfill (task #1433)."""
    init_db = storage.init_db

    def init() -> None:
        from unified_agent import org_partition_migration

        init_db()
        monkeypatch.setenv("UA_ORG_PARTITION", "1")
        conn = storage.connect(scoped=False)
        try:
            org_partition_migration.apply(conn)
        finally:
            conn.close()
            monkeypatch.delenv("UA_ORG_PARTITION")

    return init


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, tmp_path_factory, monkeypatch, bootstrap_test_store, _store_templates):
    """Point workspace and data home at temp dirs so tests never touch real state."""
    # Git discovery stops at the pytest temp root (task #2280). Project dirs
    # under tmp_path are mostly not repositories, so without a ceiling `git
    # worktree add/list` run from one walks up into whatever repository
    # encloses the host's temp root (a dotfile repo, say) and registers
    # worktrees there. A repository a test creates under tmp_path is still
    # found: git stops before entering the ceiling, not below it.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path_factory.getbasetemp().resolve()))
    ws = tmp_path / "workspace"
    home = tmp_path / "ua_home"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv(
        "UA_PROJECTS_JSON",
        json.dumps(
            [
                {
                    "id": "unified-agent",
                    "label": "Unified Agent",
                    "dirname": "unified_agent",
                    "repo": "anonb3ll/unified-agent",
                },
                {"id": "browser", "label": "Browser", "dirname": "browser"},
                {"id": "sample-store", "label": "Sun Market", "dirname": "sample-store"},
                {"id": "client-portal", "label": "TAP / DV Law", "dirname": "tap"},
            ]
        ),
    )
    # Advisory quota collection must never discover the operator's Codex logs.
    # An empty per-test tree preserves honest unknown results; native telemetry
    # tests opt into their own synthetic samples with an explicit override.
    codex_sessions = tmp_path / "codex-sessions"
    codex_sessions.mkdir()
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(codex_sessions))
    # Same for ua-node native session discovery (task #1741): never the
    # operator's own Claude Code or Codex session stores.
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(tmp_path / "claude-projects"))
    monkeypatch.setenv("UA_NODE_CODEX_HOME", str(tmp_path / "codex-home"))
    # The ACP idle window holds a finished session open for follow-ups; tests
    # that exercise it pass idle_s themselves.
    monkeypatch.setenv("UA_NODE_ACP_IDLE_S", "0")
    monkeypatch.setenv("UA_VAULT_PATH", str(tmp_path / "vault"))
    # Same hermeticity for config.project_repo_path/roots (task #371): the
    # real host's ~/dev/* checkouts must not leak into tests that create a
    # "doing" task with a non-default project. Points at a tmp dir that does
    # not exist by default, so every project resolves to "not checked out"
    # unless a test explicitly creates a subdirectory there.
    monkeypatch.setenv("UA_DEV_ROOT", str(tmp_path / "dev"))
    monkeypatch.delenv("UA_VAULT_WRITE_DIRS", raising=False)
    monkeypatch.delenv("UA_SHELL_ALLOWLIST", raising=False)
    monkeypatch.delenv("UA_ALLOW_LOCAL_FETCH", raising=False)
    monkeypatch.delenv("UA_ANOMALY_SILENT_AGENT_IGNORE", raising=False)
    monkeypatch.delenv("UA_OVERWRITE_AUTHORS", raising=False)
    monkeypatch.delenv("UA_MEMORY_DEDUP_THRESHOLD", raising=False)
    # Search-provider hermeticity: real API keys in the host env must not flip
    # provider auto-resolution mid-suite; tests opt in with setenv.
    monkeypatch.delenv("UA_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Same hermeticity for llm_client's fallback legs: doctor's
    # llm_provider_chain check probes each *configured* leg's metadata
    # endpoint, so real keys/URLs in the host env would turn a read-only
    # status call into a real network probe (and, for the cold-start
    # integration test, flip llm_provider_chain to MISCONFIGURED under an
    # inherited provider env — task #1294). Cleared from
    # llm_client.PROVIDER_CHAIN_ENV_VARS itself rather than a hand-copied
    # list of names, so the doctor check and this fixture can't drift apart.
    # Tests opt in via setenv or the leg_status(env=...) parameter.
    from unified_agent import config as _config_for_hermeticity
    from unified_agent import llm_client as _llm_client_for_hermeticity

    for _provider_env_name in _llm_client_for_hermeticity.PROVIDER_CHAIN_ENV_VARS:
        monkeypatch.delenv(_provider_env_name, raising=False)
    # The claude_cli leg (task #2025) also finds the real `claude` on PATH or
    # in ~/.local/bin, so clearing UA_CLAUDE_CLI_BIN is not enough: a test
    # reaching complete() or leg_status() would start a real CLI session on
    # the operator's subscription. The search is stubbed out, so the leg reads
    # as missing unless a test points UA_CLAUDE_CLI_BIN at a fake executable.
    monkeypatch.setattr(_llm_client_for_hermeticity, "_search_claude_cli", lambda env: None)
    monkeypatch.delenv("UA_CLAUDE_CLI_TIMEOUT_S", raising=False)
    # init_db calls ensure_dirs before the test body runs. Keep its optional
    # host key-file loader from undoing the provider environment isolation.
    monkeypatch.setattr(_config_for_hermeticity, "load_gemini_env", lambda: None)
    monkeypatch.delenv("UA_OLLAMA_TIMEOUT_S", raising=False)
    # The shared system_doctor report and the LLM leg-probe results are cached
    # in process (task #1850). Tests change state between two doctor calls and
    # expect the second to see it, so caching is off by default here; the
    # cache tests opt back in with monkeypatch.setenv. Backfill batches skip
    # their production yield for speed.
    monkeypatch.setenv("UA_DOCTOR_CACHE_S", "0")
    monkeypatch.delenv("UA_DOCTOR_MAX_STALE_S", raising=False)
    monkeypatch.setenv("UA_LLM_PROBE_CACHE_S", "0")
    monkeypatch.setenv("UA_BACKFILL_BATCH_PAUSE_S", "0")
    # The worktree survey is shared the same way (task #1897); tests create and
    # remove worktrees between surveys, so it is off here too.
    monkeypatch.setenv("UA_WORKTREE_SURVEY_CACHE_S", "0")
    from unified_agent.tools import doctor as _doctor_report_cache_owner
    from unified_agent.tools import worktree_policy as _worktree_survey_cache_owner

    _doctor_report_cache_owner.clear_report_cache()
    _worktree_survey_cache_owner.clear_survey_cache()
    _llm_client_for_hermeticity.clear_probe_cache()
    monkeypatch.delenv("UA_SUBSCRIPTION_POOL_MODEL", raising=False)
    # Disable the service_down/service_crash_loop rules by default: they shell
    # out to `systemctl --user show`, and CI runners (and plenty of dev boxes)
    # don't have the real units installed. Tests exercising these rules opt
    # back in explicitly via monkeypatch.setenv("UA_ANOMALY_SERVICES", ...).
    monkeypatch.setenv("UA_ANOMALY_SERVICES", "")
    # Same hermeticity for system_doctor's unit-template drift check: it reads
    # installed units from $XDG_CONFIG_HOME/systemd/user (~/.config fallback),
    # and the host's real units must not sway test results. Tests opt back in
    # by pointing XDG_CONFIG_HOME at their own tmp dir.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # Same hermeticity for the Mac LaunchAgent config system_doctor reads when
    # systemctl is absent: ~/Library/LaunchAgents/com.unified-agent.*.plist,
    # the launcher scripts they exec, and ~/.unified_agent/*.env. On the Mac
    # hub host those are the live hub's config and made doctor tests report
    # LIVE where they expected "cannot determine" (task #1373). Tests about
    # LaunchAgent resolution stub doctor._mac_home at their own tmp dir.
    from unified_agent.tools import doctor as _doctor_for_hermeticity

    monkeypatch.setattr(_doctor_for_hermeticity, "_mac_home", lambda: tmp_path / "mac_home")
    # Same hermeticity for the hermes_env rule: it probes a real install dir
    # (~/.hermes/hermes-agent by default). Tests opt back in with a tmp dir.
    monkeypatch.setenv("UA_ANOMALY_HERMES_DIR", "")
    # Same hermeticity for host_low_memory: it probes the real /proc/meminfo
    # by default, whose values (and thus whether the rule fires) depend on
    # the host running the suite. Tests opt back in with a fake meminfo file.
    monkeypatch.setenv("UA_ANOMALY_MEMINFO_PATH", "")
    # Same hermeticity for llm_client.is_on_battery(): it probes the real
    # /sys/class/power_supply by default, whose presence/state depends on the
    # host running the suite (desktop vs laptop). Tests opt back in with a
    # fake power-supply directory tree.
    monkeypatch.setenv("UA_POWER_SUPPLY_PATH", "")
    # Default state-sync to synchronous (no debounce) so tests stay deterministic
    # and no background timer escapes into the real vault. Tests exercising the
    # debounce path override config.state_sync_debounce_seconds directly.
    monkeypatch.setenv("UA_STATE_SYNC_DEBOUNCE", "0")
    # State sync itself defaults to ON in production (config.state_sync_enabled).
    # Off by default here, same hermeticity pattern as the switches above: any
    # init_db() a test or fixture runs takes a startup snapshot, and with sync on
    # that snapshot lands in the test's vault *before* the test body sets
    # UA_SOURCE_HOST — so it is filed under the machine's real hostname. On this
    # desktop that name is "DesktopHost", which test_sync_host_scoping already
    # writes, so the stray file collided with an expected one and hid; on a CI
    # runner it is an extra file and a red test (task #201). Tests about state
    # sync opt back in with monkeypatch.setenv("UA_STATE_SYNC", "1").
    monkeypatch.setenv("UA_STATE_SYNC", "0")
    # The review-head check defaults to ON in production (task #2320). Off here
    # because every Approval Ledger poll test would otherwise count its extra
    # GitHub write; tests/test_review_head_status.py opts back in.
    monkeypatch.setenv("UA_REVIEW_HEAD_CHECK", "0")
    # Delegation Spec Standard (task #160) is ENFORCING in production. Most
    # tests that call delegate() are exercising something else entirely (trust
    # propagation, the executor loop, callbacks) and pass a one-line task_spec;
    # requiring a full delegation spec there is incidental coupling, not
    # coverage. Same hermeticity pattern as UA_APPROVAL_TOOLS above: off by
    # default in tests, and the tests that are about the standard opt back in
    # with monkeypatch.setenv("UA_DELEGATION_SPEC_ENFORCE", "1").
    monkeypatch.delenv("UA_DELEGATION_SPEC_ENFORCE", raising=False)
    monkeypatch.setenv("UA_DELEGATION_SPEC_ENFORCE", "0")
    # Approval baseline is ON in production (`delegate` is gated unless the
    # operator opts out). Most delegate() callers here are testing something
    # else. Opt out for the suite; posture tests delete this variable.
    monkeypatch.setenv("UA_APPROVAL_BASELINE", "0")
    monkeypatch.delenv("UA_LEGACY_OPEN_POSTURE", raising=False)
    monkeypatch.delenv("UA_TRUST_STDIO", raising=False)
    monkeypatch.delenv("UA_REQUIRE_CALLER_IDENTITY", raising=False)
    monkeypatch.delenv("UA_LIFECYCLE_CAPTURE", raising=False)
    monkeypatch.delenv("UA_LIFECYCLE_EXCLUDE_KINDS", raising=False)
    monkeypatch.delenv("UA_LIFECYCLE_EXCLUDE_TOOLS", raising=False)
    # Swarm event bus (task #157): point the socket at tmp_path so a suite run
    # never publishes into the real ~/.config/agent_hub/events.sock that the
    # live hub and TUI are subscribed to. Nothing is listening on this path
    # unless a test starts a server there, and publish() treats that as an
    # ordinary no-op.
    monkeypatch.setenv("UA_EVENT_SOCKET", str(tmp_path / "events.sock"))

    from unified_agent import post_init, storage

    # The hub registers its post-init hooks (row migrations, backfills, the
    # startup vault sync) before its first init_db (task #1720). Register them
    # here too, so every storage.init_db() a test makes runs the same start-up
    # work it always has.
    post_init.register_default_hooks()

    # init_db spawns background threads (embedding backfills, startup vault
    # sync) that read UA_* env at run time. Join them before the test body so
    # a delayed straggler can't fire mid-test against a vault/DB the test
    # creates later — the root cause of the test_sync debounce flake: under
    # full-suite load the startup sync thread woke only after the test had
    # created its vault, then dumped a state snapshot into it.
    #
    # Joining only this call is not enough: fixtures and tests re-init freely
    # (17 test modules call init_db themselves), and each of those spawns its
    # own unjoined straggler. test_sync_host_scoping's `vault` fixture did
    # exactly that, and its startup sync wrote a state mirror named for the
    # machine's *real* hostname into the test vault. On this desktop that name
    # is "DesktopHost" — one the test already expects — so the stray file
    # collided with an expected one and nothing showed; on a CI runner it is a
    # fifth file and a red test (task #201). Wrapping init_db makes the join
    # automatic for every call made inside a test, which is the whole class of
    # defect rather than the one instance of it.
    real_init_db = storage.init_db

    def _init_db_and_join() -> None:
        real_init_db()
        storage.join_init_background_threads()

    monkeypatch.setattr(storage, "init_db", _init_db_and_join)
    if bootstrap_test_store:
        # Start from a copy of an already-initialized store: init_db still
        # runs in full, in a fraction of the time an empty home takes.
        from unified_agent import config as _config_for_store

        init = storage.init_db
        if os.environ.get("UA_TEST_ORG_PARTITION") == "1":
            # Run a test module against a partitioned store (task #1433):
            # application behaviour must not change.
            init = _init_partitioned_store(storage, monkeypatch)
        seed_store(_store_templates, tmp_path, _config_for_store.db_path(), init)

    yield ws

    # Drain any pending debounced snapshot a test scheduled, before monkeypatches
    # (vault path, etc.) are torn down.
    from unified_agent.sync import flush_pending_sync

    flush_pending_sync()


@pytest.fixture(autouse=True)
def _drop_coalesced_gateway_reads():
    """Read-only allow counts are held in process memory (task #1922).

    Dropped before and after every test so one test's counts are never written
    into the next test's database, or at interpreter exit into whatever
    ``db_path`` is current then.
    """
    from unified_agent import gateway

    gateway.reset_coalesced()
    yield
    gateway.reset_coalesced()


@pytest.fixture(autouse=True)
def _webui_testclient_uses_loopback_host(monkeypatch):
    """Starlette's TestClient defaults to ``Host: testserver``.

    ``/ui`` and ``/api`` reject any Host that is not loopback or listed in
    ``UA_HTTP_ALLOWED_HOSTS``. Rewriting only the default base URL keeps
    existing clients on ``127.0.0.1`` without adding ``testserver`` to the
    allowlist. An explicit ``base_url`` is left alone.
    """
    import starlette.testclient as testclient

    original = testclient.TestClient.__init__

    def _init(self, app, base_url="http://testserver", *args, **kwargs):
        if base_url.rstrip("/") == "http://testserver":
            base_url = "http://127.0.0.1"
        original(self, app, base_url, *args, **kwargs)

    monkeypatch.setattr(testclient.TestClient, "__init__", _init)


@pytest.fixture(autouse=True)
def _harness_billing_probe_is_unknown(monkeypatch):
    """The billing probe runs ``claude auth status`` / ``codex login status``.

    Task #1837: a dev host is logged in and a CI runner is not, so backend
    tests would see different ``billing``. Tests get an unknown bill by
    default; ``tests/test_harness_usage.py`` stubs the subprocess edge
    to cover the real probe.
    """
    from unified_agent import harness_billing

    monkeypatch.setattr(harness_billing, "probe", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _harness_sandbox_host_probe_passes(monkeypatch):
    """Copilot/Cursor/Agy blanket flags need a verified sandbox (task #1948).

    The real probe runs ``bwrap`` (Linux) or ``sandbox-exec`` (macOS); CI
    runners and dev hosts differ, so tests that drive those backends get a
    passing probe (and harness bypass reported off) by default.
    ``tests/test_harness_sandbox.py`` sets failing ones to prove the
    refusal, and calls the real probe with its subprocess boundary stubbed.
    """
    from unified_agent.dispatch import harness_sandbox

    harness_sandbox.reset_cache()
    monkeypatch.setattr(
        harness_sandbox,
        "probe_host",
        lambda: harness_sandbox.HostProbe(ok=True, backend="test-stub", detail="test stub"),
    )
    # The harness bypass check reads Copilot settings and refuses Agy outright;
    # tests that are not about it see bypass as off. The #1948 tests restore
    # ``_bypass_disabled_impl`` to prove the refusal.
    monkeypatch.setattr(
        harness_sandbox,
        "bypass_disabled",
        lambda cli_backend, cwd=None, env=None, binary=None: (True, "test stub"),
    )
    yield
    harness_sandbox.reset_cache()


@pytest.fixture
def seat_chief_of_staff(monkeypatch):
    """Give host-side labels the chief-of-staff default for one test.

    The only code-default chief of staff is ``cloud_agent13``, which is
    cloud-hosted: no hub workspace and no Control Center routes. A test whose
    scenario needs a host-side chief-of-staff caller seats its stand-in label
    here instead of depending on the fleet's profile map.
    """
    from unified_agent import profiles

    def seat(*labels: str) -> None:
        for label in labels:
            monkeypatch.setitem(
                profiles.DEFAULT_PROFILES_BY_IDENTITY, label, profiles.CHIEF_OF_STAFF
            )

    return seat
