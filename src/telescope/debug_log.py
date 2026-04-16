"""NDJSON agent debug logger for telescope operations.

Only writes when FLYMOON_AGENT_DEBUG_LOG env var is set to a file path.
Extracted from src/telescope_routes.py (v0.2.0 §3.1 mechanical split).
Log is rotated at 50 MB with 3 backups kept.
"""

import json
import logging
import logging.handlers
import os
import threading
import time

from src import logger

# NDJSON agent log — only written when FLYMOON_AGENT_DEBUG_LOG is set (file path).
_DEBUG_LOG_PATH = os.getenv("FLYMOON_AGENT_DEBUG_LOG", "").strip()
_DEBUG_SESSION_ID = os.getenv("FLYMOON_AGENT_DEBUG_SESSION", "flymoon")
_rtsp_recover_last_attempt_by_host_mode: dict[str, float] = {}
_rtsp_probe_fail_last_warn_by_host_mode: dict[str, float] = {}
_RTSP_RECOVERY_COOLDOWN_SECONDS = 5.0
_auto_detect_rtsp_warn_ts = 0.0

# Rotating log handler — created lazily on first write.
_ndjson_handler: logging.handlers.RotatingFileHandler | None = None
_ndjson_logger: logging.Logger | None = None
_ndjson_lock = threading.Lock()


def _get_ndjson_logger() -> logging.Logger:
    """Return (and lazily create) the rotating NDJSON file logger."""
    global _ndjson_handler, _ndjson_logger
    if _ndjson_logger is not None:
        return _ndjson_logger
    with _ndjson_lock:
        if _ndjson_logger is not None:
            return _ndjson_logger
        _ndjson_handler = logging.handlers.RotatingFileHandler(
            _DEBUG_LOG_PATH,
            maxBytes=50 * 1024 * 1024,  # 50 MB
            backupCount=3,
            encoding="utf-8",
        )
        _ndjson_handler.setFormatter(logging.Formatter("%(message)s"))
        _ndjson_logger = logging.getLogger("telescope.ndjson")
        _ndjson_logger.propagate = False
        _ndjson_logger.setLevel(logging.DEBUG)
        _ndjson_logger.addHandler(_ndjson_handler)
    return _ndjson_logger


def _agent_debug_log(
    run_id: str, hypothesis_id: str, location: str, message: str, data: dict
) -> None:
    if not _DEBUG_LOG_PATH:
        return
    try:
        payload = {
            "sessionId": _DEBUG_SESSION_ID,
            "id": f"log_{int(time.time() * 1000)}_{threading.get_ident()}",
            "timestamp": int(time.time() * 1000),
            "location": location,
            "message": message,
            "data": data,
            "runId": run_id,
            "hypothesisId": hypothesis_id,
        }
        _get_ndjson_logger().info(json.dumps(payload, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[telescope.debug_log] Failed to write agent debug log: %s", exc)
