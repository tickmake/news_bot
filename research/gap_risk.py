"""How often does an N x ATR stop actually lose more than it says?

Issue #47 part 3. `risk.portfolio_lines` warns that "a gap can lose more" than
the volatility stop without saying how much more, which is a disclaimer the
reader cannot act on. This measures it on the evaluation basket: for every
session where a symbol qualified under the shipped gates, place the same stop
`size_position` would place, walk forward the usual hold, and compare the loss
actually taken with the loss the stop implies.

    python research/fetch_basket.py /tmp/basket.pkl
    python research/gap_risk.py /tmp/basket.pkl                      # 2xATR from the entry open (section 7)
    python research/gap_risk.py /tmp/basket.pkl 3.0 signal-close     # the stop the bot places (section 11)
    python research/gap_risk.py /tmp/basket.pkl 3.0 signal-close --volume-gate

The stop multiple and its anchor are arguments (#161). `signal-close` places the
stop below the close the signal was formed on, which is how `risk.size_position`
places it; `entry-open` places it below the next open, i.e. the buy price.

The live screen admits on five gates and volume is not one of them
(`indicators._failed_gates`); volume only decides which picks are listed first.
So the run without `--volume-gate` is the one that matches what qualifies.
`--volume-gate` keeps only sessions with last volume / prior 20-session mean
>= 1.2, the "confirmed by volume" group, as a check that the answer holds there.

The last line printed is the row to paste into `risk.GAP_STUDIES`.
"""
import pickle
import sys

import numpy as np
import pandas as pd

HOLD = 20                    # SETTINGS.trade_outcome_lookback_days
GATES = {"week": 1.0, "day": 0.0, "atr": 4.5, "dd": 8.0, "volume": 1.2}
ANCHORS = ("entry-open", "signal-close")


def main(basket_path, multiple="2.0", anchor="entry-open", *flags):
    STOP_ATR_MULTIPLE = float(multiple)
    if anchor not in ANCHORS:
        raise SystemExit(f"anchor must be one of {ANCHORS}")
    volume_gate = "--volume-gate" in flags
    print(f"stop {STOP_ATR_MULTIPLE:g}xATR below the {anchor}, volume gate {'on' if volume_gate else 'off'}, hold {HOLD}")
    with open(basket_path, "rb") as handle:
        data, _meta = pickle.load(handle)  # noqa: S301 - written by fetch_basket.py
    symbols = [s for s in data if s != "SPY"]

    rows, no_trade = [], 0
    for symbol in symbols:
        bars = data[symbol].copy()
        bars.index = pd.to_datetime(bars.index.strftime("%Y-%m-%d"))
        bars = bars[~bars.index.duplicated()]
        close, high, low = bars["Close"], bars["High"], bars["Low"]

        ema20 = close.ewm(span=20, adjust=False).mean()
        true_range = pd.concat([high - low, (high - close.shift(1)).abs(),
                                (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr_pct = true_range.rolling(14).mean() / close * 100
        rolling_high = close.rolling(20).max()
        drawdown = (rolling_high - close) / rolling_high * 100
        week = (close / close.shift(5) - 1) * 100
        day = close.pct_change(fill_method=None) * 100

        qualifies = ((close > ema20) & (week >= GATES["week"]) & (day > GATES["day"])
                     & (atr_pct <= GATES["atr"]) & (drawdown <= GATES["dd"])).fillna(False)
        if volume_gate:
            volume_ratio = bars["Volume"] / bars["Volume"].shift(1).rolling(20).mean()
            qualifies &= (volume_ratio >= GATES["volume"]).fillna(False)

        values = bars.values
        columns = {name: i for i, name in enumerate(bars.columns)}
        for position in np.flatnonzero(qualifies.values):
            entry = position + 1
            if entry + HOLD >= len(bars) or not np.isfinite(atr_pct.iloc[position]):
                continue
            entry_price = values[entry, columns["Open"]]
            stop_pct = STOP_ATR_MULTIPLE * atr_pct.iloc[position]
            reference = entry_price if anchor == "entry-open" else values[position, columns["Close"]]
            stop_price = reference * (1 - stop_pct / 100)
            if entry_price <= stop_price:
                # Only with a signal-close anchor: the stock opened at or below the
                # stop, so there is nothing to buy above it. Counted, not traded.
                no_trade += 1
                continue

            # Walk the hold session by session, the way a stop order would be hit.
            exit_price, stopped, gapped = None, False, False
            for step in range(entry, entry + HOLD):
                session_open = values[step, columns["Open"]]
                session_low = values[step, columns["Low"]]
                if session_open <= stop_price:
                    # Opened below the stop: the fill is the open, not the stop.
                    exit_price, stopped, gapped = session_open, True, True
                    break
                if session_low <= stop_price:
                    exit_price, stopped = stop_price, True
                    break
            if exit_price is None:
                exit_price = values[entry + HOLD - 1, columns["Close"]]

            # The loss the stop implies from the buy price. With a signal-close anchor
            # the buy (next open) can sit above or below that close, so this differs
            # from stop_pct. The overshoot below is the loss beyond that, in percent
            # of the buy price.
            rows.append({"symbol": symbol, "stopped": stopped, "gapped": gapped,
                         "intended_loss_pct": (1 - stop_price / entry_price) * 100,
                         "stop_from_close_pct": stop_pct,
                         "realised_pct": (exit_price / entry_price - 1) * 100})

    trades = pd.DataFrame(rows)
    stopped = trades[trades.stopped]
    gapped = trades[trades.gapped]
    print(f"qualifying entries      : {len(trades)}"
          + (f" (plus {no_trade} that opened at or below the stop: not traded)" if no_trade else ""))
    print(f"stopped out             : {len(stopped)} ({100 * len(stopped) / len(trades):.1f}%)")
    print(f"  of those, gapped through: {len(gapped)} "
          f"({100 * len(gapped) / max(len(stopped), 1):.1f}% of stops, "
          f"{100 * len(gapped) / len(trades):.1f}% of entries)")
    if not gapped.empty:
        overshoot = -gapped.realised_pct - gapped.intended_loss_pct
        print("\nwhen it gapped through the stop, the extra loss beyond the stop:")
        print(f"  mean   {overshoot.mean():.2f} pp")
        print(f"  median {overshoot.median():.2f} pp")
        print(f"  90th   {overshoot.quantile(0.90):.2f} pp")
        print(f"  worst  {overshoot.max():.2f} pp")
        print(f"\n  intended loss at the stop, mean {gapped.intended_loss_pct.mean():.2f}%"
              f" -> realised mean {-gapped.realised_pct.mean():.2f}%"
              f" ({-gapped.realised_pct.mean() / gapped.intended_loss_pct.mean():.2f}x)")
    print(f"\nall stops: intended mean {stopped.intended_loss_pct.mean():.2f}%"
          f" -> realised mean {-stopped.realised_pct.mean():.2f}%")
    print(f"stop distance from the signal close: mean {trades.stop_from_close_pct.mean():.2f}%;"
          f" from the buy price: mean {trades.intended_loss_pct.mean():.2f}%"
          f" (wider than stated in {100 * (trades.intended_loss_pct > trades.stop_from_close_pct).mean():.0f}%"
          " of entries)")
    if not gapped.empty:
        print(f"\nGapStudy(multiple={STOP_ATR_MULTIPLE:g}, anchor={anchor.replace('-', '_')!r},"
              f" volume_gate={volume_gate}, entries={len(trades)},"
              f" stopped_pct={100 * len(stopped) / len(trades):.1f},"
              f" gapped_of_stops_pct={100 * len(gapped) / len(stopped):.1f},"
              f" overshoot_mean_pp={overshoot.mean():.2f}, overshoot_p90_pp={overshoot.quantile(0.90):.2f},"
              f" overshoot_worst_pp={overshoot.max():.2f})")


if __name__ == "__main__":
    main(*sys.argv[1:])
