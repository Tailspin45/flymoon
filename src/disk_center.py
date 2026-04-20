"""Closed-loop visual centering of the Sun / Moon disk in the Seestar RTSP frame.

Given a connected Seestar client and ALPACA client, this module:

1. Grabs a single RTSP frame via ffmpeg (one-shot — never starts a persistent
   reader, so it does not interfere with detector / pre-buffer / preview).
2. Detects the bright disk (Hough circles with a contour fallback).
3. Converts the pixel offset from frame center to an on-sky arcsecond offset
   using the configured FoV.
4. Issues two short ``moveaxis`` pulses (axis 0 and 1), stops the axes, and
   settles.
5. Repeats up to ``max_iterations`` or until the residual is within
   ``tolerance_px``.

Axis mapping note
-----------------
The camera's image X axis is treated as motor axis 0 (azimuth / RA-style
axis) and Y as motor axis 1 (altitude / Dec-style axis). This is correct for
short corrections where the scope is pointing near the target — we nudge the
motors directly and are not correcting full sky-frame az (which would need
``az * cos(alt)`` compensation). For multi-degree corrections use GoTo, not
this routine.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

import numpy as np

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

from src import logger
from src.constants import get_ffmpeg_path
from src.solar_timelapse import _rtsp_grab_urls

_FFMPEG = get_ffmpeg_path() or "ffmpeg"

# Default Seestar S50 preview FoV (deg). Overridable via env.
_DEFAULT_FOV_X = 1.27
_DEFAULT_FOV_Y = 0.71
_FOV_LOGGED_ONCE = False


def _effective_fov() -> Tuple[float, float]:
    global _FOV_LOGGED_ONCE
    try:
        fx = float(os.getenv("SEESTAR_FOV_DEG_X", _DEFAULT_FOV_X))
    except ValueError:
        fx = _DEFAULT_FOV_X
    try:
        fy = float(os.getenv("SEESTAR_FOV_DEG_Y", _DEFAULT_FOV_Y))
    except ValueError:
        fy = _DEFAULT_FOV_Y
    if not _FOV_LOGGED_ONCE:
        logger.info(
            "[DiskCenter] Effective FoV deg X=%.3f Y=%.3f "
            "(override with SEESTAR_FOV_DEG_X / SEESTAR_FOV_DEG_Y)",
            fx,
            fy,
        )
        _FOV_LOGGED_ONCE = True
    return fx, fy


def detect_disk(frame: np.ndarray) -> Optional[Tuple[int, int, int]]:
    """Return (cx, cy, radius) of the bright disk, or None.

    De-duplicated from ``src.solar_timelapse._detect_disk``. Hough circles
    with a contour-based fallback.
    """
    if cv2 is None:
        return None
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (9, 9), 2)
    h, w = blurred.shape
    min_r = min(h, w) // 8
    max_r = min(h, w) // 2

    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=min(h, w) // 2,
        param1=50,
        param2=30,
        minRadius=min_r,
        maxRadius=max_r,
    )
    if circles is not None:
        c = np.round(circles[0][0]).astype(int)
        return int(c[0]), int(c[1]), int(c[2])

    _, thresh = cv2.threshold(blurred, 200, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) > (min_r**2 * np.pi):
            (cx, cy), radius = cv2.minEnclosingCircle(largest)
            return int(cx), int(cy), int(radius)
    return None


def frame_offset_to_arcsec(
    cx: int,
    cy: int,
    frame_w: int,
    frame_h: int,
    fov_deg_x: float,
    fov_deg_y: float,
) -> Tuple[float, float]:
    """Map pixel offset from frame center to an on-sky arcsec offset.

    Returns (d_axis0_arcsec, d_axis1_arcsec) where positive d_axis0 means the
    disk is to the right of frame center (camera X) and positive d_axis1 means
    the disk is above frame center (camera Y inverted to natural up).

    The caller issues a motion correction **opposite** to this offset (to drive
    the disk back to center). See module docstring on the camera X -> motor
    axis0 assumption.
    """
    off_x_px = cx - frame_w / 2.0
    off_y_px = cy - frame_h / 2.0
    arcsec_per_px_x = (fov_deg_x * 3600.0) / float(frame_w)
    arcsec_per_px_y = (fov_deg_y * 3600.0) / float(frame_h)
    d_axis0 = off_x_px * arcsec_per_px_x
    # Screen coords grow downward; invert so "above center" is positive.
    d_axis1 = -off_y_px * arcsec_per_px_y
    return d_axis0, d_axis1


def _grab_frame_once(
    host: str, rtsp_port: int, timeout_sec: int = 12
) -> Optional[np.ndarray]:
    """One-shot RTSP -> decoded BGR frame. Returns None on failure."""
    if cv2 is None:
        logger.warning("[DiskCenter] cv2 not available; cannot grab frame")
        return None
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        filepath = tmp.name
    try:
        for port, _path, url in _rtsp_grab_urls(host, rtsp_port):
            cmd = [
                _FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-timeout",
                "10000000",
                "-analyzeduration",
                "10000000",
                "-probesize",
                "10000000",
                "-i",
                url,
                "-frames:v",
                "1",
                "-update",
                "1",
                "-q:v",
                "2",
                "-y",
                filepath,
            ]
            try:
                r = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=timeout_sec,
                )
                if r.returncode == 0 and os.path.getsize(filepath) > 100:
                    img = cv2.imread(filepath)
                    if img is not None:
                        if port != rtsp_port:
                            logger.info(
                                "[DiskCenter] Used RTSP port %d (configured %d)",
                                port,
                                rtsp_port,
                            )
                        return img
            except subprocess.TimeoutExpired:
                logger.debug("[DiskCenter] RTSP grab timed out on %s", url)
            except Exception as e:
                logger.debug("[DiskCenter] RTSP grab error %s: %s", url, e)
        return None
    finally:
        try:
            os.unlink(filepath)
        except OSError:
            pass


def center_on_disk(
    client,
    alpaca,
    rtsp_url: Optional[str] = None,
    *,
    target: str,
    max_iterations: int = 6,
    tolerance_px: int = 8,
    fov_deg: Optional[Tuple[float, float]] = None,
    settle_s: float = 1.5,
    logger=logger,
) -> Dict[str, Any]:
    """Iteratively nudge the mount until the bright disk is within
    ``tolerance_px`` of frame center.

    Parameters
    ----------
    client
        Connected :class:`SeestarClient` (used for host, rtsp port, log context).
    alpaca
        Connected :class:`AlpacaClient` for moveaxis + stop_axes.
    rtsp_url
        Unused — kept for forward-compat API. The RTSP URL is built from
        ``client.host`` and ``SEESTAR_RTSP_PORT``.
    target
        ``"sun"`` or ``"moon"`` — informational (used in log context).
    max_iterations
        Maximum correction iterations.
    tolerance_px
        Success when the disk center is within this many pixels of frame
        center on both axes.
    fov_deg
        Override ``(fov_x, fov_y)`` in degrees. Defaults to env or S50 preview.
    settle_s
        Sleep between iterations after axis stop.

    Returns
    -------
    dict
        ``{"success": bool, "iterations": int, "final_offset_px": (dx, dy),
        "final_offset_arcsec": (d0, d1), "reason": str}``
    """
    if cv2 is None:
        return {"success": False, "reason": "cv2_unavailable"}
    if not client or not getattr(client, "host", None):
        return {"success": False, "reason": "no_client_host"}
    if not alpaca or not alpaca.is_connected():
        return {"success": False, "reason": "alpaca_not_connected"}

    fov_x, fov_y = fov_deg if fov_deg is not None else _effective_fov()
    rtsp_port = int(os.getenv("SEESTAR_RTSP_PORT", "4554"))

    try:
        maxrate0 = float(alpaca.get_max_move_rate(0)) or 6.0
        maxrate1 = float(alpaca.get_max_move_rate(1)) or 6.0
    except Exception:
        maxrate0 = maxrate1 = 6.0

    last_offset_px: Tuple[float, float] = (0.0, 0.0)
    last_offset_arcsec: Tuple[float, float] = (0.0, 0.0)
    reason = "max_iterations_reached"

    for i in range(1, max_iterations + 1):
        frame = _grab_frame_once(client.host, rtsp_port)
        if frame is None:
            logger.warning("[DiskCenter] iter %d: frame grab failed", i)
            reason = "frame_grab_failed"
            break
        h, w = frame.shape[:2]
        disk = detect_disk(frame)
        if disk is None:
            logger.warning("[DiskCenter] iter %d: disk not detected", i)
            reason = "disk_not_detected"
            break
        cx, cy, radius = disk
        dx = cx - w / 2.0
        dy = cy - h / 2.0
        last_offset_px = (dx, dy)
        d0_arcsec, d1_arcsec = frame_offset_to_arcsec(cx, cy, w, h, fov_x, fov_y)
        last_offset_arcsec = (d0_arcsec, d1_arcsec)
        logger.info(
            "[DiskCenter] iter %d target=%s offset_px=(%.1f,%.1f) "
            "offset_arcsec=(%.1f,%.1f) radius=%d",
            i,
            target,
            dx,
            dy,
            d0_arcsec,
            d1_arcsec,
            radius,
        )

        if abs(dx) <= tolerance_px and abs(dy) <= tolerance_px:
            return {
                "success": True,
                "iterations": i,
                "final_offset_px": last_offset_px,
                "final_offset_arcsec": last_offset_arcsec,
                "reason": "within_tolerance",
            }

        # Choose rate so a ~0.5 s pulse covers the correction. rate = arcsec / 0.5s / 3600 deg/sec
        pulse_s = 0.5
        rate0 = -(d0_arcsec / pulse_s) / 3600.0  # drive opposite to offset
        rate1 = -(d1_arcsec / pulse_s) / 3600.0
        rate0 = max(-maxrate0, min(maxrate0, rate0))
        rate1 = max(-maxrate1, min(maxrate1, rate1))

        try:
            alpaca.move_axis(0, rate0, timeout_sec=2.0)
            alpaca.move_axis(1, rate1, timeout_sec=2.0)
            time.sleep(pulse_s)
        finally:
            try:
                alpaca.stop_axes(timeout_sec=2.0)
            except Exception as e:
                logger.warning("[DiskCenter] stop_axes failed: %s", e)
        time.sleep(settle_s)

    return {
        "success": False,
        "iterations": max_iterations,
        "final_offset_px": last_offset_px,
        "final_offset_arcsec": last_offset_arcsec,
        "reason": reason,
    }
