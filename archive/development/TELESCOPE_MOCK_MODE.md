# Telescope Mock Mode

## Overview

Mock mode allows you to test the telescope control interface without actual Seestar hardware. All operations are simulated with realistic delays.

## Enable Mock Mode

### Temporary (Current Session)

```bash
cd dist/Flymoon-Web
MOCK_TELESCOPE=true python app.py
```

### Permanent (Add to .env)

Add this line to your `.env` file:
```
MOCK_TELESCOPE=true
```

Then start the app normally:
```bash
cd dist/Flymoon-Web
python app.py
```

## Disable Mock Mode

### Remove from environment:
```bash
cd dist/Flymoon-Web
python app.py
```

### Or set to false in .env:
```
MOCK_TELESCOPE=false
```

## Mock Features

The mock telescope simulates:

✅ **Connection** - Instant connection with 0.5s delay
✅ **Viewing Modes** - Solar, lunar, and stop modes with delays
✅ **Recording** - Start/stop recording with timer
✅ **File Listing** - Returns sample solar and lunar transit videos
✅ **Status Updates** - Real-time status polling

## Mock Data

**Sample Files Returned:**
- Solar_2026-02-03/
  - transit_143000.mp4
  - transit_150000.mp4
- Lunar_2026-02-02/
  - moon_213000.mp4

## UI Indicator

When mock mode is active, the interface shows:
- Status: "Connected [MOCK MODE]" or "Disconnected [MOCK MODE]"
- Host: "mock.telescope"
- Connection info: "Mock telescope ready"

## Switching Between Real and Mock

1. **Stop the app**: `Ctrl+C` or kill the process
2. **Toggle MOCK_TELESCOPE** environment variable
3. **Restart the app**

## Testing Workflow

1. Start app in mock mode
2. Open http://localhost:8000/telescope
3. Click "Connect" → Mock connection succeeds
4. Click "Solar Mode" → Mode activates instantly
5. Click "Start Recording" → Recording starts with timer
6. Click "Stop Recording" → Recording stops with duration
7. Click "Refresh" files → Shows mock transit videos
8. Click "Disconnect" → Mock disconnects

## Use Cases

- **UI Development** - Test interface without hardware
- **Demo Mode** - Show the system to others
- **Integration Testing** - Verify API contracts
- **Training** - Learn the interface safely
- **Firmware Issues** - Work around Seestar firmware 6.70 timeouts

## Real Telescope

To use the real Seestar telescope:

1. Set `MOCK_TELESCOPE=false` or unset it
2. Ensure `ENABLE_SEESTAR=true`
3. Configure `SEESTAR_HOST` with your telescope's IP
4. Start the app
5. Make sure telescope is powered on and accessible

## Current Configuration

Your `.env` currently has:
```
ENABLE_SEESTAR=true
SEESTAR_HOST=192.168.7.221
SEESTAR_PORT=4700
```

To use mock mode, simply add:
```
MOCK_TELESCOPE=true
```
