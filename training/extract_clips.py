"""
E1 — Training data extractor.

Reads every MP4 + accompanying *_analysis.json from a gallery directory and
produces 15-frame grayscale clips (T=15, H=160, W=90) at 15 fps resolution,
saved as compressed .npz files in:

    data/training/positives/   — confirmed transit clips
    data/training/negatives/   — background / near-miss clips
    data/training/labels.csv   — metadata for every saved clip

Usage
-----
    python -m training.extract_clips                        # default: transits from David/
    python -m training.extract_clips --gallery /path/to/mp4s
    python -m training.extract_clips --gallery captures/   # live capture dir
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import random
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

# ── Match TransitDetector analysis resolution ────────────────────────────────
CLIP_W = 90
CLIP_H = 160
CLIP_T = 15           # frames per clip (~1 s at 15 fps)
SRC_FPS_TARGET = 15   # target fps; every 2nd frame if source is 30fps
PRE_PAD_S = 0.3       # seconds before transit start for the clip window
HARD_NEG_OFFSET_S = 3 # offset from transit for "near-miss" hard negatives
NEG_PER_POS = 3       # background clips extracted per positive

OUT_DIR = Path("data/training")
POS_DIR = OUT_DIR / "positives"
NEG_DIR = OUT_DIR / "negatives"
LABELS_CSV = OUT_DIR / "labels.csv"


# ── Frame extraction helpers ─────────────────────────────────────────────────

def _open_video(path: str) -> Optional[cv2.VideoCapture]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        print(f"  [WARN] cannot open {path}", file=sys.stderr)
        return None
    return cap


def _extract_clip(
    cap: cv2.VideoCapture,
    src_fps: float,
    center_s: float,
    clip_t: int = CLIP_T,
    w: int = CLIP_W,
    h: int = CLIP_H,
) -> Optional[np.ndarray]:
    """Return (clip_t, h, w) uint8 grayscale clip centred on center_s."""
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, round(src_fps / SRC_FPS_TARGET))
    half_span_s = (clip_t * step) / (2 * src_fps)
    start_s = max(0.0, center_s - half_span_s)
    start_f = int(start_s * src_fps)

    frames: List[np.ndarray] = []
    fi = start_f
    while len(frames) < clip_t:
        if fi >= total_frames:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA)
        frames.append(gray)
        fi += step

    if len(frames) < clip_t:
        if not frames:
            return None
        # Pad with last frame
        while len(frames) < clip_t:
            frames.append(frames[-1])

    arr = np.stack(frames[:clip_t], axis=0)  # (T, H, W) uint8
    return arr


def _random_centers(duration_s: float, n: int, exclude: List[Tuple[float, float]]) -> List[float]:
    """Pick n random seconds in the video that are far from any transit."""
    margin = (CLIP_T / SRC_FPS_TARGET) / 2 + 0.5
    candidates: List[float] = []
    attempts = 0
    while len(candidates) < n and attempts < 2000:
        t = random.uniform(margin, max(margin + 0.1, duration_s - margin))
        ok = all(abs(t - ex_c) > ex_dur / 2 + HARD_NEG_OFFSET_S
                 for ex_c, ex_dur in exclude)
        if ok:
            candidates.append(t)
        attempts += 1
    return candidates


# ── Per-video processing ─────────────────────────────────────────────────────

def process_video(mp4_path: str, json_path: str) -> Tuple[int, int]:
    """Return (n_positives, n_negatives) extracted from this video."""
    with open(json_path) as f:
        analysis = json.load(f)

    transit_events = analysis.get("transit_events", [])
    if not isinstance(transit_events, list):
        transit_events = []

    cap = _open_video(mp4_path)
    if cap is None:
        return 0, 0

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    duration_s = cap.get(cv2.CAP_PROP_FRAME_COUNT) / src_fps
    stem = Path(mp4_path).stem
    n_pos = n_neg = 0

    # ── Positive clips ────────────────────────────────────────────────────────
    event_spans: List[Tuple[float, float]] = []  # (center_s, duration_s)
    for ev in transit_events:
        s0 = float(ev.get("start_seconds", 0))
        s1 = float(ev.get("end_seconds", s0 + 1))
        dur = s1 - s0
        center = (s0 + s1) / 2
        event_spans.append((center, dur))

        # Extract clip centred on the transit
        clip = _extract_clip(cap, src_fps, center)
        if clip is None:
            continue

        fname = f"{stem}_ev{n_pos:02d}.npz"
        out = POS_DIR / fname
        np.savez_compressed(out, clip=clip)
        _write_label(fname, "positive", mp4_path, center, dur)
        n_pos += 1

        # Hard negative: near-miss clip just before/after the transit
        for offset in [-HARD_NEG_OFFSET_S, HARD_NEG_OFFSET_S]:
            t_neg = center + offset
            if 0 < t_neg < duration_s:
                neg_clip = _extract_clip(cap, src_fps, t_neg)
                if neg_clip is not None:
                    fname_n = f"{stem}_hardneg{n_neg:03d}.npz"
                    np.savez_compressed(NEG_DIR / fname_n, clip=neg_clip)
                    _write_label(fname_n, "negative_hard", mp4_path, t_neg, 0.0)
                    n_neg += 1

    # ── Random background negatives ───────────────────────────────────────────
    centers = _random_centers(duration_s, NEG_PER_POS * max(1, len(transit_events)), event_spans)
    for t in centers:
        clip = _extract_clip(cap, src_fps, t)
        if clip is None:
            continue
        fname = f"{stem}_neg{n_neg:03d}.npz"
        np.savez_compressed(NEG_DIR / fname, clip=clip)
        _write_label(fname, "negative", mp4_path, t, 0.0)
        n_neg += 1

    cap.release()
    return n_pos, n_neg


# ── Label writer ──────────────────────────────────────────────────────────────

_csv_handle = None
_csv_writer = None

def _open_labels():
    global _csv_handle, _csv_writer
    first_write = not LABELS_CSV.exists()
    _csv_handle = open(LABELS_CSV, "a", newline="")
    _csv_writer = csv.writer(_csv_handle)
    if first_write:
        _csv_writer.writerow(["filename", "label", "source_video", "center_s", "duration_s"])


def _write_label(filename: str, label: str, source: str, center: float, dur: float):
    if _csv_writer is None:
        _open_labels()
    _csv_writer.writerow([filename, label, source, f"{center:.3f}", f"{dur:.3f}"])


def _close_labels():
    if _csv_handle:
        _csv_handle.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Extract training clips from solar transit videos")
    ap.add_argument("--gallery", default="transits from David", help="Directory containing MP4 + analysis JSON files")
    ap.add_argument("--out", default=str(OUT_DIR), help="Output directory (default: data/training)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    out_root = Path(args.out)
    global POS_DIR, NEG_DIR, LABELS_CSV  # noqa: PLW0603
    POS_DIR = out_root / "positives"
    NEG_DIR = out_root / "negatives"
    LABELS_CSV = out_root / "labels.csv"
    POS_DIR.mkdir(parents=True, exist_ok=True)
    NEG_DIR.mkdir(parents=True, exist_ok=True)

    gallery = args.gallery
    mp4s = sorted(glob.glob(os.path.join(gallery, "*.mp4")))
    if not mp4s:
        print(f"No MP4 files found in {gallery!r}", file=sys.stderr)
        sys.exit(1)

    total_pos = total_neg = 0
    for mp4 in mp4s:
        stem = Path(mp4).stem
        # Look for analysis JSON in same directory
        json_candidates = [
            os.path.join(gallery, f"analyzed_{stem}_analysis.json"),
            mp4.replace(".mp4", "_analysis.json"),
        ]
        json_path = next((j for j in json_candidates if os.path.exists(j)), None)
        if json_path is None:
            print(f"  [SKIP] {stem} — no analysis JSON found")
            continue

        print(f"  Processing {stem} …", end=" ", flush=True)
        np_, nn = process_video(mp4, json_path)
        print(f"{np_} positives, {nn} negatives")
        total_pos += np_
        total_neg += nn

    _close_labels()
    print(f"\nDone: {total_pos} positive clips, {total_neg} negative clips → {OUT_DIR}")
    print(f"Labels: {LABELS_CSV}")


if __name__ == "__main__":
    main()
