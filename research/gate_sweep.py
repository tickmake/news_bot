"""Do the screen's four gate thresholds beat no gates at all?

Issue #47 part 1. The gates have shipped as plausible defaults since the start
and were never measured; `factor_study.py` tested them only at their current
settings. This sweeps each threshold over a grid, and also drops each gate in
turn, against the same yardstick the rest of the research uses: next-open
entry, exit at the close N sessions later, 10 bps per side, judged as excess
over the same-date average of every symbol with a usable forward return.

    python research/fetch_basket.py /tmp/basket.pkl
    python research/gate_sweep.py /tmp/basket.pkl [/tmp/gate_sweep.pkl]

Selection is vectorised, so the whole grid is a few seconds per hold length.
"""
import itertools
import math
import pickle
import sys

import numpy as np
import pandas as pd

COST = 2 * 10 / 1e4          # 10 bps per side, round trip
MIN_UNIVERSE = 20            # dates with fewer priced symbols are skipped
MIN_PICKS = 3                # a one-name "portfolio" is noise, not a screen

# The shipped defaults, from newsbot/config.py.
CURRENT = {"week": 1.0, "day": 0.0, "atr": 4.5, "dd": 8.0, "trend": True}
# OFF values are deliberately wide enough that the gate admits everything.
GRID = {
    "week": [-2.0, -1.0, 0.0, 0.5, 1.0, 2.0, 3.0, -99.0],
    "day": [-2.0, -1.0, 0.0, 0.5, 1.0, -99.0],
    "atr": [2.5, 3.5, 4.5, 6.0, 8.0, 99.0],
    "dd": [2.0, 4.0, 8.0, 12.0, 20.0, 99.0],
    "trend": [True, False],
}


def load_panel(path):
    with open(path, "rb") as handle:
        data, _meta = pickle.load(handle)  # noqa: S301 - written by fetch_basket.py
    symbols = [s for s in data if s != "SPY"]

    def norm(frame):
        frame = frame.copy()
        frame.index = pd.to_datetime(frame.index.strftime("%Y-%m-%d"))
        return frame[~frame.index.duplicated()]

    history = {s: norm(data[s]) for s in symbols}
    index = pd.DatetimeIndex(sorted(set().union(*(set(h.index) for h in history.values()))))
    panel = {field: pd.DataFrame({s: history[s][field] for s in symbols}).reindex(index)
             for field in ("Open", "Close", "High", "Low")}
    return symbols, index, panel, history


def gate_inputs(panel):
    """The five quantities the bot's gates are expressed in, as date x symbol."""
    close = panel["Close"].ffill(limit=3)
    high, low = panel["High"], panel["Low"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    true_range = pd.concat([(high - low).stack(),
                            (high - close.shift(1)).abs().stack(),
                            (low - close.shift(1)).abs().stack()], axis=1).max(axis=1).unstack()
    atr_pct = true_range.rolling(14).mean() / close * 100
    rolling_high = close.rolling(20).max()
    return {
        "above_ema20": close > ema20,
        "week_momentum_pct": (close / close.shift(5) - 1) * 100,
        "day_change_pct": close.pct_change(fill_method=None) * 100,
        "atr_pct": atr_pct,
        "drawdown_pct": (rolling_high - close) / rolling_high * 100,
    }


def selection_mask(inputs, week, day, atr, dd, trend):
    """The bot's `_failed_gates` logic, vectorised and with thresholds free."""
    mask = (inputs["week_momentum_pct"] >= week) & (inputs["day_change_pct"] > day) \
        & (inputs["atr_pct"] <= atr) & (inputs["drawdown_pct"] <= dd)
    if trend:
        mask &= inputs["above_ema20"]
    return mask.fillna(False)


def forward_returns(symbols, index, history, hold):
    """Enter at the next open, exit at the close `hold` bars later, net of cost."""
    out = pd.DataFrame(index=index, columns=symbols, dtype=float)
    for symbol in symbols:
        bars = history[symbol]
        positions = {d: i for i, d in enumerate(bars.index)}
        opens, closes = bars["Open"].values, bars["Close"].values
        values = []
        for date in index:
            i = positions.get(date)
            if i is None or i + hold >= len(bars):
                values.append(np.nan)
                continue
            entry = i + 1
            values.append(closes[entry + hold - 1] / opens[entry] - 1 - COST)
        out[symbol] = values
    return out


def evaluate(mask, forward, hold, lo, hi):
    """Mean excess of the selected names over the same-date universe average."""
    window = (forward.index >= lo) & (forward.index <= hi)
    fwd = forward.loc[window]
    sel = mask.reindex_like(fwd).fillna(False) & fwd.notna()
    priced = fwd.notna().sum(axis=1)
    picked = sel.sum(axis=1)
    usable = (priced >= MIN_UNIVERSE) & (picked >= MIN_PICKS)
    if not usable.any():
        return None
    fwd, sel, picked = fwd[usable], sel[usable], picked[usable]
    chosen = fwd.where(sel).sum(axis=1) / picked
    base = fwd.mean(axis=1)
    delta = chosen - base
    # Non-overlapping sample for the standard error: holds overlap otherwise.
    spaced = delta.iloc[::hold]
    se = spaced.std(ddof=1) / math.sqrt(len(spaced)) if len(spaced) > 2 else float("nan")
    return {"n": int(usable.sum()), "picks": float(picked.mean()),
            "sel": float(chosen.mean()) * 100, "base": float(base.mean()) * 100,
            "delta": float(delta.mean()) * 100,
            "t": float(spaced.mean() / se) if se and se == se and se > 0 else float("nan"),
            "better": float((delta > 0).mean()) * 100}


def main(basket_path, out_path=None):
    symbols, index, panel, history = load_panel(basket_path)
    inputs = gate_inputs(panel)
    split = int(len(index) * 0.7)
    periods = {"development": (index[260], index[split]),
               "holdout": (index[split + 1], index[-1])}
    print(f"{len(symbols)} symbols, {len(index)} sessions, "
          f"development {index[260].date()}..{index[split].date()}, "
          f"holdout {index[split + 1].date()}..{index[-1].date()}")

    rows = []
    for hold in (5, 10, 20):
        forward = forward_returns(symbols, index, history, hold)

        # No gates at all: hold every priced symbol. The yardstick.
        everything = pd.DataFrame(True, index=index, columns=symbols)
        for period, (lo, hi) in periods.items():
            result = evaluate(everything, forward, hold, lo, hi)
            if result:
                rows.append({"variant": "no_gates", "kind": "baseline", "hold": hold,
                             "period": period, **CURRENT, **result})

        # One threshold moved at a time, the rest left at the shipped value.
        for name, values in GRID.items():
            for value in values:
                settings = {**CURRENT, name: value}
                mask = selection_mask(inputs, **settings)
                for period, (lo, hi) in periods.items():
                    result = evaluate(mask, forward, hold, lo, hi)
                    if result:
                        rows.append({"variant": f"{name}={value}", "kind": f"one_{name}",
                                     "hold": hold, "period": period, **settings, **result})

        # Every gate dropped in turn, to see which one earns its place.
        for name in ("week", "day", "atr", "dd", "trend"):
            settings = {**CURRENT, name: GRID[name][-1]}
            mask = selection_mask(inputs, **settings)
            for period, (lo, hi) in periods.items():
                result = evaluate(mask, forward, hold, lo, hi)
                if result:
                    rows.append({"variant": f"drop_{name}", "kind": "leave_one_out",
                                 "hold": hold, "period": period, **settings, **result})

        # Full grid, so the best achievable combination is on the record.
        keys = list(GRID)
        for combo in itertools.product(*(GRID[k] for k in keys)):
            settings = dict(zip(keys, combo, strict=True))
            mask = selection_mask(inputs, **settings)
            for period, (lo, hi) in periods.items():
                result = evaluate(mask, forward, hold, lo, hi)
                if result:
                    rows.append({"variant": "grid", "kind": "grid", "hold": hold,
                                 "period": period, **settings, **result})
        print(f"  hold {hold}: {len(rows)} rows so far", flush=True)

    results = pd.DataFrame(rows)
    report(results)
    if out_path:
        results.to_pickle(out_path)
    return results


def report(results):
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 400)
    columns = ["n", "picks", "sel", "base", "delta", "t", "better"]
    for hold in sorted(results.hold.unique()):
        dev = results[(results.hold == hold) & (results.period == "development")]
        hold_out = results[(results.hold == hold) & (results.period == "holdout")]
        single = dev[dev.kind.isin(["baseline", "leave_one_out"]) | dev.kind.str.startswith("one_")]
        joined = (single.set_index("variant")[columns].round(2)
                  .join(hold_out.set_index("variant")[["delta", "t"]].round(2),
                        rsuffix="_holdout"))
        print(f"\n=== hold {hold} sessions: one threshold at a time "
              f"(delta = picks minus same-date universe average, pp) ===")
        print(joined.to_string())

        grid = dev[dev.kind == "grid"].copy()
        if not grid.empty:
            top = grid.nlargest(8, "delta").set_index(
                grid.nlargest(8, "delta").apply(
                    lambda r: f"week>={r.week} day>{r.day} atr<={r.atr} dd<={r.dd} trend={r.trend}",
                    axis=1))
            print(f"\n--- hold {hold}: best eight of {len(grid)} grid combinations "
                  f"(development; read with the holdout column, not alone) ---")
            print(top[columns].round(2).to_string())


if __name__ == "__main__":
    main(*sys.argv[1:])
