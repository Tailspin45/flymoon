# Flymoon Setup Guide

Complete setup instructions for Flymoon's automated aircraft transit monitoring system with Telegram notifications and telescope integration.

## Table of Contents

1. [Quick Start](#quick-start)
2. [Prerequisites](#prerequisites)
3. [Telegram Bot Setup](#telegram-bot-setup)
4. [Telescope Integration Setup](#telescope-integration-setup)
5. [Configuration](#configuration)
6. [Testing Your Setup](#testing-your-setup)
7. [Usage Examples](#usage-examples)
8. [Troubleshooting](#troubleshooting)

---

## Quick Start

Flymoon predicts when aircraft will transit the Sun or Moon from your location and can:
- Send real-time notifications via Telegram
- Automatically trigger video recording on a Seestar telescope

This guide will help you set up both features.

---

## Prerequisites

### Required
- Python 3.9 or higher
- Flymoon installed with dependencies
- Internet connection for FlightAware API access

### Optional (for Telegram notifications)
- Telegram account
- Mobile device or computer with Telegram installed

### Optional (for telescope integration)
- Seestar telescope (S50 or S30 Pro)
- Seestar connected to WiFi network
- Computer and telescope on the same network

---

## Telegram Bot Setup

Get instant notifications on your phone or computer when high-probability transits are detected.

### Step 1: Create a Telegram Bot

1. Open Telegram and search for `@BotFather`
2. Start a chat with BotFather
3. Send the command: `/newbot`
4. Follow the prompts:
   - Give your bot a name (e.g., "My Transit Monitor")
   - Give your bot a username (must end in "bot", e.g., "my_transit_monitor_bot")
5. BotFather will give you a **bot token** that looks like:
   ```
   123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
   ```
6. Copy this token - you'll need it for configuration

### Step 2: Get Your Chat ID

1. Search for `@userinfobot` in Telegram
2. Start a chat with userinfobot
3. It will automatically reply with your user information
4. Copy your **chat ID** (it's a number like `123456789`)

### Step 3: Start Your Bot

1. Search for your bot by username in Telegram
2. Start a chat with it
3. Send `/start` to activate the bot

That's it! Your Telegram bot is ready. Now add the credentials to your configuration file (see [Configuration](#configuration) section).

### Telegram Features

- Works on iOS, Android, Web, and Desktop
- Messages are free and unlimited
- Your bot is private - only people you share the username with can find it
- You can customize your bot's profile picture and description via @BotFather

---

## Telescope Integration Setup

Automate video recording on your Seestar telescope for predicted aircraft transits.

### Overview

Flymoon communicates directly with your Seestar telescope using its native JSON-RPC protocol over TCP. No external bridge apps or complex dependencies needed.

**Key Benefits:**
- Direct communication with Seestar (no middleman)
- Lightweight implementation (~400 lines of code)
- Fast response times
- Simple JSON-RPC 2.0 protocol
- No licensing concerns

**How It Works:**
1. Flymoon predicts when aircraft will transit the Sun or Moon
2. Calculates optimal recording timing with configurable buffers
3. Schedules recording to start before the transit (default: 10 seconds early)
4. Triggers Seestar via JSON-RPC command
5. Stops recording after transit completes (default: 10 seconds late)

Since aircraft transits last only 0.5-2 seconds, automated triggering is essential.

### Hardware Requirements

- Seestar telescope (S50 or S30 Pro)
- Firmware with JSON-RPC support
- Seestar connected to WiFi network
- Computer running Flymoon on the same network

### Step 1: Get Your Seestar IP Address

You need to find your Seestar's IP address on your network.

**Option A: From Your Router**
1. Log into your router's admin interface
2. Look for connected devices list
3. Find device named "Seestar" or "ZWO"
4. Note the IP address (e.g., 192.168.1.100)

**Option B: From Seestar Mobile App**
1. Open the Seestar mobile app
2. Go to Settings → Device Info
3. Note the IP address shown

### Step 2: Test Network Connectivity

Verify your computer can reach the Seestar:

```bash
ping 192.168.1.100
```

Replace `192.168.1.100` with your actual Seestar IP address. You should see successful ping responses.

### Step 3: Configure Telescope Settings

Add Seestar settings to your `.env` file (see [Configuration](#configuration) section below).

### Step 4: Put Seestar in Solar/Lunar Mode

Before running Flymoon with telescope integration:

1. Point your Seestar at the Sun or Moon
2. Use the Seestar app to enter Solar or Lunar viewing mode
3. Leave the telescope in this mode while Flymoon is monitoring

---

## Configuration

All configuration is done through the `.env` file in your Flymoon directory.

### Create Configuration File

If you don't have a `.env` file yet:

```bash
cp .env.mock .env
```

Then edit `.env` with your favorite text editor.

### Basic Configuration

```bash
# FlightAware API credentials (required)
AEROAPI_API_KEY=your_api_key_here

# Your observer location (required)
OBSERVER_LATITUDE=33.111369
OBSERVER_LONGITUDE=-117.310169
OBSERVER_ELEVATION=0
```

### Telegram Configuration (Optional)

Add these lines if you want Telegram notifications:

```bash
# Telegram Bot Settings
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_CHAT_ID=123456789
```

Replace with the bot token and chat ID from the [Telegram Bot Setup](#telegram-bot-setup) section.

### Telescope Configuration (Optional)

Add these lines if you want automated telescope control:

```bash
# Enable Seestar integration
ENABLE_SEESTAR=true

# Seestar telescope IP address (required if enabled)
SEESTAR_HOST=192.168.1.100

# TCP port (default: 4700, may vary by firmware)
SEESTAR_PORT=4700

# Connection timeout in seconds
SEESTAR_TIMEOUT=10

# Recording timing buffers (seconds)
SEESTAR_PRE_BUFFER=10   # Start recording 10s before predicted transit
SEESTAR_POST_BUFFER=10  # Continue recording 10s after transit
```

Replace `192.168.1.100` with your actual Seestar IP address.

### Complete Example Configuration

```bash
# FlightAware API
AEROAPI_API_KEY=your_api_key_here

# Observer Location
OBSERVER_LATITUDE=33.111369
OBSERVER_LONGITUDE=-117.310169
OBSERVER_ELEVATION=0

# Telegram Notifications
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_CHAT_ID=123456789

# Seestar Telescope
ENABLE_SEESTAR=true
SEESTAR_HOST=192.168.1.100
SEESTAR_PORT=4700
SEESTAR_TIMEOUT=10
SEESTAR_PRE_BUFFER=10
SEESTAR_POST_BUFFER=10
```

---

## Testing Your Setup

### Test Telegram Notifications

Run the transit capture script to test notifications:

```bash
python3 examples/transit_capture.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target moon
```

If configured correctly, you should receive a Telegram message when a high-probability transit is detected.

### Test Telescope Connection

Run the connection test:

```bash
python3 examples/seestar_transit_trigger.py --test
```

**Expected output:**
```
Seestar Connection Test
============================================================
Testing connection to 192.168.1.100:4700...

1. Connecting...
   ✓ Connected

2. Checking status...
   ✓ Status: {'connected': True, 'recording': False, ...}

3. Testing recording commands...
   Recording state: True
   Recording state: False

4. Disconnecting...
   ✓ Disconnected

============================================================
✓ Connection test complete!
```

If the test fails, see the [Troubleshooting](#troubleshooting) section.

### Test Full System

Run the automated monitoring with both Telegram and telescope enabled:

```bash
python3 examples/seestar_transit_trigger.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target moon \
  --interval 60
```

This will:
- Monitor for aircraft transits every 60 seconds
- Send Telegram notifications for high-probability transits
- Automatically trigger telescope recording
- Display status updates in the console

---

## Usage Examples

### Monitor Sun Transits with Notifications Only

```bash
python3 examples/transit_capture.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target sun \
  --interval 30
```

### Monitor Moon Transits with Telescope

```bash
python3 examples/seestar_transit_trigger.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --target moon \
  --interval 60
```

### Adjust Recording Buffers

If you're missing transits, increase the pre-buffer in your `.env` file:

```bash
SEESTAR_PRE_BUFFER=15  # Start recording 15s early instead of 10s
```

If you're capturing too much empty video, decrease the buffers:

```bash
SEESTAR_PRE_BUFFER=5
SEESTAR_POST_BUFFER=5
```

### Storage Estimates

At typical Seestar video quality (~10 MB/minute):

| Scenario | Recording Time | Storage |
|----------|----------------|---------|
| Single transit | 22 seconds | ~3.7 MB |
| 10 transits/hour | 3.7 minutes | ~37 MB |
| Full night (8 hours) | 30 minutes | ~300 MB |
| 30 nights | 15 hours | ~9 GB |

---

## Troubleshooting

### Telegram Bot Issues

**Bot not sending messages?**
- Make sure you've started a chat with your bot (send `/start`)
- Verify your bot token is correct in `.env`
- Verify your chat ID is correct in `.env`
- Check that your bot token hasn't expired

**How to find my chat ID again?**
- Use `@userinfobot` or `@get_id_bot` on Telegram

**Getting API errors?**
- Make sure there are no extra spaces in your token or chat ID
- Ensure the values are on a single line in the `.env` file

### Telescope Connection Issues

**Connection Refused**

Symptoms: `Connection refused` or timeout errors

Solutions:
1. Verify Seestar is powered on
2. Check WiFi connection (Seestar and computer on same network)
3. Confirm IP address is correct: `ping 192.168.1.100`
4. Try different ports in `.env`:
   ```bash
   SEESTAR_PORT=4700  # Try this first
   SEESTAR_PORT=4720  # Or this
   SEESTAR_PORT=8080  # Or this
   ```
5. Check firewall settings on your computer

**Heartbeat Failures**

Symptoms: Connection drops after 30-60 seconds

Solutions:
1. Check network stability
2. Ensure Seestar isn't going to sleep or standby mode
3. Keep the Seestar app closed while running Flymoon
4. Monitor Seestar's status lights for connectivity issues

**Recording Not Working**

Symptoms: Script says recording started, but no video saved

Solutions:
1. Ensure Seestar is in Solar or Lunar viewing mode (not deep sky mode)
2. Check available storage on Seestar
3. Try starting a recording manually through the Seestar app first
4. Verify firmware version supports video recording via API

**Transits Not Captured in Video**

Symptoms: Recording happens, but no aircraft visible in video

Possible causes:
1. **Timing too tight** - Increase `SEESTAR_PRE_BUFFER` to 15 or 20 seconds
2. **Predictions inaccurate** - Verify observer location coordinates are correct
3. **Clock sync issues** - Ensure your computer's clock is accurate (use NTP)
4. **Telescope pointing** - Verify Seestar is accurately pointed at Sun/Moon center

### General Issues

**No transits detected?**
- Verify your location coordinates are correct
- Try a longer monitoring period (transits are relatively rare)
- Check that FlightAware API key is valid and has available queries
- Ensure there's actual air traffic in your area at the time

**Script crashes or errors?**
- Check that all required dependencies are installed
- Verify Python version is 3.9 or higher
- Review error messages carefully
- Check file permissions on the Flymoon directory

---

## Advanced Configuration

### Timing Considerations

Aircraft transits are extremely brief (0.5-2 seconds). The buffer configuration is critical:

| Buffer | Default | Purpose |
|--------|---------|---------|
| Pre-buffer | 10s | Accounts for prediction uncertainty and trigger latency |
| Transit | ~2s | Actual transit duration |
| Post-buffer | 10s | Captures edge cases and timing variations |

**Total recording time**: ~22 seconds per transit

### Protocol Details

Flymoon uses JSON-RPC 2.0 over TCP sockets to communicate with Seestar:

**Request format:**
```json
{
  "jsonrpc": "2.0",
  "method": "start_record_avi",
  "id": 1,
  "params": {"raw": false}
}
```

**Response format:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": { ... }
}
```

### Key Commands

- `start_record_avi` - Start video recording (MP4 format)
- `stop_record_avi` - Stop video recording
- `scope_get_equ_coord` - Get telescope coordinates (used for heartbeat)

---

## Getting Help

If you encounter issues not covered in this guide:

1. Check the project's GitHub issues page
2. Review the log output carefully for error messages
3. Verify all prerequisites are met
4. Try the test commands to isolate the problem
5. Open a new issue with detailed information:
   - Error messages
   - Your configuration (with sensitive data removed)
   - Steps to reproduce
   - System information (OS, Python version, etc.)

---

## What's Next?

Once your setup is working:

1. **Optimize timing** - Adjust buffers based on your capture success rate
2. **Experiment with targets** - Try both Sun and Moon transits
3. **Monitor regularly** - Run during peak air traffic times for more transits
4. **Review captures** - Analyze your recordings to verify transit captures
5. **Share results** - Post your successful captures to astronomy communities

Happy transit hunting!

---

**Last Updated**: 2026-02-04
**Project**: Flymoon
**Documentation**: See project README for more details
