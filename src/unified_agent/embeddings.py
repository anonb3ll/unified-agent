"""Embedding utilities for semantic search using local CPU fastembed models."""

import logging
import threading
from datetime import UTC, datetime

from . import config

# The blob encoding and similarity math moved to core (task #1720) so storage
# can register the cosine_similarity() SQL function without importing the
# model client. Re-exported here so existing imports keep working.
from .vector_math import (  # noqa: F401
    bytes_to_embedding,
    cosine_similarity,
    cosine_similarity_bytes,
    embedding_to_bytes,
)

logger = logging.getLogger("unified_agent.embeddings")

_model = None
_loaded_model_name: str | None = None

# Observability for a tier that is designed to degrade silently.
#
# Every caller of generate_embedding() wraps it in a bare `except: pass` on
# purpose: a note must still save, and search must still work via FTS, when the
# model cannot load. That contract is right and is not changed here. What was
# wrong is that the degradation left no trace anywhere — no log line, no
# counter, and no system_doctor capability — so a machine that silently lost
# semantic search looked identical to one that never needed it.
#
# Recording here rather than at each call site is deliberate: there are five
# swallow sites across storage, memory and the symbol indexer, and a sixth
# added later would otherwise reintroduce the blind spot by default.
_state_lock = threading.Lock()
# Serializes inference on the shared cached model (task #1833). The background
# post-init backfills (memory, task search, symbol embeddings) and foreground
# callers share one fastembed model, and its Hugging Face tokenizer mutates
# padding/truncation state per call; concurrent embed() calls segfaulted a CI
# shard inside onnx_text_model.tokenize. One embed at a time costs little:
# a call is one text or one note's chunks, and the backfills already run serially.
_infer_lock = threading.Lock()
# Serializes construction of the cached model; see get_embedding_model.
_load_lock = threading.Lock()
_failure_count = 0
_success_count = 0
_last_error: str | None = None
_last_error_at: str | None = None
_last_success_at: str | None = None
_dimensions: int | None = None
# "success" | "failure" | None. Ordering, not just counts: a host that embedded
# fine and then lost the model has both, and only the order distinguishes it
# from one that failed once and recovered. Counting alone reported the first
# case as healthy for as long as the existing notes stayed embedded — which is
# every query-side failure, since embedding a *search query* leaves no
# un-embedded note behind to notice.
_last_outcome: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record_success(dimensions: int) -> None:
    global _success_count, _last_success_at, _dimensions, _last_outcome
    with _state_lock:
        _success_count += 1
        _last_success_at = _now()
        _dimensions = dimensions
        _last_outcome = "success"


def _record_failure(exc: BaseException) -> None:
    global _failure_count, _last_error, _last_error_at, _last_outcome
    with _state_lock:
        _failure_count += 1
        _last_error = f"{type(exc).__name__}: {exc}"
        _last_error_at = _now()
        _last_outcome = "failure"
        first = _failure_count == 1
    # First failure at warning, the rest at debug: the model is either
    # available or it is not, so a blocked host would otherwise emit one
    # warning per note saved for the life of the process.
    logger.log(
        logging.WARNING if first else logging.DEBUG,
        "embedding generation failed (%s); semantic search degrades to FTS for "
        "affected rows. Total failures this process: %d",
        f"{type(exc).__name__}: {exc}",
        _failure_count,
    )


def embedding_state() -> dict:
    """Observed embedding health for this process, without probing the model.

    Deliberately reports only what has actually happened. Probing here would
    make a read-only status call trigger a model download on a cold host, so
    `system_doctor` combines this with the count of un-embedded rows, which is
    plain SQL and needs no model.
    """
    with _state_lock:
        state = {
            "model_loaded": _model is not None,
            "model": config.embed_model(),
            "successes": _success_count,
            "failures": _failure_count,
            "last_error": _last_error,
            "last_error_at": _last_error_at,
            "last_success_at": _last_success_at,
            "dimensions": _dimensions,
            # True once anything has failed and nothing has succeeded since.
            # Clears on a genuine later success rather than on restart, so a
            # transient blip does not pin the host to "broken" forever — and a
            # host that is still failing cannot look healthy just because its
            # existing notes were embedded before the model went away.
            "degraded": _failure_count > 0 and _last_outcome != "success",
        }
    return state


def reset_embedding_state() -> None:
    """Clear recorded counters. For tests; never called in production paths."""
    global _failure_count, _success_count, _last_error, _last_error_at
    global _last_success_at, _dimensions, _last_outcome
    with _state_lock:
        _failure_count = 0
        _success_count = 0
        _last_error = None
        _last_error_at = None
        _last_success_at = None
        _dimensions = None
        _last_outcome = None


# Specific transport/cache exception classes that can escape while the model
# is being acquired. Matching a module root is deliberately not enough:
# httpx.InvalidURL and huggingface_hub.errors.HFValidationError are programming
# or configuration errors, not evidence that this host cannot fetch a model.
# Names are used instead of importing every optional transitive dependency at
# module import time.
_ACQUISITION_EXCEPTION_NAMES = {
    "httpx": frozenset(
        {
            "TransportError",
            "NetworkError",
            "ProxyError",
            "ConnectError",
            "ReadError",
            "WriteError",
            "TimeoutException",
            "ConnectTimeout",
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
            "ProtocolError",
            "LocalProtocolError",
            "RemoteProtocolError",
        }
    ),
    "httpcore": frozenset(
        {
            "NetworkError",
            "ProxyError",
            "ConnectError",
            "ReadError",
            "WriteError",
            "TimeoutException",
            "ConnectTimeout",
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
        }
    ),
    "requests": frozenset(
        {"ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "ChunkedEncodingError"}
    ),
    "urllib3": frozenset(
        {
            "NewConnectionError",
            "ConnectTimeoutError",
            "ReadTimeoutError",
            "TimeoutError",
            "ProtocolError",
            "SSLError",
            "NameResolutionError",
        }
    ),
    "urllib": frozenset({"URLError", "ContentTooShortError"}),
    "huggingface_hub": frozenset({"OfflineModeIsEnabled", "LocalEntryNotFoundError"}),
    "fsspec": frozenset({"FSTimeoutError"}),
    "socket": frozenset({"gaierror"}),
    "ssl": frozenset({"SSLError", "SSLZeroReturnError", "SSLEOFError", "SSLSyscallError"}),
}

# Builtins that can mean the model was not obtainable — but only when raised
# while fetching it (see `_PHASE_ATTR`). `ImportError` is deliberately absent:
# fastembed is a *required* dependency in pyproject.toml, so a failure to
# import it is a broken installation, which must be loud rather than excused
# as "this host has no model". A builtin `ValueError` counts only with
# fastembed's terminal all-sources message (e.g. HF rate-limiting with 429s):
# "Could not load model <name> from any source." The same type from
# construction also means "model not supported" (a bad UA_EMBED_MODEL), which
# is a configuration error and must fail, so the type alone is not enough.
_ACQUISITION_BUILTINS = (OSError, TimeoutError)
_ALL_SOURCES_EXHAUSTED = "from any source"

# Which phase of generate_embedding() an exception escaped from. Type alone is
# not enough to classify: an OSError raised while downloading the model means
# the host cannot get it, and an OSError raised during inference or float
# conversion means the code is broken. Both are OSError. Only the phase
# separates them, so it is stamped on the exception at the point it escapes.
_PHASE_ATTR = "_ua_embedding_phase"
PHASE_IMPORT = "import"  # importing fastembed itself — a required dependency
PHASE_ACQUIRE = "acquire"  # constructing TextEmbedding: fetch/cache/open
PHASE_INFER = "infer"  # embedding text and converting the result


def _tag_phase(exc: BaseException, phase: str) -> BaseException:
    """Stamp the phase, without overwriting one an inner frame already set."""
    if not hasattr(exc, _PHASE_ATTR):
        try:
            setattr(exc, _PHASE_ATTR, phase)
        except AttributeError:  # exotic exceptions with __slots__
            pass
    return exc


def _phase_of(exc: BaseException) -> str | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        phase = getattr(current, _PHASE_ATTR, None)
        if phase is not None:
            return phase
        current = current.__cause__ or current.__context__
    return None


def is_model_load_failure(exc: BaseException) -> bool:
    """True when `exc` escaped while importing or constructing the model.

    Unlike :func:`is_model_acquisition_error` this does not judge whether the
    failure is excusable; it only says the next row would fail the same way,
    so a backfill pass stops instead of retrying the model load once per row
    (task #1850).
    """
    return _phase_of(exc) in (PHASE_IMPORT, PHASE_ACQUIRE)


def is_model_acquisition_error(exc: BaseException) -> bool:
    """True when `exc` means the model was unavailable, not that we have a bug.

    The distinction decides whether a caller may downgrade a failure to "skip"
    (fastembed downloads its ONNX model on first use, so a locked-down host
    genuinely has nothing to exercise) or must let it fail. Catching every
    `Exception` collapses the two, and collapses them in the dangerous
    direction: a real regression in `generate_embedding` would be reported as
    "model unavailable", skip the entire semantic tier, and leave CI green.

    Two conditions, both required, because either alone is forgeable:

    1. The failure escaped the *acquisition* phase — constructing the model,
       which is where the download and cache read happen. A defect elsewhere
       in `generate_embedding` cannot reach this branch no matter what it
       raises, which is what makes a misspelled internal import or an
       inference-time `OSError` fail instead of skip.
    2. Its type is one of the named transport/cache failures below. A model
       whose construction fails on, say, an `AssertionError` or an invalid URL
       is a defect/configuration error, not an unreachable host.

    Wrapped exceptions are followed through `__cause__`/`__context__`, since a
    download failure commonly surfaces re-raised by a higher layer.
    """
    if _phase_of(exc) != PHASE_ACQUIRE:
        return False
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        module_root = type(current).__module__.split(".")[0]
        if module_root == "builtins" and (
            isinstance(current, _ACQUISITION_BUILTINS)
            or (isinstance(current, ValueError) and _ALL_SOURCES_EXHAUSTED in str(current))
        ):
            return True
        if type(current).__name__ in _ACQUISITION_EXCEPTION_NAMES.get(module_root, ()):
            return True
        # urllib3 wraps its connection failure in MaxRetryError rather than
        # preserving it as the exception type. Inspect only that wrapper's
        # reason; accepting every MaxRetryError would also skip HTTP/config
        # failures.
        if module_root == "urllib3" and type(current).__name__ == "MaxRetryError":
            reason = getattr(current, "reason", None)
            if reason is not None and reason is not current:
                current = reason
                continue
        current = current.__cause__ or current.__context__
    return False


def _disable_onnx_telemetry() -> None:
    """Turn off ONNX Runtime telemetry before the first session is built.

    Task #2164: a Mac review scan finished its work and then spun at
    interpreter exit for days inside ONNX Runtime's telemetry shutdown
    (`__cxa_finalize` -> OneDsTelemetry -> TransmissionPolicyManager::handleStop
    yielding forever), holding the review-pipeline lock so no later scan ran.
    Nothing here uses that telemetry. Best effort: a missing or older
    onnxruntime must not stop embeddings from loading.
    """
    try:
        import onnxruntime

        onnxruntime.disable_telemetry_events()
    except Exception as exc:  # noqa: BLE001 - telemetry is never load-bearing
        logger.debug("could not disable onnxruntime telemetry: %s", exc)


def get_embedding_model():
    """Lazy load the fastembed model on first use.

    The import and the construction are separate phases on purpose: the import
    is of a required dependency and a failure there is a broken install, while
    the construction is what reaches the network and the model cache.
    """
    global _model, _loaded_model_name
    name = config.embed_model()
    if _model is not None and _loaded_model_name == name:
        return _model
    try:
        from fastembed import TextEmbedding
    except Exception as exc:
        # Bare re-raise, never `from None`: suppressing the context would
        # discard the cause chain the classifier walks.
        _tag_phase(exc, PHASE_IMPORT)
        raise
    _disable_onnx_telemetry()
    # One construction at a time. Without this, every request that reached a
    # cold cache built its own model (each with its own ONNX thread pool) while
    # holding a database connection: a hub restart under load built a dozen.
    with _load_lock:
        if _model is not None and _loaded_model_name == name:
            return _model
        try:
            loaded = TextEmbedding(model_name=name)
        except Exception as exc:
            _tag_phase(exc, PHASE_ACQUIRE)
            raise
        _model = loaded
        _loaded_model_name = name
        return _model


def generate_embeddings(texts: list[str]) -> list[list[float]]:
    """Embed each text with one model call, in order.

    An empty list does not load the model. A failure is counted and logged
    once, then raised: callers own the decision to degrade, and a silently
    degraded host stays visible to `system_doctor`.
    """
    if not texts:
        return []
    try:
        model = get_embedding_model()
    except Exception as exc:
        # get_embedding_model stamps the precise phase (import vs acquire) and
        # _tag_phase never overwrites, so this is only a fallback for anything
        # that escapes it untagged. Defaulting to "acquire" is right because
        # everything on this side of the boundary is about obtaining the model;
        # an untagged ImportError cannot slip through as acquisition, since the
        # import is tagged at its own source before it can reach here.
        _tag_phase(exc, PHASE_ACQUIRE)
        _record_failure(exc)
        raise
    try:
        with _infer_lock:
            vectors = list(model.embed(texts))
        converted = [[float(x) for x in vector] for vector in vectors]
    except Exception as exc:
        # Inference and conversion: whatever this raises, the model itself was
        # obtained, so it can never be excused as an unavailable host.
        _tag_phase(exc, PHASE_INFER)
        _record_failure(exc)
        raise
    if len(converted) != len(texts):
        exc = RuntimeError(
            f"embedding model returned {len(converted)} vectors for {len(texts)} texts"
        )
        _tag_phase(exc, PHASE_INFER)
        _record_failure(exc)
        raise exc
    for vector in converted:
        _record_success(len(vector))
    return converted


def generate_embedding(text: str) -> list[float]:
    """Generate a vector embedding for the given text.

    Raises on failure exactly as before — callers own the decision to degrade —
    but the failure is now counted and logged first, so a silently degraded
    host is visible to `system_doctor` instead of only to whoever notices that
    semantic results stopped appearing.
    """
    return generate_embeddings([text])[0]
