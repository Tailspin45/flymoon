# Transit Capture - Quick Start Guide

## The System

This is your production transit capture system. It's designed to **automatically record** aircraft transits with your Seestar telescope, with a **manual fallback** if automatic control isn't working.

## Current Status (Firmware 6.70)

- **Automatic Mode**: Not available (JSON-RPC timeout issue)
- **Manual Mode**: ✓ Working (push notifications)
- **When Fixed**: System will automatically switch to automatic mode

## Quick Start

### 1. Test Your Seestar

```bash
python3 transit_capture.py --test-seestar
```

This tells you which mode will be used:
- **Automatic available** → Seestar will record automatically
- **Manual required** → You'll get notifications to record manually

### 2. Start Monitoring

```bash
python3 transit_capture.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun
```

The system will:
1. Try automatic mode first
2. Fall back to manual notifications if needed
3. Monitor for HIGH probability transits
4. Capture transits (automatically or notify you)

## How It Works

### Automatic Mode (Future - When Firmware Fixed)

```
[Detection] → [Scheduling] → [AUTO RECORD] → [STOP] → [Done]
```

- No user action needed
- Recordings happen automatically
- You can review captured videos later

### Manual Mode (Current - Firmware 6.70)

```
[Detection] → [Notification] → [User Opens App] → [User Records] → [Done]
```

**You receive two notifications:**

1. **Detection** (when transit first appears):
   ```
   🟢 HIGH Probability Transit Detected!
   Flight: UAL1234 in 23 minutes
   You will receive a warning 5 min before transit.
   ```

2. **Imminent** (5 minutes before):
   ```
   🚨 TRANSIT IMMINENT - 4 minutes

   ⏰ TIMING:
   Start recording: 14:23:35
   Stop recording: 14:23:55

   📱 ACTION:
   1. Open Seestar app NOW
   2. Confirm sun is centered
   3. Press RECORD at 14:23:35
   4. Press STOP at 14:23:55
   ```

## Commands

### Standard Usage

```bash
# Basic (tries automatic, falls back to manual)
python3 transit_capture.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun

# More responsive
python3 transit_capture.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun \
  --interval 5 \
  --warning 3

# Force manual mode (skip automatic test)
python3 transit_capture.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun \
  --manual
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--latitude` | Required | Your latitude |
| `--longitude` | Required | Your longitude |
| `--target` | sun | Target: sun, moon, or auto |
| `--interval` | 15 | Check every N minutes |
| `--warning` | 5 | Manual mode: warn N min before |
| `--manual` | - | Force manual mode |
| `--test-seestar` | - | Test Seestar and exit |

## What You See

### Startup (Manual Fallback)

```
============================================================
TRANSIT CAPTURE SYSTEM - STARTUP
============================================================
Target: sun
Location: 33.111369, -117.310169
Check interval: 15 min
============================================================

Attempting automatic mode (preferred)...
Testing Seestar connection...
  Seestar not responding to commands

Automatic mode unavailable (Seestar not responding)
Falling back to manual notification mode...
  PushBullet connected
  You will receive notifications to manually record

⚠️  MANUAL MODE ACTIVE (FALLBACK)
    Reason: Seestar firmware 6.70 JSON-RPC timeout issue
    You will receive notifications to manually record

============================================================
MODE: MANUAL
============================================================
⚠️  You will receive notifications for manual recording
⚠️  Keep Seestar app accessible
============================================================
MONITORING STARTED
```

### Startup (Automatic - Future)

```
Attempting automatic mode (preferred)...
Testing Seestar connection...
  Seestar responding to commands!
  Connected to Seestar at 192.168.7.221
  Setting sun viewing mode...
  Automatic mode ready
  Seestar will record transits automatically

✓✓✓ AUTOMATIC MODE ACTIVE ✓✓✓

============================================================
MODE: AUTOMATIC
============================================================
✓ Transits will be recorded automatically
✓ No user action required
============================================================
```

### During Operation

```
[14:00:00] Checking for transits...
Found 2 HIGH probability transits (out of 47 total)

[Manual Mode]
📱 Sent detection notification for UAL1234
Next check in 15 minutes at 14:15:00

[14:15:00] Checking for transits...
Found 2 HIGH probability transits (out of 51 total)
🚨 Sent IMMINENT notification for UAL1234
Next check in 15 minutes at 14:30:00

[Automatic Mode - Future]
⏰ Scheduling automatic recording for UAL1234
   Start: 14:23:35
   Duration: 20s
Waiting 480s until recording starts...
🎥 STARTING AUTOMATIC RECORDING
⏹️  STOPPING AUTOMATIC RECORDING
✓ Automatic capture complete
```

## Workflow

### Manual Mode (Current)

**Before Observing:**
1. Start the monitor: `python3 transit_capture.py --latitude ... --longitude ... --target sun`
2. Keep your phone nearby
3. Have Seestar app ready to open

**When Transit Detected:**
- You get first notification: "Transit in 23 minutes"
- Keep monitoring

**When Transit Imminent:**
- You get urgent notification with exact times
- Open Seestar app
- Verify sun/moon centered
- Press RECORD at start time
- Press STOP at stop time

### Automatic Mode (Future)

**Before Observing:**
1. Start the monitor
2. Walk away

**When Transit Detected:**
- System schedules recording
- Recording happens automatically
- No action needed

**After Session:**
- Review captured videos
- Download from Seestar if desired

## Troubleshooting

### No Notifications (Manual Mode)

Check PushBullet:
```bash
python3 -c "
from dotenv import load_dotenv
import os
load_dotenv()
from pushbullet import Pushbullet
pb = Pushbullet(os.getenv('PUSH_BULLET_API_KEY'))
pb.push_note('Test', 'PushBullet works!')
"
```

### Automatic Mode Not Working

The system will automatically detect this and fall back to manual mode. You'll see:
```
⚠️  MANUAL MODE ACTIVE (FALLBACK)
    Reason: Seestar firmware 6.70 JSON-RPC timeout issue
```

This is expected with current firmware. Continue with manual mode.

### Monitor Stops

```bash
# Run with logging
python3 transit_capture.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun 2>&1 | tee capture.log
```

## When Automatic Mode Works

Once the Seestar firmware issue is resolved:

1. Run the same command
2. System will detect Seestar is responding
3. Automatically switch to automatic mode
4. No code changes needed!

The system is designed to work **exactly the same way** whether automatic or manual. You just won't need to press buttons anymore.

## Files

- **`transit_capture.py`** - Main production system (this guide)
- **`monitor_transits.py`** - Manual-only version (if you want to force manual)
- **`examples/seestar_transit_trigger.py`** - Test/development script

## Support

- Development log: `SEESTAR_DEVELOPMENT_LOG.md`
- Manual mode guide: `TRANSIT_MONITOR_GUIDE.md`
- Integration docs: `SEESTAR_INTEGRATION.md`

---

**Bottom Line**: Run `transit_capture.py` with your coordinates. It will use the best available mode automatically.
