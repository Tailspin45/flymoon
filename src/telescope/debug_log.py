"""NDJSON agent debug logger for telescope operations.

Only writes when FLYMOON_AGENT_DEBUG_LOG env var is set to a file path.
Extracted from src/telescope_routes.py (v0.2.0 §3.1 mechanical split).
"""

import json
import os
import threading
import time

from src import logger

# NDJSON agent log — only written when FLYMOON_AGENT_DEBUG_LOG is set (file path).
_DEBUG_LOG_PATH = os.getenv("FLYMOON_AGENT_DEBUG_LOG", "").strip()
_DEBUG_SESSION_ID = os.getenv("FLYMOON_AGENT_DEBUG_SESSION", "flymoon")
_rtsp_recover_last_attempt_by_host_mode: dict[str, float] = {}
_RTSP_RECOVERY_COOLDOWN_SECONDS = 5.0
_auto_detect_rtsp_warn_ts = 0.0


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
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[telescope.debug_log] Failed to write agent debug log: %s", exc)
