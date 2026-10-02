# News Bot project memory

Last updated: 2026-10-02

## Project at a glance

Python Telegram news and market-briefing bot. `news_bot.py` contains the
application, scheduling, command handlers, market/news collection, and report
generation. Settings are validated with Pydantic Settings. `test_news_bot.py`
contains the `unittest` suite. Docker Compose is the deployment path, with
SQLite-backed trade history persisted on its data volume.

The bot also supports an optional one-way Slack mirror. Incoming webhooks send
text; chart uploads additionally use the Slack Web API when `SLACK_BOT_TOKEN`
and `SLACK_CHANNEL_ID` are configured. Slack does not receive bot commands;
Telegram remains the control channel.

## Latest committed developments

The current `main` HEAD is `c841b2a` (2026-09-22), merging the Slack chart
upload work. Recent delivered features include:

- Technical-analysis charts attached to `/recommendations` in Telegram and
  optionally uploaded to Slack.
- A more continuous trade-screener universe and a reworked `/watchlist`.
- `/stocks` weekly top picks, plus focused news/recommendation commands and
  trade signal history, `/analyze`, and Slack message mirroring.

Consult `git log` for the full commit history; this memory is a snapshot, not a
replacement for Git.

## Current local development (uncommitted at last update)

The worktree has substantial uncommitted changes in `.env.example`, `Dockerfile`,
`README.md`, `docker-compose.yml`, `news_bot.py`, and `test_news_bot.py`, plus
the new `evaluation_universe.csv`. Preserve and inspect these changes before
editing or resetting anything. The implementation and docs currently describe:

- `/performance`: a walk-forward evaluation of current trade rules using
  adjusted historical daily bars, next-session-open entry, fixed-session exit,
  transaction-cost estimates, benchmark matching, historical liquidity
  filtering, development/holdout separation, and robustness summaries.
- `/diagnostics`: signal-time cohort and metric breakdowns and paired,
  same-signal-date comparison of top-five picks against other qualifiers.
- `/pickperformance`: a separate report for picks actually delivered in
  successful Telegram text messages, backed by an immutable publication/outcome
  ledger in the trade-history database.
- An opt-in `broad-v1` historical evaluation universe in
  `evaluation_universe.csv`: 44 stocks across the US, continental Europe, India,
  and Norway. It is for `/performance` and `/diagnostics` only.

The documented design keeps historical evaluation isolated from live
recommendation, discovery, `/stocks`, `/watchlist`, and delivered-pick behavior.
It explicitly discloses limitations including survivorship/universe bias,
local-currency cross-market comparisons, liquidity as a volume proxy, and the
exploratory status of an already inspected holdout. Do not describe these
reports as a portfolio simulation or treat them as proof of profitability.

The root `.claude/agents/Bjørn.agent.md` is present but appears to be an
unconfigured agent template.

## Useful project commands and files

- Run tests: `python -m unittest -v`
- Main implementation and command routing: `news_bot.py`
- Tests: `test_news_bot.py`
- User/deployment documentation and configuration details: `README.md`
- Example environment settings: `.env.example` (never put actual credentials in
  this memory file)
- Docker deployment: `Dockerfile`, `docker-compose.yml`
- Fixed evaluation basket: `evaluation_universe.csv`

Before assuming the in-progress changes are complete or deployed, check the
worktree and run the test suite. Update this file when meaningful project
milestones supersede the snapshot above.
