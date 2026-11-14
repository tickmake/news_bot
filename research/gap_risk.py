"""How often does an N x ATR stop actually lose more than it says?

Issue #47 part 3, redone for #161. `risk.portfolio_lines` warns that a gap can
lose more than the volatility stop; this measures how much more on the
evaluation basket: for every session where a symbol passed the live screen's
gates, place the stop, buy at the next open, walk the hold forward the way a
stop order is hit, and record where the stop actually sold.

    python research/fetch_basket.py /tmp/basket.pkl
    python research/gap_risk.py /tmp/basket.pkl 3.0 signal-close     # the stop the bot places
    python research/gap_risk.py /tmp/basket.pkl 2.0 entry-open --full-history-ema   # section 7

Arguments: the stop multiple; the anchor (`signal-close` places the stop below
the close the signal was formed on, as `risk.size_position` does; `entry-open`
places it below the buy price); and flags:

  --volume-gate         also require last volume / prior 20-session mean >= 1.2.
                        Not a live gate (volume only orders the list), so only a
                        check on the "confirmed by volume" subset.
  --full-history-ema    seed EMA20 at the start of the ten years, as section 7 did.
                        By default it is seeded where the live screen seeds it: at
                        the start of the 3 months of bars it fetches.

What is measured, and in which units (#161 review): when a stop gaps, the sale
is at the open, below the stop price. That shortfall is recorded in percent of
the **stop price**: `(1 - sale / stop) * 100`. Unlike a loss measured from the
buy price, it does not depend on what anyone paid, so the bot can apply it
before a buy (`risk.trade_risk`). The old measure, points of the buy price
beyond the stop, is still printed for comparison with section 7.

The `GapStudy(...)` line is the row for `risk.GAP_STUDIES`. Keep the full output in
docs/research/data/ (tests/test_risk_reporting.py checks the two agree).

Costs (P2-5). This script has no commission, fee, spread or slippage: its
returns are price ratios in the stock's own currency. After the `GapStudy(...)`
line it prints what the two currency conversions of a NOK investor would add,
for the four illustrative scenarios of `fx_costs.py`. Everything above that
section is unchanged by it, and the 0 bps row is the result without it.
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # also when loaded by path, as the tests do
import fx_costs

HOLD = 20                    # SETTINGS.trade_outcome_lookback_days on the Pi
GATES = {"week": 1.0, "day": 0.0, "atr": 4.5, "dd": 8.0, "volume": 1.2}
ANCHORS = ("entry-open", "signal-close")
# indicators._compute_trade_metrics: market_data._fetch_ticker_history's default
# period is "3mo", about 63 sessions, and a symbol needs 30 closes to be measured.
SCREEN_SESSIONS = 63
MIN_CLOSES = 30


def screen_ema20(close, window=SCREEN_SESSIONS):
    """EMA20 (adjust=False) seeded at the first of the last `window` closes, at every session.

    That is what the live screen computes: `ewm(span=20, adjust=False)` over the
    3 months it fetched. The weights below are that recursion written out.
    """
    alpha = 2.0 / 21.0
    values = close.to_numpy(dtype=float)
    weights = alpha * (1.0 - alpha) ** np.arange(window)
    weights[-1] = (1.0 - alpha) ** (window - 1)
    ema = np.convolve(values, weights)[: len(values)]
    # Fewer bars than the window: seeded at the first bar, like a short history.
    head = close.iloc[: window - 1].ewm(span=20, adjust=False).mean().to_numpy()
    ema[: len(head)] = head
    return pd.Series(ema, index=close.index)


def qualifying(bars, volume_gate=False, full_history_ema=False):
    """(passes the five live gates, ATR %) for every session of one symbol's bars."""
    close, high, low = bars["Close"], bars["High"], bars["Low"]
    ema20 = (close.ewm(span=20, adjust=False).mean() if full_history_ema
             else screen_ema20(close))
    true_range = pd.concat([high - low, (high - close.shift(1)).abs(),
                            (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr_pct = true_range.rolling(14).mean() / close * 100
    rolling_high = close.rolling(20).max()
    drawdown = (rolling_high - close) / rolling_high * 100
    week = (close / close.shift(5) - 1) * 100
    day = close.pct_change(fill_method=None) * 100

    passes = ((close > ema20) & (week >= GATES["week"]) & (day > GATES["day"])
              & (atr_pct <= GATES["atr"]) & (drawdown <= GATES["dd"])).fillna(False)
    passes &= np.arange(len(close)) >= MIN_CLOSES - 1
    if volume_gate:
        volume_ratio = bars["Volume"] / bars["Volume"].shift(1).rolling(20).mean()
        passes &= (volume_ratio >= GATES["volume"]).fillna(False)
    return passes, atr_pct


def simulate(signal_close, atr_pct, multiple, anchor, opens, lows, closes):
    """One entry: buy at opens[0], hold len(opens) sessions behind the stop.

    None when the first open is already at or below the stop: there is nothing
    to buy above it, so it is counted as no trade.
    """
    entry = opens[0]
    stop_pct = multiple * atr_pct
    reference = entry if anchor == "entry-open" else signal_close
    stop = reference * (1 - stop_pct / 100)
    if entry <= stop:
        return None
    exit_price, stopped, gapped = closes[-1], False, False
    for session_open, session_low in zip(opens, lows, strict=True):
        if session_open <= stop:
            # Opened below the stop: the sale is at the open, not at the stop.
            exit_price, stopped, gapped = session_open, True, True
            break
        if session_low <= stop:
            exit_price, stopped = stop, True
            break
    return {
        "stopped": stopped, "gapped": gapped,
        "stop_from_reference_pct": stop_pct,
        "stop_from_entry_pct": (1 - stop / entry) * 100,
        "realised_pct": (exit_price / entry - 1) * 100,
        "below_stop_pct": (1 - exit_price / stop) * 100 if gapped else 0.0,
        "overshoot_from_entry_pp": (stop - exit_price) / entry * 100 if gapped else 0.0,
    }


def fx_sensitivity(trades):
    """The realised return of every entry under each FX scenario, as printed lines.

    `trades` needs `symbol`, `currency`, `realised_pct`, `stopped` and `gapped`.
    Gap frequency and the shortfall below the stop are not here: they are
    price ratios in the stock's own currency, which no conversion cost changes.
    """
    trades = fx_costs.add_fx_columns(trades.copy(), "realised_pct", "realised_pct")
    converted = trades["currency"].map(fx_costs.is_converted)
    lines = ["FX COST SENSITIVITY (illustrative; not verified broker pricing)"]
    lines += fx_costs.describe(trades)
    lines += [
        "This script models no commission, fee, spread or slippage, so 'gross' and 'after existing",
        "trading costs' are the same number here. Gap frequency and the sale's shortfall below the stop",
        "price are price ratios in the stock's own currency: FX cost does not change them.",
        "",
        "Mean per entry, in percent of the NOK put in (return from the buy price):",
        f"{'scenario':<20}{'bps/conv':>9}{'gross':>8}{'after costs':>12}{'FX cost':>9}"
        f"{'after FX':>10}{'vs 0 bps':>10}{'total FX cost':>15}",
    ]
    baseline = trades[fx_costs.column("after_fx", 0.0)].mean()
    for name, bps in fx_costs.FX_SCENARIOS:
        cost, after = trades[fx_costs.column("fx_cost", bps)], trades[fx_costs.column("after_fx", bps)]
        lines.append(f"{name:<20}{bps:>9g}{trades.realised_pct.mean():>8.2f}{trades.realised_pct.mean():>12.2f}"
                     f"{cost.mean():>9.3f}{after.mean():>10.2f}{after.mean() - baseline:>10.3f}"
                     f"{cost.sum() / 100:>15.1f}")
    lines += ["('total FX cost' is the sum over all entries, counted in whole positions.)", "",
              "The same for the charged (non-NOK) entries only, and what a stop then loses:",
              f"{'scenario':<20}{'bps/conv':>9}{'entries':>9}{'FX cost':>9}{'after FX':>10}"
              f"{'stopped: loss':>15}{'gapped: loss':>14}{'all stops, all entries':>24}"]
    foreign = trades[converted]
    for name, bps in fx_costs.FX_SCENARIOS:
        after = fx_costs.column("after_fx", bps)
        lines.append(f"{name:<20}{bps:>9g}{len(foreign):>9}{foreign[fx_costs.column('fx_cost', bps)].mean():>9.3f}"
                     f"{foreign[after].mean():>10.2f}{-foreign.loc[foreign.stopped, after].mean():>15.2f}"
                     f"{-foreign.loc[foreign.gapped, after].mean():>14.2f}"
                     f"{-trades.loc[trades.stopped, after].mean():>24.2f}")
    lines.append("('loss' is the mean realised loss from the buy price, positive = money lost; the last column")
    lines.append(" is the 'realised mean' of the 'all stops' line above, for every entry, NOK ones included.)")
    return lines


def main(basket_path, multiple="3.0", anchor="signal-close", *flags):
    stop_multiple = float(multiple)
    if anchor not in ANCHORS:
        raise SystemExit(f"anchor must be one of {ANCHORS}")
    volume_gate = "--volume-gate" in flags
    full_history_ema = "--full-history-ema" in flags
    seeded = "at the start of the history" if full_history_ema else f"{SCREEN_SESSIONS} sessions back"
    print(f"stop {stop_multiple:g}xATR below the {anchor}; volume gate {'on' if volume_gate else 'off'};"
          f" EMA20 seeded {seeded}; hold {HOLD}")
    with open(basket_path, "rb") as handle:
        data, _meta = pickle.load(handle)  # noqa: S301 - written by fetch_basket.py
    symbols = [s for s in data if s != "SPY"]
    last_day = max(data[s].index[-1] for s in symbols)
    print(f"basket: {len(symbols)} symbols, last session {last_day:%Y-%m-%d}")

    rows, no_trade = [], 0
    for symbol in symbols:
        bars = data[symbol].copy()
        bars.index = pd.to_datetime(bars.index.strftime("%Y-%m-%d"))
        bars = bars[~bars.index.duplicated()]
        passes, atr_pct = qualifying(bars, volume_gate, full_history_ema)
        opens, lows, closes = (bars[name].to_numpy(dtype=float) for name in ("Open", "Low", "Close"))
        for position in np.flatnonzero(passes.to_numpy()):
            entry = position + 1
            if entry + HOLD >= len(bars) or not np.isfinite(atr_pct.iloc[position]):
                continue
            window = slice(entry, entry + HOLD)
            trade = simulate(closes[position], atr_pct.iloc[position], stop_multiple, anchor,
                             opens[window], lows[window], closes[window])
            if trade is None:
                no_trade += 1
                continue
            rows.append({"symbol": symbol, "currency": fx_costs.currency_of(symbol), **trade})

    trades = pd.DataFrame(rows)
    stopped = trades[trades.stopped]
    gapped = trades[trades.gapped]
    print(f"qualifying entries      : {len(trades)}"
          + (f" (plus {no_trade} that opened at or below the stop: not traded)" if no_trade else ""))
    print(f"stopped out             : {len(stopped)} ({100 * len(stopped) / len(trades):.1f}%)")
    print(f"  of those, gapped through: {len(gapped)} "
          f"({100 * len(gapped) / max(len(stopped), 1):.1f}% of stops, "
          f"{100 * len(gapped) / len(trades):.1f}% of entries)")
    if gapped.empty:
        return
    below = gapped.below_stop_pct
    print("\nwhen it gapped, the sale was below the stop price by (percent of the stop price):")
    print(f"  mean {below.mean():.2f}%  median {below.median():.2f}%  90th {below.quantile(0.90):.2f}%"
          f"  worst {below.max():.2f}%")
    old = gapped.overshoot_from_entry_pp
    print("the same in points of the buy price (section 7's measure; depends on the buy price):")
    print(f"  mean {old.mean():.2f}  median {old.median():.2f}  90th {old.quantile(0.90):.2f}"
          f"  worst {old.max():.2f}")
    print(f"\nall stops: loss at the stop from the buy price, mean {stopped.stop_from_entry_pct.mean():.2f}%"
          f" -> realised mean {-stopped.realised_pct.mean():.2f}%")
    wider = (trades.stop_from_entry_pct > trades.stop_from_reference_pct).mean()
    print(f"stop distance from the {anchor}: mean {trades.stop_from_reference_pct.mean():.2f}%;"
          f" from the buy price: mean {trades.stop_from_entry_pct.mean():.2f}%"
          f" (wider from the buy price in {100 * wider:.0f}% of entries)")
    print(f"\nGapStudy(multiple={stop_multiple:.1f}, anchor={anchor.replace('-', '_')!r},"
          f" entries={len(trades)}, stopped_pct={100 * len(stopped) / len(trades):.1f},"
          f" gapped_of_stops_pct={100 * len(gapped) / len(stopped):.1f},"
          f" gap_below_stop_mean_pct={below.mean():.2f},"
          f" gap_below_stop_p90_pct={below.quantile(0.90):.2f},"
          f" gap_below_stop_worst_pct={below.max():.2f})")
    print()
    print("\n".join(fx_sensitivity(trades)))


if __name__ == "__main__":
    main(*sys.argv[1:])
