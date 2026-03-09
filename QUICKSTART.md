# Flymoon — Quick Start

## 1. Install

```bash
make setup
source .venv/bin/activate
```

## 2. Configure

```bash
cp .env.mock .env
```

Open `.env` and set the three required values:

```
AEROAPI_API_KEY=your_flightaware_key
OBSERVER_LATITUDE=your_lat
OBSERVER_LONGITUDE=your_lon
OBSERVER_ELEVATION=your_elevation_metres
```

Everything else is optional. Run the config wizard for a guided check of all settings:

```bash
python3 src/config_wizard.py --setup
```

## 3. Run

```bash
python app.py
```

Open the URL printed at startup (e.g. `http://192.168.1.x:8000`). The same address works from any device on your local network.

---

## Using the Map

**Set your bounding box** — drag the corners of the search rectangle to cover the sky area you can observe.

**Pick a target** — select Sun or Moon from the target toggle. Flymoon will only show flights that could transit the chosen body.

**Set minimum altitudes** — the four quadrant inputs (N / E / S / W) let you set independent minimum angles for each compass direction. Raise the value for any direction blocked by trees or buildings so those low transits are filtered out.

**Hit Search** — flights appear colour-coded by transit probability. Click any flight to see its planned route and historical track.

**Auto-refresh** — enable the timer to re-check every few minutes automatically. Sound alerts fire when a new high-probability transit appears.

---

## Telescope (Seestar S50)

Add to `.env`:

```
ENABLE_SEESTAR=true
SEESTAR_HOST=192.168.x.x   # or leave blank to auto-discover
```

Connect from the telescope panel on the right side of the map page. Flymoon will:

- Switch the scope to Solar or Lunar mode to match the selected target
- Start recording automatically before each predicted transit (default: 10 s early)
- Stop recording after the transit passes (default: 10 s buffer)
- Reconnect automatically if the scope drops off the network — but only once the target is back above the minimum altitude you set in the quadrant controls, so there are no pointless reconnect attempts overnight

Analysed composite images appear in the **Gallery** after each session.

---

## Headless / Overnight Mode

No browser needed. Both scripts run continuously in the background:

```bash
# Telegram notifications + Seestar control
python3 transit_capture.py --latitude LAT --longitude LON --target moon

# Pushbullet notifications only
python3 monitor_transits.py --latitude LAT --longitude LON --target moon
```

### macOS App

```bash
./build_mac_app.sh        # one-time build
```

Double-click **Transit Monitor.app**, choose your target, and leave it running. Logs: `/tmp/transit_monitor.log`.

---

## Notifications

Add to `.env` to receive Telegram alerts for medium and high probability transits:

```
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

See **[SETUP.md](SETUP.md)** for how to create the bot and find your chat ID.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No flights shown | Check `AEROAPI_API_KEY` and bounding box covers your sky |
| Sun/Moon not visible | Target may be below your minimum altitude — check the quadrant inputs |
| Telescope not found | Try `SEESTAR_HOST=` blank to enable auto-discovery, or check the scope is on the same subnet |
| Config errors on startup | Run `python3 src/config_wizard.py --setup` |

Full documentation → **[SETUP.md](SETUP.md)**
