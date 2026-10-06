"""How often does a 2xATR stop actually lose more than it says?

Issue #47 part 3. `risk.portfolio_lines` warns that "a gap can lose more" than
the volatility stop without saying how much more, which is a disclaimer the
reader cannot act on. This measures it on the evaluation basket: for every
session where a symbol qualified under the shipped gates, place the same stop
`size_position` would place, walk forward the usual hold, and compare the loss
actually taken with the loss the stop implies.

    python research/fetch_basket.py /tmp/basket.pkl
    python research/gap_risk.py /tmp/basket.pkl
"""
import pickle
import sys

import numpy as np
import pandas as pd

STOP_ATR_MULTIPLE = 2.0      # SETTINGS.risk_stop_atr_multiple
HOLD = 20                    # SETTINGS.trade_outcome_lookback_days
GATES = {"week": 1.0, "day": 0.0, "atr": 4.5, "dd": 8.0}


def main(basket_path):
    with open(basket_path, "rb") as handle:
        data, _meta = pickle.load(handle)  # noqa: S301 - written by fetch_basket.py
    symbols = [s for s in data if s != "SPY"]

    rows = []
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

        values = bars.values
        columns = {name: i for i, name in enumerate(bars.columns)}
        for position in np.flatnonzero(qualifies.values):
            entry = position + 1
            if entry + HOLD >= len(bars) or not np.isfinite(atr_pct.iloc[position]):
                continue
            entry_price = values[entry, columns["Open"]]
            stop_pct = STOP_ATR_MULTIPLE * atr_pct.iloc[position]
            stop_price = entry_price * (1 - stop_pct / 100)

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

            rows.append({"symbol": symbol, "stopped": stopped, "gapped": gapped,
                         "intended_loss_pct": stop_pct,
                         "realised_pct": (exit_price / entry_price - 1) * 100})

    trades = pd.DataFrame(rows)
    stopped = trades[trades.stopped]
    gapped = trades[trades.gapped]
    print(f"qualifying entries      : {len(trades)}")
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


if __name__ == "__main__":
    main(*sys.argv[1:])
