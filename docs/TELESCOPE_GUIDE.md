# Telescope Control UI - Usage Guide

## Quick Start

### 1. **Connect to Telescope**
- Click **🔌 Connect** button (top right panel)
- Wait for "Connected" status (green dot)
- All control buttons will become enabled

### 2. **Select Target (Sun or Moon)**
- Check visibility status for Sun ☀️ and Moon 🌙
- Green badge = "Visible" (above horizon)
- Grey badge = "Below Horizon" (button disabled)
- Click **☀️ View Sun** or **🌙 View Moon**
- **IMPORTANT:** Solar filter warning will appear for Sun!

### 3. **Capture Photo**
- Set exposure time (default: 1.0 seconds)
- Range: 0.1 to 30 seconds
- Click **📸 Capture Photo**
- Photo saves to telescope storage
- Appears in file list after ~2 seconds

### 4. **Record Video**
- Click **⏺️ Start Recording**
- Red pulsing dot appears when recording
- "Recording..." status shows
- Click **⏹️ Stop Recording** when done
- Video saves as MP4 to telescope storage

### 5. **View Files**
- Files appear in bottom-left panel
- Click **🔄 Refresh** to update list
- Click any file to download/view
- 📹 = Video file
- 📷 = Photo file

---

## Live Preview

**Requirements:**
- FFmpeg installed on your system
- Telescope connected and in viewing mode (Sun/Moon)
- RTSP stream available on port 4554

**Troubleshooting:**
- If "Preview Unavailable" appears:
  1. Install FFmpeg: `brew install ffmpeg` (Mac) or `apt install ffmpeg` (Linux)
  2. Ensure telescope is in Solar/Lunar mode (not deep-sky)
  3. Check SEESTAR_RTSP_PORT=4554 in .env file

**Note:** Preview may show placeholder until telescope is actively viewing a target.

---

## Mock Mode Testing

**To test without real hardware:**

1. Edit `.env` file:
   ```bash
   MOCK_TELESCOPE=true
   ENABLE_SEESTAR=false
   ```

2. Restart Flask app:
   ```bash
   python app.py
   ```

3. All features work with simulated responses:
   - Instant connection
   - Fake file list appears
   - No live preview (requires real RTSP)
   - Photo/video buttons work with delays

---

## Workflow Example

### **Capturing Sun Transit:**

```
1. Click "Connect" → Wait for green status
2. Check Sun visibility (should be "Visible" during day)
3. Click "☀️ View Sun" → Read solar filter warning!
4. Wait for live preview to appear (~5-10 seconds)
5. Click "Start Recording" before transit
6. Click "Stop Recording" after transit
7. Click "Refresh" to see new video file
8. Click video file to download
```

### **Taking Moon Photo:**

```
1. Click "Connect"
2. Check Moon visibility (visible at night)
3. Click "🌙 View Moon"
4. Set exposure time (e.g., 0.5s for bright Moon)
5. Click "Capture Photo"
6. Wait 2 seconds
7. Click "Refresh" to see new photo
8. Click photo to view/download
```

---

## Button States

| Button | When Enabled |
|--------|--------------|
| Connect | Always (when disconnected) |
| Disconnect | When connected |
| View Sun/Moon | When connected + target visible |
| Capture Photo | When connected |
| Start Recording | When connected + not recording |
| Stop Recording | When recording |
| Refresh Files | When connected |

---

## Status Indicators

### Connection Status
- **Green dot + "Connected"** → Ready to use
- **Grey dot + "Disconnected"** → Click Connect

### Recording Status  
- **Red pulsing dot** → Recording in progress
- **Grey dot** → Not recording

### Target Visibility
- **Green "Visible"** → Target above horizon, can switch
- **Grey "Below Horizon"** → Target not visible, button disabled

### Preview Status
- **"Live Stream Active"** → Preview working
- **"Preview Unavailable"** → Check FFmpeg/RTSP
- **"Not Connected"** → Connect telescope first

---

## Keyboard Shortcuts

None currently implemented. Use mouse/touch only.

---

## Tips

1. **Always use solar filter** when viewing Sun (hardware safety!)
2. **Photos work in Solar/Lunar/Scenery modes** (not deep-sky stacking)
3. **Videos save as MP4** format
4. **Recording has no duration limit** - stop manually
5. **File list refreshes** automatically after capture
6. **Manual refresh** available if files don't appear
7. **Exposure time** affects brightness (longer = brighter)
8. **Preview may be delayed** - Seestar needs time to start stream

---

## Common Issues

### "Timeout waiting for response"
- Telescope is busy or slow to respond
- Wait and try again
- Check if telescope is in correct mode

### "Not connected to telescope"  
- Click "Connect" button first
- Check SEESTAR_HOST in .env file
- Verify telescope is on and accessible

### Preview shows placeholder
- Telescope may not be in viewing mode yet
- Click Sun or Moon button first
- Wait 5-10 seconds for RTSP stream to start
- Check FFmpeg is installed

### Files not appearing
- Click "Refresh" button
- Wait 2-3 seconds after capture
- Telescope may be writing file to storage

### Buttons disabled
- Check connection status (must be connected)
- Check target visibility (Sun/Moon must be above horizon)
- Check recording state (can't start if already recording)

---

## Environment Variables

Key settings in `.env`:

```bash
# Telescope connection
SEESTAR_HOST=192.168.1.100  # Your telescope IP
SEESTAR_PORT=4700           # JSON-RPC port
SEESTAR_RTSP_PORT=4554      # RTSP stream port
SEESTAR_TIMEOUT=30          # Command timeout (seconds)

# Mode selection
ENABLE_SEESTAR=true         # Enable telescope features
MOCK_TELESCOPE=false        # Use mock client (testing)

# Observer location (for Sun/Moon visibility)
OBSERVER_LATITUDE=37.7749
OBSERVER_LONGITUDE=-122.4194
OBSERVER_ELEVATION=0
```

---

## API Endpoints (for reference)

The UI uses these backend endpoints:

- `POST /telescope/connect` - Connect to telescope
- `POST /telescope/disconnect` - Disconnect
- `GET /telescope/status` - Get connection/recording status
- `GET /telescope/target/visibility` - Get Sun/Moon positions
- `POST /telescope/target/sun` - Switch to solar mode
- `POST /telescope/target/moon` - Switch to lunar mode
- `POST /telescope/capture/photo` - Capture photo
- `POST /telescope/recording/start` - Start video recording
- `POST /telescope/recording/stop` - Stop video recording
- `GET /telescope/files` - List captured files
- `GET /telescope/preview/stream.mjpg` - MJPEG preview stream

---

## Support

For issues or questions:
1. Check server logs: Look at terminal where `python app.py` is running
2. Check browser console: F12 → Console tab
3. Try mock mode first to isolate hardware issues
4. Verify telescope is accessible via ping: `ping <SEESTAR_HOST>`
