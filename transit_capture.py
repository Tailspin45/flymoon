#!/usr/bin/env python3
"""
Automated Transit Capture System with Manual Fallback

This is the production transit capture system that will automatically
trigger Seestar video recording when high-probability transits are detected.

Current Status (Firmware 6.70):
- Automatic control: JSON-RPC timeouts (firmware issue)
- Fallback mode: Push notifications for manual recording
- Future: Will use automatic mode when firmware is fixed

The system attempts automatic control first, then gracefully falls back
to manual notifications if the Seestar isn't responding.

Usage:
    # Try automatic first, fallback to manual
    python3 transit_capture.py --latitude YOUR_LAT --longitude YOUR_LON --target sun

    # Force manual mode
    python3 transit_capture.py --latitude YOUR_LAT --longitude YOUR_LON --target sun --manual

    # Test Seestar connection
    python3 transit_capture.py --test-seestar
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional
from dotenv import load_dotenv

# Load environment first
load_dotenv()

from src import logger
from src.constants import PossibilityLevel
from src.transit import get_transits
from src.seestar_client import create_client_from_env
from telegram import Bot
from telegram.error import TelegramError


class TransitCaptureMode:
    """Enum for capture modes."""
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class TransitCaptureSystem:
    """
    Automated transit capture with graceful fallback to manual mode.

    Attempts to use direct Seestar control for fully automated capture.
    If Seestar doesn't respond (firmware issue), falls back to push
    notifications for manual recording.
    """

    def __init__(
        self,
        latitude: float,
        longitude: float,
        elevation: float,
        target: str,
        check_interval_minutes: int = 15,
        warning_minutes: int = 5,
        force_manual: bool = False,
    ):
        self.latitude = latitude
        self.longitude = longitude
        self.elevation = elevation
        self.target = target
        self.check_interval = check_interval_minutes
        self.warning_minutes = warning_minutes
        self.force_manual = force_manual

        # Capture mode (will be determined on startup)
        self.mode = None
        self.seestar_client = None
        self.telegram_bot = None
        self.telegram_chat_id = None

        # Transit tracking
        self.notified_transits = set()
        self.warned_transits = set()
        self.scheduled_recordings = {}

        logger.info("Transit Capture System initialized")

    def _test_seestar_connection(self) -> bool:
        """
        Test if Seestar is responsive.

        Returns True if Seestar responds to commands, False otherwise.
        """
        if not os.getenv("ENABLE_SEESTAR", "false").lower() == "true":
            logger.info("ENABLE_SEESTAR=false, skipping automatic mode")
            return False

        logger.info("Testing Seestar connection...")

        try:
            from src.seestar_client import SeestarClient

            host = os.getenv("SEESTAR_HOST")
            if not host:
                logger.warning("SEESTAR_HOST not configured")
                return False

            # Create test client with short timeout
            test_client = SeestarClient(
                host=host,
                port=int(os.getenv("SEESTAR_PORT", "4700")),
                timeout=5,  # Short timeout for testing
                heartbeat_interval=0  # No heartbeat for test
            )

            # Try to connect
            if not test_client.connect():
                logger.warning("Seestar connection failed")
                test_client.disconnect()
                return False

            # Stop heartbeat
            test_client._heartbeat_running = False

            # Try a simple command
            logger.info("Testing command response...")
            try:
                test_client.list_files()
                logger.info("✓ Seestar responding to commands!")
                test_client.disconnect()
                return True

            except Exception as e:
                logger.warning(f"Seestar not responding to commands: {e}")
                test_client.disconnect()
                return False

        except Exception as e:
            logger.error(f"Error testing Seestar: {e}")
            return False

    def _initialize_automatic_mode(self) -> bool:
        """Initialize automatic Seestar control mode."""
        logger.info("=" * 60)
        logger.info("INITIALIZING AUTOMATIC MODE")
        logger.info("=" * 60)

        try:
            self.seestar_client = create_client_from_env()
            if not self.seestar_client:
                logger.warning("Could not create Seestar client from environment")
                return False

            if not self.seestar_client.connect():
                logger.warning("Failed to connect to Seestar")
                return False

            logger.info(f"✓ Connected to Seestar at {self.seestar_client.host}")

            # Put Seestar in appropriate viewing mode
            logger.info(f"Setting {self.target} viewing mode...")
            if self.target == "sun":
                self.seestar_client.start_solar_mode()
            elif self.target == "moon":
                self.seestar_client.start_lunar_mode()

            logger.info("✓ Automatic mode ready")
            logger.info("  Seestar will record transits automatically")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize automatic mode: {e}")
            if self.seestar_client:
                self.seestar_client.disconnect()
            return False

    def _initialize_manual_mode(self) -> bool:
        """Initialize manual notification mode."""
        logger.info("=" * 60)
        logger.info("INITIALIZING MANUAL MODE")
        logger.info("=" * 60)

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not bot_token:
            logger.error("TELEGRAM_BOT_TOKEN required for manual mode")
            return False

        if not chat_id:
            logger.error("TELEGRAM_CHAT_ID required for manual mode")
            return False

        try:
            self.telegram_bot = Bot(token=bot_token)
            self.telegram_chat_id = chat_id
            logger.info("✓ Telegram Bot connected")
            logger.info("  You will receive notifications to manually record")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Telegram Bot: {e}")
            return False

    async def initialize(self) -> bool:
        """
        Initialize the capture system.

        Attempts automatic mode first, falls back to manual if needed.
        """
        logger.info("\n" + "=" * 60)
        logger.info("TRANSIT CAPTURE SYSTEM - STARTUP")
        logger.info("=" * 60)
        logger.info(f"Target: {self.target}")
        logger.info(f"Location: {self.latitude}, {self.longitude}")
        logger.info(f"Check interval: {self.check_interval} min")
        logger.info("=" * 60)

        # Check if forced to manual mode
        if self.force_manual:
            logger.info("Manual mode forced by --manual flag")
            self.mode = TransitCaptureMode.MANUAL
            return self._initialize_manual_mode()

        # Try automatic mode first
        logger.info("Attempting automatic mode (preferred)...")

        if self._test_seestar_connection():
            if self._initialize_automatic_mode():
                self.mode = TransitCaptureMode.AUTOMATIC
                logger.info("\n✓✓✓ AUTOMATIC MODE ACTIVE ✓✓✓")
                return True

        # Fall back to manual mode
        logger.warning("\nAutomatic mode unavailable (Seestar not responding)")
        logger.info("Falling back to manual notification mode...")

        if self._initialize_manual_mode():
            self.mode = TransitCaptureMode.MANUAL
            logger.info("\n⚠️  MANUAL MODE ACTIVE (FALLBACK)")
            logger.info("    Reason: Seestar firmware 6.70 JSON-RPC timeout issue")
            logger.info("    You will receive notifications to manually record")

            # Send notification about fallback mode
            if self.telegram_bot:
                asyncio.create_task(self._send_telegram_message(
                    "⚠️ *Transit Monitor - Manual Mode*\n\n"
                    f"Automatic Seestar control unavailable\\.\n"
                    f"Monitoring {self.target} transits at \\({self.latitude}, {self.longitude}\\)\n"
                    f"You will receive notifications to manually start/stop recording\\.\n\n"
                    f"When automatic mode is available, the system will record automatically\\."
                ))

            return True

        logger.error("\nFailed to initialize any capture mode")
        return False

    async def get_high_probability_transits(self) -> List[dict]:
        """Get upcoming HIGH probability transits."""
        try:
            transit_data = get_transits(
                self.latitude, self.longitude, self.elevation, self.target
            )

            all_transits = transit_data.get("flights", [])

            high_prob = [
                t for t in all_transits
                if t.get("possibility_level") == PossibilityLevel.HIGH.value
            ]

            logger.info(
                f"Found {len(high_prob)} HIGH probability transits "
                f"(out of {len(all_transits)} total)"
            )
            return high_prob

        except Exception as e:
            logger.error(f"Error getting transits: {e}")
            return []

    def _handle_automatic_capture(self, transit: dict) -> None:
        """Handle transit with automatic Seestar recording."""
        transit_id = f"{transit['id']}_{transit['time']}"

        if transit_id in self.scheduled_recordings:
            return  # Already scheduled

        time_minutes = transit["time"]

        # Calculate timing
        now = datetime.now()
        transit_time = now + timedelta(minutes=time_minutes)
        pre_buffer = int(os.getenv("SEESTAR_PRE_BUFFER", "10"))
        post_buffer = int(os.getenv("SEESTAR_POST_BUFFER", "10"))

        start_time = transit_time - timedelta(seconds=pre_buffer)
        stop_time = transit_time + timedelta(seconds=post_buffer)
        duration = (stop_time - start_time).total_seconds()

        # Schedule recording
        delay = (start_time - now).total_seconds()

        if delay > 0:
            logger.info(f"⏰ Scheduling automatic recording for {transit['id']}")
            logger.info(f"   Start: {start_time.strftime('%H:%M:%S')}")
            logger.info(f"   Duration: {duration:.0f}s")

            self.scheduled_recordings[transit_id] = {
                "transit": transit,
                "start_time": start_time,
                "duration": duration,
            }

            # Schedule the recording
            asyncio.create_task(
                self._execute_automatic_recording(transit_id, delay, duration)
            )
        else:
            logger.warning(f"Transit {transit['id']} too soon to schedule (in {delay:.0f}s)")

    async def _execute_automatic_recording(
        self, transit_id: str, delay: float, duration: float
    ) -> None:
        """Execute automatic recording at scheduled time."""
        try:
            # Wait until start time
            logger.info(f"Waiting {delay:.0f}s until recording starts...")
            await asyncio.sleep(delay)

            # Start recording
            logger.info(f"🎥 STARTING AUTOMATIC RECORDING")
            self.seestar_client.start_recording()

            # Wait for duration
            await asyncio.sleep(duration)

            # Stop recording
            logger.info(f"⏹️  STOPPING AUTOMATIC RECORDING")
            self.seestar_client.stop_recording()

            logger.info(f"✓ Automatic capture complete for {transit_id}")

        except Exception as e:
            logger.error(f"Error in automatic recording: {e}")

    async def _send_telegram_message(self, message: str) -> bool:
        """Send a Telegram message."""
        try:
            await self.telegram_bot.send_message(
                chat_id=self.telegram_chat_id,
                text=message,
                parse_mode='MarkdownV2'
            )
            return True
        except TelegramError as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def _handle_manual_capture(self, transit: dict) -> None:
        """Handle transit with manual notification."""
        transit_id = f"{transit['id']}_{transit['time']}"
        time_minutes = transit["time"]

        # Initial detection notification
        if transit_id not in self.warned_transits:
            self.warned_transits.add(transit_id)

            message = (
                f"🟢 *HIGH Probability Transit Detected\\!*\n\n"
                f"*Flight:* {transit['id']}\n"
                f"*Route:* {transit['origin']} → {transit['destination']}\n"
                f"*Time:* {self._format_time(time_minutes)}\n"
                f"*Alt diff:* {transit['alt_diff']:.2f}° \\| *Az diff:* {transit['az_diff']:.2f}°\n\n"
                f"⏰ You will receive a warning {self.warning_minutes} min before transit\\."
            )

            asyncio.create_task(self._send_telegram_message(message))
            logger.info(f"📱 Sent detection notification for {transit['id']}")

        # Imminent warning
        if time_minutes <= self.warning_minutes and transit_id not in self.notified_transits:
            self.notified_transits.add(transit_id)

            now = datetime.now()
            transit_time = now + timedelta(minutes=time_minutes)
            pre_buffer = int(os.getenv("SEESTAR_PRE_BUFFER", "10"))
            post_buffer = int(os.getenv("SEESTAR_POST_BUFFER", "10"))

            start_time = transit_time - timedelta(seconds=pre_buffer)
            stop_time = transit_time + timedelta(seconds=post_buffer)

            message = (
                f"🚨 *TRANSIT IMMINENT \\- {self._format_time(time_minutes)}*\n\n"
                f"*Flight:* {transit['id']}\n"
                f"*Route:* {transit['origin']} → {transit['destination']}\n\n"
                f"⏰ *TIMING:*\n"
                f"Transit at: `{transit_time.strftime('%H:%M:%S')}`\n"
                f"Start recording: `{start_time.strftime('%H:%M:%S')}`\n"
                f"Stop recording: `{stop_time.strftime('%H:%M:%S')}`\n\n"
                f"📱 *ACTION REQUIRED:*\n"
                f"1\\. Open Seestar app NOW\n"
                f"2\\. Confirm {self.target} is centered\n"
                f"3\\. Press RECORD at `{start_time.strftime('%H:%M:%S')}`\n"
                f"4\\. Press STOP at `{stop_time.strftime('%H:%M:%S')}`\n\n"
                f"*Duration:* {pre_buffer + post_buffer}s"
            )

            asyncio.create_task(self._send_telegram_message(message))
            logger.info(f"🚨 Sent IMMINENT notification for {transit['id']}")

    def _format_time(self, minutes: float) -> str:
        """Format time in human-readable form."""
        if minutes < 1:
            return f"{int(minutes * 60)} seconds"
        elif minutes < 60:
            return f"{int(minutes)} minutes"
        else:
            return f"{minutes / 60:.1f} hours"

    async def check_and_capture(self) -> None:
        """Check for transits and handle capture (automatic or manual)."""
        transits = await self.get_high_probability_transits()

        if not transits:
            logger.debug("No HIGH probability transits found")
            return

        for transit in transits:
            if self.mode == TransitCaptureMode.AUTOMATIC:
                self._handle_automatic_capture(transit)
            else:  # MANUAL
                self._handle_manual_capture(transit)

    async def run(self) -> None:
        """Main monitoring loop."""
        if not await self.initialize():
            logger.error("Failed to initialize capture system")
            sys.exit(1)

        logger.info("\n" + "=" * 60)
        logger.info(f"MODE: {self.mode.upper()}")
        logger.info("=" * 60)

        if self.mode == TransitCaptureMode.AUTOMATIC:
            logger.info("✓ Transits will be recorded automatically")
            logger.info("✓ No user action required")
        else:
            logger.info("⚠️  You will receive notifications for manual recording")
            logger.info("⚠️  Keep Seestar app accessible")

        logger.info("=" * 60)
        logger.info("MONITORING STARTED\n")

        try:
            while True:
                timestamp = datetime.now().strftime('%H:%M:%S')
                logger.info(f"[{timestamp}] Checking for transits...")

                await self.check_and_capture()

                next_check = datetime.now() + timedelta(minutes=self.check_interval)
                logger.info(
                    f"Next check in {self.check_interval} min at "
                    f"{next_check.strftime('%H:%M:%S')}\n"
                )

                await asyncio.sleep(self.check_interval * 60)

        except KeyboardInterrupt:
            logger.info("\n" + "=" * 60)
            logger.info("TRANSIT CAPTURE STOPPED BY USER")
            logger.info("=" * 60)

            if self.seestar_client:
                self.seestar_client.disconnect()

            if self.telegram_bot:
                await self._send_telegram_message(
                    "🛑 *Transit Monitor Stopped*\n\n"
                    "Monitoring has been stopped by user\\."
                )

    def cleanup(self) -> None:
        """Cleanup resources."""
        if self.seestar_client:
            self.seestar_client.disconnect()


def test_seestar() -> None:
    """Test Seestar connectivity and responsiveness."""
    print("=" * 60)
    print("SEESTAR CONNECTION TEST")
    print("=" * 60)

    if not os.getenv("ENABLE_SEESTAR", "false").lower() == "true":
        print("✗ ENABLE_SEESTAR=false in .env")
        print("  Set ENABLE_SEESTAR=true to test")
        return

    host = os.getenv("SEESTAR_HOST")
    if not host:
        print("✗ SEESTAR_HOST not configured in .env")
        return

    print(f"Testing connection to {host}...")

    try:
        from src.seestar_client import SeestarClient

        client = SeestarClient(
            host=host,
            port=int(os.getenv("SEESTAR_PORT", "4700")),
            timeout=10,
            heartbeat_interval=0
        )

        print("\n1. Connecting...")
        if not client.connect():
            print("   ✗ Connection failed")
            return
        print("   ✓ Connected")

        client._heartbeat_running = False

        print("\n2. Testing commands...")
        try:
            client.list_files()
            print("   ✓ Seestar is responding!")
            print("\n✓✓✓ AUTOMATIC MODE AVAILABLE ✓✓✓")
            print("    Your Seestar will support automatic recording")
        except Exception as e:
            print(f"   ✗ Commands timeout: {e}")
            print("\n⚠️  MANUAL MODE REQUIRED")
            print("    Firmware 6.70 JSON-RPC issue detected")
            print("    System will fall back to notifications")

        client.disconnect()
        print("\n3. Disconnected")

    except Exception as e:
        print(f"\n✗ Error: {e}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Automated transit capture with manual fallback"
    )

    parser.add_argument("--latitude", type=float, help="Observer latitude")
    parser.add_argument("--longitude", type=float, help="Observer longitude")
    parser.add_argument("--elevation", type=float, default=0, help="Elevation (m)")
    parser.add_argument(
        "--target",
        choices=["sun", "moon", "auto"],
        default="sun",
        help="Target body"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Check interval (min, default: from .env or 15)"
    )
    parser.add_argument(
        "--warning",
        type=int,
        default=5,
        help="Warning lead time for manual mode (min)"
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Force manual mode (skip automatic)"
    )
    parser.add_argument(
        "--test-seestar",
        action="store_true",
        help="Test Seestar connection and exit"
    )

    args = parser.parse_args()

    # Handle test mode
    if args.test_seestar:
        test_seestar()
        sys.exit(0)

    # Require coordinates for monitoring
    if not args.latitude or not args.longitude:
        parser.error("--latitude and --longitude required (or use --test-seestar)")

    # Get interval
    interval = args.interval or int(os.getenv("MONITOR_INTERVAL", "15"))

    # Create and run system
    system = TransitCaptureSystem(
        latitude=args.latitude,
        longitude=args.longitude,
        elevation=args.elevation,
        target=args.target,
        check_interval_minutes=interval,
        warning_minutes=args.warning,
        force_manual=args.manual,
    )

    try:
        asyncio.run(system.run())
    finally:
        system.cleanup()


if __name__ == "__main__":
    main()
