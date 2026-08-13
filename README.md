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
- **Relevance-ranked headlines** by importance, topic weight, and recency — not raw feed order.
- **Cross-outlet and cross-day deduplication**, so one story appears once.
- **Local LLM ranking** via Ollama — no API key, no data leaves your network — with a deterministic heuristic fallback.
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
- `COMMAND_POLL_ENABLED` - enable Telegram command handling (default `true`)
- `COMMAND_LONG_POLL_TIMEOUT_SECONDS` - Telegram long-poll hold time; commands respond near-instantly (default `25`)
- `COMMAND_POLL_INTERVAL_MINUTES` - **deprecated**, retained for compatibility but unused (long-polling replaced interval polling)
- `SEND_STARTUP_BRIEFING` - run one immediate briefing on container start (default `false`)
- `HEALTH_PING_ENABLED` - enable daily health ping (default `true`)
- `HEALTH_PING_CHAT_ID` - optional separate chat for health pings
- `NEWS_API_KEY` - optional NewsAPI key for structured news fetch
- `FREENEWS_API_KEY` - optional FreeNews API key
- `FREEN_EWS_API_KEY` - backward-compatible alias for existing envs
- `FINNHUB_API_KEY` - Finnhub API key (recommended primary quote source)
- `FINHUB_API_KEY` - backward-compatible alias for existing envs
- `NEWS_FETCH_PRIORITY` - provider order, e.g. `newsapi,freenews,rss`
- `FREENEWS_API_URL` - FreeNews endpoint URL (default `https://freenewsapi.com/api/v1/news`)
- `FINNHUB_API_URL` - Finnhub base URL (default `https://finnhub.io/api/v1`)
- `GLOBAL_NEWS_FEEDS` - comma-separated RSS feed URLs for global news
- `BUSINESS_NEWS_FEEDS` - comma-separated RSS feed URLs for business news
- `NORWAY_NEWS_FEEDS` - comma-separated RSS feed URLs for Norway-focused news
- `STOCK_SCREENERS` - comma-separated Yahoo predefined screener IDs for equities
- `FUND_SCREENERS` - comma-separated Yahoo predefined screener IDs for funds/ETFs
- `SCREENER_QUOTE_LIMIT` - number of quotes to fetch per screener
- `SCREENER_REQUEST_TIMEOUT_SECONDS` - timeout for screener API calls (default `6`)
- `SCREENER_CACHE_TTL_SECONDS` - in-memory screener cache duration (default `90`)
- `SCREENER_FAILURE_COOLDOWN_SECONDS` - cooldown after screener SSL/network failures (default `300`)
- `FINNHUB_REQUEST_TIMEOUT_SECONDS` - timeout for Finnhub quote calls (default `4`)
- `FINNHUB_CACHE_TTL_SECONDS` - Finnhub quote cache duration (default `120`)
- `FINNHUB_FAILURE_COOLDOWN_SECONDS` - cooldown on Finnhub failures/rate limits (default `180`)
- `FINNHUB_MAX_SYMBOLS_PER_REFRESH` - max symbols per section refreshed via Finnhub (default `16`)

### News ranking

The bot ranks headlines rather than taking whatever the feeds list first.
Ranking combines real-world importance, your topic weights, and recency, and
collapses the same story reported by multiple outlets into a single line.

Two ranking paths:

- **Local LLM ranking** (default) — a model running in Ollama on your own
  hardware. Catches cross-outlet paraphrase such as "Fed holds rates steady"
  and "Federal Reserve keeps rates unchanged", which share almost no words.
  No API key, no cost per briefing, and no headline ever leaves your network.
- **Heuristic ranking** (automatic fallback) — recency decay, keyword topic
  weights, and source tier. No network at all, fully deterministic.

The heuristic path is used whenever the LLM path is unavailable *or fails*:
Ollama not running, model not pulled, timeout, or malformed response. A
section always renders.

The LLM does not see the whole candidate pool. The heuristic pre-ranks and
the LLM re-ranks its top `NEWS_RANKER_MAX_CANDIDATES` — so the pool stays
wide enough for cross-outlet deduplication while the slow path only handles
a short, already-good list. Latency scales with that number, not pool size.

**Connecting to Ollama.** By default the bot talks to a host named `ollama`
on the *ranker network*. Two ways to provide it:

- *Reuse an existing Ollama* (recommended if you already run one). Point the
  ranker network at that project's network in `.env`:

  ```bash
  RANKER_NETWORK=poster-bot_default
  RANKER_NETWORK_EXTERNAL=true
  ```

  Find the network name with:
  `docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' ollama`

- *Use the bundled service*, off by default so it never duplicates an Ollama
  you already run:

  ```bash
  docker compose --profile bundled-ollama up -d
  ```

  This starts Ollama and pulls `NEWS_RANKER_MODEL` into a named volume.

Outside Docker, run Ollama yourself and set
`NEWS_RANKER_URL=http://localhost:11434`:

```bash
ollama pull llama3.2:3b
```

Note that `NEWS_FETCH_PRIORITY` now orders a single pooled candidate set
rather than selecting one provider — every configured provider is fetched on
each send, which is what makes cross-outlet deduplication possible.

| Variable | Default | Purpose |
|---|---|---|
| `NEWS_RANKER_ENABLED` | `true` | Kill switch |
| `NEWS_RANKER_URL` | `http://localhost:11434` | Ollama endpoint (compose sets `http://ollama:11434`) |
| `NEWS_RANKER_MODEL` | `llama3.2:3b` | Ranking model; must be pulled into Ollama |
| `NEWS_RANKER_MAX_CANDIDATES` | `20` | Shortlist size sent to the LLM |
| `NEWS_RANKER_TIMEOUT_SECONDS` | `240` | Per-call timeout |
| `RANKER_NETWORK` | bundled net | Network carrying Ollama |
| `RANKER_NETWORK_EXTERNAL` | `false` | Set `true` to reuse another project's network |
| `NEWS_MAX_AGE_HOURS` | `30` | Freshness ceiling; undated items exempt |
| `NEWS_CANDIDATE_POOL_SIZE` | `60` | Candidates ranked per section |
| `NEWS_DEDUP_WINDOW_DAYS` | `7` | Cross-day suppression window (includes today) |
| `NEWS_RECENT_TITLE_DAYS` | `3` | Recent titles sent to the ranker |
| `NEWS_TOPIC_WEIGHTS` | unset | Weight overrides, e.g. `markets:2.5,sports:0.1` |

Topic categories are `markets`, `norway`, `india`, `tech` (up-weighted) and
`sports`, `celebrity`, `crime`, `lifestyle`, `shopping` (down-weighted).
Down-weighting is not exclusion — a low-weight headline still appears when
nothing better is available.

There is no per-briefing cost — the trade is hardware instead. Measured on a
Raspberry Pi 5 (CPU only) against a warm `llama3.2:3b`:

| Candidates sent | Latency |
|---|---|
| 10 | ~51 s |
| 20 | ~74 s |
| 40 | ~150 s |

Hence the default shortlist of 20 and the 240 s timeout. Small models are the
weak link on quality, not just speed: in testing, `llama3.2:3b` collapsed
obvious repeats correctly but missed a genuine cross-outlet duplicate and
paired two different central banks as the same story. If ranking quality
matters more than keeping everything on one small box, point
`NEWS_RANKER_URL` at a machine running a 7B+ model. GPU acceleration for the
bundled service is a commented-out block in `docker-compose.yml`.

Run `/health` to see which path is active, the model, call latency, and the
last ranker error.

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

### Trade Candidate Performance

Candidate analysis fetches per-symbol history from yfinance. These controls keep a briefing (and `/now`) responsive on a cold cache:

- `TRADE_UNIVERSE_MAX` - max symbols analysed per run (default `30`)
- `TRADE_FETCH_WORKERS` - parallel history fetch workers (default `6`)
- `TRADE_HISTORY_CACHE_TTL_SECONDS` - per-symbol history cache duration (default `600`)
- `TRADE_TOTAL_DEADLINE_SECONDS` - overall deadline for candidate analysis; partial results returned if exceeded (default `45`)

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
- Telegram command handling runs in a background long-polling thread (near-instant response)

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
  - Set `FINNHUB_API_KEY` to improve quote freshness and resilience for stock/ETF rows.
- **Network/proxy failures**
  - Check outbound connectivity to `api.telegram.org`, configured RSS sources, and Yahoo endpoints.

## Security

- Do not commit `.env`.
- Keep bot tokens and API keys private.
- Rotate credentials if accidentally exposed.

## License

Internal/private project unless a license is explicitly added.
