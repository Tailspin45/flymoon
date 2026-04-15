"""Pinning tests for src.transit_classifier.

Hardware-free: no real ONNX model is required. The session is either
monkeypatched to a stub or the classifier is deliberately constructed
with a missing path so the not-available branches get exercised.
"""

import numpy as np
import pytest

from src import transit_classifier
from src.transit_classifier import (
    CLIP_H,
    CLIP_T,
    CLIP_W,
    TransitClassifier,
    get_classifier,
)


def test_module_shape_constants():
    """Guard against silent drift of the model input contract."""
    assert CLIP_T == 15
    assert CLIP_H == 160
    assert CLIP_W == 90


def test_unavailable_when_model_missing(tmp_path):
    """A non-existent model path must leave the classifier disabled and
    classify() must return the documented (False, 0.0) fallback."""
    clf = TransitClassifier(model_path=str(tmp_path / "does_not_exist.onnx"))
    assert clf.available is False
    result = clf.classify(np.zeros((CLIP_T, CLIP_H, CLIP_W), dtype=np.uint8))
    assert result == (False, 0.0)


def test_normalize_is_finite_on_constant_input():
    """The +1e-6 epsilon must guard against zero-variance input."""
    flat = np.full((CLIP_T, CLIP_H, CLIP_W), 5.0, dtype=np.float32)
    out = TransitClassifier._normalize(flat)
    assert np.isfinite(out).all()
    assert out.dtype == np.float32


def test_classify_with_mocked_session(tmp_path):
    """Inject a stub ONNX session; verify softmax + threshold logic."""
    clf = TransitClassifier(model_path=str(tmp_path / "nope.onnx"))
    assert clf.available is False  # precondition: real load failed

    class _StubSession:
        def run(self, output_names, inputs):
            # logits for [no_transit=0.1, transit=2.0]
            return [np.array([[0.1, 2.0]], dtype=np.float32)]

    clf._session = _StubSession()
    assert clf.available is True

    frames = np.zeros((CLIP_T, CLIP_H, CLIP_W), dtype=np.uint8)
    is_transit, confidence = clf.classify(frames)

    # softmax(0.1, 2.0) = [e^-1.9, e^0] / (e^-1.9 + e^0) ≈ [0.1301, 0.8699]
    assert is_transit is True
    assert confidence == pytest.approx(0.8699, abs=1e-3)


def test_get_classifier_singleton(tmp_path):
    """get_classifier() must return the same instance on repeat calls."""
    # conftest autouse fixture resets _classifier between tests, so we
    # start from None. Passing a bogus path avoids actually loading ONNX.
    bogus = str(tmp_path / "bogus.onnx")
    a = get_classifier(model_path=bogus)
    b = get_classifier(model_path=bogus)
    assert a is b
    assert transit_classifier._classifier is a
