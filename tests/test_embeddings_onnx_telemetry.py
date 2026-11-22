"""ONNX Runtime telemetry is off before the embedding model is built (task #2164).

A review scan on the Mac hub hung at interpreter exit inside ONNX Runtime's
telemetry shutdown and held the review-pipeline lock for days. These tests use
stand-in modules, so they need no real model and carry no `embeddings` marker.
"""

import sys
import types

import pytest

from unified_agent import embeddings


@pytest.fixture(autouse=True)
def clean_embedding_state(monkeypatch):
    # get_embedding_model caches the built model; each test builds its own.
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_loaded_model_name", None)
    embeddings.reset_embedding_state()
    yield
    embeddings.reset_embedding_state()


def _install_fakes(monkeypatch, *, onnxruntime=True):
    calls = []

    class TextEmbedding:
        def __init__(self, model_name):
            calls.append(("construct", model_name))

    monkeypatch.setitem(
        sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=TextEmbedding)
    )
    if onnxruntime:
        ort = types.SimpleNamespace(
            disable_telemetry_events=lambda: calls.append(("disable_telemetry",))
        )
        monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    else:
        # A None entry makes `import onnxruntime` raise ImportError.
        monkeypatch.setitem(sys.modules, "onnxruntime", None)
    return calls


def test_telemetry_is_disabled_before_the_model_is_constructed(monkeypatch):
    calls = _install_fakes(monkeypatch)

    embeddings.get_embedding_model()

    assert calls[0] == ("disable_telemetry",)
    assert calls[1][0] == "construct"


def test_a_missing_onnxruntime_does_not_block_loading(monkeypatch):
    calls = _install_fakes(monkeypatch, onnxruntime=False)

    model = embeddings.get_embedding_model()

    assert model is not None
    assert [c[0] for c in calls] == ["construct"]


def test_a_failing_disable_call_does_not_block_loading(monkeypatch):
    calls = _install_fakes(monkeypatch)

    def _boom():
        raise RuntimeError("no telemetry in this build")

    monkeypatch.setitem(
        sys.modules, "onnxruntime", types.SimpleNamespace(disable_telemetry_events=_boom)
    )

    assert embeddings.get_embedding_model() is not None
    assert [c[0] for c in calls] == ["construct"]
