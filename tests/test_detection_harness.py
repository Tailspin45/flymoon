"""
Detection test harness — validates the transit analyzer's ability to detect
objects crossing the solar/lunar disc.

Three modes:
  inject   – Inject a synthetic blob into a video (or synthetic disc) and
             run the analyzer.  Reports hit/miss.
  sweep    – Vary blob size, speed, and opacity across ranges to map the
             detection boundary.  Outputs a table.
  validate – Run the analyzer on real MP4s with known transits and report
             detection results.

Usage:
    python tests/test_detection_harness.py inject --help
    python tests/test_detection_harness.py sweep  --help
    python tests/test_detection_harness.py validate --help

Examples:
    # Inject a 12-pixel blob moving at 60 px/s across a synthetic sun disc
    python tests/test_detection_harness.py inject --size 12 --speed 60

    # Inject into a real clean video (no existing transit)
    python tests/test_detection_harness.py inject --source clean.mp4 --size 8 --speed 40

    # Sweep size 4→30 and speed 20→200 to find detection boundaries
    python tests/test_detection_harness.py sweep

    # Validate all MP4s in a directory
    python tests/test_detection_harness.py validate --dir static/captures/2026/03/
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

# Add project root to path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.transit_analyzer import analyze_video

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 30.0
DEFAULT_DURATION = 5.0  # seconds
DEFAULT_DISC_RADIUS = 300  # pixels
DEFAULT_BLOB_SIZE = 12  # pixels (diameter)
DEFAULT_BLOB_SPEED = 80  # pixels per second
DEFAULT_BLOB_OPACITY = 1.0  # 0.0 = invisible, 1.0 = fully dark
DEFAULT_BLOB_ASPECT = 1.5  # width/height elongation
DEFAULT_TRANSIT_ANGLE = 30.0  # degrees from horizontal


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class InjectionParams:
    """Describes the synthetic transit to inject."""

    blob_diameter: float = DEFAULT_BLOB_SIZE
    speed_px_per_sec: float = DEFAULT_BLOB_SPEED
    opacity: float = DEFAULT_BLOB_OPACITY
    aspect_ratio: float = DEFAULT_BLOB_ASPECT
    angle_deg: float = DEFAULT_TRANSIT_ANGLE
    blur_sigma: float = 1.5  # Gaussian blur for soft edges
    start_offset: float = 0.0  # seconds into video to start transit


@dataclass
class InjectionResult:
    """Result of injecting and analyzing a single test case."""

    params: InjectionParams
    target: str
    detected: bool
    num_events: int
    event_details: list
    ground_truth_start_sec: float
    ground_truth_end_sec: float
    matched_event: Optional[dict] = None
    source: str = "synthetic"
    analyzer_error: Optional[str] = None


@dataclass
class SweepResult:
    """Result of a parameter sweep."""

    results: List[InjectionResult]
    total: int
    detected: int
    missed: int
    detection_rate: float


# ── Synthetic video generation ────────────────────────────────────────────────


def _make_disc_frame(
    width: int,
    height: int,
    disc_radius: int,
    target: str = "sun",
    noise_std: float = 3.0,
) -> np.ndarray:
    """Generate a single frame with a bright disc on dark background."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)

    cx, cy = width // 2, height // 2

    if target == "moon":
        disc_color = (180, 180, 180)
        bg_level = 5
    else:
        disc_color = (240, 240, 240)
        bg_level = 8

    frame[:] = bg_level
    cv2.circle(frame, (cx, cy), disc_radius, disc_color, -1, cv2.LINE_AA)

    # Add subtle texture/noise for realism
    noise = np.random.normal(0, noise_std, frame.shape).astype(np.int16)
    frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    # Slight Gaussian blur to soften disc edge
    frame = cv2.GaussianBlur(frame, (3, 3), 0.8)

    return frame


def _composite_blob(
    frame: np.ndarray,
    cx: float,
    cy: float,
    params: InjectionParams,
) -> np.ndarray:
    """Composite a dark blob onto a frame at (cx, cy)."""
    h, w = frame.shape[:2]

    # Blob semi-axes
    half_w = params.blob_diameter * params.aspect_ratio / 2.0
    half_h = params.blob_diameter / 2.0

    # Create blob mask on a small patch (for efficiency)
    patch_r = int(max(half_w, half_h) * 3) + 4
    patch_size = patch_r * 2 + 1
    mask = np.zeros((patch_size, patch_size), dtype=np.float32)

    # Draw filled ellipse on mask
    angle = params.angle_deg
    cv2.ellipse(
        mask,
        (patch_r, patch_r),
        (int(half_w), int(half_h)),
        angle,
        0,
        360,
        1.0,
        -1,
        cv2.LINE_AA,
    )

    # Soften edges
    if params.blur_sigma > 0:
        ksize = int(params.blur_sigma * 4) | 1  # must be odd
        mask = cv2.GaussianBlur(mask, (ksize, ksize), params.blur_sigma)

    # Scale by opacity
    mask *= params.opacity

    # Determine paste region in frame coordinates
    x0 = int(cx) - patch_r
    y0 = int(cy) - patch_r
    x1 = x0 + patch_size
    y1 = y0 + patch_size

    # Clip to frame bounds
    mx0 = max(0, -x0)
    my0 = max(0, -y0)
    mx1 = patch_size - max(0, x1 - w)
    my1 = patch_size - max(0, y1 - h)
    fx0 = max(0, x0)
    fy0 = max(0, y0)
    fx1 = min(w, x1)
    fy1 = min(h, y1)

    if fx0 >= fx1 or fy0 >= fy1:
        return frame

    # Darken: frame * (1 - mask) — blob is dark against bright disc
    region = frame[fy0:fy1, fx0:fx1].astype(np.float32)
    m = mask[my0:my1, mx0:mx1]
    m3 = np.stack([m, m, m], axis=-1)
    frame[fy0:fy1, fx0:fx1] = np.clip(region * (1.0 - m3), 0, 255).astype(np.uint8)

    return frame


def _compute_transit_path(
    disc_cx: int,
    disc_cy: int,
    disc_radius: int,
    params: InjectionParams,
    fps: float,
    total_frames: int,
    start_frame: int,
) -> List[Tuple[int, float, float]]:
    """
    Compute (frame_index, x, y) for each frame the blob is visible.

    The blob enters from one side of the disc and exits the other side,
    travelling in a straight line at the given speed and angle.
    """
    angle_rad = np.radians(params.angle_deg)

    # Direction vector
    dx = np.cos(angle_rad) * params.speed_px_per_sec / fps
    dy = np.sin(angle_rad) * params.speed_px_per_sec / fps

    # Start position: entry point on disc edge
    # We want the path to cross through the disc centre (or near it)
    # Calculate how many frames to cross the full disc diameter
    travel_per_frame = params.speed_px_per_sec / fps
    disc_diameter = disc_radius * 2
    frames_to_cross = disc_diameter / travel_per_frame if travel_per_frame > 0 else 0

    # Start from the edge of the disc, along the reverse direction
    half_cross = frames_to_cross / 2.0
    start_x = disc_cx - dx * half_cross
    start_y = disc_cy - dy * half_cross

    positions = []
    for i in range(int(frames_to_cross) + 10):  # slight padding
        fi = start_frame + i
        if fi >= total_frames:
            break
        x = start_x + dx * i
        y = start_y + dy * i

        # Only include positions within the disc
        dist = np.sqrt((x - disc_cx) ** 2 + (y - disc_cy) ** 2)
        if dist <= disc_radius * 0.95:  # stay within 95% of radius
            positions.append((fi, x, y))

    return positions


def generate_test_video(
    output_path: str,
    params: InjectionParams,
    target: str = "sun",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: float = DEFAULT_FPS,
    duration: float = DEFAULT_DURATION,
    disc_radius: int = DEFAULT_DISC_RADIUS,
    source_video: Optional[str] = None,
) -> Tuple[str, float, float]:
    """
    Generate a test video with an injected synthetic transit.

    If source_video is provided, reads frames from it and overlays the blob.
    Otherwise generates a synthetic disc video.

    Returns (output_path, transit_start_sec, transit_end_sec).
    """
    if source_video:
        cap = cv2.VideoCapture(source_video)
        fps = cap.get(cv2.CAP_PROP_FPS) or DEFAULT_FPS
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        # Detect disc in first frame
        ret, first_frame = cap.read()
        if not ret:
            raise ValueError(f"Cannot read source video: {source_video}")

        from src.transit_analyzer import _detect_disk

        disk = _detect_disk(first_frame)
        if disk:
            disc_cx, disc_cy, disc_radius = disk
        else:
            disc_cx, disc_cy = width // 2, height // 2
            disc_radius = min(height, width) // 4
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    else:
        cap = None
        total_frames = int(fps * duration)
        disc_cx, disc_cy = width // 2, height // 2

    # Calculate transit start frame
    start_frame = (
        int(params.start_offset * fps) if params.start_offset > 0 else int(fps * 1.5)
    )
    # Ensure transit doesn't start during reference window warmup
    start_frame = max(start_frame, int(fps * 1.2))

    # Compute blob path
    path = _compute_transit_path(
        disc_cx, disc_cy, disc_radius, params, fps, total_frames, start_frame
    )

    if not path:
        raise ValueError(
            f"No valid transit positions — blob speed {params.speed_px_per_sec} px/s "
            f"may be too fast for disc radius {disc_radius}px"
        )

    transit_start_sec = path[0][0] / fps
    transit_end_sec = path[-1][0] / fps

    # Build lookup: frame_index → (x, y)
    path_lookup = {fi: (x, y) for fi, x, y in path}

    # Write video via ffmpeg (reliable cross-platform H.264 encoding)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required but not found on PATH")

    ffproc = subprocess.Popen(
        [
            ffmpeg,
            "-y",
            "-f",
            "rawvideo",
            "-vcodec",
            "rawvideo",
            "-s",
            f"{width}x{height}",
            "-pix_fmt",
            "bgr24",
            "-r",
            str(fps),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            output_path,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for fi in range(total_frames):
        if cap:
            ret, frame = cap.read()
            if not ret:
                break
        else:
            frame = _make_disc_frame(width, height, disc_radius, target)

        if fi in path_lookup:
            x, y = path_lookup[fi]
            frame = _composite_blob(frame, x, y, params)

        ffproc.stdin.write(frame.tobytes())

    ffproc.stdin.close()
    ffproc.wait()
    if cap:
        cap.release()

    return output_path, transit_start_sec, transit_end_sec


# ── Analysis + comparison ─────────────────────────────────────────────────────


def _events_overlap(
    event: dict, gt_start: float, gt_end: float, tolerance: float = 1.0
) -> bool:
    """Check if an analyzer event overlaps with the ground truth time window."""
    ev_start = event.get("start_seconds", 0)
    ev_end = event.get("end_seconds", 0)
    return ev_start <= gt_end + tolerance and ev_end >= gt_start - tolerance


def run_injection_test(
    params: InjectionParams,
    target: str = "sun",
    source_video: Optional[str] = None,
    analyzer_kwargs: Optional[dict] = None,
    keep_video: bool = False,
    output_dir: Optional[str] = None,
) -> InjectionResult:
    """
    Inject a synthetic transit and run the analyzer on it.

    Returns an InjectionResult indicating whether the transit was detected.
    """
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        suffix = (
            f"_s{params.blob_diameter}_v{params.speed_px_per_sec}_o{params.opacity}"
        )
        video_path = os.path.join(output_dir, f"test_inject{suffix}.mp4")
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        video_path = tmp.name
        tmp.close()

    try:
        _, gt_start, gt_end = generate_test_video(
            video_path,
            params,
            target=target,
            source_video=source_video,
        )

        # Run analyzer
        kwargs = {
            "output_annotated": keep_video,
            "target": target,
        }
        if analyzer_kwargs:
            kwargs.update(analyzer_kwargs)

        result = analyze_video(video_path, **kwargs)

        if result.error:
            return InjectionResult(
                params=params,
                target=target,
                detected=False,
                num_events=0,
                event_details=[],
                ground_truth_start_sec=gt_start,
                ground_truth_end_sec=gt_end,
                source=source_video or "synthetic",
                analyzer_error=result.error,
            )

        # Check if any detected event overlaps ground truth
        matched = None
        for ev in result.transit_events:
            if _events_overlap(ev, gt_start, gt_end):
                matched = ev
                break

        return InjectionResult(
            params=params,
            target=target,
            detected=matched is not None,
            num_events=len(result.transit_events),
            event_details=result.transit_events,
            ground_truth_start_sec=gt_start,
            ground_truth_end_sec=gt_end,
            matched_event=matched,
            source=source_video or "synthetic",
        )

    finally:
        if not keep_video and os.path.exists(video_path):
            os.unlink(video_path)
            # Clean up sidecar files
            base = Path(video_path)
            for sidecar in base.parent.glob(f"analyzed_{base.stem}*"):
                sidecar.unlink(missing_ok=True)
            for sidecar in base.parent.glob(f"{base.stem}_analysis*"):
                sidecar.unlink(missing_ok=True)


# ── Parameter sweep ───────────────────────────────────────────────────────────


def run_sweep(
    sizes: Optional[List[float]] = None,
    speeds: Optional[List[float]] = None,
    opacities: Optional[List[float]] = None,
    target: str = "sun",
    source_video: Optional[str] = None,
    analyzer_kwargs: Optional[dict] = None,
    keep_videos: bool = False,
    output_dir: Optional[str] = None,
) -> SweepResult:
    """
    Sweep across blob size × speed × opacity to map detection boundaries.
    """
    if sizes is None:
        sizes = [4, 6, 8, 10, 12, 16, 20, 30]
    if speeds is None:
        speeds = [20, 40, 60, 80, 120, 160, 200]
    if opacities is None:
        opacities = [1.0]

    results = []
    total = len(sizes) * len(speeds) * len(opacities)
    done = 0

    for opacity in opacities:
        for size in sizes:
            for speed in speeds:
                done += 1
                params = InjectionParams(
                    blob_diameter=size,
                    speed_px_per_sec=speed,
                    opacity=opacity,
                )
                sys.stderr.write(
                    f"\r  [{done}/{total}] size={size:>4}px  speed={speed:>4}px/s  "
                    f"opacity={opacity:.1f} ... "
                )
                sys.stderr.flush()

                r = run_injection_test(
                    params,
                    target=target,
                    source_video=source_video,
                    analyzer_kwargs=analyzer_kwargs,
                    keep_video=keep_videos,
                    output_dir=output_dir,
                )
                results.append(r)

                status = "✅ HIT" if r.detected else "❌ MISS"
                sys.stderr.write(f"{status}\n")
                sys.stderr.flush()

    detected = sum(1 for r in results if r.detected)
    return SweepResult(
        results=results,
        total=total,
        detected=detected,
        missed=total - detected,
        detection_rate=detected / total if total > 0 else 0.0,
    )


# ── Real video validation ────────────────────────────────────────────────────


def validate_real_videos(
    video_paths: List[str],
    target: str = "auto",
    analyzer_kwargs: Optional[dict] = None,
) -> List[dict]:
    """
    Run the analyzer on real MP4s with known (or suspected) transits.
    Reports detection results for each file.
    """
    results = []
    for i, vpath in enumerate(video_paths):
        sys.stderr.write(f"\r  [{i+1}/{len(video_paths)}] {Path(vpath).name} ... ")
        sys.stderr.flush()

        kwargs = {"output_annotated": False, "target": target}
        if analyzer_kwargs:
            kwargs.update(analyzer_kwargs)

        r = analyze_video(vpath, **kwargs)

        entry = {
            "file": vpath,
            "name": Path(vpath).name,
            "duration_seconds": round(r.duration_seconds, 2),
            "disk_detected": r.disk_detected,
            "disk_radius": r.disk_radius,
            "num_events": len(r.transit_events),
            "events": r.transit_events,
            "total_detections": len(r.detections),
            "error": r.error,
        }
        results.append(entry)

        if r.error:
            sys.stderr.write(f"⚠️  {r.error}\n")
        elif r.transit_events:
            sys.stderr.write(f"✅ {len(r.transit_events)} event(s)\n")
        else:
            sys.stderr.write(f"❌ no events ({len(r.detections)} raw blobs)\n")
        sys.stderr.flush()

    return results


# ── Display helpers ───────────────────────────────────────────────────────────


def print_sweep_table(sweep: SweepResult, target: str):
    """Print a human-readable detection boundary table."""
    # Group by opacity
    by_opacity = {}
    for r in sweep.results:
        op = r.params.opacity
        by_opacity.setdefault(op, []).append(r)

    for opacity, results in sorted(by_opacity.items()):
        print(f"\n{'=' * 70}")
        print(f"  Detection Boundary Map — target={target}  opacity={opacity:.1f}")
        print(f"{'=' * 70}")

        # Collect unique sizes and speeds
        sizes = sorted(set(r.params.blob_diameter for r in results))
        speeds = sorted(set(r.params.speed_px_per_sec for r in results))

        # Build lookup
        lookup = {}
        for r in results:
            lookup[(r.params.blob_diameter, r.params.speed_px_per_sec)] = r.detected

        # Header
        header = f"{'size↓ speed→':>14}"
        for spd in speeds:
            header += f" {spd:>6}"
        print(header)
        print("-" * len(header))

        # Rows
        for size in sizes:
            row = f"{size:>10} px  "
            for spd in speeds:
                hit = lookup.get((size, spd))
                if hit is None:
                    row += f" {'?':>6}"
                elif hit:
                    row += f" {'✅':>5}"
                else:
                    row += f" {'❌':>5}"
            print(row)

        # Summary
        detected = sum(1 for r in results if r.detected)
        total = len(results)
        print(f"\n  Detection rate: {detected}/{total} ({100*detected/total:.0f}%)")

    print()


def print_validation_table(results: List[dict]):
    """Print validation results for real videos."""
    print(f"\n{'=' * 78}")
    print(f"  Real Video Validation Results")
    print(f"{'=' * 78}")
    print(f"{'File':<40} {'Dur':>5} {'Disk':>5} {'Events':>7} {'Blobs':>6}")
    print("-" * 78)

    for r in results:
        name = r["name"]
        if len(name) > 38:
            name = name[:35] + "..."
        disk = "✅" if r["disk_detected"] else "❌"
        events = r["num_events"]
        ev_str = f"✅ {events}" if events > 0 else "❌ 0"
        print(
            f"{name:<40} {r['duration_seconds']:>5.1f} {disk:>5} {ev_str:>7} {r['total_detections']:>6}"
        )
        if r["error"]:
            print(f"  ⚠️  {r['error']}")
        for ev in r["events"]:
            t0 = ev.get("start_seconds", 0)
            t1 = ev.get("end_seconds", 0)
            dur_ms = ev.get("duration_ms", 0)
            conf = ev.get("confidence", "?")
            frames = ev.get("frame_count", 0)
            print(f"  └─ {t0:.2f}s–{t1:.2f}s  ({dur_ms}ms, {frames} frames, {conf})")

    detected = sum(1 for r in results if r["num_events"] > 0)
    print(f"\n  Detection rate: {detected}/{len(results)} files with events")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Transit detection test harness — validate analyzer sensitivity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── inject ────────────────────────────────────────────────────────────────
    p_inject = sub.add_parser(
        "inject",
        help="Inject a synthetic blob into a video and test detection",
    )
    p_inject.add_argument(
        "--source",
        type=str,
        default=None,
        help="Source MP4 (clean disc, no transit). Omit for synthetic disc.",
    )
    p_inject.add_argument(
        "--size", type=float, default=DEFAULT_BLOB_SIZE, help="Blob diameter in pixels"
    )
    p_inject.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_BLOB_SPEED,
        help="Blob speed in pixels/second",
    )
    p_inject.add_argument(
        "--opacity", type=float, default=DEFAULT_BLOB_OPACITY, help="Blob opacity (0-1)"
    )
    p_inject.add_argument(
        "--aspect",
        type=float,
        default=DEFAULT_BLOB_ASPECT,
        help="Blob aspect ratio (w/h)",
    )
    p_inject.add_argument(
        "--angle",
        type=float,
        default=DEFAULT_TRANSIT_ANGLE,
        help="Transit angle in degrees",
    )
    p_inject.add_argument(
        "--target", choices=["sun", "moon"], default="sun", help="Target body"
    )
    p_inject.add_argument(
        "--keep", action="store_true", help="Keep generated video files"
    )
    p_inject.add_argument(
        "--output-dir", type=str, default=None, help="Directory for output files"
    )
    p_inject.add_argument(
        "--diff-threshold",
        type=int,
        default=None,
        help="Override analyzer diff threshold",
    )
    p_inject.add_argument(
        "--min-blob-pixels",
        type=int,
        default=None,
        help="Override analyzer min blob pixels",
    )

    # ── sweep ─────────────────────────────────────────────────────────────────
    p_sweep = sub.add_parser(
        "sweep",
        help="Sweep size × speed to map detection boundaries",
    )
    p_sweep.add_argument(
        "--source", type=str, default=None, help="Source MP4 (clean disc)"
    )
    p_sweep.add_argument(
        "--target", choices=["sun", "moon"], default="sun", help="Target body"
    )
    p_sweep.add_argument(
        "--sizes",
        type=str,
        default="4,6,8,10,12,16,20,30",
        help="Comma-separated blob sizes (px)",
    )
    p_sweep.add_argument(
        "--speeds",
        type=str,
        default="20,40,60,80,120,160,200",
        help="Comma-separated speeds (px/s)",
    )
    p_sweep.add_argument(
        "--opacities",
        type=str,
        default="1.0",
        help="Comma-separated opacities (0-1)",
    )
    p_sweep.add_argument("--keep", action="store_true", help="Keep generated videos")
    p_sweep.add_argument(
        "--output-dir", type=str, default=None, help="Dir for output files"
    )
    p_sweep.add_argument("--json", action="store_true", help="Output results as JSON")
    p_sweep.add_argument("--diff-threshold", type=int, default=None)
    p_sweep.add_argument("--min-blob-pixels", type=int, default=None)

    # ── validate ──────────────────────────────────────────────────────────────
    p_val = sub.add_parser(
        "validate",
        help="Run analyzer on real MP4s and report detection results",
    )
    p_val.add_argument(
        "files",
        nargs="*",
        help="MP4 files to validate. If --dir is given, scans that directory instead.",
    )
    p_val.add_argument(
        "--dir", type=str, default=None, help="Directory to scan for MP4s"
    )
    p_val.add_argument(
        "--target", choices=["sun", "moon", "auto"], default="auto", help="Target body"
    )
    p_val.add_argument("--json", action="store_true", help="Output results as JSON")
    p_val.add_argument("--diff-threshold", type=int, default=None)
    p_val.add_argument("--min-blob-pixels", type=int, default=None)

    args = parser.parse_args()

    # Build analyzer kwargs from common options
    analyzer_kwargs = {}
    if hasattr(args, "diff_threshold") and args.diff_threshold is not None:
        analyzer_kwargs["diff_threshold"] = args.diff_threshold
    if hasattr(args, "min_blob_pixels") and args.min_blob_pixels is not None:
        analyzer_kwargs["min_blob_pixels"] = args.min_blob_pixels

    if args.command == "inject":
        params = InjectionParams(
            blob_diameter=args.size,
            speed_px_per_sec=args.speed,
            opacity=args.opacity,
            aspect_ratio=args.aspect,
            angle_deg=args.angle,
        )
        print(
            f"Injecting: size={args.size}px, speed={args.speed}px/s, "
            f"opacity={args.opacity}, target={args.target}"
        )

        r = run_injection_test(
            params,
            target=args.target,
            source_video=args.source,
            analyzer_kwargs=analyzer_kwargs or None,
            keep_video=args.keep,
            output_dir=args.output_dir,
        )

        status = "✅ DETECTED" if r.detected else "❌ MISSED"
        print(f"\nResult: {status}")
        print(
            f"  Ground truth: {r.ground_truth_start_sec:.2f}s – {r.ground_truth_end_sec:.2f}s"
        )
        print(f"  Analyzer found {r.num_events} event(s)")
        if r.matched_event:
            ev = r.matched_event
            print(
                f"  Matched event: {ev.get('start_seconds', 0):.2f}s–{ev.get('end_seconds', 0):.2f}s "
                f"({ev.get('duration_ms', 0)}ms, {ev.get('confidence', '?')})"
            )
        if r.analyzer_error:
            print(f"  ⚠️  Analyzer error: {r.analyzer_error}")

        sys.exit(0 if r.detected else 1)

    elif args.command == "sweep":
        sizes = [float(x) for x in args.sizes.split(",")]
        speeds = [float(x) for x in args.speeds.split(",")]
        opacities = [float(x) for x in args.opacities.split(",")]

        print(
            f"Sweep: {len(sizes)} sizes × {len(speeds)} speeds × {len(opacities)} opacities "
            f"= {len(sizes)*len(speeds)*len(opacities)} tests"
        )
        print(f"Target: {args.target}")
        print()

        sweep = run_sweep(
            sizes=sizes,
            speeds=speeds,
            opacities=opacities,
            target=args.target,
            source_video=args.source,
            analyzer_kwargs=analyzer_kwargs or None,
            keep_videos=args.keep,
            output_dir=args.output_dir,
        )

        if args.json:
            output = {
                "target": args.target,
                "total": sweep.total,
                "detected": sweep.detected,
                "missed": sweep.missed,
                "detection_rate": sweep.detection_rate,
                "results": [
                    {
                        "size": r.params.blob_diameter,
                        "speed": r.params.speed_px_per_sec,
                        "opacity": r.params.opacity,
                        "detected": r.detected,
                        "num_events": r.num_events,
                        "gt_start": r.ground_truth_start_sec,
                        "gt_end": r.ground_truth_end_sec,
                    }
                    for r in sweep.results
                ],
            }
            print(json.dumps(output, indent=2))
        else:
            print_sweep_table(sweep, args.target)

    elif args.command == "validate":
        video_paths = list(args.files) if args.files else []

        if args.dir:
            d = Path(args.dir)
            video_paths.extend(
                str(p)
                for p in sorted(d.glob("*.mp4"))
                if not p.stem.startswith("analyzed_")
                and not p.stem.endswith("_analyzed")
            )

        if not video_paths:
            print(
                "No MP4 files specified. Use positional args or --dir.", file=sys.stderr
            )
            sys.exit(1)

        print(f"Validating {len(video_paths)} video(s), target={args.target}")
        print()

        results = validate_real_videos(
            video_paths,
            target=args.target,
            analyzer_kwargs=analyzer_kwargs or None,
        )

        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print_validation_table(results)


if __name__ == "__main__":
    main()
