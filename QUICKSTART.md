# Zipcatcher — Quick Start

> The iPhone Seestar app can coexist with Zipcatcher, but Zipcatcher must win the
> `master_cli` race. If motion commands seem to be ignored, force-quit the app and
> trigger a mode change so Zipcatcher reclaims master (see
> [docs/SEESTAR_APP_COEXISTENCE.md](docs/SEESTAR_APP_COEXISTENCE.md)).

## Option A: Prebuilt desktop app (recommended)

**No Python installation is required** for prebuilt installers.

Download the latest release:
- https://github.com/Tailspin45/Zipcatcher/releases

Install:
- **Windows**: run the `Zipcatcher-Setup-*.exe` installer (or `START-HERE-Install-Flymoon.bat` from the end-user ZIP)
- **macOS**: open the `.dmg`, drag Zipcatcher to **Applications**, then launch it

---

## Option B: Docker

### Step 1 — Install Docker Desktop

Download and install **[Docker Desktop](https://www.docker.com/get-started/)** for your platform (Mac, Windows, Linux).

### Step 2 — Download Zipcatcher

Open a terminal:

- **Mac** — Command + Space, type `Terminal`, press Return
- **Windows** — Windows key, type `cmd`, press Enter
- **Linux** — Ctrl + Alt + T

```bash
git clone https://github.com/Tailspin45/Zipcatcher.git
cd Zipcatcher
```

No git? [Download the ZIP](https://github.com/Tailspin45/Zipcatcher/archive/refs/heads/main.zip) and unzip it instead.

### Step 3 — Run the setup wizard

```bash
docker compose run --rm flymoon python3 src/config_wizard.py --setup
```

The wizard walks you through:
- Your location (latitude / longitude / elevation)
- Optional: FlightAware API key — adds airline and route metadata ([free personal tier](https://www.flightaware.com/aeroapi/signup/personal))
- Optional: Telegram notifications
- Optional: Seestar telescope

**Note:** Zipcatcher works with zero API keys out of the box, pulling flight positions from OpenSky, ADSB-One, adsb.lol, and adsb.fi — all free with no signup.

### Step 4 — Start Zipcatcher

```bash
docker compose up -d
```

Open **[http://localhost:8000](http://localhost:8000)** in your browser.

To stop: `docker compose down`  
To view logs: `docker compose logs -f`

---

## Option C: From source (Python required, Mac / Linux)

```bash
make setup
source .venv/bin/activate
python3 src/config_wizard.py --setup
python app.py
```

Open **[http://localhost:8000](http://localhost:8000)**.

---

## First Use

1. **Draw your bounding box** — drag the corners of the search rectangle to cover the area of sky you want to monitor
2. **Pick a target** — Sun or Moon
3. **Set minimum altitudes** — use the N / E / S / W quadrant inputs to mask out directions blocked by trees or buildings (click the centre button to reset all to zero)
4. **Enable auto-refresh** — set a polling interval so Zipcatcher monitors continuously

---

## Keeping Zipcatcher Updated

```bash
git pull
docker compose build
docker compose up -d
```

---

## Telescope (Seestar S50) — optional

Add to `.env`:

```bash
ENABLE_SEESTAR=true
SEESTAR_HOST=192.168.x.x   # leave blank to auto-discover on your LAN
```

Zipcatcher will slew to the target and start recording automatically before each predicted transit. If the scope disconnects overnight it waits until the target rises above your configured minimum altitude before reconnecting.

---

## Notifications — optional

Add to `.env`:

```bash
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

See **[SETUP.md](SETUP.md)** for how to create the Telegram bot.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Page won't load | Make sure Docker Desktop is running; check `docker compose logs` |
| No flights shown | Verify bounding box covers your sky; no API key is needed for basic operation |
| Sun/Moon not appearing | Target may be below your minimum altitude — lower the quadrant inputs |
| Telescope not found | Leave `SEESTAR_HOST=` blank to enable UDP auto-discovery |
| Need to re-run setup | `docker compose run --rm flymoon python3 src/config_wizard.py --setup` |

Full documentation → **[SETUP.md](SETUP.md)**
