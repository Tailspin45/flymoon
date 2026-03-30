"""
Live detector replay test — feeds MP4 frames through TransitDetector._process_frame
to validate the LIVE detection pipeline against known transit clips.

This closes the gap where test_detection_harness.py only tests the offline
transit_analyzer, which has different logic than the real-time detector.

Usage:
    python tests/test_live_detector_replay.py "/path/to/transits/"
    python tests/test_live_detector_replay.py file1.mp4 file2.mp4 ...
    python tests/test_live_detector_replay.py --dir "/Users/Tom/flymoon/transits from David"
"""

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.transit_detector import (
    ANALYSIS_FPS,
    ANALYSIS_HEIGHT,
    ANALYSIS_WIDTH,
    TransitDetector,
)


def replay_video(filepath: str, verbose: bool = False) -> dict:
    """Feed an MP4 through the live detector frame-by-frame.

    Creates a TransitDetector with recording disabled (no RTSP needed),
    manually drives _process_frame, and collects any detections.
    """
    cap = cv2.VideoCapture(filepath)
    if not cap.isOpened():
        return {"file": filepath, "error": "cannot open", "detections": []}

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / src_fps if src_fps > 0 else 0

    detections: list = []

    def on_detect(event):
        detections.append({
            "time_s": round(event.frame_idx / ANALYSIS_FPS, 3),
            "frame": event.frame_idx,
            "confidence": event.confidence,
            "score": event.confidence_score,
            "signal_a": round(event.signal_a, 5),
            "signal_b": round(event.signal_b, 5),
            "centre_ratio": round(event.centre_ratio, 2),
        })

    det = TransitDetector(
        rtsp_url="replay://test",
        capture_dir="/tmp/replay_test",
        on_detection=on_detect,
        record_on_detect=False,
    )

    # Initialise internal state without starting the reader loop
    det._running = True
    det._start_time = time.monotonic()
    det._total_frames = 0
    det._frame_idx = 0
    det._prev_frame = None
    det._ref_frame = None
    det._consec_above = 0
    det._scores_a.clear()
    det._scores_b.clear()
    det._signal_trace.clear()
    det._disk_detected = False
    det._disc_lost_frames = 0
    det._disc_lost_warning = False

    step = max(1, round(src_fps / ANALYSIS_FPS))
    frame_no = 0
    fed = 0

    while True:
        ret, bgr = cap.read()
        if not ret:
            break
        frame_no += 1
        if (frame_no - 1) % step != 0:
            continue

        resized = cv2.resize(bgr, (ANALYSIS_WIDTH, ANALYSIS_HEIGHT))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)

        det._total_frames += 1
        det._frame_idx += 1
        fed += 1

        det._process_frame(rgb)

    cap.release()
    det._running = False

    # Collect diagnostic info from signal trace
    trace = list(det._signal_trace)
    max_a = max((t["a"] for t in trace), default=0)
    max_b = max((t["b"] for t in trace), default=0)
    max_cr = max((t["cr"] for t in trace), default=0)
    last_ta = trace[-1]["ta"] if trace else 0
    last_tb = trace[-1]["tb"] if trace else 0

    return {
        "file": Path(filepath).name,
        "duration_s": round(duration, 2),
        "src_fps": round(src_fps, 1),
        "frames_fed": fed,
        "detections": detections,
        "detected": len(detections) > 0,
        "disk_found": det._disk_detected,
        "max_signal_a": round(max_a, 5),
        "max_signal_b": round(max_b, 5),
        "max_centre_ratio": round(max_cr, 2),
        "last_thresh_a": round(last_ta, 5),
        "last_thresh_b": round(last_tb, 5),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Replay MP4s through the live TransitDetector pipeline"
    )
    parser.add_argument("files", nargs="*", help="MP4 files to replay")
    parser.add_argument("--dir", type=str, help="Directory to scan for MP4s")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    paths: list[str] = list(args.files)
    if args.dir:
        d = Path(args.dir)
        paths.extend(
            str(p) for p in sorted(d.glob("*.mp4"))
            if not p.stem.startswith("analyzed_")
        )

    if not paths:
        print("No MP4 files specified. Use positional args or --dir.", file=sys.stderr)
        sys.exit(1)

    print(f"Replaying {len(paths)} file(s) through live TransitDetector")
    print(f"Analysis: {ANALYSIS_WIDTH}x{ANALYSIS_HEIGHT} @ {ANALYSIS_FPS}fps")
    print()

    results = []
    hits = 0
    for i, p in enumerate(paths):
        name = Path(p).name
        sys.stderr.write(f"  [{i+1}/{len(paths)}] {name} ... ")
        sys.stderr.flush()

        r = replay_video(p, verbose=args.verbose)
        results.append(r)

        if r.get("error"):
            sys.stderr.write(f"ERROR: {r['error']}\n")
        elif r["detected"]:
            hits += 1
            n = len(r["detections"])
            sys.stderr.write(f"HIT ({n} detection(s))\n")
            for d in r["detections"]:
                sys.stderr.write(
                    f"    t={d['time_s']:.2f}s  [{d['confidence']}|{d['score']:.2f}]  "
                    f"A={d['signal_a']:.4f}  B={d['signal_b']:.4f}  "
                    f"CR={d['centre_ratio']:.1f}\n"
                )
        else:
            sys.stderr.write(
                f"MISS (disk={'yes' if r['disk_found'] else 'no'}, "
                f"{r['frames_fed']} frames)\n"
                f"    maxA={r.get('max_signal_a',0):.4f} vs threshA={r.get('last_thresh_a',0):.4f}  "
                f"maxB={r.get('max_signal_b',0):.4f} vs threshB={r.get('last_thresh_b',0):.4f}  "
                f"maxCR={r.get('max_centre_ratio',0):.1f}\n"
            )

    # Summary
    print()
    print("=" * 70)
    print(f"  Live Detector Replay: {hits}/{len(results)} files detected")
    print("=" * 70)
    print(f"  {'File':<40} {'Dur':>5} {'Disk':>5} {'Det':>5}")
    print("-" * 70)
    for r in results:
        name = r["file"]
        if len(name) > 38:
            name = name[:35] + "..."
        disk = "yes" if r.get("disk_found") else "no"
        det_str = f"{len(r['detections'])}" if r["detected"] else "MISS"
        dur = r.get("duration_s", 0)
        print(f"  {name:<40} {dur:>5.1f} {disk:>5} {det_str:>5}")

    print()
    missed = [r["file"] for r in results if not r["detected"] and not r.get("error")]
    if missed:
        print(f"  MISSED: {', '.join(missed)}")
    print(f"  Detection rate: {hits}/{len(results)}")
    print()

    sys.exit(0 if hits == len(results) else 1)


if __name__ == "__main__":
    main()
