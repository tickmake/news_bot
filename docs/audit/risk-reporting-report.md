# Risk calculation and reporting: audit, fix and report

Report date: 2026-11-13. Branch `fix/risk-accounting`, from `main` at `7a0ef92`. Closes #161.
**Not deployed to the Pi.** No production data was read or changed, apart from a read-only look at the Pi's non-secret settings (section 1).

This is a risk-accounting change. It does not change which stocks are picked, how they are ranked, how big the suggested position is, any gate, any setting, or any past result. It makes no claim about profit.

## 1. Starting point

| Check | Result |
|---|---|
| Branch and HEAD | Was on `fix/risk-reporting` at `7a0ef92`, equal to `origin/main`, with one uncommitted file (`research/gap_risk.py`). New branch `fix/risk-accounting` made from there; the uncommitted work is carried and finished here. |
| PR #159 (forward-test fix) | Merged 2026-11-13 14:34 UTC as `7a0ef92`. Present: `ENTRY_POLICY_AFTER_RECORDED` and the `signal_archive_evaluated` view are in `history_store.py`. |
| Open PRs | None. |
| Pi | `7a0ef92`. `.env` (read-only, non-secret keys): `RISK_STOP_ATR_MULTIPLE=3.0`, `RISK_PER_TRADE_PCT=2.0`, `RISK_MAX_POSITION_PCT=25`, `RISK_MAX_TOTAL_PCT=100`, `RISK_ACCOUNT_SIZE=50000`, `TRADE_OUTCOME_LOOKBACK_DAYS=20`. |

## 2. What the code actually does (before this change)

The path from a signal to a risk line:

1. `indicators._compute_trade_metrics` measures a stock from finished daily bars. **ATR%** is the plain average of the last 14 true ranges divided by the last close (not Wilder's smoothing).
2. `indicators._failed_gates` applies five gates. **Volume is not a gate**; it only orders the list and names the group a pick is shown under.
3. `risk.size_position` places the stop and sizes the position: `stop = close × (1 − multiple × ATR%)`, `weight = risk per trade ÷ stop distance`, capped at `RISK_MAX_POSITION_PCT`.
4. `risk.candidate_lines` and `risk.portfolio_lines` print it in `/recommendations` and `/analyze`; `trade_check.build_check` prints its own version in `/check`.
5. Nothing executes orders. Real trades are typed in with `/fill`. The forward test and the backtest hold for a fixed number of sessions and **use no stop at all**, so no recorded outcome depends on the stop.

Answers to the questions asked:

| Question | Answer |
|---|---|
| Stop anchor | The **close the signal was formed on** (`metrics.last_close`). Not the buy price, not the next open. |
| ATR multiple in production | **3.0**, from the Pi's `.env`. The code default and the `docker-compose.yml` default are 2.0; `.env.example` says 3.0. |
| The 2× / 3× discrepancy | Both are real. The research of section 7 measured a **2×ATR stop below the buy price**. The bot runs a **3×ATR stop below the signal close**. The footer printed the 2× numbers next to whatever multiple was set. |
| Does sizing use the same stop as the risk shown? | Yes in `/recommendations`. `/check` recomputed the loss itself from the same stop, for the amount typed. |
| Do Telegram and the dashboard agree? | The dashboard showed **no risk information at all**. |
| Gaps | A sentence only ("more if it gaps"), with numbers from a different stop. |
| Commission | Shown in NOK and as a share of the position, never of the account. |
| Spread, slippage, FX | Not modelled. FX is named ("+ currency exchange fee") without a number; the broker's FX rate is not known (decision D1). |
| Does a volume or liquidity check affect risk? | No. The 1.2× volume ratio orders and groups picks. The 100,000 shares/day floor exists only in the backtest. Sizing ignores both. |

## 3. Root causes

1. **The gap sentence was a constant.** `GAP_RISK_NOTE` held the section-7 numbers and was printed after any multiple. With 3×ATR it stated measurements of a stop that was never placed that way (wrong multiple, wrong anchor).
2. **The risk was computed in three places.** `candidate_lines`, `trade_check` and `fee_line` each did their own arithmetic and wording. Nothing made them agree, and the dashboard had no access to any of it (a separate image that cannot import the bot).
3. **Position, loss and cost were mixed in one line.** "size 25% of portfolio (capped; risks 1.50%) ≈ 12,500" put the exposure and the amount at risk side by side with no gap figure and no fee share.
4. **`/analyze` showed two stops.** Its sketch measured "invalidation" to the 20-day low while the risk line below it placed the volatility stop.
5. **The risk a pick was sent with was not saved.** `published_picks.snapshot_json` held the metrics and the gates but not the stop multiple, the risk budget or the account size, so the risk of a sent pick cannot be rebuilt later.

## 4. Gap risk, measured for the stop in use

`research/gap_risk.py` now takes the multiple and the anchor. Run on the 44-stock basket, ten years to 2026-11-13, five gates, 20-session hold (full table in `docs/research/2026-10-analytics-findings.md`, section 11):

| Stop | Entries | Stopped | Gapped (of stops) | Extra loss when gapped: mean / 90th / worst |
|---|---|---|---|---|
| 2×ATR below the buy price (section 7, rerun) | 26,583 | 46.5% | 14.0% | 0.93 / 2.28 / 10.82 pp |
| 2×ATR below the signal close | 26,541 | 45.9% | 14.3% | 0.95 / 2.25 / 11.22 pp |
| **3×ATR below the signal close (live)** | 26,572 | 28.9% | 13.7% | **1.02 / 2.55 / 11.94 pp** |

The rerun reproduces section 7, so the script change did not move the old result. "About 1 stop in 7 gaps" turns out to hold for the live stop too; the size of the extra loss is what differs.

## 5. The risk model, before and after

One function, `risk.trade_risk`, now works out every number once. Example: close 100, ATR 2%, the Pi's settings.

| | Before | After |
|---|---|---|
| Reference price | not stated | **100.00 close** of the signal session, named as the anchor |
| Stop | 94.00 (6.0% away) | 94.00, **3×ATR below the 100.00 close: 6.00 (6.0%) away · ATR 2.00 (2.0%)** |
| Position exposure | "size 25% of portfolio … ≈ 12,500" | its own line: **25% of the portfolio ≈ 12,500 NOK (capped at 25%)** |
| Theoretical stop risk | "(capped; risks 1.50%)" | **1.50% of the portfolio ≈ 750 NOK, before costs** |
| Gap-adjusted risk | a sentence with 2× numbers | **about 2.14% ≈ 1,069 NOK (estimate, not a maximum)**: stop distance + the 90th-percentile extra loss for this stop. "not measured for a N×ATR stop" when there is no measurement |
| Transaction costs | 118 NOK (0.9% of 12,500 NOK) | 118 NOK (0.9% of 12,500 NOK, **0.24% of 50,000 NOK**), labelled an estimate; spread, slippage and FX named as not included |
| Actual costs | `/fill` fees on the dashboard | unchanged, now captioned as paid, not estimated |
| Liquidity | none | **⚠️ Thin trading** under the backtest's 100,000 shares/day floor; **No volume data** when there is none |
| Sizing | risk ÷ stop distance, capped | **unchanged** |

A real gap can be worse than the estimate. The audit's example (stop 94, open at 85) loses 1,875 NOK, 3.75% of the account, against an estimate of 1,069 NOK. The line says "estimate, not a maximum" and the footer gives the worst measured case (11.9 points past the stop).

### Where it shows

- **`/recommendations`** per pick: stop line, size line, loss line, fee line, warnings. Footer: the multiple, the anchor, the account size used, the gap sentence for this stop, and what is not included.
- **`/analyze`**: the same lines; the sketch now uses the same stop.
- **`/check TICKER AMOUNT`**: the same stop and loss lines for the amount typed, with shares of "your money"; the fee line adds the share of the account.
- **Dashboard, Sent picks page**: a new table, "Risk when sent". One row per sent pick: close, stop, stop distance (price and %), ATR % and multiple, position (NOK and % of account), loss at the stop, loss in a gap, fees, how the gap figure and the costs were arrived at, trading warnings. Screenshot with made-up rows: `docs/audit/img/risk-when-sent.png`.

The dashboard does not calculate. A published pick now saves its risk lines (`snapshot_json` → `risk`, with `model_version` 1 and `stop_anchor`), and the page shows that record. Picks sent before this change have no record. They show **"not recorded"** and stay that way: the settings they were sized with were never saved, so any number would be a guess.

## 6. Files changed

| File | Change |
|---|---|
| `newsbot/risk.py` | `GapStudy`, `GAP_STUDIES`, `gap_study`, `gap_note` replace `GAP_RISK_NOTE`. New `TradeRisk`, `trade_risk`, `pick_risk`, `snapshot`, `round_trip_fee`, `liquidity_warnings` and the line renderers. `size_position` gives the same result as before. |
| `newsbot/trade_check.py` | `/check` uses `trade_risk` and the shared renderers. |
| `newsbot/reports.py` | `/analyze` builds the risk context before the sketch, so one stop is shown. |
| `newsbot/history_store.py` | `publish` adds `risk` to `snapshot_json` for new picks. |
| `newsbot/indicators.py` | `TradeMetrics.avg_volume_20` (a measured fact; no gate reads it). |
| `newsbot/screener.py` | A stale "2xATR" docstring. |
| `streamlit/archive_views.py`, `streamlit/app.py` | "Risk when sent" table; a caption that `/fill` fees are actual. |
| `research/gap_risk.py` | Multiple and anchor as arguments; entries that open at or below the stop are counted, not traded; prints the row for `GAP_STUDIES`. |
| `tests/test_risk_reporting.py` (new), `tests/test_risk.py`, `tests/test_trade_check.py` | 42 new tests; four pinned strings updated. |
| `README.md`, `PROJECT_MEMORY.md`, `research/README.md`, `docs/research/…findings.md` (section 11), `docs/audit/decision-log.md` (a dated note under D2) | Documentation. |

**Database changes: none.** No table, column, index or view is added or altered. The only new data is one key, `risk`, inside the JSON of `published_picks.snapshot_json`, on rows written from now on. Existing rows are not touched (`ON CONFLICT … DO NOTHING`, as before).

## 7. Tests run and their results

Python 3.15.2, a virtualenv in the session scratch folder.

| Command | Result |
|---|---|
| `python -W error::ResourceWarning -m unittest` on `main` (`7a0ef92`) | 1,021 tests, OK |
| The same on this branch | **1,063 tests, OK** (42 new) |
| `ruff check news_bot.py newsbot tests` (CI's scope) | All checks passed |
| `docker build ./streamlit` | Built; `archive_views` imports inside the image |
| Streamlit `AppTest` on the Sent picks page against a demo database | No exceptions; three recorded rows, two "not recorded" |
| The page in headless Chrome | Renders; screenshot saved |
| `research/gap_risk.py`, five runs | Section 4 |

Both test runs end with one "unclosed database" message printed while the interpreter shuts down. It is on `main` too and does not fail the run.

`ruff check streamlit` reports 7 findings in `app.py`. All 7 are on `main` already, in lines this change does not touch; CI does not lint that folder.

Two guarantees were checked by breaking them on purpose: letting `gap_study` return a row for any multiple fails 6 tests; letting a republish overwrite a pick fails 2.

What the new tests cover: the configured multiple and the anchor; the stop price; sizing against the displayed loss; long only (the bot has no short trades, so there is no short calculation to test); a gap through the stop, including a real gap worse than the estimate; a multiple that was never measured; fees, FX and missing amounts; missing or invalid ATR, close and multiple; thin trading and missing volume, and that a warning never changes a gate or a size; `/check` against `/recommendations`; the dashboard table against the Telegram lines; old rows byte for byte; a saved record not following later setting changes; no schema change and reopening twice.

## 8. Limitations and assumptions

1. **The stop is still measured from the signal close.** Buying above that close widens the real loss (by 0.07 points on average in the study, wider than stated in 57% of entries). Every line now says so. Moving the anchor to the buy price is #172 and changes what the bot advises, so it is not done here.
2. **The gap figure is an estimate.** It comes from 44 large stocks that still exist, in a rising decade, and uses the 90th percentile of gaps that happened. Smaller or failing companies gap harder. It is never called a maximum.
3. **Costs are estimates.** The fee is DNB's table (0.12%, 59 NOK minimum). The currency exchange cost is not known (decision D1) and is named, not added. Spread and slippage are not modelled: the bot has no bid/ask data.
4. **Two account sizes exist.** `/recommendations` uses `RISK_ACCOUNT_SIZE`; `/check` uses deposits and fills when there are any. Each now prints the figure it used. They are not merged here.
5. **The thin-trading floor is the backtest's**, 100,000 shares a day, the same for every market and price. It is a warning, not a measure of how much a stop slips.
6. **Only volume-gate-off rows are in the table**, because volume is not a gate. Picks listed first are the volume-confirmed ones; their measured overshoot is slightly larger (2.96 against 2.55 at the 90th percentile).
7. **`.env.example`, `docker-compose.yml` and the code disagree on the defaults** (3.0 / 2.0 / 2.0). Not changed: that is #174, and the Pi's `.env` decides what runs.
8. **Portfolio totals** ("at risk if every stop is hit") are still theoretical stop risk; they are not gap- or fee-adjusted.

## 9. Deployment and rollback

Not deployed. When the owner decides to:

**Deploy**
1. Merge the PR. Back up the database first (`scripts/pull_backups.sh`, or wait for the nightly snapshot).
2. On the Pi: `scripts/deploy.sh`, which rebuilds both images (`docker compose up -d --build`). No migration runs; no setting changes.
3. Check: `/check EQNR.OL 12000` shows a "3×ATR below the … close" line and an "if it gaps through" figure; the next `/recommendations` shows the four lines per pick; the dashboard's Sent picks page shows "Risk when sent", with older picks "not recorded".

**Roll back**
1. `git revert` the merge commit and deploy again, or check out `7a0ef92` on the Pi and rebuild.
2. No data repair is needed. Picks published while this version ran keep a `risk` key in their JSON; the old code never reads it, and the old dashboard ignores it.

**Risk of the deploy itself:** each pick is two lines longer (three with a warning), so a long screen can split into one more Telegram part.
