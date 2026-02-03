#!/usr/bin/env python3
"""
Transit Monitor - Automated notification system for high-probability transits.

This script continuously monitors for aircraft transits and sends notifications
when high-probability transits are detected. Since direct Seestar control has
compatibility issues with firmware 6.70, this provides advance warning for
manual recording via the Seestar app.

Features:
- Polls FlightAware API for upcoming transits
- Filters for HIGH probability (green) transits only
- Sends advance warning notifications (configurable lead time)
- Sends countdown notifications as transit approaches
- Includes precise timing for manual recording

Usage:
    python3 monitor_transits.py --latitude 33.111369 --longitude -117.310169 --target sun

Environment Variables:
    MONITOR_INTERVAL - Check interval in minutes (default: from .env or 15)
    TRANSIT_WARNING_MINUTES - How many minutes before transit to warn (default: 5)
    PUSH_BULLET_API_KEY - Required for notifications
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
from pushbullet import Pushbullet


class TransitMonitor:
    """Monitor for high-probability transits and send notifications."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        elevation: float,
        target: str,
        check_interval_minutes: int = 15,
        warning_minutes: int = 5,
    ):
        """
        Initialize transit monitor.

        Parameters
        ----------
        latitude : float
            Observer latitude
        longitude : float
            Observer longitude
        elevation : float
            Observer elevation in meters
        target : str
            Target celestial body ('sun', 'moon', or 'auto')
        check_interval_minutes : int
            How often to check for transits (default: 15)
        warning_minutes : int
            How many minutes before transit to send warning (default: 5)
        """
        self.latitude = latitude
        self.longitude = longitude
        self.elevation = elevation
        self.target = target
        self.check_interval = check_interval_minutes
        self.warning_minutes = warning_minutes

        # PushBullet setup
        api_key = os.getenv("PUSH_BULLET_API_KEY")
        if not api_key:
            raise ValueError(
                "PUSH_BULLET_API_KEY not set in .env - notifications required"
            )
        self.pb = Pushbullet(api_key)

        # Track notified transits to avoid duplicates
        self.notified_transits = set()
        self.warned_transits = set()

        logger.info(
            f"Transit Monitor initialized: {target} at ({latitude}, {longitude})"
        )
        logger.info(f"Check interval: {check_interval_minutes} min")
        logger.info(f"Warning lead time: {warning_minutes} min")

    async def get_high_probability_transits(self) -> List[dict]:
        """
        Get upcoming HIGH probability transits.

        Returns
        -------
        list
            List of HIGH probability transit data
        """
        try:
            all_transits = await get_transits(
                self.latitude, self.longitude, self.elevation, self.target
            )

            # Filter for HIGH probability only
            high_prob = [
                t
                for t in all_transits
                if t.get("possibility_level") == PossibilityLevel.HIGH.value
            ]

            logger.info(
                f"Found {len(high_prob)} HIGH probability transits out of {len(all_transits)} total"
            )
            return high_prob

        except Exception as e:
            logger.error(f"Error getting transits: {e}")
            return []

    def send_notification(
        self, title: str, body: str, url: Optional[str] = None
    ) -> None:
        """Send push notification via PushBullet."""
        try:
            if url:
                self.pb.push_link(title, url, body)
            else:
                self.pb.push_note(title, body)
            logger.info(f"Notification sent: {title}")
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    def format_transit_time(self, minutes: float) -> str:
        """Format transit time in human-readable form."""
        if minutes < 1:
            seconds = int(minutes * 60)
            return f"{seconds} seconds"
        elif minutes < 60:
            return f"{int(minutes)} minutes"
        else:
            hours = minutes / 60
            return f"{hours:.1f} hours"

    def get_transit_id(self, transit: dict) -> str:
        """Get unique identifier for transit."""
        return f"{transit['id']}_{transit['time']}"

    async def check_and_notify(self) -> None:
        """Check for transits and send appropriate notifications."""
        transits = await self.get_high_probability_transits()

        if not transits:
            logger.debug("No HIGH probability transits found")
            return

        now = datetime.now()

        for transit in transits:
            transit_id = self.get_transit_id(transit)
            time_minutes = transit["time"]

            # Skip if already processed
            if transit_id in self.notified_transits:
                continue

            # Initial notification for newly discovered high-probability transit
            if transit_id not in self.warned_transits:
                self.warned_transits.add(transit_id)

                title = f"🟢 HIGH Probability Transit Detected!"
                body = (
                    f"Flight: {transit['id']}\n"
                    f"Route: {transit['origin']} → {transit['destination']}\n"
                    f"Time: {self.format_transit_time(time_minutes)}\n"
                    f"Altitude Diff: {transit['alt_diff']:.2f}°\n"
                    f"Azimuth Diff: {transit['az_diff']:.2f}°\n"
                    f"\n"
                    f"⏰ You will receive a warning {self.warning_minutes} min before transit."
                )

                self.send_notification(title, body)

            # Urgent warning when transit is imminent
            if time_minutes <= self.warning_minutes:
                self.notified_transits.add(transit_id)

                # Calculate exact timing
                transit_time = now + timedelta(minutes=time_minutes)
                pre_buffer = int(os.getenv("SEESTAR_PRE_BUFFER", "10"))
                post_buffer = int(os.getenv("SEESTAR_POST_BUFFER", "10"))

                start_recording_at = transit_time - timedelta(seconds=pre_buffer)
                stop_recording_at = transit_time + timedelta(seconds=post_buffer)

                title = f"🚨 TRANSIT IMMINENT - {self.format_transit_time(time_minutes)}"
                body = (
                    f"Flight: {transit['id']}\n"
                    f"Route: {transit['origin']} → {transit['destination']}\n"
                    f"\n"
                    f"⏰ TIMING:\n"
                    f"Transit at: {transit_time.strftime('%H:%M:%S')}\n"
                    f"Start recording: {start_recording_at.strftime('%H:%M:%S')}\n"
                    f"Stop recording: {stop_recording_at.strftime('%H:%M:%S')}\n"
                    f"\n"
                    f"📱 ACTION REQUIRED:\n"
                    f"1. Open Seestar app NOW\n"
                    f"2. Confirm {self.target} is centered\n"
                    f"3. Be ready to press RECORD at {start_recording_at.strftime('%H:%M:%S')}\n"
                    f"4. Stop recording at {stop_recording_at.strftime('%H:%M:%S')}\n"
                    f"\n"
                    f"Recording duration: {pre_buffer + post_buffer} seconds"
                )

                self.send_notification(
                    title, body, url=f"http://localhost:5000/"  # Link to live view
                )

                logger.info(
                    f"URGENT: Transit {transit['id']} in {time_minutes:.1f} minutes!"
                )

    async def run(self) -> None:
        """Main monitoring loop."""
        logger.info("=" * 60)
        logger.info("Transit Monitor Started")
        logger.info("=" * 60)
        logger.info(f"Target: {self.target}")
        logger.info(f"Location: {self.latitude}, {self.longitude}")
        logger.info(f"Check interval: {self.check_interval} minutes")
        logger.info(f"Warning time: {self.warning_minutes} minutes before transit")
        logger.info("=" * 60)

        # Send startup notification
        self.send_notification(
            "🛰️ Transit Monitor Started",
            f"Monitoring for HIGH probability {self.target} transits\n"
            f"Location: {self.latitude}, {self.longitude}\n"
            f"Check interval: {self.check_interval} min\n"
            f"Warning lead: {self.warning_minutes} min",
        )

        try:
            while True:
                logger.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking for transits...")
                await self.check_and_notify()

                # Wait for next check
                logger.info(
                    f"Next check in {self.check_interval} minutes at "
                    f"{(datetime.now() + timedelta(minutes=self.check_interval)).strftime('%H:%M:%S')}"
                )
                await asyncio.sleep(self.check_interval * 60)

        except KeyboardInterrupt:
            logger.info("\n" + "=" * 60)
            logger.info("Transit Monitor Stopped by user")
            logger.info("=" * 60)
            self.send_notification(
                "🛑 Transit Monitor Stopped",
                "Monitoring has been stopped by user",
            )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Monitor for high-probability aircraft transits and send notifications"
    )

    parser.add_argument(
        "--latitude",
        type=float,
        required=True,
        help="Observer latitude in decimal degrees",
    )
    parser.add_argument(
        "--longitude",
        type=float,
        required=True,
        help="Observer longitude in decimal degrees",
    )
    parser.add_argument(
        "--elevation",
        type=float,
        default=0,
        help="Observer elevation in meters (default: 0)",
    )
    parser.add_argument(
        "--target",
        choices=["sun", "moon", "auto"],
        default="sun",
        help="Target celestial body (default: sun)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=f"Check interval in minutes (default: from .env MONITOR_INTERVAL or 15)",
    )
    parser.add_argument(
        "--warning",
        type=int,
        default=5,
        help="Minutes before transit to send warning (default: 5)",
    )

    args = parser.parse_args()

    # Get interval from args, env, or default
    interval = args.interval
    if interval is None:
        interval = int(os.getenv("MONITOR_INTERVAL", "15"))

    # Validate
    if interval < 1:
        print("ERROR: Check interval must be at least 1 minute")
        sys.exit(1)

    if args.warning < 1:
        print("ERROR: Warning time must be at least 1 minute")
        sys.exit(1)

    # Check for PushBullet API key
    if not os.getenv("PUSH_BULLET_API_KEY"):
        print("ERROR: PUSH_BULLET_API_KEY not set in .env file")
        print("Notifications are required for this monitor to work.")
        sys.exit(1)

    # Create and run monitor
    monitor = TransitMonitor(
        latitude=args.latitude,
        longitude=args.longitude,
        elevation=args.elevation,
        target=args.target,
        check_interval_minutes=interval,
        warning_minutes=args.warning,
    )

    # Run async loop
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
