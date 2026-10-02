# News Bot project memory

Last updated: 2026-10-02 (cleanup pass)

## Project at a glance

Python Telegram news and market-briefing bot. `news_bot.py` is a thin entry
point; the application lives in the `newsbot/` package (one module per concern:
config/state/logging, messaging, news and market data, the trade screener,
backtest and reports, briefing, commands and startup). Settings are validated with Pydantic Settings. `test_news_bot.py`
contains the `unittest` suite. Docker Compose is the deployment path, with
SQLite-backed trade history persisted on its data volume.

The bot also supports an optional one-way Slack mirror. Incoming webhooks send
text; chart uploads additionally use the Slack Web API when `SLACK_BOT_TOKEN`
and `SLACK_CHANNEL_ID` are configured. Slack does not receive bot commands;
Telegram remains the control channel.

## Latest committed developments

`main` is at `c841b2a` (2026-09-22, Slack chart upload). Branch
`agents/memory-file-summary-latest-devs` (`f3d4145`, pushed, not yet in a PR)
adds the evaluation work below on top of it. Delivered on `main`:

- Technical-analysis charts on `/recommendations` (Telegram, optionally Slack).
- A continuous trade-screener universe and a reworked `/watchlist`.
- `/stocks` weekly top picks, focused news/recommendation commands, trade
  signal history, `/analyze`, and Slack message mirroring.

On the evaluation branch:

- `/performance`: walk-forward evaluation of the current trade rules using
  adjusted daily bars, next-session-open entry, fixed-session exit, cost
  estimates, benchmark matching, liquidity filtering, development/holdout
  separation and robustness summaries.
- `/diagnostics`: signal-time cohort breakdowns and paired same-date comparison
  of top-five picks against other qualifiers.
- `/pickperformance`: outcomes of picks actually delivered in Telegram text,
  backed by an immutable publication/outcome ledger in the trade-history DB.
- An opt-in `broad-v1` universe in `evaluation_universe.csv` (44 stocks across
  the US, continental Europe, India and Norway), used only by `/performance`
  and `/diagnostics`.

Historical evaluation is isolated from live recommendations, discovery,
`/stocks`, `/watchlist` and delivered-pick behaviour. Known limitations:
survivorship/universe bias, local-currency cross-market comparison, volume as
a liquidity proxy, and an already-inspected holdout. Do not describe these
reports as a portfolio simulation or as proof of profitability.

Deployment: the running container on `raspberrypi5-02` predates all of the
above; a redeploy is pending. Open follow-ups live in GitHub issue #12.

## Useful project commands and files

- Run tests: `python -m unittest -v`
- Entry point: `news_bot.py`; implementation and command routing: `newsbot/` (`commands.py` routes commands, `bot.py` starts the process)
- Tests: `test_news_bot.py`
- User/deployment documentation and configuration details: `README.md`
- Example environment settings: `.env.example` (never put actual credentials in
  this memory file)
- Docker deployment: `Dockerfile`, `docker-compose.yml`
- Fixed evaluation basket: `evaluation_universe.csv`

Before assuming the in-progress changes are complete or deployed, check the
worktree and run the test suite. Update this file when meaningful project
milestones supersede the snapshot above.
