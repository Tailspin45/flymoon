"""
E1 — Synthetic transit clip generator.

Produces realistic solar-disc + dark-blob clips without real video data,
useful for data augmentation and pre-training before real clips are available.

Each positive clip: dark ellipse sweeping across a synthetic solar disc
  with limb darkening, atmospheric shimmer, and Gaussian sensor noise.
Each negative clip: same disc but no transit (or cloud/partial-disc occlusion).

Usage
-----
    python -m training.synthetic_gen
    python -m training.synthetic_gen --n_pos 500 --n_neg 500 --out data/training
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from pathlib import Path
from typing import Tuple

import numpy as np

CLIP_W = 90
CLIP_H = 160
CLIP_T = 15
OUT_DIR = Path("data/training")

RNG = np.random.default_rng(0)


# ── Solar disc synthesis ──────────────────────────────────────────────────────

def _solar_disc(h: int = CLIP_H, w: int = CLIP_W, noise_sigma: float = 4.0) -> np.ndarray:
    """
    Generate a single-frame synthetic solar disc (float32, 0–255 range).
    Includes limb darkening and mild Gaussian noise.
    """
    cy, cx = h / 2, w / 2
    radius = min(h, w) * 0.42
    y = np.arange(h, dtype=np.float32).reshape(-1, 1)
    x = np.arange(w, dtype=np.float32).reshape(1, -1)
    r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)

    # Limb darkening: Eddington approximation u=0.6
    u = 0.6
    mu = np.sqrt(np.clip(1.0 - (r / radius) ** 2, 1e-6, 1.0))
    intensity = 220.0 * (1.0 - u * (1.0 - mu))  # ~220 at disc centre, ~130 at limb
    disc = np.where(r <= radius, intensity, 20.0).astype(np.float32)  # sky ≈ 20

    # Sensor noise
    disc += RNG.normal(0, noise_sigma, disc.shape).astype(np.float32)
    return np.clip(disc, 0, 255).astype(np.float32)


def _shimmer(base: np.ndarray, amplitude: float = 2.5) -> np.ndarray:
    """Random per-frame atmospheric shimmer: small random affine warp."""
    h, w = base.shape
    dx = RNG.uniform(-amplitude, amplitude)
    dy = RNG.uniform(-amplitude, amplitude)
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    import cv2
    return cv2.warpAffine(base, M, (w, h), borderValue=20.0)


def _aircraft_mask(
    h: int, w: int,
    cx: float, cy: float,
    angle_deg: float,
    length_px: float = 5.0,
    width_px: float = 2.0,
) -> np.ndarray:
    """Boolean mask for a dark ellipse (aircraft silhouette)."""
    import cv2
    mask = np.zeros((h, w), dtype=np.uint8)
    axes = (max(1, int(length_px / 2)), max(1, int(width_px / 2)))
    cv2.ellipse(mask, (int(cx), int(cy)), axes, angle_deg, 0, 360, 1, -1)
    return mask.astype(bool)


# ── Clip generators ───────────────────────────────────────────────────────────

def generate_transit_clip(
    aircraft_length_range: Tuple[float, float] = (4.0, 10.0),
    speed_range: Tuple[float, float] = (3.0, 12.0),  # pixels per frame
    noise_sigma: float = 4.0,
    shimmer_amp: float = 2.0,
) -> np.ndarray:
    """Return (T, H, W) uint8 clip with a synthetic transit."""
    h, w = CLIP_H, CLIP_W
    radius = min(h, w) * 0.42
    cx_disc, cy_disc = w / 2, h / 2

    # Pick random entry/exit points on the disc boundary
    entry_angle = RNG.uniform(0, 2 * math.pi)
    chord_offset = RNG.uniform(-radius * 0.8, radius * 0.8)

    # Entry point perpendicular to the travel direction
    perp_angle = entry_angle + math.pi / 2
    ex = cx_disc + chord_offset * math.cos(perp_angle) - radius * math.cos(entry_angle)
    ey = cy_disc + chord_offset * math.sin(perp_angle) - radius * math.sin(entry_angle)
    # Exit point
    fx = cx_disc + chord_offset * math.cos(perp_angle) + radius * math.cos(entry_angle)
    fy = cy_disc + chord_offset * math.sin(perp_angle) + radius * math.sin(entry_angle)

    speed = RNG.uniform(*speed_range)
    aircraft_len = RNG.uniform(*aircraft_length_range)
    aircraft_w = RNG.uniform(1.5, aircraft_len * 0.4)
    heading_deg = math.degrees(math.atan2(fy - ey, fx - ex))

    # Total track length / speed → total frames needed
    track_len = math.hypot(fx - ex, fy - ey)
    total_frames_needed = max(CLIP_T + 4, int(track_len / speed) + CLIP_T)

    # Generate long clip, then pick a CLIP_T window containing the transit
    frames_long = []
    base = _solar_disc(h, w, noise_sigma)
    t = RNG.uniform(0, CLIP_T // 2)  # transit starts early in window
    transit_start_frame = int(t)

    for i in range(total_frames_needed):
        f = _shimmer(base.copy(), shimmer_amp)
        # Aircraft position
        progress = (i - transit_start_frame) * speed
        ax = ex + (fx - ex) * (progress / track_len) if track_len > 0 else ex
        ay = ey + (fy - ey) * (progress / track_len) if track_len > 0 else ey
        # Only paint if inside disc bounding box
        if 0 <= ax < w and 0 <= ay < h:
            mask = _aircraft_mask(h, w, ax, ay, heading_deg, aircraft_len, aircraft_w)
            f[mask] *= RNG.uniform(0.05, 0.25)  # aircraft is dark
        frames_long.append(np.clip(f, 0, 255).astype(np.uint8))

    # Pick a CLIP_T window that overlaps with the transit
    clip_start = max(0, transit_start_frame - CLIP_T // 2)
    clip_start = min(clip_start, len(frames_long) - CLIP_T)
    clip = np.stack(frames_long[clip_start: clip_start + CLIP_T], axis=0)
    return clip


def generate_negative_clip(
    kind: str = "clear",
    noise_sigma: float = 4.0,
    shimmer_amp: float = 2.0,
) -> np.ndarray:
    """
    Return (T, H, W) uint8 clip without a transit.

    kind: 'clear' | 'cloud' | 'sunspot'
    """
    import cv2
    h, w = CLIP_H, CLIP_W
    base = _solar_disc(h, w, noise_sigma)

    if kind == "cloud":
        # Random semi-transparent cloud edge drifting across the frame
        cloud_x = RNG.uniform(-w * 0.3, w * 0.3)
        cloud_y = RNG.uniform(-h * 0.3, h * 0.3)
        cloud_r = RNG.integers(h // 4, h // 2)
        cloud_mask = np.zeros((h, w), dtype=np.float32)
        cv2.circle(cloud_mask, (int(w / 2 + cloud_x), int(h / 2 + cloud_y)),
                   cloud_r, 1.0, -1)
        cloud_mask = cv2.GaussianBlur(cloud_mask, (31, 31), 0)
        base = base * (1.0 - cloud_mask * RNG.uniform(0.3, 0.7))

    elif kind == "sunspot":
        # Dark oval (sunspot) that stays stationary
        cx_s = RNG.uniform(w * 0.2, w * 0.8)
        cy_s = RNG.uniform(h * 0.2, h * 0.8)
        spot_mask = _aircraft_mask(
            h, w, cx_s, cy_s, angle_deg=RNG.uniform(0, 180),
            length_px=RNG.uniform(4, 12), width_px=RNG.uniform(3, 8)
        )
        base[spot_mask] *= RNG.uniform(0.3, 0.6)

    frames = []
    for _ in range(CLIP_T):
        f = _shimmer(base.copy(), shimmer_amp)
        frames.append(np.clip(f, 0, 255).astype(np.uint8))
    return np.stack(frames, axis=0)


# ── Batch generation & saving ─────────────────────────────────────────────────

def generate_dataset(
    n_pos: int,
    n_neg: int,
    out_dir: Path,
    seed: int = 0,
) -> None:
    global RNG
    RNG = np.random.default_rng(seed)

    pos_dir = out_dir / "positives"
    neg_dir = out_dir / "negatives"
    labels_csv = out_dir / "labels.csv"
    pos_dir.mkdir(parents=True, exist_ok=True)
    neg_dir.mkdir(parents=True, exist_ok=True)

    first_write = not labels_csv.exists()
    with open(labels_csv, "a", newline="") as fh:
        writer = csv.writer(fh)
        if first_write:
            writer.writerow(["filename", "label", "source_video", "center_s", "duration_s"])

        print(f"Generating {n_pos} synthetic positives …")
        for i in range(n_pos):
            clip = generate_transit_clip()
            fname = f"synth_pos_{i:04d}.npz"
            np.savez_compressed(pos_dir / fname, clip=clip)
            writer.writerow([fname, "positive_synthetic", "synthetic", "0", "1"])

        kinds = ["clear", "clear", "cloud", "sunspot"]  # 2:1:1 ratio
        print(f"Generating {n_neg} synthetic negatives …")
        for i in range(n_neg):
            kind = kinds[i % len(kinds)]
            clip = generate_negative_clip(kind=kind)
            fname = f"synth_neg_{i:04d}.npz"
            np.savez_compressed(neg_dir / fname, clip=clip)
            writer.writerow([fname, f"negative_synthetic_{kind}", "synthetic", "0", "0"])

    print(f"Saved to {out_dir}")


def main():
    ap = argparse.ArgumentParser(description="Generate synthetic solar transit training clips")
    ap.add_argument("--n_pos", type=int, default=400, help="Positive (transit) clips to generate")
    ap.add_argument("--n_neg", type=int, default=400, help="Negative clips to generate")
    ap.add_argument("--out", default=str(OUT_DIR), help="Output directory")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    generate_dataset(args.n_pos, args.n_neg, Path(args.out), seed=args.seed)


if __name__ == "__main__":
    main()
