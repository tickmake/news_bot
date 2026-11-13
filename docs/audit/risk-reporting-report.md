# Risk calculation and reporting: audit, fix and report

Report date: 2026-11-13, corrected the same day after review (section 0). Branch `fix/risk-accounting`, from `main` at `7a0ef92`. Closes #161.
**Not deployed to the Pi.** No production data was read or changed, apart from a read-only look at the Pi's non-secret settings (section 1).

This is a risk-accounting change. It does not change which stocks are picked, how they are ranked, how big the suggested position is, any gate, any setting, or any past result. It makes no claim about profit.

## 0. Correction after review: one reference price per number

The branch's first commit (`9f64042`) computed the gap estimate as `stop distance + 90th-percentile overshoot`. The stop distance is measured from the **signal close**; the overshoot was measured in points of the **buy price** (the next open). Adding the two mixed reference prices. In the example below the difference is small (1,069 against 1,073 NOK), but the sum had no justification, and its error grows with the distance between the close and the buy price.

**Corrected formula.** The study now records how far below the **stop price** a gapped stop sold, in percent of that stop price. That shortfall does not depend on anyone's buy price, so it can be applied before a buy:

    stop       = signal close × (1 − multiple × ATR%)
    gap sale   = stop × (1 − shortfall at the 90th percentile)
    gap loss   = 1 − gap sale / signal close        % of the position, if bought at that close
    in NOK     = gap loss × position;  % of the account = in NOK / account size

Close 100, ATR 2%, 3×ATR: stop 94.00; gap sale 94 × (1 − 2.75%) = 91.42; loss 8.59% of a 12,500 NOK position ≈ **1,073 NOK, 2.15% of 50,000**.

**What can be shown before a buy, and what cannot.** Every loss figure is labelled as modelled **if bought at the signal close** (`loss_basis: "if_bought_at_reference_price"` in the saved record). The actual risk of a position depends on its buy price, and no signal has one: `/fill` records trades, but not against a pick. So Telegram says "your actual risk isn't known until you buy" and the dashboard's "Actual position risk" column says "not known: no buy price is linked to a sent pick". No entry price is invented and nothing post-entry is calculated. The stop anchor, sizing and settings are unchanged.

What the buy price does to the real loss (`BuyPriceTests`): stop 94, the stock opens at 90. The shortfall is 4.26% of the stop whatever was paid. The bot's figure is 10.0% (bought at the 100 close); the actual loss is 11.76% if bought at 102 and 8.16% if bought at 98.

**Research redone for the exact method.** `research/gap_risk.py` now screens sessions as the live screen does: EMA20 seeded at the start of the 3 months (about 63 sessions) the screen fetches, 30 closes required, the five gates, no volume gate, prices adjusted the same way (`auto_adjust=True`). A test checks its gates against `indicators._failed_gates` session by session. `GAP_STUDIES` holds only the two rows printed in `docs/research/data/gap_risk_2.0x_signal-close.txt` and `gap_risk_3.0x_signal-close.txt`; a test fails if they differ.

**Records.** The saved record is now `model_version` 2, with `stop_anchor`, `loss_basis`, `entry_price: null` and `gap_fill_price`. Version 1 never left this branch, so no production row has it; the dashboard shows numbers only for version 2 and lists any other version as "risk model vN: not shown". Picks sent before this change still show "not recorded". No record is recalculated.

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

`research/gap_risk.py` takes the multiple and the anchor and screens like the live screen (section 0). Run on the 44-stock basket, ten years to 2026-11-13, 20-session hold. The shortfall is how far below the stop price a gapped stop sold, in percent of the stop price (full table in `docs/research/2026-10-analytics-findings.md`, section 11):

| Stop | Entries | Stopped | Gapped (of stops) | Shortfall below the stop: mean / 90th / worst | Old measure, points of the buy price |
|---|---|---|---|---|---|
| 2×ATR below the buy price (section 7 method) | 26,448 | 46.7% | 14.0% | 0.98 / 2.37 / 11.62% | 0.93 / 2.26 / 10.82 |
| 2×ATR below the signal close | 26,407 | 46.0% | 14.3% | 0.99 / 2.36 / 12.04% | 0.94 / 2.24 / 11.22 |
| **3×ATR below the signal close (live)** | 26,438 | 29.0% | 13.7% | **1.09 / 2.75 / 12.75%** | 1.02 / 2.55 / 11.94 |

The section-7 method reproduces section 7 in its own units (0.93 / 2.26 / 10.82 against 0.93 / 2.29 / 10.82 then; there are fewer entries because 30 closes are now required). "About 1 stop in 7 gaps" holds for the live stop too. Entries that opened at or below the stop (11 for the live stop) are counted as no trade.

## 5. The risk model, before and after

One function, `risk.trade_risk`, now works out every number once. Example: close 100, ATR 2%, the Pi's settings.

| | Before | After |
|---|---|---|
| Reference price | not stated | **100.00 close** of the signal session, named as the anchor; every loss is modelled as if bought there |
| Stop | 94.00 (6.0% away) | 94.00, **3×ATR below the 100.00 close: 6.00 (6.0%) away · ATR 2.00 (2.0%)** |
| Position exposure | "size 25% of portfolio … ≈ 12,500" | its own line: **25% of the portfolio ≈ 12,500 NOK (capped at 25%)** |
| Theoretical stop risk | "(capped; risks 1.50%)" | **if bought at the 100.00 close: 1.50% of the portfolio ≈ 750 NOK at the stop, before costs** |
| Gap-adjusted risk | a sentence with 2× numbers | **sold near 91.42, about 2.15% ≈ 1,073 NOK from that close (estimate, not a maximum)**: the stop less the 90th-percentile shortfall below it (section 0). "not measured for a N×ATR stop" when there is no measurement |
| Actual position risk | not stated | **not known until you buy** (Telegram); **"not known: no buy price is linked to a sent pick"** (dashboard) |
| Transaction costs | 118 NOK (0.9% of 12,500 NOK) | 118 NOK (0.9% of 12,500 NOK, **0.24% of 50,000 NOK**), labelled an estimate; spread, slippage and FX named as not included |
| Actual costs | `/fill` fees on the dashboard | unchanged, now captioned as paid, not estimated |
| Liquidity | none | **⚠️ Thin trading** under the backtest's 100,000 shares/day floor; **No volume data** when there is none |
| Sizing | risk ÷ stop distance, capped | **unchanged** |

The lines as `/recommendations` prints them:

    risk: volatility stop 94.00, 3×ATR below the 100.00 signal close: 6.00 (6.0%) away · ATR 2.00 (2.0%)
    loss if bought at the 100.00 close: 1.50% of the portfolio ≈ 750 NOK at the stop, before costs; your actual risk isn't known until you buy
    if it gaps through: sold near 91.42, about 2.15% of the portfolio ≈ 1,073 NOK from that close (estimate, not a maximum)

A real gap can be worse than the estimate. The audit's example (stop 94, open at 85) sells 9.6% below the stop and loses 1,875 NOK, 3.75% of the account, against an estimate of 1,073 NOK. The worst measured case, 12.75% below the stop, would lose 2,250 NOK, 4.5%. The line says "estimate, not a maximum" and the footer gives the worst measured case.

### Where it shows

- **`/recommendations`** per pick: stop line, size line, loss line, fee line, warnings. Footer: the multiple, the anchor, the account size used, the gap sentence for this stop, and what is not included.
- **`/analyze`**: the same lines; the sketch now uses the same stop.
- **`/check TICKER AMOUNT`**: the same stop and loss lines for the amount typed, with shares of "your money"; the fee line adds the share of the account.
- **Dashboard, Sent picks page**: a new table, "Risk when sent". One row per sent pick: the reference (signal close), stop, stop distance (price and %), ATR % and multiple, position (NOK and % of account), loss at the stop and in a gap if bought at the close, the gap sale price, actual position risk ("not known"), fees, how the gap figure and the costs were arrived at, trading warnings. Screenshot with made-up rows: `docs/audit/img/risk-when-sent.png`.

The dashboard does not calculate. A published pick now saves its risk lines (`snapshot_json` → `risk`, with `model_version` 2, `stop_anchor`, `loss_basis` and `entry_price: null`), and the page shows that record. Picks sent before this change have no record. They show **"not recorded"** and stay that way: the settings they were sized with were never saved, so any number would be a guess.

## 6. Files changed

| File | Change |
|---|---|
| `newsbot/risk.py` | `GapStudy`, `GAP_STUDIES`, `gap_study`, `gap_note` replace `GAP_RISK_NOTE`. New `TradeRisk`, `trade_risk`, `pick_risk`, `snapshot`, `round_trip_fee`, `liquidity_warnings` and the line renderers. The gap sale is the stop less the measured shortfall; every loss is modelled at the signal close and labelled so. `size_position` gives the same result as before. |
| `newsbot/trade_check.py` | `/check` uses `trade_risk` and the shared renderers; its footer says the losses are from the session close, not the buy price. |
| `newsbot/reports.py` | `/analyze` builds the risk context before the sketch, so one stop is shown. |
| `newsbot/history_store.py` | `publish` adds `risk` to `snapshot_json` for new picks. |
| `newsbot/indicators.py` | `TradeMetrics.avg_volume_20` (a measured fact; no gate reads it). |
| `newsbot/screener.py` | A stale "2xATR" docstring. |
| `streamlit/archive_views.py`, `streamlit/app.py` | "Risk when sent" table (version-2 records only, an "Actual position risk" column); a caption that `/fill` fees are actual. |
| `research/gap_risk.py` | Multiple and anchor as arguments; screens like the live screen (EMA20 seeded 63 sessions back, 30 closes); records the shortfall in % of the stop price; entries that open at or below the stop are counted, not traded; prints the row for `GAP_STUDIES`. |
| `docs/research/data/gap_risk_2.0x_signal-close.txt`, `gap_risk_3.0x_signal-close.txt` (new) | The research output `GAP_STUDIES` is copied from and tested against. |
| `tests/test_risk_reporting.py` (new), `tests/test_risk.py`, `tests/test_trade_check.py` | 55 new tests; pinned strings updated. |
| `README.md`, `PROJECT_MEMORY.md`, `research/README.md`, `docs/research/…findings.md` (section 11), `docs/audit/decision-log.md` (a dated note under D2) | Documentation. |

**Database changes: none.** No table, column, index or view is added or altered. The only new data is one key, `risk`, inside the JSON of `published_picks.snapshot_json`, on rows written from now on. Existing rows are not touched (`ON CONFLICT … DO NOTHING`, as before).

## 7. Tests run and their results

Python 3.15.2, a virtualenv in the session scratch folder.

| Command | Result |
|---|---|
| `python -W error::ResourceWarning -m unittest` on `main` (`7a0ef92`) | 1,021 tests, OK |
| The same on this branch, after the correction | **1,076 tests, OK** (55 new) |
| `ruff check news_bot.py newsbot tests` (CI's scope) and `ruff check research/gap_risk.py` | All checks passed |
| `docker build ./streamlit` | Built; inside the image `archive_views` reports risk model 2 and 23 columns |
| Streamlit `AppTest` on the Sent picks page against a demo database | No exceptions; three recorded rows, two "not recorded"; "Actual position risk" reads "not known: no buy price is linked to a sent pick" |
| The page in headless Chrome | Renders; screenshot saved |
| `research/gap_risk.py`, four runs (2× and 3× below the signal close, the section-7 method, the volume subset) | Section 4; the two rows the bot uses are saved in `docs/research/data/` |

Both test runs end with one "unclosed database" message printed while the interpreter shuts down. It is on `main` too and does not fail the run.

`ruff check streamlit` reports 7 findings in `app.py`. All 7 are on `main` already, in lines this change does not touch; CI does not lint that folder.

Guarantees checked by breaking them on purpose, in a copy of the tree: letting `gap_study` return a row for any multiple fails 6 tests; letting a republish overwrite a pick fails 2; putting back the old formula (stop distance + overshoot) fails 5; measuring the shortfall from the buy price instead of the stop fails 1; seeding the study's EMA20 ten years back fails 1.

What the new tests cover: the configured multiple and the anchor; the stop price; sizing against the displayed loss; long only (the bot has no short trades, so there is no short calculation to test); a gap through the stop, including a real gap worse than the estimate; the shortfall staying the same for buy prices above and below the signal close while the actual loss changes; the study's gates against the live gates; `GAP_STUDIES` against the saved research output; a multiple that was never measured; fees, FX and missing amounts; missing or invalid ATR, close and multiple; thin trading and missing volume, and that a warning never changes a gate or a size; `/check` against `/recommendations`; the dashboard table against the Telegram lines; a record of another model version listed without numbers; old rows byte for byte; a saved record not following later setting changes; no schema change and reopening twice.

## 8. Limitations and assumptions

1. **The stop is still measured from the signal close.** Buying above that close widens the real loss (by 0.07 points on average in the study, wider than stated in 57% of entries). Every loss line now says it is modelled at the close. Moving the anchor to the buy price is #172 and changes what the bot advises, so it is not done here. *(Update: done for `/check` with a buy price by P1-3; see `stop-from-buy-price-report.md`.)*
2. **No actual position risk is shown.** A sent pick has no buy price linked to it (`/fill` records trades separately), so the actual risk is "not known" everywhere. Showing it would need fills linked to picks: a new feature, not a correction.
3. **The gap figure is an estimate.** It comes from 44 large stocks that still exist, in a rising decade, and uses the 90th percentile of gaps that happened. Smaller or failing companies gap harder. It is never called a maximum.
4. **The shortfall is one number for every stock.** It is pooled over the basket, so a volatile stock is given the same shortfall below its stop as a quiet one; the stop distance already scales with ATR, the shortfall does not.
5. **Costs are estimates.** The fee is DNB's table (0.12%, 59 NOK minimum). The currency exchange cost is not known (decision D1) and is named, not added. Spread and slippage are not modelled: the bot has no bid/ask data.
6. **Two account sizes exist.** `/recommendations` uses `RISK_ACCOUNT_SIZE`; `/check` uses deposits and fills when there are any. Each now prints the figure it used. They are not merged here.
7. **The thin-trading floor is the backtest's**, 100,000 shares a day, the same for every market and price. It is a warning, not a measure of how much a stop slips.
8. **Only volume-gate-off rows are in the table**, because volume is not a gate. Picks listed first are the volume-confirmed ones; their measured shortfall is slightly larger (3.21% against 2.75% below the stop at the 90th percentile).
9. **`.env.example`, `docker-compose.yml` and the code disagree on the defaults** (3.0 / 2.0 / 2.0). Not changed: that is #174, and the Pi's `.env` decides what runs.
10. **Portfolio totals** ("at risk if every stop is hit") are still theoretical stop risk; they are not gap- or fee-adjusted.

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
