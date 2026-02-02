## Seestar Telescope Integration

Automated telescope control for aircraft transit capture using direct TCP/JSON-RPC communication.

## Overview

This feature enables Flymoon to automatically trigger video recording on a Seestar telescope when high-probability aircraft transits are predicted. By communicating directly with the Seestar via its native JSON-RPC protocol, the system eliminates external dependencies and provides a lightweight integration.

**Key Benefits:**
- ✅ **No external dependencies** - Talks directly to Seestar
- ✅ **Lightweight** - ~400 lines of code vs complex bridge apps
- ✅ **Simple** - JSON-RPC 2.0 over TCP sockets
- ✅ **Fast** - No middleman/proxy overhead
- ✅ **Open** - No licensing concerns

### How It Works

```
┌─────────────┐      ┌──────────────┐      ┌──────────┐
│  Flymoon    │─────▶│ TCP Socket   │─────▶│ Seestar  │
│  (Transit   │ JSON │  (Port 4700) │ JSON │ Telescope│
│  Predictor) │ RPC  │              │ RPC  │          │
└─────────────┘      └──────────────┘      └──────────┘
```

1. **Predict** when aircraft will transit the Sun or Moon
2. **Calculate** optimal recording timing with configurable buffers
3. **Schedule** recording to start before the transit (default: 10s early)
4. **Trigger** Seestar via JSON-RPC command
5. **Stop** recording after transit completes (default: 10s late)

Since aircraft transits last only 0.5-2 seconds, automated triggering is essential.

## Prerequisites

### Hardware
- **Seestar telescope** (S50 or S30 Pro)
  - Firmware with JSON-RPC support
  - Connected to WiFi network
  - In Solar or Lunar viewing mode

### Software
- **Python 3.9+**
- **Flymoon** with dependencies installed
- **Network access** to Seestar's IP address

### Network Setup
1. Connect Seestar to your WiFi network
2. Note the Seestar's IP address (check your router or Seestar app)
3. Ensure computer running Flymoon can reach Seestar IP
4. Test connectivity: `ping <seestar-ip>`

## Configuration

### 1. Get Seestar IP Address

**Option A: From Router**
- Log into your router
- Look for "Seestar" or "ZWO" device
- Note the assigned IP (e.g., 192.168.1.100)

**Option B: From Seestar App**
- Open Seestar mobile app
- Settings → Device Info
- Note the IP address

### 2. Configure Environment

Edit `.env` file:

```bash
# Enable Seestar integration
ENABLE_SEESTAR=true

# Seestar telescope IP address
SEESTAR_HOST=192.168.1.100

# TCP port (default: 4700, may vary by firmware version)
SEESTAR_PORT=4700

# Socket timeout in seconds
SEESTAR_TIMEOUT=10

# Recording timing buffers
SEESTAR_PRE_BUFFER=10   # Start recording 10s before transit
SEESTAR_POST_BUFFER=10  # Continue recording 10s after transit
```

### 3. Test Connection

```bash
python3 examples/seestar_transit_trigger.py --test
```

Expected output:
```
Seestar Connection Test
============================================================
Testing connection to 192.168.1.100:4700...

1. Connecting...
   ✓ Connected

2. Checking status...
   ✓ Status: {'connected': True, 'recording': False, ...}

3. Testing recording commands...
   NOTE: Video recording commands need hardware testing
   Recording state: True
   Recording state: False

4. Disconnecting...
   ✓ Disconnected

============================================================
✓ Connection test complete!
```

## Usage

### Automated Monitoring

Monitor for transits and automatically trigger Seestar:

```bash
python3 examples/seestar_transit_trigger.py \
  --latitude 33.111369 \
  --longitude -117.310169 \
  --elevation 0 \
  --target moon \
  --interval 60
```

**Output:**
```
Transit Monitor with Seestar Telescope Control
============================================================
Location: 33.111369, -117.310169 @ 0m
Target: moon
Check interval: 60s

✓ Connected to Seestar at 192.168.1.100:4700
✓ Transit recorder ready (buffers: 10s / 10s)

Monitoring for high-probability transits...
Press Ctrl+C to stop

[14:23:45] Found 2 high-probability transits
  🎯 UAL1234 (KLAX→KSAN): ETA 187s - HIGH probability
     ✓ Recording scheduled
  🎯 AAL5678 (KPHX→KLAX): ETA 245s - MEDIUM probability
     ✓ Recording scheduled
```

### Programmatic Usage

```python
from src.seestar_client import SeestarClient, TransitRecorder, create_client_from_env

# Create client from environment
client = create_client_from_env()

# Or create manually
client = SeestarClient(host="192.168.1.100", port=4700)

# Connect
client.connect()

# Check status
status = client.get_status()
print(f"Connected: {status['connected']}")

# Manual recording
client.start_recording(duration_seconds=30)
# ... wait ...
client.stop_recording()

# Automated transit recording
recorder = TransitRecorder(client, pre_buffer_seconds=10, post_buffer_seconds=10)
recorder.schedule_transit_recording(
    flight_id="UAL1234",
    eta_seconds=120,
    transit_duration_estimate=2.0
)

# Disconnect
client.disconnect()
```

## Protocol Details

### JSON-RPC 2.0 Format

Seestar uses standard JSON-RPC 2.0 over TCP sockets:

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "scope_get_equ_coord",
  "id": 1,
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": { ... }
}
```

Messages are delimited with `\r\n`.

### Known Commands

| Command | Purpose | Source |
|---------|---------|--------|
| `scope_get_equ_coord` | Get telescope coordinates (heartbeat) | seestar_alp |
| `iscope_start_view` | Initialize view mode | seestar_alp |
| `iscope_stop_view` | Stop view mode | seestar_alp |
| `iscope_start_stack` | Start image stacking | seestar_alp |

### Video Recording Commands (TBD)

The exact JSON-RPC methods for video recording need to be discovered through testing. See "Command Discovery" section below.

## Command Discovery

Since Seestar's video recording API isn't publicly documented, you'll need to discover the commands:

### Method 1: Network Traffic Capture

```bash
# Install Wireshark
brew install wireshark  # macOS
# or apt install wireshark  # Linux

# Start capture
1. Open Wireshark
2. Capture on WiFi interface
3. Filter: tcp.port == 4700 or tcp.port == 4720
4. Use Seestar app to start video recording
5. Look for JSON-RPC messages
6. Note the "method" field values
```

### Method 2: Examine seestar_alp Source

```bash
# Clone the repo
git clone https://github.com/smart-underworld/seestar_alp
cd seestar_alp

# Search for video/recording methods
grep -r "record\|video" device/
grep -r "start.*view\|stop.*view" device/

# Check Bruno API collection
ls bruno/Seestar\ Alpaca\ API/
```

### Method 3: Discovery Script

```bash
python3 examples/seestar_transit_trigger.py --discover
```

Shows a guide with likely method names to try.

### Discovered Commands

Video recording commands have been discovered from the seestar_alp source code:

- **Start recording**: `start_record_avi`
  - Parameters: `{"raw": false}` for processed MP4 video
  - Must be in solar or lunar viewing mode first

- **Stop recording**: `stop_record_avi`
  - No parameters required
  - Recording is saved as MP4 on Seestar

### Implementation

These commands are now implemented in `src/seestar_client.py`:

```python
def start_recording(self, duration_seconds=None):
    params = {"raw": False}  # Processed MP4 video
    response = self._send_command("start_record_avi", params=params)
    # ...

def stop_recording(self):
    response = self._send_command("stop_record_avi")
    # ...
```

## Timing Considerations

Aircraft transits are **extremely brief** (0.5-2 seconds). Timing is critical:

### Buffer Configuration

| Buffer | Default | Purpose |
|--------|---------|---------|
| Pre-buffer | 10s | Accounts for prediction uncertainty |
| Transit | ~2s | Actual transit duration |
| Post-buffer | 10s | Captures edge cases and variations |

**Total recording**: 22 seconds per transit

### Adjusting Buffers

If you're **missing transits**:
```bash
# Increase pre-buffer
SEESTAR_PRE_BUFFER=15
```

If you're capturing **too much empty video**:
```bash
# Reduce buffers (only if predictions are very accurate)
SEESTAR_PRE_BUFFER=5
SEESTAR_POST_BUFFER=5
```

### Storage Estimates

At typical Seestar video quality (~10 MB/minute):

| Scenario | Storage |
|----------|---------|
| Single transit (22s) | ~3.7 MB |
| 10 transits/hour | ~37 MB |
| Full night (8 hours) | ~300 MB |
| 30 nights | ~9 GB |

## Troubleshooting

### Connection Refused

**Symptoms:** `Connection refused` or timeout errors

**Solutions:**
1. Verify Seestar is powered on
2. Check WiFi connection (Seestar and computer on same network)
3. Confirm IP address:
   ```bash
   ping 192.168.1.100
   ```
4. Try different ports: 4700, 4720, 8080
5. Check firewall settings

### Wrong Port

**Symptoms:** Connection times out, no error

**Try these ports:**
```bash
# Common Seestar ports
SEESTAR_PORT=4700
SEESTAR_PORT=4720
SEESTAR_PORT=8080
```

### Heartbeat Failures

**Symptoms:** Connection drops after 30-60 seconds

**Solutions:**
1. Check network stability
2. Reduce heartbeat interval:
   ```python
   client = SeestarClient(host="...", heartbeat_interval=15)
   ```
3. Monitor Seestar app for device status

### Recording Not Working

**Symptoms:** Script says recording started, but no video saved

**Likely cause:** Video recording commands not yet implemented

**Next steps:**
1. Run command discovery (see "Command Discovery" section)
2. Update `src/seestar_client.py` with correct methods
3. Test manually with Seestar app first
4. Ensure Seestar is in Solar/Lunar mode (not deep sky)

### Transits Not Captured

**Symptoms:** Recording happens, but no aircraft in video

**Possible causes:**
1. **Timing too tight** - Increase `SEESTAR_PRE_BUFFER`
2. **Predictions inaccurate** - Verify observer location is correct
3. **Clock sync issues** - Check system clock (use NTP)
4. **Telescope pointing** - Ensure Seestar aimed at Sun/Moon

## Implementation Status

### ✅ Completed

- Direct TCP/JSON-RPC client
- Connection management
- Heartbeat keepalive
- Status monitoring
- Timing calculations
- Automated scheduling
- Configuration system
- Example scripts
- Documentation

### ⚠️ Requires Hardware Testing

- Port number verification (confirmed 4700)
- Timing precision validation
- Error handling for edge cases

### 📋 Implementation Checklist

To complete the integration:

- [x] Discover video recording JSON-RPC methods (start_record_avi, stop_record_avi)
- [x] Update `start_recording()` method
- [x] Update `stop_recording()` method
- [ ] Test with actual Seestar hardware
- [ ] Verify timing precision with real transits
- [ ] Document any quirks or limitations
- [ ] Add error recovery for network issues

## API Reference

### SeestarClient

```python
class SeestarClient:
    def __init__(self, host, port=4700, timeout=10, heartbeat_interval=30)
    def connect(self) -> bool
    def disconnect(self) -> bool
    def is_connected(self) -> bool
    def start_recording(self, duration_seconds=None) -> bool
    def stop_recording(self) -> bool
    def is_recording(self) -> bool
    def get_status(self) -> Dict[str, Any]
```

### TransitRecorder

```python
class TransitRecorder:
    def __init__(self, seestar_client, pre_buffer_seconds=10, post_buffer_seconds=10)
    def schedule_transit_recording(self, flight_id, eta_seconds, transit_duration_estimate=2.0) -> bool
    def cancel_all(self)
```

### Helper Functions

```python
def create_client_from_env() -> Optional[SeestarClient]
```

## Comparison: Direct vs seestar_alp

| Feature | Direct (This) | seestar_alp |
|---------|---------------|-------------|
| **Lines of code** | ~400 | ~10,000+ |
| **Dependencies** | None | Web server, INDI, Alpaca libraries |
| **Setup** | Configure IP | Install app, run server, configure |
| **Latency** | Direct TCP | HTTP → Server → TCP |
| **License** | MIT (your choice) | GPL-3.0 (copyleft) |
| **Maintenance** | Simple updates | Track upstream changes |
| **Features** | Transit triggering only | Full telescope control suite |

**When to use each:**
- **Direct (this)**: You only need automated transit capture
- **seestar_alp**: You want full telescope control, scheduling, mosaics, etc.

## Future Enhancements

### Potential Features

1. **Auto-discovery** - Scan network for Seestar devices
2. **Multi-telescope** - Control multiple Seestars simultaneously
3. **Recording management** - Auto-name files with transit metadata
4. **Web UI** - Browser-based control interface
5. **Machine learning** - Optimize buffers based on capture success

### Community Contributions

Help improve this integration:
1. **Test with hardware** - Discover video recording commands
2. **Document quirks** - Share findings and edge cases
3. **Submit PRs** - Improvements and bug fixes
4. **Report issues** - GitHub issues with logs

## Resources

- **Seestar Official**: https://www.seestar.com/
- **seestar_alp Project**: https://github.com/smart-underworld/seestar_alp
- **JSON-RPC 2.0 Spec**: https://www.jsonrpc.org/specification
- **Wireshark**: https://www.wireshark.org/

## Example: Complete Workflow

```bash
# 1. Setup
cp .env.mock .env
# Edit .env - set ENABLE_SEESTAR=true and SEESTAR_HOST

# 2. Test connection
python3 examples/seestar_transit_trigger.py --test

# 3. Discover commands (if video doesn't work)
python3 examples/seestar_transit_trigger.py --discover
# Follow the guide to find video recording methods

# 4. Update src/seestar_client.py with discovered commands

# 5. Run automated monitoring
python3 examples/seestar_transit_trigger.py \
  --latitude 33.11 \
  --longitude -117.31 \
  --target moon

# 6. Let it run - it will automatically trigger recordings!
```

## License

Same as Flymoon project.

---

**Status**: Framework complete, video commands need hardware testing
**Last Updated**: 2026-02-02
**Maintainer**: Flymoon project
**Contact**: Via GitHub issues
