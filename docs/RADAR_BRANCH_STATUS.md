# Flymoon — `radar` branch status
*Last updated: 2026-03-27 (by Claude Sonnet 4.6)*

---

## 1. Branch purpose

The `radar` branch extends the telescope ("scope") page with a proper radar-track
system to replace previously-jittery blip placement.  It also fixes a cluster of
reliability bugs: transit data not reaching the scope page, full-fetch timeouts,
port-conflict restarts, and garbage callsigns in the ADS-B pipeline.

---

## 2. What works ✅

### 2.1 Radar blip system (`static/telescope.js`)
- **α-β filter** smooths each track independently:
  - First detection → position stored, velocity unknown.
  - Second detection → velocity **bootstrapped directly** from displacement (no slow
    ramp-up; heading line appears correctly from the second update).
  - Subsequent detections → standard predict-correct cycle.
- **Diamond blips**: drawn as filled rotated squares, coloured by `possibility_level`:
  - `HIGH` (≤2°) → `#00FF00` (green)
  - `MEDIUM` (≤4°) → `#FFFF00` (yellow)
  - `LOW` (≤12°) → `#808080` (grey)
- **Tadpole velocity line**: fixed pixel length (`6 + speedKts/100 × 3`) drawn in the
  heading direction via a 5-second angular-space forward step.  Length is stable and
  only changes if reported airspeed changes — it no longer grows as the filter
  converges.
- **Blip persistence**: non-illuminated blips stay at alpha 0.82 (was 0.55) so they
  don't disappear between sweep passes.
- **Track lifecycle**: `pruneRadarTracks` removes stale real-flight tracks.  Synthetic
  `TEST-*` tracks are explicitly protected from pruning.
- **Enhanced mode** uses the α-β smoothed velocity to draw the prediction cone.

### 2.2 Radar test mode (`static/telescope.js`)
- "Test" button next to Default/Enhanced toggles synthetic aircraft simulation.
- Three synthetic aircraft injected every 2 s via `pushInterceptPoint`:
  - `TEST-H` (HIGH/green) — passes through disc centre (closest approach ≈ 0°).
  - `TEST-M` (MEDIUM/yellow) — passes near centre (≈ 1.5°).
  - `TEST-L` (LOW/grey) — further out (≈ 5°).
- Simulation stops cleanly; blips disappear after the simulation ends.
- First run used to drop blips; fixed by protecting `TEST-*` IDs from the real-flight
  prune cycle.

### 2.3 Scope ↔ map transit synchronisation (`static/app.js`, `static/telescope.js`)
- **Upcoming transits list** (scope page) now shows correct ETAs and live countdown:
  - Falls back to `f.time × 60` when `transit_eta_seconds` is absent (it is never
    set by the API — only `time` in minutes is returned).
  - Each entry stores its wall-clock receipt time (`ts`); `_renderUpcomingTransits()`
    subtracts elapsed time from the stored ETA on every render call.
  - Countdown shows `T−2m30s` format; transitions to `NOW` (red) at T-0; entry
    disappears 30 s after T-0.
  - `_renderUpcomingTransits()` is called once per second from the RAF draw loop.
- **`_radarFastUpdate` no longer clears the scope list on an empty recalculation**:
  Previously, if dead-reckoning drift caused `/transits/recalculate` to return 0
  confirmed transits, `injectMapTransits([])` wiped the scope's upcoming list even
  though the map still showed the cached transit.  Now `injectMapTransits` is only
  called from `_radarFastUpdate` when at least 1 transit is confirmed (lvl ≥ 1).
- **Soft refresh also pushes to radar**: After `_radarUpdateBase`, the soft-refresh
  path now also calls `pushInterceptPoint` for any confirmed transit candidates.
  This keeps radar blips alive during full-fetch outages.
- **Soft-refresh stale limit raised**: 300 s → 600 s.  Dead-reckoning continues for
  10 minutes instead of 5, giving resilience when ADS-B sources are rate-limited.
- **`_radarFastUpdate` prune guard**: `pruneRadarTracks` is only called when
  `/transits/recalculate` returned actual candidates — prevents accidental wipe of
  blips during quiet/rate-limited responses.

### 2.4 Full-fetch performance (`src/flight_sources.py`)
- `/flights` calls `get_transits()` twice (sun + moon), each spawning
  `fetch_multi_source_positions()`.  With adsb.lol timing out and adsb.fi
  rate-limited, each call could take the full 12 s wall-clock timeout → 24+ s total,
  pushing past the browser's 55 s `AbortController` timeout.
- **Fix**: 20-second module-level cache keyed on bbox coordinates.  The second call
  (moon) returns instantly from cache.  Full refresh now completes in ~12 s even
  when sources are failing.

### 2.5 Port-conflict on restart (`app.py`)
- Replaced SIGTERM + 0.8 s sleep with **SIGKILL + poll loop** (up to 3 s, 250 ms
  steps).  The new code confirms the port is actually free before letting Flask bind,
  eliminating the "Address already in use" error that occurred when the OS hadn't
  fully released the socket.

### 2.6 Garbage callsigns (`src/flight_sources.py`, `src/opensky.py`)
- `-29A30B`-style blips (a negated 24-bit ICAO hex, e.g. from a corrupted ADS-B
  frame) now filtered at source: callsigns starting with `-` or containing
  non-printable characters fall back to the ICAO hex string (or are dropped).
  Applied to both the readsb (`_parse_readsb_aircraft`) and OpenSky state-vector
  parsers.

### 2.7 ALPACA `canmoveaxis` 400 error (`src/alpaca_client.py`)
- The `_load_capabilities` loop was querying `canmoveaxis` without the required
  `Axis` parameter → HTTP 400 on every startup.
- Fixed: `canmoveaxis` is now queried with `{"Axis": "0"}`.
- `_get` failure log level demoted from `ERROR` to `WARNING` (capability probes are
  non-fatal).

### 2.8 Seestar heartbeat log noise (`src/seestar_client.py`)
- `[Wire] >>`, `[Reader] <<`, `PiStatus` events, and `pi_is_verified` heartbeats
  demoted from `WARNING` to `DEBUG`/`INFO`.  Terminal output is now clean.

### 2.9 Timelapse auto-resume spam (`src/solar_timelapse.py`, `src/telescope_routes.py`)
- `SOLAR_TIMELAPSE_AUTO_RESUME=true` (default) was starting a new timelapse session
  on every cold boot, causing continuous `[Timelapse] Frame grab failed: Connection
  refused` and `[Detector] Stream lost` warnings because the Seestar RTSP stream
  is only active when the scope is in a viewing mode.
- **Fix**: auto-resume now requires `has_frames_today()` to return `True` (i.e. at
  least one frame exists in today's directory).  Fresh boots are silent; genuine
  crash-resumes still work.

---

## 3. What doesn't work / known issues ⚠️

### 3.1 Live preview requires manual mode activation
The Seestar RTSP stream (`rtsp://<host>:4554/stream`) is only live when the scope
is in **Solar**, **Lunar**, or **Scenery** mode.  After a server restart the scope is
idle; the user must click one of those mode buttons in the telescope panel to start
the stream and see the live preview.

There is no automatic mode detection or auto-start of the stream on connect.
**Workaround**: click **Solar Mode** or **Scenery Mode** in the telescope tab.

### 3.2 Transit not reaching scope when full fetches fail continuously
When both adsb.fi (429 rate-limited, 60 s backoff) and adsb.lol (timeout) are down
simultaneously, all full fetches abort.  Soft refresh keeps running for up to 10
minutes with dead-reckoning.  Beyond that, no new flight data arrives and the scope
shows nothing.

The partial fix (soft-refresh pushes to radar, stale limit 600 s) buys time but does
not eliminate the root cause.

**Root cause**: the app is entirely dependent on free ADS-B sources with no SLA.
**Long-term fix**: configure `AEROAPI_API_KEY` and switch to `data_source=fa-only`
for reliable data, or add a local ADS-B receiver (`ADSB_LOCAL_URL`).

### 3.3 `transit_eta_seconds` never populated by the server
The `/transits/recalculate` and `/flights` endpoints return `time` (minutes to
closest approach) but not `transit_eta_seconds` (seconds).  The scope's upcoming
transit list now falls back to `time × 60`, so it displays correctly.  However, if
the transit time is >15 minutes (outside the `TOP_MINUTE=15` prediction window) the
ETA will be `—`.

### 3.4 Upcoming transit ETA ages out in memory only
When a transit is confirmed, the ETA is stored in `_upcomingTransits` with a wall-
clock timestamp.  Subsequent `injectMapTransits` calls only add/keep entries; they
do not remove expired ones (we guard against overwriting with empty results).  The
auto-expire is handled in `_renderUpcomingTransits` (entries vanish 30 s after T-0).
If the flight is still in the air but no longer qualifies (sep > 12°), it stays in
the list until the full-fetch overwrites it.

### 3.5 Code revision mismatch warning on restart
After a restart the browser console shows: `Code revision: 013cf52 — if stale:
pull, restart, or PYTHONDONTWRITEBYTECODE=1 python3 -B app.py`.  The revision is
from the last commit; unstaged changes are not reflected.  After the commit below
this will be correct.

---

## 4. Key file map

| File | Role |
|------|------|
| `app.py` | Flask entry point; port-conflict resolution; `/flights` and `/transits/recalculate` routes |
| `src/flight_sources.py` | Multi-source ADS-B fetch (adsb.lol, adsb.fi, adsb.one, OpenSky, ADSBX); 20 s cache; callsign sanitization |
| `src/opensky.py` | OpenSky state-vector parser; callsign sanitization |
| `src/transit.py` | `check_transit()`, `recalculate_transits()`, `get_possibility_level()` |
| `src/seestar_client.py` | Seestar JSON-RPC TCP client; heartbeat; `start_solar_mode()`, `start_scenery_mode()` |
| `src/alpaca_client.py` | ALPACA telescope API client; `_load_capabilities()` |
| `src/solar_timelapse.py` | RTSP frame grab; `has_frames_today()`; timelapse loop |
| `src/telescope_routes.py` | Flask telescope endpoints; auto-connect worker |
| `static/app.js` | `fetchFlights`, `softRefresh`, `_radarFastUpdate`, `_radarUpdateBase`, dead-reckoning |
| `static/telescope.js` | Radar canvas: α-β filter, blip draw, velocity line, test mode, upcoming transit list |
| `static/map.js` | Leaflet map; flight markers; possibility level colours |

---

## 5. Environment variables (relevant to this branch)

```
# ADS-B sources (all default true)
ADSB_LOL_ENABLED=true
ADSB_FI_ENABLED=true
ADSB_ONE_ENABLED=true

# Telescope
ENABLE_SEESTAR=true
MOCK_TELESCOPE=false        # was accidentally true; now false
SEESTAR_HOST=192.168.4.143
SEESTAR_PORT=4700
SEESTAR_RTSP_PORT=4554      # optional, defaults to 4554

# Timelapse
SOLAR_TIMELAPSE_AUTO_RESUME=true   # only resumes if frames exist today
SOLAR_TIMELAPSE_INTERVAL=120
```

---

## 6. Pending / recommended next steps

1. **Auto-activate RTSP on connect**: after `_auto_connect_seestar` succeeds, check
   the scope's current mode; if idle and sun is above horizon, call
   `start_solar_mode()` automatically.
2. **Populate `transit_eta_seconds`** server-side: in `check_transit()` and
   `recalculate_transits()`, add `"transit_eta_seconds": round(time * 60, 1)` to the
   response dict so the fallback in the frontend is no longer needed.
3. **Persist upcoming transits across fast-update cycles**: the current guard (don't
   clear on empty result) is correct but coarse.  A per-entry TTL based on the
   predicted transit time would be cleaner.
4. **Add `data_source` fallback UI**: let the user switch from `hybrid` to `fa-only`
   or `adsb-local` from the map page when free sources are down.
