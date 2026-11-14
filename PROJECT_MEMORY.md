# News Bot project memory

Last updated: 2026-11-14

A snapshot, not a changelog. `git log` is the source of truth for what is merged.

## Where to read

- How to work in this repository (rules, commands): [CLAUDE.md](./CLAUDE.md). Authoritative.
- What the bot does and how to run it: [README.md](./README.md).
- Research results: [docs/research/2026-10-analytics-findings.md](./docs/research/2026-10-analytics-findings.md); scripts in `research/`.
- Audit, open work and open owner decisions: [docs/audit/](./docs/audit/)
  (`remediation-roadmap.md`, `decision-log.md`).

## Project at a glance

A personal research assistant for US, Norwegian and EU stocks: Telegram briefings
with news and a trade screen, risk figures, a forward test, a log of the owner's
own trades and a read-only web dashboard. It never places orders.

`news_bot.py` is a thin entry point. The application is the `newsbot/` package,
one module per concern; settings are validated with Pydantic Settings
(`newsbot/config.py`). Telegram is the only control channel. Slack is a one-way
mirror; trades and holdings are never sent there.

| Area | Modules |
|---|---|
| Startup, schedule, commands | `bot`, `commands`, `briefing`, `messaging`, `reports` |
| Plumbing | `config`, `state`, `logsetup`, `util` |
| News | `news_feeds` (feeds, heuristic and local-LLM ranking), `catalysts` |
| Screen | `market_data`, `indicators`, `ranking`, `screener`, `charts` |
| Risk | `risk` (`trade_risk` is the one calculation every view uses), `risk_forecast`, `relative_strength`, `trade_check` |
| Store | `history_store`: SQLite with screening history, `published_picks` and `signal_archive` |
| Tests of the rules | `backtest`, `backtest_reports`, `benchmarks`, `forward_test`, `archive_quality`, `exchange_hours` |
| Own data | `stock_list`, `price_store`, `company_numbers`, `snapshots`, `data_checks`, `data_health`, `backup` |
| Scores | `score_inputs`, `scores`, `report_card` |
| Own trades | `fills`, `holding_alerts`, `scorecard` |
| Dashboard | `streamlit/` (`app.py` pages, `auth.py` and `serve.py` login), a separate image |

## What the research says (read before changing the screen)

Ten years of daily data found no reliable edge in the screen, in simple price and
earnings signals or in the four company scores over holding the same stocks. All
3,456 combinations of the five gates were swept (issue #47): none is positive in
both development and holdout at 5, 10 or 20 sessions. Do not re-tune them.
Momentum looked regime-dependent, but survivorship changes across the same
boundary, so neither is a finding. `/recommendations` is a screen, not a signal:
picks are grouped (volume-confirmed, unconfirmed, extended) and alphabetical, not
ranked 1-5. The underlying rank is still archived so `/forwardtest` can test it.

## Safeguards that matter

- `signal_archive` and `published_picks` are write-once and never pruned. A
  published pick keeps the risk lines it was sent with; older picks have none
  and are never given one.
- The forward test enters at the first open after a signal was recorded, on
  listed exchanges only. Each row carries a rule id (gates plus hold, `-h20`);
  rows from before the hold joined the id keep their old id.
- Gap-risk text exists only for measured stop multiples (`risk.GAP_STUDIES`: 2
  and 3 x ATR below the signal close); anything else prints "not measured".
- The stop is `RISK_STOP_ATR_MULTIPLE` x ATR: code default 2, `.env.example` 3.
  The live value is whatever the Pi's `.env` says.
- Company numbers from ESEF filings are dated by when the filing was added.
- The local LLM only ranks headlines.
- Still open: the forward test's 60-session threshold counts overlapping
  sessions (P1-6); costs are one flat rate for every market (P1-1); the archive
  does not record which universe a row came from (P1-7).

## Operations

- **Python:** production is 3.15 (both Dockerfiles). CI tests 3.11 and 3.14, then
  runs the suite inside the built image.
- **Checks (same as CI):**
  `ruff check news_bot.py newsbot tests streamlit research scripts`, then
  `python -W error::ResourceWarning -m coverage run -m unittest -v` and
  `python -m coverage report` (95% floor in `.coveragerc`). CI also runs
  `pip-audit`, validates the compose file and exercises the hardened image.
  Tests never reach the network.
- **Deployment:** Docker Compose on a Raspberry Pi 5 (`raspberrypi5-02`): the bot
  (`news-notifier`) and the dashboard (`dashboard-streamlit`, port 8002, password
  login, plain HTTP). `scripts/deploy.sh` backs up, rebuilds, checks health and
  prints a rollback. With the systemd timer from `scripts/install_autodeploy.sh`
  installed, a merge to `main` deploys itself.
- **Runtime:** the bot starts as root only to chown `/app/data`, then runs as
  uid 10001 on a read-only root filesystem. Data lives on the `news_bot_data`
  volume; a checked database copy is written nightly to a host folder, and
  `scripts/pull_backups.sh` pulls those to another machine.
- **News ranking:** the Pi uses an Ollama container from another compose project
  over `RANKER_NETWORK`. The bundled Ollama profile is optional and hardened.
- **Settings:** `.env.example` documents the settings and holds no real
  credentials. `FREEN_EWS_API_KEY` and `FINHUB_API_KEY` are old spellings the
  code still reads as fallbacks.
