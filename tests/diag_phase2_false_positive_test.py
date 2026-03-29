"""
Phase 2 diagnostic: false-positive test.

Runs transit_analyzer.py on full solar source clips and checks for detections
outside the known transit windows. Any detection outside a window = false positive.

Known transit windows (from docs/diag_logs/scan/ filenames):
  solar-src-1.mp4  : ~13s, ~25s, ~49s
  solar-src-2.mp4  : ~19s
  solar-src-3.mp4  : ~3s, ~6s, ~10s
  solar-src-4.mp4  : ~2s
  solar-src-5.mp4  : ~9s

For lunar FP: runs analyzer with target=moon on transit-{1,2,3}.mp4 and
checks for detections at unexpected times (outside the main transit).

Usage:
    python tests/diag_phase2_false_positive_test.py \\
        --dir "transits from David/" \\
        --output docs/diag_logs/phase2_fp_results.json

    # Test with custom parameters after tuning:
    python tests/diag_phase2_false_positive_test.py \\
        --dir "transits from David/" \\
        --diff-threshold 12 --min-travel 25 --min-speed 50
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.transit_analyzer import analyze_video

# ── Known transit windows (centre_time ± WINDOW_HALF_S) ──────────────────────

WINDOW_HALF_S = 2.5  # seconds around each known transit time

SOLAR_KNOWN = {
    "solar-src-1.mp4": [13.0, 25.0, 49.0],
    "solar-src-2.mp4": [19.0],
    "solar-src-3.mp4": [3.0, 6.0, 10.0],
    "solar-src-4.mp4": [2.0],
    "solar-src-5.mp4": [9.0],
}

MOON_STEMS = {"transit-1", "transit-2", "transit-3"}


def _in_any_window(t: float, windows: List[float]) -> bool:
    return any(abs(t - w) <= WINDOW_HALF_S for w in windows)


def _classify_events(events: list, known_windows: List[float]):
    """Return (expected_events, false_positive_events)."""
    expected, fps = [], []
    for ev in events:
        mid = (ev.get("start_seconds", 0) + ev.get("end_seconds", 0)) / 2.0
        if known_windows and _in_any_window(mid, known_windows):
            expected.append(ev)
        else:
            fps.append(ev)
    return expected, fps


def run_fp_test(
    video_path: Path,
    target: str,
    known_windows: List[float],
    analyzer_kwargs: dict,
) -> dict:
    result = analyze_video(
        str(video_path), output_annotated=False, target=target, **analyzer_kwargs
    )
    expected, fp_events = _classify_events(result.transit_events, known_windows)
    return {
        "file": video_path.name,
        "target": target,
        "duration_s": round(result.duration_seconds, 2),
        "disk_detected": result.disk_detected,
        "disk_radius": result.disk_radius,
        "total_events": len(result.transit_events),
        "expected_events": len(expected),
        "false_positives": len(fp_events),
        "fp_details": [
            {
                "start_s": round(ev.get("start_seconds", 0), 3),
                "end_s": round(ev.get("end_seconds", 0), 3),
                "dur_ms": ev.get("duration_ms", 0),
                "confidence": ev.get("confidence", "?"),
            }
            for ev in fp_events
        ],
        "error": result.error,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dir", required=True, help="Directory with MP4 files")
    parser.add_argument("--output", default="docs/diag_logs/phase2_fp_results.json")
    parser.add_argument("--diff-threshold", type=int, default=None)
    parser.add_argument("--min-travel", type=float, default=None)
    parser.add_argument("--min-speed", type=float, default=None)
    args = parser.parse_args()

    analyzer_kwargs = {}
    if args.diff_threshold is not None:
        analyzer_kwargs["diff_threshold"] = args.diff_threshold
    if args.min_travel is not None:
        analyzer_kwargs["min_travel_px"] = args.min_travel
    if args.min_speed is not None:
        analyzer_kwargs["min_speed_px_s"] = args.min_speed

    d = Path(args.dir)
    results = []
    total_fp = 0

    print(f"\nFalse-positive test — dir={d}\n" + "=" * 60)

    # Solar FP tests
    print("\n[SOLAR]")
    for stem, windows in SOLAR_KNOWN.items():
        vpath = d / stem
        if not vpath.exists():
            print(f"  {stem}: NOT FOUND (skip)")
            continue
        print(f"  {stem} ...", end=" ", flush=True)
        r = run_fp_test(vpath, "sun", windows, analyzer_kwargs)
        results.append(r)
        total_fp += r["false_positives"]
        fp_str = f"FP={r['false_positives']}" if r["false_positives"] else "FP=0 ok"
        print(f"events={r['total_events']}  expected={r['expected_events']}  {fp_str}")
        for fp in r["fp_details"]:
            print(
                f"    FP: {fp['start_s']:.2f}s–{fp['end_s']:.2f}s "
                f"({fp['dur_ms']}ms, {fp['confidence']})"
            )

    # Lunar FP tests — no known windows (full clip expected to show transit)
    # We just report all events; operator verifies timing manually.
    print("\n[LUNAR]  (all events shown; operator verify timing)")
    for stem in sorted(MOON_STEMS):
        vpath = d / f"{stem}.mp4"
        if not vpath.exists():
            print(f"  {stem}.mp4: NOT FOUND (skip)")
            continue
        print(f"  {stem}.mp4 ...", end=" ", flush=True)
        r = run_fp_test(vpath, "moon", [], analyzer_kwargs)
        results.append(r)
        str(r["total_events"]) + " event(s)"
        print(
            f"events={r['total_events']}  disk={'ok' if r['disk_detected'] else 'MISS'}"
        )
        for ev in r.get("fp_details", []):
            print(
                f"    {ev['start_s']:.2f}s–{ev['end_s']:.2f}s "
                f"({ev['dur_ms']}ms, {ev['confidence']})"
            )

    print("\n" + "=" * 60)
    solar_results = [r for r in results if r["target"] == "sun"]
    fp_count = sum(r["false_positives"] for r in solar_results)
    expected_count = sum(r["expected_events"] for r in solar_results)
    print(f"Solar: {expected_count} expected detections, {fp_count} false positives")
    if fp_count == 0:
        print("  -> PASS: zero solar FPs")
    else:
        print(f"  -> FAIL: {fp_count} solar FP(s) need investigation")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"Results -> {out}")


if __name__ == "__main__":
    main()
