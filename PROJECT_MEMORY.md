# News Bot project memory

Last updated: 2026-10-03

## Project at a glance

Python Telegram news and market-briefing bot with a trade screener. `news_bot.py`
is a thin entry point; the application lives in the `newsbot/` package, one
module per concern: config, state, logging, messaging, news and market data, the
screener, risk analytics and relative strength, backtest and reports, briefing,
commands and startup. Settings are validated with Pydantic Settings. Tests are
`unittest` files named `tests/test_*.py`. Docker Compose is the deployment path, with
SQLite trade history and bot state on the `news_bot_data` volume.

An optional one-way Slack mirror sends text through an incoming webhook; chart
uploads use the Web API when `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` are set.
Telegram is the only control channel.

## What the research says (read before changing the screen)

Ten years of daily data found no reliable edge in the screen or in simple price and
earnings signals over holding the same stocks; the gates are significantly worse than
the average stock at a 10-session hold. **The thresholds have now been swept** (issue
#47): of all 3,456 combinations of the five gates, none is positive in both development
and holdout at 5, 10 or 20 sessions, so there is nothing to tune toward -- do not
re-tune them, and treat a request to do so as already answered. Momentum looked
regime-dependent (nothing before 2022, strong since), but the share of the index that
cannot be priced today falls across the same boundary, so regime and survivorship are
confounded and neither is a finding. Details, tables and caveats:
`docs/research/2026-10-analytics-findings.md`; scripts in `research/`. Issue #12 holds
the earlier audit. `/recommendations` is a screen, not a signal, and since the ordering
between two qualifying names cannot be justified, picks are grouped into tiers rather
than ranked 1-5.

## What the bot does now

- Briefings at 07:00 and 19:00 (Europe/Oslo by default), isolated per section with a
  time budget, one briefing at a time, and a catch-up when a restart missed a slot.
- Commands: `/now`, `/news`, `/recommendations` (with charts), `/stocks`,
  `/watchlist`, `/analyze`, `/performance`, `/diagnostics`, `/pickperformance`,
  `/forwardtest`, `/health`.
- Risk analytics under each candidate and in `/analyze` (`newsbot/risk.py`): an N x ATR
  volatility stop below the signal close (`RISK_STOP_ATR_MULTIPLE`: code default 2, the
  Pi runs 3), fixed-fractional position size, earnings-date flags, correlation
  and sector clustering, totals at work and at risk. Reward:risk is measured to that
  same stop, not to the 20-day low. Correlated picks also get a combined
  stopped-out-together figure.
- One risk calculation (`risk.trade_risk`, #161) feeds `/recommendations`, `/analyze`,
  `/check` and the dashboard: position, loss at the stop, gap estimate, fee, all modelled
  as if bought at the signal close (the buy price is not known, and the text says so). The
  gap sale is the stop price less a shortfall measured in percent of the stop price, from
  `risk.GAP_STUDIES`: one row per stop that `research/gap_risk.py` measured, checked by a
  test against `docs/research/data/`; any other multiple prints "not measured". A published pick saves its risk
  lines in `published_picks.snapshot_json` under `risk`; older picks have none and are
  never given one.
- Relative strength and beta versus the market and, for US stocks, the sector ETF
  (`newsbot/relative_strength.py`). Context, not a ranking.
- Candidates grouped as volume-confirmed / unconfirmed / extended (RSI 70+) and listed
  alphabetically inside each group. `signal_archive` still records the underlying rank
  so `/forwardtest` can test whether rank predicted anything.
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
  in `tests/test_news_bot.py` and mocked in their own tests.

## Useful project commands and files

- Run tests: `python -m unittest -v`
- Entry point: `news_bot.py`; command routing: `newsbot/commands.py`; startup: `newsbot/bot.py`
- Example environment settings: `.env.example` (never put real credentials in this file)
- Fixed evaluation basket: `evaluation_universe.csv`
- User and deployment documentation: `README.md`

Update this file when a milestone supersedes the snapshot above; `git log` is the
source of truth for what is merged.
