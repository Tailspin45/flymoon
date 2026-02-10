# Transit Monitor - User Guide

## Overview

Automated notification system for high-probability aircraft transits. Since direct Seestar control has compatibility issues with firmware 6.70, this monitor sends push notifications to alert you when to manually start recording via the Seestar app.

## Features

- 🟢 **Monitors only HIGH probability transits** (green in web UI)
- ⏰ **Advance warning notifications** - Alert sent when transit first detected
- 🚨 **Imminent transit alerts** - Urgent notification with exact timing
- 📱 **Step-by-step instructions** - Clear actions to take
- ⏱️ **Precise timing** - Tells you exactly when to start/stop recording

## Requirements

1. **PushBullet account and API key** - Already configured in your `.env`
2. **Seestar app** - Must be accessible when notification arrives
3. **Python environment** - Already set up

## Quick Start

### Basic Usage

```bash
python3 monitor_transits.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun
```

### With Custom Settings

```bash
python3 monitor_transits.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun \
  --interval 10 \
  --warning 3
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--latitude` | Required | Your latitude in decimal degrees |
| `--longitude` | Required | Your longitude in decimal degrees |
| `--elevation` | 0 | Your elevation in meters |
| `--target` | sun | Target: `sun`, `moon`, or `auto` |
| `--interval` | 15 | Check for transits every N minutes |
| `--warning` | 5 | Send urgent alert N minutes before transit |

## Notification Flow

### 1. Initial Detection
When a HIGH probability transit is first detected:

```
🟢 HIGH Probability Transit Detected!

Flight: UAL1234
Route: LAX → SFO
Time: 23 minutes
Altitude Diff: 0.15°
Azimuth Diff: 0.08°

⏰ You will receive a warning 5 min before transit.
```

### 2. Imminent Alert
When transit is within warning time (default 5 minutes):

```
🚨 TRANSIT IMMINENT - 4 minutes

Flight: UAL1234
Route: LAX → SFO

⏰ TIMING:
Transit at: 14:23:45
Start recording: 14:23:35
Stop recording: 14:23:55

📱 ACTION REQUIRED:
1. Open Seestar app NOW
2. Confirm sun is centered
3. Be ready to press RECORD at 14:23:35
4. Stop recording at 14:23:55

Recording duration: 20 seconds
```

## Workflow

1. **Start the monitor** before you go out to observe
2. **Keep Seestar in view mode** (Solar System → Sun → Go Gazing)
3. **Wait for notifications** on your phone/computer
4. **When imminent alert arrives:**
   - Open Seestar app if not already open
   - Verify sun/moon is centered
   - Wait for specified start time
   - **Press RECORD button** at start time
   - **Press STOP button** at stop time

## Tips

### Timing Accuracy

- Start time includes 10-second pre-buffer (configurable in `.env`)
- Stop time includes 10-second post-buffer
- Total recording: ~20 seconds per transit
- Aircraft transit itself: <2 seconds

### Check Interval

- **10-15 minutes**: Good balance for active observing
- **5 minutes**: More responsive but more API calls
- **20-30 minutes**: Relaxed monitoring

### Warning Time

- **5 minutes**: Default, gives time to prepare
- **3 minutes**: If you're already at the scope
- **10 minutes**: If you need more preparation time

## Running in Background

### macOS/Linux

```bash
# Run with logging (recommended - stays attached to terminal)
python3 monitor_transits.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun \
  > monitor.log 2>&1

# Or use screen/tmux (see below) for detachable sessions
```

⚠️ **Warning**: Avoid using `nohup` with `&` as it's easy to forget about
background processes that continue consuming API credits.

### Keep Running in Terminal (Recommended)

```bash
# Use screen or tmux
screen -S transit
python3 monitor_transits.py --latitude 33.111369 --longitude -117.310169 --target sun

# Detach: Ctrl+A, then D
# Reattach: screen -r transit
```

## Configuration

### Environment Variables (`.env`)

```bash
# Notification settings
PUSH_BULLET_API_KEY=your_api_key_here

# Monitor settings
MONITOR_INTERVAL=15          # How often to check (minutes)

# Recording timing
SEESTAR_PRE_BUFFER=10       # Start recording N seconds early
SEESTAR_POST_BUFFER=10      # Keep recording N seconds after
```

## Troubleshooting

### No Notifications

1. **Check PushBullet API key**
   ```bash
   grep PUSH_BULLET_API_KEY .env
   ```

2. **Test PushBullet**
   ```bash
   python3 -c "
   from dotenv import load_dotenv
   import os
   load_dotenv()
   from pushbullet import Pushbullet
   pb = Pushbullet(os.getenv('PUSH_BULLET_API_KEY'))
   pb.push_note('Test', 'If you see this, PushBullet works!')
   "
   ```

3. **Check PushBullet app** - Make sure it's installed and logged in on your phone

### No High Probability Transits

- HIGH probability transits are rare (typically <1% of flights)
- Try running during peak flight times
- Check the web UI to see if any green transits appear
- You can test with lower warning time to catch more

### Monitor Stops

```bash
# Run with logging
python3 monitor_transits.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun 2>&1 | tee monitor.log
```

## Example Session

```
$ python3 monitor_transits.py --latitude 33.111 --longitude -117.310 --target sun
============================================================
Transit Monitor Started
============================================================
Target: sun
Location: 33.111, -117.31
Check interval: 15 minutes
Warning time: 5 minutes before transit
============================================================

[14:00:00] Checking for transits...
INFO: Found 2 HIGH probability transits out of 47 total
INFO: Notification sent: 🟢 HIGH Probability Transit Detected!
Next check in 15 minutes at 14:15:00

[14:15:00] Checking for transits...
INFO: Found 2 HIGH probability transits out of 51 total
Next check in 15 minutes at 14:30:00

[14:18:32] Checking for transits...
INFO: URGENT: Transit UAL1234 in 4.5 minutes!
INFO: Notification sent: 🚨 TRANSIT IMMINENT - 4 minutes
```

## Comparison with Automatic Control

| Feature | Manual (This System) | Automatic (Direct Control) |
|---------|---------------------|---------------------------|
| Firmware 6.70 | ✓ Works | ✗ Times out |
| Reliability | ✓ App tested | ✗ JSON-RPC unstable |
| Setup | ✓ Simple | Complex |
| User action | Push button on phone | None |
| Response time | ~2-5 seconds | Instant |
| Success rate | ✓ High (if user present) | ✗ Zero (firmware issue) |

## Next Steps

When Seestar firmware is updated or the JSON-RPC issue is resolved:
- Switch to automatic control using `examples/seestar_transit_trigger.py`
- No manual button pressing needed
- Fully automated capture

Until then, this notification system ensures you don't miss high-probability transits!

---

**Need Help?** Check the main project README or development log (SEESTAR_DEVELOPMENT_LOG.md)
