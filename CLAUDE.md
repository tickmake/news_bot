# news_bot: project instructions

This file is the authoritative set of instructions for Claude Code in this
repository. It was promoted from `docs/audit/proposed-CLAUDE.md` (decision D6),
which stays in place as audit history only. `README.md` explains the bot to its
user and `PROJECT_MEMORY.md` is a short snapshot; where they disagree with this
file about how to work, this file wins. Where any document disagrees with the
code about what the bot does, the code wins: fix the document.

## What this is

A personal swing-trading **research assistant** for US, Norwegian and EU stocks:
Telegram briefings, a screen, risk figures, a forward test and a read-only
dashboard. It is not a trading system. It has no broker connection and **never
places orders**; `/fill` only records trades the owner placed by hand. Do not add
order placement.

## Ground truth

- **No demonstrated edge.** The screen, the factors and the company scores have
  not beaten holding the same stocks. See
  `docs/research/2026-10-analytics-findings.md` and `docs/audit/`. Do not claim
  an edge, and do not add BUY/SELL wording, until the pre-registered protocol in
  `docs/audit/decision-log.md` D4 passes. D4 is still **proposed**, not frozen.
- **Do not re-tune the gates.** All 3,456 threshold combinations failed out of
  sample (findings section 6). `/recommendations` stays grouped and alphabetical.
- **Live settings differ from code defaults.** The Pi's `.env` overrides them
  (stop multiple, risk per trade, hold, universes, feeds, domain allowlist).
  Check both before stating what the live bot does.
- **Secrets.** Never print, log, commit or quote `.env` values, tokens, the
  dashboard password hash or session key. Read single keys only when needed.

## Research rules

Nothing enforces these; they are rules for the work.

- A result that changes advice needs pass rules written **before** the run, a
  development/holdout split, and the holdout looked at once. A holdout that has
  already been inspected is exploratory evidence only.
- Count independent dates, not rows: holds overlap. `/forwardtest` still counts
  overlapping entry sessions toward its 60-session threshold (roadmap P1-6,
  open), so do not read that threshold as 60 independent observations.
- State the universe and its survivorship status with every result. Every
  basket here is built from stocks that can be priced today.
- State the cost assumption. Today that is one flat `TRADE_BACKTEST_COST_BPS`
  per side for every market, frozen into each archived row. Spread, slippage and
  currency exchange are not modelled in the bot; a per-market cost model is
  planned (P1-1), not built.
- Keep old research results and output files. Add a rerun beside them (as
  section 10b of the findings does); never replace them.

## Data safeguards

Built into the code today. Keep them that way.

- **The original snapshot of a signal is immutable.** In `signal_archive` the
  first observation of a symbol and session wins (`archive_signal` inserts with
  `ON CONFLICT DO NOTHING`): the measurements, `qualified`, `failed_gate`,
  `rule_id`, `horizon_sessions`, `cost_bps_per_side`, `benchmark_symbol`,
  `recorded_at` and `entry_policy` are never updated. In `published_picks` the
  same holds for `published_at`, the horizon, cost and benchmark, and
  `snapshot_json` (rank, metrics, gates and the risk lines as sent). Never
  rewrite, backdate, backfill or delete any of these, and never give an old
  pick a risk snapshot it was not sent with.
- **Two lifecycle fields are filled in later, once each.** `rank` (the delivered
  order, `archive_ranks`) and `outcome_json` (settlement, `complete_archive_row`
  and `complete_pick`) are written only while still NULL. Recording a rank and
  settling an outcome are normal operation. Changing either after it is set is
  not.
- **Derived and recalculated results are stored beside the row, never over
  it.** The data-quality class of a signal lives in `signal_archive_quality`,
  insert-only per classifier version; a new classifier adds rows under a new
  version and `activate_classifier` moves the pointer. Recalculated research
  figures (a new cost model, a rerun) follow the same pattern: a new column,
  table or output file. `scripts/forward_test_integrity.py` compares the
  archive before and after a change and fails if a recorded signal or an
  existing outcome changed.
- **The forward test has two entry rules, and each row keeps its own.**
  - Rows with `entry_policy = 'after-recorded-v1'`
    (`ENTRY_POLICY_AFTER_RECORDED`, every row archived since the entry fix):
    entry is the first session after the signal date whose exchange open is
    strictly after `recorded_at`. Only exchanges in `newsbot/exchange_hours.py`
    are scored (US, `.OL`, `.DE`, `.PA`, `.AS`, `.BR`, `.MI`, `.MC`); any other
    is classed invalid and never settled.
  - Legacy rows with `entry_policy` NULL (archived before the fix): entry stays
    the first session after the signal date, whatever time the row was
    recorded. Some of these could enter at an open that had already happened.
    That is not hidden and not repaired: the classifier marks each one clean,
    potentially contaminated or invalid, and they are reported as a separate
    historical series, never mixed into the test.
  - Do not claim that every archived row satisfies the recording-time rule. Do
    not set `entry_policy` on a legacy row, re-settle it under the new rule, or
    change its class in place: its outcome was computed under the old rule and
    must keep that meaning.
- Risk text quotes gap measurements only for the **configured** stop multiple
  (`risk.GAP_STUDIES`, checked by a test against `docs/research/data/`). Any
  other multiple prints "not measured". Never borrow another stop's numbers.
- The LLM (Ollama, `news_feeds.py`) only ranks and de-duplicates headlines. It
  must never produce prices, metrics, scores or trading decisions.
- A changed signal definition (gates or hold) gets a new `rule_id`
  (`history_store.current_rule_id`). Old ids are never rewritten.

Rules for new changes:

- Schema changes are additive (`ADD COLUMN`, a new table) or a versioned
  migration, with a database backup first.
- The container's root filesystem is read-only. A new writable path goes under
  `/app/data`.

## Engineering

Use a virtualenv outside the repository (the session scratchpad), and point
`STATE_FILE` and `TRADE_HISTORY_DB_FILE` at scratch paths. These are the CI
commands (`.github/workflows/ci.yml`):

```bash
pip install -r requirements.txt ruff coverage
ruff check news_bot.py newsbot tests streamlit research scripts
python -W error::ResourceWarning -m coverage run -m unittest -v
python -m coverage report        # fails under the 95% floor in .coveragerc
```

- Production runs Python 3.15 (both Dockerfiles). CI tests 3.11 (the oldest
  supported, `ruff.toml`) and 3.14, then runs the suite again inside the built
  image on its own Python.
- When the Dockerfiles, requirements, entrypoint or compose file change, also
  run `docker compose config -q`, `docker compose --profile bundled-ollama
  config -q` and `docker build -t news-bot-ci .`.
- Tests never touch the network: mock yfinance, Finnhub, feeds, Telegram, Slack
  and Ollama. A test that reaches the trade-history store uses
  `tests/store_isolation.use_throwaway_store`.
- `FREEN_EWS_API_KEY` and `FINHUB_API_KEY` are misspelled but live aliases read
  by the code. Do not remove them without a decision from the owner.

## Deployment

- The bot runs under Docker Compose on the Pi. `scripts/deploy.sh` is the only
  deploy path; it backs up, rebuilds, checks health and prints a rollback.
- Where `scripts/install_autodeploy.sh` has been run, a systemd timer runs
  `deploy.sh` every 5 minutes, so **a merge to `main` deploys itself**. Treat a
  merge as a deploy.
- Ask the owner before restarting or reconfiguring anything on the Pi.

## Where things are

| What | Where |
|---|---|
| User documentation | `README.md` |
| Current snapshot | `PROJECT_MEMORY.md` |
| Research results | `docs/research/2026-10-analytics-findings.md`, scripts in `research/` |
| Audit, roadmap, open decisions | `docs/audit/` (`remediation-roadmap.md`, `decision-log.md`) |
| Settings and their defaults | `newsbot/config.py`, `.env.example` |
