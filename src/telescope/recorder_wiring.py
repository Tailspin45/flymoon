"""TransitRecorder scheduling glue for the telescope page background poll.

Extracted from src/telescope_routes.py ``transit_check()``
(v0.2.0 §3.1 mechanical split).

The telescope JS calls ``POST /telescope/transit/check`` every 90 s when the
scope is in solar/lunar mode.  This module holds the recorder scheduling logic
so it can be tested and extended without touching the full routes file.
"""

from src import logger


def schedule_recordings_for_transits(high_flights: list, recorder) -> None:
    """Schedule a recording for every HIGH-probability transit in *high_flights*.

    Args:
        high_flights: List of flight dicts with at least ``ident``/``id``,
            ``time`` (minutes to closest approach), and ``angular_separation``
            (degrees) keys.
        recorder: A ``TransitRecorder`` instance (from
            ``app.get_transit_recorder()``).  Pass ``None`` to no-op silently.
    """
    if recorder is None:
        return
    for flight in high_flights:
        eta_seconds = flight.get("time", 0) * 60
        flight_id = flight.get("ident") or flight.get("id", "unknown")
        try:
            recorder.schedule_transit_recording(
                flight_id=flight_id,
                eta_seconds=eta_seconds,
                transit_duration_estimate=2.0,
                sep_deg=flight.get("angular_separation", 0.0),
            )
            logger.info(
                "[TransitCheck] Scheduled recording for %s ETA=%.0fs "
                "via background telescope poll",
                flight_id,
                eta_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[TransitCheck] schedule_transit_recording failed for %s: %s",
                flight_id,
                exc,
            )
