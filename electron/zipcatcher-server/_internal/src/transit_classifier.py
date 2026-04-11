"""
E3 — ONNX inference wrapper for the TransitCNN classifier.

At runtime this module only requires `onnxruntime` (not PyTorch).
The model file is optional: if absent, `TransitClassifier.available` is False
and the caller should fall back to the existing threshold-based gates.

Model input:  (1, 1, 15, 160, 90)  float32, per-clip z-score normalised
Model output: (1, 2)  logits [no_transit, transit]
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Default model location (relative to repo root)
_DEFAULT_MODEL = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "models", "transit_classifier.onnx"
)

CLIP_T = 15  # frames per clip
CLIP_H = 160  # analysis height
CLIP_W = 90  # analysis width


class TransitClassifier:
    """
    Lightweight wrapper around the ONNX transit model.

    Parameters
    ----------
    model_path : str
        Path to `transit_classifier.onnx`.  Missing file → `available=False`.
    confidence_threshold : float
        Minimum transit probability required to accept a detection (default 0.5).
    """

    def __init__(
        self,
        model_path: str = _DEFAULT_MODEL,
        confidence_threshold: float = 0.5,
    ) -> None:
        self._session = None
        self._model_path = model_path
        self.confidence_threshold = confidence_threshold
        self._load_model()

    def _load_model(self) -> None:
        if not os.path.exists(self._model_path):
            logger.debug(
                "[CNN] Model not found at %s — classifier disabled", self._model_path
            )
            return
        try:
            import onnxruntime as ort  # noqa: F401 — lazy import

            session = ort.InferenceSession(
                self._model_path,
                providers=["CPUExecutionProvider"],
            )
            # Warm-up pass
            dummy = np.zeros((1, 1, CLIP_T, CLIP_H, CLIP_W), dtype=np.float32)
            session.run(None, {"frames": dummy})
            self._session = session
            logger.info("[CNN] TransitClassifier loaded from %s", self._model_path)
        except Exception as exc:
            logger.warning("[CNN] Failed to load model: %s", exc)

    @property
    def available(self) -> bool:
        """True only when the ONNX session is ready for inference."""
        return self._session is not None

    @staticmethod
    def _normalize(clip: np.ndarray) -> np.ndarray:
        """Per-clip z-score normalisation (float32, 0–1 range input)."""
        mu = clip.mean()
        std = clip.std() + 1e-6
        return ((clip - mu) / std).astype(np.float32)

    def classify(self, frames: np.ndarray) -> Tuple[bool, float]:
        """
        Classify a sequence of greyscale frames as transit / no-transit.

        Parameters
        ----------
        frames : np.ndarray
            Shape (T, H, W) uint8 or float32.  If T > CLIP_T, the last CLIP_T
            frames are used.  If T < CLIP_T, the first frame is repeated as padding.

        Returns
        -------
        (is_transit, confidence) : (bool, float)
            confidence is in [0, 1].  Returns (False, 0.0) if not available.
        """
        if not self.available:
            return False, 0.0

        try:
            # Shape normalisation
            arr = np.asarray(frames)
            if arr.ndim == 4:  # (T, H, W, C) — take first channel
                arr = arr[:, :, :, 0]
            if arr.ndim != 3:
                return False, 0.0

            # Temporal length normalisation
            t = arr.shape[0]
            if t < CLIP_T:
                pad = np.repeat(arr[:1], CLIP_T - t, axis=0)
                arr = np.concatenate([pad, arr], axis=0)
            arr = arr[-CLIP_T:]  # (CLIP_T, H, W)

            # Spatial resize if needed
            if arr.shape[1] != CLIP_H or arr.shape[2] != CLIP_W:
                import cv2

                arr = np.stack(
                    [
                        cv2.resize(
                            f.astype(np.float32),
                            (CLIP_W, CLIP_H),
                            interpolation=cv2.INTER_AREA,
                        )
                        for f in arr
                    ],
                    axis=0,
                )

            # Normalise to float32 [0, 1] then z-score
            if arr.dtype == np.uint8:
                arr = arr.astype(np.float32) / 255.0
            arr = self._normalize(arr)

            # Model input: (1, 1, T, H, W)
            x = arr[np.newaxis, np.newaxis, :, :, :]  # (1,1,T,H,W)
            logits = self._session.run(None, {"frames": x})[0][0]  # (2,)
            # Softmax
            e = np.exp(logits - logits.max())
            probs = e / e.sum()
            confidence = float(probs[1])  # probability of transit class
            is_transit = confidence >= self.confidence_threshold

            logger.debug(
                "[CNN] classify → transit=%.3f (threshold=%.2f)",
                confidence,
                self.confidence_threshold,
            )
            return is_transit, confidence

        except Exception as exc:
            logger.warning("[CNN] classify error: %s", exc)
            return False, 0.0

    def reload(self) -> bool:
        """Hot-reload the model from disk (e.g. after a new training run)."""
        old_session = self._session
        self._session = None
        self._load_model()
        if self.available:
            logger.info("[CNN] Model reloaded")
            return True
        self._session = old_session  # restore previous session on failure
        return False


# ── Module-level singleton (shared across detector and analyzer) ─────────────

_classifier: Optional[TransitClassifier] = None


def get_classifier(
    model_path: str = _DEFAULT_MODEL,
    confidence_threshold: float = 0.5,
) -> TransitClassifier:
    """Return the module-level singleton, creating it on first call."""
    global _classifier
    if _classifier is None:
        _classifier = TransitClassifier(
            model_path=model_path,
            confidence_threshold=confidence_threshold,
        )
    return _classifier
