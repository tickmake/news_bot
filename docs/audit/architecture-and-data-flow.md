# Architecture and data flow (as built, `d0def12`)

## Processes

| Container | Source | Role | Exposure |
|---|---|---|---|
| `daily_news_bot` | `news_bot.py` → `newsbot/bot.py:main()` | APScheduler (`BlockingScheduler`) plus a Telegram long-poll thread | Outbound only |
| `news_bot_dashboard_streamlit` | `streamlit/app.py` | "Fjord Signals" dashboard; opens the SQLite file with `mode=ro` | `0.0.0.0:8002`, no authentication |
| `ollama` (shared on the Pi) | Optional compose service | Ranks headlines with `qwen2.5:3b` | Host `*:11434` |

**Hardening:** the bot drops to uid 10001 with a read-only root filesystem and only CHOWN, SETUID and SETGID capabilities. The dashboard runs as `nobody`, read-only. Ollama runs as root with no resource limits.

**Deploy:** a systemd user timer runs every 5 minutes and calls `scripts/deploy.sh`. The script takes a `flock`, tags a rollback image, backs up the database, runs `git pull --ff-only` and `compose up --build`, then checks health.

## Scheduled jobs (`newsbot/bot.py`)

Times are in `SETTINGS.tz`.

| When | Job |
|---|---|
| 07:00 and 19:00 | Briefing |
| 12:00 | Health ping |
| Weekdays 22:30 | Holding alerts |
| 1st of the month 08:00 | Scorecard |
| 06:30 and 18:30 | Settle forward test |
| 06:45 and 18:45 | Settle picks |
| Tue–Sat 06:00 | Prices and data checks |
| Sun 05:00 | Company numbers |
| Sun 07:30 | Scores |
| 02:30 | Snapshots |
| 04:00 | Backup |
| Every minute | Heartbeat |
| At startup | Catch-up briefing |

## Data flow

```
yfinance / Finnhub ──► market_data ──► indicators (drop unfinished bar; ATR/RSI simple mean; EMA20)
                             │                  │
                             ▼                  ▼
                       price_store       screener._failed_gates ─► ranking (tiers) ─► risk.size_position / fee_line
                       (daily_prices)           │                                 relative_strength, catalysts
                                                ▼
                         history_store: signal_archive (first-write-wins), published_picks
                                                │
                     forward_test.forward_outcome (next-open entry, horizon close, flat bps)
                                                │
          briefing / reports / charts ─► messaging ─► Telegram (+ Slack copy)
                                                └──► streamlit/app.py (read-only)
ESEF (filings.xbrl.org, GLEIF) + Yahoo statements ─► company_numbers ─► score_inputs (month-end) ─► scores
RSS feeds ─► news_feeds (Ollama rank or heuristic) ─► briefing, catalysts
User /fill /deposit ─► fills (real_trades, deposits) ─► scorecard, dashboard
```

## Storage

There is one SQLite file, `/app/data/.news_bot_trade_history.db`, on the volume `news_bot_news_bot_data`.

**Tables:**
- `trade_metrics_history`
- `published_picks`
- `signal_archive`
- `daily_prices`
- `price_backfill_done`
- `company_numbers`
- `esef_filings_read`
- `company_numbers_version`
- `score_inputs`
- `company_scores`
- `stock_snapshots`
- `real_trades`
- `deposits`

**Migrations:** there is no framework. Tables use `CREATE TABLE IF NOT EXISTS`, plus ad-hoc fixes in `history_store` (renaming to a legacy table) and in `company_numbers._migrate`.

**Backups:** nightly through `newsbot/backup.py`. Each copy passes `PRAGMA integrity_check`, 14 are kept, and they go to `/data/news_bot_backups` on the NVMe. `scripts/pull_backups.sh` copies them off the box.

## Providers

| Provider | Used for | Known limits |
|---|---|---|
| Yahoo (yfinance) | Prices, discovery, earnings dates, US fundamentals | Unofficial; no publication dates; about 4 years of statements; no prices for delisted names; adjustment semantics not verified |
| Finnhub | Supplementary market data | API key; rate limits |
| filings.xbrl.org + GLEIF | NO/EU annual IFRS (ESEF) | From 2020 accounts on; HTTP 429 when rate-limited; debt and share counts often missing |
| Wikipedia revisions | S&P 500 point-in-time membership | Research only |
| RSS | News | The domain allowlist in the Pi `.env` silently drops items |

## Features: implemented vs claimed

**Implemented:**
- Screen with tiers
- Sizing and fees
- Correlation checks
- Earnings flag
- Relative strength and beta
- Forward-test archive
- Real-fill ledger
- Four descriptive scores
- Dashboard
- Backups
- Auto-deploy

**Absent:**
- Macro regime
- Sentiment score
- Per-market cost model
- Trading calendars from a library
- Survivorship-free prices
- SEC/EDGAR
- Dashboard authentication
- Type checking
