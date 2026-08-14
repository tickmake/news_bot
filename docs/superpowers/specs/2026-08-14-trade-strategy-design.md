# Short-Term Trade Candidates — Scoring Rework

Date: 2026-08-14
Status: Approved, pending implementation plan

## Problem

`get_trade_candidates` screens symbols with a six-criterion additive score and
admits anything reaching `TRADE_MIN_SCORE` (default 3). The six criteria mix two
unrelated roles — three describe momentum, three describe risk — so any three can
substitute for any other three.

The consequence is that a stock with no momentum at all is admitted on its risk
criteria alone. Verified by running the real code against synthetic history:

| Case | 1D | 5D | Score | Admitted |
|---|---|---|---|---|
| Steady decline + volume spike | −0.05% | −0.26% | 3 | yes |
| Completely flat | 0.00% | 0.00% | 3 | yes |
| Genuine uptrend | +0.17% | +0.85% | 4 | yes |

Low ATR and low drawdown are *automatically* satisfied by stocks that do not
move, so the screen is structurally biased toward the names least likely to
produce the momentum it claims to find.

Three further defects were confirmed by direct execution:

1. **Partial-bar contamination.** `volume_ratio` divides an in-progress daily
   bar by a 20-day average of completed sessions (`news_bot.py:1769`). The
   briefing runs at 07:00 and 19:00 Europe/Oslo; at 19:00 NYSE is mid-session,
   at 07:00 India's NSE is. Both runs compare partial volume against full days,
   for a different set of markets each time. `day_change_pct` is affected the
   same way.

2. **The configured watchlist is never analysed.** `_build_live_universe(limit=45)`
   returns ~67 entries; the `USA_/INDIA_/NORWAY_STOCK_UNIVERSE` and mutual-fund
   entries are appended *after* it (`news_bot.py:1818`), then
   `list(universe.items())[:30]` truncates. With screeners healthy, zero
   configured symbols survive. Probe result: 30 symbols analysed, none from the
   watchlist.

3. **The deadline check discards completed work.** `news_bot.py:1837` tests the
   deadline at the top of the `as_completed` loop and breaks before calling
   `future.result()`, throwing away a result that had already finished.

`_analyze_short_term_candidate` and `_compute_atr_percent` have no direct test
coverage. All three tests touching trade candidates mock `_analyze_short_term_candidate`
out entirely, so none of this logic has ever been executed by a test.

## Decisions

| # | Decision | Choice |
|---|---|---|
| 1 | What the screen selects for | Momentum continuation |
| 2 | Partial daily bars | Use last complete session only |
| 3 | Universe composition | Watchlist first, screener fills remainder |
| 4 | Screener role | Discovery only; contributes no signal |
| 5 | Behaviour when nothing qualifies | Report it, with diagnostic context |

## Design

### Candidate evaluation

`_analyze_short_term_candidate` currently returns `None` both for "could not
compute metrics" and "computed fine, did not qualify". That conflation makes the
decision-5 diagnostic impossible, because the caller cannot distinguish a symbol
with no data from one that failed a gate. It splits into three pure units:

```python
@dataclass(frozen=True)
class TradeMetrics:
    symbol: str
    last_close: float
    day_change_pct: float
    week_momentum_pct: float
    volume_ratio: float
    drawdown_pct: float
    atr_pct: float
    above_ema20: bool

_compute_trade_metrics(symbol) -> Optional[TradeMetrics]   # None == no usable data, only
_failed_gates(metrics)         -> List[str]                # [] == qualifies
_rank_key(metrics)             -> tuple                    # ordering only
```

#### Gates

All mandatory. Evaluated in this order so a symbol attributes to its first
meaningful blocker.

| Gate | Condition | Setting |
|---|---|---|
| `trend` | `last_close > ema20` | — |
| `momentum_5d` | `week_momentum_pct >= …` | `TRADE_MIN_WEEK_MOMENTUM_PCT` |
| `momentum_1d` | `day_change_pct > …` | `TRADE_MIN_DAY_CHANGE_PCT` |
| `volatility` | `atr_pct <= …` | `TRADE_MAX_ATR_PCT` |
| `drawdown` | `drawdown_pct <= …` | `TRADE_MAX_DRAWDOWN_PCT` |

Volume does not gate. It confirms a move but its absence is not disqualifying,
so it ranks instead.

#### Ranking

Survivors sort by `(-week_momentum_pct, -volume_ratio, atr_pct, symbol)`. Symbol
is last so ties resolve deterministically — same input, same output.

No weighted composite. Choosing one set of weights over another requires
backtesting, which this codebase has no harness for; arbitrary weights presented
as precision are worse than an explicit ordinal rule.

#### Completed-session rule

Drop the final bar when its date equals today in the index's own timezone.
yfinance returns a tz-aware `DatetimeIndex` in the exchange's timezone, so this
needs no trading calendars and no holiday table.

Deliberately conservative: after a market closes, that session is still
discarded and the prior one used.

A lighter alternative was considered and rejected — a timezone→close-time table,
so a market that has already closed today keeps today's bar. It is cheaper than
it sounds, since no holiday calendar is required (a holiday means no bar exists
for today at all), and it fails safe on half-days.

It is rejected because this is a **ranked list**. Under a close-time table the
19:00 run would place Norwegian and Indian rows on today's session while US rows
sit on yesterday's, then rank them against each other by 5D momentum — comparing
windows offset by a day. Mixed vintages across rows in a ranked comparison is
the same class of error as the partial-bar defect, and harder to notice. The
simple rule keeps every row on "last session strictly before today".

Residual variance remains: exchanges have different holiday calendars, so the
prior session may be a different date per symbol. The per-row `Session` column
makes that visible rather than hiding it behind a single header date.

Where the cost lands:

| Run | US | Norway | India | Freshness lost |
|---|---|---|---|---|
| 07:00 Oslo | prior session | prior session | prior session | none |
| 19:00 Oslo | prior session | today's discarded | today's discarded | one session |

At 07:00 Europe/Oslo, "last session before today" *is* the latest complete
session for every market in the universe — US and Norway have not opened, India
is mid-session — so the rule costs nothing on the morning run. Only the evening
run gives up a session, and only for Norway and India.

### Universe composition

```python
_build_candidate_universe(max_symbols) -> List[Tuple[str, str]]
    # 1. configured watchlist, in declaration order, deduped by symbol
    # 2. screener discovery fills remaining slots
```

Watchlist entries keep their `[USA Stock]` / `[India Fund]` labels. Dedup is by
symbol, watchlist label winning, so a name in both appears once tagged as the
user's.

- **Watchlist over budget** → truncated *and* logged as a warning. Today the
  opposite side is dropped, silently; whichever loses, it must be reported.
- **Watchlist fills budget** → screener not called at all.
- **Residual discovery bias** → screener names remain ordered by today's live
  change, so which names get a slot is still influenced by today's move, even
  though nothing about passing is. Decision 4 accepts this; watchlist-first
  shrinks it, since configured symbols are never subject to it.
- **Mutual funds** carry no meaningful volume in yfinance, so `volume_ratio`
  falls back to `1.0` and they lose every volume tiebreak against equities. This
  is correct for NAV instruments but should be documented rather than implicit.

### Output

```
⚠️ Short-Term Trade Candidates (Informational Only):
Not investment advice. No guarantee of profit. Use strict risk management.
Gates: trend›EMA20, 5D≥1.0%, 1D›0.0%, DD≤8.0%, ATR≤4.5%
Metrics use each symbol's last completed session.
```

Columns: `#, Name, Symbol, Session, Close, 1D, 5D, Volx, DD, ATR`. The `Score`
column is removed with the score. `Volx` gains a `✓` when it clears
`TRADE_MIN_VOLUME_RATIO`.

`Session` is per-row, not a header line. The universe spans USA, India, and
Norway exchanges, whose last completed sessions genuinely differ — and differ
again depending on whether the 07:00 or 19:00 run is firing. A single header
date would be wrong for some rows in the same table. Column count stays at ten,
since `Score` left as `Session` arrived.

The disclaimer changes. "No guarantee of 1-2% daily profit" implies a 1–2% daily
target that is merely not guaranteed — a profit claim inside a disclaimer. The
replacement warns without asserting a target.

### Diagnostics

Shown on every run, not only empty ones. Because gates evaluate in order, each
symbol attributes to its first failing gate and the counts partition cleanly.

```
Checked 30 · no data 4 · qualified 2
Failed: trend 18, momentum_5d 4, volatility 1, drawdown 1
```

Gates with a zero count are omitted from the `Failed:` line, which is why
`momentum_1d` does not appear above. The counts satisfy
`checked − no_data = qualified + sum(failed)` — 30 − 4 = 2 + 24 — so a line that
does not balance indicates a bug in the attribution.

This is what makes threshold tuning possible without reading container logs,
which is the only way to see it today. When the deadline truncates a run it says
`analysed 22 of 30` rather than silently reporting a smaller universe.

### Error handling

`_compute_trade_metrics` returns `None` only for genuine data problems — fewer
than 30 usable bars after dropping the partial, zero `prev_close`, uncomputable
ATR — each logged at debug with symbol and reason, and counted as `no data`.
yfinance failures keep their existing `_with_retry` behaviour. The deadline
check moves after the result is consumed.

### Configuration

| Setting | Change |
|---|---|
| `TRADE_MIN_SCORE` | Deprecated. Field retained so existing `.env` files load; startup warning if set to a non-default value; README marked deprecated. |
| `TRADE_MIN_VOLUME_RATIO` | Retained, meaning narrowed to the `✓` confirmation threshold. |
| All other `TRADE_*` | Unchanged. |

Silently ignoring `TRADE_MIN_SCORE` would repeat the exact failure mode found in
defect 2.

## Testing

Built test-first. The two probe scripts written during analysis seed the first
two groups; both are proven to reproduce the real defects.

**`_failed_gates`**
- declining stock rejected
- flat stock rejected
- volume spike alone cannot admit — direct regression for the primary defect
- qualifying stock returns `[]`

**`_compute_trade_metrics`**
- partial bar dropped when index date equals today
- `None` below 30 usable bars
- each metric correct on a fixed fixture

**`_build_candidate_universe`**
- watchlist survives healthy screeners — regression for defect 2
- over-cap watchlist truncated and warned
- screener not called when watchlist fills budget
- dedup by symbol, watchlist label wins

**`_rank_key`**
- orders by 5D, then volume, then ATR, then symbol
- deterministic on ties

**Rendering**
- diagnostic counts attribute to first failing gate
- no `Score` column
- session date present

## Scope boundaries

Verification is limited to *behavioural correctness* — that the screen does what
it claims. Whether any threshold set is profitable is not established here and
cannot be, absent a backtest harness. Building one against yfinance daily bars
is a separate and much larger project.

This design is not investment advice.

## Out of scope

- Backtesting harness
- Pullback / mean-reversion setups (decision 1 selected continuation only)
- The `feeds.apnews.com` DNS failure seen in briefing logs
- LLM ranker returning unusable selections for `global_news` and `business_news`
