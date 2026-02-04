"""
Telegram notification module for transit alerts.
"""

import os
import asyncio
from typing import List, Optional
from telegram import Bot
from telegram.error import TelegramError

from src import logger
from src.constants import PossibilityLevel, TARGET_TO_EMOJI


async def send_telegram_notification(flight_data: List[dict], target: str) -> bool:
    """
    Send Telegram notification for medium/high probability transits.

    Returns True if notification was sent successfully, False otherwise.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.warning("Telegram not configured (missing BOT_TOKEN or CHAT_ID), skipping notification")
        return False

    # Filter for medium/high probability transits
    possible_transits = []
    for flight in flight_data:
        if flight.get("possibility_level") in (
            PossibilityLevel.MEDIUM.value,
            PossibilityLevel.HIGH.value,
        ):
            eta_min = flight.get('time', 0)
            diff_sum = flight.get("alt_diff", 0) + flight.get("az_diff", 0)
            possible_transits.append(
                f"• {flight.get('id', 'Unknown')} in {eta_min} min\n"
                f"  {flight.get('origin', '?')}->{flight.get('destination', '?')}\n"
                f"  ∑△ {diff_sum:.2f}°"
            )

    if not possible_transits:
        logger.debug("No medium/high probability transits to notify")
        return False

    # Build message
    emoji = TARGET_TO_EMOJI.get(target, "")
    transit_txt = "transit" if len(possible_transits) == 1 else "transits"
    title = f"{emoji} {len(possible_transits)} possible {transit_txt}"

    message = f"<b>{title}</b>\n\n" + "\n\n".join(possible_transits[:5])  # Max 5

    # Send via Telegram
    try:
        bot = Bot(token=bot_token)
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode='HTML'
        )
        logger.info(f"✅ Telegram notification sent: {len(possible_transits)} transits")
        return True

    except TelegramError as e:
        logger.error(f"❌ Telegram notification failed: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error sending Telegram: {e}")
        return False
