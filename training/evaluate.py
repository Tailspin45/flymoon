"""
E5 — Model evaluation: recall, precision, FPR on held-out clips.

Usage
-----
    python -m training.evaluate
    python -m training.evaluate --model models/transit_classifier.onnx --data data/training
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import numpy as np

MODEL_PATH = Path("models/transit_classifier.onnx")
DATA_DIR = Path("data/training")
CLIP_T = 15


def _normalize(clip: np.ndarray) -> np.ndarray:
    mu = clip.mean()
    std = clip.std() + 1e-6
    return ((clip - mu) / std).astype(np.float32)


def load_session(model_path: Path):
    try:
        import onnxruntime as ort
    except ImportError:
        print(
            "onnxruntime is required. Install with: pip install onnxruntime",
            file=sys.stderr,
        )
        sys.exit(1)
    return ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])


def predict_batch(session, clips: np.ndarray) -> np.ndarray:
    """clips: (N, T, H, W) float32 normalised → returns (N,) transit probabilities."""
    x = clips[:, np.newaxis, :, :, :]  # (N, 1, T, H, W)
    logits = session.run(None, {"frames": x})[0]
    # Softmax (2-class output requires softmax, not sigmoid on a single logit)
    shifted = logits - logits.max(axis=1, keepdims=True)
    exp_l = np.exp(shifted)
    probs = exp_l[:, 1] / exp_l.sum(axis=1)
    return probs


def evaluate(
    model_path: Path = MODEL_PATH,
    data_dir: Path = DATA_DIR,
    threshold: float = 0.5,
    batch_size: int = 32,
) -> dict:
    session = load_session(model_path)

    pos_files = glob.glob(str(data_dir / "positives" / "*.npz"))
    neg_files = glob.glob(str(data_dir / "negatives" / "*.npz"))

    if not pos_files:
        print(f"No positive clips in {data_dir / 'positives'}", file=sys.stderr)
        sys.exit(1)

    def load_clips(files):
        clips = []
        for f in files:
            try:
                clip = np.load(f)["clip"].astype(np.float32) / 255.0
                clips.append(_normalize(clip))
            except Exception as e:
                print(f"  [WARN] cannot load {f}: {e}", file=sys.stderr)
        return (
            np.stack(clips, axis=0)
            if clips
            else np.zeros((0, CLIP_T, 1, 1), dtype=np.float32)
        )

    print(f"Loading {len(pos_files)} positive clips …")
    pos_clips = load_clips(pos_files)
    print(f"Loading {len(neg_files)} negative clips …")
    neg_clips = load_clips(neg_files)

    def run_in_batches(clips):
        if len(clips) == 0:
            return np.array([])
        probs = []
        for start in range(0, len(clips), batch_size):
            batch = clips[start : start + batch_size]
            probs.append(predict_batch(session, batch))
        return np.concatenate(probs)

    print("Running inference …")
    pos_probs = run_in_batches(pos_clips)
    neg_probs = run_in_batches(neg_clips)

    # Metrics at given threshold
    tp = int((pos_probs >= threshold).sum())
    fn = int((pos_probs < threshold).sum())
    tn = int((neg_probs < threshold).sum())
    fp = int((neg_probs >= threshold).sum())

    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall + 1e-9)

    # AUC-ROC (approximate)
    all_probs = np.concatenate([pos_probs, neg_probs])
    all_labels = np.concatenate([np.ones(len(pos_probs)), np.zeros(len(neg_probs))])
    sorted_idx = np.argsort(-all_probs)
    tp_cum = np.cumsum(all_labels[sorted_idx])
    fp_cum = np.cumsum(1 - all_labels[sorted_idx])
    tpr_curve = tp_cum / max(len(pos_probs), 1)
    fpr_curve = fp_cum / max(len(neg_probs), 1)
    auc = float(np.trapz(tpr_curve, fpr_curve)) * (
        -1 if fpr_curve[-1] < fpr_curve[0] else 1
    )
    auc = abs(auc)

    results = {
        "threshold": threshold,
        "n_pos": len(pos_probs),
        "n_neg": len(neg_probs),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "fpr": round(fpr, 4),
        "f1": round(f1, 4),
        "auc_roc": round(auc, 4),
    }

    # Print report
    print("\n── Evaluation Report ─────────────────────────────────────────")
    print(f"  Model:      {model_path}")
    print(f"  Threshold:  {threshold}")
    print(f"  Positives:  {len(pos_probs)}")
    print(f"  Negatives:  {len(neg_probs)}")
    print()
    print(f"  Recall:     {recall*100:.1f}%  (TP={tp}, FN={fn})")
    print(f"  Precision:  {precision*100:.1f}%  (TP={tp}, FP={fp})")
    print(f"  FPR:        {fpr*100:.1f}%  (FP={fp}, TN={tn})")
    print(f"  F1:         {f1:.4f}")
    print(f"  AUC-ROC:    {auc:.4f}")

    target_recall = 0.90
    target_fpr = 0.05
    if recall >= target_recall and fpr <= target_fpr:
        print(
            f"\n  ✅ PASS: recall ≥ {target_recall*100:.0f}% and FPR ≤ {target_fpr*100:.0f}%"
        )
    else:
        fails = []
        if recall < target_recall:
            fails.append(f"recall {recall*100:.1f}% < {target_recall*100:.0f}%")
        if fpr > target_fpr:
            fails.append(f"FPR {fpr*100:.1f}% > {target_fpr*100:.0f}%")
        print(f"\n  ❌ FAIL: {'; '.join(fails)}")

    # Confidence distribution
    print("\n  Confidence distribution (positives):")
    bins = [0.0, 0.3, 0.5, 0.7, 0.9, 1.01]
    hist, _ = np.histogram(pos_probs, bins=bins)
    for i, (lo, hi) in enumerate(zip(bins[:-1], bins[1:])):
        bar = "█" * int(hist[i] / max(hist) * 20) if max(hist) > 0 else ""
        print(f"    [{lo:.1f},{hi:.2f}): {hist[i]:4d}  {bar}")

    return results


def main():
    ap = argparse.ArgumentParser(description="Evaluate transit classifier")
    ap.add_argument("--model", default=str(MODEL_PATH))
    ap.add_argument("--data", default=str(DATA_DIR))
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()
    evaluate(Path(args.model), Path(args.data), args.threshold)


if __name__ == "__main__":
    main()
