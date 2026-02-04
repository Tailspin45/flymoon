# 🌙 Flymoon - Aircraft Transit Tracker

Track aircraft transiting the Sun and Moon in real-time with automatic telescope photography.

![Flymoon Interface](data/assets/flymoon2.png)

## ✨ Features

- **Real-Time Transit Detection** - Monitor flights up to 15 minutes ahead for potential transits
- **Interactive Map** - Leaflet-based visualization with flight routes, altitude indicators, and azimuth arrows
- **Smart Probability Analysis** - Color-coded transit likelihood (🟢 High, 🟠 Medium, 🟡 Low)
- **Automatic Telescope Control** - Integrated Seestar S50 support with automatic recording
- **Telegram Notifications** - Get alerts when possible transits are detected
- **Flight Tracking** - Real-time data from FlightAware AeroAPI

## 🚀 Quick Start

### Prerequisites
- Python 3.9+
- FlightAware AeroAPI account ([Free Personal Tier](https://www.flightaware.com/aeroapi/signup/personal))

### Installation

**macOS/Linux:**
```bash
make setup
source .venv/bin/activate
```

**Windows:**
```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Configuration

1. **Create `.env` file:**
   ```bash
   cp .env.mock .env
   ```

2. **Add your FlightAware API key:**
   ```
   AEROAPI_KEY=your_api_key_here
   ```

3. **Optional: Set up Telegram or Telescope** - See [SETUP.md](SETUP.md) for detailed instructions

### Run

```bash
python app.py
```

Access the web interface at `http://localhost:8000` or the displayed LAN address (e.g., `http://192.168.1.100:8000`)

## 📖 Documentation

- **[QUICKSTART.md](QUICKSTART.md)** - Fast-track setup guide
- **[SETUP.md](SETUP.md)** - Complete setup instructions (Telegram, Telescope)
- **[LICENSE](LICENSE)** - MIT License

## 🎯 How It Works

1. **Set Your Location** - Enter your coordinates (lat/lon/elevation)
2. **Define Search Area** - Draw or adjust bounding box on map
3. **Select Target** - Choose Sun, Moon, or Auto mode
4. **Monitor Transits** - View real-time flight data with transit predictions
5. **Automatic Recording** - Connected telescope automatically captures transits

### Transit Probability

Transits are ranked by the angular difference between aircraft and celestial target:

- **🟢 High (Green)** - Very likely transit, minimal angular difference
- **🟠 Medium (Orange)** - Possible transit, small angular difference
- **🟡 Low (Yellow)** - Low probability, larger angular difference

## 🗺️ Map Features

- **Altitude Overlay** - Thin horizontal bars show aircraft altitude (clickable)
- **Route Display** - Click any indicator to show planned route and historical track
- **Azimuth Arrows** - Visual direction indicators to Sun/Moon
- **Bounding Box** - Adjustable search area (drag corners to resize)

## ⚙️ Advanced Features

### Auto-Refresh Mode
Set automatic checks every N minutes with sound alerts for detected transits

### Telescope Integration
Automatic video recording when transits are detected (Seestar S50 supported)

### Telegram Notifications
Receive instant alerts on your phone for medium/high probability transits

## 🔧 Technical Details

### Transit Detection Algorithm
Uses numerical optimization to find minimum angular separation between aircraft and target. Assumes constant velocity and heading over 15-minute prediction window.

### Data Sources
- **Flight Data**: FlightAware AeroAPI
- **Celestial Calculations**: Skyfield with JPL ephemeris (de421.bsp)
- **Map Tiles**: OpenStreetMap

## 📊 Requirements

- **API Rate Limits**: FlightAware Personal tier allows 10 queries/minute
- **Network**: LAN access for telescope control (if using)
- **Storage**: ~50MB per transit video (if recording enabled)

## 🤝 Contributing

Contributions welcome! Please open an issue or pull request for:
- Bug fixes
- Feature enhancements
- Documentation improvements

**Share Your Transits!** Post your transit photos in [this issue](https://github.com/dbetm/flymoon/issues/21)

## 📝 Credits

Created with contributions from the Flymoon community. Special thanks to all contributors and transit photographers!

## 📄 License

MIT License - See [LICENSE](LICENSE) for details

---

**Pro Tip**: Use Flightradar24 alongside Flymoon for additional flight tracking context.
