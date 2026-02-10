#!/usr/bin/env python3
"""
Windows System Tray Transit Monitor

A Windows-specific system tray application for monitoring aircraft transits.
Provides a convenient GUI interface without needing to run the full web app.

Features:
- System tray icon with status indicator
- Right-click menu for target selection and controls
- Background monitoring with Telegram notifications
- Start/stop monitoring from the tray

Requirements:
    pip install pystray pillow

Usage:
    python windows_monitor.py

The monitor will appear in your system tray. Right-click to access controls.
"""

import os
import sys
import threading
import time
import asyncio
from datetime import datetime, timedelta
from typing import Optional

# Check for required packages
try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("ERROR: Required packages not installed for Windows.")
    print("Install with: pip install pystray pillow")
    sys.exit(1)

from dotenv import load_dotenv

# Load environment
load_dotenv()

from src import logger
from src.constants import PossibilityLevel
from src.transit import get_transits

# Optional Telegram support
try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning("Telegram not available. Install with: pip install python-telegram-bot")


class WindowsTransitMonitor:
    """
    Windows system tray application for transit monitoring.
    """

    def __init__(self):
        # Configuration from environment
        self.latitude = float(os.getenv("OBSERVER_LATITUDE", "0"))
        self.longitude = float(os.getenv("OBSERVER_LONGITUDE", "0"))
        self.elevation = float(os.getenv("OBSERVER_ELEVATION", "0"))
        self.target = os.getenv("MONITOR_TARGET", "sun")
        self.check_interval = int(os.getenv("MONITOR_INTERVAL", "10"))
        
        # State
        self.monitoring = False
        self.stop_monitoring_flag = threading.Event()
        self.last_check_time = None
        self.last_transit_count = 0
        
        # Telegram bot (optional)
        self.telegram_bot = None
        self.telegram_chat_id = None
        self._init_telegram()
        
        # Tracking
        self.notified_transits = set()
        
        # Create system tray icon
        self.icon = None
        
    def _init_telegram(self):
        """Initialize Telegram bot if configured."""
        if not TELEGRAM_AVAILABLE:
            return
            
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        if bot_token and chat_id:
            try:
                self.telegram_bot = Bot(token=bot_token)
                self.telegram_chat_id = chat_id
                logger.info("Telegram notifications enabled")
            except Exception as e:
                logger.error(f"Failed to initialize Telegram: {e}")
    
    def _create_icon_image(self, color: str = "gray") -> Image.Image:
        """
        Create a simple icon image for the system tray.
        
        Args:
            color: Icon color - "gray" (idle), "green" (monitoring), "orange" (transit detected)
        """
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        
        # Color mapping
        colors = {
            "gray": (128, 128, 128),
            "green": (0, 200, 0),
            "orange": (255, 165, 0),
            "red": (255, 0, 0),
        }
        fill_color = colors.get(color, colors["gray"])
        
        # Draw a moon/sun shape
        margin = 8
        draw.ellipse(
            [margin, margin, size - margin, size - margin],
            fill=fill_color,
            outline=(255, 255, 255),
            width=2
        )
        
        # Add airplane symbol if monitoring
        if self.monitoring:
            # Simple airplane shape
            center = size // 2
            draw.polygon([
                (center, margin + 4),
                (center - 8, center),
                (center, center - 4),
                (center + 8, center),
            ], fill=(255, 255, 255))
        
        return image
    
    def _update_icon(self, color: str = None):
        """Update the tray icon."""
        if self.icon:
            if color is None:
                color = "green" if self.monitoring else "gray"
            self.icon.icon = self._create_icon_image(color)
    
    def _create_menu(self):
        """Create the right-click menu."""
        return pystray.Menu(
            pystray.MenuItem(
                "Flymoon Transit Monitor",
                None,
                enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: f"Target: {self.target.upper()}",
                None,
                enabled=False
            ),
            pystray.MenuItem(
                "Set Target",
                pystray.Menu(
                    pystray.MenuItem("Sun", lambda: self._set_target("sun")),
                    pystray.MenuItem("Moon", lambda: self._set_target("moon")),
                    pystray.MenuItem("Auto", lambda: self._set_target("auto")),
                )
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: "⏹ Stop Monitoring" if self.monitoring else "▶ Start Monitoring",
                self._toggle_monitoring
            ),
            pystray.MenuItem(
                lambda item: f"Last check: {self.last_check_time.strftime('%H:%M:%S') if self.last_check_time else 'Never'}",
                None,
                enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: f"Location: {self.latitude:.4f}, {self.longitude:.4f}",
                None,
                enabled=False
            ),
            pystray.MenuItem(
                lambda item: f"Interval: {self.check_interval} min",
                None,
                enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit)
        )
    
    def _set_target(self, target: str):
        """Change the monitoring target."""
        self.target = target
        logger.info(f"Target changed to: {target}")
        self._send_notification(
            "🎯 Target Changed",
            f"Now monitoring: {target.upper()}"
        )
    
    def _toggle_monitoring(self):
        """Start or stop monitoring."""
        if self.monitoring:
            self._stop_monitoring()
        else:
            self._start_monitoring()
    
    def _start_monitoring(self):
        """Start the monitoring thread."""
        if self.monitoring:
            return
            
        self.monitoring = True
        self.stop_monitoring_flag.clear()
        self._update_icon("green")
        
        # Start monitoring thread
        thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        thread.start()
        
        logger.info("Monitoring started")
        self._send_notification(
            "🛰️ Monitoring Started",
            f"Target: {self.target.upper()}\n"
            f"Interval: {self.check_interval} min\n"
            f"Location: {self.latitude:.4f}, {self.longitude:.4f}"
        )
    
    def _stop_monitoring(self):
        """Stop the monitoring thread."""
        if not self.monitoring:
            return
            
        self.monitoring = False
        self.stop_monitoring_flag.set()
        self._update_icon("gray")
        
        logger.info("Monitoring stopped")
        self._send_notification(
            "🛑 Monitoring Stopped",
            "Transit monitoring has been stopped"
        )
    
    def _monitoring_loop(self):
        """Main monitoring loop (runs in background thread)."""
        while not self.stop_monitoring_flag.is_set():
            try:
                self._check_transits()
                self.last_check_time = datetime.now()
            except Exception as e:
                logger.error(f"Error checking transits: {e}")
            
            # Wait for next check interval
            for _ in range(self.check_interval * 60):
                if self.stop_monitoring_flag.is_set():
                    break
                time.sleep(1)
    
    def _check_transits(self):
        """Check for transits and send notifications."""
        logger.info(f"Checking for {self.target} transits...")
        
        try:
            # Get transits
            result = get_transits(
                latitude=self.latitude,
                longitude=self.longitude,
                elevation=self.elevation,
                target_name=self.target,
                test_mode=False
            )
            
            flights = result.get("flights", [])
            
            # Filter for HIGH probability transits
            high_transits = [
                f for f in flights
                if f.get("is_possible_transit") == 1
                and f.get("possibility_level") == PossibilityLevel.HIGH.value
            ]
            
            self.last_transit_count = len(high_transits)
            
            if high_transits:
                self._update_icon("orange")
                self._process_transits(high_transits)
            else:
                self._update_icon("green")
                logger.info("No high-probability transits found")
                
        except Exception as e:
            logger.error(f"Error fetching transits: {e}")
            self._update_icon("red")
    
    def _process_transits(self, transits: list):
        """Process and notify about detected transits."""
        for transit in transits:
            transit_id = f"{transit['id']}_{transit.get('time', 0):.1f}"
            
            if transit_id in self.notified_transits:
                continue
                
            self.notified_transits.add(transit_id)
            
            time_minutes = transit.get("time", 0)
            
            title = "🟢 HIGH Probability Transit!"
            body = (
                f"Flight: {transit['id']}\n"
                f"Route: {transit.get('origin', '?')} → {transit.get('destination', '?')}\n"
                f"ETA: {time_minutes:.1f} minutes\n"
                f"Alt diff: {transit.get('alt_diff', 0):.2f}°\n"
                f"Az diff: {transit.get('az_diff', 0):.2f}°"
            )
            
            self._send_notification(title, body)
            logger.info(f"Transit detected: {transit['id']} in {time_minutes:.1f} min")
    
    def _send_notification(self, title: str, body: str):
        """Send notification via Telegram."""
        if self.telegram_bot and self.telegram_chat_id:
            try:
                message = f"*{title}*\n\n{body}"
                asyncio.run(
                    self.telegram_bot.send_message(
                        chat_id=self.telegram_chat_id,
                        text=message,
                        parse_mode="Markdown"
                    )
                )
            except Exception as e:
                logger.error(f"Failed to send Telegram notification: {e}")
        
        # Also log
        logger.info(f"Notification: {title}")
    
    def _quit(self):
        """Quit the application."""
        if self.monitoring:
            self.stop_monitoring_flag.set()
        if self.icon:
            self.icon.stop()
    
    def run(self):
        """Run the system tray application."""
        # Validate configuration
        if self.latitude == 0 and self.longitude == 0:
            print("ERROR: Observer location not configured.")
            print("Set OBSERVER_LATITUDE and OBSERVER_LONGITUDE in .env")
            sys.exit(1)
        
        logger.info("=" * 60)
        logger.info("Windows Transit Monitor Starting")
        logger.info("=" * 60)
        logger.info(f"Location: {self.latitude}, {self.longitude}")
        logger.info(f"Target: {self.target}")
        logger.info(f"Check interval: {self.check_interval} minutes")
        logger.info(f"Telegram: {'Enabled' if self.telegram_bot else 'Disabled'}")
        logger.info("=" * 60)
        
        # Create and run system tray icon
        self.icon = pystray.Icon(
            "flymoon",
            self._create_icon_image(),
            "Flymoon Transit Monitor",
            self._create_menu()
        )
        
        # Auto-start monitoring
        self._start_monitoring()
        
        # Run the icon (blocks until quit)
        self.icon.run()


def main():
    """Main entry point."""
    monitor = WindowsTransitMonitor()
    monitor.run()


if __name__ == "__main__":
    main()
