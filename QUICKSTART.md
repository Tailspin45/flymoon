# Flymoon — Quick Start

## The Easy Way: Docker

### Step 1 — Install Docker Desktop

Go to **[docker.com/get-started](https://www.docker.com/get-started/)**, click the big green download button, and install it. Works on Mac, Windows, and Linux.

### Step 2 — Download Flymoon


Windows: “Press the Windows key, type cmd, then press Enter.”

Linux: “Press Ctrl+Alt+T.”

If that doesn’t work on Linux: “Open the apps menu, search for Terminal, and open it.”

Mac / Apple: “Press Command + Space, type Terminal, then press Return.”

Copy/paste the following

git clone https://github.com/Tailspin45/flymoon.git
cd flymoon

No git? [Download the zip](https://github.com/Tailspin45/flymoon/archive/refs/heads/main.zip) and unzip it instead.

### Step 3 — Run the setup wizard

docker compose run --rm flymoon python3 src/config_wizard.py --setup


This walks you through everything interactively:
- Your location (lat / lon / elevation)
- Your FlightAware API key — the wizard opens the signup page for you
- ([free personal tier](https://www.flightaware.com/aeroapi/signup/personal))
- Optional: Telegram notifications
- Optional: Seestar telescope

### Step 4 — Start Flymoon

docker compose up -d

Open **[http://localhost:8000](http://localhost:8000)** in your browser. That's it.

To stop it: `docker compose down`  
To see logs: `docker compose logs -f`

---

## First Use

1. **Draw your bounding box** — drag the corners of the search rectangle to cover the patch of sky you want to see
2. **Pick a target** — Sun or Moon
3. **Set minimum altitudes** — use the N / E / S / W quadrant inputs to mask out directions blocked by trees or buildings
3a. Click "Min Angle" in quadrant to reset to zero in each quadrant
5. **Enable auto-refresh** — set a check interval so Flymoon monitors continuously

---

## Keeping Flymoon Updated (frequent bug fixes and updates)

git pull
docker compose build
docker compose up -d


---

## Without Docker (Mac / Linux)

```bash
make setup
source .venv/bin/activate
python3 src/config_wizard.py --setup
python app.py
```

---

## Telescope (Seestar S50)

Add to `.env` with text editor:

ENABLE_SEESTAR=true
SEESTAR_HOST=192.168.x.x   # leave blank to auto-discover on your LAN

Flymoon will start recording automatically before each predicted transit and stop after. If the scope disconnects overnight it waits until the target is back above your minimum altitude before reconnecting.

---

## Notifications

Add to `.env`:

TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id


See **[SETUP.md](SETUP.md)** for how to create the Telegram bot.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Page won't load | Make sure Docker Desktop is running; check `docker compose logs` |
| No flights shown | Check `AEROAPI_API_KEY` in `.env` and ensure the bounding box covers your sky |
| Sun/Moon not appearing | Target may be below your minimum altitude — lower the quadrant inputs |
| Telescope not found | Set `SEESTAR_HOST=` blank to enable auto-discovery |
| Need to re-run setup | `docker compose run --rm flymoon python3 src/config_wizard.py --setup` |

Full documentation → **[SETUP.md](SETUP.md)**
