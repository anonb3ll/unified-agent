"""The embedding tier must report its own degradation (assessment §3.2).

Semantic search is designed to fail soft: a note saves without an embedding
row, and `memory_search` falls back to FTS. That contract is correct and these
tests assert it still holds. What they add is the other half — that failing
soft is no longer the same as failing silently.

Every test here forces the failure by patching `generate_embedding`, so none of
them needs a real model and none carries the `embeddings` marker.
"""

import logging
import sqlite3

import pytest

from unified_agent import embeddings, storage
from unified_agent.tools import doctor
from unified_agent.tools.memory import memory_save, memory_search


@pytest.fixture(autouse=True)
def clean_embedding_state():
    embeddings.reset_embedding_state()
    yield
    embeddings.reset_embedding_state()


def _break_embeddings(monkeypatch, exc=None):
    """Make the model raise the way a blocked host does."""
    error = exc or RuntimeError("403 Forbidden")

    def _raise(text: str):
        embeddings._record_failure(error)
        raise error

    monkeypatch.setattr(embeddings, "generate_embedding", _raise)
    # The call sites import the symbol from the module at call time, so
    # patching the module attribute is enough — asserted by the tests below
    # actually observing failures through memory_save.


def _doctor_capability(name: str) -> dict:
    report = doctor.system_doctor()
    for check in report["checks"]:
        if check["capability"] == name:
            return check
    raise AssertionError(f"{name} missing from system_doctor: {report['checks']}")


class TestFailureRecording:
    def test_generate_embedding_records_failure_and_still_raises(self, monkeypatch):
        """Callers keep owning the degrade decision; the failure is counted."""

        def _boom(*_args, **_kwargs):
            raise RuntimeError("403 Forbidden")

        monkeypatch.setattr(embeddings, "get_embedding_model", _boom)

        with pytest.raises(RuntimeError):
            embeddings.generate_embedding("anything")

        state = embeddings.embedding_state()
        assert state["failures"] == 1
        assert state["successes"] == 0
        assert "403 Forbidden" in state["last_error"]
        assert state["last_error_at"]

    def test_first_failure_warns_and_repeats_do_not(self, monkeypatch, caplog):
        """A blocked host must not emit one warning per note saved."""

        def _boom(*_args, **_kwargs):
            raise RuntimeError("403 Forbidden")

        monkeypatch.setattr(embeddings, "get_embedding_model", _boom)

        with caplog.at_level(logging.DEBUG, logger="unified_agent.embeddings"):
            for _ in range(4):
                with pytest.raises(RuntimeError):
                    embeddings.generate_embedding("anything")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "only the first failure should warn"
        assert embeddings.embedding_state()["failures"] == 4

    def test_state_starts_clean(self):
        state = embeddings.embedding_state()
        assert state["failures"] == 0
        assert state["successes"] == 0
        assert state["last_error"] is None


class TestSaveStillSucceeds:
    """The fail-soft contract, asserted so this change cannot tighten it."""

    def test_memory_save_succeeds_without_an_embedding(self, monkeypatch):
        _break_embeddings(monkeypatch)
        note = memory_save(title="Offline note", content="saved with no model available")
        assert note["id"]

        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE note_id=?", (note["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert rows == 0, "no embedding row, but the note saved"

    def test_memory_search_still_returns_the_note_by_keyword(self, monkeypatch):
        _break_embeddings(monkeypatch)
        memory_save(title="Worktree guide", content="git worktrees allow parallel checkouts")
        results = memory_search(query="worktrees")
        assert any("Worktree guide" == r.get("title") for r in results["results"])

    def test_update_failure_invalidates_the_old_embedding(self, monkeypatch):
        """A changed note must not remain represented by its previous vector."""
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        note = memory_save(title="Original", content="old searchable content")

        conn = storage.connect()
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings WHERE note_id=?", (note["id"],)
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()

        _break_embeddings(monkeypatch)
        memory_save(id=note["id"], title="Updated", content="new searchable content")

        conn = storage.connect()
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings WHERE note_id=?", (note["id"],)
                ).fetchone()[0]
                == 0
            )
            try:
                rowid = conn.execute(
                    "SELECT rowid FROM memory WHERE id=?", (note["id"],)
                ).fetchone()[0]
                mirror_count = conn.execute(
                    "SELECT COUNT(*) FROM memory_vec WHERE rowid=?", (rowid,)
                ).fetchone()[0]
            except Exception:
                # sqlite-vec is optional; source-row invalidation is the
                # invariant that applies on every host.
                pass
            else:
                assert mirror_count == 0
        finally:
            conn.close()

        coverage = storage.embedding_coverage()
        assert coverage["missing"] == 1
        assert _doctor_capability("semantic_search")["status"] == "MISCONFIGURED"


class TestCoverage:
    def test_coverage_counts_unembedded_notes(self, monkeypatch):
        _break_embeddings(monkeypatch)
        memory_save(title="One", content="first note")
        memory_save(title="Two", content="second note")

        coverage = storage.embedding_coverage()
        assert coverage["total"] == 2
        assert coverage["missing"] == 2
        assert coverage["embedded"] == 0

    def test_coverage_ignores_generated_index_notes(self, monkeypatch):
        """Index notes are never embedded, so counting them would report a
        backlog that the startup backfill can never clear."""
        _break_embeddings(monkeypatch)
        memory_save(title="Real", content="a real note")

        conn = storage.connect()
        try:
            ts = storage.now()
            for note_id in ("masterindex00", "tagindex_git"):
                # OR REPLACE: masterindex00 is created by memory_save itself,
                # so this must work whether or not it is already present.
                conn.execute(
                    "INSERT OR REPLACE INTO memory (id, title, content, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (note_id, note_id, "generated", ts, ts),
                )
            conn.commit()
        finally:
            conn.close()

        coverage = storage.embedding_coverage()
        assert coverage["total"] == 1, "index notes excluded from the denominator"
        assert coverage["missing"] == 1


class TestDoctorVisibility:
    def test_dormant_when_there_is_nothing_to_embed(self):
        check = _doctor_capability("semantic_search")
        assert check["status"] == "DORMANT"
        assert "no memory notes" in check["detail"]

    def test_misconfigured_names_the_model_error(self, monkeypatch):
        """The whole point: a degraded machine says so, and says why."""
        _break_embeddings(monkeypatch)
        memory_save(title="Note", content="content that will not be embedded")

        check = _doctor_capability("semantic_search")
        assert check["status"] == "MISCONFIGURED"
        assert "403 Forbidden" in check["detail"]
        assert "FTS-only" in check["detail"]

    def test_misconfigured_on_a_backlog_with_no_failure_this_process(self, monkeypatch):
        """Survives a restart: the process has no recorded failure, but the
        un-embedded rows a previous one left behind still report degraded."""
        _break_embeddings(monkeypatch)
        memory_save(title="Note", content="left unembedded by an earlier process")
        embeddings.reset_embedding_state()

        check = _doctor_capability("semantic_search")
        assert check["status"] == "MISCONFIGURED"
        assert "1 of 1 notes have no embedding" in check["detail"]

    def test_live_when_every_note_is_embedded(self, monkeypatch):
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        embeddings._record_success(384)
        memory_save(title="Embedded", content="this one gets a vector")

        check = _doctor_capability("semantic_search")
        assert check["status"] == "LIVE"
        assert "all 1 notes embedded" in check["detail"]


class TestAcquisitionErrorClassification:
    """A regression must never be excused as an unavailable host (PR #183 review).

    The skip path exists so a host with no model reports "not run" instead of
    24 assertion failures. Catching every `Exception` to get there collapsed
    that into its opposite: a genuine bug in `generate_embedding` would also be
    read as "model unavailable", skip the whole semantic tier, and leave CI
    green — a regression reported as a clean run.
    """

    @staticmethod
    def _raised_in(phase: str, exc: BaseException) -> BaseException:
        """An exception as it would escape `generate_embedding` from `phase`."""
        embeddings._tag_phase(exc, phase)
        return exc

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("cache directory is not readable"),
            TimeoutError("model download timed out"),
            ValueError("Could not load model Qdrant/bge-small-en-v1.5-onnx-Q from any source."),
        ],
        ids=["unreadable-cache", "timeout", "fastembed-all-sources-exhausted"],
    )
    def test_builtin_failures_while_fetching_are_acquisition(self, exc):
        assert (
            embeddings.is_model_acquisition_error(self._raised_in(embeddings.PHASE_ACQUIRE, exc))
            is True
        )

    def test_transport_library_errors_are_acquisition(self):
        """The real observed failure: httpx.ProxyError, which is NOT an OSError,
        so a builtin-only allowlist would misclassify the actual blocked host."""
        httpx = pytest.importorskip("httpx")
        assert not isinstance(httpx.ProxyError("403 Forbidden"), OSError)
        exc = self._raised_in(embeddings.PHASE_ACQUIRE, httpx.ProxyError("403 Forbidden"))
        assert embeddings.is_model_acquisition_error(exc) is True

    @pytest.mark.parametrize(
        "make_error",
        [
            lambda: pytest.importorskip("httpx").InvalidURL("not a URL"),
            lambda: pytest.importorskip("requests").exceptions.InvalidURL("not a URL"),
            lambda: pytest.importorskip("huggingface_hub.errors").HFValidationError(
                "invalid repo id"
            ),
        ],
        ids=["httpx-invalid-url", "requests-invalid-url", "huggingface-validation"],
    )
    def test_allowlisted_modules_still_require_transport_classes(self, make_error):
        """A module allowlist must not turn config/import defects into skips."""
        exc = self._raised_in(embeddings.PHASE_ACQUIRE, make_error())
        assert embeddings.is_model_acquisition_error(exc) is False

    @pytest.mark.parametrize(
        "exc",
        [
            AssertionError("regression"),
            TypeError("expected a str"),
            KeyError("dimensions"),
        ],
        ids=["assertion", "type", "key"],
    )
    def test_defects_are_not_acquisition(self, exc):
        assert embeddings.is_model_acquisition_error(exc) is False

    def test_unsupported_model_valueerror_while_fetching_is_not_acquisition(self):
        """UA_EMBED_MODEL naming a model fastembed does not support is a config
        error, not an unreachable host: only the all-sources message skips."""
        exc = self._raised_in(
            embeddings.PHASE_ACQUIRE,
            ValueError("Model not-a-real/model is not supported in TextEmbedding."),
        )
        assert embeddings.is_model_acquisition_error(exc) is False

    def test_valueerror_during_inference_is_not_acquisition(self):
        """ValueError while *fetching* the model is fastembed's all-sources-
        exhausted raise (acquisition); the same type during inference is a
        defect. Only the phase separates them, as with OSError/TimeoutError."""
        exc = self._raised_in(embeddings.PHASE_INFER, ValueError("embedding had the wrong shape"))
        assert embeddings.is_model_acquisition_error(exc) is False

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("device error during inference"),
            TimeoutError("inference timed out"),
        ],
        ids=["oserror", "timeout"],
    )
    def test_the_same_builtin_during_inference_is_a_defect(self, exc):
        """Type alone cannot classify: an OSError while fetching the model
        means the host cannot get it, and an OSError during inference means the
        code is broken. Only the phase separates them."""
        assert (
            embeddings.is_model_acquisition_error(self._raised_in(embeddings.PHASE_INFER, exc))
            is False
        )

    def test_importerror_is_never_acquisition(self):
        """fastembed is a required dependency, so failing to import it is a
        broken installation — loud, not excused as an unavailable host."""
        exc = self._raised_in(embeddings.PHASE_IMPORT, ImportError("No module named 'fastembd'"))
        assert embeddings.is_model_acquisition_error(exc) is False

    def test_an_untagged_exception_is_not_acquisition(self):
        """Nothing that never went through generate_embedding can claim the
        skip, so the classifier fails closed."""
        assert embeddings.is_model_acquisition_error(OSError("stray")) is False

    def test_a_wrapped_download_failure_is_still_acquisition(self):
        """Libraries re-raise; the cause chain must be followed or a genuine
        offline host would be misread as a bug and fail instead of skipping."""
        try:
            try:
                raise OSError("connection reset while fetching model")
            except OSError as cause:
                raise RuntimeError("could not initialise TextEmbedding") from cause
        except RuntimeError as exc:
            tagged = self._raised_in(embeddings.PHASE_ACQUIRE, exc)
            assert embeddings.is_model_acquisition_error(tagged) is True

    def test_a_wrapped_defect_is_not_laundered_into_acquisition(self):
        exc = self._raised_in(
            embeddings.PHASE_ACQUIRE, RuntimeError("wrapper", AssertionError("regression"))
        )
        assert embeddings.is_model_acquisition_error(exc) is False

    def test_phase_is_stamped_by_the_real_code_path(self, monkeypatch):
        """Ties the tags above to production rather than to this test's helper:
        a failure inside model.embed() must come back tagged `infer`."""

        class _Model:
            def embed(self, _texts):
                raise OSError("device error during inference")

        monkeypatch.setattr(embeddings, "get_embedding_model", lambda: _Model())
        with pytest.raises(OSError) as excinfo:
            embeddings.generate_embedding("text")

        assert embeddings._phase_of(excinfo.value) == embeddings.PHASE_INFER
        assert embeddings.is_model_acquisition_error(excinfo.value) is False

    def test_a_broken_fastembed_import_is_tagged_import_by_the_real_path(self, monkeypatch):
        """The fallback tag in generate_embedding must not launder this into
        `acquire`: a failed import of a required dependency has to stay loud."""
        import builtins

        real_import = builtins.__import__

        def _fail_fastembed(name, *args, **kwargs):
            if name == "fastembed":
                raise ImportError("No module named 'fastembd'", name="fastembd")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(embeddings, "_model", None)
        monkeypatch.setattr(builtins, "__import__", _fail_fastembed)

        with pytest.raises(ImportError) as excinfo:
            embeddings.generate_embedding("text")

        assert embeddings._phase_of(excinfo.value) == embeddings.PHASE_IMPORT
        assert embeddings.is_model_acquisition_error(excinfo.value) is False

    def test_probe_treats_a_regression_as_available(self, monkeypatch):
        """conftest's probe must let the tier run when the failure is a defect.

        Reproduces the review's case directly: `AssertionError("regression")`
        used to yield SKIP.
        """
        import conftest

        def _regression(_text):
            raise AssertionError("regression")

        monkeypatch.setattr(embeddings, "generate_embedding", _regression)
        monkeypatch.setattr(conftest, "_EMBEDDINGS_AVAILABLE", None)
        monkeypatch.setattr(conftest, "_EMBEDDINGS_ERROR", "")

        available, error = conftest.embeddings_available()
        assert available is True, "a regression must not be reported as an unavailable model"
        assert error == ""

    def test_probe_treats_a_download_failure_as_unavailable(self, monkeypatch):
        """Breaks the *model fetch*, not `generate_embedding`, so the real code
        path does the phase tagging — the probe must see a genuine offline host
        through production logic, not through a hand-tagged exception."""
        import conftest

        def _offline():
            raise OSError("connection reset while fetching model")

        monkeypatch.setattr(embeddings, "_model", None)
        monkeypatch.setattr(embeddings, "get_embedding_model", _offline)
        monkeypatch.setattr(conftest, "_EMBEDDINGS_AVAILABLE", None)
        monkeypatch.setattr(conftest, "_EMBEDDINGS_ERROR", "")

        available, error = conftest.embeddings_available()
        assert available is False
        assert "connection reset" in error


class TestSearchIndexMirror:
    """A full memory_embeddings table does not prove search works (review).

    With sqlite-vec loaded, `_semantic_similarity_rows` reads `memory_vec` and
    returns `[]` outright when it is empty — the UDF fallback fires only on
    `OperationalError`, which a present-but-empty table does not raise. Since
    `upsert_memory_vec` swallows its failures, the mirror can silently fall
    behind while every other signal reads healthy.
    """

    @staticmethod
    def _clear_mirror() -> bool:
        """Empty memory_vec. False when sqlite-vec is not loaded here."""
        conn = storage.connect()
        try:
            conn.execute("DELETE FROM memory_vec")
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def test_search_returns_nothing_when_the_mirror_is_empty(self, monkeypatch):
        """The behaviour that makes this worth reporting at all."""
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Worktrees", content="git worktrees allow parallel checkouts")
        if not self._clear_mirror():
            pytest.skip("sqlite-vec not loaded; the UDF path serves searches here")

        conn = storage.connect()
        try:
            from unified_agent.tools.memory import _semantic_similarity_rows

            rows = _semantic_similarity_rows(conn, embeddings.embedding_to_bytes([0.1] * 384))
        finally:
            conn.close()
        assert rows == [], "an empty mirror yields no semantic results, without falling back"

    def test_coverage_reports_the_mirror_gap(self, monkeypatch):
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="content")
        if not self._clear_mirror():
            pytest.skip("sqlite-vec not loaded on this host")

        coverage = storage.embedding_coverage()
        assert coverage["missing"] == 0, "the source of truth is complete"
        assert coverage["mirror_available"] is True
        assert coverage["mirror_missing"] == 1, "but the search index is not"

    def test_doctor_reports_misconfigured_not_live(self, monkeypatch):
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="content")
        embeddings._record_success(384)
        if not self._clear_mirror():
            pytest.skip("sqlite-vec not loaded on this host")

        check = _doctor_capability("semantic_search")
        assert check["status"] == "MISCONFIGURED", "was LIVE before the fix"
        assert "memory_vec search index" in check["detail"]
        assert "returns no results at all" in check["detail"]

    def test_live_once_the_mirror_is_populated(self, monkeypatch):
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="content")
        embeddings._record_success(384)

        coverage = storage.embedding_coverage()
        assert coverage["mirror_missing"] == 0
        if coverage["mirror_available"]:
            assert coverage["mirror_usable"] is True
        assert _doctor_capability("semantic_search")["status"] == "LIVE"

    def test_doctor_rejects_an_unusable_present_index(self, monkeypatch):
        """A present vec0 table must answer a probe before LIVE is allowed."""
        monkeypatch.setattr(
            storage,
            "embedding_coverage",
            lambda: {
                "total": 1,
                "embedded": 1,
                "missing": 0,
                "mirror_available": True,
                "mirror_missing": 0,
                "mirror_usable": False,
                "mirror_error": "invalid float32 vector BLOB length",
            },
        )
        check = _doctor_capability("semantic_search")
        assert check["status"] == "MISCONFIGURED"
        assert "KNN probe" in check["detail"]

    def test_coverage_marks_a_failed_knn_probe_unusable(self, monkeypatch):
        """Coverage must exercise the real vec0 path, not only count rows."""
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="content")
        if not storage.embedding_coverage()["mirror_available"]:
            pytest.skip("sqlite-vec not loaded on this host")

        real_conn = storage.connect()

        class _BrokenKnnConnection:
            def execute(self, sql, params=()):
                if "embedding MATCH" in sql:
                    raise sqlite3.OperationalError("invalid vector in search index")
                return real_conn.execute(sql, params)

            def close(self):
                real_conn.close()

        monkeypatch.setattr(storage, "connect", lambda: _BrokenKnnConnection())
        coverage = storage.embedding_coverage()
        assert coverage["mirror_available"] is True
        assert coverage["mirror_usable"] is False
        assert "invalid vector" in coverage["mirror_error"]


class TestUnverifiedAfterRestart:
    """A fresh process cannot claim LIVE on the strength of old rows (review).

    Everything on record being embedded says the model worked *once*, on some
    earlier process. If it went away since, queries fall back to FTS and the
    corpus still looks complete, so LIVE would be a false all-clear.
    """

    def test_unknown_when_nothing_has_embedded_this_process(self, monkeypatch):
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="embedded by an earlier process")
        embeddings.reset_embedding_state()  # the restart

        check = _doctor_capability("semantic_search")
        assert check["status"] == "UNKNOWN", "was a false LIVE before the fix"
        assert "readiness unverified since restart" in check["detail"]

    def test_first_success_resolves_it_to_live(self, monkeypatch):
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="embedded by an earlier process")
        embeddings.reset_embedding_state()
        assert _doctor_capability("semantic_search")["status"] == "UNKNOWN"

        embeddings._record_success(384)
        assert _doctor_capability("semantic_search")["status"] == "LIVE"

    def test_first_failure_resolves_it_to_misconfigured(self, monkeypatch):
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="embedded by an earlier process")
        embeddings.reset_embedding_state()

        embeddings._record_failure(RuntimeError("403 Forbidden"))
        check = _doctor_capability("semantic_search")
        assert check["status"] == "MISCONFIGURED"
        assert "403 Forbidden" in check["detail"]


class TestDegradationAfterEarlierSuccess:
    """Success-then-failure with no coverage backlog (PR #183 review).

    The first cut gated the failure signal on `failures and not successes`, so a
    host that embedded fine and then lost the model reported LIVE. The gap is
    not hypothetical or narrow: embedding a *search query* is the most common
    call in the system and leaves no un-embedded note behind, so coverage — the
    other signal — stays clean while every query silently falls back to FTS.
    """

    def test_query_side_failure_leaves_no_backlog_to_notice(self, monkeypatch):
        """The precondition that made LIVE reachable: coverage stays clean."""
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="content worth finding")
        assert storage.embedding_coverage()["missing"] == 0

        _break_embeddings(monkeypatch)
        memory_search(query="content")

        assert storage.embedding_coverage()["missing"] == 0, (
            "a failed query embedding creates no un-embedded note, which is why "
            "coverage alone cannot detect this"
        )

    def test_doctor_reports_misconfigured_not_live(self, monkeypatch):
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="content worth finding")
        embeddings._record_success(384)

        _break_embeddings(monkeypatch)
        memory_search(query="content")

        check = _doctor_capability("semantic_search")
        assert check["status"] == "MISCONFIGURED", "was LIVE before the fix"
        assert "403 Forbidden" in check["detail"]
        assert "FTS-only" in check["detail"]

    def test_state_marks_degraded_on_failure_after_success(self):
        embeddings._record_success(384)
        assert embeddings.embedding_state()["degraded"] is False
        embeddings._record_failure(RuntimeError("403 Forbidden"))
        assert embeddings.embedding_state()["degraded"] is True

    def test_a_later_success_clears_degradation_without_a_restart(self, monkeypatch):
        """Recovery is a real subsequent success, not a process restart — a
        transient blip must not pin the host to MISCONFIGURED forever."""
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 384)
        memory_save(title="Note", content="content")
        embeddings._record_failure(RuntimeError("transient blip"))
        assert embeddings.embedding_state()["degraded"] is True

        embeddings._record_success(384)
        assert embeddings.embedding_state()["degraded"] is False

        check = _doctor_capability("semantic_search")
        assert check["status"] == "LIVE"
        assert "recovered after 1 earlier failure" in check["detail"]


class TestEmbedModelPin:
    def test_default_model_matches_fastembed_default(self, monkeypatch):
        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        assert config_embed() == "BAAI/bge-small-en-v1.5"
        assert embeddings.embedding_state()["model"] == "BAAI/bge-small-en-v1.5"

    def test_text_embedding_receives_model_name(self, monkeypatch):
        """Construction must pass UA_EMBED_MODEL; no model download."""
        import sys
        from types import ModuleType

        monkeypatch.setenv("UA_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
        captured: dict[str, str] = {}

        class _Fake:
            def __init__(self, model_name: str = "", **_kwargs):
                captured["model_name"] = model_name

        fake = ModuleType("fastembed")
        fake.TextEmbedding = _Fake  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fastembed", fake)
        embeddings._model = None
        embeddings._loaded_model_name = None
        try:
            embeddings.get_embedding_model()
        finally:
            embeddings._model = None
            embeddings._loaded_model_name = None
        assert captured["model_name"] == "BAAI/bge-small-en-v1.5"

    def test_model_column_migration_leaves_existing_blobs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path / "home"))
        storage.init_db()
        storage.join_init_background_threads()
        blob = b"\x00\x01\x02\x03legacy-vector"
        conn = storage.connect()
        try:
            conn.execute("DROP TABLE memory_embeddings")
            conn.execute(
                "CREATE TABLE memory_embeddings (note_id TEXT PRIMARY KEY, embedding BLOB NOT NULL)"
            )
            conn.execute(
                "INSERT INTO memory_embeddings (note_id, embedding) VALUES (?, ?)",
                ("legacy", blob),
            )
            conn.commit()
            storage._migrate(conn)
            conn.commit()
            row = conn.execute(
                "SELECT embedding, model FROM memory_embeddings WHERE note_id='legacy'"
            ).fetchone()
            assert bytes(row["embedding"]) == blob
            assert row["model"] is None
            storage._migrate(conn)
            again = conn.execute(
                "SELECT embedding, model FROM memory_embeddings WHERE note_id='legacy'"
            ).fetchone()
            assert bytes(again["embedding"]) == blob
            assert again["model"] is None
        finally:
            conn.close()

    def test_doctor_reports_stored_model_mismatch(self, monkeypatch):
        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 8)
        note = memory_save(title="Pinned", content="a semantic note")
        conn = storage.connect()
        try:
            before = conn.execute(
                "SELECT embedding FROM memory_embeddings WHERE note_id=?", (note["id"],)
            ).fetchone()[0]
            conn.execute(
                "UPDATE memory_embeddings SET model=? WHERE note_id=?",
                ("sentence-transformers/all-MiniLM-L6-v2", note["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        check = _doctor_capability("semantic_search")
        assert check["status"] == "MISCONFIGURED"
        assert "sentence-transformers/all-MiniLM-L6-v2" in check["detail"]
        assert "UA_EMBED_MODEL" in check["detail"]
        assert "BAAI/bge-small-en-v1.5" in check["detail"]

        conn = storage.connect()
        try:
            after = conn.execute(
                "SELECT embedding FROM memory_embeddings WHERE note_id=?", (note["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert bytes(after) == bytes(before)

    def test_other_workspace_symbol_model_does_not_fail_semantic_search(self, monkeypatch):
        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 8)
        memory_save(title="Pinned", content="a semantic note")
        embeddings._record_success(8)
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO workspace_symbols "
                "(workspace, file_path, symbol_name, symbol_type, line_no) "
                "VALUES ('/other/workspace', 'other.py', 'Old', 'function', 1)"
            )
            symbol_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) "
                "VALUES (?, ?, 'other/model')",
                (symbol_id, embeddings.embedding_to_bytes([0.1] * 8)),
            )
            conn.commit()
        finally:
            conn.close()

        semantic = _doctor_capability("semantic_search")
        assert "other/model" not in semantic["detail"]
        assert "stored embedding model id" not in semantic["detail"]
        symbol = _doctor_capability("symbol_embeddings")
        assert symbol["status"] == "DORMANT"
        assert "other/model" not in symbol["detail"]

    def test_current_workspace_symbol_model_is_a_separate_check(self, monkeypatch):
        from unified_agent.safety import resolve_in_workspace

        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1] * 8)
        memory_save(title="Pinned", content="a semantic note")
        embeddings._record_success(8)
        workspace = str(resolve_in_workspace("."))
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO workspace_symbols "
                "(workspace, file_path, symbol_name, symbol_type, line_no) "
                "VALUES (?, 'here.py', 'Here', 'function', 1)",
                (workspace,),
            )
            symbol_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) "
                "VALUES (?, ?, 'other/model')",
                (symbol_id, embeddings.embedding_to_bytes([0.1] * 8)),
            )
            conn.commit()
        finally:
            conn.close()

        semantic = _doctor_capability("semantic_search")
        assert "other/model" not in semantic["detail"]
        symbol = _doctor_capability("symbol_embeddings")
        assert symbol["status"] == "MISCONFIGURED"
        assert "other/model" in symbol["detail"]
        assert "UA_EMBED_MODEL" in symbol["detail"]

    def test_legacy_only_symbol_rows_count_as_default_model_coverage(self, monkeypatch):
        from unified_agent.safety import resolve_in_workspace

        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        workspace = str(resolve_in_workspace("."))
        blob = embeddings.embedding_to_bytes([0.1] * 8)
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO workspace_symbols "
                "(workspace, file_path, symbol_name, symbol_type, line_no) "
                "VALUES (?, 'legacy.py', 'Legacy', 'function', 1)",
                (workspace,),
            )
            symbol_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) "
                "VALUES (?, ?, NULL)",
                (symbol_id, blob),
            )
            conn.commit()
        finally:
            conn.close()

        symbol = _doctor_capability("symbol_embeddings")
        assert symbol["status"] == "LIVE"
        assert symbol["status"] != "DORMANT"
        assert "unlabeled" in symbol["detail"]
        assert "compatible" in symbol["detail"]
        assert "BAAI/bge-small-en-v1.5" in symbol["detail"]

    def test_legacy_symbol_rows_are_excluded_when_the_model_changes(self, monkeypatch):
        from unified_agent.safety import resolve_in_workspace

        monkeypatch.setenv("UA_EMBED_MODEL", "other/model")
        workspace = str(resolve_in_workspace("."))
        blob = embeddings.embedding_to_bytes([0.1] * 8)
        conn = storage.connect()
        try:
            for name, model in (("Current", "other/model"), ("Legacy", None)):
                conn.execute(
                    "INSERT INTO workspace_symbols "
                    "(workspace, file_path, symbol_name, symbol_type, line_no) "
                    "VALUES (?, ?, ?, 'function', 1)",
                    (workspace, f"{name}.py", name),
                )
                symbol_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                conn.execute(
                    "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) "
                    "VALUES (?, ?, ?)",
                    (symbol_id, blob, model),
                )
            conn.commit()
        finally:
            conn.close()

        symbol = _doctor_capability("symbol_embeddings")
        assert symbol["status"] == "MISCONFIGURED"
        assert symbol["status"] != "LIVE"
        assert "1 of 2" in symbol["detail"]
        assert "excluded" in symbol["detail"]
        assert "other/model" in symbol["detail"]

    def test_blank_symbol_model_is_excluded_not_legacy_coverage(self, monkeypatch):
        """Blank is not NULL. Only NULL is legacy compatible coverage, so an
        all-blank workspace must not report LIVE and semantic search falls
        back to name matching."""
        from unified_agent.safety import resolve_in_workspace
        from unified_agent.tools.indexer import fs_search_symbols

        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [1.0, 0.0, 0.0, 0.0])
        workspace = str(resolve_in_workspace("."))
        blob = embeddings.embedding_to_bytes([1.0, 0.0, 0.0, 0.0])
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO workspace_symbols "
                "(workspace, file_path, symbol_name, symbol_type, line_no) "
                "VALUES (?, 'blank.py', 'BlankSym', 'function', 1)",
                (workspace,),
            )
            symbol_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) "
                "VALUES (?, ?, '')",
                (symbol_id, blob),
            )
            conn.commit()
            coverage = storage.symbol_embedding_coverage(workspace)
            assert coverage == {"total": 1, "legacy": 0, "blank": 1, "models": []}

            symbol = _doctor_capability("symbol_embeddings")
            assert symbol["status"] == "MISCONFIGURED"
            assert symbol["status"] != "LIVE"
            assert "blank" in symbol["detail"]
            assert "excluded" in symbol["detail"]
            assert "compatible" not in symbol["detail"]

            found = fs_search_symbols("BlankSym", semantic=True)
            matched = next(row for row in found["results"] if row["name"] == "BlankSym")
            assert "similarity" not in matched

            conn.execute(
                "INSERT INTO workspace_symbols "
                "(workspace, file_path, symbol_name, symbol_type, line_no) "
                "VALUES (?, 'spaces.py', 'Spaces', 'function', 2)",
                (workspace,),
            )
            spaces_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) "
                "VALUES (?, ?, '   ')",
                (spaces_id, blob),
            )
            conn.execute(
                "INSERT INTO workspace_symbols "
                "(workspace, file_path, symbol_name, symbol_type, line_no) "
                "VALUES (?, 'null.py', 'NullOnly', 'function', 3)",
                (workspace,),
            )
            null_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) "
                "VALUES (?, ?, NULL)",
                (null_id, blob),
            )
            conn.commit()
            mixed = storage.symbol_embedding_coverage(workspace)
            assert mixed["total"] == 3
            assert mixed["legacy"] == 1
            assert mixed["blank"] == 2
            assert mixed["models"] == []
            mixed_check = _doctor_capability("symbol_embeddings")
            assert mixed_check["status"] == "MISCONFIGURED"
            assert mixed_check["status"] != "LIVE"
            assert "2 of 3" in mixed_check["detail"]
            assert "blank" in mixed_check["detail"]

            conn.execute(
                "INSERT INTO workspace_symbols "
                "(workspace, file_path, symbol_name, symbol_type, line_no) "
                "VALUES (?, 'tab.py', 'TabOnly', 'function', 4)",
                (workspace,),
            )
            tab_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) "
                "VALUES (?, ?, '\t\n')",
                (tab_id, blob),
            )
            conn.commit()
            whitespace = storage.symbol_embedding_coverage(workspace)
            assert whitespace == {"total": 4, "legacy": 1, "blank": 3, "models": []}
            monkeypatch.setenv("UA_EMBED_MODEL", "other/model")
            nondefault_check = _doctor_capability("symbol_embeddings")
            assert nondefault_check["status"] == "MISCONFIGURED"
            assert "3 of 4" in nondefault_check["detail"]
            assert "1 unlabeled legacy rows are also excluded" in nondefault_check["detail"]
        finally:
            conn.close()

    def test_unlabeled_rows_are_not_a_mismatch(self, monkeypatch):
        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.2] * 8)
        note = memory_save(title="Legacy", content="unlabeled vector")
        conn = storage.connect()
        try:
            conn.execute("UPDATE memory_embeddings SET model=NULL WHERE note_id=?", (note["id"],))
            conn.commit()
        finally:
            conn.close()
        embeddings._record_success(8)
        check = _doctor_capability("semantic_search")
        assert "stored embedding model id" not in check["detail"]
        assert storage.embedding_coverage()["legacy_excluded"] == 0
        assert "unlabeled" not in check["detail"]

    def test_doctor_counts_unlabeled_rows_when_the_model_is_not_the_default(self, monkeypatch):
        """NULL-model rows stay compatible on the default model and drop out
        on any other. Doctor must not call that LIVE."""
        monkeypatch.setenv("UA_EMBED_MODEL", "other/model")
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.2] * 8)
        note = memory_save(title="Legacy", content="unlabeled vector")
        conn = storage.connect()
        try:
            conn.execute("UPDATE memory_embeddings SET model=NULL WHERE note_id=?", (note["id"],))
            conn.commit()
        finally:
            conn.close()
        embeddings._record_success(8)
        coverage = storage.embedding_coverage()
        assert coverage["missing"] == 0
        assert coverage["legacy_excluded"] == 1
        check = _doctor_capability("semantic_search")
        assert check["status"] == "MISCONFIGURED"
        assert "unlabeled" in check["detail"]
        assert "other/model" in check["detail"]
        assert check["status"] != "LIVE"

    def test_semantic_scan_skips_a_different_model(self, monkeypatch):
        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [1.0, 0.0, 0.0, 0.0])
        same = memory_save(title="Same model", content="alpha uniquephrase")
        other = memory_save(title="Other model", content="beta uniquephrase")
        from unified_agent.tools.memory import _semantic_similarity_rows

        conn = storage.connect()
        try:
            blob = conn.execute(
                "SELECT embedding FROM memory_embeddings WHERE note_id=?", (same["id"],)
            ).fetchone()["embedding"]
            conn.execute(
                "UPDATE memory_embeddings SET model=? WHERE note_id=?",
                ("other/model", other["id"]),
            )
            conn.commit()
            conn.execute("DROP TABLE IF EXISTS memory_vec")
            rows = _semantic_similarity_rows(conn, blob, exclude_index=True)
        finally:
            conn.close()
        ids = {row["note_id"] for row in rows}
        assert same["id"] in ids
        assert other["id"] not in ids

    def test_legacy_null_rows_drop_out_when_the_model_changes(self, monkeypatch):
        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.0, 1.0, 0.0, 0.0])
        note = memory_save(title="Legacy", content="gamma uniquephrase")
        from unified_agent.tools.memory import _semantic_similarity_rows

        conn = storage.connect()
        try:
            blob = conn.execute(
                "SELECT embedding FROM memory_embeddings WHERE note_id=?", (note["id"],)
            ).fetchone()["embedding"]
            conn.execute("UPDATE memory_embeddings SET model=NULL WHERE note_id=?", (note["id"],))
            conn.commit()
            conn.execute("DROP TABLE IF EXISTS memory_vec")
            included = {row["note_id"] for row in _semantic_similarity_rows(conn, blob)}
            monkeypatch.setenv("UA_EMBED_MODEL", "other/model")
            excluded = {row["note_id"] for row in _semantic_similarity_rows(conn, blob)}
        finally:
            conn.close()
        assert note["id"] in included
        assert note["id"] not in excluded

    def test_non_384_query_scans_embeddings_when_the_mirror_is_empty(self, monkeypatch):
        """memory_vec stays 384-wide. A different width must not treat an empty
        mirror as the whole result."""
        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.2] * 8)
        note = memory_save(title="Wide", content="delta uniquephrase")
        from unified_agent.tools.memory import _semantic_similarity_rows

        conn = storage.connect()
        try:
            blob = conn.execute(
                "SELECT embedding FROM memory_embeddings WHERE note_id=?", (note["id"],)
            ).fetchone()["embedding"]
            assert len(blob) != storage.MEMORY_VEC_DIM * 4
            try:
                conn.execute("DELETE FROM memory_vec")
                conn.commit()
            except Exception:
                pass
            rows = _semantic_similarity_rows(conn, blob)
        finally:
            conn.close()
        matched = [row for row in rows if row["note_id"] == note["id"]]
        assert matched
        assert matched[0]["similarity"] > 0.9


def config_embed() -> str:
    from unified_agent.config import embed_model

    return embed_model()


class TestSymbolEmbeddingDenyBinding:
    """A caller with an explicit UA_AGENT_WORKSPACES DENY binding still gets the
    hub's own symbol-index row (task #1779). The doctor is hub-owned census, so
    it reads the process workspace like the worktree survey does, rather than
    reporting MISCONFIGURED because of who asked."""

    def _deny(self):
        from unified_agent import config

        return config.workspace_root_var.set(config.WORKSPACE_DENIED)

    def test_deny_binding_with_no_rows_is_dormant_not_misconfigured(self):
        from unified_agent import config

        token = self._deny()
        try:
            symbol = _doctor_capability("symbol_embeddings")
        finally:
            config.workspace_root_var.reset(token)
        assert symbol["status"] == "DORMANT"
        assert "denied" not in symbol["detail"]

    def test_deny_binding_reads_the_process_workspace_index(self, monkeypatch):
        from unified_agent import config

        monkeypatch.delenv("UA_EMBED_MODEL", raising=False)
        workspace = str(config.process_workspace_root().resolve())
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO workspace_symbols "
                "(workspace, file_path, symbol_name, symbol_type, line_no) "
                "VALUES (?, 'hub.py', 'Hub', 'function', 1)",
                (workspace,),
            )
            symbol_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) "
                "VALUES (?, ?, 'other/model')",
                (symbol_id, embeddings.embedding_to_bytes([0.1] * 8)),
            )
            conn.commit()
        finally:
            conn.close()

        token = self._deny()
        try:
            symbol = _doctor_capability("symbol_embeddings")
        finally:
            config.workspace_root_var.reset(token)
        assert symbol["status"] == "MISCONFIGURED"
        assert "other/model" in symbol["detail"]
        assert "denied" not in symbol["detail"]


def test_concurrent_generate_embedding_calls_never_overlap(monkeypatch):
    """Inference on the shared model is serialized (task #1833).

    The fastembed tokenizer is not thread-safe; overlapping embed() calls from
    the background backfills segfaulted a CI shard. The fake model records how
    many calls are inside embed() at once; it must never exceed one.
    """
    import threading
    import time

    state = {"inside": 0, "max": 0}
    guard = threading.Lock()

    class _Model:
        def embed(self, texts):
            with guard:
                state["inside"] += 1
                state["max"] = max(state["max"], state["inside"])
            time.sleep(0.01)
            with guard:
                state["inside"] -= 1
            return iter([[0.0, 1.0] for _ in texts])

    monkeypatch.setattr(embeddings, "get_embedding_model", lambda: _Model())
    barrier = threading.Barrier(8)
    results: list[list[float]] = []
    errors: list[BaseException] = []

    def _worker():
        try:
            barrier.wait()
            for _ in range(3):
                results.append(embeddings.generate_embedding("x"))
        except BaseException as exc:  # surfaced below; a thread cannot fail the test
            errors.append(exc)

    # Daemon workers: a deadlocked one fails the assertion below instead of
    # keeping the interpreter alive at shutdown.
    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a worker did not finish"
    assert errors == []
    assert results == [[0.0, 1.0]] * 24
    assert state["max"] == 1


def test_concurrent_cold_loads_construct_the_model_once(monkeypatch):
    """Callers racing on a cold cache share one construction.

    Each request that found no model built its own, with its own ONNX thread
    pool, while holding a database connection; a hub restart under load
    built a dozen at once. Eight racing callers must construct exactly one.
    """
    import sys
    import threading
    import time
    from types import ModuleType

    built: list[int] = []
    guard = threading.Lock()

    class _Fake:
        def __init__(self, model_name: str = "", **_kwargs):
            with guard:
                built.append(1)
            time.sleep(0.05)

    fake = ModuleType("fastembed")
    fake.TextEmbedding = _Fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_loaded_model_name", None)
    barrier = threading.Barrier(8)
    models: list[object] = []

    def _worker():
        barrier.wait()
        models.append(embeddings.get_embedding_model())

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(built) == 1
    assert len(models) == 8 and all(m is models[0] for m in models)
