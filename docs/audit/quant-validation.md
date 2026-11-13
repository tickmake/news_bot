# Quantitative validation

This document builds on `docs/research/2026-10-analytics-findings.md` and does not replace it. Evidence tags are the same as in `independent-audit.md`.

## Data correctness

| Area | Status | Evidence |
|---|---|---|
| Unfinished bar | OK | `indicators._drop_unfinished_bar` / `_drop_in_progress_bar`, per exchange time zone |
| Calendars, holidays, half days | Custom handling, not validated against a calendar library | **Unverified** |
| Split and dividend adjustment | Yahoo default; not checked across markets | **Unverified**. Total-return vs price-return comparisons against benchmarks such as OSEBX are not reconciled. |
| FX | Only a text mention in `risk.fee_line`. Returns are computed in local currency, which is correct per security. There is no NOK portfolio view of US and EU risk. | R |
| Survivorship | Present in all research and live backtests. Measured in research §5 (up to 26% of the 2015 S&P 500 cannot be priced). | H |
| Delisted securities, ticker changes | Not handled | R |

## Look-ahead

| Item | Status |
|---|---|
| Backtest entry | Next open after the signal close (`backtest.py`). Volume `shift(1)`. **OK** |
| Forward test | Next-open entry; first write wins; the rule and cost are stored per row. **OK** |
| Live sizing | Uses the signal close for both the stop and the size (`risk.size_position`). Not look-ahead, but it misstates the stop distance relative to the actual fill. |
| ESEF `known_from = min(added, FYE+120d)` | **Risk:** taking `min` back-dates a filing that was added late. It should be `added` when known. |
| Yahoo fundamentals | Guessed lag, and values restated in place (`score_inputs` `INSERT OR REPLACE`). Not point-in-time. |
| Normalisation | Scores rank within sector at each month end, using that date only. **OK** (R, §10) |

## Indicators (X)

| Indicator | Result |
|---|---|
| ATR% | Simple mean of 14 True Range values. Matches an independent calculation exactly. Wilder's ATR would be about 5% higher on the test series. |
| RSI-14 | Simple averages. Matches an independent calculation. Wilder's RSI differs by about 6 points on the test series. |
| ATR with fewer than 15 bars | Returns `None`, so the stock is skipped. |
| EMA20 | `ewm(span=20, adjust=False)`; standard. |

There are no defects here. Label the formulas so the numbers are not compared with TradingView or Nordnet values.

## Stop and sizing (live settings)

Live settings, from the Pi: 3×ATR stop, 2% risk per trade, 25% maximum position, 100% maximum total, 50,000 NOK account.

| ATR | Stop | Weight | Amount (NOK) | Stated risk | DNB round trip |
|---|---|---|---|---|---|
| 1.0% | 3.0% | 25% (cap) | 12,500 | 0.75% | 0.94% |
| 2.0% | 6.0% | 25% (cap) | 12,500 | 1.50% | 0.94% |
| 3.0% | 9.0% | 22.2% | 11,111 | 2.00% | 1.06% |
| 4.5% | 13.5% | 14.8% | 7,407 | 2.00% | 1.59% |

- **Gap through the stop** (2% ATR, stop 94, open 85): the realised loss is 3.75% of the account, against the stated 1.50%.
- **Next open 1% above the signal close:** the stop distance is really 6.93%, against the stated 6.0%.
- **Gap statistics:** the measured gap statistics are for 2×ATR only (research §7). Rerunning `research/gap_risk.py` with `STOP_ATR_MULTIPLE=3.0` is required before any 3×ATR claim.

## Costs

| Context | Cost used | Realistic |
|---|---|---|
| Backtest, forward test, picks, dashboard | 10 bps per side, all markets | DNB on 7–12.5k NOK positions: 47–80 bps per side, plus FX for USD/EUR, plus spread |
| Research §8–10 | 59 NOK / 12k NOK ≈ 49 bps per side; no FX, no spread | Closer |
| `/check`, `fee_line` | `max(59, 0.12%)`, FX as text only | Closest |

**Recommendation:** add a versioned cost model per market. Use three scenarios:

| Scenario | Fee | Spread / slippage | FX |
|---|---|---|---|
| Low | Nordnet Mini tier | 5 bps | 0.15% |
| Base | DNB | 10–20 bps by liquidity | DNB FX (**owner to confirm**) |
| Adverse | DNB | 2× spread | Plus slippage |

Keep the old 10 bps rows as they are. Show the recalculated figures alongside them.

## Statistical validity

- **Research:** a single 70/30 split, with holdout looked at once and t-values from non-overlapping samples. That is reasonable. However:
  - The 3,456-combination sweep is a multiple-testing setting. A Deflated Sharpe ratio or PBO is moot because nothing passed.
  - No per-regime split beyond year-by-year.
  - No portfolio-level drawdown, Sharpe or Sortino for the actual screen.
- **Forward test:** `MIN_PAIRED_DATES=60` counts overlapping dates.
  - At 20 sessions, only dates at least 20 sessions apart are independent. Alternatively, use a date-block bootstrap with blocks of at least 20 sessions.
  - There is no per-market minimum. US is 72% of rows.
- **Metrics:** the full metric list in the brief (CAGR, MDD, Sortino, profit factor, turnover, exposure) is not produced anywhere. Produce it only for the pre-registered protocol (see `decision-log.md` D4), not as a new search.

## Verdict

The research does not establish a repeatable edge. The project's own documents say so. The audit finds no reason to doubt that conclusion, and the survivorship bias would only make it worse. The forward test cannot yet change it:
- 0 settled outcomes at the live 20-session horizon;
- about 6 independent 5-session windows.
