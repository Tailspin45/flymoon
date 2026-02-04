# Telegram Bot Setup Guide

## Step 1: Create a Telegram Bot

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
6. Copy this token - you'll need it for your `.env` file

## Step 2: Get Your Chat ID

1. Search for `@userinfobot` in Telegram
2. Start a chat with userinfobot
3. It will automatically reply with your user information
4. Copy your **chat ID** (it's a number like `123456789`)

## Step 3: Configure Your .env File

Add these two lines to your `.env` file:

```bash
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_CHAT_ID=123456789
```

Replace the values with your actual bot token and chat ID.

## Step 4: Test the Bot

1. Start a chat with your bot by searching for its username in Telegram
2. Send `/start` to your bot (this opens the chat)
3. Run your transit capture script
4. You should receive a notification in Telegram!

## Troubleshooting

**Bot not sending messages?**
- Make sure you've started a chat with your bot (send `/start`)
- Verify your bot token is correct
- Verify your chat ID is correct

**How to find my chat ID again?**
- Use `@userinfobot` or `@get_id_bot` on Telegram

**Can I use the same bot for multiple purposes?**
- Yes! The bot token stays the same, you can use it for other projects

## Notes

- Telegram bots work on iOS, Android, Web, and Desktop
- Messages are free and unlimited
- Your bot is private - only people you share the username with can find it
- You can customize your bot's profile picture and description via @BotFather
