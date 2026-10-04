# News Bot project memory

Last updated: 2026-10-03

## Project at a glance

Python Telegram news and market-briefing bot with a trade screener. `news_bot.py`
is a thin entry point; the application lives in the `newsbot/` package, one
module per concern: config, state, logging, messaging, news and market data, the
screener, risk analytics and relative strength, backtest and reports, briefing,
commands and startup. Settings are validated with Pydantic Settings. Tests are
`unittest` files named `test_*.py`. Docker Compose is the deployment path, with
SQLite trade history and bot state on the `news_bot_data` volume.

An optional one-way Slack mirror sends text through an incoming webhook; chart
uploads use the Web API when `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` are set.
Telegram is the only control channel.

## What the research says (read before changing the screen)

Ten years of daily data found no reliable edge in the screen or in simple price and
earnings signals over holding the same stocks; the gates are significantly worse than
the average stock at a 10-session hold. Momentum is regime-dependent (nothing before
2022, strong since). Details, tables and caveats: `docs/research/2026-10-analytics-findings.md`;
scripts in `research/`. Issue #12 holds the earlier audit. `/recommendations` is a
screen, not a signal. Do not tune thresholds further without a paired test against the
ungated baseline on development data first.

## What the bot does now

- Briefings at 07:00 and 19:00 (Europe/Oslo by default), isolated per section with a
  time budget, one briefing at a time, and a catch-up when a restart missed a slot.
- Commands: `/now`, `/news`, `/recommendations` (with charts), `/stocks`,
  `/watchlist`, `/analyze`, `/performance`, `/diagnostics`, `/pickperformance`,
  `/forwardtest`, `/health`.
- Risk analytics under each candidate and in `/analyze` (`newsbot/risk.py`): a 2xATR
  volatility stop, fixed-fractional position size, earnings-date flags, correlation
  and sector clustering, totals at work and at risk.
- Relative strength and beta versus the market and, for US stocks, the sector ETF
  (`newsbot/relative_strength.py`). Context, not a ranking.
- A permanent `signal_archive` of every screened symbol, settled by `/forwardtest`
  into out-of-sample results (needs about 60 paired dates before it can say anything).
- Market-data alerting when too many symbols return no data or prices go stale.

## Operations

- Runs on `tickmake@raspberrypi5-02` (Raspberry Pi 5). The container starts as root
  only to chown `/app/data`, then drops to an unprivileged user; the root filesystem is
  read-only with a `/tmp` tmpfs. See `docker-compose.yml` and `docker-entrypoint.sh`.
- CI: ruff (rules in `ruff.toml`), `pip-audit`, the suite under coverage on Python 3.11
  and 3.14 with `ResourceWarning` as an error (floor in `.coveragerc`), and a docker job.
  Dependabot proposes dependency, Docker and Actions updates.
- Tests must never reach the network; features that call yfinance are switched off
  in `test_news_bot.py` and mocked in their own tests.

## Useful project commands and files

- Run tests: `python -m unittest -v`
- Entry point: `news_bot.py`; command routing: `newsbot/commands.py`; startup: `newsbot/bot.py`
- Example environment settings: `.env.example` (never put real credentials in this file)
- Fixed evaluation basket: `evaluation_universe.csv`
- User and deployment documentation: `README.md`

Update this file when a milestone supersedes the snapshot above; `git log` is the
source of truth for what is merged.
