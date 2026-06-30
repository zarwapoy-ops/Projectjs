# manga-starz Discord Bot

A Discord notification bot for the Arabic manga website manga-starz.net. It scrapes the site for new chapters and notifies Discord users/channels when updates are available.

## Setup

### Required Secrets
- `DISCORD_BOT_TOKEN` — Your Discord bot token (from Discord Developer Portal)
- `DEVELOPER_IDS` — Comma-separated Discord user IDs that have admin access to bot commands (e.g. `123456789,987654321`)

### Running
The bot is configured to run via the "Start application" workflow using:
```
python run_bot.py
```

## Architecture
- `run_bot.py` — Entry point
- `mangastarz/bot.py` — Discord client, slash commands, polling loop
- `mangastarz/scraper.py` — Web scraping logic (cloudscraper + BeautifulSoup4)
- `mangastarz/database.py` — Async SQLite storage (subscriptions, seen chapters, cache)

## Slash Commands
- `/latest` — Show last 10 chapters from manga-starz.net
- `/search <name>` — Search for a manga/manhwa
- `/watch <name>` — Subscribe to DM notifications for new chapters
- `/unwatch <name>` — Unsubscribe from DM notifications
- `/list` — List your DM subscriptions
- `/status` — Bot status and your subscription count
- `/setchannel` — [Dev] Set notification channel
- `/watchall` — [Dev] Enable notifications for all new titles
- `/check` — [Dev] Manually trigger a chapter check
- `/adddev` / `/removedev` / `/listdevs` — [Dev] Manage developer access

## User Preferences
