#!/usr/bin/env python3
"""
Example: Automated Seestar telescope triggering for transit capture.

This script demonstrates how to use the direct Seestar client to automatically
trigger video recording when high-probability aircraft transits are predicted.

Features:
- Direct TCP/JSON-RPC communication (no seestar_alp needed)
- Automated timing with configurable buffers
- Monitors for high-probability transits
- Schedules recordings with proper pre/post buffers

Prerequisites:
1. Seestar telescope on network (get IP from Seestar app)
2. Environment variables configured in .env
3. Seestar in Solar or Lunar viewing mode
"""

import time
from datetime import datetime

from src.seestar_client import SeestarClient, TransitRecorder, create_client_from_env
from src.transit import get_transits


def monitor_and_trigger(
    latitude: float,
    longitude: float,
    elevation: float,
    target: str = "moon",
    check_interval_seconds: int = 60,
):
    """
    Monitor for transits and automatically trigger telescope recording.

    Parameters
    ----------
    latitude : float
        Observer latitude in decimal degrees
    longitude : float
        Observer longitude in decimal degrees
    elevation : float
        Observer elevation in meters
    target : str
        Target celestial body ('moon', 'sun', or 'auto')
    check_interval_seconds : int
        How often to check for transits (default: 60 seconds)
    """
    print(f"Transit Monitor with Seestar Telescope Control")
    print(f"=" * 60)
    print(f"Location: {latitude}, {longitude} @ {elevation}m")
    print(f"Target: {target}")
    print(f"Check interval: {check_interval_seconds}s")
    print()

    # Create Seestar client from environment
    client = create_client_from_env()
    if client is None:
        print("ERROR: Seestar integration not enabled or configured")
        print("Set ENABLE_SEESTAR=true and SEESTAR_HOST in .env file")
        return

    # Connect to telescope
    try:
        if not client.connect():
            print("ERROR: Failed to connect to telescope")
            return
        print(f"✓ Connected to Seestar at {client.host}:{client.port}")
    except Exception as e:
        print(f"ERROR: Connection failed: {e}")
        print("\nTroubleshooting:")
        print("1. Check Seestar is powered on and on WiFi")
        print("2. Verify SEESTAR_HOST IP address in .env")
        print("3. Check firewall allows connections")
        print("4. Try pinging the Seestar IP")
        return

    # Create transit recorder with timing buffers
    import os
    pre_buffer = int(os.getenv("SEESTAR_PRE_BUFFER", "10"))
    post_buffer = int(os.getenv("SEESTAR_POST_BUFFER", "10"))
    recorder = TransitRecorder(client, pre_buffer, post_buffer)

    print(f"✓ Transit recorder ready (buffers: {pre_buffer}s / {post_buffer}s)")
    print()
    print("Monitoring for high-probability transits...")
    print("Press Ctrl+C to stop")
    print()

    try:
        triggered_flights = set()  # Track which flights we've already triggered for

        while True:
            # Check for transits
            try:
                result = get_transits(
                    latitude, longitude, elevation, target, test_mode=False
                )

                # Filter for high-probability transits
                high_prob_transits = [
                    f
                    for f in result.get("flights", [])
                    if f.get("possibility_level") in ["MEDIUM", "HIGH"]
                ]

                if high_prob_transits:
                    timestamp = datetime.now().strftime('%H:%M:%S')
                    print(f"[{timestamp}] Found {len(high_prob_transits)} high-probability transits")

                    for flight in high_prob_transits:
                        flight_id = flight.get("id")
                        eta = flight.get("eta_seconds", 999999)
                        prob = flight.get("possibility_level")

                        # Only trigger if we haven't already and ETA is reasonable
                        if flight_id not in triggered_flights and 0 < eta < 300:  # Within 5 minutes
                            origin = flight.get("origin", "???")
                            dest = flight.get("destination", "???")

                            print(
                                f"  🎯 {flight_id} ({origin}→{dest}): "
                                f"ETA {eta:.0f}s - {prob} probability"
                            )

                            # Schedule recording
                            if recorder.schedule_transit_recording(flight_id, eta):
                                triggered_flights.add(flight_id)
                                print(f"     ✓ Recording scheduled")
                            else:
                                print(f"     ✗ Failed to schedule recording")

            except Exception as e:
                print(f"ERROR checking transits: {e}")

            # Wait before next check
            time.sleep(check_interval_seconds)

    except KeyboardInterrupt:
        print("\n\nShutting down...")

    finally:
        # Cleanup
        print("Cancelling scheduled recordings...")
        recorder.cancel_all()

        if client.is_recording():
            print("Stopping active recording...")
            client.stop_recording()

        print("Disconnecting from telescope...")
        client.disconnect()
        print("Done.")


def test_connection():
    """Test basic Seestar connection and commands."""
    print("Seestar Connection Test")
    print("=" * 60)

    client = create_client_from_env()
    if client is None:
        print("ERROR: Seestar not enabled. Set ENABLE_SEESTAR=true in .env")
        return

    print(f"Testing connection to {client.host}:{client.port}...")
    print()

    try:
        # Connect
        print("1. Connecting...")
        client.connect()
        print("   ✓ Connected")

        # Check status
        print("\n2. Checking status...")
        status = client.get_status()
        print(f"   ✓ Status: {status}")

        # Test recording (will show warning about unimplemented commands)
        print("\n3. Testing recording commands...")
        print("   NOTE: Video recording commands need hardware testing")
        client.start_recording(duration_seconds=5)
        time.sleep(1)
        print(f"   Recording state: {client.is_recording()}")

        client.stop_recording()
        print(f"   Recording state: {client.is_recording()}")

        # Disconnect
        print("\n4. Disconnecting...")
        client.disconnect()
        print("   ✓ Disconnected")

        print("\n" + "=" * 60)
        print("✓ Connection test complete!")
        print()
        print("NEXT STEPS:")
        print("1. Check Seestar logs for any errors")
        print("2. Use Wireshark to capture video recording commands")
        print("3. Update start_recording() in src/seestar_client.py")
        print("4. Test with actual transit predictions")

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        print("\nTroubleshooting:")
        print("- Verify Seestar IP address is correct")
        print("- Check Seestar is on same network")
        print("- Try different port numbers (4700, 4720, etc.)")
        print("- Enable debug logging in src/seestar_client.py")


def discover_commands():
    """
    Guide for discovering Seestar video recording commands.

    This function doesn't actually discover commands - it provides
    instructions for how to do so using network monitoring tools.
    """
    print("Seestar Video Recording Command Discovery Guide")
    print("=" * 60)
    print()
    print("The video recording JSON-RPC methods need to be discovered through")
    print("network monitoring since they're not publicly documented.")
    print()
    print("METHOD 1: Network Traffic Capture")
    print("-" * 40)
    print("1. Install Wireshark: brew install wireshark")
    print("2. Start capture on WiFi interface")
    print("3. Filter: tcp.port == 4700 or tcp.port == 4720")
    print("4. Use Seestar app to start video recording")
    print("5. Look for JSON-RPC messages like:")
    print('   {"jsonrpc":"2.0","method":"???","id":1}')
    print("6. Update start_recording() in src/seestar_client.py")
    print()
    print("METHOD 2: Examine seestar_alp Source")
    print("-" * 40)
    print("1. Clone: git clone https://github.com/smart-underworld/seestar_alp")
    print("2. Search for video/record methods:")
    print("   grep -r 'start.*record\\|video.*start' .")
    print("3. Look in device/seestar_device.py")
    print("4. Check Bruno API collection")
    print()
    print("EXPECTED METHOD NAMES (to try):")
    print("-" * 40)
    print("- iscope_start_record")
    print("- iscope_stop_record")
    print("- video_start_recording")
    print("- video_stop_recording")
    print("- start_video_capture")
    print()
    print("Once discovered, update the TODO sections in:")
    print("  src/seestar_client.py:start_recording()")
    print("  src/seestar_client.py:stop_recording()")
    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Automated Seestar telescope triggering for transit capture"
    )
    parser.add_argument("--test", action="store_true", help="Test Seestar connection only")
    parser.add_argument("--discover", action="store_true", help="Show command discovery guide")
    parser.add_argument("--latitude", type=float, help="Observer latitude")
    parser.add_argument("--longitude", type=float, help="Observer longitude")
    parser.add_argument("--elevation", type=float, default=0, help="Observer elevation (m)")
    parser.add_argument(
        "--target",
        choices=["moon", "sun", "auto"],
        default="moon",
        help="Target celestial body",
    )
    parser.add_argument(
        "--interval", type=int, default=60, help="Check interval in seconds (default: 60)"
    )

    args = parser.parse_args()

    if args.test:
        test_connection()
    elif args.discover:
        discover_commands()
    else:
        if args.latitude is None or args.longitude is None:
            print("ERROR: --latitude and --longitude are required")
            print()
            parser.print_help()
        else:
            monitor_and_trigger(
                args.latitude,
                args.longitude,
                args.elevation,
                args.target,
                args.interval,
            )
