# Automated Aircraft Transit Detection with Zipcatcher

**Zipcatcher** is an open-source tool for predicting, detecting, and recording aircraft as they cross the solar or lunar disk. It runs on any machine with Python 3.9 or later and optionally controls a ZWO Seestar S50 or S30 smart telescope.

*Zipcatcher was developed from a prototype by David Betancourt Montellano. Source code: https://github.com/Tailspin45/Zipcatcher*

---

## The Problem

A commercial aircraft transits the solar or lunar disk in roughly 0.5 to 2 seconds. The geometry is predictable — aircraft broadcast their position, speed, altitude, and heading continuously via ADS-B, and the position of the Sun or Moon is calculable to arc-second precision at any moment. Given both, it is straightforward to compute which flights will cross the disk, and when. The difficulty is doing this continuously, in advance, and triggering a camera fast enough to catch it.

Zipcatcher addresses all three.

---

## How It Works

### Prediction

Zipcatcher queries up to six flight data sources concurrently — OpenSky Network, ADSB-One, adsb.lol, adsb.fi, FlightAware AeroAPI, and ADS-B Exchange — and merges the results into a unified flight cache. Four of the six sources require no account or API key.

For each aircraft in the configured bounding box, Zipcatcher projects the flight path forward at constant velocity for up to 15 minutes and numerically optimizes for the moment of minimum angular separation from the target. Celestial positions are computed with Skyfield and the JPL DE421 ephemeris, with atmospheric refraction applied.

Candidates are classified by angular separation:

| Level | Separation | Meaning |
|-------|-----------|---------|
| High | ≤ 2.0° | Direct transit very likely |
| Medium | ≤ 4.0° | Near miss — worth recording |
| Low | ≤ 12.0° | Possible distant transit |

### The Map Interface

The main interface is a Leaflet.js map. Each aircraft in the bounding box is plotted and color-coded by transit probability. Clicking any aircraft shows its FlightAware historical track and the computed forward projection. Azimuth arrows indicate the current direction of the Sun and Moon from the observer. A countdown banner turns red when a high-probability transit is within the configured alert window.

Below the map, a sortable table lists every tracked flight with its target angle, plane angle, angular separation, altitude, speed, and predicted transit time.

### Telescope Control

Zipcatcher communicates with the Seestar S50 and S30 directly over TCP on port 4700, using the same JSON-RPC protocol the Seestar mobile app uses. No bridge software is required.

When a high-probability transit is predicted, Zipcatcher starts recording a configurable number of seconds before the predicted crossing (default: 10 s) and stops a configurable number of seconds after (default: 10 s). If a second transit falls within the same window, the recording extends to cover it rather than starting a new one.

The telescope panel streams a live MJPEG preview from the scope directly in the browser. Controls include: solar and lunar mode selection, continuous nudge (joystick-style), GoTo by coordinates, autofocus, zoom, and still capture.

Auto-discovery finds the scope via UDP broadcast on port 4720. If the connection drops overnight, Zipcatcher waits until the target rises above the configured minimum altitude before attempting to reconnect.

### Live Detection

When the telescope is connected, a frame-coherence computer-vision pipeline monitors the live RTSP stream independently of the prediction system:

- **Score A** — spike gate: flags a large per-frame brightness anomaly consistent with a fast-moving silhouette
- **Score B consecutive** — confirms the anomaly persists across multiple frames in a straight line
- **Score B matched-filter** — cross-correlates the signal against a bank of transit templates covering different speeds and sizes

All three gates require Score A to meet its threshold before accumulating, which prevents background noise from building toward a false detection. A centre-ratio gate rejects anomalies that are not concentrated near the disk centre. After a confirmed detection the system enforces a 6-second cooldown.

A lightweight CNN classifier runs over detection clips to score each event as a genuine transit or false positive. Training clips are stored in `data/training/` and the classifier can be retrained from the telescope panel.

### Post-Capture Analysis

**TransitAnalyzer** processes saved video to produce:

- A **composite image** — every frame where the aircraft was on the disk, blended over a clean reference background
- A **sidecar JSON** — frame-level signal scores and thresholds, used to annotate the scrubber in the gallery viewer

### Capture Gallery

A filmstrip at the bottom of the telescope panel accumulates every file from the session: video recordings, still captures, diff heatmaps, and composite images. The file viewer provides:

- A five-panel frame display showing the current frame with two frames of context on each side
- A frame scrubber with forward and reverse playback at native frame rate
- Non-destructive trim: mark In and Out points with the Mark button, then trim to write `trim_<filename>.mp4` alongside the original; replace the original only on explicit request
- Transit analysis overlay: runs the analyzer and draws signal data on the scrubber bar
- Composite assembly from marked frames

### Eclipse Mode

When a solar or lunar eclipse is in progress, Zipcatcher switches to timelapse mode. Frames are captured at a configurable interval throughout the event and assembled into a timelapse video. Aircraft transits detected during the eclipse are bookmarked as timestamped events within the recording.

Eclipse phase detection runs entirely in Python using Skyfield. Solar eclipses are found by scanning the six-hour window around each new moon and using a binary search to locate C1 and C4 contact times. Lunar eclipses are derived from umbral geometry.

The system issues alerts across five phases:

| Phase | Trigger | Response |
|-------|---------|----------|
| Outlook | Eclipse within 48 hours | Banner notification |
| Watch | Eclipse within 60 minutes | Countdown card |
| Warning | 30 seconds to C1 | Pulsing alert; recording arms |
| Active | C1 through C4 | Recording pinned; transit markers enabled |
| Cleared | Post-C4, 30-minute window | Summary card; file saved |

If an aircraft transit occurs during the Active phase, the existing recording is extended and a marker is dropped in the filmstrip thumbnail rather than starting a separate clip.

The entire eclipse response sequence can be rehearsed using the built-in simulator, which injects synthetic contact times and compresses the full progression from Watch through Cleared into under two minutes.

### Notifications

Telegram alerts fire for medium and high-probability transits, including the predicted transit time, flight callsign, altitude, aircraft type, and angular separation. Alerts can be muted per-session from the panel without restarting the server.

---

## Getting Started

Zipcatcher runs on macOS, Windows, and Linux with Python 3.9 or later. No API key is required for basic operation; four of the six flight data sources are free and unauthenticated.

```
git clone https://github.com/Tailspin45/Zipcatcher.git
cd Zipcatcher
make setup
source .venv/bin/activate
python app.py
```

Open `http://localhost:8000`. An interactive configuration wizard (`python3 src/config_wizard.py --setup`) walks through all settings. Pre-built installers for macOS and Windows are available from the project repository.

---

## Technical Summary

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.9+, Flask |
| Flight data | OpenSky, ADSB-One, adsb.lol, adsb.fi, FlightAware AeroAPI, ADS-B Exchange |
| Ephemeris | Skyfield + JPL DE421 |
| Map | Leaflet.js + OpenStreetMap |
| Telescope control | Seestar S50/S30 via TCP/JSON-RPC |
| Live detection | Frame-coherence pipeline + CNN classifier |
| Post-capture | TransitAnalyzer composite images + sidecar JSON |
| Eclipse detection | Skyfield eclipselib + binary-search contact solver |
| Frontend | Vanilla JS, CSS3 |
| Platform | macOS, Windows, Linux |

---

*Zipcatcher is open-source software released under the MIT License.*
*Source code and installation instructions: https://github.com/Tailspin45/Zipcatcher*
