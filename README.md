# News Bot

Automated Telegram briefing bot that sends curated daily updates with:

- Norwegian morning headlines
- Global top news
- Business stories
- Live stock movers from public market screeners
- Live mutual fund / ETF movers from public market screeners
- Short-term trade candidates (informational screener)
- Daily greeting and rotating quote (morning) / evening greeting

The bot is built in Python and scheduled with APScheduler.

## Features

- **Telegram delivery** using Bot API with HTML formatting.
- **Readable output** with concise headlines and `(more)` links.
- **Tabular finance sections** rendered via `<pre>` for clarity in Telegram.
- **Twice-daily schedule** at `07:00` and `19:00` (local timezone).
- **Deterministic daily rotation** for greetings/quote (stable within a day).
- **Live public data feeds** for news, stocks, and funds (no hardcoded default symbols).
- **Typed settings validation** via `pydantic-settings`.
- **Fallback-safe behavior** when API data is missing or incomplete.
- **Automatic retries/backoff** for external API calls.
- **Duplicate headline suppression** across same-day sends.
- **Telegram command support** (`/now`, `/morning`, `/evening`, `/watchlist`, `/health`).
- **Health ping** support for runtime monitoring.
- **CI test workflow** via GitHub Actions.

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
```

Tip: you can copy from `.env.example` and fill in your secrets.

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

### Optional

- `TZ` - timezone for scheduler (default `Europe/Oslo`)
- `RECIPIENT_NAME` - name shown in greeting (default `Sunil`)
- `TELEGRAM_MESSAGE_MAX_CHARS` - chunk size per Telegram message (default `3900`)
- `STATE_FILE` - local JSON state file path (default `.news_bot_state.json`)
- `COMMAND_POLL_ENABLED` - enable Telegram command polling (default `true`)
- `COMMAND_POLL_INTERVAL_MINUTES` - command polling cadence (default `2`)
- `SEND_STARTUP_BRIEFING` - run one immediate briefing on container start (default `false`)
- `HEALTH_PING_ENABLED` - enable daily health ping (default `true`)
- `HEALTH_PING_CHAT_ID` - optional separate chat for health pings
- `GLOBAL_NEWS_FEEDS` - comma-separated RSS feed URLs for global news
- `BUSINESS_NEWS_FEEDS` - comma-separated RSS feed URLs for business news
- `NORWAY_NEWS_FEEDS` - comma-separated RSS feed URLs for Norway-focused news
- `STOCK_SCREENERS` - comma-separated Yahoo predefined screener IDs for equities
- `FUND_SCREENERS` - comma-separated Yahoo predefined screener IDs for funds/ETFs
- `SCREENER_QUOTE_LIMIT` - number of quotes to fetch per screener
- `SCREENER_REQUEST_TIMEOUT_SECONDS` - timeout for screener API calls (default `6`)
- `SCREENER_CACHE_TTL_SECONDS` - in-memory screener cache duration (default `90`)
- `SCREENER_FAILURE_COOLDOWN_SECONDS` - cooldown after screener SSL/network failures (default `300`)

### Optional Universe Configuration

You can optionally inject your own symbols with comma-separated `Label:SYMBOL` entries.

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

If these are empty, the bot relies fully on live screener data.

### Trade Risk Controls

- `TRADE_MIN_SCORE`
- `TRADE_MIN_WEEK_MOMENTUM_PCT`
- `TRADE_MIN_DAY_CHANGE_PCT`
- `TRADE_MIN_VOLUME_RATIO`
- `TRADE_MAX_DRAWDOWN_PCT`
- `TRADE_MAX_ATR_PCT`

## Scheduling

The bot schedules:

- **07:00 local time**
- **19:00 local time**

Configured in:

```python
scheduler.add_job(job_daily_briefing, "cron", hour="7,19", minute=0)
```

Additional jobs:

- daily health ping (`12:00`)
- Telegram command polling (`interval`, default every 2 minutes)

## Telegram Commands

After sending `/start` to the bot, you can use:

- `/now` - send full briefing immediately
- `/morning` - send morning-style briefing
- `/evening` - send evening-style briefing
- `/watchlist` - send market + screener sections only
- `/health` - send runtime health report

## Testing

Run unit tests:

```bash
source .venv/bin/activate
python -m unittest -v
```

CI runs this same test suite on push/PR via `.github/workflows/ci.yml`.

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
6. Market Watch (live stock movers table)
7. Mutual Funds/ETFs (live movers table)
8. Short-Term Trade Candidates (informational table)

## Important Notes

- **No financial guarantees:** The screener is informational only and does not guarantee profits.
- **Market data coverage varies:** screener and RSS availability can differ by region/time.
- **Mutual fund availability:** Norway mutual fund coverage on Yahoo is limited; ETFs are used where needed.
- **Message splitting:** long messages are automatically split into multiple Telegram parts.

## Troubleshooting

- **`chat not found`**
  - Send `/start` to the bot first (or add it to the target group/channel with permissions).
- **No news returned**
  - Validate RSS feed URLs and domain allow/block filters.
- **No finance rows**
  - Some screener IDs may be rate-limited or empty; adjust `STOCK_SCREENERS` / `FUND_SCREENERS`.
  - If Yahoo TLS is unstable in your host network, reduce `SCREENER_REQUEST_TIMEOUT_SECONDS` and rely on cooldown to keep `/now` responsive.
- **Network/proxy failures**
  - Check outbound connectivity to `api.telegram.org`, configured RSS sources, and Yahoo endpoints.

## Security

- Do not commit `.env`.
- Keep bot tokens and API keys private.
- Rotate credentials if accidentally exposed.

## License

Internal/private project unless a license is explicitly added.
