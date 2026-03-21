"""
Phase 2 diagnostic: validate transit_analyzer.py against all 17 transit videos.

Two modes:
  baseline  — run analyze_video() with defaults on every video; print/save results
  sweep     — vary diff_threshold × min_travel_px × min_speed_px_s per target;
              find the parameter set that detects all transits with fewest FPs

Video target mapping:
  transit-{1,2,3}.mp4  →  target="moon"
  everything else       →  target="sun"

Usage:
    python tests/diag_phase2_detection_validation.py baseline \\
        --dir "transits from David/" \\
        --output docs/diag_logs/phase2_results.json

    python tests/diag_phase2_detection_validation.py sweep --target sun \\
        --dir "transits from David/" \\
        --output docs/diag_logs/phase2_sweep_solar.json

    python tests/diag_phase2_detection_validation.py sweep --target moon \\
        --dir "transits from David/" \\
        --output docs/diag_logs/phase2_sweep_lunar.json
"""

import argparse
import json
import sys
from itertools import product
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.transit_analyzer import analyze_video

# ── Target detection ──────────────────────────────────────────────────────────

MOON_STEMS = {"transit-1", "transit-2", "transit-3"}


def video_target(path: Path) -> str:
    return "moon" if path.stem in MOON_STEMS else "sun"


# ── Parameter sweep ranges ────────────────────────────────────────────────────

SOLAR_SWEEP = {
    "diff_threshold": [8, 12, 15, 20],
    "min_travel_px": [15.0, 25.0, 40.0],
    "min_speed_px_s": [30.0, 50.0, 80.0],
}

LUNAR_SWEEP = {
    "diff_threshold": [6, 8, 11, 15],
    "min_travel_px": [10.0, 15.0, 20.0, 25.0],
    "min_speed_px_s": [20.0, 30.0, 40.0, 60.0],
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run_one(video_path: Path, target: str, **kwargs) -> dict:
    """Run analyze_video and return a compact result dict."""
    result = analyze_video(
        str(video_path),
        output_annotated=False,
        target=target,
        **kwargs,
    )
    events = []
    for ev in result.transit_events:
        events.append(
            {
                "start_s": round(ev.get("start_seconds", 0), 3),
                "end_s": round(ev.get("end_seconds", 0), 3),
                "dur_ms": ev.get("duration_ms", 0),
                "frames": ev.get("frame_count", 0),
                "confidence": ev.get("confidence", "?"),
            }
        )
    return {
        "file": video_path.name,
        "target": target,
        "duration_s": round(result.duration_seconds, 2),
        "disk_detected": result.disk_detected,
        "disk_radius": result.disk_radius,
        "num_events": len(result.transit_events),
        "events": events,
        "total_blobs": len(result.detections),
        "error": result.error,
    }


def _collect_videos(directory: str, target_filter: Optional[str]) -> List[Path]:
    d = Path(directory)
    videos = sorted(
        p
        for p in d.glob("*.mp4")
        if not p.stem.startswith("analyzed_") and not p.stem.endswith("_analyzed")
    )
    if target_filter:
        videos = [v for v in videos if video_target(v) == target_filter]
    return videos


# ── Baseline mode ─────────────────────────────────────────────────────────────


def run_baseline(videos: List[Path], output_path: Path) -> None:
    print(f"\nBaseline validation — {len(videos)} video(s)\n" + "=" * 60)
    results = []
    for i, vpath in enumerate(videos):
        tgt = video_target(vpath)
        print(f"  [{i+1}/{len(videos)}] {vpath.name} ({tgt}) ...", end=" ", flush=True)
        r = _run_one(vpath, tgt)
        results.append(r)
        if r["error"]:
            print(f"ERROR: {r['error']}")
        elif r["num_events"]:
            print(f"✓ {r['num_events']} event(s): " + ", ".join(
                f"{e['start_s']:.2f}s–{e['end_s']:.2f}s ({e['dur_ms']}ms)"
                for e in r["events"]
            ))
        else:
            print(f"✗ no events  ({r['total_blobs']} blobs, disk={'✓' if r['disk_detected'] else '✗'})")

    # Summary
    detected = sum(1 for r in results if r["num_events"] > 0)
    print(f"\nSummary: {detected}/{len(results)} videos with events")
    solar_v = [r for r in results if r["target"] == "sun"]
    lunar_v = [r for r in results if r["target"] == "moon"]
    if solar_v:
        print(f"  Solar : {sum(1 for r in solar_v if r['num_events'] > 0)}/{len(solar_v)}")
    if lunar_v:
        print(f"  Lunar : {sum(1 for r in lunar_v if r['num_events'] > 0)}/{len(lunar_v)}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults → {output_path}")


# ── Sweep mode ────────────────────────────────────────────────────────────────


def run_sweep(videos: List[Path], target: str, output_path: Path) -> None:
    sweep = LUNAR_SWEEP if target == "moon" else SOLAR_SWEEP
    keys = list(sweep.keys())
    ranges = [sweep[k] for k in keys]
    combos = list(product(*ranges))

    print(f"\nParameter sweep — target={target}, {len(combos)} combos × {len(videos)} video(s)")
    print("=" * 70)

    all_results = []
    best_score = -1
    best_combo = None

    for ci, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        detected = 0
        total = len(videos)
        per_video = []

        for vpath in videos:
            r = _run_one(vpath, target, **params)
            per_video.append({"file": vpath.name, "num_events": r["num_events"],
                               "blobs": r["total_blobs"], "error": r["error"]})
            if r["num_events"] > 0:
                detected += 1

        score = detected
        tag = "✓" if detected == total else ("△" if detected > 0 else "✗")
        param_str = "  ".join(f"{k}={v}" for k, v in params.items())
        print(f"  [{ci+1:3d}/{len(combos)}] {tag} {detected}/{total}  {param_str}")

        entry = {
            "params": params,
            "detected": detected,
            "total": total,
            "detection_rate": round(detected / total, 3) if total else 0,
            "per_video": per_video,
        }
        all_results.append(entry)

        if score > best_score:
            best_score = score
            best_combo = params

    print(f"\nBest combo ({best_score}/{len(videos)} detected): {best_combo}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(
        {"target": target, "sweep": LUNAR_SWEEP if target == "moon" else SOLAR_SWEEP,
         "results": all_results, "best": best_combo},
        indent=2,
    ))
    print(f"Sweep results → {output_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_base = sub.add_parser("baseline", help="Run analyzer with defaults on all videos")
    p_base.add_argument("--dir", required=True, help="Directory containing MP4 files")
    p_base.add_argument("--output", default="docs/diag_logs/phase2_results.json")

    p_sweep = sub.add_parser("sweep", help="Parameter sweep for one target")
    p_sweep.add_argument("--dir", required=True, help="Directory containing MP4 files")
    p_sweep.add_argument("--target", choices=["sun", "moon"], required=True)
    p_sweep.add_argument("--output", default=None)

    args = parser.parse_args()

    if args.command == "baseline":
        videos = _collect_videos(args.dir, None)
        if not videos:
            print(f"No MP4 files found in: {args.dir}", file=sys.stderr)
            sys.exit(1)
        run_baseline(videos, Path(args.output))

    elif args.command == "sweep":
        videos = _collect_videos(args.dir, args.target)
        if not videos:
            print(f"No {args.target} MP4 files found in: {args.dir}", file=sys.stderr)
            sys.exit(1)
        default_out = f"docs/diag_logs/phase2_sweep_{args.target}.json"
        run_sweep(videos, args.target, Path(args.output or default_out))


if __name__ == "__main__":
    main()
