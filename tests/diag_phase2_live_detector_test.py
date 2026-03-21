"""
Phase 2 diagnostic: test TransitDetector._process_frame() on video files.

Bypasses RTSP. Decodes each video to 90x160 RGB24 at 15 fps via ffmpeg
and feeds frames into TransitDetector._process_frame(), replicating the
live RTSP loop exactly.

Primary goal: validate that the dual-signal algorithm fires on lunar clips
(transit-{1,2,3}.mp4) — the live detector has no moon-specific mode.

Usage:
    python tests/diag_phase2_live_detector_test.py \\
        "transits from David/transit-1.mp4" \\
        "transits from David/transit-2.mp4" \\
        "transits from David/transit-3.mp4" \\
        --output docs/diag_logs/phase2_live_detector.json

    # More sensitive (try if disk is detected but signal is weak):
    python tests/diag_phase2_live_detector_test.py \\
        "transits from David/transit-1.mp4" --sensitivity 0.5
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.transit_detector import (
    ANALYSIS_FPS,
    ANALYSIS_HEIGHT,
    ANALYSIS_WIDTH,
    FRAME_BYTES,
    TransitDetector,
)

MOON_STEMS = {"transit-1", "transit-2", "transit-3"}


def _video_target(path: Path) -> str:
    return "moon" if path.stem in MOON_STEMS else "sun"


def feed_video_to_detector(
    video_path: Path,
    sensitivity_scale: float = 1.0,
    verbose: bool = False,
) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"error": "ffmpeg not found on PATH"}

    target = _video_target(video_path)
    detections_fired = []

    def _on_detection(event):
        detections_fired.append({
            "frame_idx": event.frame_idx,
            "time_s": round(event.frame_idx / ANALYSIS_FPS, 3),
            "signal_a": round(event.signal_a, 5),
            "signal_b": round(event.signal_b, 5),
            "threshold_a": round(event.threshold_a, 5),
            "threshold_b": round(event.threshold_b, 5),
            "centre_ratio": round(event.centre_ratio, 2),
            "confidence": event.confidence,
        })

    detector = TransitDetector(
        rtsp_url="",
        record_on_detect=False,
        on_detection=_on_detection,
        sensitivity_scale=sensitivity_scale,
    )
    detector._running = True
    detector._start_time = 0.0

    cmd = [
        ffmpeg, "-v", "error",
        "-i", str(video_path),
        "-vf", f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT}",
        "-r", str(ANALYSIS_FPS),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-an",
        "pipe:1",
    ]

    frames_fed = 0
    disk_found_at: Optional[int] = None

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        while True:
            raw = proc.stdout.read(FRAME_BYTES)
            if len(raw) < FRAME_BYTES:
                break
            frame = (
                np.frombuffer(raw, dtype=np.uint8)
                .reshape((ANALYSIS_HEIGHT, ANALYSIS_WIDTH, 3))
                .astype(np.float32)
            )
            detector._total_frames += 1
            detector._frame_idx += 1
            detector._process_frame(frame)
            frames_fed += 1
            if disk_found_at is None and detector._disk_detected:
                disk_found_at = frames_fed
            if verbose and frames_fed % (ANALYSIS_FPS * 5) == 0:
                t = frames_fed / ANALYSIS_FPS
                print(
                    f"    t={t:.1f}s  disk={'ok' if detector._disk_detected else 'miss'}"
                    f"  consec={detector._consec_above}  fired={len(detections_fired)}"
                )
        proc.wait()
    except Exception as exc:
        return {"file": video_path.name, "target": target, "error": str(exc),
                "frames_fed": frames_fed, "detections": []}

    detector._running = False
    return {
        "file": video_path.name,
        "target": target,
        "sensitivity_scale": sensitivity_scale,
        "frames_fed": frames_fed,
        "duration_s": round(frames_fed / ANALYSIS_FPS, 2),
        "disk_detected": detector._disk_detected,
        "disk_found_at_frame": disk_found_at,
        "disk_found_at_s": round(disk_found_at / ANALYSIS_FPS, 2) if disk_found_at else None,
        "disk_radius": detector._disk_radius,
        "detected": len(detections_fired) > 0,
        "num_detections": len(detections_fired),
        "detections": detections_fired,
        "error": None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("videos", nargs="+", help="MP4 files to test")
    parser.add_argument("--sensitivity", type=float, default=1.0,
                        help="Sensitivity scale (<1=more sensitive). Default 1.0")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output", default="docs/diag_logs/phase2_live_detector.json")
    args = parser.parse_args()

    results = []
    print(f"\nLive detector file-feed test — {len(args.videos)} video(s)\n" + "=" * 60)

    for vpath_str in args.videos:
        vpath = Path(vpath_str)
        if not vpath.exists():
            print(f"  {vpath.name}: FILE NOT FOUND", file=sys.stderr)
            results.append({"file": vpath.name, "error": "not found"})
            continue

        tgt = _video_target(vpath)
        print(f"\n  {vpath.name}  (target={tgt}, sensitivity={args.sensitivity})")
        r = feed_video_to_detector(vpath, sensitivity_scale=args.sensitivity,
                                   verbose=args.verbose)
        results.append(r)

        if r.get("error"):
            print(f"    ERROR: {r['error']}")
            continue

        if r["disk_detected"]:
            print(f"    Disk  : ok  r={r['disk_radius']}px found @{r['disk_found_at_s']}s")
        else:
            print("    Disk  : MISS — detection disabled for entire run")

        print(f"    Frames: {r['frames_fed']}  ({r['duration_s']}s)")

        if r["detected"]:
            print(f"    Result: FIRED ({r['num_detections']} event(s))")
            for d in r["detections"]:
                print(f"      t={d['time_s']:.2f}s  CR={d['centre_ratio']:.2f}  "
                      f"conf={d['confidence']}  "
                      f"A={d['signal_a']:.4f}/thr={d['threshold_a']:.4f}  "
                      f"B={d['signal_b']:.4f}/thr={d['threshold_b']:.4f}")
        else:
            print("    Result: NO DETECTION")
            if not r["disk_detected"]:
                print("    NOTE: disk not found — lunar contrast may be too low for Hough")
                print("    TRY : --sensitivity 0.3, or check frame brightness in the video")

    print("\n" + "=" * 60)
    print(f"{'File':<35} {'Target':<6} {'Disk':>5} {'Fired':>6} {'#Det':>5}")
    print("-" * 60)
    for r in results:
        if r.get("error") == "not found":
            continue
        disk = "ok" if r.get("disk_detected") else "MISS"
        fired = "YES" if r.get("detected") else "no"
        print(f"  {r['file']:<33} {r.get('target','?'):<6} {disk:>5} "
              f"{fired:>6} {r.get('num_detections',0):>5}")

    n_detected = sum(1 for r in results if r.get("detected"))
    print(f"\nFired: {n_detected}/{len(results)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"Results -> {out}")


if __name__ == "__main__":
    main()
