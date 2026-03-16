# Seestar Connection Improvements

## Summary

Added robust connection retry logic with exponential backoff to handle transient network issues and Seestar telescope timeouts.

## Changes Made

### 1. **SeestarClient Retry Parameters** (`src/seestar_client.py`)

Added configurable retry parameters to the `SeestarClient` class:

- `retry_attempts`: Number of connection attempts (default: 3)
- `retry_initial_delay`: Initial delay before first retry (default: 1 second)

**Exponential Backoff:**
- Attempt 1: Immediate connection
- Attempt 2: Wait 1s, then connect
- Attempt 3: Wait 2s, then connect
- Attempt 4: Wait 4s, then connect
- And so on (each delay doubles)

### 2. **Improved Connection Logic** (`src/seestar_client.py`)

The `connect()` method now:

1. Attempts connection up to `retry_attempts` times
2. Uses exponential backoff between retries
3. Logs detailed retry information
4. Automatically discovers Seestar on the network if retries timeout
5. Continues with new discovered IP on next retry
6. Raises a clear error message after all retries exhaust

**Before:**
```
ERROR:app:Failed to connect to Seestar: timed out
RuntimeError: Connection failed: timed out
```

**After:**
```
INFO:app:Connecting to Seestar at 192.168.1.100:4700 (attempt 1/3)...
WARNING:app:[Seestar] Connection attempt 1 failed: timed out
INFO:app:[Seestar] Connection attempt 2/3 (after 1s delay)
WARNING:app:[Seestar] Connection attempt 2 failed: timed out
INFO:app:[Seestar] Connection attempt 3/3 (after 2s delay)
RuntimeError: Connection failed after 3 attempts: timed out
```

### 3. **Better Error Responses** (`src/telescope_routes.py`)

The `/telescope/connect` endpoint now:

- Catches `RuntimeError` separately for connection failures
- Returns HTTP 503 (Service Unavailable) instead of 500
- Provides helpful error message to the user
- Includes diagnostic information about what went wrong

**Example Response:**
```json
{
  "success": false,
  "connected": false,
  "error": "Connection failed after 3 attempts: timed out",
  "message": "Failed to connect to Seestar. Check that the telescope is powered on, connected to the network, and SEESTAR_HOST is correct."
}
```

### 4. **Improved Auto-Discovery** (`src/seestar_client.py`)

The auto-discovery mechanism has been upgraded to support:
- **Multiple Network Interfaces:** Scans all local subnets found on the host.
- **Common IoT Subnets:** Always scans `192.168.4.x` (Seestar/ESP32 default), `192.168.0.x`, and `192.168.1.x` regardless of local IP.
- **mDNS Resolution:** Checks `seestar.local` and `seestar-2.local`.
- **Parallel Scanning:** Uses up to 100 threads for rapid discovery.

This ensures Seestar can be found even if your network uses a large subnet mask (e.g., /22) or if the telescope is on a different but routable subnet.

### 5. **Environment Configuration** (`.env.mock`)

Added new environment variables for controlling retry behavior:

```env
# Connection retry attempts (default: 3)
SEESTAR_RETRY_ATTEMPTS=3

# Initial delay before first retry in seconds (default: 1)
SEESTAR_RETRY_INITIAL_DELAY=1
```

These are optional and have sensible defaults.

## How to Use

### Default Behavior (No Configuration Needed)

The application will automatically retry 3 times with exponential backoff when Seestar connections timeout.

### Customize Retry Behavior

Edit your `.env` file:

```env
# Try 5 times with slower initial delay (2 seconds)
SEESTAR_RETRY_ATTEMPTS=5
SEESTAR_RETRY_INITIAL_DELAY=2
```

### Example Retry Timeline

With defaults (3 attempts, 1s initial delay):

```
Time  Event
0s    Attempt 1: Try to connect → TIMEOUT
0.1s  Attempt 2: Wait 1s before retrying
1.1s  Attempt 2: Try to connect → TIMEOUT
1.2s  Attempt 3: Wait 2s before retrying
3.2s  Attempt 3: Try to connect → SUCCESS (or final timeout)
```

Total time for 3 attempts: ~3.2 seconds (vs 0.1s without retries).

## Troubleshooting

### Still Getting "Connection Failed" Errors?

1. **Check Seestar is powered on** and connected to the network
2. **Verify SEESTAR_HOST** is correct (use `/telescope/discover` endpoint to auto-find)
3. **Increase timeout** if connection is slow:
   ```env
   SEESTAR_TIMEOUT=60
   ```
4. **Increase retry attempts** for unreliable networks:
   ```env
   SEESTAR_RETRY_ATTEMPTS=5
   SEESTAR_RETRY_INITIAL_DELAY=2
   ```
5. **Check network connectivity**:
   ```bash
   ping 192.168.1.100  # (replace with your SEESTAR_HOST)
   nc -zv 192.168.1.100 4700  # Check if port 4700 is open
   ```

### Auto-Discovery Not Working?

The auto-discovery feature automatically scans:
1. All local subnets (based on network interfaces)
2. `seestar.local` and `seestar-2.local` (mDNS)

If you're on a complex network (e.g., VLANs without mDNS forwarding), you may still need to:
1. Manually set `SEESTAR_HOST` in your `.env` file to the telescope's IP
2. Ensure routing exists between your computer and the telescope

## Testing

Run the included test script to verify retry logic:

```bash
python3 test_seestar_retry.py
```

Expected output shows successful retries, failures after exhausting attempts, and exponential backoff delays.

## Files Modified

- `src/seestar_client.py` - Added retry parameters and exponential backoff logic
- `src/telescope_routes.py` - Improved error handling and retry configuration
- `.env.mock` - Added new configuration parameters
- `test_seestar_retry.py` - New test script (not committed)

## Backward Compatibility

✅ Fully backward compatible. Existing `.env` files will use sensible defaults without modification.
