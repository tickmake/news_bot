# News Bot

Automated Telegram briefing bot that sends curated daily updates with:

- Norwegian morning headlines
- Global top news
- Business stories
- Market watch (top weekly gainers in India, USA, Norway)
- Weekly mutual fund performers (India and Norway-focused instruments)
- Short-term trade candidates (informational screener)
- Daily greeting and rotating quote (morning) / evening greeting

The bot is built in Python and scheduled with APScheduler.

## Features

- **Telegram delivery** using Bot API with HTML formatting.
- **Readable output** with concise headlines and `(more)` links.
- **Tabular finance sections** rendered via `<pre>` for clarity in Telegram.
- **Twice-daily schedule** at `07:00` and `19:00` (local timezone).
- **Deterministic daily rotation** for greetings/quote (stable within a day).
- **Configurable stock/fund universes** via environment variables.
- **Fallback-safe behavior** when API data is missing or incomplete.

## Project Structure

- `news_bot.py` - main bot logic and scheduler
- `test_news_bot.py` - unit tests
- `requirements.txt` - Python dependencies
- `Dockerfile` - container image definition
- `docker-compose.yml` - service orchestration

## Requirements

- Python `3.11+` recommended
- Telegram bot token
- Telegram chat ID
- NewsAPI key

## Quick Start (Local)

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create `.env` with required values:

```bash
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
NEWS_API_KEY=your_newsapi_key
```

4. Run a one-off briefing:

```bash
set -a && . ./.env && set +a
python -c "import news_bot; print(news_bot.job_daily_briefing())"
```

5. Run scheduler mode:

```bash
set -a && . ./.env && set +a
python news_bot.py
```

## Environment Variables

### Required

- `TELEGRAM_TOKEN` - Telegram bot token
- `TELEGRAM_CHAT_ID` - target chat/channel/group ID
- `NEWS_API_KEY` - key for [NewsAPI](https://newsapi.org/)

### Optional

- `TZ` - timezone for scheduler (default `Europe/Oslo`)
- `RECIPIENT_NAME` - name shown in greeting (default `Sunil`)
- `NORWAY_NEWS_QUERY` - query terms for Norway news (default `Norway OR Norge`)

### Optional Universe Configuration

You can override default universes with comma-separated `Label:SYMBOL` entries.

Supported variables:

- `USA_STOCK_UNIVERSE`
- `INDIA_STOCK_UNIVERSE`
- `NORWAY_STOCK_UNIVERSE`
- `INDIA_MUTUAL_FUNDS`
- `NORWAY_MUTUAL_FUNDS`

Example:

```bash
USA_STOCK_UNIVERSE="Apple:AAPL,Microsoft:MSFT,NVIDIA:NVDA"
INDIA_STOCK_UNIVERSE="Reliance:RELIANCE.NS,TCS:TCS.NS"
INDIA_MUTUAL_FUNDS="Nifty BeES:NIFTYBEES.NS,Gold BeES:GOLDBEES.NS"
```

If parsing fails or values are empty, built-in defaults are used.

## Scheduling

The bot schedules:

- **07:00 local time**
- **19:00 local time**

Configured in:

```python
scheduler.add_job(job_daily_briefing, "cron", hour="7,19", minute=0)
```

## Testing

Run unit tests:

```bash
source .venv/bin/activate
python -m unittest -v
```

## Docker

Build and run with Compose:

```bash
docker compose up -d --build
```

Ensure environment values are provided in your shell or an env file before launch.

## Message Sections

Each briefing includes:

1. Time-based greeting (`Hello, <RECIPIENT_NAME>!`)
2. Morning quote (morning runs only)
3. Early Morning Norway News
4. Global News
5. Top Business Stories
6. Market Watch (weekly gainers table)
7. Mutual Funds (weekly performers table)
8. Short-Term Trade Candidates (informational table)

## Important Notes

- **No financial guarantees:** The screener is informational only and does not guarantee profits.
- **Market data coverage varies:** Yahoo Finance symbol availability can differ by region/instrument.
- **Mutual fund availability:** Norway mutual fund coverage on Yahoo is limited; ETFs are used where needed.

## Troubleshooting

- **`chat not found`**
  - Send `/start` to the bot first (or add it to the target group/channel with permissions).
- **No news returned**
  - Validate `NEWS_API_KEY` and quota.
- **No finance rows**
  - Some symbols may be unavailable temporarily; adjust universe variables.
- **Network/proxy failures**
  - Check outbound connectivity to `api.telegram.org`, NewsAPI, and Yahoo endpoints.

## Security

- Do not commit `.env`.
- Keep bot tokens and API keys private.
- Rotate credentials if accidentally exposed.

## License

Internal/private project unless a license is explicitly added.
