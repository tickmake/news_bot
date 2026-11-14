"""Which hold length and sell rule loses least after DNB fees? (#122)

The screen has no shown edge, so this asks a question that holds whatever is
bought: for a ~12,000 NOK position in any stock of stock_list.csv, how do
hold length (5, 10, 20 sessions) and the sell rule change the result once the
59 NOK minimum fee is paid on the way in and out?

Entries are every 5th session for every stock, at the next open, so no
signal is involved. Sell rules:
  time   sell at the close of the last session of the hold
  stop   also sell if the price reaches entry - 2xATR (the stop the bot places);
         a session that opens below the stop sells at that open (gapped)
  wide   like stop, at 3xATR
  trail  the same 2xATR distance, but following the highest close since entry

    python research/fetch_stock_list.py /tmp/stock_list_10y.pkl
    python research/hold_exit_study.py /tmp/stock_list_10y.pkl

Caveats: today's list, so companies that failed are missing (it flatters
every rule alike; compare rules, don't trust levels). Entries 5 sessions
apart overlap for the 10- and 20-session holds, so there are fewer
independent results than rows. Currency exchange fees for non-NOK stocks
are not included in the tables above the "FX COST SENSITIVITY" heading.

Costs, and what "net" means (P2-5). `gross` is the price return in the
stock's own currency. `net` is gross minus the broker fee both ways, as it
always was. The FX section at the end takes `net` and subtracts the two
currency conversions of a NOK investor for the four illustrative scenarios of
`fx_costs.py`; the fee is not touched, and the 0 bps row is `net` again.
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # also when loaded by path, as the tests do
import fx_costs

POSITION_NOK = 12_000.0
FEE_MIN_NOK = 59.0           # SETTINGS.broker_fee_min_nok
FEE_PCT = 0.12               # SETTINGS.broker_fee_pct
FEE_SIDE_PCT = max(FEE_MIN_NOK, POSITION_NOK * FEE_PCT / 100) / POSITION_NOK * 100
STOP_ATR_MULTIPLE = 2.0      # SETTINGS.risk_stop_atr_multiple
HOLDS = (5, 10, 20)
RULES = ("time", "stop", "wide", "trail")
WIDE_ATR_MULTIPLE = 3.0
ENTRY_EVERY = 5
SPLIT = pd.Timestamp("2021-11-01")


def _trades(symbol, bars, region):
    bars = bars.copy()
    bars.index = pd.to_datetime(bars.index.strftime("%Y-%m-%d"))
    bars = bars[~bars.index.duplicated()].dropna(subset=["Open", "High", "Low", "Close"])
    close, high, low, open_ = (bars[c].to_numpy() for c in ("Close", "High", "Low", "Open"))
    true_range = np.maximum.reduce([
        high - low,
        np.abs(high - np.roll(close, 1)),
        np.abs(low - np.roll(close, 1)),
    ])
    true_range[0] = high[0] - low[0]
    atr_pct = pd.Series(true_range).rolling(14).mean().to_numpy() / close * 100

    rows = []
    for signal in range(20, len(bars) - max(HOLDS) - 1, ENTRY_EVERY):
        entry = signal + 1
        entry_price = open_[entry]
        if not (np.isfinite(atr_pct[signal]) and entry_price > 0):
            continue
        distance = STOP_ATR_MULTIPLE * atr_pct[signal] / 100
        for hold in HOLDS:
            last = entry + hold - 1
            for rule in RULES:
                stop = entry_price * (1 - (distance * WIDE_ATR_MULTIPLE / STOP_ATR_MULTIPLE
                                           if rule == "wide" else distance))
                best = entry_price
                exit_price, stopped, gapped = close[last], False, False
                for day in range(entry, last + 1):
                    if rule != "time":
                        if day > entry and open_[day] <= stop:
                            exit_price, stopped, gapped = open_[day], True, True
                            break
                        if low[day] <= stop:
                            exit_price, stopped = stop, True
                            break
                    if rule == "trail":
                        best = max(best, close[day])
                        stop = max(stop, best * (1 - distance))
                gross = (exit_price / entry_price - 1) * 100
                rows.append((symbol, region, bars.index[entry], hold, rule, gross,
                             gross - 2 * FEE_SIDE_PCT, stopped, gapped))
    return rows


def _table(frame):
    grouped = frame.groupby(["hold", "rule"], sort=True)
    out = pd.DataFrame({
        "trades": grouped.size(),
        "gross %": grouped["gross"].mean(),
        "net %": grouped["net"].mean(),
        "median net %": grouped["net"].median(),
        "worst 5% net": grouped["net"].quantile(0.05),
        "net > 0": grouped["net"].apply(lambda s: (s > 0).mean() * 100),
        "stopped %": grouped["stopped"].mean() * 100,
        "gapped % of stops": grouped.apply(
            lambda g: g["gapped"].sum() / g["stopped"].sum() * 100 if g["stopped"].sum() else 0.0,
            include_groups=False),
    })
    # The same 20 sessions filled with back-to-back trades of this length.
    out["net per 20 sessions"] = out["net %"] * 20 / out.index.get_level_values("hold")
    return out.round(2)


def _fx_table(frame, bps):
    """Every hold and rule under one FX scenario, beside the unchanged gross and after-fee means."""
    cost, after = fx_costs.column("fx_cost", bps), fx_costs.column("after_fx", bps)
    grouped = frame.groupby(["hold", "rule"], sort=True)
    out = pd.DataFrame({
        "trades": grouped.size(),
        "charged": grouped["currency"].apply(lambda s: int(s.map(fx_costs.is_converted).sum())),
        "gross %": grouped["gross"].mean(),
        "after fees %": grouped["net"].mean(),
        "FX cost %": grouped[cost].mean(),
        "after fees+FX %": grouped[after].mean(),
        "vs 0 bps": grouped[after].mean() - grouped["net"].mean(),
        "median": grouped[after].median(),
        "worst 5%": grouped[after].quantile(0.05),
        "> 0": grouped[after].apply(lambda s: (s > 0).mean() * 100),
        "total FX cost NOK": grouped[cost].sum() * POSITION_NOK / 100,
    })
    out["per 20 sessions"] = out["after fees+FX %"] * 20 / out.index.get_level_values("hold")
    return out.round({"total FX cost NOK": 0, "FX cost %": 3, "vs 0 bps": 3}).round(2)


def fx_sensitivity(frame):
    """The FX section, as printed lines. `frame` is the trades with a `currency` column."""
    frame = fx_costs.add_fx_columns(frame.copy(), "gross", "net")
    lines = ["FX COST SENSITIVITY (illustrative; not verified broker pricing)"]
    lines += fx_costs.describe(frame)
    lines += [
        "'gross %' is the price return in the stock's own currency. 'after fees %' is gross minus the",
        f"broker fee of {FEE_SIDE_PCT:.2f}% each way: the 'net %' of the tables above, unchanged. 'after fees+FX %'",
        f"subtracts the FX cost from that. 'total FX cost NOK' sums it over the row's trades at {POSITION_NOK:,.0f} NOK each.",
        "'median', 'worst 5%', '> 0' and 'per 20 sessions' are after fees and FX. All years, all stocks.",
    ]
    for name, bps in fx_costs.FX_SCENARIOS:
        lines += ["", f"{name.upper()}: {bps:g} bps per conversion", _fx_table(frame, bps).to_string()]

    # Does the cost change which rule or which hold comes out best?
    lines += ["", "BEST RULE PER HOLD AND BEST HOLD, AFTER FEES AND FX (mean)"]
    for _name, bps in fx_costs.FX_SCENARIOS:
        mean = frame.groupby(["hold", "rule"])[fx_costs.column("after_fx", bps)].mean()
        best_rule = ", ".join(f"{hold}: {mean[hold].idxmax()} {mean[hold].max():+.2f}" for hold in HOLDS)
        per_20 = {hold: mean[(hold, "time")] * 20 / hold for hold in HOLDS}
        lines.append(f"{bps:>4g} bps  best rule by hold -> {best_rule};  time rule per 20 sessions -> "
                     + ", ".join(f"{hold}: {value:+.2f}" for hold, value in per_20.items()))

    # NOK trades pay nothing and dilute the mean, so show who pays.
    lines += ["", "20-SESSION HOLD BY CURRENCY, AFTER FEES AND FX (mean %, time rule / wide rule)"]
    held = frame[frame["hold"] == 20]
    header = f"{'currency':<10}{'trades':>8}{'after fees':>16}"
    header += "".join(f"{f'{bps:g} bps':>16}" for _name, bps in fx_costs.FX_SCENARIOS)
    lines.append(header)
    for currency, part in held.groupby("currency", sort=True):
        def pair(name, part=part):
            means = part.groupby("rule")[name].mean()
            return f"{means['time']:+.2f} / {means['wide']:+.2f}"
        row = f"{currency:<10}{len(part[part['rule'] == 'time']):>8}{pair('net'):>16}"
        row += "".join(f"{pair(fx_costs.column('after_fx', bps)):>16}" for _name, bps in fx_costs.FX_SCENARIOS)
        lines.append(row)
    return lines


def main(path):
    with open(path, "rb") as handle:
        bars, meta = pickle.load(handle)  # noqa: S301 - written by fetch_stock_list.py
    rows = []
    for symbol, history in bars.items():
        rows.extend(_trades(symbol, history, meta[symbol]["region"]))
    frame = pd.DataFrame(rows, columns=["symbol", "region", "date", "hold", "rule", "gross", "net",
                                        "stopped", "gapped"])
    print(f"{frame['symbol'].nunique()} stocks, {frame['date'].min():%Y-%m-%d} to {frame['date'].max():%Y-%m-%d}; "
          f"fee {FEE_SIDE_PCT:.2f}% each way on {POSITION_NOK:,.0f} NOK\n")
    pd.set_option("display.width", 200)
    for label, part in (("ALL YEARS", frame),
                        (f"FIRST PART (before {SPLIT:%Y-%m-%d})", frame[frame["date"] < SPLIT]),
                        (f"CHECK PART (from {SPLIT:%Y-%m-%d})", frame[frame["date"] >= SPLIT]),
                        ("OSLO ONLY, ALL YEARS", frame[frame["region"] == "Oslo"])):
        print(label)
        print(_table(part).to_string())
        print()

    # Is the ranking stable year by year? Net per 20 sessions, time rule.
    yearly = frame[frame["rule"] == "time"].assign(year=frame["date"].dt.year)
    by_year = yearly.groupby(["year", "hold"])["net"].mean().unstack() * 20 / np.array(HOLDS)
    print("NET PER 20 SESSIONS BY YEAR (time rule)")
    print(by_year.round(2).to_string())
    print()
    rule_by_year = (frame[frame["hold"] == 20].assign(year=frame["date"].dt.year)
                    .groupby(["year", "rule"])["net"].mean().unstack())
    print("20-SESSION HOLD, NET BY YEAR AND RULE")
    print(rule_by_year.round(2).to_string())
    print()
    frame["currency"] = [fx_costs.currency_of(symbol, meta[symbol].get("currency")) for symbol in frame["symbol"]]
    print("\n".join(fx_sensitivity(frame)))


if __name__ == "__main__":
    main(sys.argv[1])
